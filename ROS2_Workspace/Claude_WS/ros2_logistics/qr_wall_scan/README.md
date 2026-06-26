# ROS2 물류센터 QR 스캔 자율주행 시스템
**Autonomous QR Scanning Robot for Logistics Warehouse — ROS2 Humble / TurtleBot3**

---

## 프로젝트 개요 · Overview

TurtleBot3 Burger가 물류창고 외벽을 자율 순회하며 QR 코드를 촬영·인식하고, 각 QR의 월드 좌표와 재촬영 최적 접근 포즈를 SQLite DB에 저장하는 시스템.

> A TurtleBot3 Burger autonomously patrols warehouse walls, captures QR codes, computes their world coordinates via camera geometry, and stores results with optimal re-approach poses in SQLite.

**개발 환경**
- ROS2 Humble · Ubuntu 22.04 (VMware)
- TurtleBot3 Burger (LiDAR: LDS-01, Camera: Raspberry Pi Cam v2)
- SLAM Toolbox + AMCL localization

---

## 시스템 아키텍처 · Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    TurtleBot3                           │
│                                                         │
│  [SLAM Toolbox]  →  /map  →  WallCoveragePlanner        │
│                              ↓ ScanPose 목록 (50개)     │
│  [AMCL]  →  /amcl_pose  →  qr_snapshot_node  (★ 메인) │
│  [LiDAR] →  /scan        →      ↓                      │
│  [Camera]→  /camera/...  →   cmd_vel P-controller       │
│                              ↓ /qr/scan_complete        │
└─────────────────────────────────────────────────────────┘
                        │  (ROS2 topic, same domain_id)
┌─────────────────────────────────────────────────────────┐
│                    Remote PC                            │
│                                                         │
│  qr_database_node  →  snapshots/ 이미지 배치 처리       │
│                    →  pyzbar 재감지 + 좌표 역산          │
│                    →  SQLite qr.db 저장                 │
│                                                         │
│  qr_db_crosscheck_node  →  /qr/metadata 수신           │
│                          →  WMS DB 재고 교차검증         │
└─────────────────────────────────────────────────────────┘
```

### 파일 구성 · File Structure

```
qr_wall_scan/
├── qr_wall_scan/
│   ├── wall_coverage_planner.py   # 외벽 촬영 위치 자동 생성
│   ├── map_coord_utils.py         # 좌표계 변환 유틸
│   ├── qr_snapshot_node.py        # 메인 자율주행 + 촬영 노드 (TurtleBot3)
│   ├── qr_database_node.py        # QR 좌표 역산 + DB 저장 (Remote PC)
│   └── qr_db_crosscheck_node.py   # WMS 재고 교차검증 (Remote PC)
├── config/
│   └── scan_poses_0622.yaml       # 사전 계산된 촬영 위치 (선택)
├── setup.py
└── package.xml
```

---

## 핵심 기술 구현 · Key Technical Implementations

### 1. 외벽 커버리지 플래너 (`wall_coverage_planner.py`)

Nav2 global planner 없이 맵 이미지(PGM)를 직접 분석하여 최소 촬영 횟수로 전체 외벽을 커버하는 촬영 위치를 자동 생성.

**알고리즘 파이프라인:**

```
PGM 로드 (Y축 flip)
    ↓
자유공간(254) 최대 연결 성분 추출  →  interior_free
    ↓
RETR_EXTERNAL 컨투어  →  내부 장애물(선반 등) 제외, 순수 외벽만
    ↓
외곽 선분 8cm 간격 샘플링 + inward normal 계산
    ↓
각 벽 포인트별 촬영 후보 탐색
  - standoff: 0.80m ~ 0.30m (5cm씩 감소)
  - 사선 각도: [0°, ±15°, ±25°, ±30°]
  - 검증: 이동가능공간 + 로봇반경 거리변환 + Bresenham LOS
    ↓
FOV 커버리지 계산 (62.2° 화각 기준)
    ↓
Greedy Set Cover  →  최소 촬영 횟수 선택
    ↓
최근접 이웃 정렬  →  이동 거리 최소화
```

**핵심 설계 포인트:**
- `RETR_EXTERNAL` 컨투어로 내부 장애물의 벽 자동 제외
- erode 2px로 컨투어가 벽 픽셀 위에 올라가는 문제 해결 (오류율 21% → 0.4%)
- dist_transform 기반 inward normal: 자유공간 쪽이 항상 값이 크다는 특성 이용

### 2. cmd_vel P-Controller (`qr_snapshot_node.py`)

Nav2가 불안정한 좁은 공간에서 직접 `cmd_vel`을 퍼블리시하는 3단계 P-컨트롤러.

```
Phase 1: |yaw_error| > 0.5 rad  →  제자리 회전 (전진 없음)
Phase 2: 전진 + 조향 보정       →  lx = min(0.12, 0.5×dist)
                                    az = clamp(1.2×err, ±0.8)
Phase 3: dist < 0.12 m          →  최종 yaw 정렬
```

**장애물 대응 로직:**
- 목표까지 0.4m 이내: 장애물 무시 (벽 자체가 목적지)
- 0.4m 초과에서 3초 이상 차단: 후퇴(0.2m) → 재시도
- 3초 안전 타이머: 전방위 최솟값 < 0.13m → 즉시 정지

### 3. QR 월드 좌표 역산

카메라 핀홀 모델로 픽셀 위치 → 월드 좌표 변환:

```python
focal_len_px = (640 / 2) / tan(radians(62.2 / 2))   # ≈ 554 px

