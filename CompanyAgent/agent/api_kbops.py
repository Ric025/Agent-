"""文档卡片、标签、没答上来的问题、标准答案命中次数、检索评测 的接口（docs/借鉴开源项目.md）。归"后端·资料"管。

接口用 route 登记；要改 server.py 里已有接口的行为，用 replace_route 换掉它，不要去改 server.py。
设置里新加的键用 SETTINGS_EXT / KB_KEYS_EXT 登记校验函数，保存之后要做的事放进 SETTINGS_AFTER。

换掉的接口（原来的行为原样保留）：
- GET /api/libraries、GET /api/libraries/<id>/docs：调原来的函数拿到结果，再补字段（_call）。
- POST /api/sessions：原函数在建对话和发第一句话之间没有钩子，所以把它的逻辑搬过来，多收 docs、tags。
- PATCH /api/sessions/<id>：先处理 docs、tags，其余（title、libraries）交给原函数。
- POST /api/kb/search：原函数的逻辑搬过来，多收 docs、tags（检索时要限定文档，没法事后补）。"""
from __future__ import annotations
import base64, json, os

from .server import route, replace_route, HTTPError, SETTINGS_EXT, KB_KEYS_EXT, SETTINGS_AFTER  # noqa: F401
from . import server as srv
from . import kb as kbm
from .kb import cards, ops

TAGS_MAX_SCOPE = 10
DOCS_MAX_SCOPE = 50


def _call(fn, h, qs, *args, body=None):
    """调 server.py 里原来的接口函数，把它要发的 JSON 截下来返回（不发）。body 给了就当作请求体。"""
    box = {}
    saved = {}
    for name in ("send_json", "body"):
        if name in h.__dict__:
            saved[name] = h.__dict__[name]

    def grab(obj, code=200):
        box["obj"], box["code"] = obj, code
    h.send_json = grab
    if body is not None:
        h.body = lambda: body
    try:
        fn(h, qs, *args)
    finally:
        for name in ("send_json", "body"):
            if name in saved:
                setattr(h, name, saved[name])
            else:
                h.__dict__.pop(name, None)
    return box.get("obj"), box.get("code", 200)


def _svc(h):
    return kbm.get(h.rt)


def _cw(rt):
    caps = srv._caps(rt)
    eps = caps.endpoints()
    return caps.context_of(eps[0]) if eps else 32768


# ---------------- 设置 ----------------

def _check_cards(v):
    if v not in ("auto", "off"):
        raise HTTPError(400, "kb.cards 只能是 auto 或 off")
    return v


KB_KEYS_EXT["cards"] = _check_cards


def _after_settings(h, changed):
    """改了 kb.cards、端点或模型：后台写摘要的线程看一眼（模型可用、有待写的才会起来）。"""
    if not isinstance(changed, dict):
        return
    if "kb" in changed or "endpoints" in changed or "model" in changed:
        try:
            svc = _svc(h)
            svc.cards.down_until = 0.0
            svc.cards.kick()
        except Exception:
            pass


SETTINGS_AFTER.append(_after_settings)


# ---------------- 资料库列表、文档列表（换掉原来的，补字段） ----------------

def cards_summary(svc, lid):
    c = cards.counts(svc.store, lid)
    st, why = svc.cards.state()
    c["mode"] = st                      # model（后台用模型写）· extract（模型不可用，用开头摘录）· off（设置里关了）
    c["reason"] = why
    c["writing"] = svc.cards.current is not None
    return c


@replace_route("GET", "/api/libraries")
def api_libraries(h, qs):
    """每项多：cards {total, ready, pending, failed, extract, mode, reason, writing}、lang、tags [{tag, docs}]（最多 30 个）。"""
    items, code = _call(srv.api_libraries, h, qs)
    svc = _svc(h)
    if isinstance(items, list):
        for it in items:
            lid = it.get("id")
            it["cards"] = cards_summary(svc, lid)
            it["lang"] = cards.libs_lang(svc.store, [lid])
            it["tags"] = cards.lib_tags(svc.store, lid)
    h.send_json(items, code)


