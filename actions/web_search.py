#web_search.py
import threading
import time

# ── Gemini grounding quota circuit breaker ────────────────────────────────────
# The google_search grounding tool has its own small quota, separate from plain
# generation.  Once it is spent every call returns 429 — so retrying it at the
# top of every search only adds a dead round-trip before the DDG fallback runs.
# After a quota error, skip Gemini entirely for a cooldown period.
_QUOTA_COOLDOWN_SEC  = 900          # 15 minutes
_quota_blocked_until = 0.0
_quota_lock          = threading.Lock()
_search_slots         = threading.BoundedSemaphore(4)


def _gemini_available() -> bool:
    with _quota_lock:
        return time.monotonic() >= _quota_blocked_until


def _note_gemini_error(exc: Exception) -> None:
    """Trip the breaker when the error is a quota / rate-limit rejection."""
    global _quota_blocked_until
    msg = str(exc)
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        with _quota_lock:
            already = time.monotonic() < _quota_blocked_until
            _quota_blocked_until = time.monotonic() + _QUOTA_COOLDOWN_SEC
        if not already:
            print(
                "[WebSearch] Gemini grounding quota exhausted — skipping it for "
                f"{_QUOTA_COOLDOWN_SEC // 60} min and serving results from DDG."
            )


class _QuotaCooldown(RuntimeError):
    """Raised instead of calling Gemini while the quota breaker is open."""


def _log_gemini_failure(context: str, exc: Exception) -> None:
    """Log a Gemini failure — silently when it is just the expected cooldown."""
    if isinstance(exc, _QuotaCooldown):
        return          # announced once when the breaker tripped; not a warning
    print(f"[WebSearch] ⚠️ {context} failed ({type(exc).__name__}) — using DDG instead")


def _run_bounded(fn, timeout: float, label: str = "task"):
    """Run fn in one of a bounded number of daemon fallback workers."""
    box = [None]
    slots = _search_slots
    if not slots.acquire(blocking=False):
        print(f"[WebSearch] {label} skipped because search workers are busy")
        return None

    def _run():
        try:
            box[0] = fn()
        except Exception as e:
            _log_gemini_failure(label, e)
        finally:
            slots.release()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        print(f"[WebSearch] {label} exceeded {timeout:.0f}s — moving on")
    return box[0]

def _gemini_search(query: str) -> str:
    if not _gemini_available():
        raise _QuotaCooldown("Gemini grounding is in quota cooldown")

    from core import gemini

    # Grounded search reads a live page, so it gets a longer deadline than the
    # default — but it still HAS one, and it still walks the fallback ladder.
    try:
        response = gemini.call(query, tier=gemini.SEARCH,
                               config={"tools": [{"google_search": {}}]},
                               timeout_ms=30_000)
        if response is None:
            raise RuntimeError("every Gemini model on the ladder failed")
    except Exception as e:
        _note_gemini_error(e)
        raise

    text = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text

    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    return text[:100_000]


def _get_ddgs():
    """
    Returns the DDGS class.  The package was renamed duckduckgo-search -> ddgs;
    the legacy package's endpoints are now rejected by DuckDuckGo (news() gets a
    403 Ratelimit, text() silently returns zero results), so warn loudly if we
    end up on it instead of failing in silence.
    """
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        from duckduckgo_search import DDGS
        print(
            "[WebSearch] ⚠️ Using the deprecated 'duckduckgo-search' package — "
            "DuckDuckGo blocks its endpoints, so every search will come back "
            "empty.  Fix with:  pip install -U ddgs"
        )
        return DDGS


def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    DDGS = _get_ddgs()
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title":   r.get("title",  ""),
                    "snippet": r.get("body",   ""),
                    "url":     r.get("href",   ""),
                })
    except Exception as e:
        print(f"[WebSearch] ⚠️ DDG text search failed ({type(e).__name__}).")
    return results


def _ddg_news(query: str, max_results: int = 8) -> list[dict]:
    """DDG news search — returns actual articles, not website homepages."""
    DDGS = _get_ddgs()
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.news(query, max_results=max_results):
                results.append({
                    "title":   r.get("title",  ""),
                    "snippet": r.get("body",   ""),
                    "url":     r.get("url",    ""),
                    "source":  r.get("source", ""),
                })
    except Exception as e:
        print(f"[WebSearch] ⚠️ DDG news search failed ({type(e).__name__}); falling back to text search.")
    # Also covers the legacy-package case, where news() returns an empty list
    # instead of raising.
    if not results:
        results = _ddg_search(query, max_results=max_results)
    return results


