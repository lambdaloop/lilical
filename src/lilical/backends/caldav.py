from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import re
import zoneinfo
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date as _date_cls
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urljoin, urlparse

import caldav
import icalendar
from caldav.elements import dav as _dav_elements
from caldav.lib.error import AuthorizationError, DAVError, ReportError

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
from lilical.utils.timezone import local_iana_tz

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CalDavCursor(SyncCursor):
    _TYPE = "caldav"

    sync_token: str | None = None
    ctag: str | None = None

    def to_json(self) -> dict[str, object]:
        return {"_type": self._TYPE, "sync_token": self.sync_token, "ctag": self.ctag}

    @classmethod
    def from_json(cls, data: dict[str, object]) -> CalDavCursor:
        if data.get("_type") != cls._TYPE:
            raise ValueError(f"not a caldav cursor: {data!r}")
        return cls(sync_token=data.get("sync_token"), ctag=data.get("ctag"))  # type: ignore[reportArgumentType]


def _dav_status(e: DAVError) -> int | None:
    """Parse HTTP status code out of a DAVError's url
    field (e.g. 'HTTP/1.1 507 ...')."""
    url = getattr(e, "url", None) or ""
    m = re.search(r"\b([1-5][0-9]{2})\b", url)
    return int(m.group(1)) if m else None


def _classify_errors(f):
    op = f.__name__

    if inspect.isasyncgenfunction(f):

        @functools.wraps(f)
        async def wrapper(*args, **kwargs):
            try:
                async for item in f(*args, **kwargs):
                    yield item
            except AuthorizationError as e:
                raise AuthExpired(f"{op}: {e}") from e
            except DAVError as e:
                status = _dav_status(e)
                msg = f"{op}: {e}"
                if status in (401, 403):
                    raise AuthExpired(msg) from e
                if status == 410:
                    raise CursorExpired() from e
                if status == 412:
                    raise ConflictError(msg) from e
                if status is not None and status >= 500:
                    raise TransientError(msg) from e
                raise TransientError(msg) from e
            except CursorExpired:
                raise
            except Exception as e:
                log.exception("unclassified caldav error in %s", op)
                raise PermanentError(f"{op}: {e}") from e

        return wrapper
    else:

        @functools.wraps(f)
        async def wrapper_coro(*args, **kwargs):
            try:
                return await f(*args, **kwargs)
            except AuthorizationError as e:
                raise AuthExpired(f"{op}: {e}") from e
            except DAVError as e:
                status = _dav_status(e)
                msg = f"{op}: {e}"
                if status in (401, 403):
                    raise AuthExpired(msg) from e
                if status == 410:
                    raise CursorExpired() from e
                if status == 412:
                    raise ConflictError(msg) from e
                if status is not None and status >= 500:
                    raise TransientError(msg) from e
                raise TransientError(msg) from e
            except CursorExpired:
                raise
            except Exception as e:
                log.exception("unclassified caldav error in %s", op)
                raise PermanentError(f"{op}: {e}") from e

        return wrapper_coro


def _normalise_dt(val, tzid_hint: str | None = None) -> datetime | None:
    """Coerce an icalendar `.dt` value (date or datetime) to a datetime.

    - `date` (all-day) → naive `datetime` at midnight (matches the convention
      used by `EventStore._ensure_aware_dt`, which then assumes UTC).
    - `datetime` with tzinfo → returned as-is.
    - naive `datetime` → tzinfo set from `tzid_hint` if it resolves, else UTC.
    """
    if val is None:
        return None
    if isinstance(val, _date_cls) and not isinstance(val, datetime):
        return datetime.combine(val, time.min)
    if isinstance(val, datetime):
        if val.tzinfo is not None:
            return val
        if tzid_hint and tzid_hint != "UTC":
            try:
                return val.replace(tzinfo=zoneinfo.ZoneInfo(tzid_hint))
            except Exception:
                log.debug("unknown TZID %r, falling back to UTC", tzid_hint)
        return val.replace(tzinfo=timezone.utc)
    return None


