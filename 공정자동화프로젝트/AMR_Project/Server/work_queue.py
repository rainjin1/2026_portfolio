"""
MES 게이트웨이 서버 - 서버 메모리 작업 큐
work_queue.py

역할:
  - 주문 처리 순서를 서버가 직접 관리
  - 어떤 장비에 어떤 작업을 줄지 결정
  - 상태 변경 시에만 DB 기록 (db.py 호출)
  - 큐/할당 로직 자체는 메모리에서만 처리
"""

from collections import deque
import db

# ══════════════════════════════════════════════════════════════
# 공정 단계 정의
# ══════════════════════════════════════════════════════════════

STAGES = [
    "SORT_INPUT",   # ① 자재 이송/입력   (AMR, AMR_ARD, P1)
    "SORTING",      # ② 분류             (R1, RASPI)
    "STACKING",     # ③ 적재             (R1, P1)
    "ASSEMBLY",     # ④ 조립             (R2, P2)
    "TRANSFER",     # ⑤-1 판별대 이송    (R2, P2)
    "INSPECTION",   # ⑤-2 판별           (R2_ARD, P2)
    "OUTPUT",       # ⑥-양품 출력        (R1, P2)
    "DISPOSAL",     # ⑥-불량 폐기        (R2, P2)
    "DONE",
]

# 공정별 관여 장비 (참고용)
STAGE_DEVICES = {
    "SORT_INPUT":  ["AMR", "AMR_ARD", "P1"],
    "SORTING":     ["R1", "RASPI"],
    "STACKING":    ["R1", "P1"],
    "ASSEMBLY":    ["R2", "P2"],
    "TRANSFER":    ["R2", "P2"],
    "INSPECTION":  ["R2_ARD", "P2"],
    "OUTPUT":      ["R1", "P2"],
    "DISPOSAL":    ["R2", "P2"],
}

# ══════════════════════════════════════════════════════════════
# 서버 메모리 상태
# ══════════════════════════════════════════════════════════════

# 대기 주문 큐 (order_id)
_pending: deque = deque()

# 장비별 현재 작업: device → {"order_id": int, "process": str} | None
_active: dict = {
    "R1":      None,
    "R2":      None,
    "AMR":     None,
    "AMR_ARD": None,
    "R2_ARD":  None,
    "RASPI":   None,
    "P1":      None,
    "P2":      None,
}

# 주문별 현재 공정 캐시: order_id → process
_order_stage: dict = {}

# ══════════════════════════════════════════════════════════════
# 큐 관리
# ══════════════════════════════════════════════════════════════

def enqueue(order_id: int):
    """주문을 대기 큐 맨 뒤에 추가."""
    _pending.append(order_id)
    _order_stage[order_id] = "PENDING"
    db.set_order_process(order_id, "PENDING")


def peek_next() -> int | None:
    """다음 대기 주문 확인 (꺼내지 않음)."""
    return _pending[0] if _pending else None


def pop_next() -> int | None:
    """다음 대기 주문 꺼내기."""
    return _pending.popleft() if _pending else None


def pending_count() -> int:
    return len(_pending)


# ══════════════════════════════════════════════════════════════
# 작업 할당 / 완료
# ══════════════════════════════════════════════════════════════

def assign(device: str, order_id: int, process: str):
    """
    장비에 작업 할당.
    메모리 갱신 + DB 공정 기록.
    """
    _active[device] = {"order_id": order_id, "process": process}
    _order_stage[order_id] = process
    db.set_order_process(order_id, process, device)
    db.log_process(order_id, process, device)


def complete(device: str, result: str = "OK", note: str = None):
    """
    장비 작업 완료.
    result: 'OK' | 'NG'
    DB에 결과 기록 후 해당 장비 슬롯 비움.
    """
    work = _active.get(device)
    if not work:
        return
    db.log_process(work["order_id"], work["process"], device, result, note)
    _active[device] = None


def fail(device: str, note: str = None):
    """작업 실패 처리 (result='NG' 기록)."""
    complete(device, result="NG", note=note)


# ══════════════════════════════════════════════════════════════
# 조회
# ══════════════════════════════════════════════════════════════

def get_stage(order_id: int) -> str | None:
    """주문 현재 공정."""
    return _order_stage.get(order_id)


def get_device_work(device: str) -> dict | None:
    """장비 현재 작업 {"order_id", "process"} 또는 None."""
    return _active.get(device)


def is_device_free(device: str) -> bool:
    return _active.get(device) is None


def get_snapshot() -> dict:
    """현재 전체 상태 스냅샷 (로그·GUI용)."""
    return {
        "pending":       list(_pending),
        "active":        {d: v for d, v in _active.items()},
        "order_stages":  dict(_order_stage),
    }
