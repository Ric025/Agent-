"""向量模型和重排模型的客户端（docs/两种模式.md 2.4）。只用标准库。

- 向量：OpenAI 兼容的 POST {base_url}/embeddings（vLLM 起 Qwen3-Embedding 就是这个接口）。
  query_prefix 只加在问题前面，文档片段不加（Qwen3-Embedding 是这么要求的）。dims > 0 时请求里带 dimensions。
- 重排：POST {base_url}/rerank（vLLM 的写法，和 Jina / Cohere 兼容），返回 results[{index, relevance_score}]。"""
from __future__ import annotations
import json, socket, time, urllib.error, urllib.request

DEFAULT_PREFIX = "Instruct: 根据问题找出能回答它的公司资料片段\nQuery: "
TIMEOUT = 30


class EmbedError(Exception):
    pass


def conf(cfg, override=None):
    e = dict(cfg.get("embedding") or {}) if hasattr(cfg, "get") else dict(cfg or {})
    if override:
        e.update(override)
    out = {"engine": str(e.get("engine") or "off").lower(), "base_url": str(e.get("base_url") or "").strip().rstrip("/"),
           "model": str(e.get("model") or "").strip(), "api_key": str(e.get("api_key") or ""),
           "query_prefix": DEFAULT_PREFIX if e.get("query_prefix") is None else str(e.get("query_prefix"))}
    try:
        out["dims"] = max(0, int(e.get("dims") or 0))
    except (TypeError, ValueError):
        out["dims"] = 0
    try:
        out["batch"] = max(1, min(256, int(e.get("batch") or 16)))
    except (TypeError, ValueError):
        out["batch"] = 16
    out["internal"] = e.get("internal", True) is not False
    return out


def configured(cfg, override=None):
    c = conf(cfg, override)
    return c["engine"] == "openai" and bool(c["base_url"]) and bool(c["model"])


# 算向量用的文字是怎么拼的（ingest.index_text）。改了拼法就加一：签名变了，旧向量作废、后台重算（和换向量模型同一套机制）。
#   1：标题路径 + 正文（第九轮）
#   2：文件名（去扩展名）+ 标题路径 + 正文（docs/借鉴开源项目.md 2.1）
TEXT_VERSION = 2


def signature(cfg):
    """向量是用哪个模型、多长、什么样的文字算出来的：变了旧向量作废。"""
    c = conf(cfg)
    return "%s|%s|%s|t%d" % (c["base_url"], c["model"], c["dims"], TEXT_VERSION) if configured(cfg) else ""


def rerank_conf(cfg):
    r = dict(cfg.get("rerank") or {})
    return {"engine": str(r.get("engine") or "off").lower(), "base_url": str(r.get("base_url") or "").strip().rstrip("/"),
            "model": str(r.get("model") or "").strip(), "api_key": str(r.get("api_key") or ""),
            "internal": r.get("internal", True) is not False}


def rerank_on(cfg):
    r = rerank_conf(cfg)
    return r["engine"] == "vllm" and bool(r["base_url"]) and bool(r["model"])


def _post(c, path, body, timeout):
    h = {"Content-Type": "application/json"}
    if c.get("api_key"):
        h["Authorization"] = "Bearer " + c["api_key"]
    req = urllib.request.Request(c["base_url"] + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=h)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if c.get("internal", True) else urllib.request.build_opener()
    try:
        raw = opener.open(req, timeout=timeout).read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        raise EmbedError("服务回了 %d：%s" % (e.code, detail))
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        raise EmbedError("连不上：%s" % e)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise EmbedError("返回的不是 JSON")


def embed(cfg, texts, query=False, override=None, timeout=TIMEOUT):
    """文字 → 向量列表（和 texts 一一对应）。query=True 时每条前面加 query_prefix。按 batch 分批发。"""
    c = conf(cfg, override)
    if not configured(cfg, override):
        raise EmbedError("还没有配置向量模型")
    texts = [(c["query_prefix"] + t) if query else t for t in texts]
    out = []
    for i in range(0, len(texts), c["batch"]):
        body = {"model": c["model"], "input": texts[i:i + c["batch"]]}
        if c["dims"]:
            body["dimensions"] = c["dims"]
        j = _post(c, "/embeddings", body, timeout)
        data = j.get("data") if isinstance(j, dict) else None
        if not isinstance(data, list) or len(data) != len(body["input"]):
            raise EmbedError("返回的向量个数不对")
        data = sorted(data, key=lambda d: d.get("index", 0) if isinstance(d, dict) else 0)
        for d in data:
            v = d.get("embedding") if isinstance(d, dict) else None
            if not isinstance(v, list) or not v:
                raise EmbedError("返回里没有向量")
            out.append([float(x) for x in v])
    return out


def probe(cfg, override=None):
    """POST /api/settings/test_embedding：试着算一个向量。{"ok", "dims", "ms", "error"}。"""
    t0 = time.time()
    try:
        v = embed(cfg, ["测试"], query=False, override=override, timeout=15)
        return {"ok": True, "dims": len(v[0]), "ms": int((time.time() - t0) * 1000), "error": ""}
    except EmbedError as e:
        return {"ok": False, "dims": 0, "ms": int((time.time() - t0) * 1000), "error": str(e)}


def rerank(cfg, query, docs, timeout=TIMEOUT):
    """重排：返回和 docs 一一对应的分数。出错抛 EmbedError。"""
    r = rerank_conf(cfg)
    j = _post(r, "/rerank", {"model": r["model"], "query": query, "documents": docs, "top_n": len(docs)}, timeout)
    res = j.get("results") if isinstance(j, dict) else None
    if not isinstance(res, list):
        raise EmbedError("重排服务返回的格式不对")
    scores = [0.0] * len(docs)
    for x in res:
        if isinstance(x, dict) and isinstance(x.get("index"), int) and 0 <= x["index"] < len(docs):
            scores[x["index"]] = float(x.get("relevance_score", x.get("score", 0.0)) or 0.0)
    return scores
