"""
╔═══════════════════════════════════════════════════════╗
║        SPATIAL DRIFT — Daily Intelligence Alert        ║
║        Explore. Analyze. Anticipate.                   ║
║        v6.3 — Global Authoritative Sourcing            ║
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

# ── Source name normalisation — maps common variants to canonical names ─────
# Prevents the 2-per-source cap from being bypassed by minor name differences
SOURCE_ALIASES = {
    "reuters science": "Reuters",
    "reuters health": "Reuters",
    "bbc news science": "BBC Science",
    "bbc news": "BBC Science",
    "associated press": "AP News",
    "ap": "AP News",
    "phys org": "Phys.org",
    "science daily": "ScienceDaily",
    "agu eos": "Eos (AGU)",
    "eos agu": "Eos (AGU)",
    "nasa earth science": "NASA",
    "nasa climate": "NASA",
    "nasa earthdata": "NASA",
    "usgs earthquake hazards": "USGS",
    "usgs mineral resources": "USGS",
    "usgs volcano hazards": "USGS",
    "usgs volcano hazards program": "USGS",
    "smithsonian global volcanism program": "Smithsonian GVP",
    "smithsonian gvp": "Smithsonian GVP",
    "smithsonian ocean": "Smithsonian",
    "nature geoscience": "Nature Geoscience",
    "nature climate change": "Nature Climate Change",
    "nature astronomy": "Nature Astronomy",
    "ieee tgrs": "IEEE Transactions on Geoscience and Remote Sensing",
    "esa copernicus": "Copernicus",
    "copernicus climate change service": "Copernicus",
    "isro india": "ISRO",
    "times of india science": "Times of India",
    "the hindu science": "The Hindu",
    "hindustan times tech": "Hindustan Times",
    "down to earth india": "Down to Earth",
    "le monde science": "Le Monde",
    "süddeutsche zeitung": "Süddeutsche Zeitung",
    # Russia
    "ria novosti": "RIA Novosti Science",
    "tass": "TASS Science",
    "роскосмос": "Roscosmos",
    # China
    "xinhua": "Xinhua Science",
    "xinhua news agency": "Xinhua Science",
    "china national space administration": "CNSA (China National Space Administration)",
    "chinese academy of sciences": "Chinese Academy of Sciences",
    # Japan
    "japan aerospace exploration agency": "JAXA",
    "gsi japan": "Geospatial Information Authority of Japan (GSI)",
    "geological survey japan": "Geological Survey of Japan (GSJ)",
    "nhk world": "NHK World Science",
    "japan times": "Japan Times Science",
    # Korea
    "kari": "KARI (Korea Aerospace Research Institute)",
    "ngii": "NGII (National Geographic Information Institute Korea)",
    "kigam": "KIGAM (Korea Institute of Geoscience and Mineral Resources)",
    "yonhap": "Yonhap News Science",
    # Australia / NZ
    "csiro": "CSIRO",
    "bom australia": "Bureau of Meteorology Australia",
    "bureau of meteorology": "Bureau of Meteorology Australia",
    "linz": "LINZ (Land Information New Zealand)",
    "niwa": "NIWA (NZ)",
    # South America
    "inpe": "INPE Brazil",
    "conae argentina": "CONAE (Argentina)",
    "igm chile": "IGM Chile",
    "agência brasil": "Agência Brasil",
    # Islands / Pacific
    "spc": "Pacific Community (SPC)",
    "sopac": "Pacific Islands Applied Geoscience Commission (SOPAC)",
    "sprep": "Secretariat of the Pacific Regional Environment Programme (SPREP)",
    # Disaster & hazard agencies
    "undrr": "UNDRR (UN Office for Disaster Risk Reduction)",
    "gdacs": "GDACS (Global Disaster Alert and Coordination System)",
    "ifrc": "IFRC (International Federation of Red Cross)",
    "red cross": "IFRC (International Federation of Red Cross)",
    "iaea": "IAEA (radiation/nuclear incidents)",
    "ocha": "OCHA (UN Office for the Coordination of Humanitarian Affairs)",
    "ndma": "NDMA India (National Disaster Management Authority)",
    "ndma india": "NDMA India (National Disaster Management Authority)",
    "fema": "FEMA (US Federal Emergency Management Agency)",
    "bnpb": "Indonesian BNPB",
    "bnpb indonesia": "Indonesian BNPB",
    "phivolcs": "Philippine PHIVOLCS",
    "nhc": "NOAA National Hurricane Center",
    "national hurricane center": "NOAA National Hurricane Center",
    "copernicus ems": "Copernicus Emergency Management Service",
    "defesa civil": "Defesa Civil Brasil",
}

def normalise_source(name: str) -> str:
    """Return canonical source name; preserves original if no alias found."""
    if not name:
        return name
    key = name.strip().lower()
    return SOURCE_ALIASES.get(key, name.strip())


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


# ── FIX 1: Model deprecation early-warning ─────────────────────────────────

def _check_model_valid():
    """Send a minimal 1-token call to confirm the model name is still active.
    Catches HTTP 404 'model not found' BEFORE wasting the full 9-domain loop.
    Prints a clear action message and exits if the model is retired."""
    print(f"  🔍 Validating model '{MODEL_NAME}'...")
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL_NAME,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            },
            timeout=20,
        )
        if r.status_code == 404:
            print()
            print(f"  ❌ MODEL RETIRED: '{MODEL_NAME}' returned HTTP 404.")
            print(f"     Anthropic has deprecated this model snapshot.")
            print()
            print(f"  ACTION REQUIRED:")
            print(f"     1. Visit https://docs.anthropic.com/en/api/models")
            print(f"     2. Find the current claude-sonnet model string")
            print(f"     3. Update MODEL_NAME in spatial_drift_alert.py")
            print(f"     4. Update model in docs/index.html (Content Studio)")
            print(f"     5. Update model in daily-alert.yml (validation curl)")
            print()
            raise SystemExit(f"Model '{MODEL_NAME}' is retired. Update MODEL_NAME and redeploy.")
        elif r.status_code == 401:
            print()
            print(f"  ❌ API KEY REJECTED (HTTP 401). Key may be revoked or credit balance is $0.")
            print(f"     Visit https://console.anthropic.com → API Keys and Billing.")
            raise SystemExit("Invalid API key. Check console.anthropic.com.")
        elif r.status_code == 200:
            print(f"  ✅ Model '{MODEL_NAME}' confirmed active.")
        else:
            print(f"  ⚠️  Unexpected HTTP {r.status_code} from model check — proceeding cautiously.")
    except SystemExit:
        raise
    except Exception as e:
        print(f"  ⚠️  Model pre-check failed ({e}) — proceeding anyway.")


# ── FIX 2: URL validation via HTTP HEAD check ──────────────────────────────

def validate_urls(articles: list) -> list:
    """Fire HEAD requests on each article URL. Mark broken URLs as empty
    so the website and Telegram don't show dead links. Runs after fetch_topic."""
    if not articles:
        return articles
    checked = []
    for a in articles:
        url = a.get("url", "")
        if not is_valid_url(url):
            checked.append(a)
            continue
        try:
            resp = requests.head(
                url,
                timeout=URL_CHECK_TIMEOUT,
                allow_redirects=True,
                headers={"User-Agent": "SpatialDrift/1.0 (+https://senthil4ocean.github.io/spatial-drift/)"},
            )
            if resp.status_code in (404, 410):
                print(f"        🔗 URL dead ({resp.status_code}), cleared: {url[:60]}")
                a = {**a, "url": ""}
            # 200, 301, 302, 403 (paywalled), 429 (rate-limited) are all fine — URL exists
        except Exception:
            # Network timeout or connection error — keep URL as-is rather than wrongly clearing
            pass
        checked.append(a)
    return checked

