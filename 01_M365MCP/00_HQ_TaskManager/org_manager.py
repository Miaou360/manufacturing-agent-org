#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ACME Manufacturing M365 Automation - Organization & Icon Manager Tool
==============================================================
CLI tool to inspect, scaffold, and automatically synchronize
the 4-tier organization hierarchy (HQ -> DEPT -> JOB -> TF) and
Catppuccin Icons settings.

Usage:
  python org_manager.py list
  python org_manager.py sync-icons
  python org_manager.py create-dept <name> [--icon <icon_name>]
  python org_manager.py create-job <dept_name> <job_name> [--icon <icon_name>]
"""

import os
import sys
import json
import argparse
from pathlib import Path
from typing import Dict, Any, List, Optional

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent

# Default icon heuristics
KNOWN_DEPT_ICONS = {
    "01_DEPT_Inventory": "folder_packages",
    "02_DEPT_Invoice_Settlement": "folder_content",
    "02_DEPT_Settlement": "folder_content",
    "03_DEPT_EDI_OrderSync": "folder_api",
    "03_DEPT_EDI": "folder_api",
    "04_DEPT_Logistics_Receiving": "folder_cargo",
    "04_DEPT_Logistics": "folder_cargo",
    "05_DEPT_Program_MasterData": "folder_components",
    "05_DEPT_MasterData": "folder_components",
}

KNOWN_JOB_ICONS = {
    "00_JOB_StockAccuracy": "folder_audit",
    "01_JOB_GR_GI_Error": "folder_connection",
    "02_JOB_Backlog_Clear": "folder_queue",
    "03_JOB_Backlog_Analysis": "folder_benchmark",
}

FILE_PATTERNS = {
    "rocket": ["run_", "pipeline_monitor.py"],
    "zap": ["_engine.py", "_resolver.py"],
    "benchmark": ["_analysis.py", "analytics_"],
    "robots": ["_operator.py", "_crawler.py"],
    "workflow": ["_generator.py", "_notifier.py"],
    "database": ["db_manager.py", "_db.py"]
}

SETTINGS_PATHS = [
    WORKSPACE_ROOT / ".vscode" / "settings.json",
    PROJECT_ROOT / ".vscode" / "settings.json",
    Path(os.environ.get("APPDATA", "")) / "Antigravity IDE" / "User" / "settings.json"
]

THEME_DIST_DIR = Path(os.environ.get("USERPROFILE", "")) / ".antigravity-ide" / "extensions" / "catppuccin.catppuccin-vsc-icons-1.26.0-universal" / "dist"


def scan_organization() -> Dict[str, Any]:
    """Scans PROJECT_ROOT for HQ, DEPTs, JOBs, and TFs."""
    org_structure = {
        "hq": [],
        "depts": [],
        "jobs": {},
        "tfs": [],
        "files": []
    }
    
    if not PROJECT_ROOT.exists():
        return org_structure

    for item in sorted(PROJECT_ROOT.iterdir()):
        if not item.is_dir() or item.name.startswith("."):
            continue
        if "_HQ_" in item.name or item.name == "00_TaskManager":
            org_structure["hq"].append(item.name)
        elif "_DEPT_" in item.name or item.name == "01_Inventory_Team":
            org_structure["depts"].append(item.name)
            dept_jobs = []
            for sub in sorted(item.iterdir()):
                if sub.is_dir() and not sub.name.startswith("."):
                    if "_JOB_" in sub.name or sub.name.startswith("0"):
                        dept_jobs.append(sub.name)
                    elif "_TF_" in sub.name or "TF" in sub.name:
                        org_structure["tfs"].append(f"{item.name}/{sub.name}")
            org_structure["jobs"][item.name] = dept_jobs
        elif "_TF_" in item.name or item.name.startswith("TF"):
            org_structure["tfs"].append(item.name)

    # Scan python files for engine tagging
    for root, dirs, files in os.walk(PROJECT_ROOT):
        if any(x in root for x in [".git", "__pycache__", "site-packages", ".venv"]):
            continue
        for f in files:
            if f.endswith(".py"):
                for icon, patterns in FILE_PATTERNS.items():
                    for pat in patterns:
                        if (pat.startswith("_") and f.endswith(pat)) or (pat.endswith("_") and f.startswith(pat)) or (pat in f):
                            org_structure["files"].append((f, icon))
                            break

    return org_structure


def cmd_list():
    """Lists current organizational hierarchy and status."""
    org = scan_organization()
    print("=" * 80)
    print(f" 🏛️ {os.getenv('COMPANY_NAME', 'ACME Manufacturing').upper()} M365 AUTOMATION - VIRTUAL ORGANIZATIONAL HIERARCHY")
    print("=" * 80)
    
    print("\n[LEVEL 1: HEADQUARTERS & PMO]")
    for hq in org["hq"]:
        print(f"  🛡️ {hq}/ (Icon: folder_admin)")

    print("\n[LEVEL 2: DEPARTMENTS (DEPT)]")
    for dept in org["depts"]:
        d_icon = KNOWN_DEPT_ICONS.get(dept, "folder_packages")
        print(f"  📦 {dept}/ (Icon: {d_icon})")
        jobs = org["jobs"].get(dept, [])
        for j in jobs:
            j_icon = KNOWN_JOB_ICONS.get(j, "folder_workflows")
            print(f"     ├── ⚙️ {j}/ (Icon: {j_icon})")

    if org["tfs"]:
        print("\n[LEVEL 4: TASK FORCES & LABS (TF)]")
        for tf in org["tfs"]:
            print(f"  ⏱️ {tf} (Icon: folder_temp)")

    print("\n[KEY AUTOMATION ENGINE FILES DETECTED]")
    seen = set()
    for f, icon in org["files"]:
        if f not in seen:
            seen.add(f)
            symbol = "🚀" if icon == "rocket" else ("⚡" if icon == "zap" else ("🤖" if icon == "robots" else "📊"))
            print(f"  {symbol} {f:<30} -> {icon}")
    print("=" * 80)


def cmd_sync_icons():
    """Synchronizes folder and file icon associations into settings and Catppuccin theme."""
    org = scan_organization()
    
    # 1. Build folder associations
    folder_assoc = {
        "01_M365MCP": "folder_cloud",
        "db": "folder_database",
        "reports": "folder_docs",
        "audit_reports": "folder_docs",
        "resolution_logs": "folder_docs",
        "ontology": "folder_core",
        "scratch": "folder_temp",
        "logs": "folder_security",
        "cogi_errors": "folder_queue",
        "gi_errors": "folder_connection",
        "gr_errors": "folder_connection",
        "mb52": "folder_packages",
        "raw_history": "folder_database",
        "gi_daily_stack": "folder_packages",
        "gr_daily_stack": "folder_packages",
        "missing_gi_daily_stack": "folder_packages",
        "wms_daily_stack": "folder_packages",
        "wms_parts_stack": "folder_packages",
        ".agents": "folder_config",
        "plugins": "folder_plugins",
        "skills": "folder_functions",
        "m365": "folder_cloud",
        "TF": "folder_temp",
        "tf": "folder_temp",
        "temp": "folder_temp",
        "tmp": "folder_temp",
        "pilot": "folder_temp",
        "sandbox": "folder_temp",
        "experimental": "folder_temp"
    }

    # Add known & planned
    folder_assoc.update(KNOWN_DEPT_ICONS)
    folder_assoc.update(KNOWN_JOB_ICONS)

    for hq in org["hq"]:
        folder_assoc[hq] = "folder_admin"

    for dept in org["depts"]:
        if dept not in folder_assoc:
            folder_assoc[dept] = "folder_packages"
        for j in org["jobs"].get(dept, []):
            if j not in folder_assoc:
                if "audit" in j.lower() or "accuracy" in j.lower():
                    folder_assoc[j] = "folder_audit"
                elif "error" in j.lower() or "sync" in j.lower():
                    folder_assoc[j] = "folder_connection"
                elif "backlog" in j.lower() or "clear" in j.lower():
                    folder_assoc[j] = "folder_queue"
                elif "analysis" in j.lower() or "analytics" in j.lower():
                    folder_assoc[j] = "folder_benchmark"
                else:
                    folder_assoc[j] = "folder_workflows"

    for tf in org["tfs"]:
        name = tf.split("/")[-1]
        folder_assoc[name] = "folder_temp"

    # 2. Build file associations
    file_assoc = {}
    for f, icon in org["files"]:
        file_assoc[f] = icon

    # Standard explicit overrides
    file_assoc.update({
        "run_continuous_sync.py": "rocket",
        "run_daily_job.py": "rocket",
        "run_analysis.py": "rocket",
        "pipeline_monitor.py": "rocket",
        "run_daily_audit.py": "rocket",
        "sync_engine.py": "zap",
        "backlog_resolver.py": "zap",
        "analytics_engine.py": "benchmark",
        "audit_engine.py": "benchmark",
        "sap_operator.py": "robots",
        "wms_crawler.py": "robots",
        "cogi_crawler.py": "robots",
        "report_generator.py": "workflow",
        "notifier.py": "workflow",
        "db_manager.py": "database"
    })

    # 3. Update VS Code settings files
    for sp in SETTINGS_PATHS:
        if sp.exists():
            try:
                data = json.load(open(sp, encoding="utf-8"))
                f_cfg = data.setdefault("catppuccin-icons.associations.folders", {})
                f_cfg.update(folder_assoc)
                fi_cfg = data.setdefault("catppuccin-icons.associations.files", {})
                fi_cfg.update(file_assoc)
                with open(sp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                print(f"[OK] Updated settings: {sp}")
            except Exception as e:
                print(f"[WARN] Failed to update {sp}: {e}")

    # 4. Inject directly into theme.json of all flavors
    if THEME_DIST_DIR.exists():
        for flavor in ["mocha", "latte", "frappe", "macchiato"]:
            t_path = THEME_DIST_DIR / flavor / "theme.json"
            if t_path.exists():
                try:
                    t_data = json.load(open(t_path, encoding="utf-8"))
                    fn = t_data.setdefault("folderNames", {})
                    fne = t_data.setdefault("folderNamesExpanded", {})
                    for folder, icon in folder_assoc.items():
                        fn[folder] = icon
                        fne[folder] = f"{icon}_open"
                    fln = t_data.setdefault("fileNames", {})
                    for fname, icon in file_assoc.items():
                        fln[fname] = icon
                    with open(t_path, "w", encoding="utf-8") as f:
                        json.dump(t_data, f, indent=2, ensure_ascii=False)
                    print(f"[OK] Synchronized Catppuccin theme cache for flavor: {flavor}")
                except Exception as e:
                    print(f"[WARN] Failed to update theme for {flavor}: {e}")

    print("\n✨ All organization folders & file icons successfully synchronized!")


def cmd_create_dept(name: str, icon: Optional[str] = None):
    """Scaffolds a new department folder with standard layout."""
    if not name.startswith("0") or "_DEPT_" not in name:
        # Auto-format
        seq = "02"
        existing = [d for d in PROJECT_ROOT.iterdir() if d.is_dir() and "_DEPT_" in d.name]
        if existing:
            last = sorted([d.name for d in existing])[-1]
            try:
                seq = f"{int(last[:2]) + 1:02d}"
            except:
                pass
        dept_dir_name = f"{seq}_DEPT_{name}"
    else:
        dept_dir_name = name

    dept_path = PROJECT_ROOT / dept_dir_name
    if dept_path.exists():
        print(f"[ERR] Department folder already exists: {dept_path}")
        return

    dept_path.mkdir(parents=True, exist_ok=True)
    
    # Create README.md
    readme_content = f"""# {dept_dir_name} (Virtual Department)

