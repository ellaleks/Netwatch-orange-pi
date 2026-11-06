#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

fail() { printf 'Netwatch: %s\n' "$*" >&2; exit 1; }

interface=
gateway=
speed=1
while (($#)); do
    case "$1" in
        --interface) (($# >= 2)) || fail "Missing interface"; interface=$2; shift ;;
        --gateway) (($# >= 2)) || fail "Missing IPv4 gateway"; gateway=$2; shift ;;
        --no-speed) speed=0 ;;
        -h|--help)
            printf 'Usage: sudo bash install.sh [--interface NAME] [--gateway IPv4] [--no-speed]\n'
            exit 0 ;;
        *) fail "Unknown option: $1" ;;
    esac
    shift
done

[[ $(uname -s) == Linux && -d /run/systemd/system ]] ||
    fail "This installer requires a running Linux systemd system."
((EUID == 0)) || fail "Run with sudo bash install.sh."
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
app=/usr/local/lib/netwatch
state=/var/lib/netwatch
units=/etc/systemd/system
config=/etc/netwatch

requirements_met() {
    command -v ip >/dev/null && command -v runuser >/dev/null &&
    [[ -x /usr/bin/ping && -x /usr/bin/curl ]] &&
    /usr/bin/python3 -c 'import sqlite3; from zoneinfo import ZoneInfo; ZoneInfo("Europe/Brussels")' 2>/dev/null
}

if ! requirements_met; then
    if command -v apt-get >/dev/null; then
        apt-get update
        apt-get install -y python3 iproute2 iputils-ping curl ca-certificates tzdata util-linux
        if apt-cache show linux-sysctl-defaults >/dev/null 2>&1; then
            apt-get install -y linux-sysctl-defaults
        fi
    elif command -v dnf >/dev/null; then
        dnf install -y python3 iproute iputils curl ca-certificates tzdata util-linux
    elif command -v pacman >/dev/null; then
        pacman -S --needed --noconfirm python iproute2 iputils curl ca-certificates tzdata util-linux
    fi
    requirements_met ||
        fail "Install Python 3.9+ with sqlite3, iproute2, iputils ping, curl, tzdata and util-linux."
fi

detected=$(/usr/bin/python3 "$source_dir/netwatch_config.py" "$interface" "$gateway")
read -r interface gateway <<< "$detected"
printf 'Netwatch: interface %s, gateway %s\n' "$interface" "$gateway"

service_user=netwatch
if [[ -d $state ]]; then
    owner=$(stat -c '%U' "$state")
    if [[ $owner != root ]]; then
        service_user=$owner
    fi
fi
[[ $service_user =~ ^[a-zA-Z0-9_.-]+$ ]] || fail "Invalid data directory owner."
if ! id "$service_user" >/dev/null 2>&1; then
    [[ $service_user == netwatch ]] || fail "Unknown data directory owner: $service_user"
    useradd --system --user-group --no-create-home --shell "$(command -v nologin)" netwatch
fi
[[ $(id -u "$service_user") != 0 ]] || fail "Netwatch requires a non-root user."
if [[ ! -d $state ]]; then
    install -d -o "$service_user" -g "$(id -gn "$service_user")" -m 0750 "$state"
fi
runuser -u "$service_user" -- test -w "$state" ||
    fail "$state is not writable by $service_user; existing permissions were preserved."
for db_file in "$state"/netwatch.sqlite3{,-wal,-shm}; do
    if [[ -e $db_file ]] && ! runuser -u "$service_user" -- test -w "$db_file"; then
        fail "$db_file is not writable by $service_user; existing permissions were preserved."
    fi
done
ping_status=0
runuser -u "$service_user" -- /usr/bin/ping -4 -n -q -I "$interface" -c 1 -W 1 "$gateway" \
    >/dev/null || ping_status=$?
((ping_status <= 1)) ||
    fail "ping could not run as $service_user. Check the interface and net.ipv4.ping_group_range."

stage=$(mktemp -d)
trap 'rm -f -- "$stage/config.ini" "$stage/netwatch.service" "$stage/netwatch-speed.service"; rmdir -- "$stage"' EXIT
printf '[network]\ninterface = %s\ngateway = %s\n' "$interface" "$gateway" > "$stage/config.ini"
for unit in netwatch.service netwatch-speed.service; do
    sed "s/^User=netwatch$/User=$service_user/" "$source_dir/$unit" > "$stage/$unit"
done

install -d -m 0755 "$app" "$config"
install -m 0644 "$source_dir"/{netwatch.py,netwatch-report.py,netwatch-speed.py,netwatch_analysis.py,netwatch_config.py,netwatch_summary.py,netwatch_telegram.py} "$app/"
systemd-analyze verify "$stage/netwatch.service" "$stage/netwatch-speed.service" "$source_dir/netwatch-speed.timer"
install -m 0644 "$stage/config.ini" "$config/config.ini"
install -m 0644 "$stage/netwatch.service" "$stage/netwatch-speed.service" "$source_dir/netwatch-speed.timer" "$units/"
systemctl daemon-reload
systemctl enable netwatch.service
systemctl restart netwatch.service
systemctl is-active --quiet netwatch.service
if ((speed)); then
    systemctl enable --now netwatch-speed.timer
else
    systemctl disable --now netwatch-speed.timer
fi

printf 'Netwatch installed. Report:\n  sudo -u %s python3 %s/netwatch-report.py --hours 24\n' "$service_user" "$app"
