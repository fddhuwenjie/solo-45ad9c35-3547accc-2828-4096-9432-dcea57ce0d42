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
