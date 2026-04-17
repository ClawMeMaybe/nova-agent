"""DingTalk channel for Nova Agent gateway.

Connects via DingTalk Stream API using WebSocket long connection.
Supports both direct messages and group conversations.
Uses DingTalk's sampleMarkdown format for rich replies.

Requires: pip install dingtalk-stream
Config: Set DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET in environment or .env file
Get credentials from: https://open-dev.dingtalk.com (DingTalk Open Platform)
"""

import asyncio
import json
import os
import re
import sys
import time
import threading
import queue as Q
import requests

from nova.gateway import (
    clean_reply, build_done_text,
    HELP_TEXT, ensure_single_instance, create_agent,
)


def load_config():
    config = {}
    config['client_id'] = os.environ.get('DINGTALK_CLIENT_ID', '')
    config['client_secret'] = os.environ.get('DINGTALK_CLIENT_SECRET', '')
    users = os.environ.get('DINGTALK_ALLOWED_USERS', '')
    config['allowed_users'] = set(users.split(',')) if users else set()
    return config


def split_text(text, limit=1800):
    """Split long text into chunks within DingTalk's message limit."""
    text = (text or '').strip() or '...'
    parts = []
    while len(text) > limit:
        cut = text.rfind('\n', 0, limit)
        if cut < limit * 0.6:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts + ([text] if text else []) or ['...']


