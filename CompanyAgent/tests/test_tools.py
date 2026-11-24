"""工具（第 3 项）：文件读写改删、excel_diff、write_docx/pptx/xlsx、资料库检索、run_python、其他小工具。"""
from __future__ import annotations
import os, unittest
from helpers import Case
from agent.tools import REGISTRY, ToolError
from agent.policy import PathError
from agent.loop import Runner


class ToolCase(Case):
    def setUp(self):
        super().setUp()
        self.e = self.env([])
        self.sid = self.e.new_session()
        self.ctx = Runner(self.e.rt, self.sid, self.e.mock).ctx

    def run_tool(self, _tool, **args):
        return REGISTRY[_tool].handler(self.ctx, **args)

    def p(self, rel):
        return os.path.join(self.e.root, rel)


class Files(ToolCase):
    def test_write_read_edit_versions(self):
        r = self.run_tool("write_file", path="报告/q3.md", content="收入 100 万\n利润 10 万\n")
        self.assertEqual((r["version"], r["previous_saved_as"]), (1, None))
        self.assertEqual(REGISTRY["write_file"].risk_of(self.ctx, {"path": "报告/q3.md"}), "overwrite")
        self.assertEqual(REGISTRY["write_file"].risk_of(self.ctx, {"path": "报告/new.md"}), "create")
        r = self.run_tool("write_file", path="报告/q3.md", content="收入 120 万\n利润 10 万\n")
        self.assertEqual(r["version"], 2)
        self.assertTrue(r["previous_saved_as"].startswith(".agent/versions/"))
        self.assertTrue(os.path.isfile(self.p(r["previous_saved_as"])))
        self.assertEqual(self.run_tool("read_file", path="报告/q3.md")["text"], "收入 120 万\n利润 10 万\n")
        r = self.run_tool("edit_file", path="报告/q3.md", old="利润 10 万", new="利润 12 万")
        self.assertEqual(r["version"], 3)
        self.assertIn("利润 12 万", self.e.read("报告/q3.md"))
        with self.assertRaises(ToolError):
            self.run_tool("edit_file", path="报告/q3.md", old="不存在的话", new="x")
        self.run_tool("write_file", path="dup.txt", content="a a")
        with self.assertRaises(ToolError):
            self.run_tool("edit_file", path="dup.txt", old="a", new="b")
        arts = self.e.rt.store.q("SELECT path, version FROM artifacts ORDER BY created")
        self.assertEqual([a["version"] for a in arts if a["path"] == "报告/q3.md"], [1, 2, 3])
        self.assertTrue(self.e.events(self.sid, ("artifact",)))

    def test_read_segments_and_limits(self):
        self.e.write("long.txt", "字" * 12000)
        r = self.run_tool("read_file", path="long.txt", offset=5000, max_chars=3000)
        self.assertEqual((len(r["text"]), r["total_chars"], r["truncated"]), (3000, 12000, True))
        with self.assertRaises(ToolError):
            self.run_tool("read_file", path="没有.txt")
        with self.assertRaises(PathError):
            self.run_tool("read_file", path="../../etc/passwd")
        with self.assertRaises(PathError):
            self.run_tool("write_file", path=".agent/x.txt", content="x")

    def test_list_search_move_delete(self):
        self.e.write("a/一.txt", "订单号 SO-001 已发货")
        self.e.write("a/二.csv", "单号,金额\nSO-002,5\n")
        self.e.write(".agent/hidden.txt", "内部")
        r = self.run_tool("list_dir", path=".")
        self.assertEqual([i["name"] for i in r["items"]], ["a/"])
        r = self.run_tool("search_files", query="so-00")
        self.assertEqual(sorted(h["file"] for h in r["content_matches"]), ["a/一.txt", "a/二.csv"])
        self.assertEqual(self.run_tool("search_files", query="二", glob="*.csv")["name_matches"], ["a/二.csv"])
        self.run_tool("move_file", src="a/一.txt", dst="b/一.txt")
        self.assertTrue(os.path.isfile(self.p("b/一.txt")))
        with self.assertRaises(ToolError):
            self.run_tool("move_file", src="b/一.txt", dst="a/二.csv")
        r = self.run_tool("delete_file", path="b/一.txt")
        self.assertFalse(os.path.exists(self.p("b/一.txt")))
        self.assertTrue(os.path.isfile(self.p(r["recoverable_from"])))   # 删除是移进回收目录


