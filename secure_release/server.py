import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pandas as pd
import streamlit as st

DATA_FILE = os.path.join(os.path.dirname(__file__), "clients_data.json")
COMMANDS_FILE = os.path.join(os.path.dirname(__file__), "client_commands.json")
SECRET_TOKEN = os.getenv("CLIENT_SECRET_TOKEN") or "REPLACE_WITH_STRONG_TOKEN"
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "5000"))
STREAMLIT_PORT = int(os.getenv("STREAMLIT_PORT", "8501"))
HEARTBEAT_TIMEOUT_SECONDS = 120
UPDATE_DIR = os.path.join(os.path.dirname(__file__), "updates")
UPDATE_MANIFEST_PATH = os.path.join(UPDATE_DIR, "client_manifest.json")

ALIASES = {
    "WORKSTATION-ABCD123": "DEVICE-IGD-01",
    "WORKSTATION-XYZ987": "DEVICE-POLI-01",
    "WORKSTATION-FARM1": "DEVICE-FARMASI-01",
    "WORKSTATION-PENDAFTARAN": "DEVICE-PENDAFTARAN-01",
    "WORKSTATION-RAWATINAP": "DEVICE-RAWATINAP-01",
    "WORKSTATION-KANTOR": "DEVICE-KANTOR-01",
}

CRITICAL_IPS = ["10.10.10.68", "10.10.10.9", "10.10.10.1", "10.10.10.21"]
FAKE_HOSTNAME_MARKERS = ("test-pc", "validation-pc", "dummy-pc", "demo-pc", "sample-pc", "pc-test-01")


def is_fake_hostname(hostname: str) -> bool:
    value = str(hostname or "").strip().lower()
    if not value:
        return False
    return any(marker in value for marker in FAKE_HOSTNAME_MARKERS)


class ReuseAddressHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_client_registry(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}

    cleaned = {}
    for key, record in data.items():
        if not isinstance(record, dict):
            continue

        hostname = str(record.get("hostname") or key or "").strip().lower()
        raw_key = str(key or "").strip().lower()
        if is_fake_hostname(hostname) or is_fake_hostname(raw_key):
            continue

        cleaned[key] = record

    return cleaned


