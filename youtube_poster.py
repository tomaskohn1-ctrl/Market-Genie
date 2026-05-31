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

    # One call to the server's rich data endpoint
    try:
        r = requests.get(f"http://localhost:{port}/api/youtube/data", timeout=8)
        if r.status_code == 200:
            srv = r.json()
            defaults.update({k: srv[k] for k in srv if k in defaults})
            # social_hot → hot_tickers (add price/pct via yfinance)
            social_syms = [t["symbol"] for t in srv.get("social_hot", [])[:8]
                           if t.get("symbol")]
            if social_syms:
                defaults["hot_tickers"] = _fetch_ticker_moves(social_syms)
    except Exception as e:
        print(f"[YouTube] Data fetch error: {e}")

    # Fallback: if still no hot tickers, use mega-cap list
    if not defaults["hot_tickers"]:
        defaults["hot_tickers"] = _fetch_ticker_moves(_HOT_FALLBACK[:6])

    return defaults


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
    """
    Bloomberg-terminal-inspired grid dashboard.
    Fixed horizontal panels, amber section headers, zero floating elements.
    """
    from PIL import Image, ImageDraw

    W, H  = _VIDEO_W, _VIDEO_H
    BG    = (8, 11, 16)           # near-black
    PANEL = (13, 18, 27)          # panel background
    ALT   = (17, 24, 36)          # alternate row
    DIV   = (30, 42, 58)          # divider line
    AMBER = (220, 155, 40)        # Bloomberg-style section labels
    WHITE = (225, 235, 248)       # primary data text
    DIM   = (110, 128, 150)       # secondary/muted text
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
    fnt_lbl   = _load_font(36)     # amber section label
    fnt_logo  = _load_font(82, bold=True)

    rc  = _regime_rgb(data["regime"])
    pnl = data["pnl_today"]
    pcs = "+" if pnl >= 0 else "-"
    pc  = ic(pnl)

    hot     = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct",0)), reverse=True)
    signals = [s for s in data.get("ai_signals",[]) if s.get("confidence",0) > 65][:3]
    pos     = data.get("positions", [])
    trigger_lbl = {"premarket":"PRE-MARKET","midday":"MIDDAY",
                   "eod":"END OF DAY","afterhours":"AFTER HOURS"}.get(trigger,"LIVE")

    def div(y):
        d.line([(0,y),(W,y)], fill=DIV, width=1)

    def sec(y, left, right="", right_col=DIM):
        d.rectangle([0, y, W, y+40], fill=ALT)
        d.text((PAD, y+4), left.upper(), font=fnt_lbl, fill=AMBER)
        if right:
            d.text((W-PAD, y+4), right.upper(), font=fnt_lbl, fill=right_col, anchor="ra")
        return y + 40

    # ══ HEADER (h=106) ═══════════════════════════════════════════
    d.rectangle([0, 0, W, 106], fill=(10, 14, 22))
    d.text((PAD, 10), "MARKET GENIE", font=fnt_logo, fill=WHITE)
    d.text((W-PAD, 10), trigger_lbl, font=fnt_sm, fill=rc, anchor="ra")
    d.text((W-PAD, 62), data["timestamp"], font=fnt_xs, fill=DIM, anchor="ra")
    div(106)

    # ══ REGIME + INDICES (h=130) ══════════════════════════════════
    d.rectangle([0, 107, W, 237], fill=PANEL)
    regime = data["regime"];  score = data["regime_score"]
    nq = data["nq_pct"];  spy = data.get("spy_pct", 0.0);  vix = data["vix"]
    # Regime pill
    rtext = f" {regime}  {score} "
    try:    rw = int(d.textlength(rtext, font=fnt_lg))
    except: rw = len(rtext)*42
    d.rounded_rectangle([PAD, 120, PAD+rw+4, 192], radius=10,
                         fill=(rc[0]//7,rc[1]//7,rc[2]//7))
    d.rounded_rectangle([PAD, 120, PAD+rw+4, 192], radius=10, outline=rc, width=2)
    d.text((PAD+8, 126), rtext, font=fnt_lg, fill=rc)
    # Indices inline
    ix = PAD + rw + 36
    d.text((ix,      126), f"NQ {nq:+.2f}%",  font=fnt_sm, fill=ic(nq))
    d.text((ix+240,  126), f"SPY {spy:+.2f}%", font=fnt_sm, fill=ic(spy))
    vc = RED if vix > 20 else DIM
    d.text((W-PAD,   126), f"VIX {vix:.1f}",  font=fnt_sm, fill=vc, anchor="ra")
    div(237)

    # ══ BIGGEST MOVER (h=240) ═════════════════════════════════════
    y = sec(238, "Biggest mover today")
    d.rectangle([0, y, W, y+200], fill=PANEL)
    if hot:
        h0 = hot[0]
        hc = GREEN if h0["up"] else RED
        ha = "+" if h0["up"] else "-"
        d.text((PAD+12, y+12), h0["symbol"], font=fnt_xl, fill=WHITE)
        d.text((W-PAD-12, y+18), f"${h0['price']:,.2f}", font=fnt_md, fill=DIM, anchor="ra")
        hero_txt = f"{ha}{abs(h0['pct']):.2f}%"
        d.text((W//2, y+106), hero_txt, font=fnt_hero, fill=hc, anchor="mm")
        # Thin accent bar at bottom of panel
        bfill = int((W-PAD*2) * min(abs(h0["pct"])/10, 1.0))
        d.rectangle([PAD, y+184, W-PAD, y+192], fill=DIV)
        if bfill > 4:
            d.rectangle([PAD, y+184, PAD+bfill, y+192], fill=hc)
    else:
        d.text((W//2, y+100), "Awaiting data", font=fnt_lg, fill=DIM, anchor="mm")
    y += 200
    div(y)

    # ══ MOVERS TABLE (4 rows × 82px) ═════════════════════════════
    y = sec(y+1, "Market movers", "Change")
    for i, tk in enumerate(hot[1:5]):
        rb = ALT if i%2==0 else PANEL
        d.rectangle([0, y, W, y+82], fill=rb)
        tc  = GREEN if tk["up"] else RED
        ar  = "+" if tk["up"] else "-"
        # Left: symbol
        d.text((PAD+12, y+16), tk["symbol"], font=fnt_lg, fill=WHITE)
        # Center: %
        d.text((W//2, y+20), f"{ar}{abs(tk['pct']):.2f}%", font=fnt_md, fill=tc, anchor="mm")
        # Right: price
        d.text((W-PAD-12, y+16), f"${tk['price']:,.2f}", font=fnt_md, fill=DIM, anchor="ra")
        # mini bar
        bw2 = int((W-PAD*2-24) * min(abs(tk["pct"])/8, 1.0))
        d.rectangle([PAD+12, y+68, W-PAD-12, y+74], fill=DIV)
        if bw2>2: d.rectangle([PAD+12, y+68, PAD+12+bw2, y+74], fill=tc)
        y += 82
    div(y)

    # ══ AI SIGNALS ════════════════════════════════════════════════
    if signals:
        y = sec(y+1, "AI signals", "Conf")
        for i, sig in enumerate(signals):
            rb = ALT if i%2==0 else PANEL
            d.rectangle([0, y, W, y+74], fill=rb)
            bull = "bull" in sig.get("direction","bull").lower()
            sc2  = GREEN if bull else RED
            conf = sig.get("confidence", 70)
            lbl2 = "BULL" if bull else "BEAR"
            d.text((PAD+12, y+14), sig["symbol"], font=fnt_lg, fill=WHITE)
            try:    sx = d.textbbox((PAD+12,y+14),sig["symbol"],font=fnt_lg)[2]+16
            except: sx = PAD+12+len(sig["symbol"])*44+16
            d.rounded_rectangle([sx,y+18,sx+100,y+58],radius=6,fill=(sc2[0]//5,sc2[1]//5,sc2[2]//5))
            d.text((sx+8,y+21),lbl2,font=fnt_xs,fill=sc2)
            bx3 = sx+118; bw3 = W-PAD-12-bx3-80
            d.rectangle([bx3, y+26, bx3+bw3, y+46], fill=DIV)
            fb = int(bw3*min(conf/100,1))
            if fb>2: d.rectangle([bx3,y+26,bx3+fb,y+46],fill=sc2)
            d.text((W-PAD-12,y+14),f"{conf:.0f}%",font=fnt_md,fill=sc2,anchor="ra")
            y += 74
        div(y)

    # ══ OPEN TRADES ════════════════════════════════════════════════
    if pos:
        y = sec(y+1, "AI open trades")
        for p2 in pos[:2]:
            d.rectangle([0, y, W, y+78], fill=PANEL)
            s2  = p2["symbol"]; sd = p2["side"]
            ul  = p2["unrealized_pl"]; up = p2["unrealized_plpc"]
            sc3 = GREEN if sd=="LONG" else RED
            pc3 = GREEN if ul>=0 else RED
            ps3 = "+" if ul>=0 else "-"
            d.text((PAD+12, y+14), s2, font=fnt_lg, fill=WHITE)
            try:    bx4 = d.textbbox((PAD+12,y+14),s2,font=fnt_lg)[2]+14
            except: bx4 = PAD+12+len(s2)*44+14
            d.rounded_rectangle([bx4,y+18,bx4+108,y+58],radius=6,fill=(sc3[0]//5,sc3[1]//5,sc3[2]//5))
            d.text((bx4+8,y+21),sd,font=fnt_xs,fill=sc3)
            d.text((W-PAD-12,y+12),f"{ps3}${abs(ul):,.0f}",font=fnt_lg,fill=pc3,anchor="ra")
            d.text((W-PAD-12,y+56),f"({ps3}{abs(up):.2f}%)",font=fnt_xs,fill=pc3,anchor="ra")
            y += 78
        div(y)

    # ══ P&L (pinned near bottom) ══════════════════════════════════
    pnl_top = max(y+1, H-250)
    d.rectangle([0, pnl_top, W, H-64], fill=PANEL)
    d.text((PAD+12, pnl_top+10), "TODAY'S P&L", font=fnt_lbl, fill=AMBER)
    if trigger == "premarket" and abs(pnl) < 1:
        d.text((PAD+12, pnl_top+52), f"${data['equity']:,.0f}",
               font=_load_font(134, bold=True), fill=WHITE)
        d.text((W-PAD-12, pnl_top+80), "Opens 9:30 AM ET",
               font=fnt_sm, fill=DIM, anchor="ra")
    else:
        d.text((PAD+12, pnl_top+52), f"{pcs}${abs(pnl):,.0f}",
               font=_load_font(134, bold=True), fill=pc)
        if data["equity"] > 0:
            pd = (pnl / max(data["equity"]-pnl, 1)) * 100
            d.text((W-PAD-12, pnl_top+80), f"{pcs}{abs(pd):.2f}%",
                   font=fnt_xl, fill=pc, anchor="ra")

    # ══ FOOTER ════════════════════════════════════════════════════
    div(H-64)
    d.rectangle([0, H-64, W, H], fill=(10, 14, 22))
    d.text((W//2, H-44), "Follow @marketgenie.ai for daily AI signals",
           font=fnt_xs, fill=DIM, anchor="mm")
    d.text((W//2, H-14), "PAPER TRADING  |  NOT FINANCIAL ADVICE",
           font=fnt_nano, fill=DIV, anchor="mm")

    return img



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
