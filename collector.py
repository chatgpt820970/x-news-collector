#!/usr/bin/env python3
"""
x-news-collector: gathers the freshest trending news (with media links)
and writes data/candidates.json + data/candidates.md.

Free sources only, no API keys:
  * Google News RSS (top stories + topic feeds + priority search queries)
  * Google Trends daily-trends RSS
  * YouTube channel RSS feeds (big interview/podcast + official channels)

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
YT_MAX_AGE_HOURS = 24       # YouTube uploads are kept a bit longer (podcast clips trend slower)
TOP_N = 40                  # enough for ~20 posts/day across 7 slots
FEED_TIMEOUT = 12           # seconds per RSS fetch
PAGE_TIMEOUT = 8            # seconds per article/page fetch
MAX_PAGE_FETCHES = 220      # hard cap on page fetches per run (decode + article)
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

# Priority topics: label -> search queries (each with the editions to search in),
# keywords used to tag stories from other feeds, and the output category.
# The label is what shows up in each item's `priority_queries`.
FAMOUS_LABEL = "Famous people clips"

PRIORITY_TOPICS = {
    "Elon Musk": dict(
        queries=[("Elon Musk", ["US", "IN"])],
        keywords=["elon", "musk", "tesla", "spacex", "xai"], category="tech"),
    "Trump": dict(
        queries=[("Trump", ["US", "IN"])],
        keywords=["trump"], category="world"),
    "OpenAI OR ChatGPT OR Gemini OR Claude OR Grok": dict(
        queries=[("OpenAI OR ChatGPT OR Gemini OR Claude OR Grok", ["US", "IN"])],
        keywords=["openai", "chatgpt", "gemini", "claude", "grok", "anthropic", "altman"],
        category="ai"),
    "F1": dict(
        queries=[("F1", ["GB", "US"])],
        keywords=["f1", "formula", "verstappen", "hamilton", "norris", "leclerc", "piastri",
                  "grand prix"], category="sports"),
    "cricket": dict(
        queries=[("cricket", ["IN", "GB"])],
        keywords=["cricket", "ipl", "bcci", "kohli", "rohit", "test match", "odi", "t20"],
        category="sports"),
    "football": dict(
        queries=[("football", ["GB", "IN"])],
        keywords=["football", "soccer", "premier league", "champions league", "fifa",
                  "messi", "ronaldo", "la liga", "uefa"], category="sports"),
    # --- new topics ---
    "Celebrities & music": dict(
        # multi-word names are quoted (unquoted, Google News returned 0 results)
        queries=[('"Taylor Swift" OR Drake OR BTS OR "Bad Bunny" OR "The Weeknd" OR Beyonce '
                  'OR "Arijit Singh" OR "Diljit Dosanjh"', ["US", "IN"]),
                 ("album release OR world tour OR concert", ["US", "IN"]),
                 ("Bollywood", ["IN"]),
                 ("Hollywood trailer", ["US"]),
                 ("Grammy OR Oscars OR award show performance OR celebrity wedding", ["US"])],
        keywords=["taylor swift", "drake", "bts", "bad bunny", "the weeknd", "beyonce",
                  "beyoncé", "arijit", "diljit", "album", "world tour", "concert", "trailer",
                  "bollywood", "hollywood", "grammy", "oscars", "singer", "rapper",
                  "pop star", "music video", "box office"],
        category="celebrity_music"),
    "AI tools & agents": dict(
        queries=[("AI agent", ["US", "IN"]),
                 ("AI automation", ["US"]),
                 ("new AI tool", ["US", "IN"]),
                 ("OpenAI OR ChatGPT OR Gemini OR Claude OR Grok launch", ["US"]),
                 ("no-code AI", ["US"])],
        keywords=["ai agent", "agentic", "ai tool", "ai automation", "no-code", "no code",
                  "copilot", "llm", "ai model", "ai app", "vibe coding", "chatbot"],
        category="ai"),
    "Finance & markets": dict(
        queries=[("stock market", ["US", "IN"]),
                 ("Sensex OR Nifty", ["IN"]),
                 ("Dow Jones OR Nasdaq OR S&P 500", ["US"]),
                 ("Fed rate", ["US"]),
                 ("gold price", ["IN", "US"]),
                 ("RBI", ["IN"])],
        keywords=["sensex", "nifty", "dow jones", "nasdaq", "s&p 500", "stock market",
                  "wall street", "federal reserve", "fed rate", "rate cut", "interest rate",
                  "gold price", "gold rate", "rbi", "treasury yield", "stocks"],
        category="finance"),
    "Crypto": dict(
        queries=[("Bitcoin", ["US", "IN"]),
                 ("Ethereum", ["US"]),
                 ("crypto market", ["US", "IN"]),
                 ("Solana OR XRP", ["US"])],
        keywords=["bitcoin", "ethereum", "crypto", "cryptocurrency", "solana", "xrp", "btc",
                  "dogecoin", "stablecoin", "binance", "coinbase", "memecoin"],
        category="crypto"),
    # Video clips of famous people: interviews, podcasts, speeches, keynotes, ceremonies.
    # Keywords stay empty: stories are tagged by is_famous_clip() (famous name + clip word).
    FAMOUS_LABEL: dict(
        queries=[
            ('podcast OR interview ("Elon Musk" OR "Jensen Huang" OR "Sam Altman" OR '
             '"Mark Zuckerberg" OR "Jeff Bezos" OR "Bill Gates")', ["US", "IN"]),
            ('says interview ("Elon Musk" OR "Sam Altman" OR "Jensen Huang" OR '
             '"Warren Buffett" OR "Sundar Pichai" OR "Satya Nadella" OR "Mark Cuban")', ["US"]),
            ('podcast ("Cristiano Ronaldo" OR "Lionel Messi" OR "Virat Kohli" OR "LeBron James" '
             'OR "Eileen Gu")', ["US", "IN", "GB"]),
            ('podcast OR interview ("Taylor Swift" OR "Shah Rukh Khan" OR "Priyanka Chopra" OR '
             '"Kim Kardashian" OR "Dwayne Johnson")', ["US", "IN"]),
            ('keynote ("Jensen Huang" OR "Tim Cook" OR "Sundar Pichai" OR "Satya Nadella" OR '
             '"Elon Musk" OR "Sam Altman" OR "Lisa Su")', ["US"]),
            ('speech ("Elon Musk" OR "Bill Gates" OR "Warren Buffett" OR "Virat Kohli" OR '
             '"Shah Rukh Khan" OR "Barack Obama")', ["US", "IN"]),
            ('awarded OR ceremony ("Elon Musk" OR "Jensen Huang" OR "Lisa Su" OR "Sergey Brin" '
             'OR "Bill Gates" OR "Sam Altman")', ["US"]),
            ('"Joe Rogan" OR "Lex Fridman" OR "Diary of a CEO" OR "On Purpose" podcast', ["US", "GB"]),
        ],
        keywords=[], category="famous_clip"),
}
# Backwards-compatible view: label -> keywords
PRIORITY_QUERIES = {k: v["keywords"] for k, v in PRIORITY_TOPICS.items()}

# Diversity caps for the final top list
MAX_PER_QUERY = 3          # per priority topic (each topic gets its own cap)
MAX_PER_CATEGORY = 4       # per output category
MAX_PER_ENTITY = 1         # same famous person / entity at most once per slot
MAX_US_LEAGUE = 1          # NFL/MLB/NHL/NBA/rugby: at most 1 per run (runs feed ~1 slot each)
MIN_TREND_SCORE = 3        # engagement floor: need >=2 outlets/feeds or a trend hit
GLOBAL_GEO_BOOST = 1.3     # rank multiplier when trending in US / JP / GB (top X markets)
GLOBAL_GEOS = ("US", "JP", "GB")
INDIA_STAR_BONUS = 2       # only globally huge Indian names
GLOBAL_POP_BONUS = 5
LOCAL_PENALTY = 0.4        # local-only news (regional India policy, airline incidents, local prices)
US_LEAGUE_PENALTY = 0.5    # NFL/MLB/NHL/NBA/rugby unless a huge moment
def _kw_re(words):
    return re.compile(r"\b(" + "|".join(re.escape(k) for k in words) + r")\b", re.I)
INDIA_STARS = ["shah rukh", "srk", "kohli", "rohit sharma", "ar rahman", "a.r. rahman",
               "a r rahman", "diljit"]
INDIA_RE = _kw_re(INDIA_STARS)
GLOBAL_POP = ["dua lipa", "taylor swift", "sabrina carpenter", "billie eilish", "rihanna",
              "beyonce", "beyoncé", "bad bunny", "the weeknd", "ariana grande", "selena gomez",
              "doja cat", "olivia rodrigo", "chappell roan", "lady gaga",
              "bts", "blackpink", "jennie", "lisa", "jungkook", "stray kids", "newjeans", "aespa",
              "marvel", "zendaya", "sydney sweeney", "timothée", "timothee", "chalamet",
              "messi", "ronaldo", "mbappé", "mbappe", "haaland", "yamal",
              "f1", "formula 1", "ufc", "kardashian", "kardashians", "kim kardashian",
              "anime", "ohtani"]
GLOBAL_POP_RE = _kw_re(GLOBAL_POP)
LOCAL_RE = re.compile(r"\b(indigo|air india|akasa|spicejet|gold (price|rate)|silver (price|rate)|"
                      r"petrol|diesel|sensex|india(n)? (bonds?|stocks?|shares)|horoscope|nifty|stock markets? today|rupee|"
                      r"chennai|bengaluru|hyderabad|kolkata|lucknow|patna|jaipur|pune|"
                      r"state govt|state government|chief minister|cm |assembly|panchayat|"
                      r"municipal|district|high court|lok sabha|rajya sabha|bjp|congress)\b", re.I)
MAJOR_CRASH_RE = re.compile(r"\b(crash(es|ed)?|dead|killed|fatalities)\b", re.I)
SUPER_MOMENT_RE = re.compile(r"\b(super bowl|world series|stanley cup|nba finals|game 7|"
                             r"record[- ]breaking|historic)\b", re.I)
US_LEAGUE_RE = re.compile(r"\b(nfl|mlb|nhl|nba|rugby|super bowl|touchdown|quarterback|"
                          r"bills|rams|chiefs|dodgers|yankees|lakers|celtics)\b", re.I)
BANNED_CATEGORIES = {"ai", "tech", "world", "crypto", "finance"}  # nobody engages: never select
BANNED_RE = re.compile(r"\b(election|midterms?|senate|parliament|president|prime minister|"
                       r"bitcoin|crypto|tokens?|erc-20|coin|blockchain|stocks?|bonds?|etf|ai|openai|chatgpt|nvidia|"
                       r"software|rust|app|trump|biden|government|minister|market)\b", re.I)
LOW_CRICKET_RE = re.compile(r"\b(afghanistan|bangladesh|zimbabwe|ireland|nepal|uae|scotland|"
                            r"netherlands|namibia|oman|usa|west indies|sri lanka)\b", re.I)
BIG3_CRICKET_RE = re.compile(r"\b(india|australia|england|kohli|rohit)\b", re.I)
BIG_CRICKET_MOMENT_RE = re.compile(r"\b(world cup|final|ashes|record|historic|semi-?final)\b", re.I)
LOW_CRICKET_PENALTY = 0.5
ENTERTAINMENT_QUERIES = [
    '"Taylor Swift" OR "Sabrina Carpenter" OR "Billie Eilish" OR "Dua Lipa" OR "Ariana Grande"',
    'BTS OR Blackpink OR Jennie OR Jungkook OR "Stray Kids" OR aespa',
    'Zendaya OR "Sydney Sweeney" OR Marvel OR "red carpet" OR premiere',
    'Kardashian OR "Selena Gomez" OR Rihanna OR "Lady Gaga" OR "Bad Bunny"',
    'anime OR Ohtani OR UFC OR "viral video"',
]
COMPANY_RE = re.compile(r"\b(meta|google|apple|microsoft|amazon|tesla|spacex|openai|netflix|"
                        r"disney|marvel|nike|samsung|x|twitter)\b", re.I)
MATCH_RE = re.compile(r"\b([A-Z][a-z]+)\s+(?:vs?\.?|v)\s+([A-Z][a-z]+)")
MIN_VIDEO_SIDE = 720       # 720p minimum (shorter side) for videos
MIN_PHOTO_SIDE = 1080      # minimum shorter side for photos
MIN_PER_TOPIC = 2          # reserve up to this many of each priority topic's best stories
TOPIC_CAPS = {FAMOUS_LABEL: 4}      # per-topic overrides of MAX_PER_QUERY
CATEGORY_CAPS = {"famous_clip": 4, "sports": 4}  # per-category overrides of MAX_PER_CATEGORY (never relaxed)
CATEGORY_MIN = {"famous_clip": 2}   # reserve at least this many when available
MIN_YOUTUBE = 2            # reserve up to this many YouTube uploads ...
MAX_YOUTUBE = 6            # ... and cap them
FAMOUS_BOOST = 0.03        # rank bonus when a big name is in the headline
VIDEO_FAMOUS_BOOST = 0.12  # extra bonus when the story also has a video
POLITICIAN_WEIGHT = 0.3    # boosts are scaled down when the only names are politicians

# Output categories (the `category` field of each item)
CATEGORIES = ["famous_clip", "ai", "celebrity_music", "finance", "crypto", "sports", "tech",
              "world", "other"]

# --------------------------------------------------------------------------
# Famous people (for `famous_names`, the famous_clip category and the video boost).
# canonical name -> regex (matched on the lower-cased headline). Politicians count as
# famous names but with a smaller ranking boost, and alone they only make a famous_clip with an
# event word (ceremony, speech, ...), so ordinary politics doesn't flood the category.
# --------------------------------------------------------------------------
FAMOUS = {
    # tech / business
    "Elon Musk": r"elon musk|musk", "Jensen Huang": r"jensen huang|jensen",
    "Sam Altman": r"sam altman|altman", "Mark Zuckerberg": r"mark zuckerberg|zuckerberg",
    "Jeff Bezos": r"jeff bezos|bezos", "Bill Gates": r"bill gates",
    "Sundar Pichai": r"sundar pichai|pichai", "Satya Nadella": r"satya nadella|nadella",
    "Tim Cook": r"tim cook", "Sergey Brin": r"sergey brin", "Larry Page": r"larry page",
    "Lisa Su": r"lisa su", "Larry Ellison": r"larry ellison", "Warren Buffett": r"warren buffett|buffett",
    "Jack Ma": r"jack ma", "Mukesh Ambani": r"mukesh ambani|ambani", "Gautam Adani": r"gautam adani",
    "Ratan Tata": r"ratan tata", "Dario Amodei": r"dario amodei|amodei",
    "Demis Hassabis": r"demis hassabis|hassabis", "Marc Andreessen": r"marc andreessen",
    "Peter Thiel": r"peter thiel", "Michael Saylor": r"michael saylor|saylor",
    "Vitalik Buterin": r"vitalik buterin|vitalik", "Changpeng Zhao": r"changpeng zhao",
    "Jamie Dimon": r"jamie dimon", "Mark Cuban": r"mark cuban", "MrBeast": r"mrbeast|mr\.? beast",
    "Steve Jobs": r"steve jobs", "Nikhil Kamath": r"nikhil kamath", "Naval Ravikant": r"naval ravikant",
    "Masayoshi Son": r"masayoshi son", "Ilya Sutskever": r"ilya sutskever|sutskever",
    "Andrej Karpathy": r"karpathy", "Mustafa Suleyman": r"mustafa suleyman",
    "Daniel Ek": r"daniel ek", "David Sacks": r"david sacks", "Andrew Ng": r"andrew ng",
    "Melinda French Gates": r"melinda (?:french )?gates", "Reed Hastings": r"reed hastings",
    # athletes
    "Cristiano Ronaldo": r"cristiano ronaldo|ronaldo", "Lionel Messi": r"lionel messi|messi",
    "Virat Kohli": r"virat kohli|kohli", "MS Dhoni": r"ms dhoni|dhoni",
    "Rohit Sharma": r"rohit sharma", "Sachin Tendulkar": r"sachin tendulkar|tendulkar",
    "LeBron James": r"lebron", "Stephen Curry": r"stephen curry|steph curry",
    "Michael Jordan": r"michael jordan", "Eileen Gu": r"eileen gu",
    "Lewis Hamilton": r"lewis hamilton", "Max Verstappen": r"verstappen",
    "Lando Norris": r"lando norris", "Serena Williams": r"serena williams",
    "Novak Djokovic": r"djokovic", "Roger Federer": r"federer", "Rafael Nadal": r"rafael nadal|nadal",
    "Carlos Alcaraz": r"alcaraz", "Usain Bolt": r"usain bolt", "Neymar": r"neymar",
    "Kylian Mbappe": r"mbapp[eé]", "Erling Haaland": r"haaland", "Conor McGregor": r"mcgregor",
    "Tiger Woods": r"tiger woods", "Simone Biles": r"simone biles", "Neeraj Chopra": r"neeraj chopra",
    "Tom Brady": r"tom brady", "Shohei Ohtani": r"ohtani", "David Beckham": r"beckham",
    "Kevin Durant": r"kevin durant", "Dwyane Wade": r"dwyane wade", "Yao Ming": r"yao ming",
    "Jackie Chan": r"jackie chan",
    # entertainment / creators
    "Taylor Swift": r"taylor swift", "Shah Rukh Khan": r"shah rukh khan|shahrukh khan|srk",
    "Salman Khan": r"salman khan", "Amitabh Bachchan": r"amitabh bachchan",
    "Priyanka Chopra": r"priyanka chopra", "Deepika Padukone": r"deepika padukone",
    "Ranveer Singh": r"ranveer singh", "Alia Bhatt": r"alia bhatt", "Kim Kardashian": r"kim kardashian",
    "Kanye West": r"kanye west|kanye", "Drake": r"drake(?! maye)", "Beyonce": r"beyonc[eé]",
    "Rihanna": r"rihanna", "Selena Gomez": r"selena gomez", "Justin Bieber": r"justin bieber|bieber",
    "Oprah Winfrey": r"oprah", "Will Smith": r"will smith", "Dwayne Johnson": r"dwayne johnson",
    "Leonardo DiCaprio": r"dicaprio", "Tom Cruise": r"tom cruise", "Keanu Reeves": r"keanu reeves",
    "Zendaya": r"zendaya", "Harry Styles": r"harry styles", "Ariana Grande": r"ariana grande",
    "Lady Gaga": r"lady gaga", "Billie Eilish": r"billie eilish", "Sabrina Carpenter": r"sabrina carpenter",
    "Travis Kelce": r"travis kelce", "Jake Paul": r"jake paul", "Bad Bunny": r"bad bunny", "Ed Sheeran": r"ed sheeran",
    "Joe Rogan": r"joe rogan|rogan", "Jay Shetty": r"jay shetty", "Steven Bartlett": r"steven bartlett",
    "Lex Fridman": r"lex fridman", "Andrew Huberman": r"huberman",
    # world leaders (smaller boost, see POLITICIANS)
    "Donald Trump": r"donald trump|trump", "Narendra Modi": r"narendra modi|modi",
    "Barack Obama": r"barack obama|obama", "Xi Jinping": r"xi jinping", "Vladimir Putin": r"putin",
}
POLITICIANS = {"Donald Trump", "Narendra Modi", "Barack Obama", "Xi Jinping", "Vladimir Putin"}
FAMOUS_RE = {n: re.compile(r"(?<![\w])(?:" + rx + r")(?![\w])", re.I) for n, rx in FAMOUS.items()}
# YouTube titles often carry the guest only as a hashtag (#eileengu, #elonmusk)
FAMOUS_TAG = {n: "#" + re.sub(r"[^a-z0-9]", "", n.lower()) for n in FAMOUS}
# words that suggest a clip of the person speaking / an event moment
CLIP_RE = re.compile(
    r"\b(interview\w*|podcasts?|keynote|speech|ceremony|awarded|awards?|medal|honou?red|"
    r"commencement|fireside|on stage|opens? up|reveals?|advice|lessons?|conversation|episode|"
    r"documentary|address(?:es|ed)?|talks? about|explains?|tells|in conversation|"
    r"diary of a ceo|on purpose|60 minutes|ted talk|"
    r"clip|full video|watch)\b", re.I)
# event-style words that make even a politician-only headline a famous_clip
EVENT_RE = re.compile(r"\b(ceremony|awarded|medal|honou?red|presents?|presented|commencement|keynote)\b", re.I)


def famous_names(text: str) -> list[str]:
    """Recognised big names in a headline (canonical spelling, in order of appearance)."""
    hits = []
    text = text or ""
    low = text.lower()
    for name, rx in FAMOUS_RE.items():
        m = rx.search(text)
        if m:
            hits.append((m.start(), name))
        elif "#" in low:
            tag = FAMOUS_TAG[name]
            pos = low.find(tag)
            if pos >= 0 and not low[pos + len(tag):pos + len(tag) + 1].isalnum():
                hits.append((pos, name))
    return [n for _, n in sorted(hits)]


def is_famous_clip(title: str, from_youtube: bool = False, names: list | None = None) -> bool:
    names = famous_names(title) if names is None else names
    if not names:
        return False
    stars = [n for n in names if n not in POLITICIANS]
    if stars:
        return from_youtube or bool(CLIP_RE.search(title))
    return bool(EVENT_RE.search(title)) and (from_youtube or bool(CLIP_RE.search(title)))


# Keyword patterns (regex, matched on lower-cased headlines) for the output category.
CATEGORY_PATTERNS = {
    "crypto": r"\b(bitcoin|ethereum|ether|crypto\w*|solana|xrp|btc|dogecoin|stablecoins?|"
              r"binance|coinbase|memecoins?|blockchain|altcoins?)\b",
    "ai": r"\b(ai|a\.i\.|artificial intelligence|openai|chatgpt|gpt-?\d\w*|gemini|claude|grok|"
          r"anthropic|llms?|agentic|copilot|deepseek|perplexity|midjourney|mistral|chatbots?|"
          r"machine learning|altman|no-code|automation)\b",
    "sports": r"\b(cricket|football|soccer|f1|formula 1|grand prix|tennis|"
              r"golf|ipl|t20|odi|wicket|premier league|champions league|la liga|uefa|fifa|"
              r"olympics?|world cup|match|innings|goalkeeper|striker|batter|bowler)\b",
    "celebrity_music": r"\b(singer|rapper|album|songs?|concerts?|world tour|tour dates|"
                       r"trailer|teaser|bollywood|hollywood|tollywood|grammys?|oscars?|emmys?|"
                       r"actor|actress|celebrity|pop star|music video|box office|"
                       r"taylor swift|drake|bts|bad bunny|weeknd|beyonc[eé]|arijit|diljit|"
                       r"netflix|film|movie|wedding|engaged)\b",
    "finance": r"\b(sensex|nifty|dow|nasdaq|s&p|stocks?|stock market|shares|wall street|"
               r"fed|federal reserve|rate cut|interest rates?|inflation|rbi|gold price|"
               r"gold rate|bond yields?|treasury|rupee|ipo|earnings|markets?)\b",
    "tech": r"\b(apple|iphone|google|microsoft|android|samsung|nvidia|chips?|semiconductors?|"
            r"tesla|spacex|software|app|meta|startup|robots?|cyber\w*|smartphone|nasa|"
            r"space|satellite|rocket)\b",
    "world": r"\b(war|president|prime minister|minister|election|israel|ukraine|russia|china|"
             r"gaza|iran|trump|government|police|killed|attack|court|parliament|"
             r"congress|senate|protest|earthquake|flood)\b",
}
CATEGORY_RE = {k: re.compile(v, re.I) for k, v in CATEGORY_PATTERNS.items()}
# Google News section -> output category (used when keywords are not decisive)
SECTION_CATEGORY = {"WORLD": "world", "BUSINESS": "finance", "TECHNOLOGY": "tech",
                    "ENTERTAINMENT": "celebrity_music", "SPORTS": "sports", "SCIENCE": "tech"}

TRENDS_GEOS = ["US", "IN", "GB", "JP"]

# YouTube channel RSS feeds (free, no key): https://www.youtube.com/feeds/videos.xml?channel_id=...
# All IDs verified against the feed's channel title. kind: podcast = interview/podcast shows
# (the guest is usually in the video title, so a famous name alone makes a famous_clip),
# news = TV/interview channels and official = company / league / org channels (these need
# a clip word like interview/keynote/speech/ceremony in the title to count as famous_clip).
# The host's own name (e.g. Joe Rogan on PowerfulJRE) is not counted as a famous name.
# These are for the assistant to find the moment or the official clip; reuse is its call.
# name -> (channel_id, kind, Google News-style section hint or None)
YOUTUBE_CHANNELS = {
    "Lex Fridman": ("UCSHZKyawb77ixDdsGog4iWA", "podcast", None),
    "The Diary Of A CEO": ("UCGq-a57w-aPwyi3pW7XLiHw", "podcast", None),
    "PowerfulJRE": ("UCzQUP1qoWDoEbmsQxvdjxgQ", "podcast", None),
    "JRE Clips": ("UCnxGkOGNMqQEUMvroOWps6Q", "podcast", None),
    "Jay Shetty Podcast": ("UCbk_QsfaFZG6PdQeCvaYXJQ", "podcast", None),
    "All-In Podcast": ("UCESLZhusAkFfsNsApnjF_Cg", "podcast", None),
    "Dwarkesh Patel": ("UCXl4i9dYBrFOabk0xGmbkRA", "podcast", None),
    "Y Combinator": ("UCcefcZRL2oaA_uBNeo5UOWg", "podcast", "TECHNOLOGY"),
    "Stanford GSB": ("UCGwuxdEeCf0TIA2RbPOj-8g", "podcast", None),
    "TED": ("UCAuUUnT6oDeKwE6v1NGQxug", "podcast", None),
    "60 Minutes": ("UCsN32BtMd0IoByjJRNF12cw", "news", None),
    "Bloomberg Originals": ("UCUMZ7gohGI9HcU9VNsr2FJQ", "news", "BUSINESS"),
    "Bloomberg Television": ("UCIALMKvObZNtJ6AmdCLP7Lg", "news", "BUSINESS"),
    "CNBC Television": ("UCrp_UI8XtuYfpiqluWLD7Lw", "news", "BUSINESS"),
    "OpenAI": ("UCXZCJLdBC09xxGZ6gcdrc6A", "official", "TECHNOLOGY"),
    "Google": ("UCK8sQmJBp8GCxrOtXWBpyEA", "official", "TECHNOLOGY"),
    "NVIDIA": ("UCHuiy8bXnmK5nisYHUd1J5g", "official", "TECHNOLOGY"),
    "Microsoft": ("UCFtEEv80fQVKkD4h1PF-Xqw", "official", "TECHNOLOGY"),
    "Apple": ("UCE_M8A5yxnLfW0KghEeajjw", "official", "TECHNOLOGY"),
    "Tesla": ("UC5WjFrtBdufl6CZojX3D8dQ", "official", "TECHNOLOGY"),
    "SpaceX": ("UCtI0Hodo5o5dUb67FeUjDeA", "official", "TECHNOLOGY"),
    "The White House": ("UCYxRlFDqcWM4y7FfpiAN3KQ", "official", "WORLD"),
    "Formula 1": ("UCB_qr75-ydFVKSF9Dmo6izg", "official", "SPORTS"),
    "ICC": ("UCt2JXOLNxqry7B_4rRZME3Q", "official", "SPORTS"),
    "Royal Challengers Bengaluru": ("UCCq1xDJMBRF61kiOgU90_kw", "official", "SPORTS"),
    "NBA": ("UCWJ2lWNubArHWmf3FIHbfcQ", "official", "SPORTS"),
    "Olympic Games": ("UCTl3QQTvqHFjurroKxexy2Q", "official", "SPORTS"),
    "Real Madrid": ("UCWV3obpZVGgJ3j9FVhEjF2Q", "official", "SPORTS"),
    "FC Barcelona": ("UC14UlmYlSNiQCBe9Eookf_A", "official", "SPORTS"),
    "UR Cristiano": ("UCtxD0x6AuNNqdXO9Wp5GHew", "official", "SPORTS"),
}

STOPWORDS = set("""
a an the and or but of to in on at for from by with as is are was were be been being
it its this that these those after before over under into about amid says say said
new news live update updates latest how why what who when where will would could can
may might has have had not no vs v his her their our your he she they we you i
up out off more than just also first top day today week year video watch report
january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec monday tuesday wednesday thursday friday
saturday sunday tomorrow yesterday tonight price prices check
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
        if re.fullmatch(r"20[0-9]{2}", w):  # years ("2026") glue unrelated dated headlines
            continue
        if w.isascii() and len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def headline_key(text: str) -> str:
    t = sorted(tokens(text))
    return "-".join(t[:12])


