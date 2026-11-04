# 02_JOB_Backlog_Clear (SAP COGI Backlog Automation & Clearance Job)

> **공장 기준**: Plant `P001` (Headlamp) 전담  
> **실행 주기**: 매일(주말/토·일 포함 365일 무휴) EST 기준 **06:10 AM** (1차: 사전 Refresh 및 원본 아카이빙), **08:15 AM** (2차: 최종 리포트 발송)  
> **선행 조건**: `01_JOB_GR_GI_Error`의 GI 에러 자동 해소 완료 후 실행 (주말 동일, 10~15분 시차 버퍼)  
> **헤더 식별**: Header Text = `Backlog_Clear`, Recipient = `AUTO_AGENT`  
> **공식 수신인 (TO)**: `.env`의 `BACKLOG_PROD_TO` 참조 (Receiving W/H Leader, Supervisor, Production Control)  
> **공식 참조인 (CC)**: `.env`의 `BACKLOG_PROD_CC` 참조 (Material Planning, CKD)  
> *(테스트 시에는 `--test-email` 플래그로 지정 테스터 1인에게만 발송)*

`02_JOB_Backlog_Clear`는 ACME Manufacturing P001 공장의 생산 확정 후 자재 출고 불일치 에러(SAP COGI)를 실시간 추출하고, 동시 시점의 WMS-SAP 재고정확도를 분석하여 **전사 재고정확도를 개선하거나 보존하는 범위($\Delta V \le 0$)** 내에서만 생산창고(`SP31`, `SP11`) 및 품질창고(`SL49`)로의 이동전기(MB1B 311, Header Text: `Backlog_Clear`)를 자동 수행한 뒤 COGI 재처리(Save)까지 완료하며, 처리 불가 건의 상세 사유를 매일 아침 보고하는 시스템입니다.

---

## 🛡️ 핵심 운영 및 안전 규칙 (Core Domain Rules)

1. **파이프라인 선행 종속성 및 스마트 핸드셰이크 (Pipeline Pre-requisite & Smart Handshake)**:
   - 백로그 처리 실행 전, 반드시 **`01_JOB_GR_GI_Error`의 GI 에러 해소 파이프라인이 선행 완료**되어 있어야 합니다.
   - **사유**: GI 에러가 남아있는 상태에서 백로그를 이동시키면 창고 간 이중 전표 왜곡 및 엉뚱한 결품이 전파됩니다.
   - **타이밍 분리 (Staggered Buffer)**: `01_JOB_GR_GI_Error`가 06:00, 08:00 정각에 돌아 45~90초 만에 GI 에러를 소탕하므로, 백로그는 **06:10 AM** 및 **08:15 AM**에 실행되어 완벽히 클린해진 재고 상태에서 안심하고 착수합니다.
   - **스마트 핸드셰이크**: `backlog_pipeline.py`는 기동 시 SQLite DB(`pipeline_run_metrics`)를 조회하여 **최근 25분 이내에 성공적으로 끝난 GI 실행 이력**이 있으면 불필요한 중복 실행 없이 즉시 백로그 작업으로 진입합니다. 수동 실행 등으로 최근 이력이 없을 때만 안전하게 GI 파이프라인을 선행 호출합니다.
   - **SAP 세션 상호 락 (`sap_lock.py`)**: 두 파이프라인 모두 SAP GUI 화면을 점유하기 전 크로스 프로세스 뮤텍스 락(`sap_session_lock`)을 획득하므로 화면 가로채기나 키 입력 충돌이 원천 차단됩니다.
