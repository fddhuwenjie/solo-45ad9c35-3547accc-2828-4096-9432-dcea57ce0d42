# 预浸料铺层放行 API

仅用 Python 标准库（`wsgiref` + `sqlite3`）实现的复合材料蒙皮铺层放行服务。
按层序重放追加式铺放事件，重建分区覆盖与厚度，执行放行规则；批准版冻结
规范、材料批次与事件链快照，版本比较与 JSON 随件包均取自该快照。
铺层中途规范换版（材料替代/丢层调整/开孔边界变化）作为独立分支：
影响分析标出可沿用层与返工序列，确认后校验、批准与随件包从新分支重算。

## 运行

```bash
python3 run_server.py 8000        # 启动服务（生成 prepreg.db）
python3 demo.py                   # 端到端演示（违规→返工→复压→批准→版本比较→换版→随件包）
python3 test_rules.py             # 铺放规则覆盖测试（27 项）
python3 test_compaction.py        # 阶段压实/真空检漏覆盖测试（33 项）
python3 test_revision.py          # 规范换版覆盖测试（46 项）
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
| `DATA_MISSING` / `SPEC_DATA_MISSING` / `ROLL_UNKNOWN` / `MATERIAL_MISMATCH` / `ZONE_UNKNOWN` | 资料缺失或引用冲突（含接缝引用未定义分区） |
| `SPEC_DUPLICATE_SEQ` / `SPEC_DUPLICATE_PLY` | 规范层序 / 层号重复 |
| `INVALID_TIME` | 铺放时刻无法解析 |
| `DATUM_MISSING` | 模具基准（tool_datum）缺失或为空 |

每条违规都带 `plies`（层号）与 `zones`（区域）。存在任何违规时
`POST /jobs/{id}/approve` 返回 **409** 并附完整违规清单，拒绝放行。

## 批准与快照

- `POST /jobs/{id}/approve` `{"approved_by": "..."}` — 校验通过后冻结
  **规范哈希、材料批次清单、完整事件链及其 SHA-256** 为不可变快照。
- 批准后工单回到可追加状态（旧快照不受影响），再次批准产生新版本。
- `GET /jobs/{id}/approvals/diff?a=1&b=2` — 比较两版：规范是否变更、
  批次增删改、事件链新增序号、分区厚度变化。
- `GET /jobs/{id}/approvals/{v}/package` — JSON 随件包，完全取自冻结快照。

## 规范换版

铺层进行到一半时工艺规范可能换版（材料替代、丢层调整、开孔边界变化）。
换版不改动既有铺放/材料事件（只读），而是把新规范登记为工单的新分支。

**提议 `POST /jobs/{id}/spec-revisions`**

```json
{
  "spec": {"materials": {...}, "plies": [...], "rules": {...}},
  "reason": "材料替代：CF-EP-3K 停产",
  "effective_at": "2026-09-12T08:00:00Z",
  "base_revision": 0,
  "mapping": {"N01": "P01"}
}
```

- `base_revision` 指定派生基线（0 = 建档规范，缺省为当前生效版）；
  `mapping` 可选，用于重编号场景的显式层映射（新层号 → 旧层号），
  未提及的层按同号自动配对。
- 引擎逐层比较材料、层序、角度、正反面、覆盖区、接缝与丢层边界，
  返回影响分析：`carry_over`（可沿用的实铺层）、`rework.remove`
  （确定的揭除序列，自上而下；中间层变化时上覆层连带）、
  `rework.invalidated_checkpoints`（随之失效的压实检查点）、
  `rework.relay`（待补铺序列，按新层序）。
- 冲突时返回 **409** 且不启用新版：
  `MAPPING_AMBIGUOUS`（层映射多解）、`ZONE_DATUM_INCOMPATIBLE`
  （分区基准不兼容）、`EFFECTIVE_BEFORE_RECORDS`（生效时刻早于现场记录）、
  `LOCKED_PLY_AFFECTED`（返工需揭除已批准快照锁定的铺层）。

**确认 `POST /jobs/{id}/spec-revisions/{r}/confirm`**

固定新规范、层映射与处置决定（propose 写入后不再更改）；此后校验、
批准与 JSON 随件包均从该分支重算，已批准工单回到待放行状态。
基线已过期的提议确认时返回 409 `stale_base`。返工仍通过追加
`ply_removed` / `ply_replaced` 事件闭环。

`GET /jobs/{id}/spec-revisions`、`GET /jobs/{id}/spec-revisions/{r}`
查询换版列表与详情（含影响分析）。

## 其他接口

`GET /jobs`、`GET /jobs/{id}`、`POST /jobs/{id}/rolls`、
`GET /jobs/{id}/events`、`GET /jobs/{id}/state`（重建的分区厚度、
相邻分区厚度阶差、料卷外置时间台账）、`GET /jobs/{id}/approvals`。
