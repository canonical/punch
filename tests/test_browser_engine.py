import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from playwright.sync_api import Error as PlaywrightError
from typer.testing import CliRunner

from punch.ui.cli import app
from punch.web import (
    BrowserInstallError,
    BrowserLaunchError,
    BrowserSelectionError,
    SubmitInterruptedError,
    TimecardEntry,
    _get_browser_type,
    _launch_browser,
    submit_timecards,
)


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

    def test_prompt_text_follows_engine_list(self):
        Path(self.executable_paths["chromium"]).touch()
        Path(self.executable_paths["firefox"]).touch()
        self.console.input.side_effect = ["9", "2"]
        engines = (("chromium", "Chromium"), ("firefox", "Firefox"))

        with patch("punch.web._BROWSER_ENGINES", engines):
            selected = _get_browser_type(self.playwright, self.console)

        self.assertIs(selected, self.playwright.firefox)
        self.console.input.assert_called_with("Browser [1-2]: ")
        self.console.print.assert_any_call("[red]Choose 1 or 2.[/red]")

    def test_invalid_choice_lists_all_engines(self):
        Path(self.executable_paths["webkit"]).touch()
        self.console.input.side_effect = ["0", "3"]

        _get_browser_type(self.playwright, self.console)

        self.console.input.assert_called_with("Browser [1-3]: ")
        self.console.print.assert_any_call("[red]Choose 1, 2, or 3.[/red]")

    def test_reuses_installed_saved_choice_without_prompt(self):
        Path(self.executable_paths["webkit"]).touch()
        Path(self.preference_path).write_text('{"engine": "webkit"}')

        with patch("punch.web.subprocess.run") as run:
            selected = _get_browser_type(self.playwright, self.console)

        self.assertIs(selected, self.playwright.webkit)
        self.console.input.assert_not_called()
        run.assert_not_called()

    def test_preference_write_failure_only_warns(self):
        Path(self.executable_paths["firefox"]).touch()
        self.console.input.return_value = "2"

        with patch("punch.web.os.replace", side_effect=PermissionError("read-only")):
            selected = _get_browser_type(self.playwright, self.console)

        self.assertIs(selected, self.playwright.firefox)
        self.assertFalse(os.path.exists(self.preference_path))
        self.assertFalse(os.path.exists(f"{self.preference_path}.tmp"))
        self.console.print.assert_any_call("[yellow]Could not save browser preference: read-only[/yellow]")

    def test_install_failure_is_reported(self):
        self.console.input.return_value = "1"
        with patch(
            "punch.web.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["playwright", "install"]),
        ):
            with self.assertRaisesRegex(BrowserInstallError, "Failed to download"):
                _get_browser_type(self.playwright, self.console)

        self.assertFalse(os.path.exists(self.preference_path))

    def test_selection_can_be_cancelled_at_terminal(self):
        self.console.input.side_effect = EOFError

        with patch("punch.web._stdin_is_terminal", return_value=True):
            with self.assertRaisesRegex(BrowserSelectionError, "selection cancelled"):
                _get_browser_type(self.playwright, self.console)

        self.assertFalse(os.path.exists(self.preference_path))

    def test_non_interactive_input_without_choice_defaults_to_firefox(self):
        Path(self.executable_paths["firefox"]).touch()
        self.console.input.side_effect = ["y", EOFError]

        with patch("punch.web._stdin_is_terminal", return_value=False), \
             patch("punch.web.subprocess.run") as run:
            selected = _get_browser_type(self.playwright, self.console)

        self.assertIs(selected, self.playwright.firefox)
        run.assert_not_called()
        self.assertEqual(json.loads(Path(self.preference_path).read_text()), {"engine": "firefox"})

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

    def test_launch_failure_is_reported_with_hint(self):
        browser = Mock()
        browser.name = "webkit"
        browser.executable_path = "/browsers/webkit/pw_run.sh"
        browser.launch.side_effect = PlaywrightError(
            "BrowserType.launch: \n"
            "╔══════════════════════════════════════════════════════╗\n"
            "║ Host system is missing dependencies to run browsers. ║\n"
            "║     sudo apt-get install libicu74                    ║\n"
            "╚══════════════════════════════════════════════════════╝"
        )
        for environment, expects_hint in (({}, True), ({"SNAP_NAME": "punch", "SNAP": "/snap/punch/1"}, False)):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), \
                 patch("punch.web._get_browser_type", return_value=browser):
                with self.assertRaises(BrowserLaunchError) as raised:
                    _launch_browser(self.playwright, self.console, False)
                message = str(raised.exception)
                self.assertIn("Failed to launch webkit", message)
                self.assertIn("Host system is missing dependencies to run browsers.", message)
                self.assertIn("libicu74", message)
                self.assertEqual("playwright install-deps webkit" in message, expects_hint)

    def test_launch_failure_preserves_browser_logs(self):
        browser = Mock()
        browser.name = "chromium"
        browser.launch.side_effect = PlaywrightError(
            "BrowserType.launch: Target page, context or browser has been closed\n"
            "Browser logs:\n[pid=123][err] error while loading shared libraries: libexample.so"
        )
        with patch("punch.web._get_browser_type", return_value=browser):
            with self.assertRaises(BrowserLaunchError) as raised:
                _launch_browser(self.playwright, self.console, False)
        self.assertIn("[pid=123][err] error while loading shared libraries: libexample.so", str(raised.exception))

    def test_snap_webkit_without_snap_dir_skips_egl_default(self):
        browser = Mock()
        browser.name = "webkit"
        browser.executable_path = "/browsers/webkit/pw_run.sh"
        with patch("punch.web._get_browser_type", return_value=browser), \
             patch.dict(os.environ, {"SNAP_NAME": "punch"}, clear=True):
            _launch_browser(self.playwright, self.console, False)
        self.assertNotIn("__EGL_VENDOR_LIBRARY_DIRS", browser.launch.call_args.kwargs["env"])

    def test_other_browser_launches_are_unchanged(self):
        for name, environment in (("webkit", {}), ("chromium", {"SNAP_NAME": "punch"})):
            with self.subTest(name=name), patch.dict(os.environ, environment, clear=True):
                browser = Mock()
                browser.name = name
                with patch("punch.web._get_browser_type", return_value=browser):
                    _launch_browser(self.playwright, self.console, False)
                browser.launch.assert_called_once_with(headless=False)


