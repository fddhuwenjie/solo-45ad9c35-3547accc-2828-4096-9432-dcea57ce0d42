"""平面几何工具：多边形面积、质心、点包含、网格采样覆盖率。

坐标系由模具基准（tool_datum）定义，单位 mm。仅使用标准库。
"""


def polygon_area(points):
    """鞋带公式求有符号面积。points: [[x, y], ...]"""
    a = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def polygon_centroid(points):
    """面积加权质心；退化多边形回退为顶点平均。"""
    a = polygon_area(points)
    if abs(a) < 1e-12:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    cx = cy = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        f = x1 * y2 - x2 * y1
        cx += (x1 + x2) * f
        cy += (y1 + y2) * f
    return (cx / (6.0 * a), cy / (6.0 * a))


def point_in_polygon(x, y, poly):
    """射线法判断点是否在多边形内（边界算作内）。"""
    # 先检查是否落在边上
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        px, py = x - x1, y - y1
        cross = dx * py - dy * px
        if abs(cross) < 1e-9 and min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9 \
                and min(y1, y2) - 1e-9 <= y <= max(y1, y2) + 1e-9:
            return True
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def coverage_fraction(zone_poly, ply_poly, samples=16):
    """用规则网格采样估计分区被某张预浸料覆盖的面积比例（0~1）。"""
    xs = [p[0] for p in zone_poly]
    ys = [p[1] for p in zone_poly]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    w, h = maxx - minx, maxy - miny
    if w <= 0 or h <= 0:
        return 0.0
    total = hit = 0
    for i in range(samples):
        for j in range(samples):
            x = minx + (i + 0.5) * w / samples
            y = miny + (j + 0.5) * h / samples
            if point_in_polygon(x, y, zone_poly):
                total += 1
                if point_in_polygon(x, y, ply_poly):
                    hit += 1
    return hit / total if total else 0.0
