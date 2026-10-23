# Screen dimming

The display can lower its brightness at night. Nothing is required: with the mode left at **Off**, the station does not touch the backlight.

## Modes

| Mode | When it dims |
|---|---|
| Off | Never. The backlight is left as it is. |
| Schedule | From a clock time until another, including a window that crosses midnight. `06:00` is the first bright minute. |
| Sunrise and sunset | From sunset until sunrise. Times are computed on the station (NOAA formula, standard library only, no network) from latitude and longitude, plus a minute offset on each. |
| Light sensor | An ambient light sensor the kernel already exposes. Between 20 and 80 lux the level moves linearly from the night percent to the day percent. At or below 20 lux the idle screen uses the night clock, if that toggle is on. |

Auto-sensor with no reading falls back to the schedule if both times are set, otherwise sunrise if a location is set, otherwise the day level. The Display page says **Needs attention** in amber. Red stays reserved for the microphone off icon and stock that is down.

Coordinates on this page are stored to 2 decimal places. Leave both blank to use the station location, still rounded to 2 decimals. One of the two is not enough.

## Hardware

Probing is read-only and optional. A missing sensor is normal.

- IIO illuminance: `/sys/bus/iio/devices/*/in_illuminance*` (processed input, otherwise raw times scale). Names matching bh1750, tsl2561, or veml7700 are recognized.
- I2C: a `name` file for those chips, or one read byte on `/dev/i2c-*` at the chip's usual address. The station never writes a register. A name with no illuminance file is not a reading. Opening `/dev/i2c-*` needs the `i2c` group; the display service is not put in that group, because a missing group would stop the service. The illuminance file is the path that supplies lux.
- Brightness: `/sys/class/backlight/*/brightness` when it is writable. The group `video` is enough on the usual Pi backlight.

If the write fails, or there is no backlight, the framebuffer is copied and a black layer is composited on the copy. Opacity is `(100 − level) / 100`, the same as a CSS black overlay. Partial updates (the clock colon, the timer strip, the thinking ring) use that copy too, so an undimmed rectangle cannot punch through.

The night clock (black, `#9A7752`, `#5E4934`) follows the dim window while the night-clock toggle is on. Toggle off and the day screen stays up. Mode **Off** keeps the old hour window for that clock.

Settings live in `/etc/grok-station/env` (`GROK_STATION_DIM_MODE`, `DIM_DAY`, `DIM_NIGHT`, `DIM_FROM`, `DIM_TO`, `DIM_LAT`, `DIM_LON`, `DIM_SUNRISE`, `DIM_SUNSET`). Saving restarts the display. The admin journal records `action=dim-schedule` (or `dim-sun`, `dim-auto-sensor`, `dim-off`) and does not record the coordinates. A stamp in `/var/cache/grok-station/dim-level` remembers that this app lowered the backlight, so **Off** can put it back. The stamp is not in `/run`, which is wiped on restart.

## Validation on the Pi

What changed: Display has a Brightness card (off, schedule, sunrise, light sensor), day and night levels, dim times, sunrise offsets, and latitude/longitude to 2 decimals. The screen uses the backlight file when it can, otherwise a dark layer. The night clock follows that window when its toggle is on.

How to test:

1. On the admin page, open **Display**. Set Brightness to **Schedule**, dim from `22:00` until `06:00`, day 90, night 20. Save. The screen restarts.
2. `journalctl -u grok-station -n 30 --no-pager` shows `dim backlight 90` during the day, or `dim overlay 90` when the backlight file is not writable. Inside the window the number is 20.
3. With **Night clock** on, the idle screen in that window is the brown clock on black. Toggle it off and the day screen stays, still at the night brightness.
4. Set Brightness to **Off** and save. If this app had dimmed the backlight, it returns to full. The night clock goes back to its own hours.
5. Optional. If `in_illuminance` exists under `/sys/bus/iio/devices/`, choose **Light sensor**, cover the sensor, and the level drops. With no sensor, or a chip that is not reporting illuminance, the page shows an amber **Needs attention** banner and uses the schedule, then sunrise, then the day level. `journalctl -u grok-station` then has one `dim no-sensor` line.
6. **Sunrise** with both fields blank uses the station location rounded to 2 decimals. A latitude of `91` is rejected before anything is written, and the message is amber, not red.

Expected: the new keys are in `/etc/grok-station/env` (still `root:grok`, mode `0640`). `journalctl -u grok-station-admin` has `action=dim-schedule` (or the mode you saved) and does not contain the coordinates.
