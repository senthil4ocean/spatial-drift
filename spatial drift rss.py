#!/usr/bin/env python3
# ==============================================================================
#  SPATIAL DRIFT — RSS harvester  (no AI, no API key, no per-token cost)
# ==============================================================================
#
#  WHY THIS EXISTS
#  ---------------
#  The original spatial_drift_alert.py called the Anthropic API with the
#  server-side web_search tool. That cost money per run and stopped working
#  when the key died (the 15 Sep 2026 edition shipped 0 articles).
#
#  This script replaces the *harvesting* half with plain RSS/Atom feeds:
#    - Real articles, real URLs, real publication dates, straight from the
#      publishers. Nothing is generated, so nothing can be hallucinated.
#    - Zero cost. No key. No quota.
#    - Writes the SAME articles.json schema the website already reads, so the
#      GitHub Pages site and your portfolio keep working untouched.
#    - Sends the SAME Telegram message format as before.
#
#  What it does NOT do: write the one-line "significance" analysis, or
#  translate non-English items. That judgement needs a model. Those live in
#  the Claude dashboard artifact, which is refreshed by a scheduled Claude
#  task on your subscription. This script is the free, always-on backbone.
#
#  FEED VERIFICATION
#  -----------------
#  Every feed marked  # verified  below was fetched and confirmed to be a real,
#  working feed on 19 Sep 2026. Feeds marked  # unverified  are published feed
#  URLs that could not be reached from the sandbox used to write this script
#  (its egress is allow-listed) but should work from GitHub Actions. If one is
#  dead, the run does not fail: it is skipped and named in the FEED REPORT at
#  the end of the log. Prune anything that reports FAIL twice in a row.
#
#  ENV
#  ---
#    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   required to send; otherwise the
#                                           message is printed and not sent
#    RECENCY_DAYS      default 15
#    MAX_PER_DOMAIN    default 8
#    MAX_PER_SOURCE    default 2
#    SKIP_TELEGRAM     set to 1 to build JSON only
# ==============================================================================

import os
import re
import sys
import json
import time
import html
import hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta

try:
    import feedparser
except ImportError:
    sys.exit("Missing dependency: pip install feedparser")
try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

# ------------------------------------------------------------------ settings --
IST = timezone(timedelta(hours=5, minutes=30))
RECENCY_DAYS   = int(os.environ.get("RECENCY_DAYS", "15"))
MAX_PER_DOMAIN = int(os.environ.get("MAX_PER_DOMAIN", "8"))
MAX_PER_SOURCE = int(os.environ.get("MAX_PER_SOURCE", "2"))
SKIP_TELEGRAM  = os.environ.get("SKIP_TELEGRAM", "") == "1"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_MSG_LIMIT = 4096
FETCH_TIMEOUT = 30
UA = "Mozilla/5.0 (compatible; SpatialDriftBot/2.0; +https://github.com/senthil4ocean/spatial-drift)"

ROOT        = Path(__file__).resolve().parent
DATA_DIR    = ROOT / "data"
DOCS_DIR    = ROOT / "docs"
ARCHIVE_DIR = DATA_DIR / "archive"

