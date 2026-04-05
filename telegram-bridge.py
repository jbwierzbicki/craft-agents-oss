import json
import os
import queue
import threading
import time
import uuid

import requests
import websocket


PROTOCOL_VERSION = "1.0"
REQUEST_TIMEOUT_MS = int(os.environ.get("CRAFT_REQUEST_TIMEOUT_MS", "10000"))
CONNECT_TIMEOUT_MS = int(os.environ.get("CRAFT_CONNECT_TIMEOUT_MS", "10000"))
SEND_TIMEOUT_MS = int(os.environ.get("CRAFT_SEND_TIMEOUT_MS", "120000"))
SESSION_BLOCKING_EVENTS = {
    "permission_request": "Agent needs a permission approval in the Craft UI.",
    "credential_request": "Agent needs credentials entered in the Craft UI.",
    "auth_request": "Agent needs authentication completed in the Craft UI.",
    "plan_submitted": "Agent submitted a plan that needs approval in the Craft UI.",
}


def log(message):
    print(message, flush=True)


def require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = int(require_env("TELEGRAM_CHAT_ID"))
SESSION_ID = require_env("CRAFT_SESSION_ID")
CRAFT_SERVER_URL = require_env("CRAFT_SERVER_URL")
CRAFT_SERVER_TOKEN = require_env("CRAFT_SERVER_TOKEN")
CRAFT_WORKSPACE_ID = os.environ.get("CRAFT_WORKSPACE_ID")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


class RpcClientError(Exception):
    pass


def parse_json_response(response):
    try:
        return response.json()
    except Exception:
        return {
            "ok": False,
            "status_code": response.status_code,
            "raw_text": response.text[:500],
        }


