#!/usr/bin/env python3
"""材料谱系规则覆盖测试。

场景覆盖：
  * 整卷登记（制造/失效日期、计量单位、初始数量）与裁切/拆包/转移/退库/报废；
  * 外置时长沿父子链继承（父卷已消耗寿命计入套件）；
  * 同一裁片重复铺放（同工单/跨工单）、超额分配、失效/报废后使用、谱系成环；
  * 解冻记录缺段、父子时刻冲突、数量无法闭合 → 违规带材料单元/受影响层/事件，
    相关工单不得批准；纠正绑定以追加事件闭环；
  * 材料事件变动后仅刷新引用该谱系的工单；
  * 批准快照保存父子关系/寿命明细/数量结果，版本差异与随件包据此生成；
  * 无谱系的老工单（roll 输入）保持兼容。
"""

import io
import json
import os
import tempfile

from prepreg_release import make_app

DB = os.path.join(tempfile.gettempdir(), "prepreg_genealogy_test.db")
if os.path.exists(DB):
    os.remove(DB)
app = make_app(DB)

FULL = [[-1, -1], [401, -1], [401, 61], [-1, 61]]
ANGLES = [0, 45, -45, -45, 45, 0]


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

def new_job(with_roll=False):
    plies = [{"seq": i, "ply_id": f"P{i:02d}", "material": "CF-EP-3K",
              "angle": a, "face": "up", "zones": ["Z1", "Z2"]}
             for i, a in enumerate(ANGLES, 1)]
    body = {
        "name": "genealogy-test",
        "tool_datum": {"datum_id": "M1"},
        "zones": [
            {"zone_id": "Z1", "polygon": [[0, 0], [200, 0], [200, 60], [0, 60]],
             "adjacent": ["Z2"]},
            {"zone_id": "Z2", "polygon": [[200, 0], [400, 0], [400, 60], [200, 60]],
             "adjacent": ["Z1"]}],
        "spec": {"materials": {"CF-EP-3K": {"ply_thickness": 0.125}},
                 "plies": plies,
                 "rules": {"max_consecutive_same_angle": 4}},
    }
    if with_roll:
        body["rolls"] = [{"roll_id": "R1", "batch_no": "B1",
                          "material": "CF-EP-3K", "out_time_limit_h": 240}]
    _s, r = call("POST", "/jobs", body)
    return r["job_id"]


def reg_roll(uid, qty=100.0, limit=240.0, mfg="2026-09-01T00:00:00Z",
             exp="2026-12-01T00:00:00Z"):
    return call("POST", "/materials/units", {
        "unit_id": uid, "batch_no": f"B-{uid}", "material": "CF-EP-3K",
        "unit": "m2", "initial_qty": qty, "out_time_limit_h": limit,
        "manufactured_at": mfg, "expires_at": exp})


def mevs(*items):
    return call("POST", "/materials/events", {"events": list(items)})


def thaw(uid, at):
    return {"type": "unit_thawed", "operator": "o", "unit_id": uid, "at": at}


def fridge(uid, at):
    return {"type": "unit_refrigerated", "operator": "o", "unit_id": uid, "at": at}


def cut(parent, children, at, consumed=None, kind="unit_cut"):
    e = {"type": kind, "operator": "o", "parent": parent, "at": at,
         "children": children}
    if consumed is not None:
        e["consumed"] = consumed
    return e


def kit(uid, qty, kind="kit"):
    return {"unit_id": uid, "kind": kind, "qty": qty}


def placed(pid, t, unit=None, roll=None):
    e = {"type": "ply_placed", "operator": "o", "ply_id": pid,
         "angle": ANGLES[int(pid[1:]) - 1], "face": "up",
         "geometry": FULL, "placed_at": t}
    if unit:
        e["unit"] = unit
    if roll:
        e["roll"] = roll
    return e


def lay_with_units(jid, units, times=None):
    times = times or [f"2026-09-10T11:0{i}:00Z" for i in range(1, 7)]
    evs = [placed(f"P{i:02d}", t, unit=u)
           for i, (u, t) in enumerate(zip(units, times), 1)]
    return call("POST", f"/jobs/{jid}/events", {"events": evs})


