"""
MES 게이트웨이 서버 v2
gateway_server_20260730.py

변경 이력:
  20260730 — Stack 명령 색상 포함, SM.r1_state 누락 수정,
             AMR 상태 위치 기반으로 전환, NeedInput 자재입력 플로우 추가

스레드 구조:
  accept_thread      : TCP 9090 수신 → IP로 장비 식별 → recv 스레드 기동
  device_recv_thread : 장비별 1:1 소켓 수신 → message_queue 투입
  amr_thread         : 서버→AMR ARCL 접속 유지 → message_queue 투입
  input_thread       : 콘솔 사용자 입력 전용 (블로킹 격리)
  [메인 스레드]       : message_queue 소비 → 판단 → send_to()

장비 통신 방향:
  R1, R2, PLC1, PLC2, RASPI, R2_ARD, AMR_ARD → server:9090 접속
  AMR ← server가 ARCL 7171로 접속
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

# PLC 고정 프레임 통신 설정 (CR/LF 없음, ASCII)
# 프레임 구조: 타입(2) + 명령코드(2) + 페이로드(16) = 20바이트
PLC_DEVICES      = {"PLC1", "PLC2"}
PLC_FRAME_SIZE   = 20
PLC_HEADER_SIZE  = 4
PLC_PAYLOAD_SIZE = 16   # PLC_FRAME_SIZE - PLC_HEADER_SIZE

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

inspection_turn_count = 0


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
# 장비 상태는 명령 발행/완료 신호 기반으로 서버가 직접 관리.
# 30초 무응답 시 heartbeat timeout으로 감지.
# ══════════════════════════════════════════════════════════════════════

class SM:
    # 장비 연결 여부
    connected: dict = {d: False for d in list(DEVICE_BY_IP.values()) + ["AMR"]}

    # 마지막 수신 시각 (heartbeat timeout 판단용)
    last_seen: dict = {d: 0.0 for d in list(DEVICE_BY_IP.values()) + ["AMR"]}

    # 장비 작업 상태 (명령 송신/완료 신호 수신으로 서버가 갱신)
    r1_state  = "IDLE"   # IDLE / SORTING / STACKING
    r2_state  = "IDLE"   # IDLE / ASSEMBLY / TRANSFER / INSPECTION / DISPOSAL

    # AMR 위치 기반 상태
    # IDLE              : 대기 (도킹 or 정지)
    # GOING_TO_NEEDINPUT: NeedInput 이동 중
    # AT_NEEDINPUT      : NeedInput 도착, 자재 입력 대기
    # GOING_TO_R1INPUT  : R1Input 이동 중
    # AT_R1INPUT        : R1Input 도착, 분류 중
    # GOING_TO_NEEDRECV : NeedRecv (완성품 수령 위치) 이동 중
    # AT_NEEDRECV       : NeedRecv 도착, 사용자 수령 대기
    amr_state = "IDLE"

    # 색상 판별 대기 (R1 요청 → Pi 응답 대기 중, 분류는 주문과 무관)
    pending_color: bool = False          # Pi 응답 대기 중 여부
    color_buf: list = []                 # 수신한 색상값 임시 저장 (SortDone 시 DB 반영 후 소각)

    # 판별 진행 상태
    inspection_face   = 0      # 완료된 회전 수 (0~4)
    inspection_failed = False  # X 수신 시 True, 이후 판별 요청 없이 회전만

    # 스테이션 점유 상태 — 명령 발행 시 서버가 직접 업데이트 (SM 상태 신뢰)
    station_assembly          = None              # 조립대     (order_id or None)
    station_inspect           = None              # 판별대     (order_id or None)
    station_output: list      = [None, None, None]  # 출력대기 1,2,3 (index 0=1번)
    station_input             = 0 #입력명령 수량
    station_input_count        = 0 #입력수량 카운터

    #분류명령가능여부
    Sort_Available          = False #처음에는 분류 불가. 인풋이 없기때문.

    #적재명령가능여부
    Stack_Available         = True

    #이송대로 이송명령 가능여부
    Transfer_to_Trasfer     = True


# ══════════════════════════════════════════════════════════════════════
# SECTION 4: 소켓 관리 & 유틸리티
# ══════════════════════════════════════════════════════════════════════

_sockets: dict = {}          # device → socket
_sock_lock = threading.Lock()


def log(tag: str, msg: str):
    print(f"[{time.strftime('%H:%M:%S')}][{tag}] {msg}")


def send_to(device: str, msg: str, terminator: str = "\n"):
    """
    R1/R2/RASPI/R2_ARD/AMR_ARD/AMR 메시지 송신 (줄바꿈 기반).
    PLC1/PLC2는 plc_send_to() 사용.
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


