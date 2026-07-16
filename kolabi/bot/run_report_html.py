"""Self-contained HTML renderer for Kolabi operator run reports."""

from __future__ import annotations

import html
import json
from datetime import datetime
from decimal import Decimal

from kolabi.bot.run_report import RunReport


def render_html_report(
    report: RunReport,
    *,
    show_pair_regimes: bool = False,
) -> str:
    """Render a local, dependency-free interactive report."""

    payload = _report_payload(report, show_pair_regimes=show_pair_regimes)
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).replace("<", "\\u003c")
    title = html.escape(report.identity.name)
    visibility = "" if show_pair_regimes else " hidden"
    grid_class = "" if show_pair_regimes else " parameters-hidden"
    return (
        _TEMPLATE.replace("__TITLE__", title)
        .replace("__REPORT_DATA__", data)
        .replace("__PARAMETER_VISIBILITY__", visibility)
        .replace("__PARAMETER_GRID_CLASS__", grid_class)
    )


def _report_payload(
    report: RunReport,
    *,
    show_pair_regimes: bool,
) -> dict[str, object]:
    overview = report.financial_overview
    attempts = (
        [
            {
                "pair": outcome.key.name,
                "attempt": outcome.key.attempt,
                "tout": _number(outcome.timeout_minutes),
                "hprice": _number(outcome.head_price_spec),
                "terminal": outcome.terminal,
                "hfill": _time(outcome.head_fill_at),
                "tfill": _time(outcome.tail_fill_at),
                "net": _number(outcome.net_usd),
                "roi": _number(outcome.roi_percent),
            }
            for regime in report.regimes
            for outcome in regime.outcomes
        ]
        if show_pair_regimes
        else []
    )
    terminated = [
        {
            "pair": row.key.name,
            "attempt": row.key.attempt,
            "time": _time(row.tail_fill_at),
            "net": _number(row.net_usd),
            "roi": _number(row.roi_percent),
            "quality": row.finance_quality.value,
            "route": row.route.label if row.route else "unknown",
        }
        for row in report.terminated_rows
    ]
    cumulative = Decimal("0")
    equity: list[dict[str, object]] = []
    for row in report.terminated_rows:
        if row.net_usd is None:
            continue
        cumulative += row.net_usd
        equity.append({"time": _time(row.tail_fill_at), "value": float(cumulative)})
    return {
        "name": report.identity.name,
        "runStarted": _time(report.identity.run_started_at),
        "reportAt": _time(report.report_at),
        "overview": {
            "closed": overview.closed,
            "wins": overview.wins,
            "losses": overview.losses,
            "winRate": _number(overview.win_rate_percent),
            "gross": _number(overview.gross_usd),
            "fees": _number(overview.fees_usd),
            "net": _number(overview.net_usd),
            "profitFactor": _number(overview.profit_factor),
            "avgRoi": _number(overview.average_roi_percent),
            "aggregateRoi": _number(overview.aggregate_roi_percent),
            "roiHour": _number(overview.roi_per_hour_percent),
            "drawdown": _number(overview.max_drawdown_usd),
            "openNotional": _number(overview.open_notional_usd),
            "exact": overview.exact_rows,
            "estimated": overview.estimated_rows,
            "unavailable": overview.unavailable_rows,
        },
        "prices": _prices(report),
        "regimes": (
            [
                {
                    "pair": row.pair_name,
                    "range": (
                        str(row.first_attempt)
                        if row.first_attempt == row.last_attempt
                        else f"{row.first_attempt}-{row.last_attempt}"
                    ),
                    "first": row.first_attempt,
                    "last": row.last_attempt,
                    "tout": _number(row.timeout_minutes),
                    "hprice": _number(row.head_price_spec),
                    "attempts": row.attempts,
                    "timeouts": row.timeouts,
                    "closed": row.closed,
                    "wins": row.wins,
                    "losses": row.losses,
                    "net": _number(row.total_net_usd),
                    "roi": _number(row.average_roi_percent),
                    "final": row.final_state,
                }
                for row in report.regimes
            ]
            if show_pair_regimes
            else []
        ),
        "attempts": attempts,
        "terminated": terminated,
        "equity": equity,
        "volume": [
            {
                "market": row.market,
                "pair": row.pair_name,
                "closed": row.closed,
                "fills": row.fills,
                "turnover": _number(row.bot_usd_volume),
                "net": _number(row.net_usd),
                "life": _number(row.average_life_seconds),
                "roi": _number(row.average_roi_percent),
                "roiHour": _number(row.average_roi_per_hour_percent),
            }
            for row in report.volume_rows
        ],
    }


