"""
command_center_test.py
P1 단독 테스트용 — 다른 장비는 콘솔 명령으로 수동 시뮬레이션.

실행: python command_center_test.py
- accept_thread만 기동 (PLC1 실제 접속 대기)
- amr_thread 미기동
- 콘솔에서 다른 장비 메시지를 직접 주입
"""

import threading
import random
import db
import command_center_20260730 as cc


# ── 주입 헬퍼 ─────────────────────────────────────────────────────────

def _inject(device: str, msg: str):
    cc.message_queue.put((device, msg))
    print(f"  [INJ] → {device}: {msg!r}")


def _plc_frame(msg_type: str, cmd_code: str, payload: str = "") -> str:
    """PLC 고정 프레임 20바이트 생성 (실제 plc_recv_thread가 받는 형식과 동일)."""
    header = f"{msg_type[:2]:<2}{cmd_code[:2]:<2}"
    body   = f"{payload[:cc.PLC_PAYLOAD_SIZE]:<{cc.PLC_PAYLOAD_SIZE}}"
    return header + body


# ── 상태 출력 ─────────────────────────────────────────────────────────

def print_sm():
    s = cc.SM
    print("\n── SM 상태 ──────────────────────────────────────────────")
    print(f"  r1_state              : {s.r1_state}")
    print(f"  r2_state              : {s.r2_state}")
    print(f"  amr_state             : {s.amr_state}")
    print(f"  p1_state              : {s.p1_state}")
    print(f"  p1_ready_input        : {s.p1_ready_input}")
    print(f"  station_input         : {s.station_input}")
    print(f"  consol_input          : {s.consol_input}")
    print(f"  Sort_Available        : {s.Sort_Available}")
    print(f"  Stack_Available       : {s.Stack_Available}")
    print(f"  Assembly_Available    : {s.Assembly_Available}")
    print(f"  Transfer_to_Transfer_Available : {s.Transfer_to_Transfer_Available}")
    print(f"  inspection_state      : {s.inspection_state}")
    print(f"  inspection_face       : {s.inspection_face}")
    print(f"  inspection_last_result: {s.inspection_last_result}")
    print(f"  p2_rotation_ready     : {s.p2_rotation_ready}")
    print(f"  p2_inspecting         : {s.p2_inspecting}")
    print(f"  p2_transferring       : {s.p2_transferring}")
    print(f"  station_assembly      : {s.station_assembly}")
    print(f"  station_output        : {s.station_output}")
    print(f"  amr_ard_recv_pending  : {s.amr_ard_recv_pending}")
    print(f"  amr_ard_complete      : {s.amr_ard_complete}")
    print()


def print_wq():
    snap = cc.wq_snapshot()
    print("\n── 작업큐 스냅샷 ────────────────────────────────────────")
    print(f"  pending   : {snap['pending']}")
    print(f"  stages    : {snap['stages']}")
    print(f"  active    : {snap['active']}")
    print(f"  completed : {snap['completed']}")
    print()


def print_inv():
    inv = db.get_inventory()
    print("\n── 재고 현황 ────────────────────────────────────────────")
    for item, cnt in inv.items():
        print(f"  {item:6s}: {cnt}")
    print()


# ── 도움말 ────────────────────────────────────────────────────────────

HELP = """
── 테스트 명령어 ──────────────────────────────────────────────────────
  장비 메시지 주입:
    r1 <msg>          예) r1 SortDone  /  r1 StackDone
    r2 <msg>          예) r2 AssemblyDone  /  r2 TransferToInspectionDone
    amr <msg>         예) amr Arrived at R1Input
    amr_ard <msg>     예) amr_ard CountOK  /  amr_ard 받음  /  amr_ard 완료
    raspi <msg>       예) raspi W  (색상코드: W/Y/B/D/N/R)
    r2_ard <msg>      예) r2_ard O  /  r2_ard X
    p1 <코드> [payload]  예) p1 02  /  p1 07
    p2 <코드> [payload]  예) p2 01

  주문:
    order <w1w2w3w4>  벽 색상 4자리로 주문 생성+큐 진입
                      예) order WWWW  (base/ceil 자동 R)
    order <bw1w2w3w4c> 6자리: base + 벽×4 + ceil
                      예) order RWWWWR

  강제 진입 (단계 스킵):
    goto_input <n>    AMR→AT_R1INPUT 강제 진입 + consol_input=n 설정 후 decide()
                      예) goto_input 6  (RWWWWR 6자재)
                      이후 PLC1 "02" × n 신호 대기 (실제 장비 또는 p1 02 주입)

  서버→장비 직접 송신:
    send_p1 <코드> [payload]  서버가 PLC1에게 직접 전송
                              예) send_p1 01  /  send_p1 03
    send_p2 <코드> [payload]  서버가 PLC2에게 직접 전송
                              예) send_p2 02
    send_amr <cmd>            서버가 AMR에게 ARCL 명령 직접 전송
                              예) send_amr doTask 자재요청  /  send_amr goto 박대기

  상태 확인:
    sm     SM 전체 상태 출력
    wq     작업큐 스냅샷 출력
    inv    재고 현황 출력
    conn   장비 연결 상태 확인
    help   이 도움말
    q      종료
──────────────────────────────────────────────────────────────────────
"""


