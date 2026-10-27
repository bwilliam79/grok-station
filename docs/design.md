# grok-station design system

One system for both surfaces of grok-station:

- **Station display**: the Pi framebuffer, 2560×1706 (code scales from a 1920×1080 spec, `s = 1.333`), read from 1 to 3 m.
- **Web admin**: the browser page, on laptop (designed at 1440×900, ≥ 1024) and phone (designed at 390×844, 360 to 430).

Theme name: **Hearth**. Warm near-black, real persona photos in circles, one teal accent, state rings.

## 1. Principles
1. **Calm at rest, clear in motion.** The idle screen is quiet. Only the thing that is happening now moves: one moving or glowing element per screen.
2. **The ring tells the state.** Teal listening, lavender working, warm white speaking. The same ring language appears in the admin (recording, training, testing).
3. **Readable from across the kitchen.** On the display, anything that matters is ≥ 85 px. In the admin, body text is ≥ 15 px.
4. **Color means something.** Each state color has one job (section 2). Never use a color just to decorate.
5. **Short copy that never overflows.** Say what happened and what to do next. If text doesn't fit, rewrite it. Don't clip it or shrink it below the floor.
6. **Real faces, no cartoons.** Personas are shown as their photo portraits, or as a monogram if there's no photo.
7. **Safe by default.** Secrets are write-only. Destructive actions need a confirm. Warnings stay visible until they're fixed.
8. **Same tokens everywhere.** The display and the admin share one palette and one motion vocabulary.

## 2. Colors and tokens
| Token | CSS variable | Hex | Role |
|---|---|---|---|
| bg | `--gs-bg` | `#0D0B09` | Screen/page background, input wells |
| bg-lift | `--gs-bg-lift` | `#1A1511` | Radial lift behind the display avatar |
| sidebar | `--gs-sidebar` | `#110E0B` | Admin sidebar, phone tab bar |
| surface | `--gs-surface` | `#17130F` | Cards, panels |
| surface-2 | `--gs-surface-2` | `#211B16` | Chips, selected nav, progress tracks, secondary buttons |
| surface-3 | `--gs-surface-3` | `#2A231C` | Slider tracks, hover |
| rule | `--gs-rule` | `#2E2620` | Borders, idle ring track |
| rule-2 | `--gs-rule-2` | `#3A3028` | Input and button borders |
| text | `--gs-text` | `#F5EFE6` | Primary copy |
| muted | `--gs-muted` | `#A89B8C` | Labels, help text, sub-lines |
| faint | `--gs-faint` | `#6F655A` | Decoration and admin group labels only, never key info |
| teal | `--gs-teal` | `#3FD3B6` | **Accent.** Listening, recording, success, primary buttons, focus, progress |
| teal-bg | `--gs-teal-bg` | `#12302A` | Teal tinted fills (badges, success toasts) |
| lavender | `--gs-lavender` | `#A99CFF` | Working or waiting: thinking, queued, training, saving, testing |
| lavender-bg | `--gs-lavender-bg` | `#221E3A` | Lavender tinted fills |
| warm | `--gs-warm` | `#FFE6BF` | Speaking; get-ready countdown; the admin's current-step marker |
| amber | `--gs-amber` | `#F5C04A` | Warnings: setup needed, offline, update ready; admin errors ("Needs attention") and destructive actions; ringing timer |
| amber-bg | `--gs-amber-bg` | `#33270F` | Banner fill |
| gold | `--gs-gold` | `#FFC83D` | The main bot's star (★) in the admin Bots list. Nothing else |
| red | `--gs-red` | `#FF5A52` | **Only** mic off and a home-feed stock-down arrow (▼). Never used in the admin (errors there are amber "Needs attention", §8.2) |
| stock-up | — | `#4ADE80` | Home-feed trend up only, always with an arrow. Not a status color |
| red-bg | `--gs-red-bg` | `#3A1513` | Mic-off fills only |
| night-text / night-dim | `--gs-night-text` / `--gs-night-dim` | `#9A7752` / `#5E4934` | Night clock on `#000000` |
| on-teal | `--gs-on-teal` | `#0D0B09` | Text on teal buttons |

