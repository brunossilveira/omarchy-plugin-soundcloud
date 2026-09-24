# Omarchy SoundCloud

A dedicated SoundCloud widget for the Omarchy top bar. It keeps the normal UI in the bar rather than in a browser tab.

## Features

- Current track, artist, and artwork
- Play/pause, previous, and next controls
- Start playback from Likes or the following feed
- Persistent SoundCloud login session
- Hidden WebKitGTK playback backend after sign-in
- No Chromium tab and no SoundCloud API credentials

## Requirements

- Omarchy 4 with `omarchy-shell`
- Python 3 with PyGObject
- GTK 3 and WebKitGTK 4.1

These dependencies are present in a standard current Omarchy installation.

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

Closing that window hides it; it does not close the playback backend. Cookies are stored locally under:

```text
~/.local/share/omarchy-soundcloud/
```

No password is stored by the plugin.

## Controls

- Click: open or close the bar popup
- Middle-click: play/pause
- Right-click: next track
- Mouse wheel: previous/next
- Popup: artwork, metadata, playback controls, Likes, and Following

## Backend CLI

```sh
python3 soundcloud_app.py check
python3 soundcloud_app.py launch       # start and show the one-time login window
python3 soundcloud_app.py status
python3 soundcloud_app.py play-pause
python3 soundcloud_app.py previous
python3 soundcloud_app.py next
python3 soundcloud_app.py likes
python3 soundcloud_app.py feed
python3 soundcloud_app.py show
python3 soundcloud_app.py stop
```

The bar communicates with the backend over a mode-0600 Unix socket in `$XDG_RUNTIME_DIR`.

## Development

```sh
python3 -m unittest tests/test_soundcloud_app.py -v
node tests/test_soundcloud_model.js
omarchy plugin validate .
```

## Limitations

This plugin uses SoundCloud's web player because SoundCloud requires Artist Pro to issue official API credentials. SoundCloud can change its page structure without notice; if its playback controls or metadata markup changes, the DOM selectors in `soundcloud_app.py` may need updating. Tracks blocked from off-platform or regional playback remain subject to SoundCloud's normal restrictions.
