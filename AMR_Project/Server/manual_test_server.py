import socket
import threading
import time

# ── 설정 ──────────────────────────────────────────
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 9090

AMR_HOST = "192.168.3.11"
AMR_PORT = 7171
AMR_PASSWORD = "1234"

PLC_FRAME_SIZE = 20

# ── 소켓 상태 ──────────────────────────────────────
_sockets = {}
_lock = threading.Lock()


def log(tag, msg):
    print(f"[{time.strftime('%H:%M:%S')}][{tag}] {msg}")


def send_raw(device, data: bytes):
    with _lock:
        sock = _sockets.get(device)
    if not sock:
        log("오류", f"{device} 미접속")
        return
    try:
        sock.sendall(data)
        log("SEND", f"→ {device}: {data!r}")
    except Exception as e:
        log("오류", f"{device} 전송 실패: {e}")


# ── 수신 스레드 ────────────────────────────────────
def recv_loop(device, sock):
    buf = ""
    try:
        while True:
            data = sock.recv(1024)
            if not data:
                break
            if device == "PLC2":
                log("RECV", f"← {device}: {data!r}")
            else:
                buf += data.decode("utf-8", errors="ignore")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        log("RECV", f"← {device}: {line}")
    except Exception as e:
        log("오류", f"{device} 수신 오류: {e}")
    finally:
        with _lock:
            _sockets.pop(device, None)
        log("종료", f"{device} 연결 끊김")


# ── TCP 서버 (PLC2, AMR_ARD 접속 대기) ────────────
DEVICE_BY_IP = {
    "192.168.3.40": "PLC2",
    "192.168.3.23": "AMR_ARD",
    "192.168.3.22": "R2_ARD",
}

def accept_loop(server):
    while True:
        conn, addr = server.accept()
        ip = addr[0]
        device = DEVICE_BY_IP.get(ip, ip)
        with _lock:
            _sockets[device] = conn
        log("접속", f"{device} ({ip})")
        threading.Thread(target=recv_loop, args=(device, conn), daemon=True).start()


server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((SERVER_HOST, SERVER_PORT))
server.listen(5)
log("서버", f"포트 {SERVER_PORT} 대기 중 (PLC2, AMR_ARD 접속 대기)")
threading.Thread(target=accept_loop, args=(server,), daemon=True).start()


# ── AMR ARCL 접속 ──────────────────────────────────
def connect_amr():
    while True:
        try:
            log("AMR", f"ARCL 접속 시도 → {AMR_HOST}:{AMR_PORT}")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((AMR_HOST, AMR_PORT))
            with _lock:
                _sockets["AMR"] = sock
            log("AMR", "ARCL 접속 완료")
            recv_loop("AMR", sock)
        except Exception as e:
            log("AMR", f"접속 실패: {e}")
        time.sleep(3)

threading.Thread(target=connect_amr, daemon=True).start()


# ── 콘솔 ──────────────────────────────────────────
print("\n명령어:")
print("  ard <메시지>        → AMR_ARD 전송 (\\n 자동)")
print("  r2ard <메시지>      → R2_ARD 전송 (\\n 자동)")
print("  amr <명령>          → AMR ARCL 전송 (\\r\\n 자동)")
print("  plc <메시지>        → PLC2 전송 (20바이트 패딩)")
print("  status              → 접속 현황")
print("  q                   → 종료\n")

while True:
    try:
        cmd = input("> ").strip()
    except EOFError:
        break

    if not cmd:
        continue

    if cmd == "q":
        break

    elif cmd == "status":
        with _lock:
            connected = list(_sockets.keys())
        if connected:
            print("  접속 중:", ", ".join(connected))
        else:
            print("  접속된 장비 없음")

    elif cmd.startswith("ard "):
        msg = cmd[4:]
        send_raw("AMR_ARD", (msg + "\n").encode())

    elif cmd.startswith("r2ard "):
        msg = cmd[6:]
        send_raw("R2_ARD", (msg + "\n").encode())

    elif cmd.startswith("amr "):
        msg = cmd[4:]
        send_raw("AMR", (msg + "\r\n").encode())

    elif cmd.startswith("plc "):
        msg = cmd[4:]
        padded = msg.ljust(PLC_FRAME_SIZE)[:PLC_FRAME_SIZE]
        send_raw("PLC2", padded.encode())

    else:
        print("  알 수 없는 명령 (help: ard / amr / plc / status / q)")
