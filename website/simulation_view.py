"""Explain driver state separately from the persisted equipment record."""


def control_view(state, device_status, has_run=False):
    blocked = device_status in ("故障", "离线")
    if state == "paused":
        label, tone = ("演示已暂停", "amber") if has_run else ("尚未开始", "neutral")
        hint = "工时、产量和温度计算均冻结。设备最后记录保留，不表示仍在产生数据。"
    elif state == "stopped":
        label, tone = "停机冷却中", "neutral"
        hint = "不累计运行工时和焊接次数，温度按模型逐步下降。"
    elif blocked or device_status == "已停止":
        label, tone = f"{device_status} · 仅冷却", "red" if blocked else "neutral"
        hint = "设备未运行，只计算冷却；工时和产量均不增加。"
    else:
        label, tone = "仿真推进中", "green"
        hint = "每满 2 秒推进 1 个模拟小时，运行工时与焊接次数同步累计。"
    if blocked:
        label = f"{device_status} · 演示暂停" if state == "paused" else f"{device_status} · 仅冷却"
        tone = "red"
        hint = "设备故障或离线，禁止启动且不累计工时产量。" + ("演示已冻结。" if state == "paused" else "当前只模拟冷却。")
    start_label = "仿真推进中" if state == "running" and device_status == "运行中" else (
        "继续仿真" if state == "paused" and has_run else "开始仿真")
    return {"label": label, "tone": tone, "hint": hint, "start_label": start_label,
            "start_disabled": blocked or (state == "running" and device_status == "运行中"),
            "pause_disabled": state == "paused",
            "stop_disabled": state == "stopped" and device_status != "运行中"}
