function searchablePlayerText(player) {
  if (!player) return ""
  return [player.dbusName, player.identity, player.desktopEntry]
    .map(function(value) { return String(value || "").toLowerCase() })
    .join(" ")
}

function isExactSoundCloudPlayer(player) {
  return searchablePlayerText(player).indexOf("soundcloud") !== -1
}

function isWebKitPlayer(player) {
  return searchablePlayerText(player).indexOf("webkit") !== -1
}

function pickPlayer(players) {
  var list = Array.isArray(players) ? players : []
  for (var i = 0; i < list.length; i++) {
    if (isExactSoundCloudPlayer(list[i])) return list[i]
  }
  for (var j = 0; j < list.length; j++) {
    if (isWebKitPlayer(list[j])) return list[j]
  }
  return null
}

function displayLabel(player) {
  if (!player) return "SoundCloud"
  var title = String(player.trackTitle || "")
  var artist = String(player.trackArtist || "")
  if (title && artist) return title + "  ·  " + artist
  return title || artist || "SoundCloud"
}

function applyTrackResult(cachedTracks, result) {
  var cached = Array.isArray(cachedTracks) ? cachedTracks : []
  if (result && result.pending === true) {
    return { tracks: cached, loading: true }
  }
  var incoming = result && Array.isArray(result.tracks) ? result.tracks.slice(0, 100) : []
  return { tracks: incoming, loading: false }
}

function shouldLoadMoreTracks(state) {
  state = state || {}
  var count = Math.max(0, Math.floor(Number(state.count) || 0))
  var requestedCount = Math.max(0, Math.floor(Number(state.requestedCount) || 0))
  var yPosition = Number(state.yPosition)
  var heightRatio = Number(state.heightRatio)
  var nearEnd = state.atEnd === true
    || (Number.isFinite(yPosition) && yPosition >= 0
      && Number.isFinite(heightRatio) && heightRatio > 0
      && yPosition + heightRatio >= 0.85)
  return state.userInitiated === true
    && state.hasMore !== false
    && nearEnd
    && state.pending !== true
    && count > 0
    && count > requestedCount
}

function contentYAfterTrackUpdate(previousY, previousCount, nextCount) {
  var y = Number(previousY)
  var before = Math.max(0, Math.floor(Number(previousCount) || 0))
  var after = Math.max(0, Math.floor(Number(nextCount) || 0))
  return Number.isFinite(y) && after > before ? y : null
}

function shouldPreservePlaybackMetadata(state, currentTitle) {
  state = state || {}
  return state.playerPresent !== false
    && String(state.title || "") === ""
    && String(currentTitle || "") !== ""
    && /^https:\/\/soundcloud\.com\/(discover|feed)([/?#]|$)/.test(String(state.url || ""))
}

function selectionIsPending(playbackState) {
  return playbackState === "resolving" || playbackState === "buffering"
}

function canStartAction(actionBusy, selectionBusy, action) {
  return !actionBusy || (selectionBusy === true && action === "play")
}

function isCurrentSelectionResponse(activeRequestId, responseId) {
  return Number(activeRequestId) > 0 && Number(activeRequestId) === Number(responseId)
}

function barIconColor(playing, accent, foreground) {
  return playing === true ? accent : foreground
}

if (typeof module !== "undefined") {
  module.exports = {
    searchablePlayerText: searchablePlayerText,
    isExactSoundCloudPlayer: isExactSoundCloudPlayer,
    isWebKitPlayer: isWebKitPlayer,
    pickPlayer: pickPlayer,
    displayLabel: displayLabel,
    applyTrackResult: applyTrackResult,
    shouldLoadMoreTracks: shouldLoadMoreTracks,
    contentYAfterTrackUpdate: contentYAfterTrackUpdate,
    shouldPreservePlaybackMetadata: shouldPreservePlaybackMetadata,
    selectionIsPending: selectionIsPending,
    canStartAction: canStartAction,
    isCurrentSelectionResponse: isCurrentSelectionResponse,
    barIconColor: barIconColor
  }
}
