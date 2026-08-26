# HoYoLAB Raffle Discord Bot

Python bot that polls official HoYoLAB news feeds for Genshin Impact, Honkai: Star Rail, Zenless Zone Zero, Honkai Impact 3rd, and Honkai: Nexus Anima, then posts raffle/giveaway alerts to Discord.

It uses **discord.py** for the Discord gateway and slash commands, and **httpx** to poll HoYoLAB’s public news API every 5–10 minutes (with jitter).

## Features

- Watches official notices, events, and info posts per game
- Filters for community merch raffles without treating in-game events, store launches, or patch notes as hits
- Stores seen post IDs in SQLite so alerts are never duplicated
- Seeds existing posts on first run (no historical dump)
- Rich embeds with game name, logo, snippet, author, period, and HoYoLAB link
- Slash commands: `/set_channel`, `/check_now`, `/sample`, `/status`

## Requirements

- Python 3.10+
- A Discord application with a bot user
- Network access to `bbs-api-os.hoyolab.com` and Discord

## Discord application setup

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create an application.
2. Open **Bot** and click **Add Bot** (or use the default bot user).
3. Reset / copy the **Token**. This is `DISCORD_TOKEN`.
4. Under **Privileged Gateway Intents**:
   - **Message Content Intent** is **not** required (this bot is slash-command only).
   - **Server Members Intent** is **not** required.
   - Default intents (guilds) are enough.
5. Open **OAuth2 → URL Generator**:
   - Scopes: `bot` and `applications.commands`
   - Bot permissions: **View Channel**, **Send Messages**, **Embed Links**, **Use Application Commands**
6. Invite the bot with the generated URL and pick your server.

Optional: copy a channel ID (Discord Settings → Advanced → Developer Mode, then right-click the channel → Copy Channel ID) into `DISCORD_CHANNEL_ID`. You can also skip this and run `/set_channel` after the bot is online.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```env
DISCORD_TOKEN=your-bot-token-here
DISCORD_CHANNEL_ID=123456789012345678
```

Optional values:

| Variable | Purpose |
| --- | --- |
| `DISCORD_WEBHOOK_URL` | Also post alerts to a webhook |
| `DISCORD_GUILD_ID` | Sync slash commands to one server immediately (global sync can take up to an hour) |
| `CHECK_INTERVAL_SECONDS` | Base poll interval (default `420`) |
| `CHECK_JITTER_SECONDS` | Random ± jitter (default `90`) |
| `HOYOLAB_LANG` | `X-Rpc-Language` header (default `en-us`) |
| `RAFFLE_KEYWORDS` | Extra comma-separated keywords |

Start the bot:

```bash
python bot.py
```

In Discord, use `/set_channel` if you did not set `DISCORD_CHANNEL_ID`, then `/status` to confirm the watcher is running.

## Docker

```bash
cp .env.example .env
# set DISCORD_TOKEN (and optionally DISCORD_CHANNEL_ID) in .env
docker compose up -d --build
```

SQLite is stored in `./data` on the host so seen posts survive container rebuilds.

Logs:

```bash
docker compose logs -f bot
```

## Commands

| Command | Description |
| --- | --- |
| `/set_channel [channel]` | Set the alert channel (requires **Manage Channels**) |
| `/check_now` | Run one HoYoLAB scan immediately |
| `/sample` | Fetch a random recent official post and send it as a preview embed |
| `/status` | Uptime, tracked post count, interval, last result, alert channel |

## How matching works

Posts are fetched from HoYoLAB’s public endpoints:

- `GET https://bbs-api-os.hoyolab.com/community/post/wapi/getNewsList`
- `GET https://bbs-api-os.hoyolab.com/community/post/wapi/getPostFull`

Game IDs: Honkai Impact 3rd `1`, Genshin Impact `2`, Honkai: Star Rail `6`, Zenless Zone Zero `8`, Honkai: Nexus Anima `9`.

A post alerts only when it is official **and** looks like a community merch raffle:

- Raffle-intent phrases: raffle, giveaway, lucky draw, sweepstakes, prize event, leave a comment, comment to enter, leave a reply
- Or **how to participate** together with a physical reward term (plush, keychain, acrylic, figure, merch, vinyl, …)
- Matching uses word boundaries, so `win` inside `window` / `following` / `Wind` does not count
- Store launches, Event Warps, version update details, and maintenance/compensation posts are excluded

Bare `merch`, `prize`, `winner`, or in-game currency (primogems, stellar jade, …) is **not** enough on its own. The bare word `event` is also not enough, so routine patch notes are not treated as raffles.

## Project layout

```
bot.py                    # discord.py entrypoint
config.py                 # env + game metadata
cogs/hoyolab_watcher.py   # poll loop, embeds, slash commands
utils/api.py              # httpx HoYoLAB client
utils/db.py               # SQLite seen-post cache
utils/filters.py          # keyword + expiration matching
```

## Notes

- First successful poll **seeds** every currently listed post ID without sending alerts. Only posts that appear after that are eligible.
- If no channel (and no webhook) is configured, matching posts are left untracked so they can be sent on the next scan after you run `/set_channel`.
- HoYoLAB’s community API is unofficial. Timeouts, HTTP 429s, and payload shape changes are logged and skipped rather than crashing the watcher.
