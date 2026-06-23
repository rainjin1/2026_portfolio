"""
map_coord_utils.py — 맵 좌표계 관리 클래스 (v2)
==================================================
[v1 → v2 변경 이유]
  v1의 파일 기반(map.yaml 직접 읽기) 방식은 두 가지 근본 문제가 있음:
    1. SLAM 탐색 중에는 map.yaml이 아직 존재하지 않음
    2. 이전 세션에서 저장된 map.yaml이 현재 SLAM 세션과
       원점(origin)이 다를 수 있음 → 좌표 계산 오류

  v2는 /map 토픽(OccupancyGrid)을 primary source로 사용:
    - SLAM이 실시간으로 publish하는 라이브 데이터
    - info.origin = 현재 세션의 맵 좌하단 좌표 (항상 최신)
    - map.yaml은 선택적 교차검증 용도로만 사용

[좌표계 설명]
  /map 프레임 (월드 좌표, SLAM이 관리):
    - SLAM 시작 시 로봇 위치가 보통 (0,0) 근처
    - map.yaml의 origin = 이 프레임에서 맵 좌하단의 위치
    - TF2가 map→base_link 변환을 실시간 제공

  맵 기준 좌표 (map bottom-left 기준, 이 시스템이 저장하는 값):
    - 맵 좌하단 = (0, 0)
    - 맵 우상단 = (width_m, height_m)
    - 변환: map_x = world_x - origin_x
             map_y = world_y - origin_y

  예시:
    맵 origin = (-3.0, -2.5)   # /map 토픽에서 읽음
    터틀봇 초기 월드 좌표 = (-1.0, 0.5)  # TF2에서 읽음
    터틀봇 맵 기준 좌표 = (-1.0 - (-3.0), 0.5 - (-2.5)) = (2.0, 3.0)
"""

import math
import yaml
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MapBounds:
    """맵 경계 정보."""
    min_x: float = 0.0
    min_y: float = 0.0
    max_x: float = 0.0
    max_y: float = 0.0

    def contains(self, x: float, y: float, margin: float = 0.0) -> bool:
        return (self.min_x - margin <= x <= self.max_x + margin and
                self.min_y - margin <= y <= self.max_y + margin)

    def __str__(self):
        return (f"X:[{self.min_x:.3f} ~ {self.max_x:.3f}m]  "
                f"Y:[{self.min_y:.3f} ~ {self.max_y:.3f}m]  "
                f"({self.max_x - self.min_x:.2f} x {self.max_y - self.min_y:.2f}m)")


