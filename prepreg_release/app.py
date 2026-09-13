"""WSGI 路由层：REST API 定义。

接口一览：
  POST /jobs                              建档（模具基准/分区/铺层规范/料卷批号）
  GET  /jobs                              列表
  GET  /jobs/{id}                         详情
  POST /jobs/{id}/rolls                   登记料卷
  POST /jobs/{id}/events                  追加铺放/压实事件（只允许追加）
  GET  /jobs/{id}/events                  事件链
  GET  /jobs/{id}/state                   重建的分区覆盖/厚度/外置时间/压实检查点
  GET  /jobs/{id}/validate                放行规则校验（违规含层号与区域）
  POST /jobs/{id}/approve                 批准放行（冻结快照）
  GET  /jobs/{id}/approvals               批准版列表
  GET  /jobs/{id}/approvals/{v}/package   JSON 随件包（取自冻结快照）
  GET  /jobs/{id}/approvals/diff?a=&b=    版本比较（取自冻结快照）

规范换版（铺层中途换版：材料替代/丢层调整/开孔边界变化）：
  POST /jobs/{id}/spec-revisions          提议换版：派生自指定版本，返回影响
                                          分析（沿用层/返工序列）；冲突 409 不启用
  GET  /jobs/{id}/spec-revisions          换版列表
  GET  /jobs/{id}/spec-revisions/{r}      换版详情（含层映射与返工序列）
  POST /jobs/{id}/spec-revisions/{r}/confirm  确认启用：后续校验/批准/随件包
                                          从该分支重算，事件链保持只读

材料谱系（全局共享、跨工单，事件链只允许追加）：
  POST /materials/units                   登记整卷（制造/失效日期、计量单位、初始数量）
  GET  /materials/units                   单元列表（含数量核平与外置寿命）
  GET  /materials/units/{uid}             单元详情（谱系链/寿命明细）
  GET  /materials/units/{uid}/impact      引用该谱系的工单（仅这些工单需刷新）
  POST /materials/events                  追加裁切/拆包/转移/退库/报废/解冻/纠正绑定事件
  GET  /materials/events                  材料事件链
"""

import json
import re
import uuid
from urllib.parse import parse_qs

from . import core
from . import genealogy as _gen
from . import compaction as _comp
from . import revision as _rev
from . import environment as _env
from . import defects as _def
from .store import Store


def _validate_compaction_item(item):
    """压实现场事件入链前的最小载荷校验；阈值类核算在 analyze 阶段进行。"""
    t = item["type"]
    if not item.get("at"):
        raise ApiError(400, {"error": "invalid_compaction_event",
                             "message": f"{t} 事件需要可解析的 at 时标", "type": t})
    if core.parse_time(item["at"]) is None:
        raise ApiError(400, {"error": "invalid_compaction_event",
                             "message": f"{t} 事件 at={item['at']!r} 无法解析",
                             "type": t})
    if t == "vacuum_reading":
        p = item.get("pressure_kpa")
        if isinstance(p, bool) or not isinstance(p, (int, float)):
            raise ApiError(400, {"error": "invalid_compaction_event",
                                 "message": "vacuum_reading 需要数值 pressure_kpa（绝对压力 kPa）",
                                 "type": t})
    if t == "bag_sealed" and "zones" in item and (
            not isinstance(item["zones"], list)
            or not all(isinstance(z, str) for z in item["zones"])):
        raise ApiError(400, {"error": "invalid_compaction_event",
                             "message": "bag_sealed 的 zones 必须为字符串列表",
                             "type": t})

_STATUS = {
    200: "200 OK", 201: "201 Created", 400: "400 Bad Request",
    404: "404 Not Found", 405: "405 Method Not Allowed",
    409: "409 Conflict", 500: "500 Internal Server Error",
}


class ApiError(Exception):
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload


