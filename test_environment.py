#!/usr/bin/env python3
"""开袋回温与环境暴露校核规则覆盖测试。

核心回归：package_opened / material_temp_reading / environment_reading /
layup_paused / surface_covered / layup_resumed 六类事件必须能写入追加
事件链（历史缺陷：均返回 400 invalid_event）。

每类违规构造最小场景，断言规则码、材料单元、层号、分区与原始事件序号；
并验证补录只能追加、替代测点须写理由并派生修订、批准快照/版本差异/
JSON 随件包保留采用的环境序列、区间与决定；无 environment 规范的老工单
保持兼容。
"""

import io
import json
import os
import tempfile
from datetime import datetime, timedelta

from prepreg_release import make_app

DB = os.path.join(tempfile.gettempdir(), "prepreg_environment_test.db")
if os.path.exists(DB):
    os.remove(DB)
app = make_app(DB)

FULL = [[-1, -1], [401, -1], [401, 61], [-1, 61]]

results = []


def call(method, path, body=None, query=""):
    data = json.dumps(body, ensure_ascii=False).encode() \
        if body is not None else b""
    cap = {}

    def sr(status, headers, exc_info=None):
        cap["status"] = status

    env = {"REQUEST_METHOD": method, "PATH_INFO": path,
           "QUERY_STRING": query, "CONTENT_LENGTH": str(len(data)),
           "wsgi.input": io.BytesIO(data)}
    payload = json.loads(b"".join(app(env, sr)))
    return cap["status"], payload


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))
    results.append(ok)


