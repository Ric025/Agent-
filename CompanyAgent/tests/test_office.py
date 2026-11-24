"""文档工具（规格第 4 节、9.6 节）：write_pptx 的模板、pptx_outline、excel_diff 的两种比法、excel_query、读资料库里的文件。"""
from __future__ import annotations
import datetime, json, os, struct, sys, types, unittest, zipfile, zlib
from unittest import mock
from helpers import Case
from agent.tools import REGISTRY, ToolError
from agent.policy import PathError
from agent.loop import Runner
from agent.context import estimate_tools
from agent.tools import office

P14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"


def _bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _png():
    raw = b"\x00\xff\x00\x00"
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class OfficeCase(Case):
    def setUp(self):
        super().setUp()
        self.e = self.env([])
        self.sid = self.e.new_session()
        self.ctx = Runner(self.e.rt, self.sid, self.e.mock).ctx
        # 默认不接资料库：_read_path 退回当前文件夹
        patcher = mock.patch.dict(sys.modules, {"agent.kb": None})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_tool(self, _tool, **args):
        return REGISTRY[_tool].handler(self.ctx, **args)

    def p(self, rel):
        return os.path.join(self.e.root, rel)

    def _xlsx(self, rel, sheets):
        import openpyxl
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for name, rows in sheets.items():
            ws = wb.create_sheet(name)
            for r in rows:
                ws.append(r)
        os.makedirs(os.path.dirname(self.p(rel)) or ".", exist_ok=True)
        wb.save(self.p(rel))


# ---------------- write_pptx 的模板 ----------------

def make_template(path, names=("标题幻灯片", "标题和内容"), old_slides=3, sections=True, size=None):
    """用 python-pptx 的默认模板改出一份"公司模板"：版式改成中文名、母版背景改成深蓝、4:3、带三页旧内容、带节。"""
    import pptx
    from pptx.dml.color import RGBColor
    prs = pptx.Presentation()
    if size:
        prs.slide_width, prs.slide_height = size
    fill = prs.slide_master.background.fill
    fill.solid()
    fill.fore_color.rgb = RGBColor(0x0A, 0x24, 0x5C)
    prs.slide_layouts[0].name, prs.slide_layouts[1].name = names
    for i in range(old_slides):
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = "旧页%d" % i
        s.placeholders[1].text = "旧要点"
        s.notes_slide.notes_text_frame.text = "旧备注"
    if sections and old_slides:
        from lxml import etree
        ids = [str(sld.id) for sld in prs.slides._sldIdLst]
        ext_lst = prs.part._element.find("{http://schemas.openxmlformats.org/presentationml/2006/main}extLst")
        if ext_lst is None:
            ext_lst = etree.SubElement(prs.part._element, "{http://schemas.openxmlformats.org/presentationml/2006/main}extLst")
        ext = etree.SubElement(ext_lst, "{http://schemas.openxmlformats.org/presentationml/2006/main}ext",
                               uri="{521415D9-36F7-43E2-AB2F-B90AF26B5E84}")
        lst = etree.SubElement(ext, "{%s}sectionLst" % P14)
        sec = etree.SubElement(lst, "{%s}section" % P14, name="第一节", id="{11111111-1111-1111-1111-111111111111}")
        sl = etree.SubElement(sec, "{%s}sldIdLst" % P14)
        for i in ids:
            etree.SubElement(sl, "{%s}sldId" % P14, id=i)
    prs.save(path)


