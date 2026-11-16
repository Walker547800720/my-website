"""Single-device defense UI: one live process, traceable evidence, no old data."""

from html import escape
import io
import json
import os
from pathlib import Path
import zipfile

import altair as alt
import pandas as pd
import streamlit as st

from database.db import PROJECT_ROOT, get_connection
from services.alarm_service import list_alarms, _temperature_thresholds
from services.equipment_service import get_equipment, ConfigurationError
from services.maintenance_service import get_maintenance_plan
from services.telemetry_service import get_telemetry_history
from website.defense_session import DEVICE_ID, create_session, get_session, session_path
from website.simulation_view import control_view
from website.ui import apply_theme, badge, kpis, section, status_tone
from simulation.welding_simulator import DEMO_PROFILES, DEMO_RESPONSE, DEMO_AMBIENT_C, DEMO_ANOMALY_MARGIN_C, STEP_SECONDS, HOURS_PER_STEP


DATA_ROOT = Path(os.environ.get("WELDING_DEMO_ROOT", PROJECT_ROOT / "data" / "defense"))
PAGES = {"运行看板": ":material/monitoring:", "维护排程": ":material/event_note:",
         "故障台账": ":material/build:", "预警中心": ":material/notifications:",
         "方案与模型": ":material/schema:"}


def navigate(page):
    st.session_state["navigation"] = page
    st.session_state.pop("operation_error", None)
    st.session_state.pop("operation_feedback", None)


def _new_session():
    old = st.session_state.get("defense_session_id")
    if old:
        previous = get_session(session_path(DATA_ROOT, old))
        if not previous.finish():
            st.session_state["operation_error"] = previous.report()["reason"]
            return
    path = create_session(DATA_ROOT)
    st.session_state.clear()
    st.session_state.update(defense_session_id=path.parent.name, navigation="运行看板", active_equipment=DEVICE_ID)
    st.query_params.clear()
    st.query_params["demo"] = path.parent.name


def _command(session, action, mode_key=None):
    session.command(action, mode=st.session_state[mode_key] if mode_key else None)


def _finish_session(session):
    st.session_state.pop("operation_feedback", None)
    st.session_state.pop("operation_error", None)
    if not session.finish():
        st.session_state["operation_error"] = session.report()["reason"]


def thresholds(db_path):
    with get_connection(db_path) as connection:
        return _temperature_thresholds(connection)


