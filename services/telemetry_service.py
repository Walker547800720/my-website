"""运行数据保存与查询；运行时间增量单位为小时，温度单位为摄氏度。

状态只从 equipment 读取。保存返回新快照 dict，最新查询在无快照时返回
None，历史查询返回按 recorded_at、id 倒序排列的 dict 列表。
查询/保存不存在的设备均抛 EquipmentNotFoundError，参数无效抛 ValueError。
limit=None 查询全部历史，正整数则限制条数。所有接口支持 db_path 关键字。
"""

import math

from database.db import DEFAULT_DB_PATH, get_connection, utc_now
from services.equipment_service import EquipmentNotFoundError


SQLITE_MAX_INTEGER = 2**63 - 1


def _equipment_id(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("设备编号必须为非空字符串")
    return value.strip()


def _finite_number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}必须为有限数值")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError(f"{field}必须为有限数值") from None
    if not math.isfinite(result):
        raise ValueError(f"{field}必须为有限数值")
    return result


def _require_equipment(connection, equipment_id):
    row = connection.execute(
        "SELECT * FROM equipment WHERE equipment_id = ?", (equipment_id,)
    ).fetchone()
    if row is None:
        raise EquipmentNotFoundError(f"设备不存在：{equipment_id}")
    return row


def save_telemetry(equipment_id, temperature_c, runtime_increment=0,
                   welding_count_increment=0, *, db_path=DEFAULT_DB_PATH):
    """在同一事务中更新设备并保存快照；非运行设备仅允许两个增量均为0。"""
    equipment_id = _equipment_id(equipment_id)
    temperature_c = _finite_number(temperature_c, "温度")
    runtime_increment = _finite_number(runtime_increment, "运行时间增量")
    if runtime_increment < 0:
        raise ValueError("运行时间增量不得为负数")
    if (isinstance(welding_count_increment, bool)
            or not isinstance(welding_count_increment, int)
            or not 0 <= welding_count_increment <= SQLITE_MAX_INTEGER):
        raise ValueError("焊接次数增量必须为SQLite整数范围内的非负整数")

    with get_connection(db_path) as connection:
        # 先获取写锁，再读取状态和累计值，避免并发保存丢失增量。
        connection.execute("BEGIN IMMEDIATE")
        equipment = _require_equipment(connection, equipment_id)
        if equipment["status"] != "运行中" and (
                runtime_increment > 0 or welding_count_increment > 0):
            raise ValueError("非运行设备只能保存运行时间及焊接次数增量均为0的快照")
        total_runtime = _finite_number(
            equipment["total_runtime_hours"] + runtime_increment, "累计运行时间"
        )
        welding_count = equipment["welding_count"] + welding_count_increment
        if welding_count > SQLITE_MAX_INTEGER:
            raise ValueError("累计焊接次数超出SQLite整数范围")
        timestamp = utc_now()
        connection.execute(
            """UPDATE equipment SET temperature_c = ?, total_runtime_hours = ?,
                      welding_count = ?, updated_at = ? WHERE equipment_id = ?""",
            (temperature_c, total_runtime, welding_count, timestamp, equipment_id),
        )
        cursor = connection.execute(
            """INSERT INTO telemetry
               (equipment_id, temperature_c, status, total_runtime_hours,
                welding_count, runtime_increment, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (equipment_id, temperature_c, equipment["status"], total_runtime,
             welding_count, runtime_increment, timestamp),
        )
        return dict(connection.execute(
            "SELECT * FROM telemetry WHERE id = ?", (cursor.lastrowid,)
        ).fetchone())


def get_latest_telemetry(equipment_id, *, db_path=DEFAULT_DB_PATH):
    """查询最新快照；时间相同时以更大的id为最新。"""
    equipment_id = _equipment_id(equipment_id)
    with get_connection(db_path) as connection:
        _require_equipment(connection, equipment_id)
        row = connection.execute(
            """SELECT * FROM telemetry WHERE equipment_id = ?
               ORDER BY recorded_at DESC, id DESC LIMIT 1""", (equipment_id,)
        ).fetchone()
        return dict(row) if row is not None else None


def get_telemetry_history(equipment_id, limit=None, *, db_path=DEFAULT_DB_PATH):
    """倒序查询指定设备历史；limit为None或正整数。"""
    equipment_id = _equipment_id(equipment_id)
    if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int)
            or not 1 <= limit <= SQLITE_MAX_INTEGER):
        raise ValueError("数量限制必须为SQLite整数范围内的正整数或None")
    with get_connection(db_path) as connection:
        _require_equipment(connection, equipment_id)
        sql = """SELECT * FROM telemetry WHERE equipment_id = ?
                 ORDER BY recorded_at DESC, id DESC"""
        parameters = (equipment_id,)
        if limit is not None:
            sql += " LIMIT ?"
            parameters += (limit,)
        return [dict(row) for row in connection.execute(sql, parameters)]
