#!/usr/bin/env python3
"""规范换版覆盖测试。

场景覆盖：
  * 提议换版：逐层比较（材料/层序/角度/正反面/覆盖区/接缝/丢层边界），
    标出可沿用实铺层；中间层变化时给出确定的返工序列（连带揭除上覆层、
    失效压实检查点、待补铺层）；
  * 冲突不启用：层映射多解（重复层号/层序、显式映射多对一）、分区基准
    不兼容、生效时刻早于现场记录、已批准锁定层受影响；
  * 显式层映射（重编号）正向配对；
  * 确认后固定新规范与处置决定：校验/批准/随件包从换版分支重算，
    铺放事件链保持只读，返工仍经 ply_removed/ply_replaced 闭环；
  * 派生链：默认基线为当前生效版，基线过期（stale_base）拒绝确认；
  * 无换版老工单兼容（spec_revision=0）。
"""

import io
import json
import os
import tempfile

from prepreg_release import make_app

DB = os.path.join(tempfile.gettempdir(), "prepreg_revision_test.db")
if os.path.exists(DB):
    os.remove(DB)
app = make_app(DB)

FULL = [[-1, -1], [401, -1], [401, 61], [-1, 61]]
ANGLES = [0, 45, -45, -45, 45, 0]
EFF = "2026-09-12T08:00:00Z"   # 生效时刻：晚于全部现场记录


def call(method, path, body=None, query=""):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
    cap = {}

    def sr(status, headers, exc_info=None):
        cap["status"] = status

    env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
           "CONTENT_LENGTH": str(len(data)), "wsgi.input": io.BytesIO(data)}
    payload = json.loads(b"".join(app(env, sr)))
    return cap["status"], payload


# ---------------------------------------------------------------- 构造器

def base_plies():
    return [{"seq": i, "ply_id": f"P{i:02d}", "material": "CF-EP-3K",
             "angle": a, "face": "up", "zones": ["Z1", "Z2"]}
            for i, a in enumerate(ANGLES, 1)]


def base_spec(compaction=False):
    spec = {"materials": {"CF-EP-3K": {"ply_thickness": 0.125}},
            "plies": base_plies(),
            "rules": {"seam_min_stagger_mm": 25.0, "seam_max_gap_mm": 1.5,
                      "max_consecutive_same_angle": 4}}
    if compaction:
        spec["compaction"] = {
            "defaults": {"target_abs_kpa": 12.0, "hold_seconds": 60,
                         "max_sample_interval_s": 30,
                         "max_rise_kpa_min": 2.0, "leak_test_seconds": 60},
            "checkpoints": [{"checkpoint_id": "CP1", "after_seq": 2},
                            {"checkpoint_id": "CP2", "after_seq": 5}]}
    return spec


def new_job(compaction=False):
    body = {
        "name": "revision-test",
        "tool_datum": {"datum_id": "M1"},
        "zones": [
            {"zone_id": "Z1", "polygon": [[0, 0], [200, 0], [200, 60], [0, 60]],
             "adjacent": ["Z2"]},
            {"zone_id": "Z2", "polygon": [[200, 0], [400, 0], [400, 60], [200, 60]],
             "adjacent": ["Z1"]}],
        "spec": base_spec(compaction),
        "rolls": [{"roll_id": "R1", "batch_no": "B1", "material": "CF-EP-3K",
                   "out_time_limit_h": 240}],
    }
    _s, r = call("POST", "/jobs", body)
    return r["job_id"]


def placed(pid, angle, t, roll="R1", **kw):
    e = {"type": "ply_placed", "operator": "op1", "ply_id": pid, "roll": roll,
         "angle": angle, "face": "up", "geometry": FULL, "placed_at": t}
    e.update(kw)
    return e


def lay(jid, n=6):
    """解冻 R1 并依次铺 P01..Pn（角度与规范一致）。"""
    evs = [{"type": "roll_thawed", "operator": "op1", "roll": "R1",
            "at": "2026-09-10T06:00:00Z"}]
    for i in range(1, n + 1):
        evs.append(placed(f"P{i:02d}", ANGLES[i - 1],
                          f"2026-09-10T{7 + i:02d}:00:00Z"))
    call("POST", f"/jobs/{jid}/events", {"events": evs})


