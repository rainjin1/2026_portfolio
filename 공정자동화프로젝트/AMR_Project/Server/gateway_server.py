"""
MES 게이트웨이 서버 v2
gateway_server.py

스레드 구조:
  accept_thread      : TCP 9090 수신 → IP로 장비 식별 → recv 스레드 기동
  device_recv_thread : 장비별 1:1 소켓 수신 → message_queue 투입
  amr_thread         : 서버→AMR ARCL 접속 유지 → message_queue 투입
  [메인 스레드]       : message_queue 소비 → 판단 → send_to()

장비 통신 방향:
  R1, R2, PLC1, PLC2, RASPI, R2_ARD, AMR_ARD → server:9090 접속
  AMR ← server가 ARCL 7171로 접속
  PLC 직접 읽기/쓰기 → 필요 시 메인에서 MC Protocol 호출
"""

import socket
import threading
import queue
import time
from collections import deque
import db

# ══════════════════════════════════════════════════════════════════════
# SECTION 1: 설정
# ══════════════════════════════════════════════════════════════════════

SERVER_HOST = "192.168.3.8"
SERVER_PORT = 9090

AMR_HOST = "192.168.3.11"
AMR_PORT  = 7171

# 접속 IP → 장비명 (accept 시 식별용)
DEVICE_BY_IP = {
    "192.168.3.2":  "R1",
    "192.168.3.3":  "R2",
    "192.168.3.39": "PLC1",
    "192.168.3.40": "PLC2",
    "192.168.3.21": "RASPI",
    "192.168.3.22": "R2_ARD",
    "192.168.3.23": "AMR_ARD",
}

HEARTBEAT_TIMEOUT = 30.0   # 초 — 이 시간 동안 수신 없으면 연결 이상 경고

# ══════════════════════════════════════════════════════════════════════
# SECTION 2: 작업 큐 (서버 메모리)
#
# 서버가 주문 처리 순서와 장비 할당을 직접 관리.
# 상태 변경 시에만 db.py 호출하여 기록.
# ══════════════════════════════════════════════════════════════════════

# 주문 단위 상태 — DB 기록 및 GUI 표시용
ORDER_STATUS = [
    "PENDING",    # 대기
    "INPUT",      # 자재 입력 중
    "STACKING",   # 적재 중
    "ASSEMBLY",   # 조립 중
    "OUTPUT",     # 완성품 출력 중
    "DISPOSED",   # 불량 폐기
    "DONE",       # 완료
]

# 서버 내부 세부 공정 단계 — 작업 큐 및 장비 할당용
# 실행 중 단계: 이벤트 대기 (decide()가 명령 안 줌)
# 대기(AWAITING) 단계: 다음 명령 줄 준비 됨, 자원 확보되면 즉시 명령
WORK_STAGES = [
    # 실행 중
    "PENDING",            # 파이프라인 진입 전 대기
    "SORT_WAITING",       # 자재 부족 → AMR 투입 대기
    "SORTING",            # R1 분류 중
    "STACKING",           # R1 적재 중
    "ASSEMBLY",           # R2 조립 중
    "TRANSFER",           # R2 판별대 이송 중
    "INSPECTION",         # R2_ARD + PLC2 판별 중
    "OUTPUT_TRANSFER",    # R2 출력이송 중
    "AMR_PICKUP",         # AMR 픽업 이동 중
    "AWAITING_RECV",      # AMR 도착, 사용자 수령 대기
    "DISPOSAL",           # R2 폐기 중
    # 대기 (자원 확보되면 즉시 명령)
    "AWAITING_ASSEMBLY",  # StackDone 후 → R2 조립 명령 대기
    "AWAITING_TRANSFER",  # AssemblyDone 후 → 판별대 이송 명령 대기
    "AWAITING_OUTPUT",    # 판별 완료(양품) → 출력이송 명령 대기
    "AWAITING_AMR",       # 출력대기1 준비 → AMR 픽업 명령 대기
    "AWAITING_DISPOSAL",  # 판별 완료(불량) → 폐기 명령 대기
    # 종료
    "DONE",
    "DISPOSED",
]

