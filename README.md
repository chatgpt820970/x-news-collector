# x-news-collector

A small free bot that gathers the freshest trending news before each @chadriyo posting slot.
The AI assistant reads the result, checks it, writes captions and schedules the posts in Buffer.
This repo does **not** post anything and needs **no secrets or API keys**.

## What it does

1. Reads free feeds: Google News top stories and topic feeds (World, Business, Technology,
   Entertainment, Sports, Science) for the US, India and UK editions, plus local-language top
   stories for Japan, Saudi Arabia and Brazil (Google News has no English edition there).
   It also runs priority searches and reads Google Trends (US, IN, GB, JP). Priority topics:
   - Elon Musk, Trump, OpenAI/ChatGPT/Gemini/Claude/Grok, F1, cricket, football
   - **Celebrities & music**: big stars (Taylor Swift, Drake, BTS, Bad Bunny, The Weeknd, Beyonce,
     Arijit Singh, Diljit Dosanjh), album releases, world tours, concerts, award shows, trailers,
     weddings, Bollywood and Hollywood
   - **AI tools & agents**: AI agents, AI automation, new AI tools, AI launches, no-code AI
   - **Finance & markets**: stock market, Sensex/Nifty, Dow/Nasdaq/S&P 500, Fed rate, gold price, RBI
   - **Crypto**: Bitcoin, Ethereum, crypto market, Solana/XRP
   - **Famous people clips**: fresh interviews, podcasts, speeches, keynotes, awards and ceremonies
     of big names (Elon Musk, Jensen Huang, Sam Altman, Zuckerberg, Bezos, Bill Gates, Ronaldo, Messi,
     Virat Kohli, LeBron, Eileen Gu, Taylor Swift, Shah Rukh Khan ...), plus Joe Rogan / Lex Fridman /
     Diary of a CEO / On Purpose podcast news
   - **YouTube channel feeds** (free RSS, no key) for big interview/podcast shows and official
     channels: Lex Fridman, The Diary Of A CEO, PowerfulJRE, JRE Clips, Jay Shetty, All-In, Dwarkesh
     Patel, Y Combinator, Stanford GSB, TED, 60 Minutes, Bloomberg, CNBC, OpenAI, Google, NVIDIA,
     Microsoft, Apple, Tesla, SpaceX, The White House, Formula 1, ICC, RCB, NBA, Olympics, Real Madrid,
     FC Barcelona and UR Cristiano. These help the assistant find the actual moment or the official
     clip; the assistant decides whether a clip can be reused.
2. Keeps only stories published in the **last 9 hours** (YouTube uploads: last **24 hours**).
3. Groups headlines about the same story and scores them by how many outlets and feeds carry them,
   with a bonus for Google Trends and priority topics. Ranking is mostly by how recent the story is.
   Each story gets a `category`: `famous_clip`, `ai`, `celebrity_music`, `finance`, `crypto`,
   `sports`, `tech`, `world` or `other`. `famous_clip` = a famous person speaking or a big moment
   (interview, podcast, keynote, speech, award, ceremony), or a podcast-channel upload naming a star.
   Stories naming a famous person get a small boost, and a bigger one when they also have a video.
4. Opens the top articles and collects **image links** and **video links** (og:image, og:video,
   YouTube embeds and similar).
5. Skips anything already listed in `data/posted.json`.
6. Saves the top **40** stories (enough for ~20 posts a day). To keep the mix balanced, the best
   2 stories of every priority topic are reserved first, each priority topic is capped at 3 and
   each category at 8. `famous_clip` is capped at 4 with at least 2 reserved when available, and
   2-6 YouTube uploads are included (famous-person clips and shorts first).

## Schedule (IST)

20 posts a day across 8 posting slots. The collector runs about 25 minutes before each slot
(cron `5 23,2,4,5,8,15,17,18 * * *` in UTC):

| Run time (IST) | For slot | Cron (UTC) |
|---|---|---|
| 04:35 | 05:00 | `5 23 * * *` |
| 07:35 | 08:00 | `5 2 * * *` |
| 09:35 | 10:00 | `5 4 * * *` |
| 10:35 | 11:00 | `5 5 * * *` |
| 13:35 | 14:00 | `5 8 * * *` |
| 20:35 | 21:00 | `5 15 * * *` |
| 22:35 | 23:00 | `5 17 * * *` |
| 23:35 | 00:00 | `5 18 * * *` |

You can also run it any time: **Actions → Collect news candidates → Run workflow**.

## Output files

