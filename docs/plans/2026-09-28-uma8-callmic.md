# uma8-callmic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Virtuelles Mikrofon „UMA-8 Call Mic“ für PipeWire, das die 7 Rohkanäle des miniDSP UMA-8 per Beamforming, Hallunterdrückung und DeepFilterNet zu einem sauberen Sprachsignal in voller Bandbreite verarbeitet, bedient über ein KDE-Tray-Icon.

**Architecture:** Die Tonverarbeitung läuft vollständig in einer PipeWire-Filterkette (eigener `systemd --user`-Dienst): ein eigenes LADSPA-Plugin in Rust (`uma8_beam`, `uma8_limit`), das DeepFilterNet-LADSPA-Plugin und der eingebaute Mixer als Umschalter. Ein Python/PySide6-Tray-Programm steuert alle Plugin-Controls live über `pw-cli set-param`, kalibriert die Richtung (SRP-PHAT) und führt sie optional nach; es ist nie im Tonweg.

**Tech Stack:** Rust 2021 (`cdylib`, Crate `realfft` 3), PipeWire 1.6 filter-chain, Python ≥ 3.11 mit PySide6 und numpy, pytest.

**Spec:** `docs/specs/2026-09-28-uma8-callmic-design.md`

## Global Constraints

- **Kein `git commit` und kein `git push` ohne ausdrückliche Anweisung des Nutzers.** Jeder Task endet mit `git status --short` und einem Bericht, nicht mit einem Commit. Subagenten ausdrücklich darauf hinweisen.
- **Keine eigenen USB-/DFU-Befehle an das UMA-8.** Nur lesender Zugriff über PipeWire (`pw-record`, `pw-dump`).
- Abtastrate überall 48000 Hz.
- Raw-Quelle: `alsa_input.usb-miniDSP_micArray_RAW_SPK-00.analog-surround-71` (8 Kanäle, Kanal 7 stumm).
- Knotennamen: Quelle `uma8_callmic`, Capture-Knoten der Kette `uma8_callmic_capture`, Dienst `uma8-callmic-chain.service`.
- Plugin-Datei installiert unter `~/.local/lib/ladspa/libuma8_beam.so`; DeepFilterNet unter `/usr/lib/ladspa/libdeep_filter_ladspa.so`, Label `deep_filter_mono`, Control `Attenuation Limit (dB)`.
- Standard-Geometrie (ODAS `minidsp.cfg`): Mitte Kanal 0, Ring `[1, 6, 5, 4, 3, 2]` gegen den Uhrzeigersinn ab 90°, Radius 43 mm.
- Python: nur Standardbibliothek, numpy, PySide6 (kein scipy). Rust: einzige Abhängigkeit `realfft`.
- Alle Texte für den Nutzer auf Deutsch.
- Echtzeitcode im Plugin: keine Allokation, keine Locks, keine Systemaufrufe in `run()`.

## Dateistruktur

```
uma8-callmic/
├── plugin/
│   ├── Cargo.toml
│   └── src/
│       ├── lib.rs          Modul-Deklarationen, ladspa_descriptor()
│       ├── ladspa.rs       C-ABI-Typen, Plugin-Trait, generische Glue-Funktionen
│       ├── limiter.rs      Look-ahead-Begrenzer (DSP)
│       ├── beam.rs         Geometrie, Fractional Delay, Delay-and-Sum mit Überblendung
│       ├── dereverb.rs     Späte-Nachhall-Unterdrückung (STFT)
│       └── plugins.rs      LADSPA-Plugins uma8_beam und uma8_limit
├── uma8_callmic/
│   ├── __init__.py
│   ├── __main__.py         CLI/Einstieg (Tray, --write-config, --check-geometry)
│   ├── constants.py        Pfade, Knotennamen, Latenzen
│   ├── array.py            ArrayGeometry (Kanal → Position)
│   ├── config.py           Einstellungen laden/speichern/validieren
│   ├── params.py           Einstellungen → Plugin-Controls
│   ├── chainconf.py        PipeWire-Konfiguration erzeugen
│   ├── pwctl.py            pw-dump/pw-cli/systemctl, Status
│   ├── traystate.py        Icon-Zustand und Tooltip (ohne Qt)
│   ├── capture.py          pw-record → Ringpuffer
│   ├── doa.py              SRP-PHAT, Sprachaktivität, Winkelhilfen
│   ├── calibration.py      Auswertung der Kalibrierung
│   ├── geometry.py         Kanalzuordnung aus Raumrauschen prüfen
│   ├── tracker.py          Nachführung
│   ├── dialogs.py          Qt-Dialoge
│   ├── tray.py             Qt-Tray-Anwendung
│   └── icons/              active.svg, inactive.svg, error.svg
├── pipewire/
│   ├── uma8-callmic.conf.in
│   ├── uma8-callmic-chain.service
│   └── uma8-callmic.desktop
├── tests/
│   ├── conftest.py, ladspa_host.py, sim.py
│   └── test_*.py
├── tools/check_output.py
├── pyproject.toml, install.sh, uninstall.sh, README.md
```

---

### Task 1: Rust-Grundgerüst mit LADSPA-Hülle und Begrenzer

**Files:**
- Create: `plugin/Cargo.toml`, `plugin/src/lib.rs`, `plugin/src/ladspa.rs`, `plugin/src/limiter.rs`, `plugin/src/plugins.rs`
- Create: `pyproject.toml`, `tests/conftest.py`, `tests/ladspa_host.py`
- Test: `plugin/src/limiter.rs` (Rust-Tests), `tests/test_limit_plugin.py`

**Interfaces:**
- Produces (Rust): `ladspa::{Plugin, PortSpec, Ports, descriptor, HINT_*, PORT_*}`; `Ports::control(i, fallback) -> f32`, `Ports::read(i, offset, &mut [f32])`, `Ports::write(i, offset, &[f32])`; `limiter::Limiter::{new(sr), latency(), set_ceiling_db(db), process(&[f32], &mut [f32])}`; `plugins::all() -> Vec<Descriptor>`.
- Produces (Python-Tests): `ladspa_host.Plugin(so_path, label, sample_rate=48000, block=512)` mit `.names`, `.set(name, value)`, `.process({port: array}) -> {port: array}`, `.close()`; Fixture `plugin_so` (Pfad zur gebauten `.so`).
- LADSPA-Label `uma8_limit`, Ports: `In`, `Out`, `Ceiling (dB)`.

- [ ] **Step 1: Cargo-Projekt und pytest-Konfiguration anlegen**

`plugin/Cargo.toml`:
```toml
[package]
name = "uma8_beam"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib", "rlib"]

[dependencies]
realfft = "3"

[profile.release]
opt-level = 3
```

`pyproject.toml`:
```toml
[project]
name = "uma8-callmic"
version = "0.1.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
pythonpath = [".", "tests"]
testpaths = ["tests"]
markers = [
    "integration: braucht laufendes PipeWire und installiertes Plugin",
    "hardware: braucht das angeschlossene UMA-8",
]
addopts = "-m 'not integration and not hardware'"
```

- [ ] **Step 2: Begrenzer mit fehlschlagenden Rust-Tests schreiben**

`plugin/src/limiter.rs`:
```rust
//! Look-ahead-Begrenzer: 5 ms Vorschau, sofortiger Angriff, 100 ms Release.

pub struct Limiter {
    ring: Vec<f32>,
    pos: usize,
    gain: f32,
    release: f32,
    ceiling: f32,
}

impl Limiter {
    pub fn new(sample_rate: f32) -> Self {
        let lookahead = (0.005 * sample_rate).round() as usize;
        Limiter {
            ring: vec![0.0; lookahead + 1],
            pos: 0,
            gain: 1.0,
            release: (-1.0 / (0.1 * sample_rate)).exp(),
            ceiling: 1.0,
        }
    }

    /// Verzögerung des Ausgangs in Samples.
    pub fn latency(&self) -> usize {
        self.ring.len() - 1
    }

    pub fn set_ceiling_db(&mut self, db: f32) {
        self.ceiling = 10f32.powf(db.clamp(-12.0, 0.0) / 20.0);
    }

    pub fn process(&mut self, input: &[f32], output: &mut [f32]) {
        unimplemented!()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quiet_signal_passes_delayed() {
        let mut l = Limiter::new(48000.0);
        l.set_ceiling_db(-1.0);
        let x: Vec<f32> = (0..4800).map(|n| 0.1 * (n as f32 * 0.05).sin()).collect();
        let mut y = vec![0.0; x.len()];
        l.process(&x, &mut y);
        let d = l.latency();
        for n in d..x.len() {
            assert!((y[n] - x[n - d]).abs() < 1e-6, "n={n}");
        }
    }

    #[test]
    fn loud_signal_is_capped() {
        let mut l = Limiter::new(48000.0);
        l.set_ceiling_db(-1.0);
        let x: Vec<f32> = (0..48000).map(|n| 2.0 * (n as f32 * 0.0576).sin()).collect();
        let mut y = vec![0.0; x.len()];
        l.process(&x, &mut y);
        let ceiling = 10f32.powf(-1.0 / 20.0);
        assert!(y.iter().all(|v| v.abs() <= ceiling + 1e-6));
    }
}
```

`plugin/src/lib.rs` (vorläufig, nur für die Tests):
```rust
pub mod limiter;
```

- [ ] **Step 3: Tests laufen lassen, sie müssen fehlschlagen**

Run: `cargo test --manifest-path plugin/Cargo.toml limiter`
Expected: FAIL mit `not implemented`

- [ ] **Step 4: `process` implementieren**

In `plugin/src/limiter.rs` den Rumpf von `process` ersetzen:
```rust
    pub fn process(&mut self, input: &[f32], output: &mut [f32]) {
        let len = self.ring.len();
        for (x, y) in input.iter().zip(output.iter_mut()) {
            self.ring[self.pos] = *x;
            self.pos = (self.pos + 1) % len;
            // ältester Wert im Ring = um `len - 1` Samples verzögert
            let delayed = self.ring[self.pos];
            let peak = self.ring.iter().fold(0.0f32, |m, v| m.max(v.abs()));
            let target = if peak > self.ceiling { self.ceiling / peak } else { 1.0 };
            self.gain = if target < self.gain {
                target
            } else {
                target + (self.gain - target) * self.release
            };
            *y = delayed * self.gain;
        }
    }
```

- [ ] **Step 5: Rust-Tests laufen lassen**

Run: `cargo test --manifest-path plugin/Cargo.toml limiter`
Expected: 2 passed

- [ ] **Step 6: LADSPA-Hülle schreiben**

`plugin/src/ladspa.rs`:
```rust
//! Minimale LADSPA-Hülle: C-ABI-Typen und generische Glue-Funktionen.
//! `unsafe` ist auf diese Datei beschränkt.

use std::ffi::{c_char, c_int, c_ulong, c_void, CString};
use std::panic::{catch_unwind, AssertUnwindSafe};

pub type Data = f32;
pub type Handle = *mut c_void;

pub const PORT_INPUT: c_int = 0x1;
pub const PORT_OUTPUT: c_int = 0x2;
pub const PORT_CONTROL: c_int = 0x4;
pub const PORT_AUDIO: c_int = 0x8;

pub const HINT_BOUNDED_BELOW: c_int = 0x1;
pub const HINT_BOUNDED_ABOVE: c_int = 0x2;
pub const HINT_TOGGLED: c_int = 0x4;
pub const HINT_INTEGER: c_int = 0x20;
pub const HINT_DEFAULT_MIDDLE: c_int = 0xC0;
pub const HINT_DEFAULT_MAXIMUM: c_int = 0x140;
pub const HINT_DEFAULT_0: c_int = 0x200;

pub const PROPERTY_HARD_RT_CAPABLE: c_int = 0x4;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct PortRangeHint {
    pub hint_descriptor: c_int,
    pub lower_bound: Data,
    pub upper_bound: Data,
}

#[repr(C)]
pub struct Descriptor {
    pub unique_id: c_ulong,
    pub label: *const c_char,
    pub properties: c_int,
    pub name: *const c_char,
    pub maker: *const c_char,
    pub copyright: *const c_char,
    pub port_count: c_ulong,
    pub port_descriptors: *const c_int,
    pub port_names: *const *const c_char,
    pub port_range_hints: *const PortRangeHint,
    pub implementation_data: *mut c_void,
    pub instantiate: Option<unsafe extern "C" fn(*const Descriptor, c_ulong) -> Handle>,
    pub connect_port: Option<unsafe extern "C" fn(Handle, c_ulong, *mut Data)>,
    pub activate: Option<unsafe extern "C" fn(Handle)>,
    pub run: Option<unsafe extern "C" fn(Handle, c_ulong)>,
    pub run_adding: Option<unsafe extern "C" fn(Handle, c_ulong)>,
    pub set_run_adding_gain: Option<unsafe extern "C" fn(Handle, Data)>,
    pub deactivate: Option<unsafe extern "C" fn(Handle)>,
    pub cleanup: Option<unsafe extern "C" fn(Handle)>,
}

// Die Zeiger zeigen auf geleakte, unveränderliche Daten.
unsafe impl Send for Descriptor {}
unsafe impl Sync for Descriptor {}

pub struct PortSpec {
    pub name: &'static str,
    pub kind: c_int,
    pub hint: c_int,
    pub lower: f32,
    pub upper: f32,
}

impl PortSpec {
    pub const fn audio_in(name: &'static str) -> Self {
        PortSpec { name, kind: PORT_INPUT | PORT_AUDIO, hint: 0, lower: 0.0, upper: 0.0 }
    }
    pub const fn audio_out(name: &'static str) -> Self {
        PortSpec { name, kind: PORT_OUTPUT | PORT_AUDIO, hint: 0, lower: 0.0, upper: 0.0 }
    }
    pub const fn control(name: &'static str, lower: f32, upper: f32, hint: c_int) -> Self {
        PortSpec {
            name,
            kind: PORT_INPUT | PORT_CONTROL,
            hint: hint | HINT_BOUNDED_BELOW | HINT_BOUNDED_ABOVE,
            lower,
            upper,
        }
    }
}

/// Zugriff auf die vom Host verbundenen Port-Puffer.
pub struct Ports<'a> {
    ptrs: &'a [*mut Data],
}

impl Ports<'_> {
    /// Control-Wert; nicht verbundene Ports oder NaN liefern `fallback`.
    pub fn control(&self, i: usize, fallback: f32) -> f32 {
        let p = self.ptrs[i];
        if p.is_null() {
            return fallback;
        }
        let v = unsafe { *p };
        if v.is_finite() { v } else { fallback }
    }

    /// Kopiert `dst.len()` Samples ab `offset` aus Audio-Port `i`.
    pub fn read(&self, i: usize, offset: usize, dst: &mut [f32]) {
        let p = self.ptrs[i];
        if p.is_null() {
            dst.fill(0.0);
        } else {
            unsafe { std::ptr::copy(p.add(offset), dst.as_mut_ptr(), dst.len()) }
        }
    }

    /// Schreibt `src` ab `offset` in Audio-Port `i`.
    pub fn write(&self, i: usize, offset: usize, src: &[f32]) {
        let p = self.ptrs[i];
        if !p.is_null() {
            unsafe { std::ptr::copy(src.as_ptr(), p.add(offset), src.len()) }
        }
    }
}

pub trait Plugin: Sized + 'static {
    const UNIQUE_ID: c_ulong;
    const LABEL: &'static str;
    const NAME: &'static str;
    const PORTS: &'static [PortSpec];
    fn new(sample_rate: f32) -> Self;
    fn activate(&mut self) {}
    /// Verarbeitet `n` Samples. Darf nicht allozieren.
    fn run(&mut self, ports: &Ports, n: usize);
}

struct Instance<P: Plugin> {
    plugin: P,
    ports: Vec<*mut Data>,
    failed: bool,
}

unsafe extern "C" fn instantiate<P: Plugin>(_d: *const Descriptor, sr: c_ulong) -> Handle {
    let made = catch_unwind(|| {
        Box::new(Instance::<P> {
            plugin: P::new(sr as f32),
            ports: vec![std::ptr::null_mut(); P::PORTS.len()],
            failed: false,
        })
    });
    match made {
        Ok(b) => Box::into_raw(b) as Handle,
        Err(_) => std::ptr::null_mut(),
    }
}

unsafe extern "C" fn connect_port<P: Plugin>(h: Handle, port: c_ulong, data: *mut Data) {
    let inst = &mut *(h as *mut Instance<P>);
    if let Some(slot) = inst.ports.get_mut(port as usize) {
        *slot = data;
    }
}

unsafe extern "C" fn activate<P: Plugin>(h: Handle) {
    let inst = &mut *(h as *mut Instance<P>);
    let plugin = &mut inst.plugin;
    if catch_unwind(AssertUnwindSafe(|| plugin.activate())).is_err() {
        inst.failed = true;
    }
}

unsafe extern "C" fn run<P: Plugin>(h: Handle, n: c_ulong) {
    let inst = &mut *(h as *mut Instance<P>);
    let n = n as usize;
    if !inst.failed {
        let ports = Ports { ptrs: &inst.ports };
        let plugin = &mut inst.plugin;
        if catch_unwind(AssertUnwindSafe(|| plugin.run(&ports, n))).is_ok() {
            return;
        }
        inst.failed = true;
    }
    // Nach einem Panic dauerhaft Stille statt Absturz des Host-Prozesses.
    for (i, spec) in P::PORTS.iter().enumerate() {
        if spec.kind == PORT_OUTPUT | PORT_AUDIO && !inst.ports[i].is_null() {
            std::ptr::write_bytes(inst.ports[i], 0, n);
        }
    }
}

unsafe extern "C" fn cleanup<P: Plugin>(h: Handle) {
    drop(Box::from_raw(h as *mut Instance<P>));
}

fn leak_str(s: &str) -> *const c_char {
    CString::new(s).expect("kein NUL im Namen").into_raw()
}

pub fn descriptor<P: Plugin>() -> Descriptor {
    let kinds: Vec<c_int> = P::PORTS.iter().map(|p| p.kind).collect();
    let names: Vec<*const c_char> = P::PORTS.iter().map(|p| leak_str(p.name)).collect();
    let hints: Vec<PortRangeHint> = P::PORTS
        .iter()
        .map(|p| PortRangeHint { hint_descriptor: p.hint, lower_bound: p.lower, upper_bound: p.upper })
        .collect();
    Descriptor {
        unique_id: P::UNIQUE_ID,
        label: leak_str(P::LABEL),
        properties: PROPERTY_HARD_RT_CAPABLE,
        name: leak_str(P::NAME),
        maker: leak_str("uma8-callmic"),
        copyright: leak_str("MIT"),
        port_count: P::PORTS.len() as c_ulong,
        port_descriptors: Box::leak(kinds.into_boxed_slice()).as_ptr(),
        port_names: Box::leak(names.into_boxed_slice()).as_ptr(),
        port_range_hints: Box::leak(hints.into_boxed_slice()).as_ptr(),
        implementation_data: std::ptr::null_mut(),
        instantiate: Some(instantiate::<P>),
        connect_port: Some(connect_port::<P>),
        activate: Some(activate::<P>),
        run: Some(run::<P>),
        run_adding: None,
        set_run_adding_gain: None,
        deactivate: None,
        cleanup: Some(cleanup::<P>),
    }
}
```

`plugin/src/plugins.rs`:
```rust
//! Die LADSPA-Plugins der Bibliothek.

use crate::ladspa::*;
use crate::limiter::Limiter;
use std::ffi::c_ulong;

const CHUNK: usize = 1024;

pub struct LimitPlugin {
    limiter: Limiter,
    buf_in: Vec<f32>,
    buf_out: Vec<f32>,
}

impl Plugin for LimitPlugin {
    const UNIQUE_ID: c_ulong = 8009730;
    const LABEL: &'static str = "uma8_limit";
    const NAME: &'static str = "UMA-8 Limiter";
    const PORTS: &'static [PortSpec] = &[
        PortSpec::audio_in("In"),
        PortSpec::audio_out("Out"),
        PortSpec::control("Ceiling (dB)", -12.0, 0.0, HINT_DEFAULT_MAXIMUM),
    ];

    fn new(sample_rate: f32) -> Self {
        LimitPlugin { limiter: Limiter::new(sample_rate), buf_in: vec![0.0; CHUNK], buf_out: vec![0.0; CHUNK] }
    }

    fn run(&mut self, ports: &Ports, n: usize) {
        self.limiter.set_ceiling_db(ports.control(2, -1.0));
        let mut off = 0;
        while off < n {
            let m = (n - off).min(CHUNK);
            ports.read(0, off, &mut self.buf_in[..m]);
            self.limiter.process(&self.buf_in[..m], &mut self.buf_out[..m]);
            ports.write(1, off, &self.buf_out[..m]);
            off += m;
        }
    }
}

pub fn all() -> Vec<Descriptor> {
    vec![descriptor::<LimitPlugin>()]
}
```

