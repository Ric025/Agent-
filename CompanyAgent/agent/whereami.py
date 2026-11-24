"""用户在哪：设置里填的所在城市优先；没填时按电脑的时区推断到"哪个国家哪一带、最可能是哪个城市"。

为什么不用 IP 查：公司的网络出口常常不在本地（走 VPN、总部出口），按 IP 查出来的城市是错的；时区是用户自己电脑上的设置，
不联网、不出错得那么离谱。精度有限：一个时区盖一大片（中国全国一个时区），所以有的时区只给国家、不给城市。

模型拿到的是写好的一句话（context.env_lines），不让它自己从"美东"去猜城市——小模型会猜错。"""
from __future__ import annotations
import os, subprocess, sys, time

# 时区 → (哪一带, 最可能的城市；空字符串表示这个时区盖的范围太大，城市猜不出来)
TZ_PLACES = {
    "America/Toronto": ("加拿大东部", "多伦多"), "America/Montreal": ("加拿大东部", "蒙特利尔"),
    "America/Vancouver": ("加拿大西海岸", "温哥华"), "America/Edmonton": ("加拿大阿尔伯塔省", "卡尔加里"),
    "America/Winnipeg": ("加拿大中部", "温尼伯"), "America/Halifax": ("加拿大大西洋沿岸", "哈利法克斯"),
    "America/Regina": ("加拿大萨斯喀彻温省", "里贾纳"), "America/St_Johns": ("加拿大纽芬兰", "圣约翰斯"),
    "America/New_York": ("美国东部", "纽约"), "America/Detroit": ("美国东部", "底特律"),
    "America/Chicago": ("美国中部", "芝加哥"), "America/Denver": ("美国山地时区", "丹佛"),
    "America/Phoenix": ("美国亚利桑那州", "凤凰城"), "America/Los_Angeles": ("美国西海岸", "洛杉矶"),
    "America/Anchorage": ("美国阿拉斯加", "安克雷奇"), "Pacific/Honolulu": ("美国夏威夷", "檀香山"),
    "America/Mexico_City": ("墨西哥", "墨西哥城"), "America/Monterrey": ("墨西哥", "蒙特雷"),
    "America/Tijuana": ("墨西哥", "蒂华纳"), "America/Sao_Paulo": ("巴西", "圣保罗"),
    "America/Bogota": ("哥伦比亚", "波哥大"), "America/Lima": ("秘鲁", "利马"), "America/Santiago": ("智利", "圣地亚哥"),
    "America/Argentina/Buenos_Aires": ("阿根廷", "布宜诺斯艾利斯"), "America/Panama": ("巴拿马", "巴拿马城"),
    "Europe/London": ("英国", "伦敦"), "Europe/Dublin": ("爱尔兰", "都柏林"), "Europe/Paris": ("法国", "巴黎"),
    "Europe/Berlin": ("德国", "柏林"), "Europe/Madrid": ("西班牙", "马德里"), "Europe/Rome": ("意大利", "罗马"),
    "Europe/Amsterdam": ("荷兰", "阿姆斯特丹"), "Europe/Brussels": ("比利时", "布鲁塞尔"), "Europe/Zurich": ("瑞士", "苏黎世"),
    "Europe/Vienna": ("奥地利", "维也纳"), "Europe/Stockholm": ("瑞典", "斯德哥尔摩"), "Europe/Oslo": ("挪威", "奥斯陆"),
    "Europe/Copenhagen": ("丹麦", "哥本哈根"), "Europe/Helsinki": ("芬兰", "赫尔辛基"), "Europe/Warsaw": ("波兰", "华沙"),
    "Europe/Prague": ("捷克", "布拉格"), "Europe/Budapest": ("匈牙利", "布达佩斯"), "Europe/Lisbon": ("葡萄牙", "里斯本"),
    "Europe/Athens": ("希腊", "雅典"), "Europe/Istanbul": ("土耳其", "伊斯坦布尔"), "Europe/Moscow": ("俄罗斯西部", "莫斯科"),
    "Asia/Shanghai": ("中国", ""), "Asia/Chongqing": ("中国", ""), "Asia/Harbin": ("中国", ""), "PRC": ("中国", ""),
    "Asia/Urumqi": ("中国新疆", "乌鲁木齐"), "Asia/Hong_Kong": ("中国香港", "香港"), "Asia/Macau": ("中国澳门", "澳门"),
    "Asia/Taipei": ("中国台湾", "台北"), "Asia/Tokyo": ("日本", "东京"), "Asia/Seoul": ("韩国", "首尔"),
    "Asia/Singapore": ("新加坡", "新加坡"), "Asia/Kuala_Lumpur": ("马来西亚", "吉隆坡"), "Asia/Bangkok": ("泰国", "曼谷"),
    "Asia/Jakarta": ("印度尼西亚", "雅加达"), "Asia/Manila": ("菲律宾", "马尼拉"), "Asia/Ho_Chi_Minh": ("越南", "胡志明市"),
    "Asia/Kolkata": ("印度", ""), "Asia/Calcutta": ("印度", ""), "Asia/Karachi": ("巴基斯坦", "卡拉奇"),
    "Asia/Dubai": ("阿联酋", "迪拜"), "Asia/Riyadh": ("沙特阿拉伯", "利雅得"), "Asia/Qatar": ("卡塔尔", "多哈"),
    "Asia/Jerusalem": ("以色列", "特拉维夫"), "Asia/Tel_Aviv": ("以色列", "特拉维夫"),
    "Australia/Sydney": ("澳大利亚东部", "悉尼"), "Australia/Melbourne": ("澳大利亚东部", "墨尔本"),
    "Australia/Brisbane": ("澳大利亚昆士兰", "布里斯班"), "Australia/Perth": ("澳大利亚西部", "珀斯"),
    "Australia/Adelaide": ("澳大利亚南部", "阿德莱德"), "Pacific/Auckland": ("新西兰", "奥克兰"),
    "Africa/Johannesburg": ("南非", "约翰内斯堡"), "Africa/Cairo": ("埃及", "开罗"), "Africa/Lagos": ("尼日利亚", "拉各斯"),
    "Africa/Nairobi": ("肯尼亚", "内罗毕"), "Africa/Casablanca": ("摩洛哥", "卡萨布兰卡"),
}
CONTINENTS = {"America": "美洲", "Europe": "欧洲", "Asia": "亚洲", "Africa": "非洲", "Australia": "澳大利亚", "Pacific": "太平洋地区",
              "Atlantic": "大西洋地区", "Indian": "印度洋地区"}

