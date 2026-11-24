"""测试公用：临时数据目录和工作区、带假模型的运行时、等待、消息顺序校验。"""
from __future__ import annotations
import os, shutil, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
if APP not in sys.path:
    sys.path.insert(0, APP)
os.environ.setdefault("COMPANYAGENT_SITE", "")      # 测试不读项目目录里的 site.json（那是这台电脑自己的模型地址）

from agent.config import Config            # noqa: E402
from agent.runtime import Runtime          # noqa: E402
from agent.llm import MockLLM              # noqa: E402
from agent.context import validate_messages  # noqa: E402


def wait_until(pred, timeout=5.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


class Env:
    """一套临时环境：数据目录 + 工作区目录 + 运行时。"""

    def __init__(self, script=None, mode="auto", budgets=None, mock=None, home=None, root=None, review=None, **cfg_patch):
        self.tmp = tempfile.mkdtemp(prefix="ca_test_")
        self.home = home or os.path.join(self.tmp, "home")
        self.root = root or os.path.join(self.tmp, "ws")
        os.makedirs(self.home, exist_ok=True)
        os.makedirs(self.root, exist_ok=True)
        self.cfg = Config(self.home)
        # 旧内核的测试固定用旧内核；新内核的测试（test_lg_*.py）传 kernel="langgraph"
        patch = {"approval_mode": mode, "kernel": "builtin"}
        if budgets:
            patch["budgets"] = budgets
        patch.update(cfg_patch)
        self.cfg.update(patch)
        self.mock = mock if mock is not None else MockLLM(script or [])
        # review：后台回顾用的假模型（MockLLM）。不给时回顾不运行，现有测试的脚本不会被它吃掉
        self.review = review
        self.rt = Runtime(self.cfg, lambda: self.mock, app_dir=APP, review_llm_factory=self._review_factory())
        self.ws = self.rt.store.workspaces()[0] if self.rt.store.workspaces() else self.rt.store.add_workspace("测试", self.root)
        self._closed = False

    # ---- 便捷 ----
    def new_session(self):
        return self.rt.store.add_session(self.ws["id"])["id"]

    def start(self, text):
        sid = self.new_session()
        self.rt.send_message(sid, text)
        return sid

    def status(self, sid):
        return self.rt.store.session(sid)["status"]

    def wait_status(self, sid, *statuses, timeout=5.0):
        ok = wait_until(lambda: self.status(sid) in statuses, timeout)
        return ok

    def events(self, sid, types=None):
        return self.rt.store.events(sid, types=types)

    def types(self, sid):
        return [e["type"] for e in self.rt.store.events(sid)]

    def results(self, sid):
        return [e["payload"] for e in self.rt.store.events(sid, types=("tool_result",))]

    def write(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            return f.read()

    def restart(self, mock=None):
        """模拟进程重启：关掉旧运行时（不改账本状态），用同一个数据目录新开一个。"""
        self.rt.shutdown()
        self.mock = mock if mock is not None else MockLLM([])
        self.cfg = Config(self.home)
        self.rt = Runtime(self.cfg, lambda: self.mock, app_dir=APP, review_llm_factory=self._review_factory())
        return self.rt

    def _review_factory(self):
        return (lambda: self.review) if self.review is not None else None

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self.rt.shutdown()
            except Exception:
                pass
            shutil.rmtree(self.tmp, ignore_errors=True)


def _has_lg():
    try:
        from agent import lg
        return lg.available()[0]
    except Exception:
        return False


HAS_LG = _has_lg()
LG_SKIP = "没有安装 LangChain / LangGraph（运行 tests/run_lg_tests.sh 会建虚拟环境装好再测）"


class Case(unittest.TestCase):
    """每个测试结束时关掉运行时、删临时目录，并检查假模型收到的每次请求消息顺序都合法。"""

    def setUp(self):
        self.envs = []

    def env(self, *a, **kw):
        e = Env(*a, **kw)
        self.envs.append(e)
        return e

    def lg_env(self, *a, **kw):
        """新内核的环境：kernel=langgraph；模型不重试（假模型脚本里的错误要原样暴露）。"""
        kw.setdefault("kernel", "langgraph")
        model = dict(kw.pop("model", {}) or {})
        model.setdefault("retries", 0)
        kw["model"] = model
        return self.env(*a, **kw)

    def tearDown(self):
        for e in self.envs:
            self.check_calls(e.mock)
            if e.review is not None:
                self.check_calls(e.review)
        for e in self.envs:
            e.close()

    def check_calls(self, mock):
        for i, msgs in enumerate(getattr(mock, "calls", [])):
            errs = validate_messages(msgs)
            self.assertEqual(errs, [], "第 %d 次模型请求的消息顺序不合法：%s" % (i + 1, errs))


class ApiCase(Case):
    """起一个本机 HTTP 服务的测试基类：self.e 是环境，self.req(...) 发请求。"""
    script = []
    env_kw = {}

    def setUp(self):
        super().setUp()
        from agent.server import make_server
        self.e = self.env(list(self.script), **dict(self.env_kw))
        self.srv = make_server(self.e.rt, 0)
        self.srv.start()

    def tearDown(self):
        self.srv.stop()
        super().tearDown()

    def req(self, method, path, body=None, token=True, raw=False):
        import http.client, json
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.putrequest(method, path, skip_host=True)
        c.putheader("Host", "127.0.0.1:%d" % self.srv.port)
        if token:
            c.putheader("X-Token", self.srv.token if token is True else token)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            c.putheader("Content-Type", "application/json")
            c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        payload = r.read()
        c.close()
        if raw:
            return r.status, payload, dict(r.getheaders())
        return r.status, (json.loads(payload.decode("utf-8")) if payload else None)
