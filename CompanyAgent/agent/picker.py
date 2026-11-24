"""弹系统自带的"选择文件夹"窗口（docs/聊天为主的界面.md 1.3）。

命令和参数都是写死的，不拼接任何用户输入。服务端调用 pick_folder()；测试把它换掉，不真的弹窗。"""
from __future__ import annotations
import base64, os, shutil, subprocess, sys

TIMEOUT = 120            # 最长等多久（秒）
PROMPT = "选择一个文件夹"

# macOS：先把 osascript 自己带到前面，窗口才不会躲在浏览器后面。用户点取消时报错 -128
MAC_SCRIPT = ["activate", 'POSIX path of (choose folder with prompt "%s")' % PROMPT]
# Windows：FolderBrowserDialog 挂在一个置顶的隐形窗口上；取消时退出码 2。输出按 UTF-8，中文路径不乱码
WIN_SCRIPT = (
    "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false\n"
    "Add-Type -AssemblyName System.Windows.Forms\n"
    "$d = New-Object System.Windows.Forms.FolderBrowserDialog\n"
    "$d.Description = '%s'\n"
    "$d.ShowNewFolderButton = $true\n"
    "$f = New-Object System.Windows.Forms.Form\n"
    "$f.TopMost = $true\n"
    "if ($d.ShowDialog($f) -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath; exit 0 }\n"
    "exit 2\n" % PROMPT)


def command(platform=None):
    """这台机器上弹窗用的命令（参数列表）。弹不了返回 None。"""
    platform = platform or sys.platform
    if platform == "darwin":
        exe = shutil.which("osascript")
        if not exe:
            return None
        cmd = [exe]
        for line in MAC_SCRIPT:
            cmd += ["-e", line]
        return cmd
    if platform == "win32":
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if not exe:
            return None
        enc = base64.b64encode(WIN_SCRIPT.encode("utf-16-le")).decode("ascii")
        return [exe, "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand", enc]
    # Linux 等：有图形界面并且装了 zenity 才行
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    exe = shutil.which("zenity")
    if not exe:
        return None
    return [exe, "--file-selection", "--directory", "--title=" + PROMPT]


def parse(platform, code, out, err):
    """把命令的结果变成 ("ok", 路径) / ("cancel", None) / ("error", None)。"""
    out = (out or "").replace("﻿", "").strip().splitlines()
    path = out[-1].strip() if out else ""
    if code == 0 and path:
        if len(path) > 1 and path.endswith(("/", "\\")) and not path.endswith(":\\"):
            path = path.rstrip("/\\") or path
        return ("ok", path) if os.path.isdir(path) else ("error", None)
    if platform == "darwin" and code != 0 and "-128" in (err or ""):
        return "cancel", None
    if platform == "win32" and code == 2:
        return "cancel", None
    if platform not in ("darwin", "win32") and code == 1:
        return "cancel", None      # zenity：取消或关掉窗口
    return "error", None


def pick_folder(timeout=TIMEOUT):
    """弹窗让用户选文件夹。返回 {"supported": True, "path": 路径或 None} 或 {"supported": False}。
    等超过 timeout 秒就关掉窗口，按取消处理。"""
    cmd = command()
    if not cmd:
        return {"supported": False}
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, **kw)
    except subprocess.TimeoutExpired:
        return {"supported": True, "path": None}
    except OSError:
        return {"supported": False}
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace")
    kind, path = parse(sys.platform, r.returncode, out, err)
    if kind == "ok":
        return {"supported": True, "path": path}
    if kind == "cancel":
        return {"supported": True, "path": None}
    return {"supported": False}
