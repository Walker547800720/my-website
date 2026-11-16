"""设备运维网站；业务写入由现有服务执行。"""

from contextlib import closing
from datetime import datetime
from html import escape
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

from database.db import DEFAULT_DB_PATH, get_connection
from services.equipment_service import get_equipment, get_equipment_statistics, list_equipment
from services.telemetry_service import get_latest_telemetry, get_telemetry_history
from services.fault_service import (
    FAULT_TYPES, FAULT_LEVELS, FAULT_STATUSES, create_fault, list_faults, update_fault_status,
)
from services.maintenance_service import complete_maintenance, list_maintenance_plans
from services.alarm_service import (
    ALARM_STATUSES, TEMPERATURE_ALARM_TYPE, MAINTENANCE_ALARM_TYPE,
    check_alarms, list_alarms, confirm_alarm,
)
from website.simulation_page import render_simulation_page
from website.workflow_page import render_workflow_page
from website.runtime import get_runtime
from website.workspace_ui import render_workspace_selector
from website.device_context import initialize_device_context, select_device, device_changed, prepare_device_widget
from website.ui import apply_theme, badge, kpis, remaining_text, section, status_tone, styled_records


PAGES = {
    "运维流程": (":material/route:", "从运行数据到提醒、处理与下一维护周期，查看当前设备的完整运维过程。"),
    "设备总览": (":material/precision_manufacturing:", "设备规模、运行状态与累计作业数据。"),
    "运行监测": (":material/monitoring:", "查看已保存的最新运行快照与历史变化。"),
    "故障台账": (":material/build:", "记录设备故障，跟踪处理进度与处理说明。"),
    "维护排程": (":material/event_note:", "查看维护计划、登记维护结果与追溯历史记录。"),
    "预警中心": (":material/notifications:", "查看温度与维护预警，跟踪当前异常及历史处理记录。"),
    "仿真控制": (":material/tune:", "控制课程演示模型，查看实际保存的设备数据与温度变化。"),
}


def check_database(db_path):
    """只读打开已有数据库；文件缺失时抛错，绝不创建或初始化。"""
    database_uri = db_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True, timeout=10)) as connection:
        connection.execute("SELECT equipment_id FROM equipment LIMIT 1").fetchone()


def navigate(page, equipment_id=None):
    st.session_state.pop("operation_error", None)
    st.session_state.pop("operation_feedback", None)
    st.session_state["navigation"] = "运行看板" if page in ("仿真控制", "运行监测", "设备总览", "运维流程") else page
    if equipment_id is not None:
        select_device(equipment_id)


def temperature_thresholds(db_path):
    """Read actual display thresholds; never invent a fallback alarm setting."""
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT config_key, config_value, unit FROM system_config WHERE config_key IN (?, ?)",
            ("temperature_warning_c", "temperature_critical_c"),
        ).fetchall()
    values = {row["config_key"]: row for row in rows}
    try:
        warning, critical = values["temperature_warning_c"], values["temperature_critical_c"]
        if warning["unit"] == critical["unit"] == "℃" and 0 < warning["config_value"] < critical["config_value"] < float("inf"):
            return warning["config_value"], critical["config_value"]
    except (KeyError, TypeError):
        pass
    return None


def temperature_badge(value, thresholds):
    if thresholds is None:
        return badge("阈值未配置")
    warning, critical = thresholds
    return badge("严重超温", "red") if value >= critical else (
        badge("温度偏高", "amber") if value >= warning else badge("温度正常", "green"))


