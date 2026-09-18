#!/usr/bin/env python3
"""
Fetch Blog Posts (Anthropic Engineering + Claude Blog)
======================================================

Scrapes the index pages and returns recent posts with their content.

Usage: python3 fetch_blogs.py
Output: JSON to stdout with shape:
    {
      "generatedAt": "...",
      "posts": [
        {
          "blog": "Anthropic Engineering",
          "title": "...",
          "url": "https://...",
          "publishedAt": "YYYY-MM-DD" | null,
          "content": "...",
          "contentTruncated": false
        }
      ],
      "errors": [...]
    }

Since blog publication is rare (1-3 posts/week), we look back 7 days
and dedupe via state file at ~/.follow-builders/seen-articles.json.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
SOURCES_PATH = REPO_DIR / "config" / "my-sources.json"

USER_DIR = Path.home() / ".follow-builders"
STATE_PATH = USER_DIR / "seen-articles.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MAX_CONTENT_CHARS = 20_000
STATE_TTL_DAYS = 30


# -- State management ---------------------------------------------------------

def load_state() -> Dict[str, float]:
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return {}
    cutoff = time.time() - STATE_TTL_DAYS * 24 * 3600
    return {k: v for k, v in state.items() if v > cutoff}


def save_state(state: Dict[str, float]) -> None:
    USER_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# -- HTML utilities -----------------------------------------------------------

def fetch_html(url: str) -> Optional[str]:
    try:
        res = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        if res.status_code != 200:
            return None
        return res.text
    except Exception:
        return None


def strip_html_tags(html: str) -> str:
    """Very basic HTML tag stripping. Enough for content extraction."""
    # Remove script and style blocks entirely
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Replace block-level tags with newlines
    html = re.sub(r"</(?:p|div|br|h[1-6]|li|tr|section|article)>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Decode common entities
    text = (text
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&nbsp;", " "))
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# -- Blog-specific extractors -------------------------------------------------

def extract_anthropic_engineering_links(index_html: str) -> List[Dict[str, str]]:
    """
    Anthropic Engineering index page.
    Links look like: href="/engineering/slug-name"
    """
    # Pattern: href="/engineering/something"
    links = set()
    for m in re.finditer(
        r'href="(/engineering/[\w\-/]+)"',
        index_html,
    ):
        path = m.group(1)
        # Skip the index page itself and pagination
        if path.rstrip("/") == "/engineering":
            continue
        if "/page/" in path:
            continue
        links.add(path)
    return [
        {"url": f"https://www.anthropic.com{p}", "slug": p.split("/")[-1]}
        for p in links
    ]


def extract_claude_blog_links(index_html: str) -> List[Dict[str, str]]:
    """
    Claude Blog index page.
    Links look like: href="/blog/slug-name"
    """
    links = set()
    for m in re.finditer(
        r'href="(/blog/[\w\-/]+)"',
        index_html,
    ):
        path = m.group(1)
        if path.rstrip("/") == "/blog":
            continue
        if "/page/" in path:
            continue
        links.add(path)
    return [
        {"url": f"https://claude.com{p}", "slug": p.split("/")[-1]}
        for p in links
    ]


def _extract_anthropic_path_links(index_html: str, path_prefix: str) -> List[Dict[str, str]]:
    """
    Shared extractor for anthropic.com/<prefix>/slug listings (engineering / news / research).
    Preserves the order links appear in HTML so the caller's [:max] slice gets newest first.
    """
    seen = []
    seen_set = set()
    pattern = rf'href="({re.escape(path_prefix)}/[\w\-/]+)"'
    for m in re.finditer(pattern, index_html):
        path = m.group(1)
        if path.rstrip("/") == path_prefix:
            continue
        if "/page/" in path:
            continue
        if path in seen_set:
            continue
        seen_set.add(path)
        seen.append(path)
    return [
        {"url": f"https://www.anthropic.com{p}", "slug": p.split("/")[-1]}
        for p in seen
    ]


def extract_anthropic_news_links(index_html: str) -> List[Dict[str, str]]:
    return _extract_anthropic_path_links(index_html, "/news")


def extract_anthropic_research_links(index_html: str) -> List[Dict[str, str]]:
    return _extract_anthropic_path_links(index_html, "/research")


def extract_transformer_circuits_links(index_html: str) -> List[Dict[str, str]]:
    """
    Transformer Circuits index page.
    Links look like: href="YYYY/slug/index.html" — relative, no leading slash.
    Preserves HTML order (newest first on the index page).
    """
    seen = []
    seen_set = set()
    for m in re.finditer(
        r'href="(20\d{2}/[\w\-]+/index\.html)"',
        index_html,
    ):
        path = m.group(1)
        if path in seen_set:
            continue
        seen_set.add(path)
        seen.append(path)
    return [
        {"url": f"https://transformer-circuits.pub/{p}", "slug": p.split("/")[-2]}
        for p in seen
    ]


def extract_article_content(html: str) -> Dict[str, Any]:
    """Extract title, date, content from an article page."""
    # Title: <title>Article — Anthropic</title> or h1
    title = ""
    m = re.search(r"<title>([^<]+)</title>", html)
    if m:
        title = m.group(1).strip()
        # Strip suffix like " \ Anthropic"
        title = re.sub(r"\s*[\\|—-]\s*Anthropic\s*$", "", title).strip()
        title = re.sub(r"\s*[\\|—-]\s*Claude\s*$", "", title).strip()
    if not title:
        m = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
        if m:
            title = m.group(1).strip()

    # Published date: look for time/date tags
    published = None
    m = re.search(
        r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})',
        html,
    )
    if m:
        published = m.group(1)
    else:
        # Look for meta property="article:published_time"
        m = re.search(
            r'property="article:published_time"[^>]*content="(\d{4}-\d{2}-\d{2})',
            html,
        )
        if m:
            published = m.group(1)

    # Content: extract from <article> tag if present, else main
    content_html = ""
    m = re.search(r"<article[^>]*>(.+?)</article>", html, flags=re.DOTALL | re.IGNORECASE)
    if m:
        content_html = m.group(1)
    else:
        m = re.search(r"<main[^>]*>(.+?)</main>", html, flags=re.DOTALL | re.IGNORECASE)
        if m:
            content_html = m.group(1)
        else:
            content_html = html

    content = strip_html_tags(content_html)
    truncated = False
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS]
        truncated = True

    return {
        "title": title,
        "publishedAt": published,
        "content": content,
        "truncated": truncated,
    }


# -- Main ---------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-per-blog", type=int, default=5,
                        help="Max articles to fetch per blog (default: 5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't update state file")
    args = parser.parse_args()

    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        sources = json.load(f)

    state = load_state()
    posts: List[Dict[str, Any]] = []
    errors: List[str] = []

    for blog in sources.get("blogs", []):
        name = blog["name"]
        index_url = blog["indexUrl"]
        print(f"Fetching {name}...", file=sys.stderr, flush=True)

        index_html = fetch_html(index_url)
        if not index_html:
            errors.append(f"Could not fetch index: {name}")
            continue

        # Use blog-specific extractor. Order matters — match more specific paths first.
        if "anthropic.com/engineering" in index_url:
            links = extract_anthropic_engineering_links(index_html)
        elif "anthropic.com/news" in index_url:
            links = extract_anthropic_news_links(index_html)
        elif "anthropic.com/research" in index_url:
            links = extract_anthropic_research_links(index_html)
        elif "transformer-circuits.pub" in index_url:
            links = extract_transformer_circuits_links(index_html)
        elif "claude.com" in index_url:
            links = extract_claude_blog_links(index_html)
        else:
            links = []

        # Limit to N articles (index page lists newest first)
        links = links[:args.max_per_blog]
        print(f"  Found {len(links)} article link(s)", file=sys.stderr, flush=True)

        for link in links:
            url = link["url"]
            # Use URL as the dedupe key
            if url in state:
                continue

            article_html = fetch_html(url)
            if not article_html:
                errors.append(f"Could not fetch: {url}")
                continue

            article = extract_article_content(article_html)
            if not article.get("title"):
                continue

            posts.append({
                "blog": name,
                "title": article["title"],
                "url": url,
                "publishedAt": article["publishedAt"],
                "content": article["content"],
                "contentTruncated": article["truncated"],
            })
            if not args.dry_run:
                state[url] = time.time()

            time.sleep(0.5)

    if not args.dry_run:
        save_state(state)

    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "posts": posts,
        "stats": {
            "blogsChecked": len(sources.get("blogs", [])),
            "postsFound": len(posts),
        },
        "errors": errors,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