def iso(t0, minutes):
    return (datetime.fromisoformat(t0.replace("Z", "+00:00"))
            + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


T0 = "2026-09-10T06:00:00Z"

DEFAULTS = {"min_warmup_minutes": 120, "dew_point_margin_c": 3.0,
            "temp_min_c": 18.0, "temp_max_c": 27.0,
            "rh_min_pct": 30.0, "rh_max_pct": 65.0,
            "max_sample_interval_min": 30, "max_open_minutes": 480}


def new_job(environment=None, defaults=None, materials=None, zones_env=None,
            n_plies=2, zone_ids=("Z1", "Z2")):
    env = environment
    if env is None and environment is not False:
        env = {"defaults": defaults or DEFAULTS}
        if materials:
            env["materials"] = materials
        if zones_env:
            env["zones"] = zones_env
    plies = [{"seq": i, "ply_id": f"P{i:02d}", "material": "CF-EP-3K",
              "angle": 0, "face": "up", "zones": list(zone_ids)}
             for i in range(1, n_plies + 1)]
    body = {
        "name": "env-test", "tool_datum": {"datum_id": "M1"},
        "zones": [
            {"zone_id": "Z1", "polygon": [[0, 0], [200, 0], [200, 60], [0, 60]],
             "adjacent": ["Z2"]},
            {"zone_id": "Z2", "polygon": [[200, 0], [400, 0], [400, 60], [200, 60]],
             "adjacent": ["Z1"]}],
        "spec": {"materials": {"CF-EP-3K": {"ply_thickness": 0.125}},
                 "plies": plies,
                 "rules": {"max_consecutive_same_angle": n_plies,
                           "require_symmetry": False, "require_balance": False},
                 **({"environment": env} if env else {})},
        "rolls": [{"roll_id": "R1", "batch_no": "B1", "material": "CF-EP-3K",
                   "out_time_limit_h": 240}],
    }
    _s, r = call("POST", "/jobs", body)
    return r.get("job_id"), r


def thaw(at_min=0, roll="R1"):
    return {"type": "roll_thawed", "operator": "o", "roll": roll,
            "at": iso(T0, at_min)}


def amb(m, temp=22.0, rh=50.0, zone=None, **extra):
    e = {"type": "environment_reading", "operator": "o",
         "at": iso(T0, m), "temp_c": temp, "rh_pct": rh}
    if zone:
        e["zone"] = zone
    e.update(extra)
    return e


def mt(m, temp=20.0, roll="R1", **extra):
    e = {"type": "material_temp_reading", "operator": "o",
         "at": iso(T0, m), "roll": roll, "temp_c": temp}
    e.update(extra)
    return e


def opened(m=120, roll="R1", **extra):
    e = {"type": "package_opened", "operator": "o",
         "at": iso(T0, m), "roll": roll}
    e.update(extra)
    return e


def placed(pid, m, roll="R1"):
    return {"type": "ply_placed", "operator": "o", "ply_id": pid,
            "roll": roll, "angle": 0, "face": "up", "geometry": FULL,
            "placed_at": iso(T0, m)}


def covered(m, zones=None, plies=None):
    e = {"type": "surface_covered", "operator": "o", "at": iso(T0, m)}
    if zones:
        e["zones"] = zones
    if plies:
        e["plies"] = plies
    return e


def resumed(m, zones=None, plies=None):
    e = {"type": "layup_resumed", "operator": "o", "at": iso(T0, m)}
    if zones:
        e["zones"] = zones
    if plies:
        e["plies"] = plies
    return e


def paused(m, zones=None, plies=None):
    e = {"type": "layup_paused", "operator": "o", "at": iso(T0, m)}
    if zones:
        e["zones"] = zones
    if plies:
        e["plies"] = plies
    return e


def rules_of(jid):
    _s, r = call("GET", f"/jobs/{jid}/validate")
    return {v["rule"] for v in r["violations"]}, r


def find(jid, rule):
    _g, r = rules_of(jid)
    return [v for v in r["violations"] if v["rule"] == rule]


def _all_events(jid):
    _s, r = call("GET", f"/jobs/{jid}/events")
    return r["events"]


def append_ok(name, events):
    """六类事件必须全部 201 入链（核心回归）。"""
    jid, _ = new_job()
    s, r = call("POST", f"/jobs/{jid}/events", {"events": events})
    check(name, s.startswith("201"), f"{s} {json.dumps(r, ensure_ascii=False)[:160]}")
    return jid


def approve_blocked(name, jid):
    s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
    ok = s.startswith("409") and r.get("error") == "release_rejected"
    check(name, ok, f"[{s}]")
    return ok


# 0. 六类事件全部可写入 sqlite 追加事件链（缺陷回归：曾经全部 400）
six = [thaw(0), amb(60), mt(110), opened(120), placed("P01", 125),
       paused(130), amb(140), resumed(150),
       covered(160, zones=["Z1", "Z2"], plies=["P01"]),
       amb(180), resumed(190, zones=["Z1", "Z2"], plies=["P01"]),
       placed("P02", 200)]
jid = append_ok("六类环境事件入链 201（不再 invalid_event）", six)
s, r = call("GET", f"/jobs/{jid}/events")
types = [e["type"] for e in r["events"]]
ok = all(t in types for t in
         ("package_opened", "material_temp_reading", "environment_reading",
          "layup_paused", "surface_covered", "layup_resumed"))
check("事件链读回六类事件", ok, str(types))

# 1. 完整合格流程 → 放行、可批准；快照/随件包/区间/决定齐备
jid, _ = new_job()
good = [thaw(0), amb(60), amb(100), mt(115), opened(120),
        placed("P01", 125), amb(145), amb(170),
        placed("P02", 190), amb(210)]
call("POST", f"/jobs/{jid}/events", {"events": good})
g, r = rules_of(jid)
check("合格回温/暴露流程放行", r["release"] == "ok" and not g,
      f"{r['release']} {sorted(g)}")
s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
check("合格流程可批准", s.startswith("201"), f"[{s}]")
s, pkg = call("GET", f"/jobs/{jid}/approvals/1/package")
env = pkg["environment"]
ok = (env["enabled"] and len(env["ambient_series"]) == 5
      and len(env["material_temp_series"]) == 1
      and env["material_intervals"][0]["closed"] is True
      and len(env["surface_intervals"]) == 4
      and any(si["ply_id"] == "P01" and si["zone"] == "Z1"
              for si in env["surface_intervals"]))
check("随件包保留环境序列/开封段/表面区间（2层×2区=4段）", ok,
      f"amb={len(env['ambient_series'])} surf={len(env['surface_intervals'])}")

# 2. 无环境规范的老工单：不提交环境事件时完全兼容
jid, _ = new_job(environment=False)
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), placed("P01", 120), placed("P02", 180)]})
g, r = rules_of(jid)
check("无 environment 规范老工单兼容",
      r["release"] == "ok" and "ENV_SPEC_MISSING" not in g, sorted(g))

# 3. 有环境事件但规范缺 environment 块 → ENV_SPEC_MISSING
jid, _ = new_job(environment=False)
call("POST", f"/jobs/{jid}/events", {"events": [thaw(0), amb(60), opened(120)]})
vs = find(jid, "ENV_SPEC_MISSING")
check("有环境事件无规范 → ENV_SPEC_MISSING", len(vs) >= 1,
      json.dumps(vs, ensure_ascii=False)[:200])
approve_blocked("批准被拒：环境规范缺失", jid)

# 4. 开袋前测温缺失 → ENV_TEMP_BEFORE_OPEN_MISSING，带单元/事件
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), opened(120), placed("P01", 125), placed("P02", 190)]})
vs = find(jid, "ENV_TEMP_BEFORE_OPEN_MISSING")
ok = bool(vs) and vs[0]["details"]["units"] == ["R1"] \
    and 3 in vs[0]["details"]["events"]
