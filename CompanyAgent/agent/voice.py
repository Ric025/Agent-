"""语音输入：把一段 WAV 录音转成文字。只用标准库（本机识别时才需要 sherpa-onnx）。

两种引擎，在设置的 asr.engine 里切换：
  vllm    公司内网用 vLLM 提供的识别服务（例如 Qwen3-ASR），OpenAI 兼容接口。
          先用 /audio/transcriptions（multipart：model、file），服务返回 404/405 时
          改用 /chat/completions，音频放在 audio_url 里（data URL）。
  sherpa  本机用 sherpa-onnx 跑 SenseVoice，离线识别。
  off     关闭。

音频只在内存里处理：不写文件，不进账本，不进审计。
"""
from __future__ import annotations
import base64, io, json, os, re, secrets, socket, sys, threading, time, wave
import urllib.error, urllib.request

MAX_BYTES = 20 * 1024 * 1024        # 20 MB
DEFAULT_MAX_SECONDS = 180
TIMEOUT = 60                        # 每次请求识别服务的超时（秒）
ENGINES = ("off", "vllm", "sherpa")
LANGS = ("", "zh", "en")
LANG_NAMES = {"zh": "Chinese", "en": "English"}

MSG_OFF = "语音输入还没有开启。请到「设置 → 语音识别」里选择识别方式。"
MSG_NO_URL = "还没有填写识别服务的地址。请到「设置 → 语音识别」里填写。"
MSG_SHERPA = "本机识别需要安装 sherpa-onnx，并把 SenseVoice 模型放到设置里指定的目录。"


class ASRError(Exception):
    """code 是给 HTTP 用的状态码：409 没配置好，400 音频不合法，502 识别服务不可用。"""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


# ---------------- 配置 ----------------

def asr_conf(cfg):
    """从 Config（或带 "asr" 键的字典）里取出语音识别配置，补齐默认值。"""
    raw = cfg.get("asr") if cfg is not None else None
    c = dict(raw) if isinstance(raw, dict) else {}
    c.setdefault("engine", "off")
    for k in ("base_url", "model", "api_key", "language", "sherpa_model_dir"):
        c[k] = str(c.get(k) or "").strip()
    try:
        c["max_seconds"] = max(1, int(c.get("max_seconds") or DEFAULT_MAX_SECONDS))
    except (TypeError, ValueError):
        c["max_seconds"] = DEFAULT_MAX_SECONDS
    return c


def norm_language(lang):
    """'' / auto → 自动识别；zh、en 及其常见写法 → zh、en。其他值返回 None 表示不认识。"""
    s = str(lang or "").strip().lower()
    if s in ("", "auto"):
        return ""
    if s in ("zh", "zh-cn", "zh_cn", "cn", "chinese", "中文"):
        return "zh"
    if s in ("en", "en-us", "en_us", "english", "英文"):
        return "en"
    return None


# ---------------- WAV ----------------

def check_wav(data, max_seconds=DEFAULT_MAX_SECONDS):
    """校验是 16 位 PCM 的 WAV，返回 (时长秒, 采样率, 声道数)。不合格抛 ASRError(400)。"""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ASRError(400, "没有收到录音。")
    if len(data) > MAX_BYTES:
        raise ASRError(400, "录音太大（超过 20 MB），请分几段说。")
    try:
        with wave.open(io.BytesIO(bytes(data)), "rb") as w:
            ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
    except (wave.Error, EOFError, ValueError, OSError):
        raise ASRError(400, "收到的不是合法的 WAV 录音。")
    if width != 2 or ch not in (1, 2) or not (8000 <= rate <= 192000):
        raise ASRError(400, "录音格式不支持：需要 16 位 PCM、单声道或双声道的 WAV。")
    if n <= 0:
        raise ASRError(400, "录音是空的。")
    secs = n / float(rate)
    if secs > max_seconds + 0.5:
        raise ASRError(400, "录音太长（%d 秒），一次最多 %d 秒，请分几段说。" % (round(secs), max_seconds))
    return secs, rate, ch


