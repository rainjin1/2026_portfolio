#!/usr/bin/env python3
"""
perimeter_planner.py — PGM 맵에서 외곽 벽 경유지 자동 생성
============================================================
알고리즘:
  1. PGM 로드 + 상하 반전 (ROS 좌표 보정)
  2. 점유 셀 중 가장 큰 연결 성분 = 외곽벽 (내부 구조물 제외)
  3. 외곽벽으로부터 거리 변환 (distance transform)
  4. standoff_m 거리의 등거리선(isoline) 추출
  5. 등거리선 중 가장 큰 루프 = 로봇 경로
  6. 일정 간격으로 샘플링 + 벽을 향하는 yaw 계산

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

    # ── 5. 외곽벽으로부터 거리 변환 (자유 공간 한정) ────────────────────────
    # outer_wall=1인 픽셀을 소스로, 나머지 픽셀의 거리 계산
    outer_inv       = (1 - outer_wall).astype(np.uint8)
    dist_from_outer = cv2.distanceTransform(outer_inv, cv2.DIST_L2, 5)
    dist_from_outer = dist_from_outer * free.astype(np.float32)  # 자유 공간만

    # ── 6. standoff 거리 등거리선(isoline) 추출 ─────────────────────────────
    standoff_px = standoff_m / res
    tol         = max(2.0, standoff_px * 0.12)   # ±12% 또는 최소 ±2px

    isoline = (
        (dist_from_outer >= standoff_px - tol) &
        (dist_from_outer <= standoff_px + tol) &
        (free == 1)
    ).astype(np.uint8)

    # ── 7. 가장 큰 연결 성분 = 외곽 루프 (내부 구조물 주변 소루프 제외) ────
    n2, labels2, stats2, _ = cv2.connectedComponentsWithStats(
        isoline, connectivity=8
    )
    if n2 <= 1:
        raise RuntimeError(
            f"경유지 경로 생성 실패 (standoff={standoff_m}m). "
            f"이격 거리를 줄이거나 맵을 확인하세요."
        )
    path_label = 1 + int(np.argmax(stats2[1:, cv2.CC_STAT_AREA]))
    path_mask  = (labels2 == path_label).astype(np.uint8)

    # ── 8. 순서 있는 컨투어 추출 ─────────────────────────────────────────────
    contours, _ = cv2.findContours(
        path_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
    )
    if not contours:
        raise RuntimeError("컨투어 추출 실패")
    pts = max(contours, key=len).reshape(-1, 2)   # (N, 2) — (pixel_x, pixel_y)

    # ── 9. 일정 간격으로 샘플링 ──────────────────────────────────────────────
    interval_px = interval_m / res
    sampled     = [pts[0].tolist()]
    acc_dist    = 0.0

    for i in range(1, len(pts)):
        dx = int(pts[i][0]) - int(pts[i - 1][0])
        dy = int(pts[i][1]) - int(pts[i - 1][1])
        acc_dist += math.hypot(dx, dy)
        if acc_dist >= interval_px:
            sampled.append(pts[i].tolist())
            acc_dist = 0.0

    # ── 10. 월드 좌표 변환 + yaw 계산 (외곽벽을 향하는 방향) ─────────────────
    # 거리 변환의 그래디언트 = 벽에서 멀어지는 방향
    # 반대 방향(-gradient) = 벽을 향하는 방향 = 카메라가 바라봐야 할 방향
    grad_x = cv2.Sobel(dist_from_outer, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(dist_from_outer, cv2.CV_32F, 0, 1, ksize=3)

    waypoints: list[tuple[float, float, float]] = []
    n_pts = len(sampled)

    for i, (px, py) in enumerate(sampled):
        px, py = int(px), int(py)

        # 픽셀 → 월드 좌표
        world_x = px * res + ox
        world_y = py * res + oy

        # yaw: 그래디언트 역방향 = 외곽벽 방향
        px_c = min(max(px, 2), w - 3)
        py_c = min(max(py, 2), h - 3)
        gx   = float(grad_x[py_c, px_c])
        gy   = float(grad_y[py_c, px_c])

        if abs(gx) < 1e-3 and abs(gy) < 1e-3:
            # 그래디언트가 0이면 다음 경유지 방향으로 fallback
            ni  = (i + 1) % n_pts
            dx  = sampled[ni][0] - px
            dy  = sampled[ni][1] - py
            yaw = math.atan2(dy, dx)
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
