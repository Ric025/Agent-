"""kb.engine=langchain 的自检（tests/run_lg_tests.sh 第 4 步调用）：逐项检查 docs/内核迁移说明.md 第 11 节写的接口还在。
每项一行 [通过] / [失败]；有失败时退出码 1。"""
from __future__ import annotations
import os, sys, tempfile, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

fails = 0


def check(name, fn):
    global fails
    try:
        fn()
        print("[通过] " + name)
    except Exception as e:
        fails += 1
        print("[失败] %s：%s: %s" % (name, type(e).__name__, e))
        traceback.print_exc(limit=3)


def imports():
    from langchain_chroma import Chroma  # noqa: F401
    from langchain_openai import OpenAIEmbeddings  # noqa: F401
    from langchain_classic.retrievers import EnsembleRetriever  # noqa: F401
    import chromadb  # noqa: F401


def embeddings_kwargs():
    from langchain_openai import OpenAIEmbeddings
    OpenAIEmbeddings(model="m", base_url="http://127.0.0.1:1/v1", api_key="x", check_embedding_ctx_length=False, chunk_size=16)


def chroma_roundtrip():
    from fake_openai import FakeEmbedding
    from agent.config import Config
    from agent.runtime import Runtime
    from agent.llm import MockLLM
    from agent import kb as kbm
    fake = FakeEmbedding()
    tmp = tempfile.mkdtemp(prefix="ca_kblc_")
    cfg = Config(os.path.join(tmp, "home"))
    cfg.update({"kernel": "builtin", "kb": {"engine": "langchain"},
                "embedding": {"engine": "openai", "base_url": fake.base_url, "model": "fake"}})
    rt = Runtime(cfg, lambda: MockLLM([]), app_dir=os.path.dirname(HERE))
    try:
        d = os.path.join(tmp, "SOP")
        os.makedirs(d)
        with open(os.path.join(d, "a.txt"), "w", encoding="utf-8") as f:
            f.write("出差住宿：总监级每晚不超过 800 元。")
        lib = kbm.create_library(rt.store, "库")
        kbm.add_source(rt.store, lib["id"], d)
        rt.kb.indexer.enqueue(lib["id"])
        assert rt.kb.indexer.wait_idle(30)
        assert rt.kb.vectors().name == "langchain", rt.kb.vectors().name
        got = rt.kb.vectors().query("住宿多少钱", 3, [lib["id"]])
        assert got, "Chroma 没有返回结果"
    finally:
        rt.shutdown()
        fake.close()


check("导入 langchain-chroma / langchain-openai / langchain-classic / chromadb", imports)
check("OpenAIEmbeddings 接受 check_embedding_ctx_length=False", embeddings_kwargs)
check("Chroma 收录、按库过滤检索（假向量服务）", chroma_roundtrip)
sys.exit(1 if fails else 0)