Rules:
- Never use Plate orange `#F97316`.
- The only glow is the speaking ring.
- Display problems (too loud, didn't hear it, training failed) use **text** on a quiet **rule** ring, not red.
- Contrast: text and muted on bg or surface are ≥ 7:1.

## 3. Type
Fonts:
- **Inter Tight SemiBold (600)** for display text and headings.
- **Inter Medium (500)** for body text, with Medium Italic for quoting what the user said.
- **Admin values:** a monospace face (`ui-monospace`) for IPs, keys and versions.

### 3.1 Station display (px at 2560×1706)
| Role | Size | Font | Max |
|---|---|---|---|
| Idle clock | 400 (300 × s; 2026-10-22 refresh) | Tight 600 | fixed |
| Hero / headline | 160 | Tight 600, −2 px tracking, lh 1.02 | 1 line, ≤ 16 chars |
| Persona name | 160 | Tight 600 | 1 line, ellipsize |
| Status / progress line | 107 | Tight 600, teal | 1 line, ≤ 22 chars |
| Body / reply / sub-line | 85 to 101 | Inter 500, lh 1.18 | sub-lines 2 lines, ≤ 30 chars/line; replies auto-fit |
| Under-avatar state label | 85 | Tight 600, ring color | ≤ 12 chars |
| Top-bar clock | 96 | Tight 600 | fixed |
| Top-bar label | 72 | Inter 500, muted | ≤ 32 chars |
| Chip | 56 | Inter 500, muted | 1 line |

The floor is 56 px for anything, and 85 px for anything that matters. Auto-fit may shrink a line once, to 85% of its size. Below that, shorten the copy.

### 3.2 Web admin (px, laptop / phone)
| Role | Laptop | Phone | Font |
|---|---|---|---|
| Page title | 28 | 24 | Tight 600 |
| Card title | 18 | 17 | Tight 600 |
| Body / inputs | 15 | 16 (stops iOS zoom) | Inter 500, lh 1.45 |
| Help / meta | 13 | 13 | Inter 500, muted |
| Group label | 11, uppercase, +0.08em | 11 | Inter 500, faint |
| Mono value | 14 | 14 | ui-monospace |
| Button label | 14 | 16 | Inter 500 |

### 3.3 Admin type and spacing scale (draft, waiting on Noor's review)
All admin text sizes and spacing come from one `:root` block at the top of `admin/web/admin.css`. To change the scale, edit that block only. `tests/test_type_scale.py` fails on any raw `font-size`, any `font:` shorthand with a literal size outside that block, or any inline font style in `app.js`.

| Token | Value | Use |
|---|---|---|
| `--fs-help` | 13px | Help text, meta, pills, small buttons, group labels |
| `--fs-label` | 14px | Field labels, buttons, tabs, inputs |
| `--fs-body` | 15px | Body text (phones use 16px so iOS doesn't zoom) |
| `--fs-mono` | 13px | Monospace values (keys, fingerprints, instructions) |
| `--fs-mono-sm` | 10px | The certificate fingerprint, so all 95 characters fit on one line |
| `--fs-h3` | 18px | Card titles, tile values |
| `--fs-h2` | 22px | Dialog and sign-in headings |
| `--fs-h1` | 28px | Page and wizard titles |
| `--sp-1` … `--sp-6` | 4, 8, 12, 16, 24, 32px | Spacing steps |
| `--panel-pad` | `--sp-4` top, `--sp-2` bottom | Every bot-detail tab panel (`.cpanel`), so each tab's content starts at the same height |

The 3.2 table above is the source this scale was cut from. Where they differ, this block wins. Group labels and the sign-in headings moved onto the nearest step (13px and 22px).

## 4. Spacing, grid and safe margins
**Base unit:** 8 px (4 px for half steps). Tokens are `--gs-space-1` to `--gs-space-8`: 4, 8, 12, 16, 24, 32, 48, 64.

**Station display (2560×1706)**
- **Safe area:** 128 px left and right, 88 px top, 112 px bottom. Nothing crosses it, including ring halos and meters.
- **Top bar:** 96 px tall. The label sits left and the clock right.
- **Session layout:**
  - avatar column from x 213 to 880
  - text column from x 1067 to 2432 (content ≤ 1360 wide), vertically centered on the avatar
  - state label (y 1308) and level meter (y 1428, 120 tall) under the avatar
- **Full-width message layout:** text centered within the safe area.

**Web admin**
- **Laptop:**
  - 248 px sidebar
  - content up to 1080 px wide with 32 px gutters
  - 24 px gap between cards
- **Breakpoints:**
  - **768 to 1023 px:** the sidebar collapses to 72 px icons.
  - **Under 768 px:** phone layout, with a bottom tab bar, one column and 16 px gutters. Drawers become full screens and modals become bottom sheets.
- **Touch targets:** at least 44 px, and primary buttons 48 px on the phone.
- **Radii:** `--gs-radius-sm` 10 (inputs and buttons), `--gs-radius-md` 16 (cards), `--gs-radius-pill` 999.

## 5. Ring and avatar states
| Avatar | Display | Admin |
|---|---|---|
| Size | 667 px circle (500 × s), left edge x 213, centered at y ≈ 853 | 24 to 32 px in lists, 52 to 64 px on cards, 200 px on recording screens |

Photo treatment: v5 photo portrait, cover crop biased 30% from the top. Without a photo, a monogram on surface-2. A wake word with no persona uses the Echo mark (`assets/brand/mark.svg`, drawn on a surface-2 circle) as its avatar. The boot splash is the Echo Plymouth theme (`splash/echo.plymouth`, installed by `tools/install-splash.sh`).

| State | Ring color | Width (display / admin 200 px) | Motion |
|---|---|---|---|
| Idle / ready / problem | rule | 19 / 6 | static |
| Listening / recording | teal | 19 / 6 | pulse at 1 Hz: thickness +25%, brightness and soft glow, `--gs-ease-inout` |
| Thinking / queued / training | lavender, 270° arc on a rule track | 19 / 6 | spin, 1.2 s per turn, linear. Appears after 250 ms |
| Speaking | warm | 19 / 6 | glow and halo follow the output amplitude: 60 ms attack, 180 ms release |
| Get ready | warm arc on a rule track | 19 / 6 | arc fills over the 900 ms pre-chime pause, then hands off to listening |
| Chime / accepted | teal | 19 → 38, plus an 8 px halo at 35% | a single pulse, 300 ms, `--gs-ease-out`, synced with the chime sound |
| Success | teal | 19 / 6 | static |
| Setup needed / lock | amber | 19 / 6 | static |

The ring sits 15 px outside the photo on the display (`ring_w × 0.6 + ring_w/2` in code).

## 6. Motion tokens
| Token | Value | Use |
|---|---|---|
| `--gs-dur-instant` | 0 ms | Mic off (no animation in) |
| `--gs-dur-fast` | 150 ms | Hover, chime flash, toggle |
| `--gs-dur-base` | 240 ms | Fades, card rise (16 px), toasts in |
| `--gs-dur-chime` | 300 ms | Clip-accepted ring pulse |
| `--gs-dur-slow` | 600 ms | Crossfades (handoff, idle rotation) |
| `--gs-period-pulse` | 1000 ms | Listening pulse |
| `--gs-period-spin` | 1200 ms | Thinking spin |
| `--gs-enroll-prechime` | 900 ms | Enrollment: pause before each clip's chime ("get ready" arc fills over it) |
| `--gs-enroll-record` | 2400 ms | Enrollment: recording window per clip |
| `--gs-enroll-retry-hold` | 1500 ms | Enrollment: too loud / didn't hear it message hold before retry |
| `--gs-ease-out` | `cubic-bezier(.2,.8,.2,1)` | Things arriving |
| `--gs-ease-in` | `cubic-bezier(.4,0,1,1)` | Things leaving |
| `--gs-ease-inout` | `cubic-bezier(.45,0,.55,1)` | Loops (pulse) |
| `--gs-ease-linear` | `linear` | Spin, progress |

Rules:
- One moving hero at a time.
- Nothing flashes faster than 3 Hz.
- In night mode, nothing moves except the clock colon.
- Admin animations respect `prefers-reduced-motion`: loops become static rings and fades become 0 ms.

## 7. Iconography
- **Style:** outline icons on a 24 px grid, 1.8 px stroke (scaled up on the display), round caps and joins, `currentColor`. No emoji and no filled multi-color icons.
- **Sizes:**
  - Admin: 16 (inline), 20 (nav and buttons), 24 (card headers).
  - Display: 72 to 120 px, with the stroke scaled proportionally (about 6 px at 96).
- **Set:** Lucide, or anything that matches its geometry.
- **Core icons:** mic, mic-off, wifi, ethernet, globe (network), key, link (relay), clock and map-pin (time zone and location), audio-waveform (wake words), users (personas), volume-2, monitor (display), moon (night), download (updates), scroll-text (logs), activity (health), power (reboot), lock, check, x, alert-triangle, loader (spinner).
- **Weather (display only):** the shipped weather glyph set.

## 8. Component patterns

### 8.1 Station display
- **Top bar:** muted label at the left (for example "Wake word setup · Finn") and the 96 px clock at the right.
- **Status line:** one line at 107 px in the ring color, directly under the headline or sub-line. Never pinned to the screen bottom, and never full-width.
- **Progress:** a "Clip N of M" line, then a 1360 × 32 bar with radius 16, a surface-2 track and a teal fill of (N−1)/M. When everything is complete, the bar fills and the label becomes "Uploaded ✓".
- **Level meter:** 7 rounded bars, each 22 px wide with 16 px gaps and up to 120 px tall, refreshed at about 15 fps.
  - Teal while live.
  - All bars full in **text** color means too loud.
  - Flat **faint** bars mean nothing was heard.
- **Enrollment session:** 10 clips by default ("Clip 3 of 10", about 1 min). Training gate: recall ≥ 0.80 on held-out real clips and ≤ 1.0 false wakes per hour. If recall fails, an adaptive top-up of about 10 clips is offered. The station shows only a short pass/fail notice ("Hey Finn is ready" / "Needs 10 more clips"); detailed numbers live in the admin.
- **Notices:** the avatar with a quiet ring, a headline, and a 2-line sub-line that says what to do next.
  - Example-data or meta info goes in a 56 px chip above the headline.
  - Notices hold for 4 s unless an action is pending.
- **Full-screen message:** centered headline, auto-fit within the safe area, up to 2 lines.

### 8.2 Web admin
- **Navigation:**
  - **Laptop sidebar:** station name, an online dot and the hostname, then grouped items with an icon and label.
  - **Selection:** the selected item has a surface-2 fill and a 3 px teal left bar.
  - **Phone:** a bottom tab bar with 5 tabs, the last being More.
- **Cards:** surface fill, 1 px rule border, 16 radius, 20 to 24 padding. The 18 px title can have a status pill at the right. One topic per card.
- **Forms:**
  - Labels sit above fields, and help text goes below in muted.
  - Inputs are 38 px tall (44 on the phone), with a bg well, a rule-2 border and 10 radius.
  - Focus shows a teal border with a 3 px glow at 30%.
  - Errors show an amber border plus a "Needs attention" message underneath (no red).
- **Masked secret field (xAI key, relay token):**
  - **Write-only.** Once saved, the field shows `•••• last 4` and a "Saved" teal pill, with Replace and Test buttons. The value is never shown again.
  - **Replacing the key:** opens an empty field with paste and show/hide controls (show applies only while typing a new value), plus Save and Cancel.
- **Banners:**
  - **Width:** full content width, directly under the page header, with an icon, one line of text and an action.
  - **Amber** for warnings.
  - **Station offline:** a persistent amber banner on every page ("Can't reach the station. Retrying…") with the last "as of" time; controls are disabled until it reconnects.
  - **Amber "Needs attention"** for errors (no red), **teal** for success (dismissible).
- **Buttons:**
  - **Primary:** teal fill with on-teal text.
  - **Secondary:** surface-2 with a rule-2 border.
  - **Ghost:** text only.
  - **Danger** (e.g. Reboot, Remove bot): amber text/outline that opens a confirm; the confirm action is amber-fill. No red.
  - **Sizes:** 38 / 32 small, 48 on the phone. A button shows a spinner and lavender text while it's working.
- **Toasts:**
  - Bottom right on the laptop, top on the phone. Use a surface-3 fill and a 4 px left bar in the state color.
  - Auto-hide after 4 s, except errors (amber), which stay until dismissed.
  - **Save state indicator** (page header right): lavender "Saving…", then teal "All changes saved"; unsaved edits show an amber dot "Unsaved changes".
  - **Loading:** skeleton blocks in surface-2 at the shape of the content, never a bare "Loading…".
  - Never stack more than 2.
- **Tables:**
  - Row height 48, with a rule divider between rows, a muted 13 px uppercase header and mono for values.
  - Row actions sit at the right in a ⋯ menu.
  - On the phone, rows become stacked cards.
- **Wizard stepper:**
  - **Steps, in order:** admin password (required, first), Wi-Fi (optional when wired), xAI key, voice relay, time zone and location, wake words, audio.
  - **Laptop:** a numbered vertical rail. **Phone:** a horizontal progress bar with "Step 2 of 7".
  - **States:** done steps show a teal check, the current step a warm ring, and upcoming steps faint.
  - **Navigation:** Back and Next are pinned at the bottom of the screen. Next stays disabled until the step is valid, and optional steps show "Skip".
- **Toggles and sliders:**
  - Toggles are teal when on.
  - Sliders use a 6 px track with a teal fill, an 18 px knob, end labels and the value at the right.

### 8.3 Screen dimming
The idle screen can dim. **Off** leaves the backlight alone. **Schedule** uses two clock times (the window may cross midnight). **Sunrise** uses latitude and longitude on the station, NOAA math, no network, plus minute offsets. **Light sensor** reads a kernel illuminance file when one is already there; a missing sensor is normal and falls back to the schedule, then sunrise, then the day level.

Brightness is a percent on `/sys/class/backlight` when that file is writable. Otherwise the frame is copied and a black layer is drawn on the copy (same math as CSS opacity `(100 − level) / 100`). The working frame stays bright so partial updates do not punch a hole in the dim.

With the night-clock toggle on, schedule, sunrise, and full dark (at or below the night lux) use the night face: black, clock `#9A7752`, secondary `#5E4934`, clock 440 px @1080 (0.407 × short edge, 2026-10-22 refresh). Partial light keeps the day screen and only moves the brightness. The toggle off keeps the day screen.

The Display page saves mode, levels, times, and coordinates rounded to 2 decimals. A problem on that page is an amber **Needs attention** banner, not red. That includes a light sensor that is present but not reporting a reading.

## 9. Designing new features
New features are designed **to fit these principles and tokens first**. They don't arrive with their own look.
1. **Start from an existing pattern** in section 8, and reuse its tokens, type roles and safe area.
2. **Give the feature a state color** using the existing meanings in section 2. Do not add a new hue. If something truly has no fit, propose a token change to this document before designing.
3. **Write the copy first**, within the length limits. If it doesn't fit at the floor size, cut words.
4. **Check both surfaces.** If the feature shows on the station, define its display screen (ring state, headline and status line). If it's configured, define its admin card.
5. **Check before shipping:**
   - no text past the safe margins
   - only one moving element
   - colors used only for their meanings
   - the phone layout works at 360 px
   - reduced motion is respected
6. **Update this file** in the same change as the feature.

## 10. Display cards
Recipe, sports, calendar, and home cards use the surfaces, type, and teal accent in this document. Limits, rotation, and the signed path are in [cards.md](cards.md). Green `#4ADE80` is stock-up only, paired with an arrow. Stock-down reuses red, also with an arrow. A team logo is a monogram unless the admin allowlists an HTTPS host. Card text is data. It is never a command.

## 11. Notes vs. shipped code
- **Red mic-off color:** `#FF5A52` replaces the original mic-off `#FF4D4D`. It is mic off, and a home-feed down arrow. Not an admin error color.
- **Enrollment screen layout:** the shipped screen is full-width text with its status pinned to the bottom, which can overflow. It moves to the session layout (sections 4 and 8.1).
- **Top-bar label:** shipped at 75 px; this system uses 72.
