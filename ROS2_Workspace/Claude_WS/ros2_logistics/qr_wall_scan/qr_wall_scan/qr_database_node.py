#!/usr/bin/env python3
"""
qr_database_node.py — 스냅샷 오프라인 QR 추출 + DB 저장 노드
=============================================================
동작:
  1. /qr/scan_complete 토픽 수신 (qr_snapshot_node가 완료 시 퍼블리시)
     → 페이로드: qr_scan_results.yaml 경로
  2. yaml에서 snap_records 읽기 (각 위치별 snap_file + 촬영 포즈)
  3. snapshots/ 폴더의 이미지를 직접 열어 QR 재감지 (pyzbar)
  4. 촬영 포즈(world_x, world_y, yaw_rad, standoff_m) + QR 픽셀 위치로
     QR 월드 좌표 역산
  5. QR 재촬영을 위한 최적 접근 포즈(approach pose) 계산
  6. SQLite에 저장

실행:
  ros2 run qr_wall_scan qr_database_node --ros-args \\
    -p db_path:=/home/ubuntu22/qr_data/qr.db
"""

import json
import math
import os
import sqlite3
from datetime import datetime

import cv2
import yaml
import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from pyzbar import pyzbar


# ── 카메라 파라미터 (qr_snapshot_node 와 동일) ──────────────────────────────
IMG_WIDTH    = 640
IMG_HEIGHT   = 480
FOV_DEG      = 62.2
FOCAL_LEN_PX = (IMG_WIDTH / 2.0) / math.tan(math.radians(FOV_DEG / 2.0))

# QR 화이트리스트
WHITELIST = {
    'QR-001', 'QR-002', 'QR-003',
    'QR-CHEONAN', 'QR-PYEONGTAEK', 'QR-GONGJU', 'QR-ARRIVAL',
}

# 접근 포즈 오프셋 — QR 벽에서 이만큼 앞에 서서 재촬영
APPROACH_OFFSET_M = 0.30


