"""配置：数据目录、默认值、读写设置。只用标准库。"""
from __future__ import annotations
import json, os, sys, copy

APP_NAME = "CompanyAgent"

DEFAULTS = {
    "display_name": "Agent",
    "user_location": "",           # 所在城市（设置页能填）；空着时按电脑时区推断，见 agent/whereami.py
    "port": 8765,
    # 模型端点池。可以写多个，运行时会探活并在失败时换台。
    # 这里只是占位：真正的地址和模型名写在项目目录的 site.json（本单位的默认值，不进 git，见 load_site）、
    # 或者在界面"设置 → 模型服务"里填。代码里不要写任何单位自己的内网地址。
    "endpoints": [
        {"name": "模型服务", "base_url": "http://127.0.0.1:8000/v1", "model": "", "api_key": "", "internal": True}
    ],
    "model": {
        "context_window": 32768,   # 输入输出共用
        "max_output": 4096,
        "temperature": 0.3,
        "timeout": 180,
        "thinking": False,         # 是否开启模型的思考模式（真实模型测试后再定默认值）
        "tool_mode": "auto",       # auto | native | json
        # 手写的模型能力（只能写在配置文件里），形如 {"vision": false, "tools": true}；端点自己也可以写 caps 和 context_window
        "caps": {},
    },
    "approval_mode": "auto",       # manual 每步都问 | auto 只读和新建放行 | full 只有删除仍问
    "budgets": {"max_steps": 40, "max_failures": 4, "max_minutes": 30,
                "tool_result_chars": 6000, "sub_agent_steps": 10},
    "web": {"enabled": False, "search_url": ""},   # 联网默认关闭
    "mcp_servers": {},             # {"名字": {"command": [...], "env": {...}}}
    # 内核：auto 装了 LangChain / LangGraph 就用它，否则用过渡用的自研内核（设置页和 /状态 里提示）· langgraph · builtin
    "kernel": "auto",
    # 语音识别：off 关闭 · vllm 内网的语音识别服务（OpenAI 兼容）· sherpa 本机识别（sherpa-onnx + SenseVoice）
    "asr": {"engine": "off", "base_url": "", "model": "Qwen/Qwen3-ASR-1.7B", "api_key": "", "language": "",
            "sherpa_model_dir": "", "max_seconds": 180},
    # 长期记忆（docs/记忆设计.md）：enabled 总开关 · auto 后台回顾自动整理 · 两个本子的上限（估算 token）
    # · 每条最多多少字 · 一次运行结束时攒够几句用户发言才回顾 · 任务空闲多少分钟后回顾
    "memory": {"enabled": True, "auto": True, "user_budget": 500, "workspace_budget": 800, "entry_chars": 300,
               "nudge_turns": 4, "idle_minutes": 2},
    # 技能自学习：一次运行里成功的工具调用不少于这么多次，就提示"可以存成技能"（0 表示不提示）
    "skills": {"suggest_after": 8},
    # 默认空间的文件夹（docs/聊天为主的界面.md 1.1）：空着就按数据目录自动选
    "default_folder": "",
    # 上传文件的大小上限（MB）
    "upload_max_mb": 50,
    # 界面：theme 主题 id（docs/指令和主题.md 3），auto 跟随系统深浅色
    "ui": {"theme": "auto"},
    # 操作网页（docs/操控电脑.md 2.5）：界面只能改 enabled、allowed_domains、blocked_domains
    # view：panel 在程序里的网页面板里看（浏览器不开窗口）· window 单独开一个看得见的浏览器窗口（docs/操控电脑.md 第 9 节）
    # search_url：面板地址栏里输的不是网址时，用它搜（{q} 换成输入的词）
    "browser": {"enabled": True, "allowed_domains": [], "blocked_domains": [], "allow_local": False, "headless": False,
                "channel": "auto", "executable_path": "", "idle_minutes": 30, "max_actions": 40,
                "view": "panel", "search_url": "https://www.baidu.com/s?wd={q}"},
    # 操作桌面程序（第 3、7 节）：require_vision 要求模型能看图、能指位置
    "desktop": {"require_vision": True},
    # 资料库的语义检索（docs/两种模式.md 2.4）：向量模型由内网提供（OpenAI 兼容的 /v1/embeddings），off 时只用关键词检索。
    # query_prefix 只加在问题前面（Qwen3-Embedding 的要求）；dims 为 0 表示用模型给的长度
    "embedding": {"engine": "off", "base_url": "", "model": "Qwen/Qwen3-Embedding-0.6B", "api_key": "", "dims": 0, "batch": 16,
                  "query_prefix": "Instruct: 根据问题找出能回答它的公司资料片段\nQuery: "},
    # 重排（可选）：off 或 vllm（/v1/rerank）
    "rerank": {"engine": "off", "base_url": "", "model": "Qwen/Qwen3-Reranker-0.6B", "api_key": ""},
    # 资料库：engine 向量存在哪（auto 装了 LangChain 那几个包用 langchain，否则 builtin）· 每次检索返回几条 · 单个文件上限（MB）
    # · 一个库最多几个文件 · 后台多久同步一次（分钟，0 关掉）
    "kb": {"engine": "auto", "top_k": 5, "max_file_mb": 50, "max_files": 5000, "sync_minutes": 30,
           "min_cover": 0.35, "min_sim": 0.3,
           # 文档卡片（docs/借鉴开源项目.md 第 1 节）：auto 后台让模型给每份文档写摘要 · off 只留程序算得出的目录
           "cards": "auto"},
    # 工具按需加载和重复调用检测（docs/借鉴开源项目.md 第 3 节）：lazy 是 auto（上下文小于 64K 时开）· on · off；
    # loop_guard 是同一个调用、同样的结果连续几次就拦下（0 表示不拦）
    "tools": {"lazy": "auto", "loop_guard": 3},
    # 公司共享目录（docs/借鉴开源项目.md 第 5 节）：dir 是共享盘上的一个文件夹，空着就是没用；多久看一次有没有更新（分钟）
    "shared": {"dir": "", "sync_minutes": 30},
}


