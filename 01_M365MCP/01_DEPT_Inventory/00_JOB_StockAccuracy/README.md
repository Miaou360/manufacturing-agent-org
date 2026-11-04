# 00_StockAccuracy (재고 정확도 검증 & 온톨로지 기반 정기 알림팀)

`00_StockAccuracy`는 ACME Manufacturing Plant P001(Headlamp)의 **재고 정확도를 주기적으로 검증하고, WMS Stock List / GR(입고) / GI(출고) 및 Missing GI(생산 소모 vs 라인 출고 차이) 데이터를 통합 분석하여 오스캔(Mis-scan), 장기 체류, 동일 빈(BIN) 내 출고 정체, 선입선출(FIFO) 역전 및 품목별 실시간 회전일수(Turnover Days) 온톨로지를 구축**하는 전사 자동화 시스템입니다.

---

## 👥 주요 담당자 및 수신처 매핑
- **수신 (To)**: `.env`의 `STOCK_ACCURACY_TO` 참조 (Receiving W/H Leader, Supervisor, Warehouse Specialist)
- **참조 (CC)**: `.env`의 `STOCK_ACCURACY_CC` 참조 (Materials Planner, Materials Team)

---

## 🏭 ACME Manufacturing 창고 및 Bin 분류 규칙 (Warehouse Domain Rules)

현장 오퍼레이션 특성을 반영하여 다음과 같은 규칙으로 빈(BIN), 저장위치(Storage Location) 및 자재를 진단합니다:

### 1. 저장위치(Storage Location) 정의
- **`SL01`**: Plant 1 메인 창고
- **`SL02`**: Plant 3 메인 창고 (하이랙)
- **`SL03`**: 원자재 보관 창고
- **`SL04`**: 패스쓰루(Pass-through) 구역
- **`SP31`**: **자재 생산라인 공급 창고** (Turnover 기준 출고 대상)
- **`SP11`**: **원자재 생산라인 공급 창고** (Turnover 기준 출고 대상)
- **`SL49`**: 품질 DMA 창고

### 2. 이동 유형 (Movement Type)
- **GR (입고)**:
  - `101`: 정상 입고
  - `102`: 입고 취소 (원 거래 상쇄 무효화)
- **GI (출고 및 이동)**:
  - `311`: 저장위치 간 이전 이동 (`GI Loc` = From ➔ `GR Loc` = To)
  - `312`: 이전 이동 취소

### 3. 빈(BIN) 위치 규칙
- **`R`로 시작하는 빈 (`R...`)**: **Plant 3** 물건 (하이랙 메인 보관 창고, 장기 보관 가능)
- **`R`로 시작하지 않는 빈**: **Plant 1** 물건 (생산 조립 공장 라인 구역)
- **`D` 및 `CKD`로 시작하는 빈 (`D...`, `CKD...`)**: **우리 창고 아님 / CKD 전용 구역** ➔ **분석 대상에서 전면 제외**
- **`EXT_TRL`로 시작하는 빈 (`EXT_TRL...`, 예: `EXT_TRL TRL 1`, `EXT_TRL TRL 2`)**: **임시 스테이징 / 트레일러(Trailer) 빈** ➔ Cross-dock 등 외부 공급망에서 컨테이너/트레일러 입고 및 환적 시 임시 생성된 가상·이동형 적치 구역으로, 창고 내 고정 랙(Rack)이 아니므로 **순환 실사(Daily/Weekly Cycle Count) 대상에서 100% 자동 제외**
- **`SMT...`, `SMD...`**: 바닥에 깔려있는 **슈퍼마켓 버퍼** 구역 (1~3일 이내 초고속 회전 필수)
- **`OEM...`**: **Plant 1 OEM 전용 바닥(Floor) 버퍼** 빈 (SMT와 동일하게 바닥 빈으로 취급하며 끝자리는 층수가 아님. Daily 대상, Weekly 제외)
- **`CRT...`, `PRO...`**: **리팩 대차(Repack Cart/Dolly)** 공간 (초고속 회전 필수)

### 4. 작업자 수기 피킹(Hand-picking) 및 오스캔 고위험 구역
- **층수 판별**: 일반 랙 빈의 **가장 끝자리 숫자(1, 2, 3, 4, 5)가 랙의 층수**를 의미.
- **위험 구역**: **Plant 1이면서 끝자리가 `1` 또는 `2` (1층 및 2층)**인 빈은 작업자가 지게차 없이 손으로 직접 자재를 가져갈 수 있는 높이.
- **소물류 타깃팅**: 품명에 `SCREW`, `AIMING`/`AMING`, `U-NUT`/`NUT`, `CLIP`, `WASHER`, `PIN` 등이 포함된 품목은 박스 내 낱개 개별 포장이 되어 있어, 작업자가 박스를 통째로 스캔하지 않고 **낱개만 손으로 가져가 허수 재고(Mis-scan)가 남을 확률이 매우 높음**.

### 5. 미입고 출고(Orphan GI) 예외 처리 규칙
- WMS 출고(GI)는 존재하나 입고(GR) 이력이 없는 바코드 발생 시:
  - **원인**: 1년 분석 범위 밖 장기 재고 또는 현장 라벨 손상으로 인한 재발행 라벨.
  - **처리 방식**: 개별 바코드 수명주기 통계(체류일수)에서는 왜곡 방지를 위해 제외하되, 온톨로지 DB에 `orphan_boxes` 및 `orphan_rate` 지표로 누적 관리하여 재고 관리 신뢰도 진단에 활용.

### 6. 순환 실사(Cycle Count) 레벨링 및 현장 오퍼레이션 원칙
창고 운영 현장의 물리적 동선과 장비 활용 특성을 극대화하여 **Daily(일일)**와 **Weekly(주간)**의 역할을 분리하여 운영합니다:
- **동일 빈 집계 (Bin-Level Deduplication)**:
  - 작업자는 해당 빈(BIN)에 가서 확인하므로, **1행 = 1개 실제 물리적 랙 빈(BIN)** 기준으로 통합 표시합니다.
  - 해당 빈에 걸린 품목이 여러 개일 경우 **`Part #` 열에 콤마(`,`)로 나열**합니다.
  - 해당 빈의 대상 바코드 수량은 **`Label QTY`**로 표시합니다.
- **일정 및 발송 규칙 (Schedule Matrix)**:
  - **월~금 아침 06:50 EDT**: `[Daily]` Cycle Count Audit (**총 40개 규칙 빈: Top 15 Rapid Floor + Extended 25 Secondary, 및 무작위 검증 3개 빈 = 엑셀 총 43개 빈**) ➔ **아침 06:50 자동 실행 후 07:20까지 메일 발송 완료**.
    - **Top 15 Rapid Floor Audit**: 주간조가 07:00 출근 즉시 15분간 도보로 최우선 실사하는 1·2층/바닥 버퍼 핵심 빈 (이메일 본문 상단 하이라이트 표에 노출).
    - **Extended 25 Secondary Targets**: 당일 근무 중 교대/여유 시간에 추가 점검하는 2차 확장 실사 빈 (첨부 엑셀 Sheet 1에 연속 수록).
    - **Random 3 Validation Sample**: 머신러닝 학습 데이터의 선택 편향(Selection Bias) 보정 및 모델 비선정 구역 오차율 추정을 위한 층화 무작위 검증 빈.
  - **토요일 아침 06:50 EDT**: `[Weekly]` Deep Cycle Count (**50개 규칙 빈 + 5개 무작위 검증 빈 = 총 55개 빈**) ➔ Plant 3 하이랙(`R...`), Fast Mover 잔여 LOT, Turnover Outlier 점검. (자세한 내용은 [WEEKLY_AUDIT_GUIDE.md](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/01_DEPT_Inventory/00_JOB_StockAccuracy/WEEKLY_AUDIT_GUIDE.md) 참조)
  - **일요일 아침 06:50 EDT**: **데이터 수집 및 DB 적재는 365일 무중단 실행**, 단 작업자 휴무 및 피로도 방지를 위해 **이메일 발송만 자동 제외(Sunday Policy)**.
