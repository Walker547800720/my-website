"""Reproduce course-design acceptance conditions in a NEW, isolated database.

Run: python -m simulation.verify_core --output docs/core_acceptance
Uses a controlled clock to advance the model, not wall-clock waiting or real hardware.
"""

import argparse
import csv
import json
from pathlib import Path

from database.db import get_connection, initialize_database
from services.alarm_service import check_alarms, list_alarms
from services.equipment_service import get_equipment
from services.fault_service import create_fault, update_fault_status
from services.maintenance_service import complete_maintenance, get_maintenance_plan
from services.telemetry_service import get_telemetry_history
from simulation.welding_simulator import WeldingSimulator


def verify(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    db_path = output / "acceptance.db"
    if db_path.exists():
        raise FileExistsError("验收数据库已存在，请指定新的输出目录；不覆盖已有记录。")
    initialize_database(db_path)
    now = 0.0
    model = WeldingSimulator("WELD-001", db_path=db_path, clock=lambda: now)
    rows = []

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    def tick(count=1):
        nonlocal now
        result = None
        for _ in range(count):
            now += 2
            result = model.step()
        return result

    def checkpoint(name, runtime, remaining, status):
        device = get_equipment("WELD-001", db_path=db_path)
        plan = get_maintenance_plan("WELD-001", db_path=db_path)
        require(device["total_runtime_hours"] == runtime, f"{name}: 工时不符")
        require(plan["remaining_runtime_hours"] == remaining, f"{name}: 剩余工时不符")
        require(plan["maintenance_status"] == status, f"{name}: 维护状态不符")
        active = check_alarms("WELD-001", db_path=db_path)
        levels = {a["alarm_type"]: a["level"] for a in active}
        expected_level = {"正常": None, "即将维护": "警告", "维护到期": "严重"}[status]
        require(levels.get("维护预警") == expected_level, f"{name}: 维护预警等级不符")
        rows.append({"工况": name, "累计运行_h": runtime, "温度_C": round(device["temperature_c"], 3),
                     "下次维护目标_h": plan["next_maintenance_runtime_hours"], "剩余工时_h": remaining,
                     "维护状态": status, "维护预警": levels.get("维护预警", "无"),
                     "温度预警": levels.get("温度预警", "无"), "设备状态": device["status"], "结果": "通过"})

    checkpoint("初始正常", 75, 25, "正常")
    model.start()
    tick(14)
    checkpoint("提前提醒前一小时", 89, 11, "正常")
    tick()
    checkpoint("进入提前10小时窗口", 90, 10, "即将维护")
    alarm_id = next(a["id"] for a in list_alarms("WELD-001", db_path=db_path) if a["alarm_type"] == "维护预警")
    tick(9)
    checkpoint("到期前一小时", 99, 1, "即将维护")
    tick()
    checkpoint("恰好维护到期", 100, 0, "维护到期")
    require(next(a["id"] for a in list_alarms("WELD-001", db_path=db_path) if a["status"] != "已解除") == alarm_id,
            "持续维护异常应升级同一事件，而不是重复建单")
    tick(5)
    checkpoint("维护超期5小时", 105, -5, "维护到期")
    model.pause()
    before = get_equipment("WELD-001", db_path=db_path)
    history_count = len(get_telemetry_history("WELD-001", db_path=db_path))
    require(tick(5) is None, "暂停不应生成数据")
    require(get_equipment("WELD-001", db_path=db_path) == before, "暂停应冻结设备数据")
    require(len(get_telemetry_history("WELD-001", db_path=db_path)) == history_count, "暂停期间新增了快照")
    checkpoint("暂停保持数据", 105, -5, "维护到期")
    model.stop()
    cooled = tick()["telemetry"]
    require(cooled["runtime_increment"] == 0 and cooled["temperature_c"] < before["temperature_c"], "停机应零工时冷却")
    require(cooled["welding_count"] == before["welding_count"], "停机不能增加焊接次数")
    checkpoint("停机冷却", 105, -5, "维护到期")
    maintenance = complete_maintenance("WELD-001", "周期维护", "验收：检查焊枪、送丝与冷却系统", "验收记录", db_path=db_path)
    require(maintenance["runtime_hours"] == 105, "维护基准应为实际完成时累计工时")
    checkpoint("维护完成_105加100等于205", 105, 100, "正常")
    require(get_equipment("WELD-001", db_path=db_path)["welding_count"] == before["welding_count"], "维护不可清零累计产量")
    model.start()
    tick()
    checkpoint("下一周期继续运行", 106, 99, "正常")
    model.set_mode("temperature_anomaly")
    seen = set()
    for _ in range(10):
        seen.update(a["level"] for a in tick()["alarms"] if a["alarm_type"] == "温度预警")
    require(seen == {"警告", "严重"}, "温度异常必须先后触发警告与严重")
    checkpoint("温度异常升级", 116, 89, "正常")
    temperature_events = [a for a in list_alarms("WELD-001", db_path=db_path) if a["alarm_type"] == "温度预警"]
    require(len(temperature_events) == 1, "同一温度异常不应重复创建事件")
    model.set_mode("normal")
    tick(10)
    checkpoint("恢复正常场景_温度报警解除", 126, 79, "正常")
    require(all(a["status"] == "已解除" for a in list_alarms("WELD-001", db_path=db_path) if a["alarm_type"] == "温度预警"), "温度恢复后报警应解除")
    fault = create_fault("WELD-001", "设备运行异常", "严重", "验收：送丝机构异常", db_path=db_path)
    tick()
    checkpoint("故障状态冻结工时", 126, 79, "正常")
    try:
        model.start()
    except ValueError:
        pass
    else:
        raise AssertionError("未处理故障应阻止启动")
    update_fault_status(fault["id"], "处理中", "检查送丝机构", db_path=db_path)
    resolved = update_fault_status(fault["id"], "已处理", "处理完成并复测", db_path=db_path)
    require(resolved["description"] == fault["description"] and "检查送丝机构" in resolved["handling_note"], "故障原始描述和处理历史应保留")
    checkpoint("故障处理完成_设备待启动", 126, 79, "正常")
    require(get_equipment("WELD-001", db_path=db_path)["status"] == "已停止", "故障处理后不能自动运行")
    with get_connection(db_path) as connection:
        require(connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "验收库完整性失败")
        require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "验收库外键异常")
        require(connection.execute("SELECT count(*) FROM maintenance_records").fetchone()[0] == 1, "维护历史记录数量不符")
    with (output / "测试工况记录表.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    result = {"success": True, "isolated_database": str(db_path), "clock": "受控时钟，每次推进2秒，不等待现实时间；所有数据为课程仿真",
              "checkpoints": rows}
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"通过 {len(rows)} 个工况；输出目录：{output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    verify(parser.parse_args().output)
