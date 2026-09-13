"""阶段压实与真空袋检漏：事件重放、检查点绑定、达压保持与隔离回升率核算。

规范（spec.compaction，缺省即不启用，保持老工单兼容）：
  {
    "defaults": {"target_abs_kpa": 12.0, "hold_seconds": 300,
                 "max_sample_interval_s": 60, "max_rise_kpa_min": 2.0,
                 "leak_test_seconds": 180},
    "checkpoints": [
      {"checkpoint_id": "CP1", "after_seq": 3, "zones": ["Z1"],
       "target_abs_kpa": 12.0, "hold_seconds": 300,
       "max_sample_interval_s": 60, "max_rise_kpa_min": 2.0,
       "leak_test_seconds": 180}
    ]
  }
压力均为绝对压力（kPa），时长为秒，回升率为 kPa/min。
检查点可按层序（after_seq / after_ply）触发，并用 zones 限定分区（默认全部分区）。

现场追加事件（与铺放事件同一条只增事件链）：
  bag_sealed       封袋 {at, zones?}
  vacuum_started   抽真空 {at}
  vacuum_reading   带时标压力读数 {at, pressure_kpa}
  pump_isolated    隔离泵（开始检漏）{at}
  compaction_ended 结束 {at}

服务按事件次序把检查点绑定到封袋时已生效的铺层与袋下分区：
  * 揭除/替换被压实层只使受影响及后续检查点失效，重新压实须再追加一组事件，
    因此总是取“最后一次在该检查点应压实层全部归位之后封袋”的压实会话来核算；
  * 连续达压时长 = 抽真空阶段相邻读数（间隔不超采样上限）压力均 ≤ 目标值
    的最长连续区间；隔离后的首末读数构成检漏区间，回升率 = Δ压力 / 分钟。
"""

from .core import parse_time

STAGE_EVENTS = {
    "bag_sealed", "vacuum_started", "vacuum_reading",
    "pump_isolated", "compaction_ended",
}

_BUILTIN_DEFAULTS = {"leak_test_seconds": 60.0}
_REQUIRED_KEYS = (
    "target_abs_kpa", "hold_seconds",
    "max_sample_interval_s", "max_rise_kpa_min",
)


# ---------------------------------------------------------------- 工具

