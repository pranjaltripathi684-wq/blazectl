// blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
// Copyright (C) 2026 Pranjal Tripathi
// SPDX-License-Identifier: GPL-3.0-or-later
//
// BLAZE Web: the blazectl driver in JavaScript, talking to the mouse through WebHID (Chrome/Edge).
// It is a line-by-line port of drivers/instant.py, drivers/macro.py, service.py and webui.Api, and
// answers the same /api/... calls, so the normal BLAZE Control page runs unchanged in a browser.
// tests/test_webhid.py runs this file and the Python driver side by side on the same simulated mouse
// and requires byte-identical USB traffic.
"use strict";
(function (root, factory) {
  const lib = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = lib;
  else root.BlazeWeb = lib;
})(typeof self !== "undefined" ? self : this, function () {

// ------------------------------------------------------------------ constants (instant.py)
const VID = 0x30FA, REPORT_ID = 0x07, PACKET_LEN = 8, CONTROL_USAGE_PAGE = 0xFF01;
const CMD_MODE = 0x13, CMD_COLOR = 0x14, CMD_MEMORY = 0x18;
const MEM_IDLE = 0x00, MEM_WRITE = 0x03, MEM_READ = 0x05, MEM_COMMIT = 0x09, MEM_RELOAD = 0x10;
const DPI_LEVELS = 6, SPEED_MAX = 5, BRIGHTNESS_MAX = 7;
const HEADER_MAGIC = [0x5A, 0x03, 0xA5];
const HEADER_BLOCK = 0x00, BUTTON_BLOCK = 0x08, DPI_BLOCK = 0x10, STATE_BLOCK = 0x18;
const WRITABLE_BLOCKS = new Set([BUTTON_BLOCK, DPI_BLOCK, STATE_BLOCK]);
const MACRO_BLOCKS = new Set([0x68, 0x70, 0x78]);
for (let a = 0xA0; a < 0xF8; a += 8) MACRO_BLOCKS.add(a);
for (let a = 0x400; a < 0x800; a += 8) MACRO_BLOCKS.add(a);
const MACRO_BUTTONS = {2: "predicted", 3: "predicted", 4: "confirmed", 5: "confirmed", 6: "confirmed", 7: "confirmed"};
const BUTTON_SLOTS = [   // [app number, name, block, byte index, factory code]
  [1, "left", BUTTON_BLOCK, 0, 0x01], [2, "right", BUTTON_BLOCK, 2, 0x03], [3, "middle", BUTTON_BLOCK, 1, 0x02],
  [4, "forward", BUTTON_BLOCK, 4, 0x05], [5, "back", BUTTON_BLOCK, 3, 0x04], [6, "dpi", BUTTON_BLOCK, 7, 0x06],
  [7, "led", BUTTON_BLOCK, 5, 0x15], [8, "fire", DPI_BLOCK, 0, 0x09]];
const BUTTON_FUNCTIONS = {"left-click": 0x01, "middle-click": 0x02, "right-click": 0x03, "back": 0x04, "forward": 0x05,
  "dpi-loop": 0x06, "rapid-fire": 0x09, "led-loop": 0x15, "show-desktop": 0x07, "double-click": 0x08, "dpi-up": 0x0B, "dpi-down": 0x0C};
const KEEP_ONLY_FUNCTIONS = {"macro": 0x0D, "macro-hold": 0x0E, "media": 0x0F};
const FUNCTION_NAMES = {};
for (const [k, v] of Object.entries({...BUTTON_FUNCTIONS, ...KEEP_ONLY_FUNCTIONS})) FUNCTION_NAMES[v] = k;
const BUTTON_ALLOWED = {8: new Map([[0x09, "rapid-fire (factory)"], [0x06, "dpi-loop (verified)"]])};   // Map keeps Python's order
const POLL_BYTE = 6, POLL_RATES = {1000: 0x00, 500: 0x01, 250: 0x03, 125: 0x07};
const DPI_CODES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24];
const MODES = {};
for (const [name, value, color, speed, brightness, direction] of [
  ["static", 0x0A, 1, 0, 1, 0], ["breathing", 0x08, 1, 1, 0, 0], ["breathing-multi", 0x09, 0, 1, 0, 0],
  ["rainbow", 0x07, 0, 1, 0, 1], ["spectrum", 0x06, 0, 1, 0, 0], ["fill", 0x03, 0, 1, 1, 1], ["loop", 0x04, 0, 1, 1, 1],
  ["enraptured", 0xBB, 0, 1, 0, 0], ["flicker", 0xB8, 0, 1, 0, 0], ["ripple", 0xBA, 0, 1, 0, 1], ["startrek", 0xB9, 0, 0, 0, 0],
  ["off", 0x0A, 0, 0, 0, 0]]) MODES[name] = {name, value, color: !!color, speed: !!speed, brightness: !!brightness, direction: !!direction};

class InstantError extends Error {}
class ServiceError extends Error {}
class MacroError extends Error {}
const hex = b => Array.from(b, x => x.toString(16).padStart(2, "0")).join(" ");
const eq = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);