본 디렉터리는 ACME Manufacturing `{dept_dir_name}` 가상 부서의 배치 자동화 및 엔지니어링 허브입니다.

---

## 🏛️ 부서 개요 및 산하 단위 작업 (Batch Jobs)
* **소속 본부**: `00_HQ_TaskManager`
* **주요 R&R**: 해당 비즈니스 영역의 SAP ↔ WMS ↔ M365 자동화 거버넌스 전담

### 산하 Job 목록
* *(신규 작업 생성 시 `python 00_HQ_TaskManager/org_manager.py create-job {dept_dir_name} <JobName>` 사용)*
"""
    with open(dept_path / "README.md", "w", encoding="utf-8") as f:
        f.write(readme_content)

    print(f"[OK] Created department folder: {dept_path}")
    
    # Register icon if specified
    if icon:
        KNOWN_DEPT_ICONS[dept_dir_name] = icon
    
    cmd_sync_icons()


def cmd_create_job(dept_name: str, job_name: str, icon: Optional[str] = None):
    """Scaffolds a new job folder under a department with standard files."""
    # Find department folder
    dept_candidates = [d for d in PROJECT_ROOT.iterdir() if d.is_dir() and (dept_name in d.name)]
    if not dept_candidates:
        print(f"[ERR] Department not found matching: {dept_name}")
        return
    dept_path = dept_candidates[0]

    if not job_name.startswith("0") or "_JOB_" not in job_name:
        existing = [j for j in dept_path.iterdir() if j.is_dir() and ("_JOB_" in j.name or j.name.startswith("0"))]
        seq = "00"
        if existing:
            last = sorted([j.name for j in existing])[-1]
            try:
                seq = f"{int(last[:2]) + 1:02d}"
            except:
                seq = f"{len(existing):02d}"
        job_dir_name = f"{seq}_JOB_{job_name}"
    else:
        job_dir_name = job_name

    job_path = dept_path / job_dir_name
    if job_path.exists():
        print(f"[ERR] Job folder already exists: {job_path}")
        return

    job_path.mkdir(parents=True, exist_ok=True)
    (job_path / "db").mkdir(exist_ok=True)
    (job_path / "reports").mkdir(exist_ok=True)

    # 1. Create run_pipeline.bat
    bat_content = f"""@echo off
