#!/usr/bin/env bash
set -Eeuo pipefail

readonly PROJECT_NAME="qbit-rar-cleaner"
readonly INSTALL_DIR="/opt/${PROJECT_NAME}"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly RAR_COMMAND_PATH="/usr/local/bin/qbit-rar-cleaner"
readonly ISO_COMMAND_PATH="/usr/local/bin/qbit-iso-cleaner"
# Valódi, egyrészes RAR4 próbaarchívum egyetlen 20 bájtos szövegfájllal.
# Csak az UnRAR olvasási képességének ellenőrzésére szolgál; nem bontjuk ki.
readonly RAR_PROBE_BASE64="UmFyIRoHAM+QcwAADQAAAAAAAACEUnQgkDIAFAAAABQAAAADQqLIvrd22j4UMAgApIEAAHRlc3QudHh0gAi3dto+t3baPnRlc3QgdGV4dCBkb2N1bWVudA0KxD17AEAHAA=="

rar_probe=""
temp_config=""
temp_rar_wrapper=""
temp_iso_wrapper=""

cleanup_installer_temps() {
  rm -f -- \
    "${rar_probe:-}" \
    "${temp_config:-}" \
    "${temp_rar_wrapper:-}" \
    "${temp_iso_wrapper:-}"
}

trap cleanup_installer_temps EXIT

if [[ ${EUID} -ne 0 ]]; then
  echo "A telepítőt rootként indítsd: sudo bash install.sh" >&2
  exit 1
fi

prompt_default() {
  local prompt=$1
  local default=$2
  local value
  read -r -p "${prompt} [${default}]: " value
  printf '%s' "${value:-$default}"
}

default_user="${SUDO_USER:-accofil}"
if [[ "$default_user" == "root" ]]; then
  default_user="accofil"
fi

service_user=$(prompt_default "A qBittorrent Linux-felhasználója" "$default_user")
if ! id "$service_user" >/dev/null 2>&1; then
  echo "Nincs ilyen Linux-felhasználó: ${service_user}" >&2
  exit 1
fi
service_group=$(id -gn "$service_user")
service_uid=$(id -u "$service_user")
service_gid=$(id -g "$service_user")
service_home=$(getent passwd "$service_user" | cut -d: -f6)
if [[ -z "$service_home" || ! -d "$service_home" ]]; then
  echo "A felhasználó home könyvtára nem található: ${service_user}" >&2
  exit 1
fi

scan_default="${service_home}/storage"
scan_root=$(prompt_default "Átvizsgálandó qBittorrent save path" "$scan_default")
scan_root=$(realpath -e -- "$scan_root")
if [[ ! -d "$scan_root" ]]; then
  echo "A save path nem könyvtár: ${scan_root}" >&2
  exit 1
fi
if ! runuser -u "$service_user" -- test -r "$scan_root" || \
   ! runuser -u "$service_user" -- test -w "$scan_root" || \
   ! runuser -u "$service_user" -- test -x "$scan_root"; then
  echo "${service_user} nem tudja teljesen olvasni és írni ezt a mappát: ${scan_root}" >&2
  exit 1
fi

