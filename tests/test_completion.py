import os
from pathlib import Path
import pty
import select
import shlex
import signal
import subprocess
import tempfile
import time
import unittest

import click
from typer.main import get_command

from punch.ui.cli import app


COMPLETION = Path(__file__).resolve().parents[1] / "punch-completion.bash"


class TestBashCompletion(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        config = root / "config"
        data = root / "data"
        config.mkdir()
        data.mkdir()
        (config / "punch.yaml").write_text(
            "full_name: Test User\n"
            "categories:\n"
            "  Coding:\n"
            "    short: c\n"
            "  Meeting:\n"
            "    short: m\n"
        )
        (data / "tasks.txt").write_text(
            "2025-01-01 09:00 | Coding | Fix a bug\n"
            "2025-01-01 10:00 | Coding | Feature work\n"
            "2025-01-01 11:00 | Meeting | Standup\n"
        )
        self.env = {
            **os.environ,
            "HOME": str(root),
            "PUNCH_CONFIG_DIR": str(config),
            "PUNCH_DATA_DIR": str(data),
            "XDG_CONFIG_HOME": str(root / "xdg"),
        }

    def complete(self, *words):
        result = subprocess.run(
            [
                "bash",
                "-c",
                'set -e; source "$1"; shift; COMP_WORDS=("$@"); '
                'COMP_CWORD=$((${#COMP_WORDS[@]} - 1)); '
                '_punch_complete; printf "%s\\n" "${COMPREPLY[@]}"',
                "bash",
                str(COMPLETION),
                "punch",
                *words,
            ],
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        )
        return [line for line in result.stdout.splitlines() if line]

    def test_commands_and_options_match_cli(self):
        root = get_command(app)
        self.assertEqual({word for word in self.complete("") if not word.startswith("-")}, set(root.commands))
        groups = [([], root)]
        for name, command in root.commands.items():
            groups.append(([name], command))
            if isinstance(command, click.Group):
                self.assertEqual(
                    {word for word in self.complete(name, "") if not word.startswith("-")},
                    set(command.commands),
                )
                groups.extend(([name, subname], subcommand) for subname, subcommand in command.commands.items())

        for path, command in groups:
            with self.subTest(path=path):
                options = {"--help"}
                for param in command.params:
                    if isinstance(param, click.Option):
                        options.update(param.opts)
                        options.update(param.secondary_opts)
                self.assertEqual(set(self.complete(*path, "-")), options)

    def test_help_and_option_values(self):
        self.assertIn("weeklyeditor", self.complete("help", "w"))
        self.assertEqual(self.complete("help", "config", "pa"), ["path"])
        self.assertEqual(self.complete("export", "--format", "j"), ["json"])
        self.assertEqual(self.complete("export", "--format", "=", "c"), ["csv"])
        export_path = Path(self.temp.name) / "export file.json"
        export_path.touch()
        self.assertEqual(self.complete("export", "-o", str(export_path)[:-5]), [str(export_path)])
        self.assertEqual(self.complete("submit", "--day", "y"), ["yesterday"])
        self.assertEqual(self.complete("start", "--time", ""), [])
        self.assertEqual(self.complete("add", "--time", "=", ""), [])
        self.assertEqual(self.complete("config", "get", "full"), ["full_name"])
        self.assertEqual(self.complete("config", "get", "-v", "full"), ["full_name"])
        self.assertEqual(self.complete("config", "set", "full_name", ""), [])

    def test_add_categories_and_tasks_with_options(self):
        self.assertEqual(self.complete("add", ""), ["c", "m"])
        self.assertEqual(self.complete("add", "-t", "09", ":", "00", "c"), ["c"])
        self.assertEqual(self.complete("add", "--time", "=", "09", ":", "00", "c"), ["c"])
        self.assertEqual(self.complete("add", "--time=09:00", "c"), ["c"])
        self.assertEqual(self.complete("add", "c", ""), [":"])
        self.assertEqual(self.complete("add", "c", ":", "Fix"), ["Fix a bug"])
        self.assertEqual(self.complete("add", "-v", "c:", "Feature"), ["Feature work"])
        self.assertEqual(self.complete("add", ":", ""), [])
        self.assertEqual(self.complete("add", "c", ":", "missing"), [])

    def test_interactive_task_completion_quotes_spaces(self):
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe(
                "bash",
                ["bash", "--noprofile", "--norc", "-i"],
                {**self.env, "PS1": "READY> ", "TERM": "dumb"},
            )

        def read_prompt():
            output = b""
            deadline = time.monotonic() + 10
            while b"READY> " not in output:
                timeout = max(0, deadline - time.monotonic())
                if not select.select([fd], [], [], timeout)[0]:
                    self.fail(f"Timed out waiting for Bash prompt: {output!r}")
                output += os.read(fd, 4096)
            return output

        try:
            read_prompt()
            setup = f"source {shlex.quote(str(COMPLETION))}; punch() {{ printf 'ARG<%s>\\n' \"$@\"; }}\n"
            os.write(fd, setup.encode())
            read_prompt()
            os.write(fd, b"punch add c : Fix\t\n")
            self.assertIn(b"ARG<Fix a bug>", read_prompt())
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
