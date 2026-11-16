"""Create independent, retained demonstration rounds; never reset the base database."""

from datetime import datetime
import json
from pathlib import Path
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

from database.db import initialize_database
from services.alarm_service import check_alarms


ROUND_ID = re.compile(r"round_\d{8}_\d{6}_[0-9a-f]{8}\Z")


def demo_path(root, round_id):
    root = Path(root).resolve()
    if not isinstance(round_id, str) or not ROUND_ID.fullmatch(round_id):
        raise ValueError("无效演示编号")
    directory = (root / round_id).resolve()
    if directory.parent != root:
        raise ValueError("演示路径超出数据目录")
    return directory / "maintenance.db"


def create_demo(root):
    root = Path(root).resolve()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    round_id = "round_" + now.strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:8]
    db_path = demo_path(root, round_id)
    db_path.parent.mkdir(parents=True, exist_ok=False)
    initialize_database(db_path)
    # A round is listed only after initial data AND alarms have been committed.
    check_alarms(db_path=db_path)
    info = {"id": round_id, "created_at": now.isoformat(timespec="seconds"),
            "label": now.strftime("%m-%d %H:%M:%S") + " · " + round_id[-4:],
            "initial_runtime_hours": [75, 92, 45], "maintenance_cycle_hours": 100,
            "reminder_lead_hours": 10}
    (db_path.parent / "round.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return info


def list_demos(root):
    root = Path(root).resolve()
    if not root.exists():
        return []
    rounds = []
    for directory in root.iterdir():
        if not directory.is_dir() or not ROUND_ID.fullmatch(directory.name):
            continue
        try:
            path = demo_path(root, directory.name)
            info = json.loads((path.parent / "round.json").read_text(encoding="utf-8"))
            if info["id"] == directory.name and path.is_file():
                rounds.append(info)
        except (ValueError, OSError, KeyError):
            continue
    return sorted(rounds, key=lambda item: item["id"], reverse=True)