_wq_pending: deque = deque()      # 파이프라인 진입 전 대기 주문 [order_id, ...]
_wq_order_stage: dict = {}        # order_id → stage (삽입순서 = 오래된 주문 우선)
_completed_orders: list = []      # 완료된 주문 보관 [order_id, ...] (DB에 상세 정보 있음)
_wq_active: dict = {             # device → {"order_id", "process"} | None
    "R1":      None,
    "R2":      None,
    "AMR":     None,
    "AMR_ARD": None,
    "R2_ARD":  None,
    "RASPI":   None,
    "PLC1":    None,
    "PLC2":    None,
}


def wq_enqueue(order_id: int):
    """주문을 대기 큐 끝에 추가."""
    _wq_pending.append(order_id)
    _wq_order_stage[order_id] = "PENDING"
    db.set_order_process(order_id, "PENDING")


def wq_peek() -> int | None:
    """다음 대기 주문 확인 (꺼내지 않음)."""
    return _wq_pending[0] if _wq_pending else None


def wq_pop() -> int | None:
    """다음 대기 주문 꺼내기."""
    return _wq_pending.popleft() if _wq_pending else None


def wq_assign(device: str, order_id: int, process: str):
    """장비에 작업 할당 + DB 기록."""
    _wq_active[device] = {"order_id": order_id, "process": process}
    _wq_order_stage[order_id] = process
    db.set_order_process(order_id, process, device)
    db.log_process(order_id, process, device)


def wq_complete(device: str, result: str = "OK", note: str = None):
    """장비 작업 완료 처리 + DB 결과 기록."""
    work = _wq_active.get(device)
    if not work:
        return
    db.log_process(work["order_id"], work["process"], device, result, note)
    _wq_active[device] = None


def wq_free(device: str) -> bool:
    return _wq_active.get(device) is None


def wq_done(order_id: int):
    """주문 완료 처리 — 활성 큐에서 제거 후 완료 보관 리스트로 이동."""
    _wq_order_stage.pop(order_id, None)
    _completed_orders.append(order_id)
    db.update_order_status(order_id, "완료")
    log("WQ", f"주문 #{order_id} 완료 → 완료 보관")


def wq_snapshot() -> dict:
    """현재 전체 큐 상태 스냅샷 (로그·GUI용)."""
    return {
        "pending":    list(_wq_pending),
        "active":     dict(_wq_active),
        "stages":     dict(_wq_order_stage),
        "completed":  list(_completed_orders),
    }


# ══════════════════════════════════════════════════════════════════════
# SECTION 3: 상태 머신 (SM)
#
# 장비 상태는 명령 ACK 기반으로 서버가 직접 관리.
# 30초 무응답 시 heartbeat timeout으로 감지.
# ══════════════════════════════════════════════════════════════════════

class SM:
    # 장비 연결 여부
    connected: dict = {d: False for d in list(DEVICE_BY_IP.values()) + ["AMR"]}

    # 마지막 수신 시각 (heartbeat timeout 판단용)
    last_seen: dict = {d: 0.0 for d in list(DEVICE_BY_IP.values()) + ["AMR"]}

    # 장비 작업 상태 (명령 송신/ACK 수신으로 서버가 갱신)
    r1_state  = "IDLE"   # IDLE / SORTING / STACKING
    r2_state  = "IDLE"   # IDLE / ASSEMBLY / TRANSFER / INSPECTION / DISPOSAL
    amr_state = "IDLE"   # IDLE / MOVING / ARRIVED

    # 색상 판별 대기 (R1 요청 → Pi 응답 대기 중, 분류는 주문과 무관)
    pending_color: bool = False          # Pi 응답 대기 중 여부
    color_buf: list = []                 # 수신한 색상값 임시 저장 (최대 4개, SortDone 시 DB 반영 후 소각)

    # 판별 진행 상태
    inspection_face   = 0      # 완료된 회전 수 (0~4)
    inspection_failed = False  # X 수신 시 True, 이후 판별 요청 없이 회전만

    # 스테이션 점유 상태 — 명령 줄 때 서버가 직접 업데이트 (추적용)
    # 물리 확인은 명령 직전 PLC에 소켓으로 직접 요청
    station_assembly          = None        # 조립대     (order_id or None)
    station_inspect           = None        # 판별대     (order_id or None)
    station_output: list      = [None, None, None]  # 출력대기 1,2,3 (index 0=1번)


