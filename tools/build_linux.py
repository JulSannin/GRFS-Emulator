"""Build the Linux server folder and tar.gz, using only Python's standard library."""

import argparse
from pathlib import Path
import shutil
import tarfile

ROOT = Path(__file__).resolve().parent.parent
FILES = {
    "run-server.sh": "run-server.sh",
    "grfs.ini.example": "grfs.ini.example",
    "README.md": "docs/LINUX.md",
    "grfs-emulator.service": "deploy/grfs-emulator.service",
    "tools/fake_client.py": "tools/fake_client.py",
}


def package_files(root: Path):
    for directory in ("grfs", "schema"):
        for source in sorted((root / directory).rglob("*")):
            if source.is_file() and "__pycache__" not in source.parts and source.suffix not in (".pyc", ".pyo"):
                yield source.relative_to(root).as_posix(), source
    for name, source in FILES.items():
        yield name, root / source


def build(out: Path, archive: Path, root: Path = ROOT):
    files = list(package_files(root))
    out.mkdir(parents=True, exist_ok=True)
    for name, source in files:
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if name == "run-server.sh":
            target.chmod(0o755)
    archive.parent.mkdir(parents=True, exist_ok=True)

    def archive_entry(entry):
        entry.mode = 0o755 if entry.name.endswith("/run-server.sh") else 0o644
        entry.uid = entry.gid = 0
        entry.uname = entry.gname = "root"
        return entry

    with tarfile.open(archive, "w:gz") as bundle:
        for name, _ in files:
            bundle.add(out / name, arcname="GRFS-Linux/" + name, recursive=False, filter=archive_entry)
    return files


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "dist/linux")
    parser.add_argument("--archive", type=Path, default=ROOT / "dist/GRFS-Linux.tar.gz")
    args = parser.parse_args(argv)
    files = build(args.out.resolve(), args.archive.resolve())
    print("Ready: %s (%d files; Python 3.11+ required on the server)" % (args.archive, len(files)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
