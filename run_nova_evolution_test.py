"""Multi-field evolution test — runs Nova Agent through diverse tasks and measures memory evolution.

Tasks span 6 different fields to test knowledge composition and cross-domain learning:
1. Data Science / Analysis
2. DevOps / Infrastructure
3. Creative Writing / Content
4. System Design / Architecture
5. Debugging / Troubleshooting
6. Security / Cryptography

After each task, we measure:
- Facts added, skills crystallized, wiki pages created
- Trust scores, success rates
- Knowledge links created
- Cluster search quality
- Evolution score and trend
"""

import json
import os
import sys
import time
import threading

sys.path.insert(0, '/root/nova-agent')

from nova.memory.engine import NovaMemory


# ── Task Definitions ──

TASKS = [
    {
        "name": "data-analysis",
        "prompt": "Analyze this dataset: a CSV with columns [timestamp, user_id, action, duration]. Write a Python script that calculates: (1) average session duration per user, (2) most common action sequence, (3) anomaly detection for sessions >2x the mean. Use pandas and numpy.",
        "field": "data-science",
    },
    {
        "name": "devops-monitoring",
        "prompt": "Design a monitoring stack for a 5-server microservice deployment. Create: (1) a Prometheus config with alert rules for 95th percentile latency >500ms, (2) a Grafana dashboard JSON model showing service health, (3) a Docker Compose file for the monitoring stack. Make it production-ready.",
        "field": "devops",
    },
    {
        "name": "creative-writing",
        "prompt": "Write a short story (500 words) about an AI researcher who discovers their lab assistant is actually an evolved version of their own code. Include: a plot twist, a moral dilemma about self-awareness, and a resolution that questions what 'alive' means. Then summarize 3 key narrative techniques you used.",
        "field": "creative-writing",
    },
    {
        "name": "system-design",
        "prompt": "Design a distributed task scheduler that handles 10K concurrent jobs with priority queues, retry logic, and deadlock prevention. Specify: (1) the architecture diagram as ASCII art, (2) the data model, (3) the API endpoints, (4) failure recovery strategy. Compare this with AWS Step Functions tradeoffs.",
        "field": "system-design",
    },
    {
        "name": "debugging",
        "prompt": "A Python web app crashes every 6 hours with 'MemoryError' but the process only uses 200MB of RAM. The crash log shows: gc.collect() returns 50K objects, thread count is 150, and there are 12 open database connections that never close. Diagnose the root cause and write a fix script that addresses all three symptoms.",
        "field": "debugging",
    },
    {
        "name": "security",
        "prompt": "Implement a secure password hashing module in Python that: (1) uses argon2id with proper parameters (memory=65536, time=3, parallelism=4), (2) includes a salt verification function, (3) handles upgrade from legacy SHA-256 hashes, (4) has rate limiting. Also document 3 common mistakes in password hashing implementations.",
        "field": "security",
    },
]


def snapshot_memory(memory: NovaMemory, label: str) -> dict:
    """Take a snapshot of memory state after a task."""
    stats = memory.stats()

    # Count items per category
    categories = {}
    for r in memory._conn.execute("SELECT category, COUNT(*) FROM facts GROUP BY category").fetchall():
        categories[r[0]] = r[1]

    # Link type breakdown
    link_types = {}
    for r in memory._conn.execute("SELECT link_type, COUNT(*) FROM knowledge_links GROUP BY link_type").fetchall():
        link_types[r[0]] = r[1]

    # Cluster search quality test
    cluster_quality = {}
    for field in ['data-science', 'devops', 'system-design', 'security', 'debugging', 'creative-writing']:
        bundles = memory.cluster_search(field, min_relevance=0.2, limit=3)
        cluster_quality[field] = {
            'bundle_count': len(bundles),
            'total_items': sum(len(b['facts']) + len(b['skills']) + len(b['wiki_pages']) for b in bundles),
            'max_relevance': max((b['relevance_score'] for b in bundles), default=0),
        }

    # Needs review items
    review = memory.get_items_needing_review()

    # Context prompt budget estimate
    ctx = memory.build_context_prompt("deploy microservice architecture")
    ctx_len = len(ctx)

    return {
        'label': label,
        'total_facts': stats['total_facts'],
        'global_facts': stats['global_facts'],
        'project_facts': stats['project_facts'],
        'total_skills': stats['total_skills'],
        'global_skills': stats['global_skills'],
        'project_skills': stats['project_skills'],
        'total_wiki': stats['total_wiki_pages'],
        'global_wiki': stats['global_wiki_pages'],
        'project_wiki': stats['project_wiki_pages'],
        'total_sessions': stats['total_sessions'],
        'total_links': sum(v for v in link_types.values()) if link_types else 0,
        'link_types': link_types,
        'avg_trust': stats['avg_trust'],
        'evolution_score': stats['evolution_score'],
        'evolution_trend': stats['evolution_trend'],
        'categories': categories,
        'facts_needing_review': len(review['facts_needing_review']),
        'skills_needing_review': len(review['skills_needing_review']),
        'cluster_quality': cluster_quality,
        'context_prompt_length': ctx_len,
    }


