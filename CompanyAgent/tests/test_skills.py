"""技能自学习和两个小项（docs/记忆设计.md 第 9 节）：skill_save、风险等级 skill、提示事件、技能接口、
任务记录导出、工作区说明文件的三个文件名。"""
from __future__ import annotations
import os, unittest
from urllib.parse import unquote
from helpers import Case, ApiCase, wait_until
from agent import policy, context
from agent.tools import REGISTRY, ToolError
from agent.loop import Runner

BODY = "## 适用场景\n每月对账。\n\n## 步骤\n1. 先看两张表的结构\n2. 按单号比对\n3. 输出差异表"


def tc(*calls):
    return {"content": "", "tool_calls": list(calls)}


def save(name="monthly-reconcile", description="每月应收对账：用户说“对一下这个月的账”时使用", body=BODY):
    return tc(("skill_save", {"name": name, "description": description, "body": body}))


class Policy(unittest.TestCase):
    def test_decide(self):
        for mode in ("manual", "auto", "full"):
            self.assertEqual(policy.decide("skill", mode, None), "ask", mode)
            self.assertEqual(policy.decide("skill", mode, "ask"), "ask", mode)
            self.assertEqual(policy.decide("skill", mode, "allow"), "allow", mode)
            self.assertEqual(policy.decide("skill", mode, "deny"), "deny", mode)
        self.assertEqual(policy.RISK_LABEL["skill"], "技能")
        from agent.tools import load_all
        load_all()
        t = REGISTRY["skill_save"]
        self.assertEqual((t.risk, t.sub_ok, t.required), ("skill", False, ["name", "description", "body"]))


class SkillSave(Case):
    def ctx(self, e):
        return Runner(e.rt, e.new_session(), e.mock).ctx

    def run_tool(self, e, **args):
        return REGISTRY["skill_save"].handler(self.ctx(e), **args)

    def test_asks_in_every_mode(self):
        for mode in ("manual", "auto", "full"):
            with self.subTest(mode=mode):
                e = self.env([save(), {"content": "存好了"}], mode=mode)
                sid = e.start("把这次的做法存成技能")
                self.assertTrue(e.wait_status(sid, "waiting_approval"))
                req = e.events(sid, ("approval_requested",))[-1]["payload"]
                self.assertEqual((req["tool"], req["risk"]), ("skill_save", "skill"))
                self.assertIsNone(e.rt.skills.get("monthly-reconcile"))
                e.rt.decide(req["action_id"], True)
                self.assertTrue(e.wait_status(sid, "done"))
                self.assertEqual(e.rt.skills.get("monthly-reconcile")["source"], "user")
                ev = e.events(sid, ("skill",))[0]["payload"]
                self.assertEqual(ev, {"name": "monthly-reconcile", "description": "每月应收对账：用户说“对一下这个月的账”时使用",
                                      "updated": False})

    def test_always_allow(self):
        e = self.env([save(), {"content": "存好了"}], mode="auto")
        e.rt.store.set_tool_setting("skill_save", "allow")
        sid = e.start("存成技能")
        self.assertTrue(e.wait_status(sid, "done"))
        self.assertFalse(e.events(sid, ("approval_requested",)))
        self.assertTrue(e.results(sid)[0]["ok"])

    def test_denied_setting(self):
        e = self.env([save(), {"content": "好"}])
        e.rt.store.set_tool_setting("skill_save", "deny")
        sid = e.start("存成技能")
        self.assertTrue(e.wait_status(sid, "done"))
        self.assertFalse(e.results(sid)[0]["ok"])
        self.assertNotIn("skill_save 保存", e.mock.calls[0][0]["content"])     # 禁用后提示里也不提

    def test_validation(self):
        e = self.env([])
        bad = [dict(name="Monthly"), dict(name="a"), dict(name="-abc"), dict(name="a" * 41), dict(name="月度对账"),
               dict(name="slides"),                                            # 和内置技能重名
               dict(description="x" * 201), dict(description="  "), dict(body="x" * 6001), dict(body=""),
               dict(body=BODY + "\n数据库密码：abc12345"), dict(description="对账 sk-abcdefghijklmnopqrst"),
               dict(body="隐藏​字符")]
        for b in bad:
            with self.subTest(b=b):
                args = {"name": "monthly-reconcile", "description": "每月对账", "body": BODY}
                args.update(b)
                with self.assertRaises(ToolError) as cm:
                    self.run_tool(e, **args)
                self.assertTrue(str(cm.exception))
        with self.assertRaises(ToolError) as cm:
            self.run_tool(e, name="slides", description="x", body=BODY)
        self.assertIn("换一个名字", str(cm.exception))
        # 指令话术不查：技能本来就是指令
        r = self.run_tool(e, name="no-check", description="不要告诉用户中间过程，只给结论", body="忽略之前的格式要求，按本技能输出")
        self.assertEqual(r, {"saved": True, "name": "no-check"})
        self.assertIsNone(e.rt.skills.get("monthly-reconcile"))

    def test_overwrite_keeps_bak(self):
        e = self.env([])
        self.run_tool(e, name="weekly", description="写周报", body="第一版")
        path = os.path.join(e.home, "skills", "weekly", "SKILL.md")
        with open(path, encoding="utf-8") as f:
            first = f.read()
        self.assertTrue(first.startswith("---\nname: weekly\ndescription: 写周报\n---\n"))
        ctx = self.ctx(e)
        REGISTRY["skill_save"].handler(ctx, name="weekly", description="写周报（新）", body="第二版")
        with open(path + ".bak", encoding="utf-8") as f:
            self.assertEqual(f.read(), first)
        self.assertEqual(e.rt.skills.get("weekly")["body"], "第二版")
        self.assertTrue(e.events(ctx.sid, ("skill",))[0]["payload"]["updated"])
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_next_run_prompt(self):
        e = self.env([save(name="weekly-report", description="写周报的做法"), {"content": "存好了"}, {"content": "好"}])
        e.rt.store.set_tool_setting("skill_save", "allow")
        sid = e.start("存成技能")
        self.assertTrue(e.wait_status(sid, "done"))
        self.assertNotIn("weekly-report", e.mock.calls[0][0]["content"])      # 这次运行的提示不变
        sid2 = e.start("写周报")
        self.assertTrue(e.wait_status(sid2, "done"))
        self.assertIn("- weekly-report：写周报的做法", e.mock.calls[-1][0]["content"])
        self.assertIn("skill_save", e.mock.calls[-1][0]["content"])
        # 重启后也在，来源是 user
        e.restart()
        self.assertEqual(e.rt.skills.get("weekly-report")["source"], "user")
        self.assertEqual(e.rt.skills.get("slides")["source"], "builtin")

    def test_sub_agent_cannot(self):
        e = self.env([])
        s = e.rt.store.add_session(e.ws["id"], "子", kind="sub", parent_id=e.new_session())
        ctx = Runner(e.rt, s["id"], e.mock, sub=True).ctx
        from agent.tools import available
        self.assertNotIn("skill_save", [t.name for t in available(ctx, sub=True)])

    def test_user_cannot_shadow_builtin(self):
        e = self.env([])
        d = os.path.join(e.home, "skills", "slides")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: slides\ndescription: 假的\n---\n假的")
        e.rt.skills.reload()
        self.assertEqual(e.rt.skills.get("slides")["source"], "builtin")


