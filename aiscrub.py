#!/usr/bin/env python3
"""aiscrub: find and (re)move AI-attribution lines in a git repo.

Subcommands:
  scan   Read-only report of attributions in commit history and working tree.
  scrub  Rewrite history and tracked files to REMOVE AI attributions
         (Claude, Copilot, Cursor, ChatGPT, Aider, Cody, Codeium, Devin,
         Gemini, Tabnine, JetBrains AI, Continue, generic "by AI"...).
         Pass --kill-all-humans to instead leave ONLY AI attribution:
         replace author/committer with the AI identity, drop EVERY
         Co-Authored-By trailer (human or AI), and append the canonical
         "Authored-By: Claude" trailer to every commit.
         Dry-run by default; pass --not-dry-run to actually mutate.
  dirty  Rewrite history to ADD an attribution trailer to every commit
         that does not already have it. Default trailer attributes Claude;
         override with --attribution / --attribution-file.
         Dry-run by default; pass --not-dry-run to actually mutate.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

VERSION = "0.1.0"


# Forgiving character classes: tolerate unicode look-alikes and stray spacing
# so cosmetic variation cannot smuggle an attribution past the scanner.
_DASH = r"[-‐‑‒–—−]"   # - ‐ ‑ ‒ – — −
_COLON = r"[:：]"                                  # : ：
_AT = r"[@＠]"                                     # @ ＠
_LBRACK = r"[\[\(【［]?"                       # optional [ ( 【 ［
_RBRACK = r"[\]\)】］]?"                       # optional ] ) 】 ］
_WS = r"[ \t ​]*"                           # space, tab, NBSP, ZWSP
_BOT = r"(?:🤖|:robot:|:robot_face:)"

AI_AUTHOR_NAME = "Claude"
AI_AUTHOR_EMAIL = "noreply@anthropic.com"

COAUTHOR_PATTERN = re.compile(
    rf"^{_WS}Co{_DASH}Authored{_DASH}By{_COLON}.*$",
    re.IGNORECASE,
)

CLAUDE_PATTERNS = [
    # Co-authored-by trailer naming Claude.
    re.compile(
        rf"^{_WS}Co{_DASH}Authored{_DASH}By{_COLON}{_WS}.*\bClaude\b.*$",
        re.IGNORECASE,
    ),
    # Co-authored-by trailer with an @anthropic.com address (any name).
    re.compile(
        rf"^{_WS}Co{_DASH}Authored{_DASH}By{_COLON}.*{_AT}anthropic\.com>?{_WS}$",
        re.IGNORECASE,
    ),
    # "Generated with / by / using Claude [Code]" and similar phrasings.
    re.compile(
        r"^.*\b(?:generated|made|created|written|authored|produced)\b"
        r"\s+(?:with|by|using)\b.*\bClaude(?:\s+Code)?\b.*$",
        re.IGNORECASE,
    ),
    # Bracketed or linkified "Claude Code" reference.
    re.compile(
        rf"^.*{_LBRACK}\s*Claude\s+Code\s*{_RBRACK}.*$",
        re.IGNORECASE,
    ),
    # URLs that uniquely identify Claude Code / claude.ai code surfaces.
    re.compile(
        rf"^.*\b(?:https?://)?(?:www\.)?claude\.(?:com|ai)/claude{_DASH}code\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^.*\b(?:https?://)?(?:www\.)?claude\.ai/code\b.*$",
        re.IGNORECASE,
    ),
    # Any line led by a 🤖 emoji (or :robot: shortcode) that mentions Claude.
    re.compile(rf"^{_WS}{_BOT}.*\bClaude\b.*$", re.IGNORECASE),
    # Email-shaped attribution: <something-claude-or-anthropic@noreply-ish>.
    re.compile(
        rf"^.*<[^>]*(?:claude|anthropic)[^>]*{_AT}"
        r"(?:users\.noreply\.github\.com|anthropic\.com|noreply\.[a-z.]+)>.*$",
        re.IGNORECASE,
    ),
]

# Other AI coding-assistant attributions seen in the wild.
OTHER_AI_PATTERNS = [
    # GitHub Copilot — bot account names and Copilot-specific phrasings.
    re.compile(
        rf"^{_WS}Co{_DASH}Authored{_DASH}By{_COLON}.*\bCopilot\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^{_WS}Co{_DASH}Authored{_DASH}By{_COLON}.*\b(?:copilot{_DASH}swe{_DASH}agent|github{_DASH}copilot)\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^.*\b(?:generated|made|created|written|authored|produced)\b"
        r"\s+(?:with|by|using)\b.*\bGitHub\s+Copilot\b.*$",
        re.IGNORECASE,
    ),

    # Cursor.
    re.compile(
        r"^.*\b(?:generated|made|created|written|authored|produced)\b"
        r"\s+(?:with|by|using)\b.*\bCursor(?:\s+(?:AI|Editor))?\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^{_WS}Co{_DASH}Authored{_DASH}By{_COLON}.*{_AT}cursor\.(?:sh|com|so)>?.*$",
        re.IGNORECASE,
    ),

    # OpenAI / ChatGPT / Codex.
    re.compile(
        r"^.*\b(?:generated|made|created|written|authored|produced)\b"
        r"\s+(?:with|by|using)\b.*\b(?:ChatGPT|OpenAI|GPT-?\d+|Codex)\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^{_WS}Co{_DASH}Authored{_DASH}By{_COLON}.*{_AT}openai\.com>?.*$",
        re.IGNORECASE,
    ),

    # Aider (terminal coding agent).
    re.compile(
        rf"^{_WS}Co{_DASH}Authored{_DASH}By{_COLON}.*\baider\b.*$",
        re.IGNORECASE,
    ),
    re.compile(r"^.*\baider(?:\.chat)?\b.*\bcommit\b.*$", re.IGNORECASE),

    # Sourcegraph Cody.
    re.compile(
        r"^.*\b(?:generated|made|created|written|authored|produced)\b"
        r"\s+(?:with|by|using)\b.*\b(?:Sourcegraph\s+)?Cody\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^{_WS}Co{_DASH}Authored{_DASH}By{_COLON}.*{_AT}sourcegraph\.com>?.*$",
        re.IGNORECASE,
    ),

    # Codeium / Windsurf.
    re.compile(
        r"^.*\b(?:generated|made|created|written|authored|produced)\b"
        r"\s+(?:with|by|using)\b.*\b(?:Codeium|Windsurf)\b.*$",
        re.IGNORECASE,
    ),

    # Devin (Cognition).
    re.compile(
        r"^.*\b(?:generated|made|created|written|authored|produced)\b"
        r"\s+(?:with|by|using)\b.*\bDevin\b.*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^{_WS}Co{_DASH}Authored{_DASH}By{_COLON}.*{_AT}cognition(?:labs)?\.ai>?.*$",
        re.IGNORECASE,
    ),

    # Google Gemini / Jules.
    re.compile(
        r"^.*\b(?:generated|made|created|written|authored|produced)\b"
        r"\s+(?:with|by|using)\b.*\b(?:Gemini(?:\s+Code\s+Assist)?|Google\s+Jules)\b.*$",
        re.IGNORECASE,
    ),

    # Tabnine, Continue.dev, JetBrains AI Assistant.
    re.compile(
        r"^.*\b(?:generated|made|created|written|authored|produced)\b"
        r"\s+(?:with|by|using)\b.*\b(?:Tabnine|Continue\.dev|JetBrains\s+AI(?:\s+Assistant)?)\b.*$",
        re.IGNORECASE,
    ),

    # Generic catch-all: "Generated by AI" / "Written by an AI assistant".
    re.compile(
        r"^.*\b(?:generated|made|created|written|authored|produced)\b"
        r"\s+(?:with|by|using)\s+(?:an?\s+)?(?:AI|LLM|large\s+language\s+model)"
        r"(?:\s+(?:assistant|agent|tool|coding\s+assistant))?\b.*$",
        re.IGNORECASE,
    ),
]

ALL_AI_PATTERNS = CLAUDE_PATTERNS + OTHER_AI_PATTERNS

# Default scan/scrub matchers: every known AI attribution.
COMMIT_LINE_PATTERNS = ALL_AI_PATTERNS
FILE_LINE_PATTERNS = ALL_AI_PATTERNS

BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".zip", ".gz", ".tar", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a",
    ".mp3", ".mp4", ".mov", ".avi", ".wav",
    ".ttf", ".otf", ".woff", ".woff2",
    ".class", ".jar", ".pyc",
}


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kwargs)


def run_ok(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def ensure_git_repo() -> Path:
    r = run_ok(["git", "rev-parse", "--show-toplevel"])
    if r.returncode != 0:
        sys.exit("error: not inside a git repository")
    return Path(r.stdout.strip())


def line_matches(line: str) -> bool:
    return any(p.search(line) for p in COMMIT_LINE_PATTERNS)


def line_is_human_coauthor(line: str) -> bool:
    if not COAUTHOR_PATTERN.match(line):
        return False
    return not any(p.search(line) for p in ALL_AI_PATTERNS)


def scan_commits() -> list[tuple[str, str, list[str]]]:
    """Returns [(sha, subject, [matched lines]), ...] for commits with hits."""
    sep = "<<<COMMIT-BOUNDARY>>>"
    fmt = f"%H%n%s%n%B%n{sep}"
    r = run(["git", "log", "--all", f"--pretty=format:{fmt}"])
    hits = []
    for chunk in r.stdout.split(sep + "\n"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        lines = chunk.split("\n")
        if len(lines) < 2:
            continue
        sha = lines[0]
        subject = lines[1]
        body_lines = lines[2:]
        matched = [ln for ln in body_lines if line_matches(ln)]
        if matched:
            hits.append((sha, subject, matched))
    return hits


def list_tracked_files() -> list[Path]:
    r = run(["git", "ls-files"])
    files = []
    for line in r.stdout.splitlines():
        p = Path(line)
        if p.suffix.lower() in BINARY_EXTS:
            continue
        files.append(p)
    return files


def scan_working_tree(root: Path) -> list[tuple[Path, list[tuple[int, str]]]]:
    hits = []
    for rel in list_tracked_files():
        full = root / rel
        try:
            text = full.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        matches = []
        for i, ln in enumerate(text.splitlines(), start=1):
            if line_matches(ln):
                matches.append((i, ln))
        if matches:
            hits.append((rel, matches))
    return hits


def cmd_scan(args: argparse.Namespace) -> int:
    root = ensure_git_repo()
    commit_hits = scan_commits()
    file_hits = scan_working_tree(root)

    print(f"=== commit messages ({len(commit_hits)} commits with attributions) ===")
    for sha, subject, matched in commit_hits:
        print(f"\n{sha[:12]} {subject}")
        for ln in matched:
            print(f"    | {ln}")

    print(f"\n=== tracked files ({len(file_hits)} files with attributions) ===")
    for rel, matches in file_hits:
        print(f"\n{rel}")
        for lineno, ln in matches:
            print(f"  {lineno}: {ln}")

    total = len(commit_hits) + len(file_hits)
    print(f"\nsummary: {len(commit_hits)} commits, {len(file_hits)} files")
    return 0 if total == 0 else 1


def strip_attribution_lines(text: str) -> str:
    out_lines = []
    for ln in text.splitlines(keepends=False):
        if line_matches(ln):
            continue
        out_lines.append(ln)
    while out_lines and out_lines[-1].strip() == "":
        out_lines.pop()
    result = "\n".join(out_lines)
    if text.endswith("\n"):
        result += "\n"
    return result


def clean_working_tree(root: Path) -> list[Path]:
    changed = []
    for rel in list_tracked_files():
        full = root / rel
        try:
            text = full.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        new_text = strip_attribution_lines(text)
        if new_text != text:
            full.write_text(new_text, encoding="utf-8")
            changed.append(rel)
    return changed


def have_filter_repo() -> bool:
    return shutil.which("git-filter-repo") is not None or _filter_repo_subcmd_ok()


def _filter_repo_subcmd_ok() -> bool:
    r = run_ok(["git", "filter-repo", "--help"])
    return r.returncode == 0


def _patterns_as_bytes_literal() -> str:
    """Serialize COMMIT_LINE_PATTERNS source as a list of compiled byte regexes
    so the rewrite subprocesses match exactly what `scan` matched."""
    parts = []
    for p in COMMIT_LINE_PATTERNS:
        src_bytes = p.pattern.encode("utf-8")
        parts.append(
            f"re.compile({repr(src_bytes)}, re.IGNORECASE | re.MULTILINE)"
        )
    return "[\n    " + ",\n    ".join(parts) + ",\n]"


def rewrite_history_filter_repo() -> None:
    """Use git-filter-repo to strip attribution lines from every commit message."""
    patterns_literal = _patterns_as_bytes_literal()
    script = (
        "import re\n"
        f"PATTERNS = {patterns_literal}\n"
        "msg = commit.message\n"
        "for p in PATTERNS:\n"
        '    msg = p.sub(b"", msg)\n'
        'msg = re.sub(rb"\\n{3,}", b"\\n\\n", msg)\n'
        'commit.message = msg.strip(b"\\n") + b"\\n"\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(script)
        callback_path = f.name
    try:
        cmd = [
            "git", "filter-repo", "--force",
            "--commit-callback", Path(callback_path).read_text(encoding="utf-8"),
        ]
        r = subprocess.run(cmd, text=True)
        if r.returncode != 0:
            sys.exit("error: git filter-repo failed")
    finally:
        os.unlink(callback_path)


DEFAULT_ATTRIBUTION = (
    "🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
    "\n"
    "Co-Authored-By: Claude <noreply@anthropic.com>"
)

KILL_ATTRIBUTION = (
    "🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
    "\n"
    "Authored-By: Claude <noreply@anthropic.com>"
)

KILL_SIGNATURE = "Authored-By: Claude <noreply@anthropic.com>"
KILL_TRAILER_RE = re.compile(r"^\s*Authored-By:\s*Claude\s*<", re.IGNORECASE)


def attribution_signature(attribution: str) -> str:
    """Pick a stable identifying line from the attribution block — used as a
    substring check for idempotency. Prefer the longest non-blank line."""
    candidates = [ln.strip() for ln in attribution.splitlines() if ln.strip()]
    if not candidates:
        return attribution.strip()
    return max(candidates, key=len)


def add_attribution_filter_repo(attribution: str) -> None:
    """Append `attribution` to every commit message that does not already
    contain its signature line."""
    signature = attribution_signature(attribution)
    script = (
        "attribution = " + repr(attribution.encode("utf-8")) + "\n"
        "signature = " + repr(signature.encode("utf-8")) + "\n"
        "msg = commit.message\n"
        "if signature not in msg:\n"
        '    body = msg.rstrip(b"\\n")\n'
        '    msg = body + b"\\n\\n" + attribution + b"\\n"\n'
        "commit.message = msg\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(script)
        callback_path = f.name
    try:
        cmd = [
            "git", "filter-repo", "--force",
            "--commit-callback", Path(callback_path).read_text(encoding="utf-8"),
        ]
        r = subprocess.run(cmd, text=True)
        if r.returncode != 0:
            sys.exit("error: git filter-repo failed")
    finally:
        os.unlink(callback_path)


def add_attribution_filter_branch(attribution: str) -> None:
    """Fallback path using `git filter-branch --msg-filter`."""
    signature = attribution_signature(attribution)
    helper_src = (
        "import sys\n"
        "attribution = " + repr(attribution.encode("utf-8")) + "\n"
        "signature = " + repr(signature.encode("utf-8")) + "\n"
        "data = sys.stdin.buffer.read()\n"
        "if signature not in data:\n"
        '    body = data.rstrip(b"\\n")\n'
        '    data = body + b"\\n\\n" + attribution + b"\\n"\n'
        "sys.stdout.buffer.write(data)\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(helper_src)
        helper_path = f.name
    msg_filter = f'"{sys.executable}" "{helper_path}"'
    env = dict(os.environ)
    env["FILTER_BRANCH_SQUELCH_WARNING"] = "1"
    try:
        r = subprocess.run(
            ["git", "filter-branch", "-f", "--msg-filter", msg_filter, "--", "--all"],
            env=env,
        )
        if r.returncode != 0:
            sys.exit("error: git filter-branch failed")
    finally:
        os.unlink(helper_path)


def rewrite_history_filter_branch() -> None:
    """Fallback: filter-branch using a Python helper as --msg-filter."""
    patterns_literal = _patterns_as_bytes_literal()
    helper_src = (
        "import sys, re\n"
        f"PATTERNS = {patterns_literal}\n"
        "data = sys.stdin.buffer.read()\n"
        "for p in PATTERNS:\n"
        '    data = p.sub(b"", data)\n'
        'data = re.sub(rb"\\n{3,}", b"\\n\\n", data)\n'
        'sys.stdout.buffer.write(data.strip(b"\\n") + b"\\n")\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(helper_src)
        helper_path = f.name
    msg_filter = f'"{sys.executable}" "{helper_path}"'
    env = dict(os.environ)
    env["FILTER_BRANCH_SQUELCH_WARNING"] = "1"
    try:
        r = subprocess.run(
            ["git", "filter-branch", "-f", "--msg-filter", msg_filter, "--", "--all"],
            env=env,
        )
        if r.returncode != 0:
            sys.exit("error: git filter-branch failed")
    finally:
        os.unlink(helper_path)


def working_tree_dirty() -> bool:
    r = run(["git", "status", "--porcelain"])
    return bool(r.stdout.strip())


def current_branch() -> str:
    r = run_ok(["git", "symbolic-ref", "--short", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else "HEAD"


WARNING_BANNER = """
============================================================
  DESTRUCTIVE OPERATION — REWRITES GIT HISTORY
