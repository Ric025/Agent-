"""独立的资料库（docs/两种模式.md 第 2、3、9.2 节）。

- 资料库跟对话无关，可以有好几个；一个资料库由若干来源（文件夹或文件，可以是共享盘）组成。
- 资料库里的文件对程序是只读的：任何写工具指向资料库里的路径都拒绝（write_violation）；读的时候统一走 resolve_read。
- 收录在后台线程里做（ingest.Indexer），检索是关键词加向量的混合检索（retrieve），向量放在 vectors 里，
  向量模型和重排模型的客户端在 embed 里。
- 编号和片段的对应只存在账本里（kb_search / kb_open 的 tool_result 的 hits），见 cite_map、cited_parts。

这个文件只用标准库；numpy、LangChain 都在用到时才导入。"""
from __future__ import annotations
import json, os, threading, time

from ..store import new_id, now
from ..tools import ToolError
from .. import policy

ANSWERS_ID = "lib_answers"          # 9.5 的"标准答案"库：固定 id，第一次存标准答案时建（agent/feedback.py）
ANSWERS_NAME = "标准答案"
NAME_MAX = 40
BUILTIN_MSG = "「标准答案」是内置的资料库：不能改名、不能删、不能加来源。要改里面的内容，到「标准答案」一栏里改、删每一条。"


def check_not_builtin(lid):
    if lid == ANSWERS_ID:
        raise ValueError(BUILTIN_MSG)


# ---- 公司共享的库（docs/借鉴开源项目.md 5.5）：从共享目录导入，只读，可以停用 ----
SHARED_MSG = "「%s」是公司共享的资料库（从共享目录同步来的）：不能改名、不能加来源、不能重新收录、不能删，可以停用。"


def is_shared(store, lid):
    r = store.one("SELECT shared FROM kb_libraries WHERE id=?", (lid,)) if lid else None
    return bool(r and r.get("shared"))


def check_not_shared(store, lid):
    r = store.one("SELECT name, shared FROM kb_libraries WHERE id=?", (lid,)) if lid else None
    if r and r.get("shared"):
        raise ValueError(SHARED_MSG % r.get("name"))


def enabled_ids(store):
    """没有停用的资料库（检索默认的范围）。"""
    return [r["id"] for r in store.q("SELECT id FROM kb_libraries WHERE COALESCE(enabled,1)<>0 ORDER BY created")]


class ReadPathError(policy.PathError, ToolError):
    """resolve_read 解析不了的路径：既是 PathError（老测试、老调用方按它处理）也是 ToolError（规格第 8 节的接缝）。"""


# ---------------- 服务（运行时上的一份） ----------------

class KBService:
    """运行时上的资料库服务：后台收录线程、向量库、向量模型在不在线。"""

    def __init__(self, rt):
        self.rt = rt
        self.lock = threading.RLock()
        self.embed_alive = None          # None 未知 · True · False（最近一次调用向量模型的结果）
        self.embed_error = ""
        self._vectors = None
        self._vec_key = None
        from .ingest import Indexer
        self.indexer = Indexer(self)
        from .cards import Writer
        self.cards = Writer(self)          # 文档卡片的摘要（docs/借鉴开源项目.md 第 1 节）：后台写

    @property
    def store(self):
        return self.rt.store

    @property
    def cfg(self):
        return self.rt.cfg

    def start(self):
        """启动时：有资料库就起后台线程（接着做没做完的收录，并且同步一遍）。"""
        self.indexer.start_if_needed()
        self.cards.kick()

    def close(self):
        self.cards.stop()
        self.indexer.stop()

    # ---- 向量 ----
    def vectors(self):
        """当前这套向量库（langchain 或 builtin）。配置变了换一份。"""
        from . import vectors as vmod
        key = (vmod.engine_name(self.cfg),)
        with self.lock:
            if self._vectors is None or self._vec_key != key:
                if self._vectors is not None:
                    try:
                        self._vectors.close()
                    except Exception:
                        pass
                self._vectors = vmod.make(self, key[0])
                self._vec_key = key
            return self._vectors

    def semantic(self):
        """语义检索现在能不能用：(ok, unknown)。"""
        from .. import capability
        f = capability.semantic_feature(self.rt)
        return f["ok"], f["unknown"]

    def mark_embed(self, ok, error=""):
        self.embed_alive = bool(ok)
        self.embed_error = "" if ok else (error or "")[:300]