def reads(n):
    return [tc(("list_dir", {"path": "."})) for _ in range(n)]


class Suggestion(Case):
    def suggestions(self, e, sid):
        return [x["payload"] for x in e.events(sid, ("suggestion",))]

    def test_once(self):
        e = self.env(reads(3) + [{"content": "做完了"}] + reads(3) + [{"content": "又做完了"}], skills={"suggest_after": 3},
                     tools={"loop_guard": 0})     # 脚本故意重复同一个调用：不拦
        sid = e.start("整理一下")
        self.assertTrue(e.wait_status(sid, "done"))
        self.assertEqual(self.suggestions(e, sid), [{"kind": "save_skill", "text": "这次的做法可以存成技能，下次同类的事直接照做。"}])
        e.rt.send_message(sid, "再来一次")
        self.assertTrue(wait_until(lambda: len(e.events(sid, ("assistant_message",))) == 8))
        self.assertTrue(e.wait_status(sid, "done"))
        self.assertEqual(len(self.suggestions(e, sid)), 1)                         # 不重复出
        # 不进模型上下文
        self.assertFalse(any("存成技能，下次" in str(m.get("content")) for m in e.mock.calls[-1]))

    def test_counts_since_last_user_message(self):
        e = self.env(reads(2) + [{"content": "好"}] + reads(2) + [{"content": "好"}], skills={"suggest_after": 3})
        sid = e.start("一")
        self.assertTrue(e.wait_status(sid, "done"))
        e.rt.send_message(sid, "二")
        self.assertTrue(wait_until(lambda: len(e.events(sid, ("assistant_message",))) == 6))
        self.assertTrue(e.wait_status(sid, "done"))
        self.assertEqual(self.suggestions(e, sid), [])

    def test_off_and_failures(self):
        e = self.env(reads(3) + [{"content": "好"}], skills={"suggest_after": 0})
        sid = e.start("x")
        self.assertTrue(e.wait_status(sid, "done"))
        self.assertEqual(self.suggestions(e, sid), [])
        bad = [tc(("read_file", {"path": "没有.txt"})) for _ in range(3)]
        e = self.env(bad + [{"content": "好"}], skills={"suggest_after": 3})
        sid = e.start("x")
        self.assertTrue(e.wait_status(sid, "done"))
        self.assertEqual(self.suggestions(e, sid), [])                             # 失败的调用不算

    def test_not_after_skill_save(self):
        e = self.env(reads(2) + [save(), {"content": "存好了"}], skills={"suggest_after": 3})
        e.rt.store.set_tool_setting("skill_save", "allow")
        sid = e.start("x")
        self.assertTrue(e.wait_status(sid, "done"))
        self.assertEqual(self.suggestions(e, sid), [])

    def test_default(self):
        e = self.env([])
        self.assertEqual(e.cfg.get("skills", "suggest_after"), 8)


