# PROJECT CONTEXT — AMR 공정 MES 서버
> 이 문서는 새 Claude 세션이 프로젝트를 즉시 파악하기 위한 참조용입니다.
> 작성 기준: command_center_20260808.py (최신 버전)

---

## 1. 프로젝트 개요

**과정**: 2026년 공정자동화 교육과정 팀 프로젝트  
**목적**: 컬러 블록으로 구성된 벽 구조물(Wall)을 자동으로 정렬·조립·출력하는 스마트팩토리 시뮬레이션  
**서버 역할**: MES(Manufacturing Execution System) — 모든 장비의 명령 결정 및 공정 흐름 조율  
**주문 흐름**: 주문 수신 → 자재 적재(R1) → 조립(R2) → 판별(판별대+PLC2+R2_ARD) → 출력/폐기 → AMR 수령

---

## 2. 네트워크 구성

| 장비 | IP | 역할 |
|------|-----|------|
| 서버(MES) | 192.168.3.8 | command_center.py 실행 (TCP 9090 수신) |
| R1 (ABB 로봇 1호기) | 192.168.3.2 | 자재 분류(Sort) + 적재(Stack) |
| R2 (ABB 로봇 2호기) | 192.168.3.3 | 조립(Assembly) + 이송 + 폐기 |
| PLC1 | 192.168.3.39 | P1 컨베이어 제어 (자재 입력) |
| PLC2 | 192.168.3.40 | P2 컨베이어 제어 (판별대·출력이송) |
| RASPI | 192.168.3.21 | 라즈베리파이 (카메라/색상 판별) |
| R2_ARD | 192.168.3.22 | R2 아두이노 (판별 결과 수신) |
| AMR_ARD | 192.168.3.23 | AMR 아두이노 (자재 투입/출력 핸드셰이크) |
| AMR (LD-90) | 192.168.3.11 | 자율이동로봇 (ARCL 7171, 서버가 접속) |

**통신 방식**:
- 대부분: 장비 → 서버 9090 TCP 접속
- AMR만 역방향: 서버 → AMR 7171 ARCL 접속

**PLC 프로토콜**: 고정 20바이트 프레임 (타입2 + 코드2 + 페이로드16), CR/LF 없음

---

## 3. 서버 아키텍처

```
스레드 구조:
  accept_thread      : TCP 9090 수신 → IP로 장비 식별 → recv 스레드 기동
  device_recv_thread : 장비별 1:1 소켓 수신 → message_queue 투입
  amr_thread         : 서버→AMR ARCL 접속 유지 → message_queue 투입
  console_thread     : 콘솔 사용자 입력 (블로킹 격리)
  [메인 스레드]       : message_queue 소비 → 판단 → send_to()
```

**핵심 원칙**: 이벤트 수신 → 상태 업데이트 → `decide()` 호출. `decide()`가 모든 명령 결정.

---

## 4. 주요 상태 머신 (SM 클래스)

```python
SM.r1_state          # "IDLE" / "SORTING" / "STACKING"
SM.r2_state          # "IDLE" / "ASSEMBLY" / "TRANSFER" / "INSPECTION" / "DISPOSAL"
SM.amr_state         # "IDLE" / "GOING_TO_NEEDINPUT" / "AT_NEEDINPUT" / "GOING_TO_R1INPUT" /
                     # "AT_R1INPUT" / "GOING_TO_박대기" / "AT_박대기" /
                     # "GOING_TO_R2TRANSFER" / "AT_R2TRANSFER" /
                     # "GOING_TO_NEEDRECV" / "AT_NEEDRECV" / "COUNT_CONFIRMED"

SM.Stack_Available   # True = 적재 명령 발행 가능
SM.Sort_Available    # True = 분류 명령 발행 가능
SM.Assembly_Available# True = 조립 명령 발행 가능 (P1 "07" 수신 시 True)
SM.Transfer_to_Transfer_Available  # True = R2 출력이송 명령 가능

SM.station_assembly  # 조립대 점유 order_id (None = 비어있음)
SM.station_output    # [None, None, None] — 출력대기 1,2,3번 슬롯 (index 0 = 1번)
SM.inspection_state  # (order_id, state) or None
SM.station_input     # P1 입력컨베이어 분류 대기 수량
SM.consol_input      # 사용자 콘솔 입력 세트 수 (AMR 컨베이어 적재)

SM.p1_state          # None / "INPUT_PREPARING" / "INPUT_RECEIVING" / "SORT_MOVING"
SM.p1_ready_input    # True = AMR_ARD로부터 자재 수신 가능
```

---

## 5. 주문 흐름 단계 (WORK_STAGES)

```
PENDING           → 파이프라인 진입 전
SORT_WAITING      → 재고 부족, AMR 자재 투입 대기
STACKING          → R1 적재 중  
AWAITING_ASSEMBLY → StackDone 후 R2 조립 대기
ASSEMBLY          → R2 조립 중
AWAITING_TRANSFER → AssemblyDone 후 판별대 이송 대기
TRANSFER          → R2 판별대 이송 중
INSPECTION        → 판별 중 (4면 × R2_ARD 판독)
AWAITING_OUTPUT   → 양품 → 출력이송 대기
AWAITING_DISPOSAL → 불량 → 폐기 대기
OUTPUT_TRANSFER   → R2 출력이송 중
AWAITING_AMR      → AMR 픽업 대기
AMR_PICKUP        → AMR 이동 중
AWAITING_RECV     → AMR 도착, 사용자 수령 대기
DISPOSAL          → R2 폐기 중
DONE / DISPOSED   → 종료
```