2. **매일 06:10 AM COGI 사전 Refresh(Save) 및 처리 전 원본 백로그 히스토리 영구 보관**:
   - 매일 아침 06:10 AM 1차 수집 전, 먼저 SAP COGI에 진입하여 **전체선택(`btn[9]`) $\rightarrow$ 저장(`btn[11]`)**을 실행하여 야간 배치 확정분이나 자재 마스터 잠금(`M3 897`) 해제건을 1차 자동 Refresh합니다.
   - Refresh 직후 SAP COGI에서 새로 추출한 리스트가 당일 조치 대상이 되는 **"처리 전 원본 백로그 히스토리(Pre-Resolution Backlog History)"**입니다.
   - 원본 엑셀 파일은 `cogi_errors/raw_history/` 폴더에 `COGI_RAW_INITIAL_P001_YYYYMMDD_HHMMSS.XLS`로 영구 보관됩니다.
   - 동시에 SQLite DB의 **`raw_backlog_history`** 테이블에 매일 전건 누적 적재되어 감사 및 추적용으로 영구 저장됩니다.
3. **동시 시점 재고정확도 분석 및 가드레일 (Variance Guardrail)**:
   - 동일 시점(T=0)의 WMS 재고와 SAP MB52 재고를 비교하여 품번별/창고별 오차를 산출합니다.
   - 전사 재고 오차: $V = \sum_{loc} |SAP_{loc} - WMS_{loc}|$
   - 이동 후 가상 오차 변화량: $\Delta V = V_{after} - V_{before} \le 0$
   - **엄격한 규칙**: $\Delta V \le 0$을 만족하는 이동전기만 승인(`TRANSFER_POSTING`). 오차를 증가시키는 위험 건은 일체 발행하지 않고 안전하게 차단(`SKIP`)하여 미처리 목록으로 분류합니다.
    - **정합 창고 재고정합도 100% 절대 보존 가드레일 (Matched Warehouse Accuracy Protection - 전사 창고간 이동 공통)**:
      - 자재창고($S \in \{\text{SL00, SL01, SL02, SL03, SL49}\}$)의 WMS 재고와 SAP 재고가 일치($\text{SAP}_S = \text{WMS}_S$, 즉 $\text{Surplus}_S \le 0$)하는 경우:
      - 생산라인(`SP31`, `SP11`) 또는 타 창고(`SL02`, `SL49` 등)에 백로그나 결품이 발생하더라도 **정상 정합 창고에서는 절대 재고를 불출하지 않고 안전하게 차단(`SKIP`, `PRESERVED_WAREHOUSE_ACCURACY`)**합니다.
      - **핵심 사유**: 창고의 재고가 완벽히 일치한다는 것은 해당 창고가 출고를 누락하지 않았음을 증명합니다. 백로그 원인은 창고 미출고가 아니라 생산 라인의 초과 소모, 폐기 미반영, BOM 오류 등입니다. 억지로 정상 창고 재고를 털어 백로그를 메우면 멀쩡한 창고의 재고정합도 100%가 파괴(전산 부족 발생)되므로 출고를 통한 백로그 처리를 원천 배제합니다.
      - **가용 후보 창고 엄격 제한**: 백로그 이동전기 대상 출하창고는 반드시 **$\text{Surplus} = \text{SAP}_S - \text{WMS}_S > 0$인 순수 잉여 창고**로만 한정되며, 이동 수량 또한 가용 잉여 한도($q \le \text{Surplus}_S$) 내로 엄격히 제한됩니다.
4. **GR 에러 미해소 종속성 추적 및 보고 분리 (GR Dependency Tracking)**:
   - 백로그가 처리되지 못한 사유 중 **"GR 에러(ZTCODE_GI_ERR)가 안 풀려서 SAP 전산 재고로 유입되지 못한 건"**을 자동 교차 분석합니다.
   - 상태 코드: `BLOCKED_BY_GR_ERROR`
   - 사유 명시: `Pending inbound receipt blocked by GR error (Pending Qty: X EA, Vendor: OOO) - Upon PIC clearing GR error, stock accuracy is restored & backlog auto-clears`
   - 매일 아침 08:15 AM 리포트에서 **"📦 [Action Required by Each Planner] Pending GR Errors Blocking Backlog"** 섹션으로 별도 분리하여 각 플래너에게 명확한 조치 가이드를 제공합니다.
