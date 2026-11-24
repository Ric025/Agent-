"""模型能力和功能开关（docs/操控电脑.md 第 1 节、第 7 节）。

用两个假的 OpenAI 兼容服务（tests/fake_openai.py）验证真实的探测和选端点；演示模型（MockLLM）的组合用 Env。"""
from __future__ import annotations
import json, os, shutil, tempfile, time, unittest

from helpers import Case, ApiCase, Env, wait_until, APP
from fake_openai import FakeOpenAI
from agent import capability, context, desktop
from agent.config import Config
from agent.runtime import Runtime

IMG = capability.data_url(capability.red_png())
ALL_FEATURES = ["image_input", "browser", "browser_vision", "desktop"]


class FakeDesktop(desktop.Driver):
    name = "fake"
    platforms = ()
    needs_vision = False

    def available(self):
        return True, ""


class BrowserPresent:
    """功能状态的测试和这台电脑装没装浏览器组件无关：当作装了（缺组件那一条单独测）。"""

    def setUp(self):
        from agent import browser
        self._orig_comp = browser.component_ok
        browser.component_ok = lambda cfg: (True, "")
        super().setUp()

    def tearDown(self):
        from agent import browser
        browser.component_ok = self._orig_comp
        super().tearDown()


def feats(rt):
    return {f["id"]: f for f in capability.get(rt).features()}


class RealEnv:
    """用真实的 OpenAIClient 连假服务的运行时。"""

    def __init__(self, endpoints, **patch):
        self.tmp = tempfile.mkdtemp(prefix="ca_caps_")
        self.home = os.path.join(self.tmp, "home")
        self.root = os.path.join(self.tmp, "ws")
        os.makedirs(self.root)
        self.cfg = Config(self.home)
        conf = {"endpoints": endpoints, "kernel": "builtin", "approval_mode": "full",
                "memory": {"enabled": False, "auto": False}, "model": {"timeout": 10}}
        conf.update(patch)
        self.cfg.update(conf)
        self.rt = Runtime(self.cfg, None, app_dir=APP)
        self.ws = self.rt.store.add_workspace("测试", self.root)
        self.rt.caps.probe_all()

    def session(self):
        return self.rt.store.add_session(self.ws["id"])["id"]

    def say(self, sid, text, images=None):
        self.rt.send_message(sid, text, images)
        assert wait_until(lambda: self.rt.wait_idle(sid, 0.1), 30)
        return self.rt.store.events(sid)

    def close(self):
        self.rt.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)


class Words(unittest.TestCase):
    def test_reasons(self):
        self.assertEqual(capability.model_reason("image_input", "vision", False), "当前模型不支持看图，无法使用「看图片附件」。")
        self.assertEqual(capability.model_reason("browser", "tools", False), "当前模型不支持工具调用，无法使用「操作网页」。")
        self.assertEqual(capability.model_reason("browser", "context", False, 16000),
                         "当前模型的上下文太小（至少要 16K），无法使用「操作网页」。")
        self.assertEqual(capability.model_reason("image_input", "vision", True), "接入的模型都不支持看图，无法使用「看图片附件」。")
        self.assertEqual(capability.component_reason("browser"), "这台电脑没有安装浏览器组件，无法使用「操作网页」。重新自动安装：bash start.sh --setup（Windows：start.bat --setup）。")
        self.assertEqual(capability.component_reason("desktop"), "这台电脑上还没有桌面操控组件，无法使用「操作桌面程序」。")
        self.assertEqual(capability.setting_reason("browser"), "「操作网页」在设置里关着。")

    def test_pointing_classify(self):
        self.assertEqual(capability.classify_point(capability.parse_point("192,64")), "pixel")
        self.assertEqual(capability.classify_point(capability.parse_point("中心大约在 (210, 80)")), "pixel")
        self.assertEqual(capability.classify_point(capability.parse_point("750,250")), "norm1000")
        self.assertFalse(capability.classify_point(capability.parse_point("10,10")))
        self.assertFalse(capability.classify_point(capability.parse_point("看不出来")))

    def test_png(self):
        b = capability.red_png()
        self.assertTrue(b.startswith(b"\x89PNG"))
        self.assertTrue(capability.square_png().startswith(b"\x89PNG"))


