#!/usr/bin/env python3
"""Rank low-wait time windows from collected ThemeParks.wiki snapshots."""

from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find low-wait windows from collected snapshots.")
    parser.add_argument("--input-glob", default="data/wait_times/*.csv")
    parser.add_argument("--output", type=Path, default=Path("data/best_times.csv"))
    parser.add_argument("--min-samples", type=int, default=8)
    parser.add_argument("--top", type=int, default=5, help="Rows to keep per attraction.")
    return parser.parse_args()


def parse_wait(value: str) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def local_parts(captured_at: str, timezone: str) -> tuple[str, int]:
    parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    local = parsed.astimezone(ZoneInfo(timezone)) if timezone else parsed
    return local.strftime("%A"), local.hour


def main() -> int:
    args = parse_args()
    groups: dict[tuple[str, str, str, str, int], list[float]] = defaultdict(list)

    for path in sorted(Path().glob(args.input_glob)):
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, skipinitialspace=True)
            for row in reader:
                if row.get("entity_type") != "ATTRACTION" or row.get("status") != "OPERATING":
                    continue

                wait = parse_wait(row.get("standby_wait_minutes", ""))
                if wait is None:
                    continue

                day_name, hour = local_parts(row.get("captured_at", ""), row.get("destination_timezone", ""))
                key = (
                    row.get("park_name", ""),
                    row.get("entity_id", ""),
                    row.get("entity_name", ""),
                    day_name,
                    hour,
                )
                groups[key].append(wait)

    ranked_by_ride: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for (park_name, entity_id, entity_name, day_name, hour), waits in groups.items():
        if len(waits) < args.min_samples:
            continue
        ranked_by_ride[(park_name, entity_id)].append(
            {
                "park_name": park_name,
                "entity_id": entity_id,
                "entity_name": entity_name,
                "day_of_week": day_name,
                "local_hour": hour,
                "median_standby_wait_minutes": round(statistics.median(waits), 1),
                "sample_count": len(waits),
            }
        )

    output_rows: list[dict[str, object]] = []
    for rows in ranked_by_ride.values():
        output_rows.extend(
            sorted(rows, key=lambda row: (row["median_standby_wait_minutes"], -row["sample_count"]))[: args.top]
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "park_name",
        "entity_id",
        "entity_name",
        "day_of_week",
        "local_hour",
        "median_standby_wait_minutes",
        "sample_count",
    ]
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(sorted(output_rows, key=lambda row: (row["park_name"], row["entity_name"], row["median_standby_wait_minutes"])))

    print(f"wrote {len(output_rows)} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