def _prop_dt(prop, tzid_hint: str | None = None) -> datetime | None:
    if prop is None:
        return None
    return _normalise_dt(getattr(prop, "dt", None), tzid_hint)


def _prop_dt_tuple(prop) -> tuple[datetime, ...]:
    """Flatten EXDATE / RDATE into a tuple of datetimes.

    Each property may be a single value or a list (multiple EXDATE lines).
    Each property's `.dts` is a list of vDDDTypes whose `.dt` is the value.
    Some flavors expose `.dt` directly for single values; handle both.
    """
    if prop is None:
        return ()
    items = prop if isinstance(prop, list) else [prop]
    out: list[datetime] = []
    for p in items:
        dts = getattr(p, "dts", None)
        if dts is not None:
            for entry in dts:
                normalised = _normalise_dt(getattr(entry, "dt", None))
                if normalised is not None:
                    out.append(normalised)
        else:
            normalised = _normalise_dt(getattr(p, "dt", None))
            if normalised is not None:
                out.append(normalised)
    return tuple(out)


def _safe(fn, *, field: str, default=None):
    """Call `fn` and return its value; on exception log and return `default`.

    Used to isolate per-field VEVENT extraction so one malformed field doesn't
    drop the whole event.
    """
    try:
        return fn()
    except Exception:
        log.exception("error extracting VEVENT field %s", field)
        return default


