"""Nova TUI theme system — adapts to terminal theme automatically.

Auto mode (default): uses Textual's built-in semantic color tokens ($background,
$text, $surface, etc.) which auto-adapt to the terminal's light/dark theme.
Custom aliases ($input-bg, $user-msg) map to Textual built-ins.

Override mode (NOVA_THEME=nova-dark/nova-light): replaces all tokens with
hardcoded hex colors for a specific look.
"""

import os

# Custom aliases only — these map to Textual built-in tokens in auto mode.
# Textual's own tokens ($background, $text, $surface, $panel, $primary,
# $success, $error, $warning, $text-muted, $border) are used directly in CSS
# and should NOT be overridden in auto mode.
CUSTOM_TOKENS = {
    "input-bg": "$panel",
    "input-focus": "$primary",
    "user-msg": "$primary",
    "agent-msg": "$text",
    "tool-name": "$primary",
}

# Override themes — only used when NOVA_THEME is explicitly set
OVERRIDE_THEMES = {
    "nova-dark": {
        "background": "#1e1e2e",
        "surface": "#313244",
        "panel": "#242436",
        "text": "#cdd6f4",
        "text-muted": "#6c7086",
        "primary": "#89b4fa",
        "secondary": "#a6adc8",
        "accent": "#cba6f7",
        "success": "#a6e3a1",
        "error": "#f38ba8",
        "warning": "#fab387",
        "border": "#45475a",
        "foreground": "#cdd6f4",
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
        "text": "#4c4f69",
        "text-muted": "#9ca0b0",
        "primary": "#1e66f5",
        "secondary": "#7287fd",
        "accent": "#7287fd",
        "success": "#40a02b",
        "error": "#d20f39",
        "warning": "#fe640b",
        "border": "#bcc0cc",
        "foreground": "#4c4f69",
        "input-bg": "#dce0e8",
        "input-focus": "#1e66f5",
        "user-msg": "#04a5e5",
        "agent-msg": "#4c4f69",
        "tool-name": "#7287fd",
    },
}


def is_auto_theme():
    """Check if auto-detect mode is active (default)."""
    theme_name = os.environ.get("NOVA_THEME", "auto")
    return theme_name == "auto"


def get_theme():
    """Get theme dict — custom aliases in auto, hex overrides in explicit mode."""
    theme_name = os.environ.get("NOVA_THEME", "auto")
    if theme_name == "auto":
        return CUSTOM_TOKENS
    return OVERRIDE_THEMES.get(theme_name, OVERRIDE_THEMES["nova-dark"])


def get_theme_css():
    """Generate Textual CSS variables for the active theme.

    In auto mode: only define custom aliases ($input-bg, $user-msg, etc.)
    that reference Textual's built-in tokens. Textual's own tokens
    ($background, $text, $surface, etc.) are left untouched so they
    auto-adapt to the terminal.

    In override mode: define ALL tokens as hex colors, replacing
    Textual's defaults.
    """
    theme = get_theme()
    lines = []
    for key, value in theme.items():
        lines.append(f"${key}: {value};")
    return "\n".join(lines)


def get_color(token_name):
    """Get a color for Rich markup — ANSI named colors in auto, hex in override.

    Rich doesn't understand Textual $variables, so in auto mode we map
    semantic intent to ANSI named colors that work in any terminal.
    """
    theme = get_theme()
    value = theme.get(token_name)
    if value and value.startswith("$"):
        ANSI_MAP = {
            "$primary": "cyan",
            "$text": "white",
            "$text-muted": "grey50",
            "$success": "green",
            "$error": "red",
            "$warning": "yellow",
            "$accent": "magenta",
            "$panel": "grey15",
            "$surface": "grey11",
        }
        return ANSI_MAP.get(value, "white")
    return value or "white"