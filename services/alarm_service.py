"""温度/维护预警，显式调用 check_alarms 自动判定，不创建后台任务。

温度阈值读取system_config；维护窗口来自各设备计划。持续异常复用同一条
未解除预警及event_key（含已确认记录），等级/内容随条件更新。条件恢复后
解除，复发创建新事件键以保留历史。预警不会改变设备状态或创建故障。
"""

import math
from uuid import uuid4

from database.db import DEFAULT_DB_PATH, get_connection, utc_now
from services.equipment_service import ConfigurationError, EquipmentNotFoundError
from services.maintenance_service import MAINTENANCE_ALARM_TYPE, _load_plan, _plan_view, _text


TEMPERATURE_ALARM_TYPE = "温度预警"
ALARM_STATUSES = ("未处理", "已确认", "已解除")


class AlarmNotFoundError(LookupError):
    """预警不存在。"""


def _temperature_thresholds(connection):
    rows = connection.execute(
        """SELECT config_key, config_value, unit FROM system_config
           WHERE config_key IN (?, ?)""",
        ("temperature_warning_c", "temperature_critical_c"),
    ).fetchall()
    configs = {row["config_key"]: row for row in rows}
    try:
        warning = configs["temperature_warning_c"]
        critical = configs["temperature_critical_c"]
        if (warning["unit"] != "℃" or critical["unit"] != "℃"
                or not math.isfinite(warning["config_value"])
                or not math.isfinite(critical["config_value"])
                or not 0 < warning["config_value"] < critical["config_value"]):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ConfigurationError("温度阈值必须为有效摄氏度配置，且0<预警阈值<严重阈值") from None
    return warning["config_value"], critical["config_value"]


