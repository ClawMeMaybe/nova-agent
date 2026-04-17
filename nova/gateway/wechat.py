"""WeChat channel for Nova Agent gateway.

Connects via personal WeChat account (itchat/gechat) — no bot platform needed.
Just scan QR code to login, then send/receive messages through WeChat.

Requires: pip install pycryptodome qrcode requests
          (itchat is bundled or use gechat for newer protocol)
"""

import os
import re
import sys
import time
import threading
import queue as Q

from nova.gateway import (
    clean_reply, build_done_text,
    HELP_TEXT, ensure_single_instance, create_agent,
)


def load_config():
    config = {}
    config['allowed_users'] = set(os.environ.get('WECHAT_ALLOWED_USERS', '').split(',')) if os.environ.get('WECHAT_ALLOWED_USERS') else set()
    return config


def main():
    """Start WeChat gateway — scan QR code on first run."""
    ensure_single_instance(19530, 'WeChat')

    config = load_config()
    agent = create_agent()

    # Try to import WeChat library
    try:
        import itchat
    except ImportError:
        print('Install: pip install itchat pycryptodome qrcode requests')
        print('Or use gechat for newer WeChat protocol support')
        sys.exit(1)

    @itchat.msg_register(itchat.content.TEXT)
    def handle_text(msg):
        sender = msg.get('FromUserName', '')
        nickname = msg.get('ActualNickName', '')

        # Auth check
        allowed = config.get('allowed_users', set())
        if allowed and nickname not in allowed and sender not in allowed:
            return

        text = msg.get('Text', '')
        if text.startswith('/'):
            cmd = text.strip().lower()
            if cmd == '/stop':
                agent.abort()
                return 'Aborted'
            elif cmd == '/status':
                status = 'Running' if agent.is_running else 'Idle'
                return f'Status: {status}'
            elif cmd == '/new':
                agent.abort()
                agent.history = []
                return 'Context cleared'
            elif cmd == '/help':
                return HELP_TEXT
            return

        # Process message
        dq = agent.put_task(text, source='wechat')

        # Wait for response (sync mode for WeChat)
        full_response = ''
        start_time = time.time()
        while time.time() - start_time < 120:
            try:
                item = dq.get(timeout=5)
            except Q.Empty:
                continue
            if 'done' in item:
                full_response = item['done']
                break
            if 'next' in item:
                full_response += item['next']

        reply = build_done_text(full_response)
        # WeChat message limit ~4500 chars
        if len(reply) > 4200:
            reply = reply[:4200] + '... (truncated)'
        return reply

    @itchat.msg_register([itchat.content.PICTURE, itchat.content.RECORDING, itchat.content.VIDEO])
    def handle_media(msg):
        # For now, acknowledge media but explain limitations
        return 'Media received — currently only text input is supported.'

    # Login with QR code
    print('WeChat gateway starting...')
    print('Scan the QR code with your WeChat app to login.')
    itchat.auto_login(hotReload=True, enableCmdQR=2)

    # Send startup message to filehelper (self-chat)
    itchat.send('Nova Agent online! Send me a message to start.', 'filehelper')

    itchat.run()


if __name__ == '__main__':
    main()