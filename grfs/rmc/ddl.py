"""Protocol schema and a codec driven by it.

The schema comes from the DDL parse trees the game carries in its own executable
(schema/grfs-ddl.json, extracted with research tooling), so requests are decoded and responses
encoded exactly as the client's generated code expects - no hand-written byte layouts for the
27 OSDK protocols. Protocols Ubisoft added on top (Hermes) have no DDL in the binary; their
schemas are declared by hand with the same type syntax (see add_protocol).

Values: integers, str, bytes, bool, float; lists for qlist/std_list/qvector; lists of
(key, value) pairs for std_map (a dict is accepted when encoding); dicts for classes; for
any<Data,string> a dict with "__class__" (and "__raw__" when the class is unknown).
"""

import json
from dataclasses import dataclass, field

from .stream import Reader, Writer

IN, OUT, INOUT = 1, 2, 3

# Fields that carry the player's Ubisoft credentials. The game sends them to whatever server it
# talks to; this server never needs them, so they are skipped on the wire and never stored.
SENSITIVE = {
    ("UbiAuthenticationLoginCustomData", "m_password"),
    ("UbiAuthenticationLoginCustomData", "m_onlineKey"),
}


@dataclass(frozen=True)
class TypeRef:
    name: str                 # simple type, class name, or template base (qlist, std_map, any...)
    args: tuple = ()
    is_class: bool = False

    def __str__(self):
        return "%s<%s>" % (self.name, ",".join(map(str, self.args))) if self.args else self.name


@dataclass
class ClassDef:
    name: str
    parent: str
    fields: list              # [(name, TypeRef)]


@dataclass
class Param:
    name: str
    type: TypeRef
    direction: int


@dataclass
class MethodDef:
    id: int
    name: str
    params: list = field(default_factory=list)
    ret: TypeRef = None

    @property
    def inputs(self):
        return [p for p in self.params if p.direction in (IN, INOUT)]

    @property
    def outputs(self):
        return [p for p in self.params if p.direction in (OUT, INOUT)]

    def signature(self) -> str:
        args = ", ".join("%s%s %s" % ({IN: "", OUT: "out ", INOUT: "inout "}[p.direction],
                                       p.type, p.name) for p in self.params)
        return "%s(%s)%s" % (self.name, args, (" -> %s" % self.ret) if self.ret else "")


@dataclass
class ProtocolDef:
    name: str
    methods: dict = field(default_factory=dict)   # id -> MethodDef


SIMPLE_TYPES = {
    "bool", "byte", "char", "int8", "uint8", "int16", "uint16", "int32", "uint32", "int64",
    "uint64", "float", "double", "string", "stationurl", "buffer", "qbuffer", "buffertail",
    "datetime", "qresult", "variant",
}
LIST_TYPES = {"qlist", "std_list", "qvector", "std_vector", "list"}


def parse_type(text: str, classes=None) -> TypeRef:
    """'qlist<GameSessionKey>' -> TypeRef. Names that are not simple types are classes."""
    text = text.replace(" ", "")
    if "<" not in text:
        return TypeRef(text, (), text not in SIMPLE_TYPES)
    base, inner = text.split("<", 1)
    inner = inner[:-1]
    args, depth, cur = [], 0, ""
    for ch in inner:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        args.append(cur)
    return TypeRef(base, tuple(parse_type(a, classes) for a in args), False)


def _from_use(use: dict) -> TypeRef:
    if "args" in use:
        base = use["t"].split("<", 1)[0]
        return TypeRef(base, tuple(_from_use(a) for a in use["args"]), False)
    return TypeRef(use["t"], (), use.get("kind") == "ClassDeclaration")


