"""Nova ChatScreen — main screen layout with chat, status, and input."""

from textual.app import ComposeResult
from textual.screen import Screen

from nova.tui.widgets.chat_panel import ChatPanel
from nova.tui.widgets.input_bar import InputBar
from nova.tui.widgets.status_bar import StatusBar


class ChatScreen(Screen):
    """Main chat screen — adapts to terminal theme."""

    CSS = """
    ChatScreen {
        layout: vertical;
    }
    #chat-panel {
        height: 1fr;
    }
    #status-bar {
        height: 1;
    }
    #input-bar {
        height: 3;
    }
    """

    def compose(self) -> ComposeResult:
        yield ChatPanel(id="chat-panel")
        yield StatusBar(id="status-bar")
        yield InputBar(id="input-bar")