# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## 5. Update a session summary txt file for each session

At the beginning of a session, you will be notified. Give me a very brief and simplified summary of the previous session. 

At the end of a session or after an important addition, update the session summary.

## 6. Explanation of important code

For any important addition of a feature or function, explain the crux of the code to me in very simple words.

## 7. Make presentation slides 

After completing a stage, make slides, connect it to Google drive when asked "Make slides". 

## 8. Update artifacts at the end of every session if explicitly asked

Update these artifacts if prompted "Update Artifacts".

Status, diagram and plan: https://claude.ai/code/artifact/804fac57-f9a6-413c-b46b-3c6bbe6f55bc

EDA report: https://claude.ai/code/artifact/6bfff915-3934-49a0-b265-53ddcb9ff383

Code walkthrough: https://claude.ai/code/artifact/bdb6da45-5897-4230-b0be-ddd5014f0c34?org=4b4513c0-bacd-4aca-9cc0-be8eef6731b9