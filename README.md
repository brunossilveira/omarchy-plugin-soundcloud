# Omarchy SoundCloud

A dedicated SoundCloud widget for the Omarchy top bar. It keeps the normal UI in the bar rather than in a browser tab.

## Features

- Current track, artist, and artwork
- Play/pause, previous, and next controls
- SoundCloud-style waveform progress display with click-to-seek
- Scrollable Home and Feed tabs for choosing tracks
- Persistent SoundCloud login session
- Hidden WebKitGTK playback backend after sign-in
- No Chromium tab and no SoundCloud API credentials

## Requirements

- Omarchy 4 with `omarchy-shell`
- Python 3 with PyGObject
- GTK 3 and WebKitGTK 4.1
- `gst-plugins-good` (provides WebKitGTK's required `autoaudiosink`)
- `gst-libav` (provides AAC decoding for current SoundCloud HLS streams)

Install the GStreamer plugin package if needed:

```sh
omarchy pkg add gst-plugins-good gst-libav
```

Verify all runtime dependencies with:

```sh
python3 soundcloud_app.py check
```

## Install for development

```sh
ln -s "$PWD" ~/.config/omarchy/plugins/brunosilveira.soundcloud
omarchy-shell shell rescanPlugins
omarchy plugin enable brunosilveira.soundcloud --before omarchy.tray
```

The widget appears in the right section of the top bar, immediately before the system tray.

## First sign-in

1. Click the SoundCloud icon in the top-right bar.
2. Select `Sign in to SoundCloud`.
3. Sign in inside the one-time SoundCloud window.
4. Close the window after the account page loads.

Closing that window hides it; it does not close the playback backend. A bounded,
owner-only cookie jar is stored locally under:

```text
~/.local/share/omarchy-soundcloud/
```

WebKit itself uses an ephemeral in-memory profile and cache; it is never given a
pathname to reopen for session storage. The plugin does not store the account
password. SoundCloud session cookies remain local in the descriptor-anchored
data directory. Artwork is
downloaded only from SoundCloud's `i1.sndcdn.com` CDN through a bounded HTTPS
fetcher, validated as PNG or JPEG, and passed to the shell as bounded image data.

## Controls

- Click: open or close the bar popup
- Middle-click: play/pause
- Right-click: next track
- Mouse wheel: previous/next
- Popup: artwork, metadata, playback controls, seekable waveform, Home, and Feed

## Backend CLI

```sh
python3 soundcloud_app.py check
python3 soundcloud_app.py launch       # start and show the one-time login window
python3 soundcloud_app.py status
python3 soundcloud_app.py play-pause
python3 soundcloud_app.py previous
python3 soundcloud_app.py next
python3 soundcloud_app.py seek 0.5    # seek to 50%
python3 soundcloud_app.py play soundcloud:tracks:123456
python3 soundcloud_app.py show
python3 soundcloud_app.py stop
```

The bar keeps a direct connection to the backend over a mode-0600 Unix socket in
the owner-only `$XDG_RUNTIME_DIR/omarchy-soundcloud/` directory. The backend
checks each client's Unix peer credentials. Playback status is pushed over that
connection; normal controls and track requests do not launch a new Python process.

## Removing

To retain the local SoundCloud session for a later reinstall, remove the plugin with:

```sh
omarchy plugin remove brunosilveira.soundcloud
```

The backend watches the installed plugin path, shuts down when it disappears,
and removes its runtime socket. The owner-only SoundCloud cookie store is
intentionally retained so reinstalling does not require another sign-in.

To remove persistent state too, do this in the shown order while the plugin is
still installed:

```sh
python3 soundcloud_app.py stop
rm -r -- ~/.local/share/omarchy-soundcloud ~/.cache/omarchy-soundcloud
omarchy plugin remove brunosilveira.soundcloud
```

## Development

```sh
python3 -m unittest tests/test_soundcloud_app.py -v
node tests/test_soundcloud_model.js
omarchy plugin validate .
```

## Limitations

SoundCloud requires Artist Pro to issue official API credentials, so this plugin uses
the authenticated frontend's private JSON APIs through an isolated WebKit broker.
Home and Feed are parsed separately, and selection resolves a stable track URN to a
short-lived media URL for the local GStreamer player; selection never depends on a
rendered SoundCloud card. These private API schemas can change without notice.
Tracks blocked from off-platform or regional playback remain subject to SoundCloud's
normal restrictions.
