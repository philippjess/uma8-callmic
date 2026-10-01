#!/usr/bin/env bash
# Baut die RPMs in Podman (Fedora 44) nach packaging/out/: ./packaging/build-rpms.sh [uma8-callmic] [deepfilternet-ladspa]
#
# Ohne Argument entstehen beide Pakete. Auf dem Host braucht es nur podman, git und python3: Werkzeuge,
# Rust und Build-Abhängigkeiten kommen aus Fedora selbst, im Container. Die Quellen (versionierte und neue,
# nicht ignorierte Dateien des Arbeitsbaums) gehen per tar über stdin hinein; eingebunden wird nur
# packaging/out/ (mit :Z). Downloads (DeepFilterNet-Tarball, vendorte Crates) landen in packaging/out/cache/,
# dnf- und Cargo-Caches in den Podman-Volumes uma8-rpm-dnf und uma8-rpm-cargo.
# Ergebnis: packaging/out/*.rpm (Installieren: sudo dnf install packaging/out/*.x86_64.rpm),
# debuginfo in packaging/out/debug/, Logs und rpmlint-Ausgabe in packaging/out/logs/.
set -euo pipefail

ALL_PACKAGES=(deepfilternet-ladspa uma8-callmic)
IMAGE="${UMA8_RPM_IMAGE:-registry.fedoraproject.org/fedora:44}"
# Vendorte Crates, die unter Linux nie gebaut werden (634 von 892 MB bei DeepFilterNet 0.5.6): Cargo braucht
# von ihnen nur das Manifest. cargo vendor-filterer wäre der Standardweg, ersetzt aber bei doppelten
# Crate-Versionen auch benötigte Crates (vec_map 0.7.0) durch Stubs.
PRUNE_CRATES=('windows*' 'winapi-*-pc-windows-gnu')

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf 'Fehler: %s\n' "$*" >&2; exit 1; }
# dnf im Container, Ausgabe ins Log; nur bei Fehlern sichtbar
dnf_quiet() {
    dnf -y --setopt=keepcache=True --setopt=install_weak_deps=False "$@" >> /out/logs/dnf.log 2>&1 \
        || { tail -n 40 /out/logs/dnf.log; fail "dnf $1 fehlgeschlagen"; }
}

# --- Host -----------------------------------------------------------------------------------------

host_main() {
    local here repo out version commit snapinfo p
    local -a pkgs=()
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    repo="$(cd "$here/.." && pwd)"
    out="$here/out"
    for p in "$@"; do
        case "$p" in
            -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
            uma8-callmic|deepfilternet-ladspa) pkgs+=("$p") ;;
            *) fail "unbekanntes Paket '$p' (möglich: ${ALL_PACKAGES[*]})" ;;
        esac
    done
    [ ${#pkgs[@]} -gt 0 ] || pkgs=("${ALL_PACKAGES[@]}")
    command -v podman >/dev/null || fail "podman fehlt (sudo dnf install podman)"

    version="$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' \
               "$repo/pyproject.toml")"
    commit="$(git -C "$repo" rev-parse --short=7 HEAD)"
    # Release-Kennung steigt mit jedem Bau: Commit-Zeit, bei ungesicherten Änderungen die Bauzeit
    if [ -n "$(git -C "$repo" status --porcelain)" ]; then
        snapinfo="$(date -u +%Y%m%d%H%M%S)git${commit}.dirty"
    else
        snapinfo="$(TZ=UTC git -C "$repo" log -1 --format=%cd --date=format-local:%Y%m%d%H%M%S)git${commit}"
    fi
    mkdir -p "$out/cache" "$out/debug" "$out/logs"

    say "Baue ${pkgs[*]} in $IMAGE (uma8-callmic $version-1.$snapinfo)"
    stream_sources "$repo" "uma8-callmic-$version" | podman run --rm -i \
        -v uma8-rpm-dnf:/var/cache/libdnf5 \
        -v uma8-rpm-cargo:/root/.cargo/registry \
        -v "$out:/out:Z" \
        -e PKG_VERSION="$version" -e SNAPINFO="$snapinfo" \
        "$IMAGE" bash -c 'set -euo pipefail; mkdir -p /build; tar -xf - -C /build
                          exec bash "/build/uma8-callmic-$PKG_VERSION/packaging/build-rpms.sh" --in-container "$@"' \
        bash "${pkgs[@]}"

    say "Fertig:"
    for p in "${pkgs[@]}"; do
        find "$out" -maxdepth 1 -name "$p-[0-9]*.rpm" -printf '  %p (%s Bytes)\n' | sort
    done
    echo "Installieren (im Repo-Verzeichnis): sudo dnf install packaging/out/*.x86_64.rpm"
}

# Versionierte und neue, nicht ignorierte Dateien des Arbeitsbaums als tar mit Präfix-Verzeichnis
stream_sources() {
    local repo=$1 prefix=$2
    cd "$repo"
    git ls-files -z --cached --others --exclude-standard --deduplicate \
        | while IFS= read -r -d '' f; do if [ -e "$f" ] || [ -L "$f" ]; then printf '%s\0' "$f"; fi; done \
        | tar --null -T - --transform "flags=r;s|^|$prefix/|" -cf -
}

# --- Container ------------------------------------------------------------------------------------

TOP=/root/rpmbuild

container_main() {
    : "${PKG_VERSION:?}" "${SNAPINFO:?}"
    SRC="/build/uma8-callmic-$PKG_VERSION"
    say "Werkzeuge installieren"
    : > /out/logs/dnf.log
    dnf_quiet install \
        rpm-build rpmdevtools rpmlint glibc-langpack-en dnf5-plugins cargo curl tar xz patch findutils
    rpmdev-setuptree
    local p
    for p in "$@"; do
        case "$p" in
            deepfilternet-ladspa) prepare_dfn ;;
            uma8-callmic) prepare_uma8 ;;
        esac
        build_rpms "$p"
    done
}

