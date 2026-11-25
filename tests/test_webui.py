# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""The local web UI: security checks and the JSON API, driving the simulated mouse."""

import http.client
import json
import threading

import pytest

from blazectl.webui import catalog, make_server
from sim import make_service


@pytest.fixture
def ui(tmp_path):
    svc, fake = make_service(tmp_path)
    server, token = make_server(svc)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def call(method, path, body=None, token=token, host=f"127.0.0.1:{port}"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Host": host, "Content-Type": "application/json"}
        if token is not None:
            headers["X-Blazectl-Token"] = token
        c.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (json.loads(data) if r.getheader("Content-Type", "").startswith("application/json") else data)

    yield call, fake, token, server
    server.shutdown()
    server.server_close()


def test_listens_on_localhost_only(ui):
    _, _, _, server = ui
    assert server.server_address[0] == "127.0.0.1"


def test_page_served_with_token_and_catalog(ui):
    call, _, token, _ = ui
    status, page = call("GET", "/", token=None)
    assert status == 200 and token.encode() in page and b"/*__BOOT__*/" not in page


def test_api_requires_token(ui):
    call, fake, _, _ = ui
    assert call("POST", "/api/state", {}, token=None)[0] == 403
    assert call("POST", "/api/state", {}, token="wrong")[0] == 403
    assert call("POST", "/api/dpi", {"dpi": [400] * 6}, token="wrong")[0] == 403
    assert fake.writes() == []


def test_dns_rebinding_host_rejected(ui):
    call, _, _, _ = ui
    assert call("GET", "/", token=None, host="evil.example:80")[0] == 403
    assert call("POST", "/api/state", {}, host="evil.example")[0] == 403


def test_state_and_writes(ui):
    call, fake, _, _ = ui
    status, st = call("POST", "/api/state", {})
    assert status == 200 and st["connected"] and st["usb_id"] == "30fa:1440"
    assert st["buttons"]["7"] == "led-loop"

    status, r = call("POST", "/api/dpi", {"dpi": [400, 800, 1600, 3200, 6400, 12800]})
    assert status == 200 and "verified" in r["message"] and r["state"]["dpi"] == [400, 800, 1600, 3200, 6400, 12800]

    status, r = call("POST", "/api/polling", {"rate": 250})
    assert status == 200 and fake.mem[0x1E] == 0x03 and r["state"]["polling"] == 250

    status, r = call("POST", "/api/buttons", {"buttons": {"7": "dpi-up", "6": "dpi-down"}})
    assert status == 200 and r["state"]["buttons"]["7"] == "dpi-up" and r["state"]["buttons"]["6"] == "dpi-down"

    status, r = call("POST", "/api/lighting", {"mode": "static", "colors": ["#ff0000"] * 6, "speed": 2,
                                               "brightness": 7, "direction": 0})
    assert status == 200 and r["state"]["lighting"]["colors"][0] == "#ff0000"

    status, r = call("POST", "/api/buttons/reset", {})
    assert status == 200 and r["state"]["buttons"]["7"] == "led-loop"

    status, r = call("POST", "/api/backup", {})
    assert status == 200 and r["backup"]["blocks"]["0x00"].startswith("5a03a5")


def test_errors_are_reported_not_crashing(ui):
    call, fake, _, _ = ui
    assert call("POST", "/api/dpi", {"dpi": [2200] * 6})[0] == 409
    assert call("POST", "/api/buttons", {"buttons": {"8": "back"}})[0] == 409
    assert call("POST", "/api/buttons", {"buttons": {"1": "explode"}})[0] == 409
    assert call("POST", "/api/lighting", {"mode": "disco", "colors": [], "speed": 0, "brightness": 0, "direction": 0})[0] == 409
    assert call("POST", "/api/dpi", {})[0] == 400
    assert call("POST", "/api/nope", {})[0] == 404
    assert fake.writes() == []


def test_catalog_matches_driver():
    c = catalog()
    assert len(c["dpi_values"]) == 23 and c["poll_rates"] == [125, 250, 500, 1000]
    assert c["buttons"]["8"]["choices"] == ["dpi-loop", "rapid-fire"] or set(c["buttons"]["8"]["choices"]) == {"dpi-loop", "rapid-fire"}
    assert "dpi-up" in c["functions"]


def test_profiles_saved_on_pc(ui):
    call, _, _, _ = ui
    status, p = call("POST", "/api/profiles", {})
    assert status == 200 and p["active"] == "office" and set(p["slots"]) == {"office", "game1", "game2", "media"}
    cfg = {"dpi": [400, 800, 1600, 3200, 6400, 12800], "polling": 1000,
           "buttons": {"7": "dpi-up"}, "lighting": {"mode": "static", "colors": ["#ff0000"] * 6,
                                                    "speed": 2, "brightness": 7, "direction": 0}}
    status, p = call("POST", "/api/profiles/save", {"slot": "game1", "config": cfg, "active": "game1"})
    assert status == 200 and p["active"] == "game1" and p["slots"]["game1"]["dpi"][0] == 400
    status, p = call("POST", "/api/profiles", {})
    assert p["slots"]["game1"]["buttons"]["7"] == "dpi-up"
    # bad input is rejected, nothing written
    assert call("POST", "/api/profiles/save", {"slot": "nope", "config": cfg})[0] == 409
    assert call("POST", "/api/profiles/save", {"slot": "game2", "config": {"dpi": [1]}})[0] == 409


def test_page_has_no_external_resources(ui):
    call, _, _, _ = ui
    _, page = call("GET", "/", token=None)
    assert b"http://" not in page.replace(b"http://www.w3.org", b"") and b"https://" not in page


def test_activate_level(ui):
    call, fake, _, _ = ui
    status, r = call("POST", "/api/active", {"level": 5})
    assert status == 200 and r["state"]["active_level"] == 4 and fake.mem[0x18] & 7 == 4
    assert call("POST", "/api/active", {"level": 9})[0] == 409
    assert call("POST", "/api/active", {})[0] == 400


def test_existing_macro_binding_survives_a_gui_sync(tmp_path):
    from sim import make_service
    from blazectl.webui import Api

    svc, fake = make_service(tmp_path)
    fake.mem[0x08 + 5] = 0xAD          # button 7 bound to a macro by the official app
    api = Api(svc)
    st = api.handle("/api/state", {})
    assert st["buttons"]["7"] == "macro"
    buttons = dict(st["buttons"], **{"4": "back"})
    api.handle("/api/buttons", {"buttons": buttons})
    assert fake.mem[0x08 + 5] == 0xAD and fake.mem[0x08 + 4] & 0x1F == 0x04


def test_macro_endpoints_bind_unbind_and_library(tmp_path):
    from sim import make_service
    from blazectl.webui import Api
    from blazectl.service import ServiceError

    svc, fake = make_service(tmp_path)
    api = Api(svc)
    hi = {"mode": "times", "repeat": 2, "events": [["H", "down", 30], ["H", "up", 30], ["I", "down", 30], ["I", "up", 20]]}
    api.handle("/api/macros", {"bind": {"5": hi}})
    st = api.state()
    assert st["buttons"]["5"] == "macro" and st["macros"]["5"] == hi
    assert fake.mem[0x400:0x40C] == bytes.fromhex("0200 9e0b 1e0b 9e0c 140c 0000")
    api.handle("/api/macros", {"unbind": {"5": "back"}})
    st = api.state()
    assert st["buttons"]["5"] == "back" and st["macros"] == {}
    with pytest.raises(ServiceError):
        api.handle("/api/macros", {"bind": {"1": hi}})                     # left button refused
    with pytest.raises(ServiceError):
        api.handle("/api/macros", {"bind": {"5": {"events": [["nope", "down", 1]]}}})
    lib = api.handle("/api/macros/library/save", {"name": "Greeting", "macro": hi})["library"]
    assert lib == {"Greeting": hi}
    lib = api.handle("/api/macros/library/save", {"name": "Hello", "macro": hi, "old_name": "Greeting"})["library"]
    assert list(lib) == ["Hello"]
    assert api.handle("/api/macros/library", {})["library"] == lib
    assert api.handle("/api/macros/library/delete", {"name": "Hello"})["library"] == {}


def test_profiles_keep_macros(tmp_path):
    from blazectl.service import ProfileStore
    store = ProfileStore(str(tmp_path / "p.json"))
    cfg = {"dpi": [800] * 6, "polling": 1000, "buttons": {"5": "macro"},
           "lighting": {"mode": "static", "colors": ["#ff0000"] * 6, "speed": 2, "brightness": 7, "direction": 0},
           "macros": {"5": {"mode": "hold", "repeat": 1, "events": [["X", "down", 40], ["X", "up", 40]]}}}
    assert store.save("game1", cfg)["slots"]["game1"]["macros"] == cfg["macros"]


def test_missing_hidapi_is_a_clear_message_not_a_crash(tmp_path):
    """Windows without the hidapi package: the GUI shows the install hint instead of an error page."""
    from blazectl.service import MouseService
    from blazectl.webui import Api

    def no_hidapi():
        raise RuntimeError("Talking to the mouse on Windows needs the hidapi package. Install it with:  py -m pip install hidapi")

    api = Api(MouseService(nodes_fn=no_hidapi, settings_path=str(tmp_path / "l.json")))
    st = api.state()
    assert st["connected"] is False and "hidapi" in st["error"]
