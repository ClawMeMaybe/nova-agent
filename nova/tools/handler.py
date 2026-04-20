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
from nova.memory.engine import NovaMemory

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
        global_db = os.path.join(os.path.expanduser('~'), '.nova', 'nova.db')
        engine = NovaMemory(global_db)
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

    def _resolve_link_name(self, item_type, item_id, current_name):
        """Auto-fill link name from DB when ID provided but name empty."""
        if current_name or item_id is None:
            return current_name
        try:
            if item_type == 'fact':
                row = self.memory._conn.execute("SELECT content FROM facts WHERE id=?", (item_id,)).fetchone()
                if row:
                    return row['content'][:60]
            elif item_type == 'skill':
                row = self.memory._conn.execute("SELECT name FROM skills WHERE id=?", (item_id,)).fetchone()
                if row:
                    return row['name']
            elif item_type == 'wiki':
                row = self.memory._conn.execute("SELECT title FROM wiki_pages WHERE id=?", (item_id,)).fetchone()
                if row:
                    return row['title']
        except Exception:
            pass
        return current_name

    def _resolve_link_id(self, item_type, item_name):
        """Resolve ID from name — needed because knowledge_links has NOT NULL on source_id/target_id."""
        if not item_name:
            return None
        try:
            if item_type == 'fact':
                row = self.memory._conn.execute("SELECT id FROM facts WHERE content LIKE ?", (item_name[:40] + '%',)).fetchone()
                return row['id'] if row else None
            elif item_type == 'skill':
                row = self.memory._conn.execute("SELECT id FROM skills WHERE name=?", (item_name,)).fetchone()
                return row['id'] if row else None
            elif item_type == 'wiki':
                row = self.memory._conn.execute("SELECT id FROM wiki_pages WHERE slug=? OR title=?", (item_name, item_name)).fetchone()
                return row['id'] if row else None
        except Exception:
            pass
        return None

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

        if not content:
            return StepOutcome({"status": "error", "msg": "No content provided"}, next_prompt="\n")

        try:
            page_id = self.memory.wiki_ingest(
                title, content, tags,
                category=category, confidence=confidence
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

        if not query:
            return StepOutcome({"status": "error", "msg": "Query required"}, next_prompt="\n")

        try:
            pages = self.memory.wiki_query(query, category=category, tags=tags)
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

        if not content:
            return StepOutcome({"status": "error", "msg": "Fact content required"}, next_prompt="\n")

        try:
            fact_id = self.memory.fact_add(content, category=category, tags=tags)
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

        if not query:
            return StepOutcome({"status": "error", "msg": "Query required"}, next_prompt="\n")

        try:
            facts = self.memory.fact_search(query, category=category, min_trust=min_trust)
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

    def do_fact_feedback(self, args, response):
        """Mark a fact as helpful or unhelpful — per-turn feedback for trust evolution."""
        fact_id = args.get("id")
        helpful = args.get("helpful", True)
        reason = args.get("reason", "")

        if fact_id is None:
            return StepOutcome({"status": "error", "msg": "Fact ID required"}, next_prompt="\n")

        # Accessed-only validation
        if fact_id not in self._accessed_fact_ids:
            return StepOutcome({"status": "error", "msg": "Cannot provide feedback on knowledge not accessed this session"}, next_prompt="\n")

        # Reason validation for unhelpful feedback
        if not helpful and len(reason.strip()) < 10:
            return StepOutcome({"status": "error", "msg": "Reason must be at least 10 characters when marking knowledge as unhelpful"}, next_prompt="\n")

        try:
            event_id = self.memory.feedback_event_add(
                'fact', fact_id, '', helpful, reason,
                self._session_id, self.current_turn
            )
            result = {
                "status": "success",
                "msg": f"Fact {fact_id} marked as {'helpful' if helpful else 'unhelpful'}. Feedback event recorded (id={event_id}).",
                "event_id": event_id,
                "helpful": helpful,
            }
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_skill_feedback(self, args, response):
        """Mark a skill as helpful or unhelpful — per-turn feedback for success rate evolution."""
        name = args.get("name", "")
        helpful = args.get("helpful", True)
        reason = args.get("reason", "")

        if not name:
            return StepOutcome({"status": "error", "msg": "Skill name required"}, next_prompt="\n")

        # Accessed-only validation
        if name not in self._accessed_skill_names:
            return StepOutcome({"status": "error", "msg": "Cannot provide feedback on knowledge not accessed this session"}, next_prompt="\n")

        # Reason validation for unhelpful feedback
        if not helpful and len(reason.strip()) < 10:
            return StepOutcome({"status": "error", "msg": "Reason must be at least 10 characters when marking knowledge as unhelpful"}, next_prompt="\n")

        try:
            event_id = self.memory.feedback_event_add(
                'skill', None, name, helpful, reason,
                self._session_id, self.current_turn
            )
            result = {
                "status": "success",
                "msg": f"Skill '{name}' marked as {'helpful' if helpful else 'unhelpful'}. Feedback event recorded (id={event_id}).",
                "event_id": event_id,
                "helpful": helpful,
            }
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    # ── Knowledge Link Tools ──

    def do_link_add(self, args, response):
        """Create a link between two knowledge items."""
        source_type = args.get("source_type", "")
        source_id = args.get("source_id")
        source_name = args.get("source_name", "")
        target_type = args.get("target_type", "")
        target_id = args.get("target_id")
        target_name = args.get("target_name", "")
        link_type = args.get("link_type", "depends_on")

        if not source_type or not target_type:
            return StepOutcome({"status": "error", "msg": "Both source_type and target_type required"}, next_prompt="\n")
        if source_id is None and not source_name:
            return StepOutcome({"status": "error", "msg": "source_id or source_name required"}, next_prompt="\n")
        if target_id is None and not target_name:
            return StepOutcome({"status": "error", "msg": "target_id or target_name required"}, next_prompt="\n")

        # Auto-fill names from DB when IDs provided but names empty
        source_name = self._resolve_link_name(source_type, source_id, source_name)
        target_name = self._resolve_link_name(target_type, target_id, target_name)

        # Resolve IDs from names when only names provided (DB requires NOT NULL)
        if source_id is None and source_name:
            source_id = self._resolve_link_id(source_type, source_name)
            if source_id is None:
                return StepOutcome({"status": "error", "msg": f"Could not find {source_type} '{source_name}' to resolve source_id"}, next_prompt="\n")
        if target_id is None and target_name:
            target_id = self._resolve_link_id(target_type, target_name)
            if target_id is None:
                return StepOutcome({"status": "error", "msg": f"Could not find {target_type} '{target_name}' to resolve target_id"}, next_prompt="\n")

        try:
            link_id = self.memory.link_add(
                source_type, source_id, source_name,
                target_type, target_id, target_name, link_type
            )
            result = {
                "status": "success",
                "msg": f"Link created: {source_type}:{source_name}→{target_type}:{target_name} ({link_type}). id={link_id}",
                "link_id": link_id,
            }
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_link_search(self, args, response):
        """Search knowledge links by optional filters."""
        try:
            links = self.memory.link_search(
                source_type=args.get("source_type"),
                source_id=args.get("source_id"),
                target_type=args.get("target_type"),
                target_id=args.get("target_id"),
                link_type=args.get("link_type"),
            )
            if not links:
                result = {"status": "no_results", "msg": "No matching links"}
            else:
                summaries = []
                for lk in links:
                    summaries.append(f"{lk['source_type']}:{lk.get('source_name', lk.get('source_id'))} → {lk['target_type']}:{lk.get('target_name', lk.get('target_id'))} [{lk['link_type']}]")
                result = {"status": "success", "msg": f"Found {len(links)} links", "links": summaries}
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_cluster_search(self, args, response):
        """Search for composed knowledge bundles by topic."""
        query = args.get("query", "")
        min_relevance = args.get("min_relevance", 0.3)

        if not query:
            return StepOutcome({"status": "error", "msg": "Query required"}, next_prompt="\n")

        try:
            bundles = self.memory.cluster_search(query, min_relevance=min_relevance)
            if not bundles:
                result = {"status": "no_results", "msg": f"No knowledge clusters matching '{query}'"}
            else:
                summaries = []
                for b in bundles:
                    s = f"**[{b['topic_tag']}]** (relevance: {b['relevance_score']})\n"
                    s += f"  Facts: {len(b['facts'])}, Skills: {len(b['skills'])}, Wiki: {len(b['wiki_pages'])}\n"
                    for f in b['facts'][:3]:
                        s += f"  - fact: {f['content'][:80]} (trust: {f['trust_score']:.2f})\n"
                    for sk in b['skills'][:2]:
                        s += f"  - skill: {sk['name']} (success: {sk['success_rate']:.2f})\n"
                    for p in b['wiki_pages'][:1]:
                        s += f"  - wiki: {p['title']}\n"
                    summaries.append(s)
                result = {"status": "success", "msg": f"Found {len(bundles)} knowledge bundles", "bundles": summaries}
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    # ── SQL Sandbox Tools (LLM-as-DBA) ──

    def do_db_query(self, args, response):
        """Execute sandboxed SQL against the knowledge database."""
        sql = args.get("sql", "")

        if not sql:
            return StepOutcome({"status": "error", "msg": "SQL query required"}, next_prompt="\n")

        try:
            result = self.memory.safe_query(sql)
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

        try:
            result = self.memory.get_schema_info()
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_wiki_export(self, args, response):
        """Export wiki pages to markdown files for human browsing."""
        output_dir = args.get("output_dir") or os.path.join(self.cwd, ".nova", "wiki")

        try:
            pages = self.memory.wiki_list()
            exported = []
            for page in pages:
                full = self.memory.wiki_read(page['slug'])
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
        """Crystallize a repeatable workflow into a skill/SOP, or install a contract skill."""
        name = args.get("name", "")
        description = args.get("description", "")
        steps = args.get("steps", [])
        triggers = args.get("triggers", "")
        pitfalls = args.get("pitfalls", [])
        tags = args.get("tags", "")
        contract = args.get("contract", None)

        if not name or not triggers:
            if not name:
                return StepOutcome({"status": "error", "msg": "name is required"}, next_prompt="\n")
            return StepOutcome({"status": "error", "msg": "name and triggers are required"}, next_prompt="\n")

        # Contract skills may have empty steps
        if not steps and not contract:
            return StepOutcome({"status": "error", "msg": "steps or contract is required"}, next_prompt="\n")

        try:
            skill_id = self.memory.skill_add(
                name, description, steps,
                tags=tags, triggers=triggers, pitfalls=pitfalls,
                contract=contract
            )
            contract_info = f" Contract: {len(contract)} chars." if contract else ""
            result = {
                "status": "success",
                "msg": f"Skill '{name}' created (id={skill_id}, version=1). Triggers: {triggers}. Steps: {len(steps)}. Pitfalls: {len(pitfalls)}.{contract_info}",
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

        if not query:
            return StepOutcome({"status": "error", "msg": "Query required"}, next_prompt="\n")

        try:
            skills = self.memory.skill_search(query, min_success=min_success)
            # Track accessed skill names for success feedback
            for s in skills:
                if 'name' in s:
                    self._accessed_skill_names.append(s['name'])
            if not skills:
                result = {"status": "no_results", "msg": f"No skills matching '{query}' above success {min_success}"}
            else:
                summaries = []
                for s in skills:
                    scope_label = 'project' if s.get('project_id') else 'global'
                    summary = f"**{s['name']}** (v{s.get('version', 1)}, success: {s['success_rate']:.0%}, used {s['usage_count']}x) [{scope_label}]\n"
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

    # ── Project Management Tools ──

    def do_project_create(self, args, response):
        """Create a new project scope for knowledge isolation."""
        name = args.get("name", "")
        description = args.get("description", "")

        if not name:
            return StepOutcome({"status": "error", "msg": "Project name required"}, next_prompt="\n")

        try:
            project_id = self.memory.project_create(name, description)
            result = {
                "status": "success",
                "msg": f"Project '{name}' created (id={project_id}). Use project_select to switch scope.",
                "project_id": project_id,
            }
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_project_select(self, args, response):
        """Select a project scope — all subsequent reads/writes use this project_id."""
        project_id = args.get("project_id")

        try:
            self.memory.project_select(project_id)
            self.parent.current_project_id = project_id
            if project_id:
                info = self.memory.project_info(project_id)
                result = {
                    "status": "success",
                    "msg": f"Switched to project '{info['name']}' (id={project_id}). All knowledge operations now scoped to this project.",
                    "project_id": project_id,
                    "project_info": info,
                }
            else:
                result = {
                    "status": "success",
                    "msg": "Switched to global scope. All knowledge operations now operate on global knowledge.",
                    "project_id": None,
                }
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_project_list(self, args, response):
        """List all projects."""
        try:
            projects = self.memory.project_list()
            if not projects:
                result = {"status": "no_results", "msg": "No projects yet. Use project_create to add one."}
            else:
                summaries = []
                for p in projects:
                    s = f"  {p['id']}: {p['name']} — {p['description']} (created: {p['created_at'][:10]})"
                    summaries.append(s)
                result = {"status": "success", "msg": f"{len(projects)} projects", "projects": summaries}
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_project_info(self, args, response):
        """Get detailed info about a project including scoped knowledge counts."""
        project_id = args.get("project_id", "")

        if not project_id:
            return StepOutcome({"status": "error", "msg": "project_id required"}, next_prompt="\n")

        try:
            info = self.memory.project_info(project_id)
            if not info:
                result = {"status": "error", "msg": f"Project {project_id} not found"}
            else:
                result = {
                    "status": "success",
                    "msg": f"Project '{info['name']}'",
                    "info": info,
                }
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_fact_promote(self, args, response):
        """Promote a project-scoped fact to global scope."""
        fact_id = args.get("id")

        if fact_id is None:
            return StepOutcome({"status": "error", "msg": "Fact ID required"}, next_prompt="\n")

        try:
            success = self.memory.fact_promote(fact_id)
            if success:
                result = {"status": "success", "msg": f"Fact {fact_id} promoted to global scope. Now accessible across all projects."}
            else:
                result = {"status": "error", "msg": f"Fact {fact_id} not found"}
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_skill_promote(self, args, response):
        """Promote a project-scoped skill to global scope."""
        name = args.get("name", "")

        if not name:
            return StepOutcome({"status": "error", "msg": "Skill name required"}, next_prompt="\n")

        try:
            success = self.memory.skill_promote(name)
            if success:
                result = {"status": "success", "msg": f"Skill '{name}' promoted to global scope. Now accessible across all projects."}
            else:
                result = {"status": "error", "msg": f"Skill '{name}' not found"}
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    def do_wiki_promote(self, args, response):
        """Promote a project-scoped wiki page to global scope."""
        slug = args.get("slug", "")

        if not slug:
            return StepOutcome({"status": "error", "msg": "Wiki page slug required"}, next_prompt="\n")

        try:
            success = self.memory.wiki_promote(slug)
            if success:
                result = {"status": "success", "msg": f"Wiki page '{slug}' promoted to global scope. Now accessible across all projects."}
            else:
                result = {"status": "error", "msg": f"Wiki page '{slug}' not found"}
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        return StepOutcome(result, next_prompt=next_prompt)

    # ── Project Sync ──

    def do_project_learn(self, args, response):
        """Learn about the current project directory — scan and return structured info for knowledge generation.

        This is a read-only scan — it gathers directory info, the LLM then decides
        what knowledge to create using fact_add, wiki_ingest, skill_add, link_add.
        """
        from nova.main import build_learn_prompt

        project_root = args.get("project_root", self.cwd)
        depth = args.get("depth", "standard")

        try:
            scan = self.memory.project_scan(project_root, depth=depth)

            # Build structured prompt with quality guidelines
            learn_text = build_learn_prompt(scan)

            result = {
                "status": "success",
                "msg": f"Project '{scan['project_name']}' learned (depth={depth}). "
                       f"Language: {scan['language']}, {len(scan['file_tree'])} files found.",
                "learn_data": learn_text,
                "project_name": scan['project_name'],
                "language": scan['language'],
                "file_count": len(scan['file_tree']),
                "test_framework": scan['test_framework'],
                "lint_tool": scan['lint_tool'],
            }
        except Exception as e:
            result = {"status": "error", "msg": str(e)}

        # Return learn data in next_prompt so the LLM can analyze it
        next_prompt = self._get_anchor_prompt(skip=args.get('_index', 0) > 0)
        if result.get('learn_data'):
            next_prompt += f"\n{result['learn_data']}\n"

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
                self.memory.apply_time_decay()
            except:
                pass

        # Auto-crystallize at session end (only on success)
        if exit_reason and exit_reason.get('result') in ('EXITED', 'CURRENT_TASK_DONE'):
            task_desc = self.history_info[0] if self.history_info else 'unknown task'
            data = exit_reason.get('data', '')
            if not isinstance(data, str):
                data = str(data)[:500]
            had_knowledge = len(self._accessed_fact_ids) > 0 or len(self._accessed_skill_names) > 0
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