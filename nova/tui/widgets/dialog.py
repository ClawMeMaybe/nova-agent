"""AskUserDialog — modal dialog for human-in-the-loop questions."""

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class AskUserDialog(ModalScreen):
    """Modal overlay for ask_user questions."""

    CSS = """
    AskUserDialog {
        align: center middle;
    }
    #dialog-container {
        width: 60;
        height: auto;
        max-height: 20;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    #question-text {
        width: 100%;
        height: auto;
        max-height: 10;
        color: $text;
        margin-bottom: 1;
    }
    #button-container {
        width: 100%;
        height: auto;
        layout: horizontal;
    }
    #button-container Button {
        margin: 0 1;
    }
    """

    BINDINGS = [("escape", "dismiss", "Dismiss")]

    def __init__(self, question: str, candidates: list = None, handler=None):
        super().__init__()
        self.question = question
        self.candidates = candidates or []
        self.handler = handler

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-container"):
            yield Static(self.question, id="question-text")
            with Vertical(id="button-container"):
                for candidate in self.candidates:
                    yield Button(candidate, variant="primary", classes="candidate-btn")
                if not self.candidates:
                    yield Button("Continue", variant="primary", classes="default-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        answer = event.button.label.plain
        if self.handler:
            self.handler._ask_response_queue.put(answer)
        self.dismiss(answer)

    def action_dismiss(self) -> None:
        if self.handler:
            self.handler._ask_response_queue.put("__timeout__")
        self.dismiss("__timeout__")