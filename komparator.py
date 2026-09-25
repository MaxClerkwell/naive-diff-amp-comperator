"""
Simple comparator built from discrete transistors (no op-amp).

Topology
--------
* Q1/Q2 (2N3904): NPN differential pair, common emitter node via RE to GND
  (emitter resistor as a simple current source, I_tail ~ 0.4 mA).
* RC1/RC2: collector resistors of the differential pair.
* Q3 (2N3906): PNP output stage, base driven through RB from Q1's collector.
  If V_IN > V_REF, Q1 takes the tail current, its collector drops, Q3 turns
  on and OUT goes to VCC. If V_IN < V_REF, Q1 is off, its collector sits at
  VCC, Q3 is off and RL pulls OUT to GND.
* The sources (VCC, V_REF, V_IN) are Simulation_SPICE symbols, so the
  generated schematic can be simulated directly inside KiCad as well.

Usage:  python komparator.py
  -> komparator.net        (KiCad netlist from SKiDL)
  -> komparator.kicad_sch  (KiCad schematic, placement see LAYOUT below)
  -> komparator.cir        (SPICE netlist derived from the SKiDL circuit)
  Afterwards the SPICE netlist exported from the schematic (kicad-cli) is
  compared against the SKiDL circuit, so schematic and netlist are guaranteed
  to describe the same circuit.
"""

import os
import re
import subprocess
import shutil
from pathlib import Path

os.environ.setdefault("KICAD10_SYMBOL_DIR", "/usr/share/kicad/symbols")

import builtins

from skidl import KICAD10, POWER, Net, Part, generate_netlist, set_default_tool

default_circuit = builtins.default_circuit  # provided by SKiDL as a builtin

from kicad_sch import Placement, write_schematic

set_default_tool(KICAD10)
HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Component values (central, also used by the simulation)
# ---------------------------------------------------------------------------
VCC_V = 5.0
VREF_V = 2.5
RC = "4.7k"
RE = "4.7k"
RB = "10k"
RL = "10k"

SPICE_MODELS = """\
.model 2N3904 NPN(IS=1E-14 VAF=100 BF=300 IKF=0.4 XTB=1.5 BR=4 CJC=4E-12 CJE=8E-12 RB=20 RC=0.1 RE=0.1 TR=250E-9 TF=350E-12 ITF=1 VTF=2 XTF=3 VJE=0.75 VJC=0.75)
.model 2N3906 PNP(IS=1E-14 VAF=100 BF=200 IKF=0.4 XTB=1.5 BR=4 CJC=4.5E-12 CJE=10E-12 RB=20 RC=0.1 RE=0.1 TR=250E-9 TF=350E-12 ITF=1 VTF=2 XTF=3 VJE=0.75 VJC=0.75)"""


def model_params(name):
    """Parameter string of a .model entry in SPICE_MODELS (for KiCad Sim.Params)."""
    m = re.search(r"\.model %s \w+\((.*)\)" % re.escape(name), SPICE_MODELS)
    return m.group(1)


FP_R = "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal"
FP_TO92 = "Package_TO_SOT_THT:TO-92_Inline"


def build_comparator():
    """Build the circuit in SKiDL's default circuit and return the nets."""
    vcc, gnd = Net("VCC"), Net("GND")
    vcc.drive = gnd.drive = POWER
    v_in, v_ref, out = Net("V_IN"), Net("V_REF"), Net("OUT")
    c1, c2, tail = Net("C1"), Net("C2"), Net("TAIL")

    # --- Differential pair ------------------------------------------------
    q1 = Part("Transistor_BJT", "2N3904", ref="Q1", footprint=FP_TO92)
    q2 = Part("Transistor_BJT", "2N3904", ref="Q2", footprint=FP_TO92)
    # KiCad simulator: Gummel-Poon model with the parameters from SPICE_MODELS
    for q, dev in ((q1, "NPN"), (q2, "NPN")):
        q.fields.update({"Sim.Device": dev, "Sim.Type": "GUMMELPOON",
                         "Sim.Pins": "1=E 2=B 3=C", "Sim.Params": model_params(q.name)})
    rc1 = Part("Device", "R", ref="RC1", value=RC, footprint=FP_R)
    rc2 = Part("Device", "R", ref="RC2", value=RC, footprint=FP_R)
    re_ = Part("Device", "R", ref="RE", value=RE, footprint=FP_R)

    q1["B"] += v_in
    q2["B"] += v_ref
    q1["E"] += tail
    q2["E"] += tail
    re_[1, 2] += tail, gnd
    q1["C"] += c1
    q2["C"] += c2
    rc1[1, 2] += vcc, c1
    rc2[1, 2] += vcc, c2

    # --- PNP output stage -------------------------------------------------
    q3 = Part("Transistor_BJT", "2N3906", ref="Q3", footprint=FP_TO92)
    q3.fields.update({"Sim.Device": "PNP", "Sim.Type": "GUMMELPOON",
                      "Sim.Pins": "1=E 2=B 3=C", "Sim.Params": model_params(q3.name)})
    rb = Part("Device", "R", ref="RB", value=RB, footprint=FP_R)
    rl = Part("Device", "R", ref="RL", value=RL, footprint=FP_R)
    rb[1, 2] += c1, q3["B"]
    q3["E"] += vcc
    q3["C"] += out
    rl[1, 2] += out, gnd

    # --- Sources (SPICE symbols) -------------------------------------------
    v1 = Part("Simulation_SPICE", "VDC", ref="V1", value=f"{VCC_V:g}")
    v1.fields["Sim.Params"] = f"dc={VCC_V:g}"
    v1[1, 2] += vcc, gnd

    vref = Part("Simulation_SPICE", "VDC", ref="VREF", value=f"{VREF_V:g}")
    vref.fields["Sim.Params"] = f"dc={VREF_V:g}"
    vref[1, 2] += v_ref, gnd

    vin = Part("Simulation_SPICE", "VSIN", ref="VIN", value="VSIN")
    vin.fields["Sim.Params"] = f"dc={VREF_V:g} ampl=2 f=1k"
    vin[1, 2] += v_in, gnd

    # Set the footprint field, otherwise SKiDL complains on netlist export
    for src in (v1, vref, vin):
        src.footprint = ""

    return dict(vcc=vcc, gnd=gnd, v_in=v_in, v_ref=v_ref, out=out, c1=c1, c2=c2, tail=tail)


