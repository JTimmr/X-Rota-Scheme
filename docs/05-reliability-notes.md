# Reliability notes for the scheduler

These are publish-lifecycle issues. They are independent of For You strategy and independent of the later content backfill. Fixing them makes experiments trustworthy: you cannot learn from a slot if you are unsure whether X accepted it or whether Discord side effects ran.

Verification while this was written: `python -m unittest discover -s tests -v` (114 tests) and `python -m compileall -q src tests` both passed.

## What already works

- Timezone and DST handling, 60-day horizon.
- Fail-closed media validation; no silent text-only fallback.
- Explicit X success / known failure / unknown outcome in the Discord copy.
- Atomic `scheduled` → `live` claim so two workers cannot both tweet the same row.
- Persistent dedupe for the 4h and 15m pre-live alerts.
- Manual-X path that skips the X API and live-link channels.

## Problems that affect pipeline experiments

**Terminal `live` before the work finishes.** The database flip happens before media upload, `create_tweet`, `tweet_url`, archive, and reminders. A crash after the flip but before creation loses the automatic attempt. A crash after creation can lose the ID and the Discord record. The tick will not pick a `live` row again.

**Slot status and X status are the same field.** Manual, success, confirmed failure, and unknown all end as `live`. SQLite cannot answer "did this actually post?"

**Media upload can stall the 60-second loop.** Video processing can poll for up to 15 minutes inside `_check_go_live`, delaying other due posts and reminder checks.

**Overdue catch-up can burst.** After downtime, every due `scheduled` row can fire in one tick, with no `ORDER BY scheduled_at ASC` and no stale-slot confirmation.

**Go-live Discord side effects are not an outbox.** Link-channel sends are caught per channel; archive or reminder failure after `live` is not retried from the database.

**No actual `published_at`.** Archive text has a Discord timestamp; SQLite does not. Pipeline tests that need "minutes from schedule to X" cannot be read from the DB.

## Small fixes worth doing before strategy tests

1. Keep `scheduled` until a lease/dispatch state, then persist a real X outcome: `published`, `failed_known`, `outcome_unknown`, or `not_applicable` for manual X.
2. Store `x_post_id`, `tweet_url`, and `published_at` separately.
3. Queue archive, live-link, and reminder sends so a Discord blip cannot erase a successful tweet ID.
4. Move media processing off the reminder tick.
5. `ORDER BY scheduled_at ASC` and do not auto-dump a huge overdue backlog.
6. Persist whether live-link channels were sent, skipped, or delayed — required for the Discord-link experiment in [02](02-go-live-pipeline-and-discord-links.md).

Do not wait for content metadata (topic, campaign, semantic hash) to do these. That layer belongs with the later backfill.
