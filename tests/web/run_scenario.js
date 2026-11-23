// blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
// Copyright (C) 2026 Pranjal Tripathi
// SPDX-License-Identifier: GPL-3.0-or-later
// Runs GUI API calls through blazectl/web/webhid.js against a simulated mouse (same behaviour as
// tests/test_instant.py FakeInstantMouse). Input on stdin: {mem: hex, steps: [[path, body], ...]}.
"use strict";
const path = require("path");
const B = require(path.join(__dirname, "..", "..", "blazectl", "web", "webhid.js"));

class FakeMouse {
  constructor(mem) { this.mem = mem; this.pending = new Map(); this.reply = [0, 0, 0, 0, 0, 0, 0, 0]; this.sent = []; this.reloads = 0; }
  async setFeature(p) {
    if (p.length !== 8 || p[0] !== 7) throw new Error("bad packet " + p);
    this.sent.push(Buffer.from(p).toString("hex"));
    const [, cmd, op, idx, lo, hi, val] = p;
    if (cmd === 0x18) {
      const addr = lo | (hi << 8);
      if (op === 0x05) this.reply = [7, this.mem[addr + idx], 0, 0, 0, 0, 0, 0];
      else if (op === 0x03) this.pending.set(addr + idx, val);
      else if (op === 0x09) { for (const [a, v] of this.pending) this.mem[a] = v; this.pending.clear(); }
      else if (op === 0x10) this.reloads++;
    }
  }
  async getFeature() { return this.reply.slice(); }
}

let input = "";
process.stdin.on("data", d => { input += d; });
process.stdin.on("end", async () => {
  const sc = JSON.parse(input);
  const mouse = new FakeMouse(Array.from(Buffer.from(sc.mem, "hex")));
  const api = new B.WebApi({device: mouse, sleepFn: async () => {}});
  const results = [];
  for (const [p, body] of sc.steps) {
    try { results.push({ok: true, result: await api.handle(p, body)}); }
    catch (e) { results.push({ok: false, error: e.message}); }
  }
  process.stdout.write(JSON.stringify({results, sent: mouse.sent, mem: Buffer.from(mouse.mem).toString("hex"), reloads: mouse.reloads}));
});