qb_url=$(prompt_default "qBittorrent WebUI URL" "http://127.0.0.1:8080")
if [[ "$qb_url" =~ ^[[:digit:]]{1,5}$ ]]; then
  if ((10#$qb_url < 1 || 10#$qb_url > 65535)); then
    echo "Érvénytelen qBittorrent WebUI port: ${qb_url}" >&2
    exit 1
  fi
  qb_url="http://127.0.0.1:${qb_url}"
fi
qb_url="${qb_url%/}"
if [[ ! "$qb_url" =~ ^https?://[^[:space:]]+$ ]]; then
  echo "Érvénytelen qBittorrent WebUI URL: ${qb_url}" >&2
  echo "Példa: http://127.0.0.1:10987 (vagy csak: 10987)" >&2
  exit 1
fi
qb_username=$(prompt_default "qBittorrent WebUI felhasználónév" "$service_user")
read -r -s -p "qBittorrent WebUI jelszó: " qb_password
printf '\n'
if [[ -z "$qb_password" ]]; then
  echo "A qBittorrent jelszó nem lehet üres." >&2
  exit 1
fi

missing_packages=()
command -v python3 >/dev/null 2>&1 || missing_packages+=(python3)
python3 -m venv --help >/dev/null 2>&1 || missing_packages+=(python3-venv)
if ! command -v mount >/dev/null 2>&1 || \
   ! command -v umount >/dev/null 2>&1 || \
   ! command -v unshare >/dev/null 2>&1; then
  missing_packages+=(util-linux)
fi

rar_probe=$(mktemp)
if ! printf '%s' "$RAR_PROBE_BASE64" | base64 --decode >"$rar_probe"; then
  echo "A beépített RAR-próbaarchívum létrehozása nem sikerült." >&2
  exit 1
fi

find_rar_extractor() {
  local command_path
  command_path=$(command -v unrar || true)
  if [[ -n "$command_path" ]] && \
     LC_ALL=C LANG=C "$command_path" t -y -c- -p- "$rar_probe" \
       >/dev/null 2>&1; then
    printf '%s' "$command_path"
    return 0
  fi
  return 1
}

normalize_qb_url() {
  python3 - "$1" <<'PY'
import sys
from urllib.parse import urlsplit

value = sys.argv[1].strip()


def fail(reason):
    print(f"Érvénytelen qBittorrent WebUI URL: {value!r} ({reason})", file=sys.stderr)
    raise SystemExit(1)


if not value or any(character.isspace() for character in value):
    fail("üres vagy szóközt tartalmaz")
try:
    parsed = urlsplit(value)
    hostname = parsed.hostname
    port = parsed.port
except ValueError as exc:
    fail(str(exc))
if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
    fail("http:// vagy https:// címmel kell kezdődnie")
if parsed.username is not None or parsed.password is not None:
    fail("a felhasználónevet és jelszót ne írd az URL-be")
if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
    fail("csak a gépcímet és a portot tartalmazhatja")
if port is not None and not 1 <= port <= 65535:
    fail(f"érvénytelen port: {port}")

display_host = f"[{hostname}]" if ":" in hostname else hostname
normalized = f"{parsed.scheme.casefold()}://{display_host}"
if port is not None:
    normalized += f":{port}"
print(normalized)
PY
}

if ((${#missing_packages[@]})); then
  echo "Hiányzó csomagok telepítése: ${missing_packages[*]}"
  apt-get update
  if ! DEBIAN_FRONTEND=noninteractive apt-get install -y \
      ca-certificates "${missing_packages[@]}"; then
    echo "A program alapfüggőségeinek telepítése nem sikerült." >&2
    exit 1
  fi
fi

if ! qb_url=$(normalize_qb_url "$qb_url"); then
  echo "Példa: http://127.0.0.1:10987 (vagy a kérdésnél csak: 10987)" >&2
  exit 1
fi

unrar_command=$(find_rar_extractor || true)
if [[ -z "$unrar_command" ]]; then
  echo "UnRAR telepítése..."
  apt-get update
  if ! DEBIAN_FRONTEND=noninteractive apt-get install -y \
      ca-certificates unrar; then
    echo "FIGYELEM: az unrar telepítése nem sikerült." >&2
  fi
  hash -r
  unrar_command=$(find_rar_extractor || true)
fi

rar_available=true
if [[ -z "$unrar_command" ]]; then
  rar_available=false
  echo >&2
  echo "FIGYELEM: az unrar nem tudta megnyitni a valódi RAR-próbaarchívumot." >&2
  echo "A külön ISO-parancs ettől függetlenül települ és használható lesz." >&2
  echo "A RAR-parancs addig biztonságos hibaüzenettel leáll." >&2
  echo "Debian 12: engedélyezd a non-free komponenst, telepítsd az unrar csomagot," >&2
  echo "majd futtasd újra a telepítőt." >&2
fi
rm -f -- "$rar_probe"
rar_probe=""

mount_command=$(command -v mount || true)
umount_command=$(command -v umount || true)
unshare_command=$(command -v unshare || true)
if [[ -z "$mount_command" || -z "$umount_command" || -z "$unshare_command" ]]; then
  echo "Az ISO-módhoz szükséges mount, umount vagy unshare parancs hiányzik." >&2
  exit 1
fi

install -d -o root -g root -m 0755 "$INSTALL_DIR"
install -o root -g root -m 0755 \
  "${SCRIPT_DIR}/qbit_rar_cleaner.py" \
  "${INSTALL_DIR}/qbit_rar_cleaner.py"
install -o root -g root -m 0644 \
  "${SCRIPT_DIR}/requirements.txt" \
  "${INSTALL_DIR}/requirements.txt"

python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --disable-pip-version-check \
  -r "${INSTALL_DIR}/requirements.txt"

config_dir="${service_home}/.config/${PROJECT_NAME}"
config_file="${config_dir}/config.json"
install -d -o "$service_user" -g "$service_group" -m 0700 "$config_dir"
cache_root="${service_home}/.cache"
install -d -o "$service_user" -g "$service_group" -m 0700 "$cache_root"
work_root="${service_home}/.cache/qbit-rar-cleaner-work"
install -d -o root -g "$service_group" -m 1770 "$work_root"
lock_file="${work_root}/.operation.lock"
touch "$lock_file"
chown root:"$service_group" "$lock_file"
chmod 0660 "$lock_file"
temp_config=$(mktemp "${config_dir}/config.json.XXXXXX")

printf '%s\0' \
  "$qb_url" "$qb_username" "$qb_password" "$scan_root" "$work_root" \
  "$unrar_command" "$mount_command" "$umount_command" "$service_user" \
  "$service_uid" "$service_gid" | \
  python3 -c '
import json
import sys

target = sys.argv[1]
values = sys.stdin.buffer.read().split(b"\0")
if values and values[-1] == b"":
    values.pop()
if len(values) != 11:
    raise SystemExit("invalid installer input")
(
    qb_url,
    qb_username,
    qb_password,
    scan_root,
    work_root,
    unrar_command,
    mount_command,
    umount_command,
    service_user,
    service_uid,
    service_gid,
) = (
    value.decode("utf-8") for value in values
)
with open(target, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "qb_url": qb_url,
            "qb_username": qb_username,
            "qb_password": qb_password,
            "scan_root": scan_root,
            "work_root": work_root,
            "unrar": unrar_command,
            "seven_zip": "",
            "mount_command": mount_command,
            "umount_command": umount_command,
            "service_user": service_user,
            "service_uid": int(service_uid),
            "service_gid": int(service_gid),
            "protected_tracker_domains": ["aither.cc"],
            "min_free_bytes": 1073741824,
        },
        handle,
        ensure_ascii=False,
        indent=2,
    )
    handle.write("\n")
' "$temp_config"
chown "$service_user:$service_group" "$temp_config"
chmod 0600 "$temp_config"
mv -f -- "$temp_config" "$config_file"
unset qb_password

temp_rar_wrapper=$(mktemp)
cat >"$temp_rar_wrapper" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ \$(id -u) -eq 0 ]]; then
  exec runuser -u ${service_user@Q} -- \
    env QBIT_CLEANER_MODE=rar \
    ${INSTALL_DIR@Q}/venv/bin/python \
    ${INSTALL_DIR@Q}/qbit_rar_cleaner.py \
    --config ${config_file@Q} "\$@"
fi
if [[ \$(id -un) != ${service_user@Q} ]]; then
  echo "Ezt a parancsot ${service_user@Q} felhasználóként vagy rootként futtasd." >&2
  exit 1
fi
exec env QBIT_CLEANER_MODE=rar \
  ${INSTALL_DIR@Q}/venv/bin/python \
  ${INSTALL_DIR@Q}/qbit_rar_cleaner.py \
  --config ${config_file@Q} "\$@"
EOF
install -o root -g root -m 0755 "$temp_rar_wrapper" "$RAR_COMMAND_PATH"
rm -f -- "$temp_rar_wrapper"

temp_iso_wrapper=$(mktemp)
cat >"$temp_iso_wrapper" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ \$(id -u) -ne 0 ]]; then
  echo "Az ISO mountolásához root jogosultság kell: sudo qbit-iso-cleaner" >&2
  exit 1
fi
exec ${unshare_command@Q} --mount --propagation private -- \
  env QBIT_CLEANER_MODE=iso \
  ${INSTALL_DIR@Q}/venv/bin/python \
  ${INSTALL_DIR@Q}/qbit_rar_cleaner.py \
  --config ${config_file@Q} "\$@"
EOF
install -o root -g root -m 0755 "$temp_iso_wrapper" "$ISO_COMMAND_PATH"
rm -f -- "$temp_iso_wrapper"
trap - EXIT

echo
if [[ "$rar_available" == true ]]; then
  echo "Biztonságos RAR-próbaüzem indul. Ez nem módosít fájlt vagy torrentet."
  if ! "$RAR_COMMAND_PATH"; then
    echo "A próbaüzem hibával leállt; a telepített fájlok megmaradtak a javításhoz." >&2
    exit 1
  fi
else
  echo "A RAR-próbaüzem kimarad, mert nincs működő RAR-kibontó."
fi

echo
echo "Telepítés kész."
echo "RAR parancs: ${RAR_COMMAND_PATH}"
echo "ISO parancs: ${ISO_COMMAND_PATH}"
echo "Valódi RAR-futás: sudo ${RAR_COMMAND_PATH} --apply"
echo "Az ISO-módot csak a RAR-futás befejezése után, külön indítsd."
echo "ISO-próbaüzem: sudo ${ISO_COMMAND_PATH}"
if [[ "$rar_available" == true ]]; then
  read -r -p "Elindítsam most a valódi RAR-feldolgozást? [y/N]: " run_now
  if [[ "$run_now" =~ ^[Yy]$ ]]; then
    "$RAR_COMMAND_PATH" --apply
  else
    echo "Nem indult valódi RAR-feldolgozás."
  fi
else
  echo "RAR-feldolgozás nem indítható a hiányzó RAR-támogatás miatt."
fi
