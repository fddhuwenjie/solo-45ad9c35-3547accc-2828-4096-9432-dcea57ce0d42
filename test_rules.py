#!/usr/bin/env python3
"""规则覆盖测试：每类违规构造最小场景并断言规则码。"""

import io
import json
import os
import tempfile

from prepreg_release import make_app

DB = os.path.join(tempfile.gettempdir(), "prepreg_test.db")
if os.path.exists(DB):
    os.remove(DB)
app = make_app(DB)

FULL = [[-1, -1], [401, -1], [401, 61], [-1, 61]]
HALF = [[-1, -1], [150, -1], [150, 61], [-1, 61]]  # 只覆盖 Z1 的一部分


def call(method, path, body=None, query=""):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
    cap = {}

    def sr(status, headers, exc_info=None):
        cap["status"] = status

    env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
           "CONTENT_LENGTH": str(len(data)), "wsgi.input": io.BytesIO(data)}
    payload = json.loads(b"".join(app(env, sr)))
    return cap["status"], payload


def new_job(angles=(0, 45, -45, -45, 45, 0), extra_rules=None, drops=None,
            roll_limit=240, omit_face_for=None, dup_seq=False, datum="default"):
    plies = []
    for i, a in enumerate(angles, 1):
        p = {"seq": i, "ply_id": f"P{i:02d}", "material": "CF-EP-3K",
             "angle": a, "face": "up", "zones": ["Z1", "Z2"]}
        if omit_face_for and p["ply_id"] in omit_face_for:
            del p["face"]
        if drops and p["ply_id"] in drops:
            p["drop_at"] = drops[p["ply_id"]]
        plies.append(p)
    if dup_seq:
        plies[3]["seq"] = plies[2]["seq"]   # P04 与 P03 层序重复
    rules = {"seam_min_stagger_mm": 25.0, "seam_max_gap_mm": 1.5,
             "max_consecutive_same_angle": 4}
    if extra_rules:
        rules.update(extra_rules)
    body = {
        "name": "test",
        "tool_datum": {"datum_id": "M1"} if datum == "default" else datum,
        "zones": [
            {"zone_id": "Z1", "polygon": [[0, 0], [200, 0], [200, 60], [0, 60]],
             "adjacent": ["Z2"]},
            {"zone_id": "Z2", "polygon": [[200, 0], [400, 0], [400, 60], [200, 60]],
             "adjacent": ["Z1"]},
        ],
        "spec": {"materials": {"CF-EP-3K": {"ply_thickness": 0.125}},
                 "plies": plies, "rules": rules},
        "rolls": [{"roll_id": "R1", "batch_no": "B1", "material": "CF-EP-3K",
                   "out_time_limit_h": roll_limit}],
    }
    _s, r = call("POST", "/jobs", body)
    return r["job_id"]


def placed(pid, angle, t, roll="R1", geom=FULL, face="up", op="op1", **kw):
    e = {"type": "ply_placed", "ply_id": pid, "roll": roll,
         "angle": angle, "face": face, "geometry": geom, "placed_at": t}
    if op is not None:
        e["operator"] = op
    e.update(kw)
    return e


def thaw(roll="R1", at="2026-09-10T06:00:00Z"):
    return {"type": "roll_thawed", "operator": "op1", "roll": roll, "at": at}


def rules_of(jid):
    _s, r = call("GET", f"/jobs/{jid}/validate")
    return {v["rule"] for v in r["violations"]}, r


def check(name, jid, expect, absent=()):
    got, r = rules_of(jid)
    missing = set(expect) - got
    unexpected = set(absent) & got
    ok = not missing and not unexpected
    print(f"{'PASS' if ok else 'FAIL'}  {name}: 命中 {sorted(got)}")
    if not ok:
        if missing:
            print(f"     缺少预期规则: {missing}")
        if unexpected:
            print(f"     不应出现: {unexpected}")
        print(json.dumps(r["violations"], ensure_ascii=False, indent=1))
    return ok


def find_violations(jid, rule):
    _s, r = call("GET", f"/jobs/{jid}/validate")
    return [v for v in r["violations"] if v["rule"] == rule]