// ------------------------------------------------------------------ packets
function parseColor(color) {
  const c = String(color).trim().replace(/^#/, "");
  if (!/^[0-9a-fA-F]{6}$/.test(c)) throw new InstantError(`invalid colour '${color}', use #rrggbb`);
  return [0, 2, 4].map(i => parseInt(c.substr(i, 2), 16));
}
function modePacket(m, speed = 0, brightness = 0, direction = 0) {
  const v = m.value;
  return [REPORT_ID, CMD_MODE, 0xFF, ((v << 4) & 0xFF) | (speed & 0xF), ((direction & 1) << 4) | 0x0B, 0xF - (brightness & 0xF), v & 0xF0, 0x00];
}
function colorPacket(level, color) {
  const [r, g, b] = parseColor(color).map(c => 0xF - (c >> 4));
  return [REPORT_ID, CMD_COLOR, ((level * 2) << 4) | g, (r << 4) | b, 0, 0, 0, 0];
}
function lightingPackets(modeName, colors, speed = 2, brightness = BRIGHTNESS_MAX, direction = 0) {
  if (!(modeName in MODES)) throw new InstantError(`unknown mode '${modeName}'; choose from: ${Object.keys(MODES).join(", ")}`);
  const m = MODES[modeName];
  if (!(speed >= 0 && speed <= SPEED_MAX)) throw new InstantError(`speed must be 0-${SPEED_MAX}`);
  if (!(brightness >= 0 && brightness <= BRIGHTNESS_MAX)) throw new InstantError(`brightness must be 0-${BRIGHTNESS_MAX}`);
  if (direction !== 0 && direction !== 1) throw new InstantError("direction must be 0 or 1");
  if (m.name === "off") return [modePacket(m)].concat([0, 1, 2, 3, 4, 5].map(i => colorPacket(i, "#000000")));
  const pkts = [modePacket(m, m.speed ? speed : 0, m.brightness ? brightness : 0, m.direction ? direction : 0)];
  if (m.color) {
    colors = colors && colors.length ? colors : ["#ffffff"];
    colors.forEach(parseColor);
    for (let i = 0; i < DPI_LEVELS; i++) pkts.push(colorPacket(i, i < colors.length ? colors[i] : colors[0]));
  }
  return pkts;
}
const memoryPacket = (op, index, addr, value, last) => [REPORT_ID, CMD_MEMORY, op, index, addr & 0xFF, addr >> 8, value, last];

// ------------------------------------------------------------------ DPI / buttons
function decodeDpi(b) {
  const lo = [b[5] >> 4, b[5] & 15, b[6] >> 4, b[6] & 15, b[7] >> 4, b[7] & 15];
  return lo.map((v, i) => v | (((b[3] >> i) & 1) << 4));
}
function encodeDpi(block, codes) {
  if (codes.length !== DPI_LEVELS) throw new InstantError(`need exactly ${DPI_LEVELS} DPI values`);
  for (const c of codes) if (!DPI_CODES.includes(c)) throw new InstantError(`invalid DPI code ${c}`);
  const b = Array.from(block);
  let bit4 = 0;
  codes.forEach((c, i) => { bit4 |= ((c >> 4) & 1) << i; });
  b[3] = (block[3] & 0xC0) | bit4;
  b[5] = ((codes[0] & 15) << 4) | (codes[1] & 15);
  b[6] = ((codes[2] & 15) << 4) | (codes[3] & 15);
  b[7] = ((codes[4] & 15) << 4) | (codes[5] & 15);
  return b;
}
const DPI_TABLE = (() => { const t = {}; for (let c = 1; c <= 10; c++) t[c] = c * 200; t[12] = 2400;
  for (let c = 13; c < 24; c++) t[c] = 2400 + (c - 12) * 800; t[24] = 12800; return t; })();
function dpiTable(pid) { if (pid === 0x1440) return DPI_TABLE; const t = {}; DPI_CODES.forEach(c => { t[c] = c * 200; }); return t; }
const codeToDpi = (code, pid) => dpiTable(pid)[code] ?? null;
function dpiToCode(dpi, pid) {
  const t = dpiTable(pid);
  for (const [code, v] of Object.entries(t)) if (v === dpi) return +code;
  throw new InstantError(`${dpi} DPI is not a known value for this mouse yet. Supported: [${Object.values(t).sort((a, b) => a - b).join(", ")}]`);
}
function decodeButtons(btn, dpi) {
  const blocks = {[BUTTON_BLOCK]: btn, [DPI_BLOCK]: dpi}, out = {};
  for (const [num, , blk, idx] of BUTTON_SLOTS) out[num] = blocks[blk][idx] & 0x1F;
  return out;
}
function encodeButtons(btn, dpi, changes) {
  for (const [num, code] of Object.entries(changes)) {
    const allowed = BUTTON_ALLOWED[num];
    if (allowed && !allowed.has(code))
      throw new InstantError(`button ${num} only supports: ${[...allowed.values()].join(", ")}. Other functions (e.g. Back) do nothing on it, even when set by the official app.`);
  }
  const blocks = {[BUTTON_BLOCK]: Array.from(btn), [DPI_BLOCK]: Array.from(dpi)}, slot = {};
  for (const [num, , blk, idx] of BUTTON_SLOTS) slot[num] = [blk, idx];
  for (const [num, code] of Object.entries(changes)) {
    if (!(num in slot)) throw new InstantError(`no button ${num}; buttons are 1-8`);
    if (!(code in FUNCTION_NAMES)) throw new InstantError(`function 0x${code.toString(16).padStart(2, "0")} is not a known function`);
    const [blk, idx] = slot[num];
    if (Object.values(KEEP_ONLY_FUNCTIONS).includes(code) && (blocks[blk][idx] & 0x1F) !== code)
      throw new InstantError(`button ${num}: blazectl can keep an existing ${FUNCTION_NAMES[code]} binding but can't create one yet; bind it in the official app for now.`);
    blocks[blk][idx] = (blocks[blk][idx] & 0xE0) | code;
  }
  const nb = blocks[BUTTON_BLOCK], nd = blocks[DPI_BLOCK];
  if (!Object.values(decodeButtons(nb, nd)).includes(BUTTON_FUNCTIONS["left-click"])) throw new InstantError("refusing: at least one button must stay Left Click");
  return [nb, nd];
}

// ------------------------------------------------------------------ macros (macro.py)
const MACRO_BASE = 0x400, DELAY_STEP = 0x7F, DOWN_BIT = 0x80, MAX_PAIRS = 0x7F;
const MACRO_TIMES = 0x0D, MACRO_HOLD = 0x0E, MEDIA_CODE = 0x0F, SPECIAL_CODES = [MACRO_TIMES, MACRO_HOLD, MEDIA_CODE];
const ENTRY_BASE = 0xA1, ENTRY_STRIDE = 10, ENTRY_SLOTS = 8, POINTERS = [0x6E, 0x70, 0x72, 0x74, 0x79], MACRO_LIMIT = 0x800;
const entryAddr = s => ENTRY_BASE + ENTRY_STRIDE * s;
const KEY_NAMES = {};
"abcdefghijklmnopqrstuvwxyz".split("").forEach((c, i) => { KEY_NAMES[0x04 + i] = c.toUpperCase(); });
"1234567890".split("").forEach((c, i) => { KEY_NAMES[0x1E + i] = c; });
Object.assign(KEY_NAMES, {0x28: "Enter", 0x29: "Esc", 0x2A: "Backspace", 0x2B: "Tab", 0x2C: "Space", 0x2D: "-", 0x2E: "=",
  0x2F: "[", 0x30: "]", 0x31: "\\", 0x33: ";", 0x34: "'", 0x35: "`", 0x36: ",", 0x37: ".", 0x38: "/", 0x39: "CapsLock",
  0x46: "PrintScreen", 0x47: "ScrollLock", 0x48: "Pause", 0x49: "Insert", 0x4A: "Home", 0x4B: "PageUp", 0x4C: "Delete",
  0x4D: "End", 0x4E: "PageDown", 0x4F: "Right", 0x50: "Left", 0x51: "Down", 0x52: "Up", 0x53: "NumLock", 0x54: "KP/",
  0x55: "KP*", 0x56: "KP-", 0x57: "KP+", 0x58: "KPEnter", 0xE0: "LCtrl", 0xE1: "LShift", 0xE2: "LAlt", 0xE3: "LWin",
  0xE4: "RCtrl", 0xE5: "RShift", 0xE6: "RAlt", 0xE7: "RWin"});
for (let i = 0; i < 12; i++) KEY_NAMES[0x3A + i] = `F${i + 1}`;
"1234567890".split("").forEach((c, i) => { KEY_NAMES[0x59 + i] = `KP${c}`; });
const KEY_CODES = {};
for (const [k, v] of Object.entries(KEY_NAMES)) KEY_CODES[v.toLowerCase()] = +k;
const MEDIA_KEYS = {"volume-up": [0x0E9, "Volume Up"], "volume-down": [0x0EA, "Volume Down"], "mute": [0x0E2, "Mute"],
  "play-pause": [0x0CD, "Play/Pause"], "next-track": [0x0B5, "Next Track"], "prev-track": [0x0B6, "Previous Track"],
  "stop": [0x0B7, "Stop"], "browser": [0x196, "Browser"], "media-player": [0x183, "Media Player"], "mail": [0x18A, "Mail"],
  "calculator": [0x192, "Calculator"], "my-computer": [0x194, "My Computer"]};
const MEDIA_BY_USAGE = {};
for (const [n, [u]] of Object.entries(MEDIA_KEYS)) MEDIA_BY_USAGE[u] = n;
const MODIFIERS = {ctrl: 0xE0, shift: 0xE1, alt: 0xE2, win: 0xE3}, SHORTCUT_DELAY = 4;

function keyCode(name) {
  const s = String(name).trim();
  let code;
  if (s.toLowerCase().startsWith("0x")) code = parseInt(s, 16);
  else if (s.toLowerCase() in KEY_CODES) code = KEY_CODES[s.toLowerCase()];
  else throw new MacroError(`unknown key '${name}'`);
  if (!(code >= 0x04 && code <= 0xE7)) throw new MacroError(`key code 0x${code.toString(16).padStart(2, "0")} is out of range`);
  return code;
}
const keyName = k => KEY_NAMES[k] || `0x${k.toString(16).padStart(2, "0")}`;
function decodeEvents(data) {
  const events = []; let pairs = 0;
  for (let i = 0; i + 1 < data.length; i += 2) {
    const t = data[i], k = data[i + 1];
    if (t === 0 && k === 0) return [events, pairs];
    pairs++;
    const delay = t & DELAY_STEP;
    if (k === 0 && !(t & DOWN_BIT)) {
      if (!events.length) throw new MacroError("macro starts with a delay filler");
      events[events.length - 1].delay += delay;
      continue;
    }
    events.push({key: k, down: !!(t & DOWN_BIT), delay});
  }
  throw new MacroError("no end marker (00 00) found");
}
function encodeEvents(events) {
  const out = [];
  for (const ev of events) {
    if (!(ev.key >= 0x04 && ev.key <= 0xE7)) throw new MacroError(`key code 0x${ev.key.toString(16).padStart(2, "0")} is out of range`);
    if (ev.delay < 0) throw new MacroError("negative delay");
    const fillers = Math.floor(ev.delay / DELAY_STEP), first = ev.delay % DELAY_STEP;
    out.push((ev.down ? DOWN_BIT : 0) | first, ev.key);
    for (let f = 0; f < fillers; f++) out.push(DELAY_STEP, 0);
  }
  if (out.length / 2 > MAX_PAIRS) throw new MacroError(`macro needs ${out.length / 2} pairs, limit is ${MAX_PAIRS}`);
  return out.concat([0, 0]);
}
// bindings: {type:"macro", events:[{key,down,delay}], mode, repeat} | {type:"media", key} | {type:"shortcut", mods, key(code)}
function validate(b) {
  if (b.type === "media") { if (!(b.key in MEDIA_KEYS)) throw new MacroError(`unknown media key '${b.key}'; use one of ${Object.keys(MEDIA_KEYS).join(", ")}`); return; }
  if (b.type === "shortcut") {
    const bad = b.mods.filter(m => !(m in MODIFIERS));
    if (bad.length) throw new MacroError(`unknown modifier '${bad[0]}'; use ctrl, shift, alt, win`);
    if (!(b.key >= 0x04 && b.key <= 0xE7) || Object.values(MODIFIERS).includes(b.key)) throw new MacroError("a shortcut needs one normal key (not only modifiers)");
    return;
  }
  if (!b.events.length) throw new MacroError("a macro needs at least one event");
  if (b.mode !== "times" && b.mode !== "hold") throw new MacroError("mode must be 'times' or 'hold'");
  if (!(b.repeat >= 1 && b.repeat <= 255)) throw new MacroError("repeat must be 1-255");
}
const codeOf = b => b.type === "media" ? MEDIA_CODE : b.type === "shortcut" ? MACRO_HOLD : (b.mode === "hold" ? MACRO_HOLD : MACRO_TIMES);
const shortcutEvents = b => Object.keys(MODIFIERS).filter(m => b.mods.includes(m)).map(m => ({key: MODIFIERS[m], down: true, delay: SHORTCUT_DELAY}))
  .concat([{key: b.key, down: true, delay: SHORTCUT_DELAY}]);
function blob(b) {
  validate(b);
  if (b.type === "shortcut") return [1, 0].concat(encodeEvents(shortcutEvents(b)));
  return [b.mode === "times" ? b.repeat : 1, 0].concat(encodeEvents(b.events));
}
function toJson(b) {
  if (b.type === "media") return {type: "media", key: b.key};
  if (b.type === "shortcut") return {type: "shortcut", mods: Object.keys(MODIFIERS).filter(m => b.mods.includes(m)), key: keyName(b.key)};
  return {mode: b.mode, repeat: b.repeat, events: b.events.map(e => [keyName(e.key), e.down ? "down" : "up", e.delay])};
}
function bindingFromJson(d) {
  const kind = d.type || "macro";
  let b;
  try {
    if (kind === "media") b = {type: "media", key: String(d.key)};
    else if (kind === "shortcut") b = {type: "shortcut", mods: (d.mods || []).map(m => String(m).toLowerCase()), key: keyCode(String(d.key))};
    else if (kind === "macro") {
      if (!Array.isArray(d.events)) throw new MacroError("incomplete macro: 'events'");
      b = {type: "macro", mode: String(d.mode ?? "times"), repeat: parseInt(d.repeat ?? 1, 10),
           events: d.events.map(([k, a, ms]) => ({key: keyCode(String(k)), down: String(a).toLowerCase() === "down", delay: parseInt(ms, 10)}))};
    } else throw new MacroError(`unknown binding type '${kind}'`);
  } catch (e) { if (e instanceof MacroError) throw e; throw new MacroError(`incomplete ${kind}: ${e.message}`); }
  validate(b);
  return b;
}
function parseEntry(e) {
  if (!e.some(x => x)) return null;
  return {pairs: e[0] & 0x7F, hold: !!(e[0] & 0x80), addr: e[4] | (e[5] << 8), usage: e[2] | (e[3] << 8), tag: e[7]};
}
function makeEntry(b, addr, pairs = 0) {
  if (b.type === "media") { const u = MEDIA_KEYS[b.key][0]; return [0, 0xFF, u & 0xFF, u >> 8, addr & 0xFF, addr >> 8, 0, 0, 0, 0]; }
  if (b.type === "shortcut") return [pairs | 0x80, 0xFF, 0xB7, 0, addr & 0xFF, addr >> 8, 0, 0, 0, 0];
  return [pairs | (b.mode === "hold" ? 0x80 : 0), 0xFF, 0, 0, addr & 0xFF, addr >> 8, 0, 0x0A, 0, 0];
}
function readBindings(mem, codes) {
  const out = {};
  for (const [slotS, code] of Object.entries(codes)) {
    const slot = +slotS;
    if (!SPECIAL_CODES.includes(code) || slot >= ENTRY_SLOTS) continue;
    const e = parseEntry(Array.from(mem.slice(entryAddr(slot), entryAddr(slot) + ENTRY_STRIDE)));
    if (!e) throw new MacroError(`slot ${slot} has function 0x${code.toString(16).padStart(2, "0")} but no entry`);
    if (code === MEDIA_CODE) {
      const name = MEDIA_BY_USAGE[e.usage];
      if (!name) throw new MacroError(`slot ${slot}: media usage 0x${e.usage.toString(16).padStart(3, "0")} isn't known yet`);
      out[slot] = {type: "media", key: name}; continue;
    }
    const [events, pairs] = decodeEvents(Array.from(mem.slice(e.addr + 2, e.addr + 4 + 2 * MAX_PAIRS)));
    if (pairs !== e.pairs) throw new MacroError(`slot ${slot}: entry says ${e.pairs} pairs, data at 0x${e.addr.toString(16).padStart(3, "0")} has ${pairs}`);
    const hold = code === MACRO_HOLD;
    if (hold && e.tag === 0 && events.length && events.every(ev => ev.down)) {
      const mods = Object.entries(MODIFIERS).filter(([, c]) => events.some(ev => ev.key === c)).map(([m]) => m);
      const keys = events.map(ev => ev.key).filter(k => !Object.values(MODIFIERS).includes(k));
      if (keys.length === 1) { out[slot] = {type: "shortcut", mods, key: keys[0]}; continue; }
    }
    out[slot] = {type: "macro", events, mode: hold ? "hold" : "times", repeat: hold ? 1 : (mem[e.addr] || 1)};
  }
  return out;
}
function layout(mem, bindings) {
  const addrOf = new Map(); let pos = MACRO_BASE; const entries = {};
  for (const slot of Object.keys(bindings).map(Number).sort((a, b) => a - b)) {
    const b = bindings[slot];
    if (b.type === "media") { validate(b); entries[slot] = makeEntry(b, pos); continue; }
    const bl = blob(b), key = bl.join(",");
    if (!addrOf.has(key)) { addrOf.set(key, [pos, bl]); pos += bl.length; }
    entries[slot] = makeEntry(b, addrOf.get(key)[0], (bl.length - 4) / 2);
  }
  if (pos + 0x28 > MACRO_LIMIT) throw new MacroError(`macros need ${pos - MACRO_BASE} bytes, limit is ${MACRO_LIMIT - MACRO_BASE - 0x28}`);
  for (const [a, bl] of addrOf.values()) bl.forEach((v, i) => { mem[a + i] = v; });
  for (let slot = 0; slot < ENTRY_SLOTS; slot++) { const ea = entryAddr(slot), en = entries[slot] || new Array(ENTRY_STRIDE).fill(0); en.forEach((v, i) => { mem[ea + i] = v; }); }
  POINTERS.forEach((pa, k) => { const v = pos + 8 * k; mem[pa] = v & 0xFF; mem[pa + 1] = v >> 8; });
  return pos;
}

// ------------------------------------------------------------------ the mouse (InstantMouse)
const sleep = ms => new Promise(r => setTimeout(r, ms));
class InstantMouse {
  constructor(dev, pid = null, sleepFn = sleep) { this.dev = dev; this.pid = pid; this.sleep = sleepFn; }
  async send(p) { await this.dev.setFeature(p); }
  async setLighting(mode, colors, speed, brightness, direction) {
    const pkts = lightingPackets(mode, colors, speed, brightness, direction);
    for (const p of pkts) { await this.send(p); await this.sleep(5); }
    return pkts;
  }
  async readBlock(addr, n = 8) {
    const out = [];
    for (let i = 0; i < n; i++) {
      await this.send(memoryPacket(MEM_READ, i, addr, 0, n - 1));
      await this.sleep(2);
      const resp = await this.dev.getFeature();
      if (resp.length < 2) throw new InstantError(`short reply reading 0x${addr.toString(16)}+${i}: ${hex(resp)}`);
      out.push(resp[1]);
    }
    await this.send(memoryPacket(MEM_IDLE, 0, 0, 0, 0));
    await this.sleep(2);
    return out;
  }
  async dump(end, start = 0, progress = null) {
    const out = {};
    for (let a = start; a < end; a += 8) { out[a] = await this.readBlock(a); if (progress) progress(a + 8 - start, end - start); }
    return out;
  }
  async checkHeader(header = null) {
    header = header || await this.readBlock(HEADER_BLOCK);
    if (!eq(header.slice(0, 3), HEADER_MAGIC))
      return [false, `header is ${hex(header)}, expected it to start with 5a 03 a5. The memory layout differs from the reference mouse, so DPI writes stay locked.`];
    return [true, `header ${hex(header)} matches the Instant layout`];
  }
  async writeBlock(addr, data) {
    if (!WRITABLE_BLOCKS.has(addr) && !MACRO_BLOCKS.has(addr)) throw new InstantError(`refusing to write block 0x${addr.toString(16).padStart(2, "0")}; it isn't a decoded settings block`);
    const n = data.length;
    for (let i = 0; i < n; i++) await this.send(memoryPacket(MEM_WRITE, i, addr, data[i], n - 1));
    await this.sleep(4);
    await this.send(memoryPacket(MEM_COMMIT, 0, addr, 0, n - 1));
    await this.sleep(2);
    await this.send([REPORT_ID, CMD_MEMORY, MEM_IDLE, 0, addr & 0xFF, addr >> 8, 0, 0]);
    await this.sleep(2);
  }
  async reload() { await this.send(memoryPacket(MEM_RELOAD, 0, 0, 0, 0)); await this.sleep(5); await this.send(memoryPacket(MEM_IDLE, 0, 0, 0, 0)); await this.sleep(50); }
  async verifiedWrite(addr, before, after) {
    await this.writeBlock(addr, after);
    const check = await this.readBlock(addr);
    if (!eq(check, after)) {
      await this.writeBlock(addr, before);
      const restored = await this.readBlock(addr);
      const state = eq(restored, before) ? "restored" : `RESTORE ALSO FAILED (now ${hex(restored)})`;
      throw new InstantError(`mouse did not store block 0x${addr.toString(16).padStart(2, "0")}: wrote ${hex(after)}, read back ${hex(check)}; previous value ${state}. If another program is using the mouse at the same time (the EvoFox app, BLAZE Web in a browser, or BLAZE Control while the web page is open), close it and try again.`);
    }
    return check;
  }
  async requireHeader() { const [ok, msg] = await this.checkHeader(); if (!ok) throw new InstantError(msg); }
  async getDpi() {
    const dpi = await this.readBlock(DPI_BLOCK), state = await this.readBlock(STATE_BLOCK), codes = decodeDpi(dpi);
    return {codes, dpi: codes.map(c => codeToDpi(c, this.pid)), active: state[0] & 7};
  }
  async getPollingByte() { return (await this.readBlock(STATE_BLOCK))[POLL_BYTE]; }
  async setActiveLevel(level) {
    if (!(level >= 0 && level < DPI_LEVELS)) throw new InstantError(`level must be 1-${DPI_LEVELS}`);
    await this.requireHeader();
    const before = await this.readBlock(STATE_BLOCK);
    if ((before[0] & 7) >= DPI_LEVELS) throw new InstantError(`current state block ${hex(before)} looks unexpected; refusing to write.`);
    if (!((before[1] >> level) & 1)) throw new InstantError(`level ${level + 1} is disabled in the mouse (enabled mask 0x${before[1].toString(16).padStart(2, "0")})`);
    const after = before.slice(); after[0] = (before[0] & 0xF8) | level;
    if (eq(after, before)) { await this.reload(); return {changed: false}; }
    await this.verifiedWrite(STATE_BLOCK, before, after); await this.reload();
    return {changed: true};
  }
  async setPollingByte(value, applyNow = true) {
    if (!Object.values(POLL_RATES).includes(value)) throw new InstantError(`polling byte must be one of [${Object.values(POLL_RATES).sort((a, b) => a - b).join(", ")}]`);
    await this.requireHeader();
    const before = await this.readBlock(STATE_BLOCK);
    if (!Object.values(POLL_RATES).includes(before[POLL_BYTE])) throw new InstantError(`current state block ${hex(before)} looks unexpected; refusing to write.`);
    const after = before.slice(); after[POLL_BYTE] = value;
    if (eq(after, before)) return {changed: false};
    await this.verifiedWrite(STATE_BLOCK, before, after);
    if (applyNow) await this.reload();
    return {changed: true};
  }
  async macroImage() {
    const mem = new Array(MACRO_LIMIT).fill(0);
    const put = (a, blk) => blk.forEach((v, i) => { mem[a + i] = v; });
    const addrs = [BUTTON_BLOCK, 0x68, 0x70, 0x78]; for (let a = 0xA0; a < 0xF8; a += 8) addrs.push(a);
    for (const a of addrs) put(a, await this.readBlock(a));
    const codes = {}; for (let i = 0; i < 8; i++) codes[i] = mem[BUTTON_BLOCK + i] & 0x1F;
    const done = new Set();
    for (let slot = 0; slot < ENTRY_SLOTS; slot++) {
      const e = parseEntry(mem.slice(entryAddr(slot), entryAddr(slot) + ENTRY_STRIDE));
      if (!e || !SPECIAL_CODES.includes(codes[slot])) continue;
      const end = e.addr + 4 + 2 * e.pairs;
      if (!(e.addr >= 0x400 && e.addr < end && end <= MACRO_LIMIT)) throw new InstantError(`macro entry for slot ${slot} points outside the macro area (0x${e.addr.toString(16)})`);
      for (let a = e.addr & ~7; a < end; a += 8) if (!done.has(a)) { put(a, await this.readBlock(a)); done.add(a); }
    }
    try { return [mem, readBindings(mem, codes)]; }
    catch (err) { if (err instanceof MacroError) throw new InstantError(`can't read the macros on the mouse: ${err.message}`); throw err; }
  }
  async getMacros() {
    const [, bound] = await this.macroImage(), out = {};
    for (const [num, , blk, idx] of BUTTON_SLOTS) if (blk === BUTTON_BLOCK && idx in bound) out[num] = bound[idx];
    return out;
  }
  async setMacros(changes, applyNow = true) {
    await this.requireHeader();
    const [mem, bound] = await this.macroImage(), dpi = await this.readBlock(DPI_BLOCK);
    const current = decodeButtons(mem.slice(BUTTON_BLOCK, BUTTON_BLOCK + 8), dpi);
    const unknown = Object.entries(current).filter(([, c]) => !(c in FUNCTION_NAMES));
    if (unknown.length) throw new InstantError(`current mapping has functions blazectl doesn't know yet ${JSON.stringify(Object.fromEntries(unknown))}; refusing to write.`);
    const slotOf = {}; for (const [num, , blk, idx] of BUTTON_SLOTS) if (blk === BUTTON_BLOCK) slotOf[num] = idx;
    const next = {...bound}, btn = mem.slice(BUTTON_BLOCK, BUTTON_BLOCK + 8);
    for (const [numS, v] of Object.entries(changes)) {
      const num = +numS;
      if (!(num in MACRO_BUTTONS)) throw new InstantError(`button ${num} can't hold a macro; use buttons [2, 3, 4, 5, 6, 7]`);
      const slot = slotOf[num];
      if (typeof v === "object") {
        try { validate(v); } catch (e) { throw new InstantError(e.message); }
        next[slot] = v; btn[slot] = (btn[slot] & 0xE0) | codeOf(v);
      } else {
        if (SPECIAL_CODES.includes(v) || !(v in FUNCTION_NAMES)) throw new InstantError(`button ${num}: 0x${v.toString(16).padStart(2, "0")} is not a plain function`);
        delete next[slot]; btn[slot] = (btn[slot] & 0xE0) | v;
      }
    }
    if (!Object.values(decodeButtons(btn, dpi)).includes(BUTTON_FUNCTIONS["left-click"])) throw new InstantError("refusing: at least one button must stay Left Click");
    let end;
    try { end = layout(mem.slice(), next); } catch (e) { throw new InstantError(e.message); }
    const dataBlocks = []; for (let a = 0x400; a < ((end + 7) & ~7); a += 8) dataBlocks.push(a);
    for (const a of dataBlocks) { const b = await this.readBlock(a); b.forEach((v, i) => { mem[a + i] = v; }); }
    const after = mem.slice();
    layout(after, next);
    btn.forEach((v, i) => { after[BUTTON_BLOCK + i] = v; });
    let changed = false;
    const order = [...dataBlocks]; for (let a = 0xA0; a < 0xF8; a += 8) order.push(a); order.push(0x68, 0x70, 0x78, BUTTON_BLOCK);
    for (const a of order) {
      const old = mem.slice(a, a + 8), nw = after.slice(a, a + 8);
      if (!eq(old, nw)) { await this.verifiedWrite(a, old, nw); changed = true; }
    }
    if (changed && applyNow) await this.reload();
    return {changed, end, macros: await this.getMacros()};
  }
  async getButtons() { return decodeButtons(await this.readBlock(BUTTON_BLOCK), await this.readBlock(DPI_BLOCK)); }
  async setButtons(changes, applyNow = true) {
    await this.requireHeader();
    let btnBefore = await this.readBlock(BUTTON_BLOCK);
    const dpiBefore = await this.readBlock(DPI_BLOCK), current = decodeButtons(btnBefore, dpiBefore);
    const unknown = Object.entries(current).filter(([, c]) => !(c in FUNCTION_NAMES));
    if (unknown.length) throw new InstantError(`current mapping has functions blazectl doesn't know yet ${JSON.stringify(Object.fromEntries(unknown))}; reset them in the official app first, refusing to write.`);
    const special = [0x0D, 0x0E, 0x0F], unbind = {}, rest = {};
    for (const [n, c] of Object.entries(changes)) (special.includes(current[n]) && !special.includes(c) ? unbind : rest)[n] = c;
    let changed = false;
    if (Object.keys(unbind).length) {
      changed = (await this.setMacros(unbind, false)).changed;
      btnBefore = await this.readBlock(BUTTON_BLOCK);
    }
    const [btnAfter, dpiAfter] = encodeButtons(btnBefore, dpiBefore, rest);
    if (!eq(btnAfter, btnBefore)) { await this.verifiedWrite(BUTTON_BLOCK, btnBefore, btnAfter); changed = true; }
    if (!eq(dpiAfter, dpiBefore)) { await this.verifiedWrite(DPI_BLOCK, dpiBefore, dpiAfter); changed = true; }
    if (changed && applyNow) await this.reload();
    return {changed, buttons: decodeButtons(await this.readBlock(BUTTON_BLOCK), await this.readBlock(DPI_BLOCK))};
  }
  async setDpi(codes, applyNow = true) {
    await this.requireHeader();
    const before = await this.readBlock(DPI_BLOCK), current = decodeDpi(before);
    if (!current.every(c => DPI_CODES.includes(c))) throw new InstantError(`current DPI block ${hex(before)} decodes to [${current.join(", ")}], which is not a valid set. The layout may differ on this mouse; refusing to write.`);
    const after = encodeDpi(before, codes);
    if (eq(after, before)) return {changed: false, codes: current};
    const check = await this.verifiedWrite(DPI_BLOCK, before, after);
    if (applyNow) await this.reload();
    return {changed: true, codes: decodeDpi(check)};
  }
}

// ------------------------------------------------------------------ storage (browser: localStorage)
function memoryStorage() { const m = new Map(); return {getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v))}; }
function jsonGet(st, key, dflt) { try { const v = st.getItem(key); return v == null ? dflt : JSON.parse(v); } catch { return dflt; } }
const DEFAULT_LIGHTING = () => ({mode: "static", colors: new Array(6).fill("#ff0000"), speed: 2, brightness: BRIGHTNESS_MAX, direction: 0});
const PROFILE_SLOTS = ["office", "game1", "game2", "media"];
const MODE_LABELS = {"static": "Static", "breathing": "Breathing", "breathing-multi": "Breathing RGB", "rainbow": "Rainbow wave",
  "spectrum": "Spectrum", "fill": "Fill & drain", "loop": "Loop", "enraptured": "Enraptured", "flicker": "Flicker", "ripple": "Ripple",
  "startrek": "Star Trek", "off": "Off"};

