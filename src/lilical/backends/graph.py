from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import re
import zoneinfo
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable

from lilical.backends.base import (
    AuthExpired,
    ConflictError,
    CursorExpired,
    EventChange,
    PermanentError,
    SyncCursor,
    TransientError,
)
from lilical.models.event import Event

log = logging.getLogger(__name__)

# Evolution's public Microsoft 365 client ID. We piggy-back on it because (a) it's
# a registered, multi-tenant public client, (b) many corporate tenants that block
# generic "unknown app" consent already allow Evolution, and (c) Thunderbird's
# client ID hits "needs admin approval" in stricter tenants where Evolution does
# not. Trade-off: the user-facing consent screen reads "Evolution / GNOME".
GRAPH_CLIENT_ID = "20460e5d-ce91-49af-a3a5-70b6be7486d1"
GRAPH_AUTHORITY = "https://login.microsoftonline.com/common"
GRAPH_SCOPES = ["Calendars.ReadWrite", "User.Read"]
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# calendarView/delta requires an explicit window. We default to ±2 years; events
# outside the window simply aren't synced (Outlook desktop uses a similar bound).
_DELTA_WINDOW_PAST = timedelta(days=365)
_DELTA_WINDOW_FUTURE = timedelta(days=730)


class GraphCursor(SyncCursor):
    def __init__(self, delta_link: str | None = None) -> None:
        self.delta_link = delta_link

    def to_json(self) -> dict:
        return {"delta_link": self.delta_link}

    @classmethod
    def from_json(cls, data: dict) -> GraphCursor:
        return cls(delta_link=data.get("delta_link"))


