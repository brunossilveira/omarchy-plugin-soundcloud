#!/usr/bin/env python3
"""Hidden SoundCloud session/playback backend for an Omarchy bar plugin.

The only normal window is the one-time sign-in surface. Closing it hides the
WebKit view while preserving the session and audio. The bar talks to this
process over a user-only Unix socket.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

APP_ID = "com.github.brunosilveira.OmarchySoundCloud"
APP_NAME = "SoundCloud"
START_URI = "https://soundcloud.com/you/likes"


@dataclass(frozen=True)
class ProfilePaths:
    data_dir: Path
    cache_dir: Path
    cookie_db: Path


def profile_paths(env: Mapping[str, str] | None = None) -> ProfilePaths:
    values = os.environ if env is None else env
    home = Path(values.get("HOME", str(Path.home())))
    data_home = Path(values.get("XDG_DATA_HOME", home / ".local" / "share"))
    cache_home = Path(values.get("XDG_CACHE_HOME", home / ".cache"))
    data_dir = data_home / "omarchy-soundcloud"
    return ProfilePaths(
        data_dir=data_dir,
        cache_dir=cache_home / "omarchy-soundcloud",
        cookie_db=data_dir / "cookies.sqlite",
    )


def runtime_socket_path(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    runtime_dir = values.get("XDG_RUNTIME_DIR", f"/tmp/runtime-{os.getuid()}")
    return Path(runtime_dir) / "omarchy-soundcloud.sock"


def is_soundcloud_uri(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
    except (TypeError, ValueError):
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return host == "soundcloud.com" or host.endswith(".soundcloud.com")


def apply_graphics_workarounds(env: dict[str, str] | None = None) -> bool:
    """Avoid WebKitGTK's invalid explicit-sync commits on Hyprland."""
    values = os.environ if env is None else env
    if "WEBKIT_DISABLE_DMABUF_RENDERER" in values:
        return False
    if values.get("HYPRLAND_INSTANCE_SIGNATURE"):
        values["WEBKIT_DISABLE_DMABUF_RENDERER"] = "1"
        return True
    return False


def source_uri(source: str) -> str | None:
    return {
        "likes": "https://soundcloud.com/you/likes",
        "feed": "https://soundcloud.com/stream",
    }.get(source)


def status_script() -> str:
    return r"""(() => {
      const audio = document.querySelector('audio');
      const text = (...selectors) => {
        for (const selector of selectors) {
          const node = document.querySelector(selector);
          if (node && node.textContent.trim()) return node.textContent.trim();
        }
        return '';
      };
      const art = document.querySelector('.playbackSoundBadge__avatar .sc-artwork, .playbackSoundBadge__avatar img');
      let artUrl = art && art.src ? art.src : '';
      if (!artUrl && art) {
        const match = (art.style.backgroundImage || '').match(/url\(["']?(.*?)["']?\)/);
        if (match) artUrl = match[1];
      }
      return {
        playing: !!audio && !audio.paused,
        title: text('.playbackSoundBadge__titleLink span[aria-hidden="true"]', '.playbackSoundBadge__titleLink'),
        artist: text('.playbackSoundBadge__lightLink', '.playbackSoundBadge__titleContextContainer a'),
        artUrl,
        loggedIn: !/^\/(signin|register)/.test(location.pathname) &&
          !!document.querySelector('.header__userNavButton, a[href*="/you/library"]'),
        url: location.href,
        duration: audio && isFinite(audio.duration) ? audio.duration : 0,
        position: audio ? audio.currentTime : 0
      };
    })()"""


def command_script(action: str) -> str | None:
    scripts = {
        "play-pause": r"""(() => {
          const audio = document.querySelector('audio');
          if (!audio) return false;
          if (audio.paused) { audio.play(); } else { audio.pause(); }
          return true;
        })()""",
        "next": r"""(() => {
          const button = document.querySelector('.skipControl__next, button[title="Next"], button[aria-label="Next"]');
          if (!button) return false; button.click(); return true;
        })()""",
        "previous": r"""(() => {
          const button = document.querySelector('.skipControl__previous, button[title="Previous"], button[aria-label="Previous"]');
          if (!button) return false; button.click(); return true;
        })()""",
    }
    return scripts.get(action)