- **현장 실사 출력 및 회신용 5대 핵심 컬럼 구성 (세로 1페이지 초간단 인쇄 최적화)**:
  - **전산 목표 정보 (Cols 1-4, 네이비 헤더 `#1E293B`)**:
    - `Rank | Bin # | Part # | Label QTY`
    - (불필요한 부가 컬럼을 전면 배제하여 11pt 대형 폰트와 넉넉한 기입란 확보)
  - **현장 실측 입력란 (Col 5, 에메랄드 그린 헤더 `#065F46`)**:
    - `Counted QTY` (현장 실측 박스 수 또는 OK 기입)
  - **인쇄 설정 (Print Setup)**:
    - 세로방향 (`ORIENTATION_PORTRAIT`), Letter 규격, 1페이지 완벽 맞춤 (`fitToWidth=1, fitToHeight=1`), 여백 0.25", 행 높이 16.5pt.
    - 전체 43개 빈이 단 **1장의 종이에 딱 떨어지게 인쇄**되어 클립보드에 끼워 현장 실사 및 볼펜 수기 기입에 최적화.
  - **현장 수기 및 전산 적재 SOP**:
    1. **일치할 때**: `Counted QTY`에 **`OK`** 또는 체크(**`V`**)만 적거나 전산수량을 적으면 끝.
    2. **불일치할 때**: `Counted QTY`에 **실측 수량(예: 15, 8)**만 적으면 끝 (차이 수량 및 라벨 판정은 시스템이 자동 계산).
    3. **자동 DB 적재**: 현장 스캔본 확인 ➔ 엑셀 입력 ➔ `python audit_result_collector.py ingest-excel <엑셀경로>` 실행 시 1초 만에 SQLite DB(`AUDIT_RESULT`) 적재 및 AI 오프라인 모델 재학습 피드 완료.
- **소물류(Screw, Nut, Clip 등) 실사 방식**:
  - **Box Presence Check Only (라벨 박스 유무 확인)**: 낱개 수량 계량 불필요.
- **오배치 재고(Mis-located Stock) 육안 확인 SOP**:
  - `Ghost Stock` 의심 빈에 실물 박스가 없을 경우, 전산 즉시 삭제를 금지하고 **좌우/상하 인접 랙 빈에 물리적으로 오배치되어 있는지 1차 육안 확인** 후 수불 조정 처리.

### 6-1. 순환 실사 대상 빈(BIN) 선정 가드레일 및 예외처리 규칙 (Cycle Count Filtering Rules)
작업자의 불필요한 현장 이동과 비물리적/임시 위치 카운트 혼선을 방지하기 위해 다음 4단계 가드레일이 자동 적용됩니다:

1. **임시 스테이징 및 트레일러 빈 100% 제외 (`EXT_TRL*`) [★ 2026-10-26 반영]**:
   - **대상 식별**: `EXT_TRL TRL 1`, `EXT_TRL TRL 2`, `EXT_TRL TRL 3` 등 `EXT_TRL`로 시작하는 모든 임시 빈.
   - **발생 배경**: Cross-dock 등 공급망 간 자재 운송 트레일러(Trailer) 및 하역장 환적 구역으로, 입고 전 일시적으로 수백~수천 박스가 적치되는 구역입니다.
   - **제외 사유**: 창고 내 영구 고정된 물리적 랙(Rack) 구조물이 아니며 트레일러 입출차에 따라 자재가 수시로 이동하므로 현장 15분 도보 실사나 주간 랙 실사 대상에 적합하지 않습니다. 특히 빈 명칭 끝자리 숫자(`TRL 1`, `TRL 2`)로 인해 1·2층 도보 실사 대상(Hand-picking Tier 1/2)으로 오분류되는 문제를 원천 차단합니다.
   - **처리 방식**: 일일(Daily Top 15 / Extended 25) 및 주간(Weekly Top 50) 실사 리스트, Excel 1번 시트(`Cycle_Count_Target_Bins`), 이메일 발송 본문 표에서 **100% 원천 배제**.
2. **비물리적 가상 빈 제외 (`BINSYNTH9953`)**:
   - WMS 상 아직 랙이나 구역이 배정되지 않은 미지정(`BINSYNTH9953`) 바코드는 현장 작업자가 도보로 실사할 수 없으므로 실사 타깃에서 자동 제외.
3. **타 창고 및 CKD 전용 구역 제외 (`D*`, `CKD*`)**:
   - 외부 창고(`D...`) 및 CKD 반조립 전용 구역(`CKD...`)은 원천 데이터 필터링 단계에서 전면 제외.
4. **동선 및 층수별 실사 분리**:
   - **Daily (15분 급속 실사)**: 1·2층 도보 접근 가능 랙(끝자리 `1`, `2`), SMT/OEM 바닥 버퍼, Repack 대차만 선별.
   - **Weekly (심층 실사)**: 3~5층 고단 랙(High-Rack) 및 Plant 3 메인 창고(`R...`) 등 리치트럭/지게차 필수 구역을 토요일 전사 실사로 집중 배치.

### 7. Missing GI (소모 vs 출고 차이) & 동일 빈 출고 활성도(Bin Activity) 통합 분석
- **문제 의식**:
  - "특정 바코드가 그 빈에서 나가지 않았더라도, 그 빈의 다른 바코드(박스)가 대신 나가면 정상 출고되고 있는가?"
  - WMS Stock List 상에 오래된 박스가 남아있을 때, 이것이 **실제 랙 후열에 파묻힌 실물(Physical Trap)**인지, 아니면 **생산라인에서 바코드 스캔 없이 가져다 써버려 전산에만 남은 유령 재고(Phantom Box)**인지 구분할 필요가 있음.
- **조회 기간 설정 원칙 (Completed Weekly & Rolling 7-Day Window)**:
  - **월요일 실행 시**: 직전 완결된 1주일(일요일~토요일, 예: `2026-10-18 ~ 2026-10-24`)을 단위 구간으로 확정 수집. 오늘 당일(월요일 새벽)의 미완결 진행 데이터를 배제하여 주간 데이터의 완성도를 100% 보장.
  - **화~금요일 실행 시**: 어제자 마감 기준 7일 롤링 윈도우(`어제 - 6일 ~ 어제`)로 생산라인 전면 버퍼 시차를 상쇄.
