"""重启恢复（第 10 节第 7 项）：结果未知、等批准时重启、两步续跑、结果已做完但没记下来。"""
from __future__ import annotations
import os, unittest
from helpers import Case, wait_until
from agent.llm import MockLLM
from agent.loop import UNKNOWN_TEXT


def tc(*calls, content=""):
    return {"content": content, "tool_calls": list(calls)}


class Recovery(Case):
    def _crashed_running(self, e, tool="write_file", args=None, state="running", result=None):
        """直接在账本里造出"执行到一半程序崩溃"的现场。"""
        st = e.rt.store
        sid = st.add_session(e.ws["id"], "崩溃前的任务")["id"]
        st.add_event(sid, "user_message", {"text": "写 r.txt"})
        args = args or {"path": "r.txt", "content": "x"}
        st.add_event(sid, "assistant_message", {"content": "", "tool_calls": [
            {"id": "m1_0", "name": tool, "arguments": args, "raw": "", "error": None}], "usage": {}, "endpoint": "mock"})
        a = st.add_action(sid, 1, "m1_0", tool, args, "create", state=state)
        if result is not None:
            st.set_action(a["id"], state, result)
        st.add_event(sid, "tool_started", {"action_id": a["id"], "call_id": "m1_0", "tool": tool, "args": args})
        st.set_session(sid, status="running")
        return sid, a["id"]

    def test_running_action_becomes_unknown(self):
        e = self.env([])
        sid, aid = self._crashed_running(e)
        rt = e.restart(MockLLM([{"content": "我先确认一下 r.txt 是否存在。"}]))
        self.assertEqual(rt.recover(), [sid])
        s = rt.store.session(sid)
        self.assertEqual((s["status"], s["meta"]["reason"]), ("paused", "程序重启"))
        self.assertTrue(rt.store.events(sid, types=("notice",)))
        # 两步续跑：恢复后只是查看，不会自己跑
        self.assertEqual(e.mock.calls, [])
        self.assertEqual(rt.store.action(aid)["state"], "running")
        rt.control(sid, "resume")
        self.assertTrue(rt.wait_idle(sid, 5))
        self.assertEqual(rt.store.session(sid)["status"], "done")
        self.assertEqual(rt.store.action(aid)["state"], "unknown")
        res = rt.store.events(sid, types=("tool_result",))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["payload"]["content"], UNKNOWN_TEXT)
        self.assertFalse(os.path.exists(os.path.join(e.root, "r.txt")))   # 没有被再执行一次
        self.assertEqual(len(rt.store.events(sid, types=("tool_started",))), 1)
        self.assertEqual(e.mock.calls[0][-1]["role"], "tool")
        self.assertIn("结果未知", e.mock.calls[0][-1]["content"])
        self.assertTrue(rt.store.q("SELECT * FROM audit WHERE action_id=? AND outcome='结果未知'", (aid,)))

    def test_done_but_result_not_written(self):
        """工具做完、动作已记 done，但 tool_result 还没写就崩了：用动作里存的结果补上，不重做。"""
        e = self.env([])
        sid, aid = self._crashed_running(e, state="done", result={"ok": True, "content": "已写入 r.txt", "truncated": False})
        rt = e.restart(MockLLM([{"content": "好了"}]))
        rt.recover()
        rt.control(sid, "resume")
        rt.wait_idle(sid, 5)
        res = rt.store.events(sid, types=("tool_result",))
        self.assertEqual([r["payload"]["content"] for r in res], ["已写入 r.txt"])
        self.assertTrue(res[0]["payload"]["ok"])
        self.assertFalse(os.path.exists(os.path.join(e.root, "r.txt")))

    def test_restart_while_waiting_approval(self):
        e = self.env([tc(("write_file", {"path": "a.txt", "content": "新"}))])
        e.write("a.txt", "旧")
        sid = e.start("改 a.txt")
        self.assertTrue(e.wait_status(sid, "waiting_approval"))
        old_mock = e.mock
        aid = e.rt.store.actions(sid, states=("awaiting",))[0]["id"]
        rt = e.restart(MockLLM([{"content": "改好了"}]))
        self.check_calls(old_mock)
        self.assertEqual(rt.store.session(sid)["status"], "waiting_approval")   # 关闭不改账本
        rt.recover()
        self.assertEqual(rt.store.session(sid)["status"], "paused")
        rt.control(sid, "resume")
        self.assertTrue(wait_until(lambda: rt.store.session(sid)["status"] == "waiting_approval"))
        self.assertEqual([a["id"] for a in rt.store.actions(sid, states=("awaiting",))], [aid])   # 还是同一个批准
        self.assertEqual(len(rt.store.actions(sid)), 1)
        self.assertEqual(len(rt.store.events(sid, types=("approval_requested",))), 1)
        self.assertEqual(e.mock.calls, [])
        self.assertEqual(e.read("a.txt"), "旧")
        rt.decide(aid, True)
        self.assertTrue(rt.wait_idle(sid, 5))
        self.assertEqual(rt.store.session(sid)["status"], "done")
        self.assertEqual(e.read("a.txt"), "新")

    def test_decide_while_paused_then_resume(self):
        e = self.env([tc(("write_file", {"path": "a.txt", "content": "新"}))])
        e.write("a.txt", "旧")
        sid = e.start("改 a.txt")
        self.assertTrue(e.wait_status(sid, "waiting_approval"))
        aid = e.rt.store.actions(sid, states=("awaiting",))[0]["id"]
        rt = e.restart(MockLLM([{"content": "改好了"}]))
        rt.recover()
        rt.decide(aid, True)                      # 暂停时只记录决定
        self.assertEqual(rt.store.session(sid)["status"], "paused")
        self.assertEqual(e.read("a.txt"), "旧")
        rt.control(sid, "resume")
        rt.wait_idle(sid, 5)
        self.assertEqual(e.read("a.txt"), "新")
        self.assertEqual(rt.store.session(sid)["status"], "done")

    def test_restart_while_asking(self):
        e = self.env([tc(("ask_user", {"question": "用哪张表？", "options": ["A", "B"]}))])
        sid = e.start("对账")
        self.assertTrue(e.wait_status(sid, "waiting_input"))
        rt = e.restart(MockLLM([{"content": "用 B"}]))
        rt.recover()
        self.assertEqual(rt.store.session(sid)["status"], "paused")
        rt.send_message(sid, "B")              # 重启后直接回答：记为答案并接着跑
        rt.wait_idle(sid, 5)
        self.assertEqual(rt.store.session(sid)["status"], "done")
        self.assertEqual(len(rt.store.events(sid, types=("question",))), 1)
        self.assertIn("B", e.mock.calls[0][-1]["content"])
        self.assertEqual(rt.store.actions(sid)[0]["state"], "done")

    def test_duplicate_call_ids_after_restart(self):
        """新进程里的假模型会从 m1_0 重新编号：调用 id 必须在任务内去重，否则会认错动作。"""
        e = self.env([tc(("write_file", {"path": "a.txt", "content": "1"})), {"content": "好"}])
        sid = e.start("写")
        e.rt.wait_idle(sid, 5)
        rt = e.restart(MockLLM([tc(("write_file", {"path": "b.txt", "content": "2"})), {"content": "好"}]))
        rt.send_message(sid, "再写一个")
        rt.wait_idle(sid, 5)
        self.assertEqual(e.read("b.txt"), "2")
        ids = [c["id"] for ev in rt.store.events(sid, types=("assistant_message",)) for c in ev["payload"]["tool_calls"]]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
