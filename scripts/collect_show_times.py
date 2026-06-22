#!/usr/bin/env python3
"""Collect ThemeParks.wiki showtimes on demand.

Show schedules usually change much less often than ride waits, so this is kept
separate from the 15-minute wait-time collector.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


API_BASE_URL = "https://api.themeparks.wiki/v1"

SHOW_TIME_FIELDS = [
    "captured_at",
    "destination_id",
    "destination_name",
    "destination_timezone",
    "park_id",
    "park_name",
    "show_id",
    "show_name",
    "status",
    "source_last_updated",
    "showtime_type",
    "show_start_time",
    "show_end_time",
]


@dataclass(frozen=True)
class EntityMeta:
    entity_id: str
    name: str
    entity_type: str
    parent_id: str


class ApiError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def request_json(path: str, retries: int = 3, timeout: int = 30) -> dict[str, Any]:
    url = f"{API_BASE_URL}{path}"
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "WaitTimeHelper/1.0 (+https://github.com/)",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == retries:
                break
            time.sleep(2**attempt)

    raise ApiError(f"failed to fetch {url}: {last_error}")


def csv_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def split_ids(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def destination_filter(args: argparse.Namespace) -> set[str]:
    values: set[str] = set()
    if args.destination_ids:
        values.update(split_ids(args.destination_ids))

    env_value = os.environ.get("THEMEPARKS_DESTINATION_IDS", "")
    if env_value:
        values.update(split_ids(env_value))

    if args.destination_file.exists():
        for line in args.destination_file.read_text(encoding="utf-8").splitlines():
            clean = line.split("#", 1)[0].strip()
            if clean:
                values.add(clean)

    return values


def choose_destinations(all_destinations: list[dict[str, Any]], selected_ids: set[str]) -> list[dict[str, Any]]:
    if not selected_ids:
        return all_destinations

    chosen = [
        destination
        for destination in all_destinations
        if destination.get("id") in selected_ids or destination.get("slug") in selected_ids
    ]
    missing = selected_ids - {
        str(destination.get("id"))
        for destination in chosen
    } - {
        str(destination.get("slug"))
        for destination in chosen
    }
    if missing:
        raise ValueError(f"unknown destination ids/slugs: {', '.join(sorted(missing))}")
    return chosen


def append_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def build_meta(destination_id: str) -> dict[str, EntityMeta]:
    children_response = request_json(f"/entity/{urllib.parse.quote(destination_id)}/children")
    meta = {
        destination_id: EntityMeta(
            entity_id=destination_id,
            name=str(children_response.get("name", "")),
            entity_type=str(children_response.get("entityType", "DESTINATION")),
            parent_id="",
        )
    }

    for child in children_response.get("children", []):
        entity_id = str(child.get("id", ""))
        if not entity_id:
            continue
        meta[entity_id] = EntityMeta(
            entity_id=entity_id,
            name=str(child.get("name", "")),
            entity_type=str(child.get("entityType", "")),
            parent_id=str(child.get("parentId") or ""),
        )

    return meta


def park_for(entity_id: str, meta: dict[str, EntityMeta]) -> tuple[str, str]:
    current = meta.get(entity_id)
    while current:
        if current.entity_type == "PARK":
            return current.entity_id, current.name
        current = meta.get(current.parent_id)

    return "", ""


def show_rows(destination: dict[str, Any], captured_at: str) -> list[dict[str, Any]]:
    destination_id = str(destination["id"])
    meta = build_meta(destination_id)
    live_response = request_json(f"/entity/{urllib.parse.quote(destination_id)}/live")
    destination_timezone = str(live_response.get("timezone", ""))
    rows: list[dict[str, Any]] = []

    for entity in live_response.get("liveData", []):
        if entity.get("entityType") != "SHOW":
            continue

        park_id, park_name = park_for(str(entity.get("id", "")), meta)
        for showtime in entity.get("showtimes") or []:
            rows.append(
                {
                    "captured_at": captured_at,
                    "destination_id": destination_id,
                    "destination_name": destination.get("name", ""),
                    "destination_timezone": destination_timezone,
                    "park_id": park_id,
                    "park_name": park_name,
                    "show_id": entity.get("id", ""),
                    "show_name": entity.get("name", ""),
                    "status": entity.get("status", ""),
                    "source_last_updated": entity.get("lastUpdated", ""),
                    "showtime_type": showtime.get("type", ""),
                    "show_start_time": csv_value(showtime.get("startTime")),
                    "show_end_time": csv_value(showtime.get("endTime")),
                }
            )

    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect ThemeParks.wiki showtimes on demand.")
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--destination-ids",
        default="",
        help="Comma-separated destination IDs or slugs. Defaults to config file, then all destinations.",
    )
    parser.add_argument(
        "--destination-file",
        type=Path,
        default=Path("config/major_destinations.txt"),
        help="Optional newline-delimited destination IDs/slugs to collect.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch data but do not write files.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    captured_at = utc_now_iso()
    date_part = captured_at[:10]

    destinations_response = request_json("/destinations")
    selected = destination_filter(args)
    destinations = choose_destinations(destinations_response.get("destinations", []), selected)

    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for index, destination in enumerate(destinations, start=1):
        destination_name = destination.get("name", destination.get("id", "unknown"))
        print(f"[{index}/{len(destinations)}] collecting shows for {destination_name}", flush=True)
        try:
            rows.extend(show_rows(destination, captured_at))
        except Exception as exc:
            failures.append(
                {
                    "destination_id": str(destination.get("id", "")),
                    "destination_name": str(destination_name),
                    "error": str(exc),
                }
            )

    if args.dry_run:
        print(
            json.dumps(
                {
                    "captured_at": captured_at,
                    "destinations": len(destinations),
                    "show_rows": len(rows),
                    "failures": failures,
                },
                indent=2,
            )
        )
        return 1 if failures else 0

    append_csv(args.output_dir / "show_times" / f"{date_part}.csv", SHOW_TIME_FIELDS, rows)
    if failures:
        print(json.dumps({"failures": failures}, indent=2), file=sys.stderr)
        return 1

    print(f"wrote {len(rows)} show rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
