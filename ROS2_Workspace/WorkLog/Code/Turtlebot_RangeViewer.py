ubuntu22@ubuntu22:~$ python3 -u -c "
import rclpyiewer(Node):
from rclpy.node import Node
from sensor_msgs.msg import LaserScanr')
        # 터틀봇3 라이다용 Best Effort QoS 프로파일 설정
class QuickViewer(Node):y.qos.QoSProfile(
    def __init__(self):=rclpy.qos.ReliabilityPolicy.BEST_EFFORT,
        super().__init__('quick_viewer')icy.KEEP_LAST,
        # 터틀봇3 라이다용 Best Effort QoS 프로파일 설정
        lidar_qos = rclpy.qos.QoSProfile(
            reliability=rclpy.qos.ReliabilityPolicy.BEST_EFFORT,dar_qos)
            history=rclpy.qos.HistoryPolicy.KEEP_LAST,
            depth=5g):
        )   = msg.ranges[0]    # 정면 (0도)
        self.create_subscription(LaserScan, '/scan', self.cb, lidar_qos)
        l   = msg.ranges[90]   # 정좌측 (90도)
    def cb(self, msg):es[135]  # 좌후방 (135도)
        f   = msg.ranges[0]    # 정면 (0도) )
        lf  = msg.ranges[45]   # 좌전방 (45도))
        l   = msg.ranges[90]   # 정좌측 (90도))
        lb  = msg.ranges[135]  # 좌후방 (135도)
        b   = msg.ranges[180]  # 후방 (180도)
        rb  = msg.ranges[225]  # 우후방 (225도)화면 새로고침
        r   = msg.ranges[270]  # 정우측 (270도)======================')
        rf  = msg.ranges[315]  # 우전방 (315도)시간 RAW 데이터 뷰어')
        print('======================================================')
        print('\033[H\033[J', end='') # 터미널 화면 새로고침전방 315°]')
        print('======================================================')n')
        print('       🔍 터틀봇3 버거 LDS-01 실시간 RAW 데이터 뷰어'):.3f}m  [정우측 270°]\n')
        print('======================================================'))
        print(f'              [좌전방 45°]  [정  면 0°]  [우전방 315°]'))
        print(f'                {lf:.3f}m      {f:.3f}m      {rf:.3f}m\n')
        print(f'  [정좌측 90°]  {l:.3f}m   <-- 🤖 TURTLEBOT3 -->   {r:.3f}m  [정우측 270°]\n')
        print(f'                {lb:.3f}m      {b:.3f}m      {rb:.3f}m')
        print(f'              [좌후방 135°] [후  방 180°] [우후방 225°]')
        print('======================================================')
    try:
def main():py.spin(node)
    rclpy.init()ardInterrupt:
    node = QuickViewer()
    try:lly:
        rclpy.spin(node)e()
    except KeyboardInterrupt:
        passrclpy.shutdown()
    finally:
        node.destroy_node()
"   main()_ == '__main__':()
