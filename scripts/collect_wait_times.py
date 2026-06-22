#!/usr/bin/env python3
"""Collect ThemeParks.wiki live wait-time snapshots.

The script writes append-only CSV files partitioned by UTC date. It also keeps
a small entity catalog so downstream analysis can join rides to parks.
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
WAIT_TIME_ENTITY_TYPES = {"ATTRACTION"}

WAIT_TIME_FIELDS = [
    "captured_at",
    "destination_id",
    "destination_name",
    "destination_timezone",
    "park_id",
    "park_name",
    "entity_id",
    "entity_name",
    "entity_type",
    "status",
    "source_last_updated",
    "standby_wait_minutes",
    "single_rider_wait_minutes",
    "paid_standby_wait_minutes",
    "return_time_state",
    "return_time_start",
    "return_time_end",
    "paid_return_time_state",
    "paid_return_time_start",
    "paid_return_time_end",
    "paid_return_price_amount",
    "paid_return_price_currency",
    "boarding_group_state",
    "boarding_group_current_start",
    "boarding_group_current_end",
    "boarding_group_next_allocation_time",
    "boarding_group_estimated_wait_minutes",
]

# Verbatim API queue payload. It duplicates the parsed columns above, so it is
# only written when --include-raw-queue is passed (e.g. for debugging).
RAW_QUEUE_FIELD = "raw_queue_json"

CATALOG_FIELDS = [
    "cataloged_at",
    "destination_id",
    "destination_name",
    "destination_timezone",
    "entity_id",
    "entity_name",
    "entity_type",
    "parent_id",
    "parent_name",
    "parent_type",
    "latitude",
    "longitude",
]


@dataclass(frozen=True)
class EntityMeta:
    entity_id: str
    name: str
    entity_type: str
    parent_id: str
    latitude: str
    longitude: str


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


def existing_catalog_ids(path: Path) -> set[str]:
    """Entity IDs already recorded in today's catalog file, if any."""
    if not path.exists():
        return set()
    ids: set[str] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            entity_id = row.get("entity_id")
            if entity_id:
                ids.add(entity_id)
    return ids


def write_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n")


def csv_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def nested(source: dict[str, Any], *keys: str) -> Any:
    current: Any = source
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


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