def _doc_extra(d):
    return {"summary": d.get("summary") or "", "summary_by": d.get("summary_by") or "", "card_state": d.get("card_state") or "",
            "outline_count": len(cards.outline_of(d)), "tags": cards.tags_of(d), "lang": d.get("lang") or "",
            "keywords": cards.keywords_of(d)}


@replace_route("GET", "/api/libraries/([\\w-]+)/docs")
def api_library_docs(h, qs, lid):
    """每项多：summary、summary_by、card_state、outline_count、tags、lang、keywords。
    多两个可选的筛选：?tag=（带这个标签的）；?q= 原来按文件名筛，现在也在摘要、关键词、标签里找（原来能筛出的照样筛得出）。"""
    store = h.rt.store
    q = (srv._q1(qs, "q") or "").strip().lower()
    qs2 = {k: v for k, v in qs.items() if k != "q"} if q else qs
    rows, code = _call(srv.api_library_docs, h, qs2, lid)
    if code != 200 or not isinstance(rows, list):
        return h.send_json(rows, code)
    full = {d["id"]: d for d in store.q("SELECT * FROM kb_docs WHERE library_id=?", (lid,))}
    out = []
    tag = (srv._q1(qs, "tag") or "").strip().lower()
    for r in rows:
        d = full.get(r.get("id")) or {}
        extra = _doc_extra(d)
        if tag and tag not in [t.lower() for t in extra["tags"]]:
            continue
        if q:
            hay = " ".join([r.get("path") or "", r.get("title") or "", extra["summary"], " ".join(extra["keywords"]),
                            " ".join(extra["tags"])]).lower()
            if q not in hay:
                continue
        r.update(extra)
        out.append(r)
    h.send_json(out)


@route("GET", "/api/kb/docs")
def api_kb_docs_find(h, qs):
    """"指定文件…"的搜索框：?q=&library=（可选）→ [{id, title, path, library_id, library, kind, summary, summary_by, tags}]，
    在文件名、摘要、关键词、标签里找，按相关度排，最多 50 份。不给 q 就按资料库、路径列前 50 份。不含标准答案。"""
    from .tools import kb as kbt
    store = h.rt.store
    lid = srv._q1(qs, "library")
    libs = [lid] if lid else [r["id"] for r in kbm.libraries(store)]
    if lid and not kbm.library(store, lid):
        raise HTTPError(404, "没有这个资料库")
    rows = kbt._scope_rows(store, libs)
    q = (srv._q1(qs, "q") or "").strip()
    if q:
        ranked = kbt.find_docs(store, rows, q)
        low = q.lower()
        rest = [r for r in rows if r not in ranked and (low in (r["path"] or "").lower() or low in (r["title"] or "").lower())]
        rows = ranked + rest
    h.send_json([{"id": r["id"], "title": r["title"], "path": r["path"], "library_id": r["library_id"], "library": r["lib_name"],
                  "kind": r.get("kind") or "", "summary": r.get("summary") or "", "summary_by": r.get("summary_by") or "",
                  "tags": cards.tags_of(r)} for r in rows[:50]])


# ---------------- 文档卡片 ----------------

def _doc_or_404(h, did):
    d = h.rt.store.one("SELECT * FROM kb_docs WHERE id=?", (did,))
    if not d:
        raise HTTPError(404, "没有这个文档")
    return d


@route("GET", "/api/kb/docs/([\\w-]+)/card")
def api_doc_card(h, qs, did):
    h.send_json(cards.card(h.rt.store, _doc_or_404(h, did)))


@route("PATCH", "/api/kb/docs/([\\w-]+)")
def api_doc_patch(h, qs, did):
    """{tags?: ["…"], summary?: "…"} → 卡片。标签每个 1 到 20 个字、最多 10 个；改过标签之后不再跟着路径变；
    改过的摘要 summary_by = user，不再自动覆盖。"""
    _doc_or_404(h, did)
    h.send_json(cards.patch(h.rt.store, did, h.body()))


