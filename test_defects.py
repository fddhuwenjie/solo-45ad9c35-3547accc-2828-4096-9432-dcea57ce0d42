#!/usr/bin/env python3
"""局部缺陷处置覆盖测试。

场景覆盖：
  * 缺陷登记：原铺层/层位/分区定位，多边形+照片摘要+处置指令，指令
    版本缺失/与现行不符不可批准；
  * 规范按缺陷类型/层位/分区给尺寸上限、禁修区（含整体禁修）、补片
    材料、纤维方向、搭接宽度、逐层退让量；几何越出分区/原层即报；
  * 逐层核查：补片覆盖揭除腔、周向搭接、逐层退让、材料、纤维方向、
    正反面、接缝（间隙/错开）、与开孔净距；
  * 补片层序：受影响层必须连续不浅于源层，自上而下揭除、自下而上
    铺放，缺层/乱序/越层即“层序断裂”；后续封闭层须逐点覆盖；
  * 复检未通过/未签发/签发早于复检不可批准；
  * 牵涉已批准锁层（快照冻结窗口内的局部揭除）不可批准；
  * 只撤销相关处置：轮廓变化开启新一代（旧代留痕）、源层整层返工、
    签发后规范换版（repairs 块变化）使旧签发过期，重新签发闭环；
    无关缺陷不受影响；
  * 让步接收/报废只需签发；无 repairs 规范提交处置事件即 SPEC_MISSING；
  * 批准快照收录指令/几何/事件/人工理由，旧版 package 与 diff 可查。
"""

import io
import json
import os
import tempfile

from prepreg_release import make_app

DB = os.path.join(tempfile.gettempdir(), "prepreg_defects_test.db")
if os.path.exists(DB):
    os.remove(DB)
app = make_app(DB)

# 足够大的实铺几何，避免正常补片越界
FULL = [[-50, -50], [450, -50], [450, 110], [-50, 110]]
ANGLES = [0, 45, -45, -45, 45, 0]
T0 = "2026-09-10T06:00:00Z"


def call(method, path, body=None, query=""):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
    cap = {}

    def sr(status, headers, exc_info=None):
        cap["status"] = status

    env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
           "CONTENT_LENGTH": str(len(data)), "wsgi.input": io.BytesIO(data)}
    payload = json.loads(b"".join(app(env, sr)))
    return cap["status"], payload


results = []


def check(name, cond, info=""):
    ok = bool(cond)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": {info}"))
    results.append(ok)


def rules_of(vv):
    return {v["rule"] for v in vv}


def find(vv, rule, defect=None):
    for v in vv:
        if v["rule"] == rule and (
                defect is None
                or defect in (v.get("details") or {}).get("defects", [])):
            return v
    return None


# ---------------------------------------------------------------- 构造器

def repairs_spec(**over):
    block = {
        "version": "RP-2026-A",
        "defaults": {"min_clearance_mm": 20.0},
        "types": {
            "delamination": {
                "max_size_mm": 80.0, "max_depth_plies": 3,
                "material": "match", "angle": "match",
                "lap_width_mm": 15.0, "stepback_mm": 10.0,
                "zones": {"Z1": {"max_size_mm": 60.0}},
            },
            "porosity": {"max_size_mm": 50.0, "max_depth_plies": 1,
                         "material": "match", "angle": "match",
                         "lap_width_mm": 12.0, "stepback_mm": 8.0},
            "fod": {"no_repair": True},
        },
        "no_repair_areas": [
            {"area_id": "NR1", "zone": "Z1",
             "polygon": [[0, 0], [40, 0], [40, 60], [0, 60]]}],
        "openings": [{"opening_id": "O1",
                      "polygon": [[360, 10], [390, 10], [390, 50], [360, 50]]}],
    }
    block.update(over)
    return block


def base_spec(repairs=True, **rule_over):
    rules = {"seam_min_stagger_mm": 25.0, "seam_max_gap_mm": 1.5,
             "require_symmetry": False, "require_balance": False,
             "max_consecutive_same_angle": 4}
    rules.update(rule_over)
    spec = {
        "materials": {"CF-EP-3K": {"ply_thickness": 0.125},
                      "CF-EP-3K-B": {"ply_thickness": 0.125}},
        "plies": [{"seq": i, "ply_id": f"P{i:02d}", "material": "CF-EP-3K",
                   "angle": a, "face": "up", "zones": ["Z1", "Z2"]}
                  for i, a in enumerate(ANGLES, 1)],
        "rules": rules}
    if repairs:
        spec["repairs"] = repairs_spec()
    return spec


def new_job(spec=None, rolls=None):
    spec = spec or base_spec()
    body = {
        "name": "defect-test",
        "tool_datum": {"datum_id": "M1", "units": "mm"},
        "zones": [
            {"zone_id": "Z1", "polygon": [[0, 0], [200, 0], [200, 60], [0, 60]],
             "adjacent": ["Z2"]},
            {"zone_id": "Z2", "polygon": [[200, 0], [400, 0], [400, 60], [200, 60]],
             "adjacent": ["Z1"]}],
        "spec": spec,
        "rolls": rolls or [{"roll_id": "R1", "batch_no": "B1",
                           "material": "CF-EP-3K", "out_time_limit_h": 240}]}
    _s, r = call("POST", "/jobs", body)
    return r["job_id"]


def lay(jid, n=6):
    evs = [{"type": "roll_thawed", "operator": "op1", "roll": "R1", "at": T0}]
    for i in range(1, n + 1):
        evs.append({"type": "ply_placed", "operator": "op1",
                    "ply_id": f"P{i:02d}", "roll": "R1",
                    "angle": ANGLES[i - 1], "face": "up", "geometry": FULL,
                    "placed_at": f"2026-09-10T{7 + i:02d}:00:00Z"})
    call("POST", f"/jobs/{jid}/events", {"events": evs})


