#!/usr/bin/env python3
"""
[Stage 3] QR Database Node — Remote PC (원격 PC)
=================================================
역할:
  - 로봇이 퍼블리시한 QR 이미지(/qr/capture_image) 수신
  - 로봇이 퍼블리시한 메타데이터(/qr/metadata) 수신
  - pyzbar로 이미지 재검증 디코딩
  - SQLite DB에 (id, qr_data, x, y, timestamp, image_blob) 저장
  - 이미지 파일을 로컬 디렉토리에도 저장

의존성:
  sudo apt install ros-humble-cv-bridge
  pip install pyzbar pillow --break-system-packages
  sudo apt install libzbar0

실행 (원격 PC):
  # 로봇과 같은 ROS_DOMAIN_ID 설정 필수
  export ROS_DOMAIN_ID=<숫자>
  ros2 run <패키지명> qr_database_node --ros-args -p db_path:=/home/user/qr_data/qr.db

DB 스키마:
  qr_scans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    qr_data    TEXT NOT NULL,          -- QR 코드 디코딩 값
    x          REAL,                   -- 맵 좌하단 기준 X (미터)
    y          REAL,                   -- 맵 좌하단 기준 Y (미터)
    timestamp  TEXT,                   -- ISO 8601 문자열
    image_path TEXT,                   -- 저장된 이미지 파일 경로
    confirmed  INTEGER DEFAULT 0       -- 로봇 감지(0) vs 원격PC 재확인(1)
  )
"""

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from cv_bridge import CvBridge

import cv2
import json
import sqlite3
import os
import numpy as np
from datetime import datetime
from pyzbar import pyzbar


