"""收录（docs/两种模式.md 2.2、9.2）：后台线程把资料库的来源扫一遍，增量地切片、建全文索引、算向量。

- 加来源、重新收录都只是排一个任务，接口立刻返回；进度用 kb_progress 事件推给界面
  （{"library_id", "total", "done", "failed", "current", "state"}，只推不写账本），也能用 progress() 查。
- 排着的任务记在账本的 meta（kb_jobs）里：程序重启后接着做。
- 增量：大小和修改时间都没变就跳过；变了才重读；文件没了就从库里去掉。每个文件单独出错单独记，不影响别的文件。
- 自动同步：kb.sync_minutes（默认 30，0 表示关）后台定时把所有资料库扫一遍；线程起来时也扫一遍。
- 写账本用 store.tx()（持账本的锁）；同一个文件的"查旧记录 → 写新记录"在 ingest_lock 里做，CariAi 的 kb_add 同步收录时也拿它。
- 程序关闭时 stop()：线程在两个文件之间看一眼停止标志，最多等 5 秒。"""
from __future__ import annotations
import collections, hashlib, json, os, threading, time, traceback

from ..store import new_id, now
from .. import docparse
from . import embed as emb
from . import cards

META_JOBS = "kb_jobs"
META_SIG = "kb_vec_sig"
PROGRESS_EVERY = 0.3


def _file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 16), b""):
            h.update(b)
    return h.hexdigest()[:16]


def list_files(src, limit=5000):
    """一个来源下面要收录的文件：[(绝对路径, 相对来源的路径)]。跳过临时文件（~$ 开头）、隐藏文件和隐藏目录、.agent。"""
    p = src["path"]
    if src["kind"] == "file":
        return [(p, os.path.basename(p))] if os.path.isfile(p) else []
    out = []
    if not os.path.isdir(p):
        return out
    for d, dirs, files in os.walk(p):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and not x.startswith("~$"))
        for f in sorted(files):
            if f.startswith("~$") or f.startswith("."):
                continue
            if os.path.splitext(f)[1].lower() not in docparse.SUPPORTED:
                continue
            fp = os.path.join(d, f)
            out.append((fp, os.path.relpath(fp, p).replace("\\", "/")))
            if len(out) >= limit:
                return out
    return out