def _status_of(exc: Exception) -> int | None:
    resp = getattr(exc, "response", None)
    if resp is not None:
        status = getattr(resp, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def _classify_one(exc: Exception) -> Exception:
    status = _status_of(exc)
    if status == 401 or status == 403:
        return AuthExpired(str(exc))
    if status == 410:
        return CursorExpired()
    if status == 412:
        return ConflictError(str(exc))
    if status is not None and (status == 429 or status >= 500):
        return TransientError(str(exc))
    return PermanentError(str(exc))


def _classify_errors(f):
    if inspect.isasyncgenfunction(f):

        @functools.wraps(f)
        async def wrapper(*args, **kwargs):
            try:
                async for item in f(*args, **kwargs):
                    yield item
            except (
                AuthExpired,
                CursorExpired,
                ConflictError,
                TransientError,
                PermanentError,
            ):
                raise
            except Exception as exc:
                raise _classify_one(exc) from exc

        return wrapper

    @functools.wraps(f)
    async def wrapper(*args, **kwargs):
        try:
            return await f(*args, **kwargs)
        except (
            AuthExpired,
            CursorExpired,
            ConflictError,
            TransientError,
            PermanentError,
        ):
            raise
        except Exception as exc:
            raise _classify_one(exc) from exc

    return wrapper


# Graph emits 7-digit fractional seconds (e.g. "...09:00:00.0000000") which
# datetime.fromisoformat rejects pre-3.11-style; strip extra digits so any
# Python version parses cleanly.
_FRACTIONAL_TRIM_RE = re.compile(r"\.(\d{6})\d+")


def _parse_graph_dt(raw: str | None, tz_hint: str | None) -> datetime | None:
    """Parse a Graph dateTime string and attach tzinfo from tz_hint.

    Graph always returns naive datetimes; the timezone arrives separately on
    the parent {dateTime, timeZone} object. For all-day events the timeZone is
    typically "UTC" and the resulting datetime represents midnight on the
    given local date — matching EventStore._ensure_aware_dt's convention.
    """
    if not raw:
        return None
    cleaned = _FRACTIONAL_TRIM_RE.sub(r".\1", raw)
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        return dt
    if tz_hint and tz_hint != "UTC":
        try:
            return dt.replace(tzinfo=zoneinfo.ZoneInfo(tz_hint))
        except Exception:
            pass
    return dt.replace(tzinfo=timezone.utc)


def _safe(fn, *, field: str, default=None):
    try:
        return fn()
    except Exception:
        log.exception("error extracting Graph field %s", field)
        return default


def _graph_event_to_change(ev_json: dict, calendar_id: str) -> EventChange:
    # Delta responses mark deletions with an "@removed" key on the otherwise-stub event.
    # We key local rows on Graph's `id`, not `iCalUId` — Graph's calendarView
    # pre-expands recurring events, so every occurrence carries the same
    # iCalUId but a distinct id. Using id avoids occurrences clobbering each
    # other through apply_remote_changes's (uid, calendar_id) filter and
    # matches what /me/events/{id} expects for delete.
    if "@removed" in ev_json:
        uid = ev_json.get("id") or ev_json.get("iCalUId") or ""
        return EventChange(kind="delete", uid=uid)

    uid = ev_json.get("id") or ev_json.get("iCalUId") or ""
    body = ev_json.get("body") or {}
    location = ev_json.get("location") or {}

    start_obj = ev_json.get("start") or {}
    end_obj = ev_json.get("end") or {}
    tz = str(start_obj.get("timeZone") or "UTC")
    dtstart = _safe(
        lambda: _parse_graph_dt(start_obj.get("dateTime"), tz),
        field="start.dateTime",
    )
    dtend = _safe(
        lambda: _parse_graph_dt(end_obj.get("dateTime"), end_obj.get("timeZone") or tz),
        field="end.dateTime",
    )

    all_day = bool(ev_json.get("isAllDay"))
    status = "CANCELLED" if ev_json.get("isCancelled") else "CONFIRMED"
    show_as = str(ev_json.get("showAs") or "").lower()
    transparency = "TRANSPARENT" if show_as in {"free", "workingelsewhere"} else "OPAQUE"

    categories_raw = ev_json.get("categories") or []
    categories = tuple(str(c) for c in categories_raw if c)

    attendees_raw = ev_json.get("attendees") or []
    attendees: list[str] = []
    for a in attendees_raw:
        email = (a.get("emailAddress") or {}).get("address") if isinstance(a, dict) else None
        if email:
            attendees.append(str(email))

    last_modified = _safe(
        lambda: _parse_graph_dt(ev_json.get("lastModifiedDateTime"), "UTC"),
        field="lastModifiedDateTime",
    )

    event = Event(
        uid=uid,
        calendar_id=calendar_id,
        provider_event_id=ev_json.get("id"),
        dtstart=dtstart,
        dtend=dtend,
        tz=tz,
        all_day=all_day,
        summary=ev_json.get("subject", "") or "",
        description=body.get("content", "") or "",
        location=location.get("displayName", "") or "",
        url=ev_json.get("webLink"),
        attendees=tuple(attendees),
        categories=categories,
        status=status,
        transparency=transparency,
        last_modified=last_modified,
        etag=ev_json.get("@odata.etag"),
    )
    return EventChange(kind="upsert", event=event, uid=uid)


def _event_to_graph_json(event: Event) -> dict[str, Any]:
    body: dict[str, Any] = {
        "subject": event.summary,
        "body": {"contentType": "text", "content": event.description},
    }
    if event.location:
        body["location"] = {"displayName": event.location}
    if event.dtstart is not None:
        body["start"] = _datetime_to_graph(event.dtstart, event.tz, event.all_day)
    if event.dtend is not None:
        body["end"] = _datetime_to_graph(event.dtend, event.tz, event.all_day)
    if event.all_day:
        body["isAllDay"] = True
    return body


def _datetime_to_graph(dt: datetime, tz: str, all_day: bool) -> dict[str, str]:
    if all_day:
        return {"dateTime": dt.strftime("%Y-%m-%d"), "timeZone": tz or "UTC"}
    if dt.tzinfo is None:
        return {"dateTime": dt.isoformat(), "timeZone": tz or "UTC"}
    return {"dateTime": dt.isoformat(), "timeZone": tz or "UTC"}


def _delta_window() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = (now - _DELTA_WINDOW_PAST).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now + _DELTA_WINDOW_FUTURE).strftime("%Y-%m-%dT%H:%M:%SZ")
    return start, end


