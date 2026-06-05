"""
patch_youtube.py  — run this ONCE to apply the dashboard visual redesign.
Usage:  python patch_youtube.py
"""
import ast, os, sys

src = open("youtube_poster.py", encoding="utf-8").read()

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: Replace the old Colour helpers block with the full Design System
# ─────────────────────────────────────────────────────────────────────────────
OLD_COLOURS = """# ── Colour helpers ────────────────────────────────────────────────────────────
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
    return (rgb[0] // factor, rgb[1] // factor, rgb[2] // factor)"""

NEW_COLOURS = """# ══════════════════════════════════════════════════════════════════════════════
# DESIGN SYSTEM — matches dashboard.html CSS variables exactly
# ══════════════════════════════════════════════════════════════════════════════
_C = {
    "bg":        (8,   8,   16),
    "surface":   (14,  14,  28),
    "card":      (17,  17,  32),
    "accent":    (91,  127, 255),
    "accent_hi": (123, 155, 255),
    "purple":    (155, 89,  245),
    "blue":      (64,  169, 255),
    "green":     (0,   230, 118),
    "red":       (255, 61,  87),
    "amber":     (255, 179, 0),
    "text":      (226, 232, 240),
    "text_hi":   (247, 250, 252),
    "subtext":   (113, 128, 150),
    "muted":     (74,  85,  104),
    "green_dim": (8,   26,  18),
    "red_dim":   (28,  10,  14),
    "amber_dim": (28,  20,  4),
    "accent_dim":(12,  16,  34),
}

def _regime_color(regime):
    return {"BULLISH": _C["green"], "BEARISH": _C["red"]}.get(regime, _C["amber"])

def _pct_color(v):
    return _C["green"] if v >= 0 else _C["red"]

def _mk_bg():
    try:
        import numpy as np
        arr = __import__("numpy").zeros((_VIDEO_H, _VIDEO_W, 3), dtype=__import__("numpy").uint8)
        for y in range(_VIDEO_H):
            t = y / _VIDEO_H
            arr[y, :] = [int(8+t*5), int(8+t*4), int(16+t*12)]
        from PIL import Image
        return Image.fromarray(arr, "RGB")
    except Exception:
        from PIL import Image
        return Image.new("RGB", (_VIDEO_W, _VIDEO_H), _C["bg"])

def _draw_glow_card(d, x, y, x2, y2, radius=16, accent=None, fill=None):
    ac = accent or _C["accent"]
    bg = fill   or _C["card"]
    glow = tuple(max(0, int(c * 0.10)) for c in ac)
    d.rounded_rectangle([x-3, y-3, x2+3, y2+3], radius=radius+3, fill=glow)
    d.rounded_rectangle([x, y, x2, y2], radius=radius, fill=bg)
    border = tuple(max(0, int(c * 0.35)) for c in ac)
    d.rounded_rectangle([x, y, x2, y2], radius=radius, outline=border, width=2)

def _draw_header(d, W, label_left, label_right, color_right, fnt_l, fnt_r, timestamp=""):
    PAD = 52
    d.rectangle([0, 0, W, 120], fill=_C["surface"])
    d.text((PAD, 60), label_left,  font=fnt_l, fill=_C["accent_hi"], anchor="lm")
    d.text((W-PAD, 60), label_right, font=fnt_r, fill=color_right,   anchor="rm")
    for i in range(W):
        t = i / W
        intensity = 1.0 - abs(2*t - 1)**0.6
        c = tuple(int(cc * intensity) for cc in _C["accent"])
        d.line([(i, 118), (i, 121)], fill=c)
    if timestamp:
        d.text((PAD, 148), timestamp, font=fnt_l, fill=_C["muted"])

def _draw_caption_bar(d, W, H, caption_text, fnt_caption, fnt_nano):
    BAR_H = 105
    d.rectangle([0, H-BAR_H, W, H], fill=_C["surface"])
    for i in range(W):
        t = i / W
        intensity = 1.0 - abs(2*t - 1)**0.6
        c = tuple(int(cc * intensity * 0.8) for cc in _C["accent"])
        d.line([(i, H-BAR_H), (i, H-BAR_H+2)], fill=c)
    d.text((W//2, H-BAR_H+38), caption_text, font=fnt_caption, fill=_C["text"], anchor="mm")
    d.text((W//2, H-20), "marketgenie.ai  NOT FINANCIAL ADVICE", font=fnt_nano, fill=_C["muted"], anchor="mm")

def _draw_breaking_badge(d, W, symbol, pct, fnt):
    d.rectangle([0, 122, W, 218], fill=(140, 10, 28))
    sign = "+" if pct >= 0 else ""
    d.text((W//2, 170), f"BREAKING  {symbol} {sign}{pct:.1f}%", font=fnt, fill=_C["text_hi"], anchor="mm")

def _draw_conf_bar(d, x, y, w, h, pct, color):
    TRACK = (30, 30, 50)
    d.rounded_rectangle([x, y, x+w, y+h], radius=h//2, fill=TRACK)
    fw = max(4, int(w * min(pct/100, 1.0)))
    d.rounded_rectangle([x, y, x+fw, y+h], radius=h//2, fill=color)

def _draw_badge(d, x, y, text, color, fnt):
    try:    bw = int(d.textlength(text, font=fnt)) + 28
    except: bw = 160
    bh = 44
    dim    = tuple(max(0, int(c * 0.15)) for c in color)
    border = tuple(max(0, int(c * 0.60)) for c in color)
    d.rounded_rectangle([x, y, x+bw, y+bh], radius=bh//2, fill=dim)
    d.rounded_rectangle([x, y, x+bw, y+bh], radius=bh//2, outline=border, width=2)
    d.text((x+bw//2, y+bh//2), text, font=fnt, fill=color, anchor="mm")
    return bw

# ── Legacy colour helpers (kept for compatibility) ────────────────────────────
def _regime_rgb(regime):
    return _regime_color(regime)

def _ic(v):
    return _pct_color(v)

def _dim_color(rgb, factor=7):
    return (rgb[0]//factor, rgb[1]//factor, rgb[2]//factor)"""

