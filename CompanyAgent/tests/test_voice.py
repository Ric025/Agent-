"""语音输入：agent/voice.py 和 /api/transcribe、/api/settings/test_asr。识别服务用本文件里的假服务代替。"""
from __future__ import annotations
import http.client, io, json, os, threading, unittest, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from helpers import Case
from agent import voice
from agent.server import make_server, MASK


def make_wav(seconds=1.0, rate=16000, channels=1, width=2, amp=0):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        n = int(seconds * rate)
        if amp:
            frame = int(amp).to_bytes(width, "little", signed=True) * channels
            w.writeframes(frame * n)
        else:
            w.writeframes(b"\x00" * width * channels * n)
    return buf.getvalue()


class FakeASR:
    """假的 OpenAI 兼容识别服务。mode 决定两个接口怎么回。"""

    def __init__(self):
        self.calls = []
        self.trans = (200, {"text": "你好，世界"})
        self.chat = (200, {"choices": [{"message": {"role": "assistant", "content": "language Chinese<asr_text>你好，世界"}}]})
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                fake.calls.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if self.path.endswith("/audio/transcriptions"):
                    code, obj = fake.trans
                elif self.path.endswith("/chat/completions"):
                    code, obj = fake.chat
                else:
                    code, obj = 404, {"error": "no"}
                data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = "http://127.0.0.1:%d/v1" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def asr_cfg(**kw):
    c = {"engine": "vllm", "base_url": "", "model": "Qwen/Qwen3-ASR-1.7B", "api_key": "", "language": "",
         "sherpa_model_dir": "", "max_seconds": 180}
    c.update(kw)
    return {"asr": c}