def autoplay_script() -> str:
    return r"""(() => {
      const button = document.querySelector(
        '.soundActions__play, .playButton, button[title="Play"], button[aria-label="Play"]'
      );
      if (!button) return false;
      button.click();
      return true;
    })()"""


def dependency_check() -> dict[str, object]:
    result: dict[str, object] = {
        "python_gobject": False,
        "gtk": "3.0",
        "webkit2": "4.1",
        "ok": False,
    }
    try:
        import gi

        result["python_gobject"] = True
        gi.require_version("Gtk", "3.0")
        gi.require_version("WebKit2", "4.1")
        from gi.repository import Gtk, WebKit2  # noqa: F401

        result["ok"] = True
    except (ImportError, ValueError) as exc:
        result["error"] = str(exc)
    return result


def _load_gi():
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gio, GLib, Gtk, WebKit2

    return Gio, GLib, Gtk, WebKit2


def send_response(connection: socket.socket, payload: object) -> None:
    try:
        connection.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
    except OSError:
        pass
    finally:
        connection.close()


class ControlServer(threading.Thread):
    def __init__(self, socket_path: Path, dispatcher):
        super().__init__(name="soundcloud-control", daemon=True)
        self.socket_path = socket_path
        self.dispatcher = dispatcher
        self.server: socket.socket | None = None

    def run(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.socket_path.unlink(missing_ok=True)
            self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            self.server.listen(8)
            while True:
                connection, _ = self.server.accept()
                try:
                    raw = connection.recv(16384)
                    request = json.loads(raw.decode() or "{}")
                    command = str(request.get("command", ""))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    send_response(connection, {"ok": False, "error": "invalid request"})
                    continue
                self.dispatcher(command, connection)
        except OSError:
            return
        finally:
            self.socket_path.unlink(missing_ok=True)

    def close(self) -> None:
        if self.server:
            try:
                self.server.close()
            except OSError:
                pass
        self.socket_path.unlink(missing_ok=True)


def build_application(show_on_start: bool = False):
    Gio, GLib, Gtk, WebKit2 = _load_gi()
    paths = profile_paths()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.cache_dir.mkdir(parents=True, exist_ok=True)

    GLib.set_application_name(APP_NAME)
    GLib.set_prgname("omarchy-soundcloud")

    class SoundCloudApplication(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
            self.window = None
            self.webview = None
            self.pending_autoplay = False
            self.control_server = ControlServer(runtime_socket_path(), self._dispatch_from_thread)

        def do_activate(self):
            self._create_window()
            self.hold()
            self.control_server.start()
            if show_on_start:
                self.show_login()
            else:
                self.window.show_all()
                GLib.idle_add(self.window.hide)

        def do_shutdown(self):
            self.control_server.close()
            Gtk.Application.do_shutdown(self)

        def _create_window(self):
            manager = WebKit2.WebsiteDataManager(
                base_data_directory=str(paths.data_dir),
                base_cache_directory=str(paths.cache_dir),
            )
            context = WebKit2.WebContext.new_with_website_data_manager(manager)
            cookies = context.get_cookie_manager()
            cookies.set_persistent_storage(
                str(paths.cookie_db), WebKit2.CookiePersistentStorage.SQLITE
            )
            cookies.set_accept_policy(WebKit2.CookieAcceptPolicy.ALWAYS)

            self.window = Gtk.ApplicationWindow(application=self)
            self.window.set_title("SoundCloud sign in — close when finished")
            self.window.set_default_size(1080, 760)
            self.window.set_icon_name("audio-x-generic")
            self.window.connect("delete-event", self._hide_window)

            self.webview = WebKit2.WebView.new_with_context(context)
            settings = self.webview.get_settings()
            for name, value in (
                ("enable-webaudio", True),
                ("enable-mediasource", True),
                ("enable-media-stream", True),
                ("enable-media", True),
                ("media-playback-requires-user-gesture", False),
            ):
                settings.set_property(name, value)
            self.webview.connect("load-changed", self._load_changed)
            self.webview.connect("create", self._create_popup)
            self.window.add(self.webview)
            self.webview.load_uri(START_URI)

        def _hide_window(self, *_args):
            self.window.hide()
            return True

        def show_login(self):
            self.window.show_all()
            self.window.present()

        def _create_popup(self, source, _navigation_action):
            popup = WebKit2.WebView.new_with_related_view(source)
            popup_window = Gtk.Window(application=self)
            popup_window.set_default_size(720, 720)
            popup_window.set_title("SoundCloud sign in")
            popup_window.add(popup)
            popup.connect("close", lambda *_: popup_window.destroy())
            popup_window.show_all()
            return popup

        def _load_changed(self, _webview, event):
            if self.pending_autoplay and event == WebKit2.LoadEvent.FINISHED:
                self.pending_autoplay = False
                GLib.timeout_add(900, self._autoplay)

        def _autoplay(self):
            self._evaluate(autoplay_script(), lambda _payload: None)
            return False

        def _dispatch_from_thread(self, command, connection):
            GLib.idle_add(self._handle_command, command, connection)

        def _handle_command(self, command, connection):
            if command == "status":
                self._evaluate(
                    status_script(),
                    lambda payload: send_response(
                        connection,
                        {"ok": True, "running": True, **(payload if isinstance(payload, dict) else {})},
                    ),
                )
            elif command in {"play-pause", "next", "previous"}:
                self._evaluate(
                    command_script(command),
                    lambda payload: send_response(
                        connection, {"ok": bool(payload), "running": True}
                    ),
                )
            elif source_uri(command):
                self.pending_autoplay = True
                self.webview.load_uri(source_uri(command))
                send_response(connection, {"ok": True, "running": True})
            elif command == "show":
                self.show_login()
                send_response(connection, {"ok": True, "running": True})
            elif command == "stop":
                send_response(connection, {"ok": True, "running": False})
                self.quit()
            else:
                send_response(connection, {"ok": False, "error": "unknown command"})
            return False

        def _evaluate(self, script, callback):
            def finished(webview, result, _data):
                try:
                    js_result = webview.run_javascript_finish(result)
                    value = js_result.get_js_value()
                    payload = json.loads(value.to_json(0))
                except Exception as exc:
                    payload = {"error": str(exc)}
                callback(payload)

            self.webview.run_javascript(script, None, finished, None)

    return SoundCloudApplication()


def request_backend(command: str, timeout: float = 4.0) -> dict[str, object] | None:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(runtime_socket_path()))
        client.sendall(json.dumps({"command": command}).encode())
        chunks = []
        while True:
            chunk = client.recv(16384)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        return json.loads(b"".join(chunks).decode())
    except (OSError, json.JSONDecodeError):
        return None
    finally:
        client.close()


def launch_backend(show: bool) -> dict[str, object]:
    existing = request_backend("show" if show else "status", timeout=0.8)
    if existing is not None:
        return existing
    command = [sys.executable, str(Path(__file__).resolve()), "daemon"]
    if show:
        command.append("--show")
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True, "starting": True, "running": True}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SoundCloud backend for the Omarchy bar")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("check")
    daemon = subparsers.add_parser("daemon")
    daemon.add_argument("--show", action="store_true")
    subparsers.add_parser("launch")
    subparsers.add_parser("ensure")
    for action in ("status", "play-pause", "next", "previous", "likes", "feed", "show", "stop"):
        subparsers.add_parser(action)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    command = args.command or "status"
    if command == "check":
        result = dependency_check()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ok"] else 1
    if command in {"launch", "ensure"}:
        print(json.dumps(launch_backend(show=command == "launch"), sort_keys=True))
        return 0
    if command != "daemon":
        response = request_backend(command)
        if response is None:
            response = {"ok": False, "running": False, "loggedIn": False}
        print(json.dumps(response, sort_keys=True))
        return 0 if response.get("ok") or command == "status" else 1

    apply_graphics_workarounds()
    check = dependency_check()
    if not check["ok"]:
        print(check.get("error", "GTK/WebKit dependencies are missing"), file=sys.stderr)
        return 1
    app = build_application(show_on_start=bool(args.show))
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
