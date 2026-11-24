"""反馈和标准答案（docs/两种模式.md 9.5）：Hspace 的"越用越准"。

反馈：
- POST /api/sessions/<id>/feedback {"seq", "rating": "up" | "down" | null, "note"?} → 一条 feedback 事件
  {"seq", "rating", "note"}。同一条回答再点是改，null 是取消；以最后一条为准（current）。

标准答案：
- 内置资料库，固定 id kb.ANSWERS_ID（lib_answers）、名字"标准答案"，第一次用到时才建；没有来源（收录线程扫它时只补向量）；
  不能改名、不能删、不能加来源。
- 每条是一个问答对：一份文档（kind=qa，meta 里存问题、答案、来源）加一节一个片段，文字是"问：…\\n答：…"，进全文索引；
  配了向量模型时由收录线程补向量（和别的库一样）。
- 问题规整后（去首尾空白、全半角、大小写、末尾的问号）相同的算同一条：再存就是更新。问题 1 到 300 字，答案 1 到 4000 字。
- 检索时和别的库一起查（对话限定了范围也参与），命中排最前、standard=true、给模型看的文字里标"（标准答案）"。"""
from __future__ import annotations
import hashlib, json, re, unicodedata

from .store import new_id, now
from . import kb as kbm

RATINGS = ("up", "down", None)
NOTE_MAX = 2000
Q_MAX, A_MAX = 300, 4000
TITLE = "标准答案"


class FeedbackError(ValueError):
    """参数不对（接口回 400）。"""


# ---------------- 反馈 ----------------

def _assistant_event(store, sid, seq):
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None
    r = store.one("SELECT seq, type, session_id FROM events WHERE seq=?", (seq,))
    if not r or r["session_id"] != sid or r["type"] != "assistant_message":
        return None
    return r


def check_feedback(store, sid, body):
    """校验请求体，返回 (seq, rating, note)。不对抛 FeedbackError。"""
    if not isinstance(body, dict):
        raise FeedbackError("请求内容应该是 JSON 对象")
    seq = body.get("seq")
    if _assistant_event(store, sid, seq) is None:
        raise FeedbackError("seq 应该是这个对话里一条助手回答的事件序号")
    if "rating" not in body:
        raise FeedbackError("缺少 rating（up、down 或 null）")
    rating = body.get("rating")
    if rating not in RATINGS:
        raise FeedbackError("rating 只能是 up、down 或 null")
    note = body.get("note")
    if note is None:
        note = ""
    if not isinstance(note, str):
        raise FeedbackError("note 应该是文字")
    note = note.strip()
    if len(note) > NOTE_MAX:
        raise FeedbackError("note 最多 %d 个字" % NOTE_MAX)
    return seq, rating, note


def record(rt, sid, body):
    """写一条 feedback 事件，返回它的 payload。"""
    seq, rating, note = check_feedback(rt.store, sid, body)
    payload = {"seq": seq, "rating": rating, "note": note}
    rt.emit(sid, "feedback", payload)
    try:                                   # 点了"不对"：记进"没答上来的问题"（docs/借鉴开源项目.md 4.1）
        from .kb import ops
        ops.after_feedback(rt, sid, payload)
    except Exception:
        import traceback
        traceback.print_exc()
    return payload


def current_from_events(events):
    """{回答的 seq: {"rating", "note", "ts"}}：每条回答现在的反馈（最后一条为准；取消了的不在里面）。"""
    out = {}
    for e in events:
        if e["type"] != "feedback":
            continue
        p = e["payload"] or {}
        s = p.get("seq")
        if p.get("rating") in ("up", "down"):
            out[s] = {"rating": p["rating"], "note": p.get("note") or "", "ts": e.get("ts")}
        else:
            out.pop(s, None)
    return out


def current(store, sid):
    return current_from_events(store.events(sid, types=("feedback",)))


# ---------------- 标准答案库 ----------------

def ensure_library(store):
    lib = kbm.library(store, kbm.ANSWERS_ID)
    if lib:
        return lib
    t = now()
    store.x("INSERT OR IGNORE INTO kb_libraries(id,name,note,created,updated) VALUES(?,?,?,?,?)",
            (kbm.ANSWERS_ID, _free_name(store), "用户确认过的问答，检索时排最前", t, t))
    return kbm.library(store, kbm.ANSWERS_ID)


