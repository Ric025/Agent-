"""资料运营（docs/借鉴开源项目.md 第 4 节）：没答上来的问题、标准答案的命中次数、检索评测。全部由程序算，不调模型。

没答上来的问题（kb_gaps）：
- no_hit：Hspace 的主对话里，一次运行结束，回答里没有出处，而且这次运行里检索过、却一条强命中都没有
  （规格写的是"检索没有一条强命中"；这次运行一次都没检索时不记，免得"你好""谢谢"也算进来）。
- down：用户对一条回答点了"不对"。问题取它前面那句用户发言。
- 去重：问题规范化（去标点、空白、问句词）后相同，或者互相的覆盖率（按字算）都在 0.8 以上，算同一个问题：count 加一、
  last_ts 更新（session_id、seq 记最近一次的；状态不变：标了"不用管"的不会因为又被问到而重新打开）。
- 记成了标准答案（来自那次对话的那条回答，或者问题一样）：标成 done，answer_id 指过去。

标准答案的命中次数：一条标准答案被检索到、并且最终回答引用了它（核对数字那一步的 cited 里有它的编号），hits 加一。

检索评测（kb_eval、kb_eval_runs）：只测检索，不用模型。hitN = 前 N 条里有应命中的文件的题数。
文件名的比较：去扩展名、去首尾空白、不分大小写后，应命中的名字包含在实际命中的文件名里就算对。"""
from __future__ import annotations
import csv, io, json, re, time, unicodedata

from ..store import new_id, now

REASONS = ("no_hit", "down")
STATUSES = ("open", "done", "ignored")
REASON_TEXT = {"no_hit": "资料里没找到", "down": "回答被标了不对"}
STATUS_TEXT = {"open": "待处理", "done": "已解决", "ignored": "不用管"}
SAME = 0.8
Q_MAX = 300
NOTE_MAX = 500
EVAL_KEEP = 20
EVAL_MODES = ("auto", "keyword", "hybrid")
_QW = ("有没有", "是不是", "什么样", "怎么样", "为什么", "什么", "怎么", "多少", "如何", "哪些", "哪个", "哪里", "哪儿", "是否",
       "请问", "一下", "能否", "能不能", "可以", "吗", "呢", "吧", "啊", "呀")
_QW_RE = re.compile("|".join(sorted(_QW, key=len, reverse=True)))


# ---------------- 问题的规范化和去重 ----------------

def norm_question(q):
    s = unicodedata.normalize("NFKC", q or "").lower()
    s = _QW_RE.sub("", s)
    s = re.sub(r"[\W_]+", "", s)
    return s


def _units(s):
    """覆盖率按字算（英文和数字按词），和检索的覆盖率一个口径。"""
    return set(re.findall(r"[a-z0-9]+|[^\sa-z0-9]", s or ""))


def same_question(a, b):
    """两个规范化后的问题算不算同一个：相同，或者互相的覆盖率（按字）都在 0.8 以上。
    例："公司给员工养宠物补贴吗"和"公司对员工养宠物有补贴吗？"→ 0.9 和 0.82，算同一个。"""
    if not a or not b:
        return False
    if a == b:
        return True
    ua, ub = _units(a), _units(b)
    if not ua or not ub:
        return False
    inter = len(ua & ub)
    return inter / len(ua) >= SAME and inter / len(ub) >= SAME


def _public_gap(r):
    return {k: r.get(k) for k in ("id", "question", "reason", "count", "first_ts", "last_ts", "session_id", "seq", "status",
                                  "note", "answer_id")}