`plugin/src/lib.rs` (ersetzen):
```rust
//! LADSPA-Plugins für das miniDSP UMA-8: Beamforming, Hallunterdrückung, Begrenzer.

pub mod ladspa;
pub mod limiter;
mod plugins;

use std::ffi::c_ulong;
use std::sync::OnceLock;

static DESCRIPTORS: OnceLock<Vec<ladspa::Descriptor>> = OnceLock::new();

/// Einstiegspunkt für LADSPA-Hosts.
#[no_mangle]
pub extern "C" fn ladspa_descriptor(index: c_ulong) -> *const ladspa::Descriptor {
    let all = DESCRIPTORS.get_or_init(plugins::all);
    all.get(index as usize).map_or(std::ptr::null(), |d| d as *const _)
}
```

- [ ] **Step 7: Python-LADSPA-Host für Tests schreiben**

`tests/ladspa_host.py`:
```python
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
```

`tests/conftest.py`:
```python
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def plugin_so() -> str:
    subprocess.run(
        ["cargo", "build", "--release", "--quiet", "--manifest-path", str(ROOT / "plugin/Cargo.toml")],
        check=True,
    )
    return str(ROOT / "plugin/target/release/libuma8_beam.so")
```

- [ ] **Step 8: Plugin-Test von außen schreiben**

`tests/test_limit_plugin.py`:
```python
import numpy as np

from ladspa_host import Plugin


def test_limit_plugin_has_expected_ports(plugin_so):
    p = Plugin(plugin_so, "uma8_limit")
    assert p.names == ["In", "Out", "Ceiling (dB)"]
    p.close()


def test_limit_plugin_caps_loud_sine(plugin_so):
    p = Plugin(plugin_so, "uma8_limit")
    p.set("Ceiling (dB)", -1.0)
    t = np.arange(48000) / 48000
    out = p.process({"In": 2.0 * np.sin(2 * np.pi * 440 * t)})["Out"]
    p.close()
    assert np.max(np.abs(out)) <= 10 ** (-1 / 20) + 1e-5


def test_limit_plugin_quiet_signal_unchanged_after_latency(plugin_so):
    p = Plugin(plugin_so, "uma8_limit")
    p.set("Ceiling (dB)", -1.0)
    x = (0.1 * np.sin(2 * np.pi * 440 * np.arange(9600) / 48000)).astype(np.float32)
    out = p.process({"In": x})["Out"]
    p.close()
    np.testing.assert_allclose(out[240:], x[:-240], atol=1e-6)
```

- [ ] **Step 9: Alle Tests laufen lassen**

Run: `cargo test --manifest-path plugin/Cargo.toml && python -m pytest tests/test_limit_plugin.py -v`
Expected: Rust 2 passed, pytest 3 passed

Zusätzlich: `analyseplugin plugin/target/release/libuma8_beam.so` listet „UMA-8 Limiter“ mit Label `uma8_limit`.

- [ ] **Step 10: Stand prüfen, nicht committen**

Run: `git status --short` und dem Nutzer berichten.

---

### Task 2: Beamformer-Modul (Rust)

**Files:**
- Create: `plugin/src/beam.rs`
- Modify: `plugin/src/lib.rs` (Zeile `pub mod beam;` ergänzen)
- Test: `plugin/src/beam.rs` (Rust-Tests)

**Interfaces:**
- Produces: `beam::{CHANNELS = 7, TAPS = 32, BASE_DELAY = 25, FADE_SAMPLES = 2400}`; `Geometry { center, ring: [usize; 6], ring_offset_deg, radius_m }` mit `Geometry::UMA8`, `is_valid()`, `positions() -> [[f32; 2]; 7]`; `Steering { azimuth_deg, elevation_deg, omni }`; `fractional_delay(d) -> (usize, [f32; TAPS])`; `Beamformer::{new(sr, Geometry, Steering), latency(), set_target(Geometry, Steering), process(&[&[f32]; 7], beam: &mut [f32], raw: &mut [f32])}`.
- Geometrie-Konvention (muss mit Python `ArrayGeometry` übereinstimmen): Ringmikrofon `ring[k]` liegt bei Winkel `ring_offset_deg + 60·k` (gegen den Uhrzeigersinn), Mitte bei (0, 0).
- `raw` = Mittel-Kanal um `BASE_DELAY` verzögert.

- [ ] **Step 1: Modul mit Tests und leeren Rümpfen schreiben**

`plugin/src/beam.rs`:
```rust
//! Delay-and-Sum-Beamformer für das UMA-8 (7 Mikrofone in einer Ebene, Fernfeld).

pub const CHANNELS: usize = 7;
pub const TAPS: usize = 32;
const HALF: usize = TAPS / 2;
/// Feste Grundverzögerung in Samples: halbe FIR-Länge plus maximale Laufzeit
/// über das Array (60 mm bei 48 kHz ≈ 8,4 Samples). Das ist die Latenz.
pub const BASE_DELAY: usize = 25;
const HIST: usize = 64;
pub const FADE_SAMPLES: usize = 2400;
const SPEED_OF_SOUND: f32 = 343.0;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Geometry {
    pub center: usize,
    pub ring: [usize; 6],
    pub ring_offset_deg: f32,
    pub radius_m: f32,
}

impl Geometry {
    /// Zuordnung laut ODAS `minidsp.cfg`: Kanal 0 Mitte, 1–6 im Uhrzeigersinn ab 90°.
    pub const UMA8: Geometry = Geometry { center: 0, ring: [1, 6, 5, 4, 3, 2], ring_offset_deg: 90.0, radius_m: 0.043 };

    /// Gültig, wenn jeder der 7 Kanäle genau einmal vorkommt.
    pub fn is_valid(&self) -> bool {
        let mut seen = [false; CHANNELS];
        for &c in std::iter::once(&self.center).chain(self.ring.iter()) {
            if c >= CHANNELS || seen[c] {
                return false;
            }
            seen[c] = true;
        }
        self.radius_m > 0.0
    }

    /// Position (x, y) in Metern je Kanal.
    pub fn positions(&self) -> [[f32; 2]; CHANNELS] {
        let mut p = [[0.0; 2]; CHANNELS];
        for (k, &ch) in self.ring.iter().enumerate() {
            let a = (self.ring_offset_deg + 60.0 * k as f32).to_radians();
            p[ch] = [self.radius_m * a.cos(), self.radius_m * a.sin()];
        }
        p
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Steering {
    pub azimuth_deg: f32,
    pub elevation_deg: f32,
    pub omni: bool,
}

#[derive(Clone, Copy)]
struct Taps {
    offset: [usize; CHANNELS],
    h: [[f32; TAPS]; CHANNELS],
}

/// Gefensterte Sinc-Interpolation: y[n] = Σ h[k]·x[n − offset − k] ≈ x[n − d].
pub fn fractional_delay(d: f32) -> (usize, [f32; TAPS]) {
    unimplemented!()
}

pub struct Beamformer {
    sample_rate: f32,
    hist: [[f32; HIST]; CHANNELS],
    pos: usize,
    current: (Geometry, Steering),
    target: (Geometry, Steering),
    fading_to: (Geometry, Steering),
    taps: Taps,
    next_taps: Taps,
    fade: Option<usize>,
}

impl Beamformer {
    pub fn new(sample_rate: f32, geom: Geometry, steer: Steering) -> Self {
        unimplemented!()
    }

    /// Latenz des Beam-Ausgangs in Samples.
    pub fn latency(&self) -> usize {
        BASE_DELAY
    }

    /// Neue Geometrie/Richtung. Ungültige Geometrien werden ignoriert.
    /// Der Wechsel wird über `FADE_SAMPLES` weich übergeblendet.
    pub fn set_target(&mut self, geom: Geometry, steer: Steering) {
        unimplemented!()
    }

    /// `input[ch]` sind gleich lang wie `beam` und `raw`.
    pub fn process(&mut self, input: &[&[f32]; CHANNELS], beam: &mut [f32], raw: &mut [f32]) {
        unimplemented!()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::f32::consts::PI;
    const SR: f32 = 48000.0;

    /// Ebene Welle eines Sinus aus Richtung (az, el), analytisch je Kanal.
    fn plane_wave_sine(geom: &Geometry, az: f32, el: f32, freq: f32, len: usize) -> Vec<Vec<f32>> {
        let pos = geom.positions();
        let (a, e) = (az.to_radians(), el.to_radians());
        let u = [e.cos() * a.cos(), e.cos() * a.sin()];
        (0..CHANNELS)
            .map(|ch| {
                let lead = (pos[ch][0] * u[0] + pos[ch][1] * u[1]) / SPEED_OF_SOUND;
                (0..len).map(|n| (2.0 * PI * freq * (n as f32 / SR + lead)).sin()).collect()
            })
            .collect()
    }

    fn run(bf: &mut Beamformer, ch: &[Vec<f32>], from: usize, to: usize, beam: &mut [f32], raw: &mut [f32]) {
        let refs: [&[f32]; CHANNELS] = std::array::from_fn(|i| &ch[i][from..to]);
        bf.process(&refs, &mut beam[from..to], &mut raw[from..to]);
    }

    fn rms(x: &[f32]) -> f32 {
        (x.iter().map(|v| v * v).sum::<f32>() / x.len() as f32).sqrt()
    }

    fn steer(az: f32) -> Steering {
        Steering { azimuth_deg: az, elevation_deg: 0.0, omni: false }
    }

    #[test]
    fn uma8_geometry_matches_odas() {
        let p = Geometry::UMA8.positions();
        assert!(p[0][0].abs() < 1e-6 && p[0][1].abs() < 1e-6);
        assert!((p[1][0] - 0.0).abs() < 1e-3 && (p[1][1] - 0.043).abs() < 1e-3);
        assert!((p[2][0] - 0.037).abs() < 1e-3 && (p[2][1] - 0.021).abs() < 1e-3);
        assert!((p[4][0] - 0.0).abs() < 1e-3 && (p[4][1] + 0.043).abs() < 1e-3);
    }

    #[test]
    fn fractional_delay_matches_shifted_sine() {
        let (offset, h) = fractional_delay(25.3);
        let f = 1000.0;
        let x: Vec<f32> = (0..400).map(|n| (2.0 * PI * f * n as f32 / SR).sin()).collect();
        for n in 100..400 {
            let y: f32 = (0..TAPS).map(|k| h[k] * x[n - offset - k]).sum();
            let expect = (2.0 * PI * f * (n as f32 - 25.3) / SR).sin();
            assert!((y - expect).abs() < 2e-3, "n={n}: {y} vs {expect}");
        }
    }

    #[test]
    fn steered_beam_passes_target_and_rejects_opposite() {
        let g = Geometry::UMA8;
        for &f in &[2000.0f32, 3000.0, 4000.0, 6000.0] {
            let x = plane_wave_sine(&g, 90.0, 0.0, f, 9600);
            let (mut b_on, mut b_off, mut raw) = (vec![0.0; 9600], vec![0.0; 9600], vec![0.0; 9600]);
            run(&mut Beamformer::new(SR, g, steer(90.0)), &x, 0, 9600, &mut b_on, &mut raw);
            run(&mut Beamformer::new(SR, g, steer(270.0)), &x, 0, 9600, &mut b_off, &mut raw);
            let (r_on, r_off) = (rms(&b_on[200..]), rms(&b_off[200..]));
            assert!((r_on - 0.7071).abs() < 0.03, "f={f}: on-axis rms {r_on}");
            let db = 20.0 * (r_on / r_off).log10();
            assert!(db >= 6.0, "f={f}: nur {db} dB");
        }
    }

    #[test]
    fn impulse_from_zenith_has_base_latency() {
        let mut x = vec![vec![0.0f32; 200]; CHANNELS];
        for ch in x.iter_mut() {
            ch[50] = 1.0;
        }
        let cases = [
            Steering { azimuth_deg: 0.0, elevation_deg: 90.0, omni: false },
            Steering { azimuth_deg: 123.0, elevation_deg: 0.0, omni: true },
        ];
        for s in cases {
            let (mut beam, mut raw) = (vec![0.0; 200], vec![0.0; 200]);
            run(&mut Beamformer::new(SR, Geometry::UMA8, s), &x, 0, 200, &mut beam, &mut raw);
            let peak = (0..200).max_by(|&a, &b| beam[a].abs().total_cmp(&beam[b].abs())).unwrap();
            assert_eq!(peak, 50 + BASE_DELAY, "{s:?}");
            assert!((beam[50 + BASE_DELAY] - 1.0).abs() < 1e-3);
            assert_eq!(raw[50 + BASE_DELAY], 1.0);
        }
    }

    #[test]
    fn steering_change_is_click_free() {
        let g = Geometry::UMA8;
        let len = 24000;
        let x = plane_wave_sine(&g, 0.0, 0.0, 1000.0, len);
        let mut bf = Beamformer::new(SR, g, steer(0.0));
        let (mut y, mut raw) = (vec![0.0; len], vec![0.0; len]);
        run(&mut bf, &x, 0, len / 2, &mut y, &mut raw);
        bf.set_target(g, steer(180.0));
        run(&mut bf, &x, len / 2, len, &mut y, &mut raw);
        let max_step = 2.0 * PI * 1000.0 / SR * 1.07;
        for n in 1..len {
            assert!((y[n] - y[n - 1]).abs() <= max_step, "Sprung bei n={n}");
        }
        assert_eq!(bf.current.1, steer(180.0), "Überblendung nicht abgeschlossen");
    }

    #[test]
    fn invalid_geometry_is_ignored() {
        let mut bf = Beamformer::new(SR, Geometry::UMA8, steer(0.0));
        let mut bad = Geometry::UMA8;
        bad.ring[0] = 0;
        bf.set_target(bad, steer(90.0));
        let x = vec![vec![0.0f32; 4000]; CHANNELS];
        let (mut y, mut raw) = (vec![0.0; 4000], vec![0.0; 4000]);
        run(&mut bf, &x, 0, 4000, &mut y, &mut raw);
        assert_eq!(bf.current.0, Geometry::UMA8);
        assert_eq!(bf.current.1, steer(0.0));
    }
}
```

In `plugin/src/lib.rs` nach `pub mod ladspa;` einfügen: `pub mod beam;`

- [ ] **Step 2: Tests laufen lassen, sie müssen fehlschlagen**

Run: `cargo test --manifest-path plugin/Cargo.toml beam`
Expected: FAIL mit `not implemented`

- [ ] **Step 3: Implementieren**

In `plugin/src/beam.rs` die `unimplemented!()`-Rümpfe ersetzen und `Taps::compute` sowie `Beamformer::apply` ergänzen:
```rust
impl Taps {
    fn compute(geom: &Geometry, steer: &Steering, sample_rate: f32) -> Taps {
        let mut t = Taps { offset: [0; CHANNELS], h: [[0.0; TAPS]; CHANNELS] };
        let pos = geom.positions();
        let (az, el) = (steer.azimuth_deg.to_radians(), steer.elevation_deg.to_radians());
        let u = [el.cos() * az.cos(), el.cos() * az.sin()];
        for ch in 0..CHANNELS {
            let weight = if steer.omni {
                if ch == geom.center { 1.0 } else { 0.0 }
            } else {
                1.0 / CHANNELS as f32
            };
            // Kanal ch hört die Welle um tau Samples früher; um genau so viel mehr verzögern.
            let tau = if steer.omni {
                0.0
            } else {
                (pos[ch][0] * u[0] + pos[ch][1] * u[1]) / SPEED_OF_SOUND * sample_rate
            };
            let d = (BASE_DELAY as f32 + tau).clamp((HALF - 1) as f32, (HIST - HALF) as f32);
            let (offset, h) = fractional_delay(d);
            t.offset[ch] = offset;
            for k in 0..TAPS {
                t.h[ch][k] = h[k] * weight;
            }
        }
        t
    }
}

pub fn fractional_delay(d: f32) -> (usize, [f32; TAPS]) {
    use std::f32::consts::PI;
    let offset = d.floor() as usize - (HALF - 1);
    let mut h = [0.0f32; TAPS];
    let mut sum = 0.0;
    for (k, v) in h.iter_mut().enumerate() {
        let t = (offset + k) as f32 - d;
        let sinc = if t.abs() < 1e-6 { 1.0 } else { (PI * t).sin() / (PI * t) };
        let window = 0.5 * (1.0 + (PI * t / HALF as f32).cos());
        *v = sinc * window;
        sum += *v;
    }
    for v in h.iter_mut() {
        *v /= sum;
    }
    (offset, h)
}

impl Beamformer {
    pub fn new(sample_rate: f32, geom: Geometry, steer: Steering) -> Self {
        let taps = Taps::compute(&geom, &steer, sample_rate);
        Beamformer {
            sample_rate,
            hist: [[0.0; HIST]; CHANNELS],
            pos: 0,
            current: (geom, steer),
            target: (geom, steer),
            fading_to: (geom, steer),
            taps,
            next_taps: taps,
            fade: None,
        }
    }

    pub fn latency(&self) -> usize {
        BASE_DELAY
    }

    pub fn set_target(&mut self, geom: Geometry, steer: Steering) {
        if geom.is_valid() {
            self.target = (geom, steer);
        }
    }

    fn apply(&self, taps: &Taps) -> f32 {
        let mut acc = 0.0;
        for ch in 0..CHANNELS {
            let base = self.pos + HIST - taps.offset[ch];
            let h = &taps.h[ch];
            let x = &self.hist[ch];
            for k in 0..TAPS {
                acc += h[k] * x[(base - k) & (HIST - 1)];
            }
        }
        acc
    }

    pub fn process(&mut self, input: &[&[f32]; CHANNELS], beam: &mut [f32], raw: &mut [f32]) {
        use std::f32::consts::PI;
        for n in 0..beam.len() {
            if self.fade.is_none() && self.target != self.current {
                self.fading_to = self.target;
                self.next_taps = Taps::compute(&self.fading_to.0, &self.fading_to.1, self.sample_rate);
                self.fade = Some(0);
            }
            self.pos = (self.pos + 1) & (HIST - 1);
            for ch in 0..CHANNELS {
                self.hist[ch][self.pos] = input[ch][n];
            }
            let mut y = self.apply(&self.taps);
            if let Some(i) = self.fade {
                let g = 0.5 - 0.5 * (PI * i as f32 / FADE_SAMPLES as f32).cos();
                y = y * (1.0 - g) + self.apply(&self.next_taps) * g;
                if i + 1 >= FADE_SAMPLES {
                    self.taps = self.next_taps;
                    self.current = self.fading_to;
                    self.fade = None;
                } else {
                    self.fade = Some(i + 1);
                }
            }
            beam[n] = y;
            raw[n] = self.hist[self.current.0.center][(self.pos + HIST - BASE_DELAY) & (HIST - 1)];
        }
    }
}
```

- [ ] **Step 4: Tests laufen lassen**

Run: `cargo test --manifest-path plugin/Cargo.toml beam`
Expected: 6 passed

- [ ] **Step 5: Stand prüfen, nicht committen**

Run: `git status --short` und berichten.

---

### Task 3: Hallunterdrückung (Rust)

**Files:**
- Create: `plugin/src/dereverb.rs`
- Modify: `plugin/src/lib.rs` (`pub mod dereverb;` ergänzen)
- Test: `plugin/src/dereverb.rs` (Rust-Tests)

**Interfaces:**
- Produces: `dereverb::{FFT_LEN = 1024, HOP = 256, LATENCY = 1024}`; `Dereverb::{new(sr), set_params(enabled: bool, strength: f32, t60: f32), process(&[f32], &mut [f32])}`. Latenz ist immer `LATENCY`, auch wenn deaktiviert.

