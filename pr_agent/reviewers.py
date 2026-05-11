"""CODEOWNERS parsing.

We implement the subset of the CODEOWNERS spec that real repos use:
  - blank lines and `#` comments ignored
  - one rule per line: `<glob> <@user-or-team> [<@user-or-team> ...]`
  - last matching rule wins (per GitHub's rules)
  - `*` matches anywhere, `**` matches across path segments

This is intentionally minimal — the goal is "pick a sensible reviewer",
not "perfectly emulate GitHub". Edge cases (escapes, negations) are
documented as future work in the README.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CodeownersRule:
    pattern: str
    owners: tuple[str, ...]  # raw tokens like "@octocat", "@org/team", "user@x.com"


def parse_codeowners(text: str) -> list[CodeownersRule]:
    rules: list[CodeownersRule] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern, *owners = parts
        rules.append(CodeownersRule(pattern=pattern, owners=tuple(owners)))
    return rules


def _pattern_to_regex(pattern: str) -> re.Pattern:
    """Convert a CODEOWNERS glob to a regex.

    CODEOWNERS patterns are *roughly* gitignore-style. We handle the
    important shapes:
      `*.py`            -> any .py file
      `/path/`          -> the directory and everything under it
      `path/**`         -> path and below
      `**/dir/`         -> any dir named `dir`
    """
    # Anchor leading slash means "from repo root"; otherwise it matches anywhere.
    anchored = pattern.startswith("/")
    p = pattern.lstrip("/")
    # Trailing slash means "directory and contents".
    if p.endswith("/"):
        p = p + "**"

    # Escape regex metacharacters except the glob ones we'll handle.
    escaped = re.escape(p)
    # Restore globs.
    escaped = escaped.replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", ".")

    if anchored:
        regex = "^" + escaped + "$"
    else:
        regex = "(^|/)" + escaped + "$"
    return re.compile(regex)


def owners_for_path(rules: list[CodeownersRule], path: str) -> list[str]:
    """Return the owners list from the *last* matching rule (GitHub's rule)."""
    matched: tuple[str, ...] = ()
    for rule in rules:
        if _pattern_to_regex(rule.pattern).search(path) or fnmatch.fnmatch(path, rule.pattern):
            matched = rule.owners
    # Strip leading "@" and drop email-style entries we can't @-mention.
    out: list[str] = []
    for o in matched:
        if not o.startswith("@"):
            continue  # skip email-only entries
        token = o.lstrip("@")
        out.append(token)
    return out


def split_user_and_team(token: str) -> tuple[str, bool]:
    """`org/team` -> (token, is_team). `octocat` -> (octocat, False)."""
    return token, "/" in token
