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
  var incoming = result && Array.isArray(result.tracks) ? result.tracks.slice(0, 50) : []
  return { tracks: incoming, loading: false }
}

if (typeof module !== "undefined") {
  module.exports = {
    searchablePlayerText: searchablePlayerText,
    isExactSoundCloudPlayer: isExactSoundCloudPlayer,
    isWebKitPlayer: isWebKitPlayer,
    pickPlayer: pickPlayer,
    displayLabel: displayLabel,
    applyTrackResult: applyTrackResult
  }
}
