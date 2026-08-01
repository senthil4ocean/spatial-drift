"""
╔═══════════════════════════════════════════════════════╗
║        SPATIAL DRIFT — Daily Intelligence Alert        ║
║        Explore. Analyze. Anticipate.                   ║
║        v6.1 — Global Authoritative Sourcing            ║
╚═══════════════════════════════════════════════════════╝

CONFIRMATION OF SOURCE STRATEGY
─────────────────────────────────────────────────────────
The previous versions had Science Daily as the LOCKED priority,
which is why repeats and stale articles were a problem.

THIS VERSION uses a BROAD POOL of 12-18 authoritative sources per
domain, including:
- Peer-reviewed journals: Nature, Science, PNAS, AGU, IEEE
- Space agencies: NASA, ESA, ISRO, JAXA, CNES, DLR, CSA
- Geological surveys: USGS, BGS, GSC, Geoscience Australia
- Climate orgs: NOAA, IPCC, ECMWF, Met Office, WMO
- Industry publications: Geospatial World, GIM, SpaceNews, Mining.com
- Mainstream news: Reuters, BBC Science, AP, Phys.org, Eos
- Open data portals: Copernicus, EarthData, OpenStreetMap

Science Daily is included but NOT prioritized — it's just one
trusted source among many.

KEY FIXES IN v6.1
─────────────────────────────────────────────────────────
1. Knowledge fallback if web_search returns nothing (no more
   blank domains).
2. Source-diversity enforcement: each source capped at 2 items
   per domain.
3. Source diversity LOGGED to GitHub Actions output so you can
   verify which sources were used.
4. Cache-buster timestamp in queries to avoid stale cached
   web_search results.
"""

import os
import re
import json
import time
import html
import requests
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path

# ── Credentials ────────────────────────────────────────────────────────────────
# All credentials come ONLY from environment variables (GitHub Secrets).
# No hardcoded fallbacks — a hardcoded key in a public repo gets auto-revoked
# by Anthropic's secret scanners within minutes.
#
# In GitHub Actions these are injected from:
#   Settings → Secrets and variables → Actions
# Required secrets: ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "").strip()


def _check_credentials():
    """Fail fast with a clear message if any required secret is missing."""
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        print()
        print("  ❌ MISSING REQUIRED SECRETS:")
        for m in missing:
            print(f"     • {m}")
        print()
        print("  FIX: GitHub repo → Settings → Secrets and variables → Actions")
        print("       Add each missing secret, then re-run the workflow.")
        print()
        raise SystemExit(f"Missing secrets: {', '.join(missing)}")
    # Light sanity check on the API key shape
    if not ANTHROPIC_API_KEY.startswith("sk-ant-"):
        print()
        print("  ⚠️  ANTHROPIC_API_KEY does not start with 'sk-ant-'.")
        print("      The secret value may be malformed (extra quotes/spaces?).")
        print("      Re-create the secret cleanly if the run fails with 401.")
        print()

# ── Configuration ──────────────────────────────────────────────────────────────
MAX_ARTICLES_PER_TOPIC = 6
MAX_RETRIES_PER_TOPIC  = 3
MAX_TOKENS             = 4096
TELEGRAM_MSG_LIMIT     = 4000
DELAY_BETWEEN_DOMAINS  = 8
RECENCY_WINDOW_DAYS    = 7    # weekly run — pull only past week's news
MAX_PER_SOURCE         = 2    # source-diversity cap

# Output paths
ROOT_DIR  = Path(__file__).parent
DATA_DIR  = ROOT_DIR / "data"
DOCS_DIR  = ROOT_DIR / "docs"
DATA_DIR.mkdir(exist_ok=True)
DOCS_DIR.mkdir(exist_ok=True)
ARTICLES_FILES = [
    DATA_DIR / "articles.json",
    DOCS_DIR / "articles.json",
]
LAST_RUN_FILE = DATA_DIR / "last_run.txt"

IST = timezone(timedelta(hours=5, minutes=30))
TARGET_SLOTS = [(7, 23)]  # weekly Saturday run at 7:23 AM IST


# ═══════════════════════════════════════════════════════════════════════════════
# TOPIC DEFINITIONS — broad global authoritative source pool per domain
# ═══════════════════════════════════════════════════════════════════════════════

