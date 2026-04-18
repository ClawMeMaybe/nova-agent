"""InputBar — rich input field with /command autocomplete."""

from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import Input
from textual import events

COMMANDS = {
    "/quit": "Exit Nova Agent",
    "/stats": "Show memory statistics",
    "/wiki": "List wiki pages",
    "/cron": "List cron jobs",
    "/todo": "Show autonomous TODO",
    "/evolve": "Show evolution score",
    "/help": "Show available commands",
}


class InputBar(Container):
    """Input field with /command autocomplete."""

    DEFAULT_CSS = """
    InputBar {
        layout: horizontal;
        background: $input-bg;
        border-top: solid $border;
        height: 3;
        padding: 0 1;
    }
    InputBar Input {
        width: 1fr;
        height: 1;
        background: $input-bg;
        border: none;
    }
    InputBar:focus-within Input {
        border: solid $input-focus;
    }
    """

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Type your message or /command...", id="msg-input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return

        input_widget = event.input
        input_widget.value = ""

        if value.startswith("/"):
            self._handle_command(value)
        else:
            self._send_message(value)

    def _handle_command(self, cmd: str) -> None:
        chat = self.app.query_one("#chat-panel")
        if cmd == "/quit":
            self.app.exit()
        elif cmd == "/help":
            lines = ["**Available commands:**"]
            for k, v in COMMANDS.items():
                lines.append(f"  `{k}` — {v}")
            chat.add_agent_response("\n".join(lines))
        elif cmd in COMMANDS:
            agent = self.app.agent
            agent.put_task(cmd, source="user")
            chat.add_user_message(cmd)

    def _send_message(self, text: str) -> None:
        chat = self.app.query_one("#chat-panel")
        agent = self.app.agent
        chat.add_user_message(text)
        agent.put_task(text, source="user")

    def on_key(self, event: events.Key) -> None:
        if event.key == "up" and event.character == "/":
            input_widget = self.query_one(Input)
            if not input_widget.value:
                input_widget.value = "/"