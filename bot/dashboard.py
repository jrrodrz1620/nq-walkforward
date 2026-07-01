"""
Self-contained web dashboard (no build step, vanilla JS).

Served at ``/dashboard?token=...``; it polls ``/api/state?token=...`` every few
seconds and renders positions, PnL, recent orders and transactions.
"""

DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Futures Bot</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
       background:#0e1117;color:#e6edf3}
  header{padding:14px 18px;background:#161b22;border-bottom:1px solid #30363d;
         display:flex;align-items:center;justify-content:space-between}
  header h1{font-size:16px;margin:0;font-weight:600}
  #dot{font-size:12px;color:#8b949e}
  .wrap{padding:16px;max-width:900px;margin:0 auto}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
  .card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px}
  .card .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#8b949e}
  .card .val{font-size:22px;font-weight:700;margin-top:4px}
  h2{font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:#8b949e;margin:18px 0 8px}
  table{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:10px;overflow:hidden}
  th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #21262d;font-size:13px;white-space:nowrap}
  th{color:#8b949e;font-weight:600;font-size:11px;text-transform:uppercase}
  tr:last-child td{border-bottom:none}
  .pos{color:#3fb950}.neg{color:#f85149}.muted{color:#8b949e}
  .pill{padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600}
  .filled{background:#1a3a24;color:#3fb950}.rejected{background:#3a2a1a;color:#d29922}
  .failed{background:#3a1a1a;color:#f85149}.processing{background:#1a2a3a;color:#58a6ff}
  .scroll{overflow-x:auto}
  .err{color:#f85149;padding:20px;text-align:center}
</style></head>
<body>
<header><h1>⚡ Futures Bot</h1><span id="dot">connecting…</span></header>
<div class="wrap">
  <div class="cards" id="cards"></div>
  <h2>Open positions</h2><div class="scroll"><table id="pos"><tbody></tbody></table></div>
  <h2>Recent orders</h2><div class="scroll"><table id="ord"><tbody></tbody></table></div>
</div>
<script>
const token = new URLSearchParams(location.search).get('token') || '';
const $ = id => document.getElementById(id);
const money = n => (n>=0?'+':'') + '$' + n.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const cls = n => n>0?'pos':n<0?'neg':'muted';

async function tick(){
  try{
    const r = await fetch('/api/state?token='+encodeURIComponent(token));
    if(!r.ok){ $('dot').textContent='unauthorized — check token'; return; }
    const d = await r.json();
    $('dot').textContent = 'updated ' + new Date().toLocaleTimeString();

    $('cards').innerHTML = [
      ['Day PnL (MtM)', money(d.day_pnl_mtm), cls(d.day_pnl_mtm)],
      ['Realized today', money(d.realized_pnl_today), cls(d.realized_pnl_today)],
      ['Open positions', d.positions.length, ''],
      ['Broker', d.broker_type, 'muted'],
    ].map(([l,v,c])=>`<div class="card"><div class="lbl">${l}</div><div class="val ${c}">${v}</div></div>`).join('');

    $('pos').innerHTML = '<thead><tr><th>Symbol</th><th>Net</th><th>Avg</th><th>Mark</th><th>Unrealized</th></tr></thead><tbody>' +
      (d.positions.length ? d.positions.map(p=>{
        const u = (p.last_mark-p.avg_price)*p.net_contracts*p.multiplier;
        return `<tr><td>${p.symbol} <span class="muted">${p.contract_month}</span></td>
          <td class="${cls(p.net_contracts)}">${p.net_contracts>0?'+':''}${p.net_contracts}</td>
          <td>${p.avg_price}</td><td>${p.last_mark}</td><td class="${cls(u)}">${money(u)}</td></tr>`;
      }).join('') : '<tr><td colspan="5" class="muted">flat — no open positions</td></tr>') + '</tbody>';

    $('ord').innerHTML = '<thead><tr><th>Time</th><th>Order</th><th>Status</th><th>Fill</th><th>Note</th></tr></thead><tbody>' +
      (d.orders.length ? d.orders.map(o=>`<tr>
          <td class="muted">${(o.created_at||'').replace('T',' ').slice(5,19)}</td>
          <td>${o.action.toUpperCase()} ${o.quantity} ${o.symbol}</td>
          <td><span class="pill ${o.status}">${o.status}</span></td>
          <td>${o.fill_price??'—'}</td>
          <td class="muted">${o.reason?o.reason.slice(0,42):''}</td></tr>`).join('')
        : '<tr><td colspan="5" class="muted">no orders yet</td></tr>') + '</tbody>';
  }catch(e){ $('dot').textContent='connection error'; }
}
tick(); setInterval(tick, 5000);
</script></body></html>"""
