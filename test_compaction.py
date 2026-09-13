#!/usr/bin/env python3
"""阶段压实与真空袋检漏规则覆盖测试。

每类违规构造最小场景，断言规则码、检查点号、层号与区域，并验证：
  * 返工揭除/替代只使受影响及后续检查点失效，重新压实须追加事件；
  * 批准快照/版本差异/JSON 随件包保留压力区间、结果与事件引用；
  * 无 compaction 规范的老工单与老请求保持兼容。
"""

import io
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from prepreg_release import make_app

DB = os.path.join(tempfile.gettempdir(), "prepreg_compaction_test.db")
if os.path.exists(DB):
    os.remove(DB)
app = make_app(DB)

FULL = [[-1, -1], [401, -1], [401, 61], [-1, 61]]


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

def iso(t, sec):
    return (datetime.fromisoformat(t.replace("Z", "+00:00"))
            + timedelta(seconds=sec)).isoformat().replace("+00:00", "Z")


def new_job(checkpoints=None, defaults=None, n_plies=4, zones_cps=None,
            extra_comp=None):
    plies = [{"seq": i, "ply_id": f"P{i:02d}", "material": "CF-EP-3K",
              "angle": 0, "face": "up", "zones": ["Z1", "Z2"]}
             for i in range(1, n_plies + 1)]
    comp = None
    if checkpoints is not None or extra_comp is not None:
        comp = {"defaults": defaults or {
            "target_abs_kpa": 12.0, "hold_seconds": 120,
            "max_sample_interval_s": 60, "max_rise_kpa_min": 2.0,
            "leak_test_seconds": 120},
            "checkpoints": checkpoints if checkpoints is not None else []}
        if extra_comp:
            comp.update(extra_comp)
    body = {
        "name": "compaction-test",
        "tool_datum": {"datum_id": "M1"},
        "zones": [
            {"zone_id": "Z1", "polygon": [[0, 0], [200, 0], [200, 60], [0, 60]],
             "adjacent": ["Z2"]},
            {"zone_id": "Z2", "polygon": [[200, 0], [400, 0], [400, 60], [200, 60]],
             "adjacent": ["Z1"]}],
        "spec": {"materials": {"CF-EP-3K": {"ply_thickness": 0.125}},
                 "plies": plies,
                 "rules": {"max_consecutive_same_angle": n_plies},
                 **({"compaction": comp} if comp else {})},
        "rolls": [{"roll_id": "R1", "batch_no": "B1", "material": "CF-EP-3K",
                   "out_time_limit_h": 240}],
    }
    _s, r = call("POST", "/jobs", body)
    return r["job_id"]


def cp(cid, after_seq, zones=None, **over):
    c = {"checkpoint_id": cid, "after_seq": after_seq}
    if zones is not None:
        c["zones"] = zones
    c.update(over)
    return c


def thaw(at="2026-09-10T06:00:00Z"):
    return {"type": "roll_thawed", "operator": "o", "roll": "R1", "at": at}


def placed(pid, t):
    return {"type": "ply_placed", "operator": "o", "ply_id": pid, "roll": "R1",
            "angle": 0, "face": "up", "geometry": FULL, "placed_at": t}


def removed(pid, reason="污染"):
    return {"type": "ply_removed", "operator": "o", "ply_id": pid, "reason": reason}


def replaced(rid, t):
    return {"type": "ply_replaced", "operator": "o", "removed_ply_id": rid,
            "replacement": {"ply_id": rid, "roll": "R1", "angle": 0, "face": "up",
                            "geometry": FULL, "placed_at": t}}


