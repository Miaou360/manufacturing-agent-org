# 🔗 04_JOB_SystemReconciliation (WMS vs SAP 시스템 재고정확도 정합성 허브)

## 📌 작업 개요 (Executive Summary)
- **부서**: `01_DEPT_Inventory` (재고관리부서)
- **역할**: SAP GUI `/nztcode_stock_rec` (`radRA5` -> `radRA1`) 트랜잭션을 통해 매일 아침 06:00 집계 완료되는 **전일 마감 공식 WMS vs SAP 시스템 재고정확도 리스트**를 06:35 AM EST에 자동 추출하여 `db/system_reconciliation.db`에 적재하고, 전사 재고정확도 지표, 재무 리스크 TOP 5 품목 및 **AI 자동화(MB1B 311)의 인과적 기여도(Attribution)**를 정량 분석하는 핵심 정합성 허브입니다.
- **실행 스케줄**: 매일 아침 **06:35 AM EST** (Windows 작업 스케줄러: `SCM_Agent_Daily_System_Reconciliation`)

---

## 🏗️ 3각 재고 정합성(Triangular Reconciliation) 위계

```text
       [00_JOB_StockAccuracy]
       (WMS vs Floor Physical)
            /        \
           /          \
[04_JOB_SystemReconciliation] ── [01_JOB_GR_GI_Error & 02_JOB_Backlog_Clear]
      (WMS vs SAP ERP)             (SAP SL00 ↔ SL01 수불 불일치 자동 해소)
           │
           └───> [03_JOB_Backlog_Analysis] (주간 결함 원인 진단 결합)
```

---

## 🚀 파이프라인 구성 요소

| 파일명 | 역할 및 표준 아이콘 | 설명 |
| :--- | :--- | :--- |
| [run_systemreconciliation.py](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/01_DEPT_Inventory/04_JOB_SystemReconciliation/run_systemreconciliation.py) | 🚀 `rocket` (엔트리포인트) | 파이프라인 전체 오케스트레이션 및 CLI 런처 (`--plant`, `--system`, `--skip-extract`, `--report-only`) |
| [reconciliation_collector.py](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/01_DEPT_Inventory/04_JOB_SystemReconciliation/reconciliation_collector.py) | 🤖 `robots` (RPA 수집기) | SAP GUI ZTCODE_STOCK_REC radRA5 -> radRA1 자동 실행 및 무충돌 타임스탬프 원천 텍스트 추출 (`raw_exports/`) |
| [reconciliation_engine.py](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/01_DEPT_Inventory/04_JOB_SystemReconciliation/reconciliation_engine.py) | ⚡ `zap` (코어 분석 엔진) | UTF-16LE ALV 덤프 고속 파싱, 수량/금액 불일치 스코어링 및 `01/02 JOB` 조치 품목의 익일 100% 해소 기여도(Attribution) 역추적 |
| [ztcode_stock_backfill_collector.py](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/01_DEPT_Inventory/04_JOB_SystemReconciliation/ztcode_stock_backfill_collector.py) | ⚡ `zap` (과거 백필/학습기) | SAP ZTCODE_STOCK_REC 과거 주간/일일 시계열 데이터 자동 수집 및 품목별 통계 학습 모델(`reconciliation_material_baseline`) 생성·강화 |
| [reconciliation_notifier.py](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/01_DEPT_Inventory/04_JOB_SystemReconciliation/reconciliation_notifier.py) | ⚙️ `workflow` (리포트 생성기) | 일일 공식 리포트 생성 (`reports/Reconciliation_Report_%Y%m%d.md`), 다중 시트 Excel (`.xlsx`) 및 Outlook 임원용 HTML 브리핑 |
| [db_manager.py](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/01_DEPT_Inventory/04_JOB_SystemReconciliation/db_manager.py) | 🗄️ `database` (DB 매니저) | SQLite WAL 모드 기반 일일 요약, 품목별 스냅샷, 만성 불일치 레지스트리, 5등급 지속 학습 베이스라인 영구 저장 및 타 JOB용 연동 API 제공 |

---

## 📊 핵심 DB 스키마 (`db/system_reconciliation.db`)