5. **COGI 조회 일자 범위 규칙 (Date Range: 월초부터 어제까지)**:
   - **기본 규칙 (2일~말일)**: 당월 1일부터 **어제(Yesterday)**까지 조회 및 수정합니다.
     - 예: 오늘이 10월 23일이면 조회 범위는 `2026.10.01` ~ `2026.10.22` 입니다.
   - **월초 1일 규칙 (Month-Beginning Guardrail)**: 오늘이 매월 1일인 경우, 전월 백로그 처리를 위해 **전월 1일부터 전월 말일**까지 조회합니다.
     - 예: 오늘이 10월 1일이면 조회 범위는 `2026.09.01` ~ `2026.09.30` 입니다.
6. **MB1B 이동전기 전표일/전기일 민감도 (Posting Date Sensitivity)**:
   - 전월 백로그를 처리할 때(오늘이 1일이거나 해당 에러의 발생월이 전월인 경우), MB1B 311 자재 이동전기는 **무조건 전월 날짜(전월 말일, 예: `2026.09.30`)로 전기**해야 합니다.
   - 당월 에러 건은 당일 일자로 정상 전기합니다.
7. **대상 창고 지정 및 이동 규칙**:
   - 백로그 발생 목적지 창고: **생산창고(`SP31`, `SP11`)** 혹은 **품질창고(`SL49`)**
   - 공급 창고: WMS 대비 SAP 잉여재고($Surplus > 0$)가 존재하고 가용 재고가 충분한 보관창고(`SL01`, `SL00`, `SL03` 등)
   - 전표 식별: Header Text는 반드시 **`Backlog_Clear`**, Recipient는 **`AUTO_AGENT`**로 기록합니다.
8. **MB1B 이동 후 COGI 재처리(Save) 동적 완료 감지 및 실물 검증 (Dynamic Completion Sensing & Verification)**:
   - 품목 수가 많거나 대량 데이터 재처리 시 SAP가 백그라운드 출고(261) 처리를 완료할 때까지 처리 시간이 수십 초 이상 걸릴 수 있습니다.
   - 단순 고정 시간 대기(Sleep)를 사용하지 않고, SAP GUI 세션의 **`session.Busy` 해제 여부**, **모달 팝업창(`wnd[1]`, 완료 안내창) 등장 여부**, **하단 상태표시줄(`wnd[0]/sbar`) 메시지 갱신**을 매초 실시간 모니터링(최대 120초 동적 대기)합니다.
   - 완료 알림창이나 상태 메시지가 감지되는 즉시 팝업을 자동 닫기(`btn[0]`)하고 트랜잭션을 정상 종료(`/n`)합니다.
   - **DB 커밋 대기 및 실물 재조회**: SAP 백그라운드 DB 갱신(V1 Update Task) 커밋 완료를 위해 5초간 대기한 후, COGI를 즉시 재조회(Post-Verification Export)하여 실제 SAP 화면에 남은 실물 잔여 백로그를 직접 확인합니다.
   - **정합성 100% 보장**: 이 실물 검증 데이터가 확인된 후에만 최종 엑셀 및 이메일 리포트가 발송되므로, 수신자가 메일을 확인하고 COGI에 들어갔을 때 화면과 메일 내용이 1초의 오차도 없이 완벽하게 일치합니다.
