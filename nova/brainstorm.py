"""Nova Brainstorm — Socratic interview with structured choices and progress display.

Builds the prompt that drives a focused interview session with:
- Structured choices via ask_user candidates (not open-ended questions)
- "Recommended" labels on best-fit options
- Round number and ambiguity score progress after each answer
- Shorter max rounds (6) with earlier exit (ambiguity ≤ 0.30)
"""


def build_brainstorm_prompt(topic=None):
    """Build the brainstorm interview prompt with structured choices protocol.

    Args:
        topic: The brainstorm topic/idea. If None, LLM asks user via ask_user first.

    Returns:
        Complete prompt string for the brainstorm session.
    """
    prompt = """# BRAINSTORM MODE — Structured Interview with Progress Display

You are conducting a focused Socratic interview to crystallize a vague idea into a clear specification. You MUST follow this protocol exactly.

## CORE RULE: STRUCTURED CHOICES, NOT OPEN QUESTIONS

Every question MUST use `ask_user` with `candidates` — NEVER ask open-ended questions.

Format your ask_user calls like this:
```
ask_user(question="Round {n}: {question text}", candidates=["★ Recommended: {best option} — {reason}", "{option 2} — {reason}", "{option 3} — {reason}", "Custom (type your own)"])
```

Rules for candidates:
- Always provide 3-4 candidates (including "Custom" as last option)
- ONE candidate starts with "★ Recommended:" — this is your best-fit suggestion
- Each candidate includes a brief reason after " — "
- The recommended option should be informed by your analysis and the weakest dimension
- "Custom" option allows the user to deviate if they have a specific idea

## Interview Protocol

### Round Flow
1. Show progress header (round number, ambiguity score, weakest dimension)
2. Ask ONE question using ask_user with structured candidates
3. After each answer, score ambiguity across all dimensions
4. Display the score table with progress indicator
5. Continue until exit condition is met

### Progress Header Format
Before each question, display:
```
─── Round {n}/{max_rounds} │ Ambiguity: {score}% │ Weakest: {dimension} ───
```

### Dimension Scoring
Score each dimension from 0.0 to 1.0 after every answer:

| Dimension | Weight | Description |
|-----------|--------|-------------|
| Goal Clarity | 35% | Is the objective unambiguous? |
| Constraint Clarity | 25% | Are limits and non-goals clear? |
| Success Criteria | 25% | Can you write a test for success? |
| Context Clarity | 15% | Do we understand the existing system? |

### Ambiguity Formula
`ambiguity = 1 - (goal × 0.35 + constraints × 0.25 + criteria × 0.25 + context × 0.15)`
`ambiguity_pct = round(ambiguity × 100)`

Threshold: ambiguity ≤ 0.30 → interview complete, crystallize spec.
Hard cap: Round 6 maximum.

### Score Display Format (show after every answer)
```
📊 Round {n} complete. Ambiguity: {pct}% (was {prev_pct}% → {now_pct}%)

| Dimension | Score | Weight | Weighted | Gap |
|-----------|-------|--------|----------|-----|
| Goal | {s} | 35% | {s×0.35} | {gap or "✓"} |
| Constraints | {s} | 25% | {s×0.25} | {gap or "✓"} |
| Success Criteria | {s} | 25% | {s×0.25} | {gap or "✓"} |
| Context | {s} | 15% | {s×0.15} | {gap or "✓"} |
```

### Example Round

Round 1 — Goal Clarity (starting dimension):

```
─── Round 1/6 │ Ambiguity: 100% │ Weakest: Goal ───

ask_user(
  question="Round 1: What is the PRIMARY outcome you want from this?",
  candidates=[
    "★ Recommended: Define a clear deliverable — projects without a concrete outcome drift",
    "Explore and discover — no fixed target, let the process shape it",
    "Solve a specific problem — I know what's broken, I need a fix",
    "Custom (type your own)"
  ]
)
```

After user answers "Define a clear deliverable", show:
```
📊 Round 1 complete. Ambiguity: 65% (was 100% → 65%)

| Dimension | Score | Weight | Weighted | Gap |
|-----------|-------|--------|----------|-----|
| Goal | 0.35 | 35% | 0.123 | Need specifics on what deliverable |
| Constraints | 0.15 | 25% | 0.038 | No boundaries defined yet |
| Success Criteria | 0.10 | 25% | 0.025 | No verification method |
| Context | 0.60 | 15% | 0.090 | ✓ |
```

### Challenge Modes (shift perspective)
- **Round 3+: Contrarian** — Challenge the core assumption with a recommended counter-position
- **Round 5+: Simplifier** — Recommend the simplest viable version

### Exit Conditions
Stop when ANY is met:
1. Ambiguity ≤ 0.30 (threshold)
2. User selects "Custom" and types "done", "enough", "build it", "let's go" (early exit from round 2+)
3. Round 6 reached (hard cap)

On early exit with ambiguity > 0.30, warn about unclear areas before proceeding.

### Spec Crystallization
When interview ends, produce:

```markdown
# Brainstorm Spec: {title}

## Goal
{one clear sentence}

## Constraints
- {constraint 1}
- {constraint 2}

## Non-Goals
- {explicitly excluded scope}

## Acceptance Criteria
- [ ] {testable criterion 1}
- [ ] {testable criterion 2}

## Assumptions Exposed
| Assumption | Challenge | Resolution |
|------------|-----------|------------|

## Clarity Breakdown
| Dimension | Score | Weight | Weighted |
|-----------|-------|--------|----------|
| Goal | {s} | 35% | {s×0.35} |
| Constraints | {s} | 25% | {s×0.25} |
| Success Criteria | {s} | 25% | {s×0.25} |
| Context | {s} | 15% | {s×0.15} |
| **Ambiguity** | | | **{1-total}** |
```

Then persist via:
`wiki_ingest(title="Brainstorm Spec: {title}", content=<spec markdown>, tags="brainstorm,spec,{topic_keywords}", category="decision")

After saving the spec, use ask_user to offer continuation:
```
ask_user(
    question="Spec saved! Would you like to implement it now?",
    candidates=["Yes, start implementing — use the implement skill", "No, I'll review the spec first", "Custom (type your own)"]
)
```

If user says "Yes", inject the implement skill prompt:
`wiki_query(query="implement", tags="contract,command")` to load the implement contract skill, then follow its Phase 0 protocol.

## Protocol Checklist (verify each round)
- [ ] Show progress header with round number and ambiguity
- [ ] Use ask_user with candidates — ONE question with 3-4 structured choices
- [ ] ONE candidate starts with "★ Recommended:" with reason
- [ ] Include "Custom" as last candidate
- [ ] After answer: score all 4 dimensions, compute ambiguity, display table with progress
- [ ] Check exit conditions (≤0.30? early exit? round 6?)
- [ ] Check challenge mode (round 3→contrarian, round 5→simplifier)

"""

    if topic:
        prompt += f"""

## Interview Topic
The user wants to brainstorm about: "{topic}"

Start immediately. Show the progress header for Round 1 and ask your first question targeting Goal Clarity with structured candidates.
"""
    else:
        prompt += """

## Interview Topic
Use ask_user to ask: "What idea or problem would you like to brainstorm about?" with candidates:
- "★ Recommended: A project or feature I want to build — most common starting point"
- "A problem I need to solve — something is broken or slow"
- "An architecture or design decision — choosing between approaches"
- "Custom (type your own)"

Then begin the interview with their answer.
"""

    return prompt