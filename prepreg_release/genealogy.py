"""材料谱系：整卷登记与裁切/拆包/转移/退库/报废的追加事件重放。

背景：料卷裁成下料套件后常先回冷库、再拆给不同工单；若各工单都从本次
解冻重新计时，父卷已消耗的外置寿命与边料去向就会丢失。本模块把材料流
转建模为追加式事件链（material_events 表，只允许 INSERT），分析时沿
父子链继承外置时长并核平数量；纠正绑定（unit_rebind）同样以追加事件
闭环，不改旧记录。

事件模型：
  unit_registered   登记整卷 {unit_id, batch_no, material, unit, initial_qty,
                              manufactured_at?, expires_at?, out_time_limit_h?}
  unit_cut          裁切 {parent, at, children:[{unit_id, kind?, qty}], consumed?}
  unit_split        拆包 {parent, at, children:[...]}（套件分派不同工单）
  unit_transfer     转移 {unit_id, at, to?}
  unit_return       退库 {unit_id, at, qty?}
  unit_scrap        报废 {unit_id, at, qty?, reason?}
  unit_thawed       解冻 {unit_id, at}
  unit_refrigerated 回冻 {unit_id, at}
  unit_rebind       纠正绑定 {unit_id, parent, at, reason?}

铺层事件（ply_placed / ply_replaced.replacement）以 unit 字段引用材料单
元（无 unit 时回退原有 roll 逻辑）。子单元出生时继承父单元的累计外置
时长与在外状态，之后走自身解冻台账；裁片被引用即视为消耗（揭除不释放），
同一裁片不得二次铺放。
"""

from datetime import datetime, timezone

from .core import parse_time

EVENT_TYPES = {
    "unit_registered", "unit_cut", "unit_split", "unit_transfer",
    "unit_return", "unit_scrap", "unit_thawed", "unit_refrigerated",
    "unit_rebind",
}

QTY_TOL = 1e-6


# ---------------------------------------------------------------- 工具

def _num(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x)


def _new_unit(uid, kind, src, parent=None, born_event=None, born_at=None):
    return {
        "unit_id": uid, "kind": kind, "parent": parent,
        "batch_no": src.get("batch_no"), "material": src.get("material"),
        "manufactured_at": src.get("manufactured_at"),
        "expires_at": src.get("expires_at"),
        "unit": src.get("unit"),
        "qty": _num(src.get("initial_qty")) if kind == "roll"
        else _num(src.get("qty")),
        "out_time_limit_h": _num(src.get("out_time_limit_h")),
        "born_event": born_event, "born_at": born_at,
        "cuts": [],               # [{event, at, consumed, children}]
        "scraps": [],             # [{event, at, qty}]
        "placed_qty": 0.0,        # 铺层消耗（引用即耗尽）
        "status": "active", "scrapped_at": None, "scrapped_event": None,
        "ledger": [],             # [(ts, 'thaw'|'fridge', seq)]
        "returns": [], "transfers": [], "rebinds": [],
    }


def _inherit(units, uid, key):
    """沿父链取属性（子单元未显式声明时继承整卷登记值）；成环时止于已访节点。"""
    seen = set()
    cur = uid
    while cur is not None and cur in units and cur not in seen:
        seen.add(cur)
        v = units[cur].get(key)
        if v is not None:
            return v
        cur = units[cur]["parent"]
    return None


def _expiry_of(units, uid):
    return parse_time(_inherit(units, uid, "expires_at"))


def _remaining(u, before=None):
    """剩余数量 = 登记量 − 裁切分出 − 报废 − 铺层消耗。

    before 给出时忽略时刻晚于它的报废（铺层在报废前发生时数量判定不受其影响）。
    """
    rem = (u["qty"] or 0.0) - u["placed_qty"]
    for c in u["cuts"]:
        rem -= c["consumed"]
    for s in u["scraps"]:
        if before is not None and s["at"] is not None and s["at"] > before:
            continue
        rem -= s["qty"] or 0.0  # 缺省报废量未解析（无有效初始数量）时按 0 计
    return rem


