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
  property bool liked: false
  property bool likeBusy: false
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
  property var searchTracks: []
  property string searchKind: "tracks"
  property var likesTracks: []
  property bool likesPending: false
  property bool likesLoaded: false
  property var openedCollection: null
  property var collectionTracks: []
  property bool collectionPending: false
  property real volume: 1
  property string searchQuery: ""
  property bool searchPending: false
  property var trackArtworkData: ({})
  property var trackArtworkPending: ({})
  property bool tracksLoading: false
  property bool homeLoadMorePending: false
  property bool feedLoadMorePending: false
  readonly property bool loadMorePending: selectedTab === "home"
    ? homeLoadMorePending : (selectedTab === "feed" ? feedLoadMorePending : false)
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
    if (selectedTab === "search") focusSearchField()
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

  function isPageTab(tab) { return tab === "home" || tab === "feed" }

  // The list a played track belongs to; the backend queues it for next/previous.
  function listSource() {
    if (selectedTab === "search") return openedCollection ? "collection" : "search"
    return selectedTab
  }

  function refreshTracks() {
    if (!isPageTab(selectedTab)) return
    if (!popupOpen || tracksRequestPending || !backendConnected || !running || !loggedIn) return
    tracksLoading = true
    trackLoadAttempts += 1
    tracksRequestPending = sendCommand("tracks:" + selectedTab, "tracks:" + selectedTab) > 0
  }

  function selectTab(tab) {
    if (tab === "search") {
      selectedTab = tab
      tracks = openedCollection ? collectionTracks : searchTracks
      tracksLoading = openedCollection ? collectionPending : searchPending
      if (!openedCollection) focusSearchField()
      return
    }
    if (tab === "likes") {
      selectedTab = tab
      tracks = likesTracks
      tracksLoading = likesPending || !likesLoaded
      if (!likesLoaded && !likesPending) loadLikes()
      return
    }
    selectedTab = tab
    tracks = tab === "home" ? homeTracks : feedTracks
    tracksLoading = true
    trackLoadAttempts = 0
    runAction(tab)
    refreshTracks()
  }

  // The popup only gets keyboard focus once its focus grab is active, which
  // can land after the click that selected the tab. Keep retrying briefly
  // until the field actually holds focus.
  function focusSearchField() {
    searchFocusTimer.attempts = 0
    searchFocusTimer.restart()
  }

  function runSearch(text) {
    var query = String(text || "").trim()
    if (query === "" || !backendConnected || !running || !loggedIn) return
    searchQuery = query
    searchPending = true
    openedCollection = null
    lastError = ""
    if (selectedTab === "search") {
      tracks = []
      tracksLoading = true
    }
    var prefix = searchKind === "playlists" ? "search-playlists:"
      : (searchKind === "users" ? "search-users:" : "search:")
    if (sendCommand(prefix + query, "search") === 0) {
      searchPending = false
      tracksLoading = false
    }
  }

  function setSearchKind(kind) {
    if (searchKind === kind) return
    searchKind = kind
    if (searchQuery !== "") runSearch(searchQuery)
    else focusSearchField()
  }

  function applySearchResult(message) {
    var kind = String(message.kind || "tracks")
    if (String(message.query || "") !== searchQuery || kind !== searchKind) return
    searchPending = false
    if (kind === "tracks") {
      searchTracks = Array.isArray(message.tracks) ? message.tracks.slice(0, 100) : []
    } else {
      // Playlists and artists open into their tracks when clicked.
      var rows = []
      var collections = Array.isArray(message.collections) ? message.collections : []
      for (var index = 0; index < collections.length && index < 50; index++) {
        var item = collections[index]
        rows.push({
          collection: true, kind: String(item.kind || ""), id: Number(item.id) || 0,
          title: String(item.title || ""), artist: String(item.subtitle || ""),
          artworkId: String(item.artworkId || "")
        })
      }
      searchTracks = rows
    }
    if (message.ok !== true) lastError = String(message.error || "SoundCloud search failed")
    if (selectedTab === "search" && !openedCollection) {
      tracks = searchTracks
      tracksLoading = false
      trackList.contentY = trackList.originY
    }
  }

  function openCollection(row) {
    if (!row || !row.collection || !(row.id > 0)) return
    openedCollection = { kind: row.kind, id: row.id, title: row.title }
    collectionTracks = []
    collectionPending = true
    lastError = ""
    tracksLoading = true
    tracks = collectionTracks
    if (sendCommand("open:" + row.kind + ":" + row.id, "collection") === 0) {
      collectionPending = false
      tracksLoading = false
    }
  }

  function closeCollection() {
    openedCollection = null
    collectionPending = false
    if (selectedTab === "search") {
      tracks = searchTracks
      tracksLoading = searchPending
    }
  }

  function applyCollectionResult(message) {
    if (!openedCollection || Number(message.id) !== openedCollection.id) return
    collectionPending = false
    collectionTracks = Array.isArray(message.tracks) ? message.tracks.slice(0, 100) : []
    if (message.ok !== true) lastError = String(message.error || "SoundCloud could not open this")
    if (selectedTab === "search") {
      tracks = collectionTracks
      tracksLoading = false
      trackList.contentY = trackList.originY
    }
  }

  function loadLikes() {
    if (likesPending || !backendConnected || !running || !loggedIn) return
    likesPending = true
    if (selectedTab === "likes") tracksLoading = true
    if (sendCommand("likes", "likes") === 0) {
      likesPending = false
      if (selectedTab === "likes") tracksLoading = false
    }
  }

  function applyLikesResult(message) {
    likesPending = false
    if (message.ok === true) {
      likesLoaded = true
      likesTracks = Array.isArray(message.tracks) ? message.tracks.slice(0, 100) : []
    } else {
      lastError = String(message.error || "SoundCloud could not load your likes")
    }
    if (selectedTab === "likes") {
      tracks = likesTracks
      tracksLoading = false
    }
  }

  function refreshCurrentList() {
    lastError = ""
    if (isPageTab(selectedTab)) {
      tracksLoading = true
      trackLoadAttempts = 0
      sendCommand("refresh:" + selectedTab, "refresh")
      trackReloadTimer.restart()
    } else if (selectedTab === "likes") {
      loadLikes()
    } else if (openedCollection) {
      openCollection({ collection: true, kind: openedCollection.kind,
        id: openedCollection.id, title: openedCollection.title })
    } else if (searchQuery !== "") {
      runSearch(searchQuery)
    }
  }

  function setVolume(value) {
    var next = Math.max(0, Math.min(1, Number(value)))
    if (!isFinite(next)) return
    volume = Math.round(next * 100) / 100
    volumeSendTimer.restart()
  }

  function loadMoreTracks(yPosition, heightRatio, atEnd, userInitiated) {
    if (!isPageTab(selectedTab)) return
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

  function toggleLike() {
    if (likeBusy || playbackId === "") return
    likeBusy = sendCommand((liked ? "unlike:" : "like:") + playbackId, "like") > 0
  }

  function commandValue(action, value) {
    if (action === "launch") return "show"
    if (action === "seek") return "seek:" + String(value)
    if (action === "play") return "play-in:" + listSource() + ":" + String(value)
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
    if (popupOpen && running && loggedIn && isPageTab(selectedTab)) {
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
    searchPending = false
    likesPending = false
    collectionPending = false
    trackArtworkPending = ({})
    tracksLoading = false
    trackLoadAttempts = 0
    actionBusy = false
    selectionBusy = false
    activeSelectionRequestId = 0
    likeBusy = false
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
    if (playCount > 0) details.push("▶ " + formatCompactCount(playCount))
    return details.join("  •  ")
  }

  function trackDuration(track) {
    var durationMs = Math.max(0, Math.floor(Number(track.durationMs) || 0))
    return durationMs > 0 ? formatTime(durationMs / 1000) : "—:—"
  }

  function applyStatus(state) {
    state = state || ({})
    running = state.running === true
    var wasLoggedIn = loggedIn
    var reportsLoggedOut = /\/(signin|register)([/?#]|$)/.test(String(state.url || ""))
    loggedIn = state.loggedIn === true || (root.loggedIn && !reportsLoggedOut)
    if (state.error) lastError = String(state.error)
    playbackState = String(state.playbackState || (state.playing === true ? "playing" : "idle"))
    if (typeof state.volume === "number" && isFinite(state.volume) && !volumeSendTimer.running)
      volume = Math.max(0, Math.min(1, state.volume))
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
      liked = state.liked === true
      artist = String(state.artist || "").slice(0, 256)
      waveformLevels = Array.isArray(state.waveform) ? state.waveform : []
    }
    if (popupOpen && loggedIn && !wasLoggedIn && tracks.length === 0 && isPageTab(selectedTab)) {
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
    } else if (kind === "search") {
      applySearchResult(message)
    } else if (kind === "likes") {
      applyLikesResult(message)
    } else if (kind === "collection") {
      applyCollectionResult(message)
    } else if (kind === "like") {
      likeBusy = false
      if (message.ok !== true) lastError = String(message.error || "SoundCloud could not update this like")
      else if (SoundCloudModel.isCurrentTrack(message.playbackId, root.playbackId)) liked = message.liked === true
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
  Component.onCompleted: desktopEntryProcess.running = true

  visible: true
  implicitWidth: row.implicitWidth + Style.space(14)
  implicitHeight: barSize

  // Now-playing details in the bar itself (horizontal bars only).
  readonly property bool showNowPlaying: !root.vertical && root.hasTrack
    && root.setting("showTitle", true) !== false
  readonly property bool showBarArt: showNowPlaying && root.artDataUrl !== ""

  readonly property bool showBarControls: showNowPlaying && root.running && root.loggedIn
    && root.setting("showControls", true) !== false

  Row {
    id: row
    anchors.centerIn: parent
    spacing: Style.space(6)
    z: 1

    Text {
      anchors.verticalCenter: parent.verticalCenter
      visible: !root.showBarArt
      textFormat: Text.PlainText
      text: ""
      color: SoundCloudModel.barIconColor(root.playing, Color.accent, root.bar.barForeground)
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.body
    }

    Image {
      id: barArt
      anchors.verticalCenter: parent.verticalCenter
      visible: root.showBarArt
      width: Math.round(root.barSize * 0.62)
      height: width
      fillMode: Image.PreserveAspectCrop
      asynchronous: true
      smooth: true
      sourceSize.width: 64
      sourceSize.height: 64
      source: root.showBarArt ? root.artDataUrl : ""
      opacity: root.playing ? 1 : 0.6
    }

    Item {
      id: marquee
      anchors.verticalCenter: parent.verticalCenter
      visible: root.showNowPlaying
      readonly property real maxWidth: Style.space(Number(root.setting("titleWidth", 170)) || 170)
      readonly property real overflow: Math.max(0, marqueeText.implicitWidth - maxWidth)
      width: visible ? Math.min(marqueeText.implicitWidth, maxWidth) : 0
      height: marqueeText.implicitHeight
      clip: true

      Text {
        id: marqueeText
        textFormat: Text.PlainText
        text: root.label
        color: root.playing ? root.bar.barForeground : root.dim
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.bodySmall
        onTextChanged: {
          x = 0
          if (marqueeAnimation.running) marqueeAnimation.restart()
        }
      }

      // Scroll long titles back and forth, pausing at each end.
      SequentialAnimation {
        id: marqueeAnimation
        running: marquee.visible && marquee.overflow > 0
        loops: Animation.Infinite
        onRunningChanged: if (!running) marqueeText.x = 0
        PauseAnimation { duration: 2000 }
        NumberAnimation {
          target: marqueeText
          property: "x"
          from: 0
          to: -marquee.overflow
          duration: Math.max(1200, marquee.overflow * 35)
          easing.type: Easing.Linear
        }
        PauseAnimation { duration: 1500 }
        NumberAnimation {
          target: marqueeText
          property: "x"
          to: 0
          duration: 500
          easing.type: Easing.InOutQuad
        }
      }
    }

    Text {
      id: barPrevious
      anchors.verticalCenter: parent.verticalCenter
      leftPadding: Style.space(2)
      rightPadding: Style.space(2)
      visible: root.showBarControls
      textFormat: Text.PlainText
      text: "󰒮"
      color: barPreviousMouse.containsMouse ? Color.accent : root.bar.barForeground
      opacity: enabled ? 1 : 0.4
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.icon

      MouseArea {
        id: barPreviousMouse
        anchors.fill: parent
        anchors.margins: -Style.space(3)
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        enabled: !root.actionBusy
        onClicked: root.runAction("previous")
      }
    }

    Text {
      id: barPlayPause
      anchors.verticalCenter: parent.verticalCenter
      leftPadding: Style.space(2)
      rightPadding: Style.space(2)
      visible: root.showBarControls
      textFormat: Text.PlainText
      text: root.playIcon
      color: barPlayPauseMouse.containsMouse ? Color.accent : root.bar.barForeground
      opacity: enabled ? 1 : 0.4
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.icon

      MouseArea {
        id: barPlayPauseMouse
        anchors.fill: parent
        anchors.margins: -Style.space(3)
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        enabled: !root.actionBusy
        onClicked: root.runAction("play-pause")
        onWheel: function(wheel) {
          root.setVolume(root.volume + (wheel.angleDelta.y > 0 ? 0.05 : -0.05))
          root.bar.showTooltip(root, "Volume " + Math.round(root.volume * 100) + "%")
        }
        onExited: root.bar.hideTooltip(root)
      }
    }

    Text {
      id: barNext
      anchors.verticalCenter: parent.verticalCenter
      leftPadding: Style.space(2)
      rightPadding: Style.space(2)
      visible: root.showBarControls
      textFormat: Text.PlainText
      text: "󰒭"
      color: barNextMouse.containsMouse ? Color.accent : root.bar.barForeground
      opacity: enabled ? 1 : 0.4
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.icon

      MouseArea {
        id: barNextMouse
        anchors.fill: parent
        anchors.margins: -Style.space(3)
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        enabled: !root.actionBusy
        onClicked: root.runAction("next")
      }
    }

    Text {
      id: barLike
      anchors.verticalCenter: parent.verticalCenter
      leftPadding: Style.space(2)
      rightPadding: Style.space(2)
      visible: root.showBarControls && root.playbackId !== ""
      textFormat: Text.PlainText
      text: "󰋑"
      color: barLikeMouse.containsMouse ? Color.accent : root.liked ? Color.accent : root.bar.barForeground
      opacity: root.likeBusy ? 0.5 : 1
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.icon

      MouseArea {
        id: barLikeMouse
        anchors.fill: parent
        anchors.margins: -Style.space(3)
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        enabled: !root.likeBusy
        onClicked: root.toggleLike()
      }
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
    contentHeight: popup.fittedContentHeight(Style.space(560))

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
            - (likeButton.visible ? likeButton.width + parent.spacing : 0)
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(3)

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: root.title || (root.running && root.loggedIn ? "Nothing playing" : "SoundCloud")
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.subtitle
            font.bold: true
            elide: Text.ElideRight
          }

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: SoundCloudModel.pendingLabel(root.playbackState) || root.artist || (!root.running ? (root.launchingBackend ? "Connecting…" : "Not connected") : (!root.loggedIn ? "Sign in once to continue" : "Nothing playing"))
            color: root.dim
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
          }
        }

        Text {
          id: likeButton
          anchors.verticalCenter: parent.verticalCenter
          visible: root.running && root.loggedIn && root.playbackId !== ""
          textFormat: Text.PlainText
          text: "󰋑"
          color: root.liked ? Color.accent : root.bar.foreground
          opacity: root.likeBusy ? 0.5 : 1
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.icon

          MouseArea {
            anchors.fill: parent
            anchors.margins: -Style.space(4)
            cursorShape: Qt.PointingHandCursor
            enabled: !root.likeBusy
            onClicked: root.toggleLike()
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
        Rectangle {
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(40)
          height: width
          radius: width / 2
          color: playPauseMouse.containsMouse ? Qt.lighter(Color.accent, 1.15) : Color.accent
          enabled: !root.actionBusy && root.hasTrack
          opacity: enabled ? 1 : 0.4

          Text {
            anchors.centerIn: parent
            textFormat: Text.PlainText
            text: root.playIcon
            color: root.bar.background
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.iconLarge
          }

          MouseArea {
            id: playPauseMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.runAction("play-pause")
          }
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
        width: parent.width
        spacing: Style.space(8)
        visible: root.running && root.loggedIn

        Text {
          id: volumeIcon
          anchors.verticalCenter: parent.verticalCenter
          textFormat: Text.PlainText
          text: "󰕾"
          color: root.dim
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.icon
        }

        PanelSlider {
          anchors.verticalCenter: parent.verticalCenter
          width: parent.width - volumeIcon.width - volumePercent.width - parent.spacing * 2
          bar: root.bar
          value: root.volume
          step: 0.01
          onMoved: function(value) { root.setVolume(value) }
          onReleased: function(value) { root.setVolume(value) }
        }

        Text {
          id: volumePercent
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(34)
          horizontalAlignment: Text.AlignRight
          textFormat: Text.PlainText
          text: Math.round(root.volume * 100) + "%"
          color: root.dim
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
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

        Button {
          text: "Likes"
          foreground: root.bar.foreground
          selected: root.selectedTab === "likes"
          onClicked: root.selectTab("likes")
        }

        Button {
          text: "Search"
          foreground: root.bar.foreground
          selected: root.selectedTab === "search"
          onClicked: root.selectTab("search")
        }

        Button {
          iconText: "󰑐"
          foreground: root.bar.foreground
          enabled: !root.actionBusy
          onClicked: root.refreshCurrentList()
        }
      }

      TextField {
        id: searchField
        width: parent.width
        visible: root.running && root.loggedIn && root.selectedTab === "search"
        foreground: root.bar.foreground
        accent: Color.accent
        placeholderText: root.searchKind === "playlists" ? "Search SoundCloud playlists"
          : (root.searchKind === "users" ? "Search SoundCloud artists" : "Search SoundCloud tracks")
        maximumLength: 120
        onAccepted: root.runSearch(text)
        Keys.onEscapePressed: {
          if (text !== "") text = ""
          else root.close()
        }
      }

      Row {
        spacing: Style.space(6)
        visible: searchField.visible && !root.openedCollection

        Repeater {
          model: [["tracks", "Tracks"], ["playlists", "Playlists"], ["users", "Artists"]]
          delegate: Button {
            required property var modelData
            text: modelData[1]
            foreground: root.bar.foreground
            selected: root.searchKind === modelData[0]
            onClicked: root.setSearchKind(modelData[0])
          }
        }
      }

      Row {
        width: parent.width
        spacing: Style.space(8)
        visible: root.running && root.loggedIn && root.selectedTab === "search" && !!root.openedCollection

        Button {
          id: collectionBack
          iconText: "󰅁"
          foreground: root.bar.foreground
          onClicked: root.closeCollection()
        }

        Text {
          anchors.verticalCenter: parent.verticalCenter
          width: parent.width - collectionBack.width - parent.spacing
          textFormat: Text.PlainText
          text: root.openedCollection ? root.openedCollection.title : ""
          color: root.bar.foreground
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
          elide: Text.ElideRight
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
            property bool failed: isCurrent && root.playbackState === "error"

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
              anchors.right: trackDurationText.left
              anchors.rightMargin: Style.space(9)
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
                text: pendingLabel || (failed ? root.lastError || "Could not play this track" : root.trackDetails(track))
                textFormat: Text.PlainText
                color: failed ? root.bar.urgent : (pendingLabel ? Color.accent : root.dim)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }

            Text {
              id: trackDurationText
              anchors.right: parent.right
              anchors.rightMargin: Style.space(6)
              anchors.verticalCenter: parent.verticalCenter
              text: track.collection ? "󰅂" : root.trackDuration(track)
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
            }

            MouseArea {
              id: trackMouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              enabled: track.collection === true || ((!root.actionBusy || root.selectionBusy)
                && /^soundcloud:tracks:[1-9][0-9]*$/.test(String(track.playbackId || "")))
              onClicked: {
                if (track.collection === true) root.openCollection(modelData)
                else root.runAction("play", modelData.playbackId)
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
          text: root.selectedTab === "search" ? (root.openedCollection ? "Loading tracks…" : "Searching…")
            : "Loading " + ({ home: "Home", feed: "Feed", likes: "Likes" })[root.selectedTab] + "…"
          color: root.dim
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.body
        }

        Column {
          anchors.centerIn: parent
          visible: !root.tracksLoading && root.tracks.length === 0
          spacing: Style.space(8)

          Text {
            anchors.horizontalCenter: parent.horizontalCenter
            textFormat: Text.PlainText
            text: root.selectedTab === "likes" ? "No liked tracks yet"
              : (root.selectedTab !== "search" || root.openedCollection ? "No tracks found"
              : (root.searchQuery === "" ? "Type a search and press Enter" : "No results for “" + root.searchQuery + "”"))
            color: root.dim
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.body
          }

          Button {
            anchors.horizontalCenter: parent.horizontalCenter
            text: "Retry"
            visible: root.selectedTab !== "search" || root.searchQuery !== ""
            foreground: root.bar.foreground
            enabled: !root.actionBusy
            onClicked: root.refreshCurrentList()
          }
        }
      }

      Text {
        width: parent.width
        visible: root.running && !root.loggedIn
        textFormat: Text.PlainText
        text: "A SoundCloud window opens. Sign in there once, then close it. Playback keeps running and you stay signed in."
        color: root.dim
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
      }

      Button {
        anchors.horizontalCenter: parent.horizontalCenter
        visible: !root.running || !root.loggedIn
        text: root.running ? "Show SoundCloud sign in" : "Connect"
        foreground: root.bar.foreground
        enabled: !root.actionBusy
        onClicked: root.runAction(root.running ? "show" : "ensure")
      }

      Text {
        id: errorText
        width: parent.width
        visible: root.lastError !== ""
          && !SoundCloudModel.errorShownOnRow(root.tracks, root.playbackId, root.playbackState)
        textFormat: Text.PlainText
        text: root.lastError
        color: root.bar.urgent
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

    }
  }

  // Coalesce slider drags and wheel ticks into one volume command.
  Timer {
    id: volumeSendTimer
    interval: 90
    repeat: false
    onTriggered: root.sendCommand("volume:" + root.volume.toFixed(2), "volume")
  }

  Timer {
    id: searchFocusTimer
    property int attempts: 0
    interval: 40
    repeat: true
    onTriggered: {
      attempts += 1
      if (root.popupOpen && root.selectedTab === "search" && searchField.visible)
        searchField.forceActiveFocus()
      if (searchField.activeFocus || attempts >= 15 || !root.popupOpen) stop()
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
          // Drop the last track so a dead backend never looks like it is playing.
          root.applyStatus({ running: false, playerPresent: false })
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
    id: desktopEntryProcess
    running: false
    command: ["/usr/bin/python3", "-I", helperPath, "install-desktop-entry"]
  }

  Process {
    id: launcherProcess
    running: false
    command: []
    stdout: SplitParser {
      onRead: function(line) {
        var error = SoundCloudModel.launchError(line)
        if (error) root.lastError = error
      }
    }
    onStarted: root.lastError = ""
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.actionBusy = false
        root.launchingBackend = false
        if (!root.lastError) root.lastError = "Could not start SoundCloud"
      } else {
        root.socketProbePending = true
      }
      reconnectTimer.restart()
    }
  }
}
