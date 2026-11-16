"""Isolated single-device demonstrations. Existing production data is never opened.

The database stores authoritative samples; session.json only stores lifecycle and
mode annotations. The existing simulator and services own all business rules.
"""

import atexit
from datetime import datetime
import json
import os
from pathlib import Path
import re
from threading import Event, RLock, Thread
from time import monotonic
from uuid import uuid4

from database.db import INITIAL_CONFIG, SCHEMA_PATH, get_connection, utc_now
from services.equipment_service import create_equipment
from services.telemetry_service import save_telemetry
from website.simulation_manager import SimulationManager


DEVICE_ID = "WELD-001"
SESSION_ID = re.compile(r"demo_\d{8}_\d{6}_[0-9a-f]{8}\Z")
LEASE_SECONDS = 12.0
MAX_ACTIVE_SECONDS = 240.0


def session_path(root, session_id):
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise ValueError("无效演示编号")
    return Path(root).resolve() / session_id / "maintenance.db"


def read_info(db_path):
    return json.loads(Path(db_path).with_name("session.json").read_text(encoding="utf-8"))


def _write_info(db_path, info):
    path = Path(db_path).with_name("session.json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def create_session(root):
    session_id = "demo_" + datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:8]
    db_path = session_path(root, session_id)
    db_path.parent.mkdir(parents=True, exist_ok=False)
    # Fresh database only: do not call the legacy three-device initializer.
    with get_connection(db_path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.executemany(
            "INSERT INTO system_config VALUES (?, ?, ?, ?, ?, ?)",
            [(*row, 1, utc_now()) for row in INITIAL_CONFIG],
        )
    create_equipment(DEVICE_ID, "自动化焊接设备", temperature_c=25.0, db_path=db_path)
    save_telemetry(DEVICE_ID, 25.0, db_path=db_path)
    _write_info(db_path, {"id": session_id, "created_at": utc_now(), "ended": False,
                          "events": [], "reason": "尚未开始", "boundaries": []})
    return db_path


class DefenseSession:
    """One worker, one simulator, browser lease; ended sessions are read-only."""

    def __init__(self, db_path, *, clock=monotonic, lease_seconds=LEASE_SECONDS, manager=None):
        self.db_path = Path(db_path).resolve()
        self.manager = manager or SimulationManager(self.db_path)
        self._clock = clock
        self._lease = lease_seconds
        self._seen = clock()
        self._started = clock()
        self._lock = RLock()
        self._stop = Event()
        self._thread = None
        self.info = read_info(self.db_path)
        # Reopening/restarting never resumes calculation automatically.
        self.manager.get_model(DEVICE_ID)
        if not self.info["ended"]:
            self.info["reason"] = "尚未开始" if not self.info["events"] else "演示已暂停，可继续运行"

    def heartbeat(self):
        with self._lock:
            self._seen = self._clock()

    def _event(self, action, **fields):
        self.info["events"].append({"action": action, "at": utc_now(), **fields})
        _write_info(self.db_path, self.info)

    def command(self, action, mode=None):
        with self._lock:
            if self.info["ended"]:
                return False
            if not self.manager.command(DEVICE_ID, action, mode=mode):
                return False
            self._seen = self._clock()
            if action in ("start", "stop"):
                self._started = self._clock()
            self.info["reason"] = {"start": "运行中", "pause": "已暂停讲解",
                                    "stop": "已停机，正在冷却", "set_mode": "场景已切换"}[action]
            # Record the boundary sample ID, avoiding guessed historical modes.
            with get_connection(self.db_path) as connection:
                last_id = connection.execute("SELECT max(id) FROM telemetry").fetchone()[0]
            self._event(action, after_sample_id=last_id, mode=self.manager.get_report(DEVICE_ID)["state"]["mode"])
            return True

    def finish(self):
        with self._lock:
            if self.info["ended"]:
                return True
            if not self.manager.command(DEVICE_ID, "stop"):
                # Pause the model directly so the failed stop's error is retained.
                self.manager.get_model(DEVICE_ID).pause()
                error = self.manager.get_report(DEVICE_ID)["control_error"]
                self.info["reason"] = f"结束失败，已暂停计算，请排除停机错误后重试：{error}"
                self._event("finish_failed", error=error)
                return False
            if not self.manager.command(DEVICE_ID, "pause"):
                error = self.manager.get_report(DEVICE_ID)["control_error"]
                self.info["reason"] = f"结束失败，请重试：{error}"
                self._event("finish_failed", error=error)
                return False
            self.info.update(ended=True, reason="本轮已结束，数据已冻结")
            self._event("finish")
            self._stop.set()
            return True

    def perform(self, operation):
        """Serialize form writes with finishing/ticks, including other browsers."""
        with self._lock:
            if self.info["ended"]:
                raise ValueError("本轮已结束，数据已冻结，请开始新一轮")
            return operation()

    def tick(self):
        with self._lock:
            if self.info["ended"]:
                return
            model = self.manager.get_model(DEVICE_ID)
            if self._clock() - self._seen >= self._lease:
                if model.get_state()["state"] != "paused":
                    model.pause()
                    self.info["reason"] = "页面已离开，自动暂停；返回后点击继续"
                    self._event("lease_pause")
                return
            if self._clock() - self._started >= MAX_ACTIVE_SECONDS:
                if model.get_state()["state"] != "paused":
                    model.pause()
                    self.info["reason"] = "连续演示达到4分钟，已暂停；确认后可继续"
                    self._event("duration_pause")
                return
            before = self.manager.get_report(DEVICE_ID)["last_snapshot"]
            report = self.manager.step(DEVICE_ID)
            sample = report["last_snapshot"]
            if sample is None or (before and sample["id"] == before["id"]):
                return
            result = report["last_result"]
            if result:
                plan = result["maintenance"]
                # Hold each maintenance threshold once per cycle for explanation.
                state = plan["maintenance_status"]
                boundary = f'{plan["next_maintenance_runtime_hours"]}:{state}'
                if state != "正常" and boundary not in self.info["boundaries"]:
                    self.info["boundaries"].append(boundary)
                    model.pause()
                    self.info["reason"] = f"{state}：已暂停，讲解后可继续或停机维护"
                    self._event("maintenance_hold", sample_id=sample["id"], state=state)
            if sample["status"] != "运行中" and abs(sample["temperature_c"] - 25.0) < 0.1:
                model.pause()
                self.info["reason"] = "冷却已接近环境温度，自动暂停"
                self._event("cooling_complete", sample_id=sample["id"])

    def report(self):
        with self._lock:
            return {**self.manager.get_report(DEVICE_ID), "reason": self.info["reason"],
                    "ended": self.info["ended"], "events": list(self.info["events"])}

    def start_worker(self):
        with self._lock:
            if self._thread or self.info["ended"]:
                return
            def run():
                while not self._stop.wait(0.2):
                    try:
                        self.tick()
                    except Exception as error:
                        with self._lock:
                            self.manager.get_model(DEVICE_ID).pause()
                            self.info["reason"] = f"推进失败，已暂停：{error}"
            self._thread = Thread(target=run, daemon=True, name="defense-" + self.db_path.parent.name)
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=12)
            if self._thread.is_alive():
                raise RuntimeError("演示线程未能退出")


_sessions = {}
_registry_lock = RLock()


def get_session(db_path):
    key = os.path.normcase(str(Path(db_path).resolve()))
    with _registry_lock:
        if key not in _sessions:
            session = DefenseSession(key)
            session.start_worker()
            _sessions[key] = session
        return _sessions[key]


def close_sessions():
    with _registry_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        session.close()


atexit.register(close_sessions)
