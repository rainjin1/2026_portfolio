#!/usr/bin/env python3
"""
qr_snapshot_node.py — 자율 QR 스캔 메인 노드 (TurtleBot3 실행)
================================================================
State Machine
-------------
WAITING_MAP → MAP_RECEIVED → WAITING_TF → PLANNING → NAVIGATING → DONE

  WAITING_MAP  : /map 토픽 수신 대기
  WAITING_TF   : TF2(map→base_link) 준비 대기 (최대 30초), AMCL 위치 확인
  PLANNING     : WallCoveragePlanner로 촬영 위치 생성 + 시계방향 정렬
  NAVIGATING   : cmd_vel P-컨트롤러로 순차 이동 + 촬영 (별도 스레드)
  DONE         : qr_scan_results.yaml 저장 + /qr/scan_complete 퍼블리시

Navigation (cmd_vel P-Controller)
----------------------------------
Nav2를 사용하지 않고 cmd_vel을 직접 퍼블리시.
  Phase 1 — 제자리 회전: |yaw_error| > 0.5 rad
  Phase 2 — 전진 + 조향: lx=min(0.12, 0.5·dist), az=clamp(1.2·err, ±0.8)
  Phase 3 — 최종 정렬:  dist < 0.12 m

Obstacle Handling
-----------------
  - 목표까지 0.4m 이내 → 장애물 무시 (벽 자체가 목적지)
  - 0.4m 이상에서 3초 차단 → 후퇴 0.2m → 재시도
  - 3초 안전 타이머: 전방위 < 0.13m → 즉시 정지

Camera Management
-----------------
  camera_ros는 lifecycle 서비스 미제공.
  create_subscription / destroy_subscription 으로 도착 시 ON, 캡처 후 OFF.

QR World Coordinate
-------------------
  alpha = atan2(pixel_x − 320, focal_len_px)     # 수평 각도 오프셋
  qr_x  = robot_x + standoff × cos(robot_yaw + alpha)

실행:
  ros2 run qr_wall_scan qr_snapshot_node --ros-args \\
    -p map_yaml_path:=/home/ubuntu22/map/0622_map_final.yaml \\
    -p poses_yaml_path:=/home/ubuntu22/map/scan_poses_0622.yaml \\
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
from sensor_msgs.msg import CompressedImage, LaserScan
from std_msgs.msg import String
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, Twist
import tf2_ros
from cv_bridge import CvBridge
from pyzbar import pyzbar

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


# ── 카메라 파라미터 ───────────────────────────────────────────────────────────
IMG_WIDTH    = 640
IMG_HEIGHT   = 480
FOV_DEG      = 62.2
FOCAL_LEN_PX = (IMG_WIDTH / 2.0) / math.tan(math.radians(FOV_DEG / 2.0))

# ── QR 화이트리스트 ───────────────────────────────────────────────────────────
WHITELIST = {
    'QR-001', 'QR-002', 'QR-003',
    'QR-CHEONAN', 'QR-PYEONGTAEK', 'QR-GONGJU', 'QR-ARRIVAL',
}

# ── cmd_vel P컨트롤러 파라미터 ────────────────────────────────────────────────
LINEAR_KP     = 0.5
ANGULAR_KP    = 1.2
LINEAR_MAX    = 0.12    # m/s
ANGULAR_MAX   = 0.8     # rad/s
DIST_THRESH   = 0.12    # m  — 목표 도달 판정 거리
ANGLE_THRESH  = 0.08    # rad (~4.6°)  — 각도 정렬 완료 판정
TURN_ONLY_ANG = 0.5     # rad (~28°)  — 이 이상 벌어지면 제자리 회전 후 전진
OBSTACLE_DIST = 0.30    # m  — 전방 장애물 정지 거리
NAV_TIMEOUT   = 90.0    # 초 — 포즈 당 최대 이동 시간


class QRSnapshotNode(Node):

    def __init__(self):
        super().__init__('qr_snapshot_node')

        # ── 파라미터 ──────────────────────────────────────────────────────────
        self.declare_parameter('map_yaml_path',  '/home/ubuntu22/map/0622_map_final.yaml')
        self.declare_parameter('standoff_max',   0.80)
        self.declare_parameter('standoff_min',   0.30)
        self.declare_parameter('capture_timeout', 3.0)
        self.declare_parameter('poses_yaml_path', '')

        self.map_yaml_path   = self.get_parameter('map_yaml_path').value
        self.standoff_max    = self.get_parameter('standoff_max').value
        self.standoff_min    = self.get_parameter('standoff_min').value
        self.capture_timeout = self.get_parameter('capture_timeout').value
        self.poses_yaml_path = self.get_parameter('poses_yaml_path').value
        self.map_pgm_path    = self.map_yaml_path.replace('.yaml', '.pgm')

        # 저장 폴더: 맵 yaml 옆 snapshots/
        self.save_dir = os.path.join(
            os.path.dirname(os.path.abspath(self.map_yaml_path)), 'snapshots')
        os.makedirs(self.save_dir, exist_ok=True)

        # ── 내부 상태 ─────────────────────────────────────────────────────────
        self.state        = 'WAITING_MAP'
        self.coord        = MapCoordSystem()
        self.bridge       = CvBridge()
        self._nav_thread  = None
        self._nav_lock    = threading.Lock()

        # 촬영 제어
        self._capture_ready  = False
        self._captured_frame = None
        self._capture_lock   = threading.Lock()
        self._snap_idx       = 0
        self._last_snap_file = None

        # 장애물 감지
        self._obstacle_ahead = False
        self._scan_lock      = threading.Lock()
        self._last_scan      = None          # 최신 scan 메시지 저장용

        # AMCL 위치 보정
        self._amcl_pose      = None          # 최신 AMCL 포즈
        self._amcl_lock      = threading.Lock()

        # 결과
        self.scan_poses: list[ScanPose] = []
        self.qr_results: list[dict]    = []
        self.snap_records: list[dict]  = []

        # ── TF2 ───────────────────────────────────────────────────────────────
        self.tf_buffer   = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)

        # ── /map 구독 ─────────────────────────────────────────────────────────
        map_qos = rclpy.qos.QoSProfile(
            depth=1,
            durability=rclpy.qos.DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=rclpy.qos.ReliabilityPolicy.RELIABLE,
        )
        self.map_sub = self.create_subscription(
            OccupancyGrid, '/map', self._map_callback, map_qos)

        # ── /scan 구독 (최신 메시지 저장) ────────────────────────────────────
        self.scan_sub = self.create_subscription(
            LaserScan, '/scan', self._scan_callback,
            rclpy.qos.qos_profile_sensor_data)

        # ── /amcl_pose 구독 (위치 보정) ──────────────────────────────────────
        self.amcl_sub = self.create_subscription(
            PoseWithCovarianceStamped, '/amcl_pose',
            self._amcl_callback, 10)

        # ── 3초 안전 체크 타이머 ──────────────────────────────────────────────
        self.safety_timer = self.create_timer(3.0, self._safety_check)

        # ── 카메라 구독 (도착 시에만 생성/삭제) ──────────────────────────────
        self.img_sub = None

        # ── 퍼블리셔 ─────────────────────────────────────────────────────────
        self.cmd_pub           = self.create_publisher(Twist, '/cmd_vel', 10)
        self.qr_meta_pub       = self.create_publisher(String, '/qr/metadata', 10)
        self.scan_complete_pub = self.create_publisher(String, '/qr/scan_complete', 10)

        # ── 상태 루프 (1Hz) ──────────────────────────────────────────────────
        self.state_timer = self.create_timer(1.0, self._state_loop)

        self.get_logger().info(
            f'QR 스냅샷 노드 시작 [WAITING_MAP]\n'
            f'  맵:     {self.map_yaml_path}\n'
            f'  standoff: {self.standoff_min}~{self.standoff_max}m\n'
            f'  저장:   {self.save_dir}\n'
            f'  이동:   cmd_vel P컨트롤러 (Nav2 미사용)'
        )

    # =========================================================================
    # /scan 콜백 — 최신 메시지 저장만 (처리는 safety_check에서)
    # =========================================================================
    def _scan_callback(self, msg: LaserScan):
        with self._scan_lock:
            self._last_scan = msg

    # =========================================================================
    # /amcl_pose 콜백 — 위치 보정 반영
    # =========================================================================
    def _amcl_callback(self, msg: PoseWithCovarianceStamped):
        with self._amcl_lock:
            self._amcl_pose = msg

    # =========================================================================
    # 3초 안전 체크 타이머
    # =========================================================================
    def _safety_check(self):
        if self.state not in ('NAVIGATING', 'PLANNING'):
            return

        with self._scan_lock:
            scan = self._last_scan
        if scan is None:
            return

        n = len(scan.ranges)
        if n == 0:
            return

        # ── 전방 ±20° 장애물 감지 ────────────────────────────────────────────
        front_idx = int(20 / 360.0 * n)
        front_indices = list(range(0, front_idx + 1)) + list(range(n - front_idx, n))
        front_valid = [scan.ranges[i] for i in front_indices
                       if 0.05 < scan.ranges[i] < float('inf')
                       and not math.isnan(scan.ranges[i])]
        front_min = min(front_valid) if front_valid else float('inf')

        with self._scan_lock:
            self._obstacle_ahead = front_min < OBSTACLE_DIST

        # ── 전방위 최솟값 (긴급 정지) ────────────────────────────────────────
        all_valid = [r for r in scan.ranges
                     if 0.05 < r < float('inf') and not math.isnan(r)]
        global_min = min(all_valid) if all_valid else float('inf')

        if global_min < 0.13:   # 로봇 반경 이내 → 즉시 정지
            self._publish_cmd(0.0, 0.0)
            self.get_logger().warn(
                f'[긴급정지] 전방위 장애물 {global_min:.2f}m — 정지')

        # ── AMCL 위치 보정 상태 + 현재 위치 로그 ────────────────────────────
        with self._amcl_lock:
            amcl = self._amcl_pose

        pose = self._get_pose()
        pos_str = (f'({pose.pose.position.x:.3f}, {pose.pose.position.y:.3f})'
                   if pose else '(TF없음)')

        cov_str = ''
        if amcl:
            cov_xx = amcl.pose.covariance[0]   # x 분산
            cov_yy = amcl.pose.covariance[7]   # y 분산
            cov_str = f' | AMCL 신뢰도 σx={math.sqrt(cov_xx):.3f} σy={math.sqrt(cov_yy):.3f}'

        self.get_logger().info(
            f'[안전체크] 위치={pos_str} | '
            f'전방={front_min:.2f}m | '
            f'전방위최솟={global_min:.2f}m'
            + cov_str
        )

    # =========================================================================
    # /map 콜백
    # =========================================================================
    def _map_callback(self, msg: OccupancyGrid):
        self.coord.update_from_occupancy_grid(msg)
        if self.state == 'WAITING_MAP':
            b = self.coord.get_bounds()
            self.get_logger().info(
                f'[WAITING_MAP → MAP_RECEIVED] /map 수신\n'
                f'  맵 크기: {b.max_x:.2f} x {b.max_y:.2f} m'
            )
            self.state = 'MAP_RECEIVED'

    # =========================================================================
    # 상태머신 (1Hz)
    # =========================================================================
    def _state_loop(self):
        if self.state == 'MAP_RECEIVED':
            self.state = 'WAITING_TF'
            threading.Thread(target=self._plan_and_navigate, daemon=True).start()

    # =========================================================================
    # 촬영 계획 + 이동 시작
    # =========================================================================
    def _plan_and_navigate(self):
        # ── TF2 준비 대기 (최대 30초) ────────────────────────────────────────
        self.get_logger().info('[WAITING_TF] localization TF2 대기 중...')
        deadline = time.time() + 30.0
        start_xy = None
        while time.time() < deadline:
            pose = self._get_pose()
            if pose:
                start_xy = (pose.pose.position.x, pose.pose.position.y)
                self.get_logger().info(
                    f'[WAITING_TF → PLANNING] 현재 위치 확인: '
                    f'({start_xy[0]:.3f}, {start_xy[1]:.3f})')
                break
            time.sleep(1.0)

        if start_xy is None:
            self.get_logger().warn('TF2 30초 타임아웃 — (0,0) 사용 (localization 확인 필요)')
            start_xy = (0.0, 0.0)

        self.state = 'PLANNING'

        # 촬영 위치 로드
        if self.poses_yaml_path and os.path.isfile(self.poses_yaml_path):
            self.get_logger().info(f'사전 계산 좌표 로드: {self.poses_yaml_path}')
            try:
                self.scan_poses = _load_precomputed_poses(self.poses_yaml_path)
                self.get_logger().info(f'  → {len(self.scan_poses)}개 포즈 로드 완료')
            except Exception as e:
                self.get_logger().error(f'좌표 파일 로드 실패: {e} — 동적 계획으로 fallback')
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

        self.get_logger().info(f'촬영 위치 {len(self.scan_poses)}개 생성 완료')

        # 시계방향 정렬
        self.scan_poses = self._sort_poses_clockwise(self.scan_poses, start_xy)
        self.get_logger().info(
            f'촬영 순서 확정: 시작점({start_xy[0]:.2f},{start_xy[1]:.2f})에서 '
            f'가장 가까운 지점부터 시계방향')

        # 이동 스레드 시작
        self._nav_thread = threading.Thread(
            target=self._navigation_worker, daemon=True)
        self._nav_thread.start()
        self.state = 'NAVIGATING'

    # =========================================================================
    # 촬영 순서 정렬 — 시작점 최근접 포즈부터 시계방향(CW)
    # =========================================================================
    def _sort_poses_clockwise(
            self,
            poses: list[ScanPose],
            start_xy: tuple[float, float]) -> list[ScanPose]:
        """
        촬영 위치를 시작점 최근접 → 시계방향 순으로 정렬.

        알고리즘
        --------
        1. 모든 포즈의 무게중심(cx, cy) 계산
        2. 각 포즈의 중심 기준 각도(atan2) 계산
        3. 시작점에 가장 가까운 포즈를 기준점으로 선택
        4. 기준점 각도에서 시계방향(감소 방향)으로 정렬

        시계방향 키: (start_angle - angle) % 2π  (값이 작을수록 먼저)
        """
        if len(poses) < 2:
            return poses

        cx = sum(p.world_x for p in poses) / len(poses)
        cy = sum(p.world_y for p in poses) / len(poses)
        angles = [math.atan2(p.world_y - cy, p.world_x - cx) for p in poses]

        sx, sy = start_xy
        start_idx = min(
            range(len(poses)),
            key=lambda i: math.hypot(poses[i].world_x - sx, poses[i].world_y - sy)
        )
        start_angle = angles[start_idx]

        def _cw_key(i):
            return (start_angle - angles[i]) % (2 * math.pi)

        ordered = [poses[i] for i in sorted(range(len(poses)), key=_cw_key)]
        self.get_logger().info(
            f'[CW 정렬] 시작 포즈: #{start_idx+1} '
            f'({poses[start_idx].world_x:.2f}, {poses[start_idx].world_y:.2f})'
        )
        return ordered

    # =========================================================================
    # 네비게이션 워커 (별도 스레드)
    # =========================================================================
    def _navigation_worker(self):
        self.get_logger().info('이동 시작 (cmd_vel 직접 제어)')
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

            record = {
                'pose_idx':      i + 1,
                'world_x':       scan_pose.world_x,
                'world_y':       scan_pose.world_y,
                'yaw_rad':       scan_pose.yaw_rad,
                'yaw_deg':       scan_pose.yaw_deg,
                'standoff_m':    scan_pose.standoff_m,
                'angle_to_wall': scan_pose.angle_to_wall,
                'snap_file':     None,
                'nav_success':   False,
            }

            nav_ok = self._goto_pose(
                scan_pose.world_x, scan_pose.world_y, scan_pose.yaw_rad)

            if not nav_ok:
                self.get_logger().warn(f'  [{i+1}] 도달 실패 (타임아웃) — 다음으로 진행')
                self.snap_records.append(record)
                continue

            record['nav_success'] = True
            self.get_logger().info(f'  [{i+1}] 도착 완료 → 카메라 ON')

            self._camera_on()
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

        # 정지
        self._publish_cmd(0.0, 0.0)

        self.state = 'DONE'
        yaml_out = self._save_results()
        self.get_logger().info(
            f'[완료] 총 {total}개 위치 순회 | '
            f'QR {len(self.qr_results)}개 감지 | '
            f'저장: {self.save_dir}'
        )
        self.scan_complete_pub.publish(String(data=yaml_out))

    # =========================================================================
    # cmd_vel P컨트롤러 이동
    # =========================================================================
    def _goto_pose(self, tx: float, ty: float, tyaw: float) -> bool:
        """
        목표 포즈(tx, ty, tyaw)까지 P-컨트롤러로 이동. 20Hz 루프.

        Parameters
        ----------
        tx, ty : float  목표 위치 (map 프레임, 미터)
        tyaw   : float  목표 방향 (rad)

        Returns
        -------
        bool  True=도달 성공, False=NAV_TIMEOUT(90s) 초과

        Obstacle Logic
        --------------
        벽이 목적지이므로 근거리 장애물을 무조건 정지로 처리하면 안 됨.
        - dist > NEAR_DIST(0.4m) + 장애물 감지 → 정지 대기
        - 3초 이상 지속 → _backup() 후 재시도
        - dist ≤ NEAR_DIST → 장애물 무시하고 계속 전진
        """
        start            = time.time()
        obstacle_since   = None   # 장애물 최초 감지 시각
        NEAR_DIST        = 0.40   # 이 거리 이내면 장애물 무시
        OBSTACLE_TIMEOUT = 3.0    # 초 이상 막히면 후퇴
        BACKUP_DIST      = 0.20   # 후퇴 거리 (m)

        while time.time() - start < NAV_TIMEOUT:
            pose = self._get_pose()
            if pose is None:
                time.sleep(0.1)
                continue

            cx   = pose.pose.position.x
            cy   = pose.pose.position.y
            q    = pose.pose.orientation
            cyaw = math.atan2(
                2.0 * (q.w * q.z + q.x * q.y),
                1.0 - 2.0 * (q.y * q.y + q.z * q.z)
            )

            dx   = tx - cx
            dy   = ty - cy
            dist = math.hypot(dx, dy)

            # ── 장애물 처리 ───────────────────────────────────────────────
            with self._scan_lock:
                obstacle = self._obstacle_ahead

            if obstacle and dist > NEAR_DIST:
                # 목표까지 멀 때만 장애물 정지
                if obstacle_since is None:
                    obstacle_since = time.time()
                    self.get_logger().warn(
                        f'장애물 감지 (목표까지 {dist:.2f}m) — 정지 대기')
                    self._publish_cmd(0.0, 0.0)

                elif time.time() - obstacle_since > OBSTACLE_TIMEOUT:
                    # 3초 이상 막힘 → 후퇴
                    self.get_logger().warn('장애물 지속 → 후퇴 후 재시도')
                    self._backup(BACKUP_DIST)
                    obstacle_since = None

                else:
                    self._publish_cmd(0.0, 0.0)

                time.sleep(0.1)
                continue
            else:
                obstacle_since = None   # 장애물 해소 또는 목표 근처

            # ── 이동 제어 ─────────────────────────────────────────────────
            if dist < DIST_THRESH:
                # Phase 3: 최종 yaw 정렬
                yaw_err = self._normalize_angle(tyaw - cyaw)
                if abs(yaw_err) < ANGLE_THRESH:
                    self._publish_cmd(0.0, 0.0)
                    return True
                az = max(-ANGULAR_MAX, min(ANGULAR_MAX, ANGULAR_KP * yaw_err))
                self._publish_cmd(0.0, az)
            else:
                bearing   = math.atan2(dy, dx)
                angle_err = self._normalize_angle(bearing - cyaw)

                if abs(angle_err) > TURN_ONLY_ANG:
                    # Phase 1: 제자리 회전
                    az = max(-ANGULAR_MAX, min(ANGULAR_MAX, ANGULAR_KP * angle_err))
                    self._publish_cmd(0.0, az)
                else:
                    # Phase 2: 전진 + 조향
                    lx = min(LINEAR_MAX, LINEAR_KP * dist)
                    az = max(-ANGULAR_MAX, min(ANGULAR_MAX, ANGULAR_KP * angle_err))
                    self._publish_cmd(lx, az)

            time.sleep(0.05)   # 20 Hz

        self._publish_cmd(0.0, 0.0)
        return False

    def _backup(self, dist_m: float):
        """장애물 회피용 후퇴."""
        duration = dist_m / LINEAR_MAX
        t_start  = time.time()
        while time.time() - t_start < duration:
            self._publish_cmd(-LINEAR_MAX * 0.7, 0.0)
            time.sleep(0.05)
        self._publish_cmd(0.0, 0.0)
        time.sleep(0.3)

    @staticmethod
    def _normalize_angle(a: float) -> float:
        while a >  math.pi: a -= 2 * math.pi
        while a < -math.pi: a += 2 * math.pi
        return a

    def _publish_cmd(self, lx: float, az: float):
        msg = Twist()
        msg.linear.x  = float(lx)
        msg.angular.z = float(az)
        self.cmd_pub.publish(msg)

    # =========================================================================
    # 카메라 구독 제어
    # =========================================================================
    def _camera_on(self) -> bool:
        if self.img_sub is None:
            self.img_sub = self.create_subscription(
                CompressedImage,
                '/camera/image_raw/compressed',
                self._image_callback,
                rclpy.qos.qos_profile_sensor_data,
            )
        self.get_logger().info('카메라 구독 ON')
        return True

    def _camera_off(self):
        if self.img_sub is not None:
            self.destroy_subscription(self.img_sub)
            self.img_sub = None
        self.get_logger().info('카메라 구독 OFF')

    # =========================================================================
    # 사진 캡처 대기
    # =========================================================================
    def _wait_for_capture(self, scan_pose: ScanPose) -> bool:
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

        self._snap_idx += 1
        self._process_frame(frame, scan_pose, self._snap_idx)
        return True

    # =========================================================================
    # 이미지 콜백
    # =========================================================================
    def _image_callback(self, msg: CompressedImage):
        with self._capture_lock:
            if not self._capture_ready or self._captured_frame is not None:
                return
        try:
            frame = self.bridge.compressed_imgmsg_to_cv2(msg, desired_encoding='bgr8')
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
        캡처된 프레임에서 QR 감지 + 월드 좌표 역산 + 결과 저장.

        QR 월드 좌표 계산
        -----------------
        focal_len_px = (IMG_WIDTH/2) / tan(FOV/2)  ≈ 554 px
        alpha        = atan2(pixel_x − w/2, focal_len_px)   # 수평 각도 오프셋
        direction    = robot_yaw + alpha
        qr_world_x   = robot_x + standoff_m × cos(direction)
        qr_world_y   = robot_y + standoff_m × sin(direction)

        접근 포즈 (approach_pose)
        -------------------------
        wall_dir   = direction + π              # 촬영 방향 반대 = 벽 법선
        approach_x = robot_x + 0.10 × cos(wall_dir)
        approach_y = robot_y + 0.10 × sin(wall_dir)
        (재촬영 목적의 최적 위치 — qr_database_node에서 0.30m로 재계산)
        """
        pose_msg = self._get_pose()
        robot_x = robot_y = robot_yaw = None

        if pose_msg:
            robot_x = pose_msg.pose.position.x
            robot_y = pose_msg.pose.position.y
            q = pose_msg.pose.orientation
            robot_yaw = math.atan2(
                2 * (q.w * q.z + q.x * q.y),
                1 - 2 * (q.y * q.y + q.z * q.z)
            )

        # 이미지 저장 (넘버링)
        ts_str   = datetime.now().strftime('%Y%m%d_%H%M%S')
        raw_name = f'snap_{snap_idx:03d}_{ts_str}.jpg'
        raw_path = os.path.join(self.save_dir, raw_name)
        cv2.imwrite(raw_path, frame)
        self._last_snap_file = raw_name

        # QR 감지
        h, w = frame.shape[:2]
        qr_list = [d for d in pyzbar.decode(frame) if d.type == 'QRCODE']

        for qr in qr_list:
            qr_data = qr.data.decode('utf-8').strip()
            if not qr_data or qr_data not in WHITELIST:
                continue

            qx = qr.rect.left + qr.rect.width  / 2.0
            qy = qr.rect.top  + qr.rect.height / 2.0
            alpha = math.atan2(qx - w / 2.0, FOCAL_LEN_PX)

            qr_world_x = qr_world_y = None
            approach   = None

            if robot_x is not None:
                d         = scan_pose.standoff_m
                direction = robot_yaw + alpha
                qr_world_x = robot_x + d * math.cos(direction)
                qr_world_y = robot_y + d * math.sin(direction)

                if self.coord.initialized:
                    map_x, map_y = self.coord.world_to_map_bl(qr_world_x, qr_world_y)
                else:
                    map_x = map_y = None

                wall_dir  = direction + math.pi
                approach  = {
                    'world_x': round(robot_x + 0.10 * math.cos(wall_dir), 4),
                    'world_y': round(robot_y + 0.10 * math.sin(wall_dir), 4),
                    'yaw_rad': round(direction, 4),
                    'yaw_deg': round(math.degrees(direction), 1),
                }
            else:
                map_x = map_y = None

            result = {
                'qr_data':       qr_data,
                'timestamp':     ts_str,
                'snap_file':     raw_name,
                'pixel_x':       round(qx, 1),
                'pixel_y':       round(qy, 1),
                'alpha_deg':     round(math.degrees(alpha), 2),
                'robot_world_x': round(robot_x,   4) if robot_x   else None,
                'robot_world_y': round(robot_y,   4) if robot_y   else None,
                'robot_yaw_deg': round(math.degrees(robot_yaw), 1) if robot_yaw else None,
                'qr_world_x':    round(qr_world_x, 4) if qr_world_x else None,
                'qr_world_y':    round(qr_world_y, 4) if qr_world_y else None,
                'qr_map_x':      round(map_x, 4) if map_x else None,
                'qr_map_y':      round(map_y, 4) if map_y else None,
                'approach_pose': approach,
                'scan_pose': {
                    'world_x':  scan_pose.world_x,
                    'world_y':  scan_pose.world_y,
                    'yaw_deg':  scan_pose.yaw_deg,
                    'standoff': scan_pose.standoff_m,
                    'angle':    scan_pose.angle_to_wall,
                },
            }
            self.qr_results.append(result)

            self.qr_meta_pub.publish(
                String(data=json.dumps(result, ensure_ascii=False)))

            self.get_logger().info(
                f'    [QR] "{qr_data}"  α={math.degrees(alpha):.1f}°  '
                + (f'world=({qr_world_x:.3f},{qr_world_y:.3f})'
                   if qr_world_x else '좌표 없음')
            )

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
    # 결과 저장
    # =========================================================================
    def _save_results(self) -> str:
        yaml_path = os.path.join(self.save_dir, 'qr_scan_results.yaml')
        snapped   = sum(1 for r in self.snap_records if r['snap_file'])
        doc = {
            'session_info': {
                'map_yaml':       self.map_yaml_path,
                'snapshots_dir':  self.save_dir,
                'standoff_max_m': self.standoff_max,
                'standoff_min_m': self.standoff_min,
                'total_poses':    len(self.scan_poses),
                'snapped':        snapped,
                'qr_detected':    len(self.qr_results),
            },
            'snap_records': self.snap_records,
            'qr_results':   self.qr_results,
        }
        with open(yaml_path, 'w', encoding='utf-8') as f:
            yaml.dump(doc, f, allow_unicode=True, default_flow_style=False)

        txt_path = os.path.join(self.save_dir, 'qr_scan_results.txt')
        now_str  = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        lines    = [
            '=' * 60,
            '  QR 스냅샷 스캔 결과',
            f'  생성: {now_str}',
            f'  맵:   {self.map_yaml_path}',
            f'  촬영 위치: {len(self.scan_poses)}개 | 캡처: {snapped}개 | QR 감지: {len(self.qr_results)}개',
            '=' * 60, '',
            '[ 촬영 계획 ]',
        ]
        for r in self.snap_records:
            diag = f' (사선 {r["angle_to_wall"]}°)' if r['angle_to_wall'] > 0 else ''
            nav  = '✅' if r['nav_success'] else '❌'
            snap = r['snap_file'] or '(캡처없음)'
            lines.append(
                f'  [{r["pose_idx"]:02d}] {nav} x={r["world_x"]:7.3f}  y={r["world_y"]:7.3f}  '
                f'yaw={r["yaw_deg"]:6.1f}°  {snap}{diag}')

        lines += ['', '[ 감지된 QR ]']
        if not self.qr_results:
            lines.append('  (없음)')
        for r in self.qr_results:
            ap = r.get('approach_pose')
            lines += [
                f"  QR: {r['qr_data']}",
                f"    월드 좌표:    ({r.get('qr_world_x')}, {r.get('qr_world_y')})",
                f"    맵 기준:     ({r.get('qr_map_x')}, {r.get('qr_map_y')})",
            ]
            if ap:
                lines.append(
                    f"    접근 포즈:   x={ap['world_x']}  y={ap['world_y']}  yaw={ap['yaw_deg']}°")
            lines.append('')

        lines.append('-' * 60)
        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

        self.get_logger().info(f'결과 저장 완료:\n  {yaml_path}\n  {txt_path}')
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
