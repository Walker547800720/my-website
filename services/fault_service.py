"""故障管理；复用现有字段、连接和事务，不初始化或迁移数据库。

处理说明以带UTC时间、处理状态的文本追加到 handling_note，description 保持不变。
event_key 可为空；非空时全表唯一。同设备、同类型的相同事件键返回已有
记录且不修改设备状态；其他设备/类型占用相同键时抛 DuplicateFaultError。
查询返回 dict 列表，新增/更新返回 dict；所有函数支持 db_path 关键字。
"""

from database.db import DEFAULT_DB_PATH, get_connection, utc_now
from services.equipment_service import EquipmentNotFoundError


FAULT_TYPES = ("温度异常", "设备运行异常")
FAULT_LEVELS = ("提示", "警告", "严重")
FAULT_STATUSES = ("未处理", "处理中", "已处理")


class DuplicateFaultError(ValueError):
    """事件键已被其他设备或故障类型使用。"""


class FaultNotFoundError(LookupError):
    """需要更新的故障不存在。"""


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}必须为非空字符串")
    return value.strip()


def _require_equipment(connection, equipment_id):
    if connection.execute(
        "SELECT equipment_id FROM equipment WHERE equipment_id = ?", (equipment_id,)
    ).fetchone() is None:
        raise EquipmentNotFoundError(f"设备不存在：{equipment_id}")


def _get_fault(connection, fault_id):
    return connection.execute(
        "SELECT * FROM fault_records WHERE id = ?", (fault_id,)
    ).fetchone()


def create_fault(equipment_id, fault_type, level, description, *, event_key=None,
                 db_path=DEFAULT_DB_PATH):
    """原子写入未处理故障并将设备设为故障；事件键重试保留首次记录。"""
    equipment_id = _text(equipment_id, "设备编号")
    fault_type = _text(fault_type, "故障类型")
    level = _text(level, "故障等级")
    description = _text(description, "故障描述")
    if fault_type not in FAULT_TYPES:
        raise ValueError("故障类型仅支持：温度异常、设备运行异常")
    if level not in FAULT_LEVELS:
        raise ValueError("故障等级仅支持：提示、警告、严重")
    if event_key is not None:
        event_key = _text(event_key, "事件键")
    with get_connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_equipment(connection, equipment_id)
        if event_key is not None:
            existing = connection.execute(
                "SELECT * FROM fault_records WHERE event_key = ?", (event_key,)
            ).fetchone()
            if existing is not None:
                if (existing["equipment_id"] != equipment_id
                        or existing["fault_type"] != fault_type):
                    raise DuplicateFaultError("事件键已被其他设备或故障类型使用")
                return dict(existing)
        timestamp = utc_now()
        cursor = connection.execute(
            """INSERT INTO fault_records
               (equipment_id, fault_type, level, description, status,
                occurred_at, resolved_at, event_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (equipment_id, fault_type, level, description, "未处理", timestamp, None, event_key),
        )
        connection.execute(
            "UPDATE equipment SET status = ?, updated_at = ? WHERE equipment_id = ?",
            ("故障", timestamp, equipment_id),
        )
        return dict(_get_fault(connection, cursor.lastrowid))


def list_faults(*, db_path=DEFAULT_DB_PATH):
    """查询全部故障，按发生时间及id倒序。"""
    with get_connection(db_path) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM fault_records ORDER BY occurred_at DESC, id DESC"
        )]


def get_faults_by_equipment(equipment_id, *, db_path=DEFAULT_DB_PATH):
    """按设备查询；设备不存在抛 EquipmentNotFoundError，无故障返回空列表。"""
    equipment_id = _text(equipment_id, "设备编号")
    with get_connection(db_path) as connection:
        _require_equipment(connection, equipment_id)
        return [dict(row) for row in connection.execute(
            """SELECT * FROM fault_records WHERE equipment_id = ?
               ORDER BY occurred_at DESC, id DESC""", (equipment_id,)
        )]


def update_fault_status(fault_id, status, handling_note, *, db_path=DEFAULT_DB_PATH):
    """更新处理状态并追加非空处理说明，同事务联动设备状态。

重新设为未处理/处理中时清空 resolved_at。已处理记录再次补充说明时
保留首次完成时间；任一故障未完成，设备仍为故障，全部完成后为已停止。
"""
    if (isinstance(fault_id, bool) or not isinstance(fault_id, int)
            or not 1 <= fault_id <= 2**63 - 1):
        raise ValueError("故障id必须为SQLite整数范围内的正整数")
    status = _text(status, "处理状态")
    handling_note = _text(handling_note, "处理说明")
    if status not in FAULT_STATUSES:
        raise ValueError("处理状态仅支持：未处理、处理中、已处理")
    with get_connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        fault = _get_fault(connection, fault_id)
        if fault is None:
            raise FaultNotFoundError(f"故障不存在：{fault_id}")
        timestamp = utc_now()
        resolved_at = (fault["resolved_at"] or timestamp) if status == "已处理" else None
        entry = f"[{timestamp}] [{status}] 处理说明：{handling_note}"
        notes = f"{fault['handling_note']}\n{entry}" if fault["handling_note"] else entry
        connection.execute(
            "UPDATE fault_records SET status = ?, handling_note = ?, resolved_at = ? WHERE id = ?",
            (status, notes, resolved_at, fault_id),
        )
        pending = connection.execute(
            """SELECT 1 FROM fault_records WHERE equipment_id = ?
               AND status IN (?, ?) LIMIT 1""",
            (fault["equipment_id"], "未处理", "处理中"),
        ).fetchone()
        connection.execute(
            "UPDATE equipment SET status = ?, updated_at = ? WHERE equipment_id = ?",
            ("故障" if pending is not None else "已停止", timestamp, fault["equipment_id"]),
        )
        return dict(_get_fault(connection, fault_id))
