#!/usr/bin/env python3
"""
simulate_coverage.py — 촬영 위치 시뮬레이션 시각화
====================================================
ROS2 없이 PGM + YAML만으로 촬영 계획을 시각화한다.

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
from matplotlib.patches import FancyArrowPatch, Wedge
from matplotlib.colors import ListedColormap

# wall_coverage_planner가 같은 디렉토리에 있을 때
sys.path.insert(0, '.')
try:
    from qr_wall_scan.wall_coverage_planner import WallCoveragePlanner
except ImportError:
    from wall_coverage_planner import WallCoveragePlanner


def world_to_pixel(wx, wy, ox, oy, res, h):
    """월드 좌표 → 화면 픽셀 (matplotlib imshow 기준, y 미반전)."""
    px = (wx - ox) / res
    py = (wy - oy) / res
    return px, py


def draw_fov_wedge(ax, pose, fov_deg, standoff_m, res, color, alpha=0.15):
    """촬영 위치에서 FOV 부채꼴 그리기."""
    px = pose.world_x
    py = pose.world_y
    half_fov = fov_deg / 2.0
    yaw_deg  = pose.yaw_deg

    wedge = Wedge(
        center=(px, py),
        r=standoff_m,
        theta1=yaw_deg - half_fov,
        theta2=yaw_deg + half_fov,
        color=color,
        alpha=alpha,
        zorder=2,
    )
    ax.add_patch(wedge)


def main():
    # ── 인자 파싱 ──────────────────────────────────────────────────────────────
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    pgm_path  = sys.argv[1]
    yaml_path = sys.argv[2]
    max_std   = float(sys.argv[3]) if len(sys.argv) > 3 else 0.80
    start_x   = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
    start_y   = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0

    print(f"맵: {pgm_path}")
    print(f"파라미터: max_standoff={max_std}m  시작=({start_x},{start_y})")
    print("촬영 위치 계산 중...")

    # ── 플래너 실행 ────────────────────────────────────────────────────────────
    planner = WallCoveragePlanner(
        pgm_path, yaml_path,
        max_standoff_m=max_std,
        start_world_xy=(start_x, start_y),
    )
    poses = planner.generate(verbose=True)
    print()
    print(planner.summary(poses))
    print()

    # ── 시각화 ────────────────────────────────────────────────────────────────
    res = planner.res
    ox  = planner.ox
    oy  = planner.oy
    h   = planner.h

    raw     = cv2.imread(pgm_path, cv2.IMREAD_GRAYSCALE)
    raw     = cv2.flip(raw, 0)
    occ_raw = planner.occ_raw   # 원본 점유 마스크
    occ_fix = planner.occ       # 보정된 점유 마스크

    # 보정 전/후 비교 + 촬영 계획 — 2열 레이아웃
    fig, axes = plt.subplots(1, 2, figsize=(20, 10))
    fig.patch.set_facecolor('#1a1a2e')

    extent = [ox, ox + planner.w * res, oy, oy + planner.h * res]

    # ── 왼쪽: 보정 전/후 맵 비교 ─────────────────────────────────────────────
    ax_map = axes[0]
    ax_map.set_facecolor('#1a1a2e')
    ax_map.imshow(raw, cmap='gray', origin='lower',
                  extent=extent, alpha=0.6, zorder=0)

    # 원본 벽 (빨강)
    raw_wall = np.zeros((*occ_raw.shape, 4), dtype=np.float32)
    raw_wall[occ_raw == 1] = [1.0, 0.2, 0.2, 0.6]
    ax_map.imshow(raw_wall, origin='lower', extent=extent, zorder=1)

    # 보정 추가 픽셀 (파랑 = 갭 보정된 부분)
    added = np.logical_and(occ_fix == 1, occ_raw == 0).astype(np.uint8)
    fix_overlay = np.zeros((*added.shape, 4), dtype=np.float32)
    fix_overlay[added == 1] = [0.2, 0.6, 1.0, 0.8]
    ax_map.imshow(fix_overlay, origin='lower', extent=extent, zorder=2)

    ax_map.set_title('맵 보정 결과\n빨강=원본벽  파랑=갭 보정(연장선)',
                     color='white', fontsize=10)
    ax_map.set_xlabel('World X (m)', color='white')
    ax_map.set_ylabel('World Y (m)', color='white')
    ax_map.tick_params(colors='white')
    for sp in ax_map.spines.values():
        sp.set_edgecolor('#555555')

    # ── 오른쪽: 촬영 계획 ────────────────────────────────────────────────────
    ax  = axes[1]
    ax.set_facecolor('#1a1a2e')

    # 보정된 맵 배경
    ax.imshow(raw, cmap='gray', origin='lower',
              extent=extent, alpha=0.55, zorder=0)
    # 보정된 벽 오버레이 (반투명 파랑)
    occ_vis = np.zeros((*occ_fix.shape, 4), dtype=np.float32)
    occ_vis[occ_fix == 1] = [0.3, 0.5, 1.0, 0.35]
    ax.imshow(occ_vis, origin='lower', extent=extent, zorder=1)

    # ── 색상 팔레트 ──────────────────────────────────────────────────────────
    colors = plt.cm.plasma(np.linspace(0.1, 0.9, len(poses)))

    # ── 이동 경로선 ──────────────────────────────────────────────────────────
    if len(poses) > 1:
        path_x = [start_x] + [p.world_x for p in poses]
        path_y = [start_y] + [p.world_y for p in poses]
        ax.plot(path_x, path_y, '--', color='#aaaaaa',
                linewidth=0.8, alpha=0.5, zorder=3, label='이동 경로')

    # ── 시작 위치 ────────────────────────────────────────────────────────────
    ax.scatter(start_x, start_y, s=150, c='lime', marker='*',
               zorder=6, label='로봇 시작 위치')

    # ── FOV 부채꼴 + 촬영 위치 ───────────────────────────────────────────────
    for i, (pose, color) in enumerate(zip(poses, colors)):
        c = tuple(color)

        # FOV 부채꼴
        draw_fov_wedge(ax, pose, fov_deg=62.2,
                       standoff_m=pose.standoff_m, res=res,
                       color=c, alpha=0.18)

        # 로봇 위치 점
        ax.scatter(pose.world_x, pose.world_y, s=60, color=c,
                   zorder=5, edgecolors='white', linewidths=0.5)

        # 방향 화살표
        arrow_len = 0.25
        dx = arrow_len * math.cos(pose.yaw_rad)
        dy = arrow_len * math.sin(pose.yaw_rad)
        ax.annotate('', xy=(pose.world_x + dx, pose.world_y + dy),
                    xytext=(pose.world_x, pose.world_y),
                    arrowprops=dict(arrowstyle='->', color=c,
                                   lw=1.5), zorder=5)

        # 번호 라벨
        label_dx = -0.12 * math.sin(pose.yaw_rad)
        label_dy =  0.12 * math.cos(pose.yaw_rad)
        ax.text(pose.world_x + label_dx, pose.world_y + label_dy,
                str(i + 1),
                fontsize=7, color='white', fontweight='bold',
                ha='center', va='center', zorder=7,
                bbox=dict(boxstyle='circle,pad=0.1', facecolor=c,
                          edgecolor='none', alpha=0.8))

        # 사선 촬영 표시
        if pose.angle_to_wall > 0:
            ax.text(pose.world_x + label_dx*2.5,
                    pose.world_y + label_dy*2.5,
                    f'{pose.angle_to_wall}°',
                    fontsize=5.5, color='yellow', alpha=0.9, zorder=7)

    # ── 범례 + 정보 ──────────────────────────────────────────────────────────
    perp_patch  = mpatches.Patch(color='cyan',   alpha=0.6, label='수직 촬영')
    diag_patch  = mpatches.Patch(color='yellow', alpha=0.6, label='사선 촬영')
    ax.legend(handles=[perp_patch, diag_patch],
              loc='upper right', fontsize=8,
              facecolor='#333355', edgecolor='none', labelcolor='white')

    ax.set_xlabel('World X (m)', color='white')
    ax.set_ylabel('World Y (m)', color='white')
    ax.tick_params(colors='white')
    for spine in ax.spines.values():
        spine.set_edgecolor('#555555')

    diag_count = sum(1 for p in poses if p.angle_to_wall > 0)
    title = (f"촬영 계획  |  총 {len(poses)}개  "
             f"(수직 {len(poses)-diag_count} + 사선 {diag_count})\n"
             f"max_standoff={max_std}m  FOV=62.2°  최대 사선=30°")
    ax.set_title(title, color='white', fontsize=10, pad=12)

    plt.tight_layout()

    # 저장 + 표시
    out_path = pgm_path.replace('.pgm', '_coverage_sim.png')
    plt.savefig(out_path, dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    print(f"시뮬레이션 이미지 저장: {out_path}")
    plt.show()


if __name__ == '__main__':
    main()
