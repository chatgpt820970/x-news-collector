# x-news-collector

A small free bot that gathers the freshest trending news before each @chadriyo posting slot.
The AI assistant reads the result, checks it, writes captions and schedules the posts in Buffer.
This repo does **not** post anything and needs **no secrets or API keys**.

## What it does

1. Reads free feeds: Google News top stories and topic feeds (World, Business, Technology,
   Entertainment, Sports, Science) for the US, India and UK editions, plus local-language top
   stories for Japan, Saudi Arabia and Brazil (Google News has no English edition there).
   It also runs priority searches (Elon Musk, Trump, OpenAI/ChatGPT/Gemini/Claude/Grok, F1,
   cricket, football) and reads Google Trends (US, IN, GB, JP).
2. Keeps only stories published in the **last 9 hours**.
3. Groups headlines about the same story and scores them by how many outlets and feeds carry them,
   with a bonus for Google Trends and priority topics. Ranking is mostly by how recent the story is.
4. Opens the top articles and collects **image links** and **video links** (og:image, og:video,
   YouTube embeds and similar).
5. Skips anything already listed in `data/posted.json`.
6. Saves the top ~25 stories.

## Schedule (IST)

Runs about 25 minutes before each slot:

| Run time (IST) | For slot |
|---|---|
| 04:35 | 05:00 |
| 08:35 | 09:00 |
| 14:35 | 15:00 |
| 18:35 | 19:00 |
| 23:35 | 00:00 |

You can also run it any time: **Actions → Collect news candidates → Run workflow**.

## Output files

- `data/candidates.json`: full data for the assistant. Each item has id, headline, summary, sources,
  article_urls, published_at_utc / published_at_ist, age_minutes, trend_score, category, region,
  image_urls, video_urls and has_video. Items are in ranking order (best first), and `generated_at_ist` shows when the file was made.
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
   - Schedule: *Custom*, time zone **Asia/Kolkata**, at 04:35, 08:35, 14:35, 18:35 and 23:35.
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
