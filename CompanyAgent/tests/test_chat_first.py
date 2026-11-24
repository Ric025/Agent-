"""聊天为主（docs/聊天为主的界面.md 第 1 节）：默认空间、上传文件、带文件的发言、选文件夹的系统窗口、导出文件名。"""
from __future__ import annotations
import http.client, json, os, socket, sqlite3, threading, unittest
from unittest import mock
from urllib.parse import quote
from helpers import Case, ApiCase, wait_until
from agent import config as config_mod, context, export, picker, review
from agent.config import Config
from agent.store import Store

NOTE = "（用户附上了文件：上传/a.xlsx、上传/b.pdf。先用 read_file、excel_inspect 等工具查看，再动手。）"


# ---------------- 默认空间 ----------------

class DefaultWorkspace(Case):
    def test_runtime_alone_does_not_create(self):
        e = self.env()
        self.assertIsNone(e.rt.store.default_workspace())
        self.assertEqual(len(e.rt.store.workspaces()), 1)       # 只有测试自己建的

    def test_create_once(self):
        e = self.env()
        a = e.rt.ensure_default_workspace()
        b = e.rt.ensure_default_workspace()
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(a["is_default"])
        self.assertEqual(a["name"], "默认")
        self.assertEqual([w["id"] for w in e.rt.store.workspaces() if w["is_default"]], [a["id"]])
        self.assertTrue(os.path.isdir(a["path"]))
        # 重启后也不重复建
        rt = e.restart()
        self.assertEqual(rt.ensure_default_workspace()["id"], a["id"])
        self.assertEqual(sum(1 for w in rt.store.workspaces() if w["is_default"]), 1)

    def test_folder_custom_home(self):
        # 数据目录是另指的（测试用的就是）：<数据目录>/files
        e = self.env()
        ws = e.rt.ensure_default_workspace()
        self.assertEqual(ws["path"], os.path.realpath(os.path.join(e.home, "files")))

    def test_folder_standard_home(self):
        # 数据目录是标准位置：<用户主目录>/CompanyAgent
        e = self.env()
        fake_home = os.path.join(e.tmp, "user")
        os.makedirs(fake_home)
        with mock.patch.object(config_mod, "standard_dir", lambda: e.home), \
                mock.patch.object(config_mod, "user_home", lambda: fake_home):
            self.assertEqual(config_mod.default_folder(e.cfg), os.path.join(fake_home, "CompanyAgent"))
            ws = e.rt.ensure_default_workspace()
        self.assertEqual(ws["path"], os.path.realpath(os.path.join(fake_home, "CompanyAgent")))
        self.assertTrue(os.path.isdir(ws["path"]))

    def test_folder_from_config(self):
        e = self.env()
        want = os.path.join(e.tmp, "我的文件")
        e.cfg.update({"default_folder": want})
        ws = e.rt.ensure_default_workspace()
        self.assertEqual(ws["path"], os.path.realpath(want))
        self.assertTrue(os.path.isdir(want))
        # 之后改了设置：已有的默认空间换到新文件夹，不新建
        want2 = os.path.join(e.tmp, "另一个")
        e.cfg.update({"default_folder": want2})
        ws2 = e.rt.ensure_default_workspace()
        self.assertEqual((ws2["id"], ws2["path"]), (ws["id"], os.path.realpath(want2)))

    def test_folder_fallback(self):
        # 配置的文件夹建不了（父路径是个文件）：退到 <数据目录>/files
        e = self.env()
        blocker = os.path.join(e.tmp, "afile")
        with open(blocker, "w") as f:
            f.write("x")
        e.cfg.update({"default_folder": os.path.join(blocker, "sub")})
        ws = e.rt.ensure_default_workspace()
        self.assertEqual(ws["path"], os.path.realpath(os.path.join(e.home, "files")))

    def test_migration_idempotent(self):
        e = self.env()
        home = os.path.join(e.tmp, "old")
        os.makedirs(home)
        db = sqlite3.connect(os.path.join(home, "ledger.db"))
        db.execute("CREATE TABLE workspaces(id TEXT PRIMARY KEY, name TEXT, path TEXT, instructions TEXT DEFAULT '', "
                   "egress TEXT DEFAULT 'intranet', created REAL)")
        db.execute("INSERT INTO workspaces VALUES('w_old','旧的','/x','','intranet',1)")
        db.commit()
        db.close()
        for _ in range(2):
            s = Store(home)
            self.assertEqual(s.workspace("w_old")["is_default"], False)
            self.assertIsNone(s.default_workspace())
            s.close()