class MockCaps(BrowserPresent, Case):
    """演示模型：工具调用是、看图否；model.caps 和端点 caps 可以覆盖。"""

    def test_mock_defaults(self):
        e = self.env([])
        r = capability.get(e.rt).report()
        m = r["model"]
        self.assertEqual((m["tools"], m["vision"], m["pointing"], m["context"]), (True, False, False, 32768))
        f = feats(e.rt)
        self.assertFalse(f["image_input"]["ok"])
        self.assertEqual(f["image_input"]["reason"], "当前模型不支持看图，无法使用「看图片附件」。")
        self.assertEqual(f["image_input"]["fallback"], "可以把图里的内容用文字发来，或者上传文档。")
        self.assertEqual(f["browser_vision"]["reason"], "当前模型不支持看图，无法使用「看网页截图」。")
        self.assertEqual(f["desktop"]["reason_kind"], "model")
        for x in f.values():
            self.assertEqual(set(x), {"id", "name", "ok", "unknown", "reason_kind", "reason", "via", "fallback"})

    def test_priority(self):
        e = self.env([], model={"caps": {"vision": True}})
        self.assertEqual(capability.get(e.rt).models()[0]["source"]["vision"], "config")
        self.assertTrue(feats(e.rt)["image_input"]["ok"])
        eps = e.cfg.get("endpoints")
        eps[0]["caps"] = {"vision": False}
        e.cfg.update({"endpoints": eps})
        self.assertFalse(feats(e.rt)["image_input"]["ok"])          # 端点自己的 > 全局的

    def test_context_and_setting(self):
        e = self.env([], model={"context_window": 12000})
        f = feats(e.rt)
        self.assertEqual(f["browser"]["reason"], "当前模型的上下文太小（至少要 16K），无法使用「操作网页」。")
        e.cfg.update({"model": {"context_window": 32768}, "browser": {"enabled": False}})
        f = feats(e.rt)
        self.assertEqual((f["browser"]["reason_kind"], f["browser"]["reason"]), ("setting", "「操作网页」在设置里关着。"))
        self.assertEqual(f["browser_vision"]["reason"], "「操作网页」在设置里关着。")

    def test_tools_missing(self):
        e = self.env([], model={"caps": {"tools": False}})
        f = feats(e.rt)
        self.assertEqual(f["browser"]["reason"], "当前模型不支持工具调用，无法使用「操作网页」。")
        names = [t.name for t in __import__("agent.tools", fromlist=["available"]).available(
            __import__("types").SimpleNamespace(rt=e.rt, store=e.rt.store, cfg=e.cfg, sid="x", ws=e.ws, root=e.root,
                                                is_sub=False, runner=None))]
        self.assertNotIn("browser_open", names)

    def test_component_missing(self):
        from agent import browser
        browser.component_ok = self._orig_comp
        e = self.env([], browser={"executable_path": "/没有/这个/浏览器"})
        browser._comp_cache.clear()
        f = feats(e.rt)
        self.assertEqual(f["browser"]["reason_kind"], "component")
        self.assertEqual(f["browser"]["reason"], "这台电脑没有安装浏览器组件，无法使用「操作网页」。重新自动安装：bash start.sh --setup（Windows：start.bat --setup）。")
        browser._comp_cache.clear()

    def test_unknown_counts_as_ok(self):
        e = self.env([])
        e.rt.llm_factory_custom = False          # 当成真实模型、但没探测过
        try:
            f = feats(e.rt)
            self.assertTrue(f["image_input"]["ok"])
            self.assertTrue(f["image_input"]["unknown"])
            r = capability.get(e.rt).report()
            self.assertIsNone(r["model"]["vision"])
            self.assertEqual(r["model"]["source"]["vision"], "unknown")
            self.assertIsNone(r["model"]["probed_at"])
        finally:
            e.rt.llm_factory_custom = True

    def test_prompt_section(self):
        e = self.env([], model={"caps": {"tools": False}})
        sec = capability.get(e.rt).prompt_section()
        self.assertIn("当前模型不支持工具调用，无法使用「操作网页」。", sec)
        self.assertIn("当前模型不支持看图，无法使用「看图片附件」。可以把图里的内容用文字发来，或者上传文档。", sec)
        self.assertLessEqual(context.estimate_text(sec), 220)
        sysp = context.system_prompt(e.rt, e.ws)
        self.assertIn(sec, sysp)
        self.assertNotIn("# 操作网页", sysp)
        # 全部不可用（多个端点的说法更长）时也不超过 220
        e.cfg.update({"endpoints": e.cfg.get("endpoints") * 2, "model": {"context_window": 8000}})
        sec = capability.get(e.rt).prompt_section()
        self.assertEqual(sum(1 for f in capability.get(e.rt).features() if not f["ok"]), 4)
        self.assertLessEqual(context.estimate_text(sec), 220, sec)

    def test_prompt_absent_when_all_ok(self):
        d = desktop.register(FakeDesktop())
        try:
            e = self.env([], model={"caps": {"vision": True, "pointing": "pixel"}})
            self.assertTrue(all(f["ok"] for f in capability.get(e.rt).features()))
            self.assertEqual(capability.get(e.rt).prompt_section(), "")
            self.assertNotIn("暂时不能用的功能", context.system_prompt(e.rt, e.ws))
        finally:
            desktop.unregister(d)

    def test_mock_image_rejected_in_loop(self):
        """看图为否、而消息还是带了图（不经接口）：不发请求，直接用原话回答。"""
        e = self.env([{"content": "不该被调用"}])
        sid = e.new_session()
        e.rt.send_message(sid, "看看这张图", [IMG])
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertEqual(e.mock.calls, [])
        msg = e.events(sid, ("assistant_message",))[-1]["payload"]["content"]
        self.assertEqual(msg, "当前模型不支持看图，无法使用「看图片附件」。可以把图里的内容用文字发来，或者上传文档。")
        self.assertEqual(e.status(sid), "done")


