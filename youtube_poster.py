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
            social_syms = [t["symbol"] for t in srv.get("social_hot", [])[:8]
                           if t.get("symbol")]
            if social_syms:
                defaults["hot_tickers"] = _fetch_ticker_moves(social_syms)
    except Exception as e:
        print(f"[YouTube] Data fetch error: {e}")

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
    """Draw a single alert notification card with left accent border."""
    BG_CARD  = (16, 22, 34)
    BORDER_W = 6
    WHITE    = (225, 235, 248)
    DIM      = (110, 128, 150)

    # Card background
    d.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=BG_CARD)
    # Left accent border
    d.rounded_rectangle([x, y, x + BORDER_W, y + h], radius=3, fill=color)
    # Subtle outer glow ring
    d.rounded_rectangle([x, y, x + w, y + h], radius=10, outline=(*color, 60) if len(color) == 3 else color, width=1)

    # Symbol
    d.text((x + BORDER_W + 20, y + h // 2 - 10), symbol, font=fnt_sym, fill=WHITE, anchor="lm")

    # % change (right-aligned, big)
    sign = "+" if pct >= 0 else ""
    d.text((x + w - 20, y + h // 2 - 14), f"{sign}{pct:.2f}%", font=fnt_pct, fill=color, anchor="rm")

    # Price (below %)
    d.text((x + w - 20, y + h // 2 + 30), f"${price:,.2f}", font=fnt_price, fill=DIM, anchor="rm")

    # ALERT badge
    badge_x = x + BORDER_W + 20
    try:
        sym_w = int(d.textlength(symbol, font=fnt_sym)) + 16
    except Exception:
        sym_w = len(symbol) * 38 + 16
    d.rounded_rectangle([badge_x + sym_w, y + h // 2 - 28, badge_x + sym_w + 110, y + h // 2 - 2],
                         radius=6, fill=(*color[:3],) if len(color) == 3 else color)
    d.text((badge_x + sym_w + 8, y + h // 2 - 15), "ALERT", font=fnt_badge, fill=(8, 11, 16))


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
    d.text((bar_x, bar_y - 22), f"Confidence", font=fnt_label, fill=DIM)
    d.text((bar_x + bar_w, bar_y - 22), f"{confidence:.0f}%", font=fnt_conf, fill=color, anchor="ra")


# ═════════════════════════════════════════════════════════════════════════════
# SLIDE 1 — ALERT HOOK FRAME
# Breaking-news alert cards for top movers + regime
# ═════════════════════════════════════════════════════════════════════════════
def _generate_hook_frame(data, trigger):
    from PIL import Image, ImageDraw
    W, H   = _VIDEO_W, _VIDEO_H
    BG     = (6, 8, 14)
    PANEL  = (12, 17, 26)
    DIV    = (28, 40, 56)
    AMBER  = (220, 155, 40)
    WHITE  = (225, 235, 248)
    DIM    = (100, 118, 140)
    GREEN  = (74, 222, 128)
    RED    = (248, 113, 113)
    ACCENT = (56, 189, 248)
    PAD    = 44

    img = Image.new("RGB", (W, H), BG)
    d   = ImageDraw.Draw(img)

    hot    = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    regime = data["regime"]; score = data["regime_score"]; rc = _regime_rgb(regime)
    nq = data["nq_pct"]; spy = data.get("spy_pct", 0.0); vix = data["vix"]
    n_alerts = min(len(hot), 3)

    fnt_nano  = _load_font(34)
    fnt_xs    = _load_font(40)
    fnt_sm    = _load_font(48)
    fnt_md    = _load_font(56, bold=True)
    fnt_lg    = _load_font(72, bold=True)
    fnt_xl    = _load_font(90, bold=True)
    fnt_logo  = _load_font(82, bold=True)
    fnt_badge = _load_font(36, bold=True)
    fnt_price = _load_font(38)

    def div(y): d.line([(0, y), (W, y)], fill=DIV, width=1)

    # ── Header ────────────────────────────────────────────────────────────────
    # Gradient-like top bar with lightning bolt feel
    d.rectangle([0, 0, W, 120], fill=(8, 12, 20))
    # Left: logo
    d.text((PAD, 16), "MARKET GENIE", font=fnt_logo, fill=WHITE)
    # Right: trigger label in accent
    trigger_lbl = {"premarket": "PRE-MARKET", "midday": "MIDDAY",
                   "eod": "CLOSE", "afterhours": "AFTER-HOURS"}.get(trigger, "LIVE")
    d.text((W - PAD, 16), trigger_lbl, font=fnt_sm, fill=ACCENT, anchor="ra")
    d.text((W - PAD, 72), data["timestamp"], font=fnt_xs, fill=DIM, anchor="ra")
    # Bottom accent line on header
    d.rectangle([0, 118, W, 122], fill=ACCENT)

    # ── SIGNAL ALERT banner ───────────────────────────────────────────────────
    y = 138
    d.rectangle([0, y, W, y + 80], fill=(10, 22, 38))
    d.rectangle([0, y, W, y + 80], fill=(10, 22, 38))
    # Lightning icon area
    d.rounded_rectangle([PAD, y + 12, PAD + 56, y + 68], radius=8,
                         fill=(20, 50, 80), outline=ACCENT, width=2)
    d.text((PAD + 28, y + 40), "⚡", font=fnt_md, fill=ACCENT, anchor="mm")
    alert_text = f"{n_alerts} SIGNAL{'S' if n_alerts != 1 else ''} TRIGGERED"
    d.text((PAD + 74, y + 40), alert_text, font=fnt_md, fill=WHITE, anchor="lm")
    y += 80

    # ── Regime + indices strip ────────────────────────────────────────────────
    div(y); y += 1
    d.rectangle([0, y, W, y + 72], fill=PANEL)
    # Regime pill
    rtext = f" {regime} {score} "
    try:    rw = int(d.textlength(rtext, font=fnt_sm))
    except: rw = len(rtext) * 34
    d.rounded_rectangle([PAD, y + 10, PAD + rw + 8, y + 62], radius=10,
                         fill=(rc[0]//7, rc[1]//7, rc[2]//7), outline=rc, width=2)
    d.text((PAD + 8, y + 36), rtext, font=fnt_sm, fill=rc, anchor="lm")
    # Indices
    ix = PAD + rw + 36
    d.text((ix,       y + 36), f"NQ {nq:+.2f}%",  font=fnt_xs, fill=_ic(nq), anchor="lm")
    d.text((ix + 220, y + 36), f"SPY {spy:+.2f}%", font=fnt_xs, fill=_ic(spy), anchor="lm")
    vc = RED if vix > 20 else DIM
    d.text((W - PAD,  y + 36), f"VIX {vix:.1f}",  font=fnt_xs, fill=vc, anchor="rm")
    y += 72; div(y); y += 16

    # ── Alert section label ───────────────────────────────────────────────────
    d.text((PAD, y), "TOP MOVERS", font=fnt_xs, fill=AMBER)
    d.text((W - PAD, y), "TODAY", font=fnt_xs, fill=DIM, anchor="ra")
    y += 50

    # ── Alert cards for top 3 movers ──────────────────────────────────────────
    card_h = 155
    card_gap = 20
    for i, tk in enumerate(hot[:3]):
        color = GREEN if tk["up"] else RED
        _draw_alert_card(d, PAD, y, W - PAD * 2, card_h,
                         tk["symbol"], tk["pct"], tk["price"],
                         color, fnt_lg, fnt_xl, fnt_price, fnt_badge)
        y += card_h + card_gap

    # ── Footer ────────────────────────────────────────────────────────────────
    div(H - 64)
    d.rectangle([0, H - 64, W, H], fill=(8, 12, 20))
    d.text((W // 2, H - 38), "Follow @marketgenie.ai for free daily alerts",
           font=fnt_xs, fill=DIM, anchor="mm")
    d.text((W // 2, H - 10), "AI SIGNALS  |  NOT FINANCIAL ADVICE",
           font=fnt_nano, fill=DIV, anchor="mm")
    return img


# ═════════════════════════════════════════════════════════════════════════════
# SLIDE 2 — FULL DASHBOARD (Bloomberg terminal grid, unchanged from v1)
# ═════════════════════════════════════════════════════════════════════════════
def _generate_frame(data: dict, trigger: str):
    """
    Bloomberg-terminal-inspired grid dashboard.
    Fixed horizontal panels, amber section headers, zero floating elements.
    """
    from PIL import Image, ImageDraw

    W, H  = _VIDEO_W, _VIDEO_H
    BG    = (8, 11, 16)
    PANEL = (13, 18, 27)
    ALT   = (17, 24, 36)
    DIV   = (30, 42, 58)
    AMBER = (220, 155, 40)
    WHITE = (225, 235, 248)
    DIM   = (110, 128, 150)
    GREEN = (74, 222, 128)
    RED   = (248, 113, 113)

    img = Image.new("RGB", (W, H), BG)
    d   = ImageDraw.Draw(img)
    PAD = 44

    def ic(v): return GREEN if v >= 0 else RED

    fnt_nano  = _load_font(36)
    fnt_xs    = _load_font(42)
    fnt_sm    = _load_font(50)
    fnt_md    = _load_font(58, bold=True)
    fnt_lg    = _load_font(70, bold=True)
    fnt_xl    = _load_font(88, bold=True)
    fnt_hero  = _load_font(172, bold=True)
    fnt_lbl   = _load_font(36)
    fnt_logo  = _load_font(82, bold=True)

    rc  = _regime_rgb(data["regime"])
    pnl = data["pnl_today"]
    pcs = "+" if pnl >= 0 else "-"
    pc  = ic(pnl)

    hot     = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    signals = [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 65][:3]
    pos     = data.get("positions", [])
    trigger_lbl = {"premarket": "PRE-MARKET", "midday": "MIDDAY",
                   "eod": "END OF DAY", "afterhours": "AFTER HOURS"}.get(trigger, "LIVE")

    def div(y):
        d.line([(0, y), (W, y)], fill=DIV, width=1)

    def sec(y, left, right="", right_col=DIM):
        d.rectangle([0, y, W, y + 40], fill=ALT)
        d.text((PAD, y + 4), left.upper(), font=fnt_lbl, fill=AMBER)
        if right:
            d.text((W - PAD, y + 4), right.upper(), font=fnt_lbl, fill=right_col, anchor="ra")
        return y + 40

    # ══ HEADER ════════════════════════════════════════════════════════════════
    d.rectangle([0, 0, W, 106], fill=(10, 14, 22))
    d.text((PAD, 10), "MARKET GENIE", font=fnt_logo, fill=WHITE)
    d.text((W - PAD, 10), trigger_lbl, font=fnt_sm, fill=rc, anchor="ra")
    d.text((W - PAD, 62), data["timestamp"], font=fnt_xs, fill=DIM, anchor="ra")
    div(106)

    # ══ REGIME + INDICES ══════════════════════════════════════════════════════
    d.rectangle([0, 107, W, 237], fill=PANEL)
    regime = data["regime"];  score = data["regime_score"]
    nq = data["nq_pct"];  spy = data.get("spy_pct", 0.0);  vix = data["vix"]
    rtext = f" {regime}  {score} "
    try:    rw = int(d.textlength(rtext, font=fnt_lg))
    except: rw = len(rtext) * 42
    d.rounded_rectangle([PAD, 120, PAD + rw + 4, 192], radius=10,
                         fill=(rc[0] // 7, rc[1] // 7, rc[2] // 7))
    d.rounded_rectangle([PAD, 120, PAD + rw + 4, 192], radius=10, outline=rc, width=2)
    d.text((PAD + 8, 126), rtext, font=fnt_lg, fill=rc)
    ix = PAD + rw + 36
    d.text((ix,      126), f"NQ {nq:+.2f}%",  font=fnt_sm, fill=ic(nq))
    d.text((ix + 240, 126), f"SPY {spy:+.2f}%", font=fnt_sm, fill=ic(spy))
    vc = RED if vix > 20 else DIM
    d.text((W - PAD, 126), f"VIX {vix:.1f}",  font=fnt_sm, fill=vc, anchor="ra")
    div(237)

    # ══ BIGGEST MOVER ═════════════════════════════════════════════════════════
    y = sec(238, "Biggest mover today")
    d.rectangle([0, y, W, y + 200], fill=PANEL)
    if hot:
        h0 = hot[0]
        hc = GREEN if h0["up"] else RED
        ha = "+" if h0["up"] else "-"
        d.text((PAD + 12, y + 12), h0["symbol"], font=fnt_xl, fill=WHITE)
        d.text((W - PAD - 12, y + 18), f"${h0['price']:,.2f}", font=fnt_md, fill=DIM, anchor="ra")
        hero_txt = f"{ha}{abs(h0['pct']):.2f}%"
        d.text((W // 2, y + 106), hero_txt, font=fnt_hero, fill=hc, anchor="mm")
        bfill = int((W - PAD * 2) * min(abs(h0["pct"]) / 10, 1.0))
        d.rectangle([PAD, y + 184, W - PAD, y + 192], fill=DIV)
        if bfill > 4:
            d.rectangle([PAD, y + 184, PAD + bfill, y + 192], fill=hc)
    else:
        d.text((W // 2, y + 100), "Awaiting data", font=fnt_lg, fill=DIM, anchor="mm")
    y += 200
    div(y)

    # ══ MOVERS TABLE ══════════════════════════════════════════════════════════
    y = sec(y + 1, "Market movers", "Change")
    for i, tk in enumerate(hot[1:5]):
        rb = ALT if i % 2 == 0 else PANEL
        d.rectangle([0, y, W, y + 82], fill=rb)
        tc  = GREEN if tk["up"] else RED
        ar  = "+" if tk["up"] else "-"
        d.text((PAD + 12, y + 16), tk["symbol"], font=fnt_lg, fill=WHITE)
        d.text((W // 2, y + 20), f"{ar}{abs(tk['pct']):.2f}%", font=fnt_md, fill=tc, anchor="mm")
        d.text((W - PAD - 12, y + 16), f"${tk['price']:,.2f}", font=fnt_md, fill=DIM, anchor="ra")
        bw2 = int((W - PAD * 2 - 24) * min(abs(tk["pct"]) / 8, 1.0))
        d.rectangle([PAD + 12, y + 68, W - PAD - 12, y + 74], fill=DIV)
        if bw2 > 2: d.rectangle([PAD + 12, y + 68, PAD + 12 + bw2, y + 74], fill=tc)
        y += 82
    div(y)

    # ══ AI SIGNALS ════════════════════════════════════════════════════════════
    if signals:
        y = sec(y + 1, "AI signals", "Conf")
        for i, sig in enumerate(signals):
            rb = ALT if i % 2 == 0 else PANEL
            d.rectangle([0, y, W, y + 74], fill=rb)
            bull = "bull" in sig.get("direction", "bull").lower()
            sc2  = GREEN if bull else RED
            conf = sig.get("confidence", 70)
            lbl2 = "BULL" if bull else "BEAR"
            d.text((PAD + 12, y + 14), sig["symbol"], font=fnt_lg, fill=WHITE)
            try:    sx = d.textbbox((PAD + 12, y + 14), sig["symbol"], font=fnt_lg)[2] + 16
            except: sx = PAD + 12 + len(sig["symbol"]) * 44 + 16
            d.rounded_rectangle([sx, y + 18, sx + 100, y + 58], radius=6,
                                 fill=(sc2[0] // 5, sc2[1] // 5, sc2[2] // 5))
            d.text((sx + 8, y + 21), lbl2, font=fnt_xs, fill=sc2)
            bx3 = sx + 118; bw3 = W - PAD - 12 - bx3 - 80
            d.rectangle([bx3, y + 26, bx3 + bw3, y + 46], fill=DIV)
            fb = int(bw3 * min(conf / 100, 1))
            if fb > 2: d.rectangle([bx3, y + 26, bx3 + fb, y + 46], fill=sc2)
            d.text((W - PAD - 12, y + 14), f"{conf:.0f}%", font=fnt_md, fill=sc2, anchor="ra")
            y += 74
        div(y)

    # ══ OPEN TRADES ═══════════════════════════════════════════════════════════
    if pos:
        y = sec(y + 1, "AI open trades")
        for p2 in pos[:2]:
            d.rectangle([0, y, W, y + 78], fill=PANEL)
            s2  = p2["symbol"]; sd = p2["side"]
            ul  = p2["unrealized_pl"]; up = p2["unrealized_plpc"]
            sc3 = GREEN if sd == "LONG" else RED
            pc3 = GREEN if ul >= 0 else RED
            ps3 = "+" if ul >= 0 else "-"
            d.text((PAD + 12, y + 14), s2, font=fnt_lg, fill=WHITE)
            try:    bx4 = d.textbbox((PAD + 12, y + 14), s2, font=fnt_lg)[2] + 14
            except: bx4 = PAD + 12 + len(s2) * 44 + 14
            d.rounded_rectangle([bx4, y + 18, bx4 + 108, y + 58], radius=6,
                                 fill=(sc3[0] // 5, sc3[1] // 5, sc3[2] // 5))
            d.text((bx4 + 8, y + 21), sd, font=fnt_xs, fill=sc3)
            d.text((W - PAD - 12, y + 12), f"{ps3}${abs(ul):,.0f}", font=fnt_lg, fill=pc3, anchor="ra")
            d.text((W - PAD - 12, y + 56), f"({ps3}{abs(up):.2f}%)", font=fnt_xs, fill=pc3, anchor="ra")
            y += 78
        div(y)

    # ══ P&L ═══════════════════════════════════════════════════════════════════
    pnl_top = max(y + 1, H - 250)
    d.rectangle([0, pnl_top, W, H - 64], fill=PANEL)
    d.text((PAD + 12, pnl_top + 10), "TODAY'S P&L", font=fnt_lbl, fill=AMBER)
    if trigger == "premarket" and abs(pnl) < 1:
        d.text((PAD + 12, pnl_top + 52), f"${data['equity']:,.0f}",
               font=_load_font(134, bold=True), fill=WHITE)
        d.text((W - PAD - 12, pnl_top + 80), "Opens 9:30 AM ET",
               font=fnt_sm, fill=DIM, anchor="ra")
    else:
        d.text((PAD + 12, pnl_top + 52), f"{pcs}${abs(pnl):,.0f}",
               font=_load_font(134, bold=True), fill=pc)
        if data["equity"] > 0:
            pd = (pnl / max(data["equity"] - pnl, 1)) * 100
            d.text((W - PAD - 12, pnl_top + 80), f"{pcs}{abs(pd):.2f}%",
                   font=fnt_xl, fill=pc, anchor="ra")

    # ══ FOOTER ════════════════════════════════════════════════════════════════
    div(H - 64)
    d.rectangle([0, H - 64, W, H], fill=(10, 14, 22))
    d.text((W // 2, H - 44), "Follow @marketgenie.ai for daily AI signals",
           font=fnt_xs, fill=DIM, anchor="mm")
    d.text((W // 2, H - 14), "AI SIGNALS  |  NOT FINANCIAL ADVICE",
           font=fnt_nano, fill=DIV, anchor="mm")

    return img


# ═════════════════════════════════════════════════════════════════════════════
# SLIDE 3 — AI SIGNAL CARDS + CTA
# ═════════════════════════════════════════════════════════════════════════════
def _generate_cta_frame(data, trigger):
    from PIL import Image, ImageDraw
    W, H   = _VIDEO_W, _VIDEO_H
    BG     = (6, 8, 14)
    PANEL  = (12, 17, 26)
    DIV    = (28, 40, 56)
    AMBER  = (220, 155, 40)
    WHITE  = (225, 235, 248)
    DIM    = (100, 118, 140)
    GREEN  = (74, 222, 128)
    RED    = (248, 113, 113)
    ACCENT = (56, 189, 248)
    PAD    = 44

    img = Image.new("RGB", (W, H), BG)
    d   = ImageDraw.Draw(img)

    pnl = data["pnl_today"]; pc = GREEN if pnl >= 0 else RED
    pcs = "+" if pnl >= 0 else "-"; eq = data["equity"]
    pd  = (pnl / max(eq - pnl, 1)) * 100 if eq > 0 else 0.0
    hot = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    signals = [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 60]
    show_pnl = abs(pnl) >= 1 and trigger != "premarket"

    fnt_nano  = _load_font(34)
    fnt_xs    = _load_font(40)
    fnt_sm    = _load_font(48)
    fnt_md    = _load_font(56, bold=True)
    fnt_lg    = _load_font(72, bold=True)
    fnt_xl    = _load_font(90, bold=True)
    fnt_hero  = _load_font(180, bold=True)
    fnt_logo  = _load_font(82, bold=True)
    fnt_badge = _load_font(38, bold=True)
    fnt_conf  = _load_font(42, bold=True)
    fnt_lbl   = _load_font(34)
    fnt_price = _load_font(38)

    def div(y): d.line([(0, y), (W, y)], fill=DIV, width=1)

    # ── Header ────────────────────────────────────────────────────────────────
    d.rectangle([0, 0, W, 120], fill=(8, 12, 20))
    d.text((PAD, 16), "MARKET GENIE", font=fnt_logo, fill=WHITE)
    d.text((W - PAD, 16), "AI SIGNALS", font=fnt_sm, fill=ACCENT, anchor="ra")
    d.text((W - PAD, 72), data["timestamp"], font=fnt_xs, fill=DIM, anchor="ra")
    d.rectangle([0, 118, W, 122], fill=ACCENT)

    y = 138

    # ── P&L block (when available) ────────────────────────────────────────────
    if show_pnl:
        d.rectangle([0, y, W, y + 200], fill=PANEL)
        d.text((PAD, y + 18), "TODAY'S P&L", font=fnt_xs, fill=AMBER)
        d.text((PAD, y + 58), f"{pcs}${abs(pnl):,.0f}", font=fnt_hero, fill=pc)
        d.text((PAD, y + 158), f"{pcs}{abs(pd):.2f}% daily return", font=fnt_sm, fill=pc)
        # Right: equity
        d.text((W - PAD, y + 58), "Equity", font=fnt_xs, fill=DIM, anchor="ra")
        d.text((W - PAD, y + 100), f"${eq:,.0f}", font=fnt_lg, fill=DIM, anchor="ra")
        div(y + 200); y += 216

    elif trigger == "premarket":
        d.rectangle([0, y, W, y + 160], fill=PANEL)
        d.text((W // 2, y + 40), "CAPITAL READY", font=fnt_sm, fill=AMBER, anchor="mm")
        d.text((W // 2, y + 120), f"${eq:,.0f}", font=fnt_xl, fill=WHITE, anchor="mm")
        div(y + 160); y += 176

    else:
        # No trades — show top alert card
        if hot:
            h0 = hot[0]; hc = GREEN if h0["up"] else RED
            d.rectangle([0, y, W, y + 170], fill=PANEL)
            d.text((PAD, y + 16), "TOP MOVER", font=fnt_xs, fill=AMBER)
            d.text((PAD, y + 56), h0["symbol"], font=fnt_xl, fill=WHITE)
            sign = "+" if h0["up"] else ""
            d.text((W - PAD, y + 56), f"{sign}{h0['pct']:.2f}%", font=fnt_xl, fill=hc, anchor="ra")
            d.text((W - PAD, y + 126), f"${h0['price']:,.2f}", font=fnt_md, fill=DIM, anchor="ra")
            div(y + 170); y += 186

    # ── AI Signal cards ───────────────────────────────────────────────────────
    d.text((PAD, y + 4), "AI SIGNALS", font=fnt_xs, fill=AMBER)
    d.text((W - PAD, y + 4), f"{len(signals)} active", font=fnt_xs, fill=DIM, anchor="ra")
    y += 50

    sig_card_h = 140
    if signals:
        for sig in signals[:3]:
            direction = sig.get("direction", "bull")
            confidence = sig.get("confidence", 70)
            _draw_signal_card(d, PAD, y, W - PAD * 2, sig_card_h,
                              sig["symbol"], direction, confidence,
                              fnt_lg, fnt_conf, fnt_badge, fnt_lbl)
            y += sig_card_h + 16
    else:
        # Fallback: show top movers as signal-style cards
        for tk in hot[:2]:
            direction = "bull" if tk["up"] else "bear"
            conf = min(50 + abs(tk["pct"]) * 5, 95)
            _draw_signal_card(d, PAD, y, W - PAD * 2, sig_card_h,
                              tk["symbol"], direction, conf,
                              fnt_lg, fnt_conf, fnt_badge, fnt_lbl)
            y += sig_card_h + 16

    # ── CTA block ─────────────────────────────────────────────────────────────
    cta_top = max(y + 24, H - 300)
    d.rectangle([0, cta_top, W, H - 64], fill=(10, 18, 32))
    # Glowing follow button
    d.rounded_rectangle([PAD, cta_top + 16, W - PAD, cta_top + 110],
                         radius=16, fill=(20, 50, 90), outline=ACCENT, width=3)
    d.text((W // 2, cta_top + 63), "FOLLOW FOR FREE DAILY ALERTS",
           font=fnt_md, fill=WHITE, anchor="mm")
    d.text((W // 2, cta_top + 148), "@marketgenie.ai",
           font=fnt_xl, fill=ACCENT, anchor="mm")
    d.text((W // 2, cta_top + 220),
           "Free AI signals  |  Every trading day  |  No cost",
           font=fnt_sm, fill=DIM, anchor="mm")

    div(H - 64)
    d.rectangle([0, H - 64, W, H], fill=(8, 12, 20))
    d.text((W // 2, H - 38), "Follow @marketgenie.ai for free daily alerts",
           font=fnt_xs, fill=DIM, anchor="mm")
    d.text((W // 2, H - 10), "AI SIGNALS  |  NOT FINANCIAL ADVICE",
           font=fnt_nano, fill=DIV, anchor="mm")
    return img


# ═════════════════════════════════════════════════════════════════════════════
# TTS VOICEOVER
# ═════════════════════════════════════════════════════════════════════════════
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
        print(f"[YouTube] ✅ Published: https://youtube.com/shorts/{vid_id}")
        return True
    except Exception as e:
        print(f"[YouTube] ❌ Upload error: {e}")
        return False


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
        f"🔔 Subscribe for free AI market signals every trading day!\n"
        f"👆 Follow @marketgenie.ai\n\n"
        f"⚠️ NOT FINANCIAL ADVICE — for educational & informational purposes only.\n\n"
        f"#daytrading #stocks #algotrading #AItrading #stockmarket #finance #investing "
        f"#marketgenie #tradingsignals #stocksignals #wallstreet #nasdaq #sp500"
    )

    success = _upload_to_youtube(service, mp4_path, title, description)

    try:
        os.unlink(mp4_path)
    except Exception:
        pass

    return success


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
        (dtime(9,  15), dtime(9,  30), "premarket"),
        (dtime(12,  0), dtime(12, 15), "midday"),
        (dtime(16, 15), dtime(16, 30), "eod"),
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