def strict_similar(a: set, b: set) -> bool:
    """Tighter test used when a YouTube upload is involved (titles are short / generic)."""
    if not a or not b:
        return False
    inter = len(a & b)
    return inter >= 3 and inter / min(len(a), len(b)) >= 0.6 and inter / len(a | b) >= 0.3


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
    # extra entertainment / pop-culture supply (account niche: celebs, music, anime, viral)
    for code, ed in LOCAL_EDITIONS.items():
        if code == "JP":
            feeds.append(dict(name=f"GN entertainment JP ({ed[3]})",
                              url=gn_url("/headlines/section/topic/ENTERTAINMENT", ed),
                              edition=code, category="ENTERTAINMENT", lang=ed[3], query=None))
    for q in ENTERTAINMENT_QUERIES:
        for code in ("US", "GB"):
            ed = EN_EDITIONS[code]
            path = f"/search?q={quote_plus(q + ' when:' + str(MAX_AGE_HOURS) + 'h')}"
            feeds.append(dict(name=f"GN ent '{q[:30]}' {code}", url=gn_url(path, ed),
                              edition=code, category="ENTERTAINMENT", lang="en", query=None))
    for label, topic in PRIORITY_TOPICS.items():
        for q, codes in topic["queries"]:
            for code in codes:
                ed = EN_EDITIONS[code]
                path = f"/search?q={quote_plus(q + ' when:' + str(MAX_AGE_HOURS) + 'h')}"
                feeds.append(dict(name=f"GN search '{q}' {code}", url=gn_url(path, ed),
                                  edition=code, category=None, lang="en", query=label))
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


