"""按文档类型切片（docs/两种模式.md 2.3、9.2）：把一个文件变成若干"节"，每节再切成片段。

    parse_file(path) -> {"kind", "meta", "sections": [{"loc", "heading", "text", "tokens", "parts": [...]}], "tokens"}

每个片段：{"loc", "heading", "text", "tokens"}。heading 是标题路径（"第三章 费用报销 > 3.2 审批流程"），
loc 是出处（Word 类是最里面那层标题或"第 N 段"；PDF 是"第 12 页 · 3.2 审批流程"；PPT 是"第 5 页 · 标题"；
Excel 是"工作表「明细」· 第 2–40 行"）。片段的 text 不带标题路径，收录时由调用方把标题路径接在前面再建索引。

- Word、Markdown、文本：按标题和编号分节（样式是标题的；样式是正文但长得像标题的：短、以"第三章""3.2""步骤 4"
  "（一）""一、"之类开头、结尾没有句号）；一节太长再按段落切；相邻片段重叠一句。
- PDF：按页，页内同上；跨页的标题路径接着用。一页都读不出字的是扫描件，抛 ParseError。
- PPT：一页一个片段：标题、要点、表格里的字、备注。
- Excel、CSV：每张表一个概况片段；数据按若干行一组切，每组都带表头。表头里有"问题""答案"两列的是问答对，一行一条。

依赖 python-docx、python-pptx、openpyxl、pypdf，都在用到时才导入。"""
from __future__ import annotations
import csv, datetime, io, os, re, time

TARGET, MAX = 350, 600          # 片段大小：目标约 350 token，上限 600
OVERLAP_MAX = 80                # 重叠的那一句最多多少 token
EXCEL_MAX_ROWS = 20000
HEAD_MAX_CHARS = 40

DOC_EXT = {".docx", ".pdf"}
TEXT_EXT = {".txt", ".md", ".markdown"}
SHEET_EXT = {".xlsx", ".xlsm", ".csv"}
SLIDE_EXT = {".pptx"}
SUPPORTED = DOC_EXT | TEXT_EXT | SHEET_EXT | SLIDE_EXT


class ParseError(Exception):
    """预期内的失败：原因直接记进文档的 error。"""


def tokens(s):
    from .context import estimate_text
    return int(round(estimate_text(s or "")))


def kind_of(path):
    ext = os.path.splitext(path or "")[1].lower()
    if ext in SLIDE_EXT:
        return "slides"
    if ext in SHEET_EXT:
        return "sheet"
    if ext in DOC_EXT:
        return "doc"
    return "text"


# ---------------- 标题 ----------------

_CN = "一二三四五六七八九十百零〇两"
_HEAD = re.compile(
    r"^\s*(?:"
    r"(?P<zhang>第[%s\d]+\s*(?:章|篇|部分|卷))"
    r"|(?P<jie>第[%s\d]+\s*节)"
    r"|(?P<tiao>第[%s\d]+\s*条)"
    r"|(?P<multi>\d{1,2}(?:\.\d{1,3}){1,4})(?=[\s、.．]|[一-鿿A-Za-z])"
    r"|(?P<num>\d{1,2})(?:[.．、](?!\d)|\s+)(?=\S)"
    r"|(?P<step>(?:步骤|Step)\s*[\d%s]+)"
    r"|(?P<cnparen>[（(][%s]+[)）])"
    r"|(?P<numparen>[（(]\d{1,2}[)）])"
    r"|(?P<cn>[%s]+[、.．])"
    r")" % (_CN, _CN, _CN, _CN, _CN, _CN), re.I)
_END_PUNCT = tuple("。.;；!！?？,，")


def heading_family(text):
    """长得像标题时返回它的编号家族（同一家族同一层），否则 None。"""
    t = (text or "").strip()
    if not t or len(t) > HEAD_MAX_CHARS or t.endswith(_END_PUNCT) or "\n" in t:
        return None
    m = _HEAD.match(t)
    if not m:
        return None
    rest = t[m.end():].strip(" \t、.．:：")
    if not rest and not m.group("step"):
        return None
    for k, v in m.groupdict().items():
        if v:
            if k == "multi":
                return "num%d" % (v.count(".") + 1)
            if k == "num":
                return "num1"
            return k
    return None


