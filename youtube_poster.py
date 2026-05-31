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
                if price > 0:
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


# ═════════════════════════════════════════════════════════════════════════════
# SLIDE 1 — HOOK FRAME
# Bold opener: regime pill, top mover giant %, timestamp
# ═════════════════════════════════════════════════════════════════════════════
def _generate_hook_frame(data: dict, trigger: str):
    from PIL import Image, ImageDraw

    W, H  = _VIDEO_W, _VIDEO_H
    BG    = (8, 11, 16)
    PANEL = (13, 18, 27)
    DIV   = (30, 42, 58)
    AMBER = (220, 155, 40)
    WHITE = (225, 235, 248)
    DIM   = (110, 128, 150)
    GREEN = (74, 222, 128)
    RED   = (248, 113, 113)
    ACCENT = (56, 189, 248)   # sky blue accent for hook

    img = Image.new("RGB", (W, H), BG)
    d   = ImageDraw.Draw(img)
    PAD = 56

    hot    = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    regime = data["regime"]
    score  = data["regime_score"]
    rc     = _regime_rgb(regime)

    trigger_lbl = {
        "premarket":  "PRE-MARKET BRIEF",
        "midday":     "MIDDAY UPDATE",
        "eod":        "END OF DAY",
        "afterhours": "AFTER-HOURS WRAP",
    }.get(trigger, "LIVE UPDATE")

    fnt_nano = _load_font(36)
    fnt_xs   = _load_font(44)
    fnt_sm   = _load_font(54)
    fnt_md   = _load_font(64, bold=True)
    fnt_lg   = _load_font(80, bold=True)
    fnt_xl   = _load_font(100, bold=True)
    fnt_hero = _load_font(200, bold=True)
    fnt_logo = _load_font(86, bold=True)

    def div(y):
        d.line([(0, y), (W, y)], fill=DIV, width=1)

    # ── Header bar ────────────────────────────────────────────────────────────
    d.rectangle([0, 0, W, 110], fill=(10, 14, 22))
    d.text((PAD, 14), "MARKET GENIE", font=fnt_logo, fill=WHITE)
    d.text((W - PAD, 14), trigger_lbl, font=fnt_xs, fill=ACCENT, anchor="ra")
    d.text((W - PAD, 66), data["timestamp"], font=_load_font(38), fill=DIM, anchor="ra")
    div(110)

    # ── "AI DAILY BRIEF" label ────────────────────────────────────────────────
    d.rectangle([0, 111, W, 200], fill=PANEL)
    brief_lbl = "TODAY'S AI MARKET BRIEF"
    d.text((W // 2, 155), brief_lbl, font=fnt_sm, fill=DIM, anchor="mm")
    div(200)

    # ── Regime pill (centred, large) ───────────────────────────────────────────
    y = 240
    rtext = f"  {regime}  {score}  "
    try:    rw = int(d.textlength(rtext, font=fnt_xl))
    except: rw = len(rtext) * 60
    rx = (W - rw) // 2
    d.rounded_rectangle([rx - 10, y, rx + rw + 10, y + 100], radius=18,
                         fill=_dim_color(rc, 6))
    d.rounded_rectangle([rx - 10, y, rx + rw + 10, y + 100], radius=18,
                         outline=rc, width=3)
    d.text((W // 2, y + 50), rtext, font=fnt_xl, fill=rc, anchor="mm")

    # ── Indices row ────────────────────────────────────────────────────────────
    nq  = data["nq_pct"]
    spy = data.get("spy_pct", 0.0)
    vix = data["vix"]
    y_idx = y + 130
    d.text((PAD,       y_idx), f"NQ {nq:+.2f}%",  font=fnt_md, fill=_ic(nq))
    d.text((W // 2,    y_idx), f"SPY {spy:+.2f}%", font=fnt_md, fill=_ic(spy), anchor="la")
    vc = RED if vix > 20 else DIM
    d.text((W - PAD,   y_idx), f"VIX {vix:.1f}",  font=fnt_md, fill=vc, anchor="ra")

    # ── "BIGGEST MOVER" hero section ──────────────────────────────────────────
    y_hero = y_idx + 100
    div(y_hero)
    d.rectangle([0, y_hero + 1, W, y_hero + 52], fill=(17, 24, 36))
    d.text((W // 2, y_hero + 26), "BIGGEST MOVER", font=fnt_xs, fill=AMBER, anchor="mm")

    if hot:
        h0  = hot[0]
        hc  = GREEN if h0["up"] else RED
        ha  = "+" if h0["up"] else "-"
        sym_y = y_hero + 70
        d.text((W // 2, sym_y), h0["symbol"], font=fnt_xl, fill=WHITE, anchor="mm")
        hero_txt = f"{ha}{abs(h0['pct']):.2f}%"
        d.text((W // 2, sym_y + 220), hero_txt, font=fnt_hero, fill=hc, anchor="mm")
        d.text((W // 2, sym_y + 380), f"${h0['price']:,.2f}", font=fnt_lg, fill=DIM, anchor="mm")
        # accent bar
        bfill = int((W - PAD * 2) * min(abs(h0["pct"]) / 10, 1.0))
        bar_y = sym_y + 430
        d.rectangle([PAD, bar_y, W - PAD, bar_y + 10], fill=DIV)
        if bfill > 4:
            d.rectangle([PAD, bar_y, PAD + bfill, bar_y + 10], fill=hc)
    else:
        d.text((W // 2, y_hero + 300), "Awaiting data", font=fnt_lg, fill=DIM, anchor="mm")

    # ── Footer ────────────────────────────────────────────────────────────────
    div(H - 64)
    d.rectangle([0, H - 64, W, H], fill=(10, 14, 22))
    d.text((W // 2, H - 38), "Follow @marketgenie.ai for daily AI signals",
           font=fnt_xs, fill=DIM, anchor="mm")
    d.text((W // 2, H - 10), "PAPER TRADING  |  NOT FINANCIAL ADVICE",
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
    d.text((W // 2, H - 14), "PAPER TRADING  |  NOT FINANCIAL ADVICE",
           font=fnt_nano, fill=DIV, anchor="mm")

    return img


# ═════════════════════════════════════════════════════════════════════════════
# SLIDE 3 — P&L CLOSE-UP / CTA
# ═════════════════════════════════════════════════════════════════════════════
def _generate_cta_frame(data: dict, trigger: str):
    from PIL import Image, ImageDraw

    W, H  = _VIDEO_W, _VIDEO_H
    BG    = (8, 11, 16)
    PANEL = (13, 18, 27)
    DIV   = (30, 42, 58)
    AMBER = (220, 155, 40)
    WHITE = (225, 235, 248)
    DIM   = (110, 128, 150)
    GREEN = (74, 222, 128)
    RED   = (248, 113, 113)
    ACCENT = (56, 189, 248)

    img = Image.new("RGB", (W, H), BG)
    d   = ImageDraw.Draw(img)
    PAD = 56

    pnl  = data["pnl_today"]
    pc   = GREEN if pnl >= 0 else RED
    pcs  = "+" if pnl >= 0 else "-"
    eq   = data["equity"]
    pd   = (pnl / max(eq - pnl, 1)) * 100 if eq > 0 else 0.0

    fnt_nano = _load_font(36)
    fnt_xs   = _load_font(44)
    fnt_sm   = _load_font(54)
    fnt_md   = _load_font(66, bold=True)
    fnt_lg   = _load_font(84, bold=True)
    fnt_xl   = _load_font(108, bold=True)
    fnt_hero = _load_font(220, bold=True)
    fnt_logo = _load_font(86, bold=True)

    def div(y):
        d.line([(0, y), (W, y)], fill=DIV, width=1)

    # Header
    d.rectangle([0, 0, W, 110], fill=(10, 14, 22))
    d.text((PAD, 14), "MARKET GENIE", font=fnt_logo, fill=WHITE)
    d.text((W - PAD, 14), "RESULTS", font=fnt_xs, fill=ACCENT, anchor="ra")
    d.text((W - PAD, 66), data["timestamp"], font=_load_font(38), fill=DIM, anchor="ra")
    div(110)

    # P&L label
    d.rectangle([0, 111, W, 180], fill=PANEL)
    d.text((W // 2, 145), "TODAY'S P&L", font=fnt_sm, fill=AMBER, anchor="mm")

    # Giant P&L number
    y_pnl = 230
    if trigger == "premarket" and abs(pnl) < 1:
        pnl_str = f"${eq:,.0f}"
        sub_str = "Capital · Opens 9:30 AM ET"
        sub_col = DIM
    else:
        pnl_str = f"{pcs}${abs(pnl):,.0f}"
        sub_str = f"({pcs}{abs(pd):.2f}% daily return)"
        sub_col = pc

    d.text((W // 2, y_pnl + 180), pnl_str, font=fnt_hero, fill=pc, anchor="mm")
    d.text((W // 2, y_pnl + 340), sub_str, font=fnt_lg, fill=sub_col, anchor="mm")

    # Divider + equity bar
    div(y_pnl + 400)
    d.rectangle([0, y_pnl + 401, W, y_pnl + 461], fill=PANEL)
    d.text((W // 2, y_pnl + 431), f"Account Equity: ${eq:,.0f}", font=fnt_md, fill=DIM, anchor="mm")

    # Open positions summary
    pos = data.get("positions", [])
    y_pos = y_pnl + 510
    if pos:
        div(y_pos - 50)
        d.rectangle([0, y_pos - 50, W, y_pos - 10], fill=(17, 24, 36))
        d.text((W // 2, y_pos - 30), "OPEN POSITIONS", font=fnt_xs, fill=AMBER, anchor="mm")
        for i, p in enumerate(pos[:3]):
            ul = p["unrealized_pl"]
            uc = GREEN if ul >= 0 else RED
            us = "+" if ul >= 0 else "-"
            row_y = y_pos + i * 80
            d.rectangle([0, row_y, W, row_y + 76], fill=(PANEL if i % 2 