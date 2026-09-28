//! Die LADSPA-Plugins der Bibliothek.

use crate::beam::{Beamformer, Geometry, Steering, CHANNELS};
use crate::dereverb::{self, Dereverb};
use crate::ladspa::*;
use crate::limiter::Limiter;
use std::ffi::c_ulong;

const CHUNK: usize = 1024;
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
