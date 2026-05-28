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
def _fetch_market_data() -> dict:
    """Pull live data from Alpaca + local breadth endpoint."""
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
        "vix":          16.5,
        "timestamp":    datetime.now().strftime("%b %d  ·  %I:%M %p ET"),
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
                    "symbol":        p.get("symbol", "?"),
                    "side":          p.get("side", "long").upper(),
                    "unrealized_pl": float(p.get("unrealized_pl", 0)),
                    "unrealized_plpc": float(p.get("unrealized_plpc", 0)) * 100,
                }
                for p in positions
            ]
    except Exception as e:
        print(f"[YouTube] Positions fetch error: {e}")

    # Breadth / regime from local server
    try:
        r = requests.get(f"http://localhost:{port}/api/breadth", timeout=5)
        if r.status_code == 200:
            bd    = r.json()
            score = float(bd.get("score", 50) or 50)
            data["regime_score"] = int(score)
            data["regime"]       = "BULLISH" if score >= 70 else ("BEARISH" if score < 40 else "NEUTRAL")
            data["nq_pct"]       = float(bd.get("nq_pct", 0) or 0)
            data["vix"]          = float(bd.get("vix", 16.5) or 16.5)
    except Exception as e:
        print(f"[YouTube] Breadth fetch error: {e}")

    return data


# ── Font loader ───────────────────────────────────────────────────────────────
def _load_font(size: int, bold: bool = False):
    from PIL import ImageFont
    candidates = [
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
        f"/usr/share/fonts/truetype/liberation/LiberationSans{'-Bold' if bold else ''}.ttf",
        f"/usr/share/fonts/truetype/ubuntu/Ubuntu-{'B' if bold else 'R'}.ttf",
        f"/usr/share/fonts/truetype/freefont/FreeSans{'Bold' if bold else ''}.ttf",
    ]
    for path in candidates:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


# ── Image generation ──────────────────────────────────────────────────────────
def _regime_rgb(regime: str):
    return {
        "BULLISH": (74, 222, 128),
        "BEARISH": (248, 113, 113),
        "NEUTRAL": (250, 204, 21),
    }.get(regime, (156, 163, 175))


