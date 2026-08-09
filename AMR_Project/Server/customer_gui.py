"""
JW건설 고객 주문 웹 GUI
customer_gui.py

Flask 웹 서버 — 포트 5000, 0.0.0.0 바인딩
192.168.3.0/24 서브넷 전체 접근 가능.
"""

from flask import Flask, jsonify, request, render_template_string
import db 
import socket

app = Flask(__name__)

CC_HOST = "192.168.3.8"
CC_PORT = 9090

# ── 유틸 ─────────────────────────────────────────────

def is_cc_running() -> bool:
    """command_center TCP 9090 응답 여부 확인."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect((CC_HOST, CC_PORT))
        s.close()
        return True
    except Exception:
        return False


PROCESS_DISPLAY = {
    "PENDING":           "대기중",
    "SORT_WAITING":      "자재 준비중",
    "SORTING":           "제작중",
    "STACKING":          "제작중",
    "AWAITING_ASSEMBLY": "제작중",
    "ASSEMBLY":          "조립중",
    "AWAITING_TRANSFER": "조립중",
    "TRANSFER":          "검사중",
    "INSPECTION":        "검사중",
    "AWAITING_OUTPUT":   "검사 완료",
    "OUTPUT_TRANSFER":   "배송중",
    "AWAITING_AMR":      "배송중",
    "AMR_PICKUP":        "배송중",
    "AWAITING_RECV":     "수령 대기",
    "AWAITING_DISPOSAL": "검사중",
    "DISPOSAL":          "불량 처리중",
    "DONE":              "완료 ✓",
    "DISPOSED":          "불량 폐기",
}


def to_display(process: str | None) -> str:
    return PROCESS_DISPLAY.get(process or "", "대기중")


# ── API ──────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    return jsonify({"running": is_cc_running()})


@app.route("/api/grid")
def api_grid():
    """점유된 좌표 목록 반환 [{x, y, z}]."""
    conn = db._connect()
    rows = conn.execute("SELECT pos_x, pos_y, pos_z FROM orders").fetchall()
    conn.close()
    return jsonify([{"x": r[0], "y": r[1], "z": r[2]} for r in rows])


@app.route("/api/order", methods=["POST"])
def api_create_order():
    """
    주문 생성.
    요청: {modules: [{x, y, z, wall_top, wall_bottom, wall_left, wall_right}]}
    응답: {order_ids: [...], set_id: int|null}
    """
    data = request.json or {}
    modules = data.get("modules", [])
    if not modules:
        return jsonify({"error": "모듈 정보가 없습니다"}), 400

    set_id = None
    if len(modules) > 1:
        set_id = db.create_order_set()

    order_ids = []
    for m in modules:
        oid = db.create_order(
            m["x"], m["y"], m["z"],
            m["wall_top"], m["wall_bottom"], m["wall_left"], m["wall_right"],
            set_id=set_id,
        )
        if oid is None:
            return jsonify({"error": f"좌표 ({m['x']},{m['y']},{m['z']}) 중복 또는 오류"}), 409
        order_ids.append(oid)

    return jsonify({"order_ids": order_ids, "set_id": set_id})


@app.route("/api/lookup", methods=["POST"])
def api_lookup():
    """
    주문세트번호 또는 주문번호로 상태 조회.
    세트번호 우선 탐색.
    """
    data = request.json or {}
    number = str(data.get("number", "")).strip()
    if not number.isdigit():
        return jsonify({"error": "유효하지 않은 번호입니다"}), 400
    n = int(number)

    # 세트 먼저
    orders = db.get_orders_by_set(n)
    if orders:
        return jsonify({
            "type": "set",
            "set_id": n,
            "orders": [
                {"order_id": o["order_id"], "display": to_display(o.get("current_process"))}
                for o in orders
            ],
        })

    # 단일 주문
    order = db.get_order(n)
    if order:
        return jsonify({
            "type": "order",
            "order_id": n,
            "display": to_display(order.get("current_process")),
        })

    return jsonify({"error": "해당 번호의 주문을 찾을 수 없습니다"}), 404


@app.route("/")
def index():
    return render_template_string(HTML)


# ── HTML ─────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JW건설 모듈형 주택</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Malgun Gothic','맑은 고딕',sans-serif;background:#f0f4f8;min-height:100vh}

/* 준비중 오버레이 */
#overlay{position:fixed;inset:0;background:rgba(20,40,70,0.96);z-index:1000;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1rem}
#overlay h1{color:#fff;font-size:1.8rem;text-align:center}
#overlay p{color:#90b4d4;font-size:1.05rem}

/* 헤더 */
.header{background:#1a3a5c;color:#fff;text-align:center;padding:1.8rem 1rem;
  font-size:2rem;letter-spacing:0.04em}

/* 메인 배너 */
.main-banners{display:flex;gap:6rem;padding:3rem 2rem;max-width:1300px;margin:0 auto}
.banner{flex:1;background:#fff;border-radius:20px;padding:5rem 2rem;text-align:center;
  font-size:3.4rem;font-weight:bold;cursor:pointer;color:#1a3a5c;white-space:nowrap;
  box-shadow:0 4px 16px rgba(0,0,0,.1);transition:transform .15s,box-shadow .15s;
  line-height:1.6}
.banner:hover{transform:translateY(-4px);box-shadow:0 8px 24px rgba(0,0,0,.15)}
.banner.order{border-top:8px solid #27ae60}
.banner.check{border-top:8px solid #2980b9}

/* 화면 컨테이너 */
.screen{display:none;max-width:680px;margin:0 auto;padding:1.5rem}
.screen.active{display:block}

/* 카드 */
.card{background:#fff;border-radius:12px;padding:1.8rem;box-shadow:0 2px 12px rgba(0,0,0,.08)}
.step-info{color:#888;font-size:.88rem;margin-bottom:.4rem}
.card h2{color:#1a3a5c;margin-bottom:1.3rem;font-size:1.2rem}

/* 수량 입력 */
.count-wrap{display:flex;align-items:center;gap:1rem;margin:0.5rem 0 1.5rem}
.count-wrap button{width:44px;height:44px;border-radius:8px;border:2px solid #dde;
  background:#f0f4f8;font-size:1.3rem;cursor:pointer;color:#1a3a5c}
.count-wrap button:hover{background:#e0eaf4}
#module-count{width:70px;text-align:center;padding:.6rem;border-radius:8px;
  border:2px solid #dde;font-size:1.2rem}

/* 층수 버튼 */
.floor-btns{display:flex;gap:.7rem;margin:.6rem 0 1rem}
.floor-btn{padding:.55rem 1.3rem;border-radius:8px;border:2px solid #dde;
  background:#fff;cursor:pointer;font-size:.95rem;transition:all .12s;color:#444}
.floor-btn.active{background:#1a3a5c;color:#fff;border-color:#1a3a5c}
.floor-btn:disabled{opacity:.35;cursor:not-allowed}

/* 좌표 그리드 */
.grid-wrap{margin:1rem 0;overflow-x:auto}
.grid-outer{display:inline-flex;gap:4px}
.y-labels{display:flex;flex-direction:column;justify-content:space-around;padding:3px 0}
.y-label{width:22px;height:58px;display:flex;align-items:center;justify-content:center;
  font-size:.72rem;color:#999}
.grid-col{display:flex;flex-direction:column}
.x-row{display:flex;gap:6px;margin-bottom:4px;padding-left:26px}
.x-label{width:58px;text-align:center;font-size:.72rem;color:#999}
.grid{display:grid;grid-template-columns:repeat(4,58px);gap:6px}
.cell{width:58px;height:58px;border-radius:8px;border:2px solid #dde;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  font-size:.78rem;font-weight:bold;transition:all .12s;color:#555;user-select:none}
.cell:hover:not(.no-hover){border-color:#1a3a5c;transform:scale(1.04)}
.cell.empty{background:#fff}
.cell.f1{background:#fff9db;border-color:#f5c518;color:#7a5a00}
.cell.f2{background:#ffbfa3;border-color:#e74c3c;color:#7a1a00}
.cell.f3{background:#c0392b;border-color:#8e1c14;color:#fff;cursor:not-allowed}
.cell.picked{background:#2980b9;border-color:#1a5276;color:#fff}
.cell.dim{opacity:.35;cursor:not-allowed}
.cell.no-hover{cursor:not-allowed}

/* 범례 */
.legend{display:flex;flex-wrap:wrap;gap:.6rem 1.2rem;margin:.6rem 0;font-size:.78rem;color:#666}
.leg{display:flex;align-items:center;gap:.35rem}
.leg-dot{width:13px;height:13px;border-radius:3px;border:1px solid #ccc;flex-shrink:0}

/* 선택 안내 */
.sel-info{text-align:center;color:#1a3a5c;font-weight:bold;min-height:1.4rem;margin:.4rem 0;font-size:.95rem}

/* 벽 색상 */
.wall-grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin:.8rem 0}
.wall-item label{display:block;font-size:.88rem;color:#666;margin-bottom:.35rem;font-weight:600}
.color-btns{display:flex;gap:.4rem}
.cbtn{flex:1;padding:.45rem .2rem;border-radius:6px;border:2px solid transparent;
  cursor:pointer;font-size:.85rem;font-weight:bold;transition:all .12s}
.cbtn.pink{background:#ffd6e0;color:#8b1a3a}
.cbtn.white{background:#f5f5f5;color:#444;border-color:#ddd}
.cbtn.yellow{background:#fff3cd;color:#7a5800}
.cbtn.on{border-color:#1a3a5c;box-shadow:0 0 0 2px #1a3a5c}

/* 확인 목록 */
.mod-item{padding:.75rem 1rem;border-radius:8px;background:#f7fafc;margin-bottom:.5rem;border:1px solid #e0e8f0}
.mod-item .mod-title{font-weight:bold;color:#1a3a5c;margin-bottom:.3rem}
.mod-item .mod-detail{font-size:.87rem;color:#555}

/* 완료 박스 */
.done-box{background:#edf7ff;border:2px solid #3498db;border-radius:12px;
  padding:1.4rem 1.8rem;text-align:center;margin-top:1rem}
.done-box .set-label{font-size:.85rem;color:#666;margin-bottom:.2rem}
.done-num{font-size:2.2rem;font-weight:bold;color:#1a3a5c;letter-spacing:.08em}
.done-list{text-align:left;margin-top:.8rem}
.done-list li{padding:.35rem 0;color:#333;list-style:none;border-bottom:1px solid #d0dce8;font-size:.93rem}

/* 상태 배지 */
.status-row{display:flex;justify-content:space-between;align-items:center;
  padding:.75rem 1rem;border-radius:8px;background:#f7fafc;margin-bottom:.4rem;border:1px solid #e0e8f0}
.badge{padding:.25rem .8rem;border-radius:20px;font-size:.82rem;font-weight:bold}
.b-wait{background:#e8eef4;color:#1a3a5c}
.b-work{background:#fff3cd;color:#7a5800}
.b-done{background:#d4edda;color:#155724}
.b-bad{background:#f8d7da;color:#721c24}

/* 입력 필드 */
.input-field{width:100%;padding:.85rem 1rem;border-radius:8px;border:2px solid #dde;
  font-size:1.05rem;outline:none;transition:border-color .15s}
.input-field:focus{border-color:#1a3a5c}

/* 버튼 줄 */
.btn-row{display:flex;gap:.8rem;margin-top:1.4rem}
.btn{flex:1;padding:.85rem;border-radius:8px;border:none;font-size:.97rem;
  font-weight:bold;cursor:pointer;transition:all .12s}
.btn-p{background:#1a3a5c;color:#fff}
.btn-p:hover{background:#14304e}
.btn-p:disabled{background:#aaa;cursor:not-allowed}
.btn-s{background:#e8eef4;color:#1a3a5c}
.btn-s:hover{background:#d0dce8}

@media(max-width:600px){
  .main-banners{flex-direction:column;padding:1.5rem 1rem}
  .banner{padding:3.5rem 1.5rem;font-size:2.6rem}
  .wall-grid{grid-template-columns:1fr}
  .cell{width:52px;height:52px}
  .grid{grid-template-columns:repeat(4,52px)}
  .y-label{height:52px}
  .x-label{width:52px}
}
</style>
</head>
<body>

<!-- 준비중 오버레이 -->
<div id="overlay">
  <div style="font-size:3rem">🏗️</div>
  <h1>준비중입니다</h1>
  <p>조금만 기다려주세요!</p>
</div>

<!-- 헤더 -->
<div class="header">안녕하세요 모듈형 주택 제작사 JW건설입니다.</div>

<!-- 메인 -->
<div id="sc-main" class="screen active">
  <div class="main-banners">
    <div class="banner order" onclick="goOrder()">🏠<br>주문하기</div>
    <div class="banner check" onclick="goCheck()">📋<br>주문확인</div>
  </div>
</div>

<!-- 모듈 수 -->
<div id="sc-count" class="screen">
  <div class="card">
    <h2>몇 개의 모듈을 주문하시겠어요?</h2>
    <div class="count-wrap">
      <button onclick="adj(-1)">−</button>
      <input type="number" id="module-count" value="1" min="1" max="12" readonly>
      <button onclick="adj(1)">+</button>
    </div>
    <div class="btn-row">
      <button class="btn btn-s" onclick="show('main')">← 취소</button>
      <button class="btn btn-p" onclick="beginOrder()">다음 →</button>
    </div>
  </div>
</div>

<!-- 좌표 선택 -->
<div id="sc-coord" class="screen">
  <div class="card">
    <div class="step-info" id="coord-step"></div>
    <h2 id="coord-title">위치를 선택해주세요</h2>
    <div style="margin-bottom:.8rem">
      <div style="font-size:.88rem;color:#666;margin-bottom:.4rem">층수 선택</div>
      <div class="floor-btns">
        <button class="floor-btn" id="fb1" onclick="pickFloor(1)">1층</button>
        <button class="floor-btn" id="fb2" onclick="pickFloor(2)">2층</button>
        <button class="floor-btn" id="fb3" onclick="pickFloor(3)">3층</button>
      </div>
    </div>
    <div class="grid-wrap">
      <div class="x-row" id="x-row"></div>
      <div class="grid-outer">
        <div class="y-labels" id="y-row"></div>
        <div class="grid" id="grid"></div>
      </div>
    </div>
    <div class="legend">
      <div class="leg"><div class="leg-dot" style="background:#fff"></div>비어있음</div>
      <div class="leg"><div class="leg-dot" style="background:#fff9db;border-color:#f5c518"></div>1층 점유</div>
      <div class="leg"><div class="leg-dot" style="background:#ffbfa3;border-color:#e74c3c"></div>2층까지 점유</div>
      <div class="leg"><div class="leg-dot" style="background:#c0392b;border-color:#8e1c14"></div>만실</div>
      <div class="leg"><div class="leg-dot" style="background:#2980b9;border-color:#1a5276"></div>이번 선택</div>
    </div>
    <div class="sel-info" id="sel-info"></div>
    <div class="btn-row">
      <button class="btn btn-s" onclick="coordBack()">← 이전</button>
      <button class="btn btn-p" id="coord-next" disabled onclick="coordNext()">다음 →</button>
    </div>
  </div>
</div>

<!-- 벽 색상 -->
<div id="sc-color" class="screen">
  <div class="card">
    <div class="step-info" id="color-step"></div>
    <h2 id="color-title">벽 색상을 선택해주세요</h2>
    <div class="wall-grid" id="wall-grid"></div>
    <div class="btn-row">
      <button class="btn btn-s" onclick="colorBack()">← 이전</button>
      <button class="btn btn-p" id="color-next" disabled onclick="colorNext()">
        <span id="color-next-label">다음 →</span>
      </button>
    </div>
  </div>
</div>

<!-- 최종 확인 -->
<div id="sc-confirm" class="screen">
  <div class="card">
    <h2>주문 내역 확인</h2>
    <div id="confirm-list"></div>
    <div class="btn-row">
      <button class="btn btn-s" onclick="confirmBack()">← 수정</button>
      <button class="btn btn-p" onclick="submitOrder()">주문 완료</button>
    </div>
  </div>
</div>

<!-- 완료 -->
<div id="sc-done" class="screen">
  <div class="card" style="text-align:center">
    <div style="font-size:2.8rem;margin-bottom:.8rem">✅</div>
    <h2 style="margin-bottom:1.2rem">주문이 접수되었습니다!</h2>
    <div id="done-box" class="done-box"></div>
    <button class="btn btn-p" style="margin-top:1.5rem" onclick="show('main')">처음으로</button>
  </div>
</div>

<!-- 주문 확인 -->
<div id="sc-check" class="screen">
  <div class="card">
    <h2>주문 확인</h2>
    <p style="color:#777;margin-bottom:1rem;font-size:.93rem">주문세트번호 또는 주문번호를 입력해주세요</p>
    <input type="number" id="check-input" class="input-field" placeholder="번호 입력">
    <div class="btn-row">
      <button class="btn btn-s" onclick="show('main')">← 뒤로</button>
      <button class="btn btn-p" onclick="doLookup()">확인</button>
    </div>
    <div id="check-result" style="margin-top:1.5rem"></div>
  </div>
</div>

<script>
// ── 상태 ──────────────────────────────────────────
let total = 1, mods = [], idx = 0, selCoord = null, selFloor = 1, dbOcc = [];

const CN = {P:'분홍', W:'하양', Y:'노랑'};

// ── 서버 체크 ──────────────────────────────────────
async function chkServer(){
  try{
    const r = await fetch('/api/status');
    const d = await r.json();
    document.getElementById('overlay').style.display = d.running ? 'none' : 'flex';
  }catch(e){ document.getElementById('overlay').style.display='flex'; }
}
chkServer();
setInterval(chkServer, 5000);

// ── 화면 전환 ──────────────────────────────────────
function show(id){ document.querySelectorAll('.screen').forEach(s=>s.classList.remove('active')); document.getElementById('sc-'+id).classList.add('active'); }
function goOrder(){ show('count'); }
function goCheck(){ show('check'); document.getElementById('check-result').innerHTML=''; }

// ── 수량 조절 ──────────────────────────────────────
function adj(d){ const el=document.getElementById('module-count'); let v=parseInt(el.value)+d; if(v<1)v=1; if(v>12)v=12; el.value=v; }

// ── 주문 시작 ──────────────────────────────────────
async function beginOrder(){
  total = parseInt(document.getElementById('module-count').value);
  mods = Array.from({length:total},()=>({x:null,y:null,z:null,walls:{top:null,bottom:null,left:null,right:null}}));
  idx = 0;
  try{ const r=await fetch('/api/grid'); dbOcc=await r.json(); }catch(e){ dbOcc=[]; }
  showCoord();
}

// ── 좌표 선택 ──────────────────────────────────────
function showCoord(){
  selCoord=null; selFloor=1;
  document.getElementById('coord-step').textContent=`모듈 ${idx+1} / ${total}`;
  document.getElementById('coord-title').textContent=`${idx+1}번 모듈의 위치를 선택해주세요`;
  document.getElementById('coord-next').disabled=true;
  document.getElementById('sel-info').textContent='';
  updFloor(); renderGrid();
  show('coord');
}

function maxZ(x,y){
  let m=0;
  for(const o of dbOcc) if(o.x===x&&o.y===y&&o.z>m) m=o.z;
  for(let i=0;i<idx;i++){const mo=mods[i]; if(mo.x===x&&mo.y===y&&mo.z>m) m=mo.z;}
  return m;
}

function avail(x,y,z){ const mz=maxZ(x,y); return z===mz+1; }

function updFloor(){
  for(let z=1;z<=3;z++){
    const b=document.getElementById('fb'+z);
    b.classList.toggle('active',z===selFloor);
    b.disabled=false;
  }
}

function pickFloor(z){ selFloor=z; selCoord=null; document.getElementById('coord-next').disabled=true; document.getElementById('sel-info').textContent=''; updFloor(); renderGrid(); }

function renderGrid(){
  // X labels
  let xh=''; for(let x=1;x<=4;x++) xh+=`<div class="x-label">X${x}</div>`;
  document.getElementById('x-row').innerHTML=xh;
  // Y labels
  let yh=''; for(let y=4;y>=1;y--) yh+=`<div class="y-label">Y${y}</div>`;
  document.getElementById('y-row').innerHTML=yh;
  // Cells
  let gh='';
  for(let y=4;y>=1;y--){
    for(let x=1;x<=4;x++){
      const mz=maxZ(x,y);
      const isSel=selCoord&&selCoord.x===x&&selCoord.y===y;
      const ok=avail(x,y,selFloor);
      let cls='cell ';
      let txt='';
      if(isSel){ cls+='picked'; txt='✓'; }
      else if(mz>=3){ cls+='f3 dim no-hover'; txt='만실'; }
      else if(mz===2){ cls+='f2'+(ok?'':' dim no-hover'); txt='2층'; }
      else if(mz===1){ cls+='f1'+(ok?'':' dim no-hover'); txt='1층'; }
      else{ cls+='empty'+(ok?'':' dim no-hover'); }
      const oc=isSel?`desel()`:(ok?`sel(${x},${y})`:'');
      gh+=`<div class="${cls}" onclick="${oc}" title="X${x} Y${y}">${txt}</div>`;
    }
  }
  document.getElementById('grid').innerHTML=gh;
}

function sel(x,y){ selCoord={x,y,z:selFloor}; document.getElementById('coord-next').disabled=false; document.getElementById('sel-info').textContent=`선택됨 — X${x}, Y${y}, ${selFloor}층`; renderGrid(); }
function desel(){ selCoord=null; document.getElementById('coord-next').disabled=true; document.getElementById('sel-info').textContent=''; renderGrid(); }

function coordBack(){ if(idx===0){ show('count'); }else{ idx--; showColor(); } }
function coordNext(){ if(!selCoord) return; mods[idx].x=selCoord.x; mods[idx].y=selCoord.y; mods[idx].z=selCoord.z; showColor(); }

// ── 색상 선택 ──────────────────────────────────────
function showColor(){
  const m=mods[idx];
  document.getElementById('color-step').textContent=`모듈 ${idx+1} / ${total}`;
  document.getElementById('color-title').textContent=`${idx+1}번 모듈 (${m.x}, ${m.y}, ${m.z}층) 벽 색상 선택`;
  const isLast=(idx===total-1);
  document.getElementById('color-next-label').textContent=isLast?'확인 →':'다음 →';

  const dirs=[['top','상 (윗벽)'],['bottom','하 (아랫벽)'],['left','좌 (왼쪽벽)'],['right','우 (오른쪽벽)']];
  let html='';
  for(const [d,label] of dirs){
    html+=`<div class="wall-item"><label>${label}</label><div class="color-btns">`;
    for(const [code,name,cls] of [['P','분홍','pink'],['W','하양','white'],['Y','노랑','yellow']]){
      const on=m.walls[d]===code?' on':'';
      html+=`<button class="cbtn ${cls}${on}" onclick="pickColor('${d}','${code}',this)">${name}</button>`;
    }
    html+=`</div></div>`;
  }
  document.getElementById('wall-grid').innerHTML=html;
  chkColor();
  show('color');
}

function pickColor(dir,code,btn){
  btn.closest('.color-btns').querySelectorAll('.cbtn').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  mods[idx].walls[dir]=code;
  chkColor();
}

function chkColor(){ const w=mods[idx].walls; document.getElementById('color-next').disabled=!(w.top&&w.bottom&&w.left&&w.right); }

function colorBack(){ showCoord(); }
function colorNext(){ if(idx+1<total){ idx++; showCoord(); }else{ showConfirm(); } }

// ── 최종 확인 ──────────────────────────────────────
function showConfirm(){
  let html='';
  mods.forEach((m,i)=>{
    html+=`<div class="mod-item"><div class="mod-title">모듈 ${i+1} — (${m.x}, ${m.y}, ${m.z}층)</div>
    <div class="mod-detail">상: <b>${CN[m.walls.top]}</b> &nbsp;|&nbsp; 하: <b>${CN[m.walls.bottom]}</b> &nbsp;|&nbsp; 좌: <b>${CN[m.walls.left]}</b> &nbsp;|&nbsp; 우: <b>${CN[m.walls.right]}</b></div></div>`;
  });
  document.getElementById('confirm-list').innerHTML=html;
  show('confirm');
}
function confirmBack(){ idx=total-1; showColor(); }

async function submitOrder(){
  const payload={modules:mods.map(m=>({x:m.x,y:m.y,z:m.z,wall_top:m.walls.top,wall_bottom:m.walls.bottom,wall_left:m.walls.left,wall_right:m.walls.right}))};
  try{
    const r=await fetch('/api/order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d=await r.json();
    if(!r.ok){alert(d.error||'주문 실패'); return;}
    showDone(d.order_ids,d.set_id);
  }catch(e){ alert('서버 오류가 발생했습니다'); }
}

// ── 완료 ──────────────────────────────────────────
function showDone(ids,setId){
  let html='';
  if(setId&&ids.length>1){
    html+=`<div class="set-label">주문세트번호</div><div class="done-num">${String(setId).padStart(4,'0')}</div>`;
    html+=`<div style="font-size:.82rem;color:#666;margin:.5rem 0">(주문현황을 한꺼번에 보고싶으시다면 세트번호를 이용하세요)</div>`;
    html+=`<ul class="done-list">`;
    ids.forEach((id,i)=>{ html+=`<li>모듈${i+1} 주문번호 : <b>${String(id).padStart(4,'0')}</b></li>`; });
    html+=`</ul>`;
  }else{
    html+=`<div class="set-label">주문번호</div><div class="done-num">${String(ids[0]).padStart(4,'0')}</div>`;
  }
  document.getElementById('done-box').innerHTML=html;
  show('done');
}

// ── 주문 확인 ──────────────────────────────────────
async function doLookup(){
  const val=document.getElementById('check-input').value.trim();
  if(!val) return;
  const el=document.getElementById('check-result');
  el.innerHTML='<div style="color:#888;text-align:center;padding:.8rem">조회중...</div>';
  try{
    const r=await fetch('/api/lookup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({number:val})});
    const d=await r.json();
    if(!r.ok){el.innerHTML=`<div style="color:#c0392b;text-align:center;padding:.8rem">${d.error}</div>`;return;}
    let html='';
    if(d.type==='set'){
      html+=`<div style="font-weight:bold;margin-bottom:.7rem;color:#1a3a5c">주문세트 #${String(d.set_id).padStart(4,'0')}</div>`;
      d.orders.forEach((o,i)=>{ html+=badge(`모듈${i+1}  (주문번호 ${String(o.order_id).padStart(4,'0')})`,o.display); });
    }else{
      html+=badge(`주문번호 ${String(d.order_id).padStart(4,'0')}`,d.display);
    }
    el.innerHTML=html;
  }catch(e){el.innerHTML='<div style="color:#c0392b;text-align:center">조회 실패</div>';}
}

function badge(label,status){
  let cls='b-wait';
  if(status==='완료 ✓') cls='b-done';
  else if(['불량 폐기','불량 처리중'].includes(status)) cls='b-bad';
  else if(status!=='대기중') cls='b-work';
  return `<div class="status-row"><span>${label}</span><span class="badge ${cls}">${status}</span></div>`;
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    db.init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
