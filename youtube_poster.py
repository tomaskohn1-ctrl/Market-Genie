"""
youtube_poster.py — Market Genie → YouTube Shorts auto-poster
──────────────────────────────────────────────────────────────
Generates a 30-second Short from live Alpaca + breadth data and uploads
to YouTube 3× per day:  9:15 AM (pre-market)  ·  12:00 PM (midday)  ·  4:15 PM (EOD)

Improvements over v1:
  • 3-slide video (hook → dashboard → P&L close-up) with xfade transitions
  • TTS voiceover via gTTS (reads regime, top mover, P&L, CTA)
  • Viral-style title hooks optimised for Shorts discovery

Setup (one-time):
  1. Run youtube_setup.py locally → follow browser OAuth flow
  2. Copy the printed YOUTUBE_TOKEN_JSON value into Railway env vars
  3. Add YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET to Railway env vars
  4. Redeploy — scheduler starts automatically

Dependencies (already in requirements.txt after update):
  google-api-python-client  google-auth-oauthlib  Pillow  gtts
"""

import os, json, base64, time, threading, subprocess, tempfile
from datetime import datetime
from pathlib import Path

import requests

# ── Constants ────────────────────────────────────────────────────────────────
_YT_SCOPES   = ["https://www.googleapis.com/auth/youtube.upload"]
_VIDEO_W     = 1080
_VIDEO_H     = 1920
_VIDEO_SECS  = 60   # 9+9+10+14+10+8 = 60s (6-slide layout, Shorts max)
_CATEGORY_ID = "25"   # News & Politics

# Slide durations (must sum to _VIDEO_SECS)
_SLIDE1_SECS = 9    # hook — biggest mover / regime
_SLIDE2_SECS = 9    # market overview — SPY/QQQ/VIX/futures + live stats
_SLIDE3_SECS = 10   # AI signals — top 3 with confidence
_SLIDE4_SECS = 14   # trade setup — entry/stop/target/R:R (longest — people screenshot)
_SLIDE5_SECS = 10   # watchlist — 3-5 stocks with key levels (people screenshot)
_SLIDE6_SECS = 8    # CTA — signal count, follow, comment ask
_FADE_SECS   = 0.5  # xfade duration

