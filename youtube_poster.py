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
_VIDEO_SECS  = 30
_CATEGORY_ID = "25"   # News & Politics

# Slide durations (must sum to _VIDEO_SECS)
_SLIDE1_SECS = 9    # hook
_SLIDE2_SECS = 13   # dashboard
_SLIDE3_SECS = 8    # P&L / CTA
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
            # hot_tickers now comes directly from server's live Alpaca cache (real intraday %)
            # Only fall back to yfinance if the server didn't provide it
            if srv.get("hot_tickers"):
                defaults["hot_tickers"] = srv["hot_tickers"]
            else:
                social_syms = [t["symbol"] for t in srv.get("social_hot", [])[:8]
                               if t.get("symbol")]
                if social_syms:
                    defaults["hot_tickers"] = _fetch_ticker_moves(social_syms)
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


# ── Colour helpers ────────────────────────────────────────────────────────────
def _regime_rgb(regime: str):
    return {
        "BULLISH": (74, 222, 128),
        "BEARISH": (248, 113, 113),
        "NEUTRAL": (250, 204, 21),
    }.get(regime, (156, 163, 175))


def _ic(v):
    GREEN = (74, 222, 128)
    RED   = (248, 113, 113)
    return GREEN if v >= 0 else RED


def _dim_color(rgb, factor=7):
    return (rgb[0] // factor, rgb[1] // factor, rgb[2] // factor)


# ─────────────────────────────────────────────────────────────────────────────
# SHARED DRAWING HELPERS
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
# SLIDE 1 — ALERT HOOK FRAME
# Breaking-news alert cards for top movers + regime
# ═════════════════════════════════════════════════════════════════════════════

def _generate_hook_frame(data, trigger):
    """Slide 1 — Market overview. Clean, readable, no clutter."""
    from PIL import Image, ImageDraw
    W, H = _VIDEO_W, _VIDEO_H
    BG    = (6, 8, 14)
    WHITE = (230, 238, 250)
    DIM   = (100, 118, 140)
    AMBER = (220, 155, 40)
    GREEN = (74, 222, 128)
    RED   = (248, 113, 113)
    PANEL = (14, 20, 32)
    ACCENT= (56, 189, 248)

    regime = data.get("regime", "NEUTRAL")
    score  = data.get("regime_score", 50)
    rc     = {"BULLISH": GREEN, "BEARISH": RED, "NEUTRAL": AMBER}.get(regime, AMBER)
    spy    = data.get("spy_pct", 0.0)
    qqq    = data.get("qqq_pct", 0.0)
    nq     = data.get("nq_pct", 0.0)
    vix    = data.get("vix", 16.5)
    hot    = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)

    img = Image.new("RGB", (W, H), BG)
    d   = ImageDraw.Draw(img)

    fnt_nano  = _load_font(30)
    fnt_xs    = _load_font(40)
    fnt_sm    = _load_font(52)
    fnt_md    = _load_font(68, bold=True)
    fnt_lg    = _load_font(90, bold=True)
    fnt_xl    = _load_font(120, bold=True)
    fnt_hero  = _load_font(160, bold=True)

    PAD = 48

    # Header bar
    d.rectangle([0, 0, W, 130], fill=(10, 14, 24))
    d.text((PAD, 65), "MARKET GENIE", font=fnt_md, fill=WHITE, anchor="lm")
    trig_lbl = {"premarket": "PRE-MARKET", "midday": "MIDDAY", "eod": "CLOSE", "afterhours": "AFTER HOURS"}.get(trigger, "LIVE")
    d.text((W - PAD, 65), trig_lbl, font=fnt_sm, fill=ACCENT, anchor="rm")
    d.rectangle([0, 128, W, 132], fill=ACCENT)

    # Date
    d.text((PAD, 170), data.get("timestamp", ""), font=fnt_xs, fill=DIM)

    # Big regime pill
    y = 220
    d.rectangle([0, y, W, y + 220], fill=PANEL)
    d.rectangle([0, y, 8, y + 220], fill=rc)
    regime_label = {"BULLISH": "BULLISH DAY", "BEARISH": "BEARISH DAY", "NEUTRAL": "MIXED DAY"}.get(regime, regime)
    d.text((W // 2, y + 80), regime_label, font=fnt_xl, fill=rc, anchor="mm")
    d.text((W // 2, y + 160), f"Regime Score: {score}/100", font=fnt_sm, fill=DIM, anchor="mm")

    y += 240

    # Market indices row
    d.rectangle([0, y, W, y + 110], fill=(10, 16, 28))
    col = (W - PAD * 2) // 3
    items = [
        (f"SPY {spy:+.2f}%",  GREEN if spy >= 0 else RED),
        (f"QQQ {qqq:+.2f}%",  GREEN if qqq >= 0 else RED),
        (f"VIX {vix:.1f}",     RED if vix > 20 else DIM),
    ]
    for i, (txt, color) in enumerate(items):
        d.text((PAD + i * col + col // 2, y + 55), txt, font=fnt_sm, fill=color, anchor="mm")

    y += 130

    # Divider + "TODAY'S MOVERS"
    d.rectangle([0, y, W, y + 2], fill=(28, 40, 56))
    y += 20
    d.text((PAD, y + 28), "TODAY'S TOP MOVERS", font=fnt_xs, fill=AMBER)
    y += 72

    # Top 5 movers — simple clean rows
    row_h = max(130, (H - y - 120) // max(len(hot[:5]), 1))
    for tk in hot[:5]:
        tc = GREEN if tk["up"] else RED
        sign = "+" if tk["up"] else ""
        d.rectangle([0, y, W, y + row_h], fill=PANEL)
        d.rectangle([0, y, 6, y + row_h], fill=tc)
        mid = y + row_h // 2
        d.text((PAD + 16, mid - 22), tk["symbol"], font=fnt_lg, fill=WHITE, anchor="lm")
        d.text((PAD + 16, mid + 32), f"${tk['price']:,.2f}", font=fnt_xs, fill=DIM, anchor="lm")
        d.text((W - PAD, mid), f"{sign}{tk['pct']:.2f}%", font=fnt_lg, fill=tc, anchor="rm")
        d.rectangle([0, y + row_h - 1, W, y + row_h], fill=(20, 28, 44))
        y += row_h

    # Footer
    d.rectangle([0, H - 64, W, H], fill=(8, 12, 20))
    d.text((W // 2, H - 32), "NOT FINANCIAL ADVICE  |  AI SIGNALS  |  MARKET GENIE", font=fnt_nano, fill=DIM, anchor="mm")

    return img


def _generate_frame(data: dict, trigger: str):
    """Slide 2 — AI signals. Simple BULL/BEAR cards."""
    from PIL import Image, ImageDraw
    W, H  = _VIDEO_W, _VIDEO_H
    BG    = (6, 8, 14)
    WHITE = (230, 238, 250)
    DIM   = (100, 118, 140)
    AMBER = (220, 155, 40)
    GREEN = (74, 222, 128)
    RED   = (248, 113, 113)
    PANEL = (14, 20, 32)
    ACCENT= (56, 189, 248)
    ALT   = (10, 14, 22)

    regime = data.get("regime", "NEUTRAL")
    rc     = {"BULLISH": GREEN, "BEARISH": RED, "NEUTRAL": AMBER}.get(regime, AMBER)
    signals= [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 60]
    hot    = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    pnl    = data.get("pnl_today", 0.0)
    equity = data.get("equity", 100_000.0)

    img = Image.new("RGB", (W, H), BG)
    d   = ImageDraw.Draw(img)

    fnt_nano = _load_font(30)
    fnt_xs   = _load_font(40)
    fnt_sm   = _load_font(52)
    fnt_md   = _load_font(68, bold=True)
    fnt_lg   = _load_font(90, bold=True)
    fnt_xl   = _load_font(120, bold=True)

    PAD = 48

    # Header
    d.rectangle([0, 0, W, 130], fill=(10, 14, 24))
    d.text((PAD, 65), "MARKET GENIE", font=fnt_md, fill=WHITE, anchor="lm")
    trig_lbl = {"premarket": "PRE-MARKET", "midday": "MIDDAY", "eod": "CLOSE", "afterhours": "AFTER HOURS"}.get(trigger, "LIVE")
    d.text((W - PAD, 65), trig_lbl, font=fnt_md, fill=rc, anchor="rm")
    d.rectangle([0, 128, W, 132], fill=rc)
    d.text((PAD, 170), data.get("timestamp", ""), font=fnt_xs, fill=DIM)

    y = 210

    # Section: AI Signals
    d.text((PAD, y), "AI SIGNALS", font=fnt_sm, fill=AMBER)
    d.text((W - PAD, y), f"{len(signals)} active", font=fnt_xs, fill=DIM, anchor="ra")
    y += 60

    sig_h = max(200, min(320, (H - y - 500) // max(len(signals[:4]), 1)))
    for sig in signals[:4]:
        bull   = "bull" in sig.get("direction", "bull").lower()
        sc     = GREEN if bull else RED
        label  = "BULL" if bull else "BEAR"
        conf   = sig.get("confidence", 70)
        rb     = PANEL if signals.index(sig) % 2 == 0 else ALT
        d.rectangle([0, y, W, y + sig_h], fill=rb)
        d.rectangle([0, y, 8, y + sig_h], fill=sc)
        mid = y + sig_h // 2
        # Symbol
        d.text((PAD + 16, mid - 26), sig["symbol"], font=fnt_lg, fill=WHITE, anchor="lm")
        # Bull/Bear badge
        bw = 110
        try: bw = int(d.textlength(label, font=fnt_xs)) + 20
        except: pass
        sx = PAD + 16
        try: sx = int(d.textlength(sig["symbol"], font=fnt_lg)) + PAD + 30
        except: sx = PAD + 200
        d.rounded_rectangle([sx, mid - 28, sx + bw, mid + 4], radius=6,
                             fill=(sc[0]//5, sc[1]//5, sc[2]//5))
        d.rounded_rectangle([sx, mid - 28, sx + bw, mid + 4], radius=6, outline=sc, width=2)
        d.text((sx + bw // 2, mid - 12), label, font=fnt_xs, fill=sc, anchor="mm")
        # Confidence
        d.text((W - PAD, mid - 26), f"{conf:.0f}%", font=fnt_md, fill=sc, anchor="ra")
        # Bar
        bar_x = PAD + 16
        bar_w = W - PAD * 2 - 32
        bar_y = y + sig_h - 28
        fill_w = int(bar_w * min(conf / 100, 1.0))
        d.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + 14], radius=6, fill=(20, 30, 44))
        if fill_w > 4:
            d.rounded_rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + 14], radius=6, fill=sc)
        y += sig_h + 4

    y += 20
    d.rectangle([0, y, W, y + 2], fill=(28, 40, 56))
    y += 20

    # P&L or Equity block
    has_pnl = abs(pnl) >= 1 and trigger not in ("premarket",)
    d.rectangle([0, y, W, y + 220], fill=PANEL)
    if has_pnl:
        pc   = GREEN if pnl >= 0 else RED
        sign = "+" if pnl >= 0 else "-"
        d.text((PAD + 16, y + 28), "TODAY'S P&L", font=fnt_xs, fill=AMBER)
        d.text((PAD + 16, y + 80), f"{sign}${abs(pnl):,.0f}", font=fnt_xl, fill=pc)
    elif trigger == "premarket":
        d.text((PAD + 16, y + 28), "CAPITAL READY", font=fnt_xs, fill=AMBER)
        d.text((PAD + 16, y + 80), f"${equity:,.0f}", font=fnt_xl, fill=WHITE)
        d.text((W - PAD, y + 95), "Opens 9:30 AM ET", font=fnt_sm, fill=DIM, anchor="ra")
    else:
        d.text((PAD + 16, y + 28), "ACCOUNT", font=fnt_xs, fill=AMBER)
        d.text((PAD + 16, y + 80), f"${equity:,.0f}", font=fnt_xl, fill=WHITE)

    y += 240

    # Top mover callout if space
    if hot and y + 180 < H - 80:
        h0 = hot[0]
        hc = GREEN if h0["up"] else RED
        d.rectangle([0, y, W, y + 220], fill=ALT)
        d.rectangle([0, y, 6, y + 160], fill=hc)
        d.text((PAD + 16, y + 35), "BIGGEST MOVER", font=fnt_xs, fill=DIM)
        sign = "+" if h0["up"] else ""
        d.text((PAD + 16, y + 88), h0["symbol"], font=fnt_lg, fill=WHITE)
        d.text((W - PAD, y + 88), f"{sign}{h0['pct']:.2f}%", font=fnt_lg, fill=hc, anchor="ra")
        y += 230

    # Footer
    d.rectangle([0, H - 64, W, H], fill=(8, 12, 20))
    d.text((W // 2, H - 32), "NOT FINANCIAL ADVICE  |  AI SIGNALS  |  MARKET GENIE", font=fnt_nano, fill=DIM, anchor="mm")

    return img


def _generate_cta_frame(data, trigger):
    """Slide 3 — What to watch. Simple watchlist."""
    from PIL import Image, ImageDraw
    W, H  = _VIDEO_W, _VIDEO_H
    BG    = (6, 8, 14)
    WHITE = (230, 238, 250)
    DIM   = (100, 118, 140)
    AMBER = (220, 155, 40)
    GREEN = (74, 222, 128)
    RED   = (248, 113, 113)
    PANEL = (14, 20, 32)
    ACCENT= (56, 189, 248)

    hot     = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    signals = [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 60]
    regime  = data.get("regime", "NEUTRAL")
    score   = data.get("regime_score", 50)
    rc      = {"BULLISH": GREEN, "BEARISH": RED, "NEUTRAL": AMBER}.get(regime, AMBER)
    vix     = data.get("vix", 16.5)
    pc_ratio= data.get("put_call_ratio", 1.0)

    img = Image.new("RGB", (W, H), BG)
    d   = ImageDraw.Draw(img)

    fnt_nano = _load_font(30)
    fnt_xs   = _load_font(40)
    fnt_sm   = _load_font(52)
    fnt_md   = _load_font(68, bold=True)
    fnt_lg   = _load_font(90, bold=True)
    fnt_xl   = _load_font(120, bold=True)

    PAD = 48

    # Header
    d.rectangle([0, 0, W, 130], fill=(10, 14, 24))
    d.text((PAD, 58), "MARKET GENIE", font=fnt_sm, fill=WHITE, anchor="lm")
    d.text((W - PAD, 58), "AI SIGNALS", font=fnt_sm, fill=ACCENT, anchor="rm")
    d.rectangle([0, 104, W, 108], fill=ACCENT)
    d.text((PAD, 136), data.get("timestamp", ""), font=fnt_xs, fill=DIM)

    y = 220

    # Market health row
    d.rectangle([0, y, W, y + 130], fill=(12, 18, 30))
    col = (W - PAD * 2) // 3
    stats = [
        ("VIX", f"{vix:.1f}", RED if vix > 20 else GREEN),
        ("P/C RATIO", f"{pc_ratio:.2f}", RED if pc_ratio > 1.2 else GREEN),
        ("REGIME", f"{score}/100", rc),
    ]
    for i, (lbl, val, vc) in enumerate(stats):
        cx = PAD + i * col + col // 2
        d.text((cx, y + 36), lbl, font=fnt_xs, fill=DIM, anchor="mm")
        d.text((cx, y + 95), val, font=fnt_md, fill=vc, anchor="mm")
    y += 150

    d.rectangle([0, y, W, y + 2], fill=(28, 40, 56))
    y += 20

    # Watchlist — combine signals + movers
    d.text((PAD, y + 6), "STOCKS TO WATCH TODAY", font=fnt_sm, fill=AMBER)
    y += 60

    watch = []
    seen = set()
    for sig in signals[:3]:
        sym = sig["symbol"]
        bull = "bull" in sig.get("direction", "").lower()
        # find price from hot_tickers
        price = next((t["price"] for t in hot if t["symbol"] == sym), 0)
        pct   = next((t["pct"]   for t in hot if t["symbol"] == sym), 0)
        watch.append({"symbol": sym, "label": "BULL" if bull else "BEAR",
                      "color": GREEN if bull else RED, "price": price, "pct": pct})
        seen.add(sym)

    for tk in hot:
        if tk["symbol"] not in seen and len(watch) < 5:
            tc = GREEN if tk["up"] else RED
            watch.append({"symbol": tk["symbol"], "label": "+%" if tk["up"] else "-%",
                          "color": tc, "price": tk["price"], "pct": tk["pct"]})
            seen.add(tk["symbol"])

    card_h = max(180, min(280, (H - y - 80) // max(len(watch), 1)))
    for w in watch:
        sc = w["color"]
        d.rectangle([0, y, W, y + card_h], fill=(14, 20, 32))
        d.rectangle([0, y, 6, y + card_h], fill=sc)
        mid = y + card_h // 2
        d.text((PAD + 16, mid - 24), w["symbol"], font=fnt_lg, fill=WHITE, anchor="lm")
        # Label badge (clean: BULL/BEAR/HOT)
        badge = w["label"] if w["label"] in ("BULL", "BEAR") else ("HOT" if w["pct"] > 0 else "WEAK")
        try: bw = int(d.textlength(badge, font=fnt_xs)) + 24
        except: bw = 120
        try: sx = int(d.textlength(w["symbol"], font=fnt_lg)) + PAD + 32
        except: sx = PAD + 220
        d.rounded_rectangle([sx, mid - 30, sx + bw, mid + 6], radius=8,
                             fill=(sc[0]//5, sc[1]//5, sc[2]//5), outline=sc, width=2)
        d.text((sx + bw // 2, mid - 12), badge, font=fnt_xs, fill=sc, anchor="mm")
        # Price top-right, % below it — no overlap
        if w["price"] > 0:
            d.text((W - PAD, y + 28), f"${w['price']:,.2f}", font=fnt_md, fill=WHITE, anchor="ra")
        if w["pct"] != 0:
            sign = "+" if w["pct"] > 0 else ""
            d.text((W - PAD, y + 88), f"{sign}{w['pct']:.2f}%", font=fnt_sm, fill=sc, anchor="ra")
        d.rectangle([0, y + card_h - 1, W, y + card_h], fill=(20, 28, 44))
        y += card_h

    # Footer
    d.rectangle([0, H - 64, W, H], fill=(8, 12, 20))
    d.text((W // 2, H - 32), "NOT FINANCIAL ADVICE  |  AI SIGNALS  |  MARKET GENIE", font=fnt_nano, fill=DIM, anchor="mm")

    return img



def _generate_voiceover(data, trigger):
    """
    Generate a spoken voiceover MP3 using gTTS.
    Returns the path to the MP3 file, or None on failure.
    """
    try:
        from gtts import gTTS
    except ImportError:
        print("[YouTube] ⚠️  gtts not installed — skipping voiceover")
        return None

    regime = data["regime"].lower()
    score  = data["regime_score"]
    pnl    = data["pnl_today"]
    pcs    = "up" if pnl >= 0 else "down"
    hot    = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    vix    = data["vix"]
    nq     = data["nq_pct"]

    date_str = datetime.now().strftime("%B %d")

    # Build spoken script
    trigger_phrase = {
        "premarket":  "pre-market brief",
        "midday":     "midday update",
        "eod":        "end of day wrap",
        "afterhours": "after-hours summary",
    }.get(trigger, "live update")

    script_parts = [
        f"Market Genie A.I. {trigger_phrase} for {date_str}.",
        f"Market regime is {regime}, scoring {score} out of 100.",
    ]

    if nq != 0:
        nq_dir = "up" if nq >= 0 else "down"
        script_parts.append(f"NASDAQ futures are {nq_dir} {abs(nq):.1f} percent.")

    if vix > 20:
        script_parts.append(f"Watch out — VIX is elevated at {vix:.0f}.")

    if hot:
        h0   = hot[0]
        hdir = "up" if h0["up"] else "down"
        script_parts.append(
            f"Biggest mover: {h0['symbol']}, {hdir} {abs(h0['pct']):.1f} percent."
        )

    signals = [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 65][:1]
    if signals:
        sig  = signals[0]
        bull = "bull" in sig.get("direction", "bull").lower()
        script_parts.append(
            f"A.I. signal: {'bullish' if bull else 'bearish'} on {sig['symbol']} "
            f"with {sig.get('confidence', 70):.0f} percent confidence."
        )

    if trigger != "premarket" and abs(pnl) >= 1:
        script_parts.append(f"Today's P and L: {pcs} ${abs(pnl):,.0f}.")
    elif trigger == "afterhours" and abs(pnl) < 1:
        script_parts.append("No trades today. Signals reset tomorrow at market open.")

    script_parts.append("Follow Market Genie for free A.I. signals every single trading day.")

    script = "  ".join(script_parts)
    print(f"[YouTube] 🎙️  Voiceover script: {script[:120]}...")

    try:
        tts = gTTS(text=script, lang="en", slow=False)
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tts.save(tmp.name)
        tmp.close()
        print(f"[YouTube] ✅ Voiceover saved: {tmp.name}")
        return tmp.name
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
    Write a 30-second MP4 Short.
    If hook_frame and cta_frame are provided, creates a 3-slide video with
    xfade transitions.  Falls back to single-slide Ken-Burns if needed.
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
        slides = [
            (hook_frame, _SLIDE1_SECS),
            (frame,      _SLIDE2_SECS),
            (cta_frame,  _SLIDE3_SECS),
        ]
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

        if ok and len(clip_paths) == 3:
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

    # Generate all three slides
    hook_frame = _generate_hook_frame(data, trigger)
    dash_frame = _generate_frame(data, trigger)
    cta_frame  = _generate_cta_frame(data, trigger)

    # Generate TTS voiceover (best-effort — None if unavailable)
    audio_path = _generate_voiceover(data, trigger)

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        mp4_path = tmp.name

    if not _create_short(dash_frame, mp4_path,
                         hook_frame=hook_frame,
                         cta_frame=cta_frame,
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
    vix = data["vix"]
    vix_note = f" ⚡VIX {vix:.0f}" if vix > 22 else ""
    has_pnl = abs(pnl) >= 1

    titles = {
        "premarket": (
            f"{top_mover + '  |  ' if top_mover else ''}"
            f"AI Pre-Market {date_str} {regime_emoji}{regime} {score}/100{vix_note}"
        ),
        "midday": (
            f"{'AI Midday: ' + pnl_sign + '$' + f'{abs(pnl):,.0f}' + '  |  ' if has_pnl else ''}"
            f"{ticker_str or (regime_emoji + regime + ' ' + str(score))}  [{date_str}]"
        ),
        "eod": (
            f"{'AI closed ' + pnl_sign + '$' + f'{abs(pnl):,.0f}' + ' today  |  ' if has_pnl else ''}"
            f"{ticker_str or top_mover or (regime_emoji + regime)}  [{date_str}]"
        ),
        "afterhours": (
            f"{ticker_str + '  |  ' if ticker_str else ''}"
            f"{'AI P&L: ' + pnl_sign + '$' + f'{abs(pnl):,.0f}' + '  |  ' if has_pnl else ''}"
            f"After-Hours {date_str} {regime_emoji}{regime}"
        ),
    }
    title = titles.get(trigger, f"Market Genie AI Signals | {date_str}")[:100]

    pos_lines = "\n".join(
        f"  {p['symbol']} {p['side']}: {'+' if p['unrealized_pl'] >= 0 else '-'}${abs(p['unrealized_pl']):,.0f}"
        for p in data["positions"]
    ) or "  No open positions"

    signals = [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 65]
    sig_lines = "\n".join(
        f"  {'🟢 BULL' if 'bull' in s.get('direction','').lower() else '🔴 BEAR'} {s['symbol']} — {s.get('confidence',70):.0f}% confidence"
        for s in signals[:3]
    ) or "  No high-confidence signals"

    description = (
        f"📊 Market Genie AI Auto-Trader — {trigger.upper()} UPDATE\n\n"
        f"🧠 Regime: {regime_emoji} {regime} ({score}/100)\n"
        f"💰 Today's P&L: {pnl_sign}${abs(pnl):,.2f}\n"
        f"📈 NQ Futures: {data['nq_pct']:+.2f}%\n"
        f"🌡️ VIX: {data['vix']:.1f}\n\n"
        f"🤖 AI Signals:\n{sig_lines}\n\n"
        f"📂 Open Positions ({len(data['positions'])}):\n{pos_lines}\n\n"
        f"⚠️ NOT FINANCIAL ADVICE — for educational & informational purposes only.\n\n"
        f"#daytrading #stocks #algotrading #AItrading #stockmarket #finance #investing "
        f"#marketgenie #tradingsignals #stocksignals #wallstreet #nasdaq #sp500"
    )

    vid_id = _upload_to_youtube(service, mp4_path, title, description)

    try:
        os.unlink(mp4_path)
    except Exception:
        pass

    if vid_id:
        # Upload hook frame as custom thumbnail — what people see before clicking
        _upload_thumbnail(service, vid_id, hook_frame)
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
