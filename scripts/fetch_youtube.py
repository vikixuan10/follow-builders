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
# 这次运行新记的「已收过」先写到这里，邮件确认发出后才由 build_page.py 并入 STATE_PATH。
# 后面哪一步失败，这批视频第二天还会再取（2026-10-18 起）。
STATE_NEW_PATH = USER_DIR / "seen-videos.new.json"
# 这次没取到字幕、下次再试的视频（标题等信息一并记下，最多留 PENDING_DAYS 天，不受 48 小时回看的限制）。
PENDING_PATH = USER_DIR / "pending-videos.json"
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

PENDING_DAYS = 7
# 发布不到这么久就没有字幕的，多半是 YouTube 还没生成自动字幕，下次再试；超过了才算这个视频没有字幕。
NO_TRANSCRIPT_GRACE_HOURS = 24
# 确定取不到的：视频不存在、有年龄限制、编号不对。只有这几类（加上取到了字幕）才记为已收过。
PERMANENT_ERRORS = {"VideoUnavailable", "AgeRestricted", "InvalidVideoId"}
NO_TRANSCRIPT_ERRORS = {"TranscriptsDisabled", "NoTranscriptFound", "no_transcript"}

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


class _VideoTimeout(BaseException):
    """继承 BaseException：字幕库内部的 except Exception 拦不住它，60 秒一到一定能停下来。"""
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
    """写到 STATE_NEW_PATH，不直接改正式记录；邮件发出后由 build_page.py 并入。"""
    USER_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_NEW_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def load_pending() -> Dict[str, Dict[str, Any]]:
    try:
        with open(PENDING_PATH, "r", encoding="utf-8") as f:
            pending = json.load(f)
    except Exception:
        return {}
    cutoff = time.time() - PENDING_DAYS * 24 * 3600
    return {vid: p for vid, p in pending.items() if p.get("first", 0) > cutoff}


def save_pending(pending: Dict[str, Dict[str, Any]]) -> None:
    USER_DIR.mkdir(parents=True, exist_ok=True)
    with open(PENDING_PATH, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)


def _err(e: BaseException) -> str:
    """错误写成「类型: 说明的最后 160 个字」，代理的状态码一般在末尾。"""
    return "%s: %s" % (type(e).__name__, str(e).replace("\n", " ")[-160:])


def classify(error: Optional[str]) -> str:
    """把一次取字幕的结果分类，决定记不记已收过：
    ok / permanent（确定取不到）/ no_transcript（没有字幕）/ proxy_account（代理账户的问题）/
    blocked（被拦、超时、连不上）/ temporary（其余，比如 YouTube 改版、字幕库解析失败）。"""
    if not error:
        return "ok"
    name = error.split(":", 1)[0].strip()
    e = error.lower()
    if name in PERMANENT_ERRORS:
        return "permanent"
    if name in NO_TRANSCRIPT_ERRORS:
        return "no_transcript"
    if any(k in e for k in ("402", "407", "payment required", "proxy authentication")):
        return "proxy_account"
    if _is_rate_limit_error(error):
        return "blocked"
    return "temporary"


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


CONSENT_COOKIES = {"CONSENT": "YES+cb.20210328-17-p0.en+FX+000", "SOCS": "CAI"}
_AGO = re.compile(r"(\d+)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|wks?|w|months?|mos?|mo|years?|yrs?|y)\b\s*ago", re.I)
_UNIT_HOURS = {"s": 0, "m": 1 / 60, "h": 1, "d": 24, "w": 24 * 7, "mo": 24 * 30, "y": 24 * 365}


