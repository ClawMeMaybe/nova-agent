"""WeCom (企业微信) channel for Nova Agent gateway.

Connects via WeCom self-built application — HTTP webhook callback.
The mobile way to continue integrations when you're away from CLI.

Requires: pip install pycryptodome requests
Config: Set WECOM_* environment variables (see .env.example)
Get credentials from: https://open.work.weixin.qq.com (WeCom Admin Console)
"""

import base64
import hashlib
import json
import os
import struct
import sys
import time
import threading
import queue as Q
import xml.etree.ElementTree as ET

from nova.gateway import (
    clean_reply, build_done_text,
    HELP_TEXT, ensure_single_instance, create_agent,
)

WECOM_API_BASE = 'https://qyapi.weixin.qq.com'


# ── WeCom Crypto ──

def _decode_aes_key(aes_key):
    """Decode WeCom AES key from Base64 string (43 chars) to 32-byte key."""
    b64 = aes_key if aes_key.endswith('=') else aes_key + '='
    key = base64.b64decode(b64)
    if len(key) != 32:
        raise ValueError(f'Invalid AES key: expected 32 bytes, got {len(key)}')
    return key


def _pkcs7_pad(data, block_size=32):
    pad_len = block_size - (len(data) % block_size or block_size)
    return data + bytes([pad_len] * pad_len)


def _pkcs7_unpad(data):
    pad = data[-1]
    if pad < 1 or pad > 32:
        return data
    return data[:-pad]


def wecom_decrypt(aes_key, cipher_b64):
    """Decrypt WeCom AES-256-CBC payload. Returns (msg, corp_id)."""
    key = _decode_aes_key(aes_key)
    iv = key[:16]
    from Crypto.Cipher import AES
    cipher = AES.new(key, AES.MODE_CBC, iv)
    plain = _pkcs7_unpad(cipher.decrypt(base64.b64decode(cipher_b64)))
    msg_len = struct.unpack('>I', plain[16:20])[0]
    msg = plain[20:20 + msg_len].decode('utf-8')
    corp_id = plain[20 + msg_len:].decode('utf-8')
    return msg, corp_id


def wecom_encrypt(aes_key, plain_text, corp_id):
    """Encrypt payload for WeCom reply. Returns Base64 string."""
    key = _decode_aes_key(aes_key)
    iv = key[:16]
    random_bytes = os.urandom(16)
    msg_bytes = plain_text.encode('utf-8')
    len_bytes = struct.pack('>I', len(msg_bytes))
    corp_bytes = corp_id.encode('utf-8')
    raw = random_bytes + len_bytes + msg_bytes + corp_bytes
    padded = _pkcs7_pad(raw, 32)
    from Crypto.Cipher import AES
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return base64.b64encode(cipher.encrypt(padded)).decode('utf-8')


def wecom_signature(token, timestamp, nonce, encrypt):
    """Compute SHA1 signature for WeCom callback verification."""
    parts = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1(''.join(parts).encode('utf-8')).hexdigest()


# ── WeCom API Client ──

