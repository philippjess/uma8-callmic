# uma8-callmic: Call-Mikrofon für das miniDSP UMA-8 – PipeWire-Filterkette, LADSPA-Plugin und Tray.
# Gebaut wird mit packaging/build-rpms.sh (Podman, Fedora 44). Das Skript setzt die Version aus
# pyproject.toml und die Snapshot-Kennung aus git; von Hand z. B.:
#   rpmbuild -ba --define 'pkg_version 0.1.0' --define 'snapinfo 20261001120000git1234abc' uma8-callmic.spec
%{!?pkg_version:%{error:pkg_version fehlt: mit packaging/build-rpms.sh bauen oder --define 'pkg_version X.Y.Z' angeben}}

# Fedoras python3-pyside6 hat keine python3dist(pyside6)-Metadaten: die aus pyproject.toml erzeugte
# Abhängigkeit wäre unerfüllbar, stattdessen unten das Paket direkt
%global __requires_exclude ^python3(\\.[0-9]+)?dist\\(pyside6\\)

Name:           uma8-callmic
Version:        %{pkg_version}
Release:        1%{?snapinfo:.%{snapinfo}}%{?dist}
Summary:        Call-Mikrofon für das miniDSP UMA-8 unter PipeWire

# Das Projekt hat noch keine Lizenz festgelegt (Platzhalter). Statisch gelinkte Crates des Plugins:
# MIT bzw. MIT OR Apache-2.0, aufgeschlüsselt in LICENSE.dependencies.
License:        LicenseRef-Unknown AND MIT AND (MIT OR Apache-2.0)
Source0:        %{name}-%{version}.tar.gz
# Vendorte Crates für plugin/ (cargo vendor --locked, erzeugt von packaging/build-rpms.sh)
Source1:        %{name}-%{version}-vendor.tar.xz

ExclusiveArch:  %{rust_arches}

BuildRequires:  cargo-rpm-macros >= 26
BuildRequires:  python3-devel
BuildRequires:  systemd-rpm-macros
BuildRequires:  desktop-file-utils
# %%check: Unit-Tests (Dialoge laufen mit Qt offscreen, Plugin-Tests bauen plugin/ mit cargo)
BuildRequires:  python3-pytest
BuildRequires:  python3-numpy
BuildRequires:  python3-pyside6
BuildRequires:  python3-scipy

# context.exec mit Argumenten als Array (Echo-Referenz-Helfer): erst ab PipeWire 1.2.0, 1.0 zerlegte den Text
Requires:       pipewire >= 1.2.0
# pw-cli, pw-dump, pw-record
Requires:       pipewire-utils
# pactl
Requires:       pulseaudio-utils
# Echo-Cancel-Modul mit WebRTC-AEC für die AEC-Stufe der Kette (Fedora: pipewire-libs)
%if 0%{?__isa_bits} == 64
Requires:       libpipewire-module-echo-cancel.so()(64bit)
Requires:       libspa-aec-webrtc.so()(64bit)
%else
Requires:       libpipewire-module-echo-cancel.so
Requires:       libspa-aec-webrtc.so
%endif
Requires:       python3-pyside6
Requires:       deepfilternet-ladspa
Requires:       hicolor-icon-theme

%description
Macht aus dem miniDSP UMA-8 (Raw-Firmware) ein Telefonie-Mikrofon:
Beamforming über 7 Mikrofone, optionale Hallunterdrückung,
DeepFilterNet-Rauschunterdrückung und ein Begrenzer als PipeWire-Filterkette.
Die Kette läuft im Benutzerdienst uma8-callmic-chain.service, gesteuert von
einem KDE-Tray-Programm.

Beim ersten Start richtet das Tray den Dienst und den Autostart für den
jeweiligen Benutzer ein.

%prep
%autosetup -n %{name}-%{version}
cd plugin
tar -xJf %{SOURCE1}
%cargo_prep -v vendor

%generate_buildrequires
%pyproject_buildrequires -R

%build
cd plugin
%cargo_build
%{cargo_license_summary}
%{cargo_license} > LICENSE.dependencies
%cargo_vendor_manifest
cd ..
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -L uma8_callmic
install -Dpm0755 plugin/target/rpm/libuma8_beam.so %{buildroot}%{_libdir}/ladspa/libuma8_beam.so
install -Dpm0644 pipewire/uma8-callmic-chain.service %{buildroot}%{_userunitdir}/uma8-callmic-chain.service
sed -i 's|@BIN@|%{_bindir}/uma8-callmic|' %{buildroot}%{_userunitdir}/uma8-callmic-chain.service
install -Dpm0644 uma8_callmic/data/uma8-callmic.desktop %{buildroot}%{_datadir}/applications/uma8-callmic.desktop
sed -i 's|^Exec=@BIN@$|Exec=uma8-callmic|' %{buildroot}%{_datadir}/applications/uma8-callmic.desktop
install -Dpm0644 uma8_callmic/icons/active.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/uma8-callmic.svg

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/uma8-callmic.desktop
grep -qx 'ExecStartPre=%{_bindir}/uma8-callmic --write-config' %{buildroot}%{_userunitdir}/uma8-callmic-chain.service
cd plugin
%cargo_test
cd ..
# Einige Tests bauen plugin/ selbst: vendort und offline wie in %%prep eingerichtet
export CARGO_HOME="$PWD/plugin/.cargo" QT_QPA_PLATFORM=offscreen
# Ohne die Rechenzeit-Tests (Marker timing): Bauhosts können beliebig langsam oder ausgelastet sein
%pytest -m 'not integration and not hardware and not timing'
# Installiertes Paket außerhalb des Quellbaums: Einstiegspunkt und Vorlage aus dem Python-Paket
testhome="$(mktemp -d)"
(cd "$testhome" && HOME="$testhome" %{py3_test_envvars} %{buildroot}%{_bindir}/uma8-callmic --write-config)
grep -q 'label = deep_filter_mono' "$testhome/.config/pipewire/uma8-callmic.conf"
# Echo-Referenz-Helfer startet mit der Kette (context.exec), WirePlumber verbindet die Referenz nicht
grep -q 'args = \[ "--ref-linker" \]' "$testhome/.config/pipewire/uma8-callmic.conf"
grep -q 'node.autoconnect = false' "$testhome/.config/pipewire/uma8-callmic.conf"
rm -rf "$testhome"

%post
%systemd_user_post uma8-callmic-chain.service

%preun
%systemd_user_preun uma8-callmic-chain.service

%postun
%systemd_user_postun uma8-callmic-chain.service

%files -f %{pyproject_files}
%license plugin/LICENSE.dependencies plugin/cargo-vendor.txt
%doc README.md
%{_bindir}/uma8-callmic
%dir %{_libdir}/ladspa
%{_libdir}/ladspa/libuma8_beam.so
%{_userunitdir}/uma8-callmic-chain.service
%{_datadir}/applications/uma8-callmic.desktop
%{_datadir}/icons/hicolor/scalable/apps/uma8-callmic.svg

%changelog
* Thu Oct 01 2026 Philipp <philipp@rootshell.dev> - 0.1.0-1
- Erstes RPM: Plugin, Tray, Benutzerdienst, Startmenü-Eintrag