def _generate_frame(data: dict, trigger: str):
    """Return a 1080×1920 PIL Image ready for ffmpeg."""
    from PIL import Image, ImageDraw
    import math, random

    W, H = _VIDEO_W, _VIDEO_H
    img  = Image.new("RGB", (W, H), (13, 17, 23))
    d    = ImageDraw.Draw(img)

    # ── Subtle grid ──────────────────────────────────────────────────────────
    for y in range(0, H, 140):
        d.line([(0, y), (W, y)], fill=(25, 35, 50), width=1)

    # ── Animated equity-curve silhouette ─────────────────────────────────────
    random.seed(7)
    pts = []
    cx0, cy_base, cw, ch = 60, 820, W - 120, 340
    pnl = data["pnl_today"]
    trending_up = pnl >= 0
    for i in range(50):
        prog = i / 49
        # Base trend direction
        base_y = cy_base + ch * (1 - prog) if trending_up else cy_base + ch * prog
        noise  = random.randint(-20, 20)
        pts.append((int(cx0 + cw * prog), int(base_y + noise)))

    # Fill under curve
    fill_pts = pts + [(pts[-1][0], cy_base + ch + 10), (pts[0][0], cy_base + ch + 10)]
    rc = _regime_rgb(data["regime"])
    d.polygon(fill_pts, fill=(*rc, 0) if False else (rc[0] // 8, rc[1] // 8, rc[2] // 8))

    # Curve line
    for i in range(len(pts) - 1):
        d.line([pts[i], pts[i + 1]], fill=rc, width=5)

    # ── Logo ─────────────────────────────────────────────────────────────────
    fnt_logo = _load_font(54, bold=True)
    fnt_tag  = _load_font(28)
    fnt_sm   = _load_font(32)
    fnt_med  = _load_font(44)
    fnt_lg   = _load_font(90, bold=True)
    fnt_xl   = _load_font(130, bold=True)

    d.text((60, 90),  "MARKET GENIE",  font=fnt_logo, fill=(255, 255, 255))
    d.text((60, 158), "AI AUTO-TRADER  ·  PAPER LIVE", font=fnt_tag, fill=(75, 85, 99))

    # ── Regime badge ─────────────────────────────────────────────────────────
    regime     = data["regime"]
    score      = data["regime_score"]
    rc         = _regime_rgb(regime)
    badge_text = f"{regime}  {score}"
    d.rounded_rectangle(
        [56, 208, 400, 268], radius=22,
        fill=(rc[0] // 6, rc[1] // 6, rc[2] // 6),
        outline=rc, width=2,
    )
    d.text((72, 214), badge_text, font=fnt_sm, fill=rc)

    nq   = data["nq_pct"]
    nq_c = (74, 222, 128) if nq >= 0 else (248, 113, 113)
    d.text((420, 222), f"NQ {nq:+.2f}%   VIX {data['vix']:.1f}", font=fnt_sm, fill=(107, 114, 128))

    # ── P&L ──────────────────────────────────────────────────────────────────
    pnl       = data["pnl_today"]
    pnl_color = (74, 222, 128) if pnl >= 0 else (248, 113, 113)
    pnl_sign  = "+" if pnl >= 0 else ""

    d.text((60, 330), "TODAY'S P&L", font=fnt_tag, fill=(75, 85, 99))
    d.text((60, 368), f"{pnl_sign}${abs(pnl):,.0f}", font=fnt_xl, fill=pnl_color)

    eq = data["equity"]
    if eq > 0:
        pct   = (pnl / max(eq - pnl, 1)) * 100
        pct_s = f"{pnl_sign}{abs(pct):.2f}%"
        d.text((60, 520), pct_s, font=fnt_lg, fill=(*pnl_color[:3],))

    # ── Open positions ────────────────────────────────────────────────────────
    positions = data["positions"]
    pos_y     = 1000
    count_lbl = f"OPEN POSITIONS  ({len(positions)})"
    d.text((60, pos_y - 52), count_lbl, font=fnt_tag, fill=(75, 85, 99))
    d.line([(60, pos_y - 14), (W - 60, pos_y - 14)], fill=(30, 40, 58), width=2)

    for pos in positions[:4]:
        sym    = pos["symbol"]
        side   = pos["side"]
        upl    = pos["unrealized_pl"]
        uplpct = pos["unrealized_plpc"]
        sc     = (74, 222, 128) if side == "LONG" else (248, 113, 113)
        pc     = (74, 222, 128) if upl >= 0 else (248, 113, 113)
        ps     = "+" if upl >= 0 else ""

        # Row card
        d.rounded_rectangle([56, pos_y - 6, W - 56, pos_y + 74], radius=14, fill=(20, 28, 42))

        # Symbol
        d.text((80, pos_y + 8), sym, font=fnt_med, fill=(255, 255, 255))

        # Side badge
        sym_w = len(sym) * 26 + 80 + 12
        d.rounded_rectangle([sym_w, pos_y + 14, sym_w + 110, pos_y + 54], radius=8,
                             fill=(sc[0] // 5, sc[1] // 5, sc[2] // 5))
        d.text((sym_w + 8, pos_y + 17), side, font=fnt_sm, fill=sc)

        # P&L right-aligned
        pl_str = f"{ps}${abs(upl):,.0f}  ({ps}{abs(uplpct):.2f}%)"
        d.text((W - 70, pos_y + 8), pl_str, font=fnt_med, fill=pc, anchor="ra")

        pos_y += 104

    if not positions:
        d.text((80, pos_y + 10), "No open positions", font=fnt_med, fill=(75, 85, 99))
        pos_y += 90

    # ── Trigger label ─────────────────────────────────────────────────────────
    labels = {
        "premarket": "PRE-MARKET BRIEF",
        "midday":    "MIDDAY UPDATE",
        "eod":       "END-OF-DAY RECAP",
    }
    d.text((60, pos_y + 80), labels.get(trigger, "LIVE UPDATE"), font=fnt_med, fill=(107, 114, 128))
    d.text((60, pos_y + 136), data["timestamp"], font=fnt_sm, fill=(55, 65, 81))

    # ── Disclaimer ────────────────────────────────────────────────────────────
    disc_y = H - 110
    d.line([(60, disc_y - 18), (W - 60, disc_y - 18)], fill=(25, 35, 50), width=1)
    d.text((W // 2, disc_y), "PAPER TRADING ONLY  ·  NOT FINANCIAL ADVICE",
           font=fnt_tag, fill=(55, 65, 81), anchor="mm")
    d.text((W // 2, disc_y + 44), "@marketgenie.ai  ·  Subscribe for daily updates",
           font=fnt_tag, fill=(55, 65, 81), anchor="mm")

    return img


# ── Video creation ────────────────────────────────────────────────────────────
def _create_short(frame, output_path: str) -> bool:
    """Write a 30-second MP4 Short using ffmpeg Ken-Burns zoom from a still frame."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        frame.save(tmp.name, "PNG")
        png_path = tmp.name

    fps    = 25
    frames = _VIDEO_SECS * fps  # 750 frames

    cmd = [
        "ffmpeg", "-y",
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
    pnl_sign  = "+" if pnl >= 0 else ""
    regime    = f"{data['regime']} {data['regime_score']}"

    titles = {
        "premarket": f"AI Trader Pre-Market Brief | {regime} | {date_str}",
        "midday":    f"AI Trader Midday Update | {pnl_sign}${abs(pnl):,.0f} P&L | {date_str}",
        "eod":       f"AI Trader EOD Recap | {pnl_sign}${abs(pnl):,.0f} Today | {date_str}",
    }
    title = titles.get(trigger, f"AI Trader Update | {date_str}")

    pos_lines = "\n".join(
        f"  {p['symbol']} {p['side']}: {'+' if p['unrealized_pl'] >= 0 else ''}${p['unrealized_pl']:,.0f}"
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

def _youtube_scheduler_loop():
    """Background thread: posts to YouTube at 9:15, 12:00, 16:15 ET on weekdays."""
    import pytz
    _posted = {}   # { "2026-05-28_premarket": True, ... }

    while True:
        try:
            et_now   = datetime.now(pytz.timezone("America/New_York"))
            date_key = et_now.strftime("%Y-%m-%d")
            t        = et_now.time()
            is_wday  = et_now.weekday() <= 4   # Mon–Fri

            # Only post if YouTube creds are configured
            if not os.getenv("YOUTUBE_TOKEN_JSON"):
                time.sleep(60)
                continue

            if is_wday:
                from datetime import time as dtime
                slots = [
                    (dtime(9,  15), dtime(9,  30), "premarket"),
                    (dtime(12, 0),  dtime(12, 15), "midday"),
                    (dtime(16, 15), dtime(16, 30), "eod"),
                ]
                for start, end, trigger in slots:
                    key = f"{date_key}_{trigger}"
                    if start <= t < end and key not in _posted:
                        _posted[key] = True
                        # Run in its own thread so scheduler loop never blocks
                        threading.Thread(
                            target=post_market_update,
                            args=(trigger,),
                            daemon=True,
                            name=f"YT-{trigger}",
                        ).start()
                        # Prune old keys (keep last 30)
                        if len(_posted) > 30:
                            oldest = sorted(_posted.keys())[0]
                            del _posted[oldest]

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
