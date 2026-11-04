import sys
import time
from pathlib import Path
from graph_client import MicrosoftGraphClient

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

def monitor_approval(max_hours: int = 12):
    print(f"=== M365 Teams 승인 백그라운드 모니터링 시작 (최대 {max_hours}시간) ===", flush=True)
    start_time = time.time()
    client = MicrosoftGraphClient()
    
    while time.time() - start_time < max_hours * 3600:
        # 1. 이미 인증되었는지 확인
        status = client.get_status()
        if status.get("authenticated"):
            print(f"\n[성공] Teams 및 Graph API 인증이 확인되었습니다! 사용자: {status.get('user_display_name')}", flush=True)
            with open(Path(__file__).parent / "APPROVAL_COMPLETED.txt", "w", encoding="utf-8") as f:
                f.write(f"Approval completed at {time.ctime()} for {status.get('user_principal_name')}\n")
            return True
        
        # 2. 새로운 디바이스 코드 갱신 및 시도
        flow = client.start_device_code_flow()
        if flow.get("success"):
            print(f"[{time.strftime('%H:%M:%S')}] 대기 중... 현재 코드: {flow.get('user_code')}", flush=True)
        
        # 10분 대기 후 재확인
        time.sleep(600)
    
    print("모니터링 시간이 초과되었습니다.", flush=True)
    return False

if __name__ == "__main__":
    monitor_approval()