def plc_send_to(device: str, msg_type: str, cmd_code: str, payload: str = ""):
    """
    PLC 전용 송신. 고정 프레임 20바이트, CR/LF 없음.
    msg_type : 2바이트 ASCII (타입)
    cmd_code : 2바이트 ASCII (명령코드)
    payload  : 최대 16바이트, 부족분 공백 패딩
    """
    header = f"{msg_type[:2]:<2}{cmd_code[:2]:<2}"
    body   = f"{payload[:PLC_PAYLOAD_SIZE]:<{PLC_PAYLOAD_SIZE}}"
    frame  = (header + body).encode("ascii")

    with _sock_lock:
        sock = _sockets.get(device)
    if not sock:
        log("SEND", f"[오류] {device} 소켓 없음 — 프레임 폐기")
        return
    try:
        sock.sendall(frame)
        log("SEND", f"→ {device}: type={msg_type!r} cmd={cmd_code!r} payload={payload!r}")
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
console_queue: queue.Queue = queue.Queue()   # ("INPUT", None) | ("OUTPUT", order_id)


def console_thread():
    """
    콘솔 인터랙션 전담 스레드 — 블로킹 격리.
    console_queue를 소비하며 사용자와 순차 상호작용.

    작업 종류:
      ("INPUT",  None)     : 자재 투입 수량 입력 → AMR_ARD CheckCount
      ("OUTPUT", order_id) : 완성품 수령 확인 → wq_done()
    """
    while True:
        task_type, payload = console_queue.get()

        if task_type == "INPUT":
            print("\n" + "="*50)
            print("[자재입력요청] AMR 컨베이어에 분류할 자재 세트를 올려주세요.")
            print("="*50)
            try:
                n = int(input("  몇 세트 입력하셨나요? > ").strip())
            except (ValueError, EOFError):
                log("CONSOLE", "[오류] 잘못된 입력 — 자재 입력 취소")
                continue
            send_to("AMR_ARD", f"CheckCount:{n}")   # TODO: 네이밍 확정 필요
            log("CONSOLE", f"자재 {n}세트 입력 완료 → AMR_ARD 수량 확인 요청")

        elif task_type == "OUTPUT":
            order_id = payload
            print("\n" + "="*50)
            print(f"[완성품수령요청] 주문 #{order_id} 완성 모듈을 수령해주세요.")
            print("="*50)
            input("  수령 완료 후 Enter를 눌러주세요...")
            SM.amr_state = "IDLE"
            SM.station_output[0] = None
            wq_done(order_id)
            log("CONSOLE", f"주문 #{order_id} 수령 완료 확인")
            decide()


def plc_recv_thread(device: str, sock: socket.socket):
    """
    PLC 전용 수신 스레드. 고정 프레임 20바이트 단위로 수신.
    헤더 4바이트(타입2 + 명령코드2) + 페이로드 16바이트, ASCII, CR/LF 없음.
    수신한 20바이트를 그대로 message_queue에 투입.
    handle_plc1/2에서 msg[:4](헤더), msg[4:].strip()(페이로드)로 파싱.
    """
    while True:
        try:
            data = b""
            while len(data) < PLC_FRAME_SIZE:
                chunk = sock.recv(PLC_FRAME_SIZE - len(data))
                if not chunk:
                    raise ConnectionError("연결 종료")
                data += chunk
            SM.last_seen[device] = time.time()
            frame = data.decode("ascii", errors="ignore")
            log("RECV", f"← {device}: {frame!r}")
            message_queue.put((device, frame))
        except Exception as e:
            log("RECV", f"[오류] {device}: {e}")
            break
    _disconnect(device)