class DefaultWorkspaceApi(ApiCase):
    def test_state_and_delete(self):
        dws = self.e.rt.ensure_default_workspace()
        st, body = self.req("GET", "/api/state")
        self.assertEqual(st, 200)
        self.assertEqual(body["default_workspace_id"], dws["id"])
        flags = {w["id"]: w["is_default"] for w in body["workspaces"]}
        self.assertEqual(flags, {dws["id"]: True, self.e.ws["id"]: False})
        self.assertEqual(body["upload_max_mb"], 50)
        # 默认空间不能删，但可以改名、改指令
        st, body = self.req("DELETE", "/api/workspaces/" + dws["id"])
        self.assertEqual(st, 400)
        self.assertIn("默认空间不能删除", body["error"])
        self.assertIsNotNone(self.e.rt.store.workspace(dws["id"]))
        st, body = self.req("PATCH", "/api/workspaces/" + dws["id"], {"name": "日常", "instructions": "说中文"})
        self.assertEqual((st, body["name"], body["instructions"], body["is_default"]), (200, "日常", "说中文", True))
        # 别的工作区照常能删
        self.assertEqual(self.req("DELETE", "/api/workspaces/" + self.e.ws["id"])[0], 200)

    def test_add_same_folder_returns_existing(self):
        dws = self.e.rt.ensure_default_workspace()
        n = len(self.e.rt.store.workspaces())
        st, body = self.req("POST", "/api/workspaces", {"name": "又一个", "path": self.e.root})
        self.assertEqual((st, body["id"], body.get("existing")), (200, self.e.ws["id"], True))
        st, body = self.req("POST", "/api/workspaces", {"path": dws["path"] + "/"})
        self.assertEqual((st, body["id"], body["is_default"], body.get("existing")), (200, dws["id"], True, True))
        self.assertEqual(len(self.e.rt.store.workspaces()), n)
        # 新文件夹照常新建，不带 existing
        other = os.path.join(self.e.tmp, "新的")
        os.makedirs(other)
        st, body = self.req("POST", "/api/workspaces", {"path": other})
        self.assertEqual(st, 200)
        self.assertNotIn("existing", body)
        self.assertEqual(len(self.e.rt.store.workspaces()), n + 1)

    def test_add_same_folder_via_symlink(self):
        link = os.path.join(self.e.tmp, "链接")
        os.symlink(self.e.root, link)
        n = len(self.e.rt.store.workspaces())
        st, body = self.req("POST", "/api/workspaces", {"path": link})
        self.assertEqual((st, body["id"], body.get("existing")), (200, self.e.ws["id"], True))
        self.assertEqual(len(self.e.rt.store.workspaces()), n)

    def test_state_without_default(self):
        st, body = self.req("GET", "/api/state")
        self.assertEqual((st, body["default_workspace_id"]), (200, None))


# ---------------- 上传 ----------------

