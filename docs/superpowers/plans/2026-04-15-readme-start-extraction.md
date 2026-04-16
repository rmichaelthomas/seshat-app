# Fix README Start-Command Extraction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix false-positive start-command extraction from READMEs by adding an exclusion filter and section-aware parsing, and collect all run commands for future multi-process support.

**Architecture:** Add `_SETUP_COMMANDS` exclusion regex and `_split_sections()` / `_extract_start_commands()` helpers to `github.py`. Update `local_scanner.py` to use the shared logic for README fallback. Both modules return `start_all: list[str]` alongside the existing `start: str | None`.

**Tech Stack:** Python, regex, pytest

**Spec:** `docs/superpowers/specs/2026-04-15-readme-start-command-extraction-design.md`

---

### Task 1: Add `_SETUP_COMMANDS` exclusion filter to `github.py`

**Files:**
- Modify: `github.py:13-21` (module-level constants)
- Test: `tests/test_github.py`

- [ ] **Step 1: Write failing tests for the exclusion filter**

Add these tests to `tests/test_github.py`:

```python
def test_extract_start_skips_npm_install(importer):
    readme = "## Setup\n```\nnpm install\n```\n## Run\n```\nnpm run dev\n```"
    result = importer._extract_fields(readme)
    assert result["start"] == "npm run dev"


def test_extract_start_skips_npm_ci(importer):
    readme = "## Setup\n```\nnpm ci\n```"
    result = importer._extract_fields(readme)
    assert result["start"] is None


def test_extract_start_skips_yarn_install(importer):
    readme = "## Setup\n```\nyarn install\n```\n## Run\n```\nyarn dev\n```"
    result = importer._extract_fields(readme)
    assert result["start"] == "yarn dev"


def test_extract_start_skips_yarn_add(importer):
    readme = "```\nyarn add express\n```"
    result = importer._extract_fields(readme)
    assert result["start"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_github.py::test_extract_start_skips_npm_install tests/test_github.py::test_extract_start_skips_npm_ci tests/test_github.py::test_extract_start_skips_yarn_install tests/test_github.py::test_extract_start_skips_yarn_add -v`
Expected: FAIL — `npm install` / `npm ci` / `yarn install` / `yarn add` are currently returned as start commands.

- [ ] **Step 3: Add `_SETUP_COMMANDS` regex to `github.py`**

Add after `_START_PATTERNS` (after line 16):

```python
_SETUP_COMMANDS = re.compile(
    r"^\s*("
    r"npm\s+(?:install|ci|init|link|uninstall)"
    r"|yarn\s+(?:add|install)"
    r"|pip3?\s+install"
    r"|cargo\s+build"
    r"|go\s+(?:get|install)"
    r"|make\s+(?:install|build)"
    r"|bundle\s+install"
    r")\b",
    re.MULTILINE,
)
```

- [ ] **Step 4: Apply the filter in `_extract_fields()`**

In `github.py`, update the start-command extraction block in `_extract_fields()` (lines 111-121). Replace:

```python
        # Start command — look in fenced code blocks first, then bare lines
        start = None
        code_blocks = re.findall(r"```[^\n]*\n(.*?)```", readme, re.DOTALL)
        for block in code_blocks:
            m = _START_PATTERNS.search(block)
            if m:
                start = m.group(0).strip()
                break
        if not start:
            m = _START_PATTERNS.search(readme)
            if m:
                start = m.group(0).strip()
```

With:

```python
        # Start command — look in fenced code blocks first, then bare lines
        start = None
        code_blocks = re.findall(r"```[^\n]*\n(.*?)```", readme, re.DOTALL)
        for block in code_blocks:
            for m in _START_PATTERNS.finditer(block):
                candidate = m.group(0).strip()
                if not _SETUP_COMMANDS.match(candidate):
                    start = candidate
                    break
            if start:
                break
        if not start:
            for m in _START_PATTERNS.finditer(readme):
                candidate = m.group(0).strip()
                if not _SETUP_COMMANDS.match(candidate):
                    start = candidate
                    break
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_github.py -v`
Expected: ALL PASS (including the 4 new tests and all existing tests).