class CapsApi(ApiCase):
    script = [{"content": "好"}]

    def test_shapes(self):
        st, b = self.req("GET", "/api/capabilities")
        self.assertEqual(st, 200)
        self.assertEqual(set(b), {"models", "model", "features", "embedding"})      # embedding：docs/两种模式.md 第 5 节
        self.assertEqual(set(b["models"][0]), {"name", "model", "alive", "tools", "vision", "pointing", "context",
                                               "context_detected", "source", "probed_at"})
        self.assertEqual(set(b["model"]), {"tools", "vision", "pointing", "context", "context_detected", "source", "probed_at"})
        self.assertEqual([f["id"] for f in b["features"]], ALL_FEATURES + ["semantic_search"])
        self.assertEqual(self.req("POST", "/api/capabilities/probe")[1], b)
        st, s = self.req("GET", "/api/state")
        self.assertEqual(set(s["features"]), set(ALL_FEATURES + ["semantic_search"]))
        self.assertIs(s["features"]["image_input"], False)

    def test_image_message_rejected(self):
        st, s = self.req("POST", "/api/sessions", {"workspace_id": self.e.ws["id"], "text": "看图", "images": [IMG]})
        self.assertEqual(st, 400)
        self.assertEqual(s["error"], "当前模型不支持看图，无法使用「看图片附件」。")
        sid = self.e.new_session()
        st, s = self.req("POST", "/api/sessions/%s/message" % sid, {"text": "看图", "images": [IMG]})
        self.assertEqual((st, s["error"]), (400, "当前模型不支持看图，无法使用「看图片附件」。"))
        self.e.cfg.update({"model": {"caps": {"vision": True}}})
        st, s = self.req("POST", "/api/sessions/%s/message" % sid, {"text": "看图", "images": [IMG]})
        self.assertEqual(st, 200)

    def test_endpoint_settings(self):
        eps = self.req("GET", "/api/settings")[1]["config"]["endpoints"]
        eps[0]["context_window"] = 1000
        self.assertEqual(self.req("POST", "/api/settings", {"endpoints": eps})[0], 400)
        eps[0]["context_window"] = 65536
        eps[0]["caps"] = {"vision": True, "pointing": "norm1000"}
        self.assertEqual(self.req("POST", "/api/settings", {"endpoints": eps})[0], 200)
        self.assertEqual(self.e.cfg.get("endpoints")[0]["context_window"], 65536)
        m = self.req("GET", "/api/capabilities")[1]["models"][0]
        self.assertEqual((m["vision"], m["pointing"], m["context"]), (True, "norm1000", 65536))
        eps[0]["caps"] = {"pointing": "yes"}
        self.assertEqual(self.req("POST", "/api/settings", {"endpoints": eps})[0], 400)


