#!/usr/bin/env python3
"""AI 早报的外部检查（装在腾讯云服务器上，每 30 分钟运行一次）。

Mac 上的早报发出邮件后，会把一个小文件 last_ok.json 传到本目录（「报到」）。
每天爱尔兰时间 10:30 以后，如果今天还没有报到，就用 Resend 给 Weiwei 发一封提醒，一天最多一封。
这样即使 Mac 睡着了、Claude 没开、早报运行卡住（这几种情况下 Mac 那边什么都发不出来），她当天也能知道。

文件（都在 ~/digest-health/）：
  last_ok.json  Mac 报到时写入：{"date": "2026-10-18", "at": "...", "note": "..."}
  state.json    本程序自己的记录：哪天已经提醒过
  .env          RESEND_API_KEY=...、ALERT_TO=...（只有本用户能读）
  check.log     每次运行的一行记录
另外把状态写到 ~/web/public/digest-health/status.json（不含任何密钥），给进度看板看。
用法：python3 check.py            正常运行
      python3 check.py --dry-run  只打印会做什么，不发邮件、不改记录
      python3 check.py --dry-run --now 2026-10-18T11:00  假装现在是这个时间
"""
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home() / "digest-health"
STATUS = Path.home() / "web" / "public" / "digest-health" / "status.json"
TZ = ZoneInfo("Europe/Dublin")
DEADLINE = (10, 30)


def read_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def env():
    out = {}
    try:
        for line in (HOME / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    except Exception:
        pass
    return out


def send(subject, text):
    e = env()
    if not e.get("RESEND_API_KEY") or not e.get("ALERT_TO"):
        return "没有配置发信账号"
    body = json.dumps({"from": "AI Digest <digest@resend.dev>", "to": [e["ALERT_TO"]],
                       "subject": subject, "text": text}).encode("utf-8")
    req = urllib.request.Request("https://api.resend.com/emails", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + e["RESEND_API_KEY"]})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return "已发出" if 200 <= r.status < 300 else "发送失败：HTTP %d" % r.status
    except Exception as ex:
        return "发送失败：%s" % ex


def main():
    dry = "--dry-run" in sys.argv
    now = datetime.now(TZ)
    if "--now" in sys.argv:
        now = datetime.fromisoformat(sys.argv[sys.argv.index("--now") + 1]).replace(tzinfo=TZ)
    today = now.strftime("%Y-%m-%d")
    last = read_json(HOME / "last_ok.json", None)
    state = read_json(HOME / "state.json", {})
    past_deadline = (now.hour, now.minute) >= DEADLINE
    reported_today = bool(last) and last.get("date") == today

    action = "无事"
    if last is None:
        action = "还没收到过报到，不提醒"  # 刚装好、Mac 那边还没跑过新版本时
    elif past_deadline and not reported_today and state.get("alerted_for") != today:
        md = now.strftime("%-m 月 %-d 日")
        text = ("服务器今天（%s）到 10:30 还没收到早报发出的消息。\n\n"
                "可能是 Mac 睡着了或关机了、Claude 桌面 app 没开，或者早报运行卡住了。\n"
                "最近一次正常发出：%s。\n\n"
                "可以在 Mac 上打开 Claude 的 Routines 页面，看「Ai daily digest」的运行记录；"
                "也可以让 Claude 查一下 ~/.follow-builders/logs/ 里当天的记录。"
                % (md, last.get("at") or last.get("date")))
        if dry:
            action = "会发提醒（演练，没有发）"
        else:
            action = "发提醒：" + send("AI 早报今天到 10:30 还没发出来（%s）" % md, text)
            state["alerted_for"] = today
    status = {"last_check": now.strftime("%Y-%m-%d %H:%M"), "last_ok": (last or {}).get("at") or (last or {}).get("date"),
              "reported_today": reported_today, "alerted_for": state.get("alerted_for"), "action": action}
    line = "%s｜今天报到：%s｜%s" % (status["last_check"], "是" if reported_today else "否", action)
    if dry:
        print(line)
        return
    (HOME / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    with open(HOME / "check.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


if __name__ == "__main__":
    main()