prepare_uma8() {
    local name=uma8-callmic v=$PKG_VERSION
    say "$name: Quellen und vendorte Crates"
    tar -C /build -czf "$TOP/SOURCES/$name-$v.tar.gz" "$name-$v"
    rm -rf /tmp/uma8-vendor && mkdir -p /tmp/uma8-vendor
    (cd "$SRC/plugin" && cargo vendor --locked --quiet /tmp/uma8-vendor/vendor >/dev/null)
    tar -C /tmp/uma8-vendor -c vendor | xz -T0 > "$TOP/SOURCES/$name-$v-vendor.tar.xz"
    # Version und Snapshot fest in die Spec schreiben, damit das SRPM für sich allein baubar bleibt
    { echo "%global pkg_version $v"; echo "%global snapinfo $SNAPINFO"; cat "$SRC/packaging/$name.spec"; } \
        > "$TOP/SPECS/$name.spec"
}

prepare_dfn() {
    local name=deepfilternet-ladspa spec="$SRC/packaging/deepfilternet-ladspa.spec" v tarball vendor key patch
    local cache=/out/cache
    v="$(rpmspec -q --srpm --qf '%{version}\n' "$spec")"
    tarball="DeepFilterNet-$v.tar.gz"
    vendor="DeepFilterNet-$v-vendor.tar.xz"

    if ! (cd "$cache" && sha256sum --quiet -c "$SRC/packaging/$name.sources" >/dev/null 2>&1); then
        say "$name: DeepFilterNet $v herunterladen"
        curl -fsSL --retry 3 -o "$cache/$tarball.part" \
            "https://github.com/Rikorose/DeepFilterNet/archive/refs/tags/v$v.tar.gz"
        mv "$cache/$tarball.part" "$cache/$tarball"
        (cd "$cache" && sha256sum --quiet -c "$SRC/packaging/$name.sources") \
            || fail "Prüfsumme von $tarball passt nicht zu packaging/$name.sources: $(sha256sum "$cache/$tarball")"
    fi

    # Patches wie %autosetup anwenden; die vendorten Crates hängen nur an Manifesten und Lockfile
    rm -rf /tmp/dfn && mkdir -p /tmp/dfn
    tar -xzf "$cache/$tarball" -C /tmp/dfn --strip-components=1
    for patch in $(rpmspec -P "$spec" | awk '/^Patch[0-9]*:/ {print $2}'); do
        patch -d /tmp/dfn -p1 -s --fuzz=0 < "$SRC/packaging/$patch"
        cp "$SRC/packaging/$patch" "$TOP/SOURCES/"
    done
    key="$( { cd /tmp/dfn && find . \( -name Cargo.toml -o -name Cargo.lock \) -print0 | LC_ALL=C sort -z \
              | xargs -0 sha256sum; echo "${PRUNE_CRATES[*]}"; cargo --version; } | sha256sum)"
    if [ -f "$cache/$vendor" ] && [ "$(cat "$cache/$vendor.key" 2>/dev/null)" = "$key" ]; then
        say "$name: vendorte Crates aus dem Cache"
    else
        say "$name: Crates vendorn"
        cp /tmp/dfn/Cargo.lock /tmp/dfn-Cargo.lock
        (cd /tmp/dfn && cargo vendor --locked vendor > "/out/logs/$name-vendor.log" 2>&1) \
            || { tail -n 40 "/out/logs/$name-vendor.log"; fail "cargo vendor für $name fehlgeschlagen"; }
        cmp -s /tmp/dfn/Cargo.lock /tmp/dfn-Cargo.lock \
            || fail "cargo vendor hat Cargo.lock verändert – der Lockfile-Patch passt nicht mehr"
        prune_vendor /tmp/dfn/vendor
        tar -C /tmp/dfn -c vendor | xz -T0 > "$cache/$vendor.part"
        mv "$cache/$vendor.part" "$cache/$vendor"
        echo "$key" > "$cache/$vendor.key"
    fi
    cp "$cache/$tarball" "$cache/$vendor" "$TOP/SOURCES/"
    cp "$spec" "$TOP/SPECS/"
}

