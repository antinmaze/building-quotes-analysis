"""Check tracked or staged paths before publishing a source-only repository."""

from pathlib import Path, PurePosixPath
import subprocess
import sys


def allowed(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        return False
    if any(part.startswith(".") for part in path.parts if part != ".github"):
        return name in {".gitignore", ".env.sample"}
    if name in {"pyproject.toml", "requirements.lock", "LICENSE"}:
        return True
    root = path.parts[0]
    if root in {"src", "tests", "scripts"}:
        return path.suffix in {".py", ".sh"}
    if root == ".github":
        return len(path.parts) == 3 and path.parts[1] == "workflows" and path.suffix in {".yml", ".yaml"}
    if root == "infra":
        return path.suffix == ".bicep" or name in {"infra/bicepconfig.json", "infra/USAGE.txt"}
    return False


def main() -> int:
    command = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"] if "--staged" in sys.argv else ["git", "ls-files", "-z"]
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
