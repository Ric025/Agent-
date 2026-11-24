"""新内核：LangChain 的 create_agent（底层是一张 LangGraph 图）加官方中间件。

依赖 LangChain / LangGraph 的代码全部在这个包里：
  models.py      模型：ChatOpenAI、测试用的假模型、流式片段转发
  tools.py       业务工具包成 LangChain 工具；连接器用 langchain-mcp-adapters
  kernel.py      一个任务一次运行的"内核"：批准判定、记账、执行工具、问模型
  middleware.py  把内核挂到 create_agent 上的中间件，以及官方中间件的组装
  host.py        运行时里的宿主：检查点、任务线程的主循环、子代理

本文件只做"能不能用"的检查，不导入任何第三方包，运行时据此选择内核。
迁移说明见 docs/内核迁移说明.md。
"""
from __future__ import annotations
import importlib, sys

# (模块, pip 包名)
REQUIRED = [
    ("langchain_core", "langchain-core"),
    ("langchain.agents", "langchain"),
    ("langchain.agents.middleware", "langchain"),
    ("langgraph", "langgraph"),
    ("langgraph.types", "langgraph"),
    ("langgraph.checkpoint.sqlite", "langgraph-checkpoint-sqlite"),
    ("langchain_openai", "langchain-openai"),
]
OPTIONAL = [("langchain_mcp_adapters", "langchain-mcp-adapters")]   # 只有配置了连接器才需要

NOTICE = "没有安装 LangChain / LangGraph，当前使用过渡内核。重新自动安装：bash start.sh --setup（Windows：start.bat --setup）"

_cache = None


def available():
    """返回 (能用吗, 原因)。只做导入检查，不建任何对象。结果在进程内缓存。"""
    global _cache
    if _cache is not None:
        return _cache
    if sys.version_info < (3, 10):
        _cache = (False, "Python %d.%d 太旧，新内核需要 Python 3.11 或更新" % sys.version_info[:2])
        return _cache
    missing, bad_pkgs = [], set()
    for mod, pkg in REQUIRED:
        if pkg in bad_pkgs:
            continue
        try:
            importlib.import_module(mod)
        except Exception as e:          # 不只是 ImportError：版本不配时导入也可能抛别的错
            bad_pkgs.add(pkg)
            missing.append("%s（%s: %s）" % (pkg, type(e).__name__, str(e)[:200]))
    if missing:
        _cache = (False, "缺少或无法导入：" + "；".join(missing))
        return _cache
    try:
        from langchain.agents.middleware import InterruptOnConfig
        keys = set(getattr(InterruptOnConfig, "__annotations__", {}) or {})
        if "when" not in keys:
            _cache = (False, "langchain 版本太旧：批准中间件不支持按条件批准（InterruptOnConfig.when），需要 langchain>=1.3.3")
            return _cache
    except Exception as e:
        _cache = (False, "langchain 的批准中间件无法导入：%s: %s" % (type(e).__name__, e))
        return _cache
    _cache = (True, "")
    return _cache


def versions():
    """已安装的相关包版本，给自检和报错用。"""
    out = {}
    try:
        from importlib.metadata import version, PackageNotFoundError
    except Exception:
        return out
    for pkg in ("langchain", "langchain-core", "langgraph", "langgraph-checkpoint", "langgraph-checkpoint-sqlite",
                "langchain-openai", "langchain-mcp-adapters", "openai", "httpx", "mcp"):
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            out[pkg] = None
        except Exception:
            out[pkg] = None
    return out
