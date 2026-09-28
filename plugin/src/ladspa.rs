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
        if v.is_finite() {
            v
        } else {
            fallback
        }
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
