#!/usr/bin/env python3
"""
qr_wall_scan_node.py — 외곽 벽면 QR 탐색 통합 노드
=====================================================
동작 순서:
  1. /map 토픽 수신 대기 (MapCoordSystem 초기화)
  2. PGM+YAML로 외곽 벽 경유지 자동 생성 (perimeter_planner)
  3. Nav2 BasicNavigator로 경유지 순차 주행
  4. 주행 중 /camera/image_raw/compressed 구독, pyzbar QR 감지
  5. 감지 시 → /qr/capture_image + /qr/metadata 퍼블리시 (Stage 3 호환)
  6. scan_positions.yaml 로컬 저장

상태머신:
  WAITING_MAP → MAP_RECEIVED → NAVIGATING → DONE

토픽:
  구독: /camera/image_raw/compressed (CompressedImage)
        /map                          (OccupancyGrid)
  퍼블리시: /qr/capture_image (CompressedImage)
            /qr/metadata      (String JSON)

실행 전 필요 서비스 (터틀봇):
  ros2 launch nav2_bringup localization_launch.py \\
    map:=/home/ubuntu22/map/0622_map_final.yaml use_sim_time:=false
  ros2 launch nav2_bringup navigation_launch.py use_sim_time:=false
  ※ RViz에서 '2D Pose Estimate'로 초기 위치 설정 필수

실행:
  ros2 run qr_wall_scan qr_wall_scan_node --ros-args \\
    -p map_yaml_path:=/home/ubuntu22/map/0622_map_final.yaml \\
    -p save_dir:=/home/ubuntu22/qr_scans \\
    -p standoff_dist:=0.35 \\
    -p waypoint_interval:=0.40
"""

import json
import math
import os
import time
import threading
from datetime import datetime

import cv2
import numpy as np
import yaml
import rclpy
from rclpy.node import Node
from rclpy.executors import MultiThreadedExecutor
from nav_msgs.msg import OccupancyGrid
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from geometry_msgs.msg import PoseStamped
import tf2_ros
from cv_bridge import CvBridge
from pyzbar import pyzbar

try:
    from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
    NAV2_AVAILABLE = True
except ImportError:
    NAV2_AVAILABLE = False

from qr_wall_scan.perimeter_planner import generate_outer_wall_waypoints
from qr_wall_scan.map_coord_utils import MapCoordSystem