def prune_inactive_clients(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}

    now = datetime.now(timezone.utc)
    cleaned = {}
    removed = 0
    for key, record in data.items():
        if not isinstance(record, dict):
            continue
        ts_value = str(record.get("last_seen") or record.get("timestamp") or "").strip()
        if not ts_value:
            cleaned[key] = record
            continue
        try:
            ts = datetime.fromisoformat(ts_value.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_seconds = (now - ts).total_seconds()
            if age_seconds > HEARTBEAT_TIMEOUT_SECONDS:
                removed += 1
                cleaned[key] = record
                continue
            cleaned[key] = record
        except Exception:
            cleaned[key] = record

    if removed:
        log_debug(f"PRUNED_INACTIVE_CLIENTS retained={len(cleaned)}")
    return cleaned


def save_clients(data: dict):
    try:
        cleaned_data = sanitize_client_registry(data)
        with open(DATA_FILE, "w", encoding="utf-8") as file:
            json.dump(cleaned_data, file, ensure_ascii=False, indent=2)
        log_debug(f"SAVE_CLIENTS_OK count={len(cleaned_data)}")
    except Exception as exc:
        log_debug(f"FAILED_SAVE_CLIENTS err={exc}")


def load_clients() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            if isinstance(data, dict):
                return sanitize_client_registry(data)
            return {}
    except Exception:
        return {}


def load_commands() -> list:
    if not os.path.exists(COMMANDS_FILE):
        return []
    try:
        with open(COMMANDS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_commands(data: list):
    with open(COMMANDS_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def log_debug(message: str):
    log_path = os.path.join(os.path.dirname(__file__), "server_debug.log")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {message}\n")


class ClientApiHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/health", "/api/health"):
            log_debug(f"GET {self.path} from {self.client_address[0]}")
            self.send_json(200, {"status": "ok", "service": "hospital-monitoring-api"})
            return

        if parsed.path == "/api/heartbeat":
            log_debug(f"GET heartbeat endpoint called from {self.client_address[0]} (expected POST)")
            self.send_json(200, {
                "status": "heartbeat_endpoint_ready",
                "method": "POST",
                "hint": "Send heartbeat JSON with X-Token header to /api/heartbeat"
            })
            return

        if parsed.path == "/api/clients":
            log_debug(f"GET {self.path} from {self.client_address[0]}")
            self.send_json(200, {"clients": load_clients()})
            return

        if parsed.path == "/api/commands":
            self.handle_get_commands()
            return

        if parsed.path == "/api/update/check":
            self.handle_update_check()
            return

        if parsed.path == "/api/update/download":
            self.handle_update_download()
            return

        if parsed.path == "/api/command/ack":
            self.handle_command_ack()
            return

        log_debug(f"GET 404 {self.path} from {self.client_address[0]}")
        self.send_json(404, {"status": "not_found"})

    def handle_get_commands(self):
        token = self.headers.get("X-Token") or self.headers.get("Authorization", "").replace("Bearer ", "")
        if token != SECRET_TOKEN:
            log_debug(f"COMMANDS_BLOCKED_INVALID_TOKEN from {self.client_address[0]}")
            self.send_json(401, {"status": "invalid_token"})
            return

        client_ip = self.client_address[0]
        known_clients = load_clients()
        client_match = None
        for record in known_clients.values():
            if str(record.get("ip_address", "")).strip() == client_ip:
                client_match = record
                break

        if client_match is None:
            log_debug(f"COMMANDS_REJECTED_UNKNOWN_CLIENT ip={client_ip} reason=no_heartbeat")
            self.send_json(403, {"status": "unknown_client", "message": "heartbeat belum diterima"})
            return

        log_debug(f"GET commands from {client_ip} accepted_known_client={client_match.get('hostname', 'unknown')}")
        self.send_json(200, {"commands": load_commands()})

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/heartbeat":
            self.handle_heartbeat()
            return

        if parsed.path == "/api/command":
            self.handle_command()
            return

        if parsed.path == "/api/command/ack":
            self.handle_command_ack()
            return

        if parsed.path == "/api/command/result":
            self.handle_command_result()
            return

        if parsed.path == "/api/force_heartbeat":
            self.handle_force_heartbeat()
            return

        log_debug(f"POST 404 {self.path} from {self.client_address[0]}")
        self.send_json(404, {"status": "not_found"})

    def handle_heartbeat(self):
        token = self.headers.get("X-Token") or self.headers.get("Authorization", "").replace("Bearer ", "")
        log_debug(f"POST heartbeat from {self.client_address[0]} token={token}")
        if token != SECRET_TOKEN:
            log_debug(f"INVALID_TOKEN from {self.client_address[0]} received={token}")
            self.send_json(401, {"status": "invalid_token"})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            log_debug(f"INVALID_JSON from {self.client_address[0]} err={exc}")
            self.send_json(400, {"status": "invalid_json"})
            return

        if not isinstance(payload, dict):
            log_debug(f"INVALID_PAYLOAD from {self.client_address[0]}")
            self.send_json(400, {"status": "invalid_payload"})
            return

        hostname = str(payload.get("hostname", "unknown-pc")).strip() or "unknown-pc"
        ip_address = str(payload.get("ip_address", "0.0.0.0")).strip() or "0.0.0.0"
        now = utc_now()

        if is_fake_hostname(hostname):
            log_debug(f"REJECTED_FAKE_HEARTBEAT hostname={hostname} ip={ip_address}")
            self.send_json(400, {"status": "rejected_fake_client"})
            return

        client_data = load_clients()
        client_data[hostname.lower()] = {
            "hostname": hostname,
            "ip_address": ip_address,
            "timestamp": now,
            "last_seen": now,
            "active_window": payload.get("active_window", "-"),
            "open_windows": payload.get("open_windows", []),
            "cpu_percent": payload.get("cpu_percent", 0),
            "ram_percent": payload.get("ram_percent", 0),
            "uptime_seconds": payload.get("uptime_seconds", 0),
            "os": payload.get("os", "Windows"),
            "network": payload.get("network", {
                "gateway": "0.0.0.0",
                "interface_name": "Unknown",
                "interface_type": "LAN",
                "gateway_reachable": False,
                "critical_ips": [],
            }),
            "speaker": payload.get("speaker", {
                "audio_status": "Disconnected",
                "speaker_connected": False,
                "connection_type": "Unknown",
                "volume": 0,
                "muted": False,
                "device_name": "N/A",
            }),
        }
        clean_data = sanitize_client_registry(client_data)
        save_clients(clean_data)
        log_debug(f"HEARTBEAT_ACCEPTED hostname={hostname} ip={ip_address}")
        self.send_json(200, {"status": "ok", "hostname": hostname})

    def handle_command(self):
        token = self.headers.get("X-Token") or self.headers.get("Authorization", "").replace("Bearer ", "")
        if token != SECRET_TOKEN:
            self.send_json(401, {"status": "invalid_token"})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            log_debug(f"INVALID_COMMAND_JSON err={exc}")
            self.send_json(400, {"status": "invalid_json"})
            return

        hostname = str(payload.get("hostname", "")).strip()
        ip_address = str(payload.get("ip_address", "")).strip()
        command = str(payload.get("command", "")).strip()
        if not hostname or not command:
            self.send_json(400, {"status": "missing_hostname_or_command"})
            return

        commands = load_commands()
        entry = {
            "hostname": hostname,
            "ip_address": ip_address,
            "command": command,
            "status": "queued",
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        commands.append(entry)
        save_commands(commands)
        log_debug(f"COMMAND_QUEUED hostname={hostname} ip={ip_address} command={command}")
        self.send_json(200, {"status": "queued", "hostname": hostname, "command": command})

    def handle_force_heartbeat(self):
        token = self.headers.get("X-Token") or self.headers.get("Authorization", "").replace("Bearer ", "")
        if token != SECRET_TOKEN:
            self.send_json(401, {"status": "invalid_token"})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            log_debug(f"INVALID_FORCE_HEARTBEAT_JSON err={exc}")
            self.send_json(400, {"status": "invalid_json"})
            return

        hostname = str(payload.get("hostname", "")).strip()
        if not hostname:
            self.send_json(400, {"status": "missing_hostname"})
            return

        commands = load_commands()
        entry = {
            "hostname": hostname,
            "ip_address": str(payload.get("ip_address", "")).strip(),
            "command": "force_heartbeat",
            "status": "queued",
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        commands.append(entry)
        save_commands(commands)
        log_debug(f"FORCE_HEARTBEAT_QUEUED hostname={hostname}")
        self.send_json(200, {"status": "queued", "hostname": hostname, "command": "force_heartbeat"})

    def handle_command_ack(self):
        token = self.headers.get("X-Token") or self.headers.get("Authorization", "").replace("Bearer ", "")
        if token != SECRET_TOKEN:
            self.send_json(401, {"status": "invalid_token"})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            log_debug(f"INVALID_COMMAND_ACK_JSON err={exc}")
            self.send_json(400, {"status": "invalid_json"})
            return

        hostname = str(payload.get("hostname", "")).strip()
        command = str(payload.get("command", "")).strip()
        if not hostname or not command:
            self.send_json(400, {"status": "missing_hostname_or_command"})
            return

        commands = load_commands()
        updated = []
        for item in commands:
            if str(item.get("hostname", "")).strip() == hostname and str(item.get("command", "")).strip() == command:
                item["status"] = "done"
                item["updated_at"] = utc_now()
            updated.append(item)
        save_commands(updated)
        self.send_json(200, {"status": "acknowledged", "hostname": hostname, "command": command})

    def handle_command_result(self):
        token = self.headers.get("X-Token") or self.headers.get("Authorization", "").replace("Bearer ", "")
        if token != SECRET_TOKEN:
            self.send_json(401, {"status": "invalid_token"})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            log_debug(f"INVALID_COMMAND_RESULT_JSON err={exc}")
            self.send_json(400, {"status": "invalid_json"})
            return

        hostname = str(payload.get("hostname", "")).strip()
        command = str(payload.get("command", "")).strip()
        status = str(payload.get("status", "failed")).strip()
        output = str(payload.get("output", "")).strip()
        if not hostname or not command:
            self.send_json(400, {"status": "missing_hostname_or_command"})
            return

        commands = load_commands()
        updated = []
        for item in commands:
            if str(item.get("hostname", "")).strip() == hostname and str(item.get("command", "")).strip() == command:
                item["status"] = status or "failed"
                item["result"] = output
                item["updated_at"] = utc_now()
            updated.append(item)
        save_commands(updated)
        log_debug(f"COMMAND_RESULT hostname={hostname} command={command} status={status}")
        self.send_json(200, {"status": "ok", "hostname": hostname, "command": command})

    def handle_update_check(self):
        token = self.headers.get("X-Token") or self.headers.get("Authorization", "").replace("Bearer ", "")
        if token != SECRET_TOKEN:
            self.send_json(401, {"status": "invalid_token"})
            return

        manifest = load_update_manifest()
        self.send_json(200, manifest)

    def handle_update_download(self):
        token = self.headers.get("X-Token") or self.headers.get("Authorization", "").replace("Bearer ", "")
        if token != SECRET_TOKEN:
            self.send_json(401, {"status": "invalid_token"})
            return

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        filename = query.get("filename", ["client.py"])[0]

        manifest = load_update_manifest()
        manifest_name = str(manifest.get("filename", filename) or filename)
        candidate_paths = [
            os.path.join(os.path.dirname(__file__), manifest_name),
            os.path.join(os.path.dirname(__file__), filename),
            os.path.join(os.path.dirname(__file__), "client.py"),
        ]

        selected_path = None
        for candidate in candidate_paths:
            if os.path.exists(candidate):
                selected_path = candidate
                break

        if selected_path is None:
            self.send_json(404, {"status": "file_not_found"})
            return

        with open(selected_path, "rb") as file:
            content = file.read()

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(selected_path)}"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def default_update_manifest() -> dict:
    return {
        "version": "1.0.0",
        "filename": "client.py",
        "notes": "Versi awal client monitoring",
        "updated_at": utc_now(),
    }


def load_update_manifest() -> dict:
    os.makedirs(UPDATE_DIR, exist_ok=True)
    if not os.path.exists(UPDATE_MANIFEST_PATH):
        manifest = default_update_manifest()
        with open(UPDATE_MANIFEST_PATH, "w", encoding="utf-8") as file:
            json.dump(manifest, file, ensure_ascii=False, indent=2)
        return manifest
    try:
        with open(UPDATE_MANIFEST_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else default_update_manifest()
    except Exception:
        return default_update_manifest()


def save_update_manifest(manifest: dict):
    os.makedirs(UPDATE_DIR, exist_ok=True)
    with open(UPDATE_MANIFEST_PATH, "w", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, indent=2)


def render_dashboard():
    st.set_page_config(page_title="Hospital Monitoring", page_icon="🏥", layout="wide")
    st.title("Hospital Monitoring")

    clients = load_clients()
    if not clients:
        st.info("Belum ada heartbeat client yang masuk. Tunggu data dari client.")
        return

    rows = []
    for key, value in clients.items():
        rows.append({
            "id": key,
            "hostname": value.get("hostname", key),
            "ip_address": value.get("ip_address", "0.0.0.0"),
            "last_seen": value.get("last_seen", value.get("timestamp", "-")),
            "active_window": value.get("active_window", "-"),
            "cpu_percent": value.get("cpu_percent", 0),
            "ram_percent": value.get("ram_percent", 0),
            "os": value.get("os", "Windows"),
        })

    df = pd.DataFrame(rows)
    col1, col2, col3 = st.columns(3)
    col1.metric("Total client", len(df))
    col2.metric("CPU rata-rata", f"{df['cpu_percent'].mean():.1f}%" if not df.empty else "0%")
    col3.metric("RAM rata-rata", f"{df['ram_percent'].mean():.1f}%" if not df.empty else "0%")

    st.subheader("Daftar client")
    st.dataframe(df, use_container_width=True, hide_index=True)

    selected = st.selectbox("Pilih client", df["hostname"].tolist())
    if selected:
        selected_client = next((item for item in rows if item["hostname"] == selected), None)
        if selected_client:
            st.subheader(f"Detail {selected_client['hostname']}")
            st.json(clients.get(selected_client["id"]) or clients.get(selected_client["hostname"].lower(), {}))


def start_api_server():
    httpd = ReuseAddressHTTPServer((API_HOST, API_PORT), ClientApiHandler)
    print(f"Listening on http://{API_HOST}:{API_PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        httpd.server_close()


if __name__ == "__main__":
    start_api_server()
