"""Static results page: `python -m prelude site` -> docs/index.html (GitHub Pages, source main /docs).

Reads ONLY the committed public results (results/onset_backtest.public.json) and embeds the five aggregates the
page shows. It does not embed the full file: its per-pair records carry onset days, and the page must not show
exact dates. One self-contained file: inline CSS/JS/SVG, a CSP that forbids every external request, no API calls,
no keys. The chart is rendered to inline SVG here, so it reads the same with JavaScript off (age-matched view,
plus a static table of both controls); JS only animates between the two controls.

build() refuses to write the page when:
  - a number on the page differs from the README results table (string-for-string),
  - the page contains a date or an http(s) URL outside the allow-listed links,
  - anything looks like a wallet address.
The export then runs the repo leak scanner over docs/ (the pre-commit hook scans the staged file again).
"""
import html
import json
import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(BASE, "results", "onset_backtest.public.json")
README = os.path.join(BASE, "README.md")
OUT = os.path.join(BASE, "docs", "index.html")
REPO = "https://github.com/mgwiazd1/prelude"
LINKS = {"repo": REPO, "readme": REPO + "#readme", "demo": REPO + "#quick-start"}

HEADLINE = "We thought our wallets got in early. The control caught that it was an artifact."


def fmt_p(p):
    """README convention: 2 decimals at p >= 0.01, else 3; ', not significant' at p >= 0.05."""
    s = f"{p:.2f}" if p >= 0.01 else f"{p:.3f}"
    return s + (", not significant" if p >= 0.05 else "")


def fmt_lift(x):
    return f"{x:.1f}×"


def load():
    bt = json.load(open(RESULTS))
    n = bt["stats"]["n_pairs"]
    assert bt["sensitivity_age_matched_posthoc"]["n_pairs"] == n
    views = {}
    for key, src, label, note in (
            ("age", bt["sensitivity_age_matched_posthoc"]["hits"], "Age-matched", "added after seeing results"),
            ("mcap", bt["stats"]["hits"], "Market-cap matched", "pre-registered")):
        views[key] = {"label": label, "note": note, "spike": src["spike_hits"], "null": src["null_hits"],
                      "lift": fmt_lift(src["lift"]), "p": fmt_p(src["p_fisher"]), "n": n}
    conc = bt["concentration_leave_out_top2_posthoc"]["age_matched"]["hits"]
    extra = {"conc": f"{fmt_lift(conc['lift'])} ({conc['spike_hits']}/{n} vs {conc['null_hits']}/{n})",
             "lead": f"{bt['stats']['median_lead_hours']:.0f}h"}
    return views, extra


def readme_rows():
    """The README results table, parsed: {'age'|'mcap': (spike, null, lift, p)} as README strings."""
    rows = {}
    for line in open(README, encoding="utf-8"):
        cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[1].count("/") == 1:
            key = "age" if cells[0].lower().startswith("age-matched") else "mcap" if cells[0].lower().startswith("market-cap") else None
            if key:
                rows[key] = tuple(cells[1:])
    return rows


def check_against_readme(views):
    rows = readme_rows()
    for key, v in views.items():
        mine = (f"{v['spike']}/{v['n']}", f"{v['null']}/{v['n']}", v["lift"], v["p"])
        if rows.get(key) != mine:
            raise SystemExit(f"site: {key} numbers {mine} != README {rows.get(key)} — refusing to write")


# ---------------------------------------------------------------- chart (inline SVG, rendered here)
W, H, PAD_L, PAD_B, TOP = 430, 250, 34, 36, 30
PLOT_H = H - PAD_B - TOP


def _y(v, n):
    return TOP + PLOT_H * (1 - v / n)


