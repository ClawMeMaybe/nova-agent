"""LLM client abstraction — supports OpenAI, Anthropic, and OpenRouter APIs.

Inspired by GenericAgent's multi-session approach and Hermes' model routing.
"""

import json
import os
from typing import Any, Dict, List, Optional


class ToolCall:
    """Normalized tool call from any provider."""
    def __init__(self, id: str, name: str, arguments: str):
        self.id = id
        self.function = type('Fn', (), {'name': name, 'arguments': arguments})()


class LLMResponse:
    """Normalized LLM response."""
    def __init__(self, content: str = "", tool_calls: List[ToolCall] = None):
        self.content = content or ""
        self.tool_calls = tool_calls or []


class LLMSession:
    """Base LLM session — handles history and API calls."""

    def __init__(self, cfg: Dict[str, Any]):
        self.api_key = cfg.get('api_key', '')
        self.base_url = cfg.get('base_url', '')
        self.model = cfg.get('model', '')
        self.name = cfg.get('name', self.model)
        self.history: List[Dict] = []
        self.max_context = cfg.get('max_context', 100000)

    def chat(self, messages: List[Dict], tools: List[Dict] = None) -> LLMResponse:
        raise NotImplementedError


class AnthropicSession(LLMSession):
    """Anthropic Claude API session with proper tool_result handling."""

    def chat(self, messages: List[Dict], tools: List[Dict] = None) -> LLMResponse:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key, base_url=self.base_url or None)

        # Extract system prompt and convert messages
        system = None
        api_messages = []

        for msg in messages:
            role = msg.get('role', '')
            content = msg.get('content', '')

            if role == 'system':
                system = content if isinstance(content, str) else str(content)
                continue

            # Content can be a string or a list of content blocks (for tool use)
            if isinstance(content, list):
                # Already in Anthropic content block format — pass through
                api_messages.append({'role': role, 'content': content})
            elif isinstance(content, str):
                # Simple text message
                api_messages.append({'role': role, 'content': content})

        kwargs = {
            'model': self.model or 'claude-sonnet-4-20250514',
            'max_tokens': 8192,
            'messages': api_messages,
        }
        if system:
            kwargs['system'] = system
        if tools:
            # Convert OpenAI-style tool format to Anthropic format
            anthropic_tools = self._convert_tools(tools)
            kwargs['tools'] = anthropic_tools

        response = client.messages.create(**kwargs)

        # Parse response
        content = ""
        tool_calls = []
        for block in response.content:
            if block.type == 'text':
                content += block.text
            elif block.type == 'tool_use':
                # Anthropic returns block.input as a dict; we need to serialize it
                args_str = json.dumps(block.input, ensure_ascii=False)
                tool_calls.append(ToolCall(block.id, block.name, args_str))

        return LLMResponse(content=content, tool_calls=tool_calls)

    def _convert_tools(self, tools: List[Dict]) -> List[Dict]:
        """Convert OpenAI-style tool definitions to Anthropic format."""
        anthropic_tools = []
        for tool in tools:
            if 'function' in tool:
                fn = tool['function']
            else:
                fn = tool  # Already in our custom format

            anthropic_tools.append({
                'name': fn.get('name', ''),
                'description': fn.get('description', ''),
                'input_schema': fn.get('parameters', fn.get('input_schema', {})),
            })
        return anthropic_tools


