MODULE T_communicate
! ===================================================================
! Project  : Modular House Wall Panel Auto-Stacking System
! File     : WallSorting_Comm_20260723.mod
! Date     : 2026-07-23
! Version  : 3.0  -- Client mode, always-receive, MC server 연결
! Scope    : T_comm task -- TCP client, Master Commander 서버에 접속
!            Configure as: Task type NORMAL, no motion
!
! Architecture:
!   - 로봇이 클라이언트, MC 서버에 접속
!   - SocketReceive 상시 대기, 수신 즉시 PERS 변수 업데이트
!   - T_ROB1과 PERS 변수(temp_a ~ temp_j)로 데이터 공유
!
! TODO: 서버 IP / 포트 확정 후 "000.000.0.0" 및 0 을 교체할 것
! ===================================================================

    ! -- PERS 공유 변수 (T_ROB1과 공유) ----------------------------
    PERS string temp_a := "";   ! 범용 공유 변수 a
    PERS string temp_b := "";   ! 범용 공유 변수 b
    PERS string temp_c := "";   ! 범용 공유 변수 c
    PERS string temp_d := "";   ! 범용 공유 변수 d
    PERS string temp_e := "";   ! 범용 공유 변수 e
    PERS string temp_f := "";   ! 범용 공유 변수 f
    PERS string temp_g := "";   ! 범용 공유 변수 g
    PERS string temp_h := "";   ! 범용 공유 변수 h
    PERS string temp_i := "";   ! 범용 공유 변수 i
    PERS string temp_j := "";   ! 범용 공유 변수 j

    ! -- 소켓 변수 (T_comm 내부 전용) ------------------------------
    VAR socketdev client_sock;
    VAR string    recv_str;

    ! ================================================================
    ! soc_main
    !   MC 서버에 클라이언트로 접속 후 상시 수신 대기.
    !   수신된 문자열을 temp_* PERS 변수에 기록하여 T_ROB1과 공유.
    ! ================================================================
    PROC soc_main()

        ! -- 서버 접속 (1회) ---------------------------------------
        ! TODO: "000.000.0.0" → 실제 MC 서버 IP로 교체
        ! TODO: 0 → 실제 포트번호로 교체
        SocketCreate  client_sock;
        SocketConnect client_sock, "000.000.0.0", 0 \Time:=30;

        ! -- 상시 수신 루프 ----------------------------------------
        WHILE TRUE DO
            SocketReceive client_sock \Str:=recv_str \Time:=WAIT_MAX;
            ! TODO: recv_str 파싱 후 해당 temp_* 변수에 기록
            ! 예시) temp_a := recv_str;
        ENDWHILE

    ENDPROC

ENDMODULE
