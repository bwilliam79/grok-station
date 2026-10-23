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
