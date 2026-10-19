#!/usr/bin/env python3
"""media_prep.py: turn data/candidates.json into upload-ready media for the posting routine.

Modes
  python media_prep.py            CI (chad-media "Prepare media" workflow): non-YouTube videos + photos
  python media_prep.py box        on the box (cron): the YouTube videos CI can't download
  python media_prep.py merge PART merge a partial result into data/media_ready.json

Each video: best original via yt-dlp (4K preferred, the highest-res source of the story wins),
skipped if under 1080p; cut to 2:20 if longer (around the "most replayed" heatmap peak or a
key chapter when available, else from 0:00); a 1080p H.264/AAC ~10 Mbps file; a 4K H.264 file
when the source is 4K and the file is 512 MB or less; 3 small check frames.
Files are uploaded as assets of a GitHub Release in the public media repo. The "Publish Pages"
workflow there then mirrors every Pages URL listed in media_ready.json to GitHub Pages.

Env: MEDIA_REPO, MEDIA_PART (output path of the partial result), MAX_VIDEOS (12),
MEDIA_BUDGET_SEC, MAX_4K, KEEP_DAYS (3), PAGES_MAX_MB, SKIP_YOUTUBE=1 (CI), DRY_RUN=1 (no uploads).
Auth: GH_TOKEN env (the workflow token in CI) or, if unset, the logged-in `gh` CLI. No secrets here.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import yt_dlp
from PIL import Image

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
READY = DATA / "media_ready.json"
MODE = sys.argv[1] if len(sys.argv) > 1 else "ci"
WORK = Path(os.environ.get("MEDIA_WORK", "/tmp/media_work"))
REPO = os.environ.get("MEDIA_REPO", "chatgpt820970/chad-media")
MAX_VIDEOS = int(os.environ.get("MAX_VIDEOS", "12"))
BUDGET = int(os.environ.get("MEDIA_BUDGET_SEC", "150" if MODE == "box" else "1080"))
KEEP_DAYS = float(os.environ.get("KEEP_DAYS", "3"))
MAX_4K = int(os.environ.get("MAX_4K", "4"))
SKIP_YT = os.environ.get("SKIP_YOUTUBE") == "1"
DRY = os.environ.get("DRY_RUN") == "1"
PAGES_BASE = "https://" + REPO.split("/")[0] + ".github.io/" + REPO.split("/")[1] + "/m"
# Pages site limit is 1 GB: CI may put up to 700 MB there, the box up to 250 MB.
PAGES_MAX = int(os.environ.get("PAGES_MAX_MB", "250" if MODE == "box" else "700")) * 1048576
COOKIES = os.environ.get("YT_COOKIES_FILE")
MAX_SEC = 140              # Buffer/X: up to 2:20
MAX_BYTES = 512 * 1024 * 1024
MAX_FULL_DL = 450 * 1048576   # bigger sources are fetched as a 2:20 section instead of in full
MAX_PHOTO_STORIES = 20
IST = timezone(timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
KEY_CHAPTER = re.compile(r"highlight|goal|moment|best|key|reveal|announce|speech|performance|trailer|"
                         r"winning|reaction|full clip|the clip", re.I)
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:6.0f}s]", *a, flush=True)


def left() -> float:
    return BUDGET - (time.time() - T0)


def slug(s: str, n: int = 60) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return (s[:n].strip("-") or "item")


def run(cmd, timeout=900, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def ffprobe(path: str) -> dict:
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path], 120)
    d = json.loads(r.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        return {}
    try:
        num, den = (v.get("avg_frame_rate") or "0/1").split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except Exception:  # noqa: BLE001
        fps = 0.0
    return {"w": int(v.get("width", 0)), "h": int(v.get("height", 0)), "vcodec": v.get("codec_name"),
            "pix_fmt": v.get("pix_fmt"), "fps": fps, "has_audio": bool(a),
            "duration": float(d.get("format", {}).get("duration") or v.get("duration") or 0),
            "size": int(d.get("format", {}).get("size") or 0),
            "bit_rate": int(d.get("format", {}).get("bit_rate") or 0)}


# ------------------------------------------------------------------ sources
def is_yt(u: str) -> bool:
    return "youtube.com/watch" in u


def norm_video_url(u: str) -> str | None:
    if not u:
        return None
    p = urlparse(u)
    host = re.sub(r"^(www\.|m\.)", "", p.netloc.lower())
    if host in ("youtube.com", "youtube-nocookie.com", "youtu.be"):
        if host == "youtu.be":
            vid = p.path.strip("/").split("/")[0]
        elif p.path.startswith(("/embed/", "/shorts/", "/live/", "/v/")):
            parts = p.path.split("/")
            vid = parts[2] if len(parts) > 2 else None
        else:
            vid = (parse_qs(p.query).get("v") or [None])[0]
        return f"https://www.youtube.com/watch?v={vid}" if vid and re.fullmatch(r"[\w-]{11}", vid) else None
    if host in ("x.com", "twitter.com", "mobile.twitter.com"):
        m = re.search(r"/status/(\d+)", p.path)
        return f"https://x.com/i/status/{m.group(1)}" if m else None
    if p.scheme in ("http", "https"):
        return u
    return None


def story_sources(item: dict) -> list[str]:
    raw = [y.get("url") for y in item.get("youtube_videos") or []] + list(item.get("video_urls") or [])
    out = []
    for u in raw:
        n = norm_video_url(u)
        if n and n not in out:
            out.append(n)
    return out[:4]


def ydl_base() -> dict:
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "http_headers": {"User-Agent": UA},
         "noprogress": True}
    if COOKIES and Path(COOKIES).exists() and Path(COOKIES).stat().st_size > 0:
        o["cookiefile"] = COOKIES
    return o


def pick_window(info: dict) -> tuple[float, str]:
    """Start second of the 2:20 window for long videos: heatmap peak > key chapter > 0:00."""
    dur = info.get("duration") or 0
    if dur <= MAX_SEC + 0.5:
        return 0.0, "full"
    hm = [h for h in (info.get("heatmap") or []) if h.get("end_time") is not None]
    if len(hm) >= 10:
        # skip the opening 3% (it's "most replayed" on nearly every video) and find the peak
        body = [h for h in hm if h["start_time"] >= dur * 0.03] or hm
        peak = max(body, key=lambda h: h.get("value") or 0)
        if (peak.get("value") or 0) > 0:
            mid = (peak["start_time"] + peak["end_time"]) / 2
            start = min(max(0.0, mid - 40), max(0.0, dur - MAX_SEC))   # peak ~40 s into the clip
            return round(start, 1), f"heatmap peak at {mid:.0f}s"
    for ch in info.get("chapters") or []:
        if KEY_CHAPTER.search(ch.get("title") or "") and ch.get("start_time", 0) > 0:
            start = min(float(ch["start_time"]), max(0.0, dur - MAX_SEC))
            return round(start, 1), f"chapter '{ch.get('title')[:60]}'"
    return 0.0, "from 0:00"


def probe_source(url: str) -> dict | None:
    """Best available short side for a source, without downloading."""
    try:
        with yt_dlp.YoutubeDL({**ydl_base(), "socket_timeout": 20, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        log("  probe fail", url, str(e).splitlines()[0][:160])
        return None
    if info.get("_type") == "playlist":
        ents = [e for e in (info.get("entries") or []) if e]
        if not ents:
            return None
        info = ents[0]
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        return None
    best, best_size = 0, 0
    for f in info.get("formats") or [info]:
        if f.get("vcodec") == "none":
            continue
        w, h = f.get("width") or 0, f.get("height") or 0
        side = min(w, h) if w and h else h
        if side >= best:
            best, best_size = side, f.get("filesize") or f.get("filesize_approx") or best_size
    if not best and re.search(r"\.(mp4|m3u8|mov|webm)(\?|$)", url):
        meta = ffprobe(url)
        best = min(meta.get("w", 0), meta.get("h", 0))
    start, why = pick_window(info)
    return {"url": url, "short_side": best, "duration": info.get("duration"), "est_bytes": best_size,
            "webpage_url": info.get("webpage_url") or url, "title": info.get("title"),
            "trim_start": start, "trim_reason": why}


FORMATS = ["bv*[dynamic_range=SDR]+ba/bv*+ba/b[dynamic_range=SDR]/b",
           # fallback: plain H.264 at 1080p+ (simpler/faster to fetch; still never under 1080p)
           "bv*[vcodec^=avc1][height>=1080]+ba[ext=m4a]/bv*[height>=1080][ext=mp4]+ba/b[height>=1080]"]


def download(src: dict, base: Path) -> Path | None:
    """yt-dlp in a subprocess so a slow download can be cut off at the time budget."""
    long = (src.get("duration") or 0) > MAX_SEC + 0.5
    section = long and (src.get("est_bytes") or 10**12) > MAX_FULL_DL
    src["section_download"] = section
    for attempt, fmt in enumerate(FORMATS):
        tmo = min(left() - 20, 240 if MODE == "box" else 480)
        if tmo < 20:
            log("  no time left for download")
            return None
        for p in base.parent.glob(base.name + ".*"):
            p.unlink(missing_ok=True)
        cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-progress", "--no-playlist",
               "-f", fmt, "-S", "res,fps,vbr,abr", "--merge-output-format", "mkv", "-R", "3",
               "--fragment-retries", "5", "-N", "4", "--socket-timeout", "30", "--user-agent", UA,
               "-o", str(base) + ".%(ext)s"]
        if section:
            s0 = src["trim_start"]
            cmd += ["--download-sections", f"*{s0:.1f}-{s0 + MAX_SEC:.1f}"]
        if COOKIES and Path(COOKIES).exists():
            cmd += ["--cookies", COOKIES]
        try:
            r = run(cmd + [src["url"]], tmo)
            err = r.stderr.strip().splitlines()[-1][:200] if r.returncode and r.stderr.strip() else ""
        except subprocess.TimeoutExpired:
            err = f"timed out after {tmo:.0f}s"
        files = [p for p in base.parent.glob(base.name + ".*") if p.suffix in (".mkv", ".mp4", ".webm", ".mov")]
        if not err and files:
            return max(files, key=lambda p: p.stat().st_size)
        log(f"  download attempt {attempt + 1} failed: {err or 'no file'}")
    return None


# ------------------------------------------------------------------ encode
def _seek(src: dict) -> list[str]:
    s = 0 if src.get("section_download") else src.get("trim_start") or 0
    return ["-ss", f"{s:.2f}"] if s else []


def enc_1080(raw: Path, meta: dict, out: Path, src: dict) -> bool:
    w, h = meta["w"], meta["h"]
    vf = []
    if min(w, h) > 1080:
        vf.append("scale=-2:1080:flags=lanczos" if w >= h else "scale=1080:-2:flags=lanczos")
    if meta["fps"] > 60.5:
        vf.append("fps=60")
    vf.append("format=yuv420p")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *_seek(src), "-i", str(raw), "-t", str(MAX_SEC),
           "-map", "0:v:0", "-map", "0:a:0?", "-vf", ",".join(vf),
           "-c:v", "libx264", "-preset", "faster", "-profile:v", "high",
           "-b:v", "10M", "-maxrate", "14M", "-bufsize", "20M",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", str(out)]
    r = run(cmd, 1200)
    if r.returncode:
        log("  1080 encode fail", r.stderr[-300:])
    return r.returncode == 0 and out.exists()


def enc_4k(raw: Path, meta: dict, out: Path, src: dict) -> bool:
    w, h = meta["w"], meta["h"]
    copy = meta["vcodec"] == "h264" and meta["pix_fmt"] == "yuv420p" and meta["fps"] <= 60.5 and min(w, h) <= 2160
    vf = []
    if min(w, h) > 2160:
        vf.append("scale=-2:2160:flags=lanczos" if w >= h else "scale=2160:-2:flags=lanczos")
    if meta["fps"] > 60.5:
        vf.append("fps=60")
    vf.append("format=yuv420p")
    vcodec = ["-c:v", "copy"] if copy else [
        "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-crf", "18", "-maxrate", "40M", "-bufsize", "80M"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *_seek(src), "-i", str(raw), "-t", str(MAX_SEC),
           "-map", "0:v:0", "-map", "0:a:0?", *vcodec,
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", str(out)]
    r = run(cmd, 1500)
    if r.returncode:
        log("  4K encode fail", r.stderr[-300:])
    return r.returncode == 0 and out.exists() and out.stat().st_size <= MAX_BYTES


def frames(video: Path, dur: float, stem: str) -> list[Path]:
    outs = []
    for i, t in enumerate((min(0.5, dur * 0.05), dur / 2, max(0.0, dur - 0.7)), 1):
        fp = WORK / f"{stem}-f{i}.jpg"
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
             "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5", str(fp)], 60)
        if fp.exists():
            outs.append(fp)
    return outs


# ------------------------------------------------------------------ photos
def bigger_variants(u: str) -> list[str]:
    """Try a larger rendition first for common CDNs (BBC ichef, WordPress -WxH, ?w= params)."""
    out = []
    if "ichef.bbci.co.uk" in u:
        out.append(re.sub(r"/(standard|branded_news|news)/\d+/", r"/\1/2048/", u))
    m = re.sub(r"-\d{2,4}x\d{2,4}(\.(?:jpe?g|png|webp))", r"\1", u)
    if m != u:
        out.append(m)
    q = re.sub(r"([?&](?:w|width|resize|imwidth)=)\d{2,4}", r"\g<1>2048", u)
    if q != u and "&s=" not in u and "?s=" not in u:
        q = re.sub(r"&(?:h|height)=\d{2,4}", "", q)
        out.append(re.sub(r"\?(?:h|height)=\d{2,4}&", "?", q))
    return [x for x in dict.fromkeys(out) if x != u] + [u]


def fetch_image(u: str):
    try:
        r = requests.get(u, headers={"User-Agent": UA}, timeout=10, stream=True)
        r.raise_for_status()
        data = r.raw.read(15_000_000, decode_content=True)
        return data, Image.open(io.BytesIO(data))
    except Exception:  # noqa: BLE001
        return None


def photos_for(item: dict, stem: str) -> list[dict]:
    got, seen, tries = [], set(), 0
    for u0 in (item.get("image_urls") or [])[:6]:
        if len(got) >= 3 or left() < 120 or tries >= 12:
            break
        for u in bigger_variants(u0):      # first variant that is big enough wins
            tries += 1
            res = fetch_image(u)
            if not res:
                continue
            data, im = res
            w, h = im.size
            if min(w, h) < 1080 or not (0.33 <= w / h <= 3):
                continue
            if (w, h) in seen:
                break
            seen.add((w, h))
            fp = WORK / f"{stem}-p{len(got) + 1}.jpg"
            if im.format == "JPEG":
                fp.write_bytes(data)
            else:
                im.convert("RGB").save(fp, "JPEG", quality=93)
            got.append({"path": fp, "source_url": u, "width": w, "height": h})
            break
    return got


# ------------------------------------------------------------------ publish
CTYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def gh_api(method: str, url: str, body: dict | None = None, file: Path | None = None) -> tuple[int, dict]:
    """GitHub API call with GH_TOKEN (CI) or the box's logged-in gh CLI."""
    tok = os.environ.get("GH_TOKEN")
    hdr = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if file is not None:
        hdr["Content-Type"] = CTYPES.get(file.suffix.lower(), "application/octet-stream")
    if tok:
        hdr["Authorization"] = f"Bearer {tok}"
        full = url if url.startswith("https://") else "https://api.github.com/" + url.lstrip("/")
        if file is not None:
            with open(file, "rb") as fh:
                r = requests.request(method, full, data=fh, headers={**hdr, "Content-Length": str(file.stat().st_size)},
                                     timeout=900)
        else:
            r = requests.request(method, full, json=body, headers=hdr, timeout=60)
        try:
            return r.status_code, r.json()
        except Exception:  # noqa: BLE001
            return r.status_code, {}
    cmd = ["gh", "api", "-X", method, "-i", url]
    for k, v in hdr.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd += ["--input", str(file) if file is not None else "-"]
    r = subprocess.run(cmd, input=None if file is not None else json.dumps(body or {}),
                       capture_output=True, text=True, timeout=900)
    head, _, rest = r.stdout.partition("\r\n\r\n") if "\r\n\r\n" in r.stdout else r.stdout.partition("\n\n")
    m = re.match(r"HTTP/\S+ (\d+)", head)
    try:
        return (int(m.group(1)) if m else 0), json.loads(rest or "{}")
    except Exception:  # noqa: BLE001
        return (int(m.group(1)) if m else 0), {}