def propose(jid, spec, **kw):
    body = {"spec": spec, "reason": kw.pop("reason", "工艺换版"),
            "effective_at": kw.pop("effective_at", EFF)}
    body.update(kw)
    return call("POST", f"/jobs/{jid}/spec-revisions", body)


def spec_with(**ply_changes):
    """在基础规范上按 ply_id 修改字段，返回新规范。"""
    spec = base_spec()
    for sp in spec["plies"]:
        chg = ply_changes.get(sp["ply_id"])
        if chg:
            sp.update(chg)
    return spec


results = []


def check(name, cond, info=""):
    ok = bool(cond)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": {info}"))
    results.append(ok)


def conflict_codes(r):
    return {c["code"] for c in r.get("conflicts") or []}


# ---------------------------------------------------------------- 1. 材料替代：影响分析
jid = new_job()
lay(jid, 3)
spec = spec_with(P03={"material": "CF-EP-3K-B"})
spec["materials"]["CF-EP-3K-B"] = {"ply_thickness": 0.125}
s, r = propose(jid, spec, reason="材料替代：CF-EP-3K 停产")
imp = r.get("impact") or {}
rw = imp.get("rework") or {}
check("材料替代提议 201", s.startswith("201"), f"[{s}] {r}")
check("材料替代：P01/P02 沿用",
      [c["ply_id"] for c in imp.get("carry_over", [])] == ["P01", "P02"],
      json.dumps(imp.get("carry_over"), ensure_ascii=False))
check("材料替代：P03 须揭除（changed）",
      [(x["ply_id"], x["reason"]) for x in rw.get("remove", [])]
      == [("P03", "changed")],
      json.dumps(rw.get("remove"), ensure_ascii=False))
check("材料替代：差异明细含 material 字段",
      imp.get("changed", [{}])[0].get("diffs")
      == [{"field": "material", "from": "CF-EP-3K", "to": "CF-EP-3K-B"}],
      json.dumps(imp.get("changed"), ensure_ascii=False))
check("材料替代：待补铺序列 P03(changed)→P04..P06(pending)",
      [(x["ply_id"], x["reason"]) for x in rw.get("relay", [])]
      == [("P03", "changed"), ("P04", "pending"), ("P05", "pending"),
          ("P06", "pending")],
      json.dumps(rw.get("relay"), ensure_ascii=False))

# ---------------------------------------------------------------- 2. 顶层变化：无连带，检查点部分失效
jid = new_job(compaction=True)
lay(jid, 4)
s, r = propose(jid, spec_with(P04={"angle": 30}))
rw = r["impact"]["rework"]
check("顶层变化：仅 P04 揭除",
      [(x["ply_id"], x["reason"]) for x in rw["remove"]] == [("P04", "changed")],
      json.dumps(rw["remove"], ensure_ascii=False))
check("顶层变化：仅 CP2 失效（CP1 不含 P04）",
      rw["invalidated_checkpoints"] == ["CP2"],
      json.dumps(rw["invalidated_checkpoints"], ensure_ascii=False))

# ---------------------------------------------------------------- 3. 中间层变化：上覆连带，检查点全失效
jid = new_job(compaction=True)
lay(jid, 4)
s, r = propose(jid, spec_with(P02={"angle": 30}))
rw = r["impact"]["rework"]
check("中间层变化：揭除序列自上而下 P04→P03→P02",
      [(x["ply_id"], x["reason"]) for x in rw["remove"]]
      == [("P04", "overlying"), ("P03", "overlying"), ("P02", "changed")],
      json.dumps(rw["remove"], ensure_ascii=False))
check("中间层变化：CP1/CP2 均失效",
      rw["invalidated_checkpoints"] == ["CP1", "CP2"],
      json.dumps(rw["invalidated_checkpoints"], ensure_ascii=False))
check("中间层变化：仅 P01 沿用",
      [c["ply_id"] for c in r["impact"]["carry_over"]] == ["P01"],
      json.dumps(r["impact"]["carry_over"], ensure_ascii=False))
check("中间层变化：补铺序列 P02(changed)→P03/P04(relay)→P05/P06(pending)",
      [(x["ply_id"], x["reason"]) for x in rw["relay"]]
      == [("P02", "changed"), ("P03", "relay"), ("P04", "relay"),
          ("P05", "pending"), ("P06", "pending")],
      json.dumps(rw["relay"], ensure_ascii=False))

