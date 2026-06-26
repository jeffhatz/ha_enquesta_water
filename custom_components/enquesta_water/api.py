"""Client and parser for Enquesta/SilverBlaze Capricorn water usage pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
import ast
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

from .const import DEFAULT_BASE_URL, USER_AGENT

_LOGGER = logging.getLogger(__name__)


class EnquestaError(Exception):
    """Base Enquesta error."""


class EnquestaAuthError(EnquestaError):
    """Authentication failed."""


class EnquestaParseError(EnquestaError):
    """The portal response could not be parsed."""


@dataclass(frozen=True, slots=True)
class UsageReading:
    """A usage reading for a date or hour bucket."""

    bucket: str
    gallons: float


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """Parsed water usage snapshot."""

    meter_id: str
    daily_usage: list[UsageReading]
    hourly_usage: list[UsageReading]
    latest_day: date | None
    latest_day_gallons: float | None
    total_consumption_gallons: float | None
    daily_from: date | None
    daily_to: date | None


class EnquestaClient:
    """Small async client for the Enquesta Capricorn portal."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        username: str | None = None,
        password: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        meter_id: str | None = None,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self.username = username
        self.password = password
        self.base_url = normalize_base_url(base_url)
        self.meter_id = meter_id
        self._logged_in = False

    async def async_login(self) -> None:
        """Log in to the portal."""
        if not self.username or not self.password:
            raise EnquestaAuthError("Username and password are required")

        token: str | None = None
        login_referer = "/app/?"
        login_errors: list[str] = []
        for login_path in ("/app/?", "/app/login.jsp"):
            login_page = await self._request_text("get", login_path)
            try:
                token = _extract_csrf_token(login_page)
            except EnquestaParseError as err:
                login_errors.append(f"{login_path}: {err}")
                _LOGGER.debug("Enquesta login page at %s did not contain a CSRF token: %s", login_path, err)
                continue
            login_referer = login_path
            break

        if not token:
            raise EnquestaParseError("Login CSRF token was not found; " + "; ".join(login_errors))

        data = {
            "jspCSRFToken": token,
            "accessCode": self.username,
            "password": self.password,
            "nextPara": "",
            "nextPara_attr1": "",
        }
        response = await self._request_text(
            "post",
            "/app/capricorn?para=index",
            data=data,
            referer=login_referer,
        )

        if _is_login_page(response) or "Invalid" in response:
            raise EnquestaAuthError("Invalid Enquesta username or password")

        self._logged_in = True

    async def async_get_usage(self) -> UsageSnapshot:
        """Fetch and parse water interval usage."""
        if not self._logged_in:
            await self.async_login()

        daily_html = await self._request_text(
            "get",
            "/app/capricorn?para=smartMeterConsumV3&inquiryType=water&tab=WATSMCON",
        )
        if _is_login_page(daily_html):
            self._logged_in = False
            await self.async_login()
            daily_html = await self._request_text(
                "get",
                "/app/capricorn?para=smartMeterConsumV3&inquiryType=water&tab=WATSMCON",
            )

        daily = _parse_daily_usage(daily_html, self.meter_id)
        meter_id = self.meter_id or daily.meter_id
        latest_day = daily.latest_day
        hourly_usage: list[UsageReading] = []
        latest_day_gallons = daily.latest_day_gallons
        total_consumption_gallons = daily.total_consumption_gallons

        if latest_day:
            try:
                hourly_html = await self._request_text(
                    "post",
                    "/app/capricorn?para=smartMeterConsumV3&interval=hourlyUsage",
                    data=_hourly_form_data(daily_html, meter_id, latest_day),
                    referer="/app/capricorn?para=smartMeterConsumV3&inquiryType=water&tab=WATSMCON",
                )
                hourly = _parse_hourly_usage(hourly_html)
            except EnquestaError:
                _LOGGER.debug("Hourly usage parse failed; using daily chart only", exc_info=True)
            else:
                hourly_usage = hourly.hourly_usage
                if hourly.latest_day_gallons is not None:
                    latest_day_gallons = hourly.latest_day_gallons
                    daily.daily_usage[-1] = UsageReading(latest_day.isoformat(), latest_day_gallons)
                total_consumption_gallons = hourly.total_consumption_gallons

        return UsageSnapshot(
            meter_id=meter_id,
            daily_usage=daily.daily_usage,
            hourly_usage=hourly_usage,
            latest_day=latest_day,
            latest_day_gallons=latest_day_gallons,
            total_consumption_gallons=total_consumption_gallons,
            daily_from=daily.daily_from,
            daily_to=daily.daily_to,
        )

    async def _request_text(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        referer: str | None = None,
    ) -> str:
        """Make a portal request and return text."""
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": USER_AGENT,
        }
        if data is not None:
            headers["Origin"] = self.base_url
        if referer:
            headers["Referer"] = urljoin(f"{self.base_url}/", referer.lstrip("/"))

        async with self._session.request(
            method,
            urljoin(f"{self.base_url}/", path.lstrip("/")),
            data=data,
            headers=headers,
            raise_for_status=True,
        ) as response:
            text = await response.text()
            _LOGGER.debug(
                "Enquesta %s %s returned status=%s url=%s content_type=%s bytes=%s title=%r",
                method.upper(),
                path,
                response.status,
                response.url,
                response.headers.get("Content-Type"),
                len(text),
                _extract_title(text),
            )
            return text


