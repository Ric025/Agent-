"""上下文：投影、消息顺序校验、token 估算、压缩（第 10 节第 10 项）。"""
from __future__ import annotations
import unittest
from helpers import Case, wait_until
from agent import context
from agent.context import project, project_events, validate_messages, estimate_text, COMPRESS_PROMPT, SUMMARY_PREFIX
from agent.llm import LLMError, MockLLM


def ev(seq, etype, payload, rev=1):
    return {"seq": seq, "type": etype, "payload": payload, "revision": rev}


def am(seq, *ids, content=""):
    return ev(seq, "assistant_message", {"content": content, "tool_calls": [
        {"id": i, "name": "read_file", "arguments": {"path": "a"}, "raw": "", "error": None} for i in ids]})


def tr(seq, cid, text="结果"):
    return ev(seq, "tool_result", {"call_id": cid, "tool": "read_file", "ok": True, "content": text})


def um(seq, text, images=None):
    p = {"text": text}
    if images:
        p["images"] = images
    return ev(seq, "user_message", p)


class Projection(unittest.TestCase):
    def test_results_follow_their_assistant(self):
        evs = [um(1, "读 a"), am(2, "c1", "c2"), um(3, "顺便看看 b"), tr(4, "c2", "二"), tr(5, "c1", "一"),
               ev(6, "status", {"status": "running"}), am(7, content="好了")]
        msgs = project(evs)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "tool", "tool", "user", "assistant"])
        self.assertEqual([m.get("tool_call_id") for m in msgs[2:4]], ["c1", "c2"])   # 按调用顺序
        self.assertEqual(msgs[2]["content"], "一")
        self.assertIsNone(msgs[1]["content"])
        self.assertEqual(msgs[1]["tool_calls"][0]["function"]["arguments"], '{"path": "a"}')
        self.assertEqual(validate_messages(msgs), [])

    def test_adjacent_users_merge_and_images(self):
        imgs = ["data:image/png;base64,%d" % i for i in range(5)]
        evs = [um(1, "看图", imgs[:3]), um(2, "还有", imgs[3:]), am(3, content="收到")]
        msgs = project(evs)
        self.assertEqual(len(msgs), 2)
        parts = msgs[0]["content"]
        urls = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
        self.assertEqual(urls, imgs[2:])          # 只保留最近 3 张
        self.assertEqual(sum(1 for p in parts if p.get("text") == "[图片已省略]"), 2)

    def test_summary_starts_projection(self):
        evs = [um(1, "老话"), am(2, content="老回答"), um(3, "新话"), ev(4, "summary", {"text": "前情", "upto_seq": 2})]
        msgs = project(evs)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["content"], SUMMARY_PREFIX + "前情\n\n新话")

    def test_missing_result_gets_placeholder(self):
        msgs = project([um(1, "x"), am(2, "c1")])
        self.assertEqual(msgs[-1]["role"], "tool")
        self.assertEqual(validate_messages(msgs), [])

    def test_validator_catches_problems(self):
        good = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "a", "function": {}}, {"id": "b", "function": {}}]},
                {"role": "tool", "tool_call_id": "a", "content": ""}, {"role": "tool", "tool_call_id": "b", "content": ""}]
        self.assertEqual(validate_messages(good), [])
        self.assertTrue(validate_messages(good[:4]))                                         # 少了 b
        self.assertTrue(validate_messages(good[:3] + [good[4], good[3]]))                    # 顺序错
        self.assertTrue(validate_messages(good[:3] + [{"role": "user", "content": "插话"}] + good[3:]))   # 中间插了 user
        self.assertTrue(validate_messages(good + [{"role": "tool", "tool_call_id": "a", "content": ""}]))  # 多余 tool
        self.assertTrue(validate_messages([good[0], {"role": "assistant", "content": "x"}]))   # 首条不是 user
        self.assertTrue(validate_messages([good[1], good[0]]))                               # system 不在最前

    def test_estimate(self):
        self.assertAlmostEqual(estimate_text("中文字"), 2.25)
        self.assertAlmostEqual(estimate_text("abc" * 11), 10.0)
        self.assertEqual(context.input_budget(type("C", (), {"get": lambda s, *k, default=None: {"context_window": 32768, "max_output": 4096}[k[1]]})()),
                         32768 - 4096 - 1024)


