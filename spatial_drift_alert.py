"""
╔═══════════════════════════════════════════════════════╗
║          SPATIAL DRIFT — Daily Intelligence Alert      ║
║          Explore. Analyze. Anticipate.                 ║
║          v2.0 — Reliability & Links Update             ║
╚═══════════════════════════════════════════════════════╝

Fetches the latest geospatial news via Anthropic API (with live
web search) and delivers a formatted digest to Telegram, 4×/day.

KEY IMPROVEMENTS IN v2.0
- HTML parse mode (far more reliable than MarkdownV2)
- Clickable article links in every message
- Per-topic retry logic with exponential backoff
- Diagnostic output reveals which topic failed and why
- Wider max_tokens to prevent truncation
- Cross-topic "Trending Themes" summary at the end
- Improved JSON extraction handles wrapped/dirty responses
"""

import os
import re
import json
import time
import html
import requests
from datetime import datetime

# ── Credentials ────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "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")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8755526579:AAEIYLkfrmFV5Byprb-uyGeXzUIDaHsqk_s")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "1739337359")

# ── Topic Definitions ──────────────────────────────────────────────────────────
TOPICS = [
    ("🛰️", "Remote Sensing & Earth Observation",
     "latest remote sensing satellite imagery earth observation LiDAR breakthrough"),

    ("🗺️", "GIS & Geospatial Technology",
     "latest GIS geospatial technology mapping spatial analysis news"),

    ("🌡️", "Climatology & Atmospheric Science",
     "latest climate change extreme weather atmospheric research findings"),

    ("🌊", "Oceanography & Marine Science",
     "latest oceanography sea level ocean temperature marine discovery"),

    ("🏔️", "Plate Tectonics & Seismology",
     "latest earthquake seismology plate tectonics fault discovery"),

    ("🌋", "Volcanology",
     "latest volcanic eruption volcano monitoring activity news"),

    ("⛏️", "Mining & Mineral Resources",
     "latest mining mineral exploration lithium rare earth discovery"),

    ("🪨", "Geology & Geomorphology",
     "latest geology geological discovery rock formation stratigraphy"),

    ("🚀", "Space & Geodesy",
     "latest satellite launch space mission ESA NASA ISRO earth observation"),
]

NUM_ARTICLES_PER_TOPIC = 2
MAX_RETRIES_PER_TOPIC = 2

# ── Prompts ────────────────────────────────────────────────────────────────────
NEWS_SYSTEM_PROMPT = f"""You are the intelligence research engine for SPATIAL DRIFT — a premium geospatial news platform.

Your job: use web_search to find the {NUM_ARTICLES_PER_TOPIC} MOST RECENT and SIGNIFICANT real news articles for the given domain. Prefer articles from the last 30 days. Always perform the web search before answering.

Return ONLY a valid JSON array (no markdown fences, no preamble, no commentary).

Each item MUST have these exact keys:
- "title":        headline (string, max 100 chars)
- "summary":      1–2 sentence summary (string)
- "source":       publication name (string, e.g. "ESA", "Nature", "Reuters")
- "date":         e.g. "May 2026" or "2 weeks ago" (string)
- "url":          the actual article URL from your web search results (string, must start with http)
- "significance": one sentence on why geospatial professionals should care (string, max 110 chars)

Critical:
- The "url" field MUST be a real URL you got from web_search results, never fabricated.
- If you cannot find {NUM_ARTICLES_PER_TOPIC} articles, return what you found (1 is OK, 0 means return empty array []).
- Return the raw JSON array starting with [ and ending with ]. Nothing else."""


TRENDS_SYSTEM_PROMPT = """You are a trend analyst for SPATIAL DRIFT. Given a set of recent geospatial news headlines and summaries, identify 3 cross-cutting trends or themes that emerge across the domains.

Return ONLY a valid JSON array with exactly 3 items. Each item:
- "theme":   short trend name (string, max 50 chars)
- "insight": one sentence explaining the cross-domain pattern (string, max 150 chars)

Return raw JSON only, no markdown."""


