"""新内核的压缩、子代理、计划清单、连接器：从 test_context.py、test_mcp.py 移植。"""
from __future__ import annotations
import os, sys, unittest
from helpers import Case, HERE, wait_until, HAS_LG, LG_SKIP
from agent import context
from agent.context import COMPRESS_PROMPT


def tc(*calls, content=""):
    return {"content": content, "tool_calls": list(calls)}


def is_summary_call(messages):
    return messages and messages[0].get("role") == "user" and str(messages[0].get("content") or "").startswith(COMPRESS_PROMPT)


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LGCompression(Case):
    def test_compress_when_over_budget(self):
        state = {"n": 0, "summaries": 0}

        def fn(messages, tools):
            if is_summary_call(messages):
                state["summaries"] += 1
                return {"content": "摘要：用户要逐个读 notes.txt；已经读了几次，内容是重复的「季度数据」。"}
            if state["n"] < 8:
                state["n"] += 1
                return tc(("read_file", {"path": "notes.txt"}))
            return {"content": "读完了"}
        e = self.lg_env([fn] * 60, tools={"loop_guard": 0})     # 脚本故意重复同一个调用：不拦
        e.write("notes.txt", "季度数据。" * 120)        # 每次结果约 450 token
        sid = e.new_session()
        from agent.lg.kernel import TaskKernel
        fixed = TaskKernel(e.rt.lg, sid).fixed_tokens()
        # 对话部分只留约 1600 token
        e.rt.cfg.update({"model": {"context_window": fixed + 1600 + 1024 + 512, "max_output": 512}})
        e.rt.send_message(sid, "把 notes.txt 读 8 遍")
        self.assertTrue(e.rt.wait_idle(sid, 20))
        self.assertEqual(e.status(sid), "done")
        sums = e.events(sid, ("summary",))
        self.assertTrue(sums)
        self.assertGreaterEqual(state["summaries"], 1)
        self.assertIn("摘要：用户要逐个读", sums[-1]["payload"]["text"])
        main_calls = [c for c in e.mock.calls if not is_summary_call(c)]
        after = main_calls[-1]
        self.assertIn("摘要：用户要逐个读", after[1]["content"])        # 之后的请求从摘要开始
        self.assertLess(len(after), 2 * 8 + 2)
        for c in main_calls:
            self.assertLess(context.estimate(c), fixed + 1600 + 900 + 600)


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LGSubAgent(Case):
    def test_subagent_readonly_and_reports(self):
        seen = {}

        def main1(messages, tools):
            seen["main"] = [t["function"]["name"] for t in tools]
            return tc(("spawn_agent", {"task": "数一下根目录有几个文件"}))

        def sub1(messages, tools):
            seen["sub"] = [t["function"]["name"] for t in tools]
            seen["sub_system"] = messages[0]["content"]
            return tc(("list_dir", {"path": "."}), ("write_file", {"path": "x.txt", "content": "x"}))

        e = self.lg_env([main1, sub1, {"content": "结论：根目录有 2 个文件（a.txt、b.txt）。"}, {"content": "子代理说有 2 个文件。"}])
        e.write("a.txt", "1")
        e.write("b.txt", "2")
        sid = e.start("调查一下")
        self.assertTrue(e.rt.wait_idle(sid, 15))
        self.assertEqual(e.status(sid), "done")
        from agent.tools import REGISTRY
        for n in seen["sub"]:
            self.assertTrue(REGISTRY[n].sub_ok, n)
        for n in ("write_file", "delete_file", "spawn_agent", "ask_user", "run_python", "write_todos"):
            self.assertNotIn(n, seen["sub"])
        self.assertIn("write_file", seen["main"])
        self.assertIn("write_todos", seen["main"])
        self.assertIn("只读调查子代理", seen["sub_system"])
        res = e.results(sid)
        self.assertEqual(len(res), 1)
        self.assertIn("结论：根目录有 2 个文件", res[0]["content"])
        subs = e.rt.store.sessions(kind="sub")
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["parent_id"], sid)
        self.assertEqual(subs[0]["status"], "done")
        sub_res = e.results(subs[0]["id"])
        self.assertIn("没有这个工具：write_file", sub_res[1]["content"])
        self.assertFalse(os.path.exists(os.path.join(e.root, "x.txt")))
        self.assertFalse(e.rt.store.sessions(kind="task")[1:])

    def test_subagent_step_limit(self):
        e = self.lg_env([tc(("spawn_agent", {"task": "一直查"}))] + [tc(("list_dir", {"path": "."}))] * 3 + [{"content": "好"}],
                        budgets={"sub_agent_steps": 3})
        sid = e.start("查")
        self.assertTrue(e.rt.wait_idle(sid, 15))
        self.assertIn("子代理没有得出结论（步数用尽）", e.results(sid)[0]["content"])


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LGTodo(Case):
    def test_write_todos_becomes_todo_event(self):
        todos = [{"content": "看表", "status": "completed"}, {"content": "对账", "status": "in_progress"},
                 {"content": "写报告", "status": "pending"}]
        e = self.lg_env([tc(("write_todos", {"todos": todos})), {"content": "好"}])
        sid = e.start("对账并写报告")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertEqual(e.status(sid), "done")
        ev = e.events(sid, ("todo",))
        self.assertTrue(ev)
        want = [{"text": "看表", "status": "done"}, {"text": "对账", "status": "doing"}, {"text": "写报告", "status": "pending"}]
        self.assertEqual(ev[-1]["payload"]["items"], want)
        self.assertEqual(e.rt.store.session(sid)["meta"]["todo"], want)
        # 账本和界面沿用旧名字 todo_write（界面据此把计划步骤藏起来）
        self.assertEqual(e.events(sid, ("assistant_message",))[0]["payload"]["tool_calls"][0]["name"], "todo_write")
        self.assertEqual(e.results(sid)[0]["tool"], "todo_write")


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LGConnectors(Case):
    def test_mcp_tool_in_task(self):
        try:
            import langchain_mcp_adapters  # noqa: F401
        except Exception:
            self.skipTest("没有安装 langchain-mcp-adapters")
        fake = [sys.executable, os.path.join(HERE, "fake_mcp_server.py")]
        seen = {}

        def first(messages, tools):
            seen["tools"] = [t["function"]["name"] for t in tools]
            return tc(("mcp_demo_echo", {"text": "查工单 123"}))
        # 关掉按需加载：这里测的是连接器的工具全部交给模型（按需加载见 tests/test_toolsets.py）
        e = self.lg_env([first, {"content": "工单查到了"}],
                        mcp_servers={"demo": {"command": fake}, "broken": {"command": ["/no/such"]}}, tools={"lazy": "off"})
        sid = e.start("查一下工单")
        self.assertTrue(e.wait_status(sid, "waiting_approval", timeout=20))    # 连接器属于对外，自动模式要问
        self.assertIn("mcp_demo_echo", seen["tools"])
        a = e.rt.store.actions(sid, states=("awaiting",))[0]
        self.assertEqual((a["tool"], a["risk"]), ("mcp_demo_echo", "external"))
        e.rt.decide(a["id"], True)
        self.assertTrue(e.rt.wait_idle(sid, 20))
        self.assertEqual(e.status(sid), "done")
        self.assertEqual([r["content"] for r in e.results(sid)], ["回声：查工单 123"])
        notices = [x["payload"]["text"] for x in e.events(sid, ("notice",))]
        self.assertTrue(any("broken" in n and "启动失败" in n for n in notices), notices)


if __name__ == "__main__":
    unittest.main()