def _vevent_to_event(
    ve: icalendar.Event,
    *,
    calendar_id: str,
    href: str,
    etag: str,
    user_email: str | None = None,
) -> Event:
    dtstart_prop = ve.get("DTSTART")
    dtstart_params = getattr(dtstart_prop, "params", None) if dtstart_prop else None
    all_day = bool(dtstart_params and dtstart_params.get("VALUE") == "DATE")
    tz = str(dtstart_params.get("TZID", "UTC")) if dtstart_params else "UTC"

    dtstart = _safe(lambda: _prop_dt(dtstart_prop, tz), field="DTSTART")

    dtend_prop = ve.get("DTEND")
    dtend = _safe(lambda: _prop_dt(dtend_prop, tz), field="DTEND")
    if dtend is None and dtstart is not None:
        duration_prop = ve.get("DURATION")
        dur = _safe(lambda: getattr(duration_prop, "dt", None), field="DURATION")
        if dur is not None:
            dtend = dtstart + dur
        elif all_day:
            dtend = dtstart + timedelta(days=1)
        else:
            dtend = dtstart

    rrule_prop = ve.get("RRULE")
    rrule = (
        _safe(lambda: rrule_prop.to_ical().decode(), field="RRULE")
        if rrule_prop is not None
        else None
    )

    exdates = _safe(
        lambda: _prop_dt_tuple(ve.get("EXDATE")), field="EXDATE", default=()
    )
    rdates = _safe(lambda: _prop_dt_tuple(ve.get("RDATE")), field="RDATE", default=())

    attendees_raw = ve.get("ATTENDEE")
    self_response: str | None = None
    user_email_norm = (user_email or "").strip().lower()
    if attendees_raw is None:
        attendees: tuple[str, ...] = ()
    else:
        items = attendees_raw if isinstance(attendees_raw, list) else [attendees_raw]
        attendees = tuple(str(a) for a in items)
        # If we know the user's email, scan attendees for a matching mailto:
        # and capture that attendee's PARTSTAT parameter.
        if user_email_norm:
            for a in items:
                addr = str(a).strip().lower()
                if addr.startswith("mailto:"):
                    addr = addr[7:]
                if addr != user_email_norm:
                    continue
                params = getattr(a, "params", None)
                if params is None:
                    continue
                partstat = str(params.get("PARTSTAT", "")).upper()
                if partstat in {"ACCEPTED", "TENTATIVE", "DECLINED", "NEEDS-ACTION"}:
                    self_response = partstat
                break

    categories_raw = ve.get("CATEGORIES")
    if categories_raw is None:
        categories: tuple[str, ...] = ()
    else:
        items = categories_raw if isinstance(categories_raw, list) else [categories_raw]
        flat: list[str] = []
        for it in items:
            cats = getattr(it, "cats", None)
            if cats is not None:
                flat.extend(str(c) for c in cats)
            else:
                flat.append(str(it))
        categories = tuple(flat)

    # RECURRENCE-ID identifies an override (exception) VEVENT.
    rid_prop = ve.get("RECURRENCE-ID")
    recurrence_id = (
        _safe(lambda: _prop_dt(rid_prop, tz), field="RECURRENCE-ID")
        if rid_prop is not None
        else None
    )

    url_prop = ve.get("URL")
    url = str(url_prop) if url_prop is not None else None

    last_modified = _safe(
        lambda: _prop_dt(ve.get("LAST-MODIFIED")), field="LAST-MODIFIED"
    )

    # Re-localize events whose source had no explicit TZID (Z-suffix or bare
    # UTC). This makes the timezone combo in the event dialog show the user's
    # local zone instead of "UTC". The instant is preserved; only the
    # wall-clock representation changes. Events with an explicit non-UTC TZID
    # (e.g. TZID=Europe/London) are left alone.
    if tz == "UTC" and not all_day and dtstart is not None:
        local_name = local_iana_tz()
        if local_name != "UTC":
            local_zone = zoneinfo.ZoneInfo(local_name)
            dtstart = dtstart.astimezone(local_zone)
            if dtend is not None:
                dtend = dtend.astimezone(local_zone)
            tz = local_name

    # All-day events arrive as naive midnight datetime (no tzinfo). Attach the
    # local zone so that .date() in display code returns the right calendar day
    # for users west of UTC (otherwise May 14 00:00 UTC → May 13 in EDT).
    if all_day and dtstart is not None and dtstart.tzinfo is None:
        local_name = local_iana_tz()
        local_zone = zoneinfo.ZoneInfo(local_name)
        dtstart = dtstart.replace(tzinfo=local_zone)
        if dtend is not None and dtend.tzinfo is None:
            dtend = dtend.replace(tzinfo=local_zone)
        tz = local_name

    return Event(
        uid=str(ve.get("UID", "")),
        calendar_id=calendar_id,
        recurrence_id=recurrence_id,
        provider_event_id=href,
        dtstart=dtstart,
        dtend=dtend,
        tz=tz,
        all_day=all_day,
        summary=str(ve.get("SUMMARY", "")),
        description=str(ve.get("DESCRIPTION", "")),
        location=str(ve.get("LOCATION", "")),
        url=url,
        rrule=rrule,
        exdates=exdates,
        rdates=rdates,
        attendees=attendees,
        categories=categories,
        status=str(ve.get("STATUS", "CONFIRMED")),
        self_response=self_response,
        transparency=str(ve.get("TRANSP", "OPAQUE")),
        last_modified=last_modified,
        etag=etag,
        sequence=int(ve.get("SEQUENCE", 0)),
    )


def _discover_caldav_url(base_url: str, username: str, password: str) -> str:
    """Resolve a CalDAV base URL via /.well-known/caldav (RFC 6764).

    If the host returns a redirect from /.well-known/caldav, the redirect
    target is returned. Otherwise, the original URL is returned unchanged.
    Discovery failures (network errors, 404s, non-redirect responses) are
    swallowed so the caller can fall back to whatever the user typed.
    """
    import httpx

    if not base_url:
        return base_url
    if "://" not in base_url:
        base_url = f"https://{base_url}"

    parsed = urlparse(base_url)
    if not parsed.netloc:
        return base_url

    well_known = f"{parsed.scheme}://{parsed.netloc}/.well-known/caldav"
    try:
        auth = (username, password) if username and password else None
        with httpx.Client(auth=auth, follow_redirects=False, timeout=10.0) as client:
            resp = client.request("PROPFIND", well_known)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if location:
                    resolved = urljoin(well_known, location)
                    log.info("CalDAV discovery: %s -> %s", base_url, resolved)
                    return resolved
    except Exception:
        log.debug("CalDAV .well-known discovery failed for %s", base_url, exc_info=True)
    return base_url


