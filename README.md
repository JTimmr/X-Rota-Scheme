# X rota bot

A Discord scheduling bot for a team X rota. It stores scheduled posts in SQLite,
optionally publishes them through X, archives live slots, and sends claim/reminder
notifications.

## Setup and deployment

1. Create a Discord application/bot, invite it to the target server, and enable
   the Message Content and Server Members privileged intents.
2. Copy `.env.example` to `.env` and replace placeholder values. Keep real
   tokens and API credentials only in `.env`; it is git-ignored.
3. Start the bot:

   ```sh
   docker compose up --build -d
   ```

The retained `.env copy.example` is a local/legacy channel template; its channel
IDs are not secrets, but new deployments should start from the sanitized
`.env.example`.

The container uses the validated Python 3.12.14 slim image, installs `ffmpeg`
(including `ffprobe`) for MP4 validation, and persists SQLite/media data in
`./data`. Direct runtime dependencies are pinned in `requirements.txt`; Docker
uses `requirements.lock`, generated from the clean validated environment, to
pin runtime transitives too. Neither file contains test/lint tools. To run
without Docker, install either file plus `ffmpeg`/`ffprobe`, set the same
environment variables, and run `python src/bot.py`.

This provides practical version stability, not byte-for-byte reproducibility.
The image deliberately remains cross-platform and receives normal Debian
repository and `ffmpeg` security updates; platform digests and apt snapshots are
not frozen.

Required environment variables:

- `DISCORD_TOKEN`
- `DISCORD_GUILD_ID`
- `SCHEDULED_CHANNEL_ID`
- `ARCHIVE_CHANNEL_ID`
- `REMINDERS_CHANNEL_ID`

Optional environment variables:

- `ROTA_ALERT_ROLE_ID`: role used for required-unclaimed and daily rota alerts.
  The role must be mentionable, or the bot must have Discord's **Mention
  @everyone, @here, and All Roles** permission. If absent, invalid, or
  unresolved, alerts fall back to recently active users; users marked
  unavailable for a post are excluded.
- `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, and
  `X_ACCESS_TOKEN_SECRET`: all four enable automatic X posting. If any are
  absent, X posting is disabled.
- `X_LIVE_POST_LINK_CHANNEL_ID_1` and
  `X_LIVE_POST_LINK_CHANNEL_ID_2`: channels that receive successful x.com links.

## Scheduling and claims

The scheduling-channel panel opens one composer for required text and one
optional JPG/JPEG, PNG, WebP, GIF, or MP4. `/schedule` is the fallback and uses
the same time picker; set `post_to_x` off for a manual-X slot. The schedule is
rebuilt after changes, with posts ordered so the soonest is near the bottom and
the panel sent last. Discord has no native fixed-bottom message, so new chat
activity can still appear below the panel until the next schedule refresh.

Quick selection offers the next 25 local dates, hours 06:00-22:00, and
00/15/30/45 minutes. **Enter exact date/time** accepts `YYYY-MM-DD` plus either
`HHMM` or `HH:MM`, including times outside the quick range. Both create and edit
flows require a future time no more than 60 local calendar days away and reject
invalid daylight-saving wall times.

Claims are optional by default, including scheduled posts present during the
one-time upgrade migration. **Require a claimer** enables team notifications
when an unclaimed post enters the four-hour window. Inside and at the final
15-minute boundary, only the 15-minute alert applies; go-live retains its own
notification. Claimers receive the pre-live and go-live notifications. **Not
available** removes that user from the post's fallback alert target. Four-hour
and 15-minute deliveries are persisted in SQLite, so
restarts and claim/require toggles do not resend an alert kind that was already
recorded. Delivery is recorded only after Discord accepts every message in the
batch; as with any Discord-plus-SQLite workflow, a process crash in the narrow
gap between send and record can still produce a duplicate. Skipped optional
alerts and attempts with no target are not recorded.

Content and media can be changed before go-live. Replacement media is fully
validated before the database switches away from the old file; failed
validation leaves the existing attachment intact.

## Content, media, and X behavior

The Discord composer accepts up to 4,000 Unicode characters. Content above 280
characters is shown as an accepted **X Premium long post**; it is not blocked or
split into threads. The direct X client has a conservative 25,000 Unicode
code-point guard, while normal Discord scheduling is already capped at 4,000.
This guard is defensive, not a full implementation of X's weighted-length
rules; X can still reject text under its own validation.

Media limits:

- JPG/JPEG, PNG, or WebP: at most 5 MiB and 40,000,000 decoded pixels.
- GIF: at most 15 MiB, 1280x1080, 350 frames, and a 300,000,000 total-pixel
  budget.
- MP4: at most 512 MiB, 0.5-140 seconds, H.264 video, optional AAC audio,
  at most 60 fps and 1920x1920, with a 1:3-3:1 display aspect ratio.

File extension, MIME metadata, signature, and decoded/probed contents must
agree. Validation errors block scheduling and explain what to re-export.

At go-live, automatic slots make one X attempt. A media upload or processing
failure never falls back to a text-only post. There is no automatic scheduler
retry: failures are logged and identified in archive/reminder messages so the
team can publish manually. If `create_tweet` raises or returns no usable ID, the
outcome is treated as unknown and operators must check X before retrying because
the request may have succeeded. Manual-X slots never call X or broadcast an
x.com link, but they keep the same archive and reminder cadence.

## Verification

The test suite uses the standard library runner:

```sh
python -m unittest discover -s tests -v
python -m compileall -q src tests
```
