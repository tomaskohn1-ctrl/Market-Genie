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
    fnt_logo  = _load_font(62, bold=True)
    fnt_badge = _load_font(36, bold=True)
    fnt_price = _load_font(38)

    def div(y): d.line([(0, y), (W, y)], fill=DIV, width=1)

    # ── Header ────────────────────────────────────────────────────────────────
    # Gradient-like top bar with lightning bolt feel
    HDR = 148
    d.rectangle([0, 0, W, HDR], fill=(8, 12, 20))
    d.text((PAD, 12), "MARKET GENIE", font=fnt_logo, fill=WHITE)
    trigger_lbl = {"premarket": "PRE-MARKET", "midday": "MIDDAY",
                   "eod": "CLOSE", "afterhours": "AFTER-HOURS"}.get(trigger, "LIVE")
    d.text((W - PAD, 12), trigger_lbl, font=fnt_sm, fill=ACCENT, anchor="ra")
    d.text((PAD, 96), data["timestamp"], font=fnt_xs, fill=DIM)
    d.rectangle([0, HDR - 3, W, HDR], fill=ACCENT)

    # ── SIGNAL ALERT banner ───────────────────────────────────────────────────
    y = HDR + 14
    d.rectangle([0, y, W, y + 80], fill=(10, 22, 38))
    d.rectangle([0, y, W, y + 80], fill=(10, 22, 38))
    # Lightning icon area
    d.rounded_rectangle([PAD, y + 12, PAD + 56, y + 68], radius=8,
                         fill=(20, 50, 80), outline=ACCENT, width=2)
    d.text((PAD + 28, y + 40), "⚡", font=fnt_md, fill=ACCENT, anchor="mm")
    alert_text = f"{n_alerts} SIGNAL{'S' if n_alerts != 1 else ''} TRIGGERED"
    d.text((PAD + 74, y + 40), alert_text, font=fnt_md, fill=WHITE, anchor="lm")
    y += 80

    # ── Regime + indices — 2-row layout ─────────────────────────────────────
    div(y); y += 1
    d.rectangle([0, y, W, y + 120], fill=PANEL)
    rtext = f" {regime} {score} "
    try:    rw = int(d.textlength(rtext, font=fnt_sm))
    except: rw = len(rtext) * 34
    # Row 1: regime pill (left) + VIX (right)
    d.rounded_rectangle([PAD, y + 8, PAD + rw + 8, y + 62], radius=10,
                         fill=(rc[0]//7, rc[1]//7, rc[2]//7), outline=rc, width=2)
    d.text((PAD + 8, y + 35), rtext, font=fnt_sm, fill=rc, anchor="lm")
    vc = RED if vix > 20 else DIM
    d.text((W - PAD, y + 35), f"VIX {vix:.1f}", font=fnt_sm, fill=vc, anchor="rm")
    # Row 2: NQ, SPY, QQQ evenly spaced
    qqq = data.get("qqq_pct", 0.0)
    step2 = (W - PAD * 2) // 3
    d.text((PAD,           y + 88), f"NQ  {nq:+.2f}%",  font=fnt_xs, fill=_ic(nq))
    d.text((PAD + step2,   y + 88), f"SPY {spy:+.2f}%", font=fnt_xs, fill=_ic(spy))
    d.text((PAD + step2*2, y + 88), f"QQQ {qqq:+.2f}%", font=fnt_xs, fill=_ic(qqq))
    y += 120; div(y); y += 12

    # ── Dynamic sizing: movers + signals fill the full frame ────────────────
    signals_h  = [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 60][:3]
    fnt_conf   = _load_font(44, bold=True)
    fnt_lbl    = _load_font(34)
    FOOTER_H   = 64
    LBL_H      = 52
    GAP        = 14
    n_mov      = min(len(hot), 3)
    n_sig      = len(signals_h)
    n_sections = (1 if n_mov else 0) + (1 if n_sig else 0)
    n_cards    = n_mov + n_sig
    avail = H - y - FOOTER_H - n_sections * (LBL_H + 8) - max(n_cards - 1, 0) * GAP
    card_h = max(140, avail // n_cards) if n_cards else 180

    # Mover alert cards
    if n_mov:
        d.text((PAD, y + 8), "TOP MOVERS", font=fnt_xs, fill=AMBER)
        d.text((W - PAD, y + 8), "TODAY", font=fnt_xs, fill=DIM, anchor="ra")
        y += LBL_H + 8
        for tk in hot[:n_mov]:
            color = GREEN if tk["up"] else RED
            _draw_alert_card(d, PAD, y, W - PAD * 2, card_h,
                             tk["symbol"], tk["pct"], tk["price"],
                             color, fnt_lg, fnt_xl, fnt_price, fnt_badge)
            y += card_h + GAP

    # AI signal cards
    if n_sig:
        div(y); y += 1
        d.text((PAD, y + 8), "AI SIGNALS", font=fnt_xs, fill=AMBER)
        d.text((W - PAD, y + 8), f"{n_sig} ACTIVE", font=fnt_xs, fill=DIM, anchor="ra")
        y += LBL_H + 8
        for sig in signals_h:
            _draw_signal_card(d, PAD, y, W - PAD * 2, card_h,
                              sig["symbol"], sig.get("direction", "bull"),
                              sig.get("confidence", 70),
                              fnt_lg, fnt_conf, fnt_badge, fnt_lbl)
            y += card_h + GAP

    # Footer
    div(H - FOOTER_H)
    d.rectangle([0, H - FOOTER_H, W, H], fill=(8, 12, 20))
    # Footer: show top AI signal tickers for social proof
    sig_footer = [s for s in data.get("ai_signals",[]) if s.get("confidence",0)>60][:4]
    if sig_footer:
        parts = []  
        for s in sig_footer:
            bull = "bull" in s.get("direction","bull").lower()
            parts.append(f"{'BULL' if bull else 'BEAR'} {s['symbol']} {s.get('confidence',70):.0f}%")
        d.text((W//2, H-38), "  |  ".join(parts), font=fnt_nano, fill=DIM, anchor="mm")
    else:
        d.text((W//2, H-38), f"VIX {data.get('vix',16.5):.1f}  |  P/C {data.get('put_call_ratio',1.0):.2f}  |  Score {data.get('regime_score',50)}", font=fnt_nano, fill=DIM, anchor="mm")
    d.text((W//2, H-10), "AI SIGNALS  |  NOT FINANCIAL ADVICE", font=fnt_nano, fill=DIV, anchor="mm")
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
    fnt_logo  = _load_font(62, bold=True)

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
    d.rectangle([0, 0, W, 148], fill=(10, 14, 22))
    d.text((PAD, 12), "MARKET GENIE", font=fnt_logo, fill=WHITE)
    d.text((W - PAD, 12), trigger_lbl, font=fnt_md, fill=rc, anchor="ra")
    d.text((PAD, 96), data["timestamp"], font=fnt_xs, fill=DIM)
    d.rectangle([0, 145, W, 148], fill=rc)
    div(148)

    # ══ REGIME + INDICES ══════════════════════════════════════════════════════
    d.rectangle([0, 149, W, 310], fill=PANEL)
    regime = data["regime"];  score = data["regime_score"]
    nq = data["nq_pct"];  spy = data.get("spy_pct", 0.0);  vix = data["vix"]
    rtext = f" {regime} {score} "
    try:    rw = int(d.textlength(rtext, font=fnt_md))
    except: rw = len(rtext) * 36
    d.rounded_rectangle([PAD, 162, PAD + rw + 4, 218], radius=10,
                         fill=(rc[0] // 7, rc[1] // 7, rc[2] // 7))
    d.rounded_rectangle([PAD, 162, PAD + rw + 4, 220], radius=10, outline=rc, width=2)
    d.text((PAD + 8, 168), rtext, font=fnt_md, fill=rc)
    # Indices on 2nd row — 3 evenly spaced columns
    vc = RED if vix > 20 else DIM
    col3 = (W - PAD * 2) // 3
    idx_y = 236
    d.text((PAD,              idx_y), f"NQ {nq:+.2f}%",  font=fnt_sm, fill=ic(nq))
    d.text((PAD + col3,       idx_y), f"SPY {spy:+.2f}%", font=fnt_sm, fill=ic(spy))
    d.text((PAD + col3 * 2,   idx_y), f"VIX {vix:.1f}",  font=fnt_sm, fill=vc)
    div(310)

    # ── Dynamic heights to fill the full 1920px frame ───────────────────────
    n_movers  = len(hot[1:5])
    n_sigs    = len(signals)
    PNL_H     = 190
    FOOTER_H  = 64
    SEC_H     = 40
    n_secs    = 2 + (1 if n_sigs else 0)
    avail     = H - 238 - n_secs * SEC_H - PNL_H - FOOTER_H
    HERO_H    = max(200, int(avail * 0.32))
    MOV_ROW   = max(82,  int(avail * 0.38 / max(n_movers, 1)))
    SIG_ROW   = max(96,  int(avail * 0.30 / max(n_sigs, 1))) if n_sigs else 96

    # ══ BIGGEST MOVER ════════════════════════════════════════════════════════
    y = sec(320, "Biggest mover today")
    d.rectangle([0, y, W, y + HERO_H], fill=PANEL)
    if hot:
        h0 = hot[0]; hc = GREEN if h0["up"] else RED; ha = "+" if h0["up"] else "-"
        d.rectangle([0, y, 8, y + HERO_H], fill=hc)
        d.text((PAD + 12, y + 20), h0["symbol"], font=fnt_xl, fill=WHITE)
        d.text((W - PAD - 12, y + 24), f"${h0['price']:,.2f}", font=fnt_md, fill=DIM, anchor="ra")
        hero_sz  = min(190, max(80, HERO_H - 90))
        hero_fnt = _load_font(hero_sz, bold=True)
        d.text((W // 2, y + HERO_H // 2 + 14), f"{ha}{abs(h0['pct']):.2f}%",
               font=hero_fnt, fill=hc, anchor="mm")
        bfill = int((W - PAD * 2) * min(abs(h0["pct"]) / 10, 1.0))
        d.rectangle([PAD, y + HERO_H - 14, W - PAD, y + HERO_H - 6], fill=DIV)
        if bfill > 4:
            d.rectangle([PAD, y + HERO_H - 14, PAD + bfill, y + HERO_H - 6], fill=hc)
    else:
        d.text((W // 2, y + HERO_H // 2), "Awaiting data", font=fnt_lg, fill=DIM, anchor="mm")
    y += HERO_H; div(y)

    # ══ MOVERS TABLE ══════════════════════════════════════════════════════════
    y = sec(y + 1, "Market movers", "Change")
    for i, tk in enumerate(hot[1:5]):
        rb = ALT if i % 2 == 0 else PANEL
        d.rectangle([0, y, W, y + MOV_ROW], fill=rb)
        tc = GREEN if tk["up"] else RED; ar = "+" if tk["up"] else "-"
        d.rectangle([0, y, 5, y + MOV_ROW], fill=tc)  # direction accent bar
        mid = y + MOV_ROW // 2 - 10
        d.text((PAD + 12, mid), tk["symbol"], font=fnt_lg, fill=WHITE)
        d.text((W // 2,   mid), f"{ar}{abs(tk['pct']):.2f}%", font=fnt_md, fill=tc, anchor="mm")
        d.text((W - PAD - 12, mid), f"${tk['price']:,.2f}", font=fnt_md, fill=DIM, anchor="ra")
        bar_y = y + MOV_ROW - 14
        bw2 = int((W - PAD * 2 - 24) * min(abs(tk["pct"]) / 8, 1.0))
        d.rectangle([PAD + 12, bar_y, W - PAD - 12, bar_y + 6], fill=DIV)
        if bw2 > 2: d.rectangle([PAD + 12, bar_y, PAD + 12 + bw2, bar_y + 6], fill=tc)
        y += MOV_ROW
    div(y)

    # ══ AI SIGNALS ════════════════════════════════════════════════════════════
    if signals:
        y = sec(y + 1, "AI signals")
        for i, sig in enumerate(signals):
            rb = ALT if i % 2 == 0 else PANEL
            ROW = SIG_ROW
            d.rectangle([0, y, W, y + ROW], fill=rb)
            bull = "bull" in sig.get("direction", "bull").lower()
            sc2  = GREEN if bull else RED
            conf = sig.get("confidence", 70)
            lbl2 = "BULL" if bull else "BEAR"

            # Left accent border
            d.rectangle([0, y, 6, y + ROW], fill=sc2)

            # Symbol — fixed left column
            d.text((PAD + 12, y + ROW // 2 - 8), sig["symbol"], font=fnt_lg, fill=WHITE, anchor="lm")

            # BULL/BEAR badge — next to symbol
            try:    sym_end = d.textbbox((PAD + 12, y), sig["symbol"], font=fnt_lg)[2] + 20
            except: sym_end = PAD + 12 + len(sig["symbol"]) * 42 + 20
            badge_w = 110
            d.rounded_rectangle([sym_end, y + ROW//2 - 22, sym_end + badge_w, y + ROW//2 + 22],
                                 radius=8, fill=(sc2[0]//5, sc2[1]//5, sc2[2]//5),
                                 outline=sc2, width=2)
            d.text((sym_end + badge_w // 2, y + ROW//2), lbl2, font=fnt_xs, fill=sc2, anchor="mm")

            # Confidence % — right side, large and clear
            conf_x = W - PAD - 12
            d.text((conf_x, y + ROW//2 - 28), f"{conf:.0f}%", font=fnt_md, fill=sc2, anchor="ra")

            # Confidence bar — between badge and % number, with clear padding
            bar_x1 = sym_end + badge_w + 16
            try:    conf_w = int(d.textlength(f"{conf:.0f}%", font=fnt_md)) + 20
            except: conf_w = 100
            bar_x2 = conf_x - conf_w - 16
            bar_y  = y + ROW - 34
            if bar_x2 > bar_x1 + 20:
                d.rounded_rectangle([bar_x1, bar_y, bar_x2, bar_y + 16], radius=6, fill=DIV)
                fb = int((bar_x2 - bar_x1) * min(conf / 100, 1.0))
                if fb > 4:
                    d.rounded_rectangle([bar_x1, bar_y, bar_x1 + fb, bar_y + 16], radius=6, fill=sc2)

            y += ROW
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

    # ══ P&L — only show when real, otherwise show follow CTA ═════════════════
    pnl_top = y + 1  # flows directly after signals — no gap
    d.rectangle([0, pnl_top, W, H - 64], fill=PANEL)
    has_pnl = abs(pnl) >= 1 and trigger != "premarket"
    if has_pnl:
        d.text((PAD + 12, pnl_top + 10), "TODAY'S P&L", font=fnt_lbl, fill=AMBER)
        d.text((PAD + 12, pnl_top + 52), f"{pcs}${abs(pnl):,.0f}",
               font=_load_font(134, bold=True), fill=pc)
        if data["equity"] > 0:
            pd_val = (pnl / max(data["equity"] - pnl, 1)) * 100
            d.text((W - PAD - 12, pnl_top + 80), f"{pcs}{abs(pd_val):.2f}%",
                   font=fnt_xl, fill=pc, anchor="ra")
    elif trigger == "premarket":
        d.text((PAD + 12, pnl_top + 10), "CAPITAL READY", font=fnt_lbl, fill=AMBER)
        d.text((PAD + 12, pnl_top + 52), f"${data['equity']:,.0f}",
               font=_load_font(134, bold=True), fill=WHITE)
        d.text((W - PAD - 12, pnl_top + 80), "Opens 9:30 AM ET",
               font=fnt_sm, fill=DIM, anchor="ra")
    else:
        # No P&L — show follow CTA in this space
        ACCENT = (56, 189, 248)
        # Show VIX / P&C / regime score boxes instead of CTA
        bw3 = (W - PAD * 2 - 24) // 3
        snap_items = [
            ("VIX", f"{data.get('vix', 0):.1f}", RED if data.get('vix', 20) > 20 else GREEN),
            ("P/C", f"{data.get('put_call_ratio', 0):.2f}", RED if data.get('put_call_ratio', 1) > 1.2 else GREEN),
            ("SCORE", f"{data.get('regime_score', 50):.0f}", ACCENT),
        ]
        for ci3, (lbl3, val3, vc3) in enumerate(snap_items):
            cx3 = PAD + ci3 * (bw3 + 12)
            d.rounded_rectangle([cx3, pnl_top + 12, cx3 + bw3, pnl_top + 160],
                                 radius=10, fill=PANEL)
            d.text((cx3 + 14, pnl_top + 22), lbl3, font=fnt_sm, fill=DIM)
            d.text((cx3 + 14, pnl_top + 72), val3, font=fnt_xl, fill=vc3)

    # ══ FOOTER ════════════════════════════════════════════════════════════════
    div(H - 64)
    d.rectangle([0, H - 64, W, H], fill=(10, 14, 22))
    sig_f = [s for s in signals if s.get("confidence",0)>60][:4]
    if sig_f:
        parts_f = []
        for s in sig_f:
            bull = "bull" in s.get("direction","bull").lower()
            parts_f.append(f"{'BULL' if bull else 'BEAR'} {s['symbol']} {s.get('confidence',70):.0f}%")
        d.text((W//2, H-44), "  |  ".join(parts_f), font=fnt_nano, fill=DIM, anchor="mm")
    else:
        d.text((W//2, H-44), f"VIX {data.get('vix',16.5):.1f}  |  P/C {data.get('put_call_ratio',1.0):.2f}  |  Score {data.get('regime_score',50)}", font=fnt_nano, fill=DIM, anchor="mm")
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
    fnt_logo  = _load_font(62, bold=True)
    fnt_badge = _load_font(38, bold=True)
    fnt_conf  = _load_font(42, bold=True)
    fnt_lbl   = _load_font(34)
    fnt_price = _load_font(38)

    def div(y): d.line([(0, y), (W, y)], fill=DIV, width=1)

    # ── Header ────────────────────────────────────────────────────────────────
    HDR = 148
    d.rectangle([0, 0, W, HDR], fill=(8, 12, 20))
    d.text((PAD, 12), "MARKET GENIE", font=fnt_logo, fill=WHITE)
    d.text((W - PAD, 12), "AI SIGNALS", font=fnt_sm, fill=ACCENT, anchor="ra")
    d.text((PAD, 96), data["timestamp"], font=fnt_xs, fill=DIM)
    d.rectangle([0, HDR - 3, W, HDR], fill=ACCENT)

    y = HDR + 14

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

    sig_card_h = 210
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

    # ── Live market snapshot — fills ALL remaining space ────────────────────
    snap_top = y + 8
    snap_bot = H - 64
    remaining_h = snap_bot - snap_top

    # Section header
    d.text((PAD, snap_top + 10), "LIVE MARKET SNAPSHOT", font=fnt_xs, fill=AMBER)
    sy = snap_top + 54

    # VIX / P&C / Regime score row
    mdata = [
        ("VIX", f"{data.get('vix', 0):.1f}", RED if data.get('vix', 20) > 20 else GREEN),
        ("P/C", f"{data.get('put_call_ratio', 0):.2f}", RED if data.get('put_call_ratio', 1) > 1.2 else GREEN),
        ("SCORE", f"{data.get('regime_score', 50):.0f}", ACCENT),
    ]
    col_w = (W - PAD * 2) // 3
    for ci, (lbl, val, vc) in enumerate(mdata):
        cx = PAD + ci * col_w
        d.rounded_rectangle([cx, sy, cx + col_w - 12, sy + 140], radius=10, fill=PANEL)
        d.text((cx + 10, sy + 8), lbl, font=fnt_xs, fill=DIM)
        d.text((cx + 10, sy + 46), val, font=fnt_md, fill=vc)
    sy += 156

    div(sy); sy += 12

    # Top 2 extra signals not already shown
    extra_sigs = [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 55]
    shown_syms = {sig["symbol"] for sig in signals[:3]}
    extra = [s for s in extra_sigs if s["symbol"] not in shown_syms][:2]
    if extra:
        d.text((PAD, sy + 2), "MORE SIGNALS", font=fnt_xs, fill=AMBER)
        sy += 46
        extra_h = min(190, (snap_bot - sy - 16) // len(extra))
        for sig in extra:
            direction = sig.get("direction", "bull")
            confidence = sig.get("confidence", 70)
            _draw_signal_card(d, PAD, sy, W - PAD * 2, extra_h,
                              sig["symbol"], direction, confidence,
                              fnt_md, fnt_conf, fnt_badge, fnt_lbl)
            sy += extra_h + 12
    elif hot:
        # Show 2 more movers if no extra signals
        d.text((PAD, sy + 2), "MORE MOVERS", font=fnt_xs, fill=AMBER)
        sy += 46
        for tk in hot[1:3]:
            if sy + 100 > snap_bot: break
            hc2 = GREEN if tk["up"] else RED
            d.rectangle([0, sy, W, sy + 140], fill=PANEL)
            d.rectangle([0, sy, 6, sy + 140], fill=hc2)
            d.text((PAD + 12, sy + 24), tk["symbol"], font=fnt_xl, fill=WHITE)
            sign = "+" if tk["up"] else ""
            d.text((W - PAD, sy + 24), f"{sign}{tk['pct']:.2f}%", font=fnt_xl, fill=hc2, anchor="ra")
            d.text((W - PAD, sy + 88), f"${tk['price']:,.2f}", font=fnt_md, fill=DIM, anchor="ra")
            sy += 152

    div(H - 64)
    d.rectangle([0, H - 64, W, H], fill=(8, 12, 20))
    sig_f2 = [s for s in data.get("ai_signals",[]) if s.get("confidence",0)>60][:4]
    if sig_f2:
        parts_f2 = []
        for s in sig_f2:
            bull = "bull" in s.get("direction","bull").lower()
            parts_f2.append(f"{'BULL' if bull else 'BEAR'} {s['symbol']} {s.get('confidence',70):.0f}%")
        d.text((W//2, H-38), "  |  ".join(parts_f2), font=fnt_nano, fill=DIM, anchor="mm")
    else:
        d.text((W//2, H-38), f"VIX {data.get('vix',16.5):.1f}  |  P/C {data.get('put_call_ratio',1.0):.2f}  |  Score {data.get('regime_score',50)}", font=fnt_nano, fill=DIM, anchor="mm")
    d.text((W // 2, H - 10), "AI SIGNALS  |  NOT FINANCIAL ADVICE", font=fnt_nano, fill=DIV, anchor="mm")
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
        (dtime(9,  15), dtime(9,  30), "premarket"),
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
