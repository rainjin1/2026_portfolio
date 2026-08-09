"""
gateway_server.py
MES 게이트웨이 서버 — 3-레이어 폴링/플래그/결정 구조

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[서버 아키텍처 — 3 Layer]

  Layer 1  데이터 수집  heartbeat_loop → _poll_plc()
           PLC 센서 읽기(주기) + 로봇 TCP 상태응답
           → SM.raw_* 갱신 후 TICK 이벤트 발행

  Layer 2  플래그 파생  _derive_flags()
           raw 센서값 + SM 비즈니스 상태 → SM 플래그 갱신
           state_machine_loop가 TICK 수신 시 / 메시지 핸들러 완료 후 호출

  Layer 3  명령 결정   _tick()
           오직 SM 플래그만 읽어서 다음 명령 결정
           직접 센서 읽기 없음

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[RAPID 담당자 참고 — 로봇 메시지 규약]

  R01 수신 (서버→R01):
    Sort                       : 분류 동작 시작 (패널 물리 투입 후에만 유효)
    R01:{id}:Stack:{6자색상}    : 적재 동작 시작
      색상코드 = base(1) + wall1~4(4) + ceil(1)  예) RWBYWR

  R01 송신 (R01→서버):
    WhatColor                  : 패널 색상 인식 요청 → Pi로 중계
    R01:SortDone               : 분류 완료
    R01:{id}:StackDone         : 적재 완료
    R01:{id}:ReadyRetract      : (미사용, R02에서 수신)

  R02 수신 (서버→R02):
    R02:{id}:Assemble          : 조립 동작 시작

  R02 송신 (R02→서버):
    R02:{id}:ReadyRetract      : 패널 수령 완료, 이송실린더 후진 가능
    R02:{id}:AssembleDone      : 조립 완료

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[AMR 운영 원칙]
  출력 수거 후  → 반드시 Home (사용자 수령 필요)
  입력 투입 후  → R2Output 바로 이동 가능 (컨베이어 빔)
  Home 방문 시  → 출력 수령 + 입력 적재 한 번에 처리
  우선순위      → 입력투입 > 출력수거 > Home 자재요청

[Sort 핵심 규칙]
  Sort = 물리적 자재 투입이 선행돼야 함
  can_fulfill = True  → Sort 없이 Stack 직행
  can_fulfill = False → AMR 투입 → MATERIAL_DONE → Sort → Stack
"""

import socket
import threading
import queue
import time

import pymcprotocol
import db


# ══════════════════════════════════════════════════════════════════════
# SECTION 0: 설정 상수
# ══════════════════════════════════════════════════════════════════════

SERVER_HOST = "0.0.0.0"
SERVER_PORT = 9000

AMR_HOST = "192.168.3.11"   # AMR IP
AMR_PORT = 7171

PLC_CONFIG = {
    "P01": {"host": "192.168.3.39", "port": 3900},
    "P02": {"host": "192.168.3.40", "port": 4000},
}

# IP → 기기명 매핑 (recv_loop에서 클라이언트 식별)
IP_MAP = {
    "192.168.3.2": "R01",
    "192.168.3.3": "R02",
    "192.168.3.21": "Pi",
    "192.168.3.23": "AMR_ARD",
}

# ── PLC 레지스터 맵: (디바이스, 번호, "bit"|"word") ─────────────────
# [RAPID 담당자] 이 레지스터들이 서버 판단의 근거가 되는 센서값
# platform_clear 조건: trans_bwd==0 AND trans_detect==0 → Stack 명령 발사
PLC_REG = {
    "P01": {
        "input_sensor": ("X", 31, "bit"),   # X31  : 패널 입력도착 센서 (0=없음, 1=도착)
        "input_count":  ("D", 13311, "word"),  # D13311: 투입할 패널 수 (서버→PLC 쓰기)
        "conv_on":      ("M", 100,  "bit"),    # M100  : 컨베이어 ON/OFF
        "trans_fwd_sen":    ("M", 120,  "bit"),    # M120  : 이송실린더 전진 상태 (1=전진)
        "trans_bwd_sen":    ("M", 121,  "bit"),    # M121  : 이송실린더 전진 상태 (1=후진)
        "trans_detect": ("M", 122,  "bit"),    # M122  : 이송대 공작물 감지 (1=있음)
        "trans_bwd_cmd":    ("M", 130,  "bit"),    # M130  : 이송실린더 후진 명령 (서버→PLC)

        # P01 입력부 레지스터
        "input_barrier1_fwd_sen": ("M", 000, "bit"),# 차단기1 전센
        "input_barrier1_bwd_sen": ("M", 000, "bit"),# 차단기1 후센
        "input_barrier1_fwd_cmd": ("M", 000, "bit"),#차단기1 전진명령 (서버→PLC)
        "input_barrier1_bwd_cmd": ("M", 000, "bit"),#차단기1 후진명령 (서버→PLC)
        "input_waiting_sensor1": ("X", 00, "bit"), #X@@: 입력대기센서1
        "input_barrier2_fwd_sen": ("M", 000, "bit"),# 차단기2 전센
        "input_barrier2_bwd_sen": ("M", 000, "bit"),# 차단기2 후센
        "input_barrier2_fwd_cmd": ("M", 000, "bit"),#차단기2 전진명령 (서버→PLC)
        "input_barrier2_bwd_cmd": ("M", 000, "bit"),#차단기2 후진명령 (서버→PLC)
        "input_waiting_sensor2":("X", 00, "bit"),  #X@@: 입력대기센서2
    },
    "P02": {},  # TODO: P02 레지스터 추가
}

HEARTBEAT_INTERVAL = 1.0   # 하트비트 + PLC 폴링 주기 (초)
RETRACT_DURATION   = 1.5   # 이송실린더 후진 소요 시간 (초)


# ══════════════════════════════════════════════════════════════════════
# SECTION 1: 공유 자원 (멀티스레드 접근)
# ══════════════════════════════════════════════════════════════════════

clients: dict = {}
clients_lock  = threading.Lock()

# 이벤트 큐: recv_loop / amr_loop / heartbeat → state_machine_loop
# 메시지 형식: (sender, payload)
#   ("R01",  "R01:SortDone")       : 로봇 메시지
#   ("AMR",  "Arrived at Home")    : AMR ARCL 메시지
#   ("_INT", "TICK")               : 하트비트 주기 이벤트
#   ("_INT", "MATERIAL_DONE")      : 자재 투입 완료 내부 이벤트
message_queue: queue.Queue = queue.Queue()
terminal_queue: queue.Queue = queue.Queue()