@route("POST", "/api/kb/docs/([\\w-]+)/card")
def api_doc_card_action(h, qs, did):
    """{action: "regenerate"} → 202 {ok, queued, mode, reason}：标成待写，排进后台（模型不可用时先排着，可用了再写）。"""
    d = _doc_or_404(h, did)
    b = h.body()
    if b.get("action") != "regenerate":
        raise HTTPError(400, "action 只能是 regenerate")
    if d.get("library_id") == kbm.ANSWERS_ID or d.get("status") != "ok":
        raise HTTPError(400, "这份文档没有卡片（没收录成功，或者是标准答案）")
    svc = _svc(h)
    svc.cards.regenerate(did)
    st, why = svc.cards.state()
    h.send_json({"ok": True, "queued": True, "mode": st, "reason": why}, 202)


@route("GET", "/api/kb/sections/([\\w-]+)")
def api_section(h, qs, sec_id):
    """界面点目录项时看那一节：{id, doc_id, loc, heading, text}。"""
    try:
        sid = int(sec_id)
    except ValueError:
        raise HTTPError(404, "没有这一节")
    r = h.rt.store.one("SELECT id, doc_id, loc, heading, text FROM kb_sections WHERE id=?", (sid,))
    if not r:
        raise HTTPError(404, "没有这一节")
    h.send_json(r)


# ---------------- 对话的检索范围：docs、tags（2.4） ----------------

def check_docs(rt, v):
    if v is None:
        return None
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise HTTPError(400, "docs 应该是文档 id 的列表")
    out = []
    for x in v:
        d = rt.store.one("SELECT id, library_id FROM kb_docs WHERE id=?", (x,))
        if not d or d["library_id"] == kbm.ANSWERS_ID:
            raise HTTPError(400, "没有这个文档：%s" % x)
        if x not in out:
            out.append(x)
    if len(out) > DOCS_MAX_SCOPE:
        raise HTTPError(400, "最多指定 %d 份文件" % DOCS_MAX_SCOPE)
    return out


def check_tags(v):
    if v is None:
        return None
    try:
        out = cards.clean_tags(v)
    except ValueError as e:
        raise HTTPError(400, str(e).replace("一份文件最多", "最多选"))
    return out


@replace_route("POST", "/api/sessions")
def api_session_new(h, qs):
    """原来的逻辑（server.api_session_new）搬过来：多收 docs、tags，和 libraries 一起存进对话的 meta，再发第一句话。"""
    b = h.body()
    ws = h.ws_from(wid=b.get("workspace_id"))
    mode = srv.check_mode(b.get("mode"))
    libs = srv.check_libraries(h.rt, b.get("libraries"))
    docs = check_docs(h.rt, b.get("docs"))
    tags = check_tags(b.get("tags"))
    text = str(b.get("text") or "")
    images = srv._check_images(b.get("images"), h.rt)
    files = srv.check_files(ws, b.get("files"))
    skill = srv.check_skill(h.rt, b.get("skill"))
    meta = {}
    if libs:
        meta["libraries"] = libs
    if docs:
        meta["docs"] = docs
    if tags:
        meta["tags"] = tags
    s = h.rt.store.add_session(ws["id"], mode=mode, meta=meta or None)
    if text.strip() or images or files or skill:
        h.rt.send_message(s["id"], text, images, files, skill, srv._depth(b))
    h.send_json(h.rt.store.session(s["id"]))


@replace_route("PATCH", "/api/sessions/([\\w-]+)")
def api_session_patch(h, qs, sid):
    """多收 docs、tags（空列表表示不限）；title、libraries 照原来的处理（server.api_session_patch）。"""
    s0 = srv._session_or_404(h.rt, sid)
    b = h.body()
    if "docs" in b or "tags" in b:
        docs = check_docs(h.rt, b["docs"]) if "docs" in b else None
        tags = check_tags(b["tags"]) if "tags" in b else None
        meta = dict(s0.get("meta") or {})
        if docs is not None:
            meta["docs"] = docs
        if tags is not None:
            meta["tags"] = tags
        h.rt.store.set_session(sid, meta=meta)
        rest = {k: v for k, v in b.items() if k not in ("docs", "tags")}
        if "title" not in rest and "libraries" not in rest:
            return h.send_json(h.rt.store.session(sid))
        b = rest
    obj, code = _call(srv.api_session_patch, h, qs, sid, body=b)
    h.send_json(obj, code)


