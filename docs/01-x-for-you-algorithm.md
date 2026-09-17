# Current X For You algorithm

**Source:** [`xai-org/x-algorithm`](https://github.com/xai-org/x-algorithm) at commit [`fee1d0f3e99c25e4c10499e1aba4061c8d1cc86d`](https://github.com/xai-org/x-algorithm/commit/fee1d0f3e99c25e4c10499e1aba4061c8d1cc86d), 10 September 2026.

This is a mechanism note. It does not recommend captions, topics, or media mix. Those wait for the account backfill.

## Confidence

- **Verified current source:** wired behavior or a default in that commit.
- **Official claim:** X/xAI describes it as production code or a primary default; live overrides may still exist.
- **Strong inference:** follows from several current paths, but depends on unpublished config or data.
- **Unsupported/outdated:** absent from current public code, contradicted by it, or copied from 2023 Twitter.

The current public source is `xai-org/x-algorithm`, not `twitter/the-algorithm`. Durable ideas survived (predicted actions, freshness, in/out-of-network, negatives, author diversity). Specific heads and coefficients changed. Do not import `reply_engaged_by_author = 75` into a 2026 plan.

## What For You actually does

For each viewer request, Home Mixer:

1. Hydrates that viewer's recent actions, follows, mutes, seen/served posts, and context.
2. Retrieves candidates in parallel (Thunder for followed authors, Phoenix and SimClusters for out-of-network).
3. Drops ineligible posts, including anything older than **48 hours**.
4. Predicts that viewer's actions with Phoenix.
5. Multiplies those **predictions** by product weights, then applies author diversity, an out-of-network discount, and content diversity.
6. Runs visibility filtering, then blends the survivors with ads and modules.

Phoenix is personalized. The same post gets a different score for different viewers. There is no single global virality number.

The public home ranker does not score raw caption text as a quality essay. Content reaches it mainly as semantic IDs, media/URL flags, author relationship, age, and engagement-count transforms.

## Score construction

Simplified default:

`base = offset(sum(weight_i × predicted_action_i))`

`final = network_discount(author_diversity(cold_start(base)))`

The weights multiply **predicted probabilities or continuous predictions**, not raw likes or reports. X's own comment says it is incorrect to treat them as count exchange rates such as "one report cancels 468 likes."

### Published primary positive defaults

| Predicted action | Weight |
| --- | ---: |
| Share via copy link | 20.0 |
| Reply on an eligible mutual-follow original | 5.0 + 15.0 = 20.0 |
| Ordinary reply | 5.0 |
| Quote | 5.0 |
| Share via DM | 5.0 |
| Follow author | 4.0 |
| Share | 2.0 |
| Repost | 1.0 |
| Favorite / like | 0.5 |
| Post click | 0.4 |
| Open link | 0.2 |
| Video open | 0.07 |
| Photo expand | 0.05 |
| Binary dwell | 0.05 |
| Quoted-post click | 0.05 |
| Post unexplored (in-network only by default) | 0.02 |
| Continuous dwell | 0.004 |

### Published zeros

Profile click, video quality view/completion, quoted VQV, and continuous click dwell are **0.0** in the primary defaults.

### Published negatives

| Predicted action | Weight |
| --- | ---: |
| Report | −234.0 |
| Mute author | −58.8 |
| Not interested | −43.2 |
| Block author | −31.2 |
| Not dwelled | −0.02 |

Rare negatives have large coefficients because they are rare, so the prediction can move the sum at all. That is not a global "N likes cancel one report" rule. Personalized predictions also mean a report mainly affects similar viewers, not every viewer equally.

## Rules that matter for a one-account publisher

- **48-hour For You wall.** `AgeFilter` uses `MAX_POST_AGE = 48 hours`, with no video exemption in the published Home path.
- **Originals over replies/reposts for discovery.** Out-of-network replies/reposts are filtered; followed-author replies/reposts take the same 0.75 discount as out-of-network originals by default.
- **Out-of-network originals need a higher pre-discount score** to tie an equivalent in-network original (default multiplier 0.75).
- **Same-author decay in one request:** 1.0, 0.625, 0.4375, 0.34375, approaching 0.25. This is per-request self-competition, not a daily quota.
- **Seen/served suppression.** A post grows by reaching new eligible viewers, not by re-winning the same person's next refresh.
- **Visibility is separate from ranking.** A post can score well and still be dropped for recommendation-only spam, malicious URL, Do Not Amplify, or similar labels.

## What copy-link actually means

`ShareViaCopyLinkWeight = 20` is the weight on **P(this Home viewer will copy the link)**.

It is not a bonus for the author pasting the post URL into Discord, Telegram, or a group chat. It is not a count of visits that arrive from those chats.

X documents this distinction in `home-mixer/scorers/ranking_scorer.rs` and `home-mixer/params/param.rs`:

> For an account to count in the algorithms recommendation system, it must take place on a post served in Home Timeline. Directly navigating to a post (i.e., coordinating via groupchat) has no ranking impact.

Pipeline consequences of that comment are in [02 — Go-live pipeline and Discord links](02-go-live-pipeline-and-discord-links.md).

## What this source does not establish

- a universal best hour or weekday
- an ideal posts-per-day number
- a video percentage target
- a blanket penalty for URLs inside the post text
- a current "author replies back" ranking head
- a current bookmark coefficient
- a profile-visit benefit at the current zero default
- a first-hour boost formula
- a Premium/verified scoring multiplier

## Limits

The repository does not include production checkpoints, training data, every experiment override, or enough config to reproduce a live FARTBOY score. It explains the public mechanism and primary defaults. It cannot promise that every viewer receives identical treatment.