amr_sock      = None
amr_sock_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════
# SECTION 2: 상태 컨테이너 (SM)
#
# state_machine_loop 단일 스레드에서만 읽고 씀 → Lock 불필요
# 예외: heartbeat_loop가 SM.raw_* 에만 쓰기
#   (Python GIL 덕분에 int 단순 대입은 원자적으로 동작)
# ══════════════════════════════════════════════════════════════════════

class SM:

    # ────────────────────────────────────────────────────────────────
    # [Layer 1 출력] PLC 원시 센서값
    # heartbeat_loop(_poll_plc)에서 갱신, state_machine_loop에서 읽기만
    # ────────────────────────────────────────────────────────────────
    raw_trans_fwd_sen = 0   # M120: 이송실린더 전진 센서 (1=전진완료)
    raw_trans_bwd_sen = 0   # M121: 이송실린더 후진 센서 (1=후진완료)
    raw_trans_detect  = 0   # M122: 이송대 공작물 감지   (0=없음, 1=있음)
    # P02 센서 추가 시: raw_p02_xxx = 0

    # ────────────────────────────────────────────────────────────────
    # [Layer 2 출력] 파생 플래그
    # _derive_flags()가 갱신, _tick()이 읽기만
    # ────────────────────────────────────────────────────────────────

    # PLC 기반 플래그
    platform_clear    = False  # trans_fwd==0 AND trans_detect==0
                                # True일 때 서버가 Stack 명령 발사 가능

    # 로봇 기반 플래그
    # [RAPID 담당자] 이 플래그가 True여야 서버가 해당 로봇에 명령 전송
    r1_idle           = False  # R1이 명령 수락 가능한 상태 (IDLE)
    r2_idle           = False  # R2가 명령 수락 가능한 상태 (IDLE)

    # AMR 기반 플래그
    amr_idle          = False  # AMR이 다음 목적지 명령 수락 가능
    amr_at_home       = False  # AMR이 현재 Home에 있음
    amr_has_input     = False  # AMR 컨베이어에 투입할 자재 있음
    amr_has_output    = False  # AMR 컨베이어에 완성품 있음

    # 비즈니스 로직 기반 플래그
    r2_pending        = False  # r2_queue에 조립 대기 주문 있음
    output_waiting    = False  # R2Output에 AMR 수거 대기 완성품 있음

    # ────────────────────────────────────────────────────────────────
    # [비즈니스 상태] 기기 상태 + 큐
    # ────────────────────────────────────────────────────────────────

    # 기기 상태 문자열 (Layer 2에서 플래그로 파생됨)
    r1_state  = "IDLE"    # IDLE / SORTING / STACKING       ← 서버가 관리 (명령 기반)
    r2_state  = "IDLE"    # IDLE / ASSEMBLING               ← 서버가 관리 (명령 기반)
    amr_state = "IDLE"    # IDLE / TO_HOME / AT_HOME / TO_R1 / AT_R1 / TO_R2 / AT_R2
    # R2 상태 확장 시: "WAITING_RETRACT" 등 추가 후 _derive_robot_flags에 플래그 추가

    # 로봇이 하트비트로 자체 보고한 실제 상태 (recv_loop 스레드에서 갱신)
    # 단순 str 대입 → Python GIL 덕분에 별도 Lock 불필요
    r1_state_actual = "IDLE"   # R01:Ready→IDLE / R01:Sorting→SORTING / R01:Stacking→STACKING
    r2_state_actual = "IDLE"   # R02:Ready→IDLE / R02:Assemblying→ASSEMBLING

    r1_order  = None      # R1이 현재 처리 중인 order_id
    r2_order  = None      # R2가 현재 조립 중인 order_id

    order_queue     = []             # 터미널 입력 대기 주문 (FIFO)
    r2_queue        = queue.Queue()  # StackDone → R2 조립 대기
    r2_output_queue = []             # AssembleDone → AMR 수거 대기 완성품 order_id

    amr_carrying_input = 0    # AMR에 실린 입력자재 세트 수 (Home에서 사용자가 적재)
    amr_output_orders  = []   # AMR에 실린 완성품 order_id 목록

    # WhatColor 처리 버퍼 (Pi 카메라 색상 인식 중계)
    waiting_color   = False
    sort_colors_buf = []

    # Home 사용자 작업 상태
    home_waiting   = False
    home_recv_done = False
    home_load_done = False

    # R1 작업 결정 플래그 (핸들러에서 세팅, _tick_r1이 소비)
    r1_stack_ready    = False  # 재고 OK → platform 비면 즉시 Stack
    r1_needs_material = False  # 재고 부족 → AMR 자재 투입 필요

    # 이송실린더 후진 상태
    retracting = False   # True = 후진 중 (중복 명령 방지)


# ══════════════════════════════════════════════════════════════════════
# SECTION 3: 유틸리티
# ══════════════════════════════════════════════════════════════════════

def log(tag: str, msg: str):
    print(f"[{time.strftime('%H:%M:%S')}][{tag}] {msg}")


def send_to(name: str, message: str) -> bool:
    """등록된 TCP 클라이언트에게 메시지 전송."""
    with clients_lock:
        sock = clients.get(name)
    if not sock:
        log("SEND", f"[경고] {name} 미연결, 드롭: {message}")
        return False
    try:
        sock.sendall((message + "\n").encode())
        log("SEND", f"→ {name}: {message}")
        return True
    except Exception as e:
        log("SEND", f"[오류] {name}: {e}")
        return False


def send_amr(cmd: str) -> bool:
    """AMR ARCL 소켓으로 명령 전송."""
    with amr_sock_lock:
        sock = amr_sock
    if not sock:
        log("AMR", "[경고] AMR 미연결")
        return False
    try:
        sock.sendall((cmd + "\n").encode())
        log("AMR", f"→ AMR: {cmd}")
        return True
    except Exception as e:
        log("AMR", f"[오류] {e}")
        return False



def plc_write(plc_id: str, reg_name: str, value: int) -> bool:
    """단일 PLC 레지스터 쓰기."""
    cfg = PLC_CONFIG.get(plc_id)
    reg = PLC_REG.get(plc_id, {}).get(reg_name)
    if not cfg or reg is None:
        log("PLC", f"[오류] 레지스터 미정의: {plc_id}/{reg_name}")
        return False
    dev, num, acc = reg
    try:
        plc = pymcprotocol.Type3E()
        plc.connect(cfg["host"], cfg["port"])
        (plc.batchwrite_bitunits if acc == "bit"
         else plc.batchwrite_wordunits)(headdevice=f"{dev}{num}", values=[value])
        plc.close()
        log("PLC", f"→ {plc_id} {reg_name}={value}")
        return True
    except Exception as e:
        log("PLC", f"[오류] 쓰기 {plc_id}/{reg_name}={value}: {e}")
        return False



