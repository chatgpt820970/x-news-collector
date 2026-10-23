#!/usr/bin/env python3
"""Build a copy-only queue for the @chadriyo day slots.

Writes data/grok_queue.json and data/grok_replies.json.
No API keys in this file. Chad-media publishes only when CHAD_MEDIA_DEPLOY_KEY
is present in the environment (GitHub Actions secret). Nothing here is echoed.

Media rules:
  images -> https://raw.githubusercontent.com/...
  videos -> https://cdn.jsdelivr.net/gh/chatgpt820970/chad-media@<commit>/...
            and the file must be under 20MB
  never https://chatgpt820970.github.io/chad-media/m/...
  never a pbs.twimg.com URL reused as the media link
Every media URL is HEAD-checked for 200 and image/* or video/mp4.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
COMMONS_UA = "x-news-collector/1.0 (https://github.com/chatgpt820970/x-news-collector)"
MEDIA_REPO = "chatgpt820970/chad-media"
XNC_REPO = "chatgpt820970/x-news-collector"
MAX_VIDEO_BYTES = 20 * 1024 * 1024
DAY_SLOTS = (
    ("04:47", "05:00"),
    ("07:47", "08:00"),
    ("09:47", "10:00"),
    ("10:47", "11:00"),
    ("13:47", "14:00"),
)
REPLY_CLOCKS = ("09:30", "15:15")
LOCK_MINUTES = 90
REFRESH_HOURS = 6
CAPTION_VERSION = 2

HOSTED_VIRAL = (
    {
        "key": "cherki-goal-liverpool",
        "path": "v/livmci_cherki.mp4",
        "headline": "Cherki scores against Liverpool",
        "hook": "1 finish from Cherki against Liverpool, and the keeper never sets his feet",
        "context": (
            "The touch is gone before the replay angle catches up. "
            "Football skill clips are what verified sports accounts actually replay."
        ),
        "source": "https://github.com/chatgpt820970/chad-media/blob/main/v/livmci_cherki.mp4",
        "hashtags": ["#PremierLeague", "#Cherki"],
        "emoji": "\u26bd",
        "topic": "cherki",
        "families": ["cherki"],
    },
)

FAMILIES = {
    "musk": ("musk", "elon", "starlink", "spacex", "tesla"),
    "trump": ("trump",),
    "ronaldo": ("ronaldo",),
    "messi": ("messi",),
    "lebron": ("lebron",),
    "kardashian": ("kardashian",),
    "verstappen": ("verstappen",),
    "bad bunny": ("bad bunny",),
    "taylor": ("taylor swift",),
    "openai": ("openai", "chatgpt", "altman"),
    "cherki": ("cherki",),
    "nasa": ("nasa",),
    "ufc": ("ufc",),
}
WAR_RE = re.compile(
    r"\b(war|gaza|ukraine|missile|airstrike|invasion|hamas|hezbollah|idf|bombing|troops)\b",
    re.I,
)
MILITARY_RE = re.compile(r"\b(howitzer|missile|bomb|weapon|military|gunfire|artillery)\b", re.I)
LOCAL_RE = re.compile(r"\b(sensex|nifty|rupee|panchayat|bengaluru|horoscope)\b", re.I)
PREF_RE = re.compile(
    r"\b(ai|openai|chatgpt|grok|gemini|claude|musk|tesla|spacex|nvidia|apple|nasa|"
    r"google|microsoft|nfl|nba|ufc|formula 1|f1(?!\.\d)|premier league|champions league|ronaldo|messi|"
    r"lebron|verstappen|taylor swift|drake|beyonc|kardashian|bad bunny|billie|"
    r"rihanna|bts(?!:)|trump|white house|congress)\b",
    re.I,
)
CLASS_RULES = (
    ("ai", re.compile(r"\b(openai|chatgpt|gemini|claude|grok|anthropic|\bai\b)\b", re.I)),
    ("tech", re.compile(
        r"\b(apple|google|nvidia|tesla|spacex|microsoft|nasa|iphone|chip|robot|starlink)\b", re.I)),
    ("sports", re.compile(
        r"\b(nfl|nba|ufc|formula 1|f1(?!\.\d)|football|soccer|cricket|goal|premier league|champions league|"
        r"ronaldo|messi|lebron|verstappen|nhl)\b", re.I)),
    ("celebrity", re.compile(
        r"\b(taylor swift|drake|beyonc|kardashian|bad bunny|billie|rihanna|bts(?!:)|concert|album|oscar|prabhas)\b",
        re.I)),
    ("us_news", re.compile(
        r"\b(trump|white house|congress|senate|supreme court|biden|midterm|early voters)\b", re.I)),
)
STOP = {
    "the", "and", "for", "with", "that", "this", "from", "after", "before", "into", "over",
    "have", "has", "was", "were", "are", "its", "his", "her", "their", "about", "just",
    "will", "says", "said", "new", "out", "not", "but", "you", "your", "they", "them",
}
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]+")
NUM_RE = re.compile(r"\b\d[\d,.]*%?\b")
BAD_MEDIA_HOSTS = ("pbs.twimg.com", "twimg.com", "gstatic.com", "news.google.com", "encrypted-tbn")
STATUS_RE = re.compile(r"https?://(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d+)")
SKIP_TWEET = re.compile(r"\b(kill yourself|rape|lynch|slur)\b", re.I)
REPLY_ACCOUNTS = (
    ("elonmusk", "ai"),
    ("OpenAI", "ai"),
    ("AnthropicAI", "ai"),
    ("Google", "tech"),
    ("nvidia", "tech"),
    ("NASA", "tech"),
    ("SpaceX", "tech"),
    ("NBA", "sports"),
    ("NFL", "sports"),
    ("ESPN", "sports"),
    ("premierleague", "sports"),
    ("UFC", "sports"),
    ("F1", "sports"),
    ("PopCrave", "celebrity"),
    ("billboard", "celebrity"),
    ("Netflix", "celebrity"),
    ("YouTube", "celebrity"),
    ("rihanna", "celebrity"),
    ("Reuters", "world"),
    ("WhiteHouse", "us_news"),
    ("AP", "us_news"),
    ("Forbes", "us_news"),
    ("nytimes", "us_news"),
    ("WSJ", "us_news"),
)
REPLY_NAMES = (
    "LeBron", "Maxey", "Embiid", "Jensen", "Elon Musk", "Starlink", "Tesla", "OpenAI",
    "Gemini", "Claude", "Grok", "NASA", "Rihanna", "Netflix", "Lana Del Rey", "Taylor Swift",
    "Cherki", "Trump", "Spotify", "Nvidia", "Google", "SpaceX", "Anthropic", "Merab",
    "Baleba", "Kimi", "YouTube",
)
PREFERRED_REPLY_CATS = ("ai", "tech", "sports", "celebrity", "us_news")
TOPIC_WORDS = (
    "grok", "openai", "chatgpt", "tesla", "spacex", "starlink", "nvidia", "apple",
    "nasa", "nba", "nfl", "ufc", "ronaldo", "messi", "lebron", "trump", "taylor",
    "kardashian", "bitcoin", "google", "champions league", "premier league",
)
REPLY_PATTERNS = {
    "ai": (
        "the {t} part is the one that actually sounds usable",
        "{t} is the concrete piece in this",
    ),
    "tech": (
        "{t} is the product detail worth stopping for",
        "the {t} part is a real change",
    ),
    "sports": (
        "{t} is the bit that makes the replay worth it",
        "{t} made that look casual",
    ),
    "celebrity": (
        "{t} is the part people are going to clip",
        "they really put {t} out like it was nothing",
    ),
    "us_news": (
        "the {t} line is the one people will quote",
        "{t} is the part that is going to stick",
    ),
    "world": (
        "{t} is the concrete part of this",
        "the {t} line is what travels",
    ),
}
FACT_PATTERNS = {
    "ai": (
        "{t} is the figure that makes this concrete",
        "the {t} number is the useful part",
    ),
    "tech": (
        "{t} is the spec that actually matters",
        "the {t} figure is the real update",
    ),
    "sports": (
        "{t} is the number that makes the replay worth it",
        "the {t} line is nasty in a good way",
    ),
    "celebrity": (
        "{t} is the number people are going to clip",
        "they really put {t} out like it was nothing",
    ),
    "us_news": (
        "{t} is the figure that is going to stick",
        "the {t} number is the one worth reading twice",
    ),
    "world": (
        "{t} is the concrete figure here",
        "the {t} number is what travels",
    ),
}
GENERIC_REPLIES = {
    "ai": ("ok this is the version that actually sounds usable", "this is the useful one"),
    "tech": ("the product change is the real part", "this is a real update"),
    "sports": ("that timing is ridiculous", "that replay is nasty in a good way"),
    "celebrity": ("they really just let this one out", "the clip is the whole story"),
    "us_news": ("the middle line is the one that sticks", "that is the line people will quote"),
    "world": ("the concrete line is the one that travels", "that detail is the whole post"),
}
INSTRUCTIONS = (
    "Read this file only. Do not invent a post, a caption, a link, or a media file.",
    "On Buffer, use the channel named chadriyo (list_channels, never guess the id). schedulingType automatic, mode customScheduled, dueAt exactly slot_ist.",
    "text is the caption, copied exactly. assets are the media URLs in order: video kind uses a video url, image kind uses image urls.",
    "If Buffer rejects a media file, skip that post and continue with the next one. Do not swap in a different file.",
    "Replies come only from grok_replies.json. Post that reply text on the given post link. Add no link, no hashtag, and no extra sentence.",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def slug(text: str, n: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:n].strip("-") or "item")


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if t not in STOP}


def families_in(text: str) -> set[str]:
    low = (text or "").lower()
    found = set()
    for fam, words in FAMILIES.items():
        for w in words:
            if re.search(r"\b" + re.escape(w) + r"\b", low):
                found.add(fam)
                break
    return found


def classify(text: str) -> str:
    for name, rx in CLASS_RULES:
        if rx.search(text or ""):
            return name
    return "world"


def is_war(text: str) -> bool:
    return bool(WAR_RE.search(text or ""))


def forbidden_media_url(url: str) -> bool:
    u = (url or "").lower()
    if "github.io/chad-media/m/" in u or "chatgpt820970.github.io/chad-media/m" in u:
        return True
    if "pbs.twimg.com" in u or "twimg.com" in u:
        return True
    return False


def strip_emoji(text: str) -> str:
    return EMOJI_RE.sub("", text or "")


def clean_sentence(text: str, limit: int) -> str:
    text = strip_emoji(text or "")
    text = text.replace("?", ".")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .,-")
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" .,")
    return text


def build_caption(emoji: str, hook: str, context: str, hashtags: list[str]) -> str:
    hook = clean_sentence(hook, 170)
    context = clean_sentence(context, 180)
    tags = []
    for tag in hashtags:
        t = "#" + re.sub(r"[^A-Za-z0-9]", "", tag.lstrip("#"))
        if len(t) > 2 and t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
        if len(tags) == 2:
            break
    body = f"{emoji} {hook}\n\n{context}"
    if tags:
        body = body + "\n\n" + " ".join(tags)
    if len(body) > 280:
        overflow = len(body) - 280
        context = clean_sentence(context, max(40, len(context) - overflow - 1))
        body = f"{emoji} {hook}\n\n{context}"
        if tags:
            body = body + "\n\n" + " ".join(tags)
    if len(body) > 280:
        body = body[:280].rsplit(" ", 1)[0].rstrip()
    return body


def caption_ok(text: str) -> bool:
    if not text or len(text) > 280 or "?" in text:
        return False
    if text.count("#") > 2:
        return False
    if not EMOJI_RE.match(text):
        return False
    rest = EMOJI_RE.sub("", text, count=1)
    if EMOJI_RE.search(rest):
        return False
    return True


def lead_number(text: str) -> str | None:
    for match in NUM_RE.finditer(text or ""):
        raw = match.group(0).replace(",", "")
        if re.fullmatch(r"20\d\d", raw):
            continue
        return match.group(0)
    return None


def lead_name(headline: str, famous: list[str]) -> str:
    if famous:
        return famous[0]
    low = (headline or "").lower()
    for name in ("Bad Bunny", "Prabhas", "Kim Kardashian", "Khloe Kardashian", "Elon Musk",
                 "Max Verstappen", "Real Madrid", "Taylor Swift"):
        if name.lower() in low:
            return name
    match = re.match(r"([A-Z][\w'’.]+(?:\s+[A-Z][\w'’.]+){0,2})", headline or "")
    return match.group(1) if match else ""


def stat_lead(text: str) -> str | None:
    fact = reply_fact(text)
    if fact:
        return fact
    for match in NUM_RE.finditer(text or ""):
        raw = match.group(0).replace(",", "")
        if re.fullmatch(r"20\d\d", raw):
            continue
        if raw.isdigit() and int(raw) < 10:
            continue
        return match.group(0)
    return None


def summary_context(headline: str, summary: str) -> str:
    text = clean_sentence(summary or "", 500)
    parts = [part.strip() for part in re.split(r"(?<=[.])\s+", text) if part.strip()]
    head = (headline or "").lower()
    kept = []
    for part in parts:
        low = part.lower()
        if head and (low[:28] in head or head[:28] in low):
            continue
        kept.append(part)
    return clean_sentence(" ".join(kept[:2]), 180)


def news_caption(headline: str, summary: str, category: str, famous: list[str]) -> tuple[str, list[str]]:
    headline = clean_sentence(headline, 160)
    headline = re.sub(r"\bi want it now\b", "", headline, flags=re.I).strip(" .")
    num = stat_lead(headline) or stat_lead(summary or "")
    if num and num.lower() not in headline.lower():
        hook = f"{num} — {headline}"
    else:
        hook = headline
    who = lead_name(headline, famous)
    context = summary_context(headline, summary or "")
    if len(context) < 40:
        contexts = {
            "ai": "The concrete claim is the first line. The photos show the product, not a logo card.",
            "tech": "The product change is the first line. The photos show the thing itself.",
            "sports": "The figure in the first line is the part worth a replay. The photos are from the event.",
            "celebrity": (
                f"{who} is the name on the story. The stills are from the moment, not a poster crop."
                if who else "The name in the first line is the story. The stills are from the moment, not a poster crop."
            ),
            "us_news": "The first line is the US fact. The photos are the evidence, and the rest stays on the source page.",
            "world": "The first line is the fact. The photos are what the source actually ran.",
        }
        context = contexts.get(category, contexts["world"])
    tags = []
    if who:
        tags.append("#" + re.sub(r"[^A-Za-z0-9]", "", who))
    music = bool(re.search(r"\b(album|song|concert|award|billboard|grammy)\b", headline, re.I))
    extra = {
        "ai": "#AI", "tech": "#Tech", "sports": "#Sports",
        "celebrity": "#Music" if music else "#Movies", "us_news": "#News",
    }.get(category)
    if extra:
        tags.append(extra)
    emoji = {"ai": "\U0001F916", "tech": "\U0001F4BB", "sports": "\u26bd", "celebrity": "\U0001F3A4",
             "us_news": "\U0001F4F0"}.get(category, "\U0001F4F0")
    if category == "sports" and re.search(r"\b(nba|lebron|nfl)\b", headline, re.I):
        emoji = "\U0001F3C0" if re.search(r"\b(nba|lebron)\b", headline, re.I) else "\U0001F3C8"
    return build_caption(emoji, hook, context, tags), tags


def too_close(text: str, key: str, corpus: list[str], used_families: set[str]) -> bool:
    fams = families_in(f"{text} {key}")
    if fams & used_families:
        return True
    tok = tokens(text) | tokens(key)
    if not tok:
        return False
    for prev in corpus:
        pt = tokens(prev)
        if not pt:
            continue
        inter = tok & pt
        if len(inter) >= 3:
            return True
        union = tok | pt
        if union and len(inter) / len(union) >= 0.5:
            return True
    return False


def layout_posts(videos: list[dict], photos: list[dict], n: int) -> list[dict | None]:
    viral = [v for v in videos if v.get("type") == "viral"]
    other = [v for v in videos if v.get("type") != "viral"]
    queue = viral[:1] + other + viral[1:]
    slots: list[dict | None] = [None] * n
    if n >= 2 and queue:
        slots[1] = queue.pop(0)
    for i in (0, 3, 2, 4, 5):
        if i < n and slots[i] is None and queue:
            slots[i] = queue.pop(0)
    photo_q = list(photos)
    for i in range(n):
        if slots[i] is None and photo_q:
            slots[i] = photo_q.pop(0)
    for i in range(n):
        if slots[i] is None and queue:
            slots[i] = queue.pop(0)
    return slots


def upcoming_slot_times(now: datetime) -> list[tuple[datetime, datetime, str, str]]:
    ist = now.astimezone(IST)

    def on(day, hhmm: str) -> datetime:
        h, m = (int(x) for x in hhmm.split(":"))
        return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)

    today = ist.date()
    future = []
    for start, end in DAY_SLOTS:
        s, e = on(today, start), on(today, end)
        if s > ist:
            future.append((s, e, start, end))
    if future:
        return future
    tomorrow = today + timedelta(days=1)
    return [(on(tomorrow, s), on(tomorrow, e), s, e) for s, e in DAY_SLOTS]


def next_clock(now: datetime, hhmm: str) -> datetime:
    ist = now.astimezone(IST)
    h, m = (int(x) for x in hhmm.split(":"))
    cand = datetime(ist.year, ist.month, ist.day, h, m, tzinfo=IST)
    if cand <= ist:
        cand += timedelta(days=1)
    return cand


def fetch_bytes(url: str, timeout: int = 25, headers: dict | None = None, max_bytes: int = 0) -> tuple[bytes | None, str]:
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if max_bytes:
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    return None, ctype
            else:
                data = resp.read()
            return data, ctype
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log(f"fetch fail {url[:90]} {exc.__class__.__name__}")
        return None, ""


def head_check(url: str, kind: str) -> bool:
    if forbidden_media_url(url):
        return False
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            code = getattr(resp, "status", 200)
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            length = int(resp.headers.get("Content-Length") or 0)
    except urllib.error.HTTPError as exc:
        if exc.code != 405:
            return False
        data, ctype = fetch_bytes(url, timeout=25, max_bytes=1024)
        if data is None:
            return False
        code, length = 200, 0
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
    if code != 200:
        return False
    if kind == "image":
        return ctype.startswith("image/")
    if kind == "video":
        if ctype != "video/mp4":
            return False
        if length and length > MAX_VIDEO_BYTES:
            return False
        return True
    return False


def head_check_retry(url: str, kind: str, tries: int = 8, pause: float = 4.0) -> bool:
    for i in range(tries):
        if head_check(url, kind):
            return True
        if i + 1 < tries:
            time.sleep(pause)
    return False


def ffprobe_wh(path: Path) -> tuple[int, int]:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)],
            capture_output=True, text=True, timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0, 0
    line = (out.stdout or "").strip().splitlines()
    if not line or "x" not in line[0]:
        return 0, 0
    w, h = line[0].split("x")[:2]
    try:
        return int(w), int(h)
    except ValueError:
        return 0, 0


def ffprobe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    try:
        return float((out.stdout or "").strip() or 0)
    except ValueError:
        return 0.0


def transcode_mp4(src: Path, dst: Path) -> bool:
    vf = "scale='if(gte(iw,ih),-2,720)':'if(gte(iw,ih),720,-2)'"
    for extra in (["-c:a", "aac", "-b:a", "96k"], ["-an"]):
        cmd = [
            "ffmpeg", "-y", "-t", "32", "-i", str(src), "-vf", vf,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "30",
            *extra, "-movflags", "+faststart", str(dst),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if r.returncode == 0 and dst.exists() and 0 < dst.stat().st_size < MAX_VIDEO_BYTES:
            w, h = ffprobe_wh(dst)
            if min(w, h) >= 720 and ffprobe_duration(dst) >= 5:
                return True
        if dst.exists():
            dst.unlink()
    return False


def github_commit_sha(repo: str) -> str:
    data, _ = fetch_bytes(
        f"https://api.github.com/repos/{repo}/commits/main",
        timeout=25,
        headers={"Accept": "application/vnd.github+json", "User-Agent": COMMONS_UA},
    )
    if not data:
        return ""
    try:
        return json.loads(data).get("sha") or ""
    except json.JSONDecodeError:
        return ""


def github_tree(repo: str) -> list[dict]:
    data, _ = fetch_bytes(
        f"https://api.github.com/repos/{repo}/git/trees/main?recursive=1",
        timeout=40,
        headers={"Accept": "application/vnd.github+json", "User-Agent": COMMONS_UA},
    )
    if not data:
        return []
    try:
        tree = json.loads(data).get("tree") or []
    except json.JSONDecodeError:
        return []
    return [t for t in tree if t.get("type") == "blob"]


def media_urls(sha: str, path: str, kind: str) -> str:
    if kind == "video":
        return f"https://cdn.jsdelivr.net/gh/{MEDIA_REPO}@{sha}/{path}"
    return f"https://raw.githubusercontent.com/{MEDIA_REPO}/{sha}/{path}"


def xnc_raw(path: str) -> str:
    return f"https://raw.githubusercontent.com/{XNC_REPO}/main/{path}"


def block_state(now: datetime, ignore_keys: set[str] | None = None) -> tuple[set[str], list[str], set[str]]:
    ignore = {str(k) for k in (ignore_keys or set())}
    posted_list = load_json(DATA / "posted.json", [])
    if not isinstance(posted_list, list):
        posted_list = []
    posted = {str(k) for k in posted_list if str(k) not in ignore}
    phrases = load_json(DATA / "blocklist.json", {}).get("phrases") or []
    history = load_json(DATA / "grok_history.json", {"entries": []})
    cutoff = now - timedelta(hours=48)
    corpus: list[str] = [str(k) for k in posted_list[-50:] if str(k) not in ignore]
    fams: set[str] = set()
    for key in posted_list[-50:]:
        if str(key) in ignore:
            continue
        fams |= families_in(str(key))
        if re.search(r"20\d\d-\d\d-\d\d", str(key)):
            corpus.append(str(key))
    kept = []
    for entry in history.get("entries") or []:
        try:
            at = datetime.fromisoformat(entry["queued_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at < cutoff:
            continue
        if str(entry.get("key") or "") in ignore:
            continue
        kept.append(entry)
        corpus.append(entry.get("text") or "")
        corpus.append(entry.get("key") or "")
        fams |= set(entry.get("families") or [])
        fams |= families_in(entry.get("text") or "")
    for phrase in phrases:
        corpus.append(str(phrase))
    history["entries"] = kept
    return posted, corpus, fams


def phrase_blocked(text: str) -> bool:
    low = f" {(text or '').lower()} "
    phrases = load_json(DATA / "blocklist.json", {}).get("phrases") or []
    for phrase in phrases:
        p = re.sub(r"[^a-z0-9]+", " ", str(phrase).lower()).strip()
        if p and f" {p} " in f" {re.sub(r'[^a-z0-9]+', ' ', low)} ":
            return True
    return False


def source_of(item: dict) -> str:
    for url in item.get("article_urls") or []:
        if url.startswith("http") and "news.google.com" not in url:
            return url
    for url in item.get("video_urls") or []:
        if "youtube" in url or "youtu.be" in url:
            return url
    urls = item.get("article_urls") or []
    return urls[0] if urls else ""


def image_sources(item: dict, ready: dict) -> list[str]:
    urls: list[str] = []
    info = (ready.get("items") or {}).get(item.get("key") or "")
    if isinstance(info, dict):
        for photo in info.get("photos") or []:
            src = photo.get("source_url")
            if src:
                urls.append(src)
        for url in info.get("photo_urls") or []:
            if url and "github.io/chad-media/m/" not in url:
                urls.append(url)
    for url in item.get("image_urls") or []:
        urls.append(url)
    out = []
    for url in urls:
        if not url or not url.startswith("http"):
            continue
        if any(bad in url for bad in BAD_MEDIA_HOSTS):
            continue
        if forbidden_media_url(url):
            continue
        if url not in out:
            out.append(url)
    return out[:8]


def save_jpeg(blob: bytes, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = dest.with_suffix(".bin")
    raw.write_bytes(blob)
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(raw), "-frames:v", "1", "-q:v", "3", str(dest)],
            capture_output=True, text=True, timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired):
        raw.unlink(missing_ok=True)
        return False
    raw.unlink(missing_ok=True)
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size < 8000:
        dest.unlink(missing_ok=True)
        return False
    w, h = ffprobe_wh(dest)
    if min(w, h) < 640:
        dest.unlink(missing_ok=True)
        return False
    return True


def prepare_photo_post(item: dict, ready: dict, work: Path, used_names: set[str]) -> dict | None:
    text = f"{item.get('headline') or ''} {item.get('summary') or ''}"
    if phrase_blocked(text):
        return None
    files = []
    for url in image_sources(item, ready):
        if len(files) >= 2:
            break
        blob, ctype = fetch_bytes(url, timeout=20, max_bytes=8_000_000)
        if not blob or not (ctype.startswith("image/") or blob[:3] == b"\xff\xd8\xff" or blob[:8] == b"\x89PNG\r\n\x1a\n"):
            continue
        name = f"{slug(item.get('key') or item.get('headline') or 'photo')}-p{len(files)+1}.jpg"
        if name in used_names:
            continue
        dest = work / name
        if save_jpeg(blob, dest):
            files.append(dest)
            used_names.add(name)
    if len(files) < 2:
        for f in files:
            f.unlink(missing_ok=True)
        return None
    famous = list(item.get("famous_names") or [])
    category = classify(text)
    caption, _tags = news_caption(item.get("headline") or "", item.get("summary") or "", category, famous)
    if not caption_ok(caption):
        return None
    return {
        "key": item.get("key") or slug(item.get("headline") or "photo"),
        "type": "news",
        "caption": caption,
        "source": source_of(item),
        "media_kind": "image",
        "local_files": files,
        "remote_paths": [f"m/grok/{f.name}" for f in files],
        "families": sorted(families_in(text)),
        "category": category,
        "war": is_war(text),
        "text": text,
    }


def score_item(item: dict) -> float:
    text = f"{item.get('headline') or ''} {item.get('summary') or ''}"
    score = float(item.get("trend_score") or 0)
    if PREF_RE.search(text):
        score += 30
    if re.search(r"\b(jail|gross comment|baby daddy|dating)\b", text, re.I):
        score -= 25
    if item.get("famous_names"):
        score += 8
    if is_war(text):
        score -= 40
    if item.get("local_only") or LOCAL_RE.search(text):
        score -= 25
    cat = classify(text)
    if cat in {"ai", "tech", "sports", "celebrity", "us_news"}:
        score += 12
    return score


def commons_search(query: str) -> list[dict]:
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": "6",
        "gsrlimit": "12",
        "prop": "imageinfo",
        "iiprop": "url|size|mime",
    })
    data, _ = fetch_bytes(url, timeout=30, headers={"User-Agent": COMMONS_UA})
    if not data:
        return []
    try:
        pages = (json.loads(data).get("query") or {}).get("pages") or {}
    except json.JSONDecodeError:
        return []
    out = []
    for page in pages.values():
        if not isinstance(page, dict):
            continue
        info = (page.get("imageinfo") or [{}])[0]
        mime = info.get("mime") or ""
        if "video" not in mime and mime not in {"application/ogg", "video/webm", "video/mp4"}:
            continue
        title = page.get("title") or ""
        if MILITARY_RE.search(title) or WAR_RE.search(title):
            continue
        out.append({
            "title": title,
            "url": info.get("url") or "",
            "page": info.get("descriptionurl") or "",
            "size": info.get("size") or 0,
            "width": info.get("width") or 0,
            "height": info.get("height") or 0,
            "duration": info.get("duration") or 0,
            "mime": mime,
        })
    return out


def pick_commons(query: str, lane: str) -> dict | None:
    best = None
    best_score = -1
    for item in commons_search(query):
        w, h = int(item["width"] or 0), int(item["height"] or 0)
        dur = float(item["duration"] or 0)
        size = int(item["size"] or 0)
        if min(w, h) < 720 or dur < 6 or dur > 90 or size <= 0 or size > 28_000_000:
            continue
        if not item["url"]:
            continue
        score = 1000 - abs(dur - 18) - size / 1_000_000
        if score > best_score:
            best, best_score = item, score
    if not best:
        return None
    best["lane"] = lane
    return best


def prepare_commons_video(item: dict, work: Path) -> dict | None:
    title = re.sub(r"^File:", "", item["title"])
    title = re.sub(r"\.(webm|ogv|ogg|mp4|mpg)$", "", title, flags=re.I)
    title = title.replace("_", " ")
    key = slug(f"{item['lane']}-{title}")
    raw = work / f"{key}.src"
    blob, _ctype = fetch_bytes(item["url"], timeout=50, max_bytes=28_000_000,
                               headers={"User-Agent": COMMONS_UA})
    if not blob:
        return None
    raw.write_bytes(blob)
    dst = work / f"{key}.mp4"
    ok = transcode_mp4(raw, dst)
    raw.unlink(missing_ok=True)
    if not ok:
        return None
    dur = int(round(ffprobe_duration(dst)))
    place = clean_sentence(title, 80)
    if item["lane"] == "place":
        nice = re.sub(r"^Dance of the ", "", place)
        hook = f"{dur} seconds of {nice}"
        if re.search(r"light|aurora", nice, re.I):
            context = "The color is the post. It is a real sky, not a filter laid on afterwards."
        elif re.search(r"fall|cascade|water", nice, re.I):
            context = "The water is the post. Short, loud, and filmed on the rock."
        else:
            context = "Filmed on location. The place is the whole post, not a headline card."
        emoji = "\U0001F30D"
        tags = ["#Travel", "#World"]
    else:
        hook = f"{dur} seconds, and the goal is already in at {place}"
        context = (
            "Football skill and goal clips travel further than another match recap. "
            "The finish above is the whole post."
        )
        emoji = "\u26bd"
        tags = ["#Football", "#Goal"]
    caption = build_caption(emoji, hook, context, tags)
    if not caption_ok(caption):
        dst.unlink(missing_ok=True)
        return None
    return {
        "key": key,
        "type": "viral",
        "caption": caption,
        "source": item.get("page") or item["url"],
        "media_kind": "video",
        "local_files": [dst],
        "remote_paths": [f"v/grok/{dst.name}"],
        "families": sorted(families_in(title)),
        "category": "sports" if item["lane"] == "football" else "viral",
        "war": False,
        "text": title,
    }


def hosted_video_post(spec: dict, sha: str, tree_paths: dict[str, int]) -> dict | None:
    path = spec["path"]
    size = tree_paths.get(path)
    if size is None or size <= 0 or size > MAX_VIDEO_BYTES:
        return None
    url = media_urls(sha, path, "video")
    if not head_check(url, "video"):
        log(f"hosted video failed HEAD {path}")
        return None
    caption = build_caption(spec["emoji"], spec["hook"], spec["context"], spec["hashtags"])
    if not caption_ok(caption):
        return None
    return {
        "key": spec["key"],
        "type": "viral",
        "caption": caption,
        "source": spec["source"],
        "media_kind": "video",
        "media": [url],
        "local_files": [],
        "remote_paths": [],
        "families": spec["families"],
        "category": "sports",
        "war": False,
        "text": spec["headline"],
    }


def reply_fact(text: str) -> str:
    match = re.search(
        r"(\$?\d[\d,.]*)(?:\s*[- ]\s*|\s+)(billion|million|point|points|percent)",
        text or "",
        re.I,
    )
    if not match:
        return ""
    number = match.group(1)
    unit = match.group(2).lower()
    if unit == "points":
        unit = "point"
    return f"{number} {unit}"


def reply_name(text: str) -> str:
    low = (text or "").lower()
    for name in REPLY_NAMES:
        if name.lower() in low:
            return name
    return ""


def reply_text(text: str, category: str, account: str, status_id: str) -> str:
    fact = reply_fact(text)
    name = "" if fact else reply_name(text)
    if not fact and not name:
        name = {
            "nasa": "NASA",
            "spacex": "SpaceX",
            "nvidia": "Nvidia",
            "openai": "OpenAI",
            "netflix": "Netflix",
            "youtube": "YouTube",
            "google": "Google",
            "ufc": "UFC",
            "f1": "F1",
            "anthropicai": "Anthropic",
        }.get(account.lower(), "")
    idx = int(status_id) % 2
    if fact:
        patterns = FACT_PATTERNS.get(category) or FACT_PATTERNS["world"]
        out = patterns[idx].format(t=fact)
    elif name:
        patterns = REPLY_PATTERNS.get(category) or REPLY_PATTERNS["world"]
        out = patterns[idx].format(t=name)
    else:
        choices = GENERIC_REPLIES.get(category) or GENERIC_REPLIES["world"]
        out = choices[idx]
    out = clean_sentence(out, 160)
    out = re.sub(r"\b(\w+)(?: \1\b)+", r"\1", out, flags=re.I)
    if not out or "?" in out or "http" in out.lower() or "#" in out:
        return ""
    if re.search(r"\b(?!NASA\b|NBA\b|NFL\b|UFC\b|AI\b)[A-Z]{4,}\b", out):
        return ""
    return out[0].upper() + out[1:]


def topic_of(text: str, account: str) -> str:
    low = (text or "").lower()
    for word in TOPIC_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", low):
            return word
    return account.lower()


def profile_status_ids(account: str) -> list[str]:
    ids: list[str] = []
    for attempt in range(3):
        data, _ = fetch_bytes(
            f"https://r.jina.ai/https://x.com/{account}",
            timeout=22,
            headers={"User-Agent": UA, "Accept": "text/plain"},
        )
        if data:
            text = data.decode("utf-8", "replace")
            for user, sid in STATUS_RE.findall(text):
                if user.lower() != account.lower():
                    continue
                if sid not in ids:
                    ids.append(sid)
        if len(ids) >= 2 or attempt == 2:
            break
        time.sleep(0.6 * (attempt + 1))
    ids.sort(key=lambda sid: int(sid), reverse=True)
    return ids[:3]


def as_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        inner = value.get("text")
        if isinstance(inner, str):
            return inner
    return ""


def fetch_tweet(account: str, sid: str, default_cat: str) -> dict | None:
    data, _ = fetch_bytes(f"https://api.fxtwitter.com/{account}/status/{sid}", timeout=18)
    if not data:
        return None
    try:
        tweet = json.loads(data).get("tweet") or {}
    except json.JSONDecodeError:
        return None
    author = tweet.get("author") or {}
    ver = author.get("verification") or {}
    if not ver.get("verified"):
        return None
    if tweet.get("replying_to"):
        return None
    lang = (tweet.get("lang") or "en").lower()
    if lang not in {"en", "und", "qme", "art", ""}:
        return None
    text = as_text(tweet.get("text")) or as_text(tweet.get("raw_text"))
    if SKIP_TWEET.search(text) or is_war(text):
        return None
    if re.search(
        r"\b(voter id|voting fraud|election fraud|ebola|massacre|shooting|"
        r"pornograph|hookups?|killed|dead|dies|death|died|murder)\b",
        text,
        re.I,
    ):
        return None
    words = re.findall(r"[A-Za-z0-9']+", re.sub(r"https?://\S+|[@#]\w+", " ", text))
    created = tweet.get("created_timestamp")
    if not created:
        return None
    age = (time.time() - float(created)) / 60
    views = int(tweet.get("views") or 0)
    if age < 0 or age > 24 * 60:
        return None
    if len(words) < 5 and views < 100000:
        return None
    category = classify(text)
    if category == "world" and default_cat in {"ai", "tech", "sports", "celebrity"}:
        ai_hit = re.search(r"\b(ai|grok|openai|gemini|claude|anthropic|tesla|spacex|robot)\b", text, re.I)
        if default_cat != "ai" or ai_hit:
            category = default_cat
    reply = reply_text(text, category, account, sid)
    if not reply:
        return None
    return {
        "post_url": tweet.get("url") or f"https://x.com/{account}/status/{sid}",
        "reply": reply,
        "account": author.get("screen_name") or account,
        "verified": True,
        "views": views,
        "age_minutes": int(age),
        "category": category,
        "topic": topic_of(text, account),
    }


def pick_replies(pool: list[dict], n: int = 6) -> list[dict]:
    pool = sorted(pool, key=lambda p: (p["age_minutes"] > 180, -p["views"], p["age_minutes"]))
    picked: list[dict] = []

    def add_from(max_age: int, min_views: int) -> None:
        topics: dict[str, int] = {}
        accounts: dict[str, int] = {}
        cats: dict[str, int] = {}
        for item in picked:
            topics[item["topic"]] = topics.get(item["topic"], 0) + 1
            accounts[item["account"].lower()] = accounts.get(item["account"].lower(), 0) + 1
            cats[item["category"]] = cats.get(item["category"], 0) + 1
        urls = {p["post_url"] for p in picked}
        for item in pool:
            if len(picked) >= n:
                return
            if item["post_url"] in urls:
                continue
            if item["age_minutes"] > max_age or item["views"] < min_views:
                continue
            if topics.get(item["topic"], 0) >= 2:
                continue
            if accounts.get(item["account"].lower(), 0) >= 2:
                continue
            need_cat = len({p["category"] for p in picked}) < 4 and len(picked) >= n - 2
            if need_cat and item["category"] in cats and any(c not in cats for c in (x["category"] for x in pool)):
                continue
            picked.append(item)
            urls.add(item["post_url"])
            topics[item["topic"]] = topics.get(item["topic"], 0) + 1
            accounts[item["account"].lower()] = accounts.get(item["account"].lower(), 0) + 1
            cats[item["category"]] = cats.get(item["category"], 0) + 1

    for age, views in ((180, 20000), (180, 1000), (360, 5000), (720, 1000), (1440, 500)):
        add_from(age, views)
        if len(picked) >= n and len({p["category"] for p in picked}) >= 4:
            break
    if len({p["category"] for p in picked}) < 4:
        have = {p["category"] for p in picked}
        used = {p["post_url"] for p in picked}
        for alt in pool:
            if alt["category"] in have or alt["post_url"] in used:
                continue
            if not picked:
                break
            dupes = [p for p in picked if sum(1 for q in picked if q["category"] == p["category"]) > 1]
            if not dupes:
                break
            drop = min(dupes, key=lambda p: p["views"])
            picked.remove(drop)
            picked.append(alt)
            have.add(alt["category"])
            used.add(alt["post_url"])
            if len(have) >= 4:
                break
    return picked[:n]


def fresh_reply(item: dict, slot: list[dict]) -> str:
    used = {p["reply"].lower() for p in slot}
    current = item["reply"]
    if current.lower() not in used:
        return current
    cat = item["category"]
    for line in GENERIC_REPLIES.get(cat) or GENERIC_REPLIES["world"]:
        text = line[0].upper() + line[1:]
        if text.lower() not in used:
            return text
    return current


def split_reply_slots(pool: list[dict]) -> tuple[list[dict], list[dict]]:
    """Two lists of 6. Each prefers a spread of categories, max 2 per topic and account."""
    ranked = sorted(pool, key=lambda p: (p["age_minutes"] > 180, -p["views"], p["age_minutes"]))
    by_cat: dict[str, list[dict]] = {}
    for item in ranked:
        by_cat.setdefault(item["category"], []).append(item)
    hands: list[list[dict]] = [[], []]
    topic_counts: list[dict[str, int]] = [{}, {}]
    account_counts: list[dict[str, int]] = [{}, {}]
    used: set[str] = set()

    def take(idx: int, item: dict, protect_spread: bool) -> bool:
        slot = hands[idx]
        if len(slot) >= 6 or item["post_url"] in used:
            return False
        if topic_counts[idx].get(item["topic"], 0) >= 2:
            return False
        if account_counts[idx].get(item["account"].lower(), 0) >= 2:
            return False
        have = {p["category"] for p in slot}
        if protect_spread and len(have) < 4 and item["category"] in have:
            unused = [
                c for c in PREFERRED_REPLY_CATS
                if c not in have and any(x["post_url"] not in used for x in by_cat.get(c, []))
            ]
            if unused:
                return False
        chosen = dict(item)
        chosen["reply"] = fresh_reply(item, slot)
        slot.append(chosen)
        used.add(item["post_url"])
        topic_counts[idx][item["topic"]] = topic_counts[idx].get(item["topic"], 0) + 1
        account_counts[idx][item["account"].lower()] = account_counts[idx].get(item["account"].lower(), 0) + 1
        return True

    for cat in sorted(PREFERRED_REPLY_CATS, key=lambda name: len(by_cat.get(name, []))):
        for idx in (0, 1):
            for item in by_cat.get(cat, []):
                if take(idx, item, False):
                    break
    for max_age, min_views in ((180, 5000), (360, 2000), (720, 1000), (1440, 500)):
        for item in ranked:
            if item["category"] not in PREFERRED_REPLY_CATS:
                continue
            if item["age_minutes"] > max_age or item["views"] < min_views:
                continue
            for idx in (0, 1):
                take(idx, item, True)
    for item in ranked:
        if item["category"] == "world" and item["views"] >= 5000:
            for idx in (0, 1):
                have = {p["category"] for p in hands[idx]}
                if len(have) >= 4 and len(hands[idx]) >= 6:
                    continue
                if len(have) >= 4:
                    continue
                take(idx, item, False)
    return hands[0], hands[1]


def build_replies(now: datetime) -> list[dict]:
    found: list[dict] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        id_jobs = {pool.submit(profile_status_ids, acct): (acct, cat) for acct, cat in REPLY_ACCOUNTS}
        pairs = []
        for fut in as_completed(id_jobs):
            acct, cat = id_jobs[fut]
            try:
                ids = fut.result()
            except Exception as exc:  # noqa: BLE001
                log(f"profile fail {acct} {exc}")
                continue
            for sid in ids:
                pairs.append((acct, sid, cat))
        tweet_jobs = {pool.submit(fetch_tweet, a, s, c): (a, s) for a, s, c in pairs}
        for fut in as_completed(tweet_jobs):
            try:
                tweet = fut.result()
            except Exception as exc:  # noqa: BLE001
                log(f"tweet fail {exc}")
                continue
            if tweet:
                found.append(tweet)
    counts: dict[str, int] = {}
    for tweet in found:
        counts[tweet["category"]] = counts.get(tweet["category"], 0) + 1
    log(f"reply candidates {len(found)} cats {counts}")
    hands = split_reply_slots(found)
    slots = []
    for clock, chosen in zip(REPLY_CLOCKS, hands):
        when = next_clock(now, clock)
        slots.append({
            "time_ist": when.isoformat(),
            "label": f"{clock} IST",
            "replies": [
                {
                    "post_url": t["post_url"],
                    "reply": t["reply"],
                    "account": t["account"],
                    "verified": True,
                    "views": t["views"],
                    "age_minutes": t["age_minutes"],
                    "category": t["category"],
                    "topic": t["topic"],
                }
                for t in chosen
            ],
        })
    return slots


def run_git(cwd: Path, args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=180)


def scrub(text: str) -> str:
    if "PRIVATE KEY" in text or "BEGIN OPENSSH" in text:
        return "[redacted]"
    return text[-500:]


def publish_chad_media(files: list[tuple[str, Path]]) -> str | None:
    key = os.environ.get("CHAD_MEDIA_DEPLOY_KEY") or ""
    if not key.strip() or not files:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="chad-key-"))
    key_path = tmp / "key"
    key_path.write_text(key if key.endswith("\n") else key + "\n", encoding="utf-8")
    key_path.chmod(0o600)
    work = Path(tempfile.mkdtemp(prefix="chad-media-"))
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = (
        f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    )
    env["GIT_TERMINAL_PROMPT"] = "0"
    clone = run_git(work, [
        "git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse",
        f"git@github.com:{MEDIA_REPO}.git", "repo",
    ], env)
    if clone.returncode != 0:
        log("chad-media clone failed " + scrub(clone.stderr or ""))
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
        return None
    repo = work / "repo"
    run_git(repo, ["git", "sparse-checkout", "set", "v/grok", "m/grok", ".github/workflows"], env)
    for rel, src in files:
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    run_git(repo, ["git", "config", "user.name", "x-news-collector[bot]"], env)
    run_git(repo, ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], env)
    add = run_git(repo, ["git", "add", "--sparse", "--", *[rel for rel, _ in files]], env)
    if add.returncode != 0:
        log("chad-media add failed " + scrub(add.stderr or ""))
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
        return None
    commit = run_git(repo, ["git", "commit", "-q", "-m",
                            f"Add Grok slot media {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}"], env)
    if commit.returncode != 0:
        log("chad-media nothing to commit " + scrub(commit.stderr or ""))
        sha = run_git(repo, ["git", "rev-parse", "HEAD"], env)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
        return (sha.stdout or "").strip() or None
    pushed = None
    for attempt in range(1, 4):
        pull = run_git(repo, ["git", "pull", "-q", "--rebase", "-X", "theirs", "origin", "main"], env)
        push = run_git(repo, ["git", "push", "-q", "origin", "HEAD:main"], env)
        if pull.returncode == 0 and push.returncode == 0:
            pushed = (run_git(repo, ["git", "rev-parse", "HEAD"], env).stdout or "").strip()
            break
        log(f"chad-media push retry {attempt} " + scrub((push.stderr or pull.stderr or "")))
        time.sleep(4)
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    return pushed


def patch_pages_workflow(text: str) -> str | None:
    """Point the Pages copy loop at the Grok JSON files and stamp it so the site redeploys."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if "grok-feed-stamp:" in text:
        return re.sub(r"# grok-feed-stamp:.*", f"# grok-feed-stamp: {stamp}", text, count=1)
    marker = "for f in candidates.json media_ready.json posted.json"
    idx = text.find(marker)
    if idx < 0:
        return None
    line_start = text.rfind("\n", 0, idx) + 1
    done_at = text.find("done", idx)
    if done_at < 0:
        return None
    line_end = text.find("\n", done_at)
    if line_end < 0:
        line_end = len(text)
    indent = text[line_start:idx]
    block = (
        f"{indent}for f in candidates.json media_ready.json posted.json grok_queue.json grok_replies.json; do\n"
        f"{indent}  if [ -f \"xnc/data/$f\" ]; then cp \"xnc/data/$f\" site/feed/; fi\n"
        f"{indent}  if [ ! -s \"site/feed/$f\" ]; then\n"
        f"{indent}    curl -fsSL \"https://raw.githubusercontent.com/chatgpt820970/x-news-collector/main/data/$f\" -o \"site/feed/$f\" || true\n"
        f"{indent}  fi\n"
        f"{indent}done\n"
        f"{indent}# grok-feed-stamp: {stamp}"
    )
    return text[:line_start] + block + text[line_end:]


