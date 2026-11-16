"""三台焊接设备的确定性课程演示模型，所有物理/产量参数均为模拟值。

每个实例负责一台设备，调用者显式指定数据库。无线程、循环、sleep或自动
初始化；调用 start 后每满2秒 step 最多计算1小时，延迟调用不追补遗漏步。
初始为 paused（不写数据）；pause 冻结模型，不改变数据库设备状态；stop
通过设备服务停止运行设备，此后到时 step 继续生成零增量冷却快照。
故障/离线状态不被启动或停止覆盖，暂停后 start 恢复并重新计时。
"""

import math
from pathlib import Path
import sqlite3
from threading import RLock
from time import monotonic

from database.db import get_connection
from services.equipment_service import (
    ConfigurationError, EquipmentNotFoundError, get_equipment, update_equipment_status,
)
from services.fault_service import get_faults_by_equipment
from services.telemetry_service import save_telemetry
from services.maintenance_service import get_maintenance_plan
from services.alarm_service import check_alarms


# 仅供演示：每模拟小时固定焊接次数，以及正常温度低于预警阈值的差值。
DEMO_PROFILES = {
    "WELD-001": (20, 12.0),
    "WELD-002": (24, 10.0),
    "WELD-003": (18, 14.0),
}
STEP_SECONDS = 2.0
HOURS_PER_STEP = 1.0
DEMO_AMBIENT_C = 25.0
DEMO_RESPONSE = 0.25
DEMO_ANOMALY_MARGIN_C = 5.0
DEMO_FALLBACK_THRESHOLDS = {"temperature_warning_c": 80.0, "temperature_critical_c": 90.0}
MODES = ("normal", "temperature_anomaly")


class SimulationSyncError(RuntimeError):
    """快照已提交，后续维护/预警同步失败；本步已消耗，不能重复累计。"""

    def __init__(self, telemetry, cause):
        super().__init__(f"仿真快照已保存，但维护/预警同步失败：{cause}")
        self.telemetry = telemetry