class PptxTemplate(OfficeCase):
    SLIDES = [{"title": "收入增长 20%", "bullets": ["新客户贡献一半", "  华东占六成", "老客户复购稳定"], "notes": "先讲结论"},
              {"title": "下季度重点", "bullets": ["控制成本"] + ["要点%d：这是一条比较长的要点文字，用来测试缩小" % i for i in range(9)]}]

    def test_template_layouts_placeholders_and_untouched(self):
        import pptx
        from pptx.util import Inches
        make_template(self.p("公司模板.pptx"), size=(Inches(10), Inches(7.5)))
        before = _bytes(self.p("公司模板.pptx"))
        r = self.run_tool("write_pptx", path="演示/新汇报.pptx", title="四季度经营", subtitle="管理层会议",
                          slides=self.SLIDES, template="公司模板.pptx")
        self.assertEqual(_bytes(self.p("公司模板.pptx")), before)        # 模板没动
        self.assertEqual(r["template"], "公司模板.pptx")
        self.assertEqual(r["layouts"], {"title": "标题幻灯片", "content": "标题和内容"})
        self.assertNotIn("note", r)
        self.assertEqual(r["slides"], 3)
        prs = pptx.Presentation(self.p("演示/新汇报.pptx"))
        self.assertEqual(len(prs.slides), 3)
        self.assertEqual((prs.slide_width, prs.slide_height), (Inches(10), Inches(7.5)))   # 用模板的页面尺寸
        s0, s1, s2 = prs.slides
        self.assertEqual(s0.slide_layout.name, "标题幻灯片")
        self.assertEqual(s1.slide_layout.name, "标题和内容")
        types0 = {int(ph.placeholder_format.type): ph.text_frame.text for ph in s0.placeholders}
        self.assertEqual(types0, {3: "四季度经营", 4: "管理层会议"})
        self.assertEqual(s1.shapes.title.text, "收入增长 20%")
        body = [ph for ph in s1.placeholders if int(ph.placeholder_format.type) == 7][0]
        self.assertEqual([(p.text, p.level) for p in body.text_frame.paragraphs],
                         [("新客户贡献一半", 0), ("华东占六成", 1), ("老客户复购稳定", 0)])
        # 不写死字号和字体：样式来自模板
        for s in prs.slides:
            for sh in s.shapes:
                for p in sh.text_frame.paragraphs:
                    for run in p.runs:
                        self.assertIsNone(run.font.size)
                        self.assertIsNone(run.font.name)
        self.assertFalse([sh for sh in s1.shapes if not sh.is_placeholder])        # 没有自己画的框
        self.assertEqual(s1.notes_slide.notes_text_frame.text, "先讲结论")
        # 要点多：交给 PowerPoint 缩小文字（normAutofit），不改字号
        body2 = [ph for ph in s2.placeholders if int(ph.placeholder_format.type) == 7][0]
        self.assertEqual(len(body2.text_frame.paragraphs), 8)
        self.assertIn("normAutofit", body2.text_frame._txBody.bodyPr.xml)
        self.assertIn("fontScale", body2.text_frame._txBody.bodyPr.xml)
        # 母版背景还在；旧页、旧备注、节都去掉了，没有孤儿部件
        z = zipfile.ZipFile(self.p("演示/新汇报.pptx"))
        names = z.namelist()
        self.assertIn("0A245C", z.read("ppt/slideMasters/slideMaster1.xml").decode())
        self.assertEqual(sorted(n for n in names if n.startswith("ppt/slides/slide")), ["ppt/slides/slide1.xml", "ppt/slides/slide2.xml", "ppt/slides/slide3.xml"])
        self.assertEqual(len([n for n in names if n.startswith("ppt/notesSlides/notesSlide")]), 1)
        alltext = "".join(z.read(n).decode() for n in names if n.endswith(".xml"))
        self.assertNotIn("旧页", alltext)
        self.assertNotIn("旧备注", alltext)
        self.assertNotIn("sectionLst", z.read("ppt/presentation.xml").decode())

    def test_layouts_found_by_placeholder_type_not_name(self):
        import pptx
        make_template(self.p("t.pptx"), names=("封面", "正文页"), old_slides=0)
        # 把"节标题"版式挪到最前面也不该被当成内容版式（它的正文框小）
        r = self.run_tool("write_pptx", path="o.pptx", title="T", slides=[{"title": "A", "bullets": ["x"]}], template="t.pptx")
        self.assertEqual(r["layouts"], {"title": "封面", "content": "正文页"})
        prs = pptx.Presentation(self.p("o.pptx"))
        self.assertEqual([s.slide_layout.name for s in prs.slides], ["封面", "正文页"])
        # 没写副标题：副标题占位符去掉，不留"单击此处添加副标题"
        self.assertEqual([int(ph.placeholder_format.type) for ph in prs.slides[0].placeholders], [3])

    def test_potx_template(self):
        import pptx
        make_template(self.p("t.pptx"), old_slides=1, sections=False)
        src = zipfile.ZipFile(self.p("t.pptx"))
        with zipfile.ZipFile(self.p("t.potx"), "w") as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "[Content_Types].xml":
                    data = data.replace(b"presentationml.presentation.main+xml", b"presentationml.template.main+xml")
                dst.writestr(item, data)
        r = self.run_tool("write_pptx", path="o.pptx", title="T", slides=[{"title": "A", "bullets": ["x"]}], template="t.potx")
        self.assertEqual(r["layouts"]["content"], "标题和内容")
        self.assertEqual(len(pptx.Presentation(self.p("o.pptx")).slides), 2)

    def test_fallback_when_no_body_layout(self):
        import pptx
        from pptx.util import Inches
        prs = pptx.Presentation()
        prs.slide_width = Inches(10)
        fill = prs.slide_master.background.fill
        fill.solid()
        from pptx.dml.color import RGBColor
        fill.fore_color.rgb = RGBColor(0x12, 0x34, 0x56)
        for lay in list(prs.slide_layouts):
            if lay.name != "Blank":
                prs.slide_layouts.remove(lay)
        prs.slides.add_slide(prs.slide_layouts[0])
        prs.save(self.p("blank.pptx"))
        before = _bytes(self.p("blank.pptx"))
        r = self.run_tool("write_pptx", path="o.pptx", title="四季度", subtitle="副", slides=self.SLIDES, template="blank.pptx")
        self.assertIn("模板里没有带正文的版式，内容页用了默认画法", r["note"])
        self.assertIn("标题页用了默认画法", r["note"])
        self.assertEqual(r["layouts"], {"title": None, "content": None})
        self.assertEqual(_bytes(self.p("blank.pptx")), before)
        out = pptx.Presentation(self.p("o.pptx"))
        self.assertEqual(len(out.slides), 3)
        self.assertEqual(out.slide_width, Inches(10))
        self.assertIn("123456", zipfile.ZipFile(self.p("o.pptx")).read("ppt/slideMasters/slideMaster1.xml").decode())
        text = " ".join(sh.text_frame.text for s in out.slides for sh in s.shapes if sh.has_text_frame)
        for t in ("四季度", "收入增长 20%", "华东占六成", "控制成本"):
            self.assertIn(t, text)
        # 按页面宽度缩放：框不超出页面
        for s in out.slides:
            for sh in s.shapes:
                self.assertLessEqual(sh.left + sh.width, out.slide_width + 10)
        self.assertEqual(out.slides[1].notes_slide.notes_text_frame.text, "先讲结论")

    def test_template_errors_and_no_template_unchanged(self):
        make_template(self.p("t.pptx"), old_slides=0)
        with self.assertRaises(ToolError):
            self.run_tool("write_pptx", path="t.pptx", title="T", slides=[], template="t.pptx")
        with self.assertRaises(ToolError):
            self.run_tool("write_pptx", path="o.pptx", title="T", slides=[], template="没有.pptx")
        self.e.write("x.txt", "hi")
        with self.assertRaises(ToolError):
            self.run_tool("write_pptx", path="o.pptx", title="T", slides=[], template="x.txt")
        r = self.run_tool("write_pptx", path="plain.pptx", title="T", slides=[{"title": "A", "bullets": ["x"]}])
        self.assertNotIn("template", r)
        import pptx
        self.assertEqual(len(pptx.Presentation(self.p("plain.pptx")).slides), 2)


