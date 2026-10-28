# Display: supported screen sizes

How the station screen handles different sizes, taken from the code.

## How it draws

- `app/grok_station.py` renders with pygame into an off-screen surface and writes it to the Linux framebuffer (`/dev/fb0`, or `GROK_STATION_FB`). It doesn't use a browser, X or Wayland.
- At startup it reads the size and color depth from `/sys/class/graphics/fb0` (`virtual_size`, `bits_per_pixel`, `stride`). Whatever mode the Pi's firmware or KMS picked is the size it uses. grok-station doesn't set a resolution: `tools/boot-config.py` only adds quiet-boot options to `cmdline.txt` (and refuses to touch one with no `root=` option or with more than one non-empty line, since the Pi reads only the first line and there is no backup). A failure before the file is renamed into place leaves `cmdline.txt` untouched; a failure after it (syncing the folder) means the change is in place, so it still prints `reboot` (install.sh sets its reboot flag) and then exits nonzero and leaves `config.txt` alone.
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

## Bottom zone (0.12.16)

Only a recipe takes the whole screen. A sports or calendar tile, and the bot's listening, thinking, replying and error states, use the bottom zone: the band under the clock, date and weather, which never move or shrink. At 2560x1706 the zone runs x 128 to 2432 and from about y 800 down to the safe area; it scales with the layout on other screens (`zone_rect` in `app/grok_station.py`). When timer chips show, the zone's bottom moves up above them (`content_limit`). Home cards under the date are hidden while the zone is in use.

- **Bot states.** The zone card holds the avatar on the left, then the bot's name, the heard line and the reply. The state is an icon and a word at the top right in the ring colour: bars and "Listening", three dots and "Thinking", a speaker and "Replying". Plain text from `/show`, wake-word enrollment, the list read-back card, and a reply that carries an inline weather, time or conversion card keep their full-screen layouts.
- **A tile while the bot is active.** The tile takes the zone. The bot shrinks to the upper-left status: avatar, name, then the same icon and word. The reply text is hidden while the tile is up, with no caption; speech keeps playing. When the tile goes, the bot expands back into the zone.
- **Reply window.** The reply shows in a 4-line window (85 px text at 2560x1706). A reply that fits does not move. A longer one scrolls smoothly like a teleprompter, keeping the line being spoken at about line 2, with 60 px fades at the top and bottom edges. When speech ends it settles on the last 4 lines without fades, and then the normal hold applies. Barge-in or a new tile stops it in place. The pace comes from the wake listener: while a reply plays it posts `{"speech": {"p": 0..1}}` to `/state` five times a second, the time since the first audio chunk over the audio received so far (24 kHz PCM16; the voice pipeline has no per-word timings). With no progress (for example a late bot answer) the window waits 1.5 seconds and then scrolls at 60 px a second. `GROK_STATION_REDUCE_MOTION=1` pages 4 lines at a time with a 200 ms cross-fade instead; there is no admin switch for it yet.
- **Recipe chips.** A sports or calendar tile that arrives while a recipe is up does not replace it. It shows as a chip in the top bar left of the clock (LIVE dot, league, `HC 3 – 2 RV`; a calendar chip reads `Next 6:30 · Practice`). Saying "dismiss" or "clear that" removes the newest chip; with no chip up those words go to the bot as usual. A chip ends on its tile's own ttl. When the recipe ends, the newest chip with ttl left moves into the bottom zone. At most two chips show, newest nearest the clock; a third replaces the oldest. The station has no touch input, so a tap does nothing yet (`chip_at` is the hit test a pointer would use).
- **Contrast.** The league label on the score tile is MUTED (6.80:1 on the card) instead of FAINT (3.24:1).

## Tiles during a conversation (0.12.6)

Since 0.12.16 sports and calendar tiles use the bottom zone (above) and only a recipe is full screen; the rules below still decide when a tile stays or clears.

A score, recipe or calendar tile a bot puts up during a conversation stays on screen while the bot listens, thinks or speaks. A small status badge sits top left on the top-bar line: the bot's own avatar with the same state ring as the full-screen status, scaled down (52 px avatar, 5 px ring at 1920x1080), and the bot's name in place of the tile's caption (Sports, the recipe title or the date). With no conversation the badge is hidden and the caption is back. A bot with no picture shows the same monogram the full-screen status uses.

The tile clears when the bot starts its next response if you asked a new question or the tile's hold ran out; it is never pulled while the station is listening. A response that puts up its own tile replaces the old one. A reply and its card arrive as two posts: the display holds the reply text for about 1.5 seconds (`TEXT_CARD_WAIT`) so a card from the same persona can take the screen without flashing the text first. Spoken audio is not held. When the conversation goes idle, that conversation card returns to the clock about 60 seconds later (a recipe waits longer; see cards.md). `ttl_s` is only the upper bound, so a long ttl cannot leave a score on the clock after the conversation is over. Cards from different turns do not pile up. While a recipe card is up, “next step”, “back”, “repeat”, “start over”, and “done” are handled on the station and read in the card owner’s voice. They work in the follow-up window and after a wake word once the session has ended but the card is still up. A question that merely contains those words still goes to the bot.

## Waiting tone (0.12.7)

The thinking ring is the station's one "thinking" state. A soft two-note chime plays with that ring, from the end of the wait that puts the ring up until the first spoken audio. It waits about 0.4 seconds so a fast answer does not blip, then repeats about every 2.5 seconds. Speech, including an asking clip while the ring stays up, stops it immediately. Audio has a Waiting tone switch (on by default). Night and dim quiet settings make it quieter. See [operations.md](operations.md).
