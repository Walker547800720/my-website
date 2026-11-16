"""A read-only operational guide derived from saved records, never a scripted demo."""

from dataclasses import dataclass
from html import escape

import pandas as pd
import streamlit as st

from database.db import get_connection
from services.equipment_service import list_equipment
from simulation.welding_simulator import (
    DEMO_PROFILES, STEP_SECONDS, HOURS_PER_STEP, DEMO_AMBIENT_C,
    DEMO_RESPONSE, DEMO_ANOMALY_MARGIN_C,
)
from website.device_context import prepare_device_widget, device_changed
from website.runtime import get_runtime
from website.simulation_view import control_view
from website.ui import badge, kpis, remaining_text, section, status_tone


STAGES = ("运行监测", "提醒判断", "停机处理", "更新计划", "验证与追溯")


@dataclass(frozen=True)
class Decision:
    code: str
    stage: int
    title: str
    detail: str
    tone: str
    action: str
    page: str
    tab: str = ""


def load_workflow(equipment_id, db_path):
    """Read one SQLite snapshot so a background step cannot split the formula."""
    with get_connection(db_path) as connection:
        connection.execute("BEGIN")
        device = connection.execute("SELECT * FROM equipment WHERE equipment_id=?", (equipment_id,)).fetchone()
        plan = connection.execute("SELECT * FROM maintenance_plans WHERE equipment_id=?", (equipment_id,)).fetchone()
        records = {}
        for key, table, order in (("faults", "fault_records", "occurred_at"),
                                  ("alarms", "alarm_records", "occurred_at"),
                                  ("maintenance", "maintenance_records", "performed_at")):
            records[key] = [dict(row) for row in connection.execute(
                f"SELECT * FROM {table} WHERE equipment_id=? ORDER BY {order} DESC, id DESC", (equipment_id,))]
    return dict(device) if device else None, dict(plan) if plan else None, records


def recommend(device, plan, records, driver_state, thresholds):
    """Choose an explanation/action, without performing or claiming any operation."""
    faults = [r for r in records["faults"] if r["status"] != "已处理"]
    active = [r for r in records["alarms"] if r["status"] != "已解除"]
    if faults or device["status"] == "故障":
        return Decision("fault", 2, "先完成故障处理", f"未完成故障 {len(faults)} 条。故障处理完成前禁止启动，不能登记维护完成。",
                        "red", "处理设备故障", "故障台账", "处理故障" if faults else "新增故障")
    if device["status"] == "离线":
        return Decision("offline", 0, "设备离线，暂不能运行", "先核对设备状态；离线时不累计工时与焊接次数。",
                        "amber", "查看设备监测", "运行监测")
    if thresholds and device["temperature_c"] >= thresholds[0]:
        return Decision("temperature", 2, "当前温度需要处理", f'当前 {device["temperature_c"]:.1f} ℃，已达到预警阈值 {thresholds[0]:g} ℃。切回正常场景或停机冷却，温度恢复后自动解除。',
                        "red" if device["temperature_c"] >= thresholds[1] else "amber", "前往控制与冷却", "仿真控制")
    remaining = plan["next_maintenance_runtime_hours"] - device["total_runtime_hours"] if plan else None
    if remaining is not None and remaining <= 0:
        if device["status"] != "已停止":
            return Decision("due_stop", 2, "维护已到期，请先停机", "暂停仿真只冻结计算，不能代替停机。停机并处理未完成故障后，才能登记维护完成。",
                            "red", "前往停机", "仿真控制")
        return Decision("due_maintain", 3, "已停机，可以登记维护", "维护记录将保存实际累计工时，并据此重新计算下一次维护目标。",
                        "amber", "登记维护完成", "维护排程", "完成维护")
    if any(r["status"] == "未处理" for r in active):
        return Decision("acknowledge", 1, "有提醒尚未确认", f"当前有效预警 {len(active)} 条。确认表示已知晓，条件恢复后才会自动解除，历史记录继续保留。",
                        "amber", "查看并确认提醒", "预警中心")
    if remaining is not None and remaining <= plan["reminder_lead_hours"]:
        return Decision("near", 1, "已进入维护提醒窗口", f"{remaining_text(remaining)}，请安排停机保养。演示时也可继续运行，观察到期后的提醒升级。",
                        "amber", "前往运行控制", "仿真控制")
    if records["maintenance"]:
        latest = records["maintenance"][0]
        if remaining is not None and latest["runtime_hours"] == plan["last_maintenance_runtime_hours"]:
            return Decision("new_cycle", 4, "维护已记录，进入新周期", f'维护时累计 {latest["runtime_hours"]:g} h，当前累计 {device["total_runtime_hours"]:g} h，下次目标 {plan["next_maintenance_runtime_hours"]:g} h。累计值没有清零。',
                            "green", "核对维护历史", "维护排程", "历史记录")
    if driver_state == "running" and device["status"] == "运行中":
        return Decision("running", 0, "正在积累运行数据", "后台持续生成快照。切换页面后仍会推进，可观察工时如何接近维护目标。",
                        "green", "查看运行趋势", "运行监测")
    return Decision("ready", 0, "从设备运行开始", "先启动或继续仿真，生成运行快照，再观察阈值提醒、停机处理及下一周期。",
                    "blue", "打开仿真控制", "仿真控制")


