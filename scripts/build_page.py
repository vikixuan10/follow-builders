#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 早报网页：把每天的抓取数据和「编排」拼成网页、存档用的 Markdown 和邮件正文。

分工（不要改）：
- 英文原文（帖子全文、博客段落）由本脚本从抓取数据里原样放进页面，模型不重打，所以一个字都不会变。
- 模型只写「编排」文件 edit.json：话题分组、每条的中文摘要、博客的一句话和要点、要摘的段落编号、视频的要点。
- 本脚本负责核对：每条帖子、每篇新博客、每个有字幕的视频都必须有去处，否则报错，不生成页面。

子命令（定时任务只用这几条固定的命令，这样放行这一个脚本就不会再弹批准）：
  fetch       在后台启动抓取（prepare_digest.py），立刻返回
  fetch-wait  等抓取结束并存成当天的 data.json；一次最多等 9 分钟，没抓完就再运行一次
  view        把当天的素材写成一份文本给模型读（帖子带编号，博客带段落编号）
  build       核对 edit.json，生成网页（含最近若干天，可切换）、Markdown 存档、邮件正文
  deliver     存进 Obsidian 并发邮件；网页没发布成功时加 --page-failed
  fail        早报没生成成功时，把「失败说明.txt」发成一封邮件
  ingest      （fetch-wait 会自动调用）把一份抓取结果存成当天的 data.json，同一天多次抓取会合并

位置都从 ~/.follow-builders/config.json 的 page 段读取：
  "page": {"dir": "<存放每天数据和网页的目录>", "url": "<网页的固定网址>", "keepDays": 14}