============================================================
This will:
  * Rewrite EVERY commit on EVERY branch and tag to strip
    Claude attribution lines from commit messages.
  * Rewrite tracked files in the working tree to remove
    attribution lines, then commit the result.
  * Change commit SHAs across the entire repo.

Consequences:
  * Anyone with an existing clone will have a divergent
    history. They must reclone or hard-reset.
  * Open pull requests based on old SHAs will break.
  * Tags pointing at old commits will move.
  * Signed commits lose their signatures.

A backup of refs is saved under refs/original/ (filter-branch)
or in the .git/filter-repo/ directory (filter-repo).
============================================================
"""


PUSH_INSTRUCTIONS = """
============================================================
  NEXT STEP — FORCE PUSH
============================================================
History has been rewritten LOCALLY. To publish:

  git push --force-with-lease --all origin
  git push --force-with-lease --tags origin

GitHub branch protection — TEMPORARILY ALLOW FORCE PUSH
--------------------------------------------------------
If the target branch (e.g. main) is protected, GitHub will
reject the force push. Allow it for yourself only, push,
then revert. Use ONE of the two flows below.

Option A — Web UI (classic branch protection rule):
  1. github.com/<owner>/<repo>/settings/branches
  2. Edit the rule for the branch (e.g. "main").
  3. Under "Rules applied to everyone including administrators":
       - Uncheck "Do not allow bypassing the above settings"
         (so admins can bypass), OR
       - Check "Allow force pushes" -> "Specify who can
         force push" -> add YOUR username only.
  4. Save.
  5. Run the git push commands above.
  6. RETURN to the same settings page and revert:
       - Re-check "Do not allow bypassing..." or
       - Uncheck "Allow force pushes" / remove your username.