- [ ] **Step 1: Modul mit Tests und leeren Rümpfen schreiben**

`plugin/src/dereverb.rs`:
```rust
//! Unterdrückung des späten Nachhalls (Lebart/Habets) in der STFT-Domäne.
//! Deaktiviert ist die Verstärkung 1: exakte Rekonstruktion mit fester Latenz.

use realfft::{num_complex::Complex, ComplexToReal, RealFftPlanner, RealToComplex};
use std::sync::Arc;

pub const FFT_LEN: usize = 1024;
pub const HOP: usize = 256;
/// Latenz in Samples, unabhängig davon, ob die Unterdrückung aktiv ist.
pub const LATENCY: usize = FFT_LEN;
const BINS: usize = FFT_LEN / 2 + 1;
const LATE_ONSET_S: f32 = 0.05;
const MAX_ONSET_FRAMES: usize = 32;
const PSD_SMOOTH: f32 = 0.6;
const GAIN_SMOOTH: f32 = 0.5;
const MAX_ATTENUATION_DB: f32 = 15.0;

pub struct Dereverb {
    sample_rate: f32,
    r2c: Arc<dyn RealToComplex<f32>>,
    c2r: Arc<dyn ComplexToReal<f32>>,
    window: Vec<f32>,
    input: Vec<f32>,
    in_pos: usize,
    hop_count: usize,
    output: Vec<f32>,
    time: usize,
    frame: Vec<f32>,
    spectrum: Vec<Complex<f32>>,
    scratch_fwd: Vec<Complex<f32>>,
    scratch_inv: Vec<Complex<f32>>,
    psd: Vec<f32>,
    psd_hist: Vec<Vec<f32>>,
    hist_pos: usize,
    onset_frames: usize,
    gain: Vec<f32>,
    enabled: bool,
    strength: f32,
    t60: f32,
}

impl Dereverb {
    pub fn new(sample_rate: f32) -> Self {
        unimplemented!()
    }

    pub fn set_params(&mut self, enabled: bool, strength: f32, t60: f32) {
        self.enabled = enabled;
        self.strength = strength.clamp(0.0, 1.0);
        self.t60 = t60.clamp(0.1, 1.5);
    }

    pub fn process(&mut self, input: &[f32], output: &mut [f32]) {
        unimplemented!()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const SR: f32 = 48000.0;

    fn noise(len: usize, seed: u32) -> Vec<f32> {
        let mut s = seed;
        (0..len)
            .map(|_| {
                s = s.wrapping_mul(1664525).wrapping_add(1013904223);
                (s >> 8) as f32 / (1u32 << 24) as f32 * 2.0 - 1.0
            })
            .collect()
    }

    fn convolve(x: &[f32], h: &[f32]) -> Vec<f32> {
        let n = (x.len() + h.len()).next_power_of_two();
        let mut planner = RealFftPlanner::<f32>::new();
        let (fwd, inv) = (planner.plan_fft_forward(n), planner.plan_fft_inverse(n));
        let (mut a, mut b) = (x.to_vec(), h.to_vec());
        a.resize(n, 0.0);
        b.resize(n, 0.0);
        let (mut sa, mut sb) = (fwd.make_output_vec(), fwd.make_output_vec());
        fwd.process(&mut a, &mut sa).unwrap();
        fwd.process(&mut b, &mut sb).unwrap();
        for (p, q) in sa.iter_mut().zip(sb.iter()) {
            *p = *p * *q / n as f32;
        }
        let last = sa.len() - 1;
        sa[0].im = 0.0;
        sa[last].im = 0.0;
        let mut out = inv.make_output_vec();
        inv.process(&mut sa, &mut out).unwrap();
        out.truncate(x.len());
        out
    }

    fn energy(x: &[f32]) -> f32 {
        x.iter().map(|v| v * v).sum()
    }

    #[test]
    fn disabled_is_exact_delay() {
        let mut d = Dereverb::new(SR);
        d.set_params(false, 1.0, 0.5);
        let x = noise(20000, 1);
        let mut y = vec![0.0; x.len()];
        d.process(&x, &mut y);
        for n in LATENCY..x.len() {
            assert!((y[n] - x[n - LATENCY]).abs() < 1e-4, "n={n}");
        }
    }

    #[test]
    fn enabled_reduces_reverb_tail_and_keeps_direct_sound() {
        let t60 = 0.6;
        let len = (1.5 * SR) as usize;
        let burst = (0.3 * SR) as usize;
        let dry: Vec<f32> = noise(len, 7).iter().enumerate().map(|(n, v)| if n < burst { *v } else { 0.0 }).collect();
        let ir_len = (0.8 * SR) as usize;
        let tail = noise(ir_len, 99);
        let mut ir: Vec<f32> = (0..ir_len)
            .map(|n| 0.3 * tail[n] * (-3.0 * std::f32::consts::LN_10 * n as f32 / (t60 * SR)).exp())
            .collect();
        ir[0] = 1.0;
        let wet = convolve(&dry, &ir);

        let mut d = Dereverb::new(SR);
        d.set_params(true, 1.0, t60);
        let mut y = vec![0.0; len];
        d.process(&wet, &mut y);
        let aligned = |a: usize, b: usize| energy(&y[a + LATENCY..b + LATENCY]);

        let (t0, t1) = (burst + (0.1 * SR) as usize, burst + (0.6 * SR) as usize);
        let tail_db = 10.0 * (energy(&wet[t0..t1]) / aligned(t0, t1)).log10();
        assert!(tail_db >= 6.0, "Nachhall nur um {tail_db} dB reduziert");

        let (b0, b1) = ((0.1 * SR) as usize, burst);
        let direct_db = 10.0 * (energy(&wet[b0..b1]) / aligned(b0, b1)).log10();
        assert!(direct_db <= 3.0, "Direktschall um {direct_db} dB gedämpft");
    }
}
```

In `plugin/src/lib.rs` ergänzen: `pub mod dereverb;`

- [ ] **Step 2: Tests laufen lassen, sie müssen fehlschlagen**

Run: `cargo test --manifest-path plugin/Cargo.toml dereverb`
Expected: FAIL mit `not implemented`

- [ ] **Step 3: Implementieren**

Rümpfe von `new` und `process` ersetzen und `process_frame` ergänzen:
```rust
    pub fn new(sample_rate: f32) -> Self {
        let mut planner = RealFftPlanner::<f32>::new();
        let r2c = planner.plan_fft_forward(FFT_LEN);
        let c2r = planner.plan_fft_inverse(FFT_LEN);
        // Wurzel-Hann (periodisch): Analyse und Synthese zusammen ergeben Hann,
        // bei 75 % Überlappung summiert sich Hann zu 2.
        let window = (0..FFT_LEN).map(|j| (std::f32::consts::PI * j as f32 / FFT_LEN as f32).sin()).collect();
        let onset_frames = ((LATE_ONSET_S * sample_rate / HOP as f32).round() as usize).clamp(1, MAX_ONSET_FRAMES);
        Dereverb {
            sample_rate,
            scratch_fwd: r2c.make_scratch_vec(),
            scratch_inv: c2r.make_scratch_vec(),
            spectrum: r2c.make_output_vec(),
            r2c,
            c2r,
            window,
            input: vec![0.0; FFT_LEN],
            in_pos: 0,
            hop_count: 0,
            output: vec![0.0; 2 * FFT_LEN],
            time: FFT_LEN,
            frame: vec![0.0; FFT_LEN],
            psd: vec![0.0; BINS],
            psd_hist: vec![vec![0.0; BINS]; MAX_ONSET_FRAMES + 1],
            hist_pos: 0,
            onset_frames,
            gain: vec![1.0; BINS],
            enabled: false,
            strength: 0.6,
            t60: 0.5,
        }
    }

    pub fn process(&mut self, input: &[f32], output: &mut [f32]) {
        let ring = self.output.len();
        for (x, y) in input.iter().zip(output.iter_mut()) {
            self.input[self.in_pos] = *x;
            self.in_pos = (self.in_pos + 1) % FFT_LEN;
            self.hop_count += 1;
            if self.hop_count == HOP {
                self.hop_count = 0;
                self.process_frame();
            }
            // Alle Frames, die Zeitpunkt time − FFT_LEN abdecken, sind fertig.
            let idx = (self.time - FFT_LEN) % ring;
            *y = self.output[idx];
            self.output[idx] = 0.0;
            self.time += 1;
        }
    }

    fn process_frame(&mut self) {
        for j in 0..FFT_LEN {
            self.frame[j] = self.input[(self.in_pos + j) % FFT_LEN] * self.window[j];
        }
        let _ = self.r2c.process_with_scratch(&mut self.frame, &mut self.spectrum, &mut self.scratch_fwd);

        let delta = 3.0 * std::f32::consts::LN_10 / self.t60;
        let decay = (-2.0 * delta * (self.onset_frames * HOP) as f32 / self.sample_rate).exp();
        let slots = MAX_ONSET_FRAMES + 1;
        // hist_pos = letzter gespeicherter Frame (1 Frame alt) → D Frames alt = hist_pos + 1 − D
        let old = (self.hist_pos + 1 + slots - self.onset_frames) % slots;
        let floor = 10f32.powf(-self.strength * MAX_ATTENUATION_DB / 20.0);
        for k in 0..BINS {
            let power = self.spectrum[k].norm_sqr();
            self.psd[k] = PSD_SMOOTH * self.psd[k] + (1.0 - PSD_SMOOTH) * power;
            let late = decay * self.psd_hist[old][k];
            let target = if self.enabled {
                (1.0 - late / (self.psd[k] + 1e-12)).max(0.0).sqrt().max(floor)
            } else {
                1.0
            };
            self.gain[k] = GAIN_SMOOTH * self.gain[k] + (1.0 - GAIN_SMOOTH) * target;
            self.spectrum[k] *= self.gain[k];
        }
        self.hist_pos = (self.hist_pos + 1) % slots;
        self.psd_hist[self.hist_pos].copy_from_slice(&self.psd);

        self.spectrum[0].im = 0.0;
        self.spectrum[BINS - 1].im = 0.0;
        let _ = self.c2r.process_with_scratch(&mut self.spectrum, &mut self.frame, &mut self.scratch_inv);
        let ring = self.output.len();
        let scale = 0.5 / FFT_LEN as f32;
        let start = self.time + 1 - FFT_LEN;
        for j in 0..FFT_LEN {
            self.output[(start + j) % ring] += self.frame[j] * self.window[j] * scale;
        }
    }
```

- [ ] **Step 4: Tests laufen lassen**

Run: `cargo test --manifest-path plugin/Cargo.toml dereverb`
Expected: 2 passed. Falls `direct_db` knapp über 3 dB liegt: `PSD_SMOOTH` auf 0.7 erhöhen und erneut prüfen; Grenzwerte im Test nicht lockern, ohne den Nutzer zu fragen.

- [ ] **Step 5: Stand prüfen, nicht committen**

Run: `git status --short` und berichten.

---

### Task 4: LADSPA-Plugin `uma8_beam`, Array-Geometrie in Python, Plugin-Tests

**Files:**
- Modify: `plugin/src/plugins.rs` (BeamPlugin ergänzen, `all()` erweitern)
- Create: `uma8_callmic/__init__.py`, `uma8_callmic/array.py`, `tests/sim.py`
- Test: `tests/test_array.py`, `tests/test_beam_plugin.py`

**Interfaces:**
- Consumes: `beam::*`, `dereverb::{Dereverb, LATENCY}` aus Task 2/3; `ladspa_host.Plugin` aus Task 1.
- Produces: LADSPA-Label `uma8_beam` mit Ports in genau dieser Reihenfolge: `In 0`…`In 6`, `Beam Out`, `Raw Out`, `Azimuth (deg)`, `Elevation (deg)`, `Mode`, `Center Channel`, `Ring 0`…`Ring 5`, `Ring Offset (deg)`, `Radius (mm)`, `Gain (dB)`, `Dereverb`, `Dereverb Strength`, `Dereverb T60 (s)`, `Raw Extra Delay (samples)`. Latenz „Beam Out“ = 1049 Samples, „Raw Out“ = 1049 + Raw Extra Delay.
- Produces (Python): `uma8_callmic.array.ArrayGeometry(center=0, ring=(1,6,5,4,3,2), ring_offset_deg=90.0, radius_m=0.043)` mit `.is_valid()`, `.positions() -> np.ndarray (7, 3)`; `UMA8 = ArrayGeometry()`. `tests/sim.py`: `plane_wave(signal, positions, az_deg, el_deg, sr=48000) -> (n, 7)`, `band_noise(n, lo, hi, sr=48000, seed=0)`, `diffuse_noise(n, positions, sr=48000, n_sources=150, seed=0)`.

- [ ] **Step 1: Python-Geometrie mit Test schreiben**

`uma8_callmic/__init__.py`:
```python
"""UMA-8 Call Mic: Beamforming und Rauschunterdrückung für das miniDSP UMA-8."""
```

`uma8_callmic/array.py`:
```python
"""Array-Geometrie. Muss exakt Geometry::positions() in plugin/src/beam.rs entsprechen."""
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ArrayGeometry:
    center: int = 0
    ring: tuple[int, ...] = (1, 6, 5, 4, 3, 2)
    ring_offset_deg: float = 90.0
    radius_m: float = 0.043

    def is_valid(self) -> bool:
        channels = [self.center, *self.ring]
        return len(self.ring) == 6 and sorted(channels) == list(range(7)) and self.radius_m > 0

    def positions(self) -> np.ndarray:
        """(7, 3): Position je Kanal in Metern, z = 0."""
        p = np.zeros((7, 3))
        for k, ch in enumerate(self.ring):
            a = np.radians(self.ring_offset_deg + 60.0 * k)
            p[ch, 0] = self.radius_m * np.cos(a)
            p[ch, 1] = self.radius_m * np.sin(a)
        return p


UMA8 = ArrayGeometry()
```

`tests/test_array.py`:
```python
import numpy as np

from uma8_callmic.array import UMA8, ArrayGeometry


def test_uma8_positions_match_odas():
    p = UMA8.positions()
    np.testing.assert_allclose(p[0], [0, 0, 0], atol=1e-9)
    np.testing.assert_allclose(p[1], [0.0, 0.043, 0], atol=1e-3)
    np.testing.assert_allclose(p[2], [0.037, 0.021, 0], atol=1e-3)
    np.testing.assert_allclose(p[6], [-0.037, 0.021, 0], atol=1e-3)


def test_validity():
    assert UMA8.is_valid()
    assert not ArrayGeometry(center=1).is_valid()
    assert not ArrayGeometry(ring=(1, 2, 3)).is_valid()
```

Run: `python -m pytest tests/test_array.py -v` → Expected: 2 passed

- [ ] **Step 2: Simulationshilfen schreiben**

`tests/sim.py`:
```python
"""Testsignale für das Array: ebene Wellen und diffuses Rauschen."""
import numpy as np

C_SOUND = 343.0


def plane_wave(signal: np.ndarray, positions: np.ndarray, az_deg: float, el_deg: float, sr: int = 48000) -> np.ndarray:
    """Kanal i erhält `signal` um (p_i·u)/c vorgezogen (Bruchteil-Verschiebung per FFT)."""
    az, el = np.radians(az_deg), np.radians(el_deg)
    u = np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
    lead = positions @ u / C_SOUND
    n = len(signal)
    spec = np.fft.rfft(signal)
    f = np.fft.rfftfreq(n, 1 / sr)
    return np.stack([np.fft.irfft(spec * np.exp(2j * np.pi * f * l), n) for l in lead], axis=1)


def band_noise(n: int, lo: float, hi: float, sr: int = 48000, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / sr)
    spec[(f < lo) | (f > hi)] = 0
    x = np.fft.irfft(spec, n)
    return x / np.std(x)


def diffuse_noise(n: int, positions: np.ndarray, sr: int = 48000, n_sources: int = 150, seed: int = 0) -> np.ndarray:
    """Summe unabhängiger ebener Wellen aus gleichverteilten Richtungen der Kugel."""
    rng = np.random.default_rng(seed)
    out = np.zeros((n, positions.shape[0]))
    for _ in range(n_sources):
        v = rng.standard_normal(3)
        v /= np.linalg.norm(v)
        az = np.degrees(np.arctan2(v[1], v[0]))
        el = np.degrees(np.arcsin(v[2]))
        out += plane_wave(rng.standard_normal(n), positions, az, el, sr)
    return out / np.sqrt(n_sources)
```

- [ ] **Step 3: Fehlschlagende Plugin-Tests schreiben**

`tests/test_beam_plugin.py`:
```python
import time

import numpy as np
import pytest

from ladspa_host import Plugin
from sim import band_noise, plane_wave
from uma8_callmic.array import UMA8

BEAM_LATENCY = 1049
PORTS = (
    [f"In {i}" for i in range(7)] + ["Beam Out", "Raw Out", "Azimuth (deg)", "Elevation (deg)", "Mode",
    "Center Channel"] + [f"Ring {k}" for k in range(6)] + ["Ring Offset (deg)", "Radius (mm)", "Gain (dB)",
    "Dereverb", "Dereverb Strength", "Dereverb T60 (s)", "Raw Extra Delay (samples)"]
)


def make(plugin_so, az=0.0, el=0.0, mode=0.0, dereverb=0.0, raw_extra=0.0, block=512):
    p = Plugin(plugin_so, "uma8_beam", block=block)
    p.set("Center Channel", UMA8.center)
    for k, ch in enumerate(UMA8.ring):
        p.set(f"Ring {k}", ch)
    p.set("Ring Offset (deg)", UMA8.ring_offset_deg)
    p.set("Radius (mm)", UMA8.radius_m * 1000)
    p.set("Azimuth (deg)", az)
    p.set("Elevation (deg)", el)
    p.set("Mode", mode)
    p.set("Gain (dB)", 0.0)
    p.set("Dereverb", dereverb)
    p.set("Dereverb Strength", 0.6)
    p.set("Dereverb T60 (s)", 0.5)
    p.set("Raw Extra Delay (samples)", raw_extra)
    return p


def channels(x: np.ndarray) -> dict:
    return {f"In {i}": x[:, i].astype(np.float32) for i in range(7)}


def test_ports(plugin_so):
    p = Plugin(plugin_so, "uma8_beam")
    assert p.names == PORTS
    p.close()


@pytest.mark.parametrize("dereverb", [0.0, 1.0])
def test_latency_is_constant(plugin_so, dereverb):
    x = np.zeros((4000, 7))
    x[500, :] = 1.0  # Quelle senkrecht über dem Array
    p = make(plugin_so, el=90.0, dereverb=dereverb, raw_extra=100)
    out = p.process(channels(x))
    p.close()
    assert int(np.argmax(np.abs(out["Beam Out"]))) == 500 + BEAM_LATENCY
    assert int(np.argmax(np.abs(out["Raw Out"]))) == 500 + BEAM_LATENCY + 100


def test_directivity_2_to_6_khz(plugin_so):
    x = plane_wave(0.1 * band_noise(48000, 2000, 6000), UMA8.positions(), 90.0, 0.0)
    powers = {}
    for az in (90.0, 270.0):
        p = make(plugin_so, az=az)
        y = p.process(channels(x))["Beam Out"][4800:]
        p.close()
        powers[az] = np.mean(y.astype(np.float64) ** 2)
    assert 10 * np.log10(powers[90.0] / powers[270.0]) >= 6.0


def test_steering_change_is_click_free(plugin_so):
    t = np.arange(48000) / 48000
    x = plane_wave(np.sin(2 * np.pi * 1000 * t), UMA8.positions(), 0.0, 0.0)
    p = make(plugin_so, az=0.0)
    a = p.process(channels(x[:24000]))["Beam Out"]
    p.set("Azimuth (deg)", 180.0)
    b = p.process(channels(x[24000:]))["Beam Out"]
    p.close()
    y = np.concatenate([a, b])[BEAM_LATENCY + 100:]
    assert np.max(np.abs(np.diff(y))) <= 2 * np.pi * 1000 / 48000 * 1.1


def test_block_size_does_not_change_output(plugin_so):
    x = plane_wave(0.1 * band_noise(12000, 300, 8000, seed=5), UMA8.positions(), 45.0, 20.0)
    ref = make(plugin_so, az=45.0, dereverb=1.0, block=512)
    y_ref = ref.process(channels(x))
    ref.close()
    for block in (1, 7, 4096):
        p = make(plugin_so, az=45.0, dereverb=1.0, block=block)
        y = p.process(channels(x))
        p.close()
        np.testing.assert_allclose(y["Beam Out"], y_ref["Beam Out"], atol=1e-6)
        np.testing.assert_allclose(y["Raw Out"], y_ref["Raw Out"], atol=1e-6)


def test_silence_and_full_scale(plugin_so):
    p = make(plugin_so, dereverb=1.0)
    silent = p.process(channels(np.zeros((9600, 7))))
    loud = p.process(channels(np.ones((9600, 7)) * np.sign(np.sin(np.arange(9600) * 0.3))[:, None]))
    p.close()
    assert np.all(silent["Beam Out"] == 0) and np.all(silent["Raw Out"] == 0)
    assert np.all(np.isfinite(loud["Beam Out"])) and np.all(np.isfinite(loud["Raw Out"]))


def test_cpu_budget(plugin_so):
    x = np.random.default_rng(1).standard_normal((480000, 7)) * 0.01
    ins = channels(x)
    p = make(plugin_so, dereverb=1.0)
    start = time.perf_counter()
    p.process(ins)
    elapsed = time.perf_counter() - start
    p.close()
    assert elapsed < 0.2, f"10 s Audio brauchten {elapsed:.3f} s"
```

