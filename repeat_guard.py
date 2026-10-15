"""Repeat guard: 7-day topic/celebrity cooldown + permanent blocklist.

CLI (run before scheduling any post):
  python repeat_guard.py check "caption or source text" [--id X_POST_ID]   -> exit 1 if blocked
  python repeat_guard.py add "topic one" "topic two" [--at ISO]            -> log topics as posted
"""
from __future__ import annotations
import json, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(__file__).parent / "data"


def _load(name, default):
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _norm(s: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip() + " "


def recent_topics(now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    d = _load("recent_topics.json", {"window_days": 7, "topics": []})
    cutoff = now - timedelta(days=d.get("window_days", 7))
    out = []
    for t in d.get("topics", []):
        try:
            if datetime.fromisoformat(t["posted_at"]) >= cutoff:
                out.append(t["topic"])
        except Exception:  # noqa: BLE001
            continue
    return out


def blocked_reason(text: str, x_ids=(), now: datetime | None = None) -> str | None:
    """Return why `text` (headline/caption/key/source) may not be posted, or None."""
    bl = _load("blocklist.json", {"phrases": [], "x_ids": []})
    ids = {str(i) for i in x_ids} | set(re.findall(r"status/(\d+)", text or ""))
    hit = ids & set(bl.get("x_ids", []))
    if hit:
        return f"blocklisted post id {hit.pop()}"
    n = _norm(text)
    for p in bl.get("phrases", []):
        if _norm(p) in n:
            return f"blocklisted phrase '{p}'"
    for t in recent_topics(now):
        if _norm(t) in n:
            return f"topic '{t}' posted in the last 7 days"
    return None


def add_topics(topics, at: str | None = None) -> None:
    d = _load("recent_topics.json", {"window_days": 7, "topics": []})
    at = at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    for t in topics:
        d["topics"].append({"topic": t.lower().strip(), "posted_at": at})
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    d["topics"] = [t for t in d["topics"] if datetime.fromisoformat(t["posted_at"]) >= cutoff]
    (DATA / "recent_topics.json").write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "check":
        ids = [a[a.index("--id") + 1]] if "--id" in a else []
        r = blocked_reason(a[1], ids)
        print("BLOCKED: " + r if r else "OK")
        sys.exit(1 if r else 0)
    if len(a) >= 2 and a[0] == "add":
        at = a[a.index("--at") + 1] if "--at" in a else None
        add_topics([x for x in a[1:] if x not in ("--at", at)], at)
        print("logged")
        sys.exit(0)
    print(__doc__)
    sys.exit(2)
