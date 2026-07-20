# Delegation facts & groups — POST-build benchmarks

**Date:** 2026-07-20  
**Branch:** `feat/delegation-facts-and-groups`  
**liminate:** 0.16.0  
**Full suite:** 492 passed

## Legacy decisions (the §3 four) — must be identical to pre

```
$ seshat agreement check start_project --actor claude-code
ALLOW  mode=permitted
  Rule:   permit actor is claude-code and action is start_project
  Reason: Permitted by Agreement.
  exit=0

$ seshat agreement check stop_orphan --actor claude-code
DENY  mode=forbidden
  Rule:   forbid action is stop_orphan because "orphan termination stays in the dashboard"
  Reason: Prohibition violated: action is stop_orphan. action is stop_orphan.
  exit=1

$ seshat agreement check delete_everything --actor claude-code
DENY  mode=default-deny
  Reason: No Agreement rule permits this action (deny-by-default).
  exit=1

$ seshat agreement check start_project --actor unknown-agent
DENY  mode=default-deny
  Reason: No Agreement rule permits this action (deny-by-default).
  exit=1

```

**Diff vs `feat-delegation-facts-and-groups-benchmarks-pre.md`: EMPTY.**
All four legacy decisions are byte-identical. F-02 / consistency invariant 2 holds —
the four new facts are injected on every call, but the starter Agreement never
references them, and the `composed` program text is character-identical to main.

## New capability evidence (post-only)

```
-- group-conditioned permit: actor IS in engineering --
$ seshat groups check claude-code
engineering
$ seshat agreement check start_project --actor claude-code
ALLOW  mode=permitted
  Rule:   permit action is start_project and actor-groups includes engineering
  Reason: Permitted by Agreement.

-- same Agreement, actor NOT in engineering --
$ seshat groups check claude-code
claude-code belongs to no groups.
$ seshat agreement check start_project --actor claude-code
DENY  mode=default-deny
  Reason: No Agreement rule permits this action (deny-by-default).

-- delegation-depth: tokenless (depth 1) vs 3 delegation hops (depth 4) --
tokenless (delegation-path=[actor], depth=1) -> ALLOW  mode=permitted
delegation-path=['root-agent', 'sub-a', 'sub-b', 'sub-c'] depth=4
  -> DENY  mode=forbidden
  Rule:   forbid delegation-depth is above 3
```

Group membership flips a permit that names no actor, and delegation depth
denies a chain the Agreement never enumerated — both with zero Liminate
vocabulary change.
