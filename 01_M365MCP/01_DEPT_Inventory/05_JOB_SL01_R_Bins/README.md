# 05_JOB_SL01_R_Bins (SL01 R-Bin 이동 지연 모니터링)

> **소속 부서**: `01_DEPT_Inventory` (재고관리부)  
> **공장 기준**: Plant `P001` (Headlamp)  
> **실행 주기**: 월~토 매일 새벽 04:15 AM (일요일 제외)  
> **스케줄 독립성**: `00_JOB_StockAccuracy`의 04:00 AM WMS 파일 수집과 분리된 독립 스케줄(04:15 AM)로 기동되며, 가장 최근 수집된 WMS 스냅샷을 기반으로 실행  
> **공식 수신인 (TO)**: `.env`의 `STOCK_ACCURACY_TO`  
> **공식 참조인 (CC)**: `.env`의 `STOCK_ACCURACY_CC`  
> **데이터베이스**: `db/sl01_r_bins.db` (`SL01_R_BIN_OBSERVATION`, `SL01_R_BIN_OBSERVATION_HISTORY`, `SL01_R_BIN_SCAN_RUN`)  

---

## 🛡️ 배경 및 운영 원칙 (Domain Ground Rules & D87 Logic)

### 1. SL01 + R 빈의 물리적 성격 (이동 중 상태)
- WMS상 저장위치가 **`SL01` (Main parts, Plant 1 생산 자재)**이면서 빈 번호가 **`R` (Rack, Plant 3 하이랙)**로 잡혀 있는 자재는, 하이랙에서 생산 조립 라인이나 리팩 대차(Cart)로 불출되어 **물리적으로 이동 중인 상태**를 나타냅니다.
- 정상적인 생산 흐름에서는 불출 당일 라인 투입 또는 지정 대차로 이동 전산 스캔이 완료되어 SL01 R-빈에서 해소되어야 합니다.

### 2. D87/D88/D89 정제 규칙 (User Decisions D87, D88, D89)

- **D87-2: 체류일 계산 시 일요일 제외 영업일 적용 (토요일 포함)**:
  - 공장 특성상 토요일은 자재 불출 및 보충 작업이 진행되므로 정상 영업일로 포함합니다.
  - 일요일(공장 휴무)만 체류 일수 계산에서 엄격히 제외합니다 (`weekday != 6`).

- **D87-3: 재등장 바코드 새로 시작 및 이력 보존 (Episode Tracking)**:
  - 과거에 SL01 R-빈에 머물다 정상 불출되어 `status = 'CLEARED'` 처리되었던 바코드가 동일 바코드로 다시 등장한 경우, 이전 체류일을 누적하지 않고 **`first_seen_date = scan_date`, `days_in_sl01_r = 0`으로 새로 시작**합니다.
  - 다회차 재등장 추적을 위해 `episode` 번호를 1씩 증가시키며, 이전 회차의 관찰 기록은 `SL01_R_BIN_OBSERVATION_HISTORY`에 영구 보존합니다.

- **D88-1: 신호별 리스트 체류 기준 (Tiered Threshold)**:
  - 🔴 `Refilled` $\ge 1$ 영업일
  - 🟣 `Dup Elsewhere` $\ge 1$ 영업일
  - 🟠 `Same Part` $\ge 2$ 영업일
  - `-` (신호 없음) $\ge 2$ 영업일

- **D88-2: 교차 빈 신호 (Dup Elsewhere) 및 지속성(Persistence) 가드**:
  - SL01 R 바코드와 **같은 품번·같은 수량**이 **다른 R빈에 SL02**로 존재하는 짝을 추적합니다 (`SL01_R_BIN_CROSS_PAIR`).
  - 이 짝이 **다음 04:15 스냅샷에도 그대로 있을 때만 (하루 이상 지속 시)** 🟣 신호로 표시합니다 (첫날은 DB 기록만 수행).
  - **신호 우선순위 및 정렬**: 🔴 $\to$ 🟣 $\to$ 🟠 $\to$ `-`.
  - 한 행에 🔴와 🟣가 모두 해당하면 **`🔴🟣`**로 표시하고 🔴 우선순위(Priority 1) 및 $\ge 1$일 기준을 적용합니다.

- **D89: 신호 열 아이콘 전용 및 흑백 인쇄 대비 서식 (Thread 113, 115)**:
  - **Signal 열은 아이콘만 표시**: `🔴`, `🟣`, `🟠`, `🔴🟣`, `-` (문구 삭제).
  - **흑백 인쇄 대비 셀 배경 채우기**: 현장 흑백 출력 시 명암 구분을 위해 연한 배경색 적용 (🔴 `#FCE4D6`, 🟣 `#EDE9FE`, 🟠 `#FFF2CC`).
  - **표 아래 한 줄 영어 범례**: `🔴 bin refilled with other part · 🟣 same part/qty in another R bin (SL02) · 🟠 same part remains in bin`.
  - **12개 열 구성 (전면 영어화)**: `No.`, `Signal`, `Bin`, `Part`, `Description`, `Qty`, `Lot`, `First Seen`, `Days`, `Barcode`, `SL02 in Bin`, `Other Bin`.

---

## 📊 주요 산출물

1. **현장 실사용 세로 인쇄 엑셀 리포트**:
   - `reports/SL01_R_Bins_Lag_Report_%Y%m%d_%H%M%S.xlsx` (타임스탬프 고유 보존)
   - `reports/SL01_R_Bins_Lag_Report_latest.xlsx` (최신 심볼릭 복사본)
2. **Outlook 이메일 자동 발송 (100% 영어, 간결한 헤더)**:
   - D88-1 체류 기준 초과 자재 **전체 전수 검사 목록 (Full Census List, Top N 슬라이싱 없음)**.
   - 상단 KPI 요약 카드 (`Stalled Pallets`, `🔴 Refilled`, `🟣 Dup Elsewhere`, `🟠 Same Part`, `Stalled Total Qty`).
   - 12열 전수 리포트 첨부 (`Other Bin` 포함).

---

## 🔒 격리 및 테스트 가드레일 (Test Guardrails)

- **운영 DB/리포트 오염 방지**:
  - 단위 테스트나 임시 테스트 시에는 반드시 `scratch/` 또는 임시 디렉토리의 DB 경로(`db_path`)와 리포트 경로(`output_dir`)를 지정하여 실행해야 하며, 운영 `db/sl01_r_bins.db`나 `reports/`에 쓰기를 수행하지 않습니다.
- **테스트 이메일 사용자 단독 수신 (AGENTS.md Rule 10)**:
  - `--test` 플래그 실행 시 운영 수신인(`STOCK_ACCURACY_TO/CC`)을 원천 배제하고 `.env`의 `TEST_EMAIL_RECIPIENT`로만 단독 발송하며, 제목 접두어 `[TEST - 05_JOB]`을 강제 적용합니다.

---

## 🚀 실행 명령

```bash
# 1. 일별 정기 파이프라인 (스캔 + 리포트 생성 + 이메일 발송)
python 05_JOB_SL01_R_Bins/run_sl01_r_bins.py

# 2. 테스트 모드 (이메일을 수신 리스트 대신 본인 계정으로만 전송)
python 05_JOB_SL01_R_Bins/run_sl01_r_bins.py --test

# 3. 과거 전체 WMS 파일 기반 이력 백필
python 05_JOB_SL01_R_Bins/sl01_r_bins_operator.py --backfill

# 4. 단위 테스트 실행 (D87 규칙 및 전체 회귀 테스트)
pytest 05_JOB_SL01_R_Bins/tests/ -v
```