def record_gap(store, question, reason, sid=None, seq=None):
    """记一条（或给已有的同一个问题加一次）。返回那一条。"""
    q = " ".join((question or "").split())[:Q_MAX]
    n = norm_question(q)
    if not n or reason not in REASONS:
        return None
    t = now()
    with store.tx():
        rows = store.q("SELECT * FROM kb_gaps ORDER BY last_ts DESC")
        hit = next((r for r in rows if same_question(r.get("norm") or "", n)), None)
        if hit:
            reason2 = "down" if "down" in (reason, hit.get("reason")) else reason     # 被点了"不对"的更要紧，留着它
            store.x("UPDATE kb_gaps SET count=count+1, last_ts=?, session_id=?, seq=?, reason=? WHERE id=?",
                    (t, sid, seq, reason2, hit["id"]))
            gid = hit["id"]
        else:
            gid = new_id("g_")
            store.x("INSERT INTO kb_gaps(id,question,norm,reason,count,first_ts,last_ts,session_id,seq,status,note,answer_id) "
                    "VALUES(?,?,?,?,1,?,?,?,?,'open','',NULL)", (gid, q, n, reason, t, t, sid, seq))
    return _public_gap(store.one("SELECT * FROM kb_gaps WHERE id=?", (gid,)))


def gaps(store, status=None):
    if status is not None and status not in STATUSES:
        raise ValueError("status 只能是 %s" % "、".join(STATUSES))
    sql, args = "SELECT * FROM kb_gaps", ()
    if status:
        sql, args = sql + " WHERE status=?", (status,)
    return [_public_gap(r) for r in store.q(sql + " ORDER BY count DESC, last_ts DESC", args)]


def gap(store, gid):
    r = store.one("SELECT * FROM kb_gaps WHERE id=?", (gid,))
    if not r:
        raise KeyError("没有这一条")
    return r


def patch_gap(store, gid, body):
    gap(store, gid)
    if not isinstance(body, dict) or ("status" not in body and "note" not in body):
        raise ValueError("要给 status 或 note")
    fields = {}
    if "status" in body:
        if body["status"] not in STATUSES:
            raise ValueError("status 只能是 %s" % "、".join(STATUSES))
        fields["status"] = body["status"]
    if "note" in body:
        v = body["note"]
        if v is None:
            v = ""
        if not isinstance(v, str):
            raise ValueError("note 应该是文字")
        if len(v) > NOTE_MAX:
            raise ValueError("note 最多 %d 个字" % NOTE_MAX)
        fields["note"] = v.strip()
    store.x("UPDATE kb_gaps SET %s WHERE id=?" % ",".join("%s=?" % k for k in fields), tuple(fields.values()) + (gid,))
    return _public_gap(gap(store, gid))


def answer_gap(rt, gid, body):
    """POST /api/kb/gaps/<id>/answer {answer} → 建一条标准答案（问题就是这一条的问题），这一条标成 done。返回那条标准答案。"""
    from .. import feedback
    g = gap(rt.store, gid)
    if not isinstance(body, dict):
        raise ValueError("请求内容应该是 JSON 对象")
    item, _ = feedback.save_answer(rt, {"question": g["question"], "answer": body.get("answer")})
    rt.store.x("UPDATE kb_gaps SET status='done', answer_id=? WHERE id=?", (item["id"], gid))
    return item


def answer_saved(store, question, answer_id, sid=None, seq=None):
    """存了一条标准答案之后：来自同一次对话的同一条回答、或者问题一样的"没答上来"，标成 done。"""
    n = norm_question(question)
    for r in store.q("SELECT id, norm, session_id, seq FROM kb_gaps WHERE status<>'done'"):
        if (sid and r["session_id"] == sid and seq is not None and r["seq"] == seq) or same_question(r["norm"] or "", n):
            store.x("UPDATE kb_gaps SET status='done', answer_id=? WHERE id=?", (answer_id, r["id"]))


def gap_count(store):
    return {"open": int(store.one("SELECT COUNT(*) AS n FROM kb_gaps WHERE status='open'")["n"] or 0)}


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""


