# Enquesta Water for Home Assistant

Custom integration for Enquesta/SilverBlaze Capricorn utility portals, built from the Amarillo PayAMA water usage flow.

## What it creates

- `sensor.enquesta_water_water_total`
- `device_class: water`
- `state_class: total_increasing`
- unit: `gal`

The portal exposes interval consumption instead of a physical lifetime meter register. To make the sensor suitable for Home Assistant long-term statistics, the integration keeps a small daily usage ledger in Home Assistant storage and exposes a synthetic monotonic total. The first successful update becomes the baseline; later Enquesta daily/hourly usage increases the total.

## Install

Copy `custom_components/enquesta_water` into your Home Assistant `custom_components` directory and restart Home Assistant.

Then add the integration from **Settings > Devices & services > Add integration > Enquesta Water**.

Fields:

- Username: your Enquesta portal username.
- Password: your Enquesta portal password.
- Portal base URL: defaults to `https://amocap.enquesta.io`.
- Meter ID: optional. Leave blank to use the selected water meter from the portal.

After the sensor has long-term statistics, add it under **Settings > Dashboards > Energy > Water consumption**.