def vac(t0="2026-09-10T08:20:00Z", pre=None, post=None, zones=None,
        pump_at=60, iso_at=420, end_at=660, omit_stage=None):
    """一组合格默认值的封袋→抽真空→读数→隔离→结束事件，可用参数造故障。

    pre/post 为 (偏移秒, 绝对压力 kPa) 读数；默认读数间隔 60s：
      抽真空 100→20→10×4（连续达压 180s），隔离后 10→11.5→11.5（180s，0.5kPa/min）。
    """
    omit_stage = omit_stage or set()
    pre = [(120, 100), (180, 20), (240, 10), (300, 10), (360, 10), (420, 10)] \
        if pre is None else pre
    post = [(480, 10.0), (540, 11.5), (600, 11.5)] if post is None else post
    evs = []
    seal = {"type": "bag_sealed", "operator": "o", "at": t0}
    if zones is not None:
        seal["zones"] = zones
    if "seal" not in omit_stage:
        evs.append(seal)
    if "pump" not in omit_stage:
        evs.append({"type": "vacuum_started", "operator": "o", "at": iso(t0, pump_at)})
    for off, pressure in pre:
        evs.append({"type": "vacuum_reading", "operator": "o",
                    "at": iso(t0, off), "pressure_kpa": pressure})
    if "isolate" not in omit_stage:
        evs.append({"type": "pump_isolated", "operator": "o", "at": iso(t0, iso_at)})
    for off, pressure in post:
        evs.append({"type": "vacuum_reading", "operator": "o",
                    "at": iso(t0, off), "pressure_kpa": pressure})
    if "end" not in omit_stage:
        evs.append({"type": "compaction_ended", "operator": "o",
                    "at": iso(t0, end_at)})
    return evs


def lay(jid, plies, times=None, prefix=True):
    times = times or [f"2026-09-10T08:{i:02d}:00Z" for i in range(0, 5 * len(plies), 5)]
    evs = ([thaw()] if prefix else []) + [placed(p, t) for p, t in zip(plies, times)]
    call("POST", f"/jobs/{jid}/events", {"events": evs})


def rules_of(jid):
    _s, r = call("GET", f"/jobs/{jid}/validate")
    return {v["rule"] for v in r["violations"]}, r


def cps_of(jid):
    _s, r = call("GET", f"/jobs/{jid}/state")
    return {c["checkpoint_id"]: c for c in r["compaction"]["checkpoints"]}


results = []


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
    results.append(ok)
    return got, r


def find(jid, rule):
    _g, r = rules_of(jid)
    return [v for v in r["violations"] if v["rule"] == rule]


def approve_blocked(name, jid):
    s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
    ok = s.startswith("409") and r.get("error") == "release_rejected"
    print(f"{'PASS' if ok else 'FAIL'}  {name}: 批准被拒 [{s}]")
    results.append(ok)


# 1. 完整合格流程：封袋/抽真空/达压保持/隔离检漏/结束 → 检查点通过、可批准
jid = new_job([cp("CP1", 2)])
call("POST", f"/jobs/{jid}/events", {"events": [thaw(),
    placed("P01", "2026-09-10T08:00:00Z"),
    placed("P02", "2026-09-10T08:05:00Z")] + vac() + [
    placed("P03", "2026-09-10T08:40:00Z"),
    placed("P04", "2026-09-10T08:45:00Z")]})
_g, r = rules_of(jid)
ok = r["release"] == "ok"
print(f"{'PASS' if ok else 'FAIL'}  合格压实放行: {r['release']}")
results.append(ok)
state_cp = cps_of(jid)["CP1"]
ok = state_cp["status"] == "passed" and state_cp["required_plies"] == ["P01", "P02"]
print(f"{'PASS' if ok else 'FAIL'}  检查点绑定当时有效层 P01/P02: "
      f"{state_cp['status']} {state_cp['required_plies']}")
