//! Teuerster einzelner `run()`-Aufruf je Parameterwechsel (Entwurf in Etappen + Überblendung),
//! über die LADSPA-Schnittstelle mit 480 Samples je Aufruf (10 ms bei 48 kHz) oder `[Blockgröße]`.
//!
//!     cargo run --release --example run_cost [-- 256]

use std::ffi::CStr;
use std::time::Instant;

fn main() {
    let block: usize = std::env::args().nth(1).and_then(|a| a.parse().ok()).unwrap_or(480);
    let d = unsafe { &*uma8_beam::ladspa_descriptor(0) };
    let n = d.port_count as usize;
    let names: Vec<String> =
        (0..n).map(|i| unsafe { CStr::from_ptr(*d.port_names.add(i)) }.to_string_lossy().into_owned()).collect();
    let port = |name: &str| names.iter().position(|p| p == name).unwrap_or_else(|| panic!("Port {name}"));
    let h = unsafe { (d.instantiate.unwrap())(d, 48000) };
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
        ("Late Reverb", 1.0),
        ("Min WNG (dB)", -3.0),
        ("Azimuth (deg)", 80.0),
        ("Elevation (deg)", 30.0),
        ("Null 1 Azimuth (deg)", 180.0),
        ("Null 1 Elevation (deg)", 5.0),
        ("Null 2 Azimuth (deg)", 340.0),
        ("Null 2 Elevation (deg)", 5.0),
    ] {
        ctl[port(name)] = v;
    }
    let mut seed = 1u32;
    let mut ins: Vec<Vec<f32>> = (0..7)
        .map(|_| {
            (0..block)
                .map(|_| {
                    seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
                    ((seed >> 8) as f32 / (1u32 << 24) as f32 - 0.5) * 0.1
                })
                .collect()
        })
        .collect();
    let mut outs = vec![vec![0.0f32; block]; 2];
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
    let call = || {
        let t = Instant::now();
        unsafe { run(h, block as _) };
        t.elapsed().as_secs_f64() * 1e6
    };
    for _ in 0..200 {
        call();
    }
    let mut steady: Vec<f64> = (0..3000).map(|_| call()).collect();
    steady.sort_by(f64::total_cmp);
    println!(
        "Block {block} ({:.1} ms): ohne Wechsel Median {:.0} µs, 99,9 % {:.0} µs, max {:.0} µs",
        block as f64 / 48.0,
        steady[1500],
        steady[2997],
        steady[2999]
    );
    // Je Übergang (Entwurf + Überblendung, 0,8 s) der teuerste Aufruf; Median über 15 Übergänge
    // blendet einzelne Störungen durch das Betriebssystem aus.
    let calls = 38400 / block;
    let report = |what: &str, change: &dyn Fn(usize)| {
        let mut worst: Vec<f64> = (0..15)
            .map(|i| {
                change(i);
                (0..calls).map(|_| call()).fold(0.0, f64::max)
            })
            .collect();
        worst.sort_by(f64::total_cmp);
        println!("  {what:34} teuerster Aufruf: Median {:5.0} µs, min {:5.0} µs", worst[7], worst[0]);
    };
    // jeder Übergang ist ein echter Wechsel (Werte abwechselnd, ab dem zweiten)
    let alt = |i: usize| ((i + 1) % 2) as f32;
    report("kein Wechsel", &|_| {});
    report("Richtung, ohne Nullstellen", &|i| set("Azimuth (deg)", 80.0 + 20.0 * alt(i)));
    report("Radius, ohne Nullstellen", &|i| set("Radius (mm)", 43.0 - alt(i)));
    set("Radius (mm)", 43.0);
    report("Nullstellen an (neu zerlegt)", &|i| {
        set("Null Weight (dB)", 0.0);
        set("Null 1 Azimuth (deg)", 170.0 + i as f32);
        for _ in 0..calls {
            unsafe { run(h, block as _) };
        }
        set("Null Weight (dB)", 10.0);
    });
    report("Nullrichtung", &|i| set("Null 1 Azimuth (deg)", 190.0 + i as f32));
    report("Null Weight", &|i| set("Null Weight (dB)", 10.0 + alt(i)));
    report("Richtung, mit Nullstellen", &|i| set("Azimuth (deg)", 80.0 + 20.0 * alt(i)));
    report("Radius, mit Nullstellen", &|i| set("Radius (mm)", 43.0 - alt(i)));
    report("Ringdrehung, mit Nullstellen", &|i| set("Ring Offset (deg)", 90.0 + alt(i)));
    unsafe { (d.cleanup.unwrap())(h) };
}
