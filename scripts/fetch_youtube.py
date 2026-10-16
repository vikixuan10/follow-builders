#!/usr/bin/env python3
"""
Fetch YouTube: RSS → new videos → transcripts
=============================================

For each YouTube channel in config/my-sources.json:
 1. Fetch its RSS feed (published videos, newest first, ~15 items)
 2. Filter for videos published in the last `lookback_hours`
 3. Skip videos we've already seen (state file at ~/.follow-builders/seen-videos.json)
 4. Fetch the transcript (if available) via youtube-transcript-api
 5. Output everything as a single JSON blob to stdout

Usage:
    python3 fetch_youtube.py                 # default: 48h lookback
    python3 fetch_youtube.py --lookback 72   # custom lookback window
    python3 fetch_youtube.py --dry-run       # don't update state file

Output (to stdout): JSON with shape:
    {
      "generatedAt": "2026-04-06T00:00:00Z",
      "videos": [
        {
          "channelName": "OpenAI",
          "category": "AI-官方/研究",
          "videoId": "abc123",
          "title": "...",
          "url": "https://youtube.com/watch?v=abc123",
          "publishedAt": "2026-04-05T14:30:00Z",
          "duration": null,           # RSS doesn't include duration
          "transcript": "...",        # full transcript, or null if unavailable
          "transcriptLang": "en",     # language of transcript
          "transcriptTruncated": false
        },
        ...
      ],
      "stats": {
        "channelsChecked": 40,
        "channelsWithNew": 7,
        "videosFound": 12,
        "videosWithTranscript": 10
      },
      "errors": [...]
    }
"""

import argparse
import json
import os
import random
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import feedparser
import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

# -- Paths --------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
SOURCES_PATH = REPO_DIR / "config" / "my-sources.json"

USER_DIR = Path.home() / ".follow-builders"
STATE_PATH = USER_DIR / "seen-videos.json"
ENV_PATH = USER_DIR / ".env"

# -- Constants ----------------------------------------------------------------

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Cap transcript length so long videos don't blow up the digest JSON.
# The LLM will summarize it further; we don't need every word.
MAX_TRANSCRIPT_CHARS = 120_000  # ~30k tokens; covers ~90 min of podcast/interview

# Shorts filter: videos with transcripts shorter than this are likely Shorts (≤60s)
MIN_TRANSCRIPT_CHARS = 300  # ~60 seconds of speech ≈ 200-400 chars

# Preferred transcript languages (in order)
TRANSCRIPT_LANG_PREF = ["en", "en-US", "en-GB", "zh-Hans", "zh-CN", "zh"]

# 抓字幕之间的随机停顿（秒）。拉长 + 随机化，降低被 YouTube 判定为爬虫而限流的概率。
# 抓一轮会比原来慢（55 个视频约多花十几分钟），但定时任务在早上跑，无所谓。
TRANSCRIPT_DELAY_MIN = 5.0
TRANSCRIPT_DELAY_MAX = 10.0

# 遇到限流（429 / IpBlocked）时只重试一次，等 15 秒。
TRANSCRIPT_MAX_RETRIES = 1
RETRY_BACKOFF_BASE = 15.0

# 熔断保护：连续这么多个视频都因限流失败后，判定整个 IP 已被拦，
# 剩下的视频不再请求字幕，只带着标题和链接返回。
IP_BLOCK_CIRCUIT_BREAKER = 2

# 熔断后不做冷却重试（轮数为 0）。
# 2026-09-18 加过「等 20 分钟再补抓，最多两轮」，但外层 prepare_digest.py 只给本脚本 10 分钟，
# 于是每次都在冷却途中被强行终止，连视频列表一起丢掉：9 月 20 日到 10 月 12 日一个视频都没有。
# 2026-10-12 实测这条宽带的 IP 被字幕接口长期拦截（三周没有自愈，换客户端、换 yt-dlp 都是 429），
# 冷却没有意义。被拦时尽快结束、保住视频列表，同时少打几次接口，给它恢复的机会。
IP_BLOCK_COOLDOWN_SECONDS = 20 * 60
IP_BLOCK_MAX_COOLDOWN_ROUNDS = 0

