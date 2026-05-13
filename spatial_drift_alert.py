"""
╔═══════════════════════════════════════════════════════╗
║         SPATIAL DRIFT — Daily Intelligence Alert       ║
║         Explore. Analyze. Anticipate.                  ║
║         v5.0 FINAL — All-Domain Coverage Fix           ║
╚═══════════════════════════════════════════════════════╝

PROBLEMS FIXED IN THIS VERSION
─────────────────────────────────────────────────────────
1. ONLY REMOTE SENSING SHOWED ARTICLES (8 other domains empty)
   ROOT CAUSE: The web_search tool has per-conversation rate limits.
   When 9 domains share one HTTP session in quick succession, the
   later calls get throttled and return empty.
   FIX: Larger delays between calls (8s), explicit retry on empty
   results, and a fallback that uses only the model's knowledge
   (no web_search) so a domain never returns zero articles.

2. SCIENCE DAILY NOW USED AS PRIMARY SOURCE
   Each domain has dedicated Science Daily category URLs that the
   model is told to search by name first.

3. NODE.JS 20 DEPRECATION
   Workflow updated to actions/checkout@v5 and setup-python@v6.

4. WRONG DELIVERY TIMES
   GitHub cron is "best effort" and lags 5-30 minutes under load.
   We now schedule extra cron triggers near each target time AND
   add a deduplication file so the same time slot only sends once.
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
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "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")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8755526579:AAEIYLkfrmFV5Byprb-uyGeXzUIDaHsqk_s")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "1739337359")

# ── Configuration ──────────────────────────────────────────────────────────────
MAX_ARTICLES_PER_TOPIC = 6
MAX_RETRIES_PER_TOPIC  = 3
MAX_TOKENS             = 4096
TELEGRAM_MSG_LIMIT     = 4000
DELAY_BETWEEN_DOMAINS  = 8   # seconds — critical to avoid rate limits

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
LAST_RUN_FILE = DATA_DIR / "last_run.txt"  # deduplication marker

# IST timezone
IST = timezone(timedelta(hours=5, minutes=30))

# Target IST slots (hour, minute) — used for "this run is closest to which slot"
TARGET_SLOTS = [(8, 0), (12, 0), (16, 0), (22, 0)]


# ── Topic Definitions ──────────────────────────────────────────────────────────
TOPICS = [
    {
        "emoji": "🛰️",
        "label": "Remote Sensing & Earth Observation",
        "query": "latest remote sensing satellite imagery earth observation news 2026",
        "science_daily_urls": [
            "https://www.sciencedaily.com/news/space_time/satellites/",
            "https://www.sciencedaily.com/news/earth_climate/geography/",
        ],
    },
    {
        "emoji": "🗺️",
        "label": "GIS & Geospatial Technology",
        "query": "latest GIS geospatial mapping spatial analysis news 2026",
        "science_daily_urls": [
            "https://www.sciencedaily.com/news/computers_math/computer_modeling/",
            "https://www.sciencedaily.com/news/earth_climate/geography/",
        ],
    },
    {
        "emoji": "🌡️",
        "label": "Climatology & Atmospheric Science",
        "query": "latest climate change atmospheric science weather research 2026",
        "science_daily_urls": [
            "https://www.sciencedaily.com/news/earth_climate/climate/",
            "https://www.sciencedaily.com/news/earth_climate/weather/",
        ],
    },
    {
        "emoji": "🌊",
        "label": "Oceanography & Marine Science",
        "query": "latest oceanography sea level marine science discovery 2026",
        "science_daily_urls": [
            "https://www.sciencedaily.com/news/earth_climate/oceanography/",
            "https://www.sciencedaily.com/news/earth_climate/sea_life/",
        ],
    },
    {
        "emoji": "🏔️",
        "label": "Plate Tectonics & Seismology",
        "query": "latest earthquake seismology plate tectonics fault discovery 2026",
        "science_daily_urls": [
            "https://www.sciencedaily.com/news/earth_climate/earthquakes/",
            "https://www.sciencedaily.com/news/earth_climate/geology/",
        ],
    },
    {
        "emoji": "🌋",
        "label": "Volcanology",
        "query": "latest volcanic eruption volcano monitoring news 2026",
        "science_daily_urls": [
            "https://www.sciencedaily.com/news/earth_climate/volcanoes/",
        ],
    },
    {
        "emoji": "⛏️",
        "label": "Mining & Mineral Resources",
        "query": "latest mining mineral exploration lithium rare earth discovery 2026",
        "science_daily_urls": [
            "https://www.sciencedaily.com/news/matter_energy/mining/",
            "https://www.sciencedaily.com/news/earth_climate/geology/",
        ],
    },
    {
        "emoji": "🪨",
        "label": "Geology & Geomorphology",
        "query": "latest geology geological discovery rock formation news 2026",
        "science_daily_urls": [
            "https://www.sciencedaily.com/news/earth_climate/geology/",
            "https://www.sciencedaily.com/news/fossils_ruins/geochronology/",
        ],
    },
    {
        "emoji": "🚀",
        "label": "Space & Geodesy",
        "query": "latest satellite launch space mission earth observation 2026",
        "science_daily_urls": [
            "https://www.sciencedaily.com/news/space_time/space_exploration/",
            "https://www.sciencedaily.com/news/space_time/satellites/",
        ],
    },
]


# ── System Prompts ─────────────────────────────────────────────────────────────
NEWS_SYSTEM_PROMPT = f"""You are the news research engine for SPATIAL DRIFT.