check("开袋前测温缺失", ok, json.dumps(vs, ensure_ascii=False)[:220])

# 5. 最低回温时长不足 → ENV_WARMUP_SHORT（开封前仅 60min < 120min）
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(60), amb(100), mt(115, 20.0), opened(120),
    placed("P01", 125), placed("P02", 190)]})
vs = find(jid, "ENV_WARMUP_SHORT")
ok = bool(vs) and vs[0]["details"]["units"] == ["R1"] \
    and vs[0]["details"]["required_minutes"] == 120
check("回温不足 ENV_WARMUP_SHORT", ok,
      json.dumps(vs, ensure_ascii=False)[:220])

# 6. 无解冻记录即开封 → ENV_WARMUP_DATA_MISSING
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    amb(100), mt(115, 20.0), opened(120),
    placed("P01", 125), placed("P02", 190)]})
vs = find(jid, "ENV_WARMUP_DATA_MISSING")
check("无解冻记录 ENV_WARMUP_DATA_MISSING",
      bool(vs) and vs[0]["details"]["units"] == ["R1"],
      json.dumps(vs, ensure_ascii=False)[:200])

# 7. 开袋瞬间露点裕量不足 → ENV_DEW_MARGIN
#    22℃ / RH70% → 露点约 16.3℃；材料 18℃ → 裕量 1.7℃ < 3℃
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100, temp=22.0, rh=70.0), mt(115, temp=18.0),
    opened(120), placed("P01", 125), amb(150, temp=22.0, rh=50.0),
    placed("P02", 190)]})
vs = find(jid, "ENV_DEW_MARGIN")
ok = bool(vs) and vs[0]["details"]["units"] == ["R1"] \
    and vs[0]["details"]["dew_point_margin_c"] < 3.0
check("开袋露点裕量不足 ENV_DEW_MARGIN", ok,
      json.dumps(vs, ensure_ascii=False)[:260])

# 8. 暴露期间材料温度低于露点加裕量 → ENV_DEW_MARGIN 带层号/分区
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100, temp=22.0, rh=50.0), mt(115, temp=20.0), opened(120),
    placed("P01", 125), amb(140, temp=22.0, rh=50.0),
    mt(150, temp=13.0),  # P01 暴露中，表面过冷（22℃/50%RH 露点约 11.1℃）
    amb(170, temp=22.0, rh=50.0), placed("P02", 190)]})
vs = find(jid, "ENV_DEW_MARGIN")
ok = any(v["plies"] == ["P01"] and v["zones"] == ["Z1"]
         and v["details"]["units"] == ["R1"] for v in vs)
check("暴露期凝露风险带层号/分区/单元", ok,
      json.dumps(vs, ensure_ascii=False)[:300])

# 9. 读数断档（45min > 30min）→ ENV_SAMPLE_GAP，带原始事件
jid, _ = new_job()
evs = [thaw(0), amb(100), mt(115), opened(120), placed("P01", 125),
       amb(145, rh=50.0), amb(190, rh=50.0),  # 145→190 = 45min 断档
       placed("P02", 200)]
call("POST", f"/jobs/{jid}/events", {"events": evs})
s, r = call("GET", f"/jobs/{jid}/events")
seq_of = {e["payload"].get("at"): e["seq"] for e in r["events"]}
vs = find(jid, "ENV_SAMPLE_GAP")
ok = bool(vs) and any(v["details"]["interval_min"] >= 44.9 for v in vs) \
    and any({seq_of[iso(T0, 145)], seq_of[iso(T0, 190)]}
            <= set(v["details"]["events"]) for v in vs)
check("读数断档 ENV_SAMPLE_GAP 带事件序号", ok,
      json.dumps(vs, ensure_ascii=False)[:300])

# 10. 温度越限 → ENV_LIMIT_EXCEEDED，带层号/分区/越限时长/原始读数
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120), placed("P01", 125),
    amb(145, temp=30.0, rh=50.0),   # 高温
    amb(170, temp=30.0, rh=50.0),
    amb(195, temp=22.0, rh=50.0), placed("P02", 200)]})
vs = find(jid, "ENV_LIMIT_EXCEEDED")
ok = any(v["details"].get("metric") == "temp_c"
         and v["details"].get("direction") == "high"
         and "P01" in v["plies"] and "Z1" in v["zones"]
         and v["details"]["duration_min"] >= 24.9 for v in vs)
check("温度越限带层/分区/时长", ok, json.dumps(vs, ensure_ascii=False)[:320])

