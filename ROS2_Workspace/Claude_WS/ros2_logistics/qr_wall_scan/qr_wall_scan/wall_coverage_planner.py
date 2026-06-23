#!/usr/bin/env python3
"""
wall_coverage_planner.py — 직사각형 외곽 기반 FOV 촬영 좌표 생성
=================================================================
파이프라인:
  1. PGM 로드 + 외곽 갭 보정 (모폴로지 닫힘)
  2. 외부 flood fill → 건물 내부 자유공간(interior_free) 추출
  3. minAreaRect → 외곽 4변(직사각형) 피팅  ← 울퉁불퉁한 실제 벽 대신
  4. 직사각형 4변을 일정 간격으로 샘플링 + 내향 법선 계산
  5. 각 벽 포인트 → 내부 장애물 회피 촬영 후보 탐색 (0°→±15°→±25°→±30°)
  6. 그리디 셋 커버: 최소 촬영 횟수로 4변 전체 커버
  7. 최근접 이웃 정렬 (로봇 시작 위치 기준)

카메라: Raspberry Pi Camera v2 / 수평 FOV 62.2° / 최저 해상도 640×480
QR 70mm 기준 최대 안정 인식 거리 ≤ 0.80m
"""

import math
import yaml
import numpy as np
import cv2
from dataclasses import dataclass, field
from typing import Optional


# ── 기본 파라미터 ──────────────────────────────────────────────────────────────
FOV_DEG         = 62.2    # 수평 화각
MAX_STANDOFF_M  = 0.80    # 최대 촬영 거리
MIN_STANDOFF_M  = 0.30    # 최소 촬영 거리
ROBOT_RADIUS_M  = 0.20    # TurtleBot3 Burger 반경
MAX_VIEW_ANGLE  = 30.0    # 최대 사선 각도 (벽 법선 기준, °)
WALL_SAMPLE_M   = 0.08    # 직사각형 변 샘플링 간격 (m)
SCAN_ANGLES_DEG = [0, 15, -15, 25, -25, 30, -30]


# ── 데이터 클래스 ──────────────────────────────────────────────────────────────
@dataclass
class ScanPose:
    world_x:       float
    world_y:       float
    yaw_rad:       float
    yaw_deg:       float
    angle_to_wall: float        # 벽 법선에 대한 사선 (°)
    standoff_m:    float
    wall_side:     int = 0      # 어느 변인지 (0~3)
    covers:        object = field(default_factory=set)