**동시 처리**: 최대 3개 주문 병렬 진행 (`MAX_ACTIVE_ORDERS = 3`)

---

## 6. decide() 우선순위

```
0순위: _decide_amr_cargo()   — AMR 화물 비우기 (투입/이송 핸드셰이크)
1순위: _decide_sort()        — 분류 명령 (입력 자재 정리)
2순위: _decide_inspection()  — 판별 사이클
3순위: 주문별 _advance()     — AWAITING_* 단계 처리
4순위: _try_start_next()     — 대기 주문 파이프라인 진입
5순위: _decide_amr_idle()    — AMR 유휴 목적지 결정
```

---

## 7. Stack 명령 조건 (핵심)

Stack 명령(`R1`에 `"Stack:XXXXXX"`)은 아래 **3가지 조건을 모두 만족**할 때만 발행됨:

```python
SM.r1_state == "IDLE"          # R1 유휴
SM.Stack_Available == True     # 적재 명령 가능
db.can_fulfill_order(order_id) # 주문 재고 충족
```

**station_assembly, r2_state 등은 Stack 조건에 무관.**

Stack 명령 포맷: `"Stack:XXXXXX"` — 6자리, 각 자리 = 빈 번호  
순서: base빈 → wall1~4빈 → ceil빈  
색상코드: W=하양, Y=노랑, P=분홍  
예) `order 1 1 WWYW` → 4자리 벽 색상 지정 (base/ceil 자동 결정)  
예) `order 1 1 WWYWYW` → 6자리 (base+벽4+ceil 직접 지정)

---

## 8. 콘솔 명령어

```
order <x> <y> <색상4자리>    주문 입력 (예: order 1 1 WWYW)
order <x> <y> <색상6자리>    주문 입력 (예: order 1 1 WWYWYW)
start                        서버 시작 (decide() 활성화)
decide                       decide() 강제 호출 (디버그용) ← 금번 세션에서 추가됨
help                         명령어 목록
```

---

## 9. 알려진 버그 / 이슈

### Bug: Stack 명령 지연 (P106 지점)
- **증상**: 모든 Stack 조건이 충족됐음에도 Stack 명령이 즉시 발행되지 않고 수 분 후 발행됨
- **확인된 사항**: [15:44:53] 시점에 `can_fulfill_order(#71)=True`, `r1_state=IDLE`, `Stack_Available=True` 모두 충족 → 그러나 Stack:231452 for #71은 [15:49:00]에 #70 픽업 확인 후에야 발행
- **추가된 디버그 도구**: 콘솔 `decide` 명령으로 수동 트리거 가능
- **미해결**: 원인 분석 중

### P102 핸들러 동작
- P102는 **마지막 아이템**(consol_input==0)일 때만 `decide()` 호출
- 각 아이템마다 호출하지 않음 (의도된 설계)

---

## 10. 주요 파일 위치 (GitHub)

**Repository**: `https://github.com/rainjin1/2026_portfolio`  
**최신 서버 파일 경로**: `AMR_Project/Server/`

| 파일 | 설명 |
|------|------|
| `command_center_20260808.py` | **최신 메인 서버** (이 문서 기준) |
| `command_center_20260805.py` | 이전 버전 |
| `manual_test_server.py` | 수동 테스트용 서버 |
| `db.py` | 재고 관리, 주문 DB |
| `customer_gui.py` | 고객 주문 GUI |
| `raspi_test_server.py` | 라즈베리파이 테스트 서버 |
| `Works/` | 구버전 보관 폴더 |

---

## 11. 팀 구성

**총 9명 / 발표 대상: 동기 수강생 + 교수 2명**

**Software팀**
| 이름 | 역할 |
|------|------|
| 강진우 (팀장) | MES 서버 구현 |
| 박용의 (부팀장) | RAPID, LADDER, 서버 설계 |
| 김순호 | 아두이노 2대, 라즈베리파이 |
| 곽현수 | AMR 티칭 및 경로설계 전체 |

**Hardware팀**
| 이름 | 역할 |
|------|------|
| 나기원 | 기구설계 총괄 |
| 안영민 | 기구설계 및 설치 |
| 김수형 | 기구설치 및 전장 |
| 박상면 | 전장 및 배선 총괄 |
| 원유훈 | 기구설치 및 출력품 후처리 |

---

## 12. 이 세션에서 진행된 작업 이력

- `decide` 콘솔 명령 추가 (0805, 0808 양쪽에 적용)
- GitHub 구조 정리: `공정자동화프로젝트/AMR_Project/Server/Works/` (구버전), `AMR_Project/Server/` (최신)
- PPT 구성 9섹션 논의 (내용 미확정, 상담 가능)

---
*최종 업데이트: 2026-08-09*
