"""局部缺陷处置：缺陷登记、局部揭除、补片铺放、复检与工程师签发。

背景：既有返工只支持整层揭除（ply_removed）与整层替换
（ply_replaced）。铺层中途发现的分层、夹杂、贫胶等局部缺陷需要落到
原铺层上的具体轮廓与层位：按缺陷类型/层位/分区规定的尺寸上限、
禁修区、补片材料、纤维方向、搭接宽度与逐层退让量（挖补台阶），
做局部揭除与逐层补片；工程师签发过的补片还必须与后续封闭层逐点
对应。本模块在同一条只增事件链上追加缺陷处置事件，按事件次序
重建缺陷案例（含因轮廓变化/源层返工/规范换版产生的代际），并把
缺陷、补片与开孔统一投影到模具坐标系逐层核查。

规范（spec.repairs，缺省即不启用，保持老工单兼容）：
  {
    "version": "RP-2026-A",          # 处置指令集版本（签发快照收录）
    "defaults": {"min_clearance_mm": 20.0, "min_patch_photo": 1},
    "types": {
      "delamination": {
        "max_size_mm": 80.0, "max_depth_plies": 3,
        "material": "match", "angle": "match",
        "lap_width_mm": 15.0, "stepback_mm": 10.0,
        "zones": {"Z1": {"max_size_mm": 60.0}},
        "plies": {"P01": {"max_depth_plies": 1}},
        "no_repair": false},
      "fod": {"no_repair": true}     # 该类型整体禁修
    },
    "no_repair_areas": [
      {"area_id": "NR1", "zone": "Z1", "polygon": [[..]], "types": ["fod"]}
    ],
    "openings": [
      {"opening_id": "O1", "polygon": [[..]]}   # 模具坐标开孔
    ]
  }
defaults/类型/分区/层位四级覆盖（层位仅收窄尺寸/深度类参数）；
material="match" / angle="match" 表示与被修原层一致。

现场追加事件（与铺放事件同一张 events 表，只允许 INSERT）：
  defect_found          发现 {defect_id, defect_type, ply_id, zone, polygon,
                               instruction?, photo_digest, photo_summary,
                               disposition, at}
  defect_isolated       隔离 {defect_id, at}
  defect_ply_removed    局部揭除 {defect_id, ply_id, polygon, reason?, at}
  patch_placed          补片铺放 {defect_id, ply_id, polygon, roll?/unit?,
                               material?, angle, face, seams?,
                               photo_digest?, placed_at}
  defect_reinspected    复检 {defect_id, result(=pass/fail), method?, at}
  defect_contour_updated 轮廓变化（只撤销该缺陷的旧处置，开启新一代）
                               {defect_id, polygon, reason, photo_digest?, at}
  repair_signed         工程师签发处置指令 {defect_id, generation,
                               decision(=repair/use_as_is/reject/confirmed),
                               instruction, reason, signed_by, at}

任一处置在其当前代未闭合（局部揭除未补、补片层序断裂、复检未通过、
未签发、指令版本缺失/过期、几何越界、牵涉已锁层等）时，放行校验
带出具体缺陷号、层号与分区，工单保持不可批准。
"""

from .core import angle_diff, parse_time
from .geometry import (coverage_fraction, point_in_polygon, polygon_area,
                       polygon_centroid)

DEFECT_EVENT_TYPES = {
    "defect_found", "defect_isolated", "defect_ply_removed",
    "patch_placed", "defect_reinspected", "defect_contour_updated",
    "repair_signed",
}

# 类型级允许的参数键（defaults 为其子集）
_TYPE_KEYS = ("max_size_mm", "max_depth_plies", "material", "angle",
              "lap_width_mm", "stepback_mm", "no_repair", "min_clearance_mm")
_DEFAULT_KEYS = ("min_clearance_mm", "min_patch_photo")
_NUM_KEYS = ("max_size_mm", "max_depth_plies", "lap_width_mm",
             "stepback_mm", "min_clearance_mm", "min_patch_photo")
_POSITIVE_KEYS = ("max_size_mm", "lap_width_mm", "stepback_mm",
                  "min_clearance_mm")
_DISPOSITIONS = ("repair", "use_as_is", "reject")
_SIGN_DECISIONS = ("repair", "use_as_is", "reject", "confirmed")
_SEAM_AXES = ("x", "y")


# ---------------------------------------------------------------- 工具

