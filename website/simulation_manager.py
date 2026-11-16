"""每个数据库一个进程内共享管理器；不创建线程或定时循环。"""

from copy import deepcopy
import os
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4

import streamlit as st

from database.db import utc_now
from services.equipment_service import get_equipment
from simulation.welding_simulator import WeldingSimulator, SimulationSyncError


class SimulationManager:
    def __init__(self, db_path):
        self.db_path = str(Path(db_path).resolve())
        self._lock = RLock()
        self._models = {}
        self._reports = {}

    def get_model(self, equipment_id):
        with self._lock:
            if equipment_id not in self._models:
                self._models[equipment_id] = WeldingSimulator(equipment_id, db_path=self.db_path)
            return self._models[equipment_id]

    def _report(self, equipment_id):
        return self._reports.setdefault(equipment_id, {
            "last_result": None, "last_snapshot": None, "sync_error": None,
            "step_error": None, "control_error": None, "notice": None, "run": None,
        })

    def _begin_run(self, equipment_id):
        device = get_equipment(equipment_id, db_path=self.db_path)
        self._report(equipment_id)["run"] = {
            "id": uuid4().hex, "started_at": utc_now(), "steps": 0, "runtime_hours": 0.0,
            "baseline_runtime_hours": device["total_runtime_hours"],
            "samples": [{"simulated_hour": 0.0, "temperature_c": device["temperature_c"],
                         "total_runtime_hours": device["total_runtime_hours"], "recorded_at": utc_now()}],
        }

    def _record_snapshot(self, equipment_id, telemetry):
        """Track only committed steps, including zero-runtime cooling and sync failures."""
        run = self._report(equipment_id)["run"]
        if run is None:
            return
        run["steps"] += 1
        run["runtime_hours"] += telemetry["runtime_increment"]
        run["samples"].append({**telemetry,
                               "simulated_hour": run["steps"] * self.get_model(equipment_id).get_state()["hours_per_step"]})
        run["samples"] = run["samples"][-200:]

    def command(self, equipment_id, action, mode=None):
        """只转调模型的现有控制接口；返回成功与否，错误保留供所有会话显示。"""
        with self._lock:
            report = self._report(equipment_id)
            try:
                model = self.get_model(equipment_id)
                previous_state = model.get_state()["state"]
                if action == "start":
                    model.start(mode=mode)
                    if report["run"] is None or previous_state == "stopped":
                        self._begin_run(equipment_id)
                        report.update(last_result=None, last_snapshot=None, sync_error=None, step_error=None)
                elif action == "pause":
                    model.pause()
                elif action == "stop":
                    model.stop()
                    if report["run"] is None:
                        self._begin_run(equipment_id)
                elif action == "set_mode":
                    model.set_mode(mode)
                else:
                    raise ValueError("未知仿真控制操作")
            except (ValueError, LookupError, sqlite3.Error, OSError) as error:
                report["control_error"] = str(error)
                report["notice"] = None
                return False
            report["control_error"] = None
            report["notice"] = {"start": "仿真已开始，按演示倍率累计工时。", "pause": "演示已暂停：工时、产量、温度均冻结；设备最后记录保留。",
                                "stop": "设备已停机：不再累计工时和产量，继续模拟冷却。",
                                "set_mode": "仿真模式已更新。"}[action]
            return True

    def step(self, equipment_id):
        """每次刷新调用一次，不重试step；模型负责时间门控和原子快照保存。"""
        with self._lock:
            model = self.get_model(equipment_id)
            report = self._report(equipment_id)
            try:
                result = model.step()
            except SimulationSyncError as error:
                self._record_snapshot(equipment_id, error.telemetry)
                report.update(last_snapshot=error.telemetry, last_result=None,
                              sync_error=f"本次快照已保存，但维护/预警同步失败；未重复提交本步。{error}",
                              step_error=None)
            except (ValueError, LookupError, sqlite3.Error, OSError) as error:
                report["step_error"] = str(error)
            else:
                if result is not None:
                    self._record_snapshot(equipment_id, result["telemetry"])
                    report.update(last_result=result, last_snapshot=result["telemetry"],
                                  sync_error=None, step_error=None)
            return self.get_report(equipment_id)

    def get_report(self, equipment_id):
        with self._lock:
            return {"state": self.get_model(equipment_id).get_state(),
                    **deepcopy(self._report(equipment_id))}


@st.cache_resource(show_spinner=False)
def _cached_manager(canonical_db_path):
    # Recreate cached resources when deploying the run-tracking version; persisted data stays intact.
    manager = SimulationManager(canonical_db_path)
    return manager


def get_simulation_manager(db_path):
    """绝对路径、大小写规范化后作为缓存键；跨页面、会话复用同一实例。"""
    return _cached_manager(os.path.normcase(str(Path(db_path).resolve())))