# Windows 的时区名（tzutil /g）→ 时区。Windows 的一个名字常常盖好几个国家，所以这里的"哪一带"另写，不带城市。
WIN_TZ = {
    "China Standard Time": ("Asia/Shanghai", None), "Taipei Standard Time": ("Asia/Taipei", None),
    "Tokyo Standard Time": ("Asia/Tokyo", None), "Korea Standard Time": ("Asia/Seoul", None),
    "Singapore Standard Time": ("Asia/Singapore", ("新加坡或马来西亚", "")), "SE Asia Standard Time": ("Asia/Bangkok", ("东南亚", "")),
    "India Standard Time": ("Asia/Kolkata", None), "Arabian Standard Time": ("Asia/Dubai", ("海湾地区", "")),
    "Israel Standard Time": ("Asia/Jerusalem", None), "Turkey Standard Time": ("Europe/Istanbul", None),
    "Russian Standard Time": ("Europe/Moscow", None), "GMT Standard Time": ("Europe/London", ("英国或爱尔兰", "")),
    "W. Europe Standard Time": ("Europe/Berlin", ("欧洲中部（德国、荷兰、意大利、瑞士一带）", "")),
    "Romance Standard Time": ("Europe/Paris", ("欧洲西部（法国、西班牙、比利时一带）", "")),
    "Central Europe Standard Time": ("Europe/Budapest", ("欧洲中部", "")),
    "Central European Standard Time": ("Europe/Warsaw", ("欧洲中部", "")),
    "Eastern Standard Time": ("America/New_York", ("北美东部（美国东部或加拿大东部）", "")),
    "Central Standard Time": ("America/Chicago", ("北美中部", "")),
    "Mountain Standard Time": ("America/Denver", ("北美山地时区", "")),
    "Pacific Standard Time": ("America/Los_Angeles", ("北美西海岸（美国或加拿大）", "")),
    "US Mountain Standard Time": ("America/Phoenix", None), "Alaskan Standard Time": ("America/Anchorage", None),
    "Hawaiian Standard Time": ("Pacific/Honolulu", None), "Atlantic Standard Time": ("America/Halifax", None),
    "Canada Central Standard Time": ("America/Regina", None), "Newfoundland Standard Time": ("America/St_Johns", None),
    "Central Standard Time (Mexico)": ("America/Mexico_City", None), "E. South America Standard Time": ("America/Sao_Paulo", None),
    "AUS Eastern Standard Time": ("Australia/Sydney", ("澳大利亚东部", "")), "W. Australia Standard Time": ("Australia/Perth", None),
    "New Zealand Standard Time": ("Pacific/Auckland", None), "South Africa Standard Time": ("Africa/Johannesburg", None),
    "Egypt Standard Time": ("Africa/Cairo", None), "UTC": ("UTC", None),
}
WEEKDAYS = "一二三四五六日"
LOCATION_MAX = 40

_win_cache = {"at": 0.0, "v": ("", None)}


def _from_path(real):
    """/var/db/timezone/zoneinfo/America/Toronto → America/Toronto。"""
    real = (real or "").replace("\\", "/")
    i = real.rfind("zoneinfo/")
    if i < 0:
        return ""
    name = real[i + len("zoneinfo/"):].strip("/")
    for pre in ("posix/", "right/"):
        if name.startswith(pre):
            name = name[len(pre):]
    return name