class QRDatabaseNode(Node):
    def __init__(self):
        super().__init__('qr_database_node')

        # ── 파라미터 ──────────────────────────────────────────────────────────
        self.declare_parameter('db_path',   '/tmp/qr_data/qr.db')
        self.declare_parameter('image_dir', '/tmp/qr_data/images')

        self.db_path   = self.get_parameter('db_path').value
        self.image_dir = self.get_parameter('image_dir').value
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        os.makedirs(self.image_dir, exist_ok=True)

        # ── DB 초기화 ─────────────────────────────────────────────────────────
        self.conn   = sqlite3.connect(self.db_path)
        self._init_db()

        # ── 내부 상태 ─────────────────────────────────────────────────────────
        self.bridge          = CvBridge()
        self.pending_meta: dict[str, dict] = {}  # timestamp → meta dict (이미지 도착 전 대기)
        self.pending_image: dict[str, np.ndarray] = {}  # timestamp → cv2 frame

        # ── Subscribers ───────────────────────────────────────────────────────
        self.image_sub = self.create_subscription(
            CompressedImage, '/qr/capture_image', self.image_callback, 10)
        self.meta_sub = self.create_subscription(
            String, '/qr/metadata', self.meta_callback, 10)

        self.get_logger().info(f"QR Database Node 시작. DB: {self.db_path}")

    # ── DB 스키마 초기화 ──────────────────────────────────────────────────────
    def _init_db(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS qr_scans (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                qr_data     TEXT    NOT NULL,
                x           REAL,
                y           REAL,
                timestamp   TEXT,
                image_path  TEXT,
                confirmed   INTEGER DEFAULT 0
            )
        """)
        # 검색 인덱스
        cur.execute("CREATE INDEX IF NOT EXISTS idx_qr_data ON qr_scans (qr_data)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON qr_scans (timestamp)")
        self.conn.commit()

    # ── 메타데이터 수신 콜백 ──────────────────────────────────────────────────
    def meta_callback(self, msg: String):
        try:
            meta = json.loads(msg.data)
        except json.JSONDecodeError as e:
            self.get_logger().error(f"메타데이터 JSON 파싱 실패: {e}")
            return

        ts = meta.get('timestamp', '')
        self.pending_meta[ts] = meta
        self.get_logger().info(
            f"[META 수신] qr='{meta.get('qr_data')}' "
            f"pos=({meta.get('x')}, {meta.get('y')}) ts={ts}"
        )
        # 이미 이미지가 도착해 있으면 즉시 저장
        self._try_save(ts)

    # ── 이미지 수신 콜백 ──────────────────────────────────────────────────────
    def image_callback(self, msg: CompressedImage):
        try:
            frame = self.bridge.compressed_imgmsg_to_cv2(msg, desired_encoding='bgr8')
        except Exception as e:
            self.get_logger().error(f"압축 이미지 변환 실패: {e}")
            return

        # 이미지에서 타임스탬프 추출 불가 → 가장 최근 pending_meta와 매칭
        ts = self._latest_pending_meta_ts()
        if ts:
            self.pending_image[ts] = frame
            self._try_save(ts)
        else:
            # 메타 없이 이미지만 도착한 경우 — QR 재디코딩 후 저장
            self.get_logger().warn("메타데이터 없이 이미지 수신. 자체 디코딩 시도.")
            self._save_image_only(frame)

    # ── 메타+이미지 병합 저장 ─────────────────────────────────────────────────
    def _try_save(self, ts: str):
        meta  = self.pending_meta.get(ts)
        frame = self.pending_image.get(ts)

        if meta is None or frame is None:
            return  # 아직 한쪽이 미도착

        qr_data  = meta.get('qr_data', '')
        x        = meta.get('x')
        y        = meta.get('y')

        # 원격 PC에서 이미지 재디코딩으로 검증
        confirmed = self._verify_qr(frame, qr_data)

        # 이미지 파일 저장
        img_filename = f"{self.image_dir}/qr_{ts}_{qr_data[:16]}.jpg"
        cv2.imwrite(img_filename, frame)

        # DB 저장
        self._insert_record(qr_data, x, y, ts, img_filename, int(confirmed))

        # 대기 목록에서 제거
        self.pending_meta.pop(ts, None)
        self.pending_image.pop(ts, None)

    # ── QR 재디코딩 검증 ──────────────────────────────────────────────────────
    def _verify_qr(self, frame: np.ndarray, expected: str) -> bool:
        """원격 PC에서 이미지를 재디코딩하여 QR 값 일치 여부 확인."""
        decoded_list = pyzbar.decode(frame)
        for d in decoded_list:
            if d.data.decode('utf-8').strip() == expected:
                self.get_logger().info(f"[검증 OK] QR 재확인 성공: '{expected}'")
                return True
        self.get_logger().warn(f"[검증 FAIL] 재디코딩 불일치. 기대값: '{expected}'")
        return False

    # ── 메타 없이 이미지만 도착한 경우 ───────────────────────────────────────
    def _save_image_only(self, frame: np.ndarray):
        decoded_list = pyzbar.decode(frame)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        for d in decoded_list:
            qr_data = d.data.decode('utf-8').strip()
            img_filename = f"{self.image_dir}/qr_{ts}_{qr_data[:16]}.jpg"
            cv2.imwrite(img_filename, frame)
            self._insert_record(qr_data, None, None, ts, img_filename, confirmed=1)

    # ── DB 레코드 삽입 ────────────────────────────────────────────────────────
    def _insert_record(
        self,
        qr_data: str,
        x: float | None,
        y: float | None,
        timestamp: str,
        image_path: str,
        confirmed: int = 0,
    ):
        try:
            cur = self.conn.cursor()
            cur.execute("""
                INSERT INTO qr_scans (qr_data, x, y, timestamp, image_path, confirmed)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (qr_data, x, y, timestamp, image_path, confirmed))
            self.conn.commit()
            self.get_logger().info(
                f"[DB 저장] id={cur.lastrowid} qr='{qr_data}' "
                f"pos=({x}, {y}) confirmed={confirmed}"
            )
        except sqlite3.Error as e:
            self.get_logger().error(f"DB 저장 실패: {e}")

    # ── 유틸: 가장 최근 pending_meta timestamp ────────────────────────────────
    def _latest_pending_meta_ts(self) -> str | None:
        if not self.pending_meta:
            return None
        return max(self.pending_meta.keys())

    # ── DB 전체 조회 (디버그용) ───────────────────────────────────────────────
    def print_all_records(self):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM qr_scans ORDER BY id")
        rows = cur.fetchall()
        self.get_logger().info(f"=== DB 전체 레코드 ({len(rows)}개) ===")
        for row in rows:
            self.get_logger().info(str(row))

    def destroy_node(self):
        self.print_all_records()
        self.conn.close()
        super().destroy_node()


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
