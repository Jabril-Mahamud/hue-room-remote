// Qt.resolvedUrl() percent-encodes spaces/special characters (e.g. a space
// becomes %20); a plain "file://" strip alone would leave those in the
// filesystem path passed to python3/bash. Decode after stripping the
// scheme so a plugin/home directory containing such characters still
// resolves to a real, openable path.
function resolveScriptPath(fileUrl) {
  return decodeURIComponent(String(fileUrl).replace(/^file:\/\//, ""))
}

var API = typeof Qt !== "undefined"
  ? resolveScriptPath(Qt.resolvedUrl("hue_api.py").toString())
  : "hue_api.py"

function apiCmd(args) {
  var cmd = ["python3", API]
  for (var i = 0; i < args.length; i++) cmd.push(String(args[i]))
  return cmd
}

function isValidId(id) {
  return /^[a-zA-Z0-9_-]{1,40}$/.test(String(id))
}

// hue_api.py's write side (_write_order/_write_favorite) enforces this same
// id format before persisting hue-order.json/hue-favorite.json, but the
// read side (panel.qml's FileView onLoaded) previously trusted the file's
// shape without validating individual ids. These mirror that write-side
// validation for use on read, dropping invalid entries rather than
// rejecting the whole file -- appropriate for graceful degradation on read,
// as opposed to an atomic validated write. For legitimate data (always
// written by the already-validated Python writer) these are a no-op: every
// id already matches the pattern, so nothing gets filtered.
function sanitizeIdList(list) {
  return Array.isArray(list) ? list.map(String).filter(isValidId) : []
}

function sanitizeIdListMap(map) {
  if (!map || typeof map !== "object" || Array.isArray(map)) return {}
  var out = {}
  for (var k in map) {
    if (!Object.prototype.hasOwnProperty.call(map, k)) continue
    if (!isValidId(k)) continue
    out[k] = sanitizeIdList(map[k])
  }
  return out
}

function sanitizeIdMap(map) {
  if (!map || typeof map !== "object" || Array.isArray(map)) return {}
  var out = {}
  for (var k in map) {
    if (!Object.prototype.hasOwnProperty.call(map, k)) continue
    if (!isValidId(k)) continue
    var v = String(map[k])
    if (isValidId(v)) out[k] = v
  }
  return out
}

// A malicious/compromised bridge shouldn't be able to force unbounded
// memory use or slow the panel to a crawl -- cap the raw text a bridge
// response can be before it's even handed to JSON.parse, and cap how many
// rooms/scenes/lights get parsed out of it below.
//
// Deliberately larger than hue_api.py's MAX_RESPONSE_BYTES (1 MiB): Python
// re-emits bridge data via json.dumps(ensure_ascii=True), which escapes
// every non-ASCII character as \uXXXX and can expand non-ASCII room/scene
// names up to ~6x -- worst case, a malicious bridge packs its 1 MiB
// allowance entirely with such content, re-encoding to ~6 MiB. The real
// DoS boundary against a malicious bridge is enforced upstream by
// MAX_RESPONSE_BYTES; this cap just needs enough headroom above that
// worst case (with room for JSON structure/quoting overhead) to avoid
// rejecting legitimate, already-bounded bridge output -- it's also reused
// (via parseJsonObject) to bound reads of the local hue-favorite.json/
// hue-order.json settings files in panel.qml.
var MAX_JSON_TEXT_LENGTH = 8 * 1024 * 1024
var MAX_PARSED_ITEMS = 500
var MAX_NAME_LENGTH = 200

// Room/scene names render through QML Text elements we don't all control
// directly -- some flow into shell-provided components (e.g. Toggle.label)
// whose internal Text has no textFormat override and defaults to
// Text.AutoText, which would interpret markup-like content as rich text.
// Stripping angle brackets at the source means it's inert wherever it's
// rendered, not just in the Text elements we happen to have hardened.
// Beyond angle brackets, also strip characters that can visually mislead
// without being "markup": C0 controls/DEL, zero-width space/ZWNJ/ZWJ, line/
// paragraph separators, bidi embedding/override/isolate characters, and the
// BOM/zero-width-no-break-space. Doesn't touch surrogate-pair ranges, so
// multi-byte emoji/astral characters pass through untouched.
function sanitizeName(name) {
  return String(name)
    .replace(/[<>]/g, "")
    .replace(/[\u0000-\u001F\u007F\u200B-\u200D\u2028\u2029\u202A-\u202E\u2066-\u2069\uFEFF]/g, "")
    .slice(0, MAX_NAME_LENGTH)
}

function parseStatus(text) {
  var obj = parseJsonObject(text)
  if (!obj) return null
  var bridgeId = String(obj.bridgeId || "").trim().toLowerCase()
  if (bridgeId && !isValidId(bridgeId)) bridgeId = ""
  // Only "ble" is recognised; anything else (including absent) means the
  // bridge transport, so an unexpected value can never suppress the
  // missing-bridgeId warning that bridge users depend on.
  var transport = obj.transport === "ble" ? "ble" : "bridge"
  return { paired: !!obj.paired, bridgeId: bridgeId, transport: transport }
}

function parseJsonObject(text) {
  var raw = String(text || "").trim()
  if (!raw || raw.length > MAX_JSON_TEXT_LENGTH) return null
  try {
    var parsed = JSON.parse(raw)
    return parsed && typeof parsed === "object" ? parsed : null
  } catch (e) {
    return null
  }
}

function parseGroups(text) {
  var obj = parseJsonObject(text)
  if (!obj) return []
  var groups = []
  var seen = 0
  for (var id in obj) {
    if (!Object.prototype.hasOwnProperty.call(obj, id)) continue
    if (seen >= MAX_PARSED_ITEMS) break
    seen++
    if (!isValidId(id)) continue
    var group = obj[id]
    if (!group || typeof group !== "object") continue
    var type = String(group.type || "")
    if (type !== "Room" && type !== "Zone") continue
    var action = group.action || {}
    var bri = typeof action.bri === "number" ? action.bri : 254
    var lightIds = Array.isArray(group.lights)
      ? group.lights.slice(0, MAX_PARSED_ITEMS).map(String).filter(isValidId)
      : []
    groups.push({
      id: String(id),
      name: sanitizeName(group.name || "Group " + id),
      type: type,
      class: String(group.class || "Other").slice(0, MAX_NAME_LENGTH),
      on: !!(group.state && group.state.any_on),
      allOn: !!(group.state && group.state.all_on),
      bri: Math.max(1, Math.min(254, bri)),
      lightIds: lightIds
    })
  }
  groups.sort(function(a, b) { return a.name.localeCompare(b.name) })
  return groups
}

// Scene swatches come from a file the user hand-writes and end up in a QML
// color property, so only an exact #rrggbb is let through.
var SWATCH_RE = /^#[0-9a-fA-F]{6}$/

function parseScenes(text) {
  var obj = parseJsonObject(text)
  if (!obj) return []
  var scenes = []
  var seen = 0
  for (var id in obj) {
    if (!Object.prototype.hasOwnProperty.call(obj, id)) continue
    if (seen >= MAX_PARSED_ITEMS) break
    seen++
    if (!isValidId(id)) continue
    var scene = obj[id]
    if (!scene || typeof scene !== "object") continue
    var group = scene.group !== undefined && scene.group !== null ? String(scene.group) : ""
    if (!group || !isValidId(group)) continue
    var entry = {
      id: String(id),
      name: sanitizeName(scene.name || "Scene " + id),
      group: group
    }
    // Bridge scenes carry no section, so they all land under one heading.
    entry.section = typeof scene.section === "string" && scene.section
      ? sanitizeName(scene.section).slice(0, 40)
      : "Scenes"
    entry.sectionRank = typeof scene.sectionRank === "number" ? scene.sectionRank : 0
    if (typeof scene.swatch === "string" && SWATCH_RE.test(scene.swatch)) {
      entry.swatch = scene.swatch
    }
    scenes.push(entry)
  }
  scenes.sort(function(a, b) { return a.name.localeCompare(b.name) })
  return scenes
}

// Nerd Font glyphs verified against the installed font (a reduced "basic"
// build, not the full Material Design Icons set) by rendering each
// candidate codepoint and checking it visually -- most guessed codepoints
// turned out to be missing or wrong glyphs, so this only maps classes with
// a confirmed match. Everything else falls back to the plugin's own bulb
// icon, which every room already shows regardless.
var ROOM_CLASS_ICONS = {
  "Bedroom": "",
  "Kids bedroom": "",
  "Guest room": "",
  "Bathroom": "",
  "Toilet": "",
  "Office": "",
  "Computer": "",
  "Garden": "",
  "Balcony": "",
  "Terrace": "",
  "Porch": "",
  "Driveway": "",
  "Carport": "",
  "Garage": "",
  "TV": "",
  "Reading": "",
  "Storage": "",
  "Closet": "",
  "Home": "",
  "Barbecue": "",
  "Music": "",
  "Gym": "",
  "Nursery": ""
}
var ROOM_ICON_DEFAULT = "󰌵"

function roomIcon(className) {
  return ROOM_CLASS_ICONS[String(className || "")] || ROOM_ICON_DEFAULT
}

// Sorts `items` (each with an `id`) by position in `orderIds`, appending
// anything not listed at the end in its original order. `orderIds` is
// filtered down to ids actually present in `items` first, so stale ids
// (a room/scene since deleted on the bridge) don't inject phantom entries.
function applyOrder(items, orderIds) {
  var known = {}
  for (var i = 0; i < items.length; i++) known[items[i].id] = true
  var order = Array.isArray(orderIds) ? orderIds.filter(function(id) { return known[id] }) : []
  var rank = {}
  for (var j = 0; j < order.length; j++) rank[order[j]] = j
  var ranked = items.map(function(item, idx) {
    return { item: item, rank: Object.prototype.hasOwnProperty.call(rank, item.id) ? rank[item.id] : order.length + idx }
  })
  ranked.sort(function(a, b) { return a.rank - b.rank })
  return ranked.map(function(r) { return r.item })
}

function roomBrightness(text, lightIds) {
  var obj = parseJsonObject(text)
  if (!obj || !lightIds || lightIds.length === 0) return null
  var total = 0
  var count = 0
  for (var i = 0; i < lightIds.length; i++) {
    var light = obj[lightIds[i]]
    if (light && light.state && typeof light.state.bri === "number") {
      total += light.state.bri
      count++
    }
  }
  if (count === 0) return null
  return Math.max(1, Math.min(254, Math.round(total / count)))
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    apiCmd: apiCmd,
    resolveScriptPath: resolveScriptPath,
    isValidId: isValidId,
    sanitizeIdList: sanitizeIdList,
    sanitizeIdListMap: sanitizeIdListMap,
    sanitizeIdMap: sanitizeIdMap,
    parseStatus: parseStatus,
    parseJsonObject: parseJsonObject,
    parseGroups: parseGroups,
    parseScenes: parseScenes,
    roomIcon: roomIcon,
    applyOrder: applyOrder,
    roomBrightness: roomBrightness,
    MAX_JSON_TEXT_LENGTH: MAX_JSON_TEXT_LENGTH
  }
}
