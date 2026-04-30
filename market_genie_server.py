"""
Market Genie — Live Data Server
================================
Aggregates real-time data from Finnhub, yfinance, Reddit, and analyst consensus
into a single JSON endpoint consumed by market_genie.html.

Usage:
    python market_genie_server.py

Requires: pip install flask flask-cors requests yfinance python-dotenv
API Keys: copy .env.example → .env and fill in your keys
"""

import os
import re
import gc
import time
import json
import math
import threading
import webbrowser
import requests
import yfinance as yf
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

# ── Clear stale yfinance disk cache on every startup ─────────────────────────
# yfinance caches .info data to SQLite on disk; stale entries cause old prices.
# Clearing on startup guarantees fresh data for the session.
try:
    import glob, shutil
    _yf_cache_dirs = [
        os.path.join(os.path.expanduser("~"), ".cache", "py-yfinance"),
        os.path.join(os.environ.get("APPDATA", ""), "py-yfinance"),   # Windows
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "py-yfinance"),
    ]
    for _d in _yf_cache_dirs:
        if os.path.isdir(_d):
            shutil.rmtree(_d, ignore_errors=True)
            print(f"[Startup] Cleared yfinance cache: {_d}")
except Exception as _ce:
    print(f"[Startup] Cache clear skipped: {_ce}")

app = Flask(__name__, static_folder=".")
CORS(app)

# ── JSON sanitizer: replace Infinity / NaN with null so responses are valid JSON
import math as _math
from flask.json.provider import DefaultJSONProvider as _DJP

class _SafeJSONProvider(_DJP):
    def dumps(self, obj, **kw):
        def _clean(o):
            if isinstance(o, float):
                if _math.isnan(o) or _math.isinf(o):
                    return None
            elif isinstance(o, dict):
                return {k: _clean(v) for k, v in o.items()}
            elif isinstance(o, list):
                return [_clean(v) for v in o]
            return o
        return super().dumps(_clean(obj), **kw)

app.json_provider_class = _SafeJSONProvider
app.json = _SafeJSONProvider(app)

# ── API Keys ──────────────────────────────────────────────────────────────────
FINNHUB_KEY  = os.getenv("FINNHUB_API_KEY",  "")
QUIVER_KEY   = os.getenv("QUIVER_API_KEY",   "")
MASSIVE_KEY  = os.getenv("MASSIVE_API_KEY",  "")

# ── ntfy.sh Push Notifications ───────────────────────────────────────────────
# Uses ntfy.sh — zero extra packages, works on iOS + Android + desktop.
# Set NTFY_TOPIC in Railway Variables to enable (e.g. "market-genie-abc123")
NTFY_TOPIC   = os.getenv("NTFY_TOPIC",   "")
NTFY_SERVER  = os.getenv("NTFY_SERVER",  "https://ntfy.sh")  # self-host optionally

# Alert thresholds (overridable via Railway Variables)
ALERT_GAP_PCT      = float(os.getenv("ALERT_GAP_PCT",    "2.5"))
ALERT_KRONOS_SCORE = float(os.getenv("ALERT_KRONOS_SCORE","70"))
ALERT_VOLUME_MULT  = float(os.getenv("ALERT_VOLUME_MULT", "3.0"))

FINNHUB_BASE = "https://finnhub.io/api/v1"
QUIVER_BASE  = "https://api.quiverquant.com/beta"
MASSIVE_BASE = "https://api.massive.com"

def broadcast_push(title: str, body: str, url: str = "/", tag: str = "alert") -> int:
    """
    Send a push notification via ntfy.sh.
    Requires NTFY_TOPIC env var. Returns 1 on success, 0 on failure/disabled.
    """
    topic = os.getenv("NTFY_TOPIC", NTFY_TOPIC).strip()
    if not topic:
        return 0
    try:
        ntfy_url = f"{NTFY_SERVER.rstrip('/')}/{topic}"
        # HTTP headers must be Latin-1 — strip emojis from title for the header
        # but keep full emoji title + body in the JSON payload sent as body
        safe_title = title.encode("ascii", "ignore").decode("ascii").strip() or "Market Genie Alert"
        is_high = any(x in title for x in ["Gap Alert", "Kronos Signal", "Volume"])
        resp = requests.post(
            ntfy_url,
            data=body.encode("utf-8"),
            headers={
                "Title":    safe_title,
                "Priority": "high" if is_high else "default",
                "Tags":     tag,
                "Click":    url if url.startswith("http") else f"{NTFY_SERVER}/{topic}",
            },
            timeout=8
        )
        if resp.status_code == 200:
            print(f"[Push] Sent via ntfy: {title}")
            return 1
        else:
            print(f"[Push] ntfy error {resp.status_code}: {resp.text[:200]}")
            return 0
    except Exception as e:
        print(f"[Push] broadcast_push error: {e}")
        return 0

# ── Shared scanner universe (~200 curated names) ──────────────────────────────
# Used by Gap Scanner, Kronos Top Signals, and any future scanners.
# Covers mega-cap, semis, AI/cloud, fintech, crypto proxies, EV, biotech,
# ETFs (sector + leveraged), energy, meme, and international ADRs.
SCANNER_UNIVERSE = [
    # ── Index & Broad ETFs ──────────────────────────────────────────────────
    "SPY","QQQ","IWM","DIA","VXX","UVXY",
    # ── Mega Cap ────────────────────────────────────────────────────────────
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AVGO","JPM","V",
    "MA","UNH","XOM","WMT","LLY","JNJ","PG","HD","COST","NFLX",
    "BAC","ORCL","CRM","CVX","MRK","ABBV","TMO","ACN","ADBE","TXN",
    # ── Semiconductors ──────────────────────────────────────────────────────
    "AMD","INTC","QCOM","MU","AMAT","LRCX","KLAC","MRVL","ARM","SMCI",
    "ASML","TSM","ON","WOLF","CDNS","SNPS",
    # ── AI / Cloud / SaaS ───────────────────────────────────────────────────
    "PLTR","SNOW","DDOG","NET","CRWD","PANW","ZS","AI","PATH",
    "TWLO","HUBS","MDB","CFLT","S","GTLB","DOCN",
    # ── Consumer Tech / Platforms ───────────────────────────────────────────
    "SHOP","UBER","LYFT","DASH","ABNB","RDDT","RBLX","SNAP","PINS",
    "ROKU","ZM","DOCU","BILL","PTON","SPOT","TTD",
    # ── Fintech / Payments ──────────────────────────────────────────────────
    "COIN","HOOD","PYPL","AFRM","SOFI","NU",
    # ── Bitcoin / Crypto Proxies ────────────────────────────────────────────
    "MSTR","MARA","RIOT","CLSK","HUT","IREN","CIFR","BTBT",
    # ── EV / Auto ───────────────────────────────────────────────────────────
    "RIVN","LCID","F","GM","NIO","XPEV","LI",
    # ── China ADRs ──────────────────────────────────────────────────────────
    "BABA","JD","PDD","BIDU","KWEB",
    # ── Meme / High Short Interest ──────────────────────────────────────────
    "GME","AMC","BBAI","DJT","FFIE",
    # ── Quantum / Space / Emerging Tech ────────────────────────────────────
    "IONQ","QUBT","RGTI","LUNR","RKLB","SPCE","JOBY",
    # ── Sector ETFs ─────────────────────────────────────────────────────────
    "XLF","XLE","XLK","XLV","XLI","XLY","XLP","XLRE","XLB",
    "ARKK","GDX","GDXJ","ITB","IBB",
    # ── Leveraged / Inverse ETFs ────────────────────────────────────────────
    "SOXL","SOXS","TQQQ","SQQQ","SPXL","SPXS","TNA","TZA",
    "LABU","LABD","FNGU","FNGD","BULZ","BERZ",
    # ── Commodities & Bonds ─────────────────────────────────────────────────
    "GLD","SLV","USO","UNG","TLT","HYG","PDBC",
    # ── Energy ──────────────────────────────────────────────────────────────
    "OXY","DVN","HAL","SLB","RIG","FSLR","ENPH","SEDG","NEE","CEG",
    # ── Healthcare / Biotech ────────────────────────────────────────────────
    "MRNA","BNTX","NVAX","AMGN","GILD","BIIB","REGN","VRTX",
    "SRPT","HALO","NTLA","BEAM","EDIT","CRSP",
    # ── Consumer / Retail / Gaming ──────────────────────────────────────────
    "LULU","NKE","TGT","DKNG","PENN","MGM","WYNN","LVS",
    # ── Finance / Banks ─────────────────────────────────────────────────────
    "GS","MS","WFC","C","AXP","BRK-B","USB","SCHW",
    # ── Airlines / Travel ───────────────────────────────────────────────────
    "AAL","DAL","UAL","BA","CCL","NCLH","RCL",
    # ── Telecom / Media ─────────────────────────────────────────────────────
    "T","VZ","PARA","NFLX","DIS","WBD",
    # ── Cannabis ────────────────────────────────────────────────────────────
    "TLRY","SNDL","ACB",
]
# Deduplicate while preserving order
_seen_u = set()
_deduped = []
for _s in SCANNER_UNIVERSE:
    if _s not in _seen_u:
        _seen_u.add(_s)
        _deduped.append(_s)
SCANNER_UNIVERSE = _deduped

# ── Yahoo Finance crumb session (required for authenticated API calls) ────────
# Yahoo Finance requires a crumb (session token) + cookie for API access.
# We fetch it once at startup and refresh on 401s.
_yf_session    = None   # requests.Session with Yahoo cookies
_yf_crumb      = None   # crumb string

def _fetch_yf_crumb():
    """
    Establish a Yahoo Finance session and fetch the crumb.
    The crumb must be included as a query param on v8 API calls.
    Called once at startup; re-called automatically on 401 errors.
    """
    global _yf_session, _yf_crumb
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }
    try:
        s = requests.Session()
        s.headers.update(headers)
        # Step 1 — visit Yahoo Finance to acquire cookies
        r1 = s.get("https://finance.yahoo.com/", timeout=10)
        # Step 2 — exchange cookies for a crumb
        r2 = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10)
        if r2.status_code == 200 and r2.text and r2.text != "":
            _yf_session = s
            _yf_crumb   = r2.text.strip()
            print(f"[YF-Auth] Crumb acquired: {_yf_crumb[:6]}…")
            return True
        else:
            print(f"[YF-Auth] Crumb fetch got status {r2.status_code}: {r2.text[:80]}")
    except Exception as e:
        print(f"[YF-Auth] Crumb fetch error: {e}")
    return False

# Fetch crumb at startup (non-blocking — errors are handled gracefully in get_quote)
try:
    _fetch_yf_crumb()
except Exception as _ce:
    print(f"[YF-Auth] Startup crumb skipped: {_ce}")

# ── In-memory cache with TTL eviction ─────────────────────────────────────────
# Without eviction, _cache grows forever — stale entries stay in RAM long after
# their TTL expires, which is the primary cause of Railway OOM crashes.
_cache: dict = {}
_CACHE_MAX_SIZE = 400   # max live entries before forced eviction
_CACHE_HARD_TTL = 7200  # 2-hour hard ceiling — entries older than this are always removed

def _evict_cache() -> None:
    """Remove expired entries; if still oversized, drop oldest first."""
    now = time.time()
    expired = [k for k, v in list(_cache.items()) if now - v["ts"] > _CACHE_HARD_TTL]
    for k in expired:
        _cache.pop(k, None)
    # If still over limit, drop oldest entries to stay under cap
    if len(_cache) > _CACHE_MAX_SIZE:
        oldest = sorted(_cache.items(), key=lambda x: x[1]["ts"])
        for k, _ in oldest[: len(_cache) - _CACHE_MAX_SIZE]:
            _cache.pop(k, None)

def cached(key, ttl=30):
    """Return cached value if still fresh, else None."""
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < ttl:
        return entry["val"]
    return None

def set_cache(key, val):
    """Store value and evict stale/excess entries if cache is getting large."""
    if len(_cache) >= _CACHE_MAX_SIZE:
        _evict_cache()
    _cache[key] = {"val": val, "ts": time.time()}
    return val


# ── Finnhub helpers ────────────────────────────────────────────────────────────
def fh_get(path, params=None):
    """Call Finnhub REST API. Returns parsed JSON or {}."""
    if not FINNHUB_KEY:
        return {}
    p = params or {}
    p["token"] = FINNHUB_KEY
    try:
        r = requests.get(f"{FINNHUB_BASE}{path}", params=p, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[Finnhub] {path} error: {e}")
        return {}


# ── Massive helpers ────────────────────────────────────────────────────────────
# Auth: apiKey as query param (NOT Bearer header). API version: v2.
def massive_get(path, params=None):
    """Call Massive.com REST API v2. Returns parsed JSON or None."""
    if not MASSIVE_KEY:
        return None
    p = params or {}
    p["apiKey"] = MASSIVE_KEY          # correct auth method
    try:
        r = requests.get(f"{MASSIVE_BASE}{path}", params=p, timeout=8)
        if r.status_code == 403:
            print(f"[Massive] 403 on {path} — endpoint not in current plan")
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[Massive] {path} error: {e}")
        return None


def massive_snapshot(ticker):
    """Single ticker snapshot via Massive v2.
    NOTE: /v2/snapshot endpoints require an upgraded Massive plan (returns 403 on free).
    Returns None so callers fall back gracefully."""
    # Skip the API call — always 403 on current plan, saves ~300ms per scan
    return None


def massive_gainers_losers():
    """Top gainers/losers.
    NOTE: Massive snapshot/gainers endpoints are NOT in the current plan (403).
    Returns empty dict so api_gainers_losers() immediately falls back to yfinance."""
    return {"gainers": {}, "losers": {}}


def massive_daily_summary(date_str=None):
    """Daily OHLC for all stocks — INCLUDED in current Massive plan."""
    if not date_str:
        # Use previous trading day (skip weekends)
        d = datetime.utcnow()
        if d.weekday() == 0:   d -= timedelta(days=3)   # Monday → Friday
        elif d.weekday() == 6: d -= timedelta(days=2)   # Sunday → Friday
        else:                  d -= timedelta(days=1)
        date_str = d.strftime("%Y-%m-%d")
    key = f"massive_daily:{date_str}"
    if (v := cached(key, ttl=3600)): return v
    data = massive_get(f"/v2/aggs/grouped/locale/us/market/stocks/{date_str}", {"adjusted": "true"})
    return set_cache(key, data)


def get_quote(ticker):
    """
    Real-time quote — three-layer approach, all live sources only.

    Layer 1: yfinance fast_info — proven live, handles Yahoo auth internally,
             no disk cache, no library cache. This is the FASTEST and most
             reliable path on Windows.
    Layer 2: Yahoo Finance v8 REST API (direct) — requires crumb session.
    Layer 3: Finnhub REST — last resort.

    IMPORTANT: NEVER falls back to yf.Ticker.info / currentPrice (stale disk cache).

    Returns keys: c, d, dp, o, h, l, pc, v, avg_v
    """
    key = f"quote:{ticker}"
    if (v := cached(key, ttl=15)): return v

    # ── Layer 1: fast_info — live, no disk cache, yfinance handles auth ───────
    try:
        fi    = yf.Ticker(ticker).fast_info
        price = getattr(fi, 'last_price', None)
        prev  = (getattr(fi, 'previous_close', None)
                 or getattr(fi, 'regular_market_previous_close', None))
        if price and price > 0 and prev and prev > 0:
            open_p   = getattr(fi, 'open',     None) or prev
            day_high = getattr(fi, 'day_high', None) or price
            day_low  = getattr(fi, 'day_low',  None) or price
            volume   = int(getattr(fi, 'last_volume', None) or 0)
            avg_vol  = int(getattr(fi, 'three_month_average_volume', None) or 0)
            chg      = round(price - prev, 4)
            chg_pct  = round((chg / prev) * 100, 4)
            result = {
                "c": round(price, 4), "d": chg, "dp": chg_pct,
                "o": round(open_p, 4), "h": round(day_high, 4),
                "l": round(day_low, 4), "pc": round(prev, 4),
                "v": volume, "avg_v": avg_vol, "_source": "fast_info"
            }
            print(f"[Quote] {ticker} fast_info ${price:.2f} ({chg_pct:+.2f}%) vol={volume:,}")
            return set_cache(key, result)
        else:
            print(f"[Quote] {ticker} fast_info returned price={price} prev={prev} — trying next layer")
    except Exception as e:
        print(f"[Quote] {ticker} fast_info failed: {e}")

    # ── Layer 2: Yahoo Finance v8 REST API with crumb ─────────────────────────
    _yf_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://finance.yahoo.com/",
        "Origin": "https://finance.yahoo.com",
    }
    try:
        if not _yf_crumb:
            _fetch_yf_crumb()

        url    = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"interval": "1m", "range": "1d", "includePrePost": "false"}
        if _yf_crumb:
            params["crumb"] = _yf_crumb

        sess = _yf_session if _yf_session else requests
        r = sess.get(url, headers=_yf_headers, params=params, timeout=8)

        if r.status_code == 401:
            print(f"[Quote] {ticker} v8 401 — refreshing crumb")
            if _fetch_yf_crumb() and _yf_crumb:
                params["crumb"] = _yf_crumb
                r = (_yf_session if _yf_session else requests).get(
                    url, headers=_yf_headers, params=params, timeout=8)

        if r.status_code == 200:
            d = r.json()
            result_list = d.get("chart", {}).get("result", [])
            if result_list:
                meta  = result_list[0].get("meta", {})
                price = meta.get("regularMarketPrice", 0)
                prev  = meta.get("chartPreviousClose", 0) or meta.get("previousClose", 0)
                if price and price > 0:
                    open_p   = meta.get("regularMarketOpen",    prev)  or prev
                    day_high = meta.get("regularMarketDayHigh", price) or price
                    day_low  = meta.get("regularMarketDayLow",  price) or price
                    volume   = int(meta.get("regularMarketVolume", 0) or 0)
                    chg      = round(price - prev, 4) if prev else 0
                    chg_pct  = round((chg / prev) * 100, 4) if prev else 0
                    avg_vol  = 0
                    avg_key  = f"avgvol:{ticker}"
                    if (av := cached(avg_key, ttl=3600)):
                        avg_vol = av
                    else:
                        try:
                            qs_url    = f"https://query1.finance.yahoo.com/v11/finance/quoteSummary/{ticker}"
                            qs_params = {"modules": "summaryDetail"}
                            if _yf_crumb:
                                qs_params["crumb"] = _yf_crumb
                            qs_sess = _yf_session if _yf_session else requests
                            qs_r = qs_sess.get(qs_url, headers=_yf_headers,
                                               params=qs_params, timeout=5)
                            if qs_r.status_code == 200:
                                sd = (qs_r.json().get("quoteSummary", {})
                                                 .get("result", [{}])[0]
                                                 .get("summaryDetail", {}))
                                avg_vol = int(sd.get("averageVolume", {}).get("raw", 0) or 0)
                                set_cache(avg_key, avg_vol)
                        except Exception:
                            pass
                    result = {
                        "c": round(price, 4), "d": chg, "dp": chg_pct,
                        "o": round(open_p, 4), "h": round(day_high, 4),
                        "l": round(day_low, 4), "pc": round(prev, 4),
                        "v": volume, "avg_v": avg_vol, "_source": "yf_v8_direct"
                    }
                    print(f"[Quote] {ticker} v8-direct ${price:.2f} ({chg_pct:+.2f}%)")
                    return set_cache(key, result)
        print(f"[Quote] {ticker} v8 returned status {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"[Quote] {ticker} v8 direct failed: {e}")

    # ── Layer 3: Finnhub (15-min delayed on free tier) ────────────────────────
    data = fh_get("/quote", {"symbol": ticker})
    if data.get("c"):
        data["_source"] = "finnhub_delayed"
        print(f"[Quote] {ticker} finnhub ${data.get('c')} (15-min delay)")
    return set_cache(key, data)


def get_profile(ticker):
    key = f"profile:{ticker}"
    if (v := cached(key, ttl=3600)): return v
    data = fh_get("/stock/profile2", {"symbol": ticker})
    return set_cache(key, data)


def get_candles(ticker, resolution="5"):
    """Fetch intraday candles for today (or last trading day)."""
    key = f"candles:{ticker}:{resolution}"
    if (v := cached(key, ttl=60)): return v

    now = int(time.time())
    # Go back ~12 hours to capture today's session
    start = now - 60 * 60 * 12
    data = fh_get("/stock/candle", {
        "symbol": ticker,
        "resolution": resolution,
        "from": start,
        "to": now
    })
    return set_cache(key, data)


def get_company_news(ticker):
    key = f"news:{ticker}"
    if (v := cached(key, ttl=300)): return v
    today = datetime.utcnow().strftime("%Y-%m-%d")
    week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    data = fh_get("/company-news", {"symbol": ticker, "from": week_ago, "to": today})
    return set_cache(key, data or [])


# ── yfinance helpers ──────────────────────────────────────────────────────────
def get_yf_info(ticker):
    key = f"yf_info:{ticker}"
    if (v := cached(key, ttl=300)): return v
    try:
        t = yf.Ticker(ticker)
        info = t.info
        return set_cache(key, info)
    except Exception as e:
        print(f"[yfinance] info error for {ticker}: {e}")
        return {}


def get_pivot_points(ticker):
    """
    Calculate standard floor-trader pivot points from yesterday's OHLC.

    Formulas:
        PP = (H + L + C) / 3
        R1 = 2*PP - L    R2 = PP + (H - L)    R3 = H + 2*(PP - L)
        S1 = 2*PP - H    S2 = PP - (H - L)    S3 = L - 2*(H - PP)

    Returns a dict with pp, r1, r2, r3, s1, s2, s3 as floats,
    plus prev_h, prev_l, prev_c for reference.
    """
    key = f"pivots:{ticker}"
    if (v := cached(key, ttl=3600)): return v
    try:
        hist = yf.Ticker(ticker).history(period="5d", interval="1d")
        if hist is None or hist.empty or len(hist) < 2:
            return {}
        # Use the most recent fully-completed session (second-to-last row)
        prev = hist.iloc[-2]
        h = float(prev["High"])
        l = float(prev["Low"])
        c = float(prev["Close"])
        pp = round((h + l + c) / 3, 2)
        r  = round(h - l, 2)
        result = {
            "pp": pp,
            "r1": round(2 * pp - l, 2),
            "r2": round(pp + r,     2),
            "r3": round(h + 2 * (pp - l), 2),
            "s1": round(2 * pp - h, 2),
            "s2": round(pp - r,     2),
            "s3": round(l - 2 * (h - pp), 2),
            "prev_h": round(h, 2),
            "prev_l": round(l, 2),
            "prev_c": round(c, 2),
        }
        print(f"[Pivots] {ticker}  PP={pp}  R1={result['r1']}  S1={result['s1']}")
        return set_cache(key, result)
    except Exception as e:
        print(f"[Pivots] {ticker} failed: {e}")
        return {}


def get_extended_quote(ticker):
    """
    Fetch pre-market / after-hours price via Yahoo Finance v8 with
    includePrePost=true.

    Yahoo's v8 chart API does NOT expose preMarketPrice/postMarketPrice in the
    meta dict — those fields simply aren't returned.  Instead we detect which
    session we're in from currentTradingPeriod, then grab the last OHLCV candle
    whose timestamp falls inside that extended window.

    Returns:
        ext_price   – extended-hours last price (None during regular session)
        ext_chg_pct – % change from regular close
        ext_type    – 'pre' | 'post' | 'regular'
        reg_price   – regular-session closing price
    """
    import time as _time
    key = f"ext_quote:{ticker}"
    if (v := cached(key, ttl=30)): return v
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://finance.yahoo.com/",
        }
        params = {"interval": "1m", "range": "1d", "includePrePost": "true"}
        if _yf_crumb:
            params["crumb"] = _yf_crumb
        sess = _yf_session if _yf_session else requests
        r = sess.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            headers=headers, params=params, timeout=8
        )
        if r.status_code != 200:
            return {}

        result_data = r.json().get("chart", {}).get("result", [{}])[0]
        meta       = result_data.get("meta", {})
        timestamps = result_data.get("timestamp", [])
        closes     = result_data.get("indicators", {}).get("quote", [{}])[0].get("close", [])

        reg_price = meta.get("regularMarketPrice", 0) or 0
        trading   = meta.get("currentTradingPeriod", {})
        pre_start  = trading.get("pre",  {}).get("start", 0)
        pre_end    = trading.get("pre",  {}).get("end",   0)
        post_start = trading.get("post", {}).get("start", 0)
        post_end   = trading.get("post", {}).get("end",   0)

        now = int(_time.time())

        # Determine which extended session we're in right now
        if pre_start <= now < pre_end:
            session = "pre"
            win_start, win_end = pre_start, pre_end
        elif post_start <= now <= post_end:
            session = "post"
            win_start, win_end = post_start, post_end
        else:
            session = "regular"
            win_start, win_end = 0, 0

        ext_price = None
        ext_type  = "regular"

        if session != "regular" and timestamps:
            # Find last candle inside the extended window with a valid close
            ext_candles = [
                c for ts, c in zip(timestamps, closes)
                if win_start <= ts <= win_end and c is not None
            ]
            if ext_candles:
                ext_price = round(ext_candles[-1], 2)
                ext_type  = session

        if ext_price and reg_price:
            ext_chg_pct = round((ext_price - reg_price) / reg_price * 100, 2)
        else:
            ext_chg_pct = 0.0

        result = {
            "ext_price":   ext_price,
            "ext_chg_pct": ext_chg_pct,
            "ext_type":    ext_type,
            "reg_price":   round(reg_price, 2) if reg_price else None,
        }
        return set_cache(key, result)
    except Exception as e:
        print(f"[ExtQuote] {ticker} failed: {e}")
        return {}


def get_options_data(ticker):
    """
    Pull options chain from yfinance and identify UNUSUAL activity.

    "Unusual" = Vol/OI > 0.5  (more new contracts opened than existing OI —
    fresh positioning, not just rolling).  We scan the nearest 4 expiries so
    weekly sweeps aren't missed, then rank by estimated dollar premium.

    Returns:
        pcRatio        – put/call ratio (float)
        ivPct          – median IV as a readable pct string, e.g. "72%"
        totalCallVol   – int
        totalPutVol    – int
        optVol         – volume multiplier vs baseline, e.g. "2.3x"
        flowRows       – list of dicts: type, strike, premium, signal, iv
    """
    key = f"options:{ticker}"
    if (v := cached(key, ttl=120)): return v

    empty = {"pcRatio": "—", "ivPct": "—", "totalCallVol": 0,
             "totalPutVol": 0, "optVol": "—", "flowRows": []}

    try:
        t = yf.Ticker(ticker)
        exps = t.options
        if not exps:
            print(f"[Options] {ticker} — no expiries available")
            return set_cache(key, empty)

        # Scan up to 4 nearest expiries to catch weekly and front-month sweeps
        scan_exps = exps[:min(4, len(exps))]
        all_calls, all_puts = [], []

        for exp in scan_exps:
            try:
                chain = t.option_chain(exp)
                c = chain.calls.copy()
                p = chain.puts.copy()
                c["expiry"] = exp
                p["expiry"] = exp
                all_calls.append(c)
                all_puts.append(p)
            except Exception as ex:
                print(f"[Options] {ticker} {exp} chain error: {ex}")
                continue

        if not all_calls:
            return set_cache(key, empty)

        import pandas as pd
        calls = pd.concat(all_calls, ignore_index=True)
        puts  = pd.concat(all_puts,  ignore_index=True)

        # Fill NaN
        for df in [calls, puts]:
            df["volume"]           = df["volume"].fillna(0).astype(float)
            df["openInterest"]     = df["openInterest"].fillna(0).astype(float)
            df["impliedVolatility"] = df["impliedVolatility"].fillna(0).astype(float)
            df["lastPrice"]        = df["lastPrice"].fillna(0).astype(float)
            df["ask"]              = df.get("ask", pd.Series(dtype=float)).fillna(0).astype(float)
            df["bid"]              = df.get("bid", pd.Series(dtype=float)).fillna(0).astype(float)

        total_call_vol = int(calls["volume"].sum())
        total_put_vol  = int(puts["volume"].sum())
        pc_ratio = round(total_put_vol / total_call_vol, 2) if total_call_vol > 0 else 0

        # ── Unusualness filter: Vol/OI > 0.3 ──────────────────────────────────
        # A ratio > 0.3 means significant new positioning relative to existing OI.
        # We keep minimum volume of 10 to exclude penny-wide noise.
        def unusual_rows(df, side):
            df = df[df["volume"] >= 10].copy()
            df["vol_oi_ratio"] = df.apply(
                lambda r: r["volume"] / r["openInterest"] if r["openInterest"] > 0 else 1.5,
                axis=1
            )
            df = df[df["vol_oi_ratio"] >= 0.3]

            # Estimate dollar premium: contracts × 100 shares × mid_price
            def mid(r):
                if r["ask"] > 0 and r["bid"] > 0:
                    return (r["ask"] + r["bid"]) / 2
                return r["lastPrice"] or 0
            df["est_premium"] = df.apply(lambda r: mid(r) * r["volume"] * 100, axis=1)

            # Sort by est_premium descending
            df = df.sort_values("est_premium", ascending=False)
            rows = []
            for _, row in df.head(4).iterrows():
                iv_val = round(float(row["impliedVolatility"]) * 100, 1)
                prem   = row["est_premium"]
                if prem >= 1_000_000:
                    prem_str = f"${prem/1_000_000:.1f}M"
                elif prem >= 1_000:
                    prem_str = f"${prem/1_000:.0f}K"
                else:
                    prem_str = f"${prem:.0f}"
                vol_oi = row["vol_oi_ratio"]
                # Flag: sweep = vol/OI > 1.5, block = > 0.5, else unusual
                if vol_oi >= 1.5:   flag = "SWEEP"
                elif vol_oi >= 0.5: flag = "BLOCK"
                else:               flag = "UNUSUAL"
                exp_short = str(row.get("expiry", ""))[-5:].replace("-", "/")
                rows.append({
                    "type":    side,
                    "strike":  f"${row['strike']:.0f} {exp_short}",
                    "premium": prem_str,
                    "signal":  "BULL" if side == "CALL" else "BEAR",
                    "iv":      iv_val,
                    "flag":    flag,
                    "volOI":   f"{vol_oi:.1f}x"
                })
            return rows

        call_rows = unusual_rows(calls, "CALL")
        put_rows  = unusual_rows(puts,  "PUT")

        # Merge and take top 6 by implied premium size
        all_rows = call_rows + put_rows
        # If nothing passes the Vol/OI filter, fall back to pure top-volume
        if not all_rows:
            print(f"[Options] {ticker} — no unusual rows, falling back to top-volume")
            for df, side in [(calls, "CALL"), (puts, "PUT")]:
                for _, row in df.nlargest(3, "volume").iterrows():
                    iv_val = round(float(row["impliedVolatility"]) * 100, 1)
                    vol = int(row["volume"])
                    exp_short = str(row.get("expiry", ""))[-5:].replace("-", "/")
                    all_rows.append({
                        "type": side,
                        "strike": f"${row['strike']:.0f} {exp_short}",
                        "premium": f"Vol {vol:,}",
                        "signal": "BULL" if side == "CALL" else "BEAR",
                        "iv": iv_val,
                        "flag": "HIGH VOL",
                        "volOI": "—"
                    })

        flow_rows = all_rows[:8]  # max 8 rows in table

        # IV percentile approximation — median across all expirations
        median_iv = float(calls["impliedVolatility"].median()) if len(calls) > 0 else 0
        iv_pct_rank = min(int(median_iv * 100), 99)

        # Options volume multiplier vs a simple baseline (total vol / 500 per expiry)
        baseline = 500 * len(scan_exps)
        opt_total = total_call_vol + total_put_vol
        mult = round(opt_total / baseline, 1) if baseline > 0 else 1.0
        opt_vol_str = f"{mult:.1f}x"

        result = {
            "pcRatio":       pc_ratio,
            "ivPct":         f"{iv_pct_rank}%",
            "totalCallVol":  total_call_vol,
            "totalPutVol":   total_put_vol,
            "optVol":        opt_vol_str,
            "flowRows":      flow_rows,
            "expiry":        scan_exps[0]
        }
        print(f"[Options] {ticker} — {len(flow_rows)} unusual rows, P/C={pc_ratio}, IV={iv_pct_rank}%")
        return set_cache(key, result)

    except Exception as e:
        print(f"[Options] {ticker} error: {e}")
        return set_cache(key, empty)


