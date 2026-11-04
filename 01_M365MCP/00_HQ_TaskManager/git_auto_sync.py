#!/usr/bin/env python3
"""
ACME Manufacturing M365 Automation Platform - Git Auto-Sync & Security Guard Engine
===================================================================================
Automated Git synchronization & multi-layer pre-commit security guardrail:
1. Blocks corporate domains/emails, internal hostnames, passwords, API tokens, keys, and binary/DB dumps.
2. Dynamically cross-checks local uncommitted .env values/hosts/emails at commit time (zero secret echoing).
3. Provides pre-commit hook enforcement across operational and public repositories.
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]

# One-way SHA-256 digests of known internal identifiers (prevents plaintext leakage in scanner source)
_BLOCKED_TOKEN_SHA256: Set[str] = {
    "015edee9d149ae5fc63f6bfdbb2f840afcf1dc60b385b08d4c4b0cf8d1f23f56",
    "04ec07fe76eabd3ee8f0af427bb1fdf83ce0e4be3023d1730a2c65655ff2516c",
    "0b2872160b3a149e551087879532f0b98abc8b015aaa0d34c63027093bd96b70",
    "1a508dfec8bcfc8cc9cb6360faee2c82c4c816aaa2881ca94462e4fbeb894b54",
    "20926b97bfe2788497267756a051c1b6972f7dba362aa121f262ab7e36c6a096",
    "22ae4b8c757eacae59eb268bfbb8943aea2dfcdb356f14eee547c6cc1ac49148",
    "4755b96c2b4105da165c5f688e4ab76d7963295118201e42b7cbde5187264880",
    "676e6195087f2b3662fa5ebe054306670da943b3f0a53570f88fc514c3b12318"
}

# Generic & gitleaks-equivalent security regex patterns
SECURITY_RULES: List[Dict[str, Any]] = [
    {
        "name": "Local Windows User Path",
        "pattern": re.compile(r"C:" + r"\\Users\\(?!Public\b|Default\b|user\b)[A-Za-z0-9._-]+", re.IGNORECASE),
        "desc": "Local Windows user path detected (use %USERPROFILE% or Path.home())",
    },
    {
        "name": "Hardcoded Non-Dummy SAP Plant Code",
        "pattern": re.compile(r"\bP(?!999\b)[1-9]\d{2}\b"),
        "desc": "Non-dummy SAP plant code detected (use P001/P002/P999 or SAP_PLANT env var)",
    },
    {
        "name": "Real 8-Digit SAP Part Number (00xxxxxx)",
        "pattern": re.compile(r"\b00(?!000000)\d{6}\b"),
        "desc": "Real 8-digit SAP material number pattern detected (use 9900xxxx or MAT-xxxx)",
    },
    {
        "name": "Real Dashed Part Number (XX-XXXX-XXXXX)",
        "pattern": re.compile(r"\b\d{2}-\d{4,5}-\d{4,6}\b"),
        "desc": "Real dashed part number pattern detected (use TEST-xxxx)",
    },
    {
        "name": "Real SAP Material Document Number (499xxxxxxx)",
        "pattern": re.compile(r"\b499(?!0000\d{3})\d{7}\b"),
        "desc": "Real SAP material document number detected (use 4990000xxx)",
    },
    {
        "name": "Hardcoded Plaintext Password/Secret Assignment",
        "pattern": re.compile(r"""(?:password|pwd|secret|api_key|client_secret|access_token)\s*=\s*["'][^"'\$\{\}]{6,}["']""", re.IGNORECASE),
        "desc": "Hardcoded secret literal detected (use os.getenv)",
    },
    {
        "name": "GitHub / Cloud / API Token (gitleaks-equivalent)",
        "pattern": re.compile(
            r"(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|gho_[A-Za-z0-9]{30,}|ghs_[A-Za-z0-9]{30,}"
            r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z\-_]{35}|sk-[A-Za-z0-9\-_]{24,}|xox[baprs]-[0-9A-Za-z\-]{10,})"
        ),
        "desc": "API / OAuth / Cloud credential token detected",
    },
    {
        "name": "Private Key PEM Header",
        "pattern": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
        "desc": "Private key block detected",
    },
    {
        "name": "Webhook Secret URL",
        "pattern": re.compile(r"https://(?:hooks\.slack\.com/services/T\S+|\S+\.webhook\.office\.com/\S+)", re.IGNORECASE),
        "desc": "Incoming webhook secret URL detected",
    },
]

FORBIDDEN_EXTENSIONS: Set[str] = {
    ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm",
    ".xlsx", ".xls", ".xlsb", ".xlsm", ".csv", ".tsv", ".parquet",
    ".pdf", ".zip", ".7z", ".bundle", ".pem", ".key", ".pfx", ".p12",
}

SCAN_IGNORE_FILES: Set[str] = {
    "git_auto_sync.py",
    ".gitignore",
    ".git",
}

_SAFE_ENV_LITERALS: Set[str] = {
    "", "common", "device_code", "en", "ko", "100", "200", "300", "true", "false", "0", "1",
    "BINSYNTH9940", "smt", "smd", "oem", "p001", "p002", "acme", "ACME Manufacturing", "acme corp",
    "auto_agent", "wms_user", "your_wms_id", "your_wms_password", "your_sap_user", "your_sap_password",
    "enterprise sso", "https://wms.example.com/login", "https://wms.example.com/wms_web",
}

_SAFE_PASSWORD_TEST_VALUES: Set[str] = {
    "unused", "dummy_password", "test_password", "your_password", "placeholder", "changeme", "redacted",
}


def get_git_executable() -> str:
    """Returns working git command path."""
    local_git = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd" / "git.exe"
    if local_git.exists():
        return str(local_git)
    return "git"


def run_cmd(cmd: List[str], cwd: Path = WORKSPACE_ROOT) -> Tuple[int, str, str]:
    """Runs a system command using UTF-8 encoding."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def _load_dynamic_env_denylist(repo_root: Path) -> List[Tuple[str, str]]:
    """Loads sensitive values/hosts/emails from local .env files in memory ONLY (returns [(key, secret_str)])."""
    candidates = [
        repo_root / ".env",
        repo_root / "01_M365MCP" / ".env",
        WORKSPACE_ROOT / "01_M365MCP" / ".env",
    ]
    sensitive: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for ep in candidates:
        if not ep.is_file():
            continue
        try:
            lines = ep.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for raw in lines:
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if not v or v.lower() in _SAFE_ENV_LITERALS:
                continue
            # Full value if >= 4 chars
            if len(v) >= 4 and "example.com" not in v.lower():
                if v.lower() not in seen:
                    seen.add(v.lower())
                    sensitive.append((k, v))
            # Extract URL hostname
            if "://" in v:
                try:
                    host = (urlparse(v).hostname or "").strip().lower()
                    if host and "example.com" not in host and host not in ("localhost", "127.0.0.1") and host not in seen:
                        seen.add(host)
                        sensitive.append((f"{k}:host", host))
                except Exception:
                    pass
            # Extract comma/semicolon-separated emails, domains, or customer prefixes
            for part in re.split(r"[,;]", v):
                tok = part.strip()
                tl = tok.lower()
                if not tok or tl in _SAFE_ENV_LITERALS or "example.com" in tl:
                    continue
                if "@" in tok and len(tok) >= 5:
                    if tl not in seen:
                        seen.add(tl)
                        sensitive.append((f"{k}:email", tok))
                    dom = tl.split("@", 1)[1]
                    if dom and dom not in ("example.com", "test.local", "company.local", "corp.test") and dom not in seen:
                        seen.add(dom)
                        sensitive.append((f"{k}:domain", dom))
                elif ("PREFIX" in k or "SENDERS" in k) and len(tok) >= 3:
                    # Exclude generic role words in BRIEFING_SENDERS_*
                    if not re.match(r"^(supv|analyst|specialist|coordinator|eng\.|warehouse|prod\.|inv\.)", tl):
                        if tl not in seen:
                            seen.add(tl)
                            sensitive.append((f"{k}:token", tok))
    return sensitive


