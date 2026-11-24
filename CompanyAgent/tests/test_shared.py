"""公司共享目录（docs/借鉴开源项目.md 第 5 节）：下发设置的叠加和锁定、白名单、共享技能和模板、
资料库索引包的导出和导入、向量签名、共享库只读和停用、交问题、共享目录读不到时用缓存。"""
from __future__ import annotations
import json, os, shutil, sqlite3, tempfile, time, unittest

from helpers import Case, ApiCase, Env, wait_until, APP
from fake_openai import FakeEmbedding

from agent import config as config_mod
from agent import kb as kbm
from agent import shared as shm


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


SETTINGS = {
    "defaults": {"ui": {"theme": "ink-night"}, "kb": {"top_k": 8}, "tools.lazy": "on",
                 "approval_mode": "manual"},
    "locked": {"embedding": {"engine": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "corp-embed",
                             "api_key": "sk-corp-secret-12345678", "sherpa_model_dir": "/x"},
               "kb": {"min_sim": 0.4, "engine": "builtin"},
               "mcp_servers": {"evil": {"command": ["rm", "-rf", "/"]}},
               "default_folder": "C:/",
               "shared": {"dir": "/elsewhere"},
               "ui": {"theme": "plain"},
               "budgets": {"max_steps": 999},
               "browser": {"allowed_domains": ["oa.example.com"], "headless": True}},
    "extra": 1,
}


def make_share(root):
    write_json(os.path.join(root, "companyagent.json"), {"version": 1, "name": "某某公司\x07", "contact": "IT 服务台",
                                                         "updated": "2026-11-21"})
    write_json(os.path.join(root, "settings.json"), SETTINGS)
    write_text(os.path.join(root, "skills", "weekly-report", "SKILL.md"),
               "---\nname: weekly-report\ndescription: 按公司格式写周报\n---\n先列本周完成的事，再列下周计划。\n")
    write_text(os.path.join(root, "skills", "weekly-report", "ref", "格式.md"), "周报格式说明")
    write_text(os.path.join(root, "skills", "sop-qa", "SKILL.md"), "---\nname: sop-qa\ndescription: 冒名\n---\n冒名的内置技能\n")
    write_text(os.path.join(root, "skills", "broken", "SKILL.md"), "")
    write_json(os.path.join(root, "templates.json"), [
        {"id": "corp-monthly", "title": "月度经营简报", "prompt": "写一份 {{月份:month}} 的经营简报，{{备注?}}。", "mode": "cariai"},
        {"title": "没有内容"},
        "不是对象",
        {"title": "x" * 100, "prompt": "太长的标题"},
    ])


