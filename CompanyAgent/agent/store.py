"""账本：一个 SQLite 库，按顺序记下发生过的每件事。它是唯一的事实来源。"""
from __future__ import annotations
import json, os, sqlite3, threading, time, uuid, hashlib

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY, name TEXT, path TEXT, instructions TEXT DEFAULT '',
  egress TEXT DEFAULT 'intranet', created REAL);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, workspace_id TEXT, title TEXT, status TEXT, revision INTEGER DEFAULT 1,
  kind TEXT DEFAULT 'task', parent_id TEXT, meta TEXT DEFAULT '{}', created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, revision INTEGER, type TEXT,
  payload TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS ix_events_session ON events(session_id, seq);
CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, session_id TEXT, revision INTEGER, call_id TEXT, tool TEXT, args TEXT,
  digest TEXT, risk TEXT, state TEXT, result TEXT, created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS ix_actions_session ON actions(session_id);
CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, session_id TEXT, workspace_id TEXT, path TEXT, version INTEGER,
  hash TEXT, size INTEGER, note TEXT, backup TEXT, created REAL);
CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY, workspace_id TEXT, text TEXT, source TEXT, created REAL);
CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY, workspace_id TEXT, prompt TEXT, spec TEXT, next_run REAL,
  last_run REAL, enabled INTEGER DEFAULT 1, created REAL);
