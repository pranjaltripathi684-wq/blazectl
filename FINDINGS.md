# EvoFox Blaze Ultra: protocol findings

*Copyright © 2026 Pranjal Tripathi · part of [blazectl](README.md), GPL-3.0-or-later. Please credit the source if you reuse these findings.*

What is known about the mouse's protocol, with the evidence for each item. All findings come from the
author's own mouse, between 2026-11-21 and 2026-11-22.

## Status

| Feature | Status | Command |
|---|---|---|
| Lighting (12 modes, colour per DPI level) | ✅ working | `light` |
| Memory dump / backup | ✅ working | `dump --save` |
| DPI (all 23 values, 6 levels) | ✅ decoded + verified | `dpi --set ... --execute` |
| Polling rate (125/250/500/1000) | ✅ decoded + verified with a tester | `poll --rate N --execute` |
| Buttons 1-7: 12 functions | ✅ decoded; codes match the official app's bytes | `buttons N=function --execute` |
| Button 8 | ✅ only rapid-fire / dpi-loop work (firmware limit) | |
| GUI | ✅ tkinter, tested on Windows-style Python + simulated mouse | `gui` |
| Linux | ✅ code + udev rule ready; awaiting first real-hardware run | `scripts/linux-setup.sh` |
| Macro | ✅ decoded; writer matches the app byte for byte; awaiting real-hardware confirmation | `macro set/clear`, GUI Macro tab |
| Media keys, Shortcuts | ✅ decoded; GUI/CLI output identical to the official app | `buttons 4=media:volume-up 5=shortcut:ctrl+c` |
| Profiles Selection | ⏳ not decoded | |

## Device

- USB ID **30fa:1440**, "INSTANT USB Gaming Mouse". VID 0x30FA = Instant Microelectronics.
- Control channel: interface 1, usage page **0xFF01**, **feature report 0x07, 8 bytes**. Event channel: usage page 0xFF00, input report 0x06.
  - On Windows each collection is its own HID path, and the `hidN` numbers change between runs. On Linux, interface 1 is a single hidraw node containing all collections. blazectl finds the node by its feature report 0x07.
- Header `5a 03 a5 40 14 fa 30 10` (PID/VID little-endian).

## The official EvoFox app

- Tabs: Button, Macro, Performance, Backlight. Profiles: Office, Game I, Game II, Media. Import / Export / Reset. "Music-lighting" toggle.
- **The app does NOT read the mouse.** It shows its own saved profile. **Apply overwrites the mouse** with that profile (DPI, polling, buttons).
- **When it's running in the tray, it seems to re-apply its profile after the mouse reloads**, undoing other tools' changes. Evidence: after `buttons 8=back`, the next listing showed buttons 7 and 8 back at the app's values. Keep it closed.
- Button menu: Left Click, Middle Click, Right Click, Backward, Forward, DPI Loop, Extend » (Show Desktop, Double Click, Rapid Fire, LED Loop, DPI +, DPI −), Media », Macro », Shortcut, Profiles Selection ».

## Protocol (same family as other Instant mice)

Sources: OpenRGB InstantMouseController (constants), julianfdz/open-isy4500-customizer (MIT, verified on 30fa:1040).

- Lighting mode: `07 13 FF MS DB 0L M0 00`. Colour: `07 14 KG RB 00 00 00 00` (K = 2 x DPI level, inverted 4-bit RGB).
- Memory read: `07 18 05 i addrL addrH 00 len-1`, then GET_FEATURE (byte 1), then `07 18 00 ...`.
- Memory write: `07 18 03 i addrL addrH val len-1` per byte, then `07 18 09 00 addrL addrH 00 len-1`, then idle.
- Reload: `07 18 10 ...`. This applies memory and resets RAM lighting.

## Buttons: block 0x08 bytes 0-7 + block 0x10 byte 0

- Slot byte = flags (top 3 bits, preserved) | function code (low 5 bits).
- **Function codes** (they follow the app's menu order, NOT the reference mouse's naming):

| code | function | evidence |
|---|---|---|
| 01 | Left Click | factory on left button |
| 02 | Middle Click | blazectl/app write; it closed a browser tab |
| 03 | Right Click | factory on right button |
| 04 | Backward | factory on rear side button; goes Back |
| 05 | Forward | factory on front side button |
| 06 | DPI Loop | app write (btn3 22 → 26; btn8 e9 → e6, works) |
| 07 | Show Desktop | app write (btn5 64 → 67) |
| 08 | Double Click | app write (btn3 22 → 28) |
| 09 | Rapid Fire | factory on button 8 |
| 0B | DPI + | app write (btn7 b5 → ab) |
| 0C | DPI − | app write (btn6 66 → 6c) |
| 15 | LED Loop | app write (btn7) / factory |

