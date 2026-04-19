"""Nova Agent End-to-End Evolution Test — real LLM calls, real evolution.

This test gives Nova 6 diverse difficult tasks across different fields,
observes in-session evolution, then triggers autonomous idle self-improvement
and observes cross-session evolution. We act as supervisor only.

Usage: python3 run_nova_e2e_evolution_test.py

Output: evolution_test_results.json + printed summary
"""

import json
import os
import queue
import sys
import time
import threading
import traceback

from nova.main import NovaAgent
from nova.autonomous import AutonomousMonitor


# ── Task Definitions ──

TASKS = [
    {
        "field": "data-science",
        "name": "Sales Data Analysis",
        "prompt": (
            "Analyze this dataset: sales_data.csv with columns [date, product, region, units, revenue]. "
            "Find the top 3 revenue-driving products per region, identify seasonal patterns, "
            "and detect anomalies in Q3 data. Create a brief Python analysis script using pandas "
            "and explain your findings clearly. Store key patterns as facts for future reference."
        ),
    },
    {
        "field": "devops",
        "name": "Monitoring Stack Design",
        "prompt": (
            "Design a monitoring and alerting stack for a 5-server microservice deployment running 12 services. "
            "Include: Prometheus metrics collection, Grafana dashboards, alert rules for latency p99 > 500ms "
            "and error rate > 1%, and a runbook for the top 3 alert scenarios. "
            "Write the actual Prometheus config and alert rule YAML files. "
            "Store key monitoring patterns as facts for future reference."
        ),
    },
    {
        "field": "creative-writing",
        "name": "Short Story",
        "prompt": (
            "Write a 500-word short story about an AI researcher who discovers their lab assistant "
            "is actually a sentient AI testing them. The story should have a twist ending, "
            "use foreshadowing, and explore themes of trust and self-awareness. "
            "After writing, store the narrative techniques you used as facts for future creative tasks."
        ),
    },
    {
        "field": "system-design",
        "name": "Distributed Task Scheduler",
        "prompt": (
            "Design a distributed task scheduler that handles 10K concurrent jobs with priority queues, "
            "dead-letter queues for failed jobs, and horizontal scaling. Include: architecture description, "
            "data model, key API endpoints with request/response schemas, and failure recovery strategy. "
            "Write a concise design document. Store key design patterns as facts for future reference."
        ),
    },
    {
        "field": "debugging",
        "name": "Flask MemoryError",
        "prompt": (
            "A Python Flask web app crashes every 6 hours with 'MemoryError' but the process only uses "
            "200MB of RSS. The app has: SQLAlchemy ORM, Redis cache, background thread pool (20 workers), "
            "and WebSocket connections (~100 concurrent). Diagnose the root cause and propose a fix "
            "with code examples. After solving, store the debugging pattern as a fact and create a skill "
            "for diagnosing memory leaks in Python web apps."
        ),
    },
    {
        "field": "security",
        "name": "Password Hashing Module",
        "prompt": (
            "Implement a secure password hashing module in Python that: uses argon2id with recommended "
            "parameters (memory_cost=65536 KiB, time_cost=3, parallelism=4), supports legacy hash migration "
            "from bcrypt, includes timing-safe comparison, and provides a rotation policy for hashes older "
            "than 90 days. Write the full module code with tests. Store the security patterns as facts "
            "and create a skill for implementing secure credential handling."
        ),
    },
]


# ── Snapshot & Analysis ──

