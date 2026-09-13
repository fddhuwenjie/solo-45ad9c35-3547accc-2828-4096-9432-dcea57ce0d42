"""sqlite3 持久化层。

铺放事件表（events）只提供 INSERT 路径，不提供 UPDATE/DELETE，
保证铺放记录追加式不可篡改；批准快照（approvals）同样只增不改。
"""

import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    tool_datum  TEXT NOT NULL,          -- JSON：模具基准
    zones       TEXT NOT NULL,          -- JSON：分区边界列表
    spec        TEXT NOT NULL,          -- JSON：铺层规范（含规则阈值）
    status      TEXT NOT NULL DEFAULT 'open',   -- open | approved
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rolls (
    job_id           TEXT NOT NULL REFERENCES jobs(id),
    roll_id          TEXT NOT NULL,
    batch_no         TEXT NOT NULL,     -- 料卷批号
    material         TEXT NOT NULL,
    out_time_limit_h REAL,              -- 解冻后外置时间上限（小时）
    created_at       TEXT NOT NULL,
    PRIMARY KEY (job_id, roll_id)
);

-- 追加式事件链：PRIMARY KEY(job_id, seq)，代码中无 UPDATE/DELETE
CREATE TABLE IF NOT EXISTS events (
    job_id      TEXT NOT NULL REFERENCES jobs(id),
    seq         INTEGER NOT NULL,
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,          -- JSON
    operator    TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);

CREATE TABLE IF NOT EXISTS approvals (
    job_id      TEXT NOT NULL REFERENCES jobs(id),
    version     INTEGER NOT NULL,
    snapshot    TEXT NOT NULL,          -- JSON：冻结的规范/批次/事件链/状态
    chain_hash  TEXT NOT NULL,          -- 事件链 SHA-256
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    PRIMARY KEY (job_id, version)
);

-- 材料谱系事件链（整卷登记/裁切/拆包/转移/退库/报废/解冻/纠正绑定），
-- 全局共享、跨工单引用；与 events 表一样只允许 INSERT
CREATE TABLE IF NOT EXISTS material_events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,          -- JSON
    operator    TEXT,
    recorded_at TEXT NOT NULL
);

-- 规范换版：propose 时写入（spec/映射/返工序列自此固定），confirm 仅翻转
-- status/confirmed_at；铺放与材料事件链不受影响（保持只读）
CREATE TABLE IF NOT EXISTS spec_revisions (
    job_id         TEXT NOT NULL REFERENCES jobs(id),
    revision       INTEGER NOT NULL,
    base_revision  INTEGER NOT NULL,        -- 派生自（0 = 建档规范）
    spec           TEXT NOT NULL,           -- JSON：新规范全文
    reason         TEXT NOT NULL,           -- 变更理由
    effective_at   TEXT NOT NULL,           -- 生效时刻
    impact         TEXT NOT NULL,           -- JSON：层映射/沿用层/返工序列
    status         TEXT NOT NULL DEFAULT 'proposed',  -- proposed | confirmed
    created_at     TEXT NOT NULL,
    confirmed_at   TEXT,
    PRIMARY KEY (job_id, revision)
);
"""


class Store:
    def __init__(self, path):
        self.path = path
        with self.db() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