def svg(view, other, n):
    """Grouped bars: spike vs null hits (of n pairs) for one control; ghost tick = the other control's null."""
    ymax = n // 2 if max(view["spike"], view["null"], other["null"]) <= n // 2 else n   # 28 of 56: bars stay legible
    bw, gap = 70, 60
    x_spike, x_null = PAD_L + 55, PAD_L + 55 + bw + gap
    def bar(x, v, cls, ident):
        y = _y(v, ymax)
        return (f'<rect id="{ident}" class="{cls}" x="{x}" y="{y:.1f}" width="{bw}" height="{TOP + PLOT_H - y:.1f}" rx="3"/>'
                f'<text id="{ident}-v" class="val" x="{x + bw / 2}" y="{y - 6:.1f}" text-anchor="middle">{v}/{n}</text>')
    ticks = "".join(f'<line class="grid" x1="{PAD_L}" x2="{W - 8}" y1="{_y(t, ymax):.1f}" y2="{_y(t, ymax):.1f}"/>'
                    f'<text class="axis" x="{PAD_L - 6}" y="{_y(t, ymax) + 4:.1f}" text-anchor="end">{t}</text>'
                    for t in range(0, ymax + 1, 7))
    gy = _y(other["null"], ymax)
    ghost = (f'<line id="ghost" class="ghost" x1="{x_null - 8}" x2="{x_null + bw + 8}" y1="{gy:.1f}" y2="{gy:.1f}"/>'
             f'<text id="ghost-t" class="ghost-t" x="{x_null + bw + 10}" y="{gy + 4:.1f}">{html.escape(other["label"].split()[0].lower())} null</text>')
    return (f'<svg id="chart" viewBox="0 0 {W} {H}" role="img" aria-labelledby="chart-title chart-desc" data-ymax="{ymax}">'
            f'<title id="chart-title">Spike vs null hits, {html.escape(view["label"].lower())} control</title>'
            f'<desc id="chart-desc">{view["spike"]} of {n} spike tokens had a roster hit, versus {view["null"]} of {n} '
            f'{html.escape(view["label"].lower())} null tokens. Lift {view["lift"]}, p = {view["p"]}.</desc>'
            f'{ticks}{bar(x_spike, view["spike"], "spike", "b-spike")}{bar(x_null, view["null"], "null", "b-null")}{ghost}'
            f'<text class="cat" x="{x_spike + bw / 2}" y="{H - 12}" text-anchor="middle">spike tokens</text>'
            f'<text class="cat" x="{x_null + bw / 2}" y="{H - 12}" text-anchor="middle">null tokens</text>'
            f'<text class="axis" x="{PAD_L}" y="{TOP - 14}" text-anchor="start">hits (of {n} pairs)</text></svg>')


CSS = """
:root{color-scheme:dark;--bg:#0e1116;--panel:#161b22;--ink:#e6edf3;--mute:#9da7b3;--line:#2d333b;--spike:#e3b341;--null:#6e7681;--accent:#58a6ff}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:720px;margin:0 auto;padding:28px 16px 48px}
h1{font-size:clamp(1.45rem,4.6vw,2.1rem);line-height:1.2;margin:0 0 22px;letter-spacing:-.01em}
h2{font-size:1.05rem;margin:30px 0 8px;color:var(--mute);font-weight:600;text-transform:uppercase;letter-spacing:.06em}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
.toggle{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.toggle button{flex:1 1 220px;background:transparent;color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:10px 12px;font:inherit;font-size:.92rem;cursor:pointer;text-align:left}
.toggle button[aria-pressed=true]{border-color:var(--accent);background:#1f2a37}
.toggle small{display:block;color:var(--mute);font-size:.8rem}
svg{width:100%;height:auto;display:block}
.spike{fill:var(--spike)}.null{fill:var(--null)}
rect{transition:y .7s cubic-bezier(.2,.8,.2,1),height .7s cubic-bezier(.2,.8,.2,1)}
text{fill:var(--ink);font-size:13px}.val{font-weight:700;font-size:15px;transition:y .7s cubic-bezier(.2,.8,.2,1)}
.axis{fill:var(--mute);font-size:11px}.cat{fill:var(--mute);font-size:13px}.grid{stroke:var(--line);stroke-width:1}
.ghost{stroke:var(--mute);stroke-width:2;stroke-dasharray:4 3;transition:y1 .7s,y2 .7s}.ghost-t{fill:var(--mute);font-size:11px}
.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:12px}
.stat{border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-width:0}.stat b{display:block;font-size:clamp(.95rem,4.2vw,1.35rem);line-height:1.25;overflow-wrap:normal}.stat i{display:block;font-style:normal;font-weight:600;font-size:.85rem;color:var(--mute)}.stat span{color:var(--mute);font-size:.8rem}
table{width:100%;border-collapse:collapse;font-size:.9rem;margin-top:12px}td,th{border-bottom:1px solid var(--line);padding:6px 4px;text-align:left}th{color:var(--mute);font-weight:600}
ol{padding-left:1.2em;margin:0}li{margin:4px 0}code{background:#1f242c;border-radius:4px;padding:1px 5px;font-size:.88em}
a{color:var(--accent)}.links a{margin-right:18px;display:inline-block;margin-bottom:6px}.mute{color:var(--mute);font-size:.88rem}
.js-only{display:none}.js .js-only{display:flex}.js .no-js{display:none}
@media (prefers-reduced-motion:reduce){rect,.val,.ghost{transition:none}}
"""

