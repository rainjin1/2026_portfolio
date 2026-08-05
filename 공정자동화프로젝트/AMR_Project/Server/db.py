"""
MES 게이트웨이 서버 - SQLite DB 모듈
db.py
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "mes.db")

# ── 색상 코드 (W=하양, Y=노랑, P=분홍) ──────────────
COLOR_CODES = ("W", "Y", "P")

# WpC 빈 우선순위 (앞에서부터 채움, 각 빈 최대 MAX_WPC개)
WPC_BINS = {
    "Y": ["WpC1"],                   # 노랑
    "W": ["WpC2", "WpC3", "WpC4"],  # 하양 (2→3→4 순서로 채움)
    "P": ["WpC5", "WpC6"],          # 분홍 (5→6 순서로 채움)
}
MAX_WPC = 10  # 빈 1개당 최대 수용량

# ── 초기화 ────────────────────────────────────────
def init_db():
    """DB 및 테이블 생성"""
    conn = _connect()
    c = conn.cursor()

    # orders 테이블
    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            status      TEXT    NOT NULL DEFAULT '대기',
            pos_x       INTEGER NOT NULL,
            pos_y       INTEGER NOT NULL,
            pos_z       INTEGER NOT NULL,
            base_color  TEXT    NOT NULL DEFAULT 'random',
            ceil_color  TEXT    NOT NULL DEFAULT 'random',
            wall1_color TEXT    NOT NULL,
            wall2_color TEXT    NOT NULL,
            wall3_color TEXT    NOT NULL,
            wall4_color TEXT    NOT NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at  TEXT,
            UNIQUE(pos_x, pos_y, pos_z)
        )
    """)

    # inventory 테이블
    c.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            item    TEXT PRIMARY KEY,
            count   INTEGER NOT NULL DEFAULT 0
        )
    """)

    # state 테이블 (서버 상태 영속화)
    c.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key     TEXT PRIMARY KEY,
            value   TEXT
        )
    """)

    # 재고 초기 항목 삽입 (없을 경우)
    items = ["BpC1", "BpC2", "CpC1", "CpC2"] + [f"WpC{i}" for i in range(1, 7)]
    for item in items:
        c.execute("INSERT OR IGNORE INTO inventory (item, count) VALUES (?, 0)", (item,))

    # orders 테이블 컬럼 추가 (기존 DB 호환)
    for col, typedef in [
        ("current_process", "TEXT DEFAULT 'PENDING'"),
        ("current_device",  "TEXT"),
    ]:
        try:
            c.execute(f"ALTER TABLE orders ADD COLUMN {col} {typedef}")
        except Exception:
            pass  # 이미 존재

    # 공정 이력 테이블 (상태 변경 시마다 기록 → GUI 타임라인용)
    c.execute("""
        CREATE TABLE IF NOT EXISTS process_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id  INTEGER NOT NULL,
            process   TEXT    NOT NULL,
            device    TEXT,
            result    TEXT,
            note      TEXT,
            timestamp TEXT    DEFAULT (datetime('now', 'localtime'))
        )
    """)

    # 판별 결과 테이블 (4면 × 주문)
    c.execute("""
        CREATE TABLE IF NOT EXISTS inspection (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id  INTEGER NOT NULL,
            face      INTEGER NOT NULL,
            result    TEXT    NOT NULL,
            timestamp TEXT    DEFAULT (datetime('now', 'localtime')),
            UNIQUE(order_id, face)
        )
    """)

    conn.commit()
    conn.close()
    print("[DB] 초기화 완료")


# ── 연결 헬퍼 ─────────────────────────────────────
def _connect():
    return sqlite3.connect(DB_PATH)


# ══════════════════════════════════════════════════
# STATE
# ══════════════════════════════════════════════════

def get_state(key, default=None):
    conn = _connect()
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_state(key, value):
    conn = _connect()
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
        (key, str(value))
    )
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════
# INVENTORY
# ══════════════════════════════════════════════════

def get_inventory():
    """전체 재고 반환 {item: count}"""
    conn = _connect()
    rows = conn.execute("SELECT item, count FROM inventory").fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}


def get_stock(item):
    """특정 항목 재고 반환"""
    conn = _connect()
    row = conn.execute("SELECT count FROM inventory WHERE item=?", (item,)).fetchone()
    conn.close()
    return row[0] if row else 0


def set_stock(item, count):
    """재고 직접 설정 (초기화용)"""
    conn = _connect()
    conn.execute(
        "INSERT OR REPLACE INTO inventory (item, count) VALUES (?, ?)",
        (item, count)
    )
    conn.commit()
    conn.close()


