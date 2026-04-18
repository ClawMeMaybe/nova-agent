"""ToolIndicator — compact per-tool status line widget."""

from textual.widgets import Static

from nova.tui.styles.theme import get_theme


class ToolIndicator(Static):
    """Compact tool status line: tool_name summary ✓."""

    DEFAULT_CSS = """
    ToolIndicator {
        color: #6c7086;
        height: 1;
        padding: 0 2;
    }
    """

    def set_status(self, name: str, summary: str, status: str = "success") -> None:
        theme = get_theme()
        icon = "✓" if status == "success" else "✗" if status == "error" else "●"
        icon_color = theme["success"] if status == "success" else theme["error"] if status == "error" else theme["warning"]
        self.update(f"  [{theme['tool-name']}]{name}[/] {summary[:60]} [{icon_color}]{icon}[/]")