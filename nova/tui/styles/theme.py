"""Nova CLI theme — Rich color mapping that auto-adapts to terminal.

Rich auto-detects terminal capabilities, so ANSI named colors (cyan, green,
red, etc.) work in any terminal without configuration. Override with
NOVA_THEME=nova-dark/nova-light for specific hex color palettes.
"""

import os

# ANSI color mapping for Rich markup — auto mode (default).
# These work in any terminal — Rich resolves them based on terminal capabilities.
AUTO_ANSI_COLORS = {
    "user-msg": "cyan",
    "agent-msg": "white",
    "tool-name": "cyan",
    "success": "green",
    "error": "red",
    "warning": "yellow",
    "text-muted": "grey50",
    "primary": "cyan",
    "accent": "magenta",
}

# Override themes — Rich-compatible hex colors for specific palettes.
OVERRIDE_THEMES = {
    "nova-dark": {
        "user-msg": "#89dceb",
        "agent-msg": "#cdd6f4",
        "tool-name": "#cba6f7",
        "success": "#a6e3a1",
        "error": "#f38ba8",
        "warning": "#fab387",
        "text-muted": "#6c7086",
        "primary": "#89b4fa",
        "accent": "#cba6f7",
    },
    "nova-light": {
        "user-msg": "#04a5e5",
        "agent-msg": "#4c4f69",
        "tool-name": "#7287fd",
        "success": "#40a02b",
        "error": "#d20f39",
        "warning": "#fe640b",
        "text-muted": "#9ca0b0",
        "primary": "#1e66f5",
        "accent": "#7287fd",
    },
}


def get_color(token_name):
    """Get a Rich-compatible color value.

    Auto mode: ANSI named colors that adapt to terminal capabilities.
    Override mode (NOVA_THEME): hex colors for a specific palette.
    """
    if os.environ.get("NOVA_THEME", "auto") == "auto":
        return AUTO_ANSI_COLORS.get(token_name, "white")
    theme_name = os.environ.get("NOVA_THEME", "nova-dark")
    theme = OVERRIDE_THEMES.get(theme_name, OVERRIDE_THEMES["nova-dark"])
    return theme.get(token_name, "white")