class WecomApiClient:
    """WeCom HTTP API client with access token caching."""

    def __init__(self, corp_id, corp_secret, api_base=WECOM_API_BASE):
        self.corp_id = corp_id
        self.corp_secret = corp_secret
        self.api_base = api_base
        self._token = None
        self._token_expiry = 0
        self._lock = threading.Lock()

    def _get_token(self):
        """Fetch or refresh access token."""
        with self._lock:
            if self._token and time.time() < self._token_expiry:
                return self._token

        import requests
        url = f'{self.api_base}/cgi-bin/gettoken?corpid={self.corp_id}&corpsecret={self.corp_secret}'
        resp = requests.get(url, timeout=20)
        data = resp.json()
        if not data.get('access_token'):
            raise RuntimeError(f'WeCom gettoken failed: {data}')
        with self._lock:
            self._token = data['access_token']
            self._token_expiry = time.time() + int(data.get('expires_in', 7200)) - 60
        return self._token

    def send_text(self, agent_id, to_user, text, to_party='', to_tag='', chat_id=''):
        """Send text message via WeCom API."""
        token = self._get_token()
        import requests

        if chat_id:
            url = f'{self.api_base}/cgi-bin/appchat/send?access_token={token}'
            payload = {
                'chatid': chat_id,
                'msgtype': 'text',
                'text': {'content': text},
                'safe': 0,
            }
        else:
            url = f'{self.api_base}/cgi-bin/message/send?access_token={token}'
            payload = {
                'touser': to_user,
                'toparty': to_party,
                'totag': to_tag,
                'msgtype': 'text',
                'agentid': int(agent_id),
                'text': {'content': text},
                'safe': 0,
            }

        resp = requests.post(url, json=payload, timeout=20)
        result = resp.json()
        errcode = result.get('errcode', 0)
        if errcode != 0:
            print(f'[WeCom] send error: errcode={errcode} errmsg={result.get("errmsg", "")}')
        return errcode == 0

    def send_markdown(self, agent_id, to_user, content, to_party='', to_tag=''):
        """Send markdown message via WeCom API."""
        token = self._get_token()
        import requests

        url = f'{self.api_base}/cgi-bin/message/send?access_token={token}'
        payload = {
            'touser': to_user,
            'toparty': to_party,
            'totag': to_tag,
            'msgtype': 'markdown',
            'agentid': int(agent_id),
            'markdown': {'content': content},
        }
        resp = requests.post(url, json=payload, timeout=20)
        result = resp.json()
        errcode = result.get('errcode', 0)
        if errcode != 0:
            print(f'[WeCom] send_markdown error: errcode={errcode}')
        return errcode == 0


def _split_text(text, limit=1800):
    """Split long text into WeCom-friendly chunks."""
    text = (text or '').strip() or '...'
    parts = []
    while len(text) > limit:
        cut = text.rfind('\n', 0, limit)
        if cut < limit * 0.6:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts + ([text] if text else []) or ['...']


# ── HTTP Webhook Server ──

def _process_and_reply(agent, api, agent_id, to_user, chat_id, text):
    """Process agent task and send reply via WeCom API."""
    try:
        dq = agent.put_task(text, source='wecom')
        full_response = ''
        start_time = time.time()
        while time.time() - start_time < 120:
            try:
                item = dq.get(timeout=5)
            except Q.Empty:
                if agent.is_running and time.time() - start_time > 20:
                    api.send_text(agent_id, to_user, 'Still working...')
                continue
            if 'done' in item:
                full_response = item['done']
                break

        reply = build_done_text(full_response)
        for part in _split_text(reply, 1800):
            api.send_text(agent_id, to_user, part, chat_id=chat_id if chat_id else '')
    except Exception as e:
        print(f'[WeCom] reply error: {e}')
        api.send_text(agent_id, to_user, f'Error: {str(e)[:200]}')