def _parse_vevents(raw: str | bytes | None) -> list[icalendar.Event]:
    """Extract VEVENT components from a CalDAV event's raw iCal payload.

    CalDAV servers always wrap events in a VCALENDAR. `icalendar.Event.from_ical`
    on a VCALENDAR blob returns a `Calendar`, not an `Event` — so we must walk
    the parsed Calendar for VEVENT children. A single iCal blob can contain
    multiple VEVENTs (recurrence overrides).
    """
    if not raw:
        return []
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        parsed = icalendar.Calendar.from_ical(raw)
    except Exception:
        log.warning("failed to parse caldav event ical", exc_info=True)
        return []
    if not hasattr(parsed, "walk"):
        return []
    return list(parsed.walk("VEVENT"))  # type: ignore[return-type]


def _normalise_hex_color(s: str | None) -> str | None:
    """Strip an Apple-style alpha suffix from a hex color.

    Stalwart and Apple Calendar return `#RRGGBBAA`; we keep only `#RRGGBB`.
    Invalid / empty values yield None.
    """
    if not s:
        return None
    s = s.strip()
    if not s.startswith("#"):
        return None
    if len(s) == 9:  # #RRGGBBAA → #RRGGBB
        return s[:7].lower()
    if len(s) == 7:  # #RRGGBB
        return s.lower()
    if len(s) == 4:  # #RGB → #RRGGBB
        return f"#{s[1] * 2}{s[2] * 2}{s[3] * 2}"
    return None


def _caldav_calendar_color(cal) -> str | None:  # type: ignore[reportUnusedFunction]
    """Best-effort fetch of the Apple iCal `calendar-color` property."""
    try:
        from caldav.elements.ical import CalendarColor

        props = cal.get_properties([CalendarColor()])
        if not props:
            return None
        for v in props.values():
            normalised = _normalise_hex_color(str(v) if v is not None else None)
            if normalised:
                return normalised
    except Exception:
        log.debug("calendar-color fetch failed for %s", cal.url, exc_info=True)
    return None


