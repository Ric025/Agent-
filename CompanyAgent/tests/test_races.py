"""并发与时序：消息在线程将要结束时到达、连发消息、暂停等批准、子代理被暂停、超时、空回复。"""
from __future__ import annotations
import threading, time, unittest
from helpers import Case, wait_until
from agent.llm import MockLLM


def tc(*calls):
    return {"content": "", "tool_calls": list(calls)}


class Races(Case):
    def test_message_while_finishing(self):
        """线程刚拿到最终回复、正要结束时用户又发来消息：这条消息不能丢。"""
        e = self.env([])
        lost = 0
        for i in range(60):
            e.mock.script[:] = [{"content": "第一轮"}, {"content": "第二轮"}]
            sid = e.start("一")
            wait_until(lambda: e.events(sid, ("assistant_message",)), 3, step=0.0005)
            e.rt.send_message(sid, "二")
            self.assertTrue(e.rt.wait_idle(sid, 5))
            evs = [x for x in e.events(sid) if x["type"] in ("user_message", "assistant_message")]
            if evs[-1]["type"] != "assistant_message":
                lost += 1
            self.assertEqual(e.status(sid), "done")
        self.assertEqual(lost, 0)

    def test_message_in_gap_before_end(self):
        """确定性地造出这个时序：线程已决定"完成"、还没拿到锁落状态，此时用户发来消息。"""
        e = self.env([{"content": "第一轮"}, {"content": "第二轮"}])
        rt = e.rt
        orig = rt._end
        entered, proceed = threading.Event(), threading.Event()

        def slow_end(sid, status, reason=None):
            if status == "done" and not entered.is_set():
                entered.set()
                proceed.wait(5)
            return orig(sid, status, reason)
        rt._end = slow_end
        sid = e.start("一")
        self.assertTrue(entered.wait(5))
        self.assertEqual(e.status(sid), "running")
        rt.send_message(sid, "二")
        proceed.set()
        self.assertTrue(rt.wait_idle(sid, 5))
        self.assertEqual([x["payload"]["content"] for x in e.events(sid, ("assistant_message",))], ["第一轮", "第二轮"])
        self.assertEqual(e.status(sid), "done")

    def test_burst_of_messages(self):
        e = self.env([{"content": "回复", "delay": 0.05}] * 40)
        sid = e.start("开始")
        for i in range(8):
            e.rt.send_message(sid, "补充 %d" % i)
            time.sleep(0.01 * (i % 3))
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertEqual(e.status(sid), "done")
        evs = [x for x in e.events(sid) if x["type"] in ("user_message", "assistant_message")]
        self.assertEqual(evs[-1]["type"], "assistant_message")
        self.assertIn("补充 7", str(e.mock.calls[-1][-1]["content"]))

    def test_pause_while_waiting_approval(self):
        e = self.env([tc(("write_file", {"path": "a.txt", "content": "新"})), {"content": "好"}])
        e.write("a.txt", "旧")
        sid = e.start("改")
        self.assertTrue(e.wait_status(sid, "waiting_approval"))
        aid = e.rt.store.actions(sid, states=("awaiting",))[0]["id"]
        e.rt.control(sid, "pause")
        e.rt.wait_idle(sid, 5)
        s = e.rt.store.session(sid)
        self.assertEqual((s["status"], s["meta"]["reason"]), ("paused", "用户暂停"))
        self.assertEqual(e.rt.store.action(aid)["state"], "awaiting")
        e.rt.control(sid, "resume")
        self.assertTrue(e.wait_status(sid, "waiting_approval"))
        self.assertEqual(len(e.events(sid, ("approval_requested",))), 1)
        e.rt.decide(aid, True)
        e.rt.wait_idle(sid, 5)
        self.assertEqual((e.status(sid), e.read("a.txt")), ("done", "新"))

    def test_cancel_during_model_call(self):
        e = self.env([{"content": "慢", "delay": 3}])
        sid = e.start("做")
        self.assertTrue(wait_until(lambda: len(e.mock.calls) == 1))
        t0 = time.time()
        e.rt.control(sid, "cancel")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertLess(time.time() - t0, 1.5)
        self.assertEqual(e.status(sid), "cancelled")
        self.assertFalse(e.events(sid, ("assistant_message",)))
        # 取消后再发消息：同一段对话继续
        e.mock.script[:] = [{"content": "好的"}]
        e.rt.send_message(sid, "还是做吧")
        e.rt.wait_idle(sid, 5)
        self.assertEqual(e.status(sid), "done")

    def test_pause_interrupts_subagent(self):
        e = self.env([tc(("spawn_agent", {"task": "查一下"})), {"content": "慢", "delay": 3}])
        sid = e.start("查")
        self.assertTrue(wait_until(lambda: len(e.mock.calls) == 2))
        e.rt.control(sid, "pause")
        self.assertTrue(e.rt.wait_idle(sid, 3))
        self.assertEqual(e.status(sid), "paused")
        a = e.rt.store.actions(sid)[0]
        self.assertEqual(a["state"], "approved")            # 子代理只读，恢复后重新派
        sub = e.rt.store.sessions(kind="sub")[0]
        self.assertEqual(sub["status"], "paused")
        e.mock.script[:] = [{"content": "结论：没问题"}, {"content": "子代理说没问题"}]
        e.rt.control(sid, "resume")
        e.rt.wait_idle(sid, 5)
        self.assertEqual(e.status(sid), "done")
        self.assertIn("结论：没问题", e.results(sid)[0]["content"])
        self.assertEqual(len(e.results(sid)), 1)

    def test_change_of_mind_stops_subagent(self):
        e = self.env([tc(("spawn_agent", {"task": "查一下"})), {"content": "慢", "delay": 3}, {"content": "按新要求做了"}])
        sid = e.start("查")
        self.assertTrue(wait_until(lambda: len(e.mock.calls) == 2))
        e.rt.send_message(sid, "不用查了，直接写结论")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertEqual(e.status(sid), "done")
        self.assertEqual(e.results(sid)[0]["content"], "未执行：用户更改了要求")
        self.assertEqual(e.rt.store.actions(sid)[0]["state"], "voided")

    def test_time_budget(self):
        e = self.env([tc(("list_dir", {"path": "."}))] * 5, budgets={"max_minutes": 0.002})
        sid = e.start("看")
        e.mock.delay = 0.2
        e.rt.wait_idle(sid, 5)
        s = e.rt.store.session(sid)
        self.assertEqual((s["status"], s["meta"]["reason"]), ("paused", "超时"))

    def test_empty_reply_is_retried(self):
        e = self.env([{"content": ""}, {"content": "  "}, {"content": "真正的回答"}])
        sid = e.start("问")
        e.rt.wait_idle(sid, 5)
        self.assertEqual([x["payload"]["content"] for x in e.events(sid, ("assistant_message",))], ["真正的回答"])

    def test_concurrent_tasks(self):
        e = self.env([], mock=MockLLM([{"content": "", "tool_calls": [("list_dir", {"path": "."})]}, {"content": "完"}] * 20, delay=0.01))
        # 共用一个假模型：脚本按到达顺序弹出，所以只检查每个任务都正常结束
        sids = [e.start("任务 %d" % i) for i in range(6)]
        for sid in sids:
            self.assertTrue(e.rt.wait_idle(sid, 10))
            self.assertIn(e.status(sid), ("done",))


if __name__ == "__main__":
    unittest.main()
