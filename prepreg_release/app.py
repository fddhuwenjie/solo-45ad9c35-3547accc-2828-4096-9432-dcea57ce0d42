"""WSGI 路由层：REST API 定义。

接口一览：
  POST /jobs                              建档（模具基准/分区/铺层规范/料卷批号）
  GET  /jobs                              列表
  GET  /jobs/{id}                         详情
  POST /jobs/{id}/rolls                   登记料卷
  POST /jobs/{id}/events                  追加铺放事件（只允许追加）
  GET  /jobs/{id}/events                  事件链
  GET  /jobs/{id}/state                   重建的分区覆盖/厚度/外置时间
  GET  /jobs/{id}/validate                放行规则校验（违规含层号与区域）
  POST /jobs/{id}/approve                 批准放行（冻结快照）
  GET  /jobs/{id}/approvals               批准版列表
  GET  /jobs/{id}/approvals/{v}/package   JSON 随件包（取自冻结快照）
  GET  /jobs/{id}/approvals/diff?a=&b=    版本比较（取自冻结快照）
"""

import json
import re
import uuid
from urllib.parse import parse_qs

from . import core
from .store import Store

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
        return {
            "id": row["id"], "name": row["name"], "status": row["status"],
            "tool_datum": json.loads(row["tool_datum"]),
            "zones": json.loads(row["zones"]),
            "spec": json.loads(row["spec"]),
            "created_at": row["created_at"],
        }

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

    def analyze_job(job_id):
        job = load_job(job_id)
        rolls = load_rolls(job_id)
        events = load_events(job_id)
        state, violations = core.analyze(job, rolls, events)
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

    # ------------------------------------------------------------ 路由表
    routes = [
        ("POST", r"^/jobs$", h_create_job),
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