def split_ids(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


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


def build_catalog(destination: dict[str, Any], captured_at: str) -> tuple[dict[str, EntityMeta], list[dict[str, str]]]:
    destination_id = str(destination["id"])
    children_response = request_json(f"/entity/{urllib.parse.quote(destination_id)}/children")
    children = children_response.get("children", [])
    destination_timezone = str(children_response.get("timezone", ""))

    meta: dict[str, EntityMeta] = {}
    rows: list[dict[str, str]] = []

    destination_meta = EntityMeta(
        entity_id=destination_id,
        name=str(destination.get("name", "")),
        entity_type="DESTINATION",
        parent_id="",
        latitude="",
        longitude="",
    )
    meta[destination_id] = destination_meta

    for child in children:
        location = child.get("location") or {}
        entity_id = str(child.get("id", ""))
        if not entity_id:
            continue
        meta[entity_id] = EntityMeta(
            entity_id=entity_id,
            name=str(child.get("name", "")),
            entity_type=str(child.get("entityType", "")),
            parent_id=str(child.get("parentId") or ""),
            latitude=csv_value(location.get("latitude")),
            longitude=csv_value(location.get("longitude")),
        )

    for item in meta.values():
        if item.entity_type not in WAIT_TIME_ENTITY_TYPES:
            continue

        parent = meta.get(item.parent_id)
        rows.append(
            {
                "cataloged_at": captured_at,
                "destination_id": destination_id,
                "destination_name": str(destination.get("name", "")),
                "destination_timezone": destination_timezone,
                "entity_id": item.entity_id,
                "entity_name": item.name,
                "entity_type": item.entity_type,
                "parent_id": item.parent_id,
                "parent_name": parent.name if parent else "",
                "parent_type": parent.entity_type if parent else "",
                "latitude": item.latitude,
                "longitude": item.longitude,
            }
        )

    return meta, rows


def park_for(entity: dict[str, Any], meta: dict[str, EntityMeta]) -> tuple[str, str]:
    entity_id = str(entity.get("id", ""))
    current = meta.get(entity_id)

    while current:
        if current.entity_type == "PARK":
            return current.entity_id, current.name
        current = meta.get(current.parent_id)

    return "", ""


def live_rows(
    destination: dict[str, Any],
    meta: dict[str, EntityMeta],
    captured_at: str,
    include_raw_queue: bool = False,
) -> list[dict[str, Any]]:
    destination_id = str(destination["id"])
    live_response = request_json(f"/entity/{urllib.parse.quote(destination_id)}/live")
    destination_timezone = str(live_response.get("timezone", ""))
    wait_rows: list[dict[str, Any]] = []

    for entity in live_response.get("liveData", []):
        entity_type = entity.get("entityType")
        if entity_type not in WAIT_TIME_ENTITY_TYPES:
            continue

        park_id, park_name = park_for(entity, meta)
        queue = entity.get("queue") or {}
        price = nested(queue, "PAID_RETURN_TIME", "price") or {}
        boarding_group = queue.get("BOARDING_GROUP") or {}

        row: dict[str, Any] = {
            "captured_at": captured_at,
            "destination_id": destination_id,
            "destination_name": destination.get("name", ""),
            "destination_timezone": destination_timezone,
            "park_id": park_id,
            "park_name": park_name,
            "entity_id": entity.get("id", ""),
            "entity_name": entity.get("name", ""),
            "entity_type": entity_type,
            "status": entity.get("status", ""),
            "source_last_updated": entity.get("lastUpdated", ""),
            "standby_wait_minutes": csv_value(nested(queue, "STANDBY", "waitTime")),
            "single_rider_wait_minutes": csv_value(nested(queue, "SINGLE_RIDER", "waitTime")),
            "paid_standby_wait_minutes": csv_value(nested(queue, "PAID_STANDBY", "waitTime")),
            "return_time_state": csv_value(nested(queue, "RETURN_TIME", "state")),
            "return_time_start": csv_value(nested(queue, "RETURN_TIME", "returnStart")),
            "return_time_end": csv_value(nested(queue, "RETURN_TIME", "returnEnd")),
            "paid_return_time_state": csv_value(nested(queue, "PAID_RETURN_TIME", "state")),
            "paid_return_time_start": csv_value(nested(queue, "PAID_RETURN_TIME", "returnStart")),
            "paid_return_time_end": csv_value(nested(queue, "PAID_RETURN_TIME", "returnEnd")),
            "paid_return_price_amount": csv_value(price.get("amount")),
            "paid_return_price_currency": csv_value(price.get("currency")),
            "boarding_group_state": csv_value(boarding_group.get("allocationStatus")),
            "boarding_group_current_start": csv_value(boarding_group.get("currentGroupStart")),
            "boarding_group_current_end": csv_value(boarding_group.get("currentGroupEnd")),
            "boarding_group_next_allocation_time": csv_value(boarding_group.get("nextAllocationTime")),
            "boarding_group_estimated_wait_minutes": csv_value(boarding_group.get("estimatedWait")),
        }
        if include_raw_queue:
            row[RAW_QUEUE_FIELD] = json.dumps(queue, sort_keys=True, separators=(",", ":"))
        wait_rows.append(row)

    return wait_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect ThemeParks.wiki live wait-time snapshots.")
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--destination-ids",
        default="",
        help="Comma-separated destination IDs or slugs. Defaults to all destinations.",
    )
    parser.add_argument(
        "--destination-file",
        type=Path,
        default=Path("config/major_destinations.txt"),
        help="Optional newline-delimited destination IDs/slugs to collect.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch data but do not write files.")
    parser.add_argument(
        "--include-raw-queue",
        action="store_true",
        help="Also write the verbatim raw_queue_json column (duplicates parsed fields; off by default).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    captured_at = utc_now_iso()
    date_part = captured_at[:10]

    wait_path = args.output_dir / "wait_times" / f"{date_part}.csv"
    desired_wait_fields = (
        WAIT_TIME_FIELDS + [RAW_QUEUE_FIELD] if args.include_raw_queue else WAIT_TIME_FIELDS
    )
    if wait_path.exists():
        with wait_path.open(newline="", encoding="utf-8") as handle:
            try:
                existing_fields = next(csv.reader(handle))
            except StopIteration:
                existing_fields = []
        if existing_fields and existing_fields != desired_wait_fields:
            raise ValueError(
                f"{wait_path} already exists with fields {existing_fields}; rerun with matching --include-raw-queue setting"
            )

    wait_fields = desired_wait_fields
    destinations_response = request_json("/destinations")
    all_destinations = destinations_response.get("destinations", [])
    selected = destination_filter(args)
    destinations = choose_destinations(all_destinations, selected)

    wait_rows: list[dict[str, Any]] = []
    catalog_rows: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []

    for index, destination in enumerate(destinations, start=1):
        destination_name = destination.get("name", destination.get("id", "unknown"))
        print(f"[{index}/{len(destinations)}] collecting {destination_name}", flush=True)
        try:
            meta, destination_catalog_rows = build_catalog(destination, captured_at)
            catalog_rows.extend(destination_catalog_rows)
            wait_rows.extend(live_rows(destination, meta, captured_at, args.include_raw_queue))
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
                    "wait_rows": len(wait_rows),
                    "catalog_rows": len(catalog_rows),
                    "failures": failures,
                },
                indent=2,
            )
        )
        return 1 if failures else 0

    catalog_path = args.output_dir / "catalog" / f"{date_part}.csv"
    known_ids = existing_catalog_ids(catalog_path)
    new_catalog_rows: list[dict[str, str]] = []
    for row in catalog_rows:
        entity_id = row["entity_id"]
        if entity_id in known_ids:
            continue
        known_ids.add(entity_id)
        new_catalog_rows.append(row)

    append_csv(args.output_dir / "wait_times" / f"{date_part}.csv", wait_fields, wait_rows)
    append_csv(catalog_path, CATALOG_FIELDS, new_catalog_rows)
    write_jsonl(
        args.output_dir / "run_log.jsonl",
        {
            "captured_at": captured_at,
            "destinations_requested": len(destinations),
            "wait_rows": len(wait_rows),
            "catalog_rows_new": len(new_catalog_rows),
            "failures": failures,
        },
    )

    if failures:
        print(json.dumps({"failures": failures}, indent=2), file=sys.stderr)
        return 1

    print(f"wrote {len(wait_rows)} wait rows and {len(new_catalog_rows)} new catalog rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
