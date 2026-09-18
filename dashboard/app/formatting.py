"""Presentation helpers, exposed to the templates as Jinja filters.

Absolute times render in the container's local timezone (set ``TZ`` in compose);
everything stored is UTC.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

DASH = "—"

STORAGE_FORMAT = "%Y-%m-%d %H:%M:%S"

# A run still claiming to be running but silent for this long is suspect.
STALE_AFTER_SECONDS = 120
ABANDONED_AFTER_SECONDS = 600


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def now_text() -> str:
    return utc_now().strftime(STORAGE_FORMAT)


def abandoned_cutoff() -> str:
    """A heartbeat older than this marks a run abandoned.

    Storage format, so a query can compare it against ``heartbeat_at`` as text
    and reach the same verdict as :func:`health` does on read.
    """
    return _storage_text(utc_now() - timedelta(seconds=ABANDONED_AFTER_SECONDS))


def period_starts(now: datetime | None = None) -> dict[str, str]:
    """Storage-format start of today, of this week and of this month.

    The boundaries are local midnights, because that is what a reader means by
    "today" and the local day is what :func:`absolute` already renders. The week
    starts on Monday, following the ISO calendar. Each midnight is dropped to a
    naive wall clock first, so converting it back to UTC picks the offset in
    force at that midnight rather than the one in force now.
    """
    midnight = (now or utc_now()).astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )

    return {
        "today": _storage_text(midnight),
        "week": _storage_text(midnight - timedelta(days=midnight.weekday())),
        "month": _storage_text(midnight.replace(day=1)),
    }


def to_storage(value: Any) -> str | None:
    """Normalise an ISO-8601 timestamp from the worker into storage form."""
    parsed = parse_any(value)

    return None if parsed is None else _storage_text(parsed)


def _storage_text(moment: datetime) -> str:
    """Storage form of a moment. A naive one is read as local time."""
    return moment.astimezone(timezone.utc).strftime(STORAGE_FORMAT)


def parse_any(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text or text.startswith("0000"):
        return None

    # Python's parser wants +00:00 rather than the worker's trailing Z.
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def duration(milliseconds: Any) -> str:
    if milliseconds is None:
        return DASH

    seconds = round(int(milliseconds) / 1000)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"

    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def elapsed_ms(value: Any) -> int | None:
    """Milliseconds from the given moment until now, floored at zero."""
    parsed = parse_any(value)
    if parsed is None:
        return None

    return max(0, int((utc_now() - parsed).total_seconds())) * 1000


def run_duration_ms(run: Mapping[str, Any]) -> int | None:
    """How long a run took, or how long it has been going if still live."""
    if run.get("duration_ms") is not None:
        return int(run["duration_ms"])
    if run.get("status") == "running":
        return elapsed_ms(run.get("started_at"))

    return None


def human_delta(seconds: int) -> str:
    minutes = seconds // 60
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        return f"{minutes} min" if minutes == 1 else f"{minutes} mins"

    hours = minutes // 60
    if hours < 24:
        return "1 hour" if hours == 1 else f"{hours} hours"

    days = hours // 24
    if days < 30:
        return "1 day" if days == 1 else f"{days} days"

    months = days // 30
    if months < 12:
        return "1 month" if months == 1 else f"{months} months"

    years = days // 365

    return "1 year" if years == 1 else f"{years} years"


def relative(value: Any) -> str:
    parsed = parse_any(value)
    if parsed is None:
        return DASH

    seconds = int((utc_now() - parsed).total_seconds())
    # Also covers worker clock skew: a future stamp reads as "just now".
    if seconds < 45:
        return "just now"

    return f"{human_delta(seconds)} ago"


def absolute(value: Any) -> str:
    parsed = parse_any(value)
    if parsed is None:
        return DASH

    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def iso(value: Any) -> str:
    parsed = parse_any(value)

    return "" if parsed is None else parsed.astimezone(timezone.utc).isoformat()


def health(run: Mapping[str, Any]) -> dict[str, Any] | None:
    """Liveness of a running run, derived on read so no cron job is needed."""
    if run.get("status") != "running":
        return None

    parsed = parse_any(run.get("heartbeat_at"))
    if parsed is None:
        return None

    age = max(0, int((utc_now() - parsed).total_seconds()))
    if age > ABANDONED_AFTER_SECONDS:
        return {
            "state": "abandoned",
            "label": f"Abandoned — no heartbeat for {human_delta(age)}.",
            "age": age,
        }
    if age > STALE_AFTER_SECONDS:
        return {
            "state": "stale",
            "label": f"Possibly stale — last heartbeat {human_delta(age)} ago.",
            "age": age,
        }

    return {"state": "live", "label": "Live — heartbeat is current.", "age": age}


def display_status(run: Mapping[str, Any]) -> str:
    """A run whose heartbeat expired is reported as abandoned, not running."""
    state = health(run)
    if state is not None and state["state"] == "abandoned":
        return "abandoned"

    return str(run.get("status") or "unknown")


def label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return DASH

    return text.replace("-", " ").replace("_", " ").capitalize()


def count(value: Any) -> str:
    if value is None:
        return DASH

    return f"{int(value):,}"


def money(value: Any) -> str:
    if value is None:
        return DASH

    return f"${float(value):,.2f}"


def tokens(value: Any) -> str:
    if value is None:
        return DASH

    number = float(value)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}k"

    return str(int(number))


def one_line(text: Any, length: int = 150) -> str:
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return ""

    return collapsed if len(collapsed) <= length else collapsed[: length - 1] + "…"


def filesize(value: Any) -> str:
    if value is None:
        return DASH

    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024

    return f"{size:.1f} GB"


JINJA_FILTERS = {
    "duration": duration,
    "elapsed_ms": elapsed_ms,
    "relative": relative,
    "absolute": absolute,
    "iso": iso,
    "label": label,
    "count": count,
    "money": money,
    "tokens": tokens,
    "one_line": one_line,
    "filesize": filesize,
}
