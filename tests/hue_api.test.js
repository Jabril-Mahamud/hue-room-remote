const test = require("node:test")
const assert = require("node:assert/strict")
const HueApi = require("../hue_api.js")

test("resolveScriptPath strips the file:// scheme", () => {
  assert.equal(HueApi.resolveScriptPath("file:///home/user/plugin/hue_api.py"),
    "/home/user/plugin/hue_api.py")
})

test("resolveScriptPath decodes percent-encoded characters", () => {
  assert.equal(
    HueApi.resolveScriptPath("file:///home/user/My%20Plugins/hue_api.py"),
    "/home/user/My Plugins/hue_api.py")
})

test("isValidId accepts hyphenated Hue usernames", () => {
  assert.equal(HueApi.isValidId("abc-123_XYZ"), true)
  assert.equal(HueApi.isValidId("a".repeat(40)), true)
})

test("isValidId rejects empty, oversized, or invalid-character ids", () => {
  assert.equal(HueApi.isValidId(""), false)
  assert.equal(HueApi.isValidId("a".repeat(41)), false)
  assert.equal(HueApi.isValidId("has space"), false)
  assert.equal(HueApi.isValidId("has/slash"), false)
})

test("sanitizeIdList keeps valid ids and drops invalid ones", () => {
  assert.deepEqual(HueApi.sanitizeIdList(["1", "has space", "2"]), ["1", "2"])
})

test("sanitizeIdList returns an empty array for non-array input", () => {
  assert.deepEqual(HueApi.sanitizeIdList("not-a-list"), [])
  assert.deepEqual(HueApi.sanitizeIdList(undefined), [])
  assert.deepEqual(HueApi.sanitizeIdList({ "1": true }), [])
})

test("sanitizeIdListMap drops an invalid key and filters invalid entries from its list", () => {
  const result = HueApi.sanitizeIdListMap({
    "1": ["s1", "has space", "s2"],
    "has space": ["s3"]
  })
  assert.deepEqual(result, { "1": ["s1", "s2"] })
})

test("sanitizeIdListMap returns an empty object for non-object input", () => {
  assert.deepEqual(HueApi.sanitizeIdListMap(["not", "a", "map"]), {})
  assert.deepEqual(HueApi.sanitizeIdListMap(null), {})
})

test("sanitizeIdMap drops an entry with an invalid key or value", () => {
  const result = HueApi.sanitizeIdMap({
    "1": "s1",
    "2": "has space",
    "has space": "s2"
  })
  assert.deepEqual(result, { "1": "s1" })
})

test("sanitizeIdMap returns an empty object for non-object input", () => {
  assert.deepEqual(HueApi.sanitizeIdMap(["not", "a", "map"]), {})
  assert.deepEqual(HueApi.sanitizeIdMap(undefined), {})
})

test("parseStatus accepts a well-formed status", () => {
  const status = HueApi.parseStatus(JSON.stringify({ paired: true, bridgeId: "AABBCC" }))
  assert.deepEqual(status, { paired: true, bridgeId: "aabbcc", transport: "bridge" })
})

test("parseStatus reports unpaired", () => {
  assert.deepEqual(HueApi.parseStatus(JSON.stringify({ paired: false })), { paired: false, bridgeId: "", transport: "bridge" })
})

test("parseStatus returns null for malformed input", () => {
  assert.equal(HueApi.parseStatus(""), null)
  assert.equal(HueApi.parseStatus("not json"), null)
})

test("parseStatus drops an invalid bridgeId but keeps paired", () => {
  const status = HueApi.parseStatus(JSON.stringify({ paired: true, bridgeId: "has space" }))
  assert.deepEqual(status, { paired: true, bridgeId: "", transport: "bridge" })
})

test("parseStatus reports the ble transport", () => {
  const status = HueApi.parseStatus(JSON.stringify({ paired: true, transport: "ble" }))
  assert.deepEqual(status, { paired: true, bridgeId: "", transport: "ble" })
})

test("parseStatus treats an unknown transport as bridge", () => {
  // Anything but "ble" has to fall back to "bridge", otherwise a junk value
  // would suppress the missing-bridgeId warning bridge users rely on.
  for (const bogus of ["zigbee", "", 7, null, {}]) {
    const status = HueApi.parseStatus(JSON.stringify({ paired: true, bridgeId: "aabbcc", transport: bogus }))
    assert.equal(status.transport, "bridge")
  }
})

test("parseJsonObject rejects text over the size cap", () => {
  const huge = JSON.stringify({ a: "x".repeat(HueApi.MAX_JSON_TEXT_LENGTH) })
  assert.equal(HueApi.parseJsonObject(huge), null)
})

