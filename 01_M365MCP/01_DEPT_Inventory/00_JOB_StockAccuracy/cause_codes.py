"""
Standard Cycle Count Root Cause Code Registry (표준 실사 오차 원인 코드 마스터)
=============================================================================
01_DEPT_Inventory / 00_JOB_StockAccuracy / cause_codes.py

Standardized root cause codes for warehouse inventory discrepancies at Plant P001.
Ensures uniform taxonomy across paper return sheets, tablet entries, and WMS adjustments.
"""

from typing import Dict, Any, Optional, List

CAUSE_CODES: Dict[str, Dict[str, Any]] = {
    "CC01_GHOST_MISSING_GI": {
        "code": "CC01_GHOST_MISSING_GI",
        "name_ko": "생산 무스캔 소모 유령 재고 (Missing GI 누락)",
        "name_en": "Production Line Unscanned Consumption (Ghost Stock)",
        "category": "MISSING_GI",
        "is_discrepancy": True,
        "action_guide": "생산라인 바코드 스캔 누락 여부 확인 및 WMS 즉시 불출 처리",
        "sap_movement_suggestion": "MB1B 311 or Line Side GI"
    },
    "CC02_WRONG_BIN_PUTAWAY": {
        "code": "CC02_WRONG_BIN_PUTAWAY",
        "name_ko": "오입고 및 타 빈 물리적 오적재 (Wrong Bin)",
        "name_en": "Wrong Bin Misplacement / Physical Cross-Storage",
        "category": "PUTAWAY_ERROR",
        "is_discrepancy": True,
        "action_guide": "인접 랙 및 동일 통로 탐색 후 정상 로케이션 이동 전기",
        "sap_movement_suggestion": "WMS Bin Transfer"
    },
    "CC03_FIFO_BURIAL": {
        "code": "CC03_FIFO_BURIAL",
        "name_ko": "후열 매몰 및 선입선출 미준수로 인한 장기 방치",
        "name_en": "FIFO Inversion & Physical Burial Trap",
        "category": "FIFO_VIOLATION",
        "is_discrepancy": True,
        "action_guide": "구형 로트 전열 재배치 및 차기 생산라인 우선 불출 지정",
        "sap_movement_suggestion": "Physical Re-slotting"
    },
    "CC04_PARTIAL_PICK_UNSCANNED": {
        "code": "CC04_PARTIAL_PICK_UNSCANNED",
        "name_ko": "1·2층 소물류 낱개/봉지 수기 피킹 후 미스캔",
        "name_en": "Partial Pack Manual Pick Unscanned",
        "category": "HAND_PICK_ERROR",
        "is_discrepancy": True,
        "action_guide": "1·2층 소물 랙 단품 실측 및 부분 수량 WMS 차감 조정",
        "sap_movement_suggestion": "Inventory Count Write-off"
    },
    "CC05_DAMAGED_SCRAP_UNREPORTED": {
        "code": "CC05_DAMAGED_SCRAP_UNREPORTED",
        "name_ko": "파손 및 불량 폐기 미보고 전산 잔존",
        "name_en": "Unreported Scrap / Damage Physical Residual",
        "category": "SCRAP_UNREPORTED",
        "is_discrepancy": True,
        "action_guide": "품질팀 검사 후 폐기 전표(SAP MB1A 551) 정식 발행",
        "sap_movement_suggestion": "MB1A 551 Scrap"
    },
    "CC06_UNIT_PACK_OFFSET": {
        "code": "CC06_UNIT_PACK_OFFSET",
        "name_ko": "포장 단위(Pack Size) 차이로 인한 포장 편차",
        "name_en": "Unit Pack Size Offset Discrepancy",
        "category": "PACK_OFFSET",
        "is_discrepancy": True,
        "action_guide": "박스당 수량 온톨로지 보정 및 WMS 단위 패킹 갱신",
        "sap_movement_suggestion": "Master Data Alignment"
    },
    "CC07_IN_FLIGHT_MOVEMENT": {
        "code": "CC07_IN_FLIGHT_MOVEMENT",
        "name_ko": "실사 중 입출고/생산 투입 이동 시차",
        "name_en": "In-Flight Movement Timing Offset",
        "category": "TIMING_OFFSET",
        "is_discrepancy": False,  # Reconciled to zero true error
        "action_guide": "실사 중 입출고 전표 자동 역산 보정 (현장 오차 미계상 처리)",
        "sap_movement_suggestion": "No SAP Action Required"
    },
    "CC08_MATCH_VERIFIED": {
        "code": "CC08_MATCH_VERIFIED",
        "name_ko": "실물 일치 (오차 없음, 정상 재고)",
        "name_en": "Physical Match Confirmed (Zero Error)",
        "category": "NORMAL_MATCH",
        "is_discrepancy": False,
        "action_guide": "실사 확인 완료 (수량 변동 없음)",
        "sap_movement_suggestion": "Confirmed"
    },
    "CC09_DATA_ENTRY_ERROR": {
        "code": "CC09_DATA_ENTRY_ERROR",
        "name_ko": "바코드/라벨 오부착 또는 전산 등록 오류",
        "name_en": "Barcode / Labeling Data Entry Error",
        "category": "ADMIN_ERROR",
        "is_discrepancy": True,
        "action_guide": "라벨 재발행 및 WMS 마스터 정보 정정",
        "sap_movement_suggestion": "WMS Re-labeling"
    },
    "CC99_OTHER_UNIDENTIFIED": {
        "code": "CC99_OTHER_UNIDENTIFIED",
        "name_ko": "기타 원인 불명 불일치",
        "name_en": "Other Unidentified Discrepancy",
        "category": "UNKNOWN",
        "is_discrepancy": True,
        "action_guide": "재실사(Recount) 요청 및 관리자 정밀 현장 조사",
        "sap_movement_suggestion": "Investigate"
    }
}


def get_cause_code_info(code_str: str, allow_fallback: bool = True) -> Dict[str, Any]:
    """Retrieves cause code metadata, falling back to CC99 if unrecognized and allow_fallback=True."""
    clean_code = str(code_str).strip().upper()
    if clean_code in CAUSE_CODES:
        return CAUSE_CODES[clean_code]
    
    # Prefix or partial match lookup
    for k, v in CAUSE_CODES.items():
        if k.startswith(clean_code) or clean_code in k:
            return v

    if not allow_fallback:
        raise ValueError(f"Unrecognized cause code: '{code_str}'. Valid codes: {list_valid_cause_codes()}")

    return {
        "code": "CC99_OTHER_UNIDENTIFIED",
        "name_ko": f"기타 미등록 원인 ({code_str})",
        "name_en": f"Other Unregistered Cause ({code_str})",
        "category": "UNKNOWN",
        "is_discrepancy": True,
        "action_guide": "정밀 조사 필요",
        "sap_movement_suggestion": "Investigate"
    }


def list_valid_cause_codes() -> List[str]:
    """Returns sorted list of all standard cause code strings."""
    return sorted(list(CAUSE_CODES.keys()))
