"""这台电脑能不能跑、哪些功能能用：逐项检查，最后给出"能用什么、哪里有问题、怎么办"。

用法（在 CompanyAgent 目录下）：
    python setup/check_env.py              只在终端里显示
    python setup/check_env.py --write      另外写进 setup/环境检查结果.txt 和数据目录的 环境检查.json
    python setup/check_env.py --smoke      装好了 LangChain 时，再跑一遍新内核自检（tests/lg_smoke.py，最多 3 分钟）
    python setup/check_env.py --home 目录  数据目录不是默认的 ~/.companyagent 时

一般不用单独运行：setup/install.sh（Windows 是 install.ps1）装完会自动跑；start.sh --check 也是它。
只用标准库，Python 3.8 以上都能跑（装依赖之前也能用来看问题出在哪）。
退出码：0 没有必须处理的问题；1 有（程序没法正常用）。

新内核自检的结果记在 .tools/setup-state.json 的 lg_smoke 里。没通过时，程序在 kernel=auto 下自动先用过渡内核
（agent/runtime.py 读这个结果），免得一个没在这台电脑上验证过的内核让人用不了。"""
from __future__ import annotations
import argparse, json, os, platform, re, shutil, socket, sqlite3, subprocess, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE = os.path.join(ROOT, ".tools", "setup-state.json")
REPORT_TXT = os.path.join(HERE, "环境检查结果.txt")
sys.path.insert(0, ROOT)

GOOD, WARN, BAD, OFF, INFO = "好", "注意", "不行", "没配", "信息"
TAG = {GOOD: "  好  ", WARN: " 注意 ", BAD: " 不行 ", OFF: " 没配 ", INFO: "      "}

# 必需的依赖：(导入名, 包名, 用来做什么)
CORE = [
    ("langchain", "langchain", "新内核"), ("langgraph", "langgraph", "新内核"),
    ("langgraph.checkpoint.sqlite", "langgraph-checkpoint-sqlite", "新内核的断点续跑"),
    ("langchain_openai", "langchain-openai", "新内核接模型"), ("httpx", "httpx", "新内核接模型"),
    ("langchain_mcp_adapters", "langchain-mcp-adapters", "连接器（MCP）"),
    ("openpyxl", "openpyxl", "读写 Excel"), ("docx", "python-docx", "读写 Word"), ("pptx", "python-pptx", "读写 PPT"),
    ("pypdf", "pypdf", "读 PDF"), ("numpy", "numpy", "资料库的向量"),
]
EXTRA = [
    ("playwright", "playwright", "操作网页"), ("chromadb", "chromadb", "向量存进 Chroma（可选，不装也能语义检索）"),
    ("langchain_chroma", "langchain-chroma", "同上"), ("langchain_classic", "langchain-classic", "同上"),
]


class Report:
    def __init__(self):
        self.sections = []          # [(标题, [(状态, 名字, 说明, 怎么办)])]
        self.features = []          # [(功能, 状态, 说明)]

    def section(self, title):
        self.sections.append((title, []))

    def add(self, status, name, detail, fix=""):
        self.sections[-1][1].append((status, name, detail, fix))

    def feature(self, name, status, detail=""):
        self.features.append((name, status, detail))

    def problems(self):
        return [x for _, items in self.sections for x in items if x[0] in (BAD, WARN)]

    def text(self):
        out = ["环境检查  %s  （setup/check_env.py）" % time.strftime("%Y-%m-%d %H:%M:%S"), ""]
        for title, items in self.sections:
            out.append("==== %s ====" % title)
            for st, name, detail, fix in items:
                out.append("[%s] %s：%s" % (TAG[st], name, detail))
                if fix:
                    out.append("         → %s" % fix)
            out.append("")
        out.append("==== 结论 ====")
        ok = [n for n, st, _ in self.features if st == GOOD]
        out.append("能用：" + ("、".join(ok) if ok else "（没有）"))
        rest = [(n, st, d) for n, st, d in self.features if st != GOOD]
        if rest:
            out.append("受限或用不了：")
            for n, st, d in rest:
                out.append("  [%s] %s：%s" % (TAG[st], n, d))
        probs = self.problems()
        bad = [p for p in probs if p[0] == BAD]
        out.append("")
        if bad:
            out.append("有 %d 处必须处理的问题（标了\"不行\"的），处理办法写在每一项下面的 → 后面。" % len(bad))
        elif probs:
            out.append("没有必须处理的问题。标了\"注意\"的 %d 项会让某些功能用不了或变慢，见上面。" % len(probs))
        else:
            out.append("全部正常。")
        return "\n".join(out) + "\n"

    def data(self):
        return {"at": time.time(), "sections": [{"title": t, "items": [dict(zip(("status", "name", "detail", "fix"), x))
                                                                        for x in items]} for t, items in self.sections],
                "features": [dict(zip(("name", "status", "detail"), x)) for x in self.features]}


