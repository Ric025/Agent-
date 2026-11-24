"""本单位的默认设置 site.json（agent/config.load_site）：模型地址这类值不写进代码，放在项目目录里一个不进 git 的文件。"""
from __future__ import annotations
import json, os, shutil, tempfile, unittest
from unittest import mock

import helpers  # noqa: F401  （把项目目录加进 sys.path，并关掉开发机上的 site.json）
from agent import config as config_mod
from agent.config import Config

APP = helpers.APP


class Site(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="ca_site_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.site = os.path.join(self.d, "site.json")
        self.home = os.path.join(self.d, "home")
        os.makedirs(self.home)

    def write(self, obj):
        with open(self.site, "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))

    def cfg(self):
        with mock.patch.dict(os.environ, {"COMPANYAGENT_SITE": self.site}):
            return Config(self.home)

    def test_builtin_default_has_no_real_address(self):
        ep = config_mod.DEFAULTS["endpoints"][0]
        self.assertEqual((ep["base_url"], ep["model"]), ("http://127.0.0.1:8000/v1", ""))
        with mock.patch.dict(os.environ, {"COMPANYAGENT_SITE": ""}):
            self.assertEqual(config_mod.load_site(), {})
            self.assertEqual(Config(self.home).get("endpoints"), config_mod.DEFAULTS["endpoints"])

    def test_site_then_settings(self):
        self.write({"endpoints": [{"name": "甲", "base_url": "http://10.0.0.11:8000/v1", "model": "m1"}], "approval_mode": "manual"})
        c = self.cfg()
        self.assertEqual(c.get("endpoints")[0]["base_url"], "http://10.0.0.11:8000/v1")
        self.assertEqual(c.get("approval_mode"), "manual")
        self.assertEqual(c.get("model", "context_window"), 32768)            # 没写的项还是内置的
        # 本机的 settings.json 盖过 site.json
        c.update({"approval_mode": "full"})
        c2 = self.cfg()
        self.assertEqual(c2.get("approval_mode"), "full")
        self.assertEqual(c2.get("endpoints")[0]["name"], "甲")
        # 公司共享目录的默认值：本机的值和 site.json 一样，算"本机没改过"，会被公司默认值盖掉
        c3 = self.cfg()
        c3.set_overlay({"defaults": {("kb", "top_k"): 9}, "locked": {}})
        self.assertEqual(c3.get("kb", "top_k"), 9)

    def test_bad_file_ignored(self):
        for bad in ("{坏的", "[1, 2]", ""):
            self.write(bad)
            self.assertEqual(self.cfg().get("endpoints"), config_mod.DEFAULTS["endpoints"], bad)
        self.write("\ufeff" + json.dumps({"display_name": "带 BOM 的"}))      # Windows 记事本存的
        self.assertEqual(self.cfg().get("display_name"), "带 BOM 的")

    def test_default_location_and_ignored(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COMPANYAGENT_SITE", None)
            self.assertEqual(config_mod.site_path(), os.path.join(APP, "site.json"))
        with open(os.path.join(APP, ".gitignore"), encoding="utf-8") as f:
            ignored = f.read().split()
        for name in ("site.json", ".venv/", ".tools/", "setup/mirror.env", "setup/安装记录.txt", "setup/环境检查结果.txt",
                     "tests/realmodel/results/", "settings.json", "token", "*.db"):
            self.assertIn(name, ignored, name)
        with open(os.path.join(APP, "site.example.json"), encoding="utf-8") as f:
            ex = json.load(f)
        self.assertIn("模型服务器的地址", ex["endpoints"][0]["base_url"])       # 样例里是占位，不是真地址


class NoSecretsInRepo(unittest.TestCase):
    """要进 git 的文件里不该有：真实的内网地址、这台电脑的用户目录。上传前的最后一道检查。"""

    SKIP_DIRS = {".venv", ".tools", "__pycache__", ".git", "results", "wheels", "node_modules"}
    SKIP_FILES = {"site.json", "安装记录.txt", "环境检查结果.txt", "新内核自检.txt", "mirror.env", ".DS_Store"}

    def test_scan(self):
        import re
        # Tailscale / 运营商级 NAT 的地址段（100.64 到 100.127 开头）、macOS 和 Windows 的用户目录（样例里的 zhang 除外）
        pat = re.compile(r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b|/Users/(?!zhang\b)[A-Za-z0-9_.-]+|C:\\\\Users\\\\(?!你的名字)[A-Za-z0-9_.-]+\\\\")
        hits = []
        for root, dirs, files in os.walk(APP):
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]
            for fn in files:
                if fn in self.SKIP_FILES or not fn.endswith((".py", ".js", ".mjs", ".sh", ".bat", ".ps1", ".command", ".md", ".json", ".html", ".css", ".txt", ".example")):
                    continue
                p = os.path.join(root, fn)
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        for n, line in enumerate(f, 1):
                            m = pat.search(line)
                            if m and "test_site.py" not in p:
                                hits.append("%s:%d %s" % (os.path.relpath(p, APP), n, m.group(0)))
                except OSError:
                    continue
        self.assertEqual(hits, [], "这些地方有不该上传的地址或路径")


if __name__ == "__main__":
    unittest.main()
