//! LADSPA-Plugins für das miniDSP UMA-8: Beamforming, Hallunterdrückung, Begrenzer.

pub mod beam;
pub mod cdr;
pub mod dereverb;
pub mod jacobi;
pub mod ladspa;
pub mod limiter;
pub mod pipeline;
pub mod stft;
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