Option B — Web UI (rulesets, newer model):
  1. github.com/<owner>/<repo>/settings/rules
  2. Edit the active ruleset for the branch.
  3. Under "Bypass list", add your user with "Always" bypass.
  4. Save. Push. Then REMOVE yourself from the bypass list.

Option C — gh CLI (classic protection):
  # snapshot current protection
  gh api repos/:owner/:repo/branches/<branch>/protection > /tmp/bp.json
  # allow force push for your user only
  gh api -X PUT repos/:owner/:repo/branches/<branch>/protection/allow_force_pushes \\
    -F enabled=true
  # ... do the push ...
  # restore
  gh api -X DELETE repos/:owner/:repo/branches/<branch>/protection/allow_force_pushes

After pushing, TELL COLLABORATORS to:
  git fetch origin
  git reset --hard origin/<branch>
or reclone. Their old local branches will diverge.
============================================================
"""


def preview_working_tree(root: Path) -> list[Path]:
    """Like clean_working_tree, but reports without writing."""
    changed = []
    for rel in list_tracked_files():
        full = root / rel
        try:
            text = full.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        new_text = strip_attribution_lines(text)
        if new_text != text:
            changed.append(rel)
    return changed


def cmd_scrub(args: argparse.Namespace) -> int:
    if args.kill_all_humans:
        return cmd_kill_all_humans(args)

    root = ensure_git_repo()
    os.chdir(root)

    dry_run = not args.not_dry_run

    if dry_run:
        print("DRY RUN — no changes will be written. Pass --not-dry-run to apply.\n")
        commit_hits = scan_commits()
        file_hits = preview_working_tree(root)

        print(f"would rewrite {len(commit_hits)} commit message(s):")
        for sha, subject, matched in commit_hits:
            print(f"  {sha[:12]} {subject}")
            for ln in matched:
                print(f"      - {ln}")

        print(f"\nwould modify {len(file_hits)} tracked file(s):")
        for rel in file_hits:
            print(f"  {rel}")

        print("\nRe-run with --not-dry-run to apply.")
        return 0

    if working_tree_dirty():
        sys.exit("error: working tree has uncommitted changes; commit or stash first")

    print(WARNING_BANNER)
    if not args.yes:
        ans = input("Type 'REWRITE' to proceed: ").strip()
        if ans != "REWRITE":
            print("aborted")
            return 1

    branch = current_branch()
    print(f"\n[1/3] scrubbing tracked files on branch {branch}...")
    changed = clean_working_tree(root)
    if changed:
        print(f"  modified {len(changed)} file(s):")
        for p in changed:
            print(f"    {p}")
        run(["git", "add", "--all"])
        run(["git", "commit", "-m", "Remove Claude attributions from tracked files"])
    else:
        print("  no file changes needed")

    print("\n[2/3] rewriting commit messages across all refs...")
    if have_filter_repo():
        print("  using git-filter-repo")
        rewrite_history_filter_repo()
    else:
        print("  git-filter-repo not found; falling back to git filter-branch")
        print("  (install git-filter-repo for a faster, safer rewrite)")
        rewrite_history_filter_branch()

    print("\n[3/3] done")
    print(PUSH_INSTRUCTIONS)
    return 0


DIRTY_WARNING_BANNER = """
============================================================
  DESTRUCTIVE OPERATION — REWRITES GIT HISTORY
