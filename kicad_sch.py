"""
Writes a KiCad schematic (.kicad_sch) from a SKiDL circuit.

SKiDL can generate schematics itself, but it places symbols automatically
(and without the Sim.* fields the KiCad simulator needs). This writer takes
the parts/nets from the SKiDL circuit plus a hand-made placement and wire
routing, and copies the complete symbol definitions from the KiCad libraries.
Result: a schematic that looks clean in Eeschema and can be simulated there.
"""

import os
import re
import uuid
from pathlib import Path

SYMBOL_DIR = Path(os.environ.get("KICAD10_SYMBOL_DIR", "/usr/share/kicad/symbols"))


# ---------------------------------------------------------------------------
# Minimal S-expression parser
# ---------------------------------------------------------------------------
_TOKEN = re.compile(r'\s*(?:(\()|(\))|("(?:\\.|[^"\\])*")|([^\s()"]+))', re.S)


def parse(text):
    """Text -> nested lists; atoms stay strings (quotes are preserved)."""
    stack = [[]]
    for m in _TOKEN.finditer(text):
        op, cl, qs, atom = m.groups()
        if op:
            stack.append([])
        elif cl:
            node = stack.pop()
            stack[-1].append(node)
        elif qs is not None:
            stack[-1].append(qs)
        elif atom is not None:
            stack[-1].append(atom)
    return stack[0]


def dump(node, depth=0):
    """Nested lists -> KiCad-style formatted text."""
    ind = "\t" * depth
    if not isinstance(node, list):
        return ind + node
    if all(not isinstance(c, list) for c in node):
        return ind + "(" + " ".join(node) + ")"
    head = [c for c in node if not isinstance(c, list)]
    out = ind + "(" + " ".join(head)
    for c in node:
        if isinstance(c, list):
            out += "\n" + dump(c, depth + 1)
    return out + "\n" + ind + ")"


def q(s):
    return '"' + str(s).replace('"', '\\"') + '"'


def unq(s):
    return s[1:-1] if s.startswith('"') else s


def find_all(node, key):
    return [c for c in node if isinstance(c, list) and c and c[0] == key]


def find(node, key):
    r = find_all(node, key)
    return r[0] if r else None


# ---------------------------------------------------------------------------
# Load library symbols and resolve "extends"
# ---------------------------------------------------------------------------
_lib_cache = {}


def _lib(libname):
    if libname not in _lib_cache:
        text = (SYMBOL_DIR / f"{libname}.kicad_sym").read_text()
        root = parse(text)[0]
        _lib_cache[libname] = {unq(s[1]): s for s in find_all(root, "symbol")}
    return _lib_cache[libname]


def lib_symbol(libname, name):
    """Return the (flattened) symbol with lib_id 'lib:name'."""
    sym = _lib(libname)[name]
    ext = find(sym, "extends")
    if ext:
        parent = lib_symbol(libname, unq(ext[1]))
        parent_name = unq(ext[1])
        merged = [c for c in parent if not (isinstance(c, list) and c[0] == "property")]
        # Properties: child overrides parent
        props = {unq(p[1]): p for p in find_all(parent, "property")}
        for p in find_all(sym, "property"):
            props[unq(p[1])] = p
        # Rename unit symbols (Q_NPN_EBC_0_1 -> 2N3904_0_1)
        result = []
        for c in merged:
            if isinstance(c, list) and c[0] == "symbol":
                c = list(c)
                c[1] = q(unq(c[1]).replace(parent_name, name, 1))
            result.append(c)
        # Insert properties right after the header
        insert_at = next((i for i, c in enumerate(result)
                          if isinstance(c, list) and c[0] == "symbol"), len(result))
        result[insert_at:insert_at] = list(props.values())
        sym = result
    sym = list(sym)
    sym[1] = q(f"{libname}:{name}")
    return sym


def pin_offsets(libname, name):
    """Pin number -> (dx, dy) in schematic coordinates (y down), rotation 0."""
    sym = lib_symbol(libname, name)
    pins = {}

    def walk(node):
        for c in node:
            if isinstance(c, list):
                if c[0] == "pin":
                    at = find(c, "at")
                    num = unq(find(c, "number")[1])
                    pins[num] = (float(at[1]), -float(at[2]))
                else:
                    walk(c)
    walk(sym)
    return pins


def transform(d, rot=0, mirror=None):
    x, y = d
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    if rot == 90:
        x, y = y, -x
    elif rot == 180:
        x, y = -x, -y
    elif rot == 270:
        x, y = -y, x
    return (x, y)


# ---------------------------------------------------------------------------
# Schematic elements
# ---------------------------------------------------------------------------
def U():
    return q(uuid.uuid4())


def _prop(name, val, x, y, hide=False, justify=None):
    eff = ["effects", ["font", ["size", "1.27", "1.27"]]]
    if justify:
        eff.append(["justify", justify])
    p = ["property", q(name), q(val), ["at", f"{x:g}", f"{y:g}", "0"]]
    if hide:
        p.append(["hide", "yes"])
    p.append(eff)
    return p


