#!/usr/bin/env python3
"""Check whether a chosen daily wall-clock time survives upcoming DST changes."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def check_schedule(zone_name: str, clock_time: str, *, weekdays_only: bool = False,
                   start_date: date | None = None) -> dict:
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Use a valid IANA time zone such as America/New_York") from exc
    try:
        hour, minute = (int(piece) for piece in clock_time.split(":"))
        if len(clock_time) != 5 or not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
        chosen = time(hour, minute)
    except (ValueError, TypeError) as exc:
        raise ValueError("Use a 24-hour local time like 06:30") from exc
    start = start_date or datetime.now(zone).date()
    if not isinstance(start, date):
        raise ValueError("start_date must be a calendar date")
    examples = []
    unstable = []
    for offset in range(370):
        day = start + timedelta(days=offset)
        if weekdays_only and day.weekday() >= 5:
            continue
        local = datetime.combine(day, chosen, tzinfo=zone)
        round_trip = local.astimezone(timezone.utc).astimezone(zone)
        ambiguous = local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset()
        nonexistent = (round_trip.date(), round_trip.time().replace(tzinfo=None)) != (day, chosen)
        if ambiguous or nonexistent:
            unstable.append({"date": day.isoformat(), "reason": "repeated local time" if ambiguous and not nonexistent else "missing local time"})
        if len(examples) < 3:
            examples.append(local.isoformat())
    return {"status": "stable" if not unstable else "choose_another_time", "timezone": zone_name,
            "local_time": clock_time, "weekdays_only": weekdays_only,
            "next_examples": examples, "unstable_dates": unstable}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--time", required=True)
    parser.add_argument("--weekdays-only", action="store_true")
    args = parser.parse_args()
    try:
        result = check_schedule(args.timezone, args.time, weekdays_only=args.weekdays_only)
    except ValueError as exc:
        parser.exit(2, f"Schedule rejected: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "stable" else 1


if __name__ == "__main__":
    raise SystemExit(main())
