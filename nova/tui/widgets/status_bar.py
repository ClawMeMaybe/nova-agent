"""StatusBar — bottom bar with model name, spinner, and tool indicators."""

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static, LoadingIndicator


class StatusBar(Horizontal):
    """Bottom status bar showing model, spinner, and tool activity."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $surface;
        color: $text-muted;
        border-top: solid $border;
        padding: 0 1;
    }
    #model-label {
        width: auto;
        color: $text-muted;
    }
    #spinner-area {
        width: 3;
    }
    #tool-status {
        width: 1fr;
        color: $text-muted;
    }
    """

    _tool_count = 0

    def compose(self) -> ComposeResult:
        yield Static(id="model-label")
        yield LoadingIndicator(id="spinner-area")
        yield Static(id="tool-status")

    def on_mount(self) -> None:
        self._set_model()
        self._set_idle()

    def _set_model(self) -> None:
        agent = self.app.agent
        model_name = getattr(agent.client.backend, 'name', 'unknown')
        self.query_one("#model-label", Static).update(f" {model_name}")

    def _set_idle(self) -> None:
        self.query_one("#spinner-area", LoadingIndicator).display = False
        self.query_one("#tool-status", Static).update("")

    def set_thinking(self) -> None:
        self.query_one("#spinner-area", LoadingIndicator).display = True
        self.query_one("#tool-status", Static).update(" Thinking...")

    def set_done(self) -> None:
        self.query_one("#spinner-area", LoadingIndicator).display = False
        if self._tool_count > 0:
            self.query_one("#tool-status", Static).update(f" {self._tool_count} tools completed")
        else:
            self.query_one("#tool-status", Static).update("")

    def add_tool(self, name: str, summary: str) -> None:
        self._tool_count += 1
        self.query_one("#tool-status", Static).update(f" {name} {summary[:40]}...")

    def reset_tool_count(self) -> None:
        self._tool_count = 0