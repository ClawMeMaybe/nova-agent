"""Nova Agent — self-evolving AI agent main orchestrator.

Two-tier memory: project-local (<cwd>/.nova/nova.db) + global (~/.nova/nova.db).
"""

import json
import os
import re
import sys
import threading
import time
import queue

from nova import __version__
from nova.agent_loop import BaseHandler, StepOutcome, agent_runner_loop
from nova.llmcore import LLMClient, create_client_from_config
from nova.tools.handler import NovaHandler, smart_format, get_global_memory
from nova.memory.engine import TwoTierMemory
from nova.context.system_prompt import build_system_prompt
from nova.cron import NovaCron
from nova.autonomous import AutonomousMonitor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME_DIR = os.path.expanduser('~')


def _resolve_project_root():
    """Determine project root from cwd or NOVA_PROJECT_ROOT env var."""
    env_root = os.environ.get('NOVA_PROJECT_ROOT')
    if env_root and os.path.isdir(env_root):
        return env_root
    return os.getcwd()


def load_tool_schema():
    """Load tool definitions from schema file."""
    schema_path = os.path.join(SCRIPT_DIR, 'assets', 'tools_schema.json')
    with open(schema_path, 'r', encoding='utf-8') as f:
        return json.loads(f.read())


class NovaAgent:
    """The main agent class — orchestrates LLM, handler, and two-tier memory."""

    def __init__(self, project_root=None):
        self.client = create_client_from_config()
        self.tools_schema = load_tool_schema()

        # Two-tier memory paths
        project_root = project_root or _resolve_project_root()
        local_db = os.path.join(project_root, '.nova', 'nova.db')
        global_db = os.path.join(HOME_DIR, '.nova', 'nova.db')

        self.memory = TwoTierMemory(local_db, global_db)
        self.lock = threading.Lock()
        self.task_dir = os.path.join(project_root, '.nova', 'temp')
        os.makedirs(self.task_dir, exist_ok=True)
        self.history = []
        self.task_queue = queue.Queue()
        self._busy = threading.Event()  # Thread-safe: set when running, clear when done
        self.stop_sig = False
        self.handler = None
        self.verbose = True
        self.cron = NovaCron(self)
        self.autonomous = AutonomousMonitor(self)

    @property
    def is_running(self):
        """Thread-safe read of agent busy state."""
        return self._busy.is_set()

    def abort(self):
        if not self._busy.is_set():
            return
        print('Aborting current task...')
        self.stop_sig = True
        if self.handler:
            self.handler.code_stop_signal.append(1)

    def put_task(self, query, source="user", images=None):
        display_queue = queue.Queue()
        self.task_queue.put({
            "query": query, "source": source,
            "images": images or [], "output": display_queue
        })
        return display_queue

    def run(self):
        """Main agent loop — processes tasks from queue."""
        while True:
            task = self.task_queue.get()
            raw_query = task["query"]
            source = task["source"]
            display_queue = task["output"]

            self._busy.set()
            if source == "user":
                self.autonomous.mark_activity()

            # Create session record at task start for detailed turn tracking
            session_id = self.memory.session_create(raw_query)

            sys_prompt = build_system_prompt(self.memory)

            # Proactive recall: inject relevant prior knowledge into the task
            recall_context = self.memory.proactive_recall(raw_query)
            user_input = raw_query
            if recall_context:
                user_input = recall_context + "\n\n" + raw_query

            # Proactive skill matching: suggest relevant SOPs for this task
            skill_matches = self.memory.skill_match(raw_query)
            if skill_matches:
                skill_context = "\n[Relevant Skills — proven workflows for this task]\n"
                for sk in skill_matches:
                    tier = sk.get('_tier', 'global')
                    skill_context += f"  **{sk['name']}** (v{sk.get('version', 1)}, success: {sk['success_rate']:.0%}) [{tier}]\n"
                    skill_context += f"    Triggers: {sk.get('triggers', '')}\n"
                    for step in sk['steps'][:4]:
                        skill_context += f"    {step}\n"
                    if len(sk['steps']) > 4:
                        skill_context += f"    ... ({len(sk['steps']) - 4} more steps)\n"
                    pitfalls = sk.get('pitfalls', [])
                    if pitfalls:
                        skill_context += f"    Pitfalls: {'; '.join(pitfalls[:2])}\n"
                skill_context += "  Use these steps as a guide — adapt to your specific situation.\n"
                user_input = skill_context + "\n\n" + user_input

            # Proactive session context: inject relevant past session turns
            session_context = self.memory.session_relevant_turns(raw_query)
            if session_context:
                user_input = session_context + "\n\n" + user_input

            handler = NovaHandler(self, self.history, self.task_dir, session_id=session_id)

            if self.handler and 'key_info' in self.handler.working:
                ki = self.handler.working['key_info']
                handler.working['key_info'] = ki
                handler.working['passed_sessions'] = self.handler.working.get('passed_sessions', 0) + 1

            with self.lock:
                self.handler = handler

            try:
                result = agent_runner_loop(
                    self.client, sys_prompt, user_input,
                    handler, self.tools_schema, max_turns=40,
                    session_id=session_id, memory=self.memory
                )

                # Extract final response from the result dict
                result_data = result.get('data', '')
                if hasattr(result_data, 'content'):
                    # LLMResponse object — extract the text content
                    full_resp = result_data.content or ''
                elif isinstance(result_data, dict):
                    full_resp = json.dumps(result_data, ensure_ascii=False)
                else:
                    full_resp = str(result_data) if result_data else ''

                # Use handler history for rich context
                handler_summary = handler.history_info
                if handler_summary:
                    full_resp = '\n'.join(handler_summary) + '\n\n' + full_resp

                display_queue.put({'next': full_resp, 'source': source})
                display_queue.put({'done': full_resp, 'source': source})
                self.history = handler.history_info

            except Exception as e:
                display_queue.put({'done': f'Error: {e}', 'source': source})
            finally:
                self._busy.clear()
                self.stop_sig = False
                self.task_queue.task_done()

    def run_interactive(self):
        """Interactive CLI mode."""
        self.verbose = True
        threading.Thread(target=self.run, daemon=True).start()
        self.cron.start()
        self.autonomous.start()

        stats = self.memory.stats()
        print(f"Nova Agent v{__version__} — Self-evolving AI assistant (Two-tier SQL+Wiki memory)")
        print(f"Local: {stats['local_wiki_pages']} wiki | {stats['local_facts']} facts | {stats['local_skills']} skills | {stats['local_sessions']} sessions")
        print(f"Global: {stats['global_wiki_pages']} wiki | {stats['global_facts']} facts | {stats['global_skills']} skills | {stats['global_sessions']} sessions")
        print("Type your message, /cron for scheduled jobs, /todo for tasks, or /quit to exit.\n")

        while True:
            try:
                q = input('nova> ').strip()
            except (EOFError, KeyboardInterrupt):
                print('\nGoodbye!')
                break

            if not q:
                continue
            if q == '/quit':
                break
            if q == '/stats':
                stats = self.memory.stats()
                print(f"[Local] Wiki: {stats['local_wiki_pages']} | Facts: {stats['local_facts']} | Skills: {stats['local_skills']} | Sessions: {stats['local_sessions']} | Trust: {stats['local_avg_trust']:.2f}")
                print(f"[Global] Wiki: {stats['global_wiki_pages']} | Facts: {stats['global_facts']} | Skills: {stats['global_skills']} | Sessions: {stats['global_sessions']} | Trust: {stats['global_avg_trust']:.2f}")
                continue
            if q == '/wiki':
                pages = self.memory.wiki_list()
                for p in pages:
                    tier = p.get('_tier', '?')
                    print(f"  [{tier}] [{p['category']}] {p['title']} (tags: {p['tags']})")
                continue
            if q == '/cron':
                from nova.cron.jobs import list_jobs
                jobs = list_jobs()
                if not jobs:
                    print("No cron jobs configured.")
                else:
                    for j in jobs:
                        status = "enabled" if j.get('enabled', True) else "disabled"
                        print(f"  {j['id']}: {j['name']} ({j['schedule']['kind']}) next={j['next_run_at']} runs={j['completed_count']} {status}")
                continue
            if q == '/todo':
                todo = self.memory.wiki_read('autonomous-todo', tier='global')
                if todo and todo.get('content'):
                    print("Autonomous TODO:")
                    print(todo['content'])
                else:
                    print("No autonomous TODO yet. Agent will create one during idle self-improvement.")
                continue
            if q == '/evolve':
                score, trend = self.memory.evolution_score()
                trend_arrow = '↑' if trend > 0 else '↓' if trend < 0 else '—'
                print(f"Evolution Score: {score:.2f} (trend: {trend_arrow})")
                try:
                    recent = self.memory._local._conn.execute(
                        "SELECT loss_task, loss_efficiency, loss_recurrence, loss_knowledge_quality, "
                        "loss_total, evolution_score, created_at "
                        "FROM evolution_log ORDER BY created_at DESC LIMIT 5"
                    ).fetchall()
                    if recent:
                        for r in recent:
                            print(f"  [{r['created_at'][:10]}] L_task={r['loss_task']:.1f} L_eff={r['loss_efficiency']:.2f} "
                                  f"L_rec={r['loss_recurrence']:.2f} L_kq={r['loss_knowledge_quality']:.2f} "
                                  f"L_total={r['loss_total']:.2f} score={r['evolution_score']:.2f}")
                    else:
                        print("  No evolution log entries yet. Complete a task to start tracking.")
                except Exception:
                    print("  Evolution log not yet available (needs V4 schema migration).")
                continue

            dq = self.put_task(q)
            while True:
                item = dq.get()
                if 'next' in item:
                    print(item['next'], end='', flush=True)
                if 'done' in item:
                    print()
                    break


def main():
    """Entry point for the nova command."""
    agent = NovaAgent()
    agent.run_interactive()


if __name__ == '__main__':
    main()