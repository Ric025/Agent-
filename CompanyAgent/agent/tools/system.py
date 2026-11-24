"""看这台电脑的配置：系统、型号、处理器、内存、磁盘、显卡和屏幕；还有当地时间、时区、所在地（agent/whereami.py）。只读，不联网。

macOS 用 sysctl 和 system_profiler；Windows 用 PowerShell 的 Get-CimInstance；Linux 读 /proc 和 /sys。
每个命令都有超时，拿不到的项就不写，不报错。不取序列号、硬件 UUID 这类能唯一识别这台电脑的信息。"""
from __future__ import annotations
import getpass, json, os, platform, shutil, subprocess, sys, time
from . import tool

CMD_TIMEOUT = 20


def _run(cmd, timeout=CMD_TIMEOUT):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.decode("utf-8", "replace").strip() if p.returncode == 0 else ""


def gb(n):
    return "%.1f GB" % (n / 1024 ** 3) if n else ""


def _uptime_text(sec):
    if not sec or sec < 0:
        return ""
    d, rest = divmod(int(sec), 86400)
    h, m = rest // 3600, rest % 3600 // 60
    return ("%d 天 %d 小时" % (d, h)) if d else ("%d 小时 %d 分钟" % (h, m))


# ---------------- macOS ----------------

def mac_from_profiler(data):
    """system_profiler SPHardwareDataType SPDisplaysDataType -json 的结果 → 型号、芯片、显卡、屏幕。"""
    out = {}
    hw = ((data or {}).get("SPHardwareDataType") or [{}])[0] or {}
    if hw.get("machine_name"):
        out["型号"] = "%s（%s）" % (hw["machine_name"], hw["machine_model"]) if hw.get("machine_model") else hw["machine_name"]
    chip = hw.get("chip_type") or hw.get("cpu_type")
    if chip:
        speed = hw.get("current_processor_speed")
        out["芯片"] = "%s %s" % (chip, speed) if speed else chip
    procs = str(hw.get("number_processors") or "")
    if procs.startswith("proc "):               # "proc 12:8:4" = 共 12 核：8 个性能核、4 个能效核
        parts = procs[5:].split(":")
        if len(parts) == 3:
            out["核心"] = "%s 核（%s 个性能核、%s 个能效核）" % tuple(parts)
    elif procs:
        out["核心"] = "%s 核" % procs
    if hw.get("physical_memory"):
        out["内存"] = hw["physical_memory"]
    gpus, screens = [], []
    for g in (data or {}).get("SPDisplaysDataType") or []:
        name = g.get("sppci_model") or g.get("_name")
        if not name:
            continue
        extra = []
        if g.get("sppci_cores"):
            extra.append("%s 核" % g["sppci_cores"])
        vram = g.get("spdisplays_vram") or g.get("spdisplays_vram_shared")
        if vram:
            extra.append("显存 %s" % vram)
        gpus.append(name + ("（%s）" % "，".join(extra) if extra else ""))
        for d in g.get("spdisplays_ndrvs") or []:
            res = d.get("_spdisplays_resolution") or d.get("spdisplays_resolution") or ""
            px = d.get("_spdisplays_pixels") or ""
            nm = d.get("_name") or "屏幕"
            if nm == "Color LCD":
                nm = "内置屏幕"
            txt = nm
            if res:
                txt += "：%s" % res.replace(" @ ", "，")
            if px and px.split(" @")[0] not in res:
                txt += "（物理像素 %s）" % px
            screens.append(txt)
    if gpus:
        out["显卡"] = "；".join(gpus)
    if screens:
        out["屏幕"] = "；".join(screens)
    return out


