"""网页脚本：生成、往期文件损坏、重跑快照、帖子跨天去重、字幕总量、邮件失败重试、已收过的并入、失败通知、运行日期。
用真实数据的副本，放在临时目录里，不碰正式的数据目录、库和配置。"""
import json
import os
import shutil
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import helpers
import build_page as bp

REAL = Path("/Users/weiwei/Documents/Obsidian/AI-Digest/_meta/早报网页/days")
DAYS = ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]


class BuildPageTest(unittest.TestCase):
    def setUp(self):
        self.d = helpers.tmpdir()
        for day in DAYS:
            for f in ("data.json", "edit.json"):
                src = REAL / day / f
                if src.exists():
                    (self.d / "days" / day).mkdir(parents=True, exist_ok=True)
                    shutil.copy(src, self.d / "days" / day / f)
        self.cfg = {"timezone": "Europe/Dublin", "page": {"dir": str(self.d), "url": "https://example.test/p", "keepDays": 14},
                    "delivery": {"obsidianPath": str(self.d / "vault"), "email": "me@example.test"}}
        self.saved = {k: getattr(bp, k) for k in ("load_config", "RUN_DATE", "USER_DIR", "FETCH_STATE", "WAIT_SECONDS",
                                                  "run_deliver", "send_mail_stdlib", "report_to_server")}
        bp.load_config = lambda: self.cfg
        bp.RUN_DATE = self.d / "run.json"
        bp.USER_DIR = self.d / "user"
        bp.USER_DIR.mkdir()
        bp.FETCH_STATE = self.d / "fetch.json"
        bp.report_to_server = lambda cfg, date, note: "测试里不报到"
        self.sleep = bp.time.sleep
        bp.time.sleep = lambda s: None

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(bp, k, v)
        bp.time.sleep = self.sleep

    def build(self, date):
        return helpers.run_main(bp.main, ["build_page.py", "build", "--date", date])

    def test_build_with_picks_health_and_archive(self):
        code, out = self.build("2026-10-16")
        self.assertEqual(code, 0, out)
        res = json.loads(out)
        self.assertTrue(Path(res["存档"]).exists(), "存档应在生成时就写好")
        page = (self.d / "AI早报.html").read_text(encoding="utf-8")
        mail = (self.d / "days" / "2026-10-16" / "email.html").read_text(encoding="utf-8")
        self.assertIn("今天最值得看", page)
        self.assertIn("今天的抓取情况", page)
        self.assertIn("今天的抓取情况", mail)
        # 同一次运行再生成一次，覆盖同一个存档文件，不多出 -2
        bp.RUN_DATE.write_text(json.dumps({"date": "2026-10-16", "at": time.time(), "archived": res["存档"]}))
        code, out2 = self.build("2026-10-16")
        self.assertEqual(json.loads(out2)["存档"], res["存档"])

    def test_corrupt_past_day_does_not_break_page(self):
        (self.d / "days" / "2026-10-15" / "edit.json").write_text("{ 坏了", encoding="utf-8")
        code, out = self.build("2026-10-16")
        self.assertEqual(code, 0, out)
        left = json.loads(out)["没放进页面的往期"]
        self.assertTrue(any("2026-10-15" in x for x in left))

    def test_rerun_snapshot_drops_bad_entries(self):
        e = json.loads((self.d / "days" / "2026-10-16" / "edit.json").read_text(encoding="utf-8"))
        e["singles"][0]["summary"] = "他说这件事“很重要”——值得一看。"
        (self.d / "days" / "2026-10-16" / "edit.json").write_text(json.dumps(e, ensure_ascii=False), encoding="utf-8")
        helpers.run_main(bp.main, ["build_page.py", "view", "--date", "2026-10-16"])
        snap = json.loads((self.d / "days" / "2026-10-16" / "edit.运行前.json").read_text(encoding="utf-8"))
        bad_id = e["singles"][0]["id"]
        self.assertNotIn(bad_id, [it["id"] for it in snap["singles"]])
        self.assertEqual(len(snap["singles"]), len(e["singles"]) - 1)

    def test_segment_minutes_over_99(self):
        self.assertTrue(bp.re.fullmatch(r"(\d{1,2}:)?\d{1,3}:\d{2}", "104:46"))

    def test_ingest_drops_tweets_seen_in_past_days(self):
        old = json.loads((self.d / "days" / "2026-10-16" / "data.json").read_text(encoding="utf-8"))
        b = dict(old["x"]["builders"][0])
        b["tweets"] = b["tweets"][:1] + [dict(b["tweets"][0], id="999000111222333")]
        raw = {"x": {"builders": [b]}, "blogs": {"posts": []}, "youtube": {"videos": []}, "errors": []}
        rp = self.d / "raw.json"
        rp.write_text(json.dumps(raw), encoding="utf-8")
        code, out = helpers.run_main(bp.main, ["build_page.py", "ingest", "--raw", str(rp), "--date", "2026-10-17"])
        data = json.loads((self.d / "days" / "2026-10-17" / "data.json").read_text(encoding="utf-8"))
        ids = [t["id"] for bb in data["x"]["builders"] for t in bb["tweets"]]
        self.assertEqual(ids, ["999000111222333"])
        self.assertTrue(any("前几天已经发过" in x for x in data["errors"]))

    def test_transcript_cap(self):
        self.assertEqual(bp.transcript_cap([100, 200], 400_000), 0)
        cap = bp.transcript_cap([500_000, 100_000, 50_000], 400_000)
        self.assertLessEqual(min(500_000, cap) + min(100_000, cap) + min(50_000, cap), 400_000)
        self.assertGreater(cap, 100_000)

    def test_commit_seen(self):
        u = bp.USER_DIR
        (u / "seen-videos.json").write_text(json.dumps({"a": 1}))
        (u / "seen-videos.new.json").write_text(json.dumps({"a": 1, "b": 2}))
        bp.commit_seen()
        self.assertEqual(json.loads((u / "seen-videos.json").read_text()), {"a": 1, "b": 2})
        self.assertEqual(json.loads((u / "seen-videos.new.json").read_text()), {})

    def deliver_with(self, codes):
        self.build("2026-10-16")
        calls = []
        def fake(args):
            calls.append(args)
            return codes[min(len(calls) - 1, len(codes) - 1)]
        bp.run_deliver = fake
        (bp.USER_DIR / "seen-videos.new.json").write_text(json.dumps({"v": 1}))
        code, out = helpers.run_main(bp.main, ["build_page.py", "deliver", "--date", "2026-10-16"])
        return code, out, calls

    def test_deliver_retries_then_commits(self):
        code, out, calls = self.deliver_with([1, 0])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("--email-only" in c for c in calls))
        self.assertIn(code, (0, None))
        self.assertEqual(json.loads((bp.USER_DIR / "seen-videos.json").read_text()), {"v": 1})

    def test_deliver_fails_without_commit(self):
        code, out, calls = self.deliver_with([1, 1])
        self.assertEqual(code, 1)
        self.assertFalse((bp.USER_DIR / "seen-videos.json").exists())

    def test_fail_uses_fallback_mail(self):
        bp.run_deliver = lambda args: 1
        sent = []
        bp.send_mail_stdlib = lambda cfg, subj, text: sent.append(subj) or True
        code, out = helpers.run_main(bp.main, ["build_page.py", "fail", "--date", "2026-10-16"])
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)

    def test_fetch_wait_gives_up_after_three(self):
        bp.WAIT_SECONDS = 0
        bp.FETCH_STATE.write_text(json.dumps({"pid": os.getpid(), "started": time.time(), "date": "2026-10-16", "waits": 2}))
        code, out = helpers.run_main(bp.main, ["build_page.py", "fetch-wait"])
        self.assertEqual(code, 1)
        self.assertIn("抓取失败", out)

    def test_run_date_survives_midnight(self):
        bp.RUN_DATE.write_text(json.dumps({"date": "2026-10-16", "at": time.time() - 3600}))
        self.assertEqual(bp.run_date(self.cfg, SimpleNamespace(date=None)), "2026-10-16")
        bp.RUN_DATE.write_text(json.dumps({"date": "2026-10-16", "at": time.time() - 30 * 3600}))
        self.assertEqual(bp.run_date(self.cfg, SimpleNamespace(date=None)), bp.today_str(self.cfg))


if __name__ == "__main__":
    unittest.main()
