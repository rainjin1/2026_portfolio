# RAPID 프로그래밍 문법 정리
> 출처: 산업용로봇제어(ABB IRB1200) - 충남인력개발원, 저자 최경민 (2022)

---

## 1. 프로그램 구조

```rapid
MODULE MainModule
    ! 데이터 선언
    CONST robtarget p10 := [...];
    VAR num reg1;

    PROC main()
        ! 메인 루틴 - 반복 실행
    ENDPROC

    PROC SubRoutine()
        ! 서브 루틴
    ENDPROC
ENDMODULE
```

- 프로그램 = 데이터 + 명령어 (루틴 단위)
- 확장자: `.pgf`
- `main()` 루틴이 진입점, 반복 실행

---

## 2. 이동 명령어

### MoveL — 직선 이동 (TCP 직선 경로)
```rapid
MoveL p10, v100, fine, tool0;
MoveL Offs(p10, 100, 0, 0), v100, z50, tool0;
MoveL *, v1000\T:=5, fine, grip3;   ! 5초 소요
```

### MoveJ — 관절 이동 (비선형 경로, 빠름)
```rapid
MoveJ p10, vmax, z30, tool2;
MoveJ p20, v100, fine, tool2;
```
> TCP 경로가 곡선 → 충돌 주의. RobotStudio에서 사전 확인 필수.

### MoveAbsJ — 절대 관절 위치 이동
```rapid
CONST jointtarget calib_pos := [[0,0,0,0,0,0],[0,9E9,9E9,9E9,9E9,9E9]];
MoveAbsJ calib_pos, v100, fine, tool0;
```
> 각 축 각도를 직접 지정. 6축 고정 시 활용.

#### 6축 고정하고 이동하는 패턴
```rapid
VAR jointtarget cur;
cur := CJointT();           ! 현재 관절값 읽기
cur.robax.rax_6 := 90;      ! 6축만 고정값 지정
MoveAbsJ cur, v100, fine, tool0;
```

#### 툴 선택 루틴 (6축 회전으로 툴 전환)
```rapid
PROC SelectTool(num toolNum)
    VAR jointtarget cur;
    cur := CJointT();
    cur.robax.rax_6 := toolNum * 90;  ! 0°/90°/180°/270°
    MoveAbsJ cur, v100, fine, tool0;
ENDPROC
! 호출: SelectTool 1;  SelectTool 2;
```

### MoveC — 원호 이동
```rapid
MoveC p1, p2, v500, z30, tool2;
! p1: 원호 중간점, p2: 목표점
```

### RelTool — 툴 좌표계 기준 상대 이동
```rapid
MoveL RelTool(p1, 0, 0, 100), v100, fine, tool1;  ! 툴 Z방향 100mm
MoveL RelTool(p1, 0, 0, 0\Rz:=25), v100, fine, tool1;  ! Z축 25° 회전
```

### Offs — 포인트 기준 오프셋
```rapid
MoveL Offs(p10, 100, 0, 50), v100, fine, tool0;
! p10에서 X+100, Z+50 위치
```

---

## 3. 속도 / 존 데이터

### speeddata
```rapid
! 형식: [v_tcp, v_ori, v_leax, v_reax]
VAR speeddata vmedium := [1000, 30, 200, 15];
! v_tcp: TCP 속도 (mm/s)
! v_ori: 방향 변경 속도 (°/s)
```
- 기본 상수: v10, v50, v100, v200, v500, v1000, vmax 등
- 수동 모드 최고속도: 250 mm/s

### zonedata
- `fine`: 정위치 정지
- `z10`, `z30`, `z50`, `z100`: 코너 라운딩 (mm)

---

## 4. 데이터 타입

### robtarget
```rapid
CONST robtarget p10 := [
    [600, 500, 225.3],          ! trans (x,y,z mm)
    [1, 0, 0, 0],               ! rot (쿼터니언 q1~q4)
    [1, 1, 0, 0],               ! robconf (cf1,cf4,cf6,cfx)
    [9E9, 9E9, 9E9, 9E9, 9E9, 9E9]  ! extax (외부축)
];
```

### jointtarget
```rapid
CONST jointtarget jt1 := [
    [0, 0, 0, 0, 0, 90],        ! robax (j1~j6, 도)
    [9E9, 9E9, 9E9, 9E9, 9E9, 9E9]  ! extax
];
```

### tooldata
```rapid
PERS tooldata gripper := [
    TRUE,                        ! robhold (로봇이 툴 보유)
    [[97.4, 0, 223.1], [0.924, 0, 0.383, 0]],  ! tframe (TCP 위치/방향)
    [5, [23, 0, 75], [1,0,0,0], 0, 0, 0]        ! tload (질량/무게중심)
];
```

---

## 5. 변수 선언