- `data/candidates.json`: full data for the assistant. Each item has id, headline, summary, sources,
  article_urls, published_at_utc / published_at_ist, age_minutes, trend_score, category
  (famous_clip / ai / celebrity_music / finance / crypto / sports / tech / world / other), section (Google News
  section), priority_queries (matched priority topics), region, image_urls, video_urls, has_video,
  `famous_names` (big names recognised in the headline), `source_type` (`news`, `youtube` or
  `news+youtube`) and `youtube_videos` (title, channel, url, published time, thumbnail, is_short).
  The top-level `categories` field counts stories per category. Items are in ranking order (best first), and `generated_at_ist` shows when the file was made.
- `data/candidates.md`: the same list as an easy-to-read table, newest first.
- `data/posted.json`: a list of headlines (or the `key` field from candidates.json) that were already
  posted. The assistant adds to it after posting so stories aren't repeated.

## If the schedule doesn't fire (optional backup)

GitHub sometimes skips or delays scheduled runs, especially on new private repos. If no new
"Update news candidates" commits show up around the times above, use the free service
[cron-job.org](https://cron-job.org) to start the workflow:

1. **Make a token** on GitHub: profile picture → Settings → Developer settings →
   Personal access tokens → **Fine-grained tokens** → Generate new token.
   - Repository access: *Only select repositories* → `x-news-collector`.
   - Permissions → Repository permissions → **Actions: Read and write**.
   - Generate it and copy the token. You won't see it again.
2. **On cron-job.org**, create a free account, then *Create cronjob*:
   - URL: `https://api.github.com/repos/chatgpt820970/x-news-collector/actions/workflows/collect.yml/dispatches`
   - Schedule: *Custom*, time zone **Asia/Kolkata**, at 04:35, 07:35, 09:35, 10:35, 13:35, 20:35, 22:35 and 23:35.
   - Advanced → Request method: **POST**
   - Headers:
     - `Authorization: Bearer YOUR_TOKEN`
     - `Accept: application/vnd.github+json`
     - `X-GitHub-Api-Version: 2022-11-28`
   - Request body: `{"ref":"main"}`
3. Save, then click *Test run*. A new run should show up under the repo's **Actions** tab.

If both GitHub's schedule and cron-job.org trigger at once, nothing breaks. The runs queue one after another.

## Run locally

```bash
pip install -r requirements.txt
python collector.py
```

## Repeat guard (no reposts)

- `data/blocklist.json`: permanent blocks (phrases + X post ids). The Dua Lipa stage/performance clip is blocked forever.
- `data/recent_topics.json`: every celebrity/topic posted, with `posted_at`. Anything posted in the last **7 days** is skipped.
- `collector.py` drops candidates that hit either list. Before scheduling, the assistant must run
  `python repeat_guard.py check "<caption + source link>"` (exit 1 = blocked), and after scheduling
  `python repeat_guard.py add "<celebrity>" "<topic>" --at <ISO time>`.

## Upload-ready media (`data/media_ready.json`)

Two producers fill `data/media_ready.json`, both with `media_prep.py`, and merge into it (same
candidates generation = merged; newer generation = replaced; pull/rebase-free: fetch + reset + merge + push):

- **CI** (non-YouTube videos + photos): the last step of the collect run pushes `trigger/last.json`
  to the public repo `chatgpt820970/chad-media`, which starts its **Prepare media** workflow
  (public repo minutes). `SKIP_YOUTUBE=1` there, because YouTube blocks GitHub's servers.
- **Box** (YouTube videos): `/workspace/scanners/bin/run-media-prep.sh` on the agent box, from cron
  at 04:42, 07:42, 09:42, 10:42, 13:42, 20:42, 22:42 and 23:42 IST (about 7 min after each collect run). It runs
  `media_prep.py box` with a time budget that ends ~hh:45, merges, pushes and starts **Publish Pages**.
  Log: `/workspace/scanners/logs/media-prep.log`.

Per video: the top 12 stories with a video source are probed and the highest-resolution source
wins (a low-res re-upload never beats the original). Under 1080p = skipped. Videos longer than
2:20 are cut to a 2:20 window around the YouTube "most replayed" heatmap peak (peak ~40 s in), else a
key chapter (highlight/goal/moment/speech...), else from 0:00 (`trim_start_sec`, `trim_reason`).
Outputs: `*-1080.mp4` (H.264 High/AAC ~10 Mbps, 1080 on the short side, faststart), `*-2160.mp4`
when the source is 4K, time allows and it's 512 MB or less, and 3 check frames (480 px). Stories
without a ready video get up to 3 photos of 1080 px or more on the short side.

Hosting: every file is a Release asset in chad-media (`m-YYYYMMDD-HHMM` for CI, `...-yt` for the box;
`m-*` releases older than 3 days are deleted). **Publish Pages** mirrors every Pages URL listed in
media_ready.json to `https://chatgpt820970.github.io/chad-media/m/<file>` (direct 200, `video/mp4` /
`image/jpeg`). 4K files and anything over the Pages budget use the Release URL (302, then
`application/octet-stream`). `video_1080_backup_url` is always the Release copy (kept 3 days).

```json
{
 "generated_at_ist": "...", "candidates_generated_at_utc": "...", "candidates_generated_at_ist": "...",
 "release_tag": "m-... (CI)", "parts": [{"by": "ci|box", "release_tag": "...", "runtime_sec": 90, "generated_at_ist": "..."}],
 "stats": {"videos": 5, "videos_4k": 1, "photo_stories": 5, "skipped": 2},
 "items": {
  "<story key>": {"type": "video", "prepared_by": "ci|box", "release_tag": "...", "headline": "...", "category": "...",
   "source_url": "https://www.youtube.com/watch?v=...", "source_title": "...", "source_resolution": "3840x2160",
   "source_fps": 25.0, "source_duration": 612, "duration": 140.0, "trimmed_to_140s": true,
   "trim_start_sec": 233.0, "trim_reason": "heatmap peak at 273s", "resolution": "3840x2160",
   "video_4k_url": "https://github.com/.../releases/download/.../...-2160.mp4 or null",
   "video_4k_resolution": "3840x2160", "video_4k_size_mb": 180.2,
   "video_1080_url": "https://chatgpt820970.github.io/chad-media/m/...-1080.mp4",
   "video_1080_backup_url": "https://github.com/.../releases/download/.../...-1080.mp4",
   "video_1080_resolution": "1920x1080", "video_1080_size_mb": 80.1, "video_1080_bitrate_mbps": 10.1,
   "frame_urls": [".../m/...-f1.jpg", ".../m/...-f2.jpg", ".../m/...-f3.jpg"]},
  "<story key>": {"type": "photo", "headline": "...", "photo_urls": ["..."],
   "photos": [{"url": "...", "source_url": "...", "width": 2048, "height": 1365}]}
 },
 "skipped": [{"key": "...", "reason": "best source only 720p", "source_url": "..."}]
}
```

Secrets: `CHAD_MEDIA_DEPLOY_KEY` (here; pushes the trigger file), `XNC_DEPLOY_KEY` (in chad-media;
write deploy key for this repo), optional `YT_COOKIES` (in chad-media; if set, CI also does YouTube).
The box uses its logged-in `gh` CLI. No keys in code.
Local test without uploading: `DRY_RUN=1 MAX_VIDEOS=3 python media_prep.py box` (or no arg for CI mode).

## Grok usage

The day slots are already written. Grok copies them. It does not write captions, pick media, or invent a link.

Queue (refreshed about 30 minutes before 04:47, 07:47, 09:47, 10:47 and 13:47 IST, plus 14:55 IST for the afternoon replies):

https://raw.githubusercontent.com/chatgpt820970/x-news-collector/main/data/grok_queue.json

Replies for 09:30 and 15:15 IST:

https://raw.githubusercontent.com/chatgpt820970/x-news-collector/main/data/grok_replies.json

When the chad-media Pages feed has been updated, the same files are also at:

https://chatgpt820970.github.io/chad-media/feed/grok_queue.json

https://chatgpt820970.github.io/chad-media/feed/grok_replies.json

Do this, and nothing else:

1. Open the queue URL. Read `slots`. For each slot, schedule every item in `posts` on the Buffer channel named `chadriyo` (`list_channels`; do not guess the id). `schedulingType` is `automatic`, `mode` is `customScheduled`, and `dueAt` is that slot's `slot_ist` (IST, `+05:30`).
2. `text` is the `caption`, copied exactly. `assets` are the `media` URLs in order. `media_kind` `video` is a video asset. `media_kind` `image` is an image asset. Do not download, rehost, or crop them.
3. If Buffer rejects a media file, skip that post and continue with the next post. Do not substitute a different file.
4. Never invent a caption, a hashtag, a question, a link, or a media URL. If a slot has no post, leave it empty.
5. Open the replies URL. At 09:30 IST post the 09:30 reply texts. At 15:15 IST post the 15:15 reply texts. Reply on `post_url` with `reply` only. Add no link, no hashtag, and no extra sentence.