def _num(x):
    """有限数值；bool 不算。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x)


def _issue(code, message, event_seq=None, **detail):
    d = dict(detail)
    if event_seq is not None:
        d["event_seq"] = event_seq
    return {"code": code, "message": message, "detail": d}


# ---------------------------------------------------------------- 规范检查点

def normalize_checkpoints(spec, zones):
    """解析 spec.compaction，返回 (checkpoints, spec_issues)。

    非法检查点以 COMPACTION_SPEC_INVALID 报告且不参与后续绑定。
    """
    comp = spec.get("compaction")
    out, issues = [], []
    if not comp:
        return out, issues
    if not isinstance(comp, dict):
        issues.append(_issue("COMPACTION_SPEC_INVALID",
                             "spec.compaction 必须是对象"))
        return out, issues

    defaults = dict(_BUILTIN_DEFAULTS)
    raw_defaults = comp.get("defaults") or {}
    if not isinstance(raw_defaults, dict):
        issues.append(_issue("COMPACTION_SPEC_INVALID",
                             "spec.compaction.defaults 必须是对象"))
        raw_defaults = {}
    defaults.update(raw_defaults)

    spec_plies = spec.get("plies") or []
    seq_by_ply = {sp.get("ply_id"): sp.get("seq") for sp in spec_plies}
    max_seq = max((sp.get("seq") or 0) for sp in spec_plies) if spec_plies else 0
    all_zones = sorted(zones)

    raw_cps = comp.get("checkpoints")
    if raw_cps is None:
        return out, issues
    if not isinstance(raw_cps, list):
        issues.append(_issue("COMPACTION_SPEC_INVALID",
                             "spec.compaction.checkpoints 必须是列表"))
        return out, issues

    seen_ids = set()
    for idx, cp in enumerate(raw_cps):
        where = f"（第 {idx + 1} 个检查点）"
        if not isinstance(cp, dict):
            issues.append(_issue("COMPACTION_SPEC_INVALID",
                                 f"压实检查点{where}必须是对象"))
            continue
        cid = cp.get("checkpoint_id") or f"CP{idx + 1}"
        if not isinstance(cid, str) or not cid.strip():
            issues.append(_issue("COMPACTION_SPEC_INVALID",
                                 f"压实检查点{where}缺少有效 checkpoint_id"))
            continue
        if cid in seen_ids:
            issues.append(_issue("COMPACTION_SPEC_INVALID",
                                 f"压实检查点 {cid} 重复定义",
                                 checkpoint_id=cid))
            continue

        after_seq = cp.get("after_seq")
        after_ply = cp.get("after_ply")
        if after_seq is None and after_ply is not None:
            after_seq = seq_by_ply.get(after_ply)
            if after_seq is None:
                issues.append(_issue("COMPACTION_SPEC_INVALID",
                                     f"检查点 {cid} 的 after_ply={after_ply} "
                                     f"不在铺层规范中", checkpoint_id=cid))
                continue
        if not isinstance(after_seq, int) or isinstance(after_seq, bool) \
                or after_seq <= 0:
            issues.append(_issue("COMPACTION_SPEC_INVALID",
                                 f"检查点 {cid} 需要正整数 after_seq（或可解析的 "
                                 f"after_ply）", checkpoint_id=cid))
            continue
        if after_seq > max_seq:
            issues.append(_issue("COMPACTION_SPEC_INVALID",
                                 f"检查点 {cid} 的 after_seq={after_seq} 超出规范"
                                 f"最大层序 {max_seq}，永远无法满足",
                                 checkpoint_id=cid, after_seq=after_seq))
            continue

        cp_zones = cp.get("zones")
        if cp_zones is None:
            zids = all_zones
        else:
            if not isinstance(cp_zones, list) or not cp_zones \
                    or not all(isinstance(z, str) for z in cp_zones):
                issues.append(_issue("COMPACTION_SPEC_INVALID",
                                     f"检查点 {cid} 的 zones 必须是非空字符串列表",
                                     checkpoint_id=cid))
                continue
            zids = cp_zones
            unknown = [z for z in zids if z not in zones]
            if unknown:
                issues.append(_issue("COMPACTION_SPEC_INVALID",
                                     f"检查点 {cid} 引用未定义分区 {unknown}",
                                     checkpoint_id=cid, zones=unknown))
                continue

        vals = dict(defaults)
        vals.update({k: cp[k] for k in _REQUIRED_KEYS if k in cp})
        bad = False
        for k in ("target_abs_kpa", "hold_seconds", "max_sample_interval_s",
                  "max_rise_kpa_min", "leak_test_seconds"):
            n = _num(vals.get(k))
            if n is None:
                issues.append(_issue("COMPACTION_SPEC_INVALID",
                                     f"检查点 {cid} 缺少数值参数 {k}",
                                     checkpoint_id=cid, param=k))
                bad = True
            elif k != "max_rise_kpa_min" and n <= 0:
                issues.append(_issue("COMPACTION_SPEC_INVALID",
                                     f"检查点 {cid} 的 {k} 必须为正数",
                                     checkpoint_id=cid, param=k, value=n))
                bad = True
            elif k == "max_rise_kpa_min" and n < 0:
                issues.append(_issue("COMPACTION_SPEC_INVALID",
                                     f"检查点 {cid} 的 max_rise_kpa_min 不能为负",
                                     checkpoint_id=cid, param=k, value=n))
                bad = True
            vals[k] = n
        if bad:
            continue

        seen_ids.add(cid)
        out.append({
            "checkpoint_id": cid, "after_seq": after_seq,
            "after_ply": after_ply, "zones": sorted(zids),
            "target_abs_kpa": vals["target_abs_kpa"],
            "hold_seconds": vals["hold_seconds"],
            "max_sample_interval_s": vals["max_sample_interval_s"],
            "max_rise_kpa_min": vals["max_rise_kpa_min"],
            "leak_test_seconds": vals["leak_test_seconds"],
        })

    out.sort(key=lambda c: (c["after_seq"], c["checkpoint_id"]))
    return out, issues


# ---------------------------------------------------------------- 压实会话重放

def parse_sessions(events):
    """按事件次序切分出封袋→抽真空→读数→隔离→结束会话。

    返回 (sessions, orphan_issues)；会话结构问题先记入 session['issues']，
    待检查点绑定后再带上层号/区域上报。
    """
    sessions, orphans = [], []

    def new_session(ev):
        p = ev["payload"]
        return {
            "seal_seq": ev["seq"], "seal_at": p.get("at"),
            "seal_t": parse_time(p.get("at")),
            "bag_zones": p.get("zones"),
            "pump_seq": None, "pump_t": None,
            "iso_seq": None, "iso_t": None,
            "end_seq": None, "end_t": None, "end_at": None,
            "readings": [],          # {seq, at, t, pressure_kpa}
            "closed": False, "issues": [],
        }

    cur = None
    for ev in events:
        etype, p, seq = ev["type"], ev["payload"], ev["seq"]
        if etype == "bag_sealed":
            if cur is not None:
                cur["issues"].append(_issue(
                    "COMPACTION_SESSION_ORDER",
                    f"事件 #{seq} 重新封袋，但上一真空袋（事件 "
                    f"#{cur['seal_seq']}）未提交结束事件",
                    event_seq=seq, seal_event=cur["seal_seq"]))
                sessions.append(cur)
            cur = new_session(ev)
            continue
        if etype not in STAGE_EVENTS:
            continue
        if cur is None:
            orphans.append(_issue(
                "COMPACTION_SESSION_ORDER",
                f"压实事件 {etype}（#{seq}）之前没有封袋事件",
                event_seq=seq, stage=etype, zones=p.get("zones")))
            continue

        if etype == "vacuum_started":
            if cur["pump_seq"] is not None:
                cur["issues"].append(_issue(
                    "COMPACTION_SESSION_ORDER",
                    f"真空袋（封袋事件 #{cur['seal_seq']}）重复提交抽真空事件",
                    event_seq=seq, seal_event=cur["seal_seq"]))
            else:
                cur["pump_seq"], cur["pump_t"] = seq, parse_time(p.get("at"))
                if p.get("at") is not None and cur["pump_t"] is None:
                    cur["issues"].append(_issue(
                        "COMPACTION_DATA_MISSING",
                        f"抽真空事件 #{seq} 的时刻 {p.get('at')!r} 无法解析",
                        event_seq=seq, at=p.get("at")))
        elif etype == "vacuum_reading":
            cur["readings"].append({
                "seq": seq, "at": p.get("at"),
                "t": parse_time(p.get("at")),
                "pressure_kpa": p.get("pressure_kpa"),
            })
        elif etype == "pump_isolated":
            if cur["pump_seq"] is None:
                cur["issues"].append(_issue(
                    "COMPACTION_SESSION_ORDER",
                    f"真空袋（封袋事件 #{cur['seal_seq']}）未抽真空即隔离泵",
                    event_seq=seq, seal_event=cur["seal_seq"]))
            elif cur["iso_seq"] is not None:
                cur["issues"].append(_issue(
                    "COMPACTION_SESSION_ORDER",
                    f"真空袋（封袋事件 #{cur['seal_seq']}）重复隔离泵",
                    event_seq=seq, seal_event=cur["seal_seq"]))
            else:
                cur["iso_seq"], cur["iso_t"] = seq, parse_time(p.get("at"))
                if p.get("at") is not None and cur["iso_t"] is None:
                    cur["issues"].append(_issue(
                        "COMPACTION_DATA_MISSING",
                        f"隔离泵事件 #{seq} 的时刻 {p.get('at')!r} 无法解析",
                        event_seq=seq, at=p.get("at")))
        elif etype == "compaction_ended":
            if cur["closed"]:
                cur["issues"].append(_issue(
                    "COMPACTION_SESSION_ORDER",
                    f"真空袋（封袋事件 #{cur['seal_seq']}）重复提交结束事件",
                    event_seq=seq, seal_event=cur["seal_seq"]))
            else:
                cur["end_seq"] = seq
                cur["end_t"], cur["end_at"] = parse_time(p.get("at")), p.get("at")
                cur["closed"] = True
                if p.get("at") is not None and cur["end_t"] is None:
                    cur["issues"].append(_issue(
                        "COMPACTION_DATA_MISSING",
                        f"结束事件 #{seq} 的时刻 {p.get('at')!r} 无法解析",
                        event_seq=seq, at=p.get("at")))
                sessions.append(cur)
                cur = None
    if cur is not None:
        sessions.append(cur)  # 事件链结束时仍敞开
    return sessions, orphans


def _bag_zone_set(session, all_zones):
    bz = session["bag_zones"]
    if bz is None:
        return set(all_zones), []
    return set(bz), [z for z in bz if z not in all_zones]


# ---------------------------------------------------------------- 单会话核算

def evaluate_session(session, cp):
    """对绑定到某检查点的会话做阶段完整性/读数/达压/检漏核算。

    返回 (issues, metrics)；issues 为 (code, message, detail) 元组列表。
    """
    issues = list(session["issues"])
    seal = session["seal_seq"]
    if session["seal_t"] is None:
        issues.append(_issue(
            "COMPACTION_DATA_MISSING",
            f"封袋事件 #{seal} 缺少可解析的封袋时刻", event_seq=seal))
    if session["pump_seq"] is None:
        issues.append(_issue(
            "COMPACTION_SESSION_ORDER",
            f"真空袋（封袋事件 #{seal}）缺少抽真空事件", event_seq=seal,
            seal_event=seal))
    if session["iso_seq"] is None:
        issues.append(_issue(
            "COMPACTION_SESSION_ORDER",
            f"真空袋（封袋事件 #{seal}）缺少隔离泵事件，无法执行检漏",
            event_seq=seal, seal_event=seal))
    if not session["closed"]:
        issues.append(_issue(
            "COMPACTION_SESSION_ORDER",
            f"真空袋（封袋事件 #{seal}）缺少结束事件，压实尚未闭环",
            event_seq=seal, seal_event=seal))

    max_gap = cp["max_sample_interval_s"]
    target = cp["target_abs_kpa"]

    usable = []
    for r in session["readings"]:
        p = _num(r["pressure_kpa"])
        if r["t"] is None:
            issues.append(_issue(
                "COMPACTION_DATA_MISSING",
                f"压力读数事件 #{r['seq']} 缺少可解析时标",
                event_seq=r["seq"], at=r["at"]))
            continue
        if p is None:
            issues.append(_issue(
                "COMPACTION_DATA_MISSING",
                f"压力读数事件 #{r['seq']} 的 pressure_kpa 非数值",
                event_seq=r["seq"], pressure=r["pressure_kpa"]))
            continue
        usable.append({**r, "p": p})

    # 读数倒序 / 断档（跨抽真空与检漏全段）；违规读数标 in_order=False，
    # 不参与连续时长与检漏区间核算。
    ordered, prev = [], None
    for r in usable:
        ok_order = prev is None or (r["t"] - prev["t"]).total_seconds() >= 0
        if prev is not None:
            dt = (r["t"] - prev["t"]).total_seconds()
            if dt < 0:
                issues.append(_issue(
                    "COMPACTION_READING_ORDER",
                    f"压力读数倒序：事件 #{r['seq']}（{r['at']}）早于前一读数 "
                    f"事件 #{prev['seq']}（{prev['at']}）",
                    event_seq=r["seq"], previous_event=prev["seq"],
                    at=r["at"], previous_at=prev["at"]))
            elif dt > max_gap:
                issues.append(_issue(
                    "COMPACTION_READING_GAP",
                    f"压力读数断档：事件 #{prev['seq']} 与 #{r['seq']} 间隔 "
                    f"{dt:.0f}s，超过采样上限 {max_gap:g}s",
                    event_seq=r["seq"], previous_event=prev["seq"],
                    interval_s=round(dt, 1), limit_s=max_gap))
        if ok_order:
            ordered.append(r)
            prev = r  # 倒序读数不作为下一条的基准（防止负间隔连锁误判）

    iso_seq = session["iso_seq"]
    pre = [r for r in ordered if iso_seq is None or r["seq"] < iso_seq]
    post = [r for r in ordered if iso_seq is not None and r["seq"] > iso_seq]

    # ---- 连续达压时长（隔离前） ----
    hold = {"required_seconds": cp["hold_seconds"], "target_abs_kpa": target,
            "achieved_seconds": 0.0, "best_window": None, "min_pressure_kpa": None}
    best_span, best_window, run_start, run_last, global_min = 0.0, None, None, None, None
    for r in pre:
        if global_min is None or r["p"] < global_min:
            global_min = r["p"]
        if r["p"] <= target:
            if run_start is None:
                run_start = run_last = r
            else:
                gap = (r["t"] - run_last["t"]).total_seconds()
                if gap > max_gap:
                    run_start = r          # 断档不能计入连续时长
            run_last = r
            span = (run_last["t"] - run_start["t"]).total_seconds()
            if span >= best_span:
                best_span, best_window = span, (run_start, run_last)
        else:
            run_start = None
    hold["achieved_seconds"] = round(best_span, 1)
    hold["min_pressure_kpa"] = round(global_min, 3) if global_min is not None else None
    if best_window:
        w0, w1 = best_window
        hold["best_window"] = {
            "from": w0["at"], "to": w1["at"],
            "first_reading_event": w0["seq"], "last_reading_event": w1["seq"],
            "seconds": round((w1["t"] - w0["t"]).total_seconds(), 1),
        }

    if not pre:
        issues.append(_issue(
            "COMPACTION_DATA_MISSING",
            f"真空袋（封袋事件 #{seal}）抽真空阶段没有可核算的压力读数",
            seal_event=seal))
    elif not any(r["p"] <= target for r in pre):
        issues.append(_issue(
            "COMPACTION_PRESSURE",
            f"真空袋（封袋事件 #{seal}）最低绝对压力 "
            f"{hold['min_pressure_kpa']}kPa，未达到目标 {target:g}kPa",
            seal_event=seal, min_pressure_kpa=hold["min_pressure_kpa"],
            target_abs_kpa=target))
    elif best_span < cp["hold_seconds"]:
        issues.append(_issue(
            "COMPACTION_HOLD_SHORT",
            f"真空袋（封袋事件 #{seal}）连续达压仅 {best_span:.0f}s，"
            f"短于保持要求 {cp['hold_seconds']:g}s",
            seal_event=seal, achieved_seconds=round(best_span, 1),
            required_seconds=cp["hold_seconds"],
            window=hold["best_window"]))

    # ---- 隔离后检漏区间与回升率 ----
    leak = {"required_seconds": cp["leak_test_seconds"],
            "limit_kpa_min": cp["max_rise_kpa_min"],
            "seconds": 0.0, "rise_rate_kpa_min": None, "window": None}
    if iso_seq is not None:
        if len(post) >= 2:
            r0, r1 = post[0], post[-1]
            seconds = (r1["t"] - r0["t"]).total_seconds()
            leak["seconds"] = round(seconds, 1)
            leak["window"] = {
                "from": r0["at"], "to": r1["at"],
                "first_reading_event": r0["seq"], "last_reading_event": r1["seq"],
                "seconds": round(seconds, 1)}
            if seconds < cp["leak_test_seconds"]:
                issues.append(_issue(
                    "COMPACTION_LEAK_INTERVAL",
                    f"检漏区间仅 {seconds:.0f}s（事件 #{r0['seq']}→"
                    f"#{r1['seq']}），短于要求 {cp['leak_test_seconds']:g}s",
                    event_seq=r1["seq"], seconds=round(seconds, 1),
                    required_seconds=cp["leak_test_seconds"],
                    isolate_event=iso_seq))
            else:
                minutes = seconds / 60.0
                rise = (r1["p"] - r0["p"]) / minutes
                leak["rise_rate_kpa_min"] = round(rise, 4)
                if rise > cp["max_rise_kpa_min"]:
                    issues.append(_issue(
                        "COMPACTION_LEAK_RATE",
                        f"隔离后压力回升率 {rise:.2f}kPa/min（{r0['p']}→"
                        f"{r1['p']}kPa / {minutes:.1f}min），超过允许值 "
                        f"{cp['max_rise_kpa_min']:g}kPa/min",
                        event_seq=r1["seq"], rise_rate_kpa_min=round(rise, 4),
                        limit_kpa_min=cp["max_rise_kpa_min"],
                        window=leak["window"]))
        else:
            issues.append(_issue(
                "COMPACTION_LEAK_INTERVAL",
                f"隔离泵（事件 #{iso_seq}）后压力读数不足两条，"
                f"无法构成检漏区间",
                event_seq=iso_seq, isolate_event=iso_seq,
                required_seconds=cp["leak_test_seconds"]))

    metrics = {
        "events": {
            "bag_sealed": session["seal_seq"],
            "vacuum_started": session["pump_seq"],
            "readings": [r["seq"] for r in session["readings"]],
            "pump_isolated": session["iso_seq"],
            "compaction_ended": session["end_seq"],
        },
        "bag_zones": session["bag_zones"],
        "sealed_at": session["seal_at"], "ended_at": session["end_at"],
        "hold": hold, "leak": leak,
    }
    return issues, metrics


# ---------------------------------------------------------------- 主入口

def evaluate_compaction(job, zones, spec_plies, stack, events):
    """重建压实会话并把检查点绑定到当时有效的铺层/分区。

    返回 (state, violations)。state 含检查点结果（压力区间、指标、事件引用）
    与会话清单，供 /state、批准快照、版本差异和 JSON 随件包使用。
    """
    violations = []

    def v(rule, message, plies, zids, **details):
        violations.append({
            "rule": rule, "message": message,
            "plies": [p for p in (plies or []) if p is not None],
            "zones": zids or [], "details": details,
        })

    checkpoints, spec_issues = normalize_checkpoints(job.get("spec") or {}, zones)
    for iss in spec_issues:
        v(iss["code"], iss["message"], None, iss["detail"].get("zones"),
          **{k: x for k, x in iss["detail"].items() if k != "zones"})

    sessions, orphans = parse_sessions(events)
    for iss in orphans:
        v(iss["code"], iss["message"], None, iss["detail"].get("zones"),
          **{k: x for k, x in iss["detail"].items() if k != "zones"})

    active = [e for e in stack if e["active"]]
    active_latest = {}
    for e in active:  # 重复铺放取最后一张（DUPLICATE_PLY 已另报）
        active_latest[e["ply_id"]] = e

    cp_out = []

    for cp in checkpoints:
        k, cp_zones = cp["after_seq"], set(cp["zones"])
        required = [sp for sp in spec_plies
                    if (sp.get("seq") or 0) <= k
                    and cp_zones & set(sp.get("zones") or [])]
        required.sort(key=lambda sp: sp["seq"])
        req_pids = [sp["ply_id"] for sp in required]

        invalidations = []
        for e in stack:
            if e["ply_id"] in req_pids and e["removed_by"] is not None:
                invalidations.append({
                    "ply_id": e["ply_id"], "kind": "removed",
                    "event_seq": e["removed_by"],
                    "replaced_by_event": e["replaced_by"],
                })

        entries = [active_latest.get(pid) for pid in req_pids]
        due = all(en is not None for en in entries)

        result = {
            "checkpoint_id": cp["checkpoint_id"], "after_seq": k,
            "zones": sorted(cp_zones), "required_plies": req_pids,
            "target_abs_kpa": cp["target_abs_kpa"],
            "hold_seconds": cp["hold_seconds"],
            "max_sample_interval_s": cp["max_sample_interval_s"],
            "max_rise_kpa_min": cp["max_rise_kpa_min"],
            "leak_test_seconds": cp["leak_test_seconds"],
            "invalidations": invalidations,
            "bound_session": None, "status": "not_due",
        }

        if not due:
            cp_out.append(result)
            continue

        # 检查点归位点：当前生效的应压实层中最后一次铺放（含返工替代）事件。
        anchor = max(entries, key=lambda en: en["event_seq"])
        due_seq = anchor["event_seq"]

        all_zones = sorted(zones)
        qualified = []
        for s in sessions:
            bag_set, unknown = _bag_zone_set(s, all_zones)
            if cp_zones <= bag_set and s["seal_seq"] > due_seq:
                qualified.append(s)  # 封袋时应压实层已全部归位
        result["status"] = "missing"

        if not qualified:
            v("COMPACTION_MISSING",
              f"检查点 {cp['checkpoint_id']}（铺至规范序 {k} 层后）漏做压实："
              f"层 {req_pids}、区域 {sorted(cp_zones)} 之后没有合格封袋压实记录",
              req_pids, sorted(cp_zones), checkpoint_id=cp["checkpoint_id"],
              after_seq=k)
            cp_out.append(result)
            continue

        # 技术指标按最后一次压实会话核算（追加重压实即闭环）；
        # “后续铺层提前开始”按第一次压实窗口判定（夹气只可能发生在首压之前）。
        first, chosen = qualified[0], qualified[-1]

        # 未受返工影响的检查点若中途有未闭环/乱序的压实记录，结构问题仍留痕上报。
        for s in qualified[:-1]:
            for iss in s["issues"]:
                d = dict(iss["detail"])
                d.setdefault("checkpoint_id", cp["checkpoint_id"])
                d.setdefault("seal_event", s["seal_seq"])
                v(iss["code"],
                  f"{iss['message']}（检查点 {cp['checkpoint_id']}，层 {req_pids}，"
                  f"区域 {sorted(cp_zones)}）",
                  req_pids, sorted(cp_zones), **d)

        _, unknown_bag = _bag_zone_set(chosen, all_zones)
        for z in unknown_bag:
            v("ZONE_UNKNOWN",
              f"检查点 {cp['checkpoint_id']} 的封袋事件 "
              f"#{chosen['seal_seq']} 引用未定义分区 {z}",
              req_pids, [z], checkpoint_id=cp["checkpoint_id"],
              event_seq=chosen["seal_seq"])

        # 阈值随检查点而变，每次都按该检查点重新核算
        before = len(violations)
        issues, metrics = evaluate_session(chosen, cp)
        for iss in issues:
            d = dict(iss["detail"])
            d.setdefault("checkpoint_id", cp["checkpoint_id"])
            d.setdefault("seal_event", chosen["seal_seq"])
            v(iss["code"],
              f"{iss['message']}（检查点 {cp['checkpoint_id']}，层 {req_pids}，"
              f"区域 {sorted(cp_zones)}）",
              req_pids, sorted(cp_zones), **d)
        session_failed = len(violations) > before or bool(unknown_bag)
        early_started = False

        # 后续铺层提前开始：归位之后、首次压实结束之前铺放了更靠后的层。
        # 按事件次序判定，不依赖时标解析。
        later_pids = {sp["ply_id"] for sp in spec_plies
                      if (sp.get("seq") or 0) > k
                      and cp_zones & set(sp.get("zones") or [])}
        if first["closed"]:
            for ev in events:
                if not (due_seq < ev["seq"] < first["end_seq"]):
                    continue
                if ev["type"] == "ply_placed":
                    pid = ev["payload"].get("ply_id")
                    placed_at = ev["payload"].get("placed_at")
                elif ev["type"] == "ply_replaced":
                    repl = ev["payload"].get("replacement") or {}
                    pid = repl.get("ply_id")
                    placed_at = repl.get("placed_at")
                else:
                    continue
                if pid in later_pids:
                    early_started = True
                    v("COMPACTION_LAYUP_EARLY",
                      f"检查点 {cp['checkpoint_id']} 首次压实尚未结束（结束事件 "
                      f"#{first['end_seq']}，{first['end_at']}），后续层 {pid} "
                      f"已于 {placed_at} 继续铺放（事件 #{ev['seq']}）",
                      req_pids + [pid], sorted(cp_zones),
                      checkpoint_id=cp["checkpoint_id"],
                      early_ply=pid, early_event=ev["seq"],
                      early_at=placed_at, end_event=first["end_seq"])

        result["bound_session"] = metrics
        result["status"] = "failed" if session_failed or early_started else "passed"
        cp_out.append(result)

    sessions_out = [{
        "bag_sealed_event": s["seal_seq"], "sealed_at": s["seal_at"],
        "vacuum_started_event": s["pump_seq"],
        "pump_isolated_event": s["iso_seq"],
        "compaction_ended_event": s["end_seq"],
        "reading_events": [r["seq"] for r in s["readings"]],
        "zones": s["bag_zones"], "closed": s["closed"],
        "issue_codes": sorted({i["code"] for i in s["issues"]}),
    } for s in sessions]

    return {"checkpoints": cp_out, "sessions": sessions_out}, violations