class Schema:
    def __init__(self):
        self.classes = {"Data": ClassDef("Data", "", [])}
        self.protocols = {}

    # ----- loading -------------------------------------------------------------------------
    def load_ddl_json(self, path: str) -> "Schema":
        with open(path, encoding="utf-8") as f:
            blobs = json.load(f)
        for blob in blobs:
            for node in blob["nodes"]:
                if node["k"] == "Class":
                    fields = [(m["name"], _from_use(m["type"])) for m in node["members"]
                              if m["k"] == "Variable"]
                    self.classes[node["name"]] = ClassDef(node["name"], node["parent"], fields)
                elif node["k"] == "Protocol":
                    proto = ProtocolDef(node["name"])
                    mid = 0
                    for child in node["children"]:
                        if child["k"] not in ("RMC", "Action"):
                            continue
                        mid += 1
                        m = MethodDef(mid, child["name"])
                        for p in child["params"]:
                            if p["k"] == "Parameter":
                                m.params.append(Param(p["name"], _from_use(p["type"]), p["dir"]))
                            elif p["k"] == "ReturnValue":
                                m.ret = _from_use(p["type"])
                        proto.methods[mid] = m
                    self.protocols[proto.name] = proto
        return self

    def add_class(self, name: str, fields: list, parent: str = "") -> None:
        """fields: [(name, "type")]"""
        self.classes[name] = ClassDef(name, parent, [(n, parse_type(t)) for n, t in fields])

    def add_protocol(self, name: str, methods: dict) -> None:
        """methods: {id: (method_name, [("in"|"out"|"inout", "type", "name"), ...], ret or None)}"""
        proto = ProtocolDef(name)
        dirs = {"in": IN, "out": OUT, "inout": INOUT}
        for mid, (mname, params, ret) in methods.items():
            m = MethodDef(mid, mname, [Param(n, parse_type(t), dirs[d]) for d, t, n in params],
                          parse_type(ret) if ret else None)
            proto.methods[mid] = m
        self.protocols[name] = proto

    def method(self, protocol: str, mid: int):
        p = self.protocols.get(protocol)
        return p.methods.get(mid) if p else None

    def class_fields(self, name: str) -> list:
        """Fields in wire order: the parent's first."""
        c = self.classes.get(name)
        if c is None:
            raise KeyError("unknown class %s" % name)
        return (self.class_fields(c.parent) if c.parent else []) + list(c.fields)

    # ----- codec ---------------------------------------------------------------------------
    def read(self, r: Reader, t: TypeRef):
        n = t.name
        if t.args:
            if n in LIST_TYPES:
                return [self.read(r, t.args[0]) for _ in range(r.u32())]
            if n in ("std_map", "map"):
                return [(self.read(r, t.args[0]), self.read(r, t.args[1])) for _ in range(r.u32())]
            if n == "any":
                return self.read_any(r)
            raise KeyError("unsupported template %s" % t)
        if t.is_class or n not in SIMPLE_TYPES:
            return self.read_class(r, n)
        return _read_simple(r, n)

    def read_class(self, r: Reader, name: str) -> dict:
        out = {}
        for fname, ftype in self.class_fields(name):
            if (name, fname) in SENSITIVE or self._sensitive_inherited(name, fname):
                r.skip_string()
                continue
            out[fname] = self.read(r, ftype)
        return out

    def _sensitive_inherited(self, name: str, fname: str) -> bool:
        c = self.classes.get(name)
        while c is not None and c.parent:
            if (c.parent, fname) in SENSITIVE:
                return True
            c = self.classes.get(c.parent)
        return False

    def read_any(self, r: Reader) -> dict:
        cls = r.string()
        if not cls:
            return None
        r.u32()                      # inner length + 4
        blob = r.take(r.u32())
        if cls in self.classes:
            value = self.read_class(Reader(blob), cls)
        else:
            value = {"__raw__": blob}
        value["__class__"] = cls
        return value

    def write(self, w: Writer, t: TypeRef, v) -> None:
        n = t.name
        if t.args:
            if n in LIST_TYPES:
                items = list(v or [])
                w.u32(len(items))
                for item in items:
                    self.write(w, t.args[0], item)
                return
            if n in ("std_map", "map"):
                pairs = list(v.items()) if isinstance(v, dict) else list(v or [])
                w.u32(len(pairs))
                for k, item in pairs:
                    self.write(w, t.args[0], k)
                    self.write(w, t.args[1], item)
                return
            if n == "any":
                self.write_any(w, v)
                return
            raise KeyError("unsupported template %s" % t)
        if t.is_class or n not in SIMPLE_TYPES:
            self.write_class(w, n, v or {})
            return
        _write_simple(w, n, v)

    def write_class(self, w: Writer, name: str, v: dict) -> None:
        for fname, ftype in self.class_fields(name):
            self.write(w, ftype, v.get(fname))

    def write_any(self, w: Writer, v) -> None:
        if not v:
            w.string("")
            return
        cls = v["__class__"]
        inner = Writer()
        if "__raw__" in v:
            inner.raw(v["__raw__"])
        else:
            self.write_class(inner, cls, v)
        blob = inner.bytes()
        w.string(cls).u32(len(blob) + 4).u32(len(blob)).raw(blob)

    # ----- methods -------------------------------------------------------------------------
    def decode_request(self, m: MethodDef, data: bytes) -> dict:
        r = Reader(data)
        args = {p.name: self.read(r, p.type) for p in m.inputs}
        if r.remaining():
            args["__trailing__"] = r.rest()
        return args

    def encode_response(self, m: MethodDef, result: dict) -> bytes:
        w = Writer()
        if m.ret is not None:
            self.write(w, m.ret, result.get("retval", _default_ret(m.ret)))
        for p in m.outputs:
            self.write(w, p.type, result.get(p.name))
        return w.bytes()

    def encode_request(self, m: MethodDef, args: dict) -> bytes:
        w = Writer()
        for p in m.inputs:
            self.write(w, p.type, args.get(p.name))
        return w.bytes()

    def decode_response(self, m: MethodDef, data: bytes) -> dict:
        """The other direction, for tools and tests that act as the client."""
        r = Reader(data)
        out = {}
        if m.ret is not None:
            out["retval"] = self.read(r, m.ret)
        for p in m.outputs:
            out[p.name] = self.read(r, p.type)
        if r.remaining():
            out["__trailing__"] = r.rest()
        return out