def _free_name(store):
    """标准答案库的名字：已经有人建了一个叫"标准答案"的普通库时加"（内置）"，免得重名。"""
    name = kbm.ANSWERS_NAME
    other = kbm.library_by_name(store, name)
    if other and other["id"] != kbm.ANSWERS_ID:
        name = kbm.ANSWERS_NAME + "（内置）"
    return name


def norm_question(q):
    s = unicodedata.normalize("NFKC", q or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[?？!！。.\s]+$", "", s)
    return s


def _key(q):
    return hashlib.sha1(norm_question(q).encode("utf-8")).hexdigest()[:20]


def _clean(v, what, cap):
    if not isinstance(v, str):
        raise FeedbackError("%s应该是文字" % what)
    v = v.strip()
    if not v:
        raise FeedbackError("%s不能为空" % what)
    if len(v) > cap:
        raise FeedbackError("%s最多 %d 个字" % (what, cap))
    return v


def qa_text(question, answer):
    return "问：%s\n答：%s" % (question, answer)


def _meta(doc):
    try:
        return json.loads(doc.get("meta") or "{}")
    except ValueError:
        return {}


def public(doc):
    m = _meta(doc)
    out = {"id": doc["id"], "question": m.get("question") or "", "answer": m.get("answer") or "",
           "created": m.get("created") or doc.get("added"), "updated": m.get("updated") or doc.get("added"),
           "hits": int(doc.get("hits") or 0), "last_hit": doc.get("last_hit")}     # 4.2：被引用了几次
    if m.get("source"):
        out["source"] = m["source"]
    return out


def list_answers(store):
    if not kbm.library(store, kbm.ANSWERS_ID):
        return []
    rows = store.q("SELECT * FROM kb_docs WHERE library_id=? ORDER BY added", (kbm.ANSWERS_ID,))
    return [public(r) for r in rows]


def get_answer(store, aid):
    d = store.one("SELECT * FROM kb_docs WHERE id=? AND library_id=?", (aid, kbm.ANSWERS_ID))
    return public(d) if d else None


def _write(svc, did, question, answer, meta, new):
    """写一份标准答案文档（新建或覆盖）：一节、一个片段、全文索引；向量交给收录线程补。"""
    from .kb.ingest import fts_columns
    from .context import estimate_text
    store = svc.store
    text = qa_text(question, answer)
    tok = estimate_text(text)
    loc = question if len(question) <= 80 else question[:79] + "…"
    with svc.indexer.ingest_lock:
        if not new:
            try:
                svc.vectors().delete_docs([did])
            except Exception:
                pass
        with store.tx():
            if new:
                store.x("INSERT INTO kb_docs(id,path,title,mtime,chunks,added,library_id,source_id) VALUES(?,?,?,?,?,?,?,?)",
                        (did, did, TITLE, now(), 1, now(), kbm.ANSWERS_ID, None))
            else:
                store.x("DELETE FROM kb_fts WHERE rowid IN (SELECT id FROM kb_parts WHERE doc_id=?)", (did,))
                store.x("DELETE FROM kb_parts WHERE doc_id=?", (did,))
                store.x("DELETE FROM kb_sections WHERE doc_id=?", (did,))
                store.x("DELETE FROM kb_vectors WHERE doc_id=?", (did,))
            cur = store.db.execute("INSERT INTO kb_sections(doc_id,library_id,seq,loc,heading,text,tokens) VALUES(?,?,?,?,?,?,?)",
                                   (did, kbm.ANSWERS_ID, 0, loc, "", text, tok))
            sec = cur.lastrowid
            part = {"loc": loc, "heading": "", "text": text}
            cur = store.db.execute("INSERT INTO kb_parts(doc_id,library_id,seq,parent,loc,heading,text,tokens) VALUES(?,?,?,?,?,?,?,?)",
                                   (did, kbm.ANSWERS_ID, 0, sec, loc, "", text, tok))
            pid = cur.lastrowid
            store.db.execute("INSERT INTO kb_fts(rowid,text,heading,part_id,library_id) VALUES(?,?,?,?,?)",
                             (pid,) + fts_columns(part) + (pid, kbm.ANSWERS_ID))
            store.x("UPDATE kb_docs SET title=?, mtime=?, chunks=1, size=?, sha=?, status='ok', error='', kind='qa', tokens=?, "
                    "meta=?, family='', version_label='', effective=? WHERE id=?",
                    (TITLE, now(), len(text.encode("utf-8")), meta["key"], tok, json.dumps(meta, ensure_ascii=False), now(), did))
            store.x("UPDATE kb_libraries SET updated=? WHERE id=?", (now(), kbm.ANSWERS_ID))
    try:
        from .kb import embed as emb
        if emb.configured(svc.cfg):
            svc.indexer.enqueue(kbm.ANSWERS_ID)       # 后台补向量（扫这个库时只补向量，见 ingest.run_job）
    except Exception:
        pass


def save_answer(rt, body):
    """POST /api/kb/answers：{"question", "answer", "session_id"?, "seq"?}。同一个问题（规整后）再存就是更新。
    带了 session_id 和 seq：记下是纠正了哪条回答，并给那条回答记一条 rating=down 的反馈（它还没有反馈时）。
    返回 (那一条, 是不是新建的)。"""
    if not isinstance(body, dict):
        raise FeedbackError("请求内容应该是 JSON 对象")
    q = _clean(body.get("question"), "问题", Q_MAX)
    a = _clean(body.get("answer"), "答案", A_MAX)
    store = rt.store
    src = None
    sid, seq = body.get("session_id"), body.get("seq")
    if sid is not None or seq is not None:
        if not isinstance(sid, str) or not store.session(sid):
            raise FeedbackError("没有这个对话：%s" % sid)
        if _assistant_event(store, sid, seq) is None:
            raise FeedbackError("seq 应该是这个对话里一条助手回答的事件序号")
        src = {"session_id": sid, "seq": seq}
    svc = kbm.get(rt)
    ensure_library(store)
    key = _key(q)
    old = store.one("SELECT * FROM kb_docs WHERE library_id=? AND sha=?", (kbm.ANSWERS_ID, key))
    t = now()
    if old:
        meta = _meta(old)
        meta.update(question=q, answer=a, updated=t, key=key)
        if src:
            meta["source"] = src
        _write(svc, old["id"], q, a, meta, new=False)
        did, created = old["id"], False
    else:
        did = new_id("qa_")
        meta = {"question": q, "answer": a, "created": t, "updated": t, "key": key}
        if src:
            meta["source"] = src
        _write(svc, did, q, a, meta, new=True)
        created = True
    if src and src["seq"] not in current(store, sid):
        rt.emit(sid, "feedback", {"seq": src["seq"], "rating": "down", "note": ""})
    try:                                   # 对应的"没答上来的问题"标成 done（docs/借鉴开源项目.md 4.1）
        from .kb import ops
        ops.answer_saved(store, q, did, sid, seq)
    except Exception:
        import traceback
        traceback.print_exc()
    return get_answer(store, did), created


def update_answer(rt, aid, body):
    """PATCH /api/kb/answers/<id> {"question"?, "answer"?}。改成的问题和另一条重了 → 400。"""
    store = rt.store
    d = store.one("SELECT * FROM kb_docs WHERE id=? AND library_id=?", (aid, kbm.ANSWERS_ID))
    if not d:
        raise KeyError("没有这条标准答案")
    if not isinstance(body, dict) or ("question" not in body and "answer" not in body):
        raise FeedbackError("要给 question 或 answer")
    meta = _meta(d)
    q = _clean(body["question"], "问题", Q_MAX) if "question" in body else meta.get("question") or ""
    a = _clean(body["answer"], "答案", A_MAX) if "answer" in body else meta.get("answer") or ""
    key = _key(q)
    other = store.one("SELECT id FROM kb_docs WHERE library_id=? AND sha=? AND id<>?", (kbm.ANSWERS_ID, key, aid))
    if other:
        raise FeedbackError("已经有一条同样问题的标准答案了")
    meta.update(question=q, answer=a, updated=now(), key=key)
    _write(kbm.get(rt), aid, q, a, meta, new=False)
    return get_answer(store, aid)


def delete_answer(rt, aid):
    store = rt.store
    d = store.one("SELECT id FROM kb_docs WHERE id=? AND library_id=?", (aid, kbm.ANSWERS_ID))
    if not d:
        raise KeyError("没有这条标准答案")
    svc = kbm.get(rt)
    with svc.indexer.ingest_lock:
        try:
            svc.vectors().delete_docs([aid])
        except Exception:
            pass
        with store.tx():
            kbm.delete_doc_rows(store, [aid])
            store.x("UPDATE kb_libraries SET updated=? WHERE id=?", (now(), kbm.ANSWERS_ID))
