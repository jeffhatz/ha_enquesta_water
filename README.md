# Enquesta Water for Home Assistant

Custom integration for Enquesta/SilverBlaze Capricorn utility portals, built from the Amarillo PayAMA water usage flow.

## What it creates

- `sensor.enquesta_water_water_total`
- `device_class: water`
- `state_class: total_increasing`
- unit: `gal`
- `button.enquesta_water_backfill_hourly_history`
- external statistic: `enquesta_water:hourly_usage_by_portal_hour`

The portal exposes interval consumption instead of a physical lifetime meter register. To make the sensor suitable for Home Assistant long-term statistics, the integration keeps a small daily usage ledger in Home Assistant storage and exposes a synthetic monotonic total. The first successful update becomes the baseline; later Enquesta daily/hourly usage increases the total.

The integration also imports the latest available 24-hour usage chart into Home Assistant long-term statistics as `enquesta_water:hourly_usage_by_portal_hour`. It stores each portal hourly bucket as raw gallons at the portal's displayed hour label, so a statistics graph card can show a native hourly bar chart without relying on an image.

On first setup, the integration starts a missing-history backfill for up to 365 days of hourly portal data. It stops when Enquesta no longer returns a usable hourly chart, which lets newer accounts backfill as far as the account actually has history. You can run the same 365-day missing-history job again with the **Backfill hourly history** button entity. To choose a different range, call the `enquesta_water.backfill_hourly_history` action and set `days` to the number of days to try. Backfilled hourly statistics do not change the synthetic `water_total` sensor ledger, so the Energy dashboard total does not jump by a year of historical usage at the current time.

## Install

Copy `custom_components/enquesta_water` into your Home Assistant `custom_components` directory and restart Home Assistant.

Then add the integration from **Settings > Devices & services > Add integration > Enquesta Water**.

Fields:

- Username: your Enquesta portal username.
- Password: your Enquesta portal password.
- Portal base URL: defaults to `https://amocap.enquesta.io`.
- Meter ID: optional. Leave blank to use the selected water meter from the portal.

After the sensor has long-term statistics, add it under **Settings > Dashboards > Energy > Water consumption**.

For hourly usage, add a statistics graph card using the imported statistic:

```yaml
type: statistics-graph
title: Enquesta hourly water usage
stat_types:
  - mean
period: hour
days_to_show: 2
chart_type: bar
entities:
  - enquesta_water:hourly_usage_by_portal_hour
```