def _windows():
    now = time.time()
    if now - _win_cache["at"] < 600:
        return _win_cache["v"]
    v = ("", None)
    try:
        p = subprocess.run(["tzutil", "/g"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        key = p.stdout.decode("ascii", "replace").strip()
        if key.endswith("_dstoff"):
            key = key[:-len("_dstoff")]
        v = WIN_TZ.get(key) or ("", None)
    except (OSError, subprocess.SubprocessError):
        pass
    _win_cache.update(at=now, v=v)
    return v


def detect():
    """(时区名, 这一带的说法或 None)。读不出来时时区名是空字符串。"""
    env = (os.environ.get("TZ") or "").strip().lstrip(":")
    if env:
        name = _from_path(env) or (env if "/" in env else "")
        if name:
            return name, None
    if sys.platform == "win32":
        return _windows()
    try:
        name = _from_path(os.path.realpath("/etc/localtime"))
    except OSError:
        name = ""
    if not name:
        try:
            with open("/etc/timezone", encoding="ascii", errors="replace") as f:
                name = f.read().strip()
        except OSError:
            name = ""
    return name, None


def utc_offset(t=None):
    """"+08:00"、"-05:00"。"""
    lt = time.localtime(t)
    off = getattr(lt, "tm_gmtoff", None)
    if off is None:
        off = -(time.altzone if lt.tm_isdst > 0 else time.timezone)
    sign = "+" if off >= 0 else "-"
    off = abs(int(off))
    return "%s%02d:%02d" % (sign, off // 3600, off % 3600 // 60)


def place(tz, override=None):
    """时区 → (哪一带, 最可能的城市)。认不出来、或者是 UTC 这种看不出地方的：("", "")。"""
    if override:
        return override
    if not tz or tz.upper() in ("UTC", "GMT", "ETC/UTC", "ETC/GMT", "UCT", "ZULU") or tz.startswith("Etc/"):
        return "", ""
    if tz in TZ_PLACES:
        return TZ_PLACES[tz]
    parts = tz.split("/")
    if len(parts) >= 2 and parts[0] in CONTINENTS:
        return CONTINENTS[parts[0]], parts[-1].replace("_", " ")
    return "", ""


def _sp(city):
    """英文城市名前面空一格（"最可能是 Indianapolis"）。"""
    return (" " + city) if city and city[0].isascii() else city


def clean_location(v):
    """设置里的"所在城市"：一行字，最多 LOCATION_MAX 个字。不对就抛 ValueError。"""
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError("所在城市应该是一段文字")
    s = " ".join(v.split())
    if len(s) > LOCATION_MAX:
        raise ValueError("所在城市最多 %d 个字" % LOCATION_MAX)
    if any(ord(c) < 32 for c in s):
        raise ValueError("所在城市里有不能用的字符")
    return s


def where(cfg=None):
    """{"setting", "tz", "offset", "region", "city", "source", "text"}。text 是给人看的一句话。"""
    setting = ""
    try:
        setting = clean_location((cfg.get("user_location") if cfg is not None else "") or "")
    except (ValueError, AttributeError, TypeError):
        setting = ""
    tz, override = detect()
    region, city = place(tz, override)
    off = utc_offset()
    if setting:
        source, text = "setting", setting
    elif region and city:
        source, text = "timezone", "%s，最可能是%s" % (region, _sp(city))
    elif region:
        source, text = "timezone", "%s（具体城市不知道）" % region
    else:
        source, text = "", ""
    return {"setting": setting, "tz": tz, "offset": off, "region": region, "city": city, "source": source, "text": text}


def now_text(t=None):
    """"2026-11-23 周一 17:02"：给 system_info 用（不放进系统提示：每分钟都变，会让模型服务的缓存失效）。"""
    lt = time.localtime(t)
    return "%s 周%s %s" % (time.strftime("%Y-%m-%d", lt), WEEKDAYS[lt.tm_wday], time.strftime("%H:%M", lt))


def prompt_lines(cfg=None):
    """系统提示"当前环境"里讲所在地的两行。"""
    w = where(cfg)
    tzs = "%s，UTC%s" % (w["tz"], w["offset"]) if w["tz"] else "UTC%s" % w["offset"]
    if w["source"] == "setting":
        first = "用户所在地：%s（用户在设置里填的）。电脑时区：%s。" % (w["setting"], tzs)
    elif w["region"] and w["city"]:
        first = "用户所在地：按电脑时区（%s）推断在%s，最可能是%s。" % (tzs, w["region"], _sp(w["city"]))
    elif w["region"]:
        first = "用户所在地：按电脑时区（%s）推断在%s，具体城市不知道。" % (tzs, w["region"])
    else:
        first = "用户所在地：不知道（电脑时区：%s，看不出在哪）。" % tzs
    rule = ("用到所在地的事（天气、附近、当地时间、时差）直接按上面的所在地做，并用一句话说明是按什么判断的；"
            "连城市都不知道时才问。用户说过自己在哪（对话里、记忆里）以用户说的为准，并用 memory 记下。现在几点用 system_info 查。")
    return [first, rule]
