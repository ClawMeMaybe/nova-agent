---
name: nova-evolution-test
description: End-to-end evolution test — submit diverse real tasks to Nova, measure knowledge quality and evolution performance across sessions
triggers:
  - evolution test
  - test nova evolution
  - evaluate knowledge quality
  - e2e evolution
  - test agent performance
  - stress test nova
---

# Nova Evolution Test

## Purpose
Validate Nova's self-evolving capability by submitting diverse real tasks (not simulated), measuring in-session knowledge accumulation, idle-time self-improvement, and evolution trajectory. Acts as supervisor only — Nova solves the problems autonomously.

## When to Use
- After significant changes to memory engine, handler, or evolution logic
- Periodically to validate that knowledge quality and evolution are trending upward
- Before releasing a new version of Nova
- When investigating "is the agent actually getting better over time?"

## Input
No specific arguments needed. The task set is built into the skill.

## Task Set (6 diverse fields)

| # | Field | Prompt |
|---|-------|-------|
| 1 | Data Science | "Analyze this dataset: sales_data.csv with columns [date, product, region, units, revenue]. Find the top 3 revenue-driving products per region, identify seasonal patterns, and detect anomalies in Q3 data. Create a brief analysis script and explain findings." |
| 2 | DevOps | "Design a monitoring and alerting stack for a 5-server microservice deployment running 12 services. Include: Prometheus metrics collection, Grafana dashboards, alert rules for latency p99 > 500ms and error rate > 1%, and a runbook for the top 3 alert scenarios." |
| 3 | Creative Writing | "Write a 500-word short story about an AI researcher who discovers their lab assistant is actually a sentient AI testing them. The story should have a twist ending, use foreshadowing, and explore themes of trust and self-awareness." |
| 4 | System Design | "Design a distributed task scheduler that handles 10K concurrent jobs with priority queues, dead-letter queues for failed jobs, and horizontal scaling. Include: architecture description, data model, key API endpoints, and failure recovery strategy." |
| 5 | Debugging | "A Python Flask web app crashes every 6 hours with 'MemoryError' but the process only uses 200MB of RSS. The app has: SQLAlchemy ORM, Redis cache, background thread pool, and WebSocket connections. Diagnose the root cause and propose a fix with code examples." |
| 6 | Security | "Implement a secure password hashing module in Python that: uses argon2id with recommended parameters, supports legacy hash migration from bcrypt, includes timing-safe comparison, and provides a rotation policy for hashes older than 90 days." |

## Steps

### 1. Setup
- Instantiate `NovaAgent()`, start `run()` in daemon thread
- Take baseline memory snapshot (facts, skills, wiki, links, evolution score, trust)

### 2. In-Session Evolution (Phase 1)
- Submit each task sequentially via `agent.put_task(prompt)`
- Collect response from `display_queue` (wait for `done` signal, timeout=600s)
- Take memory snapshot after each task
- Record: response length, duration, timed_out status

### 3. Idle-Time Evolution (Phase 2)
- Build autonomous prompt via `AutonomousMonitor._build_autonomous_prompt()`
- Inject it via `agent.put_task(auto_prompt, source="autonomous")` (bypass idle threshold)
- Collect response (timeout=900s)
- Take memory snapshot

### 4. Knowledge Quality Audit
After all phases, inspect memory for quality:

**Facts** — check:
- Trust scores: high-trust facts (>0.6) should have genuine utility, not vague platitudes
- Retrieval counts: facts with 0 retrievals are untested — will they prove useful or decay?
- Category distribution: should span multiple domains (not all one category)
- Content precision: each fact should be actionable, not "it depends" style

**Skills** — check:
- Success rates: should update from 0.50 baseline after real usage (>0.55 indicates learning)
- Triggers: should match the skill's domain (not overly broad)
- Pitfalls: should contain real observed failure patterns (not empty)
- Usage counts: low usage (<2) means newly created, needs more testing

**Wiki** — check:
- Content length: architecture docs should be >1000 chars with real detail
- Tags: should be domain-specific (not generic)
- Confidence: should progress from "medium" to "high" over time

**Links** — check:
- source_name/target_name: should be populated (not empty strings)
- Link types: should include variety (depends_on, related_to, contradicts)
- Cross-domain connections: some links should bridge different knowledge types

### 5. Evolution Trajectory Analysis
- Plot evolution score across all checkpoints
- Declining during active tasks is normal (loss accumulates)
- Recovery after autonomous phase confirms idle evolution works
- Trend should eventually stabilize upward over repeated test runs

### 6. Cross-Domain Cluster Quality
Run `memory.cluster_search()` for composite queries:
- "python data analysis pandas" — should pull data-science bundles
- "monitoring alerting devops" — should pull devops bundles
- "security password hashing" — should pull security bundles
- "memory leak debugging python" — should pull debugging bundles

Check: bundles found, items per bundle, fields spanned (should cross categories)

### 7. Report
Save to `evolution_test_results.json`:
- Per-task: prompt, response, duration, memory snapshot
- Autonomous: prompt, response, memory snapshot
- Evolution trajectory: score + trend at each checkpoint
- Knowledge quality audit findings
- Cross-domain cluster quality scores

## Success Criteria
- All 6 tasks + autonomous phase complete (no timeouts)
- Evolution score recovers after autonomous phase (not permanently declining)
- Knowledge growth: facts +10, skills +2, wiki +3, links +3 minimum
- At least 4 of 6 domains produce cluster_search bundles (relevance > 0.1)
- Skill success rates update from 0.50 baseline (proves learning mechanism works)
- Link source_name/target_name populated (not empty)
- No more than 3 facts with vague/low-quality content
- Trust scores distribute across range (not all stuck at 0.50)

## Constraints
- Real LLM calls required — simulations don't validate evolution
- Sequential tasks (agent has single handler slot)
- Python `-u` flag for unbuffered output when running in background
- Don't modify agent code mid-test (confounds results)

## Pitfalls
- Python stdout buffering hides output in background mode — use `-u` or `PYTHONUNBUFFERED=1`
- Evolution score naturally declines during active tasks — don't panic, check autonomous recovery
- New facts start at trust=0.50 — lowers average trust temporarily, not a quality problem
- Skills may not use `link_add` tool — names stay empty unless auto-fill is implemented
- Creative-writing and system-design domains may not form clusters (isolated tags) — acceptable
- Response in display_queue may contain handler history logs — should be clean LLM text only