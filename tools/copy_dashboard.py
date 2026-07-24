"""
tools/copy_dashboard.py

Local dashboard for the live PAPER graduation sniper. Serves a single
auto-refreshing page at http://localhost:8770 with:

  - live GRADUATION RADAR — the bonding-curve race to 85 SOL, fed by the
    sniper's ~5s snapshot (logs/graduation_live.json)
  - realized paper equity curve (balance trajectory, custom SVG)
  - headline stats: balance, return, win rate, drawdown
  - per-token attribution + recent closes

Reads logs/graduation_trades.jsonl only. (The module keeps the legacy name
`copy_dashboard` / port 8770 because run_dashboard_forever.ps1 and the
GradDashboard24x7 scheduled task invoke `python -m tools.copy_dashboard` —
the copy-follower book it used to also show was retired 2026-07-24.)

Run: python -m tools.copy_dashboard        # then open http://localhost:8770
"""

from __future__ import annotations

import json
import os
import statistics as st
import time as _t

from aiohttp import web

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRAD_LOG = os.path.join(ROOT, "logs", "graduation_trades.jsonl")
GRAD_LIVE = os.path.join(ROOT, "logs", "graduation_live.json")
GRAD_STATE = os.path.join(ROOT, "logs", "graduation_state.json")
PORT = 8770
SEED_SOL_DEFAULT = 5.0
SOL_PRICE_USD = 85.0        # same constant grad_report uses for its $ figures


