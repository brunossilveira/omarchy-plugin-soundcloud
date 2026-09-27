import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "soundcloud_app.py"


def load_module():
    spec = importlib.util.spec_from_file_location("soundcloud_app", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ProfilePathsTest(unittest.TestCase):
    def test_uses_xdg_locations_when_present(self):
        app = load_module()
        env = {
            "HOME": "/home/tester",
            "XDG_DATA_HOME": "/custom/data",
            "XDG_CACHE_HOME": "/custom/cache",
        }

        paths = app.profile_paths(env)

        self.assertEqual(paths.data_dir, Path("/custom/data/omarchy-soundcloud"))
        self.assertEqual(paths.cache_dir, Path("/custom/cache/omarchy-soundcloud"))
        self.assertEqual(paths.cookie_db, paths.data_dir / "cookies.sqlite")

    def test_falls_back_to_home_xdg_defaults(self):
        app = load_module()

        paths = app.profile_paths({"HOME": "/home/tester"})

        self.assertEqual(paths.data_dir, Path("/home/tester/.local/share/omarchy-soundcloud"))
        self.assertEqual(paths.cache_dir, Path("/home/tester/.cache/omarchy-soundcloud"))

    def test_private_directory_rejects_symlink_and_uses_owner_only_mode(self):
        app = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)

            with self.assertRaises(PermissionError):
                app.ensure_private_directory(link / "child")

            private = root / "private"
            app.ensure_private_directory(private)
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)

    def test_runtime_profile_ignores_environment_path_overrides(self):
        app = load_module()
        trusted_home = Path(app.pwd.getpwuid(os.geteuid()).pw_dir)
        with mock.patch.dict(os.environ, {
            "HOME": "/tmp/untrusted-home",
            "XDG_DATA_HOME": "/tmp/untrusted-data",
            "XDG_CACHE_HOME": "/tmp/untrusted-cache",
        }):
            paths = app.profile_paths()

        self.assertEqual(paths.data_dir, trusted_home / ".local/share/omarchy-soundcloud")
        self.assertEqual(paths.cache_dir, trusted_home / ".cache/omarchy-soundcloud")

    def test_cookie_store_is_descriptor_anchored_and_owner_only(self):
        app = load_module()
        with tempfile.TemporaryDirectory() as directory:
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                store = app.CookieStore(directory_fd)
                record = {
                    "name": "session",
                    "value": "test-value",
                    "domain": ".soundcloud.com",
                    "path": "/",
                    "expires": None,
                    "secure": True,
                    "httpOnly": True,
                }
                store.save([record])
                self.assertEqual(store.load(), [record])
                cookie_path = Path(directory) / "cookies.json"
                self.assertEqual(stat.S_IMODE(cookie_path.stat().st_mode), 0o600)
                cookie_path.unlink()
                target = Path(directory) / "target"
                target.write_text("[]")
                cookie_path.symlink_to(target)
                with self.assertRaises(OSError):
                    store.load()
            finally:
                os.close(directory_fd)

    def test_track_cache_survives_backend_restart_in_owner_only_store(self):
        app = load_module()
        payload = {
            "home": [{
                "title": "Cached track",
                "artist": "Artist",
                "url": "https://soundcloud.com/artist/cached-track",
                "artUrl": "https://i1.sndcdn.com/artworks-test-large.jpg",
                "playCount": 12,
                "durationMs": 3456,
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                store = app.PrivateJsonStore(directory_fd, "tracks.json", app.MAX_TRACK_CACHE_BYTES)
                store.save(payload)
                self.assertEqual(store.load(), payload)
                self.assertEqual(
                    stat.S_IMODE((Path(directory) / "tracks.json").stat().st_mode),
                    0o600,
                )
            finally:
                os.close(directory_fd)

    def test_track_cache_is_sanitized_but_keeps_artwork_for_refresh(self):
        app = load_module()
        cache = app.sanitize_track_cache({
            "home": [{
                "playbackId": "soundcloud:tracks:123",
                "title": "Track",
                "artist": "Artist",
                "url": "https://soundcloud.com/artist/track",
                "artUrl": "https://i1.sndcdn.com/artworks-test-large.jpg",
                "playCount": 12,
                "durationMs": 3456,
                "privatePayload": {"secret": True},
            }],
            "evil": [{"title": "ignored"}],
        })

        self.assertEqual(set(cache), {"home"})
        self.assertEqual(set(cache["home"][0]), {
            "playbackId", "title", "artist", "url", "artUrl", "playCount", "durationMs",
        })
        self.assertEqual(
            cache["home"][0]["artUrl"],
            "https://i1.sndcdn.com/artworks-test-large.jpg",
        )


class UriPolicyTest(unittest.TestCase):
    def test_soundcloud_domains_stay_in_the_app(self):
        app = load_module()

        self.assertTrue(app.is_soundcloud_uri("https://soundcloud.com/you/likes"))
        self.assertTrue(app.is_soundcloud_uri("https://secure.soundcloud.com/sign-in"))
        self.assertTrue(app.is_soundcloud_uri("https://m.soundcloud.com/discover"))

    def test_lookalike_domains_are_rejected(self):
        app = load_module()

        self.assertFalse(app.is_soundcloud_uri("https://soundcloud.com.example.org/login"))
        self.assertFalse(app.is_soundcloud_uri("javascript:alert(1)"))
        self.assertFalse(app.is_soundcloud_uri("not a url"))
        self.assertFalse(app.is_soundcloud_uri("http://soundcloud.com/you/likes"))
        self.assertFalse(app.is_soundcloud_uri("https://user:pass@soundcloud.com/you/likes"))
        self.assertFalse(app.is_soundcloud_uri("https://soundcloud.com:8443/you/likes"))


class GraphicsWorkaroundTest(unittest.TestCase):
    def test_disables_webkit_dmabuf_on_hyprland(self):
        app = load_module()
        env = {"HYPRLAND_INSTANCE_SIGNATURE": "instance", "XDG_SESSION_TYPE": "wayland"}

        changed = app.apply_graphics_workarounds(env)

        self.assertTrue(changed)
        self.assertEqual(env["WEBKIT_DISABLE_DMABUF_RENDERER"], "1")

    def test_preserves_an_explicit_renderer_setting(self):
        app = load_module()
        env = {
            "HYPRLAND_INSTANCE_SIGNATURE": "instance",
            "WEBKIT_DISABLE_DMABUF_RENDERER": "0",
        }

        changed = app.apply_graphics_workarounds(env)

        self.assertFalse(changed)
        self.assertEqual(env["WEBKIT_DISABLE_DMABUF_RENDERER"], "0")


class BackendCommandTest(unittest.TestCase):
    def test_pagination_event_log_is_dedicated_sanitized_and_rotated(self):
        app = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pagination.log"
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            setattr(app, "_event_log_fd", fd)
            setattr(app, "MAX_EVENT_LOG_BYTES", 320)
            try:
                app.log_event("untrusted-event", secret="must-not-appear")
                for count in range(12):
                    app.log_event(
                        "track-page",
                        source="feed",
                        addedCount=count,
                        hasMore=True,
                        secret="must-not-appear",
                    )
            finally:
                app.close_runtime_event_log()

            contents = path.read_text()
            self.assertLessEqual(path.stat().st_size, app.MAX_EVENT_LOG_BYTES)
            self.assertNotIn("must-not-appear", contents)
            for line in contents.splitlines():
                record = json.loads(line)
                self.assertEqual(record["event"], "track-page")
                self.assertEqual(record["source"], "feed")

        source = MODULE_PATH.read_text()
        self.assertIn("stderr=subprocess.DEVNULL", source)
        self.assertIn("pass_fds=(event_log_fd,)", source)

    def test_runtime_socket_uses_xdg_runtime_dir(self):
        app = load_module()

        path = app.runtime_socket_path({"XDG_RUNTIME_DIR": "/run/user/123"})

        self.assertEqual(path, Path("/run/user/123/omarchy-soundcloud/control.sock"))

    def test_runtime_socket_fails_closed_without_xdg_runtime_dir(self):
        app = load_module()

        with self.assertRaises(RuntimeError):
            app.runtime_socket_path({})

    def test_control_socket_handles_multiple_correlated_requests_per_connection(self):
        app = load_module()
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "control.sock"

            def dispatch(command, connection, request_id):
                app.send_response(
                    connection,
                    {"ok": True, "command": command},
                    request_id=request_id,
                )

            server = app.ControlServer(socket_path, dispatch)
            server.start()
            try:
                for _ in range(100):
                    if socket_path.exists():
                        break
                    time.sleep(0.01)
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(1)
                client.connect(str(socket_path))
                stream = client.makefile("rwb", buffering=0)
                stream.write(b'{"id":1,"command":"status"}\n')
                stream.write(b'{"id":2,"command":"tracks"}\n')

                first = json.loads(stream.readline())
                second = json.loads(stream.readline())

                self.assertEqual(first, {"type": "response", "id": 1, "ok": True, "command": "status"})
                self.assertEqual(second, {"type": "response", "id": 2, "ok": True, "command": "tracks"})
                client.close()
            finally:
                server.close()
                server.join(timeout=1)

    def test_control_socket_rejects_oversized_frames(self):
        app = load_module()
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "control.sock"
            server = app.ControlServer(socket_path, lambda *_args: None)
            server.start()
            try:
                for _ in range(100):
                    if socket_path.exists():
                        break
                    time.sleep(0.01)
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(1)
                client.connect(str(socket_path))
                client.sendall(b"x" * (app.MAX_REQUEST_BYTES + 1))
                self.assertEqual(client.recv(1), b"")
                client.close()
            finally:
                server.close()
                server.join(timeout=1)

    def test_status_subscribers_receive_events_and_failed_connections_are_removed(self):
        app = load_module()

        class FakeConnection:
            def __init__(self, succeeds):
                self.succeeds = succeeds
                self.messages = []

            def send(self, payload):
                self.messages.append(payload)
                return self.succeeds

        subscribers = app.StatusSubscribers()
        active = FakeConnection(True)
        failed = FakeConnection(False)
        subscribers.add(active)
        subscribers.add(failed)

        subscribers.publish({"playing": True, "title": "Track"})
        subscribers.publish({"playing": False, "title": "Track"})

        self.assertEqual(active.messages[0]["type"], "status")
        self.assertEqual(active.messages[0]["state"]["title"], "Track")
        self.assertEqual(len(active.messages), 2)
        self.assertEqual(len(failed.messages), 1)

    def test_subscribers_can_receive_completed_track_events(self):
        app = load_module()

        class FakeConnection:
            def __init__(self):
                self.messages = []

            def send(self, payload):
                self.messages.append(payload)
                return True

        subscribers = app.StatusSubscribers()
        connection = FakeConnection()
        subscribers.add(connection)
        subscribers.publish_message({"type": "tracks", "source": "feed", "tracks": []})

        self.assertEqual(
            connection.messages,
            [{"type": "tracks", "source": "feed", "tracks": []}],
        )

    def test_only_known_player_actions_have_scripts(self):
        app = load_module()

        self.assertIn("playControls__play", app.command_script("play-pause"))
        self.assertIn("skipControl__next", app.command_script("next"))
        self.assertIn("skipControl__previous", app.command_script("previous"))
        self.assertIsNone(app.command_script("arbitrary-javascript"))

    def test_socket_launch_command_is_backward_compatible_with_show(self):
        app = load_module()

        self.assertEqual(app.normalize_backend_command("launch"), "show")
        self.assertEqual(app.normalize_backend_command("play-pause"), "play-pause")

    def test_seek_script_clamps_ratio_and_targets_soundcloud_timeline(self):
        app = load_module()

        script = app.seek_script(1.5)

        self.assertIn("const ratio = 1.0", script)
        self.assertIn("playbackTimeline__progressWrapper", script)
        self.assertIn("currentTime", script)

    def test_status_script_returns_required_fields(self):
        app = load_module()

        script = app.status_script()

        for field in ("playerPresent", "playing", "title", "artist", "artUrl", "loggedIn", "url"):
            self.assertIn(field, script)
        self.assertIn("playControls__play", script)
        self.assertIn("Pause", script)
        self.assertIn("playbackTimeline__timePassed", script)
        self.assertIn("playbackTimeline__duration", script)
        self.assertIn("getComputedStyle(art).backgroundImage", script)
        self.assertIn(".playbackSoundBadge__avatar [style*=\"background-image\"]", script)
        self.assertIn("signin|register", script)

    def test_status_script_clears_stale_badge_without_an_audio_player(self):
        app = load_module()
        script = app.status_script()

        self.assertIn("const playerPresent = !!audio", script)
        self.assertIn("playing: playerPresent ? !audio.paused : false", script)
        self.assertIn("artUrl: playerPresent ? artUrl : ''", script)
        validated = app.validate_status_payload({
            "playerPresent": False,
            "playing": False,
            "title": "",
            "artist": "",
            "artUrl": "",
            "loggedIn": True,
            "url": "https://soundcloud.com/feed",
            "duration": 0,
            "position": 0,
        })
        self.assertIsNotNone(validated)
        self.assertFalse(validated["playerPresent"])

    def test_status_validation_preserves_only_explicit_playback_states(self):
        app = load_module()
        base = {
            "playerPresent": False,
            "playing": False,
            "title": "",
            "artist": "",
            "artUrl": "",
            "loggedIn": True,
            "url": "https://soundcloud.com/feed",
            "duration": 0,
            "position": 0,
        }

        for state in ("idle", "resolving", "buffering", "playing", "paused", "error"):
            validated = app.validate_status_payload({**base, "playbackState": state})
            self.assertEqual(validated["playbackState"], state)
        self.assertIsNone(app.validate_status_payload({**base, "playbackState": "unknown"}))

    def test_native_player_begins_in_resolving_state_before_media_exists(self):
        app = load_module()
        player = object.__new__(app.GstPlayback)
        player.active = True
        player.playing = True
        player.metadata = {"title": "Old"}
        player.error = "old error"
        player.state = "playing"
        player.generation = 7
        player.on_change = lambda: None
        player._stop_current = lambda: setattr(player, "generation", player.generation + 1)

        generation = player.begin({"title": "Selected", "playbackId": "soundcloud:tracks:9"})
        status = player.status()

        self.assertEqual(generation, 8)
        self.assertEqual(status["playbackState"], "resolving")
        self.assertFalse(status["playerPresent"])
        self.assertFalse(status["playing"])
        self.assertEqual(player.metadata["title"], "Selected")

    def test_private_api_playback_resolution_stays_inside_webkit(self):
        app = load_module()
        script = app.api_request_capture_script()

        self.assertIn("const playbackTracks = new Map()", script)
        self.assertIn("window.__omarchyPlayApiTrack", script)
        self.assertIn("headers: new Headers(context.headers)", script)
        self.assertIn("messageHandlers.omarchyPlayback.postMessage", script)
        self.assertIn("const generation = ++playbackGeneration", script)
        self.assertIn("if (generation !== playbackGeneration) return", script)
        self.assertIn("if (pageAudio && !pageAudio.paused) pageAudio.pause()", script)
        self.assertNotIn("window.playbackTracks", script)

    def test_stream_urls_are_strictly_limited_to_soundcloud_media_hosts(self):
        app = load_module()

        self.assertEqual(
            app.validate_stream_uri("https://cf-hls-media.sndcdn.com/media/test.m3u8?token=private"),
            "https://cf-hls-media.sndcdn.com/media/test.m3u8?token=private",
        )
        self.assertEqual(
            app.validate_stream_uri("https://playback.media-streaming.soundcloud.cloud/test"),
            "https://playback.media-streaming.soundcloud.cloud/test",
        )
        for value in (
            "http://cf-hls-media.sndcdn.com/test",
            "https://sndcdn.com.evil.example/test",
            "https://user:pass@cf-hls-media.sndcdn.com/test",
            "https://example.com/test",
        ):
            self.assertIsNone(app.validate_stream_uri(value))

    def test_source_routes_are_fixed_soundcloud_pages(self):
        app = load_module()

        self.assertEqual(app.source_uri("home"), "https://soundcloud.com/discover")
        self.assertEqual(app.source_uri("likes"), "https://soundcloud.com/you/likes")
        self.assertEqual(app.source_uri("feed"), "https://soundcloud.com/feed")
        self.assertIsNone(app.source_uri("https://evil.example"))


    def test_api_tracks_script_reads_only_intercepted_route_state(self):
        app = load_module()

        feed_script = app.api_tracks_script("feed")
        home_script = app.api_tracks_script("home")

        self.assertIn('const source = "feed"', feed_script)
        self.assertNotIn("document.querySelector", feed_script)
        self.assertNotIn("fetch(", feed_script)
        self.assertIn('const source = "home"', home_script)
        self.assertIn("window.__omarchyApiTrackLists", home_script)
        self.assertIn("state.completed", feed_script)
        self.assertIn("state.error", feed_script)

    def test_track_command_allows_only_route_bound_api_sources(self):
        app = load_module()

        self.assertEqual(app.track_source_from_command("tracks:home"), "home")
        self.assertEqual(app.track_source_from_command("tracks:feed"), "feed")
        self.assertIsNone(app.track_source_from_command("tracks"))
        self.assertIsNone(app.track_source_from_command("tracks:https://evil.example"))

    def test_load_more_command_allows_only_route_bound_sources(self):
        app = load_module()

        self.assertEqual(app.load_more_source_from_command("load-more:home"), "home")
        self.assertEqual(app.load_more_source_from_command("load-more:feed"), "feed")
        self.assertIsNone(app.load_more_source_from_command("load-more"))
        self.assertIsNone(app.load_more_source_from_command("load-more:likes"))

    def test_play_command_accepts_only_stable_soundcloud_track_ids(self):
        app = load_module()

        self.assertEqual(
            app.playback_id_from_command("play:soundcloud:tracks:123"),
            "soundcloud:tracks:123",
        )
        self.assertIsNone(app.playback_id_from_command("play:soundcloud:tracks:0"))
        self.assertIsNone(app.playback_id_from_command("play-url:https://soundcloud.com/a/b"))
        self.assertIsNone(app.playback_id_from_command("play:soundcloud:users:123"))

    def test_track_pages_append_unique_tracks_and_new_navigation_resets(self):
        app = load_module()
        first = [
            {"url": "https://soundcloud.com/artist/one", "title": "One"},
            {"url": "https://soundcloud.com/artist/two", "title": "Two"},
        ]
        second = [
            {"url": "https://soundcloud.com/artist/two", "title": "Two updated"},
            {"url": "https://soundcloud.com/artist/three", "title": "Three"},
        ]

        self.assertEqual(
            [track["title"] for track in app.merge_track_pages(first, second, reset=False, limit=10)],
            ["One", "Two updated", "Three"],
        )
        self.assertEqual(
            app.merge_track_pages(first, second, reset=True, limit=10),
            second,
        )

    def test_load_more_script_scrolls_only_the_selected_route(self):
        app = load_module()

        home_script = app.load_more_script("home")
        feed_script = app.load_more_script("feed")

        self.assertIn('__omarchyLoadMore("home")', home_script)
        self.assertIn('__omarchyLoadMore("feed")', feed_script)
        self.assertNotIn("window.scrollTo", home_script)
        self.assertIsNone(app.load_more_script("likes"))

    def test_load_more_fetches_the_validated_next_api_page_without_dom_scrolling(self):
        app = load_module()
        capture_script = app.api_request_capture_script()
        harness = f"""
const captureScript = {json.dumps(capture_script)};
const posted = [];
const calls = [];
const pages = [
  {{ collection: [{{ permalink_url: 'https://soundcloud.com/a/one', title: 'One', duration: 1000, user: {{ username: 'A' }} }}], next_href: 'https://api-v2.soundcloud.com/stream?offset=10' }},
  {{ collection: [{{ permalink_url: 'https://soundcloud.com/b/two', title: 'Two', duration: 1000, user: {{ username: 'B' }} }}], next_href: null }}
];
global.location = {{ href: 'https://soundcloud.com/feed', hostname: 'soundcloud.com', pathname: '/feed' }};
global.window = global;
window.webkit = {{ messageHandlers: {{ omarchyTracks: {{ postMessage(value) {{ posted.push(JSON.parse(value)); }} }} }} }};
window.fetch = async function(input, init) {{
  calls.push(typeof input === 'string' ? input : input.url);
  const body = JSON.stringify(pages.shift());
  return new Response(body, {{ status: 200 }});
}};
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}};
XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}};
XMLHttpRequest.prototype.addEventListener = function() {{}};
eval(captureScript);
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/stream?offset=0', {{
    headers: {{ authorization: 'OAuth private-test-value' }}
  }}));
  await new Promise(setImmediate);
  const started = window.__omarchyLoadMore('feed');
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  const exhausted = window.__omarchyLoadMore('feed');
  process.stdout.write(JSON.stringify({{ started, exhausted, calls, posted, globals: Object.keys(window) }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertTrue(result["started"])
        self.assertEqual(result["calls"], [
            "https://api-v2.soundcloud.com/stream?offset=0",
            "https://api-v2.soundcloud.com/stream?offset=10",
        ])
        self.assertEqual([message["tracks"][0]["title"] for message in result["posted"]], ["One", "Two"])
        self.assertFalse(result["posted"][1]["hasMore"])
        self.assertEqual(result["exhausted"], {"started": False, "hasMore": False})
        self.assertNotIn("private-test-value", completed.stdout)

    def test_failed_load_more_emits_a_retryable_completion_event(self):
        app = load_module()
        capture_script = app.api_request_capture_script()
        harness = f"""
const posted = [];
let callCount = 0;
global.location = {{ href: 'https://soundcloud.com/feed' }};
global.window = global;
window.webkit = {{ messageHandlers: {{ omarchyTracks: {{ postMessage(value) {{ posted.push(JSON.parse(value)); }} }} }} }};
window.fetch = async function() {{
  callCount += 1;
  if (callCount > 1) return {{ ok: false, status: 503 }};
  const body = JSON.stringify({{
    collection: [{{ permalink_url: 'https://soundcloud.com/a/one', title: 'One', duration: 1000 }}],
    next_href: 'https://api-v2.soundcloud.com/stream?offset=10'
  }});
  return new Response(body, {{ status: 200 }});
}};
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}};
XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}};
XMLHttpRequest.prototype.addEventListener = function() {{}};
eval({json.dumps(capture_script)});
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/stream?offset=0'));
  await new Promise(setImmediate);
  const started = window.__omarchyLoadMore('feed');
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  const retried = window.__omarchyLoadMore('feed');
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  process.stdout.write(JSON.stringify({{ started, retried, callCount, posted }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(result["started"], {"started": True, "hasMore": True})
        self.assertEqual(result["retried"], {"started": True, "hasMore": True})
        self.assertEqual(result["callCount"], 3)
        self.assertEqual(len(result["posted"]), 3)
        self.assertEqual(result["posted"][1]["tracks"], [])
        self.assertTrue(result["posted"][1]["hasMore"])
        self.assertEqual(result["posted"][1]["addedCount"], 0)
        self.assertEqual(result["posted"][1]["error"], "Could not load more tracks")

    def test_load_more_stops_before_requesting_a_cursor_cycle(self):
        app = load_module()
        capture_script = app.api_request_capture_script()
        harness = f"""
const calls = [];
const pages = [
  {{ collection: [{{ permalink_url: 'https://soundcloud.com/a/one', title: 'One', duration: 1 }}], next_href: 'https://api-v2.soundcloud.com/stream?offset=10' }},
  {{ collection: [{{ permalink_url: 'https://soundcloud.com/a/two', title: 'Two', duration: 1 }}], next_href: 'https://api-v2.soundcloud.com/stream?offset=0' }}
];
global.location = {{ href: 'https://soundcloud.com/feed' }};
global.window = global;
window.webkit = {{ messageHandlers: {{ omarchyTracks: {{ postMessage() {{}} }} }} }};
window.fetch = async function(input) {{
  calls.push(typeof input === 'string' ? input : input.url);
  const body = JSON.stringify(pages.shift());
  return new Response(body, {{ status: 200 }});
}};
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}};
XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}};
XMLHttpRequest.prototype.addEventListener = function() {{}};
eval({json.dumps(capture_script)});
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/stream?offset=0'));
  await new Promise(setImmediate);
  window.__omarchyLoadMore('feed');
  await new Promise(setImmediate); await new Promise(setImmediate);
  const cycle = window.__omarchyLoadMore('feed');
  process.stdout.write(JSON.stringify({{ calls, cycle }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(result["calls"], [
            "https://api-v2.soundcloud.com/stream?offset=0",
            "https://api-v2.soundcloud.com/stream?offset=10",
        ])
        self.assertEqual(result["cycle"], {"started": False, "hasMore": False})

    def test_api_observation_script_exposes_only_bounded_resource_paths(self):
        app = load_module()

        script = app.api_observation_script()

        self.assertIn("api-v2.soundcloud.com", script)
        self.assertIn("candidate.pathname", script)
        self.assertNotIn("candidate.href", script)
        self.assertIn("slice(0, 32)", script)

    def test_api_request_capture_keeps_frontend_credentials_out_of_page_global_state(self):
        app = load_module()

        capture_script = app.api_request_capture_script()
        tracks_script = app.api_tracks_script("feed")

        self.assertIn("window.fetch", capture_script)
        self.assertIn("/stream", capture_script)
        self.assertNotIn("__omarchyApiRequests", capture_script)
        self.assertIn("const pagination = Object.create(null)", capture_script)
        self.assertIn("const privateHeaders = new Headers()", capture_script)
        self.assertIn("url.protocol !== 'https:'", capture_script)
        self.assertIn("url.username || url.password", capture_script)
        self.assertIn("const context = path ? requestContext(input, init) : null", capture_script)
        self.assertIn(
            "JSON.stringify({ source, tracks, reset, hasMore, addedCount })",
            capture_script,
        )
        self.assertNotIn("console.", capture_script)
        self.assertNotIn("fetch(", tracks_script)
        self.assertNotIn("performance.getEntriesByType", tracks_script)

    def test_private_transcoding_endpoints_reject_userinfo_and_nonstandard_ports(self):
        app = load_module()
        capture_script = app.api_request_capture_script()
        harness = f"""
const posted = [];
global.location = {{ href: 'https://soundcloud.com/feed', hostname: 'soundcloud.com', pathname: '/feed' }};
global.window = global;
window.webkit = {{ messageHandlers: {{ omarchyTracks: {{ postMessage(value) {{ posted.push(JSON.parse(value)); }} }} }} }};
const encode = (value) => new TextEncoder().encode(JSON.stringify(value));
const response = (value) => {{
  const bytes = encode(value); let sent = false; let cancelled = false;
  return {{ ok: true, headers: new Headers({{'content-length': String(bytes.length)}}), clone() {{ return response(value); }},
    body: {{ getReader() {{ return {{ async read() {{ if (sent) return {{done:true}}; sent=true; return {{done:false,value:bytes}}; }}, async cancel() {{ cancelled=true; }} }}; }} }} }};
}};
window.fetch = async () => response({{ collection: [
  {{kind:'track',id:1,title:'Userinfo',duration:1,permalink_url:'https://soundcloud.com/a/one',media:{{transcodings:[{{url:'https://user:pass@api-v2.soundcloud.com/media/one',format:{{protocol:'progressive',mime_type:'audio/mpeg'}}}}]}}}},
  {{kind:'track',id:2,title:'Port',duration:1,permalink_url:'https://soundcloud.com/a/two',media:{{transcodings:[{{url:'https://api-v2.soundcloud.com:8443/media/two',format:{{protocol:'progressive',mime_type:'audio/mpeg'}}}}]}}}}
], next_href:null}});
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}}; XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}}; XMLHttpRequest.prototype.addEventListener = function() {{}};
eval({json.dumps(capture_script)});
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/stream', {{headers:{{authorization:'secret'}}}}));
  for (let i=0;i<5;i++) await new Promise(setImmediate);
  process.stdout.write(JSON.stringify(posted));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)
        tracks = json.loads(completed.stdout)[0]["tracks"]
        self.assertEqual([track.get("playbackId", "") for track in tracks], ["", ""])
        self.assertNotIn("secret", completed.stdout)

    def test_broker_response_reader_cancels_an_over_limit_stream(self):
        app = load_module()
        script = app.api_request_capture_script()
        harness = f"""
let cancelled = false; let reads = 0;
global.location = {{href:'https://soundcloud.com/feed',hostname:'soundcloud.com',pathname:'/feed'}};
global.window = global;
window.webkit = {{messageHandlers:{{omarchyTracks:{{postMessage(){{}}}},omarchyPlayback:{{postMessage(){{}}}}}}}};
const chunk = new Uint8Array(1024 * 1024);
const makeResponse = () => ({{ok:true,headers:new Headers(),clone(){{return makeResponse();}},body:{{getReader(){{return {{async read(){{reads++;return {{done:false,value:chunk}};}},async cancel(){{cancelled=true;}}}};}}}}}});
window.fetch = async () => makeResponse();
global.XMLHttpRequest=function(){{}}; XMLHttpRequest.prototype.open=function(){{}};
XMLHttpRequest.prototype.setRequestHeader=function(){{}}; XMLHttpRequest.prototype.send=function(){{}};
XMLHttpRequest.prototype.addEventListener=function(){{}};
eval({json.dumps(script)});
(async()=>{{await window.fetch('https://api-v2.soundcloud.com/stream'); for(let i=0;i<6;i++) await new Promise(setImmediate); process.stdout.write(JSON.stringify({{cancelled,reads}}));}})();
"""
        result = json.loads(subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True, text=True
        ).stdout)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["reads"], 3)

    def test_newer_home_request_wins_and_embedded_tracks_are_combined_with_expanded_ids(self):
        app = load_module()
        script = app.api_request_capture_script()
        harness = f"""
const posted=[]; let finishExpansion;
global.location={{href:'https://soundcloud.com/discover',hostname:'soundcloud.com',pathname:'/discover'}}; global.window=global;
window.webkit={{messageHandlers:{{omarchyTracks:{{postMessage(v){{posted.push(JSON.parse(v));}}}},omarchyPlayback:{{postMessage(){{}}}}}}}};
const resp=(value)=>{{const bytes=new TextEncoder().encode(JSON.stringify(value));return {{ok:true,headers:new Headers(),clone(){{return resp(value);}},body:{{getReader(){{let sent=false;return {{async read(){{if(sent)return {{done:true}};sent=true;return {{done:false,value:bytes}};}},async cancel(){{}}}};}}}}}};}};
const old={{collection:[{{kind:'track',id:1,title:'Embedded',duration:1,permalink_url:'https://soundcloud.com/a/embedded'}},{{items:{{collection:[{{tracks:[{{id:2}}]}}]}}}}],next_href:null}};
const newer={{collection:[{{kind:'track',id:3,title:'Newer',duration:1,permalink_url:'https://soundcloud.com/a/newer'}}],next_href:null}};
window.fetch=async(input)=>{{const url=typeof input==='string'?input:input.url;if(url.includes('/tracks?'))return new Promise(r=>{{finishExpansion=()=>r(resp([{{kind:'track',id:2,title:'Expanded',duration:1,permalink_url:'https://soundcloud.com/a/expanded'}}]));}});if(url.includes('old'))return resp(old);return resp(newer);}};
global.XMLHttpRequest=function(){{}}; XMLHttpRequest.prototype.open=function(){{}}; XMLHttpRequest.prototype.setRequestHeader=function(){{}}; XMLHttpRequest.prototype.send=function(){{}}; XMLHttpRequest.prototype.addEventListener=function(){{}};
eval({json.dumps(script)});
(async()=>{{await window.fetch('https://api-v2.soundcloud.com/mixed-selections?old=1');for(let i=0;i<3;i++)await new Promise(setImmediate);await window.fetch('https://api-v2.soundcloud.com/mixed-selections?new=1');for(let i=0;i<5;i++)await new Promise(setImmediate);finishExpansion();for(let i=0;i<8;i++)await new Promise(setImmediate);process.stdout.write(JSON.stringify(posted));}})();
"""
        posted = json.loads(subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True, text=True
        ).stdout)
        self.assertEqual([[track["title"] for track in event["tracks"]] for event in posted], [["Newer"]])

    def test_superseded_home_expansion_cannot_replace_newer_playback_metadata(self):
        app = load_module()
        script = app.api_request_capture_script()
        harness = f"""
const posted=[]; const playback=[]; const mediaCalls=[]; let finishOldExpansion;
global.location={{href:'https://soundcloud.com/discover',hostname:'soundcloud.com',pathname:'/discover'}};
global.document={{querySelector(){{return null;}}}}; global.window=global;
window.webkit={{messageHandlers:{{omarchyTracks:{{postMessage(v){{posted.push(JSON.parse(v));}}}},omarchyPlayback:{{postMessage(v){{const {{streamUrl,...safe}}=JSON.parse(v);playback.push(safe);}}}}}}}};
const resp=(value)=>new Response(JSON.stringify(value),{{status:200}});
const track=(title,endpoint)=>({{kind:'track',id:7,urn:'soundcloud:tracks:7',title,duration:1,permalink_url:'https://soundcloud.com/a/'+title.toLowerCase(),media:{{transcodings:[{{url:'https://api-v2.soundcloud.com/media/'+endpoint,format:{{protocol:'progressive',mime_type:'audio/mpeg'}}}}]}}}});
const old={{collection:[{{items:{{collection:[{{tracks:[{{id:7}}]}}]}}}}],next_href:null}};
const newer={{collection:[track('New','new')],next_href:null}};
window.fetch=async(input)=>{{
  const url=typeof input==='string'?input:input.url;
  if(url.includes('/tracks?'))return new Promise(resolve=>{{finishOldExpansion=()=>resolve(resp([track('Old','old')]));}});
  if(url.includes('/media/')){{mediaCalls.push(new URL(url).pathname);return resp({{url:'https://cf-media.sndcdn.com/audio.mp3'}});}}
  return resp(url.includes('old=1')?old:newer);
}};
global.XMLHttpRequest=function(){{}}; XMLHttpRequest.prototype.open=function(){{}}; XMLHttpRequest.prototype.setRequestHeader=function(){{}}; XMLHttpRequest.prototype.send=function(){{}}; XMLHttpRequest.prototype.addEventListener=function(){{}};
eval({json.dumps(script)});
(async()=>{{
  await window.fetch('https://api-v2.soundcloud.com/mixed-selections?old=1');
  for(let i=0;i<3;i++)await new Promise(setImmediate);
  await window.fetch('https://api-v2.soundcloud.com/mixed-selections?new=1');
  for(let i=0;i<5;i++)await new Promise(setImmediate);
  finishOldExpansion();
  for(let i=0;i<8;i++)await new Promise(setImmediate);
  window.__omarchyPlayApiTrack('soundcloud:tracks:7',9);
  for(let i=0;i<8;i++)await new Promise(setImmediate);
  process.stdout.write(JSON.stringify({{posted,playback,mediaCalls}}));
}})().catch((error)=>{{console.error(error);process.exit(1);}});
"""
        result = json.loads(subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True, text=True
        ).stdout)

        self.assertEqual([track["title"] for track in result["posted"][0]["tracks"]], ["New"])
        self.assertEqual([message["track"]["title"] for message in result["playback"]], ["New"])
        self.assertEqual(result["mediaCalls"], ["/media/new"])
        self.assertNotIn("Old", json.dumps(result))

    def test_cached_selection_waits_for_request_context(self):
        app = load_module()
        script = app.api_request_capture_script()
        harness = f"""
const playback=[]; const calls=[];
global.location={{href:'https://soundcloud.com/feed',hostname:'soundcloud.com',pathname:'/feed'}};global.document={{querySelector(){{return null;}}}};global.window=global;
window.webkit={{messageHandlers:{{omarchyTracks:{{postMessage(){{}}}},omarchyPlayback:{{postMessage(v){{playback.push(JSON.parse(v));}}}}}}}};
const resp=(v)=>{{const b=new TextEncoder().encode(JSON.stringify(v));return {{ok:true,headers:new Headers(),clone(){{return resp(v);}},body:{{getReader(){{let s=false;return {{async read(){{if(s)return {{done:true}};s=true;return {{done:false,value:b}};}},async cancel(){{}}}};}}}}}};}};
window.fetch=async(input)=>{{const u=typeof input==='string'?input:input.url;calls.push(u);if(u.includes('/tracks?'))return resp([{{kind:'track',id:9,title:'Cached',duration:1,permalink_url:'https://soundcloud.com/a/cached',media:{{transcodings:[{{url:'https://api-v2.soundcloud.com/media/cached',format:{{protocol:'progressive',mime_type:'audio/mpeg'}}}}]}}}}]);if(u.includes('/media/'))return resp({{url:'https://cf-media.sndcdn.com/cached.mp3?token=private'}});return resp({{collection:[],next_href:null}});}};
global.XMLHttpRequest=function(){{}};XMLHttpRequest.prototype.open=function(){{}};XMLHttpRequest.prototype.setRequestHeader=function(){{}};XMLHttpRequest.prototype.send=function(){{}};XMLHttpRequest.prototype.addEventListener=function(){{}};
eval({json.dumps(script)});
(async()=>{{const accepted=window.__omarchyPlayApiTrack('soundcloud:tracks:9',7);await new Promise(r=>setTimeout(r,20));await window.fetch('https://api-v2.soundcloud.com/stream?client_id=test');for(let i=0;i<12;i++)await new Promise(setImmediate);process.stdout.write(JSON.stringify({{accepted,calls,playback:playback.map(({{streamUrl,...x}})=>x)}}));}})();
"""
        result = json.loads(subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True, text=True
        ).stdout)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["playback"][0]["requestId"], 7)
        self.assertNotIn("token=private", json.dumps(result))

    def test_backend_resolution_timeout_invalidates_the_waiting_webkit_generation(self):
        app = load_module()
        script = app.api_request_capture_script()
        source = MODULE_PATH.read_text()

        self.assertIn("const waitDeadline = Date.now() + 30000", script)
        timeout_method = source[source.index("def _resolution_timed_out"):source.index(
            "def _respond", source.index("def _resolution_timed_out")
        )]
        self.assertIn("self.selection_generation += 1", timeout_method)

    def test_gst_selection_uses_a_fresh_pipeline_and_ignores_retired_bus_messages(self):
        app = load_module()
        class Bus:
            def __init__(self): self.handler = None; self.disconnected = False
            def add_signal_watch(self): pass
            def connect(self, _name, callback, pipeline, generation):
                self.handler = (callback, pipeline, generation); return 1
            def disconnect(self, _handler): self.disconnected = True
            def remove_signal_watch(self): pass
        class Pipeline:
            def __init__(self, name): self.name=name; self.bus=Bus(); self.states=[]
            def get_bus(self): return self.bus
            def set_property(self, *_args): pass
            def set_state(self, state): self.states.append(state); return Gst.StateChangeReturn.SUCCESS
        class Factory:
            made=[]
            @classmethod
            def make(cls, _kind, name): p=Pipeline(name); cls.made.append(p); return p
        class Gst:
            ElementFactory=Factory
            class State: NULL='null'; PLAYING='playing'; PAUSED='paused'
            class StateChangeReturn: FAILURE='failure'; SUCCESS='success'
            class MessageType: ERROR='error'; EOS='eos'; STATE_CHANGED='state'
            @staticmethod
            def init(_value): pass
        player = app.GstPlayback(Gst, lambda: None)
        self.assertTrue(player.hls_slots.acquire(blocking=False))
        self.assertTrue(player.hls_slots.acquire(blocking=False))
        self.assertFalse(player.hls_slots.acquire(blocking=False))
        player.hls_slots.release()
        player.hls_slots.release()
        player.begin({"title":"A"}); self.assertTrue(player.play("https://x", {"title":"A"}))
        first = player.current
        player.begin({"title":"B"}); self.assertTrue(first.bus.disconnected)
        self.assertTrue(player.play("https://y", {"title":"B"}))
        second = player.current
        self.assertIsNot(first, second)
        message = type("Message", (), {"type": Gst.MessageType.ERROR, "src": first})()
        player._message(None, message, first, player.generation - 1)
        self.assertIs(player.current, second)
        self.assertNotEqual(player.state, "error")

    def test_failed_gstreamer_pipelines_are_disconnected_and_set_to_null(self):
        app = load_module()

        class Bus:
            def __init__(self): self.disconnected = False; self.watch_removed = False
            def add_signal_watch(self): pass
            def connect(self, *_args): return 1
            def disconnect(self, _handler): self.disconnected = True
            def remove_signal_watch(self): self.watch_removed = True

        class Pipeline:
            def __init__(self): self.bus = Bus(); self.states = []; self.source = object()
            def get_bus(self): return self.bus
            def get_by_name(self, _name): return self.source
            def set_property(self, *_args): pass
            def set_state(self, state):
                self.states.append(state)
                return Gst.StateChangeReturn.FAILURE if state == Gst.State.PLAYING else Gst.StateChangeReturn.SUCCESS

        class Factory:
            made = []
            @classmethod
            def make(cls, *_args):
                pipeline = Pipeline(); cls.made.append(pipeline); return pipeline

        class Gst:
            ElementFactory = Factory
            class State: NULL = "null"; PLAYING = "playing"; PAUSED = "paused"
            class StateChangeReturn: FAILURE = "failure"; SUCCESS = "success"
            @staticmethod
            def init(_value): pass
            @staticmethod
            def parse_launch(_description):
                pipeline = Pipeline(); Factory.made.append(pipeline); return pipeline

        for protocol, mime_type in (("", ""), ("hls", "audio/mpeg")):
            with self.subTest(protocol=protocol or "progressive"):
                player = app.GstPlayback(Gst, lambda: None)
                self.assertFalse(player.play(
                    "https://example.invalid/audio", {"title": "Track"}, protocol, mime_type
                ))
                failed = Factory.made[-1]
                self.assertIsNone(player.current)
                self.assertTrue(failed.bus.disconnected)
                self.assertTrue(failed.bus.watch_removed)
                self.assertEqual(failed.states, [Gst.State.PLAYING, Gst.State.NULL])
                player.hls_executor.shutdown(wait=False, cancel_futures=True)

    def test_hls_fetch_has_deadline_cancellation_and_proxy_isolation(self):
        app = load_module()
        self.assertEqual(app.HLS_MAX_WORKERS, 2)
        self.assertIn("ProxyHandler({})", MODULE_PATH.read_text())
        with self.assertRaises(TimeoutError):
            app.fetch_private_media(
                "https://cf-media.sndcdn.com/a", 10, deadline=time.monotonic() - 1
            )
        with self.assertRaises(app.concurrent.futures.CancelledError):
            app.fetch_private_media(
                "https://cf-media.sndcdn.com/a", 10, cancelled=lambda: True
            )

    def test_hls_fetch_enforces_deadline_during_slow_drip_reads(self):
        app = load_module()

        class SlowDripResponse:
            def __init__(self):
                self.headers = {}
                self.read_sizes = []
                self.read1_sizes = []

            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def geturl(self): return "https://cf-media.sndcdn.com/slow"

            def read(self, size):
                self.read_sizes.append(size)
                time.sleep(0.2)
                return b"x"

            def read1(self, size):
                self.read1_sizes.append(size)
                time.sleep(0.01)
                return b"x"

        response = SlowDripResponse()
        deadline = time.monotonic() + 0.03
        started = time.monotonic()
        with mock.patch.object(app._media_opener, "open", return_value=response):
            with self.assertRaises(TimeoutError):
                app.fetch_private_media(
                    "https://cf-media.sndcdn.com/slow",
                    1024 * 1024,
                    deadline=deadline,
                )

        self.assertLess(time.monotonic() - started, 0.12)
        self.assertEqual(response.read_sizes, [])
        self.assertGreaterEqual(len(response.read1_sizes), 2)
        self.assertTrue(all(size <= 64 * 1024 for size in response.read1_sizes))

    def test_api_request_capture_pushes_the_frontends_completed_response(self):
        app = load_module()

        script = app.api_request_capture_script()

        self.assertIn("boundedText(response.clone(), 2 * 1024 * 1024)", script)
        self.assertIn("addEventListener('load'", script)
        self.assertIn("messageHandlers.omarchyTracks.postMessage", script)
        self.assertIn("await homeTracks(payload, context, stagedPlayback)", script)
        self.assertIn("reset", script)

    def test_home_mixed_selections_expand_playlist_track_ids_into_real_tracks(self):
        app = load_module()
        capture_script = app.api_request_capture_script()
        harness = f"""
const posted = [];
const calls = [];
global.location = {{ href: 'https://soundcloud.com/discover', hostname: 'soundcloud.com', pathname: '/discover' }};
global.window = global;
window.webkit = {{ messageHandlers: {{
  omarchyTracks: {{ postMessage(value) {{ posted.push(JSON.parse(value)); }} }},
  omarchyPlayback: {{ postMessage() {{}} }}
}} }};
window.fetch = async function(input, init) {{
  const url = typeof input === 'string' ? input : input.url;
  calls.push(url);
  const payload = url.includes('/tracks?') ? [
    {{ kind: 'track', id: 11, urn: 'soundcloud:tracks:11', permalink_url: 'https://soundcloud.com/a/one', title: 'One', duration: 1000, user: {{ username: 'A' }}, media: {{ transcodings: [{{ url: 'https://api-v2.soundcloud.com/media/one', format: {{ protocol: 'progressive', mime_type: 'audio/mpeg' }} }}] }} }},
    {{ kind: 'track', id: 22, urn: 'soundcloud:tracks:22', permalink_url: 'https://soundcloud.com/b/two', title: 'Two', duration: 2000, user: {{ username: 'B' }}, media: {{ transcodings: [{{ url: 'https://api-v2.soundcloud.com/media/two', format: {{ protocol: 'hls', mime_type: 'audio/mp4' }} }}] }} }}
  ] : {{ collection: [
    {{ kind: 'track', id: 33, urn: 'soundcloud:tracks:33', permalink_url: 'https://soundcloud.com/c/embedded', title: 'Embedded', duration: 3000, user: {{ username: 'C' }}, media: {{ transcodings: [{{ url: 'https://api-v2.soundcloud.com/media/embedded', format: {{ protocol: 'progressive', mime_type: 'audio/mpeg' }} }}] }} }},
    {{ kind: 'selection', items: {{ collection: [{{ kind: 'system-playlist', tracks: [{{ id: 11 }}, {{ id: 22 }}] }}] }} }}
  ], next_href: null }};
  const body = JSON.stringify(payload);
  return new Response(body, {{ status: 200 }});
}};
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}};
XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}};
XMLHttpRequest.prototype.addEventListener = function() {{}};
eval({json.dumps(capture_script)});
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/mixed-selections?client_id=public-test', {{ headers: {{ authorization: 'OAuth private-test-value' }} }}));
  for (let index = 0; index < 6; index++) await new Promise(setImmediate);
  process.stdout.write(JSON.stringify({{ calls, posted }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(
            ["node", "-e", harness],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(len(result["posted"]), 1)
        self.assertEqual(
            [track["title"] for track in result["posted"][0]["tracks"]],
            ["Embedded", "One", "Two"],
        )
        self.assertEqual(
            [track["playbackId"] for track in result["posted"][0]["tracks"]],
            ["soundcloud:tracks:33", "soundcloud:tracks:11", "soundcloud:tracks:22"],
        )
        self.assertIn("/tracks?", result["calls"][1])
        self.assertNotIn("private-test-value", completed.stdout)

    def test_playback_resolves_unknown_track_id_directly_without_dom_or_list_map(self):
        app = load_module()
        capture_script = app.api_request_capture_script()
        harness = f"""
