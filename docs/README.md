# X algorithm and rota pipeline notes

These notes replace the single long report. They are about **how a scheduled post is published and announced**, not about which captions or media perform well. Content analysis waits for the historical backfill.

| Document | What it answers |
| --- | --- |
| [01 — Current X For You algorithm](01-x-for-you-algorithm.md) | How the public 2026 For You system ranks posts. |
| [02 — Go-live pipeline and Discord links](02-go-live-pipeline-and-discord-links.md) | Whether sending chat links, likes, and chore comments helps or hurts. **Start here.** |
| [03 — Scheduler lifecycle and archive metadata](03-scheduler-lifecycle-and-archive-metadata.md) | What the bot stores when a slot leaves the schedule and is archived. |
| [04 — PDF strategy audit](04-pdf-audit.md) | What to keep and discard from the September 2026 third-party memo. |
| [05 — Reliability notes](05-reliability-notes.md) | Publish-lifecycle bugs worth fixing regardless of ranking strategy. |

**Reviewed X source:** [`xai-org/x-algorithm`](https://github.com/xai-org/x-algorithm) at commit [`fee1d0f3e99c25e4c10499e1aba4061c8d1cc86d`](https://github.com/xai-org/x-algorithm/commit/fee1d0f3e99c25e4c10499e1aba4061c8d1cc86d) (10 September 2026).

**Reviewed local commit:** `8df1604fbc1996f74a61c26b2300613d3c9906e5`.
