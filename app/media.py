"""Screenshot and GIF storage.

Bytes go to ``<data_dir>/media/<run_id>/`` so the app serves them as ordinary
static files with normal caching; only metadata goes in the database. Uploads
are decoded with Pillow before being written, because the directory is served
publicly and an extension is not evidence of content.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from . import formatting
from .repository import Repository

MAX_BYTES = 32 * 1024 * 1024

THUMB_DIRNAME = "thumbs"
THUMB_SIZE = (720, 480)

# Extension -> the Pillow format the bytes must actually decode as.
EXTENSION_FORMAT = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".gif": "GIF",
    ".webp": "WEBP",
}

FORMAT_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class MediaError(ValueError):
    """Upload rejected. ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def sanitize_path(value: Any) -> str:
    """Accept only a plain relative path of at most two segments with an
    extension - the shape the worker produces (``screenshots/x.png``)."""
    text = str(value or "").replace("\\", "/")

    segments = []
    for segment in text.split("/"):
        cleaned = _UNSAFE.sub("", segment)
        if cleaned and cleaned not in {".", "..", THUMB_DIRNAME}:
            segments.append(cleaned)

    if len(segments) > 2:
        segments = [segments[0], segments[-1]]

    joined = "/".join(segments)[:191]

    return joined if "." in Path(joined).name else ""


def run_dir(media_root: Path, run_id: str) -> Path:
    return media_root / run_id


def url_for(run_id: str, rel_path: str) -> str:
    return f"/media/{run_id}/{rel_path}"


def thumb_url_for(artifact: dict[str, Any]) -> str:
    """Falls back to the full-size file when there is no thumbnail."""
    if not artifact.get("thumb_name"):
        return url_for(artifact["run_id"], artifact["rel_path"])

    return url_for(artifact["run_id"], f"{THUMB_DIRNAME}/{artifact['thumb_name']}")


def store(
    repository: Repository,
    media_root: Path,
    run_id: str,
    rel_path: Any,
    data: bytes,
) -> dict[str, Any]:
    rel_path = sanitize_path(rel_path)
    if not rel_path:
        raise MediaError("Unusable artifact path.", 400)
    if not data:
        raise MediaError("Empty request body.", 400)
    if len(data) > MAX_BYTES:
        raise MediaError(f"Artifact exceeds {MAX_BYTES // (1024 * 1024)} MB.", 413)

    extension = Path(rel_path).suffix.lower()
    expected = EXTENSION_FORMAT.get(extension)
    if expected is None:
        accepted = ", ".join(sorted(EXTENSION_FORMAT))
        raise MediaError(f"Unsupported artifact type {extension!r}; accepted: {accepted}.", 415)

    detected, size, animated = _probe(data)
    if detected != expected:
        raise MediaError(
            f"Content decodes as {detected or 'unknown'}, which does not match {extension}.", 415
        )

    target = run_dir(media_root, run_id) / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    record = {
        "run_id": run_id,
        "rel_path": rel_path,
        "file_name": Path(rel_path).name,
        "mime": FORMAT_MIME[detected],
        "bytes": len(data),
        "width": size[0] or None,
        "height": size[1] or None,
        "animated": 1 if animated else 0,
        "checksum": hashlib.sha1(data).hexdigest(),
        # Resizing an animated GIF would drop the animation, so it is served whole.
        "thumb_name": None if animated else _make_thumbnail(media_root, run_id, rel_path, data),
        "sort_order": 0,
        "created_at": formatting.now_text(),
    }

    repository.save_artifact(record)
    repository.renumber_artifacts(run_id)

    return record


def delete_run(media_root: Path, run_id: str) -> None:
    shutil.rmtree(run_dir(media_root, run_id), ignore_errors=True)


def _probe(data: bytes) -> tuple[str | None, tuple[int, int], bool]:
    """Decode far enough to trust the format, then read the metadata."""
    try:
        with Image.open(BytesIO(data)) as probe:
            probe.verify()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return None, (0, 0), False

    # verify() consumes the file object, so reopen for the actual metadata.
    with Image.open(BytesIO(data)) as image:
        return image.format, image.size, bool(getattr(image, "is_animated", False))


def _make_thumbnail(media_root: Path, run_id: str, rel_path: str, data: bytes) -> str | None:
    name = f"{hashlib.sha1(rel_path.encode()).hexdigest()[:12]}-{Path(rel_path).name}"
    target = run_dir(media_root, run_id) / THUMB_DIRNAME / name

    try:
        with Image.open(BytesIO(data)) as image:
            # Already small enough: serving the original is correct and cheaper.
            if image.width <= THUMB_SIZE[0] and image.height <= THUMB_SIZE[1]:
                return None

            image.thumbnail(THUMB_SIZE)
            target.parent.mkdir(parents=True, exist_ok=True)
            image.save(target)
    except (OSError, ValueError):
        return None

    return name