TOPICS = [
    {
        "emoji": "🛰️",
        "label": "Remote Sensing & Earth Observation",
        "keywords": "satellite imagery, earth observation, LiDAR, SAR, hyperspectral, multispectral, Sentinel, Landsat, Planet Labs, Maxar, ICEYE, optical sensing",
        "trusted_sources": [
            # Peer-reviewed
            "Nature", "Science", "Remote Sensing of Environment",
            "IEEE Transactions on Geoscience and Remote Sensing", "MDPI Remote Sensing",
            "ISPRS Journal of Photogrammetry and Remote Sensing",
            # Agencies / data providers
            "NASA", "ESA", "ISRO", "JAXA", "USGS", "NOAA", "Copernicus", "CNES", "DLR",
            "Planet Labs", "Maxar Technologies", "Airbus Defence and Space",
            # News / industry
            "SpaceNews", "Geospatial World", "GIM International", "Eos (AGU)",
            "Reuters", "BBC Science", "Phys.org", "ScienceDaily",
        ],
    },
    {
        "emoji": "🗺️",
        "label": "GIS & Geospatial Technology",
        "keywords": "GIS, geospatial AI, digital twin, spatial analysis, ArcGIS, QGIS, 3D city model, OpenStreetMap, geocoding, location intelligence",
        "trusted_sources": [
            "Esri", "Geospatial World", "GIM International", "Directions Magazine",
            "International Journal of Geographical Information Science",
            "Cartography and Geographic Information Science", "Transactions in GIS",
            "MIT Technology Review", "TechCrunch", "IEEE Spectrum",
            "Google Maps Platform", "Microsoft Planetary Computer",
            "OpenStreetMap Foundation", "QGIS", "OGC (Open Geospatial Consortium)",
            "Reuters", "Phys.org", "ScienceDaily",
        ],
    },
    {
        "emoji": "🌡️",
        "label": "Climatology & Atmospheric Science",
        "keywords": "climate change, global warming, atmospheric science, IPCC, methane, CO2, heatwave, sea ice, cyclone, ENSO, jet stream, climate model",
        "trusted_sources": [
            "Nature Climate Change", "Science", "Nature Geoscience", "PNAS",
            "Geophysical Research Letters", "Atmospheric Chemistry and Physics",
            "Journal of Climate", "Climate Dynamics",
            "IPCC", "WMO", "NOAA", "NASA Climate", "ECMWF",
            "UK Met Office", "Copernicus Climate Change Service",
            "Reuters", "BBC Science", "AP News", "Phys.org", "ScienceDaily",
            "Carbon Brief", "Inside Climate News",
        ],
    },
    {
        "emoji": "🌊",
        "label": "Oceanography & Marine Science",
        "keywords": "oceanography, sea level rise, ocean temperature, marine ecosystems, coral reef, ocean currents, deep sea, salinity, AMOC, thermohaline",
        "trusted_sources": [
            "Nature", "Nature Geoscience", "Science",
            "Journal of Geophysical Research: Oceans", "Ocean Science",
            "Limnology and Oceanography", "Marine Geology",
            "NOAA", "NASA Earth Science", "Scripps Institution of Oceanography",
            "Woods Hole Oceanographic Institution", "WMO",
            "Reuters", "BBC Science", "AP News", "Phys.org",
            "Eos (AGU)", "ScienceDaily", "Smithsonian Ocean",
        ],
    },
    {
        "emoji": "🏔️",
        "label": "Plate Tectonics & Seismology",
        "keywords": "earthquake, seismology, plate tectonics, fault, subduction, mantle, GPS geodesy, seismic activity, tsunami warning",
        "trusted_sources": [
            "Nature Geoscience", "Science", "Geophysical Research Letters",
            "Seismological Research Letters", "Earth and Planetary Science Letters",
            "Journal of Geophysical Research: Solid Earth",
            "USGS Earthquake Hazards", "EMSC", "IRIS", "GFZ Potsdam",
            "GNS Science (NZ)", "Geoscience Australia",
            "Reuters", "BBC Science", "AP News", "Phys.org", "ScienceDaily",
            "Eos (AGU)",
        ],
    },
    {
        "emoji": "🌋",
        "label": "Volcanology",
        "keywords": "volcanic eruption, volcano monitoring, lava flow, magma, ash plume, pyroclastic, volcanic gas, caldera, Smithsonian GVP",
        "trusted_sources": [
            "Nature Geoscience", "Science", "Journal of Volcanology and Geothermal Research",
            "Bulletin of Volcanology",
            "USGS Volcano Hazards Program", "Smithsonian Global Volcanism Program",
            "INGV (Italy)", "Icelandic Met Office", "VolcanoDiscovery",
            "JMA (Japan Met Agency)", "Indonesian PVMBG", "Philippine PHIVOLCS",
            "Reuters", "BBC Science", "AP News", "Phys.org", "ScienceDaily",
            "Eos (AGU)",
        ],
    },
    {
        "emoji": "⛏️",
        "label": "Mining & Mineral Resources",
        "keywords": "mining, mineral exploration, critical minerals, lithium, cobalt, rare earth elements, copper, nickel, uranium, sustainable mining",
        "trusted_sources": [
            "Mining.com", "Mining Magazine", "Mining Journal", "Mining Weekly",
            "Reuters Mining", "Bloomberg Metals & Mining", "S&P Global Market Intelligence",
            "USGS Mineral Resources", "BGS (British Geological Survey)",
            "Geological Survey of Canada", "Geoscience Australia",
            "Nature Geoscience", "Economic Geology", "Ore Geology Reviews",
            "Financial Times Mining", "Wall Street Journal",
            "Phys.org", "ScienceDaily",
        ],
    },
    {
        "emoji": "🪨",
        "label": "Geology & Geomorphology",
        "keywords": "geology, geological discovery, rock formation, stratigraphy, paleoclimate, sedimentology, mineralogy, geochronology, geomorphology",
        "trusted_sources": [
            "Nature Geoscience", "Geology (GSA)", "Earth and Planetary Science Letters",
            "Science", "Geological Society of America Bulletin",
            "Quaternary Science Reviews", "Journal of Sedimentary Research",
            "USGS", "BGS", "Geological Survey of Canada", "Geoscience Australia",
            "GFZ Potsdam",
            "Reuters", "BBC Science", "Smithsonian", "National Geographic",
            "Phys.org", "ScienceDaily", "Eos (AGU)",
        ],
    },
    {
        "emoji": "🚀",
        "label": "Space & Geodesy",
        "keywords": "satellite launch, space mission, earth observation satellite, geodesy, GNSS, GPS, reference frame, ITRF, GRACE, lunar mission, Mars mission",
        "trusted_sources": [
            "SpaceNews", "Spaceflight Now", "Ars Technica Space", "The Space Review",
            "NASA", "ESA", "ISRO", "JAXA", "CNES", "DLR", "CSA", "Roscosmos",
            "SpaceX", "Blue Origin", "Rocket Lab",
            "Reuters", "BBC Science", "AP News", "Nature Astronomy",
            "Sky and Telescope", "Phys.org", "ScienceDaily",
        ],
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════════════════════════

NEWS_SYSTEM_PROMPT = f"""You are the news research engine for SPATIAL DRIFT — a geospatial intelligence platform.

YOUR JOB: Use web_search to find the most RECENT real news articles for ONE geospatial domain.

ABSOLUTE REQUIREMENTS:
1. RECENCY: Articles MUST be from the last {RECENCY_WINDOW_DAYS} days. Reject anything older.
2. SOURCE DIVERSITY: Pull articles from MANY DIFFERENT outlets. Do not concentrate on a single source.
   The user wants global coverage — mix peer-reviewed journals, government agencies, industry
   publications, and mainstream news outlets from around the world.
3. AUTHENTIC SOURCES ONLY: Use only real, reputable publishers from the provided trusted list (or
   equivalent quality). No blogs, no aggregators, no SEO farms, no opinion sites.
4. REAL URLs: Every article URL must come from your actual web_search results — never fabricate.
5. DIFFERENT SOURCES: At most {MAX_PER_SOURCE} articles from any single source. Aim for {MAX_ARTICLES_PER_TOPIC}
   articles from {MAX_ARTICLES_PER_TOPIC} different publishers.

SEARCH STRATEGY:
- Run at least 3-4 different web_searches with varied keywords and source hints
- Combine results across queries before picking the best
- Examples:
  • "<keyword> latest news 2026"
  • "<keyword> site:nature.com"
  • "<keyword> site:reuters.com"
  • "<keyword> breakthrough this month"

OUTPUT FORMAT — STRICT:
Return ONLY a valid JSON array. Start with [ and end with ]. Nothing before or after.
No code fences, no markdown, no commentary, no <cite> tags, no HTML.
Plain text only inside JSON strings.

SCHEMA:
{{
  "title": "Plain text headline, max 100 chars",
  "summary": "1-2 sentence summary in plain text",
  "source": "Real publication name (e.g., 'Nature', 'Reuters', 'NASA', 'USGS', 'BBC Science')",
  "date": "Specific recent date like '8 May 2026' or '3 days ago'",
  "url": "https://... — REAL URL from web_search results",
  "significance": "Plain text, max 120 chars, why geospatial pros should care"
}}

Output the JSON array and nothing else."""


KB_FALLBACK_PROMPT = """You are a geospatial sciences news researcher.

Web search returned insufficient results. Based on your training knowledge,
list significant recent articles, papers, or developments from 2025-2026
for the given geospatial domain.

Use real source names (Nature, Reuters, NASA, USGS, etc.). For URL, use the
source's main domain if you don't know the specific article URL.

Return a JSON array following the exact schema below. Plain text only.
Do not return an empty array — provide at least 2-3 known items.

SCHEMA:
[
  {
    "title": "Plain text headline",
    "summary": "1-2 sentence summary",
    "source": "Publication name",
    "date": "Approximate date",
    "url": "https://... source URL",
    "significance": "Why geospatial pros care"
  }
]

Output ONLY the JSON array."""


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def clean_text(text) -> str:
    if text is None:
        return ""
    s = str(text)
    s = re.sub(r"<cite[^>]*>", "", s)
    s = re.sub(r"</cite>", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_valid_url(url) -> bool:
    if not isinstance(url, str):
        return False
    url = url.strip()
    return url.startswith(("http://", "https://")) and " " not in url and len(url) < 500


def esc_html(text) -> str:
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def now_ist() -> datetime:
    return datetime.now(IST)


def nearest_slot_label(ts: datetime) -> str:
    hour = ts.hour + ts.minute / 60
    best = min(TARGET_SLOTS, key=lambda s: abs(s[0] - hour))
    return f"{ts.strftime('%Y-%m-%d')}_{best[0]:02d}00"


# ═══════════════════════════════════════════════════════════════════════════════
# ANTHROPIC API
# ═══════════════════════════════════════════════════════════════════════════════

def call_anthropic(system_prompt: str, user_msg: str, use_search: bool = True,
                   retries: int = MAX_RETRIES_PER_TOPIC) -> dict:
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_msg}],
    }
    if use_search:
        payload["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]

    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
                timeout=180,
            )
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            try:
                body = e.response.text[:300]
            except Exception:
                body = ""
            last_err = f"HTTP {e.response.status_code}: {body}"
            if e.response.status_code in (429, 529):
                if attempt < retries:
                    wait = 15 + (attempt * 10)
                    print(f"        ↻  Rate limited. Waiting {wait}s before retry {attempt+1}...")
                    time.sleep(wait)
                    continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

        if attempt < retries:
            wait = 3 + (attempt * 4)
            print(f"        ↻  Retry {attempt+1}/{retries} in {wait}s ({last_err[:100]})")
            time.sleep(wait)

    raise RuntimeError(f"API call failed: {last_err}")