class Upload(ApiCase):
    def up(self, name, data, wid=None, token=True, length=None, query=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        q = query if query is not None else "workspace=%s&name=%s" % (wid or self.e.ws["id"], quote(name))
        c.putrequest("POST", "/api/upload?" + q, skip_host=True)
        c.putheader("Host", "127.0.0.1:%d" % self.srv.port)
        if token:
            c.putheader("X-Token", self.srv.token)
        c.putheader("Content-Type", "application/octet-stream")
        if length is not False:
            c.putheader("Content-Length", str(len(data) if length is None else length))
        c.endheaders(data)
        r = c.getresponse()
        payload = r.read()
        c.close()
        return r.status, (json.loads(payload.decode("utf-8")) if payload else None)

    def up_dir(self):
        return os.path.join(self.e.root, "上传")

    def listing(self):
        d = self.up_dir()
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def test_normal(self):
        data = b"PK\x03\x04" + os.urandom(3000)
        st, body = self.up("报表.xlsx", data)
        self.assertEqual(st, 200)
        self.assertEqual(body, {"path": "上传/报表.xlsx", "name": "报表.xlsx", "size": len(data)})
        with open(os.path.join(self.up_dir(), "报表.xlsx"), "rb") as f:
            self.assertEqual(f.read(), data)
        rows = self.e.rt.store.q("SELECT * FROM audit WHERE tool='upload'")
        self.assertEqual(len(rows), 1)
        self.assertIn("上传/报表.xlsx", rows[0]["detail"])
        # 临时文件不留
        tmpdir = os.path.join(self.e.root, ".agent", "tmp")
        self.assertEqual(os.listdir(tmpdir) if os.path.isdir(tmpdir) else [], [])
        # 空文件也可以传
        st, body = self.up("空.txt", b"")
        self.assertEqual((st, body["size"]), (200, 0))

    def test_same_name(self):
        self.assertEqual(self.up("a.txt", b"1")[1]["path"], "上传/a.txt")
        self.assertEqual(self.up("a.txt", b"2")[1]["path"], "上传/a(2).txt")
        self.assertEqual(self.up("a.txt", b"3")[1]["path"], "上传/a(3).txt")
        self.assertEqual(self.up("README", b"4")[1]["path"], "上传/README")
        self.assertEqual(self.up("README", b"5")[1]["path"], "上传/README(2)")
        with open(os.path.join(self.up_dir(), "a.txt"), "rb") as f:
            self.assertEqual(f.read(), b"1")          # 原文件没被覆盖

    def test_name_cleaning(self):
        self.assertEqual(self.up("../../evil.txt", b"x")[1]["path"], "上传/evil.txt")
        self.assertEqual(self.up("..\\..\\win.txt", b"x")[1]["path"], "上传/win.txt")
        self.assertEqual(self.up("/etc/passwd", b"x")[1]["path"], "上传/passwd")
        self.assertEqual(self.up("a\x00b\x1f\nc.txt", b"x")[1]["path"], "上传/abc.txt")
        self.assertEqual(self.up('x:y?.txt', b"x")[1]["path"], "上传/x_y_.txt")
        for bad in ("", "..", "...", " . ", "a/..", "\x01\x02"):
            st, body = self.up(bad, b"x")
            self.assertEqual(st, 400, repr(bad))
            self.assertIn("error", body)
        st, _ = self.up("", b"x", query="workspace=%s" % self.e.ws["id"])       # 没给 name
        self.assertEqual(st, 400)
        self.assertFalse(os.path.exists(os.path.join(self.e.tmp, "evil.txt")))
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.e.root), "evil.txt")))
        self.assertEqual(self.listing(), ["abc.txt", "evil.txt", "passwd", "win.txt", "x_y_.txt"])

    def test_too_large(self):
        self.e.cfg.update({"upload_max_mb": 0.001})       # 约 1 KB
        st, body = self.up("big.bin", b"x" * 2000)
        self.assertEqual(st, 413)
        self.assertIn("上限", body["error"])
        self.assertEqual(self.listing(), [])
        # 只声明了很大的长度、实际不发：按 Content-Length 先拒绝
        st, body = self.up("big.bin", b"", length=10 * 1024 * 1024 * 1024)
        self.assertEqual(st, 413)
        self.assertEqual(self.listing(), [])
        # 不超的照常
        self.assertEqual(self.up("small.bin", b"x" * 1000)[0], 200)

    def test_bad_requests(self):
        self.assertEqual(self.up("a.txt", b"x", token=False)[0], 403)
        self.assertEqual(self.up("a.txt", b"x", wid="w_nope")[0], 404)
        self.assertEqual(self.up("a.txt", b"", length=False)[0], 411)
        self.assertEqual(self.up("a.txt", b"", length=-5)[0], 400)
        self.assertEqual(self.up("a.txt", b"x", query="name=a.txt")[0], 400)     # 没给工作区
        self.assertEqual(self.req("GET", "/api/upload")[0], 405)
        self.assertEqual(self.listing(), [])

    def test_incomplete_body(self):
        # 声明 100 字节只发 40 就断开：不留下半截文件
        s = socket.create_connection(("127.0.0.1", self.srv.port), timeout=10)
        head = ("POST /api/upload?workspace=%s&name=half.txt HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Token: %s\r\n"
                "Content-Length: 100\r\n\r\n" % (self.e.ws["id"], self.srv.port, self.srv.token))
        s.sendall(head.encode() + b"y" * 40)
        s.shutdown(socket.SHUT_WR)
        resp = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
        s.close()
        self.assertIn(b" 400 ", resp.split(b"\r\n")[0])
        self.assertEqual(self.listing(), [])

    def test_path_guard(self):
        outside = os.path.join(self.e.tmp, "outside")
        os.makedirs(outside)
        # 上传/ 是指向工作区外的符号链接：拒绝，外面什么都不写
        os.symlink(outside, self.up_dir())
        st, body = self.up("a.txt", b"x")
        self.assertEqual(st, 400)
        self.assertIn("工作区之外", body["error"])
        self.assertEqual(os.listdir(outside), [])

    def test_symlink_file_not_followed(self):
        # 上传/a.txt 是指向工作区外文件的符号链接：不跟着写过去，换个名字
        outside = os.path.join(self.e.tmp, "secret.txt")
        with open(outside, "w") as f:
            f.write("原样")
        os.makedirs(self.up_dir())
        os.symlink(outside, os.path.join(self.up_dir(), "a.txt"))
        st, body = self.up("a.txt", b"new")
        self.assertEqual((st, body["path"]), (200, "上传/a(2).txt"))
        with open(outside) as f:
            self.assertEqual(f.read(), "原样")

    def test_internal_dir_symlink(self):
        # .agent 指向工作区外：临时文件也不能写出去，占位文件不留
        outside = os.path.join(self.e.tmp, "outside2")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.e.root, ".agent"))
        st, _ = self.up("a.txt", b"x")
        self.assertEqual(st, 400)
        self.assertEqual(os.listdir(outside), [])
        self.assertEqual(self.listing(), [])