- **Missing GI 다차원 통계 학습 및 품목 분류 체계 (52주 과거 데이터 기반)**:
  출고되는 박스당량(SNP)과 실제 생산 소모량의 차이 및 현장 출고 스캔 습관을 다각도로 분석하여 4대 프로파일로 자동 분류합니다:
  1. **`UNIT_PACK_OFFSET_NORMAL` (상시 포장/출고 단위 편차 품목)**:
     - **조건**: 관측 주차 $\ge 3$주, 주간별 편차 표준편차 $\sigma \le 1.0$박스, 중앙값 $|\text{Median}| \ge 1.0$박스.
     - **원인**: 박스당 포장 단위(SNP)와 라인 BOM 소모 단위 간의 고정 시차 또는 라인사이드 상시 안전 재고 보유.
     - **조치**: Ghost Stock 오탐 제외 (학습된 중앙값 기준선 대비 순수 초과 결손 $\ge 2.0$박스일 때만 이상치 판정).
  2. **`CHRONIC_UNSCANNED_LINE_ISSUE` (만성 무스캔 라인 직투입 품목)**:
     - **조건**: 관측 주차 $\ge 4$주, 결손 발생률 $\ge 80\%$, 중앙값 $\ge 1.0$박스, 편차 변동성 안정 ($\sigma \le 1.5$).
     - **원인**: 생산라인 투입 시 WMS 바코드 스캔을 누락하고 직접 소모하는 공정 프로세스 누수 품목.
     - **조치**: 창고 랙 실사 타깃에서 분리하여 '생산라인 바코드 스캔 준수 및 불출 프로세스 점검' 관리 대상으로 별도 보고.
  3. **`SPORADIC_VOLATILE_DEFICIT` (급성 돌발 결손 / 진성 유령재고 의심군)**:
     - **조건**: 주간별 편차 변동폭이 큰 품목 ($\sigma > 1.5$).
     - **원인**: 평소에는 정상 동기화되다가 특정 주차에 라인 소모 스캔이 급작스럽게 누락되어 랙 빈에 허수 박스가 남은 상태.
     - **조치**: **Daily / Weekly 순환 실사 1순위 타깃 (`Ghost Stock Audit`, Risk Score +65~90점)**으로 즉시 현장 실사.
  4. **`CHRONIC_OVER_ISSUED` (상시 과출고 품목)**:
     - **조건**: 관측 주차 $\ge 4$주, 과출고(`OVER`) 비율 $\ge 80\%$.
     - **원인**: BOM 소모 속도 대비 라인 공급 선행 출고량이 많은 품목.
     - **조치**: 선입선출 미준수로 인한 랙 후열 방치 의심 (`FIFO_PHYSICAL_TRAP`, `Rear Pallet Check`, Risk Score +30점).
  5. **`NORMAL_SYNCHRONIZED` (정상 동기화 품목)**:
     - 생산 소모와 WMS 출고가 주간 단위로 오차 없이 정상 회전하는 품목.
### 8. 다차원 위험도 점수 부여 기준 및 가중치 매트릭스 (Risk Scoring Architecture)

본 시스템은 WMS 재고의 다차원 이상 징후를 결합하여 **합산 위험도 점수(Risk Score)**를 산출하고, 이를 기준으로 실사 타깃 랙 빈(BIN)을 자동 정렬합니다.

#### A. 체류일수 및 선입선출 기준 분리 원칙
1. **재고 연령 (`Stock_Age_Days = Today - GR Date`)**: (구 Warehouse Dwell Days)
   - 온톨로지의 표준 회전일수(Trimmed Mean Days)가 입고(GR)부터 생산출고(GI)까지의 기간이므로, **재고 연령(체류일수)도 반드시 실제 입고일(GR Date)과 오늘을 비교**합니다. (협력사 제조일자와 비교 시 발생하는 억울한 오탐 방지)
   - `Turnover Anomaly`, `High-Rack Aging`, `Fast Mover Residual`, `SMT/Repack Stagnant` 판정의 절대 기준으로 사용됩니다.
2. **협력사 제조일자 (`Lot Date`)**:
   - **오직 선입선출(FIFO) 분석에만 엄격히 한정 사용**합니다:
     - `FIFO_INVERSION`: 동일 품번 내 최신 로트 대비 30일 이상 지난 구형 로트 방치.
     - `SAME_BIN_FIFO_OBSTRUCTION`: 동일 랙 빈 내 최신 로트와 구형 로트 편차 20일 이상.
     - `FIFO_PHYSICAL_TRAP`: Missing GI 출고가 충분함에도 후열에 매몰된 오래된 로트.
3. **동일 빈 체류일수 (`Bin_Dwell_Days = consecutive_days_in_bin`)**:
   - 매일 스냅샷 엑셀을 대조하는 [`BARCODE_BIN_STAY`](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/01_DEPT_Inventory/00_JOB_StockAccuracy/db_manager.py) 테이블을 통해 특정 바코드가 위치 변경 없이 해당 빈에 머문 연속 일수를 자동 카운트합니다.

#### B. SMT 슈퍼마켓 & Repack 대차 동적 회전율(Turnover) 연계 판정
- 단순 2일 체류 기준을 지양하고, **품목 고유의 표준 회전일수(Turnover Days)에 비례하여 차등 판정**합니다:
  - $\text{Warehouse Dwell} \ge \max(7.0, \, 1.5 \times \text{Std Turnover})$ 또는 $\text{Bin Stay} \ge \max(3, \, 0.5 \times \text{Std Turnover})$ 초과 시:
    ➔ **진성 정체(`SMT_STAGNANT` / `REPACK_STAGNANT`)로 판정 (+45점/+40점)**
  - 표준 회전일수 이내에 머무는 정상 버퍼:
    ➔ **단순 관리 배점(+10점)만 부여**하여 상위 15개 실사 목록을 부당하게 독점하는 현상 방지.

#### C. Plant 1 1·2층 소물류(Screw, Aiming, Pad) 및 대용량 박스 당량 가산 로직
- **소물류 타깃 키워드**: `SCREW`, `AIMING`, `AMING`, `PAD`, `CUSHION`, `NUT`, `U-NUT`, `CLIP`, `WASHER`, `RIVET`, `BOLT`, `PIN`, `SPRING`, `FASTENER`, `SPACER`
- **도보 실사 1·2층 집중**: 조립 작업자가 손으로 집어갈 수 있는 1·2층 핸드피킹 랙에 위치한 소물류는 박스를 통째로 스캔하지 않고 **낱개/봉지만 꺼내가 전산에 유령 박스가 남을 위험이 가장 높습니다.**
- **박스 당량(Quantity per Box) 및 체류일수 결합 동적 배점**:
  1. **체류/회전일수 기본 배점**:
     - 신규 입고 버퍼: **+15점**
     - 정상 주기 내 체류: **+30점**
     - 회전주기 초과 방치: **+50점**
  2. **박스 당량(Quantity per Box) 대용량 가산점**:
     - $\text{Qty} \ge 5,000\text{ pcs}$: 초대용량 소물류 **+30점** (예: Tapping Screw 24,000개 포장 박스 등)
     - $\text{Qty} \ge 1,000\text{ pcs}$: 대용량 소물류 **+20점** (예: Bezel Gear Aiming 2,000개 등)
     - $\text{Qty} \ge 200\text{ pcs}$: 중용량 소물류 **+10점**
  3. **소물류 명시적 키워드 보너스**: 품명에 Screw, Aiming, Pad 등 소물 키워드 매칭 시 **+15점** 추가 가산.
  - ➔ 이를 통해 Plant 1 1·2층의 대용량 스크류/에이밍/패드 박스는 **75~95점 이상**의 고득점을 확보하여 Daily 15분 도보 실사의 최우선 순위로 직행합니다.