class Office(ToolCase):
    def _xlsx(self, rel, rows, sheet="Sheet1"):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet
        for r in rows:
            ws.append(r)
        wb.save(self.p(rel))

    def test_excel_diff(self):
        import openpyxl
        self._xlsx("a.xlsx", [["单号", "金额", "客户"], ["0012", 100.004, "甲"], ["0013", 200, "乙"], ["0014", 50, "丙"],
                              ["0014", 51, "丙2"], ["0099", 10, "只在A"]], "台账")
        self._xlsx("b.xlsx", [["编号", "金额", "客户"], ["0012", "100", "甲"], ["0013", 210.5, "乙"], ["12", 1, "前导零不同"],
                              ["0014", "50.00", "丙"], ["0200", 5, "只在B"]], "系统")
        r = self.run_tool("excel_diff", path_a="a.xlsx", sheet_a="台账", path_b="b.xlsx", sheet_b="系统", key_a=["单号"], key_b=["编号"],
                          compare=[{"a": "金额", "b": "金额"}, {"a": "客户", "b": "客户"}], output_path="out/差异.xlsx")
        self.assertEqual((r["rows_a"], r["rows_b"]), (5, 5))
        self.assertEqual(r["matched_keys"], 3)            # 0012、0013、0014；"12" 和 "0012" 不算同一个
        self.assertEqual((r["only_in_a"], r["only_in_b"]), (1, 2))
        self.assertEqual((r["duplicate_keys_a"], r["duplicate_keys_b"]), (1, 0))
        self.assertEqual((r["keys_with_diff"], r["diff_cells"]), (1, 1))   # 100.004 对 100 在容差内
        self.assertEqual(r["samples"][0]["key"], "0013")
        self.assertEqual(r["samples"][0]["diff"], "-10.5")
        s = [x for x in r["sums"] if x["column"] == "金额"][0]
        self.assertEqual((s["sum_a"], s["sum_b"]), ("360.004", "366.50"))
        wb = openpyxl.load_workbook(self.p("out/差异.xlsx"))
        self.assertEqual(wb.sheetnames, ["汇总", "不一致", "仅表A有", "仅表B有", "重复键"])
        self.assertEqual(wb["不一致"].max_row, 2)
        self.assertEqual(sorted(str(c.value) for c in wb["仅表B有"]["B"][1:]), ["0200", "12"])
        with self.assertRaises(ToolError):
            self.run_tool("excel_diff", path_a="a.xlsx", sheet_a="台账", path_b="b.xlsx", sheet_b="系统", key_a=["没这列"], key_b=["编号"],
                          compare=[], output_path="o.xlsx")

    def test_inspect_and_read(self):
        self._xlsx("t.xlsx", [[None, None], ["姓名", "金额"], ["张三", 1.5], ["李四", 2]])
        r = self.run_tool("excel_inspect", path="t.xlsx")
        self.assertEqual(r["sheets"][0]["columns"], ["姓名", "金额"])
        self.assertEqual(r["sheets"][0]["rows"], 2)
        r = self.run_tool("excel_read", path="t.xlsx", sheet="Sheet1", start=2, limit=5)
        self.assertEqual(r["records"], [{"姓名": "李四", "金额": "2"}])

    def test_write_xlsx_formula(self):
        import openpyxl
        r = self.run_tool("write_xlsx", path="汇总.xlsx", sheets=[{"name": "合计", "rows": [["a", "b", "和"], [1, 2, "=A2+B2"]]}])
        self.assertEqual(r["cells"], 6)
        wb = openpyxl.load_workbook(self.p("汇总.xlsx"))
        self.assertEqual(wb["合计"]["C2"].value, "=A2+B2")
        self.assertTrue(wb["合计"]["A1"].font.bold)
        with self.assertRaises(ToolError):
            self.run_tool("write_xlsx", path="x.csv", sheets=[{"name": "a", "rows": []}])

    def test_write_docx(self):
        import docx
        md = "# 三季度汇报\n\n结论：**收入增长 20%**。\n\n- 第一点\n- 第二点\n1. 步骤一\n\n| 项目 | 金额 |\n|---|---|\n| 收入 | 120 |\n| 成本 | 80 |\n"
        r = self.run_tool("write_docx", path="汇报.docx", markdown=md)
        d = docx.Document(self.p("汇报.docx"))
        texts = [p.text for p in d.paragraphs]
        self.assertIn("三季度汇报", texts)
        self.assertIn("结论：收入增长 20%。", texts)
        bold = [run.text for p in d.paragraphs for run in p.runs if run.bold]
        self.assertIn("收入增长 20%", bold)
        self.assertEqual(len(d.tables), 1)
        self.assertEqual(d.tables[0].cell(2, 1).text, "80")
        self.assertEqual(r["version"], 1)

    def test_write_pptx(self):
        import pptx
        self.run_tool("write_pptx", path="汇报.pptx", title="三季度经营", subtitle="管理层会议",
                      slides=[{"title": "收入增长 20%", "bullets": ["新客户贡献一半", "老客户复购稳定"], "notes": "先讲结论"},
                              {"title": "下季度重点", "bullets": ["控制成本"]}])
        prs = pptx.Presentation(self.p("汇报.pptx"))
        alltext = " ".join(sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame)
        for t in ("三季度经营", "收入增长 20%", "新客户贡献一半", "控制成本"):
            self.assertIn(t, alltext)
        self.assertGreaterEqual(len(prs.slides), 3)


