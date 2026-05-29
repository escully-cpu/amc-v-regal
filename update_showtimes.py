#!/usr/bin/env python3
"""Fetch a 7-day window of recliner showtimes at Emma's 4 NYC theaters
   (2 Regal + 2 AMC) via Fandango's internal API. Resolves real movie
   posters from Letterboxd (with on-disk cache) and renders a unified
   day-tab dashboard at index.html.

   Run:  ./run.sh   (or)   python3 update_showtimes.py
"""
import json, html, re, sys, time
from datetime import datetime, timedelta, date as date_cls
from pathlib import Path
from curl_cffi import requests

HERE = Path(__file__).resolve().parent
POSTER_CACHE = HERE / "posters_cache.json"

# (fandango_id, display_name, page_slug, chain, theater_url, trust_all)
#
# trust_all = True  → entire theater is recliner; show every showtime even if
#                     Fandango omits the amenity tag (e.g. Regal Union Square).
# trust_all = False → mixed theater; only show shows whose amenities mention
#                     "recliner" (e.g. AMC Empire 25 — only premium houses).
THEATERS = [
    ("AAPOS", "Regal Battery Park",  "regal-battery-park-aapos",
     "regal", "https://www.regmovies.com/theatres/regal-battery-park/1335", True),
    ("AAJNK", "Regal Union Square",  "regal-union-square-screenx-and-4dx-aajnk",
     "regal", "https://www.regmovies.com/theatres/regal-union-square/1320", True),
    ("AAQCR", "AMC 34th Street 14",  "amc-34th-street-14-aaqcr",
     "amc",   "https://www.amctheatres.com/movie-theatres/new-york-city/amc-34th-street-14", True),
    ("AABQF", "AMC Village 7",       "amc-village-7-aabqf",
     "amc",   "https://www.amctheatres.com/movie-theatres/new-york-city/amc-village-7", True),
]

DAYS_AHEAD = 7  # today + next 6 days
RECLINER_TOKENS = ("recliner", "reclining", "leather rocker")


# ---------- Fandango fetch ----------

def fetch_theater_date(sess, tid, slug, date_iso):
    """Return Fandango viewModel for theater + date (uses session cookies)."""
    r = sess.get(
        f"https://www.fandango.com/napi/theaterMovieShowtimes/{tid}",
        params={"startDate": date_iso, "isdesktop": "true"},
        headers={"Accept": "application/json",
                 "Referer": f"https://www.fandango.com/{slug}/theater-page"},
        timeout=25,
    )
    r.raise_for_status()
    return r.json().get("viewModel", {}) or {}


def is_recliner_group(ag):
    blob = " ".join(a.get("name", "") for a in ag.get("amenities", []))
    blob += " " + (ag.get("amenityString") or "")
    return any(tok in blob.lower() for tok in RECLINER_TOKENS)


def format_label(variant):
    return (variant.get("filmFormatHeader") or "").strip() or "Standard"


def normalize_movie_key(title):
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


# ---------- Letterboxd poster lookup ----------