def style_rank(style_name):
    """Word 样式是标题的：返回层级（标题/Title 算 1），否则 None。"""
    n = (style_name or "").strip()
    m = re.match(r"^(?:heading|标题)\s*(\d)", n, re.I)
    if m:
        return max(1, int(m.group(1)))
    if n.lower() in ("title", "标题"):
        return 1
    return None


class Outline:
    """标题栈：样式标题按层级；编号标题的层级在第一次出现时定下（比当时栈顶深一层），之后同一家族都在这一层。"""

    def __init__(self):
        self.stack = []          # [(rank, text)]
        self.ranks = {}

    def push(self, text, rank=None, family=None):
        if rank is None:
            if family in self.ranks:
                rank = self.ranks[family]
            else:
                rank = (self.stack[-1][0] + 1) if self.stack else 1
                if family and family.startswith("num") and family[3:].isdigit():
                    d = int(family[3:])
                    shallower = [self.ranks[f] for f in self.ranks if f.startswith("num") and f[3:].isdigit() and int(f[3:]) < d]
                    if shallower:
                        rank = max(rank if not self.stack else 0, max(shallower) + 1)
                self.ranks[family] = rank
        while self.stack and self.stack[-1][0] >= rank:
            self.stack.pop()
        self.stack.append((rank, text.strip()))

    def path(self):
        return " > ".join(t for _, t in self.stack)

    def last(self):
        return self.stack[-1][1] if self.stack else ""


# ---------------- 切段 ----------------

_SENT = re.compile(r"(?<=[。！？!?；;])|(?<=\.)\s+|\n")


def _sentences(text):
    out = [s for s in _SENT.split(text) if s and s.strip()]
    return out or [text]


def _hard_split(text, limit):
    """一句话也超过上限：按字数硬切。"""
    out, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) % 50 == 0 and tokens(cur) >= limit:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def _pieces(paras, target=TARGET, limit=MAX, overlap=True):
    """一节的段落 → 片段文字列表。段落太长按句切；相邻片段重叠一句（不超过 OVERLAP_MAX）。"""
    units = []
    for p in paras:
        if tokens(p) <= limit:
            units.append(p)
            continue
        for s in _sentences(p):
            units += [s] if tokens(s) <= limit else _hard_split(s, limit)
    out, cur, cur_t = [], [], 0
    for u in units:
        t = tokens(u)
        if cur and cur_t + t > target and (cur_t + t > limit or cur_t >= target * 0.6):
            out.append(cur)
            tail = []
            if overlap:
                last = _sentences(cur[-1])[-1].strip()
                if last and tokens(last) <= OVERLAP_MAX and tokens(last) + t <= limit:
                    tail = [last]
            cur, cur_t = tail, sum(tokens(x) for x in tail)
        cur.append(u)
        cur_t += t
    if cur:
        out.append(cur)
    return ["\n".join(x).strip() for x in out if "\n".join(x).strip()]


class _Builder:
    """Word 类文档：按块（段落、表格行、标题）攒成节。"""

    def __init__(self, page_prefix=None):
        self.outline = Outline()
        self.sections = []
        self.cur = None
        self.para_no = 0
        self.page = None
        self.page_prefix = page_prefix

    def _new(self):
        head = self.outline.path()
        last = self.outline.last()
        self.cur = {"heading": head, "last": last, "paras": [], "page": self.page, "first_para": self.para_no + 1}
        self.sections.append(self.cur)

    def heading(self, text, rank=None, family=None):
        self.outline.push(text, rank, family)
        self._new()

    def set_page(self, page):
        self.page = page
        self._new()

    def para(self, text):
        text = (text or "").strip()
        if not text:
            return
        if self.cur is None:
            self._new()
        self.para_no += 1
        self.cur["paras"].append(text)

    def block(self, text):
        """一段普通文字：长得像标题就当标题。"""
        t = (text or "").strip()
        if not t:
            return
        fam = heading_family(t)
        if fam:
            self.heading(t, family=fam)
        else:
            self.para(t)

    def build(self):
        out = []
        for s in self.sections:
            if not s["paras"]:
                continue
            loc = s["last"] or ("第 %d 段" % s["first_para"])
            if s["page"] is not None:
                loc = "第 %d 页" % s["page"] + (" · " + s["last"] if s["last"] else "")
            body = "\n".join(s["paras"])
            parts = [{"loc": loc, "heading": s["heading"], "text": t, "tokens": tokens(t)} for t in _pieces(s["paras"])]
            out.append({"loc": loc, "heading": s["heading"], "text": body, "tokens": tokens(body), "parts": parts})
        return out