def user_home():
    return os.path.expanduser("~")


def standard_dir():
    """按平台算出的标准数据目录（不看 --home 和环境变量）。"""
    if sys.platform == "win32":
        return os.path.join(os.environ.get("LOCALAPPDATA", user_home()), APP_NAME)
    return os.path.join(user_home(), ".companyagent")


def data_dir(override=None):
    if override:
        d = override
    elif os.environ.get("COMPANYAGENT_HOME"):
        d = os.environ["COMPANYAGENT_HOME"]
    else:
        d = standard_dir()
    if not os.path.isdir(d):
        # 账本里有聊天记录：新建的数据目录只让当前用户访问
        os.makedirs(d, mode=0o700, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return d


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "mcp_servers":
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


_MISSING = object()


def _path_get(d, path):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return _MISSING
        cur = cur[k]
    return cur


def _path_set(d, path, value):
    cur = d
    for k in path[:-1]:
        if not isinstance(cur.get(k), dict):
            cur[k] = {}
        cur = cur[k]
    cur[path[-1]] = copy.deepcopy(value)


SITE_FILE = "site.json"


def site_path():
    """本单位默认值文件的位置：环境变量 COMPANYAGENT_SITE（空字符串表示不用），否则是项目目录里的 site.json。"""
    p = os.environ.get("COMPANYAGENT_SITE")
    if p is not None:
        return p
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), SITE_FILE)


def load_site():
    """本单位的默认设置：模型地址、模型名这类每个单位不一样、又不该写进代码里的值。
    放在项目目录的 site.json（.gitignore 里排除了，样例是 site.example.json），格式和 settings.json 一样，只写要改的项。
    先后：公司锁定 > 本机 settings.json > 公司默认 > site.json > 程序内置。读不出来就当没有。"""
    p = site_path()
    if not p or not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


class Config:
    """设置。三层（docs/借鉴开源项目.md 5.3）：公司锁定（locked）> 本机 > 公司默认（defaults）> 程序内置（DEFAULTS）。

    - self.local：本机的设置（内置默认值 + 数据目录的 settings.json），save() 只写它。公司下发的值永远不进本机的文件。
    - self.data：叠加后的值。没有下发时 self.data 就是 self.local 本身（和以前完全一样，直接改 cfg.data 照样生效）；
      有下发时是另算出来的一份，cfg.get、cfg.data 读到的都是叠加后的值。
    - 下发的值由 agent/shared.py 校验后用 set_overlay 放进来：{"defaults": {路径元组: 值}, "locked": {路径元组: 值}}。
    - "本机改过"的判断：本机的值和程序内置的默认值不一样（settings.json 里存的是完整的一份，分不出哪些是用户写的；
      所以本机特意设成和内置默认值一样的项，会被公司默认值盖掉，见笔记）。"""

    def __init__(self, home=None):
        self.home = data_dir(home)
        self.path = os.path.join(self.home, "settings.json")
        site = load_site()
        self.base = _merge(DEFAULTS, site) if site else DEFAULTS       # 内置默认值 + 本单位的默认值（site.json）
        self.data = copy.deepcopy(self.base)
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = _merge(self.base, json.load(f))
            except Exception:
                pass
        self.local = self.data
        self.overlay = None

    def get(self, *keys, default=None):
        cur = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def update(self, patch):
        self.local = _merge(self.local, patch)
        self._recompute()
        self.save()

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.local, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ---- 公司下发的设置（叠加，不落本机文件） ----
    def set_overlay(self, overlay):
        """overlay：None（没有下发）或 {"defaults": {(键, 子键…): 值}, "locked": {…}}。值必须是已经校验过的。"""
        if overlay and (overlay.get("defaults") or overlay.get("locked")):
            self.overlay = {"defaults": dict(overlay.get("defaults") or {}), "locked": dict(overlay.get("locked") or {})}
        else:
            self.overlay = None
        self._recompute()

    def _recompute(self):
        ov = self.overlay
        if not ov:
            self.data = self.local
            return
        eff = copy.deepcopy(self.local)
        for path, v in ov["defaults"].items():
            mine = _path_get(self.local, path)
            if mine is _MISSING or mine == _path_get(self.base, path):
                _path_set(eff, path, v)
        for path, v in ov["locked"].items():
            _path_set(eff, path, v)
        self.data = eff

    def locked_paths(self):
        return list((self.overlay or {}).get("locked") or {})

    def locked_value(self, path):
        """被锁住的那一项的值；没锁返回 (False, None)。path 是元组。"""
        lk = (self.overlay or {}).get("locked") or {}
        if tuple(path) in lk:
            return True, lk[tuple(path)]
        return False, None


def _same_dir(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def default_folder(cfg):
    """默认空间该用的文件夹：配置了 default_folder 就用它；数据目录是标准位置时用 <用户主目录>/CompanyAgent；
    数据目录是另指的（--home 或环境变量），用 <数据目录>/files，免得测试和演示弄乱用户主目录。"""
    conf = cfg.get("default_folder")
    if isinstance(conf, str) and conf.strip():
        return os.path.abspath(os.path.expanduser(conf.strip()))
    if _same_dir(cfg.home, standard_dir()):
        return os.path.join(user_home(), APP_NAME)
    return os.path.join(cfg.home, "files")


def upload_max_bytes(cfg):
    """上传上限（字节）。配置写错时按 50 MB。"""
    v = cfg.get("upload_max_mb")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        v = 50
    return int(v * 1024 * 1024)
