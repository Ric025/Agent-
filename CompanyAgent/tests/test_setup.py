"""自动安装和环境检查（setup/install.sh、setup/check_env.py、agent/runtime.setup_smoke_failed）。

InstallScript 用假的离线安装包（setup/wheels）把 install.sh 从头走一遍，不联网：
找 Python → 建 .venv → 装必需的 → 逐个装可选的（故意缺一个）→ 检查；再测"装过了不重装""依赖清单变了要重装"
"连不上软件源时说清楚原因、一天内不反复重试"。Windows 的 install.ps1 这里测不了。"""
from __future__ import annotations
import base64, hashlib, json, os, platform, shutil, subprocess, sys, tempfile, time, unittest, zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

from agent import runtime as runtime_mod      # noqa: E402

FAKE = {"langchain": ["langchain"], "langchain-core": ["langchain_core"], "langgraph": ["langgraph"],
        "langgraph-checkpoint-sqlite": ["langgraph/checkpoint", "langgraph/checkpoint/sqlite"],
        "langchain-openai": ["langchain_openai"], "langchain-mcp-adapters": ["langchain_mcp_adapters"], "httpx": ["httpx"],
        "openpyxl": ["openpyxl"], "python-docx": ["docx"], "python-pptx": ["pptx"], "pypdf": ["pypdf"], "numpy": ["numpy"],
        "playwright": ["playwright"], "langchain-chroma": ["langchain_chroma"], "langchain-classic": ["langchain_classic"]}
# 故意没有 chromadb：可选依赖装不上的情况


