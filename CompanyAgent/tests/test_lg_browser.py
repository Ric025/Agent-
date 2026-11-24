"""新内核（LangChain / LangGraph）上的操作网页、能力开关（docs/操控电脑.md）。没装 LangChain / LangGraph 时整个文件跳过；
另外要 playwright 的几条在没装 playwright 时跳过。依赖的接口和假设见 docs/内核迁移说明.md 第 10 节。"""
from __future__ import annotations
import importlib.util, json, re, time, unittest

from helpers import Case, wait_until, HAS_LG, LG_SKIP
from agent import capability, pageview, context, desktop

HAS_PW = importlib.util.find_spec("playwright") is not None
IMG = capability.data_url(capability.red_png())


def call(name, args):
    return {"content": "", "tool_calls": [(name, args)]}


def ref(messages, label):
    c = messages[-1]["content"] if isinstance(messages[-1]["content"], str) else ""
    m = re.search(r'\w+ "%s" \[ref=(\w+)\]' % re.escape(label), c)
    return m.group(1) if m else "e999"


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LGCaps(Case):
    def test_prompt_and_tools(self):
        seen = []

        def cap(m, t):
            seen.append((m, t))
            return {"content": "好"}
        e = self.lg_env([cap], model={"caps": {"tools": True}})
        sid = e.start("你好")
        self.assertTrue(e.rt.wait_idle(sid, 20))
        msgs, tools = seen[0]
        sysmsg = json.dumps(msgs[0], ensure_ascii=False)
        self.assertIn("当前模型不支持看图，无法使用「看图片附件」。", sysmsg)
        names = {(t.get("function") or {}).get("name") for t in tools or []}
        self.assertEqual(capability.get(e.rt).ok("browser"), "browser_open" in names)
        self.assertNotIn("desktop_open", names)

    def test_image_without_vision(self):
        e = self.lg_env([{"content": "不该被调用"}])
        sid = e.new_session()
        e.rt.send_message(sid, "看图", [IMG])
        self.assertTrue(e.rt.wait_idle(sid, 20))
        self.assertEqual(e.mock.calls, [])
        last = e.events(sid, ("assistant_message",))[-1]["payload"]["content"]
        self.assertTrue(last.startswith("当前模型不支持看图，无法使用「看图片附件」。"))

    def test_only_latest_page_result(self):
        """中间件在发给模型之前替换更早的浏览器结果，检查点里的消息不动。"""
        from agent.tools import tool, S, REGISTRY
        body = lambda i: "看了页面。\n" + pageview.wrap(["页面：第%d次" % i, "网址：u"], ["- x"] * 30)   # noqa: E731
        script = [call("browser_look", {})] * 5 + [{"content": "好"}]
        e = self.lg_env(script, mode="full")
        orig = REGISTRY["browser_look"].handler
        n = {"i": 0}

        def fake(ctx, **kw):
            n["i"] += 1
            return body(n["i"])
        REGISTRY["browser_look"].handler = fake
        try:
            sid = e.start("看五次")
            self.assertTrue(e.rt.wait_idle(sid, 30))
        finally:
            REGISTRY["browser_look"].handler = orig
        if not capability.get(e.rt).ok("browser"):
            self.skipTest("这台电脑上操作网页不可用")
        last = e.mock.calls[-1]
        full = [m for m in last if m["role"] == "tool" and pageview.DATA_BEGIN in (m["content"] or "")]
        self.assertEqual(len(full), 1)
        self.assertIn("第5次", full[0]["content"])
        self.assertEqual(sum(1 for m in last if m["role"] == "tool" and pageview.OMIT_TAIL in (m["content"] or "")), 4)


@unittest.skipUnless(HAS_LG and HAS_PW, LG_SKIP + "；或没有 playwright")
class LGBrowser(Case):
    @classmethod
    def setUpClass(cls):
        from websites import Site
        cls.site = Site()

    @classmethod
    def tearDownClass(cls):
        cls.site.close()

    def test_full_round(self):
        script = [call("browser_open", {"url": self.site.url("/form")}),
                  lambda m, t: call("browser_do", {"action": "type", "ref": ref(m, "事由"), "text": "出差"}),
                  lambda m, t: call("browser_do", {"action": "click", "ref": ref(m, "提交审批")}),
                  {"content": "提交了"}]
        e = self.lg_env(script, mode="auto", browser={"allow_local": True, "headless": True, "allowed_domains": ["127.0.0.1"]})
        sid = e.start("填报销")
        self.assertTrue(wait_until(lambda: e.rt.store.actions(sid, states=("awaiting",)), 60))
        a = e.rt.store.actions(sid, states=("awaiting",))[0]
        self.assertEqual(a["risk"], "submit")
        req = e.events(sid, ("approval_requested",))[-1]["payload"]
        self.assertEqual(req["display"]["title"], "点击按钮「提交审批」")
        e.rt.decide(a["id"], True)
        self.assertTrue(e.wait_status(sid, "done", timeout=60))
        self.assertIn("提交成功", e.results(sid)[-1]["content"])
        self.assertTrue(all("display" in x["payload"] for x in e.events(sid, ("tool_started",))
                            if x["payload"]["tool"].startswith("browser_")))
        e.rt.close_browser()


if __name__ == "__main__":
    unittest.main()
