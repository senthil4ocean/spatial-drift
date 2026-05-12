"""
╔═══════════════════════════════════════════════════════╗
║          SPATIAL DRIFT — Daily Intelligence Alert      ║
║          Explore. Analyze. Anticipate.                 ║
╚═══════════════════════════════════════════════════════╝

Fetches the latest geospatial news via Anthropic API (with
live web search) and delivers a formatted digest to Telegram.

Schedule: Daily at 7:00 AM IST via GitHub Actions.
"""

import os
import json
import time
import requests
from datetime import datetime

# ── Credentials ────────────────────────────────────────────────────────────────
# These can also be set as environment variables (recommended for security).
# If env vars are present they take priority; otherwise the defaults below are used.

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "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")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8755526579:AAEIYLkfrmFV5Byprb-uyGeXzUIDaHsqk_s")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "1739337359")

# ── Topic Definitions ──────────────────────────────────────────────────────────
TOPICS = [
    ("🛰️ Remote Sensing & Earth Observation",
     "remote sensing satellite imagery earth observation LiDAR photogrammetry 2025 2026"),

    ("🗺️ GIS & Geospatial Technology",
     "GIS geospatial technology mapping geographic information systems spatial analysis 2025 2026"),

    ("🌡️ Climatology & Atmospheric Science",
     "climatology climate change extreme weather atmospheric science IPCC 2025 2026"),

    ("🌊 Oceanography & Marine Science",
     "oceanography sea level rise ocean temperature marine science coral reefs 2025 2026"),

    ("🏔️ Plate Tectonics & Seismology",
     "plate tectonics earthquakes seismology fault lines geological activity 2025 2026"),

    ("🌋 Volcanology",
     "volcanology volcanic eruption lava magma volcanic monitoring 2025 2026"),

    ("⛏️ Mining & Mineral Resources",
     "mining geoscience mineral exploration lithium rare earth resources extraction 2025 2026"),

    ("🪨 Geology & Geomorphology",
     "geology geological survey rock formations stratigraphy geomorphology 2025 2026"),

    ("🚀 Space & Geodesy",
     "space earth observation ESA NASA Copernicus ISRO geodesy satellite launch 2025 2026"),
]

# ── Prompts ────────────────────────────────────────────────────────────────────
NEWS_SYSTEM_PROMPT = """You are a geospatial sciences news researcher for SPATIAL DRIFT — a premium intelligence platform. Search the web for the most recent and significant news on the given topic.

Return ONLY a valid JSON array (no markdown, no backticks, no preamble) with exactly 2 news items.

Each item must have:
- "title": concise news headline (string, max 90 chars)
- "summary": 1–2 sentence summary of what happened (string)
- "source": publication or agency name (string)
- "date": approximate date like "May 2026" (string)
- "significance": one sentence on why this matters for geospatial professionals (string, max 100 chars)

Return only the raw JSON array, nothing else."""


# ── Core Functions ─────────────────────────────────────────────────────────────

def fetch_news(topic_label: str, topic_query: str) -> list:
    """Call Anthropic API with live web search to get latest news for a topic."""
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "system": NEWS_SYSTEM_PROMPT,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{
                    "role": "user",
                    "content": f"Find the 2 most recent and impactful news items about: {topic_query}. Return as JSON array only."
                }],
            },
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()

        text_block = next((b for b in data.get("content", []) if b.get("type") == "text"), None)
        if not text_block:
            return []

        raw = text_block["text"].strip().replace("```json", "").replace("```", "").strip()
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end == -1:
            return []
        return json.loads(raw[start:end + 1])

    except Exception as e:
        print(f"    ⚠️  Error for '{topic_label}': {e}")
        return []


def escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def send_telegram(text: str, retries: int = 2) -> bool:
    """Send a message to Telegram with retry logic."""
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            r.raise_for_status()
            return True
        except Exception:
            # Fallback: plain text
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]},
                    timeout=15,
                )
                return True
            except Exception as e2:
                if attempt < retries:
                    time.sleep(2)
                else:
                    print(f"    ❌ Telegram failed after {retries+1} attempts: {e2}")
                    return False
    return False


def build_messages(all_results: list) -> list:
    """Build a list of Telegram-ready message strings."""
    today = datetime.now().strftime("%A, %d %B %Y")
    messages = []

    # ── Opening header ─────────────────────────────────────────────────────
    header = (
        f"🌍 *SPATIAL DRIFT*\n"
        f"_Explore\\. Analyze\\. Anticipate\\._\n"
        f"{'─' * 28}\n"
        f"📅 _{escape_md(today)}_\n\n"
        f"Your daily geospatial intelligence brief across *{len([r for r in all_results if r[1]])} domains*\\. "
        f"Use these insights to write your next blog post or LinkedIn update\\.\n"
    )
    messages.append(header)

    # ── One message per topic ───────────────────────────────────────────────
    for topic_label, articles in all_results:
        if not articles:
            continue

        block = f"*{escape_md(topic_label)}*\n{'─' * 22}\n"
        for i, a in enumerate(articles, 1):
            title       = escape_md(a.get("title", "No title"))
            summary     = escape_md(a.get("summary", ""))
            source      = escape_md(a.get("source", "Unknown"))
            date        = escape_md(a.get("date", ""))
            significance = escape_md(a.get("significance", ""))

            block += (
                f"\n*{i}\\. {title}*\n"
                f"📰 _{source}_ • _{date}_\n"
                f"{summary}\n"
                f"💡 _{significance}_\n"
            )

        messages.append(block)

    # ── Footer ──────────────────────────────────────────────────────────────
    messages.append(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🌐 *SPATIAL DRIFT* — Geospatial Intelligence\n"
        "_Ready to write\\? Open your blog or LinkedIn and own the conversation\\!_"
    )
    return messages


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print()
    print("╔══════════════════════════════════════════╗")
    print("║    SPATIAL DRIFT — Daily Alert System    ║")
    print("║    Explore. Analyze. Anticipate.         ║")
    print("╚══════════════════════════════════════════╝")
    print(f"  Run time: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print()

    all_results = []
    for topic_label, topic_query in TOPICS:
        print(f"  📡 Scanning: {topic_label} ...")
        articles = fetch_news(topic_label, topic_query)
        all_results.append((topic_label, articles))
        found = len(articles)
        print(f"     {'✅' if found else '⚠️ '} {found} article(s) found")
        time.sleep(1)  # polite rate limiting

    print()
    print("  📨 Building Telegram messages ...")
    messages = build_messages(all_results)

    print(f"  📤 Sending {len(messages)} message(s) ...")
    success = 0
    for i, msg in enumerate(messages, 1):
        ok = send_telegram(msg)
        print(f"     {'✅' if ok else '❌'} Message {i}/{len(messages)}")
        if ok:
            success += 1
        time.sleep(0.5)  # avoid Telegram flood limits

    print()
    print(f"  🎉 Done! {success}/{len(messages)} messages delivered.")
    print()


if __name__ == "__main__":
    main()
