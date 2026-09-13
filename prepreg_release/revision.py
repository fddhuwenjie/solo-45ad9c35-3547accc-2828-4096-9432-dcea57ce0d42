"""规范换版：派生提议、影响分析、返工序列与冲突判定。

铺层进行到一半时，工艺规范可能因材料替代、丢层调整或开孔边界变化而
换版。换版不改动既有铺放/材料事件（只读），而是把新规范登记为工单的
新分支：引擎逐层比较新旧规范的材料、层序、角度、正反面、覆盖区、
接缝与丢层边界，并核对现场实铺属性（角度/正反面/材料/覆盖）是否仍
满足新版对应层，标出可以沿用的实铺层；中间层变化或实铺不符时，把
必须揭除的上覆层、随之失效的压实检查点与待补铺层排成确定的返工序列。

冲突（任一命中即返回 409 且不启用新版）：
  MAPPING_AMBIGUOUS        层映射多解（层号/层序重复、显式映射多对一）
  ZONE_DATUM_INCOMPATIBLE  分区基准不兼容（引用未定义分区、随迁更改分区/基准）
  EFFECTIVE_BEFORE_RECORDS 生效时刻早于现场记录
  LOCKED_PLY_AFFECTED      返工序列需揭除已批准快照锁定的铺层

确认（confirm）后新规范生效：后续校验、批准与 JSON 随件包均从该分支
重算；返工仍通过追加 ply_removed / ply_replaced 事件闭环。
"""

import hashlib
import json

from .compaction import normalize_checkpoints
from .core import DEFAULT_RULES, angle_diff, parse_time, replay
from .geometry import coverage_fraction

# 逐层比较的标量字段（ply_id 由映射解决，不参与比较）
_SCALAR_FIELDS = ("material", "angle", "face")


def spec_hash(spec):
    """规范全文的 SHA-256（与批准快照的 spec_hash 同一口径）。"""
    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _zones_of(sp):
    return sorted(sp.get("zones") or [])


def _seams_of(sp):
    """层级接缝要求规范化：缺失与空列表等价，逐项按规范化 JSON 排序。"""
    return sorted(json.dumps(s, sort_keys=True, ensure_ascii=False)
                  for s in (sp.get("seams") or []))


def diff_ply(old, new):
    """逐字段比较一对映射层；返回差异明细（空列表 = 该层可沿用）。"""
    diffs = []
    for f in _SCALAR_FIELDS:
        if old.get(f) != new.get(f):
            diffs.append({"field": f, "from": old.get(f), "to": new.get(f)})
    if (old.get("seq") or 0) != (new.get("seq") or 0):
        diffs.append({"field": "seq", "from": old.get("seq"),
                      "to": new.get("seq")})
    if _zones_of(old) != _zones_of(new):
        diffs.append({"field": "zones", "from": _zones_of(old),
                      "to": _zones_of(new)})
    if old.get("drop_at") != new.get("drop_at"):
        diffs.append({"field": "drop_at", "from": old.get("drop_at"),
                      "to": new.get("drop_at")})
    if _seams_of(old) != _seams_of(new):
        diffs.append({"field": "seams", "from": old.get("seams") or [],
                      "to": new.get("seams") or []})
    return diffs


def _event_moment(ev):
    """现场作业时刻：仅取业务时标（placed_at/at）；无业务时标的事件
    （如过程备注）不参与生效时刻判定。"""
    p = ev.get("payload") or {}
    for key in ("placed_at", "at"):
        t = parse_time(p.get(key))
        if t is not None:
            return t
    return parse_time((p.get("replacement") or {}).get("placed_at"))


