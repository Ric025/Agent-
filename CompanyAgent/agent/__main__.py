"""启动入口：python3 -m agent [--port 8765] [--home 目录] [--mock] [--no-open]"""
from __future__ import annotations
import argparse, os, signal, sys, threading


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m agent", description="CompanyAgent 本机办公 Agent")
    ap.add_argument("--port", type=int, default=None, help="端口，默认 8765；被占用时自动往后找")
    ap.add_argument("--home", default=None, help="数据目录，默认 ~/.companyagent（Windows 为 %%LOCALAPPDATA%%\\CompanyAgent）")
    ap.add_argument("--mock", action="store_true", help="用假模型启动，不连真实模型，方便看界面")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    a = ap.parse_args(argv)

    from .config import Config
    from .runtime import Runtime
    from .server import make_server
    cfg = Config(a.home)
    factory = None
    if a.mock:
        from .llm import MockLLM, demo_script
        factory = lambda: MockLLM([demo_script] * 100000)   # noqa: E731
    try:
        rt = Runtime(cfg, factory)
    except RuntimeError as e:
        print("启动失败：%s" % e, file=sys.stderr)
        return 1
    rt.mock = a.mock
    rt.recover()
    try:
        dws = rt.ensure_default_workspace()
    except Exception as e:      # 默认空间建不了不影响启动：界面会退回"先选文件夹"
        dws = None
        print("提示：没能准备默认文件夹：%s" % e, file=sys.stderr)
    srv = make_server(rt, a.port or cfg.get("port") or 8765)
    url = srv.url()
    print("CompanyAgent 已启动%s。" % ("（演示模式：假模型）" if a.mock else ""))
    print("在浏览器打开：" + url, flush=True)
    print("数据目录：" + cfg.home)
    if dws:
        print("默认文件夹：" + dws["path"])
    if getattr(rt, "kernel_name", "builtin") == "langgraph":
        print("执行内核：LangChain / LangGraph")
    elif getattr(rt, "kernel_notice", None):
        print("提示：" + rt.kernel_notice)
        if getattr(rt, "kernel_why", ""):
            print("      原因：" + rt.kernel_why)
    print("按 Ctrl+C 退出。", flush=True)
    if not a.no_open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    stop = threading.Event()

    def on_signal(*_):
        stop.set()
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                signal.signal(sig, on_signal)
            except (ValueError, OSError):
                pass
    srv.start()
    try:
        while not stop.wait(0.5):
            pass
    finally:
        print("正在退出……", flush=True)
        srv.stop()
        rt.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
