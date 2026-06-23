# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 환경

- **ROS2 Humble** on Ubuntu 22.04 (VMware)
- **TurtleBot3 Burger** 원격 제어 (LiDAR: LDS-01)
- 원격 PC ↔ TurtleBot3 간 ROS2 토픽 통신 (동일 `ROS_DOMAIN_ID` 필수)

## 실행 명령

```bash
# 의존성 (TurtleBot3 + 원격 PC 공통)
sudo apt install ros-humble-cv-bridge ros-humble-tf2-ros ros-humble-tf2-geometry-msgs libzbar0
pip install pyzbar pillow --break-system-packages

# [Stage 1] 자율 탐색 (TurtleBot3)
ros2 launch slam_toolbox online_async_launch.py
ros2 run <pkg> auto_mapping

# 탐색 완료 후 맵 저장
ros2 run nav2_map_server map_saver_cli -f ~/map/0622_map_final

# [Stage 2] QR 스캔 (TurtleBot3)
ros2 run v4l2_camera v4l2_camera_node --ros-args -p video_device:=/dev/video0
ros2 run <pkg> qr_scanner_node --ros-args \
  -p map_yaml_path:=/home/ubuntu22/map/0622_map_final.yaml \
  -p save_dir:=/home/ubuntu22/qr_scans

# [Stage 3] DB 저장 (원격 PC)
export ROS_DOMAIN_ID=<로봇과_동일>
ros2 run <pkg> qr_database_node --ros-args \
  -p db_path:=/home/user/qr_data/qr.db \
  -p image_dir:=/home/user/qr_data/images

# DB 조회
sqlite3 /home/user/qr_data/qr.db "SELECT * FROM qr_scans;"
```

## 아키텍처

### 3단계 파이프라인

```
Stage 1: Auto_Mapping.txt  →  SLAM 맵 생성 (완료)
Stage 2: qr_scanner_node.py  →  QR 위치 감지 + 좌표 변환 + 원격 전송
Stage 3: qr_database_node.py  →  이미지 재검증 + SQLite 저장
```

공용 유틸: `map_coord_utils.py` (`MapCoordSystem` 클래스)

---

### `Auto_Mapping.txt` — 우수법 자율 탐색 FSM

6계층 제어 구조 (위에서 아래로 우선순위):

| Layer | 역할 |
|-------|------|
| 1 | 데이터 수집 콜백 (`/scan`, `/odom`) |
| 2 | TF2 30초 위치 보정 + LiDAR 거리 가공 |
| 3 | 미션 관리 — 원점 복귀 판정 |
| 4 | EMERGENCY 안전 오버라이드 |
| 5 | FSM 상태머신 |
| 6 | `cmd_vel` 퍼블리시 |

**FSM 상태 전이:**
```
WALL_FOLLOW ──(우전방 열림)──▶ EXIT_TURN ──▶ WALL_FOLLOW
            ──(우측 열림)───▶ CORNER_TURN ──(10틱+벽감지)──▶ WALL_FOLLOW
                                          ──(30틱 타임아웃)──▶ CORNER_ESCAPE
                                                               (ROTATING → DRIVING)──▶ WALL_FOLLOW
            ──(370° 우회전 누적)──▶ ISLAND_ESCAPE ──▶ WALL_FOLLOW
전방 <20cm 감지 → EMERGENCY (어느 상태에서든 즉시 오버라이드)
```

**LiDAR 각도 규칙 (LDS-01, angle_min=0, CCW):**
- 0° = 전방, 90° = 좌측, 180° = 후방, 270° = 우측
- 전방: 315~360° + 0~15°, 우전방: 300~330°, 정우측: 240~270°, 후방: 180°(단일 인덱스)

**미션 종료 조건:** `has_left_start_zone=True`(출발지 50cm 이탈) AND `dist_from_start ≤ 0.25m`

---

### `map_coord_utils.py` — 좌표계 관리

**핵심 설계 원칙**: `map.yaml` 파일이 아닌 `/map` 토픽(OccupancyGrid)을 primary source로 사용. SLAM 중 파일은 존재하지 않고 이전 세션의 파일은 origin이 다를 수 있기 때문.

```python
coord = MapCoordSystem()
coord.update_from_occupancy_grid(map_msg)        # /map 콜백에서 호출

map_x, map_y = coord.world_to_map_bl(wx, wy)    # /map 프레임 → 맵 좌하단 기준
wx, wy       = coord.map_bl_to_world(mx, my)     # 역변환 (내비게이션 목표용)
coord.record_robot_start(wx, wy)                 # READY 전환 시 1회 호출
coord.cross_check_yaml(yaml_path)                # 선택적 교차검증
```

좌표 변환: `map_x = world_x − origin_x`, `map_y = world_y − origin_y`

---

### `qr_scanner_node.py` — QR 감지 (TurtleBot3)

**시작 동기화 FSM (순서 강제):**
```
WAITING_MAP  →  /map 토픽 수신 시 전이  (타임아웃 30초)
WAITING_TF   →  TF2 map→base_link 성공 시 전이  (타임아웃 30초)
READY        →  로봇 초기 위치 기록 후 QR 처리 활성화
```
`image_callback`은 `startup_state == READY`일 때만 동작.

**감지 흐름:** pyzbar decode → 쿨다운 체크 → TF2로 현재 포즈 획득 → `world_to_map_bl()` 변환 → 범위 검증 → `/qr/capture_image` + `/qr/metadata` 퍼블리시 → `scan_positions.yaml` 로컬 저장

---

### `qr_database_node.py` — DB 저장 (원격 PC)

- `/qr/capture_image`(CompressedImage)와 `/qr/metadata`(JSON String)를 독립 콜백으로 수신
- `timestamp` 키로 메타+이미지 매칭 (`pending_meta`, `pending_image` dict)
- pyzbar로 이미지 재디코딩 후 `confirmed` 컬럼에 기록 (0=로봇감지, 1=원격PC재확인)
- SQLite 스키마: `qr_scans(id, qr_data, x, y, timestamp, image_path, confirmed)`

---

## 좌표계 요약

| 좌표계 | 원점 | 사용처 |
|--------|------|--------|
| `/map` 프레임 (월드) | SLAM 시작 시 로봇 위치 ≈ (0,0) | TF2, AMCL |
| 맵 기준 좌표 | 맵 좌하단 = (0,0) | DB 저장, scan_positions.yaml |

현재 맵 파일: `/home/ubuntu22/map/0622_map_final.pgm` / `.yaml`

## 주요 파라미터 (Auto_Mapping)

| 파라미터 | 값 | 설명 |
|----------|-----|------|
| `WALL_TARGET_DIST` | 0.17m | 우측 벽 목표 거리 |
| `OPEN_SPACE_DIST` | 0.35m | 열린 공간 판단 임계값 |
| `MAX_LINEAR` | 0.08 m/s | 최대 전진 속도 |
| `MAX_ANGULAR` | 0.40 rad/s | 최대 회전 속도 |
| `TF_CORRECTION_INTERVAL` | 300틱 (30초) | TF2 위치 보정 주기 |
| Island 감지 임계 | 370° | 우회전 누적 각도 |
