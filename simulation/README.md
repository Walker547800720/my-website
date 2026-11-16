> 当前网站采用单设备零工时答辩入口，仅调用WELD-001配置；本文描述可复用的底层模型接口。单设备会话的心跳、节点暂停、结束冻结由website/defense_session.py实现，具体操作见../docs/单设备答辩操作说明.md。

# 焊接设备运行仿真（5.4.2）

此模块只提供调用接口，不创建线程或定时任务，不接入网站。
所有模型参数均为课程演示值，不代表真实设备性能。构造时必须显式传入已有数据库路径，
不会初始化或重置数据库。每台设备使用一个 `WeldingSimulator` 实例；同一设备应复用该实例。

```python
from simulation.welding_simulator import WeldingSimulator

sim = WeldingSimulator("WELD-001", db_path="测试数据库路径")
sim.start()                       # normal；可传 mode="temperature_anomaly"
result = sim.step()               # 满2秒才计算一次，否则返回None
sim.set_mode("temperature_anomaly")
sim.pause()                      # 冻结仿真，数据库设备状态不变
sim.start()                      # 恢复，重新计时
sim.stop()                       # 运行设备转为已停止
# 停止后继续按两秒调用step，保存零增量冷却快照。
```

- 两秒现实时间代表一小时模拟时间；单次调用最多一步，不追补漏过的节拍。
  使用单调时钟；测试注入假时钟，不实际等待。重复启动不会重置正在运行的节拍。
- 数据库状态为“运行中”时，每步增加1h；WELD-001/002/003分别增加20/24/18次焊接。
  其他状态增量均为0。故障/离线不被启动或停止覆盖，也不自动生成或处理故障记录。
- 温度计算：`新温度 = 当前温度 + 0.25 × (目标温度 - 当前温度)`。
  正常目标分别低于预警配置12/10/14℃；异常目标为严重阈值+5℃。
  非运行设备向25℃冷却，低于25℃时保持原温度。切回normal可逐步恢复正常温度。
- 优先读取实际温度配置；仅缺失时使用演示阈值80/90℃，返回明确标记。
  原预警服务在配置缺失时拒绝执行，因此此时只计算温度和维护状态，不生成预警，
  返回`alarm_sync_skipped=True`及原因；不补写配置。不合法配置直接报错。
- 本任务两秒/小时的节拍独立于旧`simulation_step_minutes`配置，不修改该旧配置。
- 调用`get_equipment`、`update_equipment_status`和`get_faults_by_equipment`管理启动条件；
  `save_telemetry`原子更新累计值与快照；`get_maintenance_plan`和`check_alarms`
  沿用维护、预警判定。暂停、模式、时钟状态仅在实例内存中保存。
- 服务接口各自提交事务，模型不伪装跨服务的整体事务。快照提交后预警同步若失败，
  抛`SimulationSyncError`，其中`telemetry`可用于确认已保存的数据；本节拍已消耗，
  重复调用不重复累计。调用方可直接用现有`check_alarms`重试同步。

运行测试：`python -m unittest discover -s tests -v`。
