"""HTTP webhook channel for Nova Agent gateway.

A simple HTTP server that accepts POST requests with messages
and returns agent responses. Useful for web UIs, custom integrations,
or REST API access.

Requires: pip install flask (or just uses built-in http.server for minimal setup)
"""

import asyncio
import json
import os
import sys
import threading
import time
import queue as Q

from nova.gateway import clean_reply, create_agent, ensure_single_instance


def main_minimal():
    """Run a minimal HTTP webhook using only stdlib (no Flask required)."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    ensure_single_instance(19529, 'HTTP')

    agent = create_agent()
    allowed_tokens = set(os.environ.get('NOVA_WEB_TOKENS', '').split(',')) if os.environ.get('NOVA_WEB_TOKENS') else None

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != '/chat':
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Not found')
                return

            # Auth check
            auth = self.headers.get('Authorization', '').replace('Bearer ', '')
            if allowed_tokens and auth not in allowed_tokens:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'Unauthorized')
                return

            content_length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_length))
            message = body.get('message', '')

            if not message:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'No message provided'}).encode())
                return

            # Run agent
            dq = agent.put_task(message, source='web')
            full_response = ''

            # Wait for completion (timeout 120s)
            start = time.time()
            while time.time() - start < 120:
                try:
                    item = dq.get(timeout=5)
                except Q.Empty:
                    continue
                if 'done' in item:
                    full_response = item['done']
                    break
                if 'next' in item:
                    full_response += item['next']

            # Return response
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            response_data = {
                'response': clean_reply(full_response),
                'raw': full_response,
                'status': 'completed' if full_response else 'timeout',
            }
            self.wfile.write(json.dumps(response_data, ensure_ascii=False).encode())

        def do_GET(self):
            if self.path == '/status':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                status = {'running': agent.is_running, 'history_size': len(agent.history)}
                self.wfile.write(json.dumps(status).encode())
            else:
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                html = """<!DOCTYPE html><html><head><title>Nova Agent</title></head><body>
                <h1>Nova Agent Web</h1>
                <form id="chat">
                    <input type="text" id="msg" placeholder="Type your message..." style="width:80%">
                    <button type="submit">Send</button>
                </form>
                <div id="output" style="white-space:pre-wrap;font-family:monospace;margin-top:20px"></div>
                <script>
                document.getElementById('chat').onsubmit = async(e) => {
                    e.preventDefault();
                    const msg = document.getElementById('msg').value;
                    const out = document.getElementById('output');
                    out.textContent = 'Thinking...';
                    try {
                        const r = await fetch('/chat', {
                            method:'POST',
                            headers:{'Content-Type':'application/json'},
                            body:JSON.stringify({message:msg})
                        });
                        const data = await r.json();
                        out.textContent = data.response;
                    } catch(err) { out.textContent = 'Error: ' + err; }
                };
                </script></body></html>"""
                self.wfile.write(html.encode())

        def log_message(self, format, *args):
            pass  # Suppress verbose logging

    port = int(os.environ.get('NOVA_WEB_PORT', '8080'))
    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f'HTTP webhook running on port {port}')
    server.serve_forever()


if __name__ == '__main__':
    main_minimal()