# ---------------- 各类型 ----------------

def _decode(raw):
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _read_text(path):
    with open(path, "rb") as f:
        return _decode(f.read())


def parse_text(text, markdown=False):
    b = _Builder()
    para = []

    def flush():
        if para:
            b.para(" ".join(x.strip() for x in para) if not markdown else "\n".join(para))
            para[:] = []

    for line in text.splitlines():
        s = line.strip()
        if not s:
            flush()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush()
            b.heading(m.group(2).strip(), rank=len(m.group(1)))
            continue
        fam = heading_family(s)
        if fam:
            flush()
            b.heading(s, family=fam)
            continue
        para.append(s)
        if s.endswith(("。", "！", "？", "!", "?")) and not markdown:
            flush()
    flush()
    return b.build()


def _iter_docx_blocks(d):
    """按正文里的顺序给出段落和表格。"""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield Paragraph(child, d)
        elif tag == "tbl":
            yield Table(child, d)


def _outline_level(p):
    try:
        ppr = p._p.pPr
        if ppr is not None:
            for el in ppr.iterchildren():
                if el.tag.endswith("}outlineLvl"):
                    v = int(el.get(el.tag.split("}")[0] + "}val"))
                    return v + 1 if v < 9 else None
    except Exception:
        return None
    return None


def parse_docx(path):
    import docx
    d = docx.Document(path)
    b = _Builder()
    chars = 0
    for blk in _iter_docx_blocks(d):
        if blk.__class__.__name__ == "Table":
            for r in blk.rows:
                cells = []
                for c in r.cells:
                    t = c.text.strip()
                    if not cells or cells[-1] != t:      # 合并单元格会重复出现
                        cells.append(t)
                line = " | ".join(cells)
                chars += len(line)
                b.para(line)
            continue
        text = blk.text.strip()
        if not text:
            continue
        chars += len(text)
        rank = None
        try:
            rank = style_rank(blk.style.name if blk.style is not None else "")
        except Exception:
            rank = None
        if rank is None:
            rank = _outline_level(blk)
        if rank is not None and len(text) <= 80:
            b.heading(text, rank=rank)
        else:
            b.block(text)
    return b.build(), {"chars": chars}


