"""Build the server and native game launcher into dist/GRFS-Windows-x64.zip on Windows."""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="", help="suggest this IPv4 in the launcher; otherwise ask")
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="output directory")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("use Windows Python (or run build_windows.bat)")
    from make_bundle import build as build_payload, fetch_python
    from make_connect import build as build_client
    from make_connect import find_csc

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    csc = find_csc()
    if not csc:
        parser.error("the .NET Framework 4 C# compiler is required")
    python_zip = fetch_python(str(out / "cache"))
    with tempfile.TemporaryDirectory(prefix="grfs-build-") as folder:
        payload = Path(folder) / "runtime.zip"
        # Reuse the verified Python download and source selection of the existing tools bundle.
        # No accounts, configuration overrides, logs or build dependencies enter this payload.
        build_payload(python_zip, str(payload), top="GRFS-Runtime")
        runtime_files = []
        with zipfile.ZipFile(payload) as runtime:
            for member in runtime.infolist():
                relative = member.filename.split("/", 1)[1]
                if not relative.startswith(("app/", "python/")) or member.is_dir():
                    continue
                destination = out / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(runtime.read(member))
                runtime_files.append(relative)
    command = [csc, "/nologo", "/target:exe", "/optimize+", "/utf8output",
               "/out:" + str(out / "GRFS-Server.exe"),
               "/win32manifest:" + str(ROOT / "tools/server/app.manifest"),
               str(ROOT / "tools/server/GRFSServer.cs")]
    subprocess.run(command, check=True)
    said = build_client(str(out / "GRFS-Client.exe"), args.server, csc)
    if said:
        print(said)
    shutil.copy2(ROOT / "grfs.ini.example", out / "grfs.ini.example")
    shutil.copy2(ROOT / "docs" / "WINDOWS.md", out / "WINDOWS.md")
    archive = out / "GRFS-Windows-x64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in ["GRFS-Server.exe", "GRFS-Client.exe", "grfs.ini.example", "WINDOWS.md", *runtime_files]:
            bundle.write(out / name, name)
    print("Ready: %s" % archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
