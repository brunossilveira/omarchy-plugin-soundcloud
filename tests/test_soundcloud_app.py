import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


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

        self.assertEqual(path, Path("/run/user/123/omarchy-soundcloud.sock"))

    def test_only_known_player_actions_have_scripts(self):
        app = load_module()

        self.assertIn("audio.play()", app.command_script("play-pause"))
        self.assertIn("skipControl__next", app.command_script("next"))
        self.assertIn("skipControl__previous", app.command_script("previous"))
        self.assertIsNone(app.command_script("arbitrary-javascript"))

    def test_status_script_returns_required_fields(self):
        app = load_module()

        script = app.status_script()

        for field in ("playing", "title", "artist", "artUrl", "loggedIn", "url"):
            self.assertIn(field, script)
        self.assertIn("signin|register", script)

    def test_source_routes_are_fixed_soundcloud_pages(self):
        app = load_module()

        self.assertEqual(app.source_uri("likes"), "https://soundcloud.com/you/likes")
        self.assertEqual(app.source_uri("feed"), "https://soundcloud.com/stream")
        self.assertIsNone(app.source_uri("https://evil.example"))


class CheckResultTest(unittest.TestCase):
    def test_dependency_check_is_machine_readable(self):
        app = load_module()

        result = app.dependency_check()

        self.assertEqual(result["python_gobject"], True)
        self.assertEqual(result["gtk"], "3.0")
        self.assertEqual(result["webkit2"], "4.1")
        self.assertIn("ok", result)


if __name__ == "__main__":
    unittest.main()
