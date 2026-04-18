"""Nova tool handler — implements all atomic tools via do_ dispatch pattern.

Inspired by GenericAgent's GenericAgentHandler with enhancements from Hermes.
9 atomic tools + 2 memory tools + 4 wiki/fact tools = self-evolving + compounding capability.
"""

import json
import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import difflib
import itertools
import collections

from nova.agent_loop import BaseHandler, StepOutcome
from nova.events import AgentEvent
from nova.memory.engine import TwoTierMemory

logger = logging.getLogger("nova")


def smart_format(data, max_str_len=100, omit_str=' ... '):
    if not isinstance(data, str):
        data = str(data)
    if len(data) < max_str_len + len(omit_str) * 2:
        return data
    return f"{data[:max_str_len // 2]}{omit_str}{data[-max_str_len // 2:]}"


def format_error(e):
    exc_type, exc_value, exc_tb = sys.exc_info()
    tb = traceback.extract_tb(exc_tb)
    if tb:
        f = tb[-1]
        return f"{exc_type.__name__}: {str(e)} @ {os.path.basename(f.filename)}:{f.lineno}"
    return f"{exc_type.__name__}: {str(e)}"


def code_run(code, code_type="python", timeout=60, cwd=None, stop_signal=[]):
    """Execute code — python scripts or bash commands."""
    preview = (code[:60].replace('\n', ' ') + '...') if len(code) > 60 else code.strip()
    logger.debug(f"Running {code_type}: {preview}")

    if code_type == "python":
        tmp_file = tempfile.NamedTemporaryFile(suffix=".nova.py", delete=False, mode='w', encoding='utf-8')
        tmp_file.write(code)
        tmp_path = tmp_file.name
        tmp_file.close()
        cmd = [sys.executable, "-u", tmp_path]
    elif code_type in ("bash", "shell"):
        cmd = ["bash", "-c", code]
    else:
        return {"status": "error", "msg": f"Unsupported type: {code_type}"}

    full_stdout = []

    def stream_reader(proc, logs):
        try:
            for line_bytes in iter(proc.stdout.readline, b''):
                line = line_bytes.decode('utf-8', errors='replace')
                logs.append(line)
        except:
            pass

    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, cwd=cwd
        )
        start_t = time.time()
        t = threading.Thread(target=stream_reader, args=(process, full_stdout), daemon=True)
        t.start()

        while t.is_alive():
            if time.time() - start_t > timeout or len(stop_signal) > 0:
                process.kill()
                if time.time() - start_t > timeout:
                    full_stdout.append("\n[Timeout Error] Process killed")
                else:
                    full_stdout.append("\n[Stopped] User aborted")
                break
            time.sleep(1)

        t.join(timeout=1)
        exit_code = process.poll()
        stdout_str = "".join(full_stdout)
        status = "success" if exit_code == 0 else "error"

        return {
            "status": status,
            "stdout": smart_format(stdout_str, max_str_len=6000),
            "exit_code": exit_code
        }
    except Exception as e:
        if 'process' in locals():
            process.kill()
        return {"status": "error", "msg": str(e)}
    finally:
        if code_type == "python" and 'tmp_path' in locals() and os.path.exists(tmp_path):
            os.remove(tmp_path)


def ask_user(question, candidates=None):
    """Interrupt for human input."""
    return {
        "status": "INTERRUPT",
        "intent": "HUMAN_INTERVENTION",
        "data": {"question": question, "candidates": candidates or []}
    }