class CalDavBackend:
    def __init__(
        self,
        account_id: str,
        server_url: str,
        username: str,
        password: str,
    ) -> None:
        self.account_id = account_id
        self._server_url = server_url
        self._username = username
        self._password = password
        self._client: caldav.DAVClient | None = None  # type: ignore[reportGeneralTypeIssues]

    async def _get_client(self) -> caldav.DAVClient:  # type: ignore[reportGeneralTypeIssues]
        if self._client is None:
            resolved = await asyncio.to_thread(
                _discover_caldav_url,
                self._server_url,
                self._username,
                self._password,
            )
            self._client = await asyncio.to_thread(
                lambda: caldav.DAVClient(  # type: ignore[reportGeneralTypeIssues]
                    url=resolved,
                    username=self._username,
                    password=self._password,
                )
            )
        return self._client

    async def _run(self, fn, *args, **kwargs):  # type: ignore[reportReturnType]
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))  # type: ignore[reportReturnType]

    def _bad_server_response(self, exc: Exception) -> PermanentError:
        return PermanentError(
            f"CalDAV server at {self._server_url!r} did not return a valid "
            "XML response. Check that the URL points to a CalDAV endpoint "
            "(not the web UI), and that the username/password are correct. "
            f"(underlying error: {exc})"
        )

    @_classify_errors
    async def list_calendars(self) -> list[dict[str, object]]:
        client = await self._get_client()
        try:
            principal = await self._run(client.principal)
        except AttributeError as exc:
            # caldav.response._strip_to_multistatus tries `tree.tag` without
            # checking `tree is None`. That branch is reached when the server
            # body isn't XML at all (e.g., HTML 401 page from wrong URL/creds).
            if "tag" in str(exc):
                raise self._bad_server_response(exc) from exc
            raise
        return await self._run(
            lambda: self._fetch_calendars_with_colors(client, principal)
        )

    def _fetch_calendars_with_colors(
        self, client, principal
    ) -> list[dict[str, object]]:
        """Fetch calendar list AND colors in two PROPFINDs instead of N+2.

        `client.get_calendars()` already includes `calendar-color` in its
        depth-1 PROPFIND but discards it. We replicate the same two requests
        here and parse color alongside calendar metadata.
        """
        from caldav.collection import (
            _extract_calendar_home_set_from_results as _home,  # type: ignore[reportPrivateUsage]  # noqa: PLC0415
        )
        from caldav.collection import (
            _is_calendar_resource,  # type: ignore[reportPrivateUsage]  # noqa: PLC0415
        )

        # PROPFIND 1 (depth=0): get calendar-home-set URL from the principal.
        resp = client.propfind(
            str(principal.url),
            props=["{urn:ietf:params:xml:ns:caldav}calendar-home-set"],
            depth=0,
        )
        home_raw = _home(resp.results)
        home_url = (
            client._make_absolute_url(home_raw) if home_raw else str(principal.url)
        )

        # PROPFIND 2 (depth=1): calendar list + display names + colors in one shot.
        resp = client.propfind(
            home_url,
            props=client.CALENDAR_LIST_PROPS,  # already includes calendar-color
            depth=1,
        )

        result = []
        for item in resp.results or []:
            if not _is_calendar_resource(item.properties):
                continue
            url = str(item.href)
            if not url.startswith("http"):
                url = client._make_absolute_url(url)
            name = item.properties.get("{DAV:}displayname")
            raw_color = item.properties.get("{http://apple.com/ns/ical/}calendar-color")
            color = _normalise_hex_color(
                str(raw_color) if raw_color is not None else None
            )
            cal_id = url.rstrip("/").rsplit("/", 1)[-1] or url
            result.append(
                {
                    "id": cal_id,
                    "display_name": str(name) if name else cal_id,
                    "provider_id": url,
                    "color": color,
                }
            )
        return result

    def _events_to_changes(self, events, calendar_id: str) -> list[EventChange]:
        changes: list[EventChange] = []
        for ev in events:
            href = str(ev.url) if ev.url is not None else ""
            etag = ev.etag or ""
            try:
                vevents = _parse_vevents(ev.data)
            except Exception:
                log.exception("error iterating caldav event data for %s", href)
                continue
            for ve in vevents:
                try:
                    event = _vevent_to_event(
                        ve,
                        calendar_id=calendar_id,
                        href=href,
                        etag=etag,
                        user_email=self._username,
                    )
                except Exception:
                    log.exception("error mapping VEVENT for %s", href)
                    continue
                changes.append(EventChange(kind="upsert", event=event, uid=event.uid))
        return changes

    # ±1 year sync window mirrors `EventStore._instances_window_years = 1`.
    # We deliberately avoid `cal_obj.events()` (unbounded calendar-query REPORT)
    # because some servers — observed on Stalwart — return 507 Insufficient
    # Storage for the whole multistatus when a single stored event can't be
    # serialised by the server. A date-scoped `search()` produces a tighter
    # calendar-query and dodges the offending entries.
    _SYNC_WINDOW_DAYS = 365

    def _sync_window(self) -> tuple[datetime, datetime]:
        now = datetime.now(timezone.utc)
        return now - timedelta(days=self._SYNC_WINDOW_DAYS), now + timedelta(
            days=self._SYNC_WINDOW_DAYS
        )

    _INITIAL_SYNC_CHUNK = 250  # max EventChanges per yield to keep write-lock short

    @_classify_errors
    async def initial_sync(
        self, calendar_id: str
    ) -> AsyncIterator[tuple[list[EventChange], SyncCursor]]:
        client = await self._get_client()
        cal_obj = caldav.Calendar(client=client, url=calendar_id)  # type: ignore[reportGeneralTypeIssues]
        start, end = self._sync_window()
        events = await self._run(
            lambda: cal_obj.search(start=start, end=end, event=True, expand=False)
        )
        sync_token = await self._fetch_sync_token(cal_obj)
        changes = self._events_to_changes(events, calendar_id)
        if not changes:
            yield [], CalDavCursor(sync_token=sync_token)
            return
        chunk = self._INITIAL_SYNC_CHUNK
        for i in range(0, len(changes), chunk):
            is_last = i + chunk >= len(changes)
            yield changes[i : i + chunk], CalDavCursor(sync_token=sync_token if is_last else None)

    @_classify_errors
    async def incremental_sync(
        self, calendar_id: str, cursor: SyncCursor
    ) -> tuple[list[EventChange], SyncCursor]:
        sync_token = cursor.sync_token if isinstance(cursor, CalDavCursor) else None
        if sync_token and not sync_token.startswith("fake-"):
            client = await self._get_client()
            cal_obj = caldav.Calendar(client=client, url=calendar_id)  # type: ignore[reportGeneralTypeIssues]
            try:
                result = await self._run(
                    lambda: cal_obj.get_objects_by_sync_token(
                        sync_token=sync_token,
                        load_objects=True,
                        disable_fallback=True,
                    )
                )
            except (ReportError, DAVError) as e:
                raise CursorExpired(calendar_id) from e
            changes = self._sync_result_to_changes(result, calendar_id)
            return changes, CalDavCursor(sync_token=result.sync_token)

        # No real sync token — fall back to full date-windowed query.
        client = await self._get_client()
        cal_obj = caldav.Calendar(client=client, url=calendar_id)  # type: ignore[reportGeneralTypeIssues]
        start, end = self._sync_window()
        events = await self._run(
            lambda: cal_obj.search(start=start, end=end, event=True, expand=False)
        )
        changes = self._events_to_changes(events, calendar_id)
        return changes, cursor

    async def _fetch_sync_token(self, cal_obj) -> str | None:
        try:
            props = await self._run(
                lambda: cal_obj.get_properties([_dav_elements.SyncToken()])
            )
            token = props.get(_dav_elements.SyncToken.tag)
            return str(token) if token else None
        except Exception:
            log.debug("could not fetch sync-token for %s", cal_obj.url, exc_info=True)
            return None

    def _sync_result_to_changes(self, result, calendar_id: str) -> list[EventChange]:
        """Convert a SynchronizableCalendarObjectCollection delta into EventChanges.

        Objects with data → upsert; objects without data (404d/deleted) → delete.
        """
        changes: list[EventChange] = []
        for obj in result:
            if obj.data:
                try:
                    vevents = _parse_vevents(obj.data)
                except Exception:
                    log.exception("error parsing caldav delta object %s", obj.url)
                    continue
                href = str(obj.url) if obj.url is not None else ""
                etag = obj.etag or ""
                for ve in vevents:
                    try:
                        event = _vevent_to_event(
                            ve,
                            calendar_id=calendar_id,
                            href=href,
                            etag=etag,
                            user_email=self._username,
                        )
                    except Exception:
                        log.exception("error mapping delta VEVENT for %s", href)
                        continue
                    changes.append(
                        EventChange(kind="upsert", event=event, uid=event.uid)
                    )
            else:
                # Deleted: derive UID from the .ics filename in the URL.
                href = str(obj.url) if obj.url is not None else ""
                uid = href.rstrip("/").rsplit("/", 1)[-1]
                if uid.lower().endswith(".ics"):
                    uid = uid[:-4]
                if uid:
                    changes.append(EventChange(kind="delete", event=None, uid=uid))
        return changes

    @_classify_errors
    async def create_event(self, calendar_id: str, event: Event) -> Event:
        import dataclasses as _dc

        from lilical.backends._ical_serializer import event_to_vcalendar

        client = await self._get_client()
        cal_obj = caldav.Calendar(client=client, url=calendar_id)  # type: ignore[reportGeneralTypeIssues]
        ical_data = event_to_vcalendar(event).to_ical().decode()
        saved = await self._run(cal_obj.save_event, ical_data)
        href = str(saved.url) if saved and getattr(saved, "url", None) else None
        etag = getattr(saved, "etag", None) if saved else None
        return _dc.replace(
            event,
            provider_event_id=href or event.uid,
            etag=etag,
        )

    @_classify_errors
    async def update_event(
        self, calendar_id: str, event: Event, if_match: str | None
    ) -> Event:
        import dataclasses as _dc

        from lilical.backends._ical_serializer import event_to_vcalendar

        ical_data = event_to_vcalendar(event, sequence_bump=True).to_ical().decode()
        href = event.provider_event_id or f"{calendar_id}/{event.uid}.ics"
        client = await self._get_client()
        event_obj = caldav.CalendarObjectResource(client=client, url=href)  # type: ignore[reportGeneralTypeIssues]
        event_obj.data = ical_data
        if if_match:
            event_obj.etag = if_match
        await self._run(event_obj.save)
        new_etag = getattr(event_obj, "etag", None)
        return _dc.replace(event, etag=new_etag)

    @_classify_errors
    async def delete_event(
        self, calendar_id: str, provider_event_id: str, if_match: str | None
    ) -> None:
        client = await self._get_client()
        event_obj = caldav.CalendarObjectResource(client=client, url=provider_event_id)  # type: ignore[reportGeneralTypeIssues]
        await self._run(event_obj.delete)

    @_classify_errors
    async def update_instance(
        self,
        calendar_id: str,
        master_provider_id: str,
        recurrence_id_dt: datetime,
        event: Event,
    ) -> None:
        """Update one occurrence: append a VEVENT override to the master VCALENDAR."""
        import dataclasses as _dc

        from lilical.backends._ical_serializer import (
            event_to_vcalendar,
        )

        client = await self._get_client()
        event_obj = caldav.CalendarObjectResource(client=client, url=master_provider_id)  # type: ignore[reportGeneralTypeIssues]
        raw = await self._run(event_obj.get_data)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        master_cal = icalendar.Calendar.from_ical(raw)
        # Find and remove any existing override for this recurrence-id
        new_components = []
        for comp in master_cal.subcomponents:
            if comp.name == "VEVENT" and comp.get("RECURRENCE-ID") is not None:
                rid = comp.get("RECURRENCE-ID")
                rid_dt = rid.dt if hasattr(rid, "dt") else rid
                if (
                    isinstance(rid_dt, datetime)
                    and abs(
                        (
                            rid_dt.replace(tzinfo=None)
                            - recurrence_id_dt.replace(tzinfo=None)
                        ).total_seconds()
                    )
                    < 60
                ):
                    continue  # drop old override
            new_components.append(comp)

        # Build override VEVENT
        override = _dc.replace(event, recurrence_id=recurrence_id_dt, rrule=None)
        override_cal = event_to_vcalendar(override)
        override_ve = next(
            (c for c in override_cal.subcomponents if c.name == "VEVENT"), None
        )

        rebuilt = icalendar.Calendar()
        for comp in new_components:
            rebuilt.add_component(comp)
        if override_ve is not None:
            rebuilt.add_component(override_ve)

        event_obj.data = rebuilt.to_ical().decode()
        await self._run(event_obj.save)

    @_classify_errors
    async def delete_instance(
        self,
        calendar_id: str,
        master_provider_id: str,
        recurrence_id_dt: datetime,
    ) -> None:
        """Delete a single occurrence by appending an EXDATE to the master VCALENDAR."""
        client = await self._get_client()
        event_obj = caldav.CalendarObjectResource(client=client, url=master_provider_id)  # type: ignore[reportGeneralTypeIssues]
        raw = await self._run(event_obj.get_data)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        master_cal = icalendar.Calendar.from_ical(raw)
        for comp in master_cal.subcomponents:
            if comp.name == "VEVENT" and not comp.get("RECURRENCE-ID"):
                comp.add("exdate", recurrence_id_dt)

        event_obj.data = master_cal.to_ical().decode()
        await self._run(event_obj.save)