# ---------------------------------------------------------------- 4. 逐字段比较
jid = new_job()
lay(jid, 6)
spec = spec_with(
    P01={"angle": 5}, P02={"face": "down"}, P03={"zones": ["Z1"]},
    P04={"drop_at": 120.0}, P06={"seq": 7})
spec["plies"][4]["seams"] = [{"zone": "Z1", "axis": "x", "at": 100.0}]
s, r = propose(jid, spec)
fields = {c["ply_id"]: [d["field"] for d in c["diffs"]]
          for c in r["impact"]["changed"]}
check("逐字段比较：角度/正反面/覆盖区/丢层/接缝/层序各就各位",
      fields == {"P01": ["angle"], "P02": ["face"], "P03": ["zones"],
                 "P04": ["drop_at"], "P05": ["seams"], "P06": ["seq"]},
      json.dumps(fields, ensure_ascii=False))

# ---------------------------------------------------------------- 5-7. 层映射多解
jid = new_job()
lay(jid, 3)
spec = base_spec()
spec["plies"].append(dict(spec["plies"][2]))          # 两个 P03
s, r = propose(jid, spec)
check("映射多解：新规范层号重复 → 409 MAPPING_AMBIGUOUS",
      s.startswith("409") and "MAPPING_AMBIGUOUS" in conflict_codes(r),
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:200]}")

spec = base_spec()
spec["plies"][3]["seq"] = spec["plies"][2]["seq"]     # P04 与 P03 同层序
s, r = propose(jid, spec)
check("映射多解：新规范层序重复 → 409 MAPPING_AMBIGUOUS",
      s.startswith("409") and "MAPPING_AMBIGUOUS" in conflict_codes(r),
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:200]}")

spec = {"materials": {"CF-EP-3K": {"ply_thickness": 0.125}},
        "plies": [{"seq": 1, "ply_id": "N1", "material": "CF-EP-3K",
                   "angle": 0, "face": "up", "zones": ["Z1", "Z2"]},
                  {"seq": 2, "ply_id": "N2", "material": "CF-EP-3K",
                   "angle": 0, "face": "up", "zones": ["Z1", "Z2"]}]}
s, r = propose(jid, spec, mapping={"N1": "P01", "N2": "P01"})
check("映射多解：显式映射多对一 → 409 MAPPING_AMBIGUOUS",
      s.startswith("409") and "MAPPING_AMBIGUOUS" in conflict_codes(r),
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:200]}")

# ---------------------------------------------------------------- 8. 显式映射重编号（正向）
jid = new_job()
lay(jid, 2)
spec = base_spec()
spec["plies"][0] = {"seq": 1, "ply_id": "N01", "material": "CF-EP-3K",
                    "angle": 0, "face": "up", "zones": ["Z1", "Z2"]}
s, r = propose(jid, spec, mapping={"N01": "P01"})
imp = r.get("impact") or {}
check("显式映射重编号：N01←P01 未变，P01 实铺沿用",
      s.startswith("201")
      and imp.get("mapping", {}).get("N01") == "P01"
      and {"ply_id": "N01", "old_ply_id": "P01"} in imp.get("unchanged", [])
      and imp["carry_over"][0]["new_ply_id"] == "N01",
      f"[{s}] {json.dumps(imp, ensure_ascii=False)[:300]}")

# ---------------------------------------------------------------- 9-10. 分区基准不兼容
jid = new_job()
lay(jid, 2)
spec = spec_with(P02={"zones": ["Z1", "Z9"]})
s, r = propose(jid, spec)
check("分区基准：引用未定义分区 → 409 ZONE_DATUM_INCOMPATIBLE",
      s.startswith("409") and "ZONE_DATUM_INCOMPATIBLE" in conflict_codes(r),
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:200]}")

s, r = propose(jid, base_spec(),
               zones=[{"zone_id": "Z1",
                       "polygon": [[0, 0], [100, 0], [100, 60], [0, 60]]}])
