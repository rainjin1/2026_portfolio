import socket
import threading
import pymcprotocol

# ── 설정 ──────────────────────────────────────────
ROBOT_PORT = 9000

ROBOT_MAP = {
    "R01": "192.168.3.2",
    "R02": "192.168.3.3",
}

PLC_MAP = {
    "P01": ("192.168.3.39", 3900),
    "P02": ("192.168.3.40", 3900),
}

IP_TO_ROBOT = {v: k for k, v in ROBOT_MAP.items()}

# ── 상태 ──────────────────────────────────────────
robots = {}
robots_lock = threading.Lock()

robot_states = {
    "R01": "Unknown",
    "R02": "Unknown",
}

# ── 로봇 수신 스레드 ───────────────────────────────
def recv_loop(label, conn):
    while True:
        try:
            data = conn.recv(1024)
            if not data:
                print(f"[{label}] 연결 끊김")
                with robots_lock:
                    robots.pop(label, None)
                break
            msg = data.decode().strip()
            print(f"[{label}] {msg}")

            if ":" in msg:
                r_id, state = msg.split(":", 1)
                if r_id in robot_states:
                    robot_states[r_id] = state
        except:
            break

def handle_client(conn, addr):
    ip = addr[0]
    label = IP_TO_ROBOT.get(ip, ip)
    with robots_lock:
        robots[label] = conn
    print(f"[접속] {label} ({ip})")
    recv_loop(label, conn)

# ── PLC 쓰기 ──────────────────────────────────────
def plc_write(plc_id, device, value):
    if plc_id not in PLC_MAP:
        print(f"[오류] 알 수 없는 PLC: {plc_id}")
        return
    ip, port = PLC_MAP[plc_id]
    plc = pymcprotocol.Type3E()
    try:
        plc.connect(ip, port)
        if device.upper().startswith("D"):
            plc.batchwrite_wordunits(headdevice=device.upper(), values=[int(value)])
        elif device.upper().startswith("M"):
            plc.batchwrite_bitunits(headdevice=device.upper(), values=[int(value)])
        elif device.upper().startswith("Y"):
            plc.batchwrite_bitunits(headdevice=device.upper(), values=[int(value)])
        elif device.upper().startswith("X"):
            plc.batchwrite_bitunits(headdevice=device.upper(), values=[int(value)])
        else:
            print(f"[오류] 지원하지 않는 디바이스: {device}")
            return
        print(f"[{plc_id}] {device.upper()} = {value} 완료")
    except Exception as e:
        print(f"[오류] PLC 쓰기 실패: {e}")
    finally:
        plc.close()

# ── 서버 시작 ─────────────────────────────────────
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("0.0.0.0", ROBOT_PORT))
server.listen(5)
print(f"[서버] 포트 {ROBOT_PORT} 대기 중...")

def accept_loop():
    while True:
        conn, addr = server.accept()
        threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()

threading.Thread(target=accept_loop, daemon=True).start()

# ── 터미널 입력 ───────────────────────────────────
print("명령어: R01:메시지 / P01:D100,1 / P01:M100,1 / status / quit")
while True:
    cmd = input("> ").strip()

    if cmd == "quit":
        break

    elif cmd == "status":
        for r, s in robot_states.items():
            print(f"  {r}: {s}")
        continue

    elif ":" not in cmd:
        print("[오류] 형식: R01:메시지 또는 P01:D100,1")
        continue

    target, data = cmd.split(":", 1)

    if target.startswith("R"):
        with robots_lock:
            conn = robots.get(target)
        if not conn:
            print(f"[오류] {target} 미접속")
            continue
        try:
            conn.sendall(data.encode())
            print(f"[{target}] 전송: {data}")
        except Exception as e:
            print(f"[오류] 전송 실패: {e}")

    elif target.startswith("P"):
        if "," not in data:
            print("[오류] 형식: P01:D100,1")
            continue
        device, value = data.split(",", 1)
        plc_write(target, device.strip(), value.strip())