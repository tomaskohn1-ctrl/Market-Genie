"""
youtube_poster.py — Market Genie → YouTube Shorts auto-poster
──────────────────────────────────────────────────────────────
Generates a 30-second Short from live Alpaca + breadth data and uploads
to YouTube 3× per day:  9:15 AM (pre-market)  ·  12:00 PM (midday)  ·  4:15 PM (EOD)

Setup (one-time):
  1. Run youtube_setup.py locally → follow browser OAuth flow
  2. Copy the printed YOUTUBE_TOKEN_JSON value into Railway env vars
  3. Add YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET to Railway env vars
  4. Redeploy — scheduler starts automatically

Dependencies (already in requirements.txt after update):
  google-api-python-client  google-auth-oauthlib  Pillow
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
_CATEGORY_ID = "25"   # News & Politics — best fit for finance updates

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

        # Strip whitespace + fix base64 padding (Railway sometimes trims trailing =)
        token_b64 = token_b64.strip()
        padding   = 4 - (len(token_b64) % 4)
        if padding != 4:
            token_b64 += "=" * padding
        # Try standard then URL-safe base64
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
                # fast_info uses attribute access, not .get()
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
    """Pull live data from Alpaca + breadth + social hot tickers."""
    alp_key  = os.getenv("ALPACA_API_KEY_ID", "")
    alp_sec  = os.getenv("ALPACA_SECRET_KEY", "")
    base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    hdrs     = {"APCA-API-KEY-ID": alp_key, "APCA-API-SECRET-KEY": alp_sec}
    port     = int(os.getenv("PORT", "8080"))

    data = {
        "regime":       "NEUTRAL",
        "regime_score": 50,
        "pnl_today":    0.0,
        "equity":       88_000.0,
        "positions":    [],
        "nq_pct":       0.0,
        "spy_pct":      0.0,
        "qqq_pct":      0.0,
        "vix":          16.5,
        "hot_tickers":  [],   # [{symbol, price, pct, up}]
        "timestamp":    datetime.now().strftime("%b %d, %Y  ·  %I:%M %p ET"),
    }

    # Alpaca account P&L
    try:
        r = requests.get(f"{base_url}/v2/account", headers=hdrs, timeout=5)
        if r.status_code == 200:
            acc = r.json()
            data["equity"]    = float(acc.get("equity", 88_000))
            data["pnl_today"] = float(acc.get("equity", 88_000)) - float(acc.get("last_equity", 88_000))
    except Exception as e:
        print(f"[YouTube] Account fetch error: {e}")

    # Alpaca open positions
    try:
        r = requests.get(f"{base_url}/v2/positions", headers=hdrs, timeout=5)
        if r.status_code == 200:
            positions = r.json() if isinstance(r.json(), list) else []
            data["positions"] = [
                {
                    "symbol":          p.get("symbol", "?"),
                    "side":            p.get("side", "long").upper(),
                    "unrealized_pl":   float(p.get("unrealized_pl", 0)),
                    "unrealized_plpc": float(p.get("unrealized_plpc", 0)) * 100,
                }
                for p in positions
            ]
    except Exception as e:
        print(f"[YouTube] Positions fetch error: {e}")

    # Breadth / regime + social hot tickers from local server
    hot_syms = []
    try:
        r = requests.get(f"http://localhost:{port}/api/breadth", timeout=5)
        if r.status_code == 200:
            bd    = r.json()
            score = float(bd.get("score", 50) or 50)
            data["regime_score"] = int(score)
            data["regime"]       = "BULLISH" if score >= 70 else ("BEARISH" if score < 40 else "NEUTRAL")
            data["nq_pct"]       = float(bd.get("nq_pct", 0) or 0)
            data["spy_pct"]      = float(bd.get("spy_pct", 0) or 0)
            data["qqq_pct"]      = float(bd.get("qqq_pct", 0) or 0)
            data["vix"]          = float(bd.get("vix", 16.5) or 16.5)
            # Pull social hot tickers if breadth exposes them
            hot_syms = [t.get("symbol", t) if isinstance(t, dict) else t
                        for t in (bd.get("hot_tickers") or bd.get("social_hot") or [])]
    except Exception as e:
        print(f"[YouTube] Breadth fetch error: {e}")

    # Try live surges endpoint for hot tickers
    if not hot_syms:
        try:
            r = requests.get(f"http://localhost:{port}/api/live/surges", timeout=5)
            if r.status_code == 200:
                surges = r.json() if isinstance(r.json(), list) else []
                hot_syms = [s.get("symbol", "") for s in surges[:6] if s.get("symbol")]
        except Exception:
            pass

    if not hot_syms:
        hot_syms = _HOT_FALLBACK

    # Fetch price/% moves for hot tickers
    data["hot_tickers"] = _fetch_ticker_moves(hot_syms[:6])

    return data


# ── Font loader ───────────────────────────────────────────────────────────────
def _load_font(size: int, bold: bool = False):
    from PIL import ImageFont
    suffix = "-Bold" if bold else ""
    # Bundled fonts (committed to repo) — always available on Railway
    _here = Path(__file__).parent
    candidates = [
        str(_here / "fonts" / f"DejaVuSans{suffix}.ttf"),   # bundled ← first priority
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
    # Last resort: PIL default (11px bitmap) — text will be tiny but won't crash
    print(f"[YouTube] ⚠️  No truetype font found — text will render small")
    return ImageFont.load_default()


# ── Image generation ──────────────────────────────────────────────────────────
def _regime_rgb(regime: str):
    return {
        "BULLISH": (74, 222, 128),
        "BEARISH": (248, 113, 113),
        "NEUTRAL": (250, 204, 21),
    }.get(regime, (156, 163, 175))


def _draw_bar(d, x, y, w, h, pct, max_pct=5.0, color=(74,222,128), bg=(20,30,46)):
    """Draw a horizontal % bar for visual impact."""
    d.rounded_rectangle([x, y, x+w, y+h], radius=4, fill=bg)
    fill_w = int(w * min(abs(pct)/max(max_pct,0.01), 1.0))
    if fill_w > 4:
        d.rounded_rectangle([x, y, x+fill_w, y+h], radius=4, fill=color)


def _generate_frame(data: dict, trigger: str):
    """Return a 1080x1920 PIL Image -- hero-mover layout for maximum virality."""
    from PIL import Image, ImageDraw

    W, H  = _VIDEO_W, _VIDEO_H
    img   = Image.new("RGB", (W, H), (8, 12, 18))
    d     = ImageDraw.Draw(img)
    PAD   = 56

    # Fonts
    fnt_tiny   = _load_font(40)
    fnt_sub    = _load_font(50)
    fnt_label  = _load_font(46)
    fnt_badge  = _load_font(64, bold=True)
    fnt_logo   = _load_font(88, bold=True)
    fnt_hero_s = _load_font(80, bold=True)
    fnt_hero_p = _load_font(200, bold=True)
    fnt_row_s  = _load_font(66, bold=True)
    fnt_row_p  = _load_font(58, bold=True)
    fnt_row_pr = _load_font(48)
    fnt_pnl    = _load_font(148, bold=True)
    fnt_pct    = _load_font(68, bold=True)
    fnt_pos    = _load_font(64, bold=True)
    fnt_pos_dt = _load_font(48)

    rc        = _regime_rgb(data["regime"])
    pnl       = data["pnl_today"]
    pnl_color = (74, 222, 128) if pnl >= 0 else (248, 113, 113)
    pnl_sign  = "+" if pnl >= 0 else "-"

    # Sort hot tickers by absolute % -- biggest mover is hero
    hot = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)

    def ic(v): return (74, 222, 128) if v >= 0 else (248, 113, 113)

    # TOP BAR
    d.rectangle([0, 0, W, 118], fill=(12, 18, 28))
    d.text((PAD, 14), "MARKET GENIE", font=fnt_logo, fill=(255, 255, 255))
    tlabels = {"premarket": "PRE-MARKET", "midday": "MIDDAY", "eod": "END OF DAY", "afterhours": "AFTER HOURS"}
    d.text((W - PAD, 16), tlabels.get(trigger, "LIVE"), font=fnt_sub, fill=rc, anchor="ra")
    d.text((W - PAD, 72), data["timestamp"], font=fnt_tiny, fill=(60, 75, 92), anchor="ra")

    # REGIME + INDEX ROW
    regime = data["regime"];  score = data["regime_score"]
    bt = f"{regime}  {score}"
    try:    bw = int(d.textlength(bt, font=fnt_badge))
    except: bw = len(bt) * 38
    d.rounded_rectangle([PAD-8, 134, PAD+bw+32, 216], radius=20,
                         fill=(rc[0]//6, rc[1]//6, rc[2]//6), outline=rc, width=3)
    d.text((PAD+8, 142), bt, font=fnt_badge, fill=rc)

    nq  = data["nq_pct"];  spy = data.get("spy_pct", 0.0)
    idx_pills = [
        (f"NQ {nq:+.2f}%",  ic(nq)),
        (f"SPY {spy:+.2f}%", ic(spy)),
        (f"VIX {data['vix']:.1f}", (248, 113, 113) if data["vix"] > 20 else (100, 112, 128)),
    ]
    px = PAD + bw + 54
    for txt, col in idx_pills:
        try:    tw = int(d.textlength(txt, font=fnt_label))
        except: tw = len(txt) * 27
        d.rounded_rectangle([px-6, 140, px+tw+10, 214], radius=10, fill=(18, 28, 44))
        d.text((px, 148), txt, font=fnt_label, fill=col)
        px += tw + 36

    # HERO MOVER
    y = 236
    if hot:
        h0  = hot[0]
        hc  = ic(h0["up"] * 2 - 1)  # (74,222,128) if up else (248,113,113)
        hc  = (74, 222, 128) if h0["up"] else (248, 113, 113)
        ha  = "+" if h0["up"] else ""

        d.rounded_rectangle([PAD-8, y, W-PAD+8, y+330], radius=24,
                              fill=(hc[0]//10, hc[1]//10, hc[2]//10))
        d.rounded_rectangle([PAD-8, y, W-PAD+8, y+330], radius=24, outline=hc, width=3)

        d.text((W//2, y+26), "TOP MOVER TODAY", font=fnt_label,
               fill=(hc[0]//2, hc[1]//2, hc[2]//2), anchor="mm")
        d.text((PAD+18, y+62), h0["symbol"], font=fnt_hero_s, fill=(255, 255, 255))
        d.text((W-PAD-18, y+70), f"${h0['price']:,.2f}", font=fnt_sub,
               fill=(160, 170, 185), anchor="ra")
        d.text((W//2, y+178), ha + str(round(abs(h0['pct']),2)) + '%', font=fnt_hero_p, fill=hc, anchor='mm')
        _draw_bar(d, PAD+18, y+302, W-PAD*2-8, 16, h0["pct"],
                  max_pct=max(abs(h0["pct"]), 2), color=hc)
        y += 348

    # SUPPORTING MOVERS (2-column grid)
    rest = hot[1:5]
    if rest:
        d.text((PAD, y+4), "TODAY'S MOVERS", font=fnt_label, fill=(72, 86, 104))
        y += 52
        cw = (W - PAD*2 - 14) // 2

        for i, tk in enumerate(rest):
            col = i % 2
            cx  = PAD + col * (cw + 14)
            tc  = (74, 222, 128) if tk["up"] else (248, 113, 113)
            ar  = "+" if tk["up"] else ""

            d.rounded_rectangle([cx-4, y-4, cx+cw+4, y+118], radius=16, fill=(14, 22, 34))
            d.rounded_rectangle([cx-4, y-4, cx+cw+4, y+118], radius=16,
                                  outline=(tc[0]//5, tc[1]//5, tc[2]//5), width=2)
            d.text((cx+10, y+6), tk["symbol"], font=fnt_row_s, fill=(255, 255, 255))
            d.text((cx+10, y+66), f"${tk['price']:,.2f}", font=fnt_row_pr, fill=(140, 155, 170))
            d.text((cx+cw-6, y+6), f"{ar}{tk['pct']:+.2f}%", font=fnt_row_p, fill=tc, anchor="ra")
            _draw_bar(d, cx+10, y+98, cw-18, 12, tk["pct"],
                      max_pct=max(abs(tk["pct"]), 1), color=tc)

            if col == 1:
                y += 132

        if len(rest) % 2 == 1:
            y += 132

    # AI LIVE TRADES
    y = max(y + 14, 1200)
    d.line([(PAD, y), (W-PAD, y)], fill=(24, 36, 52), width=2)
    d.text((PAD, y+8), "AI LIVE TRADES", font=fnt_sub, fill=(72, 86, 104))
    y += 66

    for pos in data.get("positions", [])[:2]:
        sym = pos["symbol"];  side = pos["side"]
        upl = pos["unrealized_pl"];  uplpct = pos["unrealized_plpc"]
        sc  = (74, 222, 128) if side == "LONG" else (248, 113, 113)
        pc  = (74, 222, 128) if upl >= 0 else (248, 113, 113)
        ps  = "+" if upl >= 0 else "-"

        d.rounded_rectangle([PAD-8, y, W-PAD+8, y+92], radius=14, fill=(14, 22, 34))
        d.text((PAD+12, y+10), sym, font=fnt_pos, fill=(255, 255, 255))
        try:    bx = d.textbbox((PAD+12, y+10), sym, font=fnt_pos)[2] + 16
        except: bx = PAD + 12 + len(sym)*42 + 16
        d.rounded_rectangle([bx, y+16, bx+120, y+66], radius=8,
                              fill=(sc[0]//5, sc[1]//5, sc[2]//5))
        d.text((bx+10, y+20), side, font=fnt_pos_dt, fill=sc)
        d.text((W-PAD, y+8),  f"{ps}${abs(upl):,.0f}",       font=fnt_pos,    fill=pc, anchor="ra")
        d.text((W-PAD, y+56), f"({ps}{abs(uplpct):.2f}%)",   font=fnt_pos_dt, fill=pc, anchor="ra")
        y += 104

    if not data.get("positions"):
        d.text((PAD+12, y+8), "No open positions", font=fnt_pos, fill=(36, 50, 66))
        y += 76

    # P&L
    pnl_y = max(y + 14, 1560)
    d.line([(PAD, pnl_y), (W-PAD, pnl_y)], fill=(24, 36, 52), width=2)

    if trigger == "premarket" and abs(pnl) < 1:
        d.text((PAD, pnl_y+8), "ACCOUNT EQUITY", font=fnt_sub, fill=(72, 86, 104))
        d.text((PAD, pnl_y+66), f"${data['equity']:,.0f}", font=fnt_pnl, fill=(220, 230, 240))
        d.text((W-PAD, pnl_y+96), "Opens 9:30 AM ET", font=fnt_pos_dt, fill=(72, 86, 104), anchor="ra")
    else:
        d.text((PAD, pnl_y+8), "TODAY'S P&L", font=fnt_sub, fill=(72, 86, 104))
        d.text((PAD, pnl_y+66), f"{pnl_sign}${abs(pnl):,.0f}", font=fnt_pnl, fill=pnl_color)
        if data["equity"] > 0:
            pd = (pnl / max(data["equity"]-pnl, 1)) * 100
            d.text((W-PAD, pnl_y+96), f"{pnl_sign}{abs(pd):.2f}%", font=fnt_pct, fill=pnl_color, anchor="ra")

    # Footer CTA
    d.text((W//2, H-84), "Follow for daily AI trade alerts", font=fnt_sub, fill=(68, 84, 104), anchor="mm")
    d.text((W//2, H-38), "PAPER TRADING  NOT FINANCIAL ADVICE", font=fnt_tiny, fill=(40, 54, 68), anchor="mm")

    return img



# ── Video creation ────────────────────────────────────────────────────────────
def _create_short(frame, output_path: str) -> bool:
    """Write a 30-second MP4 Short using ffmpeg Ken-Burns zoom from a still frame."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        frame.save(tmp.name, "PNG")
        png_path = tmp.name

    fps    = 25
    frames = _VIDEO_SECS * fps  # 750 frames

    # Use bundled ffmpeg from imageio-ffmpeg (no system install needed)
    try:
        import imageio_ffmpeg
        ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ffmpeg_bin = "ffmpeg"   # fallback to PATH if imageio-ffmpeg unavailable

    cmd = [
        ffmpeg_bin, "-y",
        "-loop", "1", "-i", png_path,
        "-vf",
        (
            f"zoompan="
            f"z='min(zoom+0.0006,1.12)':"
            f"x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':"
            f"d={frames}:"
            f"s={_VIDEO_W}x{_VIDEO_H}:"
            f"fps={fps}"
        ),
        "-t", str(_VIDEO_SECS),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "fast",
        "-crf", "22",
        output_path,
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        os.unlink(png_path)
        if res.returncode == 0:
            print(f"[YouTube] ✅ Video created: {output_path}")
            return True
        print(f"[YouTube] ❌ ffmpeg error:\n{res.stderr[-600:]}")
        return False
    except subprocess.TimeoutExpired:
        print("[YouTube] ❌ ffmpeg timed out after 180s")
        return False
    except Exception as e:
        print(f"[YouTube] ❌ ffmpeg exception: {e}")
        return False


# ── Upload ────────────────────────────────────────────────────────────────────
def _upload_to_youtube(service, video_path: str, title: str, description: str) -> bool:
    from googleapiclient.http import MediaFileUpload

    tags = [
        "day trading", "stock market", "algo trading", "AI trading",
        "paper trading", "finance", "investing", "stocks", "trading bot",
        "market analysis", "automated trading", "quant trading",
    ]

    body = {
        "snippet": {
            "title":       title[:100],   # YouTube 100-char limit
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
    trigger: "premarket" | "midday" | "eod"
    """
    print(f"[YouTube] 🎬 Starting {trigger} post...")

    service = _get_yt_service()
    if not service:
        return False

    data  = _fetch_market_data()
    frame = _generate_frame(data, trigger)

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        mp4_path = tmp.name

    if not _create_short(frame, mp4_path):
        return False

    # ── Build title & description ─────────────────────────────────────────────
    date_str  = datetime.now().strftime("%b %d, %Y")
    pnl       = data["pnl_today"]
    pnl_sign  = "+" if pnl >= 0 else "-"
    regime    = f"{data['regime']} {data['regime_score']}"

    hot  = data.get("hot_tickers", [])
    # Build ticker snippet — only include tickers with real data
    top_tickers = [
        f"{t['symbol']} {'+' if t['up'] else ''}{t['pct']:.1f}%"
        for t in hot[:3] if t.get("price", 0) > 0
    ]
    ticker_str = "  |  ".join(top_tickers)

    titles = {
        "premarket":  f"🤖 Pre-Market Brief {date_str} | {regime} | {ticker_str or 'Top Movers'}",
        "midday":     f"🤖 AI Midday: {pnl_sign}${abs(pnl):,.0f} P&L | {ticker_str or regime} | {date_str}",
        "eod":        f"🤖 AI Closed {pnl_sign}${abs(pnl):,.0f} Today | {regime} | {ticker_str or 'Top Movers'}",
        "afterhours": f"🤖 After-Hours Wrap | {pnl_sign}${abs(pnl):,.0f} Final | {ticker_str or regime} | {date_str}",
    }
    title = titles.get(trigger, f"AI Trader Update | {date_str}")

    pos_lines = "\n".join(
        f"  {p['symbol']} {p['side']}: {'+' if p['unrealized_pl'] >= 0 else '-'}${abs(p['unrealized_pl']):,.0f}"
        for p in data["positions"]
    ) or "  No open positions"

    description = (
        f"Market Genie AI Auto-Trader — {trigger.upper()} UPDATE\n\n"
        f"Regime: {regime}\n"
        f"Today's P&L: {pnl_sign}${abs(pnl):,.2f}\n"
        f"NQ Futures: {data['nq_pct']:+.2f}%\n"
        f"VIX: {data['vix']:.1f}\n\n"
        f"Open Positions ({len(data['positions'])}):\n{pos_lines}\n\n"
        f"⚠️ PAPER TRADING ONLY — NOT FINANCIAL ADVICE\n"
        f"This is an AI-powered paper trading simulation for educational purposes only.\n\n"
        f"#daytrading #stocks #algotrading #AItrading #stockmarket #finance #investing"
    )

    success = _upload_to_youtube(service, mp4_path, title, description)

    try:
        os.unlink(mp4_path)
    except Exception:
        pass

    return success


# ── Scheduler loop ────────────────────────────────────────────────────────────
# Fires 3× per trading day using the same while-True pattern as _alp_eod_loop.
# Times (all ET):  9:15 AM pre-market  |  12:00 PM midday  |  4:15 PM EOD

_YT_POSTED_FILE = "/tmp/youtube_posted.json"


def _load_posted() -> dict:
    """Load posted-keys dict from disk (survives process restarts / redeploys)."""
    try:
        if Path(_YT_POSTED_FILE).exists():
            with open(_YT_POSTED_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_posted(d: dict):
    """Persist posted-keys dict to disk."""
    try:
        # Keep only keys from the last 7 days to avoid unbounded growth
        from datetime import datetime as _dt, timedelta
        cutoff = (_dt.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        pruned = {k: v for k, v in d.items() if k[:10] >= cutoff}
        with open(_YT_POSTED_FILE, "w") as f:
            json.dump(pruned, f)
    except Exception:
        pass


def _youtube_scheduler_loop():
    """Background thread: posts to YouTube at 9:15, 12:00, 16:15, 17:30 ET on weekdays.
    Uses a disk-persisted posted-keys dict so Railway redeploys don't cause duplicate posts.
    """
    import pytz
    from datetime import time as dtime

    # Load history from disk — survives restarts within the same day
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
            is_wday  = et_now.weekday() <= 4   # Mon–Fri

            if not os.getenv("YOUTUBE_TOKEN_JSON"):
                time.sleep(60)
                continue

            if is_wday:
                for start, end, trigger in slots:
                    key = f"{date_key}_{trigger}"
                    if start <= t < end and key not in _posted:
                        _posted[key] = True
                        _save_posted(_posted)   # ← persist before spawning thread
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
    print("[YouTube] ✅ Scheduler started — posts at 9:15 AM, 12:00 PM, 4:15 PM ET (weekdays)")
