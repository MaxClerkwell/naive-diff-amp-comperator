#!/usr/bin/env python3
"""
SPICE simulation of the transistor comparator.

The netlist is derived directly from the SKiDL circuit (komparator.py) and
simulated with libngspice (ngspice.py), the same library KiCad uses
internally. Results: a table on the console + results/summary.md, raw CSV
data and Matplotlib figures in results/.

Analyses
  1. DC sweep V_IN 0..5 V at V_REF = 2.5 V   -> transfer characteristic
  2. DC sweeps for several V_REF             -> does the threshold track V_REF?
  3. Transient: 1 kHz sine, +/-2 V around 2.5 V -> square wave at the output
  4. Transient: pulse with 200 mV overdrive  -> propagation delay, edge times

Usage: python simulate.py [--show]
  --show  opens the four individual figures tiled across the screen plus a
          combined 2x2 overview figure.
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import matplotlib

import komparator as K
from ngspice import NgSpice

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

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
# Analyses
# ---------------------------------------------------------------------------
def run_dc(ng, vref, step=0.002):
    ng.command("destroy all")
    ng.load_netlist(K.spice_netlist(sources={"VIN": "dc 0", "VREF": f"dc {vref}"}))
    ng.run(f"dc vin 0 {K.VCC_V} {step}")
    return {n: ng.vector(n) for n in ("v(v_in)", "v(out)", "v(c1)", "v(c2)", "v(tail)")}


def analyze_dc(d, vref):
    vin, out = d["v(v_in)"], d["v(out)"]
    vth = crossing(vin, out, K.VCC_V / 2)
    v10 = crossing(vin, out, 0.1 * K.VCC_V)
    v90 = crossing(vin, out, 0.9 * K.VCC_V)
    gain = np.max(np.gradient(out, vin))
    return dict(vref=vref, vth=vth, offset=vth - vref, width=v90 - v10, gain=gain,
                vlow=out[0], vhigh=out[-1])


def run_tran_sine(ng):
    ng.command("destroy all")
    ng.load_netlist(K.spice_netlist(sources={"VIN": f"dc {K.VREF_V} sin({K.VREF_V} 2 1k)"}))
    ng.run("tran 1u 3m")
    return {n: ng.vector(n) for n in ("time", "v(v_in)", "v(out)", "v(c1)", "v(c2)")}


def run_tran_pulse(ng, overdrive=0.2):
    lo, hi = K.VREF_V - overdrive, K.VREF_V + overdrive
    ng.command("destroy all")
    ng.load_netlist(K.spice_netlist(
        sources={"VIN": f"dc {lo} pulse({lo} {hi} 2u 10n 10n 10u 20u)"}))
    ng.command("option interp")
    ng.run("tran 5n 40u")
    return {n: ng.vector(n) for n in ("time", "v(v_in)", "v(out)")}


def analyze_pulse(d):
    t, vin, out = d["time"], d["v(v_in)"], d["v(out)"]
    half = K.VCC_V / 2
    t_in_r = crossing(t, vin, K.VREF_V, True)
    t_in_f = crossing(t, vin, K.VREF_V, False)
    t_out_r = crossing(t, out, half, True)
    t_out_f = crossing(t, out, half, False)
    tr = crossing(t, out, 0.9 * K.VCC_V, True) - crossing(t, out, 0.1 * K.VCC_V, True)
    tf = crossing(t, out, 0.1 * K.VCC_V, False) - crossing(t, out, 0.9 * K.VCC_V, False)
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
    ax.axvline(K.VREF_V, color="#a8a7a1", ls="--", lw=1)
    ax.annotate(f"V_REF = {K.VREF_V:g} V\nthreshold {res['vth']:.3f} V",
                xy=(res["vth"], K.VCC_V / 2), xytext=(res["vth"] + 0.4, K.VCC_V / 2),
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
    ax.axhline(K.VREF_V, color="#a8a7a1", ls="--", lw=1)
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
    ax.axhline(K.VREF_V, color="#a8a7a1", ls="--", lw=1)
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="show figures interactively")
    args = ap.parse_args()
    if not args.show:
        matplotlib.use("Agg")

    RESULTS.mkdir(exist_ok=True)
    K.build_comparator()
    (HERE / "komparator.cir").write_text(K.spice_netlist())
    ng = NgSpice()

    # 1. DC transfer characteristic
    dc = run_dc(ng, K.VREF_V)
    dc_res = analyze_dc(dc, K.VREF_V)
    save_csv(RESULTS / "dc_transfer.csv", dc)

    # 2. Threshold vs. V_REF
    family, fam_rows = [], []
    for vref in (1.0, 2.5, 4.0):
        d = run_dc(ng, vref)
        family.append((vref, d))
        r = analyze_dc(d, vref)
        fam_rows.append([f"{vref:.2f}", f"{r['vth']:.3f}", f"{r['offset'] * 1e3:+.1f}",
                         f"{r['width'] * 1e3:.1f}", f"{r['gain']:.0f}",
                         f"{r['vlow']:.3f}", f"{r['vhigh']:.3f}"])

    # 3. Transient sine
    tr = run_tran_sine(ng)
    save_csv(RESULTS / "tran_sine.csv", tr)
    t_r = crossings(tr["time"], tr["v(out)"], K.VCC_V / 2, True)
    t_f = crossings(tr["time"], tr["v(out)"], K.VCC_V / 2, False)
    in_r = crossings(tr["time"], tr["v(v_in)"], K.VREF_V, True)
    in_f = crossings(tr["time"], tr["v(v_in)"], K.VREF_V, False)
    n = min(len(t_r), len(in_r))
    m = min(len(t_f), len(in_f))
    sine_res = dict(delay_r=np.mean(t_r[:n] - in_r[:n]) if n else np.nan,
                    delay_f=np.mean(t_f[:m] - in_f[:m]) if m else np.nan,
                    vhigh=np.max(tr["v(out)"]), vlow=np.min(tr["v(out)"]),
                    duty=np.mean(tr["v(out)"] > K.VCC_V / 2))

    # 4. Pulse
    pu = run_tran_pulse(ng)
    save_csv(RESULTS / "tran_pulse.csv", pu)
    pu_res = analyze_pulse(pu)

    # ---- Table ------------------------------------------------------------
    i_tail = dc["v(tail)"][np.argmin(abs(dc["v(v_in)"] - K.VREF_V))] / 4.7e3
    op_rows = [
        ["Supply VCC", f"{K.VCC_V:g} V"],
        ["Reference V_REF", f"{K.VREF_V:g} V"],
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
    report = ("# Transistor comparator - simulation results\n\n"
              "## Characteristics\n\n" + t1 + "\n\n## Threshold as a function of V_REF\n\n" + t2 + "\n")
    (RESULTS / "summary.md").write_text(report)
    print(report)

    # ---- Figures ----------------------------------------------------------
    figs = make_figures(dc, dc_res, family, tr, pu, pu_res)
    print("Figures and CSV data written to", RESULTS.relative_to(HERE))
    if args.show:
        import matplotlib.pyplot as plt
        tile_windows(figs)
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
