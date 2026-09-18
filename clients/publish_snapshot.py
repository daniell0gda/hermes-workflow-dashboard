#!/usr/bin/env python3
"""Publish a feature-check snapshot to the Hermes dashboard.

Replaces the git-push deployment. Text (status, events, graph, metrics and the
markdown reports) goes up as one JSON request; screenshots go up one file per
request as raw bytes, and only when their sha1 differs from what the server
already has.

    python3 publish_snapshot.py --base http://truenas.lan:8080 \
        --api-key SECRET --source .gen/feature-check-dashboard

Use --dry-run to inspect what would be sent without contacting the server.
The API key may also come from the HFCD_API_KEY environment variable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Documents the worker writes per run, in the order the dashboard shows them.
PRIMARY_DOCUMENTS = (
    "team-leader.report",
    "report",
    "manual-report",
    "request",
    "plan",
    "status",
    "check",
    "code",
    "revisions",
)

DOCUMENT_DIRS = (("clusters", "cluster"), ("coder-reports", "coder"))

MEDIA_DIRS = ("screenshots",)
MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

MAX_MEDIA_BYTES = 32 * 1024 * 1024
API_KEY_HEADER = "X-API-Key"
USER_AGENT = "hfcd-publisher/1.0"


class PublishError(RuntimeError):
    pass


class DashboardClient:
    def __init__(self, base: str, api_key: str, timeout: float = 120.0) -> None:
        base = base.rstrip("/")
        # Accept a bare host, or a URL that already points at the API.
        self.base = base if base.endswith("/api") else f"{base}/api"
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method: str, path: str, data: bytes | None, content_type: str | None) -> dict:
        headers = {API_KEY_HEADER: self.api_key, "User-Agent": USER_AGENT}
        if content_type:
            headers["Content-Type"] = content_type

        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:500]
            raise PublishError(f"{method} {path} -> HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise PublishError(f"{method} {path} -> {error.reason}") from error

        return json.loads(body) if body else {}

    def publish_run(self, payload: dict) -> dict:
        return self._request("POST", "/runs", json.dumps(payload).encode(), "application/json")

    def media_manifest(self, run_id: str) -> dict[str, str]:
        response = self._request("GET", f"/runs/{urllib.parse.quote(run_id)}/media", None, None)
        files = response.get("files")

        return files if isinstance(files, dict) else {}

    def upload_media(self, run_id: str, rel_path: str, data: bytes) -> dict:
        query = urllib.parse.urlencode({"path": rel_path})
        path = f"/runs/{urllib.parse.quote(run_id)}/media?{query}"

        return self._request("POST", path, data, "application/octet-stream")


def read_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"warning: unreadable {path.name}: {error}", file=sys.stderr)
        return None


def resolve_run_dir(source: Path, run_id: str | None) -> tuple[Path, str]:
    """Accept either a snapshot root (with runs/<id>/) or a single run directory."""
    if (source / "status.json").is_file() and not (source / "runs").is_dir():
        status = read_json(source / "status.json") or {}
        resolved = run_id or status.get("run_id")
        if not resolved:
            raise PublishError(f"{source}/status.json has no run_id")
        return source, str(resolved)

    if run_id is None:
        for probe in (source / "latest" / "status.json", source / "status.json"):
            status = read_json(probe)
            if isinstance(status, dict) and status.get("run_id"):
                run_id = str(status["run_id"])
                break

    if run_id is None:
        raise PublishError("no --run-id given and no status.json with a run_id found")

    run_dir = source / "runs" / run_id
    if not run_dir.is_dir():
        raise PublishError(f"run directory not found: {run_dir}")

    return run_dir, run_id


def read_document(path: Path) -> str | None:
    """A report the worker created but has not filled in yet is not a document."""
    if not path.is_file():
        return None

    body = path.read_text(encoding="utf-8")

    return body if body.strip() else None


def collect_documents(run_dir: Path) -> list[dict]:
    """
    Collect the run's markdown, skipping empty stubs and exact duplicates.

    The worker writes report.md and team-leader.report.md with identical content
    on most runs; publishing both would give the reader two identical tabs, so
    the first slug in PRIMARY_DOCUMENTS order wins.
    """
    documents: list[dict] = []
    seen: dict[str, str] = {}

    def add(slug: str, kind: str, body: str) -> None:
        digest = hashlib.sha1(body.encode("utf-8")).hexdigest()
        if digest in seen:
            print(f"skip (same content as {seen[digest]}): {slug}", file=sys.stderr)
            return
        seen[digest] = slug
        documents.append({"slug": slug, "kind": kind, "body": body})

    for slug in PRIMARY_DOCUMENTS:
        body = read_document(run_dir / f"{slug}.md")
        if body is not None:
            add(slug, "primary", body)

    for directory, kind in DOCUMENT_DIRS:
        for path in sorted((run_dir / directory).glob("*.md")):
            body = read_document(path)
            if body is not None:
                add(f"{directory}/{path.stem}", kind, body)

    return documents


def collect_media(run_dir: Path) -> dict[str, bytes]:
    media: dict[str, bytes] = {}

    for directory in MEDIA_DIRS:
        for path in sorted((run_dir / directory).glob("*")):
            if not path.is_file() or path.suffix.lower() not in MEDIA_SUFFIXES:
                continue
            if path.stat().st_size > MAX_MEDIA_BYTES:
                print(f"skip (too large): {directory}/{path.name}", file=sys.stderr)
                continue
            media[f"{directory}/{path.name}"] = path.read_bytes()

    return media


def build_payload(run_dir: Path, run_id: str) -> dict:
    status = read_json(run_dir / "status.json")
    events = read_json(run_dir / "events.json")
    graph = read_json(run_dir / "graph.json")
    metrics = read_json(run_dir / "metrics.json")

    return {
        "run_id": run_id,
        "status": status if isinstance(status, dict) else {},
        "events": events if isinstance(events, list) else [],
        "graph": graph if isinstance(graph, dict) else None,
        "metrics": metrics if isinstance(metrics, dict) else {},
        "documents": collect_documents(run_dir),
    }


def sync_media(client: DashboardClient, run_id: str, media: dict[str, bytes]) -> tuple[int, int]:
    remote = client.media_manifest(run_id)
    uploaded = 0
    skipped = 0

    for rel_path, data in media.items():
        if remote.get(rel_path) == hashlib.sha1(data).hexdigest():
            skipped += 1
            continue
        client.upload_media(run_id, rel_path, data)
        uploaded += 1

    return uploaded, skipped


class HttpDeployment:
    """Drop-in replacement for GitDeployment: publish(source) and nothing else."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        run_id: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.client = DashboardClient(base_url, api_key, timeout)
        self.run_id = run_id

    def publish(self, source: Path | str) -> dict:
        run_dir, run_id = resolve_run_dir(Path(source), self.run_id)
        result = self.client.publish_run(build_payload(run_dir, run_id))
        uploaded, skipped = sync_media(self.client, run_id, collect_media(run_dir))

        return {
            "run_id": run_id,
            "events": result.get("events", 0),
            "documents": result.get("documents", 0),
            "media_uploaded": uploaded,
            "media_unchanged": skipped,
        }


