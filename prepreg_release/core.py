"""铺层状态重建、放行规则校验、批准快照与版本比较。

事件模型（追加式）：
  ply_placed        铺放一层 {ply_id, roll, angle, face, geometry, seams, placed_at}
  ply_removed       揭除一层 {ply_id, reason}
  ply_replaced      返工：串起揭除层与替代层 {removed_ply_id, replacement:{...ply_placed}}
  roll_thawed       料卷解冻 {roll, at}
  roll_refrigerated 料卷回冻 {roll, at}
  note              过程备注 {text}
"""

import hashlib
import json
from datetime import datetime, timezone

from .geometry import coverage_fraction, point_in_polygon, polygon_centroid

EVENT_TYPES = {
    "ply_placed", "ply_removed", "ply_replaced",
    "roll_thawed", "roll_refrigerated", "note",
}

DEFAULT_RULES = {
    "angle_tolerance_deg": 3.0,        # 纤维方向允差
    "max_consecutive_same_angle": 4,   # 连续同向最大层数
    "seam_min_stagger_mm": 25.0,       # 相邻层接缝最小错开
    "seam_max_gap_mm": 1.5,            # 接缝对接最大间隙（负值即重叠）
    "drop_min_stagger_mm": 12.0,       # 丢层错开最小距离
    "require_symmetry": True,          # 中面对称要求
    "require_balance": True,           # ±θ 平衡要求
    "coverage_min_fraction": 0.98,     # 分区覆盖率下限
    "out_time_limit_h": None,          # 外置时间默认上限（料卷级优先）
}


# ---------------------------------------------------------------- 时间工具

def parse_time(s):
    """解析 ISO-8601 时间；naive 视为 UTC。非法返回 None。"""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def angle_diff(a, b):
    """纤维方向差（0~90°，方向按模 180° 处理）。"""
    d = abs((a - b) % 180.0)
    return min(d, 180.0 - d)


def _norm_angle(a):
    """归一到 [-90, 90) 用于平衡核算。"""
    return (a + 90.0) % 180.0 - 90.0


# ---------------------------------------------------------------- 事件重放

def replay(events):
    """按 seq 重放事件链，重建铺层栈与料卷外置时间台账。

    返回 (stack, roll_ledger, anomalies)：
      stack        铺层条目列表（含已揭除），按模具上的层序排列
      roll_ledger  {roll_id: [(datetime, 'thaw'|'fridge'), ...]}
      anomalies    重放期发现的返工链断裂等违规
    """
    stack = []
    roll_ledger = {}
    anomalies = []

    def _violation(rule, message, plies=None, **details):
        anomalies.append({
            "rule": rule, "message": message,
            "plies": plies or [], "zones": [], "details": details,
        })

    for ev in events:
        etype, p = ev["type"], ev["payload"]
        if etype == "ply_placed":
            stack.append({
                "event_seq": ev["seq"], "ply_id": p.get("ply_id"),
                "active": True, "payload": p, "operator": ev.get("operator"),
                "removed_by": None, "replaced_by": None, "rework_of": None,
            })
        elif etype == "ply_removed":
            target = _last_active(stack, p.get("ply_id"))
            if target is None:
                _violation("REWORK_TARGET_NOT_FOUND",
                           f"揭除记录指向不存在或已揭除的铺层 {p.get('ply_id')}",
                           plies=[p.get("ply_id")], event_seq=ev["seq"])
            else:
                target["active"] = False
                target["removed_by"] = ev["seq"]
        elif etype == "ply_replaced":
            rid = p.get("removed_ply_id")
            repl = p.get("replacement") or {}
            target = None
            for e in stack:
                if e["ply_id"] == rid and not e["active"] \
                        and e["removed_by"] is not None and e["replaced_by"] is None:
                    target = e
            if target is None:
                _violation("REWORK_LINK_BROKEN",
                           f"返工记录引用的揭除层 {rid} 不存在或已被替代",
                           plies=[rid], event_seq=ev["seq"])
            else:
                target["replaced_by"] = ev["seq"]
                entry = {
                    "event_seq": ev["seq"], "ply_id": repl.get("ply_id", rid),
                    "active": True, "payload": repl, "operator": ev.get("operator"),
                    "removed_by": None, "replaced_by": None, "rework_of": rid,
                }
                stack.insert(stack.index(target) + 1, entry)  # 替代层回到原层位
        elif etype in ("roll_thawed", "roll_refrigerated"):
            ts = parse_time(p.get("at")) or parse_time(ev.get("recorded_at"))
            kind = "thaw" if etype == "roll_thawed" else "fridge"
            roll_ledger.setdefault(p.get("roll"), []).append((ts, kind))
    return stack, roll_ledger, anomalies