class OpenAISession(LLMSession):
    """OpenAI-compatible API session (works with OpenRouter, local models, etc)."""

    def chat(self, messages: List[Dict], tools: List[Dict] = None) -> LLMResponse:
        import openai

        client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url or None)

        # Convert messages — handle content blocks and tool results for OpenAI format
        api_messages = []
        for msg in messages:
            role = msg.get('role', '')
            content = msg.get('content', '')

            if role == 'system':
                api_messages.append({'role': 'system', 'content': content})
                continue

            # Content can be string or list of content blocks
            if isinstance(content, list):
                # Convert content blocks to OpenAI format
                openai_blocks = []
                tool_use_blocks = []
                tool_result_blocks = []

                for block in content:
                    if isinstance(block, dict):
                        btype = block.get('type', '')
                        if btype == 'text':
                            openai_blocks.append({'type': 'text', 'text': block.get('text', '')})
                        elif btype == 'tool_use':
                            tool_use_blocks.append(block)
                        elif btype == 'tool_result':
                            tool_result_blocks.append(block)

                # Tool results → OpenAI role=tool messages
                for tr in tool_result_blocks:
                    api_messages.append({
                        'role': 'tool',
                        'tool_call_id': tr.get('tool_use_id', ''),
                        'content': tr.get('content', ''),
                    })

                # Tool use → OpenAI assistant tool_calls
                if tool_use_blocks:
                    text_parts = [b.get('text', '') for b in openai_blocks if b.get('text')]
                    tc_list = [{
                        'id': b.get('id', ''),
                        'type': 'function',
                        'function': {
                            'name': b.get('name', ''),
                            'arguments': json.dumps(b.get('input', {}), ensure_ascii=False)
                        }
                    } for b in tool_use_blocks]
                    api_messages.append({
                        'role': 'assistant',
                        'content': '\n'.join(text_parts) if text_parts else None,
                        'tool_calls': tc_list,
                    })
                elif openai_blocks:
                    text = '\n'.join(b.get('text', '') for b in openai_blocks)
                    api_messages.append({'role': role, 'content': text})
            elif isinstance(content, str):
                api_messages.append({'role': role, 'content': content})

        kwargs = {
            'model': self.model or 'gpt-4o',
            'messages': api_messages,
        }
        if tools:
            kwargs['tools'] = tools
            kwargs['tool_choice'] = 'auto'

        response = client.chat.completions.create(**kwargs)

        msg = response.choices[0].message
        content = msg.content or ""

        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(ToolCall(tc.id, tc.function.name, tc.function.arguments))

        return LLMResponse(content=content, tool_calls=tool_calls)


class LLMClient:
    """Unified client wrapping a session — handles tool schema formatting."""

    def __init__(self, session: LLMSession):
        self.backend = session
        self.last_tools = ''

    def chat(self, messages: List[Dict], tools: List[Dict] = None, tools_schema: List[Dict] = None) -> LLMResponse:
        schema = tools or tools_schema
        formatted_tools = self._format_tools(schema)
        response = self.backend.chat(messages, formatted_tools)
        return response

    def _format_tools(self, tools_schema: List[Dict] = None) -> Optional[List[Dict]]:
        if not tools_schema:
            return None

        formatted = []
        for tool in tools_schema:
            if 'function' in tool:
                formatted.append(tool)
            else:
                formatted.append({
                    'function': {
                        'name': tool.get('name', ''),
                        'description': tool.get('description', ''),
                        'parameters': tool.get('parameters', {}),
                    },
                    'type': 'function'
                })
        return formatted


def create_client_from_config() -> LLMClient:
    """Create an LLM client from environment variables or config file."""
    if os.environ.get('ANTHROPIC_API_KEY'):
        session = AnthropicSession({
            'api_key': os.environ['ANTHROPIC_API_KEY'],
            'base_url': os.environ.get('ANTHROPIC_BASE_URL', ''),
            'model': os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-20250514'),
        })
        return LLMClient(session)

    if os.environ.get('OPENAI_API_KEY'):
        session = OpenAISession({
            'api_key': os.environ['OPENAI_API_KEY'],
            'base_url': os.environ.get('OPENAI_BASE_URL', ''),
            'model': os.environ.get('OPENAI_MODEL', 'gpt-4o'),
        })
        return LLMClient(session)

    if os.environ.get('OPENROUTER_API_KEY'):
        session = OpenAISession({
            'api_key': os.environ['OPENROUTER_API_KEY'],
            'base_url': 'https://openrouter.ai/api/v1',
            'model': os.environ.get('OPENROUTER_MODEL', 'anthropic/claude-sonnet-4-20250514'),
        })
        return LLMClient(session)

    raise ValueError("No API key found. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or OPENROUTER_API_KEY")