# ══════════════════════════════════════════════════════════════════════
# SECTION 4: 소켓 관리 & 유틸리티
# ══════════════════════════════════════════════════════════════════════

_sockets: dict = {}          # device → socket
_sock_lock = threading.Lock()


def log(tag: str, msg: str):
    print(f"[{time.strftime('%H:%M:%S')}][{tag}] {msg}")


def send_to(device: str, msg: str, terminator: str = "\n"):
    """
    장비에 메시지 송신.
    terminator: 기본 '\\n'. PLC 통신 시 "" 로 지정 (터미네이터 없이 전송).
    """
    with _sock_lock:
        sock = _sockets.get(device)
    if not sock:
        log("SEND", f"[오류] {device} 소켓 없음 — 메시지 폐기: {msg}")
        return
    try:
        sock.sendall((msg + terminator).encode())
        log("SEND", f"→ {device}: {msg}")
    except Exception as e:
        log("SEND", f"[오류] {device} 전송 실패: {e}")
        _disconnect(device)


def _disconnect(device: str):
    """소켓 제거 및 연결 상태 해제."""
    with _sock_lock:
        sock = _sockets.pop(device, None)
    if sock:
        try: sock.close()
        except: pass
    SM.connected[device] = False
    log("CONN", f"{device} 연결 해제")


# ══════════════════════════════════════════════════════════════════════
# SECTION 5: 수신 스레드
# ══════════════════════════════════════════════════════════════════════

message_queue: queue.Queue = queue.Queue()


def device_recv_thread(device: str, sock: socket.socket):
    """
    장비별 수신 전용 스레드.
    줄바꿈(\n) 기준으로 메시지 분리 후 message_queue 투입.
    접속 끊기면 스레드 종료.        plc의 경우 캐리지리턴, 엔터 등 제외
    """
    buf = ""
    while True:
        try:
            data = sock.recv(1024).decode(errors="ignore")
            if not data:
                break
            SM.last_seen[device] = time.time()
            buf += data
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if line:
                    log("RECV", f"← {device}: {line}")
                    message_queue.put((device, line))
        except Exception as e:
            log("RECV", f"[오류] {device}: {e}")
            break
    _disconnect(device)


def amr_thread():
    """
    AMR ARCL 접속 스레드 (서버가 AMR로 접속).
    접속 끊기면 5초 후 재시도.
    """
    while True:
        try:
            log("AMR", f"ARCL 접속 시도 → {AMR_HOST}:{AMR_PORT}")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((AMR_HOST, AMR_PORT))
            with _sock_lock:
                _sockets["AMR"] = sock
            SM.connected["AMR"] = True
            SM.last_seen["AMR"] = time.time()
            log("AMR", "ARCL 접속 완료")

            buf = ""
            while True:
                data = sock.recv(1024).decode(errors="ignore")
                if not data:
                    break
                SM.last_seen["AMR"] = time.time()
                buf += data
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        log("RECV", f"← AMR: {line}")
                        message_queue.put(("AMR", line))
        except Exception as e:
            log("AMR", f"[오류] {e} — 5초 후 재접속")
        finally:
            _disconnect("AMR")
        time.sleep(5)


