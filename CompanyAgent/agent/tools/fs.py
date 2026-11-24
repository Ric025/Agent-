"""文件工具：写只在工作区里；读也可以读工作区以外（批准级别 read_outside，见 agent/kb.resolve_read）。"""
from __future__ import annotations
import os, shutil, hashlib, fnmatch, time
from . import tool, S, I, ToolError
from .. import policy
from ..store import new_id, now

TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".xml", ".yaml", ".yml", ".ini", ".html", ".htm", ".py", ".js", ".sql", ".sh", ".bat"}


def _decode(raw):
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def extract(path):
    """把文件拆成 [(出处, 文字)]。出处是页码、工作表名等，引用时用。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXT or ext == "":
        with open(path, "rb") as f:
            return [("", _decode(f.read()))]
    if ext == ".docx":
        import docx
        d = docx.Document(path)
        parts = [p.text for p in d.paragraphs if p.text.strip()]
        for t in d.tables:
            for r in t.rows:
                parts.append(" | ".join(c.text.strip() for c in r.cells))
        return [("", "\n".join(parts))]
    if ext == ".pdf":
        import pypdf
        rd = pypdf.PdfReader(path)
        return [("第%d页" % (i + 1), (p.extract_text() or "")) for i, p in enumerate(rd.pages)]
    if ext in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            lines = []
            for n, row in enumerate(ws.iter_rows(values_only=True)):
                if n >= 2000:
                    lines.append("……（只读取了前 2000 行）")
                    break
                if any(c is not None for c in row):
                    lines.append("\t".join("" if c is None else str(c) for c in row))
            out.append(("工作表 %s" % ws.title, "\n".join(lines)))
        wb.close()
        return out
    if ext == ".pptx":
        import pptx
        prs = pptx.Presentation(path)
        out = []
        for i, sl in enumerate(prs.slides):
            texts = [sh.text_frame.text for sh in sl.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
            out.append(("第%d张" % (i + 1), "\n".join(texts)))
        return out
    raise ToolError("暂不支持读取 %s 类型的文件" % (ext or "这种"))


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 16), b""):
            h.update(b)
    return h.hexdigest()[:16]


def publish(ctx, path, note=""):
    """登记一个成品版本，并通知界面。"""
    rel = policy.relpath(ctx.root, path)
    prev = ctx.store.one("SELECT MAX(version) AS v FROM artifacts WHERE workspace_id=? AND path=?", (ctx.ws["id"], rel))
    ver = (prev["v"] or 0) + 1
    aid = new_id("f_")
    ctx.store.x("INSERT INTO artifacts(id,session_id,workspace_id,path,version,hash,size,note,created) VALUES(?,?,?,?,?,?,?,?,?)",
                (aid, ctx.sid, ctx.ws["id"], rel, ver, file_hash(path), os.path.getsize(path), note, now()))
    ctx.emit("artifact", {"id": aid, "path": rel, "version": ver, "note": note})
    return ver


def backup(ctx, path):
    """覆盖或删除之前，把旧文件留一份在 .agent/versions/ 里。"""
    if not os.path.isfile(path):
        return None
    rel = policy.relpath(ctx.root, path)
    dst = os.path.join(ctx.root, policy.INTERNAL_DIR, "versions", "%s.%s" % (rel.replace("/", "__"), time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(path, dst)
    return policy.relpath(ctx.root, dst)


def write_risk(ctx, args):
    try:
        return "overwrite" if os.path.exists(policy.resolve(ctx.root, args.get("path", ""), write=True)) else "create"
    except policy.PathError:
        return "create"


def read_risk(*keys, base="read"):
    """读文件工具的批准级别（agent.kb.read_risk）：路径在当前文件夹以外时是 read_outside。这里包一层，免得导入时互相引用。"""
    def risk(ctx, args):
        try:
            from ..kb import read_risk as rr
        except ImportError:           # 没有资料库模块时（老测试会这样模拟）：只认当前文件夹
            return base(ctx, args) if callable(base) else base
        return rr(*keys, base=base)(ctx, args)
    risk.shown = base if isinstance(base, str) else "overwrite"     # 设置页的工具表里显示哪一级
    return risk


def _read_path(ctx, path):
    """读文件的路径：当前文件夹里的，或资料库里的（资料库名/相对路径，docs/两种模式.md 第 8 节）。"""
    from ..kb import resolve_read
    return resolve_read(ctx, path)


def _shown(ctx, p):
    """结果里显示的路径：当前文件夹里的写相对路径，资料库里的写"资料库名/相对路径"。"""
    rel = policy.relpath(ctx.root, p)
    if rel == ".." or rel.startswith("../"):
        from ..kb import display_path
        return display_path(ctx.store, p, ctx.root)
    return rel


@tool("list_dir", "列出某个目录下的文件和子目录", {"path": S('目录路径，工作区根目录用 "."')},
      risk=read_risk("path"), sub_ok=True)
def list_dir(ctx, path="."):
    p = _read_path(ctx, path)
    if not os.path.isdir(p):
        raise ToolError("不是目录：%s" % path)
    items = []
    for name in sorted(os.listdir(p)):
        if name == policy.INTERNAL_DIR or name.startswith("~$"):
            continue
        fp = os.path.join(p, name)
        st = os.stat(fp)
        items.append({"name": name + ("/" if os.path.isdir(fp) else ""), "size": st.st_size if os.path.isfile(fp) else None,
                      "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return {"path": _shown(ctx, p), "count": len(items), "items": items[:300], "more": max(0, len(items) - 300)}


@tool("read_file", "读取文件内容，支持 txt/md/csv/docx/pdf/xlsx/pptx（转成文字）。大文件用 offset 分段读",
      {"path": S("文件路径"), "offset": I("从第几个字符开始，默认 0"), "max_chars": I("最多返回多少字符，默认 5000")},
      required=["path"], risk=read_risk("path"), sub_ok=True)
def read_file(ctx, path, offset=0, max_chars=5000):
    p = _read_path(ctx, path)
    if not os.path.isfile(p):
        raise ToolError("找不到文件：%s" % path)
    segs = extract(p)
    text = "\n\n".join(("【%s】\n%s" % (loc, t)) if loc else t for loc, t in segs)
    max_chars = max(200, min(int(max_chars or 5000), 20000))
    part = text[offset: offset + max_chars]
    return {"path": _shown(ctx, p), "total_chars": len(text), "offset": offset,
            "truncated": offset + max_chars < len(text), "text": part}


@tool("write_file", "新建或覆盖一个文本文件（txt/md/csv/json 等）。覆盖已有文件前系统会留旧版本",
      {"path": S("文件路径"), "content": S("完整内容")}, risk=write_risk)
def write_file(ctx, path, content):
    p = policy.resolve(ctx.root, path, write=True)
    old = backup(ctx, p)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    ver = publish(ctx, p, "覆盖" if old else "新建")
    return {"path": policy.relpath(ctx.root, p), "chars": len(content), "version": ver, "previous_saved_as": old}


@tool("edit_file", "把文本文件中的一段文字精确替换成另一段。old 必须和原文完全一致且只出现一次",
      {"path": S("文件路径"), "old": S("原文"), "new": S("替换后的文字")}, risk="overwrite")
def edit_file(ctx, path, old, new):
    p = policy.resolve(ctx.root, path, write=True)
    if not os.path.isfile(p):
        raise ToolError("找不到文件：%s" % path)
    if os.path.splitext(p)[1].lower() not in TEXT_EXT:
        raise ToolError("edit_file 只能改文本文件；Office 文件请重新生成")
    with open(p, "rb") as f:
        src = _decode(f.read())
    n = src.count(old)
    if n != 1:
        raise ToolError("原文在文件中出现了 %d 次，需要恰好 1 次。请先 read_file 确认原文" % n)
    saved = backup(ctx, p)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(src.replace(old, new))
    ver = publish(ctx, p, "修改")
    return {"path": policy.relpath(ctx.root, p), "version": ver, "previous_saved_as": saved}


SEARCH_MAX_FILES = 50000     # 在工作区以外找时（可能是整个主目录），最多看这么多个文件名
SEARCH_MAX_SECONDS = 20


@tool("search_files", "按文件名或文本内容搜索文件", {"query": S("关键词"), "glob": S("可选的文件名通配，如 *.xlsx"),
                                                   "path": S("可选：在哪个目录里找，也可以是 资料库名/目录")},
      required=["query"], risk=read_risk("path"), sub_ok=True)
def search_files(ctx, query, glob=None, path=None):
    root = os.path.realpath(_read_path(ctx, path) if path else ctx.root)
    if not os.path.isdir(root):
        raise ToolError("不是目录：%s" % path)
    shown = (lambda fp: _shown(ctx, fp)) if path else (lambda fp: policy.relpath(root, fp))
    names, hits, scanned = [], [], 0
    q = query.lower()
    outside = policy.relpath(ctx.root, root).startswith("..")
    from ..kb import _own_dirs
    own = _own_dirs(ctx)
    seen, t0, stopped = 0, time.time(), False
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != policy.INTERNAL_DIR and not x.startswith(".")
                   and not (outside and policy.is_secret(os.path.join(d, x), own))]
        if outside:
            seen += len(files)
            if seen > SEARCH_MAX_FILES or time.time() - t0 > SEARCH_MAX_SECONDS:
                stopped = True
                break
        for fn in files:
            if outside and fn.lower() in policy.SECRET_NAMES:
                continue
            if glob and not fnmatch.fnmatch(fn, glob):
                continue
            fp = os.path.join(d, fn)
            rel = shown(fp)
            if q in fn.lower():
                names.append(rel)
            if os.path.splitext(fn)[1].lower() in TEXT_EXT and os.path.getsize(fp) < 2_000_000 and scanned < 2000:
                scanned += 1
                try:
                    with open(fp, "rb") as f:
                        txt = _decode(f.read())
                except OSError:
                    continue
                for ln, line in enumerate(txt.splitlines(), 1):
                    if q in line.lower():
                        hits.append({"file": rel, "line": ln, "text": line.strip()[:160]})
                        if len(hits) >= 40:
                            break
    out = {"name_matches": names[:40], "content_matches": hits[:40]}
    if stopped:
        out["note"] = "目录太大，只找了一部分（%d 秒、约 %d 个文件）。可以换一个更小的目录再找。" % (SEARCH_MAX_SECONDS, SEARCH_MAX_FILES)
    return out


@tool("move_file", "移动或重命名工作区内的文件。目标已存在时会失败",
      {"src": S("原路径"), "dst": S("新路径")}, risk="overwrite")
def move_file(ctx, src, dst):
    a = policy.resolve(ctx.root, src, write=True)
    b = policy.resolve(ctx.root, dst, write=True)
    if not os.path.exists(a):
        raise ToolError("找不到：%s" % src)
    if os.path.exists(b):
        raise ToolError("目标已存在：%s" % dst)
    os.makedirs(os.path.dirname(b), exist_ok=True)
    shutil.move(a, b)
    return {"moved": policy.relpath(ctx.root, a), "to": policy.relpath(ctx.root, b)}


@tool("delete_file", "删除工作区内的一个文件（会先移入回收目录，仍然必须得到用户同意）", {"path": S("文件路径")}, risk="delete")
def delete_file(ctx, path):
    p = policy.resolve(ctx.root, path, write=True)
    if not os.path.isfile(p):
        raise ToolError("找不到文件：%s" % path)
    rel = policy.relpath(ctx.root, p)
    dst = os.path.join(ctx.root, policy.INTERNAL_DIR, "trash", "%s.%s" % (rel.replace("/", "__"), time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(p, dst)
    return {"deleted": rel, "recoverable_from": policy.relpath(ctx.root, dst)}
