"""Read the game's map database out of Yeti.big and write it to schema/maps.json.

MapListProtocol.GetMapList has to answer with world keys - the 32-bit bigfile keys of the .wor
files a host loads - and an empty or wrong list stops a matchmade lobby from being created. The
keys are nowhere in the executables. They are in the bigfile, in the same table the game script
loads its own map list from: a self-describing table that starts with its field list

    u32 field area size, u32 (unknown), u32 field count,
    field count x { zero-terminated name, u8 type, u32 offset }

followed directly by its records. The fields that matter are "UniqueID", "World key" (type 9, a
bigfile key), "Name id" (the Oasis string id of the map's name - the same constants the game
script switches on to number its maps), "Type" (2 = adversarial, 1 = campaign / guerrilla
level, 3 = co-op, 0 = other) and "GameMode" (1..8). The records have no count and no stated
size: the stride is the one under which every record is well formed, and the table ends at the
first record that is not.

The file is only read. Usage:

    python tools/extract_maps.py "<game>/Yeti.big" [--oasis <game>/Language/Oasis_English.inf]
                                 [-o schema/maps.json]

Names come from the English Oasis string file beside the bigfile, when it is there.
"""

import argparse
import json
import mmap
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(HERE, "schema", "maps.json")

# The start of the field list, as it is in the file: "UniqueID" is an int at offset 0 and is
# followed by "World key".
SIGNATURE = b"UniqueID\x00\x00\x00\x00\x00\x00World key\x00"
WANTED = ("UniqueID", "World key", "Name id", "Type", "GameMode")
FIELD_TYPES = {0: "int", 1: "float", 2: "bool", 4: "color", 5: "enum", 9: "key"}
MAX_FIELDS = 512


class FormatError(ValueError):
    pass


def parse_fields(data, at: int):
    """The field list whose first name starts at `at`. Returns (fields, end), fields being
    [(name, type, offset)] in file order and end the offset of the first record."""
    if at < 12:
        raise FormatError("no table header before the field list")
    area, _unknown, count = struct.unpack_from("<3I", data, at - 12)
    if not 0 < count <= MAX_FIELDS:
        raise FormatError("implausible field count %d" % count)
    fields, p = [], at
    for _ in range(count):
        end = data.find(b"\x00", p, p + 256)
        if end < 0:
            raise FormatError("unterminated field name at %#x" % p)
        name = bytes(data[p:end]).decode("latin-1")
        ftype = data[end + 1]
        offset = struct.unpack_from("<I", data, end + 2)[0]
        fields.append((name, ftype, offset))
        p = end + 6
    return fields, p, area


def _layout(fields) -> dict:
    by_name = {name: offset for name, _, offset in fields}
    missing = [name for name in WANTED if name not in by_name]
    if missing:
        raise FormatError("the table has no field %s" % ", ".join(missing))
    return {name: by_name[name] for name in WANTED}


def _record(data, base: int, layout: dict):
    """The wanted fields of the record at `base`, or None when it is not a record."""
    if base + max(layout.values()) + 4 > len(data):
        return None
    rec = {name: struct.unpack_from("<I", data, base + off)[0] for name, off in layout.items()}
    if rec["UniqueID"] >= 0x10000 or rec["Type"] >= 16 or rec["GameMode"] >= 16:
        return None
    return rec


def _walk(data, start: int, stride: int, layout: dict) -> list:
    """Records from `start` while each is well formed and the unique ids keep rising."""
    out, last, p = [], -1, start
    while True:
        rec = _record(data, p, layout)
        if rec is None or rec["UniqueID"] <= last:
            return out
        out.append(rec)
        last = rec["UniqueID"]
        p += stride


def parse_table(data, at: int) -> dict:
    """The map table whose field list starts at `at` (the "U" of "UniqueID")."""
    fields, start, area = parse_fields(data, at)
    layout = _layout(fields)
    best_stride, best = 0, []
    # The records are at least as long as the area the offsets describe; the stride is the one
    # that gives the longest run of good records.
    lo = max(area, max(layout.values()) + 4)
    for stride in range(lo - lo % 4, lo + 0x200, 4):
        run = _walk(data, start, stride, layout)
        if len(run) > len(best):
            best_stride, best = stride, run
    if len(best) < 2:
        raise FormatError("no run of records after the field list at %#x" % at)
    return {"offset": start, "stride": best_stride, "fields": fields, "records": best}