class DingTalkApp:
    """DingTalk bot connecting to Nova Agent."""

    label = 'DingTalk'
    source = 'dingtalk'

    def __init__(self, agent, config):
        self.agent = agent
        self.config = config
        self.allowed = config.get('allowed_users', set())
        self.user_tasks = {}
        self.access_token = None
        self.token_expiry = 0
        self.client = None
        self.background_tasks = set()

    def _is_public(self):
        return not self.allowed or '*' in self.allowed

    async def _get_access_token(self):
        """Fetch or refresh DingTalk access token."""
        if self.access_token and time.time() < self.token_expiry:
            return self.access_token

        def _fetch():
            resp = requests.post(
                'https://api.dingtalk.com/v1.0/oauth2/accessToken',
                json={'appKey': self.config['client_id'], 'appSecret': self.config['client_secret']},
                timeout=20
            )
            resp.raise_for_status()
            return resp.json()

        try:
            data = await asyncio.to_thread(_fetch)
            self.access_token = data.get('accessToken')
            self.token_expiry = time.time() + int(data.get('expireIn', 7200)) - 60
            return self.access_token
        except Exception as e:
            print(f'[DingTalk] token error: {e}')
            return None

    async def _send_batch_message(self, chat_id, msg_key, msg_param):
        """Send message via DingTalk API — handles both group and direct messages."""
        token = await self._get_access_token()
        if not token:
            return False
        headers = {'x-acs-dingtalk-access-token': token}

        if chat_id.startswith('group:'):
            url = 'https://api.dingtalk.com/v1.0/robot/groupMessages/send'
            payload = {
                'robotCode': self.config['client_id'],
                'openConversationId': chat_id[6:],
                'msgKey': msg_key,
                'msgParam': json.dumps(msg_param, ensure_ascii=False)
            }
        else:
            url = 'https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend'
            payload = {
                'robotCode': self.config['client_id'],
                'userIds': [chat_id],
                'msgKey': msg_key,
                'msgParam': json.dumps(msg_param, ensure_ascii=False)
            }

        def _post():
            resp = requests.post(url, json=payload, headers=headers, timeout=20)
            if resp.status_code != 200:
                raise RuntimeError(f'HTTP {resp.status_code}: {resp.text[:300]}')
            result = resp.json() if 'json' in resp.headers.get('content-type', '') else {}
            errcode = result.get('errcode')
            if errcode not in (None, 0):
                raise RuntimeError(f'API errcode={errcode}: {resp.text[:300]}')
            return True

        try:
            return await asyncio.to_thread(_post)
        except Exception as e:
            print(f'[DingTalk] send error: {e}')
            return False

    async def send_text(self, chat_id, content):
        """Send text in DingTalk's sampleMarkdown format."""
        for part in split_text(content):
            await self._send_batch_message(chat_id, 'sampleMarkdown', {'text': part, 'title': 'Nova Agent'})

    async def handle_command(self, chat_id, cmd):
        """Handle slash commands."""
        parts = (cmd or '').split()
        op = (parts[0] if parts else '').lower()

        if op == '/stop':
            state = self.user_tasks.get(chat_id)
            if state:
                state['running'] = False
            self.agent.abort()
            return await self.send_text(chat_id, 'Aborted')
        if op == '/status':
            status = 'Running' if self.agent.is_running else 'Idle'
            return await self.send_text(chat_id, f'Status: {status}')
        if op == '/new':
            self.agent.abort()
            self.agent.history = []
            return await self.send_text(chat_id, 'Context cleared')
        if op == '/help':
            return await self.send_text(chat_id, HELP_TEXT)

    async def run_agent(self, chat_id, text):
        """Run agent for a user message and stream the response."""
        state = {'running': True}
        self.user_tasks[chat_id] = state
        try:
            await self.send_text(chat_id, 'Thinking...')
            dq = self.agent.put_task(text, source=self.source)

            last_ping = time.time()
            while state['running']:
                try:
                    item = await asyncio.to_thread(dq.get, True, 3)
                except Q.Empty:
                    if self.agent.is_running and time.time() - last_ping > 20:
                        await self.send_text(chat_id, 'Still working...')
                        last_ping = time.time()
                    continue
                if 'done' in item:
                    await self.send_text(chat_id, build_done_text(item.get('done', '')))
                    break
            if not state['running']:
                await self.send_text(chat_id, 'Stopped')
        except Exception as e:
            print(f'[DingTalk] run_agent error: {e}')
            await self.send_text(chat_id, f'Error: {str(e)[:200]}')
        finally:
            self.user_tasks.pop(chat_id, None)

    async def on_message(self, content, sender_id, sender_name, conversation_type=None, conversation_id=None):
        """Handle incoming DingTalk message."""
        try:
            if not content:
                return
            if not self._is_public() and sender_id not in self.allowed:
                print(f'[DingTalk] unauthorized: {sender_id}')
                return

            is_group = conversation_type == '2' and conversation_id
            chat_id = f'group:{conversation_id}' if is_group else sender_id
            print(f'[DingTalk] {sender_name} ({sender_id}): {content[:80]}')

            if content.startswith('/'):
                return await self.handle_command(chat_id, content)

            task = asyncio.create_task(self.run_agent(chat_id, content))
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)
        except Exception as e:
            print(f'[DingTalk] on_message error: {e}')

    async def start(self):
        """Start DingTalk stream client with auto-reconnect."""
        from dingtalk_stream import AckMessage, CallbackHandler, Credential, DingTalkStreamClient
        from dingtalk_stream.chatbot import ChatbotMessage

        class DingTalkHandler(CallbackHandler):
            def __init__(self, app):
                super().__init__()
                self.app = app

            async def process(self, message):
                try:
                    chatbot_msg = ChatbotMessage.from_dict(message.data)
                    text = getattr(getattr(chatbot_msg, 'text', None), 'content', '') or ''
                    extensions = getattr(chatbot_msg, 'extensions', None) or {}
                    recognition = ((extensions.get('content') or {}).get('recognition') or '').strip() if isinstance(extensions, dict) else ''
                    if not (text := text.strip()):
                        text = recognition or str((message.data.get('text', {}) or {}).get('content', '') or '').strip()
                    sender_id = str(getattr(chatbot_msg, 'sender_staff_id', None) or getattr(chatbot_msg, 'sender_id', None) or 'unknown')
                    sender_name = getattr(chatbot_msg, 'sender_nick', None) or 'Unknown'
                    await self.app.on_message(
                        text, sender_id, sender_name,
                        message.data.get('conversationType'),
                        message.data.get('conversationId') or message.data.get('openConversationId')
                    )
                except Exception as e:
                    print(f'[DingTalk] callback error: {e}')
                return AckMessage.STATUS_OK, 'OK'

        self.client = DingTalkStreamClient(
            Credential(self.config['client_id'], self.config['client_secret'])
        )
        self.client.register_callback_handler(ChatbotMessage.TOPIC, DingTalkHandler(self))
        print('[DingTalk] bot starting...')
        while True:
            try:
                await self.client.start()
            except Exception as e:
                print(f'[DingTalk] stream error: {e}')
            print('[DingTalk] reconnect in 5s...')
            await asyncio.sleep(5)


def main():
    """Start DingTalk gateway."""
    ensure_single_instance(19533, 'DingTalk')

    config = load_config()
    if not config['client_id'] or not config['client_secret']:
        print('ERROR: Set DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET')
        print('Get them from: https://open-dev.dingtalk.com')
        sys.exit(1)

    try:
        from dingtalk_stream import DingTalkStreamClient
    except ImportError:
        print('Install: pip install dingtalk-stream')
        sys.exit(1)

    agent = create_agent()
    app = DingTalkApp(agent, config)
    asyncio.run(app.start())


if __name__ == '__main__':
    main()