"""
import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

USER_DIR = Path.home() / ".follow-builders"
CONFIG_PATH = USER_DIR / "config.json"
SCRIPT_DIR = Path(__file__).resolve().parent
RAW_PATH = Path("/tmp/digest_data.json")       # 抓取结果
RAW_TMP = Path("/tmp/digest_data.fetching.json")  # 抓取进行中先写到这里
FETCH_STATE = Path("/tmp/fb-fetch-state.json")
WAIT_SECONDS = 540  # fetch-wait 一次最多等这么久，留在 Bash 工具 10 分钟的上限以内

WEEKDAYS = "一二三四五六日"
LONG_TEXT = 600      # 超过这个长度的帖子先只显示开头
HEAD_LIMIT = 420     # 开头最多显示到这里
WORDS_PER_MIN = 230
MIN_TRANSCRIPT = 300  # 字幕短于这个长度的视频（多半是 Shorts）不写摘要
MAX_PASSAGE = 800     # 摘的博客段落最长这么多字符
KINDS = ("同一场讨论", "同一作者", "同一话题")
# 模型写的中文里不许出现的说法（Weiwei 的禁词与这张页面上被纠正过的词）
LINT = ["——", "锚点", "立住", "接住", "元层", "落盘", "持久化", "跑通", "张力", "根子", "门道", "大意"]

URL_RE = re.compile(r"https?://[^\s<>\"']+")


# ---------- 基础 ----------

def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_tz(cfg):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(cfg.get("timezone") or "Europe/Dublin")
    except Exception:
        return timezone(timedelta(hours=1))


def page_dir(cfg):
    d = (cfg.get("page") or {}).get("dir")
    if not d:
        sys.exit("config.json 里没有 page.dir，不知道把每天的数据放在哪。")
    return Path(d).expanduser()


def esc(s):
    return html.escape(str(s), quote=True)


def linkify(text):
    """转义文本，并把其中的网址变成可点的链接。文字本身一个字不动。"""
    out, pos = [], 0
    for m in URL_RE.finditer(text):
        out.append(esc(text[pos:m.start()]))
        u = m.group(0)
        out.append('<a href="%s" target="_blank" rel="noopener">%s</a>' % (esc(u), esc(u)))
        pos = m.end()
    out.append(esc(text[pos:]))
    return "".join(out)


def split_long(text):
    """长帖切成「开头」和「其余」，两段拼起来与原文完全一致。"""
    if len(text) <= LONG_TEXT:
        return text, ""
    cut = text.rfind("\n", 0, HEAD_LIMIT)
    if cut < 120:
        cut = text.rfind(". ", 0, HEAD_LIMIT) + 1
    if cut < 120:
        cut = HEAD_LIMIT
    return text[:cut], text[cut:]


def to_local(iso, tz):
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(tz)


def fmt_time(iso, tz):
    dt = to_local(iso, tz)
    return "%d 月 %d 日 %02d:%02d" % (dt.month, dt.day, dt.hour, dt.minute)


def fmt_count(n):
    n = int(n or 0)
    if n < 10000:
        return str(n)
    s = "%.1f" % (n / 10000.0)
    return (s[:-2] if s.endswith(".0") else s) + " 万"


def md_date(date_str):
    y, m, d = date_str.split("-")
    return "%d 月 %d 日" % (int(m), int(d))


def ymd_date(date_str):
    y, m, d = date_str[:10].split("-")
    return "%d 年 %d 月 %d 日" % (int(y), int(m), int(d))


def day_label(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return "%d 月 %d 日，星期%s" % (dt.month, dt.day, WEEKDAYS[dt.weekday()])


def paragraphs(content):
    return [x.strip() for x in re.split(r"\n\s*\n|\n", html.unescape(content or "")) if x.strip()]


def text_fragment(para):
    """取段落里最靠前的 5 个纯字母数字的连续单词，拼成「在原文中查看这一段」的链接后缀。"""
    words = para.split()
    for i in range(0, max(1, len(words) - 4)):
        win = words[i:i + 5]
        if len(win) == 5 and all(re.fullmatch(r"[A-Za-z0-9]+", w) for w in win):
            return "#:~:text=" + "%20".join(quote(w, safe="") for w in win)
    return ""


def seen_before(url, archive_dir, today):
    """翻存档里今天以前的早报，返回收录过这个网址的日期（去重、升序）。"""
    dates = set()
    if not archive_dir:
        return []
    for p in Path(archive_dir).glob("AI-Digest-*.md"):
        m = re.match(r"AI-Digest-(\d{4}-\d{2}-\d{2})", p.name)
        if not m or m.group(1) >= today:
            continue
        try:
            if url in p.read_text(encoding="utf-8", errors="ignore"):
                dates.add(m.group(1))
        except OSError:
            continue
    return sorted(dates)


def ts_seconds(ts):
    parts = [int(x) for x in str(ts).strip().split(":")]
    sec = 0
    for x in parts:
        sec = sec * 60 + x
    return sec


class Day(object):
    """一天的数据：抓取到的原始内容 + 模型写的编排。"""

    def __init__(self, date, data, edit, archive_dir, tz):
        self.date, self.data, self.edit, self.tz = date, data, edit or {}, tz
        self.key = "d" + date.replace("-", "")
        self.by_id = {}
        for b in data.get("x", {}).get("builders", []):
            for t in b.get("tweets", []):
                self.by_id[str(t["id"])] = (b, t)
        self.posts = data.get("blogs", {}).get("posts", [])
        self.seen = dict((p["url"], seen_before(p["url"], archive_dir, date)) for p in self.posts)
        self.videos = data.get("youtube", {}).get("videos", [])
        self.video_err = next((x for x in data.get("errors", []) if x.lower().startswith("youtube")), "")
        self.before = None  # 这次运行开始前的编排（view 时留的快照）

    def has_transcript(self, v):
        return len(v.get("transcript") or "") >= MIN_TRANSCRIPT

    def new_posts(self):
        return [p for p in self.posts if not self.seen[p["url"]]]


# ---------- 核对 ----------

def model_texts(edit):
    """列出编排里所有由模型写的中文，供禁词检查。返回 (位置, 文字)。"""
    out = []
    for i, tp in enumerate(edit.get("topics", [])):
        out.append(("第 %d 个话题的标题" % (i + 1), tp.get("title", "")))
        out.append(("第 %d 个话题的说明" % (i + 1), tp.get("note", "")))
        for it in tp.get("tweets", []):
            out.append(("帖子 %s 的摘要" % it.get("id"), it.get("summary", "")))
    for it in edit.get("singles", []):
        out.append(("帖子 %s 的摘要" % it.get("id"), it.get("summary", "")))
    for it in edit.get("folded", []):
        out.append(("帖子 %s 的折叠原因" % it.get("id"), it.get("reason", "")))
    for b in edit.get("blogs", []):
        out.append(("博客 %s 的一句话" % b.get("url"), b.get("one_line", "")))
        for p in b.get("points", []):
            out.append(("博客 %s 的要点" % b.get("url"), p))
        for ps in b.get("passages", []):
            out.append(("博客 %s 的段落摘要" % b.get("url"), ps.get("summary", "")))
    for v in edit.get("videos", []):
        out.append(("视频 %s 的跳过原因" % v.get("id"), v.get("skip", "")))
        out.append(("视频 %s 的一句话" % v.get("id"), v.get("one_line", "")))
        for p in v.get("points", []):
            out.append(("视频 %s 的要点" % v.get("id"), p))
        for s in v.get("segments", []):
            out.append(("视频 %s 的片段说明" % v.get("id"), s.get("topic", "")))
    for i, pk in enumerate(edit.get("picks", []) or [], 1):
        out.append(("picks 第 %d 条的理由" % i, pk.get("why", "")))
    return out


def blog_anchor(day, url):
    return "%s-b-%s" % (day.key, hashlib.sha1(url.encode("utf-8")).hexdigest()[:8])


def resolve_pick(day, ref):
    """把「今天最值得看」里的一条 ref（帖子编号、博客网址或视频编号）对到页面上的条目。
    返回 {kind, title, source, anchor}；对不上返回 None。"""
    ref = str(ref or "").strip()
    edit = day.edit
    if ref in day.by_id:
        b, t = day.by_id[ref]
        for i, tp in enumerate(edit.get("topics", [])):
            if any(str(it.get("id")) == ref for it in tp.get("tweets", [])):
                return {"kind": "帖子", "title": tp.get("title", ""), "source": b["name"], "anchor": "%s-t%d" % (day.key, i + 1)}
        for it in edit.get("singles", []):
            if str(it.get("id")) == ref:
                summ = re.sub(r"^(他|她)(说|认为|提到|指出|表示|写道|分享了|介绍了)[，,：:]?", "", it.get("summary", "")).strip()
                return {"kind": "帖子", "title": summ[:40] + ("……" if len(summ) > 40 else ""), "source": b["name"],
                        "anchor": "%s-tw-%s" % (day.key, ref)}
        return None  # 折叠的帖子不该被选
    for post in day.posts:
        if post["url"] == ref:
            return {"kind": "博客", "title": post.get("title", ""), "source": post.get("blog", ""), "anchor": blog_anchor(day, ref)}
    for v in day.videos:
        if str(v.get("videoId")) == ref:
            ve = next((x for x in edit.get("videos", []) if str(x.get("id")) == ref), None)
            if not ve or "skip" in ve:
                return None  # 没写摘要的视频不该被选
            return {"kind": "视频", "title": v.get("title", ""), "source": v.get("channelName", ""),
                    "anchor": "%s-v-%s" % (day.key, ref)}
    return None


def pickable_count(day):
    edit = day.edit
    n = sum(len(tp.get("tweets", [])) for tp in edit.get("topics", [])) + len(edit.get("singles", []))
    n += len(day.new_posts())
    n += sum(1 for v in edit.get("videos", []) if "skip" not in v)
    return n


def check_picks(day):
    e, picks = [], day.edit.get("picks")
    need = min(3, pickable_count(day))
    if picks is None:
        return ["还没有写 picks（今天最值得看的 3 到 5 件事）。"] if need else []
    if not isinstance(picks, list):
        return ["picks 要写成列表。"]
    if not (need <= len(picks) <= 5):
        e.append("picks 要写 %d 到 5 条，现在是 %d 条。" % (need, len(picks)))
    seen = set()
    for i, pk in enumerate(picks, 1):
        ref = str(pk.get("ref", "")).strip()
        if ref in seen:
            e.append("picks 第 %d 条和前面重复了：%s" % (i, ref))
        seen.add(ref)
        if resolve_pick(day, ref) is None:
            e.append("picks 第 %d 条的 ref 对不上（要填帖子编号、博客网址或视频编号；不能选折叠的帖子和没写摘要的视频）：%s" % (i, ref))
        why = (pk.get("why") or "").strip()
        if not why:
            e.append("picks 第 %d 条没有写为什么值得看。" % i)
        elif len(why) > 60:
            e.append("picks 第 %d 条的理由太长（%d 字），60 字以内。" % (i, len(why)))
    return e


def placements(edit):
    """每条帖子、每篇博客在编排里的位置和文字，用来比较前后两版有没有被改动。"""
    out = {}
    for tp in edit.get("topics", []):
        for it in tp.get("tweets", []):
            out["帖子 %s" % it.get("id")] = ("话题", tp.get("title"), tp.get("kind"), tp.get("note"), it.get("summary"))
    for it in edit.get("singles", []):
        out["帖子 %s" % it.get("id")] = ("其他", it.get("summary"))
    for it in edit.get("folded", []):
        out["帖子 %s" % it.get("id")] = ("折叠", it.get("reason"))
    for b in edit.get("blogs", []):
        out["博客 %s" % b.get("url")] = json.dumps(b, ensure_ascii=False, sort_keys=True)
    for v in edit.get("videos", []):
        out["视频 %s" % v.get("id")] = json.dumps(v, ensure_ascii=False, sort_keys=True)
    return out


def check(day, guard=False):
    """返回问题清单；空清单表示可以生成。guard=True 时还要核对：这次运行开始前已有的条目没有被改动。"""
    e, edit, by_id = [], day.edit, day.by_id
    if not edit:
        return ["还没有 edit.json。"]
    if guard and day.before:
        was, now = placements(day.before), placements(edit)
        changed = [k for k in was if was[k] != now.get(k)]
        if changed:
            e.append("这次运行开始前已经写好的 %d 个条目被改动或删掉了（%s%s）。已有条目可能是人工校过的，"
                     "要逐字保留：照同目录的 edit.运行前.json 把它们恢复原样，只增加原来没有的条目。"
                     % (len(changed), "、".join(changed[:5]), " 等" if len(changed) > 5 else ""))
    placed = [str(it.get("id")) for tp in edit.get("topics", []) for it in tp.get("tweets", [])]
    placed += [str(it.get("id")) for it in edit.get("singles", [])] + [str(it.get("id")) for it in edit.get("folded", [])]
    dup = sorted(set(i for i in placed if placed.count(i) > 1))
    missing = sorted(set(by_id) - set(placed))
    unknown = sorted(set(placed) - set(by_id))
    if dup:
        e.append("这些帖子出现了不止一次：%s" % "、".join(dup))
    if missing:
        e.append("这些帖子还没有安排去处（放进某个话题、singles 或 folded）：%s" % "、".join(missing))
    if unknown:
        e.append("这些编号在今天的数据里不存在：%s" % "、".join(unknown))

    for i, tp in enumerate(edit.get("topics", [])):
        name = "第 %d 个话题" % (i + 1)
        ids = [str(it.get("id")) for it in tp.get("tweets", []) if str(it.get("id")) in by_id]
        if not (tp.get("title") or "").strip():
            e.append("%s没有标题。" % name)
        if tp.get("kind") not in KINDS:
            e.append("%s的 kind 必须是这三个之一：%s。" % (name, " / ".join(KINDS)))
        if len(tp.get("tweets", [])) < 2:
            e.append("%s只有一条帖子。只有一条的放进 singles，不单独成话题。" % name)
        if tp.get("kind") == "同一场讨论" and ids:
            linked = any(str(by_id[a][1].get("quotedTweetId") or "") in ids for a in ids)
            if not linked:
                e.append("%s标了「同一场讨论」，但这几条之间没有引用关系。改成「同一话题」，或重新分组。" % name)
        if tp.get("kind") == "同一作者" and ids:
            if len(set(by_id[a][0].get("handle") for a in ids)) > 1:
                e.append("%s标了「同一作者」，但里面不止一位作者。" % name)
        for it in tp.get("tweets", []):
            if not (it.get("summary") or "").strip():
                e.append("帖子 %s 没有写摘要。" % it.get("id"))
    for it in edit.get("singles", []):
        if not (it.get("summary") or "").strip():
            e.append("帖子 %s 没有写摘要。" % it.get("id"))
    for it in edit.get("folded", []):
        if not (it.get("reason") or "").strip():
            e.append("折叠的帖子 %s 没有写原因。" % it.get("id"))

    blog_edit = dict((b.get("url"), b) for b in edit.get("blogs", []))
    for p in day.new_posts():
        b = blog_edit.get(p["url"])
        if not b:
            e.append("新博客还没有写：%s" % p["url"])
            continue
        if not (b.get("one_line") or "").strip():
            e.append("博客 %s 没有写一句话。" % p["url"])
        lo = 3 if guard else 2  # 往期按当时的规矩（2 到 4 条）写的，照样放进页面
        if len(p.get("content") or "") < 1500:
            lo = 1  # 正文只取到很短一段时（个别网页结构特殊），不硬凑要点
        if not (lo <= len(b.get("points", [])) <= 5):
            e.append("博客 %s 的要点要写 3 到 5 条，每条只讲一件事。" % p["url"])
    for url, b in blog_edit.items():
        post = next((p for p in day.posts if p["url"] == url), None)
        if not post:
            e.append("编排里的博客在今天的数据里不存在：%s" % url)
            continue
        paras = paragraphs(post.get("content"))
        if len(b.get("passages", [])) > 2:
            e.append("博客 %s 摘的段落最多 2 段。" % url)
        for ps in b.get("passages", []):
            n = ps.get("para")
            if not isinstance(n, int) or n < 0 or n >= len(paras):
                e.append("博客 %s 的段落编号 %r 不存在（素材里共 %d 段，从 0 起）。" % (url, n, len(paras)))
            elif len(paras[n]) > MAX_PASSAGE:
                e.append("博客 %s 的第 %d 段太长（%d 字符），换一段 %d 字符以内的。" % (url, n, len(paras[n]), MAX_PASSAGE))
            if not (ps.get("summary") or "").strip():
                e.append("博客 %s 的第 %r 段没有写摘要。" % (url, n))

    video_edit = dict((str(v.get("id")), v) for v in edit.get("videos", []))
    vids = dict((str(v.get("videoId")), v) for v in day.videos)
    for vid, v in vids.items():
        if day.has_transcript(v) and vid not in video_edit:
            e.append("有字幕的视频还没有写：%s（%s）" % (vid, v.get("title")))
    for vid, ve in video_edit.items():
        if vid not in vids:
            e.append("编排里的视频在今天的数据里不存在：%s" % vid)
            continue
        if "skip" in ve:
            if not (ve.get("skip") or "").strip():
                e.append("视频 %s 标了跳过，但没有写原因。" % vid)
            continue
        if not (ve.get("one_line") or "").strip():
            e.append("视频 %s 没有写一句话。" % vid)
        if not (3 <= len(ve.get("points", [])) <= 8):
            e.append("视频 %s 的要点要写 3 到 8 条。" % vid)
        for s in ve.get("segments", []):
            for k in ("start", "end"):
                if s.get(k) and not re.fullmatch(r"(\d{1,2}:)?\d{1,2}:\d{2}", str(s[k])):
                    e.append("视频 %s 的片段时间 %r 写法不对，要写成 MM:SS。" % (vid, s[k]))
            if not s.get("start"):
                e.append("视频 %s 有片段没有写开始时间。" % vid)

    if guard:
        e += check_picks(day)

    for where, text in model_texts(edit):
        for w in LINT:
            if w in (text or ""):
                e.append("%s里用了「%s」，换一种说法。" % (where, w))
        if "\u201c" in (text or "") or "\u201d" in (text or ""):
            e.append("%s里用了弯引号，改成直角引号「」。" % where)
    return e


# ---------- 网页各块 ----------

def render_orig(t):
    head, tail = split_long(t["text"])
    h = '<div class="orig-wrap"><div class="orig" lang="en">%s' % linkify(head)
    if tail:
        h += '<span class="more" hidden>%s</span>' % linkify(tail)
    h += "</div>"
    if tail:
        n = int(round(len(t["text"].split()), -1))
        h += ('<button type="button" class="expand" aria-expanded="false" '
              'data-label="展开全文 · 约 %d 词">展开全文 · 约 %d 词</button>' % (n, n))
    h += "</div>"
    return h


def quote_note(t, day):
    if not t.get("isQuote"):
        return ""
    q = str(t.get("quotedTweetId") or "")
    if q in day.by_id:
        return '<p class="qnote">引用的是上面 <a href="#%s-tw-%s">%s 的那条帖子</a>。</p>' % (
            day.key, esc(q), esc(day.by_id[q][0]["name"]))
    return '<p class="qnote">这条引用了另一条帖子，被引用的内容需到 X 上查看。</p>'


def render_voice(item, day, roles):
    b, t = day.by_id[str(item["id"])]
    role = roles.get(b.get("handle"), "")
    parts = ['<article class="voice" id="%s-tw-%s">' % (day.key, esc(t["id"]))]
    parts.append('<div class="who"><span class="name">%s</span>%s</div>' % (
        esc(b["name"]), ('<span class="role">%s</span>' % esc(role)) if role else ""))
    parts.append('<div class="stamp">%s · 赞 %s · 转发 %s</div>' % (
        fmt_time(t["createdAt"], day.tz), fmt_count(t.get("likes")), fmt_count(t.get("retweets"))))
    parts.append('<div class="texts">')
    parts.append(render_orig(t))
    parts.append('<div class="gist"><span class="lab">摘要</span>%s</div>' % esc(item.get("summary", "")))
    parts.append("</div>")
    parts.append(quote_note(t, day))
    parts.append('<a class="src" href="%s" target="_blank" rel="noopener">查看原帖 ↗</a>' % esc(t["url"]))
    parts.append("</article>")
    return "".join(parts)


def render_folded(item, day):
    b, t = day.by_id[str(item["id"])]
    return ('<li><div class="f-head"><span class="name">%s</span><span class="reason">%s</span></div>'
            '<div class="orig" lang="en">%s</div>'
            '<a class="src" href="%s" target="_blank" rel="noopener">查看原帖 ↗</a></li>') % (
        esc(b["name"]), esc(item.get("reason", "")), linkify(t["text"]), esc(t["url"]))


def blog_meta(post, dates):
    src = esc(post.get("blog", ""))
    if post.get("publishedAt"):
        src = "%s · %s发布" % (src, ymd_date(str(post["publishedAt"])))
    if dates:
        # 文章发布年份和首次收录年份不同时，把年份写出来，免得读者算不清
        first = ymd_date(dates[0]) if str(post.get("publishedAt") or "")[:4] != dates[0][:4] else md_date(dates[0])
        src = "%s · 此前已收录 %d 次，首次收录于 %s" % (src, len(dates), first)
    return src


def read_label(post):
    words = len(html.unescape(post.get("content") or "").split())
    minutes = max(1, int(round(words / float(WORDS_PER_MIN))))
    return ("阅读全文 · %d 分钟以上" if post.get("contentTruncated") else "阅读全文 · 约 %d 分钟") % minutes


def render_blog(post, entry, dates, anchor=""):
    body = ['<div class="blog-body">']
    if entry:
        paras = paragraphs(post.get("content"))
        body.append('<p class="one">%s</p>' % esc(entry.get("one_line", "")))
        body.append('<ul class="points">%s</ul>' % "".join("<li>%s</li>" % esc(p) for p in entry.get("points", [])))
        ps_list = entry.get("passages", [])
        if ps_list:
            body.append('<h4 class="sub">%s</h4>' % ("值得一读的两段原文" if len(ps_list) == 2 else "值得一读的原文段落"))
        for ps in ps_list:
            para = paras[ps["para"]]
            body.append('<div class="texts passage">')
            body.append('<div class="orig-wrap"><blockquote class="orig" lang="en">%s</blockquote></div>' % esc(para))
            body.append('<div class="gist"><span class="lab">摘要</span>%s</div>' % esc(ps.get("summary", "")))
            body.append("</div>")
            body.append('<a class="src" href="%s" target="_blank" rel="noopener">在原文中查看这一段 ↗</a>'
                        % esc(post["url"] + text_fragment(para)))
    body.append('<p class="read"><a class="src" href="%s" target="_blank" rel="noopener">%s ↗</a></p>'
                % (esc(post["url"]), read_label(post)))
    body.append("</div>")
    title = '<span class="b-title">%s</span>' % esc(post.get("title", ""))
    sub = '<span class="b-sub">%s</span>' % blog_meta(post, dates)
    aid = (' id="%s"' % anchor) if anchor else ""
    if dates:
        return '<details class="fold blog"%s><summary><span class="sum-text">%s%s</span></summary>%s</details>' % (
            aid, title, sub, "".join(body))
    return '<article class="card blog"%s><div class="b-head">%s%s</div>%s</article>' % (aid, title, sub, "".join(body))


def video_link(v, start=None):
    url = v.get("url", "")
    if start:
        url += ("&" if "?" in url else "?") + "t=%ds" % ts_seconds(start)
    return url


def render_video(v, entry, day):
    meta = [esc(v.get("channelName", ""))]
    if v.get("category"):
        meta.append(esc(v["category"]))
    if v.get("publishedAt"):
        meta.append(fmt_time(v["publishedAt"], day.tz))
    # 默认折起来：只露标题、频道和时间、一句话；点开才是要点、片段和链接（2026-10-12 Weiwei 选定，
    # 此前每张都摊开，20 个视频在手机上要滑三十多屏）。
    parts = ['<details class="fold video" id="%s-v-%s"><summary><span class="sum-text">' % (day.key, esc(v.get("videoId")))]
    parts.append('<span class="b-title">%s</span>' % esc(v.get("title", "")))
    parts.append('<span class="b-sub">%s</span>' % " · ".join(meta))
    parts.append('<span class="v-one">%s</span>' % esc(entry.get("one_line", "")))
    parts.append('</span></summary><div class="video-body">')
    parts.append('<ul class="points">%s</ul>' % "".join("<li>%s</li>" % esc(p) for p in entry.get("points", [])))
    segs = entry.get("segments", [])
    if segs:
        parts.append('<h4 class="sub">值得看的片段</h4><ul class="segs">')
        for s in segs:
            span = str(s["start"]) + ((" – " + str(s["end"])) if s.get("end") else "")
            parts.append('<li><a href="%s" target="_blank" rel="noopener">%s</a><span>%s</span></li>' % (
                esc(video_link(v, s["start"])), esc(span), esc(s.get("topic", ""))))
        parts.append("</ul>")
    parts.append('<a class="src" href="%s" target="_blank" rel="noopener">在 YouTube 上观看 ↗</a>' % esc(v.get("url", "")))
    parts.append("</div></details>")
    return "".join(parts)


def day_facts(day):
    """当天的几个数字和概览用的句子，网页、Markdown、邮件共用。"""
    edit = day.edit
    n_blogs, n_new = len(day.posts), len(day.new_posts())
    video_edit = dict((str(v.get("id")), v) for v in edit.get("videos", []))
    def summarized(v):
        ve = video_edit.get(str(v.get("videoId")))
        return bool(ve) and "skip" not in ve
    with_sum = [v for v in day.videos if summarized(v)]
    without = [v for v in day.videos if not summarized(v)]
    blog_count = "%d 篇博客" % n_blogs
    if n_blogs and not n_new:
        blog_count += "（此前均已收录）"
    if day.videos:
        video_count = "%d 个视频" % len(day.videos)
    else:
        video_count = "视频获取失败" if day.video_err else "今日无视频"
    counts = "%d 条帖子 · %s · %s" % (len(day.by_id), blog_count, video_count)
    topics = []
    for i, tp in enumerate(edit.get("topics", [])):
        names = []
        for it in tp["tweets"]:
            nm = day.by_id[str(it["id"])][0]["name"]
            if nm not in names:
                names.append(nm)
        topics.append({"anchor": "%s-t%d" % (day.key, i + 1), "title": tp["title"], "names": "、".join(names),
                       "n": len(tp["tweets"]), "tp": tp})
    rest = []  # (文字, 锚点或空)
    if edit.get("singles"):
        rest.append(("另有 %d 条帖子不属于以上话题。", len(edit["singles"]), day.key + "-singles"))
    if with_sum:
        rest.append(("视频有 %d 个写了摘要。", len(with_sum), day.key + "-videos"))
    if without:
        rest.append(("有 %d 个新视频没有摘要，只列了标题。" if not with_sum else "另有 %d 个视频只列了标题。",
                     len(without), day.key + "-videos"))
    if n_blogs:
        if n_new:
            rest.append(("博客今天有 %d 篇新文章。", n_new, day.key + "-blogs"))
        else:
            rest.append(("博客今天没有新文章，获取到的 %d 篇此前都已收录。", n_blogs, day.key + "-blogs"))
    if edit.get("folded"):
        rest.append(("还有 %d 条帖子没有实质内容或与 AI 无关，已折叠，放在页面末尾。", len(edit["folded"]), day.key + "-folded"))
    video_note = ""
    if not day.videos and day.video_err:
        video_note = "今天未能获取视频" + ("，原因是获取超时。" if "timed out" in day.video_err else "。")
    return {"counts": counts, "topics": topics, "rest": rest, "video_note": video_note,
            "with_sum": with_sum, "without": without, "video_edit": video_edit, "n_new": n_new, "n_blogs": n_blogs}


def render_day(day, hidden):
    edit, roles, f = day.edit, day.edit.get("roles", {}), day_facts(day)
    out = ['<section class="day" data-date="%s" data-label="%s"%s>' % (day.date, day_label(day.date), " hidden" if hidden else "")]
    out.append('<p class="counts">%s</p>' % esc(f["counts"]))

    picks = [(pk, resolve_pick(day, pk.get("ref"))) for pk in (edit.get("picks") or [])]
    picks = [(pk, r) for pk, r in picks if r]
    if picks:
        pl = ['<nav class="overview picks" aria-label="最值得看"><div class="eyebrow">%s</div><ol>'
              % ("今天最值得看" if not hidden else "当天最值得看")]
        for pk, r in picks:
            pl.append('<li><a href="#%s"><span class="pk-kind">%s</span><span class="ov-main">'
                      '<span class="ov-title">%s</span><span class="ov-who">%s · %s</span></span></a></li>'
                      % (r["anchor"], r["kind"], esc(r["title"]), esc(r["source"]), esc(pk.get("why", ""))))
        pl.append("</ol></nav>")
        out.append("".join(pl))

    ov = ['<nav class="overview" aria-label="当日概览"><div class="eyebrow">%s</div><ul>' % ("今日概览" if not hidden else "当日概览")]
    for tp in f["topics"]:
        ov.append('<li><a href="#%s"><span class="ov-main"><span class="ov-title">%s</span>'
                  '<span class="ov-who">%s</span></span><span class="ov-n">%d 条</span></a></li>'
                  % (tp["anchor"], esc(tp["title"]), esc(tp["names"]), tp["n"]))
    ov.append("</ul>")
    rest = ['<a href="#%s">%s</a>' % (anchor, esc(text % n)) for text, n, anchor in f["rest"]]
    if f["video_note"]:
        rest.append(esc(f["video_note"]))
    if rest:
        ov.append('<p class="ov-rest">%s</p>' % "".join(rest))
    ov.append("</nav>")
    out.append("".join(ov))

    out.append('<div class="switch" role="group" aria-label="显示顺序"><span>显示顺序</span>'
               '<button type="button" id="order-orig-%s" data-order="orig" aria-pressed="true">原文在前</button>'
               '<button type="button" id="order-zh-%s" data-order="zh" aria-pressed="false">摘要在前</button></div>'
               % (day.key, day.key))

    for tp in f["topics"]:
        t = tp["tp"]
        out.append('<section class="group" id="%s"><div class="group-head"><span class="kind">%s</span>'
                   '<h2>%s</h2><p class="note">%s</p></div><div class="card">%s</div></section>'
                   % (tp["anchor"], esc(t["kind"]), esc(t["title"]), esc(t.get("note", "")),
                      "".join(render_voice(it, day, roles) for it in t["tweets"])))

    if edit.get("singles"):
        out.append('<section class="group" id="%s-singles"><div class="group-head"><h2>其他帖子</h2></div>'
                   '<div class="card">%s</div></section>'
                   % (day.key, "".join(render_voice(it, day, roles) for it in edit["singles"])))

    if day.videos:
        vh = ['<section class="group" id="%s-videos"><div class="group-head"><h2>视频</h2></div>' % day.key]
        if f["with_sum"]:
            vh.append("".join(
                render_video(v, f["video_edit"][str(v.get("videoId"))], day) for v in f["with_sum"]))
        if f["without"]:
            def why_not(v):
                ve = f["video_edit"].get(str(v.get("videoId"))) or {}
                err = str(v.get("transcriptError") or "")
                if ve.get("skip"):
                    reason = ve["skip"]
                elif err == "over_channel_cap":
                    reason = "这个频道今天的视频超过 5 个，只列标题"
                elif "ip_block" in err:
                    reason = "这次没取到字幕，下次会再试"
                else:
                    reason = ""
                return v.get("channelName", "") + ((" · " + reason) if reason else "")
            items = "".join('<li><div class="f-head"><span class="name">%s</span><span class="reason">%s</span></div>'
                            '<a class="src" href="%s" target="_blank" rel="noopener">在 YouTube 上观看 ↗</a></li>'
                            % (esc(v.get("title", "")), esc(why_not(v)), esc(v.get("url", "")))
                            for v in f["without"])
            blocked = sum(1 for v in f["without"] if "ip_block" in str(v.get("transcriptError") or ""))
            capped = sum(1 for v in f["without"] if str(v.get("transcriptError") or "") == "over_channel_cap")
            why = ("YouTube 暂时没让取字幕，所以只能列出标题" if blocked == len(f["without"])
                   else "同一频道当天的视频超过 5 个，多出来的只列标题" if capped == len(f["without"])
                   else "没有取到字幕、视频很短、与主题无关，或同一频道当天超过 5 个")
            vh.append('<details class="fold"><summary><span class="sum-text"><span class="b-title">%s%d 个视频没有摘要</span>'
                      '<span class="b-sub">%s</span></span></summary>'
                      '<ul class="folded-list">%s</ul></details>'
                      % ("另有 " if f["with_sum"] else "", len(f["without"]), why, items))
        vh.append("</section>")
        out.append("".join(vh))

    if day.posts:
        blog_edit = dict((b.get("url"), b) for b in edit.get("blogs", []))
        note = "" if f["n_new"] else ('<p class="note">今天没有新文章。获取到的 %d 篇此前都已收录，已折叠，点击可展开。</p>' % f["n_blogs"])
        cards = "".join(render_blog(p, blog_edit.get(p["url"]), day.seen[p["url"]], blog_anchor(day, p["url"]))
                        for p in sorted(day.posts, key=lambda p: bool(day.seen[p["url"]])))
        out.append('<section class="group" id="%s-blogs"><div class="group-head"><h2>博客</h2>%s</div>%s</section>'
                   % (day.key, note, cards))

    if edit.get("folded"):
        out.append('<section class="group" id="%s-folded"><details class="fold"><summary><span class="sum-text">'
                   '<span class="b-title">已折叠 %d 条帖子</span><span class="b-sub">没有实质内容，或与 AI 无关</span>'
                   '</span></summary><ul class="folded-list">%s</ul></details></section>'
                   % (day.key, len(edit["folded"]), "".join(render_folded(it, day) for it in edit["folded"])))

    gen = to_local(day.data.get("generatedAt"), day.tz) if day.data.get("generatedAt") else None
    when = ("数据更新于 %d 月 %d 日 %02d:%02d。" % (gen.month, gen.day, gen.hour, gen.minute)) if gen else ""
    out.append('<footer class="site-footer">英文原文由程序原样引用，未作改动。话题分组、摘要和要点由 AI 生成；'
               '摘要是转述，不是逐句翻译。%s</footer>' % when)
    out.append("</section>")
    return "".join(out)


CSS = r"""
/* 样式：Weiwei 自己看的网页用「暖纸」定稿（2026-08-07 选定）：暖纸底、白卡片、细线，全页只有石墨青一个强调色，不用金色和阴影。
   字体：全页只用思源黑体（2026-10-12 定），标题靠字号和字重区分，不换字体。
   布局：单栏窄版，手机优先。最上面是今日概览，下面按话题分卡片；卡片里一人一段，原话在上、摘要在下。 */