def render_equipment_overview(db_path):
    statistics = get_equipment_statistics(db_path=db_path)
    equipment = list_equipment(db_path=db_path)
    plans = {row["equipment_id"]: row for row in list_maintenance_plans(db_path=db_path)}
    faults = [row for row in list_faults(db_path=db_path) if row["status"] != "已处理"]
    alarms = [row for row in list_alarms(db_path=db_path) if row["status"] != "已解除"]
    thresholds = temperature_thresholds(db_path)
    due = [p for p in plans.values() if p["maintenance_status"] != "正常"]
    kpis([
        ("接入设备", statistics["total_count"], "自动化焊接设备", "blue"),
        ("运行设备", statistics["status_counts"]["运行中"], f"已停止 {statistics['status_counts']['已停止']} · 离线 {statistics['status_counts']['离线']}", "green"),
        ("待处理故障", len(faults), f"故障设备 {statistics['status_counts']['故障']} 台", "red"),
        ("待维护设备", len(due), "包含即将维护与维护到期", "amber"),
    ])
    fleet, tasks = st.columns([1.9, 1], gap="large")
    with fleet:
        section("设备工作台", "查看设备当前状态与维护进度")
        if not equipment:
            st.info("暂无设备，设备接入后将在此展示。")
        for device in equipment:
            equipment_id = device["equipment_id"]
            plan = plans.get(equipment_id)
            with st.container(border=True, key=f"equipment_card_{equipment_id}"):
                title, action = st.columns([4, 1], vertical_alignment="center")
                with title:
                    st.html(f'<div class="device-title"><span class="device-icon">▦</span>{escape(equipment_id)} '
                            f'{badge(device["status"], status_tone(device["status"]))}</div>')
                action.button("查看详情", key=f"view_{equipment_id}", width="stretch",
                              on_click=navigate, args=("运行监测", equipment_id))
                maintenance = badge(plan["maintenance_status"], status_tone(plan["maintenance_status"])) if plan else badge("暂无计划")
                st.html(f'<div class="device-data">'
                        f'<div><small>当前温度</small><strong>{device["temperature_c"]:.1f} ℃</strong></div>'
                        f'<div><small>累计运行</small><strong>{device["total_runtime_hours"]:g} h</strong></div>'
                        f'<div><small>焊接次数</small><strong>{device["welding_count"]:,}</strong></div>'
                        f'<div><small>维护状态</small>{maintenance}</div></div>')
    with tasks:
        with st.container(border=True, key="tasks_panel"):
            section("待办提醒", f"当前未解除预警 {len(alarms)} 条")
            todos = []
            for fault in faults:
                todos.append((fault["equipment_id"], "故障待处理", fault["fault_type"], "red"))
            for device in equipment:
                if thresholds and device["temperature_c"] >= thresholds[0]:
                    todos.append((device["equipment_id"], "温度异常", f'当前 {device["temperature_c"]:.1f} ℃ · 预警阈值 {thresholds[0]:g} ℃',
                                  "red" if device["temperature_c"] >= thresholds[1] else "amber"))
            for plan in sorted(due, key=lambda p: p["remaining_runtime_hours"]):
                todos.append((plan["equipment_id"], plan["maintenance_status"], remaining_text(plan["remaining_runtime_hours"]), status_tone(plan["maintenance_status"])))
            for equipment_id, label, detail, tone in todos[:5]:
                st.html(f'<div class="todo"><div class="todo-title">{escape(equipment_id)}{badge(label, tone)}</div>'
                        f'<div class="todo-text">{escape(detail)}</div></div>')
            if not todos:
                st.html('<div class="empty-state">✓<strong>当前没有待办事项</strong>设备状态与维护计划正常</div>')
            if len(todos) > 5:
                st.caption(f"另有 {len(todos)-5} 项，请进入对应页面查看。")
            st.button("查看维护排程", key="overview_maintenance", width="stretch", on_click=navigate, args=("维护排程",))
            st.button("进入预警中心", key="overview_alarms", width="stretch", on_click=navigate, args=("预警中心",))
            if faults:
                st.button("处理设备故障", key="overview_faults", width="stretch", on_click=navigate, args=("故障台账",))
        with st.container(border=True, key="guide_panel"):
            section("课堂演示")
            st.button("打开演示控制", key="overview_demo", type="primary", width="stretch", on_click=navigate, args=("仿真控制",))
            with st.expander("查看三步演示路线"):
                steps = "".join(f'<div class="guide-step"><span class="step-number">{number:02}</span><span>{text}</span></div>'
                                for number, text in enumerate(("启动仿真，观察温度与累计工时", "切换异常模式，查看预警变化", "登记维护，核对下一次维护目标"), 1))
                st.html(steps)