def main():
    """Start WeCom gateway — HTTP webhook callback server."""
    ensure_single_instance(19530, 'WeCom')

    # Load config
    corp_id = os.environ.get('WECOM_CORP_ID', '')
    corp_secret = os.environ.get('WECOM_CORP_SECRET', '')
    agent_id = os.environ.get('WECOM_AGENT_ID', '')
    callback_token = os.environ.get('WECOM_CALLBACK_TOKEN', '')
    callback_aes_key = os.environ.get('WECOM_CALLBACK_AES_KEY', '')
    users = os.environ.get('WECOM_ALLOWED_USERS', '')
    allowed_users = set(users.split(',')) if users else set()
    port = int(os.environ.get('WECOM_PORT', '8081'))

    if not (corp_id and corp_secret and agent_id and callback_token and callback_aes_key):
        print('ERROR: WeCom credentials not configured.')
        print('Set WECOM_CORP_ID, WECOM_CORP_SECRET, WECOM_AGENT_ID,')
        print('    WECOM_CALLBACK_TOKEN, WECOM_CALLBACK_AES_KEY')
        print('Get credentials from: https://open.work.weixin.qq.com')
        sys.exit(1)

    # Check crypto dependency
    try:
        from Crypto.Cipher import AES
    except ImportError:
        print('Install: pip install pycryptodome requests')
        sys.exit(1)

    agent = create_agent()
    api = WecomApiClient(corp_id, corp_secret)
    seen_msg_ids = set()

    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = self.path
            if url == '/status':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                status = json.dumps({'running': agent.is_running, 'history_size': len(agent.history)})
                self.wfile.write(status.encode())
                return

            # Callback URL verification (WeCom sends GET with echostr)
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            msg_signature = params.get('msg_signature', [''])[0]
            timestamp = params.get('timestamp', [''])[0]
            nonce = params.get('nonce', [''])[0]
            echostr = params.get('echostr', [''])[0]

            if not echostr:
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(b'wecom webhook ok')
                return

            # Verify signature and decrypt echostr
            expected_sig = wecom_signature(callback_token, timestamp, nonce, echostr)
            if msg_signature != expected_sig:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'Invalid signature')
                return

            plain_echo, _ = wecom_decrypt(callback_aes_key, echostr)
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(plain_echo.encode())

        def do_POST(self):
            # Inbound message callback
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            msg_signature = params.get('msg_signature', [''])[0]
            timestamp = params.get('timestamp', [''])[0]
            nonce = params.get('nonce', [''])[0]

            content_length = int(self.headers.get('Content-Length', 0))
            raw_xml = self.rfile.read(content_length).decode('utf-8')

            # Parse encrypted content from XML
            try:
                root = ET.fromstring(raw_xml)
                encrypt = root.find('Encrypt').text or ''
            except Exception:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Invalid XML')
                return

            # Verify signature
            expected_sig = wecom_signature(callback_token, timestamp, nonce, encrypt)
            if msg_signature != expected_sig:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'Invalid signature')
                return

            # Ack immediately
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'success')

            # Decrypt and process
            try:
                decrypted_xml, _corp_id = wecom_decrypt(callback_aes_key, encrypt)
                msg_root = ET.fromstring(decrypted_xml)
            except Exception as e:
                print(f'[WeCom] decrypt error: {e}')
                return

            msg_type = msg_root.find('MsgType').text or ''
            from_user = msg_root.find('FromUserName').text or ''
            msg_id = msg_root.find('MsgId').text or ''

            # Dedup
            if msg_id in seen_msg_ids:
                return
            seen_msg_ids.add(msg_id)

            # Auth
            if allowed_users and from_user not in allowed_users and '*' not in allowed_users:
                print(f'[WeCom] unauthorized: {from_user}')
                return

            # Extract content
            content = ''
            if msg_type == 'text':
                content_el = msg_root.find('Content')
                content = content_el.text or '' if content_el else ''
            elif msg_type == 'markdown':
                content_el = msg_root.find('Content')
                content = content_el.text or '' if content_el else ''
            else:
                api.send_text(agent_id, from_user, f'Received {msg_type} — only text supported.')
                return

            chat_id = (msg_root.find('ChatId').text or '') if msg_root.find('ChatId') is not None else ''

            print(f'[WeCom] from={from_user} chatId={chat_id or "N/A"}: {content[:80]}')

            # Handle commands
            if content.startswith('/'):
                cmd = content.strip().lower()
                reply = ''
                if cmd == '/stop':
                    agent.abort()
                    reply = 'Aborted'
                elif cmd == '/status':
                    reply = f'Status: {"Running" if agent.is_running else "Idle"}'
                elif cmd == '/new':
                    agent.abort()
                    agent.history = []
                    reply = 'Context cleared'
                elif cmd == '/help':
                    reply = HELP_TEXT
                if reply:
                    api.send_text(agent_id, from_user, reply)
                return

            # Process with agent in background
            threading.Thread(target=_process_and_reply, args=(
                agent, api, agent_id, from_user, chat_id, content
            ), daemon=True).start()

        def log_message(self, format, *args):
            pass

    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f'[WeCom] webhook running on port {port} — your mobile integration bridge')
    server.serve_forever()


if __name__ == '__main__':
    main()