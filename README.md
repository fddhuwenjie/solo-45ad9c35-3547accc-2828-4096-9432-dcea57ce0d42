# 预浸料铺层放行 API

仅用 Python 标准库（`wsgiref` + `sqlite3`）实现的复合材料蒙皮铺层放行服务。
按层序重放追加式铺放事件，重建分区覆盖与厚度，执行放行规则；批准版冻结
规范、材料批次与事件链快照，版本比较与 JSON 随件包均取自该快照。

## 运行

```bash
python3 run_server.py 8000        # 启动服务（生成 prepreg.db）
python3 demo.py                   # 端到端演示（违规→返工→批准→版本比较→随件包）
python3 test_rules.py             # 规则覆盖测试（15 项）
```

## 数据模型

**建档 `POST /jobs`**

```json
{
  "name": "机翼蒙皮-A1",
  "tool_datum": {"datum_id": "MOLD-A1", "origin": [0,0,0], "units": "mm"},
  "zones": [{"zone_id": "Z1", "polygon": [[0,0],[200,0],[200,60],[0,60]],
             "adjacent": ["Z2"]}],
  "spec": {
    "materials": {"CF-EP-3K": {"ply_thickness": 0.125}},
    "plies": [{"seq": 1, "ply_id": "P01", "material": "CF-EP-3K", "angle": 0,
               "face": "up", "zones": ["Z1","Z2"], "drop_at": null}],
    "rules": {"angle_tolerance_deg": 3.0, "max_consecutive_same_angle": 4,
              "seam_min_stagger_mm": 25.0, "seam_max_gap_mm": 1.5,
              "drop_min_stagger_mm": 12.0, "require_symmetry": true,
              "require_balance": true, "coverage_min_fraction": 0.98}
  },
  "rolls": [{"roll_id": "R1", "batch_no": "B2026-0901",
             "material": "CF-EP-3K", "out_time_limit_h": 240}]
}
```

**追加事件 `POST /jobs/{id}/events`**（只允许追加，无修改/删除接口）

| type | 说明 | 关键字段 |
|---|---|---|
| `ply_placed` | 铺放一层 | `ply_id, roll, angle, face, geometry, seams, placed_at` |
| `ply_removed` | 揭除一层 | `ply_id, reason` |
| `ply_replaced` | 返工闭环 | `removed_ply_id, replacement{...}`（替代层回到原层位） |
| `roll_thawed` / `roll_refrigerated` | 料卷解冻/回冻 | `roll, at` |
| `note` | 过程备注 | `text` |

接缝格式：`{"zone": "Z1", "axis": "x", "at": 100.0, "gap": 0.6}`，
`gap < 0` 为重叠、`gap` 超上限为超隙；相邻层同区同轴接缝位置错开需 ≥
`seam_min_stagger_mm`。

## 放行规则（`GET /jobs/{id}/validate`）

| 规则码 | 检测内容 |
|---|---|
| `MISSING_PLY` / `DUPLICATE_PLY` / `UNEXPECTED_PLY` | 缺层 / 重复层 / 非规范层 |
| `ORDER_MISMATCH` | 铺层次序与规范不符 |
| `ANGLE_MISMATCH` / `FACE_MISMATCH` | 纤维方向超差 / 正反面错误 |
| `MISSING_COVERAGE` | 分区覆盖率不足（网格采样核算） |
| `SEAM_OVERLAP` / `SEAM_GAP_EXCEEDED` / `SEAM_STAGGER` | 接缝重叠 / 超隙 / 相邻层错开不足 |
| `CONSECUTIVE_ANGLE` | 连续同向层数超限 |
| `SYMMETRY_VIOLATION` / `BALANCE_VIOLATION` | 中面对称 / ±θ 平衡破坏 |
| `DROP_STAGGER` | 丢层错开不足（厚度突变） |
| `OUT_TIME_EXCEEDED` / `OUT_TIME_DATA_MISSING` | 外置时间超限 / 解冻记录缺失 |
| `REMOVAL_WITHOUT_REPLACEMENT` / `REWORK_LINK_BROKEN` | 返工未串接揭除层与替代层 |
| `DATA_MISSING` / `SPEC_DATA_MISSING` / `ROLL_UNKNOWN` / `MATERIAL_MISMATCH` / `ZONE_UNKNOWN` | 资料缺失或引用冲突 |

每条违规都带 `plies`（层号）与 `zones`（区域）。存在任何违规时
`POST /jobs/{id}/approve` 返回 **409** 并附完整违规清单，拒绝放行。

## 批准与快照

- `POST /jobs/{id}/approve` `{"approved_by": "..."}` — 校验通过后冻结
  **规范哈希、材料批次清单、完整事件链及其 SHA-256** 为不可变快照。
- 批准后工单回到可追加状态（旧快照不受影响），再次批准产生新版本。
- `GET /jobs/{id}/approvals/diff?a=1&b=2` — 比较两版：规范是否变更、
  批次增删改、事件链新增序号、分区厚度变化。
- `GET /jobs/{id}/approvals/{v}/package` — JSON 随件包，完全取自冻结快照。

## 其他接口

`GET /jobs`、`GET /jobs/{id}`、`POST /jobs/{id}/rolls`、
`GET /jobs/{id}/events`、`GET /jobs/{id}/state`（重建的分区厚度、
相邻分区厚度阶差、料卷外置时间台账）、`GET /jobs/{id}/approvals`。
