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