9. **아침 08:15 AM 일일 종합 리포트 발송 (06:10 AM & 08:15 AM 당일 누적 합산 반영)**:
   - **메일 제목 및 본문 헤더**: `Daily Backlog (COGI) Resolution & Status Report (날짜, 시간)`
   - **당일 누적 실적 종합 (Cumulative Resolution)**: 08:15 단일 실행분만 보여주지 않고, SQLite DB(`backlog_transfers`) 및 `raw_backlog_history`를 연계하여 **06:10 AM 1차 처리분 + 08:15 AM 2차 처리분을 합산한 당일 전체 실적**을 일목요연하게 보고합니다.
     - **`Total Daily Backlog`**: 당일 총 관리 대상 백로그 건수 및 수량 (06:10 최초 원본 기준 + 08:15 신규 발생분)
     - **`Transfer Posted (Today)`**: 06:10 AM 및 08:15 AM에 완료된 **당일 누적 이동전기 전표 건수 및 수량**
     - **`Completed Transfer Postings` 테이블**: 각 품번별 이동전기 전표에 실행 시간대(`Run Cycle`: `06:10 AM` / `08:15 AM`) 배지를 명시하여 어떤 차수에 처리되었는지 즉시 파악 가능
   - **1차 펜딩**: `Pending Goods Receipt (GR) Errors • Action Required by Each Planner` (08:15 시점 최종 잔여 중 GR 에러 종속 건 - 플래너 확인 필요 건)
   - **2차 펜딩**: `Pending Backlog Items` (08:15 시점 최종 잔여 - 자재,생산 함께 확인 필요 건)
   - **첨부파일**: 상세 개별 품번 내역이 담긴 `Unresolved_Backlog_Report_YYYYMMDD_0815.xlsx` 자동 첨부.

---

## 📂 디렉토리 구조

```text
02_Backlog/
├── db/                         # 🗄️ 백로그 이력 및 실행 메트릭 SQLite DB (WAL 모드)
│   └── backlog_history.db
├── cogi_errors/                # 📥 COGI 추출 엑셀 파일 저장소 (UTF-16)
│   ├── raw_history/            # 📦 [영구 보관] 매일 06:10 AM Refresh 후 처리 전 원본 백로그 히스토리
│   │   └── COGI_RAW_INITIAL_P001_YYYYMMDD_HHMMSS.XLS
│   └── COGI_P001_YYYYMMDD_HHMMSS.XLS
├── mb52/                       # 📊 동시 수집 MB52 스냅샷
├── resolution_logs/            # 📑 백로그 이동 계획 및 08:15 AM 미처리 엑셀 리포트
│   ├── Backlog_Resolution_Plan_YYYYMMDD_HHMMSS.xlsx
│   └── Unresolved_Backlog_Report_YYYYMMDD_0815.xlsx
├── backlog_pipeline.py         # 🚀 [메인 통합 실행기] 06:10, 08:15 스케줄러 / 데몬 (스마트 GI 연계)
├── cogi_collector.py           # 📥 SAP COGI 사전 Refresh(Save), 원본 추출, 재처리(Save) 실행기
├── backlog_resolver.py         # 🧠 재고정확도 시뮬레이션 및 GR 에러 연계 이동 계획 엔진
├── mb1b_backlog_executor.py    # 🤖 MB1B 311 전표 발행기 (Header: Backlog_Clear)
├── db_manager.py               # 🗃️ SQLite DB 이력(raw_backlog_history 등) 및 감사 추적 관리자
├── notifier.py                 # 📧 매일 아침 08:00 AM 미처리 백로그 Outlook 이메일 보고기
├── sap_launcher.py             # 🔑 SAP GUI 무중단 헤드리스 실행 및 SSO 세션 관리자
└── README.md
```

---

## 🗄️ 데이터베이스 스키마 (`db/backlog_history.db`)

SQLite WAL(Write-Ahead Logging) 모드를 적용하여 다중 조회 시 블로킹을 방지합니다.

### 1. `raw_backlog_history` (매일 06:10 AM 처리 전 원본 백로그 히스토리 - 신규)
- `id`: 고유 식별자 (PK)
- `history_id`: 히스토리 고유 배치 번호 (예: `BACKLOG_06:10_20261023_061000`)
- `run_date`, `run_time`: 수집 일자 및 차수 (`2026-10-23`, `06:10`)
- `material`: 품번 (예: `99000042`)
- `material_desc`: 품명
- `plant`: 플랜트 (`P001`)
- `sloc`: 결품 발생 창고 (`SP31`, `SP11`, `SL49`)
- `mty`: 이동유형 (`261`)
- `qty`: 결품 수량
- `uoe`: 단위 (`EA`, `KG`, `LB`)
- `error_code`: SAP 에러 코드 (예: `M7 021`, `M3 897`)
- `entry_date`, `error_date`: 등록일자 및 발생일자
- `source_file`: 원본 파일명 (`COGI_RAW_INITIAL_P001_YYYYMMDD_HHMMSS.XLS`)
- `created_at`: 레코드 생성 시각

