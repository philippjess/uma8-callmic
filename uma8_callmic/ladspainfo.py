"""Labels und Portnamen eines LADSPA-Plugins (ctypes), um ein veraltetes libuma8_beam.so zu erkennen.

Gelesen wird in einem eigenen Prozess (`ports`): dlopen lädt einen schon geladenen Pfad nicht neu, ein ersetztes
Plugin bliebe im Tray also unsichtbar, und ein kaputtes .so reißt so nicht das Tray mit. Nur Standardbibliothek,
damit die Datei auch als Skript (`python3 -I ladspainfo.py <plugin>`) läuft."""
import ctypes as C
import json
import subprocess
import sys


class _Descriptor(C.Structure):
    """Anfang von LADSPA_Descriptor bis port_names; der Rest wird nicht gebraucht."""
    _fields_ = [("unique_id", C.c_ulong), ("label", C.c_char_p), ("properties", C.c_int), ("name", C.c_char_p),
                ("maker", C.c_char_p), ("copyright", C.c_char_p), ("port_count", C.c_ulong),
                ("port_descriptors", C.c_void_p), ("port_names", C.POINTER(C.c_char_p))]


def read_ports(path: str) -> dict[str, list[str]]:
    """Label → Portnamen aller Plugins der Bibliothek, im eigenen Prozess geladen."""
    fn = C.CDLL(path).ladspa_descriptor
    fn.restype, fn.argtypes = C.POINTER(_Descriptor), [C.c_ulong]
    found, index = {}, 0
    while d := fn(index):
        d = d.contents
        found[d.label.decode()] = [d.port_names[k].decode() for k in range(d.port_count)]
        index += 1
    return found


def ports(path, timeout: float = 10.0) -> dict[str, list[str]]:
    """Wie read_ports, aber in einem Kindprozess. RuntimeError mit der Ursache, wenn das Plugin nicht ladbar ist."""
    r = subprocess.run([sys.executable, "-I", __file__, str(path)], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        lines = r.stderr.strip().splitlines()
        code = f"Signal {-r.returncode}" if r.returncode < 0 else f"Exit-Code {r.returncode}"
        raise RuntimeError(lines[-1] if lines else code)
    return json.loads(r.stdout)


if __name__ == "__main__":
    json.dump(read_ports(sys.argv[1]), sys.stdout)