class TestLoginInterruption(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.auth_path = Path(self.temp.name) / "auth.json"
        self.browser = Mock()
        self.browser.is_connected.return_value = True
        self.context = self.browser.new_context.return_value
        self.page = self.context.new_page.return_value
        self.page.is_closed.return_value = False

    def invoke_login(self, launch_side_effect=None):
        with patch("punch.ui.cli.load_config", return_value={"timecards_url": "https://example.test/"}), \
             patch("punch.web.get_auth_json_path", return_value=str(self.auth_path)), \
             patch("punch.web.sync_playwright"), \
             patch("punch.web._launch_browser", return_value=self.browser, side_effect=launch_side_effect):
            return CliRunner().invoke(app, ["login"])

    def test_closed_page_during_navigation_or_wait_is_reported_without_traceback(self):
        for operation in ("goto", "wait_for_url"):
            with self.subTest(operation=operation):
                self.page.reset_mock()
                self.context.storage_state.reset_mock()
                self.page.is_closed.return_value = True
                getattr(self.page, operation).side_effect = PlaywrightError("Target closed")
                result = self.invoke_login()
                self.assertEqual(result.exit_code, 1)
                self.assertIn("Login interrupted", result.output)
                self.assertNotIn("Traceback", result.output)
                self.context.storage_state.assert_not_called()
                getattr(self.page, operation).side_effect = None

    def test_browser_closed_before_page_creation_is_reported(self):
        self.browser.is_connected.return_value = False
        self.context.new_page.side_effect = PlaywrightError("Target closed")
        result = self.invoke_login()
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Login interrupted", result.output)
        self.assertNotIn("Traceback", result.output)
        self.context.storage_state.assert_not_called()

    def test_keyboard_interrupt_is_reported_without_traceback(self):
        result = self.invoke_login(launch_side_effect=KeyboardInterrupt)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Login interrupted", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_launch_error_prints_browser_logs_without_interpreting_markup(self):
        result = self.invoke_login(launch_side_effect=BrowserLaunchError(
            "Failed to launch chromium:\n[pid=123][err] missing libexample.so"
        ))
        self.assertEqual(result.exit_code, 1)
        self.assertIn("[pid=123][err] missing libexample.so", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_keyboard_interrupt_masked_by_driver_shutdown_is_reported(self):
        self.page.wait_for_url.side_effect = KeyboardInterrupt
        playwright_cm = MagicMock()
        playwright_cm.return_value.__exit__.side_effect = PlaywrightError("Connection closed")
        with patch("punch.ui.cli.load_config", return_value={"timecards_url": "https://example.test/"}), \
             patch("punch.web.get_auth_json_path", return_value=str(self.auth_path)), \
             patch("punch.web.sync_playwright", playwright_cm), \
             patch("punch.web._launch_browser", return_value=self.browser):
            result = CliRunner().invoke(app, ["login"])
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Login interrupted", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_unrelated_playwright_error_is_not_treated_as_cancellation(self):
        self.page.goto.side_effect = PlaywrightError("TLS failure")
        result = self.invoke_login()
        self.assertIsInstance(result.exception, PlaywrightError)
        self.assertIn("TLS failure", str(result.exception))
        self.assertNotIn("Login interrupted", result.output)


class TestSubmitInterruption(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.auth_path = Path(self.temp.name) / "auth.json"
        self.auth_path.write_text("{}")
        import datetime
        self.timecards = [TimecardEntry(
            "00000001", "Owner", 30, datetime.date(2026, 1, 1), datetime.time(9, 0), "work", "desc"
        )]

    def submit(self, playwright_cm=None, launch_side_effect=None):
        with patch("punch.web.get_auth_json_path", return_value=str(self.auth_path)), \
             patch("punch.web.sync_playwright", playwright_cm or MagicMock()), \
             patch("punch.web._launch_browser", side_effect=launch_side_effect):
            return submit_timecards({"timecards_url": "https://example.test/"}, self.timecards)

    def test_keyboard_interrupt_during_browser_selection(self):
        with self.assertRaisesRegex(SubmitInterruptedError, "Submission interrupted"):
            self.submit(launch_side_effect=KeyboardInterrupt)

    def test_keyboard_interrupt_masked_by_driver_shutdown(self):
        playwright_cm = MagicMock()
        playwright_cm.return_value.__exit__.side_effect = PlaywrightError("Connection closed")
        with self.assertRaisesRegex(SubmitInterruptedError, "Submission interrupted"):
            self.submit(playwright_cm, launch_side_effect=KeyboardInterrupt)

    def test_unrelated_playwright_error_propagates(self):
        with self.assertRaises(PlaywrightError):
            self.submit(launch_side_effect=PlaywrightError("Host system is missing dependencies"))


if __name__ == "__main__":
    unittest.main()