def rules_of(jid):
    _s, r = call("GET", f"/jobs/{jid}/validate")
    return {v["rule"] for v in r["violations"]}, r


def find(jid, rule):
    _g, r = rules_of(jid)
    return [v for v in r["violations"] if v["rule"] == rule]


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
    return got


def approve_blocked(name, jid):
    s, r = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
    ok = s.startswith("409") and r.get("error") == "release_rejected"
    print(f"{'PASS' if ok else 'FAIL'}  {name}: 批准被拒 [{s}]")
    results.append(ok)


# 1. 端到端：整卷 → 解冻 2h → 裁切套件+余料 → 套件回冻 → 拆包转移 → 铺层放行
reg_roll("R1")
_s, r = mevs(
    thaw("R1", "2026-09-10T06:00:00Z"),
    cut("R1", [kit(f"K{i}", 2.0) for i in range(1, 7)]
        + [kit("REM1", 80.0, "remnant")], "2026-09-10T08:00:00Z"),
    fridge("R1", "2026-09-10T08:30:00Z"),
    *[fridge(f"K{i}", "2026-09-10T08:30:00Z") for i in range(1, 7)],
    {"type": "unit_split", "operator": "o", "parent": "REM1",
     "at": "2026-09-10T09:00:00Z", "children": [kit("REM1A", 30.0),
                                               kit("REM1B", 50.0, "remnant")]},
    {"type": "unit_transfer", "operator": "o", "unit_id": "K1",
     "at": "2026-09-10T09:30:00Z", "to": "layup-room-2"},
    *[thaw(f"K{i}", "2026-09-10T10:30:00Z") for i in range(1, 7)],
)
ok = _s.startswith("201") and r["affected_jobs"] == []
print(f"{'PASS' if ok else 'FAIL'}  材料事件入链（尚无引用工单）: "
      f"[{_s}] affected={r.get('affected_jobs')}")
results.append(ok)

jid = new_job()
lay_with_units(jid, [f"K{i}" for i in range(1, 7)])
_g, r = rules_of(jid)
ok = r["release"] == "ok"
print(f"{'PASS' if ok else 'FAIL'}  谱系铺层放行: {r['release']}"
      + ("" if ok else json.dumps(r["violations"], ensure_ascii=False)))
results.append(ok)

# 外置继承：K1 出生时 R1 已在外 2h；自身 08:00→08:30 在外 0.5h，
# 10:30 再解冻至 11:01 铺放 0.52h → 合计约 3.0h
_s, st = call("GET", f"/jobs/{jid}/state")
k1 = st["materials"]["units"]["K1"]
r1 = st["materials"]["units"]["R1"]
u1 = [u for u in st["materials"]["usage"] if u["unit"] == "K1"][0]
ok = (k1["inherited_out_time_h"] == 2.0
      and abs(u1["out_time_at_placement_h"] - 3.02) < 0.05
      and r1["remaining_qty"] == 8.0
      and k1["placed_qty"] == 2.0 and k1["remaining_qty"] == 0.0
      and st["materials"]["units"]["REM1"]["remaining_qty"] == 0.0
      and st["materials"]["units"]["REM1B"]["remaining_qty"] == 50.0)
print(f"{'PASS' if ok else 'FAIL'}  外置沿父子链继承且数量核平: "
      f"K1 继承 {k1['inherited_out_time_h']}h 铺放时 "
      f"{u1['out_time_at_placement_h']}h，R1 余 {r1['remaining_qty']}m2，"
      f"REM1B 余 {st['materials']['units']['REM1B']['remaining_qty']}m2")
results.append(ok)
edges = {(e["parent"], e["child"]) for e in st["materials"]["edges"]}
ok = ("R1", "K1") in edges and ("REM1", "REM1A") in edges
print(f"{'PASS' if ok else 'FAIL'}  谱系父子边（含拆包）: {sorted(edges)[:3]}...")
results.append(ok)

