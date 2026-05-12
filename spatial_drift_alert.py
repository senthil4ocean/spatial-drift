"""
╔═══════════════════════════════════════════════════════╗
║          SPATIAL DRIFT — Daily Intelligence Alert      ║
║          Explore. Analyze. Anticipate.                 ║
║          v3.0 — Citations, Links & Reliability         ║
╚═══════════════════════════════════════════════════════╝

WHAT'S NEW IN v3.0
─────────────────────────────────────────────────────────
1. Strips <cite> and other HTML tags from API output
   (these were leaking into Telegram as raw text).
2. URLs are now MANDATORY in the prompt + extracted as
   backup from web_search tool result blocks.
3. 5 articles per domain (raised from 2).
4. max_tokens raised to 4096 to prevent JSON truncation.
5. Per-topic messages split automatically if they exceed
   Telegram's 4096-character limit.
6. Detailed per-article URL diagnostics in the logs.
7. Stronger JSON extraction: pre-strips HTML tags before
   parsing, so even citation-wrapped JSON parses cleanly.
"""

import os
import re
import json
import time
import html
import requests
from datetime import datetime
from difflib import SequenceMatcher

# ── Credentials ────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "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")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8755526579:AAEIYLkfrmFV5Byprb-uyGeXzUIDaHsqk_s")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "1739337359")

# ── Configuration ──────────────────────────────────────────────────────────────
NUM_ARTICLES_PER_TOPIC = 5
MAX_RETRIES_PER_TOPIC  = 2
MAX_TOKENS             = 4096
TELEGRAM_MSG_LIMIT     = 4000   # safe margin under 4096

# ── Topic Definitions ──────────────────────────────────────────────────────────
TOPICS = [
    ("🛰️", "Remote Sensing & Earth Observation",
     "remote sensing satellite imagery earth observation LiDAR breakthrough news"),

    ("🗺️", "GIS & Geospatial Technology",
     "GIS geospatial technology mapping spatial analysis news"),

    ("🌡️", "Climatology & Atmospheric Science",
     "climate change extreme weather atmospheric science new study"),

    ("🌊", "Oceanography & Marine Science",
     "oceanography sea level ocean temperature marine science discovery"),

    ("🏔️", "Plate Tectonics & Seismology",
     "earthquake seismology plate tectonics fault discovery"),

    ("🌋", "Volcanology",
     "volcanic eruption volcano monitoring activity news"),

    ("⛏️", "Mining & Mineral Resources",
     "mining mineral exploration lithium rare earth discovery"),

    ("🪨", "Geology & Geomorphology",
     "geology geological discovery rock formation stratigraphy"),

    ("🚀", "Space & Geodesy",
     "satellite launch space mission ESA NASA ISRO earth observation"),
]

# ── System Prompts ─────────────────────────────────────────────────────────────
NEWS_SYSTEM_PROMPT = f"""You are the news research engine for SPATIAL DRIFT.

TASK: Use web_search to find the {NUM_ARTICLES_PER_TOPIC} most recent and significant real news articles for the given geospatial domain.

OUTPUT FORMAT — STRICT:
Return ONLY a valid JSON array. Start with [ and end with ]. Nothing before or after.
Do NOT use code fences, markdown, or commentary.
Do NOT use <cite> tags, HTML tags, <span>, or any markup in any string value.
Plain text only inside JSON strings.

SCHEMA — every article object must have these exact fields:
{{
  "title": "Plain text headline, max 100 chars",
  "summary": "1-2 sentence summary in plain text, no HTML, no citations",
  "source": "Publication name (Reuters, Nature, ESA, etc.)",
  "date": "Like 'May 2026' or '3 days ago'",
  "url": "https://... — REAL URL from your web_search results",
  "significance": "Plain text, max 120 chars, why geospatial pros care"
}}

CRITICAL — URL FIELD:
- The "url" field is MANDATORY for every article.
- Use real URLs you found via web_search. They are visible in your search results.
- URLs must start with http:// or https://
- Never invent URLs. If you cannot verify a URL, exclude that article.

CRITICAL — NO CITATION TAGS:
The web_search tool will mark sources with <cite> tags in your reasoning, but you must
STRIP these from your final JSON output. The summary, title, and significance fields
must contain ONLY plain text. No <cite>, no <span>, no HTML.

Return {NUM_ARTICLES_PER_TOPIC} articles. If you find fewer real ones, return what you found.
Output the JSON array and nothing else."""


