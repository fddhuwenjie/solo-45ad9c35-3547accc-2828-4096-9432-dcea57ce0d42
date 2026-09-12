#!/usr/bin/env python3
"""端到端演示：不依赖网络，直接以 WSGI 调用走完整放行流程。

场景：
  1. 建档：模具基准、两个相邻分区、6 层对称均衡规范、料卷批号
  2. 追加铺放事件，故意引入：方向抄错（P04）、接缝错开不足（P02/P03）
  3. 校验 → 拒绝放行，违规带具体层号与区域
  4. 返工闭环（揭除层 ↔ 替代层），再校验 → 通过
  5. 批准 v1；追加新事件后批准 v2；比较两版；导出 JSON 随件包
"""

import io
import json
import os
import tempfile

from prepreg_release import make_app

DB = os.path.join(tempfile.gettempdir(), "prepreg_demo.db")
if os.path.exists(DB):
    os.remove(DB)
app = make_app(DB)


def call(method, path, body=None, query=""):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status

    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(data)), "wsgi.input": io.BytesIO(data),
    }
    payload = json.loads(b"".join(app(environ, start_response)))
    return captured["status"], payload


def show(title, status, payload, keys=None):
    print(f"\n=== {title} [{status}] ===")
    out = payload
    if keys:
        out = {k: payload[k] for k in keys if k in payload}
    print(json.dumps(out, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- 1. 建档
spec = {
    "materials": {"CF-EP-3K": {"ply_thickness": 0.125}},
    "plies": [
        {"seq": 1, "ply_id": "P01", "material": "CF-EP-3K", "angle": 0,
         "face": "up", "zones": ["Z1", "Z2"]},
        {"seq": 2, "ply_id": "P02", "material": "CF-EP-3K", "angle": 45,
         "face": "up", "zones": ["Z1", "Z2"]},
        {"seq": 3, "ply_id": "P03", "material": "CF-EP-3K", "angle": -45,
         "face": "up", "zones": ["Z1", "Z2"]},
        {"seq": 4, "ply_id": "P04", "material": "CF-EP-3K", "angle": -45,
         "face": "up", "zones": ["Z1", "Z2"]},
        {"seq": 5, "ply_id": "P05", "material": "CF-EP-3K", "angle": 45,
         "face": "up", "zones": ["Z1", "Z2"]},
        {"seq": 6, "ply_id": "P06", "material": "CF-EP-3K", "angle": 0,
         "face": "up", "zones": ["Z1", "Z2"]},
    ],
    "rules": {"angle_tolerance_deg": 3.0, "seam_min_stagger_mm": 25.0,
              "seam_max_gap_mm": 1.5, "max_consecutive_same_angle": 4},
}
job = {
    "name": "机翼蒙皮-A1",
    "tool_datum": {"datum_id": "MOLD-A1", "origin": [0, 0, 0],
                   "x_axis": [1, 0, 0], "units": "mm"},
    "zones": [
        {"zone_id": "Z1", "polygon": [[0, 0], [200, 0], [200, 60], [0, 60]],
         "adjacent": ["Z2"]},
        {"zone_id": "Z2", "polygon": [[200, 0], [400, 0], [400, 60], [200, 60]],
         "adjacent": ["Z1"]},
    ],
    "spec": spec,
    "rolls": [{"roll_id": "R1", "batch_no": "B2026-0901", "material": "CF-EP-3K",
               "out_time_limit_h": 240}],
}
status, r = call("POST", "/jobs", job)
show("建档", status, r)
jid = r["job_id"]

# ---------------------------------------------------------------- 2. 铺放
FULL = [[-1, -1], [401, -1], [401, 61], [-1, 61]]  # 覆盖 Z1+Z2
T0 = "2026-09-10T06:00:00Z"


def placed(pid, angle, t, seams=None):
    e = {"type": "ply_placed", "operator": "op-zhang", "ply_id": pid,
         "roll": "R1", "angle": angle, "face": "up",
         "geometry": FULL, "placed_at": t}
    if seams:
        e["seams"] = seams
    return e


events = [
    {"type": "roll_thawed", "operator": "op-li", "roll": "R1", "at": T0},
    placed("P01", 0, "2026-09-10T08:00:00Z"),
    placed("P02", 45, "2026-09-10T08:30:00Z",
           seams=[{"zone": "Z1", "axis": "x", "at": 100.0, "gap": 0.6}]),
    placed("P03", -45, "2026-09-10T09:00:00Z",
           seams=[{"zone": "Z1", "axis": "x", "at": 102.0, "gap": 0.4}]),
    placed("P04", 45, "2026-09-10T09:30:00Z"),   # ← 方向抄错：规范为 -45
    placed("P05", 45, "2026-09-10T10:00:00Z"),
    placed("P06", 0, "2026-09-10T10:30:00Z"),
]
status, r = call("POST", f"/jobs/{jid}/events", {"events": events})
show("追加铺放事件（含两处错误）", status, r)

# ---------------------------------------------------------------- 3. 校验拒绝
status, r = call("GET", f"/jobs/{jid}/validate")
show("校验：方向错位 + 接缝错开不足 + 对称/平衡破坏", status, r,
     keys=["release", "violation_count", "violations"])

status, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
show("尝试批准 → 拒绝放行", status, r, keys=["error", "violation_count"])

# ---------------------------------------------------------------- 4. 返工闭环
rework = [
    {"type": "ply_removed", "operator": "op-zhang", "ply_id": "P04",
     "reason": "方向抄错"},
    {"type": "ply_replaced", "operator": "op-zhang", "removed_ply_id": "P04",
     "replacement": {"ply_id": "P04", "roll": "R1", "angle": -45, "face": "up",
                     "geometry": FULL, "placed_at": "2026-09-10T11:00:00Z"}},
    {"type": "ply_removed", "operator": "op-zhang", "ply_id": "P03",
     "reason": "接缝错开不足"},
    {"type": "ply_replaced", "operator": "op-zhang", "removed_ply_id": "P03",
     "replacement": {"ply_id": "P03", "roll": "R1", "angle": -45, "face": "up",
                     "geometry": FULL, "placed_at": "2026-09-10T11:30:00Z",
                     "seams": [{"zone": "Z1", "axis": "x", "at": 140.0,
                                "gap": 0.5}]}},
]
status, r = call("POST", f"/jobs/{jid}/events", {"events": rework})
show("返工：揭除层与替代层串接", status, r)

status, r = call("GET", f"/jobs/{jid}/validate")
show("复检", status, r, keys=["release", "violation_count", "state_summary"])

# ---------------------------------------------------------------- 5. 批准与版本
status, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
show("批准 v1（冻结规范/批次/事件链）", status, r)

status, r = call("POST", f"/jobs/{jid}/events",
                 {"type": "note", "operator": "qe-wang",
                  "text": "无损检测复査通过，补记"})
show("批准后追加事件（工单回到待放行，旧快照不变）", status, r)

status, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
show("批准 v2", status, r)

status, r = call("GET", f"/jobs/{jid}/approvals/diff", query="a=1&b=2")
show("版本比较 v1→v2（取自冻结快照）", status, r)

status, r = call("GET", f"/jobs/{jid}/approvals/2/package")
show("JSON 随件包（v2 快照）", status, r,
     keys=["package", "version", "spec_hash", "chain_hash", "material_batches",
           "state"])