#### D. 생산라인 출고(GI) 이력 기반 선입선출(FIFO) 위반 확증 및 구역별 분리 로직
- **FIFO의 대원칙(LOT 기준)과 해상물류 역전(Transit Inversion) 교차 검증**:
  - **선입선출의 제1원칙은 무조건 "협력사 LOT 번호(제조일자)"**입니다. 부품의 노화, 화학적 산화 및 품질 보증기간 만료를 방지하기 위해 먼저 생산된 구형 LOT을 우선 소비하는 것이 제조업의 절대적 기본 원칙입니다.
  - **GR Date(창고 입고일) 교차 검증을 적용하는 이유**:
    - 해외 협력사의 출하 선적 순서 혼선이나 컨테이너 해상 운송/통관 지연 등으로 인해 **더 빠른 제조 LOT(구형 LOT)이 오히려 더 늦게 창고에 입고(GR)되는 '물류 역전 현상'**이 발생할 수 있습니다.
    - 이러한 경우, 최신 LOT이 생산라인에 불출되던 당시에는 **창고에 구형 LOT 실물이 입고조차 되지 않았으므로 현장 작업자가 선입선출을 물리적으로 지킬 수 없었던 불가항력 상황**입니다.
    - 따라서 **"창고 실제 입고일(GR Date)"을 생산 출고(GI) 이력과 교차 검증**하여 진성 위반과 물류 불가항력을 명확히 구분합니다:
      1. **진성 선입선출 위반 (현장/보충 책임 확증 - `Bypass Confirmed`)**:
         - 구형 LOT 박스가 **이미 창고에 입고(GR)되어 대기 중이었음에도**(`구형 박스 GR Date < 생산 출고된 최신 박스 GR Date`), 뒤늦게 들어온 신규 박스를 먼저 가져다 쓴 경우.
         - ➔ 현장에서 먼저 들어와 있던 실물을 방치하고 새 박스를 꺼내 쓴 명백한 과실이므로 **구역별 바이패스 가산점(+40점) 부여**.
      2. **물류 불가항력 오탐 방지 (소명 및 가이드 전환 - `Transit Inversion Exoneration`)**:
         - 구형 LOT이지만 신규 박스가 불출된 이후에 뒤늦게 입고된 경우.
         - ➔ 현장 작업자 과실이 아닌 공급망 물류 역전이므로 **현장 책임 바이패스에서 제외**하고, 창고 내에서 "이제 입고되었으니 최우선 소진"하도록 일반 재고 정렬 가이드로 유도.
- **물류 흐름에 따른 구역별 책임 분리 (Plant 1 Floor vs Plant 3 Replenishment)**:
  - 동일 품번(`Mat.No`)에 대해 **이미 생산라인으로 불출 완료된 최신 박스의 입고일(`latest_issued_gr_date`)**이 존재함에도 구형 박스가 창고에 14일 이상 방치된 경우:
    1. **`FIFO_BYPASS_PLANT1_FLOOR` (+40점, `FIFO Bypass Audit`)**:
       - 방치된 박스가 **Plant 1 조립 구역**에 위치한 경우:
       - ➔ Plant 1 랙 전면에 먼저 입고되어 있던 구형 박스를 눈앞에 두고 신규 박스를 먼저 조립 투입한 **Plant 1 현장 불출자 책임**.
    2. **`FIFO_BYPASS_PLANT3_REPLENISH` (+40점, `Replenish Bypass`)**:
       - 방치된 박스가 **Plant 3 하이랙(`R...`)**에 갇혀있는 경우:
       - ➔ Plant 3 리치트럭 기사가 하이랙의 구형 팔레트를 꺼내지 않고 신규 팔레트부터 Plant 1으로 보충(Replenish)해버린 **Plant 3 지게차 기사 책임**.

#### E. Daily 15분 도보 실사(Tier 1·2) vs Weekly 하이랙(Tier 3~5) 슬로우 스톡 분리 규칙
- **Daily 15분 도보 실사 한정 원칙**:
  - Daily 실사는 출근 직후 작업자가 지게차 없이 15분 만에 걸어서 실사하는 목적이므로, **대상 빈(`top_bins`)은 100% "Plant 1의 1·2층 핸드피킹 랙", "SMT 슈퍼마켓", "Repack 대차"로만 엄격히 제한**합니다.
  - 리치트럭이나 고소작업대가 필수적인 **3층 이상의 상단 하이랙(Tier 3, 4, 5: 예 `65Z 5`, `66Z 5`, `67Z 5` 등)은 Daily 실사 목록에서 원천 배제**됩니다.
- **슬로우 스톡(Slow Stock)의 우선순위 감점 및 Weekly 이관**:
  - 특정 차종/트림용 PCB 모듈, LDM 등 간헐적으로만 소량 출고되는 저회전 자재(`SLOW_MOVER`, `STAGNANT`, `PENDING_FIRST_GI` 또는 Turnover $\ge 25$일)는 대부분 5층 상단 랙에 장기 보관됩니다.
  - 슬로우 스톡은 일상 양산 조립 라인에 즉각적인 결품 충격을 주지 않으므로, **Daily 및 Weekly 실사 모두에서 점수 감점(-30점, `SLOW_STOCK_DEMOTE`)**을 적용하여 활발히 돌아가는 양산 핵심 품목(`FAST_MOVER`, `NORMAL`)에 우선순위를 양보하도록 설계합니다.
  - 이를 통해 주간 실사(Top 50)에서도 슬로우 스톡이 상위권을 독점하지 않고, 실질적으로 긴급한 양산 결손/바이패스 건이 최상단에 배치됩니다.
- **단종/부동 자재(Dead/Obsolete Stock) Daily 점수 상한**:
  - 최근 45일간 입고(GR)와 생산출고(GI)가 전무한 단종 품목은 **Daily 점수 상한 20점, Weekly 상한 35점으로 제한(`OBSOLETE_DEAD_STOCK`)**되어 실사 목록을 과도하게 점유하는 것을 원천 차단합니다.

#### F. Missing GI 결손 수량에 따른 가산 스케일링
- 단순 결손 유무에 그치지 않고, **결손 박스 수량에 비례하여 긴급도를 자동 차등화**합니다:
  $$\text{Ghost Stock 배점} = 65 + \min\left(25, \, \lfloor\text{anomaly\_diff\_box} \times 0.5\rfloor\right)$$
  - 2박스 초과 결손: **+66점**
  - 10박스 초과 결손: **+70점**
  - 50박스 이상 대형 결손: 최대 **+90점**까지 점수가 확장되어 실사 리스트 1순위로 즉각 직행.

#### G. 가중치 배점표 (Scoring Weights Matrix)