// ------------------------------------------------------------------ the API the page calls (webui.Api)
class WebApi {
  constructor({device = null, pid = 0x1440, name = "INSTANT USB Gaming Mouse", storage = null, sleepFn = sleep} = {}) {
    this.device = device; this.pid = pid; this.name = name; this.sleep = sleepFn;
    this.st = storage || memoryStorage();
    this.chain = Promise.resolve();
  }
  setDevice(device, pid, name) { this.device = device; if (pid) this.pid = pid; if (name) this.name = name; }
  async run(fn) {
    if (!this.device) throw new ServiceError("Mouse not connected. Click Connect and pick “INSTANT USB Gaming Mouse”. " +
      "First close BLAZE Control, stop the BLAZE Agent and quit the EvoFox app: only one program can talk to the mouse at a time.");
    try { return await fn(new InstantMouse(this.device, this.pid, this.sleep)); }
    catch (e) { if (e instanceof InstantError || e instanceof MacroError) throw new ServiceError(e.message); if (e instanceof ServiceError) throw e;
      throw new ServiceError(`USB error: ${e && e.message ? e.message : e}. Unplug and replug the mouse, close other mouse ` +
        "software (BLAZE Control, BLAZE Agent, the EvoFox app), then click Connect again."); }
  }
  loadLighting() { const l = jsonGet(this.st, "blazectl.lighting", null); return l && l.mode ? l : DEFAULT_LIGHTING(); }
  saveLighting(l) { this.st.setItem("blazectl.lighting", JSON.stringify(l)); }
  async relight() { const s = this.loadLighting(); await this.run(m => m.setLighting(s.mode, s.colors, s.speed, s.brightness, s.direction)); }
  async readState() {
    return this.run(async m => {
      const [ok, msg] = await m.checkHeader();
      if (!ok) throw new ServiceError(msg);
      const dpi = await m.getDpi(), pb = await m.getPollingByte();
      const poll = Object.entries(POLL_RATES).find(([, v]) => v === pb);
      let macros = {}, merr = null;
      try { macros = await m.getMacros(); } catch (e) { if (e instanceof InstantError) merr = e.message; else throw e; }
      return {dpi: dpi.dpi, active: dpi.active, polling: poll ? +poll[0] : null, buttons: await m.getButtons(), macros, merr};
    });
  }
  async state() {
    const light = this.loadLighting();
    let st;
    try { st = await this.readState(); }
    catch (e) { if (e instanceof ServiceError) return {connected: false, error: e.message, lighting: light, official_app: false}; throw e; }
    const special = b => !b ? null : b.type === "media" ? "media" : b.type === "shortcut" ? "shortcut" : null;
    const buttons = {}, macros = {};
    for (const [n, c] of Object.entries(st.buttons)) buttons[n] = special(st.macros[n]) || FUNCTION_NAMES[c] || `unknown-0x${c.toString(16).padStart(2, "0")}`;
    for (const [n, b] of Object.entries(st.macros)) macros[n] = toJson(b);
    return {connected: true, name: this.name, usb_id: `30fa:${this.pid.toString(16).padStart(4, "0")}`, dpi: st.dpi,
            active_level: st.active, polling: st.polling, buttons, macros, macro_error: st.merr, lighting: light, official_app: false};
  }
  handle(path, body = {}) {
    const p = this.chain.then(() => this._handle(path, body));      // one USB conversation at a time
    this.chain = p.catch(() => {});
    return p;
  }
  async _handle(path, body) {
    const NO_STATE = ["/api/state", "/api/profiles", "/api/profiles/save", "/api/macros/library", "/api/macros/library/save", "/api/macros/library/delete"];
    const r = await this._route(path, body);
    if (!NO_STATE.includes(path)) r.state = await this.state();
    return r;
  }
  async _route(path, body) {
    if (path === "/api/state") return this.state();
    if (path === "/api/lighting") {
      const s = {mode: String(body.mode), colors: body.colors.map(String).slice(0, DPI_LEVELS), speed: +body.speed, brightness: +body.brightness, direction: +body.direction};
      try { lightingPackets(s.mode, s.colors, s.speed, s.brightness, s.direction); } catch (e) { throw new ServiceError(e.message); }
      await this.run(m => m.setLighting(s.mode, s.colors, s.speed, s.brightness, s.direction));
      this.saveLighting(s);
      return {message: `Lighting set to ${MODE_LABELS[s.mode] || s.mode}`};
    }
    if (path === "/api/dpi") {
      const values = body.dpi.map(Number);
      if (values.length !== DPI_LEVELS) throw new ServiceError(`need ${DPI_LEVELS} DPI values`);
      let codes; try { codes = values.map(v => dpiToCode(v, this.pid)); } catch (e) { throw new ServiceError(e.message); }
      const res = await this.run(m => m.setDpi(codes));
      if (res.changed) await this.relight();
      return {message: "DPI saved and verified: " + res.codes.map(c => codeToDpi(c, this.pid)).join(" · ")};
    }
    if (path === "/api/active") {
      const level = parseInt(body.level, 10);
      if (!(level >= 1 && level <= DPI_LEVELS)) throw new ServiceError(`level must be 1-${DPI_LEVELS}`);
      await this.run(m => m.setActiveLevel(level - 1));
      await this.relight();
      return {message: `Level ${level} is now active`};
    }
    if (path === "/api/polling") {
      const rate = parseInt(body.rate, 10);
      if (!(rate in POLL_RATES)) throw new ServiceError(`polling rate must be one of [125, 250, 500, 1000]`);
      const res = await this.run(m => m.setPollingByte(POLL_RATES[rate]));
      if (res.changed) await this.relight();
      return {message: `Polling rate ${res.changed ? "saved and verified" : "already"}: ${rate} Hz`};
    }
    if (path === "/api/buttons") {
      const known = {...BUTTON_FUNCTIONS, ...KEEP_ONLY_FUNCTIONS, "shortcut": MACRO_HOLD}, mapping = {};
      for (const [num, name] of Object.entries(body.buttons)) {
        if (!(name in known)) throw new ServiceError(`unknown function '${name}' for button ${num}`);
        mapping[+num] = known[name];
      }
      const res = await this.run(m => m.setButtons(mapping));
      if (res.changed) await this.relight();
      return {message: "Buttons saved and verified"};
    }
    if (path === "/api/macros") {
      const bind = {}, unbind = {};
      for (const [n, d] of Object.entries(body.bind || {})) {
        try { bind[+n] = bindingFromJson(d); } catch (e) { throw new ServiceError(`button ${n}: macro not accepted (${e.message})`); }
      }
      for (const [n, name] of Object.entries(body.unbind || {})) {
        if (!(name in BUTTON_FUNCTIONS)) throw new ServiceError(`unknown function '${name}' for button ${n}`);
        unbind[+n] = BUTTON_FUNCTIONS[name];
      }
      if (Object.keys(bind).some(k => k in unbind)) throw new ServiceError("a button can't be bound and unbound in the same call");
      const res = await this.run(m => m.setMacros({...unbind, ...bind}));
      if (res.changed) await this.relight();
      const parts = Object.keys(bind).map(Number).sort((a, b) => a - b).map(n => `button ${n}`);
      const gone = Object.keys(unbind).map(Number).sort((a, b) => a - b).map(n => `button ${n}`);
      return {message: (parts.length ? "Saved to " + parts.join(", ") : "") + (parts.length && gone.length ? "; " : "") +
              (gone.length ? "cleared " + gone.join(", ") : "") + " · verified"};
    }
    if (path === "/api/buttons/reset") {
      const mapping = {}; for (const [num, , , , code] of BUTTON_SLOTS) mapping[num] = code;
      const res = await this.run(m => m.setButtons(mapping));
      if (res.changed) await this.relight();
      return {message: "Buttons reset to factory"};
    }
    if (path === "/api/macros/library") return {library: this.library()};
    if (path === "/api/macros/library/save") {
      let mac; try { mac = bindingFromJson(body.macro); if (mac.type !== "macro") throw new MacroError("not a macro"); }
      catch (e) { throw new ServiceError(`bad macro: ${e.message}`); }
      const name = String(body.name).trim();
      if (!name || name.length > 40) throw new ServiceError("macro name must be 1-40 characters");
      const lib = this.library();
      if (body.old_name && body.old_name !== name) delete lib[body.old_name];
      lib[name] = toJson(mac);
      this.st.setItem("blazectl.macros", JSON.stringify({macros: lib}));
      return {library: lib};
    }
    if (path === "/api/macros/library/delete") {
      const lib = this.library(); delete lib[String(body.name)];
      this.st.setItem("blazectl.macros", JSON.stringify({macros: lib}));
      return {library: lib};
    }
    if (path === "/api/profiles") return this.profiles();
    if (path === "/api/profiles/save") {
      const slot = String(body.slot || ""), data = this.profiles();
      if (!PROFILE_SLOTS.includes(slot)) throw new ServiceError(`unknown profile slot '${slot}'`);
      if (body.config != null) data.slots[slot] = this.validateProfile(body.config);
      if (body.active != null) { if (!PROFILE_SLOTS.includes(body.active)) throw new ServiceError(`unknown profile slot '${body.active}'`); data.active = body.active; }
      this.st.setItem("blazectl.profiles", JSON.stringify(data));
      return data;
    }
    if (path === "/api/backup") {
      const mem = await this.run(m => m.dump(0x800)), blocks = {};
      for (const [a, b] of Object.entries(mem)) blocks["0x" + (+a).toString(16).padStart(2, "0")] = b.map(x => x.toString(16).padStart(2, "0")).join("");
      return {message: "Backup created", backup: {usb_id: `30fa:${this.pid.toString(16).padStart(4, "0")}`, name: this.name, blocks}};
    }
    if (path.startsWith("/api/agent")) throw new ServiceError("Automation needs the desktop app (BLAZE Control). The browser can't run in the background.");
    const e = new ServiceError("not found"); e.notFound = true; throw e;
  }
  library() {
    const data = jsonGet(this.st, "blazectl.macros", {}), out = {};
    for (const [name, d] of Object.entries((data && data.macros) || {})) { try { out[name] = toJson(bindingFromJson(d)); } catch { /* skip damaged */ } }
    return out;
  }
  profiles() {
    const data = jsonGet(this.st, "blazectl.profiles", {}) || {}, slots = data.slots && typeof data.slots === "object" ? data.slots : {};
    const out = {}; for (const k of PROFILE_SLOTS) out[k] = slots[k] ?? null;
    return {active: PROFILE_SLOTS.includes(data.active) ? data.active : "office", slots: out};
  }
  validateProfile(c) {
    try {
      const dpi = c.dpi.map(v => parseInt(v, 10));
      if (dpi.length !== DPI_LEVELS || dpi.some(isNaN)) throw new Error("dpi must have 6 values");
      const buttons = {}; for (const [k, v] of Object.entries(c.buttons)) buttons[String(parseInt(k, 10))] = String(v);
      const L = c.lighting, lighting = {mode: String(L.mode), colors: L.colors.map(String).slice(0, DPI_LEVELS), speed: +L.speed, brightness: +L.brightness, direction: +L.direction};
      const macros = {}; for (const [k, v] of Object.entries(c.macros || {})) macros[String(parseInt(k, 10))] = toJson(bindingFromJson(v));
      return {dpi, polling: parseInt(c.polling, 10), buttons, lighting, macros};
    } catch (e) { throw new ServiceError(`invalid profile: ${e.message}`); }
  }
}

