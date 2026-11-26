"""Generic privacy hygiene checks across tracked text files in this repository.

Guards against accidentally committing local user paths, non-placeholder email
addresses, environment files/secrets, or local workspace path markers.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEXT_EXTENSIONS = frozenset({".html", ".js", ".css", ".md", ".py", ".yaml", ".json", ".sql", ".txt"})
SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", "out", ".venv", "venv"})
ALLOW_FILES = frozenset({"tests/test_no_private_traces.py", "tools/privacy_guard.py"})

RE_WIN_USER_PATH = re.compile(r"C:(?:\\|/)Users(?:\\|/)", re.IGNORECASE)
RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
RE_SECRET_ASSIGN = re.compile(
    r"""(?:^|[\s;])(?:API_KEY|CLIENT_SECRET|ACCESS_TOKEN|PASSWORD|SMTP_PASS)\s*=\s*["']?[^\s"']{6,}""",
    re.IGNORECASE,
)
RE_PRIVATE_MARKERS = re.compile(r"(?:CODES_Workplace|\.agents\b)")


def _tracked_files() -> list[Path]:
    return [
        p
        for p in sorted(REPO_ROOT.rglob("*"))
        if p.is_file() and not any(part in SKIP_DIRS for part in p.parts)
    ]


def test_no_env_files_in_repo() -> None:
    env_files = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in _tracked_files()
        if p.name == ".env" or p.name.startswith(".env.")
    ]
    assert not env_files, f"Found .env file(s) in repository: {env_files}"


def test_no_private_traces_in_text_files() -> None:
    hits: list[str] = []
    for path in _tracked_files():
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in ALLOW_FILES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if RE_WIN_USER_PATH.search(line):
                hits.append(f"{rel}:{line_no} [windows_user_path]")
            for match in RE_EMAIL.finditer(line):
                domain = match.group(1).lower()
                if domain != "example.invalid":
                    hits.append(f"{rel}:{line_no} [non_placeholder_email] -> {match.group(0)!r}")
            if RE_SECRET_ASSIGN.search(line):
                hits.append(f"{rel}:{line_no} [secret_assignment]")
            m_priv = RE_PRIVATE_MARKERS.search(line)
            if m_priv:
                hits.append(f"{rel}:{line_no} [workspace_marker] -> {m_priv.group(0)!r}")
    assert not hits, "Private traces found:\n" + "\n".join(hits[:40])
