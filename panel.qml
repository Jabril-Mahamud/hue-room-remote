import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "hue_api.js" as HueApi

Panel {
  id: root
  moduleName: "lunardi0x01.hue-room-remote"
  ipcTarget: "lunardi0x01.hue-room-remote"

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  property bool paired: false
  property string bridgeId: ""
  property var visibleRooms: []
  property var scenes: []
  property string favoriteRoomId: ""
  property string activeView: "list" // "list" | "room"
  property string activeRoomId: ""
  property int pendingFetches: 0
  property bool loading: false
  property bool lastFetchFailed: false
  property var actionQueue: []
  property var lastSceneByRoom: ({})
  property var roomOrder: []
  property var sceneOrderByRoom: ({})
  property var hiddenRoomIds: []
  property bool editMode: false
  // Set by checkStatusThenRefresh() so statusProc's completion handler
  // runs refresh() itself once the status check actually resolves, rather
  // than open()/openFromHotkey() calling refresh() synchronously right
  // after kicking off the (async) status check -- which would run refresh()
  // against whatever root.paired already was, not the freshly-verified value.
  property bool refreshAfterStatus: false

  readonly property var activeRoom: root.findRoom(root.activeRoomId)
  readonly property int roomCount: root.visibleRooms.length
  property string transport: "bridge"
  // A BLE bulb is reached directly over Bluetooth, so there is no bridge
  // certificate to verify and no bridgeId to be missing.
  readonly property bool insecureMode: root.paired && root.transport !== "ble" && !root.bridgeId
  readonly property var orderedRooms: HueApi.applyOrder(root.visibleRooms, root.roomOrder)
  readonly property var orderedScenesForActiveRoom: HueApi.applyOrder(
    root.scenes.filter(function(s) { return s.group === root.activeRoomId }),
    root.sceneOrderByRoom[root.activeRoomId] || [])
  // Hidden rooms stay out of the list entirely -- except while editing, when
  // they reappear (dimmed) so there's a way to find and unhide them again.
  readonly property var displayedRooms: root.editMode
    ? root.orderedRooms
    : root.orderedRooms.filter(function(r) { return root.hiddenRoomIds.indexOf(r.id) === -1 })

  readonly property string statusText: {
    if (!root.paired) return "Not paired"
    if (root.lastFetchFailed) return root.transport === "ble" ? "Light unreachable" : "Bridge unreachable"
    if (root.loading) return "Loading…"
    return ""
  }

  // Section headings in the order hue-scenes.json introduces them.
  readonly property var sceneSections: {
    var seen = {}
    var out = []
    root.orderedScenesForActiveRoom.forEach(function(s) {
      if (seen[s.section]) return
      seen[s.section] = true
      out.push({ title: s.section, rank: s.sectionRank })
    })
    out.sort(function(a, b) { return a.rank - b.rank })
    return out
  }

  function scenesInSection(title) {
    return root.orderedScenesForActiveRoom.filter(function(s) { return s.section === title })
  }

  function withAlpha(color, alpha) {
    return Qt.rgba(color.r, color.g, color.b, alpha)
  }

  // Scene swatches are tints picked to read on a dark panel. A light Omarchy
  // theme gives the panel a dark foreground, and the same tints wash out
  // against it, so they are darkened there instead.
  readonly property bool lightTheme:
    (0.299 * bar.foreground.r + 0.587 * bar.foreground.g + 0.114 * bar.foreground.b) < 0.5

  function sceneColour(swatch) {
    if (!swatch) return root.bar.foreground
    return root.lightTheme ? Qt.darker(swatch, 1.9) : swatch
  }

  function findRoom(roomId) {
    for (var i = 0; i < root.visibleRooms.length; i++) {
      if (root.visibleRooms[i].id === roomId) return root.visibleRooms[i]
    }
    return null
  }

  function resolveDefaultView() {
    root.activeRoomId = root.favoriteRoomId
    root.activeView = root.favoriteRoomId ? "room" : "list"
    if (root.favoriteRoomId) root.refreshRoomBrightness(root.favoriteRoomId)
  }

  function openRoom(roomId) {
    root.activeView = "room"
    root.activeRoomId = roomId
    root.refreshRoomBrightness(roomId)
  }

  // A group's action.bri (used to seed visibleRooms.bri from get-groups) is
  // just the bridge's record of the last command sent to the group, not a
  // reliable read of real current brightness -- it's frequently stale or
  // never-set. The only trustworthy source is each light's own state.bri, so
  // this does a one-off get-lights fetch scoped to whichever room is being
  // looked at, rather than pulling all lights on every poll cycle.
  function refreshRoomBrightness(roomId) {
    if (!root.paired || roomLightsProc.running) return
    roomLightsProc.forRoomId = roomId
    roomLightsProc.command = HueApi.apiCmd(["get-lights"])
    roomLightsProc.running = true
  }

  function openList() {
    root.activeView = "list"
  }

  // checkStatusThenRefresh() here (not just inside refresh()'s
  // `if (!root.paired)` branch) matters because root.paired otherwise never
  // gets re-verified once true -- the unpaired-polling timer below stops
  // once paired, and nothing else re-reads hue.json's actual on-disk state.
  // Without this, if credentials are removed (cleanup.sh, a failed re-pair,
  // hand-editing the file) while the shell keeps running, the panel keeps
  // trusting a stale "paired" and silently shows an empty room list instead
  // of the unpaired/pairing screen. Cheap to call unconditionally: it's a
  // local file read via hue_api.py, no network, and only runs when the user
  // is actually opening the panel rather than as background polling.
  //
  // refresh() runs from statusProc's completion handler, not synchronously
  // here -- checkStatus() only starts an async Process, so calling
  // refresh() right after it would still see the old root.paired value.
  function open() {
    root.controller.show()
    root.resolveDefaultView()
    root.checkStatusThenRefresh()
  }

  function openFromHotkey() {
    root.open()
  }

  function checkStatusThenRefresh() {
    root.refreshAfterStatus = true
    root.checkStatus()
  }

  function close() {
    root.controller.hide()
  }

  function refresh() {
    if (!root.paired) {
      root.checkStatus()
      return
    }
    if (actionProc.running || root.actionQueue.length > 0) {
      resyncTimer.restart()
      return
    }
    if (groupsProc.running || scenesProc.running) {
      resyncTimer.restart()
      return
    }
    root.lastFetchFailed = false
    if (root.visibleRooms.length === 0) root.loading = true
    Qt.callLater(startFetches)
  }

  function startFetches() {
    if (!root.paired) return
    root.pendingFetches = 2
    groupsProc.command = HueApi.apiCmd(["get-groups"])
    scenesProc.command = HueApi.apiCmd(["get-scenes"])
    groupsProc.running = true
    scenesProc.running = true
  }

  function finishFetch(success) {
    root.pendingFetches--
    if (success === false) root.lastFetchFailed = true
    if (root.pendingFetches <= 0) {
      root.loading = false
      root.assembleRooms()
    }
  }

  function assembleRooms() {
    if (root.activeView === "room" && root.findRoom(root.activeRoomId) === null) {
      root.activeView = "list"
    }
  }

  function patchRoom(roomId, changes) {
    root.visibleRooms = root.visibleRooms.map(function(r) {
      if (r.id !== roomId) return r
      var updated = {}
      for (var k in r) updated[k] = r[k]
      for (var k2 in changes) updated[k2] = changes[k2]
      return updated
    })
  }

  function toggleRoom(roomId, on) {
    if (!root.paired) return
    root.patchRoom(roomId, { on: on })
    if (on && root.lastSceneByRoom[roomId]) {
      // Applying the scene both turns the lights on and restores the look,
      // rather than just switching them on at whatever raw state they were
      // last left in.
      root.queueAction("put-group", roomId, { scene: root.lastSceneByRoom[roomId] })
      root.scheduleBrightnessRefresh(roomId)
    } else {
      root.queueAction("put-group", roomId, { on: on })
    }
    root.scheduleRefresh()
  }

  function setRoomBrightness(roomId, bri) {
    if (!root.paired) return
    var clamped = Math.max(1, Math.min(254, Math.round(bri)))
    root.patchRoom(roomId, { bri: clamped })
    root.queueAction("put-group", roomId, { bri: clamped })
    root.scheduleRefresh()
  }

  function applyScene(roomId, sceneId) {
    if (!root.paired) return
    var lastScene = {}
    for (var k in root.lastSceneByRoom) lastScene[k] = root.lastSceneByRoom[k]
    lastScene[roomId] = sceneId
    root.lastSceneByRoom = lastScene
    root.queueAction("write-order", "order", { lastScene: lastScene })
    root.patchRoom(roomId, { on: true })
    root.queueAction("put-group", roomId, { scene: sceneId })
    root.scheduleRefresh()
    root.scheduleBrightnessRefresh(roomId)
  }

  // Scenes (and re-applying one via the room toggle) can set each light to
  // a different brightness, which we have no way to know client-side. Give
  // the bridge a moment to actually apply it, then re-derive the room's
  // displayed brightness from real light state.
  function scheduleBrightnessRefresh(roomId) {
    sceneBrightnessTimer.roomId = roomId
    sceneBrightnessTimer.restart()
  }

  function setFavorite(roomId) {
    var next = root.favoriteRoomId === roomId ? "" : roomId
    root.favoriteRoomId = next
    root.queueAction("write-favorite", "favorite", { roomId: next })
  }

  function moveRoom(roomId, direction) {
    var ids = root.orderedRooms.map(function(r) { return r.id })
    var idx = ids.indexOf(roomId)
    var next = idx + direction
    if (idx < 0 || next < 0 || next >= ids.length) return
    var tmp = ids[idx]
    ids[idx] = ids[next]
    ids[next] = tmp
    root.roomOrder = ids
    root.queueAction("write-order", "order", { roomOrder: ids })
  }

  // Scenes render in sections, so ▲/▼ swaps with the nearest scene in the same
  // section; a plain neighbour swap would move it across a hidden boundary.
  function moveScene(roomId, sceneId, direction) {
    var scenes = root.orderedScenesForActiveRoom
    var ids = scenes.map(function(s) { return s.id })
    var idx = ids.indexOf(sceneId)
    if (idx < 0) return
    var next = idx + direction
    while (next >= 0 && next < ids.length && scenes[next].section !== scenes[idx].section) next += direction
    if (next < 0 || next >= ids.length) return
    var tmp = ids[idx]
    ids[idx] = ids[next]
    ids[next] = tmp
    var updated = {}
    for (var k in root.sceneOrderByRoom) updated[k] = root.sceneOrderByRoom[k]
    updated[roomId] = ids
    root.sceneOrderByRoom = updated
    root.queueAction("write-order", "order", { sceneOrder: root.sceneOrderByRoom })
  }

  function toggleRoomHidden(roomId) {
    var hidden = root.hiddenRoomIds.slice()
    var idx = hidden.indexOf(roomId)
    if (idx === -1) hidden.push(roomId)
    else hidden.splice(idx, 1)
    root.hiddenRoomIds = hidden
    root.queueAction("write-order", "order", { hiddenRooms: hidden })
  }

  function queueAction(op, targetId, body) {
    for (var i = 0; i < root.actionQueue.length; i++) {
      var pending = root.actionQueue[i]
      if (pending.op === op && pending.id === targetId) {
        for (var key in body) pending.body[key] = body[key]
        return
      }
    }
    root.actionQueue.push({ op: op, id: targetId, body: body })
    drainActionQueue()
  }

  function drainActionQueue() {
    if (actionProc.running) return
    if (root.actionQueue.length === 0) return
    var next = root.actionQueue.shift()
    actionProc.command = HueApi.apiCmd([next.op, next.id, JSON.stringify(next.body)])
    actionProc.running = true
  }

  function scheduleRefresh() {
    resyncTimer.restart()
  }

  // Pairing status is fetched through hue_api.py's `get-status` op rather
  // than reading hue.json directly, so the real username never has to
  // exist as a QML/JS string in this process at all -- only a non-secret
  // {paired, bridgeId} snapshot ever crosses into QML.
  function checkStatus() {
    if (statusProc.running) return
    statusProc.running = true
  }

  Component.onCompleted: root.checkStatus()

  // hue_api.py's own settings writes honor $XDG_CONFIG_HOME the same way;
  // reading from a different location here would mean writes and reads
  // silently disagree for anyone with it customized.
  readonly property string configHome: Quickshell.env("XDG_CONFIG_HOME") || (Quickshell.env("HOME") + "/.config")

  // Reads go through HueApi.parseJsonObject (size-capped, exception-safe)
  // rather than a raw JSON.parse, and every id is re-validated with the
  // same [a-zA-Z0-9_-]{1,40} pattern hue_api.py's write side already
  // enforces -- defense-in-depth consistency between the write path
  // (_atomic_write, regex-validated) and this read path (FileView, which
  // has no equivalent symlink/ownership hardening available at this layer).
  property FileView favoriteFile: FileView {
    path: root.configHome + "/omarchy/settings/hue-favorite.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      var parsed = HueApi.parseJsonObject(text())
      var roomId = parsed ? String(parsed.favoriteRoomId || "") : ""
      root.favoriteRoomId = (roomId === "" || HueApi.isValidId(roomId)) ? roomId : ""
    }
    onLoadFailed: root.favoriteRoomId = ""
  }

  property FileView orderFile: FileView {
    path: root.configHome + "/omarchy/settings/hue-order.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      var parsed = HueApi.parseJsonObject(text())
      root.roomOrder = HueApi.sanitizeIdList(parsed && parsed.roomOrder)
      root.sceneOrderByRoom = HueApi.sanitizeIdListMap(parsed && parsed.sceneOrder)
      root.lastSceneByRoom = HueApi.sanitizeIdMap(parsed && parsed.lastScene)
      root.hiddenRoomIds = HueApi.sanitizeIdList(parsed && parsed.hiddenRooms)
    }
    onLoadFailed: {
      root.roomOrder = []
      root.sceneOrderByRoom = {}
      root.lastSceneByRoom = {}
      root.hiddenRoomIds = []
    }
  }

  Timer {
    interval: 1500
    running: true
    onTriggered: root.checkStatus()
  }

  Timer {
    interval: 5000
    repeat: true
    running: !root.paired
    onTriggered: root.checkStatus()
  }

  Timer {
    id: resyncTimer
    interval: 700
    onTriggered: root.refresh()
  }

  Timer {
    interval: 15000
    repeat: true
    running: root.paired && root.opened
    onTriggered: root.refresh()
  }

  Timer {
    id: sceneBrightnessTimer
    property string roomId: ""
    interval: 900
    onTriggered: root.refreshRoomBrightness(roomId)
  }

  Process {
    id: statusProc
    command: HueApi.apiCmd(["get-status"])
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var status = HueApi.parseStatus(text)
        var wasPaired = root.paired
        root.paired = !!(status && status.paired)
        root.bridgeId = (status && status.bridgeId) || ""
        root.transport = (status && status.transport) || "bridge"
        if (root.refreshAfterStatus) {
          root.refreshAfterStatus = false
          root.refresh()
        } else if (root.paired && !wasPaired) {
          root.refresh()
        }
      }
    }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.paired = false
        root.bridgeId = ""
        root.transport = "bridge"
      }
    }
  }

  // groupsProc/scenesProc make finishFetch's success/failure decision
  // solely from onExited's exitCode, not from parsing stdout -- hue_api.py
  // exits non-zero specifically so a real bridge failure (network down,
  // TLS error, revoked auth) is distinguishable from "the bridge genuinely
  // has no rooms/scenes" (valid, empty, exit 0). Deciding this any other
  // way (e.g. treating empty/unparseable stdout as failure) would still be
  // wrong: it's exactly what a legitimate empty result also looks like.
  // Reading stdout.text here (a persistent StdioCollector property, not a
  // signal-only value) rather than relying on onStreamFinished having
  // already run avoids depending on the relative order the two signals
  // fire in.
  Process {
    id: groupsProc
    stdout: StdioCollector {
      id: groupsCollector
      waitForEnd: true
    }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.finishFetch(false)
        return
      }
      var fetched = HueApi.parseGroups(groupsCollector.text).filter(function(r) { return r.lightIds.length > 0 })
      var knownById = {}
      for (var i = 0; i < root.visibleRooms.length; i++) knownById[root.visibleRooms[i].id] = root.visibleRooms[i]
      // A group's `action.bri` on the bridge is just its last-sent command,
      // not a reliable "current brightness" -- refetching it here would
      // undo whatever the user (or a scene) just set. on/allOn/lightIds
      // come from real aggregate state (state.any_on etc.) and are safe
      // to trust from the bridge; bri is only ever updated by our own
      // optimistic patches.
      root.visibleRooms = fetched.map(function(r) {
        var known = knownById[r.id]
        if (!known) return r
        var merged = {}
        for (var k in r) merged[k] = r[k]
        merged.bri = known.bri
        return merged
      })
      root.finishFetch(true)
    }
  }

  Process {
    id: scenesProc
    stdout: StdioCollector {
      id: scenesCollector
      waitForEnd: true
    }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.finishFetch(false)
        return
      }
      root.scenes = HueApi.parseScenes(scenesCollector.text)
      root.finishFetch(true)
    }
  }

  Process {
    id: actionProc
    onExited: function(exitCode) {
      root.drainActionQueue()
    }
  }

  Process {
    id: roomLightsProc
    property string forRoomId: ""
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var room = root.findRoom(roomLightsProc.forRoomId)
        if (room) {
          var bri = HueApi.roomBrightness(text, room.lightIds)
          if (bri !== null) root.patchRoom(room.id, { bri: bri })
        }
      }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Flickable {
        id: scroll
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: column
          width: scroll.width
          spacing: Style.space(6)

          Item {
            width: parent.width
            height: titleRow.implicitHeight

            Row {
              id: titleRow
              anchors.left: parent.left
              anchors.right: editModeButton.left
              anchors.rightMargin: Style.space(8)
              spacing: Style.space(10)

              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "󰌵"
                color: root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.title
              }

              Column {
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(2)

                Text {
                  text: "Hue Lights"
                  color: root.bar.foreground
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.title
                  font.bold: true
                }

                Text {
                  visible: root.statusText.length > 0
                  text: root.statusText
                  textFormat: Text.PlainText
                  color: Qt.darker(root.bar.foreground, 1.4)
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }

            PanelActionButton {
              id: editModeButton
              anchors.right: parent.right
              anchors.verticalCenter: titleRow.verticalCenter
              iconText: "⚙"
              tooltipText: root.editMode ? "Done reordering" : "Reorder rooms and scenes"
              foreground: root.editMode ? Color.accent : root.bar.foreground
              fontFamily: root.bar.fontFamily
              onClicked: root.editMode = !root.editMode
            }
          }

          Rectangle {
            width: parent.width
            height: Style.spacing.hairline
            color: root.bar.foreground
            opacity: 0.12
          }

          Column {
            visible: !root.paired
            width: parent.width
            spacing: Style.space(4)

            Text {
              width: parent.width
              text: "No bridge configured yet."
              color: root.bar.foreground
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.body
              wrapMode: Text.WordWrap
            }

            Text {
              width: parent.width
              text: "Click below to start pairing — you'll be prompted to press the bridge's link button next."
              color: Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            Rectangle {
              width: parent.width
              height: pairButton.implicitHeight + Style.space(16)
              radius: Style.space(8)
              color: pairButtonMouse.containsMouse ? Qt.lighter(Color.accent, 1.2) : Color.accent

              Text {
                id: pairButton
                anchors.centerIn: parent
                text: "Pair with bridge"
                color: "#ffffff"
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
              }

              MouseArea {
                id: pairButtonMouse
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                hoverEnabled: true
                onClicked: {
                  var pairPath = HueApi.resolveScriptPath(Qt.resolvedUrl("pair.sh").toString())
                  Quickshell.execDetached(["omarchy-launch-terminal", "bash", pairPath])
                }
              }
            }
          }

          Row {
            visible: root.paired && root.loading && root.visibleRooms.length === 0
            spacing: Style.space(4)

            Text {
              text: "󰦖"
              color: Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.body

              RotationAnimator on rotation {
                running: root.loading
                from: 0
                to: 360
                duration: 800
                loops: Animation.Infinite
              }
            }

            Text {
              text: "Loading…"
              color: Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.body
            }
          }

          Text {
            visible: root.paired && root.lastFetchFailed && !root.loading
            width: parent.width
            // Two transports fail for completely different reasons, and the
            // bridge wording sends a Bluetooth user looking for hardware
            // they do not own.
            text: root.transport === "ble"
              ? "Couldn't reach the light. Check it has mains power and that hue-ble-daemon is running (systemctl --user status hue-ble-daemon)."
              : "Couldn't reach the bridge. Check the bridge is on and the IP is still valid, then re-run pair.sh."
            color: Color.urgent
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Text {
            visible: root.insecureMode
            width: parent.width
            text: "Bridge identity not verified — re-run pair.sh to confirm you're talking to your own bridge."
            color: Color.urgent
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          // ---- Room List view ------------------------------------------------

          Column {
            visible: root.paired && root.activeView === "list"
            width: parent.width
            spacing: Style.space(6)

            Repeater {
              model: root.displayedRooms

              Rectangle {
                id: roomRow
                required property var modelData
                required property int index
                readonly property bool isHidden: root.hiddenRoomIds.indexOf(modelData.id) !== -1
                width: parent.width
                height: Math.max(48, roomRowContent.implicitHeight + Style.space(16))
                opacity: isHidden ? 0.4 : 1.0
                radius: Style.space(8)
                color: roomRowMouse.containsMouse
                  ? root.withAlpha(root.bar.foreground, 0.10)
                  : root.withAlpha(root.bar.foreground, 0.04)
                border.width: 1
                border.color: root.withAlpha(root.bar.foreground, roomRowMouse.containsMouse ? 0.28 : 0.14)

                MouseArea {
                  id: roomRowMouse
                  anchors.left: parent.left
                  anchors.right: trailingControls.left
                  anchors.top: parent.top
                  anchors.bottom: parent.bottom
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.openRoom(roomRow.modelData.id)
                }

                Row {
                  id: roomRowContent
                  anchors.left: parent.left
                  anchors.leftMargin: Style.space(12)
                  anchors.right: trailingControls.left
                  anchors.rightMargin: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(8)

                  Text {
                    id: roomIconText
                    text: HueApi.roomIcon(roomRow.modelData.class)
                    color: root.bar.foreground
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.body
                    anchors.verticalCenter: parent.verticalCenter
                  }

                  Text {
                    // Row doesn't clip or constrain its children's width --
                    // `elide` only takes effect once a Text's own `width`
                    // is smaller than its natural (unwrapped) width, so
                    // without this explicit width a long room name just
                    // renders at full size and paints over the toggle/
                    // reorder/favorite controls to its right instead of
                    // eliding.
                    width: Math.max(0, roomRowContent.width - roomIconText.implicitWidth - roomRowContent.spacing)
                    text: roomRow.modelData.name
                    textFormat: Text.PlainText
                    color: root.bar.foreground
                    font.family: root.bar.fontFamily
                    font.pixelSize: Style.font.body
                    font.bold: true
                    elide: Text.ElideRight
                    anchors.verticalCenter: parent.verticalCenter
                  }
                }

                Row {
                  id: trailingControls
                  anchors.right: parent.right
                  anchors.rightMargin: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(6)

                  ToggleSwitch {
                    anchors.verticalCenter: parent.verticalCenter
                    checked: roomRow.modelData.on
                    foreground: root.bar.foreground
                    accent: Color.accent
                    onToggled: root.toggleRoom(roomRow.modelData.id, !roomRow.modelData.on)
                  }

                  PanelActionButton {
                    visible: root.editMode
                    iconText: "▲"
                    foreground: root.bar.foreground
                    fontFamily: root.bar.fontFamily
                    enabled: roomRow.index > 0
                    onClicked: root.moveRoom(roomRow.modelData.id, -1)
                  }

                  PanelActionButton {
                    visible: root.editMode
                    iconText: "▼"
                    foreground: root.bar.foreground
                    fontFamily: root.bar.fontFamily
                    enabled: roomRow.index < root.displayedRooms.length - 1
                    onClicked: root.moveRoom(roomRow.modelData.id, 1)
                  }

                  PanelActionButton {
                    visible: root.editMode
                    iconText: roomRow.isHidden ? "" : ""
                    tooltipText: roomRow.isHidden ? "Unhide room" : "Hide room"
                    foreground: root.bar.foreground
                    fontFamily: root.bar.fontFamily
                    onClicked: root.toggleRoomHidden(roomRow.modelData.id)
                  }

                  Text {
                    id: starIcon
                    anchors.verticalCenter: parent.verticalCenter
                    text: roomRow.modelData.id === root.favoriteRoomId ? "★" : "☆"
                    color: roomRow.modelData.id === root.favoriteRoomId ? Color.accent : Qt.darker(root.bar.foreground, 1.6)
                    font.pixelSize: Style.font.title

                    MouseArea {
                      anchors.fill: parent
                      anchors.margins: -Style.space(6)
                      hoverEnabled: true
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.setFavorite(roomRow.modelData.id)
                    }
                  }
                }
              }
            }

            Text {
              visible: root.paired && !root.loading && !root.lastFetchFailed && root.roomCount === 0
              text: "No rooms with lights found."
              color: Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
            }

            Text {
              visible: root.paired && !root.loading && !root.lastFetchFailed
                && root.roomCount > 0 && root.displayedRooms.length === 0
              text: "All rooms are hidden. Tap ⚙ to manage."
              color: Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          // ---- Room view -------------------------------------------------------

          Loader {
            id: roomLoader
            width: parent.width
            height: item ? item.implicitHeight : 0
            active: root.paired && root.activeView === "room" && root.activeRoom !== null
            sourceComponent: roomViewComponent
          }
        }
      }
    }
  }

  Component {
    id: roomViewComponent

    Column {
      width: parent.width
      spacing: Style.space(8)

      Item {
        width: parent.width
        height: Style.space(32)

        Rectangle {
          id: backBtn
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          width: backText.implicitWidth + Style.space(20)
          height: parent.height
          radius: Style.space(6)
          color: backMouse.containsMouse
            ? root.withAlpha(root.bar.foreground, 0.10)
            : "transparent"
          border.width: 1
          border.color: Qt.darker(root.bar.foreground, 1.6)

          Text {
            id: backText
            anchors.centerIn: parent
            text: "‹ Rooms"
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }

          MouseArea {
            id: backMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.openList()
          }
        }

        Text {
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          text: root.activeRoomId === root.favoriteRoomId ? "★" : "☆"
          color: root.activeRoomId === root.favoriteRoomId ? Color.accent : Qt.darker(root.bar.foreground, 1.6)
          font.pixelSize: Style.font.title

          MouseArea {
            anchors.fill: parent
            anchors.margins: -Style.space(6)
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.setFavorite(root.activeRoomId)
          }
        }
      }

      // root.activeRoom can go null between a bridge refresh reassigning
      // root.visibleRooms and roomLoader's own `active: ... &&
      // root.activeRoom !== null` binding tearing this component down --
      // these guards are belt-and-suspenders against that transition
      // rather than a load-bearing fix (a thrown binding expression
      // doesn't corrupt QML's binding engine the way a JS exception would
      // in an imperative handler; it just leaves the property at its last
      // value until the binding's dependencies change again), but they're
      // free and remove the theoretical window entirely.
      Toggle {
        width: parent.width
        label: root.activeRoom ? (HueApi.roomIcon(root.activeRoom.class) + "  " + root.activeRoom.name) : ""
        checked: root.activeRoom ? root.activeRoom.on : false
        foreground: root.bar.foreground
        accent: Color.accent
        fontFamily: root.bar.fontFamily
        onClicked: if (root.activeRoom) root.toggleRoom(root.activeRoomId, !root.activeRoom.on)
      }

      PanelSlider {
        visible: root.activeRoom ? root.activeRoom.on : false
        width: parent.width - Style.space(24)
        anchors.horizontalCenter: parent.horizontalCenter
        bar: root.bar
        minimum: 1
        maximum: 254
        integer: true
        step: 10
        value: root.activeRoom ? root.activeRoom.bri : 254
        onReleased: function(v) { root.setRoomBrightness(root.activeRoomId, v) }
      }

      Column {
        width: parent.width
        spacing: Style.space(6)

        Repeater {
          model: root.sceneSections

          Column {
            id: section
            required property var modelData
            readonly property var scenes: root.scenesInSection(modelData.title)
            width: parent.width
            spacing: Style.space(6)
            topPadding: Style.space(4)

            Text {
              text: section.modelData.title
              color: Qt.darker(root.bar.foreground, 1.4)
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              font.letterSpacing: 1
              font.bold: true
            }

            Repeater {
              model: section.scenes

              Rectangle {
                id: sceneRow
                required property var modelData
                required property int index
                width: parent.width
                height: sceneText.implicitHeight + Style.space(16)
                radius: Style.space(8)
                color: sceneMouse.containsMouse
                  ? root.withAlpha(root.bar.foreground, 0.10)
                  : root.withAlpha(root.bar.foreground, 0.04)
                border.width: 1
                border.color: root.withAlpha(root.bar.foreground, sceneMouse.containsMouse ? 0.28 : 0.14)

                Text {
                  id: sceneText
                  anchors.left: parent.left
                  anchors.leftMargin: Style.space(12)
                  anchors.verticalCenter: parent.verticalCenter
                  text: sceneRow.modelData.name
                  textFormat: Text.PlainText
                  color: root.sceneColour(sceneRow.modelData.swatch)
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.body
                }

                MouseArea {
                  id: sceneMouse
                  anchors.left: parent.left
                  anchors.right: sceneTrailingControls.left
                  anchors.top: parent.top
                  anchors.bottom: parent.bottom
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.applyScene(root.activeRoomId, sceneRow.modelData.id)
                }

                Row {
                  id: sceneTrailingControls
                  anchors.right: parent.right
                  anchors.rightMargin: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(2)

                  PanelActionButton {
                    visible: root.editMode
                    iconText: "▲"
                    foreground: root.bar.foreground
                    fontFamily: root.bar.fontFamily
                    enabled: sceneRow.index > 0
                    onClicked: root.moveScene(root.activeRoomId, sceneRow.modelData.id, -1)
                  }

                  PanelActionButton {
                    visible: root.editMode
                    iconText: "▼"
                    foreground: root.bar.foreground
                    fontFamily: root.bar.fontFamily
                    enabled: sceneRow.index < section.scenes.length - 1
                    onClicked: root.moveScene(root.activeRoomId, sceneRow.modelData.id, 1)
                  }
                }
              }
            }
          }
        }

        Text {
          visible: !root.scenes.some(function(s) { return s.group === root.activeRoomId })
          text: "No scenes saved for this room."
          color: Qt.darker(root.bar.foreground, 1.4)
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