chcp 65001 > nul
setlocal

cd /d "%~dp0"
set "PYTHON_EXE=%LOCALAPPDATA%\\Programs\\Python\\Python313\\python.exe"

"%PYTHON_EXE%" run_{job_name.lower()}.py %*

set EXIT_CODE=%ERRORLEVEL%
exit /b %EXIT_CODE%
"""
    with open(job_path / f"run_{job_name.lower()}_pipeline.bat", "w", encoding="utf-8") as f:
        f.write(bat_content)

    # 2. Create run_job.py
    py_content = f"""#!/usr/bin/env python3
# -*- coding: utf-8 -*-
\"\"\"
{job_dir_name} Execution Entry Point
==================================================
Department: {dept_path.name}
Autonomous pipeline launcher.
\"\"\"

import os
import sys
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser(description="{job_dir_name} Launcher")
    parser.add_argument("--test", action="store_true", help="Run in test/dry-run mode")
    args = parser.parse_args()
    
    print(f"[{job_dir_name}] Pipeline started successfully.")

if __name__ == "__main__":
    main()
"""
    with open(job_path / f"run_{job_name.lower()}.py", "w", encoding="utf-8") as f:
        f.write(py_content)

    # 3. Create README.md
    readme_content = f"""# {job_dir_name}