class MapCoordSystem:
    """
    /map 토픽(OccupancyGrid)으로부터 맵 메타데이터를 관리하고
    좌표 변환을 제공하는 클래스.

    사용법:
        coord = MapCoordSystem()
        # /map 콜백에서:
        coord.update_from_occupancy_grid(map_msg)
        # QR 감지 시:
        mx, my = coord.world_to_map_bl(robot_world_x, robot_world_y)
    """

    def __init__(self):
        # /map 토픽에서 읽은 라이브 데이터
        self._origin_x:   Optional[float] = None  # 맵 좌하단의 /map 프레임 X (미터)
        self._origin_y:   Optional[float] = None  # 맵 좌하단의 /map 프레임 Y (미터)
        self._resolution: Optional[float] = None  # 미터/셀
        self._width_cells:  int = 0
        self._height_cells: int = 0
        self.initialized: bool = False

        # 로봇 시작 위치 (노드 준비 완료 시 1회 기록)
        self.robot_start_world_x: Optional[float] = None
        self.robot_start_world_y: Optional[float] = None
        self.robot_start_map_x:   Optional[float] = None  # 맵 기준 좌표
        self.robot_start_map_y:   Optional[float] = None

    # ── /map 토픽 갱신 ────────────────────────────────────────────────────────
    def update_from_occupancy_grid(self, map_msg) -> None:
        """
        OccupancyGrid 메시지로 맵 메타데이터 갱신.
        SLAM 중에는 맵이 커질 수 있으므로 매번 갱신 필요.
        """
        self._origin_x     = map_msg.info.origin.position.x
        self._origin_y     = map_msg.info.origin.position.y
        self._resolution   = map_msg.info.resolution
        self._width_cells  = map_msg.info.width
        self._height_cells = map_msg.info.height
        self.initialized   = True

    # ── 좌표 변환 ─────────────────────────────────────────────────────────────
    def world_to_map_bl(self, world_x: float, world_y: float) -> tuple[float, float]:
        """
        /map 프레임 좌표 → 맵 좌하단(0,0) 기준 좌표.

        Args:
            world_x, world_y : TF2로 얻은 /map 프레임 좌표 (미터)
        Returns:
            (map_x, map_y)   : 맵 좌하단이 원점인 좌표 (미터)
        Raises:
            RuntimeError: 맵 데이터 미수신 시
        """
        self._require_init()
        return (world_x - self._origin_x), (world_y - self._origin_y)

    def map_bl_to_world(self, map_x: float, map_y: float) -> tuple[float, float]:
        """맵 기준 좌표 → /map 프레임 좌표 역변환 (내비게이션 목표 설정용)."""
        self._require_init()
        return (map_x + self._origin_x), (map_y + self._origin_y)

    # ── 범위 검증 ─────────────────────────────────────────────────────────────
    def get_bounds(self) -> MapBounds:
        """맵 기준 좌표계의 유효 범위 반환."""
        self._require_init()
        return MapBounds(
            min_x=0.0,
            min_y=0.0,
            max_x=self._width_cells  * self._resolution,
            max_y=self._height_cells * self._resolution,
        )

    def is_within_bounds(
        self, map_x: float, map_y: float, margin: float = 0.15
    ) -> bool:
        """
        맵 기준 좌표가 맵 범위 내에 있는지 확인.

        Args:
            margin: 경계 여유값 (미터). 0.15m = 15cm 여유.
                    SLAM 중 맵이 조금씩 커지므로 여유를 둠.
        """
        if not self.initialized:
            return False
        bounds = self.get_bounds()
        return bounds.contains(map_x, map_y, margin)

    # ── 로봇 시작 위치 기록 ───────────────────────────────────────────────────
    def record_robot_start(self, world_x: float, world_y: float) -> dict:
        """
        로봇의 초기 위치를 월드 좌표와 맵 기준 좌표 모두 기록.
        노드 준비 완료(READY) 시 1회 호출.

        Returns:
            dict: 기록된 시작 위치 정보
        """
        self._require_init()
        self.robot_start_world_x = world_x
        self.robot_start_world_y = world_y
        self.robot_start_map_x, self.robot_start_map_y = self.world_to_map_bl(
            world_x, world_y
        )

        in_bounds = self.is_within_bounds(
            self.robot_start_map_x, self.robot_start_map_y
        )

        return {
            'world':    (world_x, world_y),
            'map_bl':   (self.robot_start_map_x, self.robot_start_map_y),
            'in_bounds': in_bounds,
            'map_origin': (self._origin_x, self._origin_y),
        }

    # ── map.yaml 교차검증 (선택) ──────────────────────────────────────────────
    def cross_check_yaml(
        self, yaml_path: str, tolerance: float = 0.02
    ) -> tuple[bool, str]:
        """
        /map 토픽 데이터와 map.yaml 파일의 일관성 검사.
        두 값이 다르면 다른 세션의 맵이 혼용되고 있다는 신호.

        Args:
            yaml_path  : map.yaml 파일 경로
            tolerance  : 원점 허용 오차 (미터, 기본 2cm)
        Returns:
            (True, 설명 문자열) or (False, 불일치 내용)
        """
        if not self.initialized:
            return False, "MapCoordSystem 미초기화 — /map 토픽 대기 필요"

        if not os.path.exists(yaml_path):
            return False, f"map.yaml 없음: {yaml_path}"

        try:
            with open(yaml_path, 'r') as f:
                data = yaml.safe_load(f)
        except Exception as e:
            return False, f"map.yaml 읽기 실패: {e}"

        yaml_origin = data.get('origin', [0.0, 0.0, 0.0])
        yaml_ox  = float(yaml_origin[0])
        yaml_oy  = float(yaml_origin[1])
        yaml_res = float(data.get('resolution', 0.05))

        ox_diff  = abs(self._origin_x - yaml_ox)
        oy_diff  = abs(self._origin_y - yaml_oy)
        res_diff = abs(self._resolution - yaml_res)

        errors = []
        if ox_diff > tolerance:
            errors.append(
                f"origin.x 불일치: /map={self._origin_x:.4f} yaml={yaml_ox:.4f} "
                f"(차이={ox_diff:.4f}m > 허용{tolerance}m)"
            )
        if oy_diff > tolerance:
            errors.append(
                f"origin.y 불일치: /map={self._origin_y:.4f} yaml={yaml_oy:.4f} "
                f"(차이={oy_diff:.4f}m > 허용{tolerance}m)"
            )
        if res_diff > 0.001:
            errors.append(
                f"resolution 불일치: /map={self._resolution} yaml={yaml_res}"
            )

        if errors:
            return False, "\n  ".join(["[교차검증 FAIL]"] + errors +
                          ["→ 현재 SLAM 세션과 다른 맵의 yaml일 가능성 높음.",
                           "  /map 토픽 데이터를 우선 사용합니다."])

        return True, (
            f"[교차검증 OK] origin=({self._origin_x:.4f},{self._origin_y:.4f}) "
            f"resolution={self._resolution} — /map과 map.yaml 일치"
        )

    # ── 정보 출력 ─────────────────────────────────────────────────────────────
    def summary(self) -> str:
        if not self.initialized:
            return "MapCoordSystem: [미초기화 — /map 토픽 대기 중]"
        bounds = self.get_bounds()
        lines = [
            "── MapCoordSystem 상태 ──────────────────────────────",
            f"  맵 좌하단 원점 (월드): ({self._origin_x:.4f}, {self._origin_y:.4f}) m",
            f"  맵 크기: {bounds.max_x:.2f} x {bounds.max_y:.2f} m",
            f"  해상도: {self._resolution} m/cell  "
            f"({self._width_cells} x {self._height_cells} cells)",
        ]
        if self.robot_start_map_x is not None:
            lines += [
                f"  로봇 시작 (월드):   ({self.robot_start_world_x:.4f}, "
                f"{self.robot_start_world_y:.4f}) m",
                f"  로봇 시작 (맵기준): ({self.robot_start_map_x:.4f}, "
                f"{self.robot_start_map_y:.4f}) m",
            ]
        lines.append("────────────────────────────────────────────────────")
        return "\n".join(lines)

    # ── 내부 헬퍼 ─────────────────────────────────────────────────────────────
    def _require_init(self):
        if not self.initialized:
            raise RuntimeError(
                "MapCoordSystem 미초기화. "
                "/map 토픽(OccupancyGrid)이 수신되어야 합니다. "
                "SLAM 또는 map_server가 실행 중인지 확인하세요."
            )


