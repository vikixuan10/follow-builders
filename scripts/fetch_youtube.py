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
import random
import re
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

# 遇到限流（429 / IpBlocked）时的重试次数与退避基数（秒）：15s → 30s。
TRANSCRIPT_MAX_RETRIES = 2
RETRY_BACKOFF_BASE = 15.0

# 熔断保护：连续这么多个视频都因限流失败后，判定整个 IP 已被封，
# 后续视频不再重试也不再等待，直接快速跳过，避免空跑近一小时。
IP_BLOCK_CIRCUIT_BREAKER = 3

# 熔断后的冷却重试：实测限流多在 15-30 分钟内自愈（2026-09-15 27min、09-18 16min），
# 所以熔断后不直接放弃，等一段时间再对被跳过的视频补抓一轮，最多补几轮。
IP_BLOCK_COOLDOWN_SECONDS = 20 * 60
IP_BLOCK_MAX_COOLDOWN_ROUNDS = 2

# Prune state file entries older than this
STATE_TTL_DAYS = 14


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
    return any(k in e for k in ("ipblocked", "429", "too many", "blocking", "requestblocked"))


def fetch_transcript(video_id: str) -> Dict[str, Any]:
    """抓字幕，遇到限流类错误时按指数退避重试若干次。"""
    result = _fetch_transcript_once(video_id)
    attempt = 0
    while (
        result.get("error")
        and _is_rate_limit_error(result["error"])
        and attempt < TRANSCRIPT_MAX_RETRIES
    ):
        wait = RETRY_BACKOFF_BASE * (2 ** attempt)
        print(
            f"    ⏳ 限流，{wait:.0f}s 后重试 ({attempt + 1}/{TRANSCRIPT_MAX_RETRIES})...",
            file=sys.stderr,
            flush=True,
        )
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
        api = YouTubeTranscriptApi()
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

    videos: List[Dict[str, Any]] = []
    errors: List[str] = []
    channels_with_new = 0
    pending: List[tuple] = []   # (channel, entry) 等待抓字幕的视频

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
        pending.extend((ch, ne) for ne in new_entries)

    def record(ch: Dict[str, Any], ne: Dict[str, Any], transcript_info: Dict[str, Any]) -> None:
        """把一个视频写进结果列表并标记已见；疑似 Shorts 的只标记不收录。"""
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
        if not args.dry_run:
            state[vid] = time.time()

    def transcript_pass(items: List[tuple]) -> List[tuple]:
        """抓一轮字幕。限流的视频不落结果、攒进 deferred 返回；熔断后剩余的全部 defer。"""
        deferred: List[tuple] = []
        consecutive_blocks = 0
        for idx, (ch, ne) in enumerate(items):
            info = fetch_transcript(ne["video_id"])
            if info.get("error") and _is_rate_limit_error(info["error"]):
                consecutive_blocks += 1
                deferred.append((ch, ne))
                if consecutive_blocks >= IP_BLOCK_CIRCUIT_BREAKER:
                    rest = items[idx + 1:]
                    deferred.extend(rest)
                    print(f"  ⛔ 连续 {consecutive_blocks} 次限流，判定 IP 被封，"
                          f"本轮剩余 {len(rest)} 个视频先搁置", file=sys.stderr, flush=True)
                    break
            else:
                consecutive_blocks = 0
                record(ch, ne, info)
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
            msg = (f"IP still blocked after {rounds} cooldown round(s); "
                   f"{len(deferred)} video(s) left without transcript")
            errors.append(msg)
            print(f"  ⛔ 冷却 {rounds} 轮后仍限流，{len(deferred)} 个视频放弃字幕", file=sys.stderr, flush=True)
            for ch, ne in deferred:
                record(ch, ne, {"text": None, "lang": None, "truncated": False, "error": "skipped_ip_block"})

    # Save state
    if not args.dry_run:
        save_state(state)

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
