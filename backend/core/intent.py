"""Local input routing and validation of explicit dispatch constraints."""

import re

from backend.schemas import CruiseLimits, DispatchConstraints

HELP_MESSAGE = "请告诉我从哪里起飞、要去哪里，以及需要避开哪些区域。例如：从食堂送午餐到测国重楼顶。"
SCOPE_MESSAGE = "我可以在武大信息学部仿真场景中规划无人机配送和巡检。试着告诉我从哪里起飞、要去哪里，以及需要避开哪些区域。"
CLARIFY_INTENT_MESSAGE = "你是要配送还是巡检？可以告诉我起点和终点，例如从食堂配送到测国重楼顶。"

HELP = re.compile(r"^(?:你好|您好|嗨|hi|hello|帮助|使用帮助|怎么用|如何使用|能做什么|(?:你|voxaero)(?:怎么用|能做什么)|(?:你|系统|这里)(?:会|能)(?:规划|做|无人机)?(?:配送|巡检)吗)[.!?。！？\s]*$", re.I)
CHAT = re.compile(r"^(?:请|帮我|给我|能否|可以)*\s*(?:讲(?:个|一个|讲)|说(?:个|一个)|聊聊|写(?:一|个|首|段)|编(?:一|个|首)|翻译|天气|查(?:一下)?天气|介绍一下|解释一下|什么是|tell me a joke)", re.I)
DISPATCH_WORDS = re.compile(r"配送|送(?:到|往|至|餐|件|货)|派单|投送|巡检|安防|巡逻|起飞|航线|\b(?:deliver\w*|inspect\w*|patrol|dispatch)\b", re.I)
ROUTE_WORDS = re.compile(r"从.+(?:到|至|→|->)|飞往|前往|去|(?:->|→)|\b(?:from|to)\b", re.I)
LOW_FLIGHT = re.compile(r"(?:不要|别|不许|禁止|不走|避开|避免)(?:再|用|走|飞|进入)?\s*高空|低空(?:飞行|配送)|\b(?:no|avoid)\s+high\s+altitude\b", re.I)
UNSUPPORTED_WAYPOINT = re.compile(r"经由|途经|途径|中途(?:经过|停靠)|(?<!不要)(?<!别)(?<!不)经过")

NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千]+)"
METERS = r"(?:米|公尺|m(?![a-z]))"
TIME_UNIT = r"(?:分钟|秒钟|秒|小时|分(?=之?内|以内)|min(?![a-z])|s(?![a-z])|h(?![a-z]))"
SOFT = re.compile(r"尽量|最好|希望|优先|优先考虑|prefer|if possible", re.I)
HARD = re.compile(r"必须|务必|严格|硬性|must", re.I)
CLAUSES = re.compile(r"[，,。；;！!？?]|但是|但|同时|并且|且")
ALT_RANGE = re.compile(rf"(?P<low>{NUMBER})\s*(?:{METERS})?\s*[-–—~到至]\s*(?P<high>{NUMBER})\s*{METERS}", re.I)
MAX_OP = r"(?:不得|不要|不能|不)(?:超过|高于)|最多|至多|最高(?:为|是|飞到|飞行高度)?|上限(?:为|是)?|低于|小于|不到"
MIN_OP = r"(?:不得|不要|不能|不)低于|至少|最低(?:为|是)?|下限(?:为|是)?|高于|大于|超过"
ALT_LIMIT = re.compile(rf"(?P<op>{MAX_OP}|{MIN_OP})\s*(?P<value>{NUMBER})\s*{METERS}", re.I)
ALT_SUFFIX = re.compile(rf"(?P<value>{NUMBER})\s*{METERS}\s*(?P<op>以下|以内|以上)", re.I)
ALT_EXACT = re.compile(rf"(?:巡航(?:高度)?|飞行高度|高度)\s*(?:(?:必须|尽量|最好|希望|保持|设置|设为|为|固定|在|是)\s*)+(?P<value>{NUMBER})\s*{METERS}", re.I)
ALT_CONTEXT = re.compile(r"巡航|高空|限高|高度|低飞|低空|飞行|航高")
MEASUREMENT = re.compile(rf"{NUMBER}\s*{METERS}", re.I)
DEADLINE = re.compile(
    rf"(?:(?:限时|用时(?:不超过)?|耗时(?:不超过)?|(?:必须|务必|尽量|最好|请)?\s*在)\s*)?"
    rf"(?P<number>{NUMBER})\s*(?P<unit>{TIME_UNIT})\s*(?P<within>之内|以内|内)?", re.I,
)
DEADLINE_CONTEXT = re.compile(r"送到|送达|完成|抵达|到达|限时|时限|用时|耗时|晚于")
WALL_CLOCK = re.compile(r"(?:\d{1,2}[:：]\d{2}|[一二两三四五六七八九十\d]+点(?:半|\d+分)?)\s*(?:之前|以前|前).*(?:送到|送达|完成|到达)")


