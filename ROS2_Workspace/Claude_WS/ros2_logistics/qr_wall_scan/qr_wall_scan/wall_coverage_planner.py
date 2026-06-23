#!/usr/bin/env python3
"""
wall_coverage_planner.py — FOV 기반 외곽 벽 촬영 좌표 생성
============================================================
알고리즘 개요:
  1. PGM 맵 로드 + 거리 변환 계산
  2. 외곽 벽 컨투어 추출 → 일정 간격 샘플링
  3. 각 벽 포인트마다 촬영 후보 탐색
       순서: 수직(0°) → ±15° → ±25° → ±30° 사선
       standoff = min(dist_transform, MAX_STANDOFF) 동적 결정
  4. 그리디 셋 커버: 최소 촬영 횟수로 전체 벽 커버
  5. 최근접 이웃 정렬 (로봇 시작 위치 기준)

카메라 스펙 기준:
  - 라즈베리파이 카메라 v2 / 수평 FOV 62.2°
  - 최저 해상도 640×480 기준 QR(70mm) 안정 인식 거리 ≤ 0.8m

사용:
  from qr_wall_scan.wall_coverage_planner import WallCoveragePlanner

  planner = WallCoveragePlanner(pgm_path, yaml_path,
                                 start_world_xy=(rx, ry))
  poses = planner.generate()
  # → [ScanPose(world_x, world_y, yaw_rad, ...), ...]
"""

import math
import yaml
import numpy as np
import cv2
from dataclasses import dataclass, field
from typing import Optional


# ── 기본 파라미터 ──────────────────────────────────────────────────────────────
FOV_DEG          = 62.2    # 라즈베리파이 카메라 v2 수평 화각
MAX_STANDOFF_M   = 0.80    # 최대 촬영 거리 (640×480 / QR 70mm 기준)
MIN_STANDOFF_M   = 0.30    # 최소 촬영 거리 (로봇 안전 여유)
ROBOT_RADIUS_M   = 0.20    # TurtleBot3 Burger 반경
MAX_VIEW_ANGLE   = 30.0    # 벽 법선 기준 최대 사선 각도 (°)
WALL_SAMPLE_M    = 0.08    # 벽 포인트 샘플링 간격 (m)
SCAN_ANGLES_DEG  = [0, 15, -15, 25, -25, 30, -30]  # 촬영 시도 각도 순서


# ── 데이터 클래스 ──────────────────────────────────────────────────────────────
@dataclass
class ScanPose:
    """로봇이 이동할 촬영 위치."""
    world_x:       float         # /map 프레임 x (m)
    world_y:       float         # /map 프레임 y (m)
    yaw_rad:       float         # 벽을 정면으로 바라보는 방향 (rad)
    yaw_deg:       float         # 같은 값 (°, 사람이 읽기용)
    angle_to_wall: float         # 벽 법선에 대한 사선 각도 (°)
    standoff_m:    float         # 벽까지 실제 촬영 거리 (m)
    covers:        object = field(default_factory=set)  # 커버 벽 포인트 인덱스 set


