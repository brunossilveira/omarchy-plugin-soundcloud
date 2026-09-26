import importlib.util
import json
import os
import socket
import stat
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
            "title", "artist", "url", "artUrl", "playCount", "durationMs",
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

        for field in ("playing", "title", "artist", "artUrl", "loggedIn", "url"):
            self.assertIn(field, script)
        self.assertIn("playControls__play", script)
        self.assertIn("Pause", script)
        self.assertIn("playbackTimeline__timePassed", script)
        self.assertIn("playbackTimeline__duration", script)
        self.assertIn("getComputedStyle(art).backgroundImage", script)
        self.assertIn(".playbackSoundBadge__avatar [style*=\"background-image\"]", script)
        self.assertIn("signin|register", script)

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

    def test_api_observation_script_exposes_only_bounded_resource_paths(self):
        app = load_module()

        script = app.api_observation_script()

        self.assertIn("api-v2.soundcloud.com", script)
        self.assertIn("candidate.pathname", script)
        self.assertNotIn("candidate.href", script)
        self.assertIn("slice(0, 32)", script)

    def test_api_request_capture_does_not_retain_or_replay_frontend_credentials(self):
        app = load_module()

        capture_script = app.api_request_capture_script()
        tracks_script = app.api_tracks_script("feed")

        self.assertIn("window.fetch", capture_script)
        self.assertIn("/stream", capture_script)
        self.assertNotIn("authorization", capture_script.lower())
        self.assertNotIn("client-id", capture_script.lower())
        self.assertNotIn("__omarchyApiRequests", capture_script)
        self.assertNotIn("console.", capture_script)
        self.assertNotIn("fetch(", tracks_script)
        self.assertNotIn("performance.getEntriesByType", tracks_script)

    def test_api_request_capture_pushes_the_frontends_completed_response(self):
        app = load_module()

        script = app.api_request_capture_script()

        self.assertIn("response.clone().text()", script)
        self.assertIn("addEventListener('load'", script)
        self.assertIn("messageHandlers.omarchyTracks.postMessage", script)
        self.assertIn("normalise(JSON.parse(text))", script)

    def test_injected_track_scripts_do_not_extract_or_replay_frontend_credentials(self):
        source = MODULE_PATH.read_text()

        self.assertNotIn("def tracks_script", source)
        self.assertNotIn("__omarchyMetadataResources", source)
        self.assertNotIn("searchParams.get('client_id')", source)
        self.assertNotIn("api-v2.soundcloud.com/resolve", source)

    def test_track_metadata_counts_and_duration_are_bounded(self):
        app = load_module()
        payload = {
            "tracks": [{
                "title": "Track",
                "artist": "Artist",
                "url": "https://soundcloud.com/artist/track",
                "artUrl": "",
                "playCount": 123456,
                "durationMs": 245000,
            }]
        }

        track = app.validate_tracks_payload(payload)["tracks"][0]

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

    def test_play_track_script_clicks_existing_soundcloud_card(self):
        app = load_module()

        script = app.play_track_script("https://soundcloud.com/artist/track")

        self.assertIn("https://soundcloud.com/artist/track", script)
        self.assertIn("playableTile", script)
        self.assertIn("playButton", script)
        self.assertIn("button.click()", script)
        self.assertIn("setTimeout", script)
        self.assertIn("playControls__play", script)

    def test_play_track_script_rejects_non_soundcloud_url(self):
        app = load_module()

        self.assertIsNone(app.play_track_script("https://example.com/track"))

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
        self.assertIn("readonly property color barDim: Qt.darker(bar.barForeground, 1.5)", qml)
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
        self.assertIn('root.runAction("play-url"', qml)

    def test_track_selection_avoids_transient_signed_out_and_optimistic_metadata(self):
        qml = (MODULE_PATH.parent / "BarWidget.qml").read_text()

        self.assertIn("reportsLoggedOut", qml)
        self.assertIn("root.loggedIn && !reportsLoggedOut", qml)
        self.assertNotIn('root.title = track.title || ""', qml)
        self.assertIn("enabled: !root.actionBusy", qml)

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
        self.assertIn("soundcloud\\.com\\/(discover|feed)", qml)
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
