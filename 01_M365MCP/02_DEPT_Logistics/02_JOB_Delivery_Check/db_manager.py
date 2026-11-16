"""
Standard Database Manager entrypoint for `02_JOB_Delivery_Check` (#351).
Re-exports all symbols from `delivery_db_manager` while avoiding `sys.modules['db_manager']`
collisions when `00_JOB_StockAccuracy/historical_backfill_crawler.py` is imported in-process.
"""
from delivery_db_manager import (  # noqa: F401
    BASE_DIR,
    DB_DIR,
    DEFAULT_DB_PATH,
    connect_readonly,
    fetch_barcodes,
    fetch_horizon_parts,
    fetch_truck_plan_lines,
    fetch_truck_runs,
    fetch_verdicts,
    get_connection,
    has_notice,
    init_database,
    record_notice,
)
