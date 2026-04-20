"""Nova Agent — self-evolving AI agent main orchestrator.

Unified memory: single SQLite DB (~/.nova/nova.db) with project_id scoping.
"""

import json
import logging
import os
import re
import sys
import threading
import time
import queue

from nova import __version__
from nova.agent_loop import BaseHandler, StepOutcome, agent_runner_loop
from nova.events import EventBus
from nova.llmcore import LLMClient, create_client_from_config
from nova.tools.handler import NovaHandler, smart_format, get_global_memory
from nova.memory.engine import NovaMemory
from nova.context.system_prompt import build_system_prompt
from nova.cron import NovaCron
from nova.autonomous import AutonomousMonitor

logger = logging.getLogger("nova")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME_DIR = os.path.expanduser('~')


LEARN_QUALITY_GUIDELINES = (
    "# LEARN MODE — Project Knowledge Generation\n\n"
    "Generate structured project knowledge from project scan data.\n"
    "Follow these quality guidelines:\n\n"
    "**Facts** — must be actionable with specific values, not just descriptions:\n"
    "  Good: 'Gateway module connects to OpenClaw devices on WebSocket port 18789, supports loopback/LAN bindings'\n"
    "  Bad: 'Backend has a gateway module'\n"
    "  Include: ports, paths, commands, thresholds, version numbers, config values\n\n"
    "**Wiki pages** — must include concrete details:\n"
    "  Include: tables with routes/components, file paths, configuration values, build commands, framework versions\n\n"
    "**Skills** — must have imperative steps with specific commands:\n"
    "  Good: '1. npm run build --workspace=@clawmemaybe/shared' (specific command)\n"
    "  Bad: '1. Build the project' (vague)\n"
    "  Include: pitfalls with concrete failure modes\n\n"
    "**Dedup**: Before creating each knowledge item, search existing knowledge (fact_search, wiki_query, skill_search) "
    "to check if similar knowledge already exists. Skip creating items that are already well-covered.\n\n"
    "When scan data is provided, create facts, wiki pages, and skills about the project. Prioritize actionable knowledge."
)