# ---------------- 检索测试（多收 docs、tags） ----------------

@replace_route("POST", "/api/kb/search")
def api_kb_search(h, qs):
    """检索测试：{"query", "libraries"?: [id], "docs"?: [id], "tags"?: ["…"], "top_k"?} → {"hits", "weak", "used", "ms"}。
    编号从 1 开始，不占对话的编号。原来的逻辑（server.api_kb_search）搬过来，多了 docs、tags（两样之间是"或"，合起来再限定在 libraries 里）。
    命中多一个可选的 exact（标准答案和问题几乎一样）。"""
    from .kb import retrieve
    b = h.body()
    q = b.get("query")
    if not isinstance(q, str) or not q.strip():
        raise HTTPError(400, "缺少 query")
    base = srv.check_libraries(h.rt, b.get("libraries")) or kbm.enabled_ids(h.rt.store)   # 停用的库不参与（5.5）
    libs = kbm.with_answers(h.rt.store, base)          # 9.5：标准答案总是参与
    docs = check_docs(h.rt, b.get("docs"))
    tags = check_tags(b.get("tags"))
    doc_ids = None
    if docs or tags:
        store = h.rt.store
        plain = [x for x in base if x != kbm.ANSWERS_ID]
        allowed = {r["id"] for r in store.q("SELECT id FROM kb_docs WHERE status='ok' AND library_id IN (%s)"
                                            % (",".join("?" * len(plain)) or "NULL"), tuple(plain))} if plain else set()
        picked = set(docs or [])           # 点名的文件和标签之间是"或"（见 kb.scope_docs）
        for t in tags or []:
            picked |= set(kbm.docs_with_tag(store, plain, t))
        allowed &= picked
        doc_ids = sorted(allowed)
    k = b.get("top_k", h.rt.cfg.get("kb", "top_k", default=5))
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 20:
        raise HTTPError(400, "top_k 应该是 1 到 20 的整数")
    svc = _svc(h)
    cw = _cw(h.rt)
    res = retrieve.search(svc, [q], libs, top_k=k, context_window=cw, doc_ids=doc_ids)
    hits = retrieve.expand(h.rt.store, res["hits"])
    for i, x in enumerate(hits, 1):
        x["n"] = i
    _, kept = retrieve.render(hits, retrieve.budget(cw))
    weak = retrieve.expand(h.rt.store, res.get("weak") or [])
    for i, x in enumerate(weak, 1):
        x["n"] = i
    h.send_json({"hits": [retrieve.public(x) for x in kept], "weak": [retrieve.public(x) for x in weak],
                 "used": res["used"], "ms": res["ms"]})


# ---------------- 没答上来的问题（4.1） ----------------

@route("GET", "/api/kb/gaps")
def api_gaps(h, qs):
    """?status=open|done|ignored（不给是全部）→ [{id, question, reason, count, first_ts, last_ts, session_id, seq, status, note,
    answer_id}]，按 count 再按 last_ts 排。"""
    try:
        h.send_json(ops.gaps(h.rt.store, srv._q1(qs, "status") or None))
    except ValueError as e:
        raise HTTPError(400, str(e))


@route("GET", "/api/kb/gaps/count")
def api_gaps_count(h, qs):
    h.send_json(ops.gap_count(h.rt.store))


@route("GET", "/api/kb/gaps/export")
def api_gaps_export(h, qs):
    from urllib.parse import quote
    h.send_bytes(ops.gaps_csv(h.rt.store), "text/csv; charset=utf-8",
                 disposition="attachment; filename*=UTF-8''%s" % quote("没答上来的问题.csv"))


@route("PATCH", "/api/kb/gaps/([\\w-]+)")
def api_gap_patch(h, qs, gid):
    h.send_json(ops.patch_gap(h.rt.store, gid, h.body()))


@route("POST", "/api/kb/gaps/([\\w-]+)/answer")
def api_gap_answer(h, qs, gid):
    """{answer} → 建一条标准答案（问题用这一条的），这一条标成 done，返回那条标准答案。"""
    h.send_json(ops.answer_gap(h.rt, gid, h.body()))


