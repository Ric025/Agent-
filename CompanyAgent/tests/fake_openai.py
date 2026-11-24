"""测试用的假 OpenAI 兼容服务（docs/操控电脑.md 7.7）：/models 和 /chat/completions（流式和非流式）。
可以设成：能不能调工具、能不能看图、指位置怎么回答、上下文多大、掉线。记下每个请求。只用标准库。"""
from __future__ import annotations
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _has_image(messages):
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in c):
            return True
    return False


def _text(messages):
    out = []
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, list):
            out += [p.get("text") or "" for p in c if isinstance(p, dict)]
        elif isinstance(c, str):
            out.append(c)
    return "\n".join(out)


class FakeOpenAI:
    def __init__(self, name, model="m", tools=True, vision=False, point="", max_len=32768, reply=None,
                 vision_lies=False, fail_chat=0):
        self.name, self.model = name, model
        self.tools, self.vision, self.point, self.max_len = tools, vision, point, max_len
        self.vision_lies = vision_lies        # 探测时答得出颜色，真用图时却回 400
        self.fail_chat = fail_chat            # 正常对话请求回 500 的次数（模拟出错）
        self.reply = reply or ("来自%s的回答" % name)
        self.requests = []
        self.down = False
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj=None, raw=None, ctype="application/json"):
                body = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if fake.down:
                    self.send(503, {"error": "down"})
                    return
                if self.path.endswith("/models"):
                    self.send(200, {"data": [{"id": fake.model, "max_model_len": fake.max_len}]})
                else:
                    self.send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                if fake.down:
                    self.send(503, {"error": "down"})
                    return
                with fake.lock:
                    fake.requests.append(body)
                code, msg = fake.answer(body)
                if code != 200:
                    self.send(code, {"error": {"message": msg}})
                    return
                if body.get("stream"):
                    chunks = []
                    if msg.get("content"):
                        chunks.append({"choices": [{"index": 0, "delta": {"content": msg["content"]}}]})
                    for i, tc in enumerate(msg.get("tool_calls") or []):
                        chunks.append({"choices": [{"index": 0, "delta": {"tool_calls": [dict(tc, index=i)]}}]})
                    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
                    raw = "".join("data: %s\n\n" % json.dumps(c, ensure_ascii=False) for c in chunks) + "data: [DONE]\n\n"
                    self.send(200, raw=raw.encode("utf-8"), ctype="text/event-stream")
                else:
                    self.send(200, {"choices": [{"index": 0, "message": msg, "finish_reason": "stop"}], "usage": {}})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.base_url = "http://127.0.0.1:%d/v1" % self.port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def endpoint(self, **kw):
        ep = {"name": self.name, "base_url": self.base_url, "model": self.model, "api_key": "", "internal": True}
        ep.update(kw)
        return ep

    def answer(self, body):
        msgs = body.get("messages") or []
        text = _text(msgs)
        img = _has_image(msgs)
        probing = not body.get("stream")
        if img and (not self.vision or (self.vision_lies and not probing)):
            return 400, "The model %s is not a multimodal model" % self.model
        if img and "什么颜色" in text:
            return 200, {"role": "assistant", "content": "红色"}
        if img and "中心点的坐标" in text:
            return 200, {"role": "assistant", "content": self.point or "不知道"}
        if body.get("tools") and "get_time" in text:
            if not self.tools:
                return 200, {"role": "assistant", "content": "现在是下午三点"}
            return 200, {"role": "assistant", "content": None, "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "get_time", "arguments": "{}"}}]}
        if self.fail_chat > 0:
            self.fail_chat -= 1
            return 500, "boom"
        return 200, {"role": "assistant", "content": self.reply}

    def chats(self):
        """正常对话（流式）的请求。"""
        with self.lock:
            return [r for r in self.requests if r.get("stream")]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# ---------------- 假的向量服务和重排服务（docs/两种模式.md 第 7 节） ----------------

def hash_vector(text, dims=256):
    """字符二元组和三元组哈希成固定长度的向量：字面上相近的句子（意思相近的中文句子多半也这样）向量也相近。
    问题前面的指令（"…Query: "）去掉再算，模拟 Qwen3-Embedding 按指令理解问题。"""
    import zlib, re
    if "Query: " in text:
        text = text.split("Query: ", 1)[1]
    t = re.sub(r"[\s，。、；：！？,.;:!?（）()《》\"'“”]+", "", text.lower())
    v = [0.0] * dims
    for n in (2, 3):
        for i in range(len(t) - n + 1):
            v[zlib.crc32(t[i:i + n].encode("utf-8")) % dims] += 1.0
    return v


class FakeEmbedding:
    """OpenAI 兼容的 POST /v1/embeddings，加上 vLLM 写法的 POST /v1/rerank。记下每个请求；down=True 时回 503。
    rerank_fn(query, docs) -> 分数列表；不给时按共同的二元组个数打分。"""

    def __init__(self, dims=256, rerank_fn=None):
        self.dims = dims
        self.rerank_fn = rerank_fn
        self.requests = []
        self.down = False
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                with fake.lock:
                    fake.requests.append((self.path, body))
                if fake.down:
                    self.send(503, {"error": "down"})
                    return
                if self.path.endswith("/embeddings"):
                    inp = body.get("input")
                    inp = [inp] if isinstance(inp, str) else list(inp or [])
                    dims = int(body.get("dimensions") or fake.dims)
                    self.send(200, {"object": "list", "model": body.get("model"),
                                    "data": [{"object": "embedding", "index": i, "embedding": hash_vector(t, dims)}
                                             for i, t in enumerate(inp)]})
                elif self.path.endswith("/rerank"):
                    q, docs = body.get("query") or "", body.get("documents") or []
                    if fake.rerank_fn:
                        scores = fake.rerank_fn(q, docs)
                    else:
                        qa = hash_vector(q, 4096)
                        scores = [sum(min(a, b) for a, b in zip(qa, hash_vector(d, 4096))) for d in docs]
                    res = sorted([{"index": i, "relevance_score": float(s)} for i, s in enumerate(scores)],
                                 key=lambda x: -x["relevance_score"])
                    self.send(200, {"results": res})
                else:
                    self.send(404, {})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.base_url = "http://127.0.0.1:%d/v1" % self.port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def inputs(self):
        with self.lock:
            out = []
            for path, b in self.requests:
                if path.endswith("/embeddings"):
                    inp = b.get("input")
                    out += [inp] if isinstance(inp, str) else list(inp or [])
            return out

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
