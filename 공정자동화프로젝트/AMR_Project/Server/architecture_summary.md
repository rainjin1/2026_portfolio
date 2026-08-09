# MES Command Center — 아키텍처 요약
> ABB IRB1200 기반 모듈형 주택 벽 패널 MES 서버 (`command_center_20260730.py`)

---

## 0. 주문 도메인

### 주문 데이터 구조
고객 주문 1건 = 모듈형 주택의 특정 위치에 놓일 벽 패널 1개.

| 필드 | 내용 |
|---|---|
| 좌표 (x, y) | 탑뷰 기준 격자 좌표. 좌하단 (0,0), x 0~3, y 0~3 |
| 층수 | 1층 또는 2층. **2층은 해당 좌표에 1층이 존재해야만 주문 가능** |
| 벽 색상 | 탑뷰 기준 상·하·좌·우 각각 W(하양) / Y(노랑) / P(분홍) 독립 선택 |

### 도시 레이아웃
수평 도로 1개를 기준으로 위·아래 각각 4×2 격자 (총 4×4, 32개 유닛 위치).  
도로는 y1과 y2 사이에 위치. 좌표계 원점(0,0)은 좌하단.

### 주문 입력 채널
- **콘솔 CLI** (현재 운영): `order <x> <y> <색상4자리>` 형식으로 서버 터미널 직접 입력
- **웹 주문 폼** (구현 중): Flask 기반 GUI, 동일 네트워크 내 고객이 브라우저로 접근

### 웹 주문 화면 (`order_form.html`)

![주문 입력 화면](order_form_screenshot.png)

---

## 1. 프로젝트 개요

ABB IRB1200 로봇 2대(R1·R2), 자율주행 이동 로봇(LD-90 AMR), PLC 2대, 라즈베리파이, 아두이노 2대를 통합 제어하는 Python 기반 MES(제조 실행 시스템) 서버.  
고객 주문 → 자재 분류 → 패널 조립 → 품질 검사 → 출력 컨베이어 → AMR 수령까지 전 공정을 단일 서버에서 이벤트 기반으로 오케스트레이션.

---

## 2. 기술 스택

| 구분 | 내용 |
|---|---|
| 언어 | Python 3.11 |
| 통신 | TCP/IP 소켓 (서버: 192.168.3.8:9090) |
| DB | SQLite (mes.db, `db.py` 모듈) |
| 동시성 | `threading` + `queue.Queue` (멀티스레드) |
| AMR 제어 | ARCL 프로토콜 (192.168.3.11:7171) |
| PLC 통신 | 고정 프레임 20바이트 (ASCII, CR/LF 없음) |

---

## 3. 연결 장비 및 역할

| 장비 ID | 모델명 | 역할 |
|---|---|---|
| R1 | ABB IRB1200 | 자재 색상 분류, 조립 스택 적재 |
| R2 | ABB IRB1200 | 패널 조립, 판별대 이송, 출력/폐기 |
| PLC1 | 미쓰비시 Q시리즈 UDV | R1 입력 컨베이어 제어 |
| PLC2 | 미쓰비시 Q시리즈 UDV | R2 출력 컨베이어·판별대 회전 제어 |
| RASPI | Raspberry Pi 4B | 카메라 기반 벽 패널 색상 실시간 판별 |
| R2_ARD | Arduino Uno WiFi | 조립 패널 4면 양불 판별 (O/X 신호) |
| AMR | LD-90 | 자재 투입·완성품 수령 자율 운반 |
| AMR_ARD | Arduino Uno WiFi | AMR 컨베이어 수량 확인·물품 핸드오프 |

---

## 4. 통신 프로토콜