# ══════════════════════════════════════════════════════════════════════
# ████████████████████████████████████████████████████████████████████
#  LAYER 1: 데이터 수집 (Polling)
#
#  역할: TCP 기기 alive 확인 + PLC 센서 주기적 읽기
#        읽은 값을 SM.raw_* 에 저장, TICK 이벤트 발행
#        → state_machine_loop가 TICK 수신 시 Layer 2,3 실행
# ████████████████████████████████████████████████████████████████████
# ══════════════════════════════════════════════════════════════════════

def _poll_plc():
    """
    [Layer 1] PLC 센서 읽기 → SM.raw_* 갱신.

    읽기 실패 시 이전 값 유지 (None 대입 안 함 → 플래그 오염 방지).
    heartbeat_loop에서 매 HEARTBEAT_INTERVAL마다 호출.

    [RAPID 담당자]
    P01 M120 (trans_fwd_sen) : 이송실린더 전진 센서 (1=전진완료)
    P01 M121 (trans_bwd_sen) : 이송실린더 후진 센서 (1=후진완료)
    P01 M122 (trans_detect)  : 이송대 공작물 감지   (1=있음)
    trans_bwd_sen==1 AND trans_detect==0 → platform_clear → _tick_r1이 Stack 명령 전송
    """
    # ── P01: M120 ~ M129 일괄 읽기 ────────────────────────────────────────
    # ┌ 변경 시: headdevice 시작주소 + 아래 인덱스 주석을 함께 수정할 것 ┐
    # └ readsize 늘리면 뒤쪽 TODO 항목을 추가로 매핑 가능                └
    try:
        plc = pymcprotocol.Type3E()
        plc.connect(PLC_CONFIG["P01"]["host"], PLC_CONFIG["P01"]["port"])
        m = plc.batchread_bitunits(headdevice="M120", readsize=10)  # M120 ~ M129
        plc.close()

        SM.raw_trans_fwd_sen = m[0]   # M120: 이송실린더 전진 센서
        SM.raw_trans_bwd_sen = m[1]   # M121: 이송실린더 후진 센서
        SM.raw_trans_detect  = m[2]   # M122: 이송대 공작물 감지
        # m[3]  = M123: TODO
        # m[4]  = M124: TODO
        # m[5]  = M125: TODO
        # m[6]  = M126: TODO
        # m[7]  = M127: TODO
        # m[8]  = M128: TODO
        # m[9]  = M129: TODO
    except Exception as e:
        log("PLC", f"[오류] P01 배치 읽기 실패 (M120~M129): {e}")

    # P02 센서 추가 시:
    # v = plc_read("P02", "some_sensor")
    # if v is not None: SM.raw_p02_xxx = v


def heartbeat_loop():
    """
    [Layer 1] 주기 실행 스레드 (daemon).

    매 HEARTBEAT_INTERVAL마다:
      ① 기기 상태 조회 메시지 전송 (TCP alive 확인)
      ② PLC 센서 읽기 → SM.raw_* 갱신
      ③ TICK 이벤트 → state_machine_loop에서 Layer 2,3 실행

    [RAPID 담당자] 하트비트 응답 형식:
      R01 응답: "R01:Ready" / "R01:Sorting" / "R01:Stacking"
      R02 응답: "R02:Ready" / "R02:Assemblying"
      → _is_status_response()에서 필터링, message_queue 투입 안 함
    """
    while True:
        time.sleep(HEARTBEAT_INTERVAL)

        # ① 기기 상태 조회
        send_to("R01",     "R1_status")
        send_to("R02",     "R2_status")
        send_to("AMR_ARD", "AMR_ARD:status")

        # ② PLC 센서 폴링 → SM.raw_* 갱신
        _poll_plc()

        # ③ TICK 이벤트 발행 → state_machine_loop → _derive_flags + _tick
        message_queue.put(("_INT", "TICK"))


# ══════════════════════════════════════════════════════════════════════
# ████████████████████████████████████████████████████████████████████
#  LAYER 2: 플래그 파생 (_derive_flags)
#
#  역할: SM.raw_* + SM 비즈니스 상태 → SM 플래그 갱신
#        state_machine_loop(단일 스레드)에서만 호출
#        어떤 상태가 바뀌든 이 함수 호출 후 _tick → 항상 최신 판단
# ████████████████████████████████████████████████████████████████████
# ══════════════════════════════════════════════════════════════════════

def _derive_flags():
    """
    [Layer 2] 전체 플래그 파생 진입점.

    호출 순서:
      PLC 플래그 → 로봇 플래그 → AMR 플래그 → 로직 플래그
    """
    _derive_plc_flags()
    _derive_robot_flags()
    _derive_amr_flags()
    _derive_logic_flags()


def _derive_plc_flags():
    """
    [Layer 2 - PLC] raw 센서값 → PLC 기반 플래그.

    platform_clear:
      이송실린더 후진 완료(trans_bwd_sen==1) AND 이송대 공작물 없음(trans_detect==0)
      → True일 때 _tick_r1이 Stack 명령 전송 가능
    """
    SM.platform_clear = (SM.raw_trans_bwd_sen == 1 and SM.raw_trans_detect == 0)


def _derive_robot_flags():
    """
    [Layer 2 - Robot] 로봇 상태 교차확인 → 로봇 기반 플래그.

    r1_idle / r2_idle 조건:
      SM.r1_state        (서버 추적) == "IDLE"   ← 서버가 명령 기반으로 관리
      SM.r1_state_actual (로봇 보고) == "IDLE"   ← 하트비트 응답으로 갱신
      두 값이 일치해야 True → 다음 명령 가능

    불일치 상황:
      서버 IDLE / 로봇 Stacking → 로봇이 아직 작업 중 (정상 전환 딜레이 or 오류)
      서버 STACKING / 로봇 Ready → Done 메시지 누락 가능성 → 명령 차단 유지

    [RAPID 담당자]
    서버가 다음 명령을 보내려면 로봇이 하트비트에서 Ready(IDLE) 상태를 보고해야 함.
    SortDone / StackDone 이벤트 후 RAPID 태스크가 Ready 상태로 전환해야 교차확인 통과.

    R2 상태 확장 예시:
      SM.r2_waiting_retract = (SM.r2_state == "WAITING_RETRACT"
                                and SM.r2_state_actual == "WAITING_RETRACT")
    """
    SM.r1_idle = (SM.r1_state == "IDLE" and SM.r1_state_actual == "IDLE")
    SM.r2_idle = (SM.r2_state == "IDLE" and SM.r2_state_actual == "IDLE")


def _derive_amr_flags():
    """[Layer 2 - AMR] AMR 상태 문자열 → AMR 기반 플래그."""
    SM.amr_idle    = (SM.amr_state == "IDLE")
    SM.amr_at_home = (SM.amr_state == "AT_HOME")

    SM.amr_has_input  = (SM.amr_carrying_input > 0)
    SM.amr_has_output = (len(SM.amr_output_orders) > 0)