def describe(run_dir: Path, run_id: str) -> None:
    payload = build_payload(run_dir, run_id)
    media = collect_media(run_dir)

    print(f"run_id:    {run_id}")
    print(f"source:    {run_dir}")
    print(f"status:    {payload['status'].get('status', '?')} (phase {payload['status'].get('phase', '?')})")
    print(f"events:    {len(payload['events'])}")
    print(f"graph:     {'yes' if payload['graph'] else 'none'}")
    print(f"documents: {len(payload['documents'])}")
    for document in payload["documents"]:
        print(f"  - {document['slug']:<44} {len(document['body']):>7} chars  [{document['kind']}]")
    print(f"media:     {len(media)}")
    for rel_path, data in media.items():
        print(f"  - {rel_path:<44} {len(data):>7} bytes  {hashlib.sha1(data).hexdigest()[:12]}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base", help="Dashboard base URL, e.g. http://truenas.lan:8080")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("HFCD_API_KEY"),
        help="Value for the X-API-Key header (default: $HFCD_API_KEY)",
    )
    parser.add_argument("--run-id", default=None, help="Override the run id from status.json")
    parser.add_argument(
        "--source", default=".", help="Snapshot root (contains runs/) or a single run directory"
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--dry-run", action="store_true", help="Print what would be sent and exit")
    args = parser.parse_args()

    try:
        if args.dry_run:
            run_dir, run_id = resolve_run_dir(Path(args.source), args.run_id)
            describe(run_dir, run_id)
            return 0

        if not args.base or not args.api_key:
            parser.error("--base and --api-key (or $HFCD_API_KEY) are required unless --dry-run")

        result = HttpDeployment(args.base, args.api_key, args.run_id, args.timeout).publish(args.source)
        print(
            "published {run_id}: {events} events, {documents} documents, "
            "{media_uploaded} media uploaded ({media_unchanged} unchanged)".format(**result)
        )
        return 0
    except PublishError as error:
        print(f"publish failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
