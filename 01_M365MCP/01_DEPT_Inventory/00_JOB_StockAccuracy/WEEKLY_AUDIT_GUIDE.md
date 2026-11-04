# WEEKLY_AUDIT_GUIDE (주간 심층 순환실사 운영 가이드)

본 문서는 ACME Manufacturing Plant P001(Headlamp) 창고의 **토요일 주간 심층 순환실사(Weekly Deep Cycle Count)**에 대한 운영 원칙, 타깃팅 알고리즘, 리치트럭/지게차 동선 전략 및 데이터 구조를 정의합니다.

---

## 📅 주간 운영 일정 및 발송 규칙 (Schedule & Policy)

- **정기 발송 일시**: **매주 토요일 (Saturday) 아침 06:50 EDT** (07:20 전 산출 및 발송 완료)
- **실행 모드**: `[Weekly]` Cycle Count List
- **발송 대상**:
  - **수신 (To)**: Receiving Warehouse Team (`STOCK_ACCURACY_TO`)
  - **참조 (CC)**: Materials Team (`STOCK_ACCURACY_CC`)
- **일요일 정책 (Sunday Policy)**:
  - 일요일(Sunday)에도 WMS 스냅샷 다운로드, 어제자 GR/GI 확정분 DB 적재, 온톨로지 회전일수 재계산은 **365일 매일 무중단 실행**됩니다.
  - 단, 현장 작업자의 피로도 관리 및 휴무일을 고려하여 **일요일 정기 알림 메일 발송은 자동으로 건너뜁니다(Skip)**.

---

## 🎯 Daily vs Weekly 실사 레벨링 (Leveling Comparison)

창고 운영 현장의 물리적 동선과 장비 활용 특성을 극대화하여 Daily와 Weekly의 역할을 완벽히 분리했습니다:

