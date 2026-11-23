# Alert light: the mouse as a physical SOC beacon

When a security tool reports something, the BLAZE Agent blinks the mouse in the alert's colour and
shows a popup. When the alert ends, your lighting comes back. Alerts only change lighting, which lives
in the mouse's RAM, so they cost **no flash writes** however often they fire.

| Severity | Colour | Switches on/off every |
|---|---|---|
| critical | red | 0.25 s |
| high | orange-red | 0.4 s |
| medium | yellow | 0.7 s |
| low | blue | 1 s |

Alerts below the "From" level (Automation page, default *medium*) are ignored. A stronger alert
replaces a weaker one that's still showing. **CLEAR** (or `blazectl alert --ack`) stops it early.

## Turning it on

1. BLAZE Control → **Auto** → **Alert light**: tick *on* and choose the minimum severity.
2. Start the agent (**START**; tick *start with Windows* to keep it running).
3. Click **TEST**. The mouse should blink for 15 seconds (orange, or red if your minimum is *critical*).

## Three ways to send alerts

### 1. Append JSON lines to a file (no code changes to your tool)

Add the file under **Watch files**. The agent follows it like `tail -f`, starting at the end (old lines
don't fire). Each line is one JSON object. Several shapes are understood:

```json
{"severity": "high", "title": "Failed logon burst from 10.0.0.7"}
{"rule": {"title": "Mimikatz command line", "level": "critical"}, "host": "pc1"}
{"level": "medium", "message": "New service installed"}
{"risk": 8.1, "name": "Suspicious PowerShell"}
```

Severity can be a word (`informational`, `low`, `medium`/`warning`, `high`/`error`, `critical`) or a
0-10 number (≥9 critical, ≥7 high, ≥4 medium, ≥1 low). Sigma's `level` values work as they are.

### 2. From the command line or any script

```
blazectl alert --severity critical "Canary file touched"
blazectl alert --ack
```

If no agent is running, the command blinks the mouse itself for `--seconds` (default 8) and then puts
your lighting back.

### 3. HTTP from your own code

The agent listens on `127.0.0.1` only. The port is in `agent-endpoint.json` and a secret token in
`agent-token`, both in the settings folder (`%APPDATA%\blazectl` on Windows, `~/.config/blazectl` on
Linux). The token keeps web pages in your browser from triggering alerts.

```python
from blazectl.agent import send_alert      # pip install the blazectl folder, or copy the function
send_alert("high", "Brute force on RDP")    # None: no agent running; False: ignored (light off / below minimum)
```

## TriageLens

TriageLens already decides each alert's severity, so hook in where it finishes triaging an alert.
Either way works:

```python
# Option A: write a JSONL feed (TriageLens doesn't need blazectl installed)
import json, pathlib
FEED = pathlib.Path.home() / "TriageLens" / "alerts.jsonl"

def publish(alert):                      # call this after triage
    with FEED.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"severity": alert.level, "title": alert.title}) + "\n")
```

Then add `C:\Users\<you>\TriageLens\alerts.jsonl` under **Watch files**.

```python
# Option B: tell the agent directly
try:
    from blazectl.agent import send_alert
except ImportError:
    send_alert = None

def publish(alert):
    if send_alert and alert.level in ("high", "critical"):
        send_alert(alert.level, alert.title)
```

The field names (`alert.level`, `alert.title`) are placeholders: use whatever your alert model calls them.

## Ideas for other sources

* **Linux SSH failures** (your user needs to be able to read the journal, e.g. be in the `adm` or
  `systemd-journal` group):
  ```
  journalctl -f -u ssh | grep --line-buffered "Failed password" | \
    while read -r l; do blazectl alert --severity high "SSH: ${l##*: }"; done
  ```
* **Windows failed logons (event 4625):** reading the Security log needs admin, so this one needs an
  administrator to set up a Task Scheduler task with an *On an event* trigger (Log: Security, Event ID
  4625) that runs `blazectl alert --severity high "Failed Windows logon"`.
* **Canary files, CI failures, honeypots (AgentTripwire):** anything that can run a command or write a
  line of JSON can light the mouse.
