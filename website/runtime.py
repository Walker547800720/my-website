"""One background driver per database in a Streamlit server process.

The worker never calls Streamlit APIs. Browser reruns only render snapshots.
"""

import atexit
import os
from pathlib import Path
import sqlite3
from threading import Event, RLock, Thread
from time import monotonic

from database.db import utc_now
from services.alarm_service import check_alarms
from simulation.welding_simulator import DEMO_PROFILES
from website.simulation_manager import get_simulation_manager


class SimulationRuntime:
    def __init__(self, manager, poll_seconds=0.2, sync_seconds=1.0):
        self.manager = manager
        self.poll_seconds = poll_seconds
        self.sync_seconds = sync_seconds
        self._stop = Event()
        self._lock = RLock()
        self._thread = None
        self._errors = {}
        self._last_sync = None
        self._sync_at = 0.0

    def synchronize(self):
        """Synchronize initial conditions before returning the first page."""
        with self._lock:
            try:
                check_alarms(db_path=self.manager.db_path)
            except (ValueError, LookupError, OSError, sqlite3.Error) as error:
                self._errors["预警同步"] = str(error)
            else:
                self._errors.pop("预警同步", None)
                self._last_sync = utc_now()
            self._sync_at = monotonic()

    def tick(self):
        with self._lock:
            for equipment_id in DEMO_PROFILES:
                try:
                    report = self.manager.step(equipment_id)
                    error = report["step_error"] or report["sync_error"]
                    if error:
                        self._errors[equipment_id] = error
                    else:
                        self._errors.pop(equipment_id, None)
                except Exception as error:
                    self._errors[equipment_id] = str(error)
            if monotonic() - self._sync_at >= self.sync_seconds:
                self.synchronize()

    def _run(self):
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.poll_seconds)

    def start(self):
        with self._lock:
            if self._thread is not None:
                return
            # This is intentionally synchronous: no initial warning-less frame.
            self.synchronize()
            self._thread = Thread(target=self._run, name="weld-runtime-" + Path(self.manager.db_path).parent.name,
                                  daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        thread = self._thread
        if thread:
            thread.join(timeout=12)
            if thread.is_alive():
                raise RuntimeError("后台仿真线程未能退出")

    def status(self):
        with self._lock:
            return {"running": bool(self._thread and self._thread.is_alive() and not self._stop.is_set()),
                    "last_alarm_sync": self._last_sync, "errors": dict(self._errors)}


_registry = {}
_registry_lock = RLock()


def get_runtime(db_path):
    key = os.path.normcase(str(Path(db_path).resolve()))
    with _registry_lock:
        if key not in _registry:
            runtime = SimulationRuntime(get_simulation_manager(key))
            runtime.start()
            _registry[key] = runtime
        return _registry[key]


def suspend_workspace(db_path):
    """Archive an old demonstration by freezing every model, without deleting data."""
    key = os.path.normcase(str(Path(db_path).resolve()))
    with _registry_lock:
        runtime = _registry.pop(key, None)
        if runtime:
            for equipment_id in DEMO_PROFILES:
                runtime.manager.command(equipment_id, "pause")
            runtime.close()


def stop_all_runtimes():
    with _registry_lock:
        runtimes = list(_registry.values())
        _registry.clear()
    for runtime in runtimes:
        runtime.close()


atexit.register(stop_all_runtimes)
