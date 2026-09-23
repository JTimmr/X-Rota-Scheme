# Reliability notes for the scheduler

These are publish-lifecycle issues. They are independent of For You strategy and independent of the later content backfill. Fixing them makes experiments trustworthy: you cannot learn from a slot if you are unsure whether X accepted it or whether Discord side effects ran.

Verification while this was written: `python -m pytest -q` (140 tests) and
`python -m compileall -q src tests` both passed.

## What already works

- Timezone and DST handling, 60-day horizon.
- Fail-closed media validation; no silent text-only fallback.
- Explicit X success / known failure / unknown outcome in the Discord copy.
- Atomic `scheduled` → `live` claim so two workers cannot both tweet the same row.
- Persistent dedupe for the 4h and 15m pre-live alerts.
- Manual-X path that skips the X API and live-link channels.
- Per-post live-link disable/delay controls with persisted, per-channel delivery retries.
- Confirmed X publication time stored for successful automatic posts.
- Explicit soft cancellation with an archived, restart-safe reschedule control;
  raw message deletions cannot cancel posts.

## Problems that affect pipeline experiments

**Terminal `live` before the work finishes.** The database flip happens before media upload, `create_tweet`, `tweet_url`, archive, and reminders. A crash after the flip but before creation loses the automatic attempt. A crash after creation can lose the ID and the Discord record. The tick will not pick a `live` row again.

**Slot status and X status are the same field.** Manual, success, confirmed failure, and unknown all end as `live`. SQLite cannot answer "did this actually post?"

**Media upload can stall the 60-second loop.** Video processing can poll for up to 15 minutes inside `_check_go_live`, delaying other due posts and reminder checks.

**Overdue catch-up can burst.** After downtime, every due `scheduled` row can fire in one tick, with no `ORDER BY scheduled_at ASC` and no stale-slot confirmation.

**Archive and go-live reminder side effects are not an outbox.** Live-link sends are now persisted and retried per channel, but an archive or go-live reminder failure after `live` is still not retried from the database.

## Small fixes worth doing before strategy tests

1. Keep `scheduled` until a lease/dispatch state, then persist a real X outcome: `published`, `failed_known`, `outcome_unknown`, or `not_applicable` for manual X.
2. Store `x_post_id` separately; `tweet_url` and `x_published_at` are now persisted on confirmed success.
3. Queue archive and reminder sends too; live-link sends already use persisted per-channel delivery rows.
4. Move media processing off the reminder tick.
5. `ORDER BY scheduled_at ASC` and do not auto-dump a huge overdue backlog.
6. Add bounded retry/backoff policy for persistently missing or forbidden live-link channels.

Do not wait for content metadata (topic, campaign, semantic hash) to do these. That layer belongs with the later backfill.