- [ ] **Step 4: Tests laufen lassen, sie müssen fehlschlagen**

Run: `python -m pytest tests/test_beam_plugin.py -v`
Expected: FAIL mit `KeyError: "Label 'uma8_beam' nicht in …"`

- [ ] **Step 5: BeamPlugin implementieren**

In `plugin/src/plugins.rs` die Importe erweitern und das Plugin ergänzen:
```rust
use crate::beam::{Beamformer, Geometry, Steering, CHANNELS};
use crate::dereverb::{self, Dereverb};

const MAX_RAW_EXTRA: usize = 4800;

// Port-Indizes uma8_beam
const IN0: usize = 0;
const BEAM_OUT: usize = 7;
const RAW_OUT: usize = 8;
const AZIMUTH: usize = 9;
const ELEVATION: usize = 10;
const MODE: usize = 11;
const CENTER: usize = 12;
const RING0: usize = 13;
const RING_OFFSET: usize = 19;
const RADIUS: usize = 20;
const GAIN: usize = 21;
const DEREVERB: usize = 22;
const DR_STRENGTH: usize = 23;
const DR_T60: usize = 24;
const RAW_EXTRA: usize = 25;

pub struct BeamPlugin {
    beam: Beamformer,
    dereverb: Dereverb,
    input: [Vec<f32>; CHANNELS],
    beam_buf: Vec<f32>,
    raw_buf: Vec<f32>,
    out_buf: Vec<f32>,
    raw_delay: Vec<f32>,
    raw_pos: usize,
    gain: f32,
}

impl Plugin for BeamPlugin {
    const UNIQUE_ID: c_ulong = 8009729;
    const LABEL: &'static str = "uma8_beam";
    const NAME: &'static str = "UMA-8 Beamformer";
    const PORTS: &'static [PortSpec] = &[
        PortSpec::audio_in("In 0"),
        PortSpec::audio_in("In 1"),
        PortSpec::audio_in("In 2"),
        PortSpec::audio_in("In 3"),
        PortSpec::audio_in("In 4"),
        PortSpec::audio_in("In 5"),
        PortSpec::audio_in("In 6"),
        PortSpec::audio_out("Beam Out"),
        PortSpec::audio_out("Raw Out"),
        PortSpec::control("Azimuth (deg)", 0.0, 360.0, HINT_DEFAULT_0),
        PortSpec::control("Elevation (deg)", 0.0, 90.0, HINT_DEFAULT_0),
        PortSpec::control("Mode", 0.0, 1.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Center Channel", 0.0, 6.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Ring 0", 0.0, 6.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Ring 1", 0.0, 6.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Ring 2", 0.0, 6.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Ring 3", 0.0, 6.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Ring 4", 0.0, 6.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Ring 5", 0.0, 6.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Ring Offset (deg)", 0.0, 360.0, HINT_DEFAULT_0),
        PortSpec::control("Radius (mm)", 20.0, 60.0, HINT_DEFAULT_MIDDLE),
        PortSpec::control("Gain (dB)", 0.0, 60.0, HINT_DEFAULT_0),
        PortSpec::control("Dereverb", 0.0, 1.0, HINT_TOGGLED | HINT_DEFAULT_0),
        PortSpec::control("Dereverb Strength", 0.0, 1.0, HINT_DEFAULT_MIDDLE),
        PortSpec::control("Dereverb T60 (s)", 0.1, 1.5, HINT_DEFAULT_MIDDLE),
        PortSpec::control("Raw Extra Delay (samples)", 0.0, 4800.0, HINT_INTEGER | HINT_DEFAULT_0),
    ];

    fn new(sample_rate: f32) -> Self {
        let steer = Steering { azimuth_deg: 0.0, elevation_deg: 0.0, omni: false };
        BeamPlugin {
            beam: Beamformer::new(sample_rate, Geometry::UMA8, steer),
            dereverb: Dereverb::new(sample_rate),
            input: std::array::from_fn(|_| vec![0.0; CHUNK]),
            beam_buf: vec![0.0; CHUNK],
            raw_buf: vec![0.0; CHUNK],
            out_buf: vec![0.0; CHUNK],
            raw_delay: vec![0.0; dereverb::LATENCY + MAX_RAW_EXTRA + 1],
            raw_pos: 0,
            gain: 1.0,
        }
    }

    fn run(&mut self, ports: &Ports, n: usize) {
        let channel = |i: usize| ports.control(i, 0.0).round().clamp(0.0, 6.0) as usize;
        let geometry = Geometry {
            center: channel(CENTER),
            ring: std::array::from_fn(|k| channel(RING0 + k)),
            ring_offset_deg: ports.control(RING_OFFSET, 90.0),
            radius_m: ports.control(RADIUS, 43.0).clamp(20.0, 60.0) / 1000.0,
        };
        let steering = Steering {
            azimuth_deg: ports.control(AZIMUTH, 0.0),
            elevation_deg: ports.control(ELEVATION, 0.0).clamp(0.0, 90.0),
            omni: ports.control(MODE, 0.0) >= 0.5,
        };
        self.beam.set_target(geometry, steering);
        self.dereverb.set_params(
            ports.control(DEREVERB, 0.0) >= 0.5,
            ports.control(DR_STRENGTH, 0.6),
            ports.control(DR_T60, 0.5),
        );
        let gain_target = 10f32.powf(ports.control(GAIN, 0.0).clamp(0.0, 60.0) / 20.0);
        let raw_extra = (ports.control(RAW_EXTRA, 0.0).round().max(0.0) as usize).min(MAX_RAW_EXTRA);
        let raw_total = dereverb::LATENCY + raw_extra;
        let dlen = self.raw_delay.len();

        let mut off = 0;
        while off < n {
            let m = (n - off).min(CHUNK);
            for c in 0..CHANNELS {
                ports.read(IN0 + c, off, &mut self.input[c][..m]);
            }
            let refs: [&[f32]; CHANNELS] = std::array::from_fn(|c| &self.input[c][..m]);
            self.beam.process(&refs, &mut self.beam_buf[..m], &mut self.raw_buf[..m]);
            self.dereverb.process(&self.beam_buf[..m], &mut self.out_buf[..m]);
            for i in 0..m {
                self.gain += (gain_target - self.gain) * 0.001;
                self.raw_delay[self.raw_pos] = self.raw_buf[i];
                let delayed = self.raw_delay[(self.raw_pos + dlen - raw_total) % dlen];
                self.raw_pos = (self.raw_pos + 1) % dlen;
                self.out_buf[i] *= self.gain;
                self.raw_buf[i] = delayed * self.gain;
            }
            ports.write(BEAM_OUT, off, &self.out_buf[..m]);
            ports.write(RAW_OUT, off, &self.raw_buf[..m]);
            off += m;
        }
    }
}
```

`all()` ersetzen:
```rust
pub fn all() -> Vec<Descriptor> {
    vec![descriptor::<BeamPlugin>(), descriptor::<LimitPlugin>()]
}
```

Hinweis: Die Blockgrößen-Unabhängigkeit hängt davon ab, dass Controls pro `run()` gelesen werden und sich im Test innerhalb eines Laufs nicht ändern; die Verstärkungsglättung läuft pro Sample.

- [ ] **Step 6: Alle Tests laufen lassen**

Run: `cargo test --manifest-path plugin/Cargo.toml && python -m pytest -v`
Expected: alle Rust-Tests grün; pytest: `test_array` 2, `test_limit_plugin` 3, `test_beam_plugin` 8 passed.
Zusätzlich: `analyseplugin plugin/target/release/libuma8_beam.so` zeigt beide Plugins.

- [ ] **Step 7: Stand prüfen, nicht committen**

Run: `git status --short` und berichten.

---

### Task 5: Konstanten und gemessene Latenz von DeepFilterNet

**Files:**
- Create: `uma8_callmic/constants.py`
- Test: `tests/test_dfn_latency.py`

**Interfaces:**
- Produces: `uma8_callmic.constants` mit `SAMPLE_RATE, RAW_DEVICE, CAPTURE_NODE, SOURCE_NODE, SERVICE, DFN_PLUGIN, BEAM_PLUGIN, CONFIG_FILE, CHAIN_CONF, STATE_DIR, AUTOSTART_FILE, LAUNCHER, REPO_DIR, BEAM_LATENCY = 1049, DFN_LATENCY` (int, gemessen).

- [ ] **Step 1: Messfunktion und Test schreiben**

`tests/test_dfn_latency.py`:
```python
from pathlib import Path

import numpy as np
import pytest

from ladspa_host import Plugin

DFN = "/usr/lib/ladspa/libdeep_filter_ladspa.so"


def measure(limit_db: float) -> tuple[int, float]:
    """Verzögerung (Samples) und Korrelationsstärke von deep_filter_mono."""
    p = Plugin(DFN, "deep_filter_mono", block=480)
    p.set("Attenuation Limit (dB)", limit_db)
    x = (0.1 * np.random.default_rng(3).standard_normal(48000 * 4)).astype(np.float32)
    y = p.process({"Audio In": x})["Audio Out"]
    p.close()
    a, b = x[48000:].astype(np.float64), y[48000:].astype(np.float64)
    n = 2 * len(a)
    corr = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)
    lag = int(np.argmax(corr[:48000]))
    strength = corr[lag] / np.sqrt(np.sum(a * a) * np.sum(b * b))
    return lag, float(strength)


@pytest.mark.skipif(not Path(DFN).exists(), reason="DeepFilterNet nicht installiert")
def test_dfn_latency_constant_matches_measurement():
    from uma8_callmic.constants import DFN_LATENCY

    lag6, s6 = measure(6.0)
    lag12, s12 = measure(12.0)
    assert s6 > 0.3 and s12 > 0.3, f"Korrelation zu schwach ({s6:.2f}/{s12:.2f})"
    assert lag6 == lag12, "Latenz hängt von der Dämpfung ab"
    assert lag6 == DFN_LATENCY
```

- [ ] **Step 2: Latenz messen**

Run: `cd /home/tesla/githubprojects/uma8-callmic && python -c "import sys; sys.path[:0]=['.','tests']; from test_dfn_latency import measure; print(measure(6.0), measure(12.0))"`
Expected: zwei Tupel mit gleicher Verzögerung und Korrelation > 0.3, z. B. `(960, 0.9) (960, 0.8)`. Die gemessene Zahl ist `DFN_LATENCY`. Weichen die Verzögerungen ab oder ist die Korrelation < 0.3: anhalten und dem Nutzer die Messwerte berichten.

- [ ] **Step 3: `constants.py` mit dem Messwert schreiben**

`uma8_callmic/constants.py` (bei `DFN_LATENCY` den in Step 2 gemessenen Wert eintragen):
```python
"""Pfade, Knotennamen und gemessene Latenzen."""
from pathlib import Path

SAMPLE_RATE = 48000
RAW_DEVICE = "alsa_input.usb-miniDSP_micArray_RAW_SPK-00.analog-surround-71"
CAPTURE_NODE = "uma8_callmic_capture"
SOURCE_NODE = "uma8_callmic"
SERVICE = "uma8-callmic-chain.service"

DFN_PLUGIN = Path("/usr/lib/ladspa/libdeep_filter_ladspa.so")
BEAM_PLUGIN = Path.home() / ".local/lib/ladspa/libuma8_beam.so"
CONFIG_FILE = Path.home() / ".config/uma8-callmic/config.toml"
CHAIN_CONF = Path.home() / ".config/pipewire/uma8-callmic.conf"
STATE_DIR = Path.home() / ".local/state/uma8-callmic"
AUTOSTART_FILE = Path.home() / ".config/autostart/uma8-callmic.desktop"
LAUNCHER = Path.home() / ".local/bin/uma8-callmic"
REPO_DIR = Path(__file__).resolve().parent.parent

#: Latenz von uma8_beam „Beam Out“: BASE_DELAY 25 + Dereverb 1024
BEAM_LATENCY = 1049
#: Latenz von deep_filter_mono, gemessen mit tests/test_dfn_latency.py
DFN_LATENCY = 0  # durch den Messwert aus Task 5, Step 2 ersetzen
```

- [ ] **Step 4: Test laufen lassen**

Run: `python -m pytest tests/test_dfn_latency.py -v`
Expected: 1 passed

- [ ] **Step 5: Stand prüfen, nicht committen**

Run: `git status --short` und berichten (inkl. gemessener Latenz in ms).

---

### Task 6: Einstellungen und Abbildung auf Plugin-Controls

**Files:**
- Create: `uma8_callmic/config.py`, `uma8_callmic/params.py`
- Test: `tests/test_config.py`, `tests/test_params.py`

**Interfaces:**
- Consumes: `ArrayGeometry` (Task 4), `constants.DFN_LATENCY` (Task 5).
- Produces: `config.Config` (Felder siehe Code), `config.DIRECTION_MODES`, `config.load(path) -> LoadResult(config, warnings, broken)`, `config.save(cfg, path, broken=False)`, `Config.geometry() -> ArrayGeometry`; `params.geometry_params(cfg)`, `params.steering_params(cfg, tracked_azimuth=None)`, `params.processing_params(cfg)`, `params.mix_params(active)`, `params.all_params(cfg, tracked_azimuth=None)` – alle `dict[str, float]` mit Schlüsseln `"<knoten>:<control>"` (Knoten `beam`, `dfn`, `mix`, `limit`).

- [ ] **Step 1: Fehlschlagende Tests schreiben**

`tests/test_config.py`:
```python
from uma8_callmic.config import Config, load, save


def test_missing_file_gives_defaults(tmp_path):
    res = load(tmp_path / "config.toml")
    assert res.config == Config() and res.warnings == [] and not res.broken


def test_roundtrip(tmp_path):
    path = tmp_path / "config.toml"
    cfg = Config(active=False, direction_mode="tracking", calibrated=True, calibrated_azimuth=215.0,
                 gain_db=36.0, ring=[2, 3, 4, 5, 6, 1], dereverb=True)
    save(cfg, path)
    res = load(path)
    assert res.config == cfg and res.warnings == []


def test_invalid_values_fall_back_with_warning(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('gain_db = 500.0\ndirection_mode = "sideways"\nactive = "ja"\nmanual_azimuth = 12\n')
    res = load(path)
    assert res.config.gain_db == Config().gain_db
    assert res.config.direction_mode == "calibrated"
    assert res.config.active is True
    assert res.config.manual_azimuth == 12.0
    assert len(res.warnings) == 3


def test_invalid_ring_resets_geometry(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("center_channel = 1\nring = [1, 2, 3, 4, 5, 6]\n")
    res = load(path)
    assert res.config.center_channel == 0 and res.config.ring == [1, 6, 5, 4, 3, 2]
    assert any("Kanalzuordnung" in w for w in res.warnings)


def test_broken_file_is_backed_up_on_save(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("das ist [kein toml")
    res = load(path)
    assert res.broken and res.config == Config()
    save(res.config, path, broken=True)
    assert (tmp_path / "config.toml.broken").read_text() == "das ist [kein toml"
    assert load(path).config == Config()
```

`tests/test_params.py`:
```python
from uma8_callmic.config import Config
from uma8_callmic.constants import DFN_LATENCY
from uma8_callmic.params import all_params, mix_params, steering_params


def test_all_params_cover_every_node():
    p = all_params(Config())
    assert p["beam:Center Channel"] == 0.0
    assert [p[f"beam:Ring {k}"] for k in range(6)] == [1.0, 6.0, 5.0, 4.0, 3.0, 2.0]
    assert p["beam:Radius (mm)"] == 43.0
    assert p["beam:Raw Extra Delay (samples)"] == float(DFN_LATENCY)
    assert p["dfn:Attenuation Limit (dB)"] == 30.0
    assert p["limit:Ceiling (dB)"] == -1.0
    assert p["beam:Gain (dB)"] == 30.0
    assert {k.split(":")[0] for k in p} == {"beam", "dfn", "mix", "limit"}


def test_steering_modes():
    cfg = Config(calibrated_azimuth=200.0, calibrated_elevation=25.0, manual_azimuth=10.0)
    assert steering_params(cfg)["beam:Azimuth (deg)"] == 200.0
    cfg.direction_mode = "manual"
    assert steering_params(cfg)["beam:Azimuth (deg)"] == 10.0
    cfg.direction_mode = "tracking"
    assert steering_params(cfg)["beam:Azimuth (deg)"] == 200.0
    assert steering_params(cfg, tracked_azimuth=370.0)["beam:Azimuth (deg)"] == 10.0
    cfg.direction_mode = "omni"
    assert steering_params(cfg)["beam:Mode"] == 1.0


def test_mix_params():
    assert mix_params(True) == {"mix:Gain 1": 1.0, "mix:Gain 2": 0.0}
    assert mix_params(False) == {"mix:Gain 1": 0.0, "mix:Gain 2": 1.0}
```

- [ ] **Step 2: Tests laufen lassen, sie müssen fehlschlagen**

Run: `python -m pytest tests/test_config.py tests/test_params.py -v`
Expected: FAIL mit `ModuleNotFoundError: No module named 'uma8_callmic.config'`

- [ ] **Step 3: `config.py` implementieren**