def _derive_logic_flags():
    """
    [Layer 2 - 비즈니스] 큐/재고 상태 → 비즈니스 로직 플래그.

    r1_stack_ready / r1_needs_material 은 핸들러에서 직접 세팅되므로
    여기서는 초기화하지 않음 (파생이 아닌 의사결정 결과값).
    r2_pending / output_waiting 은 큐 상태에서 직접 파생.
    """
    SM.r2_pending     = not SM.r2_queue.empty()
    SM.output_waiting = len(SM.r2_output_queue) > 0



# ══════════════════════════════════════════════════════════════════════
# ████████████████████████████████████████████████████████████████████
#  LAYER 3: 명령 결정 (_tick)
#
#  역할: 현재 SM 플래그만 읽어 R1/R2/AMR 명령 결정 및 송신
#        직접 센서 읽기 없음 — 오직 플래그
#        state_machine_loop(단일 스레드)에서만 호출
# ████████████████████████████████████████████████████████████████████
# ══════════════════════════════════════════════════════════════════════

def _tick():
    """
    [Layer 3] 플래그 기반 전체 명령 결정 진입점.

    호출 전 반드시 _derive_flags() 선행.
    R1 → R2 → AMR 순서로 각 기기 결정.
    """
    _tick_r1()
    _tick_r2()
    _tick_amr()


def _tick_r1():
    """
    [Layer 3 - R1] R1 Stack 명령 결정.

    Stack 발사 조건 (세 조건 모두 True):
      r1_stack_ready  : 서버가 이미 재고 확인 완료, 대기 중인 상태
      r1_idle         : R1이 현재 IDLE (다른 작업 없음)
      platform_clear  : 이송실린더 후진 완료 + 이송대 공작물 없음

    platform_clear가 False이면 이 틱에서는 아무것도 안 함.
    다음 PLC 폴링 주기(~1s)에 platform_clear가 True가 되면 자동 발사.

    [RAPID 담당자]
    서버가 R01:{id}:Stack:{색상} 명령을 보내는 시점이 바로 이 조건 충족 순간.
    """
    if SM.r1_stack_ready and SM.r1_idle and SM.platform_clear:
        SM.r1_stack_ready = False   # 중복 발사 방지
        log("TICK", "R1 Stack 조건 충족 → Stack 명령 발사")
        _action_fire_stack()


def _tick_r2():
    """
    [Layer 3 - R2] R2 조립 명령 결정.

    조립 발사 조건:
      r2_idle    : R2 현재 IDLE
      r2_pending : r2_queue에 StackDone된 주문 대기 중

    R1 StackDone 수신 즉시 r2_queue에 추가되므로 자동 트리거.
    R1 Stacking 중에도 R2가 IDLE이면 이전 주문 조립 시작 가능 (병렬).

    [RAPID 담당자]
    서버가 R02:{id}:Assemble 명령을 보내는 시점.
    """
    if SM.r2_idle and SM.r2_pending:
        try:
            order_id = SM.r2_queue.get_nowait()
        except queue.Empty:
            return
        SM.r2_order = order_id
        SM.r2_state = "ASSEMBLING"
        db.update_order_status(order_id, "조립중")
        send_to("R02", f"R02:{order_id}:Assemble")
        log("TICK", f"R2 조립 시작: 주문 #{order_id}")


def _tick_amr():
    """
    [Layer 3 - AMR] AMR 다음 목적지 결정.

    AMR이 IDLE일 때만 실행. 우선순위:
      1순위 amr_has_input     : AMR에 자재 있음 → R01Insert (투입 우선)
      2순위 output_waiting    : R2Output 수거 대기 + AMR 컨베이어 비어있음 → R02Output
      3순위 r1_needs_material : 재고 부족 + AMR에 자재 없음 → Home (자재 요청)
    """
    if not SM.amr_idle:
        return

    # 1순위: AMR에 투입할 자재 있음 → R01Insert
    if SM.amr_has_input:
        SM.r1_needs_material = False
        SM.amr_state = "TO_R1"
        send_amr("goal: R01Insert")
        log("TICK", f"AMR → R01Insert (자재 {SM.amr_carrying_input}세트)")
        return

    # 2순위: R2Output 수거 대기 있고 AMR 컨베이어 비어있음
    if SM.output_waiting and not SM.amr_has_output:
        SM.amr_state = "TO_R2"
        send_amr("goal: R02Output")
        log("TICK", f"AMR → R02Output ({len(SM.r2_output_queue)}체 대기)")
        return

    # 3순위: 재고 부족 → Home으로 (사용자에게 자재 적재 요청)
    if SM.r1_needs_material and not SM.amr_has_input:
        SM.amr_state = "TO_HOME"
        send_amr("goal: Home")
        log("TICK", "AMR → Home (자재 적재 요청)")
        return

    log("TICK", "AMR 스케줄: 할 일 없음")


# ══════════════════════════════════════════════════════════════════════
# SECTION 4: 이벤트 핸들러
#
# 역할: 기기 메시지 수신 → SM 상태 갱신 → _derive_flags + _tick 호출
#       각 핸들러 마지막에 반드시 _derive_flags() → _tick() 쌍 호출
# ══════════════════════════════════════════════════════════════════════

def _dispatch(sender: str, msg: str):
    """수신 메시지를 기기별 핸들러로 라우팅."""
    if   sender == "R01":     _handle_r01(msg)
    elif sender == "R02":     _handle_r02(msg)
    elif sender == "Pi":      _handle_pi(msg)
    elif sender == "AMR":     _handle_amr(msg)
    elif sender == "AMR_ARD": _handle_amr_ard(msg)
    elif sender == "_INT":    _handle_internal(msg)
    else: log("SM", f"[미처리] {sender}: {msg}")


