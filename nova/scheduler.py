"""Nova Agent scheduler — cron-like task scheduling.

Inspired by Hermes' built-in cron system and GenericAgent's reflect/scheduler.
Allows natural-language scheduled tasks that run unattended.

Usage:
    nova-schedule add "Every morning at 9am, summarize yesterday's news"
    nova-schedule list
    nova-schedule remove <task_id>
    nova-schedule run    # Start the scheduler daemon
"""

import json
import os
import re
import sys
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional

SCHEDULE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'schedule.json')

# ── Cron parsing (simplified) ──

TIME_PATTERNS = [
    (r'every\s+(\d+)\s+minutes?', lambda m: {'interval': int(m.group(1)) * 60}),
    (r'every\s+(\d+)\s+hours?', lambda m: {'interval': int(m.group(1)) * 3600}),
    (r'every\s+day\s+at\s+(\d{1,2}):(\d{2})', lambda m: {'hour': int(m.group(1)), 'minute': int(m.group(2))}),
    (r'every\s+morning\s+at\s+(\d{1,2})', lambda m: {'hour': int(m.group(1)), 'minute': 0}),
    (r'every\s+morning', lambda m: {'hour': 9, 'minute': 0}),
    (r'every\s+night\s+at\s+(\d{1,2})', lambda m: {'hour': int(m.group(1)), 'minute': 0}),
    (r'every\s+night', lambda m: {'hour': 21, 'minute': 0}),
    (r'every\s+monday', lambda m: {'weekday': 0}),
    (r'every\s+tuesday', lambda m: {'weekday': 1}),
    (r'every\s+wednesday', lambda m: {'weekday': 2}),
    (r'every\s+thursday', lambda m: {'weekday': 3}),
    (r'every\s+friday', lambda m: {'weekday': 4}),
    (r'every\s+weekday', lambda m: {'weekday': '1-5'}),
]


def parse_schedule(text: str) -> Optional[Dict]:
    """Parse natural-language schedule into a config dict."""
    text_lower = text.lower()
    for pattern, extractor in TIME_PATTERNS:
        match = re.search(pattern, text_lower)
        if match:
            schedule = extractor(match)
            # Extract the actual task (everything after the time part)
            task_text = text
            task_match = re.search(r'(?:at\s+\d{1,2}(?:\:\d{2})?|minutes?|hours?|morning|night|weekday|monday|tuesday|wednesday|thursday|friday)[,\s]+(.+)', text_lower)
            if task_match:
                schedule['task'] = task_match.group(1).strip()
            else:
                # Task is the rest of the text after the schedule phrase
                end = match.end()
                rest = text[end:].strip().lstrip(',').strip()
                schedule['task'] = rest if rest else text
            return schedule
    return None


class ScheduleManager:
    """Manages scheduled tasks with persistence."""

    def __init__(self, filepath: str = SCHEDULE_FILE):
        self.filepath = filepath
        self.tasks: List[Dict] = self._load()

    def _load(self) -> List[Dict]:
        if os.path.exists(self.filepath):
            with open(self.filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        return []

    def _save(self):
        with open(self.filepath, 'w', encoding='utf-8') as f:
            json.dump(self.tasks, f, ensure_ascii=False, indent=2)

    def add(self, schedule_config: Dict) -> int:
        task_id = len(self.tasks) + 1
        entry = {
            'id': task_id,
            'created': datetime.now().isoformat(),
            'last_run': None,
            'enabled': True,
            **schedule_config,
        }
        self.tasks.append(entry)
        self._save()
        return task_id

    def remove(self, task_id: int) -> bool:
        self.tasks = [t for t in self.tasks if t['id'] != task_id]
        self._save()
        return True

    def list_tasks(self) -> List[Dict]:
        return self.tasks

    def should_run(self, task: Dict) -> bool:
        """Check if a task should run now."""
        now = datetime.now()

        if 'interval' in task:
            if task.get('last_run'):
                last = datetime.fromisoformat(task['last_run'])
                return (now - last).total_seconds() >= task['interval']
            return True

        if 'hour' in task and 'minute' in task:
            if now.hour == task['hour'] and now.minute == task['minute']:
                if task.get('last_run'):
                    last = datetime.fromisoformat(task['last_run'])
                    if (now - last).total_seconds() < 3600:  # Don't re-run within 1 hour
                        return False
                return True

        if 'weekday' in task:
            wd = task['weekday']
            if isinstance(wd, str) and '-' in wd:
                low, high = wd.split('-')
                if now.weekday() in range(int(low), int(high) + 1):
                    return True
            elif isinstance(wd, int) and now.weekday() == wd:
                return True

        return False

    def mark_run(self, task_id: int):
        for t in self.tasks:
            if t['id'] == task_id:
                t['last_run'] = datetime.now().isoformat()
                break
        self._save()


def run_scheduler():
    """Start the scheduler daemon — checks tasks every 60 seconds."""
    from nova.main import NovaAgent

    agent = NovaAgent()
    agent.verbose = False
    threading.Thread(target=agent.run, daemon=True).start()

    manager = ScheduleManager()

    print('Nova scheduler started. Checking every 60 seconds...')
    while True:
        time.sleep(60)
        for task in manager.list_tasks():
            if not task.get('enabled', True):
                continue
            if manager.should_run(task):
                print(f'[Schedule] Running task {task["id"]}: {task.get("task", "")[:80]}')
                dq = agent.put_task(task.get('task', ''), source='schedule')
                # Wait for completion
                try:
                    while True:
                        item = dq.get(timeout=120)
                        if 'done' in item:
                            break
                except queue.Empty:
                    pass
                manager.mark_run(task['id'])


def cli():
    """CLI for managing scheduled tasks."""
    import argparse
    parser = argparse.ArgumentParser(description='Nova Agent Scheduler')
    parser.add_argument('action', choices=['add', 'list', 'remove', 'run'])
    parser.add_argument('args', nargs='*', help='Task description or task ID')

    args = parser.parse_args()
    manager = ScheduleManager()

    if args.action == 'add':
        text = ' '.join(args.args)
        schedule = parse_schedule(text)
        if schedule:
            task_id = manager.add(schedule)
            print(f'Added task #{task_id}: {schedule}')
        else:
            print('Could not parse schedule. Try: "every morning at 9, summarize news"')

    elif args.action == 'list':
        tasks = manager.list_tasks()
        if not tasks:
            print('No scheduled tasks.')
        for t in tasks:
            print(f'  #{t["id"]} [{t.get("hour", "")}:{t.get("minute", "") or ""}] {t.get("task", "")[:60]}')

    elif args.action == 'remove':
        task_id = int(args.args[0])
        manager.remove(task_id)
        print(f'Removed task #{task_id}')

    elif args.action == 'run':
        run_scheduler()


if __name__ == '__main__':
    cli()