def approve_blocked(name, jid):
    """错误批准路径：存在违规时批准必须返回 409，不得冻结快照。"""
    s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
    ok = s.startswith("409") and r.get("error") == "release_rejected"
    print(f"{'PASS' if ok else 'FAIL'}  {name}: 批准被拒 [{s}]")
    return ok


def lay_all(jid, angles=(0, 45, -45, -45, 45, 0), **kw):
    call("POST", f"/jobs/{jid}/events", {"events": [thaw()] + [
        placed(f"P{i:02d}", a, f"2026-09-10T08:0{i}:00Z", **kw)
        for i, a in enumerate(angles, 1)]})


results = []

# 1. 缺层：P04 未铺
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [thaw()] + [
    placed(f"P{i:02d}", a, f"2026-09-10T08:0{i}:00Z")
    for i, a in enumerate([0, 45, -45, 45, 0], 1)]})
results.append(check("缺层 MISSING_PLY", jid, {"MISSING_PLY"}))

# 2. 重复层：P02 铺两次
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [thaw()] + [
    placed(f"P{i:02d}", a, f"2026-09-10T08:0{i}:00Z")
    for i, a in enumerate([0, 45, -45, -45, 45, 0], 1)] + [
    placed("P02", 45, "2026-09-10T09:00:00Z")]})
results.append(check("重复层 DUPLICATE_PLY", jid, {"DUPLICATE_PLY"}))

# 3. 层序抄错：P03 先于 P02 铺
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [thaw(),
    placed("P01", 0, "2026-09-10T08:01:00Z"),
    placed("P03", -45, "2026-09-10T08:02:00Z"),
    placed("P02", 45, "2026-09-10T08:03:00Z"),
    placed("P04", -45, "2026-09-10T08:04:00Z"),
    placed("P05", 45, "2026-09-10T08:05:00Z"),
    placed("P06", 0, "2026-09-10T08:06:00Z")]})
results.append(check("层序 ORDER_MISMATCH", jid, {"ORDER_MISMATCH"}))

# 4. 外置时间超限（上限 2h，铺放时已解冻 3h）
jid = new_job(roll_limit=2)
call("POST", f"/jobs/{jid}/events", {"events": [thaw()] + [
    placed(f"P{i:02d}", a, f"2026-09-10T09:0{i}:00Z")
    for i, a in enumerate([0, 45, -45, -45, 45, 0], 1)]})
results.append(check("外置时间 OUT_TIME_EXCEEDED", jid, {"OUT_TIME_EXCEEDED"}))

# 5. 解冻记录缺失
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    placed(f"P{i:02d}", a, f"2026-09-10T08:0{i}:00Z")
    for i, a in enumerate([0, 45, -45, -45, 45, 0], 1)]})
results.append(check("解冻资料缺失 OUT_TIME_DATA_MISSING", jid,
                     {"OUT_TIME_DATA_MISSING"}))

# 6. 揭除未串替代层
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [thaw()] + [
    placed(f"P{i:02d}", a, f"2026-09-10T08:0{i}:00Z")
    for i, a in enumerate([0, 45, -45, -45, 45, 0], 1)] + [
    {"type": "ply_removed", "operator": "op1", "ply_id": "P04",
     "reason": "污染"}]})
results.append(check("返工未闭环 REMOVAL_WITHOUT_REPLACEMENT", jid,
                     {"REMOVAL_WITHOUT_REPLACEMENT", "MISSING_PLY"}))

# 7. 返工引用不存在的揭除层
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [thaw()] + [
    placed(f"P{i:02d}", a, f"2026-09-10T08:0{i}:00Z")
    for i, a in enumerate([0, 45, -45, -45, 45, 0], 1)] + [
    {"type": "ply_replaced", "operator": "op1", "removed_ply_id": "P99",
     "replacement": {"ply_id": "P99", "roll": "R1", "angle": 0, "face": "up",
                     "geometry": FULL, "placed_at": "2026-09-10T09:00:00Z"}}]})
results.append(check("返工链断裂 REWORK_LINK_BROKEN", jid,
                     {"REWORK_LINK_BROKEN"}))