# 走代理时的重试：每次请求都会换一个住宅 IP，所以被拦了马上换 IP 再试，不用久等。
# 字幕库自己遇到「被拦」也会换 IP 重试（PROXY_LIBRARY_RETRIES 次），外层只再补一次。
PROXY_MAX_RETRIES = 1
PROXY_RETRY_WAIT = 2.0
PROXY_LIBRARY_RETRIES = 3
# 走代理时每个请求的出口 IP 都不同，连着失败两个视频不代表全被拦，放宽到连续 5 个再放弃。
PROXY_CIRCUIT_BREAKER = 5
# 走代理时视频之间只停一两秒：停顿是为了保护家里的 IP，走代理时每次出口 IP 都不同。
PROXY_DELAY_MIN = 0.5
PROXY_DELAY_MAX = 1.5

# 时间上的三道保险（2026-10-14 至 16 日连着三天，取字幕整轮超过外层给的 10 分钟，被整个掐掉，
# 连视频列表一起丢了。原因是字幕库发请求不设超时，碰上一个不响应的代理出口就一直挂着）：
REQUEST_TIMEOUT = (10, 25)        # 每个网络请求：连接最多等 10 秒，读数据最多等 25 秒
PER_VIDEO_TIMEOUT = 60            # 每个视频（含重试）最多 60 秒，超了当作这次没取到，留到下次
TRANSCRIPT_BUDGET_SECONDS = 6 * 60  # 从脚本启动算起，超过 6 分钟就不再开始取新的字幕

# 同一个频道一次最多取几个视频的字幕（2026-10-16 Weiwei 定）。多出来的只列标题，
# 也记为已见，不再回来。大会一次上传二十多个演讲时，不至于占满一整页。
PER_CHANNEL_CAP = 5

LOG_DIR = Path.home() / ".follow-builders" / "logs"

# Prune state file entries older than this
STATE_TTL_DAYS = 14


# -- 字幕请求走不走代理 ---------------------------------------------------------
# 这条家用宽带的 IP 被 YouTube 字幕接口长期拦截（见上面的说明）。字幕库官方推荐的办法是
# Webshare 的轮换住宅代理（Rotating Residential），库里有专门的接入方式。
# 在 ~/.follow-builders/.env 里写上下面两行就会自动启用；不写就直连，和以前一样。
#   WEBSHARE_PROXY_USERNAME=...
#   WEBSHARE_PROXY_PASSWORD=...
# 只有取字幕的请求走代理，查视频列表（RSS）不走，所以流量很小。

_API: Optional[YouTubeTranscriptApi] = None
USING_PROXY = False
_START = time.time()
_LOG_FILE = None


def log(msg: str) -> None:
    """进度同时写到屏幕和当天的日志文件（~/.follow-builders/logs/youtube-日期.log），出了问题有据可查。"""
    global _LOG_FILE
    print(msg, file=sys.stderr, flush=True)
    try:
        if _LOG_FILE is None:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            _LOG_FILE = open(LOG_DIR / ("youtube-%s.log" % datetime.now().strftime("%Y-%m-%d")), "a", encoding="utf-8")
            _LOG_FILE.write("\n=== %s 开始 ===\n" % datetime.now().strftime("%H:%M:%S"))
        _LOG_FILE.write("[%5.0fs] %s\n" % (time.time() - _START, msg))
        _LOG_FILE.flush()
    except Exception:
        pass


class _TimeoutSession(requests.Session):
    """给每个请求加上默认超时。字幕库自己不设超时。"""
    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        return super().request(*args, **kwargs)


class _VideoTimeout(Exception):
    pass


def _on_alarm(signum, frame):
    raise _VideoTimeout()


def _load_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not ENV_PATH.exists():
        return env
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        pass
    return env


def get_api() -> YouTubeTranscriptApi:
    """整个进程共用一个字幕客户端；有代理账号就走代理。"""
    global _API, USING_PROXY
    if _API is not None:
        return _API
    env = _load_env()
    user = os.environ.get("WEBSHARE_PROXY_USERNAME") or env.get("WEBSHARE_PROXY_USERNAME")
    password = os.environ.get("WEBSHARE_PROXY_PASSWORD") or env.get("WEBSHARE_PROXY_PASSWORD")
    if user and password:
        from youtube_transcript_api.proxies import WebshareProxyConfig
        _API = YouTubeTranscriptApi(
            proxy_config=WebshareProxyConfig(proxy_username=user, proxy_password=password,
                                             retries_when_blocked=PROXY_LIBRARY_RETRIES),
            http_client=_TimeoutSession(),
        )
        USING_PROXY = True
    else:
        _API = YouTubeTranscriptApi(http_client=_TimeoutSession())
        USING_PROXY = False
    return _API


# -- State management ---------------------------------------------------------

