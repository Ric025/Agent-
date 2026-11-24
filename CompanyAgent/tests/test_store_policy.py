"""账本（第 1 项）和路径守卫、批准判定（第 2 项）。"""
from __future__ import annotations
import os, shutil, tempfile, unittest
from helpers import APP  # noqa: F401  (把项目目录放进 sys.path)
from agent.store import Store, digest_of
from agent import policy
from agent.policy import resolve, PathError, decide
from agent.tools import validate


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.st = Store(self.tmp)
        self.ws = self.st.add_workspace("w", self.tmp)

    def tearDown(self):
        self.st.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_events_ordered_and_stamped(self):
        sid = self.st.add_session(self.ws["id"])["id"]
        a = self.st.add_event(sid, "user_message", {"text": "一"})
        rev = self.st.bump_revision(sid)
        self.assertEqual(rev, 2)
        b = self.st.add_event(sid, "user_message", {"text": "二"})
        c = self.st.add_event(sid, "notice", {"text": "三"}, revision=1)
        evs = self.st.events(sid)
        self.assertEqual([e["seq"] for e in evs], sorted([a["seq"], b["seq"], c["seq"]]))
        self.assertEqual([e["revision"] for e in evs], [1, 2, 1])
        self.assertEqual([e["payload"]["text"] for e in evs], ["一", "二", "三"])
        self.assertEqual(self.st.events(sid, since=a["seq"], types=("notice",))[0]["payload"]["text"], "三")
        self.assertEqual(self.st.last_seq(), c["seq"])
        self.assertEqual(self.st.bump_revision(sid), 3)

    def test_action_states(self):
        sid = self.st.add_session(self.ws["id"])["id"]
        a = self.st.add_action(sid, 1, "c1", "write_file", {"path": "a"}, "create")
        self.assertEqual(a["state"], "proposed")
        self.assertEqual(a["digest"], digest_of("write_file", {"path": "a"}, 1))
        self.assertNotEqual(a["digest"], digest_of("write_file", {"path": "a"}, 2))     # 修订号变了，摘要就变
        for st in ("awaiting", "approved", "running"):
            self.st.set_action(a["id"], st)
            self.assertEqual(self.st.action(a["id"])["state"], st)
        self.st.set_action(a["id"], "done", {"ok": True, "content": "好"})
        self.assertEqual(self.st.action(a["id"])["result"], {"ok": True, "content": "好"})
        self.assertEqual([x["id"] for x in self.st.actions(sid, states=("done",))], [a["id"]])
        self.st.audit(sid, self.st.action(a["id"]), "自动放行", "成功", "x" * 900)
        row = self.st.one("SELECT * FROM audit")
        self.assertEqual((row["tool"], row["decision"], len(row["detail"])), ("write_file", "自动放行", 500))

    def test_session_meta_and_settings(self):
        s = self.st.add_session(self.ws["id"], "标题", meta={"a": 1})
        self.st.set_session(s["id"], status="running", meta={"a": 2})
        s2 = self.st.session(s["id"])
        self.assertEqual((s2["status"], s2["meta"], s2["revision"]), ("running", {"a": 2}, 1))
        self.st.set_tool_setting("x", "deny")
        self.assertEqual(self.st.tool_setting("x"), "deny")
        self.st.set_tool_setting("x", "default")
        self.assertIsNone(self.st.tool_setting("x"))

    def test_new_data_dir_is_private(self):
        import stat
        from agent.config import data_dir
        d = data_dir(os.path.join(self.tmp, "新数据目录"))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        os.chmod(self.tmp, 0o755)
        self.assertEqual(data_dir(self.tmp), self.tmp)                         # 已有目录不改权限
        self.assertEqual(stat.S_IMODE(os.stat(self.tmp).st_mode), 0o755)

    def test_reopen_persists(self):
        sid = self.st.add_session(self.ws["id"])["id"]
        self.st.add_event(sid, "user_message", {"text": "持久"})
        self.st.close()
        self.st = Store(self.tmp)
        self.assertEqual(self.st.events(sid)[0]["payload"]["text"], "持久")


class PathGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "ws")
        os.makedirs(os.path.join(self.root, "sub"))
        with open(os.path.join(self.tmp, "secret.txt"), "w") as f:
            f.write("机密")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_inside_ok(self):
        self.assertEqual(resolve(self.root, "sub/a.txt"), os.path.join(os.path.realpath(self.root), "sub", "a.txt"))
        self.assertEqual(resolve(self.root, "sub\\b.txt"), os.path.join(os.path.realpath(self.root), "sub", "b.txt"))
        self.assertEqual(resolve(self.root, "sub/../a.txt"), os.path.join(os.path.realpath(self.root), "a.txt"))
        resolve(self.root, ".agent/scratch/x.txt")            # 读内部目录可以（截断的工具结果放在那里）

    def test_escapes_rejected(self):
        for bad in ("../secret.txt", "sub/../../secret.txt", os.path.join(self.tmp, "secret.txt"), "/etc/passwd", "", "  "):
            with self.assertRaises(PathError, msg=bad):
                resolve(self.root, bad)

    def test_symlink_escape(self):
        os.symlink(self.tmp, os.path.join(self.root, "link"))
        os.symlink(os.path.join(self.tmp, "secret.txt"), os.path.join(self.root, "s.txt"))
        for bad in ("link/secret.txt", "s.txt"):
            with self.assertRaises(PathError):
                resolve(self.root, bad)

    def test_internal_dir_write_rejected(self):
        for bad in (".agent/x.txt", ".agent", "sub/../.agent/versions/a"):
            with self.assertRaises(PathError):
                resolve(self.root, bad, write=True)
        resolve(self.root, ".agentx/a.txt", write=True)       # 只是名字像，不算


class Decide(unittest.TestCase):
    def test_matrix(self):
        self.assertEqual(decide("read", "auto"), "allow")
        self.assertEqual(decide("create", "auto"), "allow")
        self.assertEqual(decide("overwrite", "auto"), "ask")
        self.assertEqual(decide("exec", "auto"), "ask")
        self.assertEqual(decide("external", "auto"), "ask")
        self.assertEqual(decide("read", "manual"), "ask")
        self.assertEqual(decide("none", "manual"), "allow")
        self.assertEqual(decide("overwrite", "full"), "allow")
        self.assertEqual(decide("delete", "full"), "ask")
        self.assertEqual(decide("delete", "full", "allow"), "ask")    # 删除永远要问
        self.assertEqual(decide("read", "auto", "deny"), "deny")
        self.assertEqual(decide("read", "auto", "ask"), "ask")
        self.assertEqual(decide("exec", "manual", "allow"), "allow")

    def test_validate(self):
        sch = {"type": "object", "properties": {"n": {"type": "integer"}, "k": {"type": "string", "enum": ["a", "b"]},
                                                "xs": {"type": "array", "items": {"type": "string"}},
                                                "u": {"type": ["number", "null"]}}, "required": ["n"]}
        self.assertIsNone(validate(sch, {"n": 1, "k": "a", "xs": ["x"], "u": None}))
        self.assertIsNone(validate(sch, {"n": 2.0, "u": 1.5}))
        self.assertIn("缺少必填字段 n", validate(sch, {}))
        self.assertIn("应该是 integer", validate(sch, {"n": True}))
        self.assertIn("只能是", validate(sch, {"n": 1, "k": "c"}))
        self.assertIn("xs[1]", validate(sch, {"n": 1, "xs": ["a", 2]}))
        self.assertIn("number 或 null", validate(sch, {"n": 1, "u": "x"}))


if __name__ == "__main__":
    unittest.main()