```rapid
VAR num reg1;           ! 변수 (실행 중 변경 가능)
CONST num MAX := 100;   ! 상수 (변경 불가)
PERS num counter := 0;  ! 기억변수 (전원 꺼져도 유지)

VAR bool running := TRUE;
VAR robtarget p_temp;
VAR jointtarget jt_temp;
```

---

## 6. 제어 구조

### IF / ELSEIF / ELSE
```rapid
IF user_num = 1 THEN
    MoveL p10, v100, fine, tool0;
ELSEIF user_num = 2 THEN
    MoveL p20, v100, fine, tool0;
ELSE
    TPWrite "Invalid input";
ENDIF
```

### FOR
```rapid
FOR ct FROM 1 TO 6 DO
    MoveL Offs(p10, 0, 0, 50), v100, z20, tool0;
    MoveL p10, v20, fine, tool0;
ENDFOR
```
- STEP 사용: `FOR i FROM 10 TO 2 STEP -2 DO`
- **break 없음** → 탈출 시 `GOTO` 또는 `WHILE` 사용

### GOTO (반복문 탈출)
```rapid
FOR i FROM 1 TO 10 DO
    IF i = 5 THEN
        GOTO exitLoop;
    ENDIF
ENDFOR
exitLoop:
```

### WHILE
```rapid
WHILE TRUE DO
    IF condition THEN
        ! 작업
    ENDIF
ENDWHILE
```

---

## 7. I/O 제어

```rapid
SetDO do00, 1;              ! 디지털 출력 ON
SetDO do00, 0;              ! 디지털 출력 OFF
Reset do15;                 ! 출력 0으로
Set do15;                   ! 출력 1으로
InvertDO do15;              ! 반전

PulseDO \PLength:=0.2, do00;  ! 0.2초 펄스

WaitDI di04, 1;             ! 디지털 입력 1 될 때까지 대기
WaitDO do04, 1;             ! 디지털 출력 1 될 때까지 대기
WaitTime 1;                 ! 1초 대기
```

### 그리퍼 ON/OFF 패턴
```rapid
PROC grip_on()
    PulseDO \PLength:=0.2, do00_grip_on;
    WaitDI di00_grip_on_sen, 1;
ENDPROC

PROC grip_off()
    PulseDO \PLength:=0.2, do01_grip_off;
    WaitDI di01_grip_off_sen, 1;
ENDPROC
```

---

## 8. 기타 명령어

```rapid
AccSet 50, 100;     ! 가속도 50%, 램프 100%
CJointT()           ! 현재 관절 각도 읽기 → jointtarget 반환
CRobT()             ! 현재 TCP 위치 읽기 → robtarget 반환
Incr reg1;          ! reg1 += 1
Clear reg1;         ! reg1 = 0
EXIT;               ! 프로그램 종료
ExitCycle;          ! 현재 사이클 종료, PP를 main으로
RETURN;             ! 루틴 종료
```

### FlexPendant 대화
```rapid
TPErase;
TPWrite "메시지";
TPReadNum user_num, "숫자 입력:";
```

---

## 9. 프로시저 / 함수 / 트랩

### 프로시저 (PROC)
```rapid
PROC Routine1(num param0)
    FOR ct FROM 1 TO param0 DO
        MoveL p10, v100, fine, tool0;
    ENDFOR
ENDPROC
! 호출: Routine1 3;
```

### 함수 (FUNC) — 리턴값 있음
```rapid
FUNC num Add(num a, num b)
    RETURN a + b;
ENDFUNC
! 사용: result := Add(10, 20);
```

### 트랩 (TRAP) — 인터럽트 루틴
```rapid
VAR intnum interrupt_sign;

CONNECT interrupt_sign WITH Etrap;
ISignalDI di09_interrupt, high, interrupt_sign;
IWatch interrupt_sign;

TRAP Etrap
    VAR robtarget p1;
    StorePath;
    p1 := CRobT();
    MoveJ phome, v200, z50, tool0;
    WaitDI di08_restart, 1;
    MoveL p1, v100, fine, tool0;
    RestoPath;
    StartMove;
ENDTRAP
```

---

## 10. 배열 활용

```rapid
VAR num plate_p{6,2} := [[0,0],[0,-110],[110,0],[110,-110],[220,0],[220,-110]];

FOR ct FROM 1 TO 6 DO
    MoveJ Offs(p10, plate_p{ct,1}, plate_p{ct,2}, 50), v100, z20, tool0;
    MoveL Offs(p10, plate_p{ct,1}, plate_p{ct,2}, 0), v20, fine, tool0;
ENDFOR
```

---

## 11. 오류 처리기

```rapid
FUNC num safediv(num x, num y)
    RETURN x / y;
ERROR
    IF ERRNO = ERR_DIVZERO THEN
        TPWrite "0으로 나눌 수 없음";
        RETURN x;
    ENDIF
ENDFUNC
```
