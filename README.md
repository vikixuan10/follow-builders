# Follow Builders — Python Fork (Personal)


> **Personal fork** of [`zarazhangrui/follow-builders`](https://github.com/zarazhangrui/follow-builders) by [@zarazhangrui](https://github.com/zarazhangrui).
>
> This version is a full Python rewrite heavily customized for my own daily workflow on macOS. If you're looking for the original, well-supported project, go to the upstream repo.

## Why I forked

Zara's "follow builders, not influencers" project is excellent — I'm its target user. But two things pushed me to fork and rewrite:

1. **No Node.js on my machine.** My Mac mini runs Python out of the box; installing the Node toolchain just for this felt wrong.
2. **I want to run it entirely locally**, triggered by a Claude Code scheduled task on my own machine, not via GitHub Actions on the cloud.

So I ported the pipeline to Python 3.9, swapped the source list, tightened the output format, and wired delivery into Resend email + my Obsidian vault.

## What's different from upstream

|                    | Upstream (Zara)                               | This fork                                                     |
| ------------------ | --------------------------------------------- | ------------------------------------------------------------- |
| Language           | Node.js / JavaScript                          | Python 3.9                                                    |
| Runtime            | GitHub Actions (cloud cron)                   | Local macOS + Claude Code scheduled task (`~08:00 Dublin`)    |
| YouTube sources    | 6 curated podcasts                            | 35 channels I follow (AI research, podcasts, eng, business)   |
| X / Twitter sources| 25 builders (updated centrally by Zara)       | Same 25 builders — I still fetch Zara's `feed-x.json` live    |
| Blog sources       | Anthropic Engineering + Claude Blog           | Same                                                          |
| Output format      | Flexible                                      | Locked 4-dimension video format (一句话精髓 / 核心观点 / 最值得看的段落 / 金句), all Chinese |
| Delivery           | Telegram / Discord / email / etc.             | Resend email (HTML) + Obsidian vault archive                  |

## What I still depend on from upstream

I still pull **Zara's centrally-maintained X feed** (`feed-x.json`) from her upstream repo at fetch time — she pays for the X API and keeps it fresh via her GitHub Actions. Huge thanks to her for making that public; without it this fork wouldn't work.

## Repo layout

```
scripts/
├── fetch_youtube.py      # 35 YouTube channels via RSS + transcripts
├── fetch_blogs.py        # Anthropic + Claude blog scraping
├── prepare_digest.py     # Orchestrator; merges YouTube + blogs + Zara's X feed into one JSON
├── deliver.py            # Resend email + Obsidian vault delivery
├── resolve_channels.py   # @handle → UC channel ID resolver (one-time)
├── run_fetch.sh          # Convenience wrapper
└── run_deliver.sh        # Convenience wrapper
config/
└── my-sources.json       # My 35 channels + 2 blogs + Zara's X feed URL
prompts/
├── digest-intro.md       # Overall format
├── summarize-video.md    # Per-video format
├── summarize-blogs.md    # Per-blog format
└── summarize-tweets.md   # Per-builder tweet format
SKILL.md                  # Manual-trigger skill definition (reference)
CLAUDE.md                 # Project dev notes
```

The actual scheduled-task definition (the thing Claude Code runs every morning) lives **outside the repo** at `~/.claude/scheduled-tasks/ai-daily-digest/SKILL.md`, because that file is tied to my specific machine.

## Running it

This fork is not designed to be dropped into someone else's machine — paths, sources, and the Claude Code scheduled task are all hardcoded for my setup. If you want something similar, **start from the upstream repo**, not from this fork.

For my own reference:

```bash
cd scripts
python3 prepare_digest.py --lookback-hours 48 > /tmp/digest_data.json
# Claude Code reads that JSON and generates the Markdown digest
python3 deliver.py --file /tmp/fb-digest.md
```

## Credit

All the original design, curation, and "follow builders, not influencers" philosophy belongs to [Zara Zhang](https://x.com/zarazhangrui). This fork only rearranges her work to fit my own setup.

## License

MIT (inherited from upstream).