```python
"""Einstellungen in ~/.config/uma8-callmic/config.toml."""
from __future__ import annotations

import shutil
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .array import ArrayGeometry

DIRECTION_MODES = ("calibrated", "manual", "tracking", "omni")
DEFAULT_RING = [1, 6, 5, 4, 3, 2]


@dataclass
class Config:
    active: bool = True
    direction_mode: str = "calibrated"
    calibrated: bool = False
    calibrated_azimuth: float = 0.0
    calibrated_elevation: float = 20.0
    manual_azimuth: float = 0.0
    dereverb: bool = False
    dereverb_strength: float = 0.6
    dereverb_t60: float = 0.5
    noise_reduction_db: float = 30.0
    gain_db: float = 30.0
    ceiling_db: float = -1.0
    autostart: bool = True
    geometry_checked: bool = False
    center_channel: int = 0
    ring: list[int] = field(default_factory=lambda: list(DEFAULT_RING))
    ring_offset_deg: float = 90.0
    radius_mm: float = 43.0

    def geometry(self) -> ArrayGeometry:
        return ArrayGeometry(self.center_channel, tuple(self.ring), self.ring_offset_deg, self.radius_mm / 1000.0)


_RANGES = {
    "calibrated_azimuth": (0.0, 360.0), "calibrated_elevation": (0.0, 90.0), "manual_azimuth": (0.0, 360.0),
    "dereverb_strength": (0.0, 1.0), "dereverb_t60": (0.1, 1.5), "noise_reduction_db": (0.0, 100.0),
    "gain_db": (0.0, 60.0), "ceiling_db": (-12.0, 0.0), "ring_offset_deg": (0.0, 360.0), "radius_mm": (20.0, 60.0),
}


@dataclass
class LoadResult:
    config: Config
    warnings: list[str]
    broken: bool = False


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check(name: str, value, default):
    """Geprüfter Wert oder None, wenn ungültig."""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else None
    if isinstance(default, float):
        if not (_is_int(value) or isinstance(value, float)):
            return None
        lo, hi = _RANGES[name]
        return float(value) if lo <= value <= hi else None
    if isinstance(default, int):
        return value if _is_int(value) and 0 <= value <= 6 else None
    if isinstance(default, str):
        return value if value in DIRECTION_MODES else None
    if isinstance(default, list):
        ok = isinstance(value, list) and len(value) == 6 and all(_is_int(v) for v in value)
        return list(value) if ok else None
    return None


def load(path: Path) -> LoadResult:
    if not path.exists():
        return LoadResult(Config(), [])
    try:
        raw = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as e:
        return LoadResult(Config(), [f"Einstellungen unlesbar, Standardwerte aktiv: {e}"], broken=True)
    cfg, warnings = Config(), []
    for f in fields(Config):
        if f.name not in raw:
            continue
        checked = _check(f.name, raw[f.name], getattr(cfg, f.name))
        if checked is None:
            warnings.append(f"Ungültiger Wert für {f.name}: {raw[f.name]!r}, Standard wird verwendet")
        else:
            setattr(cfg, f.name, checked)
    if not cfg.geometry().is_valid():
        warnings.append("Ungültige Kanalzuordnung, Standard wird verwendet")
        cfg.center_channel, cfg.ring = 0, list(DEFAULT_RING)
    return LoadResult(cfg, warnings)


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    raise TypeError(f"nicht serialisierbar: {v!r}")


def save(cfg: Config, path: Path, broken: bool = False) -> None:
    """Speichert atomar; eine zuvor unlesbare Datei wird als .toml.broken gesichert."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if broken and path.exists():
        shutil.copy2(path, path.with_suffix(".toml.broken"))
    text = "# uma8-callmic Einstellungen\n" + "".join(f"{k} = {_toml(v)}\n" for k, v in asdict(cfg).items())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)
```

- [ ] **Step 4: `params.py` implementieren**

```python
"""Einstellungen → Controls der Filterkette („<knoten>:<control>“)."""
from .config import Config
from .constants import DFN_LATENCY


def geometry_params(cfg: Config) -> dict[str, float]:
    p = {"beam:Center Channel": float(cfg.center_channel)}
    for k, ch in enumerate(cfg.ring):
        p[f"beam:Ring {k}"] = float(ch)
    p["beam:Ring Offset (deg)"] = float(cfg.ring_offset_deg)
    p["beam:Radius (mm)"] = float(cfg.radius_mm)
    p["beam:Raw Extra Delay (samples)"] = float(DFN_LATENCY)
    return p


def steering_params(cfg: Config, tracked_azimuth: float | None = None) -> dict[str, float]:
    if cfg.direction_mode == "omni":
        return {"beam:Mode": 1.0, "beam:Azimuth (deg)": 0.0, "beam:Elevation (deg)": 0.0}
    if cfg.direction_mode == "manual":
        az = cfg.manual_azimuth
    elif cfg.direction_mode == "tracking" and tracked_azimuth is not None:
        az = tracked_azimuth
    else:
        az = cfg.calibrated_azimuth
    return {"beam:Mode": 0.0, "beam:Azimuth (deg)": float(az) % 360.0,
            "beam:Elevation (deg)": float(cfg.calibrated_elevation)}


def processing_params(cfg: Config) -> dict[str, float]:
    return {
        "beam:Gain (dB)": float(cfg.gain_db),
        "beam:Dereverb": 1.0 if cfg.dereverb else 0.0,
        "beam:Dereverb Strength": float(cfg.dereverb_strength),
        "beam:Dereverb T60 (s)": float(cfg.dereverb_t60),
        "dfn:Attenuation Limit (dB)": float(cfg.noise_reduction_db),
        "limit:Ceiling (dB)": float(cfg.ceiling_db),
    }


def mix_params(active: bool) -> dict[str, float]:
    return {"mix:Gain 1": 1.0 if active else 0.0, "mix:Gain 2": 0.0 if active else 1.0}


def all_params(cfg: Config, tracked_azimuth: float | None = None) -> dict[str, float]:
    return {**geometry_params(cfg), **steering_params(cfg, tracked_azimuth),
            **processing_params(cfg), **mix_params(cfg.active)}
```

- [ ] **Step 5: Tests laufen lassen**

Run: `python -m pytest tests/test_config.py tests/test_params.py -v`
Expected: 8 passed

- [ ] **Step 6: Stand prüfen, nicht committen**

Run: `git status --short` und berichten.

---

### Task 7: PipeWire-Konfiguration, Dienst und Autostart-Vorlage

**Files:**
- Create: `pipewire/uma8-callmic.conf.in`, `pipewire/uma8-callmic-chain.service`, `pipewire/uma8-callmic.desktop`, `uma8_callmic/chainconf.py`
- Test: `tests/test_chainconf.py`

**Interfaces:**
- Consumes: `params.all_params`, `Config`, `constants`.
- Produces: `chainconf.render(cfg, beam_plugin=K.BEAM_PLUGIN, dfn_plugin=K.DFN_PLUGIN) -> str`; `chainconf.write(cfg, path=K.CHAIN_CONF) -> bool` (True bei Änderung); Knoten in der Kette heißen `beam`, `dfn`, `mix`, `limit`.

- [ ] **Step 1: Vorlagen schreiben**

`pipewire/uma8-callmic.conf.in`:
```
# Erzeugt von uma8-callmic – nicht von Hand ändern, wird überschrieben.
context.properties = {
    log.level = 0
}
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt
        args = { }
        flags = [ ifexists nofail ]
    }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
    { name = libpipewire-module-filter-chain
        args = {
            node.description = "UMA-8 Call Mic"
            media.name       = "UMA-8 Call Mic"
            audio.rate       = 48000
            filter.graph = {
                nodes = [
                    { type = ladspa name = beam plugin = "@BEAM_PLUGIN@" label = uma8_beam
                      control = { @BEAM_CONTROLS@ } }
                    { type = ladspa name = dfn plugin = "@DFN_PLUGIN@" label = deep_filter_mono
                      control = { @DFN_CONTROLS@ } }
                    { type = builtin name = mix label = mixer
                      control = { @MIX_CONTROLS@ } }
                    { type = ladspa name = limit plugin = "@BEAM_PLUGIN@" label = uma8_limit
                      control = { @LIMIT_CONTROLS@ } }
                ]
                links = [
                    { output = "beam:Beam Out" input = "dfn:Audio In" }
                    { output = "dfn:Audio Out" input = "mix:In 1" }
                    { output = "beam:Raw Out"  input = "mix:In 2" }
                    { output = "mix:Out"       input = "limit:In" }
                ]
                inputs  = [ "beam:In 0" "beam:In 1" "beam:In 2" "beam:In 3" "beam:In 4" "beam:In 5" "beam:In 6" null ]
                outputs = [ "limit:Out" ]
            }
            capture.props = {
                node.name         = "@CAPTURE_NODE@"
                node.description  = "UMA-8 Call Mic (Eingang)"
                node.passive      = true
                target.object     = "@RAW_DEVICE@"
                audio.channels    = 8
                audio.position    = [ FL FR FC LFE RL RR FLC FRC ]
                stream.dont-remix = true
            }
            playback.props = {
                node.name        = "@SOURCE_NODE@"
                node.description = "UMA-8 Call Mic"
                media.class      = Audio/Source
                audio.channels   = 1
                audio.position   = [ MONO ]
                priority.session = 2500
            }
        }
    }
]
```

`pipewire/uma8-callmic-chain.service`:
```ini
[Unit]
Description=UMA-8 Call Mic (PipeWire-Filterkette)
After=pipewire.service pipewire-session-manager.service
BindsTo=pipewire.service

[Service]
Type=simple
ExecStart=/usr/bin/pipewire -c %h/.config/pipewire/uma8-callmic.conf
Restart=on-failure
RestartSec=2
Slice=session.slice

[Install]
WantedBy=default.target
```

`pipewire/uma8-callmic.desktop`:
```ini
[Desktop Entry]
Type=Application
Name=UMA-8 Call Mic
Comment=Beamforming und Rauschunterdrückung für das miniDSP UMA-8
Exec=@BIN@
Icon=audio-input-microphone
Terminal=false
X-KDE-autostart-after=panel
```

- [ ] **Step 2: Fehlschlagende Tests schreiben**

`tests/test_chainconf.py`:
```python
import re
import subprocess
import time

import pytest

from uma8_callmic import constants as K
from uma8_callmic.chainconf import render, write
from uma8_callmic.config import Config


def test_render_fills_all_placeholders():
    text = render(Config())
    assert "@" not in text.replace("@BIN@", "")
    assert f'plugin = "{K.BEAM_PLUGIN}"' in text
    assert '"Azimuth (deg)" = 0' in text
    assert '"Attenuation Limit (dB)" = 30' in text
    assert text.count("{") == text.count("}")
    assert text.count("[") == text.count("]")


def test_render_reflects_active_state():
    on = re.search(r'label = mixer\s+control = \{ (.*?) \}', render(Config(active=True))).group(1)
    off = re.search(r'label = mixer\s+control = \{ (.*?) \}', render(Config(active=False))).group(1)
    assert on == '"Gain 1" = 1 "Gain 2" = 0'
    assert off == '"Gain 1" = 0 "Gain 2" = 1'


def test_write_reports_changes(tmp_path):
    path = tmp_path / "uma8-callmic.conf"
    assert write(Config(), path) is True
    assert write(Config(), path) is False
    assert write(Config(gain_db=40.0), path) is True


@pytest.mark.integration
def test_config_starts_in_pipewire(tmp_path):
    """Probelauf: braucht installiertes Plugin (install.sh) und laufendes PipeWire."""
    path = tmp_path / "probe.conf"
    path.write_text(render(Config()).replace('"uma8_callmic', '"uma8_callmic_probe'))
    proc = subprocess.Popen(["pipewire", "-c", str(path)], stderr=subprocess.PIPE, text=True)
    time.sleep(2)
    proc.terminate()
    _, err = proc.communicate(timeout=5)
    assert "error" not in err.lower(), err
```

- [ ] **Step 3: Tests laufen lassen, sie müssen fehlschlagen**

Run: `python -m pytest tests/test_chainconf.py -v`
Expected: FAIL mit `ModuleNotFoundError: No module named 'uma8_callmic.chainconf'`

- [ ] **Step 4: `chainconf.py` implementieren**

```python
"""Erzeugt die PipeWire-Konfiguration der Filterkette aus den Einstellungen."""
from pathlib import Path

from . import constants as K
from .config import Config
from .params import all_params

TEMPLATE = K.REPO_DIR / "pipewire" / "uma8-callmic.conf.in"


def _controls(params: dict[str, float], node: str) -> str:
    items = [(key.split(":", 1)[1], value) for key, value in params.items() if key.startswith(node + ":")]
    return " ".join(f'"{name}" = {float(value):.6g}' for name, value in items)


def render(cfg: Config, beam_plugin: Path = K.BEAM_PLUGIN, dfn_plugin: Path = K.DFN_PLUGIN) -> str:
    params = all_params(cfg)
    subs = {
        "@BEAM_PLUGIN@": str(beam_plugin), "@DFN_PLUGIN@": str(dfn_plugin),
        "@CAPTURE_NODE@": K.CAPTURE_NODE, "@SOURCE_NODE@": K.SOURCE_NODE, "@RAW_DEVICE@": K.RAW_DEVICE,
        "@BEAM_CONTROLS@": _controls(params, "beam"), "@DFN_CONTROLS@": _controls(params, "dfn"),
        "@MIX_CONTROLS@": _controls(params, "mix"), "@LIMIT_CONTROLS@": _controls(params, "limit"),
    }
    text = TEMPLATE.read_text()
    for placeholder, value in subs.items():
        text = text.replace(placeholder, value)
    return text


def write(cfg: Config, path: Path = K.CHAIN_CONF) -> bool:
    """Schreibt die Konfiguration; True, wenn sich der Inhalt geändert hat."""
    text = render(cfg)
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True
```

- [ ] **Step 5: Tests laufen lassen**

Run: `python -m pytest tests/test_chainconf.py -v`
Expected: 3 passed, 1 deselected (integration)

- [ ] **Step 6: Stand prüfen, nicht committen**

Run: `git status --short` und berichten.

---

### Task 8: PipeWire-Steuerung und Tray-Zustand

**Files:**
- Create: `uma8_callmic/pwctl.py`, `uma8_callmic/traystate.py`
- Test: `tests/test_pwctl.py`, `tests/test_traystate.py`

**Interfaces:**
- Consumes: `constants`, `Config`.
- Produces: `pwctl.dump() -> list[dict]`, `pwctl.find_node(objs, name) -> int | None`, `pwctl.device_state(objs) -> "raw" | "dsp" | "missing"`, `pwctl.format_params(dict) -> str`, `pwctl.set_params(node_id, dict)` (wirft `RuntimeError`), `pwctl.fade_mix(node_id, active, steps=10)`, `pwctl.service_active() -> bool`, `pwctl.service(action)`, `pwctl.set_default_source()`, `pwctl.Status(device, service, chain, dfn)` mit `.problem -> str | None`, `pwctl.status() -> Status`; `traystate.icon_state(status, active) -> "active"|"inactive"|"error"`, `traystate.tooltip(status, cfg, tracked=None, warnings=()) -> str`.

- [ ] **Step 1: Fehlschlagende Tests schreiben**

`tests/test_pwctl.py`:
```python
import pytest

from uma8_callmic import pwctl
from uma8_callmic.pwctl import Status

OBJS = [
    {"id": 31, "type": "PipeWire:Interface:Device",
     "info": {"props": {"device.vendor.id": "0x2752", "device.product.id": "0x001d"}}},
    {"id": 77, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "uma8_callmic_capture"}}},
    {"id": 78, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "uma8_callmic"}}},
    {"id": 90, "type": "PipeWire:Interface:Link", "info": {}},
]


def test_find_node():
    assert pwctl.find_node(OBJS, "uma8_callmic_capture") == 77
    assert pwctl.find_node(OBJS, "fehlt") is None


def test_device_state():
    assert pwctl.device_state(OBJS) == "raw"
    dsp = [{"id": 1, "type": "PipeWire:Interface:Device",
            "info": {"props": {"device.vendor.id": "0x2752", "device.product.id": "0x001c"}}}]
    assert pwctl.device_state(dsp) == "dsp"
    assert pwctl.device_state([]) == "missing"


def test_format_params():
    s = pwctl.format_params({"beam:Azimuth (deg)": 90.0, "mix:Gain 1": 1})
    assert s == '{ params = [ "beam:Azimuth (deg)" 90 "mix:Gain 1" 1 ] }'


def test_set_params_calls_pw_cli(monkeypatch):
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(pwctl, "_run", lambda args, timeout=5.0: calls.append(args) or Done())
    pwctl.set_params(77, {"beam:Mode": 1.0})
    assert calls == [["pw-cli", "set-param", "77", "Props", '{ params = [ "beam:Mode" 1 ] }']]


def test_set_params_raises_on_error(monkeypatch):
    class Fail:
        returncode, stdout, stderr = 1, "", "Error: unknown object"

    monkeypatch.setattr(pwctl, "_run", lambda args, timeout=5.0: Fail())
    with pytest.raises(RuntimeError, match="unknown object"):
        pwctl.set_params(77, {"beam:Mode": 1.0})


def test_fade_mix_ends_at_target(monkeypatch):
    seen = []
    monkeypatch.setattr(pwctl, "set_params", lambda node, p: seen.append(p))
    pwctl.fade_mix(77, active=False, steps=4)
    assert len(seen) == 4
    assert seen[-1] == {"mix:Gain 1": 0.0, "mix:Gain 2": 1.0}


def test_status_problem_priority():
    assert Status("raw", True, True, False).problem.startswith("DeepFilterNet")
    assert Status("dsp", True, True, True).problem.startswith("Raw-Firmware")
    assert Status("missing", True, True, True).problem.startswith("UMA-8 nicht")
    assert Status("raw", False, False, True).problem.startswith("Dienst")
    assert Status("raw", True, False, True).problem.startswith("Filterkette")
    assert Status("raw", True, True, True).problem is None
```

`tests/test_traystate.py`:
```python
from uma8_callmic.config import Config
from uma8_callmic.pwctl import Status
from uma8_callmic.traystate import icon_state, tooltip

OK = Status("raw", True, True, True)


def test_icon_state():
    assert icon_state(OK, True) == "active"
    assert icon_state(OK, False) == "inactive"
    assert icon_state(Status("missing", True, True, True), True) == "error"


def test_tooltip_texts():
    cfg = Config(calibrated=True, calibrated_azimuth=215.0)
    assert "Aktiv" in tooltip(OK, cfg) and "215°" in tooltip(OK, cfg)
    cfg.direction_mode = "tracking"
    assert "automatisch (40°)" in tooltip(OK, cfg, tracked=40.0)
    assert "nicht angeschlossen" in tooltip(Status("missing", True, True, True), cfg)
    assert "Hinweis: kaputt" in tooltip(OK, cfg, warnings=["kaputt"])
    assert "noch nicht kalibriert" in tooltip(OK, Config())
```

- [ ] **Step 2: Tests laufen lassen, sie müssen fehlschlagen**

Run: `python -m pytest tests/test_pwctl.py tests/test_traystate.py -v`
Expected: FAIL mit `ModuleNotFoundError`

- [ ] **Step 3: `pwctl.py` implementieren**

```python
"""PipeWire- und Dienststeuerung über pw-dump, pw-cli, systemctl und pactl."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from . import constants as K


def _run(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def dump() -> list[dict]:
    r = _run(["pw-dump"])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "pw-dump fehlgeschlagen")
    return json.loads(r.stdout)


def _props(obj: dict) -> dict:
    return (obj.get("info") or {}).get("props") or {}


def find_node(objs: list[dict], name: str) -> int | None:
    for o in objs:
        if o.get("type") == "PipeWire:Interface:Node" and _props(o).get("node.name") == name:
            return o["id"]
    return None


def device_state(objs: list[dict]) -> str:
    """'raw', 'dsp' oder 'missing' anhand der USB-Produkt-ID des miniDSP-Geräts."""
    for o in objs:
        if o.get("type") != "PipeWire:Interface:Device":
            continue
        p = _props(o)
        if p.get("device.vendor.id") == "0x2752":
            return {"0x001d": "raw", "0x001c": "dsp"}.get(p.get("device.product.id"), "missing")
    return "missing"


def format_params(params: dict[str, float]) -> str:
    body = " ".join(f'"{key}" {float(value):.6g}' for key, value in params.items())
    return "{ params = [ " + body + " ] }"


def set_params(node_id: int, params: dict[str, float]) -> None:
    r = _run(["pw-cli", "set-param", str(node_id), "Props", format_params(params)])
    text = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or "error" in text.lower():
        raise RuntimeError(f"pw-cli set-param fehlgeschlagen: {text}")


def fade_mix(node_id: int, active: bool, steps: int = 10) -> None:
    """Blendet zwischen Beam-Weg (Gain 1) und Roh-Weg (Gain 2) in `steps` Schritten um."""
    for i in range(1, steps + 1):
        a = i / steps if active else 1.0 - i / steps
        set_params(node_id, {"mix:Gain 1": a, "mix:Gain 2": 1.0 - a})


def service_active() -> bool:
    return _run(["systemctl", "--user", "is-active", K.SERVICE]).stdout.strip() == "active"


def service(action: str) -> None:
    if action not in ("start", "stop", "restart"):
        raise ValueError(action)
    _run(["systemctl", "--user", action, K.SERVICE], timeout=15.0)


def set_default_source() -> None:
    _run(["pactl", "set-default-source", K.SOURCE_NODE])


@dataclass
class Status:
    device: str
    service: bool
    chain: bool
    dfn: bool

    @property
    def problem(self) -> str | None:
        if not self.dfn:
            return "DeepFilterNet nicht installiert (yay -S deepfilternet-plugin-pipewire-bin)"
        if self.device == "dsp":
            return "Raw-Firmware nötig (Mikrofon läuft mit DSP-Firmware)"
        if self.device == "missing":
            return "UMA-8 nicht angeschlossen"
        if not self.service:
            return "Dienst uma8-callmic-chain läuft nicht"
        if not self.chain:
            return "Filterkette nicht geladen"
        return None


def status() -> Status:
    try:
        objs = dump()
    except (RuntimeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        objs = []
    try:
        running = service_active()
    except (OSError, subprocess.TimeoutExpired):
        running = False
    return Status(device_state(objs), running, find_node(objs, K.CAPTURE_NODE) is not None, K.DFN_PLUGIN.exists())
```

