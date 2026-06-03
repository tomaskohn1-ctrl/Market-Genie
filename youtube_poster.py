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
_VIDEO_SECS  = 59   # 9+9+10+13+10+8 = 59s (6-slide layout)
_CATEGORY_ID = "25"   # News & Politics

# Slide durations (must sum to _VIDEO_SECS)
_SLIDE1_SECS = 9    # hook — biggest mover / regime
_SLIDE2_SECS = 9    # market overview — SPY/QQQ/VIX/futures
_SLIDE3_SECS = 10   # AI signals — top 3 with confidence
_SLIDE4_SECS = 13   # 🆕 TRADE SETUP — entry/stop/target/R:R
_SLIDE5_SECS = 10   # 🆕 WATCHLIST — 3 stocks with key levels
_SLIDE6_SECS = 8    # CTA / P&L close
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
    regime_label = {
        "BULLISH": "🟢 BULLS IN CONTROL",
        "BEARISH": "🔴 BEARS IN CONTROL",
        "NEUTRAL": "🟡 CHOPPY TAPE",
    }.get(regime, regime)
    d.text((W // 2, y + 80), regime_label, font=fnt_xl, fill=rc, anchor="mm")
    d.text((W // 2, y + 160), f"A.I. Regime Score: {score}/100", font=fnt_sm, fill=DIM, anchor="mm")

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

    # BREAKING badge for big movers
    if hot and abs(hot[0].get("pct", 0)) >= 5:
        _draw_breaking_badge(d, W, hot[0]["symbol"], hot[0]["pct"], fnt_sm)

    # Caption bar — readable on mute
    regime_c  = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(regime, "⚪")
    caption   = f"{regime_c} {regime}  •  Score {score}/100  •  Follow for daily A.I. signals 🔔"
    _draw_caption_bar(d, W, H, caption, fnt_xs, fnt_nano)

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

    # Caption bar — readable on mute
    top_sig_text = ""
    if signals:
        s0   = signals[0]
        bull = "bull" in s0.get("direction", "").lower()
        top_sig_text = f"{'🟢 BULL' if bull else '🔴 BEAR'} {s0['symbol']} {s0.get('confidence',70):.0f}%  •  "
    caption = f"{top_sig_text}Follow Market Genie for free A.I. signals 🔔"
    _draw_caption_bar(d, W, H, caption, fnt_xs, fnt_nano)

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

    # Caption bar — readable on mute
    _draw_caption_bar(d, W, H, "🔔 Follow Market Genie — free A.I. signals every trading day", fnt_xs, fnt_nano)

    return img





def _generate_context_frame(data, trigger):
    """Slide 4 — Context & insights: market pulse, top signal, watchlist."""
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

    regime  = data.get("regime", "NEUTRAL")
    rc      = {"BULLISH": GREEN, "BEARISH": RED, "NEUTRAL": AMBER}.get(regime, AMBER)
    hot     = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    signals = [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 60]
    vix     = data.get("vix", 16.5)
    spy     = data.get("spy_pct", 0.0)
    qqq     = data.get("qqq_pct", 0.0)
    nq      = data.get("nq_pct", 0.0)

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
    d.rectangle([0, 0, W, 120], fill=(10, 14, 24))
    d.text((PAD, 60), "MARKET GENIE", font=fnt_md, fill=WHITE, anchor="lm")
    d.text((W - PAD, 60), "INSIGHTS", font=fnt_md, fill=AMBER, anchor="rm")
    d.rectangle([0, 118, W, 122], fill=AMBER)
    d.text((PAD, 152), data.get("timestamp", ""), font=fnt_xs, fill=DIM)
    y = 195

    # ── Market Pulse ──────────────────────────────────────────────────────────
    d.text((PAD, y + 4), "MARKET PULSE", font=fnt_sm, fill=AMBER)
    y += 58

    regime_text = {
        "BULLISH": "Bulls in control  |  Trend is UP",
        "BEARISH": "Bears in control  |  Trend is DOWN",
        "NEUTRAL": "Mixed signals  |  No clear edge",
    }.get(regime, regime)

    pulse_h = 100
    d.rectangle([0, y, W, y + pulse_h], fill=PANEL)
    d.rectangle([0, y, 6, y + pulse_h], fill=rc)
    d.text((PAD + 16, y + pulse_h // 2), regime_text, font=fnt_sm, fill=rc, anchor="lm")
    y += pulse_h + 8

    # SPY / QQQ / NQ row
    row_h = 90
    index_rows = [
        (f"SPY  {spy:+.2f}%",  GREEN if spy >= 0 else RED),
        (f"QQQ  {qqq:+.2f}%",  GREEN if qqq >= 0 else RED),
        (f"NQ   {nq:+.2f}%",   GREEN if nq  >= 0 else RED) if abs(nq) > 0.01
        else (f"VIX  {vix:.1f}",  RED if vix > 20 else DIM),
    ]
    for txt, color in index_rows:
        rb = PANEL if index_rows.index((txt,color)) % 2 == 0 else ALT
        d.rectangle([0, y, W, y + row_h], fill=rb)
        d.text((PAD + 16, y + row_h // 2), txt, font=fnt_md, fill=color, anchor="lm")
        y += row_h

    y += 10
    d.rectangle([0, y, W, y + 2], fill=(28, 40, 56))
    y += 18

    # ── Top Signal deep-dive ──────────────────────────────────────────────────
    d.text((PAD, y + 4), "TOP AI SIGNAL", font=fnt_sm, fill=AMBER)
    y += 58

    sig_h = max(200, (H - y - 80) // max(len(signals[:3]), 1))
    for sig in signals[:3]:
        bull = "bull" in sig.get("direction", "").lower()
        sc   = GREEN if bull else RED
        conf = sig.get("confidence", 70)
        t_data = next((t for t in hot if t["symbol"] == sig["symbol"]), None)
        pct    = t_data["pct"]   if t_data else 0
        price  = t_data["price"] if t_data else 0

        d.rectangle([0, y, W, y + sig_h], fill=PANEL if signals.index(sig) % 2 == 0 else ALT)
        d.rectangle([0, y, 6, y + sig_h], fill=sc)
        mid = y + sig_h // 2

        # Symbol + label
        d.text((PAD + 16, mid - 26), sig["symbol"], font=fnt_lg, fill=WHITE, anchor="lm")
        lbl = "BULLISH" if bull else "BEARISH"
        d.text((PAD + 16, mid + 28), f"AI: {lbl} — {conf:.0f}% confidence", font=fnt_xs, fill=sc, anchor="lm")

        # Price + pct on right
        if price > 0:
            d.text((W - PAD, mid - 26), f"${price:,.2f}", font=fnt_md, fill=WHITE, anchor="ra")
            sign = "+" if pct >= 0 else ""
            d.text((W - PAD, mid + 28), f"{sign}{pct:.2f}%", font=fnt_sm, fill=sc, anchor="ra")

        d.rectangle([0, y + sig_h - 1, W, y + sig_h], fill=(20, 28, 44))
        y += sig_h

    # Caption bar — readable on mute
    regime_s  = data.get("regime", "NEUTRAL")
    score_s   = data.get("regime_score", 50)
    emoji_s   = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "🟡"}.get(regime_s, "⚪")
    _draw_caption_bar(d, W, H, f"{emoji_s} Regime {regime_s} {score_s}/100  •  Drop 🟢 or 🔴 below 👇", fnt_xs, fnt_nano)

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

    # P&L block (big, visual)
    has_pnl = abs(pnl) >= 1 and trigger not in ("premarket",)
    pnl_h   = 280
    d.rectangle([0, y, W, y + pnl_h], fill=PANEL)
    d.rectangle([0, y, 8, y + pnl_h], fill=rc)
    if has_pnl:
        pc   = GREEN if pnl >= 0 else RED
        sign = "+" if pnl >= 0 else ""
        word = "PROFIT" if pnl >= 0 else "LOSS"
        d.text((W // 2, y + 60), f"TODAY'S {word}", font=fnt_sm, fill=DIM, anchor="mm")
        d.text((W // 2, y + 170), f"{sign}${abs(pnl):,.0f}", font=fnt_hero, fill=pc, anchor="mm")
    elif trigger == "premarket":
        d.text((W // 2, y + 60), "CAPITAL DEPLOYED", font=fnt_sm, fill=DIM, anchor="mm")
        d.text((W // 2, y + 150), f"${equity:,.0f}", font=fnt_xl, fill=WHITE, anchor="mm")
        d.text((W // 2, y + 230), "Paper trading account", font=fnt_xs, fill=DIM, anchor="mm")
    else:
        d.text((W // 2, y + 80), "ACCOUNT EQUITY", font=fnt_sm, fill=DIM, anchor="mm")
        d.text((W // 2, y + 175), f"${equity:,.0f}", font=fnt_xl, fill=WHITE, anchor="mm")
    y += pnl_h + 20

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

    # ── 6. P&L + CTA ──────────────────────────────────────────────────────────
    if trigger not in ("premarket",) and abs(pnl) >= 1:
        word = "up" if pnl > 0 else "down"
        parts.append(f"Paper trading P and L: {word} ${abs(pnl):,.0f} today on a ${data.get('equity', 100000):,.0f} account.")

    parts.append(
        "Follow Market Genie for the full trade setup and watchlist every single trading day — "
        "completely free, completely automated. Drop a green or red emoji in the comments — "
        "are you bullish or bearish right now?"
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
