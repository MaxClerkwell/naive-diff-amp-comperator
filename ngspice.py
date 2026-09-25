"""
Minimal ctypes wrapper around libngspice (the same shared library that the
KiCad simulator uses). No PySpice required.

    ng = NgSpice()
    ng.load_netlist(text)
    ng.run("dc vin 0 5 0.01")
    out = ng.vector("v(out)")
"""

import ctypes
import ctypes.util

import numpy as np


class _VecInfo(ctypes.Structure):
    _fields_ = [
        ("v_name", ctypes.c_char_p),
        ("v_type", ctypes.c_int),
        ("v_flags", ctypes.c_short),
        ("v_realdata", ctypes.POINTER(ctypes.c_double)),
        ("v_compdata", ctypes.c_void_p),
        ("v_length", ctypes.c_int),
    ]


_SEND_CHAR = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
_SEND_STAT = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
_CTRL_EXIT = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_bool, ctypes.c_bool, ctypes.c_int, ctypes.c_void_p)
_SEND_DATA = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
_SEND_INIT = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
_BG_RUN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_bool, ctypes.c_int, ctypes.c_void_p)


class NgSpice:
    def __init__(self, libname=None, verbose=False):
        libname = libname or ctypes.util.find_library("ngspice") or "libngspice.so.0"
        self.lib = ctypes.CDLL(libname)
        self.verbose = verbose
        self.log = []

        # Callbacks must stay referenced as attributes, otherwise the GC frees them.
        self._cb_char = _SEND_CHAR(self._on_char)
        self._cb_stat = _SEND_STAT(lambda s, i, u: 0)
        self._cb_exit = _CTRL_EXIT(lambda st, im, qu, i, u: 0)
        self._cb_data = _SEND_DATA(lambda v, n, i, u: 0)
        self._cb_init = _SEND_INIT(lambda v, i, u: 0)
        self._cb_bg = _BG_RUN(lambda r, i, u: 0)

        self.lib.ngSpice_Init.restype = ctypes.c_int
        self.lib.ngSpice_Command.argtypes = [ctypes.c_char_p]
        self.lib.ngSpice_Command.restype = ctypes.c_int
        self.lib.ngSpice_Circ.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
        self.lib.ngSpice_Circ.restype = ctypes.c_int
        self.lib.ngGet_Vec_Info.argtypes = [ctypes.c_char_p]
        self.lib.ngGet_Vec_Info.restype = ctypes.POINTER(_VecInfo)
        self.lib.ngSpice_AllVecs.restype = ctypes.POINTER(ctypes.c_char_p)
        self.lib.ngSpice_CurPlot.restype = ctypes.c_char_p

        rc = self.lib.ngSpice_Init(self._cb_char, self._cb_stat, self._cb_exit,
                                   self._cb_data, self._cb_init, self._cb_bg, None)
        if rc != 0:
            raise RuntimeError("ngSpice_Init failed")

    # ---------------------------------------------------------------
    def _on_char(self, s, ident, user):
        msg = s.decode(errors="replace")
        self.log.append(msg)
        if self.verbose or (msg.startswith("stderr") and "spinit" not in msg):
            print(msg)
        return 0

    def command(self, cmd):
        rc = self.lib.ngSpice_Command(cmd.encode())
        if rc != 0:
            raise RuntimeError(f"ngspice command failed: {cmd}")

    def load_netlist(self, text):
        """Load a netlist from text (first line = title, .end is appended if missing)."""
        lines = [l for l in text.splitlines() if l.strip()]
        if not lines[-1].strip().lower().startswith(".end"):
            lines.append(".end")
        arr = (ctypes.c_char_p * (len(lines) + 1))()
        for i, l in enumerate(lines):
            arr[i] = l.encode()
        arr[len(lines)] = None
        rc = self.lib.ngSpice_Circ(arr)
        if rc != 0:
            raise RuntimeError("ngSpice_Circ failed:\n" + "\n".join(self.log[-20:]))

    def run(self, analysis):
        """e.g. run('dc vin 0 5 0.01') or run('tran 1u 3m').
        ngspice stores device names in lower case, so the command is lowered."""
        self.log.clear()
        self.command(analysis.lower())
        errs = [l for l in self.log if "error" in l.lower()]
        if errs:
            raise RuntimeError("ngspice reported errors:\n" + "\n".join(errs))

    def vector(self, name):
        info = self.lib.ngGet_Vec_Info(name.encode())
        if not info:
            raise KeyError(f"vector {name!r} not found (plot: {self.plot_name()})")
        v = info.contents
        if v.v_realdata:
            return np.ctypeslib.as_array(v.v_realdata, shape=(v.v_length,)).copy()
        raise ValueError(f"vector {name!r} is complex")

    def plot_name(self):
        return self.lib.ngSpice_CurPlot().decode()

    def vectors(self):
        p = self.lib.ngSpice_AllVecs(self.plot_name().encode())
        out, i = [], 0
        while p[i]:
            out.append(p[i].decode())
            i += 1
        return out