:root{
  --font:"Noto Sans SC","Source Han Sans SC","思源黑体","PingFang SC","Microsoft YaHei",sans-serif;
  --bg:#F5F1E8; --paper:#FFFFFF; --paper-2:#EFEAE0;
  --ink:#2B2A28; --ink-soft:#45443F; --muted:#7E7A70;
  --hairline:#E3DCCD; --hairline-2:#D4CCBA;
  --accent:#46656F; --accent-bg:rgba(70,101,111,.08);
  --fs-h1:20px; --fs-h2:17px; --fs-h3:15.5px; --fs-body:15px; --fs-small:13px; --fs-xs:12px;
}
@media (min-width:768px){
  :root{ --fs-h1:24px; --fs-h2:19px; --fs-h3:16.5px; --fs-body:16px; }
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#191B1E; --paper:#202327; --paper-2:#26292D;
    --ink:#E6E8EA; --ink-soft:#C9CCD0; --muted:#979CA2;
    --hairline:#33373B; --hairline-2:#3E4348;
    --accent:#86A9C5; --accent-bg:rgba(134,169,197,.10);
    color-scheme:dark;
  }
}
:root[data-theme="dark"]{
  --bg:#191B1E; --paper:#202327; --paper-2:#26292D;
  --ink:#E6E8EA; --ink-soft:#C9CCD0; --muted:#979CA2;
  --hairline:#33373B; --hairline-2:#3E4348;
  --accent:#86A9C5; --accent-bg:rgba(134,169,197,.10);
  color-scheme:dark;
}
*,*::before,*::after{ box-sizing:border-box; }
[hidden]{ display:none !important; }
html{ scroll-behavior:smooth; }
body{
  margin:0; font-family:var(--font); background:var(--bg); color:var(--ink);
  line-height:1.85; font-size:var(--fs-body); -webkit-font-smoothing:antialiased;
}
h1,h2,h3,h4{ color:var(--ink); margin:0; line-height:1.4; font-weight:600; text-wrap:balance; }
h1{ font-size:var(--fs-h1); letter-spacing:.01em; }
h2{ font-size:var(--fs-h2); }
h3{ font-size:var(--fs-h3); }
p{ margin:0; color:var(--ink-soft); }
a{ color:var(--accent); text-decoration:none; border-bottom:1px solid var(--hairline-2); }
a:hover{ border-color:var(--accent); }
:focus-visible{ outline:2px solid var(--accent); outline-offset:2px; border-radius:4px; }