class SystemPrompt(Case):
    def test_prompt_parts(self):
        e = self.env([])
        e.rt.store.x("UPDATE workspaces SET instructions=? WHERE id=?", ("金额保留两位小数", e.ws["id"]))
        e.write("AGENT.md", "汇报用表格")
        e.rt.store.x("INSERT INTO memories(id,workspace_id,text,source,created) VALUES('m1',NULL,'老板叫王总','u',1)")
        ws = e.rt.store.workspace(e.ws["id"])
        p = context.system_prompt(e.rt, ws, today="2026-11-20")
        for s in ("工作区：测试", "今天：2026-11-20", "金额保留两位小数", "汇报用表格", "老板叫王总", "你是「Agent」"):
            self.assertIn(s, p)
        self.assertIn("只读调查子代理", context.system_prompt(e.rt, ws, sub=True))


class Compression(Case):
    def _script(self, reads):
        state = {"n": 0, "summaries": 0}

        def fn(messages, tools):
            if messages[0]["content"] == COMPRESS_PROMPT:
                state["summaries"] += 1
                self.assertNotIn("tool_calls", str(messages))
                return {"content": "摘要：用户要逐个读 notes.txt；已经读了几次，内容是重复的「季度数据」。"}
            if state["n"] < reads:
                state["n"] += 1
                return {"content": "", "tool_calls": [("read_file", {"path": "notes.txt"})]}
            return {"content": "读完了"}
        return fn, state

    def test_compress_when_over_budget(self):
        fn, state = self._script(8)
        e = self.env([fn] * 40, tools={"loop_guard": 0})     # 脚本故意重复同一个调用：不拦
        e.write("notes.txt", "季度数据。" * 120)        # 每次结果约 450 token
        sid = e.new_session()
        # 按实际的固定前缀设窗口：对话部分只留约 1600 token
        from agent.loop import Runner
        r = Runner(e.rt, sid, e.mock)
        fixed = context.estimate([{"role": "system", "content": r.system}]) + context.estimate_tools(r.specs)
        e.rt.cfg.update({"model": {"context_window": fixed + 1600 + 1024 + 512, "max_output": 512}})
        e.rt.send_message(sid, "把 notes.txt 读 8 遍")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertEqual(e.status(sid), "done")
        sums = e.events(sid, ("summary",))
        self.assertTrue(sums)
        self.assertGreaterEqual(state["summaries"], 1)
        main_calls = [c for c in e.mock.calls if c[0]["content"] != COMPRESS_PROMPT]
        after = main_calls[-1]
        self.assertTrue(after[1]["content"].startswith(SUMMARY_PREFIX))   # 之后的请求从摘要开始
        self.assertIn("摘要：用户要逐个读", after[1]["content"])
        self.assertLess(len(after), 2 * 8 + 2)
        for c in main_calls:
            self.assertLess(context.estimate(c) + context.estimate_tools(r.specs), fixed + 1600 + 900)
        # 摘要覆盖到的位置之后，不能有"结果在前、调用在后"的断裂
        upto = sums[-1]["payload"]["upto_seq"]
        self.assertTrue(any(x["seq"] == upto for x in e.events(sid)))

    def test_mechanical_fallback(self):
        e = self.env([])
        sid = e.new_session()
        st = e.rt.store
        st.add_event(sid, "user_message", {"text": "做月报"})
        st.add_event(sid, "assistant_message", {"content": "", "tool_calls": [
            {"id": "c1", "name": "write_docx", "arguments": {}, "raw": "", "error": None}]})
        st.add_event(sid, "artifact", {"id": "f1", "path": "月报.docx", "version": 1, "note": ""})
        st.add_event(sid, "tool_result", {"call_id": "c1", "tool": "write_docx", "ok": True, "content": "已生成。" * 300})
        st.add_event(sid, "user_message", {"text": "再加一页"})
        bad = MockLLM([{"error": "连不上", "kind": "unreachable"}])
        text = context.compress(e.rt, sid, bad, 100)
        self.assertIn("做月报", text)
        self.assertIn("write_docx", text)
        self.assertIn("月报.docx", text)
        msgs = project(st.events(sid))
        self.assertEqual(validate_messages(msgs), [])
        self.assertTrue(msgs[0]["content"].startswith(SUMMARY_PREFIX))
        self.assertIn("再加一页", msgs[0]["content"])

    def test_no_cut_inside_open_calls(self):
        e = self.env([])
        sid = e.new_session()
        st = e.rt.store
        st.add_event(sid, "user_message", {"text": "a"})
        st.add_event(sid, "assistant_message", {"content": "", "tool_calls": [
            {"id": "c1", "name": "read_file", "arguments": {}, "raw": "", "error": None}]})
        st.add_event(sid, "user_message", {"text": "插话"})       # 结果还没出来就插话
        st.add_event(sid, "tool_result", {"call_id": "c1", "tool": "read_file", "ok": True, "content": "x" * 3000})
        m = MockLLM([{"content": "摘要"}])
        context.compress(e.rt, sid, m, 50)
        s = e.events(sid, ("summary",))[0]["payload"]
        # 只能切在第一条 assistant 之前（插话处切会让 c1 的结果失去所属调用）
        seqs = [x["seq"] for x in st.events(sid)]
        self.assertEqual(s["upto_seq"], seqs[0])
        self.assertEqual(validate_messages(project(st.events(sid))), [])