def accept_thread():
    """
    TCP 서버 수신 대기.
    접속 IP로 장비 식별 후 device_recv_thread 기동.
    미등록 IP는 즉시 거부.
    """
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((SERVER_HOST, SERVER_PORT))
    server_sock.listen(10)
    log("SERVER", f"수신 대기 중 {SERVER_HOST}:{SERVER_PORT}")

    while True:
        try:
            conn, addr = server_sock.accept()
            ip = addr[0]
            device = DEVICE_BY_IP.get(ip)

            if not device:
                log("CONN", f"[거부] 미등록 IP: {ip}")
                conn.close()
                continue

            log("CONN", f"{device} 접속 ({ip})")

            # 기존 소켓 정리 후 교체
            with _sock_lock:
                old = _sockets.get(device)
                if old:
                    try: old.close()
                    except: pass
                _sockets[device] = conn

            SM.connected[device] = True
            SM.last_seen[device] = time.time()

            threading.Thread(
                target=device_recv_thread,
                args=(device, conn),
                daemon=True
            ).start()

        except Exception as e:
            log("ACCEPT", f"[오류] {e}")


# ══════════════════════════════════════════════════════════════════════
# SECTION 6: 명령 결정 (Decision Engine)
#
# 모든 이벤트 핸들러 처리 후 decide() 호출.
# decide() → 오래된 주문부터 최대 3개 순회 → _advance()로 다음 명령 결정.
# AWAITING_* 단계인 주문에만 명령 발행, 실행 중 단계는 이벤트 대기.
# ══════════════════════════════════════════════════════════════════════

MAX_ACTIVE_ORDERS = 3


def decide():
    """
    주문 큐 기반 명령 결정 진입점.
    오래된 주문(먼저 들어온 것)부터 최대 3개 순회.
    """
    active = [
        (oid, stage)
        for oid, stage in _wq_order_stage.items()
        if stage not in ("DONE", "DISPOSED")
    ][:MAX_ACTIVE_ORDERS]

    for order_id, stage in active:
        _advance(order_id, stage)

    # 슬롯 남으면 대기 주문 파이프라인 진입 시도
    if len(active) < MAX_ACTIVE_ORDERS:
        _try_start_next()

    # AMR 명령 결정 (출력픽업 > 자재투입)
    _decide_amr()


def _advance(order_id: int, stage: str):
    """주문 단계별 다음 액션 결정. AWAITING 단계만 처리."""

    if stage == "AWAITING_ASSEMBLY":
        # R2 유휴 + 조립대 비어있으면 → 직접 R2에 조립 명령 (SM 상태 신뢰)
        if SM.r2_state == "IDLE" and SM.station_assembly is None:
            send_to("R2", "Assembly")   # TODO: 네이밍 확정 필요
            SM.r2_state = "ASSEMBLY"
            SM.station_assembly = order_id
            _wq_order_stage[order_id] = "ASSEMBLY"
            wq_assign("R2", order_id, "ASSEMBLY")
            db.set_order_process(order_id, "ASSEMBLY")

    elif stage == "AWAITING_TRANSFER":
        # R2 유휴 + 판별대 비어있으면 → 판별대 이송 명령
        if SM.r2_state == "IDLE" and SM.station_inspect is None:
            send_to("R2", "Transfer")   # TODO: 네이밍 확정 필요
            SM.r2_state = "TRANSFER"
            SM.station_inspect  = order_id
            SM.station_assembly = None
            _wq_order_stage[order_id] = "TRANSFER"
            wq_assign("R2", order_id, "TRANSFER")

    elif stage == "AWAITING_OUTPUT":
        # R2 유휴 + 출력대기 3번 비어있으면 → 출력이송 명령
        if SM.r2_state == "IDLE" and SM.station_output[2] is None:
            send_to("R2", "OutputTransfer")   # TODO: 네이밍 확정 필요
            SM.r2_state        = "OUTPUT"
            SM.station_output[2] = order_id
            SM.station_inspect = None
            _wq_order_stage[order_id] = "OUTPUT_TRANSFER"
            wq_assign("R2", order_id, "OUTPUT_TRANSFER")
            db.set_order_process(order_id, "OUTPUT")

    elif stage == "AWAITING_DISPOSAL":
        # R2 유휴이면 → 폐기 명령
        if SM.r2_state == "IDLE":
            send_to("R2", "Disposal")   # TODO: 네이밍 확정 필요
            SM.r2_state        = "DISPOSAL"
            SM.station_inspect = None
            _wq_order_stage[order_id] = "DISPOSAL"
            wq_assign("R2", order_id, "DISPOSAL")
            db.update_order_status(order_id, "DISPOSED")

    elif stage == "AWAITING_AMR":
        # AMR 유휴이면 → 출력 픽업 이동 명령
        if SM.amr_state == "IDLE":
            send_to("AMR", "GoTo:NeedRecv")   # TODO: 네이밍 확정 필요
            SM.amr_state = "MOVING_OUTPUT"
            _wq_order_stage[order_id] = "AMR_PICKUP"
            wq_assign("AMR", order_id, "AMR_PICKUP")