# ---------------------------------------------------------------------------
# Derive the SPICE netlist directly from the SKiDL circuit
# ---------------------------------------------------------------------------
def _netname(pin):
    n = pin.net.name if pin.net else "NC"
    return "0" if n == "GND" else n.lower()


def _sim_params(part):
    p = part.fields.get("Sim.Params", "")
    return dict(kv.split("=", 1) for kv in p.split())


def spice_netlist(circuit=None, title="transistor-komparator", sources=None):
    """
    Generate the ngspice netlist. `sources` allows overriding the source
    definitions, e.g. {"VIN": "dc 2.5", "VREF": "dc 1.5"}.
    """
    circuit = circuit or default_circuit
    sources = sources or {}
    lines = [title]
    for part in sorted(circuit.parts, key=lambda p: p.ref):
        ref = part.ref
        if part.name == "R":
            lines.append(f"{ref} {_netname(part[1])} {_netname(part[2])} {part.value}")
        elif part.name in ("2N3904", "2N3906"):
            lines.append(f"{ref} {_netname(part['C'])} {_netname(part['B'])} "
                         f"{_netname(part['E'])} {part.name}")
        elif part.name in ("VDC", "VSIN"):
            if ref in sources:
                spec = sources[ref]
            else:
                p = _sim_params(part)
                spec = f"dc {p.get('dc', 0)}"
                if part.name == "VSIN":
                    spec += f" sin({p.get('dc', 0)} {p.get('ampl', 1)} {p.get('f', '1k')})"
            lines.append(f"{ref} {_netname(part[1])} {_netname(part[2])} {spec}")
        else:
            raise ValueError(f"No SPICE template for {part.name}")
    lines += SPICE_MODELS.splitlines()
    lines.append(".end")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Schematic layout (mm, KiCad grid 1.27 mm; y grows downwards)
# ---------------------------------------------------------------------------
LAYOUT = {
    "Q1":   Placement("Transistor_BJT", "2N3904", (99.06, 78.74)),
    "Q2":   Placement("Transistor_BJT", "2N3904", (124.46, 78.74), mirror="y"),
    "Q3":   Placement("Transistor_BJT", "2N3906", (162.56, 78.74), mirror="x"),
    "RC1":  Placement("Device", "R", (101.6, 63.5)),
    "RC2":  Placement("Device", "R", (121.92, 63.5)),
    "RE":   Placement("Device", "R", (111.76, 96.52)),
    "RB":   Placement("Device", "R", (149.86, 78.74), rot=90),
    "RL":   Placement("Device", "R", (165.1, 96.52)),
    "V1":   Placement("Simulation_SPICE", "VDC", (68.58, 58.42)),
    "VREF": Placement("Simulation_SPICE", "VDC", (134.62, 88.9)),
    "VIN":  Placement("Simulation_SPICE", "VSIN", (76.2, 86.36)),
}
P = LAYOUT