# ---------------- 带文件的发言 ----------------

def capture(seen, reply="看过了"):
    def fn(messages, tools):
        seen.append(messages)
        return {"content": reply}
    return fn


class MessageFiles(ApiCase):
    def setUp(self):
        self.seen = []
        self.script = [capture(self.seen)] * 5
        super().setUp()
        self.e.write("上传/a.xlsx", "x")
        self.e.write("上传/b.pdf", "x")

    def test_message_with_files(self):
        sid = self.e.new_session()
        st, _ = self.req("POST", "/api/sessions/%s/message" % sid, {"text": "汇总一下", "files": ["上传/a.xlsx", "上传\\b.pdf"]})
        self.assertEqual(st, 200)
        self.assertTrue(self.e.rt.wait_idle(sid, 10))
        ev = self.e.events(sid, ("user_message",))[0]
        self.assertEqual(ev["payload"], {"text": "汇总一下", "files": ["上传/a.xlsx", "上传/b.pdf"]})
        self.assertEqual(self.seen[0][-1], {"role": "user", "content": "汇总一下\n" + NOTE})
        self.assertEqual(self.e.status(sid), "done")

    def test_files_only_and_new_session(self):
        st, s = self.req("POST", "/api/sessions", {"workspace_id": self.e.ws["id"], "files": ["上传/a.xlsx"]})
        self.assertEqual(st, 200)
        self.assertTrue(self.e.rt.wait_idle(s["id"], 10))
        ev = self.e.events(s["id"], ("user_message",))[0]
        self.assertEqual(ev["payload"]["files"], ["上传/a.xlsx"])
        self.assertEqual(self.e.rt.store.session(s["id"])["title"], "a.xlsx")
        self.assertIn("（用户附上了文件：上传/a.xlsx。", self.seen[0][-1]["content"])

    def test_bad_files(self):
        sid = self.e.new_session()
        outside = os.path.join(self.e.tmp, "o.txt")
        with open(outside, "w") as f:
            f.write("x")
        for files in (["上传/没有.xlsx"], ["../o.txt"], [outside], ["上传"], "上传/a.xlsx", [1], [""],
                      ["上传/a.xlsx"] * 21):
            st, body = self.req("POST", "/api/sessions/%s/message" % sid, {"text": "看看", "files": files})
            self.assertEqual(st, 400, files)
            self.assertIn("error", body)
        st, _ = self.req("POST", "/api/sessions", {"workspace_id": self.e.ws["id"], "text": "x", "files": ["../o.txt"]})
        self.assertEqual(st, 400)
        self.assertEqual(self.e.rt.store.sessions(self.e.ws["id"]), [self.e.rt.store.session(sid)])   # 没多建对话
        self.assertEqual(self.e.events(sid), [])


