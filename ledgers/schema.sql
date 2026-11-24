-- Append-only snapshot ledger schema for the synthetic plant simulation.
-- Every table preserves historical loads keyed by load_seq and snapshot_id.
-- Reads across ledgers use SQLite read-only URI mode (?mode=ro).

CREATE TABLE IF NOT EXISTS ledger_provenance (
    load_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    source_name TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    row_count INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_provenance_table_time
    ON ledger_provenance (table_name, captured_at);

CREATE TABLE IF NOT EXISTS action_log (
    action_id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    performed_by TEXT NOT NULL,
    started_at TEXT NOT NULL,
    inputs_ref TEXT NOT NULL,
    guard_results TEXT NOT NULL,
    outcome TEXT NOT NULL,
    approval TEXT NOT NULL,
    outputs_ref TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_action_log_type_time
    ON action_log (action_type, started_at);

CREATE TABLE IF NOT EXISTS material_master (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    material_id TEXT NOT NULL,
    description TEXT NOT NULL,
    unit_pack_qty INTEGER NOT NULL,
    small_part INTEGER,
    turnover_days_trimmed_mean REAL,
    turnover_days_median REAL,
    mover_class TEXT,
    profile_as_of TEXT
);

CREATE TABLE IF NOT EXISTS wms_box_snapshot (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    barcode TEXT NOT NULL,
    material_id TEXT NOT NULL,
    qty INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    bin_code TEXT NOT NULL,
    sloc TEXT NOT NULL,
    status TEXT NOT NULL,
    dwell_days REAL
);

CREATE INDEX IF NOT EXISTS idx_box_snapshot_id
    ON wms_box_snapshot (snapshot_id, bin_code);

CREATE TABLE IF NOT EXISTS bin_master (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    bin_code TEXT NOT NULL,
    facility TEXT NOT NULL,
    zone_code TEXT NOT NULL,
    rack_level INTEGER NOT NULL,
    count_scope TEXT NOT NULL,
    last_counted_at TEXT,
    skip_until TEXT
);

CREATE TABLE IF NOT EXISTS zone_master (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    zone_code TEXT NOT NULL,
    median_dwell_days REAL,
    lookback_days INTEGER
);

CREATE TABLE IF NOT EXISTS sloc_master (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    sloc TEXT NOT NULL,
    facility TEXT NOT NULL,
    count_eligible INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_position_snapshot (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    material_id TEXT NOT NULL,
    sloc TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    erp_qty REAL NOT NULL,
    wms_qty REAL NOT NULL,
    diff_qty REAL NOT NULL,
    consecutive_days_different INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stock_pos_mat_time
    ON stock_position_snapshot (material_id, sloc, captured_at);

CREATE TABLE IF NOT EXISTS count_task (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    run_id TEXT NOT NULL,
    bin_code TEXT NOT NULL,
    list_date TEXT NOT NULL,
    lane TEXT NOT NULL,
    rank INTEGER NOT NULL,
    risk_score REAL,
    reason TEXT,
    book_boxes INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS count_result (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    run_id TEXT NOT NULL,
    bin_code TEXT NOT NULL,
    counted_boxes INTEGER,
    diff_boxes INTEGER,
    cause_class TEXT,
    field_notes TEXT
);

CREATE TABLE IF NOT EXISTS interface_error_snapshot (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    error_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    movement_type TEXT NOT NULL,
    sloc TEXT NOT NULL,
    qty REAL NOT NULL,
    receipt_linked INTEGER NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_transfer (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    document_no TEXT NOT NULL,
    material_id TEXT NOT NULL,
    from_sloc TEXT NOT NULL,
    to_sloc TEXT NOT NULL,
    qty REAL NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    resolves_error_id TEXT,
    resolves_backlog_line_id TEXT
);

CREATE TABLE IF NOT EXISTS backlog_snapshot (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    backlog_line_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    order_no TEXT NOT NULL,
    sloc TEXT NOT NULL,
    qty REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_master (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    trailer_cap_carts INTEGER NOT NULL,
    truck_slots TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_order_snapshot (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    order_no TEXT NOT NULL,
    material_id TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    due_window TEXT NOT NULL,
    ordered_boxes INTEGER NOT NULL,
    truck_run_id TEXT
);

CREATE TABLE IF NOT EXISTS truck_run (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    truck_run_id TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    run_date TEXT NOT NULL,
    slot TEXT NOT NULL,
    planned_carts INTEGER,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goods_receipt (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    barcode TEXT NOT NULL,
    material_id TEXT NOT NULL,
    qty INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    erp_posted INTEGER NOT NULL,
    truck_run_id TEXT
);
