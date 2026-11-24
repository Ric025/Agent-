"""新内核（LangChain / LangGraph）上的记忆、历史检索、技能自学习：对应 test_memory、test_history、
test_review、test_skills 里和内核有关的部分。没装 LangChain / LangGraph 时整个文件跳过。"""
from __future__ import annotations
import json, time, unittest
from helpers import Case, wait_until, HAS_LG, LG_SKIP
from agent.llm import MockLLM
from agent import memory as mem


def tc(*calls, content=""):
    return {"content": content, "tool_calls": list(calls)}


SAVE = tc(("skill_save", {"name": "monthly-reconcile", "description": "每月对账", "body": "## 步骤\n1. 比对"}))


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LGMemoryTool(Case):
    def test_add_replace_remove(self):
        M = lambda **a: ("memory", a)          # noqa: E731
        e = self.lg_env([tc(M(action="add", target="user", content="财务总监，负责华东区")),
                         tc(M(action="replace", target="user", old_text="华东", content="财务总监，负责华南区")),
                         tc(M(action="remove", target="user", old_text="不存在")),
                         {"content": "记好了"}])
        sid = e.start("记住我是财务总监")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertEqual(e.status(sid), "done")
        self.assertEqual([r["ok"] for r in e.results(sid)], [True, True, False])
        self.assertEqual(json.loads(e.results(sid)[0]["content"])["entries"], ["财务总监，负责华东区"])
        self.assertEqual([x["payload"]["op"] for x in e.events(sid, ("memory",))], ["add", "replace"])
        self.assertEqual([x["text"] for x in mem.entries(e.rt.store, e.ws["id"], "user")], ["财务总监，负责华南区"])

    def test_snapshot_in_system_prompt(self):
        e = self.lg_env([{"content": "好"}])
        mem.add(e.rt.store, e.cfg, e.ws["id"], "user", "报告先给结论", "user")
        sid = e.start("你好")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertIn("- 报告先给结论", json.dumps(e.mock.calls[0][0], ensure_ascii=False))

    def test_session_search(self):
        e = self.lg_env([tc(("session_search", {"query": "差旅标准"})), {"content": "找到了"}])
        old = e.rt.store.add_session(e.ws["id"], "旧")["id"]
        e.rt.emit(old, "user_message", {"text": "去年的差旅标准"})
        sid = e.start("上次的差旅标准是什么")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertEqual(json.loads(e.results(sid)[0]["content"])["hits"][0]["session_id"], old)


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LGSkillSave(Case):
    def test_asks_in_every_mode(self):
        for mode in ("manual", "auto", "full"):
            with self.subTest(mode=mode):
                e = self.lg_env([SAVE, {"content": "存好了"}], mode=mode)
                sid = e.start("存成技能")
                self.assertTrue(e.wait_status(sid, "waiting_approval", timeout=10))
                req = e.events(sid, ("approval_requested",))[0]["payload"]
                self.assertEqual((req["tool"], req["risk"]), ("skill_save", "skill"))
                e.rt.decide(req["action_id"], True)
                self.assertTrue(e.wait_status(sid, "done", timeout=10))
                self.assertEqual(e.rt.skills.get("monthly-reconcile")["source"], "user")
                self.assertTrue(e.events(sid, ("skill",)))

    def test_always_allow(self):
        e = self.lg_env([SAVE, {"content": "存好了"}])
        e.rt.store.set_tool_setting("skill_save", "allow")
        sid = e.start("存成技能")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertEqual(e.status(sid), "done")
        self.assertFalse(e.events(sid, ("approval_requested",)))

    def test_suggestion_event(self):
        reads = [tc(("list_dir", {"path": "."})) for _ in range(3)]
        e = self.lg_env(reads + [{"content": "做完了"}], skills={"suggest_after": 3}, tools={"loop_guard": 0})
        sid = e.start("整理一下")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertEqual([x["payload"]["kind"] for x in e.events(sid, ("suggestion",))], ["save_skill"])


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LGReview(Case):
    def test_run_review(self):
        rv = MockLLM([tc(("memory", {"action": "add", "target": "user", "content": "报告先给结论"})), {"content": "无"}])
        e = self.lg_env([], review=rv)
        s = e.rt.store.add_session(e.ws["id"], "任务")["id"]
        e.rt.emit(s, "user_message", {"text": "以后报告先给结论"})
        e.rt.emit(s, "assistant_message", {"content": "好的", "tool_calls": []})
        self.assertEqual(e.rt.reviewer.run_once(s), "done")
        self.assertEqual([x["text"] for x in mem.entries(e.rt.store, e.ws["id"], "user")], ["报告先给结论"])
        self.assertEqual(e.events(s, ("memory",))[0]["payload"]["origin"], "review")
        self.assertGreater(e.rt.store.session(s)["meta"]["reviewed_seq"], 0)

    def test_review_rounds_limited(self):
        many = [tc(("memory", {"action": "add", "target": "user", "content": "第%d条不同的偏好" % i})) for i in range(8)]
        rv = MockLLM(many)
        e = self.lg_env([], review=rv)
        s = e.rt.store.add_session(e.ws["id"], "任务")["id"]
        e.rt.emit(s, "user_message", {"text": "一句话"})
        e.rt.reviewer.run_once(s)
        self.assertLessEqual(len(rv.calls), 4)

    def test_review_failure_counts(self):
        rv = MockLLM([{"error": "连不上"}] * 3)
        e = self.lg_env([], review=rv)
        s = e.rt.store.add_session(e.ws["id"], "任务")["id"]
        e.rt.emit(s, "user_message", {"text": "一句话"})
        self.assertEqual([e.rt.reviewer.run_once(s) for _ in range(3)], ["failed", "failed", "gave_up"])


if __name__ == "__main__":
    unittest.main()
