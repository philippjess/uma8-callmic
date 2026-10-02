#!/usr/bin/env bash
# Baut die Arch-Pakete in Podman (archlinux:latest) nach packaging/out/arch/: ./packaging/build-arch.sh [uma8-callmic] [deepfilternet-ladspa]
#
# Ohne Argument entstehen beide Pakete. Auf dem Host braucht es nur podman, git und tar. Die Quellen (versionierte
# und neue, nicht ignorierte Dateien des Arbeitsbaums) gehen per tar über stdin in den Container; eingebunden wird nur
# packaging/out/arch/ (mit :Z, kein Repo und kein $HOME). pacman- und Cargo-Cache liegen in den Podman-Volumes
# uma8-arch-pacman, uma8-arch-cargo und uma8-arch-src. makepkg läuft als unprivilegierter Benutzer „builder“.
# Ergebnis: packaging/out/arch/*.pkg.tar.zst (Installieren: sudo pacman -U packaging/out/arch/*.pkg.tar.zst),
# Logs und namcap-Ausgabe in packaging/out/arch/logs/.
set -euo pipefail

ALL_PACKAGES=(deepfilternet-ladspa uma8-callmic)   # Reihenfolge: das Plugin zuerst
IMAGE="${UMA8_ARCH_IMAGE:-docker.io/library/archlinux:latest}"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf 'Fehler: %s\n' "$*" >&2; exit 1; }

# --- Host -----------------------------------------------------------------------------------------

host_main() {
    local here repo out p
    local -a pkgs=()
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    repo="$(cd "$here/.." && pwd)"
    out="$here/out/arch"
    for p in "$@"; do
        case "$p" in
            -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
            uma8-callmic|deepfilternet-ladspa) pkgs+=("$p") ;;
            *) fail "unbekanntes Paket '$p' (möglich: ${ALL_PACKAGES[*]})" ;;
        esac
    done
    [ ${#pkgs[@]} -gt 0 ] || pkgs=("${ALL_PACKAGES[@]}")
    command -v podman >/dev/null || fail "podman fehlt (sudo dnf install podman)"
    mkdir -p "$out/logs"

    say "Baue ${pkgs[*]} in $IMAGE"
    stream_sources "$repo" | podman run --rm -i \
        -v uma8-arch-pacman:/var/cache/pacman/pkg \
        -v uma8-arch-cargo:/home/builder/.cargo \
        -v uma8-arch-src:/var/cache/makepkg-src \
        -v "$out:/out:Z" \
        "$IMAGE" bash -c 'set -euo pipefail; mkdir -p /home/repo; tar -xf - -C /home/repo
                          exec bash /home/repo/packaging/build-arch.sh --in-container "$@"' \
        bash "${pkgs[@]}"

    say "Fertig:"
    for p in "${pkgs[@]}"; do
        find "$out" -maxdepth 1 -name "$p-[0-9]*.pkg.tar.zst" -printf '  %p (%s Bytes)\n' | sort
    done
    echo "Installieren (im Repo-Verzeichnis): sudo pacman -U packaging/out/arch/*.pkg.tar.zst"
}

# Versionierte und neue, nicht ignorierte Dateien des Arbeitsbaums als tar
stream_sources() {
    local repo=$1
    cd "$repo"
    git ls-files -z --cached --others --exclude-standard --deduplicate \
        | while IFS= read -r -d '' f; do if [ -e "$f" ] || [ -L "$f" ]; then printf '%s\0' "$f"; fi; done \
        | tar --null -T - -cf -
}

# --- Container ------------------------------------------------------------------------------------

container_main() {
    local p log=/out/logs/pacman.log dep
    local -a deps
    say "Werkzeuge installieren (pacman -Syu)"
    pacman -Syu --noconfirm --needed base-devel git >"$log" 2>&1 || { tail -n 40 "$log"; fail "pacman -Syu fehlgeschlagen"; }
    pacman -S --noconfirm --needed namcap >>"$log" 2>&1 || { tail -n 40 "$log"; fail "namcap-Installation fehlgeschlagen"; }
    id builder >/dev/null 2>&1 || useradd -M -d /home/builder builder
    mkdir -p /home/builder/pkgs /home/builder/.cargo /var/cache/makepkg-src
    chown -R builder: /home/builder /home/repo /var/cache/makepkg-src

    for p in "$@"; do
        local dir="/home/repo/packaging/arch/$p"
        say "$p: Abhängigkeiten"
        # Abhängigkeiten aus dem PKGBUILD als Root installieren (makepkg -s bräuchte sudo); virtuelle Namen,
        # die erst das Schwesterpaket liefert (libdeep_filter_ladspa), sind im Repo nicht auflösbar
        mapfile -t deps < <(bash -c 'source "$1"; printf "%s\n" "${depends[@]}" "${makedepends[@]}" "${checkdepends[@]}"' _ "$dir/PKGBUILD" \
                            | sed 's/[<>=].*//' | grep -vx 'libdeep_filter_ladspa')
        pacman -S --noconfirm --needed --asdeps "${deps[@]}" >>"$log" 2>&1 || { tail -n 40 "$log"; fail "Abhängigkeiten von $p"; }

        say "$p: makepkg (Log: packaging/out/arch/logs/$p-build.log)"
        # PATH wie auf Arch üblich: Das Image setzt /usr/sbin vor /usr/bin (dort ein Symlink), Python-Installer
        # schrieben sonst #!/usr/sbin/python in die Startskripte
        su builder -c "export PATH=/usr/local/sbin:/usr/local/bin:/usr/bin; cd '$dir' && PKGDEST=/home/builder/pkgs \
                       SRCDEST=/var/cache/makepkg-src BUILDDIR=/home/builder/build \
                       makepkg -f --noconfirm --nodeps --cleanbuild" > "/out/logs/$p-build.log" 2>&1 \
            || { tail -n 60 "/out/logs/$p-build.log"; fail "makepkg für $p fehlgeschlagen"; }

        say "$p: namcap"
        { namcap "$dir/PKGBUILD"; namcap /home/builder/pkgs/"$p"-[0-9]*.pkg.tar.zst; } 2>&1 | tee "/out/logs/$p-namcap.txt" || true

        # Ältere Stände ersetzen, damit „pacman -U packaging/out/arch/*.pkg.tar.zst“ eindeutig bleibt
        rm -f /out/"$p"-[0-9]*.pkg.tar.zst
        cp /home/builder/pkgs/"$p"-[0-9]*.pkg.tar.zst /out/
        # Das Schwesterpaket (Plugin) muss für spätere Pakete nicht installiert sein: makepkg läuft mit --nodeps
    done
}

if [ "${1:-}" = "--in-container" ]; then
    shift
    container_main "$@"
else
    host_main "$@"
fi