| 구분 | 위험 유형 (Risk Category) | 판정 조건 | Daily 가산점 | Weekly 가산점 | 기본 조치 유형 (Action) |
| :--- | :--- | :--- | :---: | :---: | :--- |
| **공통** | `CRITICAL_GHOST_SUSPECT` | Missing GI 결손 (수량 비례 스케일링 적용) | **+65 ~ +90** | **+65 ~ +90** | **`Ghost Stock Audit`** |
| **공통** | `FIFO_BYPASS_PLANT1_FLOOR` | **Plant 1 랙에 먼저 입고된 구형 재고 방치 (현장 불출 바이패스)** | **+40** | **+40** | **`FIFO Bypass Audit`** |
| **공통** | `FIFO_BYPASS_PLANT3_REPLENISH`| **Plant 3 하이랙 구형 재고 미인출 (보충 지연 바이패스)** | **+40** | **+40** | **`Replenish Bypass`** |
| **공통** | `SAME_BIN_FIFO_OBSTRUCTION` | 동일 빈 내 로트 편차 $\ge 20$일 | **+35** | **+35** | **`Bin FIFO Sort`** |
| **공통** | `FIFO_PHYSICAL_TRAP` | Missing GI 충분(OVER) + 후열 매몰 | **+30** | **+30** | **`Rear Pallet Check`** |
| **공통** | `UNIT_PACK_OFFSET_NORMAL` | 4주 학습된 포장단위 상시 편차 품목 | **+10** | **+10** | **`Packaging Unit Check`** |
| **공통** | `SLOW_STOCK_DEMOTE` | **저회전 슬로우스탁 (Turnover $\ge 25$일 또는 출고 극소)** | **-30** | **-30** | **`Priority Demoted`** |
| **Daily** | `PLANT1_HAND_PICK_SMALL_PART` | Plant 1 1·2층 소물류 (체류 + 박스당량 최대 +30 + 키워드 +15) | **+50 ~ +95** | **-** | **`Box Presence Check`** |
| **Daily** | `SMT_STAGNANT` | SMT 버퍼 + 품목 회전일수 1.5배 초과 | **+45** | **+35** | **`SMT Audit`** |
| **Daily** | `REPACK_STAGNANT` | Repack 대차 + 품목 회전일수 1.2배 초과 | **+40** | **+35** | **`Repack Cart Audit`** |
| **Daily** | `FIFO_INVERSION (Plant 1)` | 재고 기준 선입선출 역전 (GI 확증 미적용 시) | **+30** | **+30** | **`FIFO Review`** |
| **Daily** | `TURNOVER_ANOMALY (Extreme)` | 창고 체류일수 $\ge 10 \times \text{Turnover}$ | **+45** | **+45** | **`Turnover Audit`** |
| **Daily** | `TURNOVER_ANOMALY (High)` | 창고 체류일수 $\ge 5 \times \text{Turnover}$ | **+30** | **+30** | **`Turnover Audit`** |
| **Weekly**| `FAST_MOVER_RESIDUAL` | Fast Mover 품목 + 잔여 바코드 체류 | **-** | **+55** | **`Fast Mover Audit`** |
| **Weekly**| `HIGH_RACK_AGING` | Plant 3 하이랙(`R...`) 장기 체류 | **-** | **+50** | **`High-Rack Audit`** |
| **공통** | `OBSOLETE_DEAD_STOCK` | 최근 45일간 입출고 전무 단종/부동 자재 | **상한 20** | **상한 35** | **`Obsolete Stock Review`** |

#### G. 조치 유형 우선순위 결정 (Action Priority Hierarchy)
1개 빈에 여러 위험 요소가 중복 감지될 경우, 다음 우선순위에 따라 현장 지시(`Action Required`)가 단일 결정됩니다:
1. **`Ghost Stock Audit`** (생산라인 무스캔 소진 유령 재고 확인 - 최우선)
2. **`FIFO Bypass Audit`** (실제 생산 출고 이력 대조 선입선출 위반 점검)
3. **`Fast Mover Audit`** (초고속 회전 잔여 바코드)
4. **`High-Rack Audit`** (하이랙 랙 장기 체류)
5. **`Box Presence Check`** (Plant 1 1·2층 소물류 박스 유무)
6. **`Bin FIFO Sort`** (동일 빈 내 선입선출 재정리)
7. **`Rear Pallet Check`** (후열 매몰 파렛트 점검)
8. **`SMT Audit` / `Repack Cart Audit`** (바닥 버퍼 회전율 초과 정체)
9. **`Turnover Audit` / `FIFO Review`**

---

## 📂 팀 폴더 및 데이터 아키텍처

```
00_JOB_StockAccuracy/
├── README.md                      # [현재 문서] 팀 운영 가이드 및 공통 도메인 규칙
├── WEEKLY_AUDIT_GUIDE.md          # 📘 [주간 가이드] 토요일 주간 심층 실사 및 하이랙/Fast Mover 운영 지침
├── config.py                      # 수신자(To/Cc), 모니터링 플랜트, 스택킹 및 DB 경로 설정
├── daily_pipeline.py              # 🚀 [통합 파이프라인] 매일 아침 06:50 EDT 5단계 자동화 실행기 (Daily/Weekly 자동 분기)
├── run_daily_pipeline.bat         # ⚙️ Windows 작업 스케줄러 등록용 실행 배치 파일
├── wms_crawler.py                 # WMS 웹 실시간 크롤러 (Stock List 다운로드 - 행 감지 강화)
├── missing_gi_crawler.py          # 📥 WMS Missing GI 롤링 7일 소모/출고 차이 자동 크롤러
├── missing_gi_backfill_crawler.py # 🔄 WMS Missing GI 과거 주간 분할 백필 및 단위 편차 학습기
├── historical_backfill_crawler.py # WMS GR/GI 과거 1년치 주간 분할 백필 & 일일 증분 크롤러
├── db_manager.py                  # SQLite DB 적재 및 스키마 관리 엔진 (GR/GI + MissingGI Snapshot + Baseline)
├── turnover_ontology_engine.py    # 바코드 수명주기 매칭, Trimmed Mean 계산 & 온톨로지 생성기 (초고속 벡터화)
├── box_aging_tracker.py           # 온톨로지 + MissingGI + Bin Activity 연동 다차원 오스캔/Ghost Stock 탐지 엔진
├── stock_verifier.py              # 실사 리스트 가공 및 Daily/Weekly HTML 리포트 렌더링
├── warehouse_notifier.py          # 창고팀 대상 정기 알림 메일 발송기 (Daily/Weekly 모드 지원)
│
├── audit_reports/                  # 📊 일일/주간 생성된 순환실사 마스터 엑셀 보관소 (WMS_Cycle_Count_Audit_List_*.xlsx)
├── logs/                           # 📝 일일 자동화 파이프라인 실행 로그 (daily_pipeline_YYYYMMDD.log)
├── wms_daily_stack/                # 📦 매일 다운로드받는 WMS Stock List 누적 저장소
├── missing_gi_daily_stack/         # 📦 WMS Missing GI 주간 단위 엑셀 스택 저장소
├── gr_daily_stack/                 # 📦 WMS GR(입고) 이력 주간/일일 엑셀 저장소
├── gi_daily_stack/                 # 📦 WMS GI(출고) 이력 주간/일일 엑셀 저장소
├── db/
│   ├── inventory_history.db        # 🗄️ SQLite 통합 이력 DB (1.22GB 규모, GR/GI + MISSING_GI_SNAPSHOT)
│   └── inventory_knowledge.db      # 🗄️ 보조 DB
└── ontology/
    └── material_turnover_ontology.json # 🧠 1,704개 품번별 1년 회전일수/도메인 지식 마스터 (온톨로지 DB, 1.05MB)
```

---

## 🛠️ 주요 스크립트 및 실행 가이드

### 1. `daily_pipeline.py` (전체 통합 파이프라인)
- 매일 아침 06:50 정기 배치로 구동되며 5단계를 완전 자동 실행합니다:
  - **STEP 1**: WMS Stock List 당일 재고 스냅샷 다운로드
  - **STEP 2**: WMS 어제자 입출고(GR/GI) + **Missing GI (최근 7일 롤링 윈도우)** 다운로드 및 DB 적재
  - **STEP 3**: 온톨로지 회전일수 마스터 재계산
  - **STEP 4**: 재고 분석, Missing GI 교차 검증 및 실사 타깃 엑셀 생성
  - **STEP 5**: Outlook 이메일 정기 발송 (일요일 제외)
```bash
# 기본 자동 실행 (평일: Daily 40빈, 토요일: Weekly 50빈)
python daily_pipeline.py --headless

# 이메일 발송 없이 검증만 실행
python daily_pipeline.py --skip-email
```

### 2. `missing_gi_crawler.py` (Missing GI 전용 크롤러)
- WMS MissingGI 페이지에서 최근 7일(또는 지정 일수)의 소모 대비 출고 차이를 크롤링하여 `missing_gi_daily_stack/`에 저장.
```bash
python missing_gi_crawler.py --days 7 --plant P001
```

### 3. `wms_crawler.py` (WMS Stock List 크롤러)
- 최신 전체 재고(~54,000건) 엑셀을 다운로드하여 `wms_daily_stack/`에 누적 저장.
```bash
python wms_crawler.py --headless
```

