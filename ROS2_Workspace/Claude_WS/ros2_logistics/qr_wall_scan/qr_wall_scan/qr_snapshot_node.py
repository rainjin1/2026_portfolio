#!/usr/bin/env python3
"""
qr_snapshot_node.py — FOV 기반 QR 스냅샷 탐색 노드
=====================================================
동작 순서:
  1. /map 수신 → WallCoveragePlanner로 촬영 위치 목록 생성
  2. Nav2로 각 촬영 위치에 순서대로 이동
  3. 도착 시 카메라에서 사진 1장만 캡처 → QR 감지
  4. QR 픽셀 위치 + 로봇 포즈 → QR 월드 좌표 역산
  5. 결과 저장 (YAML + TXT)

기존 qr_wall_scan_node.py 와 차이점:
  - 연속 영상 스트리밍 → 촬영 위치에서 1장만 캡처 (네트워크 부하 최소화)
  - 44개 경유지 → 최소 촬영 횟수 (그리디 커버리지)
  - QR 위치 정밀 역산 (픽셀 위치 + TF2 포즈 + 화각 계산)

실행:
  ros2 run qr_wall_scan qr_snapshot_node --ros-args \\
    -p map_yaml_path:=/home/ubuntu22/map/0622_map_final.yaml \\
    -p save_dir:=/home/ubuntu22/qr_scans \\
    -p capture_timeout:=3.0
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
from lifecycle_msgs.msg import Transition
from lifecycle_msgs.srv import ChangeState, GetState

CAMERA_NODE = '/v4l2_camera'   # lifecycle 제어 대상 노드 이름

try:
    from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
    NAV2_AVAILABLE = True
except ImportError:
    NAV2_AVAILABLE = False

from qr_wall_scan.wall_coverage_planner import WallCoveragePlanner, ScanPose
from qr_wall_scan.map_coord_utils import MapCoordSystem


def _load_precomputed_poses(yaml_path: str) -> list[ScanPose]:
    """config/scan_poses_0622.yaml 에서 ScanPose 목록 로드."""
    with open(yaml_path, 'r', encoding='utf-8') as f:
        doc = yaml.safe_load(f)
    poses = []
    for p in doc['poses']:
        poses.append(ScanPose(
            world_x       = float(p['x']),
            world_y       = float(p['y']),
            yaw_rad       = float(p['yaw_rad']),
            yaw_deg       = float(p['yaw_deg']),
            standoff_m    = float(p['standoff_m']),
            angle_to_wall = int(p['angle_to_wall']),
        ))
    return poses


# ── 카메라 파라미터 (라즈베리파이 카메라 v2 / 640×480) ───────────────────────
IMG_WIDTH      = 640
IMG_HEIGHT     = 480
FOV_DEG        = 62.2
FOCAL_LEN_PX   = (IMG_WIDTH / 2.0) / math.tan(math.radians(FOV_DEG / 2.0))

# ── QR 화이트리스트 ───────────────────────────────────────────────────────────
WHITELIST = {
    'QR-001', 'QR-002', 'QR-003',
    'QR-CHEONAN', 'QR-PYEONGTAEK', 'QR-GONGJU', 'QR-ARRIVAL',
}


class QRSnapshotNode(Node):

    def __init__(self):
        super().__init__('qr_snapshot_node')

        # ── 파라미터 ──────────────────────────────────────────────────────────
        self.declare_parameter('map_yaml_path',  '/home/ubuntu22/map/0622_map_final.yaml')
        self.declare_parameter('standoff_max',   0.80)
        self.declare_parameter('standoff_min',   0.30)
        self.declare_parameter('capture_timeout', 3.0)   # 사진 캡처 대기 최대 시간(초)
        # 사전 계산된 촬영 위치 파일 (비어 있으면 런타임에 동적 생성)
        self.declare_parameter('poses_yaml_path', '')

        self.map_yaml_path   = self.get_parameter('map_yaml_path').value
        self.standoff_max    = self.get_parameter('standoff_max').value
        self.standoff_min    = self.get_parameter('standoff_min').value
        self.capture_timeout = self.get_parameter('capture_timeout').value
        self.poses_yaml_path = self.get_parameter('poses_yaml_path').value
        self.map_pgm_path    = self.map_yaml_path.replace('.yaml', '.pgm')

        # 저장 폴더: 맵 yaml 옆 snapshots/ 자동 생성
        self.save_dir = os.path.join(
            os.path.dirname(os.path.abspath(self.map_yaml_path)), 'snapshots')
        os.makedirs(self.save_dir, exist_ok=True)

        # 스냅샷 번호 카운터
        self._snap_idx = 0

        # ── 내부 상태 ─────────────────────────────────────────────────────────
        self.state         = 'WAITING_MAP'
        self.coord         = MapCoordSystem()
        self.bridge        = CvBridge()
        self.navigator     = None
        self._nav_thread   = None
        self._nav_lock     = threading.Lock()

        # 촬영 제어
        self._capture_ready  = False   # True일 때만 이미지 처리
        self._captured_frame = None    # 캡처된 프레임
        self._capture_lock   = threading.Lock()

        # 결과
        self.scan_poses: list[ScanPose] = []   # 계획된 촬영 위치
        self.qr_results: list[dict]    = []    # 감지된 QR 결과
        self.snap_records: list[dict]  = []    # 위치별 스냅 기록 (DB 노드 입력용)
        self._last_snap_file: str | None = None

        # ── TF2 ───────────────────────────────────────────────────────────────
        self.tf_buffer   = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)

        # ── /map 구독 (TRANSIENT_LOCAL — map_server 호환) ─────────────────────
        map_qos = rclpy.qos.QoSProfile(
            depth=1,
            durability=rclpy.qos.DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
        )
        self.map_sub = self.create_subscription(
            OccupancyGrid, '/map', self._map_callback, map_qos)

        # ── 카메라 구독 ────────────────────────────────────────────────────────
        # 도착 시에만 생성/삭제 → lifecycle client로 카메라 자체를 on/off
        self.img_sub = None   # 기본 구독 없음

        # ── lifecycle client (v4l2_camera_node 제어) ──────────────────────────
        self._lc_change = self.create_client(
            ChangeState, f'{CAMERA_NODE}/change_state')
        self._lc_get = self.create_client(
            GetState, f'{CAMERA_NODE}/get_state')

        # ── 퍼블리셔 ─────────────────────────────────────────────────────────
        self.qr_meta_pub      = self.create_publisher(String, '/qr/metadata', 10)
        self.scan_complete_pub = self.create_publisher(String, '/qr/scan_complete', 10)

        # ── 상태 루프 (1Hz) ──────────────────────────────────────────────────
        self.state_timer = self.create_timer(1.0, self._state_loop)

        self.get_logger().info(
            f"QR 스냅샷 노드 시작 [WAITING_MAP]\n"
            f"  맵:   {self.map_yaml_path}\n"
            f"  standoff: {self.standoff_min}~{self.standoff_max}m\n"
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
                f"  맵 크기: {b.max_x:.2f} x {b.max_y:.2f} m"
            )
            self.state = 'MAP_RECEIVED'

    # =========================================================================
    # 상태머신 (1Hz)
    # =========================================================================
    def _state_loop(self):
        if self.state == 'MAP_RECEIVED':
            self._plan_and_navigate()
        elif self.state == 'NAVIGATING':
            pass  # 네비게이션은 별도 스레드에서 처리

    # =========================================================================
    # 촬영 계획 + 네비게이션 시작
    # =========================================================================
    def _plan_and_navigate(self):
        self.state = 'PLANNING'

        # 로봇 현재 위치 (시작 위치로 사용)
        start_xy = (0.0, 0.0)
        pose = self._get_pose()
        if pose:
            start_xy = (pose.pose.position.x, pose.pose.position.y)
            self.get_logger().info(f"로봇 시작 위치: {start_xy}")

        # 촬영 위치 로드 (사전 계산 파일 우선, 없으면 동적 생성)
        if self.poses_yaml_path and os.path.isfile(self.poses_yaml_path):
            self.get_logger().info(
                f'사전 계산 좌표 로드: {self.poses_yaml_path}')
            try:
                self.scan_poses = _load_precomputed_poses(self.poses_yaml_path)
                self.get_logger().info(
                    f'  → {len(self.scan_poses)}개 포즈 로드 완료')
            except Exception as e:
                self.get_logger().error(f'좌표 파일 로드 실패: {e} — 동적 계획으로 fallback')
                self.scan_poses = []
        else:
            self.scan_poses = []

        if not self.scan_poses:
            self.get_logger().info('촬영 위치 동적 계획 중...')
            try:
                planner = WallCoveragePlanner(
                    pgm_path       = self.map_pgm_path,
                    yaml_path      = self.map_yaml_path,
                    max_standoff_m = self.standoff_max,
                    min_standoff_m = self.standoff_min,
                    start_world_xy = start_xy,
                )
                self.scan_poses = planner.generate(verbose=False)
            except Exception as e:
                self.get_logger().error(f'촬영 위치 생성 실패: {e}')
                self.state = 'ERROR'
                return

        self.get_logger().info(
            f'촬영 위치 {len(self.scan_poses)}개 생성 완료'
        )

        # 시작점 최근접 → 시계방향 정렬
        self.scan_poses = self._sort_poses_clockwise(self.scan_poses, start_xy)
        self.get_logger().info(
            f'촬영 순서 확정: 시작점({start_xy[0]:.2f},{start_xy[1]:.2f})에서 '
            f'가장 가까운 지점부터 시계방향')

        if not NAV2_AVAILABLE:
            self.get_logger().error('nav2_simple_commander 미설치!')
            self.state = 'ERROR'
            return

        # 네비게이션 스레드 시작
        self._nav_thread = threading.Thread(
            target=self._navigation_worker, daemon=True)
        self._nav_thread.start()
        self.state = 'NAVIGATING'

    # =========================================================================
    # 네비게이션 워커 (별도 스레드)
    # =========================================================================
    def _navigation_worker(self):
        nav = BasicNavigator()
        self.get_logger().info(
            'Nav2 활성화 대기 중... (RViz에서 2D Pose Estimate 설정 필요)')
        nav.waitUntilNav2Active()
        self.get_logger().info('Nav2 준비 완료')

        with self._nav_lock:
            self.navigator = nav

        total = len(self.scan_poses)
        for i, scan_pose in enumerate(self.scan_poses):
            self.get_logger().info(
                f'[{i+1}/{total}] 이동 → '
                f'({scan_pose.world_x:.3f}, {scan_pose.world_y:.3f}) '
                f'yaw={scan_pose.yaw_deg:.1f}°  '
                f'dist={scan_pose.standoff_m:.2f}m'
                + (f'  사선{scan_pose.angle_to_wall}°'
                   if scan_pose.angle_to_wall > 0 else '')
            )

            # 위치별 스냅 기록 초기화
            record = {
                'pose_idx':      i + 1,
                'world_x':       scan_pose.world_x,
                'world_y':       scan_pose.world_y,
                'yaw_rad':       scan_pose.yaw_rad,
                'yaw_deg':       scan_pose.yaw_deg,
                'standoff_m':    scan_pose.standoff_m,
                'angle_to_wall': scan_pose.angle_to_wall,
                'snap_file':     None,   # 캡처 성공 시 채워짐
                'nav_success':   False,
            }

            # 목표 포즈 설정
            goal = PoseStamped()
            goal.header.frame_id = 'map'
            goal.header.stamp    = self.get_clock().now().to_msg()
            goal.pose.position.x = scan_pose.world_x
            goal.pose.position.y = scan_pose.world_y
            yaw = scan_pose.yaw_rad
            goal.pose.orientation.z = math.sin(yaw / 2.0)
            goal.pose.orientation.w = math.cos(yaw / 2.0)

            nav.goToPose(goal)

            # 도착 대기
            while not nav.isTaskComplete():
                time.sleep(0.2)

            result = nav.getResult()
            if result != TaskResult.SUCCEEDED:
                self.get_logger().warn(
                    f'  [{i+1}] 도달 실패 (결과: {result}) — 다음으로 진행')
                self.snap_records.append(record)
                continue

            record['nav_success'] = True
            self.get_logger().info(f'  [{i+1}] 도착 완료 → 카메라 ON')

            # 카메라 activate → 캡처 → deactivate
            cam_ok = self._camera_on()
            if not cam_ok:
                self.get_logger().warn(f'  [{i+1}] 카메라 ON 실패 — 건너뜀')
                self.snap_records.append(record)
                continue

            self._last_snap_file = None
            captured = self._wait_for_capture(scan_pose)
            self._camera_off()

            if captured:
                record['snap_file'] = self._last_snap_file
                self.get_logger().info(
                    f'  [{i+1}] 캡처 완료: {self._last_snap_file} '
                    f'(누적 감지: {len(self.qr_results)}개)')
            else:
                self.get_logger().warn(f'  [{i+1}] 캡처 타임아웃')

            self.snap_records.append(record)

        # 완료
        self.state = 'DONE'
        yaml_out = self._save_results()
        self.get_logger().info(
            f'[완료] 총 {total}개 위치 촬영 | '
            f'QR {len(self.qr_results)}개 감지 | '
            f'저장: {self.save_dir}'
        )
        # qr_database_node에 처리 시작 신호 (yaml 경로 전달)
        self.scan_complete_pub.publish(String(data=yaml_out))

    # =========================================================================
    # 사진 캡처 대기
    # =========================================================================
    def _wait_for_capture(self, scan_pose: ScanPose) -> bool:
        """
        _capture_ready 플래그를 세우고 capture_timeout초 내에
        이미지 콜백이 처리하도록 대기.
        """
        with self._capture_lock:
            self._captured_frame = None
            self._capture_ready  = True

        deadline = time.time() + self.capture_timeout
        while time.time() < deadline:
            time.sleep(0.05)
            with self._capture_lock:
                frame = self._captured_frame
                if frame is not None:
                    self._capture_ready = False
                    break
        else:
            with self._capture_lock:
                self._capture_ready = False
            return False

        # 스냅샷 번호 부여
        self._snap_idx += 1

        # QR 감지 + 좌표 역산
        self._process_frame(frame, scan_pose, self._snap_idx)
        return True

    # =========================================================================
    # 이미지 콜백 (capture_ready일 때 1장만 처리)
    # =========================================================================
    def _image_callback(self, msg: CompressedImage):
        with self._capture_lock:
            if not self._capture_ready or self._captured_frame is not None:
                return  # 대기 중이 아니거나 이미 캡처됨

        try:
            frame = self.bridge.compressed_imgmsg_to_cv2(
                msg, desired_encoding='bgr8')
        except Exception as e:
            self.get_logger().warn(f'이미지 변환 실패: {e}')
            return

        with self._capture_lock:
            self._captured_frame = frame

    # =========================================================================
    # 프레임 처리 + QR 위치 역산
    # =========================================================================
    def _process_frame(self, frame, scan_pose: ScanPose, snap_idx: int = 0):
        """
        캡처된 프레임에서 QR 코드 감지 후 월드 좌표 역산.

        역산 원리:
          - 로봇 포즈 (rx, ry, ryaw): TF2로 획득
          - QR 픽셀 중심 (qx, qy): pyzbar 바운딩박스 중심
          - 수평 각도 오프셋: α = atan((qx - W/2) / focal_len_px)
          - QR 방향 (월드): ryaw + α
          - QR 거리: 촬영 위치 standoff_m (벽 위에 있다고 가정)
          - QR 월드 좌표: (rx + d*cos(ryaw+α), ry + d*sin(ryaw+α))
        """
        pose_msg = self._get_pose()
        robot_x = robot_y = robot_yaw = None

        if pose_msg:
            robot_x = pose_msg.pose.position.x
            robot_y = pose_msg.pose.position.y
            q = pose_msg.pose.orientation
            robot_yaw = math.atan2(
                2*(q.w*q.z + q.x*q.y),
                1 - 2*(q.y*q.y + q.z*q.z)
            )

        # 이미지 저장 (넘버링)
        ts_str   = datetime.now().strftime('%Y%m%d_%H%M%S')
        raw_name = f'snap_{snap_idx:03d}_{ts_str}.jpg'
        raw_path = os.path.join(self.save_dir, raw_name)
        cv2.imwrite(raw_path, frame)
        self._last_snap_file = raw_name   # navigation_worker에서 record에 기록

        # QR 감지
        h, w = frame.shape[:2]
        qr_list = [d for d in pyzbar.decode(frame) if d.type == 'QRCODE']

        for qr in qr_list:
            qr_data = qr.data.decode('utf-8').strip()
            if not qr_data or qr_data not in WHITELIST:
                continue

            # QR 픽셀 중심
            qx = qr.rect.left + qr.rect.width  / 2.0
            qy = qr.rect.top  + qr.rect.height / 2.0

            # 수평 각도 오프셋 (카메라 중심 기준)
            alpha = math.atan2(qx - w / 2.0, FOCAL_LEN_PX)

            # QR 월드 좌표 역산
            qr_world_x = qr_world_y = None
            approach   = None

            if robot_x is not None:
                d = scan_pose.standoff_m
                direction = robot_yaw + alpha
                qr_world_x = robot_x + d * math.cos(direction)
                qr_world_y = robot_y + d * math.sin(direction)

                # 맵 기준 좌표
                if self.coord.initialized:
                    map_x, map_y = self.coord.world_to_map_bl(
                        qr_world_x, qr_world_y)
                else:
                    map_x = map_y = None

                # 최적 접근 포즈 (벽에서 10cm, 정면)
                # QR이 있는 벽의 법선 = 촬영 방향의 반대
                wall_dir = direction + math.pi
                approach_x = qr_world_x + 0.10 * math.cos(wall_dir)
                approach_y = qr_world_y + 0.10 * math.sin(wall_dir)
                approach_yaw = direction   # QR 방향을 바라봄
                approach = {
                    'world_x': round(approach_x,  4),
                    'world_y': round(approach_y,  4),
                    'yaw_rad': round(approach_yaw, 4),
                    'yaw_deg': round(math.degrees(approach_yaw), 1),
                }
            else:
                map_x = map_y = None

            result = {
                'qr_data':       qr_data,
                'timestamp':     ts_str,
                'snap_file':     raw_name,
                # QR 픽셀 정보
                'pixel_x':       round(qx, 1),
                'pixel_y':       round(qy, 1),
                'alpha_deg':     round(math.degrees(alpha), 2),
                # 로봇 포즈 (촬영 시점)
                'robot_world_x': round(robot_x,   4) if robot_x   else None,
                'robot_world_y': round(robot_y,   4) if robot_y   else None,
                'robot_yaw_deg': round(math.degrees(robot_yaw), 1) if robot_yaw else None,
                # QR 역산 좌표
                'qr_world_x':    round(qr_world_x, 4) if qr_world_x else None,
                'qr_world_y':    round(qr_world_y, 4) if qr_world_y else None,
                'qr_map_x':      round(map_x, 4) if map_x else None,
                'qr_map_y':      round(map_y, 4) if map_y else None,
                # 다음 노드용 최적 접근 포즈
                'approach_pose': approach,
                # 촬영 위치 정보
                'scan_pose': {
                    'world_x':  scan_pose.world_x,
                    'world_y':  scan_pose.world_y,
                    'yaw_deg':  scan_pose.yaw_deg,
                    'standoff': scan_pose.standoff_m,
                    'angle':    scan_pose.angle_to_wall,
                },
            }
            self.qr_results.append(result)

            # /qr/metadata 퍼블리시 (qr_db_crosscheck_node 연동)
            self.qr_meta_pub.publish(
                String(data=json.dumps(result, ensure_ascii=False))
            )

            self.get_logger().info(
                f'    [QR] "{qr_data}"  '
                f'α={math.degrees(alpha):.1f}°  '
                + (f'world=({qr_world_x:.3f},{qr_world_y:.3f})'
                   if qr_world_x else '좌표 없음')
            )

    # =========================================================================
    # 촬영 순서 정렬 — 시작점 최근접 포즈부터 시계방향(CW)
    # =========================================================================
    def _sort_poses_clockwise(
            self,
            poses: list[ScanPose],
            start_xy: tuple[float, float]) -> list[ScanPose]:
        """
        1. 전체 포즈의 무게중심(centroid) 계산
        2. 각 포즈의 centroid 기준 각도 계산
        3. 시작점에서 가장 가까운 포즈를 첫 번째로 설정
        4. 그 각도에서 시계방향(각도 감소, CW) 순으로 정렬
        """
        if len(poses) < 2:
            return poses

        cx = sum(p.world_x for p in poses) / len(poses)
        cy = sum(p.world_y for p in poses) / len(poses)

        angles = [math.atan2(p.world_y - cy, p.world_x - cx) for p in poses]

        # 시작점에서 가장 가까운 포즈
        sx, sy = start_xy
        start_idx = min(
            range(len(poses)),
            key=lambda i: math.hypot(poses[i].world_x - sx, poses[i].world_y - sy)
        )
        start_angle = angles[start_idx]

        # CW = start_angle 에서 각도가 줄어드는 방향
        # relative = (start_angle - angle) % 2π → 오름차순 = 시계방향
        def _cw_key(i):
            return (start_angle - angles[i]) % (2 * math.pi)

        sorted_poses = sorted(range(len(poses)), key=_cw_key)
        ordered = [poses[i] for i in sorted_poses]

        self.get_logger().info(
            f'[CW 정렬] 시작 포즈: #{start_idx+1} '
            f'({poses[start_idx].world_x:.2f}, {poses[start_idx].world_y:.2f})'
        )
        return ordered

    # =========================================================================
    # 카메라 lifecycle 제어
    # =========================================================================
    def _camera_transition(self, transition_id: int) -> bool:
        """lifecycle 상태 전이 요청 (비동기 future를 스레드에서 폴링)."""
        if not self._lc_change.wait_for_service(timeout_sec=3.0):
            self.get_logger().error('카메라 lifecycle 서비스 없음')
            return False
        req = ChangeState.Request()
        req.transition.id = transition_id
        future = self._lc_change.call_async(req)
        deadline = time.time() + 5.0
        while not future.done() and time.time() < deadline:
            time.sleep(0.05)
        if not future.done():
            self.get_logger().error(f'lifecycle transition {transition_id} 타임아웃')
            return False
        if not future.result().success:
            self.get_logger().error(f'lifecycle transition {transition_id} 거부됨')
            return False
        return True

    def _get_camera_state(self) -> str | None:
        """현재 카메라 lifecycle 상태 반환 ('unconfigured', 'inactive', 'active' 등)."""
        if not self._lc_get.wait_for_service(timeout_sec=2.0):
            return None
        future = self._lc_get.call_async(GetState.Request())
        deadline = time.time() + 3.0
        while not future.done() and time.time() < deadline:
            time.sleep(0.05)
        if not future.done():
            return None
        return future.result().current_state.label   # 'unconfigured'|'inactive'|'active'

    def _camera_on(self) -> bool:
        """카메라 activate (unconfigured→configure→inactive→activate)."""
        state = self._get_camera_state()
        if state is None:
            self.get_logger().error('카메라 상태 조회 실패')
            return False

        self.get_logger().debug(f'카메라 현재 상태: {state}')

        if state == 'unconfigured':
            if not self._camera_transition(Transition.TRANSITION_CONFIGURE):
                return False
            time.sleep(0.3)

        if state in ('unconfigured', 'inactive'):
            if not self._camera_transition(Transition.TRANSITION_ACTIVATE):
                return False
            time.sleep(0.3)

        # 이미지 구독 생성
        if self.img_sub is None:
            self.img_sub = self.create_subscription(
                CompressedImage,
                '/camera/image_raw/compressed',
                self._image_callback,
                rclpy.qos.qos_profile_sensor_data,
            )
        self.get_logger().info('카메라 ON (active)')
        return True

    def _camera_off(self):
        """카메라 deactivate + 구독 해제."""
        if self.img_sub is not None:
            self.destroy_subscription(self.img_sub)
            self.img_sub = None

        self._camera_transition(Transition.TRANSITION_DEACTIVATE)
        self.get_logger().info('카메라 OFF (inactive)')

    # =========================================================================
    # TF2 포즈 획득
    # =========================================================================
    def _get_pose(self, stamp=None) -> PoseStamped | None:
        try:
            tf_time = (rclpy.time.Time.from_msg(stamp)
                       if stamp else rclpy.time.Time())
            tf = self.tf_buffer.lookup_transform(
                'map', 'base_link', tf_time,
                timeout=rclpy.duration.Duration(seconds=0.5))
            p = PoseStamped()
            p.header           = tf.header
            p.pose.position.x  = tf.transform.translation.x
            p.pose.position.y  = tf.transform.translation.y
            p.pose.orientation = tf.transform.rotation
            return p
        except Exception:
            return None

    # =========================================================================
    # 결과 저장 (YAML + TXT)
    # =========================================================================
    def _save_results(self) -> str:
        """결과 저장 후 yaml 경로 반환."""
        # ── YAML ─────────────────────────────────────────────────────────────
        yaml_path = os.path.join(self.save_dir, 'qr_scan_results.yaml')
        snapped  = sum(1 for r in self.snap_records if r['snap_file'])
        doc = {
            'session_info': {
                'map_yaml':        self.map_yaml_path,
                'snapshots_dir':   self.save_dir,
                'standoff_max_m':  self.standoff_max,
                'standoff_min_m':  self.standoff_min,
                'total_poses':     len(self.scan_poses),
                'snapped':         snapped,
                'qr_detected':     len(self.qr_results),
            },
            # 위치별 스냅 기록 (snap_file=None이면 캡처 실패)
            'snap_records': self.snap_records,
            'qr_results': self.qr_results,
        }
        with open(yaml_path, 'w', encoding='utf-8') as f:
            yaml.dump(doc, f, allow_unicode=True, default_flow_style=False)

        # ── TXT (사람이 읽는 요약) ────────────────────────────────────────────
        txt_path = os.path.join(self.save_dir, 'qr_scan_results.txt')
        now_str  = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        lines    = [
            '=' * 60,
            '  QR 스냅샷 스캔 결과',
            f'  생성: {now_str}',
            f'  맵:   {self.map_yaml_path}',
            f'  촬영 위치: {len(self.scan_poses)}개 | QR 감지: {len(self.qr_results)}개',
            '=' * 60,
            '',
            '[ 촬영 계획 ]',
        ]
        for i, p in enumerate(self.scan_poses):
            diag = f' (사선 {p.angle_to_wall}°)' if p.angle_to_wall > 0 else ''
            lines.append(
                f'  [{i+1:02d}] x={p.world_x:7.3f}  y={p.world_y:7.3f}  '
                f'yaw={p.yaw_deg:6.1f}°  dist={p.standoff_m:.2f}m{diag}')
        lines += ['', '[ 감지된 QR ]']

        if not self.qr_results:
            lines.append('  (없음)')
        for r in self.qr_results:
            ap = r.get('approach_pose')
            lines += [
                f"  QR: {r['qr_data']}",
                f"    월드 좌표:      ({r.get('qr_world_x')}, {r.get('qr_world_y')})",
                f"    맵 기준 좌표:   ({r.get('qr_map_x')}, {r.get('qr_map_y')})",
                f"    카메라 각도:    {r.get('alpha_deg')}°",
            ]
            if ap:
                lines += [
                    f"    최적 접근 포즈:",
                    f"      x={ap['world_x']}  y={ap['world_y']}  "
                    f"yaw={ap['yaw_deg']}°",
                ]
            lines.append('')

        lines += ['-' * 60]
        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

        self.get_logger().info(
            f'결과 저장 완료:\n  {yaml_path}\n  {txt_path}')
        return yaml_path


# =============================================================================
def main(args=None):
    rclpy.init(args=args)
    node     = QRSnapshotNode()
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