def extract_response_text(api_data: dict) -> str:
    parts = [b.get("text", "") for b in api_data.get("content", []) if b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def extract_search_urls(api_data: dict) -> list:
    found = []
    for block in api_data.get("content", []):
        if block.get("type") != "web_search_tool_result":
            continue
        content = block.get("content", [])
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            title = item.get("title", "")
            if isinstance(url, str) and is_valid_url(url):
                found.append({"url": url, "title": title})
    return found


def extract_json_array(raw: str) -> list:
    if not raw:
        return []

    cleaned = re.sub(r"```(?:json)?\s*", "", raw)
    cleaned = cleaned.replace("```", "")
    cleaned = re.sub(r"<cite[^>]*>", "", cleaned)
    cleaned = re.sub(r"</cite>", "", cleaned)
    cleaned = cleaned.strip()

    start = cleaned.find("[")
    if start == -1:
        return []

    depth = 0
    in_str = False
    esc_next = False
    end = -1
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if esc_next:
            esc_next = False
            continue
        if ch == "\\":
            esc_next = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end == -1:
        return []

    candidate = cleaned[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        candidate2 = re.sub(r"<[^>]+>", "", candidate)
        try:
            return json.loads(candidate2)
        except json.JSONDecodeError:
            print(f"        ⚠️  JSON parse failed: {e}")
            return []


def best_url_match(title: str, search_urls: list) -> str:
    if not title or not search_urls:
        return ""
    best_score = 0.0
    best_url = ""
    t = title.lower()
    for entry in search_urls:
        et = entry.get("title", "").lower()
        if not et:
            continue
        s = SequenceMatcher(None, t, et).ratio()
        if s > best_score:
            best_score = s
            best_url = entry["url"]
    return best_url if best_score >= 0.35 else ""


# ═══════════════════════════════════════════════════════════════════════════════
# FETCH ONE TOPIC — with diverse sources and knowledge fallback
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_topic(topic: dict, run_token: str) -> list:
    """Fetch news for one domain. Pass 1 = web_search across diverse sources.
    Pass 2 = knowledge fallback if web returns empty."""
    label = topic["label"]
    keywords = topic["keywords"]
    sources = topic["trusted_sources"]

    ts = now_ist()
    today_str = ts.strftime("%d %B %Y")
    sources_str = ", ".join(sources[:18])  # cap displayed list

    # ── PASS 1: web search with diverse sourcing ──
    user_msg = f"""Find the latest news articles for this geospatial domain.

DOMAIN: {label}
KEYWORDS: {keywords}
DATE WINDOW: Past {RECENCY_WINDOW_DAYS} days (today is {today_str})
RUN ID: {run_token}  (use this to vary your searches and avoid cached results)

GLOBAL TRUSTED SOURCES (pull from AS MANY DIFFERENT ONES as possible):
{sources_str}

Important: Mix sources. Don't return all articles from one outlet.
At most {MAX_PER_SOURCE} per source. Aim for {MAX_ARTICLES_PER_TOPIC} articles from {MAX_ARTICLES_PER_TOPIC} different publishers.

Run multiple varied web_searches:
- "{keywords.split(',')[0].strip()} latest news"
- "{keywords.split(',')[0].strip()} 2026 research"
- "{label.lower()} discovery this month"
- "{keywords.split(',')[1].strip() if ',' in keywords else keywords} new findings"

Return {MAX_ARTICLES_PER_TOPIC} articles as a JSON array per the schema. Plain text only.
EVERY article needs a real URL from web_search."""

    articles = []
    search_urls = []
    try:
        print(f"        🔍 Pass 1: web search across {len(sources)} trusted sources...")
        api_data = call_anthropic(NEWS_SYSTEM_PROMPT, user_msg, use_search=True)
        raw_text = extract_response_text(api_data)
        articles = extract_json_array(raw_text) if raw_text else []
        search_urls = extract_search_urls(api_data)
        print(f"        🔍 Pass 1 returned: {len(articles)} articles, {len(search_urls)} candidate URLs")
    except Exception as e:
        print(f"        ⚠️  Pass 1 failed: {e}")

    # ── PASS 2: knowledge fallback if web returned empty ──
    if not articles:
        print(f"        🔍 Pass 2: knowledge fallback...")
        try:
            kb_msg = f"""Domain: {label}
Keywords: {keywords}
Provide 3-5 significant 2025-2026 developments from your training knowledge.
Use real source names from this list: {sources_str[:500]}
Return as JSON array per schema."""
            api_data = call_anthropic(KB_FALLBACK_PROMPT, kb_msg, use_search=False, retries=1)
            raw_text = extract_response_text(api_data)
            articles = extract_json_array(raw_text) if raw_text else []
            print(f"        🔍 Pass 2 returned: {len(articles)} fallback articles")
        except Exception as e:
            print(f"        ❌ Pass 2 also failed: {e}")

    if not articles:
        return []

    # ── Clean, validate, dedupe by source ──
    cleaned = []
    seen_sources = {}
    for a in articles:
        if not isinstance(a, dict):
            continue
        obj = {
            "title":        clean_text(a.get("title", "")),
            "summary":      clean_text(a.get("summary", "")),
            "source":       clean_text(a.get("source", "")),
            "date":         clean_text(a.get("date", "")),
            "significance": clean_text(a.get("significance", "")),
            "url":          str(a.get("url", "")).strip(),
        }
        if not is_valid_url(obj["url"]) and search_urls:
            obj["url"] = best_url_match(obj["title"], search_urls) or ""
        if not obj["title"]:
            continue
        src_key = obj["source"].lower().strip()
        if seen_sources.get(src_key, 0) >= MAX_PER_SOURCE:
            continue
        seen_sources[src_key] = seen_sources.get(src_key, 0) + 1
        cleaned.append(obj)

    # ── Log source diversity ──
    if cleaned:
        srcs_used = sorted(set(a["source"] for a in cleaned if a["source"]))
        print(f"        📚 Sources used ({len(srcs_used)}): {', '.join(srcs_used[:6])}{'...' if len(srcs_used) > 6 else ''}")

    return cleaned


def deduplicate_across_domains(all_results: list) -> list:
    """Remove articles whose titles closely match across domains."""
    seen_titles = []
    out = []
    for label, articles in all_results:
        keep = []
        for a in articles:
            t = (a.get("title") or "").lower().strip()
            if not t:
                continue
            is_dupe = False
            for prev in seen_titles:
                if SequenceMatcher(None, t, prev).ratio() > 0.85:
                    is_dupe = True
                    break
            if not is_dupe:
                seen_titles.append(t)
                keep.append(a)
        out.append((label, keep))
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE BUILDING
# ═══════════════════════════════════════════════════════════════════════════════

def build_compact_message(all_results: list, run_meta: dict) -> list:
    ts = now_ist()
    today = ts.strftime("%A, %d %B %Y")
    time_str = ts.strftime("%I:%M %p IST")

    successful = sum(1 for _, a in all_results if a)
    total_articles = sum(len(a) for _, a in all_results)
    total_links = sum(1 for _, ar in all_results for a in ar if is_valid_url(a.get("url", "")))

    header = (
        f"🌍 <b>SPATIAL DRIFT</b>\n"
        f"<i>Explore · Analyze · Anticipate</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 <b>{esc_html(time_str)}</b>\n"
        f"📅 <i>{esc_html(today)}</i>\n\n"
        f"📰 <b>{total_articles}</b> articles · <b>{successful}/{len(all_results)}</b> domains · "
        f"<b>{total_links}</b> live links\n\n"
    )

    topic_blocks = []
    for topic_label, articles in all_results:
        if not articles:
            topic_blocks.append(
                f"<b>{esc_html(topic_label)}</b>\n"
                f"<i>— no fresh items this cycle</i>\n"
            )
            continue
        block = f"<b>{esc_html(topic_label)}</b>\n"
        for i, a in enumerate(articles, 1):
            title  = esc_html(a["title"])
            source = esc_html(a["source"])
            date   = esc_html(a["date"])
            url    = a["url"]
            if is_valid_url(url):
                safe_url = url.replace("&", "&amp;").replace('"', "%22")
                title_html = f'<b>{i}.</b> <a href="{safe_url}">{title}</a>'
            else:
                title_html = f'<b>{i}.</b> {title}'
            block += f"{title_html}\n   <i>📰 {source} · {date}</i>\n"
        topic_blocks.append(block)

    footer = (
        f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>SPATIAL DRIFT</b>\n"
        f"<i>Tap any title to open the article.</i>\n"
        f"<i>Visit dashboard for full summaries + LinkedIn/blog generator.</i>\n"
        f"<i>Next brief next Saturday at 7:23 AM IST.</i>"
    )

    full_message = header + "\n".join(topic_blocks) + footer
    if len(full_message) <= TELEGRAM_MSG_LIMIT:
        return [full_message]

    messages = []
    current = header
    for block in topic_blocks:
        if len(current) + len(block) + len(footer) + 30 > TELEGRAM_MSG_LIMIT:
            current += f"\n<i>— continued ↓</i>"
            messages.append(current)
            current = f"🌍 <b>SPATIAL DRIFT</b> <i>(part {len(messages)+1})</i>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        current += block + "\n"

    if len(current) + len(footer) <= TELEGRAM_MSG_LIMIT:
        current += footer
        messages.append(current)
    else:
        messages.append(current)
        messages.append(footer)
    return messages


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM SEND
# ═══════════════════════════════════════════════════════════════════════════════

def send_telegram(text: str, retries: int = 3) -> tuple:
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.post(api_url, json=payload, timeout=25)
            r.raise_for_status()
            return True, None
        except requests.exceptions.HTTPError as e:
            try:
                last_err = e.response.json().get("description", str(e))
            except Exception:
                last_err = str(e)
            if "parse" in (last_err or "").lower() or "entities" in (last_err or "").lower():
                plain = re.sub(r"<[^>]+>", "", text)
                try:
                    r2 = requests.post(api_url, json={
                        "chat_id": TELEGRAM_CHAT_ID, "text": plain[:4096],
                    }, timeout=20)
                    r2.raise_for_status()
                    return True, "plain-text fallback"
                except Exception as e2:
                    last_err = f"{last_err} | plain fallback: {e2}"
        except Exception as e:
            last_err = str(e)
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    return False, last_err


# ═══════════════════════════════════════════════════════════════════════════════
# SAVE FOR WEBSITE
# ═══════════════════════════════════════════════════════════════════════════════

def save_articles_for_website(all_results: list, run_meta: dict, source_summary: dict):
    ts = now_ist()
    payload = {
        "generated_at_ist":   ts.strftime("%Y-%m-%d %H:%M:%S IST"),
        "generated_at_utc":   datetime.now(timezone.utc).isoformat(),
        "generated_iso":      ts.isoformat(),
        "display_date":       ts.strftime("%A, %d %B %Y"),
        "display_time":       ts.strftime("%I:%M %p IST"),
        "stats": {
            "total_articles":   sum(len(a) for _, a in all_results),
            "domains_total":    len(all_results),
            "domains_with_news": sum(1 for _, a in all_results if a),
            "unique_sources":   len(source_summary),
            "elapsed":          run_meta.get("elapsed", "—"),
        },
        "source_summary": source_summary,  # which sources contributed how many articles
        "domains": [
            {"label": label, "count": len(articles), "articles": articles}
            for label, articles in all_results
        ],
    }
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        for path in ARTICLES_FILES:
            path.write_text(text, encoding="utf-8")
            print(f"  💾 Wrote {payload['stats']['total_articles']} articles → {path.name}")
    except Exception as e:
        print(f"  ⚠️  Could not save articles.json: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# DEDUPLICATION
# ═══════════════════════════════════════════════════════════════════════════════
#
# Why: GitHub schedules each slot's primary + backup cron 30 min apart. Without
# dedup, both would send Telegram messages for the same slot.
#
# Bypass: Set FORCE_RUN=1 in the environment (the workflow sets this whenever
# the run is manual via workflow_dispatch or a pull_request). This ensures
# manual reruns ALWAYS execute end-to-end.
#
# Time-window dedup: We only skip if the LAST run completed less than 60
# minutes ago for THIS SAME SLOT. After 60 minutes we allow a re-run anyway
# (so the next day's same slot will run cleanly).

DEDUP_WINDOW_MINUTES = 60


def should_skip_this_run() -> bool:
    # Manual / forced runs always proceed
    if os.environ.get("FORCE_RUN", "").strip() in ("1", "true", "TRUE", "yes"):
        print("  ▶  FORCE_RUN=1 set — bypassing dedup, running full pipeline.")
        return False

    ts = now_ist()
    slot = nearest_slot_label(ts)

    if not LAST_RUN_FILE.exists():
        return False

    try:
        contents = LAST_RUN_FILE.read_text().strip()
        # New format: "slot|iso_timestamp"; old format: "slot"
        parts = contents.split("|", 1)
        last_slot = parts[0]
        last_ts_str = parts[1] if len(parts) > 1 else None
    except Exception:
        return False

    if last_slot != slot:
        return False

    # Same slot — but check if enough time has passed to allow a re-run
    if last_ts_str:
        try:
            last_ts = datetime.fromisoformat(last_ts_str)
            elapsed_min = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60
            if elapsed_min > DEDUP_WINDOW_MINUTES:
                print(f"  ⏰  Slot {slot} ran {elapsed_min:.0f} min ago (>{DEDUP_WINDOW_MINUTES}m). Allowing re-run.")
                return False
            else:
                print(f"  ⏭️  Slot {slot} ran {elapsed_min:.0f} min ago. Skipping duplicate.")
                return True
        except Exception:
            pass

    # Old format (no timestamp) — be safe and skip
    print(f"  ⏭️  Slot {slot} already processed (no timestamp). Skipping duplicate run.")
    return True


def mark_run_complete():
    ts = now_ist()
    slot = nearest_slot_label(ts)
    iso_now = datetime.now(timezone.utc).isoformat()
    try:
        LAST_RUN_FILE.write_text(f"{slot}|{iso_now}")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    start = time.time()
    ts = now_ist()
    # Unique run token so the model varies its searches between runs
    run_token = ts.strftime("%Y%m%d-%H%M")

    print()
    print("╔══════════════════════════════════════════════╗")
    print("║   SPATIAL DRIFT v6.1 — Weekly Alert          ║")
    print("║   Global Authoritative Sourcing              ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"  Run started:  {ts.strftime('%Y-%m-%d %I:%M:%S %p IST')}")
    print(f"  Run token:    {run_token}")
    print(f"  Target slot:  {nearest_slot_label(ts)}")
    print()

    # Verify all required secrets are present before doing any work
    _check_credentials()

    if should_skip_this_run():
        return

    # ── Fetch all domains ──
    all_results = []
    for idx, topic in enumerate(TOPICS):
        full_label = f"{topic['emoji']} {topic['label']}"
        print(f"  📡 [{idx+1}/{len(TOPICS)}] {topic['label']}")
        articles = fetch_topic(topic, run_token)
        all_results.append((full_label, articles))
        with_url = sum(1 for a in articles if is_valid_url(a.get("url", "")))
        status = "✅" if articles else "⚠️ "
        print(f"     {status} {len(articles)} article(s), {with_url} with valid URL")
        for a in articles[:3]:
            url = a.get("url", "")
            mark = "🔗" if is_valid_url(url) else "❌"
            src = a.get("source", "?")
            print(f"        {mark} [{src[:18]:<18}] {a.get('title','')[:50]}")
        if len(articles) > 3:
            print(f"        ... and {len(articles)-3} more")
        if idx < len(TOPICS) - 1:
            print(f"     ⏸  pausing {DELAY_BETWEEN_DOMAINS}s before next domain...")
            time.sleep(DELAY_BETWEEN_DOMAINS)

    # ── Dedupe across domains ──
    print()
    print("  🧹 Deduplicating cross-domain titles...")
    before = sum(len(a) for _, a in all_results)
    all_results = deduplicate_across_domains(all_results)
    after = sum(len(a) for _, a in all_results)
    print(f"     Removed {before - after} duplicate(s)")

    # ── Build source summary ──
    source_summary = {}
    for _, articles in all_results:
        for a in articles:
            s = a.get("source", "Unknown")
            source_summary[s] = source_summary.get(s, 0) + 1

    elapsed = f"{int(time.time() - start)}s"

    # ── Save data ──
    print()
    save_articles_for_website(all_results, {"elapsed": elapsed}, source_summary)

    # ── Print source diversity report ──
    print()
    print(f"  📚 SOURCE DIVERSITY REPORT — {len(source_summary)} unique sources contributed:")
    sorted_sources = sorted(source_summary.items(), key=lambda x: -x[1])
    for src, count in sorted_sources[:15]:
        bar = "█" * count
        print(f"     {src[:30]:<30} {bar} {count}")
    if len(sorted_sources) > 15:
        rest = sum(c for _, c in sorted_sources[15:])
        print(f"     ... and {len(sorted_sources) - 15} more sources ({rest} articles)")

    # ── Build & send Telegram ──
    print()
    print("  📨 Building Telegram message(s)...")
    messages = build_compact_message(all_results, {"elapsed": elapsed})
    print(f"     {len(messages)} message(s)")

    print(f"\n  📤 Sending to Telegram...")
    delivered = 0
    for i, msg in enumerate(messages, 1):
        ok, err = send_telegram(msg)
        if ok:
            note = f" ({err})" if err else ""
            print(f"     ✅ {i}/{len(messages)}{note}  [{len(msg)} chars]")
            delivered += 1
        else:
            print(f"     ❌ {i}/{len(messages)} — {err}")
        time.sleep(0.5)

    if delivered > 0:
        mark_run_complete()

    # ── Summary ──
    successful = sum(1 for _, a in all_results if a)
    total_articles = sum(len(a) for _, a in all_results)
    total_links = sum(1 for _, ar in all_results for a in ar if is_valid_url(a.get("url", "")))

    print()
    print("  ┌─────────────────────────────────────────┐")
    print("  │  RUN SUMMARY                            │")
    print("  ├─────────────────────────────────────────┤")
    print(f"  │  Domains scanned:    {len(TOPICS):2d}                 │")
    print(f"  │  Domains successful: {successful:2d}                 │")
    print(f"  │  Articles found:     {total_articles:2d}                 │")
    print(f"  │  Articles w/ links:  {total_links:2d}                 │")
    print(f"  │  Unique sources:     {len(source_summary):2d}                 │")
    print(f"  │  Messages sent:      {delivered:2d}/{len(messages):<2d}              │")
    print(f"  │  Total runtime:      {elapsed:<7}            │")
    print("  └─────────────────────────────────────────┘")
    print()

    if delivered == 0:
        raise SystemExit("No messages delivered.")


if __name__ == "__main__":
    main()
