#!/usr/bin/env python3
"""
wall_coverage_planner.py — 외곽 벽 기반 최소 촬영 위치 자동 생성
=================================================================
SLAM으로 생성된 맵 이미지(PGM)를 분석하여 카메라 FOV 내에서
전체 외벽을 커버하는 최소한의 촬영 위치(ScanPose) 목록을 생성한다.

Pipeline
--------
1. PGM 로드 (픽셀값: 0=벽, 205=unknown, 254=자유공간)
   Y축 flip — PGM은 상단이 y=0이고 ROS는 하단이 y=0이므로 반전 필요
2. 254(자유공간)의 최대 연결 성분 → interior_free
   (내부 장애물로 분리된 복도 공간 제외, 로봇이 실제 이동하는 공간만)
3. interior_free의 RETR_EXTERNAL 컨투어
   → 내부 장애물(선반 등)의 벽 자동 제외, 외곽 벽만 추출
   → erode 2px 처리로 컨투어가 벽 픽셀 위에 올라가는 문제 해결
4. approxPolyDP로 픽셀 노이즈 제거
5. 8cm 간격 샘플링 + inward normal 계산
   (dist_transform 기반: 자유공간 방향이 항상 값이 큼)
6. 각 벽 포인트 → 내부 촬영 후보 탐색
   (standoff 0.80→0.30m, 사선 각도 ±30° 범위 탐색, LOS 검증)
7. Greedy Set Cover → 최소 촬영 횟수로 전체 외벽 커버
8. 최근접 이웃 정렬 → 이동 거리 최소화

Camera Spec
-----------
Raspberry Pi Camera v2 / FOV 62.2° / 640×480
QR 70mm 기준 최대 안정 인식 거리: 0.80m
"""

import math
import yaml
import numpy as np
import cv2
from dataclasses import dataclass, field
from typing import Optional


# ── 기본 파라미터 ──────────────────────────────────────────────────────────────
FOV_DEG         = 62.2
MAX_STANDOFF_M  = 0.80
MIN_STANDOFF_M  = 0.30
ROBOT_RADIUS_M  = 0.20
MAX_VIEW_ANGLE  = 30.0
WALL_SAMPLE_M   = 0.08    # 외곽 컨투어 샘플링 간격 (m)
APPROX_EPSILON  = 0.008   # approxPolyDP epsilon = APPROX_EPSILON × arcLength
SCAN_ANGLES_DEG = [0, 15, -15, 25, -25, 30, -30]


@dataclass
class ScanPose:
    world_x:       float
    world_y:       float
    yaw_rad:       float
    yaw_deg:       float
    angle_to_wall: float
    standoff_m:    float
    covers:        object = field(default_factory=set)