class SubAgent(Case):
    def test_subagent_readonly_and_reports(self):
        seen = {}

        def main1(messages, tools):
            seen["main"] = [t["function"]["name"] for t in tools]
            return {"content": "", "tool_calls": [("spawn_agent", {"task": "数一下根目录有几个文件"})]}

        def sub1(messages, tools):
            seen["sub"] = [t["function"]["name"] for t in tools]
            seen["sub_system"] = messages[0]["content"]
            return {"content": "", "tool_calls": [("list_dir", {"path": "."}), ("write_file", {"path": "x.txt", "content": "x"})]}

        e = self.env([main1, sub1, {"content": "结论：根目录有 2 个文件（a.txt、b.txt）。"}, {"content": "子代理说有 2 个文件。"}])
        e.write("a.txt", "1")
        e.write("b.txt", "2")
        sid = e.start("调查一下")
        self.assertTrue(e.rt.wait_idle(sid, 5))
        self.assertEqual(e.status(sid), "done")
        from agent.tools import REGISTRY
        for n in seen["sub"]:
            self.assertTrue(REGISTRY[n].sub_ok, n)
        for n in ("write_file", "delete_file", "spawn_agent", "ask_user", "run_python"):
            self.assertNotIn(n, seen["sub"])
        self.assertIn("write_file", seen["main"])
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
        self.assertFalse(e.rt.store.sessions(kind="task")[1:])    # 子任务不出现在任务列表

    def test_subagent_step_limit(self):
        e = self.env([{"content": "", "tool_calls": [("spawn_agent", {"task": "一直查"})]}]
                     + [{"content": "", "tool_calls": [("list_dir", {"path": "."})]}] * 3 + [{"content": "好"}],
                     budgets={"sub_agent_steps": 3})
        sid = e.start("查")
        e.rt.wait_idle(sid, 5)
        self.assertIn("子代理没有得出结论（步数用尽）", e.results(sid)[0]["content"])


if __name__ == "__main__":
    unittest.main()