JS = """
(function(){
  document.documentElement.className='js';
  var V=JSON.parse(document.getElementById('site-data').textContent), svg=document.getElementById('chart');
  var ymax=+svg.getAttribute('data-ymax'), TOP=%(top)d, PH=%(ph)d;
  function y(v){return TOP+PH*(1-v/ymax);}
  function setBar(id,v,n){var r=document.getElementById(id),t=document.getElementById(id+'-v'),yy=y(v);
    r.setAttribute('y',yy.toFixed(1));r.setAttribute('height',(TOP+PH-yy).toFixed(1));t.setAttribute('y',(yy-6).toFixed(1));t.textContent=v+'/'+n;}
  function show(k){var v=V[k],o=V[k==='age'?'mcap':'age'];
    setBar('b-spike',v.spike,v.n);setBar('b-null',v['null'],v.n);
    var g=document.getElementById('ghost'),gt=document.getElementById('ghost-t'),gy=y(o['null']);
    g.setAttribute('y1',gy.toFixed(1));g.setAttribute('y2',gy.toFixed(1));gt.setAttribute('y',(gy+4).toFixed(1));gt.textContent=o.label.split(' ')[0].toLowerCase()+' null';
    document.getElementById('s-lift').textContent=v.lift;var pp=v.p.split(', '),pb=document.getElementById('s-p');pb.firstChild.nodeValue=pp[0];document.getElementById('s-pq').textContent=pp.length>1?', '+pp.slice(1).join(', '):'';document.getElementById('s-n').textContent=v.n+' pairs';
    document.getElementById('s-ctl').textContent=v.label+' ('+v.note+')';
    document.getElementById('chart-desc').textContent=v.spike+' of '+v.n+' spike tokens had a roster hit, versus '+v['null']+' of '+v.n+' '+v.label.toLowerCase()+' null tokens. Lift '+v.lift+', p = '+v.p+'.';
    var bs=document.querySelectorAll('.toggle button');for(var i=0;i<bs.length;i++)bs[i].setAttribute('aria-pressed',bs[i].getAttribute('data-k')===k?'true':'false');}
  var bs=document.querySelectorAll('.toggle button');for(var i=0;i<bs.length;i++)bs[i].addEventListener('click',function(){show(this.getAttribute('data-k'));});
})();
""" % {"top": TOP, "ph": PLOT_H}


def page(views, extra):
    age, mcap = views["age"], views["mcap"]
    n = age["n"]
    data = json.dumps({k: {kk: v[kk] for kk in ("label", "note", "spike", "null", "lift", "p", "n")} for k, v in views.items()},
                      ensure_ascii=False).replace("</", "<\\/")
    def btn(k, v):
        return (f'<button type="button" data-k="{k}" aria-pressed="{"true" if k == "age" else "false"}">'
                f'{html.escape(v["label"])}<small>{html.escape(v["note"])}</small></button>')
    table = "".join(f'<tr><td>{html.escape(v["label"])} <span class="mute">({html.escape(v["note"])})</span></td>'
                    f'<td>{v["spike"]}/{n}</td><td>{v["null"]}/{n}</td><td>{v["lift"]}</td><td>{html.escape(v["p"])}</td></tr>'
                    for v in (age, mcap))
    para = (f"We tested whether a private roster of 45 Solana operator wallets bought tokens in the 7 days before "
            f"Nansen smart-money netflow onsets more often than matched quiet tokens on the same day: {n} pairs over "
            f"90 days. Against the pre-registered market-cap-matched null the roster looked early, "
            f"{mcap['spike']}/{n} vs {mcap['null']}/{n} ({mcap['lift']}). Matching on token age instead, a control we "
            f"added after seeing that result, the lift falls to {age['lift']} ({age['spike']}/{n} vs {age['null']}/{n}, "
            f"p = {age['p']}): the roster buys young tokens, and young tokens are where onsets happen. What is left is "
            f"concentrated in two wallets; without them the age-matched lift is {extra['conc']}. "
            f"This covers this cohort only and says nothing about smart money in general.")
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer">
<meta name="color-scheme" content="dark">
<title>Prelude: the control caught it</title>
<meta name="description" content="{html.escape(HEADLINE)} {age['lift']} vs {mcap['lift']}, n={n}.">
<style>{CSS}</style>
</head><body><main>
<h1>{html.escape(HEADLINE)}</h1>