def gaps_csv(store):
    """导出：UTF-8 带 BOM（Excel 能直接开）。列：问题、原因、次数、第一次、最近一次、状态、备注。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["问题", "原因", "次数", "第一次", "最近一次", "状态", "备注"])
    for g in gaps(store):
        w.writerow([g["question"], REASON_TEXT.get(g["reason"], g["reason"]), g["count"], _ts(g["first_ts"]),
                    _ts(g["last_ts"]), STATUS_TEXT.get(g["status"], g["status"]), g["note"] or ""])
    return ("﻿" + buf.getvalue()).encode("utf-8")


# ---------------- 一次回答之后（grounding.after_answer 调） ----------------

def _user_before(store, sid, seq):
    r = store.one("SELECT payload FROM events WHERE session_id=? AND type='user_message' AND seq<? ORDER BY seq DESC LIMIT 1",
                  (sid, seq))
    if not r:
        return ""
    try:
        return (json.loads(r["payload"]) or {}).get("text") or ""
    except ValueError:
        return ""


def _run_searches(store, sid, seq):
    """这次运行（上一句用户发言之后到这条回答）里 kb_search 的结果 payload。"""
    u = store.one("SELECT MAX(seq) AS s FROM events WHERE session_id=? AND type='user_message' AND seq<?", (sid, seq))
    start = (u or {}).get("s") or 0
    out = []
    for r in store.q("SELECT payload FROM events WHERE session_id=? AND type='tool_result' AND seq>? AND seq<? ORDER BY seq",
                     (sid, start, seq)):
        try:
            p = json.loads(r["payload"])
        except ValueError:
            continue
        if p.get("tool") == "kb_search":
            out.append(p)
    return out


def after_answer(rt, ev, res, parts, mode):
    """回答写进账本、核对完之后：标准答案的命中次数；Hspace 里"没答上来"的记一笔。出错不影响回答（调用方吞掉）。"""
    store = rt.store
    sid, seq = ev.get("session_id"), ev.get("seq")
    seen = set()
    for n in res.get("cited") or []:
        h = parts.get(n) or {}
        did = h.get("doc_id")
        if h.get("standard") and did and did not in seen:
            seen.add(did)
            store.x("UPDATE kb_docs SET hits=COALESCE(hits,0)+1, last_hit=? WHERE id=?", (now(), did))
    from .. import modes
    if modes.norm(mode) != modes.HSPACE or res.get("cited"):
        return
    searches = _run_searches(store, sid, seq)
    if not searches or any(p.get("hits") for p in searches):
        return
    q = _user_before(store, sid, seq)
    if q.strip():
        record_gap(store, q, "no_hit", sid, seq)


def after_feedback(rt, sid, payload):
    """点了"不对"：问题取那条回答前面的那句用户发言，记一条 down。只记 Hspace 的主对话。"""
    if payload.get("rating") != "down":
        return None
    store = rt.store
    s = store.session(sid)
    from .. import modes
    if not s or s.get("kind") == "sub" or modes.norm(s.get("mode")) != modes.HSPACE:
        return None
    q = _user_before(store, sid, payload.get("seq"))
    if not q.strip():
        return None
    return record_gap(store, q, "down", sid, payload.get("seq"))


# ---------------- 检索评测 ----------------

def _norm_name(x):
    import os
    s = unicodedata.normalize("NFKC", str(x or "")).strip()
    s = os.path.splitext(s)[0] if os.path.splitext(s)[1].lower() in (".docx", ".pdf", ".pptx", ".xlsx", ".xlsm", ".csv",
                                                                     ".txt", ".md") else s
    return " ".join(s.split()).lower()


def name_match(expect, title):
    e, t = _norm_name(expect), _norm_name(title)
    return bool(e) and e in t


def _clean_expect(v):
    if isinstance(v, str):
        v = re.split(r"[;；\n]", v)
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError("expect 应该是文件名的列表")
    out = []
    for x in v:
        x = x.strip()
        if x and x not in out:
            out.append(x[:200])
    if not out:
        raise ValueError("要写应该命中的文件（一个或几个）")
    return out[:10]


def _eval_item(r):
    try:
        exp = json.loads(r["expect"] or "[]")
    except ValueError:
        exp = []
    return {"id": r["id"], "question": r["question"], "expect": exp, "note": r.get("note") or ""}


def _run_row(r, detail=False):
    out = {k: r.get(k) for k in ("id", "ts", "mode", "total", "hit1", "hit3", "hit5", "ms")}
    if detail:
        try:
            out["detail"] = json.loads(r.get("detail") or "{}")
        except ValueError:
            out["detail"] = {}
    return out


def eval_state(store):
    items = [_eval_item(r) for r in store.q("SELECT * FROM kb_eval ORDER BY created")]
    runs = [_run_row(r) for r in store.q("SELECT * FROM kb_eval_runs ORDER BY ts DESC LIMIT ?", (EVAL_KEEP,))]
    return {"items": items, "runs": runs}


def eval_add(store, body):
    if not isinstance(body, dict):
        raise ValueError("请求内容应该是 JSON 对象")
    q = body.get("question")
    if not isinstance(q, str) or not q.strip():
        raise ValueError("要写问题")
    q = " ".join(q.split())[:Q_MAX]
    exp = _clean_expect(body.get("expect"))
    note = body.get("note") or ""
    if not isinstance(note, str):
        raise ValueError("note 应该是文字")
    n = norm_question(q)
    for r in store.q("SELECT id, question FROM kb_eval"):
        if norm_question(r["question"]) == n:
            raise ValueError("已经有这一题了：%s" % r["question"])
    eid = new_id("ev_")
    store.x("INSERT INTO kb_eval(id,question,expect,note,created) VALUES(?,?,?,?,?)",
            (eid, q, json.dumps(exp, ensure_ascii=False), note.strip()[:NOTE_MAX], now()))
    return _eval_item(store.one("SELECT * FROM kb_eval WHERE id=?", (eid,)))


def eval_delete(store, eid):
    if not store.one("SELECT id FROM kb_eval WHERE id=?", (eid,)):
        raise KeyError("没有这一题")
    store.x("DELETE FROM kb_eval WHERE id=?", (eid,))


def table_rows(name, data):
    """上传的 CSV 或 xlsx → [[单元格文字]]。"""
    ext = (name or "").lower().rsplit(".", 1)[-1] if "." in (name or "") else ""
    if ext in ("xlsx", "xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            ws = wb.worksheets[0]
            return [["" if v is None else str(v).strip() for v in row] for row in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("读不出这个文件：CSV 要用 UTF-8 或 GBK 编码")
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text))]


def eval_import(store, rows):
    """两列"问题""应命中的文件"（一格里几个文件用分号隔开）。有表头就按表头找列，没有就取前两列。
    返回 {"added", "skipped", "reasons": [{"row", "why"}]}。"""
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    added, reasons = 0, []
    if not rows:
        return {"added": 0, "skipped": 0, "reasons": []}
    head = [(c or "").strip() for c in rows[0]]
    qi, fi, start = 0, 1, 0
    if any(h in ("问题", "question") for h in head):
        qi = next(i for i, h in enumerate(head) if h in ("问题", "question"))
        fi = next((i for i, h in enumerate(head) if "文件" in h or h in ("expect", "file", "files")), 1 if qi != 1 else 0)
        start = 1
    for i, r in enumerate(rows[start:], start + 1):
        q = r[qi] if qi < len(r) else ""
        f = r[fi] if fi < len(r) else ""
        try:
            eval_add(store, {"question": q, "expect": f})
            added += 1
        except ValueError as e:
            reasons.append({"row": i, "why": str(e)})
    return {"added": added, "skipped": len(reasons), "reasons": reasons[:50]}


def eval_seed(store):
    """从点过"有用"的回答里生成：问题是那条回答前面的用户发言，应命中的是它引用的文件（标准答案不算）。已有的不重复加。"""
    from .. import feedback, grounding
    from . import cited_parts
    added = 0
    have = {norm_question(r["question"]) for r in store.q("SELECT question FROM kb_eval")}
    for s in store.q("SELECT DISTINCT session_id FROM events WHERE type='feedback'"):
        sid = s["session_id"]
        fb = feedback.current(store, sid)
        ups = [seq for seq, v in fb.items() if v.get("rating") == "up"]
        if not ups:
            continue
        gr = grounding.latest(store, sid)
        for seq in sorted(ups):
            q = _user_before(store, sid, seq)
            n = norm_question(q)
            if not n or n in have:
                continue
            g = gr.get(seq) or {}
            parts = cited_parts(store, sid, upto_seq=seq)
            files = []
            for c in g.get("cited") or []:
                h = parts.get(c) or {}
                if h.get("standard") or not h.get("title"):
                    continue
                name = _strip_ext(h["title"])
                if name not in files:
                    files.append(name)
            if not files:
                continue
            store.x("INSERT INTO kb_eval(id,question,expect,note,created) VALUES(?,?,?,?,?)",
                    (new_id("ev_"), " ".join(q.split())[:Q_MAX], json.dumps(files, ensure_ascii=False), "从点过有用的回答生成", now()))
            have.add(n)
            added += 1
    return {"added": added}


def _strip_ext(t):
    import os
    return os.path.splitext(t or "")[0] or (t or "")


def eval_run(svc, mode="auto", context_window=32768):
    """跑一遍：每题按 top 5 检索（不带标准答案库），看前 1、3、5 条里有没有应命中的文件。存进 kb_eval_runs，只留最近 20 次。"""
    from . import retrieve, ANSWERS_ID
    store = svc.store
    mode = mode or "auto"
    if mode not in EVAL_MODES:
        raise ValueError("mode 只能是 auto、keyword 或 hybrid")
    sem_ok = svc.semantic()[0]
    if mode == "hybrid" and not sem_ok:
        raise ValueError("现在没有语义检索（没配向量模型或连不上），不能用 hybrid。用 auto 或 keyword。")
    semantic = sem_ok if mode in ("auto", "hybrid") else False
    libs = [r["id"] for r in store.q("SELECT id FROM kb_libraries ORDER BY created") if r["id"] != ANSWERS_ID]
    items = [_eval_item(r) for r in store.q("SELECT * FROM kb_eval ORDER BY created")]
    t0 = time.time()
    h1 = h3 = h5 = 0
    misses, detail = [], []
    for it in items:
        res = retrieve.search(svc, [it["question"]], libs, top_k=5, context_window=context_window, semantic=semantic)
        got = []
        for h in res["hits"]:
            name = _strip_ext(h.get("title"))
            got.append(name)
        rank = next((i + 1 for i, g in enumerate(got) if any(name_match(e, g) for e in it["expect"])), None)
        h1 += 1 if rank is not None and rank <= 1 else 0
        h3 += 1 if rank is not None and rank <= 3 else 0
        h5 += 1 if rank is not None and rank <= 5 else 0
        detail.append({"id": it["id"], "question": it["question"], "expect": it["expect"], "got": got, "rank": rank})
        if rank is None:
            misses.append({"question": it["question"], "expect": it["expect"], "got": got})
    ms = int((time.time() - t0) * 1000)
    used = "hybrid" if semantic else "keyword"
    rid = new_id("er_")
    with store.tx():
        store.x("INSERT INTO kb_eval_runs(id,ts,mode,total,hit1,hit3,hit5,ms,detail) VALUES(?,?,?,?,?,?,?,?,?)",
                (rid, now(), used, len(items), h1, h3, h5, ms, json.dumps({"asked": mode, "items": detail}, ensure_ascii=False)))
        old = [r["id"] for r in store.q("SELECT id FROM kb_eval_runs ORDER BY ts DESC")][EVAL_KEEP:]
        for x in old:
            store.x("DELETE FROM kb_eval_runs WHERE id=?", (x,))
    return {"id": rid, "mode": used, "total": len(items), "hit1": h1, "hit3": h3, "hit5": h5, "ms": ms, "misses": misses}