def build_learn_prompt(scan):
    """Build a learn prompt: quality guidelines (from contract) + dynamic scan data."""
    prompt = LEARN_QUALITY_GUIDELINES + "\n\nHere is the scan data:\n\n"

    # Core scan fields
    prompt += f"Project: {scan['project_name']}\n"
    prompt += f"Language: {scan['language']}\n"
    prompt += f"Test framework: {scan['test_framework']}\n"
    prompt += f"Lint tool: {scan['lint_tool']}\n"
    prompt += f"Key directories: {scan['key_dirs']}\n"
    prompt += f"Files found: {len(scan['file_tree'])}\n"

    if scan['readme']:
        prompt += f"\n## README\n{scan['readme'][:4000]}\n"
    if scan['package_config']:
        prompt += f"\n## Root Package Config\n{scan['package_config'][:3000]}\n"
    if scan['project_knowledge_files']:
        prompt += f"\n## Project Knowledge Files (CLAUDE.md, AGENTS.md)\n{scan['project_knowledge_files'][:6000]}\n"
    if scan['workspace_configs']:
        prompt += f"\n## Workspace/Package Configs\n{scan['workspace_configs'][:4000]}\n"
    if scan['infrastructure_configs']:
        prompt += f"\n## Infrastructure Configs (Docker, tsconfig, .env)\n{scan['infrastructure_configs'][:3000]}\n"
    if scan['ci_configs']:
        prompt += f"\n## CI Configs\n{scan['ci_configs'][:2000]}\n"

    return prompt


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
    """The main agent class — orchestrates LLM, handler, and unified memory."""

    def __init__(self, project_root=None):
        self.client = create_client_from_config()
        self.tools_schema = load_tool_schema()

        # Unified memory: single DB with project_id scoping
        global_db = os.path.join(HOME_DIR, '.nova', 'nova.db')
        self.memory = NovaMemory(global_db)
        self.current_project_id = None
        self._register_builtin_skills()
        self.lock = threading.Lock()
        self.task_dir = os.path.join(HOME_DIR, '.nova', 'temp')
        os.makedirs(self.task_dir, exist_ok=True)
        self.history = []
        self.task_queue = queue.Queue()
        self._busy = threading.Event()
        self.stop_sig = False
        self.handler = None
        self.verbose = True
        self.events = EventBus()
        self.cron = NovaCron(self)
        self.autonomous = AutonomousMonitor(self)

    @property
    def is_running(self):
        """Thread-safe read of agent busy state."""
        return self._busy.is_set()

    def _register_builtin_skills(self):
        """Auto-register built-in command contract skills."""
        from nova.brainstorm import build_brainstorm_prompt
        try:
            self.memory.skill_add(
                name="brainstorm",
                description="Socratic interview with mathematical ambiguity scoring for crystallizing ideas into specs",
                steps=[], triggers="brainstorm,socratic,interview,ambiguity,spec,clarify",
                tags="brainstorm,contract,command",
                contract=build_brainstorm_prompt(None)
            )
        except Exception:
            pass  # Skill already exists (UPSERT handles it, but just in case)
        try:
            self.memory.skill_add(
                name="learn",
                description="Project knowledge generation — scan a project directory and create facts, wiki pages, and skills",
                steps=[], triggers="learn,scan,project,knowledge,analyze,digest",
                tags="learn,contract,command",
                contract=LEARN_QUALITY_GUIDELINES
            )
        except Exception:
            pass

    def abort(self):
        if not self._busy.is_set():
            return
        logger.info('Aborting current task...')
        self.events.emit("status", "Aborting current task...")
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

            sys_prompt = build_system_prompt(self.memory, raw_query)

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
                    if sk.get('contract'):
                        skill_context += f"\n[Contract Skill — {sk['name']}]\n"
                        skill_context += sk['contract']
                        skill_context += f"\n[End Contract Skill]\n"
                    else:
                        scope = 'global' if sk.get('project_id') is None else 'project'
                        skill_context += f"  **{sk['name']}** (v{sk.get('version', 1)}, success: {sk['success_rate']:.0%}) [{scope}]\n"
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

                # Save handler history for cross-session context (not in display output)
                self.history = handler.history_info

                display_queue.put({'next': full_resp, 'source': source})
                display_queue.put({'done': full_resp, 'source': source})

                # Update skill success rates based on session outcome
                session_success = result.get('result', '') not in ('MAX_TURNS_EXCEEDED', 'EXITED')
                matched_skills = self.memory.skill_match(raw_query)
                for sk in matched_skills:
                    self.memory.skill_update_success(sk['name'], success=session_success)

            except Exception as e:
                display_queue.put({'done': f'Error: {e}', 'source': source})
            finally:
                self._busy.clear()
                self.stop_sig = False
                self.task_queue.task_done()

    def run_interactive(self):
        """Interactive CLI mode — Rich + prompt_toolkit REPL."""
        from nova.tui.app import NovaApp
        app = NovaApp(self)
        app.run()

    def _run_raw_repl(self):
        """Raw REPL fallback for terminals that can't run Textual."""
        self.verbose = True
        threading.Thread(target=self.run, daemon=True).start()
        self.cron.start()
        self.autonomous.start()

        stats = self.memory.stats()
        print(f"Nova Agent v{__version__} — Self-evolving AI assistant (Unified SQL+Wiki memory)")
        print(f"Total: {stats['total_wiki_pages']} wiki | {stats['total_facts']} facts | {stats['total_skills']} skills | {stats['total_sessions']} sessions")
        print(f"Global: {stats['global_wiki_pages']} wiki | {stats['global_facts']} facts | {stats['global_skills']} skills")
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
                print(f"[Stats] Wiki: {stats['total_wiki_pages']} | Facts: {stats['total_facts']} | Skills: {stats['total_skills']} | Sessions: {stats['total_sessions']} | Trust: {stats['avg_trust']:.2f}")
                if stats['current_project']:
                    print(f"  Project: {stats['current_project']} — {stats['project_facts']} facts, {stats['project_skills']} skills")
                continue
            if q == '/wiki':
                pages = self.memory.wiki_list()
                for p in pages:
                    scope = 'global' if p.get('project_id') is None else 'project'
                    print(f"  [{scope}] [{p['category']}] {p['title']} (tags: {p['tags']})")
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
                todo = self.memory.wiki_read('autonomous-todo')
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
                    recent = self.memory._conn.execute(
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
            if q == '/learn':
                project_root = os.getcwd()
                project_name = os.path.basename(project_root)
                scan = self.memory.project_scan(project_root, depth='standard')
                # Auto-create project scope from directory name
                try:
                    pid = self.memory.project_create(project_name, f"Auto-learned from {project_root}")
                    self.memory.project_select(pid)
                    self.current_project_id = pid
                    print(f"Created project scope '{project_name}' (id={pid})")
                except Exception:
                    print(f"Project '{project_name}' already exists, using existing scope")
                    projects = self.memory.project_list()
                    for p in projects:
                        if p['name'] == project_name:
                            self.memory.project_select(p['id'])
                            self.current_project_id = p['id']
                            break
                learn_prompt = build_learn_prompt(scan)
                dq = self.put_task(learn_prompt)
                while True:
                    try:
                        item = dq.get(timeout=600)
                    except queue.Empty:
                        break
                    if 'done' in item:
                        print(f"[Sync] Knowledge generation complete.")
                        stats = self.memory.stats()
                        print(f"[Stats] Facts: {stats['total_facts']} | Skills: {stats['total_skills']} | Wiki: {stats['total_wiki_pages']}")
                        break
                    if 'next' in item:
                        print(item['next'][:200], end='', flush=True)
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