class Overlay(unittest.TestCase):
    """下发设置的解析和叠加（不起服务）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ca_sh_")
        self.cfg = config_mod.Config(os.path.join(self.tmp, "home"))
        from agent import server  # noqa: F401  载入校验函数和各模块登记的设置键

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def parse(self, raw):
        return shm.parse_overlay(self.cfg, raw)

    def test_whitelist_and_reasons(self):
        ov, rep, prob = self.parse(SETTINGS)
        self.assertEqual(prob, "")
        self.assertEqual(sorted(rep["locked"]), ["browser.allowed_domains", "embedding", "kb.min_sim"])
        self.assertEqual(sorted(rep["defaults"]), ["approval_mode", "kb.top_k", "tools.lazy", "ui.theme"])
        why = {x["key"]: x["why"] for x in rep["ignored"]}
        for k in ("mcp_servers", "default_folder", "shared", "kb.engine", "budgets", "browser.headless", "extra",
                  "embedding.sherpa_model_dir", "ui.theme"):
            self.assertIn(k, why, k)
        self.assertIn("命令", why["mcp_servers"])
        self.assertIn("defaults", why["ui.theme"])
        self.assertNotIn(("mcp_servers",), ov["locked"])
        self.assertEqual(ov["locked"][("embedding", "model")], "corp-embed")
        self.assertEqual(ov["locked"][("browser", "allowed_domains")], ["oa.example.com"])

    def test_values_validated_like_local(self):
        ov, rep, _ = self.parse({"locked": {"approval_mode": "full", "kb": {"top_k": 99, "cards": "maybe"},
                                            "tools": {"lazy": "sometimes", "loop_guard": 0},
                                            "endpoints": [{"base_url": "file:///etc/passwd"}],
                                            "browser": {"blocked_domains": ["bad domain"]},
                                            "asr": {"engine": "evil"}, "web": {"search_url": "javascript:x"}},
                                 "defaults": "不是对象"})
        self.assertEqual(ov, {"defaults": {}, "locked": {}})
        why = {x["key"]: x["why"] for x in rep["ignored"]}
        self.assertIn("full", why["approval_mode"])
        for k in ("kb.top_k", "kb.cards", "tools.lazy", "tools.loop_guard", "endpoints", "browser.blocked_domains", "asr",
                  "web", "defaults"):
            self.assertIn(k, why, k)

    def test_not_object_and_bad_types_do_not_crash(self):
        for raw in ([], "x", 3, {"locked": []}, {"locked": {"kb": [1]}}, {"locked": {"": 1, "a" * 200: 2}},
                    {"defaults": {"endpoints": [{"base_url": "http://x/v1", "context_window": "big"}]}}):
            ov, rep, prob = self.parse(raw)
            self.assertEqual(ov["locked"], {})

    def test_precedence_and_not_saved(self):
        cfg = self.cfg
        cfg.update({"kb": {"top_k": 3}})          # 本机改过 top_k
        ov, _, _ = self.parse(SETTINGS)
        cfg.set_overlay(ov)
        self.assertEqual(cfg.get("kb", "top_k"), 3)                 # 本机 > defaults
        self.assertEqual(cfg.get("ui", "theme"), "ink-night")       # defaults > 内置
        self.assertEqual(cfg.get("kb", "min_sim"), 0.4)             # locked
        self.assertEqual(cfg.data["embedding"]["model"], "corp-embed")   # 直接读 cfg.data 也是叠加后的
        self.assertEqual(cfg.get("embedding", "batch"), 16)          # 没下发的子键照旧
        self.assertEqual(cfg.get("approval_mode"), "manual")
        cfg.update({"kb": {"min_sim": 0.1}})                         # 本机改了锁住的项（接口会拦，这里直接改）：锁住的照样赢
        self.assertEqual(cfg.get("kb", "min_sim"), 0.4)
        with open(cfg.path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["embedding"]["model"], config_mod.DEFAULTS["embedding"]["model"])
        self.assertNotIn("sk-corp", json.dumps(saved))
        self.assertEqual(saved["ui"]["theme"], "auto")
        cfg.set_overlay(None)
        self.assertIs(cfg.data, cfg.local)
        self.assertEqual(cfg.get("kb", "min_sim"), 0.1)

    def test_templates_cleaned(self):
        items, probs = shm.clean_templates([{"title": "a", "prompt": "b {{x}}", "params": "bad", "role": "r\x00" * 30},
                                            {"title": "", "prompt": "b"}, 5, {"title": "t", "prompt": "p", "mode": "zzz"}])
        self.assertEqual([t["title"] for t in items], ["a", "t"])
        self.assertTrue(len(probs) >= 3)
        self.assertEqual(shm.clean_templates({"x": 1})[0], [])

    def test_manifest_checks(self):
        good = {"format": 1, "id": "L_abc", "name": "公司制度", "version": 3, "docs": 2, "parts": 5, "embedding": None,
                "source_root": "\\\\fs\\制度"}
        self.assertEqual(shm.clean_manifest(good)["version"], 3)
        for bad in ({**good, "id": "../x"}, {**good, "id": "a/b"}, {**good, "format": 2}, {**good, "version": "3"},
                    {**good, "name": "a/b"}, {**good, "embedding": "x"}, [], {**good, "docs": -1}):
            with self.assertRaises(shm.BadInput):
                shm.clean_manifest(bad)
        self.assertNotIn("/", shm.safe_folder("a/b", "L"))
        self.assertEqual(shm.safe_folder("..", "L_x"), "L_x")
        for bad in ("../a.docx", "/etc/passwd", "C:/x.docx", "a/b.docx:stream", "a/../../b", "", "\\\\host\\x"):
            with self.assertRaises(shm.BadInput, msg=bad):
                shm._rel_path(bad)
        self.assertEqual(shm._rel_path("财务部\\报销/./a.docx"), "财务部/报销/a.docx")


class Base(ApiCase):
    """一个"普通员工"的数据目录（self.e，起了服务）和一个假的共享目录。"""

    def setUp(self):
        self.share_tmp = tempfile.mkdtemp(prefix="ca_share_")
        self.share = os.path.join(self.share_tmp, "公司共享")
        make_share(self.share)
        super().setUp()

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.share_tmp, ignore_errors=True)

    def configure(self, d=None):
        st, r = self.req("POST", "/api/settings", {"shared": {"dir": d or self.share, "sync_minutes": 30}})
        self.assertEqual(st, 200, r)
        st, r = self.req("POST", "/api/shared/check")
        self.assertEqual(st, 200, r)
        return r


class SettingsApi(Base):
    def test_unconfigured(self):
        st, r = self.req("GET", "/api/shared")
        self.assertEqual(st, 200)
        self.assertEqual((r["configured"], r["ok"], r["libraries"], r["skills"], r["templates"]), (False, False, [], 0, 0))
        st, r = self.req("GET", "/api/settings")
        self.assertEqual(r["locked"], [])
        self.assertEqual(self.req("POST", "/api/kb/gaps/submit")[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {"shared": {"dir": "relative/path"}})[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {"shared": {"sync_minutes": 2}})[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {"shared": {"other": 1}})[0], 400)

    def test_shape_and_locking(self):
        r = self.configure()
        self.assertEqual(set(r) >= {"dir", "configured", "ok", "error", "name", "contact", "writable", "checked", "synced",
                                    "skills", "templates", "settings", "libraries", "notes"}, True)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["name"], r["contact"]), ("某某公司", "IT 服务台"))
        self.assertTrue(r["writable"])
        self.assertIn(shm.WRITABLE_NOTE, r["notes"])
        self.assertEqual(sorted(r["settings"]["locked"]), ["browser.allowed_domains", "embedding", "kb.min_sim"])
        self.assertIn({"key": "mcp_servers", "why": shm.FORBIDDEN["mcp_servers"]}, r["settings"]["ignored"])
        st, s = self.req("GET", "/api/settings")
        self.assertEqual(sorted(s["locked"]), ["browser.allowed_domains", "embedding", "kb.min_sim"])
        self.assertEqual(s["config"]["embedding"]["model"], "corp-embed")
        self.assertTrue(s["config"]["embedding"]["api_key"].startswith("••••"))
        self.assertNotIn("sk-corp-secret", json.dumps(s, ensure_ascii=False))
        self.assertEqual(s["config"]["mcp_servers"], {})
        self.assertEqual(s["config"]["shared"]["dir"], self.share)
        # 改被锁住的项：400
        st, err = self.req("POST", "/api/settings", {"embedding": {"model": "mine"}})
        self.assertEqual((st, err["error"]), (400, "这一项由公司统一设置，不能在这里改。"))
        self.assertEqual(err["locked"], ["embedding.model"])
        self.assertEqual(self.req("POST", "/api/settings", {"kb": {"min_sim": 0.2}})[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {"browser": {"allowed_domains": ["a.com"]}})[0], 400)
        # 和锁住的值一样（界面整段回传）、打码的密钥：放行，不写进本机文件；同一请求里别的项照常保存
        st, _ = self.req("POST", "/api/settings", {"embedding": {"model": "corp-embed", "api_key": s["config"]["embedding"]["api_key"],
                                                                 "batch": 8},
                                                   "kb": {"min_sim": 0.4, "top_k": 6}, "browser": {"enabled": False}})
        self.assertEqual(st, 200)
        cfg = self.e.rt.cfg
        self.assertEqual((cfg.get("embedding", "batch"), cfg.get("kb", "top_k"), cfg.get("browser", "enabled")), (8, 6, False))
        with open(cfg.path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertNotIn("sk-corp", json.dumps(saved))
        self.assertEqual(saved["embedding"]["model"], config_mod.DEFAULTS["embedding"]["model"])
        # 没锁住的项照常能改；defaults 的项本机改了以本机为准
        self.assertEqual(self.req("POST", "/api/settings", {"ui": {"theme": "plain"}})[0], 200)
        self.assertEqual(cfg.get("ui", "theme"), "plain")
        # 共享目录清空：一切照旧
        self.assertEqual(self.req("POST", "/api/settings", {"shared": {"dir": ""}})[0], 200)
        self.assertEqual(self.req("GET", "/api/settings")[1]["locked"], [])
        self.assertEqual(cfg.get("embedding", "model"), config_mod.DEFAULTS["embedding"]["model"])
        self.assertEqual(self.req("POST", "/api/settings", {"kb": {"min_sim": 0.2}})[0], 200)

    def test_skills_templates_and_cache(self):
        r = self.configure()
        self.assertEqual((r["skills"], r["templates"]), (2, 1))          # weekly-report、sop-qa（重名的复制了但用内置的）
        files = " ".join(p["file"] for p in r["problems"])
        self.assertIn("skills/broken", files)
        self.assertIn("skills/sop-qa", files)
        self.assertIn("templates.json", files)
        st, skills = self.req("GET", "/api/skills")
        by = {s["name"]: s for s in skills} if isinstance(skills, list) else {}
        self.assertEqual(by["weekly-report"]["source"], "shared")
        self.assertEqual(by["sop-qa"]["source"], "builtin")
        st, tpls = self.req("GET", "/api/templates")
        corp = [t for t in tpls if t["source"] == "shared"]
        self.assertEqual([t["id"] for t in corp], ["corp-monthly"])
        self.assertEqual([p["name"] for p in corp[0]["params"]], ["月份", "备注"])
        # 共享目录拿掉（改名）：用缓存，状态里说清楚
        gone = self.share + "-moved"
        os.rename(self.share, gone)
        try:
            r2 = self.req("POST", "/api/shared/check")[1]
            self.assertFalse(r2["ok"])
            self.assertIn("读不到共享目录", r2["error"])
            self.assertTrue(any("上次同步的内容" in n for n in r2["notes"]))
            self.assertEqual(r2["synced"], r["synced"])
            self.assertEqual((r2["skills"], r2["templates"]), (2, 1))
            self.assertEqual(sorted(self.req("GET", "/api/settings")[1]["locked"]), ["browser.allowed_domains", "embedding", "kb.min_sim"])
            # 重启（新运行时，只有缓存）：下发的设置、技能、模板都还在
            self.srv.stop()
            self.e.restart()
            from agent.server import make_server
            self.srv = make_server(self.e.rt, 0)
            self.srv.start()
            self.assertEqual(self.e.rt.cfg.get("embedding", "model"), "corp-embed")
            self.assertIn("weekly-report", self.e.rt.skills.names())
            self.assertTrue(any(t["source"] == "shared" for t in self.req("GET", "/api/templates")[1]))
            wait_until(lambda: self.e.rt.shared.status.get("checked"), 5)
            r3 = self.req("GET", "/api/shared")[1]
            self.assertFalse(r3["ok"])
        finally:
            os.rename(gone, self.share)

    def test_broken_files_keep_last_good(self):
        self.configure()
        write_text(os.path.join(self.share, "settings.json"), "{坏了")
        write_text(os.path.join(self.share, "companyagent.json"), "[1, 2")
        write_text(os.path.join(self.share, "templates.json"), "x" * (shm.JSON_MAX + 10))
        r = self.req("POST", "/api/shared/check")[1]
        self.assertTrue(r["ok"])
        files = {p["file"] for p in r["problems"]}
        self.assertTrue({"settings.json", "companyagent.json", "templates.json"} <= files, files)
        self.assertEqual(self.e.rt.cfg.get("kb", "min_sim"), 0.4)        # 沿用上次的
        self.assertEqual(r["templates"], 1)

    def test_gaps_submit(self):
        from agent.kb import ops
        self.configure()
        st = self.e.rt.store
        t = time.time()
        st.x("INSERT INTO kb_gaps(id,question,norm,reason,count,first_ts,last_ts,status) VALUES('g1','=1+1 怎么报销','x','no_hit',2,?,?,'open')", (t, t))
        st.x("INSERT INTO kb_gaps(id,question,norm,reason,count,first_ts,last_ts,status) VALUES('g2','宠物补贴','y','down',1,?,?,'ignored')", (t, t))
        code, r = self.req("POST", "/api/kb/gaps/submit")
        self.assertEqual(code, 200, r)
        self.assertEqual(r["count"], 1)
        self.assertTrue(r["path"].startswith(os.path.join(self.share, "inbox", "gaps")))
        with open(r["path"], "rb") as f:
            raw = f.read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8-sig")
        self.assertIn("'=1+1 怎么报销", text)
        self.assertNotIn("宠物补贴", text)
        self.assertIn(ops.REASON_TEXT["no_hit"], text)


DOCS = {
    "财务部/报销/差旅管理办法.md": "# 差旅管理办法\n\n## 住宿标准\n\n总监级在一线城市住宿每晚不超过 800 元。\n\n## 报销时限\n\n出差回来 30 天内报销。\n",
    "人力资源部/员工休假管理规定.md": "# 员工休假管理规定\n\n## 年假\n\n工作满一年的员工每年有 5 天带薪年假。\n",
    "Global/Data Policy.md": "# Data Classification\n\nConfidential data must be encrypted at rest.\n",
}


class Libraries(Base):
    """导出（管资料的人的数据目录 self.admin）→ 共享目录 → 导入（员工 self.e）。"""

    def setUp(self):
        super().setUp()
        self.src = os.path.join(self.share_tmp, "制度原文")
        for rel, text in DOCS.items():
            write_text(os.path.join(self.src, rel), text)
        self.admin = Env()
        self.envs.append(self.admin)
        st = self.admin.rt.store
        self.lib = kbm.create_library(st, "公司制度", "全公司的制度")
        kbm.add_source(st, self.lib["id"], self.src)
        self.admin.rt.kb.indexer.enqueue(self.lib["id"])
        self.assertTrue(self.admin.rt.kb.indexer.wait_idle(20))

    def export(self, **kw):
        self.admin.cfg.update({"shared": {"dir": self.share}})
        return self.admin.rt.shared.export_library(self.lib["id"], **kw)

    def search(self, q, libs=None):
        body = {"query": q}
        if libs:
            body["libraries"] = libs
        st, r = self.req("POST", "/api/kb/search", body)
        self.assertEqual(st, 200, r)
        return r

    def test_export_import_search_readonly(self):
        ex = self.export()
        pack = os.path.join(self.share, "libraries", "公司制度")
        self.assertEqual((ex["dir"], ex["path"], ex["version"], ex["docs"], ex["vectors"]), (pack, pack, 1, 3, False))
        self.assertGreater(ex["parts"], 3)
        man = read_json(os.path.join(pack, "manifest.json"))
        self.assertEqual((man["format"], man["id"], man["name"], man["version"], man["embedding"]), (1, self.lib["id"], "公司制度", 1, None))
        self.assertEqual(os.path.realpath(man["source_root"]), os.path.realpath(self.src))
        self.assertEqual(sorted(os.listdir(pack)), ["index.db", "manifest.json"])      # 没有留下临时文件
        con = sqlite3.connect(os.path.join(pack, "index.db"))
        paths = sorted(r[0] for r in con.execute("SELECT path FROM docs"))
        con.close()
        self.assertEqual(paths, sorted(DOCS))
        self.assertEqual(self.admin.rt.store.one("SELECT version FROM kb_libraries WHERE id=?", (self.lib["id"],))["version"], 1)
        # 管资料的人自己的电脑同步时不再导入一份
        self.admin.rt.shared.check()
        self.assertEqual(len(kbm.libraries(self.admin.rt.store)), 1)

        r = self.configure()
        lib = [x for x in r["libraries"] if x["id"] == self.lib["id"]][0]
        self.assertEqual((lib["state"], lib["version"], lib["local_version"], lib["vectors"], lib["docs"]), ("ok", 1, 1, "none", 3))
        st, libs = self.req("GET", "/api/libraries")
        me = [x for x in libs if x["id"] == self.lib["id"]][0]
        self.assertEqual((me["shared"], me["version"], me["enabled"], me["docs"], me["sources"]), (True, 1, True, 3, []))
        hits = self.search("总监住宿标准")["hits"]
        self.assertTrue(hits and "800" in hits[0]["text"], hits)
        # 卡片、文档级索引、目录的节编号都重建了
        doc = self.e.rt.store.one("SELECT * FROM kb_docs WHERE library_id=? AND path LIKE '%差旅%'", (self.lib["id"],))
        st, card = self.req("GET", "/api/kb/docs/%s/card" % doc["id"])
        self.assertEqual(card["tags"], ["财务部", "报销"])
        self.assertTrue(card["outline"])
        sec = card["outline"][0]["section_id"]
        self.assertEqual(self.req("GET", "/api/kb/sections/%s" % sec)[0], 200)
        self.assertEqual(self.e.rt.store.one("SELECT COUNT(*) AS n FROM kb_docfts WHERE library_id=?", (self.lib["id"],))["n"], 3)
        # 原文件：source_root + 相对路径
        st, raw, _ = self.req("GET", "/api/kb/docs/%s/file" % doc["id"], raw=True)
        self.assertEqual(st, 200)
        self.assertIn("800", raw.decode("utf-8"))
        # 只读
        lid = self.lib["id"]
        self.assertEqual(self.req("PATCH", "/api/libraries/%s" % lid, {"name": "改名"})[0], 400)
        self.assertEqual(self.req("POST", "/api/libraries/%s/sources" % lid, {"path": self.src})[0], 400)
        self.assertEqual(self.req("POST", "/api/libraries/%s/reindex" % lid)[0], 400)
        self.assertEqual(self.req("DELETE", "/api/libraries/%s" % lid)[0], 400)
        self.assertEqual(self.req("DELETE", "/api/kb/%s" % doc["id"])[0], 400)
        self.assertEqual(self.req("PATCH", "/api/kb/docs/%s" % doc["id"], {"tags": ["x"]})[0], 400)
        self.assertEqual(self.req("POST", "/api/kb/docs/%s/card" % doc["id"], {"action": "regenerate"})[0], 400)
        self.assertEqual(self.req("POST", "/api/libraries/%s/export" % lid, {})[0], 400)
        # 本机的定时同步不会把它当成"文件都没了"删光
        self.e.rt.kb.indexer.enqueue(lid)
        self.assertTrue(self.e.rt.kb.indexer.wait_idle(10))
        self.assertEqual(self.e.rt.store.one("SELECT COUNT(*) AS n FROM kb_docs WHERE library_id=?", (lid,))["n"], 3)
        # 停用：不参与检索；启用回来
        st, me = self.req("PATCH", "/api/libraries/%s" % lid, {"enabled": False})
        self.assertEqual((st, me["enabled"]), (200, False))
        self.assertEqual(self.search("总监住宿标准")["hits"], [])
        self.assertEqual(self.req("PATCH", "/api/libraries/%s" % lid, {"enabled": "no"})[0], 400)
        self.req("PATCH", "/api/libraries/%s" % lid, {"enabled": True})
        self.assertTrue(self.search("总监住宿标准")["hits"])
        # 原文件连不上
        os.rename(self.src, self.src + "-x")
        try:
            st, err = self.req("GET", "/api/kb/docs/%s/file" % doc["id"])
            self.assertEqual(st, 404)
            self.assertIn("原文件在共享盘上，现在连不上", err["error"])
            self.assertTrue(self.search("总监住宿标准")["hits"])        # 片段文字照样能查
        finally:
            os.rename(self.src + "-x", self.src)
        # 共享目录清空之后：库还在（照样能查），这时可以删
        self.assertEqual(self.req("POST", "/api/settings", {"shared": {"dir": ""}})[0], 200)
        self.assertTrue(self.search("总监住宿标准")["hits"])
        self.assertEqual(self.req("DELETE", "/api/libraries/%s" % lid)[0], 200)
        self.assertFalse(kbm.library(self.e.rt.store, lid))

    def test_version_bump_and_failed_import_keeps_old(self):
        self.export()
        self.configure()
        lid = self.lib["id"]
        # 管资料的人改了文件、再导出一次：版本 2，员工同步后看到新内容
        write_text(os.path.join(self.src, "财务部/报销/差旅管理办法.md"),
                   "# 差旅管理办法\n\n## 住宿标准\n\n总监级在一线城市住宿每晚不超过 950 元。\n")
        self.admin.rt.kb.indexer.enqueue(lid)
        self.assertTrue(self.admin.rt.kb.indexer.wait_idle(20))
        self.assertEqual(self.export()["version"], 2)
        r = self.req("POST", "/api/shared/check")[1]
        lib = [x for x in r["libraries"] if x["id"] == lid][0]
        self.assertEqual((lib["version"], lib["local_version"], lib["state"]), (2, 2, "ok"))
        self.assertIn("950", self.search("总监住宿标准")["hits"][0]["text"])
        # 版本 3 的包是坏的：导入失败，本机还是版本 2 的内容
        pack = os.path.join(self.share, "libraries", "公司制度")
        man = read_json(os.path.join(pack, "manifest.json"))
        man["version"] = 3
        write_json(os.path.join(pack, "manifest.json"), man)
        with open(os.path.join(pack, "index.db"), "r+b") as f:
            f.seek(200)
            f.write(b"garbage" * 50)
        r = self.req("POST", "/api/shared/check")[1]
        lib = [x for x in r["libraries"] if x["id"] == lid][0]
        self.assertEqual((lib["version"], lib["local_version"], lib["state"]), (3, 2, "error"))
        self.assertIn("对不上", lib["error"])
        self.assertIn("950", self.search("总监住宿标准")["hits"][0]["text"])

    def test_malicious_pack(self):
        self.export()
        pack = os.path.join(self.share, "libraries", "公司制度")
        db = os.path.join(pack, "index.db")
        con = sqlite3.connect(db)
        con.execute("UPDATE docs SET path='../../etc/passwd' WHERE path LIKE '%差旅%'")
        con.execute("CREATE TABLE evil(x)")
        con.commit()
        con.close()
        man = read_json(os.path.join(pack, "manifest.json"))
        man.pop("sha256")                        # 没有校验和的包也认，但内容照样逐项查
        write_json(os.path.join(pack, "manifest.json"), man)
        # 另一个包：id 带路径分隔符
        write_json(os.path.join(self.share, "libraries", "坏包", "manifest.json"), dict(man, id="../../x"))
        r = self.configure()
        self.assertTrue(r["ok"])
        lib = [x for x in r["libraries"] if x["id"] == self.lib["id"]][0]
        self.assertEqual(lib["state"], "error")
        self.assertIn("文档路径不合格", lib["error"])
        self.assertTrue(any(p["file"] == "libraries/坏包/manifest.json" for p in r["problems"]))
        self.assertFalse(kbm.library(self.e.rt.store, self.lib["id"]))

    def test_vectors_signature(self):
        fake = FakeEmbedding()
        try:
            emb = {"engine": "openai", "base_url": fake.base_url, "model": "fake-embed"}
            self.admin.cfg.update({"embedding": emb})
            self.admin.rt.kb.indexer.enqueue(self.lib["id"])
            self.assertTrue(self.admin.rt.kb.indexer.wait_idle(20))
            self.assertTrue(wait_until(lambda: self.admin.rt.store.one("SELECT COUNT(*) AS n FROM kb_vectors")["n"] > 0, 10))
            ex = self.export()
            self.assertTrue(ex["vectors"])
            man = read_json(os.path.join(self.share, "libraries", "公司制度", "manifest.json"))
            self.assertEqual(man["embedding"]["model"], "fake-embed")
            self.assertEqual(man["embedding"]["vec_dims"], 256)
            # 员工：本机没配向量模型 → 丢掉向量，只用关键词
            r = self.configure()
            lib = [x for x in r["libraries"] if x["id"] == self.lib["id"]][0]
            self.assertEqual(lib["vectors"], "mismatch")
            self.assertEqual(self.e.rt.store.one("SELECT COUNT(*) AS n FROM kb_vectors")["n"], 0)
            self.assertTrue(self.search("总监住宿标准")["hits"])
            # 员工配上同一个向量模型：下一次同步重新导入，用上包里的向量（不用自己算）
            os.remove(os.path.join(self.share, "settings.json"))     # 去掉锁住的 embedding，免得它盖掉本机设置
            self.req("POST", "/api/shared/check")
            self.assertEqual(self.req("POST", "/api/settings", {"embedding": emb})[0], 200)
            r = self.req("POST", "/api/shared/check")[1]
            lib = [x for x in r["libraries"] if x["id"] == self.lib["id"]][0]
            self.assertEqual(lib["vectors"], "ok", lib)
            nvec = self.e.rt.store.one("SELECT COUNT(*) AS n FROM kb_vectors WHERE library_id=?", (self.lib["id"],))["n"]
            self.assertEqual(nvec, ex["parts"])
            self.assertTrue(self.e.rt.kb.indexer.wait_idle(20))
            # 用的是包里的那几条向量（和管资料的人电脑上的一模一样）
            a = {r["text"]: r["vec"] for r in self.admin.rt.store.q("SELECT p.text, v.vec FROM kb_vectors v JOIN kb_parts p ON p.id=v.part_id")}
            b = {r["text"]: r["vec"] for r in self.e.rt.store.q("SELECT p.text, v.vec FROM kb_vectors v JOIN kb_parts p ON p.id=v.part_id")}
            self.assertEqual(a, b)
            res = self.search("总监住宿标准")
            self.assertTrue(res["hits"])
            self.assertIn("vector", json.dumps(res["used"]))
        finally:
            fake.close()

    def test_export_target_checks(self):
        self.assertEqual(self.req("POST", "/api/libraries/%s/export" % "nope", {})[0], 404)
        sh = self.admin.rt.shared
        with self.assertRaises(ValueError):
            sh.export_library(self.lib["id"])            # 没配共享目录
        other = os.path.join(self.share_tmp, "别处")
        write_json(os.path.join(other, "manifest.json"), {"format": 1, "id": "L_other", "name": "别的库", "version": 4})
        with self.assertRaises(ValueError):
            sh.export_library(self.lib["id"], other)     # 别人的包，不覆盖
        out = sh.export_library(self.lib["id"], os.path.join(self.share_tmp, "自选"), source_root="\\\\fs\\制度")
        man = read_json(os.path.join(out["dir"], "manifest.json"))
        self.assertEqual(man["source_root"], "\\\\fs\\制度")
        with self.assertRaises(ValueError):
            sh.export_library(self.lib["id"], "relative")


class Thread(Case):
    def test_background_check_and_clean_stop(self):
        tmp = tempfile.mkdtemp(prefix="ca_share_")
        try:
            share = os.path.join(tmp, "s")
            make_share(share)
            e = self.env(shared={"dir": share, "sync_minutes": 30})
            e.restart()
            self.assertTrue(wait_until(lambda: e.rt.shared.status.get("ok"), 10))
            self.assertEqual(e.rt.cfg.get("kb", "min_sim"), 0.4)
            t0 = time.time()
            e.rt.shutdown()
            self.assertLess(time.time() - t0, 6)
            self.assertFalse(e.rt.shared.thread.is_alive())
            e._closed = True
            shutil.rmtree(e.tmp, ignore_errors=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
