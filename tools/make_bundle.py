"""Builds GRFS-Tools.zip: the server and the tools with a Python of their own, for a machine
that has none. Unpack anywhere, double-click a .bat.

    python tools/make_bundle.py                        downloads Python, writes dist/GRFS-Tools.zip
    python tools/make_bundle.py --python-zip FILE      uses an embeddable zip already on disk

An alternative to the two Windows executables built by tools/build_windows.py. The embeddable
Python is python.org's own signed build; nothing here is compiled or packed, and every script
stays readable.

The server needs nothing outside the standard library, which is what makes this possible: the
embeddable zip has no pip and no site-packages, and needs neither.
"""

import argparse
import hashlib
import io
import os
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PYTHON_VERSION = "3.11.9"
PYTHON_URL = ("https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip"
              % (PYTHON_VERSION, PYTHON_VERSION))
# The SHA-256 of that file, taken from a download over HTTPS from python.org on 2026-10-25
# (MD5 6d9aa08531d48fcc261ba667e2df17c4, as python.org lists it). A different file is refused.
PYTHON_SHA256 = "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b"
PTH_NAME = "python311._pth"

# What goes in, relative to the repository. Tests stay out; so does data/ - a bundle must not
# carry anybody's accounts.
INCLUDE_DIRS = ["grfs", "schema"]
INCLUDE_FILES = ["tools/doctor.py", "tools/fake_game.py", "tools/fake_client.py",
                 "grfs.ini.example", "README.md"]

# The .bat files are ASCII on purpose: cmd.exe reads them in the console's code page, and a
# Cyrillic prompt there comes out as garbage. The Python they start prints Russian fine.
BATS = {
    "doctor.bat": r'''@echo off
rem Checks this machine against the server: hosts file, ports, login, NAT.
"%~dp0python\python.exe" "%~dp0app\tools\doctor.py" %*
pause
''',
    "p2p-test.bat": r'''@echo off
rem Run on both machines at the same time. Asks for your name and the other player's.
"%~dp0python\python.exe" "%~dp0app\tools\doctor.py" p2p %*
pause
''',
    "fake-game.bat": r'''@echo off
rem A second player driven by hand. Type help inside.
set NAME=%~1
if "%NAME%"=="" set /p NAME=Name for the fake player:
"%~dp0python\python.exe" "%~dp0app\tools\fake_game.py" --name "%NAME%" %2 %3 %4 %5 %6
pause
''',
    "server.bat": r'''@echo off
rem The server itself. Its data (accounts, friends, clans, stats) goes to app\data.
cd /d "%~dp0app"
"%~dp0python\python.exe" -m grfs %*
pause
''',
}

README = """GRFS-Tools - сервер и проверки для Ghost Recon: Future Soldier, со своим Python.
Ничего ставить не надо: распакуй куда угодно и запускай .bat.

doctor.bat      проверяет этот компьютер: строку в hosts, службу конфигурации, вход, NAT echo,
                тип NAT. В конце пишет doctor-report.txt - его и присылай, если что-то не так.

p2p-test.bat    проверяет, доходят ли пакеты напрямую между двумя игроками (так играется
                кооператив). Запускать на ОБЕИХ машинах одновременно; спросит твоё имя и имя
                второго. Игру перед этим закрыть - нужен её порт UDP 2346.

fake-game.bat   второй игрок, которым управляешь руками (help - список команд).

server.bat      сам сервер, если он запускается на этой машине.

Перед игрой на машине, где сервера нет, в C:\\Windows\\System32\\drivers\\etc\\hosts должна быть
строка (от имени администратора):

    <адрес машины с сервером>    onlineconfigservice.ubi.com

Python: python.org, {version}, embeddable, без изменений (кроме {pth}).
"""


def fetch_python(cache_dir: str) -> bytes:
    path = os.path.join(cache_dir, os.path.basename(PYTHON_URL))
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        if hashlib.sha256(data).hexdigest() == PYTHON_SHA256:
            return data
    print("downloading %s" % PYTHON_URL)
    with urllib.request.urlopen(PYTHON_URL, timeout=120) as response:
        data = response.read()
    check_python(data)
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return data


def check_python(data: bytes, expected: str = PYTHON_SHA256) -> None:
    got = hashlib.sha256(data).hexdigest()
    if got != expected:
        raise ValueError("the embeddable Python's SHA-256 is %s, expected %s - not using it"
                         % (got, expected))


def pth_text(original: str) -> str:
    """The embeddable Python's path file, with the app folder added.

    A ._pth file replaces sys.path entirely and makes the interpreter ignore PYTHONPATH and the
    user's site-packages - which is what keeps a bundle from picking up whatever else is on the
    machine. The app folder is added beside it so `-m grfs` finds the package.
    """
    lines = [line for line in original.splitlines() if line.strip() != r"..\app"]
    lines.insert(2, r"..\app")
    return "\n".join(lines) + "\n"


def app_files(root: str = ROOT):
    """(path in the bundle, path on disk) for everything the bundle carries."""
    for top in INCLUDE_DIRS:
        for folder, dirs, files in os.walk(os.path.join(root, top)):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(folder, name)
                yield "app/" + os.path.relpath(full, root).replace(os.sep, "/"), full
    for rel in INCLUDE_FILES:
        full = os.path.join(root, rel)
        if os.path.exists(full):
            yield "app/" + rel, full


def build(python_zip: bytes, out_path: str, root: str = ROOT, top: str = "GRFS-Tools") -> list:
    """Write the bundle; returns the names in it."""
    check = zipfile.ZipFile(io.BytesIO(python_zip))
    if PTH_NAME not in check.namelist() or "python.exe" not in check.namelist():
        raise ValueError("that zip is not the embeddable Python %s" % PYTHON_VERSION)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    names = []
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as out:
        def put(name, data):
            out.writestr("%s/%s" % (top, name), data)
            names.append(name)
        for item in check.infolist():
            if item.is_dir():
                continue
            data = check.read(item.filename)
            if item.filename == PTH_NAME:
                data = pth_text(data.decode("ascii")).encode("ascii")
            put("python/" + item.filename, data)
        for name, full in app_files(root):
            with open(full, "rb") as f:
                put(name, f.read())
        for name, text in BATS.items():
            put(name, text.replace("\n", "\r\n").encode("ascii"))
        put("README.txt", README.format(version=PYTHON_VERSION, pth=PTH_NAME)
            .replace("\n", "\r\n").encode("utf-8-sig"))
    return names


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--python-zip", help="an embeddable Python %s zip already on disk"
                                             % PYTHON_VERSION)
    parser.add_argument("--out", default=os.path.join(ROOT, "dist", "GRFS-Tools.zip"))
    parser.add_argument("--cache", default=os.path.join(ROOT, "dist", "cache"))
    args = parser.parse_args(argv)
    try:
        if args.python_zip:
            with open(args.python_zip, "rb") as f:
                python_zip = f.read()
            check_python(python_zip)
        else:
            python_zip = fetch_python(args.cache)
        names = build(python_zip, args.out)
    except (OSError, ValueError) as e:
        print("cannot build: %s" % e)
        return 1
    print("wrote %s: %d files, %.1f MB" % (args.out, len(names),
                                          os.path.getsize(args.out) / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