YOUTUBE_HOSTS = {"Lex Fridman": "Lex Fridman", "The Diary Of A CEO": "Steven Bartlett",
                 "PowerfulJRE": "Joe Rogan", "JRE Clips": "Joe Rogan",
                 "Jay Shetty Podcast": "Jay Shetty"}


def collect_youtube() -> tuple[list[dict], dict]:
    """Latest uploads of the YOUTUBE_CHANNELS (RSS, ~15 newest per channel)."""
    items, stats = [], {"yt_feeds_ok": 0, "yt_feeds_failed": 0}

    def work(name):
        cid, kind, section = YOUTUBE_CHANNELS[name]
        raw = fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
        if raw is None:
            return name, None
        try:
            return name, feedparser.parse(raw)
        except Exception:  # noqa: BLE001
            return name, None

    with ThreadPoolExecutor(WORKERS) as ex:
        for name, parsed in ex.map(work, list(YOUTUBE_CHANNELS)):
            if not parsed or not parsed.entries:
                stats["yt_feeds_failed"] += 1
                continue
            stats["yt_feeds_ok"] += 1
            cid, kind, section = YOUTUBE_CHANNELS[name]
            for e in parsed.entries:
                title = html.unescape(e.get("title", "")).strip()
                vid = e.get("yt_videoid") or ""
                url = f"https://www.youtube.com/watch?v={vid}" if vid else e.get("link")
                if not title or not url:
                    continue
                thumbs = [t.get("url") for t in (e.get("media_thumbnail") or []) if t.get("url")]
                if not thumbs and vid:
                    thumbs = [f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"]
                desc = html.unescape(e.get("summary", "") or "").strip()
                items.append(dict(
                    title=title, source=f"YouTube: {name}", url=url, published=entry_time(e),
                    feed=f"YT {name}", edition="YT", category=section, lang="en", query=None,
                    related=[], source_type="youtube", yt_channel=name, yt_kind=kind,
                    video_url=url, thumb=thumbs[0] if thumbs else None,
                    is_short=bool(re.search(r"#shorts?\b", title, re.I)),
                    description=re.sub(r"\s+", " ", desc)[:400],
                ))
    return items, stats


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
        is_yt = it.get("source_type") == "youtube"
        if target is None:
            for n, c in enumerate(clusters):
                if c["lang"] != it["lang"]:
                    continue
                test = strict_similar if (is_yt or not c["has_news"]) else similar
                if any(test(tk, m) for m in c["token_sets"]):
                    target = n
                    break
        if target is None:
            clusters.append(dict(items=[], token_sets=[], lang=it["lang"], urls=set(), has_news=False))
            target = len(clusters) - 1
        c = clusters[target]
        c["has_news"] = c["has_news"] or not is_yt
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
    bad = re.compile(r"\b(live|commentary|newsletter|highlights|preview|morning|evening|briefing)\b|\band, and\b", re.I)
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


def priority_match(text_tokens: set, title: str, from_youtube: bool = False) -> list[str]:
    low = title.lower()
    hits = []
    for q, kws in PRIORITY_QUERIES.items():
        for k in kws:
            if k in text_tokens or re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", low):
                hits.append(q)
                break
    if FAMOUS_LABEL not in hits and is_famous_clip(title, from_youtube):
        hits.append(FAMOUS_LABEL)
    return hits


def cluster_topics(its: list[dict], lead: dict) -> set:
    """Priority topics of a cluster. A topic counts if the lead headline matches it,
    or if a fair share of the cluster's items came from / mention that topic
    (loose clusters would otherwise collect every topic)."""
    counts: dict[str, int] = {}
    for i in its:
        labels = set(priority_match(i["tokens"], i["title"], i.get("yt_kind") == "podcast"))
        if i["query"]:
            labels.add(i["query"])
        for l in labels:
            counts[l] = counts.get(l, 0) + 1
    lead_labels = set(priority_match(lead["tokens"], lead["title"], lead.get("yt_kind") == "podcast"))
    need = max(2, math.ceil(0.4 * len(its)))
    if len(its) == 1:
        need = 1
    return {l for l, n in counts.items() if l in lead_labels or n >= need}


def guess_section(c_items: list[dict]) -> str | None:
    """Majority Google News section (WORLD, BUSINESS, ...) of the cluster, if any."""
    votes: dict[str, int] = {}
    for it in c_items:
        if it["category"]:
            votes[it["category"]] = votes.get(it["category"], 0) + 1
    return max(votes, key=votes.get) if votes else None


def guess_category(c_items: list[dict], lead_title: str, queries=(), section=None,
                   names=(), from_youtube=False) -> str:
    """Output category: famous_clip, ai, celebrity_music, finance, crypto, sports, tech, world, other."""
    # Famous person speaking / big moment: clip-style headline, or a YouTube upload
    # from an interview/podcast channel naming the star.
    if names and is_famous_clip(lead_title, from_youtube, list(names)):
        return "famous_clip"
    if names and FAMOUS_LABEL in queries and any(n not in POLITICIANS for n in names) and \
            re.search(r"\b(says?|said|tells?|on)\b", lead_title, re.I):
        return "famous_clip"
    text = " ".join([lead_title] + [i["title"] for i in c_items[:6] if i["lang"] == "en"]).lower()
    lead = lead_title.lower()
    hits = {k: len(rx.findall(text)) for k, rx in CATEGORY_RE.items()}
    lead_hit = {k: bool(rx.search(lead)) for k, rx in CATEGORY_RE.items()}
    q_cats = [PRIORITY_TOPICS[q]["category"] for q in queries
              if q in PRIORITY_TOPICS and PRIORITY_TOPICS[q]["category"]]
    # Strong, specific signals first
    if lead_hit["crypto"] or ("crypto" in q_cats and hits["crypto"]):
        return "crypto"
    if lead_hit["ai"] or ("ai" in q_cats and hits["ai"]):
        return "ai"
    if "sports" in q_cats or section == "SPORTS" or (lead_hit["sports"] and hits["sports"] >= 2):
        return "sports"
    if "celebrity_music" in q_cats or section == "ENTERTAINMENT" or \
            (lead_hit["celebrity_music"] and hits["celebrity_music"] >= 2):
        return "celebrity_music"
    if "finance" in q_cats or (lead_hit["finance"] and hits["finance"] >= 2):
        return "finance"
    if "tech" in q_cats:
        return "tech"
    if section in SECTION_CATEGORY:
        return SECTION_CATEGORY[section]
    if "world" in q_cats:
        return "world"
    best, best_n = None, 0
    for k in ("tech", "world", "finance", "celebrity_music", "sports"):
        if hits[k] > best_n:
            best, best_n = k, hits[k]
    if best:
        return best
    return "other"


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
        queries = cluster_topics(its, lead)

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

        yt_items = [i for i in its if i.get("source_type") == "youtube"]
        news_items = [i for i in its if i.get("source_type") != "youtube"]
        from_youtube = bool(yt_items)
        # interview/podcast uploads are clip evidence by themselves; official-channel
        # uploads (F1, NBA, Tesla ...) still need a clip word in the headline
        from_podcast = any(y["yt_kind"] == "podcast" for y in yt_items)
        source_type = "youtube" if not news_items else ("news+youtube" if yt_items else "news")
        names = famous_names(lead["title"])
        # names in a YouTube title when a news headline leads (same story, video attached)
        for y in yt_items[:2]:
            names += [n for n in famous_names(y["title"]) if n not in names]
        hosts = {YOUTUBE_HOSTS.get(y["yt_channel"]) for y in yt_items}
        if not news_items:  # the podcast host is always in the title; not a reason by itself
            names = [n for n in names if n not in hosts]

        age_min = int((now - newest).total_seconds() // 60)
        window = YT_MAX_AGE_HOURS if source_type == "youtube" else MAX_AGE_HOURS
        recency = max(0.0, 1 - age_min / (window * 60))
        text_all = " ".join([lead["title"]] + [i["title"] for i in its[:10]])
        india_topic = bool(INDIA_RE.search(text_all))
        global_pop = bool(GLOBAL_POP_RE.search(text_all))
        if india_topic:
            trend_score += INDIA_STAR_BONUS
        if global_pop:
            trend_score += GLOBAL_POP_BONUS
        rank = 0.45 * recency + 0.55 * min(1.0, math.log1p(trend_score) / math.log1p(30))
        # geo boost only for specific (multi-word) trend terms, not "bond" / "rams"
        trend_geos = {g for g in ("US", "IN", "GB", "JP") for h in trend_hits
                      if f" ({g} " in h and len(h.split(" (")[0].split()) >= 2}
        if trend_geos & set(GLOBAL_GEOS):
            rank *= GLOBAL_GEO_BOOST
        local = bool(LOCAL_RE.search(lead["title"])) and not global_pop and not india_topic
        if local and not (re.search(r"indigo|air india|akasa|spicejet", lead["title"], re.I)
                          and MAJOR_CRASH_RE.search(lead["title"])):
            rank *= LOCAL_PENALTY
        us_league = bool(US_LEAGUE_RE.search(text_all))
        huge = bool(SUPER_MOMENT_RE.search(text_all)) or len(trend_geos) >= 2
        if us_league and huge:
            us_league = False  # huge moment: no penalty, no daily cap
        if us_league:
            rank *= US_LEAGUE_PENALTY
        if (re.search(r"\b(cricket|test|odi|t20)\b", text_all, re.I) and LOW_CRICKET_RE.search(lead["title"])
                and not (BIG3_CRICKET_RE.search(lead["title"]) and BIG_CRICKET_MOMENT_RE.search(text_all))):
            rank *= LOW_CRICKET_PENALTY
        if trend_score < MIN_TREND_SCORE:
            continue
        if c["lang"] != "en":
            rank *= NON_EN_PENALTY
        if names:
            weight = 1.0 if any(n not in POLITICIANS for n in names) else POLITICIAN_WEIGHT
            rank += FAMOUS_BOOST * weight
            if from_youtube:  # video already known -> full video+famous boost now
                rank += VIDEO_FAMOUS_BOOST * weight

        # skip ticker-style / too-short headlines ("$Bitcoin (BTC.CC)$", "Gold gains 1.2%")
        if c["lang"] == "en" and len(re.findall(r"[A-Za-z]{2,}", lead["title"])) < 4:
            continue

        key = headline_key(lead["title"])
        if any(is_posted(i["tokens"], posted_keys) for i in its[:20]):
            continue

        direct_urls = [d["url"] for d in direct if d.get("url")]
        yt_videos = [dict(title=y["title"], channel=y["yt_channel"], url=y["video_url"],
                          published_at_ist=ist(y["published"]), thumbnail=y.get("thumb"),
                          is_short=y["is_short"])
                     for y in yt_items[:4]]
        section = guess_section(news_items or its)
        summary = next((y["description"] for y in yt_items if y.get("description")), "") \
            if source_type == "youtube" else ""
        out.append(dict(
            id=hashlib.sha1(key.encode()).hexdigest()[:10],
            key=key,
            headline=lead["title"],
            summary=summary,
            sources=sources[:10],
            article_urls=list(dict.fromkeys(direct_urls[:3] + urls))[:8],
            published_at_utc=iso(newest),
            published_at_ist=ist(newest),
            first_seen_utc=iso(min(i["published"] for i in its)),
            age_minutes=age_min,
            trend_score=trend_score,
            rank_score=round(rank, 4),
            category=guess_category(its, lead["title"], queries, section, names, from_podcast),
            section=(section or "TOP").lower(),
            source_type=source_type,
            famous_names=names,
            region=sorted(editions),
            language=c["lang"],
            priority_queries=sorted(queries),
            google_trends=trend_hits[:5],
            india_topic=india_topic,
            global_pop=global_pop,
            local_only=local,
            us_league=us_league,
            other_headlines=[t for t in dict.fromkeys(i["title"] for i in its) if t != lead["title"]][:4],
            image_urls=([y["thumbnail"] for y in yt_videos if y["thumbnail"]][:1] +
                        [d["picture"] for d in direct if d.get("picture")])[:3],
            video_urls=[y["url"] for y in yt_videos],
            has_video=bool(yt_videos),
            youtube_videos=yt_videos,
            _boosted=from_youtube,
            _gn_urls=list(dict.fromkeys(
                ([lead["url"]] if lead.get("source_type") != "youtube" else []) +
                [i["url"] for i in news_items if i["url"]]))[:MAX_URLS_PER_CLUSTER + 2],
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
            u = u.replace("&amp;", "&").strip()  # attrs are already decoded by bs4
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
    c["image_urls"].sort(key=lambda u: ("gstatic.com" in u, "ytimg.com" in u))
    c["video_urls"] = [u for u in c["video_urls"] if media_ok(u, True)]
    c["image_urls"] = [u for u in c["image_urls"] if media_ok(u, False)]
    c["has_video"] = bool(c["video_urls"])
    # video + famous name boost for news stories (YouTube ones got it before selection)
    if c["has_video"] and c["famous_names"] and not c.get("_boosted"):
        weight = 1.0 if any(n not in POLITICIANS for n in c["famous_names"]) else POLITICIAN_WEIGHT
        c["rank_score"] = round(c["rank_score"] + VIDEO_FAMOUS_BOOST * weight, 4)
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
        f"{sum(1 for i in items if i['has_video'])} with video · "
        f"{sum(1 for i in items if i['category'] == 'famous_clip')} famous_clip · "
        f"{sum(1 for i in items if i['source_type'] != 'news')} from YouTube "
        f"(YouTube window {YT_MAX_AGE_HOURS} h)  ",
        "Sorted newest first. `rank` = ranking order in candidates.json (1 = best).",
        "",
        "| # | rank | age | published (IST) | headline | cat | famous | src | region | trend | video | image | sources |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
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
            f"{it['category']} | {md_escape(', '.join(it['famous_names'][:3])) or '—'} | {it['source_type']} | "
            f"{','.join(it['region'])} | {it['trend_score']} | {vid} | {img} | "
            f"{md_escape(', '.join(it['sources'][:4]))} |"
        )
    lines.append("")
    (DATA / "candidates.md").write_text("\n".join(lines), encoding="utf-8")


TWIMG_RES_RE = re.compile(r"/(\d{2,4})x(\d{2,4})/")


def media_ok(url: str, is_video: bool) -> bool:
    """720p gate for media whose size is in the URL (twimg variants: /vid/WxH/).
    Unknown sizes pass here; the routine verifies with check_media.py (ffprobe)."""
    m = TWIMG_RES_RE.search(url or "")
    if not m:
        return True
    side = min(int(m.group(1)), int(m.group(2)))
    return side >= (MIN_VIDEO_SIDE if is_video else MIN_PHOTO_SIDE)


def select_top(ranked: list[dict]) -> list[dict]:
    """Pick TOP_N stories. First reserve the best MIN_PER_TOPIC stories of every
    priority topic (so each topic/category is represented), then fill by rank.
    Caps: MAX_PER_QUERY per priority topic, MAX_PER_CATEGORY per category."""
    chosen: list[dict] = []
    ids: set = set()
    per_q: dict[str, int] = {}
    per_cat: dict[str, int] = {}

    n_yt = [0]
    per_ent: dict[str, int] = {}
    n_us = [0]

    def ents(c):
        h = c["headline"]
        e = [n.lower() for n in c.get("famous_names", [])]
        e += ["co:" + m.lower() for m in COMPANY_RE.findall(h)]
        e += ["match:" + "-".join(sorted(m)).lower() for m in MATCH_RE.findall(h)]
        return e

    def is_yt(c):
        return c.get("source_type") == "youtube"

    def ok(c, cat_cap):
        if c["id"] in ids:
            return False
        if any(per_q.get(q, 0) >= TOPIC_CAPS.get(q, MAX_PER_QUERY) for q in c["priority_queries"]):
            return False
        if is_yt(c) and n_yt[0] >= MAX_YOUTUBE:
            return False
        if c.get("us_league") and n_us[0] >= MAX_US_LEAGUE:
            return False
        if any(per_ent.get(e, 0) >= MAX_PER_ENTITY for e in ents(c)):
            return False
        cap = min(cat_cap, CATEGORY_CAPS.get(c["category"], cat_cap))
        return per_cat.get(c["category"], 0) < cap

    def take(c):
        chosen.append(c)
        ids.add(c["id"])
        for q in c["priority_queries"]:
            per_q[q] = per_q.get(q, 0) + 1
        per_cat[c["category"]] = per_cat.get(c["category"], 0) + 1
        n_yt[0] += is_yt(c)
        n_us[0] += bool(c.get("us_league"))
        for e in ents(c):
            per_ent[e] = per_ent.get(e, 0) + 1

    # 0a) reserve categories with a minimum (famous_clip), best-ranked first
    for cat, need in CATEGORY_MIN.items():
        for c in ranked:
            if per_cat.get(cat, 0) >= need:
                break
            if c["category"] == cat and ok(c, MAX_PER_CATEGORY):
                take(c)
    # 0b) reserve a few YouTube uploads. They are older and less covered than news, so
    #     they rank lower; prefer famous-person clips, then stars, then ready-made shorts.
    yt_pref = sorted((c for c in ranked if is_yt(c)), key=lambda c: (
        c["category"] == "famous_clip", any(n not in POLITICIANS for n in c["famous_names"]),
        any(y.get("is_short") for y in c["youtube_videos"]), c["rank_score"]), reverse=True)
    for c in yt_pref:
        if n_yt[0] >= MIN_YOUTUBE:
            break
        if ok(c, MAX_PER_CATEGORY):
            take(c)
    # 1) reservation pass per priority topic
    for label in PRIORITY_TOPICS:
        n = 0
        for c in ranked:
            if n >= MIN_PER_TOPIC or len(chosen) >= TOP_N:
                break
            if label in c["priority_queries"] and ok(c, MAX_PER_CATEGORY):
                take(c)
                n += 1
    # 2) fill by rank, then 3) relax the category cap if still short
    for cat_cap in (MAX_PER_CATEGORY, MAX_PER_CATEGORY + 2):
        for c in ranked:
            if len(chosen) >= TOP_N:
                break
            if ok(c, cat_cap):
                take(c)
    chosen.sort(key=lambda x: x["rank_score"], reverse=True)
    return chosen


def main() -> int:
    t0 = time.time()
    now = now_utc()
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)

    yt_cutoff = now - timedelta(hours=YT_MAX_AGE_HOURS)

    feeds = build_feeds()
    log(f"Fetching {len(feeds)} news feeds + {len(TRENDS_GEOS)} trends feeds + "
        f"{len(YOUTUBE_CHANNELS)} YouTube channels ...")
    with ThreadPoolExecutor(3) as ex:
        f_items = ex.submit(collect_items, feeds)
        f_trends = ex.submit(collect_trends)
        f_yt = ex.submit(collect_youtube)
        items, stats = f_items.result()
        trends = f_trends.result()
        yt_items, yt_stats = f_yt.result()
    log(f"  feeds ok={stats['feeds_ok']} failed={stats['feeds_failed']} raw items={len(items)} trends={len(trends)}")
    log(f"  youtube feeds ok={yt_stats['yt_feeds_ok']} failed={yt_stats['yt_feeds_failed']} uploads={len(yt_items)}")

    fresh = [i for i in items if i["published"] and cutoff <= i["published"] <= now + timedelta(minutes=10)]
    yt_fresh = [i for i in yt_items if i["published"] and yt_cutoff <= i["published"] <= now + timedelta(minutes=10)]
    log(f"  fresh (<= {MAX_AGE_HOURS}h) items: {len(fresh)}  youtube (<= {YT_MAX_AGE_HOURS}h): {len(yt_fresh)}")
    fresh += yt_fresh

    clusters = cluster(fresh)
    posted = load_posted()
    ranked = summarize_clusters(clusters, trends, posted, now)
    ranked = [c for c in ranked if c["category"] not in BANNED_CATEGORIES
              and not BANNED_RE.search(c["headline"])]
    log(f"  clusters: {len(clusters)}  after posted-filter: {len(ranked)}")
    top = select_top(ranked)
    log("  selected per category: " + ", ".join(
        f"{k}={sum(1 for c in top if c['category'] == k)}" for k in CATEGORIES))

    log("Resolving articles + media ...")
    with ThreadPoolExecutor(WORKERS) as ex:
        top = list(ex.map(enrich_cluster, top))
    for c in top:
        c.pop("_gn_urls", None)
        c.pop("_direct_urls", None)
        c.pop("_boosted", None)
    top.sort(key=lambda x: x["rank_score"], reverse=True)  # after the video+famous boost

    meta = dict(
        generated_at_utc=iso(now),
        generated_at_ist=ist(now),
        window_hours=MAX_AGE_HOURS,
        youtube_window_hours=YT_MAX_AGE_HOURS,
        stats=dict(feeds=len(feeds), feeds_ok=stats["feeds_ok"], feeds_failed=stats["feeds_failed"],
                   raw_items=len(items), fresh_items=len(fresh) - len(yt_fresh), clusters=len(clusters),
                   youtube_feeds_ok=yt_stats["yt_feeds_ok"], youtube_feeds_failed=yt_stats["yt_feeds_failed"],
                   youtube_uploads_fresh=len(yt_fresh),
                   youtube_items=sum(1 for c in top if c["source_type"] != "news"),
                   famous_items=sum(1 for c in top if c["famous_names"]),
                   trends_terms=len(trends), page_fetches=_page_fetches,
                   with_video=sum(1 for c in top if c["has_video"]),
                   runtime_sec=round(time.time() - t0, 1)),
        categories={k: sum(1 for c in top if c["category"] == k) for k in CATEGORIES},
        top_trends=[f'{t["term"]} ({t["geo"]} {t["traffic"]})' for t in trends[:20]],
    )
    write_outputs(top, meta)
    log(f"Wrote {len(top)} items in {meta['stats']['runtime_sec']}s "
        f"({meta['stats']['with_video']} with video, {_page_fetches} page fetches)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
