"""
amendment_diff.py — ported statement diff + amendment classifier (TI-Q7, v1.0k §55-57).

This is a verbatim port of liminate-dev/app/differ.py's parse_statements(),
diff_statements(), and the TI-Q7 classify_amendment()/classify_monotonicity()/
entrenchment_violations() section (source blob e4742da, ported per the
build's §CRITICAL FINDING: the `liminate` PyPI package does not expose a
statement differ, so the local harness cannot import the platform's copy).

Everything from _KNOWN_VERBS down through classify_amendment() below must
stay byte-identical to the corresponding section of liminate-dev/app/differ.py
— that identity is what the classifier-parity corpus (§9) proves, and it is
how a harness-attested classification here can be trusted to agree with the
platform's authoritative recompute. Do not hand-edit one copy without the
other.

apply_delta() at the bottom has no platform counterpart — it is local-only
glue for constructing a proposed Agreement text from an agent's additions/
removals (§4.2, §5.1), not part of the ported classifier.
"""

from __future__ import annotations

import re

# Deterministic, no-LLM diff engine over .limn statement lines (GAP-3).
#
# The Liminate grammar is bounded (61 reserved words) and one-statement-
# per-line, so a change between two versions of a contract can be
# classified by pattern-matching alone — no model needed to guess intent.

_KNOWN_VERBS = {"define", "require", "forbid", "permit", "remember", "about"}

_BECAUSE_RE = re.compile(r'\bbecause\s+"((?:[^"\\]|\\.)*)"\s*$')
_UNLESS_RE = re.compile(r"\bunless\b")
_UNLESS_CLAUSE_RE = re.compile(r"\bunless\b\s+(.*?)(?:\s+because\b|$)")
_DEFINE_RE = re.compile(r"^define\s+([\w-]+)\s*:\s*(.*)$")
_REMEMBER_RE = re.compile(r"^remember\s+an?\s+\S+\s+called\s+([\w-]+)\s+with\s+(.*)$")
_ABOUT_RE = re.compile(r'^about\s+"((?:[^"\\]|\\.)*)"\s*$')
_VERB_SUBJECT_RE = re.compile(r"^(require|forbid|permit)\s+([\w-]+)\s+(.*)$")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _first_number(text: str) -> str | None:
    m = _NUMBER_RE.search(text)
    return m.group(0) if m else None


def _extract_unless_clause(raw: str) -> str | None:
    m = _UNLESS_CLAUSE_RE.search(raw)
    return m.group(1).strip() if m else None


def _with_rationale(text: str | None, stmt: dict | None) -> str | None:
    if stmt is not None and stmt.get("rationale"):
        return f'{text} because "{stmt["rationale"]}"'
    return text


def _trimmed_display(stmt: dict | None, fallback: str | None) -> str | None:
    """Strip the leading verb and trailing because/unless clauses for prose.

    The JSON entry's before/after fields keep the full raw line (per the
    documented shape); this is only for the human-readable prose text.
    """
    if stmt is None:
        return fallback
    verb, subject, predicate = stmt["verb"], stmt["subject"], stmt["predicate"]
    if verb in ("require", "forbid", "permit") and predicate:
        return f"{subject} {predicate}"
    if verb == "define" and predicate:
        return predicate
    if verb == "remember" and predicate:
        return f"{subject} is {predicate}"
    return fallback


def _parse_line(raw: str) -> dict:
    try:
        return _parse_line_inner(raw)
    except Exception:
        return {
            "raw": raw,
            "verb": "other",
            "subject": raw,
            "predicate": "",
            "has_unless": False,
            "rationale": None,
        }