# ── 테스트 콘솔 스레드 ────────────────────────────────────────────────

def test_console_thread():
    print(HELP)
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue

        parts = line.split(" ", 1)
        cmd   = parts[0].lower()
        arg   = parts[1] if len(parts) > 1 else ""

        if cmd == "q":
            break

        elif cmd == "sm":
            print_sm()

        elif cmd == "conn":
            print("\n── 연결 상태 ────────────────────────────────────────")
            for dev, status in cc.SM.connected.items():
                mark = "✓" if status else "✗"
                print(f"  {mark} {dev}")
            print()

        elif cmd == "wq":
            print_wq()

        elif cmd == "inv":
            print_inv()

        elif cmd == "help":
            print(HELP)

        elif cmd in ("send_p1", "send_p2"):
            sub     = arg.split(" ", 1)
            code    = sub[0] if sub else ""
            payload = sub[1] if len(sub) > 1 else ""
            device  = "PLC1" if cmd == "send_p1" else "PLC2"
            mtype   = "P1"   if cmd == "send_p1" else "P2"
            cc.plc_send_to(device, mtype, code, payload)
            print(f"  [SEND] → {device}: type='{mtype}' cmd='{code}' payload='{payload}'")

        elif cmd == "send_amr":
            if not arg:
                print("  오류: 명령 필요  예) send_amr doTask 자재요청")
            elif not cc.SM.connected.get("AMR"):
                print("  [오류] AMR 미연결 — amr_thread 기동 중인지 확인")
            else:
                cc.amr_send(arg)
                print(f"  [SEND] → AMR: {arg!r}")

        elif cmd == "goto_input":
            try:
                n = int(arg) if arg else 6
                cc.SM.amr_state      = "AT_R1INPUT"
                cc.SM.consol_input   = n
                cc.SM.p1_ready_input = True
                print(f"  [FORCE] amr_state=AT_R1INPUT  consol_input={n}")
                cc.decide()
            except ValueError:
                print("  오류: 숫자 입력 필요  예) goto_input 6")

        elif cmd in ("r1", "r2", "amr", "amr_ard", "raspi", "r2_ard"):
            device_map = {
                "r1":      "R1",
                "r2":      "R2",
                "amr":     "AMR",
                "amr_ard": "AMR_ARD",
                "raspi":   "RASPI",
                "r2_ard":  "R2_ARD",
            }
            _inject(device_map[cmd], arg)

        elif cmd in ("p1", "p2"):
            # arg 예시: "02" 또는 "02 payload"
            sub = arg.split(" ", 1)
            code    = sub[0] if sub else ""
            payload = sub[1] if len(sub) > 1 else ""
            msg_type = "P1" if cmd == "p1" else "P2"
            device   = "PLC1" if cmd == "p1" else "PLC2"
            frame = _plc_frame(msg_type, code, payload)
            _inject(device, frame)

        elif cmd == "order":
            colors = arg.strip()
            if len(colors) == 4:
                # 벽 4자리, base/ceil = "R"
                w1, w2, w3, w4 = colors
                oid = db.create_order(
                    random.randint(1, 99), random.randint(1, 99), 1,
                    w1, w2, w3, w4,
                    base_color="R", ceil_color="R"
                )
            elif len(colors) == 6:
                # base + 벽4 + ceil
                oid = db.create_order(
                    random.randint(1, 99), random.randint(1, 99), 1,
                    colors[1], colors[2], colors[3], colors[4],
                    base_color=colors[0], ceil_color=colors[5]
                )
            else:
                print("  오류: 색상 4자리(벽만) 또는 6자리(base+벽4+ceil) 입력 필요")
                continue

            if oid:
                cc.wq_enqueue(oid)
                cc.decide()
                print(f"  [ORDER] 주문 #{oid} 생성 → 큐 진입")
            else:
                print("  [ORDER] 생성 실패 (좌표 중복 등)")

        else:
            print(f"  알 수 없는 명령: {cmd!r}  (help 참조)")


# ── 진입점 ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()

    threading.Thread(target=cc.accept_thread, daemon=True).start()
    threading.Thread(target=cc.amr_thread,   daemon=True).start()
    threading.Thread(target=test_console_thread, daemon=True).start()

    cc.log("TEST", "테스트 모드 시작 (AMR 실제 연결 포함)")
    cc.main_loop()