class WallCoveragePlanner:
    """
    외곽 벽 전체를 최소 촬영 횟수로 커버하는 촬영 위치 집합 생성.

    파라미터:
        pgm_path       : map.pgm 경로
        yaml_path      : map.yaml 경로
        fov_deg        : 카메라 수평 화각 (기본 62.2°)
        max_standoff_m : 최대 촬영 거리 (기본 0.80m)
        min_standoff_m : 최소 촬영 거리 (기본 0.30m)
        robot_radius_m : 로봇 반경 (기본 0.20m)
        max_view_angle : 최대 사선 각도 (기본 30°)
        wall_sample_m  : 벽 샘플링 간격 (기본 0.08m)
        start_world_xy : 로봇 시작 월드 좌표 (기본 (0,0))
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
        self.start_world_xy = start_world_xy

        # 맵 내부 데이터 (로드 후 설정)
        self.res  = None
        self.ox   = None
        self.oy   = None
        self.h    = None
        self.w    = None
        self.occ  = None   # 점유 마스크 uint8 (1=벽)
        self.dist = None   # distanceTransform (픽셀 단위)

        self._load_map()

    # =========================================================================
    # 1. 맵 로드 + 거리 변환
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
        img = cv2.flip(img, 0)           # ROS y축 보정 (하단=y=0)
        self.h, self.w = img.shape

        occ = (img < 50).astype(np.uint8)
        occ[[0, -1], :] = 1              # 가장자리 폐쇄 (열린 벽 방어)
        occ[:, [0, -1]] = 1
        self.occ = occ

        # 자유 공간에서 가장 가까운 벽까지 거리 (픽셀)
        free_mask = (occ == 0).astype(np.uint8)
        self.dist = cv2.distanceTransform(free_mask, cv2.DIST_L2, 5)

    # =========================================================================
    # 2. 외곽 벽 컨투어 추출
    # =========================================================================
    def _extract_wall_contour(self):
        """가장 큰 점유 연결 성분 = 외곽 벽 컨투어 반환."""
        n, labels, stats, _ = cv2.connectedComponentsWithStats(self.occ, 8)
        if n <= 1:
            raise RuntimeError("점유 영역 없음 — PGM 확인 필요")
        idx   = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        outer = (labels == idx).astype(np.uint8)
        contours, _ = cv2.findContours(
            outer, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        return max(contours, key=cv2.contourArea)

    # =========================================================================
    # 3. 벽 포인트 샘플링 + 법선 계산
    # =========================================================================
    def _sample_wall_points(self, contour) -> list:
        """
        컨투어를 wall_sample_m 간격으로 샘플링.
        각 포인트에 inward normal(자유 공간 방향 단위벡터) 포함.

        Returns: [(px, py, nx, ny), ...]  (픽셀 좌표 + 법선)
        """
        interval_px = max(1.0, self.wall_sample_m / self.res)
        pts     = contour[:, 0, :]   # shape (N, 2)
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

                # 탄젠트: 이웃 포인트로 계산
                pi  = (i - 1) % n_pts
                ni  = (i + 2) % n_pts
                tx  = float(pts[ni][0] - pts[pi][0])
                ty  = float(pts[ni][1] - pts[pi][1])
                tl  = math.hypot(tx, ty)
                if tl < 1e-6:
                    accum += interval_px
                    continue

                # 두 후보 법선 (탄젠트 ±90°)
                n1x, n1y = -ty/tl,  tx/tl
                n2x, n2y =  ty/tl, -tx/tl

                # dist_transform이 더 큰 쪽 = 자유 공간 방향 = inward normal
                def _d(nx, ny, step=3):
                    sx, sy = int(px + nx*step), int(py + ny*step)
                    if 0 <= sx < self.w and 0 <= sy < self.h:
                        return float(self.dist[sy, sx])
                    return 0.0

                nx, ny = (n1x, n1y) if _d(n1x, n1y) >= _d(n2x, n2y) else (n2x, n2y)
                sampled.append((float(px), float(py), nx, ny))
                accum += interval_px

            accum -= seg_len

        return sampled

    # =========================================================================
    # 4. 촬영 후보 탐색 (단일 벽 포인트)
    # =========================================================================
    def _find_scan_position(
        self, wall_px: float, wall_py: float, nx: float, ny: float
    ) -> Optional[ScanPose]:
        """
        벽 포인트 (wall_px, wall_py) / inward normal (nx, ny) 기준으로
        유효한 촬영 위치 탐색.

        시도 순서: 0° → ±15° → ±25° → ±30°
        각 각도에서 MAX_STANDOFF → MIN_STANDOFF 방향으로 줄여가며 탐색.
        첫 유효 위치 반환, 전부 실패 시 None.
        """
        robot_r_px = self.robot_radius_m / self.res

        for angle_deg in SCAN_ANGLES_DEG:
            if abs(angle_deg) > math.degrees(self.max_view_rad):
                continue

            a = math.radians(angle_deg)
            ca, sa = math.cos(a), math.sin(a)
            # 법선을 angle_deg만큼 회전
            dx = ca * nx - sa * ny
            dy = sa * nx + ca * ny

            # MAX → MIN standoff 0.05m 간격으로 탐색
            standoff_range = np.arange(
                self.max_standoff_m, self.min_standoff_m - 0.01, -0.05)
            for standoff_m in standoff_range:
                sp = standoff_m / self.res
                cx = int(round(wall_px + dx * sp))
                cy = int(round(wall_py + dy * sp))

                # 맵 범위 체크
                if not (1 <= cx < self.w-1 and 1 <= cy < self.h-1):
                    continue
                # 자유 공간 + 로봇 반경 여유
                if self.dist[cy, cx] < robot_r_px:
                    continue
                # 시선 확보 (Bresenham)
                if not self._line_of_sight(int(wall_px), int(wall_py), cx, cy):
                    continue

                # 월드 좌표 변환
                world_x  = cx * self.res + self.ox
                world_y  = cy * self.res + self.oy
                wall_wx  = wall_px * self.res + self.ox
                wall_wy  = wall_py * self.res + self.oy
                yaw      = math.atan2(wall_wy - world_y, wall_wx - world_x)

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
    # 5. Bresenham 시선 체크
    # =========================================================================
    def _line_of_sight(self, x0: int, y0: int, x1: int, y1: int) -> bool:
        """두 픽셀 사이에 장애물(occ==1)이 없으면 True."""
        dx = abs(x1 - x0); dy = abs(y1 - y0)
        sx = 1 if x1 > x0 else -1
        sy = 1 if y1 > y0 else -1
        err = dx - dy
        x, y = x0, y0

        while True:
            if x == x1 and y == y1:
                return True
            if not (0 <= x < self.w and 0 <= y < self.h):
                return False
            # 출발/도착점 제외한 중간만 장애물 체크
            if (x != x0 or y != y0) and self.occ[y, x] == 1:
                return False
            e2 = 2 * err
            if e2 > -dy:
                err -= dy; x += sx
            if e2 < dx:
                err += dx; y += sy

    # =========================================================================
    # 6. FOV 커버리지 계산
    # =========================================================================
    def _compute_coverage(self, pose: ScanPose, wall_pts: list) -> set:
        """
        pose 위치에서 볼 수 있는 wall_pts 인덱스 집합 반환.
        조건: ① 거리 ≤ max_standoff  ② FOV 내  ③ 사선각 ≤ max_view_angle  ④ 시선 확보
        """
        covered = set()
        px = (pose.world_x - self.ox) / self.res
        py = (pose.world_y - self.oy) / self.res
        ipx, ipy = int(round(px)), int(round(py))

        for i, (wx, wy, nx, ny) in enumerate(wall_pts):
            ddx  = wx - px
            ddy  = wy - py
            dist = math.hypot(ddx, ddy)
            if dist < 1e-3:
                covered.add(i); continue

            # ① 거리
            if dist * self.res > self.max_standoff_m:
                continue

            # ② FOV: pose의 yaw 기준 ±half_fov 이내
            angle_to_pt = math.atan2(ddy, ddx)
            diff = abs(math.atan2(
                math.sin(angle_to_pt - pose.yaw_rad),
                math.cos(angle_to_pt - pose.yaw_rad)
            ))
            if diff > self.half_fov_rad:
                continue

            # ③ 사선각: 벽 법선과 촬영 방향의 각도
            view_dx = -ddx / dist
            view_dy = -ddy / dist
            dot = max(-1.0, min(1.0, nx * view_dx + ny * view_dy))
            if math.acos(dot) > self.max_view_rad:
                continue

            # ④ 시선
            if not self._line_of_sight(ipx, ipy, int(wx), int(wy)):
                continue

            covered.add(i)

        return covered

    # =========================================================================
    # 7. 그리디 셋 커버
    # =========================================================================
    def _greedy_coverage(self, candidates: list, n_wall_pts: int) -> list:
        """
        최소 촬영 횟수로 전체 벽 포인트를 커버하는 그리디 알고리즘.
        덮이지 않는 포인트가 남아도 더 이상 커버 불가면 종료.
        """
        uncovered = set(range(n_wall_pts))
        selected  = []
        remaining = candidates.copy()

        while uncovered and remaining:
            best = max(remaining, key=lambda p: len(p.covers & uncovered))
            gain = best.covers & uncovered
            if not gain:
                break
            selected.append(best)
            uncovered -= gain
            remaining.remove(best)

        return selected

    # =========================================================================
    # 8. 최근접 이웃 정렬
    # =========================================================================
    def _sort_nearest_neighbor(self, poses: list) -> list:
        """로봇 시작 위치 기준 최근접 이웃 순으로 정렬 (greedy TSP)."""
        if not poses:
            return []
        remaining = list(poses)
        cx, cy    = self.start_world_xy
        result    = []
        while remaining:
            nearest = min(remaining,
                          key=lambda p: math.hypot(p.world_x-cx, p.world_y-cy))
            result.append(nearest)
            cx, cy = nearest.world_x, nearest.world_y
            remaining.remove(nearest)
        return result

    # =========================================================================
    # 메인 생성 함수
    # =========================================================================
    def generate(self, verbose: bool = False) -> list:
        """
        전체 파이프라인 실행.

        Returns:
            [ScanPose, ...]  — 로봇 시작 위치 기준 최근접 순
        """
        if verbose:
            print(f"[1/5] 맵 로드 완료: {self.w}×{self.h} cells, res={self.res}m")

        contour   = self._extract_wall_contour()
        wall_pts  = self._sample_wall_points(contour)
        n_pts     = len(wall_pts)
        if verbose:
            print(f"[2/5] 벽 포인트 샘플링: {n_pts}개 ({self.wall_sample_m}m 간격)")

        # 각 벽 포인트 → 촬영 후보 (중복 위치 제거)
        pose_map: dict[tuple, ScanPose] = {}
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
            print(f"[3/5] 촬영 후보: {len(candidates)}개 "
                  f"(커버 불가 벽 포인트: {skipped}개)")

        if not candidates:
            raise RuntimeError("유효한 촬영 위치 없음 — 맵 또는 파라미터 확인")

        # 커버리지 계산
        for pose in candidates:
            pose.covers = self._compute_coverage(pose, wall_pts)

        # 그리디 커버
        selected = self._greedy_coverage(candidates, n_pts)
        if verbose:
            covered = len(set().union(*[p.covers for p in selected]))
            print(f"[4/5] 그리디 선택: {len(selected)}개 촬영 위치 "
                  f"({covered}/{n_pts} 벽 포인트 커버)")

        # 최근접 이웃 정렬
        result = self._sort_nearest_neighbor(selected)
        if verbose:
            print(f"[5/5] 정렬 완료 (시작: {self.start_world_xy})")
        return result

    # =========================================================================
    # 정보 출력
    # =========================================================================
    def summary(self, poses: list) -> str:
        lines = [
            f"촬영 위치 총 {len(poses)}개",
            f"  맵 해상도: {self.res}m/cell  크기: {self.w*self.res:.1f}×{self.h*self.res:.1f}m",
            f"  standoff 범위: {self.min_standoff_m}~{self.max_standoff_m}m  "
            f"최대 사선: {math.degrees(self.max_view_rad):.0f}°",
            "",
        ]
        for i, p in enumerate(poses):
            diag = f" (사선 {p.angle_to_wall}°)" if p.angle_to_wall > 0 else ""
            lines.append(
                f"  [{i+1:02d}] x={p.world_x:7.3f}  y={p.world_y:7.3f}  "
                f"yaw={p.yaw_deg:6.1f}°  dist={p.standoff_m:.2f}m{diag}"
            )
        return "\n".join(lines)


# ── 편의 함수 (ROS 노드에서 import해서 사용) ──────────────────────────────────
def generate_scan_positions(
    pgm_path:       str,
    yaml_path:      str,
    start_world_xy: tuple = (0.0, 0.0),
    **kwargs,
) -> list:
    """WallCoveragePlanner 편의 래퍼."""
    planner = WallCoveragePlanner(
        pgm_path, yaml_path,
        start_world_xy=start_world_xy,
        **kwargs,
    )
    return planner.generate()


# ── 독립 실행 (빠른 확인용) ──────────────────────────────────────────────────
if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3:
        print("사용법: python3 wall_coverage_planner.py <map.pgm> <map.yaml>")
        sys.exit(1)

    planner = WallCoveragePlanner(sys.argv[1], sys.argv[2])
    poses   = planner.generate(verbose=True)
    print()
    print(planner.summary(poses))
