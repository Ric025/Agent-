"""权限：动作分级、批准判定、路径守卫。"""
from __future__ import annotations
import os

# none 无风险 · read 只读 · create 新建 · overwrite 覆盖 · delete 删除 · exec 执行 · external 对外 · memory 记忆
# · skill 技能（存成技能会影响以后所有同类任务：任何批准模式下都问，只有用户把工具设成"总是允许"才放行）
# · web 网页操作（每步都问时问，自动、全放行直接做）· submit 提交（每步都问、自动都问，全放行直接做）
# · read_outside 读当前文件夹以外的文件（每步都问、自动都问，全放行直接读；存密码、密钥的地方任何方式都不读，见 is_secret）
RISK_LABEL = {"none": "无", "read": "只读", "create": "新建", "overwrite": "覆盖", "delete": "删除",
              "exec": "执行代码", "external": "对外", "memory": "记忆", "skill": "技能",
              "web": "网页操作", "submit": "提交", "read_outside": "读文件夹外的文件"}
INTERNAL_DIR = ".agent"     # 工作区里 Agent 自己用的目录：版本、回收站、暂存


def decide(risk, mode="auto", setting=None):
    """返回 allow | ask | deny。删除永远要问；技能除非设成总是允许，否则也要问。"""
    if setting == "deny":
        return "deny"
    if risk == "skill":
        return "allow" if setting == "allow" else "ask"
    if risk == "delete":
        return "ask"
    if setting == "allow":
        return "allow"
    if setting == "ask":
        return "ask"
    if risk == "none":
        return "allow"
    if mode == "manual":
        return "ask"
    if mode == "full":
        return "allow"
    return "allow" if risk in ("read", "create", "memory", "web") else "ask"


# ---------------- 当前文件夹以外：全放行时能读，但这些地方任何方式都不读 ----------------
# 存密码、密钥、登录状态的地方。网页、文件里的话可能诱导 Agent 去读它们再发出去（提示注入），所以一律拒绝。
SECRET_DIRS = (
    "~/.ssh", "~/.gnupg", "~/.aws", "~/.azure", "~/.kube", "~/.docker", "~/.config/gcloud", "~/.config/gh",
    "~/.password-store", "~/Library/Keychains", "~/Library/Cookies",
    "~/Library/Application Support/Google/Chrome", "~/Library/Application Support/Microsoft Edge",
    "~/Library/Application Support/Firefox", "~/Library/Application Support/1Password",
    "~/.config/google-chrome", "~/.config/microsoft-edge", "~/.mozilla",
    "~/AppData/Roaming/Microsoft/Credentials", "~/AppData/Roaming/Microsoft/Protect", "~/AppData/Local/Microsoft/Credentials",
    "~/AppData/Local/Google/Chrome/User Data", "~/AppData/Local/Microsoft/Edge/User Data", "~/AppData/Roaming/Mozilla",
)
SECRET_NAMES = {".netrc", "_netrc", ".git-credentials", ".pgpass", ".npmrc", ".pypirc", "id_rsa", "id_dsa", "id_ecdsa",
                "id_ed25519", "login data", "cookies", "key3.db", "key4.db", "logins.json"}
SECRET_TEXT = "这是存放密码、密钥或登录状态的地方，为了安全不读（任何批准方式下都一样）。"


def expand(raw):
    """模型给的路径：~ 开头的换成用户主目录，反斜杠换成正斜杠。"""
    raw = raw.strip()
    if raw.startswith("~"):
        raw = os.path.expanduser(raw)
    return raw.replace("\\", "/")


def secret_roots(extra=()):
    out = []
    for d in tuple(SECRET_DIRS) + tuple(extra or ()):
        try:
            out.append(os.path.normcase(os.path.realpath(os.path.expanduser(d))))
        except (OSError, ValueError):
            continue
    return out


def is_secret(real, extra=()):
    """real（已经 realpath）在存密码、密钥的地方里，或者本身就是这类文件。extra：再加几个目录（本程序的数据目录）。"""
    if os.path.basename(real).lower() in SECRET_NAMES:
        return True
    r = os.path.normcase(real)
    for d in secret_roots(extra):
        try:
            if os.path.commonpath([d, r]) == d:
                return True
        except ValueError:
            continue
    return False


class PathError(Exception):
    pass


def resolve(root, rel, write=False):
    """把模型给的路径解析成工作区内的绝对路径。符号链接和 .. 逃逸会被拒绝。"""
    if not isinstance(rel, str) or not rel.strip():
        raise PathError("路径不能为空")
    rel = rel.strip().replace("\\", "/")
    realroot = os.path.realpath(root)
    cand = rel if os.path.isabs(rel) else os.path.join(realroot, rel)
    real = os.path.realpath(cand)
    try:
        inside = os.path.commonpath([realroot, real]) == realroot
    except ValueError:
        inside = False
    if not inside:
        raise PathError("路径在工作区之外：%s" % rel)
    parts = os.path.relpath(real, realroot).replace("\\", "/").split("/")
    if write and parts and parts[0] == INTERNAL_DIR:
        raise PathError("不能改动 %s 目录" % INTERNAL_DIR)
    return real


def relpath(root, path):
    return os.path.relpath(path, os.path.realpath(root)).replace("\\", "/")
