# Transistor comparator (SKiDL → KiCad → ngspice)

A simple comparator built from discrete bipolar transistors, no op-amp:

* **Q1/Q2 (2N3904)** – NPN differential pair, common emitter resistor RE (≈ 0.4 mA tail current)
* **RC1/RC2** – collector resistors
* **Q3 (2N3906)** – PNP output stage, base driven through RB from Q1's collector, load resistor RL

`OUT = VCC` if `V_IN > V_REF`, otherwise `OUT = 0 V`.

## Files

| File | Content |
|---|---|
| `komparator.py` | **SKiDL description** of the circuit, component values, SPICE models, schematic layout. Generates netlist, schematic and SPICE netlist. |
| `kicad_sch.py` | Writer that turns the SKiDL circuit + placement into a `.kicad_sch` (incl. Sim.* fields for the KiCad simulator) |
| `ngspice.py` | ctypes wrapper around `libngspice.so` (no PySpice needed) |
| `simulate.py` | **Simulation**: DC transfer, threshold vs. V_REF, transient (sine, pulse) → table + Matplotlib figures |
| `komparator.kicad_sch` / `.kicad_pro` | generated KiCad project, runs directly in the KiCad simulator (`.tran 1u 3m` is placed as text in the sheet) |
| `komparator.net` | KiCad netlist from SKiDL |
| `komparator.cir` | SPICE netlist derived from the SKiDL circuit |
| `results/` | `summary.md`, raw CSV data, PNG figures and `overview.png` (all plots on one canvas) |

## Usage

```bash
uv venv .venv && uv pip install -r requirements.txt   # or pip
.venv/bin/python komparator.py     # generate netlist + schematic, verify consistency with kicad-cli
.venv/bin/python simulate.py       # simulate, print table, write figures to results/
.venv/bin/python simulate.py --show   # additionally open the figures tiled across the screen
```

Requirements: Python ≥ 3.10, KiCad 9/10 (symbol libraries in
`/usr/share/kicad/symbols`, `kicad-cli` optional for the check), `libngspice.so.0`.

`komparator.py` re-exports the generated schematic with `kicad-cli` as a SPICE
netlist and compares its connectivity with the SKiDL circuit. The message
`Schematic <-> SKiDL netlist: IDENTICAL` confirms that schematic and simulation
describe the same circuit.

## Results (see `results/summary.md`)

At V_REF = 2.5 V the output switches at ≈ 2.48 V with a transition width of
only ~6 mV (gain ≈ 770 V/V). Propagation delay L→H is ~160 ns, H→L ~1.3 µs
because Q3 saturates and its storage time dominates. At a low V_REF (1 V)
the tail current drops sharply and the offset grows to ~240 mV; a current
mirror instead of RE would fix that.

![Overview](results/overview.png)

## License

This work is licensed under the [Creative Commons Attribution 4.0 International License](https://creativecommons.org/licenses/by/4.0/) (CC BY 4.0). See `LICENSE`.