def _scan_text_lines(
    rel_path: str,
    lines: List[str],
    dyn_denylist: List[Tuple[str, str]],
) -> List[Dict[str, Any]]:
    violations: List[Dict[str, Any]] = []
    word_re = re.compile(r"[A-Za-z0-9._-]+")
    for idx, line in enumerate(lines, 1):
        if "SECURITY_RULES" in line or "dummy" in line.lower():
            continue
        # 1. One-way SHA-256 token digest check
        for w in word_re.findall(line):
            wl = w.lower()
            h = hashlib.sha256(wl.encode("utf-8")).hexdigest()
            if h in _BLOCKED_TOKEN_SHA256:
                violations.append({
                    "file": rel_path,
                    "line": idx,
                    "rule": "Blocked Internal Identifier (SHA-256 Match)",
                    "content": f"sha256={h[:12]} (len={len(w)})",
                    "desc": "Internal company/host/plant/employee identifier must use .env or dummy placeholder.",
                })
        # 2. Static & gitleaks regex rules
        for rule in SECURITY_RULES:
            match = rule["pattern"].search(line)
            if match:
                matched_txt = match.group(0)
                if rule["name"] == "Hardcoded Plaintext Password/Secret Assignment":
                    val_m = re.search(r"""=["']([^"']+)["']""", matched_txt)
                    if val_m and val_m.group(1).strip().lower() in _SAFE_PASSWORD_TEST_VALUES:
                        continue
                h_m = hashlib.sha256(matched_txt.encode("utf-8")).hexdigest()[:12]
                violations.append({
                    "file": rel_path,
                    "line": idx,
                    "rule": rule["name"],
                    "content": f"sha256={h_m} (len={len(matched_txt)})",
                    "desc": rule["desc"],
                })
        # 3. Dynamic local .env value check (never prints secret value)
        line_low = line.lower()
        for env_key, secret_val in dyn_denylist:
            sv_low = secret_val.lower()
            if len(sv_low) <= 4:
                pat = r"(?<![A-Za-z0-9])" + re.escape(sv_low) + r"(?![A-Za-z0-9])"
                hit = re.search(pat, line_low) is not None
            else:
                hit = sv_low in line_low
            if hit:
                h_s = hashlib.sha256(secret_val.encode("utf-8")).hexdigest()[:12]
                violations.append({
                    "file": rel_path,
                    "line": idx,
                    "rule": f"Dynamic .env Leak ({env_key})",
                    "content": f"sha256={h_s} (len={len(secret_val)})",
                    "desc": f"Staged/tracked text matches local .env value '{env_key}'. Mask before committing.",
                })
    return violations