def timeline(records):
    """Only timestamps present in the database become events."""
    events = []
    for row in records["alarms"]:
        events.append((row["occurred_at"], f'预警 #{row["id"]} · {row["alarm_type"]}', f'当前状态：{row["status"]}；最新记录内容：{row["content"]}', "amber"))
        if row["resolved_at"]:
            events.append((row["resolved_at"], f'预警 #{row["id"]} 已解除', row["alarm_type"], "green"))
    for row in records["faults"]:
        events.append((row["occurred_at"], f'故障 #{row["id"]} · {row["fault_type"]}', f'当前状态：{row["status"]}；{row["description"]}', "red"))
        if row["resolved_at"]:
            events.append((row["resolved_at"], f'故障 #{row["id"]} 处理完成', row["handling_note"], "green"))
    for row in records["maintenance"]:
        events.append((row["performed_at"], f'维护 #{row["id"]} · {row["maintenance_type"]}',
                       f'{row["performed_by"]} · 累计 {row["runtime_hours"]:g} h；{row["description"]}', "green"))
    return sorted(events, key=lambda event: event[0], reverse=True)


def _go(navigate, decision, equipment_id, records):
    navigate(decision.page, equipment_id)
    if decision.tab:
        st.session_state["maintenance_tabs" if decision.page == "维护排程" else "fault_tabs"] = decision.tab
    if decision.page == "预警中心":
        st.session_state["alarm_type_filter"] = None
        st.session_state["alarm_status_filter"] = None
    if decision.page == "故障台账" and decision.tab == "处理故障":
        st.session_state["process_fault_id"] = next(r["id"] for r in records["faults"] if r["status"] != "已处理")
    st.rerun()