def _placed_nonconformity(entry, sp, rules, rolls, job_zones):
    """实铺属性与新版对应层逐项核对（角度/正反面/材料/覆盖区）。

    仅核对记录中存在的属性；缺失项由既有 DATA_MISSING 规则处置。
    返回不符明细（空列表 = 该实铺层可沿用）。
    """
    p = entry["payload"]
    out = []
    ang, req = p.get("angle"), sp.get("angle")
    if ang is not None and req is not None:
        dev = angle_diff(float(ang), float(req))
        if dev > rules["angle_tolerance_deg"]:
            out.append({"field": "angle", "actual": ang, "required": req,
                        "deviation_deg": round(dev, 2)})
    if sp.get("face") and p.get("face") and p["face"] != sp["face"]:
        out.append({"field": "face", "actual": p["face"],
                    "required": sp["face"]})
    actual_mat = None
    if p.get("roll") is not None:
        roll = rolls.get(p["roll"])
        if roll is not None:
            actual_mat = roll.get("material")
    if actual_mat is None and p.get("material") is not None:
        actual_mat = p.get("material")
    if actual_mat and sp.get("material") and actual_mat != sp["material"]:
        out.append({"field": "material", "actual": actual_mat,
                    "required": sp["material"]})
    geom = p.get("geometry")
    if geom:
        for zid in sp.get("zones") or []:
            z = job_zones.get(zid)
            if z is None:
                continue
            frac = coverage_fraction(z["polygon"], geom)
            if frac < rules["coverage_min_fraction"]:
                out.append({"field": "zones", "zone": zid,
                            "actual": round(frac, 3),
                            "required": rules["coverage_min_fraction"]})
    return out


def _build_mapping(old_plies, new_plies, explicit):
    """构建 新层号 → 旧层号 映射。

    返回 (mapping, conflicts, added, removed_old)。显式映射（重编号声明）
    优先，未提及的层按同号自动配对；层号/层序重复、显式映射多对一即
    MAPPING_AMBIGUOUS。
    """
    conflicts = []
    seen_pid, seen_seq = set(), {}
    for sp in new_plies:
        pid, sq = sp.get("ply_id"), sp.get("seq")
        if pid in seen_pid:
            conflicts.append({
                "code": "MAPPING_AMBIGUOUS",
                "message": f"新规范中铺层号 {pid} 重复，层映射无法唯一确定",
                "plies": [pid]})
        seen_pid.add(pid)
        if sq in seen_seq:
            conflicts.append({
                "code": "MAPPING_AMBIGUOUS",
                "message": f"新规范层序 {sq} 被铺层 {seen_seq[sq]} 与 {pid} "
                           f"重复使用，层序映射无法唯一确定",
                "plies": [seen_seq[sq], pid], "seq": sq})
        else:
            seen_seq[sq] = pid
    old_ids = [sp.get("ply_id") for sp in old_plies]
    dup_old = sorted({p for p in old_ids if old_ids.count(p) > 1})
    if dup_old:
        conflicts.append({
            "code": "MAPPING_AMBIGUOUS",
            "message": f"基线规范自身层号 {dup_old} 重复，无法建立映射",
            "plies": dup_old})

    mapping, used_old = {}, set()
    for npid, opid in (explicit or {}).items():
        if opid in used_old:
            conflicts.append({
                "code": "MAPPING_AMBIGUOUS",
                "message": f"旧铺层 {opid} 被多个新层争用，层映射多解",
                "plies": [opid]})
        mapping[npid] = opid
        used_old.add(opid)
    old_id_set = set(old_ids)
    for sp in new_plies:
        npid = sp.get("ply_id")
        if npid and npid not in mapping and npid in old_id_set \
                and npid not in used_old:
            mapping[npid] = npid
            used_old.add(npid)
    added = [sp.get("ply_id") for sp in new_plies
             if sp.get("ply_id") not in mapping]
    removed = sorted(old_id_set - used_old)
    return mapping, conflicts, added, removed


