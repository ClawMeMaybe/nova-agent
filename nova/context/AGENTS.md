<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-17 | Updated: 2026-04-19 -->

# context

## Purpose
Dynamic system prompt builder — assembles identity, memory context, knowledge catalog, proven facts, proactive recall, and retrieval hints into the prompt that drives every LLM interaction.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package init (empty) |
| `system_prompt.py` | `build_system_prompt(memory)` — assembles the complete system prompt |

## For AI Agents

### Working In This Directory
- `build_system_prompt()` is called on every task — performance matters
- Prompt structure: Role definition → Action Principles → Memory System docs → Memory context (from engine) → Timestamp → Memory stats → L0 meta rules
- Memory stats now show unified format: total/global/project counts, avg_trust, evolution_score
- If project is selected, project info is injected into the system prompt
- The prompt is ~3000 chars of context injection + the full system prompt text
- Never add large content dumps to the system prompt — use the catalog approach (what's available, not the content itself)

### Testing Requirements
- Test that `build_system_prompt()` returns a non-empty string
- Test that memory stats and proven facts are included when present
- Test that meta rules are injected from wiki
- Test that project info is shown when a project is selected

### Common Patterns
- Memory context comes from `memory.build_context_prompt()` — compact catalog, not full knowledge
- L0 meta rules read from `memory.read_layer('L0_meta_rules.txt')` — backward-compatible API
- Timestamp injected as `Today: YYYY-MM-DD Day HH:MM`
- Proactive recall from `memory.proactive_recall()` — keyword-based fact injection

## Dependencies

### Internal
- `nova.memory.engine.NovaMemory` — for context injection, stats, meta rules, proactive recall

### External
- `time` — For timestamp injection

<!-- MANUAL: Custom project notes can be added below -->