def sample_frame(db_path, events):
    rows = get_telemetry_history(DEVICE_ID, db_path=db_path)
    frame = pd.DataFrame(reversed(rows))
    if frame.empty:
        return frame
    frame["模拟经过小时"] = range(len(frame))  # Initial snapshot = 0; every committed step = 1 h.
    frame["采样时间"] = pd.to_datetime(frame["recorded_at"], utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%H:%M:%S")
    modes = [e for e in events if e["action"] in ("start", "set_mode")]
    def mode_at(sample_id):
        mode = "normal"
        for event in modes:
            if event["after_sample_id"] < sample_id:
                mode = event["mode"]
        return "正常运行" if mode == "normal" else "温度异常"
    frame["运行场景"] = frame["id"].map(mode_at)
    return frame


def render_dashboard(session):
    path = session.db_path
    device = get_equipment(DEVICE_ID, db_path=path)
    plan = get_maintenance_plan(DEVICE_ID, db_path=path)
    report = session.report()
    controls = control_view(report["state"]["state"], device["status"], report["run"] is not None)
    alarms = [a for a in list_alarms(db_path=path) if a["status"] != "已解除"]
    warning, critical = thresholds(path)
    status = "本轮已结束" if report["ended"] else {"paused": "已暂停", "running": "仿真推进中", "stopped": "停机冷却中"}[report["state"]["state"]]
    if not report["ended"] and report["state"]["state"] != "paused" and device["status"] in ("故障", "离线"):
        status = device["status"] + " · 仅冷却"
    st.html(f'<div class="defense-device"><div><span class="eyebrow">单设备运行现场</span><h2>自动化焊接设备 <small>{DEVICE_ID}</small></h2></div>'
            f'<div>{badge("演示：" + status, "green" if status == "仿真推进中" else "neutral")} {badge("设备记录：" + device["status"], status_tone(device["status"]))}</div></div>')
    kpis([("设备温度 / ℃", f'{device["temperature_c"]:.1f}', f"预警 {warning:g} ℃ · 严重 {critical:g} ℃", "red" if device["temperature_c"] >= warning else "blue"),
          ("累计运行 / h", f'{device["total_runtime_hours"]:g}', "从本轮零工时开始，维护不清零", "blue"),
          ("累计焊接 / 次", f'{device["welding_count"]:,}', "课程模拟产量 · 每运行小时20次", "green"),
          ("维护剩余 / h", f'{plan["remaining_runtime_hours"]:g}', plan["maintenance_status"], status_tone(plan["maintenance_status"]))])
    with st.container(border=True):
        section("运行控制", "2秒推进1个模拟小时；暂停用于讲解，停机用于故障与维护处理。")
        mode, start, pause, stop = st.columns([2, 1.2, 1.2, 1.5], vertical_alignment="bottom")
        st.session_state["defense_mode"] = report["state"]["mode"]
        mode.selectbox("运行场景", ["normal", "temperature_anomaly"], key="defense_mode",
                       format_func=lambda x: "正常运行" if x == "normal" else "温度异常",
                       disabled=report["ended"], on_change=_command, args=(session, "set_mode", "defense_mode"))
        blocked = device["status"] in ("故障", "离线") or report["ended"]
        start.button("开始运行" if device["total_runtime_hours"] == 0 else "继续运行", key="defense_start", type="primary", width="stretch",
                     disabled=blocked or controls["start_disabled"], on_click=_command, args=(session, "start"))
        pause.button("暂停讲解", key="defense_pause", width="stretch", disabled=report["ended"] or report["state"]["state"] == "paused",
                     on_click=_command, args=(session, "pause"))
        stop.button("停机并冷却", key="defense_stop", width="stretch", disabled=report["ended"] or controls["stop_disabled"],
                    on_click=_command, args=(session, "stop"))
        st.caption(report["reason"] + "。暂停时保留设备最后记录；停机后工时和产量不再增加。")
        if report["control_error"]:
            st.error(report["control_error"])
        for key in ("sync_error", "step_error"):
            if report[key]:
                st.error(report[key])
        if report["last_result"] and report["last_result"]["alarm_sync_skipped"]:
            st.warning(report["last_result"]["warning"])
    left, right = st.columns([1.55, 1])
    with left, st.container(border=True):
        section("按运行时长维护", "累计工时决定维护时机，温度预警独立判断。")
        st.progress(min(1.0, max(0.0, (plan["total_runtime_hours"] - plan["last_maintenance_runtime_hours"]) / plan["maintenance_interval_hours"])))
        st.markdown(f'**{plan["maintenance_status"]}**　目标 **{plan["next_maintenance_runtime_hours"]:g} h** − 当前 **{device["total_runtime_hours"]:g} h** = 剩余 **{plan["remaining_runtime_hours"]:g} h**')
        st.caption(f'周期 {plan["maintenance_interval_hours"]:g} h；提前 {plan["reminder_lead_hours"]:g} h提醒。首次到达提醒与到期节点会暂停，方便查看。')
        if st.button("查看计划与登记维护", key="dashboard_maintenance", disabled=report["ended"]):
            navigate("维护排程")
            st.rerun()
    with right, st.container(border=True):
        section("当前提醒", f"有效预警 {len(alarms)} 条")
        if not alarms:
            st.success("当前无温度或维护预警")
        for alarm in alarms:
            st.warning(f'{alarm["alarm_type"]} · {alarm["level"]}：{alarm["content"]}')
        if st.button("查看预警记录", key="dashboard_alarms"):
            navigate("预警中心")
            st.rerun()
    frame = sample_frame(path, report["events"])
    section("本轮运行趋势", "只展示本轮实际快照；横轴为模拟经过时间，包含停机冷却，暂停不推进。")
    window = st.radio("趋势范围", ["最近40步", "本轮全部"], horizontal=True, key="defense_trend_window")
    plotted = frame.tail(40) if window == "最近40步" else frame
    if len(frame) <= 1:
        st.info("尚未运行。图中仅有25℃、0工时的初始点，点击“开始运行”生成连续曲线。")
    for column, field, title in zip(st.columns(2), ("temperature_c", "total_runtime_hours"), ("温度趋势 / ℃", "累计运行时间 / h")):
        with column, st.container(border=True):
            section(title)
            chart = alt.Chart(plotted).mark_line(color="#3975ed", strokeWidth=2.5, point=alt.OverlayMarkDef(size=16), interpolate="linear").encode(
                x=alt.X("模拟经过小时:Q", title="本轮模拟经过 / h（含冷却）", axis=alt.Axis(tickMinStep=1)),
                y=alt.Y(f"{field}:Q", title="温度 / ℃" if field == "temperature_c" else "累计运行 / h", scale=alt.Scale(zero=True)),
                tooltip=["模拟经过小时:Q", alt.Tooltip(f"{field}:Q", format=".2f"), "运行场景:N", "status:N", "采样时间:N"])
            if field == "temperature_c":
                lines = alt.Chart(pd.DataFrame({"阈值": [warning, critical], "等级": ["警告", "严重"]})).mark_rule(strokeDash=[5,4]).encode(
                    y="阈值:Q", color=alt.Color("等级:N", scale=alt.Scale(domain=["警告", "严重"], range=["#d6a044", "#dc7067"])), tooltip=["阈值:Q", "等级:N"])
                chart += lines
            st.altair_chart(chart.properties(height=255).configure_view(stroke=None), width="stretch")
    st.caption(f"本轮 {len(frame)} 条真实快照（含1条初始化快照），当前显示 {len(plotted)} 个点。新一轮使用独立数据库，不连接旧会话。")
    with st.expander("操作时间线与本轮数据"):
        action_names = {"start": "启动/继续", "pause": "暂停讲解", "stop": "停机冷却", "set_mode": "切换场景", "finish": "结束本轮",
                        "maintenance_hold": "维护节点暂停", "lease_pause": "离开页面自动暂停", "cooling_complete": "冷却完成暂停"}
        action_names["duration_pause"] = "连续演示时限暂停"
        if report["events"]:
            events = pd.DataFrame(report["events"])
            events["action"] = events["action"].map(action_names)
            st.dataframe(events.rename(columns={"action": "操作", "at": "UTC时间", "mode": "场景", "sample_id": "快照编号"}), hide_index=True, width="stretch")
        st.dataframe(frame.rename(columns={"temperature_c": "温度℃", "total_runtime_hours": "累计工时h", "status": "设备状态"}),
                     column_order=["模拟经过小时", "温度℃", "累计工时h", "设备状态", "运行场景", "采样时间"], hide_index=True, width="stretch")


def evidence_zip(session):
    output = io.BytesIO()
    with get_connection(session.db_path) as connection:
        connection.execute("BEGIN")
        tables = {name: pd.read_sql_query(f"SELECT * FROM {name}", connection) for name in
                  ("equipment", "telemetry", "fault_records", "maintenance_plans", "maintenance_records", "alarm_records", "system_config")}
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, frame in tables.items():
            archive.writestr(name + ".csv", frame.to_csv(index=False).encode("utf-8-sig"))
        archive.writestr("session.json", json.dumps(session.info, ensure_ascii=False, indent=2))
        archive.writestr("数据说明.txt", "课程规则仿真数据，非真实设备测量。时间为UTC；温度℃、工时h、焊接次数为次。每2秒最多推进1模拟小时；快照含零增量初始点和冷却点。")
    return output.getvalue()


def render_design(db_path):
    warning, critical = thresholds(db_path)
    plan = get_maintenance_plan(DEVICE_ID, db_path=db_path)
    interval, lead = plan["maintenance_interval_hours"], plan["reminder_lead_hours"]
    rate, offset = DEMO_PROFILES[DEVICE_ID]
    st.markdown("### 设计目标与软件架构")
    st.write("第11题：自动化焊接设备运维提醒系统方案设计。以一台模拟焊接设备验证运维指标、故障台账、按工时维护排程和信息管理流程。")
    st.code("Streamlit 管理界面 → 演示会话/运行驱动 → 设备、运行数据、故障、维护、预警服务 → SQLite\nPython 规则仿真 → 运行快照 → 阈值判断 → 提醒、处理及下一维护周期", language=None)
    st.dataframe(pd.DataFrame([
        ["累计运行工时 / h", "维护排程依据", "仅有效运行步骤+1，停机、故障、离线不累计"],
        ["设备温度 / ℃", "辅助识别超温", f">={warning:g}警告，>={critical:g}严重，恢复后自动解除"],
        ["设备状态", "约束启停与维护", "已停止、运行中、故障、离线"],
        ["累计焊接次数 / 次", "辅助作业量指标", f"每模拟运行小时{rate}次，不作为维护触发条件"],
        ["维护剩余 / h", "判断提前提醒与到期", f"下次目标−累计工时；<={lead:g}提醒，<=0到期"],
    ], columns=["指标", "选取用途", "本演示规则"]), hide_index=True, width="stretch")
    st.markdown("### 仿真模型与参数假设")
    st.code(f"每{STEP_SECONDS:g}秒 → {HOURS_PER_STEP:g}个模拟小时；不追补暂停或服务器停机的时间\nT新 = T当前 + {DEMO_RESPONSE:g} × (T目标 − T当前)\n正常目标 = {warning:g} − {offset:g} = {warning-offset:g}℃；异常目标 = {critical:g} + {DEMO_ANOMALY_MARGIN_C:g} = {critical+DEMO_ANOMALY_MARGIN_C:g}℃；停机向{DEMO_AMBIENT_C:g}℃冷却\n下次维护目标 = 完成维护时的累计工时 + {interval:g}h（累计工时不清零）", language=None)
    st.info("以上温度、周期、产量和时间倍率均为课程演示参数，未经真实设备标定。温度阈值与维护参数来自本轮system_config；这是规则维护提醒，不是寿命预测或真实焊机控制。")
    st.markdown("### 现场验证顺序")
    target = plan["next_maintenance_runtime_hours"]
    st.write(f"零工时开场 → 启动并观察 → {target-lead:g}h提前提醒 → 继续至{target:g}h到期 → 停机登记维护 → 核对新目标与预警解除 → 再运行；然后登记/处理故障，并演示温度异常及恢复。")
    st.caption(f"本周期目标{target:g}h，提前{lead:g}h提醒；倍率{STEP_SECONDS:g}秒/{HOURS_PER_STEP:g}h。实际受服务器调度影响，提醒节点会暂停供讲解。")


def render_app(business_pages):
    st.set_page_config(page_title="自动化焊接设备运维提醒系统", page_icon=":material/precision_manufacturing:", layout="wide")
    apply_theme()
    for key in st.session_state.pop("clear_submitted_fields", []):
        st.session_state[key] = ""
    if "defense_session_id" not in st.session_state:
        requested = st.query_params.get("demo")
        try:
            path = session_path(DATA_ROOT, requested)
            if not path.is_file():
                raise ValueError("演示不存在")
        except ValueError:
            path = create_session(DATA_ROOT)
        st.session_state.update(defense_session_id=path.parent.name, active_equipment=DEVICE_ID)
        st.query_params.clear()
        st.query_params["demo"] = path.parent.name
    session = get_session(session_path(DATA_ROOT, st.session_state["defense_session_id"]))
    st.session_state.setdefault("navigation", "运行看板")
    if st.session_state["navigation"] not in PAGES:
        st.session_state["navigation"] = "运行看板"
    page = st.session_state["navigation"]
    with st.sidebar:
        st.html('<div class="brand"><div class="brand-mark">W</div><div><div class="brand-name">焊接设备运维</div><div class="brand-sub">MAINTENANCE DEMO</div></div></div>')
        st.html('<div class="nav-label">设备运行与运维管理</div>')
        for name, icon in PAGES.items():
            st.button(name, icon=icon, key="nav_" + name, type="primary" if page == name else "secondary", width="stretch", on_click=navigate, args=(name,))
        plan = get_maintenance_plan(DEVICE_ID, db_path=session.db_path)
        st.html(f'<div class="sidebar-note"><strong>WELD-001 · 单设备答辩</strong><br>初始0工时 · 周期{plan["maintenance_interval_hours"]:g}h · 提前{plan["reminder_lead_hours"]:g}h<br>模拟数据，不连接真实硬件</div>')
        st.button("结束本轮并冻结", key="finish_defense", width="stretch", disabled=session.report()["ended"], on_click=_finish_session, args=(session,))
        st.button("从零开始新一轮", key="new_defense", width="stretch", on_click=_new_session)
        st.caption("刷新保留本轮；离开页面约12秒自动暂停。新一轮保留旧数据，不覆盖原始库。")
    st.html(f'<div class="page-head"><div><div class="eyebrow">自动化焊接设备运维提醒系统方案设计</div><h1>{escape(page)}</h1><div class="page-desc">现场生成运行数据，验证提醒、故障处理与工时维护闭环。</div></div></div>')
    st.caption(f'本轮 {session.db_path.parent.name} · 数据从零开始 · 所有页面使用同一设备、同一数据库')

    @st.fragment(run_every=1)
    def live():
        session.heartbeat()
        report = session.report()
        if report["ended"]:
            st.info("本轮已结束，所有数据已冻结。可查看本轮曲线并下载记录，点击侧栏按钮开始新一轮。")
        if "operation_feedback" in st.session_state:
            st.success(st.session_state["operation_feedback"])
        if "operation_error" in st.session_state:
            st.error(st.session_state["operation_error"])
        if page in ("运行看板", "方案与模型"):
            try:
                render_dashboard(session) if page == "运行看板" else render_design(session.db_path)
            except ConfigurationError as error:
                st.error(f"温度配置无效：{error}。请检查system_config；可结束本轮或新建演示。不会补造预警记录。")
        elif report["ended"]:
            table = {"维护排程": "maintenance_records", "故障台账": "fault_records", "预警中心": "alarm_records"}[page]
            with get_connection(session.db_path) as connection:
                frame = pd.read_sql_query(f"SELECT * FROM {table} ORDER BY id", connection)
            if frame.empty:
                st.info("本轮暂无对应记录。")
            else:
                st.dataframe(frame, hide_index=True, width="stretch")
        else:
            business_pages[page](session.db_path)
        if report["ended"]:
            st.download_button("下载本轮数据与操作记录", data=evidence_zip(session), file_name=session.db_path.parent.name + ".zip", mime="application/zip")
    live()
    st.html('<div class="footer"><span>单设备课程仿真 · 运维指标 / 故障台账 / 工时维护排程</span><span>原始数据与旧演示完整保留</span></div>')
