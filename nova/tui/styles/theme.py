"""Nova TUI theme system — adapts to terminal theme automatically.

Auto mode (default): widgets use Textual's built-in semantic tokens ($background,
$text, $surface, $panel, $primary, etc.) directly. No custom CSS variables needed —
Textual resolves these to match the terminal's light/dark theme.

Override mode (NOVA_THEME=nova-dark/nova-light): replaces all tokens with
hardcoded hex colors for a specific palette.
"""

import os

# Override themes — replace ALL Textual built-ins + custom tokens with hex.
OVERRIDE_THEMES = {
    "nova-dark": {
        "background": "#1e1e2e",
        "surface": "#313244",
        "panel": "#242436",
        "foreground": "#cdd6f4",
        "text": "#cdd6f4",
        "text-muted": "#6c7086",
        "primary": "#89b4fa",
        "secondary": "#a6adc8",
        "accent": "#cba6f7",
        "success": "#a6e3a1",
        "error": "#f38ba8",
        "warning": "#fab387",
        "border": "#45475a",
        "boost": "#cdd6f411",
        "input-bg": "#181825",
        "input-focus": "#89b4fa",
        "user-msg": "#89dceb",
        "agent-msg": "#cdd6f4",
        "tool-name": "#cba6f7",
    },
    "nova-light": {
        "background": "#eff1f5",
        "surface": "#e6e9ef",
        "panel": "#dce0e8",
        "foreground": "#4c4f69",
        "text": "#4c4f69",
        "text-muted": "#9ca0b0",
        "primary": "#1e66f5",
        "secondary": "#7287fd",
        "accent": "#7287fd",
        "success": "#40a02b",
        "error": "#d20f39",
        "warning": "#fe640b",
        "border": "#bcc0cc",
        "boost": "#4c4f6911",
        "input-bg": "#dce0e8",
        "input-focus": "#1e66f5",
        "user-msg": "#04a5e5",
        "agent-msg": "#4c4f69",
        "tool-name": "#7287fd",
    },
}

# ANSI color mapping for Rich markup in auto mode.
# Rich doesn't understand Textual CSS variables, so we map
# semantic intent to ANSI named colors that work in any terminal.
AUTO_ANSI_COLORS = {
    "user-msg": "cyan",
    "agent-msg": "white",
    "tool-name": "cyan",
    "success": "green",
    "error": "red",
    "warning": "yellow",
    "text-muted": "grey50",
}


def is_auto_theme():
    """Check if auto-detect mode is active (default)."""
    return os.environ.get("NOVA_THEME", "auto") == "auto"


def get_theme_css():
    """Generate Textual CSS variables.

    Auto mode: return empty — widgets use Textual built-in tokens directly.
    Override mode: define ALL tokens (built-ins + custom) as hex colors.
    """
    if is_auto_theme():
        return ""
    theme_name = os.environ.get("NOVA_THEME", "nova-dark")
    theme = OVERRIDE_THEMES.get(theme_name, OVERRIDE_THEMES["nova-dark"])
    lines = []
    for key, value in theme.items():
        lines.append(f"${key}: {value};")
    return "\n".join(lines)


def get_color(token_name):
    """Get a color for Rich markup.

    Auto mode: ANSI named colors that work in any terminal.
    Override mode: hex colors from the theme.
    """
    if is_auto_theme():
        return AUTO_ANSI_COLORS.get(token_name, "white")
    theme_name = os.environ.get("NOVA_THEME", "nova-dark")
    theme = OVERRIDE_THEMES.get(theme_name, OVERRIDE_THEMES["nova-dark"])
    return theme.get(token_name, "white")