# ---------------- 小工具 ----------------

def gb(n):
    return "%.1f GB" % (n / 1073741824.0)


def mem_bytes():
    try:
        if sys.platform == "darwin":
            return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], timeout=5).strip())
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        return int(line.split()[1]) * 1024
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.ullTotalPhys)
    except Exception:
        return 0
    return 0


def os_name():
    if sys.platform == "darwin":
        return "macOS %s" % (platform.mac_ver()[0] or platform.release())
    if sys.platform == "win32":
        return "Windows %s（%s）" % (platform.release(), platform.version())
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "%s %s" % (platform.system(), platform.release())


def pkg_version(pkg):
    try:
        from importlib.metadata import version
        return version(pkg)
    except Exception:
        return None


def can_import(mod):
    try:
        __import__(mod)
        return True, ""
    except Exception as e:      # 不只是 ImportError：版本不配时导入也可能抛别的错
        return False, "%s: %s" % (type(e).__name__, str(e)[:160])


def probe(base_url, internal=True, api_key="", timeout=6):
    """GET {base}/models：(连得上吗, 说明, 模型名列表)。内网地址不走系统代理。"""
    url = base_url.rstrip("/") + "/models"
    handlers = [urllib.request.ProxyHandler({})] if internal else []
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + api_key} if api_key else {})
    t0 = time.time()
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read(200000)
        ms = int((time.time() - t0) * 1000)
        try:
            ids = [m.get("id") for m in json.loads(body.decode("utf-8", "replace")).get("data", []) if isinstance(m, dict)]
        except Exception:
            ids = []
        return True, "连得上（%d 毫秒）" % ms, ids
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "连得上，但被拒绝（HTTP %d，API 密钥不对？）" % e.code, []
        return True, "连得上（HTTP %d）" % e.code, []
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        why = getattr(e, "reason", e)
        return False, "连不上（%s）" % str(why)[:120], []


def is_internal(url):
    host = re.sub(r"^\w+://", "", url).split("/")[0].split(":")[0]
    return bool(re.match(r"^(10\.|127\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|localhost$)", host)) \
        or host.endswith(".local") or host.endswith(".lan") or host.endswith(".ts.net")


def read_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_state(update):
    s = read_state()
    s.update(update)
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        f.write(json.dumps(s, ensure_ascii=False) + "\n")


def run_smoke():
    """新内核自检：(结果 pass|fail|timeout|error, 一句话说明)。输出全文存到 setup/新内核自检.txt。"""
    out_path = os.path.join(HERE, "新内核自检.txt")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "lg_smoke.py")], cwd=ROOT, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        text = p.stdout.decode("utf-8", "replace")
        rc = p.returncode
    except subprocess.TimeoutExpired as e:
        text = (e.stdout or b"").decode("utf-8", "replace") + "\n（超过 3 分钟，停了）\n"
        rc = None
    except Exception as e:
        return "error", "没能运行自检：%s: %s" % (type(e).__name__, e)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass
    m = re.search(r"共 (\d+) 步，通过 (\d+)，失败 (\d+)", text)
    summary = ("%s 步里通过 %s 步" % (m.group(1), m.group(2))) if m else "没读到汇总"
    secs = time.time() - t0
    if rc is None:
        return "timeout", "超过 3 分钟没跑完（%s）" % summary
    if rc == 0:
        return "pass", "%s（%.0f 秒）" % (summary, secs)
    fails = re.findall(r"^\s*失败：(.+)$", text, re.M)
    return "fail", "%s；没通过的：%s" % (summary, "；".join(f[:80] for f in fails[:3]) or "见 setup/新内核自检.txt")


