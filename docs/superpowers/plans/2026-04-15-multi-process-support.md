# Multi-Process Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a project has multiple start commands (e.g., `npm run server` + `npm run dev`), combine them into a single compound shell command using `&` so both processes launch and stop together.

**Architecture:** In `github.py` `scan()` and `local_scanner.py` `scan()`, join `start_all` with ` & ` when it has 2+ entries to produce the `start` field. No changes to registry, runner, state, or API — the compound command is just a string that existing infrastructure handles via `shell=True`.

**Tech Stack:** Python, pytest

**Spec:** `docs/superpowers/specs/2026-04-15-multi-process-support-design.md`

---

### Task 1: Combine `start_all` into compound `start` in both scan methods

**Files:**
- Modify: `github.py:249-256` (scan result dict)
- Modify: `local_scanner.py:186-192` (scan result dict)
- Test: `tests/test_github.py`
- Test: `tests/test_local_scanner.py`

- [ ] **Step 1: Write failing test for github.py multi-command scan**

Add this test to `tests/test_github.py`:

```python
def test_scan_combines_multiple_start_commands(importer, tmp_path):
    repos = [{
        "name": "astroweather", "full_name": "u/astroweather",
        "clone_url": "https://github.com/u/astroweather.git",
        "description": "Daily signal system", "language": "JavaScript",
        "topics": [], "fork": False,
        "pushed_at": "2026-01-01T00:00:00Z", "default_branch": "main",
    }]
    readme = (
        "# Astroweather\n\n"
        "## Run\n"
        "Open two terminal tabs:\n"
        "```\nnpm run server\n```\n"
        "```\nnpm run dev\n```\n"
        "Open http://localhost:5173\n"
    )
    with patch.object(importer, "fetch_repos", return_value=repos), \
         patch.object(importer, "fetch_readme", return_value=readme), \
         patch.object(importer, "detect_local_path", return_value=None):
        results = importer.scan(registered_names=set())

    r = results[0]
    assert r["start"] == "npm run server & npm run dev"
    assert r["start_all"] == ["npm run server", "npm run dev"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_github.py::test_scan_combines_multiple_start_commands -v`
Expected: FAIL — `start` will be `"npm run server"` (only the first command).

- [ ] **Step 3: Write failing test for local_scanner.py multi-command scan**

Add this test to `tests/test_local_scanner.py`:

```python
def test_scan_combines_multiple_start_commands(scanner, tmp_path):
    readme = "## Usage\n```\nnpm run server\nnpm run dev\n```"
    _make_project(tmp_path, "app", {"requirements.txt": "", "README.md": readme})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["start"] == "npm run server & npm run dev"
    assert results[0]["start_all"] == ["npm run server", "npm run dev"]
```

- [ ] **Step 4: Run test to verify it fails**

Run: `python3 -m pytest tests/test_local_scanner.py::test_scan_combines_multiple_start_commands -v`
Expected: FAIL — `start` will be `"npm run server"` (only the first command).

- [ ] **Step 5: Update `github.py` `scan()` to combine start_all**

In `github.py`, in the `scan()` method, change the `start` line in the result dict (line 255). Replace:

```python
                "start":       extracted["start"],
```

With:

```python
                "start":       " & ".join(extracted["start_all"]) if len(extracted["start_all"]) > 1 else extracted["start"],
```

- [ ] **Step 6: Update `local_scanner.py` `scan()` to combine start_all**

In `local_scanner.py`, in the `scan()` method, change the `start` line in the result dict (line 190). Replace:

```python
                "start":      extracted["start"],
```

With:

```python
                "start":      " & ".join(extracted["start_all"]) if len(extracted["start_all"]) > 1 else extracted["start"],
```

- [ ] **Step 7: Run all tests to verify they pass**

Run: `python3 -m pytest tests/test_github.py tests/test_local_scanner.py -v`
Expected: ALL PASS.

- [ ] **Step 8: Verify single-command projects are unaffected**

Run: `python3 -m pytest -v`
Expected: ALL PASS across all test files. Existing tests like `test_scan_returns_structured_results` (single command) should still pass unchanged.

- [ ] **Step 9: Commit**

```bash
git add github.py local_scanner.py tests/test_github.py tests/test_local_scanner.py
git commit -m "feat: combine multiple start commands with & for multi-process projects"
```