# ---------------- pptx_outline ----------------

class PptxOutline(OfficeCase):
    def _deck(self, path):
        import pptx
        from pptx.chart.data import CategoryChartData
        from pptx.enum.chart import XL_CHART_TYPE
        from pptx.util import Inches
        make_template(path, old_slides=0)
        prs = pptx.Presentation(path)
        s = prs.slides.add_slide(prs.slide_layouts[0])
        s.shapes.title.text = "三季度经营汇报"
        s.placeholders[1].text = "财务部"
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = "收入增长 20%"
        tf = s.placeholders[1].text_frame
        tf.text = "新客户贡献一半"
        p = tf.add_paragraph()
        p.text, p.level = "华东占六成", 1
        s.notes_slide.notes_text_frame.text = "先讲结论" + "很长" * 200
        s = prs.slides.add_slide(prs.slide_layouts[5])
        s.shapes.title.text = "分区域"
        t = s.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
        t.cell(0, 0).text, t.cell(0, 1).text, t.cell(1, 0).text, t.cell(1, 1).text = "区域", "收入", "华东", "120"
        cd = CategoryChartData()
        cd.categories = ["Q1", "Q2"]
        cd.add_series("收入", (1, 2))
        s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(5), Inches(2), Inches(4), Inches(3), cd)
        img = os.path.join(os.path.dirname(path), "a.png")
        with open(img, "wb") as f:
            f.write(_png())
        s.shapes.add_picture(img, Inches(1), Inches(4))
        prs.save(path)

    def test_fields(self):
        self._deck(self.p("汇报.pptx"))
        r = self.run_tool("pptx_outline", path="汇报.pptx")
        self.assertEqual(r["slides_total"], 3)
        self.assertEqual(r["path"], "汇报.pptx")
        self.assertIn("4:3", r["size"])
        self.assertIn("25.4×19.1 cm", r["size"])
        lay = {l["name"]: l for l in r["layouts"]}
        self.assertEqual(lay["标题幻灯片"]["placeholders"], ["center_title", "subtitle"])
        self.assertEqual(lay["标题幻灯片"]["write_pptx_uses_for"], "标题页")
        self.assertEqual(lay["标题和内容"]["write_pptx_uses_for"], "内容页")
        self.assertEqual(lay["Blank"]["placeholders"], [])
        s1, s2, s3 = r["slides"]
        self.assertEqual((s1["n"], s1["layout"], s1["title"], s1["bullets"]), (1, "标题幻灯片", "三季度经营汇报", ["财务部"]))
        self.assertEqual(s2["bullets"], ["新客户贡献一半", "  华东占六成"])
        self.assertTrue(s2["notes"].startswith("先讲结论"))
        self.assertLessEqual(len(s2["notes"]), 201)
        self.assertEqual(s3["has"], {"table": 1, "chart": 1, "picture": 1})
        self.assertEqual(s3["tables"], ["区域 | 收入 / 华东 | 120"])
        self.assertNotIn("more", r)
        self.assertLess(estimate_tools([REGISTRY["pptx_outline"].spec()]), 260)

    def test_limit(self):
        import pptx
        prs = pptx.Presentation()
        for i in range(80):
            s = prs.slides.add_slide(prs.slide_layouts[1])
            s.shapes.title.text = "第%d页的标题" % i + "长" * 100
            s.placeholders[1].text_frame.text = "\n".join("要点" * 50 for _ in range(15))
            s.notes_slide.notes_text_frame.text = "备注" * 300
        prs.save(self.p("big.pptx"))
        r = self.run_tool("pptx_outline", path="big.pptx")
        self.assertEqual(r["slides_total"], 80)
        self.assertLessEqual(len(json.dumps(r, ensure_ascii=False)), 6100)
        self.assertIn("没列出", r["more"])
        self.assertIn("还有 %d 页" % (80 - len(r["slides"])), r["more"])
        self.assertTrue(all(len(b) <= 52 for s in r["slides"] for b in s["bullets"]))

    def test_errors(self):
        with self.assertRaises(ToolError):
            self.run_tool("pptx_outline", path="没有.pptx")
        self.e.write("a.txt", "x")
        with self.assertRaises(ToolError):
            self.run_tool("pptx_outline", path="a.txt")


