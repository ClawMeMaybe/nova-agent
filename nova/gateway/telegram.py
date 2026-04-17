"""Telegram channel for Nova Agent gateway.

Requires: pip install python-telegram-bot
Config: Set TG_BOT_TOKEN and TG_ALLOWED_USERS in environment or .env file
"""

import asyncio
import os
import re
import sys
import time
import threading
import queue as Q

from nova.gateway import (
    clean_reply, strip_files, extract_files, build_done_text,
    HELP_TEXT, ensure_single_instance, create_agent,
)


def load_config():
    config = {}
    # Environment variables only (no mykey.py — security)
    config['bot_token'] = os.environ.get('TG_BOT_TOKEN', '')
    users = os.environ.get('TG_ALLOWED_USERS', '')
    if users:
        config['allowed_users'] = set(users.split(','))
    else:
        config['allowed_users'] = set()

    return config


def to_html(text):
    """Convert markdown-ish text to HTML for Telegram."""
    import html as _html
    # Bold
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # Italic
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
    # Inline code
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    # Code blocks → <pre>
    parts, pos = [], 0
    for m in re.finditer(r'(`{3,})(?:\w*\n)?([\s\S]*?)\1', text):
        parts.append(re.sub(r'<[^>]+>', '', text[pos:m.start()]))  # Strip HTML outside blocks
        parts.append('<pre><code>' + _html.escape(m.group(2)) + '</code></pre>')
        pos = m.end()
    parts.append(re.sub(r'<[^>]+>', '', text[pos:]))
    return ''.join(parts)


async def stream_response(dq, msg, bot):
    """Stream agent output to a Telegram message with periodic updates."""
    last_text = ""
    while True:
        await asyncio.sleep(3)
        item = None
        try:
            while True:
                item = dq.get_nowait()
        except Q.Empty:
            pass
        if item is None:
            continue

        raw = item.get('done') or item.get('next', '')
        done = 'done' in item
        show = clean_reply(raw)

        # Telegram message limit is 4096 chars
        if len(show) > 4000:
            try:
                msg = await msg.reply_text('(continued...)')
            except Exception:
                pass
            last_text = ''
            show = show[-3900:]

        display = show if done else show + ' ...'
        if display != last_text:
            try:
                await msg.edit_text(to_html(display), parse_mode='HTML')
            except Exception:
                try:
                    await msg.edit_text(display)
                except Exception:
                    pass
            last_text = display

        if done:
            # Send any generated files
            files = extract_files(show[-1000:])
            for fpath in files:
                if not os.path.isabs(fpath):
                    fpath = os.path.join(os.environ.get('NOVA_TEMP', '/tmp'), fpath)
                if os.path.exists(fpath):
                    if fpath.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                        try:
                            await msg.reply_photo(open(fpath, 'rb'))
                        except Exception:
                            pass
                    else:
                        try:
                            await msg.reply_document(open(fpath, 'rb'))
                        except Exception:
                            pass
            break


async def handle_msg(update, ctx):
    """Handle incoming Telegram message."""
    uid = update.effective_user.id
    config = ctx.bot_data.get('config', {})
    allowed = config.get('allowed_users', set())

    if allowed and uid not in allowed:
        return await update.message.reply_text('Unauthorized')

    agent = ctx.bot_data.get('agent')
    msg = await update.message.reply_text('Thinking...')
    prompt = update.message.text
    dq = agent.put_task(prompt, source='telegram')
    await stream_response(dq, msg, ctx.bot)


async def cmd_stop(update, ctx):
    agent = ctx.bot_data.get('agent')
    agent.abort()
    await update.message.reply_text('Aborted')


async def cmd_status(update, ctx):
    agent = ctx.bot_data.get('agent')
    status = 'Running' if agent.is_running else 'Idle'
    await update.message.reply_text(f'Status: {status}')


async def cmd_new(update, ctx):
    agent = ctx.bot_data.get('agent')
    agent.abort()
    agent.history = []
    await update.message.reply_text('Context cleared')


async def cmd_help(update, ctx):
    await update.message.reply_text(HELP_TEXT)


def main():
    try:
        from telegram import Update
        from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
    except ImportError:
        print('Install: pip install python-telegram-bot')
        sys.exit(1)

    ensure_single_instance(19527, 'Telegram')

    config = load_config()
    if not config['bot_token']:
        print('ERROR: Set TG_BOT_TOKEN environment variable')
        sys.exit(1)
    if not config['allowed_users']:
        print('WARNING: TG_ALLOWED_USERS is empty — anyone can use the bot!')

    agent = create_agent()

    app = ApplicationBuilder().token(config['bot_token']).build()
    app.bot_data['agent'] = agent
    app.bot_data['config'] = config

    app.add_handler(CommandHandler('stop', cmd_stop))
    app.add_handler(CommandHandler('status', cmd_status))
    app.add_handler(CommandHandler('new', cmd_new))
    app.add_handler(CommandHandler('help', cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

    print('Telegram gateway starting...')
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()