def _last_active(stack, ply_id):
    for e in reversed(stack):
        if e["ply_id"] == ply_id and e["active"]:
            return e
    return None


def first_thaw(ledger):
    thaws = [ts for ts, kind in ledger if kind == "thaw" and ts]
    return min(thaws) if thaws else None


def out_time_hours(ledger, t):
    """截至时刻 t 的累计外置（解冻未回冻）小时数。"""
    total = 0.0
    start = None
    for ts, kind in sorted(ledger, key=lambda x: (x[0] or datetime.min.replace(tzinfo=timezone.utc))):
        if ts is None:
            continue
        if kind == "thaw" and start is None:
            start = ts
        elif kind == "fridge" and start is not None:
            end = min(ts, t)
            if end > start:
                total += (end - start).total_seconds() / 3600.0
            start = None
    if start is not None and t > start:
        total += (t - start).total_seconds() / 3600.0
    return total


# ---------------------------------------------------------------- 主分析

def analyze(job, rolls, events):
    """重建状态并执行全部放行规则。返回 (state, violations)。"""
    spec = job["spec"] or {}
    rules = dict(DEFAULT_RULES)
    rules.update(spec.get("rules") or {})
    zones = {z["zone_id"]: z for z in (job["zones"] or [])}
    materials = spec.get("materials") or {}
    spec_plies = spec.get("plies") or []

    V = []

    def v(rule, message, plies=None, zids=None, **details):
        V.append({"rule": rule, "message": message,
                  "plies": [p for p in (plies or []) if p is not None],
                  "zones": zids or [], "details": details})

    # ---- 规范自身完整性 ----
    spec_by_id = {}
    for sp in spec_plies:
        miss = [k for k in ("ply_id", "seq", "material", "angle", "zones") if k not in sp]
        if miss:
            v("SPEC_DATA_MISSING", f"铺层规范缺少字段 {miss}", plies=[sp.get("ply_id")],
              missing=miss)
            continue
        pid = sp["ply_id"]
        if pid in spec_by_id:
            v("SPEC_DUPLICATE_PLY", f"规范中铺层号 {pid} 重复", plies=[pid])
        spec_by_id[pid] = sp
        if sp["material"] not in materials:
            v("SPEC_DATA_MISSING", f"铺层 {pid} 的材料 {sp['material']} 未在材料表中定义",
              plies=[pid], material=sp["material"])
        for zid in sp.get("zones") or []:
            if zid not in zones:
                v("ZONE_UNKNOWN", f"铺层 {pid} 引用了未定义分区 {zid}",
                  plies=[pid], zids=[zid])

    # ---- 重放事件链 ----
    stack, ledger, anomalies = replay(events)
    V.extend(anomalies)
    active = [e for e in stack if e["active"]]

    # 揭除未串替代层（规范内铺层被揭除后必须返工闭环）
    for e in stack:
        if not e["active"] and e["removed_by"] and e["replaced_by"] is None \
                and e["ply_id"] in spec_by_id:
            v("REMOVAL_WITHOUT_REPLACEMENT",
              f"铺层 {e['ply_id']} 已揭除（事件 #{e['removed_by']}）但无关联替代层",
              plies=[e["ply_id"]])

    # ---- 记录完整性 / 料卷 / 外置时间 ----
    def thickness_of(entry):
        sp = spec_by_id.get(entry["ply_id"]) or {}
        mat = sp.get("material") or entry["payload"].get("material")
        info = materials.get(mat) or {}
        return info.get("ply_thickness"), mat

    for e in active:
        p, pid = e["payload"], e["ply_id"]
        miss = [k for k in ("geometry", "angle", "roll", "placed_at", "face")
                if p.get(k) is None]
        if not e.get("operator"):
            miss.append("operator")
        if miss:
            v("DATA_MISSING", f"铺层 {pid} 的铺放记录缺少字段 {miss}",
              plies=[pid], missing=miss)
        t, _mat = thickness_of(e)
        if t is None:
            v("SPEC_DATA_MISSING", f"铺层 {pid} 缺少单层厚度定义", plies=[pid])

        roll_id = p.get("roll")
        roll = rolls.get(roll_id)
        if roll_id is not None:
            if roll is None:
                v("ROLL_UNKNOWN", f"铺层 {pid} 使用未登记的料卷 {roll_id}",
                  plies=[pid], roll=roll_id)
            else:
                sp = spec_by_id.get(pid)
                if sp and roll["material"] != sp.get("material"):
                    v("MATERIAL_MISMATCH",
                      f"铺层 {pid} 规范材料 {sp.get('material')} 与料卷 {roll_id} "
                      f"材料 {roll['material']} 不符",
                      plies=[pid], roll=roll_id, batch_no=roll["batch_no"])
                placed = parse_time(p.get("placed_at"))
                lg = ledger.get(roll_id, [])
                if placed is not None:
                    ft = first_thaw(lg)
                    if ft is None or placed < ft:
                        v("OUT_TIME_DATA_MISSING",
                          f"铺层 {pid} 铺放时刻早于料卷 {roll_id} 的首次解冻记录"
                          f"（或解冻记录缺失）",
                          plies=[pid], roll=roll_id)
                    else:
                        hours = out_time_hours(lg, placed)
                        limit = roll["out_time_limit_h"] or rules.get("out_time_limit_h")
                        if limit is not None and hours > limit:
                            v("OUT_TIME_EXCEEDED",
                              f"铺层 {pid} 铺放时料卷 {roll_id}（批号 "
                              f"{roll['batch_no']}）累计外置 {hours:.1f}h，"
                              f"超过上限 {limit}h",
                              plies=[pid], roll=roll_id, batch_no=roll["batch_no"],
                              out_time_h=round(hours, 2), limit_h=limit)

    # ---- 缺层 / 重复层 / 非规范层 / 层序 ----
    by_id = {}
    for e in active:
        by_id.setdefault(e["ply_id"], []).append(e)
    for sp in spec_plies:
        pid = sp.get("ply_id")
        n = len(by_id.get(pid, []))
        if pid and n == 0:
            v("MISSING_PLY", f"规范铺层 {pid}（{sp.get('angle')}°）无有效铺放记录",
              plies=[pid], zids=sp.get("zones") or [])
        elif n > 1:
            v("DUPLICATE_PLY", f"铺层 {pid} 存在 {n} 张有效铺放（重复层）",
              plies=[pid], count=n)
    for e in active:
        if e["ply_id"] not in spec_by_id:
            v("UNEXPECTED_PLY", f"铺层 {e['ply_id']} 不在铺层规范中",
              plies=[e["ply_id"]])

    rank = {sp["ply_id"]: sp["seq"] for sp in spec_plies if "ply_id" in sp and "seq" in sp}
    ranks = [rank[e["ply_id"]] for e in active if e["ply_id"] in rank]
    ids = [e["ply_id"] for e in active if e["ply_id"] in rank]
    for i in range(len(ranks) - 1):
        if ranks[i] > ranks[i + 1]:
            v("ORDER_MISMATCH",
              f"铺层次序错误：{ids[i]}（规范序 {ranks[i]}）排在 "
              f"{ids[i + 1]}（规范序 {ranks[i + 1]}）之前",
              plies=[ids[i], ids[i + 1]])
            break

    # ---- 方向 / 正反面 / 分区覆盖 ----
    tol = rules["angle_tolerance_deg"]
    for e in active:
        p, pid = e["payload"], e["ply_id"]
        sp = spec_by_id.get(pid)
        if not sp:
            continue
        if p.get("angle") is not None and "angle" in sp:
            d = angle_diff(float(p["angle"]), float(sp["angle"]))
            if d > tol:
                v("ANGLE_MISMATCH",
                  f"铺层 {pid} 实际方向 {p['angle']}° 与规范 {sp['angle']}° "
                  f"偏差 {d:.1f}°，超过允差 {tol}°",
                  plies=[pid], actual_deg=p["angle"], spec_deg=sp["angle"])
        if sp.get("face") and p.get("face") and p["face"] != sp["face"]:
            v("FACE_MISMATCH",
              f"铺层 {pid} 正反面错误：实际 {p['face']}，规范要求 {sp['face']}",
              plies=[pid])
        geom = p.get("geometry")
        if geom:
            for zid in sp.get("zones") or []:
                z = zones.get(zid)
                if not z:
                    continue
                frac = coverage_fraction(z["polygon"], geom)
                if frac < rules["coverage_min_fraction"]:
                    v("MISSING_COVERAGE",
                      f"铺层 {pid} 对分区 {zid} 覆盖率仅 {frac:.0%}，"
                      f"低于 {rules['coverage_min_fraction']:.0%}",
                      plies=[pid], zids=[zid], coverage=round(frac, 3))

    # ---- 接缝：重叠 / 超隙 / 相邻层错开不足 ----
    max_gap = rules["seam_max_gap_mm"]
    min_stagger = rules["seam_min_stagger_mm"]
    for e in active:
        for s in e["payload"].get("seams") or []:
            if s.get("gap") is None or s.get("zone") is None:
                v("DATA_MISSING", f"铺层 {e['ply_id']} 接缝记录缺少 zone/gap",
                  plies=[e["ply_id"]], zids=[s.get("zone")])
                continue
            if s["gap"] < 0:
                v("SEAM_OVERLAP",
                  f"铺层 {e['ply_id']} 在分区 {s['zone']} 的接缝重叠 {-s['gap']}mm",
                  plies=[e["ply_id"]], zids=[s["zone"]], gap_mm=s["gap"])
            elif s["gap"] > max_gap:
                v("SEAM_GAP_EXCEEDED",
                  f"铺层 {e['ply_id']} 在分区 {s['zone']} 的接缝间隙 {s['gap']}mm "
                  f"超过上限 {max_gap}mm",
                  plies=[e["ply_id"]], zids=[s["zone"]], gap_mm=s["gap"])
    for i in range(len(active) - 1):
        a, b = active[i], active[i + 1]
        for sa in a["payload"].get("seams") or []:
            for sb in b["payload"].get("seams") or []:
                if sa.get("zone") == sb.get("zone") and sa.get("zone") \
                        and sa.get("axis", "x") == sb.get("axis", "x") \
                        and sa.get("at") is not None and sb.get("at") is not None:
                    d = abs(sa["at"] - sb["at"])
                    if d < min_stagger:
                        v("SEAM_STAGGER",
                          f"相邻铺层 {a['ply_id']} 与 {b['ply_id']} 在分区 "
                          f"{sa['zone']} 的接缝仅错开 {d}mm，小于 {min_stagger}mm",
                          plies=[a["ply_id"], b["ply_id"]], zids=[sa["zone"]],
                          stagger_mm=d)

    # ---- 连续同向 ----
    max_run = rules["max_consecutive_same_angle"]
    run = []
    for e in active + [None]:
        ang = e["payload"].get("angle") if e else None
        if e is not None and ang is not None and run \
                and angle_diff(float(ang), float(run[-1]["payload"]["angle"])) <= tol:
            run.append(e)
        else:
            if len(run) > max_run:
                v("CONSECUTIVE_ANGLE",
                  f"连续 {len(run)} 层同向（{run[0]['payload']['angle']}°），"
                  f"超过上限 {max_run}",
                  plies=[x["ply_id"] for x in run])
            run = [e] if e is not None and ang is not None else []

    # ---- 对称 / 平衡（按实际铺放角度核算）----
    angles = [(e["ply_id"], float(e["payload"]["angle"])) for e in active
              if e["payload"].get("angle") is not None]
    if rules["require_symmetry"] and angles:
        n = len(angles)
        for i in range(n // 2):
            (pid_a, aa), (pid_b, ab) = angles[i], angles[n - 1 - i]
            if angle_diff(aa, ab) > tol:
                v("SYMMETRY_VIOLATION",
                  f"中面对称破坏：第 {i + 1} 层 {pid_a}（{aa}°）与镜像层 "
                  f"{pid_b}（{ab}°）不对称",
                  plies=[pid_a, pid_b])
                break
    if rules["require_balance"] and angles:
        counts = {}
        for _pid, a in angles:
            na = round(_norm_angle(a), 3)
            counts[na] = counts.get(na, 0) + 1
        for na, c in sorted(counts.items()):
            if na in (0.0, -90.0):
                continue
            if counts.get(round(-na, 3), 0) != c:
                v("BALANCE_VIOLATION",
                  f"平衡破坏：{na}° 方向 {c} 层，{-na}° 方向 "
                  f"{counts.get(round(-na, 3), 0)} 层",
                  plies=[pid for pid, a in angles
                         if round(_norm_angle(a), 3) == na])
                break

    # ---- 丢层错开 ----
    min_drop = rules["drop_min_stagger_mm"]
    drops = sorted((sp for sp in spec_plies if sp.get("drop_at") is not None),
                   key=lambda s: s.get("seq", 0))
    for s1, s2 in zip(drops, drops[1:]):
        d = abs(s2["drop_at"] - s1["drop_at"])
        if d < min_drop:
            v("DROP_STAGGER",
              f"丢层 {s1['ply_id']}（@{s1['drop_at']}mm）与 {s2['ply_id']}"
              f"（@{s2['drop_at']}mm）错开仅 {d}mm，小于 {min_drop}mm，"
              f"将造成厚度突变",
              plies=[s1["ply_id"], s2["ply_id"]], stagger_mm=d)

    state = _build_state(job, zones, materials, spec_plies, spec_by_id,
                         rolls, stack, active, ledger, events, thickness_of)
    V.sort(key=lambda x: (x["rule"], x["plies"]))
    return state, V


def _build_state(job, zones, materials, spec_plies, spec_by_id, rolls,
                 stack, active, ledger, events, thickness_of):
    """按层序重建的分区覆盖与厚度，以及料卷外置时间台账。"""
    plies_out = []
    for pos, e in enumerate(active, 1):
        p = e["payload"]
        roll = rolls.get(p.get("roll")) or {}
        t, mat = thickness_of(e)
        plies_out.append({
            "pos": pos, "ply_id": e["ply_id"], "angle": p.get("angle"),
            "face": p.get("face"), "material": mat,
            "roll": p.get("roll"), "batch_no": roll.get("batch_no"),
            "operator": e.get("operator"), "placed_at": p.get("placed_at"),
            "rework_of": e["rework_of"], "event_seq": e["event_seq"],
        })
    removed_out = [{
        "ply_id": e["ply_id"], "removed_by_event": e["removed_by"],
        "replaced_by_event": e["replaced_by"],
    } for e in stack if not e["active"]]

    zone_state = {}
    for zid, z in zones.items():
        cx, cy = polygon_centroid(z["polygon"])
        t_actual = 0.0
        n_cover = 0
        for e in active:
            geom = e["payload"].get("geometry")
            t, _ = thickness_of(e)
            if geom and t and point_in_polygon(cx, cy, geom):
                t_actual += t
                n_cover += 1
        t_spec = 0.0
        for sp in spec_plies:
            if zid in (sp.get("zones") or []):
                info = materials.get(sp.get("material")) or {}
                t_spec += info.get("ply_thickness") or 0.0
        zone_state[zid] = {
            "thickness_mm": round(t_actual, 4),
            "spec_thickness_mm": round(t_spec, 4),
            "delta_mm": round(t_actual - t_spec, 4),
            "ply_count": n_cover,
        }

    steps = []
    seen = set()
    for z in zones.values():
        for nb in z.get("adjacent") or []:
            key = tuple(sorted((z["zone_id"], nb)))
            if nb in zone_state and key not in seen:
                seen.add(key)
                steps.append({
                    "zones": list(key),
                    "delta_mm": round(abs(zone_state[key[0]]["thickness_mm"]
                                          - zone_state[key[1]]["thickness_mm"]), 4),
                })

    last_ts = None
    for ev in events:
        ts = parse_time(ev.get("recorded_at"))
        if ts and (last_ts is None or ts > last_ts):
            last_ts = ts
    roll_state = {}
    for rid, r in rolls.items():
        lg = ledger.get(rid, [])
        out_h = out_time_hours(lg, last_ts) if last_ts else 0.0
        limit = r["out_time_limit_h"]
        roll_state[rid] = {
            "batch_no": r["batch_no"], "material": r["material"],
            "out_time_h": round(out_h, 2), "limit_h": limit,
            "remaining_h": round(limit - out_h, 2) if limit is not None else None,
        }

    return {
        "job_id": job["id"], "status": job["status"],
        "ply_count": len(active), "plies": plies_out, "removed": removed_out,
        "zones": zone_state, "thickness_steps": steps, "rolls": roll_state,
        "event_count": len(events),
    }


# ---------------------------------------------------------------- 快照与比较

def chain_hash(events):
    """事件链防篡改哈希：逐条串联 seq/type/规范化 payload。"""
    h = hashlib.sha256()
    for ev in events:
        h.update(str(ev["seq"]).encode())
        h.update(b"|")
        h.update(ev["type"].encode())
        h.update(b"|")
        h.update(json.dumps(ev["payload"], sort_keys=True,
                            ensure_ascii=False).encode())
        h.update(b"|")
        h.update((ev.get("operator") or "").encode())
        h.update(b"\n")
    return h.hexdigest()


def build_snapshot(job, rolls, events, state):
    spec_json = json.dumps(job["spec"], sort_keys=True, ensure_ascii=False)
    batches = sorted(
        ({"roll_id": r["roll_id"], "batch_no": r["batch_no"],
          "material": r["material"]} for r in rolls.values()),
        key=lambda x: x["roll_id"])
    return {
        "package": "prepreg_layup_release",
        "job_id": job["id"], "job_name": job["name"],
        "frozen_at": utcnow(),
        "tool_datum": job["tool_datum"],
        "zones": job["zones"],
        "spec": job["spec"],
        "spec_hash": hashlib.sha256(spec_json.encode()).hexdigest(),
        "material_batches": batches,
        "batch_hash": hashlib.sha256(
            json.dumps(batches, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "events": events,
        "chain_hash": chain_hash(events),
        "state": state,
    }


def diff_snapshots(sa, sb):
    """比较两个批准版快照：规范、批次、事件链、分区厚度。"""
    def _batch_map(s):
        return {b["roll_id"]: b["batch_no"] for b in s["material_batches"]}

    ba, bb = _batch_map(sa), _batch_map(sb)
    seqs_a = {e["seq"] for e in sa["events"]}
    seqs_b = {e["seq"] for e in sb["events"]}
    zones_a = sa["state"]["zones"]
    zones_b = sb["state"]["zones"]
    thickness_delta = {}
    for zid in sorted(set(zones_a) | set(zones_b)):
        ta = (zones_a.get(zid) or {}).get("thickness_mm")
        tb = (zones_b.get(zid) or {}).get("thickness_mm")
        if ta != tb:
            thickness_delta[zid] = {"from": ta, "to": tb}
    return {
        "spec_changed": sa["spec_hash"] != sb["spec_hash"],
        "spec_hash": {"a": sa["spec_hash"], "b": sb["spec_hash"]},
        "batches": {
            "added": sorted(set(bb) - set(ba)),
            "removed": sorted(set(ba) - set(bb)),
            "changed": sorted(r for r in set(ba) & set(bb) if ba[r] != bb[r]),
        },
        "events": {
            "count_a": len(sa["events"]), "count_b": len(sb["events"]),
            "added_seqs": sorted(seqs_b - seqs_a),
            "chain_hash_a": sa["chain_hash"], "chain_hash_b": sb["chain_hash"],
        },
        "thickness_delta_mm": thickness_delta,
        "ply_count": {"a": sa["state"]["ply_count"], "b": sb["state"]["ply_count"]},
    }
