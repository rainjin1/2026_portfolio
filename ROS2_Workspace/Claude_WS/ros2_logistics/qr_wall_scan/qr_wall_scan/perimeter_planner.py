#!/usr/bin/env python3
"""
perimeter_planner.py — PGM 맵에서 외곽 벽 경유지 자동 생성
============================================================
알고리즘:
  1. PGM 로드 + 상하 반전 (ROS 좌표 보정)
  2. 점유 셀 중 가장 큰 연결 성분 = 외곽벽 (내부 구조물 제외)
  3. 외곽벽의 최소 면적 회전 사각형(minAreaRect) → 꼭짓점 4개 추출
  4. 각 꼭짓점을 중심 방향으로 standoff_m 이동
  5. 거리 변환 그래디언트로 yaw(벽을 향하는 방향) 계산

사용:
  from qr_wall_scan.perimeter_planner import generate_outer_wall_waypoints

  waypoints = generate_outer_wall_waypoints(
      pgm_path  = '/home/ubuntu22/map/0622_map_final.pgm',
      yaml_path = '/home/ubuntu22/map/0622_map_final.yaml',
      standoff_m = 0.35,
      interval_m = 0.40,
  )
  # [(world_x, world_y, yaw_rad), ...]
"""

import math
import yaml
import numpy as np
import cv2