results.append(ok)
s, ap = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
ok_v1 = s.startswith("201")
print(f"{'PASS' if ok_v1 else 'FAIL'}  合格压实批准 v1 [{s}]")
results.append(ok_v1)
_s, pkg = call("GET", f"/jobs/{jid}/approvals/1/package")
p1 = {c["checkpoint_id"]: c for c in pkg["compaction"]["checkpoints"]}["CP1"]
hw, lw = p1["bound_session"]["hold"]["best_window"], p1["bound_session"]["leak"]["window"]
ok = (p1["bound_session"]["hold"]["achieved_seconds"] == 180.0
      and hw["first_reading_event"] == 8 and hw["last_reading_event"] == 11
      and p1["bound_session"]["leak"]["rise_rate_kpa_min"] == 0.75
      and lw["first_reading_event"] == 13 and lw["last_reading_event"] == 15
      and p1["bound_session"]["events"]["bag_sealed"] == 4)
print(f"{'PASS' if ok else 'FAIL'}  随件包保存压力区间/结果/事件引用: "
      f"hold={p1['bound_session']['hold']['achieved_seconds']}s "
      f"rise={p1['bound_session']['leak']['rise_rate_kpa_min']}kPa/min "
      f"window={json.dumps({'hold': hw, 'leak': lw}, ensure_ascii=False)}")
results.append(ok)

# 2. 漏做检查点 → COMPACTION_MISSING，带检查点/层号/区域，阻止批准
jid = new_job([cp("CP1", 2)])
lay(jid, ["P01", "P02", "P03", "P04"])
vs = find(jid, "COMPACTION_MISSING")
ok = len(vs) == 1 and vs[0]["plies"] == ["P01", "P02"] and vs[0]["zones"] == ["Z1", "Z2"] \
    and vs[0]["details"]["checkpoint_id"] == "CP1"
