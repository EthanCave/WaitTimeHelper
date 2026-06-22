# WaitTimeHelper

Collect ThemeParks.wiki live wait-time snapshots every 15 minutes with GitHub Actions, then analyze the CSV history to find better ride timing.

## What It Collects

The collector uses the ThemeParks.wiki V1 API:

- `GET /destinations` to discover supported destinations.
- `GET /entity/{destinationId}/children` to build an attraction catalog.
- `GET /entity/{destinationId}/live` to capture live statuses, queue waits, return times, and boarding group data.

Wait-time rows and scheduled catalog rows are limited to attractions. Show entities are not collected every 15 minutes; use the separate showtime script when you want show overlays for a specific analysis.

By default, it collects every destination exposed by ThemeParks.wiki. To limit collection to major resorts you care about, add destination IDs or slugs to [config/major_destinations.txt](config/major_destinations.txt), one per line, or pass `destination_ids` when manually running the workflow.

## Data Layout

- `data/wait_times/YYYY-MM-DD.csv`: append-only attraction wait snapshot rows.
- `data/catalog/YYYY-MM-DD.csv`: attraction catalog rows captured during each run.
- `data/show_times/YYYY-MM-DD.csv`: optional on-demand showtime rows from `scripts/collect_show_times.py`.
- `data/run_log.jsonl`: one run summary per collection attempt.

Useful wait-time columns include:

- `captured_at`: when this repository collected the snapshot in UTC.
- `source_last_updated`: when ThemeParks.wiki says the entity was last updated.
- `destination_name`, `destination_timezone`, `park_name`, `entity_name`, `entity_type`.
- `status`: `OPERATING`, `DOWN`, `CLOSED`, or `REFURBISHMENT`.
- `standby_wait_minutes`, `single_rider_wait_minutes`, `paid_standby_wait_minutes`.
- return-time and boarding-group fields where the source provides them.

Optional showtime columns include:

- `captured_at`: when this repository collected the snapshot in UTC.
- `park_name`, `show_name`, `status`.
- `show_start_time`, `show_end_time`.

## Local Use

Run a dry check without writing files:

```bash
python scripts/collect_wait_times.py --dry-run --destination-ids waltdisneyworldresort
```

Collect and append data locally:

```bash
python scripts/collect_wait_times.py --destination-ids waltdisneyworldresort
```

Collect everything:

```bash
python scripts/collect_wait_times.py
```

Collect showtimes on demand for graph overlays:

```bash
python scripts/collect_show_times.py --destination-ids waltdisneyworldresort
```

## GitHub Actions Setup

1. Push this repository to GitHub.
2. Make sure repository workflow permissions allow GitHub Actions to write contents:
   `Settings -> Actions -> General -> Workflow permissions -> Read and write permissions`.
3. The workflow in [.github/workflows/collect-wait-times.yml](.github/workflows/collect-wait-times.yml) will run every 15 minutes and commit new rows under `data/`.

GitHub schedules are not guaranteed to fire exactly on the minute, but the workflow will request snapshots on the `*/15` cron cadence.

## Analyze Best Times

Once you have a few weeks of data, rank each attraction's lowest median wait windows by local day of week and hour:

```bash
python scripts/analyze_best_times.py --min-samples 8 --top 5
```

That writes `data/best_times.csv`.
