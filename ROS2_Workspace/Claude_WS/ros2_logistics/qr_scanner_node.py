#!/usr/bin/env python3
"""
[Stage 2] QR Scanner Node — TurtleBot3 (v2)
=============================================
[v1 → v2 핵심 변경사항]
  문제: v1은 map.yaml 파일을 직접 읽어 좌표 변환 → 탐색 중엔 파일 없음, 세션 불일치 위험
  해결:
    1. /map 토픽(OccupancyGrid)을 primary source로 사용 — 항상 live 데이터
    2. 시작 시 3단계 동기화 상태머신으로 준비 완료 후에만 QR 처리
       WAITING_MAP → WAITING_TF → READY
    3. 준비 완료 시 로봇 시작 위치를 월드좌표/맵기준좌표 둘 다 기록
    4. 매 QR 감지 시 범위 검증 수행
    5. map.yaml이 있으면 선택적 교차검증 수행

[동기화 흐름]
  노드 시작
    │
    ├─ [WAITING_MAP]  /map 토픽 대기 ─────────────── 타임아웃 30초
    │       ↓ OccupancyGrid 수신
    ├─ [WAITING_TF]   map→base_link TF2 대기 ──────── 타임아웃 30초
    │       ↓ TF 변환 성공
    ├─ [READY]        로봇 초기 위치 기록 + map.yaml 교차검증
    │       ↓
    └─ QR 스캔 처리 시작

의존성:
  sudo apt install ros-humble-cv-bridge ros-humble-tf2-ros ros-humble-tf2-geometry-msgs
  pip install pyzbar pillow --break-system-packages
  sudo apt install libzbar0

실행:
  ros2 run <패키지명> qr_scanner_node --ros-args \\
    -p map_yaml_path:=/home/ubuntu/maps/map.yaml \\
    -p save_dir:=/home/ubuntu/qr_scans
"""

import rclpy
from rclpy.node import Node
from nav_msgs.msg import OccupancyGrid
from sensor_msgs.msg import Image, CompressedImage
from std_msgs.msg import String
from geometry_msgs.msg import PoseStamped
from cv_bridge import CvBridge
import tf2_ros
import tf2_geometry_msgs  # noqa: F401

import cv2
import json
import yaml
import numpy
import time
import os
from datetime import datetime
from enum import Enum, auto
from pyzbar import pyzbar

from map_coord_utils import MapCoordSystem


# ── 시작 동기화 상태 ──────────────────────────────────────────────────────────
class StartupState(Enum):
    WAITING_MAP = auto()   # /map 토픽 수신 대기
    WAITING_TF  = auto()   # TF2 map→base_link 대기
    READY       = auto()   # 동기화 완료, QR 처리 활성


# ── 타임아웃 설정 (초) ────────────────────────────────────────────────────────
TIMEOUT_MAP_SEC = 30.0
TIMEOUT_TF_SEC  = 30.0