1. **`accuracy_daily_summary`**: 일자별 공식 정확도(%), 모수 품목수, 일치/불일치 건수, 총 차이수량, 총 재무 위험금액($), 전일 대비 변동폭(DoD Delta)
2. **`accuracy_material_snapshot`**: 품목별 WMS 수량, SAP 수량, 부호형 차이수량(`wms_qty - sap_qty`), 정확도 점수(0~100점), 단가, 재무 위험금액($), 저장위치
3. **`chronic_mismatch_registry`**: 3일 이상 지속되는 만성 불일치(Ghost Inventory) 품목 추적 및 현장 실사(Cycle Count) 추천 여부
4. **`ai_attribution_log`**: 전일 AI 조치(MB1B 311) 품목의 익일 ZTCODE_STOCK_REC 100% 정합성 전환 성공률 및 공식 정확도 상승 기여분(+%p)
5. **`reconciliation_material_baseline`** (🤖 **지속 학습 통계 모델**):
   - 품목별 누적 분석 횟수(`sample_snapshots`), 완전 일치율(`match_rate`), 평균 오차(`avg_diff_qty`), 오차 표준편차(`std_diff_qty`), 평균 재무 노출액(`avg_financial_exposure`)
   - **5대 품목 성향 분류 (Classification Tiers)**:
     * `CHRONIC_PERFECT`: 95% 이상 항시 일치 (정상 모범 부품)
     * `CHRONIC_GHOST_DEFICIT`: SAP 장부 재고가 현장 WMS보다 만성적으로 많음 (SAP 허수 유령 재고 / 스크랩 누락) ➔ `00_JOB` 실사 및 감액 표적
     * `CHRONIC_OVER_STOCK`: 현장 WMS 실물이 SAP 장부보다 많음 (입고 미전표 / 이동 오더 지연)
     * `VOLATILE_FLUCTUATING`: 시계열 분산이 큼 (생산 투입 및 출고 시차가 격심한 부품)
     * `SPORADIC_DISCREPANCY`: 일시적/경미한 단순 시차성 불일치
   - **지속 강화 원칙**: 매일 아침 ZTCODE_STOCK_REC가 DB에 적재될 때마다 자동으로 모델 재연산 및 강화 수행

---

## 🏷️ 위탁 재고(Vendor Consignment) 착시 배제 원칙

- **배제 대상**: 자재 유형 `ROH2` (레진 원자재), `ROH3` (도료 및 표면처리 화학품) 및 품번 접두사 `74-*`, `89-*`에 해당하는 총 41개 품목
- **배제 사유 (Root Cause)**:
  * 레진 사일로(Silo) 및 화학품 벌크 보관 특성상 WMS 실물 재고와 SAP 장부(SL00 등) 간에 정산 주기 및 위탁 자재 인출(Consignment Draw) 시차가 구조적으로 발생합니다.
  * 이로 인해 `TEST-0001`, `TEST-0002` 등 특정 품목이 매일 약 45만 달러(전체 리스크의 약 50%) 이상의 거대한 **가짜 재무 리스크(착시)**로 랭킹 1, 2위를 독점하는 왜곡이 발생했습니다.
- **적용 결과**:
  * 파싱 엔진([reconciliation_engine.py](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/01_DEPT_Inventory/04_JOB_SystemReconciliation/reconciliation_engine.py)) 및 DB 조회 쿼리([db_manager.py](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/01_DEPT_Inventory/04_JOB_SystemReconciliation/db_manager.py))에서 자동 제외 필터링.
  * 가짜 노출액 $450,000+ 가 제거되고, 공장 공식 MM 정확도가 **96.58% ➔ 97.08%**로 현실화되었으며, 실제 현장 실사가 시급한 고가 양산 전장 모듈 및 램프 부품이 Top 5 리스크 및 만성 유령 재고 1순위로 올바르게 식별됩니다.

---

## 🔗 타 JOB과의 4대 시너지 (Inter-Job Synergies)

1. **시너지 ①: `00_JOB_StockAccuracy` 실사 타겟 연계**
   - `get_chronic_ghost_from_baseline()` API를 통해 누적 학습된 만성 유령 재고(SAP 허수) 품목을 `00_JOB` 1직 실사 리스트의 최우선 카운팅 대상으로 자동 피딩.
2. **시너지 ②: `01_JOB_GR_GI_Error` & `02_JOB_Backlog_Clear` ROI 증명**
   - 전일 우리 시스템이 실행한 MB1B 311 전표 품목이 오늘 아침 ZTCODE_STOCK_REC에서 실제로 100% 일치 상태로 전환되었는지 1:1 대조하여 **"AI 파이프라인의 정확도 개선 기여율(+X.XX%p)"**을 자동 산출.
3. **시너지 ③: `03_JOB_Backlog_Analysis` 원인 진단 결합**
   - `get_material_accuracy_profile(mat)` API를 통해 백로그 발생 품목이 (1) 창고엔 있으나 라인 이동이 지연된 시스템 병목인지, (2) 공장 전체에 자재가 없는 진짜 결품인지, (3) SAP 장부에만 있는 유령 재고인지 3단계 자동 판별.
4. **시너지 ④: `00_JOB_StockAccuracy` Missing GI 베이스라인과 결합**
   - Missing GI 학습 모델과 ZTCODE_STOCK_REC 베이스라인을 크로스 체킹하여, 장기 불일치의 원인이 패키징 단위 오차(박스당량 차이)인지, 라인 내 미전표 실물 소진인지 정밀 판별.