def _state_at(units, uid, t, visiting=()):
    """单元在时刻 t 的在外状态：'thaw' | 'fridge'；出生状态继承父单元。"""
    u = units.get(uid)
    if u is None or uid in visiting:
        return "fridge"
    state = "fridge"
    born = u["born_at"]
    pid = u["parent"]
    if pid in units and born is not None:
        state = _state_at(units, pid, born, visiting + (uid,))
    for ts, kind, _seq in sorted(u["ledger"], key=lambda x: (x[0], x[2])):
        if born is not None and ts < born:
            continue  # 出生前事件：时刻冲突已另报
        if t is not None and ts > t:
            break
        state = "thaw" if kind == "thaw" else "fridge"
    return state


def _out_time_at(units, uid, t, visiting=()):
    """截至时刻 t 的累计外置小时：出生时继承父链累计值，再叠加自身台账。"""
    if t is None:
        return 0.0
    u = units.get(uid)
    if u is None or uid in visiting:
        return 0.0
    born = u["born_at"]
    pid = u["parent"]
    total, start = 0.0, None
    if pid in units and born is not None:
        total = _out_time_at(units, pid, born, visiting + (uid,))
        if _state_at(units, pid, born, visiting + (uid,)) == "thaw":
            start = born  # 出生即在外
    for ts, kind, _seq in sorted(u["ledger"], key=lambda x: (x[0], x[2])):
        if born is not None and ts < born:
            continue
        if ts > t:
            break
        if kind == "thaw":
            if start is None:
                start = ts
        elif start is not None:
            end = min(ts, t)
            if end > start:
                total += (end - start).total_seconds() / 3600.0
            start = None
    if start is not None and t > start:
        total += (t - start).total_seconds() / 3600.0
    return total


def _ref_key(r):
    t = parse_time(r.get("placed_at"))
    return (t is None, t or datetime.max.replace(tzinfo=timezone.utc),
            r.get("event_seq") or 0)


def known_unit_ids(events):
    """事件链物化出的全部材料单元标识（整卷 + 裁切/拆包子单元）。"""
    ids = set()
    for ev in events:
        t, p = ev["type"], ev["payload"]
        if t == "unit_registered":
            ids.add(p.get("unit_id"))
        elif t in ("unit_cut", "unit_split"):
            for c in p.get("children") or []:
                if isinstance(c, dict):
                    ids.add(c.get("unit_id"))
    return ids - {None}


def _parent_map(events):
    """最终父子关系（裁切/拆包建边，纠正绑定改边）。"""
    pm = {}
    for ev in events:
        t, p = ev["type"], ev["payload"]
        if t in ("unit_cut", "unit_split"):
            for c in p.get("children") or []:
                if isinstance(c, dict) and c.get("unit_id"):
                    pm[c["unit_id"]] = p.get("parent")
        elif t == "unit_rebind" and p.get("unit_id"):
            pm[p["unit_id"]] = p.get("parent")
    return pm


def _closure(parent_map, seeds):
    """种子单元的谱系闭包：祖先链 + 全部后代。"""
    closure = set(seeds)
    for uid in list(closure):
        cur, seen = parent_map.get(uid), set()
        while cur and cur not in seen:
            seen.add(cur)
            closure.add(cur)
            cur = parent_map.get(cur)
    grew = True
    while grew:
        grew = False
        for uid, pid in parent_map.items():
            if pid in closure and uid not in closure:
                closure.add(uid)
                grew = True
    return closure


def affected_jobs(events, usage, touched):
    """材料事件变动后需要刷新的工单：引用了该谱系闭包任何单元的工单。"""
    closure = _closure(_parent_map(events), touched)
    return sorted({r["job_id"] for uid in closure for r in usage.get(uid, [])})


# ---------------------------------------------------------------- 事件重放

