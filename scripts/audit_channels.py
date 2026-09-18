#!/usr/bin/env python3
"""一次性频道审计：拉每个频道的 RSS，统计更新频率，帮助判断哪些频道值得保留。

RSS feed 只含最近 ~15 条视频，所以 c90 等于 15 时多半是被上限截断（高产频道）。
真正有用的信号是：last（最后更新日期）、days_since（距今多少天没更）、c30（近 30 天产出）。
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import feedparser

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SRC = Path(__file__).resolve().parent.parent / "config" / "my-sources.json"
now = datetime.now(timezone.utc)


def parse(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def main():
    s = json.load(open(SRC, encoding="utf-8"))
    rows = []
    chans = s["youtube_channels"]
    for i, c in enumerate(chans, 1):
        cid = c.get("channelId")
        name, cat = c["name"], c["category"]
        print(f"[{i}/{len(chans)}] {name}...", file=sys.stderr, flush=True)
        if not cid:
            rows.append({"cat": cat, "name": name, "last": "NO_ID", "c30": 0, "c90": 0, "days_since": None})
            continue
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
            feed = feedparser.parse(r.content)
            dates = sorted([parse(e.get("published", "")) for e in feed.entries
                            if parse(e.get("published", ""))], reverse=True)
        except Exception:
            rows.append({"cat": cat, "name": name, "last": "ERR", "c30": 0, "c90": 0, "days_since": None})
            continue
        if not dates:
            rows.append({"cat": cat, "name": name, "last": "EMPTY", "c30": 0, "c90": 0, "days_since": None})
            continue
        last = dates[0]
        rows.append({
            "cat": cat, "name": name,
            "last": last.strftime("%Y-%m-%d"),
            "c30": sum(1 for d in dates if (now - d).days <= 30),
            "c90": sum(1 for d in dates if (now - d).days <= 90),
            "days_since": (now - last).days,
        })
        time.sleep(0.3)

    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
