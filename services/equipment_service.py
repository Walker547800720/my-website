"""设备管理；复用现有数据库，不初始化数据库、不修改表结构。

类型字段及“故障中/维护中”状态按当前范围暂缓。
状态写入支持现有“已停止/运行中/故障/离线”四种状态。
查询返回 dict（按编号查无结果时返回 None），写入返回更新后的 dict。
新增示例：create_equipment("WELD-004", "焊接设备4", temperature_c=25.0)。
temperature_c 必须显式提供，避免为现有 NOT NULL 字段编造测量值。
所有函数均可用 db_path 关键字参数指定独立数据库。
"""

import math

from database.db import DEFAULT_DB_PATH, get_connection, utc_now


UPDATABLE_STATUSES = ("已停止", "运行中", "故障", "离线")
DATABASE_STATUSES = ("已停止", "运行中", "故障", "离线")


class DuplicateEquipmentError(ValueError):
    """设备编号已存在。"""


class EquipmentNotFoundError(LookupError):
    """需要修改的设备不存在。"""


class ConfigurationError(ValueError):
    """维护默认配置缺失或不合法。"""


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}必须为非空字符串")
    return value.strip()


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}必须为有限数值")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{field}必须为有限数值") from None
    if not math.isfinite(number):
        raise ValueError(f"{field}必须为有限数值")
    return number


def _find(connection, equipment_id):
    row = connection.execute(
        "SELECT * FROM equipment WHERE equipment_id = ?", (equipment_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def list_equipment(*, db_path=DEFAULT_DB_PATH):
    """按设备编号排序查询全部设备。"""
    with get_connection(db_path) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM equipment ORDER BY equipment_id"
        )]


def get_equipment(equipment_id, *, db_path=DEFAULT_DB_PATH):
    """按编号精确查询；不存在时返回 None。"""
    equipment_id = _text(equipment_id, "设备编号")
    with get_connection(db_path) as connection:
        return _find(connection, equipment_id)


def _maintenance_defaults(connection):
    rows = connection.execute(
        """SELECT config_key, config_value, unit FROM system_config
           WHERE config_key IN (?, ?)""",
        ("maintenance_interval_hours", "reminder_lead_hours"),
    ).fetchall()
    configs = {row["config_key"]: row for row in rows}
    try:
        interval_row = configs["maintenance_interval_hours"]
        lead_row = configs["reminder_lead_hours"]
        interval = _number(interval_row["config_value"], "维护周期")
        lead = _number(lead_row["config_value"], "提前提醒时间")
    except (KeyError, ValueError) as error:
        raise ConfigurationError("维护周期或提前提醒配置缺失或不是有限数值") from error
    if (interval_row["unit"] != "h" or lead_row["unit"] != "h"
            or interval <= 0 or lead < 0 or lead > interval):
        raise ConfigurationError("维护配置必须以小时为单位，周期大于0，提前提醒在0到周期之间")
    return interval, lead


def create_equipment(equipment_id, name, *, temperature_c, db_path=DEFAULT_DB_PATH):
    """原子新增停止设备及其当前维护计划；重复编号抛 DuplicateEquipmentError。"""
    equipment_id = _text(equipment_id, "设备编号")
    name = _text(name, "设备名称")
    temperature_c = _number(temperature_c, "初始温度")
    with get_connection(db_path) as connection:
        # 在读取配置之前开启写事务，避免读取配置与设备/计划写入之间发生竞争。
        connection.execute("BEGIN IMMEDIATE")
        if _find(connection, equipment_id) is not None:
            raise DuplicateEquipmentError(f"设备编号已存在：{equipment_id}")
        interval, lead = _maintenance_defaults(connection)
        timestamp = utc_now()
        connection.execute(
            """INSERT INTO equipment
               (equipment_id, name, total_runtime_hours, temperature_c,
                welding_count, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (equipment_id, name, 0.0, temperature_c, 0, "已停止", timestamp, timestamp),
        )
        connection.execute(
            """INSERT INTO maintenance_plans
               (equipment_id, maintenance_interval_hours,
                last_maintenance_runtime_hours, next_maintenance_runtime_hours,
                reminder_lead_hours, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (equipment_id, interval, 0.0, interval, lead, timestamp),
        )
        return _find(connection, equipment_id)


def update_equipment(equipment_id, name, *, db_path=DEFAULT_DB_PATH):
    """仅修改名称和更新时间；不接受编号、计数、运行时间或类型的修改参数。"""
    equipment_id = _text(equipment_id, "设备编号")
    name = _text(name, "设备名称")
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            "UPDATE equipment SET name = ?, updated_at = ? WHERE equipment_id = ?",
            (name, utc_now(), equipment_id),
        )
        if cursor.rowcount == 0:
            raise EquipmentNotFoundError(f"设备不存在：{equipment_id}")
        return _find(connection, equipment_id)


def update_equipment_status(equipment_id, status, *, db_path=DEFAULT_DB_PATH):
    """支持现有四种设备状态；不更改历史快照或累计数据。"""
    equipment_id = _text(equipment_id, "设备编号")
    status = _text(status, "设备状态")
    if status not in UPDATABLE_STATUSES:
        raise ValueError("设备状态仅支持：已停止、运行中、故障、离线")
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            "UPDATE equipment SET status = ?, updated_at = ? WHERE equipment_id = ?",
            (status, utc_now(), equipment_id),
        )
        if cursor.rowcount == 0:
            raise EquipmentNotFoundError(f"设备不存在：{equipment_id}")
        return _find(connection, equipment_id)


def get_equipment_statistics(*, db_path=DEFAULT_DB_PATH):
    """统计现有四种状态及累计值；空数据库各项返回0。"""
    with get_connection(db_path) as connection:
        # 一条查询提供一致的数据快照，避免并发更新时各项统计不一致。
        rows = connection.execute(
            """SELECT status, COUNT(*) AS count,
                      SUM(total_runtime_hours) AS runtime_hours,
                      SUM(welding_count) AS welding_count
               FROM equipment GROUP BY status"""
        ).fetchall()
    status_counts = dict.fromkeys(DATABASE_STATUSES, 0)
    for row in rows:
        status_counts[row["status"]] = row["count"]
    return {
        "total_count": sum(row["count"] for row in rows),
        "status_counts": status_counts,
        "total_runtime_hours": sum((row["runtime_hours"] for row in rows), 0.0),
        "welding_count": sum(row["welding_count"] for row in rows),
    }