def _clean_result(value, maximum: int) -> str:
    text = "".join(
        character if ord(character) >= 32 and ord(character) != 127 else " "
        for character in str(value or "")
    )
    return " ".join(text.split())[:maximum]


def _format_ddg(query: str, results: list[dict]) -> str:
    clean_query = _clean_result(query, 500)
    if not results:
        return f"No results found for: {clean_query}"

    lines = [f"Search results for: {clean_query}\n"]
    for i, r in enumerate(results[:10], 1):
        title = _clean_result(r.get("title"), 500)
        snippet = _clean_result(r.get("snippet"), 1_500)
        url = _clean_result(r.get("url"), 2_048)
        if title: lines.append(f"{i}. {title}")
        if snippet: lines.append(f"   {snippet}")
        if url.startswith(("http://", "https://")):
            lines.append(f"   Source: {url}")
        lines.append("")
    return "\n".join(lines).strip()[:30_000]


def _format_news(query: str, results: list[dict]) -> str:
    clean_query = _clean_result(query, 500)
    if not results:
        return f"No news found for: {clean_query}"

    lines = [f"Latest news: {clean_query}\n"]
    for i, r in enumerate(results[:10], 1):
        title = _clean_result(r.get("title"), 500)
        if not title:
            continue
        source = _clean_result(r.get("source"), 100)
        src = f"  [{source}]" if source else ""
        lines.append(f"{i}. {title}{src}")
        snippet = _clean_result(r.get("snippet"), 140)
        if snippet:
            lines.append(f"   {snippet}")
        url = _clean_result(r.get("url"), 2_048)
        if url.startswith(("http://", "https://")):
            lines.append(f"   {url}")
        lines.append("")
    return "\n".join(lines).strip()[:30_000]


# ── Briefing helper ────────────────────────────────────────────────────────────

def _gemini_headlines(n: int = 5) -> tuple[list[str], str]:
    """
    Fetches current headlines via Gemini grounded search.
    Optimised for speed: minimal prompt + strict token cap.
    Returns (headline_list, raw_text_for_display).
    """
    import re
    from core import gemini

    response = gemini.call(
        f"Current world news: {n} headlines. Numbered list, titles only.",
        tier=gemini.SEARCH,
        config={"tools": [{"google_search": {}}]},
        timeout_ms=30_000,
    )
    if response is None:
        return [], ""

    raw = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            raw += part.text

    raw = raw.strip()[:100_000]
    headlines = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Only accept lines that begin with a number — skips preamble/closing sentences
        if not re.match(r'^[\d]+[.\)\-]', line):
            continue
        clean = re.sub(r'^[\d]+[.\)\-]\s*', '', line)
        clean = re.sub(r'^\*+\s*',          '', clean).strip()
        clean = _clean_result(clean, 500)
        if clean and len(clean) > 10:
            headlines.append(clean)

    return headlines[:n], raw.strip()


# ── Modes ──────────────────────────────────────────────────────────────────────

def _search(query: str) -> str:
    """Default search — Gemini grounded, DDG fallback."""
    try:
        return _gemini_search(query)
    except Exception as e:
        _log_gemini_failure("Gemini search", e)
        results = _run_bounded(
            lambda: _ddg_search(query), timeout=5.0, label="DDG search"
        ) or []
        return _format_ddg(query, results)


def _news(query: str) -> str:
    """
    DDG first, Gemini as backup.

    The old version raced both backends in parallel and kept the first answer.
    That burned one google_search grounding call on *every* news request —
    including the startup briefing — even when DDG had already won the race.
    Grounding has a small quota, so it ran dry after a handful of launches and
    then 429'd for everything else (research/compare), which are the modes that
    actually need a synthesised answer.

    DDG news returns in well under a second and gives raw headlines, which is
    exactly what the briefing wants, so it goes first and Gemini is only touched
    when DDG comes back empty.
    """
    gemini_query = f"latest news today: {query}" if query else "top world news today"
    ddg_query    = query if query else "world news today"

    def _ddg_attempt() -> str:
        return _format_news(ddg_query, _ddg_news(ddg_query, max_results=8))

    text = _run_bounded(_ddg_attempt, timeout=5.0, label="DDG news")
    if text and len(text) > 60 and not text.startswith("No news found"):
        return text

    text = _run_bounded(
        lambda: _gemini_search(gemini_query), timeout=6.0, label="Gemini news"
    )
    if text and len(text) > 60:
        return text

    return f"No news found for: {query}"