print(f"{'PASS' if ok else 'FAIL'}  漏做检查点 COMPACTION_MISSING: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
approve_blocked("错误批准路径: 漏做压实", jid)

# 3. 后续铺层提前开始（P03 在首次压实结束事件之前已铺）→ COMPACTION_LAYUP_EARLY
jid = new_job([cp("CP1", 2)])
call("POST", f"/jobs/{jid}/events", {"events": [thaw(),
    placed("P01", "2026-09-10T08:00:00Z"),
    placed("P02", "2026-09-10T08:05:00Z"),
    {"type": "bag_sealed", "operator": "o", "at": "2026-09-10T08:10:00Z"},
    placed("P03", "2026-09-10T08:12:00Z")] + vac(t0="2026-09-10T08:10:00Z")[1:]})
vs = find(jid, "COMPACTION_LAYUP_EARLY")
ok = (len(vs) == 1 and "P03" in vs[0]["plies"] and "P01" in vs[0]["plies"]
      and vs[0]["zones"] == ["Z1", "Z2"]
      and vs[0]["details"]["early_ply"] == "P03"
      and vs[0]["details"]["checkpoint_id"] == "CP1")
print(f"{'PASS' if ok else 'FAIL'}  后续铺层提前 COMPACTION_LAYUP_EARLY: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
approve_blocked("错误批准路径: 提前铺层", jid)

# 4. 读数倒序 → COMPACTION_READING_ORDER
jid = new_job([cp("CP1", 2)])
lay(jid, ["P01", "P02"])
bad = [(120, 100), (180, 20), (300, 10), (240, 10), (360, 10), (420, 10)]
call("POST", f"/jobs/{jid}/events", {"events": vac(pre=bad)})
lay(jid, ["P03", "P04"], prefix=False)
check("读数倒序 COMPACTION_READING_ORDER", jid, {"COMPACTION_READING_ORDER"})

# 5. 读数断档（300s 无采样）→ COMPACTION_READING_GAP
jid = new_job([cp("CP1", 2)])
lay(jid, ["P01", "P02"])
bad = [(120, 10), (180, 10), (480, 10)]
call("POST", f"/jobs/{jid}/events",
      {"events": vac(pre=bad, iso_at=480, post=[(540, 10.0), (600, 11.0),
                                                (660, 11.0)])})
lay(jid, ["P03", "P04"], prefix=False)
check("读数断档 COMPACTION_READING_GAP", jid, {"COMPACTION_READING_GAP"})

# 6. 压力未达标（最低 20 kPa，目标 12）→ COMPACTION_PRESSURE
jid = new_job([cp("CP1", 2)])
lay(jid, ["P01", "P02"])
bad = [(120, 90), (180, 30), (240, 20), (300, 20), (360, 20), (420, 20)]
call("POST", f"/jobs/{jid}/events", {"events": vac(pre=bad)})
lay(jid, ["P03", "P04"], prefix=False)
check("压力未达标 COMPACTION_PRESSURE", jid, {"COMPACTION_PRESSURE"})

# 7. 连续达压时长不足（只达压 60s，要求 120s）→ COMPACTION_HOLD_SHORT
jid = new_job([cp("CP1", 2)])
lay(jid, ["P01", "P02"])
bad = [(120, 100), (180, 20), (240, 10), (300, 10), (360, 50), (420, 50)]
call("POST", f"/jobs/{jid}/events", {"events": vac(pre=bad)})
lay(jid, ["P03", "P04"], prefix=False)
check("达压保持不足 COMPACTION_HOLD_SHORT", jid, {"COMPACTION_HOLD_SHORT"})

# 8. 检漏区间不足（隔离后首末读数只相隔 60s，要求 120s）→ COMPACTION_LEAK_INTERVAL
jid = new_job([cp("CP1", 2)])
lay(jid, ["P01", "P02"])
call("POST", f"/jobs/{jid}/events",
      {"events": vac(post=[(480, 10.0), (540, 11.0)], end_at=600)})
lay(jid, ["P03", "P04"], prefix=False)
vs = find(jid, "COMPACTION_LEAK_INTERVAL")
ok = bool(vs) and vs[0]["details"]["seconds"] == 60.0
print(f"{'PASS' if ok else 'FAIL'}  检漏区间不足 COMPACTION_LEAK_INTERVAL: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)

# 8b. 缺少隔离泵事件 → 无法检漏
jid = new_job([cp("CP1", 2)])
lay(jid, ["P01", "P02"])
call("POST", f"/jobs/{jid}/events", {"events": vac(omit_stage={"isolate"})})
lay(jid, ["P03", "P04"], prefix=False)
check("缺隔离泵 COMPACTION_SESSION_ORDER", jid, {"COMPACTION_SESSION_ORDER"})

# 9. 隔离后回升率超限（2 分钟回升 10kPa = 5.0 kPa/min，限值 2.0）
jid = new_job([cp("CP1", 2)])
lay(jid, ["P01", "P02"])
call("POST", f"/jobs/{jid}/events",
      {"events": vac(post=[(480, 10.0), (540, 20.0), (600, 20.0)])})
lay(jid, ["P03", "P04"], prefix=False)
vs = find(jid, "COMPACTION_LEAK_RATE")
ok = len(vs) == 1 and vs[0]["details"]["rise_rate_kpa_min"] == 5.0
print(f"{'PASS' if ok else 'FAIL'}  回升率超限 COMPACTION_LEAK_RATE: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
approve_blocked("错误批准路径: 检漏回升率超限", jid)

# 10. 分区检查点：袋只封 Z2 不满足 Z1 检查点；补封全部区域后通过
jid = new_job([cp("CP1", 2, zones=["Z1"])])
lay(jid, ["P01", "P02"])
call("POST", f"/jobs/{jid}/events", {"events": vac(zones=["Z2"])})
lay(jid, ["P03", "P04"], prefix=False)
vs = find(jid, "COMPACTION_MISSING")
ok = bool(vs) and vs[0]["zones"] == ["Z1"]
print(f"{'PASS' if ok else 'FAIL'}  分区不覆盖仍判漏做（zones=Z1 vs 袋 Z2）: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
jid = new_job([cp("CP1", 2, zones=["Z1"])])
lay(jid, ["P01", "P02"])
call("POST", f"/jobs/{jid}/events", {"events": vac()})  # 未声明 zones = 全袋
lay(jid, ["P03", "P04"], prefix=False)
_g, r = rules_of(jid)
ok = r["release"] == "ok" and cps_of(jid)["CP1"]["zones"] == ["Z1"]
print(f"{'PASS' if ok else 'FAIL'}  全袋压实满足分区检查点: release={r['release']}")
results.append(ok)

# 11. 返工：揭除被压实层 → 受影响检查点失效；追加重新压实后恢复
jid = new_job([cp("CP1", 2), cp("CP2", 4)])
call("POST", f"/jobs/{jid}/events", {"events": [thaw(),
    placed("P01", "2026-09-10T08:00:00Z"),
    placed("P02", "2026-09-10T08:05:00Z")] + vac(t0="2026-09-10T08:10:00Z") + [
    placed("P03", "2026-09-10T08:40:00Z"),
    placed("P04", "2026-09-10T08:45:00Z")] + vac(t0="2026-09-10T08:50:00Z")})
_g, r = rules_of(jid)
ok = r["release"] == "ok"
print(f"{'PASS' if ok else 'FAIL'}  两个检查点均合格: {r['release']}")
results.append(ok)
s, _a = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
results.append(s.startswith("201"))
# P04（属 CP2，不属 CP1）被揭除替代：只应使 CP2 失效
call("POST", f"/jobs/{jid}/events", {"events": [
    removed("P04", "夹气"), replaced("P04", "2026-09-10T10:05:00Z")]})
cs = cps_of(jid)
# 精确断言：CP1 仍 passed（无失效记录），CP2 因 P04 揭除而漏压实
ok = (cs["CP1"]["status"] == "passed" and cs["CP1"]["invalidations"] == []
      and cs["CP2"]["status"] == "missing"
      and [i["ply_id"] for i in cs["CP2"]["invalidations"]] == ["P04"])
print(f"{'PASS' if ok else 'FAIL'}  揭除 P04 只使 CP2 失效，CP1 仍有效: "
      f"CP1={cs['CP1']['status']} CP2={cs['CP2']['status']} "
      f"invalid={cs['CP2']['invalidations']}")
results.append(ok)
vs = find(jid, "COMPACTION_MISSING")
ok = len(vs) == 1 and vs[0]["details"]["checkpoint_id"] == "CP2" \
    and vs[0]["plies"] == ["P01", "P02", "P03", "P04"]
print(f"{'PASS' if ok else 'FAIL'}  失效检查点阻止批准且带层号: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
# 追加重新压实（不可改旧事件）→ CP2 恢复
call("POST", f"/jobs/{jid}/events",
      {"events": vac(t0="2026-09-10T10:10:00Z")})
_g, r = rules_of(jid)
cs = cps_of(jid)
ok = r["release"] == "ok" and cs["CP2"]["status"] == "passed" \
    and cs["CP2"]["bound_session"]["sealed_at"] == "2026-09-10T10:10:00Z" \
    and len(cs["CP2"]["invalidations"]) == 1
print(f"{'PASS' if ok else 'FAIL'}  追加重新压实后 CP2 恢复（失效留痕）: "
      f"CP2={cs['CP2']['status']} seal={cs['CP2']['bound_session']['sealed_at']}")
results.append(ok)
s, _a = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
results.append(s.startswith("201"))
_s, diff = call("GET", f"/jobs/{jid}/approvals/diff", query="a=1&b=2")
changed = {c["checkpoint_id"]: c for c in diff["diff"]["compaction"]["changed"]}
# CP1/CP2 都重新绑定到追加的复压会话（事件引用与压力区间变化），CP2 状态 failed→passed
ok = ("CP1" in changed and "CP2" in changed
      and changed["CP2"]["status"] == {"from": "passed", "to": "passed"}
      and changed["CP2"]["bag_sealed_event"] == {"from": 19, "to": 34}
      and changed["CP1"]["bag_sealed_event"] == {"from": 19, "to": 34})
print(f"{'PASS' if ok else 'FAIL'}  版本差异标出复压引用变化: "
      f"{json.dumps(diff['diff']['compaction'], ensure_ascii=False)}")
results.append(ok)
_s, pkg2 = call("GET", f"/jobs/{jid}/approvals/2/package")
pkg_cps = {c["checkpoint_id"]: c for c in pkg2["compaction"]["checkpoints"]}
# v1 快照中 CP2 的封袋事件是第二次压实（较早），v2 引用的是追加的复压事件
_s, pkg1 = call("GET", f"/jobs/{jid}/approvals/1/package")
v1_cps = {c["checkpoint_id"]: c for c in pkg1["compaction"]["checkpoints"]}
ok = (pkg_cps["CP2"]["bound_session"]["events"]["bag_sealed"]
      > v1_cps["CP2"]["bound_session"]["events"]["bag_sealed"])
print(f"{'PASS' if ok else 'FAIL'}  v2 随件包 CP2 引用重新压实事件序列: "
      f"v1=#{v1_cps['CP2']['bound_session']['events']['bag_sealed']} "
      f"v2=#{pkg_cps['CP2']['bound_session']['events']['bag_sealed']}")
results.append(ok)

# 12. 规范非法：after_ply 不存在 / after_seq 超界 / 参数缺失 → COMPACTION_SPEC_INVALID
jid = new_job(extra_comp={"checkpoints": [{"checkpoint_id": "CX", "after_ply": "PXX"}]})
lay(jid, ["P01", "P02", "P03", "P04"])
check("after_ply 无法解析", jid, {"COMPACTION_SPEC_INVALID"})
jid = new_job([cp("CY", 99)])
lay(jid, ["P01", "P02", "P03", "P04"])
check("after_seq 超界", jid, {"COMPACTION_SPEC_INVALID"})
jid = new_job(extra_comp={"checkpoints": [{"checkpoint_id": "CZ", "after_seq": 2}],
                          "defaults": {"target_abs_kpa": 12.0}})
lay(jid, ["P01", "P02", "P03", "P04"])
check("检查点缺阈值参数", jid, {"COMPACTION_SPEC_INVALID"})

# 13. 入链校验：缺压力/坏时标 400；合法压实事件可追加
jid = new_job([cp("CP1", 2)])
s, r = call("POST", f"/jobs/{jid}/events",
            {"type": "vacuum_reading", "operator": "o", "at": "2026-09-10T08:20:00Z"})
ok = s.startswith("400") and r["error"] == "invalid_compaction_event"
print(f"{'PASS' if ok else 'FAIL'}  缺 pressure_kpa 被拒 [{s}]")
results.append(ok)
s, r = call("POST", f"/jobs/{jid}/events",
            {"type": "vacuum_started", "operator": "o", "at": "not-a-time"})
ok = s.startswith("400")
print(f"{'PASS' if ok else 'FAIL'}  坏时标被拒 [{s}]")
results.append(ok)

# 14. 兼容性：无 compaction 规范的老工单放行不受影响，状态含空压实视图
jid = new_job()
lay(jid, ["P01", "P02", "P03", "P04"])
_g, r = rules_of(jid)
_s, st = call("GET", f"/jobs/{jid}/state")
ok = r["release"] == "ok" and st["compaction"] == {"checkpoints": [], "sessions": []}
print(f"{'PASS' if ok else 'FAIL'}  无压实规范老工单兼容: release={r['release']}")
results.append(ok)
s, _a = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
ok = s.startswith("201")
print(f"{'PASS' if ok else 'FAIL'}  老工单批准 [{s}]")
results.append(ok)

print(f"\n{sum(results)}/{len(results)} 通过")
raise SystemExit(0 if all(results) else 1)