TASK: Find the most recent and significant real news articles for ONE specific geospatial domain.

PRIORITY SOURCE: Science Daily — always check this first by searching for the domain keyword on their site (e.g., "site:sciencedaily.com volcano" or "Science Daily climate"). They cover all earth-science topics daily.

YOUR STRATEGY:
1. Use web_search to find recent articles from Science Daily, Reuters, Nature, ESA, NASA, etc.
2. Aim for {MAX_ARTICLES_PER_TOPIC} articles. If web_search returns nothing, fall back to articles you know about.
3. Always return at least 2 articles — even if you must use slightly older significant ones from your training data when web search fails.

OUTPUT FORMAT — STRICT:
Return ONLY a valid JSON array. Start with [ and end with ]. Nothing before or after.
No code fences. No markdown. No commentary. No <cite> tags. No HTML.
Plain text only inside JSON strings.

SCHEMA — every article object must have:
{{
  "title": "Plain text headline, max 100 chars",
  "summary": "1-2 sentence summary in plain text",
  "source": "Publication name (Science Daily, Reuters, Nature, ESA, etc.)",
  "date": "Like 'May 2026' or '3 days ago'",
  "url": "https://... — REAL URL from web_search results, or the source's homepage if specific URL unknown",
  "significance": "Plain text, max 120 chars, why geospatial pros care"
}}

CRITICAL:
- Every article must have a URL. If web_search gave you a specific URL, use it.
  Otherwise use the source's main domain (e.g., https://www.sciencedaily.com/news/earth_climate/volcanoes/)
- NEVER return an empty array. Return at least 2 articles.
- Plain text only in all string fields. No <cite>, no HTML.

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
    """Return the IST time slot this run is closest to (for deduplication)."""
    hour = ts.hour + ts.minute / 60
    best = min(TARGET_SLOTS, key=lambda s: abs(s[0] - hour))
    return f"{ts.strftime('%Y-%m-%d')}_{best[0]:02d}00"


# ═══════════════════════════════════════════════════════════════════════════════
# ANTHROPIC API
# ═══════════════════════════════════════════════════════════════════════════════

def call_anthropic(system_prompt: str, user_msg: str, use_search: bool = True,
                   retries: int = MAX_RETRIES_PER_TOPIC) -> dict:
    payload = {
        "model": "claude-sonnet-4-20250514",
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
            # On 429 (rate limit) or 529 (overloaded), wait longer
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
            print(f"        Preview: {candidate[:250]!r}")
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
# FETCH ONE TOPIC — with fallback
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_topic(topic: dict) -> list:
    """Fetch news for one domain. Tries web search first, then falls back to
    model knowledge if web search returns empty (so we never get a blank domain)."""
    label = topic["label"]
    query = topic["query"]
    sd_urls = topic.get("science_daily_urls", [])

    sd_list = "\n".join(f"  - {u}" for u in sd_urls) if sd_urls else "  (none)"

    user_msg_with_search = f"""Find the latest news for this geospatial domain: "{label}"

PRIORITY: Search Science Daily first using queries like "site:sciencedaily.com {label.lower().split('&')[0].strip()}". Also check these category pages by searching for their content:
{sd_list}

Then run additional searches using these keywords: {query}

Return {MAX_ARTICLES_PER_TOPIC} most recent and relevant articles as a JSON array.
EVERY article must have a real URL (from search results) or the source's main URL.
NEVER return an empty array."""

    # ── PASS 1: with web search ────────────────────────────────────────────
    try:
        print(f"        🔍 Pass 1: web search...")
        api_data = call_anthropic(NEWS_SYSTEM_PROMPT, user_msg_with_search, use_search=True)
        raw_text = extract_response_text(api_data)
        articles = extract_json_array(raw_text) if raw_text else []
        search_urls = extract_search_urls(api_data)
    except Exception as e:
        print(f"        ⚠️  Pass 1 failed: {e}")
        articles = []
        search_urls = []

    # ── PASS 2: knowledge-only fallback if nothing came back ───────────────
    if not articles:
        print(f"        🔍 Pass 2: knowledge fallback (no web search)...")
        try:
            user_msg_kb = f"""I need news for the geospatial domain: "{label}"

Based on your training knowledge, list {MAX_ARTICLES_PER_TOPIC} significant articles, papers,
or developments from 2025-2026 in this field. Topics: {query}

For each, provide a URL pointing to the publishing source's main page if you don't know
the exact article URL. For Science Daily articles, use: {sd_urls[0] if sd_urls else 'https://www.sciencedaily.com/news/earth_climate/'}

Return a JSON array following the exact schema. NEVER return empty."""

            api_data = call_anthropic(NEWS_SYSTEM_PROMPT, user_msg_kb, use_search=False, retries=1)
            raw_text = extract_response_text(api_data)
            articles = extract_json_array(raw_text) if raw_text else []
        except Exception as e:
            print(f"        ❌ Pass 2 also failed: {e}")
            articles = []

    if not articles:
        return []

    # Clean & validate every article
    cleaned = []
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
        # Backfill URL from search if missing
        if not is_valid_url(obj["url"]) and search_urls:
            obj["url"] = best_url_match(obj["title"], search_urls) or ""
        # Final fallback: Science Daily category page
        if not is_valid_url(obj["url"]) and sd_urls:
            obj["url"] = sd_urls[0]
        if obj["title"]:
            cleaned.append(obj)
    return cleaned


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
        f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Tap any title to open the article.</i>\n"
        f"<i>Next brief in ~4 hours.</i>"
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

def save_articles_for_website(all_results: list, run_meta: dict):
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
            "elapsed":          run_meta.get("elapsed", "—"),
        },
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
# DEDUPLICATION (prevents the same slot from sending twice)
# ═══════════════════════════════════════════════════════════════════════════════

