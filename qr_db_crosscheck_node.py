#!/usr/bin/env python3
"""
qr_db_crosscheck_node.py — QR 스캔 결과 × DB 교차검증 노드
============================================================
역할:
  - /qr/metadata 구독 (qr_wall_scan_node 가 퍼블리시)
  - coupang_logistics.db 의 inventory 테이블과 대조
  - 스캔된 QR 코드가 DB에 존재하는지, 재고 상태는 어떤지 로그 출력
  - 수량 차감 없음 (위치 탐색 단계이므로)

DB 스키마 (팀원 WMS):
  inventory(item_name TEXT, hub_name TEXT, current_stock INT,
            order_quantity INT, last_updated TEXT)
  stock_history(item_name TEXT, hub_name TEXT, status TEXT, amount INT)

QR → DB 매핑:
  QR-CHEONAN   → hub_name = '천안'
  QR-GONGJU    → hub_name = '공주'
  QR-PYEONGTAEK → hub_name = '평택'
  QR-001/002/003 → 허브별 아이템 (HUB_MAPPER 참조)
  QR-ARRIVAL   → 입고/도착 이벤트 (DB 직접 매핑 없음 — 로그만)

실행:
  ros2 run qr_wall_scan qr_db_crosscheck_node
"""

import json
import sqlite3
import os

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


# ── DB 경로 (하드코딩) ────────────────────────────────────────────────────────
DB_PATH = os.path.expanduser('~/amr_project/coupang_logistics.db')

# ── QR 코드 → 허브 이름 매핑 ─────────────────────────────────────────────────
HUB_QR_MAP = {
    'QR-CHEONAN':    '천안',
    'QR-GONGJU':     '공주',
    'QR-PYEONGTAEK': '평택',
}

# ── 허브별 QR-001/002/003 → 아이템 이름 매핑 ─────────────────────────────────
HUB_MAPPER = {
    '천안': {'QR-001': '라면',   'QR-002': '음료수', 'QR-003': '과자'},
    '공주': {'QR-001': '쌀',     'QR-002': '밀가루', 'QR-003': '설탕'},
    '평택': {'QR-001': '참치캔', 'QR-002': '햄',     'QR-003': '김치'},
}

ITEM_QRS = {'QR-001', 'QR-002', 'QR-003'}


