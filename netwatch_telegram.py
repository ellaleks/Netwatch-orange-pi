"""Optional Telegram delivery and interactive pairing for Netwatch."""

import getpass
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CREDENTIAL = "netwatch-telegram.json"
CREDSTORE = Path("/etc/credstore")
MAX_REPORT_BYTES = 5 * 1024 * 1024


class TelegramError(Exception):
    pass


def telegram_api(token, method, data=b"{}", content_type="application/json"):
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise TelegramError("Invalid bot token format.")
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": content_type},
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise TelegramError(f"Telegram API returned HTTP {exc.code}.") from None
    except (URLError, OSError):
        raise TelegramError("Could not reach Telegram or the request timed out.") from None
    except ValueError:
        raise TelegramError("Invalid reply from Telegram.") from None
    if not isinstance(payload, dict) or not payload.get("ok") or "result" not in payload:
        raise TelegramError("Telegram API rejected the request.")
    return payload["result"]


def send_document(token, chat_id, path):
    if type(chat_id) is not int or chat_id <= 0:
        raise TelegramError("A private Telegram chat ID is required.")
    with path.open("rb") as report:
        document = report.read(MAX_REPORT_BYTES + 1)
    if len(document) > MAX_REPORT_BYTES:
        raise TelegramError("Report exceeds Netwatch's 5 MiB delivery limit.")
    boundary = "netwatch-" + secrets.token_hex(16)
    headers = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{path.name}"\r\n'
        'Content-Type: text/plain; charset=utf-8\r\n\r\n'
    ).encode("ascii")
    data = headers + document + f"\r\n--{boundary}--\r\n".encode("ascii")
    telegram_api(token, "sendDocument", data, f"multipart/form-data; boundary={boundary}")


def deliver_report(path):
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        return
    try:
        credential = (Path(directory) / CREDENTIAL).read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        print("Telegram delivery failed: could not read credentials. Report saved locally.",
              file=sys.stderr)
        return
    try:
        settings = json.loads(credential)
        send_document(settings["token"], settings["chat_id"], path)
    except TelegramError as exc:
        print(f"Telegram delivery failed: {exc} Report saved locally.", file=sys.stderr)
    except (KeyError, OSError, ValueError, TypeError):
        print("Telegram delivery failed: invalid credentials or unreadable report. Report saved locally.",
              file=sys.stderr)
    else:
        print(f"Telegram delivery succeeded: {path.name}")


def setup():
    if os.geteuid() != 0 or not sys.stdin.isatty():
        raise TelegramError("Run this setup interactively with sudo.")
    token = getpass.getpass("BotFather token (hidden): ").strip()
    bot = telegram_api(token, "getMe")
    code = secrets.token_urlsafe(16)
    print(f"Open https://t.me/{bot['username']}?start={code} in Telegram and press Start.")
    input("Then press Enter here: ")
    updates = telegram_api(token, "getUpdates", b'{"allowed_updates":["message"]}')
    chats = set()
    for update in updates:
        message = update.get("message", {})
        chat = message.get("chat", {})
        if chat.get("type") == "private" and message.get("text") == f"/start {code}":
            chats.add(chat["id"])
    if len(chats) != 1:
        raise TelegramError("Pairing message not found in a single private chat. Try again with a new bot.")

    CREDSTORE.mkdir(mode=0o700, exist_ok=True)
    temp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=CREDSTORE, prefix=".netwatch-", delete=False
    )
    temporary = Path(temp.name)
    try:
        with temp:
            json.dump({"token": token, "chat_id": chats.pop()}, temp)
        temporary.replace(CREDSTORE / CREDENTIAL)
    finally:
        temporary.unlink(missing_ok=True)
    print("Telegram paired. Start netwatch-speed.service to send the first report.")


if __name__ == "__main__":
    try:
        setup()
    except (OSError, EOFError, TelegramError) as exc:
        raise SystemExit(f"Netwatch: {exc}")
