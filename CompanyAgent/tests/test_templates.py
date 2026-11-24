"""带参数的任务模板（docs/借鉴开源项目.md 第 6 节）：{{…}} 的解析、填空、三种来源、接口。"""
from __future__ import annotations
import json, os, unittest
from helpers import ApiCase, APP
from agent import templates as T


def P(prompt):
    return T.parse(prompt)


def brief(p):
    return [(x["name"], x["type"], x["required"], x.get("options"), x.get("default")) for x in p["params"]]


class Parse(unittest.TestCase):
    """十来个写法，含不合法的（笔记里列的就是这些）。"""

    def test_examples(self):
        self.assertEqual(brief(P("分析 {{月份}} 的数")), [("月份", "text", True, None, None)])
        self.assertEqual(brief(P("{{月份:month}}")), [("月份", "month", True, None, None)])
        self.assertEqual(brief(P("{{截止:date}}")), [("截止", "date", True, None, None)])
        self.assertEqual(brief(P("{{页数:number=8}}")), [("页数", "number", True, None, "8")])
        self.assertEqual(brief(P("{{部门:choice=销售|市场|财务}}")), [("部门", "choice", True, ["销售", "市场", "财务"], None)])
        self.assertEqual(brief(P("{{表:file}} 和 {{库:library}}")), [("表", "file", True, None, None), ("库", "library", True, None, None)])
        self.assertEqual(brief(P("{{备注?}}")), [("备注", "text", False, None, None)])
        self.assertEqual(brief(P("{{备注？：text}}")), [("备注", "text", False, None, None)])           # 全角也认
        self.assertEqual(brief(P("{{部门：choice=销售｜市场}}")), [("部门", "choice", True, ["销售", "市场"], None)])
        self.assertEqual(brief(P("{{ 月份 }}，{{月份}}")), [("月份", "text", True, None, None)])       # 同名算一个空

    def test_invalid(self):
        for bad, why in (("{{}}", "空的"), ("{{:month}}", "冒号前面要有名字"), ("{{月份:week}}", "不认识的类型"),
                         ("{{部门:choice}}", "要写选项"), ("{{部门:choice=}}", "要写选项"), ("{{%s}}" % ("长" * 21), "最多"),
                         ("{{a=b}}", "不能有"), ("{{月份:month=十月}}", "默认值")):
            p = P("前 %s 后" % bad)
            self.assertEqual(p["params"], [], bad)
            self.assertTrue(any(why in x for x in p["errors"]), (bad, p["errors"]))
        p = P("开头 {{月份 没有收尾")
        self.assertEqual((p["params"], len(p["errors"])), ([], 1))
        p = P("{{a{{b}}}}")                                                  # 嵌套：里面那个算，外面的原样留着
        self.assertEqual([x["name"] for x in p["params"]], ["b"])
        self.assertTrue(p["errors"])
        p = P("{{x}} {{x:month}}")
        self.assertEqual(brief(p), [("x", "text", True, None, None)])
        self.assertIn("「x」前后写法不一样，按第一次的", p["errors"])


