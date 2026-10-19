#!/usr/bin/env python3
"""media_prep.py: turn data/candidates.json into upload-ready media for the posting routine.

For the top stories with video it downloads the best original with yt-dlp (4K preferred),
makes a high-bitrate 1080p H.264/AAC file (and keeps a 4K H.264 file when the source is 4K and
the file fits Buffer/X limits), grabs 3 small check frames, and finds up to 3 >=1080px photos
for stories without video. Files are published as assets of a GitHub Release in the public
media repo, and data/media_ready.json lists their public URLs per story key.

Runs inside the chad-media "Prepare media" workflow (public repo, so no private-repo minutes).
Env: MEDIA_REPO (owner/name), GH_TOKEN (workflow GITHUB_TOKEN, used by `gh`),
     MAX_VIDEOS (12), MEDIA_BUDGET_SEC (1080), KEEP_DAYS (3), MAX_4K (4).
No secrets are stored in this file.
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
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import yt_dlp
from PIL import Image

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
WORK = Path(os.environ.get("MEDIA_WORK", "/tmp/media_work"))
REPO = os.environ.get("MEDIA_REPO", "chatgpt820970/chad-media")
MAX_VIDEOS = int(os.environ.get("MAX_VIDEOS", "12"))
BUDGET = int(os.environ.get("MEDIA_BUDGET_SEC", "1080"))
KEEP_DAYS = float(os.environ.get("KEEP_DAYS", "3"))
MAX_4K = int(os.environ.get("MAX_4K", "4"))
MAX_SEC = 140              # Buffer/X: up to 2:20
MAX_BYTES = 512 * 1024 * 1024
MAX_PHOTO_STORIES = 20
DRY = os.environ.get("DRY_RUN") == "1"   # local test: no GitHub calls
# GitHub Pages copy of this run's files (the workflow deploys PAGES_DIR; replaced every run)
PAGES_DIR = Path(os.environ["PAGES_DIR"]) / "m" if os.environ.get("PAGES_DIR") else None
PAGES_BASE = os.environ.get("PAGES_BASE", "https://chatgpt820970.github.io/chad-media") + "/m"
PAGES_MAX = int(os.environ.get("PAGES_MAX_MB", "950")) * 1048576   # Pages site limit is 1 GB
COOKIES = os.environ.get("YT_COOKIES_FILE")   # optional, written from a GitHub Secret by the workflow
IST = timezone(timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:6.0f}s]", *a, flush=True)


def left() -> float:
    return BUDGET - (time.time() - T0)


def slug(s: str, n: int = 60) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return (s[:n].strip("-") or "item")


def run(cmd, timeout=900):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


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
def norm_video_url(u: str) -> str | None:
    if not u:
        return None
    p = urlparse(u)
    host = p.netloc.lower().replace("www.", "").replace("m.", "", 1) if p.netloc else ""
    if host in ("youtube.com", "youtube-nocookie.com", "youtu.be"):
        vid = None
        if host == "youtu.be":
            vid = p.path.strip("/").split("/")[0]
        elif p.path.startswith(("/embed/", "/shorts/", "/live/", "/v/")):
            vid = p.path.split("/")[2] if len(p.path.split("/")) > 2 else None
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
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "http_headers": {"User-Agent": UA}}
    if COOKIES and Path(COOKIES).exists() and Path(COOKIES).stat().st_size > 0:
        o["cookiefile"] = COOKIES
    return o


def probe_source(url: str) -> dict | None:
    """Best available short side for a source, without downloading."""
    opts = {**ydl_base(), "socket_timeout": 20, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
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
    best = 0
    for f in info.get("formats") or [info]:
        if f.get("vcodec") == "none":
            continue
        w, h = f.get("width") or 0, f.get("height") or 0
        best = max(best, min(w, h) if w and h else h)
    if not best and re.search(r"\.(mp4|m3u8|mov|webm)(\?|$)", url):
        meta = ffprobe(url)
        best = min(meta.get("w", 0), meta.get("h", 0))
    return {"url": url, "short_side": best, "duration": info.get("duration"),
            "webpage_url": info.get("webpage_url") or url, "title": info.get("title")}


def download(src: dict, base: Path) -> Path | None:
    opts = {**ydl_base(), "socket_timeout": 30, "outtmpl": str(base) + ".%(ext)s",
            "format": "bv*[dynamic_range=SDR]+ba/bv*+ba/b[dynamic_range=SDR]/b",
            "format_sort": ["res", "fps", "vbr", "abr"], "merge_output_format": "mkv",
            "retries": 3, "concurrent_fragment_downloads": 4, "noprogress": True}
    if (src.get("duration") or 0) > MAX_SEC:
        opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(0, MAX_SEC)])
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([src["url"]])
    except Exception as e:  # noqa: BLE001
        log("  download fail", str(e).splitlines()[0][:200])
        return None
    files = [p for p in base.parent.glob(base.name + ".*") if p.suffix not in (".part", ".ytdl")]
    return max(files, key=lambda p: p.stat().st_size) if files else None


# ------------------------------------------------------------------ encode
def enc_1080(src: Path, meta: dict, out: Path) -> bool:
    w, h = meta["w"], meta["h"]
    vf = []
    if min(w, h) > 1080:
        vf.append("scale=-2:1080:flags=lanczos" if w >= h else "scale=1080:-2:flags=lanczos")
    if meta["fps"] > 60.5:
        vf.append("fps=60")
    vf.append("format=yuv420p")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-t", str(MAX_SEC),
           "-map", "0:v:0", "-map", "0:a:0?", "-vf", ",".join(vf),
           "-c:v", "libx264", "-preset", "faster", "-profile:v", "high",
           "-b:v", "10M", "-maxrate", "14M", "-bufsize", "20M",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", str(out)]
    r = run(cmd, 1200)
    if r.returncode:
        log("  1080 encode fail", r.stderr[-300:])
    return r.returncode == 0 and out.exists()


def enc_4k(src: Path, meta: dict, out: Path) -> bool:
    w, h = meta["w"], meta["h"]
    copy = meta["vcodec"] == "h264" and meta["pix_fmt"] == "yuv420p" and meta["fps"] <= 60.5
    vf = []
    if min(w, h) > 2160:
        vf.append("scale=-2:2160:flags=lanczos" if w >= h else "scale=2160:-2:flags=lanczos")
    if meta["fps"] > 60.5:
        vf.append("fps=60")
    vf.append("format=yuv420p")
    vcodec = ["-c:v", "copy"] if copy and min(w, h) <= 2160 else [
        "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-crf", "18", "-maxrate", "40M", "-bufsize", "80M"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-t", str(MAX_SEC),
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
        im = Image.open(io.BytesIO(data))
        return data, im
    except Exception:  # noqa: BLE001
        return None


def photos_for(item: dict, stem: str) -> list[dict]:
    got, seen = [], set()
    tries = 0
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
API = "https://api.github.com"


def gh_headers(extra=None):
    h = {"Authorization": f"Bearer {os.environ.get('GH_TOKEN', '')}", "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    h.update(extra or {})
    return h


class Release:
    """One GitHub Release per run. Assets get real content types (video/mp4, image/jpeg).
    Files small enough also go to the Pages site dir (direct 200 + video/mp4, no redirect)."""

    def __init__(self, tag: str, title: str):
        self.tag, self.ok, self.id, self.pages_bytes = tag, False, None, 0
        if DRY:
            self.ok = True
            return
        r = requests.post(f"{API}/repos/{REPO}/releases", headers=gh_headers(), timeout=60, json={
            "tag_name": tag, "name": title, "make_latest": "false",
            "body": "Auto-generated by x-news-collector media_prep.py. Deleted after a few days."})
        self.ok = r.status_code == 201
        if self.ok:
            self.id = r.json()["id"]
        else:
            log("release create failed:", r.status_code, r.text[:300])

    def upload(self, paths: list[Path], pages: bool = True) -> bool:
        for p in paths:
            if pages and PAGES_DIR and self.pages_bytes + p.stat().st_size <= PAGES_MAX:
                PAGES_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, PAGES_DIR / p.name)
                self.pages_bytes += p.stat().st_size
            if DRY:
                continue
            ok = False
            for attempt in range(3):
                try:
                    with open(p, "rb") as fh:
                        r = requests.post(f"https://uploads.github.com/repos/{REPO}/releases/{self.id}/assets",
                                          params={"name": p.name}, data=fh, timeout=900, headers=gh_headers(
                                              {"Content-Type": CTYPES.get(p.suffix.lower(), "application/octet-stream"),
                                               "Content-Length": str(p.stat().st_size)}))
                    ok = r.status_code == 201
                    if ok:
                        break
                    log("  upload", p.name, r.status_code, r.text[:200])
                except Exception as e:  # noqa: BLE001
                    log("  upload error", p.name, e)
                time.sleep(3)
            if not ok:
                return False
        return True

    def release_url(self, p: Path) -> str:
        return f"https://github.com/{REPO}/releases/download/{self.tag}/{p.name}"

    def url(self, p: Path) -> str:
        if PAGES_DIR and (PAGES_DIR / p.name).exists():
            return f"{PAGES_BASE}/{p.name}"
        return self.release_url(p)


def cleanup_old() -> int:
    if DRY:
        return 0
    r = run(["gh", "release", "list", "--repo", REPO, "--limit", "300", "--json", "tagName,createdAt"], 120)
    n = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    for rel in json.loads(r.stdout or "[]"):
        if not rel["tagName"].startswith("m-"):
            continue
        if datetime.fromisoformat(rel["createdAt"].replace("Z", "+00:00")) < cutoff:
            d = run(["gh", "release", "delete", rel["tagName"], "--repo", REPO, "--yes", "--cleanup-tag"], 120)
            n += d.returncode == 0
    return n


def mb(p: Path) -> float:
    return round(p.stat().st_size / 1048576, 1)


# ------------------------------------------------------------------ main
def main() -> int:
    cands = json.loads((DATA / "candidates.json").read_text(encoding="utf-8"))
    items = cands.get("items", [])
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    tag = "m-" + now.strftime("%Y%m%d-%H%M")
    rel = Release(tag, f"Media {now.astimezone(IST):%Y-%m-%d %H:%M} IST")
    if not rel.ok:
        return 1
    removed = cleanup_old()
    log(f"release {tag}; removed {removed} old releases")

    ready: dict[str, dict] = {}
    skipped: list[dict] = []
    keep_4k: list[tuple[str, Path, dict, str]] = []
    video_items = [it for it in items if story_sources(it)][:MAX_VIDEOS]
    for idx, it in enumerate(video_items, 1):
        key = it.get("key") or it.get("id")
        if left() < 240:
            skipped.append({"key": key, "reason": "time budget"}); continue
        log(f"#{idx} {key}")
        probes = [p for p in (probe_source(u) for u in story_sources(it)) if p]
        if not probes:
            skipped.append({"key": key, "reason": "no source could be read"}); continue
        best = max(probes, key=lambda p: p["short_side"])  # ties keep the first (official) source
        if best["short_side"] < 1080:
            skipped.append({"key": key, "reason": f"best source only {best['short_side']}p", "source_url": best["url"]})
            continue
        stem = f"{idx:02d}-{slug(key, 50)}"
        raw = download(best, WORK / f"raw-{idx:02d}")
        meta = ffprobe(str(raw)) if raw else {}
        if not meta or min(meta["w"], meta["h"]) < 1080:
            skipped.append({"key": key, "reason": "download failed or under 1080p" + (f" ({meta['w']}x{meta['h']})" if meta else ""),
                            "source_url": best["url"]})
            continue
        dur = min(meta["duration"] or best.get("duration") or 0, MAX_SEC)
        v1080 = WORK / f"{stem}-1080.mp4"
        if not enc_1080(raw, meta, v1080):
            skipped.append({"key": key, "reason": "1080p encode failed", "source_url": best["url"]}); continue
        m1080 = ffprobe(str(v1080))
        fr = frames(v1080, m1080["duration"] or dur, stem)
        if not rel.upload([v1080, *fr]):
            skipped.append({"key": key, "reason": "upload failed"}); continue
        ready[key] = {
            "type": "video", "headline": it.get("headline"), "category": it.get("category"),
            "source_url": best["webpage_url"], "source_title": best.get("title"),
            "source_resolution": f"{meta['w']}x{meta['h']}", "source_fps": round(meta["fps"], 2),
            "duration": round(m1080["duration"], 2),
            "trimmed_to_140s": (best.get("duration") or meta["duration"]) > MAX_SEC + 0.5,
            "resolution": f"{meta['w']}x{meta['h']}" if min(meta["w"], meta["h"]) >= 2160 else f"{m1080['w']}x{m1080['h']}",
            "video_4k_url": None, "video_4k_resolution": None, "video_4k_size_mb": None,
            "video_1080_url": rel.url(v1080), "video_1080_backup_url": rel.release_url(v1080),
            "video_1080_resolution": f"{m1080['w']}x{m1080['h']}",
            "video_1080_size_mb": mb(v1080), "video_1080_bitrate_mbps": round(m1080["bit_rate"] / 1e6, 1),
            "frame_urls": [rel.url(f) for f in fr],
        }
        log(f"  ok 1080 {m1080['w']}x{m1080['h']} {mb(v1080)}MB src {meta['w']}x{meta['h']} {meta['vcodec']}")
        if min(meta["w"], meta["h"]) >= 2160:
            keep_4k.append((key, raw, meta, stem))
        else:
            raw.unlink(missing_ok=True)

    # photos for stories without a ready video
    photo_n = 0
    for it in items:
        key = it.get("key") or it.get("id")
        if key in ready or photo_n >= MAX_PHOTO_STORIES or left() < 180:
            continue
        ph = photos_for(it, f"ph-{slug(key, 50)}")
        if ph and rel.upload([p["path"] for p in ph]):
            photo_n += 1
            ready[key] = {"type": "photo", "headline": it.get("headline"), "category": it.get("category"),
                          "photo_urls": [rel.url(p["path"]) for p in ph],
                          "photos": [{"url": rel.url(p["path"]), "source_url": p["source_url"],
                                      "width": p["width"], "height": p["height"]} for p in ph]}
    log(f"photos: {photo_n} stories")

    # 4K pass last (slowest), only with time left
    done_4k = 0
    for key, raw, meta, stem in keep_4k:
        est = (min(meta["duration"], MAX_SEC) * max(meta["fps"], 24)) / 12 + 30  # ~12 fps x264 4K
        if done_4k >= MAX_4K or left() < est + 60:
            log(f"skip 4K for {key} (budget)"); continue
        v4k = WORK / f"{stem}-2160.mp4"
        if enc_4k(raw, meta, v4k) and rel.upload([v4k], pages=False):
            m4 = ffprobe(str(v4k))
            ready[key].update(video_4k_url=rel.url(v4k), video_4k_resolution=f"{m4['w']}x{m4['h']}",
                              video_4k_size_mb=mb(v4k))
            done_4k += 1
            log(f"  ok 4K {key} {mb(v4k)}MB")
        raw.unlink(missing_ok=True)

    out = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
        "candidates_generated_at_ist": cands.get("generated_at_ist"),
        "release_tag": tag, "release_url": f"https://github.com/{REPO}/releases/tag/{tag}",
        "runtime_sec": round(time.time() - T0),
        "stats": {"videos": sum(1 for v in ready.values() if v["type"] == "video"),
                  "videos_4k": done_4k, "photo_stories": photo_n, "skipped": len(skipped)},
        "items": ready, "skipped": skipped,
    }
    (DATA / "media_ready.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log("done", json.dumps(out["stats"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