# ── Analyst Sentiment (replaces StockTwits — blocked by Cloudflare) ───────────
def get_stocktwits_sentiment(ticker):
    """
    StockTwits API is permanently blocked server-side by Cloudflare.
    Replaced with yfinance analyst consensus + Reddit mention count as
    a combined sentiment signal.  Returns same field names so callers
    don't need to change.
    """
    key = f"st:{ticker}"
    if (v := cached(key, ttl=120)): return v

    bull_count = 0
    bear_count = 0
    top_msg    = ""
    total      = 0

    try:
        t = yf.Ticker(ticker)

        # ── Analyst recommendations (strongBuy / buy / hold / sell / strongSell)
        rec = t.recommendations
        if rec is not None and not rec.empty:
            # Most recent period
            latest = rec.iloc[0]
            strong_buy  = int(latest.get("strongBuy",  0) or 0)
            buy         = int(latest.get("buy",        0) or 0)
            hold        = int(latest.get("hold",       0) or 0)
            sell        = int(latest.get("sell",       0) or 0)
            strong_sell = int(latest.get("strongSell", 0) or 0)

            bull_count = strong_buy + buy
            bear_count = sell + strong_sell
            total      = bull_count + bear_count + hold

            # Build a readable summary as "topMessage"
            if total > 0:
                top_msg = (f"Analysts: {strong_buy} Strong Buy · {buy} Buy · "
                           f"{hold} Hold · {sell} Sell · {strong_sell} Strong Sell")
            print(f"[Analyst Sentiment] {ticker} — {bull_count}B / {bear_count}S / {hold}H")

        # ── Sentiment score: bulls / (bulls + bears), default neutral if no data
        sentiment_score = 0.5
        if bull_count + bear_count > 0:
            sentiment_score = bull_count / (bull_count + bear_count)

        # ── yfinance news headlines as fallback topMessage
        if not top_msg:
            try:
                news = t.news
                if news:
                    top_msg = news[0].get("content", {}).get("title", "") or news[0].get("title", "")
                    top_msg = top_msg[:200]
            except Exception:
                pass

        result = {
            "count":          total,
            "bullish":        bull_count,
            "bearish":        bear_count,
            "sentimentScore": round(sentiment_score, 2),
            "topMessage":     top_msg,
            "source":         "analyst_consensus",
        }
        return set_cache(key, result)

    except Exception as e:
        print(f"[Analyst Sentiment] {ticker} error: {e}")
        return set_cache(key, {
            "count": 0, "bullish": 0, "bearish": 0,
            "sentimentScore": 0.5, "topMessage": "", "source": "error"
        })


# ── Reddit ────────────────────────────────────────────────────────────────────
def get_reddit_mentions(ticker):
    key = f"reddit:{ticker}"
    if (v := cached(key, ttl=300)): return v
    headers = {"User-Agent": "MarketGenie/1.0"}
    results = {"wsb": 0, "options": 0, "catalyst": ""}

    try:
        # r/wallstreetbets
        url = f"https://www.reddit.com/r/wallstreetbets/search.json?q={ticker}&sort=new&t=day&limit=25"
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            posts = r.json().get("data", {}).get("children", [])
            results["wsb"] = len(posts)
            # Grab top post title as catalyst hint
            if posts:
                top = posts[0]["data"]
                results["catalyst"] = top.get("title", "")[:200]

        # r/options
        url2 = f"https://www.reddit.com/r/options/search.json?q={ticker}&sort=new&t=day&limit=10"
        r2 = requests.get(url2, headers=headers, timeout=5)
        if r2.status_code == 200:
            results["options"] = len(r2.json().get("data", {}).get("children", []))

    except Exception as e:
        print(f"[Reddit] error for {ticker}: {e}")

    return set_cache(key, results)


# ── Quiver Quant (Congress trades) ────────────────────────────────────────────
def get_congress_trades(ticker):
    key = f"congress:{ticker}"
    if (v := cached(key, ttl=900)): return v

    if not QUIVER_KEY:
        return set_cache(key, {"error": "No QUIVER_API_KEY set", "trades": []})

    try:
        url = f"{QUIVER_BASE}/historical/congresstrading/{ticker}"
        headers = {
            "Accept": "application/json",
            "X-CSRFToken": QUIVER_KEY,
            "Cookie": f"csrftoken={QUIVER_KEY}",
            "Authorization": f"Token {QUIVER_KEY}"
        }
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code != 200:
            return set_cache(key, {"error": f"HTTP {r.status_code}", "trades": []})

        all_trades = r.json()
        # Filter to last 60 days
        cutoff = (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%d")
        recent = [t for t in all_trades if t.get("Date", "") >= cutoff]
        recent = sorted(recent, key=lambda x: x.get("Date", ""), reverse=True)[:10]

        buys  = [t for t in recent if "purchase" in t.get("Transaction", "").lower()]
        sells = [t for t in recent if "sale" in t.get("Transaction", "").lower()]
        total = len(buys) + len(sells)
        buy_bias = round(len(buys) / total * 100) if total > 0 else 50

        formatted = []
        for t in recent[:5]:
            name  = t.get("Representative", "Unknown")
            parts = name.split()
            initials = (parts[0][0] + parts[-1][0]).upper() if len(parts) >= 2 else name[:2].upper()
            is_buy = "purchase" in t.get("Transaction", "").lower()
            formatted.append({
                "initials":  initials,
                "name":      name,
                "party":     t.get("Party", "?") + " · " + t.get("District", "?")[:2],
                "ticker":    ticker,
                "amount":    t.get("Range", "Unknown"),
                "type":      "BUY" if is_buy else "SELL",
                "date":      t.get("Date", "")[-5:].replace("-", "/")
            })

        result = {
            "buys":     len(buys),
            "sells":    len(sells),
            "buyBias":  buy_bias,
            "trades":   formatted
        }
        return set_cache(key, result)
    except Exception as e:
        print(f"[QuiverQuant] error for {ticker}: {e}")
        return {"error": str(e), "trades": []}


# ── Market Breadth (from public sources) ──────────────────────────────────────
def get_market_breadth():
    key = "breadth"
    if (v := cached(key, ttl=300)): return v

    result = {
        "uptrendRatio": "N/A",
        "adLine": "N/A",
        "hiLoRatio": "N/A",
        "breadthHistory": [],
        "sectorPerf": [],
        "vix": "—",
        "vixChg": 0.0,
        "vixLabel": "—",
        "spyPct": 0.0,
        "qqqPct": 0.0,
        "iwmPct": 0.0,
        "diaPct": 0.0,
        "regime": "—",
        "regimeClass": "neutral"
    }

    try:
        # ── VIX ──────────────────────────────────────────────────────────────
        try:
            vix_t = yf.Ticker("^VIX")
            vix_fi = vix_t.fast_info
            vix_price = vix_fi.last_price or 0
            vix_prev  = vix_fi.previous_close or vix_price
            vix_chg   = round(((vix_price - vix_prev) / vix_prev) * 100, 1) if vix_prev else 0
            result["vix"]    = f"{vix_price:.1f}"
            result["vixChg"] = vix_chg
            if vix_price < 15:
                result["vixLabel"] = "😴 Complacent"
            elif vix_price < 20:
                result["vixLabel"] = "😌 Low Fear"
            elif vix_price < 25:
                result["vixLabel"] = "😐 Moderate"
            elif vix_price < 30:
                result["vixLabel"] = "😟 Elevated"
            else:
                result["vixLabel"] = "🔥 Fear Zone"
        except Exception as e:
            print(f"[Breadth] VIX error: {e}")

        # ── Index ETF quotes ─────────────────────────────────────────────────
        index_etfs = {"SPY": "spyPct", "QQQ": "qqqPct", "IWM": "iwmPct", "DIA": "diaPct"}
        for sym, field in index_etfs.items():
            try:
                fi = yf.Ticker(sym).fast_info
                pct = round(((fi.last_price - fi.previous_close) / fi.previous_close) * 100, 2)
                result[field] = pct
            except:
                result[field] = 0.0

        # ── Market regime (based on SPY + VIX) ──────────────────────────────
        vix_val = float(result["vix"]) if result["vix"] != "—" else 20
        spy_pct = result["spyPct"]
        if vix_val < 20 and spy_pct > 0:
            result["regime"] = "🟢 Risk-On"
            result["regimeClass"] = "bull"
        elif vix_val > 30 or spy_pct < -1.5:
            result["regime"] = "🔴 Risk-Off"
            result["regimeClass"] = "bear"
        elif vix_val > 25 or spy_pct < -0.5:
            result["regime"] = "🟡 Caution"
            result["regimeClass"] = "neutral"
        else:
            result["regime"] = "🔵 Neutral"
            result["regimeClass"] = "neutral"

        # Sector ETFs as proxy for sector performance
        sector_etfs = {
            "Tech":    "XLK",
            "Energy":  "XLE",
            "Finance": "XLF",
            "Health":  "XLV",
            "Cons.":   "XLP",
            "Util.":   "XLU",
            "Mats.":   "XLB",
            "Indus.":  "XLI",
            "RE":      "XLRE"
        }

        sector_perf = []
        for name, etf in sector_etfs.items():
            try:
                q    = yf.Ticker(etf)
                hist = q.history(period="2d")
                if len(hist) >= 2:
                    prev  = float(hist["Close"].iloc[-2])
                    last  = float(hist["Close"].iloc[-1])
                    pct   = round(((last - prev) / prev) * 100, 2) if prev else 0.0
                elif len(hist) == 1:
                    # Only one bar — try fast_info as fallback
                    fi   = q.fast_info
                    prev = fi.previous_close or float(hist["Close"].iloc[0])
                    last = fi.last_price or float(hist["Close"].iloc[0])
                    pct  = round(((last - prev) / prev) * 100, 2) if prev else 0.0
                else:
                    pct = 0.0
                sector_perf.append({"name": name, "pct": pct})
            except Exception as _se:
                print(f"[Breadth] sector {etf} error: {_se}")
                sector_perf.append({"name": name, "pct": 0.0})

        result["sectorPerf"] = sector_perf

        # ── Breadth metrics using sector ETFs as proxy ────────────────────────
        # uptrendRatio: % of the 9 sector ETFs currently above 0% change (advancing)
        if sector_perf:
            advancing = sum(1 for s in sector_perf if s["pct"] > 0)
            declining = sum(1 for s in sector_perf if s["pct"] < 0)
            total     = len(sector_perf)
            uptrend_pct = round(advancing / total * 100)
            result["uptrendRatio"] = f"{uptrend_pct}%"
            result["adLine"]       = f"{advancing}/{declining}"    # e.g. "6/3"
        else:
            result["uptrendRatio"] = "—"
            result["adLine"]       = "—"

        # hiLoRatio: use SPY 52-week position as a proxy for new-highs ratio
        try:
            spy52 = yf.Ticker("SPY")
            spy_fi = spy52.fast_info
            spy_hi52 = getattr(spy_fi, "year_high", 0) or 0
            spy_lo52 = getattr(spy_fi, "year_low",  0) or 0
            spy_now  = getattr(spy_fi, "last_price", 0) or 0
            if spy_hi52 and spy_lo52 and spy_now:
                rng = spy_hi52 - spy_lo52
                pos = round((spy_now - spy_lo52) / rng * 100) if rng else 50
                result["hiLoRatio"] = f"{pos}%"   # e.g. "74%" = near highs
            else:
                result["hiLoRatio"] = "—"
        except Exception:
            result["hiLoRatio"] = "—"

        # breadthHistory: SPY position within a 20-day rolling range → meaningful variation
        try:
            spy_hist = yf.Ticker("SPY").history(period="90d")
            if not spy_hist.empty and len(spy_hist) >= 5:
                closes = spy_hist["Close"].tolist()
                history = []
                window_size = min(20, len(closes) - 1)
                for i in range(window_size, len(closes)):
                    window = closes[i-window_size:i+1]
                    lo, hi = min(window), max(window)
                    rng = hi - lo
                    pct = round((closes[i] - lo) / rng * 100) if rng else 50
                    history.append(round(30 + pct * 0.4, 1))  # maps 0–100% range to 30–70
                result["breadthHistory"] = history[-10:] if len(history) >= 10 else history
                print(f"[Breadth] SPY history: {len(closes)} bars → {len(result['breadthHistory'])} breadth pts (20d window)")
        except Exception as _bhe:
            print(f"[Breadth] breadthHistory error: {_bhe}")

    except Exception as e:
        print(f"[Breadth] error: {e}")

    return set_cache(key, result)


# ── Composite Score ───────────────────────────────────────────────────────────
def compute_composite(quote, options, social_st, reddit):
    score = 50  # Start neutral

    # Price momentum (25 pts)
    chg_pct = quote.get("dp", 0) or 0
    if chg_pct > 3:      score += 25
    elif chg_pct > 1:    score += 15
    elif chg_pct > 0:    score += 8
    elif chg_pct < -3:   score -= 25
    elif chg_pct < -1:   score -= 15
    elif chg_pct < 0:    score -= 8

    # Social sentiment (25 pts)
    st_score = social_st.get("sentimentScore", 0.5)
    score += int((st_score - 0.5) * 50)

    # Options flow (25 pts)
    pc = options.get("pcRatio", 0.7)
    if pc < 0.4:    score += 20
    elif pc < 0.6:  score += 10
    elif pc > 1.2:  score -= 20
    elif pc > 0.9:  score -= 10

    # Reddit buzz (5 pts)
    wsb_mentions = reddit.get("wsb", 0)
    if wsb_mentions > 20: score += 5
    elif wsb_mentions > 5: score += 2

    return max(0, min(100, score))


# ── Technical Indicators ──────────────────────────────────────────────────────
def compute_technicals(closes, highs, lows, volumes):
    """
    Calculate VWAP, RSI(14), MACD(12/26/9), Bollinger Bands(20,2),
    SMA(9/20/50) and Money Flow Index(14) from intraday bar data.
    All input lists must be the same length.

    Returns a dict of arrays aligned to the input bars.  Values that cannot
    yet be computed (e.g. first 13 bars of RSI) are returned as None so the
    frontend can hide them gracefully.
    """
    n = len(closes)
    if n < 2:
        return {}

    # ── VWAP ─────────────────────────────────────────────────────────────────
    # Cumulative sum of (typical_price × volume) / cumulative volume
    vwap = []
    cum_tpv = 0.0
    cum_vol = 0.0
    for i in range(n):
        tp = (highs[i] + lows[i] + closes[i]) / 3.0
        v  = max(volumes[i], 0)
        cum_tpv += tp * v
        cum_vol += v
        vwap.append(round(cum_tpv / cum_vol, 2) if cum_vol > 0 else closes[i])

    # ── Bollinger Bands (20-period SMA ± 2σ) ─────────────────────────────────
    bb_period = 20
    bb_upper  = [None] * n
    bb_middle = [None] * n
    bb_lower  = [None] * n
    for i in range(bb_period - 1, n):
        window = closes[i - bb_period + 1 : i + 1]
        sma = sum(window) / bb_period
        std = (sum((x - sma) ** 2 for x in window) / (bb_period - 1)) ** 0.5  # sample std dev (matches TradingView)
        bb_upper[i]  = round(sma + 2 * std, 2)
        bb_middle[i] = round(sma, 2)
        bb_lower[i]  = round(sma - 2 * std, 2)

    # ── RSI (14-period, Wilder smoothing) ─────────────────────────────────────
    rsi_period = 14
    rsi = [None] * n
    if n > rsi_period:
        deltas = [closes[i] - closes[i - 1] for i in range(1, n)]
        gains  = [max(d, 0.0) for d in deltas]
        losses = [max(-d, 0.0) for d in deltas]

        # Seed: simple average of first period
        avg_gain = sum(gains[:rsi_period]) / rsi_period
        avg_loss = sum(losses[:rsi_period]) / rsi_period

        def _rsi_val(ag, al):
            if al == 0:
                return 100.0
            return round(100.0 - (100.0 / (1.0 + ag / al)), 1)

        rsi[rsi_period] = _rsi_val(avg_gain, avg_loss)
        for i in range(rsi_period + 1, n):
            g = gains[i - 1]
            l = losses[i - 1]
            avg_gain = (avg_gain * (rsi_period - 1) + g) / rsi_period
            avg_loss = (avg_loss * (rsi_period - 1) + l) / rsi_period
            rsi[i] = _rsi_val(avg_gain, avg_loss)

    # ── MACD (12 EMA − 26 EMA, 9-period signal) ──────────────────────────────
    def _ema(values, period):
        """Exponential Moving Average; returns list with None until first value."""
        result = [None] * len(values)
        k = 2.0 / (period + 1)
        # Find first non-None value
        start = next((i for i, v in enumerate(values) if v is not None), None)
        if start is None:
            return result
        result[start] = float(values[start])
        for i in range(start + 1, len(values)):
            if values[i] is not None:
                prev = result[i - 1] if result[i - 1] is not None else values[i]
                result[i] = float(values[i]) * k + prev * (1 - k)
        return result

    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)

    macd_line = [None] * n
    for i in range(n):
        if ema12[i] is not None and ema26[i] is not None:
            macd_line[i] = round(ema12[i] - ema26[i], 4)

    macd_signal = _ema(macd_line, 9)
    macd_hist   = [None] * n
    for i in range(n):
        ml = macd_line[i]
        ms = macd_signal[i]
        if ml is not None and ms is not None:
            macd_hist[i] = round(ml - ms, 4)

    # ── Simple Moving Averages (9, 20, 50) ───────────────────────────────────
    def _sma(period):
        out = [None] * n
        for i in range(period - 1, n):
            out[i] = round(sum(closes[i - period + 1 : i + 1]) / period, 2)
        return out

    # ── Money Flow Index (14-period) ──────────────────────────────────────────
    # MFI = 100 − 100 / (1 + Positive Money Flow / Negative Money Flow)
    # Typical Price  = (High + Low + Close) / 3
    # Raw Money Flow = Typical Price × Volume
    # Positive MF    = sum of Raw MF on bars where TP > prev TP
    # Negative MF    = sum of Raw MF on bars where TP < prev TP
    mfi_period = 14
    mfi = [None] * n
    if n > mfi_period:
        # Pre-compute typical prices and raw money flows
        tp  = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]
        rmf = [tp[i] * max(volumes[i], 0) for i in range(n)]

        for i in range(mfi_period, n):
            pos_mf = 0.0
            neg_mf = 0.0
            for j in range(i - mfi_period + 1, i + 1):
                if j == 0:
                    continue
                if tp[j] > tp[j - 1]:
                    pos_mf += rmf[j]
                elif tp[j] < tp[j - 1]:
                    neg_mf += rmf[j]
                # tp[j] == tp[j-1] → neither (money flow is neutral)
            if neg_mf == 0:
                mfi[i] = 100.0
            elif pos_mf == 0:
                mfi[i] = 0.0
            else:
                mfr = pos_mf / neg_mf
                mfi[i] = round(100.0 - (100.0 / (1.0 + mfr)), 1)

    # ── TTM Squeeze (John Carter) ─────────────────────────────────────────────
    # Squeeze = Bollinger Bands inside Keltner Channels.
    # Dots: True (red)  = squeeze firing (coiled energy)
    #       False (gray) = squeeze released
    # Histogram = momentum: distance of price from midpoint of N-bar range + SMA(N)
    # Colors: +rising=lime, +falling=dark-green, -rising=orange, -falling=red
    sqz_period  = 20
    kc_mult     = 1.5   # ATR multiplier for Keltner Channels
    sqz_on      = [None] * n   # True/False: squeeze state per bar
    sqz_hist    = [None] * n   # momentum histogram value
    sqz_color   = [None] * n   # "lime","darkgreen","orange","red"

    if n >= sqz_period + 1:
        # True Range and ATR
        tr_list = []
        for i in range(1, n):
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i - 1]),
                     abs(lows[i]  - closes[i - 1]))
            tr_list.append(tr)

        # Keltner Channel (EMA ± kc_mult × ATR, both 20-period)
        ema20 = _ema(closes, sqz_period)

        for i in range(sqz_period, n):
            # ATR: simple average of last sqz_period true ranges
            atr_window = tr_list[max(0, i - sqz_period) : i]
            if not atr_window:
                continue
            atr = sum(atr_window) / len(atr_window)

            kc_upper = ema20[i] + kc_mult * atr  if ema20[i] else None
            kc_lower = ema20[i] - kc_mult * atr  if ema20[i] else None

            if bb_upper[i] is not None and kc_upper is not None:
                sqz_on[i] = (bb_upper[i] < kc_upper) and (bb_lower[i] > kc_lower)

            # Momentum: close − average of (highest_high + lowest_low)/2 and SMA
            hi20  = max(highs[i - sqz_period + 1 : i + 1])
            lo20  = min(lows [i - sqz_period + 1 : i + 1])
            delta = closes[i] - ((hi20 + lo20) / 2.0 + (bb_middle[i] or closes[i])) / 2.0
            sqz_hist[i] = round(delta, 4)

        # Assign colors based on value + direction
        for i in range(sqz_period, n):
            if sqz_hist[i] is None:
                continue
            val  = sqz_hist[i]
            prev = sqz_hist[i - 1] if sqz_hist[i - 1] is not None else 0
            if val >= 0:
                sqz_color[i] = "lime"      if val >= prev else "darkgreen"
            else:
                sqz_color[i] = "orange"    if val >= prev else "red"

    # ── VWAP Standard Deviation Bands (±1σ and ±2σ) ──────────────────────────
    # Used by professionals as dynamic support/resistance around VWAP.
    # σ = running std-dev of (typical_price − VWAP)
    vwap_u1 = [None] * n
    vwap_l1 = [None] * n
    vwap_u2 = [None] * n
    vwap_l2 = [None] * n

    if n >= 20:
        cum_tpv2 = 0.0
        cum_vol2 = 0.0
        sum_sq   = 0.0   # running sum of (tp - vwap)^2 × vol for std-dev
        for i in range(n):
            tp2 = (highs[i] + lows[i] + closes[i]) / 3.0
            v2  = max(volumes[i], 0)
            cum_tpv2 += tp2 * v2
            cum_vol2 += v2
            if cum_vol2 == 0:
                continue
            vwap_i = cum_tpv2 / cum_vol2
            sum_sq += v2 * (tp2 - vwap_i) ** 2
            if i >= 19 and cum_vol2 > 0:
                variance = sum_sq / cum_vol2
                sigma    = variance ** 0.5
                vwap_u1[i] = round(vwap_i + 1 * sigma, 2)
                vwap_l1[i] = round(vwap_i - 1 * sigma, 2)
                vwap_u2[i] = round(vwap_i + 2 * sigma, 2)
                vwap_l2[i] = round(vwap_i - 2 * sigma, 2)

    return {
        "vwap":        vwap,
        "bb_upper":    bb_upper,
        "bb_middle":   bb_middle,
        "bb_lower":    bb_lower,
        "rsi":         rsi,
        "macd_line":   [round(v, 4) if v is not None else None for v in macd_line],
        "macd_signal": [round(v, 4) if v is not None else None for v in macd_signal],
        "macd_hist":   [round(v, 4) if v is not None else None for v in macd_hist],
        "sma9":        _sma(9),
        "sma20":       _sma(20),
        "sma50":       _sma(50),
        "mfi":         mfi,
        # TTM Squeeze
        "sqz_on":      sqz_on,
        "sqz_hist":    sqz_hist,
        "sqz_color":   sqz_color,
        # VWAP Bands
        "vwap_u1":     vwap_u1,
        "vwap_l1":     vwap_l1,
        "vwap_u2":     vwap_u2,
        "vwap_l2":     vwap_l2,
    }


# ── Real-Time Prediction Engine ───────────────────────────────────────────────
def compute_prediction(price, chg_pct, quote, options, social, reddit, yf_info):
    """
    Multi-factor signal engine: Bull/Bear/Neutral + confidence + price targets.
    Inputs: price data, options flow, analyst consensus sentiment, Reddit mentions, yfinance info.
    Returns a prediction dict consumed by the frontend signal card.
    """
    if not price or price <= 0:
        return {
            "signal": "NEUTRAL", "signalIcon": "⚖", "signalColor": "#ffaa00",
            "signalBg": "rgba(255,170,0,0.08)", "confidence": 0,
            "socialScore": 0, "optionsScore": 0, "momentumScore": 0,
            "entryLow": "—", "entryHigh": "—", "stopLoss": "—",
            "target1": "—", "target2": "—", "riskReward": "—",
            "expectedMove": "—", "factors": ["Insufficient price data"],
        }

    direction_score = 0   # -100 to +100, positive = bullish
    factors = []

    # ── 1. SOCIAL SCORE (max ±35 pts) ────────────────────────────────────────
    st_sentiment = social.get("sentimentScore", 0.5)
    st_count     = social.get("count", 0)
    wsb_count    = reddit.get("wsb", 0)
    opt_count    = reddit.get("options", 0)

    st_dir = (st_sentiment - 0.5) * 60          # -30 to +30
    direction_score += st_dir

    if st_sentiment >= 0.70 and st_count >= 8:
        factors.append(f"🔥 Strong bullish analyst consensus ({int(st_sentiment*100)}% buy)")
        social_pts = 32
    elif st_sentiment >= 0.60 and st_count >= 4:
        factors.append(f"📈 Bullish analyst lean ({int(st_sentiment*100)}% buy)")
        social_pts = 22
    elif st_sentiment <= 0.30 and st_count >= 8:
        factors.append(f"📉 Bearish analyst consensus ({int((1-st_sentiment)*100)}% sell)")
        social_pts = 32
    elif st_sentiment <= 0.40 and st_count >= 4:
        factors.append(f"📉 Bearish analyst lean ({int((1-st_sentiment)*100)}% sell)")
        social_pts = 22
    elif st_count >= 4:
        factors.append(f"💬 Mixed analyst signals ({st_count} ratings)")
        social_pts = 10
    else:
        social_pts = 5

    if wsb_count >= 20:
        factors.append(f"⚡ Heavy WSB activity ({wsb_count} mentions today)")
        direction_score += 12
        social_pts = min(social_pts + 5, 35)
    elif wsb_count >= 8:
        factors.append(f"💬 Moderate WSB chatter ({wsb_count} mentions)")
        direction_score += 5
        social_pts = min(social_pts + 2, 35)

    if opt_count >= 5:
        factors.append(f"⚡ r/options buzz ({opt_count} mentions)")
    social_score = min(social_pts, 35)

    # ── 2. OPTIONS FLOW SCORE (max ±35 pts) ──────────────────────────────────
    pc_ratio  = options.get("pcRatio", None)
    iv_pct    = options.get("ivPct", 0) or 0
    total_cv  = options.get("totalCallVol", 0) or 0
    total_pv  = options.get("totalPutVol", 0) or 0

    options_pts = 0
    if isinstance(pc_ratio, (int, float)) and pc_ratio > 0:
        if pc_ratio < 0.40:
            direction_score += 35; options_pts = 35
            factors.append(f"🔥 Very low P/C ratio ({pc_ratio:.2f}) — heavy call buying")
        elif pc_ratio < 0.60:
            direction_score += 22; options_pts = 25
            factors.append(f"📈 Bullish options flow (P/C {pc_ratio:.2f})")
        elif pc_ratio < 0.80:
            direction_score += 10; options_pts = 15
            factors.append(f"📈 Slight call skew (P/C {pc_ratio:.2f})")
        elif pc_ratio < 1.00:
            options_pts = 8
        elif pc_ratio < 1.30:
            direction_score -= 10; options_pts = 15
            factors.append(f"📉 Slight put skew (P/C {pc_ratio:.2f})")
        elif pc_ratio < 1.60:
            direction_score -= 22; options_pts = 25
            factors.append(f"⚠ Bearish options flow (P/C {pc_ratio:.2f})")
        else:
            direction_score -= 35; options_pts = 35
            factors.append(f"⚠ Heavy put buying (P/C {pc_ratio:.2f})")
    else:
        options_pts = 0  # no options data
    options_score = min(options_pts, 35)

    # ── 3. PRICE & VOLUME MOMENTUM SCORE (max ±30 pts) ───────────────────────
    momentum_pts = 0

    # Today's price move
    if chg_pct >= 4:
        direction_score += 28; momentum_pts = 28
        factors.append(f"🚀 Strong upside momentum (+{chg_pct:.1f}% today)")
    elif chg_pct >= 2:
        direction_score += 18; momentum_pts = 20
        factors.append(f"📈 Positive momentum (+{chg_pct:.1f}% today)")
    elif chg_pct >= 0.5:
        direction_score += 8;  momentum_pts = 12
        factors.append(f"📈 Upward drift (+{chg_pct:.1f}% today)")
    elif chg_pct <= -4:
        direction_score -= 28; momentum_pts = 28
        factors.append(f"📉 Strong selling pressure ({chg_pct:.1f}% today)")
    elif chg_pct <= -2:
        direction_score -= 18; momentum_pts = 20
        factors.append(f"📉 Negative momentum ({chg_pct:.1f}% today)")
    elif chg_pct <= -0.5:
        direction_score -= 8;  momentum_pts = 12
        factors.append(f"📉 Downward drift ({chg_pct:.1f}% today)")
    else:
        momentum_pts = 5
        factors.append("⚖ Price action flat/neutral today")

    # Volume confirmation
    avg_v   = yf_info.get("averageVolume", 0) or 1
    today_v = yf_info.get("volume", 0) or 0
    vol_ratio = today_v / avg_v if avg_v else 1
    if vol_ratio > 2.5:
        factors.append(f"📊 Volume {vol_ratio:.1f}x avg — very strong conviction")
        momentum_pts = min(momentum_pts + 5, 30)
        direction_score += (10 if chg_pct >= 0 else -10)
    elif vol_ratio > 1.5:
        factors.append(f"📊 Above-average volume ({vol_ratio:.1f}x)")
        momentum_pts = min(momentum_pts + 2, 30)
        direction_score += (5 if chg_pct >= 0 else -5)

    # MA structure
    ma50  = yf_info.get("fiftyDayAverage", 0) or 0
    ma200 = yf_info.get("twoHundredDayAverage", 0) or 0
    above_50  = price > ma50  if ma50  else True
    above_200 = price > ma200 if ma200 else True
    if above_50 and above_200:
        direction_score += 8; momentum_pts = min(momentum_pts + 3, 30)
        factors.append("✅ Above 50-day & 200-day MA — bullish structure")
    elif not above_50 and not above_200:
        direction_score -= 8
        factors.append("❌ Below 50-day & 200-day MA — bearish structure")
    elif not above_50:
        direction_score -= 4
        factors.append("⚠ Below 50-day MA — weakening trend")

    # Short squeeze potential
    short_pct = yf_info.get("shortPercentOfFloat", 0) or 0
    if short_pct > 0.15 and chg_pct > 1:
        factors.append(f"⚡ Short squeeze setup: {short_pct*100:.0f}% float short + rising price")
        direction_score += 12
        momentum_pts = min(momentum_pts + 4, 30)

    momentum_score = min(momentum_pts, 30)

    # ── FINAL SIGNAL ─────────────────────────────────────────────────────────
    if direction_score >= 18:
        signal = "BULLISH"; signal_icon = "▲"
        signal_color = "#00ff88"; signal_bg = "rgba(0,255,136,0.08)"
    elif direction_score <= -18:
        signal = "BEARISH"; signal_icon = "▼"
        signal_color = "#ff4455"; signal_bg = "rgba(255,68,85,0.08)"
    else:
        signal = "NEUTRAL"; signal_icon = "⚖"
        signal_color = "#ffaa00"; signal_bg = "rgba(255,170,0,0.08)"

    # Confidence: weighted sum of component scores, capped 10-95
    raw = social_score + options_score + momentum_score   # max 100
    if signal == "NEUTRAL":
        confidence = min(int(raw * 0.45 + 8), 48)         # cap neutral lower
    else:
        confidence = min(int(raw * 0.75 + 15), 95)
    confidence = max(confidence, 10)

    # ── PRICE TARGETS ─────────────────────────────────────────────────────────
    day_h = quote.get("h", 0) or price
    day_l = quote.get("l", 0) or price
    atr = max(day_h - day_l, price * 0.005)

    # Blend with IV-based expected move if available
    if iv_pct and iv_pct > 0:
        iv_daily = price * (iv_pct / 100) / (252 ** 0.5)
        atr = max(atr, iv_daily)

    if signal == "BULLISH":
        entry_low  = round(price * 0.9975, 2)
        entry_high = round(price * 1.0025, 2)
        stop_loss  = round(max(price - atr * 1.5, price * 0.97), 2)
        target1    = round(price + atr * 2.0, 2)
        target2    = round(price + atr * 3.5, 2)
        rr_num = (target1 - price) / max(price - stop_loss, 0.01)
    elif signal == "BEARISH":
        entry_low  = round(price * 0.9975, 2)
        entry_high = round(price * 1.0025, 2)
        stop_loss  = round(min(price + atr * 1.5, price * 1.03), 2)
        target1    = round(price - atr * 2.0, 2)
        target2    = round(price - atr * 3.5, 2)
        rr_num = (price - target1) / max(stop_loss - price, 0.01)
    else:
        entry_low  = round(price * 0.9975, 2)
        entry_high = round(price * 1.0025, 2)
        stop_loss  = round(price - atr * 1.2, 2)
        target1    = round(price + atr * 1.5, 2)
        target2    = round(price + atr * 2.5, 2)
        rr_num = (target1 - price) / max(price - stop_loss, 0.01)

    expected_move_pct = round((atr / price) * 100, 2)
    rr_str = f"1:{rr_num:.1f}" if rr_num > 0 else "—"

    # Limit to top 4 most informative factors
    factors = factors[:4] if factors else ["⚖ Mixed signals — no clear edge"]

    return {
        "signal":        signal,
        "signalIcon":    signal_icon,
        "signalColor":   signal_color,
        "signalBg":      signal_bg,
        "confidence":    confidence,
        "socialScore":   social_score,
        "optionsScore":  options_score,
        "momentumScore": momentum_score,
        "entryLow":      f"${entry_low:.2f}",
        "entryHigh":     f"${entry_high:.2f}",
        "stopLoss":      f"${stop_loss:.2f}",
        "target1":       f"${target1:.2f}",
        "target2":       f"${target2:.2f}",
        "riskReward":    rr_str,
        "expectedMove":  f"±{expected_move_pct}%",
        "factors":       factors,
        "dirScore":      direction_score,
    }