test("parseGroups keeps only Room/Zone types and sorts by name", () => {
  const groups = HueApi.parseGroups(JSON.stringify({
    "1": { name: "Zeta Room", type: "Room", lights: ["1"], action: { bri: 120 } },
    "2": { name: "Not a room", type: "LightGroup", lights: ["2"], action: { bri: 50 } },
    "3": { name: "Alpha Zone", type: "Zone", lights: [], action: { bri: 60 } }
  }))
  assert.equal(groups.length, 2)
  assert.equal(groups[0].name, "Alpha Zone")
  assert.equal(groups[1].name, "Zeta Room")
})

test("parseGroups exposes group brightness clamped to Hue's valid range", () => {
  const groups = HueApi.parseGroups(JSON.stringify({
    "1": { name: "Over", type: "Room", lights: ["1"], action: { bri: 999 } },
    "2": { name: "Under", type: "Room", lights: ["1"], action: { bri: 0 } },
    "3": { name: "Missing", type: "Room", lights: ["1"] }
  }))
  const byName = Object.fromEntries(groups.map(g => [g.name, g]))
  assert.equal(byName.Over.bri, 254)
  assert.equal(byName.Under.bri, 1)
  assert.equal(byName.Missing.bri, 254)
})

test("parseGroups reports on/off from state.any_on, not action.on", () => {
  const groups = HueApi.parseGroups(JSON.stringify({
    "1": { name: "Room", type: "Room", lights: ["1"], state: { any_on: true, all_on: false }, action: { on: false } }
  }))
  assert.equal(groups[0].on, true)
  assert.equal(groups[0].allOn, false)
})

test("parseGroups passes through the bridge's class field, defaulting to Other", () => {
  const groups = HueApi.parseGroups(JSON.stringify({
    "1": { name: "Office", type: "Room", lights: ["1"], class: "Office" },
    "2": { name: "Mystery", type: "Room", lights: ["1"] }
  }))
  const byName = Object.fromEntries(groups.map(g => [g.name, g]))
  assert.equal(byName.Office.class, "Office")
  assert.equal(byName.Mystery.class, "Other")
})

test("parseGroups drops entries with a malformed group id", () => {
  const groups = HueApi.parseGroups(JSON.stringify({
    "1": { name: "Fine", type: "Room", lights: ["1"] },
    "has space": { name: "Bad id", type: "Room", lights: ["1"] }
  }))
  assert.deepEqual(groups.map(g => g.id), ["1"])
})

test("parseGroups skips entries whose value is null or a non-object primitive", () => {
  // A corrupted bridge response or a Hue-compatible emulator could map a
  // (validly-formatted) id to null/a number/a string instead of an object --
  // group.type would then throw, previously left completely unguarded.
  const groups = HueApi.parseGroups(JSON.stringify({
    "1": { name: "Fine", type: "Room", lights: ["1"] },
    "2": null,
    "3": 42,
    "4": "not an object"
  }))
  assert.deepEqual(groups.map(g => g.id), ["1"])
})

test("parseGroups drops malformed light ids from a group's lightIds", () => {
  const groups = HueApi.parseGroups(JSON.stringify({
    "1": { name: "Room", type: "Room", lights: ["1", "has space", "2"] }
  }))
  assert.deepEqual(groups[0].lightIds, ["1", "2"])
})

test("parseGroups truncates an overlong name", () => {
  const groups = HueApi.parseGroups(JSON.stringify({
    "1": { name: "x".repeat(500), type: "Room", lights: ["1"] }
  }))
  assert.equal(groups[0].name.length, 200)
})

test("parseGroups strips angle brackets from names so they can't render as markup downstream", () => {
  const groups = HueApi.parseGroups(JSON.stringify({
    "1": { name: "<b>Evil</b> Room", type: "Room", lights: ["1"] }
  }))
  assert.equal(groups[0].name, "bEvil/b Room")
  assert.ok(!groups[0].name.includes("<"))
  assert.ok(!groups[0].name.includes(">"))
})

test("parseGroups strips control and bidi-override characters from names", () => {
  const groups = HueApi.parseGroups(JSON.stringify({
    "1": { name: "Living‮room", type: "Room", lights: ["1"] }
  }))
  assert.equal(groups[0].name, "Livingroom")
})

test("parseGroups leaves ordinary accented Unicode names untouched", () => {
  const groups = HueApi.parseGroups(JSON.stringify({
    "1": { name: "Café", type: "Room", lights: ["1"] }
  }))
  assert.equal(groups[0].name, "Café")
})

test("roomIcon returns a mapped glyph for a known class", () => {
  const bedroomIcon = HueApi.roomIcon("Bedroom")
  const defaultIcon = HueApi.roomIcon("Other")
  assert.notEqual(bedroomIcon, defaultIcon)
  assert.equal(typeof bedroomIcon, "string")
  assert.ok(bedroomIcon.length > 0)
})

test("roomIcon falls back to the default glyph for unknown or missing classes", () => {
  const defaultIcon = HueApi.roomIcon("Other")
  assert.equal(HueApi.roomIcon("Something Hue never sends"), defaultIcon)
  assert.equal(HueApi.roomIcon(undefined), defaultIcon)
  assert.equal(HueApi.roomIcon(""), defaultIcon)
})