def get(rt):
    s = getattr(rt, "kb", None)
    if s is None:
        s = KBService(rt)
        try:
            rt.kb = s
        except Exception:
            pass
    return s


# ---------------- 资料库和来源 ----------------

def clean_name(name):
    if not isinstance(name, str):
        raise ValueError("资料库的名字应该是文字")
    n = " ".join(name.strip().split())
    if not n:
        raise ValueError("资料库的名字不能为空")
    if "/" in n or "\\" in n:
        raise ValueError("资料库的名字里不能有斜杠")
    if len(n) > NAME_MAX:
        raise ValueError("资料库的名字最多 %d 个字" % NAME_MAX)
    return n


def libraries(store):
    return store.q("SELECT * FROM kb_libraries ORDER BY created")


def library(store, lid):
    return store.one("SELECT * FROM kb_libraries WHERE id=?", (lid,))


def library_by_name(store, name):
    return store.one("SELECT * FROM kb_libraries WHERE name=?", (name,))


def create_library(store, name, note="", workspace_id=None, lid=None):
    name = clean_name(name)
    if library_by_name(store, name):
        raise ValueError("已经有一个叫「%s」的资料库了" % name)
    lid = lid or new_id("L_")
    t = now()
    store.x("INSERT INTO kb_libraries(id,name,note,created,updated,workspace_id) VALUES(?,?,?,?,?,?)",
            (lid, name, str(note or ""), t, t, workspace_id))
    return library(store, lid)


def update_library(store, lid, name=None, note=None):
    check_not_builtin(lid)
    if name is not None or note is not None:
        check_not_shared(store, lid)
    lib = library(store, lid)
    if not lib:
        raise KeyError("没有这个资料库")
    fields = {}
    if name is not None:
        n = clean_name(name)
        other = library_by_name(store, n)
        if other and other["id"] != lid:
            raise ValueError("已经有一个叫「%s」的资料库了" % n)
        fields["name"] = n
    if note is not None:
        if not isinstance(note, str):
            raise ValueError("note 应该是文字")
        fields["note"] = note
    if fields:
        fields["updated"] = now()
        store.x("UPDATE kb_libraries SET %s WHERE id=?" % ",".join("%s=?" % k for k in fields), tuple(fields.values()) + (lid,))
    return library(store, lid)


def delete_doc_rows(store, doc_ids):
    """删掉文档和它的节、片段、全文索引、向量记录（调用方持锁或在事务里）。"""
    for did in doc_ids:
        store.x("DELETE FROM kb_fts WHERE rowid IN (SELECT id FROM kb_parts WHERE doc_id=?)", (did,))
        store.x("DELETE FROM kb_parts WHERE doc_id=?", (did,))
        store.x("DELETE FROM kb_sections WHERE doc_id=?", (did,))
        store.x("DELETE FROM kb_vectors WHERE doc_id=?", (did,))
        store.x("DELETE FROM kb_chunks WHERE doc_id=?", (did,))
        store.x("DELETE FROM kb_docfts WHERE doc_id=?", (did,))
        store.x("DELETE FROM kb_docs WHERE id=?", (did,))


def delete_library(svc, lid, allow_shared=False):
    """只删索引，不动文件。公司共享的库不能删（allow_shared：共享目录里已经没有这个库了，由 agent/shared.py 放行）。"""
    store = svc.store
    if not library(store, lid):
        raise KeyError("没有这个资料库")
    check_not_builtin(lid)
    if not allow_shared:
        check_not_shared(store, lid)
    svc.indexer.cancel(lid)
    with svc.indexer.ingest_lock:
        docs = [r["id"] for r in store.q("SELECT id FROM kb_docs WHERE library_id=?", (lid,))]
        try:
            svc.vectors().delete_docs(docs)
        except Exception:
            pass
        with store.tx():
            delete_doc_rows(store, docs)
            store.x("DELETE FROM kb_sources WHERE library_id=?", (lid,))
            store.x("DELETE FROM kb_libraries WHERE id=?", (lid,))


def sources(store, lid):
    return store.q("SELECT * FROM kb_sources WHERE library_id=? ORDER BY added", (lid,))