# 11. 湿度越限（单点也要报）
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120), placed("P01", 125),
    amb(145, temp=22.0, rh=80.0),
    amb(170, temp=22.0, rh=50.0), placed("P02", 190)]})
vs = find(jid, "ENV_LIMIT_EXCEEDED")
ok = any(v["details"].get("metric") == "rh_pct"
         and v["details"].get("direction") == "high"
         and v["details"].get("limit") == 65.0 for v in vs)
check("湿度越限 ENV_LIMIT_EXCEEDED", ok,
      json.dumps(vs, ensure_ascii=False)[:260])

# 12. 覆盖窗扣减暴露：覆盖 6 小时不计入敞开时长
jid, _ = new_job(defaults={**DEFAULTS, "max_open_minutes": 120})
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120), placed("P01", 125),
    amb(145), covered(150, zones=["Z1", "Z2"], plies=["P01"]),
    amb(160),  # 覆盖中读数（不参与 P01 暴露）
    resumed(510, zones=["Z1", "Z2"], plies=["P01"]),
    amb(520), amb(540), placed("P02", 560)]})  # 暴露 25+50=75min < 120
vs = find(jid, "ENV_OPEN_TIME_EXCEEDED")
check("覆盖窗扣减敞开时长（不超限）", not vs,
      json.dumps(vs, ensure_ascii=False)[:200])
g, _r = rules_of(jid)
check("覆盖/恢复闭合无结构违规",
      "ENV_COVERAGE_NOT_CLOSED" not in g and "ENV_SAMPLE_GAP" not in g,
      sorted(g))

# 13. 无覆盖保护时敞开超时 → ENV_OPEN_TIME_EXCEEDED
jid, _ = new_job(defaults={**DEFAULTS, "max_open_minutes": 60})
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120), placed("P01", 125),
    amb(145), amb(170), placed("P02", 195)]})  # P01 暴露 70min
vs = find(jid, "ENV_OPEN_TIME_EXCEEDED")
ok = any("P01" in v["plies"] and v["details"]["limit_minutes"] == 60
         for v in vs)
check("表面敞开超时带层号", ok, json.dumps(vs, ensure_ascii=False)[:260])

# 14. 暂停不停暴露时钟：暂停 600min 照样累计敞开 → 超时
jid, _ = new_job(defaults={**DEFAULTS, "max_open_minutes": 120})
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120), placed("P01", 125),
    amb(145), paused(150), amb(160),
    resumed(750), amb(760), amb(780), placed("P02", 800)]})
vs = find(jid, "ENV_OPEN_TIME_EXCEEDED")
check("暂停不停表：敞开仍超时",
      any("P01" in v["plies"] for v in vs),
      json.dumps(vs, ensure_ascii=False)[:220])

# 15. 覆盖关系不闭合：覆盖无恢复
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120), placed("P01", 125),
    covered(150, zones=["Z1"], plies=["P01"]),
    amb(170), placed("P02", 190)]})
vs = find(jid, "ENV_COVERAGE_NOT_CLOSED")
cover_seq = [e["seq"] for e in _all_events(jid)
             if e["type"] == "surface_covered"][0]
ok = bool(vs) and any(cover_seq in v["details"]["events"]
                      and "Z1" in v["zones"] for v in vs)
check("覆盖无恢复 → 关系不闭合（顶层带层号/分区）", ok,
      json.dumps(vs, ensure_ascii=False)[:240])

# 16. 恢复无覆盖/暂停 → 关系不闭合
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120), placed("P01", 125),
    amb(145), resumed(150, zones=["Z1"], plies=["P01"]),
    amb(170), placed("P02", 190)]})
vs = find(jid, "ENV_COVERAGE_NOT_CLOSED")
check("多余恢复事件 → 关系不闭合",
      bool(vs), json.dumps(vs, ensure_ascii=False)[:200])

# 17. 事件倒序（非补录）→ ENV_EVENT_ORDER
jid, _ = new_job()
s, r = call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120)]})
s, r = call("POST", f"/jobs/{jid}/events", {"events": [
    amb(90)]})  # 正常事件时标早于前一条且未标补录
ok = s.startswith("201")  # 入链允许（只追加），分析阶段报违规
g, _r = rules_of(jid)
check("倒序事件可追加但判 ENV_EVENT_ORDER",
      ok and "ENV_EVENT_ORDER" in g, f"{s} {sorted(g)}")

# 18. 补录只能追加、允许回退、不覆盖旧事件
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120)]})
s, r = call("POST", f"/jobs/{jid}/events", {"events": [
    amb(90, backfilled=True)]})
s, evr = call("GET", f"/jobs/{jid}/events")
check("补录以追加方式入链（旧事件仍在）",
      s.startswith("200") and len(evr["events"]) == 5
      and evr["events"][-1]["payload"].get("backfilled") is True,
      f"count={len(evr['events'])}")
