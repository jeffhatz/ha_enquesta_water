"""Constants for Enquesta Water."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "enquesta_water"
PLATFORMS = [Platform.SENSOR, Platform.BUTTON]
HOURLY_STATISTIC_ID = f"{DOMAIN}:hourly_usage_by_portal_hour"

CONF_BASE_URL = "base_url"
CONF_METER_ID = "meter_id"

DEFAULT_BASE_URL = "https://amocap.enquesta.io"
DEFAULT_SCAN_INTERVAL = timedelta(hours=1)
HISTORY_BACKFILL_DAYS = 365

STORAGE_KEY = f"{DOMAIN}.usage"
STORAGE_VERSION = 1

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 Safari/605.1.15"
)
