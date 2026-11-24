"""网页面板（docs/操控电脑.md 第 9 节）：在程序里看 Agent 操作的网页，自己点、滚、打字、输网址，弹框交给用户。

要 playwright 和一个浏览器（无界面）；没有就跳过要浏览器的那几组。本机起 tests/websites.py 的小网站，browser.allow_local 打开。"""
from __future__ import annotations
import http.client, importlib.util, json, os, time, unittest

from helpers import Case, ApiCase, wait_until
from agent import browser as bmod
from agent.tools import load_all
from agent.tools import browser as btools
load_all()

HAS_PW = importlib.util.find_spec("playwright") is not None
PW_SKIP = "没有安装 playwright"


class Pure(unittest.TestCase):
    def test_normalize(self):
        n = bmod.normalize_user_url
        self.assertEqual(n("https://oa.example.com/x"), "https://oa.example.com/x")
        self.assertEqual(n("oa.example.com/a b"), "https://www.baidu.com/s?wd=oa.example.com/a%20b")     # 有空格：当成要搜的
        self.assertEqual(n("oa.example.com"), "https://oa.example.com")
        self.assertEqual(n("localhost:3000"), "http://localhost:3000")
        self.assertEqual(n("10.0.0.5:8080/oa"), "http://10.0.0.5:8080/oa")
        self.assertEqual(n("天气 上海"), "https://www.baidu.com/s?wd=%E5%A4%A9%E6%B0%94%20%E4%B8%8A%E6%B5%B7")
        self.assertTrue(n("javascript:alert(1)").startswith("https://www.baidu.com/s?wd="))
        self.assertTrue(n("file:///etc/passwd").startswith("https://www.baidu.com/s?wd="))

        class C:
            def get(self, *k, **kw):
                return "https://cn.bing.com/search?q={q}" if k == ("browser", "search_url") else kw.get("default")
        self.assertEqual(n("报销", C()), "https://cn.bing.com/search?q=%E6%8A%A5%E9%94%80")
        with self.assertRaises(bmod.BrowserError):
            n("  ")

    def test_headless_mode(self):
        class C:
            def __init__(self, view, headless=False):
                self.v = {"view": view, "headless": headless}

            def get(self, a, b, default=None):
                return self.v.get(b, default)
        self.assertTrue(bmod.headless_mode(C("panel")))
        self.assertFalse(bmod.headless_mode(C("window")))
        self.assertTrue(bmod.headless_mode(C("window", headless=True)))

    def test_human_check(self):
        class T:
            def __init__(self, title, snap):
                self.title, self.snap = title, snap
        self.assertTrue(btools.human_check(T("Just a moment...", "- heading \"请稍候\"")))
        self.assertTrue(btools.human_check(T("登录", "- text: 请拖动滑块完成验证")))
        self.assertFalse(btools.human_check(T("费用报销", "- textbox \"事由\"")))
        # 正文很后面提到"验证码"不算（只看开头 60 行）
        self.assertFalse(btools.human_check(T("制度", "\n".join(["- paragraph: 第%d条" % i for i in range(80)] + ["- text: captcha"]))))


