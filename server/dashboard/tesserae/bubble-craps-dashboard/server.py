"""Bubble Craps Dashboard — server-side data fetcher for Tesserae."""

import json
import urllib.request
from datetime import datetime
from pathlib import Path


def fetch(options, settings, *, ctx):
    """Fetch rolls, stats, and distribution from the Bubble Craps Data API.

    Args:
        options: cell_options for this cell.
        settings: plugin-level settings.
        ctx: runtime context with data_dir, panel dimensions, etc.

    Returns:
        Dictionary with rolls, stats, and distribution data — or {"error": "..."}.
    """
    api_base = options.get("api_base", "http://localhost:8000").rstrip("/")
    recent_count = int(options.get("recent_count", 10))
    roll_fetch_count = int(options.get("roll_fetch_count", 50))

    data_dir = Path(ctx["data_dir"])
    cache_file = data_dir / "cache.json"

    # Serve from cache if fresh (60 seconds)
    if cache_file.exists():
        mtime = cache_file.stat().st_mtime
        if datetime.now().timestamp() - mtime < 60:
            try:
                with open(cache_file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    headers = {"User-Agent": "tesserae/0.1 (+bubble_craps_dashboard)"}

    try:
        # Fetch rolls
        rolls_url = f"{api_base}/api/rolls?limit={roll_fetch_count}"
        req = urllib.request.Request(rolls_url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            rolls_data = json.loads(resp.read())

        # Fetch stats
        stats_url = f"{api_base}/api/rolls/stats"
        req = urllib.request.Request(stats_url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            stats_data = json.loads(resp.read())

        # Fetch distribution
        dist_url = f"{api_base}/api/rolls/distribution"
        req = urllib.request.Request(dist_url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            dist_data = json.loads(resp.read())

    except Exception:
        return {"error": "Could not reach Bubble Craps API"}

    # Compute seven-tracking stats from rolls
    rolls = rolls_data.get("rolls", [])

    rolls_since_seven = 0
    for r in rolls:
        if r.get("total") == 7:
            break
        if r.get("roll_status") == "ok":
            rolls_since_seven += 1

    gaps = []
    gap = 0
    for r in reversed(rolls):
        if r.get("roll_status") != "ok":
            continue
        if r.get("total") == 7:
            if gap > 0:
                gaps.append(gap)
            gap = 0
        else:
            gap += 1

    avg_between_sevens = (
        round(sum(gaps) / len(gaps), 1) if gaps else None
    )

    result = {
        "rolls": rolls[:recent_count],
        "stats": {
            "total_rolls": stats_data.get("total_rolls", 0),
            "successful_rolls": stats_data.get("successful_rolls", 0),
            "failed_rolls": stats_data.get("failed_rolls", 0),
            "rolls_since_seven": rolls_since_seven,
            "avg_between_sevens": avg_between_sevens,
        },
        "distribution": dist_data.get("distribution", []),
    }

    # Cache
    try:
        with open(cache_file, "w") as f:
            json.dump(result, f)
    except OSError:
        pass

    return result