class QRDbCrosscheckNode(Node):

    def __init__(self):
        super().__init__('qr_db_crosscheck_node')

        # DB 연결 확인
        if not os.path.exists(DB_PATH):
            self.get_logger().error(
                f'DB 파일 없음: {DB_PATH}\n'
                f'  coupang_logistics.db 를 ~/amr_project/ 에 배치하세요.'
            )
        else:
            self.get_logger().info(f'DB 연결 확인: {DB_PATH}')
            self._verify_db_schema()

        # /qr/metadata 구독
        self.create_subscription(String, '/qr/metadata', self._meta_callback, 10)

        self.get_logger().info(
            'QR DB 교차검증 노드 시작\n'
            '  /qr/metadata 구독 중 — QR 스캔 시 DB 대조 결과 출력'
        )

    # =========================================================================
    # /qr/metadata 콜백
    # =========================================================================
    def _meta_callback(self, msg: String):
        try:
            meta = json.loads(msg.data)
        except json.JSONDecodeError as e:
            self.get_logger().warn(f'메타데이터 파싱 실패: {e}')
            return

        qr_data  = meta.get('qr_data', '')
        map_x    = meta.get('x')
        map_y    = meta.get('y')
        coord_str = (f'({map_x:.3f}, {map_y:.3f})'
                     if map_x is not None else '(좌표없음)')

        self.get_logger().info(
            f'[교차검증] QR="{qr_data}" 위치={coord_str}'
        )

        if not os.path.exists(DB_PATH):
            self.get_logger().error('DB 파일 없음 — 교차검증 불가')
            return

        # ── 허브 전환 QR ──────────────────────────────────────────────────────
        if qr_data in HUB_QR_MAP:
            self._check_hub(qr_data, HUB_QR_MAP[qr_data], coord_str)

        # ── 아이템 QR ─────────────────────────────────────────────────────────
        elif qr_data in ITEM_QRS:
            self._check_item(qr_data, coord_str)

        # ── 도착(입고) QR ─────────────────────────────────────────────────────
        elif qr_data == 'QR-ARRIVAL':
            self.get_logger().info(
                f'  [QR-ARRIVAL] 도착 지점 확인됨 — DB 차감 없음 (입고 처리 별도)'
            )

        else:
            self.get_logger().warn(f'  알 수 없는 QR: "{qr_data}"')

    # =========================================================================
    # 허브 교차검증
    # =========================================================================
    def _check_hub(self, qr_code: str, hub_name: str, coord_str: str):
        """허브 QR 스캔 시: 해당 허브의 전체 재고 현황 조회"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cur  = conn.cursor()
            cur.execute(
                'SELECT item_name, current_stock, order_quantity '
                'FROM inventory WHERE hub_name = ? ORDER BY item_name',
                (hub_name,)
            )
            rows = cur.fetchall()
            conn.close()
        except sqlite3.Error as e:
            self.get_logger().error(f'  DB 조회 오류: {e}')
            return

        if not rows:
            self.get_logger().warn(
                f'  [{qr_code} → {hub_name}] ⚠ DB에 허브 데이터 없음'
            )
            return

        lines = [f'  [{qr_code} → {hub_name}] 재고 현황 (위치={coord_str})']
        all_ok = True
        for item_name, stock, order_qty in rows:
            status = '✅' if stock > 0 else '❌ 재고없음'
            lines.append(f'    {item_name}: 현재고={stock}, 발주량={order_qty}  {status}')
            if stock == 0:
                all_ok = False

        lines.append(f'  → 허브 상태: {"정상" if all_ok else "일부 품절"}')
        self.get_logger().info('\n'.join(lines))

    # =========================================================================
    # 아이템 교차검증
    # =========================================================================
    def _check_item(self, qr_code: str, coord_str: str):
        """아이템 QR 스캔 시: 전 허브에서 해당 QR의 아이템 재고 조회"""
        lines = [f'  [{qr_code}] 전 허브 재고 조회 (위치={coord_str})']
        found_any = False

        try:
            conn = sqlite3.connect(DB_PATH)
            cur  = conn.cursor()

            for hub_name, item_map in HUB_MAPPER.items():
                item_name = item_map.get(qr_code)
                if item_name is None:
                    continue

                cur.execute(
                    'SELECT current_stock, order_quantity FROM inventory '
                    'WHERE hub_name = ? AND item_name = ?',
                    (hub_name, item_name)
                )
                row = cur.fetchone()

                if row:
                    stock, order_qty = row
                    status = '✅' if stock > 0 else '❌ 재고없음'
                    lines.append(
                        f'    {hub_name} / {item_name}: '
                        f'현재고={stock}, 발주량={order_qty}  {status}'
                    )
                    found_any = True
                else:
                    lines.append(f'    {hub_name} / {item_name}: DB 항목 없음')

            conn.close()
        except sqlite3.Error as e:
            self.get_logger().error(f'  DB 조회 오류: {e}')
            return

        if not found_any:
            lines.append('  → ⚠ DB에 해당 아이템 데이터 없음')
        self.get_logger().info('\n'.join(lines))

    # =========================================================================
    # DB 스키마 확인 (시작 시 1회)
    # =========================================================================
    def _verify_db_schema(self):
        try:
            conn = sqlite3.connect(DB_PATH)
            cur  = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cur.fetchall()}
            conn.close()

            required = {'inventory', 'stock_history'}
            missing  = required - tables
            if missing:
                self.get_logger().warn(
                    f'DB 스키마 불완전 — 누락 테이블: {missing}'
                )
            else:
                self.get_logger().info(
                    f'DB 스키마 확인 완료: {tables}'
                )
        except sqlite3.Error as e:
            self.get_logger().error(f'DB 스키마 확인 실패: {e}')


# =============================================================================
def main(args=None):
    rclpy.init(args=args)
    node = QRDbCrosscheckNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