class KB(ToolCase):
    """kb_search 的结果改成了规格 3.1 的样子（给模型的文字 + payload 里的 hits），断言随之改写；考的事情不变。"""

    def test_add_and_search(self):
        self.assertIn("资料库是空的", self.run_tool("kb_search", query="任何"))
        self.e.write("资料/售后手册.txt", "设备出现错误码 E-1023 时，先断电重启再联系售后。\n型号 XR-7700 的保修期是两年，电池保修一年。\n")
        self.e.write("资料/财务制度.md", "差旅报销需要发票原件，三十天内提交。超过标准的部分由个人承担。\n")
        r = self.run_tool("kb_add", path="资料")
        self.assertEqual(r["indexed"], 2)
        hit = self.run_tool("kb_search", query="保修期多长").extra["hits"]
        self.assertTrue(hit and hit[0]["path"].endswith("/售后手册.txt"))
        self.assertIn("保修期", hit[0]["text"])
        short = self.run_tool("kb_search", query="报销").extra["hits"]          # 两个字的短词
        self.assertEqual([h["title"] for h in short], ["财务制度.md"])
        self.assertEqual(self.run_tool("kb_search", query="xr").extra["hits"][0]["title"], "售后手册.txt")   # 不区分大小写
        for q in ("E-1023", "XR-7700"):                                  # 错误码、型号
            h = self.run_tool("kb_search", query=q).extra["hits"]
            self.assertTrue(h, q)
            self.assertIn(q, h[0]["text"])
        none = self.run_tool("kb_search", query="火星基地")
        self.assertEqual(none.extra["hits"], [])
        self.assertIn("没有找到", none)
        self.assertIn("不要编造", none)
        # 重新导入同一个文件不会重复
        self.run_tool("kb_add", path="资料/售后手册.txt")
        self.assertEqual(self.e.rt.store.one("SELECT COUNT(*) AS n FROM kb_docs")["n"], 2)


class Compute(ToolCase):
    def test_run_python(self):
        self.e.write("数据.csv", "金额\n1\n2\n3\n")
        r = self.run_tool("run_python", code="import csv\nrows=list(csv.DictReader(open('数据.csv',encoding='utf-8')))\nprint(sum(int(x['金额']) for x in rows))")
        self.assertEqual((r["exit_code"], r["stdout"].strip()), (0, "6"))
        r = self.run_tool("run_python", code="raise SystemExit('出错了')")
        self.assertEqual(r["exit_code"], 1)
        self.assertIn("出错了", r["stderr"])
        self.assertEqual(REGISTRY["run_python"].risk, "exec")


class Misc(ToolCase):
    def test_todo_memory_skill(self):
        r = self.run_tool("todo_write", items=[{"text": "看表", "status": "done"}, {"text": "对账", "status": "doing"}])
        self.assertIn("[x] 看表", r)
        self.assertEqual(self.e.rt.store.session(self.sid)["meta"]["todo"][1]["text"], "对账")
        self.assertEqual(self.e.events(self.sid, ("todo",))[0]["payload"]["items"][0]["status"], "done")
        self.run_tool("memory", action="add", target="workspace", content="汇报用万元为单位")
        self.run_tool("memory", action="add", target="user", content="我是财务总监")
        rows = self.e.rt.store.q("SELECT text, workspace_id FROM memories ORDER BY created")
        self.assertEqual([(r["text"], r["workspace_id"] is None) for r in rows], [("汇报用万元为单位", False), ("我是财务总监", True)])
        with self.assertRaises(ToolError):
            self.run_tool("skill_load", name="没有的技能")

    def test_web_hidden_by_default(self):
        from agent.tools import available
        names = [t.name for t in available(self.ctx)]
        self.assertNotIn("web_fetch", names)
        self.assertIn("read_file", names)


if __name__ == "__main__":
    unittest.main()