def _read():
    """Every open/close from the graduation-sniper log."""
    opens, closes = [], []
    if not os.path.exists(GRAD_LOG):
        return opens, closes
    for line in open(GRAD_LOG, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        e = r.get("event")
        if e == "open":
            opens.append(r)
        elif e == "close":
            closes.append(r)
        # skip / shadow_outcome / tail_* events are ignored for equity math
    return opens, closes


def _grad_state() -> dict:
    """Seed + open-position count from the sniper's state file."""
    seed_sol, open_count = SEED_SOL_DEFAULT, 0
    if os.path.exists(GRAD_STATE):
        try:
            s = json.load(open(GRAD_STATE))
            seed_sol = (s.get("account") or {}).get("seed_sol", SEED_SOL_DEFAULT)
            open_count = len(s.get("positions") or {})
        except Exception:
            pass
    return {"seed_sol": seed_sol, "open_count": open_count}


def _data():
    opens, closes = _read()
    closes.sort(key=lambda c: c.get("ts", 0))
    gs = _grad_state()
    seed_sol = gs["seed_sol"] or SEED_SOL_DEFAULT

    # equity = balance trajectory (seed + cumulative realized PnL)
    bal, equity, peak, max_dd = seed_sol, [], seed_sol, 0.0
    for c in closes:
        bal += c.get("pnl_sol", 0.0)
        peak = max(peak, bal)
        if peak > 0:
            max_dd = max(max_dd, (peak - bal) / peak * 100)
        equity.append({"t": c.get("ts", 0), "cum": round(bal, 5)})
    cum_pnl = sum(c.get("pnl_sol", 0.0) for c in closes)
    cur_bal_sol = seed_sol + cum_pnl
    wins = sum(1 for c in closes if c.get("pnl_sol", 0) > 0)

    open_sizes = [o.get("size_sol", 0) for o in opens if "size_sol" in o]
    avg_size = round(st.mean(open_sizes), 4) if open_sizes else None

    # per-token attribution (graduation records carry `symbol`)
    by_token: dict = {}
    for c in closes:
        sym = c.get("symbol", "?")
        by_token.setdefault(sym, {"trades": 0, "net": 0.0})
        by_token[sym]["trades"] += 1
        by_token[sym]["net"] = round(by_token[sym]["net"]
                                     + c.get("pnl_sol", 0.0), 4)
    # heaviest movers first (largest absolute net)
    by_token = dict(sorted(by_token.items(),
                           key=lambda kv: -abs(kv[1]["net"])))

    return {
        "stats": {
            "seed_sol": round(seed_sol, 4),
            "sol_price_usd": SOL_PRICE_USD,
            "balance_sol": round(cur_bal_sol, 4),
            "balance_usd": round(cur_bal_sol * SOL_PRICE_USD, 2),
            "net_sol": round(cum_pnl, 4),
            "return_pct": round((cur_bal_sol / seed_sol - 1) * 100, 1)
                          if seed_sol > 0 else None,
            "max_dd_pct": round(max_dd, 1),
            "closed": len(closes), "open_now": gs["open_count"],
            "win_rate": round(wins / len(closes) * 100, 1) if closes else None,
            "avg_size_sol": avg_size,
        },
        "equity": equity,
        "by_token": by_token,
        "recent": list(reversed(closes[-15:])),
        "grad_live": _grad_live(),
    }


def _grad_live():
    """Live graduation-radar snapshot written by the sniper every ~5s."""
    if not os.path.exists(GRAD_LIVE):
        return None
    try:
        snap = json.load(open(GRAD_LIVE))
    except Exception:
        return None
    snap["age_s"] = round(_t.time() - (snap.get("ts") or 0), 1)
    return snap


async def data_handler(_req):
    return web.json_response(_data())


HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>Graduation Sniper (paper)</title>
<style>
 body{background:#0b0e14;color:#cdd6f4;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;margin:0;padding:18px}
 h1{font-size:15px;margin:0 0 4px;color:#89b4fa} .sub{color:#6c7086;margin-bottom:14px}
 .cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
 .card{background:#11151c;border:1px solid #1e2430;border-radius:8px;padding:10px 14px;min-width:120px}
 .card .v{font-size:20px;font-weight:600} .card .k{color:#6c7086;font-size:11px;text-transform:uppercase}
 .pos{color:#a6e3a1}.neg{color:#f38ba8}
 svg{background:#11151c;border:1px solid #1e2430;border-radius:8px}
 table{border-collapse:collapse;width:100%;margin-top:14px}
 th,td{text-align:right;padding:4px 8px;border-bottom:1px solid #1e2430} th{color:#6c7086;font-weight:500}
 td:first-child,th:first-child{text-align:left}
 .grid{display:flex;gap:18px;flex-wrap:wrap}
</style></head><body>
<h1>pump.bot 2.0 — graduation sniper (PAPER)</h1>
<div class=sub id=sub>loading…</div>
<div style="margin-bottom:18px">
 <div class=k style="color:#f9e2af;font-size:12px;margin-bottom:6px;font-weight:600">🎓 GRADUATION RADAR — live bonding-curve race to 85 SOL</div>
 <div id=radar style="background:#11151c;border:1px solid #1e2430;border-radius:8px;padding:14px"></div>
 <div id=radar-sub style="color:#6c7086;font-size:11px;margin-top:6px"></div>
</div>
<div class=cards id=cards></div>
<div class=grid>
 <div><div class=k style="color:#6c7086;font-size:11px;margin-bottom:4px">REALIZED PAPER EQUITY (SOL)</div>
   <svg id=chart width=720 height=240></svg></div>
 <div><div class=k style="color:#6c7086;font-size:11px;margin-bottom:4px">BY TOKEN</div>
   <table id=bt><tr><th>token</th><th>trades</th><th>net SOL</th></tr></table></div>
</div>
<div style="margin-top:18px">
 <div class=k style="color:#6c7086;font-size:11px;margin-bottom:4px">RECENT CLOSES</div>
 <table id=recent><tr><th>token</th><th>exit</th><th>net %</th><th>pnl SOL</th><th>hold</th></tr></table>
</div>
<script>
function fmtHold(s){s=+s||0;return s<90?s+'s':s<5400?Math.round(s/60)+'m':Math.round(s/3600)+'h'}
const SVG_NS='http://www.w3.org/2000/svg';
function svgEl(tag,attrs,text){
 const e=document.createElementNS(SVG_NS,tag);
 for(const k in attrs) e.setAttribute(k, attrs[k]);
 if(text!=null) e.textContent=text;
 return e;
}
function draw(eq, seed){
 const W=720,H=240,P=42,svg=document.getElementById('chart');
 while(svg.firstChild) svg.removeChild(svg.firstChild);
 if(!eq.length){
   svg.appendChild(svgEl('text',{x:W/2,y:H/2,fill:'#6c7086','text-anchor':'middle'},'no closed trades yet'));
   return;
 }
 const xs=eq.map(p=>p.t),ys=eq.map(p=>p.cum);
 const ref=Number(seed)||ys[0];
 let y0=Math.min(ref,...ys), y1=Math.max(ref,...ys);
 if(y0===y1){y0-=0.1;y1+=0.1;}
 const x0=Math.min(...xs), x1=Math.max(...xs)||x0+1;
 const sx=t=>P+(t-x0)/((x1-x0)||1)*(W-P-10);
 const sy=v=>H-30-(v-y0)/((y1-y0)||1)*(H-50);
 // seed reference line
 svg.appendChild(svgEl('line',{x1:P,y1:sy(ref),x2:W-10,y2:sy(ref),stroke:'#313244','stroke-dasharray':'4 4'}));
 svg.appendChild(svgEl('text',{x:P+2,y:sy(ref)-4,fill:'#6c7086','font-size':10},'seed '+ref.toFixed(3)));
 // path
 let dpath='';
 eq.forEach((p,i)=>{dpath+=(i?'L':'M')+sx(p.t)+' '+sy(p.cum);});
 const last=ys[ys.length-1];
 const col=last>=ref?'#a6e3a1':'#f38ba8';
 svg.appendChild(svgEl('path',{d:dpath,fill:'none',stroke:col,'stroke-width':1.5}));
 const lp=eq[eq.length-1];
 svg.appendChild(svgEl('circle',{cx:sx(lp.t),cy:sy(lp.cum),r:3,fill:col}));
 svg.appendChild(svgEl('text',{x:sx(lp.t)-6,y:sy(lp.cum)-8,fill:col,'font-size':11,'text-anchor':'end'},last.toFixed(3)+' SOL'));
}
function card(k,v,cls){return `<div class=card><div class=v ${cls?'class='+cls:''}>${v}</div><div class=k>${k}</div></div>`}
function drawRadar(g){
 const el=document.getElementById('radar'), sub=document.getElementById('radar-sub');
 if(!g){el.innerHTML='<div style="color:#6c7086">sniper not running or no snapshot yet</div>';sub.textContent='';return;}
 const LO=60, HI=86;                      // radar x-axis range in real SOL
 const pct=v=>Math.max(0,Math.min(100,(v-LO)/(HI-LO)*100));
 const entry=pct(g.entry_real_sol), exit=pct(g.exit_real_sol), grad=pct(85);
 let h='';
 if(!g.tokens || !g.tokens.length){
   h='<div style="color:#6c7086;padding:8px 0">scanning for tokens in the hot zone (65+ SOL)…</div>';
 }
 for(const t of (g.tokens||[])){
   const p=pct(t.real_sol);
   let barCol='#45475a';                                 // gray: below entry
   if(t.in_position) barCol='#a6e3a1';                   // green: WE ARE IN
   else if(t.flagged && t.flagged!=='velocity') barCol='#f38ba8';  // red: rejected
   else if(t.real_sol>=g.entry_real_sol) barCol='#f9e2af';         // yellow: in zone
   const vel=t.velocity_5m>0?`+${t.velocity_5m}`:`${t.velocity_5m}`;
   const velCol=t.velocity_5m>=1.5?'#a6e3a1':(t.velocity_5m>0?'#f9e2af':'#f38ba8');
   const tag=t.in_position?' 🟢 IN POSITION':(t.flagged?` ⛔ ${t.flagged}`:'');
   h+=`<div style="margin:7px 0">
     <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:2px">
       <span><b>${t.symbol}</b>${tag}</span>
       <span><span style="color:${velCol}">${vel} SOL/5m</span> · steps ${t.steps} · <b>${t.real_sol}</b>/85 SOL</span>
     </div>
     <div style="position:relative;height:16px;background:#181c26;border-radius:4px;overflow:hidden">
       <div style="position:absolute;left:${entry}%;width:${exit-entry}%;height:100%;background:rgba(249,226,175,0.12)"></div>
       <div style="position:absolute;left:0;width:${p}%;height:100%;background:${barCol};border-radius:4px;transition:width .8s"></div>
       <div style="position:absolute;left:${entry}%;width:1px;height:100%;background:#f9e2af"></div>
       <div style="position:absolute;left:${exit}%;width:1px;height:100%;background:#a6e3a1"></div>
       <div style="position:absolute;left:${grad}%;width:2px;height:100%;background:#cba6f7"></div>
     </div>
   </div>`;
 }
 el.innerHTML=h;
 const stale=g.age_s>30?` ⚠️ SNAPSHOT ${Math.round(g.age_s)}s OLD — sniper may be down`:'';
 sub.innerHTML=`grad book: <b>${g.balance_sol} SOL</b> · open ${g.open_positions} · tracking ${(g.tokens||[]).length} · `+
   `<span style="color:#f9e2af">│ entry ${g.entry_real_sol}</span> <span style="color:#a6e3a1">│ exit ${g.exit_real_sol}</span> <span style="color:#cba6f7">│ graduation 85</span>${stale}`;
}
async function tick(){
 const d=await (await fetch('/data')).json(),s=d.stats;
 drawRadar(d.grad_live);
 const sc=v=>v>0?'pos':v<0?'neg':'';
 const seed=`$${(s.seed_sol*s.sol_price_usd).toFixed(0)} (${s.seed_sol} SOL @ $${s.sol_price_usd})`;
 document.getElementById('sub').textContent='pump.fun bonding-curve scalp (paper)'+(d.grad_live?'':' · sniper offline')+' · seed '+seed+' · updated '+new Date().toLocaleTimeString();
 const ret = s.return_pct;
 document.getElementById('cards').innerHTML=
   `<div class=card style="border-color:#89b4fa"><div class="v ${sc(ret)}">${ret==null?'—':(ret>0?'+':'')+ret+'%'}</div><div class=k>return</div></div>`+
   `<div class=card style="border-color:#89b4fa"><div class="v ${sc(s.net_sol)}">${s.balance_sol} SOL</div><div class=k>balance ${s.balance_usd!=null?'($'+s.balance_usd+')':''}</div></div>`+
   `<div class=card><div class="v ${sc(s.net_sol)}">${s.net_sol>0?'+':''}${s.net_sol}</div><div class=k>net SOL</div></div>`+
   card('win rate',s.win_rate==null?'—':s.win_rate+'%')+
   card('closed',s.closed)+card('open now',s.open_now)+
   `<div class=card><div class="v neg">${s.max_dd_pct}%</div><div class=k>max drawdown</div></div>`+
   card('avg size',s.avg_size_sol==null?'—':s.avg_size_sol+' SOL');
 draw(d.equity, s.seed_sol);
 let bt='<tr><th>token</th><th>trades</th><th>net SOL</th></tr>';
 for(const[sym,o]of Object.entries(d.by_token))bt+=`<tr><td>${sym}</td><td>${o.trades}</td><td class=${sc(o.net)}>${o.net>0?'+':''}${o.net}</td></tr>`;
 document.getElementById('bt').innerHTML=bt;
 let rc='<tr><th>token</th><th>exit</th><th>net %</th><th>pnl SOL</th><th>hold</th></tr>';
 for(const c of d.recent)rc+=`<tr><td>${c.symbol||'?'}</td><td>${c.exit_reason||'?'}</td><td class=${sc(c.net_pct)}>${c.net_pct}</td><td class=${sc(c.pnl_sol)}>${c.pnl_sol}</td><td>${fmtHold(c.hold_s)}</td></tr>`;
 document.getElementById('recent').innerHTML=rc;
}
tick();setInterval(tick,5000);
</script></body></html>"""


async def index(_req):
    return web.Response(text=HTML, content_type="text/html")


def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/data", data_handler)
    print(f"Graduation dashboard -> http://localhost:{PORT}")
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