| 구분 | Daily Cycle Count (월~금) | Weekly Cycle Count (토요일) |
|---|---|---|
| **실사 성격** | **15분 보행 신속 실사 (Rapid Walking Audit)** | **주간 전사적 심층 실사 (Deep Inventory Audit)** |
| **운영 구역 및 장비** | Plant 1 1·2층 도보 실사 (사다리/핸드 카트) | Plant 3 하이랙(`R...`), 3~5층 고층 랙 (**리치트럭 / 포크리프트 필수**) |
| **핵심 타깃 1** | SMT 슈퍼마켓, OEM 바닥 버퍼 및 리팩 대차 정체 (`SMT/OEM Aging`) | Plant 3 하이랙 & 고층 랙 장기 체류 (`High-Rack Aging`) |
| **핵심 타깃 2** | Plant 1 1·2층 소물류 수기 피킹 (`Small Part Risk`) | **Fast Mover 장기 체류 잔여 LOT (`Fast Mover Residual`)** |
| **핵심 타깃 3** | 동일 품목 선입선출 역전 (`FIFO Inversion`) | 온톨로지 회전일수 대비 극단적 미회전치 (`Turnover Outlier`) |
| **실사 규모** | **40개 빈** (Top 15 + Extended 25) | **50개 빈** (Rank #1 ~ #50 단일 통합 리스트) |
| **첨부 엑셀 파일** | `audit_reports/WMS_Cycle_Count_Audit_List_YYYYMMDD.xlsx` | `audit_reports/WMS_Weekly_Cycle_Count_List_YYYYMMDD.xlsx` |

---

## 💡 Fast Mover 실사 방식 결정 배경 (Strategic Decision)

### ❓ 고민: "Fast Mover는 단순 랜덤 실사가 맞는가, 아니면 빠른(=오래된) LOT 기준 실사가 맞는가?"

> [!IMPORTANT]
> **결론: "Fast Mover의 최장 체류 잔여 바코드 (Oldest Residual LOTs)"를 집중 실사합니다.**

1. **단순 무작위(Random) 실사의 문제점**:
   - Fast Mover(초고속 회전 부품)는 하루에도 수십 상자씩 입출고가 이루어지는 건강한 부품입니다.
   - 이상 징후가 없는 최근 입고 정상 로트를 무작위로 추출하여 실사할 경우, 현장 작업자는 불필요한 카운트 피로도를 겪게 됩니다.
2. **Oldest Residual LOT 타깃팅의 강력한 효과**:
   - Fast Mover 품목은 1년 온톨로지 분석상 **표준 회전일수가 1~3일**에 불과합니다.
   - 그런데 동일 품목의 최신 로트가 계속 입고되어 소비되는 동안, **특정 로트/바코드가 2주(14일) 이상 WMS에 남아있다면?**
     - **원인 ① (전산상 유령 재고 Ghost Barcode)**: 작업자가 후속 입고분을 먼저 출고하고 바코드 스캔을 최신 것으로 찍어, 실물은 이미 생산 라인에 투입되어 물리적으로 없는데 WMS에만 남아있는 경우 (오스캔 누적).
     - **원인 ② (물리적 방치/누락)**: 랙 깊숙한 곳이나 구석에 밀려 장기 방치된 불량/누락 재고.
   - 따라서 **"동일 품목의 최신 입고 로트 대비 14일 이상 오래되었거나 표준 회전일수 대비 5배 이상 장기 체류 중인 잔여 바코드"**를 토요일 주간 실사 1순위로 선정하여 전산상 허수 재고를 완벽히 털어냅니다.

---

## 📧 메일 본문 구성 및 핵심 6대 컬럼

Daily 메일과의 일관성을 100% 유지하여 현장 혼선을 완전히 방지했습니다:

- **메일 제목**: `[Weekly] Cycle Count List (YYYY-MM-DD)`
- **표 구성**: 6개 핵심 컬럼으로 50개 빈 일괄 표기 (`Rank #1 ~ #50`)

| 컬럼명 | 설명 | 비고 |
|---|---|---|
| **Rank** | 우선순위 순위 (#1 ~ #50) | 위험도 점수 및 바코드 수량 가중치 정렬 |
| **Bin #** | 실제 현장 물리적 랙 빈 번호 | `BINSYNTH9953`(비물리적 빈)는 실사 동선 왜곡 방지를 위해 제외 |
| **Part #** | 해당 빈에 적치된 대상 품번 | 복수 품목 적치 시 콤마(`,`)로 나열 |
| **Label QTY** | 의심 라벨(바코드/박스) 수 | 실사 대상 박스 수 |
| **Action Required** | 현장 조치 유형 | `High-Rack Audit`, `Fast Mover Audit`, `Turnover Audit`, `SMT Audit` 등 |
| **Reason** | 핵심 선정 사유 | `High-Rack Aging`, `Fast Mover Residual`, `Turnover Outlier`, `SMT Lingering` 등 |

---

## 📊 주간 실사 엑셀 시트 구조 (`WMS_Weekly_Cycle_Count_List_YYYYMMDD.xlsx`)

전체 분석 결과는 메일에 자동 첨부되는 멀티 시트 엑셀 워크북에 체계적으로 분할 수록됩니다:

1. **`Cycle_Count_Target_Bins`**:
   - 주간 메인 실사 리스트 (Rank 1부터 50까지의 실제 물리적 빈 요약 표 + 무작위 검증 5개 빈 = 총 55개 빈).
   - **초간단 5대 컬럼 (`Rank | Bin # | Part # | Label QTY | Counted QTY`)** 전면 적용.
   - **세로 1페이지 인쇄 최적화 (`fitToWidth=1, fitToHeight=1`)**, 11pt 시원한 폰트, 여백 0.25", 행 높이 16.5pt로 현장 실사 볼펜 기입 및 1장 완벽 출력 지원.
2. **`HighRack_Aging_Master`**:
   - Plant 3 하이랙(`R...`) 및 Plant 1 3~5층 고층 랙 체류 바코드 전체 상세 데이터 (리치트럭 기사용).
3. **`FastMover_Residual_Bins`**:
   - 초고속 회전 품목(Fast Mover) 중 장기 미출고 잔여 체류 바코드 목록.
4. **`Turnover_Outliers_All`**:
   - 온톨로지 표준 회전일수 대비 극단적으로 체류가 지연된 품목 전체 목록.
5. **`All_Suspects_Top500`**:
   - 전체 위험도 상위 500개 개별 바코드 원본 데이터 (바코드, 품번, 품명, 수량, 로트일자, 체류일수 등).

---

## 🛠️ 수동 실행 및 테스트 커맨드

주간 실사 파이프라인은 토요일 아침 06:50 EDT 스케줄러를 통해 자동 실행되지만, 필요 시 터미널에서 언제든 수동 실행 및 검토할 수 있습니다:

```powershell
# 1. 주간 실사 분석 및 엑셀 생성 테스트 (이메일 발송 없이 검증)
python daily_pipeline.py --mode Weekly --skip-email

# 2. 주간 실사 이메일 본문 Outlook 창으로 미리보기 (발송 전 검토)
python warehouse_notifier.py --mode Weekly --display

# 3. 주간 실사 정식 즉시 발송
python warehouse_notifier.py --mode Weekly --send
```