# ── 독립 실행 테스트 ──────────────────────────────────────────────────────────
if __name__ == '__main__':
    import sys

    # 간단한 mock OccupancyGrid 구조체로 테스트
    class MockInfo:
        class origin:
            class position:
                x = -3.0
                y = -2.5
        resolution = 0.05
        width  = 200   # 200 * 0.05 = 10m
        height = 160   # 160 * 0.05 = 8m

    class MockMapMsg:
        info = MockInfo()

    coord = MapCoordSystem()
    coord.update_from_occupancy_grid(MockMapMsg())

    print(coord.summary())

    # 터틀봇 임의 시작 위치 테스트
    test_cases = [
        (-1.0,  0.5, "맵 안쪽"),
        ( 0.0,  0.0, "SLAM 원점 (맵 안)"),
        (-3.0, -2.5, "맵 좌하단"),
        ( 7.0,  5.5, "맵 우상단 근처"),
        (-5.0,  0.0, "맵 밖 (X 음수)"),
    ]

    print("\n월드 좌표 → 맵 기준 좌표 변환 테스트:")
    for wx, wy, label in test_cases:
        mx, my = coord.world_to_map_bl(wx, wy)
        ok = coord.is_within_bounds(mx, my)
        print(f"  [{label:20s}] world=({wx:6.2f},{wy:6.2f}) "
              f"→ map_bl=({mx:6.2f},{my:6.2f})  {'[OK]' if ok else '[범위밖!]'}")

    print()
    result = coord.record_robot_start(-1.0, 0.5)
    print(f"로봇 시작 위치 기록: {result}")

    if len(sys.argv) > 1:
        ok, msg = coord.cross_check_yaml(sys.argv[1])
        print(f"\nmap.yaml 교차검증: {msg}")
