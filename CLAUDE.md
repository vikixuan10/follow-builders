# CLAUDE.md — follow-builders（Weiwei 的 fork）

每天 08:13 自动生成 AI 早报（私密网页、概览邮件、Obsidian 存档）的脚本仓库。原版是 Zara Zhang 的 follow-builders，这里已全部改成 Python。

**本文件只写改代码时要守的规矩。** 别的都不在这里：

- 现状、等 Weiwei 定的、不要做的：`/Users/weiwei/Documents/Obsidian/AI-Digest/_meta/早报网页/交接.md`（动手前先读完「不要做的」）
- 做过什么、为什么：同库的 `log.md`
- 流程怎么走：`~/.claude/scheduled-tasks/ai-daily-digest/SKILL.md`，改流程只改这一份
- 每个提交为什么这么改：`git log`

## 在哪改，怎么上线

- 在旁边的工作目录 `/Users/weiwei/Documents/SKILL/follow-builders-加固`（hardening 分支）改，测试通过后在本目录 `git merge --ff-only hardening` 进 main，再推送。不要直接在 main 上改了等早上的运行来验证。
- 小改动提交后直接推送（2026-10-16 Weiwei 同意）。
- 改完写一行进 AI-Digest 库的 `log.md`；现状变了就改交接。

## 测试

- `~/.follow-builders/venv/bin/python -m unittest discover -s tests`，全部离线，不连网络。
- 验证 YouTube 相关的逻辑一律打桩；真实探测一次只试一个视频，一天最多一两个。

## 运行环境

- 抓取和发邮件用专用环境 `~/.follow-builders/venv`，库的版本固定在 `requirements.txt`；环境坏了运行 `bash scripts/setup_venv.sh` 重建。要加新库，同时改 `requirements.txt`。
- 定时任务用电脑上的 `python3` 调 `scripts/build_page.py`，所以 `build_page.py` 只能用标准库；需要第三方库的事，由它用专用环境去调别的脚本。
- 定时任务只运行 `build_page.py` 的固定子命令（`fetch`、`fetch-wait`、`view`、`build`、`deliver`、`fail`），不要给它加新形式的命令，否则会弹批准，早报出不来。
- 全部用 Python，不用 Node.js（这台电脑没有 Node）。
- 读 YouTube 频道网页要带同意 cookie（爱尔兰在欧盟，不带会先弹 GDPR 同意页），写法照 `fetch_youtube.py` 里的 `CONSENT_COOKIES`。

## 加减频道

1. 改 `config/my-sources.json`：加一条 `{"category": "...", "name": "...", "handle": "@xxx", "channelId": null}`，或删掉对应的一条。
2. 加了频道就运行一次 `~/.follow-builders/venv/bin/python scripts/resolve_channels.py`，它只去查还没有频道编号的那几个。
3. 下一次运行自动生效。