class Engine(unittest.TestCase):
    def setUp(self):
        self.fake = FakeASR()

    def tearDown(self):
        self.fake.close()

    def test_transcriptions_ok(self):
        r = voice.transcribe(asr_cfg(base_url=self.fake.url, api_key="k-1"), make_wav(2.0))
        self.assertEqual(r["text"], "你好，世界")
        self.assertEqual(r["engine"], "vllm")
        self.assertAlmostEqual(r["audio_seconds"], 2.0, places=2)
        self.assertIsInstance(r["seconds"], float)
        self.assertEqual(len(self.fake.calls), 1)
        call = self.fake.calls[0]
        self.assertEqual(call["path"], "/v1/audio/transcriptions")
        self.assertIn("multipart/form-data; boundary=", call["headers"]["Content-Type"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer k-1")
        self.assertIn(b'name="model"\r\n\r\nQwen/Qwen3-ASR-1.7B\r\n', call["body"])
        self.assertIn(b'name="file"; filename="audio.wav"', call["body"])
        self.assertIn(make_wav(2.0), call["body"])
        self.assertNotIn(b'name="language"', call["body"])      # 自动识别时不传语言

    def test_language_passed(self):
        voice.transcribe(asr_cfg(base_url=self.fake.url, language="zh"), make_wav())
        self.assertIn(b'name="language"\r\n\r\nzh\r\n', self.fake.calls[0]["body"])
        voice.transcribe(asr_cfg(base_url=self.fake.url, language="zh"), make_wav(), language="en")   # 参数优先
        self.assertIn(b'name="language"\r\n\r\nen\r\n', self.fake.calls[1]["body"])
        with self.assertRaises(voice.ASRError) as cm:
            voice.transcribe(asr_cfg(base_url=self.fake.url), make_wav(), language="fr")
        self.assertEqual(cm.exception.code, 400)

    def test_fallback_to_chat(self):
        for code in (404, 405):
            self.fake.calls.clear()
            self.fake.trans = (code, {"error": "Not Found"})
            r = voice.transcribe(asr_cfg(base_url=self.fake.url), make_wav())
            self.assertEqual(r["text"], "你好，世界")         # 语言前缀被去掉
            self.assertEqual([c["path"] for c in self.fake.calls], ["/v1/audio/transcriptions", "/v1/chat/completions"])
            body = json.loads(self.fake.calls[1]["body"])
            part = body["messages"][0]["content"][0]
            self.assertEqual(part["type"], "audio_url")
            self.assertTrue(part["audio_url"]["url"].startswith("data:audio/wav;base64,"))
            self.assertEqual(body["model"], "Qwen/Qwen3-ASR-1.7B")

    def test_prefix_stripped(self):
        self.fake.trans = (200, {"text": "language English<asr_text>Hello, Q3 revenue."})
        self.assertEqual(voice.transcribe(asr_cfg(base_url=self.fake.url), make_wav())["text"], "Hello, Q3 revenue.")
        self.fake.trans = (200, {"text": "language None<asr_text>"})
        self.assertEqual(voice.transcribe(asr_cfg(base_url=self.fake.url), make_wav())["text"], "")
        self.assertEqual(voice.clean_text("<|zh|><|NEUTRAL|><|Speech|><|withitn|>开会。"), "开会。")
        self.assertEqual(voice.clean_text("language Chinese"), "")
        self.assertEqual(voice.clean_text("普通的一句话"), "普通的一句话")

    def test_service_errors_are_502(self):
        self.fake.trans = (500, {"error": "boom"})
        with self.assertRaises(voice.ASRError) as cm:
            voice.transcribe(asr_cfg(base_url=self.fake.url), make_wav())
        self.assertEqual(cm.exception.code, 502)
        self.fake.trans = (401, {"error": "bad key"})
        with self.assertRaises(voice.ASRError) as cm:
            voice.transcribe(asr_cfg(base_url=self.fake.url), make_wav())
        self.assertEqual(cm.exception.code, 502)
        self.assertIn("密钥", cm.exception.msg)
        url = self.fake.url
        self.fake.close()
        with self.assertRaises(voice.ASRError) as cm:
            voice.transcribe(asr_cfg(base_url=url), make_wav())
        self.assertEqual(cm.exception.code, 502)
        self.assertIn("连不上", cm.exception.msg)
        self.fake = FakeASR()

    def test_off_and_unconfigured_are_409(self):
        for cfg in (asr_cfg(engine="off"), {"asr": {}}, {}, asr_cfg(base_url=""), asr_cfg(engine="weird")):
            with self.assertRaises(voice.ASRError) as cm:
                voice.transcribe(cfg, make_wav())
            self.assertEqual(cm.exception.code, 409, cfg)
            self.assertIn("设置", cm.exception.msg)

    def test_sherpa_missing_package(self):
        try:
            import sherpa_onnx  # noqa
            self.skipTest("装了 sherpa-onnx")
        except ImportError:
            pass
        with self.assertRaises(voice.ASRError) as cm:
            voice.transcribe(asr_cfg(engine="sherpa", sherpa_model_dir="/nope"), make_wav())
        self.assertEqual(cm.exception.code, 409)
        self.assertIn("sherpa-onnx", cm.exception.msg)

    def test_bad_audio_rejected(self):
        cfg = asr_cfg(base_url=self.fake.url, max_seconds=10)
        bad = [b"", b"not a wav at all", b"RIFF\x00\x00\x00\x00WAVEfmt ", make_wav(width=1), make_wav(seconds=0),
               make_wav(seconds=11), b"\x00" * (voice.MAX_BYTES + 1)]
        for data in bad:
            with self.assertRaises(voice.ASRError) as cm:
                voice.transcribe(cfg, data)
            self.assertEqual(cm.exception.code, 400, data[:20])
        self.assertEqual(self.fake.calls, [])       # 都没有发到识别服务
        self.assertIn("太长", self.assert_msg(cfg, make_wav(seconds=11)))
        self.assertIn("20 MB", self.assert_msg(cfg, b"\x00" * (voice.MAX_BYTES + 1)))
        # 双声道 16 位可以
        self.assertEqual(voice.transcribe(cfg, make_wav(channels=2))["text"], "你好，世界")

    def assert_msg(self, cfg, data):
        try:
            voice.transcribe(cfg, data)
        except voice.ASRError as e:
            return e.msg
        return ""

    def test_wav_samples(self):
        rate, s = voice.wav_samples(make_wav(0.01, 16000, 1, 2, amp=16384))
        self.assertEqual((rate, len(s)), (16000, 160))
        self.assertAlmostEqual(s[0], 0.5)
        rate, s = voice.wav_samples(make_wav(0.01, 48000, 2, 2, amp=-16384))
        self.assertEqual((rate, len(s)), (48000, 480))
        self.assertAlmostEqual(s[0], -0.5)

    def test_probe(self):
        r = voice.probe_engine(asr_cfg(base_url=self.fake.url))
        self.assertTrue(r["ok"])
        self.assertIn("连上了", r["detail"])
        r = voice.probe_engine(asr_cfg(engine="off"))
        self.assertFalse(r["ok"])


def snapshot(*roots):
    out = set()
    for root in roots:
        for d, _, files in os.walk(root):
            for f in files:
                out.add(os.path.join(d, f))
    return out


class Api(Case):
    def setUp(self):
        super().setUp()
        self.fake = FakeASR()
        self.e = self.env([])
        self.e.cfg.update({"asr": {"engine": "vllm", "base_url": self.fake.url, "api_key": "asr-secret-987654"}})
        self.srv = make_server(self.e.rt, 0)
        self.srv.start()
        self.port, self.tok = self.srv.port, self.srv.token

    def tearDown(self):
        self.srv.stop()
        self.fake.close()
        super().tearDown()

    def req(self, method, path, body=None, token=True, ctype=None, origin=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest(method, path, skip_host=True)
        c.putheader("Host", "127.0.0.1:%d" % self.port)
        if token:
            c.putheader("X-Token", self.tok)
        if origin:
            c.putheader("Origin", origin)
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            c.putheader("Content-Type", ctype or "application/json")
            c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        payload = r.read()
        c.close()
        return r.status, (json.loads(payload.decode("utf-8")) if payload else None)

    def wav(self, path="/api/transcribe", data=None, **kw):
        return self.req("POST", path, data if data is not None else make_wav(1.5), ctype=kw.pop("ctype", "audio/wav"), **kw)

    def test_transcribe_ok(self):
        st, r = self.wav()
        self.assertEqual(st, 200, r)
        self.assertEqual(r["text"], "你好，世界")
        self.assertEqual(r["engine"], "vllm")
        self.assertAlmostEqual(r["audio_seconds"], 1.5)
        self.assertIn("seconds", r)
        st, r = self.wav("/api/transcribe?lang=en")
        self.assertEqual(st, 200)
        self.assertIn(b'name="language"\r\n\r\nen\r\n', self.fake.calls[-1]["body"])
        self.assertEqual(self.wav("/api/transcribe?lang=xx")[0], 400)

    def test_transcribe_guards(self):
        self.assertEqual(self.wav(token=False)[0], 403)
        self.assertEqual(self.wav(origin="http://evil.com")[0], 403)
        self.assertEqual(self.wav(ctype="application/json")[0], 415)
        st, r = self.wav(data=b"hello, not a wav")
        self.assertEqual(st, 400)
        self.assertIn("WAV", r["error"])
        self.assertEqual(self.req("GET", "/api/transcribe")[0], 405)
        self.assertEqual(self.fake.calls, [])

    def test_transcribe_errors(self):
        self.e.cfg.update({"asr": {"max_seconds": 5}})
        st, r = self.wav(data=make_wav(6))
        self.assertEqual(st, 400)
        self.assertIn("太长", r["error"])
        self.fake.trans = (503, {"error": "busy"})
        st, r = self.wav()
        self.assertEqual(st, 502)
        self.assertTrue(r["error"])
        self.fake.trans = (404, {"error": "nope"})
        st, r = self.wav()
        self.assertEqual((st, r["text"]), (200, "你好，世界"))
        self.e.cfg.update({"asr": {"engine": "off"}})
        st, r = self.wav()
        self.assertEqual(st, 409)
        self.assertIn("设置", r["error"])

    def test_no_files_written(self):
        before = snapshot(self.e.home, self.e.root)
        for _ in range(3):
            self.assertEqual(self.wav()[0], 200)
        self.fake.trans = (404, {})
        self.assertEqual(self.wav()[0], 200)
        self.assertEqual(self.wav(data=b"junk")[0], 400)
        self.assertEqual(self.req("POST", "/api/settings/test_asr", {})[0], 200)
        after = snapshot(self.e.home, self.e.root)
        self.assertEqual(sorted(after - before), [])
        audit = self.e.rt.store.q("SELECT COUNT(*) AS n FROM audit")[0]["n"]
        self.assertEqual(audit, 0)

    def test_state_has_asr_and_kernel(self):
        st, s = self.req("GET", "/api/state")
        self.assertEqual(st, 200)
        self.assertEqual(s["asr_engine"], "vllm")
        self.assertEqual(s["asr_max_seconds"], 180)
        self.assertEqual(s["kernel"], getattr(self.e.rt, "kernel_name", "builtin"))
        self.e.rt.kernel_name = "langgraph"
        self.assertEqual(self.req("GET", "/api/state")[1]["kernel"], "langgraph")

    def test_settings_mask_and_keep_key(self):
        st, d = self.req("GET", "/api/settings")
        asr = d["config"]["asr"]
        self.assertTrue(asr["api_key"].startswith(MASK))
        self.assertNotIn("asr-secret", json.dumps(d))
        asr["model"] = "Qwen/Qwen3-ASR-0.6B"
        asr["language"] = "zh"
        self.assertEqual(self.req("POST", "/api/settings", {"asr": asr})[0], 200)
        self.assertEqual(self.e.cfg.get("asr", "api_key"), "asr-secret-987654")
        self.assertEqual(self.e.cfg.get("asr", "model"), "Qwen/Qwen3-ASR-0.6B")
        self.assertEqual(self.e.cfg.get("asr", "language"), "zh")
        self.assertEqual(self.req("POST", "/api/settings", {"asr": {"api_key": "new-key"}})[0], 200)
        self.assertEqual(self.e.cfg.get("asr", "api_key"), "new-key")
        for bad in ({"engine": "cloud"}, {"language": "fr"}, {"max_seconds": 9999}, {"oops": 1}, {"base_url": 3}, "x"):
            self.assertEqual(self.req("POST", "/api/settings", {"asr": bad})[0], 400, bad)

    def test_settings_test_asr(self):
        st, r = self.req("POST", "/api/settings/test_asr", {})
        self.assertEqual(st, 200)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.fake.calls[-1]["headers"]["Authorization"], "Bearer asr-secret-987654")
        # 测试还没保存的设置：打码的密钥沿用原值，且不保存
        st, r = self.req("POST", "/api/settings/test_asr", {"asr": {"engine": "vllm", "base_url": self.fake.url,
                                                                    "api_key": MASK + "7654", "model": "m2"}})
        self.assertTrue(r["ok"])
        self.assertEqual(self.fake.calls[-1]["headers"]["Authorization"], "Bearer asr-secret-987654")
        self.assertEqual(self.e.cfg.get("asr", "model"), "Qwen/Qwen3-ASR-1.7B")
        st, r = self.req("POST", "/api/settings/test_asr", {"asr": {"engine": "off"}})
        self.assertEqual(st, 200)
        self.assertFalse(r["ok"])
        self.assertEqual(self.req("POST", "/api/settings/test_asr", {}, token=False)[0], 403)


if __name__ == "__main__":
    unittest.main()