# 8. 丢层错开不足
jid = new_job(drops={"P02": 100.0, "P04": 105.0})
call("POST", f"/jobs/{jid}/events", {"events": [thaw()] + [
    placed(f"P{i:02d}", a, f"2026-09-10T08:0{i}:00Z")
    for i, a in enumerate([0, 45, -45, -45, 45, 0], 1)]})
results.append(check("丢层错开 DROP_STAGGER", jid, {"DROP_STAGGER"}))

# 9. 连续同向超限（5 层 0°）
jid = new_job(angles=(0, 0, 0, 0, 0, 90), extra_rules={
    "require_symmetry": False, "max_consecutive_same_angle": 4})
call("POST", f"/jobs/{jid}/events", {"events": [thaw()] + [
    placed(f"P{i:02d}", a, f"2026-09-10T08:0{i}:00Z")
    for i, a in enumerate([0, 0, 0, 0, 0, 90], 1)]})
results.append(check("连续同向 CONSECUTIVE_ANGLE", jid, {"CONSECUTIVE_ANGLE"}))

# 10. 接缝重叠与超隙
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [thaw(),
    placed("P01", 0, "2026-09-10T08:01:00Z",
           seams=[{"zone": "Z1", "axis": "x", "at": 80.0, "gap": -0.5}]),
    placed("P02", 45, "2026-09-10T08:02:00Z",
           seams=[{"zone": "Z2", "axis": "x", "at": 300.0, "gap": 3.0}]),
    placed("P03", -45, "2026-09-10T08:03:00Z"),
    placed("P04", -45, "2026-09-10T08:04:00Z"),
    placed("P05", 45, "2026-09-10T08:05:00Z"),
    placed("P06", 0, "2026-09-10T08:06:00Z")]})
results.append(check("接缝重叠/超隙", jid, {"SEAM_OVERLAP", "SEAM_GAP_EXCEEDED"}))

# 11. 分区覆盖不足（P02 只铺了半张）
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [thaw(),
    placed("P01", 0, "2026-09-10T08:01:00Z"),
    placed("P02", 45, "2026-09-10T08:02:00Z", geom=HALF),
    placed("P03", -45, "2026-09-10T08:03:00Z"),
    placed("P04", -45, "2026-09-10T08:04:00Z"),
    placed("P05", 45, "2026-09-10T08:05:00Z"),
    placed("P06", 0, "2026-09-10T08:06:00Z")]})
results.append(check("覆盖不足 MISSING_COVERAGE", jid, {"MISSING_COVERAGE"}))

# 12. 料卷未登记 + 非规范层
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [thaw()] + [
    placed(f"P{i:02d}", a, f"2026-09-10T08:0{i}:00Z")
    for i, a in enumerate([0, 45, -45, -45, 45, 0], 1)] + [
    placed("P07", 90, "2026-09-10T09:00:00Z", roll="R9")]})
results.append(check("未登记料卷/非规范层", jid,
                     {"ROLL_UNKNOWN", "UNEXPECTED_PLY"}))

# 13. 正反面错误
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [thaw(),
    placed("P01", 0, "2026-09-10T08:01:00Z", face="down")] + [
    placed(f"P{i:02d}", a, f"2026-09-10T08:0{i}:00Z")
    for i, a in enumerate([45, -45, -45, 45, 0], 2)]})
results.append(check("正反面 FACE_MISMATCH", jid, {"FACE_MISMATCH"}))

# 14. 干净工单：无任何违规
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [thaw()] + [
    placed(f"P{i:02d}", a, f"2026-09-10T08:0{i}:00Z")
    for i, a in enumerate([0, 45, -45, -45, 45, 0], 1)]})
s, r = call("GET", f"/jobs/{jid}/validate")
ok = r["release"] == "ok"
print(f"{'PASS' if ok else 'FAIL'}  干净工单放行: {r['release']}")
results.append(ok)

# 15. 事件链不可追加非法类型；批准后有违规仍 409
s, r = call("POST", f"/jobs/{jid}/events", {"type": "ply_deleted", "operator": "x"})
ok = s.startswith("400")
print(f"{'PASS' if ok else 'FAIL'}  非法事件类型被拒: {s}")
results.append(ok)