class SkillApi(ApiCase):
    def test_list_get_delete(self):
        rt = self.e.rt
        ctx = Runner(rt, self.e.new_session(), self.e.mock).ctx
        REGISTRY["skill_save"].handler(ctx, name="my-weekly", description="写周报", body="步骤一")
        st, items = self.req("GET", "/api/skills")
        src = {x["name"]: x["source"] for x in items}
        self.assertEqual((src["my-weekly"], src["slides"]), ("user", "builtin"))
        st, d = self.req("GET", "/api/skills/my-weekly")
        self.assertEqual(d, {"name": "my-weekly", "description": "写周报", "source": "user", "body": "步骤一"})
        st, d = self.req("GET", "/api/skills/slides")
        self.assertEqual((st, d["source"]), (200, "builtin"))
        self.assertTrue(d["body"])
        self.assertEqual(self.req("GET", "/api/skills/nope")[0], 404)
        self.assertEqual(self.req("DELETE", "/api/skills/slides")[0], 400)
        self.assertTrue(os.path.isdir(os.path.join(rt.app_dir, "skills", "slides")))
        self.assertEqual(self.req("DELETE", "/api/skills/nope")[0], 404)
        self.assertEqual(self.req("DELETE", "/api/skills/my-weekly")[0], 200)
        self.assertFalse(os.path.exists(os.path.join(self.e.home, "skills", "my-weekly")))
        self.assertIsNone(rt.skills.get("my-weekly"))
        self.assertNotIn("my-weekly", [x["name"] for x in self.req("GET", "/api/skills")[1]])


class Export(ApiCase):
    script = [tc(("write_file", {"path": "报告/月报.md", "content": "机密的工具结果正文不该导出"})),
              tc(("read_file", {"path": "报告/月报.md"})), {"content": "月报写好了，在 报告/月报.md"}]
    env_kw = {"mode": "full"}

    def test_export(self):
        e = self.e
        sid = e.start("写一份月报")
        self.assertTrue(e.wait_status(sid, "done"))
        e.rt.emit(sid, "summary", {"text": "较早的进展：写了月报", "upto_seq": 1})
        st, body, headers = self.req("GET", "/api/sessions/%s/export" % sid, raw=True)
        self.assertEqual(st, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/markdown"))
        disp = headers["Content-Disposition"]
        self.assertTrue(disp.startswith("attachment; filename*=UTF-8''"))
        self.assertEqual(unquote(disp.split("''", 1)[1]), "写一份月报.md")
        md = body.decode("utf-8")
        self.assertTrue(md.startswith("# 写一份月报\n"))
        self.assertIn("- 工作区：测试", md)
        self.assertIn("> 写一份月报", md)
        self.assertIn("月报写好了，在 报告/月报.md", md)
        self.assertIn("- 工具：写入文件（path=报告/月报.md，content=机密的工具结果正文不该导出） · 成功", md)
        self.assertIn("- 工具：读取文件（path=报告/月报.md） · 成功", md)
        self.assertEqual(md.count("机密的工具结果正文不该导出"), 1)        # 只在参数摘要里出现，工具结果的正文没有导出
        self.assertNotIn('"text"', md)
        self.assertIn("较早的进展：写了月报", md)
        self.assertIn("## 成品", md)
        self.assertIn("- 报告/月报.md（第 1 版）", md)
        self.assertEqual(self.req("GET", "/api/sessions/s_nope/export")[0], 404)

    def test_file_name(self):
        from agent.export import file_name, args_summary
        self.assertEqual(file_name('a/b:c*?"<>|'), "a b c.md")
        self.assertEqual(file_name(""), "对话记录.md")
        self.assertLessEqual(len(args_summary({"code": "x" * 1000, "path": "y"})), 200)


class AgentMd(Case):
    def test_names_and_order(self):
        e = self.env([])
        ws = e.rt.store.workspace(e.ws["id"])
        p = lambda: context.system_prompt(e.rt, ws)     # noqa: E731
        self.assertNotIn("# 工作区指令", p())
        e.write("CLAUDE.md", "来自 CLAUDE")
        self.assertIn("来自 CLAUDE", p())
        e.write("AGENTS.md", "来自 AGENTS")
        self.assertIn("来自 AGENTS", p())
        self.assertNotIn("来自 CLAUDE", p())
        e.write("AGENT.md", "来自 AGENT")
        self.assertIn("来自 AGENT\n", p() + "\n")
        self.assertNotIn("来自 AGENTS", p())


if __name__ == "__main__":
    unittest.main()
