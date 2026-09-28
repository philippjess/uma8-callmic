"""Minimaler LADSPA-Host für Tests (ctypes)."""
import ctypes as C

import numpy as np

HANDLE = C.c_void_p
PORT_INPUT, PORT_OUTPUT, PORT_CONTROL, PORT_AUDIO = 1, 2, 4, 8


class PortRangeHint(C.Structure):
    _fields_ = [("hint", C.c_int), ("lower", C.c_float), ("upper", C.c_float)]


class Descriptor(C.Structure):
    pass


Descriptor._fields_ = [
    ("unique_id", C.c_ulong), ("label", C.c_char_p), ("properties", C.c_int),
    ("name", C.c_char_p), ("maker", C.c_char_p), ("copyright", C.c_char_p),
    ("port_count", C.c_ulong), ("port_descriptors", C.POINTER(C.c_int)),
    ("port_names", C.POINTER(C.c_char_p)), ("port_range_hints", C.POINTER(PortRangeHint)),
    ("implementation_data", C.c_void_p),
    ("instantiate", C.CFUNCTYPE(HANDLE, C.POINTER(Descriptor), C.c_ulong)),
    ("connect_port", C.CFUNCTYPE(None, HANDLE, C.c_ulong, C.POINTER(C.c_float))),
    ("activate", C.CFUNCTYPE(None, HANDLE)),
    ("run", C.CFUNCTYPE(None, HANDLE, C.c_ulong)),
    ("run_adding", C.c_void_p),
    ("set_run_adding_gain", C.c_void_p),
    ("deactivate", C.CFUNCTYPE(None, HANDLE)),
    ("cleanup", C.CFUNCTYPE(None, HANDLE)),
]


def _default(h: PortRangeHint) -> float:
    lo, hi = h.lower, h.upper
    return {
        0x40: lo, 0x80: 0.75 * lo + 0.25 * hi, 0xC0: 0.5 * (lo + hi),
        0x100: 0.25 * lo + 0.75 * hi, 0x140: hi, 0x200: 0.0, 0x240: 1.0,
        0x280: 100.0, 0x2C0: 440.0,
    }.get(h.hint & 0x3C0, lo if h.hint & 1 else 0.0)


class Plugin:
    def __init__(self, so_path: str, label: str, sample_rate: int = 48000, block: int = 512):
        self._lib = C.CDLL(str(so_path))
        fn = self._lib.ladspa_descriptor
        fn.restype, fn.argtypes = C.POINTER(Descriptor), [C.c_ulong]
        index = 0
        while True:
            dp = fn(index)
            if not dp:
                raise KeyError(f"Label {label!r} nicht in {so_path}")
            if dp.contents.label.decode() == label:
                break
            index += 1
        self._dp, self.d, self.block = dp, dp.contents, block
        n = self.d.port_count
        self.names = [self.d.port_names[k].decode() for k in range(n)]
        self.kinds = [self.d.port_descriptors[k] for k in range(n)]
        self.handle = self.d.instantiate(dp, sample_rate)
        if not self.handle:
            raise RuntimeError("instantiate fehlgeschlagen")
        self.bufs: dict[str, np.ndarray] = {}
        for k, (name, kind) in enumerate(zip(self.names, self.kinds)):
            buf = np.zeros(1 if kind & PORT_CONTROL else block, dtype=np.float32)
            if kind & PORT_CONTROL:
                buf[0] = _default(self.d.port_range_hints[k])
            self.bufs[name] = buf
            self.d.connect_port(self.handle, k, buf.ctypes.data_as(C.POINTER(C.c_float)))
        if self.d.activate:
            self.d.activate(self.handle)

    def set(self, name: str, value: float) -> None:
        self.bufs[name][0] = value

    def audio_ports(self, direction: int) -> list[str]:
        return [n for n, k in zip(self.names, self.kinds) if k & PORT_AUDIO and k & direction]

    def process(self, inputs: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        length = len(next(iter(inputs.values())))
        outs = {n: np.zeros(length, dtype=np.float32) for n in self.audio_ports(PORT_OUTPUT)}
        for start in range(0, length, self.block):
            m = min(self.block, length - start)
            for name in self.audio_ports(PORT_INPUT):
                self.bufs[name][:m] = inputs[name][start:start + m] if name in inputs else 0.0
            self.d.run(self.handle, m)
            for name, out in outs.items():
                out[start:start + m] = self.bufs[name][:m]
        return outs

    def close(self) -> None:
        if self.handle:
            if self.d.deactivate:
                self.d.deactivate(self.handle)
            self.d.cleanup(self.handle)
            self.handle = None
