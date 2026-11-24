"""历史回忆（docs/记忆设计.md 4.2、4.3）：msg_fts 索引、启动回填、session_search 工具、/api/search 的底层检索。"""
from __future__ import annotations
import json, unittest
from helpers import Case
from agent.tools import REGISTRY, ToolError
from agent.tools import history
from agent.loop import Runner
from agent.store import Store


class History(Case):
    def setUp(self):
        super().setUp()
        self.e = self.env([])
        self.rt, self.store = self.e.rt, self.e.rt.store

    def task(self, title, *turns, ws=None, kind="task", parent=None):
        s = self.store.add_session((ws or self.e.ws)["id"], title, kind=kind, parent_id=parent)
        for role, text in turns:
            if role == "u":
                self.rt.emit(s["id"], "user_message", {"text": text})
            elif role == "a":
                self.rt.emit(s["id"], "assistant_message", {"content": text, "tool_calls": []})
            elif role == "t":       # 带工具调用的回复 + 工具结果
                self.rt.emit(s["id"], "assistant_message", {"content": "", "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {}}]})
                self.rt.emit(s["id"], "tool_result", {"call_id": "c1", "tool": "read_file", "ok": True, "content": text})
        return s["id"]

    def call(self, sid, sub=False, **args):
        ctx = Runner(self.rt, sid, self.e.mock, sub=sub).ctx
        return REGISTRY["session_search"].handler(ctx, **args)

    def test_index(self):
        sid = self.task("对账", ("u", "帮我核对十月的应收账款"), ("t", "工具结果里的应收账款明细"), ("a", "十月应收账款核对完了"))
        rows = self.store.q("SELECT role, session_id, seq FROM msg_fts ORDER BY seq")
        self.assertEqual([r["role"] for r in rows], ["user", "assistant"])      # 工具结果、带调用的空回复不进
        sub = self.task("子任务", ("u", "子代理里的应收账款"), kind="sub", parent=sid)
        self.assertFalse(self.store.q("SELECT 1 FROM msg_fts WHERE session_id=?", (sub,)))   # 子任务不进

    def test_backfill(self):
        sid = self.task("旧任务", ("u", "去年的预算编制"), ("a", "预算编制做好了"))
        # 模拟升级前的账本：没有索引、没有标记
        self.store.x("DELETE FROM msg_fts")
        self.store.x("DELETE FROM meta WHERE key='msg_fts_built'")
        home = self.e.home
        self.e.restart()
        st = self.e.rt.store
        self.assertEqual(len(st.q("SELECT 1 FROM msg_fts WHERE session_id=?", (sid,))), 2)
        self.assertTrue(st.meta_get("msg_fts_built"))
        st.x("DELETE FROM msg_fts")              # 有标记就不再回填
        st2 = Store(home)
        self.assertEqual(st2.q("SELECT COUNT(*) AS n FROM msg_fts")[0]["n"], 0)
        st2.close()

    def test_search_words(self):
        old = self.task("十月对账", ("u", "帮我核对十月的应收账款"), ("a", "十月应收账款核对完了，差异 3 笔"))
        self.task("周报", ("u", "写一份周报"), ("a", "周报写好了"))
        cur = self.task("现在的任务", ("u", "上次的应收账款对账结果呢"))
        r = self.call(cur, query="应收账款")
        self.assertEqual([h["session_id"] for h in r["hits"]], [old])          # 不含当前任务
        h = r["hits"][0]
        self.assertEqual((h["title"], len(h["date"])), ("十月对账", 10))
        self.assertEqual([m["role"] for m in h["matches"]], ["用户", "助手"])
        r = self.call(cur, query="应收账款 差异")                               # 多个词都要命中
        self.assertEqual([m["role"] for m in r["hits"][0]["matches"]], ["助手"])
        r = self.call(cur, query="十月 周报")
        self.assertEqual(r, {"hits": [], "note": history.NONE_NOTE})
        r = self.call(cur, query="周报")                                        # 短词（两个字）走 instr
        self.assertEqual(r["hits"][0]["title"], "周报")
        self.assertEqual(self.call(cur, query="3")["hits"][0]["session_id"], old)   # 一个字符

    def test_snippet_and_limits(self):
        long = "开头" + "很长的内容" * 100 + "关键字在这里" + "后面的内容" * 100
        sids = [self.task("任务%d" % i, ("u", long), ("u", "关键字在这里 第二条"), ("u", "关键字在这里 第三条"),
                          ("u", "关键字在这里 第四条")) for i in range(7)]
        cur = self.task("当前", ("u", "查一下"))
        r = self.call(cur, query="关键字在这里")
        self.assertEqual(len(r["hits"]), 5)                                    # 最多 5 个任务
        self.assertEqual([h["session_id"] for h in r["hits"]], list(reversed(sids))[:5])   # 最近更新的在前
        for h in r["hits"]:
            self.assertLessEqual(len(h["matches"]), 3)
        snip = history._snippet(long, ["关键字在这里"])
        self.assertLessEqual(len(snip), 240)
        self.assertIn("关键字在这里", snip)

    def test_workspace_isolation(self):
        other = self.store.add_workspace("别的", self.e.root)
        self.task("别处", ("u", "别的工作区里的报销单"), ws=other)
        mine = self.task("这里", ("u", "这里的报销单"))
        cur = self.task("当前", ("u", "查报销单"))
        self.assertEqual([h["session_id"] for h in self.call(cur, query="报销单")["hits"]], [mine])
        with self.assertRaises(ToolError):
            self.call(cur, session_id=self.store.sessions(other["id"])[0]["id"])

    def test_sub_agent_excludes_parent(self):
        old = self.task("旧", ("u", "去年的报销规定"))
        parent = self.task("主任务", ("u", "按报销规定检查"))
        sub = self.store.add_session(self.e.ws["id"], "子任务", kind="sub", parent_id=parent)["id"]
        r = self.call(sub, sub=True, query="报销规定")
        self.assertEqual([h["session_id"] for h in r["hits"]], [old])

    def test_session_and_around(self):
        turns = [("u" if i % 2 == 0 else "a", "第%d句 %s" % (i, "预算" if i == 6 else "其他")) for i in range(12)]
        old = self.task("预算", *turns)
        self.store.x("INSERT INTO artifacts(id,session_id,workspace_id,path,version,created) VALUES('ar1',?,?,?,1,1)",
                     (old, self.e.ws["id"], "报告/预算.docx"))
        cur = self.task("当前", ("u", "上次的预算"))
        hit = self.call(cur, query="预算")["hits"][0]["matches"][0]
        r = self.call(cur, session_id=old, around=hit["seq"])
        self.assertEqual([m["text"] for m in r["messages"]], ["第%d句 %s" % (i, "预算" if i == 6 else "其他") for i in range(3, 10)])
        o = self.call(cur, session_id=old)
        self.assertEqual((o["first_user_message"], o["last_assistant_message"], o["artifacts"]), ("第0句 其他", "第11句 其他", ["报告/预算.docx"]))
        r = self.call(cur, session_id=old, query="预算")                        # 指定任务时可以查它
        self.assertEqual(len(r["hits"]), 1)
        with self.assertRaises(ToolError):
            self.call(cur, session_id=old, around=99999)
        with self.assertRaises(ToolError):
            self.call(cur, around=3)
        with self.assertRaises(ToolError) as cm:
            self.call(cur)
        self.assertIn("用法", str(cm.exception))
        with self.assertRaises(ToolError):
            self.call(cur, session_id="s_nope")

    def test_deleted_session(self):
        old = self.task("要删的", ("u", "季度奖金方案"))
        cur = self.task("当前", ("u", "查一下"))
        self.assertTrue(self.call(cur, query="季度奖金")["hits"])
        self.rt.delete_session(old)
        self.assertFalse(self.store.q("SELECT 1 FROM msg_fts WHERE session_id=?", (old,)))
        self.assertEqual(self.call(cur, query="季度奖金")["hits"], [])

    def test_through_kernel(self):
        old = self.task("旧", ("u", "去年的差旅标准"))
        e = self.e
        e.mock.script[:] = [{"content": "", "tool_calls": [("session_search", {"query": "差旅标准"})]}, {"content": "找到了"}]
        sid = e.start("上次的差旅标准是什么")
        self.assertTrue(e.wait_status(sid, "done"))
        r = json.loads(e.results(sid)[0]["content"])
        self.assertEqual(r["hits"][0]["session_id"], old)


if __name__ == "__main__":
    unittest.main()