class FilesProjection(Case):
    def test_builtin_kernel(self):
        seen = []
        e = self.env([capture(seen), capture(seen)])
        sid = e.new_session()
        e.rt.send_message(sid, "看看这两个", files=["上传/a.xlsx", "上传/b.pdf"])
        self.assertTrue(e.rt.wait_idle(sid, 10))
        self.assertEqual(seen[0][-1]["content"], "看看这两个\n" + NOTE)
        # 再发一句：前一句的附件说明还在历史里
        e.rt.send_message(sid, "好的")
        self.assertTrue(e.rt.wait_idle(sid, 10))
        users = [m["content"] for m in seen[1] if m["role"] == "user"]
        self.assertEqual(users, ["看看这两个\n" + NOTE, "好的"])

    def test_project_events(self):
        evs = [{"seq": 1, "type": "user_message", "payload": {"text": "", "files": ["上传/a.xlsx", "上传/b.pdf"]}},
               {"seq": 2, "type": "user_message", "payload": {"text": "看图", "images": ["data:image/png;base64,AA"],
                                                              "files": ["上传/a.xlsx", "上传/b.pdf"]}}]
        msgs = context.project_events(evs)
        self.assertEqual(len(msgs), 1)       # 相邻的用户发言合并
        parts = msgs[0]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": NOTE})
        self.assertEqual(parts[1], {"type": "text", "text": "看图\n" + NOTE})
        self.assertEqual(parts[2]["type"], "image_url")

    def test_transcripts_and_export(self):
        evs = [{"seq": 1, "type": "user_message", "ts": 0, "payload": {"text": "看看", "files": ["上传/a.xlsx", "上传/b.pdf"]}},
               {"seq": 2, "type": "assistant_message", "ts": 0, "payload": {"content": "好"}}]
        text, n = review.transcript(evs)[:2]
        self.assertIn("用户：看看（附文件 2 个）", text)
        self.assertEqual(n, 1)
        self.assertIn("（附文件：上传/a.xlsx、上传/b.pdf）", context._transcript(None, evs, 10000))
        self.assertIn("（附文件：上传/a.xlsx、上传/b.pdf）", context.mechanical_summary(None, evs, evs))
        e = self.env()
        sid = e.new_session()
        e.rt.emit(sid, "user_message", {"text": "看看", "files": ["上传/a.xlsx"]})
        md = export.session_markdown(e.rt.store, sid)
        self.assertIn("- 附件：上传/a.xlsx", md)

    def test_export_file_name(self):
        self.assertEqual(export.file_name(""), "对话记录.md")
        self.assertEqual(export.file_name(None), "对话记录.md")
        self.assertEqual(export.file_name(" ./ "), "对话记录.md")
        self.assertEqual(export.file_name("月报/汇总"), "月报 汇总.md")


