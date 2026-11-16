-- All runtime values and maintenance intervals are measured in hours.
-- All timestamps use ISO 8601 UTC.
CREATE TABLE IF NOT EXISTS equipment (
    equipment_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(equipment_id)) > 0),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    total_runtime_hours REAL NOT NULL DEFAULT 0 CHECK (total_runtime_hours >= 0),
    temperature_c REAL NOT NULL,
    welding_count INTEGER NOT NULL DEFAULT 0 CHECK (typeof(welding_count) = 'integer' AND welding_count >= 0),
    status TEXT NOT NULL CHECK (status IN ('已停止', '运行中', '故障', '离线')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY,
    equipment_id TEXT NOT NULL REFERENCES equipment(equipment_id) ON DELETE RESTRICT,
    temperature_c REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('已停止', '运行中', '故障', '离线')),
    total_runtime_hours REAL NOT NULL CHECK (total_runtime_hours >= 0),
    welding_count INTEGER NOT NULL CHECK (typeof(welding_count) = 'integer' AND welding_count >= 0),
    runtime_increment REAL NOT NULL CHECK (runtime_increment >= 0),
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_equipment_time ON telemetry(equipment_id, recorded_at);

CREATE TABLE IF NOT EXISTS fault_records (
    id INTEGER PRIMARY KEY,
    equipment_id TEXT NOT NULL REFERENCES equipment(equipment_id) ON DELETE RESTRICT,
    fault_type TEXT NOT NULL CHECK (length(trim(fault_type)) > 0),
    level TEXT NOT NULL CHECK (level IN ('提示', '警告', '严重')),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    status TEXT NOT NULL DEFAULT '未处理' CHECK (status IN ('未处理', '处理中', '已处理')),
    occurred_at TEXT NOT NULL,
    resolved_at TEXT,
    event_key TEXT CHECK (event_key IS NULL OR length(trim(event_key)) > 0),
    handling_note TEXT NOT NULL DEFAULT '',
    CHECK ((status = '已处理' AND resolved_at IS NOT NULL AND resolved_at >= occurred_at)
        OR (status <> '已处理' AND resolved_at IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_fault_event_key ON fault_records(event_key) WHERE event_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_fault_equipment_time ON fault_records(equipment_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_fault_status_time ON fault_records(status, occurred_at);

CREATE TABLE IF NOT EXISTS maintenance_plans (
    equipment_id TEXT PRIMARY KEY NOT NULL REFERENCES equipment(equipment_id) ON DELETE RESTRICT,
    maintenance_interval_hours REAL NOT NULL CHECK (maintenance_interval_hours > 0),
    last_maintenance_runtime_hours REAL NOT NULL CHECK (last_maintenance_runtime_hours >= 0),
    next_maintenance_runtime_hours REAL NOT NULL CHECK (next_maintenance_runtime_hours > last_maintenance_runtime_hours),
    reminder_lead_hours REAL NOT NULL CHECK (reminder_lead_hours >= 0 AND reminder_lead_hours <= maintenance_interval_hours),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance_records (
    id INTEGER PRIMARY KEY,
    equipment_id TEXT NOT NULL REFERENCES equipment(equipment_id) ON DELETE RESTRICT,
    maintenance_type TEXT NOT NULL CHECK (length(trim(maintenance_type)) > 0),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    runtime_hours REAL NOT NULL CHECK (runtime_hours >= 0),
    performed_by TEXT NOT NULL CHECK (length(trim(performed_by)) > 0),
    performed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_maintenance_equipment_time ON maintenance_records(equipment_id, performed_at);

CREATE TABLE IF NOT EXISTS alarm_records (
    id INTEGER PRIMARY KEY,
    equipment_id TEXT NOT NULL REFERENCES equipment(equipment_id) ON DELETE RESTRICT,
    alarm_type TEXT NOT NULL CHECK (length(trim(alarm_type)) > 0),
    level TEXT NOT NULL CHECK (level IN ('提示', '警告', '严重')),
    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
    status TEXT NOT NULL DEFAULT '未处理' CHECK (status IN ('未处理', '已确认', '已解除')),
    occurred_at TEXT NOT NULL,
    resolved_at TEXT,
    event_key TEXT CHECK (event_key IS NULL OR length(trim(event_key)) > 0),
    CHECK ((status = '已解除' AND resolved_at IS NOT NULL AND resolved_at >= occurred_at)
        OR (status <> '已解除' AND resolved_at IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_alarm_event_key ON alarm_records(event_key) WHERE event_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_alarm_equipment_time ON alarm_records(equipment_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_alarm_status_time ON alarm_records(status, occurred_at);

CREATE TABLE IF NOT EXISTS system_config (
    config_key TEXT PRIMARY KEY NOT NULL CHECK (length(trim(config_key)) > 0),
    config_value REAL NOT NULL CHECK (config_value > 0),
    unit TEXT NOT NULL CHECK (unit IN ('℃', 'h', 'min')),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    is_simulated INTEGER NOT NULL DEFAULT 1 CHECK (is_simulated IN (0, 1)),
    updated_at TEXT NOT NULL
);