def device_recv_thread(device: str, sock: socket.socket):
    """
    R1/R2/RASPI/R2_ARD/AMR_ARD 수신 전용 스레드.
    줄바꿈(\\n) 기준으로 메시지 분리 후 message_queue 투입.
    PLC1/PLC2는 plc_recv_thread() 사용.
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

            target = plc_recv_thread if device in PLC_DEVICES else device_recv_thread
            threading.Thread(
                target=target,
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
        # R2 유휴 + 조립대 비어있으면 → R2에 조립 명령 (SM 상태 신뢰)
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
            SM.r2_state          = "OUTPUT"
            SM.station_output[2] = order_id
            SM.station_inspect   = None
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
        # AMR 유휴이면 → 완성품 수령 위치로 이동 명령
        if SM.amr_state == "IDLE":
            send_to("AMR", "GoTo:NeedRecv")   # TODO: 네이밍 확정 필요
            SM.amr_state = "GOING_TO_NEEDRECV"
            _wq_order_stage[order_id] = "AMR_PICKUP"
            wq_assign("AMR", order_id, "AMR_PICKUP")


def _try_start_next():
    """대기 주문 중 파이프라인 진입 가능한 것 시작."""
    order_id = wq_peek()
    if not order_id:
        return

    if db.can_fulfill_order(order_id):
        # 재고 충분 + 조립대 비어있으면 → 직접 적재 명령 (SM 상태 신뢰)
        if SM.station_assembly is None:
            order = db.get_order(order_id)
            colors = (
                order["base_color"]  +
                order["wall1_color"] +
                order["wall2_color"] +
                order["wall3_color"] +
                order["wall4_color"] +
                order["ceil_color"]
            )
            wq_pop()
            _wq_order_stage[order_id] = "STACKING"
            wq_assign("R1", order_id, "STACKING")
            SM.r1_state = "STACKING"
            send_to("R1", f"Stack:{colors}")   # TODO: 네이밍 확정 필요
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

    # 1순위: 출력대기 1번에 완성품 대기 중 → AWAITING_AMR 주문 있을 것 (_advance에서 처리)
    if SM.station_output[0] is not None:
        return

    # 2순위: 자재 투입 필요한 주문 있음
    sort_waiting = [
        oid for oid, stage in _wq_order_stage.items()
        if stage == "SORT_WAITING"
    ]
    if sort_waiting:
        send_to("AMR", "GoTo:NeedInput")   # TODO: 네이밍 확정 필요
        SM.amr_state = "GOING_TO_NEEDINPUT"
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

    """
    if "ColorRequest" :
        R1 → "ColorRequest" → 서버 → Raspi에 판별 요청 send_to("RASPI", "ColorRequest")
        Raspi → 색상값 → handle_raspi() → 수신 후 라즈파이 수신부에서(예 "W") 재고 +1, R1에 색상값 전달send_to("R1", "W"등)
    elif "SortDone" :
        (세트로 오기 때문에 베이스1 벽4 천장1 이 한세트)베이스재고 +1, 천장재고 +1(벽들은 ColorRequest시에 바로 자재 수량 올려줘서 ㄱㅊ),
        r1상태 레디, 결정로직 decide() 호출
    elif "StackDone" :
        r1상태 레디, wq_order_id에 해당하는 상태 조립대기 로 바꿔주고, 그냥wq도 적재쪽 끝으로 바꿔주고,
        결정로직 decide() 호출
    
    
    
    """
    pass


def handle_r2(msg: str):
    """
    R2 수신 메시지 처리.
    # TODO: 네이밍 확정 필요

    [RAPID 담당자] 예상 메시지 형태 (확정 전):
      "IDLE"           → 유휴 상태
      "AssemblyDone"   → 조립 완료
      "ReadyRetract"   → 이송대 패널 수령 완료, 후진 가능 * 필요없게 라피드 및 래더 짤 예정. 이 신호 안받을 것.
      "TransferDone"   → 판별대 이송 완료
      "DisposalDone"   → 폐기 완료
    
    if "AssemblyDone" :
        R2 상태레디, wq_order, wq 업데이트, decide()호출
    if "TransferToInspectionDone" :
        R2 상태 레디, wq 업데이트, wq 업데이트, decide()호출 (판별시작은 R2와 별개로 서버와 plc2만 소통하며 함)
    if DisposalDone :
        R2 상태 레디, station_inspect업데이트, 해당 주문에 대한 처리 어떻게 할지 고민중. 일단 그냥 폐기로, decide()호출,wq_done호출. 폐기도 일단 완료로 기록하고 DB에 폐기로 기록.
    if "TransferToOuputConvDone" :
        R2 상태 레디, wq 업데이트(배송대기 등)
        (R2는 3번에 매번 놓고, 3번 들어오면 plc2가 알아서 1번 비어있으면 1번자리로, 차있고 2번 비어있으면 2번으로, 3번만 남아있으면 그대로 유지함.)
        decide()호출
    
    
    """
    pass

def handle_plc1(msg: str):
    """
    PLC1 소켓 푸시 메시지 처리.
    PLC 래더에서 조건 성립 시 직접 서버로 전송.
    """
    # TODO: 명령코드 확정 필요
    msg_type = msg[:2]
    cmd_code = msg[2:4]
    payload  = msg[4:].strip()

    if msg_type != "P1":
        log("PLC1", f"[무시] 잘못된 타입: {msg_type!r}")
        return

    if cmd_code == "99":   #생존확인
        pass
    if cmd_code == "02":    #받음
        SM.station_input_count +=1
        if SM.station_input_count <SM.station_input:
            send_to("AMR_ARD", "Input")
        elif SM.station_input_count == SM.station_input:
            pass
    if cmd_code == "03":    #InputDone. AMR에 달린 컨베이어는 아두이노가 알아서 정지할것
        send_to("AMR_ARD", "InputDone")
        #AMR상태 idle로 바꾸기 혹은 대기장소로 이동명령
    if cmd_code == "04":    #분류가능신호
        SM.Sort_Available = True
    if cmd_code == "05":    #적재가능신호(적재실린더 후진, 물품유무센서 무)
        SM.Stack_Available = True

def handle_plc2(msg: str):
    """
    PLC1 소켓 푸시 메시지 처리.
    PLC 래더에서 조건 성립 시 직접 서버로 전송.
    """
       # TODO: 명령코드 확정 필요
    msg_type = msg[:2]
    cmd_code = msg[2:4]
    payload  = msg[4:].strip()

    if msg_type != "P2":
        log("PLC2", f"[무시] 잘못된 타입: {msg_type!r}")
        return

    if cmd_code == "99":    #생존확인
        pass
    if cmd_code =="01":     #판별대돌림
        if inspection_turn_count < 4:
            send_to("R2_ARD", "Check")
        #O X 중 하나로 받고, O면 다시 돌려(총 4회), X면 총 4회될때까지 바로바로 돌려 명령 보내고, 폐기 판정. 여기 좀 헷갈리거나 꼬일 수 있으니 송신부 처리할때 다시 고민하기


def handle_raspi(msg: str):
    """
    RaspberryPi 색상 판별 결과 처리.
    # TODO: 네이밍 확정 필요

    예상 메시지 형태 (확정 전):
      "W" / "Y" / "B" / "D" / "N" / "R"  → 색상값    
    """
    color = msg.strip()
    if color in db.WPC_MAP:
        db.adjust_stock(db.WPC_MAP[color], 1)
        send_to("R1", color)
    else:
        log("RASPI", f"[무시] 알 수 없는 색상값: {color!r}")
    

def handle_r2_ard(msg: str):
    """
    R2_ARD 양불 판별 결과 처리.
    # TODO: 네이밍 확정 필요

    예상 메시지 형태 (확정 전):
      "OK" / "NG"  → 양품 / 불량
    """
    if msg == "O":
        plc_send_to("P2", "P2", "02", "")   #회전명령
        inspection_turn_count +=1
        if inspection_turn_count = 4:
            #inspection done
            inspection_turn_count = 0
    if msg =="X":
        for i from 0 to (4-inspection_turn_count):
            #회전명령보내기
            #회전명령완료 기다리기



def handle_amr(msg: str):
    """
    AMR ARCL 수신 처리.
    ARCL 프로토콜 파싱 후 상태 갱신.
    # TODO: 네이밍 확정 필요

    주요 ARCL 상태:
      Arrived NeedInput  → 자재 투입 위치 도착
      Arrived R1Input    → R1 투입구 도착 (분류 시작)
      Arrived NeedRecv   → 완성품 수령 위치 도착
    """
    # TODO: ARCL 메시지 파싱 후 아래 분기 처리
    if "NeedInput" in msg:    # TODO: 정확한 ARCL 도착 메시지 형식 확정 필요
        SM.amr_state = "AT_NEEDINPUT"
        log("AMR", "NeedInput 도착 — 자재 입력 요청")
        console_queue.put(("INPUT", None))

    elif "R1Input" in msg:    # TODO: 네이밍 확정 필요
        SM.amr_state = "AT_R1INPUT"
        log("AMR", "R1Input 도착")
        decide()

    elif "NeedRecv" in msg:   # TODO: 네이밍 확정 필요
        SM.amr_state = "AT_NEEDRECV"
        order_id = next(
            (oid for oid, s in _wq_order_stage.items() if s == "AMR_PICKUP"),
            None
        )
        log("AMR", f"NeedRecv 도착 — 주문 #{order_id} 수령 대기")
        console_queue.put(("OUTPUT", order_id))


def handle_amr_ard(msg: str):
    """
    AMR Arduino 수신 처리.
    AMR 컨베이어 / 차단기 상태, 수량 확인 결과 수신.
    # TODO: 네이밍 확정 필요

    예상 메시지 형태 (확정 전):
      "CountOK"   → 자재 수량 확인 완료 → AMR에 R1Input 이동 명령
      "CountNG"   → 수량 불일치 → 재확인 요청
    """
    # TODO: 정확한 메시지 네이밍 확정 필요
    if msg == "CountOK":
        send_to("AMR", "GoTo:R1Input")   # TODO: 네이밍 확정 필요
        SM.amr_state = "GOING_TO_R1INPUT"
        log("AMR_ARD", "수량 확인 완료 → AMR R1Input으로 이동 명령")

    elif msg == "CountNG":
        log("AMR_ARD", "[경고] 수량 불일치 — 재확인 필요")
        console_queue.put(("INPUT", None))


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

    threading.Thread(target=accept_thread,  daemon=True).start()
    threading.Thread(target=amr_thread,     daemon=True).start()
    threading.Thread(target=console_thread, daemon=True).start()

    log("SERVER", "게이트웨이 서버 시작")
    main_loop()
