"""向量存在哪、用什么算（docs/两种模式.md 2.4 的 kb.engine）。两套实现放在同一个接口后面：

    add(parts)                  收录一批片段：parts 是 [{"id", "library_id", "doc_id", "text"}]，text 是建索引用的文字
    delete_docs(doc_ids)        删掉这些文档的片段的向量
    reset()                     全部作废（换了向量模型或向量长度）
    query(text, k, lib_ids)     按问题找最近的 k 个：[(part_id, 分数)]，问题前面的 query_prefix 由这里加
    available() -> (ok, 原因)   组件装没装

- builtin：向量以 float32 存在账本（SQLite 的 kb_vectors 表）里，用 numpy 算余弦相似度。
- langchain：Chroma（langchain-chroma，数据放 <数据目录>/chroma）+ langchain-openai 的 OpenAIEmbeddings
  （接 vLLM 时 check_embedding_ctx_length=False）。kb_vectors 表里照样记一行（vec 为空），用来数"有多少片段有向量"。
  开发环境里没装这几个包，这一套没有运行验证过，假设写在 docs/内核迁移说明.md。
两套都在用到时才导入 numpy / LangChain，没装时 import 本模块不出错。"""
from __future__ import annotations
import os, threading

from . import embed as emb

LC_PACKAGES = ("langchain_chroma", "langchain_openai", "langchain_classic", "chromadb")


def has_numpy():
    try:
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


def has_langchain():
    import importlib.util
    try:
        return all(importlib.util.find_spec(p) is not None for p in LC_PACKAGES)
    except Exception:
        return False


def engine_name(cfg):
    """kb.engine：auto 装了 LangChain 那几个包就用 langchain，否则 builtin；写了 langchain 而没装包也退回 builtin。"""
    want = str(cfg.get("kb", "engine", default="auto") or "auto").lower()
    if want == "builtin":
        return "builtin"
    if want in ("auto", "langchain") and has_langchain():
        return "langchain"
    return "builtin"


def make(svc, name):
    return LangchainVectors(svc) if name == "langchain" else BuiltinVectors(svc)


class BuiltinVectors:
    name = "builtin"

    def __init__(self, svc):
        self.svc = svc
        self.lock = threading.Lock()
        self.ver = 0
        self.cache = {}          # library_id → (ver, ids, 归一化后的矩阵)

    def available(self):
        if not has_numpy():
            return False, "这台电脑没有安装 numpy，无法使用「语义检索」。重新自动安装：bash start.sh --setup（Windows：start.bat --setup）。"
        return True, ""

    def add(self, parts):
        import numpy as np
        if not parts:
            return 0
        vecs = emb.embed(self.svc.cfg, [p["text"] for p in parts])
        store = self.svc.store
        with store.tx():
            for p, v in zip(parts, vecs):
                a = np.asarray(v, dtype=np.float32)
                store.x("INSERT OR REPLACE INTO kb_vectors(part_id,library_id,doc_id,dims,vec) VALUES(?,?,?,?,?)",
                        (p["id"], p["library_id"], p["doc_id"], int(a.shape[0]), a.tobytes()))
        with self.lock:
            self.ver += 1
        return len(parts)

    def delete_docs(self, doc_ids):
        store = self.svc.store
        for d in doc_ids:
            store.x("DELETE FROM kb_vectors WHERE doc_id=?", (d,))
        with self.lock:
            self.ver += 1

    def reset(self):
        self.svc.store.x("DELETE FROM kb_vectors")
        with self.lock:
            self.ver += 1
            self.cache.clear()

    def _matrix(self, lid):
        import numpy as np
        with self.lock:
            ent = self.cache.get(lid)
            if ent and ent[0] == self.ver:
                return ent[1], ent[2]
            ver = self.ver
        rows = self.svc.store.q("SELECT part_id, dims, vec FROM kb_vectors WHERE library_id=? AND vec IS NOT NULL", (lid,))
        if not rows:
            ids, mat = np.zeros(0, dtype=np.int64), np.zeros((0, 1), dtype=np.float32)
        else:
            dims = max(set(r["dims"] for r in rows), key=lambda d: sum(1 for r in rows if r["dims"] == d))
            rows = [r for r in rows if r["dims"] == dims]
            ids = np.asarray([r["part_id"] for r in rows], dtype=np.int64)
            mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32).reshape(len(rows), dims)
            norm = np.linalg.norm(mat, axis=1, keepdims=True)
            norm[norm == 0] = 1.0
            mat = mat / norm
        with self.lock:
            self.cache[lid] = (ver, ids, mat)
        return ids, mat

    def query(self, text, k, lib_ids):
        import numpy as np
        qv = np.asarray(emb.embed(self.svc.cfg, [text], query=True)[0], dtype=np.float32)
        n = float(np.linalg.norm(qv)) or 1.0
        qv = qv / n
        cand = []
        for lid in lib_ids:
            ids, mat = self._matrix(lid)
            if not len(ids) or mat.shape[1] != qv.shape[0]:
                continue
            sims = mat @ qv
            top = np.argsort(-sims)[:k]
            cand += [(int(ids[i]), float(sims[i])) for i in top]
        cand.sort(key=lambda x: -x[1])
        return cand[:k]

    def close(self):
        pass


