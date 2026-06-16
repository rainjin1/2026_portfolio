import rclpy
from rclpy.node import Node
from std_msgs.msg import String
import struct
import os

class RemoteBarcodeNode(Node):
    def __init__(self):
        super().__init__('remote_barcode_node')
        # ROS2 토픽 퍼블리셔 등록
        self.publisher_ = self.create_publisher(String, '/barcode_text', 10)
        
        # 바코드 리더기 커널 장치 경로
        self.device_path = '/dev/input/by-id/usb-Barcode_Scanner_Barcode_Scanner-event-kbd'
        
        # 리눅스 키보드 이벤트 매핑 테이블 (바코드 숫자 대응)
        self.key_map = {
            2: '1', 3: '2', 4: '3', 5: '4', 6: '5', 7: '6', 8: '7', 9: '8', 10: '9', 11: '0',
            28: '\n'  # 엔터키 입력 시 데이터 전송
        }
        
        self.current_barcode = ""
        self.get_logger().info(f" 원격 바코드 패키지 노드 가동! 장치 파일 읽는 중: {self.device_path}")
        
        try:
            # Non-blocking 모드로 파일 오픈
            self.file_desc = os.open(self.device_path, os.O_RDONLY | os.O_NONBLOCK)
            self.create_timer(0.01, self.read_device_events)
        except Exception as e:
            self.get_logger().error(f"❌ 장치를 열 수 없습니다! (udev 권한 확인 필요): {e}")

    def read_device_events(self):
        # 64비트 리눅스 input_event 구조체 포맷 (시간, 타입, 코드, 값)
        EVENT_FORMAT = 'llHHI'
        EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
        
        try:
            while True:
                data = os.read(self.file_desc, EVENT_SIZE)
                if not data:
                    break
                
                _, _, ev_type, code, value = struct.unpack(EVENT_FORMAT, data)
                
                # ev_type == 1 (Key 이벤트), value == 1 (Key Press)
                if ev_type == 1 and value == 1:
                    if code in self.key_map:
                        char = self.key_map[code]
                        if char == '\n':  # 줄바꿈(엔터) 조건 시 완성된 바코드 송신
                            if self.current_barcode:
                                msg = String()
                                msg.data = self.current_barcode
                                self.publisher_.publish(msg)
                                self.get_logger().info(f"🎯 [터틀봇 -> PC] 바코드 송신: {self.current_barcode}")
                                self.current_barcode = ""  # 버퍼 초기화
                        else:
                            self.current_barcode += char
        except BlockingIOError:
            pass

def main(args=None):
    rclpy.init(args=args)
    node = RemoteBarcodeNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        os.close(node.file_desc)
        rclpy.shutdown()

if __name__ == '__main__':
    main()
