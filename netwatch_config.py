import configparser
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path

CONFIG = Path("/etc/netwatch/config.ini")
EXTERNAL_TARGETS = ("1.1.1.1", "8.8.8.8")


def read_config(path=CONFIG):
    config = configparser.ConfigParser(interpolation=None)
    config.read(path, encoding="utf-8")
    return config["network"] if config.has_section("network") else {}


def network_settings():
    config = read_config()
    return config.get("interface", "wlan0"), config.get("gateway", "192.168.0.1")


def installation_config(interface, gateway):
    existing = read_config()
    if not interface:
        interface = existing.get("interface", "")
        gateway = gateway or existing.get("gateway", "")
    if not interface or not gateway:
        command = ["ip", "-4", "-j", "route", "get", EXTERNAL_TARGETS[0]]
        if interface:
            command.extend(("oif", interface))
        route = json.loads(subprocess.check_output(command, text=True))[0]
        interface = interface or route.get("dev", "")
        gateway = gateway or route.get("gateway", "")
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,15}", interface):
        raise ValueError("Could not identify the interface. Use --interface NAME.")
    if not gateway:
        raise ValueError("No IPv4 gateway found. Use --gateway ADDRESS.")
    gateway = str(ipaddress.IPv4Address(gateway))
    if gateway in EXTERNAL_TARGETS:
        raise ValueError("The gateway must differ from the external targets.")
    subprocess.run(["ip", "link", "show", "dev", interface],
                   check=True, stdout=subprocess.DEVNULL)
    return interface, gateway


if __name__ == "__main__":
    try:
        print(*installation_config(*sys.argv[1:]))
    except (OSError, ValueError, IndexError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Netwatch: {exc}")
