# X For You algorithm and rota scheduler notes

This file was a single long report. It is now split, and reframed away from content strategy.

**Start here:** [docs/README.md](docs/README.md)

The live question is not captions or media mix. It is whether the go-live pipeline — publish, then drop the x.com link into Discord so people click, like, and comment — is something For You rewards. Short answer: **no.** Details are in [docs/02-go-live-pipeline-and-discord-links.md](docs/02-go-live-pipeline-and-discord-links.md).

| Document | Topic |
| --- | --- |
| [docs/01-x-for-you-algorithm.md](docs/01-x-for-you-algorithm.md) | Current public For You ranker |
| [docs/02-go-live-pipeline-and-discord-links.md](docs/02-go-live-pipeline-and-discord-links.md) | Discord links, chore likes, what to change |
| [docs/03-scheduler-lifecycle-and-archive-metadata.md](docs/03-scheduler-lifecycle-and-archive-metadata.md) | What is stored at go-live / archive |
| [docs/04-pdf-audit.md](docs/04-pdf-audit.md) | Third-party PDF vs current source |
| [docs/05-reliability-notes.md](docs/05-reliability-notes.md) | Publish-lifecycle bugs |

Content analysis waits for the historical backfill (including price and Discord activity). The scheduler remains a clock, reminder system, and one-shot publisher.