### 4-1. PLC 이더넷 소켓 통신 (PLC1·PLC2)
```
[타입 2B][명령코드 2B][페이로드 16B]  = 총 20바이트, ASCII, CR/LF 없음
```
- 미쓰비시 Q시리즈 UDV 내장 이더넷 모듈을 통한 TCP 소켓 통신
- 서버 → PLC1: P1 prefix (예: `P100` = 생존확인, `P101` = 자재수신 명령)
- 서버 → PLC2: P2 prefix (예: `P204` = 물품이송, `P205` = 컨베이어 정지)
- PLC → 서버: `P1`/`P2` prefix + 응답코드 (99=생존확인 회신, 02/03/05/06/07)
- 3분 무통신 시 heartbeat 재송신, 수신 시 타이머 자동 리셋

### 4-2. 로봇·아두이노 (R1·R2·RASPI·R2_ARD·AMR_ARD)
- 개행(`\n`) 구분 텍스트 메시지
- 예: `SortDone:W`, `StackDone`, `AssemblyDone`, `CountOK`, `받음`

### 4-3. AMR ARCL
- 서버가 클라이언트로 ARCL 서버(AMR)에 접속, 재접속 자동화
- 패스워드 인증 자동 처리 → `executeMacro <이름>` / `goto <목표>` 명령
- 예: `executeMacro 2호기`, `executeMacro 수령요청`, `goto 박대기`
- 응답: `Completed macro <이름>`, `Arrived at <이름>` 파싱

---

## 5. 스레드 구조

```
main()
 ├─ accept_thread        # TCP 접속 수락, IP → 장비 식별, recv 스레드 생성
 ├─ amr_thread           # AMR ARCL 아웃바운드 접속, 재접속 루프
 ├─ console_thread       # 주문 입력(CLI), 자재/수령 인터랙션
 │    └─ _stdin_reader_thread  # input() 블로킹 격리
 ├─ [per-device] plc_recv_thread    # PLC1/PLC2 전용 (20B 고정 프레임)
 ├─ [per-device] device_recv_thread # 나머지 장비 (개행 기반)
 └─ main_loop()          # message_queue 소비 → 핸들러 dispatch
```

모든 수신 스레드는 수신한 메시지를 `message_queue`에만 투입.  
`main_loop()`가 단일 스레드에서 핸들러를 순차 실행 → 경쟁 조건 원천 차단.

---

## 6. 상태 머신 (SM 클래스)

단일 클래스 변수로 전체 공장 상태를 표현:

```
장비 상태:       r1_state / r2_state / amr_state / p1_state / p2_inspecting / p2_transferring
스테이션 점유:   station_assembly / station_output[3] / station_amr_conv[3] / inspection_state
자원 가용:       Sort_Available / Stack_Available / Assembly_Available
                 Transfer_to_Transfer_Available / p1_ready_input / p2_rotation_ready
AMR 화물 추적:  amr_pickup_total / amr_ard_recv_count / amr_ard_recv_pending
연결/헬스:      connected{} / last_seen{}
```

**핵심 설계 원칙:** SM 상태 변수는 *명령 발행 시점*에 서버가 직접 갱신 (장비 응답 대기 없음). 장비 응답은 다음 단계로의 전환 트리거로만 사용.

---

## 7. 결정 엔진 (Decision Engine)

```python
decide()                    # 모든 이벤트 핸들러 말미에 호출
 ├─ _decide_amr_cargo()    # 0순위: AMR 화물 비우기 (투입/수령 핸드셰이크)
 ├─ _decide_sort()         # 1순위: 자재 분류 명령
 ├─ _decide_inspection()   # 2순위: 판별 사이클 (4면 회전·양불 판정)
 ├─ _advance(order, stage) # 3순위: 주문별 AWAITING_* → 실행 명령 (최대 3개)
 ├─ _try_start_next()      # 4순위: 대기 주문 파이프라인 진입
 └─ _decide_amr_idle()     # 5순위: AMR 유휴 목적지 결정
```