def silence_wav(seconds=1.0, rate=16000):
    """程序生成的静音 WAV，用来测试识别服务通不通。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def wav_samples(data):
    """WAV → (采样率, [-1, 1] 的浮点样本列表，单声道)。给本机识别用。"""
    import array
    with wave.open(io.BytesIO(bytes(data)), "rb") as w:
        ch, rate = w.getnchannels(), w.getframerate()
        frames = w.readframes(w.getnframes())
    a = array.array("h")
    a.frombytes(frames[: len(frames) // 2 * 2])
    if sys.byteorder == "big":          # WAV 是小端
        a.byteswap()
    if ch == 2:
        return rate, [(a[i] + a[i + 1]) / 65536.0 for i in range(0, len(a) - 1, 2)]
    return rate, [x / 32768.0 for x in a]


# ---------------- 识别结果的清理 ----------------

_RE_LANG_PREFIX = re.compile(r"^\s*language\s+[A-Za-z_\-]*\s*", re.I)
_RE_SPECIAL = re.compile(r"<\|[^|<>]{0,40}\|>")


def clean_text(s):
    """去掉识别结果里的标记。

    Qwen3-ASR 的原始输出形如 "language Chinese<asr_text>你好"（没说话时可能只有 "language None<asr_text>"），
    只要 <asr_text> 后面的部分。SenseVoice 的 <|zh|><|NEUTRAL|> 这类标签也去掉。
    """
    s = str(s or "")
    if "<asr_text>" in s:
        s = s.split("<asr_text>", 1)[1]
    elif _RE_LANG_PREFIX.match(s) and re.fullmatch(r"\s*language\s+\S*\s*", s, re.I):
        s = ""                           # 只有语言标记，没有文字
    s = s.replace("</asr_text>", "")
    s = _RE_SPECIAL.sub("", s)
    return s.strip()


# ---------------- 引擎：内网识别服务（vLLM） ----------------

def _opener():
    # 识别服务在内网：不走系统代理（和 llm.py 对内网端点的做法一致）
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _multipart(fields, file_field, filename, data, ctype):
    boundary = "----ca" + secrets.token_hex(12)
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode("utf-8"))
    out.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
               % (boundary, file_field, filename, ctype)).encode("utf-8"))
    out.write(data)
    out.write(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    return out.getvalue(), "multipart/form-data; boundary=" + boundary


def _post(url, body, ctype, api_key):
    headers = {"Content-Type": ctype, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with _opener().open(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise ASRError(502, "识别服务返回的内容看不懂（不是 JSON）。")


def _http_fail(e, base):
    detail = ""
    try:
        detail = e.read().decode("utf-8", "replace")[:300]
    except Exception:
        pass
    if e.code in (401, 403):
        return ASRError(502, "识别服务拒绝了请求（HTTP %d），请检查设置里的密钥。" % e.code)
    return ASRError(502, "识别服务返回错误（HTTP %d）%s" % (e.code, "：" + detail if detail else "。"))


def _vllm(c, data, lang):
    base = c["base_url"].rstrip("/")
    if not base:
        raise ASRError(409, MSG_NO_URL)
    if not re.match(r"^https?://", base, re.I):
        raise ASRError(409, "识别服务地址要以 http:// 或 https:// 开头。")
    model = c["model"] or "Qwen/Qwen3-ASR-1.7B"
    fields = {"model": model, "response_format": "json"}
    if lang:
        fields["language"] = lang          # OpenAI 转写接口的语言参数（ISO-639-1）
    body, ctype = _multipart(fields, "file", "audio.wav", data, "audio/wav")
    try:
        try:
            j = _post(base + "/audio/transcriptions", body, ctype, c["api_key"])
            if not isinstance(j, dict) or "text" not in j:
                raise ASRError(502, "识别服务的回复里没有文字。")
            return clean_text(j.get("text"))
        except urllib.error.HTTPError as e:
            if e.code not in (404, 405):
                raise _http_fail(e, base)
        # 服务没有转写接口：改用 chat completions + audio_url
        content = [{"type": "audio_url", "audio_url": {"url": "data:audio/wav;base64," + base64.b64encode(data).decode("ascii")}}]
        msgs = [{"role": "user", "content": content}]
        payload = {"model": model, "messages": msgs, "temperature": 0, "max_tokens": 2048}
        try:
            j = _post(base + "/chat/completions", json.dumps(payload).encode("utf-8"), "application/json", c["api_key"])
        except urllib.error.HTTPError as e:
            raise _http_fail(e, base)
        try:
            text = j["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ASRError(502, "识别服务的回复里没有文字。")
        if isinstance(text, list):          # 少数服务把 content 返回成分段列表
            text = "".join(str(p.get("text") or "") for p in text if isinstance(p, dict))
        return clean_text(text)
    except ASRError:
        raise
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        raise ASRError(502, "连不上识别服务 %s：%s" % (base, reason))


# ---------------- 引擎：本机识别（sherpa-onnx + SenseVoice） ----------------
# 按 sherpa-onnx 官方示例 python-api-examples/offline-sense-voice-ctc-decode-files.py 写。未在本项目环境里运行过。

_SHERPA = {"key": None, "rec": None}
_SHERPA_LOCK = threading.Lock()


def _sherpa_recognizer(c, lang):
    try:
        import sherpa_onnx  # noqa: 只在用到时导入
    except ImportError:
        raise ASRError(409, MSG_SHERPA)
    d = os.path.expanduser(c["sherpa_model_dir"])
    if not d or not os.path.isdir(d):
        raise ASRError(409, MSG_SHERPA + (" 现在填的目录不存在：%s" % d if d else " 现在还没有填写模型目录。"))
    model = next((os.path.join(d, n) for n in ("model.int8.onnx", "model.onnx") if os.path.isfile(os.path.join(d, n))), None)
    tokens = os.path.join(d, "tokens.txt")
    if not model or not os.path.isfile(tokens):
        raise ASRError(409, MSG_SHERPA + " 目录里要有 model.int8.onnx（或 model.onnx）和 tokens.txt。")
    key = (model, tokens, lang)
    if _SHERPA["key"] == key and _SHERPA["rec"] is not None:
        return _SHERPA["rec"]
    kw = dict(model=model, tokens=tokens, num_threads=2, use_itn=True, debug=False)
    try:
        rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(language=lang or "auto", **kw)
    except TypeError:                     # 旧版本没有 language 参数
        rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(**kw)
    except Exception as e:
        raise ASRError(409, "本机识别模型加载失败：%s" % e)
    _SHERPA["key"], _SHERPA["rec"] = key, rec
    return rec


def _sherpa(c, data, lang):
    rate, samples = wav_samples(data)
    with _SHERPA_LOCK:                    # 识别器建一次后缓存；同一时间只做一段
        rec = _sherpa_recognizer(c, lang)
        try:
            try:
                import numpy as np
                samples = np.asarray(samples, dtype=np.float32)
            except ImportError:
                pass
            stream = rec.create_stream()
            stream.accept_waveform(rate, samples)
            rec.decode_stream(stream)
            res = stream.result
        except ASRError:
            raise
        except Exception as e:
            raise ASRError(502, "本机识别出错：%s" % e)
    return clean_text(getattr(res, "text", res))


# ---------------- 入口 ----------------

def transcribe(cfg, wav_bytes, language=None):
    """把 WAV 转成文字。返回 {text, engine, seconds, audio_seconds}；出错抛 ASRError。"""
    c = asr_conf(cfg)
    engine = str(c.get("engine") or "off")
    if engine not in ENGINES:
        raise ASRError(409, "不认识的识别方式「%s」。请到「设置 → 语音识别」里重新选择。" % engine)
    if engine == "off":
        raise ASRError(409, MSG_OFF)
    audio_seconds = check_wav(wav_bytes, c["max_seconds"])[0]
    lang = norm_language(language if language not in (None, "") else c.get("language"))
    if lang is None:
        raise ASRError(400, "语言只能是自动、zh 或 en。")
    t0 = time.monotonic()
    text = _vllm(c, bytes(wav_bytes), lang) if engine == "vllm" else _sherpa(c, bytes(wav_bytes), lang)
    return {"text": text, "engine": engine, "seconds": round(time.monotonic() - t0, 2),
            "audio_seconds": round(audio_seconds, 2)}


def probe_engine(cfg):
    """用 1 秒静音走一遍当前引擎。返回 {ok, engine, detail}。"""
    c = asr_conf(cfg)
    try:
        r = transcribe(cfg, silence_wav(1.0))
    except ASRError as e:
        return {"ok": False, "engine": c["engine"], "detail": e.msg}
    return {"ok": True, "engine": r["engine"], "seconds": r["seconds"],
            "detail": "连上了，用时 %.1f 秒。（测试用的是一段静音，识别结果为空是正常的。）" % r["seconds"]}