# ---------------- excel_diff ----------------

class ExcelDiff(OfficeCase):
    def _versions(self):
        self._xlsx("v1.xlsx", {"预算": [["项目", "金额", "备注"], ["差旅", 100, "甲"], ["培训", 200, "乙"], ["办公", 50, None]],
                               "旧表": [["x"]], "说明": [["版本 1"]]})
        self._xlsx("v2.xlsx", {"预算": [["项目", "金额", "备注"], ["差旅", 120.5, "甲"], ["培训", "200", "乙改"], ["办公", 50, None],
                                       ["招待", 30, None]],
                               "说明": [["版本 1"]], "新表": [["y"]]})

    def test_cells_default_mode(self):
        import openpyxl
        self._versions()
        r = self.run_tool("excel_diff", path_a="v1.xlsx", path_b="v2.xlsx", output_path="对比/差异.xlsx")
        self.assertEqual(r["mode"], "cells")
        self.assertEqual((r["sheets_only_in_a"], r["sheets_only_in_b"]), (["旧表"], ["新表"]))
        sh = {s["name"]: s for s in r["sheets"]}
        self.assertEqual(sh["预算"]["rows"], [4, 5])
        self.assertEqual(sh["预算"]["cols"], [3, 3])
        self.assertEqual(sh["说明"]["changed_cells"], 0)
        ch = {c["cell"]: c for c in r["changes"]}
        self.assertEqual(ch["B2"], {"sheet": "预算", "cell": "B2", "old": "100", "new": "120.5", "diff": "20.5"})
        self.assertNotIn("B3", ch)                 # 200 和 "200" 是同一个数
        self.assertEqual((ch["C3"]["old"], ch["C3"]["new"], ch["C3"]["diff"]), ("乙", "乙改", ""))
        self.assertEqual(ch["A5"]["new"], "招待")
        self.assertEqual(r["changed_cells"], 4)    # B2、C3、A5、B5
        wb = openpyxl.load_workbook(self.p("对比/差异.xlsx"))
        self.assertEqual(wb.sheetnames, ["汇总", "逐格差异"])
        self.assertEqual(wb["逐格差异"].max_row, 5)
        self.assertEqual(wb["逐格差异"]["E2"].value, 20.5)
        self.assertIn("仅表A有", [c.value for c in wb["汇总"]["B"]])
        self.assertEqual(r["version"], 1)

    def test_cells_named_sheets_and_many_changes(self):
        self._xlsx("a.xlsx", {"2025": [[i * j for j in range(10)] for i in range(1, 11)]})
        self._xlsx("b.xlsx", {"2026": [[i * j + 1 for j in range(10)] for i in range(1, 11)]})
        r = self.run_tool("excel_diff", path_a="a.xlsx", path_b="b.xlsx", sheet_a="2025", sheet_b="2026", mode="cells", output_path="d.xlsx")
        self.assertEqual(r["sheets"][0]["name"], "2025 / 2026")
        self.assertEqual(r["changed_cells"], 100)
        self.assertEqual(len(r["changes"]), 20)
        self.assertIn("另有 80 处", r["more"])
        with self.assertRaises(ToolError):
            self.run_tool("excel_diff", path_a="a.xlsx", path_b="b.xlsx", sheet_a="没有", mode="cells", output_path="d.xlsx")
        with self.assertRaises(ToolError):
            self.run_tool("excel_diff", path_a="a.xlsx", path_b="b.xlsx", mode="rows", output_path="d.xlsx")

    def test_key_mode_default_and_compare_default(self):
        import openpyxl
        self._xlsx("a.xlsx", {"台账": [["单号", "金额", "客户"], ["1", 10, "甲"], ["2", 20, "乙"]]})
        self._xlsx("b.xlsx", {"系统": [["单号", "金额", "客户"], ["1", 10, "甲"], ["2", 25, "乙"], ["3", 1, "丙"]]})
        r = self.run_tool("excel_diff", path_a="a.xlsx", path_b="b.xlsx", key_a=["单号"], key_b=["单号"], output_path="k.xlsx")
        self.assertNotIn("mode", r)                # 按主键：返回值和以前一样
        self.assertEqual((r["matched_keys"], r["diff_cells"], r["only_in_b"]), (2, 1, 1))
        self.assertEqual(r["samples"][0]["column"], "金额")
        self.assertEqual(openpyxl.load_workbook(self.p("k.xlsx")).sheetnames, ["汇总", "不一致", "仅表A有", "仅表B有", "重复键"])
        with self.assertRaises(ToolError):
            self.run_tool("excel_diff", path_a="a.xlsx", path_b="b.xlsx", mode="key", output_path="k.xlsx")
        with self.assertRaises(ToolError):
            self.run_tool("excel_diff", path_a="a.xlsx", path_b="b.xlsx", key_a=["单号"], key_b=["单号"], compare=[{"a": "金额"}], output_path="k.xlsx")
        with self.assertRaises(ToolError):
            self.run_tool("excel_diff", path_a="a.xlsx", path_b="b.xlsx", output_path="k.csv")