def _research(query: str) -> str:
    """
    Deep dive — asks Gemini for a comprehensive answer with context.
    Falls back to a wider DDG fetch.
    """
    research_query = (
        f"Comprehensive, detailed explanation of: {query}. "
        "Include background context, key facts, current state, and important nuances."
    )
    try:
        return _gemini_search(research_query)
    except Exception as e:
        _log_gemini_failure("Gemini research", e)
        results = _run_bounded(
            lambda: _ddg_search(query, max_results=10),
            timeout=5.0,
            label="DDG research",
        ) or []
        return _format_ddg(query, results)


def _price(query: str) -> str:
    """Product price lookup — searches for current market prices."""
    price_query = f"current price of {query} — how much does it cost today"
    try:
        return _gemini_search(price_query)
    except Exception as e:
        _log_gemini_failure("Gemini price", e)
        results = _run_bounded(
            lambda: _ddg_search(f"{query} price buy", max_results=6),
            timeout=5.0,
            label="DDG price",
        ) or []
        return _format_ddg(query, results)


def _compare(items: list[str], aspect: str) -> str:
    query = (
        f"Compare {', '.join(items)} in terms of {aspect}. "
        "Give specific facts and data."
    )
    try:
        return _gemini_search(query)
    except Exception as e:
        _log_gemini_failure("Gemini compare", e)

    all_results: dict[str, list] = {}
    for item in items:
        try:
            all_results[item] = _run_bounded(
                lambda item=item: _ddg_search(f"{item} {aspect}", max_results=3),
                timeout=5.0,
                label="DDG comparison",
            ) or []
        except Exception:
            all_results[item] = []

    lines = [f"Comparison — {_clean_result(aspect, 200).upper()}", "─" * 40]
    for item in items:
        lines.append(f"\n▸ {_clean_result(item, 200)}")
        for r in all_results.get(item, [])[:2]:
            snippet = _clean_result(r.get("snippet"), 1_500)
            if snippet:
                lines.append(f"  • {snippet}")
            url = _clean_result(r.get("url"), 2_048)
            if url.startswith(("http://", "https://")):
                lines.append(f"    {url}")
    return "\n".join(lines)[:30_000]


# ── Public entry point ─────────────────────────────────────────────────────────

def web_search(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters if isinstance(parameters, dict) else {}
    raw_query = params.get("query", "")
    raw_mode = params.get("mode", "search")
    raw_items = params.get("items", [])
    raw_aspect = params.get("aspect", "general")
    query = raw_query[:500].strip() if isinstance(raw_query, str) else ""
    mode = raw_mode[:16].lower().strip() if isinstance(raw_mode, str) else "search"
    items = [str(item)[:200] for item in raw_items[:10]] if isinstance(raw_items, list) else []
    aspect = raw_aspect[:200].strip() if isinstance(raw_aspect, str) else "general"
    aspect = aspect or "general"

    if not query and not items:
        return "Please provide a search query."

    if items and mode not in ("compare",):
        mode = "compare"

    if player:
        player.write_log(f"[Search:{mode}] request received")

    print(f"[WebSearch] 🔍 mode={mode!r}  query_length={len(query)}")

    try:
        if mode == "compare" and items:
            return _compare(items, aspect)
        if mode == "news":
            return _news(query)
        if mode == "research":
            return _research(query)
        if mode == "price":
            return _price(query)
        return _search(query)

    except Exception as e:
        print(f"[WebSearch] ❌ All backends failed ({type(e).__name__}).")
        return f"Search failed ({type(e).__name__})."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "web_search",
    "description": "Searches the web. Use for ANY question about current facts, events, prices, or topics — always prefer this over guessing. Modes: 'search' (default), 'news' (latest headlines on a topic), 'research' (deep comprehensive answer), 'price' (product cost lookup), 'compare' (side-by-side comparison of items).",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "maxLength": 500,
                "description": "Search query or topic"
            },
            "mode": {
                "type": "STRING",
                "enum": ["search", "news", "research", "price", "compare"],
                "maxLength": 16,
                "description": "search | news | research | price | compare"
            },
            "items": {
                "type": "ARRAY",
                "maxItems": 10,
                "items": {
                    "type": "STRING",
                    "maxLength": 200
                },
                "description": "Items to compare (compare mode)"
            },
            "aspect": {
                "type": "STRING",
                "maxLength": 100,
                "description": "Comparison aspect: price | specs | reviews | features"
            }
        },
        "required": []
    },
    "handler": web_search,
}