def _handle_r01(msg: str):
    """
    R01 메시지 처리.

    [RAPID 담당자 — R01이 서버에 보내는 메시지]
    WhatColor          : 분류할 패널 색상 인식 요청 → 서버가 Pi에 중계
    R01:SortDone       : 분류 완료 → 재고 업데이트 후 Stack 대기 또는 추가 자재 요청
    R01:{id}:StackDone : 적재 완료 → R2 조립 대기 큐에 추가
    """
    if msg == "WhatColor":
        # Pi(카메라)에 색상 인식 요청 중계
        SM.waiting_color = True
        send_to("Pi", "WhatColor")

    elif msg == "R01:SortDone":
        SM.r1_state = "IDLE"
        log("SM", f"SortDone | 분류 색상: {SM.sort_colors_buf}")
        _apply_sort_inventory(SM.sort_colors_buf)
        SM.sort_colors_buf = []
        db.print_inventory()

        if SM.r1_order and db.can_fulfill_order(SM.r1_order):
            # 재고 충족 → platform 비면 즉시 Stack
            SM.r1_stack_ready = True
            log("SM", "재고 충족 → Stack 대기 (platform 확인 중)")
        else:
            # 여전히 재고 부족 → 추가 자재 투입 필요
            # Sort는 물리 자재 없이 불가 → AMR에 재요청
            SM.r1_needs_material = True
            log("SM", "재고 여전히 부족 → AMR 추가 자재 요청")

    elif SM.r1_order and f"R01:{SM.r1_order}:StackDone" in msg:
        SM.r1_state       = "IDLE"
        SM.r1_stack_ready = False
        log("SM", f"StackDone | 주문 #{SM.r1_order} → R2 조립 대기")
        SM.r2_queue.put(SM.r1_order)
        SM.r1_order = None
        _try_start_next_order()   # 다음 주문 처리 시작

    elif "DISCONNECTED" in msg:
        SM.r1_state = "IDLE"
        log("SM", "[경고] R01 연결 끊김")

    _derive_flags()
    _tick()


def _handle_r02(msg: str):
    """
    R02 메시지 처리.

    [RAPID 담당자 — R02가 서버에 보내는 메시지]
    R02:{id}:ReadyRetract : 패널 수령 완료, 이송실린더 후진 가능
                            → 서버가 PLC M130=1 후진 명령 전송
    R02:{id}:AssembleDone : 조립 완료 → AMR 수거 스케줄 갱신
    """
    if SM.r2_order and f"R02:{SM.r2_order}:ReadyRetract" in msg:
        log("SM", "ReadyRetract 수신 → 이송실린더 후진 시작")
        _action_retract()

    elif SM.r2_order and f"R02:{SM.r2_order}:AssembleDone" in msg:
        completed   = SM.r2_order
        SM.r2_order = None
        SM.r2_state = "IDLE"
        log("SM", f"AssembleDone | 주문 #{completed} → R2Output 대기")
        SM.r2_output_queue.append(completed)
        db.update_order_status(completed, "이송중")

    elif "DISCONNECTED" in msg:
        SM.r2_state = "IDLE"
        log("SM", "[경고] R02 연결 끊김")

    _derive_flags()
    _tick()


def _handle_pi(msg: str):
    """Pi(카메라) 색상 인식 결과 수신 → R01에 중계."""
    color = msg.strip().upper()
    if SM.waiting_color and color in {"Y", "B", "W", "N", "D", "R"}:
        SM.waiting_color = False
        SM.sort_colors_buf.append(color)
        send_to("R01", color)
        log("SM", f"색상 중계: {color} | 누적: {SM.sort_colors_buf}")
    # Pi는 플래그 변화 없음 → _tick 불필요


def _handle_amr(msg: str):
    """
    AMR ARCL 메시지 처리. "Arrived" 수신으로 목적지 도착 확인.

    AT_HOME → 사용자 입력 대기 (home_waiting=True 동안 _tick 보류)
    AT_R1   → 자재 투입 시작 (placeholder sub-thread)
    AT_R2   → 완성품 수거 시작 (AMR_ARD 컨베이어 구동)
    """
    if "Arrived" not in msg:
        return

    if SM.amr_state == "TO_HOME":
        SM.amr_state = "AT_HOME"
        _prompt_home_user()
        # Home에서는 사용자 입력 완료 후 _after_home_complete에서 _tick 호출
        return

    elif SM.amr_state == "TO_R1":
        SM.amr_state = "AT_R1"
        log("SM", "AMR R01Insert 도착 → 자재 투입 시작")
        # 자재 투입 완료 시 message_queue에 ("_INT", "MATERIAL_DONE") 발행
        threading.Thread(target=_material_loading_thread, daemon=True).start()
        return

    elif SM.amr_state == "TO_R2":
        SM.amr_state = "AT_R2"
        # AMR 최대 3체 수용, 수거 가능한 만큼 처리
        collect = min(len(SM.r2_output_queue), 3)
        SM.amr_output_orders = SM.r2_output_queue[:collect]
        SM.r2_output_queue   = SM.r2_output_queue[collect:]
        log("SM", f"AMR R02Output 도착 → {collect}체 수거 시작")
        send_to("AMR_ARD", "AMR_ARD:CON:START")
        return

    _derive_flags()
    _tick()


def _handle_amr_ard(msg: str):
    """
    AMR 컨베이어 제어기(ARD) 완료 메시지 처리.

    AT_R2 수거 완료 → 반드시 Home으로 이동 (완성품 사용자 수령 필요)
    """
    if "Done" not in msg:
        return

    if SM.amr_state == "AT_R2":
        send_to("AMR_ARD", "AMR_ARD:CON:STOP")
        log("SM", f"R2 수거 완료 ({len(SM.amr_output_orders)}체) → Home 이동")
        SM.amr_state = "TO_HOME"
        send_amr("goal: Home")

    _derive_flags()
    _tick()


def _handle_internal(msg: str):
    """
    내부 이벤트 처리.

    TICK          : heartbeat 주기 이벤트 → 플래그 재파생 + 명령 재판단
    MATERIAL_DONE : 자재 투입 완료 → Sort 명령 + AMR 스케줄 갱신
    """
    if msg == "TICK":
        # 하트비트 주기: PLC raw값 갱신됨 → 플래그 재파생 → 명령 재판단
        _derive_flags()
        _tick()

    elif msg == "MATERIAL_DONE":
        # AMR이 R01Insert에서 자재 투입 완료
        log("SM", "자재 투입 완료 (MATERIAL_DONE)")
        SM.amr_carrying_input = 0
        SM.r1_needs_material  = False
        SM.amr_state          = "IDLE"   # 컨베이어 비었으므로 바로 이동 가능

        # Sort = 물리 자재 투입 후에만 가능 (핵심 규칙)
        _action_sort()

        _derive_flags()
        _tick()   # AMR 다음 목적지 결정 (output 대기 있으면 R02Output)


def _handle_terminal(cmd: str):
    """
    터미널 입력 처리. 내용 기반 라우팅 (AMR 상태와 무관).

    서버는 통합 관리자 → AMR 상태에 끌려다니지 않음.
    주문은 항상 수락, Home 명령은 Home 상황일 때만 유효.

      recv / load N  → AMR이 Home에 있을 때만 처리
      x,y,z,W,W,W,W → 주문 큐에 추가 (항상 수락)
    """
    c = cmd.lower().strip()

    # Home 작업 명령
    if c == "recv" or c.startswith("load"):
        if SM.home_waiting:
            _handle_home_input(cmd)
        else:
            print("[무시] AMR이 Home에 없음")
        return

    # 주문 입력 (AMR 상태 무관, 항상 수락)
    order_id = _parse_and_create_order(cmd)
    if order_id:
        SM.order_queue.append(order_id)
        log("SM", f"주문 #{order_id} 대기열 추가 (총 {len(SM.order_queue)}건)")
        if SM.r1_order is None:
            _try_start_next_order()
            _derive_flags()
            _tick()


