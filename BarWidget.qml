import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons
import "SoundCloudModel.js" as SoundCloudModel

BarWidget {
  id: root
  moduleName: "brunosilveira.soundcloud"

  property bool popupOpen: false
  property bool running: false
  property bool loggedIn: false
  property bool playing: false
  property string playbackState: "idle"
  property string playbackId: ""
  property string title: ""
  property string artist: ""
  property string artDataUrl: ""
  property var waveformLevels: []
  property real duration: 0
  property real position: 0
  property string lastError: ""
  property bool actionBusy: false
  property bool selectionBusy: false
  property int activeSelectionRequestId: 0
  property string selectedTab: "home"
  property var tracks: []
  property var homeTracks: []
  property var feedTracks: []
  property var trackArtworkData: ({})
  property var trackArtworkPending: ({})
  property bool tracksLoading: false
  property bool homeLoadMorePending: false
  property bool feedLoadMorePending: false
  readonly property bool loadMorePending: selectedTab === "home"
    ? homeLoadMorePending : feedLoadMorePending
  property bool homeHasMore: true
  property bool feedHasMore: true
  property int homeLoadMoreAtCount: 0
  property int feedLoadMoreAtCount: 0
  property int trackLoadAttempts: 0
  property bool tracksRequestPending: false
  property int nextRequestId: 1
  property var pendingRequests: ({})
  property bool connectionInitialized: false
  // Probe an existing daemon so shell restarts preserve playback, but never
  // launch one until the user interacts with the plugin.
  property bool backendWanted: true
  property bool socketProbePending: true
  property bool launchingBackend: false
  property string socketBuffer: ""
  readonly property int maxSocketFrameChars: 524288
  property bool hasTrack: title !== ""
  readonly property var activeSocket: socketLoader.item
  readonly property bool backendConnected: !!(activeSocket && activeSocket.connected)

  readonly property string helperPath: {
    var value = Qt.resolvedUrl("soundcloud_app.py").toString()
    return value.indexOf("file://") === 0 ? decodeURIComponent(value.substring(7)) : value
  }
  readonly property string socketPath: {
    var runtime = Quickshell.env("XDG_RUNTIME_DIR")
    return runtime ? String(runtime) + "/omarchy-soundcloud/control.sock" : ""
  }
  readonly property string label: SoundCloudModel.displayLabel({ trackTitle: title, trackArtist: artist })
  readonly property string safeTooltipLabel: plainForHost(label)
  readonly property string playIcon: playing ? "󰏤" : "󰐊"
  readonly property color dim: Qt.darker(bar.foreground, 1.5)

  // Bar.qml routes `omarchy-shell shell summon|hide|toggle` through these.
  readonly property bool opened: popupOpen

  function open() {
    popupOpen = true
    if (!backendConnected) startBackend(false)
    else if (tracks.length === 0) selectTab(selectedTab)
  }

  function close() { popupOpen = false }

  function toggle() {
    if (popupOpen) close()
    else open()
  }

  function plainForHost(value) {
    return String(value || "")
      .replace(/[<>&\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, "")
      .slice(0, 256)
  }

  function startBackend(showWindow) {
    if (launcherProcess.running) return
    backendWanted = true
    launchingBackend = true
    socketProbePending = true
    launcherProcess.command = ["/usr/bin/python3", "-I", helperPath, showWindow ? "launch" : "ensure"]
    launcherProcess.running = true
  }

  function refreshTracks() {
    if (!popupOpen || tracksRequestPending || !backendConnected || !running || !loggedIn) return
    tracksLoading = true
    trackLoadAttempts += 1
    tracksRequestPending = sendCommand("tracks:" + selectedTab, "tracks:" + selectedTab) > 0
  }

  function selectTab(tab) {
    selectedTab = tab
    tracks = tab === "home" ? homeTracks : feedTracks
    tracksLoading = true
    trackLoadAttempts = 0
    runAction(tab)
    refreshTracks()
  }

  function loadMoreTracks(yPosition, heightRatio, atEnd, userInitiated) {
    var requestedCount = selectedTab === "home" ? homeLoadMoreAtCount : feedLoadMoreAtCount
    var hasMore = selectedTab === "home" ? homeHasMore : feedHasMore
    if (!backendConnected || !running || !loggedIn
        || !SoundCloudModel.shouldLoadMoreTracks({
          userInitiated: userInitiated,
          hasMore: hasMore,
          atEnd: atEnd,
          yPosition: yPosition,
          heightRatio: heightRatio,
          count: tracks.length,
          requestedCount: requestedCount,
          pending: loadMorePending
        })) return
    if (selectedTab === "home") {
      homeLoadMorePending = true
      homeLoadMoreAtCount = tracks.length
    } else {
      feedLoadMorePending = true
      feedLoadMoreAtCount = tracks.length
    }
    if (sendCommand("load-more:" + selectedTab, "load-more:" + selectedTab) === 0) {
      setLoadMorePending(selectedTab, false)
    }
  }

  function setLoadMorePending(sourceTab, pending) {
    if (sourceTab === "home") homeLoadMorePending = pending
    else if (sourceTab === "feed") feedLoadMorePending = pending
  }

  function applyTracks(result, sourceTab) {
    var cached = sourceTab === "home" ? homeTracks : feedTracks
    var applied = SoundCloudModel.applyTrackResult(cached, result)
    var previousContentY = selectedTab === sourceTab && result && result.reset !== true
      ? SoundCloudModel.contentYAfterTrackUpdate(
          trackList.contentY, cached.length, applied.tracks.length)
      : null
    if (!result || result.pending !== true) {
      if (sourceTab === "home") homeTracks = applied.tracks
      else if (sourceTab === "feed") feedTracks = applied.tracks
    }
    if (selectedTab === sourceTab) tracks = applied.tracks
    if (previousContentY !== null) {
      Qt.callLater(function() {
        var maximum = Math.max(trackList.originY,
          trackList.originY + trackList.contentHeight - trackList.height)
        trackList.contentY = Math.max(trackList.originY,
          Math.min(previousContentY, maximum))
      })
    }
    if (result && result.hasMore === false) {
      if (sourceTab === "home") homeHasMore = false
      else if (sourceTab === "feed") feedHasMore = false
    } else if (result && result.reset === true) {
      if (sourceTab === "home") {
        homeHasMore = true
        homeLoadMoreAtCount = 0
      } else if (sourceTab === "feed") {
        feedHasMore = true
        feedLoadMoreAtCount = 0
      }
    }
    if (result && result.error) lastError = String(result.error)
    if (selectedTab === sourceTab) tracksLoading = applied.loading
  }

  function requestTrackArtwork(artworkId) {
    artworkId = String(artworkId || "")
    if (!/^[0-9a-f]{24}$/.test(artworkId)
        || trackArtworkData[artworkId]
        || trackArtworkPending[artworkId]) return
    var pending = Object.assign({}, trackArtworkPending)
    pending[artworkId] = true
    trackArtworkPending = pending
    if (sendCommand("artwork:" + artworkId, "artwork:" + artworkId) === 0) {
      delete pending[artworkId]
      trackArtworkPending = Object.assign({}, pending)
    }
  }

  function commandValue(action, value) {
    if (action === "launch") return "show"
    if (action === "seek") return "seek:" + String(value)
    if (action === "play") return "play:" + String(value)
    return action
  }

  function sendCommand(command, kind) {
    var socket = activeSocket
    if (!socket || !socket.connected) return 0
    var id = nextRequestId++
    pendingRequests[String(id)] = String(kind || "action")
    var payload = { id: id, command: command }
    socket.write(JSON.stringify(payload) + "\n")
    socket.flush()
    return id
  }

  function initializeConnection() {
    if (!backendConnected) {
      resetConnectionState()
      return
    }
    if (connectionInitialized) return
    connectionInitialized = true
    running = true
    launchingBackend = false
    socketProbePending = false
    actionBusy = false
    reconnectAttempt = 0
    sendCommand("subscribe", "subscribe")
    sendCommand("status", "status")
    if (popupOpen && running && loggedIn) {
      sendCommand(selectedTab, "action")
      trackReloadTimer.restart()
    }
  }

  function resetConnectionState() {
    connectionInitialized = false
    socketBuffer = ""
    pendingRequests = ({})
    tracksRequestPending = false
    homeLoadMorePending = false
    feedLoadMorePending = false
    trackArtworkPending = ({})
    tracksLoading = false
    trackLoadAttempts = 0
    actionBusy = false
    selectionBusy = false
    activeSelectionRequestId = 0
  }

  function runAction(action, value) {
    if (!SoundCloudModel.canStartAction(actionBusy, selectionBusy, action)) return false
    if (!backendConnected) {
      actionBusy = true
      startBackend(action === "launch" || action === "show")
      return true
    }
    actionBusy = true
    selectionBusy = action === "play"
    lastError = ""
    var sentId = sendCommand(commandValue(action, value), selectionBusy ? "selection" : "action")
    if (sentId > 0) {
      if (selectionBusy) activeSelectionRequestId = sentId
      return true
    }
    actionBusy = false
    selectionBusy = false
    activeSelectionRequestId = 0
    return false
  }

  function formatTime(seconds) {
    var value = Math.max(0, Math.floor(Number(seconds) || 0))
    var hours = Math.floor(value / 3600)
    var minutes = Math.floor((value % 3600) / 60)
    var remainder = value % 60
    if (hours > 0) {
      return hours + ":" + (minutes < 10 ? "0" : "") + minutes
        + ":" + (remainder < 10 ? "0" : "") + remainder
    }
    return minutes + ":" + (remainder < 10 ? "0" : "") + remainder
  }

  function formatCompactCount(value) {
    value = Math.max(0, Math.floor(Number(value) || 0))
    if (value < 1000) return String(value)
    var units = [[1000000000, "B"], [1000000, "M"], [1000, "K"]]
    for (var index = 0; index < units.length; index++) {
      if (value >= units[index][0]) {
        var compact = (value / units[index][0]).toFixed(value >= units[index][0] * 10 ? 0 : 1)
        return compact.replace(/\.0$/, "") + units[index][1]
      }
    }
    return String(value)
  }

  function trackDetails(track) {
    var details = [String(track.artist || "SoundCloud")]
    var playCount = Math.max(0, Math.floor(Number(track.playCount) || 0))
    var durationMs = Math.max(0, Math.floor(Number(track.durationMs) || 0))
    if (playCount > 0) details.push("▶ " + formatCompactCount(playCount))
    details.push(durationMs > 0 ? formatTime(durationMs / 1000) : "—:—")
    return details.join("  •  ")
  }

  function applyStatus(state) {
    state = state || ({})
    running = state.running === true
    var wasLoggedIn = loggedIn
    var reportsLoggedOut = /\/(signin|register)([/?#]|$)/.test(String(state.url || ""))
    loggedIn = state.loggedIn === true || (root.loggedIn && !reportsLoggedOut)
    if (state.error) lastError = String(state.error)
    playbackState = String(state.playbackState || (state.playing === true ? "playing" : "idle"))
    if (selectionBusy && !SoundCloudModel.selectionIsPending(playbackState)) {
      selectionBusy = false
      activeSelectionRequestId = 0
      actionBusy = false
    }
    var preserveMetadata = SoundCloudModel.shouldPreservePlaybackMetadata(state, root.title)
    playing = state.playing === true
    duration = Number(state.duration || 0)
    position = Number(state.position || 0)
    if (!preserveMetadata) {
      var incomingTitle = String(state.title || "").slice(0, 512)
      var artwork = String(state.artDataUrl || "")
      if (state.playerPresent === false) {
        artDataUrl = ""
      } else if (/^data:image\/(png|jpeg);base64,/.test(artwork) && artwork.length <= 180000) {
        artDataUrl = artwork
      } else if (incomingTitle !== title) {
        artDataUrl = ""
      }
      title = incomingTitle
      playbackId = String(state.playbackId || "")
      artist = String(state.artist || "").slice(0, 256)
      waveformLevels = Array.isArray(state.waveform) ? state.waveform : []
    }
    if (popupOpen && loggedIn && !wasLoggedIn && tracks.length === 0) {
      sendCommand(selectedTab, "action")
      trackReloadTimer.restart()
    }
  }

  function handleLine(line) {
    if (String(line || "").length > maxSocketFrameChars) {
      lastError = "SoundCloud response exceeded the security limit"
      return
    }
    var message
    try {
      message = JSON.parse(String(line || ""))
    } catch (error) {
      lastError = "Could not read SoundCloud response"
      return
    }
    if (message.type === "status") {
      applyStatus(message.state)
      return
    }
    if (message.type === "tracks") {
      var messageSource = String(message.source || "")
      setLoadMorePending(messageSource, false)
      if (message.error && Number(message.addedCount || 0) === 0) {
        if (messageSource === "home") homeLoadMoreAtCount = 0
        else if (messageSource === "feed") feedLoadMoreAtCount = 0
      }
      applyTracks(message, messageSource)
      if (message.cached !== true) trackLoadAttempts = 50
      return
    }
    if (message.type !== "response") return
    var key = String(message.id || "")
    var kind = pendingRequests[key]
    delete pendingRequests[key]
    if (kind === "status") applyStatus(message)
    else if (kind && kind.indexOf("tracks:") === 0) {
      tracksRequestPending = false
      var sourceTab = kind.substring(7)
      applyTracks(message, sourceTab)
      if (popupOpen && selectedTab === sourceTab
          && message.pending === true && trackLoadAttempts < 50) {
        trackReloadTimer.restart()
      } else if (popupOpen && selectedTab !== sourceTab) {
        trackLoadAttempts = 0
        trackReloadTimer.restart()
      }
    } else if (kind && kind.indexOf("load-more:") === 0) {
      var requestedSource = kind.substring(10)
      if (message.started !== true) {
        setLoadMorePending(requestedSource, false)
        if (requestedSource === "home") homeLoadMoreAtCount = 0
        else if (requestedSource === "feed") feedLoadMoreAtCount = 0
      }
      if (message.hasMore === false) {
        if (requestedSource === "home") homeHasMore = false
        else if (requestedSource === "feed") feedHasMore = false
      }
      if (message.ok !== true) {
        if (requestedSource === "home") homeLoadMoreAtCount = 0
        else if (requestedSource === "feed") feedLoadMoreAtCount = 0
      }
    } else if (kind && kind.indexOf("artwork:") === 0) {
      var artworkId = kind.substring(8)
      var pendingArtwork = Object.assign({}, trackArtworkPending)
      delete pendingArtwork[artworkId]
      trackArtworkPending = pendingArtwork
      var trackArtDataUrl = String(message.artDataUrl || "")
      if (message.ok === true
          && /^data:image\/(png|jpeg);base64,/.test(trackArtDataUrl)
          && trackArtDataUrl.length <= 45000) {
        var artworkData = Object.assign({}, trackArtworkData)
        if (Object.keys(artworkData).length >= 64) artworkData = ({})
        artworkData[artworkId] = trackArtDataUrl
        trackArtworkData = artworkData
      }
    } else if (kind === "action") {
      actionBusy = false
      if (message.ok !== true) lastError = String(message.error || "SoundCloud action failed")
    } else if (kind === "selection") {
      if (!SoundCloudModel.isCurrentSelectionResponse(activeSelectionRequestId, message.id)) return
      if (message.ok !== true) {
        selectionBusy = false
        activeSelectionRequestId = 0
        actionBusy = false
        lastError = String(message.error || "SoundCloud could not select this track")
      }
    }
  }

  function handleSocketChunk(chunk) {
    socketBuffer += String(chunk || "")
    var newline
    while ((newline = socketBuffer.indexOf("\n")) >= 0) {
      var line = socketBuffer.substring(0, newline)
      socketBuffer = socketBuffer.substring(newline + 1)
      if (line.length > maxSocketFrameChars) {
        socketBuffer = ""
        lastError = "SoundCloud response exceeded the security limit"
        if (activeSocket) activeSocket.connected = false
        return
      }
      if (line !== "") handleLine(line)
    }
    if (socketBuffer.length > maxSocketFrameChars) {
      socketBuffer = ""
      lastError = "SoundCloud response exceeded the security limit"
      if (activeSocket) activeSocket.connected = false
    }
  }

  onBackendConnectedChanged: initializeConnection()

  visible: true
  implicitWidth: row.implicitWidth + Style.space(14)
  implicitHeight: barSize

  Row {
    id: row
    anchors.centerIn: parent
    spacing: Style.space(6)

    Text {
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      text: ""
      color: SoundCloudModel.barIconColor(root.playing, Color.accent, root.bar.barForeground)
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.body
    }

  }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton

    onClicked: function(mouse) {
      if (mouse.button === Qt.MiddleButton) root.runAction(root.running ? "play-pause" : "launch")
      else if (mouse.button === Qt.RightButton && root.running) root.runAction("next")
      else root.toggle()
    }
    onWheel: function(wheel) {
      if (!root.running) return
      root.runAction(wheel.angleDelta.y > 0 ? "previous" : "next")
    }
    onEntered: root.bar.showTooltip(root, root.safeTooltipLabel)
    onExited: root.bar.hideTooltip(root)
  }

  PopupCard {
    id: popup
    anchorItem: root
    bar: root.bar
    owner: root
    open: root.popupOpen
    contentWidth: popup.fittedContentWidth(Style.space(408))
    contentHeight: popup.fittedContentHeight(Style.space(480))

    Column {
      id: content
      anchors.fill: parent
      spacing: Style.space(12)

      Row {
        width: parent.width
        spacing: Style.space(10)

        BorderSurface {
          width: Style.space(68)
          height: Style.space(68)
          radius: Style.spacing.labelGap
          color: Style.normalFillFor(root.bar.foreground, Color.accent)
          borderSpec: Border.controlSpec("normal", root.bar.foreground, Color.accent)

          Image {
            anchors.fill: parent
            anchors.margins: Style.space(2)
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            sourceSize.width: 256
            sourceSize.height: 256
            source: root.artDataUrl
            visible: source !== ""
          }

          Text {
            anchors.centerIn: parent
            visible: root.artDataUrl === ""
            textFormat: Text.PlainText
            text: ""
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.displayLarge
          }
        }

        Column {
          width: parent.width - Style.space(78)
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(3)

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: root.title || (root.loggedIn ? "Nothing playing" : "SoundCloud")
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.subtitle
            font.bold: true
            elide: Text.ElideRight
          }

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: SoundCloudModel.pendingLabel(root.playbackState) || root.artist || (!root.running ? "Backend is stopped" : (!root.loggedIn ? "Sign in once to continue" : "Nothing playing"))
            color: root.dim
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
          }
        }
      }

      Row {
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: Style.space(7)
        visible: root.running && root.loggedIn
        opacity: root.hasTrack ? 1 : 0.45

        Button {
          iconText: "󰒮"
          foreground: root.bar.foreground
          enabled: !root.actionBusy && root.hasTrack
          opacity: enabled ? 1 : 0.4
          onClicked: root.runAction("previous")
        }
        Button {
          iconText: root.playIcon
          foreground: root.bar.foreground
          iconSize: Style.font.iconLarge
          enabled: !root.actionBusy && root.hasTrack
          opacity: enabled ? 1 : 0.4
          onClicked: root.runAction("play-pause")
        }
        Button {
          iconText: "󰒭"
          foreground: root.bar.foreground
          enabled: !root.actionBusy && root.hasTrack
          opacity: enabled ? 1 : 0.4
          onClicked: root.runAction("next")
        }
      }

      PanelSeparator {
        visible: root.running && root.loggedIn
        foreground: root.bar.foreground
      }

      Column {
        width: parent.width
        spacing: Style.space(3)
        visible: root.running && root.loggedIn
        opacity: root.hasTrack ? 1 : 0.45

        Canvas {
          id: waveform
          width: parent.width
          height: Style.space(42)

          property real playedRatio: root.duration > 0
            ? Math.max(0, Math.min(1, root.position / root.duration))
            : 0

          property var levels: root.waveformLevels
          property color playedColor: Color.accent

          onPlayedRatioChanged: requestPaint()
          onLevelsChanged: requestPaint()
          onPlayedColorChanged: requestPaint()
          onWidthChanged: requestPaint()
          onHeightChanged: requestPaint()

          Connections {
            target: root.bar
            function onForegroundChanged() { waveform.requestPaint() }
          }

          onPaint: {
            var context = getContext("2d")
            context.reset()
            var dimColor = Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.28)
            if (!levels || levels.length === 0) {
              // No real waveform for this track: plain progress bar.
              var lineHeight = 4
              var lineY = (height - lineHeight) / 2
              context.fillStyle = dimColor
              context.fillRect(0, lineY, width, lineHeight)
              context.fillStyle = playedColor
              context.fillRect(0, lineY, width * playedRatio, lineHeight)
              return
            }
            var count = levels.length
            var gap = 2
            var barWidth = Math.max(1, (width - (count - 1) * gap) / count)
            for (var index = 0; index < count; index++) {
              var barHeight = Math.max(3, height * Number(levels[index]) / 100)
              var x = index * (barWidth + gap)
              var y = (height - barHeight) / 2
              context.fillStyle = index / count <= playedRatio
                ? playedColor
                : dimColor
              context.fillRect(x, y, barWidth, barHeight)
            }
          }

          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            enabled: root.hasTrack && root.duration > 0 && !root.actionBusy
            onClicked: function(mouse) {
              var ratio = Math.max(0, Math.min(1, mouse.x / width))
              root.position = root.duration * ratio
              waveform.requestPaint()
              root.runAction("seek", ratio)
            }
          }
        }

        Row {
          width: parent.width

          Text {
            textFormat: Text.PlainText
            text: root.formatTime(root.position)
            color: root.dim
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }

          Item { width: parent.width - parent.children[0].implicitWidth - parent.children[2].implicitWidth; height: 1 }

          Text {
            textFormat: Text.PlainText
            text: root.formatTime(root.duration)
            color: root.dim
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }

      Row {
        spacing: Style.space(8)
        visible: root.running && root.loggedIn

        Button {
          text: "Home"
          foreground: root.bar.foreground
          enabled: !root.actionBusy
          selected: root.selectedTab === "home"
          onClicked: root.selectTab("home")
        }

        Button {
          text: "Feed"
          foreground: root.bar.foreground
          enabled: !root.actionBusy
          selected: root.selectedTab === "feed"
          onClicked: root.selectTab("feed")
        }
      }

      Item {
        width: parent.width
        height: Math.max(0, parent.height - y - (errorText.visible
          ? errorText.implicitHeight + parent.spacing : 0))
        visible: root.running && root.loggedIn

        ListView {
          id: trackList
          anchors.fill: parent
          anchors.rightMargin: Style.space(6)
          clip: true
          spacing: Style.space(3)
          boundsBehavior: Flickable.StopAtBounds
          model: root.tracks
          onMovementEnded: root.loadMoreTracks(
            visibleArea.yPosition, visibleArea.heightRatio, atYEnd, true)

          footer: Item {
            width: trackList.width
            height: root.loadMorePending ? Style.space(28) : 0

            Text {
              anchors.centerIn: parent
              visible: root.loadMorePending
              textFormat: Text.PlainText
              text: "Loading more…"
              color: root.dim
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          delegate: Item {
            required property var modelData
            width: trackList.width
            height: Style.space(56)
            property var track: modelData
            property string artworkId: String(track.artworkId || "")
            property string trackArtDataUrl: String(root.trackArtworkData[artworkId] || "")
            property bool isCurrent: SoundCloudModel.isCurrentTrack(track.playbackId, root.playbackId)
            property string pendingLabel: isCurrent ? SoundCloudModel.pendingLabel(root.playbackState) : ""

            Component.onCompleted: root.requestTrackArtwork(artworkId)
            onArtworkIdChanged: root.requestTrackArtwork(artworkId)

            Rectangle {
              anchors.fill: parent
              radius: Style.spacing.labelGap
              color: Style.hoverFillFor(root.bar.foreground, Color.accent)
              visible: trackMouse.enabled && trackMouse.containsMouse
            }

            Rectangle {
              anchors.right: trackArtwork.left
              anchors.rightMargin: Style.space(3)
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(3)
              height: Style.space(40)
              radius: width / 2
              color: Color.accent
              visible: isCurrent
            }

            BorderSurface {
              id: trackArtwork
              width: Style.space(48)
              height: Style.space(48)
              anchors.left: parent.left
              anchors.leftMargin: Style.space(6)
              anchors.verticalCenter: parent.verticalCenter
              radius: Style.spacing.labelGap
              color: Style.normalFillFor(root.bar.foreground, Color.accent)
              borderSpec: Border.controlSpec("normal", root.bar.foreground, Color.accent)

              Image {
                anchors.fill: parent
                anchors.margins: Style.space(2)
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                sourceSize.width: 96
                sourceSize.height: 96
                source: trackArtDataUrl
                visible: trackArtDataUrl !== ""
              }

              Text {
                anchors.centerIn: parent
                visible: trackArtDataUrl === ""
                textFormat: Text.PlainText
                text: ""
                color: root.dim
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.icon
              }
            }

            Column {
              anchors.left: trackArtwork.right
              anchors.leftMargin: Style.space(9)
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                width: parent.width
                text: track.title || "Untitled"
                textFormat: Text.PlainText
                color: isCurrent ? Color.accent : root.bar.foreground
                font.bold: isCurrent
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
              }

              Text {
                width: parent.width
                text: pendingLabel || root.trackDetails(track)
                textFormat: Text.PlainText
                color: pendingLabel ? Color.accent : root.dim
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }

            MouseArea {
              id: trackMouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              enabled: (!root.actionBusy || root.selectionBusy)
                && /^soundcloud:tracks:[1-9][0-9]*$/.test(String(track.playbackId || ""))
              onClicked: {
                root.runAction("play", modelData.playbackId)
              }
            }
          }
        }

        Rectangle {
          anchors.right: parent.right
          width: Style.space(2)
          y: trackList.visibleArea.yPosition * parent.height
          height: Math.max(Style.space(18), trackList.visibleArea.heightRatio * parent.height)
          radius: width / 2
          color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.35)
          visible: trackList.visibleArea.heightRatio < 1
        }

        Text {
          anchors.centerIn: parent
          visible: root.tracksLoading && root.tracks.length === 0
          textFormat: Text.PlainText
          text: "Loading " + (root.selectedTab === "home" ? "Home" : "Feed") + "…"
          color: root.dim
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.body
        }

        Text {
          anchors.centerIn: parent
          visible: !root.tracksLoading && root.tracks.length === 0
          textFormat: Text.PlainText
          text: "No tracks found"
          color: root.dim
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.body
        }
      }

      Button {
        anchors.horizontalCenter: parent.horizontalCenter
        visible: !root.running || !root.loggedIn
        text: root.running ? "Show SoundCloud sign in" : "Sign in to SoundCloud"
        foreground: root.bar.foreground
        enabled: !root.actionBusy
        onClicked: root.runAction(root.running ? "show" : "launch")
      }

      Text {
        id: errorText
        width: parent.width
        visible: root.lastError !== ""
        textFormat: Text.PlainText
        text: root.lastError
        color: root.bar.urgent
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

    }
  }

  Timer {
    id: trackReloadTimer
    interval: 100
    repeat: false
    onTriggered: root.refreshTracks()
  }

  Timer {
    interval: 30000
    repeat: true
    running: root.backendConnected
    onTriggered: root.sendCommand("status", "status")
  }

  Component {
    id: socketComponent

    Socket {
      path: root.socketPath
      connected: root.backendWanted && root.socketPath !== ""
      parser: SplitParser {
        splitMarker: ""
        onRead: function(chunk) { root.handleSocketChunk(chunk) }
      }
      onConnectionStateChanged: {
        if (connected) root.initializeConnection()
        else {
          root.resetConnectionState()
          root.running = false
        }
      }
    }
  }

  Loader {
    id: socketLoader
    active: false
    sourceComponent: socketComponent
  }

  property int reconnectAttempt: 0

  Timer {
    id: reconnectTimer
    interval: Math.min(1500, 100 + root.reconnectAttempt * 100)
    repeat: root.launchingBackend
    triggeredOnStart: true
    running: root.backendWanted && !root.backendConnected
      && (root.socketProbePending || root.launchingBackend)
    onTriggered: {
      root.socketProbePending = false
      root.reconnectAttempt = Math.min(14, root.reconnectAttempt + 1)
      socketLoader.active = false
      socketLoader.active = true
    }
  }

  Process {
    id: launcherProcess
    running: false
    command: []
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.actionBusy = false
        root.launchingBackend = false
        root.lastError = "Could not start SoundCloud"
      } else {
        root.socketProbePending = true
      }
      reconnectTimer.restart()
    }
  }
}
