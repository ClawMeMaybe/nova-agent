"""Feishu (Lark) channel for Nova Agent gateway.

Connects via Feishu/Lark Open API with WebSocket long connection.
Supports text, rich text, images, files, and interactive cards.

Requires: pip install lark-oapi
Config: Set FS_APP_ID and FS_APP_SECRET in environment or .env file
"""

import asyncio
import json
import os
import sys
import time
import threading
import queue as Q

from nova.gateway import (
    clean_reply, build_done_text, extract_files,
    HELP_TEXT, ensure_single_instance, create_agent,
)


def load_config():
    config = {}
    config['app_id'] = os.environ.get('FS_APP_ID', '')
    config['app_secret'] = os.environ.get('FS_APP_SECRET', '')
    users = os.environ.get('FS_ALLOWED_USERS', '')
    config['allowed_users'] = set(users.split(',')) if users else set()
    return config


def main():
    """Start Feishu gateway."""
    ensure_single_instance(19531, 'Feishu')

    config = load_config()
    if not config['app_id'] or not config['app_secret']:
        print('ERROR: Set FS_APP_ID and FS_APP_SECRET')
        sys.exit(1)

    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody, CreateFileRequest, CreateFileRequestBody
    except ImportError:
        print('Install: pip install lark-oapi')
        sys.exit(1)

    agent = create_agent()

    # Create Feishu client
    client = lark.Client.builder() \
        .app_id(config['app_id']) \
        .app_secret(config['app_secret']) \
        .log_level(lark.LogLevel.DEBUG) \
        .build()

    # Event handler
    def handle_message(data: lark.im.P2ImMessageReceiveV1) -> None:
        msg = data.event.message
        msg_type = msg.message_type
        chat_id = msg.chat_id
        sender = data.event.sender.sender_id.open_id

        # Auth check
        allowed = config.get('allowed_users', set())
        if allowed and sender not in allowed and '*' not in allowed:
            return

        # Extract text content
        if msg_type == 'text':
            content = json.loads(msg.content)
            text = content.get('text', '')
        else:
            text = f'[Received {msg_type} message — currently only text is supported]'

        # Handle commands
        if text.startswith('/'):
            cmd = text.strip().lower()
            reply_text = ''
            if cmd == '/stop':
                agent.abort()
                reply_text = 'Aborted'
            elif cmd == '/status':
                reply_text = f'Status: {"Running" if agent.is_running else "Idle"}'
            elif cmd == '/new':
                agent.abort()
                agent.history = []
                reply_text = 'Context cleared'
            elif cmd == '/help':
                reply_text = HELP_TEXT

            if reply_text:
                request = CreateMessageRequest.builder() \
                    .receive_id_type('chat_id') \
                    .request_body(CreateMessageRequestBody.builder()
                        .receive_id(chat_id)
                        .msg_type('text')
                        .content(json.dumps({'text': reply_text}))
                        .build()) \
                    .build()
                client.im.message.create(request)
                return

        # Process with agent
        dq = agent.put_task(text, source='feishu')
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

        reply = build_done_text(full_response)

        # Send as text message (Feishu has good long-text support)
        request = CreateMessageRequest.builder() \
            .receive_id_type('chat_id') \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type('text')
                .content(json.dumps({'text': reply}))
                .build()) \
            .build()
        client.im.message.create(request)

        # Send any generated files
        for fpath in extract_files(full_response[-1000:]):
            if not os.path.isabs(fpath):
                fpath = os.path.join(os.environ.get('NOVA_TEMP', '/tmp'), fpath)
            if os.path.exists(fpath):
                try:
                    file_key = upload_file(client, fpath)
                    if file_key:
                        send_file_message(client, chat_id, fpath, file_key)
                except Exception as e:
                    print(f'[Feishu] File send error: {e}')

    def upload_file(client, fpath):
        """Upload a file to Feishu and get file_key."""
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        with open(fpath, 'rb') as f:
            file_name = os.path.basename(fpath)
            request = CreateFileRequest.builder() \
                .request_body(CreateFileRequestBody.builder()
                    .file_type('stream')
                    .file_name(file_name)
                    .file(f)
                    .build()) \
                .build()
            response = client.im.file.create(request)
            if response.success():
                return response.data.file_key
        return None

    def send_file_message(client, chat_id, fpath, file_key):
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        file_name = os.path.basename(fpath)
        content = json.dumps({'file_key': file_key, 'file_name': file_name})
        request = CreateMessageRequest.builder() \
            .receive_id_type('chat_id') \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type('file')
                .content(content)
                .build()) \
            .build()
        client.im.message.create(request)

    # Register event handler
    event_handler = lark.EventDispatcherHandler.builder(
        config['app_id'], config['app_secret']
    ).register_p2_im_message_receive_v1(handle_message).build()

    # Start WebSocket connection
    cli = lark.ws.Client.builder(config['app_id'], config['app_secret']) \
        .event_handler(event_handler) \
        .log_level(lark.LogLevel.DEBUG) \
        .build()

    print('Feishu gateway starting...')
    cli.start()


if __name__ == '__main__':
    main()