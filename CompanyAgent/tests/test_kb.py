"""资料库、按文档类型切片、混合检索、三个资料工具（docs/两种模式.md 第 2、3、7、9.2 节）。

向量用 tests/fake_openai.py 的假向量服务（字符二元组、三元组哈希成固定长度）；样例文件现场用 python-docx、python-pptx、
openpyxl 造，PDF 手写一个带文本层的最小文件。"""
from __future__ import annotations
import json, os, sqlite3, tempfile, time, unittest

from helpers import Case, ApiCase, Env, wait_until, APP
from fake_openai import FakeEmbedding
from agent import docparse, kb as kbm, context, capability, modes
from agent.kb import retrieve, embed as emb
from agent.tools import REGISTRY, ToolError
from agent.policy import PathError
from agent.loop import Runner
from agent.store import Store

SOP = ("差旅管理办法（2026 版）.docx")


def make_docx(path, title_style=True):
    import docx
    d = docx.Document()
    d.add_heading("差旅管理办法", 0)
    d.add_paragraph("第一章 总则")
    d.add_paragraph("为规范公司差旅行为，制定本办法。本办法适用于全体员工。")
    d.add_heading("第四章 住宿", 1)
    d.add_paragraph("4.1 适用范围")
    d.add_paragraph("出差期间的住宿费用按本章执行。")
    d.add_paragraph("4.2 住宿标准")
    d.add_paragraph("总监级在一线城市每晚不超过 800 元，其他城市不超过 600 元。经理级在一线城市每晚不超过 500 元。")
    d.add_paragraph("（一）超标处理")
    d.add_paragraph("超出标准的部分由个人承担，确需超标的须事先经分管副总审批。")
    d.add_paragraph("步骤 4 报销时附上酒店发票")
    d.add_paragraph("酒店发票抬头写公司全称。")
    d.add_heading("第五章 交通", 1)
    long = "".join("第%d条规定：乘坐交通工具按职级执行，具体以财务部公布的标准为准。" % i for i in range(1, 60))
    d.add_paragraph(long)
    d.save(path)


def make_pptx(path):
    import pptx
    from pptx.util import Inches
    prs = pptx.Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "2026 年三季度经营汇报"
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "华东区销售回顾"
    tf = s.placeholders[1].text_frame
    tf.text = "销售额 1.2 亿元"
    p = tf.add_paragraph()
    p.text = "同比增长 12.4%"
    p.level = 1
    s.notes_slide.notes_text_frame.text = "先讲结论再讲原因"
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "各区域对比"
    t = s.shapes.add_table(3, 2, Inches(1), Inches(2), Inches(6), Inches(2)).table
    for r, row in enumerate([["区域", "销售额"], ["华东", "1.2 亿"], ["华南", "0.9 亿"]]):
        for c, v in enumerate(row):
            t.cell(r, c).text = v
    prs.save(path)


def make_xlsx(path, rows=80, bump=None):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "明细"
    ws.append(["区域", "科目", "预算"])
    for i in range(rows):
        ws.append(["华东" if i % 2 else "华南", "科目%03d" % i, 1000 + i + ((bump or {}).get(i, 0))])
    qa = wb.create_sheet("问答")
    qa.append(["问题", "答案"])
    qa.append(["报销多久到账？", "审批通过后 5 个工作日内到账。"])
    wb.save(path)


