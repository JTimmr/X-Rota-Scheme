# Scheduler lifecycle and archive metadata

The bot never uses a database status named `archived`. A due slot is flipped from `scheduled` to `live`, the schedule-channel message is deleted, and a new message is sent to the Discord archive channel. "Archived" in day-to-day use means that archive-channel message plus the `live` SQLite row.

## SQLite `posts` row

Created when scheduled:

| Column | Meaning |
| --- | --- |
| `id` | Internal primary key |
| `discord_message_id` | Schedule-channel message ID |
| `content` | Post text |
| `scheduled_at` | Unix time the slot should fire |
| `created_by` | Discord user ID of the scheduler |
| `status` | `scheduled` |
| `created_at` | Unix time the row was inserted |
| `image_path` | Optional local media path |
| `tweet_url` | Null until a successful automatic X post |
| `skip_unclaimed_pings` | Default `1` (claimer optional) |
| `post_to_x` | `1` automatic X, `0` manual X |
| `post_to_discord` | `1` queue successful X links for live-link channels, `0` skip them |
| `discord_delay_minutes` | Delay after confirmed X publication; default `0` |
| `x_published_at` | Null until X returns a confirmed successful post ID |

Related tables that can exist before go-live:

- `claims` — who claimed the slot (`post_id`, `user_id`, `created_at`)
- `unavailable` — who opted out of fallback pings
- `post_alert_deliveries` — whether the 4h unclaimed and/or 15m pre-live alerts already sent
- `post_discord_deliveries` — one persisted live-link delivery per configured channel, including due time, attempts, Discord message ID, and delivery time

## What changes at go-live

`transition_due_post_to_live` does two writes on the `posts` row:

1. `status` = `'live'`
2. `discord_message_id` = `'live_{post_id}'`

That second write **replaces** the original schedule-channel message ID with a synthetic placeholder. The live archive Discord message ID is **not** stored.

If automatic X succeeds, a second transaction sets `tweet_url` to `https://x.com/i/web/status/{id}`, records `x_published_at`, and queues enabled Discord live-link deliveries. Each delivery is due at `x_published_at + discord_delay_minutes`. Failure and unknown outcomes leave `tweet_url` and `x_published_at` null. Manual-X slots never set them.

Nothing else on the row is updated. In particular the bot does **not** store:

- X numeric post ID as its own column (only the URL, and only on success)
- X outcome (`success` / `failed` / `unknown`)
- whether media uploaded
- archive-channel message ID
- impressions, likes, replies, or any later metrics
- topic, objective, experiment arm, or price/Discord context

Claims, unavailable rows, and alert-delivery rows are left in place. They are not copied into the archive message.

## What the Discord archive message contains

This is the human-readable archive, not a database snapshot. Text roughly includes:

- heading: manual X, went live on X, outcome unknown, or auto-post failed
- Discord timestamp of **when the archive message was sent** (`live_ts`), which is wall-clock go-live, not `scheduled_at`
- originally scheduled time (`scheduled_at`)
- scheduler mention (`created_by`)
- `tweet_url` on success only
- an embed whose description is `content` (truncated at Discord's 4,096-character embed limit)
- the media file re-uploaded from `image_path` if the file still exists

The archive message does **not** list claimers, unavailable users, `skip_unclaimed_pings`, `post_to_x` as a field (except indirectly via the heading), alert history, or SQLite `id`.

## Side effects that are not metadata

Successful automatic posts queue the bare URL for each configured live-link channel unless `post_to_discord` is off. Delivery rows retain the target channel, due time, attempt count, successful send time, and returned Discord message ID. Failed sends remain pending for the next scheduler tick.

Reminders ping claimers (or, for required-claimer slots with nobody claimed, the team) with outcome text and the same content embed. Delivery of *go-live* reminders is not written to `post_alert_deliveries`; that table only dedupes the 4h and 15m pre-live alerts.

## Implications for a later historical backfill

SQLite after go-live can reconstruct:

- text, scheduled time, who scheduled it, optional/required claiming flag, automatic vs manual X, live-link setting and delay, media path if not deleted, tweet URL and publication time if auto-X succeeded
- who had claimed or marked unavailable, and whether pre-live alerts fired
- which configured live-link channels were queued and successfully delivered

It cannot reconstruct from the database alone:

- whether X failed or the outcome was unknown
- the archive Discord message
- any performance metrics
- market or Discord-activity context

Those have to come from the archive channel, X analytics, and the external series you mentioned (price, Discord activity). For a statistics pass, treat Discord archive + X URLs as the richer source, and SQLite as the schedule ledger.

## Reliability caveat (not strategy)

The row is marked `live` **before** the X call, URL write, archive send, and reminders. A crash in the middle can leave a `live` row with no `tweet_url` and no archive message, and the scheduler will not pick that row again. Details are in [05 — Reliability notes](05-reliability-notes.md).