# ---------------- excel_query ----------------

class ExcelQuery(OfficeCase):
    def setUp(self):
        super().setUp()
        d = datetime.datetime
        self._xlsx("销售.xlsx", {"明细": [
            [None, None, None, None],
            ["区域", "季度", "销售额", "日期"],
            ["华东", "Q3", "1,200元", d(2026, 7, 5)],
            ["华东 ", "Q3", 800, d(2026, 8, 1)],
            ["ＨＢ", "Q3", "12.5%", "2026/9/30"],
            ["华南", "Q2", 500, d(2026, 4, 2)],
            ["华南", "Q3", 300.25, d(2026, 9, 1)],
            ["华北", "Q3", "待定", d(2026, 9, 2)],
            ["合计", None, 3100, None],
        ], "其他": [["a"], [1]]})

    def q(self, **kw):
        return self.run_tool("excel_query", path="销售.xlsx", **kw)

    def test_group_sum_sort_and_source(self):
        r = self.q(where=[{"col": "季度", "op": "=", "value": "Q3"}], group_by=["区域"], agg={"销售额": "sum"},
                   sort={"col": "销售额", "desc": True})
        self.assertEqual(r["columns"], ["区域", "销售额 合计"])
        self.assertEqual(r["rows"][0], ["华东", 2000])        # "华东" 和 "华东 " 算同一组，"1,200元" 当成 1200
        self.assertIn(["华南", 300.25], r["rows"])
        self.assertEqual(r["rows_used"], 5)
        self.assertEqual(r["rows_total"], 6)                  # 合计行不算
        self.assertEqual(r["excluded_total_rows"], [9])
        self.assertEqual(r["non_numeric_skipped"], {"销售额": 1})
        self.assertEqual(r["source"]["path"], "销售.xlsx")
        self.assertEqual(r["source"]["sheet"], "明细")
        self.assertEqual(r["source"]["rows"], "第 3、4、5、7、8 行")

    def test_filters(self):
        r = self.q(where=[{"col": "销售额", "op": ">=", "value": "800"}], agg={"销售额": "count"})
        self.assertEqual(r["rows"], [[2]])                    # 1200 和 800
        r = self.q(where=[{"col": "日期", "op": ">", "value": "2026-08-31"}])
        self.assertEqual([row[0] for row in r["rows"]], [5, 7, 8])   # 行号；"2026/9/30" 这种文字日期也认
        self.assertEqual(r["columns"][:2], ["行号", "区域"])
        r = self.q(where=[{"col": "区域", "op": "in", "value": ["华南", "hb"]}], agg={"*": "count"})
        self.assertEqual(r["rows"], [[3]])                    # 全角 ＨＢ 和 hb 相同
        r = self.q(where=[{"col": "区域", "op": "contains", "value": "华"}, {"col": "季度", "op": "!=", "value": "Q2"}],
                   agg={"销售额": "avg", "日期": "max"})
        self.assertEqual(r["columns"], ["销售额 平均", "日期 最大"])
        self.assertAlmostEqual(r["rows"][0][0], (1200 + 800 + 300.25) / 3, places=4)
        self.assertEqual(r["rows"][0][1], "2026-09-02")
        r = self.q(where=[{"col": "区域", "op": "=", "value": "西北"}])
        self.assertEqual(r["rows_used"], 0)
        self.assertIn("没有符合条件", r["note"])
        r = self.q(sort={"col": "销售额"}, limit=2)
        self.assertEqual([row[3] for row in r["rows"]], ["12.5%", 300.25])
        self.assertIn("只列了前 2 行", r["more"])
        r = self.q(group_by=["季度"], sort={"col": "行数", "desc": True})
        self.assertEqual(r["rows"][0], ["Q3", 5])
        r = self.run_tool("excel_query", path="销售.xlsx", sheet="其他", agg={"a": "sum"})
        self.assertEqual(r["rows"], [[1]])

    def test_errors(self):
        with self.assertRaises(ToolError) as cm:
            self.q(where=[{"col": "销售", "op": "=", "value": 1}])
        self.assertIn("最接近的是：销售额", str(cm.exception))
        self.assertIn("全部列名：区域、季度、销售额、日期", str(cm.exception))
        with self.assertRaises(ToolError):
            self.q(where=[{"col": "区域", "op": "like", "value": 1}])
        with self.assertRaises(ToolError):
            self.q(agg={"销售额": "median"})
        with self.assertRaises(ToolError):
            self.q(sort={"col": "不存在"})
        with self.assertRaises(ToolError):
            self.run_tool("excel_query", path="销售.xlsx", sheet="没有")
        self.assertEqual(REGISTRY["excel_query"].risk_of(self.ctx, {}), "read")