# ── Social Market Scan ────────────────────────────────────────────────────────

# Common non-ticker uppercase words to filter out
_TICKER_EXCLUDE = {
    'A','I','AM','AN','AS','AT','BE','BY','DO','GO','HE','IF','IN','IS','IT',
    'ME','MY','NO','OF','OK','ON','OR','SO','TO','UP','US','WE',
    'ADD','AGO','ALL','AND','ARE','ATH','ATL','ATM','AVG','BAD','BIG',
    'BOT','BUY','CAN','CEO','CFO','COO','CTO','DAY','DCA','DID','DOWN',
    'EPS','EST','ETF','FED','FOR','GDP','GET','GOT','GUY','HAS','HAD',
    'HOW','IMO','IRS','ITS','LOL','LOT','LOW','MAX','MIN','MOM','NEW',
    'NOT','NOW','OLD','OUR','OUT','OWN','PAY','PE','PUT','QOQ','RED',
    'RIP','SEC','TAX','THE','TBH','TOO','TWO','USE','USD','WAS','WAY',
    'WHO','WHY','WIN','WTF','YOY','YTD','WSB','YOLO','FOMO','TLDR',
    'AFAIK','CNBC','NEWS','CALL','PUTS','BEAR','BULL','GAIN','LOSS',
    'HOLD','SOLD','SELL','LONG','SHORT','MOON','PUMP','DUMP','HIGH',
    'JUST','THAT','THIS','WITH','YOUR','FROM','HAVE','MORE','SOME',
    'BEEN','THEY','WILL','WHAT','WHEN','WERE','YEAR','LAST','WEEK',
    'NEXT','ALSO','LIKE','MOST','GOOD','SAME','ONLY','BOTH','EVEN',
    'MUCH','SUCH','INTO','DOES','OVER','THAN','VERY','WELL','MAKE',
    'TAKE','WANT','LOOK','REAL','MANY','BACK','MADE','GIVE','KNOW',
    'WENT','FEEL','THINK','COULD','WOULD','MIGHT','THEIR','THERE',
    'WHERE','WHICH','ABOUT','OTHER','STILL','FIRST','AFTER','SINCE',
    'EVERY','WHILE','THESE','THOSE','UNTIL','UNDER','POINT','PRICE',
    'SHARE','STOCK','TRADE','MONEY','TIMES','DOING','GOING','BEING',
    'THING','GREAT','RIGHT','SMALL','LARGE','MAYBE','LATER','EARLY',
    'DAILY','TODAY','CHART','CALLS','ABOVE','BELOW','BASED','MOVES',
    'BONDS','CASH','RATE','RATES','GAINS','RISKS','RISK','PUTS','CALL',
    'DOWN','THEN','SAID','CAME','TAKE','COME','AFTER','NEVER','ALWAYS',
    'OPEN','CLOSE','HIGH','LOWS','BULL','BEAR','FLAT','LOSS','YEAR',
    'WEEK','DAYS','HOUR','PLAY','IDEA','POST','LINK','MORE','LESS',
    'HELP','NEED','WANT','KNOW','TELL','SHOW','GIVE','KEEP','FIND',
    'SEEM','LOOK','FEEL','COME','HOLD','SELL','STOP','MOVE','DROP',
    'RISE','GROW','FALL','JUMP','PUMP','DUMP','PUSH','PULL','WAIT',
    'STAY','TURN','GROW','JOIN','VOTE','EDIT','FWIW','IIRC','ASAP',
    'IMHO','LMAO','LMFAO','ROFL','AFAICT','YMMV','TIL','ELI5','AMA',
}


def get_reddit_trending_tickers():
    """
    Extract trending tickers directly from Reddit hot posts across finance subreddits.
    Used as fallback when ApeWisdom is unavailable.
    Returns data in same format as get_apewisdom_trending().
    """
    key = "reddit_trending_tickers"
    if (v := cached(key, ttl=180)): return v

    subreddits = ["wallstreetbets", "stocks", "investing", "options", "stockmarket", "pennystocks", "Daytrading"]
    headers = {"User-Agent": "MarketGenie/1.0 (market data aggregator)"}
    mention_counts = {}   # ticker → count
    dollar_counts   = {}  # ticker → $-mention count (higher confidence)

    for sub in subreddits:
        try:
            url = f"https://www.reddit.com/r/{sub}/hot.json?limit=30"
            r = requests.get(url, headers=headers, timeout=6)
            if r.status_code != 200:
                print(f"[Reddit Trending] r/{sub} returned {r.status_code}")
                continue
            posts = r.json().get("data", {}).get("children", [])
            for p in posts:
                title = p.get("data", {}).get("title", "")
                text  = p.get("data", {}).get("selftext", "")
                combined = title + " " + text[:500]

                # $TICKER pattern — highest confidence
                for t in re.findall(r'\$([A-Z]{1,5})\b', combined):
                    if t not in _TICKER_EXCLUDE and len(t) >= 2:
                        dollar_counts[t] = dollar_counts.get(t, 0) + 1
                        mention_counts[t] = mention_counts.get(t, 0) + 3

                # Bare UPPERCASE words 2-5 chars (lower confidence)
                for t in re.findall(r'\b([A-Z]{2,5})\b', combined):
                    if t not in _TICKER_EXCLUDE and len(t) >= 2:
                        mention_counts[t] = mention_counts.get(t, 0) + 1

        except Exception as e:
            print(f"[Reddit Trending] error for r/{sub}: {e}")
            continue

    if not mention_counts:
        print("[Reddit Trending] No tickers extracted — Reddit may be throttling")
        return set_cache(key, [])

    # Filter: must appear ≥2 times, or have at least one $ mention
    filtered = {t: c for t, c in mention_counts.items()
                if c >= 2 or dollar_counts.get(t, 0) >= 1}

    sorted_tickers = sorted(filtered.items(), key=lambda x: x[1], reverse=True)[:15]

    result = []
    for rank, (ticker, mentions) in enumerate(sorted_tickers, 1):
        # Approximate velocity: tickers with $ prefix signal are fresher/hotter
        dollar_boost = dollar_counts.get(ticker, 0)
        velocity = min(round(dollar_boost * 15 + (mentions - 2) * 3, 1), 200)
        result.append({
            "ticker":        ticker,
            "name":          ticker,
            "mentions":      mentions,
            "mentions_prev": max(0, mentions - 5),
            "upvotes":       0,
            "velocity":      velocity,
            "rank":          rank,
        })

    print(f"[Reddit Trending] Extracted {len(result)} tickers: {[t['ticker'] for t in result[:5]]}")
    return set_cache(key, result)


def get_apewisdom_trending():
    """ApeWisdom public API — no auth needed. Returns top tickers across Reddit.
    Priority: WSB-only (most reliable) → all-finance → Reddit direct extraction.
    NOTE: the all-finance endpoint often returns 0 results; WSB-only is the working endpoint."""
    key = "apewisdom_trending"
    if (v := cached(key, ttl=120)): return v
    headers = {"User-Agent": "MarketGenie/1.0"}
    try:
        # PRIMARY: WSB-only — consistently returns 100 results
        url = "https://apewisdom.io/api/v1.0/filter/wallstreetbets/page/1"
        r = requests.get(url, headers=headers, timeout=6)
        if r.status_code != 200 or not r.json().get("results"):
            # SECONDARY: all-finance (sometimes returns 0, try anyway)
            url2 = "https://apewisdom.io/api/v1.0/filter/all-finance-subreddits/page/1"
            r = requests.get(url2, headers=headers, timeout=6)
        if r.status_code != 200:
            print(f"[ApeWisdom] Both endpoints unavailable — using Reddit direct extraction")
            return get_reddit_trending_tickers()
        data = r.json()
        results = data.get("results", [])
        if not results:
            print("[ApeWisdom] Both endpoints returned empty results — using Reddit direct extraction")
            return get_reddit_trending_tickers()
        tickers = []
        for item in results[:15]:
            ticker = item.get("ticker", "")
            if not ticker or len(ticker) > 5:
                continue
            mentions_now  = item.get("mentions", 0)
            mentions_prev = item.get("mentions_24h_ago", 0)
            upvotes       = item.get("upvotes", 0)
            velocity = 0
            if mentions_prev and mentions_prev > 0:
                velocity = round(((mentions_now - mentions_prev) / mentions_prev) * 100, 1)
            elif mentions_now > 0:
                velocity = 100  # new appearance
            tickers.append({
                "ticker":   ticker,
                "name":     item.get("name", ticker),
                "mentions": mentions_now,
                "mentions_prev": mentions_prev,
                "upvotes":  upvotes,
                "velocity": velocity,
                "rank":     item.get("rank", 99),
            })
        print(f"[ApeWisdom] OK — {len(tickers)} tickers")
        return set_cache(key, tickers)
    except Exception as e:
        print(f"[ApeWisdom] error: {e} — falling back to Reddit direct extraction")
        return get_reddit_trending_tickers()


def get_stocktwits_trending():
    """
    StockTwits is permanently blocked server-side by Cloudflare.
    Replaced with Reddit r/stocks trending tickers as equivalent signal.
    Returns same field shape so social_market_scan() needs no changes.
    """
    key = "st_trending"
    if (v := cached(key, ttl=120)): return v
    try:
        headers = {"User-Agent": "MarketGenie/1.0 (market data aggregator)"}
        # Scan r/stocks hot posts for ticker mentions
        r = requests.get("https://www.reddit.com/r/stocks/hot.json?limit=30",
                         headers=headers, timeout=6)
        if r.status_code != 200:
            print(f"[Reddit Stocks Trending] HTTP {r.status_code}")
            return set_cache(key, [])

        posts = r.json().get("data", {}).get("children", [])
        counts = {}
        for p in posts:
            text = (p["data"].get("title", "") + " " + p["data"].get("selftext", "")[:300])
            for ticker in re.findall(r'\$([A-Z]{1,5})\b', text):
                if ticker not in _TICKER_EXCLUDE:
                    counts[ticker] = counts.get(ticker, 0) + 2
            for ticker in re.findall(r'\b([A-Z]{2,5})\b', text):
                if ticker not in _TICKER_EXCLUDE:
                    counts[ticker] = counts.get(ticker, 0) + 1

        result = []
        for ticker, count in sorted(counts.items(), key=lambda x: -x[1])[:12]:
            result.append({
                "ticker":          ticker,
                "name":            ticker,
                "watchlist_count": count,
            })
        print(f"[Reddit Stocks Trending] {len(result)} tickers: {[r['ticker'] for r in result[:5]]}")
        return set_cache(key, result)
    except Exception as e:
        print(f"[Reddit Stocks Trending] error: {e}")
        return set_cache(key, [])


def get_wsb_hot_posts(force=False):
    """Fetch WSB hot posts via Reddit public JSON API."""
    key = "wsb_hot"
    if not force and (v := cached(key, ttl=60)): return v
    try:
        url = "https://www.reddit.com/r/wallstreetbets/hot.json?limit=25"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.reddit.com/",
        }
        r = requests.get(url, headers=headers, timeout=8)
        print(f"[WSB Hot] Reddit status: {r.status_code}")
        if r.status_code != 200:
            print(f"[WSB Hot] Non-200 response body: {r.text[:300]}")
            # Don't cache failures — allow retry on next force call
            return _cache.get(key, {}).get("val", [])
        posts = r.json().get("data", {}).get("children", [])
        result = []
        for p in posts:
            d = p.get("data", {})
            title = d.get("title", "")
            flair = d.get("link_flair_text", "")
            ups   = d.get("ups", 0)
            comments = d.get("num_comments", 0)
            # Only skip pinned mod/announcement posts (not daily discussion — those are valuable)
            if d.get("stickied", False) or d.get("distinguished") == "moderator":
                continue
            result.append({
                "title":    title[:120],
                "flair":    flair,
                "ups":      ups,
                "comments": comments,
                "url":      "https://reddit.com" + d.get("permalink", ""),
            })
        print(f"[WSB Hot] Got {len(result)} posts")
        return set_cache(key, result[:8])
    except Exception as e:
        print(f"[WSB Hot] error: {e}")
        return _cache.get(key, {}).get("val", [])


def compute_market_mood(ape_tickers, st_tickers):
    """Derive an overall market mood score 0-100 from social data."""
    if not ape_tickers and not st_tickers:
        return 50, "💬 Neutral", "neutral"

    # Check velocity distribution — more positive = bullish mood
    velocities = [t["velocity"] for t in ape_tickers if t.get("velocity") is not None]
    if not velocities:
        return 50, "💬 Neutral", "neutral"

    avg_vel = sum(velocities) / len(velocities)
    positive_count = sum(1 for v in velocities if v > 10)
    negative_count = sum(1 for v in velocities if v < -10)
    ratio = positive_count / max(1, positive_count + negative_count)

    score = 50 + int((ratio - 0.5) * 60)
    score = max(10, min(90, score))

    if score >= 70:   label, cls = "🔥 Risk-On",   "hot"
    elif score >= 58: label, cls = "📈 Bullish",    "bull"
    elif score >= 42: label, cls = "💬 Mixed",      "neutral"
    elif score >= 30: label, cls = "📉 Cautious",   "bear"
    else:             label, cls = "🧊 Risk-Off",   "bear"

    return score, label, cls


@app.route("/api/social/market-scan")
def social_market_scan():
    """Full market-wide social sentiment scan. Used by the Social Sentiment panel."""
    force = request.args.get("force", "false").lower() == "true"
    cache_key = "social_market_scan"
    if not force and (v := cached(cache_key, ttl=90)): return jsonify(v)

    print(f"[Social] Running market-wide social scan (force={force})...")

    ape      = get_apewisdom_trending()
    st_trend = get_stocktwits_trending()
    wsb_posts = get_wsb_hot_posts(force=force)

    # Merge ApeWisdom + StockTwits watchlist counts
    st_map = {s["ticker"]: s["watchlist_count"] for s in st_trend}
    for t in ape:
        t["st_watchlist"] = st_map.get(t["ticker"], 0)

    # Compute momentum tag per ticker
    for t in ape:
        v = t.get("velocity", 0)
        if v > 100:   t["signal"] = "⚡ SPIKE"
        elif v > 20:  t["signal"] = "🔥 HOT"
        elif v > 5:   t["signal"] = "📈 Rising"
        elif v < -20: t["signal"] = "📉 Fading"
        elif v < -5:  t["signal"] = "🔻 Cooling"
        else:         t["signal"] = "💬 Stable"

    mood_score, mood_label, mood_class = compute_market_mood(ape, st_trend)

    # Top ticker for headline
    top = ape[0] if ape else {}

    # Build data source label based on what actually returned data
    sources = []
    if ape:
        # Check if this came from ApeWisdom (has upvotes field populated) or Reddit fallback
        sources.append("Reddit Trending" if ape[0].get("upvotes", 0) == 0 else "ApeWisdom")
    if st_trend:
        sources.append("StockTwits")
    sources.append("Reddit WSB")
    data_source = " + ".join(sources) if sources else "Reddit WSB"

    result = {
        "timestamp":   datetime.utcnow().strftime("%H:%M UTC"),
        "tickers":     ape[:10],
        "stTrending":  st_trend[:8],
        "wsbPosts":    wsb_posts,
        "moodScore":   mood_score,
        "moodLabel":   mood_label,
        "moodClass":   mood_class,
        "topTicker":   top.get("ticker", "—"),
        "topMentions": top.get("mentions", 0),
        "topVelocity": top.get("velocity", 0),
        "dataSource":  data_source,
    }
    print(f"[Social Scan] Done — {len(ape)} tickers, mood={mood_score} ({mood_label}), sources: {data_source}")
    return jsonify(set_cache(cache_key, result))


# ── Main scan endpoint ─────────────────────────────────────────────────────────
@app.route("/api/scan/<ticker>")
def scan_ticker(ticker):
    ticker = ticker.upper().strip()
    cache_key = f"scan:{ticker}"
    if (v := cached(cache_key, ttl=10)): return jsonify(v)

    print(f"[Scan] {ticker} — fetching live data...")

    # Top-level safety net: if ANYTHING goes wrong, still return a valid price response
    try:
        return _scan_ticker_inner(ticker, cache_key)
    except Exception as fatal:
        import traceback
        print(f"[Scan] {ticker} FATAL: {fatal}")
        traceback.print_exc()
        # Fall back to price-only response so the hero always shows correct data
        try:
            q = get_quote(ticker)
            price = q.get("c", 0)
            chg   = q.get("d", 0)
            dp    = q.get("dp", 0)
            minimal = {
                "ticker": ticker, "name": ticker,
                "price": f"{price:.2f}", "chg": f"{chg:+.2f}",
                "chgPct": f"{dp:+.2f}%", "dir": "up" if dp >= 0 else "down",
                "vol": "—", "avgVol": "—", "mktCap": "—", "float": "—",
                "composite": 50, "compositeColor": "#ffaa00",
                "socialTag": "—", "socialTagClass": "", "sentiment": 0.5,
                "sentimentLabel": "—", "wsb": 0, "st": 0, "tw": 0, "opt": 0,
                "catalyst": f"Partial data only — scan error: {str(fatal)[:80]}",
                "news": [], "sectorPerf": [], "priceData": {"labels": [], "prices": [], "volumes": []},
                "prediction": None,
            }
            print(f"[Scan] {ticker} returning price-only fallback: ${price:.2f}")
            return jsonify(minimal)
        except Exception as e2:
            return jsonify({"error": str(fatal), "ticker": ticker, "price": "0.00"}), 500


def _scan_ticker_inner(ticker, cache_key):
    """Full scan logic — called by scan_ticker with a top-level safety net."""

    # Each sub-call is individually guarded so one failure never crashes the whole scan
    def safe(fn, *args, default=None):
        try:
            return fn(*args)
        except Exception as e:
            print(f"[Scan] {ticker} sub-call {fn.__name__} failed: {e}")
            return default if default is not None else {}

    # ── Fire all requests in PARALLEL for speed on slow servers ─────────────
    # Options are always loaded separately via /api/options/<ticker>
    options = {}   # prediction engine handles empty options gracefully
    from concurrent.futures import ThreadPoolExecutor, as_completed as _asc

    def _yf_fast_info():
        info = {}
        try:
            fi_tmp = yf.Ticker(ticker).fast_info
            shares  = int(getattr(fi_tmp, 'shares', None) or 0)
            volume  = int(getattr(fi_tmp, 'last_volume', None) or 0)
            avg_vol = int(getattr(fi_tmp, 'three_month_average_volume', None) or 0)
            if shares:  info["sharesOutstanding"] = shares
            if volume:  info["volume"]  = volume
            if avg_vol: info["averageVolume"] = avg_vol
        except Exception as yfe:
            print(f"[Scan] {ticker} fast_info supplemental failed: {yfe}")
        return info

    _tasks = {
        "quote":    lambda: safe(get_quote,                 ticker),
        "profile":  lambda: safe(get_profile,               ticker),
        "candles":  lambda: safe(get_candles,               ticker, "5"),
        "yf_info":  _yf_fast_info,
        "social":   lambda: safe(get_stocktwits_sentiment,  ticker),
        "reddit":   lambda: safe(get_reddit_mentions,       ticker),
        "breadth":  lambda: safe(get_market_breadth),
        "news":     lambda: safe(get_company_news,          ticker, default=[]),
        "pivots":   lambda: safe(get_pivot_points,          ticker),
        "ext_q":    lambda: safe(get_extended_quote,        ticker),
    }
    _results = {}
    with ThreadPoolExecutor(max_workers=8) as _ex:
        _fmap = {_ex.submit(fn): name for name, fn in _tasks.items()}
        for _fut in _asc(_fmap, timeout=20):
            _name = _fmap[_fut]
            try:
                _results[_name] = _fut.result()
            except Exception as _fe:
                print(f"[Scan] {ticker} parallel task '{_name}' failed: {_fe}")
                _results[_name] = {} if _name != "news" else []

    quote   = _results.get("quote",   {})
    profile = _results.get("profile", {})
    candles = _results.get("candles", {})
    yf_info = _results.get("yf_info", {})
    social  = _results.get("social",  {})
    reddit  = _results.get("reddit",  {})
    breadth = _results.get("breadth", {})
    news    = _results.get("news",    [])
    pivots  = _results.get("pivots",  {})
    ext_q   = _results.get("ext_q",   {})

    # ── Price data — ONLY from live sources, never from stale .info ──────────
    price = quote.get("c") or 0
    # If get_quote() returned 0, retry directly via fast_info one more time.
    # NOTE: We do NOT fall back to yf_info.currentPrice — that value is stale
    # disk-cached data that can be months out of date (e.g. $183 instead of $347).
    if not price:
        try:
            fi2   = yf.Ticker(ticker).fast_info
            price = float(getattr(fi2, 'last_price', 0) or 0)
            print(f"[Scan] {ticker} price retry via fast_info: ${price}")
        except Exception as fe:
            print(f"[Scan] {ticker} fast_info retry failed: {fe}")
    if not price:
        print(f"[Scan] {ticker} WARNING: all live price sources returned 0 — displaying 0")

    chg       = quote.get("d", 0) or 0
    chg_pct   = quote.get("dp", 0) or 0
    direction = "up" if chg_pct >= 0 else "down"

    # Format volume
    def fmt_vol(v):
        if not v: return "—"
        v = int(v)
        return f"{v/1_000_000:.1f}M" if v > 1_000_000 else f"{v:,}"

    def fmt_cap(v):
        if not v: return "—"
        if v > 1_000_000: return f"{v/1_000_000:.2f}T"
        if v > 1_000:     return f"{v/1_000:.1f}B"
        return f"{v:.0f}M"

    # Use fast_info volume (already in quote) — not stale yf_info.volume
    today_vol = quote.get("v") or yf_info.get("volume") or yf_info.get("regularMarketVolume") or 0
    avg_vol   = quote.get("avg_v") or yf_info.get("averageVolume") or yf_info.get("averageDailyVolume3Month") or 0

    # Market cap: Finnhub profile is usually accurate; fallback uses LIVE price * shares
    mkt_cap = profile.get("marketCapitalization") or 0
    if not mkt_cap:
        shares_out = yf_info.get("sharesOutstanding", 0) or 0
        mkt_cap = (price * shares_out / 1e6) if shares_out else 0
    float_sh = yf_info.get("floatShares", 0)

    # ── Intraday chart data + technical indicators ───────────────────────────
    chart_labels     = []
    chart_timestamps = []   # Unix seconds — required by TradingView Lightweight Charts
    chart_opens      = []
    chart_highs      = []
    chart_lows       = []
    chart_prices     = []   # closes
    chart_vols       = []
    technicals       = {}

    # Primary: yfinance 1-minute bars (O, H, L, C, V → proper candlesticks + VWAP)
    try:
        hist_1m = yf.Ticker(ticker).history(period="1d", interval="1m")
        if hist_1m is not None and not hist_1m.empty and len(hist_1m) > 5:
            for idx, row in hist_1m.iterrows():
                chart_labels.append(idx.strftime("%H:%M"))
                chart_timestamps.append(int(idx.timestamp()))
                chart_opens.append(round(float(row["Open"]),   2))
                chart_highs.append(round(float(row["High"]),   2))
                chart_lows.append(round(float(row["Low"]),    2))
                chart_prices.append(round(float(row["Close"]), 2))
                chart_vols.append(round(float(row["Volume"]) / 1_000_000, 4))
            print(f"[Scan] {ticker} chart: {len(chart_prices)} 1m bars from yfinance")
    except Exception as _e1m:
        print(f"[Scan] {ticker} 1m yfinance failed: {_e1m}")

    # Secondary: Finnhub 5-minute candles
    if not chart_prices:
        if candles.get("s") == "ok" and candles.get("c"):
            ts_list  = candles["t"]
            prices_c = candles["c"]
            volumes_c= candles.get("v", [0] * len(prices_c))
            opens_c  = candles.get("o", prices_c)
            highs_c  = candles.get("h", prices_c)
            lows_c   = candles.get("l", prices_c)
            for ts, o, p, v, h, l in zip(ts_list, opens_c, prices_c, volumes_c, highs_c, lows_c):
                dt = datetime.fromtimestamp(ts)
                chart_labels.append(dt.strftime("%H:%M"))
                chart_timestamps.append(int(ts))
                chart_opens.append(round(o, 2))
                chart_highs.append(round(h, 2))
                chart_lows.append(round(l, 2))
                chart_prices.append(round(p, 2))
                chart_vols.append(round(v / 1_000_000, 2))
            print(f"[Scan] {ticker} chart: {len(chart_prices)} 5m bars from Finnhub")

    # Tertiary: yfinance 5-minute fallback
    if not chart_prices:
        try:
            hist_5m = yf.Ticker(ticker).history(period="1d", interval="5m")
            if hist_5m is not None and not hist_5m.empty:
                for idx, row in hist_5m.iterrows():
                    chart_labels.append(idx.strftime("%H:%M"))
                    chart_timestamps.append(int(idx.timestamp()))
                    chart_opens.append(round(float(row["Open"]),   2))
                    chart_highs.append(round(float(row["High"]),   2))
                    chart_lows.append(round(float(row["Low"]),    2))
                    chart_prices.append(round(float(row["Close"]), 2))
                    chart_vols.append(round(float(row["Volume"]) / 1_000_000, 2))
        except Exception as _e5m:
            print(f"[Scan] {ticker} 5m fallback also failed: {_e5m}")

    # Quaternary: 60-day daily fallback (market closed / weekend) — enough bars for all indicators
    if not chart_prices:
        try:
            hist_1d = yf.Ticker(ticker).history(period="60d", interval="1d")
            if hist_1d is not None and not hist_1d.empty:
                for idx, row in hist_1d.iterrows():
                    chart_labels.append(idx.strftime("%Y-%m-%d"))
                    chart_timestamps.append(int(idx.timestamp()))
                    chart_opens.append(round(float(row["Open"]),   2))
                    chart_highs.append(round(float(row["High"]),   2))
                    chart_lows.append(round(float(row["Low"]),    2))
                    chart_prices.append(round(float(row["Close"]), 2))
                    chart_vols.append(round(float(row["Volume"]) / 1_000_000, 2))
                print(f"[Scan] {ticker} chart: {len(chart_prices)} daily bars (60d fallback)")
        except Exception as _e1d:
            print(f"[Scan] {ticker} daily fallback also failed: {_e1d}")

    # ── Deduplicate & sort chart data by timestamp ───────────────────────────
    # TradingView Lightweight Charts requires strictly ascending, unique timestamps.
    # yfinance can return duplicate or out-of-order timestamps (e.g. pre-market
    # timezone edge cases on Windows).  Deduplicate here before computing
    # technicals so the indicator arrays stay aligned with the chart arrays.
    if chart_timestamps and len(chart_timestamps) > 1:
        seen_ts = {}
        for i, ts in enumerate(chart_timestamps):
            seen_ts[ts] = i          # last bar wins on duplicate timestamp
        keep_idx = sorted(seen_ts.values())
        if len(keep_idx) < len(chart_timestamps):
            n_dupes = len(chart_timestamps) - len(keep_idx)
            print(f"[Scan] {ticker} removed {n_dupes} duplicate timestamps from chart data")
            chart_labels     = [chart_labels[i]     for i in keep_idx]
            chart_timestamps = [chart_timestamps[i] for i in keep_idx]
            chart_opens      = [chart_opens[i]      for i in keep_idx]
            chart_highs      = [chart_highs[i]      for i in keep_idx]
            chart_lows       = [chart_lows[i]       for i in keep_idx]
            chart_prices     = [chart_prices[i]     for i in keep_idx]
            chart_vols       = [chart_vols[i]       for i in keep_idx]

    # Compute technical indicators if we have enough bars
    if chart_prices and len(chart_prices) >= 5:
        try:
            raw_vols = [v * 1_000_000 for v in chart_vols]
            technicals = compute_technicals(
                chart_prices,
                chart_highs if chart_highs else chart_prices,
                chart_lows  if chart_lows  else chart_prices,
                raw_vols
            )
            print(f"[Scan] {ticker} technicals computed ({len(chart_prices)} bars)")
        except Exception as _ete:
            print(f"[Scan] {ticker} technicals failed: {_ete}")

    # ── Social sentiment ─────────────────────────────────────────────────────
    wsb_count  = reddit.get("wsb", 0)
    st_count   = social.get("count", 0)
    tw_count   = 0  # Twitter requires paid API
    opt_count  = reddit.get("options", 0)

    max_count = max(wsb_count, st_count, tw_count, opt_count, 1)

    st_score = social.get("sentimentScore", 0.5)
    if st_score >= 0.7:
        sent_label = "🔥 Bullish"
        social_tag = "🔥 HOT"
        social_tag_class = "tag-hot"
    elif st_score >= 0.55:
        sent_label = "📈 Leaning Bullish"
        social_tag = "📈 Bullish"
        social_tag_class = "tag-bull"
    elif st_score <= 0.3:
        sent_label = "📉 Bearish"
        social_tag = "📉 Bearish"
        social_tag_class = "tag-bear"
    elif st_score <= 0.45:
        sent_label = "📉 Leaning Bearish"
        social_tag = "📉 Bearish"
        social_tag_class = "tag-bear"
    else:
        sent_label = "💬 Mixed"
        social_tag = "💬 Mixed"
        social_tag_class = "tag-neutral"

    # Build catalyst text
    catalyst_parts = []
    top_post = reddit.get("catalyst", "")
    if top_post:
        catalyst_parts.append(f'WSB top post: "{top_post}"')
    st_top = social.get("topMessage", "")
    if st_top:
        catalyst_parts.append(f'StockTwits: "{st_top}"')
    if news and len(news) > 0:
        headline = news[0].get("headline", "")[:150]
        source   = news[0].get("source", "")
        if headline:
            catalyst_parts.append(f'News ({source}): {headline}')
    catalyst_text = " | ".join(catalyst_parts) if catalyst_parts else f"No major catalyst identified for {ticker} in the last 24h."

    # ── Breadth ────────────────────────────────────────────────────────────
    sector_perf = breadth.get("sectorPerf", [])
    breadth_hist = breadth.get("breadthHistory", [40]*10)
    if len(breadth_hist) < 10:
        breadth_hist = [40]*10

    uptrend_ratio = breadth.get("uptrendRatio", "—")
    # Dynamic breadth tag based on uptrend ratio
    try:
        _utr_val = int(uptrend_ratio.replace("%","").strip())
        if _utr_val >= 70:
            _btag, _btag_cls = "📈 Expanding", "tag-bull"
        elif _utr_val >= 50:
            _btag, _btag_cls = "🔵 Neutral", "tag-blue"
        elif _utr_val >= 30:
            _btag, _btag_cls = "⚖ Mixed", "tag-neutral"
        else:
            _btag, _btag_cls = "📉 Contracting", "tag-bear"
    except Exception:
        _btag, _btag_cls = "Market Breadth", "tag-blue"

    # ── Risk metrics ───────────────────────────────────────────────────────
    short_pct = yf_info.get("shortPercentOfFloat", 0) or 0
    short_str = f"{short_pct*100:.1f}%"
    beta      = yf_info.get("beta", 1.0) or 1.0

    above_50ma = (price > yf_info.get("fiftyDayAverage", price))
    above_200ma = (price > yf_info.get("twoHundredDayAverage", price))

    if above_50ma and above_200ma:
        trend_desc = "Above 50-day & 200-day MA — bullish structure"
        trend_val  = "✅ Strong"
        trend_class = "up"
    elif above_50ma:
        trend_desc = "Above 50-day MA but below 200-day MA — mixed"
        trend_val  = "⚖ Mixed"
        trend_class = "neutral"
    else:
        trend_desc = "Below 50-day MA — bearish structure"
        trend_val  = "❌ Weak"
        trend_class = "down"

    if beta > 1.5:
        vol_desc  = f"Beta {beta:.1f} — high volatility stock, wider moves"
        vol_val   = "⚠ High"
        vol_class = "down"
    elif beta > 1.0:
        vol_desc  = f"Beta {beta:.1f} — moderate volatility, market-like swings"
        vol_val   = "⚖ Moderate"
        vol_class = "neutral"
    else:
        vol_desc  = f"Beta {beta:.1f} — low volatility, less reactive"
        vol_val   = "✅ Low"
        vol_class = "up"

    avg_v = yf_info.get("averageVolume", 0) or 1
    today_v = yf_info.get("volume", 0) or 0
    vol_ratio = today_v / avg_v if avg_v else 1
    if vol_ratio > 1.5:
        liq_desc  = f"Volume {vol_ratio:.1f}x above average — very liquid today"
        liq_val   = "✅ High"
        liq_class = "up"
    elif vol_ratio > 0.8:
        liq_desc  = "Volume near average — normal liquidity"
        liq_val   = "✅ Normal"
        liq_class = "up"
    else:
        liq_desc  = "Below-average volume — thinner market today"
        liq_val   = "⚠ Thin"
        liq_class = "neutral"

    if short_pct > 0.15:
        short_desc  = f"{short_pct*100:.1f}% float short — high squeeze potential"
        short_class = "up"
    elif short_pct > 0.05:
        short_desc  = f"{short_pct*100:.1f}% float short — moderate short interest"
        short_class = "neutral"
    else:
        short_desc  = f"{short_pct*100:.1f}% float short — low short interest"
        short_class = "neutral"

    # ── Volume tag ────────────────────────────────────────────────────────
    if vol_ratio > 2:
        volume_tag = "🔥 Massive Vol"
    elif vol_ratio > 1.3:
        volume_tag = "📈 Above Avg"
    elif vol_ratio < 0.5:
        volume_tag = "📉 Low Vol"
    else:
        volume_tag = "Normal Vol"

    # ── Gap detector ──────────────────────────────────────────────────────
    open_price = quote.get("o") or 0
    prev_close = quote.get("pc") or 0
    gap_pct    = 0.0
    gap_dir    = "none"
    gap_label  = ""
    gap_class  = ""
    if open_price and prev_close and prev_close > 0:
        gap_pct = round((open_price - prev_close) / prev_close * 100, 2)
        if gap_pct >= 1.0:
            gap_dir   = "up"
            gap_label = f"⬆ Gap Up {gap_pct:+.2f}%"
            gap_class = "tag-bull"
        elif gap_pct <= -1.0:
            gap_dir   = "down"
            gap_label = f"⬇ Gap Down {gap_pct:+.2f}%"
            gap_class = "tag-bear"
        elif abs(gap_pct) > 0.1:
            gap_dir   = "flat"
            gap_label = f"↔ Flat Open {gap_pct:+.2f}%"
            gap_class = "tag-neutral"

    # ── Pivot points ──────────────────────────────────────────────────────
    # Already computed via safe(get_pivot_points, ticker)

    # ── Extended hours (pre/post market) ──────────────────────────────────
    ext_price   = ext_q.get("ext_price")
    ext_chg_pct = ext_q.get("ext_chg_pct", 0.0)
    ext_type    = ext_q.get("ext_type", "regular")
    ext_label   = ""
    ext_class   = ""
    if ext_price and ext_type != "regular":
        sign = "+" if ext_chg_pct >= 0 else ""
        ext_label = f"{'Pre' if ext_type == 'pre' else 'Post'}-market: ${ext_price:.2f}  ({sign}{ext_chg_pct:.2f}%)"
        ext_class = "up" if ext_chg_pct >= 0 else "down"

    # ── Composite score ───────────────────────────────────────────────────
    composite = compute_composite(quote, options, social, reddit)

    # ── Real-time prediction ──────────────────────────────────────────────
    prediction = compute_prediction(price, chg_pct, quote, options, social, reddit, yf_info)

    # ── AI Setup Detector ─────────────────────────────────────────────────
    # Pass the last technicals values + social + options for a plain-English setup summary
    _tech_for_setup = {
        "rsi":       technicals.get("rsi",       []),
        "macd_hist": technicals.get("macd_hist",  []),
        "sqz_on":    technicals.get("sqz_on",     []),
        "sqz_color": technicals.get("sqz_color",  []),
        "vwap":      technicals.get("vwap",       []),
    }
    setup = _ai_setup_detector(price, chg_pct, _tech_for_setup, social, options, quote)
    if composite >= 65:
        composite_color = "#00ff88"
    elif composite >= 45:
        composite_color = "#ffaa00"
    else:
        composite_color = "#ff4455"

    # ── Assemble response ─────────────────────────────────────────────────
    result = {
        "ticker": ticker,
        "name": profile.get("name") or yf_info.get("longName") or ticker,

        # ── Company overview ──────────────────────────────────────────────
        "companyDesc":    (yf_info.get("longBusinessSummary") or "")[:500],  # cap at 500 chars
        "companySector":  yf_info.get("sector")   or "",
        "companyIndustry": yf_info.get("industry") or "",
        "companyCountry": yf_info.get("country")  or "",
        "companyEmployees": f"{yf_info.get('fullTimeEmployees', 0):,}" if yf_info.get("fullTimeEmployees") else "",
        "companyWebsite": yf_info.get("website")  or "",
        "companyExchange": yf_info.get("exchange") or "",

        "price": f"{price:.2f}",
        "chg":   f"{chg:+.2f}",
        "chgPct": f"{chg_pct:+.2f}%",
        "dir":   direction,
        "vol":    fmt_vol(today_vol),
        "avgVol": fmt_vol(avg_vol),
        "mktCap": fmt_cap(mkt_cap),
        "float":  fmt_vol(float_sh),
        "composite": composite,
        "compositeColor": composite_color,

        # Social
        "socialTag":      social_tag,
        "socialTagClass": social_tag_class,
        "sentiment":      round(st_score, 2),
        "sentimentLabel": sent_label,
        "wsb":     wsb_count,
        "st":      st_count,
        "tw":      tw_count,
        "opt":     opt_count,
        "catalyst": catalyst_text,

        # Breadth
        "uptrendRatio": breadth.get("uptrendRatio", "—"),
        "adLine":        breadth.get("adLine",       "—"),
        "hiLoRatio":     breadth.get("hiLoRatio",    "—"),
        "breadthTag":      _btag,
        "breadthTagClass": _btag_cls,
        "breadthData": breadth_hist[-10:],
        "sectorPerf": sector_perf,

        # Risk
        "trendDesc":  trend_desc,
        "trendVal":   trend_val,
        "trendValClass": trend_class,
        "volDesc":    vol_desc,
        "volVal":     vol_val,
        "volValClass": vol_class,
        "liqDesc":    liq_desc,
        "liqVal":     liq_val,
        "liqValClass": liq_class,
        "shortDesc":  short_desc,
        "shortVal":   short_str,
        "shortValClass": short_class,
        "volumeTag":  volume_tag,

        # Gap detector
        "gapPct":    gap_pct,
        "gapDir":    gap_dir,
        "gapLabel":  gap_label,
        "gapClass":  gap_class,

        # Pivot points (yesterday's OHLC-derived levels)
        "pivots": pivots,

        # Today's intraday high/low (for chart price lines)
        "dayHigh": round(quote.get("h", 0) or price, 2),
        "dayLow":  round(quote.get("l", 0) or price, 2),

        # Extended hours (pre/post market)
        "extPrice":   f"{ext_price:.2f}" if ext_price else "",
        "extChgPct":  ext_chg_pct,
        "extLabel":   ext_label,
        "extClass":   ext_class,

        # Chart + technical indicators (full OHLC for candlestick rendering)
        "priceData": {
            "labels":       chart_labels,
            "timestamps":   chart_timestamps,
            "opens":        chart_opens,
            "highs":        chart_highs,
            "lows":         chart_lows,
            "prices":       chart_prices,   # closes
            "volumes":      chart_vols,
            # Technical indicators (None where not yet calculable)
            "vwap":         technicals.get("vwap"),
            "bb_upper":     technicals.get("bb_upper"),
            "bb_middle":    technicals.get("bb_middle"),
            "bb_lower":     technicals.get("bb_lower"),
            "sma9":         technicals.get("sma9"),
            "sma20":        technicals.get("sma20"),
            "sma50":        technicals.get("sma50"),
            "mfi":          technicals.get("mfi"),
            "rsi":          technicals.get("rsi"),
            "macd_line":    technicals.get("macd_line"),
            "macd_signal":  technicals.get("macd_signal"),
            "macd_hist":    technicals.get("macd_hist"),
            # TTM Squeeze
            "sqz_on":       technicals.get("sqz_on"),
            "sqz_hist":     technicals.get("sqz_hist"),
            "sqz_color":    technicals.get("sqz_color"),
            # VWAP Bands
            "vwap_u1":      technicals.get("vwap_u1"),
            "vwap_l1":      technicals.get("vwap_l1"),
            "vwap_u2":      technicals.get("vwap_u2"),
            "vwap_l2":      technicals.get("vwap_l2"),
        },

        # Prediction signal card
        "prediction": prediction,

        # AI Setup Detector
        "setup": setup,
    }

    # Let any final assembly error bubble up to scan_ticker's top-level safety net
    return jsonify(set_cache(cache_key, result))


