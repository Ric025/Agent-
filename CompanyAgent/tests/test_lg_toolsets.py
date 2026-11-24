"""新内核的工具按需加载和重复调用检测（docs/借鉴开源项目.md 3.7、3.8）。从 test_toolsets.py 移植，没装 LangChain / LangGraph 时跳过。

新内核把全部工具登记给工具节点，在 wrap_model_call 里按带上的组过滤这次给模型看的（kernel.filter_tools）。
假模型 ScriptedChatModel 在 bind_tools 时记下工具，所以脚本函数收到的 tools 就是过滤后的那些——前提是
create_agent 每次调模型都按 request.tools 重新 bind_tools（没验证的假设，见笔记）。"""
from __future__ import annotations
import json, unittest
from helpers import Case, HAS_LG, LG_SKIP
from agent import toolsets
from agent.toolsets import GUARD_TEXT, TOOL_LOAD


def tc(*calls, content=""):
    return {"content": content, "tool_calls": list(calls)}


def names(tools):
    return [t["function"]["name"] for t in tools or []]


def capture(seen, reply):
    def fn(messages, tools):
        seen.append(names(tools))
        return reply
    return fn


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LGLazy(Case):
    def test_filtered_and_autoload(self):
        seen = []
        e = self.lg_env([capture(seen, tc(("session_search", {"query": "周报"}))), capture(seen, {"content": "查了"})])
        sid = e.start("帮我看看")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertEqual(e.status(sid), "done")
        self.assertIn(TOOL_LOAD, seen[0])
        self.assertIn("write_todos", seen[0])                       # 不是我们的工具，原样保留
        self.assertNotIn("session_search", seen[0])
        self.assertNotIn("run_python", seen[0])
        r = e.results(sid)[0]
        self.assertTrue(r["ok"], r)                                 # 直接调用没带的工具：加载并执行
        self.assertIn("session_search", seen[1])
        self.assertEqual(e.rt.store.session(sid)["meta"]["toolset"], ["memory"])

    def test_tool_load(self):
        seen = []
        e = self.lg_env([capture(seen, tc(("tool_load", {"group": "code"}))), capture(seen, {"content": "好"})], mode="manual")
        sid = e.start("x")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertEqual(e.status(sid), "done")
        self.assertFalse(e.events(sid, ("approval_requested",)))
        self.assertEqual(e.results(sid)[0]["content"], "已加载：运行代码（run_python）")
        self.assertNotIn("run_python", seen[0])
        self.assertIn("run_python", seen[1])
        st = [x["payload"] for x in e.events(sid, ("tool_started",)) if x["payload"]["tool"] == TOOL_LOAD][0]
        self.assertEqual(st["display"]["title"], "加载工具：运行代码")

    def test_off(self):
        seen = []
        e = self.lg_env([capture(seen, {"content": "好"})], tools={"lazy": "off"})
        sid = e.start("x")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertNotIn(TOOL_LOAD, seen[0])
        self.assertIn("run_python", seen[0])

    def test_signal(self):
        seen = []
        e = self.lg_env([capture(seen, {"content": "好"})])
        sid = e.start("算一下 销售.xlsx 的合计")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertIn("run_python", seen[0])
        self.assertIn("excel_read", seen[0])


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LGGuard(Case):
    def test_block_then_stop(self):
        same = tc(("list_dir", {"path": "."}))
        e = self.lg_env([same, same, same, same, {"content": "好"}])
        sid = e.start("看看")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertEqual(e.status(sid), "paused")
        self.assertEqual(e.rt.store.session(sid)["meta"].get("reason"), "重复调用")
        r = e.results(sid)
        self.assertEqual([x["ok"] for x in r], [True, True, False, False])
        self.assertEqual(r[2]["content"], GUARD_TEXT)
        lg = [x["payload"] for x in e.events(sid, ("loop_guard",))]
        self.assertEqual([(x["tool"], x["count"]) for x in lg], [("list_dir", 3), ("list_dir", 3)])
        self.assertIn(toolsets.GUARD_STOP_TEXT, [x["payload"]["text"] for x in e.events(sid, ("notice",))])
        e.rt.control(sid, "resume")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertEqual(e.status(sid), "done")
        self.assertIn(GUARD_TEXT, json.dumps(e.mock.calls[-1], ensure_ascii=False))

    def test_off(self):
        same = tc(("list_dir", {"path": "."}))
        e = self.lg_env([same] * 4 + [{"content": "好"}], tools={"loop_guard": 0})
        sid = e.start("x")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertEqual(e.status(sid), "done")
        self.assertFalse(e.events(sid, ("loop_guard",)))


if __name__ == "__main__":
    unittest.main()