def box(cx, cy, half):
    return [[cx - half, cy - half], [cx + half, cy - half],
            [cx + half, cy + half], [cx - half, cy + half]]


def found(did="D1", dtype="delamination", pid="P03", zone="Z1",
          cx=100, half=10, day="11", **kw):
    e = {"type": "defect_found", "operator": "qc-li", "defect_id": did,
         "defect_type": dtype, "ply_id": pid, "zone": zone,
         "polygon": box(cx, 30, half), "instruction": "RP-2026-A",
         "photo_digest": f"sha256:{did}", "photo_summary": f"{did} 照片摘要",
         "disposition": "repair",
         "at": f"2026-09-{day}T08:00:00Z"}
    e.update(kw)
    return e


def isolated(did="D1", day="11", hm="08:05"):
    return {"type": "defect_isolated", "operator": "qc-li", "defect_id": did,
            "at": f"2026-09-{day}T{hm}:00Z"}


def removed(pid, poly, did="D1", day="11", hm="08:20"):
    return {"type": "defect_ply_removed", "operator": "op-zhang",
            "defect_id": did, "ply_id": pid, "polygon": poly,
            "reason": "局部挖除", "at": f"2026-09-{day}T{hm}:00Z"}


def patch(pid, poly, angle, did="D1", day="11", hm="08:40", roll="R1", **kw):
    e = {"type": "patch_placed", "operator": "op-zhang", "defect_id": did,
         "ply_id": pid, "polygon": poly, "roll": roll, "angle": angle,
         "face": "up", "placed_at": f"2026-09-{day}T{hm}:00Z"}
    e.update(kw)
    return e


def reinspect(did="D1", result="pass", day="11", hm="09:00", **kw):
    e = {"type": "defect_reinspected", "operator": "qc-li", "defect_id": did,
         "result": result, "method": "ultrasonic",
         "at": f"2026-09-{day}T{hm}:00Z"}
    e.update(kw)
    return e


def sign(did="D1", gen=0, decision="confirmed", day="11", hm="09:10",
         instruction="RP-2026-A", **kw):
    e = {"type": "repair_signed", "operator": "eng-wang", "defect_id": did,
         "generation": gen, "decision": decision, "instruction": instruction,
         "reason": "几何/搭接/退让符合指令", "signed_by": "eng-wang",
         "at": f"2026-09-{day}T{hm}:00Z"}
    e.update(kw)
    return e


def full_single_repair(did="D1", cx=100, half=10, pid="P03", angle=-45,
                       day="11", lap_extra=2, sign_kw=None, roll="R1",
                       **fkw):
    """单层挖补 happy-path 事件序列。

    揭除腔比缺陷外扩 12mm；补片再外扩 16mm（周向搭接 16 ≥ 15mm）。
    """
    return [
        found(did, pid=pid, cx=cx, half=half, day=day, **fkw),
        isolated(did, day),
        removed(pid, box(cx, 30, half + 12), did, day, "08:20"),
        patch(pid, box(cx, 30, half + 12 + 16), angle, did, day, "08:40",
              roll=roll),
        reinspect(did, "pass", day, "09:00"),
        sign(did, 0, day=day, hm="09:10", **(sign_kw or {})),
    ]


# ================================================================ 1. 合规闭环
jid = new_job()
lay(jid)
s, r = call("POST", f"/jobs/{jid}/events",
            {"events": full_single_repair()})
check("合规单层挖补事件入链 201", s.startswith("201"), f"[{s}] {r}")
s, r = call("GET", f"/jobs/{jid}/validate")
check("合规单层挖补后放行通过", r["release"] == "ok" and r["violation_count"] == 0,
      json.dumps(r["violations"], ensure_ascii=False)[:400])
s, d = call("GET", f"/jobs/{jid}/defects/D1")
check("缺陷查询：状态 signed、层位 P03、分区 Z1",
      s.startswith("200") and d["status"] == "signed"
      and d["source_ply"] == "P03" and d["zone"] == "Z1"
      and d["defect_type"] == "delamination",
      json.dumps(d, ensure_ascii=False)[:300])
s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
check("含合规补片可批准", s.startswith("201"), f"[{s}] {json.dumps(r,ensure_ascii=False)[:300]}")

# ================================================================ 2. 指令版本缺失/不符
jid = new_job()
lay(jid)
evs = full_single_repair("D1", instruction=None)
# 签发仍写现行版本，检验发现时无 instruction
s, r = call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("发现时未携带指令 → DEFECT_INSTRUCTION_MISSING",
      find(r["violations"], "DEFECT_INSTRUCTION_MISSING", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))
s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
check("指令缺失工单不可批准 409", s.startswith("409"), f"[{s}]")

