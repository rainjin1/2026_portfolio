#!/usr/bin/env python3
"""
simulate_coverage.py — 직사각형 외곽 기반 촬영 위치 시뮬레이션
==============================================================
사용:
  python3 simulate_coverage.py <map.pgm> <map.yaml> [standoff_max] [start_x] [start_y]

예시:
  python3 simulate_coverage.py ~/map/0622_map_final.pgm ~/map/0622_map_final.yaml
  python3 simulate_coverage.py map.pgm map.yaml 0.8 -1.0 0.5
"""

import sys
import math
import numpy as np
import cv2
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyArrowPatch, Wedge, Polygon as MplPolygon
from matplotlib.colors import ListedColormap

sys.path.insert(0, '.')
try:
    from qr_wall_scan.wall_coverage_planner import WallCoveragePlanner
except ImportError:
    from wall_coverage_planner import WallCoveragePlanner


def draw_fov_wedge(ax, pose, fov_deg, standoff_m, color, alpha=0.15):
    wedge = Wedge(
        center=(pose.world_x, pose.world_y),
        r=standoff_m,
        theta1=pose.yaw_deg - fov_deg/2.0,
        theta2=pose.yaw_deg + fov_deg/2.0,
        color=color, alpha=alpha, zorder=2,
    )
    ax.add_patch(wedge)


