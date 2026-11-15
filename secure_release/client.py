import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import psutil
import requests

try:
    import win32gui
except Exception:  # pragma: no cover
    win32gui = None

try:
    from pycaw.pycaw import AudioUtilities
except Exception:  # pragma: no cover
    AudioUtilities = None

CLIENT_LOG_FILE = os.path.join(os.path.dirname(__file__), "client_debug.log")
SERVER_URL = os.getenv("SERVER_URL", os.getenv("API_BASE_URL", "http://10.10.10.21:5000/api/heartbeat"))
SECRET_TOKEN = os.getenv("CLIENT_SECRET_TOKEN") or "REPLACE_WITH_STRONG_TOKEN"
HEARTBEAT_INTERVAL_SECONDS = 25
RETRY_INTERVAL_SECONDS = 10
CRITICAL_IPS = ["10.10.10.68", "10.10.10.9", "10.10.10.1", "10.10.10.21"]
UPDATE_CHECK_INTERVAL_SECONDS = 300
CLIENT_VERSION = "1.0.0"
LOCAL_CLIENT_FILE = os.path.abspath(__file__)
PREFERRED_DEVICE_FILE = os.path.join(os.path.dirname(__file__), "client_preferences.json")


def log_client(message: str):
    try:
        with open(CLIENT_LOG_FILE, "a", encoding="utf-8") as file:
            file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def parse_cli_overrides() -> Dict[str, str]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--server-url", dest="server_url")
    parser.add_argument("--token", dest="token")
    parser.add_argument("--hostname", dest="hostname")
    parser.add_argument("--ip", dest="ip_address")
    known, _ = parser.parse_known_args()
    result = {}
    if known.server_url:
        result["server_url"] = known.server_url
    if known.token:
        result["token"] = known.token
    if known.hostname:
        result["hostname"] = known.hostname
    if known.ip_address:
        result["ip_address"] = known.ip_address
    return result


def resolve_runtime_values() -> Dict[str, str]:
    overrides = parse_cli_overrides()
    server_url = str(overrides.get("server_url") or os.getenv("SERVER_URL") or os.getenv("API_BASE_URL") or SERVER_URL).strip()
    token = str(overrides.get("token") or os.getenv("CLIENT_SECRET_TOKEN") or SECRET_TOKEN).strip()
    return {"server_url": server_url, "token": token}


def _windows_startupinfo() -> Optional[object]:
    if os.name != "nt":
        return None
    try:
        import win32process
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = 0
        return startup_info
    except Exception:
        return None


