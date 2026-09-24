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

console.log("SoundCloudModel tests passed");