def corners_to_world(corners, ox, oy, res):
    """픽셀 좌표 꼭짓점 → 월드 좌표."""
    return [(cx * res + ox, cy * res + oy) for cx, cy in corners]


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    pgm_path = sys.argv[1]
    yaml_path = sys.argv[2]
    max_std  = float(sys.argv[3]) if len(sys.argv) > 3 else 0.80
    start_x  = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
    start_y  = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0

    print(f"맵: {pgm_path}")
    print(f"파라미터: max_standoff={max_std}m  시작=({start_x},{start_y})")
    print("촬영 위치 계산 중...")

    planner = WallCoveragePlanner(
        pgm_path, yaml_path,
        max_standoff_m=max_std,
        start_world_xy=(start_x, start_y),
    )
    poses = planner.generate(verbose=True)
    print()
    print(planner.summary(poses))
    print()

    # ── 맵 데이터 ─────────────────────────────────────────────────────────────
    res = planner.res
    ox  = planner.ox
    oy  = planner.oy
    h   = planner.h

    raw           = cv2.imread(pgm_path, cv2.IMREAD_GRAYSCALE)
    raw           = cv2.flip(raw, 0)
    occ_raw       = planner.occ_raw
    occ_fix       = planner.occ
    interior_free = planner.interior_free
    rect_corners  = planner.rect_corners   # 픽셀 좌표 (4,2)

    # 직사각형 꼭짓점 월드 좌표
    rect_world = corners_to_world(rect_corners, ox, oy, res)

    extent = [ox, ox + planner.w * res, oy, oy + planner.h * res]

    # ── 레이아웃 ──────────────────────────────────────────────────────────────
    fig, axes = plt.subplots(1, 2, figsize=(22, 10))
    fig.patch.set_facecolor('#1a1a2e')

    # ── 왼쪽: 맵 보정 + 직사각형 피팅 결과 ───────────────────────────────────
    ax_map = axes[0]
    ax_map.set_facecolor('#1a1a2e')
    ax_map.imshow(raw, cmap='gray', origin='lower', extent=extent, alpha=0.5, zorder=0)

    # 원본 벽 (빨강)
    raw_wall = np.zeros((*occ_raw.shape, 4), dtype=np.float32)
    raw_wall[occ_raw == 1] = [1.0, 0.2, 0.2, 0.5]
    ax_map.imshow(raw_wall, origin='lower', extent=extent, zorder=1)

    # 갭 보정 추가 픽셀 (파랑)
    added = np.logical_and(occ_fix == 1, occ_raw == 0).astype(np.uint8)
    fix_ov = np.zeros((*added.shape, 4), dtype=np.float32)
    fix_ov[added == 1] = [0.2, 0.6, 1.0, 0.8]
    ax_map.imshow(fix_ov, origin='lower', extent=extent, zorder=2)

    # 내부 자유공간 (연두 — flood fill 결과)
    free_vis = np.zeros((*interior_free.shape, 4), dtype=np.float32)
    free_vis[interior_free == 1] = [0.2, 0.9, 0.3, 0.20]
    ax_map.imshow(free_vis, origin='lower', extent=extent, zorder=3)

    # 직사각형 (노랑 실선 + 꼭짓점 점)
    rect_xs = [wx for wx, wy in rect_world] + [rect_world[0][0]]
    rect_ys = [wy for wx, wy in rect_world] + [rect_world[0][1]]
    ax_map.plot(rect_xs, rect_ys, '-', color='yellow', linewidth=2.5,
                zorder=5, label='피팅 직사각형')
    for k, (wx, wy) in enumerate(rect_world):
        ax_map.scatter(wx, wy, s=120, color='yellow', zorder=6,
                       edgecolors='white', linewidths=1.2)
        ax_map.text(wx + 0.05, wy + 0.05, f'V{k}',
                    color='yellow', fontsize=9, fontweight='bold', zorder=7)

    ax_map.set_title(
        '맵 보정 + 직사각형 피팅\n'
        '빨강=원본벽  파랑=갭보정  연두=내부자유공간  노랑=피팅 사각형',
        color='white', fontsize=9)
    ax_map.set_xlabel('World X (m)', color='white')
    ax_map.set_ylabel('World Y (m)', color='white')
    ax_map.tick_params(colors='white')
    for sp in ax_map.spines.values():
        sp.set_edgecolor('#555555')

    # ── 오른쪽: 촬영 계획 ────────────────────────────────────────────────────
    ax = axes[1]
    ax.set_facecolor('#1a1a2e')
    ax.imshow(raw, cmap='gray', origin='lower', extent=extent, alpha=0.5, zorder=0)

    # 내부 자유공간 오버레이
    ax.imshow(free_vis, origin='lower', extent=extent, zorder=1)

    # 직사각형 4변 (변 번호별 색상)
    side_colors = ['#FF6B6B', '#4ECDC4', '#FFE66D', '#A8E6CF']  # 0~3변
    for i in range(4):
        p0w = rect_world[i]
        p1w = rect_world[(i+1) % 4]
        ax.plot([p0w[0], p1w[0]], [p0w[1], p1w[1]],
                '-', color=side_colors[i], linewidth=2.5,
                zorder=4, label=f'변 {i}', alpha=0.85)
    # 꼭짓점
    for k, (wx, wy) in enumerate(rect_world):
        ax.scatter(wx, wy, s=100, color='yellow', zorder=5,
                   edgecolors='white', linewidths=1.0)

    # 색상 팔레트 (촬영 위치 번호 순)
    colors = plt.cm.plasma(np.linspace(0.1, 0.9, max(len(poses), 1)))

    # 이동 경로선
    if len(poses) > 1:
        path_x = [start_x] + [p.world_x for p in poses]
        path_y = [start_y] + [p.world_y for p in poses]
        ax.plot(path_x, path_y, '--', color='#aaaaaa',
                linewidth=0.8, alpha=0.5, zorder=3)

    # 로봇 시작점
    ax.scatter(start_x, start_y, s=150, c='lime', marker='*',
               zorder=7, label='로봇 시작')

    # FOV 부채꼴 + 화살표 + 번호
    for i, (pose, color) in enumerate(zip(poses, colors)):
        c = tuple(color)
        draw_fov_wedge(ax, pose, fov_deg=62.2,
                       standoff_m=pose.standoff_m, color=c, alpha=0.20)

        ax.scatter(pose.world_x, pose.world_y, s=70, color=c,
                   zorder=6, edgecolors='white', linewidths=0.6)

        arrow_len = 0.22
        dx = arrow_len * math.cos(pose.yaw_rad)
        dy = arrow_len * math.sin(pose.yaw_rad)
        ax.annotate('', xy=(pose.world_x+dx, pose.world_y+dy),
                    xytext=(pose.world_x, pose.world_y),
                    arrowprops=dict(arrowstyle='->', color=c, lw=1.5), zorder=6)

        label_dx = -0.12 * math.sin(pose.yaw_rad)
        label_dy =  0.12 * math.cos(pose.yaw_rad)
        ax.text(pose.world_x+label_dx, pose.world_y+label_dy,
                str(i+1), fontsize=7, color='white', fontweight='bold',
                ha='center', va='center', zorder=8,
                bbox=dict(boxstyle='circle,pad=0.1', facecolor=c,
                          edgecolor='none', alpha=0.85))

        if pose.angle_to_wall > 0:
            ax.text(pose.world_x+label_dx*2.5, pose.world_y+label_dy*2.5,
                    f'{pose.angle_to_wall}°',
                    fontsize=5.5, color='yellow', alpha=0.9, zorder=8)

    # 범례
    side_patches = [mpatches.Patch(color=side_colors[i], label=f'변 {i}')
                    for i in range(4)]
    extra_patches = [
        mpatches.Patch(color='cyan',   alpha=0.7, label='수직 촬영'),
        mpatches.Patch(color='yellow', alpha=0.7, label='사선 촬영'),
    ]
    ax.legend(handles=side_patches + extra_patches,
              loc='upper right', fontsize=7,
              facecolor='#333355', edgecolor='none', labelcolor='white')

    diag_count = sum(1 for p in poses if p.angle_to_wall > 0)
    title = (f"촬영 계획  |  총 {len(poses)}개  "
             f"(수직 {len(poses)-diag_count} + 사선 {diag_count})\n"
             f"max_standoff={max_std}m  FOV=62.2°  최대 사선=30°")
    ax.set_title(title, color='white', fontsize=10, pad=12)
    ax.set_xlabel('World X (m)', color='white')
    ax.set_ylabel('World Y (m)', color='white')
    ax.tick_params(colors='white')
    for sp in ax.spines.values():
        sp.set_edgecolor('#555555')

    plt.tight_layout()
    out_path = pgm_path.replace('.pgm', '_coverage_sim.png')
    plt.savefig(out_path, dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    print(f"시뮬레이션 이미지 저장: {out_path}")
    plt.show()


if __name__ == '__main__':
    main()