g, _r = rules_of(jid)
check("补录豁免倒序违规", "ENV_EVENT_ORDER" not in g, sorted(g))

# 19. 替代测点缺理由 → 400 拒收
s, r = call("POST", f"/jobs/{jid}/events", {"events": [
    amb(95, alternative=True, amends_event=2, alt_probe="P-BACKUP")]})
check("替代测点无 reason → 400", s.startswith("400")
      and r.get("error") == "invalid_environment_event", f"[{s}] {r}")

# 20. 替代测点有理由 + amends_event → 派生修订，旧读数留痕不参与
jid, _ = new_job()
# 先制造一条“坏”环境读数（高湿，开袋露点不足），再以替代测点修订
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(110, temp=22.0, rh=80.0, probe_id="P-OLD"),
    mt(115, temp=20.0), opened(120)]})
s, evr = call("GET", f"/jobs/{jid}/events")
old_seq = [e["seq"] for e in evr["events"]
           if e["type"] == "environment_reading"][0]
# 修订读数：正常车间湿度 50%（补录身份，时标与被修订读数一致）
s, r = call("POST", f"/jobs/{jid}/events", {"events": [
    amb(110, temp=22.0, rh=50.0, probe_id="P-NEW", alternative=True,
        alt_probe="P-NEW", reason="原探头 P-OLD 校准过期，改用经校准的 P-NEW",
        amends_event=old_seq, backfilled=True)]})
check("替代测点修订入链", s.startswith("201"), f"[{s}] {r}")
call("POST", f"/jobs/{jid}/events", {"events": [
    placed("P01", 125), amb(135), amb(145), amb(170),
    placed("P02", 190), amb(205)]})
g, rr = rules_of(jid)
ok = "ENV_DEW_MARGIN" not in g and "ENV_AMENDMENT_REASON_MISSING" not in g \
    and "ENV_EVENT_ORDER" not in g
check("修订后按新测点核算（凝露违规消除、补录豁免倒序）", ok, sorted(g))
st_s, st = call("GET", f"/jobs/{jid}/state")
envst = st["environment"]
old_kept = [a for a in envst["ambient_series"]
            if a["event_seq"] == old_seq]
sup = [a for a in envst["ambient_series"] if a["superseded_by"]]
decs = envst["decisions"]
ok = (len(old_kept) == 1 and len(sup) == 1 and sup[0]["event_seq"] == old_seq
      and any(d["revision_id"] == f"AMEND-{sup[0]['superseded_by']}"
              and d["amends_event"] == old_seq and d["action"] == "adopted"
              for d in decs))
check("旧读数留痕 superseded_by + 派生 AMEND 修订决定", ok,
      json.dumps({"sup": sup, "decisions": decs}, ensure_ascii=False)[:300])

# 21. 替代测点 amends_event 指向不存在事件 → ENV_AMENDMENT_INVALID
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100)]})
s, r = call("POST", f"/jobs/{jid}/events", {"events": [
    amb(110, rh=55.0, alternative=True, reason="x", amends_event=999)]})
vs = find(jid, "ENV_AMENDMENT_INVALID")
check("修订指向缺失事件 → ENV_AMENDMENT_INVALID", bool(vs),
      json.dumps(vs, ensure_ascii=False)[:200])

# 22. 分区级限值：Z2 更严，同一越限只影响 Z2
jid, _ = new_job(zones_env={"Z2": {"rh_max_pct": 40.0}})
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120), placed("P01", 125),
    amb(145, temp=22.0, rh=50.0),   # 全局 50%：Z1 合格，Z2 超 40%
    amb(170, temp=22.0, rh=50.0), placed("P02", 190)]})
vs = find(jid, "ENV_LIMIT_EXCEEDED")
hit = [v for v in vs if v["details"].get("metric") == "rh_pct"]
ok = (hit and all(v["zones"] == ["Z2"] for v in hit)
      and not any(v["zones"] == ["Z1"] for v in hit))
check("分区级限值仅影响对应分区", ok,
      json.dumps([(v["zones"], v["details"].get("limit")) for v in hit],
                 ensure_ascii=False))

# 23. 分区专用环境读数（zone 通道）与全局通道共同兜底，无断档
jid, _ = new_job(defaults={**DEFAULTS, "max_sample_interval_min": 30})
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120), placed("P01", 125),
    amb(140), amb(145, rh=50.0, zone="Z1"),   # Z1 专用读数替换同时刻全局
    amb(170), amb(195), placed("P02", 200)]})
g, r = rules_of(jid)
check("分区读数与全局读数共同兜底",
      "ENV_SAMPLE_GAP" not in g, sorted(g))