def _try_start_next():
    """대기 주문 중 파이프라인 진입 가능한 것 시작."""
    order_id = wq_peek()
    if not order_id:
        return

    if db.can_fulfill_order(order_id):
        # 재고 충분 + 이송대 비어있으면 → 직접 적재 명령 (SM 상태 신뢰)
        if SM.station_assembly is None:
            wq_pop()
            _wq_order_stage[order_id] = "STACKING"
            wq_assign("R1", order_id, "STACKING")
            send_to("R1", "Stack")   # TODO: 네이밍 확정 필요
    else:
        # 재고 부족 → AMR 자재 투입 흐름
        wq_pop()
        _wq_order_stage[order_id] = "SORT_WAITING"
        log("WQ", f"주문 #{order_id} 재고 부족 → 자재 투입 필요")
        # AMR NeedInput 명령은 _decide_amr()에서 처리


def _decide_amr():
    """AMR 명령 결정. 출력 픽업 > 자재 투입 순."""
    if SM.amr_state != "IDLE":
        return

    # 1순위: 출력대기 1번에 완성품 대기 중
    if SM.station_output[0] is not None:
        # AWAITING_AMR 단계 주문이 있을 것 — _advance()에서 처리됨
        return

    # 2순위: 자재 투입 필요한 주문 있음
    sort_waiting = [
        oid for oid, stage in _wq_order_stage.items()
        if stage == "SORT_WAITING"
    ]
    if sort_waiting:
        send_to("AMR", "GoTo:NeedInput")   # TODO: 네이밍 확정 필요
        SM.amr_state = "MOVING_INPUT"
        log("AMR", "자재 투입 위치로 이동 명령")


# ══════════════════════════════════════════════════════════════════════
# SECTION 7: 이벤트 핸들러
#
# 메인 스레드에서 순차 실행. 블로킹 금지.
# 서브스레드 필요 시 threading.Thread로 분리.
# 메시지 네이밍은 RAPID 담당자와 협의 후 확정.
# ══════════════════════════════════════════════════════════════════════

def handle_r1(msg: str):
    """
    R1 수신 메시지 처리.
    # TODO: 네이밍 확정 필요

    [RAPID 담당자] 예상 메시지 형태 (확정 전):
      "IDLE"           → 유휴 상태
      "SortDone:색상"  → 분류 완료 (색상: W/Y/B/D/N/R)
      "StackDone"      → 적재 완료
      "ColorRequest"   → Pi 색상 판별 요청
    """
    pass


def handle_r2(msg: str):
    """
    R2 수신 메시지 처리.
    # TODO: 네이밍 확정 필요

    [RAPID 담당자] 예상 메시지 형태 (확정 전):
      "IDLE"           → 유휴 상태
      "AssemblyDone"   → 조립 완료
      "ReadyRetract"   → 이송대 패널 수령 완료, 후진 가능
      "TransferDone"   → 판별대 이송 완료
      "DisposalDone"   → 폐기 완료
    """
    pass