check("分区基准：随迁更改分区边界 → 409 ZONE_DATUM_INCOMPATIBLE",
      s.startswith("409") and "ZONE_DATUM_INCOMPATIBLE" in conflict_codes(r),
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:200]}")
s, r = propose(jid, base_spec(), tool_datum={"datum_id": "M2"})
check("分区基准：随迁更改模具基准 → 409 ZONE_DATUM_INCOMPATIBLE",
      s.startswith("409") and "ZONE_DATUM_INCOMPATIBLE" in conflict_codes(r),
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:200]}")

# ---------------------------------------------------------------- 11. 生效时刻早于现场记录
jid = new_job()
lay(jid, 2)
s, r = propose(jid, base_spec(), effective_at="2026-09-09T00:00:00Z")
check("生效时刻早于现场记录 → 409 EFFECTIVE_BEFORE_RECORDS",
      s.startswith("409") and "EFFECTIVE_BEFORE_RECORDS" in conflict_codes(r),
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:200]}")

# ---------------------------------------------------------------- 12-13. 已锁层
jid = new_job()
lay(jid, 6)
call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
s, r = propose(jid, spec_with(P02={"angle": 30}))
locked = next((c for c in r.get("conflicts", [])
               if c["code"] == "LOCKED_PLY_AFFECTED"), {})
check("已锁层受影响 → 409 LOCKED_PLY_AFFECTED（含连带层）",
      s.startswith("409")
      and locked.get("plies") == ["P02", "P03", "P04", "P05", "P06"],
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:300]}")

# 批准后才铺的层不在快照内，可随再次换版调整
spec = base_spec()
spec["plies"].append({"seq": 7, "ply_id": "P07", "material": "CF-EP-3K",
                      "angle": 0, "face": "up", "zones": ["Z1", "Z2"]})
spec["rules"]["require_symmetry"] = False
s, r = propose(jid, spec)
call("POST", f"/jobs/{jid}/spec-revisions/{r['revision']}/confirm")
call("POST", f"/jobs/{jid}/events",
     {"events": [placed("P07", 0, "2026-09-12T09:00:00Z")]})
_s, job = call("GET", f"/jobs/{jid}")
spec2 = job["spec"]
spec2["plies"][6]["angle"] = 15                       # 改未锁定的 P07
s, r = propose(jid, spec2, effective_at="2026-09-12T10:00:00Z")
check("批准后新铺层未被旧快照锁定 → 201 且仅揭除该层",
      s.startswith("201")
      and [(x["ply_id"], x["reason"])
           for x in r["impact"]["rework"]["remove"]] == [("P07", "changed")],
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:300]}")

# ---------------------------------------------------------------- 14-15. 确认后走新分支；事件链只读
jid = new_job()
lay(jid, 6)
_s, before = call("GET", f"/jobs/{jid}/events")
spec = spec_with(P06={"material": "CF-EP-3K-B"})
spec["materials"]["CF-EP-3K-B"] = {"ply_thickness": 0.125}
s, r = propose(jid, spec, reason="材料替代：P06 改用 CF-EP-3K-B")
rev_no, new_hash = r["revision"], r["spec_hash"]
_s, after = call("GET", f"/jobs/{jid}/events")
check("换版提议/确认不改事件链（只读）", before == after,
      "事件链发生变化")
s, r = call("POST", f"/jobs/{jid}/spec-revisions/{rev_no}/confirm")
check("确认换版 → 200 confirmed",
      s.startswith("200") and r.get("status") == "confirmed"
      and r.get("spec_hash") == new_hash,
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:200]}")
_s, job = call("GET", f"/jobs/{jid}")
check("确认后工单切到换版分支（spec_revision=1，新规范生效）",
      job.get("spec_revision") == 1
      and job["spec"]["plies"][5]["material"] == "CF-EP-3K-B",
      json.dumps({"spec_revision": job.get("spec_revision")}, ensure_ascii=False))
_s, r = call("GET", f"/jobs/{jid}/validate")
check("确认后校验走新分支：旧料卷 P06 报 MATERIAL_MISMATCH",
      "MATERIAL_MISMATCH" in {v["rule"] for v in r["violations"]},
      json.dumps([v["rule"] for v in r["violations"]], ensure_ascii=False))

call("POST", f"/jobs/{jid}/rolls",
     {"roll_id": "R2", "batch_no": "B2", "material": "CF-EP-3K-B",
      "out_time_limit_h": 240})
