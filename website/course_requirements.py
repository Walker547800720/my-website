"""The teacher's stated requirements and the project's concrete evidence routes.

This is an index, not a claim that unexecuted checks have passed.
"""
from simulation.welding_simulator import DEMO_PROFILES, DEMO_ANOMALY_MARGIN_C

COURSE_VERSION = "2026.11.17.R1"

REQUIREMENTS = [
    ("R01", "研究重点", "运维指标选取", "方案与模型中的指标及用途；运行看板中的温度、工时、状态、焊接次数和维护剩余", "说明书第2至3章；本轮equipment、telemetry、maintenance_plans"),
    ("R02", "研究重点", "故障台账方案", "故障台账中的登记、处理状态、原始描述与追加说明；严重温度事件关联故障", "说明书第4至6章；fault_records及处理记录"),
    ("R03", "研究重点", "基于运行时长的维护排程", "维护排程中的周期、提前量、目标与剩余；维护后按实际工时重排", "说明书第3、4、6章；maintenance_plans、maintenance_records、alarm_records"),
    ("R04", "研究重点", "运维信息管理界面", "运行看板、维护排程、故障台账、预警中心、方案与模型五页共用同一轮数据", "说明书第5章；界面截图与跨页验收记录"),
    ("D01", "设计交付要求", "系统总体方案论证", "方案与模型中的场景、目标、方案选择依据与软件分层", "说明书第1至2章；方案与模型页面"),
    ("D02", "设计交付要求", "测点或架构方案设计", "采用通知允许的软件架构路线，明确模拟数据来源、模块职责与数据流", "说明书第2至4章；架构图与输入输出表"),
    ("D03", "设计交付要求", "仿真建模", "温度递推、有效运行累计、产量计算、维护和故障事件控制", "说明书第3章；simulation/welding_simulator.py与website/defense_session.py"),
    ("D04", "设计交付要求", "模拟数据集构建", "独立零工时轮次；实际模型快照入库；冻结后导出七表及数据集说明", "telemetry.csv、运行事件.csv、数据集说明.json及SQLite记录"),
    ("D05", "设计交付要求", "监测软件界面开发", "网页启停与场景选择、温度与工时趋势、查询筛选及业务表单", "说明书第5章；程序源码与界面测试截图"),
    ("D06", "设计交付要求", "功能仿真验证", "正常维护、温度故障、人工故障、边界值、冻结与重启等分别验证", "说明书第6章；05测试与数据中的实际日志和测试工况记录表"),
    ("S01", "最终提交材料", "课程设计说明书", "独立Word材料，集中说明总体方案、模型、界面及验证", "01设计说明/课程设计说明书.docx；附录C"),
    ("S02", "最终提交材料", "仿真文件", "独立Python仿真包，含模型、配置、运行入口、说明及真实结果；网页为运行入口之一", "仿真文件_答辩提交版.zip；仿真模型与运行说明书.docx"),
    ("S03", "最终提交材料", "程序源代码", "完整网站源码及固定依赖，能本地启动同一主系统", "02程序源码；requirements.txt与本地启动入口"),
    ("S04", "最终提交材料", "界面测试截图", "保存本次实际运行的初始、提醒、到期、维护、故障及恢复页面", "04界面截图；本次验收截图索引"),
    ("S05", "最终提交材料", "测试工况记录表", "逐行记录条件、操作、预期、实际结果与证据路径", "05测试与数据/测试工况记录表.xlsx及新增验收记录"),
]


def requirement_records():
    return [dict(zip(("编号", "类别", "通知要求", "实现与展示位置", "验证证据或配套材料"), row)) for row in REQUIREMENTS]


def runtime_guidance(device, state, warning, critical, plan):
    """Explain actual state without generating data or changing business rules."""
    normal_target = warning - DEMO_PROFILES[device['equipment_id']][1]
    anomaly_target = critical + DEMO_ANOMALY_MARGIN_C
    if device['status'] == '故障':
        return f"设备当前为故障状态。先到故障台账保存处理结果，再停机冷却；温度仍超标时预警先确认，恢复后解除。维护到期时还需另行登记维护。"
    if state['state'] == 'paused':
        return "当前演示已暂停，温度、工时和焊接次数保持。按下方暂停原因选择继续运行、故障处理或维护登记。"
    if state['state'] == 'stopped':
        return "当前处于停机冷却，温度向环境温度下降，累计工时与焊接次数保持；接近环境温度后自动暂停。"
    if state['mode'] == 'normal':
        return (f"正常工况目标 {normal_target:g}℃。温度接近目标后显示趋稳属于预期，累计工时与焊接次数仍继续增加；"
                f"本周期在 {plan['next_maintenance_runtime_hours']-plan['reminder_lead_hours']:g}h 提前提醒，"
                f"{plan['next_maintenance_runtime_hours']:g}h 到期。演示超温请切换温度异常场景。")
    return (f"温度异常工况目标 {anomaly_target:g}℃；达到 {warning:g}℃ 警告，达到 {critical:g}℃ 严重。"
            "严重节点会暂停并按同一事件登记一条温度异常故障。")
