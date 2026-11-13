# 06_JOB_Data_Collector — 전사 원천 데이터 수집기 (SAP MB51 · WMS GR/GI · Pulling)

## 1. 목적 및 설계 원칙 (D104)
- **단일 원천 수집소 (`D104-1`)**: SAP `MB51`(자재전표 이력), WMS `GR List`·`GI List`(바코드 스캔 이력), WMS `PullingList`(생산 계획·실적)를 순차적으로 수집하여 **`06_JOB_Data_Collector/db/ops_raw.db`**에 적재합니다.
- **읽기 전용 공유**: `06_JOB`만 `ops_raw.db`에 쓰기(Single Writer, WAL 모드)를 수행하며, `00`·`01`·`02`·`03`·`04`·`05` 등 타 JOB은 `?mode=ro` 읽기 전용으로만 참조합니다.
- **민감정보 격리**: 모든 시스템 접속 정보(`SAP_PLANT`, `WMS_BASE_URL` 등)는 루트 `.env`에서 주입받으며, 원본 엑셀/텍스트 파일 및 `.db` 파일은 `.gitignore`로 Git 추적에서 원천 배제합니다.

## 2. 수집 대상 및 스케줄
1. **1단계 — `sap_mb51` (SAP 자재전표 이력, `D104-2 ~ D104-5`)**:
   - **일일 정기 수집**: 매일 **`02:15 EDT`** (01 야간 정시 회차 `02:00~02:05` 직후, `05` 동결 `03:45` 이전 유휴 구간)
   - **수집 범위**: 전날 하루치(`00:00 ~ 23:59 EDT`) + 소급 전기 대비(입력일 기준 조회 또는 최근 7일분 중복 제거 병합)
   - **초기 백필 (`D104-3`)**: `2026-10-01`부터 하루 단위로 분할 수집 (워터마크 `collect_watermark` 기반 실패 지점 자동 이어받기)
2. **2단계 — `wms_gr_list` · `wms_gi_list` (WMS 입출고 바코드 스캔 이력)**:
   - MB51 안정화 후 순차 구현
3. **3단계 — `wms_pulling` (WMS PullingList 생산 계획 `PROD.PLAN` vs 실적 `PROD.QTY`)**:
   - 2단계 완료 후 순차 구현

## 3. 시스템 보호 및 동결 구간 회피 규칙
- **시간대 통일**: 모든 수집·적재 시각(`collected_at_edt`, `started_at_edt`, `finished_at_edt`)은 미국 동부 현지 시각(`America/New_York`, `EDT/EST`) 문자열(`YYYY-MM-DD HH:MM:SS`)로 통일합니다.
- **SAP 전역 락 준수**: SAP GUI 접근 시 `00_HQ_TaskManager/sap_healer.py`의 전역 파일 락을 획득합니다.
- **타 JOB 동결(Blackout) 구간 자동 회피**:
  - `01_JOB` 정시/반시 회차: 매시 `:58 ~ :06`, 주간(`07:00~17:59`) `:28 ~ :36`
  - `05_JOB` 새벽 스캔: `03:45 ~ 04:30`
  - `02` / `04` / `00` 아침 피크 구간: `05:55 ~ 07:10`, `08:10 ~ 08:25`
- **워터마크 이어받기 (`collect_watermark`)**: 일자별 수집 완료 시에만 `last_completed_date`를 전진시키며, 중단·실패 시 다음 실행에서 미완료 날짜부터 자동 재개합니다.

## 4. `ops_raw.db` 핵심 스키마
1. `mb51_raw`: SAP MB51 자재전표 원문 23개 열 + 정규화 날짜/시각 + 고유키(`mat_doc_no, mat_doc_item, mat_doc_year`)
2. `mvt_type_dict`: 이동유형 사전 (`101/102`, `261/262`, `311/312`, `901/902` 등 35개 시드 + 미등록 이동유형 발견 시 `UNDEFINED` 자동 등록)
3. `collect_runs`: 수집 실행 이력 (`run_id`, `source_name`, `target_date_from`, `target_date_to`, `row_count`, `elapsed_sec`, `status`)
4. `collect_watermark`: 소스별 마지막 성공 날짜 및 이어받기 상태

