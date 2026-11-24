"""操作桌面程序：驱动接口和注册表（docs/操控电脑.md 第 3 节）。

现在不带任何真实驱动：Windows 的驱动排在阶段 4（打算基于 Windows-MCP 或 UI Automation，读界面元素清单）；
macOS 暂不做。测试里注册一个假驱动走通整条路。只用标准库。"""
from __future__ import annotations
import sys, threading


class Driver:
    """桌面驱动的接口。真实驱动继承它，实现下面几个方法。"""

    name = "base"
    platforms = ()            # 例如 ("win32",)；sys.platform 以其中之一开头才会被选用
    needs_vision = False      # 驱动需要模型看截图才能用

    def available(self):
        """(能不能用, 原因)。"""
        return False, "没有实现"

    def open_app(self, name):
        """打开或切换到一个程序。返回一句结果。"""
        raise NotImplementedError

    def look(self, image=False):
        """返回 {"title": 窗口标题, "text": 界面元素清单（每行一个元素，带编号）, "image": PNG 字节或 None}。"""
        raise NotImplementedError

    def do(self, action, target, text):
        """做一个动作（click、type、key……）。target 是元素编号或名字。返回一句结果。"""
        raise NotImplementedError


_lock = threading.Lock()
DRIVERS = []


def register(driver):
    """注册一个驱动（实例）。后注册的排在前面，方便测试覆盖。"""
    with _lock:
        DRIVERS.insert(0, driver)
    return driver


def unregister(driver):
    with _lock:
        if driver in DRIVERS:
            DRIVERS.remove(driver)


def current():
    """按当前系统选驱动：平台对得上、而且 available() 为真的第一个。没有返回 None。"""
    with _lock:
        ds = list(DRIVERS)
    for d in ds:
        if d.platforms and not any(sys.platform.startswith(p) for p in d.platforms):
            continue
        try:
            ok, _ = d.available()
        except Exception:
            ok = False
        if ok:
            return d
    return None


def component_ok():
    d = current()
    if d is None:
        return False, "这台电脑上还没有桌面操控组件"
    return True, ""
