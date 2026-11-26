"""Standalone CI privacy and repository hygiene scanner (GitGuard + banned-trace check).

Verifies that no tracked text file or repository artifact contains:
  - uncommitted environment files (.env / .env.* except .env.example)
  - local Windows/macOS user home paths
  - non-placeholder email addresses (anything other than @example.invalid)
  - hardcoded credential or token assignments
  - private operational workspace markers or tracked SQLite database files
  - oversized blobs above the size threshold

Usage:
    python tools/privacy_guard.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_TRACKED_FILE_BYTES = 5 * 1024 * 1024
TEXT_EXTENSIONS = frozenset(
    {".html", ".js", ".css", ".md", ".py", ".yaml", ".yml", ".json", ".sql", ".txt", ".sh", ".toml"}
)
SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", "out", ".venv", "venv"})
SELF_EXEMPT = frozenset({"tests/test_no_private_traces.py", "tools/privacy_guard.py"})

RE_WIN_HOME = re.compile(r"[A-Za-z]:(?:\\|/)Users(?:\\|/)", re.IGNORECASE)
RE_MAC_HOME = re.compile(r"/Users/[A-Za-z0-9._-]+/")
RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
RE_SECRET = re.compile(
    r"""(?:^|[\s;])(?:API_KEY|CLIENT_SECRET|ACCESS_TOKEN|PASSWORD|SMTP_PASS|PRIVATE_KEY)\s*=\s*["']?[^\s"']{6,}""",
    re.IGNORECASE,
)
RE_PRIVATE_MARKER = re.compile(r"(?:CODES_Workplace|\.agents\b)")


def _candidate_files(repo_root: Path) -> list[Path]:
    """Return tracked git files if inside a git repo, else walk non-skipped files."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        paths = []
        for rel in proc.stdout.splitlines():
            rel = rel.strip()
            if not rel:
                continue
            p = repo_root / rel
            if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(repo_root).parts):
                paths.append(p)
        if paths:
            return sorted(paths)
    except Exception:
        pass
    return [
        p
        for p in sorted(repo_root.rglob("*"))
        if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(repo_root).parts)
    ]


def scan_repository(repo_root: Path = REPO_ROOT) -> list[str]:
    """Scan repository files and return a list of violation strings (empty = PASS)."""
    violations: list[str] = []
    for path in _candidate_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        name = path.name

        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            violations.append(f"{rel}: [env_file] Environment file must not be tracked")
            continue

        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            violations.append(f"{rel}: [tracked_db] SQLite database must not be tracked")
            continue

        size = path.stat().st_size
        if size > MAX_TRACKED_FILE_BYTES:
            violations.append(f"{rel}: [oversized_file] {size:,} bytes > {MAX_TRACKED_FILE_BYTES:,}")

        if rel in SELF_EXEMPT:
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS and name != "Makefile":
            continue

        text = path.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if RE_WIN_HOME.search(line):
                violations.append(f"{rel}:{line_no} [windows_user_path]")
            if RE_MAC_HOME.search(line):
                violations.append(f"{rel}:{line_no} [macos_user_path]")
            for m in RE_EMAIL.finditer(line):
                domain = m.group(1).lower()
                if domain != "example.invalid":
                    violations.append(f"{rel}:{line_no} [non_placeholder_email] {m.group(0)!r}")
            if RE_SECRET.search(line):
                violations.append(f"{rel}:{line_no} [secret_assignment]")
            m_priv = RE_PRIVATE_MARKER.search(line)
            if m_priv:
                violations.append(f"{rel}:{line_no} [private_workspace_marker] {m_priv.group(0)!r}")

    return violations


def main() -> int:
    violations = scan_repository(REPO_ROOT)
    if violations:
        print(f"PRIVACY_GUARD FAIL: {len(violations)} violation(s) found:", file=sys.stderr)
        for v in violations[:50]:
            print(f"  - {v}", file=sys.stderr)
        return 1
    print(f"PRIVACY_GUARD PASS: 0 violations across {len(_candidate_files(REPO_ROOT))} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