def symbol_instance(lib_id, ref, value, at, rot=0, mirror=None, fields=None,
                    footprint="", project="", root_uuid="", pins=()):
    x, y = at
    node = ["symbol", ["lib_id", q(lib_id)], ["at", f"{x:g}", f"{y:g}", str(rot)]]
    if mirror:
        node.append(["mirror", mirror])
    node += [["unit", "1"], ["exclude_from_sim", "no"], ["in_bom", "yes"],
             ["on_board", "yes"], ["dnp", "no"], ["uuid", U()]]
    hidden = ref.startswith("#")
    # Texts right of the symbol, left for horizontally mirrored symbols
    tx, just = (x - 3.81, "right") if mirror == "y" else (x + 3.81, "left")
    node.append(_prop("Reference", ref, tx, y - 1.27, hide=hidden, justify=just))
    node.append(_prop("Value", value, tx, y + 1.27, hide=hidden, justify=just))
    node.append(_prop("Footprint", footprint, x, y, hide=True))
    node.append(_prop("Datasheet", "", x, y, hide=True))
    for k, v in (fields or {}).items():
        node.append(_prop(k, v, x, y, hide=True))
    for num in pins:
        node.append(["pin", q(num), ["uuid", U()]])
    node.append(["instances", ["project", q(project),
                 ["path", q("/" + root_uuid), ["reference", q(ref)], ["unit", "1"]]]])
    return node


def wire(a, b):
    return ["wire", ["pts", ["xy", f"{a[0]:g}", f"{a[1]:g}"], ["xy", f"{b[0]:g}", f"{b[1]:g}"]],
            ["stroke", ["width", "0"], ["type", "default"]], ["uuid", U()]]


def junction(p):
    return ["junction", ["at", f"{p[0]:g}", f"{p[1]:g}"], ["diameter", "0"],
            ["color", "0", "0", "0", "0"], ["uuid", U()]]


def label(name, p, rot=0, glob=False):
    node = ["global_label" if glob else "label", q(name)]
    if glob:
        node.append(["shape", "output" if name.upper().startswith("OUT") else "input"])
    node += [["at", f"{p[0]:g}", f"{p[1]:g}", str(rot)],
             ["effects", ["font", ["size", "1.27", "1.27"]],
              ["justify", "left", "bottom"]], ["uuid", U()]]
    return node


def text(txt, p):
    return ["text", q(txt.replace("\n", "\\n")), ["exclude_from_sim", "no"],
            ["at", f"{p[0]:g}", f"{p[1]:g}", "0"],
            ["effects", ["font", ["size", "1.27", "1.27"]], ["justify", "left", "bottom"]],
            ["uuid", U()]]


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------
class Placement:
    """Placement of a part; yields absolute pin coordinates."""

    def __init__(self, lib, name, at, rot=0, mirror=None):
        self.lib, self.name, self.at, self.rot, self.mirror = lib, name, at, rot, mirror
        self._off = pin_offsets(lib, name)

    def pin(self, num):
        dx, dy = transform(self._off[str(num)], self.rot, self.mirror)
        return (round(self.at[0] + dx, 2), round(self.at[1] + dy, 2))


def write_schematic(circuit, path, placements, wires, junctions=(), labels=(),
                    power_symbols=(), texts=(), title="", paper="A4"):
    """
    circuit       : skidl.Circuit
    placements    : {ref: Placement}
    wires         : list of polylines [(x,y), (x,y), ...]
    labels        : [(netname, (x,y), rot, global?)]
    power_symbols : [(lib_name, (x,y), rot)]  e.g. ("GND", (10, 20), 0)
    texts         : [(text, (x,y))]
    """
    path = Path(path)
    project = path.stem
    root_uuid = str(uuid.uuid4())

    lib_defs = {}
    body = []

    for part in circuit.parts:
        pl = placements[part.ref]
        lib_id = f"{pl.lib}:{pl.name}"
        lib_defs.setdefault(lib_id, lib_symbol(pl.lib, pl.name))
        # Sim.* fields: library defaults, overridden by part.fields
        fields = {unq(p[1]): unq(p[2]) for p in find_all(lib_defs[lib_id], "property")
                  if unq(p[1]).startswith("Sim.")}
        fields.update({k: v for k, v in (getattr(part, "fields", {}) or {}).items()
                       if k.startswith("Sim.")})
        pins = sorted(pin_offsets(pl.lib, pl.name), key=lambda s: int(s))
        body.append(symbol_instance(lib_id, part.ref, str(part.value), pl.at, pl.rot,
                                    pl.mirror, fields, getattr(part, "footprint", "") or "",
                                    project, root_uuid, pins))

    pwr_idx = {"#PWR": 0, "#FLG": 0}
    for name, at, rot in power_symbols:
        lib_id = f"power:{name}"
        lib_defs.setdefault(lib_id, lib_symbol("power", name))
        prefix = "#FLG" if name == "PWR_FLAG" else "#PWR"
        pwr_idx[prefix] += 1
        ref = f"{prefix}{pwr_idx[prefix]:02d}"
        body.append(symbol_instance(lib_id, ref, name, at, rot, None, {}, "",
                                    project, root_uuid, ["1"]))

    for poly in wires:
        for a, b in zip(poly, poly[1:]):
            body.append(wire(a, b))
    for p in junctions:
        body.append(junction(p))
    for item in labels:
        name, p = item[0], item[1]
        rot = item[2] if len(item) > 2 else 0
        glob = item[3] if len(item) > 3 else False
        body.append(label(name, p, rot, glob))
    for txt, p in texts:
        body.append(text(txt, p))

    root = ["kicad_sch", ["version", "20250114"], ["generator", q("eeschema")],
            ["generator_version", q("9.0")], ["uuid", q(root_uuid)], ["paper", q(paper)],
            ["title_block", ["title", q(title)], ["comment", "1", q("Generated from SKiDL")]],
            ["lib_symbols"] + list(lib_defs.values())]
    root += body
    root.append(["sheet_instances", ["path", q("/"), ["page", q("1")]]])
    root.append(["embedded_fonts", "no"])
    path.write_text(dump(root) + "\n")

    pro = path.with_suffix(".kicad_pro")
    if not pro.exists():
        pro.write_text('{\n  "meta": { "filename": "%s", "version": 3 },\n'
                       '  "schematic": { "legacy_lib_list": [] }\n}\n' % pro.name)
    return path
