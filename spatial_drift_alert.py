"""
╔═══════════════════════════════════════════════════════╗
║          SPATIAL DRIFT — Daily Intelligence Alert      ║
║          Explore. Analyze. Anticipate.                 ║
║          v4.0 — Compact, IST, Website Integration      ║
╚═══════════════════════════════════════════════════════╝

WHAT'S NEW IN v4.0
─────────────────────────────────────────────────────────
1. Single combined Telegram message when content fits.
   Auto-splits ONLY when 4096-char limit forces it.
2. No fixed article cap — returns whatever's available
   per domain (typically 3-5).
3. IST timestamp at the top of every message.
4. Saves all articles to data/articles.json which the
   companion website reads to display the same content.
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
MAX_ARTICLES_PER_TOPIC = 6        # upper bound — model returns what it finds
MAX_RETRIES_PER_TOPIC  = 2
MAX_TOKENS             = 4096
TELEGRAM_MSG_LIMIT     = 4000     # safe margin under 4096

# Output paths for the website
ROOT_DIR  = Path(__file__).parent
DATA_DIR  = ROOT_DIR / "data"
DOCS_DIR  = ROOT_DIR / "docs"
DATA_DIR.mkdir(exist_ok=True)
DOCS_DIR.mkdir(exist_ok=True)
ARTICLES_FILES = [
    DATA_DIR / "articles.json",
    DOCS_DIR / "articles.json",   # for GitHub Pages
]

# IST timezone (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

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

TASK: Use web_search to find the most recent and significant real news articles for the given geospatial domain. Return as many as you can find (up to {MAX_ARTICLES_PER_TOPIC}). Quality over quantity — only include articles you can verify.

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

Output the JSON array and nothing else."""


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def clean_text(text) -> str:
    """Strip all HTML/citation tags and normalize whitespace."""
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
            last_err = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

        if attempt < retries:
            wait = 2 ** attempt
            print(f"        ↻  Retry {attempt+1}/{retries} in {wait}s ({last_err[:80]})")
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
            print(f"        Snippet: {candidate[:200]!r}")
            return []


def best_url_match(article_title: str, search_urls: list) -> str:
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
# FETCH PER-TOPIC NEWS
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_topic(emoji: str, label: str, query: str) -> list:
    user_msg = (
        f"Search the web for the most recent and significant real news articles about: {query}.\n\n"
        f"Return up to {MAX_ARTICLES_PER_TOPIC} articles you can verify, or fewer if that's all you find. "
        f"Quality over quantity. Make sure every article has a real URL from your search results. "
        f"Return as a JSON array with the exact schema specified."
    )

    try:
        api_data = call_anthropic(NEWS_SYSTEM_PROMPT, user_msg, use_search=True)
    except Exception as e:
        print(f"        ❌ API hard failure: {e}")
        return []

    raw_text = extract_response_text(api_data)
    if not raw_text:
        print(f"        ⚠️  Empty response (stop_reason: {api_data.get('stop_reason')})")
        return []

    articles = extract_json_array(raw_text)
    if not articles:
        print(f"        ⚠️  No JSON parsed. Preview: {raw_text[:200]!r}")
        return []

    search_urls = extract_search_urls(api_data)
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

        if not is_valid_url(obj["url"]) and search_urls:
            obj["url"] = best_url_match(obj["title"], search_urls) or ""

        if obj["title"]:
            cleaned.append(obj)

    return cleaned


# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE BUILDING — COMPACT
# ═══════════════════════════════════════════════════════════════════════════════

