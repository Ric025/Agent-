"""Office 文档工具。数字由程序算，模型只负责说要比什么、要写什么。"""
from __future__ import annotations
import os, re
from decimal import Decimal, InvalidOperation
from . import tool, S, I, A, O, ToolError
from .. import policy
from .fs import backup, publish, write_risk, read_risk


def _need(mod, pip):
    try:
        return __import__(mod)
    except ImportError:
        raise ToolError("缺少组件 %s，请先安装：pip install %s" % (mod, pip))


def _read_path(ctx, path):
    """读文件的路径：当前文件夹里的，或资料库里的（资料库名/相对路径）。资料库的解析由 agent.kb 负责。"""
    try:
        from ..kb import resolve_read
    except ImportError:
        return policy.resolve(ctx.root, path)
    return resolve_read(ctx, path)


def _shown(ctx, p, given):
    """给模型看的路径：在当前文件夹里就写相对路径，否则照用户给的写法（资料库路径）。"""
    rel = policy.relpath(ctx.root, p)
    return given if rel == ".." or rel.startswith("../") else rel


def _open_wb(ctx, path):
    openpyxl = _need("openpyxl", "openpyxl")
    p = _read_path(ctx, path)
    if not os.path.isfile(p):
        raise ToolError("找不到文件：%s" % path)
    return p, openpyxl.load_workbook(p, read_only=True, data_only=True)


def _sheet(wb, name, path):
    if name in wb.sheetnames:
        return wb[name]
    raise ToolError("%s 里没有工作表「%s」，可用的有：%s" % (path, name, "、".join(wb.sheetnames)))


def _table(ws, limit=None):
    """读出表头和数据行。第一个非空行当表头。返回 (表头, [(行号, 值列表)])。"""
    header, rows = None, []
    for n, row in enumerate(ws.iter_rows(values_only=True), 1):
        if header is None:
            if any(c is not None and str(c).strip() for c in row):
                header = [("" if c is None else str(c).strip()) for c in row]
            continue
        if not any(c is not None and str(c).strip() != "" for c in row):
            continue
        rows.append((n, list(row) + [None] * (len(header) - len(row))))
        if limit and len(rows) >= limit:
            break
    return header or [], rows