@app.route("/api/watchlist")
def watchlist():
    """Return live quotes for the ticker bar."""
    cache_key = "watchlist"
    if (v := cached(cache_key, ttl=15)): return jsonify(v)

    tickers = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD", "MSFT", "META", "AMZN", "GME"]
    results = []
    for sym in tickers:
        q = get_quote(sym)
        if q:
            results.append({
                "sym":   sym,
                "price": f"{q.get('c', 0):.2f}",
                "chg":   f"{q.get('dp', 0):+.2f}%",
                "dir":   "up" if q.get("dp", 0) >= 0 else "down"
            })
    return jsonify(set_cache(cache_key, results))


@app.route("/api/watchlist/scanner")
def watchlist_scanner():
    """
    Lightweight watchlist portfolio scanner.
    Returns price, change%, volume ratio, RSI(14), squeeze state, and VWAP position
    for all watchlist tickers — designed to populate the Scanner card in the dashboard.
    Cache: 45s (fast refresh without hammering APIs).
    """
    cache_key = "watchlist_scanner"
    if (v := cached(cache_key, ttl=45)): return jsonify(v)

    SCAN_TICKERS = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD", "MSFT", "META", "AMZN", "GME"]
    results = []

    def _quick_scan(sym):
        try:
            q = get_quote(sym)
            if not q:
                return None

            price    = q.get("c", 0)
            chg_pct  = q.get("dp", 0)
            volume   = q.get("v", 0)
            avg_vol  = q.get("avg_v", 0)
            vol_ratio = min(round(volume / avg_vol, 2), 999.0) if avg_vol > 0 else 1.0

            # Get intraday bars for RSI, VWAP, Squeeze
            rsi_val    = None
            vwap_pos   = "—"
            sqz_state  = "off"
            try:
                tk   = yf.Ticker(sym)
                hist = tk.history(period="5d", interval="5m", auto_adjust=True)
                if hist is not None and len(hist) > 20:
                    closes  = hist["Close"].tolist()
                    highs   = hist["High"].tolist()
                    lows    = hist["Low"].tolist()
                    vols    = hist["Volume"].tolist()
                    n = len(closes)

                    # RSI(14)
                    if n > 15:
                        deltas = [closes[i] - closes[i-1] for i in range(1, n)]
                        gains  = [max(d, 0.0) for d in deltas]
                        losses = [max(-d, 0.0) for d in deltas]
                        ag = sum(gains[:14]) / 14
                        al = sum(losses[:14]) / 14
                        for i in range(14, n - 1):
                            ag = (ag * 13 + gains[i]) / 14
                            al = (al * 13 + losses[i]) / 14
                        rsi_val = round(100 - 100 / (1 + ag / al), 1) if al > 0 else 100.0

                    # VWAP
                    cum_tpv = cum_v = 0.0
                    vwap_vals = []
                    for i in range(n):
                        tp = (highs[i] + lows[i] + closes[i]) / 3.0
                        v  = max(vols[i], 0)
                        cum_tpv += tp * v
                        cum_v   += v
                        vwap_vals.append(cum_tpv / cum_v if cum_v > 0 else closes[i])
                    last_vwap = vwap_vals[-1]
                    vwap_pos = "above" if closes[-1] > last_vwap else "below"

                    # TTM Squeeze (20-period BB inside Keltner Channels)
                    if n >= 20:
                        sqz_period = 20
                        kc_mult    = 1.5
                        window_c   = closes[-sqz_period:]
                        window_h   = highs[-sqz_period:]
                        window_l   = lows[-sqz_period:]
                        sma20      = sum(window_c) / sqz_period
                        std20      = (sum((x - sma20)**2 for x in window_c) / sqz_period) ** 0.5
                        bb_upper   = sma20 + 2 * std20
                        bb_lower   = sma20 - 2 * std20
                        # ATR for Keltner
                        tr_vals = []
                        for i in range(max(0, n-sqz_period), n):
                            pc = closes[i-1] if i > 0 else closes[i]
                            tr_vals.append(max(highs[i]-lows[i], abs(highs[i]-pc), abs(lows[i]-pc)))
                        atr20 = sum(tr_vals) / len(tr_vals) if tr_vals else 0
                        ema20 = sum(window_c) / sqz_period  # simple approx
                        kc_upper = ema20 + kc_mult * atr20
                        kc_lower = ema20 - kc_mult * atr20
                        sqz_state = "on" if (bb_upper < kc_upper and bb_lower > kc_lower) else "off"

            except Exception as e:
                print(f"[Scanner] {sym} technicals error: {e}")

            return {
                "sym":         sym,
                "price":       f"{price:.2f}",
                "priceNum":    round(price, 2),
                "chgPct":      f"{chg_pct:+.2f}%",
                "chgVal":      chg_pct,
                "dir":         "up" if chg_pct >= 0 else "down",
                "volRatio":    f"{vol_ratio:.2f}x",
                "volRatioNum": round(vol_ratio, 2),
                "volHot":      vol_ratio >= 1.5,
                "rsi":         rsi_val,
                "rsiLabel": (
                    "OB" if rsi_val and rsi_val >= 70 else
                    "OS" if rsi_val and rsi_val <= 30 else ""
                ),
                "vwapPos":  vwap_pos,
                "sqzState": sqz_state,
            }
        except Exception as e:
            print(f"[Scanner] {sym} error: {e}")
            return None

    # Run all tickers in parallel threads for speed
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_quick_scan, sym): sym for sym in SCAN_TICKERS}
        ticker_map = {}
        for fut in as_completed(futures):
            sym = futures[fut]
            res = fut.result()
            if res:
                ticker_map[sym] = res
    # Preserve original order
    results = [ticker_map[sym] for sym in SCAN_TICKERS if sym in ticker_map]
    return jsonify(set_cache(cache_key, results))


@app.route("/api/options/<ticker>")
def api_options(ticker):
    """
    Options flow for a ticker — loaded ASYNC after the main scan so it never
    blocks quote/price delivery. Returns pcRatio, ivPct, flowRows, etc.
    Cache: 2 minutes (options chains don't change second-by-second).
    """
    ticker = ticker.upper().strip()
    key = f"options_route:{ticker}"
    if (v := cached(key, ttl=120)): return jsonify(v)
    try:
        data = get_options_data(ticker)
        return jsonify(set_cache(key, data))
    except Exception as e:
        print(f"[Options] {ticker} route error: {e}")
        return jsonify({"pcRatio": "—", "ivPct": "—", "totalCallVol": 0,
                        "totalPutVol": 0, "optVol": "—", "flowRows": [],
                        "error": str(e)})


# ── Catalyst / News endpoints ─────────────────────────────────────────────────

@app.route("/api/news/<ticker>")
def ticker_news(ticker):
    """Return last 10 news articles for a specific ticker — Catalyst panel."""
    key = f"news_panel:{ticker}"
    if (v := cached(key, ttl=180)): return jsonify(v)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    raw = fh_get("/company-news", {"symbol": ticker, "from": week_ago, "to": today}) or []
    articles = []
    seen = set()
    for a in raw:
        h = a.get("headline", "").strip()
        if not h or h in seen:
            continue
        seen.add(h)
        ts = a.get("datetime", 0)
        try:
            dt_str = datetime.utcfromtimestamp(int(ts)).strftime("%b %d %H:%M")
        except Exception:
            dt_str = "—"
        h_low = h.lower()
        if any(w in h_low for w in ["beat","surge","rally","jump","soar","record","bull","upgrade","buy"]):
            tag = "bullish"
        elif any(w in h_low for w in ["miss","drop","fall","crash","bear","downgrade","sell","cut","slump"]):
            tag = "bearish"
        else:
            tag = "neutral"
        articles.append({
            "headline": h,
            "source": a.get("source", ""),
            "url": a.get("url", "#"),
            "time": dt_str,
            "tag": tag,
            "summary": (a.get("summary","")[:160] if a.get("summary") else "")
        })
        if len(articles) >= 10:
            break
    return jsonify(set_cache(key, articles))


@app.route("/api/news/market")
def market_news():
    """Return general market news — Catalyst panel background feed."""
    key = "news_market"
    if (v := cached(key, ttl=300)): return jsonify(v)
    raw = fh_get("/news", {"category": "general"}) or []
    articles = []
    seen = set()
    for a in raw:
        h = a.get("headline", "").strip()
        if not h or h in seen:
            continue
        seen.add(h)
        ts = a.get("datetime", 0)
        try:
            dt_str = datetime.utcfromtimestamp(int(ts)).strftime("%b %d %H:%M")
        except Exception:
            dt_str = "—"
        h_low = h.lower()
        if any(w in h_low for w in ["beat","surge","rally","jump","soar","record","bull","upgrade","buy"]):
            tag = "bullish"
        elif any(w in h_low for w in ["miss","drop","fall","crash","bear","downgrade","sell","cut","slump"]):
            tag = "bearish"
        else:
            tag = "neutral"
        articles.append({"headline": h, "source": a.get("source",""), "url": a.get("url","#"), "time": dt_str, "tag": tag})
        if len(articles) >= 15:
            break
    return jsonify(set_cache(key, articles))


@app.route("/api/earnings/upcoming")
def upcoming_earnings():
    """Return earnings calendar for the next 7 days."""
    key = "earnings_upcoming"
    if (v := cached(key, ttl=900)): return jsonify(v)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    week_out = (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d")
    raw = fh_get("/calendar/earnings", {"from": today, "to": week_out}) or {}
    items = raw.get("earningsCalendar", []) if isinstance(raw, dict) else []
    results = []
    for e in items[:40]:
        sym = e.get("symbol", "")
        if not sym or len(sym) > 5:
            continue
        eps_est = e.get("epsEstimate")
        results.append({
            "symbol": sym,
            "date": e.get("date", ""),
            "hour": e.get("hour", ""),
            "epsEst": f"{eps_est:.2f}" if eps_est is not None else "—",
        })
    results.sort(key=lambda x: x["date"])
    return jsonify(set_cache(key, results[:20]))


@app.route("/api/earnings/check/<ticker>")
def check_earnings(ticker):
    """Check if a specific ticker has earnings in the next 7 days.
    Returns {hasEarnings, date, daysOut, hour, epsEst} or {hasEarnings: false}.
    Tries Finnhub calendar first, falls back to yfinance .calendar.
    """
    ticker = ticker.upper()
    key = f"earnings_check:{ticker}"
    if (v := cached(key, ttl=3600)): return jsonify(v)

    today_dt = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    # ── Strategy 1: Finnhub earnings calendar ─────────────────────────────
    try:
        today_str = today_dt.strftime("%Y-%m-%d")
        week_out  = (today_dt + timedelta(days=7)).strftime("%Y-%m-%d")
        raw = fh_get("/calendar/earnings", {"from": today_str, "to": week_out, "symbol": ticker}) or {}
        items = raw.get("earningsCalendar", []) if isinstance(raw, dict) else []
        for e in items:
            if e.get("symbol", "").upper() == ticker:
                edate = e.get("date", "")
                try:
                    days_out = (datetime.strptime(edate, "%Y-%m-%d") - today_dt).days
                except Exception:
                    days_out = 99
                eps_est = e.get("epsEstimate")
                result = {
                    "hasEarnings": True,
                    "date": edate,
                    "daysOut": days_out,
                    "hour": e.get("hour", ""),
                    "epsEst": f"{eps_est:.2f}" if eps_est is not None else "—",
                    "source": "finnhub",
                }
                return jsonify(set_cache(key, result))
    except Exception as e:
        print(f"[earnings/check] Finnhub error for {ticker}: {e}")

    # ── Strategy 2: yfinance .calendar ────────────────────────────────────
    try:
        t = yf.Ticker(ticker)
        cal = t.calendar  # dict with 'Earnings Date' key (list of Timestamps)
        if cal and "Earnings Date" in cal:
            dates = cal["Earnings Date"]
            if not isinstance(dates, list):
                dates = [dates]
            for ed in sorted(dates):
                if hasattr(ed, "to_pydatetime"):
                    ed = ed.to_pydatetime()
                elif isinstance(ed, str):
                    ed = datetime.strptime(ed[:10], "%Y-%m-%d")
                ed_norm = ed.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
                days_out = (ed_norm - today_dt).days
                if 0 <= days_out <= 7:
                    result = {
                        "hasEarnings": True,
                        "date": ed_norm.strftime("%Y-%m-%d"),
                        "daysOut": days_out,
                        "hour": "",
                        "epsEst": "—",
                        "source": "yfinance",
                    }
                    return jsonify(set_cache(key, result))
    except Exception as e:
        print(f"[earnings/check] yfinance error for {ticker}: {e}")

    result = {"hasEarnings": False}
    return jsonify(set_cache(key, result))


# ── Massive endpoints ──────────────────────────────────────────────────────────

@app.route("/api/massive/gainers-losers")
def api_gainers_losers():
    """Top market gainers and losers.
    Tries Massive v2 first (requires plan upgrade).
    Falls back to yfinance screening of active tickers.
    """
    key = "api_gl"
    if (v := cached(key, ttl=60)): return jsonify(v)

    def parse_massive(data):
        items = []
        tickers = (data.get("tickers") or data.get("results") or data.get("data") or [])
        for t in tickers[:10]:
            snap = t.get("day") or t.get("snapshot") or t
            sym  = t.get("ticker") or t.get("symbol") or t.get("sym", "")
            price = snap.get("c") or snap.get("close") or 0
            chg   = t.get("todaysChangePerc") or snap.get("todaysChangePerc") or 0
            vol   = snap.get("v") or snap.get("volume") or 0
            if not sym: continue
            items.append({
                "sym": sym,
                "price": f"{float(price):.2f}" if price else "—",
                "chgPct": f"{float(chg):+.2f}%" if chg else "—",
                "dir": "up" if float(chg or 0) >= 0 else "down",
                "vol": f"{int(vol):,}" if vol else "—"
            })
        return items

    # ── Try Massive first ──────────────────────────────────────────────────────
    raw = massive_gainers_losers()
    gainers_raw = raw.get("gainers", {}) if raw else {}
    losers_raw  = raw.get("losers",  {}) if raw else {}
    gainers = parse_massive(gainers_raw)
    losers  = parse_massive(losers_raw)

    # ── Fallback: yfinance history-based screener (works on closed-market days) ─
    if not gainers and not losers:
        print("[Movers] Massive not available — using yfinance history fallback")
        try:
            watchlist = ["NVDA","AAPL","TSLA","MSFT","AMZN","META","GOOGL",
                         "AMD","SPY","QQQ","PLTR","MSTR","COIN","SOFI","RIVN",
                         "NFLX","BABA","UBER","ARM","SMCI"]
            rows = []
            for sym in watchlist:
                try:
                    # Use history(period="5d") — works on holidays/weekends
                    hist = yf.Ticker(sym).history(period="5d", interval="1d")
                    hist = hist[hist["Volume"] > 0]   # skip zero-volume rows
                    if len(hist) < 2:
                        continue
                    price  = float(hist["Close"].iloc[-1])
                    prev   = float(hist["Close"].iloc[-2])
                    vol    = float(hist["Volume"].iloc[-1])
                    if price and prev:
                        chg_pct = ((price - prev) / prev) * 100
                        rows.append({"sym": sym, "price": price, "chg_pct": chg_pct, "vol": vol})
                except Exception:
                    pass
            rows.sort(key=lambda x: x["chg_pct"], reverse=True)
            def fmt(r):
                return {
                    "sym":    r["sym"],
                    "price":  f"{r['price']:.2f}",
                    "chgPct": f"{r['chg_pct']:+.2f}%",
                    "dir":    "up" if r["chg_pct"] >= 0 else "down",
                    "vol":    f"{int(r['vol']):,}" if r["vol"] else "—"
                }
            gainers = [fmt(r) for r in rows if r["chg_pct"] > 0][:8]
            losers  = [fmt(r) for r in reversed(rows) if r["chg_pct"] < 0][:8]
            source  = "yfinance"
        except Exception as e:
            print(f"[Movers] yfinance fallback error: {e}")
            source = "error"
    else:
        source = "massive"

    result = {"gainers": gainers, "losers": losers, "source": source}
    return jsonify(set_cache(key, result))


# ── Pre-Market / After-Hours Movers ───────────────────────────────────────────
PREMARKET_WATCHLIST = [
    # Mega-caps + high-beta
    "NVDA","AAPL","TSLA","MSFT","AMZN","META","GOOGL","GOOG",
    "AMD","NFLX","BABA","UBER","ARM","SMCI",
    # Popular day-trade / retail names
    "PLTR","MSTR","COIN","SOFI","RIVN","GME","AMC","HOOD",
    "RKLB","IONQ","MARA","RIOT","CLSK",
    # Index ETFs (give a market pulse)
    "SPY","QQQ","IWM","DIA",
    # Sector leaders
    "GLD","USO","TLT","XLF","XLE","XBI",
]

@app.route("/api/premarket")
def api_premarket():
    """
    Scan PREMARKET_WATCHLIST for extended-hours price action.
    Returns movers sorted by absolute % change — biggest first.
    Works for both pre-market AND after-hours depending on time of day.
    Cache: 60 s (short — prices move fast in extended hours).
    """
    key = "premarket_movers"
    if (v := cached(key, ttl=60)): return jsonify(v)

    results = []
    session_type = "regular"   # will be overwritten once we see ext data

    for sym in PREMARKET_WATCHLIST:
        try:
            eq = get_extended_quote(sym)
            ext_price   = eq.get("ext_price")
            ext_chg_pct = eq.get("ext_chg_pct", 0.0)
            ext_type    = eq.get("ext_type", "regular")

            if ext_price is None or ext_type == "regular":
                continue   # skip tickers with no extended data right now

            session_type = ext_type   # remember for label

            # Use reg_price already fetched in get_extended_quote — no extra yfinance call needed
            reg_close = eq.get("reg_price")

            sign = "+" if ext_chg_pct >= 0 else ""
            results.append({
                "sym":       sym,
                "ext_price": ext_price,
                "ext_chg":   f"{sign}{ext_chg_pct:.2f}%",
                "ext_chg_f": ext_chg_pct,   # raw float for sorting
                "reg_close": f"${reg_close:.2f}" if reg_close else "—",
                "dir":       "up" if ext_chg_pct >= 0 else "down",
            })
        except Exception as e:
            print(f"[PreMarket] {sym} error: {e}")

    # Sort by biggest absolute move first
    results.sort(key=lambda x: abs(x["ext_chg_f"]), reverse=True)
    # Strip raw float (not needed in UI)
    for r in results:
        r.pop("ext_chg_f", None)

    label = "Pre-Market" if session_type == "pre" else "After-Hours" if session_type == "post" else "Regular Hours"
    payload = {
        "movers":       results[:20],
        "session_type": session_type,
        "label":        label,
        "ts":           datetime.utcnow().strftime("%H:%M UTC"),
        "count":        len(results),
    }
    print(f"[PreMarket] {len(results)} movers — {label}")
    return jsonify(set_cache(key, payload))


# ── Standalone breadth endpoint ───────────────────────────────────────────────
@app.route("/api/breadth")
def api_breadth():
    """Returns market breadth + VIX + index ETF performance. No ticker needed."""
    b = get_market_breadth()
    return jsonify({
        "vix":       b.get("vix", "—"),
        "vixChg":    b.get("vixChg", 0),
        "vixLabel":  b.get("vixLabel", "—"),
        "spyPct":    b.get("spyPct", 0),
        "qqqPct":    b.get("qqqPct", 0),
        "iwmPct":    b.get("iwmPct", 0),
        "diaPct":    b.get("diaPct", 0),
        "regime":    b.get("regime", "—"),
        "regimeClass": b.get("regimeClass", "neutral"),
        "sectorPerf":  b.get("sectorPerf", []),
        "uptrendRatio": b.get("uptrendRatio", "—"),
        "breadthHistory": b.get("breadthHistory", []),
    })


# ── Index ETF bar endpoint (lightweight, 15s cache) ────────────────────────────
@app.route("/api/indices")
def api_indices():
    """Returns SPY/QQQ/IWM/DIA/VIX for the market overview bar. Fast, 15s cache."""
    key = "indices_bar"
    if (v := cached(key, ttl=15)): return jsonify(v)

    symbols = ["SPY", "QQQ", "IWM", "DIA", "^VIX"]
    out = []
    for sym in symbols:
        try:
            fi = yf.Ticker(sym).fast_info
            price = fi.last_price or 0
            prev  = fi.previous_close or price
            chg_pct = round(((price - prev) / prev) * 100, 2) if prev else 0
            out.append({
                "sym":    sym.replace("^", ""),
                "price":  round(price, 2),
                "chgPct": chg_pct
            })
        except Exception as e:
            print(f"[Indices] {sym} error: {e}")
            out.append({"sym": sym.replace("^", ""), "price": 0, "chgPct": 0})

    result = {"indices": out, "ts": int(time.time())}
    set_cache(key, result)
    return jsonify(result)


# ── Serve the HTML dashboard ───────────────────────────────────────────────────
# ── Hot Stocks endpoint ────────────────────────────────────────────────────────
@app.route("/api/hot-stocks")
def api_hot_stocks():
    """
    Top 10 Hottest Stocks — ranked by a composite Heat Score.

    Heat Score (0-100):
      • Social  (0-40 pts): Reddit/ApeWisdom mention rank + velocity
      • Momentum(0-30 pts): absolute % price change today
      • Volume  (0-30 pts): today's volume vs 3-month average volume

    Data sources: ApeWisdom (mentions), StockTwits (watchlist), yfinance (price/vol)
    Cache: 60 s
    """
    key = "hot_stocks"
    if (v := cached(key, ttl=60)): return jsonify(v)

    print("[HotStocks] Building heat rankings…")

    # ── 1. Social signals ─────────────────────────────────────────────────────
    ape      = get_apewisdom_trending()          # list of dicts: ticker, mentions, velocity, rank
    st_trend = get_stocktwits_trending()         # list of dicts: ticker, watchlist_count

    st_map = {s["ticker"]: s.get("watchlist_count", 0) for s in st_trend}

    # Build candidate pool: ApeWisdom top 20 + any StockTwits tickers not already in pool
    seen = set()
    candidates = []
    for t in ape[:20]:
        sym = t["ticker"]
        if sym not in seen:
            seen.add(sym)
            candidates.append({"sym": sym, "mentions": t.get("mentions", 0),
                                "velocity": t.get("velocity", 0),
                                "ape_rank": t.get("rank", 99),
                                "st_watch": st_map.get(sym, 0)})

    for s in st_trend[:15]:
        sym = s["ticker"]
        if sym not in seen:
            seen.add(sym)
            candidates.append({"sym": sym, "mentions": 0,
                                "velocity": 0, "ape_rank": 99,
                                "st_watch": s.get("watchlist_count", 0)})

    # ── 2. Price + volume via yfinance ────────────────────────────────────────
    # Use history(period="5d") — reliable even on holidays/weekends/pre-market
    results = []
    for c in candidates:
        sym = c["sym"]
        try:
            t    = yf.Ticker(sym)
            hist = t.history(period="5d", interval="1d")
            hist = hist[hist["Volume"] > 0]   # drop zero-volume rows (holiday rows)
            if len(hist) < 2:
                continue
            price     = float(hist["Close"].iloc[-1])
            prev      = float(hist["Close"].iloc[-2])
            today_vol = float(hist["Volume"].iloc[-1])
            avg_vol   = float(hist["Volume"].mean()) or 1

            if not price or not prev:
                continue

            chg_pct   = ((price - prev) / prev) * 100
            vol_ratio = round(today_vol / avg_vol, 2) if avg_vol else 0

            # ── Score computation ──────────────────────────────────────────────
            # Social: rank-based (rank 1→40 pts, rank 20→2 pts) + velocity bonus
            ape_rank = c["ape_rank"]
            social_base = max(0, 40 - ((ape_rank - 1) * 2))          # 40 → 2 pts
            vel = c["velocity"]
            vel_bonus = min(10, max(0, vel / 10)) if vel > 0 else 0   # up to +10
            social_score = min(40, social_base + vel_bonus)

            # Momentum: 0-30 pts — 1% move = 10 pts, capped at 3%
            momentum_score = min(30, abs(chg_pct) * 10)

            # Volume: 0-30 pts — 2× avg = 20 pts, 3× avg = 30 pts
            vol_score = min(30, vol_ratio * 10) if vol_ratio else 0

            heat = round(social_score + momentum_score + vol_score, 1)

            # Signal label
            if heat >= 70:    signal = "🔥 ON FIRE"
            elif heat >= 50:  signal = "⚡ HOT"
            elif heat >= 35:  signal = "📈 Trending"
            elif heat >= 20:  signal = "👀 Watch"
            else:             signal = "💬 Active"

            results.append({
                "sym":       sym,
                "price":     f"{price:.2f}",
                "chgPct":    f"{chg_pct:+.2f}",
                "dir":       "up" if chg_pct >= 0 else "down",
                "volRatio":  f"{vol_ratio:.1f}",
                "mentions":  c["mentions"],
                "velocity":  c["velocity"],
                "heatScore": heat,
                "signal":    signal,
                "socialScore":   round(social_score, 1),
                "momentumScore": round(momentum_score, 1),
                "volScore":      round(vol_score, 1),
            })
        except Exception as e:
            print(f"[HotStocks] {sym} error: {e}")
            continue

    results.sort(key=lambda x: x["heatScore"], reverse=True)
    top10 = results[:10]
    print(f"[HotStocks] Done — top ticker: {top10[0]['sym'] if top10 else '—'} "
          f"heat={top10[0]['heatScore'] if top10 else 0}")

    payload = {
        "stocks":    top10,
        "timestamp": datetime.utcnow().strftime("%H:%M UTC"),
        "count":     len(top10),
    }
    return jsonify(set_cache(key, payload))


# ── Intelligence & Volume Spike constants ─────────────────────────────────────
# Broader ticker list — fast_info is lightweight enough to handle 40+ per scan.
# Covers large-caps, high-beta names, meme stocks, sector ETFs for breadth signal.
VOLUME_SCAN_TICKERS = [
    # Mega-caps / indices
    'SPY','QQQ','IWM','DIA',
    # Mag 7
    'AAPL','MSFT','NVDA','TSLA','META','AMZN','GOOGL',
    # High-beta tech
    'AMD','PLTR','ARM','SMCI','MSTR','COIN','HOOD',
    # Meme / retail favorites
    'GME','AMC','SOFI','RIVN','LCID','WULF',
    # Biotech & energy
    'MRNA','BNTX','ENPH','FSLR',
    # Financials & macro
    'GS','JPM','BAC','XLF',
    # Semiconductors
    'MU','INTC','TSM','QCOM',
    # Speculative / trending
    'IONQ','RGTI','QUBT','BBAI','ACHR','JOBY',
]


# ── RSS parser helper ──────────────────────────────────────────────────────────
def parse_rss(url, timeout=6):
    """Fetch and parse an RSS/Atom feed. Returns list of {title, link, time, ts_epoch}."""
    import time as _time
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code != 200:
            return []
        # Strip XML declaration issues and parse
        content = r.content
        root = ET.fromstring(content)
        channel = root.find('channel') or root
        items_found = channel.findall('item') or root.findall('.//item')
        result = []
        now_epoch = _time.time()
        for item in items_found[:20]:
            title = (item.findtext('title') or '').strip()
            # RSS link can be element text or attribute
            link_el = item.find('link')
            link = ''
            if link_el is not None:
                link = (link_el.text or '').strip() or (link_el.get('href') or '')
            if not link:
                link = (item.findtext('guid') or '').strip()
            pub = (item.findtext('pubDate') or item.findtext('published') or '').strip()
            ts_epoch = now_epoch  # default: treat as now if unparseable
            try:
                dt = parsedate_to_datetime(pub)
                ts_epoch = dt.timestamp()
                # Human-readable: "2h ago", "Apr 10 22:15", etc.
                age_s = now_epoch - ts_epoch
                if age_s < 3600:
                    pub_str = f"{int(age_s // 60)}m ago"
                elif age_s < 86400:
                    pub_str = f"{int(age_s // 3600)}h ago"
                else:
                    pub_str = dt.strftime('%b %d %H:%M')
            except Exception:
                pub_str = pub[:16] if pub else ''
            if title:
                result.append({'title': title[:160], 'link': link, 'time': pub_str, 'ts_epoch': ts_epoch})
        # Sort newest first
        result.sort(key=lambda x: x['ts_epoch'], reverse=True)
        return result
    except ET.ParseError as e:
        print(f'[RSS] Parse error {url}: {e}')
        return []
    except Exception as e:
        print(f'[RSS] Error {url}: {e}')
        return []


# ── Fear & Greed ───────────────────────────────────────────────────────────────
def get_fear_greed():
    """CNN Fear & Greed index — public unofficial JSON endpoint."""
    key = 'fear_greed'
    if (v := cached(key, ttl=300)): return v
    try:
        url = 'https://production.dataviz.cnn.io/index/fearandgreed/graphdata'
        r = requests.get(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Referer': 'https://www.cnn.com/markets/fear-and-greed',
            'Origin': 'https://www.cnn.com',
        }, timeout=8)
        if r.status_code != 200:
            return set_cache(key, None)
        data = r.json()
        fg = data.get('fear_and_greed', {})
        score       = round(float(fg.get('score', 50)))
        rating      = fg.get('rating', 'neutral').replace('_', ' ').title()
        prev_close  = round(float(fg.get('previous_close',  score)))
        prev_week   = round(float(fg.get('previous_1_week', score)))
        prev_month  = round(float(fg.get('previous_1_month',score)))
        # Color zone
        if score <= 25:   zone, color = 'Extreme Fear', '#ff3d57'
        elif score <= 45: zone, color = 'Fear',         '#ffb300'
        elif score <= 55: zone, color = 'Neutral',      '#718096'
        elif score <= 75: zone, color = 'Greed',        '#00e676'
        else:             zone, color = 'Extreme Greed','#00ff88'
        result = {'score': score, 'rating': rating, 'zone': zone, 'color': color,
                  'prevClose': prev_close, 'prevWeek': prev_week, 'prevMonth': prev_month}
        print(f'[F&G] score={score} — {zone}')
        return set_cache(key, result)
    except Exception as e:
        print(f'[F&G] Error: {e}')
        return set_cache(key, None)


