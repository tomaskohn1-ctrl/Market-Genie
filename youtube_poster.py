"""
youtube_poster.py - Market Genie -> YouTube Shorts auto-poster
3-slide video: hook -> dashboard -> P&L/CTA with xfade + TTS voiceover.
Posts 4x per day: 9:15 AM, 12:00 PM, 4:15 PM, 5:30 PM ET (weekdays).
"""

import os, json, base64, time, threading, subprocess, tempfile
from datetime import datetime
from pathlib import Path
import requests

_YT_SCOPES   = ["https://www.googleapis.com/auth/youtube.upload"]
_VIDEO_W     = 1080
_VIDEO_H     = 1920
_VIDEO_SECS  = 30
_CATEGORY_ID = "25"
_SLIDE1_SECS = 9
_SLIDE2_SECS = 13
_SLIDE3_SECS = 8
_FADE_SECS   = 0.5


def _get_yt_credentials():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        token_b64 = os.getenv("YOUTUBE_TOKEN_JSON", "")
        if not token_b64:
            print("[YouTube] YOUTUBE_TOKEN_JSON not set")
            return None
        token_b64 = token_b64.strip()
        padding = 4 - (len(token_b64) % 4)
        if padding != 4:
            token_b64 += "=" * padding
        try:
            raw = base64.b64decode(token_b64)
        except Exception:
            raw = base64.urlsafe_b64decode(token_b64)
        token_data = json.loads(raw.decode("utf-8"))
        creds = Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.getenv("YOUTUBE_CLIENT_ID", ""),
            client_secret=os.getenv("YOUTUBE_CLIENT_SECRET", ""),
            scopes=_YT_SCOPES,
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds
    except Exception as e:
        print(f"[YouTube] Credentials error: {e}")
        return None


def _get_yt_service():
    from googleapiclient.discovery import build
    creds = _get_yt_credentials()
    if not creds:
        return None
    return build("youtube", "v3", credentials=creds)


_HOT_FALLBACK = ["NVDA", "TSLA", "AAPL", "META", "MSFT", "AMZN", "GOOGL", "MSTR"]


def _fetch_ticker_moves(symbols):
    results = []
    try:
        import yfinance as yf
        for sym in symbols[:6]:
            try:
                t = yf.Ticker(sym)
                fi = t.fast_info
                price = float(getattr(fi, "last_price", None) or getattr(fi, "regularMarketPrice", None) or 0)
                prev  = float(getattr(fi, "previous_close", None) or getattr(fi, "regularMarketPreviousClose", None) or price)
                pct   = ((price - prev) / prev * 100) if prev else 0.0
                if price > 0:
                    results.append({"symbol": sym, "price": price, "pct": pct, "up": pct >= 0})
            except Exception as e:
                print(f"[YouTube] Ticker {sym} error: {e}")
    except Exception as e:
        print(f"[YouTube] yfinance error: {e}")
    return results


def _fetch_market_data():
    port = int(os.getenv("PORT", "8080"))
    defaults = {
        "regime": "NEUTRAL", "regime_score": 50,
        "pnl_today": 0.0, "equity": 100_000.0,
        "positions": [], "nq_pct": 0.0, "spy_pct": 0.0,
        "qqq_pct": 0.0, "vix": 16.5,
        "hot_tickers": [], "social_hot": [], "ai_signals": [],
        "timestamp": datetime.now().strftime("%b %d, %Y  .  %I:%M %p ET"),
    }
    try:
        r = requests.get(f"http://localhost:{port}/api/youtube/data", timeout=8)
        if r.status_code == 200:
            srv = r.json()
            defaults.update({k: srv[k] for k in srv if k in defaults})
            social_syms = [t["symbol"] for t in srv.get("social_hot", [])[:8] if t.get("symbol")]
            if social_syms:
                defaults["hot_tickers"] = _fetch_ticker_moves(social_syms)
    except Exception as e:
        print(f"[YouTube] Data fetch error: {e}")
    if not defaults["hot_tickers"]:
        defaults["hot_tickers"] = _fetch_ticker_moves(_HOT_FALLBACK[:6])
    return defaults


def _load_font(size, bold=False):
    from PIL import ImageFont
    suffix = "-Bold" if bold else ""
    _here = Path(__file__).parent
    candidates = [
        str(_here / "fonts" / f"DejaVuSans{suffix}.ttf"),
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{suffix}.ttf",
        f"/usr/share/fonts/truetype/liberation/LiberationSans{suffix}.ttf",
        f"/usr/share/fonts/truetype/ubuntu/Ubuntu-{'B' if bold else 'R'}.ttf",
    ]
    for path in candidates:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _regime_rgb(regime):
    return {"BULLISH": (74, 222, 128), "BEARISH": (248, 113, 113), "NEUTRAL": (250, 204, 21)}.get(regime, (156, 163, 175))