WIRES = [
    # VCC rail
    [P["RC1"].pin(1), (101.6, 50.8), (165.1, 50.8), P["Q3"].pin(1)],
    [P["RC2"].pin(1), (121.92, 50.8)],
    [P["V1"].pin(1), (68.58, 50.8)], [(63.5, 50.8), (68.58, 50.8)],
    # supply GND
    [P["V1"].pin(2), (68.58, 66.04)], [(63.5, 66.04), (68.58, 66.04)],
    # C1: collector Q1 -> RC1 and detour over the top to RB
    [P["RC1"].pin(2), P["Q1"].pin(3)],
    [(101.6, 71.12), (88.9, 71.12), (88.9, 38.1), (146.05, 38.1), P["RB"].pin(1)],
    # C2
    [P["RC2"].pin(2), P["Q2"].pin(3)],
    # TAIL
    [P["Q1"].pin(1), (101.6, 88.9), (121.92, 88.9), P["Q2"].pin(1)],
    [(111.76, 88.9), P["RE"].pin(1)],
    [P["RE"].pin(2), (111.76, 102.87)],
    # inputs
    [P["Q1"].pin(2), (76.2, 78.74), P["VIN"].pin(1)],
    [P["VIN"].pin(2), (76.2, 93.98)],
    [P["Q2"].pin(2), (134.62, 78.74), P["VREF"].pin(1)],
    [P["VREF"].pin(2), (134.62, 96.52)],
    # output stage
    [P["RB"].pin(2), P["Q3"].pin(2)],
    [P["Q3"].pin(3), P["RL"].pin(1)],
    [(165.1, 88.9), (177.8, 88.9)],
    [P["RL"].pin(2), (165.1, 102.87)],
]

JUNCTIONS = [(111.76, 50.8), (121.92, 50.8), (68.58, 50.8), (68.58, 66.04),
             (101.6, 71.12), (111.76, 88.9), (165.1, 88.9)]

LABELS = [
    ("C1", (110.49, 38.1)), ("C2", (121.92, 69.85)), ("TAIL", (104.14, 88.9)),
    ("V_IN", (81.28, 78.74)), ("V_REF", (130.81, 78.74)),
    ("OUT", (177.8, 88.9), 0, True),
]

POWER_SYMBOLS = [
    ("VCC", (111.76, 50.8), 0), ("VCC", (68.58, 50.8), 0), ("PWR_FLAG", (63.5, 50.8), 0),
    ("GND", (68.58, 66.04), 0), ("PWR_FLAG", (63.5, 66.04), 0),
    ("GND", (111.76, 102.87), 0), ("GND", (76.2, 93.98), 0),
    ("GND", (134.62, 96.52), 0), ("GND", (165.1, 102.87), 0),
]

TEXTS = [
    (".tran 1u 3m", (63.5, 125.73)),
    ("Comparator built from discrete transistors:\n"
     "Q1/Q2 differential pair with emitter resistor RE, Q3 PNP output stage.\n"
     "OUT = VCC if V_IN > V_REF, otherwise 0 V.", (63.5, 30.48)),
]


# ---------------------------------------------------------------------------
# Consistency check: schematic (via kicad-cli) against the SKiDL circuit
# ---------------------------------------------------------------------------
_SPICE_PIN_ORDER = {"R": ["1", "2"], "Q": ["3", "2", "1"], "V": ["1", "2"]}


def _connectivity(lines):
    """List of SPICE lines -> set of pin groups {frozenset((ref,pin),...)}."""
    groups = {}
    for l in lines:
        tok = l.split()
        if not tok or tok[0][0].upper() not in _SPICE_PIN_ORDER:
            continue
        pins = _SPICE_PIN_ORDER[tok[0][0].upper()]
        for net, pin in zip(tok[1:1 + len(pins)], pins):
            groups.setdefault(net.lower(), set()).add((tok[0].upper(), pin))
    return {frozenset(g) for g in groups.values()}


def verify_schematic(sch_path):
    if not shutil.which("kicad-cli"):
        print("kicad-cli not found - consistency check skipped.")
        return None
    out = HERE / "build" / "kicad_export.cir"
    out.parent.mkdir(exist_ok=True)
    subprocess.run(["kicad-cli", "sch", "export", "netlist", "--format", "spice",
                    "-o", str(out), str(sch_path)], check=True, capture_output=True)
    kicad = _connectivity(out.read_text().splitlines())
    skidl = _connectivity(spice_netlist().splitlines())
    ok = kicad == skidl
    print("Schematic <-> SKiDL netlist:", "IDENTICAL" if ok else "MISMATCH!")
    if not ok:
        for g in kicad - skidl:
            print("  only in schematic:", sorted(g))
        for g in skidl - kicad:
            print("  only in SKiDL:    ", sorted(g))
    return ok


if __name__ == "__main__":
    build_comparator()
    generate_netlist(file_=str(HERE / "komparator.net"))
    (HERE / "komparator.cir").write_text(spice_netlist())
    sch = write_schematic(default_circuit, HERE / "komparator.kicad_sch", LAYOUT, WIRES,
                          JUNCTIONS, LABELS, POWER_SYMBOLS, TEXTS,
                          title="Transistor comparator (differential pair + PNP stage)")
    print("written:", sch.name, "komparator.net", "komparator.cir")
    verify_schematic(sch)