# ── Intelligence news aggregator ───────────────────────────────────────────────
def get_intelligence_news():
    """
    Aggregate breaking news, political/macro, and financial podcast RSS into
    a single intelligence payload — sorted newest-first, max 48h old.
    Sources: Google News RSS (free, no key), financial podcast RSS (public).
    """
    import time as _time
    key = 'intelligence'
    if (v := cached(key, ttl=120)): return v

    now_epoch = _time.time()
    MAX_AGE_BREAKING = 48 * 3600   # drop articles older than 48 h
    MAX_AGE_POLITICAL = 72 * 3600  # political/macro can be 3 days
    MAX_AGE_PODCAST   = 14 * 86400 # podcasts up to 2 weeks

    # ── Breaking market news — past 24 h filter via tbs=qdr:d ────────────────
    rss_market   = 'https://news.google.com/rss/search?q=stock+market+today&hl=en-US&gl=US&ceid=US:en&tbs=qdr:d'
    rss_earnings = 'https://news.google.com/rss/search?q=earnings+stocks&hl=en-US&gl=US&ceid=US:en&tbs=qdr:d'
    rss_breaking = 'https://news.google.com/rss/search?q=stock+market+breaking+news&hl=en-US&gl=US&ceid=US:en&tbs=qdr:d'
    seen = set()
    breaking = []
    for item in parse_rss(rss_market) + parse_rss(rss_earnings) + parse_rss(rss_breaking):
        age = now_epoch - item.get('ts_epoch', now_epoch)
        if age > MAX_AGE_BREAKING:
            continue
        k = item['title'].lower()
        if k in seen: continue
        seen.add(k)
        tl = k
        if any(w in tl for w in ['beat','surge','rally','jump','soar','record','upgrade','buy','gain','rise']):
            item['tag'] = 'bullish'
        elif any(w in tl for w in ['miss','drop','fall','crash','downgrade','sell','cut','slump','loss','decline']):
            item['tag'] = 'bearish'
        else:
            item['tag'] = 'neutral'
        breaking.append(item)
    # Sort newest first, cap at 12
    breaking.sort(key=lambda x: x.get('ts_epoch', 0), reverse=True)
    breaking = breaking[:12]

    # ── Political / Macro (Trump + Fed) — past 3 days ────────────────────────
    rss_trump = 'https://news.google.com/rss/search?q=Trump+tariffs+economy+trade&hl=en-US&gl=US&ceid=US:en&tbs=qdr:w'
    rss_fed   = 'https://news.google.com/rss/search?q=Federal+Reserve+inflation+rates&hl=en-US&gl=US&ceid=US:en&tbs=qdr:w'
    rss_macro = 'https://news.google.com/rss/search?q=macro+economy+GDP+CPI+jobs&hl=en-US&gl=US&ceid=US:en&tbs=qdr:w'
    seen_pol = set()
    political = []
    for item in parse_rss(rss_trump):
        age = now_epoch - item.get('ts_epoch', now_epoch)
        if age > MAX_AGE_POLITICAL: continue
        k = item['title'].lower()
        if k not in seen_pol:
            seen_pol.add(k); item['category'] = '🏛 Trump/Political'; political.append(item)
    for item in parse_rss(rss_fed):
        age = now_epoch - item.get('ts_epoch', now_epoch)
        if age > MAX_AGE_POLITICAL: continue
        k = item['title'].lower()
        if k not in seen_pol:
            seen_pol.add(k); item['category'] = '🏦 Fed/Macro'; political.append(item)
    for item in parse_rss(rss_macro):
        age = now_epoch - item.get('ts_epoch', now_epoch)
        if age > MAX_AGE_POLITICAL: continue
        k = item['title'].lower()
        if k not in seen_pol:
            seen_pol.add(k); item['category'] = '📊 Macro/Economy'; political.append(item)
    political.sort(key=lambda x: x.get('ts_epoch', 0), reverse=True)
    political = political[:12]

    # ── Podcasts / Financial Media ────────────────────────────────────────────
    podcast_feeds = [
        ('Planet Money',          'https://feeds.npr.org/510289/podcast.xml'),
        ('Bloomberg Odd Lots',    'https://feeds.megaphone.fm/LKN4671286868'),
        ('Motley Fool Money',     'https://feeds.megaphone.fm/foolmoneypodcast'),
        ('We Study Billionaires', 'https://feeds.megaphone.fm/WT9356831744'),
    ]
    seen_pod = set()
    podcasts = []
    for show_name, url in podcast_feeds:
        for item in parse_rss(url, timeout=5)[:3]:
            age = now_epoch - item.get('ts_epoch', now_epoch)
            if age > MAX_AGE_PODCAST: continue
            k = item['title'].lower()
            if k not in seen_pod:
                seen_pod.add(k); item['show'] = show_name; podcasts.append(item)
    podcasts.sort(key=lambda x: x.get('ts_epoch', 0), reverse=True)
    podcasts = podcasts[:8]

    result = {
        'breaking':  breaking,
        'political': political,
        'podcasts':  podcasts,
        'ts': datetime.utcnow().strftime('%H:%M UTC'),
    }
    print(f'[Intelligence] breaking={len(breaking)}, political={len(political)}, podcasts={len(podcasts)}')
    return set_cache(key, result)


# ── Volume Spike Scanner ───────────────────────────────────────────────────────
def get_volume_spikes():
    """
    Scan VOLUME_SCAN_TICKERS for unusual volume vs 3-month average.
    Returns list sorted by ratio descending (highest spike first).
    """
    key = 'volume_spikes'
    if (v := cached(key, ttl=120)): return v

    def fmt_v(v):
        if v >= 1_000_000: return f'{v/1_000_000:.1f}M'
        if v >= 1_000:     return f'{v/1_000:.0f}K'
        return str(int(v))

    spikes = []
    for ticker in VOLUME_SCAN_TICKERS:
        try:
            time.sleep(0.15)   # small pause — avoids rate-limit bursts on yfinance
            fi = yf.Ticker(ticker).fast_info
            # yfinance: three_month_average_volume is the avg baseline; last_volume is today
            avg_vol  = getattr(fi, 'three_month_average_volume', None)
            today_vol = getattr(fi, 'last_volume', None) or getattr(fi, 'regular_market_volume', None)
            price    = getattr(fi, 'last_price', 0) or 0
            chg_pct  = 0
            try:
                prev = getattr(fi, 'previous_close', None) or getattr(fi, 'regular_market_previous_close', None)
                if price and prev and prev > 0:
                    chg_pct = round((price - prev) / prev * 100, 2)
            except Exception:
                pass
            if not today_vol or not avg_vol or avg_vol == 0:
                continue
            ratio = round(today_vol / avg_vol, 2)
            if ratio < 1.5:
                continue  # not noteworthy
            if ratio >= 5.0:   level, color = 'EXTREME', '#ff3d57'
            elif ratio >= 3.0: level, color = 'HIGH',    '#ffb300'
            else:              level, color = 'ELEVATED','#40a9ff'
            spikes.append({
                'ticker':   ticker,
                'price':    f'${price:.2f}' if price else '—',
                'chgPct':   f'{chg_pct:+.1f}%',
                'dir':      'up' if chg_pct >= 0 else 'down',
                'vol':      fmt_v(today_vol),
                'avgVol':   fmt_v(avg_vol),
                'ratio':    ratio,
                'ratioStr': f'{ratio:.1f}x',
                'level':    level,
                'color':    color,
            })
        except Exception as e:
            print(f'[VolSpike] {ticker}: {e}')
    spikes.sort(key=lambda x: x['ratio'], reverse=True)
    result = spikes[:12]
    print(f'[VolSpike] {len(result)} spikes found')
    return set_cache(key, result)


# ── Intelligence routes ────────────────────────────────────────────────────────
@app.route('/api/intelligence')
def api_intelligence():
    """Breaking news + Trump/macro + financial podcasts — all from free RSS."""
    return jsonify(get_intelligence_news())


@app.route('/api/fear-greed')
def api_fear_greed():
    """CNN Fear & Greed index (unofficial public endpoint)."""
    data = get_fear_greed()
    if data is None:
        return jsonify({'error': 'Could not fetch data'})
    return jsonify(data)


@app.route('/api/volume-spikes')
def api_volume_spikes():
    """Volume spike scanner — popular tickers vs 3-month average."""
    return jsonify(get_volume_spikes())


@app.route("/api/search")
def api_search():
    """
    Ticker symbol search — resolves company names to tickers.
    Calls Yahoo Finance's public search endpoint.
    Returns up to 8 matches: { results: [{symbol, name, exchange, type}] }
    """
    q = request.args.get("q", "").strip()
    if not q or len(q) < 1:
        return jsonify({"results": []})

    key = f"search:{q.lower()}"
    if (v := cached(key, ttl=300)): return jsonify(v)

    try:
        url = "https://query1.finance.yahoo.com/v1/finance/search"
        params = {
            "q":             q,
            "quotesCount":   8,
            "newsCount":     0,
            "enableFuzzyQuery": True,
            "quotesQueryId": "tss_match_phrase_query",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }
        r = requests.get(url, params=params, headers=headers, timeout=6)
        if r.status_code != 200:
            return jsonify({"results": []})

        data = r.json()
        quotes = data.get("quotes", [])

        # Filter to equity-type results only (EQUITY, ETF) and clean up
        results = []
        for q_item in quotes:
            q_type = q_item.get("quoteType", "")
            if q_type not in ("EQUITY", "ETF", "MUTUALFUND"):
                continue
            symbol   = q_item.get("symbol", "")
            name     = q_item.get("longname") or q_item.get("shortname") or symbol
            exchange = q_item.get("exchange", "")
            # Skip symbols with dots or dashes (foreign listings like BRK.B is ok, but skip ^VIX etc.)
            if symbol.startswith("^") or not symbol:
                continue
            results.append({
                "symbol":   symbol,
                "name":     name[:50],       # cap name length
                "exchange": exchange,
                "type":     q_type,
            })
            if len(results) >= 8:
                break

        result = {"results": results}
        return jsonify(set_cache(key, result))

    except Exception as e:
        print(f"[Search] error for '{q}': {e}")
        return jsonify({"results": []})


@app.route("/api/ping")
def api_ping():
    """Ultra-fast health check — used by the dashboard connection indicator."""
    return jsonify({"ok": True, "ts": datetime.utcnow().strftime("%H:%M:%S UTC"),
                    "yfinance": True, "finnhub": bool(FINNHUB_KEY)})


# ── Presidential Watch ────────────────────────────────────────────────────────
# Keywords that typically move markets when mentioned by the president
_MARKET_MOVING_KEYWORDS = {
    # Bearish signals
    "tariff":        ("bearish", "🔴"),
    "tariffs":       ("bearish", "🔴"),
    "sanction":      ("bearish", "🔴"),
    "sanctions":     ("bearish", "🔴"),
    "ban":           ("bearish", "🔴"),
    "tax":           ("bearish", "🔴"),
    "investigate":   ("bearish", "🔴"),
    "investigation": ("bearish", "🔴"),
    "fine":          ("bearish", "🔴"),
    "penalty":       ("bearish", "🔴"),
    "trade war":     ("bearish", "🔴"),
    "restrict":      ("bearish", "🔴"),
    "inflation":     ("bearish", "🔴"),
    "deficit":       ("bearish", "🔴"),
    "shutdown":      ("bearish", "🔴"),
    "emergency":     ("bearish", "🔴"),
    "executive order": ("bearish", "🔴"),
    # Bullish signals
    "deal":          ("bullish", "🟢"),
    "agreement":     ("bullish", "🟢"),
    "trade deal":    ("bullish", "🟢"),
    "cut":           ("bullish", "🟢"),
    "deregulat":     ("bullish", "🟢"),
    "approved":      ("bullish", "🟢"),
    "approve":       ("bullish", "🟢"),
    "reduce":        ("bullish", "🟢"),
    "lower":         ("bullish", "🟢"),
    "stimulus":      ("bullish", "🟢"),
    "investment":    ("bullish", "🟢"),
    "jobs":          ("bullish", "🟢"),
    "growth":        ("bullish", "🟢"),
    # Neutral/watch
    "announce":      ("watch", "🟡"),
    "meeting":       ("watch", "🟡"),
    "press conference": ("watch", "🟡"),
    "statement":     ("watch", "🟡"),
    "market":        ("watch", "🟡"),
    "economy":       ("watch", "🟡"),
    "federal reserve": ("watch", "🟡"),
    "interest rate": ("watch", "🟡"),
}

def _score_text(text):
    """Return (sentiment, emoji, matched_keywords) for a piece of text."""
    text_lower = text.lower()
    bearish_hits, bullish_hits, watch_hits = [], [], []
    for kw, (sent, emoji) in _MARKET_MOVING_KEYWORDS.items():
        if kw in text_lower:
            if sent == "bearish": bearish_hits.append(kw)
            elif sent == "bullish": bullish_hits.append(kw)
            else: watch_hits.append(kw)
    if bearish_hits:   return "bearish", "🔴", bearish_hits
    if bullish_hits:   return "bullish", "🟢", bullish_hits
    if watch_hits:     return "watch",   "🟡", watch_hits
    return None, None, []