def add_source(store, lid, path):
    """加一个文件夹或文件。已经有同一个来源就返回那个。"""
    check_not_builtin(lid)
    check_not_shared(store, lid)
    if not isinstance(path, str) or not path.strip():
        raise ValueError("要给一个文件夹或文件的路径")
    p = os.path.realpath(os.path.abspath(os.path.expanduser(path.strip())))
    if not os.path.exists(p):
        raise ValueError("找不到这个文件夹或文件：%s" % path)
    kind = "dir" if os.path.isdir(p) else "file"
    if kind == "file" and os.path.splitext(p)[1].lower() not in supported_ext():
        raise ValueError("暂不支持这种文件：%s" % os.path.basename(p))
    old = store.one("SELECT * FROM kb_sources WHERE library_id=? AND path=?", (lid, p))
    if old:
        return old
    sid = new_id("S_")
    store.x("INSERT INTO kb_sources(id,library_id,path,kind,added) VALUES(?,?,?,?,?)", (sid, lid, p, kind, now()))
    store.x("UPDATE kb_libraries SET updated=? WHERE id=?", (now(), lid))
    return store.one("SELECT * FROM kb_sources WHERE id=?", (sid,))


def remove_source(svc, lid, sid):
    store = svc.store
    src = store.one("SELECT * FROM kb_sources WHERE id=? AND library_id=?", (sid, lid))
    if not src:
        raise KeyError("这个资料库里没有这个来源")
    with svc.indexer.ingest_lock:
        docs = [r["id"] for r in store.q("SELECT id FROM kb_docs WHERE source_id=?", (sid,))]
        try:
            svc.vectors().delete_docs(docs)
        except Exception:
            pass
        with store.tx():
            delete_doc_rows(store, docs)
            store.x("DELETE FROM kb_sources WHERE id=?", (sid,))


def supported_ext():
    from ..docparse import SUPPORTED
    return SUPPORTED


def summary(svc, lib):
    """GET /api/libraries 的一项。"""
    store = svc.store
    lid = lib["id"]
    n = store.one("SELECT COUNT(*) AS docs, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
                  "FROM kb_docs WHERE library_id=? AND status IN ('ok','failed')", (lid,)) or {}
    parts = store.one("SELECT COUNT(*) AS n FROM kb_parts WHERE library_id=?", (lid,)) or {}
    vecs = store.one("SELECT COUNT(*) AS n FROM kb_vectors WHERE library_id=?", (lid,)) or {}
    prog = svc.indexer.progress(lid)
    return {"id": lid, "name": lib["name"], "note": lib.get("note") or "",
            "sources": [{"id": s["id"], "path": s["path"], "kind": s["kind"]} for s in sources(store, lid)],
            "docs": int(n.get("docs") or 0) - int(n.get("failed") or 0), "parts": int(parts.get("n") or 0),
            "failed": int(n.get("failed") or 0), "vectors": int(vecs.get("n") or 0),
            "state": "indexing" if prog["active"] else "ready",
            "progress": {k: prog[k] for k in ("total", "done", "failed", "current")},
            "updated": lib.get("updated"), "synced": lib.get("synced"), "builtin": lid == ANSWERS_ID,
            # 公司共享目录（5.7）：shared 从共享目录导入的；version 共享库是导入的版本，自己的库是上次导出的版本；enabled 停用为 false
            "shared": bool(lib.get("shared")), "version": int(lib.get("version") or 0),
            "enabled": lib.get("enabled") is None or bool(lib.get("enabled"))}


def workspace_library(store, ws):
    """CariAi 的 kb_add 用的库：和这个工作区挂钩的那个（迁移出来的，或者第一次 kb_add 时建的），名字同工作区。"""
    lib = store.one("SELECT * FROM kb_libraries WHERE workspace_id=? ORDER BY created LIMIT 1", (ws["id"],))
    if lib:
        return lib
    base = (ws.get("name") or "资料").replace("/", "／").replace("\\", "＼").strip()[:NAME_MAX - 4] or "资料"
    name, i = base, 2
    while library_by_name(store, name):
        name, i = "%s（%d）" % (base, i), i + 1
    return create_library(store, name, "", workspace_id=ws["id"])


# ---------------- 文档的路径 ----------------

