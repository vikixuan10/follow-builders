#!/usr/bin/env python3
"""
Resolve YouTube Handles → Channel IDs (one-time setup script)
============================================================

Fetches each @handle's YouTube channel page and extracts its UC... channel ID.
Saves the results back into config/my-sources.json so the daily fetcher can
build YouTube RSS feed URLs (which require channel_id, not handle).

Usage: python3 resolve_channels.py
"""

import json
import re
import sys
import time
from pathlib import Path
from typing import Optional, Tuple
import requests

SCRIPT_DIR = Path(__file__).resolve().parent
SOURCES_PATH = SCRIPT_DIR.parent / "config" / "my-sources.json"

# Use a real browser UA so YouTube doesn't block us
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# EU (GDPR) consent bypass cookies. Without these, YouTube serves a
# "Before you continue to YouTube" consent wall instead of the channel page.
CONSENT_COOKIES = {
    "CONSENT": "YES+cb.20210328-17-p0.en+FX+000",
    "SOCS": "CAI",
}

# Patterns to find the channelId (UC...) in a YouTube channel page's HTML
# Tried in order of reliability.
PATTERNS = [
    re.compile(r"feeds/videos\.xml\?channel_id=(UC[\w-]{22})"),
    re.compile(r'<meta\s+itemprop="(?:identifier|channelId)"\s+content="(UC[\w-]{22})"'),
    re.compile(r'"channelId":"(UC[\w-]{22})"'),
    re.compile(r'"externalId":"(UC[\w-]{22})"'),
]


def extract_channel_id(html: str) -> Optional[str]:
    """Try each pattern in order and return the first match."""
    for pattern in PATTERNS:
        m = pattern.search(html)
        if m:
            return m.group(1)
    return None


def resolve_handle(handle: str) -> Tuple[Optional[str], Optional[str]]:
    """Returns (channel_id, error_message). One of them will be None."""
    clean = handle.lstrip("@")
    url = f"https://www.youtube.com/@{clean}"
    try:
        res = requests.get(
            url,
            headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
            cookies=CONSENT_COOKIES,
            timeout=15,
        )
        if res.status_code != 200:
            return None, f"HTTP {res.status_code}"
        channel_id = extract_channel_id(res.text)
        if not channel_id:
            return None, "channelId not found in HTML"
        return channel_id, None
    except Exception as e:
        return None, str(e)


def main() -> int:
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        sources = json.load(f)

    channels = sources["youtube_channels"]
    print(f"Resolving {len(channels)} YouTube handles → channel IDs...\n")

    success = 0
    failed = []

    for i, ch in enumerate(channels, 1):
        # Skip if already resolved
        if ch.get("channelId"):
            print(f"[{i}/{len(channels)}] ✓ {ch['name']} (already resolved: {ch['channelId']})")
            success += 1
            continue

        print(f"[{i}/{len(channels)}] {ch['name']} ({ch['handle']}) ... ", end="", flush=True)
        channel_id, err = resolve_handle(ch["handle"])
        if channel_id:
            ch["channelId"] = channel_id
            print(f"✓ {channel_id}")
            success += 1
        else:
            print(f"✗ FAILED: {err}")
            failed.append((ch["name"], ch["handle"], err))

        # Polite delay — don't hammer YouTube
        time.sleep(0.3)

    # Save back
    with open(SOURCES_PATH, "w", encoding="utf-8") as f:
        json.dump(sources, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print()
    print("─" * 40)
    print(f"✓ Resolved: {success}")
    print(f"✗ Failed:   {len(failed)}")
    print("─" * 40)
    print(f"Saved to {SOURCES_PATH}")

    if failed:
        print("\nFailed handles:")
        for name, handle, err in failed:
            print(f"  - {name} ({handle}): {err}")
        print("\nManually fix the handle or set channelId in config/my-sources.json.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