def should_skip_this_run() -> bool:
    """Check if this slot already ran in the last 2 hours."""
    ts = now_ist()
    slot = nearest_slot_label(ts)
    if LAST_RUN_FILE.exists():
        try:
            last_slot = LAST_RUN_FILE.read_text().strip()
            if last_slot == slot:
                print(f"  ⏭️  Slot {slot} already processed. Skipping duplicate run.")
                return True
        except Exception:
            pass
    return False


def mark_run_complete():
    """Write the current slot to the dedup file."""
    ts = now_ist()
    slot = nearest_slot_label(ts)
    try:
        LAST_RUN_FILE.write_text(slot)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    start = time.time()
    ts = now_ist()
    print()
    print("╔══════════════════════════════════════════════╗")
    print("║   SPATIAL DRIFT v5.0 FINAL — Daily Alert     ║")
    print("║   All-Domain Coverage Edition                ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"  Run started: {ts.strftime('%Y-%m-%d %I:%M:%S %p IST')}")
    print(f"  Target slot: {nearest_slot_label(ts)}")
    print()

    # ── Deduplication check ────────────────────────────────────────────────
    if should_skip_this_run():
        print("  This run was triggered for a slot that already fired.")
        print("  Exiting cleanly to avoid duplicate Telegram messages.")
        return

    # ── Fetch all domains ──────────────────────────────────────────────────
    all_results = []
    for idx, topic in enumerate(TOPICS):
        full_label = f"{topic['emoji']} {topic['label']}"
        print(f"  📡 [{idx+1}/{len(TOPICS)}] {topic['label']}")
        articles = fetch_topic(topic)
        all_results.append((full_label, articles))
        with_url = sum(1 for a in articles if is_valid_url(a.get("url", "")))
        status = "✅" if articles else "⚠️ "
        print(f"     {status} {len(articles)} article(s), {with_url} with valid URL")
        for a in articles[:3]:
            url = a.get("url", "")
            mark = "🔗" if is_valid_url(url) else "❌"
            print(f"        {mark} {a.get('title','')[:60]}")
        if len(articles) > 3:
            print(f"        ... and {len(articles)-3} more")

        # CRITICAL: delay between domains to avoid rate limits
        if idx < len(TOPICS) - 1:
            print(f"     ⏸  pausing {DELAY_BETWEEN_DOMAINS}s before next domain...")
            time.sleep(DELAY_BETWEEN_DOMAINS)

    elapsed = f"{int(time.time() - start)}s"

    # ── Save data for website ──────────────────────────────────────────────
    print()
    save_articles_for_website(all_results, {"elapsed": elapsed})

    # ── Build & send Telegram messages ─────────────────────────────────────
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

    # ── Mark this slot complete (dedup) ────────────────────────────────────
    if delivered > 0:
        mark_run_complete()

    # ── Summary ────────────────────────────────────────────────────────────
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
    print(f"  │  Messages sent:      {delivered:2d}/{len(messages):<2d}              │")
    print(f"  │  Total runtime:      {elapsed:<7}            │")
    print("  └─────────────────────────────────────────┘")
    print()

    if delivered == 0:
        raise SystemExit("No messages delivered.")


if __name__ == "__main__":
    main()