.site-header{
  position:sticky; top:env(safe-area-inset-top,0px); z-index:50;
  background:color-mix(in srgb,var(--bg) 92%,transparent);
  -webkit-backdrop-filter:blur(10px); backdrop-filter:blur(10px);
  border-bottom:1px solid var(--hairline);
}
.site-header-inner{
  max-width:720px; margin:0 auto; padding:10px 16px;
  display:flex; align-items:center; justify-content:space-between; gap:16px;
}
.logo{ font-size:15px; font-weight:600; color:var(--ink); }
.theme-toggle{
  background:var(--paper); border:1px solid var(--hairline-2); color:var(--ink);
  padding:7px 12px; border-radius:6px; cursor:pointer; font:400 13px var(--font); min-height:36px;
}
.theme-toggle:hover{ background:var(--paper-2); }

.shell{ max-width:720px; margin:0 auto; padding-inline:16px; padding-block:28px 48px; display:flex; flex-direction:column; gap:28px; }
@media (min-width:768px){ .shell{ padding-inline:24px; padding-block:40px 64px; gap:36px; } .site-header-inner{ padding:12px 24px; } }

.eyebrow{ font-size:12.5px; font-weight:600; letter-spacing:.1em; color:var(--accent); }

.hero{ display:flex; flex-direction:column; gap:6px; }
.hero .counts{ color:var(--muted); font-size:var(--fs-small); }

