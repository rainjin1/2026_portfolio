import socket
import threading

HOST = "0.0.0.0"
PORT = 9000

def handle_client(conn, addr):
    print(f"[접속] {addr}")

    def recv_loop():
        while True:
            try:
                data = conn.recv(1024)
                if not data:
                    break
                print(f"[수신] {data.decode().strip()}")
            except:
                break

    t = threading.Thread(target=recv_loop, daemon=True)
    t.start()

    while True:
        msg = input("> ")
        if msg == "quit":
            break
        try:
            conn.sendall(msg.encode())
        except:
            print("[오류] 전송 실패")
            break

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((HOST, PORT))
server.listen(5)
print(f"서버 대기 중 - {HOST}:{PORT}")

while True:
    conn, addr = server.accept()
    t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
    t.start()