def _parse_line_inner(raw: str) -> dict:
    first_word = raw.split(None, 1)[0] if raw.split() else ""
    verb = first_word if first_word in _KNOWN_VERBS else "other"

    working = raw
    rationale = None
    m = _BECAUSE_RE.search(working)
    if m:
        rationale = m.group(1)
        working = working[: m.start()].rstrip()

    has_unless = bool(_UNLESS_RE.search(working))

    subject = raw
    predicate = ""

    if verb == "define":
        m = _DEFINE_RE.match(working)
        if m:
            subject, predicate = m.group(1), m.group(2).strip()
    elif verb == "remember":
        m = _REMEMBER_RE.match(working)
        if m:
            subject, predicate = m.group(1), m.group(2).strip()
    elif verb == "about":
        m = _ABOUT_RE.match(raw)
        if m:
            subject, predicate = "about", m.group(1)
    elif verb in ("require", "forbid", "permit"):
        m = _VERB_SUBJECT_RE.match(working)
        if m:
            subject = m.group(2)
            pred_full = m.group(3).strip()
            predicate = _UNLESS_RE.split(pred_full, maxsplit=1)[0].strip()

    return {
        "raw": raw,
        "verb": verb,
        "subject": subject,
        "predicate": predicate,
        "has_unless": has_unless,
        "rationale": rationale,
    }


def parse_statements(source: str) -> list[dict]:
    """Split .limn source into classified statements, one per non-blank line.

    Comments and blank lines are skipped. Parsing is best-effort and never
    raises — an unrecognized line gets verb 'other' and the whole line as
    both 'raw' and 'subject'.
    """
    statements = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        statements.append(_parse_line(stripped))
    return statements


def _generate_prose(
    category: str,
    verb: str,
    subject: str,
    before: str | None,
    after: str | None,
    parent_stmt: dict | None,
    child_stmt: dict | None,
) -> str:
    trimmed_after = _trimmed_display(child_stmt, after)
    trimmed_before = _trimmed_display(parent_stmt, before)

    if category == "unchanged":
        return f"{subject} is unchanged."

    if category == "added":
        if verb == "forbid":
            return f"A new prohibition was added: {_with_rationale(trimmed_after, child_stmt)}"
        if verb == "require":
            return f"A new requirement was added: {_with_rationale(trimmed_after, child_stmt)}"
        if verb == "define":
            return f"A new definition was added: {subject}"
        if verb == "permit":
            return f"A new permission was added: {_with_rationale(trimmed_after, child_stmt)}"
        if verb == "remember":
            return f"A new value was defined: {subject}"
        return f"A new line was added: {trimmed_after}"

    if category == "removed":
        if verb == "require":
            return f"The requirement on {subject} was removed."
        if verb == "forbid":
            return f"The prohibition on {subject} was removed."
        if verb == "permit":
            return f"The permission on {subject} was removed."
        if verb == "define":
            return f"The definition of {subject} was removed."
        if verb == "remember":
            return f"The value {subject} was removed."
        return f'The line "{before}" was removed.'

    if category == "modified":
        if verb == "define":
            return f"The definition of {subject} changed: {trimmed_before} → {trimmed_after}."

        if verb == "require" and parent_stmt is not None and child_stmt is not None:
            old_num = _first_number(parent_stmt["predicate"])
            new_num = _first_number(child_stmt["predicate"])
            if old_num is not None and new_num is not None and old_num != new_num:
                return f"The requirement on {subject} changed from {old_num} to {new_num}."

        if child_stmt is not None and child_stmt["has_unless"] and (
            parent_stmt is None or not parent_stmt["has_unless"]
        ):
            clause = _extract_unless_clause(child_stmt["raw"])
            if clause:
                return f"The rule on {subject} gained a condition: {clause}."

        return f"The rule on {subject} changed: {trimmed_before} → {trimmed_after}."

    return f"{subject} changed."


def _make_entry(
    category: str,
    verb: str,
    subject: str,
    before: str | None,
    after: str | None,
    parent_stmt: dict | None,
    child_stmt: dict | None,
) -> dict:
    prose = _generate_prose(category, verb, subject, before, after, parent_stmt, child_stmt)
    return {
        "category": category,
        "subject": subject,
        "before": before,
        "after": after,
        "prose": prose,
    }


def _diff_matched(verb: str, parent_list: list[dict], child_list: list[dict]) -> list[dict]:
    entries = []
    n = min(len(parent_list), len(child_list))
    for i in range(n):
        p, c = parent_list[i], child_list[i]
        category = "unchanged" if p["raw"] == c["raw"] else "modified"
        entries.append(_make_entry(category, verb, p["subject"], p["raw"], c["raw"], p, c))
    for p in parent_list[n:]:
        entries.append(_make_entry("removed", verb, p["subject"], p["raw"], None, p, None))
    for c in child_list[n:]:
        entries.append(_make_entry("added", verb, c["subject"], None, c["raw"], None, c))
    return entries