.overview{ background:var(--paper); border:1px solid var(--hairline); border-radius:8px; padding:14px 16px; display:flex; flex-direction:column; gap:4px; }
.picks ol{ list-style:none; margin:0; padding:0; }
.picks li{ border-top:1px solid var(--hairline); }
.picks li:first-child{ border-top:0; }
.picks li a{ display:flex; align-items:flex-start; gap:12px; padding:11px 0; border:0; min-height:44px; color:inherit; }
.pk-kind{ flex:none; font-size:var(--fs-xs); color:var(--accent); border:1px solid var(--hairline-2); padding:1px 8px; border-radius:99px; margin-top:3px; white-space:nowrap; }
.overview ul{ list-style:none; margin:0; padding:0; }
.overview li{ border-top:1px solid var(--hairline); }
.overview li:first-child{ border-top:0; }
.overview li a{ display:flex; align-items:center; gap:12px; padding:11px 0; border:0; min-height:44px; color:inherit; }
.ov-main{ flex:1; min-width:0; display:flex; flex-direction:column; }
.ov-title{ font-weight:600; font-size:var(--fs-body); color:var(--ink); line-height:1.5; }
.ov-who{ font-size:var(--fs-small); color:var(--muted); line-height:1.6; }
.ov-n{ flex:none; font-size:var(--fs-xs); color:var(--muted); border:1px solid var(--hairline-2); padding:1px 9px; border-radius:99px; white-space:nowrap; font-variant-numeric:tabular-nums; }
.ov-rest{ border-top:1px solid var(--hairline); padding-top:11px; font-size:var(--fs-small); color:var(--muted); line-height:1.8; }
.ov-rest a{ color:var(--muted); }

.switch{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; font-size:var(--fs-small); color:var(--muted); }
.switch button{
  background:var(--paper); border:1px solid var(--hairline-2); color:var(--ink-soft);
  padding:6px 13px; border-radius:99px; cursor:pointer; font:400 13px var(--font); min-height:36px;
}
.switch button[aria-pressed="true"]{ background:var(--accent-bg); border-color:var(--accent); color:var(--ink); font-weight:600; }

.group{ display:flex; flex-direction:column; gap:12px; scroll-margin-top:72px; }
.group-head{ display:flex; flex-direction:column; gap:8px; align-items:flex-start; }
.group-head h2{ align-self:stretch; padding-bottom:.5em; border-bottom:1px solid var(--hairline); }
.kind{ font-size:var(--fs-xs); color:var(--muted); border:1px solid var(--hairline-2); background:var(--paper); padding:1px 9px; border-radius:99px; }
.note{ font-size:var(--fs-small); color:var(--muted); line-height:1.7; }

.card{ background:var(--paper); border:1px solid var(--hairline); border-radius:8px; padding:0 16px; }
.voice{ padding:18px 0; border-top:1px solid var(--hairline); display:flex; flex-direction:column; align-items:flex-start; gap:8px; scroll-margin-top:72px; }
.voice:first-child{ border-top:0; }
.who{ display:flex; flex-wrap:wrap; align-items:baseline; gap:0 10px; }
.name{ font-weight:600; font-size:var(--fs-h3); color:var(--ink); }
.role{ font-size:var(--fs-small); color:var(--muted); }
.stamp{ font-size:var(--fs-xs); color:var(--muted); font-variant-numeric:tabular-nums; line-height:1.5; }
.texts{ display:flex; flex-direction:column; gap:10px; align-self:stretch; min-width:0; }
.orig-wrap{ display:flex; flex-direction:column; align-items:flex-start; min-width:0; }
.orig{ white-space:pre-wrap; overflow-wrap:anywhere; color:var(--ink); line-height:1.7; margin:0; align-self:stretch; }
.orig a{ color:var(--muted); }
/* 思源黑体把弯引号、撇号、破折号、省略号做成中文全角标点，放在英文里单词中间会多出空隙。
   所以英文段落里只这几个标点改用系统自带的英文字形，其余文字仍是思源黑体。 */
@font-face{
  font-family:"EnPunct";
  src:local("Helvetica Neue"),local("HelveticaNeue"),local("Segoe UI"),local("Roboto"),local("Arial");
  unicode-range:U+2013,U+2014,U+2018,U+2019,U+201C,U+201D,U+2026;
}
[lang="en"]{ font-family:"EnPunct",var(--font); }
.expand{ background:none; border:0; padding:6px 0; min-height:36px; cursor:pointer; color:var(--accent); font:600 13px var(--font); }
.gist{ background:var(--bg); border-radius:6px; padding:10px 12px; color:var(--ink-soft); font-size:14px; line-height:1.85; }
.gist .lab{ font-size:var(--fs-xs); font-weight:600; letter-spacing:.1em; color:var(--accent); margin-right:8px; }
.zh-first .gist{ order:-1; }
.qnote{ font-size:var(--fs-small); color:var(--muted); line-height:1.6; }
.qnote a{ color:var(--muted); }
.src{ font-size:13px; font-weight:600; }

details.fold{ background:var(--paper); border:1px solid var(--hairline); border-radius:8px; }
details.fold > summary{ list-style:none; cursor:pointer; padding:12px 14px; min-height:44px; display:flex; align-items:center; gap:12px; }
details.fold > summary::-webkit-details-marker{ display:none; }
details.fold > summary::before{
  content:""; flex:none; width:7px; height:7px; margin-left:2px;
  border-right:1.5px solid var(--muted); border-bottom:1.5px solid var(--muted);
  transform:rotate(-45deg); transition:transform .15s;
}
details.fold[open] > summary::before{ transform:rotate(45deg); }
@media (prefers-reduced-motion:reduce){ details.fold > summary::before{ transition:none; } html{ scroll-behavior:auto; } }
.sum-text{ display:flex; flex-direction:column; min-width:0; }
.b-title{ font-weight:600; font-size:var(--fs-h3); color:var(--ink); line-height:1.5; }
.b-sub{ font-size:var(--fs-small); color:var(--muted); line-height:1.6; }
.b-head{ display:flex; flex-direction:column; padding-top:16px; }
.blog-body{ padding:4px 14px 16px; display:flex; flex-direction:column; align-items:flex-start; gap:12px; border-top:1px solid var(--hairline); }
.card.blog .blog-body{ padding-inline:0; border-top:0; }
.blog-body .one{ color:var(--ink); padding-top:12px; }
.points{ margin:0; padding-left:1.2em; color:var(--ink-soft); display:flex; flex-direction:column; gap:4px; }
.sub{ font-size:var(--fs-small); font-weight:600; color:var(--muted); padding-top:4px; }
.passage blockquote.orig{ border-left:2px solid var(--accent); padding-left:12px; }
.read{ padding-top:4px; }

.folded-list{ list-style:none; margin:0; padding:0 14px 6px; border-top:1px solid var(--hairline); }
.folded-list li{ padding:12px 0; border-top:1px solid var(--hairline); display:flex; flex-direction:column; align-items:flex-start; gap:6px; }
.folded-list li:first-child{ border-top:0; }
.f-head{ display:flex; flex-wrap:wrap; align-items:baseline; gap:0 10px; }
.reason{ font-size:var(--fs-small); color:var(--muted); }
.folded-list .orig{ color:var(--ink-soft); font-size:14px; }

