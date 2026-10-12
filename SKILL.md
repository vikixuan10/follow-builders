---
name: follow-builders
description: Weiwei's personalized AI & business daily digest. Monitors 40 YouTube channels (AI + business + leadership), 2 official blogs (Anthropic + Claude), and 25 AI builders on X/Twitter (reusing Zara Zhang's public feed). Generates a private web page (original posts + Chinese summaries), emails the overview with a link, and archives Markdown to Obsidian. Use when user invokes /ai, /digest, or asks for their daily YouTube/AI brief.
---

# Follow Builders — Weiwei's Personal Edition

Forked from [Zara Zhang's follow-builders](https://github.com/zarazhangrui/follow-builders) and heavily customized for Weiwei's needs.

## Architecture

**Three data sources**, all free:

1. **40 YouTube channels** (Weiwei's own list, covering AI + business + leadership)
   - Fetched via YouTube RSS feeds (free, no API key)
   - Transcripts via `youtube-transcript-api` (free, auto-captions)
2. **2 blogs** — Anthropic Engineering + Claude Blog
   - Scraped directly from the websites (free)
3. **25 X/Twitter builders** — reused from Zara's public central feed
   - Downloaded from `https://raw.githubusercontent.com/zarazhangrui/follow-builders/main/feed-x.json`
   - Zara pays the X API fees; we piggyback her public output

**No paid APIs required from user.** Only needs Resend API key for email delivery.

## Delivery

Output goes to **both**:
- **Obsidian vault** — permanent archive as dated Markdown notes
- **Resend email** — morning brief delivered to user's inbox at 8 AM Europe/Dublin

## Tech Stack

Python 3.9+ (user has no Node.js). Dependencies:
- `youtube-transcript-api` — YouTube subtitles
- `feedparser` — RSS parsing
- `requests` — HTTP

---

## Content Delivery — Digest Run

This is what runs on the schedule, or when the user invokes `/ai`.

**The procedure lives in one place only:** `~/.claude/scheduled-tasks/ai-daily-digest/SKILL.md`. Read that file and follow its six steps exactly. Do not follow any older description of the digest format.

In short (since 2026-10-12 the digest is a web page, not a long Markdown email):

1. `prepare_digest.py` fetches the data; `build_page.py ingest` stores it as the day's `data.json`.
2. `build_page.py view` writes the material for the model to read.
3. The model writes only `edit.json`: topic grouping, Chinese summaries, blog points, paragraph numbers. Original English text is copied into the page by the script, never retyped by the model.
4. `build_page.py build` validates `edit.json` and generates the page (last 14 days, switchable), a Markdown archive and the email body.
5. The page is published to Weiwei's private Artifact URL (`page.url` in `~/.follow-builders/config.json`).
6. `deliver.py` saves the Markdown archive to Obsidian and emails the overview with a link.

---

## First-Time Setup

If `~/.follow-builders/config.json` doesn't exist or `onboardingComplete` is false, run onboarding:

### Step 1: Resolve YouTube channel IDs (one-time)

```bash
cd /Users/weiwei/Documents/SKILL/follow-builders/scripts
python3 resolve_channels.py
```

This populates `config/my-sources.json` with UC-style channel IDs for all 40 channels.
(Already done. Only re-run if user adds new channels.)

### Step 2: Collect user's configuration

Ask the user:
1. **Obsidian vault path** (e.g. `/Users/weiwei/Documents/Obsidian/AI-Digest/`)
2. **Email address** for Resend
3. **Resend API key** (get from https://resend.com/api-keys, free tier = 100/day)

### Step 3: Write config files

```bash
mkdir -p ~/.follow-builders
cat > ~/.follow-builders/config.json <<EOF
{
  "language": "zh",
  "timezone": "Europe/Dublin",
  "deliveryTime": "08:00",
  "delivery": {
    "method": "obsidian_and_email",
    "obsidianPath": "<user-path>",
    "email": "<user-email>"
  },
  "onboardingComplete": true
}
EOF

cat > ~/.follow-builders/.env <<EOF
RESEND_API_KEY=<user-key>
EOF
chmod 600 ~/.follow-builders/.env
```

### Step 4: Set up cron

Use macOS launchd or system crontab:

```bash
# Runs at 8:00 AM Europe/Dublin time
(crontab -l 2>/dev/null; echo "0 8 * * * cd /Users/weiwei/Documents/SKILL/follow-builders/scripts && /usr/bin/python3 prepare_digest.py 2>/dev/null | /usr/bin/python3 -c 'import sys,json; data=json.load(sys.stdin); print(data)' ... ") | crontab -
```

NOTE: The cron approach above is naive — Claude Code agent needs to be
running. A better approach is to register a scheduled-task that triggers
Claude Code at 8 AM to run this full workflow. See scheduled-tasks MCP.

### Step 5: Welcome digest

Run the full workflow ONCE immediately so user sees what it looks like:
1. Run prepare_digest.py
2. Remix into Markdown
3. Deliver
4. Ask for feedback on length, tone, format

---

## Configuration Handling

### Source changes
- "Add a YouTube channel" → Edit `config/my-sources.json`, run `resolve_channels.py` to get channel ID
- "Remove a channel" → Delete entry from `config/my-sources.json`
- "Change my X account list" → Not possible (comes from Zara's central feed)

### Schedule changes
- "Change time to X" → Update `deliveryTime` in config.json + update cron

### Prompt customization
Copy prompts to `~/.follow-builders/prompts/` and edit there (survives git pull):

```bash
mkdir -p ~/.follow-builders/prompts
cp prompts/summarize-video.md ~/.follow-builders/prompts/summarize-video.md
# edit it
```

User prompts override repo prompts.

### Delivery changes
- "Only send to Obsidian" → Set `delivery.method` = `"obsidian"`
- "Only email" → Set `delivery.method` = `"email"`

---

## Manual Trigger

When user invokes `/ai` or `/digest` or asks "今天的 AI 早报":

1. Skip cron check — run the workflow immediately
2. Same fetch → remix → deliver flow as cron
3. Tell user: "抓取中，大概 1-2 分钟..."

---

## Testing

Quick smoke test (3 channels, 7 days back, no state update):

```bash
python3 prepare_digest.py --limit-channels 3 --lookback-hours 168 --dry-run 2>/dev/null
```

Should return JSON with videos, blogs, and X tweets populated.