def adjust_stock(item, delta):
    """재고 증감 (+1 분류완료, -1 출고)"""
    conn = _connect()
    conn.execute(
        "UPDATE inventory SET count = MAX(0, count + ?) WHERE item=?",
        (delta, item)
    )
    conn.commit()
    conn.close()


def get_pick_bin(color: str) -> str | None:
    """
    적재 시 WpC에서 꺼낼 빈 결정.
    가장 재고가 많은 빈 우선. 동수일 경우 앞번호 우선. 재고 없으면 None.
    """
    bins = WPC_BINS.get(color, [])
    best = None
    best_count = 0
    for b in bins:
        cnt = get_stock(b)
        if cnt > best_count:
            best_count = cnt
            best = b
    return best


def get_sort_bin(color: str) -> str | None:
    """
    분류 시 투입할 WpC 빈 결정.
    color: "W" / "Y" / "P"
    우선순위 목록에서 가장 재고가 적고 MAX_WPC 미만인 빈 반환.
    동수일 경우 앞번호 우선. 모두 가득 차면 None 반환.
    """
    bins = WPC_BINS.get(color, [])
    best = None
    best_count = MAX_WPC  # 이 이상이면 선택 안 함
    for b in bins:
        cnt = get_stock(b)
        if cnt < best_count:
            best_count = cnt
            best = b
    return best


def can_fulfill_order(order_id):
    """주문 1세트 이행 가능 여부 확인"""
    order = get_order(order_id)
    if not order:
        return False

    inv = get_inventory()
    if inv.get("BpC1", 0) + inv.get("BpC2", 0) < 1:
        return False
    if inv.get("CpC1", 0) + inv.get("CpC2", 0) < 1:
        return False

    # 벽 색상별 필요 수량 계산 (W/Y/P 기준)
    needed = {}
    for wall in [order["wall1_color"], order["wall2_color"],
                 order["wall3_color"], order["wall4_color"]]:
        needed[wall] = needed.get(wall, 0) + 1

    for color, qty in needed.items():
        bins = WPC_BINS.get(color, [])
        total = sum(inv.get(b, 0) for b in bins)
        if total < qty:
            return False

    return True


# ══════════════════════════════════════════════════
# ORDERS
# ══════════════════════════════════════════════════