def generate_outer_wall_waypoints(
    pgm_path: str,
    yaml_path: str,
    standoff_m: float = 0.35,
    interval_m: float = 0.40,
) -> list[tuple[float, float, float]]:
    """
    PGM 맵 기반 외곽 벽 내측 perimeter 경유지 생성.

    Args:
        pgm_path   : map.pgm 경로
        yaml_path  : map.yaml 경로
        standoff_m : 벽으로부터 로봇 이격 거리 (미터). 기본 0.35m
        interval_m : 경유지 간격 (미터). 기본 0.40m

    Returns:
        [(world_x, world_y, yaw_rad), ...]  /map 프레임(월드 좌표) 기준

    Raises:
        FileNotFoundError : PGM 파일 없음
        RuntimeError      : 외곽벽 또는 경로 추출 실패
    """
    # ── 1. 맵 메타데이터 로드 ────────────────────────────────────────────────
    with open(yaml_path, 'r') as f:
        meta = yaml.safe_load(f)
    res = float(meta['resolution'])          # m/pixel
    origin = meta['origin']
    ox = float(origin[0])                    # 맵 좌하단 world_x
    oy = float(origin[1])                    # 맵 좌하단 world_y

    # ── 2. PGM 로드 + 상하 반전 (ROS: y=0=하단, PGM: y=0=상단) ────────────
    img = cv2.imread(pgm_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"PGM 파일을 열 수 없음: {pgm_path}")
    img = cv2.flip(img, 0)
    h, w = img.shape

    # ── 3. 이진 마스크 ───────────────────────────────────────────────────────
    # ROS map 규약: 0=점유(occupied), 205=unknown, 254=자유(free)
    occ  = (img < 50).astype(np.uint8)    # 1 = 점유
    free = (img > 180).astype(np.uint8)   # 1 = 자유

    # ── 3-1. 맵 가장자리 강제 폐쇄 (열린 벽 방어) ──────────────────────────
    # 문/LiDAR 미도달로 외곽 벽 일부가 끊어진 경우에도 닫힌 루프를 보장
    occ[[0, -1], :] = 1
    occ[:, [0, -1]] = 1

    # ── 4. 외곽벽 = 가장 큰 연결된 점유 영역 ────────────────────────────────
    # 내부 구조물(선반, 기둥)은 더 작은 연결 성분으로 자동 제외
    n_lbl, labels, stats, _ = cv2.connectedComponentsWithStats(
        occ, connectivity=8
    )
    if n_lbl <= 1:
        raise RuntimeError("맵에서 점유 영역을 찾을 수 없음 — PGM 경로 확인")
    outer_idx  = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    outer_wall = (labels == outer_idx).astype(np.uint8)

    # ── 5. 외곽벽 컨투어 → 최소 면적 회전 사각형 → 꼭짓점 4개 ────────────────
    contours_wall, _ = cv2.findContours(
        outer_wall, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours_wall:
        raise RuntimeError("외곽 벽 컨투어 추출 실패")
    outer_contour = max(contours_wall, key=cv2.contourArea)
    rect          = cv2.minAreaRect(outer_contour)   # ((cx,cy),(w,h),angle)
    cx, cy        = rect[0]
    box           = cv2.boxPoints(rect)              # (4,2) float — 4개 꼭짓점

    # ── 6. 각 꼭짓점을 중심 방향으로 standoff_m 이동 ────────────────────────
    standoff_px = standoff_m / res
    sampled = []
    for bx, by in box:
        dx, dy = cx - bx, cy - by
        d = math.hypot(dx, dy)
        if d > 0:
            sampled.append((bx + dx / d * standoff_px,
                            by + dy / d * standoff_px))
        else:
            sampled.append((bx, by))

    # ── 7. 거리 변환 + 그래디언트 (yaw 계산용) ──────────────────────────────
    outer_inv       = (1 - outer_wall).astype(np.uint8)
    dist_from_outer = cv2.distanceTransform(outer_inv, cv2.DIST_L2, 5)
    dist_from_outer = dist_from_outer * free.astype(np.float32)
    grad_x = cv2.Sobel(dist_from_outer, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(dist_from_outer, cv2.CV_32F, 0, 1, ksize=3)

    # ── 8. 월드 좌표 변환 + yaw 계산 ────────────────────────────────────────
    waypoints: list[tuple[float, float, float]] = []

    for px, py in sampled:
        px_i, py_i = int(px), int(py)

        world_x = px_i * res + ox
        world_y = py_i * res + oy

        px_c = min(max(px_i, 2), w - 3)
        py_c = min(max(py_i, 2), h - 3)
        gx   = float(grad_x[py_c, px_c])
        gy   = float(grad_y[py_c, px_c])

        if abs(gx) < 1e-3 and abs(gy) < 1e-3:
            # fallback: 중심에서 꼭짓점 방향 = 벽을 향함
            yaw = math.atan2(py - cy, px - cx)
        else:
            yaw = math.atan2(-gy, -gx)   # 외곽벽을 향함

        waypoints.append((world_x, world_y, yaw))

    return waypoints


def save_waypoints_yaml(
    waypoints: list[tuple[float, float, float]],
    out_path: str,
) -> None:
    """생성된 경유지를 YAML 파일로 저장 (디버그 및 재사용용)."""
    doc = {
        'total': len(waypoints),
        'waypoints': [
            {'id': i, 'x': round(x, 4), 'y': round(y, 4),
             'yaw_deg': round(math.degrees(yaw), 1)}
            for i, (x, y, yaw) in enumerate(waypoints)
        ]
    }
    with open(out_path, 'w') as f:
        yaml.dump(doc, f, default_flow_style=False)


# ── 독립 실행 테스트 ──────────────────────────────────────────────────────────
if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3:
        print("사용법: python3 perimeter_planner.py <map.pgm> <map.yaml>")
        sys.exit(1)

    wps = generate_outer_wall_waypoints(
        pgm_path   = sys.argv[1],
        yaml_path  = sys.argv[2],
        standoff_m = float(sys.argv[3]) if len(sys.argv) > 3 else 0.35,
        interval_m = float(sys.argv[4]) if len(sys.argv) > 4 else 0.40,
    )
    print(f"생성된 경유지: {len(wps)}개")
    for i, (x, y, yaw) in enumerate(wps[:5]):
        print(f"  [{i}] x={x:.3f} y={y:.3f} yaw={math.degrees(yaw):.1f}°")
    if len(wps) > 5:
        print(f"  ... ({len(wps) - 5}개 더)")

    # 시각화 (선택)
    save_waypoints_yaml(wps, '/tmp/perimeter_waypoints.yaml')
    print("→ /tmp/perimeter_waypoints.yaml 저장 완료")