# 24. 引用未登记材料单元 → ENV_UNIT_UNKNOWN
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    amb(100), {"type": "package_opened", "operator": "o",
               "at": iso(T0, 120), "roll": "RX"}]})
vs = find(jid, "ENV_UNIT_UNKNOWN")
check("未登记单元 ENV_UNIT_UNKNOWN",
      bool(vs) and vs[0]["details"]["units"] == ["RX"],
      json.dumps(vs, ensure_ascii=False)[:200])

# 25. 入链载荷校验（400）：缺 at / 缺温度 / 缺湿度 / 湿度越界 / 缺材料引用
jid, _ = new_job()
cases = [
    ({"type": "package_opened", "operator": "o"}, "开封缺 at"),
    ({"type": "material_temp_reading", "operator": "o",
      "at": iso(T0, 100), "temp_c": 20.0}, "测温缺单元"),
    ({"type": "material_temp_reading", "operator": "o",
      "at": iso(T0, 100), "roll": "R1", "temp_c": "cold"}, "温度非数值"),
    ({"type": "environment_reading", "operator": "o",
      "at": iso(T0, 100), "temp_c": 22.0}, "读数缺 rh"),
    ({"type": "environment_reading", "operator": "o",
      "at": iso(T0, 100), "temp_c": 22.0, "rh_pct": 130.0}, "湿度越界"),
    ({"type": "package_opened", "operator": "o",
      "at": iso(T0, 100)}, "开封缺材料引用"),
]
for body, name in cases:
    s, r = call("POST", f"/jobs/{jid}/events", {"events": [body]})
    check(f"400 拒收：{name}",
          s.startswith("400") and r.get("error") in
          ("invalid_event", "invalid_environment_event"),
          f"[{s}] {r.get('error')} {r.get('message','')[:60]}")

# 26. 版本差异包含 environment 段；v1→v2 追加补录/修订事件可见
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120),
    placed("P01", 125), amb(145), amb(170), placed("P02", 190), amb(210)]})
s, ev0 = call("GET", f"/jobs/{jid}/events")
old_amb_seq = [e["seq"] for e in ev0["events"]
               if e["type"] == "environment_reading"][-1]
call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
# 追加：一条补录读数 + 一条替代测点修订（均只能新增）
call("POST", f"/jobs/{jid}/events", {"events": [
    amb(90, backfilled=True),
    amb(210, rh=52.0, alternative=True, alt_probe="P2",
        reason="探头复测，修订 210min 读数", amends_event=old_amb_seq,
        backfilled=True)]})
g, _r = rules_of(jid)
ok = "ENV_AMENDMENT_INVALID" not in g and "ENV_EVENT_ORDER" not in g
check("补录+修订追加后校验通过前置条件", ok, sorted(g))
s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
check("补录/修订后可再次批准 v2", s.startswith("201"), f"[{s}] {r.get('error','')}")
s, d = call("GET", f"/jobs/{jid}/approvals/diff", query="a=1&b=2")
ed = d["diff"]["environment"]
ok = (ed["enabled"]["a"] and ed["enabled"]["b"]
      and len(ed["events_added"]) == 2
      and ed["ambient_series_changed"] and ed["decisions_changed"]
      and any(json.loads(x)["action"] == "adopted"
              for x in ed["decisions_added"]))
check("版本差异保留环境事件/序列/修订决定变化", ok,
      json.dumps(ed, ensure_ascii=False)[:300])
s, pkg1 = call("GET", f"/jobs/{jid}/approvals/1/package")
s, pkg2 = call("GET", f"/jobs/{jid}/approvals/2/package")
ok = (len(pkg1["environment"]["events"])
      == len(pkg2["environment"]["events"]) - 2
      and any(x.get("backfilled") for x in pkg2["environment"]["events"])
      and any(d2["revision_id"].startswith("AMEND-")
              for d2 in pkg2["environment"]["decisions"]))
check("旧快照不变；v2 随件包保留补录标记与 AMEND 决定", ok,
      json.dumps(pkg2["environment"]["decisions"], ensure_ascii=False)[:200])

# 27. 材料级限值覆盖（更短回温要求触发）
jid, _ = new_job(defaults=DEFAULTS,
                 materials={"CF-EP-3K": {"min_warmup_minutes": 300}})
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(150), mt(200, 20.0), opened(240),  # 回温 240 < 300
    placed("P01", 245), placed("P02", 300)]})
vs = find(jid, "ENV_WARMUP_SHORT")
ok = bool(vs) and vs[0]["details"]["required_minutes"] == 300
check("材料级回温限值覆盖", ok, json.dumps(vs, ensure_ascii=False)[:200])

