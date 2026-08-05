MODULE MultiPick

    ! ============================================================
    ! 티칭 포인트 — FlexPendant에서 직접 수정
    ! ============================================================
    PERS robtarget p_home        := [[0,0,0],[1,0,0,0],[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]];
    PERS robtarget p_pick_origin := [[0,0,0],[1,0,0,0],[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]];
    PERS robtarget p_place_1     := [[0,0,0],[1,0,0,0],[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]];
    PERS robtarget p_place_2     := [[0,0,0],[1,0,0,0],[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]];
    PERS robtarget p_place_3     := [[0,0,0],[1,0,0,0],[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]];
    PERS robtarget p_place_4     := [[0,0,0],[1,0,0,0],[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]];
    PERS robtarget p_place_5     := [[0,0,0],[1,0,0,0],[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]];
    PERS robtarget p_place_6     := [[0,0,0],[1,0,0,0],[0,0,0,0],[9E9,9E9,9E9,9E9,9E9,9E9]];

    ! ============================================================
    ! 파라미터 — 필요 시 수정
    ! ============================================================
    CONST num SPACING  := 110;  ! 공작물 간격 (mm)
    CONST num APPROACH := 50;   ! 상하 접근 높이 (mm)
    CONST num GRIP_DLY := 0.3;  ! 그리퍼 동작 대기 (초)

    ! 3×2 그리드 오프셋 (행1: 1~3번, 행2: 4~6번)
    CONST num OFF_X{3} := [0, SPACING, SPACING*2];
    CONST num OFF_Y{2} := [0, SPACING];

    VAR robtarget p_place{6};
    VAR num       idx;

    ! ============================================================
    PROC main()
        p_place{1} := p_place_1;
        p_place{2} := p_place_2;
        p_place{3} := p_place_3;
        p_place{4} := p_place_4;
        p_place{5} := p_place_5;
        p_place{6} := p_place_6;

        MoveJ p_home, v300, z50, tool0;

        idx := 1;
        FOR row FROM 1 TO 2 DO
            FOR col FROM 1 TO 3 DO
                PickWork OFF_X{col}, OFF_Y{row};
                PlaceWork idx;
                idx := idx + 1;
            ENDFOR
        ENDFOR

        MoveJ p_home, v300, z50, tool0;
        TPWrite "완료 — 6개 이송 완료";
    ENDPROC

    ! ============================================================
    PROC PickWork(num ox, num oy)
        VAR robtarget p_app;
        VAR robtarget p_pick;

        p_app  := Offs(p_pick_origin, ox, oy, APPROACH);
        p_pick := Offs(p_pick_origin, ox, oy, 0);

        MoveJ p_app,  v300, z20,  tool0;
        MoveL p_pick, v50,  fine, tool0;
        SetDO do00, 1;          ! 그리퍼 ON — 공작물 집기
        WaitTime GRIP_DLY;
        MoveL p_app,  v100, z20, tool0;
    ENDPROC

    ! ============================================================
    PROC PlaceWork(num n)
        VAR robtarget p_app;

        p_app := Offs(p_place{n}, 0, 0, APPROACH);

        MoveJ p_app,      v300, z20,  tool0;
        MoveL p_place{n}, v50,  fine, tool0;
        SetDO do01, 1;          ! 그리퍼 OFF — 공작물 놓기
        WaitTime GRIP_DLY;
        MoveL p_app,      v100, z20, tool0;
    ENDPROC

ENDMODULE
