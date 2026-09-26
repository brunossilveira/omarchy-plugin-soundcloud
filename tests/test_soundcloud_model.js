const assert = require("node:assert/strict");
const model = require("../SoundCloudModel.js");

const exact = {
  dbusName: "org.mpris.MediaPlayer2.webkit.instance42",
  identity: "SoundCloud",
  desktopEntry: "omarchy-soundcloud",
};
const fallback = {
  dbusName: "org.mpris.MediaPlayer2.webkit.instance77",
  identity: "WebKit Media Session",
};
const chrome = {
  dbusName: "org.mpris.MediaPlayer2.chromium.instance5",
  identity: "Chrome",
  trackTitle: "Other media",
};

assert.equal(model.isExactSoundCloudPlayer(exact), true);
assert.equal(model.isExactSoundCloudPlayer(chrome), false);
assert.equal(model.isWebKitPlayer(fallback), true);
assert.equal(model.isWebKitPlayer(chrome), false);
assert.equal(model.pickPlayer([chrome, fallback, exact]), exact);
assert.equal(model.pickPlayer([chrome, fallback]), fallback);
assert.equal(model.pickPlayer([chrome]), null);
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