def render_runtime_monitoring(db_path):
    equipment = list_equipment(db_path=db_path)
    if not equipment:
        st.info("暂无可监测设备，设备接入后可选择查看运行数据。")
        return
    names = {row["equipment_id"]: row["name"] for row in equipment}
    selector, refresh = st.columns([5, 1], vertical_alignment="bottom")
    with selector:
        prepare_device_widget("monitor_equipment")
        equipment_id = st.selectbox(
            "选择设备", list(names), format_func=lambda value: f"{value} · {names[value]}",
            key="monitor_equipment", on_change=device_changed, args=("monitor_equipment",),
        )
    with refresh:
        # Streamlit按钮触发整页重跑，后续服务调用每次重新读取，未缓存数据。
        st.button("刷新数据", icon=":material/refresh:", key="refresh_monitor")
    latest = get_latest_telemetry(equipment_id, db_path=db_path)
    history = get_telemetry_history(equipment_id, db_path=db_path)
    thresholds = temperature_thresholds(db_path)
    current = next(row for row in equipment if row["equipment_id"] == equipment_id)
    if latest is None:
        st.info("该设备暂无运行快照，采样数据保存后将在此展示。")
    else:
        sample_time = pd.Timestamp(latest["recorded_at"]).tz_convert("Asia/Shanghai")
        st.caption(f"最新采样 {sample_time.strftime('%Y-%m-%d %H:%M:%S')} · 北京时间")
        kpis([
            ("采样温度 / ℃", f"{latest['temperature_c']:.1f}", "已保存的最新运行快照", "red" if thresholds and latest['temperature_c'] >= thresholds[0] else "blue"),
            ("设备当前状态", current["status"], f"最近采样时：{latest['status']}", status_tone(current["status"])),
            ("累计运行 / h", f"{latest['total_runtime_hours']:g}", "按设备累计运行时长统计", "blue"),
            ("累计焊接次数", f"{latest['welding_count']:,}", "仿真累计作业量", "green"),
        ])
        st.html(temperature_badge(latest["temperature_c"], thresholds))
    section("运行趋势", "观察温度变化与运行时长的累计过程")
    if not history:
        st.info("暂无历史运行数据，趋势图将在有采样记录后显示。")
        return
    frame = pd.DataFrame(reversed(history))
    frame["sample_time"] = (pd.to_datetime(frame["recorded_at"], utc=True)
                            .dt.tz_convert("Asia/Shanghai").dt.tz_localize(None))
    if len(frame) == 1:
        st.caption("目前仅有 1 条历史快照，图中显示实际采样点；积累更多数据后可观察趋势。")
    else:
        st.caption(f"共 {len(frame):,} 条历史快照，按采样时间排列。")
    for column, field, title in zip(st.columns(2),
                                    ("temperature_c", "total_runtime_hours"),
                                    ("温度（℃）", "累计运行时间（h）")):
        with column, st.container(border=True, key=f"chart_{field}"):
            section(f"{title}趋势")
            scale = alt.Scale(zero=False)
            if frame[field].nunique() == 1:
                value = float(frame[field].iloc[0])
                padding = max(1.0, abs(value) * 0.05)
                lower = max(0, value - padding) if field == "total_runtime_hours" else value - padding
                scale = alt.Scale(domain=[lower, value + padding], zero=False)
            chart = alt.Chart(frame).mark_line(color="#3975ed", strokeWidth=2.5, point=alt.OverlayMarkDef(size=14)).encode(
                x=alt.X("sample_time:T", title="采样时间（北京时间）",
                        axis=alt.Axis(format="%H:%M:%S" if (frame["sample_time"].max() - frame["sample_time"].min()).total_seconds() < 600 else "%m-%d %H:%M")),
                y=alt.Y(f"{field}:Q", title=title, scale=scale, axis=alt.Axis(format=".1f")),
                tooltip=[alt.Tooltip("sample_time:T", title="采样时间", format="%Y-%m-%d %H:%M:%S"),
                         alt.Tooltip(f"{field}:Q", title=title)],
            ).properties(height=270)
            if field == "temperature_c" and thresholds:
                rules = pd.DataFrame({"threshold": thresholds, "label": [f"预警 {thresholds[0]:g}℃", f"严重 {thresholds[1]:g}℃"], "color": ["#d6a044", "#dc7067"]})
                lines = alt.Chart(rules).mark_rule(strokeDash=[5, 4]).encode(
                    y="threshold:Q", color=alt.Color("color:N", scale=None),
                    tooltip=[alt.Tooltip("label:N", title="阈值")])
                chart = chart + lines
                st.caption(f"预警线 {thresholds[0]:g} ℃ · 严重线 {thresholds[1]:g} ℃")
            st.altair_chart(chart.configure_view(stroke=None).configure_axis(gridColor="#eef2f7", domain=False, labelColor="#8a97a9", titleColor="#7d8a9b"), width="stretch")


