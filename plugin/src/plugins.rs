//! Die LADSPA-Plugins der Bibliothek.

use crate::beam::{Geometry, Mode, Nulls, Steering, CHANNELS, NULL_WEIGHT_MAX_DB};
use crate::ladspa::*;
use crate::limiter::Limiter;
use crate::pipeline::{Params, Pipeline};
use crate::stft;
use std::ffi::c_ulong;

const CHUNK: usize = 1024;
const MAX_RAW_EXTRA: usize = 4800;
/// Bereich von „Min WNG (dB)“; die Mitte (−3 dB) ist der Standard. Delay-and-Sum hat +8,5 dB.
const MIN_WNG_LO: f32 = -12.0;
const MIN_WNG_HI: f32 = 6.0;
/// Bereich von „Gain (dB)“: negativ, wenn die Echounterdrückung davor schon verstärkt (Vorverstärkung +24 dB)
const GAIN_LO: f32 = -30.0;
const GAIN_HI: f32 = 60.0;

// Port-Indizes uma8_beam (Reihenfolge ist API: neue Controls nur hinten anfügen)
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
const LATE_REVERB: usize = 26;
const MIN_WNG: usize = 27;
const NULL1_AZIMUTH: usize = 28;
const NULL_WEIGHT: usize = 32;

pub struct BeamPlugin {
    pipeline: Pipeline,
    input: [Vec<f32>; CHANNELS],
    out_buf: Vec<f32>,
    raw_buf: Vec<f32>,
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
        PortSpec::control("Mode", 0.0, 2.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Center Channel", 0.0, 6.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Ring 0", 0.0, 6.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Ring 1", 0.0, 6.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Ring 2", 0.0, 6.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Ring 3", 0.0, 6.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Ring 4", 0.0, 6.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Ring 5", 0.0, 6.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Ring Offset (deg)", 0.0, 360.0, HINT_DEFAULT_0),
        PortSpec::control("Radius (mm)", 20.0, 60.0, HINT_DEFAULT_MIDDLE),
        PortSpec::control("Gain (dB)", GAIN_LO, GAIN_HI, HINT_DEFAULT_0),
        PortSpec::control("Dereverb", 0.0, 1.0, HINT_TOGGLED | HINT_DEFAULT_1),
        PortSpec::control("Dereverb Strength", 0.0, 1.0, HINT_DEFAULT_MIDDLE),
        PortSpec::control("Dereverb T60 (s)", 0.1, 1.5, HINT_DEFAULT_MIDDLE),
        PortSpec::control("Raw Extra Delay (samples)", 0.0, 4800.0, HINT_INTEGER | HINT_DEFAULT_0),
        PortSpec::control("Late Reverb", 0.0, 1.0, HINT_TOGGLED | HINT_DEFAULT_1),
        PortSpec::control("Min WNG (dB)", MIN_WNG_LO, MIN_WNG_HI, HINT_DEFAULT_MIDDLE),
        PortSpec::control("Null 1 Azimuth (deg)", 0.0, 360.0, HINT_DEFAULT_0),
        PortSpec::control("Null 1 Elevation (deg)", 0.0, 90.0, HINT_DEFAULT_0),
        PortSpec::control("Null 2 Azimuth (deg)", 0.0, 360.0, HINT_DEFAULT_0),
        PortSpec::control("Null 2 Elevation (deg)", 0.0, 90.0, HINT_DEFAULT_0),
        PortSpec::control("Null Weight (dB)", 0.0, NULL_WEIGHT_MAX_DB, HINT_DEFAULT_0),
    ];

    fn new(sample_rate: f32) -> Self {
        let params = Params {
            geometry: Geometry::UMA8,
            steering: Steering {
                azimuth_deg: 0.0,
                elevation_deg: 0.0,
                mode: Mode::Superdirective,
                min_wng_db: -3.0,
                nulls: Nulls::OFF,
            },
            dereverb: true,
            strength: 0.6,
            late_reverb: true,
            t60: 0.5,
        };
        BeamPlugin {
            pipeline: Pipeline::new(sample_rate, &params),
            input: std::array::from_fn(|_| vec![0.0; CHUNK]),
            out_buf: vec![0.0; CHUNK],
            raw_buf: vec![0.0; CHUNK],
            raw_delay: vec![0.0; stft::LATENCY + MAX_RAW_EXTRA + 1],
            raw_pos: 0,
            gain: 1.0,
        }
    }

    fn run(&mut self, ports: &Ports, n: usize) {
        let channel = |i: usize| ports.control(i, 0.0).round().clamp(0.0, 6.0) as usize;
        let params = Params {
            geometry: Geometry {
                center: channel(CENTER),
                ring: std::array::from_fn(|k| channel(RING0 + k)),
                ring_offset_deg: ports.control(RING_OFFSET, 90.0),
                radius_m: ports.control(RADIUS, 43.0).clamp(20.0, 60.0) / 1000.0,
            },
            steering: Steering {
                azimuth_deg: ports.control(AZIMUTH, 0.0),
                elevation_deg: ports.control(ELEVATION, 0.0).clamp(0.0, 90.0),
                mode: Mode::from_control(ports.control(MODE, 0.0).clamp(0.0, 2.0)),
                min_wng_db: ports.control(MIN_WNG, -3.0).clamp(MIN_WNG_LO, MIN_WNG_HI),
                // „Null Weight (dB)“ 0 = aus; Azimut/Elevation je Nullstelle wie die Blickrichtung
                nulls: Nulls {
                    dirs: std::array::from_fn(|i| {
                        let az = NULL1_AZIMUTH + 2 * i;
                        [ports.control(az, 0.0), ports.control(az + 1, 0.0).clamp(0.0, 90.0)]
                    }),
                    weight_db: ports.control(NULL_WEIGHT, 0.0).clamp(0.0, NULL_WEIGHT_MAX_DB),
                },
            },
            dereverb: ports.control(DEREVERB, 1.0) >= 0.5,
            strength: ports.control(DR_STRENGTH, 0.6),
            late_reverb: ports.control(LATE_REVERB, 1.0) >= 0.5,
            t60: ports.control(DR_T60, 0.5),
        };
        self.pipeline.set_params(&params);
        let gain_target = 10f32.powf(ports.control(GAIN, 0.0).clamp(GAIN_LO, GAIN_HI) / 20.0);
        let raw_extra = (ports.control(RAW_EXTRA, 0.0).round().max(0.0) as usize).min(MAX_RAW_EXTRA);
        let raw_total = stft::LATENCY + raw_extra;
        let dlen = self.raw_delay.len();

        let mut off = 0;
        while off < n {
            let m = (n - off).min(CHUNK);
            for c in 0..CHANNELS {
                ports.read(IN0 + c, off, &mut self.input[c][..m]);
            }
            let refs: [&[f32]; CHANNELS] = std::array::from_fn(|c| &self.input[c][..m]);
            self.pipeline.process(&refs, &mut self.out_buf[..m]);
            let center = self.pipeline.center();
            for i in 0..m {
                self.gain += (gain_target - self.gain) * 0.001;
                let x = self.input[center][i];
                self.raw_delay[self.raw_pos] = if x.is_finite() { x } else { 0.0 };
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
    vec![descriptor::<BeamPlugin>(), descriptor::<LimitPlugin>()]
}