def doc_abs(store, doc):
    """文档在电脑上的绝对路径（找不到来源时 None）。公司共享的库没有来源：用 source_root 加相对路径（5.5）。"""
    src = store.one("SELECT * FROM kb_sources WHERE id=?", (doc.get("source_id"),)) if doc.get("source_id") else None
    if not src:
        return shared_doc_abs(store, doc)
    if src["kind"] == "file":
        return src["path"]
    return os.path.join(src["path"], doc["path"])


def shared_doc_abs(store, doc):
    """共享库的文档：source_root + 相对路径（导入时已经查过相对路径里没有 .. 和绝对路径）。不是共享库、没有根时 None。"""
    lib = store.one("SELECT shared, source_root FROM kb_libraries WHERE id=?", (doc.get("library_id"),))
    root = (lib or {}).get("source_root") or ""
    rel = (doc.get("path") or "").replace("\\", "/")
    if not lib or not lib.get("shared") or not root or not rel:
        return None
    parts = [x for x in rel.split("/") if x not in ("", ".")]
    if not parts or any(x == ".." or ":" in x for x in parts) or os.path.isabs(rel):
        return None
    return os.path.join(root, *parts)


def doc_ref(store, doc, lib_name=None):
    """给模型和界面看的路径：资料库名/相对路径（和检索结果的出处一致，resolve_read 认这种写法）。"""
    if lib_name is None:
        lib = library(store, doc.get("library_id")) or {}
        lib_name = lib.get("name") or "资料库"
    return "%s/%s" % (lib_name, (doc.get("path") or "").replace("\\", "/"))


def _inside(base, real):
    try:
        return os.path.commonpath([base, real]) == base
    except ValueError:
        return False


def _source_roots(store):
    for s in store.q("SELECT s.*, l.name AS lib_name FROM kb_sources s JOIN kb_libraries l ON l.id=s.library_id"):
        yield s, os.path.realpath(s["path"])


def resolve_read(ctx, path):
    """读文件的统一入口（docs/两种模式.md 第 8 节的接缝）：
    1. 先按当前文件夹解析（policy.resolve）：存在就用它；
    2. 再按"资料库名/相对路径"解析（也认资料库来源里的绝对路径）；
    3. 路径在当前文件夹里只是不存在 → 照旧返回那个路径，由调用方说"找不到"；
    4. 当前文件夹以外的路径（绝对路径、~ 开头、../）：这次调用的批准级别是 read_outside 并且放行了
       （全放行直接放行；其他批准方式是用户点了允许）才读；存密码、密钥的地方一律不读（policy.is_secret）。
    符号链接和 .. 逃出资料库文件夹的一律拒绝。返回绝对路径（已经 realpath）。"""
    if not isinstance(path, str) or not path.strip():
        raise ReadPathError("路径不能为空")
    raw = policy.expand(path)
    ws_err, ws_path = None, None
    try:
        ws_path = policy.resolve(ctx.root, raw)
        if os.path.exists(ws_path):
            return ws_path
    except policy.PathError as e:
        ws_err = e
    hit = _resolve_library(ctx.store, raw)
    if hit is not None:
        return hit
    if ws_path is not None:
        return ws_path
    real = _outside_real(ctx, raw)
    if real is not None:
        if policy.is_secret(real, _own_dirs(ctx)):
            raise ReadPathError(policy.SECRET_TEXT)
        why = _outside_refused(ctx)
        if why:
            raise ReadPathError("%s 在当前文件夹以外。%s" % (path.strip(), why))
        if not os.path.exists(real):
            raise ReadPathError("找不到：%s" % path.strip())
        return real
    raise ReadPathError(str(ws_err) if ws_err else "找不到：%s" % path)


def _outside_real(ctx, raw):
    """raw 指向当前文件夹以外、又不在资料库里：返回 realpath；否则 None。"""
    cand = raw if os.path.isabs(raw) else os.path.join(os.path.realpath(ctx.root), raw)
    try:
        real = os.path.realpath(cand)
    except (OSError, ValueError):
        return None
    if _inside(os.path.realpath(ctx.root), real):
        return None
    return real


def _own_dirs(ctx):
    """本程序自己的数据目录（里面有设置、令牌）：和存密码的地方一样不读。"""
    home = getattr(getattr(ctx, "cfg", None), "home", None) or getattr(getattr(getattr(ctx, "rt", None), "cfg", None), "home", None)
    return (home,) if home else ()


