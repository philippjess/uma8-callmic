//! `run()` darf nicht allozieren – auch nicht bei Wechseln von Richtung, Geometrie, Modus und
//! Nullstellen oder bei unsinnigen Controls und Eingangswerten. Zählt die Allokationen des
//! Test-Threads über einen eigenen globalen Allokator, Plugin über die LADSPA-Schnittstelle.

use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;
use std::ffi::CStr;
use std::sync::atomic::{AtomicUsize, Ordering};

struct Counting;

thread_local! {
    static COUNTING: Cell<bool> = const { Cell::new(false) };
}
static ALLOCS: AtomicUsize = AtomicUsize::new(0);

fn count() {
    if COUNTING.try_with(|c| c.get()).unwrap_or(false) {
        ALLOCS.fetch_add(1, Ordering::Relaxed);
    }
}

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        count();
        System.alloc(l)
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        count();
        System.dealloc(p, l)
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, size: usize) -> *mut u8 {
        count();
        System.realloc(p, l, size)
    }
}

#[global_allocator]
static ALLOCATOR: Counting = Counting;

const BLOCK: usize = 480;

#[test]
fn run_does_not_allocate() {
    // der Zähler sieht Allokationen dieses Threads
    COUNTING.with(|c| c.set(true));
    std::hint::black_box(Box::new([0u8; 64]));
    COUNTING.with(|c| c.set(false));
    assert_eq!(ALLOCS.swap(0, Ordering::Relaxed), 2);

    let d = unsafe { &*uma8_beam::ladspa_descriptor(0) };
    let n = d.port_count as usize;
    let names: Vec<String> =
        (0..n).map(|i| unsafe { CStr::from_ptr(*d.port_names.add(i)) }.to_string_lossy().into_owned()).collect();
    let port = |name: &str| names.iter().position(|p| p == name).unwrap_or_else(|| panic!("Port {name}"));
    let h = unsafe { (d.instantiate.unwrap())(d, 48000) };
    assert!(!h.is_null());
    let mut ctl = vec![0.0f32; n];
    for (name, v) in [
        ("Center Channel", 0.0),
        ("Ring 0", 1.0),
        ("Ring 1", 6.0),
        ("Ring 2", 5.0),
        ("Ring 3", 4.0),
        ("Ring 4", 3.0),
        ("Ring 5", 2.0),
        ("Ring Offset (deg)", 90.0),
        ("Radius (mm)", 43.0),
        ("Gain (dB)", 30.0),
        ("Dereverb", 1.0),
        ("Dereverb Strength", 0.6),
        ("Dereverb T60 (s)", 0.5),
        ("Raw Extra Delay (samples)", 960.0),
        ("Late Reverb", 1.0),
        ("Min WNG (dB)", -3.0),
        ("Azimuth (deg)", 80.0),
        ("Elevation (deg)", 30.0),
    ] {
        ctl[port(name)] = v;
    }
    let mut seed = 1u32;
    let mut ins: Vec<Vec<f32>> = (0..7)
        .map(|_| {
            (0..BLOCK)
                .map(|_| {
                    seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
                    ((seed >> 8) as f32 / (1u32 << 24) as f32 - 0.5) * 0.1
                })
                .collect()
        })
        .collect();
    let mut outs = vec![vec![0.0f32; BLOCK]; 2];
    // Host und Test greifen nur über diese Zeiger zu
    let in_ptr: Vec<*mut f32> = ins.iter_mut().map(|b| b.as_mut_ptr()).collect();
    let out_ptr: Vec<*mut f32> = outs.iter_mut().map(|b| b.as_mut_ptr()).collect();
    let ctl_ptr = ctl.as_mut_ptr();
    unsafe {
        let connect = d.connect_port.unwrap();
        for (c, &p) in in_ptr.iter().enumerate() {
            connect(h, c as _, p);
        }
        connect(h, port("Beam Out") as _, out_ptr[0]);
        connect(h, port("Raw Out") as _, out_ptr[1]);
        for i in port("Azimuth (deg)")..n {
            connect(h, i as _, ctl_ptr.add(i));
        }
        (d.activate.unwrap())(h);
    }
    let run = d.run.unwrap();
    let set = |name: &str, v: f32| unsafe { *ctl_ptr.add(port(name)) = v };
    let beam_out = || unsafe { std::slice::from_raw_parts(out_ptr[0], BLOCK) }.iter().all(|v| v.is_finite());
    type Step<'a> = (&'a str, Box<dyn Fn() + 'a>);
    let steps: Vec<Step> = vec![
        ("steady", Box::new(|| {})),
        (
            "nulls on",
            Box::new(|| {
                set("Null 1 Azimuth (deg)", 180.0);
                set("Null 1 Elevation (deg)", 5.0);
                set("Null 2 Azimuth (deg)", 340.0);
                set("Null 2 Elevation (deg)", 5.0);
                set("Null Weight (dB)", 10.0);
            }),
        ),
        ("null direction", Box::new(|| set("Null 2 Azimuth (deg)", 330.0))),
        ("azimuth with nulls", Box::new(|| set("Azimuth (deg)", 120.0))),
        ("radius with nulls", Box::new(|| set("Radius (mm)", 40.0))),
        ("ring offset with nulls", Box::new(|| set("Ring Offset (deg)", 100.0))),
        (
            "ring remap",
            Box::new(|| {
                set("Ring 0", 6.0);
                set("Ring 1", 1.0);
            }),
        ),
        ("mode DS", Box::new(|| set("Mode", 2.0))),
        ("mode SD", Box::new(|| set("Mode", 0.0))),
        ("weight max", Box::new(|| set("Null Weight (dB)", 1e9))),
        (
            "NaN null controls",
            Box::new(|| {
                set("Null 1 Azimuth (deg)", f32::NAN);
                set("Null Weight (dB)", f32::INFINITY);
            }),
        ),
        (
            "same null twice",
            Box::new(|| {
                set("Null 1 Azimuth (deg)", 340.0);
                set("Null 2 Azimuth (deg)", 340.0);
                set("Null Weight (dB)", 20.0);
            }),
        ),
        ("invalid ring", Box::new(|| set("Ring 0", 1.0))),
        ("nulls off", Box::new(|| set("Null Weight (dB)", 0.0))),
    ];
    for (name, change) in &steps {
        change();
        COUNTING.with(|c| c.set(true));
        for _ in 0..60 {
            unsafe { run(h, BLOCK as _) };
        }
        COUNTING.with(|c| c.set(false));
        assert_eq!(ALLOCS.swap(0, Ordering::Relaxed), 0, "{name}: run() alloziert");
        assert!(beam_out(), "{name}");
    }
    unsafe {
        *in_ptr[3].add(7) = f32::NAN;
        *in_ptr[5].add(9) = 3e38;
    }
    COUNTING.with(|c| c.set(true));
    unsafe { run(h, BLOCK as _) };
    COUNTING.with(|c| c.set(false));
    assert_eq!(ALLOCS.load(Ordering::Relaxed), 0, "NaN-Eingang: run() alloziert");
    unsafe { (d.cleanup.unwrap())(h) };
}
