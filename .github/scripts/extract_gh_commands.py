#!/usr/bin/env python3
"""Extract all gh CLI commands and their flags from the codebase.

Scans Python source files for invocations of the ``gh`` CLI tool and
produces a structured JSON inventory of subcommands and flags used.
"""

import json
import os
import re
import sys


# Subcommands recognised by gh CLI (used to identify the command verb)
_GH_SUBCOMMANDS = {
    "api", "auth", "browse", "codespace", "config", "extension",
    "gist", "gpg-key", "issue", "label", "org", "pr", "project",
    "release", "repo", "ruleset", "run", "search", "secret",
    "ssh-key", "status", "variable", "workflow",
}

# ----- regex patterns for the different call styles in MorphoDepot -----

# 1. self.gh(<string>) or self.ghJSON(<string>) where <string> is a
#    plain or f-string (single-line or triple-quoted).
_GH_STR_CALL_RE = re.compile(
    r'self\.gh(?:JSON)?\(\s*'        # self.gh( or self.ghJSON(
    r'(?:f"""(.*?)"""'               # f"""..."""
    r'|f"([^"]*?)"'                  # f"..."
    r"|f'([^']*?)'"                  # f'...'
    r'|"""(.*?)"""'                  # """..."""
    r'|"([^"]*?)"'                   # "..."
    r"|'([^']*?)')",                 # '...'
    re.DOTALL,
)

# 2. commandList = f"""...""".replace("\n"," ").split() patterns —
#    captures the f-string content between the triple quotes.
_FSTRING_CMDLIST_RE = re.compile(
    r'(?:commandList|command)\s*=\s*f"""(.*?)"""',
    re.DOTALL,
)

# 3. commandList = ['api', 'graphql', ...] — list literal assignments.
_LIST_CMDLIST_RE = re.compile(
    r'(?:commandList|command)\s*=\s*(\[.*?\])',
    re.DOTALL,
)

# 4. self.checkCommand([self.ghExecutablePath, 'auth', 'status'])
_CHECK_CMD_RE = re.compile(
    r'self\.checkCommand\(\s*\[self\.ghExecutablePath\s*,\s*(.*?)\]',
    re.DOTALL,
)

# 5. subprocess-style [ghPath, "auth", "login", ...] (Experiments/)
_SUBPROCESS_GH_RE = re.compile(
    r'\[\s*ghPath\s*,(.*?)\]',
    re.DOTALL,
)

# 6. commandList += ["--body", ...] — appended flags
_CMDLIST_APPEND_RE = re.compile(
    r'commandList\s*\+=\s*(\[.*?\])',
    re.DOTALL,
)


def _resolve_fstring_tokens(raw: str) -> list[str]:
    """Extract static tokens from a (possibly f-string) command.

    Dynamic parts (``{...}``) are replaced with a ``<dynamic>`` placeholder
    so that the surrounding static flags remain parseable.
    """
    cleaned = " ".join(raw.split())
    cleaned = re.sub(r"\{[^}]+\}", "<dynamic>", cleaned)
    return cleaned.split()


def _extract_quoted_strings(source: str) -> list[str]:
    """Extract quoted string elements from a list-literal source fragment."""
    return re.findall(r"""['"]([^'"]*?)['"]""", source)


def _classify_tokens(tokens: list[str]) -> dict:
    """Classify a list of command tokens into subcommand(s) and flags."""
    subcommands: list[str] = []
    flags: list[str] = []
    for token in tokens:
        if token == "<dynamic>" or not token:
            continue
        if token.startswith("--"):
            flag_name = token.split("=")[0]
            flags.append(flag_name)
        elif token.startswith("-") and len(token) == 2:
            flags.append(token)
        elif token in _GH_SUBCOMMANDS:
            subcommands.append(token)
        elif subcommands and len(subcommands) == 1:
            # Second token is the action (e.g. "fork" in "repo fork")
            # Only keep it if it looks like a word, not a path or value
            if re.match(r'^[a-z][\w-]*$', token) and "/" not in token:
                subcommands.append(token)

    return {
        "subcommand": " ".join(subcommands[:2]),
        "flags": sorted(set(flags)),
    }