def render_workflow_page(db_path, navigate, thresholds):
    equipment = list_equipment(db_path=db_path)
    if not equipment:
        st.info("暂无设备，接入设备后可查看运维流程。")
        return
    names = {row["equipment_id"]: row["name"] for row in equipment}
    selector, context = st.columns([1, 1.8], vertical_alignment="bottom")
    with selector:
        prepare_device_widget("workflow_equipment")
        equipment_id = st.selectbox("当前关注设备", names, key="workflow_equipment",
                                   format_func=lambda value: f"{value} · {names[value]}",
                                   on_change=device_changed, args=("workflow_equipment",))
    device, plan, records = load_workflow(equipment_id, db_path)
    if device is None:
        st.warning("设备记录已变更，请刷新选择。")
        return
    report = get_runtime(db_path).manager.get_report(equipment_id) if equipment_id in DEMO_PROFILES else None
    driver = report["state"]["state"] if report else "paused"
    view = control_view(driver, device["status"], report["run"] is not None) if report else None
    with context:
        st.html('<div class="workflow-context">设备记录 ' + badge(device["status"], status_tone(device["status"]))
                + ('<span>仿真驱动 ' + badge(view["label"], view["tone"]) + '</span>' if view else "") + '</div>')
    decision = recommend(device, plan, records, driver, thresholds)
    steps = ""
    for i, label in enumerate(STAGES):
        current = "current" if i == decision.stage else ""
        aria = 'aria-current="step"' if current else ""
        steps += f'<div class="flow-step {current}" {aria}><span>{i+1:02}</span><strong>{label}</strong></div>'
    st.html(f'<div class="flow-rail" aria-label="运维处理路线">{steps}</div>')
    st.caption("高亮表示当前建议处理的环节；各环节的实际结果以数据库记录为准。")
    with st.container(border=True, key="workflow_decision"):
        text, action = st.columns([3, 1], vertical_alignment="center")
        with text:
            st.html(f'<div class="decision-label">{badge("当前建议", decision.tone)}</div>'
                    f'<div class="decision-title">{escape(decision.title)}</div><div class="decision-detail">{escape(decision.detail)}</div>')
        with action:
            if st.button(decision.action, type="primary", width="stretch", key="workflow_primary"):
                _go(navigate, decision, equipment_id, records)
            secondary_page = "运行监测" if decision.page == "仿真控制" else "仿真控制"
            if st.button("查看实时监测" if secondary_page == "运行监测" else "查看运行控制", width="stretch", key="workflow_control"):
                _go(navigate, Decision("control", 0, "", "", "", "", secondary_page), equipment_id, records)
    remaining = plan["next_maintenance_runtime_hours"] - device["total_runtime_hours"] if plan else None
    kpis([
        ("当前累计运行 / h", f'{device["total_runtime_hours"]:g}', "维护后继续累计，不清零", "blue"),
        ("下次维护目标 / h", f'{plan["next_maintenance_runtime_hours"]:g}' if plan else "—", "按运行工时排程", "blue"),
        ("维护超期 / h" if remaining is not None and remaining < 0 else "维护剩余 / h", f'{abs(remaining):g}' if remaining is not None else "—",
         remaining_text(remaining) if remaining is not None else "暂无维护计划",
         "neutral" if remaining is None else "red" if remaining <= 0 else "amber" if remaining <= plan["reminder_lead_hours"] else "green"),
        ("当前温度 / ℃", f'{device["temperature_c"]:.1f}', f'累计焊接 {device["welding_count"]:,} 次',
         "red" if thresholds and device["temperature_c"] >= thresholds[0] else "green"),
    ])
    rules, history = st.columns([1.05, 1], gap="large")
    with rules, st.container(border=True, key="workflow_rules"):
        section("提醒与排程依据", "判断依据来自当前设备和维护计划")
        if plan:
            origin_label = "上次维护工时" if records["maintenance"] else "计划维护基准工时"
            st.html(f'<div class="formula-block"><small>下一次维护目标</small><strong>{plan["last_maintenance_runtime_hours"]:g} + {plan["maintenance_interval_hours"]:g} = {plan["next_maintenance_runtime_hours"]:g} h</strong><p>{origin_label} + 维护周期</p></div>'
                    f'<div class="formula-block"><small>当前维护剩余</small><strong>{plan["next_maintenance_runtime_hours"]:g} − {device["total_runtime_hours"]:g} = {remaining:g} h</strong><p>下一次目标 − 当前累计工时</p></div>')
            if not records["maintenance"]:
                st.caption(f'当前按计划维护基准 {plan["last_maintenance_runtime_hours"]:g} h 排程，尚无实际维护完成记录。')
            st.caption(f'0 < 剩余 ≤ {plan["reminder_lead_hours"]:g} h：即将维护；剩余 ≤ 0 h：维护到期。')
        else:
            st.warning("该设备尚无维护计划，暂不能计算排程。")
        if thresholds:
            st.caption(f'温度 ≥ {thresholds[0]:g} ℃：警告；≥ {thresholds[1]:g} ℃：严重。超温仅提醒，不自动停机。')
        else:
            st.warning("温度阈值配置无效，暂不能说明温度判断。")
        active = [r for r in records["alarms"] if r["status"] != "已解除"]
        st.html(f'<div class="workflow-counts">有效预警 <b>{len(active)}</b> · 未完成故障 <b>{sum(r["status"] != "已处理" for r in records["faults"])}</b> · 维护记录 <b>{len(records["maintenance"])}</b></div>')
        with st.expander("指标用途与仿真假设"):
            st.markdown("**累计工时、维护剩余**：决定保养时机。**温度**：识别超温。**设备状态**：约束启动与维护。**焊接次数**：辅助观察作业量，本版本不作为维护触发条件。")
            st.write(f"本项目用规则模型生成模拟数据，验证提醒、故障台账和排程流程。每 {STEP_SECONDS:g} 秒最多推进 {HOURS_PER_STEP:g} 个模拟小时，延迟不补步；运行才累计工时与产量。")
            if equipment_id in DEMO_PROFILES:
                rate, offset = DEMO_PROFILES[equipment_id]
                st.write(f"该设备每模拟运行小时焊接 {rate} 次。正常目标温度为预警阈值 − {offset:g} ℃，异常目标为严重阈值 + {DEMO_ANOMALY_MARGIN_C:g} ℃，停机目标为 {DEMO_AMBIENT_C:g} ℃。")
            st.code(f"T下一步 = T当前 + {DEMO_RESPONSE:g} × (T目标 − T当前)", language=None)
            st.caption("参数用于课程功能验证，未经真实设备标定。温度预警不会自动创建人工故障台账；确认提醒也不代表已完成维修。")
    with history, st.container(border=True, key="workflow_history"):
        section("处理记录", "仅显示该设备已保存的预警、故障和维护事件")
        events = timeline(records)
        if not events:
            st.html('<div class="empty-state">◇<strong>还没有处理事件</strong>启动仿真产生运行数据；触发预警或登记处理后，这里会显示实际记录。</div>')
        else:
            html = ""
            for timestamp, title, detail, tone in events[:8]:
                time = pd.Timestamp(timestamp).tz_convert("Asia/Shanghai").strftime("%m-%d %H:%M:%S")
                html += f'<div class="event-row"><i class="dot {tone}"></i><div><div class="event-title">{escape(title)}</div><time>{time}</time><p>{escape(detail)}</p></div></div>'
            st.html(f'<div class="event-list">{html}</div>')
            st.caption(f"最近 {min(len(events), 8)} / {len(events)} 个事件 · 北京时间；更多记录见对应台账。")
        maintenance, faults = st.columns(2)
        if maintenance.button("维护历史", key="workflow_history_link", width="stretch"):
            _go(navigate, Decision("history", 4, "", "", "", "", "维护排程", "历史记录"), equipment_id, records)
        if faults.button("故障记录", key="workflow_fault_link", width="stretch"):
            _go(navigate, Decision("faults", 4, "", "", "", "", "故障台账", "故障记录"), equipment_id, records)
