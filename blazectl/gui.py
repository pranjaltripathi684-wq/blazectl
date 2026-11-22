# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""blazectl GUI (tkinter, ships with Python, no extra install).

Run:  python -m blazectl gui
"""

from __future__ import annotations

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

from .drivers import instant as ins
from .service import LightingSettings, MouseService, ServiceError, button_choices, official_app_running

BG, PANEL, FG, MUTED, ACCENT, OK, ERR = "#15171c", "#1f232b", "#e8eaed", "#9aa0a6", "#e0393e", "#3fb950", "#f85149"

BUTTON_LABELS = {
    1: "1  Left button", 2: "2  Right button", 3: "3  Wheel click", 4: "4  Side (front)",
    5: "5  Side (rear)", 6: "6  Top button (upper)", 7: "7  Top button (lower)", 8: "8  Red fire button",
}
FUNCTION_LABELS = {
    "left-click": "Left Click", "middle-click": "Middle Click", "right-click": "Right Click",
    "back": "Back", "forward": "Forward", "dpi-loop": "DPI Loop", "rapid-fire": "Rapid Fire", "led-loop": "LED Loop",
    "show-desktop": "Show Desktop", "double-click": "Double Click", "dpi-up": "DPI +", "dpi-down": "DPI -",
}
MODE_LABELS = {
    "static": "Static", "breathing": "Breathing", "breathing-multi": "Breathing (multicolour)", "rainbow": "Rainbow wave",
    "spectrum": "Spectrum cycle", "fill": "Fill & drain", "loop": "Loop", "enraptured": "Enraptured",
    "flicker": "Flicker", "ripple": "Ripple", "startrek": "Star Trek", "off": "Off",
}


class BlazeApp:
    def __init__(self, root: tk.Tk, service: MouseService | None = None, check_official_app: bool = True):
        self.root = root
        self.svc = service or MouseService()
        self.dpi_values = list(ins.dpi_table(0x1440).values())
        root.title("blazectl - EvoFox Blaze Ultra")
        root.configure(bg=BG)
        root.minsize(560, 520)
        self._style()

        head = tk.Frame(root, bg=BG)
        head.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(head, text="BLAZE", fg=ACCENT, bg=BG, font=("Segoe UI", 20, "bold")).pack(side="left")
        tk.Label(head, text=" ULTRA  configurator", fg=FG, bg=BG, font=("Segoe UI", 12)).pack(side="left", pady=(6, 0))
        ttk.Button(head, text="Backup…", command=self.on_backup).pack(side="right")
        ttk.Button(head, text="Read from mouse", command=self.refresh).pack(side="right", padx=6)

        self.device_var = tk.StringVar(value="Looking for mouse…")
        tk.Label(root, textvariable=self.device_var, fg=MUTED, bg=BG, anchor="w").pack(fill="x", padx=16)
        self.warn_var = tk.StringVar()
        self.warn = tk.Label(root, textvariable=self.warn_var, fg="#111", bg="#f2cc60", anchor="w", justify="left", padx=8, pady=4, wraplength=520)

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=16, pady=10)
        self.nb = nb
        self._lighting_tab(nb)
        self._dpi_tab(nb)
        self._buttons_tab(nb)
        self._polling_tab(nb)

        self.status_var = tk.StringVar(value="")
        self.status = tk.Label(root, textvariable=self.status_var, fg=MUTED, bg=PANEL, anchor="w", padx=10, pady=6)
        self.status.pack(fill="x", side="bottom")

        if check_official_app and official_app_running():
            self.warn_var.set("Warning: the EvoFox app is running and can overwrite these settings. Close it (Task Manager > End task).")
            self.warn.pack(fill="x", padx=16, before=nb)
        self.refresh()

    # ---------------------------------------------------------------- layout

    def _style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=PANEL, foreground=FG, fieldbackground="#2a2f39", bordercolor="#333944")
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background="#262b34", foreground=MUTED, padding=(14, 6))
        s.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", FG)])
        s.configure("TFrame", background=PANEL)
        s.configure("TLabel", background=PANEL, foreground=FG)
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("TButton", background="#2f3540", foreground=FG, padding=(10, 4))
        s.map("TButton", background=[("active", "#3a414e")])
        s.configure("Accent.TButton", background=ACCENT, foreground="white")
        s.map("Accent.TButton", background=[("active", "#c22f33")])
        s.configure("TRadiobutton", background=PANEL, foreground=FG)
        s.configure("TCheckbutton", background=PANEL, foreground=FG)
        s.configure("TCombobox", arrowcolor=FG, background="#2f3540")
        s.map("TCombobox",
              fieldbackground=[("readonly", "#2a2f39"), ("disabled", "#20242b")],
              foreground=[("readonly", FG), ("disabled", MUTED)],
              selectbackground=[("readonly", "#2a2f39")],
              selectforeground=[("readonly", FG)])
        self.root.option_add("*TCombobox*Listbox.background", "#2a2f39")
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        s.configure("Horizontal.TScale", background=PANEL)

    def _tab(self, nb, title):
        f = ttk.Frame(nb, padding=16)
        nb.add(f, text=title)
        return f

    def _lighting_tab(self, nb):
        f = self._tab(nb, "Lighting")
        self.light = self.svc.load_lighting()
        ttk.Label(f, text="Effect").grid(row=0, column=0, sticky="w")
        self.mode_var = tk.StringVar(value=MODE_LABELS[self.light.mode])
        mode = ttk.Combobox(f, textvariable=self.mode_var, values=list(MODE_LABELS.values()), state="readonly", width=26)
        mode.grid(row=0, column=1, columnspan=3, sticky="w", pady=4)
        mode.bind("<<ComboboxSelected>>", lambda e: self._sync_light_controls())

        ttk.Label(f, text="Colour per DPI level").grid(row=1, column=0, sticky="w", pady=(10, 0))
        sw = tk.Frame(f, bg=PANEL)
        sw.grid(row=1, column=1, columnspan=3, sticky="w", pady=(10, 0))
        self.swatches = []
        colors = (self.light.colors + [self.light.colors[0]] * 6)[:6]
        for i, c in enumerate(colors):
            b = tk.Button(sw, width=3, bg=c, activebackground=c, relief="flat", bd=0, highlightthickness=1,
                          command=lambda i=i: self.pick_color(i))
            b.grid(row=0, column=i, padx=3)
            tk.Label(sw, text=str(i + 1), fg=MUTED, bg=PANEL).grid(row=1, column=i)
            self.swatches.append(b)
        self.same_var = tk.BooleanVar(value=len(set(colors)) == 1)
        ttk.Checkbutton(f, text="Same colour for all levels", variable=self.same_var).grid(row=2, column=1, columnspan=3, sticky="w")

        self.speed_var = tk.IntVar(value=self.light.speed)
        self.bright_var = tk.IntVar(value=self.light.brightness)
        self.dir_var = tk.IntVar(value=self.light.direction)
        ttk.Label(f, text="Speed").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.speed_scale = tk.Scale(f, from_=0, to=ins.SPEED_MAX, orient="horizontal", variable=self.speed_var,
                                    bg=PANEL, fg=FG, highlightthickness=0, troughcolor="#2a2f39", length=220)
        self.speed_scale.grid(row=3, column=1, columnspan=3, sticky="w")
        ttk.Label(f, text="Brightness").grid(row=4, column=0, sticky="w")
        self.bright_scale = tk.Scale(f, from_=0, to=ins.BRIGHTNESS_MAX, orient="horizontal", variable=self.bright_var,
                                     bg=PANEL, fg=FG, highlightthickness=0, troughcolor="#2a2f39", length=220)
        self.bright_scale.grid(row=4, column=1, columnspan=3, sticky="w")
        ttk.Label(f, text="Direction").grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.dir_btns = [ttk.Radiobutton(f, text=t, value=v, variable=self.dir_var) for v, t in ((0, "Normal"), (1, "Reverse"))]
        for i, b in enumerate(self.dir_btns):
            b.grid(row=5, column=1 + i, sticky="w", pady=(6, 0))
        ttk.Button(f, text="Apply lighting", style="Accent.TButton", command=self.apply_lighting).grid(
            row=6, column=0, columnspan=4, sticky="w", pady=(18, 0))
        ttk.Label(f, text="Lighting is re-applied automatically after DPI, button or polling changes.",
                  style="Muted.TLabel").grid(row=7, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self._sync_light_controls()

    def _dpi_tab(self, nb):
        f = self._tab(nb, "DPI")
        ttk.Label(f, text="Six levels, cycled with the DPI button.", style="Muted.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.dpi_vars = []
        self.dpi_active_labels = []
        for i in range(ins.DPI_LEVELS):
            ttk.Label(f, text=f"Level {i + 1}").grid(row=i + 1, column=0, sticky="w", pady=3)
            v = tk.StringVar()
            ttk.Combobox(f, textvariable=v, values=[str(d) for d in self.dpi_values], state="readonly", width=10).grid(
                row=i + 1, column=1, sticky="w", padx=8)
            lab = ttk.Label(f, text="", style="Muted.TLabel")
            lab.grid(row=i + 1, column=2, sticky="w")
            self.dpi_vars.append(v)
            self.dpi_active_labels.append(lab)
        ttk.Button(f, text="Apply DPI", style="Accent.TButton", command=self.apply_dpi).grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(14, 0))

    def _buttons_tab(self, nb):
        f = self._tab(nb, "Buttons")
        self.btn_vars = {}
        for r, num in enumerate(sorted(BUTTON_LABELS)):
            ttk.Label(f, text=BUTTON_LABELS[num]).grid(row=r, column=0, sticky="w", pady=3)
            v = tk.StringVar()
            choices = [FUNCTION_LABELS[n] for n in button_choices(num)]
            ttk.Combobox(f, textvariable=v, values=choices, state="readonly", width=16, height=12).grid(row=r, column=1, sticky="w", padx=8)
            self.btn_vars[num] = v
        ttk.Label(f, text="Button 8 only supports Rapid Fire or DPI Loop (mouse firmware limit).",
                  style="Muted.TLabel").grid(row=9, column=0, columnspan=3, sticky="w", pady=(6, 0))
        row = tk.Frame(f, bg=PANEL)
        row.grid(row=10, column=0, columnspan=3, sticky="w", pady=(14, 0))
        ttk.Button(row, text="Apply buttons", style="Accent.TButton", command=self.apply_buttons).pack(side="left")
        ttk.Button(row, text="Factory reset", command=self.reset_buttons).pack(side="left", padx=8)

    def _polling_tab(self, nb):
        f = self._tab(nb, "Polling")
        ttk.Label(f, text="How often the mouse reports its position.", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8))
        self.poll_var = tk.IntVar(value=1000)
        for i, rate in enumerate(sorted(ins.POLL_RATES)):
            ttk.Radiobutton(f, text=f"{rate} Hz", value=rate, variable=self.poll_var).grid(row=i + 1, column=0, sticky="w", pady=2)
        ttk.Button(f, text="Apply polling rate", style="Accent.TButton", command=self.apply_polling).grid(
            row=6, column=0, sticky="w", pady=(14, 0))

    # --------------------------------------------------------------- helpers

    def say(self, text, kind="info"):
        self.status_var.set(text)
        self.status.configure(fg={"ok": OK, "error": ERR}.get(kind, MUTED))
        self.root.update_idletasks()

    def _do(self, busy, fn, done):
        self.say(busy)
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            result = fn()
        except ServiceError as e:
            self.say(f"✗ {e}", "error")
            return False, None
        finally:
            self.root.configure(cursor="")
        self.say(done(result) if callable(done) else done, "ok")
        return True, result

    def _sync_light_controls(self):
        mode = self._mode()
        m = ins.MODES[mode]
        for b in self.swatches:
            b.configure(state="normal" if m.color else "disabled")
        self.speed_scale.configure(state="normal" if m.speed else "disabled")
        self.bright_scale.configure(state="normal" if m.brightness else "disabled")
        for b in self.dir_btns:
            b.configure(state="normal" if m.direction else "disabled")

    def _mode(self) -> str:
        label = self.mode_var.get()
        return next(k for k, v in MODE_LABELS.items() if v == label)

    def pick_color(self, i):
        current = self.swatches[i].cget("bg")
        _, hexcolor = colorchooser.askcolor(color=current, title=f"Colour for DPI level {i + 1}")
        if hexcolor:
            self.set_swatch(i, hexcolor)

    def set_swatch(self, i, hexcolor):
        targets = range(6) if self.same_var.get() else [i]
        for j in targets:
            self.swatches[j].configure(bg=hexcolor, activebackground=hexcolor)

    # --------------------------------------------------------------- actions

    def refresh(self):
        ok, st = self._do("Reading mouse…", self.svc.read_state, "Settings read from mouse.")
        if not ok:
            self.device_var.set("Mouse not connected")
            return
        self.device_var.set(f"Connected: {st.name}  ({st.usb_id})")
        for i, v in enumerate(self.dpi_vars):
            v.set(str(st.dpi[i]) if st.dpi[i] is not None else "")
            self.dpi_active_labels[i].configure(text="● active" if i == st.active_level else "")
        names = ins.FUNCTION_NAMES
        for num, v in self.btn_vars.items():
            code = st.buttons.get(num)
            v.set(FUNCTION_LABELS.get(names.get(code, ""), f"unknown (0x{code:02x})"))
        if st.polling_hz:
            self.poll_var.set(st.polling_hz)

    def apply_lighting(self):
        s = LightingSettings(
            mode=self._mode(),
            colors=[b.cget("bg") for b in self.swatches],
            speed=self.speed_var.get(),
            brightness=self.bright_var.get(),
            direction=self.dir_var.get(),
        )
        ok, _ = self._do("Setting lighting…", lambda: self.svc.set_lighting(s), f"✓ Lighting set: {MODE_LABELS[s.mode]}")
        if ok:
            self.light = s

    def apply_dpi(self):
        try:
            values = [int(v.get()) for v in self.dpi_vars]
        except ValueError:
            self.say("✗ Pick a DPI value for every level.", "error")
            return
        self._do("Writing DPI…", lambda: self.svc.set_dpi(values), lambda r: f"✓ DPI saved and verified: {r}")

    def apply_polling(self):
        rate = self.poll_var.get()
        self._do("Writing polling rate…", lambda: self.svc.set_polling(rate), f"✓ Polling rate saved and verified: {rate} Hz")

    def apply_buttons(self):
        by_label = {v: k for k, v in FUNCTION_LABELS.items()}
        mapping = {}
        for num, v in self.btn_vars.items():
            name = by_label.get(v.get())
            if name is None:
                self.say(f"✗ Choose a function for button {num}.", "error")
                return
            mapping[num] = ins.BUTTON_FUNCTIONS[name]
        self._do("Writing buttons…", lambda: self.svc.set_buttons(mapping), "✓ Buttons saved and verified.")

    def reset_buttons(self):
        if not messagebox.askyesno("Factory reset", "Restore all 8 buttons to their factory functions?"):
            return
        ok, _ = self._do("Resetting buttons…", self.svc.reset_buttons, "✓ Buttons reset to factory.")
        if ok:
            self.refresh()

    def on_backup(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", initialfile="blaze-backup.json",
                                            filetypes=[("JSON", "*.json")])
        if path:
            self._do("Backing up memory…", lambda: self.svc.backup(path), f"✓ Backup saved to {path}")


def main(service: MouseService | None = None):
    root = tk.Tk()
    BlazeApp(root, service)
    root.mainloop()


if __name__ == "__main__":
    main()