def create_order(pos_x, pos_y, pos_z,
                 wall1, wall2, wall3, wall4,
                 base_color="random", ceil_color="random"):
    """
    주문 생성. 좌표 중복 시 None 반환.
    wall 색상: W/Y/B/D/N/R
    """
    if not all(c in COLOR_CODES for c in [wall1, wall2, wall3, wall4]):
        print(f"[DB] 오류: 유효하지 않은 색상 코드 (허용: {COLOR_CODES})")
        return None

    conn = _connect()
    try:
        cursor = conn.execute(
            """INSERT INTO orders
               (pos_x, pos_y, pos_z, base_color, ceil_color,
                wall1_color, wall2_color, wall3_color, wall4_color)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pos_x, pos_y, pos_z, base_color, ceil_color,
             wall1, wall2, wall3, wall4)
        )
        conn.commit()
        order_id = cursor.lastrowid
        print(f"[DB] 주문 생성: #{order_id} ({pos_x},{pos_y},{pos_z})")
        return order_id
    except sqlite3.IntegrityError:
        print(f"[DB] 오류: 좌표 ({pos_x},{pos_y},{pos_z}) 중복")
        return None
    finally:
        conn.close()


def get_order(order_id):
    """주문 조회"""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM orders WHERE order_id=?", (order_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_orders_by_status(status):
    """상태별 주문 목록"""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM orders WHERE status=? ORDER BY created_at",
        (status,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_order_status(order_id, status):
    """주문 상태 업데이트"""
    conn = _connect()
    conn.execute(
        "UPDATE orders SET status=?, updated_at=? WHERE order_id=?",
        (status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order_id)
    )
    conn.commit()
    conn.close()
    print(f"[DB] 주문 #{order_id} → {status}")


def clear_orders():
    """orders / process_log / inspection 테이블 전체 삭제 (재고는 유지)."""
    conn = _connect()
    conn.execute("DELETE FROM orders")
    conn.execute("DELETE FROM process_log")
    conn.execute("DELETE FROM inspection")
    conn.commit()
    conn.close()
    print("[DB] 주문 데이터 전체 삭제 완료")


def get_next_pending_order():
    """대기 중인 다음 주문 반환"""
    orders = get_orders_by_status("대기")
    return orders[0] if orders else None


def print_orders():
    """전체 주문 현황 출력"""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM orders ORDER BY created_at"
    ).fetchall()
    conn.close()

    print("\n[주문 현황]")
    print(f"{'ID':>4} {'상태':>8} {'좌표':>10} {'벽1':>4} {'벽2':>4} {'벽3':>4} {'벽4':>4}")
    print("-" * 50)
    for r in rows:
        coord = f"({r['pos_x']},{r['pos_y']},{r['pos_z']})"
        print(f"{r['order_id']:>4} {r['status']:>8} {coord:>10} "
              f"{r['wall1_color']:>4} {r['wall2_color']:>4} "
              f"{r['wall3_color']:>4} {r['wall4_color']:>4}")
    print()


def print_inventory():
    """재고 현황 출력"""
    inv = get_inventory()
    print("\n[재고 현황]")
    print(f"  BpC1(기초-1): {inv.get('BpC1', 0)}")
    print(f"  BpC2(기초-2): {inv.get('BpC2', 0)}")
    print(f"  CpC1(천장-1): {inv.get('CpC1', 0)}")
    print(f"  CpC2(천장-2): {inv.get('CpC2', 0)}")
    print(f"  WpC1(노랑  ): {inv.get('WpC1', 0)}")
    print(f"  WpC2(하양-1): {inv.get('WpC2', 0)}")
    print(f"  WpC3(하양-2): {inv.get('WpC3', 0)}")
    print(f"  WpC4(하양-3): {inv.get('WpC4', 0)}")
    print(f"  WpC5(분홍-1): {inv.get('WpC5', 0)}")
    print(f"  WpC6(분홍-2): {inv.get('WpC6', 0)}")
    print()


# ══════════════════════════════════════════════════
# PROCESS TRACKING
# ══════════════════════════════════════════════════

def set_order_process(order_id, process, device=None):
    """주문의 현재 공정·담당 장비 갱신."""
    conn = _connect()
    conn.execute(
        "UPDATE orders SET current_process=?, current_device=?, updated_at=? WHERE order_id=?",
        (process, device, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order_id)
    )
    conn.commit()
    conn.close()


def log_process(order_id, process, device=None, result=None, note=None):
    """공정 이력 1건 기록 (GUI 타임라인용)."""
    conn = _connect()
    conn.execute(
        "INSERT INTO process_log (order_id, process, device, result, note) VALUES (?,?,?,?,?)",
        (order_id, process, device, result, note)
    )
    conn.commit()
    conn.close()


def get_process_log(order_id):
    """주문의 전체 공정 이력 반환."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM process_log WHERE order_id=? ORDER BY timestamp",
        (order_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════
# INSPECTION
# ══════════════════════════════════════════════════

def add_inspection(order_id, face: int, result: str):
    """판별 결과 1면 기록. face=1~4, result='OK'|'NG'."""
    conn = _connect()
    conn.execute(
        "INSERT OR REPLACE INTO inspection (order_id, face, result) VALUES (?,?,?)",
        (order_id, face, result)
    )
    conn.commit()
    conn.close()


def get_inspection_results(order_id):
    """주문의 전체 판별 결과 반환 [{face, result, timestamp}]."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT face, result, timestamp FROM inspection WHERE order_id=? ORDER BY face",
        (order_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def is_order_pass(order_id) -> bool:
    """4면 모두 OK이면 True. 미완료 면이 있으면 False."""
    results = get_inspection_results(order_id)
    return len(results) == 4 and all(r["result"] == "OK" for r in results)


# ── 진입점 (단독 실행 시 테스트) ──────────────────
if __name__ == "__main__":
    init_db()

    # 재고 초기화 테스트
    set_stock("BpC", 3)
    set_stock("CpC", 3)
    set_stock("WpC1", 2)
    set_stock("WpC2", 2)
    set_stock("WpC3", 2)

    # 주문 생성 테스트
    oid = create_order(1, 1, 1, "W", "Y", "B", "W")
    create_order(1, 1, 1, "W", "Y", "B", "W")  # 중복 좌표 테스트
    create_order(1, 2, 1, "D", "N", "W", "Y")

    # 상태 업데이트 테스트
    if oid:
        update_order_status(oid, "적재중")

    print_inventory()
    print_orders()
    print(f"주문 #{oid} 이행 가능: {can_fulfill_order(oid)}")
