#!/usr/bin/env python3
"""
x-news-collector: gathers the freshest trending news (with media links)
and writes data/candidates.json + data/candidates.md.

Free sources only, no API keys:
  * Google News RSS (top stories + topic feeds + priority search queries)
  * Google Trends daily-trends RSS

Dependencies: feedparser, requests, beautifulsoup4 (plus stdlib).
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus, urlparse, urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
MAX_AGE_HOURS = 9
TOP_N = 25
FEED_TIMEOUT = 12           # seconds per RSS fetch
PAGE_TIMEOUT = 8            # seconds per article/page fetch
MAX_PAGE_FETCHES = 130      # hard cap on page fetches per run (decode + article)
MAX_URLS_PER_CLUSTER = 3    # article pages tried per story when hunting media
MAX_PAGE_BYTES = 2_000_000
WORKERS = 8

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

TOPICS = ["WORLD", "BUSINESS", "TECHNOLOGY", "ENTERTAINMENT", "SPORTS", "SCIENCE"]

# English editions supported by Google News.
EN_EDITIONS = {
    "US": ("en-US", "US", "US:en"),
    "IN": ("en-IN", "IN", "IN:en"),
    "GB": ("en-GB", "GB", "GB:en"),
}
# Google News has no English edition for JP / SA / BR (requests silently fall
# back to the US edition), so these use the local-language top stories.
# They get a small ranking penalty; the assistant can translate if picked.
LOCAL_EDITIONS = {
    "JP": ("ja", "JP", "JP:ja", "ja"),
    "SA": ("ar", "SA", "SA:ar", "ar"),
    "BR": ("pt-419", "BR", "BR:pt-419", "pt"),
}
NON_EN_PENALTY = 0.6

PRIORITY_QUERIES = {
    "Elon Musk": ["elon", "musk", "tesla", "spacex", "xai"],
    "Trump": ["trump"],
    "OpenAI OR ChatGPT OR Gemini OR Claude OR Grok":
        ["openai", "chatgpt", "gemini", "claude", "grok", "anthropic", "altman"],
    "F1": ["f1", "formula", "verstappen", "hamilton", "norris", "leclerc", "piastri", "grand prix"],
    "cricket": ["cricket", "ipl", "bcci", "kohli", "rohit", "test match", "odi", "t20"],
    "football": ["football", "soccer", "premier league", "champions league", "fifa",
                 "messi", "ronaldo", "la liga", "uefa"],
}
# Which editions to run each priority search in ("football" = soccer, so GB/IN).
PRIORITY_QUERY_EDITIONS = {
    "Elon Musk": ["US", "IN"],
    "Trump": ["US", "IN"],
    "OpenAI OR ChatGPT OR Gemini OR Claude OR Grok": ["US", "IN"],
    "F1": ["GB", "US"],
    "cricket": ["IN", "GB"],
    "football": ["GB", "IN"],
}
QUERY_CATEGORY = {"F1": "SPORTS", "cricket": "SPORTS", "football": "SPORTS",
                  "OpenAI OR ChatGPT OR Gemini OR Claude OR Grok": "TECHNOLOGY"}
# Diversity caps for the final top list
MAX_PER_QUERY = 3
MAX_PER_CATEGORY = 8

TRENDS_GEOS = ["US", "IN", "GB", "JP"]

CATEGORY_KEYWORDS = {
    "TECHNOLOGY": ["ai", "openai", "chatgpt", "gemini", "claude", "grok", "apple", "google",
                   "microsoft", "iphone", "android", "chip", "nvidia", "tesla", "spacex",
                   "software", "app", "meta", "startup", "robot", "cyber"],
    "SPORTS": ["cricket", "football", "f1", "nba", "nfl", "tennis", "match", "cup", "league",
               "goal", "wicket", "olympic", "grand prix", "coach", "vs"],
    "BUSINESS": ["stock", "market", "shares", "economy", "fed", "inflation", "bank", "price",
                 "earnings", "ipo", "tariff", "rupee", "dollar", "oil", "deal", "billion"],
    "ENTERTAINMENT": ["film", "movie", "actor", "actress", "music", "album", "netflix",
                      "bollywood", "hollywood", "star", "show", "series", "box office", "song"],
    "SCIENCE": ["nasa", "space", "study", "scientists", "climate", "planet", "health",
                "virus", "research", "nobel"],
    "WORLD": ["war", "president", "minister", "election", "israel", "ukraine", "russia",
              "china", "gaza", "iran", "trump", "government", "police", "killed"],
}

STOPWORDS = set("""
a an the and or but of to in on at for from by with as is are was were be been being
it its this that these those after before over under into about amid says say said
new news live update updates latest how why what who when where will would could can
may might has have had not no vs v his her their our your he she they we you i
up out off more than just also first top day today week year video watch report
""".split())

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
_page_fetches = 0


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ist(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")


def gn_url(path: str, ed: tuple) -> str:
    hl, gl, ceid = ed[0], ed[1], ed[2]
    sep = "&" if "?" in path else "?"
    return f"https://news.google.com/rss{path}{sep}hl={hl}&gl={gl}&ceid={ceid}"


def strip_source(title: str, source: str | None) -> str:
    title = html.unescape(title or "").strip()
    if source and title.endswith(" - " + source):
        return title[: -len(source) - 3].strip()
    # generic " - Publisher" suffix
    m = re.match(r"^(.*\S)\s+[-–|]\s+[^-–|]{2,40}$", title)
    return m.group(1) if m else title


def tokens(text: str) -> set[str]:
    text = html.unescape(text or "").lower()
    text = re.sub(r"[’'`]s\b", "", text)
    words = re.findall(r"[a-z0-9]+|[^\x00-\x7f]{1,}", text)
    out = set()
    for w in words:
        if w in STOPWORDS or (w.isascii() and len(w) < 3 and not w.isdigit()):
            continue
        if w.isascii() and len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def headline_key(text: str) -> str:
    t = sorted(tokens(text))
    return "-".join(t[:12])


def similar(a: set, b: set) -> bool:
    if not a or not b:
        return False
    inter = len(a & b)
    small = min(len(a), len(b))
    jac = inter / len(a | b)
    return (inter >= 3 and inter / small >= 0.5) or jac >= 0.45 or (inter >= 2 and small <= 3 and inter / small >= 0.67)


def fetch(url: str, timeout: int = FEED_TIMEOUT) -> bytes | None:
    try:
        r = SESSION.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.content
        log(f"  ! HTTP {r.status_code} {url}")
    except Exception as e:  # noqa: BLE001
        log(f"  ! fetch failed {url}: {e.__class__.__name__}")
    return None


def entry_time(e) -> datetime | None:
    for k in ("published_parsed", "updated_parsed"):
        t = e.get(k)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001
                pass
    return None


# --------------------------------------------------------------------------
# Feed definitions
# --------------------------------------------------------------------------
def build_feeds() -> list[dict]:
    feeds = []
    for code, ed in EN_EDITIONS.items():
        feeds.append(dict(name=f"GN top {code}", url=gn_url("", ed), edition=code,
                          category=None, lang="en", query=None))
        for t in TOPICS:
            feeds.append(dict(name=f"GN {t.lower()} {code}",
                              url=gn_url(f"/headlines/section/topic/{t}", ed),
                              edition=code, category=t, lang="en", query=None))
    for code, ed in LOCAL_EDITIONS.items():
        feeds.append(dict(name=f"GN top {code} ({ed[3]})", url=gn_url("", ed), edition=code,
                          category=None, lang=ed[3], query=None))
    for q in PRIORITY_QUERIES:
        for code in PRIORITY_QUERY_EDITIONS.get(q, ["US"]):
            ed = EN_EDITIONS[code]
            path = f"/search?q={quote_plus(q + ' when:' + str(MAX_AGE_HOURS) + 'h')}"
            feeds.append(dict(name=f"GN search '{q}' {code}", url=gn_url(path, ed),
                              edition=code, category=None, lang="en", query=q))
    return feeds


def parse_related(summary_html: str) -> list[dict]:
    """Google News top-story items list related coverage in an <ol>."""
    out = []
    if not summary_html or "<li" not in summary_html:
        return out
    try:
        soup = BeautifulSoup(summary_html, "html.parser")
        for li in soup.find_all("li"):
            a = li.find("a")
            font = li.find("font")
            if a and a.get("href"):
                out.append(dict(title=a.get_text(" ", strip=True),
                                url=a["href"],
                                source=font.get_text(strip=True) if font else None))
    except Exception:  # noqa: BLE001
        pass
    return out


def collect_items(feeds: list[dict]) -> tuple[list[dict], dict]:
    items, stats = [], {"feeds_ok": 0, "feeds_failed": 0}

    def work(f):
        raw = fetch(f["url"])
        if raw is None:
            return f, None
        try:
            return f, feedparser.parse(raw)
        except Exception:  # noqa: BLE001
            return f, None

    with ThreadPoolExecutor(WORKERS) as ex:
        for fut in as_completed([ex.submit(work, f) for f in feeds]):
            f, parsed = fut.result()
            if not parsed or not parsed.entries:
                stats["feeds_failed"] += 1
                continue
            stats["feeds_ok"] += 1
            for e in parsed.entries:
                src = (e.get("source") or {}).get("title")
                title = strip_source(e.get("title", ""), src)
                if not title:
                    continue
                related = parse_related(e.get("summary", ""))
                items.append(dict(
                    title=title, source=src, url=e.get("link"),
                    published=entry_time(e), feed=f["name"], edition=f["edition"],
                    category=f["category"], lang=f["lang"], query=f["query"],
                    related=[r for r in related if r["source"] != src],
                ))
    return items, stats


def collect_trends() -> list[dict]:
    """Return trending terms with their attached news items (direct URLs!)."""
    out = []

    def work(geo):
        raw = fetch(f"https://trends.google.com/trending/rss?geo={geo}")
        res = []
        if not raw:
            return res
        try:
            ns = {"ht": "https://trends.google.com/trending/rss"}
            root = ET.fromstring(raw)
            for it in root.iter("item"):
                term = (it.findtext("title") or "").strip()
                if not term:
                    continue
                news = []
                for ni in it.findall("ht:news_item", ns):
                    g = lambda n: (ni.findtext("ht:" + n, default="", namespaces=ns) or "").strip()  # noqa: E731
                    news.append(dict(title=html.unescape(g("news_item_title")),
                                     url=g("news_item_url") or None,
                                     picture=g("news_item_picture") or None,
                                     source=g("news_item_source") or None))
                res.append(dict(term=term, geo=geo,
                                traffic=(it.findtext("ht:approx_traffic", default="", namespaces=ns) or "").strip(),
                                tokens=tokens(term), news=news))
        except Exception as e:  # noqa: BLE001
            log(f"  ! trends parse failed {geo}: {e}")
        return res

    with ThreadPoolExecutor(4) as ex:
        for r in ex.map(work, TRENDS_GEOS):
            out.extend(r)
    return out


# --------------------------------------------------------------------------
# Clustering + scoring
# --------------------------------------------------------------------------
def _item_urls(it: dict) -> set:
    u = {it["url"]} if it["url"] else set()
    u |= {r["url"] for r in it["related"] if r.get("url")}
    return u


def cluster(items: list[dict]) -> list[dict]:
    """Greedy clustering: same story if titles overlap enough OR the items
    share a Google News article link (Google's own story grouping)."""
    items = sorted(items, key=lambda i: i["published"], reverse=True)
    clusters: list[dict] = []
    url_owner: dict[str, int] = {}
    for it in items:
        tk = tokens(it["title"])
        it["tokens"] = tk
        urls = _item_urls(it)
        target = None
        for u in urls:
            if u in url_owner:
                target = url_owner[u]
                break
        if target is None:
            for n, c in enumerate(clusters):
                if c["lang"] != it["lang"]:
                    continue
                if any(similar(tk, m) for m in c["token_sets"]):
                    target = n
                    break
        if target is None:
            clusters.append(dict(items=[], token_sets=[], lang=it["lang"], urls=set()))
            target = len(clusters) - 1
        c = clusters[target]
        c["items"].append(it)
        c["urls"] |= urls
        for u in urls:
            url_owner.setdefault(u, target)
        if len(c["token_sets"]) < 15:
            c["token_sets"].append(tk)
    # merge pass: clusters that share any article link
    parent = list(range(len(clusters)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    owner: dict[str, int] = {}
    for n, c in enumerate(clusters):
        for u in c["urls"]:
            if u in owner:
                parent[find(n)] = find(owner[u])
            else:
                owner[u] = n
    merged: dict[int, dict] = {}
    for n, c in enumerate(clusters):
        r = find(n)
        if r not in merged:
            merged[r] = dict(items=[], token_sets=[], lang=c["lang"], urls=set())
        merged[r]["items"].extend(c["items"])
        merged[r]["urls"] |= c["urls"]
    out = list(merged.values())
    for c in out:
        c["items"].sort(key=lambda i: i["published"], reverse=True)
    return out


def pick_headline(its: list[dict]) -> dict:
    """Most representative title: tokens shared by many members, short-ish,
    avoid live-blog / commentary / newsletter style titles."""
    if len(its) == 1:
        return its[0]
    freq: dict[str, int] = {}
    for i in its:
        for t in i["tokens"]:
            freq[t] = freq.get(t, 0) + 1
    bad = re.compile(r"\b(live|commentary|newsletter|highlights|preview|morning|evening|briefing|podcast)\b|\band, and\b", re.I)
    best, best_s = its[0], -1.0
    for rank, i in enumerate(its[:30]):
        if not i["tokens"]:
            continue
        s = sum(freq[t] for t in i["tokens"]) / len(i["tokens"])
        if bad.search(i["title"]):
            s *= 0.5
        if len(i["title"]) > 120:
            s *= 0.8
        if len(i["tokens"]) < 5 or i["title"].rstrip().endswith(":"):
            s *= 0.5
        s *= 1 - min(rank, 20) * 0.01
        if s > best_s:
            best, best_s = i, s
    return best


def priority_match(text_tokens: set, title: str) -> list[str]:
    low = title.lower()
    hits = []
    for q, kws in PRIORITY_QUERIES.items():
        for k in kws:
            if (" " in k and k in low) or k in text_tokens:
                hits.append(q)
                break
    return hits


def guess_category(c_items: list[dict], tk: set, title: str, queries=()) -> str:
    for q in queries:
        if QUERY_CATEGORY.get(q) == "SPORTS":
            return "SPORTS"
    votes: dict[str, int] = {}
    for it in c_items:
        if it["category"]:
            votes[it["category"]] = votes.get(it["category"], 0) + 1
    if votes:
        return max(votes, key=votes.get)
    low = title.lower()
    best, best_n = "WORLD", 0
    for cat, kws in CATEGORY_KEYWORDS.items():
        n = sum(1 for k in kws if (k in tk or (" " in k and k in low)))
        if n > best_n:
            best, best_n = cat, n
    return best


def summarize_clusters(clusters, trends, posted_keys, now) -> list[dict]:
    out = []
    for c in clusters:
        its = c["items"]
        newest = max(i["published"] for i in its)
        lead = pick_headline(its)
        all_tokens = set().union(*[i["tokens"] for i in its])
        sources, urls, feeds, editions = [], [], set(), set()
        for i in its:
            feeds.add(i["feed"])
            editions.add(i["edition"])
            if i["source"] and i["source"] not in sources:
                sources.append(i["source"])
            if i["url"] and i["url"] not in urls:
                urls.append(i["url"])
            for r in i["related"]:
                if r["source"] and r["source"] not in sources:
                    sources.append(r["source"])
                if r["url"] and r["url"] not in urls and len(urls) < 8:
                    urls.append(r["url"])
        queries = {i["query"] for i in its if i["query"]}
        queries |= set(priority_match(all_tokens, " ".join(i["title"] for i in its)))

        # Google Trends match: a trend term whose tokens are all in the story
        trend_hits, direct = [], []
        for t in trends:
            ttk = t["tokens"]
            if ttk and len(ttk) <= 4 and ttk <= all_tokens:
                trend_hits.append(f'{t["term"]} ({t["geo"]} {t["traffic"]})')
                direct.extend(t["news"])
            else:
                for n in t["news"]:
                    if similar(tokens(n["title"]), lead["tokens"]):
                        trend_hits.append(f'{t["term"]} ({t["geo"]} {t["traffic"]})')
                        direct.extend(t["news"])
                        break
        trend_hits = list(dict.fromkeys(trend_hits))

        trend_score = len(sources) + len(feeds)
        if trend_hits:
            trend_score += 4 + min(len(trend_hits), 3)
        if queries:
            trend_score += 3

        age_min = int((now - newest).total_seconds() // 60)
        recency = max(0.0, 1 - age_min / (MAX_AGE_HOURS * 60))
        rank = 0.65 * recency + 0.35 * min(1.0, math.log1p(trend_score) / math.log1p(30))
        if c["lang"] != "en":
            rank *= NON_EN_PENALTY

        key = headline_key(lead["title"])
        if any(is_posted(i["tokens"], posted_keys) for i in its[:20]):
            continue

        direct_urls = [d["url"] for d in direct if d.get("url")]
        out.append(dict(
            id=hashlib.sha1(key.encode()).hexdigest()[:10],
            key=key,
            headline=lead["title"],
            summary="",
            sources=sources[:10],
            article_urls=list(dict.fromkeys(direct_urls[:3] + urls))[:8],
            published_at_utc=iso(newest),
            published_at_ist=ist(newest),
            first_seen_utc=iso(min(i["published"] for i in its)),
            age_minutes=age_min,
            trend_score=trend_score,
            rank_score=round(rank, 4),
            category=guess_category(its, all_tokens, lead["title"], queries),
            region=sorted(editions),
            language=c["lang"],
            priority_queries=sorted(queries),
            google_trends=trend_hits[:5],
            other_headlines=[t for t in dict.fromkeys(i["title"] for i in its) if t != lead["title"]][:4],
            image_urls=[d["picture"] for d in direct if d.get("picture")][:2],
            video_urls=[],
            has_video=False,
            _gn_urls=list(dict.fromkeys([lead["url"]] + [i["url"] for i in its if i["url"]]))[:MAX_URLS_PER_CLUSTER + 2],
            _direct_urls=direct_urls[:2],
        ))
    out.sort(key=lambda x: x["rank_score"], reverse=True)
    return out


def load_posted() -> list[set]:
    p = DATA / "posted.json"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    entries = raw if isinstance(raw, list) else raw.get("posted", [])
    keys = []
    for e in entries:
        text = e if isinstance(e, str) else (e.get("key") or e.get("headline") or "")
        tk = set(text.replace("-", " ").split()) if re.fullmatch(r"[a-z0-9\-]+", text or "") else tokens(text)
        if tk:
            keys.append(tk)
    return keys


def is_posted(tk: set, posted: list[set]) -> bool:
    for p in posted:
        inter = len(tk & p)
        if inter >= 3 and inter / min(len(tk), len(p)) >= 0.6:
            return True
    return False


# --------------------------------------------------------------------------
# Media extraction
# --------------------------------------------------------------------------
def decode_gnews(url: str) -> str | None:
    """Turn a news.google.com/rss/articles/... link into the publisher URL."""
    global _page_fetches
    try:
        p = urlparse(url)
        if "news.google.com" not in p.netloc:
            return url
        gid = p.path.rstrip("/").split("/")[-1]
        _page_fetches += 1
        r = SESSION.get(f"https://news.google.com/articles/{gid}", timeout=PAGE_TIMEOUT)
        m_sg = re.search(r'data-n-a-sg="([^"]+)"', r.text)
        m_ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
        if not (m_sg and m_ts):
            return None
        req = ["Fbv4je",
               f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
               f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{gid}",{m_ts.group(1)},"{m_sg.group(1)}"]']
        r2 = SESSION.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            data=f"f.req={quote(json.dumps([[req]]))}", timeout=PAGE_TIMEOUT)
        body = r2.text.split("\n\n", 1)[1]
        decoded = json.loads(json.loads(body)[0][2])[1]
        return decoded if decoded.startswith("http") else None
    except Exception as e:  # noqa: BLE001
        log(f"  ! gnews decode failed: {e.__class__.__name__}")
        return None


YT_RE = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:embed/|watch\?v=|shorts/)|youtu\.be/)[A-Za-z0-9_\-]{6,}(?:[?&][^\s\"'<>\\)]*)?")


def page_media(url: str) -> dict:
    """Fetch an article page and pull og:image / og:video / twitter:player / YouTube."""
    global _page_fetches
    res = dict(final_url=url, images=[], videos=[], description="")
    try:
        _page_fetches += 1
        r = SESSION.get(url, timeout=PAGE_TIMEOUT, stream=True, allow_redirects=True)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            return res
        buf = b""
        for chunk in r.iter_content(65536):
            buf += chunk
            if len(buf) > MAX_PAGE_BYTES:
                break
        res["final_url"] = r.url
        text = buf.decode(r.encoding or "utf-8", errors="replace")
        soup = BeautifulSoup(text, "html.parser")

        def metas(*names):
            vals = []
            for n in names:
                for m in soup.find_all("meta", attrs={"property": n}) + soup.find_all("meta", attrs={"name": n}):
                    v = (m.get("content") or "").strip()
                    if v:
                        vals.append(urljoin(r.url, v))
            return vals

        res["images"] = metas("og:image:secure_url", "og:image", "og:image:url", "twitter:image", "twitter:image:src")
        res["videos"] = metas("og:video:secure_url", "og:video", "og:video:url", "twitter:player:stream", "twitter:player")
        d = metas("og:description", "description", "twitter:description")
        res["description"] = html.unescape(d[0]) if d else ""
        # embeds
        for tag in soup.find_all(["iframe", "a", "link"]):
            v = tag.get("src") or tag.get("href") or tag.get("data-src") or ""
            if YT_RE.match(v):
                res["videos"].append(v)
        for tag in soup.find_all(["video", "source"]):
            v = tag.get("src") or tag.get("data-src") or ""
            if v and not v.startswith("blob:"):
                res["videos"].append(urljoin(r.url, v))
        # JSON-LD VideoObject
        for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = s.string or ""
            if "VideoObject" in raw:
                for m in re.finditer(r'"(?:contentUrl|embedUrl)"\s*:\s*"([^"]+)"', raw):
                    res["videos"].append(m.group(1).replace("\\/", "/"))
        res["videos"] += YT_RE.findall(text)[:3]
    except Exception as e:  # noqa: BLE001
        log(f"  ! page fetch failed {url[:80]}: {e.__class__.__name__}")
    # clean + dedupe
    def clean(lst):
        seen, out = set(), []
        for u in lst:
            u = html.unescape(u).strip()
            if not u.startswith("http") or u in seen:
                continue
            seen.add(u)
            out.append(u)
        return out
    res["images"] = clean(res["images"])[:4]
    res["videos"] = clean(res["videos"])[:5]
    return res


def enrich_cluster(c: dict) -> dict:
    resolved = []
    tried = 0
    candidates = c["_direct_urls"] + c["_gn_urls"]
    for u in candidates:
        if tried >= MAX_URLS_PER_CLUSTER or _page_fetches >= MAX_PAGE_FETCHES:
            break
        real = decode_gnews(u)
        if not real:
            continue
        tried += 1
        m = page_media(real)
        resolved.append(m["final_url"])
        c["image_urls"] = list(dict.fromkeys(c["image_urls"] + m["images"]))[:6]
        c["video_urls"] = list(dict.fromkeys(c["video_urls"] + m["videos"]))[:6]
        if not c["summary"] and m["description"]:
            c["summary"] = m["description"][:400]
        if c["video_urls"] and c["image_urls"]:
            break
    # resolved publisher URLs go first, then any remaining (Google News) links
    c["article_urls"] = list(dict.fromkeys(resolved + c["article_urls"]))[:8]
    # og images first (bigger), trends thumbnails last
    c["image_urls"].sort(key=lambda u: "gstatic.com" in u)
    c["has_video"] = bool(c["video_urls"])
    return c


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def write_outputs(items: list[dict], meta: dict) -> None:
    DATA.mkdir(exist_ok=True)
    payload = dict(meta, items=items)
    (DATA / "candidates.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    by_new = sorted(items, key=lambda x: x["age_minutes"])
    lines = [
        "# News candidates",
        "",
        f"Generated: **{meta['generated_at_ist']}** ({meta['generated_at_utc']})  ",
        f"Window: last {MAX_AGE_HOURS} h · {len(items)} stories · "
        f"{sum(1 for i in items if i['has_video'])} with video  ",
        "Sorted newest first. `rank` = ranking order in candidates.json (1 = best).",
        "",
        "| # | rank | age | published (IST) | headline | cat | region | trend | video | image | sources |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    rank_of = {it["id"]: n + 1 for n, it in enumerate(items)}
    for n, it in enumerate(by_new, 1):
        url = it["article_urls"][0] if it["article_urls"] else ""
        head = f"[{md_escape(it['headline'])}]({url})" if url else md_escape(it["headline"])
        vid = f"[yes]({it['video_urls'][0]})" if it["has_video"] else "no"
        img = f"[img]({it['image_urls'][0]})" if it["image_urls"] else "—"
        age = f"{it['age_minutes'] // 60}h{it['age_minutes'] % 60:02d}m"
        lines.append(
            f"| {n} | {rank_of[it['id']]} | {age} | {it['published_at_ist'][11:16]} | {head} | "
            f"{it['category'].title()} | {','.join(it['region'])} | {it['trend_score']} | {vid} | {img} | "
            f"{md_escape(', '.join(it['sources'][:4]))} |"
        )
    lines.append("")
    (DATA / "candidates.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    t0 = time.time()
    now = now_utc()
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)

    feeds = build_feeds()
    log(f"Fetching {len(feeds)} news feeds + {len(TRENDS_GEOS)} trends feeds ...")
    with ThreadPoolExecutor(2) as ex:
        f_items = ex.submit(collect_items, feeds)
        f_trends = ex.submit(collect_trends)
        items, stats = f_items.result()
        trends = f_trends.result()
    log(f"  feeds ok={stats['feeds_ok']} failed={stats['feeds_failed']} raw items={len(items)} trends={len(trends)}")

    fresh = [i for i in items if i["published"] and cutoff <= i["published"] <= now + timedelta(minutes=10)]
    log(f"  fresh (<= {MAX_AGE_HOURS}h) items: {len(fresh)}")

    clusters = cluster(fresh)
    posted = load_posted()
    ranked = summarize_clusters(clusters, trends, posted, now)
    log(f"  clusters: {len(clusters)}  after posted-filter: {len(ranked)}")
    top, per_q, per_cat = [], {}, {}
    for c in ranked:
        qs = c["priority_queries"]
        if any(per_q.get(q, 0) >= MAX_PER_QUERY for q in qs):
            continue
        if per_cat.get(c["category"], 0) >= MAX_PER_CATEGORY:
            continue
        top.append(c)
        for q in qs:
            per_q[q] = per_q.get(q, 0) + 1
        per_cat[c["category"]] = per_cat.get(c["category"], 0) + 1
        if len(top) >= TOP_N:
            break

    log("Resolving articles + media ...")
    with ThreadPoolExecutor(WORKERS) as ex:
        top = list(ex.map(enrich_cluster, top))
    for c in top:
        c.pop("_gn_urls", None)
        c.pop("_direct_urls", None)

    meta = dict(
        generated_at_utc=iso(now),
        generated_at_ist=ist(now),
        window_hours=MAX_AGE_HOURS,
        stats=dict(feeds=len(feeds), feeds_ok=stats["feeds_ok"], feeds_failed=stats["feeds_failed"],
                   raw_items=len(items), fresh_items=len(fresh), clusters=len(clusters),
                   trends_terms=len(trends), page_fetches=_page_fetches,
                   with_video=sum(1 for c in top if c["has_video"]),
                   runtime_sec=round(time.time() - t0, 1)),
        top_trends=[f'{t["term"]} ({t["geo"]} {t["traffic"]})' for t in trends[:20]],
    )
    write_outputs(top, meta)
    log(f"Wrote {len(top)} items in {meta['stats']['runtime_sec']}s "
        f"({meta['stats']['with_video']} with video, {_page_fetches} page fetches)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
