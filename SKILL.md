---
name: follow-builders
description: Weiwei's personalized AI & business daily digest. Monitors 40 YouTube channels (AI + business + leadership), 2 official blogs (Anthropic + Claude), and 25 AI builders on X/Twitter (reusing Zara Zhang's public feed). Generates Chinese summaries with timestamps, delivers via email + Obsidian. Use when user invokes /ai, /digest, or asks for their daily YouTube/AI brief.
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

This is what runs on the cron schedule, or when the user invokes `/ai`.

### Step 1: Run prepare_digest.py

```bash
cd /Users/weiwei/Documents/SKILL/follow-builders/scripts
python3 prepare_digest.py --lookback-hours 48 2>/dev/null
```

This fetches all data and prints a single JSON blob to stdout with:
- `config` — user preferences (language, timezone, delivery)
- `youtube.videos[]` — new YouTube videos with transcripts (in format `[MM:SS] text`)
- `blogs.posts[]` — new blog articles with full content
- `x.builders[]` — AI builders with their recent tweets (from Zara's feed)
- `prompts.{digest_intro,summarize_video,summarize_tweets,summarize_blogs,translate}` — remix instructions
- `totals` — counts
- `errors[]` — non-fatal issues (IGNORE unless diagnostic)

### Step 2: Check for content

If `totals.youtubeVideos == 0` AND `totals.blogPosts == 0` AND `totals.xTweets == 0`:
Tell user: "今日没有新的 AI/商业内容，明天再看。" Then stop.

### Step 3: Remix content (Claude's job)

**Your ONLY job is to remix the JSON content into a Markdown digest.** Do NOT fetch anything from the web, visit URLs, or call APIs. Everything is in the JSON.

Follow the prompts in this priority order:

1. **For each YouTube video** (`youtube.videos[]`):
   - Use the `transcript` field which already contains `[MM:SS] text` timestamps
   - Group videos by `category` (AI-官方/研究, AI-访谈/播客, etc.)
   - Include the `url` field in every summary
   - **跳过 Shorts**（transcript < 300 字的视频）
   - **必须严格使用以下四个维度，不允许自行发明名称：**

   ```
   **一句话精髓：** ...（20 字以内）
   **核心观点：**（至少 5 条，最多 10 条）
   **最值得看的段落：**（3-5 段，带 [MM:SS] 时间戳）
   **金句：**（至少 1 条；实在没有写"无突出金句"）
   ```

2. **For each blog post** (`blogs.posts[]`):
   - Use the `content` field
   - Include the `url` field
   - **必须严格使用以下三个维度，不允许自行发明名称：**

   ```
   **核心要点：**（至少 5 条，最多 10 条）
   **关键数字 / 实操细节：**（如果没有写"无关键数字"）
   **金句：**（至少 1 条；实在没有写"无突出金句"）
   ```

3. **For each X builder's tweets** (`x.builders[]`): apply `prompts.summarize_tweets`
   - Process each builder's `tweets[]` array as one cohesive summary
   - Every tweet MUST include its `url`
   - **人名加粗放第一行，空一行后写内容，每人之间用 --- 分隔**

4. **Assemble the final digest**: follow `prompts.digest_intro`
   - Output is Markdown (rendered in Obsidian + email)
   - Organize by section: YouTube first (grouped by category) → Blogs → X/Twitter
   - Skip sections with no content (don't output empty headers)

**ABSOLUTE RULES:**
- Output in **Chinese** (except proper nouns, code, and original-language quotes)
- **NEVER invent content.** Only use what's in the JSON.
- **Every item MUST have its source URL.** No URL = don't include.
- Do NOT visit youtube.com, x.com, or any URL. Everything you need is in the JSON.
- **NEVER invent your own section headings.** Use exactly the ones specified above (一句话精髓, 核心观点, 最值得看的段落, 金句, 核心要点, 关键数字/实操细节). If you use any other heading names, the digest is WRONG.

### Step 4: Deliver

Save the Markdown digest to a temp file, then call deliver.py:

```bash
echo "$DIGEST_TEXT" > /tmp/fb-digest.md
cd /Users/weiwei/Documents/SKILL/follow-builders/scripts
python3 deliver.py --file /tmp/fb-digest.md
```

deliver.py reads `~/.follow-builders/config.json` to know:
- Where the Obsidian vault is (`delivery.obsidianPath`)
- The email address (`delivery.email`)

And `~/.follow-builders/.env` for:
- `RESEND_API_KEY=re_xxxxxxx`

If delivery fails, show the digest in the terminal as fallback.

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