# ── Configuration ──────────────────────────────────────────────────────────────
MAX_ARTICLES_PER_TOPIC = 5    # fortnightly edition — slightly richer per domain
MAX_RETRIES_PER_TOPIC  = 1    # was 3 — cuts wasted retries on failed domains
MAX_TOKENS             = 2200 # was 4096 — Haiku needs less headroom
TELEGRAM_MSG_LIMIT     = 4000
DELAY_BETWEEN_DOMAINS  = 6    # was 8 — fewer domains now, can move a bit faster
RECENCY_WINDOW_DAYS    = 14   # fortnightly run — pull past 2 weeks' news
MAX_PER_SOURCE         = 2    # source-diversity cap
URL_CHECK_TIMEOUT      = 6    # seconds for HTTP head check per URL
MODEL_NAME             = "claude-haiku-4-5-20251001"  # cost-optimised for news aggregation

# Output paths
ROOT_DIR    = Path(__file__).parent
DATA_DIR    = ROOT_DIR / "data"
DOCS_DIR    = ROOT_DIR / "docs"
ARCHIVE_DIR = DATA_DIR / "archive"   # Fix 3: fortnightly historical snapshots
DATA_DIR.mkdir(exist_ok=True)
DOCS_DIR.mkdir(exist_ok=True)
ARCHIVE_DIR.mkdir(exist_ok=True)
ARTICLES_FILES = [
    DATA_DIR / "articles.json",
    DOCS_DIR / "articles.json",
]
LAST_RUN_FILE = DATA_DIR / "last_run.txt"

