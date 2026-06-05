def _generate_hook_frame(data, trigger):
    """Slide 1 — Hook. Gradient bg, glow cards, exact dashboard colors."""
    from PIL import Image, ImageDraw
    W, H = _VIDEO_W, _VIDEO_H
    PAD  = 52
    regime   = data.get("regime", "NEUTRAL")
    score    = data.get("regime_score", 50)
    rc       = _regime_color(regime)
    spy      = data.get("spy_pct", 0.0)
    qqq      = data.get("qqq_pct", 0.0)
    vix      = data.get("vix", 16.5)
    hot      = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    trig_lbl = {"premarket":"PRE-MARKET","midday":"MIDDAY","eod":"CLOSE","afterhours":"AFTER HOURS"}.get(trigger,"LIVE")
    img = _mk_bg()
    d   = ImageDraw.Draw(img)
    fnt_nano = _load_font(28)
    fnt_xs   = _load_font(38)
    fnt_sm   = _load_font(50)
    fnt_md   = _load_font(64, bold=True)
    fnt_lg   = _load_font(84, bold=True)
    fnt_xl   = _load_font(110, bold=True)
    _draw_header(d, W, "MARKET GENIE", trig_lbl, _C["accent"], fnt_md, fnt_sm,
                 timestamp=data.get("timestamp", ""))
    y = 172
    if hot and abs(hot[0].get("pct", 0)) >= 5:
        _draw_breaking_badge(d, W, hot[0]["symbol"], hot[0]["pct"], fnt_sm)
        y = 235
    _draw_glow_card(d, PAD, y, W - PAD, y + 200, radius=16, accent=rc)
    regime_label = {"BULLISH": "BULLS IN CONTROL", "BEARISH": "BEARS IN CONTROL", "NEUTRAL": "CHOPPY TAPE"}.get(regime, regime)
    regime_emoji = {"BULLISH": "\U0001f7e2", "BEARISH": "\U0001f534", "NEUTRAL": "\U0001f7e1"}.get(regime, "")
    d.text((W // 2, y + 76),  f"{regime_emoji} {regime_label}", font=fnt_xl, fill=rc, anchor="mm")
    d.text((W // 2, y + 152), f"A.I. Regime Score: {score} / 100", font=fnt_sm, fill=_C["subtext"], anchor="mm")
    y += 220
    col_w = (W - PAD * 2 - 20) // 3
    for i, (lbl, val, vc) in enumerate([
        ("SPY", f"{spy:+.2f}%", _pct_color(spy)),
        ("QQQ", f"{qqq:+.2f}%", _pct_color(qqq)),
        ("VIX", f"{vix:.1f}",   _C["red"] if vix > 20 else _C["subtext"]),
    ]):
        cx = PAD + i * (col_w + 10)
        _draw_glow_card(d, cx, y, cx + col_w, y + 110, radius=12, accent=vc, fill=_C["card"])
        d.text((cx + col_w//2, y + 33), lbl, font=fnt_xs, fill=_C["subtext"], anchor="mm")
        d.text((cx + col_w//2, y + 80), val, font=fnt_md, fill=vc,           anchor="mm")
    y += 130
    d.text((PAD, y + 10), "TODAY'S TOP MOVERS", font=fnt_xs, fill=_C["amber"])
    y += 55
    n_hot  = min(len(hot), 5)
    card_h = max(120, (H - y - 130) // max(n_hot, 1))
    for tk in hot[:n_hot]:
        tc   = _pct_color(tk["pct"])
        sign = "+" if tk["up"] else ""
        _draw_glow_card(d, 0, y, W, y + card_h - 4, radius=0, accent=tc, fill=_C["card"])
        d.rectangle([0, y, 7, y + card_h - 4], fill=tc)
        mid = y + (card_h - 4) // 2
        d.text((PAD + 12, mid - 20), tk["symbol"],             font=fnt_lg, fill=_C["text_hi"], anchor="lm")
        d.text((PAD + 12, mid + 30), f"${tk['price']:,.2f}",   font=fnt_xs, fill=_C["subtext"], anchor="lm")
        d.text((W - PAD,  mid),      f"{sign}{tk['pct']:.2f}%", font=fnt_lg, fill=tc,           anchor="rm")
        y += card_h
    regime_c = {"BULLISH": "\U0001f7e2", "BEARISH": "\U0001f534", "NEUTRAL": "\U0001f7e1"}.get(regime, "")
    _draw_caption_bar(d, W, H, f"{regime_c} {regime}  Score {score}/100  Follow for free A.I. signals", fnt_xs, fnt_nano)
    return img


def _generate_frame(data: dict, trigger: str):
    """Slide 3 — A.I. Signals. Dashboard cards with confidence bars and glow."""
    from PIL import Image, ImageDraw
    W, H = _VIDEO_W, _VIDEO_H
    PAD  = 52
    regime   = data.get("regime", "NEUTRAL")
    rc       = _regime_color(regime)
    signals  = sorted([s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 60],
                      key=lambda s: s.get("confidence", 0), reverse=True)
    hot      = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    vix_val  = data.get("vix", 16.5)
    nq_val   = data.get("nq_pct", 0.0)
    n_sigs   = len([s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 65])
    pnl_val  = data.get("pnl_today", 0.0)
    trig_lbl = {"premarket":"PRE-MARKET","midday":"MIDDAY","eod":"CLOSE","afterhours":"AFTER HOURS"}.get(trigger,"LIVE")
    img = _mk_bg()
    d   = ImageDraw.Draw(img)
    fnt_nano = _load_font(28)
    fnt_xs   = _load_font(38)
    fnt_sm   = _load_font(50)
    fnt_md   = _load_font(64, bold=True)
    fnt_lg   = _load_font(84, bold=True)
    _draw_header(d, W, "MARKET GENIE", trig_lbl, rc, fnt_md, fnt_sm,
                 timestamp=data.get("timestamp", ""))
    y = 172
    d.text((PAD, y), "A.I. SIGNALS", font=fnt_sm, fill=_C["amber"])
    d.text((W - PAD, y), f"{n_sigs} high-confidence", font=fnt_xs, fill=_C["subtext"], anchor="ra")
    y += 62
    n_show = min(len(signals), 4)
    card_h = max(180, min(290, (H - y - 380) // max(n_show, 1)))
    for sig in signals[:n_show]:
        bull  = "bull" in sig.get("direction", "bull").lower()
        sc    = _C["green"] if bull else _C["red"]
        label = "BULL" if bull else "BEAR"
        conf  = sig.get("confidence", 70)
        _draw_glow_card(d, 0, y, W, y + card_h - 4, radius=0, accent=sc, fill=_C["card"])
        d.rectangle([0, y, 8, y + card_h - 4], fill=sc)
        mid = y + (card_h - 4) // 2
        d.text((PAD + 14, mid - 24), sig["symbol"], font=fnt_lg, fill=_C["text_hi"], anchor="lm")
        try:  sx = int(d.textlength(sig["symbol"], font=fnt_lg)) + PAD + 26
        except: sx = PAD + 240
        _draw_badge(d, sx, mid - 34, label, sc, fnt_xs)
        d.text((W - PAD, mid - 26), f"{conf:.0f}%",  font=fnt_md, fill=sc,           anchor="ra")
        d.text((W - PAD, mid + 14), "confidence",    font=fnt_xs, fill=_C["subtext"], anchor="ra")
        _draw_conf_bar(d, PAD + 14, y + card_h - 36, W - PAD * 2 - 28, 16, conf, sc)
        y += card_h
    y += 12
    stat_rows = []
    if abs(pnl_val) >= 1:
        sign_p = "+" if pnl_val >= 0 else ""
        stat_rows.append(("TODAY'S P&L", f"{sign_p}${abs(pnl_val):,.0f}", _pct_color(pnl_val)))
    stat_rows.append(("ACTIVE SIGNALS", f"{n_sigs} setups", _C["amber"]))
    if abs(nq_val) >= 0.05:
        stat_rows.append(("NQ FUTURES", f"{nq_val:+.2f}%", _pct_color(nq_val)))
    vix_c = _C["red"] if vix_val > 22 else (_C["amber"] if vix_val > 18 else _C["green"])
    vix_note = "HIGH" if vix_val > 22 else "CALM"
    stat_rows.append(("VIX", f"{vix_val:.1f}  {vix_note}", vix_c))
    stat_rows.append(("SCANNER", "200+ stocks  real-time", _C["accent"]))
    row_h = 82
    for lbl, val, col in stat_rows[:4]:
        _draw_glow_card(d, 0, y, W, y + row_h, radius=0, accent=col, fill=_C["surface"])
        d.rectangle([0, y, 6, y + row_h], fill=col)
        d.text((PAD + 14, y + 22), lbl, font=fnt_xs, fill=_C["subtext"], anchor="lm")
        d.text((PAD + 14, y + 60), val, font=fnt_sm,  fill=col,           anchor="lm")
        y += row_h + 3
    top_sig_text = ""
    if signals:
        s0   = signals[0]
        bull = "bull" in s0.get("direction", "").lower()
        emoji = "\U0001f7e2" if bull else "\U0001f534"
        top_sig_text = f"{emoji} {s0['symbol']} {s0.get('confidence', 70):.0f}%  "
    _draw_caption_bar(d, W, H, f"{top_sig_text}Follow for free A.I. signals", fnt_xs, fnt_nano)
    return img


def _generate_cta_frame(data, trigger):
    """Legacy stub — 6-slide path uses _generate_cta_slide."""
    return _generate_frame(data, trigger)


def _generate_context_frame(data, trigger):
    """Slide 2 — Market Overview. Dashboard 2x2 grid + top signals."""
    from PIL import Image, ImageDraw
    W, H = _VIDEO_W, _VIDEO_H
    PAD  = 52
    regime   = data.get("regime", "NEUTRAL")
    rc       = _regime_color(regime)
    score    = data.get("regime_score", 50)
    hot      = sorted(data.get("hot_tickers", []), key=lambda t: abs(t.get("pct", 0)), reverse=True)
    signals  = sorted([s for s in data.get("ai_signals", []) if s.get("confidence", 0) > 60],
                      key=lambda s: s.get("confidence", 0), reverse=True)
    vix      = data.get("vix", 16.5)
    spy      = data.get("spy_pct", 0.0)
    qqq      = data.get("qqq_pct", 0.0)
    nq       = data.get("nq_pct", 0.0)
    trig_lbl = {"premarket":"PRE-MARKET","midday":"MIDDAY","eod":"CLOSE","afterhours":"AFTER HOURS"}.get(trigger,"LIVE")
    img = _mk_bg()
    d   = ImageDraw.Draw(img)
    fnt_nano = _load_font(28)
    fnt_xs   = _load_font(38)
    fnt_sm   = _load_font(50)
    fnt_md   = _load_font(64, bold=True)
    fnt_lg   = _load_font(84, bold=True)
    fnt_xl   = _load_font(110, bold=True)
    _draw_header(d, W, "MARKET GENIE", trig_lbl, _C["accent"], fnt_md, fnt_sm,
                 timestamp=data.get("timestamp", ""))
    y = 172
    # Regime banner
    _draw_glow_card(d, PAD, y, W - PAD, y + 120, radius=14, accent=rc)
    regime_emoji = {"BULLISH": "\U0001f7e2", "BEARISH": "\U0001f534", "NEUTRAL": "\U0001f7e1"}.get(regime, "")
    d.text((W // 2, y + 60), f"{regime_emoji} {regime}  {score}/100", font=fnt_xl, fill=rc, anchor="mm")
    y += 140
    # 2x2 index grid
    nq_lbl = "NQ FUT"
    nq_val_str = f"{nq:+.2f}%" if abs(nq) >= 0.05 else "flat"
    nq_col = _pct_color(nq) if abs(nq) >= 0.05 else _C["subtext"]
    pairs = [
        ("SPY",    f"{spy:+.2f}%", _pct_color(spy)),
        ("QQQ",    f"{qqq:+.2f}%", _pct_color(qqq)),
        (nq_lbl,   nq_val_str,     nq_col),
        ("VIX",    f"{vix:.1f}",   _C["red"] if vix > 20 else _C["green"]),
    ]
    cell_w = (W - PAD * 2 - 12) // 2
    cell_h = 140
    for i, (lbl, val, vc) in enumerate(pairs):
        cx = PAD + (i % 2) * (cell_w + 12)
        cy = y + (i // 2) * (cell_h + 10)
        _draw_glow_card(d, cx, cy, cx + cell_w, cy + cell_h, radius=14, accent=vc, fill=_C["card"])
        d.text((cx + cell_w//2, cy + 38),  lbl, font=fnt_xs, fill=_C["subtext"], anchor="mm")
        d.text((cx + cell_w//2, cy + 100), val, font=fnt_lg, fill=vc,            anchor="mm")
    y += 2 * (cell_h + 10) + 16
    # Top signals
    d.text((PAD, y), "TOP A.I. SIGNALS", font=fnt_sm, fill=_C["amber"])
    y += 58
    sig_card_h = max(130, (H - y - 120) // max(min(len(signals), 3), 1))
    for sig in signals[:3]:
        bull  = "bull" in sig.get("direction", "bull").lower()
        sc    = _C["green"] if bull else _C["red"]
        label = "BULL" if bull else "BEAR"
        conf  = sig.get("confidence", 70)
        pd    = next((t for t in hot if t["symbol"] == sig["symbol"]), None)
        price = pd["price"] if pd else 0
        pct   = pd["pct"]   if pd else 0
        _draw_glow_card(d, 0, y, W, y + sig_card_h - 4, radius=0, accent=sc, fill=_C["card"])
        d.rectangle([0, y, 7, y + sig_card_h - 4], fill=sc)
        mid = y + (sig_card_h - 4) // 2
        d.text((PAD + 14, mid - 20), sig["symbol"], font=fnt_lg, fill=_C["text_hi"], anchor="lm")
        _draw_badge(d, PAD + 14, mid + 16, label, sc, fnt_xs)
        d.text((W - PAD, mid - 20), f"{conf:.0f}%", font=fnt_md, fill=sc,           anchor="ra")
        if price > 0:
            sign_p = "+" if pct >= 0 else ""
            d.text((W - PAD, mid + 18), f"${price:,.2f}  {sign_p}{pct:.2f}%", font=fnt_xs, fill=_C["subtext"], anchor="ra")
        y += sig_card_h
    regime_c = {"BULLISH": "\U0001f7e2", "BEARISH": "\U0001f534", "NEUTRAL": "\U0001f7e1"}.get(regime, "")
    _draw_caption_bar(d, W, H, f"{regime_c} {regime} {score}/100  Drop green or red below", fnt_xs, fnt_nano)
    return img

