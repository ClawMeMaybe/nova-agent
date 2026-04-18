"""NovaApp — Textual TUI application for Nova Agent."""

import logging
import threading

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import RichLog

from nova.events import AgentEvent
from nova.tui.screens.chat import ChatScreen
from nova.tui.styles.theme import get_theme_css
from nova.tui.widgets.dialog import AskUserDialog

logger = logging.getLogger("nova")


class NovaApp(App):
    """Nova Agent TUI — adapts to your terminal theme."""

    TITLE = "Nova Agent"
    SUB_TITLE = "Self-evolving AI assistant"

    CSS = """
    Screen {
        background: $background;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("ctrl+c", "abort", "Abort", show=True),
    ]

    def __init__(self, agent):
        super().__init__()
        self.agent = agent
        self._poll_handle = None

    def compose(self) -> ComposeResult:
        yield ChatScreen()

    def on_mount(self) -> None:
        # Write a visible test message FIRST to verify rendering works
        chat = self.query_one("#chat-panel", RichLog)
        chat.write("Nova Agent starting...")

        # Start agent background thread
        threading.Thread(target=self.agent.run, daemon=True).start()
        self.agent.cron.start()
        self.agent.autonomous.start()

        # Start event polling (100ms interval)
        self._poll_handle = self.set_interval(0.1, self._poll_events)

        # Show startup message
        from nova import __version__
        stats = self.agent.memory.stats()
        startup_msg = f"""**Nova Agent v{__version__}** — Self-evolving AI assistant

- Local: {stats['local_wiki_pages']} wiki | {stats['local_facts']} facts | {stats['local_skills']} skills
- Global: {stats['global_wiki_pages']} wiki | {stats['global_facts']} facts | {stats['global_skills']} skills

Type your message, or `/help` for commands."""
        chat.add_agent_response(startup_msg)

    def _poll_events(self) -> None:
        """Process all queued events from the agent."""
        events_bus = self.agent.events
        events = events_bus.poll()
        if not events:
            return

        chat = self.query_one("#chat-panel")
        status = self.query_one("#status-bar")

        for event in events:
            etype = event["type"]
            data = event["data"]

            if etype == AgentEvent.AGENT_THINKING:
                status.set_thinking()

            elif etype == AgentEvent.TOOL_CALL:
                name = data.get("name", "?")
                summary = data.get("summary", "")
                if name == "code_run":
                    pass
                elif summary.startswith("{"):
                    try:
                        import json
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
                chat.add_tool_indicator(name, summary, "running")
                status.add_tool(name, summary)

            elif etype == AgentEvent.TOOL_RESULT:
                name = data.get("name", "?")
                summary = data.get("summary", "")
                status_val = data.get("status", "done")
                chat.add_tool_indicator(name, summary, status_val)

            elif etype == AgentEvent.AGENT_RESPONSE:
                if data and isinstance(data, str) and data.strip():
                    chat.add_agent_response(data)

            elif etype == AgentEvent.AGENT_DONE:
                status.set_done()

            elif etype == AgentEvent.ERROR:
                chat.add_error(str(data) if data else "Unknown error")
                status.set_done()

            elif etype == AgentEvent.ASK_USER:
                question = data.get("question", "Please provide input:")
                candidates = data.get("candidates", [])
                handler = self.agent.handler
                self.push_screen(AskUserDialog(question, candidates, handler=handler))

            elif etype == AgentEvent.STATUS:
                chat.add_status(str(data) if data else "")

    def action_abort(self) -> None:
        """Abort current agent task."""
        self.agent.abort()
        chat = self.query_one("#chat-panel")
        chat.add_status("Aborting current task...")

    def on_unmount(self) -> None:
        """Cleanup on exit."""
        if self._poll_handle:
            self._poll_handle.stop()