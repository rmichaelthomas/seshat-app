# Delegation facts & groups — PRE-build benchmarks

**Date:** 2026-07-20  
**Branch point:** `main` @ `097e15a`  
**File SHAs verified:** `agreements.py` 33c262d · `identity.py` 01babb7 · `cli.py` abce195  
**liminate:** 0.16.0

Captured in an isolated `HOME` with a freshly-`init`ed starter Agreement,
before any code change. These four decisions must be **identical** post-build
(F-02 byte-compatibility, consistency invariant 2).

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