@unittest.skipUnless(HAS_PW, PW_SKIP)
class Panel(ApiCase):
    env_kw = {"mode": "full", "browser": {"allow_local": True, "allowed_domains": ["127.0.0.1"]}}

    def setUp(self):
        super().setUp()
        from websites import Site
        self.site = Site()
        self.addCleanup(self.site.close)
        self.m = bmod.get(self.e.rt)
        self.sid = self.e.new_session()

    def tearDown(self):
        try:
            self.m.close()
        finally:
            super().tearDown()

    def state(self):
        st, b = self.req("GET", "/api/browser/state?session_id=%s" % self.sid)
        self.assertEqual(st, 200, b)
        return b

    def inp(self, **kw):
        return self.req("POST", "/api/browser/input", dict(kw, session_id=self.sid))

    def center(self, selector):
        """元素中心在画面上的位置（0 到 1）。"""
        def run(d):
            page = self.m.tab(self.sid).handle["page"]
            box = page.locator(selector).first.bounding_box()
            vp = page.viewport_size
            return (box["x"] + box["width"] / 2) / vp["width"], (box["y"] + box["height"] / 2) / vp["height"]
        return self.m.call(run)

    def page_eval(self, js):
        return self.m.call(lambda d: self.m.tab(self.sid).handle["page"].evaluate(js))

    def watch(self):
        self.m.watch(self.sid)
        self.addCleanup(self.m.unwatch, self.sid)

    def test_no_tab_yet(self):
        b = self.state()
        self.assertEqual((b["available"], b["open"], b["view"]), (True, False, "panel"))
        self.assertEqual(self.req("POST", "/api/browser/input", {"session_id": self.sid, "kind": "click", "x": 0.5, "y": 0.5})[0], 409)
        self.assertEqual(self.req("GET", "/api/browser/state?session_id=nope")[0], 404)

    def test_frames_click_and_refs_dropped(self):
        t = self.m.open(self.sid, self.site.url("/"))
        self.assertTrue(self.m.driver.headless)                 # 面板模式：不开窗口
        t.refs = {"e5": ("link", "费用报销")}                     # 假装 Agent 看过这一页
        self.watch()
        f = self.m.wait_frame(self.sid, 0, 10)
        self.assertIsNotNone(f)
        self.assertEqual(f["jpeg"][:2], b"\xff\xd8")             # JPEG
        self.assertEqual((f["w"], f["h"]), (1280, 860))
        x, y = self.center("text=费用报销")
        self.assertEqual(self.inp(kind="click", x=x, y=y)[0], 200)
        self.assertTrue(wait_until(lambda: self.state()["url"].endswith("/form"), 10), self.state())
        self.assertEqual(t.refs, {})                             # Agent 手里那份页面概况作废了
        self.assertTrue(wait_until(lambda: (self.m.frames[self.sid]["seq"] > f["seq"]), 10))
        b = self.state()
        self.assertEqual(b["title"], "费用报销 - 测试 OA")
        self.assertGreater(b["frame"], 1)
        # 打字：点输入框，再输中文
        x, y = self.center("input[name=reason]")
        self.inp(kind="click", x=x, y=y)
        self.assertEqual(self.inp(kind="text", text="去上海出差")[0], 200)
        self.assertEqual(self.inp(kind="key", key="Backspace")[0], 200)
        self.assertEqual(self.page_eval("document.querySelector('input[name=reason]').value"), "去上海出")
        # 滚动
        self.assertEqual(self.inp(kind="wheel", x=0.5, y=0.5, dx=0, dy=300)[0], 200)
        # 后退、前进、刷新
        self.assertEqual(self.req("POST", "/api/browser/nav", {"session_id": self.sid, "action": "back"})[0], 200)
        self.assertTrue(wait_until(lambda: self.state()["url"].endswith("/"), 10))
        self.assertEqual(self.req("POST", "/api/browser/nav", {"session_id": self.sid, "action": "forward"})[0], 200)
        self.assertTrue(wait_until(lambda: self.state()["url"].endswith("/form"), 10))
        self.assertEqual(self.req("POST", "/api/browser/nav", {"session_id": self.sid, "action": "reload"})[0], 200)

    def test_size_follows_panel(self):
        self.m.open(self.sid, self.site.url("/"))
        self.watch()
        st, b = self.req("POST", "/api/browser/size", {"session_id": self.sid, "width": 700, "height": 1000})
        self.assertEqual((st, b), (200, {"width": 1024, "height": 1463}))         # 窄面板：网页还按 1024 宽排，缩小显示
        self.assertTrue(wait_until(lambda: (self.m.frames.get(self.sid) or {}).get("w") == 1024
                                   and self.m.frames[self.sid].get("h") == 1463, 10), self.m.frames.get(self.sid, {}).get("w"))
        self.assertEqual(self.m.call(lambda d: self.m.tab(self.sid).handle["page"].viewport_size), {"width": 1024, "height": 1463})
        st, b = self.req("POST", "/api/browser/size", {"session_id": self.sid, "width": 1300, "height": 900})
        self.assertEqual(b, {"width": 1300, "height": 900})                       # 够宽：一比一
        self.assertTrue(wait_until(lambda: self.m.frames[self.sid].get("w") == 1300, 10))
        # 点的位置按新的大小换算
        x, y = self.center("text=费用报销")
        self.inp(kind="click", x=x, y=y)
        self.assertTrue(wait_until(lambda: self.state()["url"].endswith("/form"), 10))
        self.assertEqual(self.req("POST", "/api/browser/size", {"session_id": self.sid, "width": 5, "height": 900})[0], 400)

    def test_address_bar_and_guards(self):
        st, b = self.req("POST", "/api/browser/nav", {"session_id": self.sid, "action": "go", "url": self.site.url("/table")})
        self.assertEqual(st, 200, b)                              # 还没有标签页：地址栏会开一个
        self.assertTrue(wait_until(lambda: self.state().get("url", "").endswith("/table"), 10))
        own = "http://127.0.0.1:%d/" % self.srv.port
        self.e.rt.http_port = self.srv.port
        st, b = self.req("POST", "/api/browser/nav", {"session_id": self.sid, "action": "go", "url": own})
        self.assertEqual(st, 409)
        self.assertIn("不能在这里打开", b["error"])
        bad = [({"kind": "type"}, 400), ({"kind": "click", "x": 2, "y": 0}, 400), ({"kind": "click", "x": True, "y": 0}, 400),
               ({"kind": "key", "key": "Meta+q"}, 400), ({"kind": "text", "text": ""}, 400), ({"kind": "text", "text": "x" * 2001}, 400)]
        for body, code in bad:
            self.assertEqual(self.inp(**body)[0], code, body)
        self.assertEqual(self.req("POST", "/api/browser/nav", {"session_id": self.sid, "action": "go", "url": ""})[0], 400)
        self.assertEqual(self.req("POST", "/api/browser/nav", {"session_id": self.sid, "action": "delete"})[0], 400)
        self.assertEqual(self.req("POST", "/api/browser/dialog", {"session_id": self.sid, "accept": "yes"})[0], 400)

    def test_dialog_goes_to_user(self):
        self.m.open(self.sid, self.site.url("/confirm"))
        self.watch()
        x, y = self.center("text=群发通知")
        import threading
        clicks = []
        th = threading.Thread(target=lambda: clicks.append(self.inp(kind="click", x=x, y=y)[0]))
        th.start()
        self.assertTrue(wait_until(lambda: (self.state().get("dialog") or {}).get("type") == "confirm", 10), self.state())
        self.assertEqual(self.state()["dialog"]["message"], "确定要群发通知吗？")
        # 弹框在事件流里也推了一次（不写账本）
        self.assertFalse(self.e.rt.store.events(self.sid, types=("browser_view",)))
        st, b = self.req("POST", "/api/browser/dialog", {"session_id": self.sid, "accept": True})
        self.assertEqual((st, b), (200, {"ok": True}))
        th.join(10)
        self.assertEqual(clicks, [200])                          # 答完了，那一下点击才算做完
        self.assertTrue(wait_until(lambda: self.page_eval("document.getElementById('r').textContent") == "已群发", 10))
        self.assertIsNone(self.state().get("dialog"))
        self.assertEqual(self.req("POST", "/api/browser/dialog", {"session_id": self.sid, "accept": True})[1], {"ok": False})
        # 再点一次、选取消
        th = threading.Thread(target=lambda: clicks.append(self.inp(kind="click", x=x, y=y)[0]))
        th.start()
        self.assertTrue(wait_until(lambda: (self.state().get("dialog") or {}).get("type") == "confirm", 10))
        self.req("POST", "/api/browser/dialog", {"session_id": self.sid, "accept": False})
        th.join(10)
        self.assertTrue(wait_until(lambda: self.page_eval("document.getElementById('r').textContent") == "没有发送", 10))

    def test_agent_dialogs_unchanged(self):
        """Agent 自己点出来的弹框照旧由 Agent 那边处理（批准过的接受、其余取消），不留给用户。"""
        t = self.m.open(self.sid, self.site.url("/confirm"))
        from agent import pageview
        self.m.look(self.sid)
        refs = pageview.refs(pageview.parse(t.snap))
        ref = next(r for r, (role, name) in refs.items() if name == "群发通知")
        r = self.m.act(self.sid, "click", ref, None, accept_dialogs=False)
        self.assertEqual([d["type"] for d in r["dialogs"]], ["confirm"])
        self.assertIsNone(self.state().get("dialog"))

    def test_new_tab_and_download(self):
        self.m.open(self.sid, self.site.url("/newtab"))
        self.watch()
        x, y = self.center("text=在新窗口打开报销表单")
        self.inp(kind="click", x=x, y=y)
        self.assertTrue(wait_until(lambda: self.state()["url"].endswith("/form"), 10), self.state())   # 跟着新开的页面走
        self.req("POST", "/api/browser/nav", {"session_id": self.sid, "action": "go", "url": self.site.url("/download")})
        self.assertTrue(wait_until(lambda: self.state()["url"].endswith("/download"), 10))
        x, y = self.center("text=下载报表")
        q = self.e.rt.subscribe() if hasattr(self.e.rt, "subscribe") else None
        self.inp(kind="click", x=x, y=y)
        path = os.path.join(self.e.root, "下载", "report.csv")
        self.assertTrue(wait_until(lambda: os.path.exists(path), 10))
        with open(path, encoding="utf-8") as f:
            self.assertIn("BX-001", f.read())
        if q is not None:
            notes = []
            end = time.time() + 5
            while time.time() < end and not notes:
                try:
                    ev = q.get(timeout=0.5)
                except Exception:
                    continue
                if ev and ev.get("type") == "browser_view" and (ev.get("payload") or {}).get("note"):
                    notes.append(ev["payload"]["note"])
            self.e.rt.unsubscribe(q)
            self.assertEqual(notes, ["下载好了：下载/report.csv"])

    def test_mjpeg_stream(self):
        self.m.open(self.sid, self.site.url("/"))
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.putrequest("GET", "/api/browser/view?session_id=%s&t=%s" % (self.sid, self.srv.token), skip_host=True)
        c.putheader("Host", "127.0.0.1:%d" % self.srv.port)
        c.endheaders()
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("multipart/x-mixed-replace", r.getheader("Content-Type"))
        head = b""
        while b"\r\n\r\n" not in head:
            head += r.read(1)
        self.assertIn(b"--frame", head)
        n = int(head.split(b"Content-Length: ")[1].split(b"\r\n")[0])
        data = r.read(n)
        self.assertEqual(data[:2], b"\xff\xd8")
        self.assertEqual(self.m.viewers.get(self.sid), 1)
        r.close()
        c.close()
        # 界面关了面板：服务端发下一帧时发现连接断了，不再算在看
        self.inp(kind="wheel", x=0.5, y=0.5, dx=0, dy=200)
        self.m._frame(self.sid, b"\xff\xd8x", {})
        self.assertTrue(wait_until(lambda: not self.m.viewers.get(self.sid), 15))
        # 没带令牌不行
        st, _ = self.req("GET", "/api/browser/view?session_id=%s" % self.sid, token=False)
        self.assertEqual(st, 403)

    def test_settings_view(self):
        self.assertEqual(self.req("POST", "/api/settings", {"browser": {"view": "both"}})[0], 400)
        self.m.open(self.sid, self.site.url("/"))
        self.assertEqual(self.req("POST", "/api/settings", {"browser": {"view": "window"}})[0], 200)
        self.assertEqual(self.e.cfg.get("browser", "view"), "window")
        self.assertIsNone(self.m.tab(self.sid))                  # 换了显示方式：浏览器关掉，下次按新方式开
        self.assertEqual(self.state()["view"], "window")
        self.e.cfg.update({"browser": {"view": "panel"}})

    def test_human_check_in_tool_result(self):
        self.e.mock.script[:] = [{"content": "", "tool_calls": [("browser_open", {"url": self.site.url("/verify")})]},
                                 {"content": "需要你验证"}]
        sid = self.e.start("打开")
        self.assertTrue(self.e.rt.wait_idle(sid, 60))
        res = self.e.results(sid)[0]["content"]
        self.assertIn("这个页面在要求人机验证", res)
        self.assertIn("网页面板", self.e.mock.calls[0][0]["content"])      # 系统提示里说了有网页面板


if __name__ == "__main__":
    unittest.main()
