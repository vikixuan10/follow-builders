"""博客抓取、总调度、投递脚本。不连网络：网络请求都换成假的。"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import helpers
import fetch_blogs as fb
import prepare_digest as pd
import deliver as dv


class BlogTest(unittest.TestCase):
    def test_longest_block_wins(self):
        html = ("<title>T</title><article><p>Related: other post</p></article>"
                "<main><article><p>" + "Real article body. " * 200 + "</p></article></main>")
        a = fb.extract_article_content(html)
        self.assertGreater(len(a["content"]), 2000)

    def test_rss_filters(self):
        now = datetime.now(timezone.utc)
        def item(title, days_ago, cats):
            c = "".join("<category>%s</category>" % x for x in cats)
            return ("<item><title>%s</title><link>https://ex.test/%s</link><pubDate>%s</pubDate>%s</item>"
                    % (title, title, format_datetime(now - timedelta(days=days_ago)), c))
        xml = "<rss>" + item("product", 1, ["Product"]) + item("customer", 1, []) + item("startup", 1, ["Startup"]) \
              + item("old", 10, ["Research"]) + "</rss>"
        links = fb.extract_rss_links(xml, {"requireCategory": True, "excludeCategories": ["Startup"], "maxAgeDays": 4})
        self.assertEqual([l["slug"] for l in links], ["product"])
        self.assertEqual(links[0]["title"], "product")


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.saved = (pd.run_script, pd.fetch_zara_x_feed, pd.X_STALE_WAIT)
        pd.X_STALE_WAIT = 0

    def tearDown(self):
        pd.run_script, pd.fetch_zara_x_feed, pd.X_STALE_WAIT = self.saved

    def test_errors_pass_through_and_stale_feed(self):
        def fake_run(name, *args):
            if name == "fetch_youtube.py":
                return {"videos": [], "stats": {}, "errors": ["YouTube：频道A 的视频列表没取到"]}
            return {"posts": [], "stats": {}, "errors": ["博客：某来源的目录页打不开"]}
        pd.run_script = fake_run
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        pd.fetch_zara_x_feed = lambda url: {"generatedAt": old, "x": []}
        code, out = helpers.run_main(pd.main, ["prepare_digest.py"])
        errs = json.loads(out)["errors"]
        self.assertIn("YouTube：频道A 的视频列表没取到", errs)
        self.assertIn("博客：某来源的目录页打不开", errs)
        self.assertTrue(any(e.startswith("X：订阅源还没更新") for e in errs))


class DeliverTest(unittest.TestCase):
    def test_email_failure_returns_1(self):
        saved = (dv.requests.post, dv.load_env, dv.load_config)
        class R:
            status_code = 429
            text = "rate limited"
        dv.requests.post = lambda *a, **k: R()
        dv.load_env = lambda: {"RESEND_API_KEY": "test"}
        dv.load_config = lambda: {"delivery": {"method": "obsidian_and_email", "email": "me@example.test"}, "timezone": "Europe/Dublin"}
        try:
            code, out = helpers.run_main(dv.main, ["deliver.py", "--email-only", "--message", "hi", "--subject", "s"])
        finally:
            dv.requests.post, dv.load_env, dv.load_config = saved
        self.assertEqual(code, 1)
        self.assertIn('"status": "failed"', out)


if __name__ == "__main__":
    unittest.main()
