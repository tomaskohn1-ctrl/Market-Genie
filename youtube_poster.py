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
    """1080x1920 Short — layout changes by trigger type for max viewer value."""
    from PIL import Image, ImageDraw
    W, H = _VIDEO_W, _VIDEO_H
    img  = Image.new("RGB", (W, H), (8, 12, 18))
    d    = ImageDraw.Draw(img)
    PAD  = 56

    # Fonts
    fnt_tiny  = _load_font(40)
    fnt_sub   = _load_font(50)
    fnt_lbl   = _load_font(46)
    fnt_badge = _load_font(64, bold=True)
    fnt_logo  = _load_font(86, bold=True)
    fnt_sym   = _load_font(72, bold=True)
    fnt_hero  = _load_font(190, bold=True)
    fnt_med   = _load_font(58, bold=True)
    fnt_sml   = _load_font(50)
    fnt_pnl   = _load_font(144, bold=True)
    fnt_pct   = _load_font(66, bold=True)
    fnt_conf  = _load_font(54, bold=True)

    rc        = _regime_rgb(data["regime"])
    pnl       = data["pnl_today"]
    pnl_color = (74, 222, 128) if pnl >= 0 else (248, 113, 113)
    pnl_sign  = "+" if pnl >= 0 else "-"

    def ic(v): return (74, 222, 128) if v >= 0 else (248, 113, 113)
    def pill(txt, col, px, py, font=None):
        font = font or fnt_lbl
        try:    tw = int(d.textlength(txt, font=font))
        except: tw = len(txt)*27
        d.rounded_rectangle([px-6, py-4, px+tw+10, py+52], radius=10, fill=(18,28,44))
        d.text((px, py+2), txt, font=font, fill=col)
        return px + tw + 32

    # ── TOP BAR ──────────────────────────────────────────────────────────
    d.rectangle([0, 0, W, 112], fill=(11, 17, 27))
    d.text((PAD, 12), "MARKET GENIE", font=fnt_logo, fill=(255, 255, 255))
    tlabels = {"premarket": "PRE-MARKET", "midday": "MIDDAY",
               "eod": "END OF DAY", "afterhours": "AFTER HOURS"}
    d.text((W-PAD, 14), tlabels.get(trigger, "LIVE"), font=fnt_sub, fill=rc, anchor="ra")
    d.text((W-PAD, 68), data["timestamp"], font=fnt_tiny, fill=(58,72,90), anchor="ra")

    # ── REGIME + INDEX PILLS ─────────────────────────────────────────────
    regime = data["regime"];  score = data["regime_score"]
    bt = f"{regime}  {score}"
    try:    bw = int(d.textlength(bt, font=fnt_badge))
    except: bw = len(bt)*38
    d.rounded_rectangle([PAD-8, 128, PAD+bw+30, 210], radius=18,
                         fill=(rc[0]//6,rc[1]//6,rc[2]//6), outline=rc, width=3)
    d.text((PAD+8, 136), bt, font=fnt_badge, fill=rc)

    nq=data["nq_pct"]; spy=data.get("spy_pct",0.0)
    px = PAD+bw+52
    px = pill(f"NQ {nq:+.2f}%",  ic(nq),  px, 136)
    px = pill(f"SPY {spy:+.2f}%", ic(spy), px, 136)
    pill(f"VIX {data['vix']:.1f}",
         (248,113,113) if data["vix"]>20 else (100,112,128), px, 136)

    y = 228  # content starts here

    hot      = sorted(data.get("hot_tickers",[]), key=lambda t:abs(t.get("pct",0)), reverse=True)
    signals  = data.get("ai_signals", [])
    positions = data.get("positions", [])

    # ══════════════════════════════════════════════════════════════════════
    # PRE-MARKET layout: AI Watchlist + top signals + futures
    # ══════════════════════════════════════════════════════════════════════
    if trigger == "premarket":
        # Section: What is the AI watching today?
        d.text((PAD, y), "WHAT AI IS WATCHING TODAY", font=fnt_sub,
               fill=(255,200,50))
        d.line([(PAD, y+54),(W-PAD,y+54)], fill=(50,70,40), width=2)
        y += 66

        # Top AI signals (direction + confidence bar)
        top_sigs = [s for s in signals if s.get("confidence",0)>60][:5]
        if not top_sigs:
            top_sigs = [{"symbol": s, "direction": "bull", "confidence": 70, "streak": 2, "both_agree": 1}
                        for s in ["TQQQ","QQQ","SPY","NVDA","TSLA"]]

        for sig in top_sigs[:5]:
            sym  = sig["symbol"]
            conf = sig.get("confidence", 70)
            dirn = sig.get("direction","bull")
            bull = "bull" in dirn.lower()
            sc   = (74,222,128) if bull else (248,113,113)
            lbl  = "BULL" if bull else "BEAR"

            d.rounded_rectangle([PAD-8, y, W-PAD+8, y+96], radius=14, fill=(14,22,34))

            # Symbol
            d.text((PAD+10, y+12), sym, font=fnt_sym, fill=(255,255,255))

            # Bull/Bear pill
            try:    sx = d.textbbox((PAD+10,y+12),sym,font=fnt_sym)[2]+14
            except: sx = PAD+10+len(sym)*44+14
            d.rounded_rectangle([sx, y+18, sx+110, y+70], radius=8,
                                  fill=(sc[0]//5,sc[1]//5,sc[2]//5))
            d.text((sx+10, y+22), lbl, font=fnt_sml, fill=sc)

            # Confidence bar + pct
            bar_x = sx+130; bar_w = W-PAD-bar_x-10
            _draw_bar(d, bar_x, y+36, bar_w, 20, conf, max_pct=100,
                      color=sc, bg=(20,30,46))
            d.text((W-PAD, y+12), f"{conf:.0f}% conf",
                   font=fnt_sml, fill=sc, anchor="ra")

            y += 108

        # Futures / market open setup
        y += 12
        d.line([(PAD,y),(W-PAD,y)], fill=(28,40,58), width=2)
        d.text((PAD, y+8), "MARKET OPENS 9:30 AM ET", font=fnt_sub, fill=(255,200,50))
        y += 68
        d.text((PAD, y), f"NQ Futures:  {nq:+.2f}%", font=fnt_med,
               fill=ic(nq))
        y += 76
        d.text((PAD, y), f"Regime:  {regime}  ({score})", font=fnt_med, fill=rc)
        y += 76

    # ══════════════════════════════════════════════════════════════════════
    # MIDDAY / EOD / AFTERHOURS layout: Social trending + AI trades + P&L
    # ══════════════════════════════════════════════════════════════════════
    else:
        # HERO: biggest mover
        if hot:
            h0 = hot[0]
            hc = ic(1 if h0["up"] else -1)
            ha = "+" if h0["up"] else ""

            d.rounded_rectangle([PAD-8, y, W-PAD+8, y+296], radius=22,
                                  fill=(hc[0]//10, hc[1]//10, hc[2]//10))
            d.rounded_rectangle([PAD-8, y, W-PAD+8, y+296], radius=22,
                                  outline=hc, width=3)
            d.text((W//2, y+22), "BIGGEST MOVER", font=fnt_lbl,
                   fill=(hc[0]//2,hc[1]//2,hc[2]//2), anchor="mm")
            d.text((PAD+18, y+56), h0["symbol"], font=fnt_sym, fill=(255,255,255))
            d.text((W-PAD-18, y+64), f"${h0['price']:,.2f}", font=fnt_sub,
                   fill=(155,165,180), anchor="ra")
            hero_pct = f"{ha}{abs(h0['pct']):.2f}%"
            d.text((W//2, y+172), hero_pct, font=fnt_hero, fill=hc, anchor="mm")
            _draw_bar(d, PAD+18, y+264, W-PAD*2-8, 18, h0["pct"],
                      max_pct=max(abs(h0["pct"]),2), color=hc)
            y += 314

        # SOCIAL TRENDING (3 tickers, smaller)
        rest = hot[1:4]
        if rest:
            d.text((PAD, y+4), "SOCIAL TRENDING", font=fnt_lbl, fill=(107,120,140))
            y += 50
            cw = (W-PAD*2-12)//3
            for i, tk in enumerate(rest):
                cx = PAD + i*(cw+6)
                tc = ic(1 if tk["up"] else -1)
                ar = "+" if tk["up"] else ""
                d.rounded_rectangle([cx-4,y-4,cx+cw+4,y+106], radius=14, fill=(14,22,34))
                d.text((cx+8,y+6),  tk["symbol"],           font=fnt_med, fill=(255,255,255))
                d.text((cx+8,y+60), f"${tk['price']:,.0f}", font=fnt_sml, fill=(130,145,160))
                d.text((cx+cw-4,y+6), f"{ar}{abs(tk['pct']):.1f}%",
                       font=fnt_conf, fill=tc, anchor="ra")
                _draw_bar(d, cx+8, y+88, cw-16, 10, tk["pct"],
                          max_pct=max(abs(tk["pct"]),1), color=tc)
            y += 122

        # AI SIGNALS (top 3 high-confidence)
        top_sigs = [s for s in signals if s.get("confidence",0)>65][:3]
        if top_sigs:
            y += 4
            d.line([(PAD,y),(W-PAD,y)], fill=(24,36,52), width=2)
            d.text((PAD, y+6), "AI SIGNALS RIGHT NOW", font=fnt_lbl, fill=(107,120,140))
            y += 54
            for sig in top_sigs:
                sym  = sig["symbol"]; conf = sig.get("confidence",70)
                bull = "bull" in sig.get("direction","bull").lower()
                sc   = (74,222,128) if bull else (248,113,113)
                lbl  = "BULL" if bull else "BEAR"
                d.rounded_rectangle([PAD-8,y,W-PAD+8,y+76], radius=12, fill=(14,22,34))
                d.text((PAD+10,y+10), sym, font=fnt_med, fill=(255,255,255))
                try:    bx2 = d.textbbox((PAD+10,y+10),sym,font=fnt_med)[2]+12
                except: bx2 = PAD+10+len(sym)*36+12
                d.rounded_rectangle([bx2,y+14,bx2+100,y+58], radius=8,
                                      fill=(sc[0]//5,sc[1]//5,sc[2]//5))
                d.text((bx2+8,y+18), lbl, font=fnt_sml, fill=sc)
                bar_x2 = bx2+118
                _draw_bar(d, bar_x2, y+26, W-PAD-bar_x2-10, 16, conf,
                          max_pct=100, color=sc, bg=(20,30,46))
                d.text((W-PAD,y+8), f"{conf:.0f}%", font=fnt_conf, fill=sc, anchor="ra")
                y += 88

        # OPEN POSITIONS
        if positions:
            y += 4
            d.line([(PAD,y),(W-PAD,y)], fill=(24,36,52), width=2)
            d.text((PAD,y+6), "AI OPEN TRADES", font=fnt_lbl, fill=(107,120,140))
            y += 54
            for pos in positions[:2]:
                sym2=pos["symbol"]; side2=pos["side"]
                upl=pos["unrealized_pl"]; uplpct=pos["unrealized_plpc"]
                sc2=(74,222,128) if side2=="LONG" else (248,113,113)
                pc2=(74,222,128) if upl>=0 else (248,113,113)
                ps2="+" if upl>=0 else "-"
                d.rounded_rectangle([PAD-8,y,W-PAD+8,y+86], radius=12, fill=(14,22,34))
                d.text((PAD+10,y+8),sym2,font=fnt_med,fill=(255,255,255))
                try:    bx3=d.textbbox((PAD+10,y+8),sym2,font=fnt_med)[2]+12
                except: bx3=PAD+10+len(sym2)*36+12
                d.rounded_rectangle([bx3,y+12,bx3+110,y+58],radius=8,fill=(sc2[0]//5,sc2[1]//5,sc2[2]//5))
                d.text((bx3+8,y+16),side2,font=fnt_sml,fill=sc2)
                d.text((W-PAD,y+6),f"{ps2}${abs(upl):,.0f}",font=fnt_med,fill=pc2,anchor="ra")
                d.text((W-PAD,y+50),f"({ps2}{abs(uplpct):.2f}%)",font=fnt_sml,fill=pc2,anchor="ra")
                y += 98

    # ── P&L FOOTER ────────────────────────────────────────────────────────
    pnl_y = max(y+12, 1380)
    d.line([(PAD,pnl_y),(W-PAD,pnl_y)], fill=(24,36,52), width=2)
    if trigger == "premarket" and abs(pnl) < 1:
        d.text((PAD,pnl_y+8),"ACCOUNT EQUITY",font=fnt_sub,fill=(107,120,140))
        d.text((PAD,pnl_y+62),f"${data['equity']:,.0f}",font=fnt_pnl,fill=(210,225,240))
    else:
        d.text((PAD,pnl_y+8),"TODAY'S P&L",font=fnt_sub,fill=(107,120,140))
        d.text((PAD,pnl_y+62),f"{pnl_sign}${abs(pnl):,.0f}",font=fnt_pnl,fill=pnl_color)
        if data["equity"]>0:
            pd=(pnl/max(data["equity"]-pnl,1))*100
            d.text((W-PAD,pnl_y+92),f"{pnl_sign}{abs(pd):.2f}%",font=fnt_pct,fill=pnl_color,anchor="ra")

    d.text((W//2,H-86),"Follow for daily AI signals",font=fnt_sub,fill=(68,84,104),anchor="mm")
    d.text((W//2,H-40),"PAPER TRADING  NOT FINANCIAL ADVICE",font=fnt_tiny,fill=(40,54,68),anchor="mm")

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