alpha     = atan2(pixel_x - 320, focal_len_px)        # 수평 각도 오프셋
direction = robot_yaw + alpha                          # 절대 방향
qr_x      = robot_x + standoff_m × cos(direction)
qr_y      = robot_y + standoff_m × sin(direction)
```

재촬영 접근 포즈: QR 좌표에서 벽 법선 방향으로 0.30m 오프셋.

### 4. 좌표계 설계 (`map_coord_utils.py`)

SLAM 중 map.yaml이 없거나 이전 세션의 origin과 다를 수 있어 `/map` 토픽(OccupancyGrid)을 primary source로 사용.

```
/map 프레임 (world)  →  map_bl 좌표 (맵 좌하단 기준)
  변환: map_x = world_x − origin_x
  역변환: world_x = map_x + origin_x
```

map.yaml 교차검증 기능으로 세션 불일치 감지.

### 5. 카메라 동적 ON/OFF

`camera_ros` 패키지는 lifecycle 서비스 미제공 → subscription 생성/삭제로 구현:

```python
def _camera_on(self):
    self.img_sub = self.create_subscription(...)   # 구독 생성

def _camera_off(self):
    self.destroy_subscription(self.img_sub)         # 구독 해제
    self.img_sub = None
```

상시 스트리밍 대신 도착 시 ON → 1장 캡처 → OFF로 리소스 절약.

---

## 실행 흐름 · Execution Flow

### Stage 0 — 맵 생성 (사전 완료)
```bash
# TurtleBot3
ros2 launch slam_toolbox online_async_launch.py
ros2 run qr_wall_scan qr_wall_scan_node   # 우수법 자율 탐색
ros2 run nav2_map_server map_saver_cli -f ~/map/0622_map_final
```

### Stage 1 — QR 스캔 순회 (TurtleBot3)
```bash
# AMCL 로컬라이제이션
ros2 launch nav2_bringup localization_launch.py \
  map:=/home/ubuntu22/map/0622_map_final.yaml

# 카메라
ros2 launch turtlebot3_bringup camera.launch.py format:=YUYV

# 메인 노드
ros2 run qr_wall_scan qr_snapshot_node --ros-args \
  -p map_yaml_path:=/home/ubuntu22/map/0622_map_final.yaml \
  -p poses_yaml_path:=/home/ubuntu22/map/scan_poses_0622.yaml
```

### Stage 2 — DB 저장 (Remote PC)
```bash
export ROS_DOMAIN_ID=<로봇과_동일>

ros2 run qr_wall_scan qr_database_node --ros-args \
  -p db_path:=/home/user/qr_data/qr.db
```

### DB 조회
```bash
sqlite3 /home/user/qr_data/qr.db \
  "SELECT qr_data, qr_world_x, qr_world_y, approach_x, approach_y, approach_yaw_deg FROM qr_scans;"
```

---

## 의존성 · Dependencies

```bash
# ROS2 패키지
sudo apt install \
  ros-humble-slam-toolbox \
  ros-humble-nav2-bringup \
  ros-humble-cv-bridge \
  ros-humble-tf2-ros \
  ros-humble-tf2-geometry-msgs

# 시스템
sudo apt install libzbar0

# Python
pip install pyzbar pillow --break-system-packages
```

---

## 결과물 · Output

순회 완료 후 맵 디렉터리에 생성:

```
/home/ubuntu22/map/
├── snapshots/
│   ├── snap_001_20241022_143021.jpg
│   ├── snap_002_20241022_143045.jpg
│   └── ...
├── qr_scan_results.yaml    # 구조화 데이터 (snap_records + qr_results)
└── qr_scan_results.txt     # 사람이 읽기 좋은 요약
```

SQLite `qr_scans` 테이블:

| 필드 | 설명 |
|------|------|
| `qr_data` | QR 코드 내용 |
| `qr_world_x/y` | QR 월드 좌표 (m) |
| `approach_x/y/yaw_deg` | 재촬영 최적 접근 포즈 |
| `robot_world_x/y` | 촬영 당시 로봇 위치 |
| `snap_file` | 스냅샷 파일명 |
| `confirmed` | pyzbar 재감지 완료 여부 |

---

## 개발 과정에서 해결한 문제들 · Problem Solving

| 문제 | 해결 |
|------|------|
| Nav2 smoother_server 타임아웃, 좁은 통로 우회 | Nav2 제거 → cmd_vel 3단계 P-컨트롤러 직접 구현 |
| camera_ros lifecycle 서비스 없음 | subscription create/destroy로 동적 ON/OFF |
| SLAM 중 map.yaml 미존재 / origin 불일치 | `/map` 토픽을 primary source로 사용 |
| TF2 미준비 상태에서 시작 위치 (0,0) 오류 | WAITING_TF 상태 추가, 30초 폴링 후 AMCL 보정 |
| 벽 앞 장애물 판정으로 정지 | NEAR_DIST=0.4m 이내 장애물 무시 로직 추가 |
| 내부 장애물(선반) 벽을 외벽으로 오인 | RETR_EXTERNAL 컨투어로 외벽만 추출 |

---

## 패키지 설치 · Build

```bash
# Ubuntu (TurtleBot3 워크스페이스)
cd ~/turtlebot3_ws
colcon build --packages-select qr_wall_scan
source install/setup.bash
```

---

*Jin · 2024 · ROS2 Humble · TurtleBot3 Burger*