============================================================
This will:
  * Append a Claude attribution to EVERY commit on EVERY ref
    that does not already have one.
  * Change commit SHAs across the entire repo.

Same consequences as `scrub`: divergent clones, broken open
PRs, moved tags, lost signatures.
============================================================
"""


def resolve_attribution(args: argparse.Namespace) -> str:
    if args.attribution_file:
        path = Path(args.attribution_file)
        if not path.is_file():
            sys.exit(f"error: --attribution-file not found: {path}")
        return path.read_text(encoding="utf-8").rstrip("\n")
    if args.attribution:
        return args.attribution.rstrip("\n")
    return DEFAULT_ATTRIBUTION


def cmd_dirty(args: argparse.Namespace) -> int:
    root = ensure_git_repo()
    os.chdir(root)

    attribution = resolve_attribution(args)
    signature = attribution_signature(attribution)
    dry_run = not args.not_dry_run

    r = run(["git", "log", "--all", "--pretty=format:%H%x09%s%x00%B%x00"])
    raw = r.stdout
    all_commits: list[tuple[str, str, str]] = []
    needing: list[tuple[str, str]] = []
    for record in raw.split("\x00\n"):
        if not record.strip():
            continue
        try:
            head, body = record.split("\x00", 1)
            sha, subject = head.split("\t", 1)
        except ValueError:
            continue
        all_commits.append((sha, subject, body))
        if signature not in body:
            needing.append((sha, subject))

    if dry_run:
        print("DRY RUN — no changes will be written. Pass --not-dry-run to apply.\n")
        print(f"attribution to be appended (signature line: {signature!r}):")
        for ln in attribution.splitlines():
            print(f"    {ln}")
        print(f"\nwould attribute {len(needing)} commit(s) "
              f"(of {len(all_commits)} total):")
        for sha, subj in needing[:50]:
            print(f"  {sha[:12]} {subj}")
        if len(needing) > 50:
            print(f"  ... and {len(needing) - 50} more")
        print(f"\n{len(all_commits) - len(needing)} commit(s) already contain "
              f"this attribution (skipped).")
        print("\nRe-run with --not-dry-run to apply.")
        return 0

    if not needing:
        print("nothing to do: every commit already contains this attribution.")
        return 0

    if working_tree_dirty():
        sys.exit("error: working tree has uncommitted changes; commit or stash first")

    print(DIRTY_WARNING_BANNER)
    if not args.yes:
        ans = input("Type 'REWRITE' to proceed: ").strip()
        if ans != "REWRITE":
            print("aborted")
            return 1

    print(f"\nattributing {len(needing)} commit(s) across all refs...")
    if have_filter_repo():
        print("  using git-filter-repo")
        add_attribution_filter_repo(attribution)
    else:
        print("  git-filter-repo not found; falling back to git filter-branch")
        print("  (install git-filter-repo for a faster, safer rewrite)")
        add_attribution_filter_branch(attribution)

    print("\ndone")
    print(PUSH_INSTRUCTIONS)
    return 0


KILL_WARNING_BANNER = """
============================================================
  DESTRUCTIVE OPERATION — REWRITES GIT HISTORY