def _check_forbidden_path(rel_path: str) -> Optional[Dict[str, Any]]:
    norm = rel_path.replace("\\", "/")
    name = Path(norm).name
    suffix = Path(norm).suffix.lower()
    if (name == ".env" or name.startswith(".env.") or name.endswith(".env")) and not name.endswith(".env.example"):
        return {
            "file": norm,
            "line": 0,
            "rule": "Forbidden .env File",
            "content": name,
            "desc": "Environment secret files (.env) must never be committed.",
        }
    if "/.pw_" in f"/{norm}" or "d107_screenshots/" in norm:
        return {
            "file": norm,
            "line": 0,
            "rule": "Forbidden Browser Session / Screenshot Artifact",
            "content": norm,
            "desc": "Playwright browser profiles and operational screenshots must never be committed.",
        }
    if norm.startswith(".agents/handoff/") and norm != ".agents/handoff/README.md":
        return {
            "file": norm,
            "line": 0,
            "rule": "Forbidden Handoff Operational File (Option A)",
            "content": norm,
            "desc": "Only .agents/handoff/README.md may be tracked in Git.",
        }
    if suffix in FORBIDDEN_EXTENSIONS:
        return {
            "file": norm,
            "line": 0,
            "rule": f"Forbidden Binary/Data File Extension ({suffix})",
            "content": suffix,
            "desc": "Databases, spreadsheets, PDFs, archives, and keys cannot be committed.",
        }
    if suffix in (".png", ".jpg", ".jpeg") and not norm.startswith("docs/screenshots/"):
        return {
            "file": norm,
            "line": 0,
            "rule": f"Unapproved Image Asset ({suffix})",
            "content": norm,
            "desc": "Image files outside docs/screenshots/ are blocked to prevent operational screenshot leaks.",
        }
    return None