- **Slot ↔ app button:**

| app button | physical | slot | factory byte |
|---|---|---|---|
| 1 Left | left | 0x08[0] | 21 |
| 3 Middle | wheel | 0x08[1] | 22 |
| 2 Right | right | 0x08[2] | 63 |
| 5 Backward | rear side | 0x08[3] | 64 |
| 4 Forward | front side | 0x08[4] | a5 |
| 7 LED Loop | behind wheel, lower | 0x08[5] | b5 |
| none | none | 0x08[6] | 00 |
| 6 DPI Loop | behind wheel, upper | 0x08[7] | 66 |
| 8 Rapid Fire | top-left red | 0x10[0] | e9 |

- **Button 8 limitation:** the app writes e4 for Backward, but it does nothing on the mouse. e6 (DPI Loop) works. blazectl allows only rapid-fire and dpi-loop there. In extend.json, button 8 held f5 (LED Loop); its behaviour there is untested.
- 0x44, 0x50, 0x60 hold `21 22 23 24 25 35 00 26 29`: probably the other profiles' button tables.
- blazectl's encoder reproduces every app-written byte exactly (unit tests).

## DPI: block 0x10 bytes 1-7

- 6 levels; low nibbles in bytes 5-7 (two per byte, even level in the high nibble); bit 4 of each code in byte 3 (bit i = level i). Byte 0 = button 8; byte 4 and the top bits of byte 3 are preserved.
- App level N = slot N.
- **Code → DPI (23 codes):** 1-10 → 200…2000 (step 200); 11 does not exist; 12 → 2400; 13-23 → 3200…11200 (step 800); 24 → 12800.
- Evidence: anchors from dumps, the app's slider stops, and a verification dump (app 3200 4000 4800 8000 9600 11200 → `e9 00 00 38 08 de f3 57` = [13,14,15,19,21,23]).

## State block 0x18

- Byte 0 low 3 bits = active DPI level index (it tracked the active level in every dump). blazectl writes it (`dpi --active N`, or clicking a level in the GUI), keeping the other bits, then reloads the mouse. First real-hardware check pending. Byte 1 = 0x3f enabled-levels mask; blazectl refuses to activate a level whose bit is clear.
- **Byte 6 = polling = (1000 / rate) − 1**: 1000 → 00, 500 → 01, 250 → 03, 125 → 07. Found by diffing 1 KB dumps (the only changed byte); verified with an online polling tester (1000 / 250 / 500 / 123 Hz).
- 0x098-0x3FF is all zeros.

## Memory 0x00-0xFF (factory/original settings)

```
0x00: 5a 03 a5 40 14 fa 30 10   header
0x08: 21 22 63 64 a5 b5 00 66   buttons
0x10: e9 00 00 30 08 35 c9 28   [0]=button 8; DPI 600 1000 2400 1800 7200 12800
0x18: 19 3f 32 00 9a ff 01 00   state (byte 6 = polling, 01 = 500 Hz)
0x20: 00 10 ff ff 8f 00 00 7f
0x28: 70 ff 00 bf 00 ff ff ff
0x30: 00 00 ff 3f 00 00 00 00
0x38: 00 00 4b 00 00 00 8f 00
0x40: 00 00 00 6e 21 22 23 24
0x48: 25 35 00 26 29 00 00 00
0x50: 21 22 23 24 25 35 00 26
0x58: 29 00 00 00 00 00 00 00
0x60: 21 22 23 24 25 35 00 26
0x68: 29 00 00 00 00 00 00 04
0x70: 08 04 10 04 18 04 40 71
0x78: 00 20 04 a0 0f 51 01 00
0x80: 00 18 94 49 09 14 24 09
0x88: 13 0a 09 08 00 00 00 00
0x90: 00 3d 07 07 00 00 00 00
0x98-0x3ff: 00
```

## Macros (decoded 2026-11-22 from 7 dumps: m_base, m_ab, m_two … m_six)