def _outside_refused(ctx):
    """这次调用能不能读当前文件夹以外：能 → ""；不能 → 原因（给模型看）。"""
    aid = getattr(ctx, "action_id", None)
    a = ctx.store.action(aid) if aid else None
    mode = (ctx.cfg.get("approval_mode") if getattr(ctx, "cfg", None) else None) or "auto"
    if not a or a.get("risk") != "read_outside" or a.get("state") not in ("approved", "running"):
        return "读当前文件夹以外的文件要用户同意；批准方式是「全放行」时直接读。"
    if getattr(ctx, "is_sub", False) and mode != "full":
        return "派出去的助手只在批准方式是「全放行」时才能读当前文件夹以外的文件。"
    return ""


def outside_paths(ctx, args, keys):
    """这些参数里，指向当前文件夹以外（又不在资料库里）的路径。"""
    out = []
    for k in keys:
        v = args.get(k) if isinstance(args, dict) else None
        if not isinstance(v, str) or not v.strip():
            continue
        raw = policy.expand(v)
        try:
            policy.resolve(ctx.root, raw)
            continue
        except policy.PathError:
            pass
        if _resolve_library(ctx.store, raw) is not None:
            continue
        if _outside_real(ctx, raw) is not None:
            out.append(v)
    return out


def read_risk(*keys, base="read"):
    """读文件工具的批准级别：路径参数都在当前文件夹或资料库里 → base；有一个在外面 → read_outside。
    base 可以是另一个 risk 函数（例如生成 PPT 时模板在外面）。"""
    keys = keys or ("path",)

    def risk(ctx, args):
        try:
            if outside_paths(ctx, args, keys):
                return "read_outside"
        except Exception:
            pass
        return base(ctx, args) if callable(base) else base
    return risk


def _resolve_library(store, raw):
    rows = list(_source_roots(store))
    if not rows:
        return None
    if os.path.isabs(raw) or (len(raw) > 2 and raw[1] == ":"):
        real = os.path.realpath(raw)
        for s, base in rows:
            if (s["kind"] == "dir" and _inside(base, real)) or (s["kind"] == "file" and real == base):
                return real if os.path.exists(real) else None
        return None
    name, _, rel = raw.partition("/")
    rel = rel.strip("/")
    found_lib = False
    for s, base in rows:
        if s["lib_name"] != name:
            continue
        found_lib = True
        if s["kind"] == "file":
            if rel == os.path.basename(base) and os.path.isfile(base):
                return base
            continue
        cand = os.path.realpath(os.path.join(base, rel)) if rel else base
        if not _inside(base, cand):
            raise ReadPathError("路径在资料库的文件夹之外：%s" % raw)
        if os.path.exists(cand):
            return cand
    if found_lib and not rel:
        return None
    return None


def library_of_path(store, real):
    """这个绝对路径在哪个资料库的哪个来源里：(来源, 资料库名) 或 None。"""
    real = os.path.realpath(real)
    for s, base in _source_roots(store):
        if (s["kind"] == "dir" and _inside(base, real)) or (s["kind"] == "file" and real == base):
            return s, s["lib_name"]
    return None


def display_path(store, real, root=None):
    """读资料库里的文件时，结果里显示的路径：资料库名/相对路径。"""
    got = library_of_path(store, real)
    if got:
        s, name = got
        if s["kind"] == "file":
            return "%s/%s" % (name, os.path.basename(real))
        rel = os.path.relpath(real, os.path.realpath(s["path"])).replace("\\", "/")
        return name if rel == "." else "%s/%s" % (name, rel)
    if root:
        rel = policy.relpath(root, real)
        return real if rel == ".." or rel.startswith("../") else rel     # 当前文件夹以外：写完整路径
    return real


WRITE_RISKS = ("create", "overwrite", "delete")
PATH_KEYS = ("path", "src", "dst", "output_path", "out", "target")


def _path_args(args):
    for k, v in (args or {}).items():
        if isinstance(v, str) and v.strip() and (k in PATH_KEYS or k.endswith("_path")):
            yield k, v