# ---------------- 检查 ----------------

def check(args):
    R = Report()
    in_venv = os.path.abspath(sys.prefix).startswith(os.path.join(ROOT, ".venv"))

    # 1. 电脑
    R.section("电脑")
    R.add(INFO, "系统", "%s（%s）" % (os_name(), platform.machine() or "?"))
    R.add(INFO, "处理器", "%s 个核" % (os.cpu_count() or "?"))
    mem = mem_bytes()
    if mem:
        big = mem >= 7.5 * 1073741824        # 标称 8 GB 的机器实际报 7.8 GB 左右
        R.add(GOOD if big else WARN, "内存", gb(mem), "" if big else "少于 8 GB：同时开着浏览器操作网页时可能会慢")
    try:
        free = shutil.disk_usage(ROOT).free
        R.add(GOOD if free >= 2 * 1073741824 else WARN, "项目所在磁盘的剩余空间", gb(free),
              "" if free >= 2 * 1073741824 else "少于 2 GB：资料库和对话记录会慢慢变大，建议腾出空间")
    except OSError:
        pass

    # 2. Python
    R.section("Python")
    v = sys.version_info
    where = "项目的 .venv" if in_venv else sys.executable
    if v >= (3, 11):
        R.add(GOOD, "版本", "%s（%s）" % (platform.python_version(), where))
    elif v >= (3, 9):
        R.add(WARN, "版本", "%s（%s）" % (platform.python_version(), where),
              "新内核和完整功能要 3.11 以上。运行 bash setup/install.sh（Windows：start.bat）会自动装一个，不改系统")
    else:
        R.add(BAD, "版本", "%s（%s）" % (platform.python_version(), where), "太旧，程序起不来。运行 bash setup/install.sh 自动装一个新的")
    if not in_venv and os.path.isfile(os.path.join(ROOT, ".venv", "pyvenv.cfg")):
        R.add(INFO, "说明", "这次检查用的不是项目的 .venv。start.sh 启动时会优先用 .venv")
    # SQLite：资料库的关键词检索要 FTS5 和 trigram 分词（SQLite 3.34 以上）
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
        con.close()
        R.add(GOOD, "SQLite", "%s，支持中文全文检索" % sqlite3.sqlite_version)
        fts_ok = True
    except Exception:
        R.add(WARN, "SQLite", "%s，不支持 trigram 分词" % sqlite3.sqlite_version,
              "资料库的中文关键词检索会差一些。用 setup/install.sh 装的 Python 自带新版 SQLite")
        fts_ok = False

    # 3. 依赖
    R.section("依赖")
    missing_core = []
    for mod, pkg, what in CORE:
        ok, why = can_import(mod)
        if ok:
            R.add(GOOD, pkg, "%s（%s）" % (pkg_version(pkg) or "已装", what))
        else:
            missing_core.append(pkg)
            R.add(BAD if pkg not in ("langchain-mcp-adapters",) else WARN, pkg, "没装或导入出错（%s）" % what,
                  "运行 bash setup/install.sh" if "No module" in why else why)
    for mod, pkg, what in EXTRA:
        ok, why = can_import(mod) if (mod != "playwright" or v >= (3, 10)) else (False, "")
        R.add(GOOD if ok else OFF, pkg, ("%s（%s）" % (pkg_version(pkg) or "已装", what)) if ok else "没装（%s）" % what)
    office_ok = all(can_import(m)[0] for m in ("openpyxl", "docx", "pptx", "pypdf"))

    # 4. 新内核
    R.section("执行内核")
    lg_ok, lg_why = False, "要 Python 3.11 以上"
    if v >= (3, 10):
        try:
            from agent import lg
            lg_ok, lg_why = lg.available()
        except Exception as e:
            lg_ok, lg_why = False, "%s: %s" % (type(e).__name__, e)
    smoke = read_state().get("lg_smoke")
    if lg_ok and args.smoke:
        print("  正在跑新内核自检（最多 3 分钟）……", flush=True)
        res, detail = run_smoke()
        write_state({"lg_smoke": res, "lg_smoke_detail": detail, "lg_smoke_python": platform.python_version(),
                     "lg_smoke_at": int(time.time())})
        smoke = res
    detail = read_state().get("lg_smoke_detail", "")
    if not lg_ok:
        R.add(WARN, "新内核（LangChain / LangGraph）", "用不了：%s" % (lg_why.split("；")[0][:160] + ("……" if "；" in lg_why else "")), "运行 bash setup/install.sh。用不了时程序自动用过渡内核，照样能用")
        R.feature("新内核", WARN, "用过渡内核代替")
    elif smoke == "pass":
        R.add(GOOD, "新内核（LangChain / LangGraph）", "能用，自检通过：%s" % detail)
        R.feature("新内核", GOOD)
    elif smoke in ("fail", "timeout", "error"):
        R.add(WARN, "新内核（LangChain / LangGraph）", "装好了，但自检没通过：%s" % detail,
              "程序先用过渡内核，照样能用。把 setup/新内核自检.txt 发给开发者")
        R.feature("新内核", WARN, "自检没通过，先用过渡内核")
    else:
        R.add(INFO, "新内核（LangChain / LangGraph）", "装好了，还没在这台电脑上自检（python setup/check_env.py --smoke）")
        R.feature("新内核", GOOD, "没自检")

    # 5. 配置和服务
    R.section("模型和服务")
    cfg = None
    try:
        from agent.config import Config
        cfg = Config(args.home)
        R.add(INFO, "数据目录", cfg.home)
    except Exception as e:
        R.add(WARN, "设置", "读不了：%s: %s" % (type(e).__name__, e))
    model_ok = False
    if cfg is not None:
        eps = cfg.get("endpoints") or []
        if not eps:
            R.add(BAD, "模型服务", "一个都没配", "在界面的 设置 → 模型服务 里填地址")
        for e in eps:
            base = str(e.get("base_url") or "")
            name = e.get("name") or base
            internal = bool(e.get("internal", is_internal(base)))
            ok, why, ids = probe(base, internal, str(e.get("api_key") or "")) if base else (False, "没写地址", [])
            want = str(e.get("model") or "")
            if ok and ids and want and want not in ids:
                R.add(WARN, "模型服务 %s" % name, "%s，但上面没有模型 %s（有：%s）" % (why, want, "、".join(ids[:5])),
                      "在 设置 → 模型服务 里把模型名改成上面列出的其中一个")
                continue
            if ok:
                model_ok = True
                R.add(GOOD, "模型服务 %s" % name, "%s  %s" % (base, why))
            else:
                fix = "这台电脑要能访问这个地址（%s 是 Tailscale 的地址，先连上 Tailscale）" % base if base.startswith("http://100.") \
                    else "检查地址是否写对、这台电脑能不能访问；也可以在 设置 → 模型服务 里点\"测试连接\""
                R.add(BAD, "模型服务 %s" % name, "%s  %s" % (base, why), fix)
        R.feature("对话和做事", GOOD if model_ok else BAD, "" if model_ok else "模型服务连不上；用 --mock 能看界面")

        for key, title, feat, off_note in (("embedding", "向量模型", "语义检索", "没配：资料库只用关键词检索，照样能用"),
                                           ("rerank", "重排模型", None, "没配（可选）"),
                                           ("asr", "语音识别", "语音输入", "没配：语音输入用不了")):
            c = cfg.get(key) or {}
            eng = str(c.get("engine") or "off")
            if eng == "off":
                R.add(OFF, title, off_note)
                if feat:
                    R.feature(feat, OFF, off_note.split("：", 1)[-1])
                continue
            if key == "asr" and eng == "sherpa":
                R.add(INFO, title, "用本机识别（sherpa）")
                if feat:
                    R.feature(feat, GOOD)
                continue
            base = str(c.get("base_url") or "")
            ok, why, _ = probe(base, is_internal(base), str(c.get("api_key") or "")) if base else (False, "没写地址", [])
            R.add(GOOD if ok else WARN, title, "%s  %s" % (base, why), "" if ok else "检查地址，或者先关掉（设置里改成不用）")
            if feat:
                R.feature(feat, GOOD if ok else WARN, "" if ok else "%s连不上" % title)

        sdir = str(cfg.get("shared", "dir", default="") or "")
        if sdir:
            okd = os.path.isdir(sdir) and os.access(sdir, os.R_OK)
            R.add(GOOD if okd else WARN, "公司共享目录", "%s  %s" % (sdir, "读得到" if okd else "读不到"),
                  "" if okd else "连上共享盘；连不上时用本机上次同步的内容")
        else:
            R.add(OFF, "公司共享目录", "没配（可选）")

    # 6. 操作网页
    R.section("操作网页")
    pw_ok = v >= (3, 10) and can_import("playwright")[0]
    browser = None
    try:
        from agent import browser as br
        for kind in (("msedge", "chrome") if sys.platform == "win32" else ("chrome",)):
            browser = browser or br.system_browser(kind)
        bundled = br.bundled_chromium() if pw_ok else False
    except Exception:
        bundled = False
    if browser:
        R.add(GOOD, "浏览器", browser)
    elif bundled:
        R.add(GOOD, "浏览器", "Playwright 自带的 Chromium")
    else:
        R.add(WARN, "浏览器", "没找到 Chrome 或 Edge", "装一个 Chrome，或者运行 .venv/bin/python -m playwright install chromium")
    if not pw_ok:
        R.add(OFF, "Playwright", "没装" + ("（要 Python 3.10 以上）" if v < (3, 10) else ""), "运行 bash setup/install.sh")
    R.feature("操作网页", GOOD if (pw_ok and (browser or bundled)) else OFF,
              "" if (pw_ok and (browser or bundled)) else ("没装 Playwright" if not pw_ok else "没有浏览器"))

    # 7. 其他
    R.section("其他")
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", 8765))
        s.close()
        R.add(GOOD, "端口 8765", "空着")
    except OSError:
        R.add(INFO, "端口 8765", "被占用了（可能程序已经开着）。启动时会自动换一个，以终端打印的地址为准")

    R.feature("读写 Excel、Word、PPT、PDF", GOOD if office_ok else BAD, "" if office_ok else "缺 Office 的库")
    R.feature("资料库（关键词检索）", GOOD if fts_ok else WARN, "" if fts_ok else "SQLite 太旧，中文检索差一些")
    order = ["对话和做事", "新内核", "读写 Excel、Word、PPT、PDF", "资料库（关键词检索）", "语义检索", "操作网页", "语音输入"]
    R.features.sort(key=lambda f: order.index(f[0]) if f[0] in order else 99)
    return R


def main():
    ap = argparse.ArgumentParser(description="检查这台电脑能不能跑、哪些功能能用")
    ap.add_argument("--write", action="store_true", help="写进 setup/环境检查结果.txt 和数据目录的 环境检查.json")
    ap.add_argument("--smoke", action="store_true", help="装好 LangChain 时跑一遍新内核自检")
    ap.add_argument("--home", default=None, help="数据目录")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")     # Windows 的终端
    except Exception:
        pass
    R = check(args)
    text = R.text()
    print(json.dumps(R.data(), ensure_ascii=False, indent=1) if args.json else text)
    if args.write:
        try:
            with open(REPORT_TXT, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass
        try:
            from agent.config import data_dir
            with open(os.path.join(data_dir(args.home), "环境检查.json"), "w", encoding="utf-8") as f:
                json.dump(R.data(), f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        print("（结果另存在 setup/环境检查结果.txt）")
    return 1 if any(p[0] == BAD for p in R.problems()) else 0


if __name__ == "__main__":
    sys.exit(main())
