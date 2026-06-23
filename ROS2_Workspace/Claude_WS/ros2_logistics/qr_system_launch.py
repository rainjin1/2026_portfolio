"""
qr_system_launch.py — 전체 QR 물류 파이프라인 실행 가이드
==========================================================

[전체 아키텍처]

  ┌──────────────────────────────────────────────────────────┐
  │                   TurtleBot3 (Ubuntu VM)                  │
  │                                                          │
  │  [1단계] slam_toolbox  ──► /map 토픽 생성               │
  │      +  Auto_Mapping.txt (우수법 자율 탐색)              │
  │                │                                         │
  │  [2단계] qr_scanner_node.py                             │
  │      - /image_raw 구독 (카메라)                         │
  │      - /tf 구독 (TF2 맵 좌표)                           │
  │      - pyzbar QR 감지                                   │
  │      - 맵 좌하단(0,0) 기준 좌표 변환                    │
  │      - /qr/capture_image 퍼블리시 (압축 이미지)         │
  │      - /qr/metadata 퍼블리시 (JSON)                     │
  │      - scan_positions.yaml 로컬 저장                    │
  └───────────────────┬──────────────────────────────────────┘
                      │ ROS2 토픽 (같은 ROS_DOMAIN_ID)
                      ▼
  ┌──────────────────────────────────────────────────────────┐
  │                    원격 PC (Remote PC)                    │
  │                                                          │
  │  [3단계] qr_database_node.py                            │
  │      - /qr/capture_image 구독                           │
  │      - /qr/metadata 구독                                │
  │      - pyzbar 재검증                                    │
  │      - SQLite qr.db 저장                                │
  │        (id, qr_data, x, y, timestamp, image_path)       │
  └──────────────────────────────────────────────────────────┘

══════════════════════════════════════════════════════════════
[STEP 0] 의존성 설치
══════════════════════════════════════════════════════════════

TurtleBot3 및 원격 PC 공통:
  sudo apt install ros-humble-cv-bridge ros-humble-tf2-ros \
                   ros-humble-tf2-geometry-msgs libzbar0
  pip install pyzbar pillow --break-system-packages

TurtleBot3 추가 (카메라 드라이버):
  sudo apt install ros-humble-v4l2-camera   # USB 웹캠 사용 시
  # 또는 Raspberry Pi Camera 사용 시 별도 드라이버

══════════════════════════════════════════════════════════════
[STEP 1] 터미널 1 — SLAM 맵 빌드 (TurtleBot3)
══════════════════════════════════════════════════════════════

  export TURTLEBOT3_MODEL=burger
  ros2 launch turtlebot3_cartographer cartographer.launch.py

  # 또는 slam_toolbox 사용:
  ros2 launch slam_toolbox online_async_launch.py

══════════════════════════════════════════════════════════════
[STEP 2] 터미널 2 — 카메라 노드 실행 (TurtleBot3)
══════════════════════════════════════════════════════════════

  # USB 웹캠
  ros2 run v4l2_camera v4l2_camera_node --ros-args \
    -p video_device:=/dev/video0 \
    -p image_size:=[640,480]

  # 토픽 확인
  ros2 topic list | grep image

══════════════════════════════════════════════════════════════
[STEP 3] 터미널 3 — 자율 탐색 (TurtleBot3)
══════════════════════════════════════════════════════════════

  ros2 run <패키지명> auto_mapping   # Auto_Mapping.txt 실행

══════════════════════════════════════════════════════════════
[STEP 4] 터미널 4 — QR 스캐너 노드 (TurtleBot3)
══════════════════════════════════════════════════════════════

  ros2 run <패키지명> qr_scanner_node --ros-args \
    -p map_yaml_path:=/home/ubuntu/maps/map.yaml \
    -p save_dir:=/home/ubuntu/qr_scans \
    -p scan_cooldown_sec:=3.0

══════════════════════════════════════════════════════════════
[STEP 5] 맵 저장 (탐색 완료 후)
══════════════════════════════════════════════════════════════

  ros2 run nav2_map_server map_saver_cli -f ~/maps/map

  # map.yaml, map.pgm 생성됨
  # map.yaml의 origin 값이 좌표 변환 기준점

══════════════════════════════════════════════════════════════
[STEP 6] 터미널 5 — DB 노드 (원격 PC)
══════════════════════════════════════════════════════════════

  # 원격 PC: 동일 ROS_DOMAIN_ID 필수
  export ROS_DOMAIN_ID=<로봇과_동일한_번호>

  # ROS2 네트워크 설정 (DDS 멀티캐스트 또는 unicast)
  # /etc/hosts 또는 RMW_IMPLEMENTATION 확인

  ros2 run <패키지명> qr_database_node --ros-args \
    -p db_path:=/home/user/qr_data/qr.db \
    -p image_dir:=/home/user/qr_data/images

══════════════════════════════════════════════════════════════
[DB 조회 예시] — SQLite3 CLI
══════════════════════════════════════════════════════════════

  sqlite3 /home/user/qr_data/qr.db

  -- 전체 조회
  SELECT * FROM qr_scans;

  -- 특정 QR 코드 위치 조회
  SELECT qr_data, x, y FROM qr_scans WHERE qr_data LIKE '%ZONE%';

  -- 최근 스캔 5개
  SELECT * FROM qr_scans ORDER BY id DESC LIMIT 5;

══════════════════════════════════════════════════════════════
[좌표계 설명]
══════════════════════════════════════════════════════════════

  맵 파일(map.pgm)의 픽셀 좌표:
    - 좌하단 = (pixel 0, pixel height)
    - ROS map.yaml의 origin = 이 픽셀의 월드 좌표

  SLAM 월드 좌표계:
    - 터틀봇 시작 위치 = (0, 0)  ← ROS 기본
    - map.yaml origin은 보통 음수값 (예: [-2.72, -2.00])

  이 시스템의 맵 기준 좌표:
    - 맵 좌하단 = (0, 0)
    - 터틀봇 시작점 = (|origin_x|, |origin_y|) 예: (2.72, 2.00)
    - 변환: map_x = world_x - origin_x
             map_y = world_y - origin_y

══════════════════════════════════════════════════════════════
[파일 구성 요약]
══════════════════════════════════════════════════════════════

  Auto_Mapping.txt      → [1단계] 우수법 자율 탐색 (기존)
  map_coord_utils.py    → [공용] 좌표 변환 유틸
  qr_scanner_node.py    → [2단계] 로봇측 QR 감지 + 전송
  qr_database_node.py   → [3단계] 원격 PC DB 저장
  qr_system_launch.py   → 실행 순서 가이드 (이 파일)

══════════════════════════════════════════════════════════════
[ROS2 패키지 등록 예시 (setup.py)]
══════════════════════════════════════════════════════════════

  entry_points={
      'console_scripts': [
          'auto_mapping      = <pkg>.auto_mapping:main',
          'qr_scanner_node   = <pkg>.qr_scanner_node:main',
          'qr_database_node  = <pkg>.qr_database_node:main',
      ],
  },
"""