# 28. 重复开封未闭合 → ENV_OPEN_NOT_CLOSED
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120),
    amb(130), opened(140),   # 未回冻/未铺放即二次开封
    placed("P01", 145), placed("P02", 200)]})
vs = find(jid, "ENV_OPEN_NOT_CLOSED")
ok = bool(vs) and any({4, 6} <= set(v["details"]["events"]) for v in vs)
check("重复开封 ENV_OPEN_NOT_CLOSED", ok,
      json.dumps(vs, ensure_ascii=False)[:240])

# 29. 返工替代层：揭除后重铺，受影响层以替代层重建表面区间
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120),
    placed("P01", 125), amb(145),
    {"type": "ply_removed", "operator": "o", "ply_id": "P01",
     "reason": "污染"},
    amb(160),
    {"type": "ply_replaced", "operator": "o", "removed_ply_id": "P01",
     "replacement": {"ply_id": "P01", "roll": "R1", "angle": 0,
                     "face": "up", "geometry": FULL,
                     "placed_at": iso(T0, 170)}},
    amb(185), amb(195), placed("P02", 210)]})
g, r = rules_of(jid)
ok = r["release"] == "ok"
check("返工替代层环境重建可放行", ok, sorted(g))
_s, st = call("GET", f"/jobs/{jid}/state")
si = [s for s in st["environment"]["surface_intervals"] if s["ply_id"] == "P01"]
# 替代层 P01 的区间起点为重铺时刻
ok = si and all(x["placed_at"] == iso(T0, 170) for x in si)
check("表面区间取自替代层重铺时刻", ok,
      json.dumps([(x["zone"], x["placed_at"]) for x in si],
                 ensure_ascii=False))

# 30. 违规阻止批准（聚合路径，以湿度越限为例，再核 plies/zones/events 齐全）
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120), placed("P01", 125),
    amb(145, rh=90.0), amb(170), placed("P02", 190)]})
s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
vs = [v for v in r.get("violations", [])
      if v["rule"] == "ENV_LIMIT_EXCEEDED"]
ok = bool(s.startswith("409") and vs and vs[0]["plies"]
          and vs[0]["zones"] and vs[0]["details"]["events"])
check("环境违规 409 阻止批准且带单元/层/原始事件", ok,
      f"plies={vs[0]['plies'] if vs else None} "
      f"zones={vs[0]['zones'] if vs else None} "
      f"events={vs[0]['details']['events'] if vs else None}")

# 31. 规范换版携带非法 environment 块 → 409 拒绝启用
jid, _ = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    thaw(0), amb(100), mt(115), opened(120),
    placed("P01", 125), placed("P02", 190)]})
call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
with open(os.path.join(tempfile.gettempdir(), "env_rev_spec.json"),
          "w", encoding="utf-8") as f:
    pass
new_spec = {"materials": {"CF-EP-3K": {"ply_thickness": 0.125}},
            "plies": [{"seq": 1, "ply_id": "P01", "material": "CF-EP-3K",
                       "angle": 0, "face": "up", "zones": ["Z1", "Z2"]},
                      {"seq": 2, "ply_id": "P02", "material": "CF-EP-3K",
                       "angle": 0, "face": "up", "zones": ["Z1", "Z2"]}],
            "rules": {"require_symmetry": False, "require_balance": False},
            "environment": {"defaults": {"max_open_minutes": -10}}}
s, r = call("POST", f"/jobs/{jid}/spec-revisions", {
    "spec": new_spec, "reason": "环境限值调整（非法值）",
    "effective_at": "2026-09-12T08:00:00Z"})
codes = [c["code"] for c in r.get("conflicts", [])]
ok = s.startswith("409") and "ENV_SPEC_INVALID" in codes
check("换版携带非法环境限值 → 409", ok, f"[{s}] {codes}")

# 32. 回温按解冻→回冻周期匹配：06:00 解冻、08:00 合格开袋、08:05 铺放、
#     08:10 回冻后，11:00 再次解冻不得反向触发 ENV_WARMUP_DATA_MISSING
jid, _ = new_job(n_plies=1, zone_ids=("Z1",))
cycle = [
    {"type": "roll_thawed", "operator": "o", "roll": "R1", "at": iso(T0, 0)},
    amb(30), amb(60), amb(90),
    mt(115, 20.0), opened(120), placed("P01", 125),
    {"type": "roll_refrigerated", "operator": "o", "roll": "R1",
     "at": iso(T0, 130)},
    amb(150), amb(180), amb(210), amb(240), amb(270),
    {"type": "roll_thawed", "operator": "o", "roll": "R1",
     "at": iso(T0, 300)},
]
call("POST", f"/jobs/{jid}/events", {"events": cycle})
g, r = rules_of(jid)
warmup_bad = [x for x in g if x in
              ("ENV_WARMUP_DATA_MISSING", "ENV_WARMUP_SHORT")]
