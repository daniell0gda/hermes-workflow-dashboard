#!/usr/bin/env python3
"""Cheap status / heartbeat pings for a run that is already published.

    python3 status_http.py --base http://truenas.lan:8080 --api-key SECRET \
        --run-id my-run --status running --phase code

    python3 status_http.py --base ... --run-id my-run --heartbeat

The API key may also come from the HFCD_API_KEY environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

STATUSES = ("running", "completed", "failed", "cancelled", "interrupted")
API_KEY_HEADER = "X-API-Key"
USER_AGENT = "hfcd-status/1.0"


def post(base: str, api_key: str, path: str, payload: dict) -> dict:
    base = base.rstrip("/")
    if not base.endswith("/api"):
        base = f"{base}/api"

    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            API_KEY_HEADER: api_key,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(str(error.reason)) from error


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base", required=True, help="Dashboard base URL, e.g. http://truenas.lan:8080")
    parser.add_argument("--api-key", default=os.environ.get("HFCD_API_KEY"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--status", default=None, choices=STATUSES)
    parser.add_argument("--phase", default=None)
    parser.add_argument("--active-node", default=None)
    parser.add_argument("--last-node", default=None)
    parser.add_argument("--error", default=None)
    parser.add_argument("--heartbeat", action="store_true", help="Only refresh heartbeat_at")
    args = parser.parse_args()

    if not args.api_key:
        parser.error("--api-key or $HFCD_API_KEY is required")

    run_path = f"/runs/{urllib.parse.quote(args.run_id)}"

    try:
        if args.heartbeat:
            print(json.dumps(post(args.base, args.api_key, f"{run_path}/heartbeat", {})))
            return 0

        payload = {}
        for field in ("status", "phase", "active_node", "last_node", "error"):
            value = getattr(args, field)
            if value is not None:
                payload[field] = value

        if not payload:
            parser.error(
                "nothing to update: pass --status/--phase/--active-node/--last-node/--error "
                "or --heartbeat"
            )

        print(json.dumps(post(args.base, args.api_key, f"{run_path}/status", payload)))
        return 0
    except RuntimeError as error:
        print(f"status update failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
