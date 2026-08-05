import socket
import time
import threading

HOST = "192.168.3.11"
PORT = 7171
PASSWORD = "1234"

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.connect((HOST, PORT))

# 수신 전용 스레드
def recv_loop():
    while True:
        try:
            data = s.recv(1024)
            if not data:
                break
            print(f"\n[수신] {data.decode()}", end="")
        except:
            break

# 패스워드 처리
data = s.recv(1024)
print(f"[수신] {data.decode()}")
s.send(f"{PASSWORD}\r\n".encode())
time.sleep(0.5)

# 수신 스레드 시작
t = threading.Thread(target=recv_loop, daemon=True)
t.start()

# 명령 입력 루프
print("명령 입력 (종료: quit)")
while True:
    cmd = input("> ")
    if cmd.lower() == "quit":
        break
    s.send(f"{cmd}\r\n".encode())

s.close()