<section class="panel" aria-label="Results">
  <div class="toggle js-only" role="group" aria-label="Null control">{btn("age", age)}{btn("mcap", mcap)}</div>
  {svg(age, mcap, n)}
  <div class="stats" aria-live="polite">
    <div class="stat"><b id="s-lift">{age['lift']}</b><span>lift</span></div>
    <div class="stat"><b id="s-p">{html.escape(age['p'].split(', ')[0])}<i id="s-pq">{html.escape((', ' + age['p'].split(', ', 1)[1]) if ', ' in age['p'] else '')}</i></b><span>p (Fisher)</span></div>
    <div class="stat"><b id="s-n">{n} pairs</b><span>n</span></div>
  </div>
  <p class="mute" style="margin:10px 0 0">Showing: <span id="s-ctl">{html.escape(age['label'])} ({html.escape(age['note'])})</span>.
  Dashed line: the other control's null hits.</p>
  <table class="no-js"><thead><tr><th>Null control</th><th>Spike hits</th><th>Null hits</th><th>Lift</th><th>p (Fisher)</th></tr></thead>
  <tbody>{table}</tbody></table>
</section>

<h2>What we tested</h2>
<p>{html.escape(para)}</p>

<h2>How it works</h2>
<ol>
<li><b>Onset:</b> the first day a token's Nansen smart-money netflow reaches $10k after a quiet week.</li>
<li><b>Hit:</b> a roster wallet's token amount rose in the 7 days before the onset, read from stored 90-day balance history.</li>
<li><b>Null:</b> a quiet token on the same day, matched on market cap (pre-registered) or token age (post hoc), same window and hit rule.</li>
<li><b>Lift</b> = spike hit rate ÷ null hit rate; p from Fisher's exact test. Median lead {extra['lead']}, set by the 7-day window.</li>
</ol>
<p class="mute">Nansen endpoints used: <code>token-screener/historical</code> (<code>trader_type=sm</code>, the onset clock),
<code>profiler/address/historical-balances</code> (entry history), <code>profiler/address/current-balance</code> (live snapshots).</p>

<h2>Links</h2>
<p class="links"><a href="{LINKS['repo']}">Repository</a><a href="{LINKS['readme']}">README</a><a href="{LINKS['demo']}">Run <code>make demo</code></a></p>
<p class="mute">Wallet identities are not published. Built for the Nansen Meridian Buildathon.</p>
</main>
<script type="application/json" id="site-data">{data}</script>
<script>{JS}</script>
</body></html>
"""


DATE = re.compile(r"\b(?:19|20)\d\d-\d\d-\d\d\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}\b", re.I)
ADDR = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b|\b0x[0-9a-fA-F]{40}\b")


def self_check(text):
    problems = []
    if DATE.search(text):
        problems.append(f"date on page: {DATE.search(text).group(0)!r}")
    urls = set(re.findall(r"https?://[^\s\"'<>)]+", text))
    if urls - set(LINKS.values()):
        problems.append(f"unexpected URL(s): {sorted(urls - set(LINKS.values()))}")
    if re.search(r"<(?:img|link|iframe|object|embed)[^>]+(?:src|href)=\"(?!data:)", text, re.I) or re.search(r"<script[^>]+src=", text, re.I):
        problems.append("external resource reference")
    if ADDR.search(text):
        problems.append("address-shaped string on page")
    return problems


def build(write=True):
    views, extra = load()
    check_against_readme(views)
    text = page(views, extra)
    problems = self_check(text)
    if problems:
        raise SystemExit("site: refusing to write docs/index.html: " + "; ".join(problems))
    if write:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as f:
            f.write(text)
    return text
