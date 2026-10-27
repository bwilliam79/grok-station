# Display: supported screen sizes

How the station screen handles different sizes, taken from the code.

## How it draws

- `app/grok_station.py` renders with pygame into an off-screen surface and writes it to the Linux framebuffer (`/dev/fb0`, or `GROK_STATION_FB`). It doesn't use a browser, X or Wayland.
- At startup it reads the size and color depth from `/sys/class/graphics/fb0` (`virtual_size`, `bits_per_pixel`, `stride`). Whatever mode the Pi's firmware or KMS picked is the size it uses. grok-station doesn't set a resolution: `tools/boot-config.py` only adds quiet-boot options to `cmdline.txt` and leaves `config.txt` alone.
- The layout is designed at 1920x1080 and scaled by the limiting side (`min(h/1080, w/1920)`). Screens taller than 16:9 get the extra height placed relative to the real edges. Icons are never smaller than 28 px.

## Supported range

`MIN_SIZE, MAX_SIZE = (800, 480), (3840, 2160)` in `app/grok_station.py`:

| Screen | What happens |
|---|---|
| 800x480 to 3840x2160 | Fills the whole screen |
| Bigger than 3840x2160 on either side | Centered 3840x2160 picture with black borders (letterboxed) |
| Smaller than 800x480 | Draws at the native size on a best-effort basis and logs a warning; things may not fit |

## Tested

The automated render tests (`tests/test_display.py`, `tests/test_timers.py`) draw every screen at:

- 800x480
- 1024x600, 1024x700, 1024x768
- 1280x720, 1280x800
- 1920x1080
- 2560x1706
- 3840x2160
- 5120x2880 (letterbox check)

They check that the layout fits. Not every size has been tried on a real panel.

## Boot splash

`splash/echo.script` uses `splash-800x480.png` on screens up to 1024x600 and the 1080p art on everything else. Both are scaled to fit.

## Untested or not supported

- **Portrait or rotated screens.** The code has no rotation handling. A rotation set in the OS changes the framebuffer size the station reads, but that hasn't been tested.
- **Touch.** There's no touch or mouse input. A touchscreen works as a plain screen.
- **More than one screen.** Only one framebuffer is used.

## Admin Display page

The admin page groups the controls under four headings:

- **Clock and night clock.** The night-clock switch, and Night clock from / until.
- **Screens.** Message hold time, card rotation, and fade between screens.
- **Brightness.** Mode, day and night levels, and Dim the screen from / until, plus sunrise offsets and coordinates.
- **Units.** Temperature.

**Advanced: team logo sources** is the last block. The two live previews, day and night, are the same size and shape, stacked, with a label under each.

Night clock hours and the dim window are different settings. They were not merged in 0.11.2.

| Control | Setting | What it does |
|---|---|---|
| Night clock from / until | `GROK_STATION_NIGHT`, whole hours such as `22-6` | When the black night clock is drawn, and only while Brightness is Off. Empty turns that clock off. `night_face` in `app/grok_station.py`. |
| Dim the screen from / until | `GROK_STATION_DIM_FROM` and `GROK_STATION_DIM_TO`, times such as `22:00` | When Schedule lowers the backlight. `select_level` in `app/dimming.py`. |

While Brightness is Schedule, Sunrise and sunset, or Light sensor, and the night clock is on, the night clock follows that brightness window instead of its own hours. `in_dim_period` in `app/dimming.py` decides that window: the dim times for Schedule, sunset to sunrise for Sunrise, and a dark reading for a light sensor. Brightness Off leaves the backlight alone, and the night clock uses its own hours. See [dimming.md](dimming.md).

## Tiles during a conversation (0.12.6)

A score, recipe or calendar tile a bot puts up during a conversation stays on screen while the bot listens, thinks or speaks. A small status badge sits top left on the top-bar line: the bot's own avatar with the same state ring as the full-screen status, scaled down (52 px avatar, 5 px ring at 1920x1080), and the bot's name in place of the tile's caption (Sports, the recipe title or the date). With no conversation the badge is hidden and the caption is back. A bot with no picture shows the same monogram the full-screen status uses.

The tile clears when the bot starts its next response if you asked a new question or the tile's hold ran out; it is never pulled while the station is listening. A response that puts up its own tile replaces the old one.
