"""取视频脚本：超时、各类错误记不记已收过、每频道上限、时间预算、待补清单、两段式记录。"""
import json
import time
import unittest
from datetime import datetime, timedelta, timezone

import helpers
import fetch_youtube as fy


def ok(vid):
    return {"text": "x" * 800, "lang": "en", "truncated": False, "error": None}


class YoutubeFetchTest(unittest.TestCase):
    def setUp(self):
        self.d = helpers.tmpdir()
        self.saved = {k: getattr(fy, k) for k in (
            "USER_DIR", "STATE_PATH", "STATE_NEW_PATH", "PENDING_PATH", "LOG_DIR", "SOURCES_PATH",
            "get_api", "fetch_channel_rss", "_fetch_transcript_once", "PER_VIDEO_TIMEOUT",
            "TRANSCRIPT_BUDGET_SECONDS", "PROXY_DELAY_MIN", "PROXY_DELAY_MAX", "USING_PROXY", "_LOG_FILE")}
        fy.USER_DIR = self.d
        fy.STATE_PATH = self.d / "seen-videos.json"
        fy.STATE_NEW_PATH = self.d / "seen-videos.new.json"
        fy.PENDING_PATH = self.d / "pending-videos.json"
        fy.LOG_DIR = self.d / "logs"
        fy._LOG_FILE = None
        fy.SOURCES_PATH = self.d / "sources.json"
        helpers.write_json(fy.SOURCES_PATH, {"youtube_channels": [{"name": "频道A", "category": "c", "channelId": "A"}]})
        fy.get_api = lambda: None
        fy.USING_PROXY = True
        fy.PROXY_DELAY_MIN = fy.PROXY_DELAY_MAX = 0
        fy.PER_VIDEO_TIMEOUT = 1
        fy.TRANSCRIPT_BUDGET_SECONDS = 600
        self.now = datetime.now(timezone.utc)
        self.rss = []
        fy.fetch_channel_rss = lambda cid: self.rss

    def tearDown(self):
        if fy._LOG_FILE:
            fy._LOG_FILE.close()
        for k, v in self.saved.items():
            setattr(fy, k, v)

    def add_video(self, vid, hours_ago=2):
        self.rss.append({"published": (self.now - timedelta(hours=hours_ago)).isoformat(),
                         "link": "https://www.youtube.com/watch?v=%s" % vid, "title": "视频 %s" % vid})

    def run_fetch(self, once):
        fy._fetch_transcript_once = once
        fy._START = time.time()
        code, out = helpers.run_main(fy.main, ["fetch_youtube.py"])
        self.assertEqual(code, 0)
        res = json.loads(out)
        new = json.loads(fy.STATE_NEW_PATH.read_text()) if fy.STATE_NEW_PATH.exists() else {}
        pend = json.loads(fy.PENDING_PATH.read_text()) if fy.PENDING_PATH.exists() else {}
        return res, new, pend

    def test_timeout_inside_library_except_is_not_swallowed(self):
        """字幕库里有 except Exception；60 秒时限到了也要停下，视频留到下次，不记已收过。"""
        self.add_video("AAAAAAAAAA1")
        def hang(vid):
            try:
                time.sleep(5)
            except Exception:
                return {"text": None, "lang": None, "truncated": False, "error": ""}
            return ok(vid)
        res, new, pend = self.run_fetch(hang)
        self.assertNotIn("AAAAAAAAAA1", new)
        self.assertIn("AAAAAAAAAA1", pend)
        self.assertTrue(any("下次会再试" in e for e in res["errors"]))

    def test_error_kinds(self):
        """确定取不到的记已收过；刚发布没字幕的、YouTube 临时出错的、代理账户出错的都下次再试。"""
        self.add_video("FRESHNOSUB1", hours_ago=1)
        self.add_video("OLDNOSUB001", hours_ago=30)
        self.add_video("GONEVIDEO01")
        self.add_video("UNPARSABLE1")
        self.add_video("PROXY407001")
        errs = {"FRESHNOSUB1": "TranscriptsDisabled", "OLDNOSUB001": "TranscriptsDisabled",
                "GONEVIDEO01": "VideoUnavailable", "UNPARSABLE1": "YouTubeDataUnparsable: could not parse",
                "PROXY407001": "ProxyError: Max retries exceeded (Caused by ProxyError('Tunnel connection failed: 407 Proxy Authentication Required'))"}
        res, new, pend = self.run_fetch(lambda vid: {"text": None, "lang": None, "truncated": False, "error": errs[vid]})
        self.assertEqual(set(new), {"OLDNOSUB001", "GONEVIDEO01"})
        self.assertEqual(set(pend), {"FRESHNOSUB1", "UNPARSABLE1", "PROXY407001"})
        self.assertTrue(any("代理账户" in e for e in res["errors"]))
        self.assertEqual(len(res["videos"]), 5)  # 都列了标题

    def test_channel_cap(self):
        for i in range(8):
            self.add_video("CAPVIDEO%03d" % i, hours_ago=i + 1)
        calls = []
        res, new, pend = self.run_fetch(lambda vid: calls.append(vid) or ok(vid))
        self.assertEqual(len(calls), fy.PER_CHANNEL_CAP)
        capped = [v for v in res["videos"] if v["transcriptError"] == "over_channel_cap"]
        self.assertEqual(len(capped), 8 - fy.PER_CHANNEL_CAP)
        self.assertEqual(len(new), 8)

    def test_budget_and_pending_retry(self):
        """时间用完的视频进待补清单；下一次运行即使已过 48 小时也会再试，取到后从清单拿掉。"""
        self.add_video("LATEVIDEO01")
        fy.TRANSCRIPT_BUDGET_SECONDS = -1
        res, new, pend = self.run_fetch(ok)
        self.assertEqual(res["videos"], [])
        self.assertIn("LATEVIDEO01", pend)
        self.assertTrue(any("时间不够" in e for e in res["errors"]))
        # 第二天：这个视频已经不在 48 小时内，也不在视频列表里了
        self.rss.clear()
        fy.TRANSCRIPT_BUDGET_SECONDS = 600
        res, new, pend = self.run_fetch(ok)
        self.assertEqual([v["videoId"] for v in res["videos"]], ["LATEVIDEO01"])
        self.assertIn("LATEVIDEO01", new)
        self.assertNotIn("LATEVIDEO01", pend)

    def test_two_phase_state(self):
        """已收过的先写进 .new 文件，正式记录不动。"""
        helpers.write_json(fy.STATE_PATH, {"OLDSEENVID1": time.time()})
        self.add_video("NEWVIDEO001")
        self.run_fetch(ok)
        self.assertEqual(set(json.loads(fy.STATE_PATH.read_text())), {"OLDSEENVID1"})
        self.assertEqual(set(json.loads(fy.STATE_NEW_PATH.read_text())), {"OLDSEENVID1", "NEWVIDEO001"})


if __name__ == "__main__":
    unittest.main()