# ---------------- 选文件夹 ----------------

class PickFolder(ApiCase):
    def pick(self, fn):
        with mock.patch.object(picker, "pick_folder", fn):
            return self.req("POST", "/api/pick_folder")

    def test_results(self):
        folder = os.path.join(self.e.tmp, "季度经营分析")
        self.assertEqual(self.pick(lambda: {"supported": True, "path": folder}),
                         (200, {"supported": True, "path": folder, "name": "季度经营分析"}))
        self.assertEqual(self.pick(lambda: {"supported": True, "path": None}), (200, {"supported": True, "path": None}))
        self.assertEqual(self.pick(lambda: {"supported": False}), (200, {"supported": False}))

        def boom():
            raise RuntimeError("坏了")
        self.assertEqual(self.pick(boom), (200, {"supported": False}))
        self.assertEqual(self.req("POST", "/api/pick_folder", token=False)[0], 403)

    def test_only_one_at_a_time(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def slow():
            calls.append(1)
            entered.set()
            release.wait(10)
            return {"supported": True, "path": "/x"}
        out = {}
        with mock.patch.object(picker, "pick_folder", slow):
            t = threading.Thread(target=lambda: out.setdefault("r", self.req("POST", "/api/pick_folder")))
            t.start()
            self.assertTrue(entered.wait(5))
            st, body = self.req("POST", "/api/pick_folder")
            self.assertEqual(st, 409)
            self.assertIn("error", body)
            release.set()
            t.join(10)
        self.assertEqual(out["r"][0], 200)
        self.assertEqual(out["r"][1]["path"], "/x")
        self.assertEqual(len(calls), 1)
        # 窗口关了以后可以再弹
        self.assertEqual(self.pick(lambda: {"supported": True, "path": None})[0], 200)


class PickerParse(unittest.TestCase):
    def test_parse(self):
        here = os.path.dirname(os.path.abspath(__file__))
        self.assertEqual(picker.parse("darwin", 0, here + "/\n", ""), ("ok", here))
        self.assertEqual(picker.parse("darwin", 1, "", "execution error: User canceled. (-128)"), ("cancel", None))
        self.assertEqual(picker.parse("darwin", 1, "", "some other error"), ("error", None))
        self.assertEqual(picker.parse("win32", 0, "﻿" + here + "\r\n", ""), ("ok", here))
        self.assertEqual(picker.parse("win32", 2, "", ""), ("cancel", None))
        self.assertEqual(picker.parse("linux", 1, "", ""), ("cancel", None))
        self.assertEqual(picker.parse("linux", 0, "/没有这个目录\n", ""), ("error", None))
        self.assertEqual(picker.parse("linux", 5, "", ""), ("error", None))

    def test_command_fixed(self):
        with mock.patch.object(picker.shutil, "which", lambda name: "/usr/bin/" + name):
            mac = picker.command("darwin")
            self.assertEqual(mac[0], "/usr/bin/osascript")
            self.assertIn("activate", mac)
            win = picker.command("win32")
            self.assertIn("-EncodedCommand", win)
            with mock.patch.dict(os.environ, {"DISPLAY": ":0"}):
                self.assertEqual(picker.command("linux")[:3], ["/usr/bin/zenity", "--file-selection", "--directory"])
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(picker.command("linux"))      # 没有图形界面
        with mock.patch.object(picker.shutil, "which", lambda name: None):
            self.assertIsNone(picker.command("darwin"))
            self.assertEqual(picker.pick_folder.__module__, "agent.picker")

    def test_pick_folder_unsupported_here(self):
        with mock.patch.object(picker, "command", lambda platform=None: None):
            self.assertEqual(picker.pick_folder(), {"supported": False})


if __name__ == "__main__":
    unittest.main()