class Probe(BrowserPresent, unittest.TestCase):
    """真实的探测代码对假服务。"""

    def setUp(self):
        super().setUp()
        self.servers = []
        self.envs = []

    def tearDown(self):
        for e in self.envs:
            e.close()
        for s in self.servers:
            s.close()
        super().tearDown()

    def fake(self, *a, **kw):
        s = FakeOpenAI(*a, **kw)
        self.servers.append(s)
        return s

    def renv(self, eps, **kw):
        e = RealEnv(eps, **kw)
        self.envs.append(e)
        return e

    def test_probe_single(self):
        a = self.fake("GX10-A", max_len=262144)
        e = self.renv([a.endpoint()])
        r = capability.get(e.rt).report()
        m = r["models"][0]
        self.assertEqual((m["alive"], m["tools"], m["vision"], m["pointing"]), (True, True, False, False))
        self.assertEqual(m["source"], {"tools": "probe", "vision": "probe", "pointing": "probe"})
        self.assertEqual((m["context"], m["context_detected"]), (32768, 262144))
        self.assertIsInstance(m["probed_at"], float)
        # 结果存在账本的 meta 里，键里带端点地址和模型名
        self.assertTrue(e.rt.store.meta_get("caps:%s|m" % a.base_url))

    def test_probe_pointing_answers(self):
        for ans, want in (("192,64", "pixel"), ("750,250", "norm1000"), ("10,10", False)):
            s = self.fake("B", vision=True, point=ans)
            self.assertEqual(capability.probe_pointing(s.endpoint(), Config(tempfile.mkdtemp())), want)

    def test_probe_tools_no(self):
        s = self.fake("T", tools=False)
        self.assertFalse(capability.probe_tools(s.endpoint(), Config(tempfile.mkdtemp())))

    def test_priority_three_layers(self):
        a = self.fake("A", vision=True, point="192,64")
        e = self.renv([a.endpoint()])
        self.assertEqual(capability.get(e.rt).models()[0]["vision"], True)
        e.cfg.update({"model": {"caps": {"vision": False}}})
        m = capability.get(e.rt).models()[0]
        self.assertEqual((m["vision"], m["source"]["vision"]), (False, "config"))
        e.cfg.update({"endpoints": [a.endpoint(caps={"vision": True})]})
        self.assertEqual(capability.get(e.rt).models()[0]["vision"], True)

    def test_three_cases(self):
        """7.4 节的三种情况。"""
        a = self.fake("GX10-A")
        b = self.fake("GX10-B", vision=True, point="750,250", max_len=262144)
        # 1. 只有一个 32K 的纯文字模型
        e = self.renv([a.endpoint()])
        f = feats(e.rt)
        self.assertTrue(f["browser"]["ok"])
        self.assertEqual(f["browser"]["via"], "GX10-A")
        for k in ("image_input", "browser_vision", "desktop"):
            self.assertFalse(f[k]["ok"])
            self.assertTrue(f[k]["fallback"])
        self.assertEqual(f["desktop"]["fallback"], "网页上的事可以改用「操作网页」来做。")
        # 2. 再接一个能看图、能指位置的大模型，排在后面
        e.cfg.update({"endpoints": [a.endpoint(), b.endpoint(context_window=262144)]})
        capability.get(e.rt).probe_all()
        d = desktop.register(FakeDesktop())
        try:
            f = feats(e.rt)
            self.assertTrue(all(f[k]["ok"] for k in ALL_FEATURES), f)
            self.assertEqual(f["image_input"]["via"], "GX10-B")
            self.assertEqual(f["browser"]["via"], "GX10-A")
            self.assertEqual(f["desktop"]["via"], "GX10-B")
            r = capability.get(e.rt).report()
            self.assertEqual((r["model"]["vision"], r["model"]["pointing"], r["model"]["context"]), (True, "norm1000", 32768))
        finally:
            desktop.unregister(d)
        f = feats(e.rt)
        self.assertEqual(f["desktop"]["reason_kind"], "component")
        # 3. 大模型掉线了：回到第一种情况（多个端点的说法）
        b.down = True
        capability.get(e.rt).probe_all()
        f = feats(e.rt)
        self.assertTrue(f["browser"]["ok"])
        self.assertEqual(f["image_input"]["reason"], "接入的模型都不支持看图，无法使用「看图片附件」。")
        self.assertIs(capability.get(e.rt).models()[1]["alive"], False)
        b.down = False
        capability.get(e.rt).probe_all()
        self.assertTrue(feats(e.rt)["image_input"]["ok"])

    def test_choose_endpoint(self):
        """7.3：普通请求去第一个；带图去第二个并提示一次；这次运行留在第二个；下一次运行回到第一个。"""
        a = self.fake("GX10-A")
        b = self.fake("GX10-B", vision=True, point="192,64", max_len=262144)
        e = self.renv([a.endpoint(), b.endpoint(context_window=262144)])
        sid = e.session()
        e.say(sid, "你好")
        self.assertEqual((len(a.chats()), len(b.chats())), (1, 0))
        e.say(sid, "看看这张图", [IMG])
        self.assertEqual((len(a.chats()), len(b.chats())), (1, 1))
        notes = [x["payload"]["text"] for x in e.rt.store.events(sid, types=("notice",))]
        self.assertEqual(notes, ["这一步需要看图，已改用 GX10-B。"])
        st = e.rt.session_status(sid)
        self.assertEqual((st["model"], st["endpoint"]), ("m", "GX10-B"))
        # 下一次运行回到第一个；更早的图换成文字（A 看不了）
        e.say(sid, "谢谢")
        self.assertEqual((len(a.chats()), len(b.chats())), (2, 1))
        self.assertFalse(capability.has_images(a.chats()[-1]["messages"]))
        self.assertIn("[图片已省略]", json.dumps(a.chats()[-1]["messages"], ensure_ascii=False))
        st = e.rt.session_status(sid)
        self.assertEqual(st["endpoint"], "GX10-A")

    def test_sticky_within_run(self):
        a = self.fake("GX10-A")
        b = self.fake("GX10-B", vision=True, max_len=262144)
        e = self.renv([a.endpoint(), b.endpoint(context_window=262144)])
        lv_llm = e.rt.llm_factory()
        lv_llm.begin_run()
        lv_llm.chat([{"role": "user", "content": [{"type": "text", "text": "图"}, {"type": "image_url", "image_url": {"url": IMG}}]}])
        lv_llm.chat([{"role": "user", "content": "纯文字"}])
        self.assertEqual((len(a.chats()), len(b.chats())), (0, 2))       # 只往上换、不来回切
        self.assertEqual(lv_llm.take_note(), "这一步需要看图，已改用 GX10-B。")
        lv_llm.begin_run()
        lv_llm.chat([{"role": "user", "content": "纯文字"}])
        self.assertEqual(len(a.chats()), 1)

    def test_budget_follows_endpoint(self):
        a = self.fake("GX10-A")
        b = self.fake("GX10-B", vision=True, max_len=262144)
        e = self.renv([a.endpoint(context_window=8192), b.endpoint(context_window=262144)])
        c = e.rt.llm_factory()
        c.begin_run()
        big = [{"role": "user", "content": "字" * 6000}]
        self.assertEqual(c.plan(big)["name"], "GX10-B")                 # 装不下就换上下文更大的
        self.assertEqual(c.plan([{"role": "user", "content": "短"}])["name"], "GX10-A")
        self.assertEqual(context.input_budget(e.cfg, c.plan(big)), 262144 - 4096 - 1024)
        sid = e.session()
        e.say(sid, "字" * 6000)
        self.assertEqual(len(b.chats()), 1)
        self.assertEqual(e.rt.store.events(sid, types=("summary",)), [])  # 换到大模型，不用压缩
        notes = [x["payload"]["text"] for x in e.rt.store.events(sid, types=("notice",))]
        self.assertEqual(notes, ["这次的内容较多，已改用上下文更大的 GX10-B。"])

    def test_vision_down_rejected(self):
        a = self.fake("GX10-A")
        b = self.fake("GX10-B", vision=True, max_len=262144)
        e = self.renv([a.endpoint(), b.endpoint()])
        b.down = True
        capability.get(e.rt).probe_all()
        from agent.server import make_server
        import http.client
        srv = make_server(e.rt, 0)
        srv.start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
            body = json.dumps({"workspace_id": e.ws["id"], "text": "看图", "images": [IMG]}).encode()
            c.request("POST", "/api/sessions", body, {"Host": "127.0.0.1:%d" % srv.port, "X-Token": srv.token,
                                                       "Content-Type": "application/json"})
            r = c.getresponse()
            self.assertEqual(r.status, 400)
            self.assertEqual(json.loads(r.read())["error"], "接入的模型都不支持看图，无法使用「看图片附件」。")
        finally:
            srv.stop()
        # 不经接口送进来的带图消息：不发请求，直接用原话
        sid = e.session()
        ev = e.say(sid, "看图", [IMG])
        self.assertEqual(len(a.chats()) + len(b.chats()), 0)
        self.assertTrue([x for x in ev if x["type"] == "assistant_message"][-1]["payload"]["content"].startswith(
            "接入的模型都不支持看图"))

    def test_failover_only_qualified(self):
        """带图的请求：能看图的那个出错，不会换到看不了图的端点。"""
        a = self.fake("GX10-A")
        b = self.fake("GX10-B", vision=True, max_len=262144, fail_chat=100)
        c = self.fake("GX10-C")
        e = self.renv([a.endpoint(), b.endpoint(), c.endpoint()])
        cl = e.rt.llm_factory()
        cl.begin_run()
        from agent.llm import LLMError
        with self.assertRaises(LLMError):
            cl.chat([{"role": "user", "content": [{"type": "text", "text": "图"}, {"type": "image_url", "image_url": {"url": IMG}}]}])
        self.assertEqual((len(a.chats()), len(c.chats())), (0, 0))
        # 纯文字请求：A 出错时换到 C
        a.fail_chat = 100
        r = cl.chat([{"role": "user", "content": "你好"}])
        self.assertEqual(r.content, "来自GX10-C的回答")

    def test_runtime_correction(self):
        """探测说能看图，真发图时服务回 4xx：改记成否，发 notice，用原话回答。"""
        a = self.fake("GX10-A", vision=True, vision_lies=True)
        e = self.renv([a.endpoint()])
        self.assertTrue(capability.get(e.rt).models()[0]["vision"])
        sid = e.session()
        ev = e.say(sid, "看图", [IMG])
        self.assertIs(capability.get(e.rt).models()[0]["vision"], False)
        notes = [x["payload"]["text"] for x in ev if x["type"] == "notice"]
        self.assertIn("模型「GX10-A」实际不支持看图，已记下，以后不再把图片发给它。", notes)
        last = [x for x in ev if x["type"] == "assistant_message"][-1]["payload"]["content"]
        self.assertTrue(last.startswith("当前模型不支持看图，无法使用「看图片附件」。"))
        self.assertEqual(e.rt.store.session(sid)["status"], "done")


if __name__ == "__main__":
    unittest.main()