def _slug(title):
    s = (title or "").lower()
    s = re.sub(r"&", " and ", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s

def upsize_letterboxd(url):
    """Replace the dimension segment in a film-poster URL to ask for 500x750."""
    # pattern: -<x>-<w>-<y>-<h>-crop
    return re.sub(r"-0-\d+-0-\d+-crop", "-0-500-0-750-crop", url)

def lookup_poster(sess, title, year_hint, cache):
    """Return a poster URL or None. Cached by title."""
    if title in cache:
        return cache[title]

    title_no_year = re.sub(r"\s*\(\d{4}\)\s*$", "", title)
    # Drop event/anniversary suffixes so re-releases find their base film page.
    base = re.split(
        r"\s+\d+(?:st|nd|rd|th)\s+anniversary\b|\s+(?:early access screening|imax preview)\b",
        title_no_year, maxsplit=1, flags=re.I,
    )[0]

    candidates = []
    for stem in (title_no_year, base) if base != title_no_year else (title_no_year,):
        slug = _slug(stem)
        if year_hint:
            candidates.append(f"{slug}-{year_hint}")
        candidates.append(slug)
    # de-dupe, preserve order
    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    for slug in candidates:
        url = f"https://letterboxd.com/film/{slug}/"
        try:
            r = sess.get(url, timeout=15)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        # Prefer the dedicated film-poster URL (clean poster aspect ratio).
        m = re.search(r'https://a\.ltrbxd\.com/resized/film-poster/[^\s"\'<>]+\.jpg', r.text)
        if m:
            cache[title] = upsize_letterboxd(m.group(0))
            return cache[title]
        # Fallback: og:image (often a non-poster crop, but better than nothing).
        m = re.search(r'<meta property="og:image" content="([^"]+)"', r.text)
        if m:
            cache[title] = m.group(1)
            return cache[title]

    cache[title] = None
    return None


# ---------- Collect ----------

def collect():
    sess = requests.Session(impersonate="chrome131")
    posters = json.loads(POSTER_CACHE.read_text()) if POSTER_CACHE.exists() else {}

    # Prime sessions for each theater (visit page once to set affinity cookies).
    for _tid, _name, slug, *_ in THEATERS:
        try:
            sess.get(f"https://www.fandango.com/{slug}/theater-page", timeout=20)
        except Exception as e:
            print(f"  warn: priming {slug}: {e}", file=sys.stderr)

    today = date_cls.today()
    dates = [(today + timedelta(days=i)).isoformat() for i in range(DAYS_AHEAD)]

    # movie_key -> {meta..., by_date: {date: {tid: [variants]}}}
    by_movie = {}

    for d in dates:
        for tid, name, slug, chain, theater_url, trust_all in THEATERS:
            print(f"[fetch] {d}  {name}", flush=True)
            try:
                vm = fetch_theater_date(sess, tid, slug, d)
            except Exception as e:
                print(f"  ERROR: {e}", file=sys.stderr)
                continue
            api_date = vm.get("date")
            if api_date and api_date != d:
                # Fandango ignored the requested date (e.g. theater closed → echoed today).
                # Skip to avoid duplicating today's shows under future days.
                if d != today.isoformat():
                    continue
            movies = vm.get("movies", []) or []
            print(f"  → {len(movies)} movies")

            for m in movies:
                kept_variants = []
                for v in m.get("variants", []) or []:
                    for ag in v.get("amenityGroups", []) or []:
                        if not trust_all and not is_recliner_group(ag):
                            continue
                        shows = [s for s in (ag.get("showtimes") or [])
                                 if s.get("type") != "pastshowtime"]
                        if not shows:
                            continue
                        kept_variants.append({
                            "format": format_label(v),
                            "amenities": [a.get("name") for a in ag.get("amenities", []) or []],
                            "shows": [{
                                "time":          s.get("date"),
                                "screen_reader": s.get("screenReaderTime"),
                                "ticketing_date": s.get("ticketingDate"),
                                "url":           s.get("ticketingJumpPageURL"),
                                "id":            s.get("id"),
                            } for s in shows],
                        })
                if not kept_variants:
                    continue

                key = normalize_movie_key(m.get("title"))
                if key not in by_movie:
                    by_movie[key] = {
                        "key":      key,
                        "title":    m.get("title"),
                        "rating":   m.get("rating") or "",
                        "runtime":  m.get("runtime") or 0,
                        "genres":   m.get("genres") or [],
                        "mop_uri":  m.get("mopURI") or "",
                        "poster":   None,        # filled in after collection
                        "by_date":  {},          # date -> tid -> variants
                    }
                by_movie[key]["by_date"].setdefault(d, {}).setdefault(tid, []).extend(kept_variants)

    # Resolve real posters via Letterboxd (cached).
    print(f"\n[posters] resolving for {len(by_movie)} movies…")
    pre_cached = sum(1 for m in by_movie.values() if m["title"] in posters)
    print(f"  {pre_cached} already cached, fetching {len(by_movie) - pre_cached} new")

    for m in by_movie.values():
        year_match = re.search(r"\((\d{4})\)", m["title"] or "")
        year = year_match.group(1) if year_match else None
        try:
            m["poster"] = lookup_poster(sess, m["title"], year, posters)
        except Exception as e:
            print(f"  poster lookup error {m['title']}: {e}", file=sys.stderr)
            m["poster"] = None
        if m["title"] not in posters or posters.get(m["title"]) != m["poster"]:
            # tiny throttle on fresh lookups
            time.sleep(0.2)

    POSTER_CACHE.write_text(json.dumps(posters, indent=2, sort_keys=True))

    return {
        "fetched_at": datetime.now().isoformat(timespec="minutes"),
        "today":      today.isoformat(),
        "dates":      dates,
        "theaters":   [{"id": t[0], "name": t[1], "chain": t[3], "url": t[4]}
                       for t in THEATERS],
        "movies":     list(by_movie.values()),
    }


# ---------- HTML rendering ----------

PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NYC Movies — Recliner Showtimes</title>
<style>
:root {{
  --bg: #0e0f13;
  --panel: #1a1c22;
  --panel-2: #23262f;
  --line: #2a2d36;
  --text: #ecedf1;
  --muted: #8b8f99;
  --accent: #f5c518;
  --regal: #c33;
  --amc:   #d92027;
  --chip:  #2c3140;
  --chip-on: #f5c518;
  --chip-on-text: #0e0f13;
}}
* {{ box-sizing: border-box; }}
html, body {{ background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", system-ui, sans-serif;
  margin: 0; padding: 0; }}
header {{ padding: 18px 28px 12px; border-bottom: 1px solid var(--line);
  position: sticky; top: 0; background: rgba(14,15,19,.93); backdrop-filter: blur(12px); z-index: 10; }}
header h1 {{ margin: 0; font-size: 22px; letter-spacing: -.01em; }}
header .sub {{ margin-top: 4px; color: var(--muted); font-size: 13px; }}
.daybar {{ display: flex; gap: 6px; margin-top: 12px; overflow-x: auto;
  padding-bottom: 4px; scrollbar-width: thin; }}
.day {{ flex: 0 0 auto; background: var(--chip); border: 1px solid var(--line);
  border-radius: 10px; padding: 8px 14px; cursor: pointer; text-align: center;
  font-size: 12px; line-height: 1.25; user-select: none;
  transition: background .12s, color .12s, border-color .12s; min-width: 76px; }}
.day .dow {{ font-weight: 600; letter-spacing: .04em; text-transform: uppercase; }}
.day .dom {{ font-size: 18px; color: var(--text); margin-top: 2px; }}
.day:hover {{ border-color: #444; }}
.day.on {{ background: var(--accent); color: var(--chip-on-text); border-color: var(--accent); }}
.day.on .dom {{ color: var(--chip-on-text); }}
.day.today::after {{ content: "•"; display: block; color: var(--accent);
  margin-top: -2px; font-size: 16px; line-height: .5; }}
.day.on.today::after {{ color: var(--chip-on-text); }}
header .controls {{ margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
.chip {{ background: var(--chip); color: var(--text); border: 1px solid var(--line);
  padding: 6px 12px; border-radius: 999px; font-size: 13px; cursor: pointer; user-select: none;
  transition: background .12s, color .12s; }}
.chip:hover {{ border-color: #444; }}
.chip.on {{ background: var(--chip-on); color: var(--chip-on-text); border-color: var(--chip-on); font-weight: 600; }}
.chip.theater.on[data-chain="regal"] {{ background: var(--regal); color: #fff; border-color: var(--regal); }}
.chip.theater.on[data-chain="amc"]   {{ background: var(--amc);   color: #fff; border-color: var(--amc); }}
#search {{ background: var(--chip); border: 1px solid var(--line); color: var(--text);
  padding: 7px 12px; border-radius: 999px; font-size: 13px; min-width: 220px; outline: none; }}
#search:focus {{ border-color: var(--accent); }}
main {{ padding: 18px 28px 64px; max-width: 1500px; margin: 0 auto; }}
.movie {{ background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
  display: grid; grid-template-columns: 130px 1fr; gap: 16px; padding: 16px;
  margin-bottom: 14px; position: relative;
  transition: opacity .25s ease, transform .25s ease; }}
.movie.fading {{ opacity: 0; transform: scale(.97); }}
.movie.hidden-marked {{ opacity: .5; }}
.movie.hidden-marked .poster {{ filter: grayscale(1) brightness(.7); }}
.hide-btn {{ position: absolute; top: 10px; right: 10px; background: rgba(20,22,28,.6);
  border: 1px solid var(--line); color: var(--muted); width: 30px; height: 30px;
  border-radius: 50%; cursor: pointer; display: flex; align-items: center;
  justify-content: center; padding: 0; transition: color .12s, background .12s,
  border-color .12s, transform .12s; backdrop-filter: blur(6px); }}
.hide-btn:hover {{ color: var(--accent); border-color: var(--accent);
  background: rgba(20,22,28,.85); transform: scale(1.08); }}
.hide-btn svg {{ width: 16px; height: 16px; display: block; }}
.hide-btn .eye-open  {{ display: block; }}
.hide-btn .eye-closed {{ display: none; }}
.movie.hidden-marked .hide-btn .eye-open  {{ display: none; }}
.movie.hidden-marked .hide-btn .eye-closed {{ display: block; }}
.movie.hidden-marked .hide-btn {{ color: var(--accent); border-color: var(--accent); }}
.movie .poster {{ width: 130px; aspect-ratio: 2/3; background: #000; border-radius: 8px;
  overflow: hidden; }}
.movie .poster img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
.movie h2 {{ margin: 0 0 4px; font-size: 18px; }}
.movie h2 a {{ color: inherit; text-decoration: none; }}
.movie h2 a:hover {{ color: var(--accent); }}
.movie .meta {{ color: var(--muted); font-size: 12.5px; margin-bottom: 10px; }}
.movie .meta .rating {{ display: inline-block; padding: 1px 6px; border: 1px solid var(--line);
  border-radius: 4px; margin-right: 8px; color: var(--text); font-weight: 600; }}
.lanes {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
.lane {{ background: var(--panel-2); border: 1px solid var(--line); border-radius: 10px;
  padding: 10px 12px; }}
.lane.regal {{ border-left: 3px solid var(--regal); }}
.lane.amc   {{ border-left: 3px solid var(--amc); }}
.lane h3 {{ margin: 0 0 8px; font-size: 13px; font-weight: 600; color: var(--text);
  display: flex; align-items: center; gap: 8px; }}
.lane h3 .chain-pill {{ font-size: 10px; letter-spacing: .04em; text-transform: uppercase;
  padding: 2px 6px; border-radius: 4px; font-weight: 700; }}
.lane.regal h3 .chain-pill {{ background: var(--regal); color: #fff; }}
.lane.amc   h3 .chain-pill {{ background: var(--amc);   color: #fff; }}
.lane.empty {{ color: var(--muted); font-size: 12.5px; padding: 16px 12px; text-align: center;
  background: transparent; border-style: dashed; }}
.variant {{ margin-bottom: 8px; }}
.variant:last-child {{ margin-bottom: 0; }}
.variant .fmt {{ font-size: 11.5px; color: var(--muted); margin-bottom: 4px;
  text-transform: uppercase; letter-spacing: .04em; }}
.times {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.times a {{ display: inline-block; background: #2f333f; color: var(--text);
  text-decoration: none; padding: 5px 10px; border-radius: 6px; font-size: 13px;
  font-variant-numeric: tabular-nums; border: 1px solid transparent; }}
.times a:hover {{ background: var(--accent); color: var(--chip-on-text); border-color: var(--accent); }}
.empty-state {{ color: var(--muted); text-align: center; padding: 60px 20px; }}
@media (max-width: 900px) {{
  .movie {{ grid-template-columns: 96px 1fr; }}
  .movie .poster {{ width: 96px; }}
  .lanes {{ grid-template-columns: 1fr; }}
  main {{ padding: 14px; }}
  header {{ padding: 14px 16px 10px; }}
}}
</style>
</head>
<body>
<header>
  <h1>🎬 NYC Movies — Recliner Showtimes</h1>
  <div class="sub">Last refreshed {fetched_pretty}
    · <span id="visible-count">0</span> movies on selected day · click any time to book on Fandango</div>
  <div class="daybar">
    {day_tabs}
  </div>
  <div class="controls">
    {theater_chips}
    <span style="width:1px;height:22px;background:var(--line);margin:0 4px;"></span>
    {format_chips}
    <span style="flex:1;"></span>
    <span class="chip" id="show-hidden-chip" title="Toggle visibility of movies you've hidden">
      👁 Hidden <span id="hidden-count">0</span>
    </span>
    <input id="search" type="search" placeholder="Search movies…">
  </div>
</header>
<main id="movie-list">
{movie_cards}
</main>
<script>
const HIDDEN_KEY = "nycMovies.hidden";
const loadHidden = () => {{
  try {{ return new Set(JSON.parse(localStorage.getItem(HIDDEN_KEY) || "[]")); }}
  catch (e) {{ return new Set(); }}
}};
const saveHidden = (set) => localStorage.setItem(HIDDEN_KEY, JSON.stringify([...set]));

const state = {{
  date: "{initial_date}",
  theaters: new Set({theater_ids_json}),
  formats: new Set(),
  q: "",
  hidden: loadHidden(),
  showHidden: false,
}};

function refreshHiddenChip() {{
  document.getElementById('hidden-count').textContent = state.hidden.size;
  const chip = document.getElementById('show-hidden-chip');
  chip.classList.toggle('on', state.showHidden);
  chip.style.display = state.hidden.size ? '' : 'none';
}}

function applyFilters() {{
  const q = state.q.trim().toLowerCase();
  let visible = 0;
  document.querySelectorAll('.movie').forEach(card => {{
    card.classList.remove('fading');
    const cardDate = card.dataset.date;
    const key = card.dataset.movieKey;
    const isHidden = state.hidden.has(key);
    card.classList.toggle('hidden-marked', isHidden);

    if (cardDate !== state.date) {{ card.style.display = 'none'; return; }}
    if (isHidden && !state.showHidden) {{ card.style.display = 'none'; return; }}

    const title = (card.dataset.title || '').toLowerCase();
    const cardTheaters = (card.dataset.theaters || '').split(',').filter(Boolean);
    const cardFormats  = (card.dataset.formats  || '').split('|').filter(Boolean);
    const matchQ = !q || title.includes(q);
    const matchT = cardTheaters.some(t => state.theaters.has(t));
    const matchF = state.formats.size === 0 ||
                   cardFormats.some(f => state.formats.has(f));
    const show = matchQ && matchT && matchF;
    card.style.display = show ? '' : 'none';
    if (show) visible++;

    card.querySelectorAll('.lane').forEach(lane => {{
      lane.style.display = state.theaters.has(lane.dataset.tid) ? '' : 'none';
    }});
  }});
  document.getElementById('visible-count').textContent = visible;
  refreshHiddenChip();
}}

document.addEventListener('click', (e) => {{
  const btn = e.target.closest('.hide-btn');
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  const key = btn.dataset.movieKey;
  const wasHidden = state.hidden.has(key);
  if (wasHidden) {{
    state.hidden.delete(key);
  }} else {{
    state.hidden.add(key);
  }}
  saveHidden(state.hidden);

  // briefly fade the current card so the toggle feels tactile
  const card = btn.closest('.movie');
  if (!wasHidden && !state.showHidden) {{
    card.classList.add('fading');
    setTimeout(applyFilters, 220);
  }} else {{
    applyFilters();
  }}
}});

document.getElementById('show-hidden-chip').addEventListener('click', () => {{
  state.showHidden = !state.showHidden;
  applyFilters();
}});

document.querySelectorAll('.day').forEach(d => {{
  d.addEventListener('click', () => {{
    state.date = d.dataset.date;
    document.querySelectorAll('.day').forEach(x => x.classList.remove('on'));
    d.classList.add('on');
    applyFilters();
    window.scrollTo({{top: 0, behavior: 'smooth'}});
  }});
}});

document.querySelectorAll('.chip[data-tid]').forEach(c => {{
  c.addEventListener('click', () => {{
    const tid = c.dataset.tid;
    if (state.theaters.has(tid)) state.theaters.delete(tid);
    else state.theaters.add(tid);
    c.classList.toggle('on');
    applyFilters();
  }});
}});

document.querySelectorAll('.chip[data-format]').forEach(c => {{
  c.addEventListener('click', () => {{
    const f = c.dataset.format;
    if (f === 'all') {{
      state.formats.clear();
      document.querySelectorAll('.chip[data-format]').forEach(x => x.classList.remove('on'));
      c.classList.add('on');
    }} else {{
      document.querySelector('.chip[data-format="all"]').classList.remove('on');
      if (state.formats.has(f)) {{ state.formats.delete(f); c.classList.remove('on'); }}
      else {{ state.formats.add(f); c.classList.add('on'); }}
      if (state.formats.size === 0)
        document.querySelector('.chip[data-format="all"]').classList.add('on');
    }}
    applyFilters();
  }});
}});

document.getElementById('search').addEventListener('input', e => {{
  state.q = e.target.value;
  applyFilters();
}});

applyFilters();
</script>
</body>
</html>"""


def _day_tabs(dates, today_iso):
    out = []
    for d in dates:
        dt = datetime.strptime(d, "%Y-%m-%d")
        dow = dt.strftime("%a").upper()
        dom = dt.strftime("%-d")
        cls = "day"
        if d == today_iso:
            cls += " today on"
            label = "Today"
        else:
            label = dow
        out.append(
            f'<div class="{cls}" data-date="{d}">'
            f'<div class="dow">{html.escape(label)}</div>'
            f'<div class="dom">{dom}</div></div>'
        )
    return "".join(out)


def render_card(m, d, theaters):
    poster_url = m.get("poster") or ""
    title = html.escape(m["title"] or "Untitled")
    rating = html.escape(m["rating"] or "")
    runtime = m["runtime"]
    runtime_str = f"{runtime//60}h {runtime%60}m" if runtime else ""
    genres = ", ".join(m["genres"][:3])
    movie_url = "https://www.fandango.com" + m["mop_uri"] if m["mop_uri"] else "#"

    day_data = m["by_date"].get(d, {})
    if not day_data:
        return None

    formats = set()
    theater_ids = []
    lanes_by_chain = {"regal": [], "amc": []}

    for t in theaters:
        tid = t["id"]
        chain = t["chain"]
        variants = day_data.get(tid, [])
        if variants:
            theater_ids.append(tid)
            vparts = []
            for v in variants:
                formats.add(v["format"])
                times_html = " ".join(
                    f'<a href="{html.escape(s["url"])}" target="_blank" rel="noopener" '
                    f'title="{html.escape(s.get("screen_reader") or "")}">'
                    f'{html.escape(s["time"] or "")}</a>'
                    for s in v["shows"]
                )
                fmt = html.escape(v["format"])
                vparts.append(
                    f'<div class="variant"><div class="fmt">{fmt}</div>'
                    f'<div class="times">{times_html}</div></div>'
                )
            lane = (
                f'<div class="lane {chain}" data-tid="{tid}">'
                f'<h3><span class="chain-pill">{chain.upper()}</span>'
                f'<a href="{html.escape(t["url"])}" target="_blank" rel="noopener" '
                f'style="color:inherit;text-decoration:none;">{html.escape(t["name"])}</a></h3>'
                f'{"".join(vparts)}</div>'
            )
        else:
            lane = (
                f'<div class="lane {chain} empty" data-tid="{tid}">'
                f'{html.escape(t["name"])} — no recliner times</div>'
            )
        lanes_by_chain[chain].append(lane)

    lanes_block = (
        '<div class="lanes">'
        f'<div>{"".join(lanes_by_chain["regal"])}</div>'
        f'<div>{"".join(lanes_by_chain["amc"])}</div>'
        '</div>'
    )

    meta_bits = []
    if rating: meta_bits.append(f'<span class="rating">{rating}</span>')
    if runtime_str: meta_bits.append(runtime_str)
    if genres: meta_bits.append(genres)
    meta_html = " · ".join(meta_bits)

    poster_img = (f'<img src="{html.escape(poster_url)}" alt="" loading="lazy">'
                  if poster_url else "")
    eye_btn = (
        '<button class="hide-btn" data-movie-key="' + html.escape(m["key"]) + '" '
        'aria-label="Hide this movie" title="Hide this movie (you\'ve seen it)">'
        '<svg class="eye-open" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>'
        '<circle cx="12" cy="12" r="3"/></svg>'
        '<svg class="eye-closed" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>'
        '<path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>'
        '<path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/>'
        '<line x1="1" y1="1" x2="23" y2="23"/></svg>'
        '</button>'
    )
    return (
        f'<article class="movie" data-date="{d}" '
        f'data-movie-key="{html.escape(m["key"])}" '
        f'data-title="{html.escape(m["title"] or "")}" '
        f'data-theaters="{",".join(theater_ids)}" '
        f'data-formats="{"|".join(sorted(formats))}">'
        f'{eye_btn}'
        f'<div class="poster">{poster_img}</div>'
        f'<div><h2><a href="{html.escape(movie_url)}" target="_blank" rel="noopener">{title}</a></h2>'
        f'<div class="meta">{meta_html}</div>'
        f'{lanes_block}</div></article>'
    )


def render_html(data):
    theaters = data["theaters"]
    theater_chips = "".join(
        f'<span class="chip theater on" data-tid="{t["id"]}" data-chain="{t["chain"]}">'
        f'{html.escape(t["name"])}</span>'
        for t in theaters
    )
    theater_ids = [t["id"] for t in theaters]

    all_formats = set()
    for m in data["movies"]:
        for day in m["by_date"].values():
            for variants in day.values():
                for v in variants:
                    all_formats.add(v["format"])
    format_chips = (
        '<span class="chip on" data-format="all" data-kind="format">All formats</span>'
        + "".join(
            f'<span class="chip" data-format="{html.escape(f)}" data-kind="format">{html.escape(f)}</span>'
            for f in sorted(all_formats)
        )
    )

    cards = []
    for d in data["dates"]:
        # sort each day by # of theaters showing it, then title
        movies_today = sorted(
            (m for m in data["movies"] if d in m["by_date"]),
            key=lambda m: (-len(m["by_date"][d]), m["title"] or "")
        )
        for m in movies_today:
            card = render_card(m, d, theaters)
            if card:
                cards.append(card)

    try:
        fetched_pretty = datetime.fromisoformat(data["fetched_at"]).strftime("%-I:%M %p")
    except Exception:
        fetched_pretty = data["fetched_at"]

    return PAGE_TEMPLATE.format(
        fetched_pretty=html.escape(fetched_pretty),
        day_tabs=_day_tabs(data["dates"], data["today"]),
        theater_chips=theater_chips,
        format_chips=format_chips,
        movie_cards="\n".join(cards) if cards else
            '<div class="empty-state">No recliner showtimes available.</div>',
        theater_ids_json=json.dumps(theater_ids),
        initial_date=data["today"],
    )


def main():
    data = collect()
    (HERE / "showtimes_cache.json").write_text(json.dumps(data, indent=2))
    (HERE / "index.html").write_text(render_html(data))
    movie_n = len(data["movies"])
    listings = sum(len(day) for m in data["movies"] for day in m["by_date"].values())
    print(f"\nWrote index.html — {movie_n} movies, {listings} (movie × day × theater) listings "
          f"across {len(data['dates'])} days")


if __name__ == "__main__":
    main()