### 4. `historical_backfill_crawler.py` (GR/GI 이력 크롤러)
- 1주일 분할 청크로 과거 1년치 백필 또는 일일 어제자 증분 수집.
```bash
python historical_backfill_crawler.py --daily
```

---

## 🗄️ SQLite 데이터베이스 구조 (`db/inventory_history.db`)

1. **`GR_TRANSACTIONS`**: 입고 이력 (`barcode` PK, `mat_no`, `bin`, `gr_time`, `gr_qty`, `move_type` 등)
2. **`GI_TRANSACTIONS`**: 출고 및 이동 이력 (`barcode`, `mat_no`, `gi_loc`, `gr_loc`, `gi_time`, `qty`, `move_type` 등)
3. **`BARCODE_LIFECYCLE`**: 바코드별 입출고 종합 수명주기 (`dwell_days`, `status`, `is_orphan` 등)
4. **`MATERIAL_ONTOLOGY`**: 품번별 온톨로지 지식 마스터 (`trimmed_mean_days`, `mover_class`, `orphan_rate` 등)
5. **`MISSING_GI_SNAPSHOT`**: Missing GI 스냅샷 이력 (`mat_no`, `prod_bom`, `gi_qty`, `diff_box`, `diff_flag`, `target_bins` 등)
6. **`MISSING_GI_BASELINE`**: 4주 이상 학습된 출고/포장 단위 상시 편차 마스터 (`is_unit_offset_item`, `median_diff_box`, `std_diff_box` 등)
7. **`BARCODE_BIN_STAY`**: WMS 일일 스냅샷 기반 동일 빈 연속 체류일수 역추적 마스터 (`consecutive_days_in_bin`, `warehouse_dwell_days` 등)

---

## 📊 첨부 엑셀 시트 구성

| 시트명 | 대상 | 주요 목적 |
| :--- | :--- | :--- |
| **`Cycle_Count_Target_Bins`** | 공통 (Daily 43개, Weekly 55개) | 현장 작업자가 직접 찾아가 실사할 랙 빈 통합 목록 (Top + Extended + Random 표본) |
| **`Shadow_EV_Ranking`** | 관리자 분석용 (Top 15) | **기대가치 점수($\text{Priority EV} = \frac{\text{Expected Loss}}{\text{Est Minutes}}$) 기준 순위 vs 규칙 순위 비교 분석 시트** |
| **`MissingGI_Ghost_Suspects`** | 공통 (최대 300건) | **생산라인 무스캔 소진 유령 재고(Ghost Stock) 의심 박스 전수 목록** |
| **`SameBin_FIFO_Obstruction`** | 공통 (최대 200건) | **동일 빈 내 신규 입고 대비 방치 박스 목록 (선입선출 정리)** |
| **`HighRack_Aging_Master`** | Weekly 전용 | Plant 3 하이랙 및 3~5층 고단 랙 장기 체류 재고 |
| **`FastMover_Residual_Bins`** | Weekly 전용 | 초고속 회전 자재의 미스캔 잔여 라벨 |
| **`Plant1_Small_Parts_BoxCheck`** | Daily 전용 | Plant 1 1·2층 소물류 수기 피킹 유무 확인 |
| **`SMT_Floor_Stagnation`** | Daily 전용 | 바닥 슈퍼마켓 버퍼 및 대차 체류 재고 (회전일수 초과 품목) |
| **`FIFO_Inversion_Master`** | Daily 전용 | 품번 단위 선입선출 역전 품목 |
| **`All_Suspects_Top500`** | 공통 (Top 500) | 전체 이상 징후 위험도 순위 마스터 |

---

## 🤖 9. 오프라인 모델 평가 및 학습 프레임워크 (`model/`)

실제 현장 실사 실적(`AUDIT_RESULT` Ground Truth)이 누적되면 즉시 가동할 수 있도록 설계된 **오프라인 머신러닝 학습 및 Walk-Forward 평가 프레임워크**입니다. 현재는 실물 라벨 수집 전이므로 SAP vs WMS 시스템 불일치 데이터(ZTCODE_STOCK_REC)를 **약한 대리 라벨(Weak Proxy Label)**로 플러그인하여 파이프라인의 엔드투엔드 무결성을 검증합니다.

> [!WARNING]
> **대리 라벨(Weak Proxy Label) 해석상 한계 주의**
> - ZTCODE_STOCK_REC는 공장 전체 품목(Material) 합산 기준이므로 특정 물리적 빈(Bin)의 실제 수량 차이를 보증하지 않습니다.
> - 전표 처리 시차 및 행정 노이즈가 포함되므로, 본 지표는 **ML 파이프라인 골격 검증용**이며 현행 도메인 규칙을 대체하지 않습니다.

### 주요 모듈 구성
1. **[`build_dataset.py`](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/01_DEPT_Inventory/00_JOB_StockAccuracy/model/build_dataset.py)**:
   - `AUDIT_FEATURE_SNAPSHOT` 시점 피처와 라벨 소스를 Point-in-time으로 결합.
   - **라벨 플러그인 아키텍처**:
     - `Zm212ProxyLabel`: `run_date` 기준 +1~7일 내 ZTCODE_STOCK_REC 불일치 발생 여부를 빈 단위로 전파.
     - `AuditResultLabel`: 향후 현장 실사 Ground Truth 테이블 연계용 표준 인터페이스 (`NotImplementedError`).
   - **누수 방지 검증 (`verify_no_data_leakage`)**: `created_at <= run_date 23:59:59`를 강제하여 미래 정보 유출 원천 차단.
2. **[`train_lgbm.py`](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/01_DEPT_Inventory/00_JOB_StockAccuracy/model/train_lgbm.py)**:
   - LightGBM 이진 분류기 골격.
   - 극단적 클래스 불균형(`scale_pos_weight`), 범주형 피처(`primary_mover_class`, `plant`, `tier`), 다중 핫 인코딩(`cat_CRITICAL_GHOST` 등) 자동 처리.
   - **가드레일**: 양성 샘플 수가 200건 미만인 경우 과적합 방지를 위해 학습을 자동 건너뛰고 경고 로그 기록.
3. **[`evaluate.py`](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/01_DEPT_Inventory/00_JOB_StockAccuracy/model/evaluate.py)**:
   - 시간순(Walk-Forward) 분할을 통해 `Baseline_Rule_Score` vs `Baseline_Shadow_EV` vs `Candidate_LightGBM` 비교.
   - **핵심 지표**:
     - `Precision@15`: Daily 15분 도보 실사 적중률
     - `Precision@40`: 일일 전체 실사 적중률
     - `Recall@15`, `Recall@40`: 전체 불일치 중 포착 비율
     - `Discovered Loss / Minute`: 작업분당 기대 발견 금액 ($/min)
     - `Random Sample IPW Error Rate`: 역확률 가중(IPW) 기반 공장 전체 기저 오차율
     - `Unselected Floor Error Rate`: 미선택 바닥 재고의 그림자 누락(False Negative) 오차율
4. **리포트 출력 (`model/reports/`)**:
   - `evaluation_summary_%Y%m%d_%H%M%S.md`: 상단 대리 라벨 한계 경고 배너 및 모델별 지표 비교표.
   - `evaluation_metrics_%Y%m%d_%H%M%S.csv`: 지표 원본 데이터셋.