@dataclass(frozen=True, slots=True)
class _ParsedDailyUsage:
    """Internal parsed daily usage."""

    meter_id: str
    daily_usage: list[UsageReading]
    latest_day: date | None
    latest_day_gallons: float | None
    total_consumption_gallons: float | None
    daily_from: date | None
    daily_to: date | None


@dataclass(frozen=True, slots=True)
class _ParsedHourlyUsage:
    """Internal parsed hourly usage."""

    hourly_usage: list[UsageReading]
    latest_day_gallons: float | None
    total_consumption_gallons: float | None


def _parse_daily_usage(html: str, meter_id: str | None = None) -> _ParsedDailyUsage:
    """Parse the daily usage chart from a Capricorn page."""
    parsed_meter_id = _extract_meter_id(html) or meter_id
    labels = _extract_axis_labels(html)
    usage = _extract_series_data(html, r"id\s*:\s*[\"']consumptionData")
    if not labels or not usage:
        raise EnquestaParseError(_parse_error("Daily usage chart was not found", html))
    if not parsed_meter_id:
        raise EnquestaParseError(_parse_error("Meter ID was not found", html))

    readings = [
        UsageReading(bucket=str(label), gallons=float(value))
        for label, value in zip(labels, usage, strict=False)
    ]
    latest_day = _parse_iso_date(readings[-1].bucket) if readings else None
    latest_gallons = readings[-1].gallons if readings else None

    return _ParsedDailyUsage(
        meter_id=parsed_meter_id,
        daily_usage=readings,
        latest_day=latest_day,
        latest_day_gallons=latest_gallons,
        total_consumption_gallons=_extract_total_consumption(html),
        daily_from=_extract_hidden_date(html, "dailyFromDate"),
        daily_to=_extract_hidden_date(html, "dailyToDate"),
    )


def _parse_hourly_usage(html: str) -> _ParsedHourlyUsage:
    """Parse the hourly usage chart from a Capricorn page."""
    labels = _extract_axis_labels(html)
    usage = _extract_series_data(html, r"id\s*:\s*[\"']consumptionData")
    readings = [
        UsageReading(bucket=str(label), gallons=float(value))
        for label, value in zip(labels, usage, strict=False)
    ]

    actual = _extract_series_data(html, r"name\s*:\s*[\"']Actual Reading[\"']")
    latest_gallons = float(actual[-1]) if actual else None
    return _ParsedHourlyUsage(
        hourly_usage=readings,
        latest_day_gallons=latest_gallons,
        total_consumption_gallons=_extract_total_consumption(html),
    )


def _hourly_form_data(html: str, meter_id: str, day: date) -> dict[str, str]:
    """Build the form data used by the hourly interval page."""
    from_date = _extract_hidden_date(html, "dailyFromDate") or day
    to_date = _extract_hidden_date(html, "dailyToDate") or day
    return {
        "para": "smartMeterConsumV3",
        "downloadConsumption": "",
        "userAction": "",
        "type": "hourly",
        "inquiryType": "water",
        "day": day.isoformat(),
        "dailyFromDate": from_date.isoformat(),
        "dailyToDate": to_date.isoformat(),
        "tab": "WATSMCON",
        "print": "N",
        "intervalDropdown": "hourlyUsage",
        "selectedMeterId": meter_id,
        "TOU_fromDate": _format_us_date(from_date),
        "month_from": "",
        "day_from": "",
        "year_from": "",
        "TOU_toDate": _format_us_date(to_date),
        "month_to": "",
        "day_to": "",
        "year_to": "",
    }