def build_compact_message(all_results: list, run_meta: dict) -> list:
    """
    Build a single combined message. Auto-splits ONLY when 4096-char limit forces it.
    Each split is a continuation, so it reads as one flow.
    """
    ts = now_ist()
    today = ts.strftime("%A, %d %B %Y")
    time_str = ts.strftime("%I:%M %p IST")

    successful = sum(1 for _, a in all_results if a)
    total_articles = sum(len(a) for _, a in all_results)
    total_links = sum(1 for _, ar in all_results for a in ar if is_valid_url(a.get("url", "")))

    # ── Header ──
    header = (
        f"🌍 <b>SPATIAL DRIFT</b>\n"
        f"<i>Explore · Analyze · Anticipate</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 <b>{esc_html(time_str)}</b>\n"
        f"📅 <i>{esc_html(today)}</i>\n\n"
        f"📰 <b>{total_articles}</b> articles · <b>{successful}/{len(all_results)}</b> domains · "
        f"<b>{total_links}</b> live links\n\n"
    )

    # ── Build all topic blocks ──
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
            title       = esc_html(a["title"])
            source      = esc_html(a["source"])
            date        = esc_html(a["date"])
            url         = a["url"]

            if is_valid_url(url):
                safe_url = url.replace("&", "&amp;").replace('"', "%22")
                title_html = f'<b>{i}.</b> <a href="{safe_url}">{title}</a>'
            else:
                title_html = f'<b>{i}.</b> {title}'

            block += f"{title_html}\n   <i>📰 {source} · {date}</i>\n"
        topic_blocks.append(block)

    # ── Footer ──
    footer = (
        f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>SPATIAL DRIFT</b>\n"
        f"<i>Tap any title to open the article.</i>\n"
        f"<i>Visit your dashboard for full summaries + LinkedIn/blog generator.</i>\n"
        f"<i>Next brief in ~6 hours.</i>"
    )

    # ── Try to fit everything in ONE message first ──
    full_message = header + "\n".join(topic_blocks) + footer
    if len(full_message) <= TELEGRAM_MSG_LIMIT:
        return [full_message]

    # ── Otherwise, pack greedily into as few messages as possible ──
    messages = []
    current = header
    is_first = True

    for block in topic_blocks:
        # Check if block fits in current message
        if len(current) + len(block) + len(footer) + 2 > TELEGRAM_MSG_LIMIT:
            # Flush current (without footer)
            current += f"\n<i>— continued ↓</i>"
            messages.append(current)
            # Start next message
            current = f"🌍 <b>SPATIAL DRIFT</b> <i>(part {len(messages)+1})</i>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        current += block + "\n"

    # Add footer to last message
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
        "disable_web_page_preview": True,  # cleaner look in a single big message
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
# SAVE DATA FOR WEBSITE
# ═══════════════════════════════════════════════════════════════════════════════

def save_articles_for_website(all_results: list, run_meta: dict):
    """Write the latest articles to a JSON file the website can read."""
    ts = now_ist()
    payload = {
        "generated_at_ist":   ts.strftime("%Y-%m-%d %H:%M:%S IST"),
        "generated_at_utc":   datetime.now(timezone.utc).isoformat(),
        "display_date":       ts.strftime("%A, %d %B %Y"),
        "display_time":       ts.strftime("%I:%M %p IST"),
        "stats": {
            "total_articles":   sum(len(a) for _, a in all_results),
            "domains_total":    len(all_results),
            "domains_with_news": sum(1 for _, a in all_results if a),
            "elapsed":          run_meta.get("elapsed", "—"),
        },
        "domains": [
            {
                "label":    label,
                "count":    len(articles),
                "articles": articles,
            }
            for label, articles in all_results
        ],
    }

    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        for path in ARTICLES_FILES:
            path.write_text(text, encoding="utf-8")
            print(f"  💾 Wrote {payload['stats']['total_articles']} articles → {path}")
    except Exception as e:
        print(f"  ⚠️  Could not save articles.json: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    start = time.time()
    ts = now_ist()
    print()
    print("╔══════════════════════════════════════════════╗")
    print("║    SPATIAL DRIFT v4.0 — Daily Alert          ║")
    print("║    Explore. Analyze. Anticipate.             ║")
    print("╚══════════════════════════════════════════════╝")
    print(f"  Run started: {ts.strftime('%Y-%m-%d %I:%M:%S %p IST')}")
    print()

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
        time.sleep(1)

    elapsed = f"{int(time.time() - start)}s"

    # ── Save for website ──
    print()
    save_articles_for_website(all_results, {"elapsed": elapsed})

    # ── Build & send Telegram ──
    print()
    print("  📨 Building Telegram message(s)...")
    messages = build_compact_message(all_results, {"elapsed": elapsed})
    print(f"     {len(messages)} message(s) — {'single combined' if len(messages)==1 else 'auto-split due to size'}")

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
    print(f"  │  Messages sent:      {delivered:2d}/{len(messages):<2d}              │")
    print(f"  │  Total runtime:      {elapsed:<7}            │")
    print("  └─────────────────────────────────────────┘")
    print()

    if delivered == 0:
        raise SystemExit("No messages delivered.")


if __name__ == "__main__":
    main()