def find_tables(data) -> list:
    out, p = [], data.find(SIGNATURE)
    while p >= 0:
        out.append(p)
        p = data.find(SIGNATURE, p + 1)
    return out


def read_oasis(path: str) -> dict:
    """id -> text from an Oasis_<language>.inf: lines of ID:Type:Mask:{custom:}Text."""
    names = {}
    if not path or not os.path.exists(path):
        return names
    with open(path, "rb") as f:
        text = f.read().decode("utf-8", "replace")
    for line in text.splitlines():
        m = re.match(r"(\d+):(\d+):(\d+):(.*)$", line)
        if m:
            value = m.group(4)
            if m.group(3) != "0":           # a mask: the text carries two numbers first
                value = re.sub(r"^\d+:\d+:", "", value)
            names[int(m.group(1))] = value.strip()
    return names


def build(table: dict, names: dict, source: str) -> dict:
    """The JSON the server loads: every record, and the records grouped by map."""
    maps = {}
    for rec in table["records"]:
        if rec["Name id"] in (0, 0xFFFFFFFF):
            continue
        entry = maps.setdefault((rec["Type"], rec["Name id"]), {
            "name_id": rec["Name id"], "name": names.get(rec["Name id"], ""),
            "type": rec["Type"], "modes": {}})
        keys = entry["modes"].setdefault(str(rec["GameMode"]), [])
        if "0x%08x" % rec["World key"] not in keys:
            keys.append("0x%08x" % rec["World key"])
    return {
        "source": "%s, map table at %#x, %d records of %#x bytes (tools/extract_maps.py)"
                  % (source, table["offset"], len(table["records"]), table["stride"]),
        "types": {"0": "other", "1": "campaign / guerrilla level", "2": "adversarial",
                  "3": "co-op"},
        "maps": [dict(m, modes=dict(sorted(m["modes"].items(), key=lambda kv: int(kv[0]))))
                 for m in sorted(maps.values(), key=lambda m: (m["type"], m["name_id"]))],
        "records": [{"id": r["UniqueID"], "key": "0x%08x" % r["World key"],
                     "name_id": r["Name id"], "type": r["Type"], "mode": r["GameMode"]}
                    for r in table["records"]],
    }


def dumps(result: dict) -> str:
    """JSON with one map or record to a line, so that a re-extraction diffs readably."""
    parts = []
    for key, value in result.items():
        if isinstance(value, list):
            rows = ",\n".join("  " + json.dumps(row) for row in value)
            parts.append(" %s: [\n%s\n ]" % (json.dumps(key), rows))
        else:
            parts.append(" %s: %s" % (json.dumps(key), json.dumps(value)))
    return "{\n" + ",\n".join(parts) + "\n}\n"


def extract(bigfile: str, oasis: str = None) -> dict:
    with open(bigfile, "rb") as f:
        data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            tables = []
            for at in find_tables(data):
                try:
                    tables.append(parse_table(data, at))
                except FormatError:
                    continue
        finally:
            data.close()
    if not tables:
        raise FormatError("no map table in %s" % bigfile)
    table = max(tables, key=lambda t: len(t["records"]))
    if oasis is None:
        oasis = os.path.join(os.path.dirname(os.path.abspath(bigfile)), "Language",
                             "Oasis_English.inf")
    return build(table, read_oasis(oasis), os.path.basename(bigfile))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("bigfile", help="the game's Yeti.big")
    parser.add_argument("--oasis", help="an Oasis_<language>.inf for the map names")
    parser.add_argument("-o", "--out", default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    try:
        result = extract(args.bigfile, args.oasis)
    except (OSError, FormatError) as e:
        print("extract_maps: %s" % e, file=sys.stderr)
        return 1
    with open(args.out + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        f.write(dumps(result))
    os.replace(args.out + ".tmp", args.out)
    adversarial = [m for m in result["maps"] if m["type"] == 2]
    print("%s: %d records, %d adversarial maps -> %s" % (result["source"], len(result["records"]),
                                                         len(adversarial), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
