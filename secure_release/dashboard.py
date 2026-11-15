import ipaddress
import json
import os
import re
import socket
import subprocess
from datetime import datetime, timezone
from urllib.parse import urljoin

import pandas as pd
import requests
import streamlit as st

DATA_FILE = os.path.join(os.path.dirname(__file__), "clients_data.json")
ALIASES_FILE = os.path.join(os.path.dirname(__file__), "client_aliases.json")
COMMANDS_FILE = os.path.join(os.path.dirname(__file__), "client_commands.json")
API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:5000")
CLIENT_SECRET_TOKEN = os.getenv("CLIENT_SECRET_TOKEN") or "REPLACE_WITH_STRONG_TOKEN"
DEFAULT_GATEWAY = os.getenv("DEFAULT_GATEWAY", "10.10.10.1")
ROUTER_IP = os.getenv("ROUTER_IP", DEFAULT_GATEWAY or "10.10.10.1")
ROUTER_USERNAME = os.getenv("ROUTER_USERNAME") or "REPLACE_WITH_ROUTER_USERNAME"
ROUTER_PASSWORD = os.getenv("ROUTER_PASSWORD") or "REPLACE_WITH_ROUTER_PASSWORD"


def _router_config() -> dict:
    """Return router config defensively from module globals or environment variables."""
    ip_value = globals().get("ROUTER_IP") or os.getenv("ROUTER_IP", DEFAULT_GATEWAY or "10.10.10.1")
    username_value = globals().get("ROUTER_USERNAME") or os.getenv("ROUTER_USERNAME") or "REPLACE_WITH_ROUTER_USERNAME"
    password_value = globals().get("ROUTER_PASSWORD") or os.getenv("ROUTER_PASSWORD") or "REPLACE_WITH_ROUTER_PASSWORD"
    globals()["ROUTER_IP"] = ip_value
    globals()["ROUTER_USERNAME"] = username_value
    globals()["ROUTER_PASSWORD"] = password_value
    return {"ip": ip_value, "username": username_value, "password": password_value}


FAKE_CLIENT_MARKERS = (
    "test-pc",
    "validation-pc",
    "dummy-pc",
    "demo-pc",
    "sample-pc",
    "pc-test-01",
    "pc-real-01",
)
ACTIVE_HEARTBEAT_THRESHOLD_MINUTES = 10


def is_fake_hostname_name(hostname: str) -> bool:
    value = str(hostname or "").strip().lower()
    if not value:
        return False
    if value in FAKE_CLIENT_MARKERS:
        return True
    return any(marker in value for marker in FAKE_CLIENT_MARKERS)


def sanitize_clients(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}

    cleaned = {}
    for key, record in data.items():
        if not isinstance(record, dict):
            continue

        hostname = str(record.get("hostname") or key or "").strip().lower()
        raw_key = str(key or "").strip().lower()
        ip_address = str(record.get("ip_address") or "").strip()

        if is_fake_hostname_name(hostname) or is_fake_hostname_name(raw_key):
            continue
        if ip_address.startswith("127.0.0.") and "pc" in hostname:
            continue

        cleaned[key] = record

    return cleaned


@st.cache_data
def load_clients() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return sanitize_clients(data)
    except Exception:
        return {}


@st.cache_data
def load_aliases() -> dict:
    if not os.path.exists(ALIASES_FILE):
        return {}
    try:
        with open(ALIASES_FILE, "r", encoding="utf8") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@st.cache_data
