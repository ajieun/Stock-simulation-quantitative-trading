"""浏览器控制台的单页界面（无构建步骤，纯原生 JS）。"""

UI_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A 股 AI 量化交易控制台</title>
<style>
  :root{
    --bg:#0b0f14; --panel:#131920; --panel2:#1a222c; --border:#232d3a;
    --text:#d7e0ea; --muted:#7d8ea3; --green:#2ea043; --red:#f85149;
    --blue:#4b9cf5; --amber:#d29922; --purple:#a371f7;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-size:14px;
       font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  ::-webkit-scrollbar{width:9px;height:9px}
  ::-webkit-scrollbar-thumb{background:#2c3846;border-radius:5px}

  .topbar{display:flex;align-items:center;gap:14px;padding:0 20px;height:56px;
          background:var(--panel);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:20}
  .brand{font-weight:600;font-size:15px;letter-spacing:.3px}
  .dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px;vertical-align:middle}
  .pill{font-size:12px;padding:3px 10px;border-radius:20px;border:1px solid var(--border);color:var(--muted)}
  .pill.blue{color:var(--blue);border-color:#4b9cf540;background:#4b9cf514}
  .pill.green{color:var(--green);border-color:#2ea04340;background:#2ea04314}
  .pill.red{color:var(--red);border-color:#f8514940;background:#f8514914}
  .pill.amber{color:var(--amber);border-color:#d2992240;background:#d2992214}
  .spacer{flex:1}
  button{font-family:inherit;font-size:13px;padding:7px 14px;border-radius:6px;cursor:pointer;
         border:1px solid var(--border);background:var(--panel2);color:var(--text);transition:.15s}
  button:hover{background:#222d3a;border-color:#31404f}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.primary{background:#1f6feb;border-color:#1f6feb;color:#fff}
  button.primary:hover{background:#2f7ff5}
  button.danger{background:#da3633;border-color:#da3633;color:#fff}
  button.danger:hover{background:#e74c48}
  button.ghost{background:transparent}
  button.sm{padding:4px 10px;font-size:12px}

  .wrap{max-width:1440px;margin:0 auto;padding:18px 20px 60px}
  .tabs{display:flex;gap:4px;margin-bottom:18px;border-bottom:1px solid var(--border)}
  .tab{padding:10px 16px;cursor:pointer;color:var(--muted);font-size:13px;
       border-bottom:2px solid transparent;transition:.15s}
  .tab:hover{color:var(--text)}
  .tab.active{color:var(--blue);border-bottom-color:var(--blue)}

  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:18px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px 16px}
  .card .label{color:var(--muted);font-size:12px;margin-bottom:8px}
  .card .value{font-size:21px;font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-.3px}
  .card .sub{font-size:12px;color:var(--muted);margin-top:5px}

  .panel{background:var(--panel);border:1px solid var(--border);border-radius:8px;
         margin-bottom:18px;overflow:hidden}
  .panel-head{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border)}
  .panel-head h2{font-size:13px;font-weight:600;color:var(--muted);letter-spacing:.4px;text-transform:uppercase}
  .panel-body{padding:0}
  .panel-body.pad{padding:16px}

  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
  th,td{padding:9px 14px;text-align:left;border-bottom:1px solid var(--border);white-space:nowrap}
  th{background:#101720;color:var(--muted);font-size:12px;font-weight:500;position:sticky;top:0}
  tbody tr:last-child td{border-bottom:none}
  tbody tr:hover{background:#161e27}
  .pos{color:var(--green)} .neg{color:var(--red)} .muted{color:var(--muted)}
  .num{font-variant-numeric:tabular-nums}
  .scroll{max-height:440px;overflow:auto}

  .tag{padding:2px 8px;border-radius:4px;font-size:12px}
  .tag-buy{background:#2ea04320;color:var(--green)}
  .tag-sell{background:#f8514920;color:var(--red)}
  .tag-hold{background:#7d8ea320;color:var(--muted)}
  .tag-pending{background:#d2992220;color:var(--amber)}
  .tag-ok{background:#2ea04320;color:var(--green)}
  .tag-bad{background:#f8514920;color:var(--red)}

  .pending{border:1px solid #d2992250;background:#d2992210}
  .pending .panel-head{border-bottom-color:#d2992230}
  .order-card{padding:12px 16px;border-bottom:1px solid var(--border);
              display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .order-card:last-child{border-bottom:none}
  .order-main{font-size:15px;font-weight:600;min-width:230px}
  .order-meta{color:var(--muted);font-size:12px;flex:1;min-width:200px}
  .copy{font-family:Consolas,monospace;background:#0b0f14;border:1px solid var(--border);
        padding:4px 8px;border-radius:4px;font-size:12px;color:var(--amber)}

  .empty{padding:34px;text-align:center;color:var(--muted)}
  .logs{font-family:Consolas,"Courier New",monospace;font-size:12.5px;line-height:1.7;
        padding:10px 14px;height:520px;overflow-y:auto;background:#080c11}
  .logs .t{color:#55657a}
  .logs .INFO{color:#8fb6d9} .logs .WARNING{color:var(--amber)}
  .logs .ERROR{color:var(--red)} .logs .DEBUG{color:#5d6c7d}
  textarea{width:100%;height:560px;background:#080c11;color:var(--text);border:1px solid var(--border);
           border-radius:6px;padding:14px;font-family:Consolas,monospace;font-size:12.5px;
           line-height:1.6;resize:vertical}
  .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  .hint{color:var(--muted);font-size:12px}
  .toast{position:fixed;right:20px;bottom:20px;padding:12px 18px;border-radius:8px;
         background:#1f6feb;color:#fff;font-size:13px;opacity:0;transform:translateY(10px);
         transition:.25s;pointer-events:none;max-width:420px;z-index:100}
  .toast.show{opacity:1;transform:translateY(0)}
  .toast.err{background:#da3633}
  .toast.ok{background:#238636}
  .warnbox{background:#d2992210;border:1px solid #d2992240;border-radius:6px;
           padding:10px 14px;color:var(--amber);font-size:12.5px;margin-bottom:14px;line-height:1.7}
  .scan-row.held{background:#4b9cf50a}
</style>
</head>
<body>

<div class="topbar">
  <span class="brand">A 股 AI 量化交易控制台</span>
  <span class="pill" id="modePill">加载中</span>
  <span class="pill" id="enginePill"><span class="dot" style="background:#7d8ea3"></span>未运行</span>
  <span class="spacer"></span>
  <label class="hint" title="实盘启动前必须勾选">
    <input type="checkbox" id="riskAck"> 已知晓实盘风险
  </label>
  <button id="btnScan" onclick="runScan()">立即扫描</button>
  <button id="btnStart" class="primary" onclick="startEngine()">启动引擎</button>
  <button id="btnStop" class="danger" onclick="stopEngine()" disabled>停止</button>
</div>

<div class="wrap">
  <div class="tabs">
    <div class="tab active" data-tab="overview" onclick="switchTab('overview')">总览</div>
    <div class="tab" data-tab="decisions" onclick="switchTab('decisions')">AI 决策</div>
    <div class="tab" data-tab="orders" onclick="switchTab('orders')">成交记录</div>
    <div class="tab" data-tab="logs" onclick="switchTab('logs')">运行日志</div>
    <div class="tab" data-tab="config" onclick="switchTab('config')">配置</div>
  </div>

  <div id="tab-overview">
    <div class="cards" id="cards"></div>
    <div id="pendingBox"></div>
    <div class="panel">
      <div class="panel-head"><h2>当前持仓</h2>
        <span class="spacer" style="flex:1"></span>
        <span class="hint" id="posHint"></span>
      </div>
      <div class="panel-body" id="positions"></div>
    </div>
    <div class="panel">
      <div class="panel-head"><h2>扫描结果</h2><span class="hint" id="scanHint">点击右上角「立即扫描」</span></div>
      <div class="panel-body scroll" id="scanResult"><div class="empty">尚未扫描</div></div>
    </div>
  </div>

  <div id="tab-decisions" style="display:none">
    <div class="panel"><div class="panel-head"><h2>AI 决策记录</h2></div>
      <div class="panel-body scroll" id="decisions"></div></div>
  </div>

  <div id="tab-orders" style="display:none">
    <div class="panel"><div class="panel-head"><h2>委托与成交</h2></div>
      <div class="panel-body scroll" id="orders"></div></div>
  </div>

  <div id="tab-logs" style="display:none">
    <div class="panel">
      <div class="panel-head"><h2>运行日志</h2>
        <span class="spacer" style="flex:1"></span>
        <button class="sm ghost" onclick="S.logSeq=0;document.getElementById('logBox').innerHTML=''">清屏</button>
      </div>
      <div class="logs" id="logBox"></div>
    </div>
  </div>

  <div id="tab-config" style="display:none">
    <div class="warnbox">
      修改后点击「保存」会先做语法与结构校验，通过才写入 config.yaml。改动需要重启引擎才生效。
    </div>
    <div class="panel">
      <div class="panel-head"><h2>config.yaml</h2>
        <span class="spacer" style="flex:1"></span>
        <button class="ghost" onclick="loadConfig()">重新读取</button>
        <button class="primary" onclick="saveConfig()">保存</button>
      </div>
      <div class="panel-body pad"><textarea id="cfgText" spellcheck="false"></textarea></div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const S = { state:null, logSeq:0, tab:'overview' };
const money = v => (v==null||isNaN(v)) ? '-' : Number(v).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const num = (v,d=2) => (v==null||isNaN(v)) ? '-' : Number(v).toFixed(d);
const sign = v => (v>0?'+':'');
const cls = v => v>0?'pos':(v<0?'neg':'muted');
const esc = s => String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const sideTag = s => `<span class="tag ${s==='BUY'?'tag-buy':'tag-sell'}">${s==='BUY'?'买入':'卖出'}</span>`;

function toast(msg, kind){
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + (kind||'');
  clearTimeout(el._t);
  el._t = setTimeout(()=>{ el.className='toast '+ (kind||''); }, 3200);
}

async function api(path, opts){
  const res = await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts));
  let data = null;
  try { data = await res.json(); } catch(e) {}
  if(!res.ok){
    throw new Error((data && (data.detail||data.error)) || ('HTTP '+res.status));
  }
  return data;
}

function switchTab(name){
  S.tab = name;
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active', t.dataset.tab===name));
  ['overview','decisions','orders','logs','config'].forEach(k=>{
    document.getElementById('tab-'+k).style.display = (k===name) ? '' : 'none';
  });
  if(name==='logs') scrollLogs();
  if(name==='config' && !document.getElementById('cfgText').value) loadConfig();
}

/* ---------------------------- 状态刷新 ---------------------------- */
async function refresh(){
  try{
    S.state = await api('/api/state');
    render();
  }catch(e){
    document.getElementById('enginePill').innerHTML =
      '<span class="dot" style="background:#f85149"></span>与服务断开';
  }
}

function render(){
  const d = S.state;
  const eng = d.engine;

  document.getElementById('modePill').textContent = d.mode_text;
  document.getElementById('modePill').className = 'pill ' + (d.is_live ? 'red' : 'blue');

  const ep = document.getElementById('enginePill');
  const dot = eng.running ? 'green' : (eng.state==='error' ? 'red' : '#7d8ea3');
  const label = eng.running ? ('运行中 ' + eng.mode) : (eng.state==='error' ? '异常' : '未运行');
  ep.innerHTML = `<span class="dot" style="background:${dot}"></span>${label}`;
  ep.className = 'pill ' + (eng.running ? 'green' : (eng.state==='error'?'red':''));

  document.getElementById('btnStart').disabled = eng.running;
  document.getElementById('btnStop').disabled = !eng.running;

  const pnl = d.day_pnl_pct;
  document.getElementById('cards').innerHTML = `
    <div class="card"><div class="label">总资产</div>
      <div class="value">${money(d.total_asset)}</div>
      <div class="sub">${d.is_live?'实盘账户':'模拟账户'}</div></div>
    <div class="card"><div class="label">当日盈亏</div>
      <div class="value ${cls(pnl)}">${sign(pnl)}${num(pnl)}%</div>
      <div class="sub">基准 ${money(d.day_start)}</div></div>
    <div class="card"><div class="label">可用资金</div>
      <div class="value">${money(d.cash)}</div>
      <div class="sub">仓位 ${num(d.exposure_pct,1)}%</div></div>
    <div class="card"><div class="label">持仓市值</div>
      <div class="value">${money(d.market_value)}</div>
      <div class="sub">${d.positions.length} 只标的</div></div>
    <div class="card"><div class="label">AI 调用（今日）</div>
      <div class="value">${d.ai_calls}</div>
      <div class="sub">${d.ai_tokens.toLocaleString()} tokens</div></div>
    <div class="card"><div class="label">待执行委托</div>
      <div class="value ${d.pending.length?'':'muted'}">${d.pending.length}</div>
      <div class="sub">${d.is_manual?'半自动模式':'自动执行'}</div></div>`;

  renderPending(d.pending);
  renderPositions(d.positions);
  renderDecisions(d.decisions);
  renderOrders(d.orders);
}

function renderPending(list){
  const box = document.getElementById('pendingBox');
  if(!list.length){ box.innerHTML=''; return; }
  box.innerHTML = `<div class="panel pending">
    <div class="panel-head"><h2 style="color:#d29922">待人工执行（请在券商客户端下单后再回填）</h2></div>
    <div class="panel-body">${list.map(o=>`
      <div class="order-card">
        <div class="order-main">${sideTag(o.side)} ${esc(o.symbol)} ${esc(o.name)}
          <span class="copy">${o.qty} 股 @ ${num(o.price)}</span></div>
        <div class="order-meta">${esc(o.message||'')}<br>金额 ${money(o.amount)} · ${esc((o.ts||'').slice(11,19))}</div>
        <div style="display:flex;gap:8px">
          <button class="sm primary" onclick="confirmOrder(${o.id})">已执行，回填</button>
          <button class="sm ghost" onclick="dismissOrder(${o.id})">忽略</button>
        </div>
      </div>`).join('')}</div>
  </div>`;
}

function renderPositions(list){
  const el = document.getElementById('positions');
  document.getElementById('posHint').textContent = list.length ? '' : '空仓';
  if(!list.length){ el.innerHTML='<div class="empty">当前无持仓</div>'; return; }
  el.innerHTML = `<table><thead><tr>
    <th>代码</th><th>名称</th><th>持仓</th><th>可卖</th><th>成本</th>
    <th>现价</th><th>市值</th><th>盈亏</th><th>持有天数</th></tr></thead>
    <tbody>${list.map(p=>`<tr>
      <td>${esc(p.symbol)}</td><td>${esc(p.name)}</td>
      <td class="num">${p.qty}</td><td class="num">${p.available}</td>
      <td class="num">${num(p.avg_cost)}</td><td class="num">${num(p.last_price)}</td>
      <td class="num">${money(p.market_value)}</td>
      <td class="num ${cls(p.pnl_pct)}">${sign(p.pnl_pct)}${num(p.pnl_pct)}%</td>
      <td class="num">${p.holding_days}</td></tr>`).join('')}</tbody></table>`;
}

function renderDecisions(list){
  const el = document.getElementById('decisions');
  if(!list.length){ el.innerHTML='<div class="empty">暂无决策</div>'; return; }
  el.innerHTML = `<table><thead><tr>
    <th>时间</th><th>代码</th><th>名称</th><th>动作</th><th>置信度</th>
    <th>评分</th><th>价格</th><th>执行</th><th>说明</th></tr></thead>
    <tbody>${list.map(x=>`<tr>
      <td class="muted">${esc((x.ts||'').slice(11,19))}</td>
      <td>${esc(x.symbol)}</td><td>${esc(x.name)}</td>
      <td><span class="tag ${x.action==='BUY'?'tag-buy':(x.action==='SELL'?'tag-sell':'tag-hold')}">${esc(x.action)}</span></td>
      <td class="num">${num(x.confidence)}</td>
      <td class="num">${num(x.technical_score)}</td>
      <td class="num">${num(x.price)}</td>
      <td>${x.executed?'<span class="tag tag-ok">已执行</span>':'<span class="muted">未执行</span>'}</td>
      <td class="muted">${esc(x.reject_reason||'')}</td></tr>`).join('')}</tbody></table>`;
}

function renderOrders(list){
  const el = document.getElementById('orders');
  if(!list.length){ el.innerHTML='<div class="empty">暂无记录</div>'; return; }
  el.innerHTML = `<table><thead><tr>
    <th>时间</th><th>代码</th><th>名称</th><th>方向</th><th>价格</th>
    <th>数量</th><th>金额</th><th>费用</th><th>状态</th><th>备注</th></tr></thead>
    <tbody>${list.map(o=>{
      const st = o.status==='FILLED' ? '<span class="tag tag-ok">成交</span>'
        : o.status==='PENDING_MANUAL' ? '<span class="tag tag-pending">待执行</span>'
        : o.status==='DISMISSED' ? '<span class="muted">已忽略</span>'
        : '<span class="tag tag-bad">拒绝</span>';
      return `<tr>
        <td class="muted">${esc((o.ts||'').slice(11,19))}</td>
        <td>${esc(o.symbol)}</td><td>${esc(o.name)}</td>
        <td>${sideTag(o.side)}</td>
        <td class="num">${num(o.price)}</td><td class="num">${o.qty}</td>
        <td class="num">${money(o.amount)}</td><td class="num">${num(o.fee)}</td>
        <td>${st}</td><td class="muted">${esc(o.message||'')}</td></tr>`;
    }).join('')}</tbody></table>`;
}

/* ---------------------------- 日志 ---------------------------- */
async function tickLogs(){
  try{
    const d = await api('/api/logs?after=' + S.logSeq);
    S.logSeq = d.seq;
    if(!d.lines.length) return;
    const box = document.getElementById('logBox');
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    for(const l of d.lines){
      const div = document.createElement('div');
      div.innerHTML = `<span class="t">${l.ts}</span> <span class="${l.level}">${l.level.padEnd(7)}</span> ${esc(l.msg)}`;
      box.appendChild(div);
    }
    while(box.childNodes.length > 600) box.removeChild(box.firstChild);
    if(atBottom) scrollLogs();
  }catch(e){}
}
function scrollLogs(){
  const box = document.getElementById('logBox');
  box.scrollTop = box.scrollHeight;
}

/* ---------------------------- 操作 ---------------------------- */
async function startEngine(){
  const ack = document.getElementById('riskAck').checked;
  if(S.state && S.state.is_live && !ack){
    toast('实盘模式请先勾选「已知晓实盘风险」', 'err');
    return;
  }
  if(S.state && S.state.is_manual && S.state.is_live && !confirm('半自动模式：程序只生成委托，需你在券商客户端手动下单。继续？')){
    return;
  }
  try{
    document.getElementById('btnStart').disabled = true;
    await api('/api/engine/start', {method:'POST', body: JSON.stringify({live_ack: ack})});
    toast('引擎启动中…', 'ok');
    setTimeout(refresh, 1200);
  }catch(e){
    toast('启动失败：' + e.message, 'err');
    document.getElementById('btnStart').disabled = false;
  }
}

async function stopEngine(){
  try{
    await api('/api/engine/stop', {method:'POST'});
    toast('已发送停止指令');
    setTimeout(refresh, 1200);
  }catch(e){ toast('停止失败：' + e.message, 'err'); }
}

async function runScan(){
  const btn = document.getElementById('btnScan');
  btn.disabled = true; btn.textContent = '扫描中…';
  document.getElementById('scanHint').textContent = '正在拉取行情并计算指标…';
  try{
    const d = await api('/api/scan', {method:'POST'});
    if(!d.ok){ throw new Error(d.error||'扫描失败'); }
    document.getElementById('scanHint').textContent =
      `${d.ts} · 总资产 ${money(d.total_asset)} · 交易日 ${d.trading_day?'是':'否'}`;
    const el = document.getElementById('scanResult');
    if(!d.rows.length){ el.innerHTML = '<div class="empty">没有可用数据</div>'; }
    else el.innerHTML = `<table><thead><tr>
      <th>状态</th><th>代码</th><th>名称</th><th>现价</th><th>涨跌</th>
      <th>技术评分</th><th>量比</th><th>RSI</th><th>ATR%</th><th>说明</th></tr></thead>
      <tbody>${d.rows.map(r=>{
        const st = r.held ? '<span class="tag tag-ok">持仓</span>'
          : r.candidate ? '<span class="tag tag-buy">候选</span>'
          : '<span class="muted">观望</span>';
        const note = r.blockers.length ? `<span class="neg">${esc(r.blockers.join('；'))}</span>`
          : `<span class="muted">${esc(r.reasons.slice(0,2).join('；'))}</span>`;
        return `<tr class="scan-row ${r.held?'held':''}">
          <td>${st}</td><td>${esc(r.symbol)}</td><td>${esc(r.name)}</td>
          <td class="num">${num(r.price)}</td>
          <td class="num ${cls(r.pct_change)}">${sign(r.pct_change)}${num(r.pct_change)}%</td>
          <td class="num"><b>${num(r.score)}</b></td>
          <td class="num">${num(r.volume_ratio)}</td>
          <td class="num">${r.rsi==null?'-':num(r.rsi,1)}</td>
          <td class="num">${r.atr_pct==null?'-':num(r.atr_pct,1)}</td>
          <td>${note}</td></tr>`;
      }).join('')}</tbody></table>`;
  }catch(e){
    toast('扫描失败：' + e.message, 'err');
    document.getElementById('scanHint').textContent = '扫描失败';
  }finally{
    btn.disabled = false; btn.textContent = '立即扫描';
  }
}

async function confirmOrder(id){
  if(!confirm('确认已在券商客户端完成该笔委托？将按委托价格回填到本地账户。')) return;
  try{
    const d = await api(`/api/orders/${id}/confirm`, {method:'POST'});
    if(d.success){ toast('已回填', 'ok'); } else { toast('回填失败：' + d.message, 'err'); }
    refresh();
  }catch(e){ toast('回填失败：' + e.message, 'err'); }
}

async function dismissOrder(id){
  try{
    await api(`/api/orders/${id}/dismiss`, {method:'POST'});
    toast('已忽略');
    refresh();
  }catch(e){ toast('操作失败：' + e.message, 'err'); }
}

async function loadConfig(){
  try{
    const d = await api('/api/config');
    document.getElementById('cfgText').value = d.content;
  }catch(e){ toast('读取失败：' + e.message, 'err'); }
}

async function saveConfig(){
  const content = document.getElementById('cfgText').value;
  try{
    await api('/api/config', {method:'PUT', body: JSON.stringify({content})});
    toast('配置已保存，重启引擎后生效', 'ok');
  }catch(e){ toast('保存失败：' + e.message, 'err'); }
}

/* ---------------------------- 启动 ---------------------------- */
switchTab('overview');
refresh();
tickLogs();
setInterval(refresh, 3000);
setInterval(tickLogs, 2000);
</script>
</body>
</html>"""