# ── Credentials ──────────────────────────────────────────────────────────────
def _get_yt_credentials():
    """Decode token from YOUTUBE_TOKEN_JSON env var (base64 JSON)."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        token_b64 = os.getenv("YOUTUBE_TOKEN_JSON", "")
        if not token_b64:
            print("[YouTube] ⚠️  YOUTUBE_TOKEN_JSON not set — posting disabled")
            return None

        token_b64 = token_b64.strip()
        padding   = 4 - (len(token_b64) % 4)
        if padding != 4:
            token_b64 += "=" * padding
        try:
            raw = base64.b64decode(token_b64)
        except Exception:
            raw = base64.urlsafe_b64decode(token_b64)
        token_data = json.loads(raw.decode("utf-8"))
        creds = Credentials(
            token         = token_data.get("token"),
            refresh_token = token_data.get("refresh_token"),
            token_uri     = "https://oauth2.googleapis.com/token",
            client_id     = os.getenv("YOUTUBE_CLIENT_ID", ""),
            client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", ""),
            scopes        = _YT_SCOPES,
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds
    except Exception as e:
        print(f"[YouTube] ❌ Credentials error: {e}")
        return None


def _get_yt_service():
    from googleapiclient.discovery import build
    creds = _get_yt_credentials()
    if not creds:
        return None
    return build("youtube", "v3", credentials=creds)


# ── Market Data ───────────────────────────────────────────────────────────────
_HOT_FALLBACK = ["NVDA", "TSLA", "AAPL", "META", "MSFT", "AMZN", "GOOGL", "MSTR"]

def _fetch_ticker_moves(symbols: list) -> list:
    """Return [{symbol, price, pct, up}] for each symbol via yfinance fast_info."""
    results = []
    try:
        import yfinance as yf
        for sym in symbols[:6]:
            try:
                t  = yf.Ticker(sym)
                fi = t.fast_info
                price = (getattr(fi, "last_price", None)
                         or getattr(fi, "regularMarketPrice", None) or 0)
                prev  = (getattr(fi, "previous_close", None)
                         or getattr(fi, "regularMarketPreviousClose", None) or price)
                price = float(price or 0)
                prev  = float(prev  or price)
                pct   = ((price - prev) / prev * 100) if prev else 0.0
                if price >= 15:   # filter low-price stocks — keeps movers list credible
                    results.append({"symbol": sym, "price": price, "pct": pct, "up": pct >= 0})
            except Exception as e:
                print(f"[YouTube] Ticker {sym} error: {e}")
    except Exception as e:
        print(f"[YouTube] yfinance import error: {e}")
    return results


def _fetch_market_data() -> dict:
    """Pull rich live data from /api/youtube/data (single server call)."""
    port = int(os.getenv("PORT", "8080"))

    defaults = {
        "regime": "NEUTRAL", "regime_score": 50,
        "pnl_today": 0.0, "equity": 100_000.0,
        "positions": [], "nq_pct": 0.0, "spy_pct": 0.0,
        "qqq_pct": 0.0, "vix": 16.5,
        "hot_tickers": [], "social_hot": [], "ai_signals": [],
        "timestamp": datetime.now().strftime("%b %d, %Y  ·  %I:%M %p ET"),
    }

    try:
        r = requests.get(f"http://localhost:{port}/api/youtube/data", timeout=8)
        if r.status_code == 200:
            srv = r.json()
            defaults.update({k: srv[k] for k in srv if k in defaults})
            # hot_tickers: prefer Alpaca live cache; fall back to yfinance for real data
            ht = srv.get("hot_tickers", [])
            # If Alpaca cache has data but all moves are tiny/zero (outside hours), refresh via yfinance
            if ht and all(abs(t.get("pct", 0)) < 0.1 for t in ht):
                ht = []  # stale — force yfinance refresh
            if ht:
                defaults["hot_tickers"] = ht
            else:
                # Prefer signal tickers first (most relevant for traders), then social, then fallback
                sig_syms    = [s["symbol"] for s in srv.get("ai_signals", []) if s.get("symbol")][:5]
                social_syms = [t["symbol"] for t in srv.get("social_hot", [])[:6] if t.get("symbol")]
                candidates  = list(dict.fromkeys(sig_syms + social_syms + _HOT_FALLBACK))[:10]
                defaults["hot_tickers"] = _fetch_ticker_moves(candidates)
    except Exception as e:
        print(f"[YouTube] Data fetch error: {e}")

    # Sanity filter: drop tickers with >25% move — likely bad pre-market snapshot
    # (e.g. HPE showed -21.9% pre-market when it was actually +33% at open)
    defaults["hot_tickers"] = [
        t for t in defaults["hot_tickers"]
        if abs(t.get("pct", 0)) <= 25
    ] or defaults["hot_tickers"]  # keep all if everything is filtered

    if not defaults["hot_tickers"]:
        defaults["hot_tickers"] = _fetch_ticker_moves(_HOT_FALLBACK[:6])

    return defaults


# ── Font loader ───────────────────────────────────────────────────────────────
def _load_font(size: int, bold: bool = False):
    from PIL import ImageFont
    suffix = "-Bold" if bold else ""
    _here = Path(__file__).parent
    candidates = [
        str(_here / "fonts" / f"DejaVuSans{suffix}.ttf"),
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{suffix}.ttf",
        f"/usr/share/fonts/truetype/liberation/LiberationSans{suffix}.ttf",
        f"/usr/share/fonts/truetype/ubuntu/Ubuntu-{'B' if bold else 'R'}.ttf",
        f"/usr/share/fonts/truetype/freefont/FreeSans{'Bold' if bold else ''}.ttf",
    ]
    for path in candidates:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        except Exception:
            pass
    print(f"[YouTube] ⚠️  No truetype font found — text will render small")
    return ImageFont.load_default()


# ══════════════════════════════════════════════════════════════════════════════
# DESIGN SYSTEM — matches dashboard.html CSS variables exactly
# ══════════════════════════════════════════════════════════════════════════════
_C = {
    "bg":        (8,   8,   16),
    "surface":   (14,  14,  28),
    "card":      (17,  17,  32),
    "accent":    (91,  127, 255),
    "accent_hi": (123, 155, 255),
    "purple":    (155, 89,  245),
    "blue":      (64,  169, 255),
    "green":     (0,   230, 118),
    "red":       (255, 61,  87),
    "amber":     (255, 179, 0),
    "text":      (226, 232, 240),
    "text_hi":   (247, 250, 252),
    "subtext":   (113, 128, 150),
    "muted":     (74,  85,  104),
    "green_dim": (8,   26,  18),
    "red_dim":   (28,  10,  14),
    "amber_dim": (28,  20,  4),
    "accent_dim":(12,  16,  34),
}

def _regime_color(regime):
    return {"BULLISH": _C["green"], "BEARISH": _C["red"]}.get(regime, _C["amber"])

def _pct_color(v):
    return _C["green"] if v >= 0 else _C["red"]

def _mk_bg():
    try:
        import numpy as np
        arr = __import__("numpy").zeros((_VIDEO_H, _VIDEO_W, 3), dtype=__import__("numpy").uint8)
        for y in range(_VIDEO_H):
            t = y / _VIDEO_H
            arr[y, :] = [int(8+t*5), int(8+t*4), int(16+t*12)]
        from PIL import Image
        return Image.fromarray(arr, "RGB")
    except Exception:
        from PIL import Image
        return Image.new("RGB", (_VIDEO_W, _VIDEO_H), _C["bg"])

def _draw_glow_card(d, x, y, x2, y2, radius=16, accent=None, fill=None):
    ac = accent or _C["accent"]
    bg = fill   or _C["card"]
    glow = tuple(max(0, int(c * 0.10)) for c in ac)
    d.rounded_rectangle([x-3, y-3, x2+3, y2+3], radius=radius+3, fill=glow)
    d.rounded_rectangle([x, y, x2, y2], radius=radius, fill=bg)
    border = tuple(max(0, int(c * 0.35)) for c in ac)
    d.rounded_rectangle([x, y, x2, y2], radius=radius, outline=border, width=2)

def _draw_header(d, W, label_left, label_right, color_right, fnt_l, fnt_r, timestamp=""):
    PAD = 52
    d.rectangle([0, 0, W, 120], fill=_C["surface"])
    d.text((PAD, 60), label_left,  font=fnt_l, fill=_C["accent_hi"], anchor="lm")
    d.text((W-PAD, 60), label_right, font=fnt_r, fill=color_right,   anchor="rm")
    for i in range(W):
        t = i / W
        intensity = 1.0 - abs(2*t - 1)**0.6
        c = tuple(int(cc * intensity) for cc in _C["accent"])
        d.line([(i, 118), (i, 121)], fill=c)
    if timestamp:
        d.text((PAD, 148), timestamp, font=fnt_l, fill=_C["muted"])

def _draw_caption_bar(d, W, H, caption_text, fnt_caption, fnt_nano):
    BAR_H = 105
    d.rectangle([0, H-BAR_H, W, H], fill=_C["surface"])
    for i in range(W):
        t = i / W
        intensity = 1.0 - abs(2*t - 1)**0.6
        c = tuple(int(cc * intensity * 0.8) for cc in _C["accent"])
        d.line([(i, H-BAR_H), (i, H-BAR_H+2)], fill=c)
    d.text((W//2, H-BAR_H+38), caption_text, font=fnt_caption, fill=_C["text"], anchor="mm")
    d.text((W//2, H-20), "marketgenie.ai  NOT FINANCIAL ADVICE", font=fnt_nano, fill=_C["muted"], anchor="mm")

def _draw_breaking_badge(d, W, symbol, pct, fnt):
    d.rectangle([0, 122, W, 218], fill=(140, 10, 28))
    sign = "+" if pct >= 0 else ""
    d.text((W//2, 170), f"BREAKING  {symbol} {sign}{pct:.1f}%", font=fnt, fill=_C["text_hi"], anchor="mm")

def _draw_conf_bar(d, x, y, w, h, pct, color):
    TRACK = (30, 30, 50)
    d.rounded_rectangle([x, y, x+w, y+h], radius=h//2, fill=TRACK)
    fw = max(4, int(w * min(pct/100, 1.0)))
    d.rounded_rectangle([x, y, x+fw, y+h], radius=h//2, fill=color)

def _draw_badge(d, x, y, text, color, fnt):
    try:    bw = int(d.textlength(text, font=fnt)) + 28
    except: bw = 160
    bh = 44
    dim    = tuple(max(0, int(c * 0.15)) for c in color)
    border = tuple(max(0, int(c * 0.60)) for c in color)
    d.rounded_rectangle([x, y, x+bw, y+bh], radius=bh//2, fill=dim)
    d.rounded_rectangle([x, y, x+bw, y+bh], radius=bh//2, outline=border, width=2)
    d.text((x+bw//2, y+bh//2), text, font=fnt, fill=color, anchor="mm")
    return bw

# ── Legacy colour helpers (kept for compatibility) ────────────────────────────
def _regime_rgb(regime):
    return _regime_color(regime)

def _ic(v):
    return _pct_color(v)

def _dim_color(rgb, factor=7):
    return (rgb[0]//factor, rgb[1]//factor, rgb[2]//factor)


# ─────────────────────────────────────────────────────────────────────────────
# SHARED DRAWING HELPERS (legacy alert/signal cards — kept for compatibility)
# ─────────────────────────────────────────────────────────────────────────────
def _draw_alert_card(d, x, y, w, h, symbol, pct, price, color, fnt_sym, fnt_pct, fnt_price, fnt_badge):
    """Clean card: colored left border, symbol left, large % right, price below %."""
    BG_CARD  = (14, 20, 32)
    BORDER_W = 8
    WHITE    = (225, 235, 248)
    DIM      = (110, 128, 150)
    d.rounded_rectangle([x, y, x + w, y + h], radius=12, fill=BG_CARD)
    d.rounded_rectangle([x, y, x + BORDER_W, y + h], radius=4, fill=color)
    d.rounded_rectangle([x, y, x + w, y + h], radius=12, outline=color, width=1)
    cx = x + BORDER_W + 24
    cy = y + h // 2
    d.text((cx, cy - 10), symbol, font=fnt_sym, fill=WHITE, anchor="lm")
    sign = "+" if pct >= 0 else ""
    d.text((x + w - 24, cy - 18), f"{sign}{pct:.2f}%", font=fnt_pct, fill=color, anchor="rm")
    d.text((x + w - 24, cy + 30), f"${price:,.2f}", font=fnt_price, fill=DIM, anchor="rm")


def _draw_signal_card(d, x, y, w, h, symbol, direction, confidence, fnt_sym, fnt_conf, fnt_badge, fnt_label):
    """Draw an AI signal card with BULL/BEAR badge and confidence bar."""
    BG_CARD = (16, 22, 34)
    GREEN   = (74, 222, 128)
    RED     = (248, 113, 113)
    WHITE   = (225, 235, 248)
    DIM     = (110, 128, 150)
    DIV     = (30, 42, 58)
    BORDER_W = 6

    bull  = "bull" in direction.lower()
    color = GREEN if bull else RED
    label = "BULL" if bull else "BEAR"

    d.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=BG_CARD)
    d.rounded_rectangle([x, y, x + BORDER_W, y + h], radius=3, fill=color)
    d.rounded_rectangle([x, y, x + w, y + h], radius=10, outline=color, width=1)

    # Symbol
    d.text((x + BORDER_W + 20, y + 20), symbol, font=fnt_sym, fill=WHITE)

    # BULL/BEAR badge
    try:
        sw = int(d.textlength(symbol, font=fnt_sym)) + 16
    except Exception:
        sw = len(symbol) * 38 + 16
    bx = x + BORDER_W + 20 + sw
    d.rounded_rectangle([bx, y + 22, bx + 120, y + 62], radius=8,
                         fill=(color[0]//5, color[1]//5, color[2]//5))
    d.rounded_rectangle([bx, y + 22, bx + 120, y + 62], radius=8, outline=color, width=2)
    d.text((bx + 8, y + 32), label, font=fnt_badge, fill=color)

    # Confidence bar
    bar_x = x + BORDER_W + 20
    bar_y = y + h - 38
    bar_w = w - BORDER_W - 40
    d.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + 18], radius=6, fill=DIV)
    fill_w = int(bar_w * min(confidence / 100, 1.0))
    if fill_w > 4:
        d.rounded_rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + 18], radius=6, fill=color)
    d.text((bar_x + bar_w, bar_y - 22), f"{confidence:.0f}%", font=fnt_conf, fill=color, anchor="ra")


# ═════════════════════════════════════════════════════════════════════════════
# SLIDE 1 — HOOK FRAME
# Breaking-news alert cards for top movers + regime
# ═════════════════════════════════════════════════════════════════════════════

def _draw_caption_bar(d, W, H, caption_text, fnt_caption, fnt_nano):
    """
    Draw a semi-transparent caption bar at the very bottom of the frame.
    This ensures the key message is readable even with no sound.
    """
    WHITE = (230, 238, 250)
    DIM   = (100, 118, 140)
    BAR_H = 100
    # Dark gradient bar
    d.rectangle([0, H - BAR_H, W, H], fill=(6, 8, 14))
    d.rectangle([0, H - BAR_H, W, H - BAR_H + 2], fill=(56, 189, 248))  # accent top border
    # Caption text
    d.text((W // 2, H - BAR_H + 32), caption_text,
           font=fnt_caption, fill=WHITE, anchor="mm")
    # Tiny sub-label
    d.text((W // 2, H - 22), "marketgenie.ai  |  NOT FINANCIAL ADVICE",
           font=fnt_nano, fill=DIM, anchor="mm")


def _draw_breaking_badge(d, W, symbol, pct, fnt):
    """Red BREAKING banner just below the header for big movers (≥5%)."""
    d.rectangle([0, 132, W, 222], fill=(180, 15, 15))
    sign = "+" if pct >= 0 else ""
    d.text((W // 2, 177),
           f"⚡ BREAKING  {symbol} {sign}{pct:.1f}%  ⚡",
           font=fnt, fill=(255, 255, 255), anchor="mm")


def _generate_hook_frame(data, trigger):
    """Slide 1 — Hook. Gradient bg, glow cards, exact dashboard colors."""
    from PIL import Image, ImageDraw
    W, H = _VIDEO_W, _VIDEO_H
    PAD  = 52
    regime   = data.get("regime", "NEUTRAL")
    score    = data.get("regime_score", 50)
    rc       = _regime_color(regime)
    spy      = data.get("spy_pct", 0.0)
    qqq      = data.get("qqq_pct", 0.0)
    vix      = data.get("vix", 16.5)
    hot      = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    trig_lbl = {"premarket":"PRE-MARKET","midday":"MIDDAY","eod":"CLOSE","afterhours":"AFTER HOURS"}.get(trigger,"LIVE")
    img = _mk_bg()
    d   = ImageDraw.Draw(img)
    fnt_nano = _load_font(28)
    fnt_xs   = _load_font(38)
    fnt_sm   = _load_font(50)
    fnt_md   = _load_font(64, bold=True)
    fnt_lg   = _load_font(84, bold=True)
    fnt_xl   = _load_font(110, bold=True)
    _draw_header(d, W, "MARKET GENIE", trig_lbl, _C["accent"], fnt_md, fnt_sm,
                 timestamp=data.get("timestamp", ""))
    y = 172
    if hot and abs(hot[0].get("pct", 0)) >= 5:
        _draw_breaking_badge(d, W, hot[0]["symbol"], hot[0]["pct"], fnt_sm)
        y = 235
    _draw_glow_card(d, PAD, y, W - PAD, y + 200, radius=16, accent=rc)
    regime_label = {"BULLISH": "BULLS IN CONTROL", "BEARISH": "BEARS IN CONTROL", "NEUTRAL": "CHOPPY TAPE"}.get(regime, regime)
    regime_emoji = {"BULLISH": "\U0001f7e2", "BEARISH": "\U0001f534", "NEUTRAL": "\U0001f7e1"}.get(regime, "")
    d.text((W // 2, y + 76),  f"{regime_emoji} {regime_label}", font=fnt_xl, fill=rc, anchor="mm")
    d.text((W // 2, y + 152), f"A.I. Regime Score: {score} / 100", font=fnt_sm, fill=_C["subtext"], anchor="mm")
    y += 220
    col_w = (W - PAD * 2 - 20) // 3
    for i, (lbl, val, vc) in enumerate([
        ("SPY", f"{spy:+.2f}%", _pct_color(spy)),
        ("QQQ", f"{qqq:+.2f}%", _pct_color(qqq)),
        ("VIX", f"{vix:.1f}",   _C["red"] if vix > 20 else _C["subtext"]),
    ]):
        cx = PAD + i * (col_w + 10)
        _draw_glow_card(d, cx, y, cx + col_w, y + 110, radius=12, accent=vc, fill=_C["card"])
        d.text((cx + col_w//2, y + 33), lbl, font=fnt_xs, fill=_C["subtext"], anchor="mm")
        d.text((cx + col_w//2, y + 80), val, font=fnt_md, fill=vc,           anchor="mm")
    y += 130
    d.text((PAD, y + 10), "TODAY'S TOP MOVERS", font=fnt_xs, fill=_C["amber"])
    y += 55
    n_hot  = min(len(hot), 5)
    card_h = max(120, (H - y - 130) // max(n_hot, 1))
    for tk in hot[:n_hot]:
        tc   = _pct_color(tk["pct"])
        sign = "+" if tk["up"] else ""
        _draw_glow_card(d, 0, y, W, y + card_h - 4, radius=0, accent=tc, fill=_C["card"])
        d.rectangle([0, y, 7, y + card_h - 4], fill=tc)
        mid = y + (card_h - 4) // 2
        d.text((PAD + 12, mid - 20), tk["symbol"],             font=fnt_lg, fill=_C["text_hi"], anchor="lm")
        d.text((PAD + 12, mid + 30), f"${tk['price']:,.2f}",   font=fnt_xs, fill=_C["subtext"], anchor="lm")
        d.text((W - PAD,  mid),      f"{sign}{tk['pct']:.2f}%", font=fnt_lg, fill=tc,           anchor="rm")
        y += card_h
    regime_c = {"BULLISH": "\U0001f7e2", "BEARISH": "\U0001f534", "NEUTRAL": "\U0001f7e1"}.get(regime, "")
    _draw_caption_bar(d, W, H, f"{regime_c} {regime}  Score {score}/100  Follow for free A.I. signals", fnt_xs, fnt_nano)
    return img


def _generate_frame(data: dict, trigger: str):
    """Slide 3 — A.I. Signals. Dashboard cards with confidence bars and glow."""
    from PIL import Image, ImageDraw
    W, H = _VIDEO_W, _VIDEO_H
    PAD  = 52
    regime   = data.get("regime", "NEUTRAL")
    rc       = _regime_color(regime)
    signals  = sorted([s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 60],
                      key=lambda s: s.get("confidence", 0), reverse=True)
    hot      = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    vix_val  = data.get("vix", 16.5)
    nq_val   = data.get("nq_pct", 0.0)
    n_sigs   = len([s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 65])
    pnl_val  = data.get("pnl_today", 0.0)
    trig_lbl = {"premarket":"PRE-MARKET","midday":"MIDDAY","eod":"CLOSE","afterhours":"AFTER HOURS"}.get(trigger,"LIVE")
    img = _mk_bg()
    d   = ImageDraw.Draw(img)
    fnt_nano = _load_font(28)
    fnt_xs   = _load_font(38)
    fnt_sm   = _load_font(50)
    fnt_md   = _load_font(64, bold=True)
    fnt_lg   = _load_font(84, bold=True)
    _draw_header(d, W, "MARKET GENIE", trig_lbl, rc, fnt_md, fnt_sm,
                 timestamp=data.get("timestamp", ""))
    y = 172
    d.text((PAD, y), "A.I. SIGNALS", font=fnt_sm, fill=_C["amber"])
    d.text((W - PAD, y), f"{n_sigs} high-confidence", font=fnt_xs, fill=_C["subtext"], anchor="ra")
    y += 62
    n_show = min(len(signals), 4)
    card_h = max(180, min(290, (H - y - 380) // max(n_show, 1)))
    for sig in signals[:n_show]:
        bull  = "bull" in sig.get("direction", "bull").lower()
        sc    = _C["green"] if bull else _C["red"]
        label = "BULL" if bull else "BEAR"
        conf  = sig.get("confidence", 70)
        _draw_glow_card(d, 0, y, W, y + card_h - 4, radius=0, accent=sc, fill=_C["card"])
        d.rectangle([0, y, 8, y + card_h - 4], fill=sc)
        mid = y + (card_h - 4) // 2
        d.text((PAD + 14, mid - 24), sig["symbol"], font=fnt_lg, fill=_C["text_hi"], anchor="lm")
        try:  sx = int(d.textlength(sig["symbol"], font=fnt_lg)) + PAD + 26
        except: sx = PAD + 240
        _draw_badge(d, sx, mid - 34, label, sc, fnt_xs)
        d.text((W - PAD, mid - 26), f"{conf:.0f}%",  font=fnt_md, fill=sc,           anchor="ra")
        d.text((W - PAD, mid + 14), "confidence",    font=fnt_xs, fill=_C["subtext"], anchor="ra")
        _draw_conf_bar(d, PAD + 14, y + card_h - 36, W - PAD * 2 - 28, 16, conf, sc)
        y += card_h
    y += 12
    stat_rows = []
    if abs(pnl_val) >= 1:
        sign_p = "+" if pnl_val >= 0 else ""
        stat_rows.append(("TODAY'S P&L", f"{sign_p}${abs(pnl_val):,.0f}", _pct_color(pnl_val)))
    stat_rows.append(("ACTIVE SIGNALS", f"{n_sigs} setups", _C["amber"]))
    if abs(nq_val) >= 0.05:
        stat_rows.append(("NQ FUTURES", f"{nq_val:+.2f}%", _pct_color(nq_val)))
    vix_c = _C["red"] if vix_val > 22 else (_C["amber"] if vix_val > 18 else _C["green"])
    vix_note = "HIGH" if vix_val > 22 else "CALM"
    stat_rows.append(("VIX", f"{vix_val:.1f}  {vix_note}", vix_c))
    stat_rows.append(("SCANNER", "200+ stocks  real-time", _C["accent"]))
    row_h = 82
    for lbl, val, col in stat_rows[:4]:
        _draw_glow_card(d, 0, y, W, y + row_h, radius=0, accent=col, fill=_C["surface"])
        d.rectangle([0, y, 6, y + row_h], fill=col)
        d.text((PAD + 14, y + 22), lbl, font=fnt_xs, fill=_C["subtext"], anchor="lm")
        d.text((PAD + 14, y + 60), val, font=fnt_sm,  fill=col,           anchor="lm")
        y += row_h + 3
    top_sig_text = ""
    if signals:
        s0   = signals[0]
        bull = "bull" in s0.get("direction", "").lower()
        emoji = "\U0001f7e2" if bull else "\U0001f534"
        top_sig_text = f"{emoji} {s0['symbol']} {s0.get('confidence', 70):.0f}%  "
    _draw_caption_bar(d, W, H, f"{top_sig_text}Follow for free A.I. signals", fnt_xs, fnt_nano)
    return img


def _generate_cta_frame(data, trigger):
    """Legacy stub — 6-slide path uses _generate_cta_slide."""
    return _generate_frame(data, trigger)


def _generate_context_frame(data, trigger):
    """Slide 2 — Market Overview. Dashboard 2x2 grid + top signals."""
    from PIL import Image, ImageDraw
    W, H = _VIDEO_W, _VIDEO_H
    PAD  = 52
    regime   = data.get("regime", "NEUTRAL")
    rc       = _regime_color(regime)
    score    = data.get("regime_score", 50)
    hot      = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    signals  = sorted([s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 60],
                      key=lambda s: s.get("confidence", 0), reverse=True)
    vix      = data.get("vix", 16.5)
    spy      = data.get("spy_pct", 0.0)
    qqq      = data.get("qqq_pct", 0.0)
    nq       = data.get("nq_pct", 0.0)
    trig_lbl = {"premarket":"PRE-MARKET","midday":"MIDDAY","eod":"CLOSE","afterhours":"AFTER HOURS"}.get(trigger,"LIVE")
    img = _mk_bg()
    d   = ImageDraw.Draw(img)
    fnt_nano = _load_font(28)
    fnt_xs   = _load_font(38)
    fnt_sm   = _load_font(50)
    fnt_md   = _load_font(64, bold=True)
    fnt_lg   = _load_font(84, bold=True)
    fnt_xl   = _load_font(110, bold=True)
    _draw_header(d, W, "MARKET GENIE", trig_lbl, _C["accent"], fnt_md, fnt_sm,
                 timestamp=data.get("timestamp", ""))
    y = 172
    # Regime banner
    _draw_glow_card(d, PAD, y, W - PAD, y + 120, radius=14, accent=rc)
    regime_emoji = {"BULLISH": "\U0001f7e2", "BEARISH": "\U0001f534", "NEUTRAL": "\U0001f7e1"}.get(regime, "")
    d.text((W // 2, y + 60), f"{regime_emoji} {regime}  {score}/100", font=fnt_xl, fill=rc, anchor="mm")
    y += 140
    # 2x2 index grid
    nq_lbl = "NQ FUT"
    nq_val_str = f"{nq:+.2f}%" if abs(nq) >= 0.05 else "flat"
    nq_col = _pct_color(nq) if abs(nq) >= 0.05 else _C["subtext"]
    pairs = [
        ("SPY",    f"{spy:+.2f}%", _pct_color(spy)),
        ("QQQ",    f"{qqq:+.2f}%", _pct_color(qqq)),
        (nq_lbl,   nq_val_str,     nq_col),
        ("VIX",    f"{vix:.1f}",   _C["red"] if vix > 20 else _C["green"]),
    ]
    cell_w = (W - PAD * 2 - 12) // 2
    cell_h = 140
    for i, (lbl, val, vc) in enumerate(pairs):
        cx = PAD + (i % 2) * (cell_w + 12)
        cy = y + (i // 2) * (cell_h + 10)
        _draw_glow_card(d, cx, cy, cx + cell_w, cy + cell_h, radius=14, accent=vc, fill=_C["card"])
        d.text((cx + cell_w//2, cy + 38),  lbl, font=fnt_xs, fill=_C["subtext"], anchor="mm")
        d.text((cx + cell_w//2, cy + 100), val, font=fnt_lg, fill=vc,            anchor="mm")
    y += 2 * (cell_h + 10) + 16
    # Top signals
    d.text((PAD, y), "TOP A.I. SIGNALS", font=fnt_sm, fill=_C["amber"])
    y += 58
    sig_card_h = max(130, (H - y - 120) // max(min(len(signals), 3), 1))
    for sig in signals[:3]:
        bull  = "bull" in sig.get("direction", "bull").lower()
        sc    = _C["green"] if bull else _C["red"]
        label = "BULL" if bull else "BEAR"
        conf  = sig.get("confidence", 70)
        pd    = next((t for t in hot if t["symbol"] == sig["symbol"]), None)
        price = pd["price"] if pd else 0
        pct   = pd["pct"]   if pd else 0
        _draw_glow_card(d, 0, y, W, y + sig_card_h - 4, radius=0, accent=sc, fill=_C["card"])
        d.rectangle([0, y, 7, y + sig_card_h - 4], fill=sc)
        mid = y + (sig_card_h - 4) // 2
        d.text((PAD + 14, mid - 20), sig["symbol"], font=fnt_lg, fill=_C["text_hi"], anchor="lm")
        _draw_badge(d, PAD + 14, mid + 16, label, sc, fnt_xs)
        d.text((W - PAD, mid - 20), f"{conf:.0f}%", font=fnt_md, fill=sc,           anchor="ra")
        if price > 0:
            sign_p = "+" if pct >= 0 else ""
            d.text((W - PAD, mid + 18), f"${price:,.2f}  {sign_p}{pct:.2f}%", font=fnt_xs, fill=_C["subtext"], anchor="ra")
        y += sig_card_h
    regime_c = {"BULLISH": "\U0001f7e2", "BEARISH": "\U0001f534", "NEUTRAL": "\U0001f7e1"}.get(regime, "")
    _draw_caption_bar(d, W, H, f"{regime_c} {regime} {score}/100  Drop green or red below", fnt_xs, fnt_nano)
    return img



def _generate_trade_setup_slide(data, trigger):
    """
    Slide 4 — THE TAKE-AWAY SLIDE.
    Shows the #1 AI signal as a full trade plan: entry zone, stop, target, R:R.
    Viewers pause and screenshot this. It's the reason they follow.
    """
    from PIL import Image, ImageDraw
    W, H  = _VIDEO_W, _VIDEO_H
    BG    = (4, 6, 12)
    WHITE = (230, 238, 250)
    DIM   = (100, 118, 140)
    AMBER = (220, 155, 40)
    GREEN = (74, 222, 128)
    RED   = (248, 113, 113)
    PANEL = (12, 18, 30)
    ACCENT= (56, 189, 248)
    GOLD  = (255, 200, 50)

    signals = sorted(
        [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 65],
        key=lambda s: s.get("confidence", 0), reverse=True
    )
    hot = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)

    img = Image.new("RGB", (W, H), BG)
    d   = ImageDraw.Draw(img)

    fnt_nano = _load_font(28)
    fnt_xs   = _load_font(38)
    fnt_sm   = _load_font(50)
    fnt_md   = _load_font(64, bold=True)
    fnt_lg   = _load_font(88, bold=True)
    fnt_xl   = _load_font(110, bold=True)
    fnt_hero = _load_font(140, bold=True)

    PAD = 48

    # Header
    d.rectangle([0, 0, W, 120], fill=(10, 14, 24))
    d.text((PAD, 60), "MARKET GENIE", font=fnt_md, fill=WHITE, anchor="lm")
    d.text((W - PAD, 60), "TRADE SETUP", font=fnt_md, fill=GOLD, anchor="rm")
    d.rectangle([0, 118, W, 122], fill=GOLD)

    y = 145

    if not signals:
        # No signal — show "waiting for setup" placeholder
        d.text((W // 2, H // 2 - 60), "⏳ SCANNING...", font=fnt_xl, fill=DIM, anchor="mm")
        d.text((W // 2, H // 2 + 40), "No high-confidence setup yet", font=fnt_sm, fill=DIM, anchor="mm")
        d.text((W // 2, H // 2 + 110), "Check back at market open", font=fnt_xs, fill=DIM, anchor="mm")
        _draw_caption_bar(d, W, H, "📸 Screenshot the trade setup when it appears!", fnt_xs, fnt_nano)
        return img

    sig   = signals[0]
    sym   = sig["symbol"]
    bull  = "bull" in sig.get("direction", "bull").lower()
    conf  = sig.get("confidence", 70)
    sc    = GREEN if bull else RED
    label = "BULL" if bull else "BEAR"

    # Get price from hot_tickers or use 0
    price_data = next((t for t in hot if t["symbol"] == sym), None)
    price = price_data["price"] if price_data else 0

    # Calculate levels
    stop_pct   = float(os.getenv("ALPACA_STOP_PCT",   "0.0075"))
    target_pct = float(os.getenv("ALPACA_TARGET_PCT", "0.015"))
    strong_pct = float(os.getenv("ALPACA_STRONG_TARGET_PCT", "0.020"))
    use_strong = conf >= 88
    actual_target_pct = strong_pct if use_strong else target_pct

    if price > 0:
        if bull:
            stop_price   = price * (1 - stop_pct)
            target_price = price * (1 + actual_target_pct)
        else:
            stop_price   = price * (1 + stop_pct)
            target_price = price * (1 - actual_target_pct)
        rr = actual_target_pct / stop_pct
    else:
        stop_price = target_price = 0
        rr = actual_target_pct / stop_pct

    # ── Big symbol + direction pill ───────────────────────────────────────────
    d.rectangle([0, y, W, y + 200], fill=PANEL)
    d.rectangle([0, y, 8, y + 200], fill=sc)
    mid_hero = y + 100
    d.text((PAD + 24, mid_hero), sym, font=fnt_hero, fill=WHITE, anchor="lm")
    # Direction badge
    try: sym_w = int(d.textlength(sym, font=fnt_hero)) + PAD + 36
    except: sym_w = PAD + 300
    pill_w = 180
    d.rounded_rectangle([sym_w, mid_hero - 40, sym_w + pill_w, mid_hero + 14],
                         radius=10, fill=(sc[0]//4, sc[1]//4, sc[2]//4), outline=sc, width=3)
    d.text((sym_w + pill_w // 2, mid_hero - 13), label, font=fnt_md, fill=sc, anchor="mm")
    # Confidence on right
    d.text((W - PAD, mid_hero - 30), f"{conf:.0f}%", font=fnt_xl, fill=sc, anchor="rm")
    d.text((W - PAD, mid_hero + 30), "A.I. confidence", font=fnt_nano, fill=DIM, anchor="rm")
    y += 210

    # ── Price levels ─────────────────────────────────────────────────────────
    d.text((PAD, y + 8), "📋  TRADE PLAN", font=fnt_sm, fill=AMBER)
    y += 68

    def _level_row(label_txt, value_txt, color, bg, y_pos):
        row_h = 105
        d.rectangle([0, y_pos, W, y_pos + row_h], fill=bg)
        d.rectangle([0, y_pos, 6, y_pos + row_h], fill=color)
        d.text((PAD + 16, y_pos + row_h // 2 - 14), label_txt, font=fnt_xs, fill=DIM, anchor="lm")
        d.text((PAD + 16, y_pos + row_h // 2 + 28), value_txt, font=fnt_md, fill=color, anchor="lm")
        return y_pos + row_h + 4

    if price > 0:
        y = _level_row("ENTRY ZONE",
                        f"${price:,.2f}  (current price)",
                        ACCENT, PANEL, y)
        y = _level_row("STOP LOSS",
                        f"${stop_price:,.2f}  (-{stop_pct*100:.2f}%)",
                        RED, (18, 10, 10), y)
        y = _level_row("PRICE TARGET",
                        f"${target_price:,.2f}  (+{actual_target_pct*100:.2f}%)" + (" 🔥" if use_strong else ""),
                        GREEN, (8, 20, 12), y)

        # R:R display
        rr_h = 90
        d.rectangle([0, y, W, y + rr_h], fill=(16, 22, 36))
        d.rectangle([0, y, W, y + 3], fill=GOLD)
        d.text((PAD + 16, y + rr_h // 2), "RISK / REWARD", font=fnt_xs, fill=DIM, anchor="lm")
        d.text((W - PAD, y + rr_h // 2), f"{rr:.1f} : 1", font=fnt_lg, fill=GOLD, anchor="rm")
        y += rr_h + 8
    else:
        d.text((W // 2, y + 60), "Price data loading...", font=fnt_sm, fill=DIM, anchor="mm")
        y += 140

    # ── Quality badges ────────────────────────────────────────────────────────
    badges = []
    if sig.get("both_agree") == 1: badges.append(("✓ Both Models Agree", GREEN))
    if sig.get("streak", 0) >= 2:  badges.append((f"⚡ Streak {sig['streak']}x", AMBER))
    if conf >= 90:                  badges.append(("🔥 High Conviction", sc))

    bx = PAD
    for badge_txt, badge_col in badges[:3]:
        try: bw = int(d.textlength(badge_txt, font=fnt_nano)) + 28
        except: bw = 200
        d.rounded_rectangle([bx, y + 8, bx + bw, y + 54], radius=8,
                             fill=(badge_col[0]//5, badge_col[1]//5, badge_col[2]//5),
                             outline=badge_col, width=2)
        d.text((bx + bw // 2, y + 31), badge_txt, font=fnt_nano, fill=badge_col, anchor="mm")
        bx += bw + 12
    y += 68

    # Caption
    if price > 0:
        caption = f"📸 Screenshot this! {sym} {label} — Stop ${stop_price:,.2f} → Target ${target_price:,.2f}"
    else:
        caption = f"📸 Screenshot this! {sym} {label} setup — {conf:.0f}% A.I. confidence"
    _draw_caption_bar(d, W, H, caption, fnt_xs, fnt_nano)
    return img


def _generate_watchlist_slide(data, trigger):
    """
    Slide 5 — TODAY'S WATCHLIST.
    3-5 stocks with key price levels. Viewers write these down / screenshot.
    Shows: symbol, direction bias, current price, key level to watch.
    """
    from PIL import Image, ImageDraw
    W, H  = _VIDEO_W, _VIDEO_H
    BG    = (4, 6, 12)
    WHITE = (230, 238, 250)
    DIM   = (100, 118, 140)
    AMBER = (220, 155, 40)
    GREEN = (74, 222, 128)
    RED   = (248, 113, 113)
    PANEL = (12, 18, 30)
    ALT   = (8, 12, 22)
    GOLD  = (255, 200, 50)
    ACCENT= (56, 189, 248)

    signals = sorted(
        [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 60],
        key=lambda s: s.get("confidence", 0), reverse=True
    )
    hot = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    stop_pct   = float(os.getenv("ALPACA_STOP_PCT",   "0.0075"))
    target_pct = float(os.getenv("ALPACA_TARGET_PCT", "0.015"))

    img = Image.new("RGB", (W, H), BG)
    d   = ImageDraw.Draw(img)

    fnt_nano = _load_font(28)
    fnt_xs   = _load_font(38)
    fnt_sm   = _load_font(50)
    fnt_md   = _load_font(64, bold=True)
    fnt_lg   = _load_font(80, bold=True)

    PAD = 48

    # Header
    d.rectangle([0, 0, W, 120], fill=(10, 14, 24))
    d.text((PAD, 60), "MARKET GENIE", font=fnt_md, fill=WHITE, anchor="lm")
    d.text((W - PAD, 60), "WATCHLIST", font=fnt_md, fill=GOLD, anchor="rm")
    d.rectangle([0, 118, W, 122], fill=GOLD)
    d.text((PAD, 152), f"📋  Stocks to watch {datetime.now().strftime('%b %d')}  —  key levels inside", font=fnt_xs, fill=DIM)
    y = 200

    # Build watchlist from signals (preferred) + hot movers
    seen = set()
    watch_items = []
    for sig in signals[:5]:
        sym   = sig["symbol"]
        bull  = "bull" in sig.get("direction", "bull").lower()
        conf  = sig.get("confidence", 70)
        pd    = next((t for t in hot if t["symbol"] == sym), None)
        price = pd["price"] if pd else 0
        watch_items.append({
            "symbol": sym, "bull": bull, "conf": conf, "price": price,
            "stop":   price * (1 - stop_pct) if price and bull else price * (1 + stop_pct) if price else 0,
            "target": price * (1 + target_pct) if price and bull else price * (1 - target_pct) if price else 0,
            "pct":    pd["pct"] if pd else 0,
            "source": "signal",
        })
        seen.add(sym)

    # Fill remaining slots with top movers not already in signals
    for tk in hot:
        if tk["symbol"] not in seen and len(watch_items) < 5:
            watch_items.append({
                "symbol": tk["symbol"], "bull": tk["up"], "conf": 0,
                "price": tk["price"], "stop": 0, "target": 0,
                "pct": tk["pct"], "source": "mover",
            })
            seen.add(tk["symbol"])

    if not watch_items:
        d.text((W // 2, H // 2), "Loading watchlist...", font=fnt_sm, fill=DIM, anchor="mm")
        _draw_caption_bar(d, W, H, "🔔 Follow for daily watchlist with key levels", fnt_xs, fnt_nano)
        return img

    card_h = min(250, max(180, (H - y - 110) // min(len(watch_items), 5)))

    for item in watch_items[:5]:
        sc     = GREEN if item["bull"] else RED
        bg     = PANEL if watch_items.index(item) % 2 == 0 else ALT
        sym    = item["symbol"]
        price  = item["price"]
        conf   = item["conf"]
        target = item["target"]
        stop   = item["stop"]

        d.rectangle([0, y, W, y + card_h], fill=bg)
        d.rectangle([0, y, 8, y + card_h], fill=sc)
        mid = y + card_h // 2

        # Symbol
        d.text((PAD + 20, mid - 28), sym, font=fnt_lg, fill=WHITE, anchor="lm")

        # Direction label
        dir_lbl = "WATCH LONG 🟢" if item["bull"] else "WATCH SHORT 🔴"
        if item["source"] == "mover":
            dir_lbl = f"{'UP' if item['bull'] else 'DOWN'} {abs(item['pct']):.1f}% {'📈' if item['bull'] else '📉'}"
        d.text((PAD + 20, mid + 22), dir_lbl, font=fnt_xs, fill=sc, anchor="lm")

        # Price + levels on right
        if price > 0:
            d.text((W - PAD, mid - 42), f"${price:,.2f}", font=fnt_md, fill=WHITE, anchor="rm")
            if target > 0 and conf > 0:
                tgt_sign = "+" if item["bull"] else "-"
                d.text((W - PAD, mid + 8), f"Target: ${target:,.2f}", font=fnt_xs, fill=GREEN if item["bull"] else RED, anchor="rm")
                d.text((W - PAD, mid + 46), f"Stop:   ${stop:,.2f}", font=fnt_xs, fill=RED, anchor="rm")
            elif item["pct"] != 0:
                sign = "+" if item["pct"] > 0 else ""
                d.text((W - PAD, mid + 8), f"{sign}{item['pct']:.2f}% today", font=fnt_xs, fill=sc, anchor="rm")

        # Confidence badge if signal
        if conf > 0:
            d.text((PAD + 20, y + 12), f"A.I. {conf:.0f}% confident", font=fnt_nano, fill=DIM, anchor="lm")

        d.rectangle([0, y + card_h - 1, W, y + card_h], fill=(20, 28, 44))
        y += card_h

    _draw_caption_bar(d, W, H, "📸 Screenshot this watchlist! Entry + stop + target included 👇", fnt_xs, fnt_nano)
    return img


def _generate_cta_slide(data, trigger):
    """
    Slide 6 — CTA / P&L Close.
    Clean close: P&L summary, follow/comment ask, what's coming next.
    """
    from PIL import Image, ImageDraw
    W, H  = _VIDEO_W, _VIDEO_H
    BG    = (4, 6, 12)
    WHITE = (230, 238, 250)
    DIM   = (100, 118, 140)
    AMBER = (220, 155, 40)
    GREEN = (74, 222, 128)
    ACCENT= (56, 189, 248)
    RED   = (248, 113, 113)
    PANEL = (12, 18, 30)
    GOLD  = (255, 200, 50)

    regime = data.get("regime", "NEUTRAL")
    score  = data.get("regime_score", 50)
    pnl    = data.get("pnl_today", 0.0)
    equity = data.get("equity", 100_000.0)
    rc     = {"BULLISH": GREEN, "BEARISH": RED, "NEUTRAL": AMBER}.get(regime, AMBER)
    signals = [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 65]

    next_post = {
        "premarket": "Midday update → 12:00 PM ET",
        "midday":    "End of day wrap → 4:15 PM ET",
        "eod":       "After-hours report → 5:30 PM ET",
        "afterhours":"Pre-market brief → 9:35 AM ET",
    }.get(trigger, "Next update coming soon")

    img = Image.new("RGB", (W, H), BG)
    d   = ImageDraw.Draw(img)

    fnt_nano = _load_font(30)
    fnt_xs   = _load_font(40)
    fnt_sm   = _load_font(52)
    fnt_md   = _load_font(68, bold=True)
    fnt_lg   = _load_font(90, bold=True)
    fnt_xl   = _load_font(120, bold=True)
    fnt_hero = _load_font(150, bold=True)

    PAD = 48

    # Header
    d.rectangle([0, 0, W, 120], fill=(10, 14, 24))
    d.text((W // 2, 60), "MARKET GENIE", font=fnt_md, fill=WHITE, anchor="mm")
    d.rectangle([0, 118, W, 122], fill=rc)

    y = 150

    # P&L block — show if there's real P&L, otherwise show what the AI did today
    has_pnl = abs(pnl) >= 1 and trigger not in ("premarket",)
    n_sigs  = len(signals)
    hot_cta = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    vix_cta = data.get("vix", 16.5)

    pnl_h = 240
    d.rectangle([0, y, W, y + pnl_h], fill=PANEL)
    d.rectangle([0, y, 8, y + pnl_h], fill=rc)
    if has_pnl:
        pc   = GREEN if pnl >= 0 else RED
        sign = "+" if pnl >= 0 else ""
        word = "PROFIT" if pnl >= 0 else "LOSS"
        d.text((W // 2, y + 55), f"TODAY'S A.I. {word}", font=fnt_sm, fill=DIM, anchor="mm")
        d.text((W // 2, y + 160), f"{sign}${abs(pnl):,.0f}", font=fnt_hero, fill=pc, anchor="mm")
    else:
        # No P&L — show what the AI scanned/found today instead
        d.text((W // 2, y + 45), "A.I. SCANNED TODAY", font=fnt_sm, fill=DIM, anchor="mm")
        d.text((W // 2, y + 130), "200+ stocks", font=fnt_xl, fill=ACCENT, anchor="mm")
        d.text((W // 2, y + 200), f"Found {n_sigs} high-confidence setup{'s' if n_sigs != 1 else ''}", font=fnt_xs, fill=AMBER, anchor="mm")
    y += pnl_h + 16

    # Regime summary
    d.rectangle([0, y, W, y + 100], fill=(16, 22, 36))
    regime_c = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(regime, "⚪")
    d.text((W // 2, y + 50), f"Regime: {regime_c} {regime}  ({score}/100)", font=fnt_sm, fill=rc, anchor="mm")
    y += 115

    # Signal count
    if signals:
        d.rectangle([0, y, W, y + 90], fill=PANEL)
        d.text((W // 2, y + 45), f"🤖  {len(signals)} A.I. signal{'s' if len(signals) != 1 else ''} active today", font=fnt_sm, fill=WHITE, anchor="mm")
        y += 105

    # Follow / comment block
    d.rectangle([0, y, W, y + 140], fill=(10, 14, 24))
    d.rectangle([0, y, W, y + 3], fill=GOLD)
    d.text((W // 2, y + 45), "🔔  FOLLOW for daily A.I. signals", font=fnt_sm, fill=GOLD, anchor="mm")
    d.text((W // 2, y + 100), "💬  Drop 🟢 BULL or 🔴 BEAR below!", font=fnt_xs, fill=WHITE, anchor="mm")
    y += 155

    # Next post
    d.rectangle([0, y, W, y + 80], fill=PANEL)
    d.text((W // 2, y + 40), f"⏰  {next_post}", font=fnt_xs, fill=DIM, anchor="mm")

    _draw_caption_bar(d, W, H, "Tap follow — free A.I. signals + trade setups daily 🔔", fnt_xs, fnt_nano)
    return img


def _build_voiceover_script(data, trigger):
    """
    Full 55-58 second script — educational, actionable, something the viewer takes away.
    Covers: hook → regime → macro → movers → AI signals → trade setup → watchlist → CTA.
    """
    regime  = data.get("regime", "NEUTRAL")
    score   = data.get("regime_score", 50)
    pnl     = data.get("pnl_today", 0.0)
    hot     = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    vix     = data.get("vix", 16.5)
    nq      = data.get("nq_pct", 0.0)
    spy     = data.get("spy_pct", 0.0)
    signals = sorted(
        [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 65],
        key=lambda s: s.get("confidence", 0), reverse=True
    )
    stop_pct   = float(os.getenv("ALPACA_STOP_PCT",   "0.0075"))
    target_pct = float(os.getenv("ALPACA_TARGET_PCT", "0.015"))
    strong_pct = float(os.getenv("ALPACA_STRONG_TARGET_PCT", "0.020"))

    date_str = datetime.now().strftime("%B %d")
    parts = []

    # ── 1. HOOK ───────────────────────────────────────────────────────────────
    if trigger == "premarket":
        if hot and abs(hot[0].get("pct", 0)) >= 2:
            h0   = hot[0]
            verb = "gapping UP" if h0["up"] else "gapping DOWN"
            parts.append(f"Stop scrolling. {h0['symbol']} is {verb} {abs(h0['pct']):.1f} percent pre-market — and my A.I. already has a trade plan ready.")
        elif regime == "BULLISH":
            parts.append(f"Good morning — the tape is setting up BULLISH on {date_str}. Here's everything you need before the bell.")
        elif regime == "BEARISH":
            parts.append(f"Warning — the A.I. is reading BEARISH this morning. Here's what to watch and how to protect yourself.")
        else:
            parts.append(f"Pre-market brief for {date_str}. Mixed signals — here's what the A.I. is seeing and the one setup worth watching.")
    elif trigger == "midday":
        if hot and abs(hot[0].get("pct", 0)) >= 4:
            h0 = hot[0]
            parts.append(f"Midday update — {h0['symbol']} is {'ripping' if h0['up'] else 'dumping'} {abs(h0['pct']):.1f} percent. Here's what the A.I. says to do about it.")
        else:
            parts.append(f"Halfway through {date_str} — here's your midday market check, the A.I. signals, and the one setup I'm watching for the second half.")
    elif trigger in ("eod", "afterhours"):
        if hot and abs(hot[0].get("pct", 0)) >= 4:
            h0 = hot[0]
            verb = "exploded" if h0["up"] else "collapsed"
            parts.append(f"{h0['symbol']} {verb} {abs(h0['pct']):.1f} percent today — here's the full breakdown and what the A.I. is watching tomorrow.")
        elif abs(pnl) >= 50:
            word = "banked" if pnl > 0 else "down"
            parts.append(f"The A.I. auto-trader {word} {'$' + f'{abs(pnl):,.0f}' if pnl > 0 else '$' + f'{abs(pnl):,.0f}'} today. Here's the full recap and tomorrow's setup.")
        else:
            parts.append(f"End of day wrap for {date_str}. Here's what moved, what the A.I. is signalling, and your watchlist for tomorrow.")
    else:
        parts.append(f"Market Genie A.I. update for {date_str}. Here's the full breakdown.")

    # ── 2. REGIME + MACRO ────────────────────────────────────────────────────
    if regime == "BULLISH":
        parts.append(f"Market regime: BULLISH at {score} out of 100. The A.I. is seeing broad strength — bulls have the edge right now.")
    elif regime == "BEARISH":
        parts.append(f"Market regime: BEARISH at {score} out of 100. The tape is weak — the A.I. is in defense mode and favoring short setups.")
    else:
        parts.append(f"Market regime: NEUTRAL at {score} out of 100. No clean directional edge — the A.I. is being selective.")

    if abs(nq) >= 0.3:
        nq_word = "up" if nq >= 0 else "down"
        parts.append(f"NASDAQ {'futures ' if trigger == 'premarket' else ''}{nq_word} {abs(nq):.1f} percent. {'Tech is leading.' if nq > 0 else 'Tech is under pressure.'}")

    if vix > 25:
        parts.append(f"VIX at {vix:.0f} — that's elevated fear. Size down, widen stops, be patient.")
    elif vix > 20:
        parts.append(f"VIX at {vix:.0f} — a little nervous out there. Keep discipline.")
    else:
        parts.append(f"VIX at {vix:.1f} — volatility is calm. Good conditions for clean entries.")

    # ── 3. TOP MOVERS ────────────────────────────────────────────────────────
    if len(hot) >= 2:
        h0, h1 = hot[0], hot[1]
        parts.append(
            f"Top movers: {h0['symbol']} {'up' if h0['up'] else 'down'} {abs(h0['pct']):.1f} percent, "
            f"and {h1['symbol']} {'up' if h1['up'] else 'down'} {abs(h1['pct']):.1f} percent."
        )
    elif hot:
        h0 = hot[0]
        parts.append(f"Biggest mover: {h0['symbol']}, {'up' if h0['up'] else 'down'} {abs(h0['pct']):.1f} percent.")

    # ── 4. AI SIGNALS + TRADE SETUP ──────────────────────────────────────────
    if signals:
        sig   = signals[0]
        bull  = "bull" in sig.get("direction", "bull").lower()
        conf  = sig.get("confidence", 70)
        sym   = sig["symbol"]
        price_data = next((t for t in hot if t["symbol"] == sym), None)
        price = price_data["price"] if price_data else 0
        use_strong = conf >= 88
        actual_target = strong_pct if use_strong else target_pct

        if conf >= 90:
            confidence_phrase = f"The A.I. is extremely high conviction on this one — {conf:.0f} percent confidence, both models in full agreement."
        elif conf >= 80:
            confidence_phrase = f"Strong signal — {conf:.0f} percent confidence. Both the Kronos neural model and TFM are aligned."
        else:
            confidence_phrase = f"Decent setup — {conf:.0f} percent A.I. confidence."

        direction_phrase = "BULLISH" if bull else "BEARISH"
        parts.append(f"Number one A.I. signal: {direction_phrase} on {sym}. {confidence_phrase}")

        if price > 0:
            if bull:
                stop_p   = price * (1 - stop_pct)
                target_p = price * (1 + actual_target)
                parts.append(
                    f"The trade plan: entry around ${price:,.2f}, stop at ${stop_p:,.2f}, "
                    f"target ${target_p:,.2f}. That's a {actual_target/stop_pct:.1f} to 1 risk reward. Screenshot the next slide."
                )
            else:
                stop_p   = price * (1 + stop_pct)
                target_p = price * (1 - actual_target)
                parts.append(
                    f"Short setup at ${price:,.2f}, stop ${stop_p:,.2f}, target ${target_p:,.2f}. "
                    f"{actual_target/stop_pct:.1f} to 1 risk reward. Screenshot the next slide."
                )
        else:
            parts.append(f"Scroll to the trade setup slide and screenshot it — entry, stop, and target are all there.")

        if len(signals) >= 2:
            sig2  = signals[1]
            bull2 = "bull" in sig2.get("direction", "bull").lower()
            parts.append(f"Also watching {sig2['symbol']} — {'bullish' if bull2 else 'bearish'} at {sig2.get('confidence', 70):.0f} percent.")

        if len(signals) >= 3:
            sig3  = signals[2]
            bull3 = "bull" in sig3.get("direction", "bull").lower()
            parts.append(f"And {sig3['symbol']} {'long' if bull3 else 'short'} setup at {sig3.get('confidence', 70):.0f} percent confidence.")
    else:
        parts.append("No high-confidence setups right now — the A.I. is waiting for a cleaner entry. Patience is a position.")

    # ── 5. WATCHLIST TEASE ────────────────────────────────────────────────────
    if signals or hot:
        watch_names = [s["symbol"] for s in signals[:3]] or [t["symbol"] for t in hot[:3]]
        if len(watch_names) >= 3:
            parts.append(f"Watchlist for today: {watch_names[0]}, {watch_names[1]}, and {watch_names[2]}. Key levels are on the next slide — screenshot it.")
        elif len(watch_names) >= 1:
            parts.append(f"Key name on the watchlist today: {watch_names[0]}. Level breakdown is on the next slide.")

    # ── 6. REAL-TIME CONTEXT + CTA ────────────────────────────────────────────
    # Add a real-time data point viewers can verify themselves
    n_sigs = len(signals)
    if n_sigs > 0:
        parts.append(f"The A.I. scanned over 200 stocks in real-time today and found {n_sigs} high-confidence setup{'s' if n_sigs != 1 else ''}.")
    else:
        parts.append("The A.I. scanned over 200 stocks in real-time today. No high-confidence setups yet — it's waiting for the right moment.")

    if trigger not in ("premarket",) and abs(pnl) >= 1:
        word = "up" if pnl > 0 else "down"
        parts.append(f"Paper trading result: {word} ${abs(pnl):,.0f} today. Completely automated.")

    parts.append(
        "Follow Market Genie — free A.I. signals, trade setups with entry stop and target, "
        "and a live watchlist every single trading day. "
        "Drop a green emoji if you're bullish, red if you're bearish. See you tomorrow."
    )

    return "  ".join(parts)


def _generate_voiceover(data, trigger):
    """
    Generate a spoken voiceover MP3.
    Tries edge-tts first (neural, human-sounding, free).
    Falls back to gTTS if edge-tts is unavailable.
    Returns path to MP3, or None on failure.
    """
    script = _build_voiceover_script(data, trigger)
    print(f"[YouTube] 🎙️  Voiceover script: {script[:160]}...")

    # ── Primary: edge-tts (Microsoft neural voices — sounds human) ───────────
    # Voice options: en-US-GuyNeural (confident male), en-US-JennyNeural (clear female),
    #                en-US-AriaNeural (expressive female), en-US-DavisNeural (deep male)
    _EDGE_VOICE = os.getenv("YT_VOICE", "en-US-GuyNeural")
    try:
        import asyncio, edge_tts
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.close()

        async def _run_edge():
            communicate = edge_tts.Communicate(script, _EDGE_VOICE, rate="+8%", volume="+10%")
            await communicate.save(tmp.name)

        # Run async in a fresh event loop (works inside threads)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("closed")
            loop.run_until_complete(_run_edge())
        except RuntimeError:
            asyncio.run(_run_edge())

        print(f"[YouTube] ✅ edge-tts voiceover saved ({_EDGE_VOICE}): {tmp.name}")
        return tmp.name
    except ImportError:
        print("[YouTube] ⚠️  edge-tts not installed — falling back to gTTS")
        print("[YouTube]    Install: pip install edge-tts")
    except Exception as e:
        print(f"[YouTube] ⚠️  edge-tts failed ({e}) — falling back to gTTS")

    # ── Fallback: gTTS ────────────────────────────────────────────────────────
    try:
        from gtts import gTTS
        tts = gTTS(text=script, lang="en", slow=False)
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tts.save(tmp.name)
        tmp.close()
        print(f"[YouTube] ✅ gTTS voiceover saved: {tmp.name}")
        return tmp.name
    except ImportError:
        print("[YouTube] ❌ Neither edge-tts nor gTTS installed — skipping voiceover")
        return None
    except Exception as e:
        print(f"[YouTube] ❌ gTTS error: {e}")
        return None


# ═════════════════════════════════════════════════════════════════════════════
# VIDEO CREATION — multi-slide with xfade + optional audio
# ═════════════════════════════════════════════════════════════════════════════
def _get_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _create_short(frame, output_path, hook_frame=None, cta_frame=None, audio_path=None):
    """
    Write a 58-second MP4 Short from 6 slides.
    Slides: hook → overview → signals → trade setup → watchlist → CTA.
    Falls back to single-slide Ken-Burns if needed.
    Mixes in audio_path if supplied.
    """
    # Save all frames to temp PNGs
    tmp_files = []

    def save_tmp(img):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img.save(f.name, "PNG")
            tmp_files.append(f.name)
            return f.name

    ffmpeg_bin = _get_ffmpeg()
    fps = 25

    # ── Multi-slide path — encode each slide then concat ─────────────────────
    if hook_frame is not None and cta_frame is not None:
        # Pull extra slides attached as attributes
        _overview  = getattr(_create_short, "_overview_frame",  None)
        _setup     = getattr(_create_short, "_setup_frame",     None)
        _watchlist = getattr(_create_short, "_watchlist_frame", None)

        # Build 6-slide sequence (fall back gracefully if new frames missing)
        slides = [(hook_frame, _SLIDE1_SECS)]
        if _overview:
            slides.append((_overview,  _SLIDE2_SECS))
        slides.append((frame, _SLIDE3_SECS))   # signals slide
        if _setup:
            slides.append((_setup,     _SLIDE4_SECS))
        if _watchlist:
            slides.append((_watchlist, _SLIDE5_SECS))
        slides.append((cta_frame, _SLIDE6_SECS))
        clip_paths = []
        ok = True
        for img, secs in slides:
            png = save_tmp(img)
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as cf:
                clip_path = cf.name
            tmp_files.append(clip_path)
            clip_cmd = [
                ffmpeg_bin, "-y",
                "-loop", "1", "-t", str(secs), "-i", png,
                "-vf", f"scale={_VIDEO_W}:{_VIDEO_H},setsar=1",
                "-r", str(fps),
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-preset", "fast", "-crf", "22",
                "-an", clip_path,
            ]
            res = subprocess.run(clip_cmd, capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                print(f"[YouTube] Slide encode failed: {res.stderr[-300:]}")
                ok = False; break
            clip_paths.append(clip_path)

        if ok and len(clip_paths) >= 2:
            # Write concat list file
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as lf:
                for cp in clip_paths:
                    lf.write(f"file '{cp}'\n")
                list_path = lf.name
            tmp_files.append(list_path)

            concat_cmd = [ffmpeg_bin, "-y",
                          "-f", "concat", "-safe", "0", "-i", list_path]
            if audio_path:
                concat_cmd += ["-i", audio_path]
            concat_cmd += ["-c:v", "copy"]
            if audio_path:
                concat_cmd += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
            concat_cmd += ["-movflags", "+faststart", output_path]

            try:
                res = subprocess.run(concat_cmd, capture_output=True, text=True, timeout=120)
                _cleanup(tmp_files, audio_path)
                if res.returncode == 0:
                    print(f"[YouTube] Multi-slide video created: {output_path}")
                    return True
                print(f"[YouTube] Concat failed, falling back\n{res.stderr[-400:]}")
            except Exception as e:
                print(f"[YouTube] Concat exception: {e}")
                _cleanup(tmp_files, audio_path)
        else:
            _cleanup(tmp_files, audio_path)

    # ── Single-slide fallback (original Ken-Burns) ────────────────────────────
    print("[YouTube] Using single-slide Ken-Burns fallback")
    p = save_tmp(frame)
    frames_total = _VIDEO_SECS * fps

    cmd = [
        ffmpeg_bin, "-y",
        "-loop", "1", "-i", p,
    ]

    if audio_path:
        cmd += ["-i", audio_path]

    zoom_filter = (
        f"zoompan="
        f"z='min(zoom+0.0006,1.12)':"
        f"x='iw/2-(iw/zoom/2)':"
        f"y='ih/2-(ih/zoom/2)':"
        f"d={frames_total}:"
        f"s={_VIDEO_W}x{_VIDEO_H}:"
        f"fps={fps}"
    )

    cmd += ["-vf", zoom_filter]

    if audio_path:
        cmd += ["-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "128k",
                "-shortest"]

    cmd += [
        "-t", str(_VIDEO_SECS),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "fast",
        "-crf", "22",
        output_path,
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        _cleanup(tmp_files, audio_path)
        if res.returncode == 0:
            print(f"[YouTube] ✅ Video created (fallback): {output_path}")
            return True
        print(f"[YouTube] ❌ ffmpeg error:\n{res.stderr[-600:]}")
        return False
    except subprocess.TimeoutExpired:
        print("[YouTube] ❌ ffmpeg timed out after 240s")
        return False
    except Exception as e:
        print(f"[YouTube] ❌ ffmpeg exception: {e}")
        return False


def _cleanup(paths: list, *extra):
    for p in list(paths) + list(extra):
        if p:
            try:
                os.unlink(p)
            except Exception:
                pass


# ── Upload ────────────────────────────────────────────────────────────────────
def _upload_to_youtube(service, video_path: str, title: str, description: str) -> bool:
    from googleapiclient.http import MediaFileUpload

    tags = [
        "day trading", "stock market", "algo trading", "AI trading",
        "paper trading", "finance", "investing", "stocks", "trading bot",
        "market analysis", "automated trading", "quant trading",
        "market genie", "AI signals", "stock signals",
    ]

    body = {
        "snippet": {
            "title":       title[:100],
            "description": description,
            "tags":        tags,
            "categoryId":  _CATEGORY_ID,
        },
        "status": {
            "privacyStatus":          "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media   = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True, chunksize=1024 * 1024)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    try:
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                print(f"[YouTube] Upload progress: {pct}%")
        vid_id = response.get("id", "?")
        print(f"[YouTube] Published: https://youtube.com/shorts/{vid_id}")
        return vid_id
    except Exception as e:
        print(f"[YouTube] Upload error: {e}")
        return None


def _upload_thumbnail(service, video_id, thumbnail_img):
    """Upload the hook frame as the custom thumbnail."""
    try:
        from googleapiclient.http import MediaIoBaseUpload
        import io
        buf = io.BytesIO()
        thumbnail_img.save(buf, format="JPEG", quality=92)
        buf.seek(0)
        media = MediaIoBaseUpload(buf, mimetype="image/jpeg", resumable=False)
        service.thumbnails().set(videoId=video_id, media_body=media).execute()
        print(f"[YouTube] Custom thumbnail set for {video_id}")
    except Exception as e:
        print(f"[YouTube] Thumbnail upload failed (non-fatal): {e}")


# ── Main entry point ──────────────────────────────────────────────────────────
def post_market_update(trigger: str = "midday"):
    """
    Generate and upload one Market Genie Short.
    trigger: "premarket" | "midday" | "eod" | "afterhours"
    """
    print(f"[YouTube] 🎬 Starting {trigger} post...")

    service = _get_yt_service()
    if not service:
        return False

    data = _fetch_market_data()

    # ── Generate all 6 slides ─────────────────────────────────────────────────
    print("[YouTube] Rendering 6 slides...")
    slide1_hook      = _generate_hook_frame(data, trigger)        # 1 — hook
    slide2_overview  = _generate_context_frame(data, trigger)     # 2 — market overview
    slide3_signals   = _generate_frame(data, trigger)             # 3 — AI signals
    slide4_setup     = _generate_trade_setup_slide(data, trigger) # 4 — trade plan ★
    slide5_watchlist = _generate_watchlist_slide(data, trigger)   # 5 — watchlist ★
    slide6_cta       = _generate_cta_slide(data, trigger)         # 6 — CTA / P&L

    # Attach extra slides so _create_short can pick them up
    _create_short._overview_frame  = slide2_overview
    _create_short._setup_frame     = slide4_setup
    _create_short._watchlist_frame = slide5_watchlist

    # Generate TTS voiceover (best-effort — None if unavailable)
    audio_path = _generate_voiceover(data, trigger)

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        mp4_path = tmp.name

    if not _create_short(slide3_signals, mp4_path,
                         hook_frame=slide1_hook,
                         cta_frame=slide6_cta,
                         audio_path=audio_path):
        return False

    # ── Title & description ───────────────────────────────────────────────────
    date_str = datetime.now().strftime("%b %d")
    pnl      = data["pnl_today"]
    pnl_sign = "+" if pnl >= 0 else "-"
    regime   = data["regime"]
    score    = data["regime_score"]

    hot = data.get("hot_tickers", [])
    hot_sorted = sorted(hot, key=lambda t: abs(t.get("pct", 0)), reverse=True)
    top_tickers = [
        f"{t['symbol']} {'+' if t['up'] else ''}{t['pct']:.1f}%"
        for t in hot_sorted[:3] if t.get("price", 0) >= 15
    ]
    ticker_str = "  |  ".join(top_tickers)
    top_mover = (f"{hot_sorted[0]['symbol']} {'+' if hot_sorted[0]['up'] else ''}{hot_sorted[0]['pct']:.1f}%"
                 if hot_sorted else "")

    regime_emoji = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(regime, "⚪")
    vix  = data["vix"]
    h0   = hot_sorted[0] if hot_sorted else None
    sigs = [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 65]

    def _viral_title(trigger):
        """Generate a click-optimised title with curiosity gap."""
        # Premarket
        if trigger == "premarket":
            if h0 and abs(h0.get("pct", 0)) >= 2:
                verb  = "SURGING" if h0["up"] else "CRASHING"
                emoji = "🚀" if h0["up"] else "💥"
                return f"{h0['symbol']} is {verb} pre-market {emoji} A.I. signals for {date_str}"
            if regime == "BULLISH":
                return f"🟢 Bullish setup brewing — A.I. pre-market brief {date_str}"
            if regime == "BEARISH":
                return f"⚠️ Danger zone pre-market — A.I. spotted {len(sigs)} bearish signals {date_str}"
            return f"Market opens in minutes — A.I. scanning 200 stocks right now 📊 {date_str}"

        # Midday
        if trigger == "midday":
            if h0 and abs(h0.get("pct", 0)) >= 4:
                verb  = "RIPPING" if h0["up"] else "GETTING CRUSHED"
                emoji = "🔥" if h0["up"] else "🔴"
                return f"{h0['symbol']} is {verb} {abs(h0['pct']):.1f}% {emoji} A.I. midday update {date_str}"
            if sigs:
                top_sig = sigs[0]
                bull    = "bull" in top_sig.get("direction", "").lower()
                emoji   = "🟢" if bull else "🔴"
                return f"A.I. just flagged {top_sig['symbol']} at {top_sig.get('confidence',70):.0f}% confidence {emoji} {date_str}"
            return f"Midday signals — A.I. just re-scanned 200 stocks 🧠 {date_str}"

        # EOD
        if trigger == "eod":
            if abs(pnl) >= 100:
                emoji = "💰" if pnl > 0 else "📉"
                word  = "made" if pnl > 0 else "lost"
                return f"The A.I. {word} ${abs(pnl):,.0f} today {emoji} here's what it traded {date_str}"
            if h0 and abs(h0.get("pct", 0)) >= 5:
                verb  = "EXPLODED" if h0["up"] else "COLLAPSED"
                emoji = "🚀" if h0["up"] else "💥"
                return f"{h0['symbol']} {verb} {abs(h0['pct']):.1f}% at close {emoji} full A.I. wrap {date_str}"
            if regime == "BULLISH":
                return f"🟢 Bulls won today — A.I. end-of-day breakdown {date_str}"
            if regime == "BEARISH":
                return f"🔴 Market got wrecked — A.I. post-mortem {date_str}"
            return f"A.I. close report: {len(sigs)} signals fired today 📊 {date_str}"

        # After-hours
        if trigger == "afterhours":
            if h0 and abs(h0.get("pct", 0)) >= 5:
                verb  = "RIPPING" if h0["up"] else "GETTING DESTROYED"
                emoji = "🚀" if h0["up"] else "💣"
                return f"{h0['symbol']} {verb} {abs(h0['pct']):.1f}% after hours {emoji} A.I. breakdown {date_str}"
            if sigs:
                top_sig = sigs[0]
                bull    = "bull" in top_sig.get("direction", "").lower()
                conf    = top_sig.get("confidence", 70)
                emoji   = "🟢" if bull else "🔴"
                if conf >= 90:
                    return f"A.I. is {conf:.0f}% confident on {'BULL' if bull else 'BEAR'} {top_sig['symbol']} after hours {emoji} {date_str}"
            if ticker_str:
                return f"{ticker_str} | A.I. after-hours report {regime_emoji} {date_str}"
            return f"After-hours A.I. signals — what to watch before tomorrow's open 👀 {date_str}"

        return f"Market Genie A.I. Signals | {date_str}"

    title = _viral_title(trigger)[:100]

    pos_lines = "\n".join(
        f"  {p['symbol']} {p['side']}: {'+' if p['unrealized_pl'] >= 0 else '-'}${abs(p['unrealized_pl']):,.0f}"
        for p in data["positions"]
    ) or "  No open positions"

    signals = [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 65]
    sig_lines = "\n".join(
        f"  {'🟢 BULL' if 'bull' in s.get('direction','').lower() else '🔴 BEAR'} {s['symbol']} — {s.get('confidence',70):.0f}% confidence"
        for s in signals[:3]
    ) or "  No high-confidence signals"

    # Ticker-specific hashtags for searchability
    ticker_tags = " ".join(
        f"#{t['symbol'].lower()}" for t in hot_sorted[:5] if t.get("symbol")
    )
    sig_ticker_tags = " ".join(
        f"#{s['symbol'].lower()}" for s in sigs[:3] if s.get("symbol")
    )

    # Comment CTA — comments are the #1 YouTube algorithm signal
    comment_cta = (
        "💬 DROP A COMMENT — are you bullish 🟢 or bearish 🔴 on the market right now?\n"
        "Every comment helps the algorithm show this to more traders. 🙏\n\n"
    )

    description = (
        f"📊 Market Genie AI — {trigger.upper()} UPDATE | {date_str}\n\n"
        f"{comment_cta}"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧠 AI Regime: {regime_emoji} {regime} ({score}/100)\n"
        f"💰 Today's P&L: {pnl_sign}${abs(pnl):,.2f}\n"
        f"📈 NQ Futures: {data['nq_pct']:+.2f}%\n"
        f"🌡️ VIX: {data['vix']:.1f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🤖 AI Signals:\n{sig_lines}\n\n"
        f"📂 Open Positions ({len(data['positions'])}):\n{pos_lines}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔔 FOLLOW for free AI signals every single trading day.\n"
        f"This bot scans 200+ stocks in real-time — completely automated.\n\n"
        f"⚠️ NOT FINANCIAL ADVICE — educational & informational purposes only.\n\n"
        f"#daytrading #stocks #algotrading #AItrading #stockmarket #finance #investing "
        f"#marketgenie #tradingsignals #stocksignals #wallstreet #nasdaq #sp500 #trading "
        f"#stocktrading #daytrader #technicalanalysis #options #swingtrading "
        f"{ticker_tags} {sig_ticker_tags}"
    )

    vid_id = _upload_to_youtube(service, mp4_path, title, description)

    try:
        os.unlink(mp4_path)
    except Exception:
        pass

    if vid_id:
        # Upload hook frame as custom thumbnail — what people see before clicking
        _upload_thumbnail(service, vid_id, slide1_hook)
        return True

    return False


# ── Scheduler loop ────────────────────────────────────────────────────────────
_YT_POSTED_FILE = "/tmp/youtube_posted.json"


def _load_posted() -> dict:
    try:
        if Path(_YT_POSTED_FILE).exists():
            with open(_YT_POSTED_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_posted(d: dict):
    try:
        from datetime import datetime as _dt, timedelta
        cutoff = (_dt.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        pruned = {k: v for k, v in d.items() if k[:10] >= cutoff}
        with open(_YT_POSTED_FILE, "w") as f:
            json.dump(pruned, f)
    except Exception:
        pass


def _youtube_scheduler_loop():
    """Background thread: posts at 9:15, 12:00, 16:15, 17:30 ET on weekdays."""
    import pytz
    from datetime import time as dtime

    _posted = _load_posted()

    slots = [
        (dtime(9,  35), dtime(9,  50), "premarket"),  # delayed 9:35: 5 min of open data — first prints confirmed, snapshot stable
        (dtime(12,  0), dtime(12, 15), "midday"),
        (dtime(16, 20), dtime(16, 35), "eod"),  # delayed: flattener runs 3:55, capture after close
        (dtime(17, 30), dtime(17, 45), "afterhours"),
    ]

    while True:
        try:
            et_now   = datetime.now(pytz.timezone("America/New_York"))
            date_key = et_now.strftime("%Y-%m-%d")
            t        = et_now.time()
            is_wday  = et_now.weekday() <= 4

            if not os.getenv("YOUTUBE_TOKEN_JSON"):
                time.sleep(60)
                continue

            if is_wday:
                for start, end, trigger in slots:
                    key = f"{date_key}_{trigger}"
                    if start <= t < end and key not in _posted:
                        _posted[key] = True
                        _save_posted(_posted)
                        threading.Thread(
                            target=post_market_update,
                            args=(trigger,),
                            daemon=True,
                            name=f"YT-{trigger}",
                        ).start()

        except Exception as e:
            print(f"[YouTube] Scheduler error: {e}")

        time.sleep(30)


def start_youtube_scheduler():
    """Call once at server startup. No-op if YOUTUBE_TOKEN_JSON is not set."""
    if not os.getenv("YOUTUBE_TOKEN_JSON"):
        print("[YouTube] YOUTUBE_TOKEN_JSON not set — auto-posting disabled "
              "(set env var after running youtube_setup.py)")
        return
    t = threading.Thread(target=_youtube_scheduler_loop, daemon=True, name="YouTubeScheduler")
    t.start()
    print("[YouTube] ✅ Scheduler started — posts at 9:15 AM, 12:00 PM, 4:15 PM, 5:30 PM ET (weekdays)")
