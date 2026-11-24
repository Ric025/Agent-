"""技能：一个文件夹加一份 SKILL.md（公开的 Agent Skills 格式）。

两个来源：程序目录下的 skills/ 是内置的（builtin），数据目录下的 skills/ 是用户确认后保存的（user）。
重名时内置的优先：skill_save 不允许和内置技能重名，手工放进数据目录的同名技能也不会盖掉内置的。"""
from __future__ import annotations
import os, re, threading


def parse(raw):
    """SKILL.md 的文字 → (元信息 dict, 正文)。"""
    meta, body = {}, raw
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", raw, re.S)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"')
    return meta, body


class Skills:
    def __init__(self, dirs):
        """dirs：目录列表。每项是路径，或 (路径, 来源)。只给路径时，第一个算内置，其余算用户的。"""
        self.dirs = []
        for i, d in enumerate(dirs):
            if isinstance(d, (tuple, list)):
                self.dirs.append((d[0], d[1]))
            else:
                self.dirs.append((d, "builtin" if i == 0 else "user"))
        self.lock = threading.Lock()
        self.items = {}
        self.reload()

    def reload(self):
        """重新读一遍技能目录。新字典建好后一次换上，读的人不会看到读了一半的状态。"""
        items = {}
        for d, source in self.dirs:
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                f = os.path.join(d, name, "SKILL.md")
                if not os.path.isfile(f):
                    continue
                try:
                    with open(f, encoding="utf-8") as fh:
                        raw = fh.read()
                except (OSError, UnicodeDecodeError):
                    continue
                meta, body = parse(raw)
                key = meta.get("name") or name
                if key in items and items[key]["source"] == "builtin" and source != "builtin":
                    continue
                from .modes import parse_modes
                from .toolsets import parse_tools_meta
                # tools：技能要用的工具组或工具名（docs/借鉴开源项目.md 3.3 第 4 条），没写就是 None，按 toolsets.SKILL_GROUPS
                items[key] = {"name": key, "description": meta.get("description", ""), "body": body.strip(), "path": f,
                              "source": source, "dir": os.path.dirname(f), "modes": parse_modes(meta.get("modes")),
                              "tools": parse_tools_meta(meta.get("tools"))}
        with self.lock:
            self.items = items
        return self

    def dir_of(self, source):
        """某个来源的目录（user：数据目录下的 skills/）。"""
        for d, s in self.dirs:
            if s == source:
                return d
        return None

    def get(self, name):
        return self.items.get(name)

    def names(self):
        return list(self.items)

    def index(self, mode=None):
        """技能清单。给了 mode 就只列这个模式的（元信息里写 modes: [hspace] 之类；不写两边都有）。"""
        from .modes import visible
        return [{"name": s["name"], "description": s["description"], "source": s["source"], "modes": list(s.get("modes") or [])}
                for s in self.items.values() if mode is None or visible(mode, s.get("modes"))]