def file_read(path, start=1, keyword=None, count=200, show_linenos=True):
    """Read file content with line numbers, keyword search, and smart truncation."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            stream = ((i, l.rstrip('\r\n')) for i, l in enumerate(f, 1))
            stream = itertools.dropwhile(lambda x: x[0] < start, stream)
            if keyword:
                before = collections.deque(maxlen=count // 3)
                for i, l in stream:
                    if keyword.lower() in l.lower():
                        res = list(before) + [(i, l)] + list(itertools.islice(stream, count - len(before) - 1))
                        break
                    before.append((i, l))
                else:
                    return f"Keyword '{keyword}' not found after line {start}."
            else:
                res = list(itertools.islice(stream, count))

            result = "\n".join(f"{i}|{l}" if show_linenos else l for i, l in res)
            return result
    except FileNotFoundError:
        return f"File not found: {path}"
    except Exception as e:
        return f"Error: {str(e)}"


def file_write(path, content, mode="overwrite"):
    """Write content to file."""
    try:
        dirpath = os.path.dirname(path)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        with open(path, 'a' if mode == 'append' else 'w', encoding='utf-8') as f:
            f.write(content)
        return {"status": "success", "msg": f"Written {len(content)} bytes to {path}", "bytes": len(content)}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def file_patch(path, old_content, new_content):
    """Patch file by replacing unique old_content block with new_content."""
    try:
        if not os.path.exists(path):
            return {"status": "error", "msg": "File not found"}
        with open(path, 'r', encoding='utf-8') as f:
            full_text = f.read()
        if not old_content:
            return {"status": "error", "msg": "old_content is empty"}
        count = full_text.count(old_content)
        if count == 0:
            return {"status": "error", "msg": "old_content not found. Use file_read first to verify content."}
        if count > 1:
            return {"status": "error", "msg": f"Found {count} matches — provide more specific context for uniqueness."}
        updated = full_text.replace(old_content, new_content)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(updated)
        return {"status": "success", "msg": "File patched successfully"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def get_global_memory():
    """Get memory context for injection into prompts."""
    try:
        project_root = os.environ.get('NOVA_PROJECT_ROOT', os.getcwd())
        local_db = os.path.join(project_root, '.nova', 'nova.db')
        global_db = os.path.join(os.path.expanduser('~'), '.nova', 'nova.db')
        engine = TwoTierMemory(local_db, global_db)
        try:
            ctx = engine.build_context_prompt()
        finally:
            engine.close()
        return ctx
    except Exception:
        return ""


class NovaHandler(BaseHandler):
    """Nova Agent tool handler — implements all atomic + memory + wiki/fact tools."""

    def __init__(self, parent, last_history=None, cwd='./temp', session_id=None):
        self.parent = parent
        self.working = {}
        self.cwd = cwd
        self.current_turn = 0
        self.history_info = last_history if last_history else []
        self.code_stop_signal = []
        self.memory = parent.memory
        self.events = parent.events if hasattr(parent, 'events') else None
        self._ask_response_queue = queue.Queue()
        self._accessed_fact_ids = []
        self._accessed_skill_names = []
        self._session_id = session_id

    def _get_abs_path(self, path):
        if not path:
            return ""
        return os.path.abspath(os.path.join(self.cwd, path))

    def _get_anchor_prompt(self, skip=False):
        if skip:
            return "\n"
        h_str = "\n".join(self.history_info[-20:])
        prompt = f"\n### [WORKING MEMORY]\n<history>\n{h_str}\n</history>"
        prompt += f"\nCurrent turn: {self.current_turn}\n"
        if self.working.get('key_info'):
            prompt += f"\n<key_info>{self.working.get('key_info')}</key_info>"
        return prompt

    # ── Atomic Tools ──

    def do_code_run(self, args, response):
        """Execute code — python scripts or shell commands."""
        code_type = args.get("type", "python")
        code = args.get("code") or args.get("script")
        if not code:
            return StepOutcome("[Error] Code missing. Provide 'script' argument.", next_prompt="\n")
        timeout = args.get("timeout", 60)
        cwd = os.path.normpath(os.path.abspath(os.path.join(self.cwd, args.get("cwd", './'))))

        result = code_run(code, code_type, timeout, cwd, stop_signal=self.code_stop_signal)
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_ask_user(self, args, response):
        """Ask the user a question — human-in-the-loop."""
        question = args.get("question", "Please provide input:")
        candidates = args.get("candidates", [])

        # Emit event for TUI dialog — agent waits for response
        if self.events:
            self.events.emit(AgentEvent.ASK_USER, {"question": question, "candidates": candidates})
            # Wait for user answer from TUI dialog (timeout 300s)
            try:
                answer = self._ask_response_queue.get(timeout=300)
                if answer == "__timeout__":
                    result = ask_user(question, candidates)
                    return StepOutcome(result, next_prompt="\n[System] User did not respond. Proceeding with default.", should_exit=True)
                # Inject answer as continuation prompt
                return StepOutcome({"status": "answered", "answer": answer}, next_prompt=f"\n[User Response] {answer}\n")
            except queue.Empty:
                result = ask_user(question, candidates)
                return StepOutcome(result, next_prompt="", should_exit=True)
        else:
            # Raw REPL mode — use original behavior
            result = ask_user(question, candidates)
            return StepOutcome(result, next_prompt="", should_exit=True)

    def do_file_read(self, args, response):
        """Read file content with optional keyword search."""
        path = self._get_abs_path(args.get("path", ""))
        start = args.get("start", 1)
        count = args.get("count", 200)
        keyword = args.get("keyword")
        show_linenos = args.get("show_linenos", True)

        result = file_read(path, start=start, keyword=keyword, count=count, show_linenos=show_linenos)
        result = smart_format(result, max_str_len=20000)
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_file_write(self, args, response):
        """Write content to a file."""
        path = self._get_abs_path(args.get("path", ""))
        content = args.get("content", "")
        mode = args.get("mode", "overwrite")

        if not content:
            return StepOutcome({"status": "error", "msg": "No content provided"}, next_prompt="\n")

        result = file_write(path, content, mode=mode)
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_file_patch(self, args, response):
        """Patch a file — replace a unique block of old content with new."""
        path = self._get_abs_path(args.get("path", ""))
        old_content = args.get("old_content", "")
        new_content = args.get("new_content", "")

        result = file_patch(path, old_content, new_content)
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_web_scan(self, args, response):
        """Scan current browser page content and tabs."""
        return StepOutcome(
            {"status": "info", "msg": "Web scanning requires browser integration. Use code_run with requests/beautifulsoup4 for simple web tasks."},
            next_prompt=self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        )

    def do_web_execute_js(self, args, response):
        """Execute JavaScript in browser."""
        return StepOutcome(
            {"status": "info", "msg": "JS execution requires browser integration. Use code_run with selenium/playwright for web automation."},
            next_prompt=self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        )

    # ── Memory Tools ──

    def do_update_working_checkpoint(self, args, response):
        """Update working memory — key info for the current task."""
        key_info = args.get("key_info", "")
        related_sop = args.get("related_sop", "")
        if key_info:
            self.working['key_info'] = key_info
        if related_sop:
            self.working['related_sop'] = related_sop
        self.working['passed_sessions'] = 0
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome({"result": "working key_info updated"}, next_prompt=next_prompt)

    def do_start_long_term_update(self, args, response):
        """Distill task experience into long-term memory — wiki + facts."""
        summary = args.get("summary", "")
        category = args.get("category", "insight")

        prompt = "### [Distill Experience] Extract verified, durable knowledge from this task:\n"
        prompt += "- **Quick facts** (paths/credentials/config) → use fact_add\n"
        prompt += "- **Rich knowledge** (architecture decisions, debugging patterns) → use wiki_ingest\n"
        prompt += "- **Repeatable workflows** (multi-step procedures you'd do again) → use skill_add with numbered steps, triggers, and pitfalls\n"
        prompt += "- **Complex task experience** (key pitfalls/workflows) → use wiki_ingest with category=pattern\n"
        prompt += "- **Prohibited**: temporary variables, unverified info, common knowledge.\n"
        prompt += f"\nCurrent category hint: {category}\n"
        prompt += get_global_memory()
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome({"action": "start_memory_update"}, next_prompt=prompt)

    # ── Wiki/Fact Tools (Karpathy's compounding + Hermes trust) ──

    def do_wiki_ingest(self, args, response):
        """Ingest knowledge into wiki — the compounding mechanism."""
        title = args.get("title", "Untitled")
        content = args.get("content", "")
        tags = args.get("tags", "")
        category = args.get("category", "reference")
        confidence = args.get("confidence", "medium")
        tier = args.get("tier", "auto")

        if not content:
            return StepOutcome({"status": "error", "msg": "No content provided"}, next_prompt="\n")

        try:
            page_id = self.memory.wiki_ingest(
                title, content, tags,
                category=category, confidence=confidence,
                tier=tier
            )
            result = {
                "status": "success",
                "msg": f"Wiki page created/updated (id={page_id}). Knowledge compounds across sessions.",
                "page_id": page_id,
            }
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_wiki_query(self, args, response):
        """Search wiki pages — recall accumulated knowledge from past sessions."""
        query = args.get("query", "")
        category = args.get("category")
        tags = args.get("tags")
        tier = args.get("tier", "auto")

        if not query:
            return StepOutcome({"status": "error", "msg": "Query required"}, next_prompt="\n")

        try:
            pages = self.memory.wiki_query(query, category=category, tags=tags, tier=tier)
            if not pages:
                result = {"status": "no_results", "msg": f"No wiki pages matching '{query}'"}
            else:
                summaries = []
                for p in pages:
                    s = f"**{p['title']}** [{p['category']}] (tags: {p['tags']})\n"
                    # Show first 200 chars of content as snippet
                    snippet = p['content'][:200].replace('\n', ' ')
                    s += f"  > {snippet}...\n"
                    if p.get('cross_refs'):
                        s += f"  refs: {', '.join(p['cross_refs'])}\n"
                    summaries.append(s)
                result = {
                    "status": "success",
                    "msg": f"Found {len(pages)} wiki pages",
                    "pages": summaries,
                }
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_fact_add(self, args, response):
        """Add a verified fact to the knowledge base."""
        content = args.get("content", "")
        category = args.get("category", "general")
        tags = args.get("tags", "")
        tier = args.get("tier", "auto")

        if not content:
            return StepOutcome({"status": "error", "msg": "Fact content required"}, next_prompt="\n")

        try:
            fact_id = self.memory.fact_add(content, category=category, tags=tags, tier=tier)
            result = {
                "status": "success",
                "msg": f"Fact added (id={fact_id}). Trust score starts at 0.5, evolves with use.",
                "fact_id": fact_id,
            }
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_fact_search(self, args, response):
        """Search facts — trust-ranked retrieval of verified knowledge."""
        query = args.get("query", "")
        category = args.get("category")
        min_trust = args.get("min_trust", 0.3)
        tier = args.get("tier", "auto")

        if not query:
            return StepOutcome({"status": "error", "msg": "Query required"}, next_prompt="\n")

        try:
            facts = self.memory.fact_search(query, category=category, min_trust=min_trust, tier=tier)
            # Track accessed fact IDs for trust feedback
            for f in facts:
                if 'id' in f:
                    self._accessed_fact_ids.append(f['id'])
            if not facts:
                result = {"status": "no_results", "msg": f"No facts matching '{query}' above trust {min_trust}"}
            else:
                summaries = []
                for f in facts:
                    summaries.append(f"[{f['category']}] {f['content']} (trust: {f['trust_score']:.2f}, tags: {f['tags']})")
                result = {
                    "status": "success",
                    "msg": f"Found {len(facts)} facts",
                    "facts": summaries,
                }
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    # ── SQL Sandbox Tools (LLM-as-DBA) ──

    def do_db_query(self, args, response):
        """Execute sandboxed SQL against the knowledge database."""
        sql = args.get("sql", "")
        tier = args.get("tier", "auto")

        if not sql:
            return StepOutcome({"status": "error", "msg": "SQL query required"}, next_prompt="\n")

        try:
            result = self.memory.safe_query(sql, tier=tier)
            # Track accessed fact IDs for trust feedback on SELECT
            if result.get('status') == 'success' and 'rows' in result and 'facts' in sql.lower():
                for row in result.get('rows', []):
                    if 'id' in row:
                        self._accessed_fact_ids.append(row['id'])
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_db_schema(self, args, response):
        """Inspect the knowledge database schema."""
        tier = args.get("tier", "auto")

        try:
            result = self.memory.get_schema_info(tier=tier)
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_wiki_export(self, args, response):
        """Export wiki pages to markdown files for human browsing."""
        tier = args.get("tier", "local")
        output_dir = args.get("output_dir") or os.path.join(self.cwd, ".nova", "wiki")

        try:
            pages = self.memory.wiki_list(tier=tier)
            exported = []
            for page in pages:
                full = self.memory.wiki_read(page['slug'], tier=tier)
                if full:
                    md_path = os.path.join(output_dir, f"{full['slug']}.md")
                    content = (
                        f"---\n"
                        f"title: {full['title']}\n"
                        f"category: {full['category']}\n"
                        f"tags: {full['tags']}\n"
                        f"confidence: {full['confidence']}\n"
                        f"---\n\n"
                        f"{full['content']}"
                    )
                    result = file_write(md_path, content, mode="overwrite")
                    if result.get("status") == "success":
                        exported.append(md_path)
            result = {
                "status": "success",
                "exported": exported,
                "count": len(exported),
                "output_dir": output_dir,
            }
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    # ── Cron Tool ──

    def do_cron(self, args, response):
        """Manage cron jobs: create, list, remove, run."""
        from nova.cron.jobs import create_job, list_jobs, get_job, remove_job

        action = args.get("action", "list")
        try:
            if action == "create":
                prompt = args.get("prompt", "")
                schedule = args.get("schedule", "")
                name = args.get("name")
                if not prompt or not schedule:
                    return StepOutcome({"status": "error", "msg": "prompt and schedule required for create"}, next_prompt="\n")
                job = create_job(prompt, schedule, name=name)
                result = {
                    "status": "success",
                    "msg": f"Cron job '{job['name']}' created (id={job['id']}, next_run={job['next_run_at']})",
                    "job": {
                        "id": job["id"],
                        "name": job["name"],
                        "schedule": job["schedule"],
                        "next_run_at": job["next_run_at"],
                    },
                }
            elif action == "list":
                jobs = list_jobs()
                summaries = []
                for j in jobs:
                    summaries.append(
                        f"  {j['id']}: {j['name']} ({j['schedule']['kind']}) next={j['next_run_at']} runs={j['completed_count']} enabled={j['enabled']}"
                    )
                result = {
                    "status": "success",
                    "msg": f"{len(jobs)} cron jobs",
                    "jobs": summaries,
                }
            elif action == "remove":
                job_id = args.get("job_id", "")
                if not job_id:
                    return StepOutcome({"status": "error", "msg": "job_id required for remove"}, next_prompt="\n")
                removed = remove_job(job_id)
                result = {"status": "success" if removed else "error", "msg": f"Job {job_id} removed" if removed else f"Job {job_id} not found"}
            elif action == "run":
                from nova.cron.scheduler import tick
                executed = tick(self.parent, verbose=True)
                result = {"status": "success", "msg": f"Executed {executed} jobs"}
            else:
                result = {"status": "error", "msg": f"Unknown action: {action}. Use create/list/remove/run."}
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    # ── Skill Tools ──

    def do_skill_add(self, args, response):
        """Crystallize a repeatable workflow into a skill/SOP."""
        name = args.get("name", "")
        description = args.get("description", "")
        steps = args.get("steps", [])
        triggers = args.get("triggers", "")
        pitfalls = args.get("pitfalls", [])
        tags = args.get("tags", "")
        tier = args.get("tier", "global")

        if not name or not steps or not triggers:
            return StepOutcome({"status": "error", "msg": "name, steps, and triggers are required"}, next_prompt="\n")

        try:
            skill_id = self.memory.skill_add(
                name, description, steps,
                tags=tags, triggers=triggers, pitfalls=pitfalls,
                tier=tier
            )
            result = {
                "status": "success",
                "msg": f"Skill '{name}' created (id={skill_id}, version=1). Triggers: {triggers}. Steps: {len(steps)}. Pitfalls: {len(pitfalls)}.",
                "skill_id": skill_id,
            }
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_skill_search(self, args, response):
        """Search for crystallized skills/SOPs by keywords."""
        query = args.get("query", "")
        min_success = args.get("min_success", 0.3)
        tier = args.get("tier", "global")

        if not query:
            return StepOutcome({"status": "error", "msg": "Query required"}, next_prompt="\n")

        try:
            skills = self.memory.skill_search(query, min_success=min_success, tier=tier)
            # Track accessed skill names for success feedback
            for s in skills:
                if 'name' in s:
                    self._accessed_skill_names.append(s['name'])
            if not skills:
                result = {"status": "no_results", "msg": f"No skills matching '{query}' above success {min_success}"}
            else:
                summaries = []
                for s in skills:
                    tier_label = s.get('_tier', tier)
                    summary = f"**{s['name']}** (v{s.get('version', 1)}, success: {s['success_rate']:.0%}, used {s['usage_count']}x) [{tier_label}]\n"
                    summary += f"  Description: {s['description']}\n"
                    summary += f"  Triggers: {s.get('triggers', '')}\n"
                    summary += f"  Steps:\n"
                    for step in s['steps'][:6]:
                        summary += f"    {step}\n"
                    if len(s['steps']) > 6:
                        summary += f"    ... ({len(s['steps']) - 6} more steps)\n"
                    pitfalls = s.get('pitfalls', [])
                    if pitfalls:
                        summary += f"  Pitfalls: {'; '.join(pitfalls[:3])}\n"
                    summaries.append(summary)
                result = {
                    "status": "success",
                    "msg": f"Found {len(skills)} skills",
                    "skills": summaries,
                }
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    # ── Meta tool ──

    def do_no_tool(self, args, response):
        """Called when LLM produces text without calling a tool."""
        content = getattr(response, 'content', '') or ""
        if not content.strip():
            return StepOutcome({}, next_prompt="[System] Blank response, regenerate and use a tool")

        code_blocks = re.findall(r"```[a-zA-Z]*\n[\s\S]{50,}?```", content)
        if len(code_blocks) == 1:
            return StepOutcome({}, next_prompt="[System] Detected code block without tool call. Please use code_run, file_write, or file_patch to execute it.")

        return StepOutcome(response, next_prompt=None)

    # ── Turn end ──

    def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason):
        content = getattr(response, 'content', '') or ""
        summary_match = re.search(r"<summary>(.*?)</summary>", content, re.DOTALL)
        if summary_match:
            summary = summary_match.group(1).strip()
        else:
            tc = tool_calls[0]
            tool_name = tc['tool_name']
            if tool_name == 'no_tool':
                summary = "Direct text response"
            else:
                summary = f"Called {tool_name}"

        summary = smart_format(summary, max_str_len=100)
        self.history_info.append(f'[Agent] {summary}')

        if turn % 7 == 0:
            next_prompt += f"\n[DANGER] Turn {turn}: If no progress, switch strategy or ask_user."

        # Trust decay on session end (lightweight maintenance)
        if exit_reason:
            try:
                self.memory._local.apply_time_decay()
                self.memory._global.apply_time_decay()
            except:
                pass

        # Auto-crystallize at session end (only on success)
        if exit_reason and exit_reason.get('result') in ('EXITED', 'CURRENT_TASK_DONE'):
            task_desc = self.history_info[0] if self.history_info else 'unknown task'
            data = exit_reason.get('data', '')
            if not isinstance(data, str):
                data = str(data)[:500]
            had_knowledge = self.memory._knowledge_produced
            if self._session_id:
                self.memory.session_update(
                    self._session_id, summary=task_desc, result=data,
                    had_knowledge=had_knowledge
                )
                self.memory.session_crystallize(self._session_id)
            else:
                self.memory.archive_session(task_desc, task_desc, data)

        # Evolution loss: compute on ALL outcomes (success AND failure)
        # Gradient is the sole feedback mechanism — no flat trust/skill updates
        if exit_reason and self._session_id:
            task_success = exit_reason.get('result') in ('EXITED', 'CURRENT_TASK_DONE')
            # Extract hindsight hint from recent turns on failure (OPD-inspired)
            hindsight_hint = ''
            if not task_success and self.history_info:
                hint = '; '.join(self.history_info[-3:])
                hindsight_hint = hint[:200]
            try:
                loss_data = self.memory.compute_evolution_loss(
                    session_id=self._session_id,
                    turns_used=turn,
                    max_turns=40,
                    task_success=task_success,
                    accessed_fact_ids=self._accessed_fact_ids,
                    accessed_skill_names=self._accessed_skill_names,
                    hindsight_hint=hindsight_hint,
                )
                self.memory.evolution_log_add(self._session_id, loss_data)
                self.memory.apply_gradient(loss_data)

                if loss_data['improvement_targets']:
                    next_prompt += f"\n[Evolution] Negative gradient on: {', '.join(loss_data['improvement_targets'])}. "
                    next_prompt += f"Evolution score: {loss_data['evolution_score']:.2f}. "
                    next_prompt += "Autonomous mode should prioritize improving these skills."
            except Exception as e:
                logger.error(f"Evolution loss error: {e}")

        # Crystallization nudge — remind the agent to save knowledge before finishing
        if exit_reason and exit_reason.get('result') == 'CURRENT_TASK_DONE':
            next_prompt += "\n[Nudge] Before finishing, consider crystallizing what you learned. Use wiki_ingest for rich knowledge, fact_add for quick facts."

        return next_prompt