const playback = [];
const calls = [];
global.location = {{ href: 'https://soundcloud.com/feed', hostname: 'soundcloud.com', pathname: '/feed' }};
global.document = {{ querySelector() {{ return null; }} }};
global.window = global;
window.webkit = {{ messageHandlers: {{
  omarchyTracks: {{ postMessage() {{}} }},
  omarchyPlayback: {{ postMessage(value) {{ playback.push(JSON.parse(value)); }} }}
}} }};
window.fetch = async function(input) {{
  const url = typeof input === 'string' ? input : input.url;
  calls.push(url);
  let payload;
  if (url.includes('/tracks?')) payload = [{{
    kind: 'track', id: 777, urn: 'soundcloud:tracks:777',
    permalink_url: 'https://soundcloud.com/a/direct', title: 'Direct', duration: 3000,
    user: {{ username: 'A' }}, media: {{ transcodings: [{{
      url: 'https://api-v2.soundcloud.com/media/direct',
      format: {{ protocol: 'progressive', mime_type: 'audio/mpeg' }}
    }}] }}
  }}];
  else if (url.includes('/media/direct')) payload = {{ url: 'https://cf-media.sndcdn.com/direct.mp3?token=private' }};
  else payload = {{ collection: [], next_href: null }};
  const body = JSON.stringify(payload);
  return new Response(body, {{ status: 200 }});
}};
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}};
XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}};
XMLHttpRequest.prototype.addEventListener = function() {{}};
eval({json.dumps(capture_script)});
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/stream?client_id=public-test', {{ headers: {{ authorization: 'OAuth private-test-value' }} }}));
  await new Promise(setImmediate);
  const accepted = window.__omarchyPlayApiTrack('soundcloud:tracks:777', 42);
  for (let index = 0; index < 8; index++) await new Promise(setImmediate);
  const safePlayback = playback.map(({{ streamUrl, ...message }}) => message);
  process.stdout.write(JSON.stringify({{ accepted, calls, playback: safePlayback }}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True, text=True
        )
        result = json.loads(completed.stdout)

        self.assertTrue(result["accepted"])
        self.assertIn("/tracks?", result["calls"][1])
        self.assertEqual(result["playback"][0]["track"]["playbackId"], "soundcloud:tracks:777")
        self.assertEqual(result["playback"][0]["requestId"], 42)
        self.assertTrue(result["playback"][0]["ok"])
        self.assertNotIn("private-test-value", completed.stdout)

    def test_late_resolution_cannot_replace_a_newer_track_selection(self):
        app = load_module()
        capture_script = app.api_request_capture_script()
        harness = f"""
const playback = [];
let finishA;
global.location = {{ href: 'https://soundcloud.com/feed', hostname: 'soundcloud.com', pathname: '/feed' }};
global.document = {{ querySelector() {{ return null; }} }};
global.window = global;
window.webkit = {{ messageHandlers: {{
  omarchyTracks: {{ postMessage() {{}} }},
  omarchyPlayback: {{ postMessage(value) {{ playback.push(JSON.parse(value)); }} }}
}} }};
const response = (payload) => {{
  const body = JSON.stringify(payload);
  return new Response(body, {{ status: 200 }});
}};
const track = (id, name) => ({{
  kind: 'track', id, urn: 'soundcloud:tracks:' + id,
  permalink_url: 'https://soundcloud.com/a/' + name.toLowerCase(), title: name,
  duration: 1000, user: {{ username: 'A' }}, media: {{ transcodings: [{{
    url: 'https://api-v2.soundcloud.com/media/' + name.toLowerCase(),
    format: {{ protocol: 'progressive', mime_type: 'audio/mpeg' }}
  }}] }}
}});
window.fetch = async function(input) {{
  const url = typeof input === 'string' ? input : input.url;
  if (url.includes('/media/a')) return await new Promise((resolve) => {{
    finishA = () => resolve(response({{ url: 'https://cf-media.sndcdn.com/a.mp3?token=private' }}));
  }});
  if (url.includes('/media/b')) return response({{ url: 'https://cf-media.sndcdn.com/b.mp3?token=private' }});
  return response({{ collection: [track(1, 'A'), track(2, 'B')], next_href: null }});
}};
global.XMLHttpRequest = function() {{}};
XMLHttpRequest.prototype.open = function() {{}};
XMLHttpRequest.prototype.setRequestHeader = function() {{}};
XMLHttpRequest.prototype.send = function() {{}};
XMLHttpRequest.prototype.addEventListener = function() {{}};
eval({json.dumps(capture_script)});
(async () => {{
  await window.fetch(new Request('https://api-v2.soundcloud.com/stream?client_id=public-test'));
  await new Promise(setImmediate);
  window.__omarchyPlayApiTrack('soundcloud:tracks:1', 1);
  await new Promise(setImmediate);
  window.__omarchyPlayApiTrack('soundcloud:tracks:2', 2);
  for (let index = 0; index < 4; index++) await new Promise(setImmediate);
  finishA();
  for (let index = 0; index < 4; index++) await new Promise(setImmediate);
  process.stdout.write(JSON.stringify(playback.map(({{ streamUrl, ...message }}) => message)));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
        completed = subprocess.run(
            ["node", "-e", harness], check=True, capture_output=True, text=True
        )
        result = json.loads(completed.stdout)

        self.assertEqual([message["requestId"] for message in result], [2])
        self.assertEqual(result[0]["track"]["title"], "B")
        self.assertNotIn("token=private", completed.stdout)

    def test_injected_track_scripts_do_not_extract_or_replay_frontend_credentials(self):
        source = MODULE_PATH.read_text()

        self.assertNotIn("def tracks_script", source)
        self.assertNotIn("__omarchyMetadataResources", source)
        self.assertNotIn("window.latestRequestContext", source)
        self.assertNotIn("api-v2.soundcloud.com/resolve", source)

    def test_track_metadata_counts_and_duration_are_bounded(self):
        app = load_module()
        payload = {
            "tracks": [{
                "playbackId": "soundcloud:tracks:123",
                "title": "Track",
                "artist": "Artist",
                "url": "https://soundcloud.com/artist/track",
                "artUrl": "",
                "playCount": 123456,
                "durationMs": 245000,
            }]
        }

        track = app.validate_tracks_payload(payload)["tracks"][0]

        self.assertEqual("soundcloud:tracks:123", track["playbackId"])
        self.assertEqual(123456, track["playCount"])
        self.assertEqual(245000, track["durationMs"])
        payload["tracks"][0]["playCount"] = "123456"
        payload["tracks"][0]["durationMs"] = -1
        track = app.validate_tracks_payload(payload)["tracks"][0]
        self.assertEqual(0, track["playCount"])
        self.assertEqual(0, track["durationMs"])

    def test_track_artwork_urls_are_validated_before_tokenization(self):
        app = load_module()
        payload = {
            "tracks": [{
                "title": "Track",
                "artist": "Artist",
                "url": "https://soundcloud.com/artist/track",
                "artUrl": "https://i1.sndcdn.com/artworks-test-t120x120.jpg",
            }]
        }
        validated = app.validate_tracks_payload(payload)

        self.assertEqual(validated["tracks"][0]["artUrl"], payload["tracks"][0]["artUrl"])
        payload["tracks"][0]["artUrl"] = "http://127.0.0.1/private.png"
        self.assertEqual(app.validate_tracks_payload(payload)["tracks"][0]["artUrl"], "")

    def test_track_artwork_is_rewritten_to_a_small_cdn_variant(self):
        app = load_module()

        self.assertEqual(
            app.normalize_list_artwork_url(
                "https://i1.sndcdn.com/artworks-example-t500x500.jpg"
            ),
            "https://i1.sndcdn.com/artworks-example-t120x120.jpg",
        )
        self.assertEqual(
            app.normalize_list_artwork_url("https://evil.example/art-large.jpg"),
            "",
        )

    def test_chunked_artwork_body_is_decoded_with_a_strict_size_limit(self):
        app = load_module()

        self.assertEqual(
            b"Wikipedia",
            app._decode_chunked_body(b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n", 16),
        )
        self.assertIsNone(app._decode_chunked_body(b"11\r\ntoo-large-for-cap\r\n0\r\n\r\n", 8))
        self.assertIsNone(app._decode_chunked_body(b"4\r\nWiki", 16))

    def test_play_track_script_uses_only_stable_playback_id(self):
        app = load_module()

        script = app.play_track_script("soundcloud:tracks:123", 17)

        self.assertIn("soundcloud:tracks:123", script)
        self.assertIn("__omarchyPlayApiTrack(playbackId, 17)", script)
        self.assertIn("__omarchyPlayApiTrack", script)
        self.assertNotIn("document.querySelector", script)
        self.assertNotIn("button.click()", script)

    def test_play_track_script_rejects_invalid_playback_id(self):
        app = load_module()

        self.assertIsNone(app.play_track_script("https://soundcloud.com/artist/track", 1))
        self.assertIsNone(app.play_track_script("soundcloud:tracks:not-a-number", 1))
        self.assertIsNone(app.play_track_script("soundcloud:tracks:123", -1))

    def test_remote_payloads_are_schema_bounded_before_ipc(self):
        app = load_module()
        payload = {
            "title": "t" * 600,
            "artist": "artist",
            "playing": True,
            "loggedIn": True,
            "duration": 123,
            "position": 12,
            "url": "https://soundcloud.com/artist/track",
            "artUrl": "https://evil.example/image.png",
        }

        self.assertIsNone(app.validate_status_payload(payload))
        self.assertIsNone(app.validate_tracks_payload({"tracks": [{"title": "x" * 600}]}))

    def test_artwork_urls_are_restricted_to_soundcloud_cdn(self):
        app = load_module()

        self.assertTrue(app.is_allowed_artwork_uri("https://i1.sndcdn.com/artworks-test-t120x120.png"))
        self.assertFalse(app.is_allowed_artwork_uri("http://i1.sndcdn.com/image.png"))
        self.assertFalse(app.is_allowed_artwork_uri("https://127.0.0.1/image.png"))
        self.assertFalse(app.is_allowed_artwork_uri("file:///etc/passwd"))

    def test_artwork_cache_caps_pending_fetches(self):
        app = load_module()
        release = threading.Event()
        cache = app.ArtworkCache(maximum_entries=2, maximum_pending=3)
        try:
            with mock.patch.object(
                app,
                "fetch_artwork_data_url",
                side_effect=lambda _uri: release.wait(1) or "",
            ):
                for index in range(20):
                    cache.get(f"https://i1.sndcdn.com/artworks-{index}.jpg", lambda: None)
                self.assertLessEqual(len(cache.inflight), 3)
        finally:
            release.set()
            cache.close()


class CheckResultTest(unittest.TestCase):
    def test_dependency_check_is_machine_readable(self):
        app = load_module()

        result = app.dependency_check()

        self.assertEqual(result["python_gobject"], True)
        self.assertEqual(result["gtk"], "3.0")
        self.assertEqual(result["webkit2"], "4.1")
        self.assertEqual(result["gstreamer_autoaudiosink"], True)
        self.assertEqual(result["gstreamer_aac_decoder"], True)
        self.assertEqual(result["gstreamer_playback_elements"], True)
        self.assertIn("ok", result)


class BarWidgetTest(unittest.TestCase):
    def test_runtime_commands_use_one_persistent_socket(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("Socket {", qml)
        self.assertIn("SplitParser {", qml)
        self.assertIn("socket.write(JSON.stringify(payload)", qml)
        self.assertNotIn("id: statusProcess", qml)
        self.assertNotIn("id: tracksProcess", qml)
        self.assertNotIn("id: actionProcess", qml)
        self.assertIn('splitMarker: ""', qml)
        self.assertIn("maxSocketFrameChars", qml)

    def test_connected_backend_never_sends_unsupported_launch_command(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn('if (action === "launch") return "show"', qml)
        self.assertIn("function initializeConnection()", qml)
        self.assertIn('sendCommand("subscribe", "subscribe")', qml)
        self.assertIn('sendCommand("status", "status")', qml)
        self.assertIn("onBackendConnectedChanged: initializeConnection()", qml)

    def test_track_label_is_tooltip_only(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertNotIn('visible: !root.bar.vertical && root.title !== ""', qml)
        self.assertIn("showTooltip(root, root.safeTooltipLabel)", qml)
        self.assertIn('replace(/[<>&', qml)

    def test_bar_remains_icon_only_while_popup_uses_artwork(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertNotIn("id: barArtwork", qml)
        self.assertIn('text: ""', qml)
        self.assertIn("source: root.artDataUrl", qml)
        self.assertNotIn("source: track.artUrl", qml)
        self.assertNotIn("source: root.artUrl", qml)

    def test_track_list_requests_bounded_backend_artwork_by_token(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()
        source = MODULE_PATH.read_text()

        self.assertIn('"artwork:" + artworkId', qml)
        self.assertIn("source: trackArtDataUrl", qml)
        self.assertIn("visible: trackArtDataUrl !== \"\"", qml)
        self.assertNotIn("source: track.artUrl", qml)
        self.assertIn('command.startswith("artwork:")', source)
        self.assertIn("LIST_ARTWORK_BYTES", source)

    def test_track_list_displays_play_count_and_duration(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("formatCompactCount", qml)
        self.assertIn("track.playCount", qml)
        self.assertIn("track.durationMs", qml)
        self.assertIn('"▶ —"', qml)
        self.assertIn('"—:—"', qml)

    def test_theme_colors_follow_bar_palette_except_soundcloud_brand_accent(self):
        import re

        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()
        self.assertIn("readonly property color dim: Qt.darker(bar.foreground, 1.5)", qml)
        self.assertIn("function onForegroundChanged() { waveform.requestPaint() }", qml)
        self.assertEqual(set(re.findall(r"#[0-9A-Fa-f]{6}", qml)), {"#ff5500"})

    def test_every_qml_text_sink_is_plain_text(self):
        import re

        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()
        missing = []
        for match in re.finditer(r"\bText\s*\{", qml):
            index, depth = match.end(), 1
            while index < len(qml) and depth:
                depth += (qml[index] == "{") - (qml[index] == "}")
                index += 1
            if "textFormat: Text.PlainText" not in qml[match.end():index]:
                missing.append(qml[:match.start()].count("\n") + 1)
        self.assertEqual(missing, [])

    def test_launcher_is_isolated_and_does_not_collect_unbounded_stderr(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn('["/usr/bin/python3", "-I", helperPath', qml)
        self.assertNotIn("StdioCollector", qml)
        self.assertNotIn("Component.onCompleted: startBackend", qml)

    def test_webkit_profile_is_ephemeral_and_backend_watches_removal(self):
        source = MODULE_PATH.read_text()

        self.assertIn("WebsiteDataManager.new_ephemeral()", source)
        self.assertNotIn("set_persistent_storage", source)
        self.assertIn("def _check_installation", source)

    def test_popup_uses_seekable_waveform_instead_of_feed_buttons(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertNotIn('text: "Likes"', qml)
        self.assertNotIn('text: "Following"', qml)
        self.assertIn("Canvas {", qml)
        self.assertIn('root.runAction("seek"', qml)

    def test_popup_has_larger_scrollable_home_and_feed_browser(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("Style.space(408)", qml)
        self.assertIn("Style.space(480)", qml)
        self.assertIn('text: "Home"', qml)
        self.assertIn('text: "Feed"', qml)
        self.assertIn("ListView {", qml)
        self.assertIn('root.runAction("play"', qml)
        self.assertIn("modelData.playbackId", qml)
        self.assertNotIn('root.runAction("play-url"', qml)

    def test_track_selection_avoids_transient_signed_out_and_optimistic_metadata(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("reportsLoggedOut", qml)
        self.assertIn("root.loggedIn && !reportsLoggedOut", qml)
        self.assertNotIn('root.title = track.title || ""', qml)
        self.assertIn("(!root.actionBusy || root.selectionBusy)", qml)
        self.assertIn('selectionBusy = action === "play"', qml)
        self.assertIn("activeSelectionRequestId = sentId", qml)
        self.assertIn("isCurrentSelectionResponse(activeSelectionRequestId, message.id)", qml)
        self.assertIn("SoundCloudModel.selectionIsPending(playbackState)", qml)
        self.assertIn('kind === "selection"', qml)

    def test_tab_loading_preserves_now_playing_state_and_each_tab_cache(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("property var homeTracks", qml)
        self.assertIn("property var feedTracks", qml)
        self.assertNotIn("    tracks = []\n    tracksLoading = true", qml)
        self.assertIn("if (!preserveMetadata)", qml)

    def test_track_refresh_requests_the_selected_route_bound_api_source(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn(
            'sendCommand("tracks:" + selectedTab, "tracks:" + selectedTab)',
            qml,
        )

    def test_pending_track_fetch_preserves_the_tab_cache(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("SoundCloudModel.applyTrackResult(cached, result)", qml)

    def test_track_refresh_has_no_legacy_navigation_delay(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertNotIn("interval: 1400", qml)
        self.assertIn("interval: 100", qml)
        self.assertIn("runAction(tab)\n    refreshTracks()", qml)
        self.assertIn("message.pending === true", qml)
        self.assertIn('if (message.type === "tracks")', qml)

    def test_track_lists_request_more_before_reaching_the_exact_end(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertNotIn("onContentYChanged: root.loadMoreTracks", qml)
        self.assertNotIn("onCountChanged: root.loadMoreTracks", qml)
        self.assertIn("onMovementEnded: root.loadMoreTracks", qml)
        self.assertIn("userInitiated: userInitiated", qml)
        self.assertIn("yPosition: yPosition", qml)
        self.assertIn("heightRatio: heightRatio", qml)
        self.assertIn('sendCommand("load-more:" + selectedTab', qml)

    def test_lazy_loading_has_bounded_observability_events(self):
        source = MODULE_PATH.read_text()

        self.assertIn('"lazy-load-request"', source)
        self.assertIn('"track-page"', source)

    def test_coalesced_socket_frames_are_limited_after_complete_lines_are_parsed(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()
        function = qml[qml.index("function handleSocketChunk"):qml.index(
            "onBackendConnectedChanged"
        )]

        self.assertLess(function.index("while ((newline"), function.index(
            "if (socketBuffer.length > maxSocketFrameChars)"
        ))
        self.assertIn("if (line.length > maxSocketFrameChars)", function)

    def test_disconnect_clears_partial_frames_and_inflight_request_state(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("function resetConnectionState()", qml)
        self.assertIn('socketBuffer = ""', qml)
        self.assertIn("pendingRequests = ({})", qml)
        self.assertIn("tracksRequestPending = false", qml)
        self.assertIn("trackArtworkPending = ({})", qml)
        self.assertIn("root.resetConnectionState()", qml)

    def test_empty_navigation_status_updates_timeline_without_erasing_metadata(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("var preserveMetadata", qml)
        self.assertIn("SoundCloudModel.shouldPreservePlaybackMetadata(state, root.title)", qml)
        self.assertLess(qml.index("playing = state.playing === true"), qml.index(
            "if (!preserveMetadata)"
        ))
        self.assertLess(qml.index("duration = Number(state.duration || 0)"), qml.index(
            "if (!preserveMetadata)"
        ))
        self.assertNotIn('if (!hasIncomingTrack && root.title !== "") return', qml)

    def test_now_playing_sections_keep_their_layout_while_data_is_missing(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("property bool hasTrack", qml)
        self.assertIn("opacity: root.hasTrack ? 1 : 0.45", qml)
        self.assertNotIn('visible: root.running && root.loggedIn && root.title !== ""', qml)

    def test_popup_has_no_bottom_shortcut_help_text(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertNotIn("Middle-click: play/pause", qml)

    def test_track_browser_fills_remaining_popup_height(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn(
            "height: Math.max(0, parent.height - y - (errorText.visible",
            qml,
        )
        self.assertIn("id: errorText", qml)
        self.assertNotIn("height: Style.space(178)", qml)

    def test_readme_documents_safe_removal_and_persistent_state(self):
        readme = (MODULE_PATH.parent / "README.md").read_text()

        self.assertIn("## Removing", readme)
        self.assertIn("omarchy plugin remove brunosilveira.soundcloud", readme)
        self.assertIn("ephemeral in-memory profile and cache", readme)
        self.assertIn("python3 soundcloud_app.py stop", readme)


if __name__ == "__main__":
    unittest.main()
