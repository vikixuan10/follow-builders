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

Dedupe via state file at ~/.follow-builders/seen-articles.json:

- A URL stays "seen" for as long as it is still listed on the index page
  (its timestamp is refreshed on every run) and for STATE_TTL_DAYS after it
  drops off. Before 2026-10-12 the timestamp was never refreshed, so every
  article came back as "new" once a month.
- Every link on the index page is considered, in page order; anything not in
  the state file is new. At most --max-per-blog new articles are fetched per
  blog per run, the rest wait for the next run.
- The first time a blog is indexed, only the top --max-per-blog links are
  fetched; the rest of the page is recorded as seen so the back catalogue
  doesn't trickle in for days. "baseline:<blog name>" keys in the state file
  mark blogs that have been through this.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
SOURCES_PATH = REPO_DIR / "config" / "my-sources.json"

USER_DIR = Path.home() / ".follow-builders"
STATE_PATH = USER_DIR / "seen-articles.json"
# 这次运行的去重记录先写到这里，邮件确认发出后才由 build_page.py 并入 STATE_PATH（2026-10-18 起）。
STATE_NEW_PATH = USER_DIR / "seen-articles.new.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MAX_CONTENT_CHARS = 20_000
STATE_TTL_DAYS = 30
BASELINE_PREFIX = "baseline:"


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
    """写到 STATE_NEW_PATH，不直接改正式记录；邮件发出后由 build_page.py 并入。"""
    USER_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_NEW_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# -- HTML utilities -----------------------------------------------------------

def fetch_html(url: str) -> Optional[str]:
    try:
        res = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        if res.status_code != 200:
            return None
        if not res.encoding or res.encoding.lower() in ("iso-8859-1", "latin-1"):
            res.encoding = "utf-8"
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

