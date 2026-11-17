"""Read consistent session evidence; no fabricated observations or pass counts."""
import csv
from datetime import datetime, timezone
import io
import json
import zipfile

import pandas as pd

from database.db import get_connection
from simulation.welding_simulator import STEP_SECONDS, HOURS_PER_STEP, DEMO_RESPONSE
from website.course_requirements import COURSE_VERSION, requirement_records

TABLES = ('equipment', 'telemetry', 'fault_records', 'maintenance_plans', 'maintenance_records', 'alarm_records', 'system_config')


def evidence_zip(session):
    """Serialize the frozen dataset and lifecycle annotations under the session lock."""
    with session._lock:
        with get_connection(session.db_path) as connection:
            connection.execute('BEGIN')
            tables = {name: pd.read_sql_query('SELECT * FROM '+name, connection) for name in TABLES}
            integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
            foreign_keys = connection.execute('PRAGMA foreign_key_check').fetchall()
        info = json.loads(json.dumps(session.info, ensure_ascii=False))
        telemetry = tables['telemetry'].sort_values('id')
        first = telemetry.iloc[0].to_dict() if len(telemetry) else None
        last = telemetry.iloc[-1].to_dict() if len(telemetry) else None
        delta = float(telemetry['runtime_increment'].sum()) if len(telemetry) else 0.0
        cumulative = float(last['total_runtime_hours']-first['total_runtime_hours']) if first else 0.0
        manifest = {
            '软件版本': COURSE_VERSION,
            '生成时间UTC': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            '数据来源': '本轮Python规则模型与业务表单实际生成；不是真实焊机采集数据',
            '本轮编号': info['id'], '本轮已冻结': bool(info['ended']),
            '单位': {'温度':'℃','工时及维护周期':'h','焊接次数':'次','数据库与事件时间':'UTC'},
            '模型参数': {'现实节拍秒':STEP_SECONDS,'每步模拟小时':HOURS_PER_STEP,'温度响应系数':DEMO_RESPONSE},
            '各表行数': {name:len(frame) for name,frame in tables.items()},
            '实际运行增量h':delta, '首末累计工时差h':cumulative,
            '数据质量核对': {'数据库完整性':integrity,'外键错误数':len(foreign_keys),
                           '增量与累计差一致':abs(delta-cumulative)<1e-9},
            '说明': ['telemetry含初始零增量和停机冷却快照；暂停不产生新快照。',
                   '按快照序号理解模拟经过时间，每个计算快照代表1小时，包含零运行增量冷却。',
                   '场景及控制操作按session.json的事件边界追溯；不会用最终场景覆盖全部历史。',
                   'fault_records及alarm_records是导出时状态，升级、处理与解除可能已发生；应结合时间线和阶段证据解释。',
                   '原始数据的检查结果不代表未执行的工况通过；课程外部材料分别提交。']
        }
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, frame in tables.items():z.writestr(name+'.csv',frame.to_csv(index=False).encode('utf-8-sig'))
            z.writestr('session.json',json.dumps(info,ensure_ascii=False,indent=2))
            z.writestr('数据集说明.json',json.dumps(manifest,ensure_ascii=False,indent=2))
            z.writestr('运行事件.csv',pd.DataFrame(info['events']).to_csv(index=False).encode('utf-8-sig'))
            requirements=requirement_records();s=io.StringIO(newline='');writer=csv.DictWriter(s,fieldnames=list(requirements[0]));writer.writeheader();writer.writerows(requirements)
            z.writestr('课题要求与证据索引.csv',s.getvalue().encode('utf-8-sig'))
            z.writestr('数据说明.txt','课程规则仿真数据，非真实设备测量。温度℃、工时h、焊接次数次，数据库时间UTC。每2秒最多计算1模拟小时，不追补延迟。七表CSV保存导出时真实内容；数据集说明.json给出单位、计数、参数与质量核对，session.json及运行事件.csv保存生命周期。外部说明书、源码、截图和工况表按课题要求分别提交。')
        return output.getvalue()
