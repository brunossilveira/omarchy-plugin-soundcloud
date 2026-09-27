# AGENTS.md

Guidance for coding agents working in this repository.

## What this is

An [Omarchy](https://omarchy.org) 4 bar-widget plugin (`brunosilveira.soundcloud`) that puts SoundCloud
playback and browsing into the Omarchy top bar, with no browser tab and no official
SoundCloud API credentials (those require Artist Pro).

It works by running a hidden, sandboxed WebKitGTK instance that is signed in to
soundcloud.com, intercepting the web frontend's own private `api-v2.soundcloud.com` JSON
calls to build the Home and Feed track lists and to resolve short-lived stream URLs,
and then playing the audio locally through GStreamer. The bar widget (QML, running
inside `omarchy-shell` / Quickshell) talks to that backend over a Unix socket.

The user-facing documentation is `README.md`; keep it in sync when behavior,
requirements, CLI commands, or on-disk state change (a test checks parts of it).

## Layout

```
manifest.json            Omarchy plugin manifest (id, kind=bar-widget, entry point)
BarWidget.qml            Bar icon + popup UI; socket client; all UI state
SoundCloudModel.js       Pure UI decision logic, shared by QML and Node tests
soundcloud_app.py        Backend: CLI, daemon, IPC server, WebKit broker, GStreamer player
tests/test_soundcloud_app.py    Python unittest suite (also drives injected JS via `node -e`)
tests/test_soundcloud_model.js  Node assert tests for SoundCloudModel.js
```

There is no build step, no package manager, and no third-party Python or JS
dependencies. Python uses only the standard library plus PyGObject (`gi`) at runtime.

## Architecture

### Process model

```
omarchy-shell (Quickshell)
  └─ BarWidget.qml ──JSON lines over Unix socket──▶ soundcloud_app.py daemon (Gtk.Application)
                                                     ├─ ControlServer thread (socket accept/read)
                                                     ├─ WebKit2.WebView (hidden; ephemeral profile)
                                                     │    └─ injected api_request_capture_script()
                                                     ├─ GstPlayback (playbin3 / appsrc for HLS)
                                                     └─ ArtworkCache thread pools (i1.sndcdn.com)
```

- `BarWidget.qml` launches the backend only on user interaction, via
  `/usr/bin/python3 -I soundcloud_app.py ensure|launch`. On shell start it only
  probes for an existing socket so playback survives shell restarts.
- `launch_backend()` spawns `soundcloud_app.py daemon [--show]` detached, with a
  whitelisted environment and `PATH=/usr/bin:/bin`.
- The daemon is a `Gtk.Application`. All GTK/WebKit/GStreamer work happens on the
  GLib main loop. Socket threads hand work over with `GLib.idle_add`
  (`_dispatch_from_thread`), bounded by `MAX_PENDING_DISPATCHES`.
- The daemon quits when the plugin install path
  (`~/.config/omarchy/plugins/brunosilveira.soundcloud`) disappears, or 10 s after
  the last status subscriber disconnects.

### IPC protocol

- Socket: `$XDG_RUNTIME_DIR/omarchy-soundcloud/control.sock` (dir 0700, socket 0600,
  `.lock` file with `flock` for single instance). Peer UID is checked with `SO_PEERCRED`.
- Framing: one JSON object per `\n`-terminated line. Requests:
  `{"id": <int>, "command": "<string>"}`. Request max `MAX_REQUEST_BYTES` (4 KiB),
  response max `MAX_RESPONSE_BYTES` (512 KiB); QML enforces the same limit as
  `maxSocketFrameChars`.
- Replies to a request: `{"type": "response", "id": ..., "ok": ..., ...}`.
- Pushed events: `{"type": "status", "state": {...}}` and
  `{"type": "tracks", "source": "home"|"feed", "tracks": [...], "reset", "hasMore", "addedCount", ...}`.
- Commands (see `_handle_command`): `status`, `subscribe`, `tracks:home|feed`,
  `load-more:home|feed`, `artwork:<24-hex token>`, `play-pause`, `next`, `previous`,
  `seek:<0..1>`, `play:soundcloud:tracks:<id>`, `home`, `feed`, `likes`, `show`
  (`launch` is normalized to `show`), `stop`, `inspect-api` (diagnostics, socket only, not in the CLI).
- The QML side tracks in-flight requests in `pendingRequests` keyed by id; the
  `kind` string (`status`, `tracks:home`, `load-more:feed`, `artwork:<id>`,
  `action`, `selection`) decides how the response is applied in `handleLine`.

Adding a command means touching: the Python dispatcher, possibly `parse_args`/`main`
for CLI access, `BarWidget.qml` (`commandValue`/`sendCommand` + `handleLine`), the
README CLI section, and tests on both sides.

### Track lists (Home / Feed)

- `api_request_capture_script()` is injected at document start into the top frame
  of `https://soundcloud.com/*`. It wraps `fetch`/`XMLHttpRequest` to observe the
  frontend's `/mixed-selections` (Home) and `/stream` (Feed) calls, normalizes
  results, and posts them to Python through the `omarchyTracks` script message handler.
- Pagination (`window.__omarchyLoadMore`) follows the validated `next_href` from the
  API, not DOM scrolling, and stops on cursor cycles.
- Python validates every page (`validate_tracks_payload`), merges by URL
  (`merge_track_pages`, cap `MAX_TRACKS` = 100), persists to `tracks.json`, and pushes a
  `tracks` event. Artwork URLs are replaced by opaque 24-hex tokens
  (`_secure_tracks`); QML fetches images with `artwork:<token>`.
- Frontend auth headers are captured in closure scope only. They must never be
  written to page globals, posted to Python, logged, or replayed outside WebKit
  (tests enforce this).

### Playback

1. QML sends `play:soundcloud:tracks:<id>` (a stable track URN, never a DOM card).
2. Python bumps `selection_generation`, calls `GstPlayback.begin()` (state
   `resolving`), and evaluates `play_track_script()`.
3. The injected script resolves the track's transcodings through api-v2 and posts
   `{requestId, streamUrl, protocol, mimeType, track}` to `omarchyPlayback`.
4. Python drops stale generations, validates the stream host (`*.sndcdn.com` /
   `*.soundcloud.cloud`, https, port 443) and starts GStreamer: progressive via
   `playbin3`, HLS via `appsrc` fed by a bounded fetcher (`hls_segment_urls`,
   `fetch_private_media`: no proxies, no redirects, deadlines, size caps).
5. A 15 s timeout (`_resolution_timed_out`) fails selections stuck in `resolving`.

`play-pause`/`seek` use `GstPlayback` when it is active. Otherwise they fall back to
clicking the SoundCloud web player via `command_script`/`seek_script`. `next`/`previous`
always go through the web player. `playbackState` is one of
`idle|resolving|buffering|playing|paused|error`.

### UI (`BarWidget.qml` + `SoundCloudModel.js`)

- The bar shows only an icon; track info goes in the tooltip (sanitized by `plainForHost`).
- The popup has Home/Feed tabs, artwork, metadata, prev/play/next, a decorative
  canvas waveform with click-to-seek, and a lazily loaded `ListView`.
- Every `Text` uses `textFormat: Text.PlainText`. Remote strings must never be
  rendered as rich text.
- Colors follow `root.bar.*` palette. The only hardcoded color is the SoundCloud
  accent `#ff5500`.
- Put branching/decision logic in `SoundCloudModel.js` as pure functions (exported
  via `module.exports` for Node) and cover it in `tests/test_soundcloud_model.js`.
  Keep QML for wiring and layout.

## Security invariants (do not weaken)

This code treats the SoundCloud page and network as untrusted. Most of the
complexity exists to hold these lines:

- Every value crossing from WebKit into Python is schema- and size-bounded
  (`validate_status_payload`, `validate_tracks_payload`, `_bounded_*`, `MAX_*`
  constants) before it reaches IPC or disk.
- Navigation is limited to `SOUNDCLOUD_WEB_HOSTS` (`_decide_policy`).
  `_evaluate` refuses to run scripts on non-SoundCloud URIs.
- Artwork is fetched only from `ARTWORK_HOSTS` (`i1.sndcdn.com`), verified as
  PNG/JPEG by magic bytes and dimensions, and passed to QML as bounded `data:` URLs.
- Private files and dirs are opened descriptor-relative with `O_NOFOLLOW`,
  owner-checked, and kept at mode 0600/0700 (`ensure_private_directory`,
  `validate_private_file`, `CookieStore`, `PrivateJsonStore`). `os.umask(0o077)`
  is set in `main`.
- WebKit uses `WebsiteDataManager.new_ephemeral()`. Session persistence exists only
  via the bounded `cookies.json` jar, which Python restores and saves. The account
  password is never stored.
- The optional pagination log (`log_event`) writes only whitelisted, credential-free
  fields.
- Subprocesses use absolute `/usr/bin/python3 -I` and a whitelisted environment.

When adding a feature, add its limits and validation too, and add a test that
proves the hostile case is rejected.

## State on disk

- `~/.local/share/omarchy-soundcloud/` (or `$XDG_DATA_HOME`): `cookies.json`,
  `tracks.json`. A legacy `cookies.sqlite` is migrated once.
- `$XDG_RUNTIME_DIR/omarchy-soundcloud/`: `control.sock`, `control.lock`,
  `pagination.log`.
- `~/.cache/omarchy-soundcloud/`: computed in `profile_paths` but not used by the
  current daemon. The README's purge steps still remove it.

Removing the plugin keeps the data dir on purpose, so reinstalling does not require
another sign-in.

## Development

Runtime requirements: Omarchy 4 (`omarchy-shell`), Python 3 + PyGObject, GTK 3,
WebKitGTK 4.1, libsoup 3, GStreamer with `gst-plugins-good` and `gst-libav`.

```sh
python3 -m unittest tests/test_soundcloud_app.py -v   # backend + injected-JS harnesses (needs node)
node tests/test_soundcloud_model.js                   # QML model logic
omarchy plugin validate .                             # manifest / plugin validation
python3 soundcloud_app.py check                       # runtime dependency check (JSON)
```

The Python tests do not need GTK or a display. They import the module by path and
exercise pure functions, the socket server, and the injected JS (run under
`node -e` with stubbed `window`/`fetch`/`XMLHttpRequest`).

Live testing (the plugin dir is symlinked into Omarchy):

```sh
ln -s "$PWD" ~/.config/omarchy/plugins/brunosilveira.soundcloud
omarchy-shell shell rescanPlugins
omarchy plugin enable brunosilveira.soundcloud --before omarchy.tray
python3 soundcloud_app.py stop     # restart the daemon after backend changes
python3 soundcloud_app.py status   # inspect state from the CLI
```

QML changes take effect after a plugin rescan or shell reload. Python changes take
effect after the daemon is stopped and relaunched from the bar.

## Conventions

- Keep the single-file layout. `soundcloud_app.py` is intentionally self-contained
  (standard library + `gi` only) so it can run with `python3 -I`. Don't add
  third-party dependencies.
- Injected page JavaScript lives as raw Python string builders (`*_script()`).
  Interpolate values only through `json.dumps` or validated numbers, never raw strings.
- Prefer behavioral tests: call the function, run the socket, or run the injected
  script under Node and assert on outcomes. `BarWidgetTest` has older tests that
  regex `BarWidget.qml` source. Don't add more of those. When QML logic needs
  coverage, move it into `SoundCloudModel.js` and test it there.
- Regression tests carry a comment that explains the real-world failure they guard
  (see `tests/test_soundcloud_model.js`). Keep doing that.
- Every async path that can be superseded (track selection, route navigation,
  artwork) uses a generation or request id, so late results cannot overwrite newer
  state. Keep that pattern for new async work.

## Known fragility

- SoundCloud's private api-v2 schemas and web DOM selectors (in `status_script`,
  `command_script`, `seek_script`, `autoplay_script`) can change without notice.
  When playback or lists break, check these first. The `inspect-api` socket command
  reports the api-v2 paths the page has requested.
- On Hyprland, `WEBKIT_DISABLE_DMABUF_RENDERER=1` is set automatically
  (`apply_graphics_workarounds`) to avoid WebKitGTK explicit-sync crashes.
- Region- or off-platform-restricted tracks keep SoundCloud's normal restrictions.