TRENDS_SYSTEM_PROMPT = """You are a trend analyst for SPATIAL DRIFT.

Given news from multiple geospatial domains, identify 3 cross-cutting trends.

Return ONLY a JSON array with exactly 3 items:
[
  {"theme": "Short trend name (max 50 chars)", "insight": "One sentence on the cross-domain pattern (max 160 chars)"}
]

Plain text only. No HTML, no <cite> tags, no markdown. Just the raw JSON array."""


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def clean_text(text) -> str:
    """Strip all HTML/citation tags and normalize whitespace."""
    if text is None:
        return ""
    s = str(text)
    # Remove <cite ...>...</cite> with content preserved
    s = re.sub(r"<cite[^>]*>", "", s)
    s = re.sub(r"</cite>", "", s)
    # Remove any other HTML tags (e.g. <span>, <a>, <div>)
    s = re.sub(r"<[^>]+>", "", s)
    # Decode any HTML entities
    s = html.unescape(s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_valid_url(url) -> bool:
    """Check if a value is a valid http(s) URL."""
    if not isinstance(url, str):
        return False
    url = url.strip()
    return url.startswith(("http://", "https://")) and " " not in url and len(url) < 500


def esc_html(text) -> str:
    """Escape for Telegram HTML mode (only < > & need escaping)."""
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


# ═══════════════════════════════════════════════════════════════════════════════
# ANTHROPIC API CALL
# ═══════════════════════════════════════════════════════════════════════════════

def call_anthropic(system_prompt: str, user_msg: str, use_search: bool = True,
                   retries: int = MAX_RETRIES_PER_TOPIC) -> dict:
    """Call Anthropic API. Returns the full JSON response dict on success."""
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
            last_err = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

        if attempt < retries:
            wait = 2 ** attempt
            print(f"        ↻  Retry {attempt+1}/{retries} in {wait}s ({last_err[:80]})")
            time.sleep(wait)

    raise RuntimeError(f"API call failed after {retries+1} attempts. Last error: {last_err}")


def extract_response_text(api_data: dict) -> str:
    """Join all text blocks from a Messages API response."""
    parts = [b.get("text", "") for b in api_data.get("content", []) if b.get("type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def extract_search_urls(api_data: dict) -> list:
    """Extract URL+title pairs from web_search_tool_result blocks as backup."""
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


# ═══════════════════════════════════════════════════════════════════════════════
# JSON EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

def extract_json_array(raw: str) -> list:
    """Bulletproof JSON array extraction — strips HTML tags first, then walks brackets."""
    if not raw:
        return []

    # Pre-clean: strip code fences and HTML tags GLOBALLY before extraction
    cleaned = re.sub(r"```(?:json)?\s*", "", raw)
    cleaned = cleaned.replace("```", "")
    cleaned = re.sub(r"<cite[^>]*>", "", cleaned)
    cleaned = re.sub(r"</cite>", "", cleaned)
    cleaned = cleaned.strip()

    start = cleaned.find("[")
    if start == -1:
        return []

    # Walk to find matching ]
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
        # Last-ditch: try removing any remaining stray tags
        candidate2 = re.sub(r"<[^>]+>", "", candidate)
        try:
            return json.loads(candidate2)
        except json.JSONDecodeError:
            print(f"        ⚠️  JSON parse failed: {e}")
            print(f"        Snippet (first 250 chars): {candidate[:250]!r}")
            return []


# ═══════════════════════════════════════════════════════════════════════════════
# FETCH NEWS FOR ONE TOPIC
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_topic(emoji: str, label: str, query: str) -> list:
    """Fetch news for one topic with retries and URL backfill."""
    user_msg = (
        f"Search the web for the {NUM_ARTICLES_PER_TOPIC} most recent and significant "
        f"news articles about: {query}.\n\n"
        f"Return as a JSON array with the exact schema specified. "
        f"Make sure every article includes a real URL from your search results."
    )

    try:
        api_data = call_anthropic(NEWS_SYSTEM_PROMPT, user_msg, use_search=True)
    except Exception as e:
        print(f"        ❌ API hard failure: {e}")
        return []

    raw_text = extract_response_text(api_data)
    if not raw_text:
        print(f"        ⚠️  Empty text response (stop_reason: {api_data.get('stop_reason')})")
        return []

    articles = extract_json_array(raw_text)
    if not articles:
        print(f"        ⚠️  No JSON array parsed. Raw text preview:")
        print(f"        {raw_text[:300]!r}")
        return []

    # Backup URL pool from web_search tool results
    search_urls = extract_search_urls(api_data)

    # Clean every text field & validate/backfill URLs
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

        # If URL missing or invalid, try to match by title from search results
        if not is_valid_url(obj["url"]) and search_urls:
            obj["url"] = best_url_match(obj["title"], search_urls) or ""

        if obj["title"]:
            cleaned.append(obj)

    return cleaned


def best_url_match(article_title: str, search_urls: list) -> str:
    """Find the best-matching URL from search results by title similarity."""
    if not article_title or not search_urls:
        return ""
    best_score = 0.0
    best_url = ""
    title_lower = article_title.lower()
    for entry in search_urls:
        entry_title = entry.get("title", "").lower()
        if not entry_title:
            continue
        score = SequenceMatcher(None, title_lower, entry_title).ratio()
        if score > best_score:
            best_score = score
            best_url = entry["url"]
    return best_url if best_score >= 0.35 else ""


# ═══════════════════════════════════════════════════════════════════════════════
# TREND ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_trends(all_results: list) -> list:
    """Identify cross-domain trends from collected articles."""
    lines = []
    for label, articles in all_results:
        for a in articles:
            lines.append(f"- [{label}] {a['title']} — {a['summary']}")
    if not lines:
        return []

    user_msg = (
        "Below are recent geospatial news items from multiple domains. "
        "Identify exactly 3 cross-cutting trends connecting them. Return raw JSON only.\n\n"
        + "\n".join(lines[:40])
    )

    try:
        api_data = call_anthropic(TRENDS_SYSTEM_PROMPT, user_msg, use_search=False, retries=1)
        raw = extract_response_text(api_data)
        trends = extract_json_array(raw)
        cleaned = []
        for t in trends:
            if isinstance(t, dict):
                cleaned.append({
                    "theme":   clean_text(t.get("theme", "")),
                    "insight": clean_text(t.get("insight", "")),
                })
        return cleaned
    except Exception as e:
        print(f"  ⚠️  Trends analysis failed: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM MESSAGE BUILDING
# ═══════════════════════════════════════════════════════════════════════════════

def build_topic_message(topic_label: str, articles: list) -> list:
    """Build one or more message strings for a topic. Splits if too long."""
    if not articles:
        return [
            f"<b>{esc_html(topic_label)}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>No fresh articles fetched this run — will retry next cycle.</i>"
        ]

    header = f"<b>{esc_html(topic_label)}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    messages = []
    current = header

    for i, a in enumerate(articles, 1):
        title       = esc_html(a["title"])
        summary     = esc_html(a["summary"])
        source      = esc_html(a["source"])
        date        = esc_html(a["date"])
        significance = esc_html(a["significance"])
        url         = a["url"]

        if is_valid_url(url):
            # Escape special chars for HTML attribute context
            safe_url = url.replace("&", "&amp;").replace('"', "%22")
            title_html = f'<b>{i}. <a href="{safe_url}">{title}</a></b>'
            link_line = f'🔗 <a href="{safe_url}">Open article →</a>\n'
        else:
            title_html = f'<b>{i}. {title}</b>'
            link_line = ""

        block = (
            f"\n{title_html}\n"
            f"📰 <i>{source}</i>  •  <i>{date}</i>\n"
            f"{summary}\n"
            f"{link_line}"
            f"💡 <i>{significance}</i>\n"
        )

        # If adding this block would exceed limit, flush current and start new
        if len(current) + len(block) > TELEGRAM_MSG_LIMIT:
            messages.append(current)
            current = f"<b>{esc_html(topic_label)} (cont.)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

        current += block

    if current.strip():
        messages.append(current)
    return messages


def build_all_messages(all_results: list, trends: list, run_meta: dict) -> list:
    """Build the complete list of Telegram-ready HTML message strings."""
    messages = []
    now = datetime.now()
    today = now.strftime("%A, %d %B %Y")
    time_str = now.strftime("%H:%M UTC")

    successful = sum(1 for _, a in all_results if a)
    total_articles = sum(len(a) for _, a in all_results)
    total_links = sum(1 for _, ar in all_results for a in ar if is_valid_url(a.get("url", "")))

    # ── Opening header ─────────────────────────────────────────────────────
    header = (
        f"🌍 <b>SPATIAL DRIFT</b>\n"
        f"<i>Explore · Analyze · Anticipate</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 <b>{esc_html(today)}</b>  •  🕐 <code>{esc_html(time_str)}</code>\n\n"
        f"Your geospatial intelligence brief:\n"
        f"📰 <b>{total_articles}</b> articles across <b>{successful}/{len(all_results)}</b> domains\n"
        f"🔗 <b>{total_links}</b> direct article links\n\n"
        f"<i>Tap any title or link to open the source. Use this material to fuel your blog and LinkedIn content.</i>"
    )
    messages.append(header)

    # ── Per-topic messages ──────────────────────────────────────────────────
    for topic_label, articles in all_results:
        topic_messages = build_topic_message(topic_label, articles)
        messages.extend(topic_messages)

    # ── Trends ──────────────────────────────────────────────────────────────
    if trends:
        block = (
            "📊 <b>TRENDING THEMES</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Cross-domain patterns emerging today:</i>\n"
        )
        for i, t in enumerate(trends, 1):
            block += f"\n<b>{i}. {esc_html(t['theme'])}</b>\n{esc_html(t['insight'])}\n"
        messages.append(block)

    # ── Footer ──────────────────────────────────────────────────────────────
    messages.append(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>SPATIAL DRIFT</b> — <i>Explore. Analyze. Anticipate.</i>\n\n"
        f"<i>Next brief in ~6 hours.</i>\n"
        f"<i>Run: {successful}/{len(all_results)} domains • {total_articles} articles • "
        f"{total_links} links • {esc_html(run_meta.get('elapsed','—'))}</i>"
    )

    return messages


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM DELIVERY
# ═══════════════════════════════════════════════════════════════════════════════

def send_telegram(text: str, retries: int = 3) -> tuple:
    """Send HTML message to Telegram. Returns (ok, error_or_None)."""
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
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

            # If HTML parse error, fall back to plain text
            if "parse" in (last_err or "").lower() or "entities" in (last_err or "").lower():
                plain = re.sub(r"<[^>]+>", "", text)
                try:
                    r2 = requests.post(api_url, json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": plain[:4096],
                    }, timeout=20)
                    r2.raise_for_status()
                    return True, f"plain-text fallback (HTML err: {last_err[:80]})"
                except Exception as e2:
                    last_err = f"{last_err} | plain fallback: {e2}"
        except Exception as e:
            last_err = str(e)

        if attempt < retries - 1:
            time.sleep(2 ** attempt)

    return False, last_err


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    start = time.time()
    print()
    print("╔══════════════════════════════════════════════╗")
    print("║    SPATIAL DRIFT v3.0 — Daily Alert          ║")
    print("║    Explore. Analyze. Anticipate.             ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"  Run started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Target: {NUM_ARTICLES_PER_TOPIC} articles × {len(TOPICS)} domains")
    print()

    # ── Fetch each topic ────────────────────────────────────────────────────
    all_results = []
    for emoji, label, query in TOPICS:
        full_label = f"{emoji} {label}"
        print(f"  📡 [{label}]")
        articles = fetch_topic(emoji, label, query)
        all_results.append((full_label, articles))
        with_url = sum(1 for a in articles if is_valid_url(a.get("url", "")))
        status = "✅" if articles else "⚠️ "
        print(f"     {status} {len(articles)} article(s) found, {with_url} with valid URL")
        for a in articles:
            url = a.get("url", "")
            mark = "🔗" if is_valid_url(url) else "❌"
            print(f"        {mark} {a.get('title','')[:65]}")
            if not is_valid_url(url):
                print(f"           url value: {url[:80]!r}")
        time.sleep(1)

    # ── Trends ──────────────────────────────────────────────────────────────
    print()
    print("  🧠 Analyzing cross-topic trends ...")
    trends = fetch_trends(all_results)
    print(f"     {'✅' if trends else '⚠️ '} {len(trends)} trend(s) identified")

    # ── Build & send ────────────────────────────────────────────────────────
    elapsed = f"{int(time.time() - start)}s"
    print()
    print("  📨 Building Telegram messages ...")
    messages = build_all_messages(all_results, trends, {"elapsed": elapsed})
    print(f"     {len(messages)} message(s) ready")

    print()
    print(f"  📤 Sending to Telegram ...")
    delivered = 0
    for i, msg in enumerate(messages, 1):
        ok, err = send_telegram(msg)
        if ok:
            note = f" ({err})" if err else ""
            print(f"     ✅ {i}/{len(messages)}{note}")
            delivered += 1
        else:
            print(f"     ❌ {i}/{len(messages)} — {err}")
        time.sleep(0.5)

    # ── Summary ─────────────────────────────────────────────────────────────
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
    print(f"  │  Trends identified:  {len(trends):2d}                 │")
    print(f"  │  Messages delivered: {delivered:2d}/{len(messages):<2d}              │")
    print(f"  │  Total runtime:      {elapsed:<7}            │")
    print("  └─────────────────────────────────────────┘")
    print()

    if delivered == 0:
        raise SystemExit("No messages delivered.")


if __name__ == "__main__":
    main()
