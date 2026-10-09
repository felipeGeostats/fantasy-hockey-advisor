"""Daily advisor for a Yahoo Fantasy Hockey team.

Flow: gather league state (read-only Yahoo API) -> Claude proposes moves via
tools -> guardrails validate every proposal in code -> a checklist of moves is
pushed to your phone. You apply them in the Yahoo app; nothing is written to Yahoo.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tomllib
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import requests

import nhl
from yahoo import Yahoo

HERE = Path(__file__).parent
STARTING_POSITIONS = ["C", "LW", "RW", "D", "G"]


class ToolError(Exception):
    pass


def env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Missing environment variable: {name}")
    return val


# ------------------------------------------------------------------ context
def readable_stats(raw: dict, cats: dict) -> dict:
    return {cats[k]["name"]: v for k, v in raw.items() if k in cats and v not in (None, "-")}


def annotate(players, sched, today_iso, week_end, cats, known_teams, warnings):
    days = [d for d in sorted(sched) if d >= today_iso and (not week_end or d <= week_end)]
    for p in players:
        team = nhl.norm(p["nhl_team"])
        if known_teams and team not in known_teams:
            warnings.add(f"Unmapped NHL team abbreviation '{p['nhl_team']}' "
                         f"({p['name']}); add it to YAHOO_TO_NHL in nhl.py")
        p["game_today"] = sched.get(today_iso, {}).get(team)
        p["games_left_this_week"] = sum(1 for d in days if team in sched[d])
        p["stats"] = readable_stats(p.pop("stats_raw", {}), cats)
    return players


def gather(y: Yahoo, cfg: dict, today: dt.date) -> dict:
    lid = str(cfg["league"].get("league_id", "")).strip()
    teams = y.my_teams()
    if lid:
        teams = [t for t in teams if f".l.{lid}." in t["team_key"]]
    if not teams:
        sys.exit("No current-season NHL team found for this Yahoo account / league_id.")
    if len(teams) > 1:
        names = ", ".join(f"{t['name']} ({t['team_key']})" for t in teams)
        sys.exit(f"Several NHL teams found; set league.league_id in config.toml. {names}")

    tk = teams[0]["team_key"]
    lk = tk.rsplit(".t.", 1)[0]
    stats_type = cfg["agent"].get("stats_type", "lastmonth")
    today_iso = today.isoformat()

    settings = y.league_settings(lk)
    cats = settings["categories"]
    meta = y.team_meta(tk)
    matchup = y.my_matchup(lk, tk)
    sched = nhl.week_schedule(today)
    known = {t for day in sched.values() for t in day}
    week_end = matchup["week_end"] if matchup else None
    warnings: set[str] = set()

    roster = y.roster(tk, today_iso)
    stats = y.player_stats(lk, [p["player_key"] for p in roster], stats_type)
    for p in roster:
        p["stats_raw"] = stats.get(p["player_key"], {})
    annotate(roster, sched, today_iso, week_end, cats, known, warnings)

    n = int(cfg["agent"].get("free_agents_per_position", 8))
    league_positions = {rp["position"] for rp in settings["roster_positions"]}
    fas: dict[str, dict] = {}
    for pos in [p for p in STARTING_POSITIONS if p in league_positions]:
        for p in y.free_agents(lk, pos, n, stats_type, "FA"):
            fas.setdefault(p["player_key"], p)
    for p in y.free_agents(lk, None, n, stats_type, "W"):
        fas.setdefault(p["player_key"], p)
    annotate(list(fas.values()), sched, today_iso, week_end, cats, known, warnings)

    if matchup:
        for t in matchup["teams"]:
            t["stats"] = readable_stats(t.pop("stats_raw", {}), cats)

    return {"team_key": tk, "league_key": lk, "team_name": meta["name"],
            "adds_this_week": meta["adds_this_week"], "settings": settings,
            "matchup": matchup, "roster": roster, "free_agents": list(fas.values()),
            "schedule": sched, "stats_type": stats_type, "warnings": sorted(warnings)}


# ------------------------------------------------------------------ tools
TOOLS = [
    {
        "name": "recommend_lineup",
        "description": (
            "Recommend lineup slot changes for TODAY. Include only players whose "
            "slot changes; swaps go in one call. Use 'BN' for bench, and IR/IR+/NA slots only "
            "for players listing them as eligible. Validated against eligibility and slot counts."),
        "input_schema": {
            "type": "object",
            "properties": {
                "assignments": {
                    "type": "array",
                    "items": {"type": "object",
                              "properties": {"player_key": {"type": "string"},
                                             "position": {"type": "string"}},
                              "required": ["player_key", "position"]}},
                "reason": {"type": "string"}},
            "required": ["assignments", "reason"]},
    },
    {
        "name": "recommend_add_drop",
        "description": (
            "Recommend adding one listed free agent / waiver player, optionally dropping one rostered player. "
            "Subject to weekly and per-run add caps and the owner's protected list. "
            "A waiver player would be a claim and would not join the roster today."),
        "input_schema": {
            "type": "object",
            "properties": {"add_player_key": {"type": "string"},
                           "drop_player_key": {"type": "string"},
                           "reason": {"type": "string"}},
            "required": ["add_player_key", "reason"]},
    },
    {
        "name": "finish",
        "description": ("Call exactly once when done. Short notes for the owner: key reasoning, "
                        "uncertainties (e.g. goalie starts), anything to watch. The move checklist is added automatically."),
        "input_schema": {"type": "object",
                         "properties": {"summary": {"type": "string"}},
                         "required": ["summary"]},
    },
]

SYSTEM = """You are the daily advisor for the owner's Yahoo Fantasy Hockey team. You run each morning.
You cannot change the team yourself: record each move with the tools, and the owner applies
them by hand in the Yahoo app from the checklist they receive. Recommend only moves worth
the owner's time, and make the full set consistent (recommend add/drops before lineup moves).