def write_violation(ctx, tool_name, args, risk):
    """写工具指向资料库里的路径：返回一句拒绝的话；否则 None。两个内核在批准之前调用（不弹批准卡片）。
    - 资料库来源是当前文件夹里的子文件夹或文件：里面一律不能写；
    - 来源就是当前文件夹或包着它（例如迁移来的库）：已经收进库的文件不能改、不能删，新建文件照常；
    - 来源在当前文件夹外面：policy.resolve 本来就不让写。"""
    if risk not in WRITE_RISKS:
        return None
    store = getattr(ctx, "store", None)
    root = getattr(ctx, "root", None)
    if store is None or not root:
        return None
    rows = list(_source_roots(store))
    if not rows:
        return None
    realroot = os.path.realpath(root)
    for _, v in _path_args(args):
        try:
            real = policy.resolve(root, v, write=True)
        except policy.PathError:
            continue                    # 交给工具自己按原来的规矩拒绝
        for s, base in rows:
            if s["kind"] == "file":
                if real == base:
                    return _deny(s["lib_name"], v)
                continue
            if not _inside(base, real):
                continue
            if _inside(realroot, base) and base != realroot:
                return _deny(s["lib_name"], v)
            if os.path.lexists(real):
                return _deny(s["lib_name"], v)
    return None


def _deny(lib_name, path):
    return ("「%s」在资料库「%s」里。资料库里的文件是只读的，不能改、不能删、不能在里面新建。"
            "要改的话另存一份到当前文件夹里（换个路径）。" % (path, lib_name))


# ---------------- 对话的检索范围 ----------------

def root_session(store, sid):
    s = store.session(sid) if sid else None
    if s and s.get("kind") == "sub" and s.get("parent_id"):
        p = store.session(s["parent_id"])
        if p:
            return p
    return s


def scope(ctx):
    """这个对话默认查哪些资料库（id 列表）：对话的 meta.libraries（子代理用父对话的）；空或没写表示全部。
    已经删掉的库自动去掉。"""
    store = ctx.store
    s = root_session(store, getattr(ctx, "sid", None)) or {}
    want = (s.get("meta") or {}).get("libraries") or []
    all_ids = enabled_ids(store)          # 停用的库（5.5）不参与检索
    if want:
        chosen = [x for x in want if x in all_ids]
        if chosen:
            return with_answers(store, chosen)
    return all_ids


def scope_meta(ctx):
    """对话 meta 里的检索范围：(libraries, docs, tags)，子代理用父对话的。"""
    s = root_session(ctx.store, getattr(ctx, "sid", None)) or {}
    m = s.get("meta") or {}
    return list(m.get("libraries") or []), list(m.get("docs") or []), list(m.get("tags") or [])


def docs_with_tag(store, lib_ids, tag):
    """这些库里带这个标签的文档 id（标签按去首尾空白、不分大小写比）。"""
    want = str(tag or "").strip().lower()
    if not want or not lib_ids:
        return []
    out = []
    for r in store.q("SELECT id, tags FROM kb_docs WHERE status='ok' AND library_id IN (%s)" % ",".join("?" * len(lib_ids)),
                     tuple(lib_ids)):
        try:
            tags = json.loads(r["tags"] or "[]")
        except ValueError:
            tags = []
        if any(isinstance(t, str) and t.strip().lower() == want for t in tags):
            out.append(r["id"])
    return out


def scope_docs(ctx, lib_ids=None):
    """docs/借鉴开源项目.md 2.4：对话的 meta 里还可以有 docs（文档 id）和 tags。
    指定的文件和标签之间是"或"：点了名的文件一定查，标签再把带这些标签的文件带上（联调后改的：原来取交集，
    点名的文件没有那个标签时会被悄悄排除，界面上却还显示着它）。几个标签之间也是"或"。合起来再限定在所选的资料库里。
    返回 None（不限文档）或文档 id 的列表（可能为空：范围里一份都没有）。标准答案不受这个限制（检索时另外带上）。"""
    _, docs, tags = scope_meta(ctx)
    if not docs and not tags:
        return None
    store = ctx.store
    libs = [x for x in (lib_ids if lib_ids is not None else scope(ctx)) if x != ANSWERS_ID]
    if not libs:
        return []
    allowed = {r["id"] for r in store.q("SELECT id FROM kb_docs WHERE status='ok' AND library_id IN (%s)"
                                        % ",".join("?" * len(libs)), tuple(libs))}
    picked = set(docs or [])
    for t in tags or []:           # 标签之间、标签和点名的文件之间都是"或"
        picked |= set(docs_with_tag(store, libs, t))
    allowed &= picked
    return sorted(allowed)