IST = timezone(timedelta(hours=5, minutes=30))
TARGET_SLOTS = [(7, 23)]  # fortnightly Saturday run at 7:23 AM IST


# ═══════════════════════════════════════════════════════════════════════════════
# TOPIC DEFINITIONS — broad global authoritative source pool per domain
# ═══════════════════════════════════════════════════════════════════════════════

TOPICS = [
    # ── DOMAIN 1: MERGED (Remote Sensing + GIS) ──────────────────────────────────
    {
        "emoji": "🛰️",
        "label": "Remote Sensing & GIS",
        "keywords": (
            "satellite imagery, earth observation, LiDAR, SAR, hyperspectral, "
            "multispectral, Sentinel, Landsat, Planet Labs, Maxar, ICEYE, optical sensing, "
            "radar interferometry, InSAR, change detection, image classification, "
            "GIS, geospatial AI, digital twin, spatial analysis, ArcGIS, QGIS, "
            "3D city model, OpenStreetMap, geocoding, location intelligence, "
            "geospatial cloud, spatial data infrastructure, web mapping"
        ),
        "trusted_sources": [
            "Nature", "Science", "Remote Sensing of Environment",
            "IEEE Transactions on Geoscience and Remote Sensing", "MDPI Remote Sensing",
            "ISPRS Journal of Photogrammetry and Remote Sensing",
            "International Journal of Geographical Information Science",
            "Cartography and Geographic Information Science", "Transactions in GIS",
            "NASA", "ESA", "ISRO", "JAXA", "USGS", "NOAA", "Copernicus",
            "CNES", "DLR", "KARI (Korea Aerospace Research Institute)",
            "CNSA (China National Space Administration)", "Roscosmos",
            "Planet Labs", "Maxar Technologies", "Airbus Defence and Space",
            "Esri", "Google Maps Platform", "Microsoft Planetary Computer",
            "OpenStreetMap Foundation", "QGIS", "OGC (Open Geospatial Consortium)",
            "SpaceNews", "Geospatial World", "GIM International", "Directions Magazine",
            "Eos (AGU)", "MIT Technology Review", "TechCrunch", "IEEE Spectrum",
            "Reuters", "BBC Science", "Phys.org", "ScienceDaily",
        ],
    },
    # ── DOMAIN 2 ────────────────────────────────────────────────────────────────
    {
        "emoji": "🌡️",
        "label": "Climatology & Atmospheric Science",
        "keywords": (
            "climate change, global warming, atmospheric science, IPCC, methane, "
            "CO2, heatwave, sea ice, cyclone, ENSO, jet stream, climate model, "
            "aerosol, ozone, precipitation extremes, climate attribution"
        ),
        "trusted_sources": [
            "Nature Climate Change", "Science", "Nature Geoscience", "PNAS",
            "Geophysical Research Letters", "Atmospheric Chemistry and Physics",
            "Journal of Climate", "Climate Dynamics",
            "IPCC", "WMO", "NOAA", "NASA Climate", "ECMWF",
            "UK Met Office", "Copernicus Climate Change Service",
            "Bureau of Meteorology Australia", "Japan Meteorological Agency",
            "China Meteorological Administration",
            "Reuters", "BBC Science", "AP News", "Phys.org", "ScienceDaily",
            "Carbon Brief", "Inside Climate News",
        ],
    },
    # ── DOMAIN 3 ────────────────────────────────────────────────────────────────
    {
        "emoji": "🌊",
        "label": "Oceanography & Marine Science",
        "keywords": (
            "oceanography, sea level rise, ocean temperature, marine ecosystems, "
            "coral reef, ocean currents, deep sea, salinity, AMOC, thermohaline, "
            "ocean acidification, Pacific, Indian Ocean, Arctic Ocean"
        ),
        "trusted_sources": [
            "Nature", "Nature Geoscience", "Science",
            "Journal of Geophysical Research: Oceans", "Ocean Science",
            "Limnology and Oceanography", "Marine Geology",
            "NOAA", "NASA Earth Science", "Scripps Institution of Oceanography",
            "Woods Hole Oceanographic Institution", "WMO",
            "JAMSTEC (Japan Agency for Marine-Earth Science and Technology)",
            "NIWA (NZ)", "CSIRO Australia",
            "Reuters", "BBC Science", "AP News", "Phys.org",
            "Eos (AGU)", "ScienceDaily", "Smithsonian Ocean",
        ],
    },
    # ── DOMAIN 4 ────────────────────────────────────────────────────────────────
    {
        "emoji": "🏔️",
        "label": "Plate Tectonics & Seismology",
        "keywords": (
            "earthquake, seismology, plate tectonics, fault, subduction, mantle, "
            "GPS geodesy, seismic activity, tsunami warning, crustal deformation"
        ),
        "trusted_sources": [
            "Nature Geoscience", "Science", "Geophysical Research Letters",
            "Seismological Research Letters", "Earth and Planetary Science Letters",
            "Journal of Geophysical Research: Solid Earth",
            "USGS Earthquake Hazards", "EMSC", "IRIS", "GFZ Potsdam",
            "GNS Science (NZ)", "Geoscience Australia",
            "JMA (Japan Met Agency)", "China Earthquake Networks Center",
            "KIGAM (Korea Institute of Geoscience and Mineral Resources)",
            "Reuters", "BBC Science", "AP News", "Phys.org", "ScienceDaily",
            "Eos (AGU)",
        ],
    },
    # ── DOMAIN 5 ────────────────────────────────────────────────────────────────
    {
        "emoji": "🌋",
        "label": "Volcanology",
        "keywords": (
            "volcanic eruption, volcano monitoring, lava flow, magma, ash plume, "
            "pyroclastic, volcanic gas, caldera, Smithsonian GVP, "
            "ring of fire, volcanic hazard"
        ),
        "trusted_sources": [
            "Nature Geoscience", "Science", "Journal of Volcanology and Geothermal Research",
            "Bulletin of Volcanology",
            "USGS Volcano Hazards Program", "Smithsonian Global Volcanism Program",
            "INGV (Italy)", "Icelandic Met Office", "VolcanoDiscovery",
            "JMA (Japan Met Agency)", "Indonesian PVMBG", "Philippine PHIVOLCS",
            "GNS Science (NZ)", "Geoscience Australia",
            "Reuters", "BBC Science", "AP News", "Phys.org", "ScienceDaily",
            "Eos (AGU)",
        ],
    },
    # ── DOMAIN 6: MERGED (Mining + Geology) ──────────────────────────────────────
    {
        "emoji": "⛏️",
        "label": "Mining & Geology",
        "keywords": (
            "mining, mineral exploration, critical minerals, lithium, cobalt, "
            "rare earth elements, copper, nickel, uranium, sustainable mining, "
            "deep sea mining, battery metals, mineral mapping, "
            "geology, geological discovery, rock formation, stratigraphy, "
            "paleoclimate, sedimentology, mineralogy, geochronology, geomorphology, "
            "landslide, soil erosion, river geomorphology"
        ),
        "trusted_sources": [
            "Mining.com", "Mining Magazine", "Mining Journal", "Mining Weekly",
            "Reuters Mining", "Bloomberg Metals & Mining", "S&P Global Market Intelligence",
            "USGS Mineral Resources", "BGS (British Geological Survey)",
            "Geological Survey of Canada", "Geoscience Australia",
            "KIGAM (Korea Institute of Geoscience and Mineral Resources)",
            "Geological Survey of Japan (GSJ)", "China Geological Survey",
            "Nature Geoscience", "Economic Geology", "Ore Geology Reviews",
            "Geology (GSA)", "Earth and Planetary Science Letters",
            "Geological Society of America Bulletin",
            "Quaternary Science Reviews", "Journal of Sedimentary Research",
            "Financial Times Mining", "Wall Street Journal",
            "Reuters", "BBC Science", "Smithsonian", "National Geographic",
            "Phys.org", "ScienceDaily", "Eos (AGU)",
        ],
    },
    # ── DOMAIN 7 ────────────────────────────────────────────────────────────────
    {
        "emoji": "🚀",
        "label": "Space & Geodesy",
        "keywords": (
            "satellite launch, space mission, earth observation satellite, geodesy, "
            "GNSS, GPS, reference frame, ITRF, GRACE, lunar mission, Mars mission, "
            "satellite constellation, commercial space"
        ),
        "trusted_sources": [
            "SpaceNews", "Spaceflight Now", "Ars Technica Space", "The Space Review",
            "NASA", "ESA", "ISRO", "JAXA", "CNES", "DLR", "CSA", "Roscosmos",
            "CNSA (China National Space Administration)",
            "KARI (Korea Aerospace Research Institute)",
            "SpaceX", "Blue Origin", "Rocket Lab",
            "Reuters", "BBC Science", "AP News", "Nature Astronomy",
            "Sky and Telescope", "Phys.org", "ScienceDaily",
        ],
    },
    # ── DOMAIN 8 ────────────────────────────────────────────────────────────────
    {
        "emoji": "🧊",
        "label": "Cryosphere & Polar Science",
        "keywords": (
            "Arctic, Antarctic, ice sheet, permafrost, glacier retreat, sea ice extent, "
            "polar science, ice core, cryosphere, Greenland ice, "
            "polar expedition, frozen ground thaw"
        ),
        "trusted_sources": [
            "Nature Geoscience", "Nature Climate Change", "Science",
            "The Cryosphere", "Journal of Glaciology",
            "Geophysical Research Letters", "Polar Research",
            "NASA Cryosphere", "NSIDC (National Snow and Ice Data Center)",
            "NCAR", "Alfred Wegener Institute", "Norwegian Polar Institute",
            "British Antarctic Survey", "Scott Polar Research Institute",
            "Reuters", "BBC Science", "AP News", "Phys.org",
            "ScienceDaily", "Eos (AGU)",
        ],
    },
    # ── DOMAIN 9: NEW — DISASTER & HAZARD MONITORING ─────────────────────────────
    {
        "emoji": "🆘",
        "label": "Disaster & Hazard Monitoring",
        "keywords": (
            "landslide, flood, drought, cyclone, hurricane, typhoon, storm surge, "
            "earthquake disaster, tsunami, wildfire, forest fire, "
            "radiation leak, nuclear accident, industrial disaster, chemical spill, "
            "dam failure, mudslide, avalanche, disaster response, emergency management, "
            "humanitarian crisis, disaster risk reduction, early warning system, "
            # Non-English disaster terms — native scripts for broader coverage
            "inondation catastrophe, terremoto desastre, überschwemmung katastrophe, "
            "洪水 灾害, 地震 災害, 홍수 재난, наводнение бедствие, "
            "बाढ़ आपदा, चक्रवात आपदा"
        ),
        "trusted_sources": [
            # Disaster-specific / multilateral bodies
            "UNDRR (UN Office for Disaster Risk Reduction)", "ReliefWeb",
            "GDACS (Global Disaster Alert and Coordination System)",
            "IFRC (International Federation of Red Cross)",
            "WHO Health Emergencies", "IAEA (radiation/nuclear incidents)",
            "OCHA (UN Office for the Coordination of Humanitarian Affairs)",
            # National disaster management agencies
            "NDMA India (National Disaster Management Authority)",
            "FEMA (US Federal Emergency Management Agency)",
            "JMA (Japan Meteorological Agency)",
            "China Earthquake Administration", "NEMA (Nigeria)",
            "Philippine PHIVOLCS", "Indonesian BNPB",
            "Emergency Management Australia", "NEMA (New Zealand)",
            "Defesa Civil Brasil",
            # Scientific / monitoring
            "USGS Earthquake Hazards", "EMSC", "GNS Science (NZ)",
            "Copernicus Emergency Management Service", "NASA Disasters Program",
            "NOAA National Hurricane Center",
            # Global + regional news
            "Reuters", "AP News", "BBC Science", "Al Jazeera",
            "Xinhua Science", "RIA Novosti Science", "NHK World Science",
            "Yonhap News Science", "Agência Brasil", "Agencia EFE Science",
            "Down to Earth", "The Hindu",
            "Phys.org", "ScienceDaily",
        ],
        "translate_to_english": True,
        "region_tags": [
            "India", "Japan", "China", "Korea", "Russia",
            "Southeast Asia", "Latin America", "Africa", "Global",
        ],
        "lang_tag": "multi",
    },
    # ── DOMAIN 10: INDIA ─────────────────────────────────────────────────────────
    {
        "emoji": "🇮🇳",
        "label": "India",
        "keywords": (
            "geospatial India, GIS India, ISRO, remote sensing India, "
            "NRSC National Remote Sensing Centre, Survey of India, "
            "SVAMITVA drone mapping, PM Gati Shakti, land records India, "
            "cadastral mapping India, geospatial policy India, "
            "satellite imagery India, coastal mapping India"
        ),
        "trusted_sources": [
            "ISRO", "NRSC (National Remote Sensing Centre)", "Survey of India",
            "Ministry of Science and Technology India",
            "National Informatics Centre India",
            "The Hindu", "Times of India", "Hindustan Times",
            "Down to Earth", "The Wire Science", "India Today Science",
            "Economic Times Tech", "Business Standard Tech", "Livemint",
            "Geospatial World", "GIM International",
            "GeoIntelligence India", "Geospatial Media India",
            "PIB (Press Information Bureau)", "NITI Aayog",
        ],
        "translate_to_english": False,
        "region_tags": ["India"],
        "lang_tag": "en",
    },
    # ── DOMAIN 11: GLOBAL MULTILINGUAL ────────────────────────────────────────────
    {
        "emoji": "🌐",
        "label": "Global Geospatial Intelligence",
        "keywords": (
            "геопространственные данные Россия, Роскосмос, ДЗЗ Россия, "
            "спутниковые снимки, российская картография, "
            "遥感 中国, 地理信息系统 中国, 北斗导航, 高分系列卫星, 自然资源部, "
            "地理空間情報 日本, JAXA 衛星, 国土地理院, リモートセンシング 日本, "
            "지리정보 한국, KOMPSAT, 국토정보공사, 원격탐사 한국, "
            "geospatial Australia, Geoscience Australia, LINZ New Zealand, "
            "Pacific islands mapping, Pacific geospatial, Great Barrier Reef mapping, "
            "ocean territory Australia, remote sensing Oceania, "
            "sensoriamento remoto América do Sul, INPE Brasil, Embrapa geoespacial, "
            "geografía CONAE Argentina, IGM Chile, cartografia Venezuela, "
            "geomática Colombia, teledetección Perú, "
            "small island developing states SIDS geospatial, "
            "Caribbean mapping, Pacific island remote sensing, "
            "island coastal erosion mapping, atoll sea level rise"
        ),
        "trusted_sources": [
            "Roscosmos", "Роскосмос (Roscosmos)", "SCANEX (Russia)",
            "RIA Novosti Science", "TASS Science",
            "CNSA (China National Space Administration)",
            "Chinese Academy of Sciences", "Ministry of Natural Resources China",
            "Xinhua Science", "China Daily Science", "Journal of Remote Sensing China",
            "National Geomatics Center of China",
            "JAXA", "Geospatial Information Authority of Japan (GSI)",
            "Geological Survey of Japan (GSJ)", "JAMSTEC",
            "NHK World Science", "Japan Times Science",
            "KARI (Korea Aerospace Research Institute)",
            "NGII (National Geographic Information Institute Korea)",
            "KIGAM (Korea Institute of Geoscience and Mineral Resources)",
            "Yonhap News Science",
            "Geoscience Australia", "CSIRO", "Bureau of Meteorology Australia",
            "LINZ (Land Information New Zealand)", "NIWA (NZ)",
            "Pacific Community (SPC)", "The Conversation Australia",
            "INPE Brazil", "Embrapa Territorial",
            "CONAE (Argentina)", "IGM Chile", "Agencia EFE Science",
            "Agência Brasil", "El País Ciencia",
            "Pacific Islands Applied Geoscience Commission (SOPAC)",
            "Caribbean Community Climate Change Centre",
            "Secretariat of the Pacific Regional Environment Programme (SPREP)",
            "Nature Geoscience", "Reuters", "AP News",
        ],
        "translate_to_english": True,
        "region_tags": [
            "Russia", "China", "Japan", "Korea",
            "Australia", "New Zealand", "Pacific Islands",
            "South America", "Caribbean", "Island Nations",
        ],
        "lang_tag": "multi",
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
6. LANGUAGE: If you find articles in French, German, Spanish, Portuguese, or any other language,
   translate the title, summary, and significance fields into clear English. Keep the original
   source name and URL. Add a "lang" field with the original language code (e.g. "fr", "de", "es").

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
  "title": "Plain text headline in English, max 100 chars",
  "summary": "1-2 sentence summary in English",
  "source": "Real publication name (e.g., 'Nature', 'Reuters', 'ISRO', 'Le Monde')",
  "date": "Specific recent date like '8 May 2026' or '3 days ago'",
  "url": "https://... — REAL URL from web_search results",
  "significance": "Plain text in English, max 120 chars, why geospatial pros should care",
  "lang": "en"  // original language code — "en", "fr", "de", "es", "pt", "hi" etc.
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
        "model": MODEL_NAME,
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
    label    = topic["label"]
    keywords = topic["keywords"]
    sources  = topic["trusted_sources"]
    translate = topic.get("translate_to_english", False)
    regions   = topic.get("region_tags", [])

    ts = now_ist()
    today_str = ts.strftime("%d %B %Y")
    sources_str = ", ".join(sources[:20])

    # Extra instruction for multi-language domains
    lang_note = ""
    if translate:
        regions_str = ', '.join(regions) if regions else 'Global'
        lang_note = (
            f"\nREGIONS TO COVER: {regions_str}"
            "\nIMPORTANT: Search in local languages too:"
            "\n  Russian: use Cyrillic keywords, search ru-language sites"
            "\n  Chinese: use Chinese characters, search CNSA/CAS/Xinhua"
            "\n  Japanese: use Japanese characters, search JAXA/GSI/NHK"
            "\n  Korean: use Hangul keywords, search KARI/NGII/Yonhap"
            "\n  Spanish: search INPE/CONAE/IGM Chile and regional sites"
            "\n  Portuguese: search Agência Brasil, Portuguese universities"
            "\nFor each non-English article found, TRANSLATE title/summary/significance"
            " into clear English. Keep original URL and source name."
            " Set the 'lang' field to the ISO code: ru/zh/ja/ko/es/pt/fr/de/id/ar/other."
            "\nFor Pacific/Island nations: search SOPAC, SPREP, Pacific Community, Caribbean."
            "\nFor Australia/NZ: search Geoscience Australia, CSIRO, LINZ, NIWA."
        )
        if "Disaster" in label:
            lang_note += (
                "\nDISASTER-SPECIFIC: Cover natural hazards (landslides, floods, "
                "droughts, cyclones/hurricanes/typhoons, storm surges, earthquakes, "
                "tsunamis, wildfires) AND man-made disasters (radiation/nuclear "
                "leaks, industrial accidents, chemical spills, dam failures) from "
                "ANY country. Prioritize events with confirmed casualties, "
                "displacement, or infrastructure damage. Search GDACS, ReliefWeb, "
                "and national disaster agencies (NDMA India, FEMA, BNPB Indonesia, "
                "PHIVOLCS Philippines) alongside mainstream news in every language."
            )

    # ── PASS 1: web search ──
    user_msg = f"""Find the latest news articles for this geospatial domain.

DOMAIN: {label}
KEYWORDS: {keywords}
DATE WINDOW: Past {RECENCY_WINDOW_DAYS} days (today is {today_str})
RUN ID: {run_token}  (use this to vary your searches and avoid cached results){lang_note}

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
        print(f"        🔍 Pass 1: web search across {len(sources)} trusted sources{' (multi-language)' if translate else ''}...")
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
            "source":       normalise_source(clean_text(a.get("source", ""))),
            "date":         clean_text(a.get("date", "")),
            "significance": clean_text(a.get("significance", "")),
            "url":          str(a.get("url", "")).strip(),
            "lang":         str(a.get("lang", "en")).strip().lower()[:5],
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

    # ── Fix 2: HTTP HEAD check — remove dead links ──
    cleaned = validate_urls(cleaned)

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
        f"<i>Next brief in 2 weeks — Saturday at 7:23 AM IST.</i>"
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
            "total_articles":    sum(len(a) for _, a in all_results),
            "domains_total":     len(all_results),
            "domains_with_news": sum(1 for _, a in all_results if a),
            "unique_sources":    len(source_summary),
            "elapsed":           run_meta.get("elapsed", "—"),
        },
        "source_summary": source_summary,
        "domains": [
            {"label": label, "count": len(articles), "articles": articles}
            for label, articles in all_results
        ],
    }
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2)

        # Current week files (always overwrite)
        for path in ARTICLES_FILES:
            path.write_text(text, encoding="utf-8")
            print(f"  💾 Wrote {payload['stats']['total_articles']} articles → {path.name}")

        # Fix 3: Weekly archive snapshot — never overwritten
        archive_name = f"articles-{ts.strftime('%Y-%m-%d')}.json"
        archive_path = ARCHIVE_DIR / archive_name
        archive_path.write_text(text, encoding="utf-8")
        print(f"  📦 Archived → data/archive/{archive_name}")

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
    print("║   SPATIAL DRIFT v6.3 — Fortnightly Alert     ║")
    print("║   Global Authoritative Sourcing              ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"  Run started:  {ts.strftime('%Y-%m-%d %I:%M:%S %p IST')}")
    print(f"  Run token:    {run_token}")
    print(f"  Target slot:  {nearest_slot_label(ts)}")
    print()

    # Verify all required secrets are present before doing any work
    _check_credentials()

    # Fix 1: Confirm model is active before running the full 10-domain loop
    _check_model_valid()

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