def load_state() -> Dict[str, float]:
    """Returns {video_id: first_seen_timestamp}. Auto-prunes old entries."""
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return {}
    # Prune old entries
    cutoff = time.time() - STATE_TTL_DAYS * 24 * 3600
    return {vid: ts for vid, ts in state.items() if ts > cutoff}


def save_state(state: Dict[str, float]) -> None:
    USER_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# -- RSS fetching -------------------------------------------------------------

def fetch_channel_rss(channel_id: str) -> Optional[List[Dict[str, Any]]]:
    """Fetch a YouTube channel's RSS feed. Returns list of entries or None on error."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        res = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        if res.status_code != 200:
            return None
        feed = feedparser.parse(res.content)
        if feed.bozo and not feed.entries:
            return None
        return list(feed.entries)
    except Exception:
        return None


def parse_rfc3339(ts: str) -> Optional[datetime]:
    """Parse the RSS <published> timestamp into a UTC datetime."""
    try:
        # feedparser normalizes to struct_time; we can re-parse the raw string
        # Format: 2026-04-05T14:30:00+00:00
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


# -- Transcript fetching ------------------------------------------------------

def _is_rate_limit_error(err: Optional[str]) -> bool:
    """限流类错误（IP 被封 / 429）才值得重试；字幕禁用、无字幕等不必重试。"""
    if not err:
        return False
    e = err.lower()
    return any(k in e for k in ("ipblocked", "429", "too many", "blocking", "requestblocked", "timeout", "timed out"))


def fetch_transcript(video_id: str) -> Dict[str, Any]:
    """抓字幕，最多 PER_VIDEO_TIMEOUT 秒；超时当作限流处理（留到下次，不标记已见）。"""
    old = signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(PER_VIDEO_TIMEOUT)
    try:
        return _fetch_transcript_with_retries(video_id)
    except _VideoTimeout:
        return {"text": None, "lang": None, "truncated": False,
                "error": "timeout: no answer within %ds (counted as blocked)" % PER_VIDEO_TIMEOUT}
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _fetch_transcript_with_retries(video_id: str) -> Dict[str, Any]:
    """抓字幕，遇到限流类错误时按指数退避重试若干次。"""
    result = _fetch_transcript_once(video_id)
    attempt = 0
    max_retries = PROXY_MAX_RETRIES if USING_PROXY else TRANSCRIPT_MAX_RETRIES
    while (
        result.get("error")
        and _is_rate_limit_error(result["error"])
        and attempt < max_retries
    ):
        wait = PROXY_RETRY_WAIT if USING_PROXY else RETRY_BACKOFF_BASE * (2 ** attempt)
        log(f"    ⏳ 限流，{wait:.0f}s 后重试 ({attempt + 1}/{max_retries})...")
        time.sleep(wait)
        result = _fetch_transcript_once(video_id)
        attempt += 1
    return result


def _fetch_transcript_once(video_id: str) -> Dict[str, Any]:
    """
    Fetch transcript for a video, preferring English, falling back to Chinese.
    Returns: {"text": str|None, "lang": str|None, "truncated": bool, "error": str|None}
    """
    try:
        api = get_api()
        transcript_list = api.list(video_id)
    except (TranscriptsDisabled, VideoUnavailable) as e:
        return {"text": None, "lang": None, "truncated": False, "error": type(e).__name__}
    except Exception as e:
        return {"text": None, "lang": None, "truncated": False, "error": str(e)[:200]}

    # Try each preferred language
    transcript = None
    for lang in TRANSCRIPT_LANG_PREF:
        try:
            transcript = transcript_list.find_transcript([lang])
            break
        except NoTranscriptFound:
            continue

    # Fallback: take any transcript
    if transcript is None:
        try:
            for t in transcript_list:
                transcript = t
                break
        except Exception:
            pass

    if transcript is None:
        return {"text": None, "lang": None, "truncated": False, "error": "no_transcript"}

    # Fetch the actual entries
    try:
        entries = transcript.fetch()
    except Exception as e:
        return {"text": None, "lang": None, "truncated": False, "error": str(e)[:200]}

    # entries is a FetchedTranscript object with .snippets, each has .text + .start + .duration
    # Convert to "[MM:SS] text" format so the LLM can use timestamps
    lines = []
    total_chars = 0
    truncated = False
    snippets = getattr(entries, "snippets", None) or list(entries)
    for snip in snippets:
        text = getattr(snip, "text", None)
        start = getattr(snip, "start", None)
        if text is None and isinstance(snip, dict):
            text = snip.get("text")
            start = snip.get("start")
        if text is None:
            continue
        # Clean up newlines in transcript text
        text = text.replace("\n", " ").strip()
        if not text:
            continue
        mm = int(start // 60) if start is not None else 0
        ss = int(start % 60) if start is not None else 0
        line = f"[{mm:02d}:{ss:02d}] {text}"
        lines.append(line)
        total_chars += len(line) + 1
        if total_chars > MAX_TRANSCRIPT_CHARS:
            truncated = True
            break

    return {
        "text": "\n".join(lines),
        "lang": getattr(transcript, "language_code", None),
        "truncated": truncated,
        "error": None,
    }


# -- Main ---------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback", type=int, default=48,
                        help="Lookback window in hours (default: 48)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't update state file (for testing)")
    parser.add_argument("--limit-channels", type=int, default=None,
                        help="Only process first N channels (for testing)")
    parser.add_argument("--no-transcripts", action="store_true",
                        help="Skip transcript fetching (RSS only)")
    args = parser.parse_args()

    # Load sources
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        sources = json.load(f)

    channels = sources["youtube_channels"]
    if args.limit_channels:
        channels = channels[:args.limit_channels]

    # Load state
    state = load_state()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.lookback)

    if not args.no_transcripts:
        get_api()
        log("字幕请求：" + ("走 Webshare 代理" if USING_PROXY else "直连（没有配置代理）"))

    videos: List[Dict[str, Any]] = []
    errors: List[str] = []
    channels_with_new = 0
    pending: List[tuple] = []   # (channel, entry) 等待抓字幕的视频
    capped: List[tuple] = []    # 超过每个频道上限的视频：不取字幕，只列标题

    for i, ch in enumerate(channels, 1):
        if not ch.get("channelId"):
            errors.append(f"No channelId for {ch['name']}")
            continue

        print(f"[{i}/{len(channels)}] Checking {ch['name']}...", file=sys.stderr, flush=True)
        entries = fetch_channel_rss(ch["channelId"])
        if entries is None:
            errors.append(f"RSS failed: {ch['name']}")
            continue

        new_entries = []
        for e in entries:
            published_str = e.get("published", "")
            published = parse_rfc3339(published_str)
            if not published:
                continue
            if published < cutoff:
                continue
            # Extract video ID from link
            link = e.get("link", "")
            m = re.search(r"watch\?v=([\w-]{11})", link)
            if not m:
                continue
            video_id = m.group(1)
            if video_id in state:
                continue
            new_entries.append({
                "video_id": video_id,
                "title": e.get("title", "Untitled"),
                "link": link,
                "published": published,
            })

        if not new_entries:
            continue

        channels_with_new += 1
        print(f"  → {len(new_entries)} new video(s)", file=sys.stderr, flush=True)
        new_entries.sort(key=lambda ne: ne["published"], reverse=True)
        pending.extend((ch, ne) for ne in new_entries[:PER_CHANNEL_CAP])
        capped.extend((ch, ne) for ne in new_entries[PER_CHANNEL_CAP:])

    def record(ch: Dict[str, Any], ne: Dict[str, Any], transcript_info: Dict[str, Any], mark_seen: bool = True) -> None:
        """把一个视频写进结果列表并标记已见；疑似 Shorts 的只标记不收录。
        mark_seen=False：这次被拦或超时、只列了标题的视频，不标记已见，下次运行再试着取字幕。"""
        vid = ne["video_id"]
        transcript_text = transcript_info.get("text") or ""
        if transcript_text and len(transcript_text) < MIN_TRANSCRIPT_CHARS:
            print(f"    ⏭ Skipped (likely Shorts, {len(transcript_text)} chars): {ne['title'][:50]}", file=sys.stderr)
            if not args.dry_run:
                state[vid] = time.time()
            return
        videos.append({
            "channelName": ch["name"],
            "category": ch["category"],
            "videoId": vid,
            "title": ne["title"],
            "url": f"https://www.youtube.com/watch?v={vid}",
            "publishedAt": ne["published"].isoformat(),
            "transcript": transcript_info["text"],
            "transcriptLang": transcript_info["lang"],
            "transcriptTruncated": transcript_info["truncated"],
            "transcriptError": transcript_info["error"],
        })
        if mark_seen and not args.dry_run:
            state[vid] = time.time()

    def transcript_pass(items: List[tuple]) -> List[tuple]:
        """抓一轮字幕。限流的视频不落结果、攒进 deferred 返回；熔断后剩余的全部 defer。"""
        deferred: List[tuple] = []
        consecutive_blocks = 0
        for idx, (ch, ne) in enumerate(items):
            if time.time() - _START > TRANSCRIPT_BUDGET_SECONDS:
                rest = items[idx:]
                late.extend(rest)
                log(f"  ⏱ 已用 {time.time() - _START:.0f} 秒，剩下 {len(rest)} 个视频留到下次再取字幕")
                break
            t0 = time.time()
            info = fetch_transcript(ne["video_id"])
            log(f"    {ch['name']}｜{ne['title'][:50]}｜{time.time() - t0:.1f}s｜"
                + ("取到字幕" if info.get("text") else ("没取到：" + str(info.get("error"))[:80])))
            if info.get("error") and _is_rate_limit_error(info["error"]):
                consecutive_blocks += 1
                deferred.append((ch, ne))
                breaker = PROXY_CIRCUIT_BREAKER if USING_PROXY else IP_BLOCK_CIRCUIT_BREAKER
                if consecutive_blocks >= breaker:
                    rest = items[idx + 1:]
                    deferred.extend(rest)
                    log(f"  ⛔ 连续 {consecutive_blocks} 次限流，判定 IP 被封，本轮剩余 {len(rest)} 个视频先搁置")
                    break
            else:
                consecutive_blocks = 0
                record(ch, ne, info)
            if USING_PROXY:
                time.sleep(random.uniform(PROXY_DELAY_MIN, PROXY_DELAY_MAX))
            else:
                time.sleep(random.uniform(TRANSCRIPT_DELAY_MIN, TRANSCRIPT_DELAY_MAX))
        return deferred

    def cooldown(seconds: int) -> None:
        """等限流自愈；每分钟打一行心跳，免得被外层当成卡死。"""
        remaining = seconds
        while remaining > 0:
            step = min(60, remaining)
            time.sleep(step)
            remaining -= step
            print(f"    ⏳ 冷却中，还剩 {remaining // 60} 分钟", file=sys.stderr, flush=True)

    late: List[tuple] = []  # 时间不够、没来得及取字幕的：不收录、不标记已见，下次再取

    for ch, ne in capped:
        record(ch, ne, {"text": None, "lang": None, "truncated": False, "error": "over_channel_cap"})
    if capped:
        log(f"  同一频道超过 {PER_CHANNEL_CAP} 个的 {len(capped)} 个视频只列标题，不取字幕")

    if args.no_transcripts:
        for ch, ne in pending:
            record(ch, ne, {"text": None, "lang": None, "truncated": False, "error": "skipped"})
    else:
        deferred = transcript_pass(pending)
        rounds = 0
        while deferred and rounds < IP_BLOCK_MAX_COOLDOWN_ROUNDS:
            rounds += 1
            print(f"  🕒 {len(deferred)} 个视频因限流搁置，等 {IP_BLOCK_COOLDOWN_SECONDS // 60} 分钟后"
                  f"补抓（第 {rounds}/{IP_BLOCK_MAX_COOLDOWN_ROUNDS} 轮）", file=sys.stderr, flush=True)
            cooldown(IP_BLOCK_COOLDOWN_SECONDS)
            deferred = transcript_pass(deferred)
        if deferred:
            msg = (f"Transcript endpoint is blocking this IP (HTTP 429); "
                   f"{len(deferred)} video(s) returned without transcript")
            errors.append(msg)
            log(f"  ⛔ 字幕接口拦了这个 IP，{len(deferred)} 个视频只带标题和链接返回")
            for ch, ne in deferred:
                record(ch, ne, {"text": None, "lang": None, "truncated": False, "error": "skipped_ip_block"}, mark_seen=False)

    if late:
        errors.append(f"Out of time: {len(late)} video(s) left for the next run")

    # Save state
    if not args.dry_run:
        save_state(state)
    log(f"结束：收录 {len(videos)} 个视频，其中有字幕 {sum(1 for v in videos if v.get('transcript'))} 个；"
        f"留到下次 {len(late)} 个；用时 {time.time() - _START:.0f} 秒")

    videos_with_transcript = sum(1 for v in videos if v.get("transcript"))

    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "lookbackHours": args.lookback,
        "videos": videos,
        "stats": {
            "channelsChecked": len(channels),
            "channelsWithNew": channels_with_new,
            "videosFound": len(videos),
            "videosWithTranscript": videos_with_transcript,
        },
        "errors": errors,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