- [ ] **Step 6: Commit**

```bash
git add github.py tests/test_github.py
git commit -m "feat: add _SETUP_COMMANDS exclusion filter for README extraction"
```

---

### Task 2: Add `_split_sections()` and `_extract_start_commands()` to `github.py`

**Files:**
- Modify: `github.py` (add two new functions, update `_extract_fields`)
- Test: `tests/test_github.py`

- [ ] **Step 1: Write failing tests for section-aware extraction**

Add these tests to `tests/test_github.py`. These test the new `_extract_start_commands` function directly (imported at module level):

```python
from github import _extract_start_commands


def test_extract_start_commands_prefers_run_section():
    readme = "## Setup\n```\nnpm run build\n```\n## Running\n```\nnpm run dev\n```"
    result = _extract_start_commands(readme)
    assert result == ["npm run dev"]


def test_extract_start_commands_falls_back_to_unclassified():
    readme = "## About\n```\npython3 app.py\n```"
    result = _extract_start_commands(readme)
    assert result == ["python3 app.py"]


def test_extract_start_commands_falls_back_to_setup_section():
    readme = "## Install\n```\npython3 setup.py\n```"
    result = _extract_start_commands(readme)
    assert result == ["python3 setup.py"]


def test_extract_start_commands_collects_multiple():
    readme = "## Usage\n```\nnpm run server\nnpm run dev\n```"
    result = _extract_start_commands(readme)
    assert result == ["npm run server", "npm run dev"]


def test_extract_start_commands_empty_readme():
    assert _extract_start_commands("") == []


def test_extract_start_commands_no_headings():
    readme = "Run this:\n```\nnode server.js\n```"
    result = _extract_start_commands(readme)
    assert result == ["node server.js"]


def test_extract_start_commands_getting_started_heading():
    readme = "## Getting Started\n```\nnpm run dev\n```\n## Build\n```\nnpm run build\n```"
    result = _extract_start_commands(readme)
    assert result == ["npm run dev"]


def test_extract_start_commands_filters_setup_in_run_section():
    readme = "## Usage\n```\nnpm install\nnpm run dev\n```"
    result = _extract_start_commands(readme)
    assert result == ["npm run dev"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_github.py::test_extract_start_commands_prefers_run_section tests/test_github.py::test_extract_start_commands_collects_multiple -v`
Expected: FAIL — `_extract_start_commands` does not exist yet.

- [ ] **Step 3: Implement `_split_sections()` in `github.py`**

Add after the `_SETUP_COMMANDS` definition:

```python
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)", re.MULTILINE)

_RUN_HEADINGS = re.compile(
    r"(?:run|start|usage|develop|launch|getting.started|quick.start)",
    re.IGNORECASE,
)
_SETUP_HEADINGS = re.compile(
    r"(?:setup|install|prerequisites|requirements|build|dependencies)",
    re.IGNORECASE,
)


def _split_sections(readme: str) -> list[tuple[str, str]]:
    """Split README into (heading, body) tuples. Preamble gets heading ''."""
    splits = list(_HEADING_RE.finditer(readme))
    sections: list[tuple[str, str]] = []
    if not splits:
        return [("", readme)]
    # Preamble before first heading
    if splits[0].start() > 0:
        sections.append(("", readme[: splits[0].start()]))
    for i, m in enumerate(splits):
        heading = m.group(2).strip()
        start = m.end()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(readme)
        sections.append((heading, readme[start:end]))
    return sections
```

- [ ] **Step 4: Implement `_extract_start_commands()` in `github.py`**

Add after `_split_sections`:

```python
def _find_commands_in_text(text: str) -> list[str]:
    """Find all start-pattern matches in text, excluding setup commands.
    Searches fenced code blocks first, then bare lines."""
    commands: list[str] = []
    seen = set()
    code_blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
    for block in code_blocks:
        for m in _START_PATTERNS.finditer(block):
            cmd = m.group(0).strip()
            if not _SETUP_COMMANDS.match(cmd) and cmd not in seen:
                commands.append(cmd)
                seen.add(cmd)
    # Also search bare lines (outside code blocks)
    bare = re.sub(r"```[^\n]*\n.*?```", "", text, flags=re.DOTALL)
    for m in _START_PATTERNS.finditer(bare):
        cmd = m.group(0).strip()
        if not _SETUP_COMMANDS.match(cmd) and cmd not in seen:
            commands.append(cmd)
            seen.add(cmd)
    return commands


def _extract_start_commands(readme: str) -> list[str]:
    """Extract all start commands from a README using section-aware parsing."""
    sections = _split_sections(readme)

    run_cmds: list[str] = []
    unclassified_cmds: list[str] = []
    setup_cmds: list[str] = []

    for heading, body in sections:
        cmds = _find_commands_in_text(body)
        if not cmds:
            continue
        if _RUN_HEADINGS.search(heading):
            run_cmds.extend(cmds)
        elif _SETUP_HEADINGS.search(heading):
            setup_cmds.extend(cmds)
        else:
            unclassified_cmds.extend(cmds)

    return run_cmds or unclassified_cmds or setup_cmds
```

- [ ] **Step 5: Update `_extract_fields()` to use `_extract_start_commands()`**

Replace the start-command block in `_extract_fields()` (the code from Task 1 step 4) with:

```python
        # Start commands — section-aware extraction
        commands = _extract_start_commands(readme)
        start = commands[0] if commands else None
```

And update the return statement to include `start_all`:

```python
        return {"port": port, "start": start, "start_all": commands, "notes": notes}
```

- [ ] **Step 6: Update `scan()` to pass through `start_all`**

In the `scan()` method, update the result dict (around line 173) to include:

```python
                "start":       extracted["start"],
                "start_all":   extracted["start_all"],
```

- [ ] **Step 7: Update existing tests that check `_extract_fields` return value**

Update `test_extract_fields_missing` and `test_extract_fields_empty_readme` to also check `start_all`:

```python
def test_extract_fields_missing(importer):
    readme = "# My App\n\nNo useful info here."
    result = importer._extract_fields(readme)
    assert result["port"] is None
    assert result["start"] is None
    assert result["start_all"] == []


def test_extract_fields_empty_readme(importer):
    result = importer._extract_fields("")
    assert result["port"] is None
    assert result["start"] is None
    assert result["start_all"] == []
    assert result["notes"] is None
```

Update `test_scan_returns_structured_results` to also assert on `start_all`:

```python
    assert r["start"] == "python3 app.py"
    assert r["start_all"] == ["python3 app.py"]
```

- [ ] **Step 8: Run all tests to verify they pass**

Run: `python -m pytest tests/test_github.py -v`
Expected: ALL PASS.

- [ ] **Step 9: Commit**

```bash
git add github.py tests/test_github.py
git commit -m "feat: section-aware README extraction with _extract_start_commands"
```

---

### Task 3: Update `local_scanner.py` to use shared logic

**Files:**
- Modify: `local_scanner.py:1-10` (imports), `local_scanner.py:59-62` (`_find_start`), `local_scanner.py:124-152` (start extraction in `_extract`)
- Test: `tests/test_local_scanner.py`

- [ ] **Step 1: Write failing tests for local scanner changes**

Add these tests to `tests/test_local_scanner.py`:

```python
def test_extract_start_skips_npm_install_in_readme(scanner, tmp_path):
    readme = "## Setup\n```\nnpm install\n```\n## Run\n```\nnpm run dev\n```"
    _make_project(tmp_path, "app", {"package.json": "{}", "README.md": readme})
    results = scanner.scan(str(tmp_path), registered_names=set())
    # package.json has no scripts, so falls through to README
    assert results[0]["start"] is None or results[0]["start"] == "npm run dev"


def test_extract_start_all_from_readme(scanner, tmp_path):
    readme = "## Usage\n```\nnpm run server\nnpm run dev\n```"
    _make_project(tmp_path, "app", {"requirements.txt": "", "README.md": readme})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["start_all"] == ["npm run server", "npm run dev"]


def test_extract_start_all_from_package_json(scanner, tmp_path):
    pkg = json.dumps({"scripts": {"dev": "next dev", "start": "node index.js"}})
    _make_project(tmp_path, "app", {"package.json": pkg})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["start"] == "next dev"
    assert results[0]["start_all"] == ["next dev", "node index.js"]


def test_extract_start_all_empty_when_none(scanner, tmp_path):
    _make_project(tmp_path, "app", {"go.mod": "module app\n"})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["start_all"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_local_scanner.py::test_extract_start_all_from_readme tests/test_local_scanner.py::test_extract_start_all_from_package_json tests/test_local_scanner.py::test_extract_start_all_empty_when_none -v`
Expected: FAIL — `start_all` key does not exist in results.

- [ ] **Step 3: Update imports in `local_scanner.py`**

Change line 8 from:

```python
from github import _PORT_PATTERNS, _START_PATTERNS
```

To:

```python
from github import _PORT_PATTERNS, _START_PATTERNS, _SETUP_COMMANDS, _extract_start_commands
```

- [ ] **Step 4: Rename `_find_start` to `_find_starts` and apply exclusion filter**

Replace the `_find_start` function (lines 59-62):

```python
def _find_starts(text: str) -> list[str]:
    """Apply _START_PATTERNS to text, return all matches excluding setup commands."""
    results = []
    for m in _START_PATTERNS.finditer(text):
        cmd = m.group(0).strip()
        if not _SETUP_COMMANDS.match(cmd) and cmd not in results:
            results.append(cmd)
    return results
```

- [ ] **Step 5: Update `_extract()` to return `start_all` and use new functions**

Replace the start command extraction section (lines 124-152) in `_extract()`:

```python
    # ── Start command extraction ───────────────────────────────────────────
    start_all: list[str] = []

    # 1. package.json scripts.dev then scripts.start
    if pkg_scripts:
        for key in ("dev", "start"):
            val = pkg_scripts.get(key, "").strip()
            if val:
                start_all.append(val)

    # 2. Makefile
    if not start_all:
        text = _read(project_dir / "Makefile")
        if text:
            start_all = _find_starts(text)

    # 3. README — use section-aware extraction
    if not start_all:
        for readme_name in ("README.md", "README.rst", "README"):
            text = _read(project_dir / readme_name)
            if text:
                start_all = _extract_start_commands(text)
                if start_all:
                    break

    start = start_all[0] if start_all else None

    return {"port": port, "start": start, "start_all": start_all}
```

- [ ] **Step 6: Update `scan()` to pass through `start_all`**

In the `scan()` method result dict (around line 183), add `start_all`:

```python
            results.append({
                "name":       name,
                "directory":  str(child),
                "port":       extracted["port"],
                "start":      extracted["start"],
                "start_all":  extracted["start_all"],
                "registered": registered,
            })
```

- [ ] **Step 7: Run all tests to verify they pass**

Run: `python -m pytest tests/test_local_scanner.py -v`
Expected: ALL PASS.

- [ ] **Step 8: Run the full test suite**

Run: `python -m pytest -v`
Expected: ALL PASS across all test files.

- [ ] **Step 9: Commit**

```bash
git add local_scanner.py tests/test_local_scanner.py
git commit -m "feat: update local_scanner to use shared section-aware extraction"
```
