# Yahoo Fantasy Hockey advisor

Every morning it reads your roster, matchup, free agents and the NHL schedule,
has Claude work out the best moves, and pushes a checklist to your phone:

    ROSTER MOVES (do these first)
    [ ] Add Player X (EDM) — drop Player Y. 4 games left this week, hot on the PP.
    LINEUP FOR TODAY
    [ ] Player X: BN -> Util
    [ ] Player Z: C -> BN

You apply the moves in the Yahoo app. Yahoo's API is read-only, so the advisor never
changes your team. Every suggestion is still checked in code: slot eligibility and counts,
weekly add caps, your protected-players list, and adds limited to real available players.

## Setup

1. **Yahoo app** — https://developer.yahoo.com/apps/create/
   Redirect URI `https://localhost:8080`, Confidential Client, API permission
   **Fantasy Sports → Read** (nothing else). Save the Client ID and Client Secret.
2. **Apply for API access** — https://sports.yahoo.com/developer/access/
   Enter your Client ID; describe it as a personal, read-only tool for your own single
   league. Approval is a manual review; until it's granted, the API returns 401/403 errors.
3. **Python environment** (3.11+):
   `pip install -r requirements.txt`
4. **Log in once** — set your Client ID/Secret as environment variables (see below),
   then run `python auth_setup.py` and save the refresh token it prints.
5. **Anthropic API key** — https://console.anthropic.com (pay-as-you-go, separate from
   a Claude.ai subscription; a daily run costs a few cents).
6. **Phone notifications** — install the free **ntfy** app, subscribe to a hard-to-guess
   topic name, and put that name in `config.toml` under `[notify]`.
7. **Edit `config.toml`** — league_id if you're in several NHL leagues, protected
   players, strategy notes.
8. **Test locally** — set all environment variables and run `python agent.py`.
9. **Schedule it** — push to a **private** GitHub repo, add the secrets
   `ANTHROPIC_API_KEY`, `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`, `YAHOO_REFRESH_TOKEN`
   (Settings → Secrets and variables → Actions), then Actions → Run workflow to test.
   It then runs daily at 8:00 Pacific.

### Setting environment variables
macOS / Linux:
```
export YAHOO_CLIENT_ID="..." YAHOO_CLIENT_SECRET="..." YAHOO_REDIRECT_URI="https://localhost:8080"
export YAHOO_REFRESH_TOKEN="..." ANTHROPIC_API_KEY="..."
```
Windows PowerShell:
```
$env:YAHOO_CLIENT_ID="..."; $env:YAHOO_CLIENT_SECRET="..."; $env:YAHOO_REDIRECT_URI="https://localhost:8080"
$env:YAHOO_REFRESH_TOKEN="..."; $env:ANTHROPIC_API_KEY="..."
```

## Notes
- If a run prints "Unmapped NHL team abbreviation", add the pair to `YAHOO_TO_NHL` in `nhl.py`.
- In the first weeks of the season set `stats_type = "season"` in config.toml.
- GitHub pauses scheduled workflows after 60 days without repo activity; re-enable from the Actions tab.