def parse_pdf(path):
    import pypdf
    rd = pypdf.PdfReader(path)
    b = _Builder()
    total = 0
    for i, page in enumerate(rd.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        total += len(re.sub(r"\s", "", text))
        b.set_page(i + 1)
        para = []

        def flush():
            if para:
                b.para("".join(para) if all(re.search(r"[一-鿿]$", x) for x in para[:-1]) else " ".join(para))
                para[:] = []

        for line in text.splitlines():
            s = line.strip()
            if not s:
                flush()
                continue
            fam = heading_family(s)
            if fam:
                flush()
                b.outline.push(s, family=fam)
                b._new()
                continue
            para.append(s)
            if s.endswith(("。", "！", "？", "；", "!", "?")):
                flush()
        flush()
    if len(rd.pages) and total < 10:
        raise ParseError("这份 PDF 是扫描件，读不出文字")
    return b.build(), {"pages": len(rd.pages)}


def _shape_texts(shapes, title_shape):
    """一页里除标题以外的文字：要点（带层级）、表格、组合里的形状。"""
    lines, tables = [], []
    for sh in shapes:
        if title_shape is not None and sh.shape_id == title_shape.shape_id:
            continue
        if getattr(sh, "shape_type", None) == 6 or sh.__class__.__name__ == "GroupShape":     # 组合
            l2, t2 = _shape_texts(sh.shapes, None)
            lines += l2
            tables += t2
            continue
        if getattr(sh, "has_table", False) and sh.has_table:
            rows = []
            for r in sh.table.rows:
                rows.append(" | ".join(c.text.strip().replace("\n", " ") for c in r.cells))
            tables.append("\n".join(rows))
            continue
        if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
            for p in sh.text_frame.paragraphs:
                t = (p.text or "").strip()
                if t:
                    lines.append("  " * min(int(p.level or 0), 4) + "- " + t)
    return lines, tables


def parse_pptx(path):
    import pptx
    prs = pptx.Presentation(path)
    out = []
    for i, sl in enumerate(prs.slides):
        title_shape = sl.shapes.title if sl.shapes.title is not None else None
        title = title_shape.text_frame.text.strip().replace("\n", " ") if title_shape is not None and title_shape.has_text_frame else ""
        lines, tables = _shape_texts(sl.shapes, title_shape)
        if not title and lines:
            title = lines.pop(0).lstrip(" -")
        notes = ""
        try:
            if sl.has_notes_slide:
                notes = sl.notes_slide.notes_text_frame.text.strip()
        except Exception:
            notes = ""
        body = []
        if title:
            body.append(title)
        body += lines
        for t in tables:
            body.append("表格：\n" + t)
        if notes:
            body.append("备注：" + notes)
        text = "\n".join(body).strip()
        if not text:
            continue
        loc = "第 %d 页" % (i + 1) + (" · " + title[:40] if title else "")
        parts = _pieces(body, target=MAX, limit=MAX, overlap=False) if tokens(text) > MAX else [text]
        out.append({"loc": loc, "heading": title, "text": text, "tokens": tokens(text),
                    "parts": [{"loc": loc, "heading": title, "text": t, "tokens": tokens(t)} for t in parts]})
    return out, {"slides": len(prs.slides)}


QA_Q = ("问题", "question", "q", "问", "提问")
QA_A = ("答案", "answer", "a", "答", "回答", "解答")


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else ("%.10g" % v)
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%d") if (v.hour, v.minute, v.second) == (0, 0, 0) else v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, datetime.date):
        return v.strftime("%Y-%m-%d")
    return str(v).strip().replace("\n", " ")


def qa_columns(header):
    """表头里有"问题""答案"两列（或 question / answer、Q / A）：返回 (问题列, 答案列)，否则 None。"""
    norm = [str(h or "").strip().lower() for h in header]
    qi = next((i for i, h in enumerate(norm) if h in QA_Q), None)
    ai = next((i for i, h in enumerate(norm) if h in QA_A), None)
    if qi is None or ai is None or qi == ai:
        return None
    return qi, ai


def _table_sections(name, rows, label):
    """一张表（[(行号, [单元格])]，第一项是表头）→ 节。label：出处的前缀（"工作表「明细」"或空）。
    返回 (节列表, 是不是问答对)。"""
    if not rows:
        return [], False
    hdr_no, header = rows[0]
    data = rows[1:]
    pre = _dot(label) if label else ""
    qa = qa_columns(header)
    out = []
    if qa:
        qi, ai = qa
        for no, r in data:
            q = r[qi] if qi < len(r) else ""
            a = r[ai] if ai < len(r) else ""
            if not q and not a:
                continue
            extra = ["%s：%s" % (header[j], r[j]) for j in range(len(r)) if j not in (qi, ai) and j < len(header)
                     and header[j] and r[j]]
            text = "问：%s\n答：%s" % (q, a) + ("\n" + "；".join(extra) if extra else "")
            loc = pre + "第 %d 行" % no
            out.append({"loc": loc, "heading": q[:60], "text": text, "tokens": tokens(text),
                        "parts": [{"loc": loc, "heading": q[:60], "text": text, "tokens": tokens(text)}]})
        return out, True
    head_line = " | ".join(header)
    cols = "、".join(h for h in header if h)
    sample = [" | ".join(r) for _, r in data[:3]]
    ov = "%s：共 %d 行数据、%d 列\n列：%s" % (("工作表「%s」" % name) if label else name, len(data), len(header), cols)
    if sample:
        ov += "\n前几行：\n" + head_line + "\n" + "\n".join(sample)
    ov_loc = _dot(label or name) + "概况"
    out.append({"loc": ov_loc, "heading": label or name, "text": ov, "tokens": tokens(ov),
                "parts": [{"loc": ov_loc, "heading": label or name, "text": ov, "tokens": tokens(ov)}]})
    ht = tokens(head_line)
    group, gt, first = [], ht, None
    for no, r in data:
        line = " | ".join(r)
        t = tokens(line) + 0.5
        if group and (gt + t > TARGET):
            out.append(_group(head_line, group, first, last_no, pre))
            group, gt = [], ht
        if not group:
            first = no
        group.append(line)
        gt += t
        last_no = no
    if group:
        out.append(_group(head_line, group, first, last_no, pre))
    return out, False


def _dot(label):
    """出处里的分隔："工作表「明细」· 第 2–40 行"（」后面不加空格），其余 "第 5 页 · 标题"。"""
    return label + ("· " if label.endswith("」") else " · ")


def _group(head_line, lines, first, last, pre):
    text = "表头：" + head_line + "\n" + "\n".join(lines)
    if tokens(text) > MAX:
        text = text[:int(len(text) * MAX / max(1, tokens(text)))]
    loc = pre + ("第 %d–%d 行" % (first, last) if last != first else "第 %d 行" % first)
    return {"loc": loc, "heading": "", "text": text, "tokens": tokens(text),
            "parts": [{"loc": loc, "heading": "", "text": text, "tokens": tokens(text)}]}


def _trim_rows(rows):
    """去掉全空的行，单元格转成文字，去掉右边全空的列。返回 [(行号, [单元格])]。"""
    out = []
    width = 0
    for no, r in rows:
        cells = [_cell(v) for v in r]
        if not any(cells):
            continue
        while cells and not cells[-1]:
            cells.pop()
        width = max(width, len(cells))
        out.append((no, cells))
    return [(no, c + [""] * (width - len(c))) for no, c in out]


def parse_xlsx(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out, total, qa_sheets, sheets = [], 0, 0, 0
    try:
        for ws in wb.worksheets:
            rows = []
            for n, r in enumerate(ws.iter_rows(values_only=True), 1):
                if n > EXCEL_MAX_ROWS:
                    break
                rows.append((n, r))
            rows = _trim_rows(rows)
            if not rows:
                continue
            sheets += 1
            total += max(0, len(rows) - 1)
            secs, qa = _table_sections(ws.title, rows, "工作表「%s」" % ws.title)
            qa_sheets += 1 if qa else 0
            out += secs
    finally:
        wb.close()
    return out, {"sheets": sheets, "rows": total, "qa": bool(sheets) and qa_sheets == sheets}


def parse_csv(path):
    text = _read_text(path)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    rows = [(i, r) for i, r in enumerate(csv.reader(io.StringIO(text), dialect), 1) if i <= EXCEL_MAX_ROWS]
    rows = _trim_rows(rows)
    name = os.path.splitext(os.path.basename(path))[0]
    secs, qa = _table_sections(name, rows, "")
    return secs, {"sheets": 1, "rows": max(0, len(rows) - 1), "qa": qa}


def parse_file(path):
    """一个文件 → {"kind", "meta", "sections", "tokens"}。读不出来抛 ParseError 或底层库的异常。"""
    ext = os.path.splitext(path)[1].lower()
    meta = {}
    if ext == ".docx":
        secs, meta = parse_docx(path)
    elif ext == ".pdf":
        secs, meta = parse_pdf(path)
    elif ext == ".pptx":
        secs, meta = parse_pptx(path)
    elif ext in (".xlsx", ".xlsm"):
        secs, meta = parse_xlsx(path)
    elif ext == ".csv":
        secs, meta = parse_csv(path)
    elif ext in TEXT_EXT:
        text = _read_text(path)
        secs = parse_text(text, markdown=ext in (".md", ".markdown"))
        meta = {"chars": len(text)}
    else:
        raise ParseError("暂不支持 %s 类型的文件" % (ext or "这种"))
    kind = kind_of(path)
    if meta.pop("qa", False):
        kind = "qa"
    return {"kind": kind, "meta": meta, "sections": secs, "tokens": sum(s["tokens"] for s in secs)}


# ---------------- 新旧版本（9.2） ----------------

_V_YEAR = re.compile(r"(20\d{2})\s*年?\s*(?:修订版|修订|版本|版)")
_V_NUM = re.compile(r"(?<![A-Za-z])[Vv]\s?(\d+(?:\.\d+)*)(?![\d.])")
_V_NTH = re.compile(r"第\s*([%s\d]+)\s*版" % _CN)
_V_DATE = re.compile(r"(?<!\d)(20\d{2})\s*[-_.年/]\s*(\d{1,2})(?:\s*[-_.月/]\s*(\d{1,2})\s*日?)?(?!\d)")
_V_COMPACT = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
_V_YEAR_ONLY = re.compile(r"(?<!\d)(20\d{2})(?!\d)\s*年?")
_FAMILY_JUNK = re.compile(r"[（(]\s*[)）]|[\[【]\s*[\]】]")
_CN_NUM = {c: i for i, c in enumerate("零一二三四五六七八九十")}


def _date_ts(y, m=1, d=1):
    try:
        return time.mktime(datetime.date(int(y), int(m or 1), int(d or 1)).timetuple())
    except (ValueError, OverflowError):
        return None


def version_info(name, mtime=None):
    """文件名 → {"family", "version_label", "effective", "vnum"}。
    family：去掉版本和日期后的标题（比较时去空白、转小写）；version_label："2026 版""V3""2025-06"；
    effective：标题里的日期（只有年份按 1 月 1 日），没有就用修改时间；vnum：V3、第 2 版这类的数字（比较新旧时优先用）。"""
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    label, eff, vnum, rest = "", None, None, stem
    m = _V_YEAR.search(stem)
    if m:
        label, eff = "%s 版" % m.group(1), _date_ts(m.group(1))
        rest = stem[:m.start()] + stem[m.end():]
    else:
        m = _V_NUM.search(stem)
        if m:
            label = "V" + m.group(1)
            vnum = tuple(int(x) for x in m.group(1).split("."))
            rest = stem[:m.start()] + stem[m.end():]
        else:
            m = _V_NTH.search(stem)
            if m:
                v = m.group(1)
                n = int(v) if v.isdigit() else _CN_NUM.get(v, 0)
                label, vnum = "第 %s 版" % v, (n,)
                rest = stem[:m.start()] + stem[m.end():]
    dm = _V_DATE.search(rest) or None
    if dm:
        y, mo, d = dm.group(1), dm.group(2), dm.group(3)
        eff = _date_ts(y, mo, d) or eff
        if not label:
            label = "%s-%02d" % (y, int(mo)) + ("-%02d" % int(d) if d else "")
        rest = rest[:dm.start()] + rest[dm.end():]
    else:
        cm = _V_COMPACT.search(rest)
        if cm:
            eff = _date_ts(cm.group(1), cm.group(2), cm.group(3)) or eff
            if not label:
                label = "%s-%s-%s" % cm.groups()
            rest = rest[:cm.start()] + rest[cm.end():]
        elif not label:
            ym = _V_YEAR_ONLY.search(rest)
            if ym:
                label, eff = ym.group(1), _date_ts(ym.group(1))
                rest = rest[:ym.start()] + rest[ym.end():]
    fam = _FAMILY_JUNK.sub("", rest)
    fam = re.sub(r"(?:最新|新版|旧版|修订稿|草案|定稿|终稿)$", "", fam.strip(" -_.·、，,（(）)[]【】"))
    fam = fam.strip(" -_.·、，,（(）)[]【】")
    if eff is None:
        eff = float(mtime) if mtime else None
    return {"family": fam or stem, "version_label": label, "effective": eff, "vnum": vnum}


def family_key(family):
    return re.sub(r"\s+", "", (family or "")).lower()


def newer(a, b):
    """a 比 b 新吗？a、b 是带 version_label、effective 的 dict。两份都有版本号（V3、第 2 版）时比版本号，否则比日期。"""
    va, vb = _vnum(a.get("version_label")), _vnum(b.get("version_label"))
    if va is not None and vb is not None and va != vb:
        return va > vb
    return (a.get("effective") or 0) > (b.get("effective") or 0)


def _vnum(label):
    m = re.match(r"^V(\d+(?:\.\d+)*)$", label or "")
    if m:
        return tuple(int(x) for x in m.group(1).split("."))
    m = re.match(r"^第 (\S+) 版$", label or "")
    if m:
        v = m.group(1)
        return (int(v) if v.isdigit() else _CN_NUM.get(v, 0),)
    return None
