from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from lilical.backends.base import PermanentError, TransientError

log = logging.getLogger(__name__)


def canonicalize_source(source: str) -> str:
    """Normalize a user-typed source string to a stable canonical form.

    - webcal:// → https://
    - bare absolute path / ~ → file:///abs/path
    - http(s):// passed through unchanged
    """
    s = source.strip()
    if not s:
        raise ValueError("empty source")
    if s.startswith("webcal://"):
        return "https://" + s[len("webcal://") :]
    if s.startswith(("http://", "https://", "file://")):
        return s
    # Bare path: expand ~ and resolve to absolute file:// URL.
    p = Path(s).expanduser()
    if not p.is_absolute():
        p = p.resolve()
    return f"file://{p}"


def _file_path_from_url(source: str) -> Path:
    """Extract the local filesystem path from a file:// URL."""
    parsed = urlparse(source)
    if parsed.scheme != "file":
        raise ValueError(f"not a file URL: {source}")
    # Reconstruct path; urlparse splits on first '/' so netloc is usually empty.
    return Path(parsed.netloc + parsed.path)


async def fetch_ics(
    source: str,
    *,
    prev_etag: str | None = None,
    prev_last_modified: str | None = None,
) -> tuple[bytes | None, str | None, str | None]:
    """Fetch an ICS source if it has changed.

    Returns (body, etag, last_modified). `body` is None when the source is
    unchanged (HTTP 304 or matching file mtime). `etag` and `last_modified`
    are bandwidth hints to pass back next call.

    Raises PermanentError for non-recoverable failures (bad URL, file not
    found, 4xx) and TransientError for recoverable ones (network, 5xx).
    """
    canon = canonicalize_source(source)
    parsed = urlparse(canon)

    if parsed.scheme == "file":
        return _fetch_file(canon, prev_last_modified)
    if parsed.scheme in ("http", "https"):
        return await _fetch_http(canon, prev_etag, prev_last_modified)
    raise PermanentError(f"unsupported scheme: {parsed.scheme}")


def _fetch_file(
    source: str, prev_last_modified: str | None
) -> tuple[bytes | None, str | None, str | None]:
    path = _file_path_from_url(source)
    try:
        st = path.stat()
    except FileNotFoundError as e:
        raise PermanentError(f"file not found: {path}") from e
    except OSError as e:
        raise TransientError(f"stat failed for {path}: {e}") from e

    mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    if prev_last_modified and prev_last_modified == mtime_iso:
        return None, None, mtime_iso
    try:
        body = path.read_bytes()
    except OSError as e:
        raise TransientError(f"read failed for {path}: {e}") from e
    return body, None, mtime_iso


async def _fetch_http(
    source: str, prev_etag: str | None, prev_last_modified: str | None
) -> tuple[bytes | None, str | None, str | None]:
    headers: dict[str, str] = {"User-Agent": "lilical/0.1 (+subscription)"}
    if prev_etag:
        headers["If-None-Match"] = prev_etag
    if prev_last_modified:
        headers["If-Modified-Since"] = prev_last_modified

    try:
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True
        ) as client:
            resp = await client.get(source, headers=headers)
    except httpx.HTTPError as e:
        raise TransientError(f"HTTP error fetching {source}: {e}") from e

    if resp.status_code == 304:
        return None, prev_etag, prev_last_modified
    if 400 <= resp.status_code < 500:
        raise PermanentError(f"HTTP {resp.status_code} for {source}")
    if resp.status_code >= 500:
        raise TransientError(f"HTTP {resp.status_code} for {source}")

    etag = resp.headers.get("ETag")
    last_modified = resp.headers.get("Last-Modified")
    return resp.content, etag, last_modified


__all__ = [
    "canonicalize_source",
    "fetch_ics",
]