def _default_ret(t: TypeRef):
    return {"qresult": 0x10001, "bool": True}.get(t.name, 0)


def _read_simple(r: Reader, n: str):
    if n == "bool":
        return r.bool()
    if n in ("byte", "uint8"):
        return r.u8()
    if n in ("char", "int8"):
        return r.i8()
    if n == "uint16":
        return r.u16()
    if n == "int16":
        return r.i16()
    if n in ("uint32", "qresult"):
        return r.u32()
    if n == "int32":
        return r.i32()
    if n in ("uint64", "datetime"):
        return r.u64()
    if n == "int64":
        return r.i64()
    if n == "float":
        return r.f32()
    if n == "double":
        return r.f64()
    if n in ("string", "stationurl"):
        return r.string()
    if n == "buffer":
        return r.buffer()
    if n == "qbuffer":
        return r.qbuffer()
    if n == "buffertail":
        return r.rest()
    if n == "variant":
        kind = r.u8()
        return {0: lambda: None, 1: r.i64, 2: r.f64, 3: r.bool, 4: r.string, 5: r.u64,
                6: r.u64}[kind]()
    raise KeyError(n)


def _write_simple(w: Writer, n: str, v) -> None:
    if n == "bool":
        w.bool(bool(v))
    elif n in ("byte", "uint8"):
        w.u8(v or 0)
    elif n in ("char", "int8"):
        w.i8(v or 0)
    elif n == "uint16":
        w.u16(v or 0)
    elif n == "int16":
        w.i16(v or 0)
    elif n in ("uint32", "qresult"):
        w.u32(v or 0)
    elif n == "int32":
        w.i32(v or 0)
    elif n in ("uint64", "datetime"):
        w.u64(v or 0)
    elif n == "int64":
        w.i64(v or 0)
    elif n == "float":
        w.f32(v or 0.0)
    elif n == "double":
        w.f64(v or 0.0)
    elif n in ("string", "stationurl"):
        w.string(v or "")
    elif n == "buffer":
        w.buffer(v or b"")
    elif n == "qbuffer":
        w.qbuffer(v or b"")
    elif n == "buffertail":
        w.raw(v or b"")
    elif n == "variant":
        if v is None:
            w.u8(0)
        elif isinstance(v, bool):
            w.u8(3).bool(v)
        elif isinstance(v, int):
            w.u8(1).i64(v)
        elif isinstance(v, float):
            w.u8(2).f64(v)
        else:
            w.u8(4).string(str(v))
    else:
        raise KeyError(n)


def describe(value, limit: int = 160) -> str:
    """Short, log-safe rendering of decoded parameters."""
    def fmt(v):
        if isinstance(v, bytes):
            return "<%d bytes>" % len(v)
        if isinstance(v, dict):
            return "{" + ", ".join("%s=%s" % (k, fmt(x)) for k, x in v.items()
                                   if not k.startswith("__") or k == "__class__") + "}"
        if isinstance(v, (list, tuple)):
            inner = ", ".join(fmt(x) for x in v[:8])
            return "[" + inner + (", ...%d" % len(v) if len(v) > 8 else "") + "]"
        return repr(v)
    text = fmt(value)
    return text if len(text) <= limit else text[:limit] + "..."
