# 外部检查：到 10:30 还没发出早报就提醒

装在腾讯云服务器 `ubuntu@43.163.99.36` 的 `~/digest-health/`，由 crontab 每 30 分钟运行一次：

    */30 * * * * /usr/bin/python3 /home/ubuntu/digest-health/check.py >/dev/null 2>&1

- Mac 上的早报发出邮件后，`build_page.py deliver` 用 ssh 把 `last_ok.json` 写到服务器（「报到」），ssh 设置在 `~/.follow-builders/config.json` 的 `health` 里。
- 爱尔兰时间 10:30 以后，如果今天还没有报到，服务器用 Resend 发一封提醒，一天最多一封。发信账号在服务器的 `~/digest-health/.env`（权限 600）。
- 状态写在 https://weiweixuan.me/digest-health/status.json（不含密钥）。
- 演练：`python3 check.py --dry-run --now 2026-10-18T11:00`。

这里的 `check.py` 是服务器上那份的副本，改了要重新传上去。
