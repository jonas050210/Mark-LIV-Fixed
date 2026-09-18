import webbrowser
from urllib.parse import quote_plus


def weather_action(
    parameters: dict,
    player=None,
    session_memory=None,
) -> str:
    p    = parameters if isinstance(parameters, dict) else {}
    city = p.get("city")
    when = p.get("time", "today")

    if not city or not isinstance(city, str) or not city.strip():
        msg = "Sir, the city is missing for the weather report."
        _log(msg, player)
        return msg

    city = city.strip()
    when = (when or "today").strip()

    search_query  = f"weather in {city} {when}"
    url           = f"https://www.google.com/search?q={quote_plus(search_query)}"

    try:
        opened = webbrowser.open(url)
        if not opened:
            raise RuntimeError("webbrowser.open returned False")
    except Exception as e:
        msg = f"Sir, I couldn't open the browser for the weather report: {e}"
        _log(msg, player)
        return msg

    msg = f"Showing the weather for {city}, {when}, sir."
    _log(msg, player)

    if session_memory:
        try:
            session_memory.set_last_search(query=search_query, response=msg)
        except Exception:
            pass

    return msg


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "weather_report",
    "description": (
        "Opens a live Google weather search for a city in the browser and speaks "
        "the result. Use it when the user asks about weather, rain, temperature, "
        "wind or a forecast for a place, optionally for a day other than today. "
        "For any other topic use web_search instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "city": {
                "type": "STRING",
                "description": "City name, e.g. 'Neumarkt in der Oberpfalz'."
            },
            "time": {
                "type": "STRING",
                "description": "Optional time frame such as 'today', 'tomorrow' "
                               "or 'this weekend'. Defaults to 'today'."
            }
        },
        "required": [
            "city"
        ]
    },
    "handler": weather_action,
}
