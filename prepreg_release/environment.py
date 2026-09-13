"""开袋回温与环境暴露校核：开封/测温/环境读数/暂停/覆盖/恢复事件重放。

背景：冷藏预浸料一出库即拆袋时，若材料表面温度仍低于车间空气露点会
凝露；铺层中途湿度越限或敞开放置过久，外置寿命台账反映不了。本模块按
追加式事件链（与铺放事件同一张 events 表，只允许 INSERT）按时标还原
每个材料单元的开封段与每个未封闭铺层表面（层×分区）的暴露区间，逐段
核算最低回温时长、开袋露点裕量、温湿度越限持续时间、采样断档与允许
敞开时长。

规范（spec.environment，缺省即不启用，保持老工单兼容）：
  {
    "defaults": {"min_warmup_minutes": 120, "dew_point_margin_c": 3.0,
                 "temp_min_c": 18.0, "temp_max_c": 27.0,
                 "rh_min_pct": 30.0, "rh_max_pct": 65.0,
                 "max_sample_interval_min": 30, "max_open_minutes": 480},
    "materials": {"CF-EP-3K": {...覆盖项...}},
    "zones":     {"Z1": {...覆盖项...}}
  }
材料级覆盖回温/露点/温湿度/敞开时长；分区级覆盖露点/温湿度/采样/敞开。

现场追加事件（均带可解析 at 时标）：
  package_opened        包装开封 {at, unit?/roll?, material?}
  material_temp_reading 材料温度 {at, unit?/roll?, temp_c, probe_id?}
  environment_reading   车间读数 {at, temp_c, rh_pct, zone?, probe_id?}
  layup_paused          暂停   {at, zones?, plies?}（暂停不停暴露时钟）
  surface_covered       覆盖   {at, zones?, plies?}（覆盖暂停暴露时钟）
  layup_resumed         恢复   {at, zones?, plies?}（配对暂停或覆盖）

后两类读数可带 alternative=true + reason + amends_event 派生修订，
或带 backfilled=true 补录；补录只能追加、允许时标回退且不得覆盖旧事件。
"""

import math

from .core import parse_time

ENV_EVENT_TYPES = {
    "package_opened", "material_temp_reading", "environment_reading",
    "layup_paused", "surface_covered", "layup_resumed",
}

_LIMIT_KEYS = (
    "min_warmup_minutes", "dew_point_margin_c",
    "temp_min_c", "temp_max_c", "rh_min_pct", "rh_max_pct",
    "max_sample_interval_min", "max_open_minutes",
)
_MATERIAL_KEYS = ("min_warmup_minutes", "dew_point_margin_c",
                  "temp_min_c", "temp_max_c", "rh_min_pct", "rh_max_pct",
                  "max_open_minutes")
_ZONE_KEYS = ("dew_point_margin_c", "temp_min_c", "temp_max_c",
              "rh_min_pct", "rh_max_pct", "max_sample_interval_min",
              "max_open_minutes")
_POSITIVE_KEYS = ("min_warmup_minutes", "max_sample_interval_min",
                  "max_open_minutes")


# ---------------------------------------------------------------- 工具

