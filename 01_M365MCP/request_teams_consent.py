import sys
import time
import webbrowser
from typing import Dict, Any

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import pyperclip
import json
from pathlib import Path
from graph_client import MicrosoftGraphClient

_orig_print = print
def print(*args, **kwargs):
    kwargs.setdefault("flush", True)
    _orig_print(*args, **kwargs)

def trigger_consent_request():
    print("=" * 65)
    print("      Microsoft Teams 연동 및 관리자 승인 요청 (Entra ID)")
    print("=" * 65)
    
    client = MicrosoftGraphClient()
    print("\n[1단계] Microsoft 디바이스 인증 요청 생성 중...")
    
    flow_res = client.start_device_code_flow()
    if not flow_res.get("success"):
        print(f"❌ 실패: {flow_res.get('error')}")
        return

    user_code = flow_res["user_code"]
    verify_url = flow_res["verification_uri"]
    flow_obj = flow_res["flow"]

    # 현재 코드 저장
    auth_file = Path(__file__).parent / "CURRENT_AUTH.json"
    with open(auth_file, "w", encoding="utf-8") as f:
        json.dump({"user_code": user_code, "verification_uri": verify_url, "created_at": time.time()}, f)

    # 1. 클립보드에 복사
    try:
        pyperclip.copy(user_code)
        clipboard_msg = " (클립보드에 자동 복사되었습니다! Ctrl+V로 붙여넣으세요)"
    except Exception:
        clipboard_msg = ""

    print(f"\n[2단계] 인증 코드 발급 완료!")
    print(f"  👉 인증 코드: 【 {user_code} 】{clipboard_msg}")
    print(f"  👉 인증 URL: {verify_url}")

    # 2. 브라우저 자동 오픈
    print("\n[3단계] 브라우저를 엽니다...")
    try:
        webbrowser.open(verify_url)
    except Exception:
        print(f"브라우저를 직접 열고 {verify_url} 로 접속해주세요.")

    print("\n" + "-" * 65)
    print("💡 브라우저에서 진행하실 내용:")
    print("  1. 열린 페이지에 코드 입력 후 회사 계정(<corporate-account>)으로 로그인")
    print("  2. 만약 바로 승인 화면이 나오면 [동의/수락] 클릭")
    print("  3. 만약 '관리자 승인 필요' 화면이 나오면:")
    print("     - 사유(예: 'MCP AI 어시스턴트 연동을 통한 Teams 업무 자동화') 입력")
    print("     - 【 승인 요청 】 버튼 클릭!")
    print("-" * 65)
    print("💬 관리자 승인 요청 후 팀즈 메신저로 보낼 메시지 예시:")
    print("   '관리자님 안녕하세요, MCP 업무 자동화 연동을 위해 방금 Azure AD에")
    print("    승인 요청(요청자: <담당자>)을 등록했습니다. 승인 부탁드립니다!'")
    print("-" * 65)
    
    print("\n[4단계] 관리자 승인 대기 중... (승인이 완료되면 자동으로 감지하여 저장합니다)")
    print("(취소하시려면 Ctrl+C를 누르세요)\n")

    res = client.complete_device_code_flow(flow_obj)
    if res.get("success"):
        print("\n" + "=" * 65)
        print(f"🎉 축하합니다! Teams 및 M365 클라우드 인증이 완료되었습니다!")
        print(f"   로그인된 사용자: {res.get('user')}")
        print("   이제 Teams 채팅 조회, 메시지 전송 기능이 활성화되었습니다.")
        print("=" * 65)
    else:
        print(f"\n⚠️ 결과: {res.get('error')}")

if __name__ == "__main__":
    trigger_consent_request()