| | |
|---|---|
| Button function | **0x0D** = macro, play N times · **0x0E** = macro, repeat while held |
| Macro data | from **0x400**, back to back, one copy per distinct macro (shared by buttons) |
| Macro format | `[repeat, 00]` + 2-byte pairs + `00 00` |
| Pair (T, K) | T bit 7 = key down, T bits 0-6 = delay after the event in ms; K = HID keyboard usage |
| Long delays | event keeps `delay % 127`, then one `7f 00` filler pair per 127 ms |
| Per-button entry | 10 bytes at **0xA1 + 10 × slot**: `[pairs \| 0x80 if hold, ff, 00, 00, addrL, addrH, 00, 0a, 00, 00]`; all zero = no macro |
| Trailing pointers | LE words at 0x6e, 0x70, 0x72, 0x74, 0x79 = end-of-macros + 0, 8, 16, 24, 32 (0x400… with no macros) |

Evidence:

- m_ab: A↓110 A↑2232 KPEnter↓90 KPEnter↑976 B↓80 B↑20 on button 7 → `01 00 ee 04 49 04 7f00×17 da 28 57 28 7f00×7 d0 05 14 05 00 00`.
  KeypadEnter was stored as 0x28 (main Enter).
- m_two: re-recording replaced the macro with C; buttons 6 and 7 both got an entry pointing at the single copy at 0x400.
- m_three: cycle times 3 → header byte `03`; unbinding button 7 zeroed its entry (slot 5, 0xD3).
- m_four: "until released" → button code 0x0E, entry byte 0 bit 7, header back to 1; button 4 (slot 4) entry at 0xC9.
- m_five: "until clicked again" alone was never written by the app (dump identical). Not supported.
- m_six: a second macro (D) went to 0x408, its entry address = 0x0408, pointers moved +8.
- blazectl's writer reproduces the app's changes base→ab, two→three, three→four and five→six **byte for byte**
  (tests/test_macro.py).
- Slots 1, 2 (buttons 3, 2) are predicted from the spacing; slots 3, 4, 5, 7 are confirmed (slot 3 by the shortcut in m7_ms).
- Data before the macros at 0x400 (5 small records) is what the trailing pointers point at; the app leaves stale bytes
  there after moving the pointers, and so does blazectl.

## Media keys and shortcuts (m7_base vs m7_ms, 2026-11-22)

App change: button 4 → Media → Volume Up, button 7 → Media → Play/Pause, button 5 → Shortcut → Ctrl + C.

| | button code | entry (10 bytes at 0xA1 + 10 × slot) | data |
|---|---|---|---|
| Media | **0x0F** | `00 ff uL uH aL aH 00 00 00 00`: u = USB HID consumer usage (Volume Up `e9`, Play/Pause `cd`), a = next free data address | none |
| Shortcut | **0x0E** (like a hold macro) | `82 ff b7 00 aL aH 00 00 00 00`: pairs \| 0x80; byte 2 = `b7`, byte 7 = `00` (a recorded hold macro has `00`/`0a`) | `01 00` + one key-down per key, 4 ms apart (`84 e0 84 06` = Ctrl↓ C↓), `00 00` |

- The app lays everything out in ascending button-slot order, storing each distinct data blob once. This rule also
  reproduces all six macro dumps, so blazectl uses it everywhere.
- Shortcut modifiers are stored in the order Ctrl, Shift, Alt, Win (left-hand keys E0-E3), then the key.
- The media list in the app: Browser, Media Player, Mail, Volume Up, Volume Down, Previous Track, Next Track, Mute, and
  more below. The codes are the standard USB HID consumer ones: e9 and cd from the dump; **Browser (0x196, a 16-bit
  code) and Mute (e2) written by blazectl and tested on the real mouse** (2026-11-22). Others: Mail 0x18A,
  Media Player 0x183, Calculator 0x192, My Computer 0x194, Next b5, Previous b6, Stop b7, Vol− ea.
- The baseline dump still held the "hi" macro blazectl wrote for the button-5 test (at 0x410), which confirms a
  blazectl-written macro survived on the real mouse.
- Clicking the same three settings in blazectl's GUI produces memory identical to the app's dump (outside the DPI/state
  bytes the app also rewrites).

## Open

- Macro mode "cycle until clicked again". Profiles Selection. (probably use the 0x20-0x9F areas).
- Profiles (Office / Game I / Game II / Media) and Music-lighting.
- Whether Mouse Sensitivity / Scroll Speed / Double Click Speed in the app are Windows settings (likely) or mouse memory.