# 2. 外置超限：父卷已消耗 3h（限 2h），套件继承后铺放即超限
reg_roll("R2", limit=2.0)
mevs(thaw("R2", "2026-09-10T06:00:00Z"),
     cut("R2", [kit("KA", 5.0), kit("KB", 5.0)], "2026-09-10T09:00:00Z"),
     fridge("R2", "2026-09-10T09:30:00Z"), fridge("KA", "2026-09-10T09:30:00Z"),
     thaw("KA", "2026-09-10T10:00:00Z"))
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    placed("P01", "2026-09-10T10:30:00Z", unit="KA")]})
vs = find(jid, "OUT_TIME_EXCEEDED")
ok = (len(vs) == 1 and vs[0]["plies"] == ["P01"]
      and vs[0]["details"]["units"] == ["KA"]
      and vs[0]["details"]["out_time_h"] == 4.0)  # 继承 3h + 自身 1h
print(f"{'PASS' if ok else 'FAIL'}  父卷消耗寿命计入套件 OUT_TIME_EXCEEDED: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
approve_blocked("错误批准路径: 继承外置超限", jid)

# 3. 同一裁片重复铺放（同工单两层引用同一套件）
reg_roll("R3")
mevs(thaw("R3", "2026-09-10T06:00:00Z"),
     cut("R3", [kit("KC", 2.0), kit("KD", 2.0)], "2026-09-10T08:00:00Z"),
     thaw("KC", "2026-09-10T09:00:00Z"), thaw("KD", "2026-09-10T09:00:00Z"))
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    placed("P01", "2026-09-10T10:00:00Z", unit="KC"),
    placed("P02", "2026-09-10T10:05:00Z", unit="KC"),
    placed("P03", "2026-09-10T10:10:00Z", unit="KD")]})
vs = find(jid, "UNIT_PLACED_TWICE")
ok = (len(vs) == 1 and set(vs[0]["plies"]) == {"P01", "P02"}
      and vs[0]["details"]["units"] == ["KC"]
      and len(vs[0]["details"]["refs"]) == 2)
print(f"{'PASS' if ok else 'FAIL'}  同一裁片重复铺放 UNIT_PLACED_TWICE: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
approve_blocked("错误批准路径: 裁片重复铺放", jid)

# 4. 跨工单重复铺放：两个工单引用同一裁片，两工单都被拦截
jid_b = new_job()
call("POST", f"/jobs/{jid_b}/events", {"events": [
    placed("P01", "2026-09-10T12:00:00Z", unit="KC")]})
for j, name in ((jid, "工单A"), (jid_b, "工单B")):
    vs = find(j, "UNIT_PLACED_TWICE")
    ok = bool(vs) and set(vs[0]["details"]["jobs"]) == {jid, jid_b}
    print(f"{'PASS' if ok else 'FAIL'}  跨工单重复铺放拦截（{name}）: "
          f"jobs={vs[0]['details']['jobs'] if vs else None}")
    results.append(ok)

# 5. 超额分配：子单元合计超过父卷剩余
reg_roll("R4", qty=10.0)
mevs(thaw("R4", "2026-09-10T06:00:00Z"),
     cut("R4", [kit("KE", 6.0)], "2026-09-10T08:00:00Z"),
     cut("R4", [kit("KF", 6.0)], "2026-09-10T08:30:00Z"))  # 累计 12 > 10
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    placed("P01", "2026-09-10T10:00:00Z", unit="KF")]})
vs = find(jid, "UNIT_OVER_ALLOCATED")
ok = (len(vs) == 1 and vs[0]["details"]["consumed"] == 6.0
      and vs[0]["details"]["remaining"] == 4.0
      and vs[0]["plies"] == ["P01"] and bool(vs[0]["details"]["events"]))
print(f"{'PASS' if ok else 'FAIL'}  超额分配 UNIT_OVER_ALLOCATED: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
approve_blocked("错误批准路径: 超额分配", jid)

# 6. 失效后使用：铺放时刻晚于整卷失效日期（沿谱系继承）
reg_roll("R5", exp="2026-09-09T00:00:00Z")
mevs(thaw("R5", "2026-09-08T06:00:00Z"),
     cut("R5", [kit("KG", 2.0)], "2026-09-08T08:00:00Z"),
     thaw("KG", "2026-09-10T08:00:00Z"))
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    placed("P01", "2026-09-10T10:00:00Z", unit="KG")]})
vs = find(jid, "UNIT_EXPIRED")
ok = len(vs) == 1 and vs[0]["plies"] == ["P01"] \
    and vs[0]["details"]["units"] == ["KG"]
