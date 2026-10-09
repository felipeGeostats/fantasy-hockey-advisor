"""Minimal read-only Yahoo Fantasy Sports API client for NHL leagues.

Uses Yahoo's XML responses (cleaner to parse than their JSON) and OAuth2
refresh tokens, so it can run unattended once auth_setup.py has been run.
"""
from __future__ import annotations

import base64
import os
import time
import xml.etree.ElementTree as ET

import requests

BASE = "https://fantasysports.yahooapis.com/fantasy/v2/"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"


class YahooError(RuntimeError):
    pass


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _t(el, path, default=None):
    found = el.find(path) if el is not None else None
    return found.text if found is not None and found.text is not None else default


def _stat_map(el, path):
    return {_t(s, "stat_id"): _t(s, "value") for s in el.findall(path)}


def parse_player(p: ET.Element) -> dict:
    return {
        "player_key": _t(p, "player_key"),
        "name": _t(p, "name/full"),
        "nhl_team": _t(p, "editorial_team_abbr"),
        "eligible": [e.text for e in p.findall("eligible_positions/position")],
        "position_type": _t(p, "position_type"),  # P = skater, G = goalie
        "selected": _t(p, "selected_position/position"),
        "status": _t(p, "status"),  # e.g. DTD, O, IR, IR-LT
        "injury_note": _t(p, "injury_note"),
        "stats_raw": _stat_map(p, "player_stats/stats/stat"),
    }


class Yahoo:
    def __init__(self, client_id, client_secret, refresh_token, redirect_uri=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.redirect_uri = redirect_uri or os.environ.get("YAHOO_REDIRECT_URI", "oob")
        self._access = None
        self._expires = 0.0

    # ---------- auth / transport ----------
    def _token(self) -> str:
        if self._access and time.time() < self._expires - 60:
            return self._access
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = requests.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": self.refresh_token,
                  "redirect_uri": self.redirect_uri},
            timeout=30,
        )
        if r.status_code >= 400:
            raise YahooError(f"Token refresh failed ({r.status_code}): {r.text[:300]}")
        j = r.json()
        self._access = j["access_token"]
        self._expires = time.time() + int(j.get("expires_in", 3600))
        self.refresh_token = j.get("refresh_token", self.refresh_token)
        return self._access

    def _req(self, method, path, body=None):
        headers = {"Authorization": f"Bearer {self._token()}"}
        if body:
            headers["Content-Type"] = "application/xml"
        r = requests.request(method, BASE + path, headers=headers, data=body, timeout=30)
        if r.status_code >= 400:
            raise YahooError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return _strip_ns(ET.fromstring(r.content)) if r.content.strip() else None

    def _get(self, path):
        return self._req("GET", path)

    # ---------- reads ----------
    def my_teams(self) -> list[dict]:
        """All of the logged-in user's teams in the current NHL season."""
        r = self._get("users;use_login=1/games;game_keys=nhl/teams")
        teams = []
        for g in r.findall("users/user/games/game"):
            for t in g.findall("teams/team"):
                teams.append({"team_key": _t(t, "team_key"), "name": _t(t, "name"),
                              "season": _t(g, "season")})
        return teams

    def league_settings(self, lk) -> dict:
        r = self._get(f"league/{lk}/settings")
        lg = r.find("league")
        s = lg.find("settings")
        positions = [{"position": _t(rp, "position"),
                      "count": int(_t(rp, "count", "0")),
                      "position_type": _t(rp, "position_type")}
                     for rp in s.findall("roster_positions/roster_position")]
        cats = {}
        for st in s.findall("stat_categories/stats/stat"):
            cats[_t(st, "stat_id")] = {
                "name": _t(st, "display_name"),
                "scored": _t(st, "is_only_display_stat", "0") != "1",
            }
        modifiers = {_t(m, "stat_id"): _t(m, "value")
                     for m in s.findall("stat_modifiers/stats/stat")}
        max_adds = _t(s, "max_weekly_adds") or _t(s, "max_adds")
        return {
            "league_name": _t(lg, "name"),
            "scoring_type": _t(lg, "scoring_type"),  # head, headpoint, point, roto
            "current_week": _t(lg, "current_week"),
            "roster_positions": positions,
            "categories": cats,
            "point_values": modifiers,
            "max_weekly_adds": int(max_adds) if max_adds and max_adds.isdigit() else None,
        }

    def team_meta(self, tk) -> dict:
        t = self._get(f"team/{tk}").find("team")
        return {"name": _t(t, "name"),
                "adds_this_week": int(_t(t, "roster_adds/value", "0") or 0)}

    def roster(self, tk, date) -> list[dict]:
        r = self._get(f"team/{tk}/roster;date={date}")
        return [parse_player(p) for p in r.findall("team/roster/players/player")]

    def player_stats(self, lk, keys, stats_type) -> dict:
        out = {}
        for i in range(0, len(keys), 25):
            chunk = ",".join(keys[i:i + 25])
            r = self._get(f"league/{lk}/players;player_keys={chunk}/stats;type={stats_type}")
            for p in r.findall("league/players/player"):
                out[_t(p, "player_key")] = _stat_map(p, "player_stats/stats/stat")
        return out

    def free_agents(self, lk, position, count, stats_type, status="FA") -> list[dict]:
        q = f"players;status={status};sort=AR;sort_type={stats_type};count={count}"
        if position:
            q += f";position={position}"
        r = self._get(f"league/{lk}/{q}/stats;type={stats_type}")
        players = [parse_player(p) for p in r.findall("league/players/player")]
        for p in players:
            p["availability"] = ("free agent (immediate)" if status == "FA"
                                 else "on waivers (adding = waiver claim, not immediate)")
        return players

    def my_matchup(self, lk, tk) -> dict | None:
        r = self._get(f"league/{lk}/scoreboard")
        for m in r.findall("league/scoreboard/matchups/matchup"):
            teams = m.findall("teams/team")
            if any(_t(t, "team_key") == tk for t in teams):
                return {
                    "week": _t(m, "week"),
                    "week_start": _t(m, "week_start"),
                    "week_end": _t(m, "week_end"),
                    "teams": [{"name": _t(t, "name"),
                               "is_me": _t(t, "team_key") == tk,
                               "points": _t(t, "team_points/total"),
                               "stats_raw": _stat_map(t, "team_stats/stats/stat")}
                              for t in teams],
                }
        return None