def _replay_cut(units, ev, prob):
    t, p, seq = ev["type"], ev["payload"], ev["seq"]
    verb = "裁切" if t == "unit_cut" else "拆包"
    parent_id = p.get("parent")
    parent = units.get(parent_id)
    if parent is None:
        prob("UNIT_UNKNOWN",
             f"{verb}事件 #{seq} 引用未登记的父单元 {parent_id}",
             [parent_id], event=seq)
        return
    at = parse_time(p.get("at"))
    if at is None:
        prob("THAW_LOG_GAP",
             f"{verb}事件 #{seq} 缺少可解析时刻，套件无法继承父单元 "
             f"{parent_id} 的外置时长", [parent_id], event=seq)
    if not parent["ledger"] and parent["kind"] == "roll":
        prob("THAW_LOG_GAP",
             f"父卷 {parent_id} 没有任何解冻记录即{verb}（事件 #{seq}），"
             f"外置时长无法核计", [parent_id], event=seq)
    born = parent["born_at"]
    if at is not None and born is not None and at < born:
        prob("PARENT_CHILD_TIME_CONFLICT",
             f"{verb}事件 #{seq} 时刻 {p.get('at')} 早于父单元 {parent_id} "
             f"的产生/制造时刻", [parent_id], event=seq)
    if parent["status"] == "scrapped":
        sat = parent["scrapped_at"]
        if sat is None or at is None or at >= sat:
            prob("UNIT_SCRAPPED",
                 f"{verb}事件 #{seq} 的父单元 {parent_id} 已报废"
                 f"（报废事件 #{parent['scrapped_event']}）",
                 [parent_id], event=seq)
    exp = _expiry_of(units, parent_id)
    if exp is not None and at is not None and at > exp:
        prob("UNIT_EXPIRED",
             f"{verb}事件 #{seq} 时刻晚于父单元 {parent_id} 的失效日期 "
             f"{_inherit(units, parent_id, 'expires_at')}",
             [parent_id], event=seq)

    children = p.get("children") or []
    total, made = 0.0, []
    for c in children:
        if not isinstance(c, dict):
            continue
        cid, qty = c.get("unit_id"), _num(c.get("qty"))
        if not cid or cid in units or qty is None:
            continue  # 唯一标识/正数量由入链校验保证；防御性跳过
        total += qty
        child = _new_unit(cid, c.get("kind") or "kit", c, parent=parent_id,
                          born_event=seq, born_at=at)
        child["qty"] = qty
        units[cid] = child
        made.append(cid)
    declared = _num(p.get("consumed"))
    if declared is not None and abs(declared - total) > QTY_TOL:
        prob("QTY_NOT_CLOSED",
             f"{verb}事件 #{seq} 声明从 {parent_id} 裁下 {declared:g}，"
             f"子单元合计 {total:g}，差 {round(declared - total, 6):g} "
             f"边料去向不明，数量无法闭合",
             [parent_id] + made, event=seq,
             declared=declared, allocated=round(total, 6))
    consumed = declared if declared is not None else total
    # 超额分配在重放后的数量核平阶段按最终台账判定（纠正绑定会转移归属）
    parent["cuts"].append({"event": seq, "at": at, "consumed": consumed,
                           "children": made, "kind": t})


# ---------------------------------------------------------------- 主评估