def _extract_anthropic_path_links(index_html: str, path_prefix: str,
                                  skip_prefixes: tuple = ()) -> List[Dict[str, str]]:
    """
    Shared extractor for anthropic.com/<prefix>/slug listings (engineering / news / research).
    Preserves the order links appear in HTML (roughly newest first).
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
        if any(path.startswith(sp) for sp in skip_prefixes):
            continue
        if path in seen_set:
            continue
        seen_set.add(path)
        seen.append(path)
    return [
        {"url": f"https://www.anthropic.com{p}", "slug": p.split("/")[-1]}
        for p in seen
    ]


def extract_anthropic_engineering_links(index_html: str) -> List[Dict[str, str]]:
    return _extract_anthropic_path_links(index_html, "/engineering")


def extract_anthropic_news_links(index_html: str) -> List[Dict[str, str]]:
    return _extract_anthropic_path_links(index_html, "/news")


def extract_anthropic_research_links(index_html: str) -> List[Dict[str, str]]:
    # /research/team/<name> are team pages, not articles. They sit at the top of
    # the index and used to take 4 of the 5 slots.
    return _extract_anthropic_path_links(index_html, "/research", skip_prefixes=("/research/team/",))


def extract_claude_blog_links(index_html: str) -> List[Dict[str, str]]:
    """
    Claude Blog index page. Around 2026-10-07 the blog moved from
    claude.com/blog/<slug> to claude.com/resources/articles/<slug>
    (claude.com/blog now redirects there). Both forms are accepted; an article
    recorded under the old URL counts as seen via "aliases".
    """
    seen = []
    seen_set = set()
    for m in re.finditer(r'href="(?:https://claude\.com)?/(blog|resources/articles)/([\w\-]+)/?"', index_html):
        section, slug = m.group(1), m.group(2)
        if slug in seen_set:
            continue
        seen_set.add(slug)
        seen.append((section, slug))
    return [
        {
            "url": f"https://claude.com/{section}/{slug}",
            "slug": slug,
            "aliases": [f"https://claude.com/{other}/{slug}"
                        for other in ("blog", "resources/articles") if other != section],
        }
        for section, slug in seen
    ]


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


def extract_rss_links(xml: str, blog: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    RSS 来源（OpenAI News、Google DeepMind Blog 等，2026-10-16 加）。按订阅源里的顺序（新的在前）返回文章链接。
    - maxAgeDays：比这更早的条目不看（默认 4 天），订阅源里积压的旧文不会被当成新文章。
    - requireCategory / excludeCategories：按 <category> 筛。OpenAI 的客户案例要么没有分类、要么标 Startup。
    """
    max_age = blog.get("maxAgeDays", 4)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age)
    exclude = set(blog.get("excludeCategories", []))
    out, seen = [], set()
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        m = re.search(r"<link>\s*([^<\s]+)\s*</link>", item)
        if not m:
            continue
        url = m.group(1).strip()
        if url in seen:
            continue
        cats = [c.strip() for c in re.findall(r"<category>(?:<!\[CDATA\[)?([^<\]]+)", item)]
        if blog.get("requireCategory") and not cats:
            continue
        if exclude & set(cats):
            continue
        d = re.search(r"<pubDate>([^<]+)</pubDate>", item)
        try:
            published = parsedate_to_datetime(d.group(1).strip()) if d else None
        except Exception:
            published = None
        if published is not None and published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if published is None or published < cutoff:
            continue
        seen.add(url)
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
        title = re.sub(r"\s+", " ", t.group(1)).strip() if t else ""
        out.append({"url": url, "slug": url.rstrip("/").split("/")[-1], "published": published.strftime("%Y-%m-%d"),
                    "title": title.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')})
    return out


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

    # Content: 取 <article> 和 <main> 里最长的那一块。有的网页第一个 <article> 是「相关文章」卡片
    # （2026-10-16 DeepMind 一篇只取到 110 个字符），所以不再只取第一个。都没有才用整页。
    blocks = re.findall(r"<article[^>]*>(.+?)</article>", html, flags=re.DOTALL | re.IGNORECASE)
    blocks += re.findall(r"<main[^>]*>(.+?)</main>", html, flags=re.DOTALL | re.IGNORECASE)
    texts = [strip_html_tags(b) for b in blocks]
    content = max(texts, key=len) if texts else strip_html_tags(html)
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
                        help="Max new articles to fetch per blog per run (default: 5)")
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
        index_url = blog.get("feedUrl") if blog.get("type") == "rss" else blog["indexUrl"]
        print(f"Fetching {name}...", file=sys.stderr, flush=True)

        index_html = fetch_html(index_url)
        if not index_html:
            errors.append(f"博客：{name} 的目录页打不开")
            continue
        if blog.get("type") == "rss" and "<item" not in index_html:
            # 被拦时返回的是网页，或者订阅源换了格式：一条条目都解析不出来
            errors.append(f"博客：{name} 的订阅源里一条条目都没有（可能被拦，或换了格式）")
            continue

        # Use blog-specific extractor. Order matters — match more specific paths first.
        if blog.get("type") == "rss":
            links = extract_rss_links(index_html, blog)
        elif "anthropic.com/engineering" in index_url:
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

        print(f"  Found {len(links)} article link(s)", file=sys.stderr, flush=True)
        if not links:
            # The page loaded but nothing matched: the site layout or URLs changed.
            # (An RSS feed with nothing in the last few days is normal, not an error.)
            if blog.get("type") != "rss":
                errors.append(f"博客：{name} 的目录页上一篇文章都没认出来（网站可能改版了）")
            continue

        now = time.time()
        marker = BASELINE_PREFIX + name
        first_time = marker not in state
        fetched = 0

        for pos, link in enumerate(links):
            url = link["url"]
            # URL is the dedupe key. Still listed -> refresh, so it never expires while listed.
            if any(k in state for k in [url] + link.get("aliases", [])):
                state[url] = now
                continue
            if first_time and pos >= args.max_per_blog:
                state[url] = now  # back catalogue of a blog we haven't indexed before
                continue
            if fetched >= args.max_per_blog:
                continue  # leave for the next run

            article_html = fetch_html(url)
            if not article_html:
                errors.append(f"博客：{name} 有一篇文章打不开（{url}）")
                continue

            article = extract_article_content(article_html)
            if not article.get("title"):
                continue

            posts.append({
                "blog": name,
                "title": link.get("title") or re.sub(r"\s+", " ", article["title"]).strip(),
                "url": url,
                "publishedAt": article["publishedAt"] or link.get("published"),
                "content": article["content"],
                "contentTruncated": article["truncated"],
            })
            fetched += 1
            state[url] = now

            time.sleep(0.5)

        state[marker] = now

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
