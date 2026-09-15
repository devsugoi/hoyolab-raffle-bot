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

- Raffle-intent phrases: raffle, giveaway, lucky draw, sweepstakes, prize event, leave a comment, comment to enter, comment for a chance to win, leave a reply
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

## Raffle result reminders

Keyword matching remains the first layer. A matched raffle that was successfully
announced is saved with its full content, then Gemini extracts dates in a separate
background worker. Gemini does not decide whether to send the original raffle alert.
The integration adapts the structured-call and transient-retry approach from the
companion Discord reminder bot using this project's existing `httpx` dependency.

Set `GEMINI_API_KEY` in `.env` using your own Google AI Studio key, then restart the
bot. No credentials are copied from another project. `GEMINI_MODEL` defaults to
`gemini-3.5-flash`; select a model available to your account that supports structured
outputs. Check [Google's model pricing](https://ai.google.dev/gemini-api/docs/pricing)
and [your rate limits](https://ai.google.dev/gemini-api/docs/rate-limits). Free-tier
availability and quotas are model/project dependent; small output does not eliminate
request quotas or input/thinking tokens.

| Variable | Default | Behavior |
| --- | --- | --- |
| `GEMINI_API_KEY` | Empty | Missing key defers extraction; original alerts still work |
| `GEMINI_API_KEY_2` … `_20` | Empty | Ordered backup keys for quota/auth failures |
| `GEMINI_FALLBACK_MODEL`, `_2` … `_20` | Empty | Ordered model fallbacks for 404/repeated 5xx |
| `GEMINI_MODEL` | `gemini-3.5-flash` | Structured date extraction model |
| `GEMINI_MIN_INTERVAL_SECONDS` | `15` | Request spacing per process |
| `REMINDER_TIMEZONE` | `Asia/Singapore` | IANA timezone when the post omits one |
| `REMINDER_DEFAULT_HOUR` | `18` | Local hour for dates with no clock time |
| `REMINDER_POLL_SECONDS` | `60` | Extraction/delivery idle polling interval |
| `HISTORICAL_CATCHUP_DAYS` | `30` | Historical reminders older than this are expired |

Winner announcement dates take precedence over event end dates. For "within three
days after the event ends", the reminder uses the three-day upper bound. Dates without
years and relative weekdays are resolved against the **post's publication date**,
never the backfill date. Unknown or ambiguous dates produce no reminder. Python checks
ISO syntax, impossible dates, chronological order, and plausibility against publication.
Date-only reminders use the configured evening hour, which may be before an event
actually closes if its closing time was not stated. Explicit times/offsets are preserved;
Discord renders timestamps in each reader's local timezone.

SQLite automatically adds a `raffle_reminders` table and two indexes on bot startup or
backfill. No destructive migration or separate database is required. Existing tables
and historical records are retained. Each post ID has one persistent record, including
its content, normalized dates, status, retry time, delivery destination and sent time.
Completed `no_date`, `invalid`, `expired`, and `sent` records are not reanalyzed by backfill.
Malformed analysis is retried up to three times, one hour apart, then marked `invalid`.
Temporary API errors leave extraction queued. Like the companion reminder bot, a key
returning HTTP 429 is cooled down for one hour and the next configured key is tried.
HTTP 401/403 also switches keys. A model returning 404 or still returning 5xx after two
paced retries is cooled down for five minutes and the next model is tried on the same
key. Both sets preserve configuration order, ignore blanks/duplicates, and allow gaps
in numbering. Keys can be configured using numbered entries even if the primary is empty.

All calls, including fallback attempts, share the same request pacing and lock. If all
options are cooling down, work stays queued until an option becomes available; cooled
options are not immediately retried. Other HTTP errors (such as malformed requests)
defer extraction for an hour without trying every key/model. Transport errors defer for
one minute. Cooldowns are in memory per process; deferred work and its retry time are
persisted in SQLite. Long fallback chains are bounded by the existing four-minute
analysis timeout and resume after a one-minute deferral. Key values are never logged.
Google quotas are per project, so additional keys in the same project do not provide
additional quota. Configure only keys/models you intend the bot to use.

Example (fill keys yourself; blank fallbacks remain disabled):

```env
GEMINI_API_KEY=your-primary-key
GEMINI_API_KEY_2=your-backup-key
GEMINI_MODEL=gemini-3.5-flash
GEMINI_FALLBACK_MODEL=gemini-3.5-flash-lite
GEMINI_FALLBACK_MODEL_2=
```

Requests have a 4,096 output-token cap;
posts over 60,000 body characters are rejected rather than silently truncated.

Independent workers ensure HoYoLAB outages and AI quota failures do not prevent due
reminders from being sent. Overdue scheduled reminders are sent after restart, even if
an ordinary scheduled reminder is now older than the historical catch-up window.
Reminders go to the configured channel, or to the webhook if no channel is configured.
Unlike original alerts, a reminder uses **one** destination. Channel destinations are
pinned before first delivery; webhook credentials stay in configuration, not SQLite.

### One-time historical backfill

After setting the Gemini key, run from the project directory:

```bash
.venv/bin/python backfill_raffle_reminders.py
```

Docker equivalent, after rebuilding the image with these changes:

```bash
docker compose run --rm bot python backfill_raffle_reminders.py
```

Use `--limit 10` to bound the number of historical posts fetched in a run. Backfill
selects only `seen_posts.notified=1`; seeded/rejected records cannot reliably identify
raffles and are excluded. Existing reminder records are skipped before any fetch or AI
call. Old records contain no bodies or dates, so the original post is fetched again
through the existing HoYoLAB client and its retries. Missing/deleted posts are logged
as failures (exit code 1) and can be retried by rerunning the command.

Backfill queues recovered content and runs the same sequential extraction worker used
for new raffles. Future results are scheduled; results within the catch-up window become
due; older results are persisted as `expired`. The CLI never connects to Discord: the
running bot sends due catch-up reminders, identified as recovered older raffles. If the
bot is stopped, it sends them after startup. When all usable keys/models fail, the CLI stops extraction;
remaining work persists for the bot to resume. Repeating backfill never resets sent or
completed records. The normal startup does not scan historical `seen_posts`.

Run only one backfill process at a time to keep requests low. SQLite claims prevent
concurrent processing of the same queued raffle, but pacing is per process: running the
bot and backfill together can each issue one AI request at a time. Stop the bot during
backfill if you need a single request stream for a tight free-tier quota.

### Delivery guarantees and limitations

Atomic SQLite claims with five-minute leases prevent overlapping workers from sending
the same record concurrently; failed delivery retries after five minutes. Sent records
are never reset. A process interrupted before sending releases its work through lease
expiry. Discord and SQLite cannot commit atomically: a crash or ambiguous network error
after Discord accepts a message but before SQLite records success can cause a duplicate
on retry. Delivery favors retrying over silently losing a reminder; strict exactly-once
delivery is not promised.

The existing watcher skips known post IDs and does not detect edits. Unsent reminder
rescheduling on author edits is a future improvement; sent reminders remain completed.
Image-only dates are not read. AI extraction is best effort even with validation. No
historical Discord messages are scanned, and sample previews never create reminders.

### Tests

The SQLite runtime must be 3.35+ (for atomic `RETURNING` claims); the Docker image
provides this. IANA timezone data must be installed on the host. On systems without
it, such as Windows, install the `tzdata` Python package.

No additional test dependencies are required:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q bot.py config.py cogs utils backfill_raffle_reminders.py tests
```

Tests use temporary SQLite databases and mocked Gemini/HoYoLAB/Discord responses.
Natural-language fixtures verify the structured request/response contract and downstream
scheduling; they do not measure live Gemini interpretation accuracy. No production data
is mutated and no real messages are sent. There is no configured lint or type-check tool.
