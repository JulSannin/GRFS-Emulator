"""Builds GRFS-Connect.exe: what a player on another machine runs to play on this server.

    python tools/make_connect.py                        suggests this machine's LAN address
    python tools/make_connect.py --server 192.168.1.8   suggests that one
    python tools/make_connect.py --server ""            suggests nothing: the exe asks

The source is tools/connect/GRFSConnect.cs. The compiler is the C# compiler of .NET Framework 4,
which every Windows 10 and 11 has in C:\\Windows\\Microsoft.NET, so there is nothing to install,
and the exe runs on any of them as it is. The address is baked in as the one the exe offers;
whoever runs it can type another.

An exe, unlike GRFS-Tools.zip (tools/make_bundle.py), because this one is small, is not packed and
unpacks nothing: there is little in it for an antivirus to object to. SmartScreen still warns
about any unsigned exe that came from the internet - "More info", then "Run anyway".
"""

import argparse
import ipaddress
import os
import socket
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "tools", "connect", "GRFSConnect.cs")
MANIFEST = os.path.join(ROOT, "tools", "connect", "app.manifest")
REFERENCES = ["System.Core.dll", "Microsoft.CSharp.dll", "System.Web.Extensions.dll"]

BAKED = """// Written by tools/make_connect.py for this build.
namespace GrfsConnect
{
    internal static class Baked
    {
        public const string Server = "%s";
    }
}
"""


def find_csc():
    """The .NET Framework 4 C# compiler, or None."""
    windir = os.environ.get("WINDIR", r"C:\Windows")
    for framework in ("Framework64", "Framework"):
        path = os.path.join(windir, "Microsoft.NET", framework, "v4.0.30319", "csc.exe")
        if os.path.exists(path):
            return path
    return None


def lan_addresses(addresses=None):
    """This machine's addresses another player could reach, best first: 192.168/16, then 10/8,
    then 172.16/12. Loopback, link-local, 100.64/10 (where VPNs such as AmneziaVPN put their
    adapter) and public addresses are left out."""
    if addresses is None:
        try:
            addresses = socket.gethostbyname_ex(socket.gethostname())[2]
        except OSError:
            addresses = []
    ranks = [ipaddress.ip_network(n) for n in ("192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12")]
    found = []
    for text in addresses:
        try:
            ip = ipaddress.IPv4Address(text)
        except ValueError:
            continue
        rank = next((i for i, net in enumerate(ranks) if ip in net), None)
        if rank is not None and text not in [a for _, a in found]:
            found.append((rank, text))
    return [a for _, a in sorted(found, key=lambda pair: pair[0])]


def default_server():
    """What the server would have clients use: its advertise setting when it has one, else the
    best LAN address. With the alternatives, to show."""
    sys.path.insert(0, ROOT)
    try:
        from grfs.config import Config
        advertise = Config().get("advertise")
    except Exception:
        advertise = ""
    finally:
        sys.path.remove(ROOT)
    lan = lan_addresses()
    if advertise and not advertise.startswith("127."):
        return advertise, [a for a in lan if a != advertise]
    return (lan[0] if lan else ""), lan[1:]


def build(out_path, server="", csc=None):
    """Compile the exe with `server` as the address it offers; returns the compiler's output."""
    if server:
        server = str(ipaddress.IPv4Address(server))
    csc = csc or find_csc()
    if not csc:
        raise OSError("no C# compiler: .NET Framework 4 is not in %s"
                      % os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Microsoft.NET"))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="grfs-connect-") as folder:
        baked = os.path.join(folder, "Baked.cs")
        with open(baked, "w", encoding="utf-8") as f:
            f.write(BAKED % server)
        argv = [csc, "/nologo", "/target:exe", "/optimize+", "/utf8output",
                "/out:" + os.path.abspath(out_path), "/win32manifest:" + MANIFEST]
        argv += ["/reference:" + name for name in REFERENCES]
        argv += [SOURCE, baked]
        result = subprocess.run(argv, capture_output=True)
    said = result.stdout.decode("utf-8", "replace").strip()
    if result.returncode != 0:
        raise OSError("the compiler failed:\n" + said)
    return said


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--server", help="the address the exe offers; by default this machine's"
                                         " LAN address, and \"\" for none")
    parser.add_argument("--out", default=os.path.join(ROOT, "dist", "GRFS-Connect.exe"))
    args = parser.parse_args(argv)
    server, others = (args.server, []) if args.server is not None else default_server()
    try:
        said = build(args.out, server)
    except (OSError, ValueError) as e:
        print("cannot build: %s" % e)
        return 1
    if said:
        print(said)
    print("wrote %s, %d KB, offering %s" % (args.out, os.path.getsize(args.out) // 1024,
                                            server or "no address"))
    if others:
        print("other addresses of this machine: %s (--server picks one)" % ", ".join(others))
    return 0


if __name__ == "__main__":
    sys.exit(main())
