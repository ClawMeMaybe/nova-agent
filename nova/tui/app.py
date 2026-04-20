"""NovaApp — Rich + prompt_toolkit inline REPL for Nova Agent.

No alternate screen buffer, no full-screen takeover.
Content prints inline, just like aider/claude-code/opencode.
"""

import json
import logging
import os
import queue
import threading

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory

from nova import __version__
from nova.events import AgentEvent, EventBus
from nova.main import build_learn_prompt
from nova.brainstorm import build_brainstorm_prompt
from nova.skill_parser import parse_skill_markdown
from nova.tui.styles.theme import get_color

logger = logging.getLogger("nova")

COMMANDS = {
    "/quit": "Exit Nova Agent",
    "/exit": "Exit Nova Agent",
    "/q": "Exit Nova Agent",
    "/stats": "Show memory statistics",
    "/wiki": "List wiki pages",
    "/cron": "List cron jobs",
    "/todo": "Show autonomous TODO",
    "/evolve": "Show evolution score",
    "/learn": "Learn about current project directory and generate knowledge",
    "/brainstorm": "Socratic interview with ambiguity scoring",
    "/skill-install": "Install a skill from .md file or URL",
    "/help": "Show available commands",
}


class NovaApp:
    """Rich + prompt_toolkit inline REPL — works in any terminal."""

    def __init__(self, agent):
        self.agent = agent
        self.console = Console()
        self._running = True
        self._event_buffer = []
        self._event_lock = threading.Lock()

    def run(self):
        """Main REPL loop."""
        # Start background threads
        threading.Thread(target=self.agent.run, daemon=True).start()
        self.agent.cron.start()
        self.agent.autonomous.start()

        # Register event listener
        self.agent.events.add_listener(self._on_event)

        # Show startup banner
        self._show_banner()

        # REPL loop with prompt_toolkit
        completer = WordCompleter(list(COMMANDS.keys()))
        session = PromptSession(
            history=InMemoryHistory(),
            completer=completer,
            multiline=False,
        )

        while self._running:
            try:
                text = session.prompt("nova> ")
            except KeyboardInterrupt:
                # Ctrl+C — abort current task
                if self.agent.is_running:
                    self.agent.abort()
                    self.console.print("[grey50]Aborting current task...[/]")
                continue
            except EOFError:
                # Ctrl+D — exit
                break

            text = text.strip()
            if not text:
                # Flush buffered events when idle
                self._flush_events()
                continue

            if text.startswith("/"):
                self._handle_command(text)
            else:
                self._send_message(text)

        self.console.print("[grey50]Goodbye![/]")

    def _show_banner(self):
        """Show startup banner with Rich formatting."""
        stats = self.agent.memory.stats()
        model_name = getattr(self.agent.client.backend, 'name', 'unknown')

        banner = Markdown(f"""**Nova Agent v{__version__}** — Self-evolving AI assistant

- Model: {model_name}
- Local: {stats['project_wiki_pages']} wiki | {stats['project_facts']} facts | {stats['project_skills']} skills
- Global: {stats['global_wiki_pages']} wiki | {stats['global_facts']} facts | {stats['global_skills']} skills

Type your message, or `/help` for commands.""")
        self.console.print(banner)
        self.console.print()

    def _on_event(self, event_type, data=None):
        """EventBus listener — buffer events, flush when input is idle."""
        with self._event_lock:
            self._event_buffer.append((event_type, data))

    def _flush_events(self):
        """Render all buffered events inline."""
        with self._event_lock:
            events = self._event_buffer[:]
            self._event_buffer.clear()

        for event_type, data in events:
            self._render_event(event_type, data)

    def _render_event(self, event_type, data):
        """Render a single event using Rich."""
        if event_type == AgentEvent.AGENT_RESPONSE:
            if data and isinstance(data, str) and data.strip():
                self.console.print(Markdown(data))

        elif event_type == AgentEvent.TOOL_CALL:
            if isinstance(data, dict):
                name = data.get("name", "?")
                summary = data.get("summary", "")
                if summary.startswith("{"):
                    try:
                        args_dict = json.loads(summary.rstrip("..."))
                        if "path" in args_dict:
                            summary = args_dict["path"]
                        elif "query" in args_dict:
                            summary = args_dict["query"][:40]
                        elif "code" in args_dict:
                            summary = args_dict["code"][:40]
                        elif "sql" in args_dict:
                            summary = args_dict["sql"][:40]
                        else:
                            summary = ""
                    except Exception:
                        summary = ""
                tool_color = get_color("tool-name")
                self.console.print(
                    f"  [{tool_color}]{name}[/] {summary[:60]}",
                    highlight=False
                )

        elif event_type == AgentEvent.TOOL_RESULT:
            if isinstance(data, dict):
                name = data.get("name", "?")
                summary = data.get("summary", "")
                status_val = data.get("status", "done")
                tool_color = get_color("tool-name")
                success_color = get_color("success")
                error_color = get_color("error")
                warning_color = get_color("warning")
                if status_val == "success":
                    icon = f"[bold {success_color}]✓[/]"
                elif status_val == "done":
                    icon = f"[bold {warning_color}]●[/]"
                else:
                    icon = f"[bold {error_color}]✗[/]"
                self.console.print(
                    f"  [{tool_color}]{name}[/] {summary[:60]} {icon}",
                    highlight=False
                )

        elif event_type == AgentEvent.AGENT_THINKING:
            muted = get_color("text-muted")
            self.console.print(f"[{muted}]Thinking...[/]", highlight=False)

        elif event_type == AgentEvent.AGENT_DONE:
            muted = get_color("text-muted")
            tool_count = 0
            if isinstance(data, dict):
                tool_count = data.get("tool_count", 0)
            if tool_count > 0:
                self.console.print(f"[{muted}]{tool_count} tools completed[/]", highlight=False)

        elif event_type == AgentEvent.ERROR:
            error_color = get_color("error")
            self.console.print(f"[bold {error_color}]Error:[/] {data}", highlight=False)

        elif event_type == AgentEvent.STATUS:
            muted = get_color("text-muted")
            self.console.print(f"[{muted}]{data}[/]", highlight=False)

        elif event_type == AgentEvent.ASK_USER:
            self._handle_ask_user(data)

    def _handle_ask_user(self, data):
        """Handle ask_user event with prompt_toolkit."""
        if not isinstance(data, dict):
            return
        question = data.get("question", "Please provide input:")
        candidates = data.get("candidates", [])

        self.console.print(f"\n[bold]{question}[/]")

        if candidates:
            for i, c in enumerate(candidates):
                self.console.print(f"  {i+1}. {c}")
            try:
                answer = PromptSession().prompt("Your answer: ")
                # Try to match numeric input to candidate
                try:
                    idx = int(answer) - 1
                    if 0 <= idx < len(candidates):
                        answer = candidates[idx]
                except ValueError:
                    pass
            except (EOFError, KeyboardInterrupt):
                answer = "__timeout__"
        else:
            try:
                answer = PromptSession().prompt("Your answer: ")
            except (EOFError, KeyboardInterrupt):
                answer = "__timeout__"

        handler = self.agent.handler
        if handler and hasattr(handler, '_ask_response_queue'):
            handler._ask_response_queue.put(answer)

    def _handle_command(self, cmd):
        """Handle /commands."""
        if cmd in ("/quit", "/exit", "/q"):
            self._running = False
            return

        if cmd == "/help":
            lines = Text()
            lines.append("Available commands:\n", style="bold")
            for k, v in COMMANDS.items():
                lines.append(f"  {k} ", style="cyan")
                lines.append(f"— {v}\n")
            self.console.print(lines)
            return

        if cmd == "/stats":
            stats = self.agent.memory.stats()
            self.console.print(f"[bold]Local:[/] Wiki: {stats['project_wiki_pages']} | Facts: {stats['project_facts']} | Skills: {stats['project_skills']} | Sessions: {stats['project_sessions']} | Trust: {stats['avg_trust']:.2f}")
            self.console.print(f"[bold]Global:[/] Wiki: {stats['global_wiki_pages']} | Facts: {stats['global_facts']} | Skills: {stats['global_skills']} | Sessions: {stats['global_sessions']} | Trust: {stats['avg_trust']:.2f}")
            return

        if cmd == "/wiki":
            pages = self.agent.memory.wiki_list()
            if not pages:
                self.console.print("[grey50]No wiki pages.[/]")
            for p in pages:
                tier = p.get('_tier', '?')
                self.console.print(f"  [{tier}] [{p['category']}] {p['title']} (tags: {p['tags']})")
            return

        if cmd == "/cron":
            from nova.cron.jobs import list_jobs
            jobs = list_jobs()
            if not jobs:
                self.console.print("[grey50]No cron jobs configured.[/]")
            else:
                for j in jobs:
                    status = "enabled" if j.get('enabled', True) else "disabled"
                    self.console.print(f"  {j['id']}: {j['name']} ({j['schedule']['kind']}) next={j['next_run_at']} runs={j['completed_count']} {status}")
            return

        if cmd == "/todo":
            todo = self.agent.memory.wiki_read('autonomous-todo', tier='global')
            if todo and todo.get('content'):
                self.console.print(Markdown(todo['content']))
            else:
                self.console.print("[grey50]No autonomous TODO yet. Agent will create one during idle self-improvement.[/]")
            return

        if cmd == "/evolve":
            score, trend = self.agent.memory.evolution_score()
            trend_arrow = '↑' if trend > 0 else '↓' if trend < 0 else '—'
            self.console.print(f"[bold]Evolution Score:[/] {score:.2f} (trend: {trend_arrow})")
            try:
                recent = self.agent.memory._local._conn.execute(
                    "SELECT loss_task, loss_efficiency, loss_recurrence, loss_knowledge_quality, "
                    "loss_total, evolution_score, created_at "
                    "FROM evolution_log ORDER BY created_at DESC LIMIT 5"
                ).fetchall()
                if recent:
                    for r in recent:
                        self.console.print(
                            f"  [{r['created_at'][:10]}] L_task={r['loss_task']:.1f} "
                            f"L_eff={r['loss_efficiency']:.2f} L_rec={r['loss_recurrence']:.2f} "
                            f"L_kq={r['loss_knowledge_quality']:.2f} L_total={r['loss_total']:.2f} "
                            f"score={r['evolution_score']:.2f}"
                        )
            except Exception:
                pass
            return

        if cmd.startswith("/brainstorm"):
            topic = cmd[len("/brainstorm"):].strip() or None
            if topic:
                self.console.print(f"[bold]Brainstorm:[/] {topic}")
            else:
                self.console.print("[bold]Brainstorm Mode[/] — Socratic interview with ambiguity scoring")
            brainstorm_prompt = build_brainstorm_prompt(topic)
            dq = self.agent.put_task(brainstorm_prompt, source="user")
            while True:
                self._flush_events()
                try:
                    item = dq.get(timeout=0.5)
                except queue.Empty:
                    if not self.agent.is_running:
                        break
                    continue
                if 'done' in item:
                    self.console.print("[bold]Brainstorm complete![/] Spec saved to memory.")
                    break
            return

        if cmd == "/learn":
            project_root = os.getcwd()
            project_name = os.path.basename(project_root)
            scan = self.agent.memory.project_scan(project_root, depth='standard')
            # Auto-create project scope
            try:
                pid = self.agent.memory.project_create(project_name, f"Auto-learned from {project_root}")
                self.agent.memory.project_select(pid)
                self.agent.current_project_id = pid
                self.console.print(f"[bold]Created project scope:[/] {project_name} (id={pid})")
            except Exception:
                projects = self.agent.memory.project_list()
                for p in projects:
                    if p['name'] == project_name:
                        self.agent.memory.project_select(p['id'])
                        self.agent.current_project_id = p['id']
                        break
                self.console.print(f"[bold]Using existing project scope:[/] {project_name}")
            learn_prompt = build_learn_prompt(scan)
            dq = self.agent.put_task(learn_prompt, source="user")
            # Wait for learn to complete
            while True:
                self._flush_events()
                try:
                    item = dq.get(timeout=0.5)
                except queue.Empty:
                    if not self.agent.is_running:
                        break
                    continue
                if 'done' in item:
                    stats = self.agent.memory.stats()
                    self.console.print(f"[bold]Sync complete![/] Facts: {stats['total_facts']} | Skills: {stats['total_skills']} | Wiki: {stats['total_wiki_pages']}")
                    break
            return
        if cmd.startswith("/skill-install"):
            source = cmd[len("/skill-install"):].strip()
            if not source:
                self.console.print("[bold]Usage:[/] /skill-install <file_path_or_url>")
                return

            # Local file — parse and install directly
            if not source.startswith(("http://", "https://")):
                path = os.path.expanduser(source)
                if not os.path.isfile(path):
                    self.console.print(f"[bold red]File not found:[/] {path}")
                    return
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()
                    parsed = parse_skill_markdown(content, filename=os.path.basename(path))
                    self.agent.memory.skill_add(
                        name=parsed["name"], description=parsed["description"],
                        steps=[], triggers=parsed["triggers"],
                        tags=parsed["tags"], contract=parsed["contract"],
                    )
                    self.console.print(f"[bold green]Skill installed:[/] {parsed['name']} ({len(parsed['contract'])} chars contract)")
                except Exception as e:
                    self.console.print(f"[bold red]Error:[/] {e}")
                return

            # URL — let the LLM agent handle it (any platform: GitHub, Clawhub, Skillhub, etc.)
            install_prompt = (
                f"Install skills from this URL: {source}\n\n"
                "Instructions:\n"
                "1. Use web_fetch or available tools to explore the URL and find skill .md files\n"
                "2. For each skill .md file found, fetch its full content\n"
                "3. Parse the content using the skill_parser: extract name, description, triggers, tags from YAML frontmatter, use the body as contract\n"
                "4. Install each skill into memory using skill_add with the parsed fields and contract=body_content\n"
                "5. Report which skills were installed with their names and contract sizes\n\n"
                "The URL might be a GitHub repo, Clawhub page, Skillhub link, or any other platform. "
                "Navigate the site structure to find skill definition files (.md files with behavioral contracts).\n"
            )
            dq = self.agent.put_task(install_prompt, source="user")
            self.console.print(f"[bold]Installing skills from:[/] {source}")
            while True:
                self._flush_events()
                try:
                    item = dq.get(timeout=0.5)
                except queue.Empty:
                    if not self.agent.is_running:
                        break
                    continue
                if 'done' in item:
                    self.console.print("[bold]Skill install complete.[/]")
                    break
            return

        user_color = get_color("user-msg")
        self.console.print(f"[bold {user_color}]You:[/] {cmd}")
        self.agent.put_task(cmd, source="user")

    def _send_message(self, text):
        """Send user message to agent and flush events as they arrive."""
        user_color = get_color("user-msg")
        self.console.print(f"[bold {user_color}]You:[/] {text}")

        # Submit task — we'll pick up events via the listener
        self.agent.put_task(text, source="user")

        # Wait for agent to finish, flushing events periodically
        while self.agent.is_running:
            self._flush_events()
            threading.Event().wait(0.1)

        # Final flush
        self._flush_events()