def make_pdf(path, pages):
    """手写的最小 PDF：每页一段 ASCII 文字（Helvetica）。pages 是每页的行列表；空列表表示这页没有文字（像扫描件）。"""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        ops = "BT /F1 12 Tf 72 720 Td 16 TL " + " ".join("(%s) Tj T*" % ln.replace("(", "\\(").replace(")", "\\)")
                                                       for ln in lines) + " ET" if lines else ""
        stream = ops.encode("latin-1")
        objs.append("<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), ops))
        content_no = len(objs)
        objs.append("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                    "/Contents %d 0 R >>" % content_no)
        kids.append(len(objs))
    objs[1] = "<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join("%d 0 R" % k for k in kids), len(kids))
    out = b"%PDF-1.4\n"
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += ("%d 0 obj\n%s\nendobj\n" % (i, o)).encode("latin-1")
    x = len(out)
    out += ("xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)).encode()
    out += b"".join(("%010d 00000 n \n" % o).encode() for o in offs)
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)).encode()
    with open(path, "wb") as f:
        f.write(out)


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


# ======================================================================
class Parse(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ca_parse_")

    def p(self, name):
        return os.path.join(self.d, name)

    def test_docx_headings_paths_and_sizes(self):
        make_docx(self.p(SOP))
        r = docparse.parse_file(self.p(SOP))
        self.assertEqual(r["kind"], "doc")
        locs = [s["loc"] for s in r["sections"]]
        # 样式是标题的（第四章 住宿）和样式是正文但长得像标题的（第一章、4.2、（一）、步骤 4）都认出来
        for want in ("第一章 总则", "4.1 适用范围", "4.2 住宿标准", "（一）超标处理", "步骤 4 报销时附上酒店发票"):
            self.assertIn(want, locs)
        sec = next(s for s in r["sections"] if s["loc"] == "4.2 住宿标准")
        self.assertEqual(sec["heading"], "第四章 住宿 > 4.2 住宿标准")
        self.assertIn("800 元", sec["text"])
        sub = next(s for s in r["sections"] if s["loc"] == "（一）超标处理")
        self.assertEqual(sub["heading"], "第四章 住宿 > 4.2 住宿标准 > （一）超标处理")
        step = next(s for s in r["sections"] if s["loc"].startswith("步骤 4"))
        self.assertTrue(step["heading"].startswith("第四章 住宿 > "))
        # 长的一节按段落（句子）切，每个片段不超过 600，相邻片段重叠一句
        traffic = next(s for s in r["sections"] if s["loc"] == "第五章 交通")
        self.assertGreater(len(traffic["parts"]), 2)
        for part in traffic["parts"]:
            self.assertLessEqual(part["tokens"], docparse.MAX)
            self.assertEqual(part["heading"], "第五章 交通")
        a, b = traffic["parts"][0]["text"], traffic["parts"][1]["text"]
        self.assertTrue(b.split("。")[0] in a, "相邻片段应重叠一句")

    def test_heading_detection(self):
        f = docparse.heading_family
        for t in ("第三章 费用报销", "3.2 审批流程", "3.2审批流程", "步骤 4", "（一）总体要求", "一、总则", "第十二条 附则"):
            self.assertTrue(f(t), t)
        for t in ("总监级在一线城市每晚不超过 800 元。", "2026年公司继续加大投入", "3.2 审批流程。", "这是一段普通的话",
                  "1.5%的费率", "x" * 60):
            self.assertFalse(f(t), t)

    def test_text_and_markdown(self):
        write(self.d, "a.md", "# 费用报销 SOP\n\n## 1. 准备单据\n\n发票原件和审批单。\n\n## 2. 提交\n\n在 OA 里提交。\n")
        r = docparse.parse_file(self.p("a.md"))
        self.assertEqual([s["heading"] for s in r["sections"]], ["费用报销 SOP > 1. 准备单据", "费用报销 SOP > 2. 提交"])
        write(self.d, "b.txt", "没有标题的一段话。\n\n第二段话。")
        r = docparse.parse_file(self.p("b.txt"))
        self.assertEqual(r["sections"][0]["loc"], "第 1 段")
        self.assertEqual(r["kind"], "text")

    def test_pdf_pages_and_scanned(self):
        make_pdf(self.p("p.pdf"), [["Expense policy overview."], ["3.2 Approval flow", "Manager approves within 3 days."],
                                   ["Second line on page three."]])
        r = docparse.parse_file(self.p("p.pdf"))
        self.assertEqual(r["meta"]["pages"], 3)
        locs = [s["loc"] for s in r["sections"]]
        self.assertIn("第 2 页 · 3.2 Approval flow", locs)
        self.assertIn("第 3 页 · 3.2 Approval flow", locs)          # 跨页的标题路径接着用
        make_pdf(self.p("scan.pdf"), [[], []])
        with self.assertRaises(docparse.ParseError) as cm:
            docparse.parse_file(self.p("scan.pdf"))
        self.assertEqual(str(cm.exception), "这份 PDF 是扫描件，读不出文字")

    def test_pptx_one_part_per_slide(self):
        make_pptx(self.p("deck.pptx"))
        r = docparse.parse_file(self.p("deck.pptx"))
        self.assertEqual(r["kind"], "slides")
        self.assertEqual(r["meta"]["slides"], 3)
        self.assertEqual([len(s["parts"]) for s in r["sections"]], [1, 1, 1])
        s2 = r["sections"][1]
        self.assertEqual(s2["loc"], "第 2 页 · 华东区销售回顾")
        for t in ("华东区销售回顾", "销售额 1.2 亿元", "同比增长 12.4%", "备注：先讲结论再讲原因"):
            self.assertIn(t, s2["text"])
        self.assertIn("华南 | 0.9 亿", r["sections"][2]["text"])           # 表格里的字

    def test_xlsx_overview_groups_and_qa(self):
        make_xlsx(self.p("预算.xlsx"))
        r = docparse.parse_file(self.p("预算.xlsx"))
        self.assertEqual(r["kind"], "sheet")
        ov = r["sections"][0]
        self.assertEqual(ov["loc"], "工作表「明细」· 概况")
        self.assertIn("共 80 行数据、3 列", ov["text"])
        groups = [s for s in r["sections"] if "行" in s["loc"] and "明细" in s["loc"] and "概况" not in s["loc"]]
        self.assertGreater(len(groups), 1)
        self.assertTrue(groups[0]["loc"].startswith("工作表「明细」· 第 2–"))
        for g in groups:
            self.assertTrue(g["text"].startswith("表头：区域 | 科目 | 预算"))
            self.assertLessEqual(g["tokens"], docparse.MAX)
        qa = [s for s in r["sections"] if "问答" in s["loc"]]
        self.assertEqual(qa[0]["text"], "问：报销多久到账？\n答：审批通过后 5 个工作日内到账。")
        self.assertEqual(qa[0]["loc"], "工作表「问答」· 第 2 行")
        write(self.d, "faq.csv", "question,answer\n发票丢了怎么办？,找开票方补开并说明情况。\n")
        r = docparse.parse_file(self.p("faq.csv"))
        self.assertEqual(r["kind"], "qa")
        self.assertIn("答：找开票方补开并说明情况。", r["sections"][0]["text"])

    def test_versions(self):
        v = docparse.version_info
        self.assertEqual(v("差旅管理办法（2026 版）.docx")["family"], "差旅管理办法")
        self.assertEqual(v("差旅管理办法（2026 版）.docx")["version_label"], "2026 版")
        self.assertEqual(v("差旅管理办法2025版.docx")["family"], "差旅管理办法")
        self.assertEqual(v("费用报销SOP_V3.docx")["version_label"], "V3")
        self.assertEqual(v("预算表_2025-06.xlsx")["version_label"], "2025-06")
        self.assertEqual(v("经营汇报20250601.pptx")["version_label"], "2025-06-01")
        self.assertTrue(docparse.newer(v("x_V10.docx", 1), v("x_V9.docx", 2)))
        self.assertTrue(docparse.newer(v("x（2026 版）.docx"), v("x（2025 版）.docx")))
        self.assertEqual(v("第三季度经营汇报.pptx", 123.0)["effective"], 123.0)


# ======================================================================
OLD_SCHEMA = """
CREATE TABLE workspaces(id TEXT PRIMARY KEY, name TEXT, path TEXT, instructions TEXT DEFAULT '', egress TEXT DEFAULT 'intranet', created REAL);
CREATE TABLE sessions(id TEXT PRIMARY KEY, workspace_id TEXT, title TEXT, status TEXT, revision INTEGER DEFAULT 1,
  kind TEXT DEFAULT 'task', parent_id TEXT, meta TEXT DEFAULT '{}', created REAL, updated REAL);
CREATE TABLE kb_docs(id TEXT PRIMARY KEY, workspace_id TEXT, path TEXT, title TEXT, mtime REAL, chunks INTEGER, added REAL);
CREATE VIRTUAL TABLE kb_chunks USING fts5(text, doc_id UNINDEXED, loc UNINDEXED, workspace_id UNINDEXED, tokenize='trigram');
"""


class Migration(Case):
    def test_old_ledger_opens_and_migrates_once(self):
        tmp = tempfile.mkdtemp(prefix="ca_mig_")
        home, ws = os.path.join(tmp, "home"), os.path.join(tmp, "ws")
        os.makedirs(home)
        write(ws, "资料/售后手册.txt", "错误码 E-1023：先断电重启。")
        write(ws, "根目录说明.md", "差旅报销三十天内提交。")
        db = sqlite3.connect(os.path.join(home, "ledger.db"))
        db.executescript(OLD_SCHEMA)
        db.execute("INSERT INTO workspaces VALUES('w_1','售后组',?, '', 'intranet', 1.0)", (ws,))
        db.execute("INSERT INTO sessions(id,workspace_id,title,status,created,updated) VALUES('s_old','w_1','老对话','done',1,1)")
        for did, path, text in (("d_1", "资料/售后手册.txt", "错误码 E-1023：先断电重启。"),
                                ("d_2", "根目录说明.md", "差旅报销三十天内提交。")):
            db.execute("INSERT INTO kb_docs VALUES(?,?,?,?,?,?,?)", (did, "w_1", path, os.path.basename(path), 1.0, 1, 1.0))
            db.execute("INSERT INTO kb_chunks(text,doc_id,loc,workspace_id) VALUES(?,?,?,?)", (text, did, "", "w_1"))
        db.commit()
        db.close()
        st = Store(home)
        try:
            self.assertEqual(st.session("s_old")["mode"], "cariai")
            libs = st.q("SELECT * FROM kb_libraries")
            self.assertEqual([(l["name"], l["workspace_id"]) for l in libs], [("售后组", "w_1")])
            srcs = sorted((s["kind"], os.path.relpath(s["path"], ws)) for s in st.q("SELECT * FROM kb_sources"))
            self.assertEqual(srcs, [("dir", "资料"), ("file", "根目录说明.md")])
            docs = {d["id"]: d for d in st.q("SELECT * FROM kb_docs")}
            self.assertEqual(docs["d_1"]["path"], "售后手册.txt")
            self.assertEqual(docs["d_1"]["library_id"], libs[0]["id"])
            self.assertEqual(st.one("SELECT COUNT(*) AS n FROM kb_parts")["n"], 2)
            self.assertEqual(st.one("SELECT COUNT(*) AS n FROM kb_chunks")["n"], 2)          # 老表不动
            hits = retrieve.keyword(st, "E-1023", [libs[0]["id"]])
            self.assertEqual(len(hits), 1)
        finally:
            st.close()
        st = Store(home)          # 再开一次：不重复迁移
        try:
            self.assertEqual(st.one("SELECT COUNT(*) AS n FROM kb_libraries")["n"], 1)
            self.assertEqual(st.one("SELECT COUNT(*) AS n FROM kb_sources")["n"], 2)
            self.assertEqual(st.one("SELECT COUNT(*) AS n FROM kb_parts")["n"], 2)
        finally:
            st.close()
        # 运行时打开老库：同步时按新切法重读（size 是空的），老对话照常是 CariAi
        e = self.env(home=home, root=ws)
        self.assertTrue(e.rt.kb.indexer.wait_idle(10))
        d1 = e.rt.store.one("SELECT * FROM kb_docs WHERE id='d_1'")
        self.assertEqual((d1["status"], d1["size"] is not None), ("ok", True))
        self.assertEqual(e.rt.store.sessions(mode="cariai")[0]["id"], "s_old")
        self.assertEqual(e.rt.store.sessions(mode="hspace"), [])


# ======================================================================
class KBEnv(Case):
    """一套环境 + 一个资料库（来源是临时目录里的"制度"文件夹）。"""
    embed = False
    rerank = None

    def setUp(self):
        super().setUp()
        self.fake = None
        patch = {}
        if self.embed:
            self.fake = FakeEmbedding(rerank_fn=self.rerank)
            patch["embedding"] = {"engine": "openai", "base_url": self.fake.base_url, "model": "fake-embed"}
            # 假向量服务（字符二元组哈希）意思相近的句子相似度也只有 0.1 左右：门槛按它调低（真模型用默认的 0.3）
            patch["kb"] = {"min_sim": 0.05}
            if self.rerank:
                patch["rerank"] = {"engine": "vllm", "base_url": self.fake.base_url, "model": "fake-rerank"}
        self.e = self.env([], mode="full", **patch)
        self.lib_dir = os.path.join(self.e.tmp, "制度")
        os.makedirs(self.lib_dir)

    def tearDown(self):
        super().tearDown()
        if self.fake:
            self.fake.close()

    def add(self, rel, text):
        return write(self.lib_dir, rel, text)

    def library(self, name="公司制度"):
        lib = kbm.create_library(self.e.rt.store, name)
        kbm.add_source(self.e.rt.store, lib["id"], self.lib_dir)
        self.index(lib["id"])
        return lib

    def index(self, lid):
        self.e.rt.kb.indexer.enqueue(lid)
        self.assertTrue(self.e.rt.kb.indexer.wait_idle(20))

    def ctx(self, mode="hspace"):
        sid = self.e.rt.store.add_session(self.e.ws["id"], mode=mode)["id"]
        return Runner(self.e.rt, sid, self.e.mock).ctx

    def search(self, ctx, **a):
        return REGISTRY["kb_search"].handler(ctx, **a)


class Ingest(KBEnv):
    def test_incremental_failures_and_progress(self):
        make_docx(os.path.join(self.lib_dir, SOP))
        os.makedirs(os.path.join(self.lib_dir, "汇报"))
        make_pptx(os.path.join(self.lib_dir, "汇报/三季度经营汇报.pptx"))
        make_xlsx(os.path.join(self.lib_dir, "预算.xlsx"))
        self.add("说明.txt", "这是一份说明。")
        self.add("~$临时.docx", "x")
        self.add(".隐藏/a.txt", "x")
        with open(os.path.join(self.lib_dir, "坏文件.docx"), "wb") as f:
            f.write(b"not a zip")
        make_pdf(os.path.join(self.lib_dir, "扫描件.pdf"), [[]])
        q = self.e.rt.subscribe()
        lib = self.library()
        store = self.e.rt.store
        docs = {d["title"]: d for d in store.q("SELECT * FROM kb_docs WHERE library_id=?", (lib["id"],))}
        self.assertEqual(set(docs), {SOP, "三季度经营汇报.pptx", "预算.xlsx", "说明.txt", "坏文件.docx", "扫描件.pdf"})
        self.assertEqual(docs["三季度经营汇报.pptx"]["path"], "汇报/三季度经营汇报.pptx")
        self.assertEqual(docs["坏文件.docx"]["status"], "failed")
        self.assertIn("读不出来", docs["坏文件.docx"]["error"])
        self.assertEqual(docs["扫描件.pdf"]["error"], "这份 PDF 是扫描件，读不出文字")
        self.assertEqual((docs[SOP]["kind"], docs[SOP]["family"], docs[SOP]["version_label"]), ("doc", "差旅管理办法", "2026 版"))
        sm = kbm.summary(self.e.rt.kb, kbm.library(store, lib["id"]))
        self.assertEqual((sm["docs"], sm["failed"], sm["state"]), (4, 2, "ready"))
        self.assertIsNotNone(sm["synced"])
        evs = []
        while not q.empty():
            ev = q.get_nowait()
            if ev and ev["type"] == "kb_progress":
                evs.append(ev["payload"])
        self.e.rt.unsubscribe(q)
        self.assertTrue(evs)
        self.assertEqual(set(evs[-1]), {"library_id", "total", "done", "failed", "current", "state"})
        self.assertEqual((evs[-1]["total"], evs[-1]["done"], evs[-1]["failed"], evs[-1]["state"]), (6, 6, 2, "ready"))
        # 增量：没变的不重读
        calls = []
        orig = docparse.parse_file
        docparse.parse_file = lambda p: (calls.append(os.path.basename(p)), orig(p))[1]
        try:
            self.index(lib["id"])
            self.assertEqual(calls, [])
            time.sleep(0.01)
            self.add("说明.txt", "这是一份改过的说明，内容变长了。")
            os.remove(os.path.join(self.lib_dir, "预算.xlsx"))
            self.index(lib["id"])
            self.assertEqual(calls, ["说明.txt"])
        finally:
            docparse.parse_file = orig
        titles = {d["title"] for d in store.q("SELECT title FROM kb_docs WHERE library_id=?", (lib["id"],))}
        self.assertNotIn("预算.xlsx", titles)
        self.assertEqual(store.one("SELECT COUNT(*) AS n FROM kb_parts p LEFT JOIN kb_docs d ON d.id=p.doc_id "
                                   "WHERE d.id IS NULL")["n"], 0)
        self.assertIn("改过的说明", " ".join(r["text"] for r in store.q("SELECT text FROM kb_parts")))

    def test_skipped_and_limits(self):
        self.e.cfg.update({"kb": {"max_file_mb": 0.00001, "max_files": 5000}})
        self.add("大文件.txt", "字" * 2000)
        lib = self.library()
        d = self.e.rt.store.one("SELECT * FROM kb_docs WHERE library_id=?", (lib["id"],))
        self.assertEqual(d["status"], "skipped")
        self.assertIn("没有收录", d["error"])

    def test_resume_after_restart(self):
        self.add("a.txt", "报销要在三十天内提交。")
        lib = kbm.create_library(self.e.rt.store, "库")
        kbm.add_source(self.e.rt.store, lib["id"], self.lib_dir)
        self.e.rt.store.meta_set("kb_jobs", json.dumps([lib["id"]]))      # 上次排着没做的任务
        self.e.restart()
        self.assertTrue(self.e.rt.kb.indexer.wait_idle(10))
        self.assertEqual(self.e.rt.store.one("SELECT COUNT(*) AS n FROM kb_docs WHERE status='ok'")["n"], 1)
        self.assertEqual(json.loads(self.e.rt.store.meta_get("kb_jobs")), [])

    def test_shutdown_stops_thread(self):
        for i in range(30):
            self.add("f%02d.txt" % i, "第 %d 份文件。" % i * 50)
        lib = kbm.create_library(self.e.rt.store, "库")
        kbm.add_source(self.e.rt.store, lib["id"], self.lib_dir)
        self.e.rt.kb.indexer.enqueue(lib["id"])
        t0 = time.time()
        self.e.rt.shutdown()
        self.assertLess(time.time() - t0, 6)
        self.assertFalse(self.e.rt.kb.indexer.thread.is_alive())
        self.e._closed = True


class Search(KBEnv):
    def setUp(self):
        super().setUp()
        make_docx(os.path.join(self.lib_dir, SOP))
        self.add("差旅管理办法（2025 版）.txt", "4.2 住宿标准\n总监级在一线城市每晚不超过 700 元。\n")
        self.add("报销问答.csv", "问题,答案\n报销多久到账？,审批通过后 5 个工作日内到账。\n")
        self.lib = self.library()

    def test_keyword_only_and_feature(self):
        f = capability.semantic_feature(self.e.rt)
        self.assertEqual((f["ok"], f["reason_kind"]), (False, "setting"))
        self.assertEqual(f["reason"], "还没有接入向量模型，无法使用「语义检索」。")
        self.assertEqual(f["fallback"], "现在用关键词检索：要用资料里出现过的词来问。")
        ctx = self.ctx()
        r = self.search(ctx, query="住宿标准")
        self.assertEqual(r.extra["used"], {"keyword": True, "vector": False, "rerank": False})
        hits = r.extra["hits"]
        self.assertTrue(hits)
        self.assertTrue(all(h["via"] == "keyword" for h in hits))
        self.assertEqual(set(hits[0]), set(retrieve.PUBLIC_KEYS) | {"library_id"} | ({"parts"} if "parts" in hits[0] else set()))
        self.assertTrue(r.startswith(retrieve.BEGIN))
        self.assertTrue(r.rstrip().endswith(retrieve.END))
        self.assertEqual(r.extra["display"], {"kind": "kb", "title": "在资料里找「住宿标准」", "page": "", "host": "", "text": ""})
        # 新旧版本：新的排前面，旧的标出来
        first = hits[0]
        self.assertEqual((first["title"], first["superseded"]), (SOP, False))
        old = next(h for h in hits if h["title"].startswith("差旅管理办法（2025"))
        self.assertTrue(old["superseded"])
        self.assertEqual(old["newer"], "《差旅管理办法（2026 版）》")
        self.assertIn("（旧版，现行的是《差旅管理办法（2026 版）》）", r)
        self.assertLess(r.index("800 元"), r.index("700 元"))
        self.assertFalse(any(h["standard"] for h in hits))
        # Hspace 系统提示里有那句原话
        sysp = context.system_prompt(self.e.rt, self.e.ws, mode="hspace")
        self.assertIn("还没有接入向量模型，无法使用「语义检索」。现在用关键词检索", sysp)
        self.assertNotIn("语义检索", context.system_prompt(self.e.rt, self.e.ws, mode="cariai"))

    def test_qa_whole_answer_and_kind_filter(self):
        ctx = self.ctx()
        r = self.search(ctx, query="报销多久到账", kind="sheet")
        h = r.extra["hits"]
        self.assertEqual([x["kind"] for x in h], ["qa"])
        self.assertIn("答：审批通过后 5 个工作日内到账。", h[0]["text"])
        r = self.search(ctx, query="报销多久到账", kind="slides")
        self.assertEqual(r.extra["hits"], [])
        self.assertIn("资料库里没有找到相关内容。如实告诉用户查不到，不要编造。", r)

    def test_numbering_reuse_and_kb_open(self):
        ctx = self.ctx()
        a = self.search(ctx, query="住宿标准").extra["hits"]
        self.assertEqual([h["n"] for h in a], list(range(1, len(a) + 1)))
        # 直接调处理函数不写账本：把结果写进账本再查第二次
        from agent.loop import write_result
        write_result(self.e.rt, ctx.sid, {"id": "c1", "name": "kb_search"}, True, "x", extra={"hits": a})
        b = self.search(ctx, queries=["住宿标准", "报销多久到账"]).extra["hits"]
        by_part = {h["part_id"]: h["n"] for h in a}
        for h in b:
            if h["part_id"] in by_part:
                self.assertEqual(h["n"], by_part[h["part_id"]])        # 同一个片段还用原来的编号
            else:
                self.assertGreater(h["n"], len(a))                    # 新的从后面接着编
        o = REGISTRY["kb_open"].handler(ctx, ref="[1]")
        self.assertIn("[1] ", o)
        self.assertIn("（整节）", o)
        self.assertEqual(o.extra["hits"][0]["n"], 1)
        with self.assertRaises(ToolError):
            REGISTRY["kb_open"].handler(ctx, ref="99")
        f = REGISTRY["kb_open"].handler(ctx, ref="差旅管理办法（2026 版）")
        self.assertIn("4.2 住宿标准", f)
        self.assertIn("（全文到此结束）", f)
        with self.assertRaises(ToolError):
            REGISTRY["kb_open"].handler(ctx, ref="差旅管理办法")        # 两份都对得上：要写全

    def test_kb_open_in_parts_with_more(self):
        self.e.cfg.update({"model": {"context_window": 8192}})
        self.add("长文.txt", "\n\n".join("第%d节 标题\n" % i + "内容很长。" * 120 for i in range(1, 9)))
        self.index(self.lib["id"])
        ctx = self.ctx()
        from agent.loop import write_result
        r1 = REGISTRY["kb_open"].handler(ctx, ref="长文.txt")
        self.assertIn("more=true", r1)
        write_result(self.e.rt, ctx.sid, {"id": "o1", "name": "kb_open"}, True, str(r1), extra=r1.extra)
        r2 = REGISTRY["kb_open"].handler(ctx, ref="长文.txt", more=True)
        self.assertNotEqual(r1.extra["open"]["next"], r2.extra["open"]["next"])
        self.assertGreater(r2.extra["open"]["next"], r1.extra["open"]["next"])
        self.assertLessEqual(context.estimate_text(str(r1)), 8192 * 0.25 + 200)

    def test_kb_list(self):
        r = REGISTRY["kb_list"].handler(self.ctx())
        self.assertEqual(r["libraries"], [{"name": "公司制度", "files": 3, "failed": 0}])
        names = [f["name"] for f in r["files"]]
        self.assertIn("公司制度/" + SOP, names)
        qa = next(f for f in r["files"] if f["name"].endswith(".csv"))
        self.assertEqual((qa["kind"], qa["size"]), ("问答对", "1 行"))
        self.assertEqual(len(REGISTRY["kb_list"].handler(self.ctx(), query="2025")["files"]), 1)

    def test_scope_and_library_arg(self):
        other = os.path.join(self.e.tmp, "销售")
        write(other, "s.txt", "华东区住宿标准另有规定。")
        lib2 = kbm.create_library(self.e.rt.store, "销售资料")
        kbm.add_source(self.e.rt.store, lib2["id"], other)
        self.index(lib2["id"])
        ctx = self.ctx()
        self.assertEqual({h["library"] for h in self.search(ctx, query="住宿标准").extra["hits"]}, {"公司制度", "销售资料"})
        self.assertEqual({h["library"] for h in self.search(ctx, query="住宿标准", library="销售资料").extra["hits"]}, {"销售资料"})
        with self.assertRaises(ToolError):
            self.search(ctx, query="住宿标准", library="没有的库")
        s = self.e.rt.store.session(ctx.sid)
        self.e.rt.store.set_session(ctx.sid, meta=dict(s["meta"], libraries=[lib2["id"]]))
        self.assertEqual({h["library"] for h in self.search(ctx, query="住宿标准").extra["hits"]}, {"销售资料"})

    def test_budget(self):
        for i in range(12):
            self.add("长%02d.txt" % i, "住宿标准说明 %d。" % i + "住宿标准的细则很多。" * 300)
        self.index(self.lib["id"])
        self.e.cfg.update({"model": {"context_window": 8192}})
        ctx = self.ctx()
        r = self.search(ctx, query="住宿标准", top_k=10)
        self.assertEqual(retrieve.budget(8192), 1500)
        self.assertLessEqual(context.estimate_text(str(r)), 1500)
        self.assertEqual(retrieve.budget(32768), 5898)
        self.assertEqual(retrieve.budget(262144), 8000)

    def test_read_only_and_resolve_read(self):
        ctx = self.ctx("cariai")
        # resolve_read：资料库名/相对路径
        p = kbm.resolve_read(ctx, "公司制度/" + SOP)
        self.assertEqual(p, os.path.realpath(os.path.join(self.lib_dir, SOP)))
        r = REGISTRY["read_file"].handler(ctx, path="公司制度/报销问答.csv")
        self.assertEqual(r["path"], "公司制度/报销问答.csv")
        self.assertIn("报销多久到账", r["text"])
        names = [x["name"] for x in REGISTRY["list_dir"].handler(ctx, path="公司制度")["items"]]
        self.assertIn(SOP, names)
        found = REGISTRY["search_files"].handler(ctx, query="到账", path="公司制度")
        self.assertEqual(found["content_matches"][0]["file"], "公司制度/报销问答.csv")
        with self.assertRaises(PathError):
            kbm.resolve_read(ctx, "公司制度/../../../etc/passwd")
        with self.assertRaises(ToolError):
            kbm.resolve_read(ctx, "公司制度/../../../etc/passwd")
        os.symlink("/etc", os.path.join(self.lib_dir, "外面"))
        with self.assertRaises(PathError):
            kbm.resolve_read(ctx, "公司制度/外面/passwd")
        with self.assertRaises(PathError):
            REGISTRY["read_file"].handler(ctx, path="../../etc/passwd")      # 老规矩照旧
        # 资料库的来源在当前文件夹里：里面一律不能写
        inner = os.path.join(self.e.root, "SOP")
        write(inner, "流程.txt", "老流程")
        lib2 = kbm.create_library(self.e.rt.store, "流程库")
        kbm.add_source(self.e.rt.store, lib2["id"], inner)
        for tool, args in (("write_file", {"path": "SOP/流程.txt", "content": "x"}), ("write_file", {"path": "SOP/新.txt", "content": "x"}),
                           ("edit_file", {"path": "SOP/流程.txt", "old": "老", "new": "新"}),
                           ("delete_file", {"path": "SOP/流程.txt"}), ("move_file", {"src": "SOP/流程.txt", "dst": "b.txt"}),
                           ("write_docx", {"path": "SOP/x.docx", "markdown": "# x"})):
            risk = REGISTRY[tool].risk_of(ctx, args)
            msg = kbm.write_violation(ctx, tool, args, risk)
            self.assertTrue(msg and "只读" in msg, (tool, args))
        self.assertIsNone(kbm.write_violation(ctx, "write_file", {"path": "外面.txt", "content": "x"}, "create"))
        self.assertIsNone(kbm.write_violation(ctx, "read_file", {"path": "SOP/流程.txt"}, "read"))

    def test_read_only_through_loop(self):
        inner = os.path.join(self.e.root, "SOP")
        write(inner, "流程.txt", "老流程")
        lib2 = kbm.create_library(self.e.rt.store, "流程库")
        kbm.add_source(self.e.rt.store, lib2["id"], inner)
        self.e.mock.script[:] = [{"tool_calls": [("write_file", {"path": "SOP/流程.txt", "content": "改掉"})]}, {"content": "好"}]
        sid = self.e.rt.store.add_session(self.e.ws["id"], mode="hspace")["id"]
        self.e.rt.send_message(sid, "把流程改一下")
        self.assertTrue(self.e.rt.wait_idle(sid, 10))
        r = self.e.results(sid)[0]
        self.assertFalse(r["ok"])
        self.assertIn("只读", r["content"])
        with open(os.path.join(inner, "流程.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "老流程")
        self.assertEqual(self.e.events(sid, ("approval_requested",)), [])


class Vector(KBEnv):
    embed = True

    def setUp(self):
        super().setUp()
        self.add("住宿.txt", "出差住宿：总监级每晚不超过 800 元。")
        self.add("交通.txt", "交通工具：总监可乘坐飞机经济舱。")
        self.add("会议.txt", "会议纪要要在三天内发出。")
        self.lib = self.library()

    def test_vector_keyword_fusion_and_prefix(self):
        store = self.e.rt.store
        self.assertEqual(store.one("SELECT COUNT(*) AS n FROM kb_vectors")["n"], store.one("SELECT COUNT(*) AS n FROM kb_parts")["n"])
        f = capability.semantic_feature(self.e.rt)
        self.assertTrue(f["ok"])
        self.assertFalse(f["unknown"])
        doc_inputs = list(self.fake.inputs())
        self.assertTrue(doc_inputs and not any(x.startswith("Instruct:") for x in doc_inputs))   # 片段不加前缀
        ctx = self.ctx()
        # 关键词查不到（三字一组没有一样的），向量找得到
        self.assertEqual(retrieve.keyword(store, "住宿多少钱", [self.lib["id"]]), [])
        r = self.search(ctx, query="住宿多少钱")
        self.assertEqual(r.extra["used"]["vector"], True)
        self.assertEqual(r.extra["hits"][0]["title"], "住宿.txt")
        self.assertEqual(r.extra["hits"][0]["via"], "vector")
        q_inputs = self.fake.inputs()[len(doc_inputs):]
        self.assertEqual(q_inputs, [emb.DEFAULT_PREFIX + "住宿多少钱"])                     # 问题加前缀
        # 两路都命中
        both = self.search(ctx, query="总监级每晚住宿").extra["hits"]
        self.assertEqual(both[0]["via"], "both")
        # 向量服务掉线：退回关键词，功能状态跟着变
        self.fake.down = True
        r = self.search(ctx, query="会议纪要")
        self.assertEqual(r.extra["used"]["vector"], False)
        self.assertEqual(r.extra["hits"][0]["title"], "会议.txt")
        f = capability.semantic_feature(self.e.rt)
        self.assertEqual((f["ok"], f["reason_kind"], f["reason"]), (False, "model", "向量模型连不上，无法使用「语义检索」。"))

    def test_signature_change_reembeds(self):
        store = self.e.rt.store
        n = store.one("SELECT COUNT(*) AS n FROM kb_vectors")["n"]
        self.e.cfg.update({"embedding": {"dims": 64}})
        self.index(self.lib["id"])
        self.assertEqual(store.one("SELECT COUNT(*) AS n FROM kb_vectors WHERE dims=64")["n"], n)

    def test_component_missing(self):
        from agent.kb import vectors
        orig = vectors.has_numpy
        vectors.has_numpy = lambda: False
        try:
            f = capability.semantic_feature(self.e.rt)
            self.assertEqual((f["ok"], f["reason_kind"]), (False, "component"))
            r = self.search(self.ctx(), query="会议纪要")
            self.assertFalse(r.extra["used"]["vector"])
        finally:
            vectors.has_numpy = orig


class Rerank(KBEnv):
    embed = True
    rerank = staticmethod(lambda q, docs: [1.0 if "会议" in d else 0.0 for d in docs])

    def test_rerank_changes_order(self):
        self.add("a.txt", "住宿标准：总监每晚 800 元。")
        self.add("b.txt", "会议上也讨论了住宿标准。")
        lib = self.library()
        r = self.search(self.ctx(), query="住宿标准总监")
        self.assertTrue(r.extra["used"]["rerank"])
        self.assertEqual(r.extra["hits"][0]["title"], "b.txt")
        self.assertTrue(any(p == "/v1/rerank" for p, _ in self.fake.requests))


# ======================================================================
class Budgets(Case):
    """小型机的指标（docs/两种模式.md 第 7 节）。"""

    def test_tool_defs_and_prompt(self):
        e = self.env([])
        specs = [REGISTRY[n].spec() for n in ("kb_search", "kb_open", "kb_list")]
        self.assertLessEqual(context.estimate_tools(specs), 450)
        self.assertLessEqual(context.estimate_text(modes.HSPACE_SECTION), 350)
        h = context.system_prompt(e.rt, e.ws, mode="hspace")
        c = context.system_prompt(e.rt, e.ws, mode="cariai")
        self.assertIn(modes.HSPACE_SECTION, h)
        self.assertNotIn("# 你怎么工作", h)
        self.assertIn("# 你怎么工作", c)
        self.assertEqual(c, context.system_prompt(e.rt, e.ws))           # 不写模式就是 CariAi，和原来一样
        self.assertIn("# 必须遵守", h)


def _kb_full(msgs):
    return [m for m in msgs if m.get("role") == "tool" and retrieve.BEGIN in (m.get("content") or "")]


class FullRound(Case):
    def setUp(self):
        super().setUp()

    def _env(self, script):
        e = self.env(script, mode="full")
        d = os.path.join(e.tmp, "SOP")
        os.makedirs(d)
        make_docx(os.path.join(d, SOP))
        lib = kbm.create_library(e.rt.store, "公司 SOP")
        kbm.add_source(e.rt.store, lib["id"], d)
        e.rt.kb.indexer.enqueue(lib["id"])
        self.assertTrue(e.rt.kb.indexer.wait_idle(20))
        return e

    def test_six_searches_keep_two(self):
        script = [{"tool_calls": [("kb_search", {"query": q})]} for q in ("住宿标准", "超标处理", "适用范围", "交通", "总则", "酒店发票")]
        script.append({"content": "好了 [1]"})
        e = self._env(script)
        sid = e.rt.store.add_session(e.ws["id"], mode="hspace")["id"]
        e.rt.send_message(sid, "查六次")
        self.assertTrue(e.rt.wait_idle(sid, 20))
        last = e.mock.calls[-1]
        self.assertEqual(len(_kb_full(last)), 2)
        omitted = [m for m in last if m.get("role") == "tool" and m.get("content") == "（更早的检索结果已省略，需要时重新检索）"]
        self.assertEqual(len(omitted), 4)
        # 账本里是全文，编号在一个对话里不重复
        res = e.results(sid)
        self.assertTrue(all(retrieve.BEGIN in r["content"] for r in res))
        ns = [h["n"] for r in res for h in r["hits"]]
        by = {}
        for r in res:
            for h in r["hits"]:
                by.setdefault(h["n"], set()).add(h["part_id"])
        self.assertTrue(all(len(v) == 1 for v in by.values()))
        self.assertEqual(sorted(set(ns)), list(range(1, max(ns) + 1)))
        self.assertEqual(res[0]["display"]["title"], "在资料里找「住宿标准」")
        ts = e.events(sid, ("tool_started",))[0]["payload"]
        self.assertEqual(ts["display"]["kind"], "kb")
        # 重启后接着问：编号接着往后编
        before = max(ns)
        e.restart(mock=type(e.mock)([{"tool_calls": [("kb_search", {"query": "第五章 交通 乘坐交通工具"})]},
                                     {"tool_calls": [("kb_search", {"query": "住宿标准"})]}, {"content": "完"}]))
        e.rt.send_message(sid, "再查")
        self.assertTrue(e.rt.wait_idle(sid, 20))
        res2 = e.results(sid)[len(res):]
        first_hits = {}
        for r in res:
            for h in r["hits"]:
                for p in [h["part_id"]] + h.get("parts", []):
                    first_hits.setdefault(p, h["n"])
        for r in res2:
            for h in r["hits"]:
                old = [first_hits[p] for p in [h["part_id"]] + h.get("parts", []) if p in first_hits]
                if old:
                    self.assertEqual(h["n"], old[0])
                else:
                    self.assertGreater(h["n"], before)
        self.assertEqual(kbm.cited_parts(e.rt.store, sid)[1]["part_id"], res[0]["hits"][0]["part_id"])

    def test_demo_hspace_answer_and_not_found(self):
        from agent.llm import MockLLM, demo_script
        e = self._env([])
        e.mock = MockLLM([demo_script] * 20)
        e.rt.llm_factory = lambda: e.mock
        sid = e.rt.store.add_session(e.ws["id"], mode="hspace")["id"]
        e.rt.send_message(sid, "总监级在一线城市住宿每晚不超过多少钱？")
        self.assertTrue(e.rt.wait_idle(sid, 20))
        names = [c["name"] for ev in e.events(sid, ("assistant_message",)) for c in ev["payload"].get("tool_calls") or []]
        self.assertEqual(names, ["kb_search"])
        ans = e.events(sid, ("assistant_message",))[-1]["payload"]["content"]
        self.assertIn("[1]", ans)
        self.assertIn("800", ans)
        self.assertIn(1, kbm.cited_parts(e.rt.store, sid))
        e.rt.send_message(sid, "火星基地的门禁密码是什么")
        self.assertTrue(e.rt.wait_idle(sid, 20))
        ans = e.events(sid, ("assistant_message",))[-1]["payload"]["content"]
        self.assertIn("资料里没有找到", ans)
        # CariAi 的对话照旧走演示的老路子
        sid2 = e.rt.store.add_session(e.ws["id"])["id"]
        e.rt.send_message(sid2, "你好")
        self.assertTrue(e.rt.wait_idle(sid2, 20))
        names = [c["name"] for ev in e.events(sid2, ("assistant_message",)) for c in ev["payload"].get("tool_calls") or []]
        self.assertEqual(names, ["list_dir"])


# ======================================================================
class Modes(Case):
    def test_tools_prompt_skills_templates_search(self):
        e = self.env([])
        s_h = e.rt.store.add_session(e.ws["id"], mode="hspace")
        s_c = e.rt.store.add_session(e.ws["id"])
        self.assertEqual((s_h["mode"], s_c["mode"]), ("hspace", "cariai"))
        sub = e.rt.store.add_session(e.ws["id"], kind="sub", parent_id=s_h["id"])
        self.assertEqual(sub["mode"], "hspace")                       # 子代理跟着父对话
        rh, rc = Runner(e.rt, s_h["id"], e.mock), Runner(e.rt, s_c["id"], e.mock)
        self.assertEqual(set(rh.tools), set(rc.tools))                # 9.1：两个模式的工具一样（全部）
        for n in ("kb_search", "kb_open", "kb_list", "kb_add", "write_docx", "browser_open"):
            if n in REGISTRY and (REGISTRY[n].enabled is None or REGISTRY[n].enabled(rh.ctx)):
                self.assertIn(n, rh.tools)
        self.assertIn("你在 Hspace 里", rh.system)
        self.assertNotIn("你在 Hspace 里", rc.system)
        self.assertEqual(modes.parse_modes("[hspace, cariai]"), ["cariai", "hspace"])
        self.assertEqual(modes.parse_modes("[ hspace ]"), ["hspace"])
        self.assertEqual(modes.parse_modes(None), ["cariai", "hspace"])
        self.assertEqual(modes.parse_modes("hspace，cariai"), ["cariai", "hspace"])
        sk = {s["name"]: s for s in e.rt.skills.index()}
        if "kb-research" in sk:
            self.assertEqual(sk["kb-research"]["modes"], ["hspace"])
            self.assertNotIn("kb-research", rc.system)
            self.assertIn("kb-research", rh.system)
        self.assertTrue(all("modes" in s for s in sk.values()))
        # 搜索以前的对话：默认只在当前模式里找
        e.rt.emit(s_h["id"], "user_message", {"text": "预算表核对的事"})
        e.rt.emit(s_c["id"], "user_message", {"text": "预算表核对的事情"})
        other_h = e.rt.store.add_session(e.ws["id"], mode="hspace")
        r = REGISTRY["session_search"].handler(Runner(e.rt, other_h["id"], e.mock).ctx, query="预算表")
        self.assertEqual([h["session_id"] for h in r["hits"]], [s_h["id"]])


class ApiModesKB(ApiCase):
    script = []

    def test_sessions_state_templates(self):
        wid = self.e.ws["id"]
        st, s = self.req("GET", "/api/state")
        self.assertEqual([m["id"] for m in s["modes"]], ["cariai", "hspace"])
        self.assertEqual(set(s["modes"][1]), {"id", "name", "tagline", "prompt_line", "placeholder"})
        self.assertEqual(s["modes"][1]["prompt_line"], "想查什么？")
        self.assertIn("semantic_search", s["features"])
        st, h = self.req("POST", "/api/sessions", {"workspace_id": wid, "mode": "hspace"})
        self.assertEqual((st, h["mode"]), (200, "hspace"))
        st, c = self.req("POST", "/api/sessions", {"workspace_id": wid})
        self.assertEqual(c["mode"], "cariai")
        self.assertEqual(self.req("POST", "/api/sessions", {"workspace_id": wid, "mode": "codex"})[0], 400)
        self.assertEqual([x["id"] for x in self.req("GET", "/api/sessions?mode=hspace")[1]], [h["id"]])
        self.assertEqual(self.req("GET", "/api/sessions?mode=bad")[0], 400)
        self.assertEqual(len(self.req("GET", "/api/sessions")[1]), 2)
        self.assertEqual(self.req("GET", "/api/sessions/%s/status" % h["id"])[1]["mode"], "hspace")
        st, tpl = self.req("GET", "/api/templates?mode=hspace")
        self.assertGreaterEqual(len(tpl), 6)
        self.assertTrue(all(t["mode"] == "hspace" for t in tpl))
        self.assertTrue(all(t["mode"] == "cariai" for t in self.req("GET", "/api/templates?mode=cariai")[1]))
        st, sk = self.req("GET", "/api/skills")
        self.assertTrue(all(isinstance(x["modes"], list) for x in sk))
        # 发消息带 depth（9.4 以后用）：现在不拒绝
        st, _ = self.req("POST", "/api/sessions/%s/message" % c["id"], {"text": "你好", "depth": "deep"})
        self.assertEqual(st, 200)
        um = [e for e in self.e.events(c["id"], ("user_message",))][-1]["payload"]
        self.assertEqual(um["depth"], "deep")
        st, _ = self.req("GET", "/api/sessions/%s/file_token_check?t=%s" % (c["id"], self.srv.token), token=False)
        self.assertEqual(st, 404)         # 查询串里的令牌照样认（没有这个接口所以 404，而不是 403）

    def test_libraries_api(self):
        d = os.path.join(self.e.tmp, "SOP")
        os.makedirs(d)
        make_docx(os.path.join(d, SOP))
        write(d, "faq.csv", "问题,答案\n报销多久到账？,5 个工作日。\n")
        st, lib = self.req("POST", "/api/libraries", {"name": "公司 SOP", "note": "制度"})
        self.assertEqual((st, lib["state"], lib["docs"]), (200, "ready", 0))
        self.assertEqual(self.req("POST", "/api/libraries", {"name": "公司 SOP"})[0], 400)
        self.assertEqual(self.req("POST", "/api/libraries", {"name": "a/b"})[0], 400)
        st, lib = self.req("POST", "/api/libraries/%s/sources" % lib["id"], {"path": d})
        self.assertEqual(st, 200)
        self.assertEqual(lib["sources"][0]["kind"], "dir")
        self.assertEqual(self.req("POST", "/api/libraries/%s/sources" % lib["id"], {"path": "/没有/这个"})[0], 400)
        self.assertTrue(self.e.rt.kb.indexer.wait_idle(20))
        st, libs = self.req("GET", "/api/libraries")
        # cards、lang、tags：docs/借鉴开源项目.md 1.5（GET /api/libraries 由 agent/api_kbops.py 换过，补了这三项）
        # shared、version、enabled：5.7（公司共享目录，kb.summary 里加的）
        self.assertEqual(set(libs[0]), {"id", "name", "note", "sources", "docs", "parts", "failed", "vectors", "state",
                                        "progress", "updated", "synced", "builtin", "cards", "lang", "tags",
                                        "shared", "version", "enabled"})
        self.assertEqual((libs[0]["docs"], libs[0]["state"]), (2, "ready"))
        self.assertEqual(set(libs[0]["progress"]), {"total", "done", "failed", "current"})
        st, docs = self.req("GET", "/api/libraries/%s/docs" % lib["id"])
        # 后面七项：docs/借鉴开源项目.md 1.5（GET /api/libraries/<id>/docs 由 agent/api_kbops.py 换过；keywords 是规格之外多给的）
        self.assertEqual(set(docs[0]), {"id", "title", "path", "kind", "size", "parts", "status", "error", "mtime", "version_label",
                                        "summary", "summary_by", "card_state", "outline_count", "tags", "lang", "keywords"})
        self.assertEqual(len(self.req("GET", "/api/libraries/%s/docs?q=faq" % lib["id"])[1]), 1)
        self.assertEqual(self.req("GET", "/api/libraries/%s/docs?status=failed" % lib["id"])[1], [])
        did = next(x["id"] for x in docs if x["title"] == SOP)
        st, parts = self.req("GET", "/api/kb/docs/%s/parts" % did)
        self.assertTrue(parts)
        self.assertTrue({"seq", "loc", "heading", "text", "tokens"} <= set(parts[0]))
        st, s = self.req("POST", "/api/kb/search", {"query": "住宿标准"})
        self.assertEqual(set(s), {"hits", "weak", "used", "ms"})
        self.assertEqual(s["used"], {"keyword": True, "vector": False, "rerank": False})
        self.assertEqual(s["hits"][0]["n"], 1)
        st, p = self.req("GET", "/api/kb/parts/%s" % s["hits"][0]["part_id"])
        self.assertEqual(set(p), {"part", "section", "doc", "library"})
        self.assertTrue({"doc_id", "part_id", "library", "title", "path", "loc", "heading", "text"} <= set(p["part"]))
        self.assertTrue({"heading", "loc", "text"} <= set(p["section"]))
        self.assertEqual(self.req("GET", "/api/kb/parts/999999")[0], 404)
        st, raw, hdr = self.req("GET", "/api/kb/docs/%s/file" % did, raw=True)
        self.assertEqual(st, 200)
        self.assertTrue(raw.startswith(b"PK"))
        st, lib2 = self.req("PATCH", "/api/libraries/%s" % lib["id"], {"name": "SOP 库"})
        self.assertEqual(lib2["name"], "SOP 库")
        self.assertEqual(self.req("POST", "/api/libraries/%s/reindex" % lib["id"])[0], 200)
        self.assertTrue(self.e.rt.kb.indexer.wait_idle(20))
        # 对话的检索范围
        wid = self.e.ws["id"]
        st, h = self.req("POST", "/api/sessions", {"workspace_id": wid, "mode": "hspace", "libraries": [lib["id"]]})
        self.assertEqual(h["meta"]["libraries"], [lib["id"]])
        self.assertEqual(self.req("POST", "/api/sessions", {"workspace_id": wid, "libraries": ["L_none"]})[0], 400)
        st, h2 = self.req("PATCH", "/api/sessions/%s" % h["id"], {"libraries": []})
        self.assertEqual(h2["meta"]["libraries"], [])
        sid = lib["sources"][0]["id"]
        st, lib3 = self.req("DELETE", "/api/libraries/%s/sources/%s" % (lib["id"], sid))
        self.assertEqual((lib3["sources"], lib3["docs"]), ([], 0))
        self.assertEqual(self.req("DELETE", "/api/libraries/%s" % lib["id"])[0], 200)
        self.assertTrue(os.path.isfile(os.path.join(d, SOP)))          # 只删索引
        self.assertEqual(self.req("GET", "/api/libraries")[1], [])

    def test_settings_and_capabilities(self):
        fake = FakeEmbedding()
        try:
            st, _ = self.req("POST", "/api/settings", {"embedding": {"engine": "openai", "base_url": fake.base_url, "model": "m",
                                                                     "api_key": "sk-secret-12345678"},
                                                       "rerank": {"engine": "off"}, "kb": {"top_k": 4}})
            self.assertEqual(st, 200)
            conf = self.req("GET", "/api/settings")[1]["config"]
            self.assertTrue(conf["embedding"]["api_key"].startswith("••••"))
            st, _ = self.req("POST", "/api/settings", {"embedding": {"api_key": conf["embedding"]["api_key"]}})
            self.assertEqual(self.e.rt.cfg.get("embedding", "api_key"), "sk-secret-12345678")
            self.assertEqual(self.req("POST", "/api/settings", {"kb": {"engine": "builtin"}})[0], 400)
            self.assertEqual(self.req("POST", "/api/settings", {"embedding": {"engine": "bad"}})[0], 400)
            st, t = self.req("POST", "/api/settings/test_embedding")
            self.assertEqual((t["ok"], t["dims"]), (True, 256))
            self.assertEqual(set(t), {"ok", "dims", "ms", "error"})
            st, caps = self.req("GET", "/api/capabilities")
            self.assertEqual(set(caps["embedding"]), {"engine", "model", "alive", "dims", "kb_engine"})
            self.assertEqual((caps["embedding"]["alive"], caps["embedding"]["dims"], caps["embedding"]["kb_engine"]),
                             (True, 256, "builtin"))
            f = {x["id"]: x for x in caps["features"]}["semantic_search"]
            self.assertTrue(f["ok"])
            st, t = self.req("POST", "/api/settings/test_embedding", {"embedding": {"base_url": "http://127.0.0.1:1/v1"}})
            self.assertFalse(t["ok"])
            st, t = self.req("POST", "/api/settings/test_embedding", {"base_url": fake.base_url, "model": "x", "api_key": ""})
            self.assertTrue(t["ok"])
        finally:
            fake.close()


if __name__ == "__main__":
    unittest.main()
