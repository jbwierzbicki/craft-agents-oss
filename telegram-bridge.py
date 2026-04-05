import os
import re
import time
import json
import requests
import threading
import websocket

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
SESSION_ID = os.environ["CRAFT_SESSION_ID"]
CRAFT_SERVER_URL = os.environ["CRAFT_SERVER_URL"]
CRAFT_SERVER_TOKEN = os.environ["CRAFT_SERVER_TOKEN"]

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

def get_updates(offset=None):
    try:
        params = {"timeout": 30, "offset": offset}
        r = requests.get(f"{API}/getUpdates", params=params, timeout=35)
        return r.json().get("result", [])
    except Exception as e:
        print(f"Error getting updates: {e}")
        return []

def send_message(chat_id, text):
    text = text.strip()
    for i in range(0, len(text), 4000):
        chunk = text[i:i+4000]
        try:
            requests.post(f"{API}/sendMessage", json={
                "chat_id": chat_id,
                "text": chunk,
            }, timeout=10)
        except Exception as e:
            print(f"Error sending message: {e}")

def ask_agent(message):
    result_chunks = []
    done = threading.Event()
    error_holder = []

    def on_message(ws, raw):
        try:
            print(f"RAW MESSAGE: {raw[:500]}")
            data = json.loads(raw)
            msg_type = data.get("type")
            print(f"TYPE: {msg_type}, KEYS: {list(data.keys())}")

            if msg_type == "assistant_chunk":
                chunk = data.get("content", "")
                if chunk:
                    result_chunks.append(chunk)

            elif msg_type in ("session_complete", "turn_complete", "message_complete"):
                print(f"Done signal received: {msg_type}")
                done.set()
                ws.close()

            elif msg_type == "error":
                error_holder.append(data.get("message", "Unknown error"))
                done.set()
                ws.close()

        except Exception as e:
            print(f"on_message error: {e}, raw: {raw[:200]}")

    def on_open(ws):
        print("WebSocket connected, sending message...")
        payload = json.dumps({
            "type": "send_message",
            "sessionId": SESSION_ID,
            "content": message,
            "token": CRAFT_SERVER_TOKEN,
        })
        ws.send(payload)

    def on_error(ws, error):
        print(f"WebSocket error: {error}")
        error_holder.append(str(error))
        done.set()

    def on_close(ws, code, msg):
        print(f"WebSocket closed: {code} {msg}")
        done.set()

    ws_app = websocket.WebSocketApp(
        CRAFT_SERVER_URL,
        header={"Authorization": f"Bearer {CRAFT_SERVER_TOKEN}"},
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    thread = threading.Thread(target=ws_app.run_forever)
    thread.daemon = True
    thread.start()

    done.wait(timeout=120)

    if error_holder:
        return f"Agent error: {error_holder[0]}"

    response = "".join(result_chunks).strip()
    if not response:
        return "No response from agent."
    return response


def main():
    print(f"Telegram bridge started. Allowed chat ID: {ALLOWED_CHAT_ID}")
    print(f"Using session: {SESSION_ID}")
    print(f"Server: {CRAFT_SERVER_URL}")
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

            print(f"Received: {text}")
            send_message(chat_id, "⏳ Thinking...")

            response = ask_agent(text)
            print(f"Response: {response[:100]}...")
            send_message(chat_id, response)

        time.sleep(1)

if __name__ == "__main__":
    main()
