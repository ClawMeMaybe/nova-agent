"""QQ channel for Nova Agent gateway.

Connects via QQ Bot Platform using WebSocket long connection (qq-botpy).
No public webhook required — works behind NAT/firewall.

Requires: pip install qq-botpy
Config: Set QQ_APP_ID and QQ_APP_SECRET in environment or .env file
"""

import json
import os
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
    config['app_id'] = os.environ.get('QQ_APP_ID', '')
    config['app_secret'] = os.environ.get('QQ_APP_SECRET', '')
    users = os.environ.get('QQ_ALLOWED_USERS', '')
    config['allowed_users'] = set(users.split(',')) if users else set()
    return config


def main():
    """Start QQ gateway."""
    ensure_single_instance(19532, 'QQ')

    config = load_config()
    if not config['app_id'] or not config['app_secret']:
        print('ERROR: Set QQ_APP_ID and QQ_APP_SECRET')
        print('Get them from: https://q.qq.com (QQ Open Platform)')
        sys.exit(1)

    try:
        import botpy
        from botpy import logging
        from botpy.message import GroupMessage, DirectMessage
    except ImportError:
        print('Install: pip install qq-botpy')
        sys.exit(1)

    agent = create_agent()
    log = logging.get_logger()

    class NovaQQClient(botpy.Client):
        async def on_at_message_create(self, message: GroupMessage):
            """Handle @bot messages in group channels."""
            content = message.content.strip()
            # Remove the @mention prefix
            content = re.sub(r'<@!\d+>', '', content).strip()

            # Auth
            allowed = config.get('allowed_users', set())
            author_id = message.author.member_openid
            if allowed and author_id not in allowed and '*' not in allowed:
                return

            # Commands
            if content.startswith('/'):
                cmd = content.strip().lower()
                if cmd == '/stop':
                    agent.abort()
                    await message.reply(content='Aborted')
                    return
                elif cmd == '/help':
                    await message.reply(content=HELP_TEXT)
                    return

            # Process with agent
            await message.reply(content='Thinking...')
            dq = agent.put_task(content, source='qq')

            full_response = ''
            start = time.time()
            while time.time() - start < 120:
                try:
                    item = dq.get(timeout=5)
                except Q.Empty:
                    continue
                if 'done' in item:
                    full_response = item['done']
                    break

            reply = build_done_text(full_response)
            # QQ message limit
            if len(reply) > 2000:
                for i in range(0, len(reply), 2000):
                    chunk = reply[i:i+2000]
                    await message.reply(content=chunk)
            else:
                await message.reply(content=reply)

        async def on_direct_message_create(self, message: DirectMessage):
            """Handle direct messages."""
            content = message.content.strip()

            allowed = config.get('allowed_users', set())
            author_id = message.author.user_openid
            if allowed and author_id not in allowed and '*' not in allowed:
                return

            # Commands
            if content.startswith('/'):
                cmd = content.strip().lower()
                if cmd == '/stop':
                    agent.abort()
                    await message.reply(content='Aborted')
                    return

            dq = agent.put_task(content, source='qq')
            full_response = ''
            start = time.time()
            while time.time() - start < 120:
                try:
                    item = dq.get(timeout=5)
                except Q.Empty:
                    continue
                if 'done' in item:
                    full_response = item['done']
                    break

            reply = build_done_text(full_response)
            if len(reply) > 2000:
                reply = reply[:2000] + '...'
            await message.reply(content=reply)

    intents = botpy.Intents(
        public_guilds=True,
        public_messages=True,
        direct_message=True,
    )
    client = NovaQQClient(intents=intents)
    client.run(appid=config['app_id'], token=config['app_secret'])


if __name__ == '__main__':
    main()