def _num(x):
    """有限数值；bool 不算。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x)


def dew_point_c(temp_c, rh_pct):
    """Magnus 公式露点（℃）；输入越界返回 None。"""
    t, rh = _num(temp_c), _num(rh_pct)
    if t is None or rh is None or rh <= 0 or rh > 100 or t <= -45 or t > 60:
        return None
    a, b = 17.62, 243.12
    gamma = a * t / (b + t) + math.log(rh / 100.0)
    return b * gamma / (a - gamma)


def _iso(t):
    return t.isoformat() if t else None


def _subtract_windows(segments, windows):
    """从段集合中扣除保护窗 [(a,b)]；窗 b=None 表示开放（切掉 a 之后的尾）。"""
    out = []
    for s, e in segments:
        cur = [(s, e)]
        for a, b in windows:
            nxt = []
            for x, y in cur:
                if b is not None and b <= x:
                    nxt.append((x, y))
                    continue
                if a >= y:
                    nxt.append((x, y))
                    continue
                if a > x:
                    nxt.append((x, min(a, y) if y is not None else a))
                if b is not None and b < y:
                    nxt.append((max(b, x), y))
                # b is None 且 a <= y：a 之后尾巴全部切掉
            cur = [c for c in nxt if c[1] is None or c[1] > c[0]]
        out.extend(cur)
    return sorted(out, key=lambda c: c[0])


# ---------------------------------------------------------------- 规范解析

def normalize_environment(spec):
    """解析 spec.environment，返回 (config, issues)。"""
    issues = []
    env = (spec or {}).get("environment")
    empty = {"defaults": {k: None for k in _LIMIT_KEYS},
             "materials": {}, "zones": {}, "active": False}
    if not env:
        return empty, issues
    if not isinstance(env, dict):
        return empty, [{"code": "ENV_SPEC_INVALID",
                        "message": "spec.environment 必须是对象"}]

    def parse_block(raw, where, allowed):
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            issues.append({"code": "ENV_SPEC_INVALID",
                           "message": f"environment.{where} 必须是对象"})
            return {}
        out = {}
        for k, val in raw.items():
            if k not in allowed:
                issues.append({"code": "ENV_SPEC_INVALID",
                               "message": f"environment.{where} 含不支持的限值键 {k}",
                               "detail": {"key": k}})
                continue
            n = _num(val)
            if n is None:
                issues.append({"code": "ENV_SPEC_INVALID",
                               "message": f"environment.{where}.{k} 必须是数值",
                               "detail": {"key": k, "value": val}})
                continue
            if k in _POSITIVE_KEYS and n <= 0:
                issues.append({"code": "ENV_SPEC_INVALID",
                               "message": f"environment.{where}.{k} 必须为正数",
                               "detail": {"key": k, "value": n}})
                continue
            out[k] = n
        return out

    defaults = parse_block(env.get("defaults") or {}, "defaults", _LIMIT_KEYS)
    materials, zones = {}, {}
    raw_m = env.get("materials")
    if raw_m is not None:
        if not isinstance(raw_m, dict):
            issues.append({"code": "ENV_SPEC_INVALID",
                           "message": "environment.materials 必须是对象"})
        else:
            for m, block in raw_m.items():
                materials[m] = parse_block(block, f"materials.{m}",
                                           _MATERIAL_KEYS)
    raw_z = env.get("zones")
    if raw_z is not None:
        if not isinstance(raw_z, dict):
            issues.append({"code": "ENV_SPEC_INVALID",
                           "message": "environment.zones 必须是对象"})
        else:
            for z, block in raw_z.items():
                zones[z] = parse_block(block, f"zones.{z}", _ZONE_KEYS)

    cfg = {"defaults": {k: defaults.get(k) for k in _LIMIT_KEYS},
           "materials": materials, "zones": zones,
           "active": bool(defaults)}
    if cfg["defaults"]["temp_min_c"] is not None \
            and cfg["defaults"]["temp_max_c"] is not None \
            and cfg["defaults"]["temp_min_c"] > cfg["defaults"]["temp_max_c"]:
        issues.append({"code": "ENV_SPEC_INVALID",
                       "message": "environment.defaults 温度下限高于上限"})
    if cfg["defaults"]["rh_min_pct"] is not None \
            and cfg["defaults"]["rh_max_pct"] is not None \
            and cfg["defaults"]["rh_min_pct"] > cfg["defaults"]["rh_max_pct"]:
        issues.append({"code": "ENV_SPEC_INVALID",
                       "message": "environment.defaults 湿度下限高于上限"})
    return cfg, issues


def _limits_for(config, material, zone):
    """defaults + 材料级 + 分区级依次覆盖。"""
    out = dict(config["defaults"])
    if material:
        out.update(config["materials"].get(material) or {})
    if zone:
        out.update(config["zones"].get(zone) or {})
    return out


# ---------------------------------------------------------------- 入链校验

def validate_event_item(item):
    """六类环境事件入链前最小载荷校验；返回错误消息字符串或 None。"""
    t = item["type"]
    if t not in ENV_EVENT_TYPES:
        return None
    at = item.get("at")
    if not at or parse_time(at) is None:
        return f"{t} 事件需要可解析的 at 时标（得到 {at!r}）"
    if t == "material_temp_reading":
        if _num(item.get("temp_c")) is None:
            return "material_temp_reading 需要数值 temp_c"
        if not item.get("unit") and not item.get("roll"):
            return "material_temp_reading 需要 unit 或 roll 引用材料单元"
    elif t == "environment_reading":
        if _num(item.get("temp_c")) is None:
            return "environment_reading 需要数值 temp_c"
        rh = _num(item.get("rh_pct"))
        if rh is None:
            return "environment_reading 需要数值 rh_pct"
        if rh <= 0 or rh > 100:
            return "environment_reading 的 rh_pct 必须在 (0,100] 区间"
    elif t == "package_opened":
        if not (item.get("unit") or item.get("roll") or item.get("material")):
            return "package_opened 需要 unit / roll / material 至少一项材料引用"
    for k in ("zones", "plies"):
        if k in item and item[k] is not None and (
                not isinstance(item[k], list)
                or not all(isinstance(x, str) for x in item[k])):
            return f"{t} 的 {k} 必须为字符串列表"
    if item.get("alternative") and not str(item.get("reason") or "").strip():
        return f"{t} 采用替代测点（alternative）必须写明 reason"
    if item.get("alternative") and not isinstance(
            item.get("amends_event"), int):
        return f"{t} 采用替代测点必须以整数 amends_event 指向被修订读数"
    return None


# ---------------------------------------------------------------- 主评估

def evaluate_environment(job, spec, zones, spec_by_id, materials,
                         stack, events, roll_ledger, genealogy, rolls):
    """按时标还原暴露区间并执行全部环境校核。返回 (state, violations)。"""
    violations = []

    def v(rule, message, units=None, plies=None, zids=None, **details):
        violations.append({
            "rule": rule, "message": message,
            "plies": [p for p in (plies or []) if p is not None],
            "zones": zids or list(details.get("zones") or []),
            "details": {**{k: x for k, x in details.items() if k != "zones"},
                        "units": [u for u in (units or []) if u]},
        })

    config, spec_issues = normalize_environment(spec)
    for iss in spec_issues:
        d = iss.get("detail") or {}
        v(iss["code"], iss["message"], zids=d.get("zones"),
          **{k: x for k, x in d.items() if k != "zones"})

    env_events = [e for e in events if e["type"] in ENV_EVENT_TYPES]
    if env_events and not config["active"]:
        v("ENV_SPEC_MISSING",
          "现场提交了开袋/测温/环境事件，但规范未冻结 environment 限值块，"
          "回温与暴露校核无法执行",
          units=sorted({_subject(e["payload"]) for e in env_events
                        if e["type"] in ("package_opened",
                                         "material_temp_reading")
                        and _subject(e["payload"])}),
          events=sorted(e["seq"] for e in env_events))

    active = [e for e in stack if e["active"]]

    # ---- 材料引用解析：roll/unit → 材料牌号 ----
    unit_material = {}
    for uid, u in ((genealogy or {}).get("state") or {}).get("units", {}).items():
        if u.get("material"):
            unit_material[uid] = u["material"]
        if u.get("born_at"):
            unit_material.setdefault(uid, u.get("material"))
    unit_born = {uid: parse_time(u.get("born_at"))
                 for uid, u in ((genealogy or {}).get("state") or {})
                 .get("units", {}).items() if u.get("born_at")}
    for rid, r in (rolls or {}).items():
        if r.get("material"):
            unit_material[rid] = r["material"]

    def material_of(p):
        subj = _subject(p)
        if subj and subj in unit_material and unit_material[subj]:
            return unit_material[subj]
        return p.get("material")

    def t_of(e):
        return parse_time(e["payload"].get("at"))

    # ---- 引用/时标有效性 ----
    for e in env_events:
        p, seq, t = e["payload"], e["seq"], t_of(e)
        if t is None:
            v("ENV_DATA_MISSING", f"{e['type']} 事件 #{seq} 缺少可解析时标",
              units=[_subject(p)], events=[seq])
        if e["type"] in ("package_opened", "material_temp_reading"):
            subj = _subject(p)
            mat = material_of(p)
            if subj and subj not in unit_material and mat is None:
                v("ENV_UNIT_UNKNOWN",
                   f"{e['type']} 事件 #{seq} 引用的材料单元 {subj} 未登记"
                   f"（料卷/谱系均无记录）",
                   units=[subj], events=[seq])
            if mat is not None and mat not in materials and config["active"]:
                v("ENV_MATERIAL_UNKNOWN",
                   f"{e['type']} 事件 #{seq} 的材料 {mat} 不在规范材料表中",
                   units=[subj], events=[seq], material=mat)
        if e["type"] in ("environment_reading", "layup_paused",
                         "surface_covered", "layup_resumed") \
                or e["type"] == "package_opened":
            for z in p.get("zones") or []:
                if z not in zones:
                    v("ZONE_UNKNOWN",
                       f"{e['type']} 事件 #{seq} 引用未定义分区 {z}",
                       units=[_subject(p)], zids=[z], events=[seq])

    # ---- 时标倒序（补录豁免）：正常事件不得早于序上最近的正常事件 ----
    last_regular = None
    for e in env_events:
        t, p = t_of(e), e["payload"]
        if t is None or p.get("backfilled"):
            continue
        if last_regular is not None and t < last_regular[0]:
            pe = last_regular[1]
            v("ENV_EVENT_ORDER",
              f"环境事件倒序：事件 #{e['seq']}（{p.get('at')}）早于事件 "
              f"#{pe['seq']}（{pe['payload'].get('at')}）；历史读数须以"
              f"补录（backfilled=true）追加",
              units=[_subject(p)], events=[pe["seq"], e["seq"]],
              at=p.get("at"), previous_at=pe["payload"].get("at"))
        last_regular = (t, e)

    # ---- 暂停/覆盖 → 恢复 配对窗 ----
    windows, pause_stack, cover_stack = [], [], []
    for e in env_events:
        t, p = t_of(e), e["payload"]
        if t is None:
            continue
        if e["type"] == "layup_paused":
            pause_stack.append(_open_window("pause", e, t))
        elif e["type"] == "surface_covered":
            cover_stack.append(_open_window("cover", e, t))
        elif e["type"] == "layup_resumed":
            _close_window(pause_stack, cover_stack, e, t, windows, v)
    for st, label in ((pause_stack, "暂停"), (cover_stack, "覆盖")):
        for w in st:
            v("ENV_COVERAGE_NOT_CLOSED",
              f"{label}事件 #{w['open_seq']} 之后没有配对的恢复事件，"
              f"暂停/覆盖关系不闭合",
              events=[w["open_seq"]],
              zids=sorted(w["zones"]) if w["zones"] else [],
              plies=sorted(w["plies"]) if w["plies"] else [])
    for w in windows:
        if w["zones"]:
            for z in w["zones"]:
                if z not in zones:
                    v("ZONE_UNKNOWN",
                      f"覆盖/暂停窗事件 #{w['open_seq']} 引用未定义分区 {z}",
                      zids=[z], events=[w["open_seq"]])

    bag_windows = _bag_windows(events)

    # ---- 读数序列与替代测点修订 ----
    raw_mt = [{"seq": e["seq"], "t": t_of(e), "p": e["payload"],
               "subject": _subject(e["payload"])}
              for e in env_events
              if e["type"] == "material_temp_reading" and t_of(e)]
    raw_amb = [{"seq": e["seq"], "t": t_of(e), "p": e["payload"],
                "zone": e["payload"].get("zone")}
               for e in env_events
               if e["type"] == "environment_reading" and t_of(e)]
    # 跨序列 amends（环境读数修订测温/测温修订环境读数）在入序列前拦截
    mt_seqs = {r["seq"] for r in raw_mt}
    amb_seqs = {r["seq"] for r in raw_amb}
    for r in raw_mt:
        if r["p"].get("alternative") and r["p"].get("amends_event") in amb_seqs:
            v("ENV_AMENDMENT_INVALID",
              f"材料温度读数事件 #{r['seq']} 的 amends_event="
              f"{r['p'].get('amends_event')} 指向环境读数，修订必须同序列",
              events=[r["seq"], r["p"]["amends_event"]],
              amends_event=r["p"]["amends_event"])
    for r in raw_amb:
        if r["p"].get("alternative") and r["p"].get("amends_event") in mt_seqs:
            v("ENV_AMENDMENT_INVALID",
              f"环境读数事件 #{r['seq']} 的 amends_event="
              f"{r['p'].get('amends_event')} 指向材料温度读数，修订必须同序列",
              events=[r["seq"], r["p"]["amends_event"]],
              amends_event=r["p"]["amends_event"])
    adopted_mt, dec_mt = _adopt_series(raw_mt, "材料温度", v)
    adopted_amb, dec_amb = _adopt_series(raw_amb, "环境", v)
    decisions = dec_mt + dec_amb

    # ---- 台账：回冻/解冻（roll 走事件链；unit 取出生时刻保守起算）----
    fridge_by_subject, thaw_last = {}, {}
    for rid, ledger in (roll_ledger or {}).items():
        fridge_by_subject[rid] = sorted(
            ts for ts, kind in ledger if kind == "fridge" and ts)
        thaws = sorted(ts for ts, kind in ledger if kind == "thaw" and ts)
        if thaws:
            thaw_last[rid] = thaws[-1]
    warmup_start = dict(thaw_last)
    for uid, born in unit_born.items():
        warmup_start.setdefault(uid, born)

    # ---- 铺放引用时刻（含返工替代层）----
    placements = {}
    for e in events:
        if e["type"] == "ply_placed":
            p = e["payload"]
            subj, t = _subject(p), parse_time(p.get("placed_at"))
            if subj and t:
                placements.setdefault(subj, []).append(
                    (t, p.get("ply_id"), e["seq"]))
        elif e["type"] == "ply_replaced":
            repl = e["payload"].get("replacement") or {}
            subj, t = _subject(repl), parse_time(repl.get("placed_at"))
            if subj and t:
                placements.setdefault(subj, []).append(
                    (t, repl.get("ply_id",
                                 e["payload"].get("removed_ply_id")),
                     e["seq"]))

    horizon = _horizon(events, placements)

    # ---- 材料单元开封段 ----
    opens = sorted(({"seq": e["seq"], "t": t_of(e),
                     "subject": _subject(e["payload"]),
                     "material": material_of(e["payload"]),
                     "payload": e["payload"]}
                    for e in env_events
                    if e["type"] == "package_opened" and t_of(e)),
                   key=lambda o: (o["t"], o["seq"]))
    material_segments = []
    for i, o in enumerate(opens):
        subj, t0 = o["subject"], o["t"]
        close_t, reason = None, None
        for ft in fridge_by_subject.get(subj, []):
            if ft >= t0:
                close_t, reason = ft, "refrigerated"
                break
        for pt, _pid, _sq in sorted(placements.get(subj, [])):
            if pt >= t0 and (close_t is None or pt < close_t):
                close_t, reason = pt, "placed"
                break
        end = close_t or horizon
        # 下一次开封前本段未闭合 → 重复开封
        nxt = opens[i + 1] if i + 1 < len(opens) else None
        if nxt and nxt["subject"] == subj and (close_t is None
                                               or close_t > nxt["t"]):
            v("ENV_OPEN_NOT_CLOSED",
              f"材料单元 {subj} 事件 #{nxt['seq']} 重复开封：上一次开封"
              f"（事件 #{o['seq']}）在该时刻前未经回冻或铺放闭合",
              units=[subj], events=[o["seq"], nxt["seq"]])
        material_segments.append({
            "subject": subj, "material": o["material"],
            "open_event": o["seq"], "opened_at": o["payload"].get("at"),
            "close_reason": reason,
            "close_t": close_t, "closed_at": _iso(close_t),
            "open_t": t0, "end_t": end, "closed": close_t is not None,
        })
        if close_t is None:
            v("ENV_OPEN_NOT_CLOSED",
              f"材料单元 {subj} 开封事件 #{o['seq']} 之后没有回冻或铺放"
              f"闭合，开封段按最新记录截断，开封关系不闭合",
              units=[subj], events=[o["seq"]],
              opened_at=o["payload"].get("at"))

        limits = _limits_for(config, o["material"], None)
        # 最低回温时长
        warm_min = limits.get("min_warmup_minutes")
        if warm_min is not None:
            start = warmup_start.get(subj)
            if start is None:
                v("ENV_WARMUP_DATA_MISSING",
                  f"材料单元 {subj} 开封（事件 #{o['seq']}）前没有解冻/出生"
                  f"记录，最低回温时长无法核计",
                  units=[subj], events=[o["seq"]],
                  required_minutes=warm_min)
            elif t0 < start:
                v("ENV_WARMUP_DATA_MISSING",
                  f"材料单元 {subj} 开封时刻 {o['payload'].get('at')} 早于"
                  f"最近解冻/出生 {start.isoformat()}，回温记录缺段",
                  units=[subj], events=[o["seq"]],
                  thaw_at=start.isoformat())
            else:
                warm_minutes = (t0 - start).total_seconds() / 60.0
                if warm_minutes + 1e-9 < warm_min:
                    v("ENV_WARMUP_SHORT",
                      f"材料单元 {subj} 开封前仅回温 {warm_minutes:.0f}min，"
                      f"低于最低回温时长 {warm_min:g}min（事件 #{o['seq']}）",
                      units=[subj], events=[o["seq"]],
                      warmup_min=round(warm_minutes, 1),
                      required_minutes=warm_min,
                      warmup_from=start.isoformat())

        mt = [r for r in adopted_mt if r["subject"] == subj]
        amb_global = [r for r in adopted_amb if r["zone"] is None]
        # 开袋前测温
        before = [r for r in mt if r["t"] <= t0]
        if not before:
            v("ENV_TEMP_BEFORE_OPEN_MISSING",
              f"材料单元 {subj} 开封（事件 #{o['seq']}）前没有任何材料温度"
              f"读数，无法核对开袋露点裕量",
              units=[subj], events=[o["seq"]])
        # 开袋瞬间露点裕量
        _check_open_dew(v, o, before, amb_global, limits)
        # 开封段采样/越限（全车间通道）
        _check_sample_gap(v, amb_global, [(t0, end)], limits,
                          units=[subj], plies=[], zids=[],
                          label=f"材料单元 {subj} 开封段",
                          ref_events=[o["seq"]])
        _check_excursions(v, amb_global, [(t0, end)], limits,
                          units=[subj], plies=[], zids=[])
        # 允许敞开时长（扣封袋窗；暂停/覆盖对材料本体不适用）
        open_segs = _subtract_windows([(t0, end)], bag_windows)
        minutes = sum((b - a).total_seconds() / 60.0 for a, b in open_segs)
        max_open = limits.get("max_open_minutes")
        if max_open is not None and minutes > max_open + 1e-9:
            v("ENV_OPEN_TIME_EXCEEDED",
              f"材料单元 {subj} 自开封（事件 #{o['seq']}）累计敞开 "
              f"{minutes:.0f}min，超过允许敞开时长 {max_open:g}min",
              units=[subj], events=[o["seq"]],
              open_minutes=round(minutes, 1), limit_minutes=max_open,
              closed=close_t is not None)

    # ---- 未封闭铺层表面：每层 × 规范分区 ----
    # 栈序（active 顺序）即层序；同区后铺层闭合该表面
    surface_parts = []
    for pos, e in enumerate(active):
        p = e["payload"]
        t0 = parse_time(p.get("placed_at"))
        if t0 is None:
            continue
        pid = e["ply_id"]
        sp = spec_by_id.get(pid) or {}
        subj = _subject(p)
        mat = (unit_material.get(subj) if subj else None) \
            or sp.get("material") or p.get("material")
        zs = sp.get("zones") or []
        later = active[pos + 1:]
        for zid in zs:
            if zid not in zones:
                continue
            close = None
            for e2 in later:
                z2 = (spec_by_id.get(e2["ply_id"]) or {}).get("zones") or []
                if zid in z2:
                    close = (parse_time(e2["payload"].get("placed_at")),
                             e2["event_seq"])
                    break
            raw_end = close[0] if close and close[0] else horizon
            parts = [(t0, raw_end)]
            cw = [(w["from_t"], w["to_t"]) for w in windows
                  if w["kind"] == "cover" and w["to_t"] is not None
                  and _window_hits(w, pid, zid)]
            bw = [(a, b) for a, b, wz in bag_windows
                  if wz is None or zid in wz]
            parts = _subtract_windows(_subtract_windows(parts, cw), bw)
            surface_parts.append({
                "ply": pid, "zone": zid, "material": mat, "subject": subj,
                "placed_event": e["event_seq"],
                "placed_at": p.get("placed_at"),
                "close_event": close[1] if close else None,
                "start_t": t0, "end_t": raw_end,
                "open_end": close is None, "segments": parts,
            })

    for part in surface_parts:
        pid, zid = part["ply"], part["zone"]
        limits = _limits_for(config, part["material"], zid)
        amb = _ambient_for(adopted_amb, zid)
        _check_sample_gap(v, amb, part["segments"], limits,
                          units=[part["subject"]], plies=[pid], zids=[zid],
                          label=f"铺层 {pid} 分区 {zid} 暴露段",
                          ref_events=[part["placed_event"]])
        _check_excursions(v, amb, part["segments"], limits,
                          units=[part["subject"]], plies=[pid], zids=[zid])
        mt = [r for r in adopted_mt
              if r["subject"] == part["subject"]] if part["subject"] else []
        _check_surface_dew(v, part, mt, amb, limits, material_segments)
        minutes = sum((b - a).total_seconds() / 60.0
                      for a, b in part["segments"])
        max_open = limits.get("max_open_minutes")
        if max_open is not None and minutes > max_open + 1e-9:
            v("ENV_OPEN_TIME_EXCEEDED",
              f"铺层 {pid} 在分区 {zid} 的表面累计敞开 {minutes:.0f}min"
              f"（已扣覆盖/封袋），超过允许敞开时长 {max_open:g}min",
              units=[part["subject"]], plies=[pid], zids=[zid],
              open_minutes=round(minutes, 1), limit_minutes=max_open,
              events=[part["placed_event"]])

    state = _build_state(config, env_events, raw_amb, raw_mt, adopted_amb,
                         adopted_mt, material_segments, surface_parts,
                         windows, bag_windows, decisions)
    violations.sort(key=lambda x: (x["rule"], x["plies"], x["zones"]))
    return state, violations


# ---------------------------------------------------------------- 配对窗

def _open_window(kind, e, t):
    p = e["payload"]
    return {"kind": kind, "from_t": t, "to_t": None,
            "open_seq": e["seq"], "close_seq": None,
            "open_at": p.get("at"), "close_at": None,
            "zones": set(p.get("zones")) if p.get("zones") else None,
            "plies": set(p.get("plies")) if p.get("plies") else None}


def _scope_match(w, wz, wp):
    """开/关窗作用域必须同为全局或同为相同的 zones/plies 集合。"""
    if (w["zones"] is None) != (wz is None):
        return False
    if wz is not None and w["zones"] != wz:
        return False
    if (w["plies"] is None) != (wp is None):
        return False
    if wp is not None and w["plies"] != wp:
        return False
    return True


def _close_window(pause_stack, cover_stack, e, t, windows, v):
    p = e["payload"]
    wz = set(p.get("zones")) if p.get("zones") else None
    wp = set(p.get("plies")) if p.get("plies") else None
    for stack in (pause_stack, cover_stack):
        for i in range(len(stack) - 1, -1, -1):
            if _scope_match(stack[i], wz, wp):
                w = stack[i]
                w["to_t"], w["close_seq"], w["close_at"] = \
                    t, e["seq"], p.get("at")
                windows.append(w)
                del stack[i]
                return
    v("ENV_COVERAGE_NOT_CLOSED",
      f"恢复事件 #{e['seq']} 没有可配对的暂停/覆盖事件，覆盖关系不闭合",
      events=[e["seq"]], zones=sorted(wz) if wz else [],
      plies=sorted(wp) if wp else [])


def _window_hits(w, pid, zid):
    if w["zones"] is not None and zid not in w["zones"]:
        return False
    if w["plies"] is not None and pid not in w["plies"]:
        return False
    return True


def _bag_windows(events):
    """压实封袋窗 [(seal_t, end_t|None, zones|None)]，未结束会话末端开放。"""
    out = []
    seal_t, seal_z = None, None
    for e in events:
        t = e["type"]
        if t == "bag_sealed":
            seal_t = parse_time(e["payload"].get("at"))
            z = e["payload"].get("zones")
            seal_z = set(z) if isinstance(z, list) else None
        elif t == "compaction_ended" and seal_t is not None:
            out.append((seal_t, parse_time(e["payload"].get("at")), seal_z))
            seal_t, seal_z = None, None
    if seal_t is not None:
        out.append((seal_t, None, seal_z))
    return out


# ---------------------------------------------------------------- 修订

def _adopt_series(raw, label, v):
    """按 seq 应用替代测点修订；返回 (采用读数, 决定列表)。"""
    by_seq = {r["seq"]: r for r in raw}
    superseded = {}
    decisions = []
    for r in sorted(raw, key=lambda x: x["seq"]):
        p = r["p"]
        if not p.get("alternative"):
            continue
        reason = str(p.get("reason") or "").strip()
        amends = p.get("amends_event")
        if not reason:
            v("ENV_AMENDMENT_REASON_MISSING",
              f"{label}读数事件 #{r['seq']} 标记为替代测点但未说明理由，"
              f"该读数不予采用",
              events=[r["seq"]],
              probe_id=p.get("alt_probe") or p.get("probe_id"))
            r["_rejected"] = "reason_missing"
            decisions.append({"seq": r["seq"], "kind": label,
                              "action": "rejected", "reason": "reason_missing"})
            continue
        if not isinstance(amends, int) or isinstance(amends, bool) \
                or amends not in by_seq:
            v("ENV_AMENDMENT_INVALID",
              f"{label}读数事件 #{r['seq']} 的 amends_event={amends!r} "
              f"未指向同序列既有读数事件，无法派生修订",
              events=[r["seq"]], amends_event=amends)
            r["_rejected"] = "amends_missing"
            decisions.append({"seq": r["seq"], "kind": label,
                              "action": "rejected", "reason": "amends_missing"})
            continue
        superseded[amends] = r["seq"]
        decisions.append({
            "seq": r["seq"], "kind": label, "action": "adopted",
            "amends_event": amends, "revision_id": f"AMEND-{r['seq']}",
            "reason": reason,
            "probe_id": p.get("alt_probe") or p.get("probe_id"),
            "backfilled": bool(p.get("backfilled")),
        })
    adopted = [{**r, "superseded_by": superseded.get(r["seq"])}
               for r in raw if r["seq"] not in superseded
               and not r.get("_rejected")]
    adopted.sort(key=lambda r: (r["t"], r["seq"]))
    return adopted, decisions


# ---------------------------------------------------------------- 采样/越限

def _ambient_for(adopted, zid):
    """分区可用环境读数：全车间通道 + 该分区通道；同时刻专用读数替换全局读数。"""
    pool = [r for r in adopted if r["zone"] is None or r["zone"] == zid]
    pool.sort(key=lambda r: (r["t"], 0 if r["zone"] is None else 1, r["seq"]))
    out = []
    for r in pool:
        if out and out[-1]["t"] == r["t"]:
            if r["zone"] == zid and out[-1]["zone"] is None:
                out[-1] = r  # 同时刻以分区专用读数为准
            continue  # 同通道同时刻保留先到的一条
        out.append(r)
    return out


def _check_sample_gap(v, readings, segments, limits, units, plies, zids,
                      label, ref_events):
    """暴露段内读数断档与边界夹逼：空窗与暴露段交集超过采样间隔即报。"""
    max_gap = limits.get("max_sample_interval_min")
    if max_gap is None:
        return
    rs = sorted(readings, key=lambda r: (r["t"], r["seq"]))
    for s, e in segments:
        pre = [r for r in rs if r["t"] <= s]
        post = [r for r in rs if r["t"] >= e]
        inside = [r for r in rs if s < r["t"] < e]
        if not pre and not post and not inside:
            gap_min = (e - s).total_seconds() / 60.0
            v("ENV_SAMPLE_GAP",
              f"{label}在暴露区间 {_iso(s)}~{_iso(e)} 内没有任何环境读数，"
              f"采样断档",
              units=units, plies=plies, zids=zids, events=list(ref_events),
              interval_min=round(gap_min, 1), limit_minutes=max_gap,
              window={"from": _iso(s), "to": _iso(e)})
            continue
        # 边界无夹逼读数时以段边界时刻作哨兵
        anchors = [pre[-1]] if pre else [{"t": s, "seq": None}]
        anchors.extend(inside)
        anchors.append(post[0] if post else {"t": e, "seq": None})
        for a, b in zip(anchors, anchors[1:]):
            ia = max(a["t"], s)
            ib = min(b["t"], e)
            gap_min = (ib - ia).total_seconds() / 60.0
            if gap_min > max_gap + 1e-9:
                evs = [x["seq"] for x in (a, b) if x.get("seq")]
                v("ENV_SAMPLE_GAP",
                  f"{label}环境读数断档：{_iso(a['t'])}~{_iso(b['t'])} 与暴露"
                  f"区间相交 {gap_min:.0f}min，超过最大采样间隔 "
                  f"{max_gap:g}min",
                  units=units, plies=plies, zids=zids,
                  events=evs or list(ref_events),
                  interval_min=round(gap_min, 1), limit_minutes=max_gap,
                  window={"from": _iso(a["t"]), "to": _iso(b["t"])})


def _check_excursions(v, readings, segments, limits, units, plies, zids):
    """温湿度越限：相邻同向越限读数合并为组，与暴露段相交时长为越限时长。"""
    if not segments:
        return
    rs = sorted(readings, key=lambda r: (r["t"], r["seq"]))
    _excursion_series(v, rs, segments, limits, units, plies, zids,
                      key="temp_c", name="温度", unit="℃",
                      lo_key="temp_min_c", hi_key="temp_max_c")
    _excursion_series(v, rs, segments, limits, units, plies, zids,
                      key="rh_pct", name="相对湿度", unit="%RH",
                      lo_key="rh_min_pct", hi_key="rh_max_pct")


def _excursion_series(v, rs, segments, limits, units, plies, zids,
                      key, name, unit, lo_key, hi_key):
    lo, hi = limits.get(lo_key), limits.get(hi_key)
    if lo is None and hi is None:
        return
    groups, cur = [], None
    for r in rs:
        val = _num(r["p"].get(key))
        if val is None:
            cur = None
            continue
        if lo is not None and val < lo:
            direction, bound = "low", lo
        elif hi is not None and val > hi:
            direction, bound = "high", hi
        else:
            cur = None
            continue
        if cur and cur["dir"] == direction:
            cur["readings"].append(r)
        else:
            cur = {"dir": direction, "bound": bound, "readings": [r]}
            groups.append(cur)
    for g in groups:
        rr = g["readings"]
        t0, t1 = rr[0]["t"], rr[-1]["t"]
        minutes = 0.0
        hit = False
        for s, e in segments:
            a, b = max(t0, s), min(t1, e)
            if b >= a and t0 <= e and t1 >= s:
                hit = True
                minutes += max(0.0, (b - a).total_seconds() / 60.0)
        # 单点越限读数落在暴露段内也要报（时长 0）
        if not hit and any(t0 == t1 and s <= t0 <= e for s, e in segments):
            hit = True
        if not hit:
            continue
        v("ENV_LIMIT_EXCEEDED",
          f"{name}越限（{'高于上限' if g['dir'] == 'high' else '低于下限'} "
          f"{g['bound']:g}{unit}），与暴露区间相交 {minutes:.0f}min",
          units=units, plies=plies, zids=zids,
          events=[r["seq"] for r in rr],
          metric=key, direction=g["dir"], limit=g["bound"],
          duration_min=round(minutes, 1),
          first_reading=rr[0]["p"].get(key),
          last_reading=rr[-1]["p"].get(key),
          window={"from": rr[0]["p"].get("at"), "to": rr[-1]["p"].get("at")})


# ---------------------------------------------------------------- 露点

def _ambient_at(readings, t):
    """时刻 t 的夹逼环境读数：优先不晚于 t 的最近一条，否则取 t 之后首条。"""
    pre = [r for r in readings if r["t"] <= t]
    if pre:
        return pre[-1]
    post = [r for r in readings if r["t"] >= t]
    return post[0] if post else None


def _dew_margin(material_temp, ambient_temp, rh):
    """材料温度 − 露点（℃）。"""
    dp = dew_point_c(ambient_temp, rh)
    if material_temp is None or dp is None:
        return None
    return material_temp - dp


def _check_open_dew(v, opener, before, amb, limits):
    """开袋瞬间露点裕量：开封前最近材料测温 × 开封时刻夹逼环境读数。"""
    req = limits.get("dew_point_margin_c")
    if req is None:
        return
    subj, t0 = opener["subject"], opener["t"]
    if not before:
        return
    mt = before[-1]
    mval = _num(mt["p"].get("temp_c"))
    env = _ambient_at(amb, t0)
    if env is None:
        v("ENV_SAMPLE_GAP",
          f"材料单元 {subj} 开封时刻（事件 #{opener['seq']}）没有可夹逼的"
          f"车间环境读数，露点裕量无法核计",
          units=[subj], events=[opener["seq"]])
        return
    t_c, rh = _num(env["p"].get("temp_c")), _num(env["p"].get("rh_pct"))
    margin = _dew_margin(mval, t_c, rh)
    if margin is not None and margin < req - 1e-9:
        v("ENV_DEW_MARGIN",
          f"材料单元 {subj} 开袋瞬间材料温度 {mval:g}℃，露点裕量仅 "
          f"{margin:.1f}℃（要求 ≥ {req:g}℃），有凝露风险",
          units=[subj], events=[opener["seq"], mt["seq"], env["seq"]],
          material_temp_c=mval, ambient_temp_c=t_c, rh_pct=rh,
          dew_point_margin_c=round(margin, 2), required_margin_c=req,
          at=opener["payload"].get("at"))


def _check_surface_dew(v, part, mt_series, amb, limits, material_segments):
    """暴露段内每次材料测温与当时夹逼环境读数核对露点裕量。

    测温须落在该单元开封之后的暴露子区间内（每个子区间取不晚于其末端、
    且不早于段起点的最近一次测温，避免用过冷/过热的历史读数误判）。
    """
    req = limits.get("dew_point_margin_c")
    if req is None:
        return
    open_subjects = [m for m in material_segments
                     if m["subject"] == part["subject"]]
    open_start = min((m["open_t"] for m in open_subjects), default=None)
    reported = set()
    for a, b in part["segments"]:
        candidates = [r for r in mt_series
                      if a <= r["t"] <= b
                      and (open_start is None or r["t"] >= open_start)]
        if not candidates:
            continue
        for r in candidates:
            mval = _num(r["p"].get("temp_c"))
            env = _ambient_at(amb, r["t"])
            if mval is None or env is None:
                continue
            t_c, rh = _num(env["p"].get("temp_c")), _num(env["p"].get("rh_pct"))
            margin = _dew_margin(mval, t_c, rh)
            if margin is None or margin >= req - 1e-9:
                continue
            key = r["seq"]
            if key in reported:
                continue
            reported.add(key)
            v("ENV_DEW_MARGIN",
              f"铺层 {part['ply']}（分区 {part['zone']}）暴露期间材料温度 "
              f"{mval:g}℃，露点裕量仅 {margin:.1f}℃（要求 ≥ {req:g}℃），"
              f"有凝露风险",
              units=[part["subject"]], plies=[part["ply"]],
              zids=[part["zone"]],
              events=[r["seq"], env["seq"], part["placed_event"]],
              material_temp_c=mval, ambient_temp_c=t_c, rh_pct=rh,
              dew_point_margin_c=round(margin, 2), required_margin_c=req,
              at=r["p"].get("at"))


# ---------------------------------------------------------------- 其他

def _subject(p):
    return p.get("unit") or p.get("roll")


def _horizon(events, placements):
    """核算视界：全部业务时标最大值。"""
    latest = None

    def consider(t):
        nonlocal latest
        if t and (latest is None or t > latest):
            latest = t

    for e in events:
        p = e["payload"]
        consider(parse_time(p.get("at")))
        consider(parse_time(p.get("placed_at")))
        repl = p.get("replacement")
        if isinstance(repl, dict):
            consider(parse_time(repl.get("placed_at")))
    for refs in placements.values():
        for t, _pid, _seq in refs:
            consider(t)
    return latest


# ---------------------------------------------------------------- 状态输出

def _build_state(config, env_events, raw_amb, raw_mt, adopted_amb,
                 adopted_mt, material_segments, surface_parts, windows,
                 bag_windows, decisions):
    # 从修订决定反填被替代读数的 superseded_by（旧读数保留留痕）
    replaced = {d["amends_event"]: d["seq"] for d in decisions
                if d.get("action") == "adopted"}
    rejected = {d["seq"] for d in decisions if d.get("action") == "rejected"}
    for r in raw_amb + raw_mt:
        if r["seq"] in replaced:
            r["superseded_by"] = replaced[r["seq"]]
        if r["seq"] in rejected:
            r["_rejected"] = r.get("_rejected") or "rejected"
    amb_out = [{
        "event_seq": r["seq"], "at": r["p"].get("at"), "zone": r["zone"],
        "temp_c": _num(r["p"].get("temp_c")),
        "rh_pct": _num(r["p"].get("rh_pct")),
        "probe_id": r["p"].get("probe_id"),
        "alternative": bool(r["p"].get("alternative")),
        "backfilled": bool(r["p"].get("backfilled")),
        "adopted": not r.get("_rejected"),
        "rejected_reason": r.get("_rejected"),
        "supersedes": r["p"].get("amends_event")
        if r["p"].get("alternative") else None,
        "superseded_by": r.get("superseded_by"),
    } for r in sorted(raw_amb, key=lambda r: (r["t"], r["seq"]))]
    mt_out = [{
        "event_seq": r["seq"], "at": r["p"].get("at"), "unit": r["subject"],
        "temp_c": _num(r["p"].get("temp_c")),
        "probe_id": r["p"].get("probe_id"),
        "alternative": bool(r["p"].get("alternative")),
        "backfilled": bool(r["p"].get("backfilled")),
        "adopted": not r.get("_rejected"),
        "rejected_reason": r.get("_rejected"),
        "supersedes": r["p"].get("amends_event")
        if r["p"].get("alternative") else None,
        "superseded_by": r.get("superseded_by"),
    } for r in sorted(raw_mt, key=lambda r: (r["t"], r["seq"]))]

    mat_out = [{
        "unit": m["subject"], "material": m["material"],
        "open_event": m["open_event"], "opened_at": m["opened_at"],
        "closed_at": m["closed_at"], "close_reason": m["close_reason"],
        "closed": m["closed"],
        "interval": {"from": m["opened_at"],
                     "to": m["closed_at"] or _iso(m["end_t"])},
    } for m in material_segments]

    surf_out = []
    for p in surface_parts:
        ivs = [{"from": _iso(a), "to": _iso(b)} for a, b in p["segments"]]
        if p["open_end"] and ivs:
            ivs[-1]["to"] = None
            ivs[-1]["open_ended"] = True
        surf_out.append({
            "ply_id": p["ply"], "zone": p["zone"], "material": p["material"],
            "unit": p["subject"], "placed_event": p["placed_event"],
            "placed_at": p["placed_at"], "close_event": p["close_event"],
            "exposure_intervals": ivs,
        })

    return {
        "enabled": config["active"],
        "limits": {"defaults": config["defaults"],
                   "materials": config["materials"],
                   "zones": config["zones"]},
        "events": [{"seq": e["seq"], "type": e["type"],
                    "at": e["payload"].get("at"),
                    "backfilled": bool(e["payload"].get("backfilled")),
                    "operator": e.get("operator")}
                   for e in sorted(env_events, key=lambda x: x["seq"])],
        "ambient_series": amb_out,
        "material_temp_series": mt_out,
        "material_intervals": mat_out,
        "surface_intervals": surf_out,
        "cover_windows": [{
            "kind": w["kind"], "open_event": w["open_seq"],
            "close_event": w["close_seq"], "from": w["open_at"],
            "to": w["close_at"], "closed": w["to_t"] is not None,
            "zones": sorted(w["zones"]) if w["zones"] is not None else None,
            "plies": sorted(w["plies"]) if w["plies"] is not None else None,
        } for w in windows],
        "bag_windows": [{"from": _iso(a), "to": _iso(b),
                         "open_ended": b is None} for a, b, _z in bag_windows],
        "decisions": decisions,
    }


def empty_state():
    """无环境规范时的空状态（老工单快照结构一致）。"""
    return {"enabled": False,
            "limits": {"defaults": {k: None for k in _LIMIT_KEYS},
                       "materials": {}, "zones": {}},
            "events": [], "ambient_series": [], "material_temp_series": [],
            "material_intervals": [], "surface_intervals": [],
            "cover_windows": [], "bag_windows": [], "decisions": []}
