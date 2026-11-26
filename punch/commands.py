from datetime import datetime, timedelta
import json
import os
import re
import sys
import tempfile
from rich.console import Console
from rich.markup import escape
from rich.tree import Tree
from rich.syntax import Syntax
import yaml

from playwright.sync_api import TimeoutError

from punch.config import get_config_path, set_config_value
from punch.export import export_csv, export_json
from punch.report import generate_report
from punch.tasks import CMDLINE_SEPARATOR, TaskEntry, parse_new_task_string, write_task
from punch.web import DRY_RUN_SUFFIX, AuthFileNotFoundError, BrowserInstallError, BrowserLaunchError, BrowserSelectionError, LoginInterruptedError, MissingTimecardsUrl, SubmitInterruptedError, NoCaseMappingError, get_timecards, login_to_site, submit_timecards, timecard_id

# Constants for progress file handling
_PROGRESS_TEMP_SUFFIX = '.tmp'
    
def time_to_current_datetime(time_str: str) -> datetime:
    """Combine the current date with a given time string (HH:MM) to produce a datetime."""
    now = datetime.now()
    t = datetime.strptime(time_str, "%H:%M").time()
    return datetime.combine(now.date(), t)

def show_config(config):
    """
    Pretty-print the loaded config as YAML.
    """
    from rich.syntax import Syntax

    console = Console()
    yaml_str = yaml.dump(config, sort_keys=False, allow_unicode=True)
    syntax = Syntax(yaml_str, "yaml", theme="ansi_dark", line_numbers=False)
    console.print(syntax)

def prompt_with_hint(console, prompt, current_value):
    """
    Prompt the user with a hint of the current value.
    Returns the new value or the current value if input is empty.
    """
    value = console.input(f"{prompt}{f' [{current_value!r}]' if current_value else ''}: ").strip()
    return value if value else current_value

def prompt_category(console, existing_short=None, existing_name=None, existing_caseid=None):
    """
    Prompt for a single category's details.
    Returns (short, name, caseid) or (None, None, None) if user is done.
    """
    short = console.input(
        f"  Short code (e.g. 'dev', 'mtg'){f' [{existing_short}]' if existing_short else ''} [leave empty to finish]: "
    ).strip()
    if not short:
        return None, None, None

    # Require non-empty category name
    while True:
        name = console.input(
            f"[{short!r}]  Full category name{f' [{existing_name}]' if existing_name else ''}: "
        ).strip()
        if name:
            break
        elif existing_name:
            name = existing_name
            break
        else:
            console.print("[red]Category name cannot be empty.[/red]")

    # Prompt for caseid, repeat if not a number (allow empty to skip)
    while True:
        caseid = console.input(
            f"[{short!r}]  Case id to file timecards against"
            f"{f' [{existing_caseid}]' if existing_caseid else ''} (leave empty to skip): "
        ).strip()
        if not caseid and existing_caseid:
            caseid = existing_caseid
            break
        if not caseid:
            caseid = None
            break
        if caseid.isdigit():
            caseid = caseid.zfill(8)
            break
        else:
            console.print("[red]Case id must be a number or empty.[/red]")
    return short, name, caseid

def print_existing_categories(console, categories):
    if categories:
        console.print("[yellow]Existing categories:[/yellow]")
        for name, cat in categories.items():
            short = cat.get("short", "")
            caseid = cat.get("caseid", "")
            console.print(f"  [cyan]{short}[/cyan]: {name}" + (f" (caseid: {caseid})" if caseid else ""))