class RpcClient:
    def __init__(self, url, token, workspace_id=None):
        self.url = url
        self.token = token
        self.workspace_id = workspace_id
        self.app = None
        self.thread = None
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.event_queue = queue.Queue()
        self.closed = threading.Event()
        self.close_code = None
        self.close_reason = None
        self.last_error = None
        self.handshake_id = None

    def connect(self):
        self.handshake_id = self._register_pending()
        self.app = websocket.WebSocketApp(
            self.url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self.thread = threading.Thread(target=self.app.run_forever, daemon=True)
        log(f"Connecting to Craft server: {self.url}")
        self.thread.start()
        self._wait_for_pending(self.handshake_id, CONNECT_TIMEOUT_MS, "handshake")

    def invoke(self, channel, *args, timeout_ms=REQUEST_TIMEOUT_MS):
        request_id = self._register_pending()
        envelope = {
            "id": request_id,
            "type": "request",
            "channel": channel,
            "args": list(args),
        }
        self._send(envelope)
        return self._wait_for_pending(request_id, timeout_ms, channel)

    def next_event(self, timeout_seconds):
        try:
            return self.event_queue.get(timeout=timeout_seconds)
        except queue.Empty:
            return None

    def close(self):
        if self.app is not None:
            try:
                self.app.close()
            except Exception:
                pass
        self.closed.set()

    def _register_pending(self):
        request_id = str(uuid.uuid4())
        with self.pending_lock:
            self.pending[request_id] = queue.Queue(maxsize=1)
        return request_id

    def _resolve_pending_queue(self, request_id):
        with self.pending_lock:
            pending_queue = self.pending.pop(request_id, None)
        return pending_queue

    def _send(self, envelope):
        if self.app is None or self.closed.is_set():
            raise RpcClientError("WebSocket is not connected.")
        raw = json.dumps(envelope)
        log(
            f"Sending envelope type={envelope.get('type')} id={envelope.get('id')} "
            f"channel={envelope.get('channel')}"
        )
        self.app.send(raw)

    def _wait_for_pending(self, request_id, timeout_ms, label):
        with self.pending_lock:
            pending_queue = self.pending.get(request_id)
        if pending_queue is None:
            raise RpcClientError(f"Missing pending request state for {label}.")
        try:
            outcome = pending_queue.get(timeout=timeout_ms / 1000)
        except queue.Empty as exc:
            self._resolve_pending_queue(request_id)
            raise RpcClientError(f"Timed out waiting for {label} after {timeout_ms}ms.") from exc

        if outcome["ok"]:
            return outcome["value"]
        raise RpcClientError(outcome["error"])

    def _fail_all_pending(self, error_message):
        with self.pending_lock:
            pending_items = list(self.pending.items())
            self.pending.clear()
        for _, pending_queue in pending_items:
            try:
                pending_queue.put_nowait({"ok": False, "error": error_message})
            except queue.Full:
                pass

    def _resolve_pending(self, request_id, ok, value=None, error=None):
        pending_queue = self._resolve_pending_queue(request_id)
        if pending_queue is None:
            return
        payload = {"ok": ok, "value": value, "error": error}
        try:
            pending_queue.put_nowait(payload)
        except queue.Full:
            pass

    def _on_open(self, _ws):
        handshake = {
            "id": self.handshake_id,
            "type": "handshake",
            "protocolVersion": PROTOCOL_VERSION,
            "token": self.token,
        }
        if self.workspace_id:
            handshake["workspaceId"] = self.workspace_id
        log(
            f"WebSocket opened, sending handshake "
            f"workspaceId={self.workspace_id or 'unset'}"
        )
        self._send(handshake)

    def _on_message(self, _ws, raw):
        try:
            envelope = json.loads(raw)
        except Exception as exc:
            log(f"Failed to decode message: {exc}; raw={raw[:300]!r}")
            return

        envelope_type = envelope.get("type")
        envelope_id = envelope.get("id")
        log(
            f"Received envelope type={envelope_type} id={envelope_id} "
            f"channel={envelope.get('channel')}"
        )

        if envelope_type == "handshake_ack":
            client_id = envelope.get("clientId")
            log(f"Handshake acknowledged. clientId={client_id}")
            self._resolve_pending(envelope_id, True, envelope)
            return

        if envelope_type == "response":
            if envelope.get("error"):
                self._resolve_pending(
                    envelope_id,
                    False,
                    error=envelope["error"].get("message", "Unknown RPC error"),
                )
            else:
                self._resolve_pending(envelope_id, True, envelope.get("result"))
            return

        if envelope_type == "event":
            self.event_queue.put(envelope)
            return

        if envelope_type == "error":
            error_message = envelope.get("error", {}).get("message", "Unknown protocol error")
            self._resolve_pending(envelope_id, False, error=error_message)
            return

        log(f"Ignoring unexpected envelope: {raw[:300]!r}")

    def _on_error(self, _ws, error):
        self.last_error = str(error)
        log(f"WebSocket error: {self.last_error}")

    def _on_close(self, _ws, close_status_code, close_msg):
        self.close_code = close_status_code
        self.close_reason = close_msg
        log(
            f"WebSocket closed: code={close_status_code} "
            f"reason={close_msg!r}"
        )
        self.closed.set()
        close_detail = f"WebSocket closed before completion (code={close_status_code}, reason={close_msg!r})"
        if self.last_error:
            close_detail = f"{close_detail}; last_error={self.last_error}"
        self._fail_all_pending(close_detail)


def get_updates(offset=None):
    params = {"timeout": 30, "offset": offset}
    try:
        response = requests.get(f"{API}/getUpdates", params=params, timeout=35)
        response.raise_for_status()
        payload = parse_json_response(response)
    except Exception as exc:
        log(f"Error getting Telegram updates: {exc}")
        return []

    if not payload.get("ok", False):
        log(f"Telegram getUpdates failed: {payload}")
        return []
    return payload.get("result", [])


def send_message(chat_id, text):
    text = text.strip() or "No response from agent."
    for start in range(0, len(text), 4000):
        chunk = text[start:start + 4000]
        try:
            response = requests.post(
                f"{API}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
                timeout=10,
            )
            payload = parse_json_response(response)
            if response.status_code >= 400 or not payload.get("ok", False):
                log(
                    f"Telegram sendMessage failed: status={response.status_code} "
                    f"body={payload}"
                )
        except Exception as exc:
            log(f"Error sending Telegram message: {exc}")


def determine_workspace_id(client):
    if CRAFT_WORKSPACE_ID:
        log(f"Using configured workspace: {CRAFT_WORKSPACE_ID}")
        return CRAFT_WORKSPACE_ID

    workspaces = client.invoke("workspaces:get")
    if not workspaces:
        raise RpcClientError(
            "No workspaces available. Set CRAFT_WORKSPACE_ID explicitly."
        )

    if len(workspaces) == 1:
        workspace_id = workspaces[0]["id"]
        log(f"Using only available workspace: {workspace_id}")
        return workspace_id

    log(
        f"Searching {len(workspaces)} workspaces for session {SESSION_ID}"
    )
    for workspace in workspaces:
        workspace_id = workspace.get("id")
        if not workspace_id:
            continue
        try:
            sessions = client.invoke("sessions:get", workspace_id)
        except RpcClientError as exc:
            log(f"Failed to inspect workspace {workspace_id}: {exc}")
            continue
        if any(session.get("id") == SESSION_ID for session in sessions or []):
            log(f"Matched session {SESSION_ID} to workspace {workspace_id}")
            return workspace_id

    raise RpcClientError(
        "Could not determine the workspace for CRAFT_SESSION_ID. "
        "Set CRAFT_WORKSPACE_ID explicitly."
    )


def build_response(delta_parts, completed_text):
    response = "".join(delta_parts).strip()
    final_text = completed_text.strip() if completed_text else ""
    if final_text and len(final_text) >= len(response):
        return final_text
    if response:
        return response
    return ""


def ask_agent(message):
    client = RpcClient(
        CRAFT_SERVER_URL,
        CRAFT_SERVER_TOKEN,
        workspace_id=CRAFT_WORKSPACE_ID,
    )
    delta_parts = []
    completed_text = None
    blocked_message = None

    try:
        client.connect()
        workspace_id = determine_workspace_id(client)
        if workspace_id != client.workspace_id:
            client.invoke("window:switchWorkspace", workspace_id)
            client.workspace_id = workspace_id
            log(f"Bound client to workspace {workspace_id}")
        elif workspace_id:
            client.invoke("window:switchWorkspace", workspace_id)
            log(f"Confirmed workspace binding for {workspace_id}")

        result = client.invoke("sessions:sendMessage", SESSION_ID, message)
        log(f"sessions:sendMessage response: {result!r}")

        deadline = time.time() + (SEND_TIMEOUT_MS / 1000)
        while time.time() < deadline:
            timeout_seconds = max(0.1, min(1.0, deadline - time.time()))
            envelope = client.next_event(timeout_seconds)
            if envelope is None:
                if client.closed.is_set():
                    raise RpcClientError(
                        "Connection closed before the agent finished responding. "
                        f"code={client.close_code}, reason={client.close_reason!r}"
                    )
                continue

            if envelope.get("channel") != "session:event":
                continue

            args = envelope.get("args") or []
            if not args or not isinstance(args[0], dict):
                continue

            event = args[0]
            if event.get("sessionId") != SESSION_ID:
                continue

            event_type = event.get("type")
            log(f"Session event: {event_type}")

            if event_type == "text_delta":
                delta = event.get("delta", "")
                if delta:
                    delta_parts.append(delta)
                continue

            if event_type == "text_complete":
                completed_text = event.get("text") or completed_text
                continue

            if event_type == "error":
                return f"Agent error: {event.get('error', 'Unknown error')}"

            if event_type == "interrupted":
                response = build_response(delta_parts, completed_text)
                return response or "The agent was interrupted before finishing."

            if event_type == "complete":
                response = build_response(delta_parts, completed_text)
                return response or "The agent completed without returning any text."

            if event_type in SESSION_BLOCKING_EVENTS:
                blocked_message = SESSION_BLOCKING_EVENTS[event_type]
                log(f"Blocking event encountered: {event_type}")

        if blocked_message:
            return blocked_message
        return "The agent took too long to respond. Try again."
    except RpcClientError as exc:
        log(f"RPC error: {exc}")
        return f"Error contacting agent: {exc}"
    except Exception as exc:
        log(f"Unexpected error: {exc}")
        return f"Error contacting agent: {exc}"
    finally:
        client.close()


def main():
    log(f"Telegram bridge started. Allowed chat ID: {ALLOWED_CHAT_ID}")
    log(f"Using session: {SESSION_ID}")
    log(f"Server: {CRAFT_SERVER_URL}")
    log(f"Configured workspace: {CRAFT_WORKSPACE_ID or 'auto-detect'}")
    log(
        f"Timeouts: connect={CONNECT_TIMEOUT_MS}ms "
        f"request={REQUEST_TIMEOUT_MS}ms send={SEND_TIMEOUT_MS}ms"
    )
    offset = None

    while True:
        updates = get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text", "").strip()

            if not chat_id or not text:
                continue

            if chat_id != ALLOWED_CHAT_ID:
                send_message(chat_id, "Unauthorized.")
                continue

            log(f"Received Telegram message: {text[:200]!r}")
            send_message(chat_id, "Thinking...")

            response = ask_agent(text)
            log(f"Sending Telegram response preview: {response[:200]!r}")
            send_message(chat_id, response)

        time.sleep(1)


if __name__ == "__main__":
    main()