============================================================
This will:
  * Replace author/committer with the AI identity
    (Claude <noreply@anthropic.com>) on EVERY commit.
  * Drop EVERY Co-Authored-By trailer (human or AI) — Claude
    is now THE author, not a co-author.
  * Append the canonical "Authored-By: Claude" trailer to
    every commit.
  * Change commit SHAs across the entire repo.

Same consequences as `scrub`: divergent clones, broken open
PRs, moved tags, lost signatures.
============================================================
"""


def kill_all_humans_filter_repo() -> None:
    patterns_literal = _patterns_as_bytes_literal()
    coauthor_src = repr(COAUTHOR_PATTERN.pattern.encode("utf-8"))
    kill_attribution_bytes = repr(KILL_ATTRIBUTION.encode("utf-8"))
    script = (
        "import re\n"
        f"PATTERNS = {patterns_literal}\n"
        f"COAUTHOR_RE = re.compile({coauthor_src}, re.IGNORECASE | re.MULTILINE)\n"
        f"AI_NAME = {AI_AUTHOR_NAME.encode('utf-8')!r}\n"
        f"AI_EMAIL = {AI_AUTHOR_EMAIL.encode('utf-8')!r}\n"
        f"KILL_ATTRIBUTION = {kill_attribution_bytes}\n"
        "msg = commit.message\n"
        "kept = []\n"
        "for line in msg.split(b'\\n'):\n"
        "    if COAUTHOR_RE.match(line) is not None:\n"
        "        continue\n"
        "    if any(p.search(line) for p in PATTERNS):\n"
        "        continue\n"
        "    kept.append(line)\n"
        "msg = b'\\n'.join(kept)\n"
        "msg = re.sub(rb'\\n{3,}', b'\\n\\n', msg)\n"
        "msg = msg.rstrip(b'\\n') + b'\\n\\n' + KILL_ATTRIBUTION + b'\\n'\n"
        "commit.message = msg\n"
        "commit.author_name = AI_NAME\n"
        "commit.author_email = AI_EMAIL\n"
        "commit.committer_name = AI_NAME\n"
        "commit.committer_email = AI_EMAIL\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(script)
        callback_path = f.name
    try:
        cmd = [
            "git", "filter-repo", "--force",
            "--commit-callback", Path(callback_path).read_text(encoding="utf-8"),
        ]
        r = subprocess.run(cmd, text=True)
        if r.returncode != 0:
            sys.exit("error: git filter-repo failed")
    finally:
        os.unlink(callback_path)


def kill_all_humans_filter_branch() -> None:
    patterns_literal = _patterns_as_bytes_literal()
    coauthor_src = repr(COAUTHOR_PATTERN.pattern.encode("utf-8"))
    kill_attribution_bytes = repr(KILL_ATTRIBUTION.encode("utf-8"))
    helper_src = (
        "import sys, re\n"
        f"PATTERNS = {patterns_literal}\n"
        f"COAUTHOR_RE = re.compile({coauthor_src}, re.IGNORECASE | re.MULTILINE)\n"
        f"KILL_ATTRIBUTION = {kill_attribution_bytes}\n"
        "data = sys.stdin.buffer.read()\n"
        "kept = []\n"
        "for line in data.split(b'\\n'):\n"
        "    if COAUTHOR_RE.match(line) is not None:\n"
        "        continue\n"
        "    if any(p.search(line) for p in PATTERNS):\n"
        "        continue\n"
        "    kept.append(line)\n"
        "data = b'\\n'.join(kept)\n"
        "data = re.sub(rb'\\n{3,}', b'\\n\\n', data)\n"
        "data = data.rstrip(b'\\n') + b'\\n\\n' + KILL_ATTRIBUTION + b'\\n'\n"
        "sys.stdout.buffer.write(data)\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(helper_src)
        helper_path = f.name
    msg_filter = f'"{sys.executable}" "{helper_path}"'
    env_filter = (
        f'GIT_AUTHOR_NAME="{AI_AUTHOR_NAME}"; '
        f'GIT_AUTHOR_EMAIL="{AI_AUTHOR_EMAIL}"; '
        f'GIT_COMMITTER_NAME="{AI_AUTHOR_NAME}"; '
        f'GIT_COMMITTER_EMAIL="{AI_AUTHOR_EMAIL}"; '
        "export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL"
    )
    env = dict(os.environ)
    env["FILTER_BRANCH_SQUELCH_WARNING"] = "1"
    try:
        r = subprocess.run(
            [
                "git", "filter-branch", "-f",
                "--env-filter", env_filter,
                "--msg-filter", msg_filter,
                "--", "--all",
            ],
            env=env,
        )
        if r.returncode != 0:
            sys.exit("error: git filter-branch failed")
    finally:
        os.unlink(helper_path)


def cmd_kill_all_humans(args: argparse.Namespace) -> int:
    root = ensure_git_repo()
    os.chdir(root)

    dry_run = not args.not_dry_run
    ai_author_str = f"{AI_AUTHOR_NAME} <{AI_AUTHOR_EMAIL}>"
    kill_signature = KILL_SIGNATURE

    sep = "<<<COMMIT-BOUNDARY>>>"
    fmt = f"%H%n%an <%ae>%n%s%n%B%n{sep}"
    r = run(["git", "log", "--all", f"--pretty=format:{fmt}"])

    affected = []
    for chunk in r.stdout.split(sep + "\n"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        lines = chunk.split("\n")
        if len(lines) < 3:
            continue
        sha = lines[0]
        author = lines[1]
        subject = lines[2]
        body_lines = lines[3:]

        coauthors = [ln for ln in body_lines if COAUTHOR_PATTERN.match(ln)]
        has_kill = any(KILL_TRAILER_RE.match(ln) for ln in body_lines)
        author_changes = author != ai_author_str

        if author_changes or coauthors or not has_kill:
            affected.append({
                "sha": sha,
                "author": author,
                "subject": subject,
                "coauthors": coauthors,
                "has_kill": has_kill,
                "author_changes": author_changes,
            })

    if dry_run:
        print("DRY RUN — no changes will be written. Pass --not-dry-run to apply.\n")
        print(f"would rewrite {len(affected)} commit(s) to leave only AI attribution:\n")
        for c in affected:
            print(f"  {c['sha'][:12]} {c['subject']}")
            if c['author_changes']:
                print(f"      author: {c['author']} -> {ai_author_str}")
            for ln in c['coauthors']:
                print(f"      drop:   {ln}")
            if not c['has_kill']:
                print(f"      add:    {kill_signature}")
        print("\nRe-run with --not-dry-run to apply.")
        return 0

    if not affected:
        print("nothing to do: every commit already has AI-only attribution.")
        return 0

    if working_tree_dirty():
        sys.exit("error: working tree has uncommitted changes; commit or stash first")

    print(KILL_WARNING_BANNER)
    if not args.yes:
        ans = input("Type 'REWRITE' to proceed: ").strip()
        if ans != "REWRITE":
            print("aborted")
            return 1

    print(f"\nrewriting {len(affected)} commit(s) to leave only AI attribution...")
    if have_filter_repo():
        print("  using git-filter-repo")
        kill_all_humans_filter_repo()
    else:
        print("  git-filter-repo not found; falling back to git filter-branch")
        print("  (install git-filter-repo for a faster, safer rewrite)")
        kill_all_humans_filter_branch()

    print("\ndone")
    print(PUSH_INSTRUCTIONS)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aiscrub",
        description="Find, remove, or add AI attributions in a git repo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  aiscrub scan\n"
            "      Report every commit and tracked file containing an attribution.\n"
            "\n"
            "  aiscrub scrub\n"
            "      Dry-run preview of what would be rewritten. Safe.\n"
            "\n"
            "  aiscrub scrub --not-dry-run\n"
            "      DESTRUCTIVE: rewrites history and tracked files. Prompts first.\n"
            "\n"
            "  aiscrub scrub --not-dry-run --yes\n"
            "      DESTRUCTIVE and unattended. No confirmation prompt.\n"
            "\n"
            "  aiscrub dirty\n"
            "      Dry-run preview of ADDING attributions to every commit.\n"
            "\n"
            "  aiscrub dirty --not-dry-run\n"
            "      DESTRUCTIVE: append a Claude attribution to every commit\n"
            "      that does not already have one.\n"
            "\n"
            "  aiscrub dirty --attribution-file ./trailer.txt\n"
            "      Use a custom attribution block from a file instead of\n"
            "      the default Claude trailer.\n"
            "\n"
            "  aiscrub scrub --kill-all-humans\n"
            "      Dry-run preview of replacing every human attribution\n"
            "      with the AI default.\n"
            "\n"
            "  aiscrub scrub --kill-all-humans --not-dry-run\n"
            "      DESTRUCTIVE: rewrite every commit to leave only AI\n"
            "      attribution (author, committer, trailers).\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    sub = parser.add_subparsers(
        dest="cmd",
        required=True,
        metavar="<command>",
        title="commands",
    )

    sp_scan = sub.add_parser(
        "scan",
        help="report attributions (read-only, never modifies the repo)",
        description=(
            "Read-only scan of every commit on every ref and every tracked file in "
            "the working tree. Prints what was found and exits non-zero if any "
            "attributions exist (useful in CI)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp_scan.set_defaults(func=cmd_scan)

    sp_scrub = sub.add_parser(
        "scrub",
        help="rewrite history and files (dry-run by default)",
        description=(
            "Remove Claude attributions from commit messages (across every ref) "
            "and from tracked files. Dry-run by default — pass --not-dry-run to "
            "actually mutate. Destructive: rewrites SHAs and breaks signatures."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp_scrub.add_argument(
        "--not-dry-run",
        action="store_true",
        help="actually rewrite history and files (DESTRUCTIVE; default is dry-run)",
    )
    sp_scrub.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the interactive 'REWRITE' confirmation prompt",
    )
    sp_scrub.add_argument(
        "--kill-all-humans",
        action="store_true",
        help=(
            "instead of removing AI attributions, leave ONLY the AI: "
            "replace author/committer with the AI identity, drop EVERY "
            "Co-Authored-By trailer, and append the canonical "
            "'Authored-By: Claude' trailer to every commit"
        ),
    )
    sp_scrub.set_defaults(func=cmd_scrub)

    sp_dirty = sub.add_parser(
        "dirty",
        help="ADD Claude attributions to every commit (dry-run by default)",
        description=(
            "Inverse of `scrub`. Appends a Claude attribution trailer to every "
            "commit message on every ref that does not already have one. "
            "Idempotent. Dry-run by default — pass --not-dry-run to mutate. "
            "Destructive: rewrites SHAs and breaks signatures."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp_dirty.add_argument(
        "--not-dry-run",
        action="store_true",
        help="actually rewrite history (DESTRUCTIVE; default is dry-run)",
    )
    sp_dirty.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the interactive 'REWRITE' confirmation prompt",
    )
    sp_dirty.add_argument(
        "--attribution",
        metavar="TEXT",
        default=None,
        help=(
            "the attribution block to append to each commit. May contain "
            "newlines. Defaults to the standard Claude Code trailer."
        ),
    )
    sp_dirty.add_argument(
        "--attribution-file",
        metavar="PATH",
        default=None,
        help="read the attribution block from a file (overrides --attribution)",
    )
    sp_dirty.set_defaults(func=cmd_dirty)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