print(f"{'PASS' if ok else 'FAIL'}  失效后使用 UNIT_EXPIRED: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
approve_blocked("错误批准路径: 失效后使用", jid)

# 7. 报废后使用：套件报废后再铺放；报废前的铺放不受影响
reg_roll("R6")
mevs(thaw("R6", "2026-09-10T06:00:00Z"),
     cut("R6", [kit("KH", 2.0), kit("KI", 2.0)], "2026-09-10T08:00:00Z"),
     thaw("KH", "2026-09-10T08:30:00Z"), thaw("KI", "2026-09-10T08:30:00Z"),
     {"type": "unit_scrap", "operator": "o", "unit_id": "KH",
      "at": "2026-09-10T09:00:00Z", "reason": "污染"})
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    placed("P01", "2026-09-10T10:00:00Z", unit="KH"),   # 报废后
    placed("P02", "2026-09-10T08:45:00Z", unit="KI")]})  # 正常
vs = find(jid, "UNIT_SCRAPPED")
ok = len(vs) == 1 and vs[0]["plies"] == ["P01"] \
    and vs[0]["details"]["units"] == ["KH"]
print(f"{'PASS' if ok else 'FAIL'}  报废后使用 UNIT_SCRAPPED: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
approve_blocked("错误批准路径: 报废后使用", jid)

# 8. 谱系成环：纠正绑定把整卷挂到自己的后代之下
reg_roll("R7")
mevs(thaw("R7", "2026-09-10T06:00:00Z"),
     cut("R7", [kit("KJ", 2.0)], "2026-09-10T08:00:00Z"),
     {"type": "unit_rebind", "operator": "o", "unit_id": "R7", "parent": "KJ",
      "at": "2026-09-10T09:00:00Z", "reason": "误操作演示环"},
     thaw("KJ", "2026-09-10T09:30:00Z"))
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    placed("P01", "2026-09-10T10:00:00Z", unit="KJ")]})
vs = find(jid, "GENEALOGY_CYCLE")
ok = len(vs) == 1 and set(vs[0]["details"]["units"]) == {"R7", "KJ"} \
    and bool(vs[0]["details"]["events"])
print(f"{'PASS' if ok else 'FAIL'}  谱系成环 GENEALOGY_CYCLE: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
approve_blocked("错误批准路径: 谱系成环", jid)

# 9. 解冻记录缺段：裁切缺时刻；父卷无解冻记录即裁切；重复解冻
reg_roll("R8")
mevs(cut("R8", [kit("KK", 2.0)], "2026-09-10T08:00:00Z"))  # 父卷无解冻记录
reg_roll("R9")
mevs(thaw("R9", "2026-09-10T06:00:00Z"),
     {"type": "unit_cut", "operator": "o", "parent": "R9",
      "children": [kit("KL", 2.0)]},                        # 缺 at
     thaw("R9", "2026-09-10T09:00:00Z"))                    # 重复解冻（未回冻）
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    placed("P01", "2026-09-10T10:00:00Z", unit="KK"),
    placed("P02", "2026-09-10T10:05:00Z", unit="KL")]})
vs = find(jid, "THAW_LOG_GAP")
units_hit = {u for v in vs for u in v["details"]["units"]}
ok = len(vs) >= 3 and {"R8", "R9"} <= units_hit \
    and all(v["details"]["events"] for v in vs)
print(f"{'PASS' if ok else 'FAIL'}  解冻记录缺段 THAW_LOG_GAP: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
approve_blocked("错误批准路径: 解冻记录缺段", jid)

# 10. 父子时刻冲突：裁切时刻早于父卷制造日期
reg_roll("R10", mfg="2026-09-10T00:00:00Z")
mevs(thaw("R10", "2026-09-10T06:00:00Z"),
     cut("R10", [kit("KM", 2.0)], "2026-09-09T08:00:00Z"))  # 早于制造日期
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    placed("P01", "2026-09-10T10:00:00Z", unit="KM")]})
vs = find(jid, "PARENT_CHILD_TIME_CONFLICT")
ok = len(vs) == 1 and vs[0]["details"]["units"] == ["R10"] \
    and vs[0]["plies"] == ["P01"]
