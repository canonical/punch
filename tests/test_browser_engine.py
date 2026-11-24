import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from punch.web import BrowserInstallError, BrowserSelectionError, _get_browser_type, _launch_browser


class TestBrowserEngine(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.preference_path = os.path.join(self.temp.name, "browser.json")
        self.preference_patch = patch(
            "punch.web._get_browser_preference_path",
            return_value=self.preference_path,
        )
        self.preference_patch.start()
        self.addCleanup(self.preference_patch.stop)

        self.executable_paths = {
            engine: os.path.join(self.temp.name, engine)
            for engine in ("chromium", "firefox", "webkit")
        }
        self.playwright = SimpleNamespace(
            **{
                engine: SimpleNamespace(executable_path=path)
                for engine, path in self.executable_paths.items()
            }
        )
        self.console = Mock()

    def test_prompts_then_downloads_and_remembers_choice(self):
        self.console.input.return_value = "2"

        def install(command, check):
            self.assertTrue(check)
            self.assertEqual(command[-2:], ["install", "firefox"])
            Path(self.executable_paths["firefox"]).touch()

        with patch("punch.web.subprocess.run", side_effect=install) as run:
            selected = _get_browser_type(self.playwright, self.console)

        self.assertIs(selected, self.playwright.firefox)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(json.loads(Path(self.preference_path).read_text()), {"engine": "firefox"})

    def test_reuses_installed_saved_choice_without_prompt(self):
        Path(self.executable_paths["webkit"]).touch()
        Path(self.preference_path).write_text('{"engine": "webkit"}')

        with patch("punch.web.subprocess.run") as run:
            selected = _get_browser_type(self.playwright, self.console)

        self.assertIs(selected, self.playwright.webkit)
        self.console.input.assert_not_called()
        run.assert_not_called()

    def test_install_failure_is_reported(self):
        self.console.input.return_value = "1"
        with patch(
            "punch.web.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["playwright", "install"]),
        ):
            with self.assertRaisesRegex(BrowserInstallError, "Failed to download"):
                _get_browser_type(self.playwright, self.console)

        self.assertFalse(os.path.exists(self.preference_path))

    def test_selection_can_be_cancelled(self):
        self.console.input.side_effect = EOFError

        with self.assertRaisesRegex(BrowserSelectionError, "selection cancelled"):
            _get_browser_type(self.playwright, self.console)

        self.assertFalse(os.path.exists(self.preference_path))

    def test_invalid_saved_choice_prompts_again(self):
        Path(self.preference_path).write_text('{"engine": "unknown"}')
        Path(self.executable_paths["chromium"]).touch()
        self.console.input.return_value = "1"

        selected = _get_browser_type(self.playwright, self.console)

        self.assertIs(selected, self.playwright.chromium)
        self.assertEqual(json.loads(Path(self.preference_path).read_text()), {"engine": "chromium"})
        self.console.print.assert_any_call(
            "[yellow]Saved browser choice is invalid; choose an engine again.[/yellow]"
        )

    def test_snap_webkit_preserves_library_path_for_both_modes(self):
        browser = Mock()
        browser.name = "webkit"
        browser.executable_path = "/browsers/webkit/pw_run.sh"
        with patch("punch.web._get_browser_type", return_value=browser), patch.dict(
            os.environ, {"SNAP_NAME": "punch", "SNAP": "/snap/punch/1", "LD_LIBRARY_PATH": "/snap/libs"}, clear=True
        ):
            for headless, directory in ((False, "minibrowser-gtk"), (True, "minibrowser-wpe")):
                with self.subTest(headless=headless):
                    _launch_browser(self.playwright, self.console, headless)
                    kwargs = browser.launch.call_args.kwargs
                    base = f"/browsers/webkit/{directory}"
                    self.assertEqual(kwargs["executable_path"], f"{base}/bin/MiniBrowser")
                    self.assertEqual(kwargs["headless"], headless)
                    self.assertEqual(kwargs["env"]["LD_LIBRARY_PATH"], f"{base}/lib:{base}/sys/lib:/snap/libs")
                    self.assertEqual(kwargs["env"]["WEBKIT_INJECTED_BUNDLE_PATH"], f"{base}/lib")
                    self.assertEqual(kwargs["env"]["__EGL_VENDOR_LIBRARY_DIRS"], "/snap/punch/1/usr/share/glvnd/egl_vendor.d")
                    if headless:
                        self.assertEqual(kwargs["env"]["WEBKIT_INSPECTOR_RESOURCES_PATH"], f"{base}/share")

    def test_other_browser_launches_are_unchanged(self):
        for name, environment in (("webkit", {}), ("chromium", {"SNAP_NAME": "punch"})):
            with self.subTest(name=name), patch.dict(os.environ, environment, clear=True):
                browser = Mock()
                browser.name = name
                with patch("punch.web._get_browser_type", return_value=browser):
                    _launch_browser(self.playwright, self.console, False)
                browser.launch.assert_called_once_with(headless=False)


if __name__ == "__main__":
    unittest.main()