### 실행 방법 (Offline CLI)
```bash
# 1. 특정 일자 데이터셋 생성 (Ground Truth 실사 결과 기준)
python model/build_dataset.py --run-date 2026-10-26 --db-path db/dev_stock_accuracy.db --label-source audit_result

# 2. Walk-Forward 오프라인 모델 평가 실행 (Ground Truth 실사 결과 기준)
python model/evaluate.py --db-path db/dev_stock_accuracy.db --run-dates 2026-10-22 2026-10-26 --label-source audit_result

# 3. ZTCODE_STOCK_REC 약한 대리 라벨 기준 평가 (필요시)
python model/evaluate.py --db-path db/dev_stock_accuracy.db --run-dates 2026-10-22 2026-10-26 --label-source proxy
```

---

## 📋 10. 현장 실사 실적 원장(`AUDIT_RESULT`) 및 결과 수집 파이프라인

현장 실사자가 바닥 재고를 직접 검증한 실적을 원장에 영구 보존하고, 실시간 입출고 시차 보정 및 관리자 재실사(Recount)를 지원하여 오프라인 머신러닝 학습의 **확정 정답 라벨(Ground Truth)**로 활용하는 종합 수집 체계입니다.

### 1. `AUDIT_RESULT` 원장 스키마
`AUDIT_ASSIGNMENT`의 `assignment_id`를 외래키(FK)로 조인하여 **1:N 관계**를 지원합니다 (재실사 허용).

| 컬럼명 | 데이터 타입 | 설명 및 제약조건 |
| :--- | :--- | :--- |
| `result_id` | `INTEGER PK` | 고유 실적 일련번호 (Auto Increment) |
| `assignment_id` | `TEXT NOT NULL` | 지시 원장 고유 식별자 (`AUDIT_ASSIGNMENT.assignment_id` FK) |
| `run_id` | `TEXT NOT NULL` | 실사 회차 ID (예: `20261026_DAILY`) |
| `bin` | `TEXT NOT NULL` | 실사 대상 물리적 랙 빈 (예: `SMT 9947`) |
| `mat_no` | `TEXT` | 주 품번 |
| `counted_at` | `TEXT NOT NULL` | 현장 실사 완료 시각 (`YYYY-MM-DD HH:MM:SS`) |
| `book_qty_at_count` | `REAL NOT NULL` | 지시서 발행 시점의 전산 장부 수량 |
| `counted_qty` | `REAL NOT NULL` | 작업자가 현장에서 실측한 실제 물리적 수량 |
| `diff_qty` | `REAL NOT NULL` | 최종 오차 수량 (입출고 시차 보정 반영 후) |
| `diff_amount` | `REAL NOT NULL` | 오차 금액 (`diff_qty * unit_price`, $) |
| `cause_code` | `TEXT NOT NULL` | 표준 오차 원인 코드 (`CC01` ~ `CC99`) |
| `minutes_spent` | `REAL NOT NULL` | 해당 빈 실사에 소요된 실제 작업 시간(분) |
| `counter_id` | `TEXT NOT NULL` | 실사자/검수자 식별자 (예: `USER_SYNTH_9930`, `SUPERVISOR_01`) |
| `is_recount` | `INTEGER DEFAULT 0` | 1차 실사(0) vs 관리자 재실사(1) 구분 (1:N 지원) |
| `is_confirmed` | `INTEGER DEFAULT 1` | 최종 확정 여부 (1=확정, 0=재실사 대기/미확정) |
| `audit_origin` | `TEXT DEFAULT 'PHYSICAL_CYCLE_COUNT'` | 실물 실사(`PHYSICAL_CYCLE_COUNT`) vs WMS 행정 수정(`WMS_ADMIN_ADJUSTMENT`) 엄격 구분 |
| `wms_adjustment_status` | `TEXT DEFAULT 'NOT_ADJUSTED'` | WMS 사후 재고 전표 조정 상태 (`NOT_ADJUSTED`, `ADJUSTED`) |
| `in_flight_gr_qty` | `REAL DEFAULT 0.0` | 지시~실사 시차 동안 발생한 실시간 입고량 ($\Delta GR$) |
| `in_flight_gi_qty` | `REAL DEFAULT 0.0` | 지시~실사 시차 동안 발생한 실시간 출고량 ($\Delta GI$) |
| `in_flight_adjusted` | `INTEGER DEFAULT 0` | 실시간 입출고 보정 발생 여부 (1/0) |
| `effective_book_qty` | `REAL` | 시차 보정된 유효 장부 수량 ($\text{Book} + \Delta GR - \Delta GI$) |
| `notes` | `TEXT` | 현장 특이사항 및 관리자 코멘트 |

---

### 2. 표준 실사 오차 원인 코드 마스터 (`cause_codes.py`)
현장 종이 회신 시트, 태블릿/CLI 입력 및 WMS 조정 간 일관된 원인 분석을 위해 표준 코드표를 정의합니다:

| 코드 | 국문 명칭 | 영문 명칭 | 불일치 여부 | 조치 가이드 |
| :--- | :--- | :--- | :---: | :--- |
| `CC01_GHOST_MISSING_GI` | 생산 무스캔 소모 유령 재고 | Production Unscanned Consumption (Ghost Stock) | **불일치** | 생산라인 바코드 스캔 누락 확인 및 WMS 즉시 불출 |
| `CC02_WRONG_BIN_PUTAWAY` | 오입고 및 타 빈 물리적 오적재 | Wrong Bin Misplacement / Cross-Storage | **불일치** | 인접 랙 탐색 후 정상 로케이션 이동 전기 |
| `CC03_FIFO_BURIAL` | 후열 매몰 및 선입선출 미준수 | FIFO Inversion & Physical Burial Trap | **불일치** | 구형 로트 전열 재배치 및 차기 생산라인 우선 불출 |
| `CC04_PARTIAL_PICK_UNSCANNED`| 1·2층 소물류 낱개 수기 피킹 미스캔 | Partial Pack Manual Pick Unscanned | **불일치** | 1·2층 소물 랙 단품 실측 및 부분 수량 WMS 차감 조정 |
| `CC05_DAMAGED_SCRAP_UNREPORTED`| 파손 및 불량 폐기 미보고 전산 잔존 | Unreported Scrap / Damage Physical Residual | **불일치** | 품질팀 검사 후 폐기 전표(SAP MB1A 551) 정식 발행 |
| `CC06_UNIT_PACK_OFFSET` | 포장 단위(Pack Size) 차이 편차 | Unit Pack Size Offset Discrepancy | **불일치** | 박스당 수량 온톨로지 보정 및 WMS 단위 패킹 갱신 |
| `CC07_IN_FLIGHT_MOVEMENT` | 실사 중 입출고/생산 투입 이동 시차 | In-Flight Movement Timing Offset | **정상 (오차 상쇄)** | 전표 자동 역산 보정으로 현장 오차 미계상 처리 |
| `CC08_MATCH_VERIFIED` | 실물 일치 (오차 없음, 정상 재고) | Physical Match Confirmed (Zero Error) | **정상 (일치)** | 실사 확인 완료 (수량 변동 없음) |
| `CC09_DATA_ENTRY_ERROR` | 바코드/라벨 오부착 또는 전산 오류 | Barcode / Labeling Data Entry Error | **불일치** | 라벨 재발행 및 WMS 마스터 정보 정정 |
| `CC99_OTHER_UNIDENTIFIED` | 기타 원인 불명 불일치 | Other Unidentified Discrepancy | **불일치** | 재실사(Recount) 요청 및 관리자 정밀 현장 조사 |

---

### 3. 실사 도중 발생한 입출고 시차 보정 규칙 (In-Flight Movement Reconciliation)
아침 06:50 지시서 발행 시점($t_{\text{snapshot}}$)과 실제 작업자가 카운팅한 시점($t_{\text{counted}}$) 사이에 창고 입고($\Delta GR$) 또는 생산 불출($\Delta GI$)이 발생하여 생기는 가짜 불일치(False Discrepancy)를 다음과 같이 자동 보정합니다:

