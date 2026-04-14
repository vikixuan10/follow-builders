#!/usr/bin/env python3
"""
Prepare Digest — The Orchestrator
=================================

Gathers everything Claude needs to produce the daily digest:
  1. Fetches Zara's central X feed (public GitHub raw URL, free to use)
  2. Runs fetch_youtube.py (new videos + transcripts from user's 40 channels)
  3. Runs fetch_blogs.py (Anthropic + Claude blog posts)
  4. Loads user config and prompts

Outputs a single JSON blob to stdout. Claude reads that blob, remixes the
content following the prompts, and produces the final digest.

Usage: python3 prepare_digest.py [--lookback-hours 48]

This script is the daily entry point. The cron job calls Claude Code with
something like: "Run prepare_digest.py then remix the JSON into a digest."
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# -- Paths --------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
SOURCES_PATH = REPO_DIR / "config" / "my-sources.json"
PROMPTS_DIR = REPO_DIR / "prompts"

USER_DIR = Path.home() / ".follow-builders"
USER_CONFIG_PATH = USER_DIR / "config.json"
USER_PROMPTS_DIR = USER_DIR / "prompts"

# -- Default user config ------------------------------------------------------

DEFAULT_CONFIG = {
    "language": "zh",
    "timezone": "Europe/Dublin",
    "deliveryTime": "08:00",
    "delivery": {
        "method": "obsidian_and_email",
        "obsidianPath": None,          # set after Obsidian is installed
        "email": None,                 # Resend destination email
    },
    "onboardingComplete": False,
}

# -- Prompt files to load -----------------------------------------------------

PROMPT_FILES = [
    "digest-intro.md",
    "summarize-video.md",     # for each YouTube video (with timestamps)
    "summarize-tweets.md",
    "summarize-blogs.md",
    "translate.md",
]


# -- Helpers ------------------------------------------------------------------

def load_user_config() -> Dict[str, Any]:
    if USER_CONFIG_PATH.exists():
        try:
            with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def load_prompt(filename: str) -> Optional[str]:
    """User's custom prompt > repo default prompt."""
    user_path = USER_PROMPTS_DIR / filename
    if user_path.exists():
        return user_path.read_text(encoding="utf-8")
    default_path = PROMPTS_DIR / filename
    if default_path.exists():
        return default_path.read_text(encoding="utf-8")
    return None


def run_script(script_name: str, *args: str) -> Optional[Dict[str, Any]]:
    """Run a fetch script as subprocess, return parsed JSON."""
    script_path = SCRIPT_DIR / script_name
    try:
        result = subprocess.run(
            [sys.executable, str(script_path), *args],
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max
        )
        if result.returncode != 0:
            return {"error": f"{script_name} exited with code {result.returncode}", "stderr": result.stderr[-500:]}
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {"error": f"{script_name} timed out after 10 minutes"}
    except json.JSONDecodeError as e:
        return {"error": f"{script_name} returned invalid JSON: {e}"}
    except Exception as e:
        return {"error": f"{script_name} failed: {e}"}


def fetch_zara_x_feed(url: str) -> Optional[Dict[str, Any]]:
    try:
        res = requests.get(url, timeout=15)
        if res.status_code != 200:
            return None
        return res.json()
    except Exception:
        return None


# -- Main ---------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-hours", type=int, default=48,
                        help="YouTube lookback window in hours (default: 48)")
    parser.add_argument("--skip-youtube", action="store_true")
    parser.add_argument("--skip-blogs", action="store_true")
    parser.add_argument("--skip-x", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't update state files")
    parser.add_argument("--limit-channels", type=int, default=None,
                        help="YouTube: only check first N channels (testing)")
    args = parser.parse_args()

    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        sources = json.load(f)

    config = load_user_config()
    errors: List[str] = []

    # 1. YouTube
    youtube_data: Dict[str, Any] = {"videos": [], "stats": {}}
    if not args.skip_youtube:
        print("▸ Fetching YouTube...", file=sys.stderr, flush=True)
        yt_args = ["--lookback", str(args.lookback_hours)]
        if args.dry_run:
            yt_args.append("--dry-run")
        if args.limit_channels:
            yt_args += ["--limit-channels", str(args.limit_channels)]
        result = run_script("fetch_youtube.py", *yt_args)
        if result and "error" not in result:
            youtube_data = result
        else:
            errors.append(f"YouTube: {result.get('error') if result else 'failed'}")

    # 2. Blogs
    blogs_data: Dict[str, Any] = {"posts": [], "stats": {}}
    if not args.skip_blogs:
        print("▸ Fetching blogs...", file=sys.stderr, flush=True)
        blog_args = []
        if args.dry_run:
            blog_args.append("--dry-run")
        result = run_script("fetch_blogs.py", *blog_args)
        if result and "error" not in result:
            blogs_data = result
        else:
            errors.append(f"Blogs: {result.get('error') if result else 'failed'}")

    # 3. Zara's X feed
    x_data: Dict[str, Any] = {"x": [], "stats": {}}
    if not args.skip_x:
        print("▸ Fetching Zara's X feed...", file=sys.stderr, flush=True)
        zara_url = sources.get("zara_x_feed_url")
        if zara_url:
            feed = fetch_zara_x_feed(zara_url)
            if feed:
                x_data = {
                    "x": feed.get("x", []),
                    "stats": {
                        "builders": len(feed.get("x", [])),
                        "totalTweets": sum(len(b.get("tweets", [])) for b in feed.get("x", [])),
                        "generatedAt": feed.get("generatedAt"),
                    },
                }
            else:
                errors.append("Could not fetch Zara's X feed")

    # 4. Prompts
    prompts = {}
    for filename in PROMPT_FILES:
        key = filename.replace(".md", "").replace("-", "_")
        content = load_prompt(filename)
        if content:
            prompts[key] = content
        else:
            errors.append(f"Prompt missing: {filename}")

    # 5. Assemble output
    output = {
        "status": "ok",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "config": config,
        "youtube": {
            "videos": youtube_data.get("videos", []),
            "stats": youtube_data.get("stats", {}),
        },
        "blogs": {
            "posts": blogs_data.get("posts", []),
            "stats": blogs_data.get("stats", {}),
        },
        "x": {
            "builders": x_data.get("x", []),
            "stats": x_data.get("stats", {}),
        },
        "prompts": prompts,
        "totals": {
            "youtubeVideos": len(youtube_data.get("videos", [])),
            "blogPosts": len(blogs_data.get("posts", [])),
            "xTweets": sum(len(b.get("tweets", [])) for b in x_data.get("x", [])),
        },
        "errors": errors,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