# ---------------------------------------------------------------- 回归：
# 以下均为曾错误放行（release=ok 且批准 201 冻结）的路径，现必须在
# 校验中给出带层号/区域的违规，并在批准时返回 409。

# 16. placed_at 无法解析
jid = new_job()
evs = [thaw()] + [placed(f"P{i:02d}", a, f"2026-09-10T08:0{i}:00Z")
                  for i, a in enumerate([0, 45, -45, -45, 45, 0], 1)]
evs[3]["placed_at"] = "not-a-time"          # P03 铺放时刻无法解析
call("POST", f"/jobs/{jid}/events", {"events": evs})
vs = find_violations(jid, "INVALID_TIME")
ok = len(vs) == 1 and vs[0]["plies"] == ["P03"] and "Z1" in vs[0]["zones"]
print(f"{'PASS' if ok else 'FAIL'}  placed_at 无法解析 INVALID_TIME: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
results.append(approve_blocked("错误批准路径: INVALID_TIME", jid))

# 17. 铺层规范缺少 face
jid = new_job(omit_face_for={"P03"})
lay_all(jid)
vs = find_violations(jid, "SPEC_DATA_MISSING")
ok = any(v["plies"] == ["P03"] and "face" in v["details"].get("missing", [])
         for v in vs)
print(f"{'PASS' if ok else 'FAIL'}  规范缺 face SPEC_DATA_MISSING: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
results.append(approve_blocked("错误批准路径: 规范缺 face", jid))

# 18. 规范 seq 重复
jid = new_job(dup_seq=True)
lay_all(jid)
vs = find_violations(jid, "SPEC_DUPLICATE_SEQ")
ok = len(vs) == 1 and set(vs[0]["plies"]) == {"P03", "P04"}
print(f"{'PASS' if ok else 'FAIL'}  规范 seq 重复 SPEC_DUPLICATE_SEQ: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
results.append(approve_blocked("错误批准路径: seq 重复", jid))

# 19. 接缝引用不存在的分区 Z404
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [thaw(),
    placed("P01", 0, "2026-09-10T08:01:00Z"),
    placed("P02", 45, "2026-09-10T08:02:00Z",
           seams=[{"zone": "Z404", "axis": "x", "at": 100.0, "gap": 0.5}]),
    placed("P03", -45, "2026-09-10T08:03:00Z"),
    placed("P04", -45, "2026-09-10T08:04:00Z"),
    placed("P05", 45, "2026-09-10T08:05:00Z"),
    placed("P06", 0, "2026-09-10T08:06:00Z")]})
vs = find_violations(jid, "ZONE_UNKNOWN")
ok = len(vs) == 1 and vs[0]["plies"] == ["P02"] and vs[0]["zones"] == ["Z404"]
print(f"{'PASS' if ok else 'FAIL'}  接缝引用 Z404 ZONE_UNKNOWN: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
results.append(approve_blocked("错误批准路径: 接缝分区 Z404", jid))

# 20. tool_datum 为空
jid = new_job(datum={})
lay_all(jid)
vs = find_violations(jid, "DATUM_MISSING")
ok = len(vs) == 1
print(f"{'PASS' if ok else 'FAIL'}  模具基准为空 DATUM_MISSING: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
results.append(approve_blocked("错误批准路径: tool_datum 为空", jid))

# 21. 缺少 operator 的 DATA_MISSING 须带出规范已知的 Z1
jid = new_job()
evs = [thaw()] + [placed(f"P{i:02d}", a, f"2026-09-10T08:0{i}:00Z")
                  for i, a in enumerate([0, 45, -45, -45, 45, 0], 1)]
del evs[4]["operator"]                       # P04 缺 operator
call("POST", f"/jobs/{jid}/events", {"events": evs})
vs = find_violations(jid, "DATA_MISSING")
ok = (len(vs) == 1 and vs[0]["plies"] == ["P04"]
      and "operator" in vs[0]["details"].get("missing", [])
      and "Z1" in vs[0]["zones"])
print(f"{'PASS' if ok else 'FAIL'}  缺 operator 带出 Z1: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
results.append(approve_blocked("错误批准路径: 缺 operator", jid))

print(f"\n{sum(results)}/{len(results)} 通过")
raise SystemExit(0 if all(results) else 1)
