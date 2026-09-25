#!/usr/bin/env python3
"""
SPICE simulation of the transistor comparator.

The KiCad schematic given on the command line is exported to a SPICE netlist
with `kicad-cli` and simulated with libngspice (ngspice.py), the same library
KiCad uses internally. Supply, reference and tail resistor values are read
from that netlist, so the simulation always reflects the schematic as drawn.
Results: a table on the console + results/summary.md, raw CSV data and
Matplotlib figures in results/.

Analyses
  1. DC sweep V_IN 0..5 V at V_REF = 2.5 V   -> transfer characteristic
  2. DC sweeps for several V_REF             -> does the threshold track V_REF?
  3. Transient: 1 kHz sine, +/-2 V around 2.5 V -> square wave at the output
  4. Transient: pulse with 200 mV overdrive  -> propagation delay, edge times

Usage: uv run simulate.py komparator.kicad_sch [--show] [--results DIR]
  --show  opens the four individual figures tiled across the screen plus a
          combined 2x2 overview figure.
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import matplotlib

from ngspice import NgSpice

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

# Circuit values, filled from the schematic netlist in main()
VCC, VREF = 5.0, 2.5

# Colors: categorical palette (blue, orange, aqua, yellow)
C_IN, C_OUT, C_C1, C_C2 = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
GRID = dict(color="#d9d8d3", linewidth=0.6)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def crossings(x, y, level, rising=True):
    """All points where y crosses `level` in the given direction (linearly interpolated)."""
    s = np.sign(y - level)
    if rising:
        idx = np.where((s[:-1] < 0) & (s[1:] >= 0))[0]
    else:
        idx = np.where((s[:-1] > 0) & (s[1:] <= 0))[0]
    return np.array([x[i] + (level - y[i]) * (x[i + 1] - x[i]) / (y[i + 1] - y[i]) for i in idx])


def crossing(x, y, level, rising=True):
    """First crossing, or NaN if there is none."""
    c = crossings(x, y, level, rising)
    return c[0] if len(c) else np.nan


def si(v, unit=""):
    """Format a value with an SI prefix, e.g. 1.6e-7 s -> '160 ns'."""
    if np.isnan(v):
        return "n/a"
    for f, p in ((1e-9, "n"), (1e-6, "µ"), (1e-3, "m"), (1, ""), (1e3, "k")):
        if abs(v) < f * 1000:
            return f"{v / f:.3g} {p}{unit}"
    return f"{v:.3g} {unit}"


def table(rows, header):
    """Simple Markdown table as a string."""
    rows = [[str(c) for c in r] for r in rows]
    w = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(header)]
    line = lambda r: "| " + " | ".join(c.ljust(w[i]) for i, c in enumerate(r)) + " |"
    return "\n".join([line(header), "|" + "|".join("-" * (x + 2) for x in w) + "|"]
                     + [line(r) for r in rows])


def style(ax, xlabel, ylabel, title):
    ax.set_title(title, loc="left", fontsize=11, fontweight="bold")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.grid(True, **GRID)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.spines["left"].set_color("#a8a7a1")
    ax.spines["bottom"].set_color("#a8a7a1")


# ---------------------------------------------------------------------------
# Netlist from the KiCad schematic
# ---------------------------------------------------------------------------
_SI = {"t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3, "m": 1e-3, "u": 1e-6, "µ": 1e-6,
       "n": 1e-9, "p": 1e-12, "f": 1e-15}


def parse_value(tok):
    """SPICE number with SI suffix ('4.7k', '2.5', '10u') -> float."""
    m = re.match(r"([-+]?[\d.]+(?:e[-+]?\d+)?)\s*(meg|[tgkmunpfµ])?", tok.lower())
    if not m:
        raise ValueError(f"cannot parse value {tok!r}")
    return float(m.group(1)) * _SI.get(m.group(2) or "", 1.0)


class SchematicNetlist:
    """
    SPICE netlist exported from a .kicad_sch via kicad-cli.

    Net names are normalised (GND -> 0, leading '/' stripped, lower case) so
    that vectors can be addressed as v(out), v(v_in), v(c1), ... The voltage
    sources driving V_IN and V_REF are located by net name, so their reference
    designators in the schematic do not matter.
    """

    def __init__(self, sch_path):
        self.path = Path(sch_path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        if not shutil.which("kicad-cli"):
            raise RuntimeError("kicad-cli not found; it is needed to export the netlist")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "netlist.cir"
            subprocess.run(["kicad-cli", "sch", "export", "netlist", "--format", "spice",
                            "-o", str(out), str(self.path)], check=True, capture_output=True)
            raw = out.read_text().splitlines()

        # join "+" continuation lines, drop title/analysis directives
        lines = []
        for l in raw:
            if l.startswith("+"):
                lines[-1] += " " + l[1:].strip()
            elif l.strip():
                lines.append(l.rstrip())
        skip = (".title", ".tran", ".dc", ".ac", ".op", ".end", ".save", ".probe")
        lines = [l for l in lines if not l.lower().startswith(skip)]

        self.lines, self.sources = [], {}
        for l in lines:
            tok = l.split()
            if tok[0][0] in "RQVCL":
                n = 3 if tok[0][0] == "Q" else 2
                tok[1:1 + n] = [self._net(t) for t in tok[1:1 + n]]
                l = " ".join(tok)
                if tok[0][0] == "V":
                    self.sources[tok[1]] = tok[0]          # positive net -> source name
            self.lines.append(l)

        self.vin_src = self.sources["v_in"]
        self.vref_src = self.sources["v_ref"]
        self.vcc = self._dc_value(self.sources["vcc"])
        self.vref = self._dc_value(self.vref_src)
        self.re = self._resistor_between("tail", "0")

    @staticmethod
    def _net(name):
        n = name.lstrip("/").lower()
        return "0" if n in ("gnd", "0") else n

    def _line(self, ref):
        return next(l for l in self.lines if l.split()[0].lower() == ref.lower())

    def _dc_value(self, ref):
        tok = self._line(ref).split()
        return parse_value(tok[tok.index("dc") + 1] if "dc" in tok
                           else tok[tok.index("DC") + 1])

    def _resistor_between(self, a, b):
        for l in self.lines:
            tok = l.split()
            if tok[0][0] == "R" and {tok[1], tok[2]} == {a, b}:
                return parse_value(tok[3])
        raise ValueError(f"no resistor between {a} and {b}")

    def render(self, sources=None, title="transistor comparator"):
        """Netlist text; `sources` maps source ref -> new spec, e.g. {'VIN': 'dc 0'}."""
        sources = {k.lower(): v for k, v in (sources or {}).items()}
        out = [title]
        for l in self.lines:
            ref = l.split()[0]
            if ref.lower() in sources:
                tok = l.split()
                l = f"{ref} {tok[1]} {tok[2]} {sources[ref.lower()]}"
            out.append(l)
        out.append(".end")
        return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Analyses
# ---------------------------------------------------------------------------
def run_dc(ng, nl, vref, step=0.002):
    ng.command("destroy all")
    ng.load_netlist(nl.render(sources={nl.vin_src: "dc 0", nl.vref_src: f"dc {vref}"}))
    ng.run(f"dc {nl.vin_src} 0 {nl.vcc} {step}")
    return {n: ng.vector(n) for n in ("v(v_in)", "v(out)", "v(c1)", "v(c2)", "v(tail)")}


def analyze_dc(d, vref, vcc):
    vin, out = d["v(v_in)"], d["v(out)"]
    vth = crossing(vin, out, vcc / 2)
    v10 = crossing(vin, out, 0.1 * vcc)
    v90 = crossing(vin, out, 0.9 * vcc)
    gain = np.max(np.gradient(out, vin))
    return dict(vref=vref, vth=vth, offset=vth - vref, width=v90 - v10, gain=gain,
                vlow=out[0], vhigh=out[-1])


def run_tran_sine(ng, nl):
    ng.command("destroy all")
    ng.load_netlist(nl.render(sources={nl.vin_src: f"dc {nl.vref} sin({nl.vref} 2 1k)"}))
    ng.run("tran 1u 3m")
    return {n: ng.vector(n) for n in ("time", "v(v_in)", "v(out)", "v(c1)", "v(c2)")}


def run_tran_pulse(ng, nl, overdrive=0.2):
    lo, hi = nl.vref - overdrive, nl.vref + overdrive
    ng.command("destroy all")
    ng.load_netlist(nl.render(
        sources={nl.vin_src: f"dc {lo} pulse({lo} {hi} 2u 10n 10n 10u 20u)"}))
    ng.command("option interp")
    ng.run("tran 5n 40u")
    return {n: ng.vector(n) for n in ("time", "v(v_in)", "v(out)")}


def analyze_pulse(d, vref, vcc):
    t, vin, out = d["time"], d["v(v_in)"], d["v(out)"]
    half = vcc / 2
    t_in_r = crossing(t, vin, vref, True)
    t_in_f = crossing(t, vin, vref, False)
    t_out_r = crossing(t, out, half, True)
    t_out_f = crossing(t, out, half, False)
    tr = crossing(t, out, 0.9 * vcc, True) - crossing(t, out, 0.1 * vcc, True)
    tf = crossing(t, out, 0.1 * vcc, False) - crossing(t, out, 0.9 * vcc, False)
    return dict(tpd_lh=t_out_r - t_in_r, tpd_hl=t_out_f - t_in_f, tr=tr, tf=tf,
                vhigh=np.max(out), vlow=np.min(out[t > 5e-6]))


# ---------------------------------------------------------------------------
# Plots (each draws into a given Axes so they can be reused for the overview)
# ---------------------------------------------------------------------------
def draw_dc(ax, d, res):
    vin = d["v(v_in)"]
    ax.plot(vin, d["v(out)"], color=C_OUT, lw=2, label="OUT")
    ax.plot(vin, d["v(c1)"], color=C_C1, lw=1.5, label="C1 (collector Q1)")
    ax.plot(vin, d["v(c2)"], color=C_C2, lw=1.5, label="C2 (collector Q2)")
    ax.axvline(VREF, color="#a8a7a1", ls="--", lw=1)
    ax.annotate(f"V_REF = {VREF:g} V\nthreshold {res['vth']:.3f} V",
                xy=(res["vth"], VCC / 2), xytext=(res["vth"] + 0.4, VCC / 2),
                fontsize=9, color="#52514e")
    style(ax, "V_IN [V]", "Voltage [V]", "DC transfer characteristic")
    ax.legend(frameon=False)


def draw_dc_family(ax, runs):
    cols = [C_IN, C_OUT, C_C1, C_C2]
    for (vref, d), c in zip(runs, cols):
        ax.plot(d["v(v_in)"], d["v(out)"], color=c, lw=2, label=f"V_REF = {vref:g} V")
        ax.axvline(vref, color=c, ls=":", lw=1)
    style(ax, "V_IN [V]", "OUT [V]", "Switching threshold tracks V_REF")
    ax.legend(frameon=False)


def draw_tran_top(ax, d):
    t = d["time"] * 1e3
    ax.plot(t, d["v(v_in)"], color=C_IN, lw=2, label="V_IN")
    ax.axhline(VREF, color="#a8a7a1", ls="--", lw=1)
    ax.plot(t, d["v(out)"], color=C_OUT, lw=2, label="OUT")
    style(ax, "Time [ms]", "Voltage [V]", "Transient: 1 kHz sine at the input")
    ax.legend(frameon=False, loc="upper right")


def draw_tran_bottom(ax, d):
    t = d["time"] * 1e3
    ax.plot(t, d["v(c1)"], color=C_C1, lw=1.5, label="C1")
    ax.plot(t, d["v(c2)"], color=C_C2, lw=1.5, label="C2")
    style(ax, "Time [ms]", "Voltage [V]", "Collector nodes of the differential pair")
    ax.legend(frameon=False, loc="upper right")


def draw_pulse(ax, d, res):
    t = d["time"] * 1e6
    ax.plot(t, d["v(v_in)"], color=C_IN, lw=2, label="V_IN")
    ax.plot(t, d["v(out)"], color=C_OUT, lw=2, label="OUT")
    ax.axhline(VREF, color="#a8a7a1", ls="--", lw=1)
    style(ax, "Time [µs]", "Voltage [V]",
          f"Pulse response: t_pd(LH) {si(res['tpd_lh'], 's')}, t_pd(HL) {si(res['tpd_hl'], 's')}")
    ax.legend(frameon=False, loc="center right")


def make_figures(dc, dc_res, family, tr, pu, pu_res):
    """Four individual figures + one combined overview; all saved to results/."""
    import matplotlib.pyplot as plt
    figs = []

    fig, ax = plt.subplots(figsize=(8, 4.5), num="DC transfer")
    draw_dc(ax, dc, dc_res)
    figs.append((fig, "dc_transfer.png"))

    fig, ax = plt.subplots(figsize=(8, 4.5), num="Threshold vs V_REF")
    draw_dc_family(ax, family)
    figs.append((fig, "dc_vref_family.png"))

    fig, axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True, num="Transient sine")
    draw_tran_top(axes[0], tr)
    draw_tran_bottom(axes[1], tr)
    axes[0].set_xlabel("")
    figs.append((fig, "tran_sine.png"))

    fig, ax = plt.subplots(figsize=(8, 4.5), num="Pulse response")
    draw_pulse(ax, pu, pu_res)
    figs.append((fig, "tran_pulse.png"))

    # Combined overview: everything on one large canvas (2 x 2)
    fig, axes = plt.subplots(2, 2, figsize=(18, 10), num="Comparator overview")
    draw_dc(axes[0, 0], dc, dc_res)
    draw_dc_family(axes[0, 1], family)
    draw_tran_top(axes[1, 0], tr)
    draw_pulse(axes[1, 1], pu, pu_res)
    fig.suptitle("Transistor comparator - simulation overview", fontsize=14, fontweight="bold")
    figs.append((fig, "overview.png"))

    for fig, name in figs:
        fig.tight_layout()
        fig.savefig(RESULTS / name, dpi=150)
    return figs


def tile_windows(figs):
    """Arrange the figure windows in a grid across the screen (TkAgg/QtAgg)."""
    import matplotlib.pyplot as plt
    try:
        mgr = figs[0][0].canvas.manager
        win = mgr.window
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()   # Tk
    except Exception:
        return
    cols = 3
    n = len(figs)
    rows = -(-n // cols)
    w, h = sw // cols, sh // rows
    for i, (fig, _) in enumerate(figs):
        x, y = (i % cols) * w, (i // cols) * h
        try:
            fig.canvas.manager.window.wm_geometry(f"{w}x{h - 40}+{x}+{y}")
        except Exception:
            pass


def save_csv(path, d):
    keys = list(d)
    np.savetxt(path, np.column_stack([d[k] for k in keys]), delimiter=",",
               header=",".join(keys), comments="")


# ---------------------------------------------------------------------------
def main():
    global VCC, VREF, RESULTS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("schematic", type=Path, help="KiCad schematic (.kicad_sch) to simulate")
    ap.add_argument("--show", action="store_true", help="show figures interactively")
    ap.add_argument("--results", type=Path, default=RESULTS, help="output directory")
    args = ap.parse_args()
    if not args.show:
        matplotlib.use("Agg")
    RESULTS = args.results
    RESULTS.mkdir(exist_ok=True)

    nl = SchematicNetlist(args.schematic)
    VCC, VREF = nl.vcc, nl.vref
    (RESULTS / "netlist.cir").write_text(nl.render())
    print(f"Schematic: {args.schematic}  (VCC = {VCC:g} V, V_REF = {VREF:g} V, "
          f"RE = {nl.re:g} Ohm, sources {nl.vin_src}/{nl.vref_src})\n")
    ng = NgSpice()

    # 1. DC transfer characteristic
    dc = run_dc(ng, nl, VREF)
    dc_res = analyze_dc(dc, VREF, VCC)
    save_csv(RESULTS / "dc_transfer.csv", dc)

    # 2. Threshold vs. V_REF
    family, fam_rows = [], []
    for vref in sorted({1.0, VREF, 4.0}):
        d = run_dc(ng, nl, vref)
        family.append((vref, d))
        r = analyze_dc(d, vref, VCC)
        fam_rows.append([f"{vref:.2f}", f"{r['vth']:.3f}", f"{r['offset'] * 1e3:+.1f}",
                         f"{r['width'] * 1e3:.1f}", f"{r['gain']:.0f}",
                         f"{r['vlow']:.3f}", f"{r['vhigh']:.3f}"])

    # 3. Transient sine
    tr = run_tran_sine(ng, nl)
    save_csv(RESULTS / "tran_sine.csv", tr)
    t_r = crossings(tr["time"], tr["v(out)"], VCC / 2, True)
    t_f = crossings(tr["time"], tr["v(out)"], VCC / 2, False)
    in_r = crossings(tr["time"], tr["v(v_in)"], VREF, True)
    in_f = crossings(tr["time"], tr["v(v_in)"], VREF, False)
    n = min(len(t_r), len(in_r))
    m = min(len(t_f), len(in_f))
    sine_res = dict(delay_r=np.mean(t_r[:n] - in_r[:n]) if n else np.nan,
                    delay_f=np.mean(t_f[:m] - in_f[:m]) if m else np.nan,
                    vhigh=np.max(tr["v(out)"]), vlow=np.min(tr["v(out)"]),
                    duty=np.mean(tr["v(out)"] > VCC / 2))

    # 4. Pulse
    pu = run_tran_pulse(ng, nl)
    save_csv(RESULTS / "tran_pulse.csv", pu)
    pu_res = analyze_pulse(pu, VREF, VCC)

    # ---- Table ------------------------------------------------------------
    i_tail = dc["v(tail)"][np.argmin(abs(dc["v(v_in)"] - VREF))] / nl.re
    op_rows = [
        ["Supply VCC", f"{VCC:g} V"],
        ["Reference V_REF", f"{VREF:g} V"],
        ["Switching threshold (OUT = VCC/2)", f"{dc_res['vth']:.4f} V"],
        ["Offset threshold - V_REF", f"{dc_res['offset'] * 1e3:+.1f} mV"],
        ["Transition width (10 % -> 90 % OUT)", f"{dc_res['width'] * 1e3:.1f} mV"],
        ["Max. DC gain dOUT/dV_IN", f"{dc_res['gain']:.0f} V/V"],
        ["OUT low (V_IN = 0 V)", f"{dc_res['vlow'] * 1e3:.2f} mV"],
        ["OUT high (V_IN = 5 V)", f"{dc_res['vhigh']:.3f} V"],
        ["Tail current at V_IN = V_REF", si(i_tail, "A")],
        ["Propagation delay LH (pulse, 200 mV overdrive)", si(pu_res["tpd_lh"], "s")],
        ["Propagation delay HL (pulse, 200 mV overdrive)", si(pu_res["tpd_hl"], "s")],
        ["Rise time OUT 10-90 %", si(pu_res["tr"], "s")],
        ["Fall time OUT 90-10 %", si(pu_res["tf"], "s")],
        ["1 kHz sine: delay rising / falling",
         f"{si(sine_res['delay_r'], 's')} / {si(sine_res['delay_f'], 's')}"],
        ["1 kHz sine: OUT duty cycle", f"{sine_res['duty'] * 100:.1f} %"],
    ]
    t1 = table(op_rows, ["Parameter", "Value"])
    t2 = table(fam_rows, ["V_REF [V]", "Threshold [V]", "Offset [mV]", "Width [mV]",
                          "Gain [V/V]", "OUT low [V]", "OUT high [V]"])
    report = (f"# Transistor comparator - simulation results\n\nSchematic: `{args.schematic.name}`\n\n"
              "## Characteristics\n\n" + t1 + "\n\n## Threshold as a function of V_REF\n\n" + t2 + "\n")
    (RESULTS / "summary.md").write_text(report)
    print(report)

    # ---- Figures ----------------------------------------------------------
    figs = make_figures(dc, dc_res, family, tr, pu, pu_res)
    print("Figures and CSV data written to", RESULTS)
    if args.show:
        import matplotlib.pyplot as plt
        tile_windows(figs)
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