class WallCoveragePlanner:
    """
    외곽 직사각형 4변을 최소 촬영 횟수로 커버하는 위치 집합 생성.

    직사각형은 울퉁불퉁한 LiDAR 맵을 보정해 얻은 이상적인 외곽.
    촬영 후보는 반드시 건물 내부 자유공간(interior_free)에 위치해야 함.
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

        # 로드 후 설정
        self.res           = None
        self.ox            = None
        self.oy            = None
        self.h             = None
        self.w             = None
        self.occ_raw       = None   # 원본 점유 마스크 (시각화용)
        self.occ           = None   # 갭 보정된 점유 마스크 (시각화용)
        self.interior_free = None   # 건물 내부 자유공간 (flood fill 결과)
        self.dist          = None   # distanceTransform (interior_free 기준)
        self.rect_corners  = None   # 직사각형 4꼭짓점 픽셀 좌표 (시각화용)

        self._load_map()

    # =========================================================================
    # 1. 맵 로드 + 갭 보정 + 외부 flood fill
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

        # 원본 점유 마스크
        occ = (img < 50).astype(np.uint8)
        occ[[0, -1], :] = 1
        occ[:, [0, -1]] = 1
        self.occ_raw = occ.copy()

        # 큰 커널로 외벽 갭 보정 (외벽이 완전히 닫혀야 내부 추출 가능)
        occ_closed = self._close_wall_gaps(occ, max_gap_m=0.50)
        self.occ   = occ_closed

        # 외벽 컨투어 내부 채우기 → 건물 내부 자유공간 추출
        # (외부 flood fill 대신 — 갭 크기와 무관하게 작동)
        self.interior_free = self._fill_building_interior(occ_closed, occ)

        # 거리 변환 (interior_free 기준)
        self.dist = cv2.distanceTransform(self.interior_free, cv2.DIST_L2, 5)

    # =========================================================================
    # 1-1. 외곽 갭 보정 (점유 마스크 기준 모폴로지 닫힘)
    # =========================================================================
    def _close_wall_gaps(self, occ: np.ndarray, max_gap_m: float = 0.50) -> np.ndarray:
        """
        max_gap_m 이하의 벽 갭을 모폴로지 닫힘으로 브리징.
        기본 0.50m — 외벽이 완전히 닫혀야 내부 채우기가 정확해짐.
        """
        k      = max(3, int(max_gap_m / self.res))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        return cv2.morphologyEx(occ, cv2.MORPH_CLOSE, kernel)

    # =========================================================================
    # 1-2. 외벽 컨투어 내부 채우기 → 건물 내부 자유공간 추출
    # =========================================================================
    def _fill_building_interior(
        self, occ_closed: np.ndarray, occ_raw: np.ndarray
    ) -> np.ndarray:
        """
        외벽 컨투어 내부를 채워 건물 전체 내부 영역을 구한 뒤,
        원본 점유 픽셀(occ_raw)을 제외하면 내부 자유공간이 남음.

        외부 flood fill 대신 이 방법을 쓰면 갭 크기와 무관하게 안정적으로 동작.

        알고리즘:
          1. occ_closed의 가장 큰 연결 성분 = 외벽 덩어리
          2. 그 외부 컨투어를 FILLED로 채움 → building_mask (내부 + 벽 포함)
          3. building_mask & ~occ_raw = 내부 자유공간
        """
        # 가장 큰 연결 성분 (외벽)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(occ_closed, 8)
        if n <= 1:
            raise RuntimeError("점유 영역 없음 — PGM 확인 필요")
        idx        = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        outer_mask = (labels == idx).astype(np.uint8)

        # 외부 컨투어 → 내부까지 채우기
        contours, _ = cv2.findContours(
            outer_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        filled = np.zeros_like(occ_closed)
        cv2.drawContours(filled, contours, -1, 1, thickness=cv2.FILLED)

        # 내부 자유공간 = 채워진 영역 - 원본 점유 픽셀
        interior = ((filled == 1) & (occ_raw == 0)).astype(np.uint8)
        return interior

    # =========================================================================
    # 2. 직사각형 피팅 — minAreaRect로 4꼭짓점 특정
    # =========================================================================
    def _fit_rectangle(self) -> np.ndarray:
        """
        interior_free 외부 컨투어에 최소 면적 회전 직사각형 피팅.
        건물이 기울어져 있어도 정확한 외곽 파악.

        Returns: corners (4, 2) 픽셀 좌표 float array
                 순서: boxPoints 기준 (반시계 방향 보장 안 됨 → 정렬 필요)
        """
        contours, _ = cv2.findContours(
            self.interior_free, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not contours:
            raise RuntimeError("내부 자유공간 컨투어 없음 — PGM/파라미터 확인")
        contour = max(contours, key=cv2.contourArea)

        rect    = cv2.minAreaRect(contour)
        corners = cv2.boxPoints(rect).astype(np.float32)  # (4,2)

        # 꼭짓점을 반시계 방향으로 정렬 (변 순서 일관성 확보)
        corners = self._order_corners_ccw(corners)
        self.rect_corners = corners.copy()
        return corners

    @staticmethod
    def _order_corners_ccw(pts: np.ndarray) -> np.ndarray:
        """4개 꼭짓점을 무게중심 기준 반시계 방향으로 정렬."""
        cx, cy = pts.mean(axis=0)
        angles = np.arctan2(pts[:, 1] - cy, pts[:, 0] - cx)
        order  = np.argsort(angles)           # 각도 오름차순 = CCW
        return pts[order]

    # =========================================================================
    # 3. 직사각형 4변 샘플링 + 내향 법선 계산
    # =========================================================================
    def _sample_rect_wall_points(self, corners: np.ndarray) -> list:
        """
        직사각형 4변을 wall_sample_m 간격으로 샘플링.
        각 포인트에 건물 내부 방향 단위 법선 포함.

        Returns: [(px, py, nx, ny, side_idx), ...]  픽셀 좌표 + 법선 + 변 번호
        """
        interval_px = max(1.0, self.wall_sample_m / self.res)
        center      = corners.mean(axis=0)
        sampled     = []
        n           = len(corners)  # 4

        for i in range(n):
            p0 = corners[i]
            p1 = corners[(i + 1) % n]

            seg_len = math.hypot(p1[0]-p0[0], p1[1]-p0[1])
            if seg_len < 1e-6:
                continue

            # 변의 단위 탄젠트
            tx = (p1[0]-p0[0]) / seg_len
            ty = (p1[1]-p0[1]) / seg_len

            # 두 후보 법선 (좌/우)
            n1x, n1y = -ty,  tx
            n2x, n2y =  ty, -tx

            # 직사각형 중심을 향하는 쪽 = 내향 법선
            mid_x = (p0[0]+p1[0]) / 2.0
            mid_y = (p0[1]+p1[1]) / 2.0
            to_cx = center[0] - mid_x
            to_cy = center[1] - mid_y
            if n1x*to_cx + n1y*to_cy >= 0:
                nx, ny = n1x, n1y
            else:
                nx, ny = n2x, n2y

            # 변을 따라 샘플링
            n_samples = max(1, int(seg_len / interval_px))
            for j in range(n_samples):
                t  = j / n_samples
                px = p0[0] + t * (p1[0]-p0[0])
                py = p0[1] + t * (p1[1]-p0[1])
                sampled.append((float(px), float(py), nx, ny, i))

        return sampled

    # =========================================================================
    # 4. 촬영 후보 탐색 (단일 벽 포인트)
    # =========================================================================
    def _find_scan_position(
        self,
        wall_px: float, wall_py: float,
        nx: float, ny: float,
        side_idx: int,
    ) -> Optional[ScanPose]:
        """
        직사각형 변의 벽 포인트에서 유효한 촬영 위치 탐색.
        조건: interior_free 내부 + 로봇 반경 여유 + 시선 확보
        """
        robot_r_px = self.robot_radius_m / self.res

        for angle_deg in SCAN_ANGLES_DEG:
            if abs(angle_deg) > math.degrees(self.max_view_rad):
                continue

            a = math.radians(angle_deg)
            ca, sa = math.cos(a), math.sin(a)
            dx = ca * nx - sa * ny
            dy = sa * nx + ca * ny

            standoff_range = np.arange(
                self.max_standoff_m, self.min_standoff_m - 0.01, -0.05)
            for standoff_m in standoff_range:
                sp = standoff_m / self.res
                cx = int(round(wall_px + dx * sp))
                cy = int(round(wall_py + dy * sp))

                if not (1 <= cx < self.w-1 and 1 <= cy < self.h-1):
                    continue
                # 건물 내부 자유공간 확인 (맵 밖 / 외부 공간 제외)
                if self.interior_free[cy, cx] == 0:
                    continue
                # 로봇 반경 여유 (장애물에서 충분히 떨어져야 함)
                if self.dist[cy, cx] < robot_r_px:
                    continue
                # 시선 확보
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
                    wall_side     = side_idx,
                )

        return None

    # =========================================================================
    # 5. Bresenham 시선 체크
    # =========================================================================
    def _line_of_sight(self, x0: int, y0: int, x1: int, y1: int) -> bool:
        """두 픽셀 사이 장애물(occ==1) 없으면 True."""
        dx = abs(x1-x0); dy = abs(y1-y0)
        sx = 1 if x1 > x0 else -1
        sy = 1 if y1 > y0 else -1
        err = dx - dy
        x, y = x0, y0

        while True:
            if x == x1 and y == y1:
                return True
            if not (0 <= x < self.w and 0 <= y < self.h):
                return False
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
        pose 에서 볼 수 있는 wall_pts 인덱스 집합.
        조건: ① 거리 ≤ max_standoff  ② FOV 내  ③ 사선각 ≤ max_view_angle  ④ LOS
        """
        covered = set()
        px = (pose.world_x - self.ox) / self.res
        py = (pose.world_y - self.oy) / self.res
        ipx, ipy = int(round(px)), int(round(py))

        for i, (wx, wy, nx, ny, _side) in enumerate(wall_pts):
            ddx  = wx - px
            ddy  = wy - py
            dist = math.hypot(ddx, ddy)
            if dist < 1e-3:
                covered.add(i); continue

            if dist * self.res > self.max_standoff_m:
                continue

            angle_to_pt = math.atan2(ddy, ddx)
            diff = abs(math.atan2(
                math.sin(angle_to_pt - pose.yaw_rad),
                math.cos(angle_to_pt - pose.yaw_rad)
            ))
            if diff > self.half_fov_rad:
                continue

            view_dx = -ddx / dist
            view_dy = -ddy / dist
            dot = max(-1.0, min(1.0, nx * view_dx + ny * view_dy))
            if math.acos(dot) > self.max_view_rad:
                continue

            if not self._line_of_sight(ipx, ipy, int(wx), int(wy)):
                continue

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
        전체 파이프라인:
          맵 보정 → 직사각형 피팅 → 4변 샘플링 → 촬영 후보 → 그리디 커버 → 정렬

        Returns: [ScanPose, ...]
        """
        if verbose:
            px = int(self.interior_free.sum())
            print(f"[1/5] 맵 로드 완료: {self.w}×{self.h}  "
                  f"내부 자유공간 {px}px ({px*self.res**2:.2f}㎡)")

        # 직사각형 피팅
        corners  = self._fit_rectangle()
        if verbose:
            wc = [(cx*self.res+self.ox, cy*self.res+self.oy)
                  for cx, cy in corners]
            print(f"[2/5] 직사각형 꼭짓점 (월드 좌표):")
            for k, (wx, wy) in enumerate(wc):
                print(f"       V{k}: ({wx:.3f}, {wy:.3f})")

        # 4변 샘플링
        wall_pts = self._sample_rect_wall_points(corners)
        n_pts    = len(wall_pts)
        if verbose:
            print(f"[3/5] 직사각형 벽 포인트: {n_pts}개 ({self.wall_sample_m}m 간격)")

        # 촬영 후보 탐색 (중복 위치 제거)
        pose_map: dict = {}
        skipped = 0
        for wx, wy, nx, ny, side in wall_pts:
            pose = self._find_scan_position(wx, wy, nx, ny, side)
            if pose is None:
                skipped += 1
                continue
            key = (round(pose.world_x, 2), round(pose.world_y, 2))
            if key not in pose_map:
                pose_map[key] = pose

        candidates = list(pose_map.values())
        if verbose:
            print(f"[4/5] 촬영 후보: {len(candidates)}개  "
                  f"(커버 불가 포인트: {skipped}개)")

        if not candidates:
            raise RuntimeError("유효한 촬영 위치 없음 — 맵 또는 파라미터 확인")

        # 커버리지 계산 + 그리디 선택
        for pose in candidates:
            pose.covers = self._compute_coverage(pose, wall_pts)

        selected = self._greedy_coverage(candidates, n_pts)
        if verbose:
            covered = len(set().union(*[p.covers for p in selected]))
            side_counts = {}
            for p in selected:
                side_counts[p.wall_side] = side_counts.get(p.wall_side, 0) + 1
            print(f"[5/5] 그리디 선택: {len(selected)}개  "
                  f"({covered}/{n_pts} 커버)")
            for s, cnt in sorted(side_counts.items()):
                print(f"       변 {s}: {cnt}개 촬영")

        result = self._sort_nearest_neighbor(selected)
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
        side_label = {0: "북", 1: "서", 2: "남", 3: "동"}
        for i, p in enumerate(poses):
            diag  = f" (사선 {p.angle_to_wall}°)" if p.angle_to_wall > 0 else ""
            side  = side_label.get(p.wall_side, str(p.wall_side))
            lines.append(
                f"  [{i+1:02d}] 변{p.wall_side}({side})  "
                f"x={p.world_x:7.3f}  y={p.world_y:7.3f}  "
                f"yaw={p.yaw_deg:6.1f}°  dist={p.standoff_m:.2f}m{diag}"
            )
        return "\n".join(lines)


# ── 편의 함수 ──────────────────────────────────────────────────────────────────
def generate_scan_positions(
    pgm_path:       str,
    yaml_path:      str,
    start_world_xy: tuple = (0.0, 0.0),
    **kwargs,
) -> list:
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
    print()
    print(planner.summary(poses))
