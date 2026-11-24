"""新内核的重启恢复：从 test_recovery.py 移植。

LangGraph 恢复时会把被中断的节点从头再跑一遍。这里验证：执行到一半的动作重跑时判为"结果未知"、不再执行；
已经做完但结果没写进图里的，照抄账本里的结果；等批准、等回答时重启，继续后还在等同一个批准或回答。

"执行到一半程序崩溃"用一个抛 BaseException 的测试工具模拟：它不是 Exception，不会被内核当成工具报错，
会一路冲出 LangGraph 和任务线程，留下 actions 表里一条 running 的动作和停在工具节点上的检查点，
和进程被杀掉时的现场一样。"""
from __future__ import annotations
import os, unittest
from helpers import Case, wait_until, HAS_LG, LG_SKIP
from agent.llm import MockLLM
from agent.loop import UNKNOWN_TEXT


def tc(*calls, content=""):
    return {"content": content, "tool_calls": list(calls)}


class Crash(BaseException):
    """模拟进程在工具执行中途被杀。"""


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LGRecovery(Case):
    def setUp(self):
        super().setUp()
        from agent.tools import tool, REGISTRY
        self.runs = []
        runs = self.runs

        @tool("_crash_test", "测试用：写文件后崩溃", {"path": {"type": "string"}}, risk="create")
        def _crash(ctx, path):
            runs.append(path)
            with open(os.path.join(ctx.root, path), "w", encoding="utf-8") as f:
                f.write("x")
            raise Crash()
        self.addCleanup(REGISTRY.pop, "_crash_test", None)

    def _crash_run(self, e):
        sid = e.start("写 r.txt")
        self.assertTrue(wait_until(lambda: self.runs and e.rt.store.actions(sid) and
                                   e.rt.store.actions(sid)[0]["state"] == "running", 10))
        wait_until(lambda: not (e.rt.lives.get(sid) and e.rt.lives[sid].thread and e.rt.lives[sid].thread.is_alive()), 5)
        return sid, e.rt.store.actions(sid)[0]["id"]

    def test_running_action_becomes_unknown(self):
        e = self.lg_env([tc(("_crash_test", {"path": "r.txt"}))])
        sid, aid = self._crash_run(e)
        self.assertEqual(e.status(sid), "running")
        rt = e.restart(MockLLM([{"content": "我先确认一下 r.txt 是否存在。"}]))
        self.assertEqual(rt.recover(), [sid])
        s = rt.store.session(sid)
        self.assertEqual((s["status"], s["meta"]["reason"]), ("paused", "程序重启"))
        self.assertTrue(rt.store.events(sid, types=("notice",)))
        self.assertEqual(e.mock.calls, [])                  # 打开历史任务不调用模型
        self.assertEqual(rt.store.action(aid)["state"], "running")
        rt.control(sid, "resume")
        self.assertTrue(rt.wait_idle(sid, 10))
        self.assertEqual(rt.store.session(sid)["status"], "done")
        self.assertEqual(rt.store.action(aid)["state"], "unknown")
        res = rt.store.events(sid, types=("tool_result",))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["payload"]["content"], UNKNOWN_TEXT)
        self.assertEqual(self.runs, ["r.txt"])             # LangGraph 重跑了工具节点，但工具没有被再执行一次
        self.assertEqual(len(rt.store.events(sid, types=("tool_started",))), 1)
        self.assertEqual(e.mock.calls[0][-1]["role"], "tool")
        self.assertIn("结果未知", e.mock.calls[0][-1]["content"])
        self.assertTrue(rt.store.q("SELECT * FROM audit WHERE action_id=? AND outcome='结果未知'", (aid,)))

    def test_done_but_result_not_written(self):
        e = self.lg_env([tc(("_crash_test", {"path": "r.txt"}))])
        sid, aid = self._crash_run(e)
        e.rt.store.set_action(aid, "done", {"ok": True, "content": "已写入 r.txt", "truncated": False})
        rt = e.restart(MockLLM([{"content": "好了"}]))
        rt.recover()
        rt.control(sid, "resume")
        self.assertTrue(rt.wait_idle(sid, 10))
        res = rt.store.events(sid, types=("tool_result",))
        self.assertEqual([r["payload"]["content"] for r in res], ["已写入 r.txt"])
        self.assertTrue(res[0]["payload"]["ok"])
        self.assertEqual(self.runs, ["r.txt"])
        self.assertIn("已写入 r.txt", e.mock.calls[0][-1]["content"])

    def test_new_message_after_crash(self):
        """崩溃后不点继续、直接发新消息：执行到一半的那一步记为结果未知，再带上新消息。"""
        e = self.lg_env([tc(("_crash_test", {"path": "r.txt"}))])
        sid, aid = self._crash_run(e)
        rt = e.restart(MockLLM([{"content": "好"}]))
        rt.recover()
        rt.send_message(sid, "先别管了")
        self.assertTrue(rt.wait_idle(sid, 10))
        self.assertEqual(rt.store.action(aid)["state"], "unknown")
        self.assertEqual([m["role"] for m in e.mock.calls[0][-2:]], ["tool", "user"])
        self.assertEqual(self.runs, ["r.txt"])

    def test_restart_while_waiting_approval(self):
        e = self.lg_env([tc(("write_file", {"path": "a.txt", "content": "新"}))])
        e.write("a.txt", "旧")
        sid = e.start("改 a.txt")
        self.assertTrue(e.wait_status(sid, "waiting_approval", timeout=10))
        old_mock = e.mock
        aid = e.rt.store.actions(sid, states=("awaiting",))[0]["id"]
        rt = e.restart(MockLLM([{"content": "改好了"}]))
        self.check_calls(old_mock)
        self.assertEqual(rt.store.session(sid)["status"], "waiting_approval")
        rt.recover()
        self.assertEqual(rt.store.session(sid)["status"], "paused")
        rt.control(sid, "resume")
        self.assertTrue(wait_until(lambda: rt.store.session(sid)["status"] == "waiting_approval", 10))
        self.assertEqual([a["id"] for a in rt.store.actions(sid, states=("awaiting",))], [aid])
        self.assertEqual(len(rt.store.actions(sid)), 1)
        self.assertEqual(len(rt.store.events(sid, types=("approval_requested",))), 1)
        self.assertEqual(e.mock.calls, [])
        self.assertEqual(e.read("a.txt"), "旧")
        rt.decide(aid, True)
        self.assertTrue(rt.wait_idle(sid, 10))
        self.assertEqual(rt.store.session(sid)["status"], "done")
        self.assertEqual(e.read("a.txt"), "新")
        self.assertEqual(len(rt.store.events(sid, types=("approval_requested",))), 1)

    def test_decide_while_paused_then_resume(self):
        e = self.lg_env([tc(("write_file", {"path": "a.txt", "content": "新"}))])
        e.write("a.txt", "旧")
        sid = e.start("改 a.txt")
        self.assertTrue(e.wait_status(sid, "waiting_approval", timeout=10))
        aid = e.rt.store.actions(sid, states=("awaiting",))[0]["id"]
        rt = e.restart(MockLLM([{"content": "改好了"}]))
        rt.recover()
        rt.decide(aid, True)
        self.assertEqual(rt.store.session(sid)["status"], "paused")
        self.assertEqual(e.read("a.txt"), "旧")
        rt.control(sid, "resume")
        self.assertTrue(rt.wait_idle(sid, 10))
        self.assertEqual(e.read("a.txt"), "新")
        self.assertEqual(rt.store.session(sid)["status"], "done")

    def test_restart_while_asking(self):
        e = self.lg_env([tc(("ask_user", {"question": "用哪张表？", "options": ["A", "B"]}))])
        sid = e.start("对账")
        self.assertTrue(e.wait_status(sid, "waiting_input", timeout=10))
        rt = e.restart(MockLLM([{"content": "用 B"}]))
        rt.recover()
        self.assertEqual(rt.store.session(sid)["status"], "paused")
        rt.send_message(sid, "B")
        self.assertTrue(rt.wait_idle(sid, 10))
        self.assertEqual(rt.store.session(sid)["status"], "done")
        self.assertEqual(len(rt.store.events(sid, types=("question",))), 1)
        self.assertIn("B", e.mock.calls[0][-1]["content"])
        self.assertEqual(rt.store.actions(sid)[0]["state"], "done")

    def test_duplicate_call_ids_after_restart(self):
        e = self.lg_env([tc(("write_file", {"path": "a.txt", "content": "1"})), {"content": "好"}])
        sid = e.start("写")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        rt = e.restart(MockLLM([tc(("write_file", {"path": "b.txt", "content": "2"})), {"content": "好"}]))
        rt.send_message(sid, "再写一个")
        self.assertTrue(rt.wait_idle(sid, 10))
        self.assertEqual(e.read("b.txt"), "2")
        ids = [c["id"] for ev in rt.store.events(sid, types=("assistant_message",)) for c in ev["payload"]["tool_calls"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_task_from_builtin_kernel(self):
        """旧内核留下的任务（没有检查点）：继续时把账本投影成消息交给新内核，执行中的动作判为结果未知。"""
        e = self.lg_env([])
        st = e.rt.store
        sid = st.add_session(e.ws["id"], "旧任务")["id"]
        st.add_event(sid, "user_message", {"text": "写 r.txt"})
        args = {"path": "r.txt", "content": "x"}
        st.add_event(sid, "assistant_message", {"content": "", "tool_calls": [
            {"id": "m1_0", "name": "write_file", "arguments": args, "raw": "", "error": None}], "usage": {}, "endpoint": "mock"})
        a = st.add_action(sid, 1, "m1_0", "write_file", args, "create", state="running")
        st.set_session(sid, status="running")
        rt = e.restart(MockLLM([{"content": "我先看看 r.txt 在不在。"}]))
        rt.recover()
        rt.control(sid, "resume")
        self.assertTrue(rt.wait_idle(sid, 10))
        self.assertEqual(rt.store.session(sid)["status"], "done")
        self.assertEqual(rt.store.action(a["id"])["state"], "unknown")
        self.assertFalse(os.path.exists(os.path.join(e.root, "r.txt")))
        self.assertIn("结果未知", e.mock.calls[0][-1]["content"])


if __name__ == "__main__":
    unittest.main()