class Release:
    """One GitHub Release per run. Assets get real content types (video/mp4, image/jpeg).
    Files within the Pages budget get a Pages URL (the Publish Pages workflow mirrors them)."""

    def __init__(self, tag: str, title: str):
        self.tag, self.ok, self.id, self.pages_bytes = tag, False, None, 0
        self.on_pages: set[str] = set()
        self.lock = threading.Lock()
        if DRY:
            self.ok = True
            return
        code, js = gh_api("POST", f"repos/{REPO}/releases", {
            "tag_name": tag, "name": title, "make_latest": "false",
            "body": "Auto-generated by x-news-collector media_prep.py. Deleted after a few days."})
        self.ok = code == 201
        if self.ok:
            self.id = js["id"]
        else:
            log("release create failed:", code, str(js)[:300])

    def upload(self, paths: list[Path], pages: bool = True) -> bool:
        for p in paths:
            if not DRY:
                ok = False
                for _ in range(3):
                    code, js = gh_api("POST", f"https://uploads.github.com/repos/{REPO}/releases/{self.id}/assets"
                                      f"?name={p.name}", file=p)
                    ok = code == 201
                    if ok:
                        break
                    log("  upload", p.name, code, str(js)[:200])
                    time.sleep(3)
                if not ok:
                    return False
            with self.lock:
                if pages and self.pages_bytes + p.stat().st_size <= PAGES_MAX:
                    self.pages_bytes += p.stat().st_size
                    self.on_pages.add(p.name)
        return True

    def release_url(self, p: Path) -> str:
        return f"https://github.com/{REPO}/releases/download/{self.tag}/{p.name}"

    def url(self, p: Path) -> str:
        return f"{PAGES_BASE}/{p.name}" if p.name in self.on_pages else self.release_url(p)