$$\text{EffectiveBookQty} = \text{BookQty}_{\text{snapshot}} + \Delta GR - \Delta GI$$
$$\text{TrueDiffQty} = \text{CountedQty} - \text{EffectiveBookQty}$$

- **완전 상쇄 판정 (`in_flight_offset_matched`)**:
  - 만약 $\text{CountedQty} == \text{EffectiveBookQty}$ (즉 $\text{TrueDiffQty} == 0$)이면, 외견상 차이는 100% 입출고 시차로 인한 것이므로 원인 코드를 **`CC07_IN_FLIGHT_MOVEMENT`**로 자동 지정하고 오차 수량을 0.0으로 확정하여 현장 실사자에게 불필요한 페널티를 부과하지 않습니다.
- **순수 물리적 오차 추출**:
  - 만약 시차 보정 후에도 차이가 남는다면($\text{TrueDiffQty} \neq 0$), 시차 변동분을 제외한 순수 물리적 차이만을 `diff_qty`로 기록합니다.

---

### 4. 물리적 실사(Physical Audit)와 WMS 단순 행정 수정의 엄격한 분리
- **원칙**: WMS 재고 수정 이력(`WMS_ADMIN_ADJUSTMENT`)만으로 실사 결과를 대체하지 않습니다.
- **이유**: 단순 전표 취소, 시스템 전산 대체, 마이너스 재고 일괄 보정 등은 현장 물리적 사실(Ground Truth)을 대변하지 못하며 위양성/위음성을 양산합니다.
- **적용**: 머신러닝 학습 및 모델 평가(`AuditResultLabel`) 시 **`audit_origin == 'PHYSICAL_CYCLE_COUNT'` 및 `is_confirmed == 1`인 레코드만을 정답 라벨로 엄격 필터링**합니다.

---

### 5. 결과 수집 경로 및 CLI 도구 (`audit_result_collector.py`)

#### A. 현장 클립보드 인쇄 및 결과 수기/전산 입력 최적화 엑셀 (`Cycle_Count_Target_Bins`)
현장 작업자가 종이로 출력하여 바인더에 끼우고 실사 후 스캔 회신하거나, 전산에서 즉시 입력할 수 있도록 엑셀 서식을 고도화하였습니다:
- **인쇄(Print Setup) 최적화**:
  - 가로(Landscape) 방향, US Letter, 1페이지 너비 자동 맞춤 (`fitToWidth=1, fitToHeight=0`).
  - 매 페이지마다 제목 행 자동 반복 인쇄 (`$1:$1`).
  - 수기 볼펜 작성을 위한 넉넉한 행 높이(Data Row Height 26pt, Header 28pt) 및 외곽선 그리드라인 명시.
- **2단계 시각적 분리 (컬러 헤더)**:
  - **전산 목표 정보 [SYSTEM TARGET] (Col A~H, 네이비 `#1E293B`)**: `Rank`, `Bin #`, `Part #`, `Label QTY`, `Action Required`, `Reason`, `Stock Age (Days)`, `Bin Stay (Days)`
  - **현장 실사 입력란 [FIELD AUDIT INPUT] (Col I~O, 에메랄드 `#065F46`)**:
    - `Physical Match? (OK/NG)`: 실물 일치 여부 (OK / NG 수기 체크 및 입력)
    - `Counted QTY`: 현장 실측 물리적 수량
    - `Diff QTY`: Excel 자동 계산 수식 (`=IF(ISBLANK(J2),"",J2-D2)`)
    - `Discrepancy Cause`: 불일치 원인 (`CC01` ~ `CC99` 또는 Ghost/Misplaced 등)
    - `Auditor`: 실사자 성명/이니셜
    - `Count Time`: 실사 일시
    - `Field Notes`: 현장 특이사항 및 수기 메모

#### B. 종이 실사 후 엑셀 입력 회신 시트 자동 인제스트 (`ingest_audit_results_from_excel`)
- `Physical Match? (OK/NG)` 컬럼에 **`OK`**가 입력된 경우, 별도로 수량을 다시 적지 않아도 전산 수량(`counted_qty = book_qty`) 및 일치 코드(`CC08_MATCH_VERIFIED`)로 자동 매핑됩니다.
- **`NG`**가 입력되거나 실측 수량 편차가 발생한 경우 오차 수량과 원인 코드를 정밀 적재합니다.
```bash
python audit_result_collector.py --ingest-excel audit_reports/WMS_Cycle_Count_Audit_List_20261026.xlsx --run-id 20261026_DAILY --db-path db/inventory_history.db
```

#### C. 태블릿 / CLI 단건 즉시 입력 (`record_single_audit_result`)
- 현장 작업자/관리자가 태블릿에서 수량을 입력하는 즉시 입출고 시차 보정과 단가 조회가 실시간 수행되어 `AUDIT_RESULT`에 적재됩니다.

#### D. 개발 및 시뮬레이션용 현실적 데이터 시딩
```bash
python audit_result_collector.py --seed-run-id 20261026_DAILY --db-path db/dev_stock_accuracy.db
```

---

## 📈 11. 지속적 학습 루프 및 타율·이력 관리 체계 (D74 Continuous Learning Loop)

현장 실사 결과(Ground Truth)를 바탕으로 데이터 규모에 맞춘 **단계형 지도학습(Phased Supervised Learning)**과 규칙별 적중률, 품번/빈 이력 추적을 상시 수행합니다.

### 1. 단계형 머신러닝 학습 로드맵
| 단계 | 데이터 기준 | 모델 구조 | 동작 방식 및 승격 기준 |
| :--- | :--- | :--- | :--- |
| **0단계: 데이터 위생** | 10/27 즉시 | 원인 빈칸=NULL 순수 팩트 보존 | 임의 원인 추정 금지, 시딩 함수 운영 DB 차단 |
| **1단계: 적중률·이력** | 매 실사 입력 후 | 규칙별 적중률 + 품번/빈 Beta(1,3) 평활화 | 소수 표본(1~2회) 극단치(0%/100%) 방지, 주간 타율 메일 섹션 4·5·6 탑재 |
| **2단계: 정규화 회귀** | 불일치 라벨 $\ge$ 50건 | L2 Regularized Logistic Regression | 섀도 모드 (Excel 비교 시트), 규칙별 실제 가중치 추정, 주 1회 오프라인 재학습 |
| **3단계: LightGBM** | 불일치 라벨 $\ge$ 200건 | LightGBM Gradient Boosting | 섀도 모드, 무작위 표본 대비 2주 연속 Lift 초과 시 **사용자 승인 후** 승격 |

### 2. 신규 원장 스키마
1. **`AUDIT_HIT_RATE_DAILY`**: 회차별 모델 타율(TOP 15, EXTENDED 25, Model Overall, Rank 1~5, 6~15, 16~40), 7일/28일 이동 누적 타율, 28일 무작위 기준선 및 Lift.
2. **`AUDIT_RULE_HIT_RATE`**: 12대 표준 규칙(`Ghost Stock`, `SMT Stagnant`, `FIFO Bypass` 등)별 표본 수($n$), 불일치 수($d$), 실제 타율, Beta 평활 점수.
3. **`AUDIT_HISTORICAL_DISCREPANCY_SCORE`**: 빈(`BIN`) 및 품번(`MATERIAL`)별 누적 실사 횟수, 불일치 횟수, Beta(1,3) 평활 위험도 $\frac{d + 1}{n + 4}$, 최근 실사일 및 이력.