class Indexer:
    def __init__(self, svc):
        self.svc = svc
        self.cv = threading.Condition(threading.RLock())
        self.ingest_lock = threading.RLock()
        self.queue = collections.deque()
        self.thread = None
        self.stop_ev = threading.Event()
        self.prog = {}
        self.current_job = None
        self.cancelled = set()
        self.next_sync = 0.0
        self._last_emit = 0.0

    @property
    def store(self):
        return self.svc.store

    @property
    def cfg(self):
        return self.svc.cfg

    def _int(self, key, default):
        v = self.cfg.get("kb", key, default=default)
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    # ---- 任务 ----
    def start_if_needed(self):
        """有资料库（或者有排着没做完的任务）才起线程：线程起来先接着做没做完的，再把所有资料库同步一遍。"""
        pending = self._pending()
        libs = [r["id"] for r in self.store.q("SELECT id FROM kb_libraries ORDER BY created")]
        if not libs and not pending:
            return
        for lid in pending + libs:
            self._push(lid)
        self._start()

    def _pending(self):
        try:
            v = json.loads(self.store.meta_get(META_JOBS) or "[]")
            return [x for x in v if isinstance(x, str)]
        except ValueError:
            return []

    def _save_pending(self):
        with self.cv:
            ids = list(self.queue) + ([self.current_job] if self.current_job else [])
        try:
            self.store.meta_set(META_JOBS, json.dumps(list(dict.fromkeys(ids))))
        except Exception:
            pass

    def _push(self, lid):
        with self.cv:
            self.cancelled.discard(lid)
            if lid not in self.queue:
                self.queue.append(lid)
            p = self.prog.setdefault(lid, {"active": True, "total": 0, "done": 0, "failed": 0, "current": ""})
            p["active"] = True
            self.cv.notify_all()

    def enqueue(self, lid):
        """排一次收录（加来源、重新收录、换了向量模型）。立刻返回。"""
        if self.svc.rt.closing if hasattr(self.svc.rt, "closing") else False:
            return
        self._push(lid)
        self._save_pending()
        self._start()

    def enqueue_all(self):
        for r in self.store.q("SELECT id FROM kb_libraries ORDER BY created"):
            self.enqueue(r["id"])

    def cancel(self, lid):
        with self.cv:
            self.cancelled.add(lid)
            try:
                self.queue.remove(lid)
            except ValueError:
                pass
            self.prog.pop(lid, None)
        self._save_pending()

    def _start(self):
        with self.cv:
            if self.thread is not None and self.thread.is_alive():
                return
            if self.stop_ev.is_set():
                return
            self.next_sync = time.time() + self._int("sync_minutes", 30) * 60
            self.thread = threading.Thread(target=self._run, name="kb-indexer", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_ev.set()
        with self.cv:
            self.cv.notify_all()
            t = self.thread
        if t is not None and t is not threading.current_thread():
            t.join(5)

    def progress(self, lid):
        with self.cv:
            p = dict(self.prog.get(lid) or {"active": False, "total": 0, "done": 0, "failed": 0, "current": ""})
            p["active"] = bool(p.get("active")) or lid in self.queue or self.current_job == lid
        return p

    def busy(self):
        with self.cv:
            return bool(self.queue) or self.current_job is not None

    def wait_idle(self, timeout=10):
        """测试和接口用：等排着的任务都做完。"""
        end = time.time() + timeout
        while time.time() < end:
            if not self.busy():
                return True
            time.sleep(0.02)
        return not self.busy()

    def _run(self):
        while not self.stop_ev.is_set():
            with self.cv:
                if not self.queue:
                    sync = self._int("sync_minutes", 30)
                    wait = (self.next_sync - time.time()) if sync > 0 else 3600
                    if wait > 0:
                        self.cv.wait(min(wait, 3600))
                        if self.stop_ev.is_set():
                            return
                        if not self.queue and (sync <= 0 or time.time() < self.next_sync):
                            continue
                    if not self.queue and sync > 0:
                        self.next_sync = time.time() + sync * 60
                        for r in self.store.q("SELECT id FROM kb_libraries ORDER BY created"):
                            if r["id"] not in self.queue:
                                self.queue.append(r["id"])
                        if not self.queue:
                            continue
                lid = self.queue.popleft()
                self.current_job = lid
            try:
                self.run_job(lid)
            except Exception:
                if not self.stop_ev.is_set():
                    traceback.print_exc()
            finally:
                with self.cv:
                    self.current_job = None
                    p = self.prog.get(lid)
                    if p is not None and lid not in self.queue:
                        p["active"] = False
                if not self.stop_ev.is_set():
                    self._save_pending()
                    self._emit(lid, force=True)

    def _stopped(self, lid=None):
        return self.stop_ev.is_set() or (lid is not None and lid in self.cancelled)

    def _emit(self, lid, force=False):
        t = time.time()
        if not force and t - self._last_emit < PROGRESS_EVERY:
            return
        self._last_emit = t
        p = self.progress(lid)
        try:
            self.svc.rt.emit(None, "kb_progress", {"library_id": lid, "total": p["total"], "done": p["done"],
                                                    "failed": p["failed"], "current": p["current"],
                                                    "state": "indexing" if p["active"] else "ready"}, store=False)
        except Exception:
            pass

    # ---- 一次收录 ----
    def run_job(self, lid):
        """把一个资料库的来源扫一遍：增量收录、去掉没了的文件、补上缺的向量、记下同步时间。"""
        store = self.store
        if not store.one("SELECT id FROM kb_libraries WHERE id=?", (lid,)):
            return
        self.check_signature()
        from . import ANSWERS_ID
        if lid == ANSWERS_ID:
            # 9.5 的标准答案库没有来源（内容由 agent/feedback.py 写）：扫它时只补向量，不能当成"文件没了"删掉
            self.fill_vectors(lid)
            if not self._stopped(lid):
                store.x("UPDATE kb_libraries SET synced=? WHERE id=?", (now(), lid))
            return
        from . import is_shared
        if is_shared(store, lid):
            # 公司共享的库（docs/借鉴开源项目.md 5.5）：内容来自共享目录的索引包（agent/shared.py 导入），没有来源，
            # 不参与本机的定时同步和重新收录（不然会被当成"文件都没了"删光）。只补向量：包里的向量签名和本机不一致、
            # 被丢掉了，而本机配了向量模型时，在这里用本机的模型重算。synced 是从共享目录同步的时间，这里不动它。
            self.fill_vectors(lid)
            return
        srcs = store.q("SELECT * FROM kb_sources WHERE library_id=? ORDER BY added", (lid,))
        limit = max(1, self._int("max_files", 5000))
        files = []
        for s in srcs:
            if len(files) >= limit:
                break
            files += [(s, a, r) for a, r in list_files(s, limit - len(files))]
        with self.cv:
            p = self.prog.setdefault(lid, {})
            p.update(active=True, total=len(files), done=0, failed=0, current="")
        self._emit(lid, force=True)
        seen = set()
        for s, a, r in files:
            if self._stopped(lid):
                return
            seen.add((s["id"], r))
            with self.cv:
                self.prog[lid]["current"] = r
            try:
                res = self.ingest_file(lid, s, a, r)
            except Exception as e:      # 不该出现：ingest_file 自己会把文件的错误记进文档
                res = {"status": "failed", "error": "%s: %s" % (type(e).__name__, e)}
            with self.cv:
                self.prog[lid]["done"] += 1
                if res.get("status") == "failed":
                    self.prog[lid]["failed"] += 1
            self._emit(lid)
        if self._stopped(lid):
            return
        # 文件没了（或者来源被去掉了）：从库里去掉
        gone = [d["id"] for d in store.q("SELECT id, source_id, path FROM kb_docs WHERE library_id=?", (lid,))
                if (d["source_id"], d["path"]) not in seen and not (len(files) >= limit and d["source_id"] in {x["id"] for x in srcs})]
        if gone:
            from . import delete_doc_rows
            with self.ingest_lock:
                try:
                    self.svc.vectors().delete_docs(gone)
                except Exception:
                    pass
                with store.tx():
                    delete_doc_rows(store, gone)
        self.fill_vectors(lid)
        if not self._stopped(lid):
            store.x("UPDATE kb_libraries SET synced=? WHERE id=?", (now(), lid))
            with self.cv:
                self.prog[lid]["current"] = ""
            try:
                self.svc.cards.kick()         # 有待写的摘要：后台写（模型不可用时不起线程）
            except Exception:
                pass

    def ingest_file(self, lid, src, abs_path, rel, force=False):
        """收一个文件。返回 {"status": ok | failed | skipped | same, "doc_id", "parts", "error"}。"""
        store = self.store
        try:
            st = os.stat(abs_path)
        except OSError as e:
            return {"status": "failed", "error": str(e)}
        with self.ingest_lock:
            doc = store.one("SELECT * FROM kb_docs WHERE library_id=? AND source_id=? AND path=?", (lid, src["id"], rel))
        if (not force and doc and doc.get("size") == st.st_size and doc.get("mtime") == st.st_mtime
                and doc.get("status") in ("ok", "failed", "skipped")):
            if cards.needs_outline(doc):
                # 老文档（上一轮收录的）还没有卡片：文件没变也补目录、语言、标签、开头摘录（按"没有目录"判断）
                with self.ingest_lock:
                    cards.refresh(store, doc["id"], changed=False, src_kind=src.get("kind"), rel=rel)
            return {"status": "same", "doc_id": doc["id"], "parts": doc.get("chunks") or 0, "error": doc.get("error") or ""}
        max_mb = self.cfg.get("kb", "max_file_mb", default=50)
        try:
            max_bytes = float(max_mb) * 1024 * 1024
        except (TypeError, ValueError):
            max_bytes = 50 * 1024 * 1024
        parsed, status, error, sha = None, "ok", "", ""
        if st.st_size > max_bytes:
            status, error = "skipped", "文件超过 %s MB，没有收录" % max_mb
        else:
            try:
                sha = _file_hash(abs_path)
                parsed = docparse.parse_file(abs_path)
                if not parsed["sections"]:
                    status, error = "failed", "这份文件里没有读到文字"
            except docparse.ParseError as e:
                status, error = "failed", str(e)
            except Exception as e:
                status, error = "failed", "读不出来：%s: %s" % (type(e).__name__, str(e)[:200])
        title = os.path.basename(abs_path)
        ver = docparse.version_info(title, st.st_mtime)
        with self.ingest_lock:
            doc = store.one("SELECT * FROM kb_docs WHERE library_id=? AND source_id=? AND path=?", (lid, src["id"], rel))
            if doc:
                try:
                    self.svc.vectors().delete_docs([doc["id"]])
                except Exception:
                    pass
            did = doc["id"] if doc else new_id("d_")
            kind = (parsed or {}).get("kind") or docparse.kind_of(abs_path)
            n_parts = 0
            with store.tx():
                if doc:
                    store.x("DELETE FROM kb_fts WHERE rowid IN (SELECT id FROM kb_parts WHERE doc_id=?)", (did,))
                    store.x("DELETE FROM kb_parts WHERE doc_id=?", (did,))
                    store.x("DELETE FROM kb_sections WHERE doc_id=?", (did,))
                    store.x("DELETE FROM kb_vectors WHERE doc_id=?", (did,))
                    store.x("DELETE FROM kb_chunks WHERE doc_id=?", (did,))
                else:
                    store.x("INSERT INTO kb_docs(id,path,title,mtime,chunks,added,library_id,source_id) VALUES(?,?,?,?,?,?,?,?)",
                            (did, rel, title, st.st_mtime, 0, now(), lid, src["id"]))
                seq = 0
                for si, sec in enumerate((parsed or {}).get("sections") or []):
                    cur = store.db.execute("INSERT INTO kb_sections(doc_id,library_id,seq,loc,heading,text,tokens) VALUES(?,?,?,?,?,?,?)",
                                           (did, lid, si, sec["loc"], sec["heading"], sec["text"], sec["tokens"]))
                    sec_id = cur.lastrowid
                    for part in sec["parts"]:
                        cur = store.db.execute("INSERT INTO kb_parts(doc_id,library_id,seq,parent,loc,heading,text,tokens) "
                                               "VALUES(?,?,?,?,?,?,?,?)",
                                               (did, lid, seq, sec_id, part["loc"], part["heading"], part["text"], part["tokens"]))
                        pid = cur.lastrowid
                        store.db.execute("INSERT INTO kb_fts(rowid,text,heading,part_id,library_id) VALUES(?,?,?,?,?)",
                                         (pid,) + fts_columns(part) + (pid, lid))
                        seq += 1
                n_parts = seq
                store.x("UPDATE kb_docs SET title=?, mtime=?, chunks=?, size=?, sha=?, status=?, error=?, kind=?, tokens=?, "
                        "meta=?, family=?, version_label=?, effective=? WHERE id=?",
                        (title, st.st_mtime, n_parts, st.st_size, sha, status, error, kind,
                         int((parsed or {}).get("tokens") or 0), json.dumps((parsed or {}).get("meta") or {}, ensure_ascii=False),
                         ver["family"], ver["version_label"], ver["effective"], did))
                store.x("UPDATE kb_libraries SET updated=? WHERE id=?", (now(), lid))
            # 文档卡片（docs/借鉴开源项目.md 第 1 节）：目录重算、语言、标签、开头摘录；摘要标成待写，交给后台
            cards.refresh(store, did, changed=True, src_kind=src.get("kind"), rel=rel)
        return {"status": status, "doc_id": did, "parts": n_parts, "error": error}

    # ---- 向量 ----
    def check_signature(self):
        """换了向量模型或向量长度：旧向量作废，所有资料库重新算（界面上看 vectors 和 parts 的差）。"""
        sig = emb.signature(self.cfg)
        old = self.store.meta_get(META_SIG) or ""
        if sig and old and sig != old:
            try:
                self.svc.vectors().reset()
            except Exception:
                pass
            for r in self.store.q("SELECT id FROM kb_libraries"):
                self._push(r["id"])
        if sig and sig != old:
            self.store.meta_set(META_SIG, sig)

    def fill_vectors(self, lid):
        """给还没有向量的片段算向量。语义检索用不了（没配、缺组件）就不做；向量模型出错就停在这里，下次同步再补。"""
        if not emb.configured(self.cfg):
            return 0
        vs = self.svc.vectors()
        ok, _ = vs.available()
        if not ok:
            return 0
        batch = emb.conf(self.cfg)["batch"]
        done = 0
        while not self._stopped(lid):
            rows = self.store.q("SELECT p.id, p.library_id, p.doc_id, p.heading, p.text, d.title FROM kb_parts p "
                                "JOIN kb_docs d ON d.id=p.doc_id "
                                "LEFT JOIN kb_vectors v ON v.part_id=p.id WHERE p.library_id=? AND v.part_id IS NULL "
                                "ORDER BY p.id LIMIT ?", (lid, batch * 4))
            if not rows:
                break
            try:
                with self.ingest_lock:
                    vs.add([{"id": r["id"], "library_id": r["library_id"], "doc_id": r["doc_id"], "text": index_text(r)}
                            for r in rows])
                self.svc.mark_embed(True)
                done += len(rows)
            except emb.EmbedError as e:
                self.svc.mark_embed(False, str(e))
                break
        return done


def fts_columns(part):
    """全文索引的两列：(最里面那层标题 + 正文, 外面几层标题)。外层标题权重低（retrieve.HEADING_WEIGHT）。"""
    path = [x.strip() for x in (part.get("heading") or "").split(" > ") if x.strip()]
    inner = path[-1] if path else ""
    text = part.get("text") or ""
    return ((inner + "\n" + text) if inner and not text.startswith(inner) else text), " > ".join(path[:-1])


def index_text(part):
    """算向量（和重排）用的文字：文件名（去扩展名）+ 标题路径 + 正文（docs/借鉴开源项目.md 2.1）。
    表格的片段标题路径里只有工作表名，带上文件名才分得清是哪张表。全文索引不用它（见 fts_columns）。
    改了拼法要把 embed.TEXT_VERSION 加一，旧向量才会作废重算。标准答案的"文件名"是固定的"标准答案"，不带。"""
    h = (part.get("heading") or "").strip()
    t = part.get("text") or ""
    body = (h + "\n" + t) if h and not t.startswith(h) else t
    title = part.get("title") or ""
    if part.get("library_id") == "lib_answers":
        return body
    name = os.path.splitext(title)[0].strip() if title else ""
    if name and not body.startswith(name):
        return "《%s》\n%s" % (name, body)
    return body