def _num(x):
    """有限数值；bool 不算。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x)


def _valid_polygon(poly):
    if not isinstance(poly, list) or len(poly) < 3:
        return False
    for pt in poly:
        if not (isinstance(pt, list) and len(pt) == 2):
            return False
        if _num(pt[0]) is None or _num(pt[1]) is None:
            return False
    return True


def polygon_bounds(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def characteristic_size(poly):
    """缺陷特征尺寸：外接矩形长边（mm）。"""
    minx, miny, maxx, maxy = polygon_bounds(poly)
    return max(maxx - minx, maxy - miny)


def polygons_overlap(a, b, samples=10):
    """两多边形是否相交（任一顶点在对方内部或采样网格命中）。"""
    for x, y in a:
        if point_in_polygon(x, y, b):
            return True
    for x, y in b:
        if point_in_polygon(x, y, a):
            return True
    minx, miny, maxx, maxy = polygon_bounds(a)
    w, h = maxx - minx, maxy - miny
    if w <= 0 or h <= 0:
        return False
    for i in range(samples):
        for j in range(samples):
            x = minx + (i + 0.5) * w / samples
            y = miny + (j + 0.5) * w / samples
            if point_in_polygon(x, y, a) and point_in_polygon(x, y, b):
                return True
    return False


def polygon_contains(outer, inner, samples=12):
    """outer 是否在采样意义上完全包含 inner（退化为质心/顶点判定）。"""
    for x, y in inner:
        if not point_in_polygon(x, y, outer):
            return False
    minx, miny, maxx, maxy = polygon_bounds(inner)
    w, h = maxx - minx, maxy - miny
    if w <= 0 or h <= 0:
        return True
    for i in range(samples):
        for j in range(samples):
            x = minx + (i + 0.5) * w / samples
            y = miny + (j + 0.5) * w / samples
            if point_in_polygon(x, y, inner) \
                    and not point_in_polygon(x, y, outer):
                return False
    return True


def min_gap(a, b):
    """两多边形边界最小间距（mm）；相交或接触返回 0。

    以双方顶点到对方多边形的最近距离近似（顶点采样），配合
    polygons_overlap 判定相交，足以核查净距下限。
    """
    if polygons_overlap(a, b):
        return 0.0
    best = None
    for x, y in a:
        d = _point_to_polygon_dist(x, y, b)
        best = d if best is None else min(best, d)
    for x, y in b:
        d = _point_to_polygon_dist(x, y, a)
        best = d if best is None else min(best, d)
    return best or 0.0


def _point_to_polygon_dist(x, y, poly):
    best = None
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        if L2 == 0:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L2))
        px, py = x1 + t * dx, y1 + t * dy
        d = ((x - px) ** 2 + (y - py) ** 2) ** 0.5
        best = d if best is None else min(best, d)
    return best


def offset_outward(poly, distance):
    """沿多边形每条边外法线取距 distance 的探针点（含角平分线方向）。"""
    pts = []
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        L = (dx * dx + dy * dy) ** 0.5 or 1.0
        nx, ny = -dy / L, dx / L
        mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        for sgn in (1, -1):  # 两侧都探：不自交时外侧命中、内侧不命中
            pts.append((mx + sgn * nx * distance, my + sgn * ny * distance))
    for i in range(n):  # 顶点角平分线方向
        x, y = poly[i]
        px, py = poly[(i - 1) % n]
        nx_, ny_ = poly[(i + 1) % n]
        v1x, v1y = px - x, py - y
        v2x, v2y = nx_ - x, ny_ - y
        l1 = (v1x * v1x + v1y * v1y) ** 0.5 or 1.0
        l2 = (v2x * v2x + v2y * v2y) ** 0.5 or 1.0
        bx, by = v1x / l1 + v2x / l2, v1y / l1 + v2y / l2
        bl = (bx * bx + by * by) ** 0.5 or 1.0
        bx, by = bx / bl, by / bl
        for sgn in (1, -1):
            pts.append((x + sgn * bx * distance, y + sgn * by * distance))
    return pts


def lap_width_around(inner, outer, required_mm, samples_per_edge=4):
    """外多边形相对内多边形的周向最小搭接（mm）。

    沿内多边形边界均匀取采样点，沿局部法线两侧探 required_mm：朝腔外
    的一侧很快离开 outer（即搭接量），朝腔内一侧要穿过整个腔体才离开，
    故取两侧可达距离的较小者作为周向最小搭接。调用方须先确认 inner 被
    outer 包含。
    """
    min_hit = None
    n = len(inner)
    for i in range(n):
        x1, y1 = inner[i]
        x2, y2 = inner[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        L = (dx * dx + dy * dy) ** 0.5 or 1.0
        nx, ny = -dy / L, dx / L
        for k in range(samples_per_edge):
            f = (k + 0.5) / samples_per_edge
            bx, by = x1 + f * dx, y1 + f * dy
            reaches = []
            for sgn in (1, -1):
                step = max(required_mm / 8.0, 0.5)
                d = 0.0
                while d < required_mm + 1e-9:
                    d += step
                    if not point_in_polygon(bx + sgn * nx * d,
                                            by + sgn * ny * d, outer):
                        break
                reaches.append(d - step)
            min_hit = min(reaches) if min_hit is None \
                else min(min_hit, min(reaches))
    return min_hit or 0.0


# ---------------------------------------------------------------- 规范解析

def normalize_repairs(spec, zones, materials, spec_ply_ids):
    """解析 spec.repairs，返回 (config, issues)。

    config: {"active", "version", "defaults", "types", "no_repair_areas",
             "openings"}。非法条目以 REPAIR_SPEC_INVALID 报告且剔除。
    """
    issues = []
    raw = (spec or {}).get("repairs")
    empty = {"active": False, "version": None,
             "defaults": {k: None for k in _DEFAULT_KEYS},
             "types": {}, "no_repair_areas": [], "openings": []}
    if not raw:
        return empty, issues
    if not isinstance(raw, dict):
        return empty, [{"code": "REPAIR_SPEC_INVALID",
                        "message": "spec.repairs 必须是对象", "detail": {}}]

    version = raw.get("version")
    if version is not None and not isinstance(version, str):
        issues.append({"code": "REPAIR_SPEC_INVALID",
                       "message": "repairs.version 必须是字符串",
                       "detail": {"version": version}})
        version = None

    defaults = {}
    raw_defaults = raw.get("defaults") or {}
    if not isinstance(raw_defaults, dict):
        issues.append({"code": "REPAIR_SPEC_INVALID",
                       "message": "repairs.defaults 必须是对象"})
        raw_defaults = {}
    for key, val in raw_defaults.items():
        if key not in _DEFAULT_KEYS:
            issues.append({"code": "REPAIR_SPEC_INVALID",
                           "message": f"repairs.defaults 含不支持的键 {key}",
                           "detail": {"key": key}})
            continue
        n = _num(val)
        if n is None or (key in _POSITIVE_KEYS and n <= 0):
            issues.append({"code": "REPAIR_SPEC_INVALID",
                           "message": f"repairs.defaults.{key} 必须是正数",
                           "detail": {"key": key, "value": val}})
            continue
        defaults[key] = n

    def parse_block(block, where):
        out = {}
        for key, val in block.items():
            if key not in _TYPE_KEYS:
                issues.append({"code": "REPAIR_SPEC_INVALID",
                               "message": f"{where} 含不支持的键 {key}",
                               "detail": {"key": key}})
                continue
            if key in ("material", "angle", "no_repair"):
                if key == "no_repair":
                    if not isinstance(val, bool):
                        issues.append({"code": "REPAIR_SPEC_INVALID",
                                       "message": f"{where}.no_repair 必须是布尔",
                                       "detail": {"value": val}})
                        continue
                elif key == "material":
                    if not isinstance(val, str) or not val:
                        issues.append({"code": "REPAIR_SPEC_INVALID",
                                       "message": f"{where}.material 必须是非空字符串",
                                       "detail": {"value": val}})
                        continue
                    if val != "match" and val not in materials:
                        issues.append({"code": "REPAIR_SPEC_INVALID",
                                       "message": f"{where}.material 引用未定义材料 {val}",
                                       "detail": {"material": val}})
                        continue
                elif not (val == "match" or _num(val) is not None):
                    issues.append({"code": "REPAIR_SPEC_INVALID",
                                   "message": f"{where}.angle 必须是数值或 'match'",
                                   "detail": {"value": val}})
                    continue
                out[key] = val
                continue
            n = _num(val)
            if n is None or (key in _POSITIVE_KEYS and n <= 0) \
                    or (key == "max_depth_plies"
                        and (n < 1 or int(n) != n)):
                issues.append({"code": "REPAIR_SPEC_INVALID",
                               "message": f"{where}.{key} 必须是正数值"
                                          f"（max_depth_plies 为正整数）",
                               "detail": {"key": key, "value": val}})
                continue
            out[key] = int(n) if key == "max_depth_plies" else n
        return out

    types = {}
    raw_types = raw.get("types")
    if raw_types is not None:
        if not isinstance(raw_types, dict):
            issues.append({"code": "REPAIR_SPEC_INVALID",
                           "message": "repairs.types 必须是对象"})
        else:
            for dtype, block in raw_types.items():
                if not isinstance(block, dict):
                    issues.append({"code": "REPAIR_SPEC_INVALID",
                                   "message": f"repairs.types.{dtype} 必须是对象"})
                    continue
                # zones/plies 为分层覆盖块，不进类型基础参数
                entry = parse_block(
                    {k: v for k, v in block.items()
                     if k not in ("zones", "plies")},
                    f"repairs.types.{dtype}")
                zones_ov = {}
                for zid, zb in (block.get("zones") or {}).items():
                    if zid not in zones:
                        issues.append({"code": "REPAIR_SPEC_INVALID",
                                       "message": f"repairs.types.{dtype}.zones 引用"
                                                  f"未定义分区 {zid}",
                                       "detail": {"zone": zid}})
                        continue
                    if not isinstance(zb, dict):
                        issues.append({"code": "REPAIR_SPEC_INVALID",
                                       "message": f"repairs.types.{dtype}.zones."
                                                  f"{zid} 必须是对象"})
                        continue
                    zones_ov[zid] = parse_block(
                        zb, f"repairs.types.{dtype}.zones.{zid}")
                plies_ov = {}
                for pid, pb in (block.get("plies") or {}).items():
                    if pid not in spec_ply_ids:
                        issues.append({"code": "REPAIR_SPEC_INVALID",
                                       "message": f"repairs.types.{dtype}.plies 引用"
                                                  f"规范外铺层 {pid}",
                                       "detail": {"ply_id": pid}})
                        continue
                    if not isinstance(pb, dict):
                        issues.append({"code": "REPAIR_SPEC_INVALID",
                                       "message": f"repairs.types.{dtype}.plies."
                                                  f"{pid} 必须是对象"})
                        continue
                    # 层位只允许收窄尺寸/深度/禁修，不改搭接/补片材料
                    narrow = {k: v for k, v in pb.items()
                              if k in ("max_size_mm", "max_depth_plies",
                                       "no_repair", "min_clearance_mm")}
                    bad = sorted(set(pb) - set(narrow))
                    if bad:
                        issues.append({"code": "REPAIR_SPEC_INVALID",
                                       "message": f"repairs.types.{dtype}.plies."
                                                  f"{pid} 层位只允许覆盖 "
                                                  f"max_size_mm/max_depth_plies/"
                                                  f"no_repair/min_clearance_mm；"
                                                  f"非法键 {bad}",
                                       "detail": {"keys": bad}})
                    plies_ov[pid] = parse_block(
                        narrow, f"repairs.types.{dtype}.plies.{pid}")
                types[dtype] = {"base": entry, "zones": zones_ov,
                                "plies": plies_ov}

    no_repair_areas = []
    seen_area = set()
    for idx, area in enumerate(raw.get("no_repair_areas") or []):
        if not isinstance(area, dict):
            issues.append({"code": "REPAIR_SPEC_INVALID",
                           "message": f"禁修区第 {idx + 1} 项必须是对象"})
            continue
        aid = area.get("area_id") or f"NR{idx + 1}"
        if aid in seen_area:
            issues.append({"code": "REPAIR_SPEC_INVALID",
                           "message": f"禁修区 {aid} 重复定义",
                           "detail": {"area_id": aid}})
            continue
        poly = area.get("polygon")
        zid = area.get("zone")
        if zid is not None and zid not in zones:
            issues.append({"code": "REPAIR_SPEC_INVALID",
                           "message": f"禁修区 {aid} 引用未定义分区 {zid}",
                           "detail": {"area_id": aid, "zone": zid}})
            continue
        if not _valid_polygon(poly):
            issues.append({"code": "REPAIR_SPEC_INVALID",
                           "message": f"禁修区 {aid} 的 polygon 必须是 ≥3 个点",
                           "detail": {"area_id": aid}})
            continue
        atypes = area.get("types")
        if atypes is not None and (
                not isinstance(atypes, list)
                or not all(isinstance(t, str) for t in atypes)):
            issues.append({"code": "REPAIR_SPEC_INVALID",
                           "message": f"禁修区 {aid} 的 types 必须是字符串列表",
                           "detail": {"area_id": aid}})
            continue
        seen_area.add(aid)
        no_repair_areas.append({"area_id": aid, "zone": zid, "polygon": poly,
                                "types": atypes})

    openings = []
    seen_op = set()
    for idx, op in enumerate(raw.get("openings") or []):
        if not isinstance(op, dict):
            issues.append({"code": "REPAIR_SPEC_INVALID",
                           "message": f"开孔第 {idx + 1} 项必须是对象"})
            continue
        oid = op.get("opening_id") or f"O{idx + 1}"
        if oid in seen_op:
            issues.append({"code": "REPAIR_SPEC_INVALID",
                           "message": f"开孔 {oid} 重复定义",
                           "detail": {"opening_id": oid}})
            continue
        if not _valid_polygon(op.get("polygon")):
            issues.append({"code": "REPAIR_SPEC_INVALID",
                           "message": f"开孔 {oid} 的 polygon 必须是 ≥3 个点",
                           "detail": {"opening_id": oid}})
            continue
        seen_op.add(oid)
        openings.append({"opening_id": oid, "polygon": op["polygon"]})

    return {"active": True, "version": version, "defaults": defaults,
            "types": types, "no_repair_areas": no_repair_areas,
            "openings": openings}, issues


def resolve_instruction(config, dtype, zid, pid):
    """defaults → 类型 → 分区 → 层位 四级覆盖，返回生效指令字典。"""
    out = dict(config["defaults"])
    spec = config["types"].get(dtype)
    if spec:
        out.update(spec.get("base") or {})
        out.update((spec.get("zones") or {}).get(zid) or {})
        out.update((spec.get("plies") or {}).get(pid) or {})
    return out


# ---------------------------------------------------------------- 入链校验

def validate_event_item(item):
    """缺陷处置事件入链前最小载荷校验；返回错误消息字符串或 None。"""
    t = item.get("type")
    if t not in DEFECT_EVENT_TYPES:
        return None
    if t == "defect_found":
        for k in ("defect_id", "defect_type", "ply_id", "zone", "polygon",
                  "photo_summary", "disposition", "at"):
            if item.get(k) is None:
                return f"defect_found 需要 {k}"
        if not _valid_polygon(item.get("polygon")):
            return "defect_found 的 polygon 必须是 ≥3 个 [x,y] 点"
        if item.get("disposition") not in _DISPOSITIONS:
            return (f"defect_found 的 disposition 必须是 {list(_DISPOSITIONS)}"
                    f" 之一")
        if not str(item.get("photo_summary") or "").strip():
            return "defect_found 需要 photo_summary（照片摘要）"
        if parse_time(item.get("at")) is None:
            return f"defect_found 的 at={item.get('at')!r} 无法解析"
        if item.get("instruction") is not None \
                and not isinstance(item["instruction"], str):
            return "defect_found 的 instruction 必须是版本字符串"
    elif t == "defect_isolated":
        if not item.get("defect_id") or not item.get("at"):
            return "defect_isolated 需要 defect_id 与 at"
        if parse_time(item.get("at")) is None:
            return f"defect_isolated 的 at={item.get('at')!r} 无法解析"
    elif t == "defect_ply_removed":
        for k in ("defect_id", "ply_id", "polygon", "at"):
            if item.get(k) is None:
                return f"defect_ply_removed 需要 {k}"
        if not _valid_polygon(item.get("polygon")):
            return "defect_ply_removed 的 polygon 必须是 ≥3 个 [x,y] 点"
        if parse_time(item.get("at")) is None:
            return f"defect_ply_removed 的 at={item.get('at')!r} 无法解析"
    elif t == "patch_placed":
        for k in ("defect_id", "ply_id", "polygon", "angle", "face",
                  "placed_at"):
            if item.get(k) is None:
                return f"patch_placed 需要 {k}"
        if not _valid_polygon(item.get("polygon")):
            return "patch_placed 的 polygon 必须是 ≥3 个 [x,y] 点"
        if _num(item.get("angle")) is None:
            return "patch_placed 的 angle 必须是数值"
        if not (item.get("roll") or item.get("unit")
                or item.get("material")):
            return "patch_placed 需要 roll / unit / material 至少一项材料引用"
        if parse_time(item.get("placed_at")) is None:
            return (f"patch_placed 的 placed_at={item.get('placed_at')!r}"
                    f" 无法解析")
    elif t == "defect_reinspected":
        if not item.get("defect_id") or not item.get("at"):
            return "defect_reinspected 需要 defect_id 与 at"
        if item.get("result") not in ("pass", "fail"):
            return "defect_reinspected 的 result 必须是 pass/fail"
        if parse_time(item.get("at")) is None:
            return f"defect_reinspected 的 at={item.get('at')!r} 无法解析"
    elif t == "defect_contour_updated":
        for k in ("defect_id", "polygon", "reason", "at"):
            if item.get(k) is None:
                return f"defect_contour_updated 需要 {k}"
        if not _valid_polygon(item.get("polygon")):
            return "defect_contour_updated 的 polygon 必须是 ≥3 个 [x,y] 点"
        if parse_time(item.get("at")) is None:
            return (f"defect_contour_updated 的 at={item.get('at')!r}"
                    f" 无法解析")
    elif t == "repair_signed":
        for k in ("defect_id", "generation", "decision", "instruction",
                  "reason", "signed_by", "at"):
            if item.get(k) is None:
                return f"repair_signed 需要 {k}"
        if item.get("decision") not in _SIGN_DECISIONS:
            return (f"repair_signed 的 decision 必须是 {list(_SIGN_DECISIONS)}"
                    f" 之一")
        if not isinstance(item.get("generation"), int) \
                or isinstance(item.get("generation"), bool) \
                or item["generation"] < 0:
            return "repair_signed 的 generation 必须是非负整数"
        if parse_time(item.get("at")) is None:
            return f"repair_signed 的 at={item.get('at')!r} 无法解析"
    return None


# ---------------------------------------------------------------- 案例重建

def rebuild_cases(events):
    """按事件次序重建缺陷案例（含代际）。

    返回 {defect_id: case}：
      case = {"found": 事件, "generations": {g: {
                "contour": 多边形, "opened_seq", "events": [...],
                "isolated": 事件|None, "removals": {ply_id: 事件},
                "patches": {ply_id: 事件}, "reinspections": [事件],
                "sign": 事件|None, "contour_events": [更新事件]}},
              "gen_order": [..], "current_gen": int}
    """
    cases = {}
    for ev in events:
        if ev["type"] not in DEFECT_EVENT_TYPES:
            continue
        p = ev["payload"]
        did = p.get("defect_id")
        if did is None:
            continue
        case = cases.setdefault(did, {"found": None, "generations": {},
                                      "gen_order": [], "current_gen": 0})
        if ev["type"] == "defect_found":
            if case["found"] is not None:
                case.setdefault("duplicates", []).append(ev["seq"])
                continue
            case["found"] = ev
            g = case["generations"][0] = _new_gen(ev["seq"], p["polygon"])
            case["gen_order"] = [0]
            case["current_gen"] = 0
            continue
        if case["found"] is None:
            case.setdefault("orphans", []).append(ev)
            continue
        if ev["type"] == "defect_contour_updated":
            gno = case["current_gen"] + 1
            case["generations"][gno] = _new_gen(ev["seq"], p["polygon"])
            case["gen_order"].append(gno)
            case["current_gen"] = gno
        g = case["generations"][case["current_gen"]]
        g["events"].append(ev)
        if ev["type"] == "defect_isolated":
            if g["isolated"] is None:
                g["isolated"] = ev
        elif ev["type"] == "defect_ply_removed":
            g["removals"][p["ply_id"]] = ev
        elif ev["type"] == "patch_placed":
            g["patches"][p["ply_id"]] = ev
        elif ev["type"] == "defect_reinspected":
            g["reinspections"].append(ev)
        elif ev["type"] == "repair_signed":
            g["sign"] = ev
    return cases


def _new_gen(opened_seq, contour):
    return {"contour": contour, "opened_seq": opened_seq, "events": [],
            "isolated": None, "removals": {}, "patches": {},
            "reinspections": [], "sign": None, "contour_events": []}


# ---------------------------------------------------------------- 主评估

def evaluate_defects(job, spec, zones, spec_plies, materials, stack,
                     events, rolls, genealogy=None,
                     confirmed_revisions=(), locked_ply_ids=()):
    """重建缺陷处置案例并执行全部逐层核查。返回 (state, violations)。

    confirmed_revisions  已确认换版记录 [{"revision", "confirmed_at",
                         "spec", "base_revision"}]（按 revision 升序），
                         用于判定签发后相关指令是否换版；
    locked_ply_ids       最近批准快照冻结的实铺层号集合；快照后才出现
                         的铺层不在其中，局部揭除牵涉已锁层即不可批准。
    """
    violations = []

    def v(rule, message, defects=None, plies=None, zids=None, **details):
        violations.append({
            "rule": rule, "message": message,
            "plies": [p for p in (plies or []) if p is not None],
            "zones": zids or [],
            "details": {"defects": [d for d in (defects or []) if d],
                        **details}})

    config, spec_issues = normalize_repairs(
        spec, {z: zz for z, zz in zones.items()}, materials,
        {sp.get("ply_id") for sp in spec_plies})
    for iss in spec_issues:
        d = iss.get("detail") or {}
        v(iss["code"], iss["message"], zids=[d["zone"]] if d.get("zone") else None,
          **{k: x for k, x in d.items() if k != "zone"})

    defect_events = [e for e in events if e["type"] in DEFECT_EVENT_TYPES]
    if defect_events and not config["active"]:
        v("REPAIR_SPEC_MISSING",
          "现场提交了缺陷处置事件，但规范未冻结 repairs 处置指令块，"
          "尺寸上限/补片材料/搭接退让等核查无法执行",
          defects=sorted({e["payload"].get("defect_id")
                          for e in defect_events
                          if e["payload"].get("defect_id")}),
          events=sorted(e["seq"] for e in defect_events))

    spec_by_id = {sp.get("ply_id"): sp for sp in spec_plies}
    rank = {sp.get("ply_id"): sp.get("seq") for sp in spec_plies}
    active = [e for e in stack if e["active"]]
    active_latest = {}
    for e in active:
        active_latest[e["ply_id"]] = e
    # 源层返工（整层揭除+替代）：替代层归位事件 seq
    replacement_seq = {}
    for e in stack:
        if e.get("replaced_by") is not None:
            rid = e["ply_id"]
            repl = next((x for x in stack
                         if x.get("rework_of") == rid and x["active"]), None)
            if repl is not None:
                replacement_seq[rid] = repl["event_seq"]

    unit_material = {}
    for uid, u in ((genealogy or {}).get("state") or {}).get("units", {}).items():
        if u.get("material"):
            unit_material[uid] = u["material"]
    for rid, r in (rolls or {}).items():
        if r.get("material"):
            unit_material[rid] = r["material"]

    def patch_material_of(p):
        subj = p.get("roll") or p.get("unit")
        if subj and unit_material.get(subj):
            return unit_material[subj]
        return p.get("material")

    angle_tol = float(((spec.get("rules") or {}).get("angle_tolerance_deg"))
                      or 3.0)
    seam_min_stagger = (spec.get("rules") or {}).get("seam_min_stagger_mm")
    seam_max_gap = (spec.get("rules") or {}).get("seam_max_gap_mm")
    seam_min_stagger = _num(seam_min_stagger)
    seam_max_gap = _num(seam_max_gap)
    current_version = config.get("version")
    cases = rebuild_cases(events)

    cases_out = []

    for did in sorted(cases):
        case = cases[did]
        found = case["found"]
        if found is None:
            v("DEFECT_ORPHAN_EVENT",
              f"缺陷 {did} 的处置事件缺少 defect_found 登记",
              defects=[did], events=case.get("orphans") or [])
            continue
        fp = found["payload"]
        dtype, pid, zid = fp.get("defect_type"), fp.get("ply_id"), \
            fp.get("zone")
        contour0 = fp.get("polygon")
        for seq in case.get("duplicates") or []:
            v("DEFECT_DUPLICATE", f"缺陷 {did} 被重复登记（事件 #{seq}）",
              defects=[did], plies=[pid], zids=[zid], event_seq=seq)
        if zid not in zones:
            v("ZONE_UNKNOWN", f"缺陷 {did} 引用未定义分区 {zid}",
              defects=[did], plies=[pid], zids=[zid])
        if pid not in spec_by_id:
            v("DEFECT_PLY_UNKNOWN",
              f"缺陷 {did} 落在规范外铺层 {pid} 上，层位无法核对",
              defects=[did], plies=[pid], zids=[zid])

        # ---- 指令版本（发现时携带，缺失即不可批准）----
        instr_version = fp.get("instruction")
        if not instr_version:
            v("DEFECT_INSTRUCTION_MISSING",
              f"缺陷 {did}（{dtype}，层 {pid}，分区 {zid}）发现时未携带"
              f"处置指令版本，处置无依据",
              defects=[did], plies=[pid], zids=[zid])
        elif config["active"] and current_version \
                and instr_version != current_version:
            v("REPAIR_INSTRUCTION_VERSION",
              f"缺陷 {did} 引用处置指令 {instr_version}，与现行指令集 "
              f"{current_version} 不一致，需按新版重新签发",
              defects=[did], plies=[pid], zids=[zid],
              instruction=instr_version, current=current_version)

        # ---- 缺陷几何：分区/原层内、禁修区、尺寸上限 ----
        if _valid_polygon(contour0):
            if zid in zones:
                zp = zones[zid]["polygon"]
                if not polygon_contains(zp, contour0):
                    v("DEFECT_GEOMETRY_OUT_OF_BOUNDS",
                      f"缺陷 {did} 轮廓越出分区 {zid} 边界",
                      defects=[did], plies=[pid], zids=[zid])
            src = active_latest.get(pid)
            src_geom = src and src["payload"].get("geometry")
            if src_geom and not polygons_overlap(src_geom, contour0):
                v("DEFECT_GEOMETRY_OUT_OF_BOUNDS",
                  f"缺陷 {did} 轮廓落在原铺层 {pid} 实铺几何之外",
                  defects=[did], plies=[pid], zids=[zid])
            for area in config["no_repair_areas"]:
                if area["zone"] and area["zone"] != zid:
                    continue
                if area["types"] and dtype not in area["types"]:
                    continue
                if polygons_overlap(area["polygon"], contour0):
                    v("DEFECT_NO_REPAIR_ZONE",
                      f"缺陷 {did}（{dtype}）位于禁修区 {area['area_id']}，"
                      f"层 {pid}、分区 {zid} 不得修补",
                      defects=[did], plies=[pid],
                      zids=[zid] if zid else [],
                      area_id=area["area_id"])
            instr = resolve_instruction(config, dtype, zid, pid) \
                if config["active"] else {}
            if instr.get("no_repair"):
                v("DEFECT_NO_REPAIR_ZONE",
                  f"缺陷类型 {dtype} 在层 {pid}、分区 {zid} 属整体禁修",
                  defects=[did], plies=[pid], zids=[zid])
            size = characteristic_size(contour0)
            limit = instr.get("max_size_mm")
            if limit is not None and size > limit:
                v("DEFECT_SIZE_EXCEEDED",
                  f"缺陷 {did} 特征尺寸 {size:.1f}mm 超过 {dtype} 在层 "
                  f"{pid}、分区 {zid} 的上限 {limit:g}mm",
                  defects=[did], plies=[pid], zids=[zid],
                  size_mm=round(size, 2), limit_mm=limit)

        # ---- 逐代核查（历史代只入状态；闭合性只核当前代）----
        gens_out = []
        for gno in case["gen_order"]:
            g = case["generations"][gno]
            gout = _generation_state(did, gno, g, found)
            gens_out.append(gout)
            if gno != case["current_gen"]:
                continue
            _check_current_generation(
                v, did, found, g, config, instr, dtype, pid, zid,
                contour0, spec_by_id, rank, active_latest, stack,
                replacement_seq, zones, materials, rolls, patch_material_of,
                angle_tol, seam_min_stagger, seam_max_gap,
                current_version, confirmed_revisions, locked_ply_ids)

        cases_out.append({
            "defect_id": did, "defect_type": dtype, "type": dtype,
            "source_ply": pid, "zone": zid,
            "found_event": found["seq"], "found_at": fp.get("at"),
            "instruction": instr_version, "disposition": fp.get("disposition"),
            "photo_digest": fp.get("photo_digest"),
            "photo_summary": fp.get("photo_summary"),
            "contour": contour0,
            "current_generation": case["current_gen"],
            "generations": gens_out,
            "status": _case_status(case, config, instr_version,
                                   current_version, replacement_seq,
                                   confirmed_revisions),
        })

    state = {"enabled": config["active"],
             "instruction_version": config.get("version"),
             "openings": config["openings"],
             "defects": cases_out}
    return state, violations


def _generation_state(did, gno, g, found):
    def ev_brief(ev):
        return {"event_seq": ev["seq"], "at": ev["payload"].get("at")
                or ev["payload"].get("placed_at"),
                "operator": ev.get("operator")}

    return {
        "generation": gno, "opened_event": g["opened_seq"],
        "contour": g["contour"],
        "isolated": ev_brief(g["isolated"]) if g["isolated"] else None,
        "removals": [{"ply_id": pid_, **ev_brief(ev),
                      "polygon": ev["payload"].get("polygon")}
                     for pid_, ev in sorted(g["removals"].items())],
        "patches": [{"ply_id": pid_, **ev_brief(ev),
                     "polygon": ev["payload"].get("polygon"),
                     "angle": ev["payload"].get("angle"),
                     "material": ev["payload"].get("material"),
                     "roll": ev["payload"].get("roll"),
                     "unit": ev["payload"].get("unit")}
                    for pid_, ev in sorted(g["patches"].items())],
        "reinspections": [{"event_seq": ev["seq"],
                           "result": ev["payload"].get("result"),
                           "at": ev["payload"].get("at"),
                           "method": ev["payload"].get("method")}
                          for ev in g["reinspections"]],
        "sign": ({"event_seq": g["sign"]["seq"],
                  "decision": g["sign"]["payload"].get("decision"),
                  "instruction": g["sign"]["payload"].get("instruction"),
                  "signed_by": g["sign"]["payload"].get("signed_by"),
                  "reason": g["sign"]["payload"].get("reason"),
                  "at": g["sign"]["payload"].get("at")}
                 if g["sign"] else None),
        "events": [ev["seq"] for ev in g["events"]],
    }


def _check_current_generation(v, did, found, g, config, instr, dtype,
                              source_pid, zid, contour, spec_by_id, rank,
                              active_latest, stack, replacement_seq, zones,
                              materials, rolls, patch_material_of, angle_tol,
                              seam_min_stagger, seam_max_gap,
                              current_version, confirmed_revisions,
                              locked_ply_ids):
    """当前代的全部闭合性与逐层几何核查。"""
    fp = found["payload"]
    disposition = fp.get("disposition")
    removals, patches = g["removals"], g["patches"]
    remov_seqs = [ev["seq"] for ev in removals.values()]
    patch_seqs = [ev["seq"] for ev in patches.values()]

    # ---- 受影响层位：显式 affected_plies（须连续、不浅于源层）----
    explicit = fp.get("affected_plies")
    affected = []
    if explicit is not None:
        if not isinstance(explicit, list) or not explicit \
                or not all(isinstance(x, str) for x in explicit):
            v("DEFECT_DATA_MISSING",
              f"缺陷 {did} 的 affected_plies 必须是非空字符串列表",
              defects=[did], plies=[source_pid], zids=[zid])
        else:
            rsrc = rank.get(source_pid)
            bad = [p for p in explicit if rank.get(p) is None]
            if bad:
                v("DEFECT_PLY_UNKNOWN",
                  f"缺陷 {did} 的 affected_plies 引用规范外铺层 {bad}",
                  defects=[did], plies=bad, zids=[zid])
            too_shallow = [p for p in explicit
                           if rsrc is not None and (rank.get(p) or 0) > rsrc]
            if too_shallow:
                v("PATCH_SEQUENCE_BROKEN",
                  f"缺陷 {did} 的受影响层 {too_shallow} 浅于源层 "
                  f"{source_pid}，补片不得越过缺陷层位向上扩展",
                  defects=[did], plies=too_shallow, zids=[zid])
            valid = [p for p in explicit if rank.get(p) is not None]
            if rsrc is not None and valid:
                seq_set = {rank[p] for p in valid}
                lo, hi = min(seq_set), max(seq_set)
                if seq_set != set(range(lo, hi + 1)) \
                        or source_pid not in valid:
                    v("PATCH_SEQUENCE_BROKEN",
                      f"缺陷 {did} 的受影响层必须自源层 {source_pid} 起"
                      f"在规范层序上连续",
                      defects=[did], plies=valid, zids=[zid])
            affected = [p for p in sorted(valid, key=lambda x: -(rank.get(x) or 0))]
    if not affected:
        # 由局部揭除记录推导（自上而下），默认仅源层
        affected = sorted(removals, key=lambda x: -(rank.get(x) or 0)) \
            or [source_pid]

    depth = len(affected)
    deepest = affected[-1]
    shallowest = affected[0]
    max_depth = instr.get("max_depth_plies")
    if max_depth is not None and depth > max_depth:
        v("DEFECT_DEPTH_EXCEEDED",
          f"缺陷 {did} 局部揭除深达 {depth} 层（{deepest}..{shallowest}），"
          f"超过 {dtype} 在层 {source_pid}、分区 {zid} 的深度上限 "
          f"{max_depth} 层",
          defects=[did], plies=affected, zids=[zid],
          depth=depth, limit_plies=max_depth)

    # ---- 牵涉已锁层：被局部揭除的层在最近批准快照中已冻结 ----
    locked_set = set(locked_ply_ids or ())
    if locked_set:
        locked_hit = sorted(pid_ for pid_ in removals if pid_ in locked_set)
        if locked_hit:
            v("DEFECT_LOCKED_PLY",
              f"缺陷 {did} 的局部揭除牵涉已批准锁定的铺层 {locked_hit}"
              f"（分区 {zid}），须先经换版/特许流程",
              defects=[did], plies=locked_hit, zids=[zid])

    if disposition != "repair":
        # 让步接收/报废：不应出现修补作业；只需签发闭环
        stray = sorted(set(removals) | set(patches))
        if stray:
            v("DEFECT_STAGE_ORDER",
              f"缺陷 {did} 处置为 {disposition}，但存在局部揭除/补片记录 "
              f"{stray}，处置方式与作业记录矛盾",
              defects=[did], plies=stray, zids=[zid])
        _check_signoff(v, did, found, g, disposition, source_pid, zid,
                       None, current_version, confirmed_revisions,
                       replacement_seq)
        return

    # ---- 工艺次序：先隔离、再自上而下局部揭除 ----
    if g["isolated"] is None:
        v("DEFECT_STAGE_ORDER",
          f"缺陷 {did} 已进入修补但缺少隔离记录（defect_isolated）",
          defects=[did], plies=affected, zids=[zid])
    else:
        first_action = min(remov_seqs + patch_seqs, default=None)
        if first_action is not None and g["isolated"]["seq"] > first_action:
            v("DEFECT_STAGE_ORDER",
              f"缺陷 {did} 在隔离（事件 #{g['isolated']['seq']}）之前已开始"
              f"局部揭除/补片（事件 #{first_action}）",
              defects=[did], plies=affected, zids=[zid])

    missing_removal = [p for p in affected if p not in removals]
    extra_removal = sorted(set(removals) - set(affected),
                           key=lambda x: -(rank.get(x) or 0))
    if extra_removal:
        v("PATCH_SEQUENCE_BROKEN",
          f"缺陷 {did} 的局部揭除层 {extra_removal} 不在受影响层位内",
          defects=[did], plies=extra_removal, zids=[zid])
    if missing_removal:
        v("PATCH_OPEN",
          f"缺陷 {did} 的受影响层 {missing_removal} 尚未局部揭除，"
          f"修补序列未闭合",
          defects=[did], plies=missing_removal, zids=[zid])

    # 揭除次序：自上而下（seq 递增方向与层序降序一致）
    order = [p for p in affected if p in removals]
    prev_seq = None
    for p in order:
        s = removals[p]["seq"]
        if prev_seq is not None and s <= prev_seq:
            v("PATCH_SEQUENCE_BROKEN",
              f"缺陷 {did} 局部揭除次序错误：{p}（事件 #{s}）未按自上而下"
              f"顺序进行",
              defects=[did], plies=order, zids=[zid])
            break
        prev_seq = s

    # ---- 逐层揭除退让（每层揭除轮廓比下一层外扩 stepback）----
    stepback = instr.get("stepback_mm")
    if stepback is not None:
        up = [p for p in affected if p in removals]  # 自上而下
        for i in range(len(up) - 1):
            upper, lower = up[i], up[i + 1]
            pu = removals[upper]["payload"]["polygon"]
            pl = removals[lower]["payload"]["polygon"]
            if not polygons_overlap(pu, pl):
                v("PATCH_STEPBACK",
                  f"缺陷 {did} 层 {upper} 的揭除轮廓与下层 {lower} 不相交，"
                  f"挖补台阶断裂",
                  defects=[did], plies=[upper, lower], zids=[zid])
                continue
            # 下层轮廓应被上层包含（上层揭得更大形成台阶）
            if not polygon_contains(pu, pl):
                v("PATCH_STEPBACK",
                  f"缺陷 {did} 层 {upper} 的揭除轮廓未完全覆盖下层 {lower}，"
                  f"逐层退让方向错误",
                  defects=[did], plies=[upper, lower], zids=[zid])
                continue
            reach = lap_width_around(pl, pu, stepback)
            if reach + 1e-6 < stepback:
                v("PATCH_STEPBACK",
                  f"缺陷 {did} 层 {upper} 相对 {lower} 的揭除退让仅 "
                  f"{reach:.1f}mm，小于逐层退让量 {stepback:g}mm",
                  defects=[did], plies=[upper, lower], zids=[zid],
                  stepback_mm=round(reach, 2), required_mm=stepback)

    # ---- 补片层序：每个受影响层一张，自下而上铺放 ----
    missing_patch = [p for p in affected if p not in patches]
    extra_patch = sorted(set(patches) - set(affected),
                         key=lambda x: -(rank.get(x) or 0))
    if extra_patch:
        v("PATCH_SEQUENCE_BROKEN",
          f"缺陷 {did} 的补片 {extra_patch} 不在受影响层位内",
          defects=[did], plies=extra_patch, zids=[zid])
    if missing_patch:
        v("PATCH_SEQUENCE_BROKEN",
          f"缺陷 {did} 的补片层序断裂：{missing_patch} 尚未铺放补片"
          f"（工程师已签补片无法与后续封闭层对应）",
          defects=[did], plies=missing_patch, zids=[zid])

    up_patch = [p for p in affected if p in patches]
    prev_seq = None
    for p in reversed(up_patch):  # 自下而上 → 事件序递增
        s = patches[p]["seq"]
        if prev_seq is not None and s <= prev_seq:
            v("PATCH_SEQUENCE_BROKEN",
              f"缺陷 {did} 补片铺放次序错误：{p}（事件 #{s}）未按自下而上"
              f"顺序铺放",
              defects=[did], plies=up_patch, zids=[zid])
            break
        prev_seq = s
    for pid_, ev in removals.items():
        if pid_ in patches and patches[pid_]["seq"] <= ev["seq"]:
            v("PATCH_SEQUENCE_BROKEN",
              f"缺陷 {did} 层 {pid_} 的补片（事件 #{patches[pid_]['seq']}）"
              f"铺放在该层局部揭除（事件 #{ev['seq']}）之前",
              defects=[did], plies=[pid_], zids=[zid])

    # ---- 逐层补片几何：覆盖揭除腔、周向搭接、退让复刻 ----
    lap = instr.get("lap_width_mm")
    for p in up_patch:
        pp = patches[p]["payload"]
        patch_poly = pp.get("polygon")
        removal = removals.get(p)
        if removal is not None and _valid_polygon(patch_poly):
            cavity = removal["payload"]["polygon"]
            if not polygon_contains(patch_poly, cavity):
                v("PATCH_COVERAGE",
                  f"缺陷 {did} 在层 {p}、分区 {zid} 的补片未完全覆盖该层"
                  f"局部揭除轮廓",
                  defects=[did], plies=[p], zids=[zid])
            elif lap is not None:
                reach = lap_width_around(cavity, patch_poly, lap)
                if reach + 1e-6 < lap:
                    v("PATCH_LAP_INSUFFICIENT",
                      f"缺陷 {did} 在层 {p}、分区 {zid} 的补片周向最小搭接 "
                      f"{reach:.1f}mm，小于要求 {lap:g}mm",
                      defects=[did], plies=[p], zids=[zid],
                      lap_mm=round(reach, 2), required_mm=lap)
        # 补片几何不得越出该层实铺原层
        orig = active_latest.get(p)
        if orig is not None and orig["payload"].get("geometry") \
                and not polygon_contains(orig["payload"]["geometry"],
                                         patch_poly):
            v("DEFECT_GEOMETRY_OUT_OF_BOUNDS",
              f"缺陷 {did} 在层 {p} 的补片越出该层实铺几何边界",
              defects=[did], plies=[p], zids=[zid])
        # 逐层退让复刻：浅层补片比深层补片外扩 stepback
        if stepback is not None:
            idx = affected.index(p)
            if idx < len(affected) - 1:
                lower = affected[idx + 1]
                if lower in patches:
                    pu, pl = patch_poly, patches[lower]["payload"]["polygon"]
                    if polygon_contains(pu, pl):
                        reach = lap_width_around(pl, pu, stepback)
                        if reach + 1e-6 < stepback:
                            v("PATCH_STEPBACK",
                              f"缺陷 {did} 层 {p} 补片相对下层 {lower} 退让仅 "
                              f"{reach:.1f}mm，小于逐层退让量 {stepback:g}mm",
                              defects=[did], plies=[p, lower], zids=[zid],
                              stepback_mm=round(reach, 2),
                              required_mm=stepback)
                    else:
                        v("PATCH_STEPBACK",
                          f"缺陷 {did} 层 {p} 补片未覆盖下层 {lower} 补片，"
                          f"挖补台阶未复刻",
                          defects=[did], plies=[p, lower], zids=[zid])

    # ---- 补片材料 / 纤维方向 ----
    for p in up_patch:
        pp = patches[p]["payload"]
        sp = spec_by_id.get(p) or {}
        req_mat = instr.get("material")
        actual_mat = patch_material_of(pp)
        if req_mat == "match":
            req_mat = sp.get("material")
        if req_mat and actual_mat and req_mat != actual_mat:
            v("PATCH_MATERIAL",
              f"缺陷 {did} 层 {p}、分区 {zid} 的补片材料 {actual_mat} 与指令"
              f"要求 {req_mat} 不符",
              defects=[did], plies=[p], zids=[zid],
              material=actual_mat, required=req_mat)
        req_ang = instr.get("angle")
        if req_ang == "match":
            req_ang = sp.get("angle")
        actual_ang = _num(pp.get("angle"))
        if req_ang is not None and actual_ang is not None:
            d = angle_diff(actual_ang, float(req_ang))
            if d > angle_tol:
                v("PATCH_ANGLE_MISMATCH",
                  f"缺陷 {did} 层 {p}、分区 {zid} 的补片方向 {actual_ang}° "
                  f"与指令 {req_ang}° 偏差 {d:.1f}°，超过允差 {angle_tol}°",
                  defects=[did], plies=[p], zids=[zid],
                  actual_deg=actual_ang, required_deg=req_ang)
        if pp.get("face") and sp.get("face") and pp["face"] != sp["face"]:
            v("PATCH_FACE_MISMATCH",
              f"缺陷 {did} 层 {p} 的补片正反面 {pp['face']} 与原层要求 "
              f"{sp['face']} 不符",
              defects=[did], plies=[p], zids=[zid])

    # ---- 补片与开孔净距（模具坐标统一投影）----
    min_clear = instr.get("min_clearance_mm")
    if min_clear is not None:
        for p in up_patch:
            pp = patches[p]["payload"]["polygon"]
            for op in config["openings"]:
                gap = min_gap(pp, op["polygon"])
                if gap + 1e-6 < min_clear:
                    v("PATCH_CLEARANCE",
                      f"缺陷 {did} 层 {p} 的补片距开孔 {op['opening_id']} "
                      f"净距仅 {gap:.1f}mm，小于下限 {min_clear:g}mm",
                      defects=[did], plies=[p], zids=[zid],
                      opening_id=op["opening_id"], gap_mm=round(gap, 2),
                      required_mm=min_clear)
        if _valid_polygon(contour):
            for op in config["openings"]:
                gap = min_gap(contour, op["polygon"])
                if gap + 1e-6 < min_clear:
                    v("PATCH_CLEARANCE",
                      f"缺陷 {did}（层 {source_pid}，分区 {zid}）轮廓距开孔 "
                          f"{op['opening_id']} 净距仅 {gap:.1f}mm，"
                          f"小于下限 {min_clear:g}mm",
                      defects=[did], plies=[source_pid], zids=[zid],
                      opening_id=op["opening_id"], gap_mm=round(gap, 2),
                      required_mm=min_clear)

    # ---- 补片接缝：自身间隙与相邻层（含原层封闭层）错开 ----
    seam_layers = []  # (ply, 补片事件 payload, 事件 seq) 供相邻层错开核查
    for p in up_patch:
        pp = patches[p]["payload"]
        for s in pp.get("seams") or []:
            if s.get("gap") is None or s.get("zone") is None:
                v("DEFECT_DATA_MISSING",
                  f"缺陷 {did} 层 {p} 的补片接缝缺少 zone/gap",
                  defects=[did], plies=[p], zids=[s.get("zone")])
                continue
            if s["zone"] not in zones:
                v("ZONE_UNKNOWN",
                  f"缺陷 {did} 层 {p} 的补片接缝引用未定义分区 {s['zone']}",
                  defects=[did], plies=[p], zids=[s["zone"]])
            if seam_max_gap is not None and s["gap"] > seam_max_gap:
                v("PATCH_SEAM_GAP",
                  f"缺陷 {did} 层 {p}、分区 {s['zone']} 的补片接缝间隙 "
                  f"{s['gap']}mm 超过上限 {seam_max_gap}mm",
                  defects=[did], plies=[p], zids=[s["zone"]], gap_mm=s["gap"])
            if s.get("gap", 0) < 0:
                v("PATCH_SEAM_GAP",
                  f"缺陷 {did} 层 {p}、分区 {s['zone']} 的补片接缝重叠 "
                  f"{-s['gap']}mm",
                  defects=[did], plies=[p], zids=[s["zone"]],
                  gap_mm=s["gap"])
        seam_layers.append((p, pp, patches[p]["seq"]))
    if seam_min_stagger is not None:
        # 补片与相邻补片
        for i in range(len(seam_layers) - 1):
            _check_patch_seam_stagger(
                v, did, seam_layers[i], seam_layers[i + 1],
                seam_min_stagger, zones)
        # 最浅补片与首个后续封闭原层
        if shallowest in patches:
            closing = _first_closing_ply(shallowest, rank, active_latest)
            if closing is not None:
                cp = active_latest[closing]["payload"]
                _check_patch_seam_stagger(
                    v, did,
                    (shallowest, patches[shallowest]["payload"],
                     patches[shallowest]["seq"]),
                    (closing, cp, active_latest[closing]["event_seq"]),
                    seam_min_stagger, zones)

    # ---- 后续封闭层逐点对应：封闭层实铺几何必须盖住最浅补片 ----
    if shallowest in patches:
        closing = _first_closing_ply(shallowest, rank, active_latest)
        if closing is not None:
            cp_geom = active_latest[closing]["payload"].get("geometry")
            top_patch = patches[shallowest]["payload"]["polygon"]
            if cp_geom and not polygon_contains(cp_geom, top_patch):
                v("PATCH_CLOSING_MISMATCH",
                  f"缺陷 {did} 的最浅补片（层 {shallowest}，分区 {zid}）"
                  f"未被后续封闭层 {closing} 逐点覆盖，工程师已签补片无法"
                  f"与封闭层对应",
                  defects=[did], plies=[shallowest, closing], zids=[zid])

    # ---- 复检：最后一张补片之后须有通过复检 ----
    last_patch_seq = max(patch_seqs, default=None)
    last_removal_seq = max(remov_seqs, default=None)
    horizon = max(s for s in [last_patch_seq, last_removal_seq] if s is not None) \
        if (last_patch_seq is not None or last_removal_seq is not None) else None
    reins = g["reinspections"]
    after = [r for r in reins if horizon is None or r["seq"] > horizon]
    passing = [r for r in after if r["payload"].get("result") == "pass"]
    if not after or not passing or after[-1]["payload"].get("result") != "pass":
        v("DEFECT_REINSPECTION_OPEN",
          f"缺陷 {did}（层 {source_pid}，分区 {zid}）修补后复检未闭合："
          f"缺少通过的 defect_reinspected 记录",
          defects=[did], plies=affected, zids=[zid])
    # 曾判不合格但其后补做通过的，fail 记录保留在状态/事件链中留痕，不再阻断

    _check_signoff(v, did, found, g, "repair", source_pid, zid,
                   passing[-1] if passing else None,
                   current_version, confirmed_revisions, replacement_seq)


# 铺层规范全局规则（接缝错开/间隙口径与主铺层一致）由调用方解析后传入


def _first_closing_ply(shallowest_pid, rank, active_latest):
    """浅于补片顶层、已实铺的第一张原层（规范序紧邻其后）。"""
    base = rank.get(shallowest_pid)
    if base is None:
        return None
    candidates = sorted(
        (p for p in active_latest if (rank.get(p) or 0) > base),
        key=lambda p: rank[p])
    return candidates[0] if candidates else None


def _check_patch_seam_stagger(v, did, a, b, min_stagger, zones):
    for sa in a[1].get("seams") or []:
        for sb in b[1].get("seams") or []:
            if sa.get("zone") == sb.get("zone") and sa.get("zone") \
                    and sa.get("axis", "x") == sb.get("axis", "x") \
                    and sa.get("at") is not None and sb.get("at") is not None:
                d = abs(sa["at"] - sb["at"])
                if d < min_stagger:
                    v("PATCH_SEAM_STAGGER",
                      f"缺陷 {did} 层 {a[0]} 与 {b[0]} 在分区 {sa['zone']} "
                      f"的补片接缝仅错开 {d}mm，小于 {min_stagger}mm",
                      defects=[did], plies=[a[0], b[0]], zids=[sa["zone"]],
                      stagger_mm=d, required_mm=min_stagger)


def _check_signoff(v, did, found, g, decision, source_pid, zid,
                   passing_reinspection, current_version,
                   confirmed_revisions, replacement_seq):
    """工程师签发：处置闭环的最终闸口，并核对指令版本/源层返工/换版。"""
    sign = g.get("sign")
    fp = found["payload"]
    if sign is None:
        v("REPAIR_NOT_SIGNED",
          f"缺陷 {did}（层 {source_pid}，分区 {zid}）的处置未经工程师签发，"
          f"工单不可批准",
          defects=[did], plies=[source_pid], zids=[zid])
        return
    sp = sign["payload"]
    signed_decision = sp.get("decision")
    # 修补类签发用 confirmed；让步/报废签发须与处置方式同名
    if decision == "repair":
        if signed_decision not in ("repair", "confirmed"):
            v("REPAIR_NOT_SIGNED",
              f"缺陷 {did} 的签发决定 {signed_decision} 与修补处置不符"
              f"（应为 confirmed/repair）",
              defects=[did], plies=[source_pid], zids=[zid],
              signed_decision=signed_decision)
    elif signed_decision != decision:
        v("REPAIR_NOT_SIGNED",
          f"缺陷 {did} 的签发决定 {signed_decision} 与处置方式 "
          f"{decision} 不符",
          defects=[did], plies=[source_pid], zids=[zid],
          signed_decision=signed_decision)
    if passing_reinspection is not None:
        sign_t = parse_time(sp.get("at"))
        reins_t = parse_time(passing_reinspection["payload"].get("at"))
        early = sign_t is not None and reins_t is not None and sign_t < reins_t
        # 时标缺失时退回事件次序判定
        if early or (
                (sign_t is None or reins_t is None)
                and sign["seq"] < passing_reinspection["seq"]):
            v("REPAIR_NOT_SIGNED",
              f"缺陷 {did} 的签发（事件 #{sign['seq']}，{sp.get('at')}）"
              f"早于通过复检（事件 #{passing_reinspection['seq']}，"
              f"{passing_reinspection['payload'].get('at')}），"
              f"复检后须重新签发",
              defects=[did], plies=[source_pid], zids=[zid],
              sign_event=sign["seq"],
              reinspection_event=passing_reinspection["seq"])
    # 指令版本：签发所依据版本须为现行
    sv = sp.get("instruction")
    if not sv:
        v("DEFECT_INSTRUCTION_MISSING",
          f"缺陷 {did} 的签发（事件 #{sign['seq']}）未收录处置指令版本",
          defects=[did], plies=[source_pid], zids=[zid])
    elif current_version and sv != current_version:
        v("REPAIR_INSTRUCTION_VERSION",
          f"缺陷 {did} 的签发依据指令 {sv}，现行版本为 {current_version}，"
          f"签发快照已过期，需重新签发",
          defects=[did], plies=[source_pid], zids=[zid],
          instruction=sv, current=current_version)
    # 签发后源层整层返工：只撤销该缺陷处置
    repl_seq = replacement_seq.get(source_pid)
    if repl_seq is not None and repl_seq > sign["seq"]:
        v("REPAIR_SOURCE_REWORKED",
          f"缺陷 {did} 签发后源层 {source_pid} 已整层返工（替代层事件 "
          f"#{repl_seq}），原处置仅对旧铺层有效，需重新登记/签发",
          defects=[did], plies=[source_pid], zids=[zid],
          replacement_event=repl_seq, sign_event=sign["seq"])
    # 签发后相关规范换版（指令块变化）：只撤销引用旧指令的处置
    for rev in confirmed_revisions or ():
        confirmed_at = parse_time(rev.get("confirmed_at"))
        sign_at = parse_time(sp.get("at"))
        if confirmed_at is None or sign_at is None:
            continue
        if confirmed_at > sign_at and _repairs_changed(rev):
            v("REPAIR_SPEC_SUPERSEDED",
              f"缺陷 {did} 签发后规范已换版至 v{rev.get('revision')} 且"
              f"处置指令相关内容变化，原签发撤销，需按新指令重新签发",
              defects=[did], plies=[source_pid], zids=[zid],
              revision=rev.get("revision"), sign_event=sign["seq"])
            break


def _repairs_changed(rev):
    """已确认换版是否改变 repairs 指令块（与基线比较）。"""
    new = rev.get("spec") or {}
    base = rev.get("base_spec") or {}
    import json as _json
    return _json.dumps(new.get("repairs"), sort_keys=True, ensure_ascii=False) \
        != _json.dumps(base.get("repairs"), sort_keys=True, ensure_ascii=False)


def _case_status(case, config, instr_version, current_version,
                 replacement_seq, confirmed_revisions):
    """案例级状态摘要（供 /state 与随件包）。"""
    g = case["generations"][case["current_gen"]]
    if g["sign"] is not None:
        stale = False
        if current_version:
            sv = g["sign"]["payload"].get("instruction")
            stale = bool(sv and sv != current_version)
        if not stale:
            pid = case["found"]["payload"]["ply_id"]
            repl = replacement_seq.get(pid)
            if repl is not None and repl > g["sign"]["seq"]:
                stale = True
        if not stale:
            for rev in confirmed_revisions or ():
                ca = parse_time(rev.get("confirmed_at"))
                sa = parse_time(g["sign"]["payload"].get("at"))
                if ca and sa and ca > sa and _repairs_changed(rev):
                    stale = True
                    break
        return "signed_stale" if stale else "signed"
    if g["patches"] or g["removals"]:
        return "repair_open"
    if g["isolated"]:
        return "isolated"
    return "found"


def empty_state():
    """无处置规范时的空状态（老工单快照结构一致）。"""
    return {"enabled": False, "instruction_version": None,
            "openings": [], "defects": []}
