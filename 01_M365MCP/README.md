# Microsoft 365 (M365) Hybrid MCP Server

이 프로젝트는 **Microsoft 365 (Outlook 메일/캘린더, OneDrive, Excel, Teams, SharePoint)**와 AI 에이전트를 연결하는 **하이브리드 Model Context Protocol (MCP)** 서버입니다.

---

## 🌟 핵심 특징 (하이브리드 아키텍처)

1. **로컬 모드 (즉시 사용 가능, 별도 Azure 설정 불필요)**
   - Windows에 로그인되어 실행 중인 **Outlook 데스크톱 앱** 및 **로컬 OneDrive**, **Excel**을 COM 인터페이스를 통해 직접 제어합니다.
   - 복잡한 Azure Entra ID(Azure AD) 테넌트 관리자 승인 절차 없이, **현재 PC 계정으로 메일 조회, 발송, 일정 관리, 엑셀 처리**가 즉시 가능합니다.

2. **클라우드 모드 (Microsoft Graph API)**
   - Teams 채팅/채널 메시지 송수신, 클라우드 SharePoint 사이트 및 파일 검색 등을 지원합니다.
   - 필요 시 `.env`에 `M365_CLIENT_ID`를 설정하고 디바이스 코드(`python auth_cli.py login`)로 한 번만 인증하면 토큰이 로컬에 안전하게 캐싱되어 유지됩니다.

---

## 🛠️ 제공되는 MCP 도구 목록

### 1. 상태 확인 및 진단
- `m365_get_status`: 로컬 Outlook, OneDrive 및 클라우드 Graph API의 연결 상태 및 준비된 기능 확인

### 2. Outlook 이메일
- `outlook_get_unread_emails(max_count=10)`: 받은 편지함의 최근 읽지 않은 메일 목록 조회
- `outlook_search_emails(query, max_count=10)`: 키워드(제목, 발신자, 본문)로 최근 메일 검색
- `outlook_read_email(entry_id)`: 특정 메일의 본문 전체 텍스트 및 첨부파일 목록 확인
- `outlook_create_draft(to, subject, body, cc, bcc)`: Outlook 임시 보관함에 메일 초안 작성 (즉시 발송되지 않음)
- `outlook_send_email(to, subject, body, cc, bcc, display_first)`: 메일 발송 (display_first=True 시 발송 전 창을 띄워 사용자 검토 가능)

### 3. Outlook 캘린더
- `outlook_get_calendar_events(days_ahead=7)`: 향후 N일간의 회의 및 일정 조회
- `outlook_create_calendar_event(subject, start_time, end_time, location, body, reminder_minutes)`: 새 일정 및 회의 등록

### 4. 로컬 OneDrive
- `onedrive_list_files(subpath, max_count=50)`: 로컬 OneDrive 폴더 내 파일 및 디렉터리 목록 조회
- `onedrive_search_files(keyword, ext_filter, max_count=30)`: OneDrive 내 파일 재귀 검색

### 5. Excel 자동화
- `excel_list_sheets(file_path)`: 엑셀 파일 내 시트 목록 조회
- `excel_read_sheet(file_path, sheet_name, max_rows=50)`: 엑셀 시트 데이터를 JSON/테이블 형식으로 읽기
- `excel_write_cell(file_path, sheet_name, cell, value)`: 특정 셀(예: 'B2')에 값 입력/수정

### 6. PowerPoint (PPT) 자동화
- `ppt_read_presentation(file_path, max_slides=50)`: 슬라이드 제목, 본문 텍스트, 표, 발표자 노트 추출
- `ppt_create_presentation(file_path, title, subtitle, slides_data_json)`: 새로운 PPT 프레젠테이션 생성

### 7. Teams & 클라우드 SharePoint (Graph API 설정 시)
- `teams_list_chats()`: 최근 Teams 채팅 목록 조회
- `teams_get_messages(chat_id)`: 특정 채팅방의 대화 내역 조회
- `teams_send_chat_message(chat_id, content)`: Teams 채팅방에 메시지 전송
- `teams_list_joined_teams()`: 소속된 Teams 팀 목록 조회
- `teams_list_channels(team_id)`: 팀 내 채널 목록 조회
- `teams_send_channel_message(team_id, channel_id, content)`: 팀 채널에 메시지 전송
- `sharepoint_search_sites(query)`: 접근 가능한 SharePoint 사이트 검색
- `onedrive_cloud_search(query)`: 클라우드 OneDrive 파일 검색
- `onedrive_cloud_upload(local_path, remote_folder)`: 클라우드 OneDrive에 파일 업로드

---

## 🚀 빠른 시작 및 상태 확인

### 연결 상태 확인 CLI
```bash
python auth_cli.py status
```
실행 시 로컬 Outlook, OneDrive, Microsoft Graph의 연결 상태를 한눈에 확인할 수 있습니다.

### Teams / Cloud Graph API 활성화 및 관리자 승인 요청
Teams 연동을 위한 Microsoft Entra ID(Azure AD) 로그인 및 관리자 승인 요청은 아래 한 줄로 즉시 실행할 수 있습니다:
```bash
python request_teams_consent.py
```
1. 코드가 클립보드에 자동 복사되고 브라우저가 자동으로 열립니다.
2. 브라우저에서 코드를 붙여넣고 회사 계정으로 로그인합니다.
3. **[관리자 승인 필요]** 화면이 나타나면 사유를 입력하고 **[승인 요청]** 버튼을 클릭합니다.
4. 관리자에게 팀즈로 승인 요청 사실을 알리고, 승인이 완료되면 스크립트가 자동으로 인증을 완료합니다!

---

## 📂 파일 구조
- `server.py`: MCP Server 구동 진입점 (Stdio 프로토콜)
- `local_office.py`: Outlook COM, 로컬 OneDrive, Excel, PowerPoint 제어 로직
- `graph_client.py`: Microsoft Graph API 및 MSAL 인증 클라이언트
- `config.py`: 환경 변수 및 공통 설정
- `request_teams_consent.py`: Teams 관리자 승인 요청 및 브라우저/클립보드 연동 스크립트
- `auth_cli.py`: 연결 상태 확인 및 로그인 지원 CLI
- `.env.example`: Azure 설정 템플릿

---

## 🏛️ ACME Manufacturing 자재관리 가상 조직 및 전사 데이터 온톨로지
- **전사 부서 간 데이터 온톨로지 & 지식그래프**: [ENTERPRISE_DATA_ONTOLOGY.md](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/ENTERPRISE_DATA_ONTOLOGY.md) (부서별 DB 연결 및 5대 불변 엔터티 명세)
- **전사 조직도 및 팀별 운영 현황 총괄**: [00_HQ_TaskManager](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/00_HQ_TaskManager/README.md)
- **재고/자재 부서 통합 DB 아키텍처**: [01_DEPT_Inventory/DATABASE_ARCHITECTURE.md](file:///%USERPROFILE%/Workspace/manufacturing-agent-org/01_M365MCP/01_DEPT_Inventory/DATABASE_ARCHITECTURE.md)