class LangchainVectors:
    """LangChain 这一套（没有运行验证过）。"""
    name = "langchain"

    def __init__(self, svc):
        self.svc = svc
        self.lock = threading.Lock()
        self._db = None

    def available(self):
        if not has_langchain():
            return False, "这台电脑没有安装向量库组件（langchain-chroma 等），无法使用「语义检索」。重新自动安装：bash start.sh --setup（Windows：start.bat --setup）。"
        return True, ""

    def _embeddings(self):
        from langchain_openai import OpenAIEmbeddings
        c = emb.conf(self.svc.cfg)
        kw = {"model": c["model"], "base_url": c["base_url"], "api_key": c["api_key"] or "EMPTY",
              "check_embedding_ctx_length": False, "chunk_size": c["batch"]}
        if c["dims"]:
            kw["dimensions"] = c["dims"]
        prefix = c["query_prefix"]

        class PrefixedEmbeddings(OpenAIEmbeddings):
            """query_prefix 只加在问题上（Chroma 的 Retriever 检索时调 embed_query）。"""

            def embed_query(self, text, **kw2):
                return super().embed_query(text if text.startswith(prefix) else prefix + text, **kw2)

        return PrefixedEmbeddings(**kw)

    def db(self):
        with self.lock:
            if self._db is None:
                from langchain_chroma import Chroma
                self._db = Chroma(collection_name="kb", embedding_function=self._embeddings(),
                                  persist_directory=os.path.join(self.svc.cfg.home, "chroma"),
                                  collection_metadata={"hnsw:space": "cosine"})
            return self._db

    def add(self, parts):
        if not parts:
            return 0
        try:
            self.db().add_texts([p["text"] for p in parts],
                                metadatas=[{"library_id": p["library_id"], "doc_id": p["doc_id"], "part_id": int(p["id"])}
                                           for p in parts],
                                ids=[str(p["id"]) for p in parts])
        except Exception as e:
            raise emb.EmbedError("向量库收录失败：%s: %s" % (type(e).__name__, e))
        store = self.svc.store
        with store.tx():
            for p in parts:
                store.x("INSERT OR REPLACE INTO kb_vectors(part_id,library_id,doc_id,dims,vec) VALUES(?,?,?,?,NULL)",
                        (p["id"], p["library_id"], p["doc_id"], 0))
        return len(parts)

    def delete_docs(self, doc_ids):
        store = self.svc.store
        for d in doc_ids:
            ids = [str(r["part_id"]) for r in store.q("SELECT part_id FROM kb_vectors WHERE doc_id=?", (d,))]
            if ids:
                try:
                    self.db().delete(ids=ids)
                except Exception:
                    pass
            store.x("DELETE FROM kb_vectors WHERE doc_id=?", (d,))

    def reset(self):
        try:
            self.db().reset_collection()
        except Exception:
            pass
        self.svc.store.x("DELETE FROM kb_vectors")

    def retriever(self, lib_ids, k):
        """Chroma 的 Retriever（混合检索时交给 EnsembleRetriever）。"""
        flt = {"library_id": {"$in": list(lib_ids)}} if lib_ids else None
        return self.db().as_retriever(search_type="similarity", search_kwargs={"k": k, "filter": flt})

    def query(self, text, k, lib_ids):
        try:
            flt = {"library_id": {"$in": list(lib_ids)}} if lib_ids else None
            got = self.db().similarity_search_with_relevance_scores(text, k=k, filter=flt)
        except Exception as e:
            raise emb.EmbedError("向量库检索失败：%s: %s" % (type(e).__name__, e))
        return [(int(d.metadata.get("part_id")), float(s)) for d, s in got if d.metadata.get("part_id") is not None]

    def close(self):
        self._db = None