# 이 파일은 실행 가이드 + 문서입니다.
# ROS2 launch 파일로 변환하려면 아래 코드 사용:

from launch import LaunchDescription
from launch_ros.actions import Node
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    """
    TurtleBot3 측 노드만 포함 (qr_database_node는 원격 PC에서 별도 실행).
    ros2 launch <패키지명> qr_system_launch.py map_yaml:=/path/to/map.yaml
    """
    map_yaml_arg = DeclareLaunchArgument(
        'map_yaml',
        default_value='',
        description='SLAM으로 저장한 map.yaml 경로'
    )
    save_dir_arg = DeclareLaunchArgument(
        'save_dir',
        default_value='/tmp/qr_scans',
        description='QR 스캔 결과 저장 디렉토리'
    )

    # [1단계] 자율 탐색 노드
    auto_mapping_node = Node(
        package='<패키지명>',           # TODO: 실제 패키지명으로 교체
        executable='auto_mapping',
        name='auto_mapping',
        output='screen',
    )

    # [2단계] QR 스캐너 노드
    qr_scanner_node = Node(
        package='<패키지명>',           # TODO: 실제 패키지명으로 교체
        executable='qr_scanner_node',
        name='qr_scanner_node',
        output='screen',
        parameters=[{
            'map_yaml_path': LaunchConfiguration('map_yaml'),
            'save_dir':      LaunchConfiguration('save_dir'),
            'scan_cooldown_sec': 3.0,
        }]
    )

    return LaunchDescription([
        map_yaml_arg,
        save_dir_arg,
        auto_mapping_node,
        qr_scanner_node,
    ])