def fake_wheels(out):
    """一组假的安装包：只有空的模块，版本都是 9.9.9（满足 requirements 里的下限）。"""
    os.makedirs(out, exist_ok=True)
    for name, mods in FAKE.items():
        dn, ver = name.replace("-", "_"), "9.9.9"
        di = "%s-%s.dist-info" % (dn, ver)
        files = {m + "/__init__.py": b"# fake\n" for m in mods}
        files[di + "/METADATA"] = ("Metadata-Version: 2.1\nName: %s\nVersion: %s\n" % (name, ver)).encode()
        files[di + "/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: fake\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        rec = ""
        for f, b in files.items():
            h = base64.urlsafe_b64encode(hashlib.sha256(b).digest()).rstrip(b"=").decode()
            rec += "%s,sha256=%s,%d\n" % (f, h, len(b))
        files[di + "/RECORD"] = (rec + di + "/RECORD,,\n").encode()
        with zipfile.ZipFile(os.path.join(out, "%s-%s-py3-none-any.whl" % (dn, ver)), "w") as z:
            for f, b in files.items():
                z.writestr(f, b)


class SmokeGate(unittest.TestCase):
    """新内核自检没通过时，kernel=auto 先用过渡内核。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, ".tools"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def state(self, obj):
        with open(os.path.join(self.d, ".tools", "setup-state.json"), "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))

    def test_reads_state(self):
        f = runtime_mod.setup_smoke_failed
        self.assertEqual(f(self.d), "")                                        # 没有记录
        self.state({"ok": True, "lg_smoke": "pass", "lg_smoke_python": platform.python_version()})
        self.assertEqual(f(self.d), "")
        self.state({"ok": True, "lg_smoke": "fail", "lg_smoke_detail": "12 步里通过 10 步", "lg_smoke_python": platform.python_version()})
        self.assertEqual(f(self.d), "12 步里通过 10 步")
        self.state({"ok": True, "lg_smoke": "timeout", "lg_smoke_python": "2.7.18"})   # 测的是别的 Python
        self.assertEqual(f(self.d), "")
        self.state("{坏的")
        self.assertEqual(f(self.d), "")

    def test_auto_falls_back(self):
        self.state({"lg_smoke": "fail", "lg_smoke_detail": "没通过的：批准中间件", "lg_smoke_python": platform.python_version()})
        rt = runtime_mod.Runtime.__new__(runtime_mod.Runtime)
        rt.app_dir = self.d
        rt.cfg = mock.Mock()
        rt.cfg.get = lambda *k, **kw: "auto" if k == ("kernel",) else kw.get("default")
        with mock.patch("agent.lg.available", return_value=(True, "")):
            runtime_mod.Runtime._choose_kernel(rt)
        self.assertEqual(rt.kernel_name, "builtin")
        self.assertIn("自检没通过", rt.kernel_notice)
        self.assertIn("批准中间件", rt.kernel_notice)
        self.assertIsNone(rt.lg)


class CheckEnv(unittest.TestCase):
    def test_report(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        with open(os.path.join(home, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"endpoints": [{"name": "测试", "base_url": "http://127.0.0.1:9/v1", "model": "m"}]}, f)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, os.path.join(APP, "setup", "check_env.py"), "--home", home, "--json"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60)
        self.assertIn(p.returncode, (0, 1), p.stderr.decode("utf-8", "replace"))
        d = json.loads(p.stdout.decode("utf-8"))
        titles = [s["title"] for s in d["sections"]]
        for t in ("电脑", "Python", "依赖", "执行内核", "模型和服务", "操作网页"):
            self.assertIn(t, titles)
        feats = {x["name"]: x for x in d["features"]}
        self.assertEqual(feats["对话和做事"]["status"], "不行")        # 127.0.0.1:9 连不上
        items = {i["name"]: i for s in d["sections"] for i in s["items"]}
        self.assertIn("连不上", items["模型服务 测试"]["detail"])
        self.assertTrue(items["模型服务 测试"]["fix"])
        self.assertEqual(p.returncode, 1)                               # 有"不行"的项


@unittest.skipIf(sys.platform == "win32" or not shutil.which("bash") or not shutil.which("curl"), "要 bash 和 curl")
class InstallScript(unittest.TestCase):
    def setUp(self):
        try:
            import ensurepip  # noqa: F401
        except ImportError:
            self.skipTest("这个 Python 建不了带 pip 的虚拟环境（缺 ensurepip）")
        if sys.version_info < (3, 11):
            self.skipTest("要用 Python 3.11+ 来建 .venv")
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.root = os.path.join(self.d, "CompanyAgent")
        os.makedirs(self.root)
        for sub in ("agent", "setup"):
            shutil.copytree(os.path.join(APP, sub), os.path.join(self.root, sub),
                            ignore=shutil.ignore_patterns("__pycache__", "wheels", "mirror.env", "安装记录.txt", "环境检查结果.txt", "新内核自检.txt"))
        os.makedirs(os.path.join(self.root, "tests"))
        fake_wheels(os.path.join(self.root, "setup", "wheels"))
        self.home = os.path.join(self.d, "home")
        os.makedirs(self.home)
        with open(os.path.join(self.home, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"endpoints": [{"name": "测试", "base_url": "http://127.0.0.1:9/v1", "model": "m"}]}, f)

    def run_sh(self, *args, timeout=240):
        env = dict(os.environ, CA_SETUP_PYTHON=sys.executable, COMPANYAGENT_HOME=self.home, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PIP_INDEX_URL", None)
        p = subprocess.run(["bash", os.path.join(self.root, "setup", "install.sh")] + list(args), cwd=self.root, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace")

    def state(self):
        with open(os.path.join(self.root, ".tools", "setup-state.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_install_flow(self):
        # 1. 从头装：离线安装包，必需的都装上，可选的 chromadb 装不上
        rc, out = self.run_sh()
        self.assertEqual(rc, 0, out[-3000:])
        self.assertIn("用 setup/wheels 里的安装包", out)
        self.assertIn("chromadb：没装上", out)
        self.assertIn("openpyxl", out)
        st = self.state()
        self.assertIs(st["ok"], True)
        self.assertEqual(st["extra_failed"], "chromadb")
        vpy = os.path.join(self.root, ".venv", "bin", "python")
        self.assertTrue(os.path.isfile(vpy))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "setup", "环境检查结果.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.home, "环境检查.json")))
        # 2. 装过了：--if-needed 什么都不做，而且快
        t0 = time.time()
        rc, out = self.run_sh("--if-needed")
        self.assertEqual((rc, out.strip()), (0, ""))
        self.assertLess(time.time() - t0, 5)
        # 3. 依赖清单变了、又连不上软件源：说清楚原因，记下失败；一天内 --if-needed 不再重试
        shutil.rmtree(os.path.join(self.root, "setup", "wheels"))
        with open(os.path.join(self.root, "setup", "requirements-extra.txt"), "a", encoding="utf-8") as f:
            f.write("# 改了一行\n")
        with open(os.path.join(self.root, "setup", "mirror.env"), "w", encoding="utf-8") as f:
            f.write("# 测试\nPYPI_MIRROR=http://127.0.0.1:9/simple\nNOT_A_KEY=x\n")
        rc, out = self.run_sh("--if-needed")
        self.assertEqual(rc, 1, out[-2000:])
        self.assertIn("连不上 Python 软件源 http://127.0.0.1:9/simple（返回：没有响应）", out)
        self.assertIn("mirror.env", out)
        st = self.state()
        self.assertIs(st["ok"], False)
        rc, out = self.run_sh("--if-needed")
        self.assertEqual(rc, 3)
        self.assertIn("今天先不再重试", out)
        # 原来装好的 .venv 还在、还能用（start.sh 会接着用它）
        p = subprocess.run([vpy, "-c", "import importlib.util as u, sys; sys.exit(0 if all(u.find_spec(m) for m in ('openpyxl', 'langchain', 'langgraph')) else 1)"])
        self.assertEqual(p.returncode, 0)
        with open(os.path.join(self.root, "setup", "安装记录.txt"), encoding="utf-8") as f:
            self.assertIn("mirror.env 里不认识的键：NOT_A_KEY", f.read())


if __name__ == "__main__":
    unittest.main()