- [ ] **Step 4: `traystate.py` implementieren**

```python
"""Icon-Zustand und Tooltip-Text des Tray-Icons (ohne Qt, testbar)."""
from __future__ import annotations

from .config import Config
from .pwctl import Status

MODE_TEXT = {"calibrated": "Richtung: kalibriert", "manual": "Richtung: manuell",
             "tracking": "Richtung: automatisch", "omni": "Richtung: alle"}


def icon_state(status: Status, active: bool) -> str:
    if status.problem:
        return "error"
    return "active" if active else "inactive"


def tooltip(status: Status, cfg: Config, tracked: float | None = None, warnings=()) -> str:
    lines = ["UMA-8 Call Mic"]
    if status.problem:
        lines.append("⚠ " + status.problem)
    else:
        lines.append("Aktiv: Beamforming + Rauschunterdrückung" if cfg.active else "Deaktiviert: Rohsignal")
        mode = MODE_TEXT[cfg.direction_mode]
        if cfg.direction_mode == "calibrated":
            mode += f" ({cfg.calibrated_azimuth:.0f}°)" if cfg.calibrated else " (noch nicht kalibriert)"
        elif cfg.direction_mode == "manual":
            mode += f" ({cfg.manual_azimuth:.0f}°)"
        elif cfg.direction_mode == "tracking" and tracked is not None:
            mode += f" ({tracked:.0f}°)"
        lines.append(mode)
    lines += [f"Hinweis: {w}" for w in warnings]
    return "\n".join(lines)
```

- [ ] **Step 5: Tests laufen lassen**

Run: `python -m pytest tests/test_pwctl.py tests/test_traystate.py -v`
Expected: 9 passed

- [ ] **Step 6: Stand prüfen, nicht committen**

Run: `git status --short` und berichten.

---

### Task 9: Richtungsschätzung und Sprachaktivität

**Files:**
- Create: `uma8_callmic/doa.py`, `uma8_callmic/calibration.py`
- Test: `tests/test_doa.py`, `tests/test_calibration.py`

**Interfaces:**
- Consumes: `ArrayGeometry.positions()`; `tests/sim.py`.
- Produces: `doa.DoaResult(azimuth, elevation, confidence, peak)`, `doa.SrpPhat(positions, sample_rate=48000, nfft=1024, fmin=300, fmax=6000, az_step=5, elevations=(0,15,30,45))` mit `.estimate(block (n, 7)) -> DoaResult`; `doa.VoiceDetector(threshold_db=6.0, flatness_max=0.6, sample_rate=48000)` mit `.is_speech(mono) -> bool`; `doa.angle_diff(a, b) -> float`, `doa.circular_mean(angles) -> float`; `calibration.CalibrationOutcome(ok, azimuth, elevation, message)`, `calibration.evaluate(results, min_blocks=5, min_confidence=0.05, max_spread=20.0)`.

- [ ] **Step 1: Fehlschlagende Tests schreiben**

`tests/test_doa.py`:
```python
import numpy as np
import pytest

from sim import band_noise, plane_wave
from uma8_callmic.array import UMA8
from uma8_callmic.doa import SrpPhat, VoiceDetector, angle_diff, circular_mean


@pytest.fixture(scope="module")
def srp():
    return SrpPhat(UMA8.positions())


@pytest.mark.parametrize("az", range(0, 360, 30))
def test_srp_finds_azimuth(srp, az):
    rng = np.random.default_rng(az)
    x = plane_wave(band_noise(9600, 300, 6000, seed=az), UMA8.positions(), az, 20.0)
    x += 0.1 * rng.standard_normal(x.shape)
    r = srp.estimate(x)
    assert angle_diff(r.azimuth, az) < 15.0, r
    assert r.confidence > 0.05


def test_angle_helpers():
    assert angle_diff(350, 10) == 20
    assert abs(circular_mean([350, 10, 0]) - 0) < 1e-6 or abs(circular_mean([350, 10, 0]) - 360) < 1e-6
    assert abs(circular_mean([80, 100]) - 90) < 1e-6


def speech_like(n, seed=0):
    t = np.arange(n) / 48000
    f0 = 140 + 20 * np.sin(2 * np.pi * 3 * t)
    phase = 2 * np.pi * np.cumsum(f0) / 48000
    voiced = sum(np.sin(k * phase) / k for k in range(1, 20))
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 4 * t) ** 2
    return voiced * envelope


def test_voice_detector():
    rng = np.random.default_rng(0)
    vad = VoiceDetector()
    for _ in range(5):
        assert not vad.is_speech(0.001 * rng.standard_normal(9600))
    assert vad.is_speech(0.02 * speech_like(9600))
    assert not vad.is_speech(0.001 * rng.standard_normal(9600))
```

`tests/test_calibration.py`:
```python
from uma8_callmic.calibration import evaluate
from uma8_callmic.doa import DoaResult


def r(az, conf=0.3, el=20.0):
    return DoaResult(az, el, conf, 0.5)


def test_consistent_results_are_accepted():
    out = evaluate([r(210), r(215), r(220), r(212), r(218)])
    assert out.ok and abs(out.azimuth - 215) < 2 and "215°" in out.message


def test_too_few_blocks():
    out = evaluate([r(210)] * 3)
    assert not out.ok and "Zu wenig Sprache" in out.message


def test_scattered_results_are_rejected():
    out = evaluate([r(0), r(90), r(180), r(270), r(45)])
    assert not out.ok and "nicht eindeutig" in out.message


def test_low_confidence_is_rejected():
    out = evaluate([r(100, conf=0.01)] * 6)
    assert not out.ok
```

- [ ] **Step 2: Tests laufen lassen, sie müssen fehlschlagen**

Run: `python -m pytest tests/test_doa.py tests/test_calibration.py -v`
Expected: FAIL mit `ModuleNotFoundError`

- [ ] **Step 3: `doa.py` implementieren**

```python
"""Richtungsschätzung (SRP-PHAT), Sprachaktivität und Winkelhilfen."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

C_SOUND = 343.0


def angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def circular_mean(angles) -> float:
    r = np.radians(np.asarray(angles, dtype=float))
    return float(np.degrees(np.arctan2(np.sin(r).mean(), np.cos(r).mean())) % 360.0)


@dataclass
class DoaResult:
    azimuth: float
    elevation: float
    confidence: float  # Haupt- minus Nebenmaximum (außerhalb ±30°)
    peak: float        # normierte SRP am Maximum (−1 … 1)


class SrpPhat:
    def __init__(self, positions: np.ndarray, sample_rate: int = 48000, nfft: int = 1024,
                 fmin: float = 300.0, fmax: float = 6000.0, az_step: float = 5.0,
                 elevations=(0.0, 15.0, 30.0, 45.0)):
        self.nfft = nfft
        freqs = np.fft.rfftfreq(nfft, 1 / sample_rate)
        self.bins = np.where((freqs >= fmin) & (freqs <= fmax))[0]
        w = 2 * np.pi * freqs[self.bins]
        m = positions.shape[0]
        self.pairs = [(i, j) for i in range(m) for j in range(i + 1, m)]
        az_grid, el_grid = np.meshgrid(np.arange(0.0, 360.0, az_step), np.asarray(elevations, float), indexing="ij")
        self.grid_az, self.grid_el = az_grid.ravel(), el_grid.ravel()
        a, e = np.radians(self.grid_az), np.radians(self.grid_el)
        u = np.stack([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)], axis=1)
        lead = u @ positions.T / C_SOUND                                    # (G, M)
        dl = np.stack([lead[:, i] - lead[:, j] for i, j in self.pairs], 1)  # (G, P)
        # X_i X_j* = |S|² e^{+jω(lead_i − lead_j)} → Ausrichten mit e^{−jω·dl}
        self.steer = np.exp(-1j * dl[:, :, None] * w[None, None, :])      # (G, P, K)
        self.window = np.hanning(nfft)

    def cross_spectra(self, block: np.ndarray) -> np.ndarray:
        hop = self.nfft // 2
        frames = np.stack([block[s:s + self.nfft] for s in range(0, len(block) - self.nfft + 1, hop)])
        spec = np.fft.rfft(frames * self.window[None, :, None], axis=1)[:, self.bins, :]   # (F, K, M)
        out = np.empty((len(self.pairs), len(self.bins)), dtype=complex)
        for p, (i, j) in enumerate(self.pairs):
            c = spec[:, :, i] * np.conj(spec[:, :, j])
            out[p] = np.mean(c / (np.abs(c) + 1e-12), axis=0)
        return out

    def estimate(self, block: np.ndarray) -> DoaResult:
        cs = self.cross_spectra(np.asarray(block, dtype=float))
        srp = np.real(np.einsum("gpk,pk->g", self.steer, cs)) / cs.size
        best = int(np.argmax(srp))
        far = angle_diff_array(self.grid_az, self.grid_az[best]) > 30.0
        second = float(np.max(srp[far])) if far.any() else -1.0
        return DoaResult(float(self.grid_az[best]), float(self.grid_el[best]),
                         float(srp[best] - second), float(srp[best]))


def angle_diff_array(a: np.ndarray, b: float) -> np.ndarray:
    return np.abs((a - b + 180.0) % 360.0 - 180.0)


class VoiceDetector:
    """Sprache = Energie über adaptivem Grundrauschen und geringe spektrale Flachheit."""

    def __init__(self, threshold_db: float = 6.0, flatness_max: float = 0.6, sample_rate: int = 48000):
        self.threshold = 10 ** (threshold_db / 10)
        self.flatness_max = flatness_max
        self.sr = sample_rate
        self.noise: float | None = None

    def _flatness(self, x: np.ndarray) -> float:
        nfft = 1024
        frames = np.stack([x[s:s + nfft] for s in range(0, len(x) - nfft + 1, nfft // 2)])
        p = np.mean(np.abs(np.fft.rfft(frames * np.hanning(nfft), axis=1)) ** 2, axis=0)
        f = np.fft.rfftfreq(nfft, 1 / self.sr)
        band = p[(f >= 300) & (f <= 4000)] + 1e-20
        return float(np.exp(np.mean(np.log(band))) / np.mean(band))

    def is_speech(self, mono: np.ndarray) -> bool:
        x = np.asarray(mono, dtype=float)
        e = float(np.mean(x * x)) + 1e-20
        if self.noise is None:
            self.noise = e
            return False
        speech = e > self.noise * self.threshold and self._flatness(x) < self.flatness_max
        if e < self.noise:
            self.noise = 0.7 * self.noise + 0.3 * e
        elif not speech:
            self.noise *= 1.05
        return speech
```

- [ ] **Step 4: `calibration.py` implementieren**

```python
"""Auswertung einer Kalibrierung aus mehreren Richtungsschätzungen."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .doa import DoaResult, angle_diff, circular_mean


@dataclass
class CalibrationOutcome:
    ok: bool
    azimuth: float
    elevation: float
    message: str


def evaluate(results: list[DoaResult], min_blocks: int = 5, min_confidence: float = 0.05,
             max_spread: float = 20.0) -> CalibrationOutcome:
    if len(results) < min_blocks:
        return CalibrationOutcome(False, 0.0, 0.0,
                                  "Zu wenig Sprache erkannt – bitte näher oder lauter sprechen und wiederholen.")
    az = circular_mean([r.azimuth for r in results])
    el = float(np.mean([r.elevation for r in results]))
    spread = float(np.mean([angle_diff(r.azimuth, az) for r in results]))
    conf = float(np.median([r.confidence for r in results]))
    if conf < min_confidence or spread > max_spread:
        return CalibrationOutcome(False, az, el,
                                  f"Richtung nicht eindeutig (Streuung {spread:.0f}°) – bitte wiederholen.")
    return CalibrationOutcome(True, az, el,
                              f"Richtung {az:.0f}°, Höhe {el:.0f}° – eindeutig (Streuung {spread:.0f}°).")
```

- [ ] **Step 5: Tests laufen lassen**

Run: `python -m pytest tests/test_doa.py tests/test_calibration.py -v`
Expected: 18 passed (12 Richtungen + 2 + 4)

- [ ] **Step 6: Stand prüfen, nicht committen**

Run: `git status --short` und berichten.

---

### Task 10: Kanalzuordnung aus Raumrauschen prüfen

**Files:**
- Create: `uma8_callmic/geometry.py`
- Test: `tests/test_geometry.py`

**Interfaces:**
- Consumes: `ArrayGeometry`; `tests/sim.diffuse_noise`.
- Produces: `geometry.coherence_matrix(block, sample_rate=48000, nfft=1024, fmin=200, fmax=2000) -> (7, 7)`, `geometry.detect(coh, radius_m, ring_offset_deg) -> (ArrayGeometry, contrast)`, `geometry.GeometryCheck(detected, matches_default, contrast, message)`, `geometry.check(block (n, ≥7), default, min_contrast=0.05) -> GeometryCheck`.

- [ ] **Step 1: Fehlschlagende Tests schreiben**

`tests/test_geometry.py`:
```python
import numpy as np
import pytest

from sim import diffuse_noise
from uma8_callmic.array import UMA8
from uma8_callmic.geometry import check


@pytest.fixture(scope="module")
def diffuse():
    x = diffuse_noise(48000 * 4, UMA8.positions(), seed=1)
    return x + 0.05 * np.random.default_rng(2).standard_normal(x.shape)


def test_default_mapping_is_confirmed(diffuse):
    res = check(diffuse, UMA8)
    assert res.matches_default, res.message
    assert res.contrast > 0.05


def test_permuted_channels_are_detected(diffuse):
    perm = [3, 0, 5, 1, 6, 2, 4]  # physischer Kanal i landet auf Aufnahme-Kanal perm[i]
    shuffled = np.empty_like(diffuse)
    for i, p in enumerate(perm):
        shuffled[:, p] = diffuse[:, i]
    res = check(shuffled, UMA8)
    assert not res.matches_default
    assert res.detected.center == perm[UMA8.center]
    expected = {frozenset((perm[UMA8.ring[k]], perm[UMA8.ring[(k + 1) % 6]])) for k in range(6)}
    got = {frozenset((res.detected.ring[k], res.detected.ring[(k + 1) % 6])) for k in range(6)}
    assert got == expected


def test_incoherent_noise_is_rejected():
    x = np.random.default_rng(0).standard_normal((48000 * 2, 8))
    res = check(x, UMA8)
    assert res.detected is None and "Standardzuordnung bleibt" in res.message
```

- [ ] **Step 2: Tests laufen lassen, sie müssen fehlschlagen**

Run: `python -m pytest tests/test_geometry.py -v`
Expected: FAIL mit `ModuleNotFoundError`

- [ ] **Step 3: Implementieren**

`uma8_callmic/geometry.py`:
```python
"""Kanalzuordnung aus diffusem Raumrauschen: Kohärenz sinkt mit dem Mikrofonabstand."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .array import ArrayGeometry


def coherence_matrix(block: np.ndarray, sample_rate: int = 48000, nfft: int = 1024,
                     fmin: float = 200.0, fmax: float = 2000.0) -> np.ndarray:
    hop = nfft // 2
    frames = np.stack([block[s:s + nfft] for s in range(0, len(block) - nfft + 1, hop)])
    spec = np.fft.rfft(frames * np.hanning(nfft)[None, :, None], axis=1)
    f = np.fft.rfftfreq(nfft, 1 / sample_rate)
    spec = spec[:, (f >= fmin) & (f <= fmax), :]
    cross = np.einsum("fki,fkj->kij", spec, np.conj(spec)) / len(frames)
    auto = np.real(np.einsum("kii->ki", cross))
    coh = np.abs(cross) ** 2 / (auto[:, :, None] * auto[:, None, :] + 1e-30)
    return coh.mean(axis=0)


def detect(coh: np.ndarray, radius_m: float, ring_offset_deg: float) -> tuple[ArrayGeometry, float]:
    """Mitte = höchste mittlere Kohärenz; Ring = Kette der jeweils kohärentesten Nachbarn."""
    off = coh * (1 - np.eye(coh.shape[0]))
    center = int(np.argmax(off.sum(axis=1)))
    ring_channels = [c for c in range(coh.shape[0]) if c != center]
    order = [min(ring_channels)]
    while len(order) < 6:
        rest = [c for c in ring_channels if c not in order]
        order.append(max(rest, key=lambda c: off[order[-1], c]))
    neighbours = np.mean([off[order[k], order[(k + 1) % 6]] for k in range(6)])
    opposite = np.mean([off[order[k], order[k + 3]] for k in range(3)])
    return ArrayGeometry(center, tuple(order), ring_offset_deg, radius_m), float(neighbours - opposite)


def _neighbour_pairs(ring) -> set[frozenset]:
    return {frozenset((ring[k], ring[(k + 1) % 6])) for k in range(6)}


@dataclass
class GeometryCheck:
    detected: ArrayGeometry | None
    matches_default: bool
    contrast: float
    message: str


def check(block: np.ndarray, default: ArrayGeometry, min_contrast: float = 0.05) -> GeometryCheck:
    coh = coherence_matrix(np.asarray(block, dtype=float)[:, :7])
    geom, contrast = detect(coh, default.radius_m, default.ring_offset_deg)
    if contrast < min_contrast:
        return GeometryCheck(None, False, contrast,
                             "Raumgeräusch zu leise oder zu gerichtet – Standardzuordnung bleibt aktiv.")
    same = geom.center == default.center and _neighbour_pairs(geom.ring) == _neighbour_pairs(default.ring)
    return GeometryCheck(geom, same, contrast,
                         "Standardzuordnung bestätigt." if same else "Abweichende Kanalzuordnung erkannt.")
```

- [ ] **Step 4: Tests laufen lassen**

Run: `python -m pytest tests/test_geometry.py -v`
Expected: 3 passed

- [ ] **Step 5: Stand prüfen, nicht committen**

Run: `git status --short` und berichten.

---

### Task 11: Aufnahme und Nachführung

**Files:**
- Create: `uma8_callmic/capture.py`, `uma8_callmic/tracker.py`
- Test: `tests/test_tracker.py`, `tests/test_capture.py`

**Interfaces:**
- Consumes: `doa.{DoaResult, angle_diff, circular_mean}`.
- Produces: `capture.Capture(target, channels, seconds=12.0, sample_rate=48000, command=None)` mit `.latest(frames) -> np.ndarray | None`, `.total() -> int`, `.alive`, `.close()`; `tracker.Tracker(estimator, vad, apply, initial_azimuth=0.0, center=0, hysteresis=15.0, window=5, min_confidence=0.05)` mit `.feed(block) -> float | None` und `.current`; `tracker.TrackerThread(capture, tracker, block_frames=9600, interval=0.2)` mit `.start()`, `.stop()`.

- [ ] **Step 1: Fehlschlagende Tests schreiben**

`tests/test_tracker.py`:
```python
import numpy as np

from uma8_callmic.doa import DoaResult
from uma8_callmic.tracker import Tracker


class FakeEstimator:
    def __init__(self, results):
        self.results = list(results)

    def estimate(self, block):
        return self.results.pop(0)


class AlwaysSpeech:
    def is_speech(self, mono):
        return True


def run(results, initial=0.0):
    applied = []
    t = Tracker(FakeEstimator(results), AlwaysSpeech(), applied.append, initial_azimuth=initial)
    block = np.zeros((9600, 8))
    outs = [t.feed(block) for _ in results]
    return applied, outs, t


def test_moves_after_consistent_estimates():
    applied, outs, t = run([DoaResult(90, 20, 0.3, 0.5)] * 3)
    assert outs[:2] == [None, None]
    assert abs(applied[0] - 90) < 1e-6 and abs(t.current - 90) < 1e-6


def test_small_changes_are_ignored():
    applied, _, _ = run([DoaResult(10, 20, 0.3, 0.5)] * 4)
    assert applied == []


def test_low_confidence_is_ignored():
    applied, _, _ = run([DoaResult(180, 20, 0.01, 0.5)] * 5)
    assert applied == []


def test_wraparound_mean():
    applied, _, _ = run([DoaResult(a, 20, 0.3, 0.5) for a in (170, 190, 180)], initial=0.0)
    assert abs(applied[0] - 180) < 1e-6
```