def with_answers(store, lib_ids):
    """9.5：标准答案库是全局的纠正，限定了范围也参与检索（库已经建了时）。"""
    ids = list(lib_ids or [])
    if ANSWERS_ID not in ids and library(store, ANSWERS_ID):
        ids.append(ANSWERS_ID)
    return ids


# ---------------- 编号和出处（3.1） ----------------

KB_RESULT_TOOLS = ("kb_search", "kb_open")


def _ledger_sessions(store, sid):
    root = root_session(store, sid)
    if not root:
        return []
    ids = [root["id"]] + [r["id"] for r in store.q("SELECT id FROM sessions WHERE parent_id=?", (root["id"],))]
    return ids


def cited_parts(store, sid, upto_seq=None):
    """这个对话（含子代理）里到 upto_seq 为止编过号的片段：{n: hit}。hit 就是 kb_search / kb_open 结果 payload 里的那一项
    （n、part_id、doc_id、title、loc、text……）。同一个编号以第一次出现的为准。
    给 9.3 的核对数字用：cited_parts(store, sid, msg_seq)[n]["text"] 是当时给模型看的原文；
    要片段所在整节的全文用 part_detail(store, hit["part_id"])["section"]["text"]。"""
    out = {}
    ids = _ledger_sessions(store, sid)
    if not ids:
        return out
    rows = store.q("SELECT seq, payload FROM events WHERE type='tool_result' AND session_id IN (%s) %s ORDER BY seq"
                   % (",".join("?" * len(ids)), "AND seq<=?" if upto_seq else ""),
                   tuple(ids) + ((int(upto_seq),) if upto_seq else ()))
    for r in rows:
        try:
            p = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
        except ValueError:
            continue
        if p.get("tool") not in KB_RESULT_TOOLS:
            continue
        for h in p.get("hits") or []:
            n = h.get("n")
            if isinstance(n, int) and n not in out:
                out[n] = dict(h, seq=r["seq"])
    return out


def cite_map(store, sid):
    """{part_id: n} 和目前最大的编号。编号在一个对话里不重复；同一个片段再次被找到还用原来的编号。"""
    by_part, top = {}, 0
    for n, h in sorted(cited_parts(store, sid).items()):
        top = max(top, n)
        for pid in [h.get("part_id")] + list(h.get("parts") or []):
            if pid is not None and pid not in by_part:
                by_part[pid] = n
    return by_part, top


def part_detail(store, part_id):
    """一个片段和它所在的整节（GET /api/kb/parts/<id>，界面的"来源"面板用）。没有返回 None。"""
    try:
        pid = int(part_id)
    except (TypeError, ValueError):
        return None
    p = store.one("SELECT * FROM kb_parts WHERE id=?", (pid,))
    if not p:
        return None
    doc = store.one("SELECT * FROM kb_docs WHERE id=?", (p["doc_id"],)) or {}
    lib = library(store, p["library_id"]) or {}
    sec = store.one("SELECT * FROM kb_sections WHERE id=?", (p["parent"],)) if p.get("parent") else None
    ref = doc_ref(store, doc, lib.get("name")) if doc else ""
    return {"part": {"id": p["id"], "part_id": p["id"], "doc_id": p["doc_id"], "library": lib.get("name"),
                     "library_id": lib.get("id"), "title": doc.get("title"), "path": ref, "seq": p["seq"], "loc": p["loc"],
                     "heading": p["heading"], "text": p["text"], "tokens": p["tokens"], "kind": doc.get("kind") or "",
                     "version_label": doc.get("version_label") or "", "superseded": False, "newer": "",
                     "standard": p["library_id"] == ANSWERS_ID},
            "section": ({"id": sec["id"], "loc": sec["loc"], "heading": sec["heading"], "text": sec["text"],
                         "tokens": sec["tokens"]} if sec else None),
            "doc": {"id": doc.get("id"), "title": doc.get("title"), "path": ref,
                    "kind": doc.get("kind"), "version_label": doc.get("version_label") or ""},
            "library": {"id": lib.get("id"), "name": lib.get("name")}}


def stem(title):
    return os.path.splitext(title or "")[0] or (title or "")
