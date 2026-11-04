"""Check tracked or staged paths before publishing a source-only repository."""

import argparse
from pathlib import Path, PurePosixPath
import subprocess
import sys

PRIVATE_CAMPAIGN_FILES = {
    "PLAN-TRAITEMENT.md",
    "scripts/create_po_questionnaire.py",
    "scripts/group_waves_by_client.py",
    "scripts/prepare_wave2.py",
    "scripts/prepare_wave3.py",
    "scripts/report_wave1_selection.py",
    "scripts/select_wave2.py",
    "scripts/verify_pilot.py",
    "tests/test_wave_preparation.py",
}


def allowed(name: str) -> bool:
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts:
        return False
    if path.as_posix() in PRIVATE_CAMPAIGN_FILES:
        return False
    if any(part.startswith(".") for part in path.parts if part != ".github"):
        return name in {".gitignore", ".env.sample"}
    if name in {"pyproject.toml", "requirements.lock", "LICENSE"}:
        return True
    if name == "README.md":
        return True
    root = path.parts[0]
    if root in {"src", "tests", "scripts"}:
        return path.suffix in {".py", ".sh"}
    if root == ".github":
        return len(path.parts) == 3 and path.parts[1] == "workflows" and path.suffix in {".yml", ".yaml"}
    if root == "infra":
        return path.suffix == ".bicep" or name in {
            "infra/bicepconfig.json", "infra/USAGE.txt", "infra/main.parameters.example.json",
        }
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="Check staged additions and changes only")
    mode.add_argument("--include-untracked", action="store_true",
                      help="Also check untracked, non-ignored commit candidates")
    args = parser.parse_args()
    if args.staged:
        command = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]
    elif args.include_untracked:
        command = ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    else:
        command = ["git", "ls-files", "-z"]
    names = subprocess.check_output(command).decode().split("\0")
    rejected = [name for name in names if name and (
        not allowed(name) or Path(name).is_symlink()
    )]
    if rejected:
        print("Publication refusée : seuls les fichiers source autorisés peuvent être suivis.", file=sys.stderr)
        for name in rejected:
            print(name, file=sys.stderr)
        return 1
    print("Fichiers publics : code et infrastructure uniquement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
