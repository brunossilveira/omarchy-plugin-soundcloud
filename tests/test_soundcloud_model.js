const assert = require("node:assert/strict");
const model = require("../SoundCloudModel.js");

assert.equal(model.displayLabel({ trackTitle: "Track", trackArtist: "Artist" }), "Track  ·  Artist");
assert.equal(model.displayLabel(null), "SoundCloud");

const cachedFeed = [{ title: "Cached feed track" }];
const pendingFeed = model.applyTrackResult(cachedFeed, { pending: true, tracks: [] });
assert.deepEqual(pendingFeed.tracks, cachedFeed);
assert.equal(pendingFeed.loading, true);

const refreshedFeed = [{ title: "Refreshed feed track" }];
const completedFeed = model.applyTrackResult(pendingFeed.tracks, {
  pending: false,
  tracks: refreshedFeed,
});
assert.deepEqual(completedFeed.tracks, refreshedFeed);
assert.equal(completedFeed.loading, false);

assert.equal(model.shouldLoadMoreTracks({
  userInitiated: true, atEnd: true, count: 20, requestedCount: 0, pending: false,
}), true);
assert.equal(model.shouldLoadMoreTracks({
  userInitiated: true, atEnd: true, count: 20, requestedCount: 20, pending: false,
}), false);
assert.equal(model.shouldLoadMoreTracks({
  userInitiated: true, atEnd: true, count: 20, requestedCount: 0, pending: true,
}), false);
assert.equal(model.shouldLoadMoreTracks({
  userInitiated: true, atEnd: false, count: 20, requestedCount: 0, pending: false,
}), false);

// Regression: Qt may not report atYEnd even when the user has visibly reached
// the bottom area. Loading must begin from the visible-area ratio instead.
assert.equal(model.shouldLoadMoreTracks({
  userInitiated: true, atEnd: false, yPosition: 0.76, heightRatio: 0.10,
  count: 20, requestedCount: 0, pending: false,
}), true);
assert.equal(model.shouldLoadMoreTracks({
  userInitiated: true, atEnd: false, yPosition: 0.74, heightRatio: 0.10,
  count: 20, requestedCount: 0, pending: false,
}), false);

// Regression: appending a page changes the model and scroll geometry. That
// automatic update must not immediately request another page in a loop.
assert.equal(model.shouldLoadMoreTracks({
  userInitiated: false, atEnd: true, yPosition: 0.90, heightRatio: 0.10,
  count: 21, requestedCount: 11, pending: false,
}), false);

assert.equal(model.contentYAfterTrackUpdate(240, 11, 21), 240);
assert.equal(model.contentYAfterTrackUpdate(240, 21, 21), null);

// A badge restored by SoundCloud without a real audio player is stale. The UI
// must clear it instead of preserving it as an actionable current track.
assert.equal(model.shouldPreservePlaybackMetadata({
  playerPresent: false, title: "", url: "https://soundcloud.com/feed",
}, "Old track"), false);
assert.equal(model.shouldPreservePlaybackMetadata({
  playerPresent: true, title: "", url: "https://soundcloud.com/feed",
}, "Playing track"), true);

assert.equal(model.selectionIsPending("resolving"), true);
assert.equal(model.selectionIsPending("buffering"), true);
assert.equal(model.selectionIsPending("playing"), false);
assert.equal(model.selectionIsPending("paused"), false);
assert.equal(model.selectionIsPending("error"), false);
assert.equal(model.selectionIsPending("idle"), false);
// Every pending state needs visible feedback; settled states show none.
for (const state of ["resolving", "buffering", "playing", "paused", "error", "idle"]) {
  assert.equal(model.pendingLabel(state) !== "", model.selectionIsPending(state));
}

// A newer selection supersedes A while controls remain globally busy.  A's
// eventual response cannot complete or clear B.
assert.equal(model.canStartAction(true, true, "play"), true);
assert.equal(model.canStartAction(true, true, "next"), false);
assert.equal(model.isCurrentSelectionResponse(42, 41), false);
assert.equal(model.isCurrentSelectionResponse(42, 42), true);

assert.equal(model.isCurrentTrack("soundcloud:tracks:9", "soundcloud:tracks:9"), true);
assert.equal(model.isCurrentTrack("soundcloud:tracks:8", "soundcloud:tracks:9"), false);
// Nothing playing must not mark rows that also lack an id.
assert.equal(model.isCurrentTrack("", ""), false);
assert.equal(model.isCurrentTrack(undefined, ""), false);

// The bar follows the theme palette: playback uses the theme accent, while
// every non-playing state matches the other bar icons.
assert.equal(model.barIconColor(true, "accent", "foreground"), "accent");
assert.equal(model.barIconColor(false, "accent", "foreground"), "foreground");

var requestEvents = [];
function observeListEvent(reason, count, hasMore) {
  if (model.shouldLoadMoreTracks({
    userInitiated: reason === "movement-ended",
    hasMore: hasMore,
    atEnd: false,
    yPosition: 0.86,
    heightRatio: 0.1,
    count: count,
    requestedCount: requestEvents.length ? 11 : 0,
    pending: false
  })) requestEvents.push({ reason: reason, count: count });
}

observeListEvent("movement-ended", 11, true);
observeListEvent("page-appended", 21, true);
assert.deepEqual(requestEvents, [{ reason: "movement-ended", count: 11 }]);
observeListEvent("movement-ended", 21, true);
assert.equal(requestEvents.length, 2);
observeListEvent("movement-ended", 31, false);
assert.equal(requestEvents.length, 2);

console.log("SoundCloudModel tests passed");
