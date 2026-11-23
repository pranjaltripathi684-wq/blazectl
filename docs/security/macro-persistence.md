# Your mouse can type for you: keystroke macros as a persistence spot

*blazectl security note · Pranjal Tripathi · November 2026*

**Summary.** The EvoFox Blaze Ultra stores keyboard macros in its own flash memory. Writing them
needs no administrator rights on Windows: any program running as the normal user can do it, and
the mouse accepts the write without any confirmation. Once stored, a macro plays on **any** computer
the mouse is plugged into, the first time the bound button is pressed. Reinstalling the OS doesn't
remove it, and antivirus doesn't look there.

This note explains the risk, its limits, and how to check a mouse with `blazectl audit`. It contains
no working attack: everything here was tested on my own mouse and a simulated one, with harmless
sequences.

## Background

The mouse is configured through an 8-byte HID feature report (`0x07`) on a vendor-defined collection
(usage page `0xFF01`). Its memory protocol (read, write, commit, reload) is documented in
[FINDINGS.md](../../FINDINGS.md). Macros live from address `0x400`:

```
0x400  [repeat, 00] (T,K)(T,K)... (00 00)   T bit 7 = key down, T bits 0-6 = delay ms, K = HID key code
0x0A1  10-byte entry per button -> points at the macro
0x008  button slot byte -> function 0x0D (play N times) / 0x0E (while held) / 0x0F (media key)
```

Nothing in the protocol is authenticated or signed. The firmware stores whatever arrives.

## Who can write a macro?

| System | Who can write the mouse's memory | Why |
|---|---|---|
| Windows | **any process of the logged-in user**, no admin, no prompt | Windows locks the mouse's pointer collection, but vendor-defined collections can be opened by normal users. That's how the official app and blazectl work. |
| Linux (default) | root only | `/dev/hidraw*` is root-owned. |
| Linux with blazectl's udev rule | any process of the active desktop user | `TAG+="uaccess"` grants the seat user access (that's the point of the rule). |

So on Windows, malware that is already running as you (a malicious download, a compromised game mod,
a bad VS Code extension) can also write to your mouse.

## What an attacker gets

* **Persistence outside the disk.** The macro survives an OS reinstall, a disk wipe and moving to a
  new PC. Endpoint tools scan files, the registry and memory, not mouse flash.
* **Cross-machine spread with a human trigger.** A mouse is shared, lent, taken to college labs and
  plugged into work laptops. Pressing the bound button replays the keystrokes on that machine.
* **It runs as a keyboard.** To the OS the keystrokes are normal typing, so anything the user could
  type, the macro can type (for example opening the Run box and entering a command).

### Limits (why this is not a panic)

* The attacker **already needed code running on one of your machines** to write the macro.
* It needs **a button press**. The mouse has no timer, and plain clicks don't trigger macros unless the
  left button itself is rebound (blazectl refuses that; `audit` flags it as HIGH).
* A macro is **visible**: it types where you can see it, and it fails on a locked screen or with a
  different keyboard layout. On a standard (non-admin) account, an elevation prompt asks for a password
  the macro doesn't know. **On an admin account it's worse**: unlike software-injected input, keystrokes
  from a real USB device *can* answer a UAC "Yes/No" prompt.
* Space is small: 127 events per macro, about 1 KB in total, so roughly 60 typed characters per macro.
* Fixing it is easy once you know: overwrite the button (`blazectl macro clear N --execute`) or reset
  the buttons in the app.

This is the same family as **BadUSB** (Nohl and Lell, Black Hat USA 2014), but much weaker: nothing
is reflashed, and the device only replays stored keystrokes when a button is pressed. MITRE ATT&CK's
closest match is *T1200 Hardware Additions*. Here the "addition" is a trusted device you already own.

## Detection: `blazectl audit`

```
blazectl audit                     # read-only: reads 0x000-0xFFF from the mouse
blazectl audit --save evidence.json   # also keep the raw memory
blazectl audit --dump old-backup.json # audit a saved dump instead
blazectl audit --json              # for scripts / SIEM; exit code 0 clean, 1 check, 2 high
```

It decodes every macro, shortcut and media key, **renders what it would type** (US layout), and rates it:

| Level | Examples |
|---|---|
| **HIGH** | opens a command entry point **and** types into it (`[Win+R]cmd⏎`), or types command-like text and presses Enter (`curl …⏎`), or a macro on the left button |
| medium | a launcher on its own (a `Win+R` shortcut), command-like words without Enter, data in normally-empty memory (0x0F8-0x3FF, 0x800-0xF9F), an undecodable macro area |
| low | types text and presses Enter (`Hi⏎`); leftover data containing command-like words |
| info | game macros, `Ctrl+C`, media keys, stale leftovers of old macros |

Launchers it knows: `Win+R`, `Win+X`, a lone `Win` tap (Start search), `Ctrl+Alt+T` and `Alt+F2`
(Linux), `Ctrl+Shift+Enter` (run as admin). Command words include `cmd`, `powershell`, `mshta`,
`rundll32`, `certutil`, `curl`, `wget`, `iex`, `bash`, `/dev/tcp`, `base64`/`-enc`, `reg add`,
`schtasks`, URLs and UNC paths. Words are matched whole, so "model" doesn't match "del".

### Example (the macro is from the test suite, simulated mouse)

```
!! [HIGH  ] button 6: could launch a command
           opens the Run box (Windows); types command-like text (cmd); then presses Enter
           types: [Win+R]cmd⏎
```

On my real mouse, after all the decoding experiments, the audit is clean. It also shows the leftover
tails of old test macros (`⏎b`, `i`), which both the official app and blazectl leave behind when a
macro is replaced:

```
   [INFO  ] button 6: types text            types: c
   [INFO  ] leftover key data at 0x428 (not on any button)   types: ⏎b
RESULT: clean.
```

## Recommendations

**Users**
* Run `blazectl audit` on a mouse you bought second-hand or lent out, and after any malware scare.
* Don't plug your gaming mouse into machines you don't trust, and don't plug untrusted mice into yours.
* If you don't use macros, leave every button on a plain function.

**Vendors (EvoFox/Amkette and other Instant-chip brands)**
* Show the stored macros when the app starts, and warn when they don't match the app's own profile
  (today the app never reads the mouse; see FINDINGS.md).
* Require a physical confirmation (hold a button while plugging in) before macro memory can be written,
  or add a "macro lock" in the firmware.
* Never allow a macro on the primary button.

**blazectl** (this project)
* Writes are already limited to decoded blocks and read back. It refuses a macro on button 1, and
  `audit` is read-only.
* The GUI shows every stored macro by reading the mouse, so a planted one is visible on the Buttons page.

## Disclosure

This is a design property of the chip family's configuration protocol, not a bug in one product, and
using it needs code already running on the victim's machine. I'm sharing it as a defensive note with a
detection tool, and no attack tooling. If you're a vendor and want details, open an issue on the
repository.