def _line_number(source: str, pos: int) -> int:
    """Return 1-based line number for character position *pos*."""
    return source[:pos].count("\n") + 1


def extract_from_file(filepath: str) -> list[dict]:
    """Extract gh CLI invocations from a single Python file."""
    with open(filepath) as fh:
        source = fh.read()

    results: list[dict] = []
    seen: set[str] = set()

    def _add(info: dict, line: int) -> None:
        if not info["subcommand"]:
            return
        key = f"{info['subcommand']}|{'|'.join(info['flags'])}"
        if key not in seen:
            seen.add(key)
            results.append({**info, "file": filepath, "line": line})

    # 1. String arguments to self.gh() / self.ghJSON()
    for match in _GH_STR_CALL_RE.finditer(source):
        raw = next(g for g in match.groups() if g is not None)
        tokens = _resolve_fstring_tokens(raw)
        if tokens:
            _add(_classify_tokens(tokens), _line_number(source, match.start()))

    # 2. commandList/command = f"""...""" patterns
    for match in _FSTRING_CMDLIST_RE.finditer(source):
        tokens = _resolve_fstring_tokens(match.group(1))
        if tokens:
            # Also look for += appended flags near this line
            search_start = match.end()
            search_end = min(search_start + 300, len(source))
            nearby = source[search_start:search_end]
            for append_match in _CMDLIST_APPEND_RE.finditer(nearby):
                extra = _extract_quoted_strings(append_match.group(1))
                tokens.extend(extra)
            _add(_classify_tokens(tokens), _line_number(source, match.start()))

    # 3. commandList/command = [...] list literal
    for match in _LIST_CMDLIST_RE.finditer(source):
        list_source = match.group(1)
        # Skip lists that reference gitExecutablePath (those are git, not gh)
        if "gitExecutablePath" in list_source:
            continue
        tokens = _extract_quoted_strings(list_source)
        if tokens:
            search_start = match.end()
            search_end = min(search_start + 300, len(source))
            nearby = source[search_start:search_end]
            for append_match in _CMDLIST_APPEND_RE.finditer(nearby):
                extra = _extract_quoted_strings(append_match.group(1))
                tokens.extend(extra)
            _add(_classify_tokens(tokens), _line_number(source, match.start()))

    # 4. self.checkCommand([self.ghExecutablePath, ...])
    for match in _CHECK_CMD_RE.finditer(source):
        tokens = _extract_quoted_strings(match.group(1))
        if tokens:
            _add(_classify_tokens(tokens), _line_number(source, match.start()))

    # 5. subprocess-style [ghPath, ...] (Experiments)
    for match in _SUBPROCESS_GH_RE.finditer(source):
        tokens = _extract_quoted_strings(match.group(1))
        if tokens:
            _add(_classify_tokens(tokens), _line_number(source, match.start()))

    return results


def extract_all(root: str) -> list[dict]:
    """Walk *root* and extract gh CLI commands from all Python files."""
    root = os.path.normpath(root)
    all_commands: list[dict] = []
    for dirpath, dirs, filenames in os.walk(root):
        # Skip hidden directories (but not the root itself)
        rel = os.path.relpath(dirpath, root)
        if rel != "." and any(
            part.startswith(".") for part in rel.split(os.sep)
        ):
            continue
        for fname in filenames:
            if fname.endswith(".py"):
                filepath = os.path.join(dirpath, fname)
                all_commands.extend(extract_from_file(filepath))
    return all_commands


def main() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    commands = extract_all(root)

    # Build a compact summary grouped by subcommand
    summary: dict[str, list[str]] = {}
    for cmd in commands:
        sub = cmd["subcommand"]
        for flag in cmd["flags"]:
            summary.setdefault(sub, [])
            if flag not in summary[sub]:
                summary[sub].append(flag)
        if not cmd["flags"]:
            summary.setdefault(sub, [])

    output = {
        "commands": commands,
        "summary": summary,
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