class Render(unittest.TestCase):
    def test_fill(self):
        r = T.render("分析 {{月份:month}} 的经营表，{{页数:number=8}} 页", {"月份": "2026-10"})
        self.assertEqual(r["text"], "分析 2026-10 的经营表，8 页")
        r = T.render("分析 {{月份:month}}", {"月份": "2026年3月"})
        self.assertEqual(r["text"], "分析 2026-03")                         # 规整成 YYYY-MM
        r = T.render("截至 {{d:date}}", {"d": "2026/1/5"})
        self.assertEqual(r["text"], "截至 2026-01-05")

    def test_optional_drops_punct(self):
        tpl = "生成汇总表（Excel），{{特别关注?}}。"
        self.assertEqual(T.render(tpl, {})["text"], "生成汇总表（Excel）。")
        self.assertEqual(T.render(tpl, {"特别关注": "差旅费"})["text"], "生成汇总表（Excel），差旅费。")
        self.assertEqual(T.render("写周报 {{备注?}}", {"备注": "  "})["text"], "写周报")

    def test_missing_invalid(self):
        r = T.render("{{月份:month}} {{部门:choice=销售|市场}} {{n:number}}", {"部门": "法务", "n": "abc"})
        self.assertEqual(r["missing"], ["月份"])
        self.assertEqual([x["name"] for x in r["invalid"]], ["部门", "n"])

    def test_values_not_parsed_again(self):
        r = T.render("给 {{客户}} 写简报", {"客户": "{{月份}} 公司"})
        self.assertEqual(r["text"], "给 {{月份}} 公司 写简报")

    def test_bad_placeholder_left_as_is(self):
        r = T.render("{{月份:week}} 和 {{a}}", {"a": "1"})
        self.assertEqual(r["text"], "{{月份:week}} 和 1")

    def test_explicit_params(self):
        params = T.norm_params([{"name": "季度", "type": "choice", "options": ["Q3", "Q4"], "label": "哪个季度"},
                                {"name": "备注", "required": False}])
        self.assertEqual(params[0]["label"], "哪个季度")
        r = T.render("{{季度}} 汇报，{{备注}}", {"季度": "Q4"}, params)       # 有 params 以它为准（这里的 {{季度}} 是下拉）
        self.assertEqual(r["text"], "Q4 汇报")
        self.assertEqual(T.render("{{季度}}", {"季度": "Q1"}, params)["invalid"][0]["name"], "季度")
        for bad in ([{"name": ""}], [{"name": "a"}, {"name": "a"}], [{"name": "a", "type": "choice"}], [{"name": "a", "type": "x"}], "x"):
            with self.assertRaises(T.TemplateError):
                T.norm_params(bad)


class Builtin(unittest.TestCase):
    def test_builtin_templates(self):
        with open(os.path.join(APP, "agent", "templates.json"), encoding="utf-8") as f:
            raw = json.load(f)
        items = [T.normalize(x, "builtin") for x in raw]
        ids = [x["id"] for x in items]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(x["id"] == r.get("id") for x, r in zip(items, raw)))       # 内置的 id 是写死的短名
        self.assertFalse([x for x in items if x.get("errors")])
        with_params = [x for x in items if x["params"]]
        self.assertGreaterEqual(len(with_params), 5)
        by = {x["id"]: x for x in items}
        self.assertEqual([p["type"] for p in by["monthly-ops"]["params"]], ["month"])
        self.assertEqual([p["type"] for p in by["reconcile"]["params"]], ["file", "file"])
        self.assertEqual(by["quarterly-ppt"]["params"][0]["type"], "choice")