def handle_plc1(msg: str):
    """
    PLC1 소켓 푸시 메시지 처리.
    PLC 래더에서 조건 성립 시 직접 서버로 전송.
    # TODO: 네이밍 확정 필요

    예상 메시지 형태 (래더 담당자와 협의 후 확정):
      "PlatformClear"     → 이송실린더 후진 완료 + 공작물 없음
      "MaterialDetected"  → 이송대 공작물 감지
    """
    pass


def handle_plc2(msg: str):
    """
    PLC2 소켓 푸시 메시지 처리.
    # TODO: 네이밍 확정 필요

    예상 메시지 형태 (래더 담당자와 협의 후 확정):
      "AssemblyReady"  → 조립대 준비 완료
      "RotateDone"     → 90도 회전 완료
      "InspectReady"   → 판별대 물품 도착 확인
      "OutputDone"     → 출력 컨베이어 이송 완료
    """
    pass


def handle_raspi(msg: str):
    """
    RaspberryPi 색상 판별 결과 처리.
    # TODO: 네이밍 확정 필요

    예상 메시지 형태 (확정 전):
      "W" / "Y" / "B" / "D" / "N" / "R"  → 색상값
    """
    pass


def handle_r2_ard(msg: str):
    """
    R2_ARD 양불 판별 결과 처리.
    # TODO: 네이밍 확정 필요

    예상 메시지 형태 (확정 전):
      "OK" / "NG"  → 양품 / 불량
    """
    pass


def handle_amr(msg: str):
    """
    AMR ARCL 수신 처리.
    ARCL 프로토콜 파싱 후 상태 갱신.
    # TODO: 네이밍 확정 필요

    주요 ARCL 상태:
      Arrived R01Insert  → R1 투입구 도착
      Arrived R02Output  → R2 출력구 도착
      Arrived NeedRecv   → 완성품 수령 위치 도착
    """
    pass


def handle_amr_ard(msg: str):
    """
    AMR Arduino 수신 처리.
    AMR 컨베이어 / 차단기 상태 수신.
    # TODO: 네이밍 확정 필요
    """
    pass


HANDLERS = {
    "R1":      handle_r1,
    "R2":      handle_r2,
    "PLC1":    handle_plc1,
    "PLC2":    handle_plc2,
    "RASPI":   handle_raspi,
    "R2_ARD":  handle_r2_ard,
    "AMR":     handle_amr,
    "AMR_ARD": handle_amr_ard,
}


# ══════════════════════════════════════════════════════════════════════
# SECTION 8: 메인 루프
# ══════════════════════════════════════════════════════════════════════

def _check_heartbeat_timeouts():
    """30초 이상 수신 없는 장비 경고."""
    now = time.time()
    for device, last in SM.last_seen.items():
        if SM.connected.get(device) and last > 0:
            if (now - last) > HEARTBEAT_TIMEOUT:
                log("HB", f"[경고] {device} {HEARTBEAT_TIMEOUT}s 동안 응답 없음")


def main_loop():
    last_hb_check = time.time()
    while True:
        try:
            device, msg = message_queue.get(timeout=1.0)
            handler = HANDLERS.get(device)
            if handler:
                handler(msg)
            else:
                log("MAIN", f"[미등록 핸들러] {device}: {msg}")
        except queue.Empty:
            pass

        if time.time() - last_hb_check >= 10.0:
            _check_heartbeat_timeouts()
            last_hb_check = time.time()


# ══════════════════════════════════════════════════════════════════════
# SECTION 9: 진입점
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    db.init_db()

    threading.Thread(target=accept_thread, daemon=True).start()
    threading.Thread(target=amr_thread,    daemon=True).start()

    log("SERVER", "게이트웨이 서버 시작")
    main_loop()
