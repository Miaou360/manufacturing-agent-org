"""
Resource Guard & Concurrency Controller
로컬 PC(Windows 11, 12코어, 32GB OEM)의 시스템 자원을 보호하고,
과부하 없이 안전하게 병렬 처리 및 배치 작업을 수행할 수 있도록 지원하는 공용 헬퍼 모듈입니다.
"""

import os
import gc
import sys
import time
import psutil
import sqlite3
from typing import Optional

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def set_low_priority() -> bool:
    """현재 프로세스의 우선순위를 BELOW_NORMAL_PRIORITY_CLASS로 낮추어 포그라운드 작업 보호"""
    try:
        if sys.platform == "win32":
            p = psutil.Process()
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            return True
    except Exception:
        pass
    return False


def get_safe_workers(max_cap: int = 4) -> int:
    """
    안전한 병렬 처리 워커(스레드/프로세스) 수 계산
    - 전체 코어의 최대 1/3, 기본 상한 4개 이하로 제한
    """
    total_cores = os.cpu_count() or 4
    calculated = max(1, total_cores // 3)
    return min(max_cap, calculated)


def is_memory_safe(threshold_percent: float = 80.0) -> bool:
    """현재 시스템 OEM 사용률이 안전 임계치(기본 80%) 이하인지 확인"""
    try:
        mem = psutil.virtual_memory()
        return mem.percent < threshold_percent
    except Exception:
        return True


def wait_for_memory(threshold_percent: float = 80.0, timeout: int = 60) -> bool:
    """메모리 사용률이 임계치 이하로 내려갈 때까지 대기"""
    start = time.time()
    while time.time() - start < timeout:
        if is_memory_safe(threshold_percent):
            return True
        gc.collect()
        time.sleep(3)
    return False


def configure_sqlite_performance(conn: sqlite3.Connection):
    """
    SQLite 연결 시 고성능 및 비차단 동시성을 위한 WAL(Write-Ahead Logging) 설정
    """
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA cache_size = -64000;")  # 64MB 캐시
        conn.execute("PRAGMA busy_timeout = 10000;") # 10초 대기
    except Exception:
        pass


if __name__ == "__main__":
    print("[Resource Guard 상태 점검]")
    print(f"- 논리 CPU 코어 수: {os.cpu_count()}")
    print(f"- 권장 최대 병렬 워커 수: {get_safe_workers()}개")
    mem = psutil.virtual_memory()
    print(f"- 총 OEM: {mem.total / (1024**3):.2f} GB (가용: {mem.available / (1024**3):.2f} GB, 사용률: {mem.percent}%)")
    print(f"- 메모리 안전 상태: {'정상 (Safe)' if is_memory_safe() else '주의 (High Usage)'}")
    if set_low_priority():
        print("- 프로세스 우선순위: Below Normal (배경 작업 최적화 성공)")