def load_command_queue() -> list:
    if not os.path.exists(COMMANDS_FILE):
        return []
    try:
        with open(COMMANDS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_aliases(aliases: dict):
    try:
        with open(ALIASES_FILE, "w", encoding="utf-8") as file:
            json.dump(aliases, file, ensure_ascii=False, indent=2)
    except Exception:
        pass


def save_command_queue(queue: list):
    try:
        with open(COMMANDS_FILE, "w", encoding="utf-8") as file:
            json.dump(queue, file, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_display_name(hostname: str, aliases: dict) -> str:
    key = str(hostname or "").strip()
    if not key:
        return "Unknown"
    alias = aliases.get(key.lower()) if isinstance(aliases, dict) else None
    if isinstance(alias, dict):
        return str(alias.get("name") or alias.get("label") or key)
    if isinstance(alias, str) and alias.strip():
        return alias.strip()
    return key


def parse_minutes(value) -> float:
    if not value:
        return 999999.0
    try:
        ts = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return 999999.0


def has_recent_heartbeat(hostname: str, record: dict | None = None, threshold_minutes: int = ACTIVE_HEARTBEAT_THRESHOLD_MINUTES) -> bool:
    if record is not None:
        for key in ("last_seen", "timestamp"):
            value = record.get(key)
            if value and parse_minutes(value) <= threshold_minutes:
                return True

    heartbeat = find_last_heartbeat(str(hostname or "").upper())
    if heartbeat.get("timestamp"):
        return parse_minutes(heartbeat.get("timestamp")) <= threshold_minutes
    return False


def resolve_client_status(record: dict, hostname: str) -> str:
    explicit_status = str(record.get("status") or "").strip().lower()
    ts_value = record.get("last_seen") or record.get("timestamp") or ""
    log_heartbeat = find_last_heartbeat(str(hostname or "").upper())
    threshold = ACTIVE_HEARTBEAT_THRESHOLD_MINUTES
    active_window = str(record.get("active_window") or "-").strip().lower()

    if "offline / no heartbeat" in active_window:
        return "offline"
    if "online" in active_window and "offline" not in active_window:
        return "online"

    if log_heartbeat and log_heartbeat.get("timestamp"):
        last_log_minutes = parse_minutes(log_heartbeat.get("timestamp"))
        if last_log_minutes <= threshold:
            return "online"
        return "offline"

    if ts_value:
        minutes = parse_minutes(ts_value)
        if explicit_status in {"online", "offline"}:
            if explicit_status == "online":
                return "online" if minutes <= threshold else "offline"
            return "offline"
        return "online" if minutes <= threshold else "offline"

    return "online" if explicit_status == "online" else "offline" if explicit_status == "offline" else "offline"


def normalize_ipv4(value, fallback: str = DEFAULT_GATEWAY or "192.168.9.1") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    if not text or text == "None":
        return fallback
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", text):
        parts = [int(part) for part in text.split(".")]
        if all(0 <= part <= 255 for part in parts):
            return ".".join(str(part) for part in parts)
    return fallback


def mask_ip_for_display(value, enabled: bool = False, hide_last_digits: int = 5) -> str:
    raw = str(value or "").strip()
    if not raw or raw in {"None", "-"}:
        return raw
    if not enabled:
        return raw
    if hide_last_digits <= 0:
        return raw
    if len(raw) <= hide_last_digits:
        return "*" * len(raw)
    return raw[:-hide_last_digits] + ("x" * hide_last_digits)


def format_percent(value) -> str:
    try:
        return f"{float(value or 0):.0f}%"
    except Exception:
        return "0%"


def normalize_client(record: dict, key: str) -> dict:
    hostname = str(record.get("hostname") or key or "unknown").strip() or "unknown"
    last_seen = record.get("last_seen") or record.get("timestamp") or "-"
    status = resolve_client_status(record, hostname)

    network = record.get("network") or {}
    speaker = record.get("speaker") or {}
    gateway_value = normalize_ipv4(network.get("gateway") or DEFAULT_GATEWAY or "192.168.9.1", DEFAULT_GATEWAY or "192.168.9.1")

    return {
        "id": key,
        "hostname": hostname,
        "display_name": get_display_name(hostname, load_aliases()),
        "ip_address": normalize_ipv4(record.get("ip_address"), "0.0.0.0"),
        "status": status,
        "last_seen": str(last_seen),
        "active_window": record.get("active_window", "-"),
        "cpu_percent": float(record.get("cpu_percent", 0) or 0),
        "ram_percent": float(record.get("ram_percent", 0) or 0),
        "os": record.get("os", "Windows"),
        "network": {
            **network,
            "gateway": gateway_value,
            "interface_name": network.get("interface_name", "Unknown"),
        },
        "speaker": speaker,
        "open_windows": record.get("open_windows", []),
        "gateway": gateway_value,
        "interface_name": network.get("interface_name", "Unknown"),
        "audio_status": speaker.get("audio_status", "Disconnected"),
        "speaker_connected": speaker.get("speaker_connected", False),
    }


def status_badge_html(status: str) -> str:
    palette = {
        "online": "#22c55e",
        "offline": "#ef4444",
    }
    label_map = {
        "online": "Online",
        "offline": "Offline",
    }
    color = palette.get(status, "#94a3b8")
    text = label_map.get(status, "Offline")
    return f"<span style='background:{color}; color:white; border-radius:999px; padding:5px 10px; font-size:12px; font-weight:700;'>{text}</span>"


def style_status_value(value):
    if value == "Online":
        return "background-color: #dcfce7; color: #166534; font-weight: 700;"
    if value == "Offline":
        return "background-color: #fee2e2; color: #991b1b; font-weight: 700;"
    return "background-color: #f1f5f9; color: #334155; font-weight: 600;"


def detail_summary_for_row(row: dict) -> str:
    ip_value = str(row.get("ip_address") or "-")
    gateway_value = str((row.get("network") or {}).get("gateway") or "-")
    audio_value = str((row.get("speaker") or {}).get("audio_status") or "-")
    active_window = str(row.get("active_window") or "-")
    return f"IP: {ip_value} | Gateway: {gateway_value} | Audio: {audio_value} | Window: {active_window}"


def command_status_badge(status: str) -> str:
    normalized = str(status or "queued").strip().lower()
    palette = {
        "queued": "#f59e0b",
        "executed": "#22c55e",
        "done": "#22c55e",
        "failed": "#ef4444",
        "error": "#ef4444",
    }
    labels = {
        "queued": "Queued",
        "executed": "Executed",
        "done": "Executed",
        "failed": "Failed",
        "error": "Failed",
    }
    color = palette.get(normalized, "#94a3b8")
    label = labels.get(normalized, "Queued")
    return f"<span style='background:{color}; color:white; border-radius:999px; padding:4px 10px; font-size:11px; font-weight:700;'>{label}</span>"


def issue_remote_command(hostname: str, command: str):
    if not hostname or not command:
        st.warning("Pilih client dan isi perintah terlebih dahulu.")
        return False

    payload = {"hostname": hostname, "ip_address": "", "command": command}
    try:
        response = requests.post(
            f"{API_BASE_URL}/api/command",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json", "X-Token": CLIENT_SECRET_TOKEN},
            timeout=5,
        )
        if response.status_code == 200:
            st.success(f"Perintah dikirim ke {hostname}: {command}")
            return True
        st.error(f"Gagal kirim perintah. HTTP {response.status_code}: {response.text[:200]}")
        return False
    except Exception as exc:
        st.error(f"Gagal terhubung ke API: {exc}")
        return False


def set_client_alias(hostname: str, label: str):
    if not hostname:
        return False
    aliases = load_aliases()
    clean_name = str(label or "").strip()
    if clean_name:
        aliases[str(hostname).lower()] = clean_name
    else:
        aliases.pop(str(hostname).lower(), None)
    save_aliases(aliases)
    st.cache_data.clear()
    return True


def issue_force_heartbeat(hostname: str):
    if not hostname:
        st.warning("Pilih client terlebih dahulu.")
        return False

    payload = {"hostname": hostname, "command": "force_heartbeat"}
    try:
        response = requests.post(
            f"{API_BASE_URL}/api/force_heartbeat",
            json=payload,
            headers={"Content-Type": "application/json", "X-Token": CLIENT_SECRET_TOKEN},
            timeout=5,
        )
        if response.status_code == 200:
            st.success(f"Perintah force heartbeat dikirim ke {hostname}.")
            return True
        st.error(f"Gagal memaksa heartbeat. HTTP {response.status_code}: {response.text[:200]}")
        return False
    except Exception as exc:
        st.error(f"Gagal terhubung ke API: {exc}")
        return False


def issue_test_command(hostname: str):
    return issue_remote_command(hostname, "hostname")


def detect_local_network() -> dict:
    gateway = DEFAULT_GATEWAY or "192.168.9.1"
    subnet = "192.168.2.0/24"
    active_ips = []
    interface_names = []

    try:
        if os.name == "nt":
            powershell = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty IPAddress; if ($ips) { $ips | ForEach-Object { $_ } }; $gw = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty NextHop -First 1; if ($gw) { $gw }",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            output = powershell.stdout or ""
            values = []
            for line in output.splitlines():
                candidate = line.strip()
                if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", candidate):
                    values.append(candidate)
            if values:
                active_ips = list(dict.fromkeys(values))
                gateway = next((v for v in values if v.count(".") == 3 and v.startswith("192.168") is False), gateway)
                if len(values) >= 2 and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", values[-1]):
                    gateway = values[-1]

            if not active_ips:
                result = subprocess.run(["ipconfig"], capture_output=True, text=True, check=False)
                output = result.stdout or ""
                for line in output.splitlines():
                    if re.search(r"IPv4 Address.*?:\s*(\d+\.\d+\.\d+\.\d+)", line):
                        match = re.search(r"IPv4 Address.*?:\s*(\d+\.\d+\.\d+\.\d+)", line)
                        if match:
                            active_ips.append(match.group(1))
                    gateway_match = re.search(r"Default Gateway.*?:\s*(\d+\.\d+\.\d+\.\d+)", line)
                    if gateway_match:
                        gateway = gateway_match.group(1)
                    if "Ethernet adapter" in line or "Wireless LAN adapter" in line:
                        interface_names.append(line.strip())
        else:
            result = subprocess.run(["ip", "route"], capture_output=True, text=True, check=False)
            output = result.stdout or ""
            for line in output.splitlines():
                if "default" in line.lower() and "." in line:
                    gateway = line.split()[-1]
            result = subprocess.run(["hostname", "-I"], capture_output=True, text=True, check=False)
            for ip_text in (result.stdout or "").split():
                if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip_text):
                    active_ips.append(ip_text)
    except Exception:
        pass

    if not active_ips:
        try:
            hostname = socket.gethostname()
            infos = socket.gethostbyname_ex(hostname)
            for item in infos[2]:
                if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", item):
                    active_ips.append(item)
        except Exception:
            pass

    if active_ips:
        for ip_text in active_ips:
            try:
                ip_obj = ipaddress.ip_address(ip_text)
                if ip_obj.version == 4:
                    subnet = str(ipaddress.ip_network(f"{ip_text}/24", strict=False))
                    break
            except Exception:
                continue

    gateway = normalize_ipv4(gateway, DEFAULT_GATEWAY or "192.168.9.1")
    return {"gateway": gateway, "subnet": subnet, "local_ips": active_ips, "interfaces": interface_names}


def discover_network_devices():
    """Discover active devices on the local LAN using gateway + ARP + ping fallback."""
    net_info = detect_local_network()
    gateway_ip = normalize_ipv4(net_info.get("gateway"), DEFAULT_GATEWAY or "192.168.9.1")
    subnet_value = net_info.get("subnet") or "192.168.2.0/24"
    try:
        network = ipaddress.ip_network(subnet_value, strict=False)
    except Exception:
        network = ipaddress.ip_network("192.168.2.0/24", strict=False)

    def ping_host(ip_text: str) -> bool:
        try:
            if os.name == "nt":
                completed = subprocess.run(
                    ["ping", "-n", "1", "-w", "250", ip_text],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                completed = subprocess.run(
                    ["ping", "-c", "1", "-W", "0.3", ip_text],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            return completed.returncode == 0
        except Exception:
            return False

    def parse_arp_entries() -> list:
        entries = []
        try:
            if os.name == "nt":
                completed = subprocess.run(["arp", "-a"], capture_output=True, text=True, check=False)
                for line in completed.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 3 and parts[0].count(".") == 3:
                        ip_text = parts[0]
                        try:
                            ip_obj = ipaddress.ip_address(ip_text)
                            if ip_obj in network:
                                entries.append(ip_text)
                        except Exception:
                            continue
                if not entries:
                    netneighbor = subprocess.run(
                        ["powershell", "-NoProfile", "-Command", "Get-NetNeighbor -AddressFamily IPv4 | Select-Object -ExpandProperty IPAddress"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    for line in (netneighbor.stdout or "").splitlines():
                        candidate = line.strip()
                        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", candidate):
                            try:
                                ip_obj = ipaddress.ip_address(candidate)
                                if ip_obj in network:
                                    entries.append(candidate)
                            except Exception:
                                continue
            else:
                completed = subprocess.run(["ip", "neigh"], capture_output=True, text=True, check=False)
                for line in completed.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 4 and parts[0].count(".") == 3:
                        ip_text = parts[0]
                        try:
                            ip_obj = ipaddress.ip_address(ip_text)
                            if ip_obj in network:
                                entries.append(ip_text)
                        except Exception:
                            continue
        except Exception:
            return []
        return entries

    def resolve_hostname(ip_text: str) -> str:
        try:
            return socket.gethostbyaddr(ip_text)[0]
        except Exception:
            return "Unknown"

    def classify_device(hostname: str, ip_text: str, gateway_ip: str) -> str:
        lowered = (hostname or "").lower()
        if ip_text == gateway_ip or ip_text.endswith(".1") or ip_text.endswith(".254"):
            return "Access Point"
        if any(token in lowered for token in ["router", "ap", "access point", "wifi", "wireless", "wifi-ap", "nas", "printer", "laptop", "desktop", "pc"]):
            return "Wi‑Fi / LAN"
        return "Wi‑Fi / LAN"

    discovered = {}
    arp_entries = parse_arp_entries()
    local_ip_set = set((net_info.get("local_ips") or []))
    candidate_ips = list(dict.fromkeys(arp_entries))

    for host in range(1, 255):
        ip_text = str(ipaddress.IPv4Address(int(network.network_address) + host))
        if ip_text == "255.255.255.255":
            continue
        if ip_text not in candidate_ips:
            candidate_ips.append(ip_text)

    seen = set()
    for ip_text in candidate_ips:
        try:
            ip_obj = ipaddress.ip_address(ip_text)
            if ip_obj not in network:
                continue
        except Exception:
            continue
        if ip_text in seen:
            continue
        seen.add(ip_text)

        if ip_text == gateway_ip:
            discovered[ip_text] = {
                "hostname": "Router / AP",
                "ip": gateway_ip,
                "gateway": gateway_ip,
                "router_ap": gateway_ip,
                "interface": "Default Gateway",
                "status": "online",
                "source_type": "Access Point",
                "source": "Gateway",
            }
            continue

        reachable = ping_host(ip_text) or ip_text in arp_entries
        if reachable or ip_text in local_ip_set or ip_text.startswith("192.168.2."):
            hostname_lookup = resolve_hostname(ip_text)
            discovered[ip_text] = {
                "hostname": hostname_lookup,
                "ip": ip_text,
                "gateway": gateway_ip,
                "router_ap": gateway_ip,
                "interface": "Local Network",
                "status": "online",
                "source_type": classify_device(hostname_lookup, ip_text, gateway_ip),
                "source": "ARP" if ip_text in arp_entries else ("Local IP" if ip_text in local_ip_set else "Ping"),
            }

    rows = sorted(discovered.values(), key=lambda item: item["ip"])
    if not rows and gateway_ip:
        rows = [{
            "hostname": "Router / AP",
            "ip": gateway_ip,
            "gateway": gateway_ip,
            "router_ap": gateway_ip,
            "interface": "Default Gateway",
            "status": "online",
            "source_type": "Access Point",
            "source": "Gateway",
        }]
    return rows


def connected_devices_summary(selected_client: dict) -> dict:
    network_data = selected_client.get("network") or {}
    speaker_data = selected_client.get("speaker") or {}

    audio_devices = []
    if isinstance(speaker_data.get("available_devices"), list):
        audio_devices = speaker_data.get("available_devices")
    elif isinstance(speaker_data.get("devices"), list):
        audio_devices = speaker_data.get("devices")

    network_adapters = []
    if isinstance(network_data.get("available_adapters"), list):
        network_adapters = network_data.get("available_adapters")
    elif isinstance(network_data.get("interfaces"), list):
        network_adapters = network_data.get("interfaces")

    return {
        "audio": {
            "speaker_connected": bool(speaker_data.get("speaker_connected", False)),
            "audio_status": speaker_data.get("audio_status", "Disconnected"),
            "available_devices": audio_devices,
            "device_name": speaker_data.get("device_name", "N/A"),
        },
        "network": {
            "gateway": normalize_ipv4(network_data.get("gateway") or DEFAULT_GATEWAY or "192.168.9.1", DEFAULT_GATEWAY or "192.168.9.1"),
            "interface_name": network_data.get("interface_name", "Unknown"),
            "available_adapters": network_adapters,
            "critical_ips": network_data.get("critical_ips", []),
        },
    }


def issue_device_preference(hostname: str, kind: str, device_name: str):
    if not hostname or not kind or not device_name:
        st.warning("Nama client atau device tidak valid.")
        return False

    st.info(f"Preferensi {kind} untuk {hostname} disimpan secara lokal: {device_name}")
    return True


def format_duration_minutes(minutes_value: float) -> str:
    try:
        total_minutes = max(0, int(float(minutes_value or 0)))
    except Exception:
        total_minutes = 0

    days, rem = divmod(total_minutes, 24 * 60)
    hours, rem = divmod(rem, 60)
    if days > 0:
        return f"{days} hari {hours} jam" if hours else f"{days} hari"
    if hours > 0:
        return f"{hours} jam {rem} menit" if rem else f"{hours} jam"
    if rem > 0:
        return f"{rem} menit"
    return "kurang dari 1 menit"


def get_offline_since(record: dict, hostname: str) -> str:
    last_seen = str(record.get("last_seen") or record.get("timestamp") or "-")
    if last_seen == "-":
        heartbeat_info = find_last_heartbeat(hostname)
        if heartbeat_info and heartbeat_info.get("timestamp"):
            last_seen = heartbeat_info.get("timestamp")
        else:
            return "Belum pernah online / tidak ada heartbeat"

    try:
        ts = str(last_seen).replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        elapsed_minutes = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
        return format_duration_minutes(elapsed_minutes)
    except Exception:
        return "Tidak diketahui"


def find_last_heartbeat(hostname: str) -> dict:
    if not hostname:
        return {}

    log_path = os.path.join(os.path.dirname(__file__), "server_debug.log")
    if not os.path.exists(log_path):
        return {}

    target = str(hostname).strip().upper()
    last_hit = {}
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as file:
            for line in file:
                if "HEARTBEAT_ACCEPTED" not in line:
                    continue
                if target not in line.upper():
                    continue
                match = re.search(r"\[(.*?)\].*?HEARTBEAT_ACCEPTED hostname=(.*?) ip=(.*?)(?:\s|$)", line)
                if not match:
                    continue
                timestamp = match.group(1).strip()
                detected_hostname = (match.group(2) or "").strip().upper()
                ip_value = (match.group(3) or "").strip()
                if detected_hostname == target:
                    last_hit = {"timestamp": timestamp, "hostname": detected_hostname, "ip": ip_value}
    except Exception:
        return {}
    return last_hit


def list_known_hostnames_from_log() -> dict:
    log_path = os.path.join(os.path.dirname(__file__), "server_debug.log")
    if not os.path.exists(log_path):
        return {}

    known = {}
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as file:
            for line in file:
                if "HEARTBEAT_ACCEPTED" not in line:
                    continue
                match = re.search(r"\[(.*?)\].*?HEARTBEAT_ACCEPTED hostname=(.*?) ip=(.*?)(?:\s|$)", line)
                if not match:
                    continue
                timestamp = match.group(1).strip()
                hostname = (match.group(2) or "").strip().upper()
                ip_value = (match.group(3) or "").strip()
                if not hostname:
                    continue
                if hostname.lower() in FAKE_CLIENT_MARKERS or any(marker in hostname.lower() for marker in FAKE_CLIENT_MARKERS):
                    continue
                known[hostname] = {"timestamp": timestamp, "ip": ip_value, "hostname": hostname}
    except Exception:
        return {}
    return known


def get_recent_heartbeat_hosts() -> dict:
    recent = {}
    for hostname, info in list_known_hostnames_from_log().items():
        ts_value = info.get("timestamp")
        if not ts_value:
            continue
        if parse_minutes(ts_value) <= ACTIVE_HEARTBEAT_THRESHOLD_MINUTES:
            recent[hostname] = info
    return recent


def get_recent_heartbeat_rows() -> list:
    """Return active devices derived from the server heartbeat log for the Network page."""
    rows = []
    seen = set()
    for hostname, info in get_recent_heartbeat_hosts().items():
        ip_text = str(info.get("ip") or "").strip()
        if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", ip_text):
            continue
        key = (str(hostname).upper(), ip_text)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "hostname": str(hostname).strip(),
            "ip": ip_text,
            "gateway": ROUTER_IP,
            "connected_at": info.get("timestamp") or "Tidak diketahui",
            "source_type": "Heartbeat log",
            "source": "server_debug.log",
            "status": "online",
        })
    return rows


def build_known_clients() -> dict:
    active = sanitize_clients(load_clients())
    aliases = load_aliases()
    merged = {}
    seen = set()

    for key, record in active.items():
        hostname = str(record.get("hostname") or key or "").strip()
        if not hostname or is_fake_hostname_name(hostname):
            continue
        host_key = hostname.upper()
        seen.add(host_key)
        merged[host_key] = record

    for hostname, heartbeat in list_known_hostnames_from_log().items():
        host_key = str(hostname or "").upper()
        if not host_key or is_fake_hostname_name(host_key):
            continue
        if host_key in seen:
            continue
        seen.add(host_key)
        merged[host_key] = {
            "hostname": host_key,
            "ip_address": heartbeat.get("ip", "0.0.0.0"),
            "last_seen": heartbeat.get("timestamp", "-"),
            "timestamp": heartbeat.get("timestamp", "-"),
            "status": "offline",
            "active_window": "Offline / no heartbeat",
            "open_windows": [],
            "cpu_percent": 0,
            "ram_percent": 0,
            "os": "Windows",
            "network": {
                "gateway": DEFAULT_GATEWAY or "192.168.9.1",
                "interface_name": "Unknown",
                "interface_type": "LAN",
                "gateway_reachable": False,
                "critical_ips": [],
            },
            "speaker": {
                "speaker_connected": False,
                "audio_status": "Disconnected",
                "connection_type": "Unknown",
                "volume": 0,
                "muted": False,
                "device_name": "N/A",
            },
        }

    for host_key, record in list(merged.items()):
        hostname = str(record.get("hostname") or host_key).strip()
        if not hostname or is_fake_hostname_name(hostname):
            del merged[host_key]
            continue
        ts_value = record.get("last_seen") or record.get("timestamp") or find_last_heartbeat(hostname.upper()).get("timestamp")
        if ts_value:
            record["last_seen"] = str(ts_value)
            record["timestamp"] = str(ts_value)
            record["status"] = "online" if parse_minutes(ts_value) <= ACTIVE_HEARTBEAT_THRESHOLD_MINUTES else "offline"
        else:
            record["status"] = "offline"
        merged[host_key] = record

    existing_keys = {str(key).lower() for key in merged.keys()}
    for key, label in aliases.items():
        if not isinstance(label, str) or not label.strip():
            continue
        key_text = str(key).strip()
        if not key_text:
            continue
        if key_text.lower() in existing_keys:
            continue
        if is_fake_hostname_name(key_text):
            continue
        last_heartbeat = find_last_heartbeat(key_text.upper())
        if not last_heartbeat.get("timestamp"):
            continue

        merged[key_text.upper()] = {
            "hostname": key_text.upper(),
            "ip_address": last_heartbeat.get("ip", "0.0.0.0"),
            "last_seen": last_heartbeat.get("timestamp", "-"),
            "timestamp": last_heartbeat.get("timestamp", "-"),
            "status": "offline" if parse_minutes(last_heartbeat.get("timestamp")) > ACTIVE_HEARTBEAT_THRESHOLD_MINUTES else "online",
            "active_window": "Offline / no heartbeat",
            "open_windows": [],
            "cpu_percent": 0,
            "ram_percent": 0,
            "os": "Windows",
            "network": {
                "gateway": DEFAULT_GATEWAY or "192.168.9.1",
                "interface_name": "Unknown",
                "interface_type": "LAN",
                "gateway_reachable": False,
                "critical_ips": [],
            },
            "speaker": {
                "speaker_connected": False,
                "audio_status": "Disconnected",
                "connection_type": "Unknown",
                "volume": 0,
                "muted": False,
                "device_name": "N/A",
            },
        }

    return merged


def render_rename_section(rows: list):
    st.markdown("### Rename client")
    if not rows:
        st.info("Belum ada client yang tersedia untuk diubah nama.")
        return

    aliases = load_aliases()
    options = [(item["display_name"], item["hostname"]) for item in rows]
    display_names = [name for name, _ in options]
    selected_name = st.selectbox("Pilih client", display_names, index=0)
    selected_hostname = next((hostname for name, hostname in options if name == selected_name), rows[0]["hostname"])

    current_alias = aliases.get(str(selected_hostname).lower())
    alias_name = current_alias.get("name") if isinstance(current_alias, dict) else current_alias

    new_name = st.text_input("Nama baru", value=str(alias_name or selected_name), placeholder="Masukkan alias baru")
    if st.button("Simpan rename"):
        if set_client_alias(selected_hostname, new_name):
            st.success(f"Alias untuk {selected_hostname} berhasil disimpan.")
            st.rerun()


def render_command_section(rows: list):
    st.markdown("### Command")
    if not rows:
        st.info("Belum ada client yang bisa dikirim perintah.")
        return

    command_targets = [(item["display_name"], item["hostname"]) for item in rows]
    target_names = [label for label, _ in command_targets]
    chosen_label = st.selectbox("Pilih client", target_names, index=0)
    hostname = next((host for label, host in command_targets if label == chosen_label), rows[0]["hostname"])
    command_text = st.text_input("Perintah", value="hostname")
    if st.button("Kirim perintah"):
        issue_remote_command(hostname, command_text)


def infer_device_type(hostname: str, source_type: str = "", ip_text: str = "", mac: str = "") -> str:
    text = str(hostname or "").lower()
    source_lower = str(source_type or "").lower()
    ip_value = str(ip_text or "").lower()
    mac_text = str(mac or "").lower()

    if any(token in text for token in [
        "iphone", "ipad", "android", "galaxy", "pixel", "xiaomi", "redmi", "miui",
        "oneplus", "oppo", "vivo", "huawei", "motorola", "nokia", "samsung", "sony",
        "phone", "smartphone"
    ]):
        return "Smartphone"

    if any(token in text for token in ["tablet", "kindle", "galaxytab", "ipad"]):
        return "Tablet"

    if any(token in text for token in ["laptop", "desktop", "pc", "notebook", "lenovo", "dell", "asus", "acer", "hp", "msi", "surface", "workstation"]):
        return "Laptop/Desktop"

    if any(token in text for token in ["tv", "smarttv", "androidtv", "bravia", "roku", "chromecast", "firetv", "xiaomi tv", "tvbox"]):
        return "Smart TV"

    if any(token in text for token in ["printer", "print", "epson", "canon", "hp printer"]):
        return "Printer"

    if any(token in text for token in ["camera", "ipcam", "cctv", "nest", "doorbell", "webcam"]):
        return "Camera"

    if any(token in text for token in ["nas", "synology", "qnap", "wdmycloud", "server"]):
        return "NAS/Server"

    if any(token in text for token in ["router", "ap", "access point", "mesh", "tenda", "tplink", "linksys", "zte", "huawei", "asus router", "wifi"]):
        return "Router/AP"

    if any(token in text for token in ["watch", "wearos", "fitbit", "smartwatch"]):
        return "Smartwatch"

    if any(token in text for token in ["echo", "alexa", "googlehome", "speaker", "sonos"]):
        return "Speaker/IoT"

    if source_lower and "lan" in source_lower and ip_value.endswith(".1"):
        return "Router/AP"

    if mac_text and mac_text.startswith("dc:a6:32"):
        return "Smartphone"

    return "Unknown"


def infer_connection_type(hostname: str, source_type: str = "", ip_text: str = "", gateway: str = "") -> str:
    text = str(hostname or "").lower()
    if not text and source_type:
        text = str(source_type).lower()
    if ip_text and gateway and str(ip_text).strip() == str(gateway).strip():
        return "Router"
    if any(token in text for token in ["wifi", "wireless", "wlan", "laptop", "phone", "tablet", "desktop", "pc"]):
        return "Wi‑Fi"
    if source_type and "lan" in str(source_type).lower():
        return "LAN"
    if source_type and "access point" in str(source_type).lower():
        return "LAN"
    if ip_text and ip_text.endswith(".1"):
        return "LAN"
    return "Wi‑Fi"


def normalize_router_link_value(raw_type: str = "", fallback: str = "") -> str:
    """Normalize router-reported link type to a consistent LAN/Wi‑Fi label."""
    value = str(raw_type or "").strip().lower()
    if not value:
        value = str(fallback or "").strip().lower()

    if any(token in value for token in ["wireless", "wifi", "wlan", "radio"]):
        return "Wi‑Fi"
    if any(token in value for token in ["lan", "ethernet", "wired", "cable", "static"]):
        return "LAN"
    if any(token in value for token in ["router", "ap", "access point", "mesh", "bridge"]):
        return "LAN"
    if value in {"wifi", "wifi-5g", "wlan-2g", "wlan-5g"}:
        return "Wi‑Fi"
    if value in {"lan", "lan1", "lan2", "ethernet1", "ethernet2"}:
        return "LAN"

    return "Wi‑Fi" if fallback.lower() in {"wifi", "wireless"} else "LAN" if fallback.lower() in {"lan", "ethernet", "wired"} else "Wi‑Fi"


def infer_router_link_type(raw_type: str = "", hostname: str = "", ip_text: str = "", gateway: str = "") -> str:
    detected = normalize_router_link_value(raw_type)
    if detected in {"LAN", "Wi‑Fi"}:
        return detected

    text = str(hostname or "").lower()
    if ip_text and gateway and str(ip_text).strip() == str(gateway).strip():
        return "Router"
    if any(token in text for token in ["wifi", "wireless", "wlan", "phone", "tablet", "laptop", "desktop", "pc"]):
        return "Wi‑Fi"
    if any(token in text for token in ["router", "ap", "mesh", "nas", "server", "printer", "tv"]):
        return "LAN"
    if ip_text and ip_text.endswith(".1"):
        return "LAN"
    return "Wi‑Fi"


def normalize_network_rows(rows: list) -> list:
    normalized = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        host = str(row.get("hostname") or "Unknown").strip() or "Unknown"
        ip_text = str(row.get("ip") or "").strip()
        gateway = str(row.get("gateway") or ROUTER_IP or DEFAULT_GATEWAY or "192.168.4.1").strip()
        source_type = str(row.get("source_type") or "").strip() or "DHCP"
        source = str(row.get("source") or "").strip() or "Router"
        status = str(row.get("status") or "online").strip().lower() or "online"
        raw_link_type = str(row.get("router_link_type") or row.get("type") or row.get("connection_type") or source_type or "").strip()
        connection_type = infer_router_link_type(raw_link_type, host, ip_text, gateway)
        connected_at = get_connection_seen_timestamp(host, row.get("connected_at") or row.get("lease_time") or row.get("last_seen") or "")
        device_type = infer_device_type(host, source_type, ip_text, row.get("mac") or "")
        normalized.append({
            "hostname": host,
            "ip": ip_text,
            "gateway": gateway,
            "connection_type": connection_type,
            "device_type": device_type,
            "connected_at": connected_at,
            "source_type": source_type,
            "source": source,
            "status": status,
        })
    return normalized


def render_network_section():
    st.markdown("### Network discovery")

    refresh_col, toggle_col, _ = st.columns([1, 1.5, 4.5])
    with refresh_col:
        if st.button("Refresh", key="network_refresh"):
            st.cache_data.clear()
            st.rerun()
    with toggle_col:
        st.session_state.setdefault("network_ip_mask", False)
        st.checkbox("Sembunyikan 5 digit IP", key="network_ip_mask", value=st.session_state.get("network_ip_mask", False))

    st.caption("Memuat data jaringan dari DHCP modem/router 10.10.10.1...")
    with st.spinner("Mengambil data router dan perangkat yang terhubung..."):
        router_rows = normalize_network_rows(cached_router_dhcp_clients())

    if not router_rows:
        fallback_rows = get_recent_heartbeat_rows()
        if not fallback_rows:
            for key, record in build_known_clients().items():
                ip_text = str((record.get("network") or {}).get("gateway") or record.get("ip_address") or "").strip()
                if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", ip_text):
                    ip_text = str(record.get("ip_address") or "").strip()
                if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", ip_text):
                    continue
                fallback_rows.append({
                    "hostname": str(record.get("hostname") or key).strip() or "Unknown",
                    "ip": ip_text,
                    "gateway": ROUTER_IP,
                    "connected_at": str(record.get("last_seen") or "Tidak diketahui"),
                    "source_type": "Fallback local client",
                    "source": "heartbeat/local",
                    "status": "online" if str(record.get("status") or "").lower() == "online" else "offline",
                })
        if fallback_rows:
            router_rows = normalize_network_rows(fallback_rows)
            st.warning("DHCP modem/router tidak bisa dibaca saat ini. Sistem menampilkan data heartbeat lokal sebagai fallback agar daftar perangkat tetap terlihat.")
        else:
            st.warning("Tidak ada perangkat yang terbaca dari DHCP modem/router 10.10.10.1. Pastikan login router berhasil dan sesi modem aktif.")
            return

    router_ap_rows = []
    non_router_rows = []
    detected_router_ips = set()
    for row in router_rows:
        hostname = str(row.get("hostname") or "Unknown").strip()
        ip_text = str(row.get("ip") or "").strip()
        source_type = str(row.get("source_type") or "").strip()
        device_type = str(row.get("device_type") or "").strip()
        masked_ip = mask_ip_for_display(ip_text, enabled=st.session_state.get("network_ip_mask", False))
        if is_router_ap_device(hostname, ip_text, source_type, device_type):
            detected_router_ips.add(ip_text)
            router_ap_rows.append({
                "Hostname": hostname,
                "IP": masked_ip,
                "Koneksi": row.get("connection_type") or "Wi‑Fi",
                "Jenis perangkat": device_type or "Router/AP",
                "Status": str(row.get("status") or "online").strip().lower() == "online" and "Online" or "Offline",
            })
        else:
            non_router_rows.append({
                "Hostname": hostname,
                "IP": masked_ip,
                "Koneksi": row.get("connection_type") or "Wi‑Fi",
                "Jenis perangkat": device_type or "Unknown",
            })

    for ip_value in sorted(KNOWN_ROUTER_IPS):
        if ip_value in detected_router_ips:
            continue
        router_ap_rows.append({
            "Hostname": "Router",
            "IP": mask_ip_for_display(ip_value, enabled=st.session_state.get("network_ip_mask", False)),
            "Koneksi": "LAN",
            "Jenis perangkat": "Router/AP",
            "Status": "Offline",
        })

    total_router = len(router_ap_rows)
    total_non_router = len(non_router_rows)
    total_all = len(router_rows)

    st.info("Ringkasan perangkat berdasarkan daftar router dan device yang terdeteksi dari DHCP modem/router 10.10.10.1.")

    summary_cards = st.columns(3)
    card_styles = [
        """
        <div style='background:linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%); border-radius:16px; padding:16px 18px; min-height:120px; color:white; box-shadow: 0 8px 20px rgba(79, 70, 229, 0.18); border:1px solid rgba(255,255,255,0.12);'>
            <div style='font-size:11px; opacity:0.8; letter-spacing:0.12em; text-transform:uppercase;'>{label}</div>
            <div style='font-size:34px; font-weight:700; margin-top:10px; line-height:1.1;'>{value}</div>
        </div>
        """,
        """
        <div style='background:linear-gradient(135deg, #0f766e 0%, #14b8a6 100%); border-radius:16px; padding:16px 18px; min-height:120px; color:white; box-shadow: 0 8px 20px rgba(20, 184, 166, 0.18); border:1px solid rgba(255,255,255,0.12);'>
            <div style='font-size:11px; opacity:0.8; letter-spacing:0.12em; text-transform:uppercase;'>{label}</div>
            <div style='font-size:34px; font-weight:700; margin-top:10px; line-height:1.1;'>{value}</div>
        </div>
        """,
        """
        <div style='background:linear-gradient(135deg, #1f2937 0%, #374151 100%); border-radius:16px; padding:16px 18px; min-height:120px; color:white; box-shadow: 0 8px 20px rgba(31, 41, 55, 0.18); border:1px solid rgba(255,255,255,0.12);'>
            <div style='font-size:11px; opacity:0.8; letter-spacing:0.12em; text-transform:uppercase;'>{label}</div>
            <div style='font-size:34px; font-weight:700; margin-top:10px; line-height:1.1;'>{value}</div>
        </div>
        """,
    ]

    with summary_cards[0]:
        st.markdown(card_styles[0].format(label="Router total", value=str(total_router)), unsafe_allow_html=True)
    with summary_cards[1]:
        st.markdown(card_styles[1].format(label="Perangkat total (tanpa router)", value=str(total_non_router)), unsafe_allow_html=True)
    with summary_cards[2]:
        st.markdown(card_styles[2].format(label="Total semua perangkat", value=str(total_all)), unsafe_allow_html=True)

    router_badges = []
    for row in router_ap_rows:
        badge_text = f"{row['IP']} ({row['Hostname']})"
        if str(row.get("Status", "Online")).lower() == "offline":
            badge_text += " — Offline"
        color = "#fef2f2" if str(row.get("Status", "Online")).lower() == "offline" else "#e0e7ff"
        text_color = "#7f1d1d" if str(row.get("Status", "Online")).lower() == "offline" else "#1e1b4b"
        border_color = "#fecaca" if str(row.get("Status", "Online")).lower() == "offline" else "#c7d2fe"
        router_badges.append(
            f"<span style='display:inline-block; margin:6px 8px 6px 0; padding:8px 12px; background:{color}; color:{text_color}; border:1px solid {border_color}; border-radius:999px; font-weight:600; font-size:13px;'>{badge_text}</span>"
        )

    if router_badges:
        badge_html = "".join(router_badges)
        st.markdown(
            f"""
            <div style='margin-top:18px; background:#f8fafc; border:1px solid #e2e8f0; border-radius:14px; padding:14px 16px;'>
                <div style='font-size:11px; color:#475569; letter-spacing:0.12em; text-transform:uppercase; margin-bottom:10px;'>Daftar Router</div>
                <div>{badge_html}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    all_devices_table = pd.DataFrame([
        {
            "Hostname": str(row.get("hostname") or "Unknown").strip(),
            "IP": mask_ip_for_display(str(row.get("ip") or "").strip(), enabled=st.session_state.get("network_ip_mask", False)),
            "Jenis perangkat": str(row.get("device_type") or "Unknown").strip(),
            "Sumber data": str(row.get("source") or "DHCP Client List").strip(),
        }
        for row in router_rows
    ])

    if not all_devices_table.empty:
        st.markdown("### Semua perangkat")
        st.dataframe(all_devices_table, use_container_width=True, hide_index=True, height=680)

    st.caption("Sumber data: DHCP Client List modem/router 10.10.10.1")


def _collect_router_form_fields(html: str) -> dict:
    """Collect the router login form fields and hidden values needed for a POST login request."""
    if not html:
        return {}

    fields = {}
    for name_match in re.finditer(r'name\s*=\s*["\']([^"\']+)["\']', html, flags=re.I):
        name = name_match.group(1).strip()
        if not name:
            continue
        fields[name] = ""

    for attr, key in [(r'value\s*=\s*["\']([^"\']*)["\']', "value"), (r'id\s*=\s*["\']([^"\']*)["\']', "id")]:
        for match in re.finditer(attr, html, flags=re.I):
            value = match.group(1).strip()
            if value and key == "value":
                fields.setdefault("value", value)

    for key in list(fields.keys()):
        lower = key.lower()
        if lower in {"user_name", "username", "user"}:
            fields[key] = ROUTER_USERNAME
        elif lower in {"loginpp", "password", "pwd", "pass", "passwd"}:
            fields[key] = ROUTER_PASSWORD
        elif lower in {"submit", "button", "login", "lang", "remember"}:
            fields[key] = ""
    if "user_name" not in fields and "username" not in fields:
        fields["user_name"] = ROUTER_USERNAME
    if "loginpp" not in fields and "password" not in fields and "pass" not in fields:
        fields["loginpp"] = ROUTER_PASSWORD
    return fields


def _router_login_via_requests(session: requests.Session, base_url: str) -> bool:
    """Try to authenticate to the modem using a persistent requests session and cookies."""
    login_candidates = [
        f"{base_url}/html/login_inter.html",
        f"{base_url}/cgi-bin/luci",
        f"{base_url}/cgi-bin/webs",
        f"{base_url}/cgi-bin/luci/;stok=/admin/login/",
        f"{base_url}/login.cgi",
    ]
    for login_url in login_candidates:
        try:
            resp = session.get(login_url, timeout=8, allow_redirects=True)
            text = resp.text or ""
            if not text:
                continue
            payload = _collect_router_form_fields(text)
            if not payload:
                continue

            post_candidates = [login_url]
            if "login_inter.html" in login_url.lower():
                post_candidates.extend([
                    f"{base_url}/cgi-bin/luci",
                    f"{base_url}/cgi-bin/webs",
                    f"{base_url}/cgi-bin/luci/;stok=/admin/login/",
                    f"{base_url}/login.cgi",
                ])

            for post_url in post_candidates:
                try:
                    post_resp = session.post(post_url, data=payload, timeout=8, allow_redirects=True)
                    post_text = post_resp.text or ""
                    if post_text and ("login" not in post_text.lower() or "user_name" not in post_text.lower()):
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def debug_router_access() -> dict:
    """Return a compact debug report for the router login and DHCP page state."""
    debug = {
        "router_ip": ROUTER_IP,
        "username": ROUTER_USERNAME,
        "password_set": bool(ROUTER_PASSWORD),
        "http": {},
        "browser": {},
    }

    try:
        r = requests.get(f"http://{ROUTER_IP}/html/login_inter.html", timeout=10, allow_redirects=True)
        debug["http"] = {
            "status_code": r.status_code,
            "ok": r.ok,
            "final_url": r.url,
            "snippet": (r.text or "")[:300].replace("\n", " "),
        }
    except Exception as exc:
        debug["http"] = {"error": str(exc)}

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"http://{ROUTER_IP}/html/login_inter.html", wait_until="domcontentloaded", timeout=20000)
            debug["browser"] = {
                "url": page.url,
                "title": page.title(),
                "user_name_fields": page.locator('input[name="user_name"]').count(),
                "username_fields": page.locator('input[name="username"]').count(),
                "loginpp_fields": page.locator('input[name="loginpp"]').count(),
                "password_fields": page.locator('input[name="password"]').count(),
                "body_snippet": (page.locator("body").inner_text() or "")[:300],
            }
            browser.close()
    except Exception as exc:
        debug["browser"] = {"error": str(exc)}

    return debug


def _extract_router_login_payload(html: str) -> dict:
    """Extract login fields from the router’s custom HTML and map them to the expected username/password values."""
    if not html:
        return {}

    payload = {}
    for match in re.finditer(r'name\s*=\s*["\']([^"\']+)["\']', html, flags=re.I):
        name = match.group(1).strip()
        if name:
            payload.setdefault(name, "")

    for name in list(payload.keys()):
        lower = str(name).lower()
        if any(token in lower for token in ["user", "login_name", "admin", "userid"]) and not any(token in lower for token in ["pass", "pwd", "secret"]):
            payload[name] = ROUTER_USERNAME
        elif any(token in lower for token in ["pass", "pwd", "passwd", "loginpp", "password", "secret", "key"]):
            payload[name] = ROUTER_PASSWORD
        elif any(token in lower for token in ["submit", "button", "ok", "login", "lang", "remember", "captcha"]):
            payload[name] = payload.get(name) or ""

    if not payload:
        for key in ["user_name", "username", "user", "login_name", "userid"]:
            payload[key] = ROUTER_USERNAME
        for key in ["loginpp", "password", "pass", "passwd", "pwd"]:
            payload[key] = ROUTER_PASSWORD

    if "user_name" not in payload and "username" not in payload and "user" not in payload:
        payload["user_name"] = ROUTER_USERNAME
    if "loginpp" not in payload and "password" not in payload and "pass" not in payload and "passwd" not in payload and "pwd" not in payload:
        payload["loginpp"] = ROUTER_PASSWORD

    return payload


def _parse_router_rows_from_html(html: str, label: str = "DHCP Client List") -> list:
    if not html:
        return []

    rows = []
    seen = set()

    if "#user_list" in html or "user_list" in html.lower() or "dhcp_userListHead" in html:
        for tr_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, flags=re.I | re.S):
            tr_html = tr_match.group(1)
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr_html, flags=re.I | re.S)
            if len(cells) < 6:
                continue
            cleaned = []
            for cell in cells:
                text = re.sub(r"<[^>]+>", " ", cell, flags=re.S)
                text = re.sub(r"\s+", " ", text).strip()
                cleaned.append(text)
            if not cleaned:
                continue
            hostname = cleaned[1].strip() if len(cleaned) > 1 else "Unknown"
            mac = cleaned[2].strip() if len(cleaned) > 2 else ""
            ip_value = cleaned[3].strip() if len(cleaned) > 3 else ""
            lease_time = cleaned[4].strip() if len(cleaned) > 4 else ""
            type_value = cleaned[5].strip() if len(cleaned) > 5 else ""
            if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", ip_value):
                continue
            key = (hostname, ip_value, mac)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "hostname": hostname or "Unknown",
                "ip": ip_value,
                "mac": mac,
                "lease_time": lease_time,
                "gateway": ROUTER_IP,
                "source_type": "Router DHCP",
                "source": "DHCP Client List",
                "status": "online",
                "router_link_type": type_value,
            })
        if rows:
            return rows

    table_rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", html, flags=re.I | re.S)
    for tr_html in table_rows:
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", tr_html, flags=re.I | re.S)
        if not cells:
            continue

        cleaned = []
        for cell in cells:
            text = re.sub(r"<[^>]+>", " ", cell, flags=re.S)
            text = re.sub(r"\s+", " ", text)
            cleaned.append(text.strip())

        row_text = " | ".join(cleaned)
        ip_matches = re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", row_text)
        if not ip_matches:
            continue

        ip_value = ip_matches[0]
        hostname = "Unknown"
        mac = ""
        lease_time = ""
        type_value = ""
        seen_values = set()

        for cell_text in cleaned:
            if not cell_text:
                continue
            if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", cell_text):
                continue
            if re.search(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", cell_text):
                mac = cell_text
                continue
            if re.fullmatch(r"\d+", cell_text) and len(cell_text) >= 3:
                continue
            if any(token in cell_text.lower() for token in ["dynamic", "static", "wireless", "lan", "wifi"]):
                type_value = cell_text
                continue
            if not re.fullmatch(r"[0-9A-Fa-f:\-]{8,}", cell_text):
                if cell_text not in seen_values:
                    hostname = cell_text
                    seen_values.add(cell_text)
                    break

        if cleaned and len(cleaned) >= 5:
            if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", cleaned[3]):
                ip_value = cleaned[3]
            if len(cleaned) >= 5 and cleaned[4] and re.fullmatch(r"\d+", cleaned[4]):
                lease_time = cleaned[4]
            if len(cleaned) >= 6 and cleaned[5] and cleaned[5].strip():
                type_value = cleaned[5].strip()
            if len(cleaned) >= 2 and cleaned[1].strip():
                hostname = cleaned[1].strip()
            if len(cleaned) >= 3 and re.search(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", cleaned[2]):
                mac = cleaned[2].strip()

        if hostname in {"Unknown", ""} and cleaned and len(cleaned) > 1 and cleaned[1].strip():
            hostname = cleaned[1].strip()

        key = (hostname, ip_value, mac)
        if key in seen:
            continue
        seen.add(key)

        rows.append({
            "hostname": hostname or "Unknown",
            "ip": ip_value,
            "mac": mac,
            "lease_time": lease_time or "",
            "gateway": ROUTER_IP,
            "source_type": "Router DHCP",
            "source": "DHCP Client List",
            "status": "online",
            "router_link_type": type_value,
        })

    if rows:
        return rows

    generic_ip_matches = re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", html)
    uniq = []
    for ip_value in generic_ip_matches:
        if ip_value not in uniq:
            uniq.append(ip_value)
    for ip_value in uniq[:25]:
        if ip_value.startswith("0.") or ip_value.startswith("127."):
            continue
        rows.append({
            "hostname": "Unknown",
            "ip": ip_value,
            "mac": "",
            "lease_time": "",
            "gateway": ROUTER_IP,
            "source_type": "Router DHCP",
            "source": "DHCP Client List",
            "status": "online",
        })

    return rows


@st.cache_data(ttl=45)
def cached_router_dhcp_clients() -> list:
    return fetch_router_dhcp_clients()


def fetch_router_dhcp_clients() -> list:
    """Fetch the live DHCP client list directly from the modem/router, with a short cache to avoid repeated expensive logins."""
    _router_config()
    base_url = f"http://{ROUTER_IP}"

    browser_rows = _router_dhcp_list_via_browser()
    if browser_rows:
        return browser_rows

    session = requests.Session()
    try:
        login_ok = _router_login_via_requests(session, base_url)
        if login_ok:
            dhcp_url = f"{base_url}/html/dhcp_user_list_inter.html"
            try:
                resp = session.get(dhcp_url, timeout=10, allow_redirects=True)
                html = resp.text or ""
                rows = _parse_router_rows_from_html(html)
                if rows:
                    return rows
            except Exception:
                pass
    except Exception:
        pass

    try:
        raw = requests.get(f"{base_url}/html/dhcp_user_list_inter.html", timeout=10, allow_redirects=True)
        html = raw.text or ""
        if html:
            rows = _parse_router_rows_from_html(html)
            if rows:
                return rows
    except Exception:
        pass

    return []


def _router_dhcp_list_via_browser() -> list:
    """Use Playwright to log in to the modem and read DHCP client rows from the router page."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(25000)

            login_url = f"http://{ROUTER_IP}/html/login_inter.html"
            page.goto(login_url, wait_until="domcontentloaded", timeout=20000)

            payload = _extract_router_login_payload(page.content() or "")
            if payload:
                for key, value in payload.items():
                    if value in {ROUTER_USERNAME, ROUTER_PASSWORD}:
                        selector = f'input[name="{key}"]'
                        if page.locator(selector).count() > 0:
                            page.fill(selector, str(value))

            for selector in [
                'input[type="submit"]',
                'input[value*="Login"]',
                'input[value*="login"]',
                '#login_btn',
                '#login_btn_mar',
                'button[type="submit"]',
                'button',
            ]:
                loc = page.locator(selector)
                if loc.count() > 0:
                    try:
                        loc.first.click()
                        break
                    except Exception:
                        pass

            page.wait_for_timeout(3000)
            page.goto(f"http://{ROUTER_IP}/html/dhcp_user_list_inter.html", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)
            html = page.content() or ""
            browser.close()
            return _parse_router_rows_from_html(html)
    except Exception:
        return []


def render_dashboard():
    try:
        st.set_page_config(page_title="DKT Monitoring System", page_icon="", layout="wide")
        st.markdown(
            """
            <style>
            .stApp [data-testid='stToolbar'] { display: none; }
            .stApp [data-testid='stDecoration'] { display: none; }
            .stApp [data-testid='stHeader'] { background: rgba(0,0,0,0); }
            .stApp [data-testid='stStatusWidget'] { display: none; }
            .stRadio > div {
                transition: none !important;
                filter: none !important;
            }
            div[data-testid='stDataFrame'] .stButton > button {
                background: #2563eb;
                color: white;
                border-radius: 8px;
                border: none;
                padding: 0.35rem 0.8rem;
                font-weight: 600;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        st.title("DKT Monitoring System")
        st.caption("Status aktif dihitung otomatis: jika window aktif berisi 'Offline / no heartbeat', maka client otomatis menjadi Offline.")
        st.info("Memuat data dashboard…")

        with st.spinner("Mengambil data client dan jaringan…"):
            try:
                clients = build_known_clients()
            except Exception as exc:
                st.error("Gagal memuat data client dari file heartbeat atau log lokal.")
                st.exception(exc)
                return
        rows = [normalize_client(record, key) for key, record in clients.items()]

        menu = st.radio("Menu", ["Dashboard", "Rename", "Network", "Command", "SERVER", "SATUSEHAT", "BPJS", "TTE"], horizontal=True, index=0, key="main_menu")

        if menu == "Rename":
            render_rename_section(rows)
            return

        if menu == "Network":
            st.markdown("### Router debug")
            if st.button("Debug router access"):
                try:
                    debug = debug_router_access()
                    st.json(debug)
                except Exception as exc:
                    st.exception(exc)
            render_network_section()
            return

        if menu == "Command":
            render_command_section(rows)
            return

        if menu == "SERVER":
            render_development_module("SERVER")
            return

        if menu == "SATUSEHAT":
            render_development_module("SATUSEHAT")
            return

        if menu == "BPJS":
            render_development_module("BPJS")
            return

        if menu == "TTE":
            render_development_module("TTE")
            return

        if not clients:
            st.warning("Belum ada client yang terdeteksi. Silakan tunggu heartbeat masuk atau cek file log.")
            st.markdown(
                """
                <div class="card">
                    <h3 style="margin:0; color:#0f172a;">Belum ada client online</h3>
                    <p style="margin-top:8px; color:#475569;">Dashboard siap menampilkan data saat client mulai mengirim heartbeat.</p>
                </div>
                """,
                unsafe_allow_html=True,
            )
            return

        st.markdown("### Client status")
        if not rows:
            st.info("Tidak ada data client yang valid.")
            return

        recent_log_clients = get_recent_heartbeat_hosts()
        online_clients = sum(1 for row in rows if row.get("status") == "online")
        total_clients = len(rows)
        offline_clients = sum(1 for row in rows if row.get("status") == "offline")

        summary_cols = st.columns(3)
        cards = [
            ("Total Client", total_clients, "#2563eb"),
            ("Online", online_clients, "#16a34a"),
            ("Offline", offline_clients, "#ef4444"),
        ]
        for idx, (label, value, color) in enumerate(cards):
            with summary_cols[idx]:
                st.markdown(
                    f"""
                    <div style="background:{color}; border-radius:12px; padding:14px 18px; color:white; min-height:92px;">
                        <div style="font-size:12px; opacity:0.9;">{label}</div>
                        <div style="font-size:30px; font-weight:700; margin-top:6px;">{value}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

        st.write("")

        refresh_col, mask_col, _ = st.columns([1, 1.5, 5])
        with refresh_col:
            if st.button("Refresh"):
                st.cache_data.clear()
                st.rerun()
        with mask_col:
            st.session_state.setdefault("dashboard_ip_mask", False)
            st.checkbox("Sembunyikan 5 digit IP", key="dashboard_ip_mask", value=st.session_state.get("dashboard_ip_mask", False))

        st.write("")

        df = pd.DataFrame(rows)
        df["status"] = df["status"].map({"online": "Online", "offline": "Offline"})
        df["cpu_percent"] = df["cpu_percent"].apply(lambda value: format_percent(value))
        df["ram_percent"] = df["ram_percent"].apply(lambda value: format_percent(value))
        df["gateway"] = df["gateway"].apply(lambda value: normalize_ipv4(value, DEFAULT_GATEWAY or "192.168.9.1"))
        df["ip_address"] = df["ip_address"].apply(lambda value: mask_ip_for_display(value, enabled=st.session_state.get("dashboard_ip_mask", False)))
        df["gateway"] = df["gateway"].apply(lambda value: mask_ip_for_display(value, enabled=st.session_state.get("dashboard_ip_mask", False)))
        df["speaker_connected"] = df["speaker_connected"].map({True: "Ya", False: "Tidak"})
        df = df.rename(columns={
            "display_name": "Nama",
            "hostname": "Hostname",
            "ip_address": "IP",
            "status": "Status",
            "last_seen": "Terakhir aktif",
            "os": "OS",
            "cpu_percent": "CPU %",
            "ram_percent": "RAM %",
            "gateway": "Gateway",
            "interface_name": "Interface",
            "audio_status": "Audio",
            "speaker_connected": "Speaker",
            "active_window": "Window aktif",
        })
        display_df = df[["Nama", "Hostname", "IP", "Status", "Terakhir aktif", "OS", "CPU %", "RAM %", "Gateway", "Interface", "Audio", "Speaker", "Window aktif"]]
        styled_df = display_df.style.map(style_status_value, subset=["Status"])
        table_height = max(420, min(1200, len(display_df) * 32 + 90))
        st.dataframe(styled_df, use_container_width=True, hide_index=True, height=table_height)

        select_options = [ item["display_name"] for item in rows]
        selected_display_name = st.selectbox("Pilih client untuk detail", select_options, index=0 if select_options else 0, key="client_detail_select")
        selected = next((item for item in rows if item["display_name"] == selected_display_name), rows[0])
        selected_hostname = selected["hostname"]

        st.write("")
        if st.button("Force heartbeat sekarang"):
            issue_force_heartbeat(selected_hostname)

        st.markdown("### Detail client")
        left, right = st.columns(2)
        with left:
            st.markdown(f"**Nama**: {selected.get('display_name', selected.get('hostname'))}")
            st.markdown(f"**Hostname**: {selected.get('hostname')}")
            st.markdown(f"**IP**: {mask_ip_for_display(selected.get('ip_address'), enabled=st.session_state.get('dashboard_ip_mask', False))}")
            st.markdown(f"**OS**: {selected.get('os')}")
            st.markdown(f"**Status**: {status_badge_html(selected.get('status'))}", unsafe_allow_html=True)
            st.markdown(f"**Terakhir aktif**: {selected.get('last_seen')}")
            st.markdown(f"**Offline sejak**: {get_offline_since(selected, selected.get('hostname', ''))}")
            st.markdown(f"**CPU**: {selected.get('cpu_percent')}%")
            st.markdown(f"**RAM**: {selected.get('ram_percent')}%")

        with right:
            network = selected.get("network") or {}
            gateway_value = normalize_ipv4(network.get("gateway") or DEFAULT_GATEWAY or "192.168.9.1", DEFAULT_GATEWAY or "192.168.9.1")
            st.markdown("**Network**")
            st.json({
                "gateway": mask_ip_for_display(gateway_value, enabled=st.session_state.get("dashboard_ip_mask", False)),
                "interface_name": network.get("interface_name", "Unknown"),
                "interface_type": network.get("interface_type", "LAN"),
                "gateway_reachable": network.get("gateway_reachable", False),
                "critical_ips": [mask_ip_for_display(item, enabled=st.session_state.get("dashboard_ip_mask", False)) for item in network.get("critical_ips", [])],
            })

            speaker = selected.get("speaker") or {}
            st.markdown("**Audio / speaker**")
            st.json({
                "speaker_connected": speaker.get("speaker_connected", False),
                "audio_status": speaker.get("audio_status", "Disconnected"),
                "connection_type": speaker.get("connection_type", "Unknown"),
                "volume": speaker.get("volume", 0),
                "muted": speaker.get("muted", False),
                "device_name": speaker.get("device_name", "N/A"),
            })

        heartbeat_info = find_last_heartbeat(selected_hostname)
        if heartbeat_info:
            st.markdown("### Heatbeat terakhir")
            st.info(f"Terakhir diterima: {heartbeat_info.get('timestamp')} | IP: {mask_ip_for_display(heartbeat_info.get('ip'), enabled=st.session_state.get('dashboard_ip_mask', False))}")
        else:
            st.markdown("### Heatbeat terakhir")
            st.warning(f"Belum ada heartbeat baru yang diterima untuk {selected_hostname}.")

        st.markdown("### Window aktif")
        st.code(selected.get("active_window", "-"), language="text")

        open_windows = selected.get("open_windows") or []
        if open_windows:
            st.markdown("### Daftar window terbuka")
            for item in open_windows[:12]:
                st.write(f"- {item}")

        st.markdown("### Informasi perangkat terhubung")
        device_summary = connected_devices_summary(selected)
        dev_left, dev_right = st.columns(2)
        with dev_left:
            st.markdown("**Audio / speaker**")
            st.json(device_summary["audio"])
        with dev_right:
            st.markdown("**Network / koneksi**")
            st.json(device_summary["network"])

        st.markdown("### Daftar perangkat LAN yang terdeteksi")
        lan_devices = get_connected_lan_devices_for_client(selected)
        if lan_devices:
            lan_df = pd.DataFrame(lan_devices)
            lan_df = lan_df[["hostname", "ip", "gateway", "source_type", "source", "status"]]
            lan_df = lan_df.rename(columns={
                "hostname": "Hostname",
                "ip": "IP",
                "gateway": "Gateway",
                "source_type": "Tipe",
                "source": "Sumber",
                "status": "Status",
            })
            st.dataframe(lan_df, use_container_width=True, hide_index=True)
        else:
            st.info("Belum ada perangkat yang terdeteksi pada jaringan lokal untuk client ini.")

        st.markdown("### Pilih default audio & network device")
        audio_devices = device_summary["audio"].get("available_devices", [])
        network_adapters = device_summary["network"].get("available_adapters", [])

        audio_options = [item.get("name", "Unknown") for item in audio_devices if isinstance(item, dict)]
        network_options = [item.get("name", "Unknown") for item in network_adapters if isinstance(item, dict)]

        if audio_options:
            selected_audio = st.selectbox("Default Audio Device", audio_options)
            if st.button("Set default audio"):
                issue_device_preference(selected_hostname, "audio", selected_audio)
        else:
            st.info("Tidak ada device audio yang terdeteksi.")

        if network_options:
            selected_network = st.selectbox("Default Network Adapter", network_options)
            if st.button("Set default network"):
                issue_device_preference(selected_hostname, "network", selected_network)
        else:
            st.info("Tidak ada adapter jaringan yang terdeteksi.")

        st.caption(f"Pembaruan terakhir: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as exc:
        st.error("Dashboard gagal dimuat karena terjadi error pada sisi aplikasi.")
        st.info("Silakan refresh halaman. Jika terus terjadi, cek log server atau jalankan aplikasi dalam mode terminal untuk melihat traceback.")
        st.exception(exc)


def get_connection_seen_timestamp(hostname: str, fallback: str = "") -> str:
    hb = find_last_heartbeat(str(hostname or "").upper())
    if hb and hb.get("timestamp"):
        return hb.get("timestamp")
    if fallback and str(fallback).strip() not in {"", "-", "None"}:
        return str(fallback)
    return "Tidak diketahui"


KNOWN_ROUTER_IPS = {
    "10.10.10.10",
    "10.10.10.45",
    "10.10.10.46",
    "10.10.10.56",
    "10.10.10.61",
    "10.10.10.139",
    "10.10.10.165",
}


def is_router_ap_device(hostname: str, ip_text: str = "", source_type: str = "", device_type: str = "") -> bool:
    ip_value = str(ip_text or "").strip()
    if ip_value in KNOWN_ROUTER_IPS:
        return True
    return False


def render_development_module(title: str):
    st.title(title)
    st.markdown(
        f"""
        <div style='background:#fff7ed; border:1px solid #fdba74; border-radius:14px; padding:16px 18px; margin-bottom:18px; color:#9a4d00; font-weight:600;'>
            Sedang dalam tahap pengembangan. Fitur <b>{title}</b> masih belum tersedia untuk penggunaan produksi.
        </div>
        """,
        unsafe_allow_html=True,
    )

    card_data = [
        ("Status", "Belum aktif", "#2563eb"),
        ("Integrasi", "Dalam proses", "#16a34a"),
        ("Keamanan", "Menunggu validasi", "#f59e0b"),
        ("Operasional", "On progress", "#7c3aed"),
    ]

    cols = st.columns(4)
    for col_idx, (label, value, color) in enumerate(card_data):
        with cols[col_idx]:
            st.markdown(
                f"""
                <div style='background:linear-gradient(135deg, {color} 0%, rgba(255,255,255,0.15) 100%); border-radius:16px; padding:18px 16px; min-height:130px; color:white; box-shadow:0 10px 24px rgba(15,23,42,0.08);'>
                    <div style='font-size:11px; opacity:0.85; letter-spacing:0.12em; text-transform:uppercase;'>{label}</div>
                    <div style='font-size:24px; font-weight:700; margin-top:12px; line-height:1.2;'>{value}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.write("")
    st.warning(f"Fitur {title} sedang dalam tahap pengembangan. Modul ini akan segera ditambahkan dan disesuaikan dengan kebutuhan operasional.")
    st.info("Mohon menunggu update berikutnya. Saat ini halaman ini hanya berfungsi sebagai placeholder untuk persiapan pengembangan modul.")