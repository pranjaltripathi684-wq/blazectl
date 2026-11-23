# Getting the Blaze Ultra into OpenRGB and libratbag/Piper

Status as of November 2026. Both projects were checked at their current main branch.

## OpenRGB: it already works (under another name)

OpenRGB's `InstantMouseController` already detects **30fa:1440**, the Blaze Ultra's USB ID, because
it's the same Instant chip and product ID as the **Anko KM43243952** (a Kmart mouse):

```
Controllers/InstantMouseController/InstantMouseDevices.h
    #define ANKO_KM43243952_VID   0x1440        <- named _VID, but it's a product ID
Controllers/InstantMouseController/InstantMouseControllerDetect.cpp
    REGISTER_HID_DETECTOR_IPU("Anko KM43243952 USB Gaming Mouse", ..., 0x30FA, 0x1440, 1, 0xFF01, 0x01)
```

Its lighting packets (`07 13 FF MS DN -B M- --` for the mode, `07 14 XG RB ...` for the colour) are
the same ones blazectl uses and that were tested on a real Blaze Ultra. So OpenRGB should already
control the Blaze Ultra's lighting; it just shows it as "Anko KM43243952 USB Gaming Mouse".

**Test it:** install OpenRGB, close BLAZE Control and the EvoFox app, then check that the mouse is
listed and that Static, Breathing and Rainbow work. That test result is the contribution.

**Contribute.** OpenRGB is on GitLab (`gitlab.com/CalcProgrammer1/OpenRGB`), and its CONTRIBUTING.md
says AI-generated submissions aren't accepted and AI must not be credited in commits. You are
responsible for understanding and testing anything you submit. So do it yourself, in your own words:

1. **Easiest: an issue.** Report that the EvoFox Blaze Ultra (India, Amkette) is 30fa:1440 and works
   with InstantMouseController, and list what you tested.
2. **A small merge request**, if you want one. Three changes:
   - add "EvoFox Blaze Ultra" to the detector's name for 0x1440 (the same detector covers both mice, so
     don't register a second one);
   - add it to the "Identified devices" comment in `RGBController_InstantMouse.cpp`;
   - (optional) rename the two `ANKO_..._VID` macros to `_PID`, since they're product IDs.

   Build OpenRGB and test it on your mouse before submitting.

Facts you can cite: USB ID 30fa:1440, product string "INSTANT USB Gaming Mouse", control collection on
interface 1, usage page 0xFF01, feature report 0x07 (8 bytes), and the evidence in [FINDINGS.md](../FINDINGS.md).

## libratbag / Piper: needs a new driver

libratbag (the library behind **Piper**, the standard Linux mouse configurator) has no driver for
Instant chips. Adding one would give every Linux distro DPI, polling, button and lighting support for
this mouse family. blazectl's FINDINGS.md has everything a driver needs.

How it would map onto libratbag (compare the small `src/driver-sinowealth-nubwo.c` and `src/driver-etekcity.c`):

| libratbag concept | Blaze Ultra |
|---|---|
| `probe` | open the hidraw node with feature report 0x07, check the header `5a 03 a5` at 0x00 |
| profiles | 1 (the official app's 4 profiles live on the PC; tables at 0x44/0x50/0x60 aren't decoded) |
| resolutions | 6 levels, block 0x10 bytes 3, 5-7; the 23 DPI values; active level = block 0x18 byte 0 |
| report rate | block 0x18 byte 6 = 1000/Hz − 1 (125/250/500/1000) |
| buttons | 8; block 0x08 + block 0x10 byte 0; function codes in FINDINGS.md (button 8 is limited) |
| macros | function 0x0D/0x0E, data at 0x400, entries at 0xA1 + 10×slot |
| LEDs | 1 RGB zone, 12 effects; packets 0x13/0x14 (RAM only, lost on reload) |
| `commit` | memory write `07 18 03 …`, commit `07 18 09 …`, read back to verify, then reload `07 18 10` |
| device file | `data/devices/evofox-blaze-ultra.device` with `DeviceMatch=usb:30fa:1440`, `Driver=instant` |

Suggested path:

1. Open an issue on `github.com/libratbag/libratbag` asking whether they'd accept an Instant driver,
   and link FINDINGS.md.
2. Write the driver in C (about 500-800 lines, like the drivers above) and test it on Kali with
   `ratbagctl` and Piper.
3. Check their contribution and AI rules at the time you submit, and as with OpenRGB, submit only code
   you understand and have tested yourself.

That's a good portfolio project in its own right: C, a real upstream codebase, code review by maintainers.