call("POST", f"/jobs/{jid}/events", {"events": [
    {"type": "roll_thawed", "operator": "op1", "roll": "R2",
     "at": "2026-09-12T09:00:00Z"},
    {"type": "ply_removed", "operator": "op1", "ply_id": "P06",
     "reason": "规范换版：材料替代"},
    {"type": "ply_replaced", "operator": "op1", "removed_ply_id": "P06",
     "replacement": {"ply_id": "P06", "roll": "R2", "angle": 0, "face": "up",
                     "geometry": FULL, "placed_at": "2026-09-12T10:00:00Z"}}]})
_s, r = call("GET", f"/jobs/{jid}/validate")
check("按返工序列换料重铺后校验通过", r["release"] == "ok",
      json.dumps(r["violations"], ensure_ascii=False)[:300])
s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
check("换版分支批准 v1（spec_hash 为新版）",
      s.startswith("201") and r.get("spec_hash") == new_hash,
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:200]}")
_s, pkg = call("GET", f"/jobs/{jid}/approvals/1/package")
check("随件包取自换版分支（P06 材料 CF-EP-3K-B）",
      pkg["spec"]["plies"][5]["material"] == "CF-EP-3K-B"
      and pkg["spec_hash"] == new_hash,
      json.dumps(pkg["spec"]["plies"][5], ensure_ascii=False))

# ---------------------------------------------------------------- 16. 派生链与基线过期
jid = new_job()
lay(jid, 6)
spec = spec_with(P06={"material": "CF-EP-3K-B"})
spec["materials"]["CF-EP-3K-B"] = {"ply_thickness": 0.125}
s, r = propose(jid, spec)
rev1 = r["revision"]
call("POST", f"/jobs/{jid}/spec-revisions/{rev1}/confirm")
_s, job = call("GET", f"/jobs/{jid}")
spec2 = job["spec"]
spec2["plies"][4]["angle"] = 30                       # v2：基于 v1 改 P05
s, r = propose(jid, spec2)
rev2 = r["revision"]
check("派生链：v2 默认基线为当前生效版 v1",
      s.startswith("201") and r.get("base_revision") == 1,
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:200]}")
call("POST", f"/jobs/{jid}/spec-revisions/{rev2}/confirm")
s, r = propose(jid, spec_with(P04={"angle": 30}), base_revision=0)
rev3 = r["revision"]
s, r = call("POST", f"/jobs/{jid}/spec-revisions/{rev3}/confirm")
check("基线过期：基于 v0 的提议在 v2 生效后确认 → 409 stale_base",
      s.startswith("409") and r.get("error") == "stale_base",
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:200]}")
_s, r = call("GET", f"/jobs/{jid}/spec-revisions")
check("换版列表：v1/v2 confirmed，v3 proposed",
      [x["status"] for x in r["revisions"]]
      == ["confirmed", "confirmed", "proposed"],
      json.dumps(r["revisions"], ensure_ascii=False)[:300])

# ---------------------------------------------------------------- 17. 重复确认
jid = new_job()
lay(jid, 2)
s, r = propose(jid, spec_with(P05={"angle": 30}))
rev = r["revision"]
call("POST", f"/jobs/{jid}/spec-revisions/{rev}/confirm")
s, r = call("POST", f"/jobs/{jid}/spec-revisions/{rev}/confirm")
check("重复确认 → 409 already_confirmed",
      s.startswith("409") and r.get("error") == "already_confirmed",
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:200]}")

# ---------------------------------------------------------------- 18. 批准后换版：新增层 → 补铺 → 再批准 → diff
jid = new_job()
lay(jid, 6)
call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
spec = base_spec()
spec["plies"].append({"seq": 7, "ply_id": "P07", "material": "CF-EP-3K",
                      "angle": 0, "face": "up", "zones": ["Z1", "Z2"]})
spec["rules"]["require_symmetry"] = False
s, r = propose(jid, spec, reason="开孔边界变化：端部补一层 0° 增强")
rw = r["impact"]["rework"]
check("批准后新增层：无揭除、全部沿用、P07 待补铺",
      s.startswith("201") and rw["remove"] == []
      and len(r["impact"]["carry_over"]) == 6
      and [(x["ply_id"], x["reason"]) for x in rw["relay"]]
      == [("P07", "added")],
      f"[{s}] {json.dumps(r.get('impact'), ensure_ascii=False)[:300]}")
