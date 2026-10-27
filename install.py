#!/usr/bin/env python3
"""Install the self-contained Skill without pip or any third-party runtime."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

NAME = "oceanbase-compete"


def install(skills_dir, update=False):
    source = Path(__file__).resolve().parent
    root = Path(skills_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / NAME
    if destination.exists() and not update:
        raise ValueError("Skill already exists; use --update to replace it")
    temporary = Path(tempfile.mkdtemp(prefix=".obcomp-install-", dir=root))
    staged = temporary / NAME
    backup = root / (".obcomp-backup-" + uuid.uuid4().hex)
    try:
        shutil.copytree(source / "skills" / NAME, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        shutil.copytree(source / "src" / "oceanbase_compete", staged / "scripts" / "oceanbase_compete", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        result = subprocess.run([sys.executable, str(staged / "scripts" / "obcomp.py"), "--version"], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ValueError("Staged Skill did not pass its executable check")
        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(staged, destination)
        except BaseException:
            if backup.exists():
                os.replace(backup, destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return destination
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills"
    parser.add_argument("--skills-dir", type=Path, default=default)
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    try:
        destination = install(args.skills_dir, args.update)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Installed: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
