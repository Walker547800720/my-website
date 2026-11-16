"""仅为现有故障表新增处理说明字段；不修改或解析已有描述。

在项目目录执行：python -m database.migrate_fault_handling_note
重复执行无副作用；新数据库由 schema.sql 直接包含此字段。
"""

from pathlib import Path

from database.db import DEFAULT_DB_PATH, get_connection


def migrate_fault_handling_note(db_path=DEFAULT_DB_PATH):
    """事务内新增 handling_note；已存在返回 False，新增成功返回 True。"""
    if not Path(db_path).is_file():
        raise FileNotFoundError(f"待迁移数据库不存在：{db_path}")
    with get_connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(fault_records)")}
        if not columns:
            raise ValueError("待迁移数据库缺少 fault_records 表")
        if "handling_note" in columns:
            return False
        connection.execute(
            "ALTER TABLE fault_records ADD COLUMN handling_note TEXT NOT NULL DEFAULT ''"
        )
    return True


if __name__ == "__main__":
    changed = migrate_fault_handling_note()
    print("迁移完成：已新增 handling_note，已有数据保留。" if changed else "handling_note 已存在，无需迁移。")