**아키텍처 원칙:** *모든 장비 명령 송신은 decide() 계층에서만 발행*. 핸들러는 SM 상태 갱신 + decide() 호출만 담당. 명령 송신 책임을 결정 로직에 집중시켜 제어 흐름 추적 용이.

---

## 8. 주문 파이프라인 (Work Queue)

```
PENDING
 → SORT_WAITING     재고 부족 시 AMR 자재 투입 대기
 → SORTING          R1 색상 분류 중
 → STACKING         R1 패널 스택 적재 중
 → AWAITING_ASSEMBLY
 → ASSEMBLY         R2 패널 조립 중
 → AWAITING_TRANSFER
 → TRANSFER         R2 → 판별대 이송 중
 → INSPECTION       PLC2 회전 + R2_ARD 판별 (4면)
 → AWAITING_OUTPUT / AWAITING_DISPOSAL
 → OUTPUT_TRANSFER  R2 → 출력 컨베이어 이송 중
 → AWAITING_AMR
 → AMR_PICKUP       AMR 2호기 이동 중
 → AWAITING_RECV    AMR 수령 위치 도착, 사용자 대기
 → DONE / DISPOSED
```

최대 3개 주문 동시 진행 (`MAX_ACTIVE_ORDERS = 3`). 선입선출, 오래된 주문 우선.

---

## 9. 데이터베이스 (SQLite)

| 테이블 | 내용 |
|---|---|
| `orders` | 주문 정보 (좌표, 층수, 벽/베이스/천장 색상, 현재 공정, 상태) |
| `inventory` | 자재 빈(BpC1/2, CpC1/2, WpC1~6) 재고 수량 |
| `process_log` | 공정 이력 (주문별 단계·장비·결과·타임스탬프) |
| `inspection` | 판별 결과 (주문별 면×양불) |
| `state` | 서버 상태 영속화 |

---

## 10. 주요 알고리즘

**색상 분류 빈 배정** (`get_sort_bin` / `get_pick_bin`)  
색상(W/Y/P) → 지정된 WpC 빈 우선순위 순으로 채움. 적재 시 역순으로 수량 많은 빈부터 꺼냄.

**판별대 4면 회전 로직**  
PLC2 "01"(회전 명령) → PLC2 "02"(회전 완료) 핸드셰이크 반복. 불량 판정 시 잔여 회전으로 초기 위치 복귀 후 AWAITING_DISPOSAL 전환.

**AMR 픽업 수량 동적 계산**  
AMR 2호기 도착 시점 `station_output[0~1]` 개수로 `pickup_total` 설정. 이후 PLC2 "03"(물품도착) 수신 시 `station_output[2]`가 [0][1] 모두 점유된 경우 `pickup_total += 1`로 갱신 → 컨베이어 이송 도중 도착한 3번째 물품도 빠짐없이 수령.

**출력 컨베이어 슬롯 관리**  
`station_output[2]`는 R2 OutputTransfer 명령 발행 시 즉시 세팅 (물리 도착 전에 논리적 선점). PLC2 "03"을 유일한 슬롯 확정 기준으로 사용 → R2 완료 신호와의 순서 역전 문제 해소.

---

## 11. 서버 구성 다이어그램

```
[고객] ─── (Web UI: 주문 폼) ─────────────────────────┐
                                                        ▼
                                               [Command Center Server]
                                               192.168.3.8:9090
                                                  │    (Python)
          ┌───────────────────────────────────────┤
          │                                       │
    [R1 ABB IRB1200]   [R2 ABB IRB1200]          │
    분류·적재           조립·이송·출력            │
          │                   │                   │
    [PLC1]              [PLC2]  [R2_ARD]          │
    입력컨베이어        판별대·출력컨베이어        │
          │                                       │
    [RASPI]  ←── 색상 판별                        │
          │                                       │
    [AMR LD-90] ←── ARCL (아웃바운드 접속) ───────┘
          │
    [AMR_ARD]
    AMR 컨베이어
```