def _prices(report: RunReport) -> dict[str, object]:
    snapshot = report.market_snapshot
    if snapshot is None:
        return {}
    return {
        "time": _time(snapshot.recorded_at),
        "mark": _number(snapshot.mark_price),
        "last": _number(snapshot.last_price),
        "bid": _number(snapshot.bid_price),
        "ask": _number(snapshot.ask_price),
        "mid": _number(snapshot.mid_price),
        "index": _number(snapshot.index_price),
        "source": snapshot.source,
    }


def _number(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _time(value: datetime | None) -> str:
    return "" if value is None else value.isoformat()


_TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ - Kolabi run report</title>
<style>
:root{color-scheme:light;--ink:#182026;--muted:#63717b;--line:#d7dde1;--soft:#f5f7f8;--blue:#1769aa;--green:#167347;--red:#b23a32;--amber:#9a6400;--white:#fff}
*{box-sizing:border-box}body{margin:0;background:var(--white);color:var(--ink);font:14px/1.35 system-ui,-apple-system,"Segoe UI",sans-serif;letter-spacing:0}header{border-bottom:1px solid var(--line);padding:18px 24px 14px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0}.meta{color:var(--muted)}main{max-width:1800px;margin:auto}.band{padding:16px 24px;border-bottom:1px solid var(--line)}.metrics{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:0;border:1px solid var(--line)}.metric{padding:10px 12px;border-right:1px solid var(--line)}.metric:last-child{border-right:0}.metric b{display:block;font-size:18px;font-variant-numeric:tabular-nums}.metric span{color:var(--muted);font-size:12px}.positive{color:var(--green)}.negative{color:var(--red)}.quality{color:var(--amber)}.controls{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:12px 0}.controls label{display:grid;gap:4px;color:var(--muted);font-size:12px}.controls select,.controls input{height:34px;border:1px solid #aeb8bf;background:#fff;padding:0 9px;min-width:150px}.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.chart-grid.parameters-hidden{grid-template-columns:1fr}.chart{border:1px solid var(--line);padding:10px;min-height:240px}.chart canvas{width:100%;height:190px;display:block}.table-wrap{overflow:auto;max-height:540px;border:1px solid var(--line)}table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}th{position:sticky;top:0;background:var(--soft);z-index:1;color:#39464e;font-size:12px;cursor:pointer}td.num,th.num{text-align:right}tr:hover td{background:#f7fafc}.status{font-weight:600}.empty{padding:20px;color:var(--muted)}@media(max-width:900px){header,.band{padding-left:12px;padding-right:12px}.metrics{grid-template-columns:repeat(2,1fr)}.metric:nth-child(2n){border-right:0}.chart-grid{grid-template-columns:1fr}.table-wrap{max-height:420px}}
</style>
</head>
<body>
<header><h1 id="title"></h1><div class="meta" id="meta"></div></header>
<main>
<section class="band"><div class="metrics" id="metrics"></div></section>
<section class="band"><h2>Market and performance</h2><div class="chart-grid__PARAMETER_GRID_CLASS__"><div class="chart"><strong>Cumulative net USD equivalent</strong><canvas id="equity" width="800" height="190"></canvas></div><div class="chart"__PARAMETER_VISIBILITY__><strong>Selected pair parameter history</strong><canvas id="parameters" width="800" height="190"></canvas></div></div></section>
<section class="band"__PARAMETER_VISIBILITY__><h2>Parameter regimes</h2><div class="controls"><label>Pair<select id="pairFilter"></select></label><label>Terminal<select id="terminalFilter"></select></label><label>Search<input id="search" type="search" placeholder="Pair or attempt"></label></div><div class="table-wrap"><table id="regimes"><thead><tr><th>Pair</th><th>Attempts</th><th class="num">tOut</th><th class="num">hPrice</th><th class="num">N</th><th class="num">tOuts</th><th class="num">Closed</th><th>W/L</th><th class="num">Net USD</th><th class="num">Avg ROI %</th><th>Final</th></tr></thead><tbody></tbody></table></div></section>
<section class="band"__PARAMETER_VISIBILITY__><h2>Attempt details</h2><div class="table-wrap"><table id="attempts"><thead><tr><th>Pair</th><th class="num">#</th><th class="num">tOut</th><th class="num">hPrice</th><th>Terminal</th><th>H fill</th><th>T fill</th><th class="num">Net USD</th><th class="num">ROI %</th></tr></thead><tbody></tbody></table></div></section>
<section class="band"><h2>Market and pair finance</h2><div class="table-wrap"><table id="volume"><thead><tr><th>Market</th><th>Pair</th><th class="num">Closed</th><th class="num">Fills</th><th class="num">USD turnover</th><th class="num">Net USD</th><th class="num">Avg life</th><th class="num">Avg ROI %</th><th class="num">ROI/h %</th></tr></thead><tbody></tbody></table></div></section>
</main>
<script id="report-data" type="application/json">__REPORT_DATA__</script>
<script>
const d=JSON.parse(document.getElementById('report-data').textContent);const $=s=>document.querySelector(s);const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const fmt=(v,n=4)=>v==null?'':Number(v).toFixed(n);const signed=(v,n=4)=>v==null?'':`${v>=0?'+':''}${fmt(v,n)}`;const clock=s=>s?s.slice(5,16).replace('T',' '):'';const life=s=>{if(s==null)return'';let n=Math.round(s),h=Math.floor(n/3600),m=Math.floor(n%3600/60),x=n%60;return[h,m,x].map(v=>String(v).padStart(2,'0')).join(':')};
$('#title').textContent=d.name;$('#meta').textContent=`Run ${clock(d.runStarted)} | Report ${clock(d.reportAt)} | Mark ${fmt(d.prices.mark,5)} | Last ${fmt(d.prices.last,5)} | ${d.prices.source||'no price source'}`;
const o=d.overview;const metric=[['Net USD',signed(o.net,6)],['Closed',o.closed],['Win rate',o.winRate==null?'':fmt(o.winRate,2)+'%'],['Profit factor',fmt(o.profitFactor,3)],['Aggregate ROI',signed(o.aggregateRoi,4)+'%'],['ROI/h',signed(o.roiHour,4)+'%'],['Fees USD',fmt(o.fees,6)],['Max drawdown',fmt(o.drawdown,6)],['Open notional',o.openNotional==null?'n/a':fmt(o.openNotional,6)],['Evidence',`${o.exact} exact / ${o.estimated} est / ${o.unavailable} n/a`]];$('#metrics').innerHTML=metric.map(([k,v])=>`<div class="metric"><b class="${k==='Net USD'?(o.net>=0?'positive':'negative'):k==='Evidence'?'quality':''}">${v}</b><span>${k}</span></div>`).join('');
const pairs=[...new Set(d.regimes.map(r=>r.pair))].sort();$('#pairFilter').innerHTML='<option value="">All pairs</option>'+pairs.map(v=>`<option>${esc(v)}</option>`).join('');const terminals=[...new Set(d.attempts.map(r=>r.terminal))].sort();$('#terminalFilter').innerHTML='<option value="">All states</option>'+terminals.map(v=>`<option>${esc(v)}</option>`).join('');
function cls(v){return v==null?'':v>=0?'positive':'negative'}function render(){const pair=$('#pairFilter').value,term=$('#terminalFilter').value,q=$('#search').value.toLowerCase();const hasState=r=>!term||d.attempts.some(a=>a.pair===r.pair&&a.attempt>=r.first&&a.attempt<=r.last&&a.terminal===term);const regimes=d.regimes.filter(r=>(!pair||r.pair===pair)&&hasState(r)&&(!q||`${r.pair} ${r.range}`.toLowerCase().includes(q)));$('#regimes tbody').innerHTML=regimes.map(r=>`<tr><td>${esc(r.pair)}</td><td>${esc(r.range)}</td><td class="num">${fmt(r.tout,2)}</td><td class="num">${fmt(r.hprice,2)}</td><td class="num">${r.attempts}</td><td class="num">${r.timeouts}</td><td class="num">${r.closed}</td><td>${r.wins}/${r.losses}</td><td class="num ${cls(r.net)}">${signed(r.net,6)}</td><td class="num ${cls(r.roi)}">${signed(r.roi,4)}</td><td class="status">${esc(r.final)}</td></tr>`).join('');const attempts=d.attempts.filter(r=>(!pair||r.pair===pair)&&(!term||r.terminal===term)&&(!q||`${r.pair} ${r.attempt}`.toLowerCase().includes(q)));$('#attempts tbody').innerHTML=attempts.map(r=>`<tr><td>${esc(r.pair)}</td><td class="num">${r.attempt}</td><td class="num">${fmt(r.tout,2)}</td><td class="num">${fmt(r.hprice,2)}</td><td class="status">${esc(r.terminal)}</td><td>${clock(r.hfill)}</td><td>${clock(r.tfill)}</td><td class="num ${cls(r.net)}">${signed(r.net,6)}</td><td class="num ${cls(r.roi)}">${signed(r.roi,4)}</td></tr>`).join('');drawParameters(pair||pairs[0]);}
for(const el of [$('#pairFilter'),$('#terminalFilter'),$('#search')])el.addEventListener('input',render);$('#volume tbody').innerHTML=d.volume.map(r=>`<tr><td>${esc(r.market)}</td><td>${esc(r.pair)}</td><td class="num">${r.closed}</td><td class="num">${r.fills}</td><td class="num">${fmt(r.turnover,6)}</td><td class="num ${cls(r.net)}">${signed(r.net,6)}</td><td class="num">${life(r.life)}</td><td class="num ${cls(r.roi)}">${signed(r.roi,4)}</td><td class="num ${cls(r.roiHour)}">${signed(r.roiHour,4)}</td></tr>`).join('');
function line(canvas,values,color){const c=canvas.getContext('2d'),w=canvas.width,h=canvas.height,p=24;c.clearRect(0,0,w,h);c.strokeStyle='#d7dde1';c.beginPath();c.moveTo(p,h-p);c.lineTo(w-p,h-p);c.stroke();if(!values.length){c.fillStyle='#63717b';c.fillText('No data',p,p);return}let lo=Math.min(0,...values),hi=Math.max(0,...values);if(lo===hi){lo-=1;hi+=1}c.strokeStyle=color;c.lineWidth=2;c.beginPath();values.forEach((v,i)=>{const x=p+i*(w-2*p)/Math.max(values.length-1,1),y=h-p-(v-lo)*(h-2*p)/(hi-lo);i?c.lineTo(x,y):c.moveTo(x,y)});c.stroke();c.fillStyle='#63717b';c.fillText(fmt(hi,4),2,12);c.fillText(fmt(lo,4),2,h-5)}line($('#equity'),d.equity.map(x=>x.value),'#1769aa');
function drawParameters(pair){const rows=d.attempts.filter(r=>r.pair===pair),canvas=$('#parameters'),c=canvas.getContext('2d'),w=canvas.width,h=canvas.height,p=28;c.clearRect(0,0,w,h);if(!rows.length){c.fillStyle='#63717b';c.fillText('No pair data',p,p);return}const tracks=[['tout','#1769aa',20,76],['hprice','#9a6400',94,150]];for(const [key,color,top,bottom] of tracks){const vals=rows.map(r=>r[key]).filter(v=>v!=null),lo=Math.min(...vals),hi=Math.max(...vals);c.strokeStyle='#d7dde1';c.beginPath();c.moveTo(p,bottom);c.lineTo(w-p,bottom);c.stroke();c.strokeStyle=color;c.lineWidth=2;c.beginPath();rows.forEach((r,i)=>{if(r[key]==null)return;const x=p+i*(w-2*p)/Math.max(rows.length-1,1),y=bottom-(r[key]-lo)*(bottom-top)/Math.max(hi-lo,1);if(i)c.lineTo(x,y);else c.moveTo(x,y)});c.stroke();c.fillStyle=color;c.fillText(key==='tout'?'tOut':'hPrice',2,top+10)}c.fillStyle='#63717b';c.fillText('outcome',2,178);rows.forEach((r,i)=>{if(r.terminal==='active')return;const x=p+i*(w-2*p)/Math.max(rows.length-1,1);c.fillStyle=r.terminal==='roi>=0'?'#167347':r.terminal==='roi<0'?'#b23a32':'#9aa5ac';c.fillRect(x-1,170,3,8)});c.fillStyle='#63717b';c.fillText(pair,p,12)}
for(const table of document.querySelectorAll('table')){table.querySelectorAll('th').forEach((th,index)=>th.addEventListener('click',()=>{const body=table.tBodies[0],rows=[...body.rows],numeric=th.classList.contains('num');rows.sort((a,b)=>{const x=a.cells[index].textContent.trim(),y=b.cells[index].textContent.trim();return numeric?(parseFloat(x)||0)-(parseFloat(y)||0):x.localeCompare(y)});rows.forEach(row=>body.appendChild(row))}))}render();
</script>
</body></html>'''