def _extract_csrf_token(html: str) -> str:
    """Extract the login CSRF token."""
    parser = _InputValueParser("jspCSRFToken")
    parser.feed(html)
    if parser.value:
        return parser.value

    for tag in re.findall(r"<input\b[^>]*>", html, re.I):
        if not re.search(r'\bname\s*=\s*["\']jspCSRFToken["\']', tag, re.I):
            continue
        match = re.search(r'\bvalue\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if match:
            return match.group(1)

    title = _extract_title(html)
    detail = f" on page {title!r}" if title else ""
    raise EnquestaParseError(f"Login CSRF token was not found{detail}; {_response_fingerprint(html)}")


class _InputValueParser(HTMLParser):
    """Find the value attribute for a named input."""

    def __init__(self, name: str) -> None:
        """Initialize parser."""
        super().__init__()
        self._name = name.lower()
        self.value: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Inspect input elements."""
        if tag.lower() != "input":
            return
        values = {key.lower(): value for key, value in attrs if value is not None}
        if values.get("name", "").lower() == self._name:
            self.value = values.get("value")


def _extract_meter_id(html: str) -> str | None:
    """Extract selected meter ID."""
    match = re.search(r'id=["\']selectedMeterId["\'][^>]*value=["\']([^"\']+)["\']', html)
    if match:
        return match.group(1)
    match = re.search(r'name=["\']selectedMeterId["\'][^>]*value=["\']([^"\']+)["\']', html)
    if match:
        return match.group(1)
    match = re.search(r"Meter ID:\s*([A-Za-z0-9_-]+)", html)
    if match:
        return match.group(1)
    match = re.search(r"\bmeterId=([A-Za-z0-9_-]+)", html)
    if match:
        return match.group(1)
    return None


def _extract_axis_labels(html: str) -> list[Any]:
    """Extract the populated xAxisLabelArray."""
    arrays = re.finditer(r"xAxisLabelArray\s*=\s*", html)
    labels: list[Any] = []
    for match in arrays:
        value = _literal_js_array_at(html, match.end())
        if value:
            labels = value
    return labels


def _extract_series_data(html: str, series_marker: str) -> list[float]:
    """Extract the data array following a series marker."""
    marker = re.search(series_marker, html)
    if not marker:
        return []
    data_match = re.search(r"data\s*:\s*", html[marker.end() :])
    if not data_match:
        return []
    return [float(value) for value in _literal_js_array_at(html, marker.end() + data_match.end())]


def _literal_js_array_at(text: str, start: int) -> list[Any]:
    """Parse a simple JavaScript array literal at or after start."""
    array_start = text.find("[", start)
    if array_start == -1:
        return []

    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(array_start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                literal = text[array_start : index + 1]
                try:
                    value = ast.literal_eval(literal)
                except (SyntaxError, ValueError) as err:
                    raise EnquestaParseError("Could not parse chart data") from err
                return value if isinstance(value, list) else []
    return []


def _extract_total_consumption(html: str) -> float | None:
    """Extract total consumption from chart subtitle."""
    match = re.search(r"Total Consumption of\s*([0-9,]+(?:\.[0-9]+)?)\s*GAL", html, re.I)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def _extract_hidden_date(html: str, name: str) -> date | None:
    """Extract an ISO date from hidden form fields."""
    patterns = (
        rf'name=["\']{re.escape(name)}["\'][^>]*value=["\']([^"\']+)["\']',
        rf'id=["\']{re.escape(name)}["\'][^>]*value=["\']([^"\']+)["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return _parse_iso_date(match.group(1))
    return None


def _parse_iso_date(value: str) -> date | None:
    """Parse an ISO date string."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _format_us_date(value: date) -> str:
    """Format date as MM/DD/YYYY for the portal form."""
    return value.strftime("%m/%d/%Y")


def _is_login_page(html: str) -> bool:
    """Return true if the response is the login page."""
    return "My Account Login" in html and "login-form" in html


def _extract_title(html: str) -> str | None:
    """Extract an HTML title for diagnostics."""
    match = re.search(r"<title[^>]*>\s*(.*?)\s*</title>", html, re.I | re.S)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _parse_error(message: str, html: str) -> str:
    """Build a parse error with page context."""
    title = _extract_title(html)
    if title:
        return f"{message} on page {title!r}"
    return f"{message}; {_response_fingerprint(html)}"


def _response_fingerprint(html: str) -> str:
    """Return a short response summary for diagnostics."""
    snippet = re.sub(r"\s+", " ", html).strip()[:180]
    return f"response_bytes={len(html)} response_start={snippet!r}"


def normalize_base_url(base_url: str) -> str:
    """Normalize a copied portal URL to a scheme and host."""
    parsed = urlsplit(base_url.strip() or DEFAULT_BASE_URL)
    if not parsed.scheme:
        parsed = urlsplit(f"https://{base_url.strip()}")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")
