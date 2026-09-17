# Audit of the September 2026 FARTBOY CTO X strategy PDF

The PDF is a short creative-strategy memo. It is useful as editorial opinion. It is not a reliable description of the 2026 For You ranker, and it is not a study of this bot's go-live pipeline.

Content verdicts in the PDF ("IRL meets work", "too little video", and so on) should wait for the historical backfill, normalized by price and Discord activity. This note only scores the algorithm claims and the pipeline-relevant advice.

## Overall

The phrase "how the 2026 X algorithm actually ranks posts" overreaches. Several claims mix current Phoenix facts with 2023 coefficients and unsupported folklore.

## Algorithm claims

| PDF claim | Verdict |
| --- | --- |
| For You predicts actions and weights them | **Verified.** Current production Phoenix, not the older Grok-1 demo. |
| Likes are cheap | **Partly true.** Favorite is 0.5; retrieval still trains on favorites. |
| OON posts get 0.75 | **Verified** as the published primary default. |
| Originals from followed accounts beat their replies/reposts | **Verified** as a default discount on followed-author replies/reposts. |
| Mutuals got a July boost | **Verified, narrowly:** +15 on predicted reply for mutual-follow **originals**. |
| Author replies back is one of the strongest current signals | **Outdated.** Matches 2023 `reply_engaged_by_author = 75`. Not a named current scorer term. |
| Bookmarks are a highest-value current head | **Unsupported** in the published weighted sum. |
| Profile visits are high value | **Contradicted** by primary default 0.0. |
| Photo expansion is high value | **Overstated.** Coefficient 0.05. |
| Video views/completion are favored | **Misleading.** Video-open 0.07; VQV/completion 0.0. |
| Immersive videos stay eligible longer | **Not valid for For You Home.** Published Home path still drops candidates older than 48 hours. |
| Keep links out of the main post | **Unsupported** as a ranking rule. `OpenLinkWeight` is +0.2. That is about URLs *inside the tweet*, not Discord chat links. |
| Heavy negative feedback can sink a post | **Verified** as personalized predictions and visibility labels, not raw global count math. |

## Pipeline-relevant advice

The PDF's "reply to every comment quickly" is good community practice if the replies are real. It is **not** justified by a current author-reply-back coefficient.

Nothing in the PDF correctly describes the Discord live-link loop. The public ranker comment that groupchat direct navigation has no ranking impact is the relevant current source for that loop. See [02 — Go-live pipeline and Discord links](02-go-live-pipeline-and-discord-links.md).

Fixed UTC windows, 1–2 posts/day as an algorithm limit, 35–40% video, and 2–4 week recycling are **not** encoded as Home Mixer rules. They may still be reasonable editorial habits. Do not put them in the bot as ranking law.

## What to keep as hypotheses for the later content pass

- specific community and IRL stories
- original artwork and distinct video
- genuine holder amplification
- fewer near-identical posts
- staff meaningful replies
- use this account's own analytics, including price and Discord context

## What not to encode in the scheduler

- 2023 author-reply-back / bookmark / profile-click "weights"
- universal best-time windows
- fixed daily quotas or format percentages
- a ban on URLs in the tweet body
- automatic recycling
- copying X coefficients into a local quality score
- treating Discord "go like this" pings as a ranking feature