def publish_pages_hook() -> int:
    key = os.environ.get("CHAD_MEDIA_DEPLOY_KEY") or ""
    if not key.strip():
        log("CHAD_MEDIA_DEPLOY_KEY not set, Pages feed copy skipped.")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="chad-key-"))
    key_path = tmp / "key"
    key_path.write_text(key if key.endswith("\n") else key + "\n", encoding="utf-8")
    key_path.chmod(0o600)
    work = Path(tempfile.mkdtemp(prefix="chad-pages-"))
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = (
        f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    )
    env["GIT_TERMINAL_PROMPT"] = "0"
    clone = run_git(work, [
        "git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse",
        f"git@github.com:{MEDIA_REPO}.git", "repo",
    ], env)
    if clone.returncode != 0:
        log("pages hook clone failed " + scrub(clone.stderr or ""))
        return 0
    repo = work / "repo"
    run_git(repo, ["git", "sparse-checkout", "set", ".github/workflows"], env)
    yml = repo / ".github/workflows/publish-pages.yml"
    if not yml.exists():
        log("publish-pages.yml missing, skip")
        return 0
    text = yml.read_text(encoding="utf-8")
    patched = patch_pages_workflow(text)
    if patched is None:
        log("Pages workflow copy loop not found, skip")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
        return 0
    yml.write_text(patched, encoding="utf-8")
    run_git(repo, ["git", "config", "user.name", "x-news-collector[bot]"], env)
    run_git(repo, ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], env)
    run_git(repo, ["git", "add", "--", ".github/workflows/publish-pages.yml"], env)
    commit = run_git(repo, ["git", "commit", "-q", "-m", "Publish Grok queue on the Pages feed"], env)
    if commit.returncode != 0:
        log("pages hook commit skipped " + scrub(commit.stderr or ""))
        return 0
    ok = False
    for attempt in range(1, 4):
        pull = run_git(repo, ["git", "pull", "-q", "--rebase", "-X", "theirs", "origin", "main"], env)
        push = run_git(repo, ["git", "push", "-q", "origin", "HEAD:main"], env)
        if pull.returncode == 0 and push.returncode == 0:
            ok = True
            break
        log(f"pages hook push retry {attempt}")
        time.sleep(4)
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    log("Pages feed hook pushed." if ok else "Pages feed hook failed.")
    return 0