def seed_initial_knowledge(memory: NovaMemory):
    """Seed baseline knowledge to give the agent something to build on."""
    # Core environment facts
    memory.fact_add("Nova project root is /root/nova-agent", category="environment", tags="paths,nova,project")
    memory.fact_add("Nova uses Anthropic API via DashScope proxy for LLM calls", category="environment", tags="nova,llm,api")
    memory.fact_add("Python 3.12 is the runtime environment", category="environment", tags="python,environment")
    memory.fact_add("pytest is the test runner, ruff is the linter", category="environment", tags="testing,linting,python")

    # Cross-domain seed facts
    memory.fact_add("pandas DataFrame groupby is the standard pattern for per-user aggregation", category="pattern", tags="data-science,pandas,aggregation")
    memory.fact_add("Prometheus alerting rules use FOR clause to require sustained violation before alerting", category="pattern", tags="devops,monitoring,prometheus")
    memory.fact_add("argon2id is the recommended password hashing algorithm per OWASP 2023", category="pattern", tags="security,passwords,hashing")
    memory.fact_add("Distributed task schedulers need priority queues + retry with exponential backoff", category="pattern", tags="system-design,scheduling,distributed")
    memory.fact_add("MemoryError with low RAM usage often indicates thread/connection leak, not data size", category="debugging", tags="debugging,python,leak")

    # Seed skills
    memory.skill_add(
        "analyze-tabular-data",
        "Analyze CSV/tabular data using pandas — compute aggregations, detect anomalies, generate insights",
        ["1. Load data with pandas.read_csv", "2. Compute groupby aggregations per dimension", "3. Calculate statistics: mean, median, 95th percentile", "4. Detect anomalies using z-score or IQR method", "5. Visualize key findings with matplotlib", "6. Verify: check output matches expected shape and values"],
        triggers="analyze,data,statistics,pandas,CSV",
        tags="data-science,analysis,pandas",
    )
    memory.skill_add(
        "design-monitoring-stack",
        "Design a production monitoring stack with Prometheus + Grafana + alerting",
        ["1. Define SLIs and SLOs for target services", "2. Write Prometheus config with scrape targets and alert rules", "3. Create Grafana dashboard model showing health metrics", "4. Write Docker Compose for monitoring deployment", "5. Verify: test alert rules against sample metrics"],
        triggers="monitor,prometheus,grafana,devops,alert",
        tags="devops,monitoring,prometheus,grafana",
    )