print(f"{'PASS' if ok else 'FAIL'}  父子时刻冲突 PARENT_CHILD_TIME_CONFLICT: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
approve_blocked("错误批准路径: 父子时刻冲突", jid)

# 11. 数量无法闭合：声明裁下量与子单元合计不符（边料去向不明）；退库量不符
reg_roll("R11", qty=100.0)
mevs(thaw("R11", "2026-09-10T06:00:00Z"),
     cut("R11", [kit("KN", 8.0)], "2026-09-10T08:00:00Z", consumed=10.0),
     {"type": "unit_return", "operator": "o", "unit_id": "KN",
      "at": "2026-09-10T09:00:00Z", "qty": 7.5})  # 核算剩余 8，声明 7.5
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    placed("P01", "2026-09-10T10:00:00Z", unit="KN")]})
vs = find(jid, "QTY_NOT_CLOSED")
kinds = {(v["details"].get("declared"), v["details"]["units"][0]) for v in vs}
ok = len(vs) == 2 and (10.0, "R11") in kinds and (7.5, "KN") in kinds
print(f"{'PASS' if ok else 'FAIL'}  数量无法闭合 QTY_NOT_CLOSED: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
approve_blocked("错误批准路径: 数量无法闭合", jid)

# 12. 纠正绑定：套件错挂他卷导致超额，追加 rebind 事件纠正后放行
reg_roll("R12", qty=100.0)
reg_roll("R13", qty=5.0)
mevs(thaw("R12", "2026-09-10T06:00:00Z"),
     thaw("R13", "2026-09-10T06:00:00Z"),
     cut("R13", [kit("KO", 8.0)], "2026-09-10T08:00:00Z"))  # 错挂 R13 → 超额
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    placed("P01", "2026-09-10T10:00:00Z", unit="KO")]})
_g, r = rules_of(jid)
ok = "UNIT_OVER_ALLOCATED" in {v["rule"] for v in r["violations"]}
print(f"{'PASS' if ok else 'FAIL'}  错挂父卷导致超额: {sorted(v['rule'] for v in r['violations'])}")
results.append(ok)
approve_blocked("错误批准路径: 错挂父卷", jid)
_s, me = call("GET", "/materials/events")
n_before = len(me["events"])
_s, r = mevs({"type": "unit_rebind", "operator": "qe", "unit_id": "KO",
              "parent": "R12", "at": "2026-09-10T10:30:00Z",
              "reason": "裁切台账核对：KO 实际裁自 R12"})
ok = _s.startswith("201") and r["affected_jobs"] == [jid]
print(f"{'PASS' if ok else 'FAIL'}  纠正绑定追加记录且仅刷新引用工单: "
      f"[{_s}] affected={r.get('affected_jobs')}")
results.append(ok)
_s, me = call("GET", "/materials/events")
_g, r2 = rules_of(jid)
GENEALOGY_RULES = {"UNIT_UNKNOWN", "UNIT_PLACED_TWICE", "UNIT_OVER_ALLOCATED",
                   "UNIT_EXPIRED", "UNIT_SCRAPPED", "GENEALOGY_CYCLE",
                   "THAW_LOG_GAP", "PARENT_CHILD_TIME_CONFLICT",
                   "QTY_NOT_CLOSED"}
leftover = sorted({v["rule"] for v in r2["violations"]} & GENEALOGY_RULES)
ok = (len(me["events"]) == n_before + 1          # 旧事件保留，纠正为追加
      and not leftover)                          # 谱系违规全部闭环
print(f"{'PASS' if ok else 'FAIL'}  纠正后谱系违规闭环（事件链只增）: "
      f"events {n_before}→{len(me['events'])} 遗留={leftover}")
results.append(ok)
_s, st = call("GET", f"/jobs/{jid}/state")
ko = st["materials"]["units"]["KO"]
ok = ko["parent"] == "R12" and ko["rebinds"] and ko["remaining_qty"] == 0.0
print(f"{'PASS' if ok else 'FAIL'}  状态反映纠正后的父子关系: parent={ko['parent']}")
results.append(ok)

