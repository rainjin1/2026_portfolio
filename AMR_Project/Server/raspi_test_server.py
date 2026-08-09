import socket
import threading
import time

HOST = "0.0.0.0"
PORT = 9090

raspi_sock = None
raspi_lock = threading.Lock()


def recv_loop(conn, addr):
    global raspi_sock
    print(f"[접속] RASPI {addr}")
    buf = ""
    try:
        while True:
            data = conn.recv(1024)
            if not data:
                break
            buf += data.decode("utf-8", errors="ignore")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if line:
                    print(f"[{time.strftime('%H:%M:%S')}][RECV] ← RASPI: {line}")
    except Exception as e:
        print(f"[오류] {e}")
    finally:
        with raspi_lock:
            raspi_sock = None
        conn.close()
        print(f"[종료] RASPI 연결 끊김")


def accept_loop(server):
    global raspi_sock
    while True:
        conn, addr = server.accept()
        with raspi_lock:
            if raspi_sock:
                raspi_sock.close()
            raspi_sock = conn
        threading.Thread(target=recv_loop, args=(conn, addr), daemon=True).start()


server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((HOST, PORT))
server.listen(5)
print(f"[서버] {PORT} 포트 대기 중...")
print("명령: 1 = ColorRequest 전송 / q = 종료")

threading.Thread(target=accept_loop, args=(server,), daemon=True).start()

while True:
    cmd = input("> ").strip()
    if cmd == "q":
        break
    elif cmd == "1":
        with raspi_lock:
            sock = raspi_sock
        if not sock:
            print("[오류] RASPI 미접속")
            continue
        try:
            sock.sendall("ColorRequest\n".encode("utf-8"))
            print(f"[{time.strftime('%H:%M:%S')}][SEND] → RASPI: ColorRequest")
        except Exception as e:
            print(f"[오류] 전송 실패: {e}")
    else:
        print("명령: 1 = ColorRequest / q = 종료")