def run_evolution_test():
    """Run the multi-field evolution test against Nova Agent."""
    print("=" * 70)
    print("NOVA AGENT MULTI-FIELD EVOLUTION TEST")
    print("=" * 70)
    print("\nThis test measures how Nova's memory evolves across 6 diverse task fields.")
    print("It does NOT actually run the LLM — it simulates knowledge accumulation")
    print("and tests the memory engine's composition + evolution capabilities.\n")

    # Initialize unified memory
    db_path = '/root/.nova/nova.db'
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    memory = NovaMemory(db_path)

    # Seed initial knowledge
    seed_initial_knowledge(memory)

    # Take baseline snapshot
    baseline = snapshot_memory(memory, "baseline")
    print(f"Baseline: {baseline['total_facts']} facts ({baseline['global_facts']} global), "
          f"{baseline['total_skills']} skills, {baseline['total_links']} links")
    print(f"          avg_trust={baseline['avg_trust']:.3f}, "
          f"evolution score={baseline['evolution_score']:.3f}")
    print()

    # Simulate task execution
    snapshots = [baseline]

    for i, task in enumerate(TASKS):
        print(f"--- Task {i+1}/{len(TASKS)}: {task['name']} ({task['field']}) ---")
        print(f"    Prompt: {task['prompt'][:80]}...")

        # Simulate session creation
        sid = memory.session_create(task['prompt'])

        # Simulate knowledge crystallization
        field = task['field']

        new_facts = []
        if field == 'data-science':
            new_facts.append(("Z-score anomaly detection flags data points >2 standard deviations from mean", "pattern", "data-science,anomaly,statistics"))
            new_facts.append(("pandas rolling window with .rolling(n).mean() computes moving averages", "pattern", "data-science,pandas,time-series"))
        elif field == 'devops':
            new_facts.append(("Grafana dashboard variables enable template reuse across environments", "pattern", "devops,grafana,dashboard"))
            new_facts.append(("Prometheus FOR clause duration prevents flapping alerts on transient spikes", "pattern", "devops,prometheus,alerting"))
        elif field == 'creative-writing':
            new_facts.append(("In medias res opening starts narrative mid-action for immediate engagement", "pattern", "creative-writing,narrative,technique"))
            new_facts.append(("Foreshadowing plants early details that pay off later in the plot arc", "pattern", "creative-writing,narrative,technique"))
        elif field == 'system-design':
            new_facts.append(("Priority queue with bounded capacity prevents resource starvation under load", "pattern", "system-design,scheduling,queue"))
            new_facts.append(("Distributed consensus requires quorum — majority of nodes must agree before committing", "pattern", "system-design,distributed,consensus"))
        elif field == 'debugging':
            new_facts.append(("Thread leak pattern: unclosed connections + growing thread count → eventual MemoryError", "pattern", "debugging,python,leak"))
            new_facts.append(("gc.collect() returning high object count with low process RAM indicates reference cycle leak", "debugging", "debugging,python,gc"))
        elif field == 'security':
            new_facts.append(("argon2id parameters: memory_cost=65536 KiB, time_cost=3 iterations, parallelism=4 threads", "pattern", "security,argon2,hashing"))
            new_facts.append(("Legacy hash migration must verify old hash then re-hash with new algorithm on next login", "pattern", "security,migration,hashing"))

        fact_ids = []
        for content, cat, tags in new_facts:
            fid = memory.fact_add(content, category=cat, tags=tags)
            fact_ids.append(fid)
            print(f"    + fact [{cat}]: {content[:50]} (id={fid})")

        # Add task-specific skill (some tasks)
        if field == 'data-science':
            memory.skill_add(
                "detect-data-anomalies",
                "Detect anomalies in tabular data using statistical methods",
                ["1. Load data and compute baseline statistics", "2. Calculate z-scores for each value", "3. Flag values with |z| > threshold (default 2)", "4. Report anomalies with context", "5. Verify: count anomalies matches expected range"],
                triggers="anomaly,outlier,detect,statistics",
                tags="data-science,anomaly,statistics",
            )
        elif field == 'security':
            memory.skill_add(
                "secure-password-hashing",
                "Implement secure password hashing with argon2id and legacy migration",
                ["1. Install argon2-cffi package", "2. Configure argon2id params: memory=65536, time=3, parallelism=4", "3. Hash with random salt per password", "4. Add migration path for legacy SHA-256 hashes", "5. Implement rate limiting on verify endpoint", "6. Verify: test hash + verify round-trip, test migration path"],
                triggers="password,hash,argon2,security,credentials",
                pitfalls=["Never use MD5 or SHA-256 alone for passwords — always use argon2id or bcrypt", "Never hardcode salt — use os.urandom(16) for each password"],
                tags="security,password,argon2,hashing",
            )

        # Create knowledge links
        skills_for_field = {
            'data-science': ['analyze-tabular-data', 'detect-data-anomalies'],
            'devops': ['design-monitoring-stack'],
            'security': ['secure-password-hashing'],
        }
        if field in skills_for_field and fact_ids:
            for skill_name in skills_for_field[field]:
                skill_row = memory._conn.execute("SELECT id, name FROM skills WHERE name=?", (skill_name,)).fetchone()
                if skill_row and fact_ids:
                    link_id = memory.link_add('fact', fact_ids[0], '', 'skill', skill_row['id'], skill_name, 'depends_on')
                    print(f"    + link: fact:{fact_ids[0]} → skill:{skill_name} (depends_on, id={link_id})")

        # Simulate feedback: mark first fact as helpful
        if fact_ids:
            memory.feedback_event_add('fact', fact_ids[0], '', True, "", session_id=sid, turn_num=1)

        # Simulate evolution loss recording
        memory.compute_evolution_loss(
            session_id=sid, turns_used=8 + i*2, max_turns=40,
            task_success=True, accessed_fact_ids=fact_ids[:2],
            accessed_skill_names=[], hindsight_hint=''
        )

        # Take snapshot after this task
        snap = snapshot_memory(memory, f"after-task-{i+1}-{task['name']}")
        snapshots.append(snap)

        print(f"    Memory: {snap['total_facts']} facts, {snap['total_links']} links, "
              f"ev={snap['evolution_score']:.3f}")
        print()

    # ── Final Analysis ──

    print("=" * 70)
    print("EVOLUTION ANALYSIS")
    print("=" * 70)

    final = snapshots[-1]

    print("\n--- Memory Growth ---")
    print(f"  Baseline → Final:")
    print(f"    Facts:      {baseline['total_facts']} → {final['total_facts']} (+{final['total_facts'] - baseline['total_facts']})")
    print(f"    Skills:     {baseline['total_skills']} → {final['total_skills']} (+{final['total_skills'] - baseline['total_skills']})")
    print(f"    Wiki:       {baseline['total_wiki']} → {final['total_wiki']}")
    print(f"    Links:      {baseline['total_links']} → {final['total_links']} (+{final['total_links'] - baseline['total_links']})")
    print(f"    Sessions:   {baseline['total_sessions']} → {final['total_sessions']}")

    print("\n--- Knowledge Composition (cluster_search) ---")
    for field, quality in final['cluster_quality'].items():
        print(f"  {field:20s}: bundles={quality['bundle_count']}, items={quality['total_items']}, "
              f"max_relevance={quality['max_relevance']:.3f}")

    print("\n--- Link Type Distribution ---")
    if final['link_types']:
        for lt, count in sorted(final['link_types'].items()):
            print(f"  {lt}: {count}")

    print("\n--- Evolution Trajectory ---")
    for snap in snapshots:
        trend_str = str(snap['evolution_trend'])
        print(f"  {snap['label']:30s}: score={snap['evolution_score']:.3f} trend={trend_str}")

    print("\n--- Trust Evolution ---")
    print(f"  Baseline avg trust: {baseline['avg_trust']:.3f}")
    print(f"  Final avg trust:    {final['avg_trust']:.3f}")

    print("\n--- Category Distribution ---")
    print("  Categories:", dict(final['categories']))

    print("\n--- Cascade Flags ---")
    print(f"  Facts needing review: {final['facts_needing_review']}")
    print(f"  Skills needing review: {final['skills_needing_review']}")

    print("\n--- Dynamic Context ---")
    print(f"  Context prompt length for 'deploy microservice architecture': {final['context_prompt_length']} chars")

    # Cross-domain cluster test
    print("\n--- Cross-Domain Cluster Quality ---")
    cross_domain_queries = [
        "python distributed system security",
        "monitoring data anomaly detection",
        "architecture debugging best practices",
    ]
    for q in cross_domain_queries:
        bundles = memory.cluster_search(q, min_relevance=0.15, limit=5)
        total_items = sum(len(b['facts']) + len(b['skills']) + len(b['wiki_pages']) for b in bundles)
        fields_covered = set()
        for b in bundles:
            for f in b['facts']:
                fields_covered.add(f.get('category', 'unknown'))
        print(f"  '{q}': bundles={len(bundles)}, items={total_items}, fields={fields_covered}")

    # Save results
    results = {
        'snapshots': snapshots,
        'cross_domain_queries': cross_domain_queries,
    }
    with open('/root/nova-agent/evolution_test_results.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)

    memory.close()
    print("\n" + "=" * 70)
    print("TEST COMPLETE — results saved to evolution_test_results.json")
    print("=" * 70)


if __name__ == '__main__':
    run_evolution_test()