def _ic(v):
    return (74, 222, 128) if v >= 0 else (248, 113, 113)


def _generate_hook_frame(data, trigger):
    from PIL import Image, ImageDraw
    W, H = _VIDEO_W, _VIDEO_H
    BG = (8, 11, 16); PANEL = (13, 18, 27); DIV = (30, 42, 58)
    AMBER = (220, 155, 40); WHITE = (225, 235, 248); DIM = (110, 128, 150)
    GREEN = (74, 222, 128); RED = (248, 113, 113); ACCENT = (56, 189, 248)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    PAD = 56
    hot = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    regime = data["regime"]; score = data["regime_score"]; rc = _regime_rgb(regime)
    trigger_lbl = {"premarket": "PRE-MARKET BRIEF", "midday": "MIDDAY UPDATE",
                   "eod": "END OF DAY", "afterhours": "AFTER-HOURS WRAP"}.get(trigger, "LIVE UPDATE")
    fnt_nano = _load_font(36); fnt_xs = _load_font(44); fnt_sm = _load_font(54)
    fnt_md = _load_font(64, bold=True); fnt_lg = _load_font(80, bold=True)
    fnt_xl = _load_font(100, bold=True); fnt_hero = _load_font(200, bold=True)
    fnt_logo = _load_font(86, bold=True)

    def div(y):
        d.line([(0, y), (W, y)], fill=DIV, width=1)

    d.rectangle([0, 0, W, 110], fill=(10, 14, 22))
    d.text((PAD, 14), "MARKET GENIE", font=fnt_logo, fill=WHITE)
    d.text((W - PAD, 14), trigger_lbl, font=fnt_xs, fill=ACCENT, anchor="ra")
    d.text((W - PAD, 66), data["timestamp"], font=_load_font(38), fill=DIM, anchor="ra")
    div(110)
    d.rectangle([0, 111, W, 200], fill=PANEL)
    d.text((W // 2, 155), "TODAY'S AI MARKET BRIEF", font=fnt_sm, fill=DIM, anchor="mm")
    div(200)
    y = 240
    rtext = "  " + regime + "  " + str(score) + "  "
    try:
        rw = int(d.textlength(rtext, font=fnt_xl))
    except Exception:
        rw = len(rtext) * 60
    rx = (W - rw) // 2
    d.rounded_rectangle([rx - 10, y, rx + rw + 10, y + 100], radius=18, fill=(rc[0]//6, rc[1]//6, rc[2]//6))
    d.rounded_rectangle([rx - 10, y, rx + rw + 10, y + 100], radius=18, outline=rc, width=3)
    d.text((W // 2, y + 50), rtext, font=fnt_xl, fill=rc, anchor="mm")
    nq = data["nq_pct"]; spy = data.get("spy_pct", 0.0); vix = data["vix"]
    y_idx = y + 130
    d.text((PAD, y_idx), f"NQ {nq:+.2f}%", font=fnt_md, fill=_ic(nq))
    d.text((W // 2, y_idx), f"SPY {spy:+.2f}%", font=fnt_md, fill=_ic(spy), anchor="la")
    vc = RED if vix > 20 else DIM
    d.text((W - PAD, y_idx), f"VIX {vix:.1f}", font=fnt_md, fill=vc, anchor="ra")
    y_hero = y_idx + 100
    div(y_hero)
    d.rectangle([0, y_hero + 1, W, y_hero + 52], fill=(17, 24, 36))
    d.text((W // 2, y_hero + 26), "BIGGEST MOVER", font=fnt_xs, fill=AMBER, anchor="mm")
    if hot:
        h0 = hot[0]; hc = GREEN if h0["up"] else RED; ha = "+" if h0["up"] else "-"
        sym_y = y_hero + 70
        d.text((W // 2, sym_y), h0["symbol"], font=fnt_xl, fill=WHITE, anchor="mm")
        d.text((W // 2, sym_y + 220), f"{ha}{abs(h0['pct']):.2f}%", font=fnt_hero, fill=hc, anchor="mm")
        d.text((W // 2, sym_y + 380), f"${h0['price']:,.2f}", font=fnt_lg, fill=DIM, anchor="mm")
        bfill = int((W - PAD * 2) * min(abs(h0["pct"]) / 10, 1.0))
        bar_y = sym_y + 430
        d.rectangle([PAD, bar_y, W - PAD, bar_y + 10], fill=DIV)
        if bfill > 4:
            d.rectangle([PAD, bar_y, PAD + bfill, bar_y + 10], fill=hc)
    else:
        d.text((W // 2, y_hero + 300), "Awaiting data", font=fnt_lg, fill=DIM, anchor="mm")
    div(H - 64)
    d.rectangle([0, H - 64, W, H], fill=(10, 14, 22))
    d.text((W // 2, H - 38), "Follow @marketgenie.ai for daily AI signals", font=fnt_xs, fill=DIM, anchor="mm")
    d.text((W // 2, H - 10), "PAPER TRADING  |  NOT FINANCIAL ADVICE", font=fnt_nano, fill=DIV, anchor="mm")
    return img


def _generate_frame(data, trigger):
    from PIL import Image, ImageDraw
    W, H = _VIDEO_W, _VIDEO_H
    BG = (8, 11, 16); PANEL = (13, 18, 27); ALT = (17, 24, 36); DIV = (30, 42, 58)
    AMBER = (220, 155, 40); WHITE = (225, 235, 248); DIM = (110, 128, 150)
    GREEN = (74, 222, 128); RED = (248, 113, 113)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    PAD = 44

    def ic(v):
        return GREEN if v >= 0 else RED

    fnt_nano = _load_font(36); fnt_xs = _load_font(42); fnt_sm = _load_font(50)
    fnt_md = _load_font(58, bold=True); fnt_lg = _load_font(70, bold=True)
    fnt_xl = _load_font(88, bold=True); fnt_hero = _load_font(172, bold=True)
    fnt_lbl = _load_font(36); fnt_logo = _load_font(82, bold=True)
    rc = _regime_rgb(data["regime"])
    pnl = data["pnl_today"]; pcs = "+" if pnl >= 0 else "-"; pc = ic(pnl)
    hot = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    signals = [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 65][:3]
    pos = data.get("positions", [])
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

    d.rectangle([0, 0, W, 106], fill=(10, 14, 22))
    d.text((PAD, 10), "MARKET GENIE", font=fnt_logo, fill=WHITE)
    d.text((W - PAD, 10), trigger_lbl, font=fnt_sm, fill=rc, anchor="ra")
    d.text((W - PAD, 62), data["timestamp"], font=fnt_xs, fill=DIM, anchor="ra")
    div(106)
    d.rectangle([0, 107, W, 237], fill=PANEL)
    regime = data["regime"]; score = data["regime_score"]
    nq = data["nq_pct"]; spy = data.get("spy_pct", 0.0); vix = data["vix"]
    rtext = " " + regime + "  " + str(score) + " "
    try:
        rw = int(d.textlength(rtext, font=fnt_lg))
    except Exception:
        rw = len(rtext) * 42
    d.rounded_rectangle([PAD, 120, PAD + rw + 4, 192], radius=10, fill=(rc[0]//7, rc[1]//7, rc[2]//7))
    d.rounded_rectangle([PAD, 120, PAD + rw + 4, 192], radius=10, outline=rc, width=2)
    d.text((PAD + 8, 126), rtext, font=fnt_lg, fill=rc)
    ix = PAD + rw + 36
    d.text((ix, 126), f"NQ {nq:+.2f}%", font=fnt_sm, fill=ic(nq))
    d.text((ix + 240, 126), f"SPY {spy:+.2f}%", font=fnt_sm, fill=ic(spy))
    vc = RED if vix > 20 else DIM
    d.text((W - PAD, 126), f"VIX {vix:.1f}", font=fnt_sm, fill=vc, anchor="ra")
    div(237)
    y = sec(238, "Biggest mover today")
    d.rectangle([0, y, W, y + 200], fill=PANEL)
    if hot:
        h0 = hot[0]; hc = GREEN if h0["up"] else RED; ha = "+" if h0["up"] else "-"
        d.text((PAD + 12, y + 12), h0["symbol"], font=fnt_xl, fill=WHITE)
        d.text((W - PAD - 12, y + 18), f"${h0['price']:,.2f}", font=fnt_md, fill=DIM, anchor="ra")
        d.text((W // 2, y + 106), f"{ha}{abs(h0['pct']):.2f}%", font=fnt_hero, fill=hc, anchor="mm")
        bfill = int((W - PAD * 2) * min(abs(h0["pct"]) / 10, 1.0))
        d.rectangle([PAD, y + 184, W - PAD, y + 192], fill=DIV)
        if bfill > 4:
            d.rectangle([PAD, y + 184, PAD + bfill, y + 192], fill=hc)
    else:
        d.text((W // 2, y + 100), "Awaiting data", font=fnt_lg, fill=DIM, anchor="mm")
    y += 200; div(y)
    y = sec(y + 1, "Market movers", "Change")
    for i, tk in enumerate(hot[1:5]):
        rb = ALT if i % 2 == 0 else PANEL
        d.rectangle([0, y, W, y + 82], fill=rb)
        tc = GREEN if tk["up"] else RED; ar = "+" if tk["up"] else "-"
        d.text((PAD + 12, y + 16), tk["symbol"], font=fnt_lg, fill=WHITE)
        d.text((W // 2, y + 20), f"{ar}{abs(tk['pct']):.2f}%", font=fnt_md, fill=tc, anchor="mm")
        d.text((W - PAD - 12, y + 16), f"${tk['price']:,.2f}", font=fnt_md, fill=DIM, anchor="ra")
        bw2 = int((W - PAD * 2 - 24) * min(abs(tk["pct"]) / 8, 1.0))
        d.rectangle([PAD + 12, y + 68, W - PAD - 12, y + 74], fill=DIV)
        if bw2 > 2:
            d.rectangle([PAD + 12, y + 68, PAD + 12 + bw2, y + 74], fill=tc)
        y += 82
    div(y)
    if signals:
        y = sec(y + 1, "AI signals", "Conf")
        for i, sig in enumerate(signals):
            rb = ALT if i % 2 == 0 else PANEL
            d.rectangle([0, y, W, y + 74], fill=rb)
            bull = "bull" in sig.get("direction", "bull").lower()
            sc2 = GREEN if bull else RED; conf = sig.get("confidence", 70)
            d.text((PAD + 12, y + 14), sig["symbol"], font=fnt_lg, fill=WHITE)
            try:
                sx = d.textbbox((PAD + 12, y + 14), sig["symbol"], font=fnt_lg)[2] + 16
            except Exception:
                sx = PAD + 12 + len(sig["symbol"]) * 44 + 16
            d.rounded_rectangle([sx, y + 18, sx + 100, y + 58], radius=6, fill=(sc2[0]//5, sc2[1]//5, sc2[2]//5))
            d.text((sx + 8, y + 21), "BULL" if bull else "BEAR", font=fnt_xs, fill=sc2)
            bx3 = sx + 118; bw3 = W - PAD - 12 - bx3 - 80
            d.rectangle([bx3, y + 26, bx3 + bw3, y + 46], fill=DIV)
            fb = int(bw3 * min(conf / 100, 1))
            if fb > 2:
                d.rectangle([bx3, y + 26, bx3 + fb, y + 46], fill=sc2)
            d.text((W - PAD - 12, y + 14), f"{conf:.0f}%", font=fnt_md, fill=sc2, anchor="ra")
            y += 74
        div(y)
    if pos:
        y = sec(y + 1, "AI open trades")
        for p2 in pos[:2]:
            d.rectangle([0, y, W, y + 78], fill=PANEL)
            s2 = p2["symbol"]; sd = p2["side"]
            ul = p2["unrealized_pl"]; up = p2["unrealized_plpc"]
            sc3 = GREEN if sd == "LONG" else RED
            pc3 = GREEN if ul >= 0 else RED; ps3 = "+" if ul >= 0 else "-"
            d.text((PAD + 12, y + 14), s2, font=fnt_lg, fill=WHITE)
            try:
                bx4 = d.textbbox((PAD + 12, y + 14), s2, font=fnt_lg)[2] + 14
            except Exception:
                bx4 = PAD + 12 + len(s2) * 44 + 14
            d.rounded_rectangle([bx4, y + 18, bx4 + 108, y + 58], radius=6, fill=(sc3[0]//5, sc3[1]//5, sc3[2]//5))
            d.text((bx4 + 8, y + 21), sd, font=fnt_xs, fill=sc3)
            d.text((W - PAD - 12, y + 12), f"{ps3}${abs(ul):,.0f}", font=fnt_lg, fill=pc3, anchor="ra")
            d.text((W - PAD - 12, y + 56), f"({ps3}{abs(up):.2f}%)", font=fnt_xs, fill=pc3, anchor="ra")
            y += 78
        div(y)
    pnl_top = max(y + 1, H - 250)
    d.rectangle([0, pnl_top, W, H - 64], fill=PANEL)
    d.text((PAD + 12, pnl_top + 10), "TODAY'S P&L", font=fnt_lbl, fill=AMBER)
    if trigger == "premarket" and abs(pnl) < 1:
        d.text((PAD + 12, pnl_top + 52), f"${data['equity']:,.0f}", font=_load_font(134, bold=True), fill=WHITE)
        d.text((W - PAD - 12, pnl_top + 80), "Opens 9:30 AM ET", font=fnt_sm, fill=DIM, anchor="ra")
    else:
        d.text((PAD + 12, pnl_top + 52), f"{pcs}${abs(pnl):,.0f}", font=_load_font(134, bold=True), fill=pc)
        if data["equity"] > 0:
            pd = (pnl / max(data["equity"] - pnl, 1)) * 100
            d.text((W - PAD - 12, pnl_top + 80), f"{pcs}{abs(pd):.2f}%", font=fnt_xl, fill=pc, anchor="ra")
    div(H - 64)
    d.rectangle([0, H - 64, W, H], fill=(10, 14, 22))
    d.text((W // 2, H - 44), "Follow @marketgenie.ai for daily AI signals", font=fnt_xs, fill=DIM, anchor="mm")
    d.text((W // 2, H - 14), "PAPER TRADING  |  NOT FINANCIAL ADVICE", font=fnt_nano, fill=DIV, anchor="mm")
    return img


def _generate_cta_frame(data, trigger):
    from PIL import Image, ImageDraw
    W, H = _VIDEO_W, _VIDEO_H
    BG = (8, 11, 16); PANEL = (13, 18, 27); DIV = (30, 42, 58)
    AMBER = (220, 155, 40); WHITE = (225, 235, 248); DIM = (110, 128, 150)
    GREEN = (74, 222, 128); RED = (248, 113, 113); ACCENT = (56, 189, 248)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    PAD = 56
    pnl = data["pnl_today"]; pc = GREEN if pnl >= 0 else RED
    pcs = "+" if pnl >= 0 else "-"; eq = data["equity"]
    pd = (pnl / max(eq - pnl, 1)) * 100 if eq > 0 else 0.0
    fnt_nano = _load_font(36); fnt_xs = _load_font(44); fnt_sm = _load_font(54)
    fnt_md = _load_font(66, bold=True); fnt_lg = _load_font(84, bold=True)
    fnt_xl = _load_font(108, bold=True); fnt_hero = _load_font(220, bold=True)
    fnt_logo = _load_font(86, bold=True)

    def div(y):
        d.line([(0, y), (W, y)], fill=DIV, width=1)

    d.rectangle([0, 0, W, 110], fill=(10, 14, 22))
    d.text((PAD, 14), "MARKET GENIE", font=fnt_logo, fill=WHITE)
    d.text((W - PAD, 14), "RESULTS", font=fnt_xs, fill=ACCENT, anchor="ra")
    d.text((W - PAD, 66), data["timestamp"], font=_load_font(38), fill=DIM, anchor="ra")
    div(110)
    d.rectangle([0, 111, W, 180], fill=PANEL)
    d.text((W // 2, 145), "TODAY'S P&L", font=fnt_sm, fill=AMBER, anchor="mm")
    y_pnl = 230
    if trigger == "premarket" and abs(pnl) < 1:
        pnl_str = f"${eq:,.0f}"; sub_str = "Capital - Opens 9:30 AM ET"; sub_col = DIM
    else:
        pnl_str = f"{pcs}${abs(pnl):,.0f}"
        sub_str = f"({pcs}{abs(pd):.2f}% daily return)"; sub_col = pc
    d.text((W // 2, y_pnl + 180), pnl_str, font=fnt_hero, fill=pc, anchor="mm")
    d.text((W // 2, y_pnl + 340), sub_str, font=fnt_lg, fill=sub_col, anchor="mm")
    div(y_pnl + 400)
    d.rectangle([0, y_pnl + 401, W, y_pnl + 461], fill=PANEL)
    d.text((W // 2, y_pnl + 431), f"Account Equity: ${eq:,.0f}", font=fnt_md, fill=DIM, anchor="mm")
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
            rb = PANEL if i % 2 == 0 else (17, 24, 36)
            d.rectangle([0, row_y, W, row_y + 76], fill=rb)
            d.text((PAD + 12, row_y + 14), p["symbol"], font=fnt_lg, fill=WHITE)
            d.text((W - PAD - 12, row_y + 14), f"{us}${abs(ul):,.0f}", font=fnt_lg, fill=uc, anchor="ra")
        y_pos += len(pos[:3]) * 80 + 20
    cta_top = max(y_pos + 40, H - 340)
    d.rectangle([0, cta_top, W, H - 64], fill=(14, 22, 38))
    d.rounded_rectangle([PAD, cta_top + 20, W - PAD, cta_top + 130], radius=14,
                         fill=(20, 40, 70), outline=ACCENT, width=2)
    d.text((W // 2, cta_top + 75), "FOLLOW FOR DAILY AI SIGNALS", font=fnt_md, fill=WHITE, anchor="mm")
    d.text((W // 2, cta_top + 165), "@marketgenie.ai", font=fnt_xl, fill=ACCENT, anchor="mm")
    d.text((W // 2, cta_top + 260), "Free AI-powered signals every trading day",
           font=fnt_sm, fill=DIM, anchor="mm")
    div(H - 64)
    d.rectangle([0, H - 64, W, H], fill=(10, 14, 22))
    d.text((W // 2, H - 38), "Follow @marketgenie.ai for daily AI signals", font=fnt_xs, fill=DIM, anchor="mm")
    d.text((W // 2, H - 10), "PAPER TRADING  |  NOT FINANCIAL ADVICE", font=fnt_nano, fill=DIV, anchor="mm")
    return img


def _generate_voiceover(data, trigger):
    try:
        from gtts import gTTS
    except ImportError:
        print("[YouTube] gtts not installed - skipping voiceover")
        return None
    regime = data["regime"].lower(); score = data["regime_score"]
    pnl = data["pnl_today"]; pcs = "up" if pnl >= 0 else "down"
    hot = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    vix = data["vix"]; nq = data["nq_pct"]
    date_str = datetime.now().strftime("%B %d")
    trigger_phrase = {"premarket": "pre-market brief", "midday": "midday update",
                      "eod": "end of day wrap", "afterhours": "after-hours summary"}.get(trigger, "update")
    parts = [
        f"Market Genie A.I. {trigger_phrase} for {date_str}.",
        f"Market regime is {regime}, scoring {score} out of 100.",
    ]
    if nq != 0:
        parts.append(f"NASDAQ futures are {'up' if nq >= 0 else 'down'} {abs(nq):.1f} percent.")
    if vix > 20:
        parts.append(f"Watch out. VIX is elevated at {vix:.0f}.")
    if hot:
        h0 = hot[0]
        parts.append(f"Biggest mover: {h0['symbol']}, {'up' if h0['up'] else 'down'} {abs(h0['pct']):.1f} percent.")
    signals = [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 65][:1]
    if signals:
        sig = signals[0]; bull = "bull" in sig.get("direction", "bull").lower()
        parts.append(f"A.I. signal: {'bullish' if bull else 'bearish'} on {sig['symbol']} "
                     f"with {sig.get('confidence', 70):.0f} percent confidence.")
    if trigger != "premarket" and abs(pnl) >= 1:
        parts.append(f"Today's P and L: {pcs} ${abs(pnl):,.0f}.")
    parts.append("Follow Market Genie for free A.I. signals every trading day.")
    script = "  ".join(parts)
    try:
        tts = gTTS(text=script, lang="en", slow=False)
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tts.save(tmp.name); tmp.close()
        return tmp.name
    except Exception as e:
        print(f"[YouTube] gTTS error: {e}")
        return None


def _get_ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _cleanup(paths):
    for p in paths:
        if p:
            try:
                os.unlink(p)
            except Exception:
                pass


def _create_short(frame, output_path, hook_frame=None, cta_frame=None, audio_path=None):
    tmp_files = []

    def save_tmp(img):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img.save(f.name, "PNG"); tmp_files.append(f.name); return f.name

    ffmpeg_bin = _get_ffmpeg(); fps = 25

    if hook_frame is not None and cta_frame is not None:
        p1 = save_tmp(hook_frame); p2 = save_tmp(frame); p3 = save_tmp(cta_frame)
        fade1 = _SLIDE1_SECS - _FADE_SECS
        fade2 = _SLIDE1_SECS + _SLIDE2_SECS - _FADE_SECS
        total = _SLIDE1_SECS + _SLIDE2_SECS + _SLIDE3_SECS
        fc = (
            f"[0:v]scale={_VIDEO_W}:{_VIDEO_H},setsar=1,fps={fps}[v0];"
            f"[1:v]scale={_VIDEO_W}:{_VIDEO_H},setsar=1,fps={fps}[v1];"
            f"[2:v]scale={_VIDEO_W}:{_VIDEO_H},setsar=1,fps={fps}[v2];"
            f"[v0][v1]xfade=transition=fade:duration={_FADE_SECS}:offset={fade1}[x1];"
            f"[x1][v2]xfade=transition=fade:duration={_FADE_SECS}:offset={fade2}[vout]"
        )
        cmd = [ffmpeg_bin, "-y",
               "-loop", "1", "-t", str(_SLIDE1_SECS + 1), "-i", p1,
               "-loop", "1", "-t", str(_SLIDE2_SECS + 1), "-i", p2,
               "-loop", "1", "-t", str(_SLIDE3_SECS + 1), "-i", p3]
        if audio_path:
            cmd += ["-i", audio_path]
        cmd += ["-filter_complex", fc, "-map", "[vout]"]
        if audio_path:
            cmd += ["-map", "3:a", "-c:a", "aac", "-b:a", "128k", "-shortest"]
        cmd += ["-t", str(total), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-preset", "fast", "-crf", "22", output_path]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            _cleanup(tmp_files + ([audio_path] if audio_path else []))
            if res.returncode == 0:
                print(f"[YouTube] Multi-slide video created: {output_path}")
                return True
            print(f"[YouTube] xfade failed, falling back\n{res.stderr[-400:]}")
        except Exception as e:
            print(f"[YouTube] Multi-slide exception: {e}")
            _cleanup(tmp_files + ([audio_path] if audio_path else []))

    p = save_tmp(frame)
    frames_total = _VIDEO_SECS * fps
    cmd = [ffmpeg_bin, "-y", "-loop", "1", "-i", p]
    if audio_path:
        cmd += ["-i", audio_path]
    zoom_filter = (f"zoompan=z='min(zoom+0.0006,1.12)':x='iw/2-(iw/zoom/2)'"
                   f":y='ih/2-(ih/zoom/2)':d={frames_total}:s={_VIDEO_W}x{_VIDEO_H}:fps={fps}")
    cmd += ["-vf", zoom_filter]
    if audio_path:
        cmd += ["-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "128k", "-shortest"]
    cmd += ["-t", str(_VIDEO_SECS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-preset", "fast", "-crf", "22", output_path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        _cleanup(tmp_files + ([audio_path] if audio_path else []))
        if res.returncode == 0:
            print(f"[YouTube] Video created (fallback): {output_path}")
            return True
        print(f"[YouTube] ffmpeg error:\n{res.stderr[-600:]}")
        return False
    except Exception as e:
        print(f"[YouTube] ffmpeg exception: {e}")
        return False


def _upload_to_youtube(service, video_path, title, description):
    from googleapiclient.http import MediaFileUpload
    tags = ["day trading", "stock market", "algo trading", "AI trading", "paper trading",
            "finance", "investing", "stocks", "trading bot", "market analysis",
            "automated trading", "quant trading", "market genie", "AI signals"]
    body = {
        "snippet": {"title": title[:100], "description": description,
                    "tags": tags, "categoryId": _CATEGORY_ID},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(video_path, mimetype="video/mp4", resumable=True, chunksize=1024*1024)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    try:
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"[YouTube] Upload: {int(status.progress()*100)}%")
        print(f"[YouTube] Published: https://youtube.com/shorts/{response.get('id','?')}")
        return True
    except Exception as e:
        print(f"[YouTube] Upload error: {e}")
        return False


def post_market_update(trigger="midday"):
    print(f"[YouTube] Starting {trigger} post...")
    service = _get_yt_service()
    if not service:
        return False
    data = _fetch_market_data()
    hook_frame = _generate_hook_frame(data, trigger)
    dash_frame = _generate_frame(data, trigger)
    cta_frame  = _generate_cta_frame(data, trigger)
    audio_path = _generate_voiceover(data, trigger)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        mp4_path = tmp.name
    if not _create_short(dash_frame, mp4_path, hook_frame=hook_frame,
                         cta_frame=cta_frame, audio_path=audio_path):
        return False
    date_str = datetime.now().strftime("%b %d")
    pnl = data["pnl_today"]; pnl_sign = "+" if pnl >= 0 else "-"
    regime = data["regime"]; score = data["regime_score"]
    hot = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    top_tickers = [f"{t['symbol']} {'+' if t['up'] else ''}{t['pct']:.1f}%"
                   for t in hot[:3] if t.get("price", 0) > 0]
    ticker_str = "  |  ".join(top_tickers)
    vix = data["vix"]; vix_note = f"  VIX {vix:.0f}!" if vix > 22 else ""
    titles = {
        "premarket": (f"AI Pre-Market {date_str}: {regime} {score}/100{vix_note}"
                      + (f"  |  {hot[0]['symbol']} {'+' if hot[0]['up'] else ''}{hot[0]['pct']:.1f}%" if hot else "")),
        "midday":    (f"AI Midday P&L: {pnl_sign}${abs(pnl):,.0f} - {regime} {score}"
                      + (f"  |  {ticker_str}" if ticker_str else "")),
        "eod":       (f"AI Closed {pnl_sign}${abs(pnl):,.0f} Today - {regime}"
                      + (f"  |  {ticker_str}" if ticker_str else f"  |  {date_str}")),
        "afterhours": (f"After-Hours: AI Final P&L {pnl_sign}${abs(pnl):,.0f}"
                       + (f"  |  {ticker_str}" if ticker_str else f"  |  {regime}")),
    }
    title = titles.get(trigger, f"Market Genie AI Update | {date_str}")[:100]
    pos_lines = "\n".join(
        f"  {p['symbol']} {p['side']}: {'+' if p['unrealized_pl']>=0 else '-'}${abs(p['unrealized_pl']):,.0f}"
        for p in data["positions"]) or "  No open positions"
    signals = [s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 65]
    sig_lines = "\n".join(
        f"  {'BULL' if 'bull' in s.get('direction','').lower() else 'BEAR'} {s['symbol']} - {s.get('confidence',70):.0f}% confidence"
        for s in signals[:3]) or "  No high-confidence signals"
    description = (
        f"Market Genie AI Auto-Trader - {trigger.upper()} UPDATE\n\n"
        f"Regime: {regime} ({score}/100)\n"
        f"Today's P&L: {pnl_sign}${abs(pnl):,.2f}\n"
        f"NQ Futures: {data['nq_pct']:+.2f}%\n"
        f"VIX: {data['vix']:.1f}\n\n"
        f"AI Signals:\n{sig_lines}\n\n"
        f"Open Positions ({len(data['positions'])}):\n{pos_lines}\n\n"
        f"Subscribe for free AI market signals every trading day!\n"
        f"Follow @marketgenie.ai\n\n"
        f"PAPER TRADING ONLY - NOT FINANCIAL ADVICE\n\n"
        f"#daytrading #stocks #algotrading #AItrading #stockmarket #finance #investing "
        f"#marketgenie #tradingsignals #wallstreet #nasdaq #sp500"
    )
    success = _upload_to_youtube(service, mp4_path, title, description)
    try:
        os.unlink(mp4_path)
    except Exception:
        pass
    return success


_YT_POSTED_FILE = "/tmp/youtube_posted.json"


def _load_posted():
    try:
        if Path(_YT_POSTED_FILE).exists():
            with open(_YT_POSTED_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_posted(d):
    try:
        from datetime import datetime as _dt, timedelta
        cutoff = (_dt.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        pruned = {k: v for k, v in d.items() if k[:10] >= cutoff}
        with open(_YT_POSTED_FILE, "w") as f:
            json.dump(pruned, f)
    except Exception:
        pass


def _youtube_scheduler_loop():
    import pytz
    from datetime import time as dtime
    _posted = _load_posted()
    slots = [
        (dtime(9, 15),  dtime(9, 30),  "premarket"),
        (dtime(12, 0),  dtime(12, 15), "midday"),
        (dtime(16, 15), dtime(16, 30), "eod"),
        (dtime(17, 30), dtime(17, 45), "afterhours"),
    ]
    while True:
        try:
            et_now = datetime.now(pytz.timezone("America/New_York"))
            date_key = et_now.strftime("%Y-%m-%d")
            t = et_now.time(); is_wday = et_now.weekday() <= 4
            if not os.getenv("YOUTUBE_TOKEN_JSON"):
                time.sleep(60); continue
            if is_wday:
                for start, end, trigger in slots:
                    key = f"{date_key}_{trigger}"
                    if start <= t < end and key not in _posted:
                        _posted[key] = True; _save_posted(_posted)
                        threading.Thread(target=post_market_update, args=(trigger,),
                                         daemon=True, name=f"YT-{trigger}").start()
        except Exception as e:
            print(f"[YouTube] Scheduler error: {e}")
        time.sleep(30)


def start_youtube_scheduler():
    if not os.getenv("YOUTUBE_TOKEN_JSON"):
        print("[YouTube] YOUTUBE_TOKEN_JSON not set - auto-posting disabled")
        return
    t = threading.Thread(target=_youtube_scheduler_loop, daemon=True, name="YouTubeScheduler")
    t.start()
    print("[YouTube] Scheduler started - posts at 9:15 AM, 12:00 PM, 4:15 PM, 5:30 PM ET")
