"""ChatPanel — scrollable markdown-rendered chat history."""

from rich.markdown import Markdown
from textual.widgets import RichLog

from nova.tui.styles.theme import get_color


class ChatPanel(RichLog):
    """Scrollable chat history with markdown rendering."""

    DEFAULT_CSS = """
    ChatPanel {
        background: $surface;
        color: $text;
        border: none;
        scrollbar-size: 1 1;
        padding: 1 2;
    }
    """

    def add_user_message(self, text: str) -> None:
        user_color = get_color("user-msg")
        prefix = f"[bold {user_color}]You:[/] "
        self.write(f"{prefix}{text}")

    def add_agent_response(self, text: str) -> None:
        md = Markdown(text)
        self.write(md)

    def add_tool_indicator(self, name: str, summary: str, status: str = "done") -> None:
        tool_color = get_color("tool-name")
        success_color = get_color("success")
        warning_color = get_color("warning")
        error_color = get_color("error")
        if status == "success":
            icon = f"[bold {success_color}]✓[/]"
        elif status == "done":
            icon = f"[bold {warning_color}]●[/]"
        else:
            icon = f"[bold {error_color}]✗[/]"
        tool_label = f"[{tool_color}]{name}[/]"
        self.write(f"  {tool_label} {summary[:60]} {icon}")

    def add_error(self, text: str) -> None:
        error_color = get_color("error")
        self.write(f"[bold {error_color}]Error:[/] {text}")

    def add_status(self, text: str) -> None:
        muted_color = get_color("text-muted")
        self.write(f"[{muted_color}]{text}[/]")