def _handle_home_input(cmd: str):
    """
    AMR Home 대기 중 사용자 입력 처리.
      recv   : 완성품 수령 완료
      load N : 자재 N세트 AMR에 적재 완료
    """
    c = cmd.lower().strip()

    if c == "recv":
        if SM.home_recv_done:
            print("  [이미 수령 완료]")
            return
        for oid in SM.amr_output_orders:
            db.update_order_status(oid, "완료")
            log("SM", f"★ 주문 #{oid} 완료")
        SM.amr_output_orders = []
        SM.home_recv_done    = True
        db.print_orders()
        print("  [수령 완료]")
        _check_home_complete()

    elif c.startswith("load"):
        if SM.home_load_done:
            print("  [이미 적재 완료]")
            return
        parts = c.split()
        try:    n = int(parts[1]) if len(parts) > 1 else 0
        except: n = 0
        SM.amr_carrying_input = n
        SM.home_load_done     = True
        log("SM", f"자재 {n}세트 AMR 적재")
        print(f"  [적재 완료: {n}세트]")
        _check_home_complete()

    else:
        if not SM.home_recv_done and SM.amr_output_orders:
            print(f"  ▶ 완성품 내린 후 'recv' 입력")
        if not SM.home_load_done and SM.r1_needs_material:
            print(f"  ▶ 자재 올린 후 'load N' 입력")


# ══════════════════════════════════════════════════════════════════════
# SECTION 5: Home 작업 관리
# ══════════════════════════════════════════════════════════════════════

def _prompt_home_user():
    """AMR Home 도착 시 사용자에게 해야 할 작업 출력."""
    SM.home_waiting   = True
    SM.home_recv_done = (len(SM.amr_output_orders) == 0)   # 수거할 완성품 없으면 완료 처리
    SM.home_load_done = not SM.r1_needs_material             # 자재 불필요하면 완료 처리

    print("\n" + "=" * 48)
    print("[AMR Home 도착]")
    if SM.amr_output_orders:
        print(f"  ▶ 완성품 {len(SM.amr_output_orders)}체 수령 요청")
        print(f"    주문번호: {SM.amr_output_orders}")
        print(f"    완료 후 → 'recv' 입력")
    if SM.r1_needs_material:
        print(f"  ▶ 자재 1세트 적재 요청 (AMR 컨베이어에 올려주세요)")
        print(f"    완료 후 → 'load N' 입력 (N = 세트 수)")
    print("=" * 48 + "\n")

    # 처리 항목 없으면 즉시 완료
    if SM.home_recv_done and SM.home_load_done:
        log("SM", "Home: 처리 항목 없음 → 즉시 완료")
        _after_home_complete()


def _check_home_complete():
    """recv + load 모두 완료됐으면 Home 작업 종료."""
    if SM.home_recv_done and SM.home_load_done:
        _after_home_complete()
    elif not SM.home_recv_done:
        print("  ▶ 남은 작업: 완성품 수령 후 'recv'")
    else:
        print("  ▶ 남은 작업: 자재 적재 후 'load N'")


def _after_home_complete():
    """Home 작업 완료 → AMR IDLE 복귀 + 다음 스케줄 결정."""
    SM.home_waiting   = False
    SM.home_recv_done = False
    SM.home_load_done = False
    SM.amr_state      = "IDLE"
    log("SM", "Home 작업 완료 → AMR 스케줄 재결정")
    _derive_flags()
    _tick()


# ══════════════════════════════════════════════════════════════════════
# SECTION 6: 액션 함수 (하드웨어 명령 송신)
#
# _tick 또는 핸들러에서 호출.
# 실제 기기에 명령 전송 + SM 상태 갱신.
# ══════════════════════════════════════════════════════════════════════

def _action_fire_stack():
    """
    R1 Stack 명령 송신.

    [RAPID 담당자]
    R01:{id}:Stack:{6자색상} 수신 시 적재 동작 시작.
    색상코드 = base(1자) + wall1(1자) + wall2(1자) + wall3(1자) + wall4(1자) + ceil(1자)
    예) RWBYWR  →  R=베이스, W/B/Y/W=벽4개, R=천장
    """
    if SM.r1_order is None:
        log("SM", "[오류] _action_fire_stack: r1_order 없음")
        return
    order = db.get_order(SM.r1_order)
    if not order:
        log("SM", f"[오류] 주문 #{SM.r1_order} DB 조회 실패")
        return

    color_str = (
        order["base_color"][0].upper() +
        order["wall1_color"]           +
        order["wall2_color"]           +
        order["wall3_color"]           +
        order["wall4_color"]           +
        order["ceil_color"][0].upper()
    )
    SM.r1_state = "STACKING"
    db.update_order_status(SM.r1_order, "적재중")
    send_to("R01", f"R01:{SM.r1_order}:Stack:{color_str}")
    _deduct_stack_inventory(order)   # 명령 전송 즉시 재고 차감
    log("SM", f"Stack 명령 | 주문 #{SM.r1_order} | 색상: {color_str}")
    db.print_inventory()


def _action_sort():
    """
    R1 Sort 명령 송신.

    [RAPID 담당자]
    'Sort' 수신 시 분류 동작 시작. 패널 1세트(6개) 처리.

    ★ 주의: 반드시 AMR이 자재를 R01Insert에 투입한 후 호출해야 함.
       물리 자재 없이 Sort 명령만으로는 의미 없음.
       MATERIAL_DONE 내부 이벤트 수신 후에만 이 함수 호출.
    """
    if SM.r1_state != "IDLE":
        log("SM", f"[무시] Sort 시도 — R1 현재: {SM.r1_state}")
        return
    SM.r1_state        = "SORTING"
    SM.sort_colors_buf = []
    SM.waiting_color   = False
    send_to("R01", "Sort")
    log("SM", "Sort 명령 송신")