test("parseScenes keeps only scenes tied to a group and sorts by name", () => {
  const scenes = HueApi.parseScenes(JSON.stringify({
    "s2": { name: "Zen", type: "GroupScene", group: "1" },
    "s1": { name: "Bright", type: "GroupScene", group: "1" },
    "s3": { name: "Ungrouped", type: "LightScene", lights: ["1", "2"] }
  }))
  assert.deepEqual(scenes, [
    { id: "s1", name: "Bright", group: "1" },
    { id: "s2", name: "Zen", group: "1" }
  ])
})

test("parseScenes returns an empty list for empty/malformed input", () => {
  assert.deepEqual(HueApi.parseScenes(""), [])
  assert.deepEqual(HueApi.parseScenes("not json"), [])
})

test("parseScenes drops entries with a malformed scene or group id", () => {
  const scenes = HueApi.parseScenes(JSON.stringify({
    "s1": { name: "Fine", group: "1" },
    "has space": { name: "Bad scene id", group: "1" },
    "s2": { name: "Bad group id", group: "not valid" }
  }))
  assert.deepEqual(scenes.map(s => s.id), ["s1"])
})

test("parseScenes skips entries whose value is null or a non-object primitive", () => {
  const scenes = HueApi.parseScenes(JSON.stringify({
    "s1": { name: "Fine", group: "1" },
    "s2": null,
    "s3": 42,
    "s4": "not an object"
  }))
  assert.deepEqual(scenes.map(s => s.id), ["s1"])
})

test("parseScenes truncates an overlong name", () => {
  const scenes = HueApi.parseScenes(JSON.stringify({
    "s1": { name: "x".repeat(500), group: "1" }
  }))
  assert.equal(scenes[0].name.length, 200)
})

test("parseScenes strips angle brackets from names so they can't render as markup downstream", () => {
  const scenes = HueApi.parseScenes(JSON.stringify({
    "s1": { name: "<script>bad</script>", group: "1" }
  }))
  assert.equal(scenes[0].name, "scriptbad/script")
})

test("parseScenes strips control and bidi-override characters from names", () => {
  const scenes = HueApi.parseScenes(JSON.stringify({
    "s1": { name: "Movie‮night", group: "1" }
  }))
  assert.equal(scenes[0].name, "Movienight")
})

test("parseScenes leaves ordinary accented Unicode names untouched", () => {
  const scenes = HueApi.parseScenes(JSON.stringify({
    "s1": { name: "Café", group: "1" }
  }))
  assert.equal(scenes[0].name, "Café")
})

test("applyOrder sorts items by position in the order list", () => {
  const items = [{ id: "1" }, { id: "2" }, { id: "3" }]
  const sorted = HueApi.applyOrder(items, ["3", "1", "2"])
  assert.deepEqual(sorted.map(i => i.id), ["3", "1", "2"])
})

test("applyOrder appends unlisted ids at the end in their original order", () => {
  const items = [{ id: "1" }, { id: "2" }, { id: "3" }]
  const sorted = HueApi.applyOrder(items, ["2"])
  assert.deepEqual(sorted.map(i => i.id), ["2", "1", "3"])
})

test("applyOrder ignores stale ids not present in items", () => {
  const items = [{ id: "1" }, { id: "2" }]
  const sorted = HueApi.applyOrder(items, ["deleted-id", "2", "1"])
  assert.deepEqual(sorted.map(i => i.id), ["2", "1"])
})

test("applyOrder returns items unchanged when order is empty or missing", () => {
  const items = [{ id: "1" }, { id: "2" }]
  assert.deepEqual(HueApi.applyOrder(items, []).map(i => i.id), ["1", "2"])
  assert.deepEqual(HueApi.applyOrder(items, undefined).map(i => i.id), ["1", "2"])
})

test("roomBrightness averages the actual state.bri of a room's lights", () => {
  const text = JSON.stringify({
    "1": { state: { bri: 100 } },
    "2": { state: { bri: 200 } },
    "3": { state: { bri: 50 } } // not in the room, should be ignored
  })
  assert.equal(HueApi.roomBrightness(text, ["1", "2"]), 150)
})

test("roomBrightness clamps to Hue's valid range and skips lights without bri", () => {
  const text = JSON.stringify({
    "1": { state: { bri: 999 } },
    "2": { state: { on: true } } // no bri (on/off-only light), ignored
  })
  assert.equal(HueApi.roomBrightness(text, ["1", "2"]), 254)
})

test("roomBrightness returns null when there's nothing usable", () => {
  assert.equal(HueApi.roomBrightness(JSON.stringify({}), ["1"]), null)
  assert.equal(HueApi.roomBrightness("", ["1"]), null)
  assert.equal(HueApi.roomBrightness(JSON.stringify({ "1": { state: { bri: 10 } } }), []), null)
})
