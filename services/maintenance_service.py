"""维护排程，时间单位为小时；查询返回dict/列表，派生值不入库。

提醒窗口使用设备计划 reminder_lead_hours（初始10h）；到期优先于即将维护。
完成维护基于当前累计时间和设备计划周期重新排程，不修改设备或故障记录。
完成登记要求设备已停止且没有未完成故障，状态约束与写入在同一事务检查。
"""

import math

from database.db import DEFAULT_DB_PATH, get_connection, utc_now
from services.equipment_service import EquipmentNotFoundError


MAINTENANCE_ALARM_TYPE = "维护预警"


class MaintenancePlanNotFoundError(LookupError):
    """设备没有维护计划。"""


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}必须为非空字符串")
    return value.strip()


def _load_plan(connection, equipment_id):
    equipment = connection.execute(
        "SELECT * FROM equipment WHERE equipment_id = ?", (equipment_id,)
    ).fetchone()
    if equipment is None:
        raise EquipmentNotFoundError(f"设备不存在：{equipment_id}")
    plan = connection.execute(
        "SELECT * FROM maintenance_plans WHERE equipment_id = ?", (equipment_id,)
    ).fetchone()
    if plan is None:
        raise MaintenancePlanNotFoundError(f"设备没有维护计划：{equipment_id}")
    return {**dict(plan), "total_runtime_hours": equipment["total_runtime_hours"],
            "equipment_status": equipment["status"]}


def _plan_view(plan):
    remaining = plan["next_maintenance_runtime_hours"] - plan["total_runtime_hours"]
    if remaining <= 0:
        status = "维护到期"
    elif remaining <= plan["reminder_lead_hours"]:
        status = "即将维护"
    else:
        status = "正常"
    return {**dict(plan), "remaining_runtime_hours": remaining, "maintenance_status": status}


def get_maintenance_plan(equipment_id, *, db_path=DEFAULT_DB_PATH):
    """查询设备计划及实时剩余时间、维护状态；设备/计划不存在时抛异常。"""
    equipment_id = _text(equipment_id, "设备编号")
    with get_connection(db_path) as connection:
        connection.execute("BEGIN")
        return _plan_view(_load_plan(connection, equipment_id))


def list_maintenance_plans(*, db_path=DEFAULT_DB_PATH):
    """按设备编号查询全部已有计划及派生状态。"""
    with get_connection(db_path) as connection:
        return [_plan_view(row) for row in connection.execute(
            """SELECT p.*, e.total_runtime_hours FROM maintenance_plans p
               JOIN equipment e USING (equipment_id) ORDER BY equipment_id"""
        )]


def calculate_remaining_hours(equipment_id, *, db_path=DEFAULT_DB_PATH):
    return get_maintenance_plan(equipment_id, db_path=db_path)["remaining_runtime_hours"]


def get_maintenance_status(equipment_id, *, db_path=DEFAULT_DB_PATH):
    return get_maintenance_plan(equipment_id, db_path=db_path)["maintenance_status"]


def complete_maintenance(equipment_id, maintenance_type, description, performed_by,
                         *, db_path=DEFAULT_DB_PATH):
    """同一事务保存维护记录、推进计划并解除维护预警，返回新维护记录dict。"""
    equipment_id = _text(equipment_id, "设备编号")
    maintenance_type = _text(maintenance_type, "维护类型")
    description = _text(description, "维护说明")
    performed_by = _text(performed_by, "维护人员")
    with get_connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        plan = _load_plan(connection, equipment_id)
        if plan["equipment_status"] != "已停止":
            raise ValueError("请先停机，并处理未完成故障后，再登记维护完成")
        if connection.execute(
                "SELECT 1 FROM fault_records WHERE equipment_id=? AND status!='已处理' LIMIT 1",
                (equipment_id,)).fetchone():
            raise ValueError("设备仍有未完成故障，请处理后再登记维护完成")
        runtime = plan["total_runtime_hours"]
        target = runtime + plan["maintenance_interval_hours"]
        if not math.isfinite(target) or target <= runtime:
            raise ValueError("下一次维护目标超出有效数值范围")
        timestamp = utc_now()
        cursor = connection.execute(
            """INSERT INTO maintenance_records
               (equipment_id, maintenance_type, description, runtime_hours, performed_by, performed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (equipment_id, maintenance_type, description, runtime, performed_by, timestamp),
        )
        connection.execute(
            """UPDATE maintenance_plans SET last_maintenance_runtime_hours = ?,
                      next_maintenance_runtime_hours = ?, updated_at = ? WHERE equipment_id = ?""",
            (runtime, target, timestamp, equipment_id),
        )
        connection.execute(
            """UPDATE alarm_records SET status = ?, resolved_at = ?
               WHERE equipment_id = ? AND alarm_type = ? AND status IN (?, ?)""",
            ("已解除", timestamp, equipment_id, MAINTENANCE_ALARM_TYPE, "未处理", "已确认"),
        )
        return dict(connection.execute(
            "SELECT * FROM maintenance_records WHERE id = ?", (cursor.lastrowid,)
        ).fetchone())