def run_config_wizard(config, config_path):
    """
    Interactively prompt the user for config values and update the config file.
    """
    from ruamel.yaml import YAML
    yaml_ruamel = YAML()
    yaml_ruamel.preserve_quotes = True

    console = Console()
    console.print("[bold green]Welcome to the Punch configuration wizard![/bold green]")

    # Full name
    current_full_name = config.get("full_name", "")
    config["full_name"] = prompt_with_hint(console, "Enter your full name", current_full_name)

    # Timecards submissions link
    current_url = config.get("timecards_url", "")
    config["timecards_url"] = prompt_with_hint(console, "Enter the new timecard link (URL)", current_url)

    date_format = config.get("date_format", "%d/%m/%Y")
    config["date_format"] = prompt_with_hint(console, "Enter the timecard date format", date_format)

    time_format = config.get("time_format", "%H:%M")
    config["time_format"] = prompt_with_hint(console, "Enter the timecard time format", time_format)

    # Timecards rounding
    current_round = config.get("timecards_round", 0)
    while True:
        round_str = prompt_with_hint(
            console,
            "Round tracked time to nearest <X> minutes (set to 0 to disable rounding)",
            current_round
        )
        try:
            config["timecards_round"] = int(round_str)
            break
        except (TypeError, ValueError):
            console.print("[red]Please enter a valid integer for rounding.[/red]")

    # Categories
    # Load existing categories as a dict (name -> entry)
    categories = {}
    existing_categories = config.get("categories", {})
    if isinstance(existing_categories, dict):
        categories.update(existing_categories)
    elif isinstance(existing_categories, list):
        for cat in existing_categories:
            name = cat.get("name") or cat.get("category") or cat.get("short")
            if name:
                categories[name] = cat

    console.print("Let's add your categories. Enter each category's details. Leave short code empty to finish.")
    print_existing_categories(console, categories)

    # Add new categories, don't replace existing
    while True:
        short, name, caseid = prompt_category(console)
        if not short:
            break
        cat_entry = {"short": short}
        if caseid:
            cat_entry["caseid"] = caseid
        categories[name] = cat_entry
        console.print()

    if categories:
        config["categories"] = categories

    # Save config with comments/whitespace preserved if possible
    if YAML is not None:
        try:
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    data = yaml_ruamel.load(f)
                if data is None:
                    data = {}
            else:
                data = {}
            data.update(config)
            with open(config_path, "w") as f:
                yaml_ruamel.dump(data, f)
        except Exception as e:
            console.print(f"[red]Failed to save config with formatting: {e}[/red]")
            with open(config_path, "w") as f:
                yaml.dump(config, f, sort_keys=False, allow_unicode=True)
    else:
        with open(config_path, "w") as f:
            yaml.dump(config, f, sort_keys=False, allow_unicode=True)

    console.print(f"[bold green]Configuration saved to {config_path}[/bold green]")

def handle_start(args, tasks_file):
    start_dt = args.time
    write_task(tasks_file, "", "start", "", start_dt)

def handle_help(parser):
    parser.print_help()

def get_category_by_short(categories, arg_str):
    for cat in categories:
        if categories[cat]['short'] == arg_str:
            return cat, categories[cat]
    return None, None

def handle_add(_args, task: TaskEntry, tasks_file, console):
    try:
        write_task(tasks_file, task.category, task.task, task.notes, task.finish)
        console.print(f"✓ Task logged: {task.category} : {task.task} : {task.notes}", style="bold green")
    except ValueError as e:
        console.print(f"✗ Error saving task: {e}", style="bold red")
        sys.exit(1)