class WallCoveragePlanner:
    """
    외곽 벽(내부 장애물 제외)을 최소 촬영 횟수로 커버하는 위치 집합 생성.

    핵심: 자유공간(254)의 RETR_EXTERNAL 컨투어 = 외곽 벽 경계.
    내부 장애물(선반 등)의 벽은 이 컨투어에 포함되지 않음.
    """

    def __init__(
        self,
        pgm_path:        str,
        yaml_path:       str,
        fov_deg:         float = FOV_DEG,
        max_standoff_m:  float = MAX_STANDOFF_M,
        min_standoff_m:  float = MIN_STANDOFF_M,
        robot_radius_m:  float = ROBOT_RADIUS_M,
        max_view_angle:  float = MAX_VIEW_ANGLE,
        wall_sample_m:   float = WALL_SAMPLE_M,
        approx_epsilon:  float = APPROX_EPSILON,
        start_world_xy:  tuple = (0.0, 0.0),
    ):
        self.pgm_path       = pgm_path
        self.yaml_path      = yaml_path
        self.fov_rad        = math.radians(fov_deg)
        self.half_fov_rad   = self.fov_rad / 2.0
        self.max_standoff_m = max_standoff_m
        self.min_standoff_m = min_standoff_m
        self.robot_radius_m = robot_radius_m
        self.max_view_rad   = math.radians(max_view_angle)
        self.wall_sample_m  = wall_sample_m
        self.approx_epsilon = approx_epsilon
        self.start_world_xy = start_world_xy

        self.res           = None
        self.ox            = None
        self.oy            = None
        self.h             = None
        self.w             = None
        self.occ_raw       = None   # 원본 벽 마스크 (img==0)
        self.occ           = None   # = occ_raw (LOS용)
        self.interior_free = None   # 자유공간(254) 최대 연결 성분
        self.free_movable  = None   # img>0 (이동 가능 공간)
        self.dist          = None   # distTransform (free_movable 기준)
        self.outer_contour = None   # approxPolyDP 외곽 컨투어 (시각화용)

        self._load_map()

    # =========================================================================
    # 1. 맵 로드
    # =========================================================================
    def _load_map(self):
        with open(self.yaml_path, 'r') as f:
            meta = yaml.safe_load(f)
        self.res = float(meta['resolution'])
        origin   = meta['origin']
        self.ox  = float(origin[0])
        self.oy  = float(origin[1])

        img = cv2.imread(self.pgm_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(f"PGM 없음: {self.pgm_path}")
        img = cv2.flip(img, 0)
        self.h, self.w = img.shape

        # 픽셀 분류: 0=벽, 205=unknown, 254=자유공간
        occ = (img == 0).astype(np.uint8)
        occ[[0, -1], :] = 1
        occ[:, [0, -1]] = 1
        self.occ_raw = occ.copy()
        self.occ     = occ

        # interior_free: 254(자유공간)의 최대 연결 성분
        free254 = (img == 254).astype(np.uint8)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(free254, 8)
        if n <= 1:
            raise RuntimeError("자유공간(254) 없음 — PGM 확인")

        sx = int((self.start_world_xy[0] - self.ox) / self.res)
        sy = int((self.start_world_xy[1] - self.oy) / self.res)
        sx = max(0, min(sx, self.w - 1))
        sy = max(0, min(sy, self.h - 1))
        start_label = int(labels[sy, sx]) if free254[sy, sx] else 0
        idx = start_label if start_label > 0 else 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        self.interior_free = (labels == idx).astype(np.uint8)

        # 이동 가능 공간 = 비점유 (254 + 205)
        self.free_movable = (img > 0).astype(np.uint8)

        # 거리 변환: 벽(0)으로부터 거리
        self.dist = cv2.distanceTransform(self.free_movable, cv2.DIST_L2, 5)

    # =========================================================================
    # 2. 외곽 컨투어 추출 (내부 장애물 제외)
    # =========================================================================
    def _extract_outer_contour(self) -> np.ndarray:
        """
        외벽 외곽 컨투어 추출.

        interior_free를 2px erode한 뒤 RETR_EXTERNAL로 외부 컨투어만 추출.
        - erode 목적: 굵은 벽의 중심이 아닌 자유공간 쪽(안쪽 경계)을 따르게 함
          → 보간점이 벽 픽셀(=0) 위에 올라가는 비율: 21% → 0.4%
        - RETR_EXTERNAL: 내부 장애물(선반 등)의 컨투어는 자동 제외됨

        Returns
        -------
        np.ndarray
            approxPolyDP 적용된 외곽 컨투어 (N×1×2 형태)
        """
        k = np.ones((3, 3), np.uint8)
        interior_inner = cv2.erode(self.interior_free, k, iterations=2)

        contours, _ = cv2.findContours(
            interior_inner, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not contours:
            # fallback: erode 없이 재시도
            contours, _ = cv2.findContours(
                self.interior_free, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not contours:
            raise RuntimeError("외곽 컨투어 없음 — interior_free 확인")
        outer = max(contours, key=cv2.contourArea)

        eps    = self.approx_epsilon * cv2.arcLength(outer, True)
        approx = cv2.approxPolyDP(outer, eps, True)
        self.outer_contour = approx  # 시각화용 저장
        return approx

    # =========================================================================
    # 3. 벽 포인트 샘플링 + inward normal
    # =========================================================================
    def _sample_wall_points(self, contour: np.ndarray) -> list:
        """
        외곽 컨투어를 wall_sample_m(기본 8cm) 간격으로 균등 샘플링.

        각 샘플 포인트에서 inward normal(내부 방향 법선)을 계산한다.
        법선 방향 결정: 좌·우 후보 중 dist_transform 값이 큰 쪽이 자유공간 방향.

        Returns
        -------
        list of (px, py, nx, ny)
            px, py : 벽 포인트 픽셀 좌표 (float)
            nx, ny : 자유공간 방향 단위 법선 벡터
        """
        interval_px = max(1.0, self.wall_sample_m / self.res)
        pts     = contour[:, 0, :]   # (N, 2)
        n_pts   = len(pts)
        sampled = []
        accum   = 0.0

        for i in range(n_pts):
            p0 = pts[i].astype(float)
            p1 = pts[(i + 1) % n_pts].astype(float)
            seg_len = math.hypot(p1[0]-p0[0], p1[1]-p0[1])
            if seg_len < 1e-6:
                continue

            while accum <= seg_len:
                t  = accum / seg_len
                px = p0[0] + t * (p1[0] - p0[0])
                py = p0[1] + t * (p1[1] - p0[1])

                pi = (i - 1) % n_pts
                ni = (i + 2) % n_pts
                tx = float(pts[ni][0] - pts[pi][0])
                ty = float(pts[ni][1] - pts[pi][1])
                tl = math.hypot(tx, ty)

                if tl > 1e-6:
                    n1x, n1y = -ty/tl,  tx/tl
                    n2x, n2y =  ty/tl, -tx/tl

                    def _d(nx, ny, step=3):
                        sx_ = int(px + nx * step)
                        sy_ = int(py + ny * step)
                        if 0 <= sx_ < self.w and 0 <= sy_ < self.h:
                            return float(self.dist[sy_, sx_])
                        return 0.0

                    nx, ny = (n1x, n1y) if _d(n1x, n1y) >= _d(n2x, n2y) else (n2x, n2y)
                    sampled.append((float(px), float(py), nx, ny))

                accum += interval_px
            accum -= seg_len

        return sampled

    # =========================================================================
    # 4. 촬영 후보 탐색
    # =========================================================================
    def _find_scan_position(
        self,
        wall_px: float, wall_py: float,
        nx: float, ny: float,
    ) -> Optional[ScanPose]:
        """
        벽 포인트 (wall_px, wall_py)를 촬영할 수 있는 최적 로봇 위치를 탐색.

        탐색 전략
        ---------
        사선 각도 [0°, ±15°, ±25°, ±30°] × standoff [0.80→0.30m, 5cm씩 감소]
        순서로 후보를 탐색하여 최초로 통과하는 위치를 반환.

        통과 조건 (AND)
        ---------------
        1. 맵 경계 내부
        2. 이동 가능 공간 (free_movable > 0)
        3. 벽으로부터 로봇 반경(0.20m) 이상 거리 (dist_transform 기준)
        4. 벽 포인트까지 시야 확보 (Bresenham LOS)

        Returns
        -------
        ScanPose or None
        """
        robot_r_px = self.robot_radius_m / self.res

        for angle_deg in SCAN_ANGLES_DEG:
            if abs(angle_deg) > math.degrees(self.max_view_rad):
                continue
            a = math.radians(angle_deg)
            ca, sa = math.cos(a), math.sin(a)
            dx = ca * nx - sa * ny
            dy = sa * nx + ca * ny

            for standoff_m in np.arange(self.max_standoff_m, self.min_standoff_m - 0.01, -0.05):
                sp = standoff_m / self.res
                cx = int(round(wall_px + dx * sp))
                cy = int(round(wall_py + dy * sp))

                if not (1 <= cx < self.w-1 and 1 <= cy < self.h-1):
                    continue
                if self.free_movable[cy, cx] == 0:
                    continue
                if self.dist[cy, cx] < robot_r_px:
                    continue
                if not self._line_of_sight(int(wall_px), int(wall_py), cx, cy):
                    continue

                world_x = cx * self.res + self.ox
                world_y = cy * self.res + self.oy
                wall_wx = wall_px * self.res + self.ox
                wall_wy = wall_py * self.res + self.oy
                yaw     = math.atan2(wall_wy - world_y, wall_wx - world_x)

                return ScanPose(
                    world_x       = round(world_x, 4),
                    world_y       = round(world_y, 4),
                    yaw_rad       = round(yaw, 4),
                    yaw_deg       = round(math.degrees(yaw), 1),
                    angle_to_wall = abs(angle_deg),
                    standoff_m    = round(standoff_m, 2),
                )
        return None

    # =========================================================================
    # 5. Bresenham LOS
    # =========================================================================
    def _line_of_sight(self, x0: int, y0: int, x1: int, y1: int) -> bool:
        dx = abs(x1-x0); dy = abs(y1-y0)
        sx = 1 if x1 > x0 else -1
        sy = 1 if y1 > y0 else -1
        err = dx - dy
        x, y = x0, y0
        while True:
            if x == x1 and y == y1: return True
            if not (0 <= x < self.w and 0 <= y < self.h): return False
            if (x != x0 or y != y0) and self.occ[y, x] == 1: return False
            e2 = 2 * err
            if e2 > -dy: err -= dy; x += sx
            if e2 <  dx: err += dx; y += sy

    # =========================================================================
    # 6. FOV 커버리지
    # =========================================================================
    def _compute_coverage(self, pose: ScanPose, wall_pts: list) -> set:
        """
        주어진 ScanPose에서 볼 수 있는 벽 포인트 인덱스 집합을 반환.

        포인트 포함 조건 (AND)
        ----------------------
        1. 거리 ≤ max_standoff_m (0.80m)
        2. FOV 내 (카메라 화각 62.2° = ±31.1°)
        3. 시야각 ≤ max_view_angle (30°) — 벽 법선 기준 사선 제한
        4. Bresenham LOS 통과

        Returns
        -------
        set of int — 커버되는 wall_pts 인덱스
        """
        covered = set()
        ppx = (pose.world_x - self.ox) / self.res
        ppy = (pose.world_y - self.oy) / self.res
        ipx, ipy = int(round(ppx)), int(round(ppy))

        for i, (wx, wy, nxi, nyi) in enumerate(wall_pts):
            ddx = wx - ppx; ddy = wy - ppy
            d   = math.hypot(ddx, ddy)
            if d < 1e-3: covered.add(i); continue
            if d * self.res > self.max_standoff_m: continue

            apt  = math.atan2(ddy, ddx)
            diff = abs(math.atan2(math.sin(apt - pose.yaw_rad),
                                  math.cos(apt - pose.yaw_rad)))
            if diff > self.half_fov_rad: continue

            vdx = -ddx/d; vdy = -ddy/d
            dot = max(-1.0, min(1.0, nxi*vdx + nyi*vdy))
            if math.acos(dot) > self.max_view_rad: continue

            if not self._line_of_sight(ipx, ipy, int(wx), int(wy)): continue
            covered.add(i)

        return covered

    # =========================================================================
    # 7. 그리디 셋 커버
    # =========================================================================
    def _greedy_coverage(self, candidates: list, n_wall_pts: int) -> list:
        uncovered = set(range(n_wall_pts))
        selected  = []
        remaining = candidates.copy()
        while uncovered and remaining:
            best = max(remaining, key=lambda p: len(p.covers & uncovered))
            gain = best.covers & uncovered
            if not gain: break
            selected.append(best)
            uncovered -= gain
            remaining.remove(best)
        return selected

    # =========================================================================
    # 8. 최근접 이웃 정렬
    # =========================================================================
    def _sort_nearest_neighbor(self, poses: list) -> list:
        if not poses: return []
        remaining = list(poses)
        cx, cy = self.start_world_xy
        result = []
        while remaining:
            nearest = min(remaining, key=lambda p: math.hypot(p.world_x-cx, p.world_y-cy))
            result.append(nearest)
            cx, cy = nearest.world_x, nearest.world_y
            remaining.remove(nearest)
        return result

    # =========================================================================
    # 메인
    # =========================================================================
    def generate(self, verbose: bool = False) -> list:
        if verbose:
            px = int(self.interior_free.sum())
            print(f"[1/5] 맵 로드: {self.w}×{self.h}  자유공간 {px}px ({px*self.res**2:.2f}㎡)")

        contour  = self._extract_outer_contour()
        wall_pts = self._sample_wall_points(contour)
        n_pts    = len(wall_pts)
        if verbose:
            print(f"[2/5] 외곽 컨투어 {len(contour)}pts → 샘플 {n_pts}개")

        pose_map: dict = {}
        skipped = 0
        for wx, wy, nx, ny in wall_pts:
            pose = self._find_scan_position(wx, wy, nx, ny)
            if pose is None:
                skipped += 1
                continue
            key = (round(pose.world_x, 2), round(pose.world_y, 2))
            if key not in pose_map:
                pose_map[key] = pose

        candidates = list(pose_map.values())
        if verbose:
            print(f"[3/5] 촬영 후보: {len(candidates)}개  커버불가: {skipped}개")
        if not candidates:
            raise RuntimeError("유효한 촬영 위치 없음")

        for pose in candidates:
            pose.covers = self._compute_coverage(pose, wall_pts)

        selected = self._greedy_coverage(candidates, n_pts)
        if verbose:
            covered = len(set().union(*[p.covers for p in selected]))
            print(f"[4/5] 그리디 선택: {len(selected)}개  ({covered}/{n_pts} 커버)")

        result = self._sort_nearest_neighbor(selected)
        if verbose:
            print(f"[5/5] 정렬 완료 (시작: {self.start_world_xy})")
        return result

    def summary(self, poses: list) -> str:
        lines = [
            f"촬영 위치 총 {len(poses)}개",
            f"  해상도: {self.res}m/cell  맵: {self.w*self.res:.1f}×{self.h*self.res:.1f}m",
            f"  standoff: {self.min_standoff_m}~{self.max_standoff_m}m  최대 사선: {math.degrees(self.max_view_rad):.0f}°",
            "",
        ]
        for i, p in enumerate(poses):
            diag = f" (사선 {p.angle_to_wall}°)" if p.angle_to_wall > 0 else ""
            lines.append(
                f"  [{i+1:02d}] x={p.world_x:7.3f}  y={p.world_y:7.3f}  "
                f"yaw={p.yaw_deg:6.1f}°  dist={p.standoff_m:.2f}m{diag}"
            )
        return "\n".join(lines)


def generate_scan_positions(pgm_path, yaml_path, start_world_xy=(0.0,0.0), **kwargs):
    planner = WallCoveragePlanner(pgm_path, yaml_path,
                                  start_world_xy=start_world_xy, **kwargs)
    return planner.generate()


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3:
        print("사용법: python3 wall_coverage_planner.py <map.pgm> <map.yaml>")
        sys.exit(1)
    planner = WallCoveragePlanner(sys.argv[1], sys.argv[2])
    poses   = planner.generate(verbose=True)
    print(); print(planner.summary(poses))