# 13. 兼容性：老 roll 输入工单不受谱系影响，materials 为空视图
jid = new_job(with_roll=True)
call("POST", f"/jobs/{jid}/events", {"events": [
    {"type": "roll_thawed", "operator": "o", "roll": "R1",
     "at": "2026-09-10T06:00:00Z"}] + [
    placed(f"P{i:02d}", f"2026-09-10T08:0{i}:00Z", roll="R1")
    for i in range(1, 7)]})
_g, r = rules_of(jid)
_s, st = call("GET", f"/jobs/{jid}/state")
ok = (r["release"] == "ok"
      and st["materials"] == {"units": {}, "edges": [], "usage": []})
print(f"{'PASS' if ok else 'FAIL'}  老 roll 工单兼容: release={r['release']} "
      f"materials={st['materials']}")
results.append(ok)
s, _a = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
ok = s.startswith("201")
print(f"{'PASS' if ok else 'FAIL'}  老工单批准 [{s}]")
results.append(ok)

# 14. 仅刷新引用该谱系的工单：无关工单不出现在 affected_jobs
reg_roll("R14")
mevs(thaw("R14", "2026-09-10T06:00:00Z"),
     cut("R14", [kit("KP", 2.0)], "2026-09-10T08:00:00Z"),
     thaw("KP", "2026-09-10T09:00:00Z"))
jid_a, jid_x = new_job(), new_job()
call("POST", f"/jobs/{jid_a}/events", {"events": [
    placed("P01", "2026-09-10T10:00:00Z", unit="KP")]})
call("POST", f"/jobs/{jid_x}/events", {"events": [
    placed("P01", "2026-09-10T10:00:00Z", unit="KN")]})  # 另一谱系
_s, r = mevs({"type": "unit_scrap", "operator": "o", "unit_id": "KP",
              "at": "2026-09-10T11:00:00Z", "reason": "复检不合格"})
ok = r["affected_jobs"] == [jid_a]
print(f"{'PASS' if ok else 'FAIL'}  材料事件仅刷新引用谱系的工单: "
      f"affected={r['affected_jobs']}（无关工单 {jid_x} 不在内）")
results.append(ok)
_s, r = call("GET", "/materials/units/KP/impact")
ok = (len(r["affected_jobs"]) == 1 and r["affected_jobs"][0]["job_id"] == jid_a
      and r["affected_jobs"][0]["release"] == "rejected")
print(f"{'PASS' if ok else 'FAIL'}  impact 端点给出受影响工单刷新结果: "
      f"{json.dumps(r, ensure_ascii=False)}")
results.append(ok)

# 15. 批准快照保存谱系；版本差异与随件包据此生成
reg_roll("R15")
mevs(thaw("R15", "2026-09-10T06:00:00Z"),
     cut("R15", [kit(f"KQ{i}", 2.0) for i in range(1, 7)]
         + [kit("REM15", 80.0, "remnant")], "2026-09-10T08:00:00Z"),
     fridge("R15", "2026-09-10T09:00:00Z"),
     *[fridge(f"KQ{i}", "2026-09-10T09:00:00Z") for i in range(1, 7)],
     *[thaw(f"KQ{i}", "2026-09-10T10:00:00Z") for i in range(1, 7)])
jid = new_job()
lay_with_units(jid, [f"KQ{i}" for i in range(1, 7)])
_g, r = rules_of(jid)
ok = r["release"] == "ok"
print(f"{'PASS' if ok else 'FAIL'}  快照场景放行: {r['release']}"
      + ("" if ok else json.dumps(r["violations"], ensure_ascii=False)))
results.append(ok)
s, _a = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
ok = s.startswith("201")
print(f"{'PASS' if ok else 'FAIL'}  谱系工单批准 v1 [{s}]")
results.append(ok)
_s, pkg = call("GET", f"/jobs/{jid}/approvals/1/package")
m = pkg["materials"]
kq1 = m["units"]["KQ1"]
ok = (("R15", "KQ1") in {(e["parent"], e["child"]) for e in m["edges"]}
      and kq1["inherited_out_time_h"] == 2.0
      and kq1["placed_qty"] == 2.0
      and m["units"]["R15"]["remaining_qty"] == 8.0
      and any(u["unit"] == "KQ1" and u["out_time_at_placement_h"] == 4.02
              for u in m["usage"]))
