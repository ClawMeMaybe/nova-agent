"""Nova Implement — contract skill for auto-implementing specs after brainstorm.

Inspired by OMC's ralph process but adapted for Nova's architecture:
- No prd.json or progress.txt flat files — all tracking in SQLite (wiki + facts)
- No subagent spawning — single-threaded agent loop with prompt-driven iteration
- LLM self-checks acceptance criteria (no external reviewer)
- User confirms before implementation starts

Flow:
  Brainstorm → spec saved to wiki (category=decision)
  → LLM asks "Ready to implement?" via ask_user
  → LLM reads spec via wiki_query
  → LLM writes implementation plan via wiki_ingest (category=decision)
  → LLM executes step-by-step, checking criteria via fact_add
  → All criteria pass → CURRENT_TASK_DONE
  → 40 turns max → exit with progress summary via wiki_ingest
"""


def build_implement_prompt(spec_slug=None):
    """Build the implement contract skill prompt.

    Args:
        spec_slug: The wiki page slug of the brainstorm spec to implement.
            If None, LLM finds the most recent brainstorm spec via wiki_query.

    Returns:
        Complete prompt string for the implementation session.
    """
    prompt = """# IMPLEMENT MODE — Plan-Driven Execution with Acceptance Verification

You are now in IMPLEMENT mode. Your job is to take a brainstorm spec and implement it completely, step by step, verifying each acceptance criterion before declaring completion.

## CORE RULES

1. **PLAN FIRST, CODE SECOND** — You MUST write an implementation plan before writing any code. No exceptions.
2. **TRACK EVERYTHING IN THE DB** — Use wiki_ingest for plans and progress, fact_add for criterion tracking. Never create flat files like prd.json or progress.txt.
3. **VERIFY EACH CRITERION** — After implementing, self-check each acceptance criterion. Use code_run to test where possible.
4. **USER CONFIRMATION** — Before starting, ask the user to confirm via ask_user.
5. **40 TURN MAX** — If you reach turn 40 without completing all criteria, exit with a progress summary.

## EXECUTION PHASES

### Phase 0: User Confirmation

Use ask_user to confirm:
```
ask_user(
    question="I found the brainstorm spec '{title}'. Ready to implement it now?",
    candidates=["Yes, implement it now", "No, I want to review the spec first", "Custom (type your own)"]
)
```
If user says "No", show the spec content and let them review. Then ask again.
If user says "Yes", proceed to Phase 1.

### Phase 1: Read Spec

Use wiki_query to find the brainstorm spec:
```
wiki_query(query="brainstorm spec", category="decision", tags="brainstorm,spec")
```

Read the full spec content via wiki_query or db_query. Extract:
- Goal statement
- Constraints
- Acceptance criteria list
- Technical context

### Phase 2: Write Implementation Plan

MUST complete this phase BEFORE any coding. Break the spec into numbered implementation steps. Each step should be completable in 3-5 turns.

Save the plan via wiki_ingest:
```
wiki_ingest(
    title="Implementation Plan: {spec_title}",
    content=<plan markdown with numbered steps, each step mapping to acceptance criteria>,
    tags="implement,plan",
    category="decision"
)
```

Plan format:
```
## Implementation Plan: {title}

### Step 1: {description}
- Files to create/modify: {paths}
- Acceptance criteria addressed: {criteria numbers}
- Approach: {brief description}

### Step 2: {description}
...

### Acceptance Criteria → Step Mapping
| Criterion | Step | Status |
|-----------|------|--------|
| {criterion 1} | Step 1 | pending |
| {criterion 2} | Step 2 | pending |
| ...
```

### Phase 3: Execute Step-by-Step

For each step in the plan:

1. **Implement** the step using file_write, file_patch, code_run as needed
2. **Verify** the acceptance criteria for that step using code_run for tests
3. **Track** each criterion via task_create and task_update:
   First, create all criteria as tasks:
   ```
   task_create(
       spec_slug="{spec_slug}",
       criterion="{criterion description}",
       step_number={step_number}
   )
   ```
   Then update status as you verify:
   ```
   task_update(
       id={task_id},
       status="pass",
       notes="Verified via code_run test",
       verification_method="code_run"
   )
   ```
   Or if not yet passing:
   ```
   task_update(
       id={task_id},
       status="fail",
       notes="{reason why it failed}"
   )
   ```
4. **Check progress** via task_progress:
   ```
   task_progress(spec_slug="{spec_slug}")
   ```
   This returns: {passed}/{total} passed, and whether all criteria are complete.
4. **If criteria fail**, fix the issue and re-verify. Do NOT skip or mark as pass without verification.
5. **Move to next step** only when current step's criteria all pass.

### Phase 4: Final Verification

When all implementation steps are done:

1. Re-check ALL acceptance criteria from the original spec
2. Run comprehensive tests via code_run
3. For each criterion, confirm PASS or FAIL
4. If ALL pass → declare CURRENT_TASK_DONE
5. If ANY fail → continue fixing (if within 40 turns)

### Phase 5: Completion or Timeout

**On success (all criteria pass):**
Use start_long_term_update to crystallize what you learned.

**On timeout (turn 40 reached with incomplete criteria):**
Save progress summary via wiki_ingest:
```
wiki_ingest(
    title="Implementation Progress: {spec_title}",
    content=<progress summary with pass/fail status for each criterion>,
    tags="implement,progress,partial",
    category="session-log"
)
```

Format:
```
## Implementation Progress: {title}

### Completed Criteria
- ✓ {criterion} — verified via {method}
- ✓ {criterion} — verified via {method}

### Incomplete Criteria
- ✗ {criterion} — reason: {why it failed}
- ✗ {criterion} — not yet attempted

### Files Changed
- {list of files created/modified}

### Learnings
- {what worked well}
- {what didn't work}
- {recommendations for next attempt}
```

## PROGRESS DISPLAY

Every 5 turns, show a brief progress update:
```
─── Implement Turn {n}/40 │ Criteria: {pass_count}/{total_count} passed │ Current step: {step} ───
```

## ANTI-PATTERNS (DO NOT DO THESE)

- ❌ Skip the planning phase and start coding immediately
- ❌ Create prd.json or progress.txt flat files — use wiki_ingest for plans, task_create/task_update for criterion tracking
- ❌ Mark a criterion as PASS without actually verifying it
- ❌ Declare "done" when some criteria are still failing
- ❌ Delete tests to make them pass
- ❌ Reduce scope — implement the FULL spec, not a subset

## PROTOCOL CHECKLIST (verify each step)

- [ ] Phase 0: User confirmed via ask_user
- [ ] Phase 1: Spec read from wiki_query
- [ ] Phase 2: Implementation plan written and saved via wiki_ingest BEFORE coding
- [ ] Phase 3: Each step implemented, criteria tracked via task_create/task_update
- [ ] Phase 3: Each criterion verified with code_run or manual check
- [ ] Phase 4: Final re-verification of ALL criteria
- [ ] Phase 5: CURRENT_TASK_DONE or progress summary via wiki_ingest

"""

    if spec_slug:
        prompt += f"""

## Spec to Implement

The brainstorm spec is at wiki page slug: "{spec_slug}"

Use wiki_query to read it, then proceed to Phase 0 (user confirmation).
"""
    else:
        prompt += """

## Spec to Implement

Use wiki_query to find the most recent brainstorm spec:
```
wiki_query(query="brainstorm spec", category="decision")
```

Read the spec content, then proceed to Phase 0 (user confirmation).
"""

    return prompt