def analyze(job, events, base_spec, new_spec, effective_at,
            locked_plies=(), explicit_mapping=None,
            new_zones=None, new_tool_datum=None, rolls=None):
    """换版影响分析。

    job               工单（zones/tool_datum 为建档基准）
    events            铺放事件链（只读）
    base_spec         基线规范全文（派生自指定版本）
    new_spec          新规范全文
    effective_at      生效时刻（datetime，调用方已解析）
    locked_plies      已批准快照锁定的铺层号
    explicit_mapping  可选显式层映射 {新层号: 旧层号}
    new_zones / new_tool_datum  请求若试图随迁更改分区/基准
    rolls             工单料卷台账（核对实铺材料用）

    返回 (impact, conflicts)；conflicts 非空时不得启用新版。映射多解时
    impact 为 None——映射不定，处置序列不可信。
    """
    old_plies = (base_spec or {}).get("plies") or []
    new_plies = (new_spec or {}).get("plies") or []
    job_zones = {z["zone_id"]: z for z in (job.get("zones") or [])}
    conflicts = []

    # ---- 分区基准兼容 ----
    if new_zones is not None and new_zones != job.get("zones"):
        conflicts.append({
            "code": "ZONE_DATUM_INCOMPATIBLE",
            "message": "换版不得更改分区边界（分区基准属于建档数据）"})
    if new_tool_datum is not None and new_tool_datum != job.get("tool_datum"):
        conflicts.append({
            "code": "ZONE_DATUM_INCOMPATIBLE",
            "message": "换版不得更改模具基准（tool_datum 属于建档数据）"})
    for sp in new_plies:
        unknown = [z for z in (sp.get("zones") or []) if z not in job_zones]
        if unknown:
            conflicts.append({
                "code": "ZONE_DATUM_INCOMPATIBLE",
                "message": f"新规范铺层 {sp.get('ply_id')} 引用工单未定义的"
                           f"分区 {unknown}",
                "plies": [sp.get("ply_id")], "zones": unknown})
    comp = (new_spec or {}).get("compaction") or {}
    for cp in comp.get("checkpoints") or []:
        if not isinstance(cp, dict):
            continue
        unknown = [z for z in (cp.get("zones") or []) if z not in job_zones]
        if unknown:
            conflicts.append({
                "code": "ZONE_DATUM_INCOMPATIBLE",
                "message": f"新规范压实检查点 {cp.get('checkpoint_id')} 引用"
                           f"工单未定义的分区 {unknown}",
                "zones": unknown})

    # ---- 环境限值块合法性（非法块不得随换版生效）----
    from .environment import normalize_environment
    env_cfg, env_issues = normalize_environment(new_spec or {})
    for iss in env_issues:
        conflicts.append({
            "code": "ENV_SPEC_INVALID",
            "message": f"新规范 environment 限值块非法：{iss['message']}",
            "detail": iss.get("detail") or {}})
    for zid in env_cfg["zones"]:
        if zid not in job_zones:
            conflicts.append({
                "code": "ZONE_DATUM_INCOMPATIBLE",
                "message": f"新规范环境限值引用工单未定义的分区 {zid}",
                "zones": [zid]})
    for mat in env_cfg["materials"]:
        if mat not in ((new_spec or {}).get("materials") or {}):
            conflicts.append({
                "code": "ENV_SPEC_INVALID",
                "message": f"新规范环境限值引用未定义材料 {mat}",
                "detail": {"material": mat}})

    # ---- 生效时刻不得早于现场记录 ----
    moments = [m for m in (_event_moment(ev) for ev in events) if m]
    if effective_at is not None and moments:
        latest = max(moments)
        if effective_at < latest:
            conflicts.append({
                "code": "EFFECTIVE_BEFORE_RECORDS",
                "message": f"生效时刻 {effective_at.isoformat()} 早于最新现场"
                           f"记录 {latest.isoformat()}",
                "effective_at": effective_at.isoformat(),
                "latest_record": latest.isoformat()})

    # ---- 层映射 ----
    mapping, map_conflicts, added, removed_old = _build_mapping(
        old_plies, new_plies, explicit_mapping)
    conflicts.extend(map_conflicts)
    if map_conflicts:
        return None, conflicts  # 映射不定，处置序列不可信

    old_by_id = {sp["ply_id"]: sp for sp in old_plies}
    new_by_id = {sp["ply_id"]: sp for sp in new_plies}
    removed_set = set(removed_old)

    # ---- 逐层比较 ----
    changed, unchanged = [], []
    for npid in sorted(mapping):
        opid = mapping[npid]
        diffs = diff_ply(old_by_id[opid], new_by_id[npid])
        if diffs:
            changed.append({"ply_id": npid, "old_ply_id": opid,
                            "diffs": diffs})
        else:
            unchanged.append({"ply_id": npid, "old_ply_id": opid})

    # ---- 实铺处置：可沿用层与返工序列 ----
    stack, _ledger, _anomalies = replay(events)
    active = [e for e in stack if e["active"]]
    changed_old = {c["old_ply_id"] for c in changed}
    new_by_old = {opid: npid for npid, opid in mapping.items()}
    rules = dict(DEFAULT_RULES)
    rules.update((new_spec or {}).get("rules") or {})

    must = {}          # active 下标 → 揭除原因
    nc_details = {}    # active 下标 → 实铺不符明细
    for idx, e in enumerate(active):
        pid = e["ply_id"]
        if pid not in old_by_id:
            continue  # 非规范层属既有违规（UNEXPECTED_PLY），换版不处置
        if pid in removed_set:
            must[idx] = "dropped"    # 新规范已删除该层
        elif pid in changed_old:
            must[idx] = "changed"    # 规范要求已变，旧实铺不再有效
        else:
            # 规范未变≠可沿用：实铺属性须同时满足新版对应层要求
            sp = new_by_id.get(new_by_old.get(pid))
            if sp is not None:
                nc = _placed_nonconformity(e, sp, rules, rolls or {},
                                           job_zones)
                if nc:
                    must[idx] = "nonconforming"
                    nc_details[idx] = nc
    if must:
        first = min(must)
        for idx in range(first, len(active)):
            must.setdefault(idx, "overlying")  # 上覆层连带揭除

    remove = []
    for idx in sorted(must, reverse=True):  # 自上而下依次揭除
        item = {"ply_id": active[idx]["ply_id"], "pos": idx + 1,
                "event_seq": active[idx]["event_seq"], "reason": must[idx]}
        if idx in nc_details:
            item["details"] = nc_details[idx]
        remove.append(item)

    carry_over = [{
        "ply_id": e["ply_id"], "pos": idx + 1, "event_seq": e["event_seq"],
        "new_ply_id": new_by_old.get(e["ply_id"]),
    } for idx, e in enumerate(active) if idx not in must]

    # ---- 随之失效的压实检查点（基线规范定义） ----
    remove_pids = {r["ply_id"] for r in remove}
    checkpoints, _issues = normalize_checkpoints(base_spec or {}, job_zones)
    invalidated = []
    for cp in checkpoints:
        req = {sp.get("ply_id") for sp in old_plies
               if (sp.get("seq") or 0) <= cp["after_seq"]}
        if req & remove_pids:
            invalidated.append(cp["checkpoint_id"])

    # ---- 待补铺序列（按新规范层序） ----
    satisfied = {c["new_ply_id"] for c in carry_over if c["new_ply_id"]}
    changed_new = {c["ply_id"] for c in changed}
    added_set = set(added)
    remove_new = {new_by_old.get(r["ply_id"]) for r in remove}
    relay = []
    for sp in sorted(new_plies,
                     key=lambda s: (s.get("seq") or 0, s.get("ply_id") or "")):
        npid = sp.get("ply_id")
        if npid in satisfied:
            continue
        if npid in added_set:
            reason = "added"       # 新增层
        elif npid in changed_new:
            reason = "changed"     # 要求已变，按新规范重铺
        elif npid in remove_new:
            reason = "relay"       # 上覆连带揭除，原样补铺
        else:
            reason = "pending"     # 尚未铺到，按新规范续铺
        relay.append({"ply_id": npid, "seq": sp.get("seq"),
                      "material": sp.get("material"),
                      "angle": sp.get("angle"), "face": sp.get("face"),
                      "zones": sp.get("zones") or [], "reason": reason})

    # ---- 已锁层（最近批准快照冻结的实铺层） ----
    locked_hit = sorted(set(locked_plies or ()) & remove_pids)
    if locked_hit:
        conflicts.append({
            "code": "LOCKED_PLY_AFFECTED",
            "message": f"返工序列需揭除已批准锁定的铺层 {locked_hit}，"
                       f"换版不得启用",
            "plies": locked_hit})

    impact = {
        "mapping": {k: mapping[k] for k in sorted(mapping)},
        "unchanged": unchanged, "changed": changed,
        "added": sorted(added_set), "removed": sorted(removed_set),
        "carry_over": carry_over,
        "rework": {"remove": remove,
                   "invalidated_checkpoints": invalidated,
                   "relay": relay},
    }
    return impact, conflicts
