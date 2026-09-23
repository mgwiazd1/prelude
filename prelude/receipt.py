"""Receipt card — deterministic PIL PNG, no network (spec §F shareable hook).

Stat form is LIFT-first (v1.5): 'wallet 0x… — front-ran in 7/50 spike windows
vs 1/50 control (lift), median lead 14h; entered $TOKEN at T−14h (tx-pinned)'
"""
import os
import textwrap

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
BG = (12, 14, 20)
FG = (235, 238, 245)
ACCENT = (122, 205, 139)
DIM = (150, 156, 168)
WARN = (240, 190, 90)
# the 2026-09-23 backtest did not support "wallets move first" as a habit;
# the product measures lead claims against matched nulls
TAGLINE = "lead claims, tested against matched nulls"


def _font(size, bold=False):
    try:
        return ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()


def lift_stat(spike_hits, spike_windows, control_hits, control_windows):
    return f"{spike_hits}/{spike_windows} spike windows vs {control_hits}/{control_windows} control"


def render(wallet, token, lift_text, median_lead_h, instance_lead_h, out_path,
           iqr_h=None):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 8], fill=ACCENT)
    d.text((60, 48), "PRELUDE", font=_font(34, True), fill=ACCENT)
    d.text((60, 96), TAGLINE, font=_font(20), fill=DIM)
    d.text((60, 180), f"wallet {wallet[:8]}…{wallet[-4:]}", font=_font(28, True), fill=FG)
    iqr = f" (IQR {iqr_h[0]:.0f}–{iqr_h[1]:.0f}h)" if iqr_h else ""
    body = textwrap.fill(f"{token} — {lift_text} (lift); median lead {median_lead_h:.0f}h{iqr}; "
                         f"entered at T−{instance_lead_h:.0f}h (tx-pinned)", 52)
    d.multiline_text((60, 250), body, font=_font(26), fill=FG, spacing=10)
    # only caller is the fixture demo — say so on the image itself
    d.text((60, H - 70), "PRELUDE · receipt · SYNTHETIC demo data (fixtures)",
           font=_font(18), fill=WARN)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img.save(out_path)
    return out_path


def _pct(h, n):
    return f"{h}/{n}"


def render_confound(bt, out_path, repo_url=None):
    """X-post card: the age confound itself. bt = out/onset_backtest.json.
    Every number carries its n; the headline row is the age-matched control."""
    pre, age = bt["stats"], bt["sensitivity_age_matched_posthoc"]
    n = pre["n_pairs"]
    ph, ah = pre["hits"], age["hits"]
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 8], fill=ACCENT)
    d.text((60, 40), "PRELUDE", font=_font(30, True), fill=ACCENT)
    d.text((60, 80), TAGLINE, font=_font(20), fill=DIM)
    d.text((60, 140), f"{ph['lift']}\u00d7 \u2192 {ah['lift']}\u00d7",
           font=_font(96, True), fill=FG)
    d.text((60, 262), f"when you control for token age.  n={n}.",
           font=_font(34), fill=FG)
    y = 340
    d.text((60, y), f"age-matched null (control added after seeing results)",
           font=_font(21, True), fill=WARN)
    d.text((60, y + 30), f"  {_pct(ah['spike_hits'], n)} spike vs {_pct(ah['null_hits'], n)} null"
           f"  \u00b7  {ah['lift']}\u00d7  \u00b7  p={ah['p_fisher']:.2f} (not significant)",
           font=_font(21), fill=FG)
    d.text((60, y + 72), "pre-registered null (market-cap matched)",
           font=_font(21, True), fill=DIM)
    d.text((60, y + 102), f"  {_pct(ph['spike_hits'], n)} spike vs {_pct(ph['null_hits'], n)} null"
           f"  \u00b7  {ph['lift']}\u00d7  \u00b7  p={ph['p_fisher']:.3f}",
           font=_font(21), fill=DIM)
    d.text((60, H - 76), "hit = tracked wallet added in the 7d before a Nansen smart-money "
           "netflow onset \u00b7 Solana \u00b7 daily", font=_font(17), fill=DIM)
    d.text((60, H - 46), (repo_url + "  \u00b7  " if repo_url else "") +
           "MIT \u00b7 one-command run", font=_font(17), fill=DIM)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img.save(out_path)
    return out_path