def _run_hidden_command(args: List[str], capture: bool = True) -> subprocess.CompletedProcess:
    startup_info = _windows_startupinfo()
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    if capture:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            shell=False,
            startupinfo=startup_info,
            creationflags=creationflags,
            check=False,
        )

    return subprocess.run(
        args,
        shell=False,
        startupinfo=startup_info,
        creationflags=creationflags,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "UNKNOWN-PC"


def get_local_ip() -> str:
    try:
        interfaces = psutil.net_if_addrs()
        for _, addresses in interfaces.items():
            for address in addresses:
                if getattr(address, "family", None) == socket.AF_INET:
                    ip = address.address
                    if ip and ip != "127.0.0.1" and not ip.startswith("169.254."):
                        return ip
        return "127.0.0.1"
    except Exception:
        return "127.0.0.1"


def get_active_interface_name() -> str:
    local_ip = get_local_ip()
    try:
        result = _run_hidden_command(["ipconfig"])
        output = result.stdout or ""
        current_name = None
        for line in output.splitlines():
            text = line.strip()
            if text.endswith(":") and ("Ethernet adapter" in text or "Wireless LAN adapter" in text or "Wi-Fi" in text):
                current_name = text[:-1]
            if "IPv4 Address" in line and local_ip in line:
                if current_name:
                    return current_name
        return "Unknown"
    except Exception:
        return "Unknown"


def get_default_gateway() -> str:
    try:
        result = _run_hidden_command(["ipconfig"])
        output = result.stdout or ""
        for line in output.splitlines():
            if "Default Gateway" in line and ":" in line:
                value = line.split(":", 1)[-1].strip()
                if value:
                    return value
        return "0.0.0.0"
    except Exception:
        return "0.0.0.0"


def detect_interface_type() -> str:
    try:
        result = _run_hidden_command(["netsh", "wlan", "show", "interfaces"])
        output = result.stdout or ""
        if "SSID" in output or "BSSID" in output:
            return "WiFi"
    except Exception:
        pass
    return "LAN"


def ping_ip(ip: str, timeout_ms: int = 500) -> bool:
    if not ip or ip == "0.0.0.0":
        return False
    try:
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
        result = _run_hidden_command(cmd, capture=False)
        return result.returncode == 0
    except Exception:
        return False


def get_active_window_title() -> str:
    if win32gui is None:
        return "N/A (non-Windows)"
    try:
        hwnd = win32gui.GetForegroundWindow()
        if hwnd:
            title = win32gui.GetWindowText(hwnd)
            if title:
                return title.strip()
        return "Desktop / Tidak ada judul"
    except Exception:
        return "Unknown"


def get_open_windows(limit: int = 12) -> List[str]:
    if win32gui is None:
        return []
    windows: List[str] = []
    try:
        def enum_windows(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title and title.strip():
                    windows.append(title.strip())
        win32gui.EnumWindows(enum_windows, None)
    except Exception:
        return []

    unique = []
    seen = set()
    for title in windows:
        cleaned = title.strip()
        if not cleaned:
            continue
        if cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        unique.append(cleaned)

    return unique[:limit]


def get_system_metrics() -> Dict[str, float]:
    try:
        cpu = psutil.cpu_percent(interval=0.2)
    except Exception:
        cpu = 0.0

    try:
        ram = psutil.virtual_memory().percent
    except Exception:
        ram = 0.0

    return {"cpu_percent": float(cpu), "ram_percent": float(ram)}


def parse_audio_name(device_name: str) -> str:
    lowered = (device_name or "").lower()
    if "bluetooth" in lowered:
        return "Bluetooth"
    if "jack" in lowered or "line out" in lowered or "aux" in lowered or "headphone" in lowered:
        return "AUX/Jack"
    return "Default Audio"


def get_audio_status() -> Dict[str, object]:
    result = {
        "speaker_connected": False,
        "audio_status": "Disconnected",
        "connection_type": "Unknown",
        "volume": 0,
        "muted": False,
        "device_name": "N/A",
    }

    if AudioUtilities is None:
        return result

    try:
        speakers = AudioUtilities.GetSpeakers()
        if speakers is not None:
            result["speaker_connected"] = True
            result["audio_status"] = "Connected"
            device_name = getattr(speakers, "FriendlyName", "") or "Default Speaker"
            result["device_name"] = str(device_name)
            result["connection_type"] = parse_audio_name(str(device_name))

            try:
                volume = speakers.SimpleAudioVolume
                if volume is not None:
                    result["muted"] = bool(volume.GetMute())
                    result["volume"] = int(round(volume.GetMasterVolumeLevelScalar() * 100))
            except Exception:
                result["volume"] = 0
    except Exception:
        result["speaker_connected"] = False
        result["audio_status"] = "Disconnected"
        result["connection_type"] = "Unknown"
        result["volume"] = 0
        result["muted"] = False
        result["device_name"] = "N/A"

    return result


def get_uptime_seconds() -> int:
    try:
        return int(time.time() - psutil.boot_time())
    except Exception:
        return 0


def get_connected_devices() -> List[Dict[str, object]]:
    devices: List[Dict[str, object]] = []
    try:
        net = psutil.net_connections(kind='inet')
        for item in net:
            if item.status in ('ESTABLISHED', 'LISTEN'):
                devices.append({
                    "local": str(item.laddr) if getattr(item, 'laddr', None) else "",
                    "remote": str(item.raddr) if getattr(item, 'raddr', None) else "",
                    "status": str(item.status),
                    "pid": getattr(item, 'pid', None),
                })
    except Exception:
        pass
    return devices[:20]


def apply_preferred_device(command: str) -> str:
    try:
        if command.startswith("SET_PREFERRED_AUDIO:"):
            value = command.split(":", 1)[1].strip()
            path = os.path.join(os.path.dirname(__file__), "client_preferences.json")
            prefs = {}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as file:
                        prefs = json.load(file)
                except Exception:
                    prefs = {}
            prefs["preferred_audio"] = value
            with open(path, "w", encoding="utf-8") as file:
                json.dump(prefs, file, ensure_ascii=False, indent=2)
            return f"Audio preference saved: {value}"

        if command.startswith("SET_PREFERRED_NETWORK:"):
            value = command.split(":", 1)[1].strip()
            path = os.path.join(os.path.dirname(__file__), "client_preferences.json")
            prefs = {}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as file:
                        prefs = json.load(file)
                except Exception:
                    prefs = {}
            prefs["preferred_network"] = value
            with open(path, "w", encoding="utf-8") as file:
                json.dump(prefs, file, ensure_ascii=False, indent=2)
            return f"Network preference saved: {value}"

        return "Unsupported preference command"
    except Exception as exc:
        return f"error: {exc}"


def send_heartbeat(payload: Dict[str, object]) -> bool:
    headers = {
        "Content-Type": "application/json",
        "X-Token": SECRET_TOKEN,
    }
    try:
        response = requests.post(
            SERVER_URL,
            data=json.dumps(payload, ensure_ascii=False),
            headers=headers,
            timeout=5,
        )
        log_client(f"heartbeat_attempt status={response.status_code} url={SERVER_URL} hostname={payload.get('hostname')} ip={payload.get('ip_address')}")
        if response.status_code == 200:
            return True
        log_client(f"heartbeat_rejected status={response.status_code} body={response.text[:300]}")
        return False
    except requests.RequestException as exc:
        log_client(f"heartbeat_exception err={exc} url={SERVER_URL}")
        return False


def send_command_ack(hostname: str, command: str) -> bool:
    headers = {
        "Content-Type": "application/json",
        "X-Token": SECRET_TOKEN,
    }
    payload = {"hostname": hostname, "command": command}
    try:
        response = requests.post(
            f"{SERVER_URL.replace('/api/heartbeat', '/api/command/ack')}",
            data=json.dumps(payload, ensure_ascii=False),
            headers=headers,
            timeout=5,
        )
        return response.status_code == 200
    except requests.RequestException:
        return False


def send_command_result(hostname: str, command: str, status: str, output: str = "") -> bool:
    headers = {
        "Content-Type": "application/json",
        "X-Token": SECRET_TOKEN,
    }
    payload = {
        "hostname": hostname,
        "command": command,
        "status": status,
        "output": output,
    }
    try:
        response = requests.post(
            SERVER_URL.replace('/api/heartbeat', '/api/command/result'),
            data=json.dumps(payload, ensure_ascii=False),
            headers=headers,
            timeout=5,
        )
        return response.status_code == 200
    except requests.RequestException:
        return False


def execute_local_command(command: str) -> str:
    if not command or not command.strip():
        return "empty command"

    try:
        if os.name == 'nt':
            completed = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            out = (completed.stdout or "") + (completed.stderr or "")
            return str(out).strip() or f"exit_code={completed.returncode}"

        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
        )
        out = (completed.stdout or "") + (completed.stderr or "")
        return str(out).strip() or f"exit_code={completed.returncode}"
    except Exception as exc:
        return f"error: {exc}"


def poll_force_heartbeat() -> None:
    try:
        response = requests.get(
            SERVER_URL.replace('/api/heartbeat', '/api/commands'),
            headers={"X-Token": SECRET_TOKEN},
            timeout=5,
        )
        if response.status_code != 200:
            return
        data = response.json() if hasattr(response, 'json') else {}
        commands = data.get('commands', []) if isinstance(data, dict) else []
        if not isinstance(commands, list):
            return
        for item in commands:
            if isinstance(item, dict) and str(item.get('command', '')).lower() == 'force_heartbeat':
                hostname = str(item.get('hostname', '')).strip()
                if hostname:
                    send_heartbeat(build_payload())
                    send_command_ack(hostname, 'force_heartbeat')
    except Exception:
        pass


def poll_commands() -> None:
    try:
        response = requests.get(
            SERVER_URL.replace('/api/heartbeat', '/api/commands'),
            headers={"X-Token": SECRET_TOKEN},
            timeout=5,
        )
        if response.status_code != 200:
            return

        data = response.json() if hasattr(response, 'json') else {}
        commands = data.get('commands', []) if isinstance(data, dict) else []
        if not isinstance(commands, list):
            return

        hostname = get_hostname()
        for item in commands:
            if not isinstance(item, dict):
                continue
            if str(item.get('hostname', '')).strip().lower() != hostname.lower():
                continue
            command = str(item.get('command', '')).strip()
            if not command:
                continue
            if str(item.get('status', '')).strip().lower() == 'done':
                continue

            if command.startswith('SET_PREFERRED_AUDIO:') or command.startswith('SET_PREFERRED_NETWORK:'):
                result = apply_preferred_device(command)
                send_command_result(hostname, command, 'executed', result)
                continue

            result = execute_local_command(command)
            if result and 'error:' not in result.lower():
                send_command_result(hostname, command, 'executed', result)
            else:
                send_command_result(hostname, command, 'failed', result)
    except Exception:
        pass


def ensure_single_instance() -> None:
    if os.name != "nt":
        return
    try:
        import win32event
        import win32api

        mutex_name = "HospitalClientMonitoring_{0}".format(get_hostname())
        mutex = win32event.CreateMutex(None, False, mutex_name)
        if win32api.GetLastError() != 0:
            sys.exit(0)
    except Exception:
        pass


def run_once() -> None:
    try:
        refresh_runtime_config()
        payload = build_payload()
        ok = send_heartbeat(payload)
        log_client(f"run_once_heartbeat ok={ok} hostname={payload.get('hostname')} ip={payload.get('ip_address')}")
        return ok
    except Exception as exc:
        log_client(f"run_once_exception err={exc}")
        return False


def main() -> None:
    ensure_single_instance()
    refresh_runtime_config()
    log_client(f"client_started server={SERVER_URL} token={SECRET_TOKEN}")
    while True:
        try:
            poll_update_check()
            poll_commands()
            refresh_runtime_config()
            payload = build_payload()
            send_heartbeat(payload)
        except Exception as exc:
            log_client(f"main_loop_exception err={exc}")
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