def _record_frame(records, time_columns=()):
    """只格式化显示时间，不改变数据库中的ISO 8601值。"""
    frame = pd.DataFrame(records)
    for column in time_columns:
        frame[column] = (pd.to_datetime(frame[column], utc=True)
                         .dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d %H:%M:%S").fillna("—"))
    return frame


def _submit_operation(operation, success_message, clear_fields=()):
    st.session_state.pop("operation_error", None)
    st.session_state.pop("operation_feedback", None)
    try:
        if "defense_session_id" in st.session_state:
            from website.defense_page import DATA_ROOT
            from website.defense_session import get_session, session_path
            session = get_session(session_path(DATA_ROOT, st.session_state["defense_session_id"]))
            session.perform(operation)
        else:
            operation()
    except (ValueError, LookupError) as error:
        st.session_state["operation_error"] = f"操作失败：{error}"
    except sqlite3.Error:
        st.session_state["operation_error"] = "保存失败，数据库暂时不可用，请稍后重试。"
    else:
        st.session_state["operation_feedback"] = success_message
        st.session_state["clear_submitted_fields"] = list(clear_fields)
    st.rerun()


def render_fault_ledger(db_path):
    faults = list_faults(db_path=db_path)
    equipment = list_equipment(db_path=db_path)
    kpis([(label, sum(row["status"] == label for row in faults), hint, tone) for label, hint, tone in
          (("未处理", "等待排查与处理", "red"), ("处理中", "跟踪维修处理进度", "amber"), ("已处理", "保留完整处理记录", "green"))])
    records_tab, create_tab, process_tab = st.tabs(
        ["故障记录", "新增故障", "处理故障"], key="fault_tabs", on_change="rerun"
    )
    with records_tab:
        if not faults:
            st.html('<div class="empty-state">◇<strong>暂无故障记录</strong>在“新增故障”中登记现象，再到“处理故障”跟踪处理结果。</div>')
        else:
            frame = _record_frame(faults, ("occurred_at",))
            st.caption(f"共 {len(faults):,} 条故障记录 · 时间为北京时间")
            st.dataframe(styled_records(frame, ["level", "status"]), hide_index=True, width="stretch",
                         column_order=("id", "equipment_id", "fault_type", "level", "occurred_at", "status"),
                         column_config={"id": "记录编号", "equipment_id": "设备编号", "fault_type": "故障类型",
                                        "level": "等级", "occurred_at": "发生时间", "status": "处理状态"})
    with create_tab:
        if not equipment:
            st.info("暂无设备，无法登记故障。")
        else:
            names = {row["equipment_id"]: row["name"] for row in equipment}
            # 同一次提交重试沿用事件键，成功后才为下一次登记分配新事件键。
            st.session_state.setdefault("new_fault_event_key", uuid4().hex)
            prepare_device_widget("fault_equipment")
            with st.form("create_fault_form", clear_on_submit=False):
                device_id = st.selectbox("故障设备", list(names),
                                        format_func=lambda value: f"{value} · {names[value]}", key="fault_equipment", disabled=len(names) == 1)
                left, right = st.columns(2)
                fault_type = left.selectbox("故障类型", FAULT_TYPES)
                level = right.selectbox("故障等级", FAULT_LEVELS)
                description = st.text_area("故障描述", key="fault_description_input", help="填写原始故障现象，处理说明将单独保存。")
                submitted = st.form_submit_button("登记故障", type="primary")
            if submitted:
                def register_fault():
                    create_fault(device_id, fault_type, level, description,
                                 event_key=st.session_state["new_fault_event_key"], db_path=db_path)
                    select_device(device_id)
                    st.session_state.pop("new_fault_event_key", None)
                _submit_operation(register_fault, "故障登记成功，设备状态已按故障管理规则更新。", ("fault_description_input",))
    with process_tab:
        if not faults:
            st.info("暂无可处理的故障记录。")
        else:
            by_id = {row["id"]: row for row in faults}
            fault_id = st.selectbox("选择故障", list(by_id), key="process_fault_id",
                                    format_func=lambda value: f"#{value} · {by_id[value]['equipment_id']} · "
                                    f"{by_id[value]['fault_type']} · {by_id[value]['status']}")
            selected = by_id[fault_id]
            st.markdown("**原始故障描述**")
            st.text(selected["description"])
            st.markdown("**已有处理说明**")
            st.text(selected["handling_note"] or "暂无处理说明。")
            with st.form(f"process_fault_form_{fault_id}", clear_on_submit=True):
                status = st.selectbox("处理状态", FAULT_STATUSES,
                                      index=FAULT_STATUSES.index(selected["status"]))
                note = st.text_area("处理说明", help="必填，将追加保存并保留原始故障描述。")
                submitted = st.form_submit_button("保存处理结果", type="primary")
            if submitted:
                _submit_operation(
                    lambda: update_fault_status(fault_id, status, note, db_path=db_path),
                    "处理结果已保存，设备状态已按故障管理规则更新。",
                )


def render_maintenance_schedule(db_path):
    plans = list_maintenance_plans(db_path=db_path)
    kpis([(label, sum(plan["maintenance_status"] == label for plan in plans), hint, tone) for label, hint, tone in
          (("维护到期", "优先安排维护", "red"), ("即将维护", "已进入提前提醒窗口", "amber"), ("正常", "尚未进入提醒窗口", "green"))])
    # 现有维护服务没有历史查询接口，仅补充读取，不在网站中实现维护业务规则。
    with get_connection(db_path) as connection:
        records = [dict(row) for row in connection.execute(
            "SELECT * FROM maintenance_records ORDER BY performed_at DESC, id DESC"
        )]
    plans_tab, history_tab, complete_tab = st.tabs(
        ["维护计划", "历史记录", "完成维护"], key="maintenance_tabs", on_change="rerun"
    )
    with plans_tab:
        if not plans:
            st.info("暂无维护计划。")
        else:
            for column, plan in zip(st.columns(len(plans)), plans):
                with column, st.container(border=True, key=f"maintenance_card_{plan['equipment_id']}"):
                    tone = status_tone(plan["maintenance_status"])
                    interval = plan["maintenance_interval_hours"]
                    elapsed = plan["total_runtime_hours"] - plan["last_maintenance_runtime_hours"]
                    progress = min(100, max(0, elapsed / interval * 100))
                    st.html(f'<div class="maintenance-row"><strong>{escape(plan["equipment_id"])}</strong>{badge(plan["maintenance_status"], tone)}</div>'
                            f'<div class="section-title">{remaining_text(plan["remaining_runtime_hours"])}</div>'
                            f'<div class="progress-track"><div class="progress-fill {tone}" style="width:{progress:.2f}%"></div></div>'
                            f'<div class="section-subtitle">当前累计 {plan["total_runtime_hours"]:g} h · 维护目标 {plan["next_maintenance_runtime_hours"]:g} h</div>')
            st.caption("按累计运行小时排程 · 完成维护后更新下一周期，累计运行时间保留。")
            frame = pd.DataFrame(plans)
            frame["remaining_display"] = frame["remaining_runtime_hours"].map(remaining_text)
            st.dataframe(styled_records(frame, ["maintenance_status"]), hide_index=True, width="stretch",
                         column_order=("equipment_id", "maintenance_interval_hours", "total_runtime_hours",
                                       "next_maintenance_runtime_hours", "remaining_display", "maintenance_status"),
                         column_config={
                             "equipment_id": "设备编号",
                             "maintenance_interval_hours": st.column_config.NumberColumn("维护周期（h）", format="%.2f"),
                             "total_runtime_hours": st.column_config.NumberColumn("累计运行（h）", format="%.2f"),
                             "next_maintenance_runtime_hours": st.column_config.NumberColumn("下次维护目标（h）", format="%.2f"),
                             "remaining_display": "维护提醒",
                             "maintenance_status": "维护状态",
                         })
    with history_tab:
        if not records:
            st.info("暂无历史维护记录。")
        else:
            st.caption(f"共 {len(records):,} 条维护记录 · 时间为北京时间")
            st.dataframe(_record_frame(records, ("performed_at",)), hide_index=True, width="stretch",
                         column_order=("equipment_id", "maintenance_type", "performed_by", "performed_at",
                                       "runtime_hours", "description"),
                         column_config={"equipment_id": "设备编号", "maintenance_type": "维护类型",
                                        "performed_by": "维护人员", "performed_at": "维护时间",
                                        "runtime_hours": st.column_config.NumberColumn("维护时累计运行（h）", format="%.2f"),
                                        "description": "维护说明"})
    with complete_tab:
        if not plans:
            st.info("暂无可登记维护的设备计划。")
        else:
            prepare_device_widget("maintenance_equipment")
            st.info("本系统按先停机、处理故障、再登记维护完成的流程管理；累计工时和产量保留。")
            focused = st.session_state["maintenance_equipment"]
            focused_device = get_equipment(focused, db_path=db_path)
            if focused_device and focused_device["status"] != "已停止":
                st.warning(f'{focused} 当前为{focused_device["status"]}，尚不能登记维护完成。')
                if st.button("前往该设备停机或处理故障", key="maintenance_go_stop"):
                    navigate("故障台账" if focused_device["status"] == "故障" else "仿真控制", focused)
                    st.rerun()
            with st.form("complete_maintenance_form", clear_on_submit=False):
                device_id = st.selectbox("维护设备", [row["equipment_id"] for row in plans], key="maintenance_equipment", disabled=len(plans) == 1)
                left, right = st.columns(2)
                maintenance_type = left.text_input("维护类型", key="maintenance_type_input")
                performed_by = right.text_input("维护人员", key="maintenance_person_input")
                description = st.text_area("维护说明", key="maintenance_description_input")
                st.caption("提交后保存维护记录、更新下一周期并解除对应维护预警，累计运行时间不清零。")
                submitted = st.form_submit_button("确认完成维护", type="primary")
            if submitted:
                def record_maintenance():
                    complete_maintenance(device_id, maintenance_type, description, performed_by, db_path=db_path)
                    select_device(device_id)
                _submit_operation(
                    record_maintenance,
                    "维护已登记，维护计划与历史记录已更新。",
                    ("maintenance_type_input", "maintenance_person_input", "maintenance_description_input"),
                )


def render_alarm_center(db_path):
    refresh, synchronize = st.columns([1, 3], vertical_alignment="bottom")
    with refresh:
        st.button("刷新列表", icon=":material/refresh:", key="refresh_alarms")
    with synchronize:
        if st.button("检查并同步预警", icon=":material/fact_check:", key="check_alarms"):
            _submit_operation(
                lambda: check_alarms(db_path=db_path),
                "预警检查完成，记录已按当前条件同步。",
            )
    alarms = list_alarms(db_path=db_path)
    equipment = list_equipment(db_path=db_path)
    active = [row for row in alarms if row["status"] != "已解除"]
    kpis([
        ("当前预警", len(active), "包含未处理与已确认", "blue"),
        ("严重", sum(row["level"] == "严重" for row in active), "建议优先检查", "red"),
        ("警告", sum(row["level"] == "警告" for row in active), "关注温度与维护状态", "amber"),
        ("已解除", sum(row["status"] == "已解除" for row in alarms), "历史记录保留可追溯", "green"),
    ])
    st.caption("统计覆盖本轮设备的全部预警，不随列表筛选变化。")
    names = {row["equipment_id"]: row["name"] for row in equipment}
    device_filter, type_filter, status_filter = st.columns(3)
    with device_filter:
        prepare_device_widget("alarm_device_filter")
        if len(names) == 1 and "alarm_device_filter" not in st.session_state:
            st.session_state["alarm_device_filter"] = next(iter(names))
        device_id = st.selectbox("筛选设备", [None, *names], key="alarm_device_filter",
                                 format_func=lambda value: "全部设备" if value is None else f"{value} · {names[value]}",
                                 on_change=device_changed, args=("alarm_device_filter",), disabled=len(names) == 1)
    with type_filter:
        types = list(dict.fromkeys([TEMPERATURE_ALARM_TYPE, MAINTENANCE_ALARM_TYPE,
                                   *(row["alarm_type"] for row in alarms)]))
        alarm_type = st.selectbox("预警类型", [None, *types], key="alarm_type_filter",
                                  format_func=lambda value: "全部类型" if value is None else value)
    with status_filter:
        status = st.selectbox("预警状态", [None, *ALARM_STATUSES], key="alarm_status_filter",
                              format_func=lambda value: "全部状态" if value is None else value)
    include_history = st.checkbox("包含已解除的历史预警", key="alarm_include_history")
    # 这里只做列表展示筛选，预警判断、去重、升级和解除均由服务处理。
    filtered = [row for row in alarms
                if (device_id is None or row["equipment_id"] == device_id)
                and (alarm_type is None or row["alarm_type"] == alarm_type)
                and (status is None or row["status"] == status)
                and (include_history or status == "已解除" or row["status"] != "已解除")]
    st.markdown("#### 预警记录")
    if not alarms:
        st.info("暂无预警记录。可点击“检查并同步预警”按已保存的设备数据检查。")
    elif not filtered:
        st.info("当前筛选条件下暂无预警记录，可调整筛选或查看已解除的历史预警。")
    else:
        st.caption(f"筛选结果 {len(filtered):,} 条 · 时间为北京时间")
        st.dataframe(styled_records(_record_frame(filtered, ("occurred_at", "resolved_at")), ["level", "status"]),
                     hide_index=True, width="stretch",
                     column_order=("id", "equipment_id", "alarm_type", "level", "content",
                                   "occurred_at", "status", "resolved_at"),
                     column_config={"id": "记录编号", "equipment_id": "设备编号", "alarm_type": "预警类型",
                                    "level": "等级", "content": st.column_config.TextColumn("预警内容", width="large"),
                                    "occurred_at": "发生时间", "status": "处理状态", "resolved_at": "解除时间"})
    actionable = {row["id"]: row for row in filtered if row["status"] == "未处理"}
    if actionable:
        st.divider()
        st.markdown("#### 确认预警")
        with st.form("resolve_alarm_form"):
            alarm_id = st.selectbox("选择预警", list(actionable), key="resolve_alarm_id",
                                    format_func=lambda value: f"#{value} · {actionable[value]['equipment_id']} · "
                                    f"{actionable[value]['alarm_type']} · {actionable[value]['level']}")
            st.caption("确认表示已知晓，不表示异常已消失。完成维护或温度恢复后，系统自动解除同一事件。")
            submitted = st.form_submit_button("确认已知晓", type="primary")
        if submitted:
            _submit_operation(lambda: confirm_alarm(alarm_id, db_path=db_path), "预警已确认；待条件恢复后自动解除。")


def render_simulation_controls(db_path):
    render_simulation_page(db_path, navigate, temperature_thresholds(db_path))


def render_operational_workflow(db_path):
    render_workflow_page(db_path, navigate, temperature_thresholds(db_path))


def main():
    from website.defense_page import render_app
    render_app({"故障台账": render_fault_ledger,
                "维护排程": render_maintenance_schedule,
                "预警中心": render_alarm_center})


if __name__ == "__main__":
    main()