def _ago_to_datetime(text: str) -> Optional[datetime]:
    """「5h ago」「1d ago」「3 weeks ago」→ 大概的发布时间。只到天这一级精度，够 48 小时的回看用。"""
    m = _AGO.search(text or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit.startswith("mo"):
        key = "mo"
    elif unit.startswith("mi") or unit == "m":
        key = "m"
    else:
        key = unit[0]
    return datetime.now(timezone.utc) - timedelta(hours=n * _UNIT_HOURS[key])


def fetch_channel_page(channel_id: str) -> Optional[List[Dict[str, Any]]]:
    """订阅源（RSS）取不到时的备用办法：直接读频道的「视频」网页（2026-10-18 起，那天 YouTube 的
    订阅源对所有频道都返回 404）。返回的条目格式和订阅源一样：published / link / title。"""
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    try:
        res = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                           cookies=CONSENT_COOKIES, timeout=20)
        if res.status_code != 200:
            return None
        m = re.search(r"var ytInitialData = (\{.*?\});</script>", res.text, re.S)
        if not m:
            return None
        data = json.loads(m.group(1))
    except Exception:
        return None
    out: List[Dict[str, Any]] = []
    seen = set()

    def add(vid, title, when_text):
        if not vid or vid in seen:
            return
        published = _ago_to_datetime(when_text)
        if not published:
            return
        seen.add(vid)
        out.append({"published": published.isoformat(), "link": f"https://www.youtube.com/watch?v={vid}",
                    "title": title or "Untitled"})

    def walk(o):
        if isinstance(o, dict):
            lv = o.get("lockupViewModel")
            if isinstance(lv, dict) and lv.get("contentType", "").endswith("VIDEO"):
                md = (lv.get("metadata") or {}).get("lockupMetadataViewModel") or {}
                parts = [((p.get("text") or {}).get("content") or "")
                         for row in ((md.get("metadata") or {}).get("contentMetadataViewModel") or {}).get("metadataRows", [])
                         for p in row.get("metadataParts", [])]
                add(lv.get("contentId"), (md.get("title") or {}).get("content"), " ".join(parts))
            vr = o.get("videoRenderer")
            if isinstance(vr, dict):
                title = "".join(r.get("text", "") for r in ((vr.get("title") or {}).get("runs") or []))
                add(vr.get("videoId"), title, (vr.get("publishedTimeText") or {}).get("simpleText", ""))
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(data)
    return out or None


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
    return any(k in e for k in ("ipblocked", "429", "too many", "blocking", "requestblocked", "timeout", "timed out",
                                "max retries exceeded", "proxyerror", "connectionerror", "connection aborted"))


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
        classify(result.get("error")) == "blocked"
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
        return {"text": None, "lang": None, "truncated": False, "error": _err(e)}

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
        return {"text": None, "lang": None, "truncated": False, "error": _err(e)}

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
    pending_store = load_pending()
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=args.lookback)

    if not args.no_transcripts:
        get_api()
        log("字幕请求：" + ("走 Webshare 代理" if USING_PROXY else "直连（没有配置代理）"))

    videos: List[Dict[str, Any]] = []
    errors: List[str] = []
    channels_with_new = 0
    pending: List[tuple] = []   # (channel, entry) 等待抓字幕的视频
    capped: List[tuple] = []    # 超过每个频道上限的视频：不取字幕，只列标题
    used_page: List[str] = []   # 订阅源取不到、改读频道网页的频道

    for i, ch in enumerate(channels, 1):
        if not ch.get("channelId"):
            errors.append(f"No channelId for {ch['name']}")
            continue

        print(f"[{i}/{len(channels)}] Checking {ch['name']}...", file=sys.stderr, flush=True)
        entries = fetch_channel_rss(ch["channelId"])
        if not entries:
            entries = fetch_channel_page(ch["channelId"])
            if entries:
                used_page.append(ch["name"])
            time.sleep(random.uniform(0.3, 0.8))
        if entries is None:
            errors.append(f"YouTube：{ch['name']} 的视频列表没取到（订阅源和频道网页都没取到）")
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

    # 上次没取到字幕的视频（7 天内），这次排在最前面再试；不受 48 小时回看和每频道上限的限制
    by_name = {c["name"]: c for c in channels}
    queued = {ne["video_id"] for _, ne in pending + capped}
    retries: List[tuple] = []
    for vid, pe in pending_store.items():
        ch = by_name.get(pe.get("channel"))
        if not ch or vid in state or vid in queued:
            continue
        published = parse_rfc3339(pe.get("published", "")) or now_utc
        retries.append((ch, {"video_id": vid, "title": pe.get("title", "Untitled"),
                             "link": pe.get("link", ""), "published": published}))
    if retries:
        log(f"  上次没取到字幕的 {len(retries)} 个视频，这次再试")
    pending = retries + pending

    def record(ch: Dict[str, Any], ne: Dict[str, Any], transcript_info: Dict[str, Any], mark_seen: bool = True) -> None:
        """把一个视频写进结果列表。
        mark_seen=True：取到了字幕，或者确定取不到，记为已收过（邮件发出后才正式生效），从待补清单里拿掉。
        mark_seen=False：这次只列标题，放进待补清单，下次运行再试。疑似 Shorts 的只记已收过、不收录。"""
        vid = ne["video_id"]
        transcript_text = transcript_info.get("text") or ""
        if transcript_text and len(transcript_text) < MIN_TRANSCRIPT_CHARS:
            print(f"    ⏭ Skipped (likely Shorts, {len(transcript_text)} chars): {ne['title'][:50]}", file=sys.stderr)
            state[vid] = time.time()
            pending_store.pop(vid, None)
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
        if mark_seen:
            state[vid] = time.time()
            pending_store.pop(vid, None)
        else:
            keep_for_later(ch, ne)

    def keep_for_later(ch: Dict[str, Any], ne: Dict[str, Any]) -> None:
        old = pending_store.get(ne["video_id"]) or {}
        pending_store[ne["video_id"]] = {
            "channel": ch["name"], "title": ne["title"], "link": ne.get("link", ""),
            "published": ne["published"].isoformat(), "first": old.get("first", time.time()),
        }

    retry_kinds: Dict[str, int] = {}   # 这次没取到、下次再试的，按错误类型计数
    proxy_problem: List[str] = []

    def note_retry(info: Dict[str, Any]) -> None:
        err = str(info.get("error") or "")
        name = err.split(":", 1)[0].strip() or "未知"
        retry_kinds[name] = retry_kinds.get(name, 0) + 1
        if classify(err) == "proxy_account" and not proxy_problem:
            proxy_problem.append(err[-120:])

    def transcript_pass(items: List[tuple]) -> List[tuple]:
        """取一轮字幕。被拦的视频攒进 deferred 返回；连续被拦到熔断线，剩下的全部 defer。"""
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
            kind = classify(info.get("error"))
            log(f"    {ch['name']}｜{ne['title'][:50]}｜{time.time() - t0:.1f}s｜"
                + ("取到字幕" if info.get("text") else ("没取到（%s）：%s" % (kind, str(info.get("error"))[-160:]))))
            if kind in ("blocked", "proxy_account"):
                consecutive_blocks += 1
                deferred.append((ch, ne, info))
                breaker = PROXY_CIRCUIT_BREAKER if USING_PROXY else IP_BLOCK_CIRCUIT_BREAKER
                if consecutive_blocks >= breaker:
                    rest = items[idx + 1:]
                    deferred.extend((c, n, {"error": "skipped_after_blocks"}) for c, n in rest)
                    log(f"  ⛔ 连续 {consecutive_blocks} 次没取到，本轮剩余 {len(rest)} 个视频先搁置")
                    break
            else:
                consecutive_blocks = 0
                if kind in ("ok", "permanent"):
                    record(ch, ne, info)
                elif kind == "no_transcript":
                    age_h = (now_utc - ne["published"]).total_seconds() / 3600
                    if age_h >= NO_TRANSCRIPT_GRACE_HOURS:
                        record(ch, ne, info)
                    else:
                        record(ch, ne, info, mark_seen=False)
                        note_retry(info)
                else:  # temporary：YouTube 改版、字幕库解析失败等，先列标题，下次再试
                    record(ch, ne, info, mark_seen=False)
                    note_retry(info)
            if USING_PROXY:
                time.sleep(random.uniform(PROXY_DELAY_MIN, PROXY_DELAY_MAX))
            else:
                time.sleep(random.uniform(TRANSCRIPT_DELAY_MIN, TRANSCRIPT_DELAY_MAX))
        return deferred

    late: List[tuple] = []  # 时间不够、没来得及取字幕的：这次不收录，放进待补清单，下次再取

    for ch, ne in capped:
        record(ch, ne, {"text": None, "lang": None, "truncated": False, "error": "over_channel_cap"})
    if capped:
        log(f"  同一频道超过 {PER_CHANNEL_CAP} 个的 {len(capped)} 个视频只列标题，不取字幕")

    if args.no_transcripts:
        for ch, ne in pending:
            record(ch, ne, {"text": None, "lang": None, "truncated": False, "error": "skipped"}, mark_seen=False)
    else:
        deferred = transcript_pass(pending)
        for ch, ne, info in deferred:
            note_retry(info)
            record(ch, ne, {"text": None, "lang": None, "truncated": False,
                            "error": "skipped_ip_block: " + str(info.get("error") or "")[-120:]}, mark_seen=False)
        if deferred:
            log(f"  ⛔ {len(deferred)} 个视频这次没取到字幕，只带标题和链接返回，下次再试")
    for ch, ne in late:
        keep_for_later(ch, ne)

    if used_page:
        errors.append("YouTube：%d 个频道的订阅源取不到，改读了频道网页（发布时间只精确到天）" % len(used_page))
    n_retry = sum(retry_kinds.values())
    if n_retry:
        errors.append("YouTube：%d 个视频这次没取到字幕（%s），下次会再试" % (
            n_retry, "、".join("%s %d 个" % (k, c) for k, c in sorted(retry_kinds.items(), key=lambda kv: -kv[1]))))
    if proxy_problem:
        errors.append("YouTube：代理账户可能出了问题（%s），请看 Webshare 后台的流量和续费" % proxy_problem[0])
    if late:
        errors.append(f"YouTube：时间不够，{len(late)} 个视频留到下次再取字幕")

    # Save state（已收过的写到 seen-videos.new.json，邮件发出后才并入正式记录）
    if not args.dry_run:
        save_state(state)
        save_pending(pending_store)
    log(f"结束：收录 {len(videos)} 个视频，其中有字幕 {sum(1 for v in videos if v.get('transcript'))} 个；"
        f"留到下次 {n_retry + len(late)} 个；用时 {time.time() - _START:.0f} 秒")

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
