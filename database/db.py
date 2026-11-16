"""Connections and repeatable initialization; standard library only."""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "maintenance.db"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

INITIAL_EQUIPMENT = (
    ("WELD-001", 75.0, 65.0, 1250),
    ("WELD-002", 92.0, 68.0, 1580),
    ("WELD-003", 45.0, 62.0, 820),
)
INITIAL_CONFIG = (
    ("temperature_warning_c", 80.0, "℃", "温度预警阈值（模拟参数）"),
    ("temperature_critical_c", 90.0, "℃", "温度严重阈值（模拟参数）"),
    ("maintenance_interval_hours", 100.0, "h", "默认维护周期（模拟参数）"),
    ("reminder_lead_hours", 10.0, "h", "默认提前提醒时间（模拟参数）"),
    ("simulation_step_minutes", 1.0, "min", "仿真步长（模拟参数，仅存储配置）"),
)


def utc_now():
    """Return an ISO 8601 UTC timestamp with a consistent precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@contextmanager
def get_connection(db_path=DEFAULT_DB_PATH):
    """Commit on success, roll back on error, and always close the connection."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(db_path=DEFAULT_DB_PATH):
    """Create schema and missing seeds atomically without resetting existing data."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    timestamp = utc_now()
    with get_connection(db_path) as connection:
        # Explicit BEGIN includes DDL in the same transaction as seed data.
        connection.execute("BEGIN IMMEDIATE")
        statement = ""
        for line in schema.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                connection.execute(statement)
                statement = ""
        if statement.strip():
            raise ValueError("Incomplete schema SQL")

        for equipment_id, runtime, temperature, count in INITIAL_EQUIPMENT:
            cursor = connection.execute(
                """INSERT INTO equipment
                   (equipment_id, name, total_runtime_hours, temperature_c,
                    welding_count, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(equipment_id) DO NOTHING""",
                (equipment_id, f"自动化焊接设备 {equipment_id}", runtime,
                 temperature, count, "已停止", timestamp, timestamp),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """INSERT INTO telemetry
                       (equipment_id, temperature_c, status, total_runtime_hours,
                        welding_count, runtime_increment, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (equipment_id, temperature, "已停止", runtime, count, 0.0, timestamp),
                )
            connection.execute(
                """INSERT INTO maintenance_plans
                   (equipment_id, maintenance_interval_hours,
                    last_maintenance_runtime_hours, next_maintenance_runtime_hours,
                    reminder_lead_hours, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(equipment_id) DO NOTHING""",
                (equipment_id, 100.0, 0.0, 100.0, 10.0, timestamp),
            )
        connection.executemany(
            """INSERT INTO system_config
               (config_key, config_value, unit, description, is_simulated, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(config_key) DO NOTHING""",
            [(*config, 1, timestamp) for config in INITIAL_CONFIG],
        )
    return Path(db_path).resolve()