def append_posted(keys: list[str]) -> None:
    path = DATA / "posted.json"
    current = load_json(path, [])
    if not isinstance(current, list):
        current = []
    have = {str(k) for k in current}
    add = [k for k in keys if k not in have]
    if not add:
        return
    text = path.read_text(encoding="utf-8").rstrip()
    if not text.endswith("]"):
        current.extend(add)
        path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return
    body = text[:-1].rstrip()
    if not body.endswith(",") and not body.endswith("["):
        body += ","
    extra = "\n".join("  " + json.dumps(k, ensure_ascii=False) + "," for k in add).rstrip(",")
    path.write_text(body + "\n" + extra + "\n]\n", encoding="utf-8")


def remember(posts: list[dict], now: datetime, slot_by_key: dict[str, str]) -> None:
    history = load_json(DATA / "grok_history.json", {"window_hours": 48, "entries": []})
    entries = history.get("entries") or []
    have = {e.get("key") for e in entries}
    for post in posts:
        if post["key"] in have:
            continue
        entries.append({
            "key": post["key"],
            "text": post.get("text") or post["caption"],
            "families": post.get("families") or [],
            "queued_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "slot": slot_by_key.get(post["key"], ""),
        })
    cutoff = now - timedelta(hours=48)
    kept = []
    for entry in entries:
        try:
            at = datetime.fromisoformat(entry["queued_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at >= cutoff:
            kept.append(entry)
    history["window_hours"] = 48
    history["entries"] = kept
    (DATA / "grok_history.json").write_text(
        json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def attach_urls(post: dict, sha: str, fallback_dir: Path | None) -> dict | None:
    if post.get("media"):
        return post
    urls = []
    kind = post["media_kind"]
    if sha:
        for rel, src in zip(post["remote_paths"], post["local_files"]):
            url = media_urls(sha, rel, "video" if kind == "video" else "image")
            if head_check_retry(url, "video" if kind == "video" else "image"):
                urls.append(url)
            else:
                log(f"HEAD failed {url}")
    elif fallback_dir is not None and kind == "image":
        fallback_dir.mkdir(parents=True, exist_ok=True)
        for src in post["local_files"]:
            dest = fallback_dir / src.name
            shutil.copy2(src, dest)
            urls.append(xnc_raw(f"data/grok_media/{dest.name}"))
    if kind == "video" and len(urls) != 1:
        return None
    if kind == "image" and len(urls) < 2:
        return None
    post["media"] = urls
    return post


def verify_file() -> int:
    path = DATA / "grok_queue.json"
    doc = load_json(path, None)
    if not isinstance(doc, dict):
        log("no queue to verify")
        return 1
    kept_slots = []
    for slot in doc.get("slots") or []:
        posts = []
        for post in slot.get("posts") or []:
            kind = "video" if post.get("media_kind") == "video" else "image"
            ok = True
            for url in post.get("media") or []:
                if not head_check_retry(url, kind, tries=4, pause=3):
                    log(f"drop unverified {post.get('key')} {url}")
                    ok = False
                    break
            if ok and post.get("media") and caption_ok(post.get("caption") or ""):
                posts.append(post)
        if posts:
            slot["posts"] = posts
            kept_slots.append(slot)
    doc["slots"] = kept_slots
    doc["verified"] = True
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"verified slots {len(kept_slots)}")
    return 0 if kept_slots else 1


def reusable_post(post: dict, slot_start: datetime, generated_at: datetime | None, now: datetime) -> bool:
    if not post or not post.get("media") or not caption_ok(post.get("caption") or ""):
        return False
    kind = "video" if post.get("media_kind") == "video" else "image"
    if not all(head_check(url, kind) for url in post["media"]):
        return False
    if slot_start - now <= timedelta(minutes=LOCK_MINUTES):
        return True
    if generated_at and now - generated_at < timedelta(hours=REFRESH_HOURS):
        return True
    return False


def select_posts(now: datetime, posted: set[str], corpus: list[str], fams: set[str], sha: str,
                 tree_paths: dict[str, int], work: Path) -> list[dict]:
    ready = load_json(DATA / "media_ready.json", {})
    candidates = load_json(DATA / "candidates.json", {}).get("items") or []
    ranked = sorted(candidates, key=score_item, reverse=True)
    videos: list[dict] = []
    used_fams = set(fams)
    corpus = list(corpus)

    def accept(post: dict) -> bool:
        if post["key"] in posted:
            return False
        if phrase_blocked(post.get("text") or "") or phrase_blocked(post["key"]):
            return False
        if too_close(post.get("text") or "", post["key"], corpus, used_fams):
            return False
        if post.get("war"):
            return False
        return True

    def take(post: dict) -> None:
        videos.append(post)
        used_fams.update(post.get("families") or [])
        corpus.append(post.get("text") or "")
        corpus.append(post["key"])

    for spec in HOSTED_VIRAL:
        post = hosted_video_post(spec, sha, tree_paths)
        if post and accept(post):
            take(post)
            break
    if len(videos) < 3:
        for query, lane in (
            ("northern lights filetype:video", "place"),
            ("waterfall filetype:video", "place"),
            ("soccer goal filetype:video", "football"),
        ):
            if len(videos) >= 3:
                break
            found = pick_commons(query, lane)
            if not found:
                log(f"no commons hit for {query}")
                continue
            post = prepare_commons_video(found, work)
            if post and accept(post):
                take(post)
    photos: list[dict] = []
    war_used = False
    names: set[str] = set()
    for item in ranked:
        if len(photos) >= 4:
            break
        key = item.get("key") or ""
        text = f"{item.get('headline') or ''} {item.get('summary') or ''}"
        if not key or key in posted or phrase_blocked(text):
            continue
        if too_close(text, key, corpus, used_fams):
            continue
        if is_war(text):
            if war_used:
                continue
        post = prepare_photo_post(item, ready, work, names)
        if not post:
            continue
        if post.get("war"):
            war_used = True
        photos.append(post)
        used_fams.update(post.get("families") or [])
        corpus.append(text)
        corpus.append(key)
    return videos, photos, used_fams


def build(dry: bool) -> int:
    now = datetime.now(timezone.utc)
    existing = load_json(DATA / "grok_queue.json", {})
    ignore_keys: set[str] = set()
    if existing.get("caption_version") != CAPTION_VERSION:
        ignore_keys = {str(k) for k in (existing.get("queued_keys") or [])}
    posted, corpus, fams = block_state(now, ignore_keys)
    sha = github_commit_sha(MEDIA_REPO)
    tree = github_tree(MEDIA_REPO)
    tree_paths = {t["path"]: int(t.get("size") or 0) for t in tree}
    log(f"chad-media {sha[:12] if sha else 'no-sha'} files {len(tree_paths)}")
    work = Path(tempfile.mkdtemp(prefix="grok-build-"))
    reply_pool = ThreadPoolExecutor(max_workers=1)
    reply_fut = reply_pool.submit(build_replies, now)
    videos, photos, _fams = select_posts(now, posted, corpus, fams, sha, tree_paths, work)
    slots_meta = upcoming_slot_times(now)
    generated_at = None
    if existing.get("generated_at_utc"):
        try:
            generated_at = datetime.fromisoformat(existing["generated_at_utc"])
            if generated_at.tzinfo is None:
                generated_at = generated_at.replace(tzinfo=timezone.utc)
        except ValueError:
            generated_at = None
    existing_by_slot = {}
    for slot in existing.get("slots") or []:
        posts = slot.get("posts") or []
        if posts:
            existing_by_slot[slot.get("slot_ist")] = posts[0]

    chosen: list[tuple[tuple, dict | None]] = []
    needed = 0
    for meta in slots_meta:
        start = meta[0]
        prev = None if dry else existing_by_slot.get(start.isoformat())
        if existing.get("caption_version") != CAPTION_VERSION:
            prev = None
        if prev and reusable_post(prev, start, generated_at, now):
            chosen.append((meta, prev))
        else:
            chosen.append((meta, None))
            needed += 1
    fresh_layout = layout_posts(videos, photos, sum(1 for _m, post in chosen if post is None))
    fi = 0
    arranged = []
    for meta, prev in chosen:
        if prev is not None:
            arranged.append((meta, prev))
        else:
            arranged.append((meta, fresh_layout[fi] if fi < len(fresh_layout) else None))
            fi += 1

    upload: list[tuple[str, Path]] = []
    for _meta, post in arranged:
        if not post or post.get("media"):
            continue
        for rel, src in zip(post.get("remote_paths") or [], post.get("local_files") or []):
            upload.append((rel, src))
    new_sha = None if dry else publish_chad_media(upload)
    use_sha = new_sha or sha
    fallback = None if (dry or new_sha or not upload) else (DATA / "grok_media")
    if new_sha:
        log(f"published media {new_sha[:12]}")
    elif upload and not dry:
        log("chad-media publish unavailable, images fall back to this repo")

    final_slots = []
    final_posts = []
    for meta, post in arranged:
        start, end, _a, _b = meta
        if dry and post and not post.get("media"):
            post["media"] = [f"https://dry.invalid/{src.name}" for src in post.get("local_files") or []]
        elif post and not post.get("media"):
            post = attach_urls(post, new_sha or ("" if fallback is not None else use_sha), fallback)
        if not post or not post.get("media"):
            continue
        if post.get("media_kind") == "video" and len(post["media"]) != 1:
            continue
        if post.get("media_kind") != "video" and len(post["media"]) < 2:
            continue
        if any(forbidden_media_url(u) for u in post["media"]):
            continue
        public = {
            "key": post["key"],
            "type": post["type"],
            "caption": post["caption"],
            "media": post["media"],
            "media_kind": post["media_kind"],
            "source": post.get("source") or "",
        }
        final_slots.append({
            "slot_ist": start.isoformat(),
            "slot_end_ist": end.isoformat(),
            "slot_time": start.strftime("%H:%M"),
            "window": f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')} IST",
            "posts": [public],
        })
        final_posts.append(post)
    try:
        replies = reply_fut.result(timeout=150)
    except Exception as exc:  # noqa: BLE001
        log(f"replies failed {exc}")
        replies = []
    reply_pool.shutdown(wait=False)
    videos_n = sum(1 for p in final_posts if p.get("media_kind") == "video")
    viral_n = sum(1 for p in final_posts if p.get("type") == "viral")
    doc = {
        "caption_version": CAPTION_VERSION,
        "generated_at_utc": now.isoformat(timespec="seconds"),
        "generated_at_ist": now.astimezone(IST).strftime("%Y-%m-%d %H:%M IST"),
        "timezone": "Asia/Kolkata",
        "buffer_channel": "chadriyo",
        "media_commit": use_sha,
        "instructions": list(INSTRUCTIONS),
        "mix": {
            "posts": len(final_posts),
            "videos": videos_n,
            "viral": viral_n,
            "photo_posts": len(final_posts) - videos_n,
        },
        "slots": final_slots,
        "queued_keys": [p["key"] for p in final_posts],
    }
    replies_doc = {
        "generated_at_utc": now.isoformat(timespec="seconds"),
        "generated_at_ist": now.astimezone(IST).strftime("%Y-%m-%d %H:%M IST"),
        "timezone": "Asia/Kolkata",
        "buffer_channel": "chadriyo",
        "rules": "6 targets, max 2 per topic, at least 4 categories, verified accounts, no links in the reply text.",
        "slots": replies,
    }
    if dry:
        out = Path("/tmp/grok-preview")
        out.mkdir(parents=True, exist_ok=True)
        (out / "grok_queue.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (out / "grok_replies.json").write_text(json.dumps(replies_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log(f"dry-run wrote /tmp/grok-preview mix={doc['mix']}")
    else:
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / "grok_queue.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (DATA / "grok_replies.json").write_text(json.dumps(replies_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        slot_by_key = {}
        for slot in final_slots:
            for post in slot["posts"]:
                slot_by_key[post["key"]] = slot["slot_ist"]
        remember(final_posts, now, slot_by_key)
        append_posted([p["key"] for p in final_posts])
        log(f"wrote queue mix={doc['mix']}")
    shutil.rmtree(work, ignore_errors=True)
    if videos_n < 2 or viral_n < 1 or len(final_posts) < 1:
        log(f"mix short videos={videos_n} viral={viral_n} posts={len(final_posts)}")
    return 0 if final_posts else 1


def self_test() -> int:
    hook_caption = build_caption("\u26bd", "1 Cherki finish against Liverpool",
                                 "The touch is the whole clip. Verified sports accounts replay this kind of finish.",
                                 ["#PremierLeague", "#Cherki"])
    assert caption_ok(hook_caption), hook_caption
    assert hook_caption.startswith("\u26bd ")
    assert hook_caption.count("#") == 2
    assert "?" not in hook_caption
    assert len(hook_caption) <= 280
    long_cap = build_caption("\U0001F30D", "30 seconds of a very long place name " * 4,
                             "context " * 40, ["#Travel", "#World", "#Extra"])
    assert caption_ok(long_cap), long_cap
    assert forbidden_media_url("https://chatgpt820970.github.io/chad-media/m/a.jpg")
    assert forbidden_media_url("https://pbs.twimg.com/media/abc.jpg")
    assert not forbidden_media_url("https://raw.githubusercontent.com/chatgpt820970/chad-media/main/m/a.jpg")
    fams = {"musk"}
    assert too_close("Starlink adds capacity after Musk comment", "starlink-musk", [], fams)
    assert not too_close("Bad Bunny wins artist of the year", "bad-bunny-award", [], fams)
    assert too_close("Petr Yan trilogy", "ufc-yan", ["petr yan trilogy breakdown merab"], set())
    videos = [
        {"type": "viral", "key": "v1", "media_kind": "video"},
        {"type": "viral", "key": "v2", "media_kind": "video"},
        {"type": "news", "key": "v3", "media_kind": "video"},
    ]
    photos = [{"type": "news", "key": "p1", "media": ["a", "b"]}, {"type": "news", "key": "p2", "media": ["c", "d"]}]
    laid = layout_posts(videos, photos, 5)
    assert all(laid), laid
    assert sum(1 for p in laid if p.get("media_kind") == "video") == 3
    assert laid[1]["type"] == "viral"
    assert {p["category"] for p in (
        {"category": "ai"}, {"category": "sports"}, {"category": "tech"}, {"category": "celebrity"},
    )} >= {"ai", "sports"}
    reply = reply_text("Grok just booked the trip for 40 dollars", "ai", "elonmusk", "10")
    assert reply and "?" not in reply and "http" not in reply and "#" not in reply
    shout = reply_text("CHET WILD IN OT SEE SOON", "sports", "ESPN", "99")
    assert shout and "CHET" not in shout and "WILD" not in shout and "SOON" not in shout
    fact_reply = reply_text("Lana hit 1 billion streams", "celebrity", "PopCrave", "8")
    assert "1 billion" in fact_reply
    kalki, _tags = news_caption(
        "Kalki 2 BTS: Prabhas returns as Bhairava",
        "Prabhas is back as Bhairava in Kalki 2. The makers released a behind-the-scenes video on his birthday.",
        "celebrity",
        ["Prabhas"],
    )
    assert "2 —" not in kalki
    assert "only claim" not in kalki.lower()
    assert "birthday" in kalki.lower()
    assert caption_ok(kalki)
    nasa_reply = reply_text("That is our jam from the telescope", "tech", "NASA", "4")
    assert "NASA" in nasa_reply and "CHET" not in nasa_reply
    sample = (
        "          for f in candidates.json media_ready.json posted.json grok_queue.json grok_replies.json; do\n"
        "            [ -f \"xnc/data/$f\" ] && cp \"xnc/data/$f\" site/feed/ || true\n"
        "          done\n"
    )
    patched = patch_pages_workflow(sample)
    assert patched and "curl -fsSL" in patched and "grok-feed-stamp:" in patched
    assert patch_pages_workflow(patched) != patched
    pool = [
        {"post_url": f"https://x.com/a/status/{i}", "reply": "r", "account": acct, "verified": True,
         "views": 100000 - i, "age_minutes": 20, "category": cat, "topic": topic}
        for i, (acct, cat, topic) in enumerate([
            ("elonmusk", "ai", "grok"),
            ("elonmusk", "ai", "grok"),
            ("elonmusk", "ai", "grok"),
            ("NBA", "sports", "nba"),
            ("NFL", "sports", "nfl"),
            ("NASA", "tech", "nasa"),
            ("Reuters", "us_news", "congress"),
            ("BillieEilish", "celebrity", "billie"),
            ("OpenAI", "ai", "openai"),
        ])
    ]
    picked = pick_replies(pool, 6)
    assert len(picked) == 6
    assert len({p["topic"] for p in picked}) >= 4 or len({p["category"] for p in picked}) >= 4
    assert max(sum(1 for p in picked if p["topic"] == t) for t in {p["topic"] for p in picked}) <= 2
    assert len({p["category"] for p in picked}) >= 4
    spread = pool + [
        {"post_url": "https://x.com/a/status/tech2", "reply": "r", "account": "nvidia", "verified": True,
         "views": 80000, "age_minutes": 30, "category": "tech", "topic": "nvidia"},
        {"post_url": "https://x.com/a/status/us2", "reply": "r", "account": "AP", "verified": True,
         "views": 70000, "age_minutes": 40, "category": "us_news", "topic": "trump"},
        {"post_url": "https://x.com/a/status/celeb2", "reply": "r", "account": "Netflix", "verified": True,
         "views": 90000, "age_minutes": 15, "category": "celebrity", "topic": "netflix"},
    ]
    morning, afternoon = split_reply_slots(spread)
    assert len(morning) == 6 and len(afternoon) == 6, (len(morning), len(afternoon))
    assert len({p["category"] for p in morning}) >= 4
    assert len({p["category"] for p in afternoon}) >= 4
    for hand in (morning, afternoon):
        assert max(sum(1 for p in hand if p["topic"] == t) for t in {p["topic"] for p in hand}) <= 2
    slots = upcoming_slot_times(datetime(2026, 10, 23, 9, 40, tzinfo=timezone.utc))
    assert len(slots) == 5
    assert slots[0][0].strftime("%H:%M") == "04:47"
    log("self-test ok")
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    if "--publish-pages" in argv:
        return publish_pages_hook()
    if "--verify" in argv:
        return verify_file()
    return build("--dry-run" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