def _action_retract():
    """
    ReadyRetract 수신 후 2초 딜레이 → 후진 명령 → RETRACT_DURATION 후 M130 해제.

    서브스레드로 실행하여 state_machine_loop 블로킹 없음.
    흐름:
      2s 대기 → M130=1 (후진 명령) → RETRACT_DURATION 대기 → M130=0 (해제)
      → SM.retracting=False → 다음 폴링에서 trans_bwd_sen==1 확인 → platform_clear
    """
    if SM.retracting:
        log("SM", "[무시] 이미 후진 중")
        return
    SM.retracting = True

    def _do():
        time.sleep(2.0)
        plc_write("P01", "trans_bwd_cmd", 1)
        log("SM", "이송실린더 후진 명령 (PLC M130=1)")
        time.sleep(RETRACT_DURATION)
        plc_write("P01", "trans_bwd_cmd", 0)
        SM.retracting = False
        log("RETRACT", "후진 완료, M130=0 해제")

    threading.Thread(target=_do, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════
# SECTION 7: 비즈니스 로직 헬퍼
# ══════════════════════════════════════════════════════════════════════

def _try_start_next_order():
    """
    order_queue에서 다음 주문을 꺼내 R1 작업 결정.

    can_fulfill = True  : 재고 충족 → r1_stack_ready=True (Sort 없이 Stack 대기)
    can_fulfill = False : 재고 부족 → r1_needs_material=True
                          → _tick_amr → AMR Home → 사용자 적재 → R01Insert
                          → MATERIAL_DONE → Sort → can_fulfill 재확인 → Stack

    Sort는 자재 투입(MATERIAL_DONE) 후에만 발생. 여기서 직접 Sort 안 함.
    """
    if SM.r1_order is not None or not SM.order_queue:
        return

    SM.r1_order = SM.order_queue.pop(0)
    log("SM", f"다음 주문 #{SM.r1_order} 처리 시작")

    if db.can_fulfill_order(SM.r1_order):
        log("SM", "  → 재고 충족: Stack 대기 (platform 확인 후 발사)")
        SM.r1_stack_ready = True
    else:
        log("SM", "  → 재고 부족: AMR 자재 투입 요청")
        SM.r1_needs_material = True


def _apply_sort_inventory(colors: list):
    """Sort 완료 후 분류된 패널 재고 반영 (+1)."""
    db.adjust_stock("BpC", 1)
    db.adjust_stock("CpC", 1)
    for c in colors:
        wpc = db.WPC_MAP.get(c)
        if wpc:
            db.adjust_stock(wpc, 1)
            log("INV", f"+1: {wpc} ({c})")
        else:
            log("INV", f"[경고] 알 수 없는 색상: {c}")


def _deduct_stack_inventory(order: dict):
    """Stack 명령 전송 직후 사용된 재고 차감 (-1)."""
    db.adjust_stock("BpC", -1)
    db.adjust_stock("CpC", -1)
    for k in ["wall1_color", "wall2_color", "wall3_color", "wall4_color"]:
        wpc = db.WPC_MAP.get(order.get(k, ""))
        if wpc:
            db.adjust_stock(wpc, -1)
            log("INV", f"-1: {wpc}")


# ══════════════════════════════════════════════════════════════════════
# SECTION 8: 자재 투입 (Placeholder)
#
# AMR 물리 도킹 미구현 → Jin이 직접 테스트하며 구현
# 완료 시 반드시 message_queue.put(("_INT", "MATERIAL_DONE")) 호출
# ══════════════════════════════════════════════════════════════════════

def _material_loading_thread():
    """
    [Placeholder] 자재 투입 시퀀스.

    AMR AT_R1 도착 시 sub-thread로 실행.
    완료 시 ("_INT", "MATERIAL_DONE") 이벤트 발행 → _handle_internal에서 Sort 명령.

    TODO: PLC 차단기 제어, AMR_ARD 통신 시퀀스 Jin이 구현.
    아래는 구조 참고용 예시.
    """
    panel_count = 6  # 천장 1 + 벽 4 + 기초 1

    # ── 아래는 예시 구조 (실제 구현 시 교체) ────────────────────────
    # plc_write("P01", "input_count", panel_count)
    # plc_write("P01", "conv_on", 1)
    # plc_poll_until("P01", "input_sensor", target=1, timeout=30)
    # plc_write("P01", "conv_on", 0)
    # send_to("AMR_ARD", "AMR_ARD:CON:START")
    # SM.amr_ard_done_event.wait(timeout=15)
    # send_to("AMR_ARD", "AMR_ARD:CON:STOP")
    # for i in range(1, panel_count):
    #     gate = (i % 2) + 1
    #     send_to("AMR_ARD", f"AMR_ARD:GATE:{gate}:OPEN")
    #     ...
    # ────────────────────────────────────────────────────────────────

    log("LOAD", f"[Placeholder] 자재 투입 완료 ({panel_count}개)")
    message_queue.put(("_INT", "MATERIAL_DONE"))


# ══════════════════════════════════════════════════════════════════════
# SECTION 9: 인프라 (소켓 / 스레드)
# ══════════════════════════════════════════════════════════════════════

def accept_loop():
    """[인프라] TCP 연결 수락 루프 (daemon)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((SERVER_HOST, SERVER_PORT))
    srv.listen(10)
    log("ACCEPT", f"TCP 포트 {SERVER_PORT} 대기")
    while True:
        try:
            conn, addr = srv.accept()
            name = IP_MAP.get(addr[0])
            if not name:
                log("ACCEPT", f"[거부] 미등록 IP: {addr[0]}")
                conn.close()
                continue
            with clients_lock:
                if name in clients:
                    try: clients[name].close()
                    except: pass
                clients[name] = conn
            log("ACCEPT", f"[연결] {name} ({addr[0]})")
            threading.Thread(target=recv_loop, args=(name, conn),
                             name=f"recv_{name}", daemon=True).start()
        except Exception as e:
            log("ACCEPT", f"[오류] {e}")


def recv_loop(name: str, sock: socket.socket):
    """[인프라] TCP 수신 루프 (daemon, 기기당 1개)."""
    buf = ""
    try:
        while True:
            data = sock.recv(1024)
            if not data:
                break
            buf += data.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                log("RECV", f"← {name}: {line}")
                if not _is_status_response(name, line):
                    message_queue.put((name, line))
    except Exception as e:
        log("RECV", f"[오류] {name}: {e}")
    finally:
        with clients_lock:
            if clients.get(name) is sock:
                del clients[name]
        log("RECV", f"[끊김] {name}")
        message_queue.put((name, "DISCONNECTED"))


def _is_status_response(name: str, msg: str) -> bool:
    """
    하트비트 응답 메시지 여부 확인.
    True이면 message_queue에 투입 안 함.

    동시에 로봇 자체 보고 상태(SM.r1/r2_state_actual)를 갱신.
    recv_loop 스레드에서 호출되지만 str 단순 대입이므로 GIL로 안전.

    [RAPID 담당자]
    하트비트("R1_status") 수신 시 로봇은 현재 RAPID 태스크 상태로 응답해야 함:
      R01:Ready    → 대기 중 (IDLE)
      R01:Sorting  → 분류 태스크 실행 중
      R01:Stacking → 적재 태스크 실행 중
    서버는 이 값과 자체 추적 상태(SM.r1_state)가 일치해야 다음 명령 전송.
    """
    # R01 상태 정규화 매핑
    R01_STATUS = {
        "R01:Ready":    "IDLE",
        "R01:Sorting":  "SORTING",
        "R01:Stacking": "STACKING",
    }
    # R02 상태 정규화 매핑
    R02_STATUS = {
        "R02:Ready":       "IDLE",
        "R02:Assemblying": "ASSEMBLING",
    }

    if name == "R01":
        for key, state in R01_STATUS.items():
            if msg.startswith(key):
                SM.r1_state_actual = state   # 교차확인용 실제 상태 갱신
                return True

    elif name == "R02":
        for key, state in R02_STATUS.items():
            if msg.startswith(key):
                SM.r2_state_actual = state   # 교차확인용 실제 상태 갱신
                return True

    elif name == "AMR_ARD":
        return any(msg.startswith(k) for k in
                   ["AMR_ARD:Ready", "AMR_ARD:Input", "AMR_ARD:Output"])

    return False


def amr_loop():
    """[인프라] AMR ARCL 연결 루프 (자동 재접속, daemon)."""
    global amr_sock
    log("AMR", f"연결 시도 ({AMR_HOST}:{AMR_PORT})")
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((AMR_HOST, AMR_PORT))
            with amr_sock_lock:
                amr_sock = sock
            log("AMR", "연결 성공")
            buf = ""
            while True:
                data = sock.recv(1024)
                if not data:
                    break
                buf += data.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        log("AMR", f"← AMR: {line}")
                        message_queue.put(("AMR", line))
        except Exception as e:
            log("AMR", f"[오류] {e}")
        finally:
            with amr_sock_lock:
                amr_sock = None
            time.sleep(5)


def terminal_loop():
    """[인프라] 표준입력 읽기 루프 (daemon)."""
    while True:
        try:
            terminal_queue.put(input().strip())
        except EOFError:
            break


def state_machine_loop():
    """
    [인프라] 메인 이벤트 루프 (non-daemon, 메인 컨트롤러).

    모든 _derive_flags, _tick, 핸들러 호출은 이 스레드에서만 발생.
    SM 변수는 이 스레드 전용 → Lock 불필요.

    처리 우선순위:
      ① 터미널 입력 (non-blocking)
      ② 기기 메시지 / 내부 이벤트 (50ms 타임아웃)
    """
    _do_init()
    log("SM", "이벤트 루프 시작")
    while True:
        # ① 터미널 입력 (non-blocking)
        try:
            cmd = terminal_queue.get_nowait()
            _handle_terminal(cmd)
        except queue.Empty:
            pass

        # ② 기기 메시지 / TICK 이벤트 (50ms 대기)
        try:
            sender, msg = message_queue.get(timeout=0.05)
            _dispatch(sender, msg)
        except queue.Empty:
            pass


# ══════════════════════════════════════════════════════════════════════
# SECTION 10: 초기화
# ══════════════════════════════════════════════════════════════════════

def _do_init():
    log("INIT", "=== 초기화 시작 ===")
    print("\n초기 재고 입력 (엔터 스킵 = DB 기존 값 유지)")
    print("형식: BpC=3 CpC=3 WpC1=2 WpC2=2 WpC3=2 WpC4=0 WpC5=0 WpC6=0\n")
    try:
        line = terminal_queue.get(timeout=60)
        if line:
            _parse_and_set_inventory(line)
    except queue.Empty:
        log("INIT", "스킵 — 기존 DB 값 유지")
    db.print_inventory()

    log("INIT", "R01 접속 대기 (최대 60s)...")
    deadline = time.time() + 60
    while time.time() < deadline:
        with clients_lock:
            if "R01" in clients:
                break
        time.sleep(0.5)
    else:
        log("INIT", "[경고] R01 미접속 — 동기화 스킵")
        return

    _sync_inventory_to_r01()
    log("INIT", "=== 초기화 완료 ===")
    print("\n주문 입력: x,y,z,W1,W2,W3,W4  예) 1,1,1,W,Y,B,W\n")


def _sync_inventory_to_r01():
    """현재 재고를 R01에 전송 → R01 PERS 변수 초기화."""
    inv  = db.get_inventory()
    keys = ["BpC", "CpC", "WpC1", "WpC2", "WpC3", "WpC4", "WpC5", "WpC6"]
    msg  = ";".join(f"{k}:{inv.get(k, 0)}" for k in keys)
    for attempt in range(1, 4):
        send_to("R01", msg)
        deadline  = time.time() + 5.0
        confirmed = False
        buf       = []
        while time.time() < deadline:
            try:
                item = message_queue.get(timeout=0.1)
                s, m = item
                if s == "R01" and (msg in m or m.strip() == "OK"):
                    confirmed = True
                    break
                else:
                    buf.append(item)
            except queue.Empty:
                continue
        for b in buf:
            message_queue.put(b)
        if confirmed:
            log("INIT", "R01 재고 동기화 완료")
            return
        log("INIT", f"복명복창 미확인 ({attempt}/3)")
    log("INIT", "[경고] 동기화 확인 실패 — 계속 진행")


def _parse_and_set_inventory(line: str):
    for part in line.strip().split():
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                db.set_stock(k.strip(), int(v.strip()))
                log("INIT", f"재고 설정: {k.strip()}={v.strip()}")
            except ValueError:
                pass


def _parse_and_create_order(cmd: str):
    parts = cmd.strip().split(",")
    if len(parts) != 7:
        print(f"[오류] 형식: x,y,z,W1,W2,W3,W4  입력: '{cmd}'")
        return None
    try:
        x, y, z = int(parts[0]), int(parts[1]), int(parts[2])
        w1, w2, w3, w4 = (p.strip().upper() for p in parts[3:])
        return db.create_order(x, y, z, w1, w2, w3, w4)
    except Exception as e:
        print(f"[오류] 파싱 실패: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════
# 진입점
# ══════════════════════════════════════════════════════════════════════

def main():
    log("MAIN", "MES 게이트웨이 서버 시작")
    db.init_db()
    threads = [
        threading.Thread(target=accept_loop,       name="accept",        daemon=True),
        threading.Thread(target=heartbeat_loop,     name="heartbeat",     daemon=True),
        threading.Thread(target=amr_loop,           name="amr",           daemon=True),
        threading.Thread(target=terminal_loop,      name="terminal",      daemon=True),
        threading.Thread(target=state_machine_loop, name="state_machine"),  # non-daemon
    ]
    for t in threads:
        t.start()
        log("MAIN", f"스레드 시작: {t.name}")
    threads[-1].join()
    log("MAIN", "서버 종료")


if __name__ == "__main__":
    main()
