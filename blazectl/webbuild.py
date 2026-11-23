# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build BLAZE Web: the same page as the desktop app, talking to the mouse through WebHID.

    blazectl build-web --out site        ->  site/index.html (one self-contained file)

The page is the app's web/index.html with the JavaScript driver (web/webhid.js) inlined in place of
the local Python server. Nothing is loaded from the internet; the Content-Security-Policy forbids
all network connections, so the page can only talk to the mouse you pick in Chrome's device chooser.
Settings, profiles and the macro library are kept in the browser (localStorage).
"""
from __future__ import annotations

import json
import os
from importlib import resources
from pathlib import Path

CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
       "connect-src 'none'; base-uri 'none'; form-action 'none'")

GLUE = r"""
// ---- BLAZE Web glue: route the page's api() calls to the WebHID driver ----
const webApi = new BlazeWeb.WebApi({storage: window.localStorage});
const READ_ONLY = ["/api/state", "/api/profiles", "/api/profiles/save", "/api/macros/library",
                   "/api/macros/library/save", "/api/macros/library/delete", "/api/backup"];
let writeOk = false;
window.BLAZE_API = async (path, body) => {
  if (!READ_ONLY.includes(path) && !writeOk) {
    writeOk = confirm("Before BLAZE Web writes to the mouse:\n\nClose BLAZE Control, stop the BLAZE Agent and quit the " +
      "EvoFox app (check the tray). Two programs writing to the mouse at the same time can damage its settings.\n\n" +
      "Are they all closed?");
    if (!writeOk) throw new Error("Not written. Close the other mouse programs, then press Sync again.");
  }
  try { return await webApi.handle(path, body); }
  catch (e) { throw new Error(e && e.message ? e.message : String(e)); }
};
async function blazeConnect(dev) {
  if (!dev.opened) await dev.open();
  webApi.setDevice(new BlazeWeb.HidTransport(dev), dev.productId, dev.productName || "INSTANT USB Gaming Mouse");
  webApi.hidDevice = dev;
}
addEventListener("DOMContentLoaded", async () => {
  const auto = document.querySelector('.nav[data-page="auto"]');
  if (auto) auto.style.display = "none";                       // background jobs need the desktop app
  const btn = document.querySelector("#retryBtn"), msg = document.querySelector("#ovmsg");
  if (!("hid" in navigator)) {
    msg.innerHTML = "This browser can't talk to USB devices.<br>Open this page in <b>Chrome</b> or <b>Edge</b> on a computer.";
    btn.style.display = "none";
    return;
  }
  btn.textContent = "Connect mouse";
  btn.onclick = async () => {
    try {
      const devs = await navigator.hid.requestDevice({filters: BlazeWeb.HID_FILTERS});
      const d = devs.find(BlazeWeb.hasControl) || devs[0];
      if (!d) return;
      await blazeConnect(d);
      await refresh(false);
    } catch (e) { toast(e.message, "err"); }
  };
  navigator.hid.addEventListener("disconnect", e => {
    if (webApi.hidDevice === e.device) { webApi.setDevice(null); webApi.hidDevice = null; refresh(true); }
  });
  navigator.hid.addEventListener("connect", async e => {
    if (!webApi.hidDevice && BlazeWeb.hasControl(e.device)) { try { await blazeConnect(e.device); refresh(true); } catch {} }
  });
  const known = (await navigator.hid.getDevices()).filter(BlazeWeb.hasControl);
  if (known.length) { try { await blazeConnect(known[0]); await refresh(true); } catch {} }
});
"""


def build(out_dir: str | os.PathLike = "site") -> Path:
    from .webui import catalog

    web = resources.files("blazectl").joinpath("web")
    html = web.joinpath("index.html").read_text(encoding="utf-8")
    driver = web.joinpath("webhid.js").read_text(encoding="utf-8")
    boot = json.dumps({"token": "", "catalog": catalog(), "web": True})
    if "/*__BOOT__*/null" not in html or "<script>" not in html:
        raise RuntimeError("index.html doesn't have the expected BOOT marker / script tag")
    html = html.replace("/*__BOOT__*/null", boot)
    head = (f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
            '<meta name="description" content="BLAZE Web: configure the EvoFox Blaze Ultra in Chrome or Edge (WebHID). '
            'Open source, nothing leaves your computer.">\n')
    html = html.replace("<head>", "<head>\n" + head, 1)
    html = html.replace("<script>", "<script>\n" + driver.replace("</script", "<\\/script") + "\n" + GLUE + "\n</script>\n<script>", 1)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    page = out / "index.html"
    page.write_text(html, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    return page