class WeldingSimulator:
    def __init__(self, equipment_id, *, db_path, clock=monotonic):
        if not isinstance(equipment_id, str) or equipment_id.strip() not in DEMO_PROFILES:
            raise ValueError("仿真模型仅支持 WELD-001、WELD-002、WELD-003")
        if not Path(db_path).is_file():
            raise FileNotFoundError(f"仿真数据库不存在：{db_path}")
        if not callable(clock):
            raise ValueError("clock必须为可调用的单调时钟")
        self.equipment_id = equipment_id.strip()
        self.db_path = Path(db_path)
        self._clock = clock
        self._lock = RLock()
        self._state = "paused"
        self._mode = "normal"
        self._next_step_at = None
        self._equipment()
        get_maintenance_plan(self.equipment_id, db_path=self.db_path)

    def _equipment(self):
        equipment = get_equipment(self.equipment_id, db_path=self.db_path)
        if equipment is None:
            raise EquipmentNotFoundError(f"设备不存在：{self.equipment_id}")
        return equipment

    def _thresholds(self):
        with get_connection(self.db_path) as connection:
            rows = connection.execute(
                """SELECT config_key, config_value, unit FROM system_config
                   WHERE config_key IN (?, ?)""", tuple(DEMO_FALLBACK_THRESHOLDS),
            ).fetchall()
        configs = {row["config_key"]: row for row in rows}
        values, missing = {}, []
        for key, fallback in DEMO_FALLBACK_THRESHOLDS.items():
            if key not in configs:
                values[key] = fallback
                missing.append(key)
            else:
                value = configs[key]["config_value"]
                if (configs[key]["unit"] != "℃" or not isinstance(value, (int, float))
                        or not math.isfinite(value)):
                    raise ConfigurationError("仿真温度配置必须为有限的摄氏度数值")
                values[key] = float(value)
        if not 0 < values["temperature_warning_c"] < values["temperature_critical_c"]:
            raise ConfigurationError("仿真温度配置须满足0<预警阈值<严重阈值")
        return values, missing

    def get_state(self):
        """模型状态仅在内存中，不增加数据库状态枚举。"""
        with self._lock:
            return {"equipment_id": self.equipment_id, "state": self._state, "mode": self._mode,
                    "step_seconds": STEP_SECONDS, "hours_per_step": HOURS_PER_STEP,
                    "welding_per_hour": DEMO_PROFILES[self.equipment_id][0],
                    "parameter_source": "课程仿真演示参数，不代表真实设备规格"}

    def set_mode(self, mode):
        with self._lock:
            if mode not in MODES:
                raise ValueError("mode仅支持normal或temperature_anomaly")
            self._mode = mode
            return self.get_state()

    def start(self, mode=None):
        """启动/恢复当前设备；拒绝故障、离线和仍有未完成故障的设备。"""
        with self._lock:
            if mode is not None and mode not in MODES:
                raise ValueError("mode仅支持normal或temperature_anomaly")
            equipment = self._equipment()
            self._thresholds()  # 配置不合法时，在状态写入前拒绝。
            if equipment["status"] not in ("已停止", "运行中"):
                raise ValueError("故障或离线设备不能由仿真启动")
            faults = get_faults_by_equipment(self.equipment_id, db_path=self.db_path)
            if any(row["status"] != "已处理" for row in faults):
                raise ValueError("设备仍有未完成故障，不能由仿真启动")
            if equipment["status"] == "已停止":
                update_equipment_status(self.equipment_id, "运行中", db_path=self.db_path)
            if mode is not None:
                self._mode = mode
            if self._state != "running":
                self._next_step_at = self._clock() + STEP_SECONDS
            self._state = "running"
            return self.get_state()

    def pause(self):
        """暂停后不产生快照，不累计时间，也不进行冷却计算。"""
        with self._lock:
            self._state = "paused"
            self._next_step_at = None
            return self.get_state()

    def stop(self):
        """停止当前运行设备；保留外部故障/离线状态，之后允许单步冷却。"""
        with self._lock:
            if self._equipment()["status"] == "运行中":
                update_equipment_status(self.equipment_id, "已停止", db_path=self.db_path)
            if self._state != "stopped":
                self._next_step_at = self._clock() + STEP_SECONDS
            self._state = "stopped"
            return self.get_state()

    def step(self):
        """未到2秒或暂停时返回None，否则保存一步并返回快照、维护和预警结果。

        只在缺配置时用演示阈值，且明确跳过原服务无法执行的预警同步。
        各既有服务分别管理事务；快照成功后若同步失败，抛带快照的
        SimulationSyncError，不回滚已提交快照，也不重复消耗同一个节拍。
        """
        with self._lock:
            now = self._clock()
            if self._state == "paused" or now < self._next_step_at:
                return None
            equipment = self._equipment()
            thresholds, missing = self._thresholds()
            get_maintenance_plan(self.equipment_id, db_path=self.db_path)
            running = equipment["status"] == "运行中"
            current = equipment["temperature_c"]
            if running:
                target = (thresholds["temperature_critical_c"] + DEMO_ANOMALY_MARGIN_C
                          if self._mode == "temperature_anomaly" else
                          thresholds["temperature_warning_c"] - DEMO_PROFILES[self.equipment_id][1])
            else:
                target = min(current, DEMO_AMBIENT_C)
            temperature = current + DEMO_RESPONSE * (target - current)
            telemetry = save_telemetry(
                self.equipment_id, temperature,
                runtime_increment=HOURS_PER_STEP if running else 0,
                welding_count_increment=DEMO_PROFILES[self.equipment_id][0] if running else 0,
                db_path=self.db_path,
            )
            self._next_step_at = now + STEP_SECONDS
            try:
                maintenance = get_maintenance_plan(self.equipment_id, db_path=self.db_path)
                alarms = None if missing else check_alarms(self.equipment_id, db_path=self.db_path)
            except (ValueError, LookupError, sqlite3.Error) as error:
                raise SimulationSyncError(telemetry, error) from error
            return {"telemetry": telemetry, "maintenance": maintenance, "alarms": alarms,
                    "thresholds": thresholds,
                    "threshold_source": "仿真演示参数（部分或全部配置缺失）" if missing else "system_config",
                    "alarm_sync_skipped": bool(missing),
                    "warning": ("缺少" + ", ".join(missing) +
                                "；温度计算使用演示阈值，原预警服务须有完整配置，本步跳过预警同步。") if missing else None}
