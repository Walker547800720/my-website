"""A run-focused simulation screen with current maintenance evidence."""

from html import escape
import sqlite3

import altair as alt
import pandas as pd
import streamlit as st

from services.alarm_service import list_alarms
from services.equipment_service import get_equipment
from services.maintenance_service import get_maintenance_plan
from simulation.welding_simulator import DEMO_PROFILES, MODES, DEMO_AMBIENT_C
from website.runtime import get_runtime
from website.simulation_view import control_view
from website.ui import badge, remaining_text, status_tone
from website.device_context import device_changed, select_device


def _command(manager, equipment_id, action, mode_key=None):
    select_device(equipment_id)
    manager.command(equipment_id, action, mode=st.session_state[mode_key] if mode_key else None)


def render_simulation_page(db_path, navigate, thresholds):
    manager = get_runtime(db_path).manager
    st.caption("演示倍率：2 秒 = 1 个模拟小时。暂停冻结计算；停机停止工时累计并继续冷却。超温仅提醒，不自动停机。")
    st.session_state.setdefault("simulation_device_tabs", st.session_state.get("active_equipment", "WELD-001"))
    # Keep the initial browser selection aligned too: remounting a tracked tab
    # with its implicit first-tab default can send WELD-001 back to the server.
    tabs = st.tabs(list(DEMO_PROFILES), default=st.session_state["simulation_device_tabs"], key="simulation_device_tabs", on_change=device_changed,
                   args=("simulation_device_tabs",))
    for equipment_id, tab in zip(DEMO_PROFILES, tabs):
        if not tab.open:
            continue  # Hidden device controls must not emit callbacks on remount.
        with tab.container(border=True, key=f"sim_card_{equipment_id}"):
            try:
                report = manager.get_report(equipment_id)
                device = get_equipment(equipment_id, db_path=db_path)
                if device is None:
                    raise LookupError("设备记录不存在")
                state, run = report["state"], report["run"]
                view = control_view(state["state"], device["status"], run is not None)
                st.html(f'<div class="device-title"><span class="device-icon">▦</span>{escape(equipment_id)} '
                        f'{badge(view["label"], view["tone"])}</div>')
                mode_column, start, pause, stop = st.columns([2.4, 1.2, 1.2, 1.4], vertical_alignment="bottom")
                mode_key = f"simulation_mode_{manager.db_path}_{equipment_id}"
                st.session_state[mode_key] = state["mode"]
                with mode_column:
                    st.selectbox("运行场景", MODES,
                                 format_func=lambda mode: "正常运行" if mode == "normal" else "温度异常",
                                 key=mode_key, on_change=_command,
                                 args=(manager, equipment_id, "set_mode", mode_key))
                start.button(view["start_label"], key=f"sim_start_{equipment_id}", type="primary", width="stretch",
                             disabled=view["start_disabled"], on_click=_command, args=(manager, equipment_id, "start"))
                pause.button("暂停仿真", key=f"sim_pause_{equipment_id}", width="stretch",
                             disabled=view["pause_disabled"], on_click=_command, args=(manager, equipment_id, "pause"))
                stop.button("停机并冷却", key=f"sim_stop_{equipment_id}", width="stretch",
                            disabled=view["stop_disabled"], on_click=_command, args=(manager, equipment_id, "stop"))
                st.caption(view["hint"])
                if report["control_error"]:
                    st.error(f'控制失败：{report["control_error"]}')
                plan = get_maintenance_plan(equipment_id, db_path=db_path)
                active = [alarm for alarm in list_alarms(equipment_id, db_path=db_path) if alarm["status"] != "已解除"]
                temperature, increment, runtime, welding, remaining = st.columns(5)
                temperature.metric("当前温度 / ℃", f'{device["temperature_c"]:.1f}')
                increment.metric("本轮运行 / h", f'{run["runtime_hours"]:g}' if run else "0")
                runtime.metric("设备累计 / h", f'{device["total_runtime_hours"]:g}')
                welding.metric("累计焊接次数", f'{device["welding_count"]:,}')
                left = plan["remaining_runtime_hours"]
                remaining.metric("维护剩余 / h" if left >= 0 else "维护超期 / h", f'{abs(left):g}')
                if thresholds and device["temperature_c"] >= thresholds[0]:
                    critical = device["temperature_c"] >= thresholds[1]
                    notify = st.error if critical else st.warning
                    notify(f'{"严重超温" if critical else "温度预警"}：当前 {device["temperature_c"]:.1f} ℃，'
                           f'预警阈值 {thresholds[0]:g} ℃，严重阈值 {thresholds[1]:g} ℃。可切回正常场景或停机冷却，系统不会自动停机。')
                reminder = plan["reminder_lead_hours"]
                st.html(f'<div class="maintenance-row"><span class="section-title">工时维护判断</span>'
                        f'{badge(plan["maintenance_status"], status_tone(plan["maintenance_status"]))}</div>')
                st.caption(f'下次维护目标 {plan["next_maintenance_runtime_hours"]:g} h − 当前累计 {device["total_runtime_hours"]:g} h '
                           f'= 剩余 {left:g} h；剩余 ≤ {reminder:g} h 提前提醒，剩余 ≤ 0 h 到期。')
                if plan["maintenance_status"] != "正常":
                    notify = st.error if left <= 0 else st.warning
                    notify(f'{remaining_text(left)}。当前有效预警 {len(active)} 条；维护登记后更新下一周期并保留历史。')
                else:
                    st.caption(f'维护状态正常 · 当前有效预警 {len(active)} 条')
                alerts, maintenance = st.columns(2)
                if alerts.button("查看当前预警", key=f"sim_alarms_{equipment_id}", width="stretch"):
                    st.session_state["alarm_device_filter"] = equipment_id
                    navigate("预警中心")
                    st.rerun()
                if maintenance.button("登记该设备维护", key=f"sim_maintain_{equipment_id}", width="stretch"):
                    navigate("维护排程", equipment_id)
                    st.session_state["maintenance_tabs"] = "完成维护"
                    st.rerun()
                st.markdown("**本轮温度变化**")
                if run is None:
                    st.info("点击“开始仿真”建立本轮曲线。已有设备累计工时和历史记录保留。")
                else:
                    frame = pd.DataFrame(run["samples"])
                    st.caption(f'本轮已计算 {run["steps"]} 步 · 模拟经过 {run["steps"] * state["hours_per_step"]:g} h（含冷却）'
                               f' · 累计运行增加 {run["runtime_hours"]:g} h。曲线只展示本轮最近 200 个点。')
                    values = frame["temperature_c"].tolist() + (list(thresholds) if thresholds else [])
                    lower, upper = min(values), max(values)
                    padding = max(2, (upper-lower)*.12)
                    chart = alt.Chart(frame).mark_line(color="#3975ed", strokeWidth=2.5, point=alt.OverlayMarkDef(size=24)).encode(
                        x=alt.X("simulated_hour:Q", title="本轮模拟经过时间 / h（含停机冷却）",
                                scale=alt.Scale(zero=False), axis=alt.Axis(tickMinStep=1)),
                        y=alt.Y("temperature_c:Q", title="温度 / ℃", scale=alt.Scale(domain=[lower-padding, upper+padding], zero=False)),
                        tooltip=[alt.Tooltip("simulated_hour:Q", title="模拟经过 / h"),
                                 alt.Tooltip("temperature_c:Q", title="温度 / ℃", format=".2f"),
                                 alt.Tooltip("total_runtime_hours:Q", title="累计运行 / h")],
                    ).properties(height=240)
                    if thresholds:
                        rules = alt.Chart(pd.DataFrame({"limit": thresholds, "color": ["#d6a044", "#dc7067"]})).mark_rule(strokeDash=[5, 4]).encode(
                            y="limit:Q", color=alt.Color("color:N", scale=None))
                        chart += rules
                    st.altair_chart(chart.configure_view(stroke=None).configure_axis(gridColor="#eef2f7"), width="stretch")
                with st.expander("模型依据与设备最后记录"):
                    st.write(f'设备最后记录：{device["status"]}。此字段与“演示是否暂停”含义不同。')
                    if thresholds:
                        target = thresholds[1]+5 if state["mode"] == "temperature_anomaly" else thresholds[0]-DEMO_PROFILES[equipment_id][1]
                        st.write(f'运行场景目标温度 {target:g} ℃；停机后向 {DEMO_AMBIENT_C:g} ℃冷却。'
                                 f'预警 {thresholds[0]:g} ℃，严重 {thresholds[1]:g} ℃。目标是演示参数，不代表真实设备。')
                    st.code("T新 = T当前 + 0.25 × (T目标 − T当前)", language=None)
                    st.caption("因此温度会逐渐接近目标并稳定，不是故意生成随机波动。暂停后继续沿用本轮；停机后再次开始建立新曲线。服务重启后本轮曲线清空，数据库历史保留。")
                    st.caption("仿真由服务端后台统一推进，切换页面、关闭浏览器均不停止；暂停会冻结计算。每个数据库在同一服务进程中共享一个驱动器，页面读取不额外推进。")
                    if report["notice"]:
                        st.caption(f'最近操作：{report["notice"]}')
                    if report["last_snapshot"]:
                        snapshot = report["last_snapshot"]
                        saved_at = pd.Timestamp(snapshot["recorded_at"]).tz_convert("Asia/Shanghai")
                        st.caption(f'最近采样 {saved_at:%H:%M:%S}（北京时间）· 运行增量 {snapshot["runtime_increment"]:g} h')
                if report["sync_error"]:
                    st.error(report["sync_error"])
                if report["step_error"]:
                    st.error(report["step_error"])
                if report["last_result"] and report["last_result"]["alarm_sync_skipped"]:
                    st.warning(report["last_result"]["warning"])
            except (ValueError, LookupError, sqlite3.Error, OSError) as error:
                st.error(f"{equipment_id} 仿真暂不可用：{error}")