`tests/test_capture.py`:
```python
import sys
import time

import numpy as np

from uma8_callmic.capture import Capture


def test_capture_reads_frames_from_command():
    data = np.arange(3 * 4800, dtype=np.float32).reshape(4800, 3)
    script = f"import sys,numpy as n; sys.stdout.buffer.write(n.arange({data.size}, dtype=n.float32).tobytes())"
    cap = Capture("egal", 3, seconds=1.0, command=[sys.executable, "-c", script])
    for _ in range(50):
        if cap.total() >= 4800:
            break
        time.sleep(0.05)
    block = cap.latest(4800)
    cap.close()
    np.testing.assert_array_equal(block, data)
```

- [ ] **Step 2: Tests laufen lassen, sie müssen fehlschlagen**

Run: `python -m pytest tests/test_tracker.py tests/test_capture.py -v`
Expected: FAIL mit `ModuleNotFoundError`

- [ ] **Step 3: `capture.py` implementieren**

```python
"""Mehrkanal-Aufnahme über pw-record (Rohdaten auf stdout) in einen Ringpuffer."""
from __future__ import annotations

import subprocess
import threading

import numpy as np


class Capture:
    def __init__(self, target: str, channels: int, seconds: float = 12.0, sample_rate: int = 48000,
                 command: list[str] | None = None):
        self.channels = channels
        self.buf = np.zeros((int(seconds * sample_rate), channels), dtype=np.float32)
        self.written = 0
        self.lock = threading.Lock()
        cmd = command or ["pw-record", "--target", target, "--rate", str(sample_rate),
                          "--channels", str(channels), "--format", "f32", "--raw", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self) -> None:
        frame_bytes = 4 * self.channels
        pending = b""
        while True:
            chunk = self.proc.stdout.read(frame_bytes * 1024)
            if not chunk:
                break
            data = pending + chunk
            usable = len(data) - len(data) % frame_bytes
            pending = data[usable:]
            frames = np.frombuffer(data[:usable], dtype=np.float32).reshape(-1, self.channels)
            with self.lock:
                idx = (self.written + np.arange(len(frames))) % len(self.buf)
                self.buf[idx] = frames
                self.written += len(frames)

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def total(self) -> int:
        with self.lock:
            return self.written

    def latest(self, frames: int) -> np.ndarray | None:
        with self.lock:
            if self.written < frames or frames > len(self.buf):
                return None
            idx = (self.written - frames + np.arange(frames)) % len(self.buf)
            return self.buf[idx].copy()

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
```

- [ ] **Step 4: `tracker.py` implementieren**

```python
"""Nachführung: schätzt bei Sprache die Richtung und stellt den Strahl nach."""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Callable

import numpy as np

from .doa import angle_diff, circular_mean

log = logging.getLogger(__name__)


class Tracker:
    def __init__(self, estimator, vad, apply: Callable[[float], None], initial_azimuth: float = 0.0,
                 center: int = 0, hysteresis: float = 15.0, window: int = 5, min_confidence: float = 0.05):
        self.estimator, self.vad, self.apply = estimator, vad, apply
        self.current = initial_azimuth
        self.center = center
        self.hysteresis, self.min_confidence = hysteresis, min_confidence
        self.history: deque[float] = deque(maxlen=window)

    def feed(self, block: np.ndarray) -> float | None:
        if not self.vad.is_speech(block[:, self.center]):
            return None
        r = self.estimator.estimate(block[:, :7])
        if r.confidence < self.min_confidence:
            return None
        self.history.append(r.azimuth)
        if len(self.history) < 3:
            return None
        target = circular_mean(self.history)
        if angle_diff(target, self.current) <= self.hysteresis:
            return None
        self.current = target
        self.apply(target)
        return target


class TrackerThread(threading.Thread):
    def __init__(self, capture, tracker: Tracker, block_frames: int = 9600, interval: float = 0.2):
        super().__init__(daemon=True)
        self.capture, self.tracker = capture, tracker
        self.block_frames, self.interval = block_frames, interval
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            block = self.capture.latest(self.block_frames)
            if block is None:
                continue
            try:
                self.tracker.feed(block)
            except Exception:  # Nachführung darf nie die Anwendung beenden
                log.exception("Nachführung fehlgeschlagen")

    def stop(self) -> None:
        self._stop.set()
```

- [ ] **Step 5: Tests laufen lassen**

Run: `python -m pytest tests/test_tracker.py tests/test_capture.py -v`
Expected: 5 passed

- [ ] **Step 6: Stand prüfen, nicht committen**

Run: `git status --short` und berichten.

---

### Task 12: Tray-Anwendung, Dialoge, Icons, Einstiegspunkt

**Files:**
- Create: `uma8_callmic/icons/active.svg`, `uma8_callmic/icons/inactive.svg`, `uma8_callmic/icons/error.svg`
- Create: `uma8_callmic/dialogs.py`, `uma8_callmic/tray.py`, `uma8_callmic/__main__.py`
- Test: `tests/test_dialogs.py`

**Interfaces:**
- Consumes: alles aus Task 4–11.
- Produces: `python -m uma8_callmic` (Tray), `--write-config`, `--check-geometry`; `dialogs.OptionsDialog(cfg, on_change(dict), on_check_geometry(), meter=True)`, `dialogs.CalibrationDialog(geometry, on_accept(az, el))`, `dialogs.GeometryDialog(default, on_done(ran: bool, adopt: ArrayGeometry | None))`, `dialogs.level_dbfs(block) -> float`; `tray.TrayApp(app)` mit `.shutdown()`.

- [ ] **Step 1: Icons anlegen**

`uma8_callmic/icons/active.svg`:
```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect x="24" y="8" width="16" height="30" rx="8" fill="#2e9d5b"/><path d="M16 30a16 16 0 0 0 32 0" fill="none" stroke="#2e9d5b" stroke-width="4" stroke-linecap="round"/><path d="M32 46v10M22 56h20" stroke="#2e9d5b" stroke-width="4" stroke-linecap="round"/><path d="M50 16a20 20 0 0 1 0 20M56 10a28 28 0 0 1 0 32" fill="none" stroke="#2e9d5b" stroke-width="3" stroke-linecap="round"/></svg>
```

`uma8_callmic/icons/inactive.svg`:
```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect x="24" y="8" width="16" height="30" rx="8" fill="#8a8f98"/><path d="M16 30a16 16 0 0 0 32 0" fill="none" stroke="#8a8f98" stroke-width="4" stroke-linecap="round"/><path d="M32 46v10M22 56h20" stroke="#8a8f98" stroke-width="4" stroke-linecap="round"/></svg>
```

`uma8_callmic/icons/error.svg`:
```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect x="20" y="8" width="16" height="30" rx="8" fill="#d64545"/><path d="M12 30a16 16 0 0 0 32 0" fill="none" stroke="#d64545" stroke-width="4" stroke-linecap="round"/><path d="M28 46v10M18 56h20" stroke="#d64545" stroke-width="4" stroke-linecap="round"/><circle cx="50" cy="48" r="12" fill="#d64545"/><path d="M50 41v8" stroke="#fff" stroke-width="4" stroke-linecap="round"/><circle cx="50" cy="55" r="2.2" fill="#fff"/></svg>
```

- [ ] **Step 2: Fehlschlagende Dialog-Tests schreiben**

`tests/test_dialogs.py`:
```python
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from uma8_callmic.array import UMA8
from uma8_callmic.config import Config
from uma8_callmic.dialogs import CalibrationDialog, GeometryDialog, OptionsDialog, level_dbfs


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_options_dialog_reports_changes(app):
    changes = []
    dlg = OptionsDialog(Config(), changes.append, lambda: None, meter=False)
    dlg.gain.setValue(42)
    dlg.direction.setCurrentIndex(2)
    assert changes[-1]["gain_db"] == 42.0
    assert changes[-1]["direction_mode"] == "tracking"
    assert not dlg.manual.isEnabled()
    dlg.direction.setCurrentIndex(1)
    assert dlg.manual.isEnabled()
    dlg.close()


def test_other_dialogs_construct(app):
    CalibrationDialog(UMA8, lambda az, el: None).close()
    GeometryDialog(UMA8, lambda ran, adopt: None).close()


def test_level_dbfs():
    assert abs(level_dbfs(np.full(100, 0.5)) - (-6.02)) < 0.01
```

Run: `python -m pytest tests/test_dialogs.py -v` → Expected: FAIL mit `ModuleNotFoundError`

- [ ] **Step 3: `dialogs.py` implementieren**

```python
"""Optionen-, Kalibrierungs- und Kanalzuordnungsdialog."""
from __future__ import annotations

from typing import Callable

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QLabel, QProgressBar, QPushButton, QSlider, QVBoxLayout, QWidget)

from . import constants as K
from .array import ArrayGeometry
from .calibration import evaluate
from .capture import Capture
from .config import Config
from .doa import SrpPhat, VoiceDetector
from .geometry import check as check_geometry

DIRECTIONS = [("Kalibriert", "calibrated"), ("Manuell", "manual"),
              ("Automatisch nachführen", "tracking"), ("Alle Richtungen", "omni")]
BLOCK = 9600  # 0,2 s


def level_dbfs(block: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64)))) + 1e-12
    return 20.0 * np.log10(rms)


def _slider(lo: int, hi: int, value: float) -> QSlider:
    s = QSlider(Qt.Orientation.Horizontal)
    s.setRange(lo, hi)
    s.setValue(int(round(value)))
    return s


def _with_label(widget: QWidget, label: QLabel) -> QWidget:
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(widget, 1)
    row.addWidget(label)
    return box


class LevelMeter(QProgressBar):
    def __init__(self, lo: int = -60, hi: int = 0):
        super().__init__()
        self.setRange(lo, hi)
        self.setValue(lo)
        self.setFormat("%v dBFS")

    def show_level(self, db: float) -> None:
        self.setValue(int(max(self.minimum(), min(self.maximum(), db))))


class OptionsDialog(QDialog):
    def __init__(self, cfg: Config, on_change: Callable[[dict], None], on_check_geometry: Callable[[], None],
                 meter: bool = True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("UMA-8 Call Mic – Optionen")
        self.on_change = on_change
        form = QFormLayout()
        self.direction = QComboBox()
        for text, key in DIRECTIONS:
            self.direction.addItem(text, key)
        self.direction.setCurrentIndex([k for _, k in DIRECTIONS].index(cfg.direction_mode))
        form.addRow("Richtung", self.direction)
        cal = (f"Kalibriert: {cfg.calibrated_azimuth:.0f}°, Höhe {cfg.calibrated_elevation:.0f}°"
               if cfg.calibrated else "Noch nicht kalibriert (Tray-Menü → Kalibrieren…)")
        form.addRow("", QLabel(cal))
        self.manual = _slider(0, 359, cfg.manual_azimuth)
        self.manual_label = QLabel(f"{cfg.manual_azimuth:.0f}°")
        form.addRow("Winkel (manuell)", _with_label(self.manual, self.manual_label))
        self.dereverb = QCheckBox("Hallunterdrückung")
        self.dereverb.setChecked(cfg.dereverb)
        form.addRow("", self.dereverb)
        self.strength = _slider(0, 100, cfg.dereverb_strength * 100)
        form.addRow("Stärke", self.strength)
        self.t60 = QDoubleSpinBox()
        self.t60.setRange(0.1, 1.5)
        self.t60.setSingleStep(0.05)
        self.t60.setSuffix(" s")
        self.t60.setValue(cfg.dereverb_t60)
        form.addRow("Nachhallzeit des Raums", self.t60)
        self.noise = _slider(0, 100, cfg.noise_reduction_db)
        self.noise_label = QLabel(f"{cfg.noise_reduction_db:.0f} dB")
        form.addRow("Rauschunterdrückung", _with_label(self.noise, self.noise_label))
        self.gain = _slider(0, 60, cfg.gain_db)
        self.gain_label = QLabel(f"{cfg.gain_db:.0f} dB")
        form.addRow("Verstärkung", _with_label(self.gain, self.gain_label))
        self.meter = LevelMeter()
        form.addRow("Pegel (Ausgang)", self.meter)
        self.autostart = QCheckBox("Beim Login starten")
        self.autostart.setChecked(cfg.autostart)
        form.addRow("", self.autostart)
        geo = QPushButton("Kanalzuordnung prüfen…")
        geo.clicked.connect(on_check_geometry)
        form.addRow("", geo)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        for signal in (self.direction.currentIndexChanged, self.manual.valueChanged, self.dereverb.toggled,
                       self.strength.valueChanged, self.t60.valueChanged, self.noise.valueChanged,
                       self.gain.valueChanged, self.autostart.toggled):
            signal.connect(self._changed)
        self._update_enabled()
        self.capture = None
        if meter:
            self.capture = Capture(K.SOURCE_NODE, 1, seconds=1.0)
            self.timer = QTimer(self)
            self.timer.timeout.connect(self._meter_tick)
            self.timer.start(100)
        self.finished.connect(self._cleanup)

    def _update_enabled(self) -> None:
        self.manual.setEnabled(self.direction.currentData() == "manual")
        self.strength.setEnabled(self.dereverb.isChecked())
        self.t60.setEnabled(self.dereverb.isChecked())

    def _changed(self, *_):
        updates = {
            "direction_mode": self.direction.currentData(),
            "manual_azimuth": float(self.manual.value()),
            "dereverb": self.dereverb.isChecked(),
            "dereverb_strength": self.strength.value() / 100.0,
            "dereverb_t60": round(self.t60.value(), 2),
            "noise_reduction_db": float(self.noise.value()),
            "gain_db": float(self.gain.value()),
            "autostart": self.autostart.isChecked(),
        }
        self.manual_label.setText(f"{updates['manual_azimuth']:.0f}°")
        self.noise_label.setText(f"{updates['noise_reduction_db']:.0f} dB")
        self.gain_label.setText(f"{updates['gain_db']:.0f} dB")
        self._update_enabled()
        self.on_change(updates)

    def _meter_tick(self) -> None:
        block = self.capture.latest(4800) if self.capture else None
        if block is not None:
            self.meter.show_level(level_dbfs(block))

    def _cleanup(self, *_):
        if self.capture:
            self.capture.close()
            self.capture = None


class _RecordingDialog(QDialog):
    """Gemeinsame Aufnahme-Mechanik: Start, Fortschritt, Pegel, Übernehmen/Abbrechen."""

    def __init__(self, title: str, intro: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.info = QLabel(intro)
        self.info.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.meter = LevelMeter(-100, -30)
        self.start_btn = QPushButton("Start")
        self.accept_btn = QPushButton("Übernehmen")
        self.accept_btn.setEnabled(False)
        cancel = QPushButton("Abbrechen")
        self.start_btn.clicked.connect(self.start)
        self.accept_btn.clicked.connect(self._accept)
        cancel.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.start_btn)
        row.addStretch(1)
        row.addWidget(self.accept_btn)
        row.addWidget(cancel)
        layout = QVBoxLayout(self)
        layout.addWidget(self.info)
        layout.addWidget(self.progress)
        layout.addWidget(self.meter)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.capture = None
        self.ticks = 0
        self.finished.connect(self._cleanup)

    def _begin(self, seconds: float) -> None:
        self._cleanup()
        self.ticks = 0
        self.accept_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.capture = Capture(K.RAW_DEVICE, 8, seconds=seconds)
        self.timer.start(200)

    def _finish(self) -> None:
        self.timer.stop()
        self._cleanup()
        self.start_btn.setEnabled(True)
        self.start_btn.setText("Wiederholen")

    def _no_data(self) -> bool:
        if self.ticks >= 10 and self.capture.total() == 0:
            self._finish()
            self.info.setText("Keine Daten vom UMA-8 – angeschlossen und mit Raw-Firmware?")
            return True
        return False

    def _cleanup(self, *_):
        self.timer.stop()
        if self.capture:
            self.capture.close()
            self.capture = None

    def start(self):
        raise NotImplementedError

    def _tick(self):
        raise NotImplementedError

    def _accept(self):
        raise NotImplementedError


class CalibrationDialog(_RecordingDialog):
    QUIET_TICKS, SPEAK_TICKS = 10, 25  # 2 s still, 5 s sprechen

    def __init__(self, geometry: ArrayGeometry, on_accept: Callable[[float, float], None], parent=None):
        super().__init__("UMA-8 Call Mic – Kalibrieren",
                         "Setz dich hin wie beim Telefonieren. Nach dem Start: 2 Sekunden still sein, "
                         "dann 5 Sekunden normal sprechen.", parent)
        self.geometry, self.on_accept = geometry, on_accept
        self.result: tuple[float, float] | None = None

    def start(self):
        self.estimator = SrpPhat(self.geometry.positions())
        self.vad = VoiceDetector()
        self.results = []
        self.result = None
        self.info.setText("Bitte still sein …")
        self._begin(3.0)

    def _tick(self):
        self.ticks += 1
        total = self.QUIET_TICKS + self.SPEAK_TICKS
        self.progress.setValue(int(100 * min(self.ticks, total) / total))
        if self._no_data():
            return
        block = self.capture.latest(BLOCK)
        if block is None:
            return
        center = block[:, self.geometry.center]
        self.meter.show_level(level_dbfs(center))
        speech = self.vad.is_speech(center)
        if self.ticks == self.QUIET_TICKS:
            self.info.setText("Jetzt normal sprechen …")
        if self.ticks > self.QUIET_TICKS and speech:
            self.results.append(self.estimator.estimate(block[:, :7]))
        if self.ticks >= total:
            self._finish()
            outcome = evaluate(self.results)
            self.info.setText(outcome.message)
            if outcome.ok:
                self.result = (outcome.azimuth, outcome.elevation)
                self.accept_btn.setEnabled(True)

    def _accept(self):
        if self.result:
            self.on_accept(*self.result)
        self.accept()


class GeometryDialog(_RecordingDialog):
    TICKS = 50  # 10 s

    def __init__(self, default: ArrayGeometry, on_done: Callable[[bool, ArrayGeometry | None], None], parent=None):
        super().__init__("UMA-8 Call Mic – Kanalzuordnung",
                         "Prüft, welcher Kanal welches Mikrofon ist. Bitte 10 Sekunden still sein; "
                         "normales Raumgeräusch oder ein Lüfter sind gut.", parent)
        self.default, self.on_done = default, on_done
        self.detected: ArrayGeometry | None = None
        self.ran = False

    def start(self):
        self.detected = None
        self.info.setText("Messe Raumgeräusch …")
        self._begin(12.0)

    def _tick(self):
        self.ticks += 1
        self.progress.setValue(int(100 * min(self.ticks, self.TICKS) / self.TICKS))
        if self._no_data():
            return
        recent = self.capture.latest(BLOCK)
        if recent is not None:
            self.meter.show_level(level_dbfs(recent[:, self.default.center]))
        if self.ticks < self.TICKS:
            return
        block = self.capture.latest(self.TICKS * BLOCK)
        self._finish()
        if block is None:
            self.info.setText("Zu wenig Daten – bitte wiederholen.")
            return
        res = check_geometry(block, self.default)
        self.ran = True
        self.info.setText(f"{res.message} (Kontrast {res.contrast:.2f})")
        if res.detected is not None and not res.matches_default:
            self.detected = res.detected
            self.accept_btn.setText("Erkannte Zuordnung übernehmen")
            self.accept_btn.setEnabled(True)
        self.on_done(True, None)

    def _accept(self):
        if self.detected is not None:
            self.on_done(True, self.detected)
        self.accept()
```

- [ ] **Step 4: `tray.py` implementieren**