if OLD_COLOURS not in src:
    print("ERROR: OLD_COLOURS block not found in file")
    sys.exit(1)

src = src.replace(OLD_COLOURS, NEW_COLOURS, 1)
print("Step 1: Design system injected OK")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: Replace old drawing helpers (the duplicate _draw_caption_bar etc.)
# ─────────────────────────────────────────────────────────────────────────────
OLD_HELPERS_MARKER = """# ─────────────────────────────────────────────────────────────────────────────
# SHARED DRAWING HELPERS
# ─────────────────────────────────────────────────────────────────────────────"""

NEW_HELPERS_MARKER = """# ─────────────────────────────────────────────────────────────────────────────
# SHARED DRAWING HELPERS (legacy alert/signal cards — kept for compatibility)
# ─────────────────────────────────────────────────────────────────────────────"""

src = src.replace(OLD_HELPERS_MARKER, NEW_HELPERS_MARKER, 1)

# Remove the old duplicate _draw_caption_bar and _draw_breaking_badge
# that appear in the SLIDE 1 section (just before _generate_hook_frame)
OLD_DUP = """# ═════════════════════════════════════════════════════════════════════════════
# SLIDE 1 — ALERT HOOK FRAME
# Breaking-news alert cards for top movers + regime
# ═════════════════════════════════════════════════════════════════════════════

def _draw_caption_bar(d, W, H, caption_text, fnt_caption, fnt_nano):
    \"\"\"
    Draw a semi-transparent caption bar at the very bottom of the frame.
    This ensures the key message is readable even with no sound.
    \"\"\"
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
    \"\"\"Red BREAKING banner just below the header for big movers (≥5%).\"\"\"""
    d.rectangle([0, 132, W, 222], fill=(180, 15, 15))
    sign = "+" if pct >= 0 else ""
    d.text((W // 2, 177),
           f"⚡ BREAKING  {symbol} {sign}{pct:.1f}%  ⚡",
           font=fnt, fill=(255, 255, 255), anchor="mm")"""

NEW_DUP = """# ═════════════════════════════════════════════════════════════════════════════
# SLIDE 1 — HOOK FRAME
# ═════════════════════════════════════════════════════════════════════════════"""

if OLD_DUP in src:
    src = src.replace(OLD_DUP, NEW_DUP, 1)
    print("Step 2: Duplicate helpers removed OK")
else:
    # Try a shorter match
    OLD_DUP2 = "# ═════════════════════════════════════════════════════════════════════════════\n# SLIDE 1 — ALERT HOOK FRAME"
    NEW_DUP2 = "# ═════════════════════════════════════════════════════════════════════════════\n# SLIDE 1 — HOOK FRAME"
    src = src.replace(OLD_DUP2, NEW_DUP2, 1)
    print("Step 2: Used short match for section header")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: Splice in new slide functions
# ─────────────────────────────────────────────────────────────────────────────
tree  = ast.parse(src)
lines = src.split("\n")

def line_to_offset(lineno):
    return sum(len(l)+1 for l in lines[:lineno-1])

fns = {}
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef):
        if node.name not in fns:
            fns[node.name] = node.lineno

# Find start of _generate_hook_frame and end (start of _generate_trade_setup_slide)
start_line = fns.get("_generate_hook_frame")
end_line   = fns.get("_generate_trade_setup_slide")

if not start_line or not end_line:
    print(f"ERROR: start_line={start_line} end_line={end_line}")
    sys.exit(1)

start_byte = line_to_offset(start_line)
end_byte   = line_to_offset(end_line)
print(f"Step 3: Splicing bytes {start_byte}–{end_byte}")

new_slides = open("new_slides.py", encoding="utf-8").read()
src = src[:start_byte] + new_slides + "\n\n" + src[end_byte:]

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4: Verify and write
# ─────────────────────────────────────────────────────────────────────────────
try:
    ast.parse(src)
    print("Step 4: Final syntax OK")
except SyntaxError as e:
    lines2 = src.split("\n")
    ctx = lines2[max(0,e.lineno-3):e.lineno+2] if e.lineno else []
    print(f"SYNTAX ERROR line {e.lineno}: {e.msg}")
    for i, l in enumerate(ctx, max(1, (e.lineno or 3)-2)):
        print(f"  {i}: {l}")
    sys.exit(1)

fns2 = [n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)]
required = ["_mk_bg","_draw_glow_card","_draw_header","_draw_caption_bar",
            "_generate_hook_frame","_generate_frame","_generate_context_frame",
            "_generate_trade_setup_slide","_generate_watchlist_slide",
            "_generate_cta_slide","post_market_update"]
all_ok = all(fn in fns2 for fn in required)
for fn in required:
    print(f"  {'OK' if fn in fns2 else 'MISSING'} {fn}")

if all_ok:
    with open("youtube_poster.py", "w", encoding="utf-8") as f:
        f.write(src)
    os.remove("new_slides.py")
    print("\nDONE — youtube_poster.py patched successfully")
else:
    print("\nNOT WRITTEN — missing functions above")
    sys.exit(1)