rev = r["revision"]
call("POST", f"/jobs/{jid}/spec-revisions/{rev}/confirm")
_s, job = call("GET", f"/jobs/{jid}")
check("批准后确认换版：工单回到待放行（open）",
      job["status"] == "open" and job["spec_revision"] == 1,
      json.dumps({"status": job["status"]}, ensure_ascii=False))
_s, r = call("GET", f"/jobs/{jid}/validate")
check("换版后校验：新增层 P07 报 MISSING_PLY",
      "MISSING_PLY" in {v["rule"] for v in r["violations"]},
      json.dumps([v["rule"] for v in r["violations"]], ensure_ascii=False))
call("POST", f"/jobs/{jid}/events",
     {"events": [placed("P07", 0, "2026-09-12T09:00:00Z")]})
_s, r = call("GET", f"/jobs/{jid}/validate")
check("补铺 P07 后校验通过", r["release"] == "ok",
      json.dumps(r["violations"], ensure_ascii=False)[:300])
s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
check("换版分支再批准 v2", s.startswith("201") and r.get("version") == 2,
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:200]}")
_s, r = call("GET", f"/jobs/{jid}/approvals/diff", query="a=1&b=2")
check("版本比较：规范已变更、层数 6→7",
      r["diff"]["spec_changed"] is True
      and r["diff"]["ply_count"] == {"a": 6, "b": 7},
      json.dumps(r["diff"], ensure_ascii=False)[:300])

# ---------------------------------------------------------------- 19-20. 参数校验
jid = new_job()
s, r = propose(jid, base_spec(), base_revision=99)
check("基线版本不存在 → 404", s.startswith("404")
      and r.get("error") == "base_revision_not_found",
      f"[{s}] {json.dumps(r, ensure_ascii=False)[:200]}")
s, r = call("POST", f"/jobs/{jid}/spec-revisions",
            {"spec": base_spec(), "effective_at": EFF})
check("缺变更理由 → 400", s.startswith("400")
      and r.get("error") == "missing_fields", f"[{s}] {r}")
s, r = call("POST", f"/jobs/{jid}/spec-revisions",
            {"spec": base_spec(), "reason": "x"})
check("缺生效时刻 → 400", s.startswith("400")
      and r.get("error") == "invalid_effective_at", f"[{s}] {r}")
s, r = call("POST", f"/jobs/{jid}/spec-revisions",
            {"spec": {"plies": []}, "reason": "x", "effective_at": EFF})
check("空铺层规范 → 400", s.startswith("400")
      and r.get("error") == "invalid_spec", f"[{s}] {r}")

# ---------------------------------------------------------------- 21. 换版详情
_s, r = call("GET", f"/jobs/{jid}/spec-revisions")
check("无换版工单列表为空", r.get("revisions") == [], json.dumps(r))
jid2 = new_job()
lay(jid2, 3)
s, r = propose(jid2, spec_with(P03={"angle": 30}), reason="丢层调整")
rev = r["revision"]
_s, r = call("GET", f"/jobs/{jid2}/spec-revisions/{rev}")
check("换版详情：含理由/生效时刻/映射/返工序列",
      r.get("reason") == "丢层调整" and r.get("effective_at") == EFF
      and r.get("status") == "proposed"
      and r["impact"]["mapping"]["P03"] == "P03"
      and r["impact"]["rework"]["remove"][0]["ply_id"] == "P03",
      json.dumps(r, ensure_ascii=False)[:300])

# ---------------------------------------------------------------- 22. 老工单兼容
jid = new_job()
lay(jid, 6)
_s, job = call("GET", f"/jobs/{jid}")
_s, r = call("GET", f"/jobs/{jid}/validate")
check("老工单兼容：spec_revision=0 且校验通过",
      job.get("spec_revision") == 0 and r["release"] == "ok",
      json.dumps({"spec_revision": job.get("spec_revision"),
                  "release": r["release"]}, ensure_ascii=False))

print(f"\n{sum(results)}/{len(results)} 通过")
raise SystemExit(0 if all(results) else 1)
