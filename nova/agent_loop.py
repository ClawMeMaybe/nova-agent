"""Core agent loop — the heart of Nova Agent.

Inspired by GenericAgent's ~100-line loop, simplified and enhanced.
Pattern: LLM call → tool dispatch → StepOutcome → next prompt → loop.

CRITICAL: The loop must maintain full conversation history across turns.
Without it, the LLM loses context after each tool result.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from nova.events import AgentEvent

logger = logging.getLogger("nova")


@dataclass
class StepOutcome:
    """Result from a tool execution — drives the loop forward."""
    data: Any
    next_prompt: Optional[str] = None
    should_exit: bool = False


class BaseHandler:
    """Base class for tool handlers. Subclass and add do_<tool_name> methods."""

    events = None  # EventBus instance, set by parent agent

    def tool_before_callback(self, tool_name, args, response):
        pass

    def tool_after_callback(self, tool_name, args, response, ret):
        pass

    def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason):
        return next_prompt

    def dispatch(self, tool_name, args, response, index=0):
        method_name = f"do_{tool_name}"
        if hasattr(self, method_name):
            args['_index'] = index
            ret = self.tool_before_callback(tool_name, args, response)
            outcome = getattr(self, method_name)(args, response)
            self.tool_after_callback(tool_name, args, response, ret)
            return outcome
        elif tool_name == 'bad_json':
            return StepOutcome(None, next_prompt=args.get('msg', 'bad_json'), should_exit=False)
        else:
            return StepOutcome(None, next_prompt=f"Unknown tool: {tool_name}", should_exit=False)


def _get_pretty_json(data):
    if isinstance(data, dict) and "script" in data:
        data = data.copy()
        data["script"] = data["script"].replace("; ", ";\n  ")
    return json.dumps(data, indent=2, ensure_ascii=False)


def agent_runner_loop(client, system_prompt, user_input, handler, tools_schema,
                      max_turns=40, session_id=None, memory=None):
    """The core agent loop — perceive → reason → execute → remember → loop.

    Maintains full conversation history so the LLM always has context.
    Message format follows Anthropic's tool use protocol:
      system → user(task) → assistant(text + tool_use) → user(tool_result + continuation) → ...
    """
    # Build initial conversation
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input}
    ]
    turn = 0
    exit_reason = None

    while turn < max_turns:
        turn += 1
        logger.debug(f"Turn {turn}")
        if handler.events:
            handler.events.emit(AgentEvent.AGENT_THINKING, {"turn": turn})

        # Call LLM with full history
        response = client.chat(messages=messages, tools=tools_schema)

        # Parse tool calls
        if not response.tool_calls:
            tool_calls = [{'tool_name': 'no_tool', 'args': {}}]
        else:
            tool_calls = [
                {'tool_name': tc.function.name, 'args': json.loads(tc.function.arguments), 'id': tc.id}
                for tc in response.tool_calls
            ]

        # Add assistant response to history
        # Build the assistant message content blocks for Anthropic format
        assistant_content = []
        if response.content:
            assistant_content.append({"type": "text", "text": response.content})
        for tc_info in tool_calls:
            if tc_info['tool_name'] != 'no_tool':
                assistant_content.append({
                    "type": "tool_use",
                    "id": tc_info['id'],
                    "name": tc_info['tool_name'],
                    "input": tc_info['args']
                })

        if assistant_content:
            messages.append({"role": "assistant", "content": assistant_content})

        tool_results = []
        next_prompts = set()

        # Emit LLM text response even when tool calls are present
        # (progress headers, score tables, explanations alongside ask_user etc.)
        if handler.events and response.content:
            handler.events.emit(AgentEvent.AGENT_RESPONSE, response.content)

        for ii, tc in enumerate(tool_calls):
            tool_name, args, tid = tc['tool_name'], tc['args'], tc.get('id', '')

            if tool_name != 'no_tool':
                logger.debug(f"Tool: {tool_name}")
                if handler.events:
                    handler.events.emit(AgentEvent.TOOL_CALL, {"name": tool_name, "summary": _compact_args(args)})

            outcome = handler.dispatch(tool_name, args, response, index=ii)

            if outcome.should_exit:
                exit_reason = {'result': 'EXITED', 'data': outcome.data}
                break
            if not outcome.next_prompt:
                exit_reason = {'result': 'CURRENT_TASK_DONE', 'data': outcome.data}
                break

            if outcome.data is not None and tool_name != 'no_tool':
                datastr = json.dumps(outcome.data, ensure_ascii=False) if isinstance(outcome.data, (dict, list)) else str(outcome.data)
                tool_results.append({'tool_use_id': tid, 'content': datastr})
                # Emit tool result event for TUI — extract msg for cleaner display
                if handler.events:
                    result_data = outcome.data if isinstance(outcome.data, dict) else {}
                    display_msg = result_data.get("msg", "") or str(outcome.data)[:100]
                    handler.events.emit(AgentEvent.TOOL_RESULT, {"name": tool_name, "summary": display_msg, "status": "success" if result_data.get("status") == "success" else "done"})

            next_prompts.add(outcome.next_prompt)

        if not next_prompts or exit_reason:
            break

        # Build the next user message with tool results
        next_prompt = handler.turn_end_callback(
            response, tool_calls, tool_results, turn,
            '\n'.join(next_prompts), exit_reason
        )

        # Record session turn for detailed history
        if session_id and memory:
            assistant_text = response.content or ""
            thinking_text = ""
            if "<thinking>" in assistant_text and "</thinking>" in assistant_text:
                import re as _re
                _tm = _re.search(r"<thinking>(.*?)</thinking>", assistant_text, _re.DOTALL)
                if _tm:
                    thinking_text = _tm.group(1).strip()
            for tc in tool_calls:
                if tc['tool_name'] != 'no_tool':
                    memory.session_turn_add(
                        session_id=session_id, turn_num=turn, role='assistant',
                        content=assistant_text[:2000], tool_name=tc['tool_name'],
                        tool_args=json.dumps(tc['args'], ensure_ascii=False)[:2000],
                        thinking=thinking_text[:500]
                    )
            # Record tool results as user role turns
            for tr in tool_results:
                memory.session_turn_add(
                    session_id=session_id, turn_num=turn, role='user',
                    tool_result=tr['content'][:2000]
                )

        # Build user content blocks: tool_results + continuation text
        user_content = []
        for tr in tool_results:
            user_content.append({
                "type": "tool_result",
                "tool_use_id": tr['tool_use_id'],
                "content": tr['content']
            })
        # Add continuation prompt as text
        user_content.append({"type": "text", "text": next_prompt})

        messages.append({"role": "user", "content": user_content})

    if exit_reason:
        handler.turn_end_callback(response, tool_calls, tool_results, turn, '', exit_reason)
        if handler.events:
            handler.events.emit(AgentEvent.AGENT_DONE, {"result": exit_reason.get('result', 'UNKNOWN')})
    elif not exit_reason:
        if handler.events:
            handler.events.emit(AgentEvent.AGENT_DONE, {"result": "MAX_TURNS_EXCEEDED"})

    return exit_reason or {'result': 'MAX_TURNS_EXCEEDED'}


def _compact_args(args):
    a = {k: v for k, v in args.items() if k != '_index'}
    s = json.dumps(a, ensure_ascii=False)
    return s[:200] + '...' if len(s) > 200 else s