# ---------------- 检索评测（4.3） ----------------

@route("GET", "/api/kb/eval")
def api_eval(h, qs):
    h.send_json(ops.eval_state(h.rt.store))


@route("POST", "/api/kb/eval")
def api_eval_add(h, qs):
    """{question, expect: ["文件名", …] 或 "a；b", note?} → 那一题。"""
    h.send_json(ops.eval_add(h.rt.store, h.body()))


@route("DELETE", "/api/kb/eval/([\\w-]+)")
def api_eval_del(h, qs, eid):
    ops.eval_delete(h.rt.store, eid)
    h.send_json({"ok": True})


IMPORT_MAX = 10 * 1024 * 1024


@route("POST", "/api/kb/eval/import")
def api_eval_import(h, qs):
    """上传 CSV 或 xlsx：两列"问题""应命中的文件"（一格里几个文件用分号隔开）。两种传法：
    - 请求体是文件的原始字节，?name=题目.xlsx（看扩展名认格式；不给按 CSV）；
    - JSON：{"name": "题目.xlsx", "data": base64 或 data URL} 或 {"csv": "问题,应命中的文件\\n…"}。
    → {"added", "skipped", "reasons": [{"row", "why"}]}。"""
    ctype = (h.headers.get("Content-Type") or "").lower()
    name = srv._q1(qs, "name") or ""
    if "json" in ctype:
        b = h.body()
        if isinstance(b.get("csv"), str):
            data, name = b["csv"].encode("utf-8"), name or "x.csv"
        else:
            raw = b.get("data")
            if not isinstance(raw, str) or not raw:
                raise HTTPError(400, "要给 data（文件内容的 base64）或 csv")
            if raw.startswith("data:") and "," in raw:
                raw = raw.split(",", 1)[1]
            try:
                data = base64.b64decode(raw)
            except Exception:
                raise HTTPError(400, "data 不是合法的 base64")
            name = str(b.get("name") or name or "x.csv")
    else:
        n = int(h.headers.get("Content-Length") or 0)
        if n <= 0:
            raise HTTPError(400, "没有收到文件")
        if n > IMPORT_MAX:
            raise HTTPError(413, "文件太大（最多 10 MB）")
        data = h.rfile.read(n)
    if os.path.splitext(name)[1].lower() not in ("", ".csv", ".txt", ".xlsx", ".xlsm"):
        raise HTTPError(400, "只认 CSV 或 xlsx")
    try:
        rows = ops.table_rows(name, data)
    except ValueError:
        raise
    except Exception as e:
        raise HTTPError(400, "读不出这个文件：%s" % e)
    h.send_json(ops.eval_import(h.rt.store, rows))


@route("POST", "/api/kb/eval/seed")
def api_eval_seed(h, qs):
    """从点过"有用"的回答里生成（问题 → 它引用的文件），已有的不重复加。→ {"added"}。"""
    h.send_json(ops.eval_seed(h.rt.store))


@route("POST", "/api/kb/eval/run")
def api_eval_run(h, qs):
    """{mode?: auto | keyword | hybrid} → {id, mode, total, hit1, hit3, hit5, ms, misses: [{question, expect, got}]}。
    hybrid 在没有语义检索时返回 400。返回的 mode 是实际用的（keyword 或 hybrid）。"""
    b = h.body()
    mode = b.get("mode") or "auto"
    try:
        h.send_json(ops.eval_run(_svc(h), mode, _cw(h.rt)))
    except ValueError as e:
        raise HTTPError(400, str(e))


@route("GET", "/api/kb/eval/runs/([\\w-]+)")
def api_eval_run_detail(h, qs, rid):
    """一次评测的明细（每题的应命中、实际前 5、名次）。规格里没有，界面"和上一次比"时可以用。"""
    r = h.rt.store.one("SELECT * FROM kb_eval_runs WHERE id=?", (rid,))
    if not r:
        raise HTTPError(404, "没有这一次")
    h.send_json(ops._run_row(r, detail=True))