print(f"{'PASS' if ok else 'FAIL'}  批准版保存父子关系/寿命明细/数量结果: "
      f"KQ1 继承 {kq1['inherited_out_time_h']}h 消耗 {kq1['placed_qty']}m2，"
      f"R15 余 {m['units']['R15']['remaining_qty']}m2")
results.append(ok)
# 追加材料事件（余料报废 + 整卷再次出库）→ v2 快照应体现数量与寿命变化
mevs({"type": "unit_scrap", "operator": "o", "unit_id": "REM15",
      "at": "2026-09-10T12:00:00Z", "qty": 10.0, "reason": "边料老化"},
     thaw("R15", "2026-09-10T12:30:00Z"))
call("POST", f"/jobs/{jid}/events",
     {"type": "note", "operator": "qe", "text": "谱系复核"})
s, _a = call("POST", f"/jobs/{jid}/approve", {"approved_by": "qe-wang"})
ok = s.startswith("201")
print(f"{'PASS' if ok else 'FAIL'}  谱系工单批准 v2 [{s}]")
results.append(ok)
_s, diff = call("GET", f"/jobs/{jid}/approvals/diff", query="a=1&b=2")
md = diff["diff"]["materials"]
qty_hit = {c["unit"] for c in md["qty_changed"]}
ot_hit = {c["unit"] for c in md["out_time_changed"]}
ok = ("REM15" in qty_hit and "R15" in ot_hit
      and {c["unit"] for c in md["parent_changed"]} == set()
      and md["units_added"] == [] and md["units_removed"] == [])
print(f"{'PASS' if ok else 'FAIL'}  版本差异标出谱系数量/寿命变化: "
      f"qty={sorted(qty_hit)} out_time={sorted(ot_hit)}")
results.append(ok)
_s, pkg2 = call("GET", f"/jobs/{jid}/approvals/2/package")
ok = (pkg2["materials"]["units"]["REM15"]["scrapped_qty"] == 10.0
      and pkg2["materials"]["units"]["REM15"]["status"] == "scrapped")
print(f"{'PASS' if ok else 'FAIL'}  v2 随件包反映报废结果: "
      f"REM15 scrapped={pkg2['materials']['units']['REM15']['scrapped_qty']}")
results.append(ok)

# 16. 入链校验：父单元未登记 / 子标识占用 / 数量非正 / 重复登记
s, r = mevs(cut("R404", [kit("KX", 1.0)], "2026-09-10T08:00:00Z"))
ok = s.startswith("400")
print(f"{'PASS' if ok else 'FAIL'}  裁切父单元未登记被拒 [{s}]")
results.append(ok)
s, r = mevs(cut("R15", [kit("KQ1", 1.0)], "2026-09-10T08:00:00Z"))
ok = s.startswith("400")
print(f"{'PASS' if ok else 'FAIL'}  子单元标识重复被拒 [{s}]")
results.append(ok)
s, r = mevs(cut("R15", [kit("KY", -1.0)], "2026-09-10T08:00:00Z"))
ok = s.startswith("400")
print(f"{'PASS' if ok else 'FAIL'}  子单元数量非正被拒 [{s}]")
results.append(ok)
s, r = reg_roll("R15")
ok = s.startswith("409")
print(f"{'PASS' if ok else 'FAIL'}  整卷重复登记被拒 [{s}]")
results.append(ok)
s, r = call("POST", "/materials/events",
            {"type": "unit_thawed", "unit_id": "R15", "at": "not-a-time"})
ok = s.startswith("400")
print(f"{'PASS' if ok else 'FAIL'}  材料事件坏时标被拒 [{s}]")
results.append(ok)

# 17. 铺层引用未登记单元；缺 roll 且缺 unit 仍报 DATA_MISSING
jid = new_job()
call("POST", f"/jobs/{jid}/events", {"events": [
    placed("P01", "2026-09-10T10:00:00Z", unit="K404")]})
vs = find(jid, "UNIT_UNKNOWN")
ok = len(vs) == 1 and vs[0]["plies"] == ["P01"] \
    and vs[0]["details"]["units"] == ["K404"]