Daily priorities, in order:
1. Never leave a starting slot empty or filled by a player without a game today if an eligible
   bench player has one. Prefer players with games today in every active slot.
2. Bench players who are out (status O, IR, IR-LT, IR-NR). Use judgment on DTD.
3. Move IR-eligible players into IR/IR+ slots when that frees a useful roster spot.
4. Goalies: start goalies whose team plays today. Starting goalies aren't confirmed in this
   data, so favour each team's established No. 1 and note the uncertainty in the summary.
5. Add/drops are optional. Make one only for a clear upgrade given the league's scoring, the
   current matchup, and games left this week. Don't churn: never drop an established, valuable
   player for a short-term stream, and never drop a protected player.
A player added from free agency can be slotted into today's lineup; reference them by their
player_key in recommend_lineup. Waiver claims can't play today.
Rejected tool calls explain why; fix and retry, or skip the move.
Keep the final notes short and plain.
"""


class Run:
    """Holds a simulated copy of the team; tools validate and record recommendations only."""

    def __init__(self, cfg: dict, ctx: dict, today: dt.date):
        self.cfg, self.ctx = cfg, ctx
        self.today_iso = today.isoformat()
        self.roster = {p["player_key"]: dict(p) for p in ctx["roster"]}
        self.original_slot = {k: p["selected"] for k, p in self.roster.items()}
        self.fa = {p["player_key"]: p for p in ctx["free_agents"]}
        self.slots = {rp["position"]: rp["count"] for rp in ctx["settings"]["roster_positions"]
                      if rp["position"] != "BN"}
        moves = cfg["moves"]
        league_cap = ctx["settings"]["max_weekly_adds"]
        own_cap = int(moves.get("weekly_add_cap", 99))
        self.weekly_cap = min(own_cap, league_cap) if league_cap else own_cap
        self.protected = {n.strip().lower() for n in moves.get("protected_players", [])}
        self.transactions: list[str] = []
        self.summary: str | None = None

    def handle(self, name: str, args: dict) -> tuple[str, bool]:
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            return f"Unknown tool {name}", True
        try:
            return fn(args), False
        except ToolError as e:
            return f"REJECTED: {e}", True

    # --- lineup
    def _eligible(self, p, pos):
        el = set(p["eligible"])
        if pos == "BN" or pos in el:
            return True
        if pos == "Util":
            return p["position_type"] == "P"
        if pos == "F":
            return bool(el & {"C", "LW", "RW"})
        return False

    def tool_recommend_lineup(self, args):
        new = {k: p["selected"] for k, p in self.roster.items()}
        for a in args.get("assignments", []):
            k, pos = a["player_key"], a["position"]
            p = self.roster.get(k)
            if not p:
                raise ToolError(f"{k} is not on the roster (after recommended adds/drops)")
            if pos != "BN" and pos not in self.slots:
                raise ToolError(f"League has no '{pos}' slot. Slots: {sorted(self.slots)}")
            if not self._eligible(p, pos):
                raise ToolError(f"{p['name']} is not eligible for {pos} (eligible: {p['eligible']})")
            new[k] = pos
        counts = Counter(v for v in new.values() if v != "BN")
        over = {pos: c for pos, c in counts.items() if c > self.slots.get(pos, 0)}
        if over:
            raise ToolError(f"Too many players in {over}; limits are {self.slots}")
        changed = sum(1 for k, pos in new.items() if self.roster[k]["selected"] != pos)
        for k, pos in new.items():
            self.roster[k]["selected"] = pos
        return f"Recorded {changed} lineup change(s)." if changed else "No changes needed."

    # --- add/drop
    def tool_recommend_add_drop(self, args):
        moves = self.cfg["moves"]
        if not moves.get("suggest_add_drops", True):
            raise ToolError("Add/drop suggestions are disabled in config.")
        if len(self.transactions) >= int(moves.get("max_adds_per_run", 1)):
            raise ToolError("Per-run add limit reached.")
        if self.ctx["adds_this_week"] + len(self.transactions) >= self.weekly_cap:
            raise ToolError(f"Weekly add cap reached ({self.weekly_cap}).")
        add_key, drop_key = args["add_player_key"], args.get("drop_player_key") or None
        add = self.fa.get(add_key)
        if not add:
            raise ToolError("Add target must be one of the listed free agents/waiver players.")
        drop = None
        if drop_key:
            drop = self.roster.get(drop_key)
            if not drop:
                raise ToolError(f"{drop_key} is not on the roster")
            if drop["name"].lower() in self.protected:
                raise ToolError(f"{drop['name']} is protected and can't be dropped.")

        waiver = "waiver" in add["availability"]
        line = (("Claim " if waiver else "Add ") + f"{add['name']} ({add['nhl_team']})"
                + (f" — drop {drop['name']}" if drop else "") + f". {args.get('reason', '')}".rstrip())
        self.transactions.append(line)
        if waiver:
            return f"Recorded waiver claim: {line} Roster unchanged for today."
        if drop:
            self.roster.pop(drop_key)
        self.roster[add_key] = dict(add, selected="BN")
        return (f"Recorded: {line} {add['name']} is now on the simulated roster (on BN); "
                f"slot them with recommend_lineup if they should play today.")

    def tool_finish(self, args):
        self.summary = args.get("summary", "")
        return "Logged."

    # --- output
    def checklist(self) -> str:
        out = []
        if self.transactions:
            out.append("ROSTER MOVES (do these first)")
            out += [f"[ ] {t}" for t in self.transactions]
        lineup = []
        for k, p in self.roster.items():
            before = self.original_slot.get(k, "BN")
            if p["selected"] != before:
                lineup.append(f"[ ] {p['name']}: {before} -> {p['selected']}")
        if lineup:
            out.append(("\n" if out else "") + "LINEUP FOR TODAY")
            out += sorted(lineup)
        return "\n".join(out) if out else "No moves today — your lineup is already set."


# ------------------------------------------------------------------ main
def build_prompt(ctx, cfg, run: Run, today):
    s = ctx["settings"]
    payload = {
        "date": today.isoformat(),
        "team": ctx["team_name"],
        "league": {"name": s["league_name"], "scoring_type": s["scoring_type"],
                   "scored_categories": [c["name"] for c in s["categories"].values() if c["scored"]],
                   "point_values": {s["categories"].get(k, {}).get("name", k): v
                                    for k, v in s["point_values"].items()} or None,
                   "slots": run.slots},
        "limits": {"adds_used_this_week": ctx["adds_this_week"], "weekly_add_cap": run.weekly_cap,
                   "max_adds_this_run": cfg["moves"].get("max_adds_per_run", 1),
                   "add_drop_suggestions": cfg["moves"].get("suggest_add_drops", True),
                   "protected_players": cfg["moves"].get("protected_players", [])},
        "stats_window": ctx["stats_type"],
        "matchup": ctx["matchup"],
        "roster": [{k: p.get(k) for k in ("player_key", "name", "nhl_team", "eligible", "selected",
                                          "status", "injury_note", "game_today",
                                          "games_left_this_week", "stats")} for p in ctx["roster"]],
        "available_players": [{k: p.get(k) for k in ("player_key", "name", "nhl_team", "eligible",
                                                      "status", "availability", "game_today",
                                                      "games_left_this_week", "stats")}
                              for p in ctx["free_agents"]],
    }
    strategy = cfg["moves"].get("strategy", "").strip()
    return (("Owner's strategy notes:\n" + strategy + "\n\n" if strategy else "")
            + "Today's league state (JSON):\n" + json.dumps(payload, separators=(",", ":")))


def notify(cfg, title, text):
    topic = cfg.get("notify", {}).get("ntfy_topic", "").strip()
    if not topic:
        return
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=text.encode(),
                      headers={"Title": title}, timeout=15)
    except requests.RequestException as e:
        print(f"Notification failed: {e}")


def main():
    with open(HERE / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    today = dt.datetime.now(ZoneInfo(cfg["league"].get("timezone", "America/Vancouver"))).date()

    y = Yahoo(env("YAHOO_CLIENT_ID"), env("YAHOO_CLIENT_SECRET"), env("YAHOO_REFRESH_TOKEN"))
    ctx = gather(y, cfg, today)
    run = Run(cfg, ctx, today)
    for w in ctx["warnings"]:
        print("WARNING:", w)

    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    messages = [{"role": "user", "content": build_prompt(ctx, cfg, run, today)}]
    final_text = ""
    for _ in range(int(cfg["agent"].get("max_turns", 20))):
        resp = client.messages.create(model=cfg["agent"]["model"], max_tokens=4000,
                                      system=SYSTEM, tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        final_text = "".join(b.text for b in resp.content if b.type == "text") or final_text
        if resp.stop_reason != "tool_use":
            break
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                out, is_err = run.handle(block.name, block.input)
                print(f"[{block.name}] {json.dumps(block.input)[:300]}\n  -> {out[:300]}")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": out, "is_error": is_err})
        messages.append({"role": "user", "content": results})
        if run.summary is not None:
            break

    notes = (run.summary or final_text).strip()
    body = (run.checklist()
            + ("\n\nNOTES\n" + notes if notes else "")
            + ("\n\nWarnings:\n- " + "\n- ".join(ctx["warnings"]) if ctx["warnings"] else ""))
    print("\n" + body)
    notify(cfg, f"{ctx['team_name']} — {today.isoformat()}", body)


if __name__ == "__main__":
    main()