# Crates aus PRUNE_CRATES auf Manifest, build.rs und leere src/lib.rs kürzen; die Prüfsummenliste wird
# geleert, die Paket-Prüfsumme (gegen Cargo.lock) bleibt
prune_vendor() {
    local vendor=$1 pattern d sum
    for pattern in "${PRUNE_CRATES[@]}"; do
        for d in "$vendor"/$pattern; do
            [ -d "$d" ] || continue
            sum="$(sed -n 's/.*"package":"\([0-9a-f]*\)".*/\1/p' "$d/.cargo-checksum.json")"
            [ -n "$sum" ] || fail "keine Paket-Prüfsumme in $d/.cargo-checksum.json"
            find "$d" -mindepth 1 -maxdepth 1 ! -name Cargo.toml ! -name build.rs -exec rm -rf {} +
            mkdir "$d/src" && : > "$d/src/lib.rs"
            printf '{"files":{},"package":"%s"}' "$sum" > "$d/.cargo-checksum.json"
        done
    done
}

build_rpms() {
    local name=$1 spec="$TOP/SPECS/$1.spec" rc round f
    say "$name: Build-Abhängigkeiten"
    dnf_quiet builddep "$spec"
    if grep -q '^%generate_buildrequires' "$spec"; then
        # Dynamische Build-Abhängigkeiten (pyproject) nachinstallieren, bis rpmbuild nichts mehr vermisst
        for round in 1 2 3 4 5; do
            rc=0
            rpmbuild -br "$spec" > "/out/logs/$name-buildreqs.log" 2>&1 || rc=$?
            [ "$rc" -eq 0 ] && break
            [ "$rc" -eq 11 ] || { cat "/out/logs/$name-buildreqs.log"; fail "rpmbuild -br für $name: Exit $rc"; }
            [ "$round" -lt 5 ] || fail "Build-Abhängigkeiten von $name nach 5 Runden nicht vollständig"
            dnf_quiet builddep "$TOP"/SRPMS/"$name"-*.buildreqs.nosrc.rpm
            rm -f "$TOP"/SRPMS/"$name"-*.buildreqs.nosrc.rpm
        done
    fi

    say "$name: rpmbuild (Log: packaging/out/logs/$name-build.log)"
    rpmbuild -ba "$spec" > "/out/logs/$name-build.log" 2>&1 \
        || { tail -n 60 "/out/logs/$name-build.log"; fail "rpmbuild für $name fehlgeschlagen"; }

    say "$name: rpmlint"
    rpmlint -c "$SRC/packaging/rpmlint.toml" "$spec" "$TOP"/SRPMS/"$name"-[0-9]*.src.rpm "$TOP"/RPMS/*/"$name"-*.rpm \
        2>&1 | tee "/out/logs/$name-rpmlint.txt" || true

    # Ältere Stände dieses Pakets ersetzen, damit „dnf install packaging/out/*.x86_64.rpm“ eindeutig bleibt
    find /out /out/debug -maxdepth 1 \( -name "$name-[0-9]*.rpm" -o -name "$name-debug*.rpm" \) -delete
    cp "$TOP"/SRPMS/"$name"-[0-9]*.src.rpm /out/
    for f in "$TOP"/RPMS/*/"$name"-*.rpm; do
        case "$f" in
            *-debuginfo-*|*-debugsource-*) cp "$f" /out/debug/ ;;
            *) cp "$f" /out/ ;;
        esac
    done
}

if [ "${1:-}" = "--in-container" ]; then
    shift
    container_main "$@"
else
    host_main "$@"
fi
