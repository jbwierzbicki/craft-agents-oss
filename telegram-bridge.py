import os
import subprocess
import requests
import time
import json

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
    # Split into chunks if over Telegram's 4096 char limit
    for i in range(0, len(text), 4000):
        chunk = text[i:i+4000]
        try:
            requests.post(f"{API}/sendMessage", json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown"
            }, timeout=10)
        except Exception as e:
            print(f"Error sending message: {e}")

def ask_agent(message):
    try:
        env = {
            **os.environ,
            "CRAFT_SERVER_URL": CRAFT_SERVER_URL,
            "CRAFT_SERVER_TOKEN": CRAFT_SERVER_TOKEN,
        }
        result = subprocess.run(
            ["bun", "run", "/app/apps/cli/src/index.ts",
             "send", SESSION_ID, message],
            capture_output=True,
            text=True,
            timeout=120,
            env=env
        )
        response = result.stdout.strip()
        if not response:
            response = result.stderr.strip()
        if not response:
            response = "No response from agent."
        return response
    except subprocess.TimeoutExpired:
        return "The agent took too long to respond. Try again."
    except Exception as e:
        return f"Error contacting agent: {e}"

def main():
    print(f"Telegram bridge started. Allowed chat ID: {ALLOWED_CHAT_ID}")
    print(f"Using session: {SESSION_ID}")
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