def _number(text: str) -> float:
    if text[0].isascii():
        return float(text)
    digits = dict(zip("零一二三四五六七八九", range(10))) | {"〇": 0, "两": 2}
    units = {"十": 10, "百": 100, "千": 1000}
    total = current = 0
    for character in text:
        if character in units:
            total += (current or 1) * units[character]
            current = 0
        else:
            current = current * 10 + digits[character]
    return float(total + current)


def _bound(value: float, operation: str) -> CruiseLimits:
    if operation in {"以下", "以内"} or re.fullmatch(MAX_OP, operation):
        return CruiseLimits(max_alt=value, max_exclusive=operation in {"低于", "小于", "不到"})
    return CruiseLimits(min_alt=value, min_exclusive=operation in {"高于", "大于", "超过"})


def intersect_limits(left: CruiseLimits | None, right: CruiseLimits | None) -> CruiseLimits | None:
    if left is None or right is None:
        return left or right
    lower, lower_strict = max(((limit.min_alt, limit.min_exclusive) for limit in (left, right)
                              if limit.min_alt is not None), default=(None, False))
    upper, upper_inclusive = min(((limit.max_alt, not limit.max_exclusive) for limit in (left, right)
                                 if limit.max_alt is not None), default=(None, True))
    return CruiseLimits(min_alt=lower, max_alt=upper, min_exclusive=lower_strict, max_exclusive=not upper_inclusive)


def parse_constraints(query: str) -> DispatchConstraints:
    hard_altitude = soft_altitude = None
    deadlines = {False: [], True: []}
    if WALL_CLOCK.search(query):
        raise ValueError("请把送达时刻换成飞行时限，例如‘起飞后 60 秒内送达’。")
    for clause in CLAUSES.split(query):
        soft = bool(SOFT.search(clause) and not HARD.search(clause))
        ranges = list(ALT_RANGE.finditer(clause))
        limits = [CruiseLimits(min_alt=_number(item["low"]), max_alt=_number(item["high"])) for item in ranges]
        matches = (item for pattern in (ALT_LIMIT, ALT_SUFFIX) for item in pattern.finditer(clause)
                   if not any(item.start() < span.end() and span.start() < item.end() for span in ranges))
        limits.extend(_bound(_number(item["value"]), item["op"]) for item in matches)
        for item in ALT_EXACT.finditer(clause):
            altitude = _number(item["value"])
            limits.append(CruiseLimits(min_alt=altitude, max_alt=altitude))
        if "全程" in clause and limits:
            raise ValueError("当前支持巡航高度约束，请明确巡航阶段所需的高度范围。")
        if not limits and ALT_CONTEXT.search(clause) and MEASUREMENT.search(clause):
            raise ValueError("请明确巡航高度条件，例如‘巡航不超过 50 米’。")
        for limit in limits:
            if soft:
                soft_altitude = intersect_limits(soft_altitude, limit)
            else:
                hard_altitude = intersect_limits(hard_altitude, limit)
        for item in DEADLINE.finditer(clause):
            if item["within"] or DEADLINE_CONTEXT.search(clause):
                unit = item["unit"].lower()
                multiplier = 3600 if unit in {"小时", "h"} else 60 if unit in {"分钟", "分", "min"} else 1
                deadlines[soft].append(_number(item["number"]) * multiplier)
    return DispatchConstraints(cruise=hard_altitude, preferred_cruise=soft_altitude,
                               deadline_seconds=min(deadlines[False], default=None),
                               preferred_deadline_seconds=min(deadlines[True], default=None))