```python
"""KDE-Tray-Anwendung: Zustand anzeigen, umschalten, Dialoge öffnen, nachführen."""
from __future__ import annotations

import logging
import shutil

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import chainconf, pwctl
from . import constants as K
from .array import ArrayGeometry
from .capture import Capture
from .config import load, save
from .dialogs import CalibrationDialog, GeometryDialog, OptionsDialog
from .doa import SrpPhat, VoiceDetector
from .params import all_params, steering_params
from .traystate import icon_state, tooltip
from .tracker import Tracker, TrackerThread

log = logging.getLogger(__name__)
ICON_DIR = K.REPO_DIR / "uma8_callmic" / "icons"


class TrayApp:
    def __init__(self, app):
        self.app = app
        res = load(K.CONFIG_FILE)
        self.cfg, self.cfg_broken, self.warnings = res.config, res.broken, list(res.warnings)
        self.tracked: float | None = None
        self.tracker_thread: TrackerThread | None = None
        self.tracker_capture: Capture | None = None
        self.chain_node: int | None = None
        self.dialogs: dict[str, object] = {}
        self.icons = {name: QIcon(str(ICON_DIR / f"{name}.svg")) for name in ("active", "inactive", "error")}

        self.tray = QSystemTrayIcon(self.icons["inactive"])
        menu = QMenu()
        self.act_active = QAction("Aktiv", menu)
        self.act_active.setCheckable(True)
        self.act_active.setChecked(self.cfg.active)
        self.act_active.toggled.connect(self.set_active)
        menu.addAction(self.act_active)
        menu.addSeparator()
        menu.addAction("Kalibrieren…", self.open_calibration)
        menu.addAction("Optionen…", self.open_options)
        menu.addSeparator()
        menu.addAction("Beenden", app.quit)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.show()

        chainconf.write(self.cfg)
        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()
        self.update_tracking()
        if not self.cfg.geometry_checked:
            QTimer.singleShot(1500, self.open_geometry)
        elif not self.cfg.calibrated:
            self.tray.showMessage("UMA-8 Call Mic", "Bitte einmal kalibrieren: Rechtsklick → Kalibrieren…")

    # --- Zustand ---------------------------------------------------------------

    def refresh(self) -> None:
        st = pwctl.status()
        try:
            node = pwctl.find_node(pwctl.dump(), K.CAPTURE_NODE) if st.chain else None
        except Exception:  # pw-dump hängt/fehlt: Zustand beim nächsten Durchlauf erneut prüfen
            log.warning("pw-dump fehlgeschlagen", exc_info=True)
            node = None
        if node is not None and node != self.chain_node:
            self.chain_node = node
            self.apply_all()  # Kette (neu) gestartet: Live-Werte an Einstellungen angleichen
        elif node is None:
            self.chain_node = None
        self.tray.setIcon(self.icons[icon_state(st, self.cfg.active)])
        self.tray.setToolTip(tooltip(st, self.cfg, self.tracked, self.warnings))

    def _set(self, params: dict[str, float]) -> None:
        if self.chain_node is None:
            return
        try:
            pwctl.set_params(self.chain_node, params)
        except (RuntimeError, OSError) as e:
            log.warning("%s", e)

    def apply_all(self) -> None:
        self._set(all_params(self.cfg, self.tracked))

    def save_config(self) -> None:
        save(self.cfg, K.CONFIG_FILE, broken=self.cfg_broken)
        self.cfg_broken = False
        chainconf.write(self.cfg)

    # --- Aktionen --------------------------------------------------------------

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.act_active.toggle()

    def set_active(self, checked: bool) -> None:
        self.cfg.active = checked
        self.save_config()
        if self.chain_node is not None:
            try:
                pwctl.fade_mix(self.chain_node, checked)
            except (RuntimeError, OSError) as e:
                log.warning("%s", e)
        self.refresh()

    def apply_options(self, updates: dict) -> None:
        autostart_before = self.cfg.autostart
        for key, value in updates.items():
            setattr(self.cfg, key, value)
        self.save_config()
        self.apply_all()
        if self.cfg.autostart != autostart_before:
            self.set_autostart(self.cfg.autostart)
        self.update_tracking()
        self.refresh()

    def set_autostart(self, enabled: bool) -> None:
        if enabled:
            template = (K.REPO_DIR / "pipewire" / "uma8-callmic.desktop").read_text()
            K.AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
            K.AUTOSTART_FILE.write_text(template.replace("@BIN@", shutil.which("uma8-callmic") or str(K.LAUNCHER)))
        elif K.AUTOSTART_FILE.exists():
            K.AUTOSTART_FILE.unlink()

    def calibrated(self, azimuth: float, elevation: float) -> None:
        self.cfg.calibrated = True
        self.cfg.calibrated_azimuth = round(azimuth, 1)
        self.cfg.calibrated_elevation = round(elevation, 1)
        self.save_config()
        self._set(steering_params(self.cfg, self.tracked))
        self.refresh()

    def geometry_done(self, ran: bool, adopt: ArrayGeometry | None) -> None:
        if ran:
            self.cfg.geometry_checked = True
        if adopt is not None:
            self.cfg.center_channel, self.cfg.ring = adopt.center, list(adopt.ring)
        self.save_config()
        self.apply_all()
        if ran and not self.cfg.calibrated:
            self.tray.showMessage("UMA-8 Call Mic", "Jetzt bitte einmal kalibrieren: Rechtsklick → Kalibrieren…")

    def _show(self, key: str, factory) -> None:
        dlg = self.dialogs.get(key)
        if dlg is None or not dlg.isVisible():
            dlg = factory()
            self.dialogs[key] = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def open_options(self) -> None:
        self._show("options", lambda: OptionsDialog(self.cfg, self.apply_options, self.open_geometry))

    def open_calibration(self) -> None:
        self._show("calibration", lambda: CalibrationDialog(self.cfg.geometry(), self.calibrated))

    def open_geometry(self) -> None:
        self._show("geometry", lambda: GeometryDialog(self.cfg.geometry(), self.geometry_done))

    # --- Nachführung -----------------------------------------------------------

    def _apply_tracked(self, azimuth: float) -> None:
        self.tracked = azimuth
        self._set(steering_params(self.cfg, azimuth))

    def update_tracking(self) -> None:
        want = self.cfg.direction_mode == "tracking"
        if want and self.tracker_thread is None:
            self.tracker_capture = Capture(K.RAW_DEVICE, 8, seconds=3.0)
            tracker = Tracker(SrpPhat(self.cfg.geometry().positions()), VoiceDetector(), self._apply_tracked,
                              initial_azimuth=self.cfg.calibrated_azimuth, center=self.cfg.center_channel)
            self.tracker_thread = TrackerThread(self.tracker_capture, tracker)
            self.tracker_thread.start()
        elif not want and self.tracker_thread is not None:
            self._stop_tracking()

    def _stop_tracking(self) -> None:
        if self.tracker_thread:
            self.tracker_thread.stop()
            self.tracker_thread = None
        if self.tracker_capture:
            self.tracker_capture.close()
            self.tracker_capture = None
        self.tracked = None

    def shutdown(self) -> None:
        self._stop_tracking()
        for dlg in self.dialogs.values():
            dlg.close()
```

- [ ] **Step 5: `__main__.py` implementieren**

```python
"""Einstieg: Tray-Anwendung oder Hilfsbefehle."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from logging.handlers import RotatingFileHandler

from . import constants as K


def _check_geometry_cli() -> int:
    from .capture import Capture
    from .config import load
    from .geometry import check

    cfg = load(K.CONFIG_FILE).config
    print("Bitte 10 Sekunden still sein …", flush=True)
    cap = Capture(K.RAW_DEVICE, 8, seconds=12.0)
    try:
        time.sleep(10.5)
        block = cap.latest(10 * K.SAMPLE_RATE)
    finally:
        cap.close()
    if block is None:
        print("Keine Daten vom UMA-8 (angeschlossen, Raw-Firmware?)")
        return 1
    res = check(block, cfg.geometry())
    print(res.message)
    print(f"Kontrast: {res.contrast:.3f}")
    if res.detected is not None:
        print(f"Erkannt: Mitte {res.detected.center}, Ring {list(res.detected.ring)}")
    return 0 if res.matches_default else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="uma8-callmic", description="UMA-8 Call Mic")
    ap.add_argument("--write-config", action="store_true",
                    help="PipeWire-Konfiguration aus den Einstellungen schreiben und beenden")
    ap.add_argument("--check-geometry", action="store_true",
                    help="Kanalzuordnung 10 s lang prüfen und Ergebnis ausgeben")
    args = ap.parse_args(argv)

    K.STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[RotatingFileHandler(K.STATE_DIR / "log", maxBytes=1_000_000, backupCount=2)])

    if args.write_config:
        from .chainconf import write
        from .config import load

        write(load(K.CONFIG_FILE).config)
        print(K.CHAIN_CONF)
        return 0
    if args.check_geometry:
        return _check_geometry_cli()

    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication

    from .tray import TrayApp

    lock = QLockFile(str(K.STATE_DIR / "tray.lock"))
    if not lock.tryLock(100):
        print("uma8-callmic läuft bereits.")
        return 0
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("uma8-callmic")
    app.setDesktopFileName("uma8-callmic")
    tray = TrayApp(app)
    code = app.exec()
    tray.shutdown()
    return code


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Tests laufen lassen**

Run: `python -m pytest -v`
Expected: alle Tests grün (inkl. 3 neue in `test_dialogs.py`); integration/hardware deselected.

Zusätzlich: `python -m uma8_callmic --help` zeigt beide Optionen.

- [ ] **Step 7: Stand prüfen, nicht committen**

Run: `git status --short` und berichten.

---

### Task 13: Installation, Deinstallation, README

**Files:**
- Create: `install.sh`, `uninstall.sh`, `README.md`, `tools/check_output.py`

**Interfaces:**
- Consumes: `python -m uma8_callmic --write-config`, Vorlagen aus Task 7.
- Produces: installierte Dateien laut Spec; Befehl `uma8-callmic` in `~/.local/bin`; `tools/check_output.py` (für Task 14).

- [ ] **Step 1: `install.sh` schreiben**

```bash
#!/usr/bin/env bash
# Installiert uma8-callmic für den aktuellen Benutzer (ohne root).
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
LADSPA_DIR="$HOME/.local/lib/ladspa"
BIN="$HOME/.local/bin/uma8-callmic"
UNIT_DIR="$HOME/.config/systemd/user"
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf 'Fehler: %s\n' "$*" >&2; exit 1; }

say "Voraussetzungen prüfen"
command -v pipewire >/dev/null || fail "PipeWire fehlt"
command -v cargo >/dev/null || fail "Rust fehlt: sudo pacman -S rust"
python3 -c 'import PySide6, numpy' 2>/dev/null || fail "Python-Module fehlen: sudo pacman -S pyside6 python-numpy"
[ -f /usr/lib/ladspa/libdeep_filter_ladspa.so ] || fail "DeepFilterNet fehlt: yay -S deepfilternet-plugin-pipewire-bin"
lsusb -d 2752:001d >/dev/null || echo "Hinweis: UMA-8 mit Raw-Firmware (2752:001d) nicht gefunden – Installation läuft weiter."

say "Plugin bauen"
cargo build --release --quiet --manifest-path "$REPO/plugin/Cargo.toml"
install -Dm644 "$REPO/plugin/target/release/libuma8_beam.so" "$LADSPA_DIR/libuma8_beam.so"

say "Startbefehl einrichten"
install -d "$(dirname "$BIN")"
cat > "$BIN" <<EOF
#!/bin/sh
PYTHONPATH="$REPO\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m uma8_callmic "\$@"
EOF
chmod 755 "$BIN"

say "Filterkette und Dienst einrichten"
"$BIN" --write-config >/dev/null
install -Dm644 "$REPO/pipewire/uma8-callmic-chain.service" "$UNIT_DIR/uma8-callmic-chain.service"
systemctl --user daemon-reload
systemctl --user enable uma8-callmic-chain.service
systemctl --user restart uma8-callmic-chain.service

say "Autostart einrichten"
install -Dm644 "$REPO/pipewire/uma8-callmic.desktop" "$HOME/.config/autostart/uma8-callmic.desktop"
sed -i "s|@BIN@|$BIN|" "$HOME/.config/autostart/uma8-callmic.desktop"

say "Als Standard-Mikrofon setzen"
sleep 2
pactl set-default-source uma8_callmic || echo "Hinweis: Standard-Mikrofon nicht gesetzt (Dienst prüfen: systemctl --user status uma8-callmic-chain)"

say "Fertig. Tray jetzt starten mit: uma8-callmic &"
```

- [ ] **Step 2: `uninstall.sh` schreiben**

```bash
#!/usr/bin/env bash
# Entfernt uma8-callmic. Einstellungen bleiben, außer man bestätigt die Löschung.
set -euo pipefail
systemctl --user disable --now uma8-callmic-chain.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/uma8-callmic-chain.service"
systemctl --user daemon-reload
pkill -f "python3 -m uma8_callmic" 2>/dev/null || true
rm -f "$HOME/.config/pipewire/uma8-callmic.conf" \
      "$HOME/.local/lib/ladspa/libuma8_beam.so" \
      "$HOME/.local/bin/uma8-callmic" \
      "$HOME/.config/autostart/uma8-callmic.desktop"
read -r -p "Einstellungen und Log löschen? [j/N] " answer
if [ "${answer,,}" = "j" ]; then
    rm -rf "$HOME/.config/uma8-callmic" "$HOME/.local/state/uma8-callmic"
fi
echo "uma8-callmic entfernt."
```

- [ ] **Step 3: `tools/check_output.py` schreiben**

```python
#!/usr/bin/env python3
"""Nimmt 10 s vom virtuellen Mikrofon auf und prüft Aussetzer, Pegel und Bandbreite."""
import subprocess
import sys

import numpy as np

SR, SECONDS = 48000, 10
raw = subprocess.run(
    ["timeout", str(SECONDS), "pw-record", "--target", "uma8_callmic", "--rate", str(SR),
     "--channels", "1", "--format", "f32", "--raw", "-"],
    capture_output=True,
).stdout
x = np.frombuffer(raw[: len(raw) - len(raw) % 4], dtype=np.float32)[SR // 2:]
ok = True
print(f"Samples: {len(x)} (erwartet ≈ {(SECONDS - 0.5) * SR:.0f})")
if len(x) < (SECONDS - 1.5) * SR:
    print("FEHLER: zu wenige Samples – Kette läuft nicht oder stockt")
    ok = False
zero_runs = np.diff(np.flatnonzero(np.diff(np.concatenate([[1], (x == 0).astype(int), [1]]))))[::2]
longest = int(zero_runs.max()) if zero_runs.size else 0
print(f"Längste Folge exakter Nullen: {longest} Samples")
if longest > SR // 100:
    print("FEHLER: Aussetzer (> 10 ms Stille)")
    ok = False
rms_db = 20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-12)
print(f"Pegel: {rms_db:.1f} dBFS")
spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
f = np.fft.rfftfreq(len(x), 1 / SR)
ratio = 10 * np.log10(spec[(f > 8000) & (f < 16000)].mean() / spec[(f > 1000) & (f < 4000)].mean())
print(f"Energie 8–16 kHz relativ zu 1–4 kHz: {ratio:.1f} dB")
if ratio < -60:
    print("FEHLER: keine Energie oberhalb 8 kHz")
    ok = False
print("OK" if ok else "PROBLEME GEFUNDEN")
sys.exit(0 if ok else 1)
```

- [ ] **Step 4: `README.md` schreiben**

````markdown
# uma8-callmic

Macht aus dem miniDSP UMA-8 (Raw-Firmware) ein gutes Telefonie-Mikrofon unter Linux:
Beamforming über 7 Mikrofone, optionale Hallunterdrückung, DeepFilterNet-Rauschunterdrückung,
volle Bandbreite. Bedienung über ein KDE-Tray-Icon.

## Voraussetzungen

- Arch Linux mit PipeWire und WirePlumber
- `sudo pacman -S rust pyside6 python-numpy`
- `yay -S deepfilternet-plugin-pipewire-bin`
- UMA-8 mit **Raw-Firmware** (`micArray_vf_raw_v1.3_up.bin`, USB-ID `2752:001d`). Firmware nur mit dem
  offiziellen miniDSP-Tool wechseln (Windows-VM: siehe `firmware/docker-compose.usb.yml`).

## Installation

```sh
./install.sh
uma8-callmic &
```

Beim ersten Start prüft das Programm die Kanalzuordnung (10 s still sein) und bittet danach um eine
Kalibrierung (Rechtsklick → Kalibrieren…).

## Bedienung

- Linksklick aufs Icon: aktiv ↔ deaktiviert (Rohsignal)
- Rechtsklick: Aktiv, Kalibrieren…, Optionen…, Beenden
- Icon grün = aktiv, grau = deaktiviert, rot = Problem (Tooltip zeigt die Ursache)

## Tests

```sh
python -m pytest            # Unit-Tests
cargo test --manifest-path plugin/Cargo.toml
python -m pytest -m integration   # nach install.sh, braucht PipeWire
python3 tools/check_output.py     # mit angeschlossenem UMA-8
```

## Deinstallation

```sh
./uninstall.sh
```
````

- [ ] **Step 5: Skripte prüfen**

Run: `chmod +x install.sh uninstall.sh tools/check_output.py && bash -n install.sh && bash -n uninstall.sh && python -m py_compile tools/check_output.py && echo ok`
Expected: `ok`

- [ ] **Step 6: Stand prüfen, nicht committen**

Run: `git status --short` und berichten.

---

### Task 14: Inbetriebnahme am Gerät und Abnahme

Braucht das angeschlossene UMA-8 mit Raw-Firmware und den Nutzer. Keine DFU-Befehle.

**Files:**
- Modify: nur falls Befunde es erfordern (dann Befund zuerst dem Nutzer berichten).

- [ ] **Step 1: Installieren**

Run: `./install.sh`
Expected: alle Schritte ohne Fehler; `systemctl --user is-active uma8-callmic-chain` → `active`; `pactl list sources short | grep uma8_callmic` zeigt die Quelle.

- [ ] **Step 2: Integrationstest der Konfiguration**

Run: `python -m pytest -m integration -v`
Expected: 1 passed

- [ ] **Step 3: Kanalzuordnung am echten Gerät prüfen**

Nutzer bitten, 10 s still zu sein. Run: `uma8-callmic --check-geometry`
Expected: „Standardzuordnung bestätigt.“ (Exit 0). Bei „Abweichende Kanalzuordnung erkannt“ (Exit 2): Ergebnis dem Nutzer zeigen, erkannte Zuordnung in `~/.config/uma8-callmic/config.toml` übernehmen (`center_channel`, `ring`) und erneut prüfen. Bei „zu leise“: Standard behalten und im Bericht vermerken.

- [ ] **Step 4: Tray starten, Kalibrierung durch den Nutzer**

Run: `uma8-callmic &`
Nutzer: Rechtsklick → Kalibrieren… → Start, 2 s still, 5 s sprechen → Übernehmen.
Expected: Tooltip zeigt „Richtung: kalibriert (xxx°)“.

- [ ] **Step 5: Ausgang technisch prüfen**

Nutzer bitten, während der Messung normal zu sprechen. Run: `python3 tools/check_output.py`
Expected: `OK` – keine Aussetzer, Pegel zwischen −35 und −10 dBFS beim Sprechen, Energie oberhalb 8 kHz vorhanden. Liegt der Pegel außerhalb: Verstärkung in den Optionen anpassen und Wert berichten.

- [ ] **Step 6: Umschalten ohne Klick prüfen**

Während `pw-record --target uma8_callmic --rate 48000 --channels 1 /tmp/claude-…/scratchpad/toggle.wav` 15 s läuft, klickt der Nutzer dreimal aufs Icon. Danach mit numpy die größte Sample-Differenz um die Umschaltzeitpunkte gegen die Umgebung vergleichen; kein Ausreißer > 3× typische Differenz.

- [ ] **Step 7: Hörvergleich und echter Anruf durch den Nutzer**

Nutzer vergleicht aktiv/deaktiviert per Linksklick und führt einen echten Anruf. Rückmeldung zu Klang, Rauschen, Hall und Pegel einholen; Einstellungen gemeinsam anpassen.

- [ ] **Step 8: Abschlussbericht, nicht committen**

Run: `git status --short`. Dem Nutzer berichten: Testergebnisse, Messwerte, offene Punkte. Commit/Push nur auf ausdrückliche Anweisung.