def _new_msal_app(cache_json: str | None):
    import msal

    cache = msal.SerializableTokenCache()
    if cache_json:
        cache.deserialize(cache_json)
    app = msal.PublicClientApplication(
        GRAPH_CLIENT_ID,
        authority=GRAPH_AUTHORITY,
        token_cache=cache,
    )
    return app, cache


def initiate_graph_device_flow() -> tuple[Any, Any, dict]:
    """Start a device-code flow. Returns (msal_app, cache, flow_dict).

    The flow_dict has 'user_code', 'verification_uri', 'message', 'expires_in'.
    Pass the same app/cache/flow to complete_graph_device_flow to block until done.
    """
    app, cache = _new_msal_app(None)
    flow = app.initiate_device_flow(scopes=GRAPH_SCOPES)
    if "user_code" not in flow:
        err = flow.get("error_description") or flow.get("error") or "init failed"
        raise RuntimeError(str(err))
    return app, cache, flow


def complete_graph_device_flow(app: Any, cache: Any, flow: dict) -> str:
    """Block until the user signs in via the device code; return serialized cache."""
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        err = result.get("error_description") or result.get("error") or "auth failed"
        raise RuntimeError(str(err))
    return cache.serialize()


class GraphBackend:
    def __init__(
        self,
        account_id: str,
        token_cache_json: str | None = None,
        on_token_refreshed: Callable[[str], None] | None = None,
    ) -> None:
        self.account_id = account_id
        self._cache_json = token_cache_json
        self._on_token_refreshed = on_token_refreshed
        self._http = None  # httpx.AsyncClient, created lazily

    def _acquire_token(self) -> str:
        app, cache = _new_msal_app(self._cache_json)
        accounts = app.get_accounts()
        if not accounts:
            raise AuthExpired("no cached account; re-authenticate required")
        result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            raise AuthExpired("silent token acquisition failed")
        if cache.has_state_changed:
            self._cache_json = cache.serialize()
            if self._on_token_refreshed is not None:
                try:
                    self._on_token_refreshed(self._cache_json)
                except Exception:
                    log.exception("on_token_refreshed callback raised")
        return result["access_token"]

    def _get_http(self):
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
    ):
        import httpx

        token = await asyncio.to_thread(self._acquire_token)
        hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        full = url if url.startswith("http") else f"{GRAPH_BASE}{url}"
        client = self._get_http()
        try:
            resp = await client.request(method, full, json=json_body, headers=hdrs)
        except httpx.HTTPError as exc:
            raise TransientError(str(exc)) from exc
        if resp.status_code >= 400:
            resp.raise_for_status()
        return resp

    # Microsoft Graph color enum → hex map.
    # Source: https://learn.microsoft.com/en-us/graph/api/resources/calendar
    _GRAPH_COLOR_MAP: dict[str, str] = {
        "lightBlue": "#5e9fff",
        "lightGreen": "#5cc97a",
        "lightOrange": "#f59e0b",
        "lightGray": "#9ca3af",
        "lightYellow": "#eab308",
        "lightTeal": "#14b8a6",
        "lightPink": "#ec4899",
        "lightBrown": "#a16207",
        "lightRed": "#ef4444",
        "maxColor": "#6366f1",
    }

    @_classify_errors
    async def list_calendars(self) -> list:
        resp = await self._request(
            "GET", "/me/calendars?$select=id,name,color,hexColor"
        )
        data = resp.json()
        out = []
        for cal in data.get("value", []):
            # Prefer hexColor (newer field, exact hex) over the color enum.
            colour = cal.get("hexColor") or self._GRAPH_COLOR_MAP.get(
                cal.get("color", "")
            )
            if colour and not colour.startswith("#"):
                colour = "#" + colour
            out.append({
                "id": cal.get("id", ""),
                "display_name": cal.get("name") or cal.get("id") or "",
                "provider_id": cal.get("id", ""),
                "color": (colour or "").lower() or None,
            })
        return out

    @_classify_errors
    async def initial_sync(
        self, calendar_id: str
    ) -> AsyncIterator[tuple[list[EventChange], SyncCursor]]:
        start, end = _delta_window()
        url = (
            f"/me/calendars/{calendar_id}/calendarView/delta"
            f"?startDateTime={start}&endDateTime={end}"
        )
        async for batch, cursor in self._drain_delta(url, calendar_id):
            yield batch, cursor

    @_classify_errors
    async def incremental_sync(
        self, calendar_id: str, cursor: SyncCursor
    ) -> tuple[list[EventChange], SyncCursor]:
        delta_link = getattr(cursor, "delta_link", None)
        if not delta_link:
            raise CursorExpired(calendar_id)
        changes: list[EventChange] = []
        new_cursor = GraphCursor()
        async for batch, c in self._drain_delta(delta_link, calendar_id):
            changes.extend(batch)
            new_cursor = c  # last yielded cursor carries the final delta link
        return changes, new_cursor

    async def _drain_delta(
        self, url: str, calendar_id: str
    ) -> AsyncIterator[tuple[list[EventChange], SyncCursor]]:
        next_url: str | None = url
        while next_url:
            resp = await self._request("GET", next_url)
            data = resp.json()
            batch = [
                _graph_event_to_change(ev, calendar_id) for ev in data.get("value", [])
            ]
            delta = data.get("@odata.deltaLink")
            link = data.get("@odata.nextLink")
            cursor = GraphCursor(delta_link=delta) if delta else GraphCursor()
            yield batch, cursor
            next_url = link

    @_classify_errors
    async def create_event(self, calendar_id: str, event: Event) -> Event:
        resp = await self._request(
            "POST",
            f"/me/calendars/{calendar_id}/events",
            json_body=_event_to_graph_json(event),
        )
        data = resp.json()
        return Event(
            uid=data.get("iCalUId") or event.uid,
            calendar_id=calendar_id,
            provider_event_id=data.get("id"),
            summary=event.summary,
            description=event.description,
            location=event.location,
            dtstart=event.dtstart,
            dtend=event.dtend,
            tz=event.tz,
            all_day=event.all_day,
            etag=data.get("@odata.etag"),
        )

    @_classify_errors
    async def update_event(
        self, calendar_id: str, event: Event, if_match: str | None
    ) -> Event:
        if not event.provider_event_id:
            raise PermanentError("update_event requires provider_event_id")
        headers = {"If-Match": if_match} if if_match else None
        resp = await self._request(
            "PATCH",
            f"/me/events/{event.provider_event_id}",
            json_body=_event_to_graph_json(event),
            headers=headers,
        )
        data = resp.json()
        return Event(
            uid=event.uid,
            calendar_id=calendar_id,
            provider_event_id=event.provider_event_id,
            summary=event.summary,
            description=event.description,
            location=event.location,
            dtstart=event.dtstart,
            dtend=event.dtend,
            tz=event.tz,
            all_day=event.all_day,
            etag=data.get("@odata.etag", event.etag),
        )

    @_classify_errors
    async def delete_event(
        self, calendar_id: str, uid: str, if_match: str | None
    ) -> None:
        # Caller passes provider_event_id in uid (Graph deletes by event id).
        headers = {"If-Match": if_match} if if_match else None
        await self._request(
            "DELETE",
            f"/me/events/{uid}",
            headers=headers,
        )

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