### 2. `cogi_snapshots` (COGI 사이클별 운영 스냅샷)
- `id`: 고유 식별자 (PK)
- `snapshot_id`: 배치 고유 번호 (예: `BACKLOG_08:00_20261023_080000`)
- `material`, `plant`, `sloc`, `mty`, `qty`, `uoe`, `error_code`
- `resolution_status`: 처리 상태 (`PENDING`, `APPROVED_FULL_RESOLUTION`, `BLOCKED_BY_GR_ERROR`, `SKIP`)

### 3. `backlog_transfers` (MB1B 311 이동전기 실행 감사 이력)
- `id`: 고유 식별자 (PK)
- `batch_id`: 배치 번호
- `material`, `from_sloc`, `to_sloc`, `transfer_qty`: 자재 및 이동 수량
- `header_text`: `Backlog_Clear` (고정)
- `recipient`: `AUTO_AGENT`
- `sap_doc_no`: 생성된 SAP 자재전표 번호 (10자리)
- `sap_raw_msg`: SAP 상태바 원문 응답
- `status`: `SUCCESS` / `FAILED`
- `variance_before`, `variance_after`, `delta_variance`: 재고정확도 오차 변화량 감사 기록

### 4. `daily_backlog_metrics` (일일 실행 및 발송 통계)
- `run_date`, `run_time`: 실행 일자 및 차수 (`06:10` / `08:15`)
- `total_cogi_items`, `total_cogi_qty`: 총 백로그 건수 및 수량
- `resolved_items`, `resolved_qty`: 자동 해소 완료 건수 및 수량
- `unresolved_items`, `unresolved_qty`: 최종 미처리 건수 및 수량
- `email_sent`, `email_sent_at`: 이메일 발송 여부 및 시각

---

## 🚀 실행 가이드

### 1. 즉시 1회 테스트 실행 (수동 모드)
```bash
# 1) 즉시 전체 사이클 실행 (선행 GI 클리어 검증 -> 06:10 형식의 사전 Refresh 및 원본 아카이빙 -> MB1B 이동 -> COGI 저장)
python backlog_pipeline.py --run-now --pre-refresh

# 2) 실행과 동시에 08:15 AM 형식의 이메일 및 엑셀 리포트까지 발송 테스트 (나에게만 안전 발송)
python backlog_pipeline.py --run-now --test-email

# 3) 운영 배포용 전체 수신인/참조인 공식 발송
python backlog_pipeline.py --run-now --email
```

### 2. 매일 06:10 AM / 08:15 AM 정기 스케줄러 가동
```bash
python backlog_pipeline.py --schedule
```
- **06:10 AM (1차)**:
  1. `01_JOB_GR_GI_Error` 스마트 핸드셰이크 (06:00 정시 해소 완료 이력 자동 감지 후 스킵)
  2. SAP COGI 진입 $\rightarrow$ **사전 Refresh (전체선택 $\rightarrow$ 저장)**
  3. 갱신된 백로그 목록을 **처리 전 원본 히스토리**로 `cogi_errors/raw_history/` 및 DB `raw_backlog_history`에 영구 보관
  4. 재고정확도 가드레일($\Delta V \le 0$) 분석 및 MB1B 311 이동전기 (`Backlog_Clear`)
  5. COGI 재진입 $\rightarrow$ 최종 저장(Save) 확정 (이메일 미발송)
- **08:15 AM (2차)**:
  1. `01_JOB_GR_GI_Error` 스마트 핸드셰이크 (08:00 정시 해소 완료 이력 자동 감지 후 스킵)
  2. 백로그 추출 및 2차 MB1B 311 이동전기 + COGI 재처리 확정
  3. 최종 미처리 백로그 목록 및 GR 에러 대기건 **Outlook 일일 리포트 자동 발송**