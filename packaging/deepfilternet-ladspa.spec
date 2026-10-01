# DeepFilterNet (Rauschunterdrückung per neuronalem Netz) als LADSPA-Plugin für PipeWire-Filterketten.
# Gebaut mit packaging/build-rpms.sh (Podman, Fedora 44): offline aus vendorten Crates (Source1).
# Das Modell steckt per include_bytes! im Plugin (~55 MB); kein debuginfo-Paket, das Plugin wird gestrippt.
%global debug_package %{nil}
%global rustflags_debuginfo 0

Name:           deepfilternet-ladspa
Version:        0.5.6
Release:        1%{?dist}
Summary:        DeepFilterNet-Rauschunterdrückung als LADSPA-Plugin

# DeepFilterNet (Code und mitgelieferte Modelle): MIT OR Apache-2.0. Dazu die statisch gelinkten Crates,
# zusammengefasst aus LICENSE.dependencies (siehe %%build), und das von jemalloc-sys gebaute jemalloc
# (BSD-2-Clause).
License:        (MIT OR Apache-2.0) AND (0BSD OR MIT OR Apache-2.0) AND Apache-2.0 AND (Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT) AND BSD-2-Clause AND MIT AND (MIT OR Zlib OR Apache-2.0) AND Unlicense AND (Unlicense OR MIT)
URL:            https://github.com/Rikorose/DeepFilterNet
Source0:        %{url}/archive/v%{version}/DeepFilterNet-%{version}.tar.gz
# cargo vendor --locked nach Patch0, windows*-Crates auf Manifeste gekürzt (packaging/build-rpms.sh)
Source1:        DeepFilterNet-%{version}-vendor.tar.xz
# Lockfile: time 0.3.28 baut mit aktuellem Rust nicht (E0282) – cargo update -p time --precise 0.3.36
Patch0:         deepfilternet-0.5.6-time-0.3.36.patch
# Jede Instanz startete einen nie endenden, alle 2 ms pollenden Worker-Thread; jetzt Stop-Flag + join
Patch1:         deepfilternet-0.5.6-ladspa-worker-thread.patch

ExclusiveArch:  %{rust_arches}
# jemalloc-sys 0.5.4 baut jemalloc 5.3.0 aus mitgelieferten Quellen
Provides:       bundled(jemalloc) = 5.3.0

BuildRequires:  cargo-rpm-macros >= 26
# jemalloc-sys, tract-linalg (C/Assembler)
BuildRequires:  gcc
BuildRequires:  make

%description
DeepFilterNet unterdrückt Störgeräusche in Sprache mit einem neuronalen Netz
in Echtzeit (48 kHz, 20 ms Latenz). Dieses Paket enthält das LADSPA-Plugin mit
den Labels deep_filter_mono und deep_filter_stereo, z. B. für
PipeWire-Filterketten. Das Low-Latency-Modell DeepFilterNet3 ist eingebaut.

Gegenüber Upstream 0.5.6 beendet jede Plugin-Instanz beim Aufräumen ihren
Worker-Thread, statt ihn weiterlaufen zu lassen.

%prep
%autosetup -n DeepFilterNet-%{version} -p1
tar -xJf %{SOURCE1}
%cargo_prep -v vendor
# Git-Abhängigkeit des Workspaces (hdf5-rust für pyDF-data) kommt ebenfalls aus vendor/
cat >> .cargo/config.toml << 'EOF'
[source."git+https://github.com/aldanor/hdf5-rust.git?rev=26046fb"]
git = "https://github.com/aldanor/hdf5-rust.git"
rev = "26046fb"
replace-with = "vendored-sources"
EOF

%build
%cargo_build -- -p deep-filter-ladspa
# Ins Plugin gelinkte Crates: Lizenzen und Liste für die bundled(crate(…))-Provides. Wie %%cargo_license und
# %%cargo_vendor_manifest, aber nur für dieses Workspace-Mitglied – sonst stünden auch GUI-, Python- und
# Windows-Crates der anderen Mitglieder drin („cargo2rpm … -p“ ist in Fedora 44 kaputt).
%global dep_tree %{__cargo} tree -Z avoid-dev-deps -p deep-filter-ladspa --offline --edges no-build,no-dev,no-proc-macro --target "$(rustc -vV | sed -n 's/^host: //p')" --prefix none
%{dep_tree} --format '{l}' | sed 's/ (\*)$//' | sort -u
%{dep_tree} --format '{l}: {p}' | sed 's/ (.*)$//' | sort -u > LICENSE.dependencies
%{dep_tree} --format '{p}' | grep -v ' (/' | sed 's/ (.*)$//' | sort -u > cargo-vendor.txt

%install
install -Dpm0755 target/rpm/libdeep_filter_ladspa.so %{buildroot}%{_libdir}/ladspa/libdeep_filter_ladspa.so
%{__strip} --strip-unneeded %{buildroot}%{_libdir}/ladspa/libdeep_filter_ladspa.so

%check
# u. a. Patch1: Instanzen anlegen und aufräumen darf keine Threads zurücklassen
%cargo_test -- -p deep-filter-ladspa

%files
%license LICENSE LICENSE-MIT LICENSE-APACHE LICENSE.dependencies cargo-vendor.txt
%doc ladspa/README.md ladspa/filter-chain-configs
%dir %{_libdir}/ladspa
%{_libdir}/ladspa/libdeep_filter_ladspa.so

%changelog
* Thu Oct 01 2026 Philipp <philipp@rootshell.dev> - 0.5.6-1
- Erstes Paket: LADSPA-Plugin aus DeepFilterNet 0.5.6, offline aus vendorten Crates
- Lockfile: time 0.3.36, damit es mit aktuellem Rust baut
- Worker-Thread je Instanz wird beim Aufräumen beendet (Thread-Leck behoben)