def cleanup_old() -> int:
    if DRY:
        return 0
    r = run(["gh", "release", "list", "--repo", REPO, "--limit", "300", "--json", "tagName,createdAt"], 120)
    n = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    for rel in json.loads(r.stdout or "[]"):
        if rel["tagName"].startswith("m-") and \
                datetime.fromisoformat(rel["createdAt"].replace("Z", "+00:00")) < cutoff:
            d = run(["gh", "release", "delete", rel["tagName"], "--repo", REPO, "--yes", "--cleanup-tag"], 120)
            n += d.returncode == 0
    return n


def mb(p: Path) -> float:
    return round(p.stat().st_size / 1048576, 1)


# ------------------------------------------------------------------ merge
def _side(res: str | None) -> int:
    try:
        w, h = (int(x) for x in (res or "0x0").split("x"))
        return min(w, h)
    except Exception:  # noqa: BLE001
        return 0


def _score(v: dict) -> tuple:
    return (v.get("type") == "video", _side(v.get("source_resolution")), bool(v.get("video_4k_url")))


def merge_into_ready(part: dict) -> bool:
    """Merge a partial result into data/media_ready.json. False if the part is for older candidates."""
    try:
        cur = json.loads(READY.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        cur = {}
    gen = part.get("candidates_generated_at_utc") or ""
    if cur.get("candidates_generated_at_utc") != gen:
        if (cur.get("candidates_generated_at_utc") or "") > gen:
            # Older part: keep only stories that are still candidates now (their media is still valid).
            try:
                now_keys = {i.get("key") for i in json.loads((DATA / "candidates.json").read_text(encoding="utf-8"))["items"]}
            except Exception:  # noqa: BLE001
                now_keys = set()
            part = {**part, "items": {k: v for k, v in part.get("items", {}).items() if k in now_keys},
                    "skipped": [], "candidates_generated_at_utc": cur["candidates_generated_at_utc"],
                    "candidates_generated_at_ist": cur.get("candidates_generated_at_ist")}
            log(f"part is for older candidates ({gen}); merging {len(part['items'])} stories still in candidates")
            if not part["items"]:
                return False
            gen = cur["candidates_generated_at_utc"]
        else:
            cur = {"items": {}, "skipped": [], "parts": []}
    items = cur.get("items", {})
    for k, v in part.get("items", {}).items():
        if k not in items or _score(v) >= _score(items[k]):
            items[k] = v
    sk = {s["key"]: s for s in cur.get("skipped", [])}
    for s in part.get("skipped", []):
        if s["key"] not in items or items[s["key"]].get("type") != "video":
            sk[s["key"]] = s
    parts = [p for p in cur.get("parts", []) if p.get("by") != part["by"]] + [
        {"by": part["by"], "release_tag": part["release_tag"], "runtime_sec": part["runtime_sec"],
         "generated_at_ist": part["generated_at_ist"]}]
    now = datetime.now(timezone.utc)
    out = {
        "generated_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_ist": now.astimezone(IST).strftime("%Y-%m-%d %H:%M IST"),
        "candidates_generated_at_utc": gen,
        "candidates_generated_at_ist": part.get("candidates_generated_at_ist"),
        "release_tag": next((p["release_tag"] for p in parts if p["by"] == "ci"), part["release_tag"]),
        "parts": parts,
        "stats": {"videos": sum(1 for v in items.values() if v["type"] == "video"),
                  "videos_4k": sum(1 for v in items.values() if v.get("video_4k_url")),
                  "photo_stories": sum(1 for v in items.values() if v["type"] == "photo"),
                  "skipped": 0},
        "items": items,
        "skipped": [s for k, s in sk.items() if not (k in items and items[k].get("type") == "video")],
    }
    out["stats"]["skipped"] = len(out["skipped"])
    READY.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return True


# ------------------------------------------------------------------ main
def process_video(it: dict, idx: int, sources: list[str], rel: Release, prefix: str, keep_4k: list):
    """Returns (entry, None) or (None, skip_reason_dict)."""
    key = it.get("key") or it.get("id")
    probes = [p for p in (probe_source(u) for u in sources) if p]
    if not probes:
        return None, {"key": key, "reason": "no source could be read", "source_url": sources[0]}
    best = max(probes, key=lambda p: p["short_side"])  # ties keep the first (official) source
    if best["short_side"] < 1080:
        return None, {"key": key, "reason": f"best source only {best['short_side']}p", "source_url": best["url"]}
    stem = f"{prefix}{idx:02d}-{slug(key, 50)}"
    raw = download(best, WORK / f"raw-{prefix}{idx:02d}")
    meta = ffprobe(str(raw)) if raw else {}
    if not meta or min(meta["w"], meta["h"]) < 1080:
        return None, {"key": key, "reason": "download failed or under 1080p" + (f" ({meta['w']}x{meta['h']})" if meta else ""),
                      "source_url": best["url"]}
    v1080 = WORK / f"{stem}-1080.mp4"
    if not enc_1080(raw, meta, v1080, best):
        return None, {"key": key, "reason": "1080p encode failed", "source_url": best["url"]}
    m1080 = ffprobe(str(v1080))
    fr = frames(v1080, m1080["duration"], stem)
    if not rel.upload([v1080, *fr]):
        return None, {"key": key, "reason": "upload failed", "source_url": best["url"]}
    long = (best.get("duration") or 0) > MAX_SEC + 0.5
    entry = {
        "type": "video", "headline": it.get("headline"), "category": it.get("category"),
        "prepared_by": MODE, "release_tag": rel.tag,
        "source_url": best["webpage_url"], "source_title": best.get("title"),
        "source_resolution": f"{meta['w']}x{meta['h']}", "source_fps": round(meta["fps"], 2),
        "source_duration": best.get("duration"),
        "duration": round(m1080["duration"], 2),
        "trimmed_to_140s": long, "trim_start_sec": best["trim_start"] if long else 0,
        "trim_reason": best["trim_reason"] if long else "full",
        "resolution": f"{meta['w']}x{meta['h']}" if min(meta["w"], meta["h"]) >= 2160 else f"{m1080['w']}x{m1080['h']}",
        "video_4k_url": None, "video_4k_resolution": None, "video_4k_size_mb": None,
        "video_1080_url": rel.url(v1080), "video_1080_backup_url": rel.release_url(v1080),
        "video_1080_resolution": f"{m1080['w']}x{m1080['h']}",
        "video_1080_size_mb": mb(v1080), "video_1080_bitrate_mbps": round(m1080["bit_rate"] / 1e6, 1),
        "frame_urls": [rel.url(f) for f in fr],
    }
    log(f"  ok 1080 {m1080['w']}x{m1080['h']} {mb(v1080)}MB src {meta['w']}x{meta['h']} {meta['vcodec']} ({entry['trim_reason']})")
    if min(meta["w"], meta["h"]) >= 2160:
        keep_4k.append((key, raw, meta, stem, best))
    else:
        raw.unlink(missing_ok=True)
    return entry, None


def main() -> int:
    if MODE == "merge":
        return 0 if merge_into_ready(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))) else 3
    box = MODE == "box"
    cands = json.loads((DATA / "candidates.json").read_text(encoding="utf-8"))
    items = cands.get("items", [])
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    tag = "m-" + now.strftime("%Y%m%d-%H%M") + ("-yt" if box else "")
    try:
        prev = json.loads(READY.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        prev = {}
    same_gen = prev.get("candidates_generated_at_utc") == cands.get("generated_at_utc")

    video_items = [it for it in items if story_sources(it)][:MAX_VIDEOS]
    todo, skipped = [], []
    for idx, it in enumerate(video_items, 1):
        key = it.get("key") or it.get("id")
        srcs = story_sources(it)
        yt = [s for s in srcs if is_yt(s)]
        mine = yt if box else ([s for s in srcs if not is_yt(s)] if SKIP_YT else srcs)
        if box and same_gen and prev.get("items", {}).get(key, {}).get("prepared_by") == "box":
            continue   # already done for these candidates
        if mine:
            todo.append((idx, it, mine))
        elif SKIP_YT and yt and not box:
            skipped.append({"key": key, "reason": "YouTube source: prepared on the box", "source_url": yt[0]})
    if box and not todo:
        log("box: no YouTube items to prepare")
        return 0

    rel = Release(tag, f"Media {now.astimezone(IST):%Y-%m-%d %H:%M} IST" + (" (box/YouTube)" if box else ""))
    if not rel.ok:
        return 1
    if not box:
        log(f"removed {cleanup_old()} old releases")
    log(f"release {tag}; {len(todo)} video stories to try")

    ready: dict[str, dict] = {}
    keep_4k: list = []

    def one(job):
        idx, it, srcs = job
        key = it.get("key") or it.get("id")
        if left() < (40 if box else 240):
            return key, None, {"key": key, "reason": "time budget", "source_url": srcs[0]}
        log(f"#{idx} {key}")
        try:
            entry, skip = process_video(it, idx, srcs, rel, "yt" if box else "", keep_4k)
        except Exception as e:  # noqa: BLE001
            entry, skip = None, {"key": key, "reason": f"error: {str(e)[:150]}", "source_url": srcs[0]}
        return key, entry, skip

    # The box downloads 3 at a time (YouTube throttles each connection); CI 2.
    with ThreadPoolExecutor(max_workers=int(os.environ.get("MEDIA_WORKERS", "3" if box else "2"))) as ex:
        for key, entry, skip in ex.map(one, todo):
            if entry:
                ready[key] = entry
            else:
                skipped.append(skip)
    order = {(j[1].get("key") or j[1].get("id")): j[0] for j in todo}
    keep_4k.sort(key=lambda k: order.get(k[0], 99))

    photo_n = 0
    if not box:
        for it in items:
            key = it.get("key") or it.get("id")
            if key in ready or photo_n >= MAX_PHOTO_STORIES or left() < 180:
                continue
            ph = photos_for(it, f"ph-{slug(key, 50)}")
            if ph and rel.upload([p["path"] for p in ph]):
                photo_n += 1
                ready[key] = {"type": "photo", "headline": it.get("headline"), "category": it.get("category"),
                              "prepared_by": MODE, "release_tag": rel.tag,
                              "photo_urls": [rel.url(p["path"]) for p in ph],
                              "photos": [{"url": rel.url(p["path"]), "source_url": p["source_url"],
                                          "width": p["width"], "height": p["height"]} for p in ph]}
        log(f"photos: {photo_n} stories")

    # 4K pass last (slowest), only with time left
    fps_4k = 40 if box else 12
    done_4k = 0
    for key, raw, meta, stem, src in keep_4k:
        est = (min(meta["duration"], MAX_SEC) * max(meta["fps"], 24)) / fps_4k + 15
        if done_4k >= MAX_4K or left() < est + 10:
            log(f"skip 4K for {key} (budget)"); raw.unlink(missing_ok=True); continue
        v4k = WORK / f"{stem}-2160.mp4"
        if enc_4k(raw, meta, v4k, src) and rel.upload([v4k], pages=False):
            m4 = ffprobe(str(v4k))
            ready[key].update(video_4k_url=rel.release_url(v4k), video_4k_resolution=f"{m4['w']}x{m4['h']}",
                              video_4k_size_mb=mb(v4k))
            done_4k += 1
            log(f"  ok 4K {key} {mb(v4k)}MB")
        raw.unlink(missing_ok=True)

    part = {
        "by": MODE, "release_tag": tag,
        "generated_at_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "candidates_generated_at_utc": cands.get("generated_at_utc"),
        "candidates_generated_at_ist": cands.get("generated_at_ist"),
        "runtime_sec": round(time.time() - T0), "items": ready, "skipped": skipped,
    }
    out = Path(os.environ.get("MEDIA_PART", str(WORK / "part.json")))
    out.write_text(json.dumps(part, ensure_ascii=False, indent=1), encoding="utf-8")
    log("done", json.dumps({"videos": sum(v["type"] == "video" for v in ready.values()), "videos_4k": done_4k,
                            "photo_stories": photo_n, "skipped": len(skipped)}), "->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