print(f"{'PASS' if ok else 'FAIL'}  引用未登记单元 UNIT_UNKNOWN: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
jid = new_job()
e = placed("P01", "2026-09-10T10:00:00Z")  # 既无 roll 也无 unit
call("POST", f"/jobs/{jid}/events", {"events": [e]})
vs = find(jid, "DATA_MISSING")
ok = bool(vs) and "roll" in vs[0]["details"].get("missing", [])
print(f"{'PASS' if ok else 'FAIL'}  缺材料引用 DATA_MISSING: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)

# 18. 回归：整卷 10.0 单次裁出 12.0 → 最终台账核平报超额，拒绝批准
reg_roll("RQ", qty=10.0)
mevs(thaw("RQ", "2026-09-10T06:00:00Z"),
     cut("RQ", [kit(f"QR{i}", 2.0) for i in range(1, 7)],
         "2026-09-10T08:00:00Z"))  # 合计 12 > 10
jid = new_job()
lay_with_units(jid, [f"QR{i}" for i in range(1, 7)])
vs = find(jid, "UNIT_OVER_ALLOCATED")
_g, r = rules_of(jid)
ok = (len(vs) == 1 and vs[0]["details"]["consumed"] == 12.0
      and vs[0]["details"]["remaining"] == 10.0
      and bool(vs[0]["details"]["events"])
      and r["release"] == "rejected")
print(f"{'PASS' if ok else 'FAIL'}  单次裁出超额被最终台账拦截: "
      f"{json.dumps(vs, ensure_ascii=False)}")
results.append(ok)
_s, st = call("GET", f"/jobs/{jid}/state")
ok = st["materials"]["units"]["RQ"]["remaining_qty"] == -2.0
print(f"{'PASS' if ok else 'FAIL'}  台账亏损留痕 remaining_qty=-2.0: "
      f"{st['materials']['units']['RQ']['remaining_qty']}")
results.append(ok)
approve_blocked("错误批准路径: 单次裁出超额", jid)

# 19. 回归：unit_scrap 省略 qty → 按事件时剩余解析，材料查询/校验不再 500
reg_roll("RS", qty=50.0)
mevs(thaw("RS", "2026-09-10T06:00:00Z"),
     cut("RS", [kit("QS1", 20.0), kit("QS2", 30.0, "remnant")],
         "2026-09-10T08:00:00Z"),
     {"type": "unit_scrap", "operator": "o", "unit_id": "QS1",
      "at": "2026-09-10T09:00:00Z", "reason": "污染"})  # 缺省 qty = 全部剩余
_s, r = call("GET", "/materials/units")
us = {u["unit_id"]: u for u in r["units"]}
ok = (_s.startswith("200") and us["QS1"]["scrapped_qty"] == 20.0
      and us["QS1"]["status"] == "scrapped"
      and us["QS1"]["remaining_qty"] == 0.0
      and us["RS"]["remaining_qty"] == 0.0)  # RS：50 − 20(QS1) − 30(QS2)
print(f"{'PASS' if ok else 'FAIL'}  缺省报废量按事件时剩余解析: "
      f"QS1 scrapped={us['QS1']['scrapped_qty']} RS remaining="
      f"{us['RS']['remaining_qty']} [{_s}]")
results.append(ok)
_s, r = call("GET", "/materials/units/QS1")
ok = _s.startswith("200") and r["unit_id"] == "QS1"
print(f"{'PASS' if ok else 'FAIL'}  单元详情查询恢复 [{_s}]")
results.append(ok)
jid = new_job()  # 不引用任何谱系的无关工单
call("POST", f"/jobs/{jid}/events", {"events": [
    placed("P01", "2026-09-10T10:00:00Z", unit="K404")]})
_s, r = call("GET", f"/jobs/{jid}/validate")
ok = _s.startswith("200") and r["release"] == "rejected"  # UNIT_UNKNOWN 而非 500
print(f"{'PASS' if ok else 'FAIL'}  无关工单校验恢复 [{_s}]: {r['release']}")
results.append(ok)
_s, r = call("GET", "/materials/units/QS1/impact")
ok = _s.startswith("200")
print(f"{'PASS' if ok else 'FAIL'}  impact 查询恢复 [{_s}]")
results.append(ok)

print(f"\n{sum(results)}/{len(results)} 通过")
raise SystemExit(0 if all(results) else 1)