def scan_file_security(file_path: Path, repo_root: Path = WORKSPACE_ROOT, dyn_denylist: Optional[List[Tuple[str, str]]] = None) -> List[Dict[str, Any]]:
    """Scans a single working-tree file for sensitive security patterns."""
    violations: List[Dict[str, Any]] = []
    try:
        rel_str = str(file_path.relative_to(repo_root)).replace("\\", "/")
    except ValueError:
        rel_str = file_path.name

    path_v = _check_forbidden_path(rel_str)
    if path_v:
        return [path_v]

    try:
        if file_path.stat().st_size > 10 * 1024 * 1024:
            return [{
                "file": rel_str,
                "line": 0,
                "rule": "File size exceeds 10MB limit",
                "content": f"Size: {file_path.stat().st_size / (1024*1024):.2f} MB",
                "desc": "Files larger than 10MB must be added to .gitignore.",
            }]
    except Exception:
        pass

    if file_path.name in SCAN_IGNORE_FILES or file_path.name.endswith(".pyc"):
        return violations

    if dyn_denylist is None:
        dyn_denylist = _load_dynamic_env_denylist(repo_root)

    try:
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        violations.extend(_scan_text_lines(rel_str, lines, dyn_denylist))
    except Exception as e:
        violations.append({
            "file": rel_str,
            "line": 0,
            "rule": "Read Error",
            "content": str(e),
            "desc": "Failed to read file",
        })
    return violations


