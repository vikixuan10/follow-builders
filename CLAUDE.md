# CLAUDE.md — follow-builders (Weiwei's fork)

## 项目简介
这是 Weiwei 个人的 AI & 商业每日早报系统，fork 自 Zara Zhang 的 follow-builders。
用 Python 3.9 重写（原版是 Node.js，Weiwei 的机器没有 Node）。

## 关键路径
- 脚本: `scripts/` (Python: fetch_youtube.py, fetch_blogs.py, prepare_digest.py, deliver.py)
- 频道列表: `config/my-sources.json` (40 个 YouTube 频道，已解析 channelId)
- Prompts: `prompts/` (中文输出格式)
- 用户配置: `~/.follow-builders/config.json` + `.env`
- Obsidian 输出: `/Users/weiwei/Documents/Obsidian/AI-Digest/`
- 定时任务: `~/.claude/scheduled-tasks/ai-daily-digest/SKILL.md`

## 注意事项
- 不要用 Node.js 写新功能，全部用 Python
- Python 3.9 兼容性：不能用 `str | None`，要用 `Optional[str]`
- 爱尔兰在欧盟，YouTube 会弹 GDPR consent wall，需要 consent cookies
- Zara 的 .js 文件保留作参考，不要删
- deliver.py 发邮件时会把 Markdown 转 HTML（用 `markdown` 库）
- 邮件收件人: vikixuan10@gmail.com

## 添加新频道
1. 编辑 `config/my-sources.json`，加一条 `{"category": "...", "name": "...", "handle": "@xxx", "channelId": null}`
2. 运行 `python3 scripts/resolve_channels.py`
3. 下次 digest 自动包含

## 测试命令
```bash
cd scripts
python3 fetch_youtube.py --limit-channels 3 --lookback 168 --dry-run --no-transcripts 2>/dev/null
python3 prepare_digest.py --limit-channels 3 --lookback-hours 168 --dry-run 2>/dev/null | head -50
```
