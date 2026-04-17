"""Nova autonomous monitor — self-improvement when user is idle.

Inspired by GenericAgent's reflect/autonomous.py + autonomous_operation_sop.
When user is idle >30min, the agent reviews its memory, plans high-value tasks, and executes them.
"""

import time
import threading


IDLE_THRESHOLD = 1800  # 30 minutes
CHECK_INTERVAL = 600   # 10 minutes


class AutonomousMonitor:
    """Monitors user activity and triggers autonomous self-improvement when idle."""

    def __init__(self, agent):
        self.agent = agent
        self._last_activity = time.time()
        self._running = False
        self._thread = None

    def mark_activity(self):
        """Reset idle timer — called when user sends a message."""
        self._last_activity = time.time()

    def start(self):
        """Start the autonomous monitor thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="nova-autonomous")
        self._thread.start()

    def stop(self):
        """Stop the autonomous monitor."""
        self._running = False

    def _run_loop(self):
        """Check every CHECK_INTERVAL, trigger autonomous task if idle > IDLE_THRESHOLD."""
        while self._running:
            time.sleep(CHECK_INTERVAL)
            if not self._running:
                break
            idle = time.time() - self._last_activity
            if idle > IDLE_THRESHOLD and not self.agent.is_running:
                self._trigger_autonomous()

    def _trigger_autonomous(self):
        """Inject an autonomous task into the agent's queue."""
        prompt = self._build_autonomous_prompt()
        if prompt:
            print("[Autonomous] User idle >30min, starting self-improvement task")
            self.agent.put_task(prompt, source="autonomous")
            # Reset activity to prevent rapid re-triggering
            self._last_activity = time.time()

    def _build_autonomous_prompt(self) -> str:
        """Build prompt based on TODO state and memory stats.

        Uses wiki page 'autonomous-todo' as the TODO list.
        Injects memory stats for context.
        Follows value formula: 'AI training data can't cover' × 'lasting benefit'.
        """
        stats = self.agent.memory.stats()

        # Check for existing TODO in memory
        todo_page = self.agent.memory.wiki_read('autonomous-todo', tier='global')
        todo_content = ""
        if todo_page and todo_page.get('content'):
            todo_content = todo_page['content']

        prompt = "[AUTONOMOUS MODE] Self-improvement session — user is idle.\n\n"
        prompt += "## Memory Stats\n"
        prompt += f"- Local: {stats['local_wiki_pages']} wiki, {stats['local_facts']} facts, {stats['local_skills']} skills\n"
        prompt += f"- Global: {stats['global_wiki_pages']} wiki, {stats['global_facts']} facts, {stats['global_skills']} skills\n"
        prompt += f"- Avg trust (local): {stats['local_avg_trust']:.2f}, (global): {stats['global_avg_trust']:.2f}\n\n"

        if todo_content.strip():
            prompt += "## Existing TODO (from autonomous-todo wiki page)\n"
            prompt += todo_content + "\n\n"
            prompt += "## Instructions\n"
            prompt += "Pick ONE item from the TODO above. Execute it efficiently (max 30 turns).\n"
            prompt += "After completing, update the autonomous-todo wiki page (remove completed item, add new ones if discovered).\n"
            prompt += "Always crystallize learnings: wiki_ingest for rich knowledge, fact_add for quick facts.\n"
        else:
            prompt += "## No existing TODO — enter planning mode\n"
            prompt += "Review your memory (db_query, wiki_query) and plan 3-5 high-value tasks.\n"
            prompt += "Use the value formula: prioritize tasks where 'AI training data can't cover' × 'lasting benefit for future collaboration' is highest.\n"
            prompt += "Priority order: memory review > environment discovery > skill refinement > knowledge audit.\n"
            prompt += "Write your planned tasks to the 'autonomous-todo' wiki page (category=decision, tags='autonomous,planning').\n"
            prompt += "Then execute ONE of the planned tasks (max 30 turns).\n"
            prompt += "Always crystallize learnings before finishing.\n\n"

        prompt += "## Constraints\n"
        prompt += "- Max 30 turns — be efficient, experiment-driven\n"
        prompt += "- Do NOT ask_user — this is autonomous mode\n"
        prompt += "- Always crystallize before finishing\n"
        prompt += "- Write session report as wiki page (category=session-log)\n"

        return prompt