def _fetch_google_news_rss(query, max_items=8, max_age_hours=48):
    """Pull Google News RSS for a search query. Returns list of {title, link, ts, ts_epoch, source}.
    Uses &tbs=qdr:w (past week) to get recent results, then filters to max_age_hours."""
    import time as _time
    try:
        url = (f"https://news.google.com/rss/search"
               f"?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en&tbs=qdr:d")
        r = requests.get(url, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        now   = _time.time()
        cutoff = now - (max_age_hours * 3600)
        items = []
        for item in root.findall(".//item"):
            if len(items) >= max_items:
                break
            title   = (item.findtext("title")   or "").strip()
            link    = (item.findtext("link")    or "").strip()
            pub     = (item.findtext("pubDate") or "").strip()
            source  = (item.findtext("source")  or "Google News").strip()
            # Parse pubDate to epoch for age filtering
            ts_epoch = 0
            ts = pub[:16] if pub else "—"
            try:
                dt       = parsedate_to_datetime(pub)
                ts_epoch = dt.timestamp()
                # Relative time label
                age_h = (now - ts_epoch) / 3600
                if age_h < 1:
                    ts = f"{int(age_h * 60)}m ago"
                elif age_h < 24:
                    ts = f"{int(age_h)}h ago"
                else:
                    ts = dt.strftime("%b %d %H:%M")
            except Exception:
                pass
            # Skip articles older than cutoff
            if ts_epoch and ts_epoch < cutoff:
                continue
            items.append({"title": title, "link": link, "ts": ts,
                          "ts_epoch": ts_epoch, "source": source})
        return items
    except Exception as e:
        print(f"[PresWatch] Google RSS error: {e}")
        return []

def _fetch_truth_social_rss(max_items=5):
    """Try to pull Trump's Truth Social posts via RSS."""
    urls = [
        "https://truthsocial.com/@realDonaldTrump.rss",
        "https://rss.truthsocial.com/@realDonaldTrump",
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=6,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            items = []
            for item in root.findall(".//item")[:max_items]:
                title  = (item.findtext("title")   or "").strip()
                link   = (item.findtext("link")    or "").strip()
                pub    = (item.findtext("pubDate") or "").strip()
                desc   = (item.findtext("description") or "").strip()
                # Strip HTML tags from description
                import re as _re
                desc = _re.sub(r"<[^>]+>", "", desc)[:280]
                import time as _time
                ts_epoch = 0
                ts = pub[:16] if pub else "—"
                try:
                    dt = parsedate_to_datetime(pub)
                    ts_epoch = dt.timestamp()
                    age_h = (_time.time() - ts_epoch) / 3600
                    if age_h < 1:    ts = f"{int(age_h*60)}m ago"
                    elif age_h < 24: ts = f"{int(age_h)}h ago"
                    else:            ts = dt.strftime("%b %d %H:%M")
                except Exception:
                    pass
                items.append({
                    "title": desc or title,
                    "link": link, "ts": ts, "ts_epoch": ts_epoch, "source": "Truth Social"
                })
            if items:
                print(f"[PresWatch] Truth Social RSS OK — {len(items)} posts")
                return items
        except Exception as e:
            print(f"[PresWatch] Truth Social RSS {url} error: {e}")
    return []

@app.route("/api/presidential_watch")
def presidential_watch():
    """Fetch and score Trump/presidential posts that could move markets."""
    key = "presidential_watch"
    if (cached_val := cached(key, ttl=180)):
        return jsonify(cached_val)

    alerts = []

    # 1. Truth Social posts (direct from source)
    ts_posts = _fetch_truth_social_rss()
    for post in ts_posts:
        text = post["title"]
        sentiment, emoji, keywords = _score_text(text)
        alerts.append({
            "text":      text[:200],
            "source":    "🇺🇸 Truth Social",
            "ts":        post["ts"],
            "ts_epoch":  post.get("ts_epoch", 0),
            "link":      post["link"],
            "sentiment": sentiment or "neutral",
            "emoji":     emoji or "⚪",
            "keywords":  keywords[:4],
            "impact":    "HIGH" if sentiment in ("bearish","bullish") else "WATCH",
        })

    # 2. Google News — Trump + market/economy/tariff
    for query in ["Trump tariff economy", "Trump executive order market", "Trump trade"]:
        for item in _fetch_google_news_rss(query, max_items=5, max_age_hours=24):
            text = item["title"]
            sentiment, emoji, keywords = _score_text(text)
            if not keywords:
                continue   # skip irrelevant news
            # Deduplicate by title similarity
            existing_titles = [a["text"][:60].lower() for a in alerts]
            if text[:60].lower() in existing_titles:
                continue
            alerts.append({
                "text":      text[:200],
                "source":    f"📰 {item['source']}",
                "ts":        item["ts"],
                "ts_epoch":  item.get("ts_epoch", 0),
                "link":      item["link"],
                "sentiment": sentiment or "neutral",
                "emoji":     emoji or "⚪",
                "keywords":  keywords[:4],
                "impact":    "HIGH" if sentiment in ("bearish","bullish") else "WATCH",
            })

    # Sort: HIGH impact first, then by recency within each group
    alerts.sort(key=lambda x: (0 if x["impact"] == "HIGH" else 1,
                                -x.get("ts_epoch", 0)))
    alerts = alerts[:10]   # cap at 10

    # Overall signal: most severe sentiment wins
    sentiments = [a["sentiment"] for a in alerts]
    if "bearish" in sentiments:   overall = ("bearish", "🔴", "MARKET RISK — Bearish Signal")
    elif "bullish" in sentiments: overall = ("bullish", "🟢", "POSITIVE — Bullish Signal")
    elif alerts:                  overall = ("watch",   "🟡", "MONITOR — Posts Detected")
    else:                         overall = ("neutral", "⚪", "No alerts found")

    result = {
        "overall_sentiment": overall[0],
        "overall_emoji":     overall[1],
        "overall_label":     overall[2],
        "alert_count":       len(alerts),
        "alerts":            alerts,
        "as_of":             datetime.now().strftime("%H:%M:%S"),
    }
    set_cache(key, result)
    return jsonify(result)


# ── Macro Pulse ───────────────────────────────────────────────────────────────
_MACRO_SYMBOLS = {
    "SPY":    {"label": "S&P 500",   "icon": "📈"},
    "QQQ":    {"label": "NASDAQ",    "icon": "💻"},
    "^VIX":   {"label": "VIX",       "icon": "🌡️"},
    "^TNX":   {"label": "10Y Yield", "icon": "📊"},
    "DX-Y.NYB": {"label": "DXY",     "icon": "💵"},
    "GC=F":   {"label": "Gold",      "icon": "🥇"},
    "CL=F":   {"label": "Oil",       "icon": "🛢️"},
    "BTC-USD": {"label": "BTC",      "icon": "₿"},
}

@app.route("/api/macro_pulse")
def macro_pulse():
    key = "macro_pulse"
    if (v := cached(key, ttl=30)): return jsonify(v)
    result = []
    for sym, meta in _MACRO_SYMBOLS.items():
        try:
            fi    = yf.Ticker(sym).fast_info
            price = round(float(getattr(fi, "last_price", 0) or 0), 4)
            prev  = round(float(getattr(fi, "previous_close", 0) or 0), 4)
            if not price or not prev:
                continue
            chg     = round(price - prev, 4)
            chg_pct = round((chg / prev) * 100, 2)
            # Format price nicely
            if price > 1000:  fmt = f"{price:,.0f}"
            elif price > 10:  fmt = f"{price:,.2f}"
            else:             fmt = f"{price:.4f}"
            result.append({
                "sym":     sym,
                "label":   meta["label"],
                "icon":    meta["icon"],
                "price":   fmt,
                "chg_pct": chg_pct,
                "dir":     "up" if chg_pct >= 0 else "down",
            })
        except Exception as e:
            print(f"[Macro] {sym} error: {e}")
    set_cache(key, result)
    return jsonify(result)


# ── Real Kronos AI Model — lazy singleton loader ───────────────────────────────
import threading as _threading
import sys as _sys
import os as _os

_kronos_predictor      = None
_kronos_predictor_lock = _threading.Lock()
_kronos_loading        = False   # guard against concurrent load attempts

def _load_kronos_predictor():
    """Load KronosPredictor once and cache it globally. Thread-safe."""
    global _kronos_predictor, _kronos_loading
    if _kronos_predictor is not None:
        return _kronos_predictor
    with _kronos_predictor_lock:
        if _kronos_predictor is not None:
            return _kronos_predictor
        if _kronos_loading:
            return None   # already being loaded by another thread
        _kronos_loading = True

    try:
        print("[Kronos] Loading model from HuggingFace (first run — ~1 min)…")
        # Ensure the model package is importable from this file's directory
        _app_dir = _os.path.dirname(_os.path.abspath(__file__))
        if _app_dir not in _sys.path:
            _sys.path.insert(0, _app_dir)

        from model import KronosTokenizer, Kronos, KronosPredictor

        tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
        model_k   = Kronos.from_pretrained("NeoQuasar/Kronos-mini")
        predictor = KronosPredictor(model_k, tokenizer, device="cpu", max_context=2048)

        with _kronos_predictor_lock:
            _kronos_predictor = predictor
        print("[Kronos] Model ready ✓")
        return predictor

    except Exception as e:
        print(f"[Kronos] Failed to load model: {e}")
        _kronos_loading = False   # allow retry
        return None


def _get_kronos_ohlcv(ticker, lookback=200):
    """Fetch 5-min OHLCV bars for a ticker; return (df, timestamps) or (None, None)."""
    import pandas as pd
    try:
        t    = yf.Ticker(ticker)
        hist = t.history(period="30d", interval="5m")
        hist = hist[hist["Volume"] >= 0].dropna(subset=["Close"])
        if len(hist) < 50:
            return None, None

        hist = hist.tail(lookback)
        hist.index = hist.index.tz_localize(None) if hist.index.tzinfo else hist.index

        df = pd.DataFrame({
            "open":   hist["Open"].values,
            "high":   hist["High"].values,
            "low":    hist["Low"].values,
            "close":  hist["Close"].values,
            "volume": hist["Volume"].values,
        })
        timestamps = pd.Series(pd.to_datetime(hist.index))
        return df, timestamps
    except Exception:
        return None, None


# Kick off model pre-load in background the moment the module is imported
# (works for both Gunicorn and local __main__ runs)
_threading.Thread(target=_load_kronos_predictor, daemon=True).start()


def _make_future_timestamps(last_ts, n_bars=20, interval_min=5):
    """Generate n_bars future 5-min timestamps, skipping weekends."""
    import pandas as pd
    from datetime import timedelta
    ts = pd.Timestamp(last_ts)
    future = []
    delta = timedelta(minutes=interval_min)
    cur = ts
    for _ in range(n_bars):
        cur += delta
        while cur.weekday() >= 5:   # skip Saturday / Sunday
            cur += timedelta(days=1)
        future.append(cur)
    return pd.Series(future)


# ── Kronos Real-AI Price Forecast ─────────────────────────────────────────────
@app.route("/api/forecast/<ticker>")
def api_forecast(ticker):
    """
    Real Kronos foundation-model price forecast (transformer trained on 45+ exchanges).
    Uses KronosPredictor (NeoQuasar/Kronos-mini, 4.1M params) for 20-bar ahead prediction.
    Falls back to GBM+Momentum if model is still loading.
    Cache: 90s
    """
    import numpy as np
    from datetime import timedelta

    ticker = ticker.upper()
    key = f"forecast:{ticker}"
    if (v := cached(key, ttl=90)): return jsonify(v)

    try:
        pred_len = 20
        predictor = _load_kronos_predictor()

        # ── Fetch OHLCV data ──────────────────────────────────────────────────
        df, x_ts = _get_kronos_ohlcv(ticker, lookback=300)
        if df is None or len(df) < 50:
            return jsonify({"error": "Insufficient data for forecast"}), 400

        last_price = float(df["close"].iloc[-1])
        last_ts    = x_ts.iloc[-1]

        if predictor is not None:
            # ── Real Kronos inference ─────────────────────────────────────────
            y_ts = _make_future_timestamps(last_ts, n_bars=pred_len, interval_min=5)

            pred_df = predictor.predict(
                df=df,
                x_timestamp=x_ts,
                y_timestamp=y_ts,
                pred_len=pred_len,
                T=1.0, top_k=0, top_p=0.9, sample_count=3,
                verbose=False
            )

            mean_path = pred_df["close"].values
            # Approx bands from high/low predictions
            upper_68  = pred_df["high"].values
            lower_68  = pred_df["low"].values
            upper_95  = pred_df["high"].values * 1.005
            lower_95  = pred_df["low"].values * 0.995

            prob_up     = float(np.mean(mean_path > last_price) * 100)
            if prob_up == 0 or prob_up == 100:   # heuristic when all same direction
                prob_up = 85.0 if mean_path[-1] > last_price else 15.0
            expected_chg = round((float(mean_path[-1]) - last_price) / last_price * 100, 2)
            future_ts    = [int(ts.timestamp()) for ts in y_ts]
            model_label  = "Kronos-mini (Transformer · 4.1M params)"

        else:
            # ── GBM fallback while model is loading ──────────────────────────
            closes  = df["close"].values.astype(float)
            log_ret = np.log(closes[1:] / closes[:-1])
            mu      = float(np.mean(log_ret))
            sigma   = float(np.std(log_ret))
            adj_mu  = mu * 0.7 + float(np.mean(log_ret[-10:])) * 0.3
            n_sims  = 300
            np.random.seed(int(last_price * 1000) % (2**31))
            shocks  = np.random.standard_normal((n_sims, pred_len))
            paths   = np.zeros((n_sims, pred_len + 1))
            paths[:, 0] = last_price
            for i in range(pred_len):
                paths[:, i+1] = paths[:, i] * np.exp((adj_mu - 0.5*sigma**2) + sigma*shocks[:, i])
            forecast  = paths[:, 1:]
            mean_path = np.mean(forecast, axis=0)
            upper_68  = np.percentile(forecast, 84,   axis=0)
            lower_68  = np.percentile(forecast, 16,   axis=0)
            upper_95  = np.percentile(forecast, 97.5, axis=0)
            lower_95  = np.percentile(forecast, 2.5,  axis=0)
            prob_up   = float(np.mean(forecast[:, -1] > last_price) * 100)
            expected_chg = round((float(mean_path[-1]) - last_price) / last_price * 100, 2)
            ts_naive  = last_ts.to_pydatetime() if hasattr(last_ts, 'to_pydatetime') else last_ts
            ts_naive  = ts_naive.replace(tzinfo=None)
            future_ts = []
            for _ in range(pred_len):
                ts_naive += timedelta(seconds=300)
                while ts_naive.weekday() >= 5:
                    ts_naive += timedelta(days=1)
                future_ts.append(int(ts_naive.timestamp()))
            model_label = "GBM+Momentum (Kronos loading…)"

        result = {
            "ticker":       ticker,
            "anchor_price": round(last_price, 4),
            "anchor_ts":    int(last_ts.timestamp()) if hasattr(last_ts, 'timestamp') else int(last_ts.value // 1e9),
            "timestamps":   future_ts,
            "mean":         [round(float(v), 4) for v in mean_path],
            "upper_68":     [round(float(v), 4) for v in upper_68],
            "lower_68":     [round(float(v), 4) for v in lower_68],
            "upper_95":     [round(float(v), 4) for v in upper_95],
            "lower_95":     [round(float(v), 4) for v in lower_95],
            "prob_up":      round(prob_up, 1),
            "expected_chg": expected_chg,
            "pred_len":     pred_len,
            "context_bars": len(df),
            "model":        model_label,
        }
        return jsonify(set_cache(key, result))

    except Exception as e:
        print(f"[Forecast] {ticker} error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ── Kronos Top Signals Scanner — real AI on elite 40-stock universe ────────────
@app.route("/api/kronos/scanner")
def kronos_scanner():
    """
    Runs the real Kronos-mini transformer forecast on 40 high-volume US stocks.
    Uses predict_batch() for efficiency. Falls back to GBM if model not yet loaded.
    Cache: 10 minutes.
    """
    import numpy as np
    import pandas as pd
    from concurrent.futures import ThreadPoolExecutor, as_completed as _asc2

    # Elite universe — top 40 most liquid US stocks/ETFs for day trading
    ELITE_UNIVERSE = [
        "AAPL","MSFT","NVDA","TSLA","AMZN","META","GOOGL","AMD","SPY","QQQ",
        "SOFI","PLTR","RIVN","LCID","NIO","MARA","RIOT","COIN","HOOD","RBLX",
        "SNAP","UBER","LYFT","ABNB","SHOP","PYPL","ROKU","ZM","ARKK",
        "SQQQ","TQQQ","SPXL","UPRO","IWM","DIA","XLF","XLE","GLD","MSTR",
    ]

    key = "kronos:scanner"
    if (v := cached(key, ttl=600)): return jsonify(v)

    pred_len = 20
    LOOKBACK = 200   # must be identical for all tickers in batch

    # ── Step 1: Fetch OHLCV for all tickers in parallel ──────────────────────
    def _fetch(sym):
        df, ts = _get_kronos_ohlcv(sym, lookback=LOOKBACK)
        if df is None or len(df) < LOOKBACK:
            return sym, None, None
        return sym, df, ts

    ticker_data = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(_fetch, s): s for s in ELITE_UNIVERSE}
        for fut in _asc2(futs, timeout=60):
            try:
                sym, df, ts = fut.result()
                if df is not None:
                    ticker_data[sym] = (df, ts)
            except Exception:
                pass

    valid_syms = list(ticker_data.keys())
    if not valid_syms:
        return jsonify({"error": "No data available", "signals": [], "scanned": 0, "found": 0, "ts": int(time.time())}), 200

    results = []
    predictor = _load_kronos_predictor()

    if predictor is not None:
        # ── Real Kronos batch inference ───────────────────────────────────────
        try:
            # Trim all to shortest available length (batch requires identical seq_len)
            min_len = min(len(ticker_data[s][0]) for s in valid_syms)
            min_len = min(min_len, LOOKBACK)

            df_list, x_ts_list, y_ts_list = [], [], []
            for sym in valid_syms:
                df, ts = ticker_data[sym]
                df_trim = df.tail(min_len).reset_index(drop=True)
                ts_trim = ts.tail(min_len).reset_index(drop=True)
                last_ts = ts_trim.iloc[-1]
                y_ts = _make_future_timestamps(last_ts, n_bars=pred_len, interval_min=5)
                df_list.append(df_trim)
                x_ts_list.append(ts_trim)
                y_ts_list.append(y_ts)

            pred_dfs = predictor.predict_batch(
                df_list, x_ts_list, y_ts_list, pred_len,
                T=1.0, top_k=0, top_p=0.9, sample_count=1, verbose=False
            )

            for i, sym in enumerate(valid_syms):
                try:
                    pred_df  = pred_dfs[i]
                    last_p   = float(ticker_data[sym][0]["close"].iloc[-1])
                    mean_end = float(pred_df["close"].iloc[-1])
                    closes_pred = pred_df["close"].values
                    exp_chg_pct = (mean_end - last_p) / last_p * 100
                    # Sigmoid on expected % change with wide denominator (6.0)
                    # Gives realistic spread: ±2%→73%, ±5%→84%, ±8%→93% (capped 95%)
                    # Kronos predicts large moves on 5-min bars so wide scale avoids 100% clustering
                    prob_up = float(50.0 + 50.0 * math.tanh(exp_chg_pct / 6.0))
                    prob_up = max(5.0, min(95.0, round(prob_up, 1)))
                    exp_chg  = round(exp_chg_pct, 2)
                    results.append({
                        "sym":          sym,
                        "price":        round(last_p, 2),
                        "prob_up":      round(prob_up, 1),
                        "direction":    "up" if prob_up >= 50 else "down",
                        "prob_pct":     round(prob_up, 1) if prob_up >= 50 else round(100 - prob_up, 1),
                        "expected_chg": exp_chg,
                        "bars":         min_len,
                    })
                except Exception:
                    pass

        except Exception as e:
            print(f"[KronosScanner] Batch inference error: {e}")
            predictor = None   # fall through to GBM below

    if not results:
        # ── GBM fallback ─────────────────────────────────────────────────────
        for sym in valid_syms:
            try:
                df, _ = ticker_data[sym]
                closes  = df["close"].values.astype(float)
                last_p  = float(closes[-1])
                log_ret = np.log(closes[1:] / closes[:-1])
                mu      = float(np.mean(log_ret))
                sigma   = float(np.std(log_ret))
                if sigma < 1e-8 or last_p <= 0:
                    continue
                adj_mu  = mu * 0.7 + float(np.mean(log_ret[-10:])) * 0.3
                n_sims  = 300
                np.random.seed(int(last_p * 1000) % (2**31))
                shocks  = np.random.standard_normal((n_sims, pred_len))
                paths   = np.zeros((n_sims, pred_len + 1)); paths[:, 0] = last_p
                for i in range(pred_len):
                    paths[:, i+1] = paths[:, i] * np.exp((adj_mu - 0.5*sigma**2) + sigma*shocks[:, i])
                finals  = paths[:, 1:][:, -1]
                prob_up = float(np.mean(finals > last_p) * 100)
                exp_chg = round((float(np.mean(finals)) - last_p) / last_p * 100, 2)
                results.append({
                    "sym": sym, "price": round(last_p, 2),
                    "prob_up": round(prob_up, 1),
                    "direction": "up" if prob_up >= 50 else "down",
                    "prob_pct": round(prob_up, 1) if prob_up >= 50 else round(100 - prob_up, 1),
                    "expected_chg": exp_chg, "bars": len(df),
                })
            except Exception:
                pass

    # Filter to strong signals and rank
    strong = [r for r in results if r["prob_up"] >= 70 or r["prob_up"] <= 30]
    strong.sort(key=lambda x: abs(x["prob_up"] - 50), reverse=True)

    payload = {
        "signals":  strong[:15],
        "scanned":  len(valid_syms),
        "found":    len(strong),
        "ts":       int(time.time()),
        "model":    "Kronos-mini" if predictor is not None else "GBM+Momentum",
    }
    return jsonify(set_cache(key, payload))


# ── Scalp Alert Scanner — 5-10 min momentum/volume/VWAP signals ───────────────
_scalp_cache = {"ts": 0, "data": None}
_SCALP_TTL   = 30   # 30-second cache for fresher signals

_SCALP_UNIVERSE = [
    # ── Mega-cap tech ─────────────────────────────────────────────────────
    "AAPL","MSFT","NVDA","TSLA","AMZN","META","AMD","GOOGL","GOOG","AVGO",
    "ORCL","CRM","ADBE","INTC","QCOM","MU","ARM","AMAT","LRCX","KLAC",
    "MRVL","TXN","SMCI","DELL","HPQ",
    # ── High-beta / retail favorites ──────────────────────────────────────
    "PLTR","SOFI","COIN","HOOD","MARA","RIOT","MSTR","CLSK","HUT",
    "GME","AMC","SOUN","IONQ","QBTS","RGTI","BBAI",
    "RBLX","SNAP","PINS","RDDT","HIMS","OPEN","SPCE",
    # ── ETFs (leveraged + sector) ─────────────────────────────────────────
    "SPY","QQQ","IWM","TQQQ","SQQQ","SPXL","SPXS","LABU","LABD",
    "UVXY","VXX","ARKK","ARKG","SOXL","SOXS",
    "XLF","XLE","XLK","XBI","XLV","GDX","GDXJ",
    # ── Growth / SaaS ────────────────────────────────────────────────────
    "SHOP","SNOW","DDOG","NET","CRWD","OKTA","ZS","PANW","S",
    "BILL","HUBS","MDB","APP","TTD","ROKU","TWLO",
    # ── Financials ───────────────────────────────────────────────────────
    "JPM","BAC","GS","MS","C","WFC","BX","KKR","SCHW",
    "NU","AFRM","UPST","PYPL","V","MA",
    # ── EV / Autos ───────────────────────────────────────────────────────
    "RIVN","LCID","NIO","XPEV","LI","F","GM",
    # ── Energy ───────────────────────────────────────────────────────────
    "XOM","CVX","SLB","HAL","OXY","DVN","FANG","AR",
    # ── Healthcare / Biotech ─────────────────────────────────────────────
    "MRNA","NVAX","BNTX","PFE","ABBV","LLY","ISRG","DXCM",
    "CRSP","EDIT","SRPT","REGN","BIIB","VRTX",
    # ── Consumer / Media ─────────────────────────────────────────────────
    "NFLX","DIS","UBER","LYFT","ABNB","DASH","SPOT","CHWY","ETSY",
    # ── China ADRs ───────────────────────────────────────────────────────
    "BABA","JD","PDD","BIDU","BILI",
    # ── Commodities / Macro ──────────────────────────────────────────────
    "GLD","SLV","USO","GOLD","NEM",
]

@app.route("/api/scalp/scanner")
def scalp_scanner():
    try:
        return _scalp_scanner_inner()
    except Exception as e:
        print(f"[Scalp] Fatal: {e}")
        return jsonify({"alerts": [], "total": 0, "scanned": 0,
                        "ts": int(time.time()), "spy_ret1": 0.0,
                        "error": str(e)}), 200

def _scalp_scanner_inner():
    import numpy as np
    now = time.time()
    if _scalp_cache["data"] and now - _scalp_cache["ts"] < _SCALP_TTL:
        return jsonify(_scalp_cache["data"])

    # ── EMA helper ────────────────────────────────────────────────────────────
    def _ema(arr, period):
        k = 2.0 / (period + 1)
        e = float(arr[0])
        for v in arr[1:]:
            e = float(v) * k + e * (1 - k)
        return e

    # ── Fetch SPY 1-min for RS baseline + trend direction ─────────────────────
    spy_ret1     = 0.0
    spy_bull_trend = False   # SPY 9-EMA > 20-EMA
    spy_bear_trend = False
    try:
        spy_tk  = yf.Ticker("SPY")
        spy_raw = spy_tk.history(period="1d", interval="1m", prepost=True)
        if spy_raw is not None and len(spy_raw) >= 25:
            spy_raw.columns = [c.lower() for c in spy_raw.columns]
            sc = spy_raw["close"].values.astype(float)
            spy_ret1 = float((sc[-1] - sc[-2]) / sc[-2] * 100) if sc[-2] > 0 else 0.0
            spy_ema9  = _ema(sc, 9)
            spy_ema20 = _ema(sc, 20)
            spy_bull_trend = spy_ema9 > spy_ema20
            spy_bear_trend = spy_ema9 < spy_ema20
    except Exception:
        pass

    def _fetch_sym(sym):
        try:
            tk  = yf.Ticker(sym)
            df  = tk.history(period="1d", interval="1m", prepost=True)
            if df is None or len(df) < 25:
                return None
            df.columns = [c.lower() for c in df.columns]
            closes  = df["close"].values.astype(float)
            volumes = df["volume"].values.astype(float)
            highs   = df["high"].values.astype(float)
            lows    = df["low"].values.astype(float)
            last_p  = closes[-1]
            if last_p <= 0:
                return None

            # ── Hard filters (skip noisy/illiquid symbols) ─────────────────
            if last_p < 2.0:           # ignore sub-$2 stocks
                return None
            vol_avg20 = float(np.mean(volumes[-21:-1]))
            # Estimated daily dollar volume: avg 1-min vol × price × 390 bars/day
            est_daily_dv = vol_avg20 * last_p * 390
            if est_daily_dv < 1_000_000:   # require at least $1M/day liquidity
                return None

            # ── Signal 1: Volume Surge ─────────────────────────────────────
            vol_ratio  = float(volumes[-1] / vol_avg20) if vol_avg20 > 0 else 1.0
            vol_up_dir = closes[-1] > closes[-2]
            vol_surge  = vol_ratio >= 2.0

            # ── NEW — Relative Volume at Time-of-Day (RVOL) ────────────────
            # Compare current bar volume vs average of same 10 bars earlier
            # (approx same time-of-day context without needing historical days)
            rvol_ref  = float(np.mean(volumes[-31:-21])) if len(volumes) >= 31 else vol_avg20
            rvol      = float(volumes[-1] / rvol_ref) if rvol_ref > 0 else vol_ratio
            rvol_high = rvol >= 2.5   # stronger threshold vs same-time reference

            # ── Signal 2: VWAP Cross ───────────────────────────────────────
            typical   = (highs + lows + closes) / 3.0
            cum_tpv   = np.cumsum(typical * volumes)
            cum_vol   = np.cumsum(volumes)
            vwap_arr  = cum_tpv / np.where(cum_vol > 0, cum_vol, 1.0)
            vwap_now  = float(vwap_arr[-1])
            cross_up  = float(closes[-2]) < float(vwap_arr[-2]) and last_p >= vwap_now
            cross_dn  = float(closes[-2]) > float(vwap_arr[-2]) and last_p <= vwap_now
            vwap_cross = cross_up or cross_dn

            # ── Signal 3: Momentum Burst ───────────────────────────────────
            bar_chgs    = np.abs(np.diff(closes[-21:]))
            avg_bar_chg = float(np.mean(bar_chgs[:-1])) if len(bar_chgs) > 1 else 0.01
            last_chg    = abs(float(closes[-1] - closes[-2]))
            mom_burst   = last_chg >= 1.8 * avg_bar_chg and avg_bar_chg > 0
            mom_up      = closes[-1] > closes[-2]

            # ── Signal 4: 3-Bar Trend Lock ────────────────────────────────
            moves    = [closes[i] - closes[i-1] for i in range(-3, 0)]
            all_up   = all(m > 0 for m in moves)
            all_dn   = all(m < 0 for m in moves)
            trend_lock = all_up or all_dn
            trend_up   = all_up

            # ── Signal 5: Relative Strength vs SPY ────────────────────────
            sym_ret1  = float((closes[-1] - closes[-2]) / closes[-2] * 100) if closes[-2] > 0 else 0.0
            rs_strong = abs(sym_ret1) > abs(spy_ret1) * 1.5 and abs(sym_ret1) > 0.15
            rs_up     = sym_ret1 > 0

            # ── PRE-MOVE Signal 6: Bollinger Band Squeeze ─────────────────
            bb_squeeze = False
            bb_up      = last_p >= vwap_now
            if len(closes) >= 35:
                std_recent = float(np.std(closes[-10:]))
                std_prior  = float(np.std(closes[-30:-10]))
                bb_squeeze = std_recent < std_prior * 0.65 and std_prior > 0

            # ── PRE-MOVE Signal 7: NR7 ────────────────────────────────────
            nr7    = False
            nr7_up = last_p >= vwap_now
            if len(highs) >= 8:
                bar_ranges  = highs - lows
                current_rng = float(bar_ranges[-1])
                nr7 = current_rng < float(np.min(bar_ranges[-8:-1]))

            # ── PRE-MOVE Signal 8: Accelerating Volume ────────────────────
            vol_accel    = False
            vol_accel_up = closes[-1] > closes[-2]
            if len(volumes) >= 22:
                v1 = float(volumes[-3])
                v2 = float(volumes[-2])
                v3 = float(volumes[-1])
                vol_avg = float(np.mean(volumes[-21:-1]))
                vol_accel = (v1 < v2 < v3) and v3 > vol_avg * 1.2 and v2 > vol_avg * 0.8

            pre_move = bb_squeeze or nr7 or vol_accel

            # ── NEW Signal 9: EMA 9/20 Stack ──────────────────────────────
            # Bull structure: 9-EMA above 20-EMA; bear: 9-EMA below 20-EMA
            ema9_val       = _ema(closes, 9)
            ema20_val      = _ema(closes, 20)
            ema_stack_bull = ema9_val > ema20_val
            ema_stack_bear = ema9_val < ema20_val

            # ── NEW Signal 10: SPY Trend Alignment ────────────────────────
            # Stock going up AND SPY in bull EMA structure → confirmed momentum
            spy_align_bull = spy_bull_trend and sym_ret1 > 0
            spy_align_bear = spy_bear_trend and sym_ret1 < 0

            # ── NEW Signal 11: Multi-Timeframe (5-min constructed) ────────
            # Build synthetic 5-min bars from 1-min data; check VWAP position
            mtf_bull = False
            mtf_bear = False
            if len(closes) >= 15:
                c5 = [float(np.mean(closes[i:i+5])) for i in range(-15, 0, 5)]
                v5 = [float(np.sum(volumes[i:i+5]))  for i in range(-15, 0, 5)]
                h5 = [float(np.max(highs[i:i+5]))    for i in range(-15, 0, 5)]
                l5 = [float(np.min(lows[i:i+5]))     for i in range(-15, 0, 5)]
                if len(c5) >= 2:
                    tp5   = [(h5[i]+l5[i]+c5[i])/3 for i in range(len(c5))]
                    tot_v = max(sum(v5), 1e-9)
                    vwap5 = sum(tp5[i]*v5[i] for i in range(len(c5))) / tot_v
                    mtf_bull = c5[-1] > vwap5 and c5[-1] > c5[-2]
                    mtf_bear = c5[-1] < vwap5 and c5[-1] < c5[-2]

            # ── Directional scoring (11 signals) ──────────────────────────
            bull = sum([
                vol_surge       and vol_up_dir,     # 1
                cross_up,                           # 2
                mom_burst       and mom_up,         # 3
                trend_lock      and trend_up,       # 4
                rs_strong       and rs_up,          # 5
                bb_squeeze      and bb_up,          # 6
                nr7             and nr7_up,         # 7
                vol_accel       and vol_accel_up,   # 8
                ema_stack_bull,                     # 9 NEW
                spy_align_bull,                     # 10 NEW
                mtf_bull,                           # 11 NEW
            ])
            bear = sum([
                vol_surge       and not vol_up_dir, # 1
                cross_dn,                           # 2
                mom_burst       and not mom_up,     # 3
                trend_lock      and not trend_up,   # 4
                rs_strong       and not rs_up,      # 5
                bb_squeeze      and not bb_up,      # 6
                nr7             and not nr7_up,     # 7
                vol_accel       and not vol_accel_up, # 8
                ema_stack_bear,                     # 9 NEW
                spy_align_bear,                     # 10 NEW
                mtf_bear,                           # 11 NEW
            ])

            direction = "up" if bull >= bear else "down"

            # ── RSI exhaustion penalty ─────────────────────────────────────
            # Don't chase overbought bulls or oversold bears
            rsi_val = 50.0
            if len(closes) >= 16:
                deltas = np.diff(closes[-15:])
                gains  = float(np.mean(np.where(deltas > 0, deltas, 0.0)))
                losses = float(np.mean(np.where(deltas < 0, -deltas, 0.0)))
                if losses > 0:
                    rsi_val = 100.0 - 100.0 / (1.0 + gains / losses)
                elif gains > 0:
                    rsi_val = 100.0
            rsi_exhausted = (direction == "up" and rsi_val > 75) or \
                            (direction == "down" and rsi_val < 25)

            score = max(bull, bear)
            # RSI exhaustion shown as a UI warning colour but does NOT reduce score
            # (removing the penalty so borderline setups still fire)

            if score < 3:          # minimum threshold to appear
                return None

            # A = 5+/11, B = 3-4/11
            grade = "A" if score >= 5 else "B"

            return {
                "sym":        sym,
                "price":      round(float(last_p), 2),
                "direction":  direction,
                "grade":      grade,
                "score":      int(score),
                "chg_pct":    round(float(sym_ret1), 2),
                "vol_ratio":  round(float(vol_ratio), 1),
                "rvol":       round(float(rvol), 1),
                "vwap":       round(float(vwap_now), 2),
                "vs_vwap":    round(float((last_p - vwap_now) / vwap_now * 100), 2) if vwap_now else 0.0,
                "rsi":        round(float(rsi_val), 1),
                "ema_stack":  bool(ema_stack_bull if direction == "up" else ema_stack_bear),
                "spy_aligned": bool(spy_align_bull if direction == "up" else spy_align_bear),
                "mtf":        bool(mtf_bull if direction == "up" else mtf_bear),
                "pre_move":   bool(pre_move),
                "signals": {
                    "vol_surge":    bool(vol_surge),
                    "rvol_high":    bool(rvol_high),
                    "vwap_cross":   bool(vwap_cross),
                    "vwap_cross_up":bool(cross_up),
                    "mom_burst":    bool(mom_burst),
                    "trend_lock":   bool(trend_lock),
                    "trend_up":     bool(trend_up),
                    "rs_strong":    bool(rs_strong),
                    "bb_squeeze":   bool(bb_squeeze),
                    "nr7":          bool(nr7),
                    "vol_accel":    bool(vol_accel),
                    "ema_stack":    bool(ema_stack_bull if direction == "up" else ema_stack_bear),
                    "spy_aligned":  bool(spy_align_bull if direction == "up" else spy_align_bear),
                    "mtf":          bool(mtf_bull if direction == "up" else mtf_bear),
                },
            }
        except Exception as e:
            print(f"[Scalp] {sym}: {e}")
            return None

    # Parallel fetch — 20 threads for speed across large universe
    import concurrent.futures
    alerts = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
            futures = {ex.submit(_fetch_sym, sym): sym for sym in _SCALP_UNIVERSE}
            for fut in concurrent.futures.as_completed(futures, timeout=30):
                try:
                    res = fut.result()
                    if res:
                        alerts.append(res)
                except Exception:
                    pass
    except Exception as e:
        print(f"[Scalp] Batch error: {e}")

    # Sort: A grades first, then by score desc, then by vol_ratio desc
    grade_order = {"A": 0, "B": 1}
    alerts.sort(key=lambda x: (grade_order.get(x["grade"], 2), -x["score"], -x["vol_ratio"]))

    payload = {
        "alerts":   alerts[:25],   # show top 25 A/B signals
        "total":    len(alerts),
        "scanned":  len(_SCALP_UNIVERSE),
        "ts":       int(now),
        "spy_ret1": round(spy_ret1, 3),
    }
    _scalp_cache["ts"]   = now
    _scalp_cache["data"] = payload
    return jsonify(payload)


# ── Contract Award Scanner — USASpending.gov ─────────────────────────────────
_contract_cache = {"ts": 0, "data": None}
_CONTRACT_TTL   = 300   # 5-minute cache

# Publicly traded companies that commonly receive government contracts
# Key = uppercase fragment to search for in recipient name, Value = ticker
_CONTRACTOR_TICKERS = {
    # Defense primes
    "LOCKHEED MARTIN":     "LMT",
    "RAYTHEON":            "RTX",
    "NORTHROP GRUMMAN":    "NOC",
    "GENERAL DYNAMICS":    "GD",
    "L3HARRIS":            "LHX",
    "BOEING":              "BA",
    "TEXTRON":             "TXT",
    "TRANSDIGM":           "TDG",
    "HOWMET":              "HWM",
    "HEICO":               "HEI",
    "MOOG":                "MOG.A",
    "CURTISS-WRIGHT":      "CW",
    "TRIUMPH GROUP":       "TGI",
    "AEROJET":             "AJRD",
    # Mid/small defense & services
    "LEIDOS":              "LDOS",
    "SAIC":                "SAIC",
    "BOOZ ALLEN":          "BAH",
    "CACI":                "CACI",
    "MANTECH":             "MANT",
    "PARSONS":             "PSN",
    "KRATOS":              "KTOS",
    "MERCURY SYSTEMS":     "MRCY",
    "DXC TECHNOLOGY":      "DXC",
    "KBR INC":             "KBR",
    "MAXIMUS":             "MMS",
    "ICF INTERNATIONAL":   "ICFI",
    # AI / Tech contractors
    "PALANTIR":            "PLTR",
    "BIGBEAR":             "BBAI",
    "SOUNDHOUND":          "SOUN",
    "IONQ":                "IONQ",
    "IBM":                 "IBM",
    "ORACLE":              "ORCL",
    "MICROSOFT":           "MSFT",
    "AMAZON":              "AMZN",
    "GOOGLE":              "GOOGL",
    "DELL":                "DELL",
    "CISCO":               "CSCO",
    "ACCENTURE":           "ACN",
    "SCIENCE APPLICATIONS": "SAIC",
    # Space & emerging
    "ROCKET LAB":          "RKLB",
    "PLANET LABS":         "PL",
    "SPIRE GLOBAL":        "SPIR",
    "INTUITIVE MACHINES":  "LUNR",
    "REDWIRE":             "RDW",
    "JOBY":                "JOBY",
    "ARCHER AVIATION":     "ACHR",
    "KULR":                "KULR",
    # Healthcare & services
    "HUMANA":              "HUM",
    "UNITEDHEALTH":        "UNH",
    "CVS":                 "CVS",
    "QUEST DIAGNOSTICS":   "DGX",
    "LABCORP":             "LH",
    "CENTENE":             "CNC",
    "MOLINA":              "MOH",
    # Engineering & construction
    "JACOBS":              "J",
    "AECOM":               "ACM",
    "FLUOR":               "FLR",
    "QUANTA":              "PWR",
    "DYCOM":               "DY",
}

# Runtime cache: company name → ticker (persists for the session)
_name_ticker_cache: dict = {}

def _match_contractor_ticker(recipient_name: str) -> str | None:
    """
    Two-stage lookup:
    1. Fast static dict — catches well-known contractors instantly.
    2. Finnhub company search — dynamically finds ANY publicly traded company
       by name so we're not limited to 60 pre-listed names.
    Results are cached in-process so repeat names skip the API call.
    """
    if not recipient_name:
        return None

    name_up = recipient_name.upper()

    # Stage 1: static lookup (fast, covers most common contractors)
    for fragment, ticker in _CONTRACTOR_TICKERS.items():
        if fragment in name_up:
            _name_ticker_cache[recipient_name] = ticker
            return ticker

    # Stage 2: in-process cache from previous Finnhub searches
    if recipient_name in _name_ticker_cache:
        return _name_ticker_cache[recipient_name]   # may be None (known miss)

    # Stage 3: Finnhub company search — finds any US-listed stock
    if not FINNHUB_KEY:
        _name_ticker_cache[recipient_name] = None
        return None

    # Clean up legal suffixes that confuse search (INC, LLC, CORP, etc.)
    clean = re.sub(
        r"\b(INC\.?|LLC\.?|CORP\.?|CORPORATION|CO\.?|LTD\.?|L\.L\.C\.?|"
        r"INCORPORATED|LIMITED|HOLDINGS?|GROUP|TECHNOLOGIES?|SOLUTIONS?|"
        r"SERVICES?|SYSTEMS?|INTERNATIONAL|ENTERPRISES?)\b",
        "", name_up
    ).strip().rstrip(",").strip()

    try:
        r = requests.get(
            f"{FINNHUB_BASE}/search",
            params={"q": clean, "token": FINNHUB_KEY},
            timeout=6,
        )
        if r.status_code == 200:
            hits = r.json().get("result", [])
            # Pick first US-exchange common stock
            for h in hits[:5]:
                sym  = h.get("symbol", "")
                typ  = h.get("type", "")
                exch = h.get("primaryExchange", "")
                # Accept Common Stock on US exchanges, skip ETFs/funds/foreign
                if (typ in ("Common Stock", "") and
                        "." not in sym and          # skip BRK.B style
                        len(sym) <= 5 and
                        any(x in exch.upper() for x in ["NASDAQ", "NYSE", ""])):
                    _name_ticker_cache[recipient_name] = sym
                    print(f"[Contracts] Matched '{clean}' → {sym}")
                    return sym
    except Exception as e:
        print(f"[Contracts] Finnhub search error for '{clean}': {e}")

    _name_ticker_cache[recipient_name] = None  # cache miss to avoid re-querying
    return None

@app.route("/api/contracts/scanner")
def contracts_scanner():
    try:
        return _contracts_scanner_inner()
    except Exception as e:
        print(f"[Contracts] Fatal: {e}")
        return jsonify({"alerts": [], "total": 0, "ts": int(time.time()), "error": str(e)}), 200

def _contracts_scanner_inner():
    now = time.time()
    if _contract_cache["data"] and now - _contract_cache["ts"] < _CONTRACT_TTL:
        return jsonify(_contract_cache["data"])

    end_dt   = datetime.utcnow()
    start_dt = end_dt - timedelta(days=2)   # 48-hr window covers weekends

    body = {
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],
            "time_period": [{
                "start_date": start_dt.strftime("%Y-%m-%d"),
                "end_date":   end_dt.strftime("%Y-%m-%d"),
                "date_type":  "action_date",
            }],
            "award_amounts": [{"lower_bound": 1_000_000}],
        },
        "fields": [
            "Award ID", "Recipient Name", "Award Amount",
            "Awarding Agency", "Description", "Action Date",
        ],
        "page":  1,
        "limit": 200,
        "sort":  "Award Amount",
        "order": "desc",
    }

    try:
        resp = requests.post(
            "https://api.usaspending.gov/api/v2/search/spending_by_award/",
            json=body,
            timeout=20,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        print(f"[Contracts] API error: {e}")
        return jsonify({"alerts": [], "total": 0, "ts": int(now), "error": str(e)}), 200

    results   = raw.get("results", [])
    alerts    = []
    seen_tix  = set()

    for award in results:
        recipient = award.get("Recipient Name") or ""
        ticker    = _match_contractor_ticker(recipient)
        if not ticker or ticker in seen_tix:
            continue
        seen_tix.add(ticker)

        try:
            tk      = yf.Ticker(ticker)
            fi      = tk.fast_info
            price   = float(fi.last_price   or 0)
            prev    = float(fi.previous_close or 0)
            chg_pct = float((price - prev) / prev * 100) if prev > 0 else 0.0
            mkt_cap = float(fi.market_cap   or 0)
        except Exception:
            price = chg_pct = mkt_cap = 0.0

        if price <= 0:
            continue

        amount = float(award.get("Award Amount") or 0)
        if mkt_cap <= 0:          cap_label = "Unknown"
        elif mkt_cap < 2e9:       cap_label = "Small Cap"
        elif mkt_cap < 10e9:      cap_label = "Mid Cap"
        else:                     cap_label = "Large Cap"

        amount_fmt = (f"${amount/1e9:.2f}B" if amount >= 1e9
                      else f"${amount/1e6:.1f}M")

        alerts.append({
            "ticker":      ticker,
            "recipient":   recipient[:55],
            "amount":      int(amount),
            "amount_fmt":  amount_fmt,
            "agency":      (award.get("Awarding Agency") or "")[:50],
            "description": (award.get("Description")     or "")[:80],
            "date":        (award.get("Action Date")      or ""),
            "price":       round(float(price),   2),
            "chg_pct":     round(float(chg_pct), 2),
            "mkt_cap":     int(mkt_cap),
            "cap_label":   cap_label,
        })

    alerts.sort(key=lambda x: x["amount"], reverse=True)

    out = {"alerts": alerts[:20], "total": len(alerts), "ts": int(now)}
    _contract_cache["ts"]   = now
    _contract_cache["data"] = out
    return jsonify(out)


# ── WebSocket token (client uses this to connect directly to Finnhub WS) ──────
@app.route("/api/ws_token")
def ws_token():
    """Returns the Finnhub API key so the browser can open a WebSocket directly."""
    return jsonify({"token": FINNHUB_KEY, "enabled": bool(FINNHUB_KEY)})


# ── Pre-market / intraday gap scanner ─────────────────────────────────────────
@app.route("/api/premarket/gaps")
def premarket_gaps():
    """
    Scans ~200 liquid stocks for intraday gaps vs previous close.
    Works pre-market, at open, and intraday.
    Returns tickers with abs(gap) >= 1.5%, sorted largest first.
    Cache: 2 minutes.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed as _asc3

    UNIVERSE = SCANNER_UNIVERSE

    key = "premarket:gaps"
    if (v := cached(key, ttl=120)): return jsonify(v)

    def _gap(sym):
        try:
            q = get_quote(sym)
            curr = q.get("c") or 0
            prev = q.get("pc") or 0
            vol  = int(q.get("v") or 0)
            avg_v = int(q.get("avg_v") or 1) or 1
            if not curr or not prev or prev == 0:
                return None
            gap_pct = (curr - prev) / prev * 100
            if abs(gap_pct) < 1.5:
                return None
            return {
                "sym":       sym,
                "price":     round(curr, 2),
                "prev":      round(prev, 2),
                "gap_pct":   round(gap_pct, 2),
                "direction": "up" if gap_pct > 0 else "down",
                "vol":       vol,
                "vol_ratio": min(round(vol / avg_v, 2), 999.0) if avg_v else 0,
            }
        except Exception:
            return None

    results = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(_gap, s): s for s in UNIVERSE}
        for fut in _asc3(futs, timeout=35):
            try:
                r = fut.result()
            except Exception:
                r = None
            if r:
                results.append(r)

    results.sort(key=lambda x: abs(x["gap_pct"]), reverse=True)
    payload = {
        "gaps":    results[:15],
        "scanned": len(UNIVERSE),
        "found":   len(results),
        "ts":      int(time.time()),
    }
    return jsonify(set_cache(key, payload))


# ── VIX Dashboard ─────────────────────────────────────────────────────────────
@app.route("/api/vix_dashboard")
def vix_dashboard():
    key = "vix_dashboard"
    if (v := cached(key, ttl=60)): return jsonify(v)
    try:
        vix   = yf.Ticker("^VIX").fast_info
        vvix  = yf.Ticker("^VVIX").fast_info
        vix9d = yf.Ticker("^VIX9D").fast_info

        vix_val  = round(float(getattr(vix,  "last_price", 0) or 0), 2)
        vvix_val = round(float(getattr(vvix, "last_price", 0) or 0), 2)
        vix9_val = round(float(getattr(vix9d,"last_price", 0) or 0), 2)

        vix_prev = round(float(getattr(vix, "previous_close", 0) or 0), 2)
        vix_chg  = round(vix_val - vix_prev, 2) if vix_prev else 0
        vix_pct  = round((vix_chg / vix_prev) * 100, 2) if vix_prev else 0

        # 52-week range for percentile
        hist = yf.Ticker("^VIX").history(period="1y", interval="1d")
        vix_52_low  = round(float(hist["Close"].min()), 2) if len(hist) else 10
        vix_52_high = round(float(hist["Close"].max()), 2) if len(hist) else 80
        vix_pctile  = round(((vix_val - vix_52_low) / max(vix_52_high - vix_52_low, 1)) * 100, 1)

        # Regime classification
        if vix_val < 13:     regime, regime_color, strategy = "Ultra Low", "#00e676", "✅ Sell premium — IV cheap, buy spreads"
        elif vix_val < 18:   regime, regime_color, strategy = "Low",       "#00e676", "✅ Normal trading — defined risk setups"
        elif vix_val < 25:   regime, regime_color, strategy = "Elevated",  "#ffb300", "⚠️ Reduce size — wider stops needed"
        elif vix_val < 35:   regime, regime_color, strategy = "High Fear", "#ff3d57", "🔴 Small size or cash — extreme moves"
        else:                regime, regime_color, strategy = "Extreme",   "#ff3d57", "🚨 Stay flat — institutional capitulation"

        # Contango/backwardation signal (VIX9D vs VIX)
        if vix9_val and vix_val:
            if vix9d_vs := vix9_val - vix_val:
                term_signal = "⚠️ Backwardation — near-term fear spike" if vix9d_vs > 1.5 else "✅ Contango — normal structure"
            else:
                term_signal = "—"
        else:
            term_signal = "—"

        result = {
            "vix":          vix_val,
            "vvix":         vvix_val,
            "vix9d":        vix9_val,
            "vix_chg":      vix_chg,
            "vix_pct":      vix_pct,
            "vix_52_low":   vix_52_low,
            "vix_52_high":  vix_52_high,
            "vix_pctile":   vix_pctile,
            "regime":       regime,
            "regime_color": regime_color,
            "strategy":     strategy,
            "term_signal":  term_signal,
        }
        set_cache(key, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Multi-Timeframe Analysis ───────────────────────────────────────────────────
def _mtf_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    avg_g  = sum(gains[:period])  / period
    avg_l  = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period-1) + gains[i])  / period
        avg_l = (avg_l * (period-1) + losses[i]) / period
    if avg_l == 0: return 100.0
    return round(100 - (100 / (1 + avg_g / avg_l)), 1)

def _mtf_trend(closes):
    if len(closes) < 20: return "—", "neutral"
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else sma20
    price = closes[-1]
    if price > sma20 > sma50:   return "Strong ↑", "bullish"
    elif price > sma20:         return "Above MA ↑", "bullish"
    elif price < sma20 < sma50: return "Strong ↓", "bearish"
    elif price < sma20:         return "Below MA ↓", "bearish"
    return "Ranging", "neutral"

@app.route("/api/multitf/<ticker>")
def multi_timeframe(ticker):
    ticker = ticker.upper().strip()
    key    = f"multitf:{ticker}"
    if (v := cached(key, ttl=45)): return jsonify(v)

    timeframes = [
        ("5m",  "5 Min",  {"period": "1d",  "interval": "5m"}),
        ("15m", "15 Min", {"period": "5d",  "interval": "15m"}),
        ("1h",  "1 Hour", {"period": "1mo", "interval": "1h"}),
        ("1d",  "Daily",  {"period": "6mo", "interval": "1d"}),
    ]
    result = []
    try:
        t = yf.Ticker(ticker)
        for tf_id, tf_label, params in timeframes:
            try:
                hist   = t.history(**params)
                if len(hist) < 5: continue
                closes = [float(c) for c in hist["Close"].dropna()]
                rsi    = _mtf_rsi(closes)
                trend, trend_sent = _mtf_trend(closes)
                # RSI signal
                if rsi is None:         rsi_sig, rsi_sent = "—", "neutral"
                elif rsi >= 70:         rsi_sig, rsi_sent = f"{rsi} OB", "bearish"
                elif rsi <= 30:         rsi_sig, rsi_sent = f"{rsi} OS", "bullish"
                elif rsi >= 55:         rsi_sig, rsi_sent = f"{rsi} Bull", "bullish"
                elif rsi <= 45:         rsi_sig, rsi_sent = f"{rsi} Bear", "bearish"
                else:                   rsi_sig, rsi_sent = f"{rsi} Neut", "neutral"
                # Overall timeframe signal
                if trend_sent == "bullish" and rsi_sent == "bullish":   sig, sig_col = "BUY",  "#00e676"
                elif trend_sent == "bearish" and rsi_sent == "bearish": sig, sig_col = "SELL", "#ff3d57"
                elif trend_sent == "bullish":                            sig, sig_col = "WATCH ↑", "#ffb300"
                elif trend_sent == "bearish":                            sig, sig_col = "WATCH ↓", "#ffb300"
                else:                                                    sig, sig_col = "NEUTRAL", "#718096"
                result.append({
                    "tf":        tf_label,
                    "trend":     trend,
                    "trend_sent":trend_sent,
                    "rsi":       rsi_sig,
                    "rsi_sent":  rsi_sent,
                    "signal":    sig,
                    "sig_color": sig_col,
                })
            except Exception as e:
                print(f"[MTF] {ticker} {tf_id} error: {e}")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Alignment score: how many TFs agree
    buys  = sum(1 for r in result if r["signal"] == "BUY")
    sells = sum(1 for r in result if r["signal"] == "SELL")
    total = len(result)
    if   buys  == total: alignment = ("STRONG BUY",  "#00e676", "All timeframes aligned bullish")
    elif sells == total: alignment = ("STRONG SELL", "#ff3d57", "All timeframes aligned bearish")
    elif buys  >= 3:     alignment = ("BUY BIAS",    "#00e676", f"{buys}/{total} timeframes bullish")
    elif sells >= 3:     alignment = ("SELL BIAS",   "#ff3d57", f"{sells}/{total} timeframes bearish")
    elif buys  >= 2:     alignment = ("LEAN BULL",   "#ffb300", f"{buys}/{total} timeframes bullish")
    elif sells >= 2:     alignment = ("LEAN BEAR",   "#ffb300", f"{sells}/{total} timeframes bearish")
    else:                alignment = ("MIXED",        "#718096", "No clear directional alignment")

    out = {"ticker": ticker, "timeframes": result, "alignment": alignment[0],
           "align_color": alignment[1], "align_desc": alignment[2]}
    set_cache(key, out)
    return jsonify(out)


# ── Economic Calendar ─────────────────────────────────────────────────────────
@app.route("/api/economic_calendar")
def economic_calendar():
    key = "econ_calendar_5d"
    if (v := cached(key, ttl=3600)): return jsonify(v)

    # ── Helpers ────────────────────────────────────────────────
    def next_business_days(n=5):
        """Return the next n weekdays (Mon-Fri) starting from today."""
        days, d = [], datetime.now().date()
        while len(days) < n:
            if d.weekday() < 5:
                days.append(d)
            d += timedelta(days=1)
        return days

    HIGH_KW = ["CPI","FOMC","Fed","Nonfarm","GDP","PCE","Payroll","Unemployment",
               "PMI","Retail Sales","PPI","Interest Rate","Inflation","Balance of Trade",
               "Durable Goods","Housing Starts","Consumer Confidence","ISM"]
    LOW_KW  = ["Speech","Speaks","Auction","Bill","Bond","Note","Redbook"]

    def classify_impact(title):
        if any(k.lower() in title.lower() for k in HIGH_KW): return "high"
        if any(k.lower() in title.lower() for k in LOW_KW):  return "low"
        return "medium"

    def parse_ff_time(date_str):
        """Return (date_obj, time_str) from ForexFactory ISO timestamp."""
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            # Convert to ET (UTC-4 or UTC-5 depending on DST)
            from datetime import timezone
            import time as _time
            et_offset = -4 if _time.daylight else -5
            dt_et = dt.astimezone(timezone(timedelta(hours=et_offset)))
            return dt_et.date(), dt_et.strftime("%I:%M %p")
        except Exception:
            return None, "—"

    # ── Fetch ForexFactory JSON feeds (public, no key needed) ──
    ff_events = []
    ff_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json, text/plain, */*",
    }
    for ff_url in [
        "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
    ]:
        try:
            r = requests.get(ff_url, headers=ff_headers, timeout=8)
            if r.status_code == 200:
                for ev in r.json():
                    if ev.get("country", "").upper() != "USD":
                        continue  # US-only
                    date_obj, time_str = parse_ff_time(ev.get("date", ""))
                    title = ev.get("title", "").strip()
                    if not title or not date_obj:
                        continue
                    impact = ev.get("impact", "").lower()
                    if impact not in ("high", "medium", "low"):
                        impact = classify_impact(title)
                    ff_events.append({
                        "date_obj":  date_obj,
                        "name":      title,
                        "time":      time_str,
                        "impact":    impact,
                        "forecast":  ev.get("forecast", "") or "",
                        "previous":  ev.get("previous", "") or "",
                        "actual":    ev.get("actual",   "") or "",
                    })
        except Exception as e:
            print(f"[EconCal] FF error ({ff_url}): {e}")

    # ── Fallback: Econoday RSS if FF failed ───────────────────
    if not ff_events:
        try:
            url = "https://www.econoday.com/by_country/US/en-us/eventfeed.rss"
            r   = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                for item in root.findall(".//item")[:30]:
                    title = (item.findtext("title") or "").strip()
                    pub   = (item.findtext("pubDate") or "").strip()
                    try:
                        dt       = parsedate_to_datetime(pub)
                        date_obj = dt.date()
                        time_str = dt.strftime("%I:%M %p")
                    except Exception:
                        date_obj, time_str = None, "—"
                    if title and date_obj:
                        ff_events.append({
                            "date_obj": date_obj,
                            "name": title,
                            "time": time_str,
                            "impact": classify_impact(title),
                            "forecast": "", "previous": "", "actual": "",
                        })
        except Exception as e:
            print(f"[EconCal] econoday fallback error: {e}")

    # ── Group by next 5 business days ─────────────────────────
    bdays     = next_business_days(5)
    bdays_set = {d for d in bdays}
    days_out  = []
    today     = datetime.now().date()

    for bday in bdays:
        label = "Today" if bday == today else bday.strftime("%A, %b %d")
        day_events = sorted(
            [e for e in ff_events if e["date_obj"] == bday],
            key=lambda e: e.get("time", "")
        )
        # Remove date_obj before serializing
        clean = [{k: v for k, v in e.items() if k != "date_obj"} for e in day_events]
        days_out.append({"date": bday.strftime("%Y-%m-%d"), "label": label, "events": clean})

    # ── Hard-coded fallback if still empty ────────────────────
    total = sum(len(d["events"]) for d in days_out)
    if total == 0:
        seed_events = [
            ("Initial Jobless Claims",      "08:30 AM", "medium", "220K", "225K"),
            ("CPI m/m",                     "08:30 AM", "high",   "0.3%", "0.4%"),
            ("Core PCE Price Index m/m",    "08:30 AM", "high",   "0.2%", "0.3%"),
            ("Retail Sales m/m",            "08:30 AM", "medium", "0.4%", "0.6%"),
            ("ISM Manufacturing PMI",       "10:00 AM", "high",   "50.3", "49.8"),
            ("Nonfarm Payrolls",            "08:30 AM", "high",   "200K", "185K"),
            ("Unemployment Rate",           "08:30 AM", "high",   "4.1%", "4.1%"),
            ("GDP Growth Rate QoQ",         "08:30 AM", "high",   "2.4%", "2.1%"),
        ]
        for i, (name, t, imp, fore, prev) in enumerate(seed_events):
            d_idx = min(i // 2, 4)
            days_out[d_idx]["events"].append({
                "name": name, "time": t, "impact": imp,
                "forecast": fore, "previous": prev, "actual": "",
            })

    result = {
        "days":   days_out,
        "as_of":  datetime.now().strftime("%b %d, %H:%M"),
        "range":  "Next 5 Business Days",
    }
    set_cache(key, result)
    return jsonify(result)


# ── Relative Strength vs SPY ──────────────────────────────────────────────────
@app.route("/api/relative_strength/<ticker>")
def relative_strength(ticker):
    ticker = ticker.upper().strip()
    key    = f"rs:{ticker}"
    if (v := cached(key, ttl=120)): return jsonify(v)
    try:
        t_hist   = yf.Ticker(ticker).history(period="1d", interval="5m")
        spy_hist = yf.Ticker("SPY").history(period="1d",  interval="5m")
        if len(t_hist) < 5 or len(spy_hist) < 5:
            return jsonify({"error": "Not enough data"}), 400

        t_closes   = [float(c) for c in t_hist["Close"].dropna()]
        spy_closes = [float(c) for c in spy_hist["Close"].dropna()]
        min_len    = min(len(t_closes), len(spy_closes))
        t_closes   = t_closes[-min_len:]
        spy_closes = spy_closes[-min_len:]

        # Normalize both to 100 at open
        t_norm   = [round((c / t_closes[0])   * 100, 3) for c in t_closes]
        spy_norm = [round((c / spy_closes[0]) * 100, 3) for c in spy_closes]
        rs_line  = [round(t_norm[i] - spy_norm[i], 3) for i in range(min_len)]

        # Labels (every 5 mins from open)
        labels   = [t_hist.index[-min_len + i].strftime("%H:%M") for i in range(min_len)] if hasattr(t_hist.index[0], 'strftime') else list(range(min_len))

        # RS summary
        rs_now   = rs_line[-1]
        rs_trend = "outperforming" if rs_now > 0.5 else "underperforming" if rs_now < -0.5 else "in-line"
        t_move   = round(t_norm[-1] - 100, 2)
        spy_move = round(spy_norm[-1] - 100, 2)

        result = {
            "ticker":    ticker,
            "labels":    labels,
            "t_norm":    t_norm,
            "spy_norm":  spy_norm,
            "rs_line":   rs_line,
            "rs_now":    rs_now,
            "rs_trend":  rs_trend,
            "t_move":    t_move,
            "spy_move":  spy_move,
        }
        set_cache(key, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Dark Pool Detector ────────────────────────────────────────────────────────
@app.route("/api/dark_pool/<ticker>")
def dark_pool(ticker):
    ticker = ticker.upper().strip()
    key    = f"darkpool:{ticker}"
    if (v := cached(key, ttl=45)): return jsonify(v)
    try:
        t    = yf.Ticker(ticker)
        fi   = t.fast_info
        hist = t.history(period="5d", interval="1d")

        price      = float(getattr(fi, "last_price",               0) or 0)
        volume     = int(getattr(fi,   "last_volume",              0) or 0)
        avg_vol    = int(getattr(fi,   "three_month_average_volume",0) or 0)
        day_high   = float(getattr(fi, "day_high",                 price) or price)
        day_low    = float(getattr(fi, "day_low",                  price) or price)

        vol_ratio  = round(volume / avg_vol, 2) if avg_vol else 0
        price_range_pct = round(((day_high - day_low) / price) * 100, 2) if price else 0

        # Dark pool signature: high volume, tight price range
        # Score 0-100
        vol_score   = min(vol_ratio * 20, 50)         # up to 50 pts for volume
        tight_score = max(0, 50 - price_range_pct * 5) # up to 50 pts for tight range
        dp_score    = round(vol_score + tight_score, 1)

        if dp_score >= 70:   signal, color = "🔵 STRONG Dark Pool Activity", "#40a9ff"
        elif dp_score >= 45: signal, color = "🔵 Possible Dark Pool",        "#40a9ff"
        elif vol_ratio >= 3: signal, color = "⚡ Unusual Volume Spike",      "#ffb300"
        else:                signal, color = "✅ Normal Activity",            "#718096"

        # Infer direction from recent price action
        if len(hist) >= 2:
            recent_chg = float(hist["Close"].iloc[-1]) - float(hist["Close"].iloc[-2])
            direction  = "accumulation (bullish bias)" if recent_chg >= 0 else "distribution (bearish bias)"
        else:
            direction  = "unknown"

        result = {
            "ticker":          ticker,
            "dp_score":        dp_score,
            "signal":          signal,
            "signal_color":    color,
            "vol_ratio":       vol_ratio,
            "price_range_pct": price_range_pct,
            "direction":       direction,
            "volume":          f"{volume/1_000_000:.1f}M" if volume > 1_000_000 else f"{volume:,}",
            "avg_volume":      f"{avg_vol/1_000_000:.1f}M" if avg_vol > 1_000_000 else f"{avg_vol:,}",
        }
        set_cache(key, result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Short Interest Squeeze Screener ───────────────────────────────────────────
# High-short-interest watchlist (commonly shorted stocks worth monitoring)
_SHORT_WATCHLIST = [
    "GME","AMC","BBBY","UPST","SOFI","LCID","RIVN","NKLA","HOOD","CLOV",
    "MARA","RIOT","COIN","PLTR","ARKK","NIO","XPEV","OPEN","WISH","SPCE",
    "TSLA","NVDA","META","AAPL","MSFT","AMZN","AMD","SMCI","CVNA","W",
]

@app.route("/api/short_interest")
def short_interest():
    key = "short_interest"
    if (v := cached(key, ttl=1800)): return jsonify(v)   # 30-min cache

    results = []
    tickers_to_check = _SHORT_WATCHLIST[:20]  # limit to 20 to keep it fast

    for sym in tickers_to_check:
        try:
            info = yf.Ticker(sym).info
            short_float = info.get("shortPercentOfFloat")   # e.g. 0.12 = 12%
            short_ratio = info.get("shortRatio")            # days to cover
            shares_short= info.get("sharesShort")           # raw share count
            shares_short_prior = info.get("sharesShortPriorMonth")
            price       = info.get("currentPrice") or info.get("regularMarketPrice") or 0
            chg_pct     = info.get("regularMarketChangePercent") or 0
            vol_ratio   = 1.0
            try:
                t       = yf.Ticker(sym)
                hist    = t.history(period="21d", interval="1d")
                if len(hist) >= 5:
                    today_vol = float(hist["Volume"].iloc[-1])
                    avg_vol   = float(hist["Volume"].iloc[:-1].mean())
                    vol_ratio = round(today_vol / avg_vol, 2) if avg_vol > 0 else 1.0
            except Exception:
                pass

            if short_float is None:
                continue

            sf_pct  = round(short_float * 100, 1)
            dtc     = round(float(short_ratio), 1) if short_ratio else None

            # Squeeze Score (0–100)
            # High short float → pressure; high DTC → harder to cover; vol spike + up → squeeze active
            sf_score    = min(sf_pct * 1.5, 40)             # up to 40 pts (short float)
            dtc_score   = min((dtc or 0) * 3, 20)           # up to 20 pts (days to cover)
            vol_score   = min((vol_ratio - 1) * 10, 20)     # up to 20 pts (unusual volume)
            mom_score   = max(0, min(chg_pct * 2, 20))      # up to 20 pts (positive momentum)
            squeeze_score = round(sf_score + dtc_score + vol_score + mom_score, 1)

            # Short change MoM
            short_chg = None
            if shares_short and shares_short_prior and shares_short_prior > 0:
                short_chg = round(((shares_short - shares_short_prior) / shares_short_prior) * 100, 1)

            # Signal
            if squeeze_score >= 70:
                signal = "🔥 ACTIVE SQUEEZE"
                sig_color = "#ff6b3d"
            elif squeeze_score >= 50:
                signal = "⚡ BUILDING PRESSURE"
                sig_color = "var(--amber)"
            elif sf_pct >= 20:
                signal = "👀 HIGH SHORT FLOAT"
                sig_color = "var(--accent)"
            else:
                signal = "📊 NORMAL"
                sig_color = "var(--muted)"

            results.append({
                "sym":          sym,
                "price":        round(float(price), 2) if price else 0,
                "chg_pct":      round(float(chg_pct), 2),
                "short_float":  sf_pct,
                "days_to_cover":dtc,
                "vol_ratio":    vol_ratio,
                "short_chg_mom":short_chg,
                "squeeze_score":squeeze_score,
                "signal":       signal,
                "sig_color":    sig_color,
            })
        except Exception as e:
            print(f"[ShortInterest] {sym} error: {e}")

    # Sort by squeeze score descending
    results.sort(key=lambda x: x["squeeze_score"], reverse=True)
    result = {
        "stocks":    results[:15],
        "as_of":     datetime.now().strftime("%H:%M"),
        "source":    "yfinance (FINRA short data)",
    }
    set_cache(key, result)
    return jsonify(result)


# ── ETF Flow Monitor ───────────────────────────────────────────────────────────
_ETF_UNIVERSE = [
    {"sym": "SPY",  "name": "S&P 500",        "sector": "Broad Market"},
    {"sym": "QQQ",  "name": "Nasdaq 100",      "sector": "Broad Market"},
    {"sym": "IWM",  "name": "Russell 2000",    "sector": "Broad Market"},
    {"sym": "DIA",  "name": "Dow Jones",       "sector": "Broad Market"},
    {"sym": "XLK",  "name": "Technology",      "sector": "Sector"},
    {"sym": "XLF",  "name": "Financials",      "sector": "Sector"},
    {"sym": "XLE",  "name": "Energy",          "sector": "Sector"},
    {"sym": "XLV",  "name": "Healthcare",      "sector": "Sector"},
    {"sym": "XLI",  "name": "Industrials",     "sector": "Sector"},
    {"sym": "XLC",  "name": "Comm Services",   "sector": "Sector"},
    {"sym": "XLRE", "name": "Real Estate",     "sector": "Sector"},
    {"sym": "XLU",  "name": "Utilities",       "sector": "Sector"},
    {"sym": "XLP",  "name": "Cons Staples",    "sector": "Sector"},
    {"sym": "XLY",  "name": "Cons Discretionary","sector": "Sector"},
    {"sym": "GLD",  "name": "Gold",            "sector": "Commodity"},
    {"sym": "SLV",  "name": "Silver",          "sector": "Commodity"},
    {"sym": "TLT",  "name": "Long-Term Bonds", "sector": "Fixed Income"},
    {"sym": "HYG",  "name": "High Yield Bonds","sector": "Fixed Income"},
    {"sym": "ARKK", "name": "ARK Innovation",  "sector": "Thematic"},
    {"sym": "SQQQ", "name": "3× Short QQQ",   "sector": "Inverse"},
]

@app.route("/api/etf_flows")
def etf_flows():
    import math as _math
    key = "etf_flows"
    if (v := cached(key, ttl=120)): return jsonify(v)   # 2-min cache

    results = []
    for etf in _ETF_UNIVERSE:
        sym = etf["sym"]
        try:
            t    = yf.Ticker(sym)
            hist = t.history(period="22d", interval="1d")
            # Drop any rows with zero/NaN volume (holidays, weekends)
            hist = hist[hist["Volume"] > 0].dropna(subset=["Close","Volume"])
            if len(hist) < 2:
                continue

            price     = float(hist["Close"].iloc[-1])
            prev_close= float(hist["Close"].iloc[-2])
            chg_pct   = round(((price - prev_close) / prev_close) * 100, 2)

            today_vol = float(hist["Volume"].iloc[-1])
            avg_vol   = float(hist["Volume"].iloc[:-1].mean()) if len(hist) > 1 else today_vol
            # Guard against NaN/0 (can happen on holidays)
            if _math.isnan(today_vol) or today_vol == 0: today_vol = avg_vol
            if _math.isnan(avg_vol)   or avg_vol == 0:   avg_vol   = 1.0
            vol_ratio = round(today_vol / avg_vol, 2)

            # Guard against NaN from closed-market days
            if _math.isnan(chg_pct): chg_pct = 0.0

            # Estimate net flow: vol × direction → positive = inflow, negative = outflow
            raw_flow   = vol_ratio * chg_pct
            flow_score = round(raw_flow if not _math.isnan(raw_flow) else 0.0, 2)

            if flow_score >= 2.0:
                flow_label = "🟢 Strong Inflow"
                flow_color = "var(--green)"
            elif flow_score >= 0.5:
                flow_label = "↗ Inflow"
                flow_color = "#88cc44"
            elif flow_score <= -2.0:
                flow_label = "🔴 Strong Outflow"
                flow_color = "var(--red)"
            elif flow_score <= -0.5:
                flow_label = "↘ Outflow"
                flow_color = "#ff6666"
            else:
                flow_label = "→ Neutral"
                flow_color = "var(--muted)"

            # 5-day trend: count green vs red days
            closes  = list(hist["Close"].iloc[-6:])
            up_days = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
            trend_5d = f"{up_days}/5 up"

            results.append({
                "sym":        sym,
                "name":       etf["name"],
                "sector":     etf["sector"],
                "price":      round(price, 2),
                "chg_pct":    chg_pct,
                "vol_ratio":  vol_ratio,
                "flow_score": flow_score,
                "flow_label": flow_label,
                "flow_color": flow_color,
                "trend_5d":   trend_5d,
            })
        except Exception as e:
            print(f"[ETFFlows] {sym} error: {e}")

    # Sort: biggest inflows first, then outflows
    results.sort(key=lambda x: x["flow_score"], reverse=True)

    # Summarize rotation signal
    inflows  = [r for r in results if r["flow_score"] >= 0.5]
    outflows = [r for r in results if r["flow_score"] <= -0.5]
    rotation_note = ""
    if inflows:
        top_in  = ", ".join(r["sym"] for r in inflows[:3])
        rotation_note += f"Money flowing INTO: {top_in}. "
    if outflows:
        top_out = ", ".join(r["sym"] for r in outflows[:3])
        rotation_note += f"Rotating OUT OF: {top_out}."

    result = {
        "etfs":           results,
        "inflow_count":   len(inflows),
        "outflow_count":  len(outflows),
        "rotation_note":  rotation_note.strip(),
        "as_of":          datetime.now().strftime("%H:%M"),
    }
    set_cache(key, result)
    return jsonify(result)


_SERVER_START = int(time.time())  # unique per server restart

@app.route("/")
def index():
    # Use the directory where this server file lives — works regardless of where Python is run from
    server_dir = os.path.dirname(os.path.abspath(__file__))
    resp = send_from_directory(server_dir, "dashboard.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"]        = "no-cache"
    resp.headers["Expires"]       = "0"
    return resp


@app.route("/predictor")
def predictor():
    """Stock Probability Predictor — clean single-screen Kronos + AI prediction app."""
    server_dir = os.path.dirname(os.path.abspath(__file__))
    resp = send_from_directory(server_dir, "predictor.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"]        = "no-cache"
    resp.headers["Expires"]       = "0"
    return resp


# ── PWA static assets ─────────────────────────────────────────────────────────

@app.route("/manifest.json")
def pwa_manifest():
    server_dir = os.path.dirname(os.path.abspath(__file__))
    resp = send_from_directory(server_dir, "manifest.json", mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "public, max-age=86400"  # cache 1 day
    return resp

@app.route("/sw.js")
def pwa_sw():
    server_dir = os.path.dirname(os.path.abspath(__file__))
    resp = send_from_directory(server_dir, "sw.js", mimetype="application/javascript")
    # Service worker must NOT be cached — always fetch fresh so updates deploy immediately
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp

@app.route("/icon-192.png")
def pwa_icon_192():
    server_dir = os.path.dirname(os.path.abspath(__file__))
    resp = send_from_directory(server_dir, "icon-192.png", mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=604800"  # cache 1 week
    return resp

@app.route("/icon-512.png")
def pwa_icon_512():
    server_dir = os.path.dirname(os.path.abspath(__file__))
    resp = send_from_directory(server_dir, "icon-512.png", mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=604800"  # cache 1 week
    return resp


# ── Push Notification Endpoints ───────────────────────────────────────────────

@app.route("/api/push/status")
def push_status():
    """Return ntfy push config status."""
    topic = os.getenv("NTFY_TOPIC", NTFY_TOPIC).strip()
    return jsonify({
        "enabled": bool(topic),
        "topic":   topic if topic else None,
        "server":  NTFY_SERVER,
        "thresholds": {
            "gap_pct":      ALERT_GAP_PCT,
            "kronos_score": ALERT_KRONOS_SCORE,
            "volume_mult":  ALERT_VOLUME_MULT
        }
    })

@app.route("/api/push/test", methods=["POST"])
def push_test():
    """Send a test push notification via ntfy — returns full diagnostic info."""
    topic = os.getenv("NTFY_TOPIC", NTFY_TOPIC).strip()
    if not topic:
        return jsonify({"ok": False, "error": "NTFY_TOPIC not set", "sent": 0})

    ntfy_url = f"{NTFY_SERVER.rstrip('/')}/{topic}"
    try:
        resp = requests.post(
            ntfy_url,
            data="Push notifications are working! You'll get gap, Kronos, and volume alerts.".encode("utf-8"),
            headers={
                "Title":    "Market Genie Test Alert",
                "Priority": "default",
                "Tags":     "test",
            },
            timeout=10
        )
        return jsonify({
            "ok":        resp.status_code == 200,
            "sent":      1 if resp.status_code == 200 else 0,
            "status":    resp.status_code,
            "ntfy_url":  ntfy_url,
            "response":  resp.text[:300]
        })
    except Exception as e:
        return jsonify({"ok": False, "sent": 0, "error": str(e), "ntfy_url": ntfy_url})

# ── Custom Price Alert Store ──────────────────────────────────────────────────
# Browser registers alerts here; background thread checks them every 2 min.
# Alerts survive as long as the server process is running.
# Format: { alert_id: {ticker, price, dir, label, registered_at, fired} }

_custom_alerts      = {}
_custom_alerts_lock = threading.Lock()

@app.route("/api/alerts", methods=["GET"])
def api_alerts_list():
    with _custom_alerts_lock:
        return jsonify(list(_custom_alerts.values()))

@app.route("/api/alerts/register", methods=["POST"])
def api_alerts_register():
    """Register a price alert. Browser sends this when user adds an alert."""
    data = request.get_json(silent=True) or {}
    alert_id = str(data.get("id", ""))
    ticker   = str(data.get("ticker", "")).upper().strip()
    try:
        price = float(data.get("price", 0))
    except (TypeError, ValueError):
        price = 0
    direction = data.get("dir", "above")   # "above" or "below"
    label     = data.get("label", "")

    if not alert_id or not ticker or price <= 0:
        return jsonify({"ok": False, "error": "Missing required fields"}), 400

    with _custom_alerts_lock:
        _custom_alerts[alert_id] = {
            "id":            alert_id,
            "ticker":        ticker,
            "price":         price,
            "dir":           direction,
            "label":         label,
            "registered_at": datetime.utcnow().isoformat(),
            "fired":         False,
        }
    print(f"[PriceAlert] Registered: {ticker} {direction} ${price:.2f} (id={alert_id})")
    return jsonify({"ok": True, "id": alert_id})

@app.route("/api/alerts/clear/<alert_id>", methods=["POST", "DELETE"])
def api_alerts_clear(alert_id):
    """Remove a price alert (called when user deletes or it fires)."""
    with _custom_alerts_lock:
        removed = _custom_alerts.pop(str(alert_id), None)
    if removed:
        print(f"[PriceAlert] Cleared: {alert_id}")
    return jsonify({"ok": True, "removed": bool(removed)})

@app.route("/api/alerts/clear-all", methods=["POST"])
def api_alerts_clear_all():
    """Remove all price alerts for a clean slate."""
    with _custom_alerts_lock:
        count = len(_custom_alerts)
        _custom_alerts.clear()
    return jsonify({"ok": True, "cleared": count})

@app.route("/api/push/price-alert", methods=["POST"])
def push_price_alert():
    """
    Browser calls this immediately when an alert fires (tab is open).
    Sends an ntfy push so the user gets a phone notification even in background.
    """
    data     = request.get_json(silent=True) or {}
    ticker   = str(data.get("ticker", "")).upper()
    price    = data.get("price", 0)
    target   = data.get("target", 0)
    direction= data.get("dir", "above")
    try:
        price  = float(price)
        target = float(target)
    except (TypeError, ValueError):
        pass

    arrow = "↑" if direction == "above" else "↓"
    title = f"Price Alert: {ticker} {arrow} ${target:.2f}"
    body  = f"{ticker} reached ${price:.2f} — your {direction} ${target:.2f} alert triggered"

    sent = broadcast_push(title=title, body=body, url="/?tab=chart", tag=f"price-{ticker}")
    return jsonify({"ok": True, "sent": sent})


# ── Alert Scheduler (background thread) ──────────────────────────────────────
# Runs every 5 minutes during market hours; checks for alert conditions.
# Tracks what it already alerted to avoid duplicate pushes.

_alert_sent = {}   # key -> last_sent_ts, prevents repeat alerts within cooldown

def _alert_cooldown(key: str, seconds: int = 900) -> bool:
    """Return True if we should skip this alert (sent recently)."""
    now = time.time()
    last = _alert_sent.get(key, 0)
    if now - last < seconds:
        return True
    _alert_sent[key] = now
    return False

def _run_alert_checks():
    """Check gap scanner and Kronos scanner, fire push on thresholds."""
    try:
        # Skip if ntfy not configured
        if not os.getenv("NTFY_TOPIC", NTFY_TOPIC).strip():
            return

        now = datetime.now()
        # Only run during extended trading hours (5am–8pm ET, rough approximation)
        if not (5 <= now.hour <= 20):
            return

        # ── 1. Gap alerts ────────────────────────────────────────────────────
        try:
            gap_result = _compute_gap_scan()  # reuse gap scanner logic
            if gap_result:
                for item in gap_result[:3]:  # top 3 gaps only
                    gap = abs(item.get("gap_pct", 0))
                    ticker = item.get("ticker", "")
                    direction = "▲" if item.get("gap_pct", 0) > 0 else "▼"
                    if gap >= ALERT_GAP_PCT and not _alert_cooldown(f"gap:{ticker}", 1800):
                        broadcast_push(
                            title=f"📊 Gap Alert: {ticker} {direction}{gap:.1f}%",
                            body=f"{ticker} pre-market gap of {direction}{gap:.1f}% — price ${item.get('price', 0):.2f}",
                            url="/?tab=signal",
                            tag=f"gap-{ticker}"
                        )
        except Exception as e:
            print(f"[AlertScheduler] Gap check error: {e}")

        # ── 2. Kronos signal alerts ──────────────────────────────────────────
        try:
            cached_kron = cached("kronos_scanner", ttl=0)  # read from cache only
            if cached_kron:
                for item in cached_kron[:3]:
                    score = item.get("score", 0)
                    ticker = item.get("ticker", "")
                    direction = item.get("direction", "BULL")
                    if score >= ALERT_KRONOS_SCORE and not _alert_cooldown(f"kronos:{ticker}", 3600):
                        emoji = "🟢" if direction == "BULL" else "🔴"
                        broadcast_push(
                            title=f"{emoji} Kronos Signal: {ticker}",
                            body=f"{ticker} scored {score:.0f}/100 ({direction}) — Kronos forecast active",
                            url="/?tab=signal",
                            tag=f"kronos-{ticker}"
                        )
        except Exception as e:
            print(f"[AlertScheduler] Kronos check error: {e}")

        # ── 3. Custom price alerts (server-side backup check) ────────────────
        # These fire even if the browser tab is closed.
        try:
            with _custom_alerts_lock:
                pending = [a for a in _custom_alerts.values() if not a["fired"]]

            if pending:
                # Batch-fetch prices for all pending tickers
                tickers_needed = list({a["ticker"] for a in pending})
                prices = {}
                for sym in tickers_needed:
                    try:
                        q = get_quote(sym)
                        if q.get("c"):
                            prices[sym] = q["c"]
                    except Exception:
                        pass

                fired_ids = []
                for alert in pending:
                    sym    = alert["ticker"]
                    cur    = prices.get(sym)
                    if cur is None:
                        continue
                    target = alert["price"]
                    hit    = (alert["dir"] == "above" and cur >= target) or \
                             (alert["dir"] == "below" and cur <= target)
                    if hit and not _alert_cooldown(f"custom_price:{alert['id']}", 3600):
                        arrow = "↑" if alert["dir"] == "above" else "↓"
                        broadcast_push(
                            title=f"Price Alert: {sym} {arrow} ${target:.2f}",
                            body=f"{sym} hit ${cur:.2f} — your {alert['dir']} ${target:.2f} alert triggered",
                            url="/?tab=chart",
                            tag=f"price-{sym}"
                        )
                        fired_ids.append(alert["id"])
                        print(f"[PriceAlert] Fired (server): {sym} @ ${cur:.2f} (target {alert['dir']} ${target:.2f})")

                # Mark fired alerts
                if fired_ids:
                    with _custom_alerts_lock:
                        for fid in fired_ids:
                            if fid in _custom_alerts:
                                _custom_alerts[fid]["fired"] = True

        except Exception as e:
            print(f"[AlertScheduler] Custom price alert check error: {e}")

    except Exception as e:
        print(f"[AlertScheduler] Error: {e}")

def _compute_gap_scan():
    """Lightweight gap scan for alert scheduler (reuses cached data if fresh)."""
    cached_gaps = cached("premarket_gaps", ttl=0)
    if cached_gaps:
        return cached_gaps
    # If cache is stale, do a quick partial scan (top 30 tickers only)
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        UNIVERSE = SCANNER_UNIVERSE[:30]
        results = []
        def _quick_quote(sym):
            try:
                q = get_quote(sym)
                gap = q.get("dp", 0)
                if abs(gap) >= ALERT_GAP_PCT:
                    return {"ticker": sym, "gap_pct": gap, "price": q.get("c", 0)}
            except Exception:
                pass
            return None
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(_quick_quote, s): s for s in UNIVERSE}
            for f in as_completed(futs, timeout=20):
                r = f.result()
                if r:
                    results.append(r)
        return sorted(results, key=lambda x: abs(x["gap_pct"]), reverse=True)
    except Exception:
        return []

def _alert_scheduler_loop():
    """Runs forever, checks alerts every 2 minutes.
    Custom price alerts need faster polling than gap/Kronos checks."""
    # Stagger first check by 30s so server is fully up
    time.sleep(30)
    while True:
        try:
            _run_alert_checks()
        except Exception as e:
            print(f"[AlertScheduler] Loop error: {e}")
        time.sleep(120)  # 2-minute interval

# ── Background cache janitor — evicts stale entries every 10 minutes ──────────
def _cache_janitor_loop():
    """Periodically purge expired cache entries to prevent unbounded memory growth."""
    while True:
        try:
            time.sleep(600)  # every 10 minutes
            before = len(_cache)
            _evict_cache()
            gc.collect()
            after = len(_cache)
            if before != after:
                print(f"[CacheJanitor] Evicted {before - after} entries ({after} remain)")
        except Exception as e:
            print(f"[CacheJanitor] Error: {e}")

_janitor_thread = threading.Thread(target=_cache_janitor_loop, daemon=True, name="CacheJanitor")
_janitor_thread.start()
print("[CacheJanitor] Started — evicting stale cache entries every 10 min")

# Start the alert scheduler in a daemon thread
_alert_thread = threading.Thread(target=_alert_scheduler_loop, daemon=True, name="AlertScheduler")
_alert_thread.start()
print("[AlertScheduler] Started — checking every 5 min during market hours")


# ── Quick price check endpoint ─────────────────────────────────────────────────
@app.route("/api/heatmap")
def api_heatmap():
    """
    S&P 500 heat map — ~70 large-cap stocks grouped by sector with % change.
    Uses yf.download() batch fetch for speed. Cache: 60s.
    """
    key = "heatmap"
    if (v := cached(key, ttl=60)): return jsonify(v)

    HEATMAP_SECTORS = {
        "Technology":    [("AAPL","Apple"),("MSFT","Microsoft"),("NVDA","Nvidia"),
                          ("META","Meta"),("AVGO","Broadcom"),("ORCL","Oracle"),
                          ("CRM","Salesforce"),("AMD","AMD"),("ADBE","Adobe"),
                          ("CSCO","Cisco"),("INTC","Intel"),("QCOM","Qualcomm")],
        "Financials":    [("JPM","JPMorgan"),("V","Visa"),("MA","Mastercard"),
                          ("BAC","BofA"),("GS","Goldman"),("MS","Morgan Stanley"),
                          ("WFC","Wells Fargo"),("BLK","BlackRock"),("SCHW","Schwab")],
        "Healthcare":    [("UNH","UnitedHealth"),("LLY","Eli Lilly"),("JNJ","J&J"),
                          ("ABBV","AbbVie"),("MRK","Merck"),("TMO","Thermo Fisher"),
                          ("ABT","Abbott"),("PFE","Pfizer"),("AMGN","Amgen")],
        "Cons. Discr.":  [("AMZN","Amazon"),("TSLA","Tesla"),("HD","Home Depot"),
                          ("MCD","McDonald's"),("NKE","Nike"),("SBUX","Starbucks"),
                          ("BKNG","Booking"),("LOW","Lowe's")],
        "Comm. Svcs.":   [("GOOGL","Alphabet"),("NFLX","Netflix"),("DIS","Disney"),
                          ("CMCSA","Comcast"),("T","AT&T"),("VZ","Verizon")],
        "Industrials":   [("GE","GE"),("CAT","Caterpillar"),("HON","Honeywell"),
                          ("UPS","UPS"),("BA","Boeing"),("RTX","Raytheon"),
                          ("DE","Deere"),("LMT","Lockheed")],
        "Energy":        [("XOM","ExxonMobil"),("CVX","Chevron"),("COP","ConocoPhillips"),
                          ("SLB","SLB"),("EOG","EOG"),("MPC","Marathon")],
        "Cons. Staples": [("WMT","Walmart"),("PG","P&G"),("KO","Coca-Cola"),
                          ("PEP","PepsiCo"),("COST","Costco"),("PM","Philip Morris")],
        "Materials":     [("LIN","Linde"),("APD","Air Products"),("FCX","Freeport"),
                          ("NEM","Newmont"),("NUE","Nucor")],
    }

    # Flatten
    sym_to_info = {}
    for sector, stocks in HEATMAP_SECTORS.items():
        for sym, name in stocks:
            sym_to_info[sym] = {"name": name, "sector": sector}

    all_syms = list(sym_to_info.keys())

    try:
        # Batch download — single HTTP call instead of 70 individual ones
        raw = yf.download(
            all_syms, period="2d", interval="1d",
            group_by="ticker", progress=False, threads=True,
            auto_adjust=True, actions=False
        )

        def get_pct(sym):
            try:
                if len(all_syms) == 1:
                    closes = raw["Close"].dropna()
                else:
                    closes = raw[sym]["Close"].dropna()
                if len(closes) >= 2:
                    return round(((float(closes.iloc[-1]) - float(closes.iloc[-2])) /
                                   float(closes.iloc[-2])) * 100, 2)
                return 0.0
            except Exception:
                return None

        sectors_out = []
        for sector_name, stocks in HEATMAP_SECTORS.items():
            sector_stocks = []
            for sym, name in stocks:
                pct = get_pct(sym)
                if pct is None:
                    continue
                sector_stocks.append({"sym": sym, "name": name, "pct": pct})
            if sector_stocks:
                # Sort by pct desc within sector
                sector_stocks.sort(key=lambda x: x["pct"], reverse=True)
                sectors_out.append({"name": sector_name, "stocks": sector_stocks})

        # Free the large DataFrame immediately — it can be 50-100 MB in memory
        del raw
        gc.collect()

        result = {"sectors": sectors_out, "as_of": datetime.now().strftime("%H:%M")}
        return jsonify(set_cache(key, result))

    except Exception as e:
        print(f"[Heatmap] error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart/<ticker>/<tf>")
def api_chart_tf(ticker, tf):
    """
    Multi-timeframe chart data.
    tf values: 1D (1m today), 5D (5m × 5 days), 1M (30m × 1 month), 3M (1d × 3 months), 1Y (1d × 1 year)
    Returns OHLCV + full technicals aligned to the timeframe.
    Cache: 30s for intraday, 300s for daily.
    """
    ticker = ticker.upper().strip()
    tf     = tf.upper().strip()
    if tf not in ("1D","5D","1M","3M","1Y"):
        return jsonify({"error": "invalid timeframe"}), 400

    cache_key = f"chart:{ticker}:{tf}"
    ttl = 30 if tf in ("1D","5D") else 300
    if (v := cached(cache_key, ttl=ttl)): return jsonify(v)

    # Map to yfinance period/interval
    TF_MAP = {
        "1D": ("1d",  "1m"),
        "5D": ("5d",  "5m"),
        "1M": ("1mo", "30m"),
        "3M": ("3mo", "1d"),
        "1Y": ("1y",  "1d"),
    }
    period, interval = TF_MAP[tf]

    chart_labels = []; chart_timestamps = []; chart_opens = []
    chart_highs  = []; chart_lows      = []; chart_prices = []; chart_vols = []

    try:
        hist = yf.Ticker(ticker).history(period=period, interval=interval)
        if hist is None or hist.empty:
            return jsonify({"error": "no data"}), 404
        for idx, row in hist.iterrows():
            # yfinance returns timezone-aware timestamps — convert to UTC unix seconds
            ts = int(idx.timestamp())
            chart_labels.append(idx.strftime("%m/%d %H:%M" if tf in ("5D","1M") else ("%H:%M" if tf == "1D" else "%m/%d/%y")))
            chart_timestamps.append(ts)
            chart_opens.append(round(float(row["Open"]),   2))
            chart_highs.append(round(float(row["High"]),   2))
            chart_lows.append(round(float(row["Low"]),    2))
            chart_prices.append(round(float(row["Close"]), 2))
            chart_vols.append(round(float(row["Volume"]) / 1_000_000, 4))
    except Exception as e:
        print(f"[Chart/{tf}] {ticker} error: {e}")
        return jsonify({"error": str(e)}), 500

    # Deduplicate timestamps (same logic as scan endpoint)
    if len(chart_timestamps) > 1:
        seen = {}
        for i, ts in enumerate(chart_timestamps): seen[ts] = i
        keep = sorted(seen.values())
        if len(keep) < len(chart_timestamps):
            chart_labels     = [chart_labels[i]     for i in keep]
            chart_timestamps = [chart_timestamps[i] for i in keep]
            chart_opens      = [chart_opens[i]      for i in keep]
            chart_highs      = [chart_highs[i]      for i in keep]
            chart_lows       = [chart_lows[i]       for i in keep]
            chart_prices     = [chart_prices[i]     for i in keep]
            chart_vols       = [chart_vols[i]       for i in keep]

    technicals = {}
    if len(chart_prices) >= 10:
        try:
            raw_vols = [v * 1_000_000 for v in chart_vols]
            technicals = compute_technicals(chart_prices, chart_highs, chart_lows, raw_vols)
        except Exception as te:
            print(f"[Chart/{tf}] {ticker} technicals error: {te}")

    result = {
        "ticker": ticker, "tf": tf,
        "labels":     chart_labels,
        "timestamps": chart_timestamps,
        "opens":      chart_opens,
        "highs":      chart_highs,
        "lows":       chart_lows,
        "prices":     chart_prices,
        "volumes":    chart_vols,
        **technicals,
    }
    return jsonify(set_cache(cache_key, result))


def _ai_setup_detector(price, chg_pct, technicals, social, options, quote):
    """
    Reads all available indicator data and generates a plain-English trade setup
    summary with a bias (Bullish / Bearish / Neutral) and key reasons.
    """
    signals = []
    bias_score = 0   # positive = bullish, negative = bearish

    closes = technicals.get("prices", []) if isinstance(technicals, dict) and "prices" in technicals else []
    rsi_arr   = technicals.get("rsi",       []) or []
    macd_h    = technicals.get("macd_hist", []) or []
    sqz_on_a  = technicals.get("sqz_on",   []) or []
    sqz_c     = technicals.get("sqz_color", []) or []
    vwap_a    = technicals.get("vwap",      []) or []

    # Last non-None values
    def last_val(arr):
        return next((v for v in reversed(arr) if v is not None), None)

    rsi   = last_val(rsi_arr)
    mh    = last_val(macd_h)
    sqz   = last_val(sqz_on_a)
    sqz_col = last_val(sqz_c)
    vwap_val= last_val(vwap_a)

    # RSI signal
    if rsi is not None:
        if rsi > 70:
            signals.append(f"RSI {rsi:.0f} — overbought territory"); bias_score -= 1
        elif rsi < 30:
            signals.append(f"RSI {rsi:.0f} — oversold, watch for bounce"); bias_score += 2
        elif rsi > 55:
            signals.append(f"RSI {rsi:.0f} — momentum trending up"); bias_score += 1
        elif rsi < 45:
            signals.append(f"RSI {rsi:.0f} — momentum fading"); bias_score -= 1

    # MACD histogram
    if mh is not None:
        if mh > 0:
            signals.append("MACD histogram positive — bulls in control"); bias_score += 1
        else:
            signals.append("MACD histogram negative — bears in control"); bias_score -= 1

    # TTM Squeeze
    if sqz is not None:
        if sqz:
            signals.append("🟥 TTM Squeeze ACTIVE — explosive move building"); bias_score += 0  # directional unknown
        elif sqz_col in ("lime", "darkgreen"):
            signals.append("🟢 Squeeze FIRED bullish — momentum expanding up"); bias_score += 2
        elif sqz_col in ("orange", "red"):
            signals.append("🔴 Squeeze FIRED bearish — momentum expanding down"); bias_score -= 2

    # VWAP position
    if vwap_val and price:
        diff_pct = ((price - vwap_val) / vwap_val) * 100
        if diff_pct > 0.3:
            signals.append(f"Price ${price:.2f} above VWAP ${vwap_val:.2f} (+{diff_pct:.1f}%) — bullish intraday bias"); bias_score += 1
        elif diff_pct < -0.3:
            signals.append(f"Price ${price:.2f} below VWAP ${vwap_val:.2f} ({diff_pct:.1f}%) — bearish intraday bias"); bias_score -= 1
        else:
            signals.append(f"Price hugging VWAP ${vwap_val:.2f} — indecision zone")

    # Social sentiment
    st_score = social.get("sentimentScore", 0.5) if social else 0.5
    if st_score >= 0.7:
        signals.append("Social sentiment 🔥 HOT — crowd momentum bullish"); bias_score += 1
    elif st_score <= 0.3:
        signals.append("Social sentiment bearish — crowd fading"); bias_score -= 1

    # Options flow
    pc = options.get("pcRatio") if options else None
    try:
        pc_f = float(pc) if pc else None
        if pc_f is not None:
            if pc_f < 0.5:
                signals.append(f"Put/Call ratio {pc_f:.2f} — heavy call buying, bullish flow"); bias_score += 1
            elif pc_f > 1.5:
                signals.append(f"Put/Call ratio {pc_f:.2f} — heavy put buying, hedging detected"); bias_score -= 1
    except Exception:
        pass

    # Day change
    if chg_pct > 3:
        signals.append(f"Strong gap/momentum +{chg_pct:.1f}% on the day"); bias_score += 1
    elif chg_pct < -3:
        signals.append(f"Significant sell-off {chg_pct:.1f}% on the day"); bias_score -= 1

    # Final bias
    if bias_score >= 3:
        bias, bias_color, bias_icon = "STRONG BULL", "#00e676", "🚀"
    elif bias_score >= 1:
        bias, bias_color, bias_icon = "BULLISH",     "#00e676", "📈"
    elif bias_score <= -3:
        bias, bias_color, bias_icon = "STRONG BEAR", "#ff3d57", "🐻"
    elif bias_score <= -1:
        bias, bias_color, bias_icon = "BEARISH",     "#ff3d57", "📉"
    else:
        bias, bias_color, bias_icon = "NEUTRAL",     "#ffb300", "⚖"

    return {
        "bias":       bias,
        "biasColor":  bias_color,
        "biasIcon":   bias_icon,
        "biasScore":  bias_score,
        "signals":    signals[:5],   # top 5 signals
        "summary":    f"{bias_icon} {bias}: " + " · ".join(signals[:3]) if signals else f"{bias_icon} {bias}: No strong signals detected.",
    }


# ── AI Trade Thesis ───────────────────────────────────────────────────────────
# Aggregates all available data for a ticker and asks Claude for a structured
# bull case / bear case / recommended action.
# Requires CLAUDE_KEY in Railway Variables (or .env locally).
# NOTE: Named CLAUDE_KEY (not ANTHROPIC_API_KEY) to avoid Railway's
# BuildKit secret injection which causes build failures on *_KEY variable names.

@app.route("/api/thesis/<ticker>")
def api_thesis(ticker):
    ticker = ticker.upper().strip()

    # ── Gate on API key ───────────────────────────────────────────────────────
    api_key = os.getenv("CLAUDE_KEY", "").strip()
    if not api_key:
        return jsonify({
            "ok": False,
            "error": "CLAUDE_KEY not configured",
            "setup": "Add CLAUDE_KEY to Railway Variables (get one free at console.anthropic.com)"
        }), 503

    # ── Cache — thesis is expensive, cache 10 minutes ─────────────────────────
    cache_key = f"thesis:{ticker}"
    if (v := cached(cache_key, ttl=600)):
        return jsonify(v)

    # ── Gather data from all available sources ────────────────────────────────
    data = {"ticker": ticker}

    # 1. Quote
    try:
        q = get_quote(ticker)
        data["price"]      = q.get("c", 0)
        data["change_pct"] = q.get("dp", 0)
        data["volume"]     = q.get("v", 0)
        data["avg_volume"] = q.get("avg_v", 0)
        data["open"]       = q.get("o", 0)
        data["high"]       = q.get("h", 0)
        data["low"]        = q.get("l", 0)
        data["prev_close"] = q.get("pc", 0)
        vol_ratio = round(q.get("v", 0) / q.get("avg_v", 1), 2) if q.get("avg_v") else "N/A"
        data["vol_ratio"]  = vol_ratio
    except Exception as e:
        data["quote_error"] = str(e)

    # 2. Recent news headlines (last 5)
    try:
        today    = datetime.utcnow().strftime("%Y-%m-%d")
        week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        news = fh_get("/company-news", {"symbol": ticker, "from": week_ago, "to": today})
        headlines = [n.get("headline", "") for n in (news or [])[:5] if n.get("headline")]
        data["headlines"] = headlines
    except Exception:
        data["headlines"] = []

    # 3. StockTwits sentiment
    try:
        st_r = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json",
            headers={"User-Agent": "MarketGenie/1.0"}, timeout=5
        )
        if st_r.status_code == 200:
            msgs = st_r.json().get("messages", [])
            bulls = sum(1 for m in msgs if (m.get("entities", {}).get("sentiment") or {}).get("basic") == "Bullish")
            bears = sum(1 for m in msgs if (m.get("entities", {}).get("sentiment") or {}).get("basic") == "Bearish")
            total = bulls + bears
            data["st_bull_pct"] = round(bulls / total * 100) if total else 50
            data["st_bear_pct"] = round(bears / total * 100) if total else 50
            data["st_messages"]  = len(msgs)
    except Exception:
        pass

    # 4. Kronos score (from cache — only present if scanner has run)
    try:
        kron_cache = cached("kronos_scanner", ttl=0)
        if kron_cache:
            match = next((k for k in kron_cache if k.get("ticker") == ticker), None)
            if match:
                data["kronos_score"]     = match.get("score", 0)
                data["kronos_direction"] = match.get("direction", "N/A")
                data["kronos_signals"]   = match.get("signals", [])
    except Exception:
        pass

    # 5. Pre-market gap (from cache)
    try:
        gaps = cached("premarket_gaps", ttl=0)
        if gaps:
            gap_match = next((g for g in gaps if g.get("ticker") == ticker), None)
            if gap_match:
                data["premarket_gap_pct"] = gap_match.get("gap_pct", 0)
    except Exception:
        pass

    # 6. Short interest (from cache)
    try:
        si_cache = cached("short_interest", ttl=0)
        if si_cache and isinstance(si_cache, list):
            si_match = next((s for s in si_cache if s.get("ticker") == ticker), None)
            if si_match:
                data["short_float_pct"] = si_match.get("shortFloatPct", 0)
                data["days_to_cover"]   = si_match.get("daysToCover", 0)
    except Exception:
        pass

    # 7. Finnhub company profile (sector/industry)
    try:
        profile = fh_get("/stock/profile2", {"symbol": ticker})
        data["company_name"] = profile.get("name", ticker)
        data["industry"]     = profile.get("finnhubIndustry", "Unknown")
        data["market_cap"]   = profile.get("marketCapitalization", 0)
    except Exception:
        data["company_name"] = ticker

    # ── Build the prompt ──────────────────────────────────────────────────────
    vol_str = (f"{data.get('vol_ratio', 'N/A')}x average"
               if isinstance(data.get("vol_ratio"), (int, float))
               else "unknown")

    headlines_str = "\n".join(f"  - {h}" for h in data.get("headlines", [])) or "  - No recent news found"

    kronos_str = ""
    if "kronos_score" in data:
        kronos_str = (f"\nKronos AI Score: {data['kronos_score']:.0f}/100 "
                      f"({data['kronos_direction']}) — signals: {', '.join(data.get('kronos_signals', []))}")

    gap_str = (f"\nPre-market gap: {data['premarket_gap_pct']:+.1f}%"
               if "premarket_gap_pct" in data else "")

    si_str = (f"\nShort interest: {data.get('short_float_pct', 0):.1f}% of float, "
              f"{data.get('days_to_cover', 0):.1f} days to cover"
              if "short_float_pct" in data else "")

    st_str = (f"\nStockTwits sentiment: {data.get('st_bull_pct', 50)}% bullish / "
              f"{data.get('st_bear_pct', 50)}% bearish ({data.get('st_messages', 0)} messages)"
              if "st_messages" in data else "")

    mktcap = data.get("market_cap", 0)
    cap_str = (f"${mktcap/1000:.1f}B" if mktcap >= 1000 else
               f"${mktcap:.0f}M" if mktcap else "unknown")

    prompt = f"""You are a professional day trader and market analyst. Analyze the following real-time market data for {ticker} ({data.get('company_name', ticker)}) and produce a concise trade thesis.

MARKET DATA (as of right now):
Ticker: {ticker}
Company: {data.get('company_name', ticker)} | Industry: {data.get('industry', 'Unknown')} | Market Cap: {cap_str}
Price: ${data.get('price', 0):.2f} | Change: {data.get('change_pct', 0):+.2f}%
Open: ${data.get('open', 0):.2f} | High: ${data.get('high', 0):.2f} | Low: ${data.get('low', 0):.2f} | Prev Close: ${data.get('prev_close', 0):.2f}
Volume: {vol_str}{gap_str}{kronos_str}{si_str}{st_str}

RECENT NEWS HEADLINES:
{headlines_str}

INSTRUCTIONS:
Based solely on the data above, produce a structured JSON trade thesis. Be specific, direct, and actionable — no filler language. Write for an experienced day trader who wants signal, not explanation.

Return ONLY valid JSON in this exact format (no markdown, no extra text):
{{
  "bull_case": "2 sentences maximum. What could drive this stock up today? Be specific to the data.",
  "bear_case": "2 sentences maximum. What could push this stock down? Be specific to the data.",
  "action": "BUY | SELL | WATCH | AVOID",
  "action_reason": "One sentence explaining the action recommendation.",
  "confidence": 7,
  "key_catalysts": ["catalyst 1", "catalyst 2", "catalyst 3"],
  "risk_level": "LOW | MEDIUM | HIGH",
  "time_horizon": "Intraday | Swing (2-5 days) | Both"
}}

confidence is an integer 1-10. Be honest — use 5 for neutral/unclear setups, not 7+ for everything.
"""

    # ── Call Claude via plain HTTP (no anthropic SDK needed) ─────