def _key(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
    s = str(v).strip().replace(",", "").replace("，", "").replace("¥", "").replace("￥", "")
    if not s or not re.fullmatch(r"[-+]?\d+(\.\d+)?", s):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _show(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return ("%.6f" % v).rstrip("0").rstrip(".")
    return str(v)


@tool("excel_inspect", "查看工作簿结构：有哪些工作表、列名、行数、前几行样例。处理表格前先用它", {"path": S("xlsx 路径")}, risk=read_risk("path"), sub_ok=True)
def excel_inspect(ctx, path):
    p, wb = _open_wb(ctx, path)
    sheets = []
    for ws in wb.worksheets:
        header, rows = _table(ws)
        sheets.append({"name": ws.title, "rows": len(rows), "columns": header,
                       "sample": [[_show(c) for c in r[:len(header)]] for _, r in rows[:3]]})
    wb.close()
    return {"path": _shown(ctx, p, path), "sheets": sheets}


@tool("excel_read", "读取工作表的部分数据行（返回带列名的记录）",
      {"path": S("xlsx 路径"), "sheet": S("工作表名"), "start": I("从第几条数据开始，默认 1"), "limit": I("最多多少条，默认 30，最多 200")},
      required=["path", "sheet"], risk=read_risk("path"), sub_ok=True)
def excel_read(ctx, path, sheet, start=1, limit=30):
    p, wb = _open_wb(ctx, path)
    header, rows = _table(_sheet(wb, sheet, path))
    wb.close()
    limit = max(1, min(int(limit or 30), 200))
    start = max(1, int(start or 1))
    part = rows[start - 1: start - 1 + limit]
    return {"columns": header, "total_rows": len(rows), "start": start,
            "records": [dict(zip(header, [_show(c) for c in r])) for _, r in part]}


_STR = {"type": "string"}


@tool("excel_diff", "比两张表（A 旧 B 新），出差异工作簿 output_path(.xlsx)。mode=key：按 key_a/key_b 主键逐行对账，compare 默认同名列；"
      "cells：按位置逐格比，不给 sheet 就比全部同名表。不给 mode：有 key_a 用 key，否则 cells",
      {"path_a": _STR, "path_b": _STR, "output_path": _STR, "mode": {"type": "string", "enum": ["key", "cells"]},
       "sheet_a": _STR, "sheet_b": _STR, "key_a": {"type": "array", "items": _STR}, "key_b": {"type": "array", "items": _STR},
       "compare": {"type": "array", "items": {"type": "object", "properties": {"a": _STR, "b": _STR}}}},
      required=["path_a", "path_b", "output_path"],
      risk=read_risk("path_a", "path_b", base=lambda ctx, a: write_risk(ctx, {"path": a.get("output_path", "")})))
def excel_diff(ctx, path_a, path_b, output_path, mode=None, sheet_a=None, sheet_b=None, key_a=None, key_b=None, compare=None):
    _need("openpyxl", "openpyxl")
    if mode not in (None, "", "key", "cells"):
        raise ToolError("mode 只能是 key 或 cells")
    mode = mode or ("key" if (key_a or key_b) else "cells")
    out = policy.resolve(ctx.root, output_path, write=True)
    if not out.lower().endswith(".xlsx"):
        raise ToolError("output_path 必须以 .xlsx 结尾")
    if mode == "cells":
        return _diff_cells(ctx, path_a, path_b, out, sheet_a, sheet_b)
    return _diff_keys(ctx, path_a, sheet_a, path_b, sheet_b, key_a or [], key_b or [], compare, out)


def _save_diff(ctx, wb, out, note):
    from openpyxl.styles import Font
    for sh in wb.worksheets:
        for c in sh[1]:
            c.font = Font(bold=True)
        sh.freeze_panes = "A2"
    old = backup(ctx, out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    wb.save(out)
    return publish(ctx, out, note), old


def _diff_keys(ctx, path_a, sheet_a, path_b, sheet_b, key_a, key_b, compare, out):
    openpyxl = _need("openpyxl", "openpyxl")
    if len(key_a) != len(key_b) or not key_a:
        raise ToolError("key_a 和 key_b 必须一一对应且不能为空")
    if compare is not None and not all(isinstance(c, dict) and "a" in c and "b" in c for c in compare):
        raise ToolError('compare 的每一项要写成 {"a": 表A列名, "b": 表B列名}')
    sides = []
    for path, sheet in ((path_a, sheet_a), (path_b, sheet_b)):
        p, wb = _open_wb(ctx, path)
        ws = _sheet(wb, sheet, path) if sheet else wb.worksheets[0]
        header, rows = _table(ws)
        wb.close()
        sides.append((path, ws.title, header, rows))
    if compare is None:     # 没给要比的列：比两边同名的列（主键除外）
        compare = [{"a": c, "b": c} for c in sides[0][2] if c and c in sides[1][2] and c not in key_a and c not in key_b]
    built = []
    for (path, sheet, header, rows), keys, cols in ((sides[0], key_a, [c["a"] for c in compare]), (sides[1], key_b, [c["b"] for c in compare])):
        missing = [c for c in keys + cols if c not in header]
        if missing:
            raise ToolError("%s 的「%s」里没有这些列：%s。实际列名：%s" % (path, sheet, "、".join(missing), "、".join(header)))
        index, dups = {}, {}
        for n, r in rows:
            k = tuple(_key(r[header.index(c)]) for c in keys)
            if k in index:
                dups.setdefault(k, [index[k][0]]).append(n)
            else:
                index[k] = (n, r)
        built.append({"header": header, "rows": rows, "index": index, "dups": dups})
    a, b = built
    tol = Decimal("0.005")
    mismatches, sums = [], []
    for c in compare:
        ia, ib = a["header"].index(c["a"]), b["header"].index(c["b"])
        sa = sb = Decimal(0)
        numeric = False
        for k, (na, ra) in a["index"].items():
            va = ra[ia]
            x = _num(va)
            if x is not None:
                sa += x
                numeric = True
            if k not in b["index"]:
                continue
            nb, rb = b["index"][k]
            vb = rb[ib]
            y = _num(vb)
            if x is not None and y is not None:
                if abs(x - y) > tol:
                    mismatches.append((k, c["a"], _show(va), _show(vb), str(x - y), na, nb))
            elif _key(va) != _key(vb):
                mismatches.append((k, c["a"], _show(va), _show(vb), "", na, nb))
        for k, (nb, rb) in b["index"].items():
            y = _num(rb[ib])
            if y is not None:
                sb += y
        if numeric:
            sums.append({"column": c["a"], "sum_a": str(sa), "sum_b": str(sb), "diff": str(sa - sb)})
    only_a = [(k, a["index"][k][0]) for k in a["index"] if k not in b["index"]]
    only_b = [(k, b["index"][k][0]) for k in b["index"] if k not in a["index"]]
    matched = len(a["index"]) - len(only_a)
    bad_keys = {m[0] for m in mismatches}
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "汇总"
    for row in [["项目", "数值"], ["表A数据行", len(a["rows"])], ["表B数据行", len(b["rows"])], ["两边都有的主键", matched],
                ["其中有差异的主键", len(bad_keys)], ["差异单元格", len(mismatches)], ["仅表A有", len(only_a)], ["仅表B有", len(only_b)],
                ["表A重复主键", len(a["dups"])], ["表B重复主键", len(b["dups"])]]:
        ws.append(row)
    ws.append([])
    ws.append(["比较列", "表A合计", "表B合计", "差额(A-B)"])
    for s in sums:
        ws.append([s["column"], float(s["sum_a"]), float(s["sum_b"]), float(s["diff"])])
    ws2 = wb.create_sheet("不一致")
    ws2.append(list(key_a) + ["列", "表A值", "表B值", "差额(A-B)", "表A行号", "表B行号"])
    for m in mismatches:
        ws2.append(list(m[0]) + [m[1], m[2], m[3], m[4], m[5], m[6]])
    for title, items, hdr, src in (("仅表A有", only_a, a["header"], a), ("仅表B有", only_b, b["header"], b)):
        w = wb.create_sheet(title)
        w.append(["行号"] + hdr)
        for k, n in items:
            w.append([n] + [_show(c) for c in src["index"][k][1][:len(hdr)]])
    w = wb.create_sheet("重复键")
    w.append(["来源", "主键", "出现的行号"])
    for name, side in (("表A", a), ("表B", b)):
        for k, ns in side["dups"].items():
            w.append([name, " / ".join(k), "、".join(map(str, ns))])
    ver, old = _save_diff(ctx, wb, out, "对账差异表")
    return {"output": policy.relpath(ctx.root, out), "version": ver, "rows_a": len(a["rows"]), "rows_b": len(b["rows"]),
            "matched_keys": matched, "keys_with_diff": len(bad_keys), "diff_cells": len(mismatches),
            "only_in_a": len(only_a), "only_in_b": len(only_b), "duplicate_keys_a": len(a["dups"]), "duplicate_keys_b": len(b["dups"]),
            "sums": sums, "samples": [{"key": " / ".join(m[0]), "column": m[1], "a": m[2], "b": m[3], "diff": m[4]} for m in mismatches[:10]],
            "note": "以上数字由程序计算。向用户汇报时请原样引用，不要改动。", "previous_saved_as": old}


_CELL_LIST = 20          # 返回值里最多列多少个变了的格子
_CELL_SHEET_MAX = 100000  # 差异工作簿里最多写多少行


def _grid(ws):
    """整张表按位置读成二维列表，去掉末尾的空行空列。"""
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    blank = lambda v: v is None or (isinstance(v, str) and not v.strip())
    while rows and all(blank(v) for v in rows[-1]):
        rows.pop()
    ncol = 0
    for r in rows:
        for i in range(len(r) - 1, -1, -1):
            if not blank(r[i]):
                ncol = max(ncol, i + 1)
                break
    return [r[:ncol] + [None] * (ncol - len(r[:ncol])) for r in rows], ncol


def _same(x, y):
    if (x is None or x == "") and (y is None or y == ""):
        return True, ""
    nx, ny = _num(x), _num(y)
    if nx is not None and ny is not None:
        return nx == ny, str(ny - nx)
    return _key(x) == _key(y), ""


def _diff_cells(ctx, path_a, path_b, out, sheet_a, sheet_b):
    openpyxl = _need("openpyxl", "openpyxl")
    from openpyxl.utils import get_column_letter
    pa, wa = _open_wb(ctx, path_a)
    pb, wbb = _open_wb(ctx, path_b)
    try:
        if sheet_a or sheet_b:
            na, nb = sheet_a or sheet_b, sheet_b or sheet_a
            pairs = [(_sheet(wa, na, path_a), _sheet(wbb, nb, path_b))]
            only_a, only_b = [], []
        else:
            pairs = [(wa[n], wbb[n]) for n in wa.sheetnames if n in wbb.sheetnames]
            only_a = [n for n in wa.sheetnames if n not in wbb.sheetnames]
            only_b = [n for n in wbb.sheetnames if n not in wa.sheetnames]
        sheets, changes, total = [], [], 0
        for sa, sb in pairs:
            ga, ca = _grid(sa)
            gb, cb = _grid(sb)
            name = sa.title if sa.title == sb.title else "%s / %s" % (sa.title, sb.title)
            n = 0
            for r in range(max(len(ga), len(gb))):
                ra = ga[r] if r < len(ga) else []
                rb = gb[r] if r < len(gb) else []
                for c in range(max(ca, cb)):
                    x = ra[c] if c < len(ra) else None
                    y = rb[c] if c < len(rb) else None
                    eq, d = _same(x, y)
                    if not eq:
                        n += 1
                        changes.append((name, "%s%d" % (get_column_letter(c + 1), r + 1), _show(x), _show(y), d))
            total += n
            info = {"name": name, "rows": [len(ga), len(gb)], "cols": [ca, cb], "changed_cells": n}
            if len(ga) != len(gb) and n > max(ca, cb) * 3 and n * 2 > max(ca, cb) * max(1, min(len(ga), len(gb))):
                info["hint"] = "行数变了且很多格子不同，可能是中间插入或删除了行，按主键比更准"
            sheets.append(info)
    finally:
        wa.close()
        wbb.close()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "汇总"
    ws.append(["工作表", "表A行数", "表A列数", "表B行数", "表B列数", "变了的格子"])
    for s in sheets:
        ws.append([s["name"], s["rows"][0], s["cols"][0], s["rows"][1], s["cols"][1], s["changed_cells"]])
    for n in only_a:
        ws.append([n, "仅表A有"])
    for n in only_b:
        ws.append([n, "仅表B有"])
    w = wb.create_sheet("逐格差异")
    w.append(["工作表", "位置", "旧值(A)", "新值(B)", "差(B-A)"])
    for ch in changes[:_CELL_SHEET_MAX]:
        w.append([ch[0], ch[1], ch[2], ch[3], float(ch[4]) if ch[4] else ""])
    if len(changes) > _CELL_SHEET_MAX:
        w.append(["（另有 %d 处没写入）" % (len(changes) - _CELL_SHEET_MAX)])
    ver, old = _save_diff(ctx, wb, out, "逐格差异表")
    res = {"mode": "cells", "output": policy.relpath(ctx.root, out), "version": ver, "a": _shown(ctx, pa, path_a), "b": _shown(ctx, pb, path_b),
           "sheets_only_in_a": only_a, "sheets_only_in_b": only_b, "sheets": sheets, "changed_cells": total,
           "changes": [{"sheet": c[0], "cell": c[1], "old": c[2], "new": c[3], "diff": c[4]} for c in changes[:_CELL_LIST]]}
    if total > _CELL_LIST:
        res["more"] = "另有 %d 处没列出，见差异工作簿的「逐格差异」" % (total - _CELL_LIST)
    if not pairs:
        res["more"] = "两个文件没有同名的工作表，请用 sheet_a、sheet_b 指定要比的表"
    res["note"] = "以上数字由程序计算，diff 是新值减旧值。向用户汇报时请原样引用。"
    res["previous_saved_as"] = old
    return res


@tool("write_xlsx", "生成 Excel 工作簿。每个单元格是一个值；以 = 开头的字符串会成为公式",
      {"path": S("输出路径，.xlsx"), "sheets": A("工作表", O({"name": S("表名"), "rows": A("行，第一行是表头", {"type": "array", "items": {}})}))},
      risk=write_risk)
def write_xlsx(ctx, path, sheets):
    openpyxl = _need("openpyxl", "openpyxl")
    from openpyxl.styles import Font
    p = policy.resolve(ctx.root, path, write=True)
    if not p.lower().endswith(".xlsx"):
        raise ToolError("path 必须以 .xlsx 结尾")
    if not sheets:
        raise ToolError("至少要有一个工作表")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    cells = 0
    for sh in sheets:
        ws = wb.create_sheet(str(sh["name"])[:31] or "Sheet")
        widths = {}
        for row in sh["rows"]:
            ws.append([c if isinstance(c, (int, float, str)) or c is None else str(c) for c in row])
            cells += len(row)
            for i, c in enumerate(row):
                w = sum(2 if ord(ch) > 255 else 1 for ch in str(c if c is not None else ""))
                widths[i] = min(60, max(widths.get(i, 6), w + 2))
        for c in ws[1]:
            c.font = Font(bold=True)
        ws.freeze_panes = "A2"
        for i, w in widths.items():
            ws.column_dimensions[openpyxl.utils.get_column_letter(i + 1)].width = w
    old = backup(ctx, p)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    wb.save(p)
    ver = publish(ctx, p, "工作簿")
    return {"path": policy.relpath(ctx.root, p), "sheets": len(sheets), "cells": cells, "version": ver, "previous_saved_as": old}


_INLINE = re.compile(r"(\*\*[^*]+\*\*)")


def _runs(par, text):
    for part in _INLINE.split(text):
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            par.add_run(part[2:-2]).bold = True
        elif part:
            par.add_run(part)


@tool("write_docx", "根据 Markdown 生成 Word 文档。支持标题(#)、段落、列表(- 或 1.)、表格(|)、加粗(**)",
      {"path": S("输出路径，.docx"), "markdown": S("正文 Markdown")}, risk=write_risk)
def write_docx(ctx, path, markdown):
    docx = _need("docx", "python-docx")
    from docx.oxml.ns import qn
    p = policy.resolve(ctx.root, path, write=True)
    if not p.lower().endswith(".docx"):
        raise ToolError("path 必须以 .docx 结尾")
    d = docx.Document()
    for name in ("Normal", "Heading 1", "Heading 2", "Heading 3", "List Bullet", "List Number"):
        try:
            st = d.styles[name]
            st.font.name = "Microsoft YaHei"
            st.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
        except Exception:
            pass
    lines = markdown.replace("\r\n", "\n").split("\n")
    i, blocks = 0, 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if not ln.strip():
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            d.add_heading(m.group(2).strip(), level=min(len(m.group(1)), 3))
        elif ln.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", lines[i + 1]) and "-" in lines[i + 1]:
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                if not re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", lines[i]):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            ncol = max(len(r) for r in rows)
            t = d.add_table(rows=len(rows), cols=ncol)
            t.style = "Table Grid"
            for ri, r in enumerate(rows):
                for ci in range(ncol):
                    cell = t.cell(ri, ci)
                    cell.text = ""
                    _runs(cell.paragraphs[0], r[ci] if ci < len(r) else "")
                    if ri == 0:
                        for run in cell.paragraphs[0].runs:
                            run.bold = True
            blocks += 1
            continue
        elif re.match(r"^\s*[-*]\s+", ln):
            _runs(d.add_paragraph(style="List Bullet"), re.sub(r"^\s*[-*]\s+", "", ln))
        elif re.match(r"^\s*\d+[.)]\s+", ln):
            _runs(d.add_paragraph(style="List Number"), re.sub(r"^\s*\d+[.)]\s+", "", ln))
        else:
            _runs(d.add_paragraph(), ln.strip())
        blocks += 1
        i += 1
    old = backup(ctx, p)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    d.save(p)
    ver = publish(ctx, p, "Word 文档")
    return {"path": policy.relpath(ctx.root, p), "blocks": blocks, "version": ver, "previous_saved_as": old}


# ---------------- PPT ----------------

_PH_NAMES = {1: "title", 3: "center_title", 4: "subtitle", 2: "body", 7: "object", 18: "picture", 8: "chart", 12: "table",
             9: "bitmap", 10: "media", 11: "org_chart", 14: "header", 5: "vertical_title", 6: "vertical_body", 17: "vertical_object"}
_LATENT = (13, 15, 16)    # 页码、页脚、日期：新建页时不会带上，也不算版式的内容


def _ph_type(ph):
    try:
        return int(ph.placeholder_format.type)
    except Exception:
        return None


def _vertical(ph):
    try:
        return (ph._element.txBody.bodyPr.get("vert") or "horz") not in ("horz", "")
    except Exception:
        return False


def _area(ph):
    try:
        return (ph.width or 0) * (ph.height or 0)
    except Exception:
        return 0


def _pick_layouts(prs):
    """按占位符的类型认版式（不看名字）。返回 (标题版式, 内容版式, 空白版式)，没有的是 None。"""
    title_l = content_l = None
    best_t = best_c = None
    blank_l, blank_n = None, None
    for master in prs.slide_masters:
        for lay in master.slide_layouts:
            phs = [ph for ph in lay.placeholders if _ph_type(ph) not in _LATENT]
            types = [_ph_type(ph) for ph in phs]
            if blank_n is None or len(phs) < blank_n:
                blank_l, blank_n = lay, len(phs)
            has_title = 1 in types or 3 in types
            if has_title and 4 in types:
                score = (3 in types, -len(phs))
                if best_t is None or score > best_t:
                    title_l, best_t = lay, score
            bodies = [ph for ph in phs if _ph_type(ph) in (2, 7) and not _vertical(ph)]
            others = [ph for ph in phs if _ph_type(ph) not in (1, 3, 2, 7)]
            if 1 in types and bodies and not any(_vertical(ph) for ph in phs if _ph_type(ph) in (1, 3)):
                score = (len(bodies) == 1 and not others, max(_area(ph) for ph in bodies))
                if best_c is None or score > best_c:
                    content_l, best_c = lay, score
    return title_l, content_l, blank_l


def _layout_use(prs):
    t, c, _ = _pick_layouts(prs)
    return {id(t): "标题页", id(c): "内容页"} if t is not c else {id(c): "标题页、内容页"}


def _open_pptx(p):
    """打开 .pptx；.potx（模板格式）在内存里改一下内容类型再打开，原文件不动。"""
    pptx = _need("pptx", "python-pptx")
    if not p.lower().endswith(".potx"):
        return pptx.Presentation(p)
    import io, zipfile
    src, buf = zipfile.ZipFile(p), io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "[Content_Types].xml":
                data = data.replace(b"presentationml.template.main+xml", b"presentationml.presentation.main+xml")
            dst.writestr(item, data)
    src.close()
    buf.seek(0)
    return pptx.Presentation(buf)


def _clear_slides(prs):
    """去掉全部页：从页列表里删掉，再去掉到页的关系，保存时这些页就不会写进文件。
    节（section）和自定义放映也引用页，一并去掉，免得 PowerPoint 打开时要"修复"。"""
    pres = prs.part._element
    for el in list(pres):
        if el.tag.endswith("}custShowLst"):
            pres.remove(el)
    ext_lst = [el for el in pres if el.tag.endswith("}extLst")]
    for lst in ext_lst:
        for ext in list(lst):
            if any(ch.tag.endswith("}sectionLst") for ch in ext):
                lst.remove(ext)
    sld_lst = prs.slides._sldIdLst
    for sld in list(sld_lst):
        rid = sld.rId
        sld_lst.remove(sld)
        prs.part.drop_rel(rid)


def _level(b):
    s = str(b)
    n = len(s) - len(s.lstrip(" "))
    return min(n // 2, 4), s.strip()


def _scale(prs):
    from pptx.util import Inches
    return prs.slide_width / Inches(13.333), prs.slide_height / Inches(7.5)


def _draw(prs, slide, kind, title, body, n=None):
    """自己画的版式（没有模板，或模板里没有合适的版式时）。按页面尺寸缩放。"""
    from pptx.util import Pt, Emu, Inches
    from pptx.dml.color import RGBColor
    sx, sy = _scale(prs)
    ink, grey, accent = RGBColor(0x1A, 0x1A, 0x1A), RGBColor(0x66, 0x66, 0x66), RGBColor(0xB4, 0x23, 0x18)

    def at(x, y, w, h):
        return Emu(int(Inches(x) * sx)), Emu(int(Inches(y) * sy)), Emu(int(Inches(w) * sx)), Emu(int(Inches(h) * sy))

    def box(x, y, w, h, text, size, bold=False, color=ink):
        tb = slide.shapes.add_textbox(*at(x, y, w, h))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.paragraphs[0].text = text
        for r in tf.paragraphs[0].runs:
            r.font.size, r.font.bold, r.font.color.rgb, r.font.name = Pt(size), bold, color, "Microsoft YaHei"
        return tf

    def rule(y):
        ln = slide.shapes.add_shape(1, *at(0.7, y, 0.9, 0.06))
        ln.fill.solid()
        ln.fill.fore_color.rgb = accent
        ln.line.fill.background()

    if kind == "title":
        rule(2.6)
        box(0.7, 2.8, 11.9, 1.6, title, 40, True)
        if body:
            box(0.7, 4.4, 11.9, 0.8, body, 20, False, grey)
        return
    rule(0.65)
    box(0.7, 0.8, 11.9, 1.0, title, 28, True)
    tf = box(0.7, 2.0, 11.9, 4.8, "", 20)
    for i, b in enumerate(body):
        lv, text = _level(b)
        par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        par.text = ("    " * lv) + ("•  " if lv == 0 else "–  ") + text
        par.space_after = Pt(10)
        for r in par.runs:
            r.font.size, r.font.color.rgb, r.font.name = Pt(20 if len(body) <= 5 else 17), ink, "Microsoft YaHei"
    if n is not None:
        box(12.0, 6.9, 1.0, 0.4, str(n), 11, False, grey)


def _fill_body(ph, bullets):
    """往模板的正文占位符里填要点：只写字和层级，字体、颜色、项目符号都用模板的。要点多时让文字缩小以适应框。"""
    from pptx.enum.text import MSO_AUTO_SIZE
    tf = ph.text_frame
    for i, b in enumerate(bullets):
        lv, text = _level(b)
        par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        par.text = text
        par.level = lv
    weight = max(len(bullets), sum(len(_level(b)[1]) for b in bullets) / 40.0)
    if weight > 6:
        tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
        fit = tf._txBody.bodyPr.find("{http://schemas.openxmlformats.org/drawingml/2006/main}normAutofit")
        if fit is not None:
            fit.set("fontScale", str(max(55000, int(100000 * 6 / weight) // 2500 * 2500)))
            fit.set("lnSpcReduction", "10000")


def _drop(shape):
    el = shape._element
    el.getparent().remove(el)


def _from_layout(prs, layout, title, body, kind):
    """用模板的版式建一页，填标题和正文；没用上的占位符去掉（免得放映前显示"单击此处添加文本"）。"""
    s = prs.slides.add_slide(layout)
    used = set()
    tph = next((ph for ph in s.placeholders if _ph_type(ph) in (1, 3)), None)
    if tph is not None:
        tph.text_frame.text = title
        used.add(tph.placeholder_format.idx)
    if kind == "title":
        sub = next((ph for ph in s.placeholders if _ph_type(ph) == 4), None)
        if sub is not None and body:
            sub.text_frame.text = body
            used.add(sub.placeholder_format.idx)
    else:
        bodies = [ph for ph in s.placeholders if _ph_type(ph) in (2, 7) and not _vertical(ph)]
        if bodies:
            ph = max(bodies, key=_area)
            _fill_body(ph, body)
            used.add(ph.placeholder_format.idx)
    for ph in list(s.placeholders):
        if ph.placeholder_format.idx not in used:
            _drop(ph)
    return s


def _blank_slide(prs, layout):
    s = prs.slides.add_slide(layout)
    for ph in list(s.placeholders):
        _drop(ph)
    return s


@tool("write_pptx", "根据大纲生成 PPT。每页一个标题和几条要点，要点要短，前面空两格是下一级。"
      "template：公司已有的 .pptx，用它的母版和版式（先用 pptx_outline 看）",
      {"path": S("输出路径，.pptx"), "title": S("整份的标题"), "subtitle": _STR, "template": _STR,
       "slides": A("", O({"title": _STR, "bullets": {"type": "array", "items": _STR}, "notes": S("备注")}, ["title", "bullets"]))},
      required=["path", "title", "slides"], risk=read_risk("template", base=write_risk))
def write_pptx(ctx, path, title, slides, subtitle="", template=None):
    pptx = _need("pptx", "python-pptx")
    from pptx.util import Inches
    p = policy.resolve(ctx.root, path, write=True)
    if not p.lower().endswith(".pptx"):
        raise ToolError("path 必须以 .pptx 结尾")
    info, notes = {}, []
    if template:
        tp = _read_path(ctx, template)
        if not os.path.isfile(tp):
            raise ToolError("找不到模板：%s" % template)
        if not tp.lower().endswith((".pptx", ".potx")):
            raise ToolError("template 要是 .pptx 或 .potx 文件")
        if os.path.realpath(tp) == os.path.realpath(p):
            raise ToolError("path 不能和 template 是同一个文件，换个文件名")
        try:
            prs = _open_pptx(tp)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError("模板打不开：%s（%s）" % (template, e))
        _clear_slides(prs)
        title_l, content_l, blank_l = _pick_layouts(prs)
        if blank_l is None:
            raise ToolError("模板里一个版式都没有：%s" % template)
        info = {"template": _shown(ctx, tp, template),
                "layouts": {"title": title_l.name if title_l else None, "content": content_l.name if content_l else None}}
    else:
        prs = pptx.Presentation()
        prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
        title_l = content_l = None
        blank_l = prs.slide_layouts[6]
    if title_l is not None:
        _from_layout(prs, title_l, title, subtitle, "title")
    else:
        _draw(prs, _blank_slide(prs, blank_l), "title", title, subtitle)
        if template:
            notes.append("模板里没有标题版式，标题页用了默认画法")
    for n, sl in enumerate(slides, 2):
        bullets = [b for b in sl.get("bullets", []) if str(b).strip()][:8]
        if content_l is not None:
            s = _from_layout(prs, content_l, sl["title"], bullets, "content")
        else:
            s = _blank_slide(prs, blank_l)
            _draw(prs, s, "content", sl["title"], bullets, n)
        if sl.get("notes"):
            s.notes_slide.notes_text_frame.text = sl["notes"]
    if template and content_l is None:
        notes.append("模板里没有带正文的版式，内容页用了默认画法")
    old = backup(ctx, p)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    prs.save(p)
    ver = publish(ctx, p, "演示文稿")
    res = {"path": policy.relpath(ctx.root, p), "slides": len(slides) + 1, "version": ver, "previous_saved_as": old}
    res.update(info)
    if notes:
        res["note"] = "；".join(notes)
    return res


_OUTLINE_MAX = 6000


def _cut(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n] + "…"


def _shape_items(shapes, acc):
    for sh in shapes:
        if sh.shape_type == 6:      # 组合：往里找
            _shape_items(sh.shapes, acc)
            continue
        if getattr(sh, "has_chart", False) and sh.has_chart:
            acc["has"]["chart"] = acc["has"].get("chart", 0) + 1
        elif getattr(sh, "has_table", False) and sh.has_table:
            acc["has"]["table"] = acc["has"].get("table", 0) + 1
            rows = [" | ".join(c.text.strip() for c in r.cells) for r in sh.table.rows]
            acc["tables"].append(" / ".join(rows))
        elif sh.shape_type == 13 or (sh.is_placeholder and hasattr(sh, "image")):
            acc["has"]["picture"] = acc["has"].get("picture", 0) + 1
        if sh.is_placeholder and _ph_type(sh) in (1, 3) and not acc["title"]:
            acc["title"] = sh.text_frame.text if sh.has_text_frame else ""
            continue
        if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
            for par in sh.text_frame.paragraphs:
                t = "".join(r.text for r in par.runs) or par.text
                if t.strip():
                    acc["bullets"].append((par.level, t))


@tool("pptx_outline", "看一份 PPT 的结构：每页的版式、标题、要点（前面空两格是下一级）、表格、图表图片、备注，和可用的版式",
      {"path": _STR}, risk=read_risk("path"), sub_ok=True)
def pptx_outline(ctx, path):
    _need("pptx", "python-pptx")
    p = _read_path(ctx, path)
    if not os.path.isfile(p):
        raise ToolError("找不到文件：%s" % path)
    if not p.lower().endswith((".pptx", ".potx")):
        raise ToolError("只能看 .pptx 或 .potx 文件")
    try:
        prs = _open_pptx(p)
    except ToolError:
        raise
    except Exception as e:
        raise ToolError("文件打不开：%s（%s）" % (path, e))
    w, h = prs.slide_width or 0, prs.slide_height or 0
    ratio = "16:9" if h and abs(w / h - 16 / 9) < 0.02 else "4:3" if h and abs(w / h - 4 / 3) < 0.02 else "%.2f:1" % (w / h if h else 0)
    use = _layout_use(prs)
    layouts = []
    for master in prs.slide_masters:
        for lay in master.slide_layouts:
            item = {"name": lay.name, "placeholders": [_PH_NAMES.get(_ph_type(ph), "other") for ph in lay.placeholders
                                                         if _ph_type(ph) not in _LATENT]}
            if id(lay) in use:
                item["write_pptx_uses_for"] = use[id(lay)]
            layouts.append(item)
    total = len(prs.slides)
    tight = total > 15
    lim = {"title": 60, "bullet": 50 if tight else 100, "bullets": 6 if tight else 12, "table": 80 if tight else 200, "notes": 60 if tight else 200}
    res = {"path": _shown(ctx, p, path), "slides_total": total,
           "size": "%.1f×%.1f cm（%s）" % (w / 360000.0, h / 360000.0, ratio),
           "layouts": layouts[:20], "slides": []}
    if len(layouts) > 20:
        res["layouts_more"] = len(layouts) - 20
    import json
    used = len(json.dumps(res, ensure_ascii=False))
    for n, s in enumerate(prs.slides, 1):
        acc = {"title": "", "bullets": [], "tables": [], "has": {}}
        _shape_items(s.shapes, acc)
        item = {"n": n, "layout": s.slide_layout.name, "title": _cut(acc["title"], lim["title"])}
        bl = acc["bullets"]
        if bl:
            item["bullets"] = ["  " * min(lv, 4) + _cut(t, lim["bullet"]) for lv, t in bl[:lim["bullets"]]]
            if len(bl) > lim["bullets"]:
                item["bullets"].append("（还有 %d 条）" % (len(bl) - lim["bullets"]))
        if acc["tables"]:
            item["tables"] = [_cut(t, lim["table"]) for t in acc["tables"][:2]]
        if acc["has"]:
            item["has"] = acc["has"]
        try:
            nt = s.notes_slide.notes_text_frame.text if s.has_notes_slide else ""
        except Exception:
            nt = ""
        if nt and nt.strip():
            item["notes"] = _cut(nt, lim["notes"])
        size = len(json.dumps(item, ensure_ascii=False)) + 1
        if used + size > _OUTLINE_MAX - 60:
            res["more"] = "还有 %d 页没列出（第 %d 页起）" % (total - n + 1, n)
            break
        res["slides"].append(item)
        used += size
    return res


# ---------------- excel_query：不写代码查表里的数 ----------------

_OPS = ("=", "!=", ">", ">=", "<", "<=", "contains", "in")
_AGG = {"sum": "合计", "avg": "平均", "count": "计数", "min": "最小", "max": "最大"}
_TOTAL_WORDS = ("合计", "总计", "小计", "总和", "total", "subtotal", "grand total")


def _norm(s):
    import unicodedata
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s if s is not None else ""))).casefold()


def _qnum(v):
    """数字或能看成数字的文字（千分位、货币符号、"元"、全角、百分号）。不是数字返回 None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
    if not isinstance(v, str):
        return None
    import unicodedata
    s = unicodedata.normalize("NFKC", v).strip().replace(" ", "")
    pct = s.endswith("%")
    s = re.sub(r"^[¥$€£]|[¥$€£]$", "", s.rstrip("%"))
    s = re.sub(r"(元|人民币|rmb|cny)$", "", s, flags=re.I).replace(",", "")
    if not re.fullmatch(r"[-+]?(\d+(\.\d*)?|\.\d+)", s):
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d / 100 if pct else d


def _qdate(v):
    import datetime as dt
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    if isinstance(v, str):
        import unicodedata
        s = unicodedata.normalize("NFKC", v).strip()
        m = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?", s)
        if m:
            try:
                return dt.datetime(*[int(x) for x in m.groups() if x is not None])
            except ValueError:
                return None
        m = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})月?", s)
        if m:
            try:
                return dt.datetime(int(m.group(1)), int(m.group(2)), 1)
            except ValueError:
                return None
    return None


def _cmp_pair(x, y):
    """把单元格值 x 和条件值 y 变成可比较的一对：都是日期、都是数字，或都是规整后的文字。"""
    dx = _qdate(x)
    if dx is not None:
        dy = _qdate(y)
        if dy is not None:
            return dx, dy
    nx, ny = _qnum(x), _qnum(y)
    if nx is not None and ny is not None:
        return nx, ny
    return _norm(x), _norm(y)


def _match(x, op, y):
    if op == "in":
        vals = y if isinstance(y, list) else [y]
        return any(_match(x, "=", v) for v in vals)
    if op == "contains":
        return _norm(y) in _norm(x)
    if x is None or (isinstance(x, str) and not x.strip()):
        return op == "!=" if y not in (None, "") else op == "="
    a, b = _cmp_pair(x, y)
    if op == "=":
        return a == b
    if op == "!=":
        return a != b
    if type(a) is str and _qnum(y) is not None:      # 文字和数字比大小：不算符合
        return False
    try:
        return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
    except TypeError:
        return False


def _col(header, name, what="列"):
    norm = [_norm(h) for h in header]
    n = _norm(name)
    if n in norm:
        return norm.index(n)
    import difflib
    close = difflib.get_close_matches(n, [x for x in norm if x], n=3, cutoff=0.4)
    close += [h for h in norm if h and (n in h or h in n) and h not in close]
    hint = "，最接近的是：%s" % "、".join(header[norm.index(c)] for c in close[:3]) if close else ""
    raise ToolError("没有%s「%s」%s。全部列名：%s" % (what, name, hint, "、".join(h for h in header if h)))


def _out(v):
    if isinstance(v, Decimal):
        if v == v.to_integral_value():
            return int(v)
        return float(round(v, 6))
    if hasattr(v, "isoformat"):
        s = v.isoformat()
        return s[:10] if s.endswith("T00:00:00") else s
    return v if isinstance(v, (int, float)) or v is None else _show(v)


@tool("excel_query", "不写代码查表里的数：筛选、分组、汇总、排序，程序计算。"
      "where 每条 {col, op, value}，op 为 = != > >= < <= contains in（in 的 value 是列表）；"
      "agg 如 {\"销售额\": \"sum\"}，可用 sum avg count min max；sort 如 {\"col\": \"销售额\", \"desc\": true}。"
      "都不给就列出符合条件的行。结果里有出处，回答时引用",
      {"path": _STR, "sheet": _STR, "where": {"type": "array", "items": {"type": "object"}},
       "group_by": {"type": "array", "items": _STR}, "agg": {"type": "object"}, "sort": {"type": "object"},
       "limit": {"type": "integer"}},
      required=["path"], risk=read_risk("path"), sub_ok=True)
def excel_query(ctx, path, sheet=None, where=None, group_by=None, agg=None, sort=None, limit=50):
    p, wb = _open_wb(ctx, path)
    try:
        ws = _sheet(wb, sheet, path) if sheet else wb.worksheets[0]
        header, rows = _table(ws)
    finally:
        wb.close()
    if not header:
        raise ToolError("「%s」是空的" % ws.title)
    limit = max(1, min(int(limit or 50), 200))
    conds = []
    for c in where or []:
        if not isinstance(c, dict) or "col" not in c:
            raise ToolError('where 的每一条要写成 {"col": 列名, "op": "=", "value": 值}')
        op = str(c.get("op") or "=").strip()
        op = {"==": "=", "<>": "!=", "≠": "!=", "≥": ">=", "≤": "<=", "包含": "contains"}.get(op, op)
        if op not in _OPS:
            raise ToolError("op 只能是：%s" % " ".join(_OPS))
        conds.append((_col(header, c["col"]), op, c.get("value")))
    # 合计、小计行不算数据，免得重复计算
    totals, data = [], []
    for n, r in rows:
        first = next((v for v in r if v is not None and str(v).strip()), None)
        if isinstance(first, str) and _norm(first) in [_norm(w) for w in _TOTAL_WORDS]:
            totals.append(n)
        else:
            data.append((n, r))
    hit = [(n, r) for n, r in data if all(_match(r[i], op, v) for i, op, v in conds)]
    gcols = [_col(header, g) for g in (group_by or [])]
    aggs = []
    for k, fn in (agg or {}).items():
        fn = str(fn).strip().lower()
        fn = {"mean": "avg", "average": "avg", "求和": "sum", "平均": "avg", "计数": "count", "最小": "min", "最大": "max"}.get(fn, fn)
        if fn not in _AGG:
            raise ToolError("agg 的算法只能是：sum avg count min max")
        idx = None if k in ("*", "行数") else _col(header, k)
        aggs.append((k, idx, fn))
    bad = {}
    if gcols or aggs:
        if not aggs:
            aggs = [("*", None, "count")]
        groups, shown = {}, {}
        for n, r in hit:
            key = tuple(_norm(_key(r[i])) for i in gcols)     # 空格、全半角不同算同一组
            groups.setdefault(key, []).append(r)
            shown.setdefault(key, [_key(r[i]) for i in gcols])
        columns = [header[i] for i in gcols] + [("行数" if idx is None else header[idx]) + " " + _AGG[fn] for _, idx, fn in aggs]
        table = []
        for key, rs in groups.items():
            line = list(shown[key])
            for k, idx, fn in aggs:
                if idx is None:
                    line.append(len(rs))
                    continue
                vals = [r[idx] for r in rs if r[idx] is not None and str(r[idx]).strip() != ""]
                if fn == "count":
                    line.append(len(vals))
                    continue
                nums = []
                for v in vals:
                    d = _qdate(v) if fn in ("min", "max") and _qnum(v) is None else None
                    x = d if d is not None else _qnum(v)
                    if x is None:
                        bad[header[idx]] = bad.get(header[idx], 0) + 1
                    else:
                        nums.append(x)
                if not nums:
                    line.append(None)
                elif fn == "sum":
                    line.append(sum(nums, Decimal(0)))
                elif fn == "avg":
                    line.append(sum(nums, Decimal(0)) / len(nums))
                else:
                    try:
                        line.append(min(nums) if fn == "min" else max(nums))
                    except TypeError:
                        line.append(None)
            table.append(line)
        if not gcols and not table:
            table = [[0 if fn == "count" or idx is None else None for _, idx, fn in aggs]]
    else:
        columns = ["行号"] + [h for h in header if h]
        keep = [i for i, h in enumerate(header) if h]
        table = [[n] + [r[i] for i in keep] for n, r in hit]
    if sort:
        if not isinstance(sort, dict) or "col" not in sort:
            raise ToolError('sort 要写成 {"col": 列名, "desc": true}')
        norm_cols = [_norm(c) for c in columns]
        s = _norm(sort["col"])
        if s in norm_cols:
            si = norm_cols.index(s)
        else:
            cand = [i for i, c in enumerate(norm_cols) if c.startswith(s)]
            if not cand:
                raise ToolError("sort 的列「%s」不在结果里。结果的列：%s" % (sort["col"], "、".join(columns)))
            si = cand[0]

        def sk(line):
            v = line[si]
            if v is None or v == "":
                return (2, 0)
            d = _qdate(v) if not isinstance(v, (int, float, Decimal)) else None
            if d is not None:
                return (0, d.timestamp())
            x = _qnum(v)
            return (0, x) if x is not None else (1, _norm(v))
        filled = [l for l in table if sk(l)[0] < 2]
        empty = [l for l in table if sk(l)[0] == 2]
        try:
            filled.sort(key=sk, reverse=bool(sort.get("desc")))
        except TypeError:
            filled.sort(key=lambda l: _norm(l[si]), reverse=bool(sort.get("desc")))
        table = filled + empty
    res = {"source": {"path": _shown(ctx, p, path), "sheet": ws.title}, "rows_total": len(data), "rows_used": len(hit),
           "columns": columns, "rows": [[_out(v) for v in line] for line in table[:limit]]}
    nums = [n for n, _ in hit]
    if nums:
        res["source"]["rows"] = ("第 %s 行" % "、".join(map(str, nums))) if len(nums) <= 20 else \
            "第 %d–%d 行中符合条件的 %d 行" % (nums[0], nums[-1], len(nums))
    if len(table) > limit:
        res["more"] = "共 %d 行，只列了前 %d 行" % (len(table), limit)
    if totals:
        res["excluded_total_rows"] = totals
    if bad:
        res["non_numeric_skipped"] = bad
    if not hit and rows:
        res["note"] = "没有符合条件的行。检查条件的写法，或先用 excel_read 看看这一列的值长什么样"
    else:
        res["note"] = "数字由程序计算。回答时原样引用，并写明出处（文件、工作表）"
    return res