# ---------------- 读资料库里的文件 ----------------

class ReadPath(OfficeCase):
    def test_fallback_without_kb(self):
        self.assertEqual(office._read_path(self.ctx, "a.xlsx"), os.path.join(os.path.realpath(self.e.root), "a.xlsx"))
        with self.assertRaises(PathError):
            office._read_path(self.ctx, "../外面.xlsx")

    def test_uses_kb_resolve_read(self):
        import tempfile, shutil
        lib = tempfile.mkdtemp(prefix="office_lib_")
        self.addCleanup(shutil.rmtree, lib, True)
        calls = []

        def resolve_read(ctx, path):
            calls.append(path)
            if path.startswith("公司SOP/"):
                return os.path.join(lib, path.split("/", 1)[1])
            if path.startswith("坏库/"):
                raise ToolError("资料库里没有这个文件")
            return os.path.join(os.path.realpath(ctx.root), path)
        fake = types.ModuleType("agent.kb")
        fake.resolve_read = resolve_read
        import openpyxl
        wb = openpyxl.Workbook()
        wb.active.append(["项目", "金额"])
        wb.active.append(["差旅", 1])
        wb.save(os.path.join(lib, "预算.xlsx"))
        wb.active["B2"] = 2
        wb.save(os.path.join(lib, "预算2.xlsx"))
        make_template(os.path.join(lib, "模板.pptx"), old_slides=1)
        with mock.patch.dict(sys.modules, {"agent.kb": fake}):
            r = self.run_tool("excel_inspect", path="公司SOP/预算.xlsx")
            self.assertEqual(r["path"], "公司SOP/预算.xlsx")       # 资料库的文件照用户的写法显示
            self.assertEqual(r["sheets"][0]["columns"], ["项目", "金额"])
            self.assertEqual(self.run_tool("excel_read", path="公司SOP/预算.xlsx", sheet="Sheet")["total_rows"], 1)
            r = self.run_tool("excel_diff", path_a="公司SOP/预算.xlsx", path_b="公司SOP/预算2.xlsx", output_path="差异.xlsx")
            self.assertEqual(r["changes"][0]["cell"], "B2")
            self.assertTrue(os.path.isfile(self.p("差异.xlsx")))  # 写在当前文件夹
            self.assertEqual(self.run_tool("excel_query", path="公司SOP/预算.xlsx", agg={"金额": "sum"})["source"]["path"], "公司SOP/预算.xlsx")
            self.assertEqual(self.run_tool("pptx_outline", path="公司SOP/模板.pptx")["slides_total"], 1)
            r = self.run_tool("write_pptx", path="新.pptx", title="T", slides=[{"title": "A", "bullets": ["x"]}], template="公司SOP/模板.pptx")
            self.assertEqual(r["template"], "公司SOP/模板.pptx")
            self.assertEqual(r["path"], "新.pptx")
            with self.assertRaises(ToolError):
                self.run_tool("pptx_outline", path="坏库/x.pptx")
            # 写操作不经过资料库：输出路径永远在当前文件夹里
            n = len(calls)
            with self.assertRaises(PathError):
                self.run_tool("write_pptx", path="../公司SOP/新.pptx", title="T", slides=[])
            self.assertEqual(len(calls), n)
        self.assertIn("公司SOP/预算2.xlsx", calls)


class Specs(unittest.TestCase):
    def test_tool_definitions_are_short(self):
        n = {k: estimate_tools([REGISTRY[k].spec()]) for k in ("write_pptx", "pptx_outline", "excel_diff", "excel_query")}
        self.assertLessEqual(n["write_pptx"], 260, n)
        self.assertLessEqual(n["pptx_outline"], 260, n)
        self.assertLessEqual(n["excel_diff"], 260, n)
        self.assertLessEqual(n["excel_query"], 300, n)

    def test_py39_syntax(self):
        import ast
        with open(office.__file__, encoding="utf-8") as f:
            ast.parse(f.read(), feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
