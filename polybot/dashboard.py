"""Real-time trading dashboard — FastAPI + Modal ASGI app with dark theme."""

import os, json, datetime, logging
import modal

logger = logging.getLogger(__name__)

_client = None

def _get_client():
    global _client
    if _client is None:
        url = os.environ.get("REDIS_URL")
        if url:
            import redis as _redis
            _client = _redis.from_url(url)
    return _client


# --- Modal app ----------------------------------------------------------------

image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "redis>=5.0.0",
    "fastapi>=0.115.0",
    "uvicorn>=0.30.0",
)

stub = modal.App("polybot-dashboard", image=image)
app = stub


# --- Redis data fetcher -------------------------------------------------------

def fetch_data():
    r = _get_client()
    if not r:
        return {"error": "no redis", "cities": {}, "ensemble": {},
                "bucket_scan": [], "resolved": [], "pnl": 0, "cycle": {},
                "live_trades": [], "live_pnl": 0, "live_win_rate": 0,
                "live_pnl_total": 0, "live_trade_count": 0,
                "city_metrics": {}, "gfs_ensemble_top": None, "bankroll": 4.83,
                "live_mode": True}

    cities = {}
    try:
        for key in r.keys("city:*"):
            raw = r.hgetall(key)
            name = key.decode() if isinstance(key, bytes) else str(key)
            city_name = name.split(":", 1)[1] if ":" in name else name
            cities[city_name] = {}
            for k, v in raw.items():
                k2 = k.decode() if isinstance(k, bytes) else str(k)
                v2 = v.decode() if isinstance(v, bytes) else str(v)
                cities[city_name][k2] = v2
    except Exception as e:
        logger.error(f"fetch_data cities error: {e}")

    ensemble = {}
    try:
        if r.exists("ensemble"):
            raw = r.hgetall("ensemble")
            for k, v in raw.items():
                k2 = k.decode() if isinstance(k, bytes) else str(k)
                v2 = v.decode() if isinstance(v, bytes) else str(v)
                ensemble[k2] = v2
    except Exception:
        pass

    bucket_scan = []
    try:
        for item in r.lrange("bucket_scan", 0, 19):
            try:
                bucket_scan.append(json.loads(item))
            except Exception:
                pass
    except Exception:
        pass

    resolved = []
    try:
        for item in r.lrange("resolved_trades", 0, 9):
            try:
                resolved.append(json.loads(item))
            except Exception:
                pass
    except Exception:
        pass

    pnl = 0
    try:
        pnl_raw = r.get("session_pnl")
        if pnl_raw:
            val = pnl_raw.decode() if isinstance(pnl_raw, bytes) else str(pnl_raw)
            pnl = int(float(val))
    except Exception:
        pass

    cycle = {}
    try:
        if r.exists("cycle"):
            raw = r.hgetall("cycle")
            for k, v in raw.items():
                k2 = k.decode() if isinstance(k, bytes) else str(k)
                v2 = v.decode() if isinstance(v, bytes) else str(v)
                cycle[k2] = v2
    except Exception:
        pass

    # --- Live trades (real Polymarket orders) ---
    live_trades = []
    live_pnl = 0
    live_win_rate = 0
    live_pnl_total = 0.0
    live_trade_count = 0
    try:
        raw_trades = r.lrange("live_trades", 0, 49)
        wins = 0
        total_resolved = 0
        cumulative_pnl = 0.0
        for item in raw_trades:
            try:
                t = json.loads(item)
                live_trades.append(t)
                if t.get("status") == "resolved":
                    total_resolved += 1
                    profit = float(t.get("profit_usd", t.get("pnl", 0)))
                    cumulative_pnl += profit
                    if profit > 0:
                        wins += 1
            except Exception:
                pass
        live_pnl = round(cumulative_pnl, 2)
        if total_resolved > 0:
            live_win_rate = round(wins / total_resolved * 100, 1)
        try:
            live_pnl_total = float(r.get("live_pnl_total") or cumulative_pnl)
            live_trade_count = int(r.get("live_trade_count") or total_resolved)
        except Exception:
            live_pnl_total = round(cumulative_pnl, 2)
            live_trade_count = total_resolved
    except Exception:
        pass

    # --- Bankroll ---
    bankroll = 4.83
    try:
        bankroll_raw = r.get("bankroll")
        if bankroll_raw:
            bankroll = float(bankroll_raw.decode() if isinstance(bankroll_raw, bytes) else bankroll_raw)
    except Exception:
        pass

    city_metrics = {}
    try:
        for key in r.scan_iter("city_metrics:*"):
            raw = r.hgetall(key)
            name = key.decode() if isinstance(key, bytes) else str(key)
            slug = name.split(":", 1)[1] if ":" in name else name
            city_metrics[slug] = {}
            for k, v in raw.items():
                k2 = k.decode() if isinstance(k, bytes) else str(k)
                v2 = v.decode() if isinstance(v, bytes) else str(v)
                city_metrics[slug][k2] = v2
    except Exception:
        pass

    market_status = {}
    try:
        for key in r.scan_iter("market_status:*"):
            raw = r.hgetall(key)
            name = key.decode() if isinstance(key, bytes) else str(key)
            city_name = name.split(":", 1)[1] if ":" in name else name
            market_status[city_name] = {}
            for k, v in raw.items():
                k2 = k.decode() if isinstance(k, bytes) else str(k)
                v2 = v.decode() if isinstance(v, bytes) else str(v)
                market_status[city_name][k2] = v2
    except Exception:
        pass

    accuracy_data = {}
    try:
        if r.exists("accuracy"):
            raw = r.hgetall("accuracy")
            for k, v in raw.items():
                k2 = k.decode() if isinstance(k, bytes) else str(k)
                v2 = v.decode() if isinstance(v, bytes) else str(v)
                accuracy_data[k2] = v2
    except Exception:
        pass

    # Get top GFS bucket for ensemble display
    # Select the city with the most GFS data (highest member count) rather than first alphabetically
    gfs_ensemble_top = None
    try:
        best_city = None
        best_count = 0
        best_temps = []
        best_mean = None
        best_spread = None

        for key in r.scan_iter("gfs_ensemble:*"):
            raw_temps = r.lrange(key, 0, -1)
            if not raw_temps:
                continue
            slug = key.decode().split(":", 1)[1] if isinstance(key, bytes) else key.split(":", 1)[1]
            temps_f = [float(t.decode() if isinstance(t, bytes) else t) for t in raw_temps]
            count = len(temps_f)

            # Pick city with highest member count (most complete data)
            if count > best_count:
                best_count = count
                best_city = slug
                best_temps = temps_f
                # Fetch metrics for this city
                try:
                    metrics_raw = r.hget(f"city_metrics:{slug}", "gfs_ensemble_mean")
                    if metrics_raw:
                        best_mean = float(metrics_raw.decode() if isinstance(metrics_raw, bytes) else metrics_raw)
                    spread_raw = r.hget(f"city_metrics:{slug}", "gfs_ensemble_spread")
                    if spread_raw:
                        best_spread = float(spread_raw.decode() if isinstance(spread_raw, bytes) else spread_raw)
                except Exception:
                    pass

        if best_temps and best_city:
            # Compute bucket probabilities using proper bucket counting
            # Bucket edges match the standard Polymarket NYC-style buckets
            bucket_edges = [65, 66, 68, 70, 72, 74, 76, 78, 80, 82, 84, 9999]
            bucket_names = [
                "\u226565F", "66-67F", "68-69F", "70-71F", "72-73F",
                "74-75F", "76-77F", "78-79F", "80-81F", "82-83F", "\u226584F"
            ]
            n = len(best_temps)
            best_bucket = None
            best_prob = 0
            bucket_probs = {}

            for i in range(len(bucket_names)):
                low = bucket_edges[i]
                high = bucket_edges[i + 1]
                if high >= 9999:
                    count_in = sum(1 for t in best_temps if t >= low)
                else:
                    count_in = sum(1 for t in best_temps if low <= t < high)
                prob = count_in / n if n > 0 else 0
                bucket_probs[bucket_names[i]] = round(prob * 100, 1)
                if prob > best_prob:
                    best_prob = prob
                    best_bucket = bucket_names[i]

            # Fallback: if no bucket has >10% probability, use nearest to mean
            if best_prob < 0.10 and best_mean:
                nearest_edge = min(bucket_edges[:-1], key=lambda e: abs(e - best_mean))
                idx = bucket_edges.index(nearest_edge)
                best_bucket = bucket_names[idx] if idx < len(bucket_names) else f"{nearest_edge}F+"
                best_prob = bucket_probs.get(best_bucket, 0)

            gfs_ensemble_top = {
                "city": best_city,
                "bucket": best_bucket or "N/A",
                "probability": round(best_prob * 100, 1),
                "mean_f": round(best_mean, 1) if best_mean else round(sum(best_temps) / n, 1) if n else None,
                "spread_f": round(best_spread, 1) if best_spread else (
                    round(max(best_temps) - min(best_temps), 1) if n else None
                ),
                "members": n,
                "bucket_probs": bucket_probs,
            }
            print(f"[DASHBOARD] GFS top: {best_city} mean={best_mean}F top_bucket={best_bucket} P={best_prob:.0%}")
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"GFS ensemble top error: {e}")

    return {
        "cities": cities, "ensemble": ensemble, "bucket_scan": bucket_scan,
        "resolved": resolved, "pnl": pnl, "cycle": cycle,
        "live_trades": live_trades[:10], "live_pnl": live_pnl,
        "live_win_rate": live_win_rate, "live_pnl_total": live_pnl_total,
        "live_trade_count": live_trade_count, "city_metrics": city_metrics,
        "market_status": market_status, "accuracy": accuracy_data,
        "gfs_ensemble_top": gfs_ensemble_top, "bankroll": bankroll,
        "live_mode": True,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


# --- HTML template (inline) ---------------------------------------------------

_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Polybot Dashboard — LIVE</title>
<style>
:root{--bg:#0d1117;--fg:#c9d1d9;--dim:#8b949e;--accent:#58a6ff;
--green:#3fb950;--red:#f85149;--yellow:#d29922;--card:#161b22;
--border:#30363d;--magenta:#bc8cff;--orange:#db6d28;--cyan:#39d0d8;}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:monospace;background:var(--bg);color:var(--fg);font-size:13px;line-height:1.4;padding:12px;min-height:100vh}
.hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--border)}
.hdr h1{font-size:15px;color:var(--cyan)}.hdr .meta{font-size:11px;color:var(--dim);text-align:right}
.pnl{font-weight:bold;font-size:18px}.pnl.up{color:var(--green)}.pnl.down{color:var(--red)}
.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:10px}
.card h2{font-size:11px;letter-spacing:.5px;text-transform:uppercase;color:var(--dim);margin-bottom:6px;border-bottom:1px solid var(--border);padding-bottom:4px}
.tbl{width:100%;border-collapse:collapse}
.tbl th{text-align:left;font-size:10px;color:var(--dim);padding:3px 5px;border-bottom:1px solid var(--border)}
.tbl td{padding:3px 5px;font-size:12px}
.badge{display:inline-block;padding:1px 5px;border-radius:3px;font-size:10px;font-weight:600}
.bg-green{background:rgba(63,185,80,.15);color:var(--green)}
.bg-red{background:rgba(248,81,73,.15);color:var(--red)}
.bg-yellow{background:rgba(210,153,34,.15);color:var(--yellow)}
.bg-live{background:rgba(63,185,80,.25);color:var(--green);border:1px solid var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.6}}
.sec{margin-bottom:3px;font-size:12px}.sec b{color:var(--accent)}
.ts{text-align:right;color:var(--dim);font-size:11px;margin-top:10px}
.full{grid-column:1/-1}
.summary{display:flex;gap:20px;align-items:center;background:var(--card);border:1px solid var(--border);border-radius:6px;padding:10px 16px;margin-bottom:12px;flex-wrap:wrap}
.summary-item{text-align:center}.summary-item .label{font-size:10px;color:var(--dim);text-transform:uppercase}.summary-item .value{font-size:20px;font-weight:700}
.val-up{color:var(--green)}.val-down{color:var(--red)}.val-dim{color:var(--fg)}
</style>
</head>
<body>
<div class="hdr"><h1>POLYBOT v5 <span class="badge bg-live">● LIVE TRADING ACTIVE</span></h1>
<div class="meta"><div>UTC <span id="utcTime">--:--:--</span> | Cities <span id="cityCount">0</span> | Bankroll $<span id="bankroll">--</span> | Engine: GFS 31-member</div></div></div>
<div class="summary">
<div class="summary-item"><div class="label">Live P&amp;L</div><div class="value" id="sumPnl">--</div></div>
<div class="summary-item"><div class="label">Win Rate</div><div class="value val-dim" id="sumWr">--</div></div>
<div class="summary-item"><div class="label">Live Trades</div><div class="value val-dim" id="sumTrades">--</div></div>
</div>
<div class="g">
<div class="card full"><h2>City Temperature Forecast</h2>
<table class="tbl"><thead><tr><th>CITY</th><th>LIVE &deg;F</th><th>LIVE &deg;C</th><th>ECMWF</th><th>ICON</th><th>UKMO</th><th>GFS</th><th>SKY</th><th>TREND</th><th>SLOPE</th><th>PEAK</th><th>MARKET</th><th>STATUS</th><th>TIME</th></tr></thead>
<tbody id="cityBody"><tr><td colspan="14" style="text-align:center;color:var(--dim)">Loading...</td></tr></tbody></table>
</div>
<div class="card"><h2>Ensemble Agreement</h2><div id="ensPanel"><div style="color:var(--dim);font-size:11px">Waiting for GFS data...</div></div></div>
<div class="card"><h2>Bucket Scan</h2><table class="tbl"><thead><tr><th>BUCKET</th><th>P</th><th>Q</th><th>EDGE</th><th>HIT</th></tr></thead><tbody id="bktBody"><tr><td colspan="5">No scans</td></tr></tbody></table></div>
<div class="card full"><h2>Live Trade Feed</h2><div id="feed" style="max-height:150px;overflow-y:auto;font-size:11px"></div></div>
</div>
<div class="ts">Updated: <span id="upd">--</span> | Auto-refresh: 5s</div>
<script>
var S={cities:{},bucket_scan:[],resolved:[],pnl:0,cycle:{},live_trades:[],live_pnl:0,live_win_rate:0,live_pnl_total:0,live_trade_count:0,city_metrics:{},feed:[],gfs_ensemble_top:null,bankroll:4.83};
function f2c(f){if(f==null||f===''||f==='N/A')return null;var n=parseFloat(f);return isNaN(n)?null:(n-32)*5/9;}
function fmtF(v){if(v==null||v===''||v==='N/A')return'&mdash;';var n=parseFloat(v);return isNaN(n)?v:n.toFixed(1)+'&deg;F';}
function fmtC(v){var c=f2c(v);return c==null?'&mdash;':c.toFixed(1)+'&deg;C';}
function fmtTime(iso){if(!iso)return'&mdash;';try{return new Date(iso).toISOString().replace('T',' ').substr(0,19);}catch(e){return iso.substr(0,19);}}
function getMetrics(c){var m=S.city_metrics[c]||{};if(!m||Object.keys(m).length===0){var s=c.toLowerCase().replace(/ /g,'_');m=S.city_metrics[s]||{};}if(!m||Object.keys(m).length===0){var keys=Object.keys(S.city_metrics);for(var i=0;i<keys.length;i++){if(keys[i].indexOf(c.toLowerCase().replace(/ /g,'_'))===0||c.toLowerCase().indexOf(keys[i].replace(/_/g,' '))===0){m=S.city_metrics[keys[i]]||{};break;}}}return m||{};}
function renderCities(){var tb=document.getElementById('cityBody');var keys=Object.keys(S.cities);if(!keys.length){tb.innerHTML='<tr><td colspan="14" style="text-align:center;color:var(--dim)">Waiting...</td></tr>';return;}var html='';for(var i=0;i<keys.length;i++){var c=keys[i];var d=S.cities[c]||{};var m=getMetrics(c);var lv=parseFloat(d.live_temp);var lf=isNaN(lv)?'&mdash;':lv.toFixed(1)+'&deg;F';var lc=isNaN(lv)?'&mdash;':((lv-32)*5/9).toFixed(1)+'&deg;C';var gfs=(m.gfs_ensemble_mean!==undefined&&m.gfs_ensemble_mean!==''&&m.gfs_ensemble_mean!=='N/A')?parseFloat(m.gfs_ensemble_mean).toFixed(1)+'&deg;F':(m.gfs_mean!==undefined&&m.gfs_mean!==''&&m.gfs_mean!=='N/A')?parseFloat(m.gfs_mean).toFixed(1)+'&deg;F':'&mdash;';var sky=d.sky||'&mdash;';var trend=d.trend||'&mdash;';var slope=(m.slope_f_per_15min!==undefined&&m.slope_f_per_15min!=='')?parseFloat(m.slope_f_per_15min).toFixed(2)+'F/5m':'&mdash;';var peak=m.peak_window==='1'?'<span class="badge bg-green">PEAK</span>':'<span class="badge bg-yellow">off</span>';var mkt=d.market_date||'&mdash;';var mktStatus=d.market_status||'&mdash;';var mktStatusClass=d.market_status==='OPEN'?'bg-green':(d.market_status==='CLOSED'?'bg-yellow':'');var mktStatusHtml=mktStatusClass?'<span class="badge '+mktStatusClass+'">'+mktStatus+'</span>':mktStatus;var resolveTime=d.resolve_time||'&mdash;';html+='<tr style="color:var(--accent);font-weight:600"><td>'+c+'</td><td>'+lf+'</td><td style="color:var(--cyan)">'+lc+'</td><td>'+fmtF(d.ecmwf)+'</td><td>'+fmtF(d.icon)+'</td><td>'+fmtF(d.ukmo)+'</td><td style="color:var(--green)">'+gfs+'</td><td>'+sky+'</td><td>'+trend+'</td><td style="color:var(--cyan)">'+slope+'</td><td>'+peak+'</td><td>'+mkt+'</td><td>'+mktStatusHtml+'</td><td>'+resolveTime+'</td></tr>';}tb.innerHTML=html;}
function renderEnsemble(){var g=S.gfs_ensemble_top;var p=document.getElementById('ensPanel');if(!g||!g.bucket){p.innerHTML='<div style="color:var(--dim);font-size:11px">No GFS ensemble data yet. Run a scan.</div>';return;}var html='<div class="sec"><b>City:</b> <span style="color:var(--accent)">'+(g.city||'N/A')+'</span> ('+g.members+'-member GFS)</div>';html+='<div class="sec"><b>Mean:</b> '+(g.mean_f?g.mean_f+'&deg;F':'N/A')+' | <b>Spread:</b> '+(g.spread_f?'&plusmn;'+g.spread_f+'&deg;F':'N/A')+'</div>';html+='<div class="sec"><b>Top Bucket:</b> <span style="color:var(--cyan);font-size:14px">'+g.bucket+'</span> <span style="color:var(--green)">'+g.probability+'%</span></div>';if(g.bucket_probs){html+='<div style="margin-top:6px;font-size:10px;color:var(--dim)">Bucket distribution:</div>';var bp=g.bucket_probs;var bn=Object.keys(bp);for(var i=0;i<bn.length;i++){var bname=bn[i];var bval=bp[bname];if(bval>1){var barW=Math.min(Math.round(bval*1.5),150);var bar='<span style="display:inline-block;background:rgba(88,166,255,0.3);height:8px;width:'+barW+'px;margin-right:4px;vertical-align:middle"></span>';html+='<div style="font-size:10px;padding:1px 0">'+id+': '+bar+bval+'%</div>';}};}p.innerHTML=html;}
function render(){renderCities();renderEnsemble();var pnl=S.live_pnl_total||0;var el=document.getElementById('sumPnl');el.textContent=(pnl>=0?'+':'-')+'$'+Math.abs(pnl).toFixed(2);el.className='value '+(pnl>=0?'val-up':'val-down');document.getElementById('sumWr').textContent=S.live_win_rate+'%';document.getElementById('sumTrades').textContent=S.live_trade_count;document.getElementById('bankroll').textContent=S.bankroll.toFixed(2);document.getElementById('upd').textContent=new Date().toLocaleTimeString();document.getElementById('utcTime').textContent=new Date().toUTCString().slice(17,25);document.getElementById('cityCount').textContent=Object.keys(S.cities).length;var feedEl=document.getElementById('feed');if(feedEl&&S.live_trades.length){var ft='';for(var i=0;i<Math.min(S.live_trades.length,20);i++){var t=S.live_trades[i];ft+='<div style="padding:2px 0;border-bottom:1px solid var(--border)"><span style="color:var(--cyan)">'+(t.city||'?')+'</span> '+(t.side||'?')+' '+(t.bucket||'?')+' @ '+(t.price||'?')+' size='+(t.size||'?')+' <span style="color:'+(t.status==='resolved'?(parseFloat(t.pnl||0)>0?'var(--green)':'var(--red)'):'var(--dim)')+'">'+(t.status||'open')+'</span></div>';}feedEl.innerHTML=ft;}else if(feedEl){feedEl.innerHTML='<div style="color:var(--dim)">No live trades yet</div>';}}
async function poll(){try{var resp=await fetch('/api/data');if(!resp.ok){console.error('API error:',resp.status);return;}var data=await resp.json();S.cities=data.cities||{};S.bucket_scan=data.bucket_scan||[];S.city_metrics=data.city_metrics||{};S.gfs_ensemble_top=data.gfs_ensemble_top||null;S.bankroll=data.bankroll||4.83;S.live_trades=data.live_trades||[];S.live_pnl_total=data.live_pnl_total||0;S.live_win_rate=data.live_win_rate||0;S.live_trade_count=data.live_trade_count||0;render();}catch(e){console.error('Poll error:',e);}}
poll();setInterval(poll,5000);
</script></body></html>'''


@stub.function(secrets=[modal.Secret.from_name("redis-url")], image=image)
@modal.asgi_app()
def fastapi_app():
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse
    web = FastAPI()
    @web.get("/")
    async def root():
        return HTMLResponse(content=_HTML)
    @web.get("/api/data")
    async def api_data():
        return JSONResponse(content=fetch_data())
    return web