class Api(ApiCase):
    def test_list_crud_render(self):
        st, lst = self.req("GET", "/api/templates?mode=cariai")
        self.assertEqual(st, 200)
        self.assertTrue(all(t["mode"] == "cariai" for t in lst))
        self.assertTrue(all({"id", "title", "prompt", "role", "mode", "params", "source"} <= set(t) for t in lst))
        self.assertTrue(all(t["source"] == "builtin" for t in lst))
        self.assertEqual(self.req("GET", "/api/templates?mode=bad")[0], 400)
        # 新建本机模板
        st, t = self.req("POST", "/api/templates", {"title": "月报", "prompt": "写 {{月份:month}} 的月报，{{备注?}}。", "role": "我的"})
        self.assertEqual(st, 200, t)
        self.assertEqual((t["source"], [p["name"] for p in t["params"]]), ("user", ["月份", "备注"]))
        self.assertTrue(t["id"].startswith("u_"))
        self.assertNotIn("mode", t)
        for m in ("cariai", "hspace"):                                    # 没写 mode：两个模式都有
            self.assertIn(t["id"], [x["id"] for x in self.req("GET", "/api/templates?mode=" + m)[1]])
        with open(os.path.join(self.e.home, "templates.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)[0]["title"], "月报")
        st, t2 = self.req("PATCH", "/api/templates/" + t["id"], {"title": "月度报告", "mode": "hspace"})
        self.assertEqual((st, t2["title"], t2["mode"], t2["prompt"]), (200, "月度报告", "hspace", t["prompt"]))
        self.assertNotIn(t["id"], [x["id"] for x in self.req("GET", "/api/templates?mode=cariai")[1]])
        # 填空
        st, r = self.req("POST", "/api/templates/render", {"id": t["id"], "values": {"月份": "2026-10"}})
        self.assertEqual((st, r), (200, {"text": "写 2026-10 的月报。"}))
        st, r = self.req("POST", "/api/templates/render", {"id": t["id"], "values": {}})
        self.assertEqual((st, r["missing"]), (400, ["月份"]))
        self.assertIn("月份", r["error"])
        st, r = self.req("POST", "/api/templates/render", {"id": t["id"], "values": {"月份": "十月"}})
        self.assertEqual((st, r["invalid"][0]["name"]), (400, "月份"))
        st, r = self.req("POST", "/api/templates/render", {"id": "reconcile", "values": {"销售明细": "a.xlsx", "财务入账表": "b.xlsx"}})
        self.assertEqual(st, 200)
        self.assertTrue(r["text"].startswith("把 a.xlsx 和 b.xlsx 按单号对账"))
        st, r = self.req("POST", "/api/templates/render", {"prompt": "{{a}}！", "values": {"a": "好"}})
        self.assertEqual((st, r["text"]), (200, "好！"))
        self.assertEqual(self.req("POST", "/api/templates/render", {"id": "没有", "values": {}})[0], 404)
        self.assertEqual(self.req("POST", "/api/templates/render", {"values": {}})[0], 400)
        # 解析（界面实时显示）
        st, r = self.req("POST", "/api/templates/parse", {"prompt": "{{a:month}} {{b:x}}"})
        self.assertEqual((st, [p["name"] for p in r["params"]], len(r["errors"])), (200, ["a"], 1))
        # 只能改本机的
        self.assertEqual(self.req("PATCH", "/api/templates/reconcile", {"title": "x"})[0], 403)
        self.assertEqual(self.req("DELETE", "/api/templates/reconcile")[0], 403)
        self.assertEqual(self.req("PATCH", "/api/templates/u_none", {"title": "x"})[0], 404)
        for bad in ({"title": "", "prompt": "x"}, {"title": "x"}, {"title": "x", "prompt": "y", "mode": "z"},
                    {"title": "x", "prompt": "y", "foo": 1}, {"title": "长" * 41, "prompt": "y"},
                    {"title": "x", "prompt": "y", "params": [{"name": "a", "type": "choice"}]}):
            self.assertEqual(self.req("POST", "/api/templates", bad)[0], 400, bad)
        self.assertEqual(self.req("DELETE", "/api/templates/" + t["id"])[0], 200)
        self.assertNotIn(t["id"], [x["id"] for x in self.req("GET", "/api/templates")[1]])

    def test_shared_source_hook(self):
        """公司共享目录的模板由"后端·共享"用 register_source 接进来；同一个 id：本机 > 公司 > 内置。"""
        saved = list(T.SOURCES)
        try:
            T.register_source("shared", lambda rt: [{"id": "co-1", "title": "公司模板", "prompt": "{{部门:choice=销售|财务}} 月报"},
                                                    {"id": "reconcile", "title": "公司版对账", "prompt": "对账", "mode": "cariai"}])
            st, lst = self.req("GET", "/api/templates")
            by = {x["id"]: x for x in lst}
            self.assertEqual(by["co-1"]["source"], "shared")
            self.assertEqual((by["reconcile"]["source"], by["reconcile"]["title"]), ("shared", "公司版对账"))
            self.assertEqual(self.req("DELETE", "/api/templates/co-1")[0], 403)
            T.register_source("shared", lambda rt: 1 / 0)                 # 读不到：当作空的
            self.assertEqual(self.req("GET", "/api/templates")[0], 200)
        finally:
            T.SOURCES[:] = saved


if __name__ == "__main__":
    unittest.main()