# ── Core: API call with retries ────────────────────────────────────────────────

def call_anthropic(system_prompt: str, user_msg: str, use_search: bool = True, max_tokens: int = 1500, retries: int = MAX_RETRIES_PER_TOPIC) -> str:
    """Call Anthropic API, return raw text response. Retries with exponential backoff."""
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
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
                timeout=120,
            )
            r.raise_for_status()
            data = r.json()

            # Join all text blocks (the model may emit multiple text blocks
            # interleaved with tool-use blocks when web search runs)
            text_parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
            combined = "\n".join(t for t in text_parts if t).strip()

            if not combined:
                raise ValueError(f"No text content in response. Stop reason: {data.get('stop_reason')}")

            return combined

        except requests.exceptions.HTTPError as e:
            last_err = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

        if attempt < retries:
            wait = 2 ** attempt
            print(f"        ↻  Retry {attempt+1}/{retries} after {wait}s ({last_err})")
            time.sleep(wait)

    raise RuntimeError(f"API call failed after {retries+1} attempts. Last error: {last_err}")


# ── JSON extraction ────────────────────────────────────────────────────────────

def extract_json_array(raw: str) -> list:
    """
    Robustly extract a JSON array from text. Handles:
    - Plain JSON arrays
    - ```json fenced blocks
    - JSON wrapped in explanatory text
    - Mid-text arrays
    Returns [] if nothing parseable found.
    """
    if not raw:
        return []

    # Strip code fences if present
    cleaned = re.sub(r"```(?:json)?\s*", "", raw)
    cleaned = cleaned.replace("```", "").strip()

    # Find the first balanced [...] block
    start = cleaned.find("[")
    if start == -1:
        return []

    # Walk forward to find the matching ]
    depth = 0
    in_string = False
    escape = False
    end = -1
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
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

    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as e:
        print(f"        ⚠️  JSON parse error: {e}")
        print(f"        Raw snippet: {cleaned[start:start+200]!r}")
        return []


# ── Fetch news for one topic ───────────────────────────────────────────────────

def fetch_topic(emoji: str, label: str, query: str) -> list:
    """Fetch news for one topic, with retries and diagnostic output."""
    user_msg = (
        f"Search the web for: {query}. "
        f"Find the {NUM_ARTICLES_PER_TOPIC} most recent and significant articles. "
        f"Return as JSON array with the exact schema specified."
    )
    try:
        raw = call_anthropic(NEWS_SYSTEM_PROMPT, user_msg, use_search=True)
        articles = extract_json_array(raw)
        if not articles:
            print(f"        ⚠️  Empty result. Raw response preview:")
            print(f"        {raw[:300]!r}")
            return []
        return articles
    except Exception as e:
        print(f"        ❌  Hard failure: {e}")
        return []


# ── Cross-topic trends ─────────────────────────────────────────────────────────

def fetch_trends(all_articles: list) -> list:
    """Analyze trends across all collected articles."""
    if not all_articles:
        return []

    # Build context from titles + summaries
    context_lines = []
    for label, articles in all_articles:
        for a in articles:
            context_lines.append(f"- [{label}] {a.get('title','')} — {a.get('summary','')}")

    if not context_lines:
        return []

    user_msg = (
        "Below are recent geospatial news items from multiple domains. "
        "Identify 3 cross-cutting trends that connect them.\n\n"
        + "\n".join(context_lines[:30])  # cap to avoid token bloat
    )

    try:
        raw = call_anthropic(TRENDS_SYSTEM_PROMPT, user_msg, use_search=False, max_tokens=800, retries=1)
        return extract_json_array(raw)
    except Exception as e:
        print(f"  ⚠️  Trends analysis failed: {e}")
        return []


# ── HTML formatting helpers ────────────────────────────────────────────────────