check("二次解冻不反向触发回温缺段/不足", not warmup_bad and r["release"] == "ok",
      f"{sorted(g)}")
# 开封段以 06:00 解冻为回温起点，正好 120min，合格
_s, st = call("GET", f"/jobs/{jid}/state")
mi = st["environment"]["material_intervals"][0]
ok = mi["interval"]["from"] == iso(T0, 120) and mi["closed"] is True
check("开封段闭合于铺放且回温取自本周期 06:00 解冻", ok,
      json.dumps(mi["interval"], ensure_ascii=False))

# 32b. 上一周期已回冻、本周期再开封但无本周期解冻 → 应报缺段（不被历史解冻掩盖）
jid, _ = new_job(n_plies=1, zone_ids=("Z1",))
cycle_b = [
    {"type": "roll_thawed", "operator": "o", "roll": "R1", "at": iso(T0, 0)},
    amb(90), mt(115, 20.0), opened(120), placed("P01", 125),
    {"type": "roll_refrigerated", "operator": "o", "roll": "R1",
     "at": iso(T0, 130)},
    amb(240),
    # 再次开封（在回冻之后），但没有对应的本周期解冻记录
    {"type": "package_opened", "operator": "o", "at": iso(T0, 300),
     "roll": "R1"},
    amb(330),
]
call("POST", f"/jobs/{jid}/events", {"events": cycle_b})
vs = find(jid, "ENV_WARMUP_DATA_MISSING")
open_seqs = [e["seq"] for e in _all_events(jid)
             if e["type"] == "package_opened"]
ok = bool(vs) and any(open_seqs[-1] in v["details"]["events"] for v in vs)
check("回冻后再开封无本周期解冻 → ENV_WARMUP_DATA_MISSING", ok,
      json.dumps(vs, ensure_ascii=False)[:220])

# 33. 缺开袋前测温且已铺放 P1：审批 409，违规项同时列 R1 / P1 / 开封事件号
jid, _ = new_job(n_plies=1, zone_ids=("Z1",))
# 事件编排：1 解冻、2 环境读数、3 开封（无材料测温）、4 铺放 P1、5 环境读数
seq33 = [
    {"type": "roll_thawed", "operator": "o", "roll": "R1", "at": iso(T0, 0)},
    amb(90),
    {"type": "package_opened", "operator": "o", "at": iso(T0, 120),
     "roll": "R1"},
    {"type": "ply_placed", "operator": "o", "ply_id": "P01",
     "roll": "R1", "angle": 0, "face": "up", "geometry": FULL,
     "placed_at": iso(T0, 125)},
    amb(150),
]
call("POST", f"/jobs/{jid}/events", {"events": seq33})
s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
vs = [v for v in r.get("violations", [])
      if v["rule"] == "ENV_TEMP_BEFORE_OPEN_MISSING"]
ok = (s.startswith("409") and bool(vs)
      and vs[0]["details"]["units"] == ["R1"]
      and vs[0]["plies"] == ["P01"]
      and vs[0]["details"]["events"] == [3])
check("缺开袋前测温+已铺放 → 409 并列 R1/P01/事件3", ok,
      f"[{s}] units={vs[0]['details']['units'] if vs else None} "
      f"plies={vs[0]['plies'] if vs else None} "
      f"events={vs[0]['details']['events'] if vs else None}")

# 33b. 同一缺陷但尚未铺放时：仍 409，plies 为空（不臆造受影响层）
jid, _ = new_job(n_plies=1, zone_ids=("Z1",))
call("POST", f"/jobs/{jid}/events", {"events": [
    {"type": "roll_thawed", "operator": "o", "roll": "R1", "at": iso(T0, 0)},
    amb(90),
    {"type": "package_opened", "operator": "o", "at": iso(T0, 120),
     "roll": "R1"},
    amb(150),
]})
s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe"})
vs = [v for v in r.get("violations", [])
      if v["rule"] == "ENV_TEMP_BEFORE_OPEN_MISSING"]
ok = (s.startswith("409") and bool(vs)
      and vs[0]["details"]["units"] == ["R1"]
      and vs[0]["plies"] == [])
check("缺测温未铺放 → 409 且 plies 为空", ok,
      f"plies={vs[0]['plies'] if vs else None}")

# ------------------------------------------------------------------ 汇总
total, passed = len(results), sum(1 for x in results if x)
print(f"\n{passed}/{total} 通过")
raise SystemExit(0 if passed == total else 1)