class QRWallScanNode(Node):

    def __init__(self):
        super().__init__('qr_wall_scan_node')

        # ── 파라미터 ──────────────────────────────────────────────────────────
        self.declare_parameter('map_yaml_path',     '/home/ubuntu22/map/0622_map_final.yaml')
        self.declare_parameter('save_dir',          '/home/ubuntu22/qr_scans')
        self.declare_parameter('standoff_dist',     0.35)
        self.declare_parameter('waypoint_interval', 0.40)
        self.declare_parameter('scan_cooldown_sec', 3.0)

        self.map_yaml_path     = self.get_parameter('map_yaml_path').value
        self.save_dir          = self.get_parameter('save_dir').value
        self.standoff_dist     = self.get_parameter('standoff_dist').value
        self.waypoint_interval = self.get_parameter('waypoint_interval').value
        self.scan_cooldown     = self.get_parameter('scan_cooldown_sec').value
        self.map_pgm_path      = self.map_yaml_path.replace('.yaml', '.pgm')

        os.makedirs(self.save_dir, exist_ok=True)

        # ── 내부 상태 ─────────────────────────────────────────────────────────
        self.state         = 'WAITING_MAP'
        self.coord         = MapCoordSystem()
        self.bridge        = CvBridge()
        self.last_scan:    dict[str, float] = {}
        self.scan_results: list[dict]       = []
        self.navigator     = None
        self._nav_thread   = None
        self._nav_lock     = threading.Lock()

        # ── TF2 ───────────────────────────────────────────────────────────────
        self.tf_buffer   = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)

        # ── Subscribers ───────────────────────────────────────────────────────
        map_qos = rclpy.qos.QoSProfile(
            depth=1,
            durability=rclpy.qos.DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
        )
        self.map_sub = self.create_subscription(
            OccupancyGrid, '/map', self._map_callback, map_qos)

        # /camera/image_raw/compressed (CompressedImage)
        self.img_sub = self.create_subscription(
            CompressedImage,
            '/camera/image_raw/compressed',
            self._image_callback,
            10)

        # ── Publishers ────────────────────────────────────────────────────────
        self.qr_image_pub = self.create_publisher(
            CompressedImage, '/qr/capture_image', 10)
        self.qr_meta_pub  = self.create_publisher(
            String, '/qr/metadata', 10)

        # ── 상태 루프 타이머 (1Hz) ────────────────────────────────────────────
        self.state_timer = self.create_timer(1.0, self._state_loop)

        if not NAV2_AVAILABLE:
            self.get_logger().error(
                "nav2_simple_commander 미설치!\n"
                "  sudo apt install ros-humble-nav2-simple-commander"
            )

        self.get_logger().info(
            f"QR Wall Scan 노드 시작 [WAITING_MAP]\n"
            f"  맵:   {self.map_yaml_path}\n"
            f"  이격: {self.standoff_dist}m | 간격: {self.waypoint_interval}m\n"
            f"  저장: {self.save_dir}"
        )

    # =========================================================================
    # /map 콜백
    # =========================================================================
    def _map_callback(self, msg: OccupancyGrid):
        self.coord.update_from_occupancy_grid(msg)

        if self.state == 'WAITING_MAP':
            b = self.coord.get_bounds()
            self.get_logger().info(
                f"[WAITING_MAP → MAP_RECEIVED] /map 수신\n"
                f"  맵 크기: {b.max_x:.2f} x {b.max_y:.2f} m\n"
                f"  원점:    ({self.coord._origin_x:.4f}, {self.coord._origin_y:.4f})"
            )
            self.state = 'MAP_RECEIVED'

    # =========================================================================
    # 상태머신 루프 (1Hz)
    # =========================================================================
    def _state_loop(self):
        if self.state == 'MAP_RECEIVED':
            self._start_navigation_pipeline()

        elif self.state == 'NAVIGATING':
            with self._nav_lock:
                nav = self.navigator
            if nav is not None and nav.isTaskComplete():
                result = nav.getResult()
                if result == TaskResult.SUCCEEDED:
                    self.get_logger().info('✅ 외곽 벽 순회 완료!')
                else:
                    self.get_logger().warn(f'주행 종료 (결과: {result})')
                self.state = 'DONE'
                self._save_scan_results()
                self.get_logger().info(
                    f"[완료] QR 감지 {len(self.scan_results)}개 | "
                    f"저장: {self.save_dir}/scan_positions.yaml"
                )

    # =========================================================================
    # 경유지 생성 + Nav2 주행 시작
    # =========================================================================
    def _start_navigation_pipeline(self):
        self.state = 'GENERATING'

        self.get_logger().info('외곽 벽 경유지 생성 중...')
        try:
            waypoints_raw = generate_outer_wall_waypoints(
                pgm_path   = self.map_pgm_path,
                yaml_path  = self.map_yaml_path,
                standoff_m = self.standoff_dist,
                interval_m = self.waypoint_interval,
            )
        except Exception as e:
            self.get_logger().error(f'경유지 생성 실패: {e}')
            self.state = 'ERROR'
            return

        self.get_logger().info(f'경유지 {len(waypoints_raw)}개 생성 완료')

        if not NAV2_AVAILABLE:
            self.state = 'ERROR'
            return

        # PoseStamped 변환
        nav_poses: list[PoseStamped] = []
        for wx, wy, yaw in waypoints_raw:
            p               = PoseStamped()
            p.header.frame_id = 'map'
            p.header.stamp  = self.get_clock().now().to_msg()
            p.pose.position.x = wx
            p.pose.position.y = wy
            # yaw → quaternion (z, w만 사용, pitch=roll=0)
            p.pose.orientation.z = math.sin(yaw / 2.0)
            p.pose.orientation.w = math.cos(yaw / 2.0)
            nav_poses.append(p)

        # Nav2는 별도 스레드에서 초기화 (waitUntilNav2Active 블로킹)
        def _nav_worker():
            nav = BasicNavigator()
            self.get_logger().info('Nav2 활성화 대기 중... (RViz에서 2D Pose Estimate 설정 필요)')
            nav.waitUntilNav2Active()
            self.get_logger().info('Nav2 준비 완료 — 외곽 벽 주행 시작!')

            with self._nav_lock:
                self.navigator = nav
            self.state = 'NAVIGATING'

            nav.followWaypoints(nav_poses)

        self._nav_thread = threading.Thread(target=_nav_worker, daemon=True)
        self._nav_thread.start()

    # =========================================================================
    # 이미지 콜백 — QR 감지 (/camera/image_raw/compressed)
    # =========================================================================
    def _image_callback(self, msg: CompressedImage):
        # NAVIGATING 상태일 때만 처리
        if self.state != 'NAVIGATING':
            return

        # CompressedImage → OpenCV
        try:
            frame = self.bridge.compressed_imgmsg_to_cv2(msg, desired_encoding='bgr8')
        except Exception as e:
            self.get_logger().warn(
                f'이미지 변환 실패: {e}', throttle_duration_sec=5.0)
            return

        WHITELIST = {
            'QR-001', 'QR-002', 'QR-003',
            'QR-CHEONAN', 'QR-PYEONGTAEK', 'QR-GONGJU', 'QR-ARRIVAL',
        }
        qr_list = [d for d in pyzbar.decode(frame) if d.type == 'QRCODE']
        for qr in qr_list:
            qr_data = qr.data.decode('utf-8').strip()
            if not qr_data or qr_data not in WHITELIST:
                continue

            # 쿨다운: 같은 QR은 scan_cooldown초마다 1회만
            now = time.time()
            if now - self.last_scan.get(qr_data, 0) < self.scan_cooldown:
                continue
            self.last_scan[qr_data] = now

            # 현재 로봇 포즈 (map 프레임, TF2) — 이미지 캡처 시점 기준
            pose    = self._get_pose(msg.header.stamp)
            map_x   = None
            map_y   = None
            world_x = None
            world_y = None
            in_bounds = False

            if pose is not None and self.coord.initialized:
                world_x = pose.pose.position.x
                world_y = pose.pose.position.y
                try:
                    map_x, map_y = self.coord.world_to_map_bl(world_x, world_y)
                    in_bounds    = self.coord.is_within_bounds(map_x, map_y)
                except RuntimeError as e:
                    self.get_logger().warn(f'좌표 변환 실패: {e}')

            if map_x is not None:
                self.get_logger().info(
                    f'[QR 감지] "{qr_data}" | '
                    f'맵기준=({map_x:.3f}, {map_y:.3f}) | '
                    f'{"OK" if in_bounds else "⚠범위밖"}'
                )
            else:
                self.get_logger().info(f'[QR 감지] "{qr_data}" | 좌표 획득 실패')

            # QR 박스 오버레이
            frame = self._draw_overlay(frame, qr, qr_data, map_x, map_y)

            # 이미지 저장
            ts_str   = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:19]
            img_name = f'qr_{ts_str}_{qr_data[:16]}.jpg'
            img_path = os.path.join(self.save_dir, img_name)
            cv2.imwrite(img_path, frame)

            # /qr/capture_image 퍼블리시
            self._publish_compressed(frame)

            # /qr/metadata 퍼블리시 (Stage 3 qr_database_node 호환)
            meta = {
                'qr_data':   qr_data,
                'x':         round(map_x,   4) if map_x   is not None else None,
                'y':         round(map_y,   4) if map_y   is not None else None,
                'world_x':   round(world_x, 4) if world_x is not None else None,
                'world_y':   round(world_y, 4) if world_y is not None else None,
                'in_bounds': in_bounds,
                'timestamp': ts_str,
                'img_file':  img_name,
            }
            self.qr_meta_pub.publish(
                String(data=json.dumps(meta, ensure_ascii=False))
            )
            self.scan_results.append(meta)
            self._save_scan_results()

    # ── TF2 포즈 획득 ─────────────────────────────────────────────────────────
    def _get_pose(self, stamp=None) -> PoseStamped | None:
        try:
            tf_time = (rclpy.time.Time.from_msg(stamp)
                       if stamp is not None else rclpy.time.Time())
            tf = self.tf_buffer.lookup_transform(
                'map', 'base_link',
                tf_time,
                timeout=rclpy.duration.Duration(seconds=0.3)
            )
            p = PoseStamped()
            p.header           = tf.header
            p.pose.position.x  = tf.transform.translation.x
            p.pose.position.y  = tf.transform.translation.y
            p.pose.orientation = tf.transform.rotation
            return p
        except Exception:
            return None

    # ── QR 오버레이 ───────────────────────────────────────────────────────────
    def _draw_overlay(self, frame, qr, qr_data: str,
                      map_x: float | None, map_y: float | None):
        pts = qr.polygon
        if len(pts) == 4:
            cv2.polylines(
                frame,
                [np.array([(p.x, p.y) for p in pts], dtype=int)],
                True, (0, 255, 0), 2
            )
        coord_str = (f'({map_x:.2f},{map_y:.2f})' if map_x is not None
                     else '(?,?)')
        cv2.putText(
            frame, f'{qr_data} {coord_str}',
            (qr.rect.left, max(qr.rect.top - 10, 10)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2
        )
        return frame

    # ── CompressedImage 퍼블리시 ──────────────────────────────────────────────
    def _publish_compressed(self, frame):
        _, buf     = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        msg        = CompressedImage()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.format = 'jpeg'
        msg.data   = buf.tobytes()
        self.qr_image_pub.publish(msg)

    # ── scan_positions.yaml 저장 ──────────────────────────────────────────────
    def _save_scan_results(self):
        out_path = os.path.join(self.save_dir, 'scan_positions.yaml')
        doc = {
            'session_info': {
                'map_yaml':           self.map_yaml_path,
                'map_origin_world_x': self.coord._origin_x,
                'map_origin_world_y': self.coord._origin_y,
                'standoff_dist_m':    self.standoff_dist,
                'waypoint_interval_m': self.waypoint_interval,
            },
            'qr_scan_positions': self.scan_results,
        }
        with open(out_path, 'w', encoding='utf-8') as f:
            yaml.dump(doc, f, allow_unicode=True, default_flow_style=False)


# =============================================================================
def main(args=None):
    rclpy.init(args=args)
    node     = QRWallScanNode()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