def diff_statements(parent_src: str, child_src: str) -> list[dict]:
    """Compare two .limn sources. Returns a list of change entries.

    Matching is by (verb, subject). A subject present in both with a
    different raw line is 'modified'. Present only in child is 'added'.
    Only in parent is 'removed'. Identical raw line is 'unchanged'.
    """
    try:
        parent_stmts = parse_statements(parent_src)
        child_stmts = parse_statements(child_src)
    except Exception:
        return []

    def index(stmts: list[dict]) -> dict[tuple[str, str], list[dict]]:
        idx: dict[tuple[str, str], list[dict]] = {}
        for s in stmts:
            idx.setdefault((s["verb"], s["subject"]), []).append(s)
        return idx

    parent_idx = index(parent_stmts)
    child_idx = index(child_stmts)

    changes: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()

    for s in parent_stmts + child_stmts:
        key = (s["verb"], s["subject"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        changes.extend(
            _diff_matched(key[0], parent_idx.get(key, []), child_idx.get(key, []))
        )

    return changes


# ── TI-Q7 (v1.0k §55-57) — amendment monotonicity + entrenchment ──────────
#
# Deterministic classification over diff_statements() output. No LLM: the
# grammar is bounded, so intent (does this change add or remove a
# restriction?) is pattern-matched from category + verb, exactly like the
# prose generator above. Fail-safe throughout: any change this classifier
# cannot prove is restriction-adding is treated as restriction-removing
# (de-escalating), never as safe.

_QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')

_CEILING_RE = re.compile(r"\bat most\b|\bno more than\b|<=|<")
_FLOOR_RE = re.compile(r"\bat least\b|\bno less than\b|>=|>")


def _entry_verb(entry: dict) -> str:
    """The verb of a diff_statements() entry. diff_statements() does not
    store verb on the entry itself (only category/subject/before/after/
    prose), so it is re-derived by re-parsing whichever raw line the entry
    carries — the same deterministic parser, not a second implementation."""
    raw = entry["after"] if entry["after"] is not None else entry["before"]
    return _parse_line(raw)["verb"]


def _entry_statements(entry: dict) -> tuple[dict | None, dict | None]:
    """Re-parse an entry's before/after raw lines back into full statement
    dicts (verb/subject/predicate/has_unless), for the entries — 'modified',
    mainly — where the classifier needs more than category and verb."""
    parent_stmt = _parse_line(entry["before"]) if entry["before"] is not None else None
    child_stmt = _parse_line(entry["after"]) if entry["after"] is not None else None
    return parent_stmt, child_stmt


def _threshold_direction(before_predicate: str, after_predicate: str) -> str | None:
    """'tightened' | 'loosened' | None (ambiguous) for a require predicate's
    numeric threshold change. 'Tightened' = the direction that reduces
    permission: a floor (at least/>=/>) raised, or a ceiling (at most/<=/<)
    lowered. Any predicate shape this cannot confidently read — no operator
    words found, both floor- and ceiling-shaped language present, or no
    parseable numbers — returns None so the caller fails safe."""
    old_num = _first_number(before_predicate)
    new_num = _first_number(after_predicate)
    if old_num is None or new_num is None or old_num == new_num:
        return None
    try:
        old_val, new_val = float(old_num), float(new_num)
    except ValueError:
        return None

    is_ceiling = bool(_CEILING_RE.search(before_predicate) or _CEILING_RE.search(after_predicate))
    is_floor = bool(_FLOOR_RE.search(before_predicate) or _FLOOR_RE.search(after_predicate))

    if is_floor and not is_ceiling:
        return "tightened" if new_val > old_val else "loosened"
    if is_ceiling and not is_floor:
        return "tightened" if new_val < old_val else "loosened"
    return None  # ambiguous predicate shape — fail safe, never monotonic


def _is_monotonic_entry(entry: dict) -> bool:
    """True only for the explicit §56 restriction-adding shapes. Everything
    else — including any 'modified' shape not explicitly listed, per the
    fail-safe invariant — is restriction-removing."""
    category = entry["category"]
    if category == "unchanged":
        return True  # excluded by the caller; harmless either way
    verb = _entry_verb(entry)

    if category == "added":
        return verb in ("forbid", "require")
    if category == "removed":
        return verb == "permit"
    if category == "modified" and verb == "require":
        parent_stmt, child_stmt = _entry_statements(entry)
        if parent_stmt is not None and child_stmt is not None:
            direction = _threshold_direction(parent_stmt["predicate"], child_stmt["predicate"])
            if direction == "tightened":
                return True
    return False


def classify_monotonicity_from_changes(changes: list[dict]) -> str:
    """'monotonic' | 'de-escalating' over an already-computed diff_statements()
    change list. Exposed separately from classify_monotonicity() because the
    Sentinel recompute (app/sentinel.py) classifies a stored proposed_delta
    directly, without reconstructing full before/after Agreement sources the
    platform may not hold (v1.0k §6.2)."""
    for entry in changes:
        if entry["category"] == "unchanged":
            continue
        if not _is_monotonic_entry(entry):
            return "de-escalating"
    return "monotonic"


def classify_monotonicity(before_src: str, after_src: str) -> str:
    """Return 'monotonic' | 'de-escalating' over the statement diff.
    Fail-safe: any change not provably restriction-adding is de-escalating."""
    return classify_monotonicity_from_changes(diff_statements(before_src, after_src))


def _touched_keys(entry: dict) -> set[tuple[str, str]]:
    """(verb, subject)-style keys this entry touches. Includes the entry's
    parsed (verb, subject) pair, plus (verb, literal) for every quoted
    string literal in its predicate(s) — a fact-comparison statement like
    `forbid action is "stop_orphan"` parses to subject 'action' (the fact
    name), so protecting the specific rule by its value ('stop_orphan')
    requires also keying on the quoted literal, not only the bare subject."""
    verb = _entry_verb(entry)
    keys = {(verb, entry["subject"])}
    for raw in (entry.get("before"), entry.get("after")):
        if raw is None:
            continue
        predicate = _parse_line(raw)["predicate"] or ""
        for literal in _QUOTED_RE.findall(predicate):
            keys.add((verb, literal))
    return keys


def entrenchment_violations(
    before_src: str, after_src: str, entrenched_keys: set[tuple[str, str]]
) -> list[tuple[str, str]]:
    """The protected (verb, subject) keys this amendment touches (added,
    removed, or modified). Empty = clean. Sorted for deterministic output."""
    if not entrenched_keys:
        return []
    changes = diff_statements(before_src, after_src)
    touched: set[tuple[str, str]] = set()
    for entry in changes:
        if entry["category"] == "unchanged":
            continue
        touched |= _touched_keys(entry)
    return sorted(touched & entrenched_keys)


def classify_amendment(
    before_src: str, after_src: str, entrenched_keys: set[tuple[str, str]]
) -> dict:
    """The full §55-57 verdict for a proposed amendment. entrenched-violation
    takes precedence over monotonicity — an entrenched rule cannot be
    monotonically edited either."""
    violations = entrenchment_violations(before_src, after_src, entrenched_keys)
    if violations:
        return {"class": "entrenched-violation", "violations": violations}
    return {"class": classify_monotonicity(before_src, after_src)}


# ── Local-only: proposed-Agreement construction (no platform counterpart) ──
#
# amend_agreement (mcp_server.py) and `agreement amend --apply` (cli.py) both
# express a proposed change as explicit full canonical statement lines to add
# or remove, never a free-form edit. This is the one place that turns those
# lines into a candidate Agreement source, in memory only — neither caller
# writes the result to disk itself (TI-Q6c).


def apply_delta(before_src: str, additions: list[str], removals: list[str]) -> str:
    """Return before_src with each exact-match removal line dropped and each
    addition line appended. Matching is by stripped raw line text — the
    caller must propose full canonical lines, not partial edits."""
    removal_set = {r.strip() for r in removals if r.strip()}
    lines = [line for line in before_src.splitlines() if line.strip() not in removal_set]
    for addition in additions:
        addition = addition.strip()
        if addition:
            lines.append(addition)
    result = "\n".join(lines)
    if result and not result.endswith("\n"):
        result += "\n"
    return result
