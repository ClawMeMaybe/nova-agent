"""Nova cron scheduler — background thread for scheduled tasks."""

import threading
import time
from nova.cron.scheduler import tick


class NovaCron:
    """Background scheduler that ticks every 5 minutes."""

    TICK_INTERVAL = 300  # 5 minutes

    def __init__(self, agent):
        self.agent = agent
        self._thread = None
        self._running = False

    def start(self):
        """Start the cron scheduler thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="nova-cron")
        self._thread.start()

    def stop(self):
        """Stop the cron scheduler."""
        self._running = False

    def _run_loop(self):
        """Tick every TICK_INTERVAL seconds."""
        while self._running:
            time.sleep(self.TICK_INTERVAL)
            if not self._running:
                break
            try:
                executed = tick(self.agent, verbose=False)
                if executed > 0:
                    print(f"[Cron] Executed {executed} scheduled job(s)")
            except Exception as e:
                print(f"[Cron] Tick error: {e}")