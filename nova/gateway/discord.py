"""Discord channel for Nova Agent gateway.

Requires: pip install discord.py
Config: Set DISCORD_BOT_TOKEN and DISCORD_ALLOWED_CHANNELS in environment
"""

import asyncio
import os
import re
import sys
import threading
import queue as Q

from nova.gateway import (
    clean_reply, build_done_text, extract_files,
    HELP_TEXT, ensure_single_instance, create_agent,
)


class NovaDiscordBot:
    """Discord bot that connects to Nova Agent."""

    def __init__(self, agent, token, allowed_channels=None):
        self.agent = agent
        self.token = token
        self.allowed_channels = allowed_channels or set()
        self.user_tasks = {}

    async def run(self):
        try:
            import discord
            from discord.ext import commands
        except ImportError:
            print('Install: pip install discord.py')
            sys.exit(1)

        intents = discord.Intents.default()
        intents.message_content = True
        bot = commands.Bot(command_prefix='/', intents=intents)

        @bot.event
        async def on_ready():
            print(f'Discord bot connected: {bot.user}')

        @bot.command(name='help')
        async def cmd_help(ctx):
            await ctx.send(HELP_TEXT)

        @bot.command(name='stop')
        async def cmd_stop(ctx):
            self.agent.abort()
            await ctx.send('Aborted')

        @bot.command(name='status')
        async def cmd_status(ctx):
            status = 'Running' if self.agent.is_running else 'Idle'
            await ctx.send(f'Status: {status}')

        @bot.command(name='new')
        async def cmd_new(ctx):
            self.agent.abort()
            self.agent.history = []
            await ctx.send('Context cleared')

        @bot.command(name='chat')
        async def cmd_chat(ctx, *, text: str):
            if self.allowed_channels and str(ctx.channel.id) not in self.allowed_channels:
                return

            await ctx.send('Thinking...')
            dq = self.agent.put_task(text, source='discord')

            # Stream output
            last_content = ''
            while True:
                await asyncio.sleep(2)
                item = None
                try:
                    while True:
                        item = dq.get_nowait()
                except Q.Empty:
                    pass
                if item is None:
                    continue
                if 'done' in item:
                    result = build_done_text(item['done'])
                    # Discord has 2000 char limit per message
                    if len(result) > 1900:
                        chunks = [result[i:i+1900] for i in range(0, len(result), 1900)]
                        for chunk in chunks:
                            await ctx.send(chunk)
                    else:
                        await ctx.send(result)
                    # Send files
                    for fpath in extract_files(item['done'][-1000:]):
                        if not os.path.isabs(fpath):
                            fpath = os.path.join(os.environ.get('NOVA_TEMP', '/tmp'), fpath)
                        if os.path.exists(fpath):
                            try:
                                await ctx.send(file=discord.File(fpath))
                            except Exception:
                                pass
                    break

        await bot.start(self.token)


def main():
    ensure_single_instance(19528, 'Discord')

    token = os.environ.get('DISCORD_BOT_TOKEN', '')
    channels = os.environ.get('DISCORD_ALLOWED_CHANNELS', '')
    allowed = set(channels.split(',')) if channels else set()

    if not token:
        print('ERROR: Set DISCORD_BOT_TOKEN environment variable')
        sys.exit(1)

    agent = create_agent()
    bot = NovaDiscordBot(agent, token, allowed)
    asyncio.run(bot.run())


if __name__ == '__main__':
    main()