def make_app(db_path):
    store = Store(db_path)

    # ------------------------------------------------------------ 数据装载
    def load_job(job_id):
        with store.db() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ApiError(404, {"error": "job_not_found", "job_id": job_id})
            rev = conn.execute(
                "SELECT revision,spec FROM spec_revisions"
                " WHERE job_id=? AND status='confirmed'"
                " ORDER BY revision DESC LIMIT 1", (job_id,)).fetchone()
        job = {
            "id": row["id"], "name": row["name"], "status": row["status"],
            "tool_datum": json.loads(row["tool_datum"]),
            "zones": json.loads(row["zones"]),
            "spec": json.loads(row["spec"]),
            "created_at": row["created_at"],
            # 生效规范版本：0 = 建档规范；>0 = 已确认换版
            "spec_revision": rev["revision"] if rev else 0,
        }
        if rev:  # 校验/批准/随件包一律从已确认的换版分支重算
            job["spec"] = json.loads(rev["spec"])
        return job

    def load_rolls(job_id):
        with store.db() as conn:
            rows = conn.execute(
                "SELECT * FROM rolls WHERE job_id=? ORDER BY roll_id",
                (job_id,)).fetchall()
        return {r["roll_id"]: dict(r) for r in rows}

    def load_events(job_id):
        with store.db() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE job_id=? ORDER BY seq",
                (job_id,)).fetchall()
        return [{
            "seq": r["seq"], "type": r["type"], "payload": json.loads(r["payload"]),
            "operator": r["operator"], "recorded_at": r["recorded_at"],
        } for r in rows]

    # ------------------------------------------------------------ 材料谱系装载
    def load_material_events():
        with store.db() as conn:
            rows = conn.execute(
                "SELECT * FROM material_events ORDER BY seq").fetchall()
        return [{
            "seq": r["seq"], "type": r["type"], "payload": json.loads(r["payload"]),
            "operator": r["operator"], "recorded_at": r["recorded_at"],
        } for r in rows]

    def load_unit_usage():
        """跨全部工单的铺层材料单元引用：{unit_id: [引用明细]}。"""
        with store.db() as conn:
            rows = conn.execute(
                "SELECT job_id,seq,type,payload FROM events"
                " WHERE type IN ('ply_placed','ply_replaced')"
                " ORDER BY job_id,seq").fetchall()
        usage = {}
        for r in rows:
            p = json.loads(r["payload"])
            if r["type"] == "ply_placed":
                unit, pid, at = p.get("unit"), p.get("ply_id"), p.get("placed_at")
            else:
                repl = p.get("replacement") or {}
                unit = repl.get("unit")
                pid = repl.get("ply_id", p.get("removed_ply_id"))
                at = repl.get("placed_at")
            if unit:
                usage.setdefault(unit, []).append({
                    "job_id": r["job_id"], "ply_id": pid,
                    "event_seq": r["seq"], "placed_at": at,
                })
        return usage

    def evaluate_materials(job_id):
        """谱系评估（聚焦本工单）：无材料事件且无 unit 引用时返回 None。"""
        usage = load_unit_usage()
        mat_events = load_material_events()
        if not mat_events and not any(
                r["job_id"] == job_id for refs in usage.values() for r in refs):
            return None
        return _gen.evaluate(mat_events, usage, focus_job=job_id)

    def _confirmed_revision_context(job_id):
        """已确认换版记录（含基线规范全文），供缺陷签发的换版撤销判定。"""
        with store.db() as conn:
            rows = conn.execute(
                "SELECT revision,base_revision,spec,confirmed_at"
                " FROM spec_revisions WHERE job_id=? AND status='confirmed'"
                " ORDER BY revision", (job_id,)).fetchall()
            base_row = conn.execute(
                "SELECT spec FROM jobs WHERE id=?", (job_id,)).fetchone()
            spec_cache = {0: json.loads(base_row["spec"])} if base_row else {}
            out = []
            for r in rows:
                if r["base_revision"] not in spec_cache:
                    if r["base_revision"] == 0:
                        spec_cache[0] = json.loads(base_row["spec"])
                    else:
                        br = conn.execute(
                            "SELECT spec FROM spec_revisions"
                            " WHERE job_id=? AND revision=? AND status='confirmed'",
                            (job_id, r["base_revision"])).fetchone()
                        spec_cache[r["base_revision"]] = \
                            json.loads(br["spec"]) if br else {}
                out.append({"revision": r["revision"],
                            "base_revision": r["base_revision"],
                            "confirmed_at": r["confirmed_at"],
                            "spec": json.loads(r["spec"]),
                            "base_spec": spec_cache[r["base_revision"]]})
        return out

    def analyze_job(job_id):
        job = load_job(job_id)
        rolls = load_rolls(job_id)
        events = load_events(job_id)
        state, violations = core.analyze(
            job, rolls, events, genealogy=evaluate_materials(job_id),
            defects_context={
                "confirmed_revisions": _confirmed_revision_context(job_id),
                "locked_ply_ids": _locked_ply_ids(job_id)})
        return job, rolls, events, state, violations

    # ------------------------------------------------------------ 处理器
    def h_create_job(ctx):
        b = ctx["body"]
        missing = [k for k in ("name", "tool_datum", "zones", "spec") if k not in b]
        if missing:
            raise ApiError(400, {"error": "missing_fields", "fields": missing})
        if not isinstance(b["spec"].get("plies"), list) or not b["spec"]["plies"]:
            raise ApiError(400, {"error": "invalid_spec",
                                 "message": "spec.plies 必须是非空列表"})
        for z in b["zones"]:
            if "zone_id" not in z or "polygon" not in z:
                raise ApiError(400, {"error": "invalid_zone",
                                     "message": "每个分区需要 zone_id 与 polygon"})
        job_id = uuid.uuid4().hex[:12]
        now = core.utcnow()
        with store.db() as conn:
            conn.execute(
                "INSERT INTO jobs(id,name,tool_datum,zones,spec,status,created_at)"
                " VALUES(?,?,?,?,?,'open',?)",
                (job_id, b["name"], json.dumps(b["tool_datum"], ensure_ascii=False),
                 json.dumps(b["zones"], ensure_ascii=False),
                 json.dumps(b["spec"], ensure_ascii=False), now))
            for r in b.get("rolls") or []:
                _insert_roll(conn, job_id, r, now)
        return 201, {"job_id": job_id, "status": "open"}

    def _insert_roll(conn, job_id, r, now):
        missing = [k for k in ("roll_id", "batch_no", "material") if k not in r]
        if missing:
            raise ApiError(400, {"error": "missing_roll_fields", "fields": missing})
        conn.execute(
            "INSERT INTO rolls(job_id,roll_id,batch_no,material,out_time_limit_h,"
            "created_at) VALUES(?,?,?,?,?,?)",
            (job_id, r["roll_id"], r["batch_no"], r["material"],
             r.get("out_time_limit_h"), now))

    def h_list_jobs(ctx):
        with store.db() as conn:
            rows = conn.execute(
                "SELECT id,name,status,created_at FROM jobs ORDER BY created_at"
            ).fetchall()
        return 200, {"jobs": [dict(r) for r in rows]}

    def h_get_job(ctx, job_id):
        job = load_job(job_id)
        job["rolls"] = list(load_rolls(job_id).values())
        return 200, job

    def h_add_roll(ctx, job_id):
        load_job(job_id)
        with store.db() as conn:
            _insert_roll(conn, job_id, ctx["body"], core.utcnow())
        return 201, {"roll_id": ctx["body"]["roll_id"]}

    def h_append_events(ctx, job_id):
        job = load_job(job_id)
        b = ctx["body"]
        items = b.get("events") if isinstance(b, dict) and "events" in b else [b]
        if not isinstance(items, list) or not items:
            raise ApiError(400, {"error": "no_events"})
        now = core.utcnow()
        seqs = []
        with store.db() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq),0) AS m FROM events WHERE job_id=?",
                (job_id,)).fetchone()
            seq = row["m"]
            for item in items:
                if not isinstance(item, dict) or item.get("type") not in core.EVENT_TYPES:
                    raise ApiError(400, {
                        "error": "invalid_event",
                        "message": f"事件类型须为 {sorted(core.EVENT_TYPES)}",
                        "got": item.get("type") if isinstance(item, dict) else None})
                if item["type"] == "ply_replaced" and (
                        "removed_ply_id" not in item or "replacement" not in item):
                    raise ApiError(400, {
                        "error": "invalid_rework",
                        "message": "返工事件必须同时给出 removed_ply_id 与 replacement"})
                if item["type"] in _comp.STAGE_EVENTS:
                    _validate_compaction_item(item)
                if item["type"] in _env.ENV_EVENT_TYPES:
                    msg = _env.validate_event_item(item)
                    if msg:
                        raise ApiError(400, {
                            "error": "invalid_environment_event",
                            "message": msg, "type": item["type"]})
                if item["type"] in _def.DEFECT_EVENT_TYPES:
                    msg = _def.validate_event_item(item)
                    if msg:
                        raise ApiError(400, {
                            "error": "invalid_defect_event",
                            "message": msg, "type": item["type"]})
                seq += 1
                payload = {k: v for k, v in item.items()
                           if k not in ("type", "operator")}
                conn.execute(  # 仅 INSERT：铺放记录只允许追加
                    "INSERT INTO events(job_id,seq,type,payload,operator,recorded_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (job_id, seq, item["type"],
                     json.dumps(payload, ensure_ascii=False),
                     item.get("operator"), now))
                seqs.append(seq)
            if job["status"] == "approved":
                # 批准版已冻结；新事件使工单回到待放行状态，旧快照不受影响
                conn.execute("UPDATE jobs SET status='open' WHERE id=?", (job_id,))
        return 201, {"seqs": seqs}

    def h_list_events(ctx, job_id):
        load_job(job_id)
        return 200, {"events": load_events(job_id)}

    def h_state(ctx, job_id):
        _job, _rolls, _events, state, _v = analyze_job(job_id)
        return 200, state

    def h_validate(ctx, job_id):
        _job, _rolls, _events, state, violations = analyze_job(job_id)
        return 200, {
            "job_id": job_id,
            "release": "rejected" if violations else "ok",
            "violation_count": len(violations),
            "violations": violations,
            "state_summary": {
                "ply_count": state["ply_count"],
                "zones": state["zones"],
                "rolls": state["rolls"],
                "compaction": {
                    "checkpoints": [{
                        "checkpoint_id": c["checkpoint_id"],
                        "status": c["status"],
                        "after_seq": c["after_seq"],
                        "zones": c["zones"],
                    } for c in state["compaction"]["checkpoints"]],
                },
                "environment": {
                    "enabled": state["environment"].get("enabled", False),
                    "event_count": len(state["environment"].get("events") or []),
                    "ambient_count": len(
                        state["environment"].get("ambient_series") or []),
                    "material_temp_count": len(
                        state["environment"].get("material_temp_series") or []),
                    "material_intervals": len(
                        state["environment"].get("material_intervals") or []),
                    "surface_intervals": len(
                        state["environment"].get("surface_intervals") or []),
                    "decisions": state["environment"].get("decisions") or [],
                },
                "repairs": {
                    "enabled": state["repairs"].get("enabled", False),
                    "instruction_version":
                        state["repairs"].get("instruction_version"),
                    "defect_count": len(
                        state["repairs"].get("defects") or []),
                    "open_count": sum(
                        1 for d in state["repairs"].get("defects") or []
                        if d.get("status") != "signed"),
                    "defects": [{"defect_id": d["defect_id"],
                                 "type": d["type"],
                                 "source_ply": d["source_ply"],
                                 "zone": d["zone"],
                                 "status": d["status"],
                                 "generation": d["current_generation"]}
                                for d in state["repairs"].get("defects") or []],
                },
            },
        }

    def h_approve(ctx, job_id):
        approved_by = (ctx["body"] or {}).get("approved_by")
        if not approved_by:
            raise ApiError(400, {"error": "missing_fields", "fields": ["approved_by"]})
        job, rolls, events, state, violations = analyze_job(job_id)
        if violations:
            raise ApiError(409, {
                "error": "release_rejected",
                "message": "存在未闭环违规，拒绝放行",
                "violation_count": len(violations),
                "violations": violations})
        snapshot = core.build_snapshot(job, rolls, events, state)
        with store.db() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM approvals WHERE job_id=?",
                (job_id,)).fetchone()
            version = row["v"] + 1
            snapshot["version"] = version
            conn.execute(
                "INSERT INTO approvals(job_id,version,snapshot,chain_hash,"
                "approved_by,approved_at) VALUES(?,?,?,?,?,?)",
                (job_id, version, json.dumps(snapshot, ensure_ascii=False),
                 snapshot["chain_hash"], approved_by, core.utcnow()))
            conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (job_id,))
        return 201, {"job_id": job_id, "version": version,
                     "chain_hash": snapshot["chain_hash"],
                     "spec_hash": snapshot["spec_hash"]}

    def _load_snapshot(job_id, version):
        with store.db() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE job_id=? AND version=?",
                (job_id, version)).fetchone()
        if row is None:
            raise ApiError(404, {"error": "approval_not_found",
                                 "job_id": job_id, "version": version})
        return dict(row), json.loads(row["snapshot"])

    def h_list_approvals(ctx, job_id):
        load_job(job_id)
        with store.db() as conn:
            rows = conn.execute(
                "SELECT version,chain_hash,approved_by,approved_at FROM approvals"
                " WHERE job_id=? ORDER BY version", (job_id,)).fetchall()
        return 200, {"approvals": [dict(r) for r in rows]}

    def h_package(ctx, job_id, version):
        load_job(job_id)
        meta, snapshot = _load_snapshot(job_id, int(version))
        snapshot["approved_by"] = meta["approved_by"]
        snapshot["approved_at"] = meta["approved_at"]
        return 200, snapshot  # 随件包完全取自冻结快照

    def h_diff(ctx, job_id):
        load_job(job_id)
        q = parse_qs(ctx["query"])
        try:
            a, b = int(q["a"][0]), int(q["b"][0])
        except (KeyError, IndexError, ValueError):
            raise ApiError(400, {"error": "missing_query",
                                 "message": "需要查询参数 a 与 b（批准版号）"})
        _ma, sa = _load_snapshot(job_id, a)
        _mb, sb = _load_snapshot(job_id, b)
        return 200, {"job_id": job_id, "from": a, "to": b,
                     "diff": core.diff_snapshots(sa, sb)}

    # ------------------------------------------------------------ 规范换版
    def _revision_spec(job_id, rev_no):
        """指定版本规范全文：0 = 建档规范；>0 = 已确认换版（proposed 不作基线）。"""
        with store.db() as conn:
            if rev_no == 0:
                row = conn.execute("SELECT spec FROM jobs WHERE id=?",
                                   (job_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT spec FROM spec_revisions"
                    " WHERE job_id=? AND revision=? AND status='confirmed'",
                    (job_id, rev_no)).fetchone()
        return json.loads(row["spec"]) if row else None

    def _locked_ply_ids(job_id):
        """最近批准快照冻结的实铺层号（换版返工不得揭除）。"""
        with store.db() as conn:
            row = conn.execute(
                "SELECT snapshot FROM approvals WHERE job_id=?"
                " ORDER BY version DESC LIMIT 1", (job_id,)).fetchone()
        if row is None:
            return set()
        snap = json.loads(row["snapshot"])
        return {p["ply_id"]
                for p in (snap.get("state") or {}).get("plies") or []}

    def h_propose_revision(ctx, job_id):
        job = load_job(job_id)
        b = ctx["body"]
        new_spec = b.get("spec")
        if not isinstance(new_spec, dict) \
                or not isinstance(new_spec.get("plies"), list) \
                or not new_spec["plies"]:
            raise ApiError(400, {"error": "invalid_spec",
                                 "message": "spec.plies 必须是非空列表"})
        for sp in new_spec["plies"]:
            if not isinstance(sp, dict) or not sp.get("ply_id") \
                    or not isinstance(sp.get("seq"), int) \
                    or isinstance(sp.get("seq"), bool):
                raise ApiError(400, {
                    "error": "invalid_spec",
                    "message": "每个规范层需要 ply_id 与整数 seq"})
        reason = b.get("reason")
        if not reason or not isinstance(reason, str):
            raise ApiError(400, {"error": "missing_fields", "fields": ["reason"]})
        effective_at = core.parse_time(b.get("effective_at")) \
            if b.get("effective_at") else None
        if effective_at is None:
            raise ApiError(400, {"error": "invalid_effective_at",
                                 "message": "effective_at 缺失或无法解析"})
        base = b.get("base_revision", job["spec_revision"])
        if not isinstance(base, int) or isinstance(base, bool) or base < 0:
            raise ApiError(400, {"error": "invalid_base_revision",
                                 "message": "base_revision 必须是非负整数"})
        base_spec = _revision_spec(job_id, base)
        if base_spec is None:
            raise ApiError(404, {"error": "base_revision_not_found",
                                 "base_revision": base})
        explicit = b.get("mapping")
        if explicit is not None:
            if not isinstance(explicit, dict) or not all(
                    isinstance(k, str) and isinstance(v, str)
                    for k, v in explicit.items()):
                raise ApiError(400, {
                    "error": "invalid_mapping",
                    "message": "mapping 必须是 {新层号: 旧层号} 的字典"})
            new_ids = {sp["ply_id"] for sp in new_spec["plies"]}
            old_ids = {sp.get("ply_id")
                       for sp in (base_spec or {}).get("plies") or []}
            for k, v in explicit.items():
                if k not in new_ids:
                    raise ApiError(400, {"error": "invalid_mapping",
                                         "message": f"映射目标 {k} 不在新规范中"})
                if v not in old_ids:
                    raise ApiError(400, {"error": "invalid_mapping",
                                         "message": f"映射来源 {v} 不在基线规范中"})
        impact, conflicts = _rev.analyze(
            job, load_events(job_id), base_spec, new_spec, effective_at,
            locked_plies=_locked_ply_ids(job_id), explicit_mapping=explicit,
            new_zones=b.get("zones"), new_tool_datum=b.get("tool_datum"),
            rolls=load_rolls(job_id))
        if conflicts:
            raise ApiError(409, {
                "error": "spec_revision_conflict",
                "message": "规范换版存在冲突，未启用新版",
                "conflicts": conflicts, "impact": impact})
        now = core.utcnow()
        with store.db() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(revision),0) AS m FROM spec_revisions"
                " WHERE job_id=?", (job_id,)).fetchone()
            rev_no = row["m"] + 1
            conn.execute(  # 规范/映射/处置决定写入即固定，confirm 不再改
                "INSERT INTO spec_revisions(job_id,revision,base_revision,spec,"
                "reason,effective_at,impact,status,created_at)"
                " VALUES(?,?,?,?,?,?,?,'proposed',?)",
                (job_id, rev_no, base, json.dumps(new_spec, ensure_ascii=False),
                 reason, b["effective_at"],
                 json.dumps(impact, ensure_ascii=False), now))
        return 201, {"job_id": job_id, "revision": rev_no,
                     "base_revision": base, "status": "proposed",
                     "spec_hash": _rev.spec_hash(new_spec), "impact": impact}

    def h_list_revisions(ctx, job_id):
        load_job(job_id)
        with store.db() as conn:
            rows = conn.execute(
                "SELECT revision,base_revision,reason,effective_at,status,"
                "created_at,confirmed_at FROM spec_revisions WHERE job_id=?"
                " ORDER BY revision", (job_id,)).fetchall()
        return 200, {"revisions": [dict(r) for r in rows]}

    def _load_revision(job_id, rev_no):
        with store.db() as conn:
            row = conn.execute(
                "SELECT * FROM spec_revisions WHERE job_id=? AND revision=?",
                (job_id, rev_no)).fetchone()
        if row is None:
            raise ApiError(404, {"error": "revision_not_found",
                                 "job_id": job_id, "revision": rev_no})
        return row

    def h_get_revision(ctx, job_id, rev):
        load_job(job_id)
        row = _load_revision(job_id, int(rev))
        out = dict(row)
        out["spec"] = json.loads(out["spec"])
        out["impact"] = json.loads(out["impact"])
        return 200, out

    def h_confirm_revision(ctx, job_id, rev):
        job = load_job(job_id)
        rev_no = int(rev)
        row = _load_revision(job_id, rev_no)
        if row["status"] == "confirmed":
            raise ApiError(409, {"error": "already_confirmed",
                                 "revision": rev_no})
        if row["base_revision"] != job["spec_revision"]:
            raise ApiError(409, {
                "error": "stale_base",
                "message": f"该换版派生自规范 v{row['base_revision']}，但当前"
                           f"生效为 v{job['spec_revision']}，需重新提议",
                "base_revision": row["base_revision"],
                "current_revision": job["spec_revision"]})
        # 提议后现场可能已变（新铺层/新批准）：按当前事件链与最新锁层
        # 状态重算影响；仍有冲突则不得启用——修订保持 proposed，
        # spec_revision 不切换
        stored = json.loads(row["impact"])
        _impact, conflicts = _rev.analyze(
            job, load_events(job_id),
            _revision_spec(job_id, row["base_revision"]),
            json.loads(row["spec"]), core.parse_time(row["effective_at"]),
            locked_plies=_locked_ply_ids(job_id),
            explicit_mapping=stored.get("mapping") or None,
            rolls=load_rolls(job_id))
        if conflicts:
            raise ApiError(409, {
                "error": "spec_revision_conflict",
                "message": "提议后现场状态已变化，换版存在冲突，未启用新版",
                "conflicts": conflicts, "impact": _impact})
        with store.db() as conn:
            conn.execute(
                "UPDATE spec_revisions SET status='confirmed', confirmed_at=?"
                " WHERE job_id=? AND revision=?",
                (core.utcnow(), job_id, rev_no))
            if job["status"] == "approved":
                # 规范分支已切换，原批准失效，需按新规范重新放行
                conn.execute("UPDATE jobs SET status='open' WHERE id=?",
                             (job_id,))
        return 200, {"job_id": job_id, "revision": rev_no,
                     "status": "confirmed",
                     "spec_hash": _rev.spec_hash(json.loads(row["spec"]))}

    # ------------------------------------------------------------ 材料谱系
    def _insert_material_event(conn, item, now):
        """材料事件只 INSERT；返回全局 seq。"""
        payload = {k: v for k, v in item.items()
                   if k not in ("type", "operator")}
        cur = conn.execute(
            "INSERT INTO material_events(type,payload,operator,recorded_at)"
            " VALUES(?,?,?,?)",
            (item["type"], json.dumps(payload, ensure_ascii=False),
             item.get("operator"), now))
        return cur.lastrowid

    def h_create_material_unit(ctx):
        """登记整卷：制造/失效日期、计量单位与初始数量（落 unit_registered 事件）。"""
        b = ctx["body"]
        missing = [k for k in ("unit_id", "batch_no", "material", "unit",
                               "initial_qty") if k not in b]
        if missing:
            raise ApiError(400, {"error": "missing_fields", "fields": missing})
        qty = b.get("initial_qty")
        if isinstance(qty, bool) or not isinstance(qty, (int, float)) or qty <= 0:
            raise ApiError(400, {"error": "invalid_material_unit",
                                 "message": "initial_qty 必须是正数"})
        for k in ("manufactured_at", "expires_at"):
            if b.get(k) is not None and core.parse_time(b[k]) is None:
                raise ApiError(400, {"error": "invalid_material_unit",
                                     "message": f"{k}={b[k]!r} 无法解析"})
        if b["unit_id"] in _gen.known_unit_ids(load_material_events()):
            raise ApiError(409, {"error": "unit_exists",
                                 "unit_id": b["unit_id"]})
        item = {"type": "unit_registered", "operator": ctx["body"].get("operator"),
                **{k: b[k] for k in (
                    "unit_id", "batch_no", "material", "unit", "initial_qty",
                    "manufactured_at", "expires_at", "out_time_limit_h")
                    if k in b}}
        with store.db() as conn:
            seq = _insert_material_event(conn, item, core.utcnow())
        return 201, {"unit_id": b["unit_id"], "seq": seq}

    def _validate_material_event(item, known):
        """材料事件入链前的最小校验；业务规则（超额/成环/闭合）在分析阶段判定。"""
        t = item.get("type")
        if t not in _gen.EVENT_TYPES:
            raise ApiError(400, {
                "error": "invalid_material_event",
                "message": f"材料事件类型须为 {sorted(_gen.EVENT_TYPES)}",
                "type": t})
        at = item.get("at")
        if at is not None and core.parse_time(at) is None:
            raise ApiError(400, {"error": "invalid_material_event",
                                 "message": f"at={at!r} 无法解析", "type": t})
        if t in ("unit_cut", "unit_split"):
            if item.get("parent") not in known:
                raise ApiError(400, {"error": "invalid_material_event",
                                     "message": f"父单元 {item.get('parent')} 未登记",
                                     "type": t})
            children = item.get("children")
            if not isinstance(children, list) or not children:
                raise ApiError(400, {"error": "invalid_material_event",
                                     "message": f"{t} 需要非空 children 列表",
                                     "type": t})
            seen = set()
            for c in children:
                if not isinstance(c, dict) or not c.get("unit_id"):
                    raise ApiError(400, {"error": "invalid_material_event",
                                         "message": "每个子单元需要 unit_id",
                                         "type": t})
                if c["unit_id"] in known or c["unit_id"] in seen:
                    raise ApiError(400, {"error": "invalid_material_event",
                                         "message": f"材料单元标识 "
                                         f"{c['unit_id']} 已被占用",
                                         "type": t})
                q = c.get("qty")
                if isinstance(q, bool) or not isinstance(q, (int, float)) or q <= 0:
                    raise ApiError(400, {"error": "invalid_material_event",
                                         "message": f"子单元 {c['unit_id']} 的 qty "
                                         f"必须是正数", "type": t})
                seen.add(c["unit_id"])
            known.update(seen)  # 同批后续事件可引用本批生成的子单元
        elif t == "unit_registered":
            uid = item.get("unit_id")
            if not uid:
                raise ApiError(400, {"error": "invalid_material_event",
                                     "message": "unit_registered 需要 unit_id",
                                     "type": t})
            if uid in known:
                raise ApiError(400, {"error": "invalid_material_event",
                                     "message": f"材料单元标识 {uid} 已被占用",
                                     "type": t})
            known.add(uid)
        else:
            if item.get("unit_id") not in known:
                raise ApiError(400, {"error": "invalid_material_event",
                                     "message": f"材料单元 {item.get('unit_id')} "
                                     f"未登记", "type": t})
            if t == "unit_rebind" and not item.get("parent"):
                raise ApiError(400, {"error": "invalid_material_event",
                                     "message": "unit_rebind 需要 parent", "type": t})

    def h_append_material_events(ctx):
        b = ctx["body"]
        items = b.get("events") if isinstance(b, dict) and "events" in b else [b]
        if not isinstance(items, list) or not items \
                or not all(isinstance(i, dict) for i in items):
            raise ApiError(400, {"error": "no_events"})
        known = _gen.known_unit_ids(load_material_events())
        for item in items:
            _validate_material_event(item, known)
        now = core.utcnow()
        with store.db() as conn:
            seqs = [_insert_material_event(conn, item, now) for item in items]
        # 材料事件变动后，仅刷新引用该谱系的工单
        touched = set()
        for item in items:
            if item["type"] in ("unit_cut", "unit_split"):
                touched.add(item["parent"])
                touched.update(c["unit_id"] for c in item["children"])
            else:
                touched.add(item.get("unit_id"))
        affected = _gen.affected_jobs(load_material_events(), load_unit_usage(),
                                      touched)
        return 201, {"seqs": seqs, "affected_jobs": affected}

    def h_list_material_events(ctx):
        return 200, {"events": load_material_events()}

    def _materials_view():
        return _gen.evaluate(load_material_events(), load_unit_usage())

    def h_list_material_units(ctx):
        result = _materials_view()
        return 200, {"units": list(result["state"]["units"].values())}

    def h_get_material_unit(ctx, uid):
        result = _materials_view()
        u = result["state"]["units"].get(uid)
        if u is None:
            raise ApiError(404, {"error": "unit_not_found", "unit_id": uid})
        return 200, {
            **u,
            "edges": [e for e in result["state"]["edges"]
                      if e["child"] == uid or e["parent"] == uid],
            "usage": [r for r in result["state"]["usage"] if r["unit"] == uid],
            "violations": [v for v in result["violations"]
                           if uid in v["details"].get("units", [])],
        }

    def h_material_impact(ctx, uid):
        mat_events = load_material_events()
        if uid not in _gen.known_unit_ids(mat_events):
            raise ApiError(404, {"error": "unit_not_found", "unit_id": uid})
        jobs = _gen.affected_jobs(mat_events, load_unit_usage(), {uid})
        out = []
        for jid in jobs:
            try:
                _j, _r, _e, _s, violations = analyze_job(jid)
            except ApiError:
                continue
            out.append({"job_id": jid,
                        "release": "rejected" if violations else "ok",
                        "violation_count": len(violations)})
        return 200, {"unit_id": uid, "affected_jobs": out}

    # ------------------------------------------------------------ 局部缺陷处置
    def h_list_defects(ctx, job_id):
        _job, _rolls, _events, state, _violations = analyze_job(job_id)
        rep = state.get("repairs") or {}
        return 200, {
            "job_id": job_id,
            "instruction_version": rep.get("instruction_version"),
            "openings": rep.get("openings") or [],
            "defects": [{"defect_id": d["defect_id"], "type": d["type"],
                         "source_ply": d["source_ply"], "zone": d["zone"],
                         "status": d["status"],
                         "generation": d["current_generation"],
                         "instruction": d["instruction"],
                         "disposition": d["disposition"],
                         "found_event": d["found_event"]}
                        for d in rep.get("defects") or []]}

    def h_get_defect(ctx, job_id, defect_id):
        _job, _rolls, _events, state, violations = analyze_job(job_id)
        d = next((x for x in (state.get("repairs") or {}).get("defects") or []
                  if x["defect_id"] == defect_id), None)
        if d is None:
            raise ApiError(404, {"error": "defect_not_found",
                                 "job_id": job_id, "defect_id": defect_id})
        return 200, {
            **d,
            "violations": [x for x in violations
                           if defect_id in (x.get("details") or {})
                           .get("defects", [])]}

    # ------------------------------------------------------------ 路由表
    routes = [        ("POST", r"^/jobs$", h_create_job),
        ("GET", r"^/jobs$", h_list_jobs),
        ("GET", r"^/jobs/([^/]+)$", h_get_job),
        ("POST", r"^/jobs/([^/]+)/rolls$", h_add_roll),
        ("POST", r"^/jobs/([^/]+)/events$", h_append_events),
        ("GET", r"^/jobs/([^/]+)/events$", h_list_events),
        ("GET", r"^/jobs/([^/]+)/state$", h_state),
        ("GET", r"^/jobs/([^/]+)/validate$", h_validate),
        ("POST", r"^/jobs/([^/]+)/approve$", h_approve),
        ("GET", r"^/jobs/([^/]+)/approvals$", h_list_approvals),
        ("GET", r"^/jobs/([^/]+)/approvals/diff$", h_diff),
        ("GET", r"^/jobs/([^/]+)/approvals/(\d+)/package$", h_package),
        ("POST", r"^/jobs/([^/]+)/spec-revisions$", h_propose_revision),
        ("GET", r"^/jobs/([^/]+)/spec-revisions$", h_list_revisions),
        ("GET", r"^/jobs/([^/]+)/spec-revisions/(\d+)$", h_get_revision),
        ("POST", r"^/jobs/([^/]+)/spec-revisions/(\d+)/confirm$",
         h_confirm_revision),
        ("GET", r"^/jobs/([^/]+)/defects$", h_list_defects),
        ("GET", r"^/jobs/([^/]+)/defects/([^/]+)$", h_get_defect),
        ("POST", r"^/materials/units$", h_create_material_unit),
        ("GET", r"^/materials/units$", h_list_material_units),
        ("GET", r"^/materials/units/([^/]+)$", h_get_material_unit),
        ("GET", r"^/materials/units/([^/]+)/impact$", h_material_impact),
        ("POST", r"^/materials/events$", h_append_material_events),
        ("GET", r"^/materials/events$", h_list_material_events),
    ]

    def app(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        raw = environ["wsgi.input"].read(length) if length else b""
        body = None
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return _respond(start_response, 400, {"error": "invalid_json"})
        ctx = {"body": body or {}, "query": environ.get("QUERY_STRING", "")}
        matched_path = False
        try:
            for m, pattern, handler in routes:
                match = re.match(pattern, path)
                if match:
                    if m == method:
                        status, payload = handler(ctx, *match.groups())
                        return _respond(start_response, status, payload)
                    matched_path = True
            if matched_path:
                return _respond(start_response, 405, {"error": "method_not_allowed"})
            return _respond(start_response, 404, {"error": "not_found"})
        except ApiError as e:
            return _respond(start_response, e.status, e.payload)
        except Exception as e:  # 兜底，避免泄露堆栈
            return _respond(start_response, 500, {"error": "internal_error",
                                                  "message": str(e)})

    def _respond(start_response, status, payload):
        data = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
        start_response(_STATUS[status], [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(data))),
        ])
        return [data]

    return app
