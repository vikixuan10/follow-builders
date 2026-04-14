#!/usr/bin/env python3
"""
Deliver Digest (Obsidian + Resend email)
========================================

Reads the digest text (Markdown) and delivers it via two channels:
  1. Writes to Obsidian vault as a new daily note (if obsidianPath configured)
  2. Sends via Resend email API (if email configured)

Config from ~/.follow-builders/config.json:
  {
    "delivery": {
      "method": "obsidian_and_email" | "obsidian" | "email" | "stdout",
      "obsidianPath": "/Users/weiwei/Documents/Obsidian/vault/AI-Digest",
      "email": "weiwei@example.com"
    },
    "timezone": "Europe/Dublin"
  }

Resend API key from ~/.follow-builders/.env:
  RESEND_API_KEY=re_xxxxxxxx

Usage:
  cat digest.md | python3 deliver.py
  python3 deliver.py --file digest.md
  python3 deliver.py --message "digest text"
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
from zoneinfo import ZoneInfo

import markdown
import requests

USER_DIR = Path.home() / ".follow-builders"
CONFIG_PATH = USER_DIR / "config.json"
ENV_PATH = USER_DIR / ".env"


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {
            "delivery": {"method": "stdout"},
            "timezone": "Europe/Dublin",
        }
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"delivery": {"method": "stdout"}, "timezone": "Europe/Dublin"}


def load_env() -> Dict[str, str]:
    """Load KEY=VALUE pairs from ~/.follow-builders/.env"""
    env: Dict[str, str] = {}
    if not ENV_PATH.exists():
        return env
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        pass
    return env


def get_digest_text(args) -> str:
    if args.message:
        return args.message
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    # Read from stdin
    return sys.stdin.read()


def save_to_obsidian(digest: str, obsidian_path: str, tz_name: str) -> Optional[str]:
    """
    Write the digest to Obsidian vault as a new Markdown file.
    Returns the path to the created file, or None on failure.
    """
    try:
        vault_dir = Path(obsidian_path).expanduser()
        vault_dir.mkdir(parents=True, exist_ok=True)
        # Use local date for filename
        try:
            now_local = datetime.now(ZoneInfo(tz_name))
        except Exception:
            now_local = datetime.now()
        filename = f"AI-Digest-{now_local.strftime('%Y-%m-%d')}.md"
        filepath = vault_dir / filename
        # If file already exists, append a counter
        if filepath.exists():
            i = 2
            while (vault_dir / f"AI-Digest-{now_local.strftime('%Y-%m-%d')}-{i}.md").exists():
                i += 1
            filepath = vault_dir / f"AI-Digest-{now_local.strftime('%Y-%m-%d')}-{i}.md"
        filepath.write_text(digest, encoding="utf-8")
        return str(filepath)
    except Exception as e:
        print(f"Obsidian save error: {e}", file=sys.stderr)
        return None


def send_resend_email(digest: str, api_key: str, to_email: str, tz_name: str) -> bool:
    """Send digest via Resend API. Returns True on success."""
    try:
        now_local = datetime.now(ZoneInfo(tz_name)) if tz_name else datetime.now()
    except Exception:
        now_local = datetime.now()
    date_str = now_local.strftime("%Y-%m-%d")

    # Convert Markdown → HTML for rich email rendering
    html_body = markdown.markdown(
        digest,
        extensions=["tables", "fenced_code", "nl2br"],
    )
    # Wrap in a styled HTML template for clean email display
    html_email = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         font-size: 15px; line-height: 1.7; color: #222; max-width: 680px;
         margin: 0 auto; padding: 20px; }}
  h1 {{ font-size: 22px; border-bottom: 2px solid #333; padding-bottom: 8px; }}
  h2 {{ font-size: 18px; color: #1a1a1a; margin-top: 28px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }}
  h3 {{ font-size: 15px; color: #333; margin-top: 20px; }}
  a {{ color: #1a73e8; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  blockquote {{ border-left: 3px solid #888; padding: 8px 14px; margin: 10px 0;
                background: #f7f7f5; font-size: 14px; }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 20px 0; }}
  strong {{ color: #111; }}
  ul {{ padding-left: 20px; }}
  li {{ margin: 4px 0; }}
  code {{ background: #f0f0f0; padding: 1px 4px; border-radius: 3px; font-size: 13px; }}
</style></head><body>
{html_body}
</body></html>"""

    try:
        res = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "from": "AI Digest <digest@resend.dev>",
                "to": [to_email],
                "subject": f"AI & 商业早报 — {date_str}",
                "html": html_email,
                "text": digest,
            },
            timeout=30,
        )
        if res.status_code >= 400:
            print(f"Resend API error: {res.status_code} {res.text}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"Resend error: {e}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Read digest from file")
    parser.add_argument("--message", help="Digest text as arg")
    args = parser.parse_args()

    config = load_config()
    env = load_env()
    delivery = config.get("delivery", {})
    method = delivery.get("method", "stdout")
    tz_name = config.get("timezone", "Europe/Dublin")

    digest = get_digest_text(args).strip()
    if not digest:
        print(json.dumps({"status": "skipped", "reason": "empty digest"}))
        return 0

    results = {"obsidian": None, "email": None}

    # Obsidian
    if method in ("obsidian", "obsidian_and_email"):
        obsidian_path = delivery.get("obsidianPath")
        if obsidian_path:
            saved = save_to_obsidian(digest, obsidian_path, tz_name)
            results["obsidian"] = {"ok": saved is not None, "path": saved}
        else:
            results["obsidian"] = {"ok": False, "error": "obsidianPath not configured"}

    # Email
    if method in ("email", "obsidian_and_email"):
        api_key = env.get("RESEND_API_KEY") or os.environ.get("RESEND_API_KEY")
        to_email = delivery.get("email")
        if not api_key:
            results["email"] = {"ok": False, "error": "RESEND_API_KEY not in .env"}
        elif not to_email:
            results["email"] = {"ok": False, "error": "email not configured"}
        else:
            ok = send_resend_email(digest, api_key, to_email, tz_name)
            results["email"] = {"ok": ok, "to": to_email}

    # Stdout fallback
    if method == "stdout" or (not results.get("obsidian") and not results.get("email")):
        print(digest)
        return 0

    print(json.dumps({"status": "ok", "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
