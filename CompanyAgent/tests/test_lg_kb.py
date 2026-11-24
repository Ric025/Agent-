"""新内核（LangChain / LangGraph）上的两种模式和资料工具（docs/两种模式.md）；以及 kb.engine=langchain 的向量库。
没装对应的包时跳过。依赖的接口和假设见 docs/内核迁移说明.md 第 11 节。"""
from __future__ import annotations
import json, os, unittest

from helpers import Case, HAS_LG, LG_SKIP
from fake_openai import FakeEmbedding
from agent import kb as kbm, modes
from agent.kb import retrieve, vectors

HAS_LC_KB = vectors.has_langchain()


def call(name, args):
    return {"content": "", "tool_calls": [(name, args)]}


def _lib(e, files):
    d = os.path.join(e.tmp, "SOP")
    os.makedirs(d, exist_ok=True)
    for rel, text in files.items():
        with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
            f.write(text)
    lib = kbm.create_library(e.rt.store, "公司 SOP")
    kbm.add_source(e.rt.store, lib["id"], d)
    e.rt.kb.indexer.enqueue(lib["id"])
    assert e.rt.kb.indexer.wait_idle(20)
    return lib


@unittest.skipUnless(HAS_LG, LG_SKIP)
class LGModes(Case):
    def test_hspace_prompt_hits_and_keep_two(self):
        seen = []

        def last(m, t):
            seen.append(m)
            return {"content": "住宿标准见 [1]"}
        qs = ["住宿标准", "报销时限", "交通标准", "会议纪要"]
        e = self.lg_env([call("kb_search", {"query": q}) for q in qs] + [last])
        _lib(e, {"a.txt": "住宿标准：总监每晚 800 元。", "b.txt": "报销时限：三十天内。", "c.txt": "交通标准：经济舱。",
                 "d.txt": "会议纪要三天内发出。"})
        sid = e.rt.store.add_session(e.ws["id"], mode="hspace")["id"]
        e.rt.send_message(sid, "查一下")
        self.assertTrue(e.rt.wait_idle(sid, 30))
        sysmsg = json.dumps(seen[-1][0], ensure_ascii=False)
        self.assertIn("你在 Hspace 里", sysmsg)
        res = e.results(sid)
        self.assertTrue(all(r.get("hits") for r in res))
        self.assertEqual(res[0]["display"]["kind"], "kb")
        full = [m for m in seen[-1] if m.get("role") == "tool" and retrieve.BEGIN in (m.get("content") or "")]
        self.assertEqual(len(full), 2)

    def test_read_only(self):
        e = self.lg_env([call("write_file", {"path": "SOP/a.txt", "content": "x"}), {"content": "好"}])
        d = os.path.join(e.root, "SOP")
        os.makedirs(d)
        with open(os.path.join(d, "a.txt"), "w", encoding="utf-8") as f:
            f.write("原文")
        lib = kbm.create_library(e.rt.store, "库")
        kbm.add_source(e.rt.store, lib["id"], d)
        sid = e.start("改一下")
        self.assertTrue(e.rt.wait_idle(sid, 30))
        r = e.results(sid)[0]
        self.assertFalse(r["ok"])
        self.assertIn("只读", r["content"])


@unittest.skipUnless(HAS_LC_KB, "没有安装 langchain-chroma、langchain-openai、langchain-classic、chromadb")
class LangchainVectors(Case):
    def test_add_query_delete(self):
        fake = FakeEmbedding()
        try:
            e = self.env([], embedding={"engine": "openai", "base_url": fake.base_url, "model": "fake"}, kb={"engine": "langchain"})
            self.assertEqual(e.rt.kb.vectors().name, "langchain")
            lib = _lib(e, {"住宿.txt": "出差住宿：总监级每晚不超过 800 元。", "会议.txt": "会议纪要要在三天内发出。"})
            n = e.rt.store.one("SELECT COUNT(*) AS n FROM kb_vectors")["n"]
            self.assertEqual(n, e.rt.store.one("SELECT COUNT(*) AS n FROM kb_parts")["n"])
            got = e.rt.kb.vectors().query("住宿多少钱", 5, [lib["id"]])
            top = e.rt.store.one("SELECT d.title FROM kb_parts p JOIN kb_docs d ON d.id=p.doc_id WHERE p.id=?", (got[0][0],))
            self.assertEqual(top["title"], "住宿.txt")
            self.assertTrue(any(x.startswith("Instruct:") for x in fake.inputs()))      # 问题加了前缀
            docs = [r["id"] for r in e.rt.store.q("SELECT id FROM kb_docs")]
            e.rt.kb.vectors().delete_docs(docs)
            self.assertEqual(e.rt.kb.vectors().query("住宿多少钱", 5, [lib["id"]]), [])
        finally:
            fake.close()


if __name__ == "__main__":
    unittest.main()
