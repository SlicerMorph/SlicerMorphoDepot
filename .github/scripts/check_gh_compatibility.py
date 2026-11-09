#!/usr/bin/env python3
"""Check gh CLI compatibility by cross-referencing extracted commands against
recent gh CLI release notes for deprecation and breaking-change notices.

Usage:
    python check_gh_compatibility.py [--inventory inventory.json]
                                      [--releases <N>]
                                      [--output report.md]

The script:
1. Reads the command inventory produced by ``extract_gh_commands.py``.
2. Fetches recent release notes from the ``cli/cli`` GitHub repository
   using the GitHub REST API.
3. Searches each release body for keywords that signal deprecations or
   breaking changes.
4. Cross-references any flagged release notes against the subcommands and
   flags used in the inventory.
5. Produces a Markdown report and exits with a non-zero code when issues
   are found.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any


_DEPRECATION_KEYWORDS = [
    "deprecat",
    "breaking change",
    "removed",
    "no longer support",
    "will be removed",
    "replace with",
    "renamed",
    "obsolete",
    "sunset",
    "end of life",
    "end-of-life",
    "backward.incompatible",
    "backwards.incompatible",
]

_KEYWORD_RE = re.compile(
    "|".join(_DEPRECATION_KEYWORDS), re.IGNORECASE
)


def fetch_releases(count: int = 20) -> list[dict[str, Any]]:
    """Fetch the latest *count* releases from cli/cli using the GitHub API.

    Prefers the ``gh`` CLI when available so that authentication is handled
    automatically.  Falls back to ``curl`` with a ``GITHUB_TOKEN`` env var.
    """
    gh = os.environ.get("GH_CLI_PATH", "gh")
    try:
        raw = subprocess.check_output(
            [
                gh, "api",
                f"/repos/cli/cli/releases?per_page={count}",
                "--cache", "1h",
            ],
            text=True,
            stderr=subprocess.PIPE,
        )
        return json.loads(raw)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    # Fallback: curl
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = ["-H", f"Authorization: token {token}"] if token else []
    try:
        raw = subprocess.check_output(
            [
                "curl", "-sf",
                *headers,
                "-H", "Accept: application/vnd.github+json",
                f"https://api.github.com/repos/cli/cli/releases?per_page={count}",
            ],
            text=True,
            stderr=subprocess.PIPE,
        )
        return json.loads(raw)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []


def _flagged_lines(body: str) -> list[str]:
    """Return lines from *body* that contain deprecation keywords."""
    flagged = []
    for line in body.splitlines():
        if _KEYWORD_RE.search(line):
            flagged.append(line.strip())
    return flagged


def check_compatibility(
    inventory: dict[str, Any],
    releases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cross-reference *inventory* against *releases*.

    Returns a list of alert dicts for each potential compatibility issue.
    """
    # Collect all subcommands and flags in use
    used_subcommands: set[str] = set()
    used_flags: set[str] = set()
    for cmd in inventory.get("commands", []):
        sub = cmd.get("subcommand", "")
        if sub:
            used_subcommands.add(sub)
            # Also add the top-level verb (e.g. "repo" from "repo fork")
            parts = sub.split()
            if parts:
                used_subcommands.add(parts[0])
        for flag in cmd.get("flags", []):
            used_flags.add(flag)

    alerts: list[dict[str, Any]] = []
    for release in releases:
        tag = release.get("tag_name", "unknown")
        body = release.get("body", "") or ""
        flagged = _flagged_lines(body)
        if not flagged:
            continue
        for line in flagged:
            line_lower = line.lower()
            # Check if any of our subcommands or flags appear in this line
            matched_commands: list[str] = []
            matched_flags: list[str] = []
            for sub in used_subcommands:
                # Look for mentions of the subcommand tokens
                for token in sub.split():
                    if re.search(rf"\b{re.escape(token)}\b", line_lower):
                        matched_commands.append(sub)
                        break
            for flag in used_flags:
                if flag in line_lower or flag.lstrip("-") in line_lower:
                    matched_flags.append(flag)
            if matched_commands or matched_flags:
                alerts.append({
                    "release": tag,
                    "line": line,
                    "matched_commands": sorted(set(matched_commands)),
                    "matched_flags": sorted(set(matched_flags)),
                })
    return alerts


