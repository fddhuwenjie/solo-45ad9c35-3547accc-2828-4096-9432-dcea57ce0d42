"""预浸料铺层放行 API（仅标准库：WSGI + sqlite3）。

按层序重放追加式铺放事件，重建分区覆盖与厚度，执行放行规则校验；
批准版冻结规范、材料批次与事件链快照，版本比较与 JSON 随件包均取自快照。
"""

from .app import make_app

__all__ = ["make_app"]