> **소속 부서**: `{dept_path.name}`  
> **공장 기준**: Plant `P001` (Headlamp) / `P002`  
> **실행 주기**: TBD (예: 매일 06:00 AM)  
> **선행 조건**: 없음  
> **공식 수신인 (TO)**: Planner `<user_placeholder>`  

## 🛡️ 개요 및 운영 규칙
본 작업은 {dept_path.name} 산하의 독립 배치 자동화 작업입니다.

---

## 🚀 수동 실행 방법
```bash
{job_dir_name}\\run_{job_name.lower()}_pipeline.bat
```
"""
    with open(job_path / "README.md", "w", encoding="utf-8") as f:
        f.write(readme_content)

    print(f"[OK] Created job scaffold: {job_path}")
    if icon:
        KNOWN_JOB_ICONS[job_dir_name] = icon

    cmd_sync_icons()


def main():
    parser = argparse.ArgumentParser(description="ACME Manufacturing Organization & Icon Manager")
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    subparsers.add_parser("list", help="List organizational structure and assigned icons")
    subparsers.add_parser("sync-icons", help="Synchronize Catppuccin folder and file icons")

    git_parser = subparsers.add_parser("sync-git", help="Security scan and auto-sync to GitHub")
    git_parser.add_argument("-m", "--message", help="Custom commit message")

    dept_parser = subparsers.add_parser("create-dept", help="Scaffold a new department")
    dept_parser.add_argument("name", help="Department name (e.g. Invoice_Settlement)")
    dept_parser.add_argument("--icon", help="Catppuccin folder icon name (e.g. folder_content)")

    job_parser = subparsers.add_parser("create-job", help="Scaffold a new job under a department")
    job_parser.add_argument("dept", help="Department name substring (e.g. Inventory)")
    job_parser.add_argument("job", help="Job name (e.g. StockAccuracy)")
    job_parser.add_argument("--icon", help="Catppuccin folder icon name (e.g. folder_audit)")

    args = parser.parse_args()

    if args.command == "list" or not args.command:
        cmd_list()
    elif args.command == "sync-icons":
        cmd_sync_icons()
    elif args.command == "sync-git":
        import git_auto_sync
        git_auto_sync.sync_to_github(custom_message=args.message)
    elif args.command == "create-dept":
        cmd_create_dept(args.name, args.icon)
    elif args.command == "create-job":
        cmd_create_job(args.dept, args.job, args.icon)


if __name__ == "__main__":
    main()