def generate_report(
    inventory: dict[str, Any],
    releases: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
    gh_version: str,
) -> str:
    """Produce a Markdown report."""
    lines: list[str] = []
    lines.append("# gh CLI Compatibility Report\n")
    lines.append(f"**Checked against:** {len(releases)} most recent `cli/cli` releases\n")
    lines.append(f"**Current gh version:** {gh_version}\n")

    # Inventory summary
    lines.append("## Commands used by MorphoDepot\n")
    lines.append("| Subcommand | Flags | File | Line |")
    lines.append("|------------|-------|------|------|")
    for cmd in inventory.get("commands", []):
        sub = cmd.get("subcommand", "")
        flags = ", ".join(cmd.get("flags", [])) or "—"
        filepath = cmd.get("file", "")
        line_no = cmd.get("line", "")
        lines.append(f"| `{sub}` | `{flags}` | `{filepath}` | {line_no} |")
    lines.append("")

    # Alerts
    if alerts:
        lines.append("## ⚠️  Potential compatibility issues\n")
        for alert in alerts:
            lines.append(f"### Release `{alert['release']}`\n")
            lines.append(f"> {alert['line']}\n")
            if alert["matched_commands"]:
                lines.append(
                    f"**Matched subcommands:** {', '.join(f'`{c}`' for c in alert['matched_commands'])}\n"
                )
            if alert["matched_flags"]:
                lines.append(
                    f"**Matched flags:** {', '.join(f'`{f}`' for f in alert['matched_flags'])}\n"
                )
    else:
        lines.append("## ✅  No compatibility issues detected\n")
        lines.append(
            "None of the recent `cli/cli` release notes mention deprecations "
            "or breaking changes that affect the commands used by MorphoDepot.\n"
        )

    return "\n".join(lines)


def get_gh_version() -> str:
    """Return the installed gh CLI version string."""
    gh = os.environ.get("GH_CLI_PATH", "gh")
    try:
        return subprocess.check_output(
            [gh, "--version"], text=True, stderr=subprocess.PIPE
        ).strip().splitlines()[0]
    except Exception:
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description="Check gh CLI compatibility")
    parser.add_argument(
        "--inventory",
        default="gh_inventory.json",
        help="Path to inventory JSON from extract_gh_commands.py",
    )
    parser.add_argument(
        "--releases",
        type=int,
        default=20,
        help="Number of recent cli/cli releases to check (default: 20)",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Path to write Markdown report (default: stdout)",
    )
    args = parser.parse_args()

    # Load inventory
    if not os.path.exists(args.inventory):
        print(f"Error: inventory file '{args.inventory}' not found.", file=sys.stderr)
        print("Run extract_gh_commands.py first.", file=sys.stderr)
        sys.exit(2)

    with open(args.inventory) as fh:
        inventory = json.load(fh)

    # Fetch releases
    releases = fetch_releases(count=args.releases)
    if not releases:
        print("Warning: could not fetch cli/cli releases.", file=sys.stderr)

    # Check compatibility
    alerts = check_compatibility(inventory, releases)

    # Report
    gh_version = get_gh_version()
    report = generate_report(inventory, releases, alerts, gh_version)
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(report)
        print(f"Report written to {args.output}")
    else:
        print(report)

    # Write alerts to GITHUB_OUTPUT if running in Actions
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as fh:
            fh.write(f"has_alerts={'true' if alerts else 'false'}\n")
            fh.write(f"alert_count={len(alerts)}\n")

    if alerts:
        sys.exit(1)


if __name__ == "__main__":
    main()