# --------------------------------------------------------------------- feeds --
# Feeds whose entire output belongs to one domain.
DEDICATED = {
    "Plate Tectonics & Seismology": [
        ("USGS Earthquakes M4.5+", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_week.atom"),  # verified
    ],
    "Volcanology": [
        ("Smithsonian GVP Weekly Volcanic Activity", "https://volcano.si.edu/news/WeeklyVolcanoRSS.xml"),  # verified
        ("The Watchers", "https://watchers.news/feed/"),  # verified
    ],
    "Disaster & Hazard Monitoring": [
        ("GDACS", "https://www.gdacs.org/xml/rss.xml"),  # verified
        ("Copernicus Emergency Management Service", "https://mapping.emergency.copernicus.eu/latest/feed/"),  # verified
        ("ReliefWeb", "https://reliefweb.int/updates/rss.xml"),  # unverified - 403 from sandbox, works publicly
    ],
    "Mining & Geology": [
        ("MINING.COM", "https://www.mining.com/feed/"),  # verified
        ("Mining Weekly", "https://www.miningweekly.com/page/rss-feed/feed:latest-news"),  # verified
        ("Natural Resources Canada Geospatial", "https://natural-resources.canada.ca/science-data/science-research/geomatics/geospatial-news/rss.xml"),  # verified
    ],
    "Space & Geodesy": [
        ("Spaceflight Now", "https://spaceflightnow.com/feed/"),  # verified
        ("NASA JPL", "https://www.jpl.nasa.gov/feeds/news/"),  # verified
        ("Phys.org Space", "https://phys.org/rss-feed/space-news/"),  # verified
        ("ScienceDaily Space", "https://www.sciencedaily.com/rss/space_time.xml"),  # verified
        ("SpaceNews", "https://spacenews.com/feed/"),  # unverified - robots-blocked from sandbox
    ],
    "Remote Sensing & GIS": [
        ("NASA Science Earth", "https://science.nasa.gov/feed/?science_org=19791%2C22453"),  # verified
        ("ESA Observing the Earth", "https://www.esa.int/rssfeed/Our_Activities/Observing_the_Earth"),  # verified
        ("Geospatial World", "https://www.geospatialworld.net/feed/"),  # verified
    ],
    "Oceanography & Marine Science": [
        ("NOAA Ocean Service", "https://oceanservice.noaa.gov/rss/nosnews.xml"),  # verified
        ("Woods Hole (WHOI)", "https://www.whoi.edu/feed/"),  # verified
    ],
    "Cryosphere & Polar Science": [
        ("The Cryosphere (Copernicus)", "https://tc.copernicus.org/xml/rss2_0.xml"),  # verified
    ],
    "Climatology & Atmospheric Science": [
        ("Nature Climate Change", "https://www.nature.com/nclimate.rss"),  # verified
        ("Carbon Brief", "https://www.carbonbrief.org/feed/"),  # verified
    ],
    "India": [
        # India is the weakest feed area: ISRO publishes no feed, and several
        # Indian outlets block automated fetches. These are published feed URLs
        # that should work from GitHub Actions; check the FEED REPORT.
        ("PIB India", "https://www.pib.gov.in/ViewRss.aspx?reg=1&lang=1"),  # unverified - feed valid, was empty at check
        ("The Hindu Sci-Tech", "https://www.thehindu.com/sci-tech/feeder/default.rss"),  # unverified - blocked from sandbox
        ("Down To Earth", "https://www.downtoearth.org.in/rss/all"),  # unverified
        ("Indian Express Technology", "https://indianexpress.com/section/technology/feed/"),  # unverified - blocked from sandbox
    ],
    "Global Geospatial Intelligence": [
        ("Mongabay", "https://news.mongabay.com/feed/"),  # verified
    ],
}

# General science feeds, routed to a domain by keyword match.
SHARED = [
    ("Phys.org Earth", "https://phys.org/rss-feed/earth-news/"),                 # verified
    ("ScienceDaily Earth & Climate", "https://www.sciencedaily.com/rss/earth_climate.xml"),  # verified
    ("Nature", "https://www.nature.com/nature.rss"),                             # verified
    ("Nature Geoscience", "https://www.nature.com/ngeo.rss"),                    # verified
    ("Eos (AGU)", "https://eos.org/feed"),                                       # verified
]

# Domain order, emoji, and the keywords used to route SHARED items.
DOMAINS = [
    ("🛰️", "Remote Sensing & GIS",
     "satellite imagery|earth observation|remote sensing|lidar|synthetic aperture|\\bsar\\b|hyperspectral|"
     "sentinel-|landsat|planet labs|maxar|insar|interferometr|change detection|\\bgis\\b|geospatial|"
     "digital twin|arcgis|qgis|openstreetmap|geocod|cartograph|mapping technolog"),
    ("🌡️", "Climatology & Atmospheric Science",
     "climate change|global warming|atmospher|\\bipcc\\b|methane|carbon dioxide|\\bco2\\b|heatwave|heat wave|"
     "el ni[nñ]o|la ni[nñ]a|\\benso\\b|jet stream|climate model|aerosol|ozone|monsoon|greenhouse gas|"
     "carbon budget|emission|drought index|precipitation extreme"),
    ("🌊", "Oceanography & Marine Science",
     "ocean|marine|sea level|coral|\\bamoc\\b|salinity|acidification|deep.?sea|estuar|tide|"
     "altimetry|argo float|fisher|seagrass|mangrove|plankton|reef"),
    ("🏔️", "Plate Tectonics & Seismology",
     "earthquake|seismic|seismolog|tectonic|fault|subduction|mantle|rupture|aftershock|tsunami|"
     "crustal deformation|magnitude [0-9]|geodesy|\\bgnss\\b"),
    ("🌋", "Volcanology",
     "volcan|eruption|lava|magma|ash plume|pyroclastic|caldera|fumarole|\\bvei\\b"),
    ("⛏️", "Mining & Geology",
     "mining|\\bmine\\b|mineral|lithium|cobalt|rare earth|copper|nickel|uranium|ore body|"
     "geolog|stratigraph|sediment|mineralog|geochronolog|tailings|quarry|geomorpholog|soil erosion"),
    ("🚀", "Space & Geodesy",
     "satellite launch|launch|rocket|spacecraft|orbit|constellation|\\bgps\\b|galileo|beidou|navic|"
     "geodesy|reference frame|\\bitrf\\b|\\bgrace\\b|lunar|mars mission|space agency|space station"),
    ("🧊", "Cryosphere & Polar Science",
     "arctic|antarctic|ice sheet|ice shelf|permafrost|glacier|sea ice|polar|ice core|cryospher|"
     "greenland|thwaites|snow cover|frozen ground"),
    ("🆘", "Disaster & Hazard Monitoring",
     "flood|landslide|mudslide|cyclone|hurricane|typhoon|storm surge|wildfire|forest fire|drought|"
     "disaster|evacuat|casualt|displaced|humanitarian|early warning|dam failure|"
     "radiation leak|nuclear accident|chemical spill|avalanche|emergency"),
    ("🇮🇳", "India",
     "india|indian|\\bisro\\b|\\bnrsc\\b|survey of india|svamitva|gati shakti|bhuvan|navic|"
     "chennai|bengaluru|mumbai|delhi|kerala|tamil nadu|andhra|odisha|gujarat|bay of bengal"),
    ("🌐", "Global Geospatial Intelligence",
     "china|chinese|russia|russian|roscosmos|\\bcnsa\\b|beidou|gaofen|japan|\\bjaxa\\b|korea|kompsat|"
     "brazil|\\binpe\\b|argentina|conae|australia|csiro|new zealand|\\blinz\\b|pacific island|caribbean"),
]

# ROUTING PRIORITY — which domain claims a shared-feed item when several match.
#
# This is deliberately NOT the display order above. The thematic domains use
# broad keywords ("remote sensing", "satellite imagery"), so if they were tried
# first they would swallow the region-scoped domains: an ISRO remote-sensing
# story would file under Remote Sensing & GIS and never reach India. Most
# specific first, broadest (Remote Sensing & GIS) last.
ROUTE_ORDER = [
    "Disaster & Hazard Monitoring",      # event-driven, unambiguous
    "Volcanology",
    "Plate Tectonics & Seismology",
    "India",                             # region-scoped, and the reader's priority
    "Global Geospatial Intelligence",
    "Cryosphere & Polar Science",
    "Oceanography & Marine Science",
    "Climatology & Atmospheric Science",
    "Space & Geodesy",
    "Mining & Geology",
    "Remote Sensing & GIS",              # broadest — catch-all
]

# The Global domain must not hoover up every story that merely mentions a
# country, so it requires a region term AND a geospatial term.
GLOBAL_GEO_TERMS = ("satellit|remote sensing|earth observation|geospatial|mapping|cartograph|"
                    "\\bgis\\b|geodes|survey|imagery|\\bsar\\b|lidar|land cover|deforestation")

# --------------------------------------------------------------- utilities ----
def now_ist():
    return datetime.now(IST)

def esc_html(t):
    return html.escape(str(t or ""), quote=False)

def clean_text(t, limit=420):
    if not t:
        return ""
    t = re.sub(r"<[^>]+>", " ", str(t))          # strip markup
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        cut = t[:limit].rsplit(" ", 1)[0]
        t = cut + "…"
    return t

def is_valid_url(u):
    return isinstance(u, str) and u.startswith(("http://", "https://")) and len(u) > 12

def norm_title(t):
    return re.sub(r"[^a-z0-9]", "", str(t or "").lower())[:70]

def entry_datetime(e):
    for key in ("published_parsed", "updated_parsed"):
        tm = e.get(key)
        if tm:
            try:
                return datetime(*tm[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None

def fetch_feed(url):
    """Fetch with a real UA, then hand the bytes to feedparser."""
    r = requests.get(url, timeout=FETCH_TIMEOUT,
                     headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    r.raise_for_status()
    return feedparser.parse(r.content)

# ------------------------------------------------------------------ harvest ---
feed_report = []   # (source, url, status, item_count)

def harvest(source, url):
    """Return a list of normalised article dicts from one feed. Never raises."""
    try:
        parsed = fetch_feed(url)
    except Exception as exc:
        feed_report.append((source, url, f"FAIL {type(exc).__name__}", 0))
        print(f"    ✖  {source}: {type(exc).__name__}")
        return []

    entries = parsed.get("entries") or []
    if not entries:
        feed_report.append((source, url, "EMPTY", 0))
        print(f"    ·  {source}: feed parsed but empty")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENCY_DAYS)
    out, undated = [], 0
    for e in entries:
        link  = (e.get("link") or "").strip()
        title = clean_text(e.get("title"), 240)
        if not title or not is_valid_url(link):
            continue
        dt = entry_datetime(e)
        if dt is None:
            undated += 1
            continue                      # no date means we cannot vouch for recency
        if dt < cutoff:
            continue
        summary = clean_text(e.get("summary") or e.get("description") or "")
        out.append({
            "title":   title,
            "summary": summary,
            "source":  source,
            "date":    dt.astimezone(IST).strftime("%d %B %Y"),
            "url":     link,
            "significance": "",           # left blank: analysis belongs to the Claude dashboard
            "lang":    "en",
            "_dt":     dt,
            "_text":   (title + " " + summary).lower(),
        })
    feed_report.append((source, url, "OK", len(out)))
    note = f" ({undated} undated skipped)" if undated else ""
    print(f"    ✓  {source}: {len(out)} in window{note}")
    return out

def main():
    started = time.time()
    ts = now_ist()
    print("=" * 62)
    print(f"SPATIAL DRIFT — RSS harvest  {ts.strftime('%Y-%m-%d %H:%M IST')}")
    print(f"window={RECENCY_DAYS}d  max/domain={MAX_PER_DOMAIN}  max/source={MAX_PER_SOURCE}")
    print("=" * 62)

    # 1. dedicated feeds, bucketed straight into their domain
    buckets = {label: [] for _, label, _ in DOMAINS}
    for _, label, _ in DOMAINS:
        feeds = DEDICATED.get(label, [])
        if not feeds:
            continue
        print(f"\n  {label} — {len(feeds)} dedicated feed(s)")
        for source, url in feeds:
            buckets[label].extend(harvest(source, url))

    # 2. shared feeds, routed by keyword
    print(f"\n  Shared science feeds — {len(SHARED)}")
    shared_items = []
    for source, url in SHARED:
        shared_items.extend(harvest(source, url))

    patterns = {label: pattern for _, label, pattern in DOMAINS}
    unrouted = 0
    for item in shared_items:
        placed = False
        for label in ROUTE_ORDER:
            if not re.search(patterns[label], item["_text"]):
                continue
            # Global needs a region term AND a geospatial term, or it claims
            # every story that happens to name a country.
            if label == "Global Geospatial Intelligence" and not re.search(GLOBAL_GEO_TERMS, item["_text"]):
                continue
            buckets[label].append(item)
            placed = True
            break
        if not placed:
            unrouted += 1
    if unrouted:
        print(f"    ·  {unrouted} shared item(s) matched no domain keywords — dropped")

    # 3. rank, cap per source, cap per domain, dedupe globally
    seen_url, seen_title = set(), set()
    results = []
    for emoji, label, _ in DOMAINS:
        items = sorted(buckets[label], key=lambda a: a["_dt"], reverse=True)
        picked, per_source = [], {}
        for a in items:
            key_u = a["url"].split("?")[0].rstrip("/")
            key_t = norm_title(a["title"])
            if key_u in seen_url or key_t in seen_title:
                continue
            if per_source.get(a["source"], 0) >= MAX_PER_SOURCE:
                continue
            seen_url.add(key_u)
            seen_title.add(key_t)
            per_source[a["source"]] = per_source.get(a["source"], 0) + 1
            picked.append({k: v for k, v in a.items() if not k.startswith("_")})
            if len(picked) >= MAX_PER_DOMAIN:
                break
        results.append((f"{emoji} {label}", picked))
        print(f"  {emoji} {label}: {len(picked)}")

    # 4. write the website JSON, in the schema the site already reads
    source_summary = {}
    for _, arts in results:
        for a in arts:
            source_summary[a["source"]] = source_summary.get(a["source"], 0) + 1

    total = sum(len(a) for _, a in results)
    payload = {
        "generated_at_ist": ts.strftime("%Y-%m-%d %H:%M:%S IST"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "generated_iso":    ts.isoformat(),
        "display_date":     ts.strftime("%A, %d %B %Y"),
        "display_time":     ts.strftime("%I:%M %p IST"),
        "stats": {
            "total_articles":    total,
            "domains_total":     len(results),
            "domains_with_news": sum(1 for _, a in results if a),
            "unique_sources":    len(source_summary),
            "elapsed":           f"{int(time.time() - started)}s",
        },
        "source_summary": source_summary,
        "domains": [{"label": lbl, "count": len(arts), "articles": arts} for lbl, arts in results],
    }

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    for d in (DATA_DIR, DOCS_DIR, ARCHIVE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for path in (DATA_DIR / "articles.json", DOCS_DIR / "articles.json"):
        path.write_text(text, encoding="utf-8")
        print(f"\n  💾 wrote {total} articles → {path.relative_to(ROOT)}")
    snap = ARCHIVE_DIR / f"articles-{ts.strftime('%Y-%m-%d')}.json"
    snap.write_text(text, encoding="utf-8")
    print(f"  📦 archived → {snap.relative_to(ROOT)}")

    # 5. feed health report — prune anything that FAILs twice running
    print("\n  FEED REPORT")
    for source, url, status, n in feed_report:
        flag = "ok " if status == "OK" else "!! "
        print(f"    {flag}{status:18} {n:>3}  {source}")
    dead = [f"{s} ({st})" for s, _, st, _ in feed_report if st != "OK"]

    # 6. Telegram
    if total == 0:
        print("\n  ⚠️  No articles in window — sending nothing. Check the FEED REPORT above.")
        return 0
    messages = build_messages(results, payload)
    if SKIP_TELEGRAM or not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("\n  ℹ️  Telegram not configured (or SKIP_TELEGRAM=1) — message preview:\n")
        print(messages[0][:1200])
    else:
        for i, m in enumerate(messages, 1):
            ok, info = send_telegram(m)
            print(f"  📨 part {i}/{len(messages)}: {'sent' if ok else 'FAILED ' + str(info)}")
            if len(messages) > 1:
                time.sleep(2)
    if dead:
        print(f"\n  ⚠️  {len(dead)} feed(s) not OK: {', '.join(dead[:8])}")
    print(f"\n  done in {int(time.time() - started)}s")
    return 0

# ----------------------------------------------------------------- telegram ---
def build_messages(results, payload):
    ts = now_ist()
    total = payload["stats"]["total_articles"]
    good  = payload["stats"]["domains_with_news"]
    links = sum(1 for _, arts in results for a in arts if is_valid_url(a["url"]))

    header = (
        f"🌍 <b>SPATIAL DRIFT</b>\n"
        f"<i>Explore · Analyze · Anticipate</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 <b>{esc_html(ts.strftime('%I:%M %p IST'))}</b>\n"
        f"📅 <i>{esc_html(ts.strftime('%A, %d %B %Y'))}</i>\n\n"
        f"📰 <b>{total}</b> articles · <b>{good}/{len(results)}</b> domains · "
        f"<b>{links}</b> live links\n\n"
    )
    footer = (
        f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>SPATIAL DRIFT</b>\n"
        f"<i>Tap any title to open the article.</i>\n"
        f"<i>Harvested directly from publisher feeds — every link is a real article.</i>\n"
        f"<i>Next brief on the 1st or 15th, 7:23 AM IST.</i>"
    )

    blocks = []
    for label, arts in results:
        if not arts:
            blocks.append(f"<b>{esc_html(label)}</b>\n<i>— no fresh items this cycle</i>\n")
            continue
        b = f"<b>{esc_html(label)}</b>\n"
        for i, a in enumerate(arts, 1):
            t = esc_html(a["title"])
            if is_valid_url(a["url"]):
                safe = a["url"].replace("&", "&amp;").replace('"', "%22")
                t = f'<a href="{safe}">{t}</a>'
            b += f"<b>{i}.</b> {t}\n   <i>📰 {esc_html(a['source'])} · {esc_html(a['date'])}</i>\n"
        blocks.append(b)

    whole = header + "\n".join(blocks) + footer
    if len(whole) <= TELEGRAM_MSG_LIMIT:
        return [whole]

    msgs, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + len(footer) + 40 > TELEGRAM_MSG_LIMIT:
            msgs.append(cur + "\n<i>— continued ↓</i>")
            cur = f"🌍 <b>SPATIAL DRIFT</b> <i>(part {len(msgs)+1})</i>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        cur += b + "\n"
    msgs.append(cur + footer if len(cur) + len(footer) <= TELEGRAM_MSG_LIMIT else cur)
    return msgs

def send_telegram(text, retries=3):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(api, json=payload, timeout=45)
            if r.status_code == 200:
                return True, "ok"
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code == 429:
                wait = int(r.json().get("parameters", {}).get("retry_after", 5)) + 1
                time.sleep(wait)
                continue
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(3 + attempt * 3)
    return False, last

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
