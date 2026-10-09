"""NHL schedule lookups via the public api-web.nhle.com endpoint."""
from __future__ import annotations

import datetime as dt

import requests

SCHEDULE = "https://api-web.nhle.com/v1/schedule/{date}"

# Yahoo's editorial team abbreviations don't always match the NHL's.
# Unknown abbreviations are reported by agent.py, so extend this if a warning shows up.
YAHOO_TO_NHL = {
    "LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL", "MON": "MTL",
    "WAS": "WSH", "CLS": "CBJ", "CLB": "CBJ", "CAL": "CGY", "NAS": "NSH",
    "VEG": "VGK", "LV": "VGK", "ANH": "ANA", "WIN": "WPG", "UTH": "UTA", "UTAH": "UTA",
}


def norm(abbr: str | None) -> str:
    a = (abbr or "").upper()
    return YAHOO_TO_NHL.get(a, a)


def week_schedule(start: dt.date) -> dict[str, dict[str, str]]:
    """{ 'YYYY-MM-DD': {team_abbrev: 'vs BOS' | '@BOS'} } for the 7 days from `start`.

    Only regular-season and playoff games are kept (preseason doesn't count in Yahoo).
    """
    r = requests.get(SCHEDULE.format(date=start.isoformat()), timeout=20)
    r.raise_for_status()
    out: dict[str, dict[str, str]] = {}
    for day in r.json().get("gameWeek", []):
        games = {}
        for g in day.get("games", []):
            if g.get("gameType") not in (2, 3):
                continue
            away, home = g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"]
            games[away] = f"@{home}"
            games[home] = f"vs {away}"
        out[day["date"]] = games
    return out