def _sync_alarm(connection, equipment_id, alarm_type, level, content, timestamp):
    active = connection.execute(
        """SELECT * FROM alarm_records WHERE equipment_id = ? AND alarm_type = ?
           AND status IN (?, ?) ORDER BY id""",
        (equipment_id, alarm_type, "未处理", "已确认"),
    ).fetchall()
    if level is None:
        connection.execute(
            """UPDATE alarm_records SET status = ?, resolved_at = ?
               WHERE equipment_id = ? AND alarm_type = ? AND status IN (?, ?)""",
            ("已解除", timestamp, equipment_id, alarm_type, "未处理", "已确认"),
        )
        return None
    if active:
        # 已确认预警也属于同一未解除事件，不重置确认状态/发生时间/事件键。
        alarm_id = active[0]["id"]
        connection.execute(
            "UPDATE alarm_records SET level = ?, content = ? WHERE id = ?",
            (level, content, alarm_id),
        )
    else:
        cursor = connection.execute(
            """INSERT INTO alarm_records
               (equipment_id, alarm_type, level, content, status, occurred_at, resolved_at, event_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (equipment_id, alarm_type, level, content, "未处理", timestamp, None,
             f"{alarm_type}:{uuid4().hex}"),
        )
        alarm_id = cursor.lastrowid
    return dict(connection.execute("SELECT * FROM alarm_records WHERE id = ?", (alarm_id,)).fetchone())


def check_alarms(equipment_id=None, *, db_path=DEFAULT_DB_PATH):
    """检查指定设备或全部设备；原子同步预警并返回本次仍有效的预警列表。"""
    if equipment_id is not None:
        equipment_id = _text(equipment_id, "设备编号")
    with get_connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        warning, critical = _temperature_thresholds(connection)
        if equipment_id is None:
            equipment_rows = connection.execute("SELECT * FROM equipment ORDER BY equipment_id").fetchall()
        else:
            equipment_rows = connection.execute(
                "SELECT * FROM equipment WHERE equipment_id = ?", (equipment_id,)
            ).fetchall()
            if not equipment_rows:
                raise EquipmentNotFoundError(f"设备不存在：{equipment_id}")
        timestamp = utc_now()
        result = []
        for equipment in equipment_rows:
            device_id = equipment["equipment_id"]
            temperature = equipment["temperature_c"]
            if not math.isfinite(temperature):
                raise ValueError("设备温度必须为有限数值")
            plan = _plan_view(_load_plan(connection, device_id))
            temperature_level = ("严重" if temperature >= critical else
                                 "警告" if temperature >= warning else None)
            maintenance_level = ("严重" if plan["maintenance_status"] == "维护到期" else
                                 "警告" if plan["maintenance_status"] == "即将维护" else None)
            for alarm_type, level, content in (
                (TEMPERATURE_ALARM_TYPE, temperature_level,
                 f"温度{temperature:g}℃，预警阈值{warning:g}℃，严重阈值{critical:g}℃"),
                (MAINTENANCE_ALARM_TYPE, maintenance_level,
                 f"{plan['maintenance_status']}，剩余运行时间{plan['remaining_runtime_hours']:g}h，"
                 f"维护目标{plan['next_maintenance_runtime_hours']:g}h"),
            ):
                alarm = _sync_alarm(connection, device_id, alarm_type, level, content, timestamp)
                if alarm is not None:
                    result.append(alarm)
        return result


def list_alarms(equipment_id=None, status=None, *, db_path=DEFAULT_DB_PATH):
    """查询全部或指定设备的预警，可按现有处理状态筛选，按时间及id倒序。"""
    if equipment_id is not None:
        equipment_id = _text(equipment_id, "设备编号")
    if status is not None:
        status = _text(status, "预警状态")
        if status not in ALARM_STATUSES:
            raise ValueError("预警状态仅支持：未处理、已确认、已解除")
    conditions, parameters = [], []
    if equipment_id is not None:
        conditions.append("equipment_id = ?")
        parameters.append(equipment_id)
    if status is not None:
        conditions.append("status = ?")
        parameters.append(status)
    sql = "SELECT * FROM alarm_records"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    with get_connection(db_path) as connection:
        if equipment_id is not None and connection.execute(
            "SELECT 1 FROM equipment WHERE equipment_id = ?", (equipment_id,)
        ).fetchone() is None:
            raise EquipmentNotFoundError(f"设备不存在：{equipment_id}")
        return [dict(row) for row in connection.execute(
            sql + " ORDER BY occurred_at DESC, id DESC", parameters
        )]


def resolve_alarm(alarm_id, *, db_path=DEFAULT_DB_PATH):
    """解除一条预警；重复解除保留原解除时间，不影响设备或故障记录。"""
    if (isinstance(alarm_id, bool) or not isinstance(alarm_id, int)
            or not 1 <= alarm_id <= 2**63 - 1):
        raise ValueError("预警id必须为SQLite整数范围内的正整数")
    with get_connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM alarm_records WHERE id = ?", (alarm_id,)).fetchone()
        if row is None:
            raise AlarmNotFoundError(f"预警不存在：{alarm_id}")
        if row["status"] != "已解除":
            connection.execute(
                "UPDATE alarm_records SET status = ?, resolved_at = ? WHERE id = ?",
                ("已解除", utc_now(), alarm_id),
            )
        return dict(connection.execute("SELECT * FROM alarm_records WHERE id = ?", (alarm_id,)).fetchone())


def confirm_alarm(alarm_id, *, db_path=DEFAULT_DB_PATH):
    """Acknowledge an active condition without clearing it or creating another event."""
    if isinstance(alarm_id, bool) or not isinstance(alarm_id, int) or not 1 <= alarm_id <= 2**63 - 1:
        raise ValueError("预警id必须为SQLite整数范围内的正整数")
    with get_connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM alarm_records WHERE id=?", (alarm_id,)).fetchone()
        if row is None:
            raise AlarmNotFoundError(f"预警不存在：{alarm_id}")
        if row["status"] == "已解除":
            raise ValueError("该预警已解除，无需确认")
        connection.execute("UPDATE alarm_records SET status='已确认' WHERE id=?", (alarm_id,))
        return dict(connection.execute("SELECT * FROM alarm_records WHERE id=?", (alarm_id,)).fetchone())
