"""
MES Command Center v1
command_center_20260730.py

변경 이력:
  20260730 — gateway_server에서 리네임. Stack 명령 색상 포함, SM.r1_state 누락 수정,
             AMR 상태 위치 기반으로 전환, NeedInput 자재입력 플로우 추가,
             PLC 고정헤더 소켓통신 적용, inspection_turn_count → SM.inspection_face 통일,
             handle_r2_ard 문법 오류 수정

스레드 구조:
  accept_thread      : TCP 9090 수신 → IP로 장비 식별 → recv 스레드 기동
  device_recv_thread : 장비별 1:1 소켓 수신 → message_queue 투입
  amr_thread         : 서버→AMR ARCL 접속 유지 → message_queue 투입
  console_thread     : 콘솔 사용자 입력 전용 (블로킹 격리)
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

AMR_HOST     = "192.168.3.11"
AMR_PORT     = 7171
AMR_PASSWORD = "1234"   # ARCL 접속 패스워드 — 실제값으로 변경 필요

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

HEARTBEAT_TIMEOUT = 180.0   # 초 — 전체 장비 하트비트 / 연결 이상 경고 (3분)

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
    #"DUMP_TRANSFER" 추가요망
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
    # IDLE                : 대기 (도킹 or 정지)
    # GOING_TO_NEEDINPUT : 자재요청 이동 중
    # AT_NEEDINPUT       : NeedInput 도착, 자재 입력 대기
    # GOING_TO_R1INPUT   : 1호기 이동 중
    # AT_R1INPUT         : R1Input 도착, 투입 중
    # GOING_TO_박대기    : 투입 완료 후 대기장소 이동 중
    # AT_박대기          : 대기장소 도착 (IDLE 전환 대기)
    # GOING_TO_R2TRANSFER: 2호기 이동 중
    # AT_R2TRANSFER      : R2 출력 컨베이어 도착, 이송 중
    # GOING_TO_NEEDRECV  : 수령요청 이동 중
    # AT_NEEDRECV        : NeedRecv 도착, 사용자 수령 대기
    # COUNT_CONFIRMED    : 수량 확인 완료, 1호기 이동 명령 대기
    amr_state = "IDLE"

    # 색상 판별 대기 (R1 요청 → Pi 응답 대기 중, 분류는 주문과 무관)
    pending_color: bool = False          # Pi 응답 대기 중 여부
    color_buf: list = []                 # 수신한 색상값 임시 저장 (SortDone 시 DB 반영 후 소각)
    color_request_count: int = 0         # Sort 중 ColorRequest 순서 (0=CpC, 1~4=WpC, 5=BpC)

    # 판별 진행 상태
    inspection_face               = 0                  # 완료된 회전 수 (0~4)
    inspection_last_result: str | None = None          # R2_ARD 결과 임시 저장 ("O" / "X")
    inspection_rotations_remaining: int = 0            # Good/Bad 후 초기위치 복귀 잔여 회전 수
    p2_rotation_ready: bool            = True          # PLC2 회전 가능 여부

    # 스테이션 점유 상태 — 명령 발행 시 서버가 직접 업데이트 (SM 상태 신뢰)
    station_assembly: int | None          = None              # 조립대     (order_id or None)
    station_output:   list[int | None]    = [None, None, None]  # 출력대기 1,2,3 (index 0=1번)
    station_amr_conv: list[int | None]    = [None, None, None]  # AMR 컨베이어 적재 (수령 완료 대기)
    # (order_id, state) or None — state: "input_moving" / "awaiting" / "inspecting" / "Good" / "Bad"
    inspection_state: tuple | None        = None              # 판별대 상태
    station_input                         = 0   # R1 P1 입력 컨베이어 분류 대기 수량
    consol_input                          = 0   # 사용자 콘솔 입력 세트 수 (AMR 컨베이어 적재)

    # 분류명령 가능여부
    Sort_Available      = False  # 처음에는 분류 불가. 인풋이 없기때문.

    # P1 입력 컨베이어 수신 준비 여부
    # True : AMR_ARD로부터 다음 자재 수신 가능
    # False: 수신 명령 발행 후 PLC1 "02" 수신 후
    p1_ready_input      = True

    # AMR_ARD 출력 수령 상태 (출력 컨베이어 → AMR 컨베이어 핸드셰이크)
    amr_ard_recv_pending = False  # "받음" 수신 후 decide() 트리거용 플래그
    amr_ard_recv_count: int = 0   # "받음" 수신 횟수 (AT_R2TRANSFER 도착 시 초기화)
    amr_pickup_total:   int = 0   # 이번 픽업에서 수령할 총 수량 (station_output 기준)

    # P1 컨베이어 작업 상태
    # None             : 유휴
    # "INPUT_RECEIVING": AMR → P1 입력 수신 중
    # "SORT_MOVING"    : 분류완료 후 후단 자재 → 분류위치 이동 중
    p1_state = None

    # P2 작업 상태 (판별대·이송 컨베이어 독립 운용 → 동시 True 가능)
    p2_inspecting   = False   # 판별대 회전 중
    p2_transferring = False   # 출력 컨베이어 → AMR 이송 중

    # 적재명령 가능여부
    Stack_Available     = True

    # 조립명령 가능여부 (P1 "07" 수신 시 True, 조립 명령 발행 시 False)
    Assembly_Available  = False

    # 이송대로 이송명령 가능여부
    Transfer_to_Transfer_Available = True #amr과 p2를 통해 이송 명령 중일때는 False로 바뀌어야함. 이후 끝 신호 보내며 True로 다시 바꿈.

    # R1 초기 자재 현황 문자열 (서버 시작 시 입력, R1 접속 후 전송)
    r1_init_data: str | None = None


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


def amr_send(cmd: str):
    """AMR ARCL 명령 전송 (LD-90, \r\n 필수)."""
    send_to("AMR", cmd, terminator="\r\n")


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
_stdin_queue:  queue.Queue = queue.Queue()   # 사용자 입력 줄 (stdin 전용 스레드가 투입)


def _stdin_reader_thread():
    """stdin 전용 스레드 — input() 블로킹을 격리해 console_thread와 충돌 방지."""
    while True:
        try:
            line = input()
            _stdin_queue.put(line.strip())
        except EOFError:
            break


def console_thread():
    """
    콘솔 인터랙션 전담 스레드.
    - console_queue : 자재입력 / 수령확인 태스크 수신 (장비 이벤트 발생 시)
    - _stdin_queue  : 사용자 직접 입력 (주문 생성 등)

    주문 입력 형식:
      order <x> <y> <W1W2W3W4>      예) order 1 1 WWYW
      order <x> <y> <BW1W2W3W4C>   예) order 1 1 WWYWYW  (base+벽4+ceil)
    색상 코드: W=하양 / Y=노랑 / P=분홍
    """
    threading.Thread(target=_stdin_reader_thread, daemon=True).start()
    print("\n[콘솔] 주문입력: order <x> <y> <색상4자리>  예) order 1 1 WWYW")

    waiting_for: str | None = None      # "INPUT_COUNT" | "OUTPUT_CONFIRM"
    output_order_id: int | None = None

    while True:
        # ── console_queue 태스크 처리 (비블로킹) ──────────────────────
        try:
            task_type, payload = console_queue.get_nowait()

            if task_type == "INPUT":
                print("\n" + "="*50)
                print("[자재입력요청] AMR 컨베이어에 자재 세트를 올려주세요.")
                print("="*50)
                print("  몇 세트 입력하셨나요? > ", end="", flush=True)
                waiting_for = "INPUT_COUNT"

            elif task_type == "OUTPUT":
                output_order_id = payload
                print("\n" + "="*50)
                print(f"[완성품수령요청] 완성 모듈을 수령해주세요.")
                print("-"*50)
                for i, oid in enumerate(SM.station_amr_conv):
                    slot_label = f"  {i+1}번칸 = " + (f"주문 #{oid} 모듈" if oid is not None else "(비어있음)")
                    print(slot_label)
                print("="*50)
                print("  수령 완료 후 Enter > ", end="", flush=True)
                waiting_for = "OUTPUT_CONFIRM"

        except queue.Empty:
            pass

        # ── stdin 입력 처리 (비블로킹) ────────────────────────────────
        try:
            line = _stdin_queue.get_nowait()

            if waiting_for == "INPUT_COUNT":
                try:
                    n = int(line)
                    SM.consol_input = n
                    send_to("AMR_ARD", f"CheckCount:{n}")
                    log("CONSOLE", f"자재 {n}세트 입력 완료 → AMR_ARD 수량 확인 요청")
                    waiting_for = None
                except ValueError:
                    log("CONSOLE", "[오류] 숫자 입력 필요")
                    print("  몇 세트 입력하셨나요? > ", end="", flush=True)

            elif waiting_for == "OUTPUT_CONFIRM":
                SM.amr_state = "IDLE"
                # station_amr_conv에서 해당 주문 슬롯 정리
                for i, oid in enumerate(SM.station_amr_conv):
                    if oid == output_order_id:
                        SM.station_amr_conv[i] = None
                        break
                wq_done(output_order_id)
                log("CONSOLE", f"주문 #{output_order_id} 수령 완료 확인")
                waiting_for = None
                output_order_id = None
                decide()

            else:
                # ── 일반 명령 ──────────────────────────────────────────
                parts = line.split()
                cmd = parts[0].lower() if parts else ""

                if cmd == "order" and len(parts) == 2 and parts[1].lower() == "clear":
                    _wq_pending.clear()
                    _wq_order_stage.clear()
                    _completed_orders.clear()
                    for k in _wq_active:
                        _wq_active[k] = None
                    db.clear_orders()
                    log("CONSOLE", "주문 큐 + DB 주문 데이터 전체 초기화 완료")

                elif cmd == "order" and len(parts) >= 4:
                    try:
                        x, y = int(parts[1]), int(parts[2])
                    except ValueError:
                        print("  오류: order <x정수> <y정수> <색상>  예) order 1 1 WWYW")
                        continue
                    colors = parts[3].upper()
                    if len(colors) == 4:
                        w1, w2, w3, w4 = colors
                        oid = db.create_order(x, y, 1, w1, w2, w3, w4)
                    elif len(colors) == 6:
                        oid = db.create_order(
                            x, y, 1,
                            colors[1], colors[2], colors[3], colors[4],
                            base_color=colors[0], ceil_color=colors[5]
                        )
                    else:
                        print("  색상: 4자리(벽만) 또는 6자리(base+벽4+ceil)")
                        continue
                    if oid:
                        wq_enqueue(oid)
                        decide()
                        log("CONSOLE", f"주문 #{oid} 생성 → 큐 진입")
                    else:
                        log("CONSOLE", "주문 생성 실패 (좌표 중복 등)")

                elif cmd == "help":
                    print("  order <x> <y> <색상4자리>   예) order 1 1 WWYW")
                    print("  order <x> <y> <색상6자리>   예) order 1 1 WWYWYW  (base+벽4+ceil)")
                    print("  색상코드: W=하양 / Y=노랑 / P=분홍")

                elif line:
                    print(f"  알 수 없는 명령: {line!r}  (help 입력)")

        except queue.Empty:
            time.sleep(0.05)


def plc_recv_thread(device: str, sock: socket.socket):
    """
    PLC 전용 수신 스레드. 고정 프레임 20바이트 단위로 수신.
    헤더 4바이트(타입2 + 명령코드2) + 페이로드 16바이트, ASCII, CR/LF 없음.
    수신한 20바이트를 그대로 message_queue에 투입.
    handle_plc1/2에서 msg[:4](헤더), msg[4:].strip()(페이로드)로 파싱.

    PLC1 전용: 30초 무통신 시 하트비트("00") 재송신, 수신("99") 시 타이머 자동 초기화.
    """
    sock.settimeout(HEARTBEAT_TIMEOUT)
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
        except socket.timeout:
            if device == "PLC1":
                plc_send_to("PLC1", "P1", "00", "")
                log("PLC1", "하트비트 재송신 (3분 무통신)")
            elif device == "PLC2":
                plc_send_to("PLC2", "P2", "00", "")
                log("PLC2", "하트비트 재송신 (3분 무통신)")
        except Exception as e:
            log("RECV", f"[오류] {device}: {e}")
            break
    _disconnect(device)


def device_recv_thread(device: str, sock: socket.socket):
    """
    R1/R2/RASPI/R2_ARD/AMR_ARD 수신 전용 스레드.
    줄바꿈(\\n) 기준으로 메시지 분리 후 message_queue 투입.
    PLC1/PLC2는 plc_recv_thread() 사용.
    R1 전용: 30초 무통신 시 ConnectionCheck 재송신.
    """
    buf = ""
    if device == "R1":
        sock.settimeout(HEARTBEAT_TIMEOUT)
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
                    # AMR_ARD 상태/하트비트 메시지 로그 억제
                    if device == "AMR_ARD" and line.startswith("AMR_ARD:"):
                        continue
                    log("RECV", f"← {device}: {line}")
                    message_queue.put((device, line))
        except socket.timeout:
            if device == "R1":
                send_to("R1", "ConnectionCheck")
                log("R1", "하트비트 재송신 (30초 무통신)")
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
            authenticated = False
            skip_dump = False   # 인증 직후 help dump 무시 플래그
            while True:
                data = sock.recv(1024).decode(errors="ignore")
                if not data:
                    break
                SM.last_seen["AMR"] = time.time()
                buf += data
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    if not authenticated and "password" in line.lower():
                        sock.sendall((AMR_PASSWORD + "\r\n").encode())
                        authenticated = True
                        skip_dump = True   # 이후 help dump 무시 시작
                        log("AMR", "ARCL 패스워드 전송 완료")
                    elif skip_dump:
                        if "End of commands" in line:
                            skip_dump = False  # dump 끝 → 이후 정상 수신
                            log("AMR", "ARCL 연결 준비 완료")
                    else:
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
    접속 IP로 장비 식별 후 recv 스레드 기동.
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

            if device == "PLC1":
                plc_send_to("PLC1", "P1", "00", "")
                log("PLC1", "연결확인 하트비트 전송")
            if device == "PLC2":
                plc_send_to("PLC2", "P2", "00", "")
                log("PLC2", "연결확인 하트비트 전송")
            if device == "R1":
                send_to("R1", "ConnectionCheck")
                log("R1", "연결확인 하트비트 전송")
                if SM.r1_init_data:
                    send_to("R1", SM.r1_init_data)
                    log("R1", f"초기 자재 현황 전송: {SM.r1_init_data}")

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
    # 0순위: AMR 화물 비우기 (최우선)
    _decide_amr_cargo()

    # 1순위: 분류 명령 (입력 자재 정리 → 재고 확보)
    _decide_sort()

    # 2순위: 판별 사이클
    _decide_inspection()

    # 3순위: 주문별 다음 명령
    active = [
        (oid, stage)
        for oid, stage in _wq_order_stage.items()
        if stage not in ("DONE", "DISPOSED")
    ][:MAX_ACTIVE_ORDERS]

    for order_id, stage in active:
        _advance(order_id, stage)

    # 4순위: 대기 주문 파이프라인 진입 시도
    if len(active) < MAX_ACTIVE_ORDERS:
        _try_start_next()

    # 5순위: AMR IDLE 목적지 결정
    _decide_amr_idle()


def _build_stack_cmd(order: dict) -> str:
    """
    Stack 명령 문자열 생성 + 재고 차감.
    순서: base → wall×4 → ceil
    반환: "Stack:XXXXXX" (각 자리 = 빈번호)
    BpC/CpC는 재고 많은 쪽 우선. WpC는 get_pick_bin() 사용.
    """
    bins = []

    # 베이스 (BpC) — 재고 많은 쪽 우선
    b1, b2 = db.get_stock("BpC1"), db.get_stock("BpC2")
    bpc = "BpC1" if b1 >= b2 else "BpC2"
    db.adjust_stock(bpc, -1)
    bins.append(bpc[3:])   # "1" or "2"

    # 벽 × 4 (WpC) — get_pick_bin 사용
    for key in ["wall1_color", "wall2_color", "wall3_color", "wall4_color"]:
        wpc = db.get_pick_bin(order[key])
        db.adjust_stock(wpc, -1)
        bins.append(wpc[3:])

    # 천장 (CpC) — 재고 많은 쪽 우선
    c1, c2 = db.get_stock("CpC1"), db.get_stock("CpC2")
    cpc = "CpC1" if c1 >= c2 else "CpC2"
    db.adjust_stock(cpc, -1)
    bins.append(cpc[3:])   # "1" or "2"

    return "Stack:" + "".join(bins)


def _advance(order_id: int, stage: str):
    """주문 단계별 다음 액션 결정. AWAITING 단계만 처리."""

    if stage == "AWAITING_ASSEMBLY":
        # R2 유휴 + 조립대 비어있음 + 조립 가능 → R2에 조립 명령
        if SM.r2_state == "IDLE" and SM.station_assembly is None and SM.Assembly_Available:
            send_to("R2", "Assembly")   # TODO: 네이밍 확정 필요
            SM.r2_state = "ASSEMBLY"
            SM.station_assembly = order_id
            SM.Assembly_Available = False
            _wq_order_stage[order_id] = "ASSEMBLY"
            wq_assign("R2", order_id, "ASSEMBLY")
            db.set_order_process(order_id, "ASSEMBLY")

    elif stage == "AWAITING_TRANSFER":
        # R2 유휴 + 판별대 비어있으면 → 판별대 이송 명령
        if SM.r2_state == "IDLE" and SM.inspection_state is None:
            send_to("R2", "Transfer")   # TODO: 네이밍 확정 필요
            SM.r2_state = "TRANSFER"
            SM.inspection_state = (order_id, "input_moving")
            SM.station_assembly = None
            _wq_order_stage[order_id] = "TRANSFER"
            wq_assign("R2", order_id, "TRANSFER")

    elif stage == "AWAITING_OUTPUT":
        # R2 유휴 + 출력대기 3번 비어있으면 + 컨베이어 안전 확인 → 출력이송 명령
        if SM.r2_state == "IDLE" and SM.station_output[2] is None and SM.Transfer_to_Transfer_Available:
            send_to("R2", "OutputTransfer")   # TODO: 네이밍 확정 필요
            # ── 이중 가드: 아래 두 값은 항상 동시에 세팅되고 PLC2 "03" 수신 시 동시에 해제됨.
            # Transfer_to_Transfer_Available=False + station_output[2]≠None 이 둘이 함께
            # 다음 OutputTransfer 명령 발행을 막는다. 딜레이로 "03"이 늦게 와도 이중으로 안전.
            SM.Transfer_to_Transfer_Available = False
            SM.r2_state          = "OUTPUT"
            SM.station_output[2] = order_id   # 물리 도착 전 논리 선점 (확정은 PLC2 "03" 기준)
            SM.inspection_state  = None
            _wq_order_stage[order_id] = "OUTPUT_TRANSFER"
            wq_assign("R2", order_id, "OUTPUT_TRANSFER")
            db.set_order_process(order_id, "OUTPUT")

    elif stage == "AWAITING_DISPOSAL":
        # R2 유휴이면 → 폐기 명령
        if SM.r2_state == "IDLE":
            send_to("R2", "Disposal")   # TODO: 네이밍 확정 필요
            SM.r2_state         = "DISPOSAL"
            SM.inspection_state = None
            _wq_order_stage[order_id] = "DISPOSAL"
            wq_assign("R2", order_id, "DISPOSAL")
            db.update_order_status(order_id, "DISPOSED")

    elif stage == "AWAITING_AMR":
        # AMR 유휴이면 → 출력 컨베이어 위치로 이동 명령
        if SM.amr_state == "IDLE":
            amr_send("executeMacro 2호기")
            SM.amr_state = "GOING_TO_R2TRANSFER"
            _wq_order_stage[order_id] = "AMR_PICKUP"
            wq_assign("AMR", order_id, "AMR_PICKUP")

    elif stage == "SORT_WAITING":
        # 재고 확보됐으면 → 적재 명령 (wq_pop은 _try_start_next에서 이미 수행)
        if db.can_fulfill_order(order_id):
            if SM.r1_state == "IDLE" and SM.Stack_Available and SM.station_assembly is None:
                order = db.get_order(order_id)
                stack_cmd = _build_stack_cmd(order)   # 빈번호 형식 + 재고 차감
                _wq_order_stage[order_id] = "STACKING"
                wq_assign("R1", order_id, "STACKING")
                SM.r1_state = "STACKING"
                SM.Stack_Available = False
                send_to("R1", stack_cmd)
                log("R1", f"주문 #{order_id} → {stack_cmd}")


def _try_start_next():
    """대기 주문 중 파이프라인 진입 가능한 것 시작."""
    order_id = wq_peek()
    if not order_id:
        return

    if db.can_fulfill_order(order_id):
        # 재고 충분 + R1 유휴 + 적재 가능 + 조립대 비어있으면 → 적재 명령
        if SM.r1_state == "IDLE" and SM.Stack_Available and SM.station_assembly is None:
            order = db.get_order(order_id)
            stack_cmd = _build_stack_cmd(order)   # 빈번호 형식 + 재고 차감
            wq_pop()
            _wq_order_stage[order_id] = "STACKING"
            wq_assign("R1", order_id, "STACKING")
            SM.r1_state = "STACKING"
            SM.Stack_Available = False
            send_to("R1", stack_cmd)
            log("R1", f"주문 #{order_id} → {stack_cmd}")
    else:
        # 재고 부족 → AMR 자재 투입 흐름
        wq_pop()
        _wq_order_stage[order_id] = "SORT_WAITING"
        log("WQ", f"주문 #{order_id} 재고 부족 → 자재 투입 필요")
        # AMR NeedInput 명령은 _decide_amr()에서 처리


def _decide_amr_cargo():
    """
    AMR 화물 비우기 — 0순위 최우선.
    AT_R1INPUT(투입) / AT_R2TRANSFER(이송) 상태에서만 동작.
    """
    # 투입완료: AT_R1INPUT + 잔여 자재 없음 + p1_state None → InputDone + 다음 목적지 결정
    if SM.amr_state == "AT_R1INPUT" and SM.consol_input == 0 and SM.p1_state is None:
        send_to("AMR_ARD", "InputDone", "\n")
        awaiting = next((oid for oid, s in _wq_order_stage.items() if s == "AWAITING_AMR"), None)
        if awaiting:
            amr_send("executeMacro 2호기")
            SM.amr_state = "GOING_TO_R2TRANSFER"
            _wq_order_stage[awaiting] = "AMR_PICKUP"
            wq_assign("AMR", awaiting, "AMR_PICKUP")
            log("AMR", "투입 완료 → AWAITING_AMR 주문 존재, 직접 2호기 이동")
        else:
            amr_send("goto 박대기")
            SM.amr_state = "GOING_TO_박대기"
            log("AMR", "투입 완료 → 대기 없음, 박대기 이동")
        return

    # 투입작업: AT_R1INPUT + 잔여 자재 있음 + P1 준비됨 → 투입 트리거
    if SM.amr_state == "AT_R1INPUT" and SM.consol_input > 0 and SM.p1_ready_input:
        SM.p1_ready_input = False
        SM.p1_state = "INPUT_RECEIVING"
        send_to("AMR_ARD", "Input")            # TODO: 네이밍 확정 필요
        plc_send_to("PLC1", "P1", "01", "")   # TODO: 명령코드 확정 필요. 의미는 받아!
        log("AMR", "AT_R1INPUT — 투입 트리거 발행")
        return

    # 이송작업: AT_R2TRANSFER + T_to_T 해제됐으면 → AMR_ARD 수령 준비 후 이송 시작 (최초 1회)
    if SM.amr_state == "AT_R2TRANSFER" and SM.Transfer_to_Transfer_Available:
        SM.Transfer_to_Transfer_Available = False
        SM.p2_transferring = True
        send_to("AMR_ARD", "받아")             # AMR_ARD 수령 준비 (최초 1회만)
        plc_send_to("PLC2", "P2", "04", "")   # 이송대 물품 1개 이송 시작
        log("AMR", "AT_R2TRANSFER — AMR_ARD 수령 준비 통보, PLC2 이송 시작")

    # 출력 수령 핸드셰이크: AMR_ARD "받음" 수신 후 count 기반 분기
    if SM.amr_ard_recv_pending:
        SM.amr_ard_recv_pending = False
        if SM.amr_ard_recv_count < SM.amr_pickup_total:
            plc_send_to("PLC2", "P2", "04", "")   # 다음 물품 이송
            log("AMR_ARD", f"수령 {SM.amr_ard_recv_count}/{SM.amr_pickup_total} → PLC2 다음 이송")
        else:
            # 모든 수령 완료
            SM.Transfer_to_Transfer_Available = True
            SM.p2_transferring = False
            SM.amr_pickup_total = 0
            SM.amr_ard_recv_count = 0
            send_to("AMR_ARD", "완료")
            plc_send_to("PLC2", "P2", "05", "")   # 컨 정지
            amr_send("executeMacro 수령요청")
            SM.amr_state = "GOING_TO_NEEDRECV"
            log("AMR_ARD", "모든 출력 수령 완료 → AMR_ARD 통보, PLC2 정지, AMR NeedRecv 이동")


def _decide_sort():
    """
    분류명령 결정 — 1순위.
    Sort_Available + station_input > 0 + R1 유휴 + P1 준비 + SORT_WAITING 주문 존재 시 발행.
    """
    if not SM.Sort_Available:
        return
    if SM.station_input == 0:
        return
    if SM.r1_state != "IDLE":
        return
    if not SM.p1_ready_input:
        return
    if not any(s == "SORT_WAITING" for s in _wq_order_stage.values()):
        return

    send_to("R1", "Sort")   # TODO: 네이밍 확정 필요
    SM.r1_state = "SORTING"
    SM.Sort_Available = False
    log("R1", "분류 명령 발행")


def _decide_inspection():
    """
    판별 사이클 결정 — 2순위.
    inspection_state 기반으로 판별 요청 / 회전 명령 / 양불 판정 처리.
    모든 명령 결정은 여기서. handle_r2_ard / handle_plc2는 SM 업데이트 + decide() 호출만.
    """
    if SM.inspection_state is None:
        return
    order_id, state = SM.inspection_state

    # 면 준비 완료 → 판별 요청
    if state == "awaiting" and SM.p2_rotation_ready:
        SM.inspection_state = (order_id, "inspecting")
        send_to("R2_ARD", "Check")   # TODO: 네이밍 확정 필요
        return

    # 판별 결과 수신 → 회전 or 완료 판정
    if state == "inspecting" and SM.inspection_last_result is not None:
        result = SM.inspection_last_result
        SM.inspection_last_result = None   # 소각

        if result == "O":
            SM.inspection_face += 1
            if SM.inspection_face < 4:
                SM.p2_rotation_ready = False
                SM.inspection_state  = (order_id, "awaiting")
                plc_send_to("PLC2", "P2", "01", "")   # 판별대 회전
            else:
                # 4면 완료 → 초기위치 복귀 1회 후 AWAITING_OUTPUT
                SM.inspection_state            = (order_id, "Good")
                SM.inspection_face             = 0
                SM.inspection_rotations_remaining = 0   # 즉시 보내는 "01"이 마지막 복귀 회전
                SM.p2_rotation_ready           = False
                plc_send_to("PLC2", "P2", "01", "")   # 초기위치 복귀 회전 (4번째)
                log("INSP", f"주문 #{order_id} 4면 완료 → 초기위치 복귀 후 AWAITING_OUTPUT")

        elif result == "X":
            remaining = 4 - SM.inspection_face
            bad_face  = SM.inspection_face
            SM.inspection_state            = (order_id, "Bad")
            SM.inspection_face             = 0
            SM.inspection_rotations_remaining = remaining - 1   # 즉시 보내는 "01" 1회 차감
            if remaining > 0:
                SM.p2_rotation_ready = False
                plc_send_to("PLC2", "P2", "01", "")   # 초기위치 복귀 회전 시작
            else:
                _wq_order_stage[order_id] = "AWAITING_DISPOSAL"
            log("INSP", f"주문 #{order_id} 불량 판정 (face={bad_face}) → 복귀 {remaining}회 후 AWAITING_DISPOSAL")

    # Good/Bad 복귀 회전 처리
    if state in ("Good", "Bad") and SM.p2_rotation_ready:
        if SM.inspection_rotations_remaining > 0:
            SM.inspection_rotations_remaining -= 1
            SM.p2_rotation_ready = False
            plc_send_to("PLC2", "P2", "01", "")
        else:
            if state == "Good":
                _wq_order_stage[order_id] = "AWAITING_OUTPUT"
                log("INSP", f"주문 #{order_id} 복귀 완료 → AWAITING_OUTPUT")
            else:
                _wq_order_stage[order_id] = "AWAITING_DISPOSAL"
                log("INSP", f"주문 #{order_id} 복귀 완료 → AWAITING_DISPOSAL")


def _decide_amr_idle():
    """AMR IDLE 목적지 결정 — 3순위, _advance() 이후 실행."""
    # 수량 확인 완료 → 1호기 이동
    if SM.amr_state == "COUNT_CONFIRMED":
        amr_send("executeMacro 1호기")
        SM.amr_state = "GOING_TO_R1INPUT"
        log("AMR", "수량 확인 완료 → 1호기 이동 명령")
        return

    if SM.amr_state != "IDLE":
        return

    # 출력대기 1번에 완성품 대기 중 → _advance AWAITING_AMR에서 처리
    if SM.station_output[0] is not None:
        return

    # 자재 투입 필요한 주문 있음
    sort_waiting = [
        oid for oid, stage in _wq_order_stage.items()
        if stage == "SORT_WAITING"
    ]
    if sort_waiting and SM.station_input == 0:
        amr_send("executeMacro 자재요청")
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

    if msg.startswith("R1_state:"):
        state = msg.split(":", 1)[1].strip()
        log("R1", f"상태 확인 회신: {state}")
        if state == "ERROR":
            log("R1", "[경고] R1 ERROR 상태 수신 — 확인 필요")
        return

    if "ColorRequest" in msg:
        n = SM.color_request_count
        SM.color_request_count += 1

        if n == 0:   # 1번째 — 천장(CpC) 자체 배정
            c1 = db.get_stock("CpC1")
            c2 = db.get_stock("CpC2")
            if c1 <= c2:
                db.adjust_stock("CpC1", 1)
                send_to("R1", "Pos:1")
                log("R1", "CpC 자체 배정 → CpC1 (Pos:1)")
            else:
                db.adjust_stock("CpC2", 1)
                send_to("R1", "Pos:2")
                log("R1", "CpC 자체 배정 → CpC2 (Pos:2)")

        elif 1 <= n <= 4:   # 2~5번째 — 벽(WpC) RASPI 판별
            SM.pending_color = True
            send_to("RASPI", "ColorRequest")
            log("R1", f"WpC 색상 판별 요청 → RASPI 전달 ({n}번째)")

        elif n == 5:   # 6번째 — 베이스(BpC) 자체 배정
            b1 = db.get_stock("BpC1")
            b2 = db.get_stock("BpC2")
            if b1 <= b2:
                db.adjust_stock("BpC1", 1)
                send_to("R1", "Pos:1")
                log("R1", "BpC 자체 배정 → BpC1 (Pos:1)")
            else:
                db.adjust_stock("BpC2", 1)
                send_to("R1", "Pos:2")
                log("R1", "BpC 자체 배정 → BpC2 (Pos:2)")

    elif "SortDone" in msg:
        # 모든 재고(BpC/CpC/WpC)는 ColorRequest 시점에 이미 처리됨
        SM.r1_state = "IDLE"
        SM.p1_state = "SORT_MOVING"
        SM.station_input -= 1
        SM.Sort_Available = False
        SM.color_request_count = 0
        if SM.station_input > 0:
            plc_send_to("PLC1", "P1", "04") # p1에게 컨베이어 재조정명령
            # TODO: PLC1 후단 자재 이동 명령 발행 — 래더담당자 명령코드 확정 필요
            log("R1", "분류 완료 → 재고 업데이트 필요(TODO), 후단 자재 이동 대기")
        decide()

    elif "StackDone" in msg:
        wq_complete("R1")
        SM.r1_state = "IDLE"
        log("R1", "적재 동작 완료 → P1 07 신호 대기")
        decide()
    


def handle_r2(msg: str):
    """R2 수신 메시지 처리."""

    if "AssemblyDone" in msg:
        wq_complete("R2")
        SM.r2_state = "IDLE"
        order_id = next(
            (oid for oid, s in _wq_order_stage.items() if s == "ASSEMBLY"),
            None
        )
        if order_id:
            _wq_order_stage[order_id] = "AWAITING_TRANSFER"
        log("R2", f"조립 완료 → 주문 #{order_id} AWAITING_TRANSFER")
        decide()

    elif "TransferToInspectionDone" in msg:
        wq_complete("R2")
        SM.r2_state = "IDLE"
        if SM.inspection_state is not None:
            order_id, _ = SM.inspection_state
            SM.inspection_state = (order_id, "awaiting")
            _wq_order_stage[order_id] = "INSPECTION"
            log("R2", f"판별대 이송 완료 → 주문 #{order_id} INSPECTION 시작")
        decide()

    elif "DisposalDone" in msg:
        wq_complete("R2")
        SM.r2_state = "IDLE"
        SM.inspection_state = None
        order_id = next(
            (oid for oid, s in _wq_order_stage.items() if s == "DISPOSAL"),
            None
        )
        if order_id:
            _wq_order_stage.pop(order_id, None)
            _completed_orders.append(order_id)
            log("R2", f"폐기 완료 → 주문 #{order_id} DISPOSED")
        decide()

    elif "TransferToOutputConvDone" in msg:
        # station_output[2]는 OutputTransfer 명령 발행 시 이미 세팅됨
        # [0]/[1] 확정은 PLC2 "03" (물품도착) 기준으로 처리
        wq_complete("R2")
        SM.r2_state = "IDLE"
        log("R2", "출력이송 완료 → R2 IDLE (station[2] PLC2 물품도착 대기 중)")
        decide()


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

    if cmd_code == "99":    # 생존확인
        pass
    if cmd_code == "02":    # 받음 — p1_state에 따라 의미 분기
        if SM.p1_state == "INPUT_RECEIVING":   # AMR 컨베이어 → R1 컨베이어 1개 이송 확인
            SM.station_input += 1       # R1 컨베이어 대기 수량 증가
            SM.consol_input -= 1        # AMR 컨베이어 잔여 수량 감소
            SM.p1_ready_input = True    #다음거 받을 준비된것
            if SM.consol_input == 0:
                SM.p1_state = None
                SM.Sort_Available = True
                plc_send_to("PLC1", "P1", "03", "")         # P1에게 입력 끝 신호
                # InputDone + 박대기 이동은 _decide_amr_cargo()에서 처리
            decide()                    # consol_input > 0이면 _decide_amr_cargo에서 다음 투입 트리거
    if cmd_code == "05":    # 분류컨베이어 자재위치 재조정 끝, 분류가능
        SM.Sort_Available = True
        SM.p1_state = None
        decide()
    if cmd_code == "06":    # 적재가능신호(적재실린더 후진, 물품유무센서 무)
        SM.Stack_Available = True
        decide()
    if cmd_code == "07":    # 적재 완료 확인 (실린더 전진 + 물품 있음 → 조립 가능)
        SM.Assembly_Available = True
        order_id = next(
            (oid for oid, s in _wq_order_stage.items() if s == "STACKING"),
            None
        )
        if order_id:
            _wq_order_stage[order_id] = "AWAITING_ASSEMBLY"
        log("PLC1", f"적재 완료 확인 → Stack_Available, 주문 #{order_id} AWAITING_ASSEMBLY")
        decide()


def handle_plc2(msg: str):
    """
    PLC2 소켓 푸시 메시지 처리.
    PLC 래더에서 조건 성립 시 직접 서버로 전송.
    """
    # TODO: 명령코드 확정 필요
    msg_type = msg[:2]
    cmd_code = msg[2:4]
    payload  = msg[4:].strip()

    if msg_type != "P2":
        log("PLC2", f"[무시] 잘못된 타입: {msg_type!r}")
        return

    if cmd_code == "99":    # 생존확인 회신
        pass
    if cmd_code == "02":    # 판별대 회전 완료
        SM.p2_rotation_ready = True
        decide()
    if cmd_code == "03":    # 물품도착 — R2 이송 완료, station[2] 아이템을 앞 슬롯으로 확정
        # ── 이중 가드 해제: AWAITING_OUTPUT에서 동시에 세팅한 두 값을 여기서 동시에 해제.
        # T_to_T_A=True + station_output[2]=None(분기 내부) → 다음 OutputTransfer 허용.
        SM.Transfer_to_Transfer_Available = True   # R2 이송 완료 → 항상 해제
        order_id = SM.station_output[2]
        if order_id is not None:
            if SM.station_output[0] is None:
                SM.station_output[0] = order_id
                SM.station_output[2] = None
                _wq_order_stage[order_id] = "AWAITING_AMR"
                log("PLC2", f"물품도착 → 주문 #{order_id} station[0] 확정, AWAITING_AMR")
            elif SM.station_output[1] is None:
                SM.station_output[1] = order_id
                SM.station_output[2] = None
                _wq_order_stage[order_id] = "AWAITING_AMR"
                log("PLC2", f"물품도착 → 주문 #{order_id} station[1] 확정, AWAITING_AMR")
            else:
                # [0], [1] 모두 점유 → [2] 유지, AMR이 2호기 대기 중이면 pickup_total 갱신
                _wq_order_stage[order_id] = "AWAITING_AMR"
                if SM.amr_state == "AT_R2TRANSFER":
                    SM.amr_pickup_total += 1
                log("PLC2", f"물품도착 → station[0],[1] 모두 점유, 주문 #{order_id} station[2] 유지 (pickup_total={SM.amr_pickup_total})")
            decide()
        else:
            log("PLC2", "[경고] 물품도착 수신했으나 station[2]가 비어있음")


def handle_raspi(msg: str):
    """
    RaspberryPi 색상 판별 결과 처리 (벽 패널 전용).
    색상값(W/Y/P) → get_sort_bin()으로 빈 결정 → 재고 업데이트 → Pos:N 으로 R1에 응답.
    """
    color = msg.strip()
    if color in db.WPC_BINS:
        wpc = db.get_sort_bin(color)
        if wpc:
            db.adjust_stock(wpc, 1)
            pos = int(wpc[3:])           # "WpC2" → 2
            send_to("R1", f"Pos:{pos}")
            log("RASPI", f"색상 {color!r} → {wpc} (Pos:{pos}) → R1 전송")
        else:
            log("RASPI", f"[경고] {color!r} 모든 빈 가득 참 (MAX={db.MAX_WPC}) — R1 응답 불가")
    else:
        log("RASPI", f"[무시] 알 수 없는 색상값: {color!r}")


def handle_r2_ard(msg: str):
    """
    R2_ARD 양불 판별 결과 처리.
    # TODO: 네이밍 확정 필요

    예상 메시지 형태 (확정 전):
      "O" / "X"  → 양품 / 불량
    """
    if msg == "O":
        SM.inspection_last_result = "O"
        log("R2_ARD", "판별 양품 → decide() 위임")
        decide()
    elif msg == "X":
        SM.inspection_last_result = "X"
        log("R2_ARD", "판별 불량 → decide() 위임")
        decide()


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
    if "Completed macro 자재요청" in msg:
        SM.amr_state = "AT_NEEDINPUT"
        log("AMR", "NeedInput 도착 — 자재 입력 요청")
        console_queue.put(("INPUT", None))

    elif "Completed macro 1호기" in msg:
        SM.amr_state = "AT_R1INPUT"
        log("AMR", "R1Input 도착 — 투입 트리거는 decide()에서 처리")
        decide()

    elif "Completed macro 2호기" in msg:
        SM.amr_state = "AT_R2TRANSFER"
        SM.amr_pickup_total = sum(1 for x in SM.station_output[:2] if x is not None)
        SM.amr_ard_recv_count = 0
        log("AMR", f"R2Transfer 도착 — 수령 대상 {SM.amr_pickup_total}개, 이송 트리거는 decide()에서 처리")
        decide()

    elif "Arrived at 박대기" in msg:
        SM.amr_state = "IDLE"
        log("AMR", "박대기 도착 → IDLE")
        decide()

    elif "Completed macro 수령요청" in msg:
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
        SM.amr_state = "COUNT_CONFIRMED"
        log("AMR_ARD", "수량 확인 완료 → decide() 위임")
        decide()

    elif msg == "CountNG":
        log("AMR_ARD", "[경고] 수량 불일치 — 재확인 필요")
        console_queue.put(("INPUT", None))

    elif msg == "받음":
        # station_output 최저 인덱스 → station_amr_conv 최저 빈 슬롯으로 이동
        src = next((i for i, x in enumerate(SM.station_output) if x is not None), None)
        dst = next((i for i, x in enumerate(SM.station_amr_conv) if x is None), None)
        if src is not None and dst is not None:
            order_id = SM.station_output[src]
            SM.station_amr_conv[dst] = order_id
            SM.station_output[src] = None
            log("AMR_ARD", f"출력 모듈 수령 → station_output[{src}] → station_amr_conv[{dst}] (주문 #{order_id})")
        SM.amr_ard_recv_count += 1
        SM.amr_ard_recv_pending = True
        log("AMR_ARD", f"수령 {SM.amr_ard_recv_count}/{SM.amr_pickup_total}")
        decide()

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
    """30초 이상 수신 없는 장비 경고. AMR/RASPI 제외."""
    _HB_SKIP = {"AMR", "RASPI"}
    now = time.time()
    for device, last in SM.last_seen.items():
        if device in _HB_SKIP:
            continue
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

    print("─" * 60)
    print("  초기 자재 현황 설정")
    print("  1. 새로 입력")
    print("  2. 이전 값 유지 (DB 현재값 사용)")
    print("─" * 60)
    _choice = input("  선택 (1/2): ").strip()

    if _choice == "1":
        print("  형식: BpC1:00;BpC2:00;CpC1:00;CpC2:00;WpC1:00;WpC2:00;WpC3:00;WpC4:00;WpC5:00;WpC6:00;")
        _init_input = input("  > ").strip()
        if _init_input:
            # DB 업데이트
            for entry in _init_input.rstrip(";").split(";"):
                if ":" in entry:
                    item, val = entry.split(":", 1)
                    try:
                        db.set_stock(item.strip(), int(val.strip()))
                    except ValueError:
                        print(f"  [경고] 파싱 실패: {entry!r} — 건너뜀")
            SM.r1_init_data = _init_input
            log("SERVER", f"초기 자재 현황 입력 및 DB 업데이트: {_init_input}")
        else:
            log("SERVER", "초기 자재 현황 미입력 — 건너뜀")
    else:
        # DB 현재값으로 R1 전송용 문자열 생성
        _inv = db.get_inventory()
        SM.r1_init_data = ";".join(f"{k}:{v:02d}" for k, v in _inv.items()) + ";"
        log("SERVER", f"이전 값 유지 → R1 전송 데이터: {SM.r1_init_data}")

    threading.Thread(target=accept_thread,  daemon=True).start()
    threading.Thread(target=amr_thread,     daemon=True).start()
    threading.Thread(target=console_thread, daemon=True).start()

    log("SERVER", "Command Center 시작")
    main_loop()