def esc(text: str) -> str:
    """Escape text for Telegram HTML mode. Only <, >, & need escaping."""
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def is_valid_url(url: str) -> bool:
    """Basic URL sanity check."""
    if not isinstance(url, str):
        return False
    return url.startswith(("http://", "https://")) and len(url) < 500


# ── Build Telegram messages ────────────────────────────────────────────────────

def build_messages(all_results: list, trends: list, run_meta: dict) -> list:
    """Build the list of HTML-formatted Telegram message strings."""
    messages = []
    now = datetime.now()
    today = now.strftime("%A, %d %B %Y")
    time_str = now.strftime("%H:%M UTC")

    # Count successes
    successful = sum(1 for _, a in all_results if a)
    total_articles = sum(len(a) for _, a in all_results)

    # ── Opening header ─────────────────────────────────────────────────────
    header = (
        f"🌍 <b>SPATIAL DRIFT</b>\n"
        f"<i>Explore · Analyze · Anticipate</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 <b>{esc(today)}</b>\n"
        f"🕐 <code>{esc(time_str)}</code>\n\n"
        f"Your geospatial intelligence brief — "
        f"<b>{total_articles} articles</b> across <b>{successful}/{len(all_results)} domains</b>.\n\n"
        f"<i>Tap any link to open the source article. Use these to fuel your next blog post or LinkedIn update.</i>"
    )
    messages.append(header)

    # ── One message per topic ───────────────────────────────────────────────
    for emoji_label, articles in all_results:
        if not articles:
            # Show a brief placeholder so user knows the domain was attempted
            messages.append(
                f"{esc(emoji_label)}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>No fresh articles fetched in this run — will retry next cycle.</i>"
            )
            continue

        block = f"<b>{esc(emoji_label)}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        for i, a in enumerate(articles, 1):
            title       = esc(a.get("title", "Untitled"))
            summary     = esc(a.get("summary", ""))
            source      = esc(a.get("source", "Unknown"))
            date        = esc(a.get("date", ""))
            significance = esc(a.get("significance", ""))
            url         = a.get("url", "").strip()

            # Title with link if URL is valid
            if is_valid_url(url):
                # In HTML mode, URLs in href need quotes escaped
                safe_url = url.replace('"', '%22')
                title_html = f'<a href="{safe_url}"><b>{i}. {title}</b></a>'
                link_line = f'🔗 <a href="{safe_url}">Read full article</a>\n'
            else:
                title_html = f'<b>{i}. {title}</b>'
                link_line = ""

            block += (
                f"\n{title_html}\n"
                f"📰 <i>{source}</i>  •  <i>{date}</i>\n"
                f"{summary}\n"
                f"{link_line}"
                f"💡 <i>{significance}</i>\n"
            )

        messages.append(block)

    # ── Trends section ──────────────────────────────────────────────────────
    if trends:
        trends_block = (
            "📊 <b>TRENDING THEMES</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Patterns emerging across the geospatial landscape today:</i>\n"
        )
        for i, t in enumerate(trends, 1):
            theme   = esc(t.get("theme", ""))
            insight = esc(t.get("insight", ""))
            trends_block += f"\n<b>{i}. {theme}</b>\n{insight}\n"
        messages.append(trends_block)

    # ── Footer ──────────────────────────────────────────────────────────────
    messages.append(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🌐 <b>SPATIAL DRIFT</b>\n"
        "<i>Explore. Analyze. Anticipate.</i>\n\n"
        f"Next brief: in ~6 hours.\n"
        f"<i>Run stats: {successful}/{len(all_results)} domains • {total_articles} articles • {esc(run_meta.get('elapsed','—'))}</i>"
    )

    return messages


# ── Telegram sending ───────────────────────────────────────────────────────────