def print_report(report):
    """
    Pretty-print the report dictionary using a rich Tree.
    The report dict should be in the format:
      {category: {"tasks": [(task, duration)], "total": timedelta}} if collapsed,
      or {category: {"tasks": [(task, notes, duration)], "total": timedelta}} if not collapsed.
    Also prints the sum of all durations at the bottom.
    """
    console = Console()
    tree = Tree("Task Report")

    # Find max length for left part (task or task | notes)
    max_left_len = 0
    for cat_data in report.values():
        for entry in cat_data["tasks"]:
            if len(entry) == 2:
                task = entry[0]
                left = f"{task}"
            elif len(entry) == 3:
                task, notes, _ = entry
                left = f"{task}"
                if notes:
                    left += f" | {notes}"
            max_left_len = max(max_left_len, len(left))

    total_duration = timedelta(0)

    for category, cat_data in report.items():
        # Format category total
        cat_total = cat_data["total"]
        cat_total_mins = int(cat_total.total_seconds() // 60)
        cat_hours = int(cat_total.total_seconds() // 3600)
        cat_mins = int((cat_total.total_seconds() % 3600) // 60)
        cat_total_str = f"{cat_hours}:{cat_mins:02d}"
        cat_node = tree.add(f"[bold]{category}[/bold] [dim]{cat_total_str} ({cat_total_mins} min)[/dim]")
        
        for entry in cat_data["tasks"]:
            if len(entry) == 2:
                # collapsed: (task, duration)
                task, duration = entry
                minutes = int(duration.total_seconds() // 60)
                left = f"{task}"
            elif len(entry) == 3:
                # not collapsed: (task, notes, duration)
                task, notes, duration = entry
                minutes = int(duration.total_seconds() // 60)
                left = f"{task}"
                if notes:
                    left += f" | {notes}"
            # Pad left part so all durations align
            # Format duration as H:MM (no seconds, no days)
            hours = int(duration.total_seconds() // 3600)
            minutes = int((duration.total_seconds() % 3600) // 60)
            duration_str = f"{hours}:{minutes:02d}"
            line = f"{left.ljust(max_left_len)} {duration_str.rjust(10)} ({minutes + hours*60} min)"
            cat_node.add(line)
            total_duration += duration

    total_minutes = int(total_duration.total_seconds() // 60)
    total_hours = int(total_duration.total_seconds() // 3600)
    remainder_minutes = total_minutes % 60
    # Print total as H:MM (not days)
    total_str = f"{total_hours}:{remainder_minutes:02d}"
    tree.add(f"[bold yellow]Total: {total_str.rjust(max_left_len+8)} ({total_minutes} min)[/bold yellow]")

    console.print(tree)

def handle_report(args, tasks_file, console):
    console.print(f"From: {getattr(args, 'from_')} To: {args.to}", style="bold blue")
    try:
        report = generate_report(tasks_file, getattr(args, 'from_'), args.to)
        print_report(report)
    except ValueError as e:
        console.print(f"Error generating report: {e}", style="bold red")

def handle_export(args, tasks_file, console):
    exported_content = None
    if args.format == "json":
        exported_content = export_json(tasks_file, getattr(args, 'from_'), args.to)
    elif args.format == "csv":
        exported_content = export_csv(tasks_file, getattr(args, 'from_'), args.to)
    if args.output:
        with open(args.output, "w") as f:
            f.write(exported_content)
        console.print(f"Exported to {args.output}", style="bold green")
    else:
        console.print(exported_content)

def handle_login(args, config, console):
    try:
        login_to_site(config, args.verbose)
    except (BrowserInstallError, BrowserLaunchError, BrowserSelectionError, LoginInterruptedError, MissingTimecardsUrl) as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        sys.exit(1)

def show_timecards_table(timecards, title="Timecards for submission"):
    """
    Display the timecards in a table format using rich.
    """
    from rich.table import Table
    from rich.console import Console
    
    console = Console()
    table = Table(title=title, show_footer=True)

    table.add_column("Case no.", justify="center", style="cyan")
    table.add_column("Task", justify="left", style="magenta", max_width=50, no_wrap=True)
    table.add_column("Work performed", justify="left", style="green", max_width=50, no_wrap=True)
    table.add_column("Minutes", justify="right", style="yellow")
    table.add_column("Start time", justify="right", style="blue")

    total_minutes = 0
    for tc in timecards:
        minutes = int(getattr(tc, "minutes", 0) or 0)
        total_minutes += minutes
        table.add_row(
            str(getattr(tc, "case_no", "")),
            str(getattr(tc, "desc", "")),
            str(getattr(tc, "work_performed", "")),
            str(minutes),
            datetime.combine(
                getattr(tc, "start_date"), getattr(tc, "start_time")
            ).strftime("%Y-%m-%d %H:%M"),
        )

    # Footer: label + total in the Minutes column
    table.columns[2].footer = "Total"
    table.columns[3].footer = str(total_minutes)

    console.print(table)

def _get_progress_path():
    config_dir = os.path.dirname(get_config_path())
    return os.path.join(config_dir, "submit_progress.json")

def _load_progress():
    path = _get_progress_path()
    if not os.path.exists(path):
        return set()
    try:
        with open(path) as f:
            progress = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        print(
            f"Warning: ignoring invalid submit progress file at {path}: {e}",
            file=sys.stderr,
        )
        return set()

    if not isinstance(progress, dict):
        print(
            f"Warning: ignoring invalid submit progress file at {path}: expected an object",
            file=sys.stderr,
        )
        return set()

    submitted = progress.get("submitted", [])
    if not isinstance(submitted, list) or not all(isinstance(item, str) for item in submitted):
        print(
            f"Warning: ignoring invalid submit progress file at {path}: expected 'submitted' to be a list of strings",
            file=sys.stderr,
        )
        return set()

    return set(submitted)

def _save_progress(submitted_ids):
    path = _get_progress_path()
    dir_name = os.path.dirname(path)
    # Write to a temporary file in the same directory, then atomically replace
    with tempfile.NamedTemporaryFile(mode='w', dir=dir_name, delete=False, suffix=_PROGRESS_TEMP_SUFFIX, encoding='utf-8') as f:
        json.dump({"submitted": list(submitted_ids)}, f)
        f.flush()
        os.fsync(f.fileno())
        temp_path = f.name
    # Atomic replace (works on both POSIX and Windows as of Python 3.3)
    try:
        os.replace(temp_path, path)
    except OSError:
        # Clean up temp file if replace fails
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise

def _clear_progress():
    path = _get_progress_path()
    if os.path.exists(path):
        os.remove(path)

def handle_submit(args, config, tasks_file, console):
    try:
        if args.interactive:
            args.headed = True  # --interactive implies --headed

        timecards = []
        try:
            timecards = get_timecards(config, tasks_file, getattr(args, 'from_'), args.to)
        except AuthFileNotFoundError as e:
            console.print("[red]Auth info file not found. Please login first using the 'login' command.[/red]")
            return
        except NoCaseMappingError as e:
            console.print(f"[red]{e}[/red]")
            return

        if not timecards or len(timecards) == 0:
            console.print("No timecards found for submission.", style="bold red")
            return

        resume = getattr(args, 'resume', False)
        submitted_ids = _load_progress() if resume else set()
    
        if resume and submitted_ids:
            pending = [tc for tc in timecards if timecard_id(tc) not in submitted_ids]
          
            skipped = len(timecards) - len(pending)
            if not pending:
                console.print("[green]All timecards already submitted.[/green]")
                _clear_progress()
                return
            console.print(f"[cyan]Resuming: skipping {skipped} already submitted entr{'y' if skipped == 1 else 'ies'}.[/cyan]")
            timecards = pending

        no_case_entries = [tc for tc in timecards if getattr(tc, "case_no") is None]

        if no_case_entries:
            show_timecards_table(no_case_entries, title="Entries with missing case numbers (won't be submitted)")

        timecards = [tc for tc in timecards if getattr(tc, "case_no") is not None]

        if not timecards:
            console.print("No timecards with case numbers to submit.", style="bold red")
            return

        show_timecards_table(timecards)

        suffix = DRY_RUN_SUFFIX if args.dry_run else ""
        proceed = console.input(f"Proceed with submission?{suffix} (y/N): ").strip().lower()
        if proceed != "y":
            console.print("Submission cancelled.", style="bold yellow")
            return

        def on_progress(tc_id):
            submitted_ids.add(tc_id)
            _save_progress(submitted_ids)

        submission_completed = submit_timecards(
            config,
            timecards,
            headless=not args.headed,
            interactive=args.interactive,
            dry_run=args.dry_run,
            verbose=args.verbose,
            sleep=args.sleep,
            on_progress=on_progress if not args.dry_run else None,
        )

        if not args.dry_run and submission_completed:
            _clear_progress()

    except SubmitInterruptedError as e:
        console.print(f"[red]{e}[/red]")
        if not args.dry_run and submitted_ids:
            console.print("[yellow]Rerun the original submit command with '--resume', keeping the same date options, to skip entries already submitted.[/yellow]")
        sys.exit(1)
    except (BrowserInstallError, BrowserLaunchError, BrowserSelectionError, LoginInterruptedError, MissingTimecardsUrl) as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        sys.exit(1)


def handle_tutorial(console: Console):
    """
    Interactive tutorial walking users through punch features.
    """
    console.print("\n")
    console.print("[bold cyan]Welcome to Punch! 👊[/bold cyan]")
    console.print("[bold cyan]A CLI tool for tracking your tasks and time[/bold cyan]\n")

    sections = [
        {
            "title": "🚀 Getting Started",
            "content": [
                "Punch helps you track tasks and generate timecards. The basic workflow is:",
                "  1. Mark the start of your day: [cyan]punch start[/cyan]",
                "  2. Add tasks as you work: [cyan]punch add[/cyan]",
                "  3. View your work: [cyan]punch report[/cyan]",
                "  4. Export and submit: [cyan]punch export[/cyan] and [cyan]punch submit[/cyan]",
            ]
        },
        {
            "title": "📋 Adding Tasks",
            "content": [
                "Add a task by running: [cyan]punch add[/cyan]",
                "",
                "You can specify details inline:",
                "  [cyan]punch add dev : Fix login bug : Added authentication[/cyan]",
                "",
                "Or use interactive mode (just type [cyan]punch add[/cyan] with no args).",
                "",
                "Format: [yellow]punch add [category] : [task name] : [optional notes][/yellow]",
                "",
                "Categories come from your config (run [cyan]punch config wizard[/cyan]).",
                "Example categories: [cyan]dev[/cyan], [cyan]mtg[/cyan] (meeting), [cyan]admin[/cyan]",
            ]
        },
        {
            "title": "⏰ Specifying Times",
            "content": [
                "Most commands accept a [cyan]-t HH:MM[/cyan] flag to set the start time.",
                "",
                "Examples:",
                "  [cyan]punch start -t 09:00[/cyan]      # Mark day start at 9:00 AM",
                "  [cyan]punch add -t 10:30 dev : Task[/cyan]  # Log task at 10:30 AM",
                "",
                "If you don't specify a time, punch uses the current time.",
            ]
        },
        {
            "title": "📊 Viewing Your Work",
            "content": [
                "Get a report of your tasks:",
                "  [cyan]punch report[/cyan]                    # Today's report",
                "  [cyan]punch report -d yesterday[/cyan]      # Yesterday's report",
                "  [cyan]punch report -d monday[/cyan]        # Last Monday's report",
                "  [cyan]punch report -f 2025-01-01 -t 2025-01-31[/cyan]  # Date range",
                "",
                "Natural language dates work: yesterday, today, monday, tuesday, etc.",
            ]
        },
        {
            "title": "💾 Exporting Data",
            "content": [
                "Export your tasks in different formats:",
                "  [cyan]punch export --format json[/cyan]      # JSON format (default)",
                "  [cyan]punch export --format csv[/cyan]       # CSV format",
                "  [cyan]punch export -d today -o tasks.json[/cyan]  # Save to file",
                "  [cyan]punch export -f 2025-01-01 -t 2025-01-31[/cyan]  # Date range",
            ]
        },
        {
            "title": "🏷️ Excluding Time from Reports",
            "content": [
                "Mark tasks that shouldn't count as work by ending with [cyan]**[/cyan]:",
                "  [cyan]punch add admin : lunch**[/cyan]",
                "  [cyan]punch add admin : break**[/cyan]",
                "",
                "These tasks will be excluded from reports and timecard calculations.",
            ]
        },
        {
            "title": "📅 Weekly Editor",
            "content": [
                "Use the interactive weekly editor for a full-week view:",
                "  [cyan]punch weeklyeditor[/cyan]",
                "",
                "Navigate with arrow keys, add tasks with [cyan]a[/cyan], edit with [cyan]e[/cyan],",
                "delete with [cyan]d[/cyan], and quit with [cyan]q[/cyan].",
            ]
        },
        {
            "title": "⚙️ Configuration",
            "content": [
                "Set up your categories and preferences:",
                "  [cyan]punch config wizard[/cyan]        # Interactive setup",
                "  [cyan]punch config show[/cyan]         # View current config",
                "  [cyan]punch config set key value[/cyan] # Set a value",
                "  [cyan]punch config edit[/cyan]         # Edit in $EDITOR",
                "  [cyan]punch config path[/cyan]         # Show config file path",
            ]
        },
        {
            "title": "🔐 Salesforce Integration",
            "content": [
                "Connect to Salesforce and submit timecards:",
                "  [cyan]punch login[/cyan]              # Log in and store credentials",
                "  [cyan]punch submit[/cyan]            # Submit today's timecard",
                "  [cyan]punch submit -d yesterday[/cyan] # Submit yesterday's timecard",
                "  [cyan]punch submit -n[/cyan]          # Dry-run (preview without submitting)",
                "  [cyan]punch submit -f DATE -t DATE[/cyan]  # Submit date range",
            ]
        },
        {
            "title": "💡 Pro Tips",
            "content": [
                "• Use [cyan]punch[/cyan] with no args to enter interactive task-add mode",
                "• Press Tab to autocomplete commands and options",
                "• Bash/Zsh completion scripts are available:",
                "    source punch-completion.bash  # or zsh-completion",
                "• Run [cyan]punch help[/cyan] to see all commands",
                "• Run [cyan]punch help COMMAND[/cyan] for detailed command help",
                "• View changelog: [cyan]punch whats-new[/cyan]",
            ]
        },
        {
            "title": "📚 Common Workflows",
            "content": [
                "[yellow]Daily workflow:[/yellow]",
                "  1. [cyan]punch start[/cyan] (mark day start)",
                "  2. [cyan]punch add[/cyan] (add tasks throughout the day)",
                "  3. [cyan]punch report[/cyan] (verify your day)",
                "",
                "[yellow]Weekly workflow:[/yellow]",
                "  1. Use [cyan]punch weeklyeditor[/cyan] to review/edit entire week",
                "  2. [cyan]punch export -f MONDAY -t FRIDAY[/cyan] to export week",
                "  3. [cyan]punch submit -f MONDAY -t FRIDAY[/cyan] to submit",
            ]
        },
    ]

    for i, section in enumerate(sections):
        console.print(f"\n[bold yellow]{section['title']}[/bold yellow]")
        console.print("[dim]" + "─" * 50 + "[/dim]")
        for line in section['content']:
            console.print(line)
        
        if i < len(sections) - 1:
            prompt = console.input("\nPress Enter to continue (or type 'q' to quit): ").strip().lower()
            if prompt == 'q':
                console.print("\n[dim]Exiting tutorial. Happy tracking! 👊[/dim]\n")
                return

    console.print("\n[bold cyan]You've completed the tutorial![/bold cyan]")
    console.print("[dim]Now try these commands to get started:[/dim]")
    console.print("  1. [cyan]punch config wizard[/cyan]  - Set up your categories")
    console.print("  2. [cyan]punch add[/cyan]           - Add your first task")
    console.print("  3. [cyan]punch report[/cyan]        - View your work\n")
