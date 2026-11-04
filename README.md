# Manufacturing SCM & Inventory Multi-Agent Organization (`manufacturing-agent-org`)

An autonomous multi-agent operations architecture for manufacturing supply chain management (SCM), warehouse inventory accuracy, ERP-WMS interface error resolution, and production backlog clearing, integrated with Microsoft 365 (MCP), SAP GUI Scripting, and Selenium WMS automation.

Repository: https://github.com/Miaou360/manufacturing-agent-org

---

## Architecture Overview

```text
01_M365MCP/
├── 00_HQ_TaskManager/               # Shared governance, SAP/Selenium locks, env & security guards
│   ├── git_auto_sync.py             # Pre-commit secret/PII/binary scanner (GitGuard)
│   ├── persistence_guard.py         # Multi-snapshot variance persistence guardrail
│   ├── sap_healer.py / sap_lock.py  # SAP GUI session recovery & single-writer mutex
│   ├── wms_mapping.py               # Storage location (SLoc) & settled-state objective function
│   └── ztcode_stock_guard.py        # ERP GR/GI interface collision & chain protection guard
└── 01_DEPT_Inventory/               # Autonomous Inventory & Warehouse Operations Department
    ├── 00_JOB_StockAccuracy/        # Daily/Weekly risk-scored & stratified cycle-count engine + LightGBM pipeline
    ├── 01_JOB_GR_GI_Error/          # Continuous 30m/1h ERP-WMS GI/GR interface error classification & auto-clearing
    ├── 02_JOB_Backlog_Clear/        # COGI production backlog root-cause engine & accuracy-preserving MB1B resolver
    ├── 03_JOB_Backlog_Analysis/     # MB51 movement decomposition & longitudinal backlog analytics mart
    ├── 04_JOB_SystemReconciliation/ # Plant-wide SAP vs. WMS stock reconciliation & variance tracking
    ├── 05_JOB_SL01_R_Bins/          # Receiving/return staging bin aging & clearance automation
    └── 06_JOB_Data_Collector/       # Daily SAP MB51 material document ledger collector & retention manager
```

## Key Design Principles

1. **Zero-Collision Concurrency & Resource Guardrails**: Single-instance Selenium/SAP GUI mutexes, SQLite WAL mode, read-only URI inspection, and bounded worker pools (`<= 4` workers) so background jobs never contend with foreground workstation apps.
2. **Accuracy-Preserving ERP Automation**: Every automated transfer (`MB1B 311/312`) is gated by a multi-snapshot persistence check, active GR/GI interface chain protection, and a strict plant-wide accuracy objective function (`Delta_Accuracy >= 0`).
3. **Ground-Truth Fact Integrity**: Cycle-count results store only verified physical counts; unverified fields remain `NULL` and weak proxies are strictly isolated for ML experimentation.
4. **Configuration & Secret Isolation**: Zero credentials, internal hostnames, customer/vendor names, or real part numbers in source code. All environment-specific parameters are injected exclusively via uncommitted `.env` (`.env.example` template provided).

## Quick Start

1. **Install Dependencies**:
   ```bash
   pip install pandas openpyxl pyyaml python-dotenv pytest lightgbm scikit-learn
   ```
2. **Configure Environment**:
   ```bash
   cp .env.example 01_M365MCP/.env
   # Fill in local SAP/WMS/M365 settings in 01_M365MCP/.env (never committed)
   ```
3. **Install Pre-Commit Security Guard**:
   ```bash
   python 01_M365MCP/00_HQ_TaskManager/git_auto_sync.py install-hook --repo .
   ```
4. **Run Unit Test Suite (Synthetic Fixtures)**:
   ```bash
   pytest 01_M365MCP/00_HQ_TaskManager/tests
   pytest 01_M365MCP/01_DEPT_Inventory/01_JOB_GR_GI_Error/tests
   ```

---

## 한국어 요약 (Overview in Korean)

제조 현장의 **SAP ERP — WMS(창고관리시스템) — Microsoft 365** 간 데이터 정합성과 재고 정확도를 자율 유지하기 위해 설계된 가상 조직형 멀티 에이전트 파이프라인(`00_HQ` 본부 + `01_DEPT_Inventory` 산하 `00`~`06` 실무 JOB)입니다. 모든 민감 설정·계정·호스트·실데이터는 `.env` 환경변수로 분리되어 있으며, 합성 픽스처 기반 단위 테스트(`pytest`)와 커밋 전 보안 스캐너(`GitGuard`)를 포함합니다.

## License

MIT License — Copyright (c) 2026 Seunghun Yea. See [LICENSE](LICENSE).