def send_telegram(text: str, retries: int = 3) -> tuple:
    """Send a message to Telegram. Returns (success: bool, error: str|None)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4096],  # Telegram hard limit
        "parse_mode": "HTML",
        "disable_web_page_preview": False,  # We WANT link previews for the first link
    }

    last_err = None
    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, timeout=20)
            r.raise_for_status()
            return True, None
        except requests.exceptions.HTTPError as e:
            try:
                err_body = e.response.json()
                last_err = err_body.get("description", str(e))
            except Exception:
                last_err = str(e)
            # If HTML parse failed, retry as plain text
            if "parse" in (last_err or "").lower():
                plain = re.sub(r"<[^>]+>", "", text)
                payload_fallback = {"chat_id": TELEGRAM_CHAT_ID, "text": plain[:4096]}
                try:
                    requests.post(url, json=payload_fallback, timeout=15).raise_for_status()
                    return True, f"sent as plain text (HTML failed: {last_err})"
                except Exception as e2:
                    last_err = f"{last_err} | plain fallback also failed: {e2}"
        except Exception as e:
            last_err = str(e)

        if attempt < retries - 1:
            time.sleep(2 ** attempt)

    return False, last_err


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    start = time.time()
    print()
    print("╔══════════════════════════════════════════╗")
    print("║    SPATIAL DRIFT v2.0 — Daily Alert      ║")
    print("║    Explore. Analyze. Anticipate.         ║")
    print("╚══════════════════════════════════════════╝")
    print(f"  Run started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print()

    # ── Fetch each topic ────────────────────────────────────────────────────
    all_results = []
    for emoji, label, query in TOPICS:
        full_label = f"{emoji} {label}"
        print(f"  📡 [{label}]")
        articles = fetch_topic(emoji, label, query)
        all_results.append((full_label, articles))
        status = "✅" if articles else "⚠️ "
        print(f"     {status} {len(articles)} article(s) found")
        if articles:
            for a in articles:
                url = a.get("url", "")
                has_url = "🔗" if is_valid_url(url) else "  "
                print(f"        {has_url} {a.get('title', 'No title')[:70]}")
        time.sleep(1)  # rate limit politeness

    # ── Trends analysis ─────────────────────────────────────────────────────
    print()
    print("  🧠 Analyzing cross-topic trends ...")
    trends = fetch_trends(all_results)
    print(f"     {'✅' if trends else '⚠️ '} {len(trends)} trend(s) identified")

    # ── Build & send messages ───────────────────────────────────────────────
    elapsed = f"{int(time.time() - start)}s"
    print()
    print("  📨 Building Telegram messages ...")
    messages = build_messages(all_results, trends, {"elapsed": elapsed})
    print(f"     {len(messages)} message(s) ready")

    print()
    print(f"  📤 Sending to Telegram ...")
    delivered = 0
    for i, msg in enumerate(messages, 1):
        ok, err = send_telegram(msg)
        if ok:
            print(f"     ✅ {i}/{len(messages)}" + (f" ({err})" if err else ""))
            delivered += 1
        else:
            print(f"     ❌ {i}/{len(messages)} — {err}")
        time.sleep(0.5)

    # ── Summary ─────────────────────────────────────────────────────────────
    print()
    print("  ┌─────────────────────────────────────┐")
    print(f"  │  RUN SUMMARY                        │")
    print("  ├─────────────────────────────────────┤")
    print(f"  │  Domains scanned:    {len(TOPICS):2d}             │")
    print(f"  │  Domains successful: {sum(1 for _,a in all_results if a):2d}             │")
    print(f"  │  Articles found:     {sum(len(a) for _,a in all_results):2d}             │")
    print(f"  │  Trends identified:  {len(trends):2d}             │")
    print(f"  │  Messages delivered: {delivered:2d}/{len(messages)}           │")
    print(f"  │  Total runtime:      {elapsed:<6}         │")
    print("  └─────────────────────────────────────┘")
    print()

    # Exit non-zero if we delivered nothing (so GitHub Actions shows red)
    if delivered == 0:
        raise SystemExit("No messages delivered.")


if __name__ == "__main__":
    main()