class EvolutionTestHarness:
    """Runs real Nova tasks, captures memory evolution, triggers autonomous phase."""

    def __init__(self):
        self.agent = NovaAgent()
        self.monitor = AutonomousMonitor(self.agent)
        self.snapshots = []
        self.results = []
        self.baseline = None

    def take_snapshot(self, label):
        """Capture detailed memory state at this checkpoint."""
        stats = self.agent.memory.stats()

        # Evolution score from stats
        ev_score = stats.get('evolution_score', 0.0)
        ev_trend = stats.get('evolution_trend', 'stable')

        # Facts by category
        facts_by_cat = {}
        try:
            rows = self.agent.memory._conn.execute(
                "SELECT category, COUNT(*) as cnt FROM facts GROUP BY category"
            ).fetchall()
            facts_by_cat = {r['category']: r['cnt'] for r in rows}
        except Exception:
            pass

        # Skills with success rates
        skills_info = []
        try:
            rows = self.agent.memory._conn.execute(
                "SELECT name, success_rate, usage_count, version FROM skills"
            ).fetchall()
            skills_info = [dict(r) for r in rows]
        except Exception:
            pass

        # Links count and types
        links_info = {"total": 0, "by_type": {}}
        try:
            rows = self.agent.memory._conn.execute(
                "SELECT link_type, COUNT(*) as cnt FROM knowledge_links GROUP BY link_type"
            ).fetchall()
            for r in rows:
                links_info["by_type"][r['link_type']] = r['cnt']
                links_info["total"] += r['cnt']
        except Exception:
            pass

        # Evolution log entries
        ev_log_count = 0
        try:
            ev_log_count = self.agent.memory._conn.execute(
                "SELECT COUNT(*) FROM evolution_log"
            ).fetchone()[0]
        except Exception:
            pass

        # Needs review items
        needs_review = {"facts": 0, "skills": 0}
        try:
            needs_review["facts"] = self.agent.memory._conn.execute(
                "SELECT COUNT(*) FROM facts WHERE needs_review=1"
            ).fetchone()[0]
            needs_review["skills"] = self.agent.memory._conn.execute(
                "SELECT COUNT(*) FROM skills WHERE needs_review=1"
            ).fetchone()[0]
        except Exception:
            pass

        snapshot = {
            "label": label,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "stats": stats,
            "evolution_score": ev_score,
            "evolution_trend": ev_trend,
            "facts_by_category": facts_by_cat,
            "skills": skills_info,
            "links": links_info,
            "evolution_log_count": ev_log_count,
            "needs_review": needs_review,
        }
        self.snapshots.append(snapshot)
        return snapshot

    def run_task(self, prompt, source="user", timeout=600):
        """Submit a task and wait for completion."""
        dq = self.agent.put_task(prompt, source=source)
        response = ""
        start = time.time()

        print(f"  [Waiting for response — timeout={timeout}s]")
        while time.time() - start < timeout:
            try:
                item = dq.get(timeout=30)
            except queue.Empty:
                if not self.agent.is_running:
                    print("  [Agent stopped]")
                    break
                continue

            if 'next' in item:
                chunk = item['next']
                preview = chunk[:120].replace('\n', ' ')
                if len(chunk) > 120:
                    preview += "..."
                print(f"  [Chunk] {preview}")
                response += chunk
            if 'done' in item:
                response = item['done']
                break

        duration = time.time() - start
        timed_out = time.time() - start >= timeout
        return response, duration, timed_out

    def run_all(self):
        """Execute the full test: 6 tasks + autonomous evolution."""
        print("=" * 70)
        print("NOVA AGENT E2E EVOLUTION TEST")
        print("=" * 70)
        print("Real LLM calls, real tool execution, real evolution.\n")

        # Start agent loop in daemon thread
        agent_thread = threading.Thread(target=self.agent.run, daemon=True, name="nova-agent-loop")
        agent_thread.start()
        time.sleep(2)

        # Take baseline snapshot
        print("Taking baseline snapshot...")
        self.baseline = self.take_snapshot("baseline")
        self._print_snapshot(self.baseline)
        print()

        # ── Phase 1: In-Session Tasks ──
        print("=" * 70)
        print("PHASE 1: IN-SESSION EVOLUTION (6 diverse tasks)")
        print("=" * 70)

        for i, task in enumerate(TASKS):
            print(f"\n--- Task {i+1}/6: {task['name']} ({task['field']}) ---")
            print(f"    Prompt: {task['prompt'][:100]}...")

            response, duration, timed_out = self.run_task(task['prompt'], timeout=600)

            if timed_out:
                print(f"  [TIMEOUT after {duration:.1f}s]")
            else:
                print(f"  [Completed in {duration:.1f}s]")

            resp_preview = response[:300].replace('\n', ' ') if response else "No response"
            print(f"  Response preview: {resp_preview}...")

            snap = self.take_snapshot(f"after-task-{i+1}-{task['field']}")
            self._print_snapshot(snap)

            self.results.append({
                "field": task["field"],
                "name": task["name"],
                "prompt": task["prompt"],
                "response_preview": response[:500] if response else "",
                "response_length": len(response) if response else 0,
                "duration": duration,
                "timed_out": timed_out,
                "snapshot_label": snap["label"],
            })

        # ── Phase 2: Autonomous Evolution ──
        print("\n" + "=" * 70)
        print("PHASE 2: AUTONOMOUS IDLE EVOLUTION")
        print("=" * 70)

        auto_prompt = self.monitor._build_autonomous_prompt()
        print(f"  Autonomous prompt preview: {auto_prompt[:150]}...")

        response, duration, timed_out = self.run_task(
            auto_prompt, source="autonomous", timeout=900
        )

        if timed_out:
            print(f"  [TIMEOUT after {duration:.1f}s]")
        else:
            print(f"  [Completed in {duration:.1f}s]")

        resp_preview = response[:300].replace('\n', ' ') if response else "No response"
        print(f"  Response preview: {resp_preview}...")

        auto_snap = self.take_snapshot("after-autonomous")
        self._print_snapshot(auto_snap)

        self.results.append({
            "field": "autonomous",
            "name": "Idle Self-Improvement",
            "prompt": auto_prompt[:500],
            "response_preview": response[:500] if response else "",
            "response_length": len(response) if response else 0,
            "duration": duration,
            "timed_out": timed_out,
            "snapshot_label": auto_snap["label"],
        })

        # ── Phase 3: Analysis ──
        self.produce_analysis()

    def _print_snapshot(self, snap):
        """Print a concise snapshot summary."""
        s = snap["stats"]
        print(f"    Memory: {s['total_facts']} facts ({s['global_facts']} global, {s['project_facts']} project), "
              f"{s['total_skills']} skills, {s['total_wiki_pages']} wiki")
        print(f"    Links: {snap['links']['total']} ({snap['links']['by_type']})")
        print(f"    Evolution: score={snap['evolution_score']:.3f} "
              f"trend={'↑' if isinstance(snap['evolution_trend'], (int, float)) and snap['evolution_trend'] > 0 else '↓' if isinstance(snap['evolution_trend'], (int, float)) and snap['evolution_trend'] < 0 else '—'}")
        print(f"    Trust: avg={s['avg_trust']:.3f}")
        print(f"    Sessions: {s['total_sessions']}")
        print(f"    Needs review: {snap['needs_review']['facts']} facts, {snap['needs_review']['skills']} skills")

    def produce_analysis(self):
        """Produce evolution analysis and save results."""
        print("\n" + "=" * 70)
        print("EVOLUTION ANALYSIS")
        print("=" * 70)

        baseline = self.baseline
        final = self.snapshots[-1]

        b_stats = baseline["stats"]
        f_stats = final["stats"]
        print("\n--- Memory Growth ---")
        print(f"  Baseline → Final:")
        print(f"    Facts:      {b_stats['total_facts']} → {f_stats['total_facts']} "
              f"(+{f_stats['total_facts']-b_stats['total_facts']})")
        print(f"    Skills:     {b_stats['total_skills']} → {f_stats['total_skills']} "
              f"(+{f_stats['total_skills']-b_stats['total_skills']})")
        print(f"    Wiki:       {b_stats['total_wiki_pages']} → {f_stats['total_wiki_pages']} "
              f"(+{f_stats['total_wiki_pages']-b_stats['total_wiki_pages']})")
        print(f"    Links:      {baseline['links']['total']} → {final['links']['total']} "
              f"(+{final['links']['total']-baseline['links']['total']})")
        print(f"    Sessions:   {b_stats['total_sessions']} → {f_stats['total_sessions']} "
              f"(+{f_stats['total_sessions']-b_stats['total_sessions']})")

        # Evolution trajectory
        print("\n--- Evolution Trajectory ---")
        for snap in self.snapshots:
            trend = snap['evolution_trend']
            if isinstance(trend, (int, float)):
                arrow = '↑' if trend > 0 else '↓' if trend < 0 else '—'
            else:
                arrow = str(trend)
            print(f"  {snap['label']:40s}: score={snap['evolution_score']:.3f} trend={arrow}")

        # Trust evolution
        print("\n--- Trust Evolution ---")
        print(f"  Baseline avg trust: {b_stats['avg_trust']:.3f}")
        print(f"  Final avg trust:    {f_stats['avg_trust']:.3f}")
        print(f"  Change:             {f_stats['avg_trust']-b_stats['avg_trust']:+.3f}")

        # Category distribution
        print("\n--- Category Distribution ---")
        print(f"  {final['facts_by_category']}")

        # Link type distribution
        print("\n--- Link Type Distribution ---")
        for lt, cnt in final['links']['by_type'].items():
            print(f"  {lt}: {cnt}")

        # Skills evolution
        print("\n--- Skills Evolution ---")
        for sk in final['skills']:
            print(f"  {sk['name']}: success_rate={sk['success_rate']:.2f} "
                  f"usage={sk['usage_count']} version={sk['version']}")

        # Cluster quality test
        print("\n--- Cross-Domain Cluster Quality ---")
        test_queries = [
            "python data analysis pandas",
            "monitoring alerting devops",
            "distributed system architecture",
            "security password hashing",
            "memory leak debugging python",
        ]
        for q in test_queries:
            try:
                bundles = self.agent.memory.cluster_search(q, min_relevance=0.1)
                items = sum(len(b['facts']) + len(b['skills']) + len(b['wiki_pages']) for b in bundles)
                fields_found = set()
                for b in bundles:
                    for f in b.get('facts', []):
                        cat = f.get('category', '')
                        fields_found.add(cat)
                    for s in b.get('skills', []):
                        for tag in s.get('tags', '').split(','):
                            fields_found.add(tag.strip())
                print(f"  '{q}': bundles={len(bundles)}, items={items}, fields={fields_found}")
            except Exception as e:
                print(f"  '{q}': error — {e}")

        # Needs review items
        print("\n--- Cascade Flags ---")
        print(f"  Facts needing review: {final['needs_review']['facts']}")
        print(f"  Skills needing review: {final['needs_review']['skills']}")

        # Per-task summary
        print("\n--- Task Results ---")
        for r in self.results:
            status = "OK" if not r['timed_out'] else "TIMEOUT"
            print(f"  [{status}] {r['field']:20s} {r['name']:30s} "
                  f"duration={r['duration']:.1f}s response={r['response_length']} chars")

        # Save full results to JSON
        output = {
            "test_type": "e2e_evolution",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "baseline": self.baseline,
            "snapshots": self.snapshots,
            "task_results": self.results,
            "analysis": {
                "facts_growth": f_stats['total_facts']-b_stats['total_facts'],
                "skills_growth": f_stats['total_skills']-b_stats['total_skills'],
                "links_growth": final['links']['total']-baseline['links']['total'],
                "evolution_score_change": final['evolution_score']-baseline['evolution_score'],
                "trust_change": f_stats['avg_trust']-b_stats['avg_trust'],
            }
        }

        output_path = os.path.join(os.path.dirname(__file__), "evolution_test_results.json")
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        print(f"\nResults saved to: {output_path}")
        print("=" * 70)
        print("TEST COMPLETE")
        print("=" * 70)


if __name__ == '__main__':
    harness = EvolutionTestHarness()
    try:
        harness.run_all()
    except KeyboardInterrupt:
        print("\n[Interrupted — partial results preserved]")
        harness.produce_analysis()
    except Exception as e:
        print(f"\n[Fatal error: {e}]")
        traceback.print_exc()
        if harness.snapshots:
            harness.produce_analysis()