jid = new_job()
lay(jid)
evs = full_single_repair("D1")
evs[0]["instruction"] = "RP-2010-OLD"
evs[-1]["instruction"] = "RP-2010-OLD"   # 签发同样引用旧版
s, r = call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("指令版本与现行不符 → REPAIR_INSTRUCTION_VERSION（发现+签发各一）",
      len([v for v in r["violations"]
           if v["rule"] == "REPAIR_INSTRUCTION_VERSION"
           and "D1" in v["details"]["defects"]]) == 2,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# ================================================================ 3. 尺寸上限（类型/分区/层位）
jid = new_job()
lay(jid)
# Z1 分区上限 60mm（特征尺寸 = 2*half）；half=35 → 70mm 越限
s, r = call("POST", f"/jobs/{jid}/events",
            {"events": [found("D1", half=35)]})
s, r = call("GET", f"/jobs/{jid}/validate")
v = find(r["violations"], "DEFECT_SIZE_EXCEEDED", "D1")
check("分区尺寸上限 60mm：70mm 缺陷报 DEFECT_SIZE_EXCEEDED",
      v is not None and v["details"]["limit_mm"] == 60.0
      and v["plies"] == ["P03"] and v["zones"] == ["Z1"],
      json.dumps(v, ensure_ascii=False) if v else "none")

# Z2 分区用类型默认 80mm：70mm 合规
jid = new_job()
lay(jid)
s, r = call("POST", f"/jobs/{jid}/events",
            {"events": [found("D1", zone="Z2", cx=300, half=35)]})
s, r = call("GET", f"/jobs/{jid}/validate")
check("同尺寸在 Z2（类型上限 80）不越限",
      find(r["violations"], "DEFECT_SIZE_EXCEEDED", "D1") is None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# 层位收窄深度：porosity 默认 max_depth_plies=1，显式两层受影响即越深度
jid = new_job()
lay(jid)
evs = [found("D1", dtype="porosity", half=8,
             affected_plies=["P03", "P02"]),
       isolated(),
       removed("P03", box(100, 30, 8 + 10)),
       removed("P02", box(100, 30, 8)),
       patch("P02", box(100, 30, 8 + 2), 45),
       patch("P03", box(100, 30, 8 + 12), -45),
       reinspect(), sign()]
s, r = call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
v = find(r["violations"], "DEFECT_DEPTH_EXCEEDED", "D1")
check("深度上限：porosity 揭 2 层报 DEFECT_DEPTH_EXCEEDED（带层号）",
      v is not None and v["details"]["depth"] == 2
      and v["details"]["limit_plies"] == 1,
      json.dumps(v, ensure_ascii=False) if v else "none")

# ================================================================ 4. 禁修区
jid = new_job()
lay(jid)
# NR1 覆盖 Z1 的 x∈[0,40]；缺陷中心 cx=25, half=10 → 相交
s, r = call("POST", f"/jobs/{jid}/events",
            {"events": [found("D1", cx=25, half=10)]})
s, r = call("GET", f"/jobs/{jid}/validate")
v = find(r["violations"], "DEFECT_NO_REPAIR_ZONE", "D1")
check("落入禁修区 NR1 → DEFECT_NO_REPAIR_ZONE",
      v is not None and v["details"]["area_id"] == "NR1"
      and v["plies"] == ["P03"] and v["zones"] == ["Z1"],
      json.dumps(v, ensure_ascii=False) if v else "none")

jid = new_job()
lay(jid)
s, r = call("POST", f"/jobs/{jid}/events",
            {"events": [found("D1", dtype="fod", half=8)]})
s, r = call("GET", f"/jobs/{jid}/validate")
check("整体禁修类型 fod → DEFECT_NO_REPAIR_ZONE",
      find(r["violations"], "DEFECT_NO_REPAIR_ZONE", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# ================================================================ 5. 几何越界
jid = new_job()
lay(jid)
# 缺陷越出 Z1（x 上限 200）
s, r = call("POST", f"/jobs/{jid}/events",
            {"events": [found("D1", cx=195, half=10)]})
s, r = call("GET", f"/jobs/{jid}/validate")
check("缺陷越出分区边界 → DEFECT_GEOMETRY_OUT_OF_BOUNDS",
      find(r["violations"], "DEFECT_GEOMETRY_OUT_OF_BOUNDS", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

jid = new_job()
lay(jid)
# 补片越出该层实铺几何：把揭除/补片放到 Z2 远端贴近边缘但用小实铺层
s, r = call("POST", f"/jobs/{jid}/events", {"events": [
    {"type": "ply_removed", "operator": "o", "ply_id": "P03", "reason": "x"},
    {"type": "ply_replaced", "operator": "o", "removed_ply_id": "P03",
     "replacement": {"ply_id": "P03", "roll": "R1", "angle": -45, "face": "up",
                     "geometry": [[0, 0], [120, 0], [120, 60], [0, 60]],
                     "placed_at": "2026-09-10T12:00:00Z"}}]})
evs = [found("D1", cx=105, half=8), isolated(),
       removed("P03", box(105, 30, 20)),           # 揭除已到 x=125，越实铺
       patch("P03", box(105, 30, 22), -45),
       reinspect(), sign()]
s, r = call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("补片越出原层实铺几何 → DEFECT_GEOMETRY_OUT_OF_BOUNDS",
      find(r["violations"], "DEFECT_GEOMETRY_OUT_OF_BOUNDS", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# 缺陷落在不存在的层/分区
jid = new_job()
lay(jid)
call("POST", f"/jobs/{jid}/events", {"events": [found("D1", pid="P99")]})
s, r = call("GET", f"/jobs/{jid}/validate")
check("缺陷层位规范外 → DEFECT_PLY_UNKNOWN",
      find(r["violations"], "DEFECT_PLY_UNKNOWN", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))
call("POST", f"/jobs/{jid}/events", {"events": [
    {"type": "defect_found", "operator": "q", "defect_id": "D2",
     "defect_type": "delamination", "ply_id": "P03", "zone": "Z9",
     "polygon": box(100, 30, 8), "instruction": "RP-2026-A",
     "photo_summary": "x", "disposition": "repair",
     "at": "2026-09-12T08:00:00Z"}]})
s, r = call("GET", f"/jobs/{jid}/validate")
check("缺陷分区未定义 → ZONE_UNKNOWN（带缺陷/层/分区）",
      find(r["violations"], "ZONE_UNKNOWN", "D2") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# ================================================================ 6. 逐层退让（挖补台阶）
# 合规两层挖补：揭除自浅而深每层内收 12mm（≥stepback 10），补片自深而浅
# 每层外扩 12mm，补片比对应揭除腔再外扩 16mm（周向搭接 ≥15）
jid = new_job()
lay(jid)
evs = [found("D1", half=8, affected_plies=["P03", "P02"]), isolated(),
       removed("P03", box(100, 30, 8 + 24)),
       removed("P02", box(100, 30, 8 + 12)),
       patch("P02", box(100, 30, 8 + 12 + 16), 45, hm="08:40"),
       patch("P03", box(100, 30, 8 + 24 + 16), -45, hm="08:50"),
       reinspect(hm="09:00"), sign(hm="09:10")]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("合规两层挖补（退让/搭接/方向/封闭层）放行通过",
      r["release"] == "ok",
      json.dumps([(v["rule"], v["plies"]) for v in r["violations"]],
                 ensure_ascii=False)[:400])

jid = new_job()
lay(jid)
# 两层挖补：P03 揭除只比 P02 大 4mm（< stepback 10）
evs = [found("D1", half=8, affected_plies=["P03", "P02"]), isolated(),
       removed("P03", box(100, 30, 12)),    # 24 vs 16 → 退让 4mm
       removed("P02", box(100, 30, 8)),
       patch("P02", box(100, 30, 10), 45),
       patch("P03", box(100, 30, 14), -45),
       reinspect(), sign()]
s, r = call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
v = find(r["violations"], "PATCH_STEPBACK", "D1")
check("揭除退让不足 10mm → PATCH_STEPBACK（层 P03/P02）",
      v is not None and set(v["plies"]) == {"P02", "P03"},
      json.dumps(v, ensure_ascii=False) if v else "none")

jid = new_job()
lay(jid)
# 补片台阶未复刻：P03 补片与 P02 补片同尺寸
evs = [found("D1", half=8, affected_plies=["P03", "P02"]), isolated(),
       removed("P03", box(100, 30, 20)),
       removed("P02", box(100, 30, 8)),
       patch("P02", box(100, 30, 20), 45),     # 错误：与浅层一样大
       patch("P03", box(100, 30, 20), -45),
       reinspect(), sign()]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("补片台阶未逐层退让 → PATCH_STEPBACK",
      find(r["violations"], "PATCH_STEPBACK", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# ================================================================ 7. 覆盖/搭接
jid = new_job()
lay(jid)
# 补片不覆盖揭除腔（补片偏心且更小）
evs = [found("D1", half=10), isolated(),
       removed("P03", box(100, 30, 22)),
       patch("P03", box(100, 30, 12), -45),    # 12 < 22 未覆盖腔
       reinspect(), sign()]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("补片未覆盖揭除腔 → PATCH_COVERAGE",
      find(r["violations"], "PATCH_COVERAGE", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

jid = new_job()
lay(jid)
# 覆盖腔但周向搭接不足：腔 half=25，补片 half=27 → 搭接 2mm < 15
evs = [found("D1", half=10), isolated(),
       removed("P03", box(100, 30, 25)),
       patch("P03", box(100, 30, 27), -45),
       reinspect(), sign()]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
v = find(r["violations"], "PATCH_LAP_INSUFFICIENT", "D1")
check("周向搭接 2mm < 15mm → PATCH_LAP_INSUFFICIENT",
      v is not None and v["details"]["required_mm"] == 15.0,
      json.dumps(v, ensure_ascii=False) if v else "none")

# ================================================================ 8. 材料/方向/正反面
jid = new_job()
lay(jid)
call("POST", f"/jobs/{jid}/rolls",
     {"roll_id": "R2", "batch_no": "B2", "material": "CF-EP-3K-B",
      "out_time_limit_h": 240})
evs = full_single_repair("D1", roll="R2")  # 补片用 B 料，指令 match 原层 CF-EP-3K
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
v = find(r["violations"], "PATCH_MATERIAL", "D1")
check("补片材料不符 → PATCH_MATERIAL（实际/要求）",
      v is not None and v["details"]["material"] == "CF-EP-3K-B"
      and v["details"]["required"] == "CF-EP-3K",
      json.dumps(v, ensure_ascii=False) if v else "none")

jid = new_job()
lay(jid)
evs = full_single_repair("D1", angle=90)   # P03 要求 -45
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
v = find(r["violations"], "PATCH_ANGLE_MISMATCH", "D1")
check("补片纤维方向错误 → PATCH_ANGLE_MISMATCH",
      v is not None and v["details"]["actual_deg"] == 90
      and v["details"]["required_deg"] == -45,
      json.dumps(v, ensure_ascii=False) if v else "none")

jid = new_job()
lay(jid)
evs = full_single_repair("D1")
evs[3]["face"] = "down"   # 补片正反面错误
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("补片正反面错误 → PATCH_FACE_MISMATCH",
      find(r["violations"], "PATCH_FACE_MISMATCH", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# ================================================================ 9. 层序：受影响层/揭除/铺放次序
jid = new_job()
lay(jid)
# 受影响层不连续（P03 与 P01，跳过 P02）且浅于源层检查
evs = [found("D1", half=8, affected_plies=["P03", "P01"])]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("受影响层不连续 → PATCH_SEQUENCE_BROKEN",
      find(r["violations"], "PATCH_SEQUENCE_BROKEN", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

jid = new_job()
lay(jid)
# 受影响层浅于源层（P04 比 P03 浅）
evs = [found("D1", half=8, affected_plies=["P03", "P04"])]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("受影响层浅于源层 → PATCH_SEQUENCE_BROKEN",
      find(r["violations"], "PATCH_SEQUENCE_BROKEN", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

jid = new_job()
lay(jid)
# 两层但缺 P02 补片 → 层序断裂，且未复检通过/签发
evs = [found("D1", half=8, affected_plies=["P03", "P02"]), isolated(),
       removed("P03", box(100, 30, 20)),
       removed("P02", box(100, 30, 8)),
       patch("P03", box(100, 30, 22), -45),
       reinspect(), sign()]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("缺层补片 → PATCH_SEQUENCE_BROKEN（带缺失层号）",
      (lambda v: v is not None and "P02" in v["plies"])
      (find(r["violations"], "PATCH_SEQUENCE_BROKEN", "D1")),
      json.dumps(find(r["violations"], "PATCH_SEQUENCE_BROKEN", "D1"),
                 ensure_ascii=False))

jid = new_job()
lay(jid)
# 揭除次序颠倒（先深 P02 后浅 P03）
evs = [found("D1", half=8, affected_plies=["P03", "P02"]), isolated(),
       removed("P02", box(100, 30, 8), hm="08:20"),
       removed("P03", box(100, 30, 20), hm="08:30"),
       patch("P02", box(100, 30, 10), 45, hm="08:40"),
       patch("P03", box(100, 30, 22), -45, hm="08:50"),
       reinspect(hm="09:00"), sign(hm="09:10")]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("揭除未按自上而下 → PATCH_SEQUENCE_BROKEN",
      find(r["violations"], "PATCH_SEQUENCE_BROKEN", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

jid = new_job()
lay(jid)
# 补片铺放次序颠倒（先浅 P03 后深 P02）
evs = [found("D1", half=8, affected_plies=["P03", "P02"]), isolated(),
       removed("P03", box(100, 30, 20), hm="08:20"),
       removed("P02", box(100, 30, 8), hm="08:30"),
       patch("P03", box(100, 30, 22), -45, hm="08:40"),
       patch("P02", box(100, 30, 10), 45, hm="08:50"),
       reinspect(hm="09:00"), sign(hm="09:10")]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("补片未按自下而上 → PATCH_SEQUENCE_BROKEN",
      find(r["violations"], "PATCH_SEQUENCE_BROKEN", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# ================================================================ 10. 工艺次序/复检/签发
jid = new_job()
lay(jid)
# 未隔离即揭除
evs = full_single_repair("D1")
del evs[1]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("未隔离即修补 → DEFECT_STAGE_ORDER",
      find(r["violations"], "DEFECT_STAGE_ORDER", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

jid = new_job()
lay(jid)
# 复检不合格且无后续通过
evs = full_single_repair("D1")
evs[4]["result"] = "fail"
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("复检未闭合 → DEFECT_REINSPECTION_OPEN",
      find(r["violations"], "DEFECT_REINSPECTION_OPEN", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

jid = new_job()
lay(jid)
# 复检失败后追加通过 → 闭环放行（fail 记录在状态中留痕，不再阻断）
evs = full_single_repair("D1")
evs[4]["result"] = "fail"
evs[4]["at"] = "2026-09-11T09:00:00Z"
evs[5]["at"] = "2026-09-11T09:40:00Z"
evs.append(reinspect("D1", "pass", hm="09:30"))
# 调整：复检通过 09:30 后须在其后签发 → 重排签发
evs[5]["at"] = "2026-09-11T09:40:00Z"
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
_s, d = call("GET", f"/jobs/{jid}/defects/D1")
g0 = d["generations"][0]
check("复检 fail→pass 后闭环放行，fail 在复检序列中留痕",
      r["release"] == "ok"
      and [x["result"] for x in g0["reinspections"]] == ["fail", "pass"],
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

jid = new_job()
lay(jid)
# 无工程师签发
evs = full_single_repair("D1")[:-1]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("未签发 → REPAIR_NOT_SIGNED，工单不可批准",
      find(r["violations"], "REPAIR_NOT_SIGNED", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))
s, _ = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
check("未签工单拒绝批准 409", s.startswith("409"), f"[{s}]")

jid = new_job()
lay(jid)
# 签发早于通过复检
evs = full_single_repair("D1")
evs[5]["at"] = "2026-09-11T08:55:00Z"   # sign 08:55 < reinspect 09:00
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("签发早于复检 → REPAIR_NOT_SIGNED（须复检后重新签发）",
      find(r["violations"], "REPAIR_NOT_SIGNED", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# ================================================================ 11. 封闭层对应
jid = new_job()
lay(jid)
# 源层为最表层 P06：无后续封闭层，不应误报
evs = full_single_repair("D1", pid="P06", angle=0, day="11")
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("表层 P06 修补无封闭层 → 不报 PATCH_CLOSING_MISMATCH",
      find(r["violations"], "PATCH_CLOSING_MISMATCH", "D1") is None
      and r["release"] == "ok",
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

jid = new_job()
lay(jid, 4)   # 只铺到 P04；缺陷源层 P03，封闭层 P04 已在
# 正常情况应通过（FULL 全覆盖）
evs = full_single_repair("D1", pid="P03")
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("封闭层 P04 逐点覆盖补片 → 通过（另有 P05/P06 缺层属既有规则）",
      find(r["violations"], "PATCH_CLOSING_MISMATCH", "D1") is None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

jid = new_job()
# 封闭层实铺几何很小，盖不住补片
body = {
    "name": "t", "tool_datum": {"datum_id": "M1"},
    "zones": [{"zone_id": "Z1", "polygon": [[0, 0], [200, 0], [200, 60], [0, 60]]},
              {"zone_id": "Z2", "polygon": [[200, 0], [400, 0], [400, 60], [200, 60]]}],
    "spec": base_spec(),
    "rolls": [{"roll_id": "R1", "batch_no": "B1", "material": "CF-EP-3K",
               "out_time_limit_h": 240}]}
s, r = call("POST", "/jobs", body)
jid = r["job_id"]
call("POST", f"/jobs/{jid}/events", {"events": [
    {"type": "roll_thawed", "operator": "o", "roll": "R1", "at": T0}] + [
    {"type": "ply_placed", "operator": "o", "ply_id": f"P{i:02d}", "roll": "R1",
     "angle": ANGLES[i - 1], "face": "up",
     "geometry": FULL if i != 4 else [[0, 0], [120, 0], [120, 60], [0, 60]],
     "placed_at": f"2026-09-10T{7+i:02d}:00:00Z"} for i in range(1, 5)]})
# 先补齐 P05/P06 不铺（避免干扰，仅看 P04）；缺陷 P03 补片中心 100 宽 ~28，
# P04 只到 x=120，补片到 x=124/128 → 不被覆盖
evs = [found("D1", pid="P03", half=10), isolated(),
       removed("P03", box(100, 30, 22)),
       patch("P03", box(100, 30, 24), -45),
       reinspect(), sign()]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
v = find(r["violations"], "PATCH_CLOSING_MISMATCH", "D1")
check("封闭层未逐点覆盖补片 → PATCH_CLOSING_MISMATCH（P03/P04）",
      v is not None and set(v["plies"]) == {"P03", "P04"},
      json.dumps(v, ensure_ascii=False) if v else "none")

# ================================================================ 12. 开孔净距 / 接缝
jid = new_job()
lay(jid)
# 开孔 O1 在 x∈[360,390]；缺陷中心 350,half=8 → 342..358，净距 2mm < 20
evs = [found("D1", zone="Z2", cx=350, half=8), isolated(),
       removed("P03", box(350, 30, 20)),
       patch("P03", box(350, 30, 22), -45),
       reinspect(), sign()]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
v = find(r["violations"], "PATCH_CLEARANCE", "D1")
check("补片/缺陷距开孔净距不足 → PATCH_CLEARANCE",
      v is not None and v["details"]["opening_id"] == "O1"
      and v["details"]["required_mm"] == 20.0,
      json.dumps(v, ensure_ascii=False) if v else "none")

jid = new_job()
lay(jid)
# 补片接缝超隙 + 与封闭层 P04 接缝错开不足
evs = [found("D1", half=10), isolated(),
       removed("P03", box(100, 30, 22)),
       patch("P03", box(100, 30, 24), -45, seams=[
           {"zone": "Z1", "axis": "x", "at": 100.0, "gap": 3.0}]),  # 超隙
       reinspect(), sign()]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("补片接缝超隙 → PATCH_SEAM_GAP",
      find(r["violations"], "PATCH_SEAM_GAP", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# ================================================================ 13. 让步接收 / 报废
for disp, decision in (("use_as_is", "use_as_is"), ("reject", "reject")):
    jid = new_job()
    lay(jid)
    evs = [found("D1", half=10, disposition=disp),
           sign("D1", 0, decision=decision)]
    call("POST", f"/jobs/{jid}/events", {"events": evs})
    s, r = call("GET", f"/jobs/{jid}/validate")
    check(f"{disp} 签发后放行通过", r["release"] == "ok",
          json.dumps(r["violations"], ensure_ascii=False)[:300])

jid = new_job()
lay(jid)
# 让步却做了局部揭除 → 矛盾
evs = [found("D1", half=10, disposition="use_as_is"),
       removed("P03", box(100, 30, 20)),
       sign("D1", 0, decision="use_as_is")]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("让步接收却局部揭除 → DEFECT_STAGE_ORDER",
      find(r["violations"], "DEFECT_STAGE_ORDER", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# ================================================================ 14. 无 repairs 规范
jid = new_job(spec=base_spec(repairs=False))
lay(jid)
call("POST", f"/jobs/{jid}/events", {"events": [found("D1")]})
s, r = call("GET", f"/jobs/{jid}/validate")
check("无 repairs 规范却提交缺陷事件 → REPAIR_SPEC_MISSING",
      find(r["violations"], "REPAIR_SPEC_MISSING", "D1") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# 无规范也无缺陷事件：老工单兼容
jid = new_job(spec=base_spec(repairs=False))
lay(jid)
s, r = call("GET", f"/jobs/{jid}/validate")
check("无缺陷老工单兼容（repairs 空状态）",
      r["release"] == "ok", json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# ================================================================ 15. 牵涉已锁层
jid = new_job()
lay(jid, 6)
s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
check("锁层前置：先批准 v1", s.startswith("201"), f"[{s}] {r}")
# 批准后对已锁 P03 做局部揭除（事件 seq 在快照之后？不——快照含全部铺层，
# 局部揭除发生在批准之后，locked_cutoff = 快照最大 seq）
evs = full_single_repair("D1", day="12")
s, r = call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/validate")
v = find(r["violations"], "DEFECT_LOCKED_PLY", "D1")
check("对已批准锁定层局部揭除 → DEFECT_LOCKED_PLY",
      v is not None and v["plies"] == ["P03"],
      json.dumps(v, ensure_ascii=False) if v else json.dumps(
          rules_of(r["violations"]), ensure_ascii=False))

# 先局部修补、后批准：批准后不再报锁层（揭除发生在快照冻结窗口之前，
# 且批准时该缺陷必须已闭环；此处用“批准后新增缺陷”对照）
jid = new_job()
lay(jid)
evs = full_single_repair("D1")
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
check("闭环缺陷随层批准 v1", s.startswith("201"), f"[{s}] {r}")
# 批准后新铺 P07（未锁），在 P07 上修补不得报锁层
spec7 = base_spec()
spec7["plies"].append({"seq": 7, "ply_id": "P07", "material": "CF-EP-3K",
                       "angle": 0, "face": "up", "zones": ["Z1", "Z2"]})
s, rr = call("POST", f"/jobs/{jid}/spec-revisions", {
    "spec": spec7, "reason": "端部加层",
    "effective_at": "2026-09-12T08:00:00Z"})
rev = rr.get("revision")
call("POST", f"/jobs/{jid}/spec-revisions/{rev}/confirm")
call("POST", f"/jobs/{jid}/events", {"events": [
    {"type": "ply_placed", "operator": "o", "ply_id": "P07", "roll": "R1",
     "angle": 0, "face": "up", "geometry": FULL,
     "placed_at": "2026-09-12T09:00:00Z"}]})
evs2 = full_single_repair("D2", pid="P07", angle=0, day="13")
call("POST", f"/jobs/{jid}/events", {"events": evs2})
s, r = call("GET", f"/jobs/{jid}/validate")
check("未锁定的新铺层 P07 修补不报 DEFECT_LOCKED_PLY",
      find(r["violations"], "DEFECT_LOCKED_PLY", "D2") is None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# ================================================================ 16. 轮廓变化：只撤销相关处置（代际）
jid = new_job()
lay(jid)
call("POST", f"/jobs/{jid}/events", {"events": full_single_repair("D1")})
# 另有一个独立缺陷 D2 也闭环
call("POST", f"/jobs/{jid}/events",
     {"events": full_single_repair("D2", cx=150, day="11")})
s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
check("代际前置：两缺陷闭环后批准", s.startswith("201"), f"[{s}] {r}")
# D1 复拍发现轮廓扩大 → 轮廓更新事件开启新一代；旧签发只对旧轮廓有效
call("POST", f"/jobs/{jid}/events", {"events": [
    {"type": "defect_contour_updated", "operator": "qc-li", "defect_id": "D1",
     "polygon": box(100, 30, 14), "reason": "复拍确认实际边界更大",
     "photo_digest": "sha256:D1b",
     "at": "2026-09-13T08:00:00Z"}]})
s, d = call("GET", f"/jobs/{jid}/defects/D1")
check("轮廓变化：当前代=1，旧代留痕",
      d["current_generation"] == 1 and len(d["generations"]) == 2
      and d["generations"][0]["sign"] is not None,
      json.dumps({"gen": d["current_generation"],
                  "gens": len(d["generations"])}, ensure_ascii=False))
s, r = call("GET", f"/jobs/{jid}/validate")
check("新一代未处置 → 旧签发不再使其闭合（REPAIR_NOT_SIGNED）",
      find(r["violations"], "REPAIR_NOT_SIGNED", "D1") is not None
      and find(r["violations"], "REPAIR_NOT_SIGNED", "D2") is None,
      json.dumps([(v["rule"], v["details"].get("defects"))
                  for v in r["violations"]], ensure_ascii=False))
# 旧版 package 仍收录旧代几何/事件/人工理由
_s, pkg = call("GET", f"/jobs/{jid}/approvals/1/package")
pd1 = next(d for d in pkg["repairs"]["defects"] if d["defect_id"] == "D1")
check("旧版随件包：旧代几何/签发理由/事件引用不变",
      pd1["current_generation"] == 0
      and pd1["generations"][0]["sign"]["reason"]
      and pd1["generations"][0]["sign"]["instruction"] == "RP-2026-A"
      and pd1["contour"] == box(100, 30, 10),
      json.dumps(pd1["generations"][0], ensure_ascii=False)[:300])
# 按新一代重做并重新签发 → 重新闭环；D2 始终不受影响
new_evs = [isolated("D1", "13", "08:05"),
           removed("P03", box(100, 30, 26), "D1", "13", "08:20"),
           patch("P03", box(100, 30, 42), -45, "D1", "13", "08:40"),
           reinspect("D1", "pass", "13", "09:00"),
           sign("D1", 1, day="13", hm="09:10",
                reason="按扩大后轮廓重新挖补，复检通过")]
call("POST", f"/jobs/{jid}/events", {"events": new_evs})
s, r = call("GET", f"/jobs/{jid}/validate")
check("新一代重新处置+签发后两缺陷均闭环",
      r["release"] == "ok",
      json.dumps(r["violations"], ensure_ascii=False)[:400])
s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
check("重新闭环后可再批准 v2", s.startswith("201"), f"[{s}] {r}")
_s, dd = call("GET", f"/jobs/{jid}/approvals/diff", query="a=1&b=2")
repdiff = dd["diff"]["repairs"]
check("版本比较：D1 代际/状态变化可识别，D2 不在变化列表",
      {"D1"} == {x["defect_id"] for x in repdiff["defects_changed"]}
      and repdiff["instruction_version"]["a"] == "RP-2026-A",
      json.dumps(repdiff, ensure_ascii=False)[:400])

# ================================================================ 17. 源层返工：只撤销该缺陷处置
jid = new_job()
lay(jid)
call("POST", f"/jobs/{jid}/events",
     {"events": full_single_repair("D1")})
call("POST", f"/jobs/{jid}/events",
     {"events": full_single_repair("D2", cx=150)})
call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
# 源层 P03 整层返工
call("POST", f"/jobs/{jid}/events", {"events": [
    {"type": "ply_removed", "operator": "o", "ply_id": "P03", "reason": "整层返工"},
    {"type": "ply_replaced", "operator": "o", "removed_ply_id": "P03",
     "replacement": {"ply_id": "P03", "roll": "R1", "angle": -45, "face": "up",
                     "geometry": FULL,
                     "placed_at": "2026-09-13T10:00:00Z"}}]})
s, r = call("GET", f"/jobs/{jid}/validate")
check("源层整层返工后只撤销 D1（REPAIR_SOURCE_REWORKED），D2 不受影响",
      find(r["violations"], "REPAIR_SOURCE_REWORKED", "D1") is not None
      and find(r["violations"], "REPAIR_SOURCE_REWORKED", "D2") is None,
      json.dumps([(v["rule"], v["details"].get("defects"))
                  for v in r["violations"]], ensure_ascii=False))

# ================================================================ 18. 签发后规范换版（repairs 块变化）
jid = new_job()
lay(jid)
call("POST", f"/jobs/{jid}/events",
     {"events": full_single_repair("D1")})
# 换版：只改 repairs 指令版本（铺层规范不变，全部实铺沿用）
new_spec = base_spec()
new_spec["repairs"] = repairs_spec(version="RP-2027-B")
s, rr = call("POST", f"/jobs/{jid}/spec-revisions", {
    "spec": new_spec, "reason": "修理指令换版：搭接 15→15，版本升级",
    "effective_at": "2026-09-12T08:00:00Z"})
rev = rr.get("revision")
check("repairs 换版提议 201", s.startswith("201"), f"[{s}] {rr}")
call("POST", f"/jobs/{jid}/spec-revisions/{rev}/confirm")
s, r = call("GET", f"/jobs/{jid}/validate")
v = find(r["violations"], "REPAIR_SPEC_SUPERSEDED", "D1")
check("签发后指令换版 → REPAIR_SPEC_SUPERSEDED（旧签发撤销）",
      v is not None and v["details"]["revision"] == rev,
      json.dumps(v, ensure_ascii=False) if v else
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))
# 按新指令重新签发（事件在换版确认之后）
call("POST", f"/jobs/{jid}/events", {"events": [
    sign("D1", 0, instruction="RP-2027-B", day="12", hm="10:00",
         reason="按 RP-2027-B 复核几何/搭接，符合新版")]})
s, r = call("GET", f"/jobs/{jid}/validate")
check("按新版重新签发后闭环",
      r["release"] == "ok",
      json.dumps(r["violations"], ensure_ascii=False)[:400])

# 换版但 repairs 块不变：旧签发仍然有效
jid = new_job()
lay(jid)
call("POST", f"/jobs/{jid}/events",
     {"events": full_single_repair("D1")})
new_spec = base_spec()
new_spec["plies"].append({"seq": 7, "ply_id": "P07", "material": "CF-EP-3K",
                          "angle": 0, "face": "up", "zones": ["Z1", "Z2"]})
s, rr = call("POST", f"/jobs/{jid}/spec-revisions", {
    "spec": new_spec, "reason": "仅铺层加层，修理指令不变",
    "effective_at": "2026-09-12T08:00:00Z"})
rev = rr["revision"]
call("POST", f"/jobs/{jid}/spec-revisions/{rev}/confirm")
s, r = call("GET", f"/jobs/{jid}/validate")
check("换版不涉 repairs：旧签发不撤销（仅 P07 缺层）",
      find(r["violations"], "REPAIR_SPEC_SUPERSEDED", "D1") is None
      and find(r["violations"], "MISSING_PLY") is not None,
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# ================================================================ 19. 入链载荷校验
jid = new_job()
lay(jid)
s, r = call("POST", f"/jobs/{jid}/events", {"events": [
    {"type": "defect_found", "operator": "q", "defect_id": "X",
     "defect_type": "delamination", "ply_id": "P03", "zone": "Z1",
     "polygon": [[0, 0], [10, 0], [10, 10]], "disposition": "repair",
     "at": "2026-09-12T08:00:00Z"}]})
check("缺 photo_summary/多边形退化 → 400", s.startswith("400"), f"[{s}] {r}")
s, r = call("POST", f"/jobs/{jid}/events", {"events": [
    {"type": "patch_placed", "operator": "o", "defect_id": "X",
     "ply_id": "P03", "polygon": box(1, 1, 1), "angle": -45, "face": "up",
     "placed_at": "bad-time"}]})
check("补片时刻无法解析 → 400", s.startswith("400"), f"[{s}] {r}")
s, r = call("POST", f"/jobs/{jid}/events", {"events": [
    {"type": "repair_signed", "operator": "e", "defect_id": "X",
     "generation": 0, "decision": "confirmed", "instruction": "RP",
     "reason": "r", "signed_by": "e", "at": "2026-09-12T08:00:00Z",
     "extra_hack": 1}]})
# 多余字段允许（载荷按白名单落库时剔 type/operator，其余保留）
check("签发额外字段不影响入链 201", s.startswith("201"), f"[{s}] {r}")

# 孤儿处置事件（未登记缺陷）
s, r = call("POST", f"/jobs/{jid}/events", {"events": [
    isolated("GHOST", "12")]})
s, r = call("GET", f"/jobs/{jid}/validate")
check("无 defect_found 的处置事件 → DEFECT_ORPHAN_EVENT",
      any(v["rule"] == "DEFECT_ORPHAN_EVENT"
          and "GHOST" in v["details"]["defects"]
          for v in r["violations"]),
      json.dumps(sorted(rules_of(r["violations"])), ensure_ascii=False))

# ================================================================ 20. 快照随件包内容
jid = new_job()
lay(jid)
call("POST", f"/jobs/{jid}/events",
     {"events": full_single_repair("D1")})
s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
_s, pkg = call("GET", f"/jobs/{jid}/approvals/1/package")
pd1 = next(d for d in pkg["repairs"]["defects"] if d["defect_id"] == "D1")
g0 = pd1["generations"][0]
check("随件包收录：指令版本/开孔/照片摘要/几何/事件链/人工理由",
      pkg["repairs"]["instruction_version"] == "RP-2026-A"
      and pkg["repairs"]["openings"][0]["opening_id"] == "O1"
      and pd1["photo_summary"]
      and pd1["photo_digest"] == "sha256:D1"
      and g0["removals"][0]["polygon"] == box(100, 30, 22)
      and g0["patches"][0]["angle"] == -45
      and g0["reinspections"][0]["result"] == "pass"
      and g0["sign"]["reason"] and g0["sign"]["signed_by"]
      and g0["events"],
      json.dumps(g0, ensure_ascii=False)[:500])
# 事件链本身也含全部处置事件（同一追加链 + chain_hash 覆盖）
types = [e["type"] for e in pkg["events"]]
check("处置事件在同一追加事件链上且被 chain_hash 覆盖",
      {"defect_found", "defect_isolated", "defect_ply_removed",
       "patch_placed", "defect_reinspected", "repair_signed"} <= set(types)
      and len(pkg["chain_hash"]) == 64,
      str(sorted(set(types))))

# 缺陷列表接口
s, r = call("GET", f"/jobs/{jid}/defects")
check("缺陷列表：摘要含状态/层位/分区/代际",
      s.startswith("200") and r["defects"][0]["defect_id"] == "D1"
      and r["defects"][0]["status"] == "signed"
      and r["instruction_version"] == "RP-2026-A",
      json.dumps(r, ensure_ascii=False)[:300])
s, r = call("GET", f"/jobs/{jid}/defects/NOPE")
check("查询不存在缺陷 → 404", s.startswith("404"), f"[{s}]")

print(f"\n{sum(results)}/{len(results)} 通过")
raise SystemExit(0 if all(results) else 1)
