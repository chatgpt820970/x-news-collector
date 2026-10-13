#!/usr/bin/env python3
"""Usage: check_media.py <url-or-file> [--photo]
Probes the media with ffprobe and prints WxH plus PASS/FAIL (>=720p video, >=1080 photo short side).
For X posts, pass the highest-bitrate mp4 variant URL. Exit code 0 = PASS, 1 = FAIL, 2 = error."""
import json, subprocess, sys

def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__); return 2
    photo = "--photo" in sys.argv
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height,bit_rate",
                              "-of", "json", args[0]], capture_output=True, text=True, timeout=60)
        st = json.loads(out.stdout or "{}").get("streams", [])
    except Exception as e:  # noqa: BLE001
        print(f"ERROR {e}"); return 2
    if not st:
        print(f"ERROR no video stream: {out.stderr.strip()[:200]}"); return 2
    w, h = int(st[0]["width"]), int(st[0]["height"])
    need = 1080 if photo else 720
    ok = min(w, h) >= need
    print(f"{w}x{h} bitrate={st[0].get('bit_rate', '?')} min_side={min(w, h)} need>={need} {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