class QRDatabaseNode(Node):

    def __init__(self):
        super().__init__('qr_database_node')

        self.declare_parameter('db_path',
                               '/home/ubuntu22/qr_data/qr.db')

        self.db_path = self.get_parameter('db_path').value
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        self._init_db()

        # /qr/scan_complete 구독
        self.create_subscription(
            String, '/qr/scan_complete', self._on_scan_complete, 10)

        self.get_logger().info(
            f'QR DB 노드 시작 — /qr/scan_complete 대기 중\n'
            f'  DB: {self.db_path}'
        )

    # =========================================================================
    # DB 초기화
    # =========================================================================
    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS qr_scans (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                qr_data          TEXT    NOT NULL,
                -- QR 월드 좌표 (map 프레임 기준)
                qr_world_x       REAL,
                qr_world_y       REAL,
                -- 접근 포즈 (QR 재촬영 최적 로봇 위치)
                approach_x       REAL,
                approach_y       REAL,
                approach_yaw_deg REAL,
                -- 촬영 당시 로봇 포즈
                robot_world_x    REAL,
                robot_world_y    REAL,
                robot_yaw_deg    REAL,
                standoff_m       REAL,
                -- 스냅 정보
                pose_idx         INTEGER,
                snap_file        TEXT,
                -- QR 픽셀 위치 (이미지 내)
                pixel_x          REAL,
                pixel_y          REAL,
                alpha_deg        REAL,
                -- 확인 여부 (1 = DB 노드가 직접 재감지)
                confirmed        INTEGER DEFAULT 1,
                created_at       TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
        self.get_logger().info('DB 스키마 준비 완료')

    # =========================================================================
    # /qr/scan_complete 콜백
    # =========================================================================
    def _on_scan_complete(self, msg: String):
        yaml_path = msg.data.strip()
        self.get_logger().info(f'[scan_complete] yaml 수신: {yaml_path}')

        if not os.path.exists(yaml_path):
            self.get_logger().error(f'yaml 파일 없음: {yaml_path}')
            return

        self._process_yaml(yaml_path)

    # =========================================================================
    # yaml → 스냅 이미지 처리
    # =========================================================================
    def _process_yaml(self, yaml_path: str):
        with open(yaml_path, 'r', encoding='utf-8') as f:
            doc = yaml.safe_load(f)

        snap_records = doc.get('snap_records', [])
        snapshots_dir = doc.get('session_info', {}).get('snapshots_dir', '')

        if not snapshots_dir or not os.path.isdir(snapshots_dir):
            # fallback: yaml 옆 snapshots/
            snapshots_dir = os.path.join(os.path.dirname(yaml_path), 'snapshots')

        self.get_logger().info(
            f'스냅 기록 {len(snap_records)}개 처리 시작\n'
            f'  snapshots 폴더: {snapshots_dir}'
        )

        total_saved = 0
        for record in snap_records:
            snap_file = record.get('snap_file')
            if not snap_file:
                continue  # 네비 실패 또는 캡처 타임아웃

            img_path = os.path.join(snapshots_dir, snap_file)
            if not os.path.exists(img_path):
                self.get_logger().warn(f'이미지 없음: {img_path}')
                continue

            saved = self._process_snap(img_path, record)
            total_saved += saved

        self.get_logger().info(
            f'[완료] {total_saved}건 DB 저장 완료 → {self.db_path}'
        )

    # =========================================================================
    # 스냅 이미지 QR 감지 + 역산 + DB 저장
    # =========================================================================
    def _process_snap(self, img_path: str, record: dict) -> int:
        img = cv2.imread(img_path)
        if img is None:
            self.get_logger().warn(f'이미지 읽기 실패: {img_path}')
            return 0

        h, w = img.shape[:2]
        qr_list = [d for d in pyzbar.decode(img) if d.type == 'QRCODE']

        saved = 0
        for qr in qr_list:
            qr_data = qr.data.decode('utf-8').strip()
            if qr_data not in WHITELIST:
                continue

            # ── QR 픽셀 중심 ──────────────────────────────────────────────
            qx = qr.rect.left + qr.rect.width  / 2.0
            qy = qr.rect.top  + qr.rect.height / 2.0

            # ── 수평 각도 오프셋 ───────────────────────────────────────────
            alpha = math.atan2(qx - w / 2.0, FOCAL_LEN_PX)

            # ── 촬영 포즈 ─────────────────────────────────────────────────
            robot_x   = float(record['world_x'])
            robot_y   = float(record['world_y'])
            robot_yaw = float(record['yaw_rad'])
            standoff  = float(record['standoff_m'])

            # ── QR 월드 좌표 역산 ─────────────────────────────────────────
            direction  = robot_yaw + alpha
            qr_world_x = robot_x + standoff * math.cos(direction)
            qr_world_y = robot_y + standoff * math.sin(direction)

            # ── 접근 포즈 계산 ────────────────────────────────────────────
            # QR 벽의 법선 방향 = 촬영 방향의 반대
            wall_normal = direction + math.pi
            approach_x   = qr_world_x + APPROACH_OFFSET_M * math.cos(wall_normal)
            approach_y   = qr_world_y + APPROACH_OFFSET_M * math.sin(wall_normal)
            approach_yaw = direction   # QR을 정면으로 바라보는 방향

            self.get_logger().info(
                f'  [QR] "{qr_data}"  '
                f'world=({qr_world_x:.3f}, {qr_world_y:.3f})  '
                f'approach=({approach_x:.3f}, {approach_y:.3f}, '
                f'{math.degrees(approach_yaw):.1f}°)'
            )

            # ── DB 저장 ───────────────────────────────────────────────────
            conn = sqlite3.connect(self.db_path)
            conn.execute('''
                INSERT INTO qr_scans (
                    qr_data,
                    qr_world_x, qr_world_y,
                    approach_x, approach_y, approach_yaw_deg,
                    robot_world_x, robot_world_y, robot_yaw_deg, standoff_m,
                    pose_idx, snap_file,
                    pixel_x, pixel_y, alpha_deg,
                    confirmed
                ) VALUES (?,  ?,?,  ?,?,?,  ?,?,?,?,  ?,?,  ?,?,?,  1)
            ''', (
                qr_data,
                round(qr_world_x, 4), round(qr_world_y, 4),
                round(approach_x, 4), round(approach_y, 4),
                round(math.degrees(approach_yaw), 2),
                round(robot_x, 4), round(robot_y, 4),
                round(math.degrees(robot_yaw), 2),
                round(standoff, 3),
                record.get('pose_idx'),
                os.path.basename(img_path),
                round(qx, 1), round(qy, 1),
                round(math.degrees(alpha), 2),
            ))
            conn.commit()
            conn.close()
            saved += 1

        return saved


# =============================================================================
def main(args=None):
    rclpy.init(args=args)
    node = QRDatabaseNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