CREATE TABLE IF NOT EXISTS tool_settings(tool TEXT PRIMARY KEY, setting TEXT);
CREATE TABLE IF NOT EXISTS kb_docs(id TEXT PRIMARY KEY, workspace_id TEXT, path TEXT, title TEXT, mtime REAL, chunks INTEGER,
  added REAL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, session_id TEXT, action_id TEXT, tool TEXT,
  digest TEXT, risk TEXT, decision TEXT, outcome TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS memory_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, memory_id TEXT, workspace_id TEXT,
  op TEXT, before TEXT, after TEXT, origin TEXT, session_id TEXT, undone INTEGER DEFAULT 0, entry TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS kb_libraries(id TEXT PRIMARY KEY, name TEXT, note TEXT DEFAULT '', created REAL, updated REAL,
  workspace_id TEXT, synced REAL);
CREATE TABLE IF NOT EXISTS kb_sources(id TEXT PRIMARY KEY, library_id TEXT, path TEXT, kind TEXT, added REAL);
CREATE TABLE IF NOT EXISTS kb_sections(id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT, library_id TEXT, seq INTEGER,
  loc TEXT, heading TEXT, text TEXT, tokens INTEGER);
CREATE INDEX IF NOT EXISTS ix_kb_sections_doc ON kb_sections(doc_id, seq);
CREATE TABLE IF NOT EXISTS kb_parts(id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT, library_id TEXT, seq INTEGER,
  parent INTEGER, loc TEXT, heading TEXT, text TEXT, tokens INTEGER);
CREATE INDEX IF NOT EXISTS ix_kb_parts_doc ON kb_parts(doc_id, seq);
CREATE INDEX IF NOT EXISTS ix_kb_parts_lib ON kb_parts(library_id);
CREATE TABLE IF NOT EXISTS kb_vectors(part_id INTEGER PRIMARY KEY, library_id TEXT, doc_id TEXT, dims INTEGER, vec BLOB);
CREATE INDEX IF NOT EXISTS ix_kb_vectors_lib ON kb_vectors(library_id);
"""
FTS = "CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks USING fts5(text, doc_id UNINDEXED, loc UNINDEXED, workspace_id UNINDEXED, tokenize='trigram')"
FTS_FALLBACK = "CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks USING fts5(text, doc_id UNINDEXED, loc UNINDEXED, workspace_id UNINDEXED)"
# 资料片段的全文索引（docs/两种模式.md 2.1）：rowid 就是 kb_parts.id，part_id 另存一份方便读。
# 正文和标题路径分两列：检索时标题的权重低一些（不然一节下面的每个小片段都因为标题而排在正文真正命中的片段前面）
KB_FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(text, heading, part_id UNINDEXED, library_id UNINDEXED%s)")
# 历史发言的全文索引：只收任务（kind='task'）里用户的话和助手的答复，工具结果不进
MSG_FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(text, session_id UNINDEXED, workspace_id UNINDEXED, "
           "seq UNINDEXED, role UNINDEXED, ts UNINDEXED%s)")
INDEXED = {"user_message": ("user", "text"), "assistant_message": ("assistant", "content")}

# 动作状态：proposed 已提议 → awaiting 等批准 → approved 已批准 → running 执行中 → done/failed
#          denied 被拒绝 · voided 因改指令作废 · unknown 执行中途中断，结果未知


def now():
    return time.time()


def new_id(prefix=""):
    return prefix + uuid.uuid4().hex[:12]


def digest_of(tool, args, revision):
    raw = json.dumps([tool, args, revision], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class Store:
    def __init__(self, home):
        self.path = os.path.join(home, "ledger.db")
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.trigram = True
        try:
            self.db.execute(FTS)
        except sqlite3.OperationalError:
            self.trigram = False
            self.db.execute(FTS_FALLBACK)
        self.db.execute(MSG_FTS % (", tokenize='trigram'" if self.trigram else ""))
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(kb_fts)").fetchall()]
        if cols and "heading" not in cols:
            self.db.execute("DROP TABLE kb_fts")       # 早一版只有 text 一列：重建，再从 kb_parts 补上
        self.db.execute(KB_FTS % (", tokenize='trigram'" if self.trigram else ""))
        if cols and "heading" not in cols:
            from .kb.ingest import fts_columns
            for r in self.db.execute("SELECT id, text, heading, library_id FROM kb_parts").fetchall():
                self.db.execute("INSERT INTO kb_fts(rowid,text,heading,part_id,library_id) VALUES(?,?,?,?,?)",
                                (r[0],) + fts_columns({"text": r[1], "heading": r[2]}) + (r[0], r[3]))
        self._migrate()
        self._migrate_kb()
        self._backfill_fts()

    # ---- 启动时的迁移（幂等） ----
    def _migrate(self):
        with self.lock:
            cols = {r["name"] for r in self.q("PRAGMA table_info(memories)")}
            for col, typ in (("updated", "REAL"), ("origin", "TEXT"), ("session_id", "TEXT")):
                if col not in cols:
                    self.db.execute("ALTER TABLE memories ADD COLUMN %s %s" % (col, typ))
            # 旧数据回填：source 以 s_ 开头的是对话里记下的（source 是任务 id），其余算用户自己写的
            self.db.execute("UPDATE memories SET updated=created WHERE updated IS NULL")
            self.db.execute("UPDATE memories SET origin='chat', session_id=source WHERE origin IS NULL AND source LIKE 's\\_%' ESCAPE '\\'")
            self.db.execute("UPDATE memories SET origin='user' WHERE origin IS NULL")
            lcols = {r["name"] for r in self.q("PRAGMA table_info(memory_log)")}
            if "entry" not in lcols:
                self.db.execute("ALTER TABLE memory_log ADD COLUMN entry TEXT")
            # 默认空间（docs/聊天为主的界面.md 1.1）
            wcols = {r["name"] for r in self.q("PRAGMA table_info(workspaces)")}
            if "is_default" not in wcols:
                self.db.execute("ALTER TABLE workspaces ADD COLUMN is_default INTEGER DEFAULT 0")
            # 两种模式（docs/两种模式.md 1.3）：老对话没有这一列，加列时的默认值让它们都算 CariAi
            scols = {r["name"] for r in self.q("PRAGMA table_info(sessions)")}
            if "mode" not in scols:
                self.db.execute("ALTER TABLE sessions ADD COLUMN mode TEXT DEFAULT 'cariai'")
            # 独立的资料库（2.1）：kb_docs 多几列
            dcols = {r["name"] for r in self.q("PRAGMA table_info(kb_docs)")}
            for col, typ in (("library_id", "TEXT"), ("source_id", "TEXT"), ("size", "INTEGER"), ("sha", "TEXT"),
                             ("status", "TEXT DEFAULT 'ok'"), ("error", "TEXT DEFAULT ''"), ("kind", "TEXT"),
                             ("tokens", "INTEGER DEFAULT 0"), ("meta", "TEXT DEFAULT '{}'"),
                             ("family", "TEXT DEFAULT ''"), ("version_label", "TEXT DEFAULT ''"), ("effective", "REAL")):
                if col not in dcols:
                    self.db.execute("ALTER TABLE kb_docs ADD COLUMN %s %s" % (col, typ))
            self.db.execute("CREATE INDEX IF NOT EXISTS ix_kb_docs_lib ON kb_docs(library_id)")
            self._migrate_round10(dcols)
            self._migrate_shared()

    def _migrate_shared(self):
        """docs/借鉴开源项目.md 第 5 节（公司共享目录）：kb_libraries 多几列。幂等。
        - shared：1 = 从共享目录导入的库（只读：不能加来源、不能重新收录、不能删，可以停用）
        - version：共享库是导入的版本；自己的库是上一次导出的版本（0 = 没导出过）
        - enabled：0 = 停用（不参与检索）
        - source_root：共享库的原文件所在的根（打开原文件时用它加文档的相对路径）
        - built：索引包是什么时候建的（manifest 的 built）
        - shared_info：导入时记下的其他东西（JSON：向量用没用上、签名、来源文件夹名……）"""
        lcols = {r["name"] for r in self.q("PRAGMA table_info(kb_libraries)")}
        for col, typ in (("shared", "INTEGER DEFAULT 0"), ("version", "INTEGER DEFAULT 0"), ("enabled", "INTEGER DEFAULT 1"),
                         ("source_root", "TEXT DEFAULT ''"), ("built", "REAL"), ("shared_info", "TEXT DEFAULT '{}'")):
            if col not in lcols:
                self.db.execute("ALTER TABLE kb_libraries ADD COLUMN %s %s" % (col, typ))

    def _migrate_round10(self, dcols):
        """docs/借鉴开源项目.md 第 1、2、4 节：文档卡片、标签、语言；没答上来的问题；标准答案的命中次数；检索评测。幂等。
        - kb_docs 多：outline（JSON）、summary、keywords（JSON）、summary_by（model / extract / user / 空）、
          card_state（outline / ready / failed）、card_at、lang（zh / en / mixed）、tags（JSON）、
          tags_manual（1 = 手工改过，不再跟着路径变；规格里没有这一列，见笔记）、hits、last_hit（标准答案用）。
        - 老文档 outline 为空：下次同步时补（ingest 按"没有目录"判断，文件没变也补）。"""
        for col, typ in (("outline", "TEXT"), ("summary", "TEXT DEFAULT ''"), ("keywords", "TEXT DEFAULT '[]'"),
                         ("summary_by", "TEXT DEFAULT ''"), ("card_state", "TEXT DEFAULT ''"), ("card_at", "REAL"),
                         ("lang", "TEXT DEFAULT ''"), ("tags", "TEXT DEFAULT '[]'"), ("tags_manual", "INTEGER DEFAULT 0"),
                         ("hits", "INTEGER DEFAULT 0"), ("last_hit", "REAL")):
            if col not in dcols:
                self.db.execute("ALTER TABLE kb_docs ADD COLUMN %s %s" % (col, typ))
        # 文档级的全文索引（kb_list 的 query 用）：rowid 不固定，按 doc_id 删改
        self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS kb_docfts USING fts5(title, summary, keywords, tags, "
                        "doc_id UNINDEXED, library_id UNINDEXED%s)" % (", tokenize='trigram'" if self.trigram else ""))
        self.db.executescript("""
CREATE TABLE IF NOT EXISTS kb_gaps(id TEXT PRIMARY KEY, question TEXT, norm TEXT, reason TEXT, count INTEGER DEFAULT 1,
  first_ts REAL, last_ts REAL, session_id TEXT, seq INTEGER, status TEXT DEFAULT 'open', note TEXT DEFAULT '', answer_id TEXT);
CREATE INDEX IF NOT EXISTS ix_kb_gaps_status ON kb_gaps(status);
CREATE TABLE IF NOT EXISTS kb_eval(id TEXT PRIMARY KEY, question TEXT, expect TEXT, note TEXT DEFAULT '', created REAL);
CREATE TABLE IF NOT EXISTS kb_eval_runs(id TEXT PRIMARY KEY, ts REAL, mode TEXT, total INTEGER, hit1 INTEGER, hit3 INTEGER,
  hit5 INTEGER, ms INTEGER, detail TEXT);
""")

    def _migrate_kb(self):
        """老的资料（按工作区存、只有 kb_chunks）→ 独立的资料库（docs/两种模式.md 2.1）。幂等：
        每个有资料的工作区变成一个同名的资料库，来源是那个工作区里当初收录的目录和文件（不是整个工作区文件夹：
        自动同步会扫来源，整个文件夹会把输出、上传的文件都收进来）；文档挂到库上，老片段原样转成片段和全文索引
        （每个老片段自成一节）。老表 kb_chunks 不删、不改。只处理还没挂到库上的文档，所以重复启动不会重复迁移。"""
        with self.lock:
            rows = self.q("SELECT DISTINCT workspace_id FROM kb_docs WHERE library_id IS NULL AND workspace_id IS NOT NULL")
            if not rows:
                return
            from .docparse import kind_of
            self.db.execute("BEGIN")
            try:
                for r in rows:
                    wid = r["workspace_id"]
                    ws = self.one("SELECT * FROM workspaces WHERE id=?", (wid,))
                    lib = self.one("SELECT * FROM kb_libraries WHERE workspace_id=? ORDER BY created LIMIT 1", (wid,))
                    t = now()
                    if lib is None:
                        base = ((ws or {}).get("name") or "资料").replace("/", "／").strip() or "资料"
                        name, i = base, 2
                        while self.one("SELECT id FROM kb_libraries WHERE name=?", (name,)):
                            name, i = "%s（%d）" % (base, i), i + 1
                        lib = {"id": new_id("L_"), "name": name}
                        self.db.execute("INSERT INTO kb_libraries(id,name,note,created,updated,workspace_id) VALUES(?,?,?,?,?,?)",
                                        (lib["id"], name, "", t, t, wid))
                    root = (ws or {}).get("path") or ""
                    srcs = {}

                    def source_for(rel):
                        """老的 kb_add 收的是某个目录或文件：一级目录下的文档归到那个目录，根目录下的文件各算一个来源。"""
                        rel = (rel or "").replace("\\", "/")
                        top, _, rest = rel.partition("/")
                        spath, kind, inner = ((os.path.join(root, top), "dir", rest) if rest
                                              else (os.path.join(root, rel), "file", os.path.basename(rel)))
                        if spath not in srcs:
                            got = self.one("SELECT id FROM kb_sources WHERE library_id=? AND path=?", (lib["id"], spath))
                            sid_ = got["id"] if got else new_id("S_")
                            if not got:
                                self.db.execute("INSERT INTO kb_sources(id,library_id,path,kind,added) VALUES(?,?,?,?,?)",
                                                (sid_, lib["id"], spath, kind, t))
                            srcs[spath] = sid_
                        return srcs[spath], inner

                    for d in self.q("SELECT * FROM kb_docs WHERE workspace_id=? AND library_id IS NULL", (wid,)):
                        src_id, inner = source_for(d["path"]) if root else (None, d["path"])
                        chunks = self.q("SELECT rowid AS rid, text, loc FROM kb_chunks WHERE doc_id=? ORDER BY rowid", (d["id"],))
                        from .context import estimate_text
                        total = 0
                        for seq, c in enumerate(chunks):
                            tok = int(estimate_text(c["text"] or ""))
                            total += tok
                            cur = self.db.execute("INSERT INTO kb_sections(doc_id,library_id,seq,loc,heading,text,tokens) "
                                                  "VALUES(?,?,?,?,?,?,?)", (d["id"], lib["id"], seq, c["loc"] or "", "",
                                                                            c["text"] or "", tok))
                            sec = cur.lastrowid
                            cur = self.db.execute("INSERT INTO kb_parts(doc_id,library_id,seq,parent,loc,heading,text,tokens) "
                                                  "VALUES(?,?,?,?,?,?,?,?)", (d["id"], lib["id"], seq, sec, c["loc"] or "", "",
                                                                              c["text"] or "", tok))
                            pid = cur.lastrowid
                            self.db.execute("INSERT INTO kb_fts(rowid,text,heading,part_id,library_id) VALUES(?,?,?,?,?)",
                                            (pid, c["text"] or "", "", pid, lib["id"]))
                        # size 留空：下次同步时按新的切法重读一遍（老片段在那之前照常能查）
                        self.db.execute("UPDATE kb_docs SET library_id=?, source_id=?, path=?, status='ok', error='', kind=?, "
                                        "tokens=?, chunks=?, meta='{}' WHERE id=?",
                                        (lib["id"], src_id, inner, kind_of(d["path"] or ""), total, len(chunks), d["id"]))
                self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('kb_migrated', ?)", (str(now()),))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def _backfill_fts(self):
        """历史发言索引只在第一次启动时补一遍，之后由 add_event 随写随建。"""
        if self.meta_get("msg_fts_built"):
            return
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.execute("DELETE FROM msg_fts")
                rows = self.db.execute(
                    "SELECT e.seq, e.session_id, e.type, e.payload, e.ts, s.workspace_id FROM events e "
                    "JOIN sessions s ON s.id=e.session_id WHERE s.kind='task' AND e.type IN ('user_message','assistant_message') "
                    "ORDER BY e.seq").fetchall()
                for r in rows:
                    try:
                        payload = json.loads(r["payload"])
                    except ValueError:
                        continue
                    self._index(r["seq"], r["session_id"], r["workspace_id"], r["type"], payload, r["ts"])
                self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('msg_fts_built', ?)", (str(now()),))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def _index(self, seq, sid, ws_id, etype, payload, ts):
        role, key = INDEXED[etype]
        text = (payload or {}).get(key) if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            return
        self.db.execute("INSERT INTO msg_fts(text,session_id,workspace_id,seq,role,ts) VALUES(?,?,?,?,?,?)",
                        (text, sid, ws_id, seq, role, ts))

    def delete_fts(self, sid):
        self.x("DELETE FROM msg_fts WHERE session_id=?", (sid,))

    # ---- 键值 ----
    def meta_get(self, key, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def meta_set(self, key, value):
        self.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def tx(self):
        """一段要么全做、要么全不做的写操作：with store.tx(): ...（持有账本锁）。"""
        return _Tx(self)

    # ---- 基础 ----
    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            return cur.lastrowid

    def close(self):
        with self.lock:
            self.db.close()

    # ---- 工作区 ----
    def add_workspace(self, name, path, instructions="", is_default=False):
        wid = new_id("w_")
        self.x("INSERT INTO workspaces(id,name,path,instructions,created,is_default) VALUES(?,?,?,?,?,?)",
               (wid, name, os.path.abspath(path), instructions, now(), 1 if is_default else 0))
        return self.workspace(wid)

    def workspace(self, wid):
        return _ws(self.one("SELECT * FROM workspaces WHERE id=?", (wid,)))

    def workspaces(self):
        return [_ws(w) for w in self.q("SELECT * FROM workspaces ORDER BY created")]

    def default_workspace(self):
        """默认空间：is_default=1 的那个（万一有多个，取最早建的）。没有时返回 None。"""
        return _ws(self.one("SELECT * FROM workspaces WHERE is_default=1 ORDER BY created LIMIT 1"))

    # ---- 任务 ----
    def add_session(self, workspace_id, title="", kind="task", parent_id=None, meta=None, mode=None):
        """mode：cariai（默认）或 hspace（docs/两种模式.md 1.3）。子代理不写时跟着父对话。"""
        sid = new_id("s_")
        t = now()
        if mode is None and parent_id:
            p = self.one("SELECT mode FROM sessions WHERE id=?", (parent_id,))
            mode = (p or {}).get("mode")
        mode = mode if mode in ("cariai", "hspace") else "cariai"
        self.x("INSERT INTO sessions(id,workspace_id,title,status,kind,parent_id,meta,created,updated,mode) "
               "VALUES(?,?,?,?,?,?,?,?,?,?)",
               (sid, workspace_id, title, "idle", kind, parent_id, json.dumps(meta or {}, ensure_ascii=False), t, t, mode))
        return self.session(sid)

    def session(self, sid):
        s = self.one("SELECT * FROM sessions WHERE id=?", (sid,))
        if s:
            s["meta"] = json.loads(s["meta"] or "{}")
            s["mode"] = s.get("mode") or "cariai"
        return s

    def sessions(self, workspace_id=None, kind="task", mode=None):
        sql, args = "SELECT * FROM sessions WHERE kind=?", [kind]
        if workspace_id:
            sql += " AND workspace_id=?"
            args.append(workspace_id)
        if mode:
            sql += " AND COALESCE(mode,'cariai')=?"
            args.append(mode)
        rows = self.q(sql + " ORDER BY updated DESC", tuple(args))
        for s in rows:
            s["meta"] = json.loads(s["meta"] or "{}")
            s["mode"] = s.get("mode") or "cariai"
        return rows

    def set_session(self, sid, **fields):
        if "meta" in fields:
            fields["meta"] = json.dumps(fields["meta"], ensure_ascii=False)
        fields["updated"] = now()
        keys = ",".join("%s=?" % k for k in fields)
        self.x("UPDATE sessions SET %s WHERE id=?" % keys, tuple(fields.values()) + (sid,))

    def bump_revision(self, sid):
        with self.lock:
            self.x("UPDATE sessions SET revision=revision+1, updated=? WHERE id=?", (now(), sid))
            return self.session(sid)["revision"]

    # ---- 事件 ----
    def add_event(self, sid, etype, payload, revision=None):
        with self.lock:
            if revision is None:
                s = self.one("SELECT revision FROM sessions WHERE id=?", (sid,))
                revision = s["revision"] if s else 0
            t = now()
            seq = self.x("INSERT INTO events(session_id,revision,type,payload,ts) VALUES(?,?,?,?,?)",
                         (sid, revision, etype, json.dumps(payload, ensure_ascii=False), t))
            if etype in INDEXED:
                s = self.one("SELECT kind, workspace_id FROM sessions WHERE id=?", (sid,))
                if s and s["kind"] == "task":
                    self._index(seq, sid, s["workspace_id"], etype, payload, t)
            return {"seq": seq, "session_id": sid, "revision": revision, "type": etype, "payload": payload, "ts": t}

    def events(self, sid, since=0, types=None):
        rows = self.q("SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq", (sid, since))
        out = []
        for r in rows:
            if types and r["type"] not in types:
                continue
            r["payload"] = json.loads(r["payload"])
            out.append(r)
        return out

    def events_all(self, since=0, limit=500):
        rows = self.q("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (since, limit))
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    def last_seq(self):
        r = self.one("SELECT MAX(seq) AS m FROM events")
        return r["m"] or 0

    # ---- 动作 ----
    def add_action(self, sid, revision, call_id, tool, args, risk, state="proposed"):
        aid = new_id("a_")
        t = now()
        self.x("INSERT INTO actions(id,session_id,revision,call_id,tool,args,digest,risk,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (aid, sid, revision, call_id, tool, json.dumps(args, ensure_ascii=False), digest_of(tool, args, revision), risk, state, t, t))
        return self.action(aid)

    def action(self, aid):
        a = self.one("SELECT * FROM actions WHERE id=?", (aid,))
        if a:
            a["args"] = json.loads(a["args"] or "{}")
            a["result"] = json.loads(a["result"]) if a["result"] else None
        return a

    def actions(self, sid, states=None):
        rows = self.q("SELECT id FROM actions WHERE session_id=? ORDER BY created", (sid,))
        out = [self.action(r["id"]) for r in rows]
        return [a for a in out if not states or a["state"] in states]

    def set_action(self, aid, state, result=None):
        if result is None:
            self.x("UPDATE actions SET state=?, updated=? WHERE id=?", (state, now(), aid))
        else:
            self.x("UPDATE actions SET state=?, result=?, updated=? WHERE id=?",
                   (state, json.dumps(result, ensure_ascii=False), now(), aid))

    def audit(self, sid, action, decision, outcome="", detail=""):
        self.x("INSERT INTO audit(ts,session_id,action_id,tool,digest,risk,decision,outcome,detail) VALUES(?,?,?,?,?,?,?,?,?)",
               (now(), sid, action["id"], action["tool"], action["digest"], action["risk"], decision, outcome, detail[:500]))

    def audit_note(self, tool, risk, decision, outcome="", detail="", sid=None):
        """不属于某个动作的操作（例如用户上传文件）也记一行审计。"""
        self.x("INSERT INTO audit(ts,session_id,action_id,tool,digest,risk,decision,outcome,detail) VALUES(?,?,?,?,?,?,?,?,?)",
               (now(), sid, None, tool, digest_of(tool, detail, 0), risk, decision, outcome, detail[:500]))

    # ---- 工具设置 ----
    def tool_setting(self, tool):
        r = self.one("SELECT setting FROM tool_settings WHERE tool=?", (tool,))
        return r["setting"] if r else None

    def set_tool_setting(self, tool, setting):
        if setting in (None, "", "default"):
            self.x("DELETE FROM tool_settings WHERE tool=?", (tool,))
        else:
            self.x("INSERT INTO tool_settings(tool,setting) VALUES(?,?) ON CONFLICT(tool) DO UPDATE SET setting=excluded.setting", (tool, setting))


def _ws(w):
    """工作区记录：is_default 转成 true / false。"""
    if w is not None:
        w["is_default"] = bool(w.get("is_default"))
    return w


class _Tx:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.store.lock.acquire()
        try:
            self.store.db.execute("BEGIN")
        except Exception:
            self.store.lock.release()
            raise
        return self.store

    def __exit__(self, et, ev, tb):
        try:
            self.store.db.execute("COMMIT" if et is None else "ROLLBACK")
        finally:
            self.store.lock.release()
        return False
