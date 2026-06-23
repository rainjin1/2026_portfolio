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

        # ── 맵 캐시 (최적 접근 포즈 계산용) ──────────────────────────────────
        # OccupancyGrid 수신 시 1회 계산: 자유↔벽 거리 변환 + 그래디언트
        self._map_cache_ready = False
        self._dist_map        = None   # cv2 distanceTransform 결과
        self._grad_x_map      = None   # Sobel X (월드 X 방향 기울기)
        self._grad_y_map      = None   # Sobel Y (월드 Y 방향 기울기)

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

        # 맵 캐시: 최초 1회만 계산 (localization 단계에서는 맵 고정)
        if not self._map_cache_ready:
            self._update_map_cache(msg)

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
    # 맵 캐시 업데이트 (최초 1회)
    # =========================================================================
    def _update_map_cache(self, msg: OccupancyGrid):
        """
        OccupancyGrid → 거리 변환 + 그래디언트 캐시.
        각 픽셀에서 가장 가까운 벽까지의 거리와 방향을 미리 계산.
        """
        try:
            arr = np.frombuffer(bytes(msg.data), dtype=np.int8).reshape(
                (msg.info.height, msg.info.width))
            # OccupancyGrid: -1=unknown, 0=free, 100=occupied
            # 65 이상을 벽(occupied)으로 처리
            occupied  = (arr >= 65).astype(np.uint8)
            free_mask = (occupied == 0).astype(np.uint8)

            dist = cv2.distanceTransform(free_mask, cv2.DIST_L2, 5)
            self._dist_map   = dist
            self._grad_x_map = cv2.Sobel(dist, cv2.CV_32F, 1, 0, ksize=3)
            self._grad_y_map = cv2.Sobel(dist, cv2.CV_32F, 0, 1, ksize=3)
            self._map_cache_ready = True
            self.get_logger().info(
                f'맵 캐시 준비 완료 ({msg.info.width}×{msg.info.height} cells) '
                '— 최적 접근 포즈 계산 가능'
            )
        except Exception as e:
            self.get_logger().warn(f'맵 캐시 계산 실패: {e}')

    # =========================================================================
    # 최적 접근 포즈 계산
    # =========================================================================
    def _compute_optimal_pose(
        self,
        world_x: float,
        world_y: float,
        standoff: float = 0.10,
    ) -> 'tuple[float | None, float | None, float | None]':
        """
        QR 스캔 시 로봇 위치 기반으로 벽에서 standoff 거리의 최적 접근 포즈 계산.

        원리:
          - 거리 변환 그래디언트 = 벽으로부터 멀어지는 방향
          - 그래디언트 반대 방향 = 가장 가까운 벽을 향하는 수직 방향
          - 최적 위치 = 로봇 위치 + (현재 벽 거리 − standoff) × 벽 방향 단위벡터
          - yaw = 벽을 정면으로 바라보는 각도

        Args:
            world_x, world_y : QR 스캔 시 로봇 위치 (/map 프레임, 미터)
            standoff         : 벽으로부터 이격 거리 (기본 0.10m = 10cm)
        Returns:
            (opt_x, opt_y, yaw_rad) 또는 계산 불가 시 (None, None, None)
        """
        if not self._map_cache_ready or not self.coord.initialized:
            return None, None, None

        res = self.coord._resolution
        ox  = self.coord._origin_x
        oy  = self.coord._origin_y
        h, w = self._dist_map.shape

        px = int(round((world_x - ox) / res))
        py = int(round((world_y - oy) / res))

        if not (1 <= px < w - 1 and 1 <= py < h - 1):
            self.get_logger().warn(
                f'최적 포즈 계산: 픽셀({px},{py})이 맵 범위 밖')
            return None, None, None

        gx    = float(self._grad_x_map[py, px])
        gy    = float(self._grad_y_map[py, px])
        g_len = math.hypot(gx, gy)

        if g_len < 1e-3:
            return None, None, None

        # 그래디언트 반대 방향 = 가장 가까운 벽을 향하는 단위벡터
        ux = -gx / g_len
        uy = -gy / g_len

        # 현재 로봇 위치 → 벽까지 거리 (m)
        dist_m = float(self._dist_map[py, px]) * res

        # 벽에서 standoff만큼 떨어진 최적 위치
        move_m = dist_m - standoff
        opt_x  = world_x + ux * move_m
        opt_y  = world_y + uy * move_m

        # 벽을 정면으로 바라보는 yaw (벽 방향 = ux, uy)
        yaw = math.atan2(uy, ux)

        return opt_x, opt_y, yaw

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

            # 최적 접근 포즈 계산 (벽에서 10cm, 정면)
            opt_x, opt_y, opt_yaw = (None, None, None)
            if world_x is not None:
                opt_x, opt_y, opt_yaw = self._compute_optimal_pose(
                    world_x, world_y, standoff=0.10)

            if map_x is not None:
                opt_str = (
                    f'→ 최적포즈=({opt_x:.3f},{opt_y:.3f}) '
                    f'yaw={math.degrees(opt_yaw):.1f}°'
                    if opt_x is not None else '→ 최적포즈 계산불가'
                )
                self.get_logger().info(
                    f'[QR 감지] "{qr_data}" | '
                    f'맵기준=({map_x:.3f}, {map_y:.3f}) | '
                    f'{"OK" if in_bounds else "⚠범위밖"} | {opt_str}'
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
                # 다음 노드(자동 배송)용 최적 접근 포즈
                # 벽에서 수직 10cm, 벽을 정면으로 바라보는 방향
                'approach_pose': {
                    'world_x':  round(opt_x,   4) if opt_x   is not None else None,
                    'world_y':  round(opt_y,   4) if opt_y   is not None else None,
                    'yaw_rad':  round(opt_yaw, 4) if opt_yaw is not None else None,
                    'yaw_deg':  round(math.degrees(opt_yaw), 1) if opt_yaw is not None else None,
                } if opt_x is not None else None,
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
        # ── YAML 저장 (프로그래밍용) ─────────────────────────────────────────
        yaml_path = os.path.join(self.save_dir, 'scan_positions.yaml')
        doc = {
            'session_info': {
                'map_yaml':            self.map_yaml_path,
                'map_origin_world_x':  self.coord._origin_x,
                'map_origin_world_y':  self.coord._origin_y,
                'standoff_dist_m':     self.standoff_dist,
                'waypoint_interval_m': self.waypoint_interval,
            },
            'qr_scan_positions': self.scan_results,
        }
        with open(yaml_path, 'w', encoding='utf-8') as f:
            yaml.dump(doc, f, allow_unicode=True, default_flow_style=False)

        # ── TXT 저장 (사람이 읽는 요약) ──────────────────────────────────────
        txt_path = os.path.join(self.save_dir, 'scan_positions.txt')
        now_str  = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        lines    = [
            '=' * 56,
            '  QR 스캔 결과 + 최적 접근 포즈',
            f'  생성: {now_str}',
            f'  맵:   {self.map_yaml_path}',
            '=' * 56,
            '',
        ]
        for i, r in enumerate(self.scan_results, start=1):
            ap = r.get('approach_pose')
            lines += [
                f"[{i}] {r['qr_data']}",
                f"    스캔 위치 (맵기준)  : x={r.get('x')}, y={r.get('y')}",
                f"    스캔 위치 (월드)    : x={r.get('world_x')}, y={r.get('world_y')}",
            ]
            if ap:
                lines += [
                    f"    최적 접근 포즈     :",
                    f"      x      = {ap['world_x']} m",
                    f"      y      = {ap['world_y']} m",
                    f"      yaw    = {ap['yaw_deg']}°  ({ap['yaw_rad']} rad)",
                    f"      ※ 벽에서 수직 10cm, 벽을 정면으로 바라보는 방향",
                ]
            else:
                lines.append('    최적 접근 포즈     : 계산 불가 (맵 정보 부족)')
            lines += [f"    이미지             : {r.get('img_file')}", '']

        lines += [
            '-' * 56,
            f'  총 {len(self.scan_results)}개 QR 감지',
            '-' * 56,
        ]
        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))


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