def audit_staged_index(repo_root: Path = WORKSPACE_ROOT) -> Tuple[bool, List[Dict[str, Any]]]:
    """Scans git staged index (--cached) for pre-commit enforcement."""
    git_bin = get_git_executable()
    code, stdout, err = run_cmd([git_bin, "diff", "--cached", "--name-only", "--diff-filter=ACMR"], cwd=repo_root)
    if code != 0:
        print(f"[GitGuard] git diff --cached failed: {err or stdout}")
        return False, []
    staged_files = [ln.strip().strip('"') for ln in stdout.splitlines() if ln.strip()]
    if not staged_files:
        return True, []

    dyn_denylist = _load_dynamic_env_denylist(repo_root)
    all_violations: List[Dict[str, Any]] = []
    for rel in staged_files:
        norm = rel.replace("\\", "/")
        pv = _check_forbidden_path(norm)
        if pv:
            all_violations.append(pv)
            continue
        if Path(norm).name in SCAN_IGNORE_FILES or norm.endswith(".pyc"):
            continue
        proc = subprocess.run(
            [git_bin, "show", f":{norm}"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            continue
        blob_bytes = proc.stdout
        if len(blob_bytes) > 10 * 1024 * 1024:
            all_violations.append({
                "file": norm,
                "line": 0,
                "rule": "Staged blob exceeds 10MB limit",
                "content": f"Size: {len(blob_bytes)/(1024*1024):.2f} MB",
                "desc": "Files larger than 10MB cannot be committed.",
            })
            continue
        lines = blob_bytes.decode("utf-8", errors="ignore").splitlines()
        all_violations.extend(_scan_text_lines(norm, lines, dyn_denylist))

    return len(all_violations) == 0, all_violations


def audit_working_tree(repo_root: Path = WORKSPACE_ROOT) -> Tuple[bool, List[Dict[str, Any]]]:
    """Scans all modified/untracked/tracked files in working tree."""
    git_bin = get_git_executable()
    code, stdout, _ = run_cmd([git_bin, "status", "--porcelain"], cwd=repo_root)
    if code != 0:
        print("[GitGuard] Git status failed:", stdout)
        return False, []

    files_set: Set[Path] = set()
    for line in stdout.splitlines():
        if not line.strip():
            continue
        rel_path = line[3:].strip().strip('"')
        target_path = repo_root / rel_path
        if target_path.exists() and target_path.is_file():
            files_set.add(target_path)

    code_ls, stdout_ls, _ = run_cmd([git_bin, "ls-files"], cwd=repo_root)
    if code_ls == 0:
        for rel_path in stdout_ls.splitlines():
            rel_path = rel_path.strip().strip('"')
            if rel_path:
                target_path = repo_root / rel_path
                if target_path.exists() and target_path.is_file():
                    files_set.add(target_path)

    dyn_denylist = _load_dynamic_env_denylist(repo_root)
    all_violations: List[Dict[str, Any]] = []
    for f in sorted(files_set):
        v = scan_file_security(f, repo_root=repo_root, dyn_denylist=dyn_denylist)
        if v:
            all_violations.extend(v)
    return len(all_violations) == 0, all_violations


def install_pre_commit_hook(repo_root: Path = WORKSPACE_ROOT) -> bool:
    """Installs the pre-commit security hook into <repo_root>/.git/hooks/pre-commit."""
    git_bin = get_git_executable()
    code, git_common_dir, _ = run_cmd([git_bin, "rev-parse", "--git-common-dir"], cwd=repo_root)
    if code == 0 and git_common_dir:
        gdir = Path(git_common_dir)
        if not gdir.is_absolute():
            gdir = (repo_root / gdir).resolve()
    else:
        gdir = repo_root / ".git"

    hook_path = gdir / "hooks" / "pre-commit"
    scanner_posix = Path(__file__).resolve().as_posix()
    py_posix = Path(sys.executable).resolve().as_posix()
    hook_content = f"""#!/bin/sh
# GitGuard Pre-Commit Security Hook (241-D / 243)
REPO_ROOT="$(git rev-parse --show-toplevel)"
"{py_posix}" "{scanner_posix}" pre-commit --repo "$REPO_ROOT"
status=$?
if [ $status -ne 0 ]; then
    echo ""
    echo "[GitGuard BLOCKED] Sensitive pattern, .env value, or forbidden data file detected in staged changes."
    echo "Sanitize the flagged file/line or update .gitignore before committing."
    exit 1
fi
exit 0
"""
    try:
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        hook_path.write_bytes(hook_content.replace("\r\n", "\n").encode("utf-8"))
        try:
            os.chmod(hook_path, 0o755)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[GitGuard] Pre-commit hook install failed: {e}")
        return False


def sync_to_github(custom_message: Optional[str] = None) -> bool:
    """Performs security scan, stages clean files, commits, and pushes to remote."""
    git_bin = get_git_executable()
    print("\n[1/4] Running GitGuard security audit...")
    is_safe, violations = audit_working_tree(WORKSPACE_ROOT)
    if not is_safe:
        print("[GitGuard BLOCKED] Security violations detected:")
        for v in violations:
            print(f"  - [{v['rule']}] {v['file']}:{v['line']} -> {v['content']}")
        return False

    code, stdout, _ = run_cmd([git_bin, "status", "--porcelain"])
    if not stdout.strip():
        print("[2/4] Working tree clean. Nothing to sync.")
        return True

    code, _, err = run_cmd([git_bin, "add", "."])
    if code != 0:
        print(f"git add failed: {err}")
        return False

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = custom_message or f"Auto-sync: Workspace update [{now_str}]"
    code, out, err = run_cmd([git_bin, "commit", "-m", msg])
    if code != 0:
        print(f"git commit failed: {err or out}")
        return False
    print(out)

    code, out, err = run_cmd([git_bin, "push", "origin", "main"])
    if code != 0:
        print(f"git push failed: {err or out}")
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="GitGuard Auto-Sync & Pre-Commit Security Engine")
    parser.add_argument(
        "action",
        choices=["scan", "pre-commit", "scan-ops", "sync", "status", "install-hook"],
        help="Action to execute",
    )
    parser.add_argument("-m", "--message", type=str, default=None, help="Commit message for sync")
    parser.add_argument("--repo", type=str, default=None, help="Target git repository root")
    args = parser.parse_args()

    target_repo = Path(args.repo).resolve() if args.repo else WORKSPACE_ROOT

    if args.action == "pre-commit":
        is_safe, violations = audit_staged_index(target_repo)
        if is_safe:
            sys.exit(0)
        print(f"[GitGuard] Pre-commit check failed in {target_repo}:")
        for v in violations:
            print(f"  - {v['file']}:{v['line']} [{v['rule']}] -> {v['content']}")
        sys.exit(1)

    elif args.action == "scan":
        print(f"[GitGuard] Scanning working tree ({target_repo})...")
        is_safe, violations = audit_working_tree(target_repo)
        if is_safe:
            print("[GitGuard PASS] 0 security violations found.")
            sys.exit(0)
        print("[GitGuard FAIL] Violations detected:")
        for v in violations:
            print(f"  - {v['file']}:{v['line']} [{v['rule']}] -> {v['content']}")
        sys.exit(1)

    elif args.action == "status":
        git_bin = get_git_executable()
        _, out, _ = run_cmd([git_bin, "status", "-s"], cwd=target_repo)
        print(out if out else "(clean)")

    elif args.action == "install-hook":
        if install_pre_commit_hook(target_repo):
            print(f"[GitGuard] Installed pre-commit hook in {target_repo}")
            sys.exit(0)
        sys.exit(1)

    elif args.action == "scan-ops":
        import env_guard
        env_guard.verify_ops_env("SCAN_OPS")
        bad = []
        m365_dir = WORKSPACE_ROOT / "01_M365MCP"
        skip_dirs = {"__pycache__", ".git", "tests", "test", "01_Inventory_Team", "logs", "reports", "backups", "archive"}
        out_line_re = re.compile(r"(subject\s*=|subject_prefix\s*=|ws\[|<h1>|<h2>|<div class=\"footer\">|Materials Planning & Inventory)", re.IGNORECASE)
        dummy_lit_re = re.compile(r"(\[ACME|ACME Corp|ACME Manufacturing|Plant P001|Plant P002|\[SL TN\])")
        for dp, dns, fns in os.walk(m365_dir):
            dns[:] = [d for d in dns if d not in skip_dirs]
            for fn in sorted(fns):
                if not fn.endswith(".py") or fn in ("env_guard.py", "git_auto_sync.py", "org_manager.py"):
                    continue
                fp = Path(dp) / fn
                for idx, ln in enumerate(fp.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    s = ln.strip()
                    if s.startswith("#") or "_get_ops_env(" in s:
                        continue
                    if out_line_re.search(s) and dummy_lit_re.search(s):
                        bad.append(f"{fp.relative_to(WORKSPACE_ROOT)}:{idx}")
        if bad:
            for b in bad:
                print(f"  - {b}")
            sys.exit(1)
        print("[OpsGuard PASS] .env configured and 0 dummy output literals.")
        sys.exit(0)

    elif args.action == "sync":
        sys.exit(0 if sync_to_github(custom_message=args.message) else 1)


if __name__ == "__main__":
    main()
