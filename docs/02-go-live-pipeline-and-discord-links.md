# Go-live pipeline and Discord links

This is the operational question: once a slot fires, the bot publishes to X and drops the x.com URL into Discord so a small group can click, like, and comment. Some of those people treat it as a chore and interact without reading.

Content quality is out of scope here. The scheduler does not choose captions or media. It is a clock, a reminder system, and a one-shot publisher.

## Direct answer

**Sending the live x.com link into Discord chats is not something the public For You ranker rewards.** X's own ranking comments say an action only counts if it happens on a post **served in Home Timeline**. Direct navigation — "coordinating via groupchat" — has **no ranking impact**.

So the current "post goes live → dump the URL → people click and tap like/reply" loop is not a For You growth tactic. It is community ops. It may still be useful for holders who do not live in the X app. It is not the copy-link signal people quote from the weights file.

If the goal is For You, the highest-leverage pipeline change is:

1. Keep publishing originals on X.
2. Keep the archive and claimer reminders for the team.
3. **Stop, delay, or narrow the automatic chat-link dump**, and stop asking people to like/comment from that link.
4. If the inner group should respond at all, they should meet the post in Home or Following and leave a real reply, not a duty like from a Discord URL.

That is a **strong inference from current source comments**, not a live A/B result on this account. The scheduler now supports the clean test: publish to X while either skipping the live-link channels or delaying their URL delivery.

## What the bot does today at go-live

For an automatic-X slot the 60-second scheduler:

1. Marks the SQLite row `scheduled` → `live`.
2. Deletes the schedule-channel message.
3. Uploads media if needed and calls X once.
4. Stores `tweet_url` and the confirmed X publication time on success.
5. Queues one persisted URL delivery per configured live-link channel, unless Discord live links are disabled for the post.
6. Posts a richer record to the archive channel.
7. Pings claimers in reminders: "Your post just went live! Time to share the link and engage with replies." plus the URL.
8. Sends queued live-link URLs when their per-post delay expires; immediate deliveries run later in the same scheduler tick.

Manual-X slots skip the X call and skip the live-link channels. They still archive and remind.

Claims are optional by default. An unclaimed optional post gets no targeted "get ready to engage" ping. The chore loop therefore depends on whoever happens to watch the link channels, not on a required owner.

## Why this is not the copy-link strategy

The public weight `ShareViaCopyLinkWeight = 20` is the largest positive coefficient. That number is widely misread.

| What people assume | What the code does |
| --- | --- |
| Author or Discord bot pastes the URL into a chat | No bonus. That is outbound distribution, not a Home predicted action. |
| Chat members click the URL | Direct navigation. X says this has no ranking impact. |
| Chat members like/comment after that click | Those actions are not Home-served. Same comment: they do not count in the recommendation system the way Home actions do. |
| A stranger sees the post in For You and copies the link to send to a friend | This is the predicted action with weight 20. |

The ranker is asking, for each Home viewer, "will **this person** copy the link?" It is not asking "how many Discord members did we send the link to?"

Likes from the rota are also a weak object even when they *are* Home-served: favorite weight is 0.5. Unread chore replies are not the same as predicted genuine replies (5.0, or 20.0 only for mutual-follow originals). A duty reply from someone who did not dwell is closer to noise, and **not dwelled** is an explicit negative prediction (−0.02).

## Can Discord links hurt For You?

X does **not** publish a "Discord dump penalty." The documented claim is that groupchat coordination **does not help ranking**, not that it applies a coded multiplier against the account.

There are still plausible second-order costs:

**Seen/served suppression.** For You growth comes from new eligible Home viewers. If the same inner circle opens the post from Discord first, that session is not a Home serve. If they later skip it in Home, or never see it there because it was already consumed, the account lost the viewers most willing to reply or copy-link.

**Low-dwell, high-tap pattern.** People who like without reading are exactly the behavior the dwell and not-dwelled heads are trying to separate from real attention. If those people are also typical Home viewers for this account, teaching Phoenix "this audience favorites without dwelling" is not the goal.

**Self-competition is irrelevant here; authenticity is not.** Author diversity only kicks in when several of the account's posts compete in one request. The Discord loop does not create that. It can still create a small, repeated engager cluster whose actions look unlike the broader audience Phoenix is trying to predict.

**Community value is real and separate.** Holders who live in Discord may want the URL for price chat, screenshots, or morale. That is not For You. Keep it if it is worth it for Discord. Do not keep it because "copy-link is 20."

## Recommendation

Treat the live-link channels as a **distribution switch**, not as ranking fuel.

| Pipeline | For You (public code) | Discord ops | Suggested default |
| --- | --- | --- | --- |
| Current: immediate URL dump + "go like/comment" | No ranking credit for those clicks; possible Home-serve waste | High awareness, chore engagement | Do not treat as the growth path |
| Remind claimers, but do not send public chat links | Removes the groupchat coordination path | Team still knows the slot fired | Best first algorithm-facing change |
| Delay the Discord URL | Gives Home a head start before direct-nav | Still informs the server later | Best experiment if Discord awareness still matters |
| No X post, Discord only | No For You candidate | Internal preview | Use only for dry runs |
| Archive-only, no link channels, no "go engage" | Cleanest For You isolation | Ops record only | Control arm |

Practical rule for the inner group, if they stay in the loop at all:

- Do not click the Discord URL to farm a like.
- Open X Home or Following, find the post if it appears, and reply only if there is something real to say.
- The claimer/owner's job is to **answer incoming genuine replies**, not to seed the first ten likes.

That last point is community operations, not a current "author replies back" coefficient. The 2023 `reply_engaged_by_author = 75` head is not in the 2026 weighted scorer. Real conversation can still matter because Phoenix predicts replies, quotes, and copy-link from *other* viewers.

## How to test this without a content study

Do not wait for the historical caption backfill. The pipeline can be tested on ordinary upcoming slots, with content held as similar as it already is.

Use the three live-link settings available while scheduling and on each scheduled-post card:

1. **No Discord live links** — X post, archive, reminders; link channels silent.
2. **Delayed Discord links** — same X post; URL hits chats after a chosen lag (30–60 minutes is a reasonable first band; 3 hours is a second band).
3. **Current immediate dump** — control.

Keep one primary change at a time. Judge with impression-normalized outcomes at 1h / 3h / 24h / 48h, not first-ten-minutes like count. The 48-hour window matches For You eligibility.

Price, Discord activity, and "not all times are equal" still confound this. That is why matched pairs on nearby days, or a simple A/B on consecutive similar slots, beat a vibes read. The backfill can later check whether price regime swamped the pipeline effect.

Until that test runs, the evidence-based stance is:

**You are not better off sending chat links for the algorithm. You may still send them for the community. If you have to pick one default while waiting for data, stop using the live-link channels as an engagement squad, and keep archive plus owner reminders.**

## Out of scope

- Which topics, art, or videos work.
- Best hour or posts-per-day.
- Whether a URL inside the X post text itself is good (that is a caption choice; `OpenLinkWeight` is a small positive 0.2, not a ban).
- Recoding X's weights into the Discord bot.
