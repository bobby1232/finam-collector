DASHBOARD_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>RI Labs · Market Data</title>
  <script src="https://unpkg.com/lightweight-charts@4.2.2/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    :root { color-scheme: dark; --bg:#0b0e14; --panel:#121722; --line:#242c3b; --muted:#8792a6; --text:#eef3fb; --accent:#7aa2ff; }
    * { box-sizing:border-box }
    body { margin:0; background:var(--bg); color:var(--text); font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif; }
    .wrap { max-width:1500px; margin:auto; padding:20px; }
    .top { display:flex; align-items:flex-start; justify-content:space-between; gap:18px; margin-bottom:14px; }
    h1 { font-size:22px; margin:0 0 5px; font-weight:650; letter-spacing:-.02em; }
    .sub { color:var(--muted); font-size:13px; }
    .status { padding:7px 10px; border:1px solid var(--line); border-radius:10px; font-size:12px; color:var(--muted); }
    .controls { display:flex; gap:8px; flex-wrap:wrap; padding:12px; background:var(--panel); border:1px solid var(--line); border-radius:14px; }
    select,button { background:#0d121b; color:var(--text); border:1px solid var(--line); border-radius:9px; padding:8px 10px; font-size:13px; cursor:pointer; }
    button.active { border-color:var(--accent); color:#fff; }
    .kpis { display:grid; grid-template-columns:repeat(5,1fr); gap:10px; margin:12px 0; }
    .kpi { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:12px; min-width:0; }
    .kpi .label { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.05em; }
    .kpi .value { font-size:18px; margin-top:5px; overflow:hidden; text-overflow:ellipsis; }
    .chartbox { background:var(--panel); border:1px solid var(--line); border-radius:14px; overflow:hidden; }
    #chart { height:610px; }
    .foot { color:var(--muted); font-size:11px; margin-top:10px; display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; }
    @media(max-width:800px){ .kpis{grid-template-columns:repeat(2,1fr)} #chart{height:500px}.top{flex-direction:column} }
  </style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div><h1>Market Data</h1><div class="sub">FINAM live + MOEX historical backfill · PostgreSQL</div></div>
    <div id="status" class="status">Подключение…</div>
  </div>

  <div class="controls">
    <select id="symbol"></select>
    <select id="interval">
      <option value="1m">1 мин</option><option value="5m">5 мин</option>
      <option value="15m">15 мин</option><option value="1h">1 час</option>
      <option value="4h">4 часа</option><option value="1d">1 день</option>
    </select>
    <button data-days="1">1 день</button>
    <button data-days="7" class="active">7 дней</button>
    <button data-days="30">1 месяц</button>
    <button data-days="0">С июня</button>
  </div>

  <div class="kpis">
    <div class="kpi"><div class="label">Последняя цена</div><div class="value" id="last">—</div></div>
    <div class="kpi"><div class="label">Изменение периода</div><div class="value" id="change">—</div></div>
    <div class="kpi"><div class="label">Максимум</div><div class="value" id="high">—</div></div>
    <div class="kpi"><div class="label">Минимум</div><div class="value" id="low">—</div></div>
    <div class="kpi"><div class="label">Объём</div><div class="value" id="volume">—</div></div>
  </div>

  <div class="chartbox"><div id="chart"></div></div>
  <div class="foot"><span>Автообновление: 30 сек.</span><span id="meta"></span></div>
</div>

<script>
const chartEl = document.getElementById('chart');
const chart = LightweightCharts.createChart(chartEl, {
  layout:{background:{color:'#121722'},textColor:'#8792a6'},
  grid:{vertLines:{color:'#1b2230'},horzLines:{color:'#1b2230'}},
  rightPriceScale:{borderColor:'#242c3b'},
  timeScale:{borderColor:'#242c3b',timeVisible:true,secondsVisible:false},
  crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
});
const candle = chart.addCandlestickSeries({
  upColor:'#36c98f', downColor:'#f05d6f', borderVisible:false,
  wickUpColor:'#36c98f', wickDownColor:'#f05d6f',
  priceFormat:{type:'price',precision:2,minMove:0.01}
});
const volume = chart.addHistogramSeries({
  priceFormat:{type:'volume'}, priceScaleId:'vol',
});
chart.priceScale('vol').applyOptions({scaleMargins:{top:0.78,bottom:0}});
new ResizeObserver(() => chart.applyOptions({width:chartEl.clientWidth})).observe(chartEl);

let days = 7;
const symbolEl = document.getElementById('symbol');
const intervalEl = document.getElementById('interval');

function fmt(v){ return v == null ? '—' : Number(v).toLocaleString('ru-RU',{maximumFractionDigits:4}); }

async function loadInstruments(){
  const r = await fetch('/api/instruments'); const j = await r.json();
  symbolEl.innerHTML = j.instruments.map(x=>`<option value="${x.symbol}">${x.symbol.replace('@MISX','').replace('@RTSX','')}</option>`).join('');
}

async function loadChart(fit=false){
  const symbol = symbolEl.value, interval = intervalEl.value;
  document.getElementById('status').textContent='Обновление…';
  const r = await fetch(`/api/chart?symbol=${encodeURIComponent(symbol)}&interval=${interval}&days=${days}`);
  const j = await r.json();
  const bars = j.bars || [];
  candle.setData(bars.map(x=>({time:x.time,open:x.open,high:x.high,low:x.low,close:x.close})));
  volume.setData(bars.map(x=>({time:x.time,value:x.volume||0,color:x.close>=x.open?'rgba(54,201,143,.38)':'rgba(240,93,111,.38)'})));
  if (fit) chart.timeScale().fitContent();

  if(bars.length){
    const first=bars[0], last=bars[bars.length-1];
    const highs=bars.map(x=>x.high), lows=bars.map(x=>x.low);
    const totalVol=bars.reduce((a,x)=>a+(Number(x.volume)||0),0);
    const ch=(last.close/first.open-1)*100;
    document.getElementById('last').textContent=fmt(last.close);
    document.getElementById('change').textContent=(ch>=0?'+':'')+ch.toFixed(2)+'%';
    document.getElementById('high').textContent=fmt(Math.max(...highs));
    document.getElementById('low').textContent=fmt(Math.min(...lows));
    document.getElementById('volume').textContent=fmt(totalVol);
  }
  document.getElementById('meta').textContent=`${symbol} · ${interval} · ${bars.length.toLocaleString('ru-RU')} свечей · ${j.source_note||''}`;
  document.getElementById('status').textContent='● online';
}

document.querySelectorAll('button[data-days]').forEach(btn=>btn.onclick=()=>{
  document.querySelectorAll('button[data-days]').forEach(x=>x.classList.remove('active'));
  btn.classList.add('active'); days=Number(btn.dataset.days);
  if(days===0 && intervalEl.value==='1m') intervalEl.value='15m';
  loadChart(true);
});
symbolEl.onchange=()=>loadChart(true);
intervalEl.onchange=()=>loadChart(true);

(async()=>{ await loadInstruments(); await loadChart(true); setInterval(()=>loadChart(false),30000); })();
</script>
</body>
</html>"""
