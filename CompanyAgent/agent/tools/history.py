"""历史回忆：在本工作区以前的任务里查找说过的话（msg_fts 全文索引）。

只有任务（kind='task'）里用户的话和助手的答复进索引，工具结果不进。
检索的做法同 kb_search：3 个字符以上的词走 FTS5，短词用 instr。"""
from __future__ import annotations
import time
from . import tool, S, ToolError

MAX_SESSIONS = 5
MAX_MATCHES = 3
SNIPPET = 240
AROUND = 3
MSG_CHARS = 600
ROLE = {"user": "用户", "assistant": "助手"}
NONE_NOTE = "没有找到。可以换个说法再查一次，或者直接问用户。"


def _date(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts or 0))


def _cut(text, n):
    text = text or ""
    return text if len(text) <= n else text[:n - 1] + "…"


def _snippet(text, words):
    """以第一个命中处为中心取一段，总长不超过 SNIPPET 个字符。"""
    low = text.lower()
    pos = [low.find(w.lower()) for w in words if low.find(w.lower()) >= 0]
    if len(text) <= SNIPPET:
        return text
    p = min(pos) if pos else 0
    start = max(0, min(p - SNIPPET // 2, len(text) - SNIPPET))
    out = text[start:start + SNIPPET]
    if start > 0:
        out = "…" + out[1:]
    if start + SNIPPET < len(text):
        out = out[:-1] + "…"
    return out


def search(store, ws_id, query, exclude=None, session_id=None, max_sessions=MAX_SESSIONS, mode=None):
    """mode：只在这个模式的对话里找（docs/两种模式.md 1.3；不给就不限）。返回 hits 列表：[{session_id, title, date, matches: [{seq, role, snippet}]}]，按任务最近更新时间倒序。"""
    words = [w for w in (query or "").split() if w]
    if not words:
        return []
    long_ = [w for w in words if len(w) >= 3] if store.trigram else []
    short = [w for w in words if w not in long_]
    sql = "SELECT text, session_id, seq, role FROM msg_fts WHERE workspace_id=?"
    args = [ws_id]
    if long_:
        sql += " AND msg_fts MATCH ?"
        args.append(" AND ".join('"%s"' % w.replace('"', '""') for w in long_))
    for w in short:
        # 用 instr 而不是 LIKE：trigram 表上的 LIKE 会走索引，SQLite 3.37 等版本对不足三个字的词直接返回空
        sql += " AND instr(lower(text), lower(?)) > 0"
        args.append(w)
    if session_id:
        sql += " AND session_id=?"
        args.append(session_id)
    elif exclude:
        sql += " AND session_id<>?"
        args.append(exclude)
    sql += " LIMIT 2000"
    try:
        rows = store.q(sql, tuple(args))
    except Exception:
        rows = []
    by = {}
    for r in rows:
        by.setdefault(r["session_id"], []).append(r)
    sessions = []
    for sid, rs in by.items():
        s = store.one("SELECT id, title, updated, kind, workspace_id, mode FROM sessions WHERE id=?", (sid,))
        if not s or s["kind"] != "task" or s["workspace_id"] != ws_id:
            continue
        if mode and (s.get("mode") or "cariai") != mode:
            continue
        sessions.append((s, rs))
    sessions.sort(key=lambda x: -(x[0]["updated"] or 0))
    hits = []
    for s, rs in sessions[:max_sessions]:
        rs = sorted(rs, key=lambda r: int(r["seq"]))[-MAX_MATCHES:]      # 每个任务取最近的几条
        hits.append({"session_id": s["id"], "title": s["title"] or "（无标题）", "date": _date(s["updated"]),
                     "matches": [{"seq": int(r["seq"]), "role": ROLE.get(r["role"], r["role"]), "snippet": _snippet(r["text"], words)}
                                 for r in rs]})
    return hits


def _messages(store, sid):
    """这个任务里用户的话和有内容的助手答复，按顺序。"""
    out = []
    for e in store.events(sid, types=("user_message", "assistant_message")):
        p = e["payload"]
        text = p.get("text") if e["type"] == "user_message" else p.get("content")
        if isinstance(text, str) and text.strip():
            out.append({"seq": e["seq"], "role": "用户" if e["type"] == "user_message" else "助手", "text": text})
    return out


def around_of(store, s, seq):
    msgs = _messages(store, s["id"])
    idx = next((i for i, m in enumerate(msgs) if m["seq"] == seq), None)
    if idx is None:
        raise ToolError("这个任务里没有 seq=%s 的发言。around 要填 session_search 结果里某条命中的 seq。" % seq)
    part = msgs[max(0, idx - AROUND): idx + AROUND + 1]
    return {"session_id": s["id"], "title": s["title"] or "（无标题）", "date": _date(s["updated"]),
            "messages": [dict(m, text=_cut(m["text"], MSG_CHARS)) for m in part]}


def overview(store, s):
    msgs = _messages(store, s["id"])
    first = next((m for m in msgs if m["role"] == "用户"), None)
    last = next((m for m in reversed(msgs) if m["role"] == "助手"), None)
    arts = []
    for r in store.q("SELECT path FROM artifacts WHERE session_id=? ORDER BY created", (s["id"],)):
        if r["path"] not in arts:
            arts.append(r["path"])
    return {"session_id": s["id"], "title": s["title"] or "（无标题）", "date": _date(s["updated"]),
            "first_user_message": _cut(first["text"], MSG_CHARS) if first else "",
            "last_assistant_message": _cut(last["text"], MSG_CHARS) if last else "", "artifacts": arts}


@tool("session_search", "在本工作区以前的任务里查找说过的话和做过的事。用户提到以前的工作，而当前对话里没有相关内容时使用",
      {"query": S("要找的关键词，几个词即可"),
       "session_id": S("可选：只看这一个任务"),
       "around": {"type": "integer", "description": "可选，和 session_id 一起用：某条命中的 seq，返回它前后的对话"}},
      required=[], risk="read", sub_ok=True)
def session_search(ctx, query=None, session_id=None, around=None):
    store, ws_id = ctx.store, ctx.ws["id"]
    query = (query or "").strip()
    if session_id:
        s = store.one("SELECT id, title, updated, kind, workspace_id FROM sessions WHERE id=?", (session_id,))
        if not s or s["workspace_id"] != ws_id or s["kind"] != "task":
            raise ToolError("本工作区没有这个任务：%s。session_id 要用 session_search 结果里的值。" % session_id)
        if around is not None:
            return around_of(store, s, int(around))
        if not query:
            return overview(store, s)
    elif around is not None:
        raise ToolError("around 要和 session_id 一起用：先用 query 查到命中，再把那条的 session_id 和 seq 填进来。")
    if not query:
        raise ToolError("没有给查找条件。用法：query 填几个关键词；或者 session_id 看某个任务的概况；"
                        "或者 session_id 加 around（某条命中的 seq）看那条前后的对话。")
    exclude = ctx.sid
    if ctx.is_sub:
        me = store.one("SELECT parent_id FROM sessions WHERE id=?", (ctx.sid,))
        exclude = (me or {}).get("parent_id") or ctx.sid
    from .. import modes
    hits = search(store, ws_id, query, exclude=None if session_id else exclude, session_id=session_id,
                  mode=None if session_id else modes.of_ctx(ctx))
    if not hits:
        return {"hits": [], "note": NONE_NOTE}
    return {"hits": hits}