class QRScannerNode(Node):
    def __init__(self):
        super().__init__('qr_scanner_node')

        # ── 파라미터 ──────────────────────────────────────────────────────────
        self.declare_parameter('map_yaml_path',    '')      # 선택. 교차검증용
        self.declare_parameter('scan_cooldown_sec', 3.0)
        self.declare_parameter('save_dir',         '/tmp/qr_scans')

        self.map_yaml_path = self.get_parameter('map_yaml_path').value
        self.scan_cooldown = self.get_parameter('scan_cooldown_sec').value
        self.save_dir      = self.get_parameter('save_dir').value
        os.makedirs(self.save_dir, exist_ok=True)

        # ── 내부 상태 ─────────────────────────────────────────────────────────
        self.startup_state: StartupState = StartupState.WAITING_MAP
        self.startup_time:  float        = self.get_clock().now().nanoseconds * 1e-9
        self.coord = MapCoordSystem()   # 좌표계 관리 (v2 방식)

        self.bridge    = CvBridge()
        self.last_scan: dict[str, float] = {}
        self.scan_results: list[dict]    = []

        # ── TF2 ───────────────────────────────────────────────────────────────
        self.tf_buffer   = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)

        # ── Subscribers ───────────────────────────────────────────────────────
        # 1순위: /map 토픽 — 라이브 맵 메타데이터
        self.map_sub = self.create_subscription(
            OccupancyGrid, '/map', self.map_callback, 1)

        # 카메라 — READY 상태일 때만 처리
        self.image_sub = self.create_subscription(
            Image, '/image_raw', self.image_callback, 10)

        # ── Publishers ────────────────────────────────────────────────────────
        self.qr_image_pub = self.create_publisher(CompressedImage, '/qr/capture_image', 10)
        self.qr_meta_pub  = self.create_publisher(String, '/qr/metadata', 10)

        # ── 주기적 동기화 체크 타이머 (1초마다) ──────────────────────────────
        self.sync_timer = self.create_timer(1.0, self._sync_check_loop)

        self.get_logger().info(
            "QR Scanner Node 시작 [WAITING_MAP] — /map 토픽 대기 중..."
        )

    # =========================================================================
    # 1단계: /map 콜백 — 라이브 맵 메타데이터 수신
    # =========================================================================
    def map_callback(self, msg: OccupancyGrid):
        """
        /map 토픽 수신 시 호출.
        SLAM 중에는 맵이 커질 수 있으므로 매번 좌표계를 갱신.
        """
        was_init = self.coord.initialized
        self.coord.update_from_occupancy_grid(msg)

        # 처음 맵 수신 시 상태 전이 및 상세 로그
        if self.startup_state == StartupState.WAITING_MAP:
            self.startup_state = StartupState.WAITING_TF
            self.get_logger().info(
                f"\n{'='*55}\n"
                f"[WAITING_MAP → WAITING_TF] /map 토픽 수신 완료\n"
                f"{self.coord.summary()}"
                f"\n  다음: TF2 map→base_link 변환 대기 중...\n"
                f"{'='*55}"
            )

        # SLAM 중 맵 크기 변화 감지 (정보 로그)
        elif was_init and self.startup_state == StartupState.READY:
            bounds = self.coord.get_bounds()
            self.get_logger().info(
                f"[맵 갱신] 크기: {bounds.max_x:.2f}x{bounds.max_y:.2f}m  "
                f"origin=({self.coord._origin_x:.4f},{self.coord._origin_y:.4f})",
                throttle_duration_sec=5.0
            )

    # =========================================================================
    # 2단계: 동기화 체크 루프 — TF 대기 및 준비 완료 판정
    # =========================================================================
    def _sync_check_loop(self):
        """1초마다 동기화 상태를 점검."""
        now = self.get_clock().now().nanoseconds * 1e-9
        elapsed = now - self.startup_time

        # ── WAITING_MAP: 타임아웃 체크 ────────────────────────────────────────
        if self.startup_state == StartupState.WAITING_MAP:
            if elapsed > TIMEOUT_MAP_SEC:
                self.get_logger().error(
                    f"[TIMEOUT] {TIMEOUT_MAP_SEC}초 동안 /map 토픽 미수신.\n"
                    f"  SLAM(slam_toolbox) 또는 map_server가 실행 중인지 확인하세요.\n"
                    f"  명령: ros2 topic echo /map --once"
                )
            else:
                self.get_logger().info(
                    f"[WAITING_MAP] /map 대기 중... ({elapsed:.0f}s / {TIMEOUT_MAP_SEC}s)",
                    throttle_duration_sec=5.0
                )
            return

        # ── WAITING_TF: TF2 변환 시도 ─────────────────────────────────────────
        if self.startup_state == StartupState.WAITING_TF:
            pose = self._try_get_pose()

            if pose is None:
                if elapsed > TIMEOUT_MAP_SEC + TIMEOUT_TF_SEC:
                    self.get_logger().error(
                        f"[TIMEOUT] TF2 map→base_link 변환 {TIMEOUT_TF_SEC}초 이상 실패.\n"
                        f"  SLAM 또는 AMCL이 실행 중이고 초기 포즈가 설정되었는지 확인하세요.\n"
                        f"  명령: ros2 run tf2_tools view_frames"
                    )
                else:
                    self.get_logger().info(
                        "[WAITING_TF] TF2 map→base_link 대기 중...",
                        throttle_duration_sec=3.0
                    )
                return

            # TF 성공 → 로봇 초기 위치 기록 + 검증
            self._finalize_startup(pose)
            return

        # ── READY: 주기적 범위 검증 ───────────────────────────────────────────
        if self.startup_state == StartupState.READY:
            # 5초마다 현재 위치 범위 체크 (경고용)
            pose = self._try_get_pose()
            if pose is not None:
                try:
                    mx, my = self.coord.world_to_map_bl(
                        pose.pose.position.x, pose.pose.position.y
                    )
                    if not self.coord.is_within_bounds(mx, my):
                        self.get_logger().warn(
                            f"[경고] 로봇이 맵 경계 밖에 있습니다!\n"
                            f"  현재 맵 기준 좌표: ({mx:.3f}, {my:.3f})\n"
                            f"  유효 범위: {self.coord.get_bounds()}\n"
                            f"  AMCL 초기 포즈 또는 SLAM 상태를 확인하세요."
                        )
                except RuntimeError:
                    pass

    # =========================================================================
    # 준비 완료 처리 — 로봇 초기 위치 기록 + 교차검증
    # =========================================================================
    def _finalize_startup(self, initial_pose: PoseStamped):
        """
        동기화 완료 시 1회 호출.
        로봇의 임의 시작 위치를 월드/맵 두 좌표계 모두 기록하고
        map.yaml 교차검증을 수행.
        """
        wx = initial_pose.pose.position.x
        wy = initial_pose.pose.position.y

        # 로봇 시작 위치 기록 (MapCoordSystem에 저장)
        start_info = self.coord.record_robot_start(wx, wy)

        # 범위 검증
        if not start_info['in_bounds']:
            bounds = self.coord.get_bounds()
            self.get_logger().error(
                f"\n{'!'*55}\n"
                f"[동기화 실패] 로봇 시작 위치가 맵 범위 밖에 있습니다!\n"
                f"  로봇 위치 (맵 기준): "
                f"({start_info['map_bl'][0]:.4f}, {start_info['map_bl'][1]:.4f}) m\n"
                f"  유효 범위: {bounds}\n"
                f"  원인:\n"
                f"    - AMCL 초기 포즈가 맵 밖에 설정되었거나\n"
                f"    - SLAM 중 원점이 음수 방향으로 이동했을 가능성\n"
                f"  해결:\n"
                f"    - RViz에서 '2D Pose Estimate'로 올바른 초기 위치 재설정\n"
                f"    - 또는 map_yaml_path 파라미터를 현재 세션의 맵으로 설정\n"
                f"{'!'*55}"
            )
            # 범위 밖이어도 READY로 전환 (SLAM 중엔 맵이 커질 수 있음)
            # 단, 경고를 명확히 남김

        # map.yaml 교차검증 (경로가 설정된 경우)
        yaml_check_msg = "(map.yaml 경로 미설정 — 교차검증 건너뜀)"
        if self.map_yaml_path:
            ok, yaml_check_msg = self.coord.cross_check_yaml(self.map_yaml_path)
            if not ok:
                self.get_logger().warn(
                    f"\n{'*'*55}\n"
                    f"{yaml_check_msg}\n"
                    f"  → /map 토픽 데이터로 진행합니다. (더 신뢰성 높음)\n"
                    f"{'*'*55}"
                )

        # 상태 전이
        self.startup_state = StartupState.READY
        mx, my = start_info['map_bl']
        ox, oy = start_info['map_origin']

        self.get_logger().info(
            f"\n{'='*55}\n"
            f"[WAITING_TF → READY] 동기화 완료!\n"
            f"\n"
            f"  ▶ 맵 원점 (월드):      ({ox:.4f}, {oy:.4f}) m\n"
            f"  ▶ 로봇 시작 (월드):    ({wx:.4f}, {wy:.4f}) m\n"
            f"  ▶ 로봇 시작 (맵기준):  ({mx:.4f}, {my:.4f}) m\n"
            f"  ▶ 맵 크기:             {self.coord.get_bounds()}\n"
            f"\n"
            f"  교차검증: {yaml_check_msg}\n"
            f"\n"
            f"  ✅ QR 코드 스캔을 시작합니다.\n"
            f"{'='*55}"
        )

    # =========================================================================
    # 3단계: 이미지 콜백 — QR 감지 및 좌표 저장
    # =========================================================================
    def image_callback(self, msg: Image):
        # READY 상태가 아니면 이미지 처리 스킵
        if self.startup_state != StartupState.READY:
            return

        try:
            frame = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
        except Exception as e:
            self.get_logger().warn(f"imgmsg 변환 실패: {e}")
            return

        qr_list = pyzbar.decode(frame)
        for qr in qr_list:
            qr_data = qr.data.decode('utf-8').strip()
            if not qr_data:
                continue

            # 쿨다운 체크
            now = time.time()
            if now - self.last_scan.get(qr_data, 0) < self.scan_cooldown:
                continue
            self.last_scan[qr_data] = now

            # 현재 로봇 위치 (map 프레임)
            pose = self._try_get_pose()
            if pose is None:
                self.get_logger().warn(f"QR '{qr_data}' 감지 — TF 실패. 좌표 없이 저장.")
                map_x, map_y, in_bounds = None, None, False
            else:
                world_x = pose.pose.position.x
                world_y = pose.pose.position.y

                try:
                    map_x, map_y = self.coord.world_to_map_bl(world_x, world_y)
                except RuntimeError as e:
                    self.get_logger().error(f"좌표 변환 실패: {e}")
                    map_x, map_y = None, None

                in_bounds = (
                    self.coord.is_within_bounds(map_x, map_y)
                    if map_x is not None else False
                )

                if not in_bounds:
                    self.get_logger().warn(
                        f"[경고] QR '{qr_data}' 감지 위치 맵 범위 밖!\n"
                        f"  월드=({world_x:.3f},{world_y:.3f})  "
                        f"맵기준=({map_x:.3f},{map_y:.3f})\n"
                        f"  범위: {self.coord.get_bounds()}\n"
                        f"  좌표는 저장하지만 신뢰성을 확인하세요."
                    )

            self.get_logger().info(
                f"[QR 감지] data='{qr_data}' | "
                f"맵기준=({map_x:.3f},{map_y:.3f}) | "
                f"{'범위OK' if in_bounds else '⚠범위밖'}"
                if map_x is not None else
                f"[QR 감지] data='{qr_data}' | 좌표 획득 실패"
            )

            # 이미지에 QR 박스 그리기
            frame = self._draw_qr_overlay(frame, qr, qr_data, map_x, map_y)

            # 이미지 저장 및 전송
            ts_str = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:19]
            img_path = f"{self.save_dir}/qr_{ts_str}_{qr_data[:16]}.jpg"
            cv2.imwrite(img_path, frame)
            self._publish_compressed(frame)

            # 메타데이터 구성 및 퍼블리시
            meta = {
                'qr_data':   qr_data,
                'x':         round(map_x, 4) if map_x is not None else None,
                'y':         round(map_y, 4) if map_y is not None else None,
                'world_x':   round(pose.pose.position.x, 4) if pose else None,
                'world_y':   round(pose.pose.position.y, 4) if pose else None,
                'in_bounds': in_bounds,
                'timestamp': ts_str,
                'img_file':  os.path.basename(img_path),
            }
            self.qr_meta_pub.publish(
                String(data=json.dumps(meta, ensure_ascii=False))
            )

            self.scan_results.append(meta)
            self._save_scan_results()

    # ── TF2 헬퍼 ─────────────────────────────────────────────────────────────
    def _try_get_pose(self) -> PoseStamped | None:
        """map → base_link TF2 변환 시도. 실패 시 None 반환."""
        try:
            tf = self.tf_buffer.lookup_transform(
                'map', 'base_link',
                rclpy.time.Time(),
                timeout=rclpy.duration.Duration(seconds=0.5)
            )
            pose = PoseStamped()
            pose.header           = tf.header
            pose.pose.position.x  = tf.transform.translation.x
            pose.pose.position.y  = tf.transform.translation.y
            pose.pose.orientation = tf.transform.rotation
            return pose
        except Exception:
            return None

    # ── 이미지 오버레이 ───────────────────────────────────────────────────────
    def _draw_qr_overlay(self, frame, qr, qr_data: str,
                          map_x: float | None, map_y: float | None):
        pts = qr.polygon
        if len(pts) == 4:
            cv2.polylines(
                frame,
                [numpy.array([(p.x, p.y) for p in pts], dtype=int)],
                True, (0, 255, 0), 2
            )
        coord_str = (f"({map_x:.2f},{map_y:.2f})" if map_x is not None
                     else "(?,?)")
        cv2.putText(frame, f"{qr_data} {coord_str}",
                    (qr.rect.left, qr.rect.top - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
        return frame

    # ── 압축 이미지 퍼블리시 ──────────────────────────────────────────────────
    def _publish_compressed(self, frame):
        msg = CompressedImage()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.format = 'jpeg'
        msg.data   = self.bridge.cv2_to_compressed_imgmsg(frame).data
        self.qr_image_pub.publish(msg)

    # ── YAML 저장 ─────────────────────────────────────────────────────────────
    def _save_scan_results(self):
        """
        스캔 결과를 YAML로 저장.
        맵 원점 정보도 함께 기록하여 나중에 좌표 역추적 가능.
        """
        out_path = f"{self.save_dir}/scan_positions.yaml"
        doc = {
            'session_info': {
                'map_origin_world_x': self.coord._origin_x,
                'map_origin_world_y': self.coord._origin_y,
                'robot_start_world_x': self.coord.robot_start_world_x,
                'robot_start_world_y': self.coord.robot_start_world_y,
                'robot_start_map_x':   self.coord.robot_start_map_x,
                'robot_start_map_y':   self.coord.robot_start_map_y,
                'map_yaml_path':       self.map_yaml_path or 'N/A',
            },
            'qr_scan_positions': self.scan_results,
        }
        with open(out_path, 'w', encoding='utf-8') as f:
            yaml.dump(doc, f, allow_unicode=True, default_flow_style=False)

        self.get_logger().info(
            f"[저장] {out_path}  ({len(self.scan_results)}개 QR)",
            throttle_duration_sec=2.0
        )


def main(args=None):
    rclpy.init(args=args)
    node = QRScannerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
