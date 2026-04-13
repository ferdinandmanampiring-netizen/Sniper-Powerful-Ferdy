import os
from pathlib import Path
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from dotenv import load_dotenv
import yaml
import time
import sqlite3
import logging

class TelegramAgent:
    def __init__(self):
        load_dotenv()  # Pastikan .env kebaca di sini juga
        self.api_id = os.getenv("TELEGRAM_API_ID")
        self.api_hash = os.getenv("TELEGRAM_API_HASH")
        self.session_string = os.getenv("TELETHON_SESSION")

        if not self.api_id or not self.api_hash:
            raise ValueError("Kunci Telegram di .env belum terbaca!")

        # Opsi A: StringSession (tanpa file SQLite -> menghindari 'database is locked' di Windows)
        if not self.session_string:
            raise ValueError(
                "TELETHON_SESSION belum ada di .env. Jalankan scripts/generate_string_session.py sekali untuk membuatnya."
            )

        self.client = TelegramClient(StringSession(self.session_string), int(self.api_id), self.api_hash)

        # Load whitelist chat IDs dari config/settings.yaml
        base_dir = Path(__file__).resolve().parents[1]
        settings_path = base_dir / "config" / "settings.yaml"
        with open(settings_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        tg_cfg = (cfg.get("telegram") or {})
        self.allowed_chat_ids = set(tg_cfg.get("allowed_chat_ids") or [])
        self.allowed_forward_sender_ids = set(tg_cfg.get("allowed_forward_sender_ids") or [])

    async def start_listening(self, callback_function):
        @self.client.on(events.NewMessage(chats=list(self.allowed_chat_ids) if self.allowed_chat_ids else None))
        async def handler(event):
            msg = event.message
            raw_text = msg.message or ""
            received_ts = time.time()

            # Logging sumber pesan untuk verifikasi whitelist
            chat_id = None
            chat_title = None
            fwd_sender_id = None
            fwd_sender_name = None
            try:
                chat = await event.get_chat()
                chat_id = getattr(chat, "id", None)
                chat_title = getattr(chat, "title", None) or getattr(chat, "username", None) or getattr(chat, "first_name", None)
                logging.info(f"[TG] from_chat_id={chat_id} title={chat_title}")
            except Exception:
                # Jika gagal resolve chat, tetap lanjut proses
                pass

            # Forward sender filter (untuk kasus sinyal berasal dari user/channel yang di-forward)
            if self.allowed_forward_sender_ids:
                fwd = getattr(msg, "fwd_from", None)
                if fwd:
                    fwd_from = getattr(fwd, "from_id", None) or getattr(fwd, "from_name", None)
                    # Telethon: from_id bisa PeerUser/PeerChannel; ambil .user_id/.channel_id jika ada
                    sender_id = None
                    if hasattr(fwd_from, "user_id"):
                        sender_id = int(fwd_from.user_id)
                    elif hasattr(fwd_from, "channel_id"):
                        sender_id = int(fwd_from.channel_id)
                    elif isinstance(fwd_from, int):
                        sender_id = int(fwd_from)

                    fwd_sender_id = sender_id
                    fwd_sender_name = getattr(fwd, "from_name", None)

                    # ID forward sender yang user berikan biasanya bernilai negatif; jika Telethon memberi positif,
                    # kita cek kedua bentuknya.
                    if sender_id is not None:
                        if (sender_id not in self.allowed_forward_sender_ids) and (-sender_id not in self.allowed_forward_sender_ids):
                            return

            # Panggil fungsi untuk proses data ke Logic Agent
            await callback_function(
                {
                    "raw_text": raw_text,
                    "received_ts": received_ts,
                    "chat_id": chat_id,
                    "chat_title": chat_title,
                    "fwd_sender_id": fwd_sender_id,
                    "fwd_sender_name": fwd_sender_name,
                }
            )
            
        # Connect dulu, lalu pastikan session sudah authorized (tanpa prompt interactive)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise ValueError("StringSession Telegram belum authorized. Jalankan scripts/generate_string_session.py sekali lalu simpan TELETHON_SESSION ke .env.")

        # start() biasanya no-op jika sudah connect+authorized
        await self.client.start()
        await self.client.run_until_disconnected()