def _mac(info):
    ver = platform.mac_ver()[0]
    info["系统"] = "macOS %s" % ver if ver else "macOS"
    raw = _run(["system_profiler", "SPHardwareDataType", "SPDisplaysDataType", "-json"])
    try:
        info.update(mac_from_profiler(json.loads(raw)) if raw else {})
    except ValueError:
        pass
    if "芯片" not in info:
        brand = _run(["sysctl", "-n", "machdep.cpu.brand_string"], 5)
        if brand:
            info["芯片"] = brand
    if "内存" not in info:
        mem = _run(["sysctl", "-n", "hw.memsize"], 5)
        if mem.isdigit():
            info["内存"] = gb(int(mem))
    vm = _run(["vm_stat"], 5)
    if vm:
        page, free = 4096, 0
        for line in vm.splitlines():
            if "page size of" in line:
                try:
                    page = int(line.split("page size of")[1].split()[0])
                except (IndexError, ValueError):
                    pass
            for key in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
                if line.startswith(key + ":"):
                    try:
                        free += int(line.split(":")[1].strip().rstrip("."))
                    except ValueError:
                        pass
        if free:
            info["内存可用"] = "约 %s" % gb(free * page)
    boot = _run(["sysctl", "-n", "kern.boottime"], 5)          # { sec = 1700000000, usec = 0 } ...
    if "sec =" in boot:
        try:
            info["开机时长"] = _uptime_text(time.time() - int(boot.split("sec =")[1].split(",")[0]))
        except (IndexError, ValueError):
            pass


# ---------------- Windows ----------------

WIN_PS = ("$ErrorActionPreference='SilentlyContinue';"
          "$o=[ordered]@{};"
          "$c=Get-CimInstance Win32_ComputerSystem; $o.maker=$c.Manufacturer; $o.model=$c.Model; $o.mem=$c.TotalPhysicalMemory;"
          "$o.cpu=@(Get-CimInstance Win32_Processor | ForEach-Object { $_.Name.Trim() });"
          "$o.cores=(Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum;"
          "$o.gpu=@(Get-CimInstance Win32_VideoController | ForEach-Object { @{name=$_.Name; ram=$_.AdapterRAM; res=('' + $_.CurrentHorizontalResolution + ' x ' + $_.CurrentVerticalResolution)} });"
          "$os=Get-CimInstance Win32_OperatingSystem; $o.os=$os.Caption; $o.build=$os.BuildNumber; $o.free=$os.FreePhysicalMemory;"
          "$o | ConvertTo-Json -Depth 4 -Compress")


def win_from_ps(d):
    out = {}
    if d.get("os"):
        out["系统"] = "%s（版本 %s）" % (d["os"], d["build"]) if d.get("build") else d["os"]
    maker, model = (d.get("maker") or "").strip(), (d.get("model") or "").strip()
    if maker or model:
        out["型号"] = " ".join(x for x in (maker, model) if x)
    cpus = d.get("cpu") or []
    cpus = cpus if isinstance(cpus, list) else [cpus]
    if cpus:
        out["处理器"] = "；".join(str(c) for c in cpus if c)
    if d.get("cores"):
        out["核心"] = "%s 核" % d["cores"]
    if d.get("mem"):
        out["内存"] = gb(int(d["mem"]))
    if d.get("free"):
        out["内存可用"] = "约 %s" % gb(int(d["free"]) * 1024)
    gs = d.get("gpu") or []
    gs = gs if isinstance(gs, list) else [gs]
    names, res = [], []
    for g in gs:
        if not isinstance(g, dict) or not g.get("name"):
            continue
        ram = g.get("ram")
        names.append(g["name"] + ("（显存 %s）" % gb(int(ram)) if ram and int(ram) > 0 else ""))
        r = (g.get("res") or "").strip()
        if r and r != "x" and r[0].isdigit():
            res.append(r)
    if names:
        out["显卡"] = "；".join(names)
    if res:
        out["屏幕"] = "；".join(res)
    return out


def _windows(info):
    info["系统"] = "Windows %s" % platform.release()
    raw = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", WIN_PS])
    try:
        info.update(win_from_ps(json.loads(raw)) if raw else {})
    except (ValueError, TypeError):
        pass
    if "处理器" not in info and platform.processor():
        info["处理器"] = platform.processor()
    if "内存" not in info:
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
            m = MS()
            m.len = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                info["内存"], info["内存可用"] = gb(m.total), "约 %s" % gb(m.avail)
        except Exception:
            pass
    try:
        import ctypes
        info["开机时长"] = _uptime_text(ctypes.windll.kernel32.GetTickCount64() / 1000)
    except Exception:
        pass


# ---------------- Linux ----------------