// ------------------------------------------------------------------ WebHID transport
class HidTransport {
  constructor(device) { this.device = device; }
  async setFeature(pkt) { await this.device.sendFeatureReport(pkt[0], new Uint8Array(pkt.slice(1))); }
  async getFeature() {
    const dv = await this.device.receiveFeatureReport(REPORT_ID);
    const b = new Uint8Array(dv.buffer, dv.byteOffset, dv.byteLength);
    // Chrome returns the report ID as byte 0 on some systems and not on others: normalise to [id, data...]
    return b.length >= PACKET_LEN && b[0] === REPORT_ID ? Array.from(b.slice(0, PACKET_LEN)) : [REPORT_ID, ...Array.from(b.slice(0, PACKET_LEN - 1))];
  }
}
const HID_FILTERS = [{vendorId: VID, usagePage: CONTROL_USAGE_PAGE}];
function hasControl(dev) { return dev.collections.some(c => c.usagePage === CONTROL_USAGE_PAGE); }

return {InstantMouse, WebApi, HidTransport, HID_FILTERS, hasControl, ServiceError, InstantError, MacroError,
        lightingPackets, encodeDpi, decodeDpi, encodeButtons, decodeButtons, encodeEvents, decodeEvents, layout,
        readBindings, bindingFromJson, toJson, VID, CONTROL_USAGE_PAGE, memoryStorage};
});
