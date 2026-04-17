"""Nova gateway — multi-platform messaging channels.

Supports Telegram, Discord, and a simple HTTP webhook.
Each channel runs as an independent process connecting to the shared NovaAgent.
"""

import asyncio
import json
import os
import re
import queue
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nova.main import NovaAgent


# ── Shared utilities ──

TAG_PATS = [r'<' + t + r'>.*?</' + t + r'>' for t in ('thinking', 'summary', 'tool_use', 'file_content')]
HELP_TEXT = """Commands:
/help - Show help
/stop - Abort current task
/status - Agent status
/new - Clear conversation context
/llm [n] - List or switch LLM models"""


def clean_reply(text):
    for pat in TAG_PATS:
        text = re.sub(pat, '', text or '', flags=re.DOTALL)
    return re.sub(r'\n{3,}', '\n\n', text).strip() or '...'


def extract_files(text):
    return re.findall(r'\[FILE:([^\]]+)\]', text or '')


def strip_files(text):
    return re.sub(r'\[FILE:[^\]]+\]', '', text or '').strip()


def build_done_text(raw_text):
    files = [p for p in extract_files(raw_text) if os.path.exists(p)]
    body = strip_files(clean_reply(raw_text))
    if files:
        body = (body + '\n\n' if body else '') + '\n'.join(f'Generated: {p}' for p in files)
    return body or '...'


def ensure_single_instance(port, label):
    try:
        lock_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lock_sock.bind(('127.0.0.1', port))
        return lock_sock
    except OSError:
        print(f'[{label}] Another instance already running.')
        sys.exit(1)


def create_agent(project_root=None):
    """Create and start the shared agent instance."""
    agent = NovaAgent(project_root=project_root)
    agent.verbose = False
    threading.Thread(target=agent.run, daemon=True).start()
    return agent