def _read(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _linux(info):
    rel = _read("/etc/os-release")
    name = next((ln.split("=", 1)[1].strip().strip('"') for ln in rel.splitlines() if ln.startswith("PRETTY_NAME=")), "")
    info["系统"] = "%s（内核 %s）" % (name or "Linux", platform.release())
    prod = " ".join(x for x in (_read("/sys/class/dmi/id/sys_vendor").strip(), _read("/sys/class/dmi/id/product_name").strip()) if x)
    if prod:
        info["型号"] = prod
    cpu = next((ln.split(":", 1)[1].strip() for ln in _read("/proc/cpuinfo").splitlines() if ln.lower().startswith("model name")), "")
    if cpu:
        info["处理器"] = cpu
    mem = {}
    for ln in _read("/proc/meminfo").splitlines():
        k, _, v = ln.partition(":")
        if v.strip().endswith("kB"):
            try:
                mem[k] = int(v.split()[0]) * 1024
            except ValueError:
                pass
    if mem.get("MemTotal"):
        info["内存"] = gb(mem["MemTotal"])
    if mem.get("MemAvailable"):
        info["内存可用"] = "约 %s" % gb(mem["MemAvailable"])
    up = _read("/proc/uptime").split()
    if up:
        try:
            info["开机时长"] = _uptime_text(float(up[0]))
        except ValueError:
            pass
    if shutil.which("lspci"):
        gpus = [ln.split(":", 2)[-1].strip() for ln in _run(["lspci"], 5).splitlines()
                if " VGA " in ln or "3D controller" in ln or "Display controller" in ln]
        if gpus:
            info["显卡"] = "；".join(gpus)


# ---------------- 磁盘 ----------------

def disks():
    roots = []
    if sys.platform == "win32":
        roots = ["%s:\\" % c for c in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists("%s:\\" % c)]
    else:
        roots = ["/"]
        if sys.platform == "darwin" and os.path.isdir("/Volumes"):
            roots += [os.path.join("/Volumes", n) for n in sorted(os.listdir("/Volumes"))]
        home = os.path.expanduser("~")
        if home not in roots:
            roots.append(home)
    out, seen = [], set()
    for r in roots:
        try:
            u = shutil.disk_usage(r)
        except OSError:
            continue
        key = (u.total, round(u.free / 1024 ** 3))       # 同一块盘的不同挂载点（/、/Volumes/Macintosh HD、主目录）只列一次
        if key in seen or u.total <= 0:
            continue
        seen.add(key)
        out.append("%s：共 %s，剩 %s（%d%%）" % (r, gb(u.total), gb(u.free), round(u.free * 100 / u.total)))
    return out


def collect(cfg=None):
    info = {}
    try:
        if sys.platform == "darwin":
            _mac(info)
        elif sys.platform == "win32":
            _windows(info)
        else:
            _linux(info)
    except Exception:
        pass
    info.setdefault("系统", "%s %s" % (platform.system(), platform.release()))
    info["架构"] = platform.machine()
    if "核心" not in info and os.cpu_count():
        info["核心"] = "%d 个逻辑核" % os.cpu_count()
    d = disks()
    if d:
        info["磁盘"] = d
    info["电脑名"] = platform.node()
    try:
        info["用户"] = getpass.getuser()
    except Exception:
        pass
    info["主目录"] = os.path.expanduser("~")
    try:
        from .. import whereami
        w = whereami.where(cfg)
        info["当地时间"] = whereami.now_text()
        info["时区"] = "%s（UTC%s）" % (w["tz"], w["offset"]) if w["tz"] else "UTC%s" % w["offset"]
        if w["text"]:
            info["所在地"] = w["text"] + ("（用户在设置里填的）" if w["source"] == "setting" else "（按时区推断）")
    except Exception:
        info["时区"] = time.strftime("%Z") or ""
    info["本程序用的 Python"] = "%s（%s）" % (platform.python_version(), sys.executable)
    order = ["系统", "型号", "芯片", "处理器", "核心", "内存", "内存可用", "显卡", "屏幕", "磁盘", "架构", "开机时长",
             "电脑名", "用户", "主目录", "当地时间", "时区", "所在地", "本程序用的 Python"]
    return {k: info[k] for k in order if info.get(k)}


@tool("system_info", "查看电脑配置：系统、处理器、内存、磁盘、显卡", {}, required=[], sub_ok=True)
def system_info(ctx):
    return collect(getattr(ctx, "cfg", None))