def evaluate(events, usage, focus_job=None):
    """重放材料事件链并核算谱系规则。

    events    材料事件链 [{seq, type, payload, ...}]（按 seq 升序）
    usage     {unit_id: [{job_id, ply_id, event_seq, placed_at}]}（跨全部工单
              的铺层引用；裁片被引用即消耗，揭除不释放）
    focus_job 若给出，仅返回影响该工单的违规与该工单引用的谱系视图

    返回 {"state": {"units": ..., "edges": ..., "usage": ...},
          "violations": [...]}。
    """
    units = {}
    raw = []

    def prob(rule, message, unit_ids, event=None, events=None, refs=None,
             **extra):
        evs = list(events or [])
        if event is not None:
            evs.append(event)
        raw.append({
            "rule": rule, "message": message,
            "units": [u for u in unit_ids if u],
            "events": sorted({e for e in evs if e is not None}),
            "refs": refs or [], "extra": extra,
        })

    # ---------------- 1. 重放材料事件链 ----------------
    for ev in events:
        t, p, seq = ev["type"], ev["payload"], ev["seq"]
        if t == "unit_registered":
            uid = p.get("unit_id")
            if not uid or uid in units:
                continue  # 唯一性由入链保证；重复登记以首条为准
            born = parse_time(p.get("manufactured_at")) or parse_time(p.get("at"))
            units[uid] = _new_unit(uid, "roll", p, born_event=seq, born_at=born)
        elif t in ("unit_cut", "unit_split"):
            _replay_cut(units, ev, prob)
        elif t == "unit_scrap":
            u = units.get(p.get("unit_id"))
            if u is None:
                prob("UNIT_UNKNOWN",
                     f"报废事件 #{seq} 引用未登记的材料单元 {p.get('unit_id')}",
                     [p.get("unit_id")], event=seq)
                continue
            at = parse_time(p.get("at"))
            qty = _num(p.get("qty"))
            if qty is None:
                qty = None  # 缺省为全部剩余，核平阶段按最终台账结算
            u["scraps"].append({"event": seq, "at": at, "qty": qty})
            u["status"] = "scrapped"
            u["scrapped_at"] = at
            u["scrapped_event"] = seq
        elif t in ("unit_thawed", "unit_refrigerated"):
            u = units.get(p.get("unit_id"))
            if u is None:
                prob("UNIT_UNKNOWN",
                     f"{'解冻' if t == 'unit_thawed' else '回冻'}事件 #{seq} "
                     f"引用未登记的材料单元 {p.get('unit_id')}",
                     [p.get("unit_id")], event=seq)
                continue
            at = parse_time(p.get("at"))
            if at is None:
                prob("THAW_LOG_GAP",
                     f"材料单元 {u['unit_id']} 的"
                     f"{'解冻' if t == 'unit_thawed' else '回冻'}事件 #{seq} "
                     f"缺少可解析时刻，解冻记录缺段",
                     [u["unit_id"]], event=seq)
                continue
            if u["born_at"] is not None and at < u["born_at"]:
                prob("PARENT_CHILD_TIME_CONFLICT",
                     f"材料单元 {u['unit_id']} 的事件 #{seq} 时刻 "
                     f"{p.get('at')} 早于其产生时刻",
                     [u["unit_id"]], event=seq)
            u["ledger"].append(
                (at, "thaw" if t == "unit_thawed" else "fridge", seq))
        elif t == "unit_return":
            u = units.get(p.get("unit_id"))
            if u is None:
                prob("UNIT_UNKNOWN",
                     f"退库事件 #{seq} 引用未登记的材料单元 {p.get('unit_id')}",
                     [p.get("unit_id")], event=seq)
                continue
            # 声明量与核算剩余的对账在核平阶段按最终台账判定
            u["returns"].append({"event": seq,
                                 "at": parse_time(p.get("at")),
                                 "qty": _num(p.get("qty"))})
        elif t == "unit_transfer":
            u = units.get(p.get("unit_id"))
            if u is None:
                prob("UNIT_UNKNOWN",
                     f"转移事件 #{seq} 引用未登记的材料单元 {p.get('unit_id')}",
                     [p.get("unit_id")], event=seq)
                continue
            u["transfers"].append({"event": seq,
                                   "at": parse_time(p.get("at")),
                                   "to": p.get("to")})
        elif t == "unit_rebind":
            u = units.get(p.get("unit_id"))
            new_parent = p.get("parent")
            if u is None or new_parent not in units:
                prob("UNIT_UNKNOWN",
                     f"纠正绑定事件 #{seq} 引用未登记的材料单元 "
                     f"{p.get('unit_id')} 或父单元 {new_parent}",
                     [p.get("unit_id"), new_parent], event=seq)
                continue
            old = units.get(u["parent"])
            if old is not None:
                # 数量归属随纠正转移：出生裁切组从旧父账上减去该单元份额
                for grp in old["cuts"]:
                    if u["unit_id"] in grp["children"]:
                        grp["children"].remove(u["unit_id"])
                        grp["consumed"] -= u["qty"] or 0.0
                        if not grp["children"] and abs(grp["consumed"]) <= QTY_TOL:
                            old["cuts"].remove(grp)
                        break
            u["parent"] = new_parent
            np = units[new_parent]
            np["cuts"].append({"event": seq, "at": parse_time(p.get("at")),
                               "consumed": u["qty"] or 0.0,
                               "children": [u["unit_id"]], "kind": t})
            u["rebinds"].append({"event": seq, "parent": new_parent,
                                 "at": parse_time(p.get("at")),
                                 "reason": p.get("reason")})
            if u["born_at"] is not None and np["born_at"] is not None \
                    and u["born_at"] < np["born_at"]:
                prob("PARENT_CHILD_TIME_CONFLICT",
                     f"纠正绑定事件 #{seq} 把 {u['unit_id']} 挂到 "
                     f"{new_parent} 之下，但子单元产生时刻早于新父单元",
                     [u["unit_id"], new_parent], event=seq)

    # ---------------- 2. 数量核平（基于纠正绑定后的最终台账） ----------------
    # 按事件序重放每单元的扣减（裁切/拆包分出、报废、纠正绑定转入），
    # 识别超额分配；缺省报废量按事件发生时的剩余量解析，避免 None 参与汇总。
    _VERB = {"unit_cut": "裁切", "unit_split": "拆包", "unit_rebind": "纠正绑定"}
    for uid in sorted(units):
        u = units[uid]
        if not u["qty"]:
            continue  # 整卷未登记有效初始数量：QTY_NOT_CLOSED 在步骤 5 报
        debits = [(c["event"], "cut", c) for c in u["cuts"]]
        debits += [(s["event"], "scrap", s) for s in u["scraps"]]
        debits.sort(key=lambda d: d[0])
        avail = u["qty"]
        for seq, kind, d in debits:
            if kind == "cut":
                amt = d["consumed"]
                if amt > avail + QTY_TOL:
                    prob("UNIT_OVER_ALLOCATED",
                         f"{_VERB.get(d['kind'], '裁切')}事件 #{seq} 从 {uid} "
                         f"分出 {amt:g}，超过其剩余 {round(avail, 6):g}"
                         f"（超额分配）",
                         [uid] + list(d["children"]), event=seq,
                         consumed=amt, remaining=round(avail, 6))
                avail -= amt
            else:
                if d["qty"] is None:
                    d["qty"] = max(avail, 0.0)  # 缺省报废量 = 当时剩余
                elif d["qty"] > avail + QTY_TOL:
                    prob("UNIT_OVER_ALLOCATED",
                         f"报废事件 #{seq} 报废 {d['qty']:g}，超过单元 {uid} "
                         f"剩余 {round(avail, 6):g}",
                         [uid], event=seq,
                         qty=d["qty"], remaining=round(avail, 6))
                avail -= d["qty"]
        # 退库声明量对账：按事件序定位当时核算剩余
        for r in u["returns"]:
            if r["qty"] is None:
                continue
            rem = u["qty"]
            rem -= sum(c["consumed"] for c in u["cuts"]
                       if c["event"] < r["event"])
            rem -= sum(s["qty"] for s in u["scraps"]
                       if s["event"] < r["event"])
            if abs(r["qty"] - rem) > QTY_TOL:
                prob("QTY_NOT_CLOSED",
                     f"退库事件 #{r['event']} 声明退回 {r['qty']:g}，与单元 "
                     f"{uid} 核算剩余 {round(rem, 6):g} 不符，数量无法闭合",
                     [uid], event=r["event"],
                     declared=r["qty"], remaining=round(rem, 6))

    # ---------------- 3. 谱系成环 ----------------
    reported = set()
    for uid in sorted(units):
        seen, cur = [], uid
        while cur is not None and cur in units:
            if cur in seen:
                cyc = seen[seen.index(cur):]
                key = tuple(sorted(cyc))
                if key not in reported:
                    reported.add(key)
                    evs = [units[c]["born_event"] for c in cyc]
                    evs += [r["event"] for c in cyc for r in units[c]["rebinds"]]
                    prob("GENEALOGY_CYCLE",
                         f"材料谱系成环：{' → '.join(cyc + [cyc[0]])}",
                         cyc, events=[e for e in evs if e is not None])
                break
            seen.append(cur)
            cur = units[cur]["parent"]

    # ---------------- 4. 解冻台账缺段（逐单元一次） ----------------
    for uid in sorted(units):
        u = units[uid]
        state = "fridge"
        if u["parent"] in units and u["born_at"] is not None:
            state = _state_at(units, u["parent"], u["born_at"])
        for ts, kind, seq in sorted(u["ledger"], key=lambda x: (x[0], x[2])):
            if u["born_at"] is not None and ts < u["born_at"]:
                continue  # 出生前事件：时刻冲突已报
            if kind == "thaw" and state == "thaw":
                prob("THAW_LOG_GAP",
                     f"材料单元 {uid} 事件 #{seq} 重复解冻（上一段尚未回冻），"
                     f"解冻记录缺段", [uid], event=seq)
            elif kind == "fridge" and state == "fridge":
                prob("THAW_LOG_GAP",
                     f"材料单元 {uid} 事件 #{seq} 回冻时没有在外记录，"
                     f"解冻记录缺段", [uid], event=seq)
            state = "thaw" if kind == "thaw" else "fridge"

    # ---------------- 5. 整卷初始数量 ----------------
    for uid in sorted(units):
        u = units[uid]
        if u["kind"] == "roll" and not u["qty"]:
            prob("QTY_NOT_CLOSED",
                 f"整卷 {uid} 未登记有效初始数量，数量无法闭合",
                 [uid], event=u["born_event"])

    # ---------------- 6. 铺层引用（裁片消耗与使用拦截） ----------------
    for uid in sorted(usage):
        refs = usage[uid]
        u = units.get(uid)
        if u is None:
            prob("UNIT_UNKNOWN",
                 f"铺层引用未登记的材料单元 {uid}", [uid], refs=refs)
            continue
        if len(refs) > 1:
            prob("UNIT_PLACED_TWICE",
                 f"同一裁片 {uid} 被铺放引用 {len(refs)} 次"
                 f"（裁片只能铺一次）", [uid], refs=refs, count=len(refs))
        first = min(refs, key=_ref_key)
        placed = parse_time(first.get("placed_at"))
        if placed is not None and u["born_at"] is not None \
                and placed < u["born_at"]:
            prob("PARENT_CHILD_TIME_CONFLICT",
                 f"铺层 {first['ply_id']} 铺放时刻早于材料单元 {uid} 的产生时刻",
                 [uid], refs=[first])
        if u["status"] == "scrapped":
            sat = u["scrapped_at"]
            if sat is None or placed is None or placed >= sat:
                prob("UNIT_SCRAPPED",
                     f"铺层 {first['ply_id']} 使用已报废的材料单元 {uid}"
                     f"（报废事件 #{u['scrapped_event']}）",
                     [uid], refs=[first], event=u["scrapped_event"])
        exp = _expiry_of(units, uid)
        if exp is not None and placed is not None and placed > exp:
            prob("UNIT_EXPIRED",
                 f"铺层 {first['ply_id']} 铺放时刻晚于材料单元 {uid} 的失效日期 "
                 f"{_inherit(units, uid, 'expires_at')}",
                 [uid], refs=[first])
        if placed is not None:
            hours = _out_time_at(units, uid, placed)
            if hours <= QTY_TOL and u["born_at"] is not None \
                    and _state_at(units, uid, placed) == "fridge":
                prob("OUT_TIME_DATA_MISSING",
                     f"铺层 {first['ply_id']} 铺放时材料单元 {uid} 没有在外记录"
                     f"（解冻记录缺失）", [uid], refs=[first])
            else:
                limit = _inherit(units, uid, "out_time_limit_h")
                if limit is not None and hours > limit:
                    prob("OUT_TIME_EXCEEDED",
                         f"铺层 {first['ply_id']} 铺放时材料单元 {uid} 累计外置 "
                         f"{hours:.1f}h（含父链继承），超过上限 {limit:g}h",
                         [uid], refs=[first],
                         out_time_h=round(hours, 2), limit_h=limit)
        avail = _remaining(u, before=placed)
        if u["qty"] and avail <= QTY_TOL:
            prob("UNIT_OVER_ALLOCATED",
                 f"铺层 {first['ply_id']} 引用材料单元 {uid} 时其数量已耗尽"
                 f"（剩余 {round(avail, 6):g}）",
                 [uid], refs=[first], remaining=round(avail, 6))
        u["placed_qty"] = max(avail, 0.0) if u["qty"] else 0.0

    # ---------------- 7. 汇总状态 ----------------
    latest = None
    for ev in events:
        ts = parse_time(ev["payload"].get("at")) or parse_time(ev.get("recorded_at"))
        if ts and (latest is None or ts > latest):
            latest = ts
    for refs in usage.values():
        for r in refs:
            ts = parse_time(r.get("placed_at"))
            if ts and (latest is None or ts > latest):
                latest = ts

    units_out = {}
    for uid in sorted(units):
        u = units[uid]
        born = u["born_at"]
        inherited = 0.0
        if u["parent"] in units and born is not None:
            inherited = _out_time_at(units, u["parent"], born)
        units_out[uid] = {
            "unit_id": uid, "kind": u["kind"], "parent": u["parent"],
            "batch_no": _inherit(units, uid, "batch_no"),
            "material": _inherit(units, uid, "material"),
            "unit": _inherit(units, uid, "unit"),
            "manufactured_at": _inherit(units, uid, "manufactured_at"),
            "expires_at": _inherit(units, uid, "expires_at"),
            "qty": u["qty"],
            "allocated_qty": round(sum(c["consumed"] for c in u["cuts"]), 6),
            "scrapped_qty": round(sum(s["qty"] or 0.0 for s in u["scraps"]), 6),
            "placed_qty": round(u["placed_qty"], 6),
            "remaining_qty": round(_remaining(u), 6),
            "status": u["status"],
            "born_event": u["born_event"],
            "born_at": born.isoformat() if born else None,
            "inherited_out_time_h": round(inherited, 2),
            "out_time_h": round(_out_time_at(units, uid, latest), 2)
            if latest else 0.0,
            "out_time_limit_h": _inherit(units, uid, "out_time_limit_h"),
            "rebinds": [{**rb, "at": rb["at"].isoformat() if rb["at"] else None}
                        for rb in u["rebinds"]],
        }
    edges_out = [{
        "parent": u["parent"], "child": uid, "event": u["born_event"],
        "qty": u["qty"],
    } for uid, u in sorted(units.items()) if u["parent"]]
    usage_out = []
    for uid in sorted(usage):
        for r in usage[uid]:
            placed = parse_time(r.get("placed_at"))
            usage_out.append({
                "unit": uid, "job_id": r["job_id"], "ply_id": r["ply_id"],
                "event_seq": r.get("event_seq"), "placed_at": r.get("placed_at"),
                "out_time_at_placement_h":
                    round(_out_time_at(units, uid, placed), 2)
                    if placed is not None and uid in units else None,
            })

    # ---------------- 8. 违规整理（谱系闭包 → 受影响工单/层） ----------------
    parent_map = {uid: u["parent"] for uid, u in units.items()}
    V = []
    for p in raw:
        closure = _closure(parent_map, p["units"])
        jobs = {r["job_id"] for uid in closure for r in usage.get(uid, [])}
        jobs |= {r["job_id"] for r in p["refs"]}
        if focus_job is not None and focus_job not in jobs:
            continue
        plies = {r["ply_id"] for r in p["refs"]
                 if focus_job is None or r["job_id"] == focus_job}
        if not p["refs"]:
            plies |= {r["ply_id"] for uid in closure
                      for r in usage.get(uid, [])
                      if focus_job is None or r["job_id"] == focus_job}
        V.append({
            "rule": p["rule"], "message": p["message"],
            "plies": sorted(x for x in plies if x),
            "zones": [],
            "details": {**p["extra"], "units": p["units"],
                        "events": p["events"], "jobs": sorted(jobs),
                        "refs": p["refs"]},
        })

    # ---------------- 9. focus 裁剪谱系视图 ----------------
    if focus_job is not None:
        referenced = {uid for uid, refs in usage.items()
                      if any(r["job_id"] == focus_job for r in refs)}
        keep = _closure(parent_map, referenced)
        units_out = {k: v for k, v in units_out.items() if k in keep}
        edges_out = [e for e in edges_out if e["child"] in keep]
        usage_out = [r for r in usage_out if r["job_id"] == focus_job]

    return {"state": {"units": units_out, "edges": edges_out,
                      "usage": usage_out},
            "violations": V}