.site-footer{ border-top:1px solid var(--hairline); padding-top:16px; font-size:var(--fs-xs); color:var(--muted); line-height:1.8; }
"""

JS = r"""
(function(){
  var root = document.documentElement;
  var KEY_T = 'ai-zaobao-theme', KEY_O = 'ai-zaobao-order';
  function get(k){ try { return localStorage.getItem(k); } catch (e) { return null; } }
  function set(k, v){ try { localStorage.setItem(k, v); } catch (e) {} }

  var tbtn = document.querySelector('.theme-toggle');
  function isDark(){
    var t = root.getAttribute('data-theme');
    if (t) return t === 'dark';
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }
  function label(){ tbtn.textContent = isDark() ? '☀ 浅色' : '☾ 深色'; }
  var st = get(KEY_T);
  if (st === 'dark' || st === 'light') root.setAttribute('data-theme', st);
  label();
  tbtn.addEventListener('click', function(){
    var next = isDark() ? 'light' : 'dark';
    root.setAttribute('data-theme', next); set(KEY_T, next); label();
  });

  var KEY_S = 'ai-zaobao-style';
  var sbtns = document.querySelectorAll('.stylesw button');
  function applyStyle(v){
    if (v === 'gold') root.setAttribute('data-style', 'gold'); else root.removeAttribute('data-style');
    for (var i = 0; i < sbtns.length; i++) sbtns[i].setAttribute('aria-pressed', String(sbtns[i].getAttribute('data-style') === v));
  }
  applyStyle(get(KEY_S) === 'gold' ? 'gold' : 'paper');
  for (var j = 0; j < sbtns.length; j++) sbtns[j].addEventListener('click', function(){
    var v = this.getAttribute('data-style'); applyStyle(v); set(KEY_S, v);
  });

  var obtns = document.querySelectorAll('.switch button');
  function applyOrder(v){
    document.body.classList.toggle('zh-first', v === 'zh');
    for (var i = 0; i < obtns.length; i++) obtns[i].setAttribute('aria-pressed', String(obtns[i].getAttribute('data-order') === v));
  }
  applyOrder(get(KEY_O) === 'zh' ? 'zh' : 'orig');
  for (var i = 0; i < obtns.length; i++) obtns[i].addEventListener('click', function(){
    var v = this.getAttribute('data-order'); applyOrder(v); set(KEY_O, v);
  });

  /* 点「最值得看」或概览里的链接时，目标若在折叠块里，先把它展开 */
  document.addEventListener('click', function(e){
    var a = e.target.closest ? e.target.closest('a[href^="#"]') : null;
    if (!a) return;
    var el = document.getElementById(a.getAttribute('href').slice(1));
    while (el) {
      if (el.tagName === 'DETAILS') el.open = true;
      el = el.parentElement ? el.parentElement.closest('details') : null;
    }
  });

  document.addEventListener('click', function(e){
    var b = e.target.closest ? e.target.closest('.expand') : null;
    if (!b) return;
    var more = b.parentNode.querySelector('.more');
    var open = b.getAttribute('aria-expanded') === 'true';
    more.hidden = open;
    b.setAttribute('aria-expanded', String(!open));
    b.textContent = open ? b.getAttribute('data-label') : '收起';
  });

  /* 往期切换：页面里带着最近若干天，最新的排在最前 */
  var days = Array.prototype.slice.call(document.querySelectorAll('.day'));
  var title = document.getElementById('day-title');
  var prev = document.getElementById('prev-day'), next = document.getElementById('next-day');
  var idx = 0;
  function show(i, scroll){
    idx = i;
    for (var k = 0; k < days.length; k++) days[k].hidden = (k !== i);
    title.textContent = days[i].getAttribute('data-label');
    prev.disabled = (i >= days.length - 1);
    next.disabled = (i <= 0);
    if (scroll) window.scrollTo(0, 0);
  }
  if (days.length < 2) { prev.hidden = true; next.hidden = true; }
  prev.addEventListener('click', function(){ if (idx < days.length - 1) show(idx + 1, true); });
  next.addEventListener('click', function(){ if (idx > 0) show(idx - 1, true); });
  show(0, false);
})();
"""

EXTRA_CSS = r"""
/* 两套样式：默认「暖纸」，页头可切到「暖金」（coaching-html-style 模板的配色）。差别都收在下面这些变量里。 */
:root{
  --shadow:none; --radius-card:8px; --link-line:var(--hairline-2); --accent-line:var(--accent);
  --chip-bg:var(--paper); --chip-fg:var(--muted); --chip-line:var(--hairline-2); --lh:1.85;
}
:root[data-style="gold"]{
  --bg:#FBF9F4; --paper:#FFFFFF; --paper-2:#F0E9DC;
  --ink:#1c1814; --ink-soft:#2e2620; --muted:#7a6e5a;
  --hairline:#e8dfc9; --hairline-2:#d9cdb6;
  --accent:#7d6f3a; --accent-bg:rgba(181,166,99,.14); --accent-line:#B5A663;
  --shadow:0 1px 2px rgba(28,24,20,.04),0 8px 24px -8px rgba(28,24,20,.12);
  --radius-card:14px; --link-line:#D4B896;
  --chip-bg:rgba(181,166,99,.14); --chip-fg:#7d6f3a; --chip-line:transparent;
  --fs-h1:22px; --fs-h2:19px; --fs-h3:16px; --lh:1.7;
}
@media (min-width:768px){ :root[data-style="gold"]{ --fs-h1:28px; --fs-h2:24px; --fs-h3:17px; } }
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"])[data-style="gold"]{
    --bg:#1A1612; --paper:#221C16; --paper-2:#1F1A14;
    --ink:#f0e6d2; --ink-soft:#d6c9b0; --muted:#98917f;
    --hairline:#2d2620; --hairline-2:#3a322a;
    --accent:#C9BA77; --accent-bg:rgba(201,186,119,.14); --accent-line:#C9BA77;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px -8px rgba(0,0,0,.5);
    --link-line:#8a7d4a; --chip-bg:rgba(201,186,119,.14); --chip-fg:#C9BA77;
  }
}
:root[data-theme="dark"][data-style="gold"]{
  --bg:#1A1612; --paper:#221C16; --paper-2:#1F1A14;
  --ink:#f0e6d2; --ink-soft:#d6c9b0; --muted:#98917f;
  --hairline:#2d2620; --hairline-2:#3a322a;
  --accent:#C9BA77; --accent-bg:rgba(201,186,119,.14); --accent-line:#C9BA77;
  --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px -8px rgba(0,0,0,.5);
  --link-line:#8a7d4a; --chip-bg:rgba(201,186,119,.14); --chip-fg:#C9BA77;
}
body{ line-height:var(--lh); }
a{ border-bottom-color:var(--link-line); }
.ov-rest a,.qnote a,.orig a{ border-bottom-color:var(--hairline-2); }
.overview,.card{ border-radius:var(--radius-card); box-shadow:var(--shadow); }
.ov-n,.kind{ color:var(--chip-fg); border-color:var(--chip-line); background:var(--chip-bg); }
.passage blockquote.orig{ border-left-color:var(--accent-line); }
.switch button[aria-pressed="true"]{ border-color:var(--accent-line); }
:root[data-style="gold"] .group-head h2{ padding-bottom:0; border-bottom:0; }
:root[data-style="gold"] .eyebrow{ display:inline-flex; align-items:center; gap:10px; letter-spacing:.14em; }
:root[data-style="gold"] .eyebrow::before{ content:""; width:24px; height:1px; background:var(--accent-line); }
.hdr-controls{ display:flex; align-items:center; gap:8px; }
.stylesw{ display:flex; border:1px solid var(--hairline-2); border-radius:6px; overflow:hidden; background:var(--paper); }
.stylesw button{ background:transparent; border:0; padding:7px 10px; min-height:36px; cursor:pointer; font:400 13px var(--font); color:var(--muted); }
.stylesw button[aria-pressed="true"]{ background:var(--accent-bg); color:var(--ink); font-weight:600; }
.daynav{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.daynav h1{ flex:1; min-width:0; }
.daynav button{
  background:var(--paper); border:1px solid var(--hairline-2); color:var(--ink-soft);
  padding:6px 12px; border-radius:99px; cursor:pointer; font:400 13px var(--font); min-height:36px; white-space:nowrap;
}
.daynav button:disabled{ opacity:.4; cursor:default; }
.day{ display:flex; flex-direction:column; gap:28px; }
@media (min-width:768px){ .day{ gap:36px; } }
.day > .counts{ color:var(--muted); font-size:var(--fs-small); margin-top:-22px; }
@media (min-width:768px){ .day > .counts{ margin-top:-30px; } }
details.video{ scroll-margin-top:72px; }
.v-one{ font-size:var(--fs-small); color:var(--ink-soft); line-height:1.6; padding-top:2px; }
.video-body{ padding:14px 14px 16px; display:flex; flex-direction:column; align-items:flex-start; gap:10px; border-top:1px solid var(--hairline); }
.segs{ list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:6px; align-self:stretch; }
.segs li{ display:flex; gap:12px; align-items:baseline; font-size:14px; color:var(--ink-soft); }
.segs a{ flex:none; font-variant-numeric:tabular-nums; font-weight:600; font-size:13px; white-space:nowrap; }
.segs span{ flex:1; min-width:0; }
"""

FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=Noto+Sans+SC:wght@400;500;600;700&display=swap">')


def render_page(days):
    """days：最新的排在最前。返回 (完整网页, 发布用的片段)。"""
    body = ['<header class="site-header"><div class="site-header-inner"><span class="logo">AI 早报</span><div class="hdr-controls">'
            '<div class="stylesw" role="group" aria-label="页面样式">'
            '<button type="button" id="style-paper" data-style="paper" aria-pressed="true">暖纸</button>'
            '<button type="button" id="style-gold" data-style="gold" aria-pressed="false">暖金</button></div>'
            '<button class="theme-toggle" type="button" aria-label="切换浅色或深色">☾ 深色</button></div></div></header>',
            '<main class="shell">',
            '<div class="daynav"><h1 id="day-title">%s</h1>'
            '<button type="button" id="prev-day">‹ 前一天</button>'
            '<button type="button" id="next-day">后一天 ›</button></div>' % esc(day_label(days[0].date))]
    for i, d in enumerate(days):
        body.append(render_day(d, hidden=(i > 0)))
    body.append("</main>")
    body = "".join(body)
    head = "<title>AI 早报</title>\n%s\n<style>%s%s</style>\n" % (FONTS, CSS, EXTRA_CSS)
    script = "<script>%s</script>\n" % JS
    full = ('<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="UTF-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">\n'
            + head + "</head>\n<body>\n" + body + "\n" + script + "</body>\n</html>\n")
    return full, head + body + "\n" + script


# ---------- Markdown 存档 ----------

def md_quote(text):
    return "\n".join("> " + ln if ln.strip() else ">" for ln in text.split("\n"))


def render_markdown(day, page_url):
    edit, roles, f = day.edit, day.edit.get("roles", {}), day_facts(day)
    L = ["# AI 早报 — %s" % day.date, "", "> %s" % f["counts"]]
    if page_url:
        L.append("> 网页版：%s" % page_url)
    picks = [(pk, resolve_pick(day, pk.get("ref"))) for pk in (edit.get("picks") or [])]
    picks = [(pk, r) for pk, r in picks if r]
    if picks:
        L += ["", "## 今天最值得看", ""]
        for i, (pk, r) in enumerate(picks, 1):
            L.append("%d. 【%s】**%s**（%s）：%s" % (i, r["kind"], r["title"], r["source"], pk.get("why", "")))
    L += ["", "## 今日概览", ""]
    for tp in f["topics"]:
        L.append("- **%s**（%s，%d 条）" % (tp["title"], tp["names"], tp["n"]))
    for text, n, _ in f["rest"]:
        L.append("- " + text % n)
    if f["video_note"]:
        L.append("- " + f["video_note"])

    def voice(it):
        b, t = day.by_id[str(it["id"])]
        role = roles.get(b.get("handle"), "")
        out = ["### %s%s" % (b["name"], ("（%s）" % role) if role else ""), "",
               "摘要：%s" % it.get("summary", ""), "", md_quote(t["text"]), "",
               "[查看原帖](%s) · %s · 赞 %s · 转发 %s" % (t["url"], fmt_time(t["createdAt"], day.tz),
                                                    fmt_count(t.get("likes")), fmt_count(t.get("retweets"))), ""]
        return out

    for tp in f["topics"]:
        t = tp["tp"]
        L += ["", "## %s" % t["title"], "", "%s。%s" % (t["kind"], t.get("note", "")), ""]
        for it in t["tweets"]:
            L += voice(it)
    if edit.get("singles"):
        L += ["", "## 其他帖子", ""]
        for it in edit["singles"]:
            L += voice(it)
    if day.videos:
        L += ["", "## 视频", ""]
        for v in f["with_sum"]:
            ve = f["video_edit"][str(v.get("videoId"))]
            L += ["### %s：%s" % (v.get("channelName", ""), v.get("title", "")), "", ve.get("one_line", ""), ""]
            L += ["- " + p for p in ve.get("points", [])]
            if ve.get("segments"):
                L += ["", "值得看的片段："]
                for s in ve["segments"]:
                    span = str(s["start"]) + ((" – " + str(s["end"])) if s.get("end") else "")
                    L.append("- [%s](%s) %s" % (span, video_link(v, s["start"]), s.get("topic", "")))
            L += ["", "[在 YouTube 上观看](%s)" % v.get("url", ""), ""]
        if f["without"]:
            L += ["没有摘要的视频（没有字幕、很短，或与主题无关）：", ""]
            L += ["- [%s](%s)（%s）" % (v.get("title", ""), v.get("url", ""), v.get("channelName", "")) for v in f["without"]]
    if day.posts:
        blog_edit = dict((b.get("url"), b) for b in edit.get("blogs", []))
        L += ["", "## 博客", ""]
        for p in sorted(day.posts, key=lambda p: bool(day.seen[p["url"]])):
            dates, b = day.seen[p["url"]], blog_edit.get(p["url"])
            L += ["### %s" % p.get("title", ""), "", html.unescape(blog_meta(p, dates)), ""]
            if b:
                paras = paragraphs(p.get("content"))
                L += [b.get("one_line", ""), ""] + ["- " + x for x in b.get("points", [])] + [""]
                for ps in b.get("passages", []):
                    L += [md_quote(paras[ps["para"]]), "", "摘要：%s" % ps.get("summary", ""), ""]
            L += ["[阅读全文](%s)" % p["url"], ""]
    if edit.get("folded"):
        L += ["", "## 已折叠的帖子", ""]
        for it in edit["folded"]:
            b, t = day.by_id[str(it["id"])]
            L += ["- %s（%s）：[查看原帖](%s)" % (b["name"], it.get("reason", ""), t["url"])]
    L += ["", "---", "", "英文原文由程序原样引用，未作改动。话题分组、摘要和要点由 AI 生成；摘要是转述，不是逐句翻译。", ""]
    return "\n".join(L)


# ---------- 邮件正文 ----------

def render_email(day, page_url):
    f = day_facts(day)
    picks = [(pk, resolve_pick(day, pk.get("ref"))) for pk in (day.edit.get("picks") or [])]
    picks = [(pk, r) for pk, r in picks if r]
    pick_rows = []
    for i, (pk, r) in enumerate(picks):
        border = "" if i == 0 else "border-top:1px solid #E3DCCD;"
        pick_rows.append('<div style="padding:11px 0;%s"><div style="font-size:12px;color:#46656F;font-weight:600">%s · %s</div>'
                         '<div style="font-weight:600;color:#2B2A28">%s</div>'
                         '<div style="font-size:13px;color:#45443F">%s</div></div>'
                         % (border, r["kind"], esc(r["source"]), esc(r["title"]), esc(pk.get("why", ""))))
    picks_html = ('<div style="font-size:12.5px;letter-spacing:.1em;color:#46656F;font-weight:600;margin-top:18px">今天最值得看</div>'
                  '<div style="background:#FFFFFF;border:1px solid #E3DCCD;border-radius:8px;padding:4px 16px;margin:8px 0 6px">%s</div>'
                  % "".join(pick_rows)) if pick_rows else ""
    rows = []
    for i, tp in enumerate(f["topics"]):
        border = "" if i == 0 else "border-top:1px solid #E3DCCD;"
        rows.append('<div style="padding:11px 0;%s"><div style="font-weight:600;color:#2B2A28">%s</div>'
                    '<div style="font-size:13px;color:#7E7A70">%s · %d 条</div></div>'
                    % (border, esc(tp["title"]), esc(tp["names"]), tp["n"]))
    rest = "".join(esc(text % n) for text, n, _ in f["rest"]) + esc(f["video_note"])
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
            '<body style="margin:0;background:#F5F1E8">'
            '<div style="max-width:600px;margin:0 auto;padding:28px 20px 36px;background:#F5F1E8;color:#2B2A28;'
            "font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;font-size:15px;line-height:1.8\">"
            '<div style="font-size:12.5px;letter-spacing:.1em;color:#46656F;font-weight:600">AI 早报</div>'
            '<h1 style="font-size:20px;line-height:1.4;margin:6px 0 4px;color:#2B2A28">%s</h1>'
            '<div style="font-size:13px;color:#7E7A70">%s</div>'
            '%s'
            '<div style="font-size:12.5px;letter-spacing:.1em;color:#46656F;font-weight:600;margin-top:18px">今日概览</div>'
            '<div style="background:#FFFFFF;border:1px solid #E3DCCD;border-radius:8px;padding:4px 16px;margin:8px 0 14px">%s</div>'
            '<p style="font-size:13px;color:#7E7A70;margin:0 0 22px">%s</p>'
            '<a href="%s" style="display:inline-block;background:#46656F;color:#FFFFFF;text-decoration:none;'
            'padding:12px 24px;border-radius:8px;font-weight:600;font-size:15px">打开今天的早报</a>'
            '<p style="font-size:12px;color:#7E7A70;margin:22px 0 0">这是你的私密页面，要在浏览器里登录 Claude 才能打开。'
            '完整内容也已存进 Obsidian。</p>'
            '</div></body></html>') % (esc(day_label(day.date)), esc(f["counts"]), picks_html, "".join(rows) or
                                       '<div style="padding:11px 0;color:#7E7A70">今天没有成组的话题。</div>',
                                       rest, esc(page_url))


# ---------- 子命令 ----------

def today_str(cfg):
    return datetime.now(get_tz(cfg)).strftime("%Y-%m-%d")


def merge_data(old, new):
    """同一天再次抓取时合并：帖子按编号、博客按网址、视频按编号去重，已有的保留。"""
    builders, order = {}, []
    for src in (old, new):
        for b in src.get("x", {}).get("builders", []):
            h = b.get("handle")
            if h not in builders:
                builders[h] = dict(b, tweets=[])
                order.append(h)
            have = set(str(t["id"]) for t in builders[h]["tweets"])
            builders[h]["tweets"] += [t for t in b.get("tweets", []) if str(t["id"]) not in have]
    posts, seen_urls = [], set()
    for src in (old, new):
        for p in src.get("blogs", {}).get("posts", []):
            if p["url"] not in seen_urls:
                seen_urls.add(p["url"])
                posts.append(p)
    videos, pos = [], {}
    for src in (old, new):
        for v in src.get("youtube", {}).get("videos", []):
            vid = v.get("videoId")
            if vid not in pos:
                pos[vid] = len(videos)
                videos.append(v)
            elif v.get("transcript") and not videos[pos[vid]].get("transcript"):
                videos[pos[vid]] = v  # 之前没拿到字幕，这次拿到了，用新的
    errors = new.get("errors", [])
    if videos:  # 之前抓到过视频，就不再报「没抓到」
        errors = [x for x in errors if not x.lower().startswith("youtube")]
    return {"generatedAt": new.get("generatedAt") or old.get("generatedAt"),
            "x": {"builders": [builders[h] for h in order]}, "blogs": {"posts": posts},
            "youtube": {"videos": videos}, "errors": errors}


def cmd_ingest(a, cfg):
    raw = json.loads(Path(a.raw).read_text(encoding="utf-8"))
    date = a.date or today_str(cfg)
    ddir = page_dir(cfg) / "days" / date
    ddir.mkdir(parents=True, exist_ok=True)
    new = {"generatedAt": raw.get("generatedAt"),
           "x": {"builders": raw.get("x", {}).get("builders", [])},
           "blogs": {"posts": raw.get("blogs", {}).get("posts", [])},
           "youtube": {"videos": raw.get("youtube", {}).get("videos", [])},
           "errors": [x for x in raw.get("errors", []) if not x.startswith("Prompt missing")]}
    path = ddir / "data.json"
    if path.exists():
        new = merge_data(json.loads(path.read_text(encoding="utf-8")), new)
    path.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    n = sum(len(b.get("tweets", [])) for b in new["x"]["builders"])
    print(json.dumps({"date": date, "帖子": n, "博客": len(new["blogs"]["posts"]),
                      "视频": len(new["youtube"]["videos"]), "data": str(path),
                      "edit": str(ddir / "edit.json")}, ensure_ascii=False))


def load_day(cfg, date, need_edit=True):
    ddir = page_dir(cfg) / "days" / date
    dpath, epath = ddir / "data.json", ddir / "edit.json"
    if not dpath.exists():
        return None
    data = json.loads(dpath.read_text(encoding="utf-8"))
    edit = json.loads(epath.read_text(encoding="utf-8")) if epath.exists() else None
    if need_edit and edit is None:
        return None
    archive = (cfg.get("delivery") or {}).get("obsidianPath")
    day = Day(date, data, edit, archive, get_tz(cfg))
    bpath = ddir / "edit.运行前.json"
    if bpath.exists():
        try:
            day.before = json.loads(bpath.read_text(encoding="utf-8"))
        except Exception:
            day.before = None
    return day


def cmd_view(a, cfg):
    date = a.date or today_str(cfg)
    day = load_day(cfg, date, need_edit=False)
    if day is None:
        sys.exit("%s 还没有 data.json，先运行 ingest。" % date)
    # 给已有的编排留一份快照：build 时用它核对已有条目没有被改动
    epath = page_dir(cfg) / "days" / date / "edit.json"
    if epath.exists():
        (page_dir(cfg) / "days" / date / "edit.运行前.json").write_text(epath.read_text(encoding="utf-8"), encoding="utf-8")
    placed = set()
    for tp in day.edit.get("topics", []):
        placed |= set(str(it.get("id")) for it in tp.get("tweets", []))
    placed |= set(str(it.get("id")) for it in day.edit.get("singles", []) + day.edit.get("folded", []))
    known = {}  # 以前各天用过的身份，旧的先读，新的覆盖
    for d in sorted(p.name for p in (page_dir(cfg) / "days").iterdir() if p.is_dir()):
        ep = page_dir(cfg) / "days" / d / "edit.json"
        if d <= date and ep.exists():
            try:
                known.update(json.loads(ep.read_text(encoding="utf-8")).get("roles", {}))
            except Exception:
                pass
    L = ["=== 帖子（共 %d 条%s）===" % (len(day.by_id), "；标 * 的还没写进 edit.json" if day.edit else ""), ""]
    for b in day.data.get("x", {}).get("builders", []):
        for t in b.get("tweets", []):
            tid = str(t["id"])
            q = ""
            if t.get("isQuote"):
                qid = str(t.get("quotedTweetId") or "")
                q = "｜引用了 %s（%s）" % (qid, "在今天的数据里" if qid in day.by_id else "不在今天的数据里，看不到内容")
            L.append("%s[%s] %s（@%s）｜%s｜赞 %s 转发 %s%s" % (
                "" if (not day.edit or tid in placed) else "* ", tid, b.get("name"), b.get("handle"),
                fmt_time(t["createdAt"], day.tz), fmt_count(t.get("likes")), fmt_count(t.get("retweets")), q))
            L.append("    简介：%s" % (b.get("bio") or "").replace("\n", " ")[:160])
            if b.get("handle") in known:
                L.append("    以前用的身份（照抄进 roles）：%s" % known[b.get("handle")])
            L += [t["text"], "", "-----", ""]
    L += ["", "=== 博客（共 %d 篇）===" % len(day.posts), ""]
    for p in day.posts:
        dates = day.seen[p["url"]]
        if dates:
            L += ["[%s] %s｜此前已收录 %d 次。不用写，脚本会自动折叠。" % (p["url"], p.get("title"), len(dates)), ""]
            continue
        L.append("[%s] %s｜%s｜%s｜新文章，要写" % (p["url"], p.get("title"), p.get("blog"), p.get("publishedAt")))
        for i, para in enumerate(paragraphs(p.get("content"))):
            L.append("  [%d]（%d 字符）%s" % (i, len(para), para))
        L.append("")
    L += ["", "=== 视频（共 %d 个）===" % len(day.videos), ""]
    for v in day.videos:
        if not day.has_transcript(v):
            L += ["[%s] %s｜%s｜没有字幕或很短（%s）。不用写，脚本会自动列出。" % (
                v.get("videoId"), v.get("channelName"), v.get("title"), v.get("transcriptError") or "字幕太短"), ""]
            continue
        L.append("[%s] %s｜%s｜%s｜%s｜有字幕，要写" % (
            v.get("videoId"), v.get("channelName"), v.get("category"), v.get("title"), v.get("url")))
        L += [v.get("transcript") or "", "", "-----", ""]
    out = page_dir(cfg) / "days" / date / "素材.txt"
    out.write_text("\n".join(L), encoding="utf-8")
    print(json.dumps({"date": date, "素材": str(out), "行数": len(L), "字符数": sum(len(x) for x in L)}, ensure_ascii=False))


def cmd_build(a, cfg):
    date = a.date or today_str(cfg)
    pdir = page_dir(cfg)
    page_url = (cfg.get("page") or {}).get("url", "")
    keep = int((cfg.get("page") or {}).get("keepDays", 14))
    today = load_day(cfg, date, need_edit=False)
    if today is None:
        sys.exit("%s 还没有 data.json，先运行 ingest。" % date)
    problems = check(today, guard=True)
    if problems:
        print("edit.json 有 %d 处问题，网页没有生成。改完再运行一次：" % len(problems))
        for x in problems:
            print("- " + x)
        sys.exit(1)

    days, left_out = [today], []
    for d in sorted((p.name for p in (pdir / "days").iterdir() if p.is_dir()), reverse=True):
        if d >= date or len(days) >= keep or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            continue
        old = load_day(cfg, d)
        if old is not None and not check(old):
            days.append(old)
        else:
            left_out.append(d)

    full, fragment = render_page(days)
    (pdir / "AI早报.html").write_text(full, encoding="utf-8")
    (pdir / "发布用.html").write_text(fragment, encoding="utf-8")
    ddir = pdir / "days" / date
    (ddir / "digest.md").write_text(render_markdown(today, page_url), encoding="utf-8")
    (ddir / "email.html").write_text(render_email(today, page_url), encoding="utf-8")
    print(json.dumps({"状态": "已生成", "日期": date, "页面里的天数": len(days),
                      "发布用": str(pdir / "发布用.html"), "本地网页": str(pdir / "AI早报.html"),
                      "存档": str(ddir / "digest.md"), "邮件正文": str(ddir / "email.html"),
                      "网址": page_url,
                      "没放进页面的往期": left_out or "无"}, ensure_ascii=False, indent=1))


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def cmd_fetch(a, cfg):
    """在后台启动抓取，立刻返回。抓取可能超过 10 分钟，所以不在这条命令里等。"""
    if FETCH_STATE.exists():
        try:
            st = json.loads(FETCH_STATE.read_text(encoding="utf-8"))
            if pid_alive(st.get("pid")):
                print("抓取已经在进行中（%d 秒前开始）。接着运行 fetch-wait。" % (time.time() - st.get("started", time.time())))
                return
        except Exception:
            pass
    out = open(str(RAW_TMP), "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(SCRIPT_DIR / "prepare_digest.py"), "--lookback-hours", "48"],
                            cwd=str(SCRIPT_DIR), stdout=out, stderr=subprocess.DEVNULL, start_new_session=True)
    FETCH_STATE.write_text(json.dumps({"pid": proc.pid, "started": time.time(), "date": today_str(cfg)}), encoding="utf-8")
    print("抓取已开始。接着运行 fetch-wait，等它抓完。")


def cmd_fetch_wait(a, cfg):
    """等抓取结束；结束后存成当天的数据。一次最多等 9 分钟，没抓完就再运行一次。"""
    if not FETCH_STATE.exists():
        sys.exit("还没有开始抓取，先运行 fetch。")
    st = json.loads(FETCH_STATE.read_text(encoding="utf-8"))
    t0 = time.time()
    while pid_alive(st.get("pid")) and time.time() - t0 < WAIT_SECONDS:
        time.sleep(5)
    if pid_alive(st.get("pid")):
        print("还在抓取，已经抓了 %d 分钟。再运行一次 fetch-wait。" % ((time.time() - st.get("started", t0)) / 60))
        return
    try:
        raw = json.loads(RAW_TMP.read_text(encoding="utf-8"))
    except Exception as e:
        print("抓取失败：输出不是合法的 JSON（%s）。" % e)
        sys.exit(1)
    RAW_PATH.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    try:
        FETCH_STATE.unlink()
    except OSError:
        pass
    print("抓取完成，用时 %d 分钟。" % ((time.time() - st.get("started", t0)) / 60))
    a.raw, a.date = str(RAW_PATH), st.get("date")
    cmd_ingest(a, cfg)


def run_deliver(args):
    r = subprocess.run([sys.executable, str(SCRIPT_DIR / "deliver.py")] + args, cwd=str(SCRIPT_DIR))
    return r.returncode


def cmd_deliver(a, cfg):
    """把当天的 Markdown 存进 Obsidian，并发邮件。"""
    date = a.date or today_str(cfg)
    ddir = page_dir(cfg) / "days" / date
    md, email = ddir / "digest.md", ddir / "email.html"
    if not md.exists():
        sys.exit("%s 还没有生成存档，先运行 build。" % date)
    if a.page_failed:
        # 网页没更新成：邮件里直接带完整内容
        args = ["--file", str(md), "--subject", "AI 早报 — %s（网页没更新成）" % date]
    else:
        args = ["--file", str(md), "--email-html", str(email), "--subject", "AI 早报 — %s" % date]
    sys.exit(run_deliver(args))


def cmd_fail(a, cfg):
    """早报没生成成功时，把「失败说明.txt」里的文字发一封邮件，不存档。"""
    date = a.date or today_str(cfg)
    note = page_dir(cfg) / "days" / date / "失败说明.txt"
    text = note.read_text(encoding="utf-8").strip() if note.exists() else "没有写失败说明。"
    sys.exit(run_deliver(["--email-only", "--subject", "AI 早报今天没有生成（%s）" % date, "--message", text]))


def main():
    ap = argparse.ArgumentParser(description="AI 早报网页")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("fetch")
    sub.add_parser("fetch-wait")
    p = sub.add_parser("deliver")
    p.add_argument("--date")
    p.add_argument("--page-failed", action="store_true", help="网页没发布成功时用：邮件里直接带完整内容")
    p = sub.add_parser("fail")
    p.add_argument("--date")
    p = sub.add_parser("ingest")
    p.add_argument("--raw", required=True, help="prepare_digest.py 的输出文件")
    p.add_argument("--date")
    p = sub.add_parser("view")
    p.add_argument("--date")
    p = sub.add_parser("build")
    p.add_argument("--date")
    a = ap.parse_args()
    cfg = load_config()
    if a.cmd == "fetch":
        cmd_fetch(a, cfg)
    elif a.cmd == "fetch-wait":
        cmd_fetch_wait(a, cfg)
    elif a.cmd == "deliver":
        cmd_deliver(a, cfg)
    elif a.cmd == "fail":
        cmd_fail(a, cfg)
    elif a.cmd == "ingest":
        cmd_ingest(a, cfg)
    elif a.cmd == "view":
        cmd_view(a, cfg)
    elif a.cmd == "build":
        cmd_build(a, cfg)
    else:
        ap.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
