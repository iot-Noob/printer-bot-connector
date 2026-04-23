import ssl
import websockets
import asyncio
from rocketchat_async import RocketChat
import urllib3
import os
import subprocess
import tempfile
from datetime import datetime
from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, HttpUrl
import httpx
from .config import config
from .storage_service import storage
from .print_manager import (
    print_manager,
    CONVERT_TO_PDF_EXTENSIONS,
    sibling_pdf_if_any,
)
import logging
import signal
import re
import hashlib
from pathlib import Path
from typing import Optional, Dict, List, Any, Set
from collections import deque
import time
import shlex

# ============================================================================
# LOGGING SETUP
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("printer_bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ============================================================================
# SSL PATCH (YOUR WORKING VERSION - KEPT AS IS)
# ============================================================================

original_connect = websockets.connect
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from urllib.parse import unquote
from .MenuOptionHelper import DynamicMenu

load_dotenv()


def patched_connect(uri, **kwargs):
    kwargs["ssl"] = kwargs.get("ssl", True)
    if isinstance(kwargs["ssl"], bool) and kwargs["ssl"]:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        kwargs["ssl"] = ssl_context
    return original_connect(uri, **kwargs)


websockets.connect = patched_connect

load_dotenv()


# Configuration is now in config.py


# ============================================================================
# PRODUCTION PRINTER BOT
# ============================================================================


class PrinterBot:
    # Allowed file extensions for automatic processing
    ALLOWED_EXTENSIONS = {".doc", ".docx", ".pdf", ".bmp", ".png", ".jpg", ".jpeg"}

    def __init__(self):
        self.config = config
        self.mainLock = asyncio.Lock()
        self.rocket = None
        self.pending_jobs = {}
        self.downloads_dir = Path(self.config.downloads_dir)
        self.downloads_dir.mkdir(exist_ok=True)

        self._processing_messages: Set[str] = set()
        self.dmc = None
        self._rate_limit: Dict[str, float] = {}
        self._download_semaphore = asyncio.Semaphore(
            self.config.max_concurrent_downloads
        )

        # Shutdown logic
        self.subscribers: Dict[str, bool] = {}
        self._shutdown_event = asyncio.Event()
        self._tasks: List[asyncio.Task] = []

        logger.info(f"Bot initialized for user: {self.config.username}")

    HELP_MESSAGE = (
        "📋 **Available Commands:**\n"
        "├─ `menu` - Open main menu\n"
        "├─ `m` - Open main menu (shortcut)\n"
        "├─ `help` - Show this help\n"
        "├─ `h` - Show this help (shortcut)\n"
        "├─ `status` - Check bot status\n"
        "└─ Supported file types: .doc, .docx, .pdf, .bmp, .png, .jpg, .jpeg"
    )

    def _check_rate_limit(self, user_id: str) -> bool:
        """Simple rate limiting per user."""
        now = time.time()
        if user_id in self._rate_limit:
            if now - self._rate_limit[user_id] < self.config.rate_limit_seconds:
                return False
        self._rate_limit[user_id] = now
        return True

    def _validate_user_id(self, user_id: str) -> bool:
        """Validate user ID to prevent path traversal (Regex hardened)."""
        return bool(re.match(r"^[a-zA-Z0-9_-]+$", user_id))

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename to prevent RCE."""
        from urllib.parse import unquote

        decoded = unquote(filename)
        basename = os.path.basename(decoded)
        safe = re.sub(r"[^\w\-\.]", "_", basename)
        if not safe or safe in (".", ".."):
            safe = "unnamed_file"
        return safe[:250]

    async def _list_files_async(self, user_dir: Path) -> List[Dict]:
        """Async file listing using thread pool."""

        def list_files_sync():
            files = []
            if not user_dir.exists():
                return []
            for f in user_dir.iterdir():
                if f.is_file():
                    files.append(
                        {
                            "name": f.name,
                            "size": f.stat().st_size,
                            "type": self.get_file_type(f.name),
                        }
                    )
            return sorted(files, key=lambda x: x["name"])

        return await asyncio.to_thread(list_files_sync)

    def get_file_type(self, filename: str) -> str:
        """Helper to get file extension/type."""
        ext = os.path.splitext(filename)[1].lower().strip(".")
        if ext in ("doc", "docx"):
            return "word"
        if ext in ("xls", "xlsx", "csv"):
            return "excel"
        if ext in ("ppt", "pptx"):
            return "powerpoint"
        return ext or "unknown"

    def initialize(self):
        if not self.rocket:
            self.rocket = RocketChat()
            logger.info(f"RocketChat instance created for: {self.config.username}")
        return self

    async def connect(self):
        try:
            self.initialize()
            server_url_str = str(self.config.server_url).rstrip("/")

            if server_url_str.startswith("https://"):
                ws_url = server_url_str.replace("https://", "wss://") + "/websocket"
            elif server_url_str.startswith("http://"):
                ws_url = server_url_str.replace("http://", "ws://") + "/websocket"
            else:
                ws_url = f"wss://{server_url_str}/websocket"

            logger.info(f"Connecting to: {ws_url}")

            await self.rocket.start(
                address=ws_url,
                username=self.config.username,
                password=self.config.password,
            )

            self.dmc = DynamicMenu(self)
            self._register_menus()
            logger.info("Connected successfully!")

        except Exception as e:
            logger.error(f"Connection error: {e}")
            raise ValueError(f"error occur connect: {e}")

    async def shutdown(self):
        """Graceful shutdown handler."""
        logger.info("Shutting down...")
        self._shutdown_event.set()

        for task in self._tasks:
            if not task.done():
                task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        if self.rocket:
            try:
                await self.rocket.close()
            except:
                pass

        logger.info("Shutdown complete")

    async def download_file(
        self, file_link: str, file_name: str, user_data: dict = {}
    ) -> Optional[str]:
        """Download file using httpx (non-blocking) and concurrency control."""
        async with self._download_semaphore:
            try:
                server_url = str(self.config.server_url).rstrip("/")
                full_url = f"{server_url}{file_link}"
                sender_id = user_data.get("sender_id", "unknown")

                if not self._validate_user_id(sender_id):
                    return None

                safe_name = self._sanitize_filename(file_name)
                # Hardened Path:
                user_dir = (self.downloads_dir / sender_id).resolve()
                if not str(user_dir).startswith(str(self.downloads_dir.resolve())):
                    logger.error(f"Path traversal attempt: {user_dir}")
                    return None

                user_dir.mkdir(parents=True, exist_ok=True)
                local_path = user_dir / safe_name

                async with httpx.AsyncClient(verify=self.config.ssl_verify) as client:
                    # Non-blocking login
                    login_resp = await client.post(
                        f"{server_url}/api/v1/login",
                        json={
                            "user": self.config.username,
                            "password": self.config.password,
                        },
                        timeout=self.config.api_timeout,
                    )

                    if login_resp.status_code != 200:
                        return None

                    login_data = login_resp.json().get("data", login_resp.json())
                    headers = {
                        "X-Auth-Token": login_data.get("authToken"),
                        "X-User-Id": login_data.get("userId"),
                    }

                    # Streaming download
                    async with client.stream(
                        "GET",
                        full_url,
                        headers=headers,
                        timeout=self.config.api_timeout,
                    ) as response:
                        if response.status_code != 200:
                            return None

                        content_length = response.headers.get("Content-Length")
                        max_size = self.config.max_file_size_mb * 1024 * 1024

                        if content_length and int(content_length) > max_size:
                            logger.error(f"File too large: {content_length}")
                            return None

                        total_size = 0
                        import aiofiles

                        async with aiofiles.open(local_path, "wb") as f:
                            async for chunk in response.aiter_bytes():
                                total_size += len(chunk)
                                if total_size > max_size:
                                    logger.error("Download size exceeded limit")
                                    break
                                await f.write(chunk)

                        if total_size <= max_size:
                            logger.info(f"Downloaded: {safe_name}")
                            return str(local_path)
                        else:
                            if local_path.exists():
                                local_path.unlink()
                            return None

            except Exception as e:
                logger.error(f"Download error: {e}")
                return None

    async def _handle_file_arrival(self, image_link, title, sender_id, room_id, msg_id):
        """Internal helper to handle download + auto-prompt for seamless UX."""
        try:
            local_path = await self.download_file(
                image_link, title, user_data={"sender_id": sender_id}
            )

            if local_path:
                self.pending_jobs[sender_id] = {
                    "file_path": local_path,
                    "filename": title,
                }
                await self.rocket.send_message(
                    f"✅ **{title}** is ready!\n"
                    "🖨️ Type `all` to print, or a range like `1-2` (or ignore).",
                    room_id,
                )
        except Exception as e:
            logger.error(f"Error in file arrival: {e}")

    async def get_user_settings(self, user_id: str) -> dict:
        return await storage.get_user_settings(user_id)

    async def estimate_pages(self, file_path: str, file_name: str) -> int:
        file_type = self.get_file_type(file_name)

        if file_type == "pdf":
            try:
                import PyPDF2

                with open(file_path, "rb") as f:
                    reader = PyPDF2.PdfReader(f)
                    return len(reader.pages)
            except:
                return 1
        return 1

    async def update_user_settings(self, user_id: str, key: str, value):
        await storage.update_user_settings(user_id, key, value)

    async def get_available_printers(self) -> list:
        return await print_manager.get_available_printers()

    async def get_print_queue(self) -> list:
        return await print_manager.get_print_queue()

    async def cancel_print_job(self, job_id: str) -> bool:
        return await print_manager.cancel_print_job(job_id)

    async def print_file(
        self, file_path: str, user_id: str, page_range: str = None
    ) -> str:
        """Isolated print logic – now directly passing files to the Universal Engine."""
        settings = await storage.get_user_settings(user_id)
        # We no longer convert to PDF here as print_manager handles Word/Excel natively
        return await print_manager.print_file(file_path, settings, page_range)

    def _register_menus(self):
        self.dmc.register_callable_menu(
            "main",
            "📋 Main Menu",
            {
                "1": {"text": "📁 Show Downloads", "action": self.menu_show_downloads},
                "2": {"text": "🖨️ Print Files", "next_menu": "print_menu"},
                "3": {"text": "⚙️ Printer Settings", "next_menu": "settings_menu"},
                "4": {"text": "📊 Printer Status", "next_menu": "status_menu"},
                "5": {"text": "🗑️ File Management", "next_menu": "file_menu"},
                "h": {"text": "❓ Help", "action": self.menu_help},
            },
        )

        self.dmc.register_callable_menu(
            "print_menu",
            "🖨️ Print Menu",
            {
                "1": {"text": "Print All Files", "action": self.menu_print_all},
                "2": {"text": "Print Last File", "action": self.menu_print_last},
                "3": {"text": "Select File to Print", "action": self.menu_select_file},
                "4": {"text": "Convert Last to PDF", "action": self.menu_convert_last},
                "5": {
                    "text": "Select File to Convert",
                    "action": self.menu_select_file_for_conversion,
                },
                "b": {"text": "◀ Back", "next_menu": "main"},
            },
        )

        self.dmc.register_callable_menu(
            "settings_menu",
            "⚙️ Printer Settings",
            {
                "1": {"text": "Paper Size", "next_menu": "paper_menu"},
                "2": {"text": "Print Quality", "next_menu": "quality_menu"},
                "3": {"text": "Color Mode", "next_menu": "color_menu"},
                "4": {"text": "Copies", "action": self.menu_set_copies},
                "5": {
                    "text": "Select Printer",
                    "action": self.menu_select_printer,
                },  # NEW
                "b": {"text": "◀ Back", "next_menu": "main"},
            },
        )

        self.dmc.register_callable_menu(
            "paper_menu",
            "📄 Paper Size",
            {
                "1": {
                    "text": "A4",
                    "action": self.menu_set_paper,
                    "args": {"size": "A4"},
                },
                "2": {
                    "text": "Letter",
                    "action": self.menu_set_paper,
                    "args": {"size": "Letter"},
                },
                "3": {
                    "text": "Legal",
                    "action": self.menu_set_paper,
                    "args": {"size": "Legal"},
                },
                "b": {"text": "◀ Back", "next_menu": "settings_menu"},
            },
        )

        self.dmc.register_callable_menu(
            "quality_menu",
            "🎨 Print Quality",
            {
                "1": {
                    "text": "Draft",
                    "action": self.menu_set_quality,
                    "args": {"quality": "draft"},
                },
                "2": {
                    "text": "Normal",
                    "action": self.menu_set_quality,
                    "args": {"quality": "normal"},
                },
                "3": {
                    "text": "High",
                    "action": self.menu_set_quality,
                    "args": {"quality": "high"},
                },
                "b": {"text": "◀ Back", "next_menu": "settings_menu"},
            },
        )

        self.dmc.register_callable_menu(
            "color_menu",
            "🎨 Color Mode",
            {
                "1": {
                    "text": "Color",
                    "action": self.menu_set_color,
                    "args": {"color": "color"},
                },
                "2": {
                    "text": "Black & White",
                    "action": self.menu_set_color,
                    "args": {"color": "grayscale"},
                },
                "b": {"text": "◀ Back", "next_menu": "settings_menu"},
            },
        )

        self.dmc.register_callable_menu(
            "status_menu",
            "📊 Printer Status",
            {
                "1": {"text": "Available Printers", "action": self.menu_show_printers},
                "2": {"text": "Print Queue", "action": self.menu_show_queue},
                "3": {"text": "Cancel Job", "action": self.menu_cancel_job},
                "4": {"text": "Retry/Resume Job", "action": self.menu_retry_job},
                "b": {"text": "◀ Back", "next_menu": "main"},
            },
        )

        self.dmc.register_callable_menu(
            "file_menu",
            "🗑️ File Management",
            {
                "1": {"text": "📁 Show Downloads", "action": self.menu_show_downloads},
                "2": {"text": "🗑️ Remove File", "action": self.menu_remove_file},
                "3": {"text": "💥 Remove All Files", "action": self.menu_remove_all},
                "b": {"text": "◀ Back", "next_menu": "main"},
            },
        )

    # ============ Menu Action Methods ============

    async def menu_show_downloads(self, user_id: str, room_id: str) -> str:
        user_dir = (self.downloads_dir / user_id).resolve()

        if not user_dir.exists():
            return "📁 No files found. Send me a file!"

        files = await self._list_files_async(user_dir)
        if not files:
            return "📁 Directory is empty."

        def format_size(size_bytes: int) -> str:
            if size_bytes < 1024:
                return f"{size_bytes} B"
            if size_bytes < 1024 * 1024:
                return f"{size_bytes / 1024:.1f} KB"
            return f"{size_bytes / (1024 * 1024):.1f} MB"

        file_list = []
        for f in files[:20]:
            file_list.append(f"📄 `{f['name']}` ({format_size(f['size'])})")

        message = f"📁 **Your Downloads**\n\n" + "\n".join(file_list)
        return message

    async def menu_help(self, user_id: str, room_id: str) -> str:
        return self.HELP_MESSAGE

    async def menu_print_all(self, user_id: str, room_id: str) -> str:
        user_dir = self.downloads_dir / user_id
        if not user_dir.exists():
            return "📁 No files found."

        files = await self._list_files_async(user_dir)
        stems_with_doc = {
            Path(f["name"]).stem
            for f in files
            if any(f["name"].lower().endswith(e) for e in CONVERT_TO_PDF_EXTENSIONS)
        }
        results = []
        for f in files:
            p = Path(f["name"])
            if p.suffix.lower() == ".pdf" and p.stem in stems_with_doc:
                results.append(
                    f"⏭️ Skipped {p.name} (same name as a document; already covered when printing the source file)."
                )
                continue
            res = await self.print_file(str(user_dir / f["name"]), user_id)
            results.append(res)

        return "\n".join(results)

    async def menu_print_last(self, user_id: str, room_id: str) -> str:
        user_dir = self.downloads_dir / user_id
        if not user_dir.exists():
            return "📁 No files found."

        files = await self._list_files_async(user_dir)
        if not files:
            return "📁 No files found."

        return await self.print_file(str(user_dir / files[-1]["name"]), user_id)

    async def menu_select_file(self, user_id: str, room_id: str):
        user_dir = self.downloads_dir / user_id
        if not user_dir.exists():
            return "📁 No files found."

        files = await self._list_files_async(user_dir)
        if not files:
            return "📁 No files found."

        options = {}
        for i, f in enumerate(files[:20], 1):
            options[str(i)] = {
                "text": f"📄 {f['name']}",
                "action": "menu_print_selected_file",
                "args": {"filename": f["name"]},
            }

        options["b"] = {"text": "◀ Back", "next_menu": "print_menu"}
        self.dmc.register_callable_menu("file_select", "📁 Select File", options)
        await self.dmc.set_user_menu(user_id, "file_select")
        await self.dmc._display_menu(user_id, room_id, "file_select")
        return False

    async def menu_print_selected_file(
        self, user_id: str, room_id: str, filename: str
    ) -> str:
        user_dir = self.downloads_dir / user_id
        file_path = str(user_dir / filename)

        pages = await self.estimate_pages(file_path, filename)
        if pages > 1:
            await self.rocket.send_message(
                f"✅ Received: {filename}\n🖨️ Type 'all' to print, or 'convert' to PDF.",
                room_id,
            )
            self.pending_jobs[user_id] = {"file_path": file_path, "filename": filename}
            return False

        return await self.print_file(file_path, user_id)

    async def menu_set_paper(self, user_id: str, room_id: str, size: str) -> str:
        await self.update_user_settings(user_id, "paper_size", size)
        return f"✅ Paper size set to: {size}"

    async def menu_set_quality(self, user_id: str, room_id: str, quality: str) -> str:
        await self.update_user_settings(user_id, "quality", quality)
        return f"✅ Print quality set to: {quality}"

    async def menu_set_color(self, user_id: str, room_id: str, color: str) -> str:
        await self.update_user_settings(user_id, "color", color)
        return f"✅ Color mode set to: {color}"

    async def menu_set_copies(self, user_id: str, room_id: str) -> str:
        return "📊 Enter number of copies (1-99):"

    async def menu_convert_last(self, user_id: str, room_id: str) -> str:
        """Convert the most recent (by name sort) file that can become PDF."""
        user_dir = self.downloads_dir / user_id
        if not user_dir.exists():
            return "📁 No files found."

        files = await self._list_files_async(user_dir)
        if not files:
            return "📁 No files found."

        last_convertible = None
        for f in reversed(files):
            low = f["name"].lower()
            if any(low.endswith(e) for e in CONVERT_TO_PDF_EXTENSIONS):
                last_convertible = f
                break

        if not last_convertible:
            for f in reversed(files):
                if f["name"].lower().endswith(".pdf"):
                    return f"✅ Last file is already a PDF: {f['name']}"
            return "📁 No convertible files found. Add a Word, Excel, PPT, OpenDocument, text, or HTML file."

        file_path = str(user_dir / last_convertible["name"])
        before = sibling_pdf_if_any(file_path)
        pdf_path, conv_err = await print_manager.convert_office_to_pdf(file_path)
        if pdf_path:
            if before:
                return f"✅ PDF already present: {Path(pdf_path).name}"
            return f"✅ Converted to PDF: {Path(pdf_path).name}"
        detail = f" {conv_err}" if conv_err else ""
        return f"❌ Conversion failed.{detail}"[:600]

    async def menu_select_file_for_conversion(self, user_id: str, room_id: str):
        """Show selection menu for conversion (convertible formats only)."""
        user_dir = self.downloads_dir / user_id
        if not user_dir.exists():
            return "📁 No files found."

        files = await self._list_files_async(user_dir)
        if not files:
            return "📁 No files found."

        convertible = [
            f
            for f in files
            if any(f["name"].lower().endswith(e) for e in CONVERT_TO_PDF_EXTENSIONS)
        ][:20]
        if not convertible:
            for f in files:
                if f["name"].lower().endswith(".pdf"):
                    return "📁 Only pure PDFs here — add an office or text file to convert."
            return "📁 No files that can be converted to PDF."

        options = {}
        for i, f in enumerate(convertible, 1):
            options[str(i)] = {
                "text": f"📄 {f['name']}",
                "action": "menu_convert_selected_file",
                "args": {"filename": f["name"]},
            }

        options["b"] = {"text": "◀ Back", "next_menu": "print_menu"}
        self.dmc.register_callable_menu(
            "convert_select", "📁 Select File to Convert", options
        )
        await self.dmc.set_user_menu(user_id, "convert_select")
        await self.dmc._display_menu(user_id, room_id, "convert_select")
        return False

    async def menu_convert_selected_file(
        self, user_id: str, room_id: str, filename: str
    ) -> str:
        """Action for menu_select_file_for_conversion"""
        user_dir = self.downloads_dir / user_id
        file_path = str(user_dir / filename)
        before = sibling_pdf_if_any(file_path)
        pdf_path, conv_err = await print_manager.convert_office_to_pdf(file_path)
        if pdf_path:
            if before:
                return f"✅ PDF already present: {Path(pdf_path).name}"
            return f"✅ Converted: {Path(pdf_path).name}"
        detail = f" {conv_err}" if conv_err else ""
        return f"❌ Conversion failed for {filename}.{detail}"[:600]

    async def menu_select_printer(self, user_id: str, room_id: str):
        """Show printer selection menu"""
        printers = await self.get_available_printers()
        if not printers:
            return "📠 No printers found."

        options = {}
        for i, p in enumerate(printers[:20], 1):
            options[str(i)] = {
                "text": p,
                "action": "menu_set_printer",
                "args": {"printer": p},
            }
        options["d"] = {
            "text": "Default Printer",
            "action": self.menu_set_printer,
            "args": {"printer": None},
        }
        options["b"] = {"text": "◀ Back", "next_menu": "settings_menu"}

        self.dmc.register_callable_menu("printer_select", "📠 Select Printer", options)
        await self.dmc.set_user_menu(user_id, "printer_select")
        await self.dmc._display_menu(user_id, room_id, "printer_select")
        return False

    async def menu_set_printer(self, user_id: str, room_id: str, printer: str = None):
        """Set user's preferred printer"""
        await self.update_user_settings(user_id, "printer", printer)
        printer_name = printer if printer else "Default"
        return f"✅ Printer set to: {printer_name}"

    async def menu_show_printers(self, user_id: str, room_id: str) -> str:
        printers = await self.get_available_printers()
        current = (await storage.get_user_settings(user_id)).get("printer")
        current_text = f" (current: {current or 'Default'})"

        printer_list = "\n".join([f"├─ {p}" for p in printers[:20]])
        return f"📠 **Printers**{current_text}\n\n{printer_list}"

    async def menu_show_queue(self, user_id: str, room_id: str) -> str:
        queue = await self.get_print_queue()
        queue_list = "\n".join([f"├─ {q}" for q in queue[:10]])
        return f"📊 **Queue**\n\n{queue_list or 'Empty'}"

    async def menu_cancel_job(self, user_id: str, room_id: str) -> str:
        queue = await self.get_print_queue()
        if not queue:
            return "📁 Print queue is empty."

        options = {}
        # Max 8 jobs for menu readability
        for i, q in enumerate(queue[:8], 1):
            options[str(i)] = {
                "text": f"❌ Cancel {q}",
                "action": self.menu_cancel_selected_job,
                "args": {"job_id": q},
            }

        options["a"] = {"text": "🔥 Cancel All Jobs", "action": self.menu_cancel_all}
        options["b"] = {"text": "◀ Back", "next_menu": "status_menu"}

        self.dmc.register_callable_menu(
            "cancel_select", "🔧 Select Job to Cancel", options
        )
        await self.dmc.set_user_menu(user_id, "cancel_select")
        await self.dmc._display_menu(user_id, room_id, "cancel_select")
        return False

    async def menu_retry_job(self, user_id: str, room_id: str) -> str:
        """Show list of jobs to retry/resume"""
        queue = await print_manager.get_print_queue()
        if not queue:
            return "📁 Print queue is empty."

        options = {}
        for i, q in enumerate(queue[:8], 1):
            options[str(i)] = {
                "text": f"🔄 Resume {q}",
                "action": self.menu_retry_selected_job,
                "args": {"job_id": q},
            }

        options["a"] = {"text": "🚀 Resume All Jobs", "action": self.menu_retry_all}
        options["b"] = {"text": "◀ Back", "next_menu": "status_menu"}

        self.dmc.register_callable_menu(
            "retry_select", "🔧 Select Job to Resume", options
        )
        await self.dmc.set_user_menu(user_id, "retry_select")
        await self.dmc._display_menu(user_id, room_id, "retry_select")
        return False

    async def menu_cancel_selected_job(self, user_id: str, room_id: str, job_id: str):
        """Execute cancellation"""
        # Fix: Route through print_manager
        success = await print_manager.cancel_print_job(job_id)
        if success:
            return f"✅ Successfully cancelled: {job_id}"
        return f"❌ Failed to cancel: {job_id}"

    async def menu_retry_selected_job(self, user_id: str, room_id: str, job_id: str):
        """Execute resume"""
        success = await print_manager.resume_print_job(job_id)
        if success:
            return f"✅ Successfully resumed: {job_id}"
        return f"❌ Failed to resume: {job_id}"

    async def menu_cancel_all(self, user_id: str, room_id: str):
        """Cancel all jobs in queue"""
        queue = await print_manager.get_print_queue()
        count = 0
        for q in queue:
            # Fix: Platform specific strings logic
            match = re.search(r"#(\d+)", q) if os.name == "nt" else (q,)
            target_id = match.group(1) if os.name == "nt" and match else q
            if await print_manager.cancel_print_job(target_id):
                count += 1
        return f"🔥 Cancelled {count} jobs from queue."

    async def menu_retry_all(self, user_id: str, room_id: str):
        """Resume all jobs in queue"""
        queue = await print_manager.get_print_queue()
        count = 0
        for q in queue:
            match = re.search(r"#(\d+)", q) if os.name == "nt" else (q,)
            target_id = match.group(1) if os.name == "nt" and match else q
            if await print_manager.resume_print_job(target_id):
                count += 1
        return f"🚀 Resumed {count} jobs in queue."

    async def menu_remove_file(self, user_id: str, room_id: str):
        """Show file selection menu for removal"""
        user_dir = self.downloads_dir / user_id
        if not user_dir.exists():
            return "📁 No files found."

        files = await self._list_files_async(user_dir)
        if not files:
            return "📁 No files found."

        options = {}
        for i, f in enumerate(files[:20], 1):
            options[str(i)] = {
                "text": f"🗑️ {f['name']}",
                "action": "menu_remove_selected_file",
                "args": {"filename": f["name"]},
            }

        options["b"] = {"text": "◀ Back", "next_menu": "file_menu"}
        self.dmc.register_callable_menu(
            "remove_select", "🗑️ Select File to Remove", options
        )
        await self.dmc.set_user_menu(user_id, "remove_select")
        await self.dmc._display_menu(user_id, room_id, "remove_select")
        return False

    async def menu_remove_selected_file(
        self, user_id: str, room_id: str, filename: str
    ) -> str:
        """Remove the selected file"""
        user_dir = self.downloads_dir / user_id
        file_path = user_dir / filename
        if file_path.exists():
            file_path.unlink()
            return f"✅ Removed: {filename}"
        return f"❌ File not found: {filename}"

    async def menu_remove_all(self, user_id: str, room_id: str) -> str:
        """Remove all files in user's download directory"""
        user_dir = self.downloads_dir / user_id
        if not user_dir.exists():
            return "📁 No files found."

        files = await self._list_files_async(user_dir)
        count = 0
        for f in files:
            file_path = user_dir / f["name"]
            if file_path.exists():
                file_path.unlink()
                count += 1
        return f"🗑️ Removed {count} files."

    async def chat_bot(
        self,
        file={},
        attachments={},
        msg="",
        msg_id=None,
        room_id=None,
        sender_id=None,
        sender=None,
    ):
        try:
            if attachments:
                ra = [dict(ra) for ra in attachments]
                title = ra[0].get("title", "")
                image_link = ra[0].get("title_link", "")

                # Validate file extension
                file_extension = os.path.splitext(title.lower())[1]
                if file_extension not in self.ALLOWED_EXTENSIONS:
                    await self.rocket.send_message(
                        f"❌ Unsupported file type: {file_extension}. "
                        f"Only {', '.join(sorted(self.ALLOWED_EXTENSIONS))} files are allowed.",
                        room_id,
                    )
                    return

                # Rate limiting check
                if not self._check_rate_limit(sender_id):
                    await self.rocket.send_message(
                        "⏳ Please wait a moment before sending another file.", room_id
                    )
                    return

                await self.rocket.send_message(f"📥 Receiving: {title}", room_id)

                # Use a wrapper to handle the download and the subsequent prompt
                asyncio.create_task(
                    self._handle_file_arrival(
                        image_link, title, sender_id, room_id, msg_id
                    )
                )

            elif msg:
                command = msg.lower().strip()

                # Handle pending jobs
                if sender_id in self.pending_jobs:
                    job = self.pending_jobs.pop(sender_id)
                    if command == "all":
                        result = await self.print_file(job["file_path"], sender_id)
                    elif command == "convert":
                        before = sibling_pdf_if_any(job["file_path"])
                        pdf_path, conv_err = await print_manager.convert_office_to_pdf(
                            job["file_path"]
                        )
                        if pdf_path:
                            pr = await self.print_file(pdf_path, sender_id)
                            if before:
                                result = f"✅ PDF ready ({Path(pdf_path).name}). {pr}"
                            else:
                                result = f"✅ Converted to PDF and printing. {pr}"
                        else:
                            detail = f" {conv_err}" if conv_err else ""
                            result = f"❌ Conversion failed.{detail}"[:600]
                    elif re.match(r"^\d+-\d+$", command):
                        result = await self.print_file(
                            job["file_path"], sender_id, page_range=command
                        )
                    else:
                        result = f"❌ Invalid range: {command}"
                    await self.rocket.send_message(result, room_id)
                    return

                # Menu navigation
                if self.dmc and await self.dmc.is_in_menu(sender_id):
                    await self.dmc.menu_worker(sender_id, command, room_id)
                    return

                # Global commands
                if command in ("menu", "m"):
                    await self.dmc.set_user_menu(sender_id, "main")
                    await self.dmc._display_menu(sender_id, room_id, "main")
                    return

                if command in ("help", "h"):
                    await self.rocket.send_message(self.HELP_MESSAGE, room_id)
                    return

                if command == "status":
                    settings = await self.get_user_settings(sender_id)
                    selected_printer = settings.get("printer", "Default")
                    await self.rocket.send_message(
                        f"📊 **Bot Status**\n\n"
                        f"├─ Paper: {settings['paper_size']}\n"
                        f"├─ Quality: {settings['quality']}\n"
                        f"├─ Color: {settings['color']}\n"
                        f"├─ Copies: {settings['copies']}\n"
                        f"├─ Printer: {selected_printer}\n"
                        f"└─ Connected: ✅",
                        room_id,
                    )
                    return

                await self.rocket.send_message(
                    "❌ Unknown command. Type `menu` or `help`", room_id
                )

        except Exception as e:
            logger.error(f"Error in chat_bot: {e}")

    def handle_messages(self, message_data):
        try:
            msg_id = message_data.get("_id")
            if not msg_id:
                return

            sender = message_data.get("u", {}).get("username")
            if sender == self.config.username:
                return

            if message_data.get("t"):
                return

            if msg_id in self._processing_messages:
                return

            self._processing_messages.add(msg_id)

            if len(self._processing_messages) > 1000:
                # Use deque for better dedup
                self._processing_messages = set(list(self._processing_messages)[-800:])

            room_id = message_data.get("rid")
            sender_id = message_data.get("u", {}).get("_id")
            text = message_data.get("msg", "")
            files = message_data.get("files", [])
            attachments = message_data.get("attachments", [])

            asyncio.create_task(
                self.chat_bot(
                    files, attachments, text, msg_id, room_id, sender_id, sender
                )
            )

        except Exception as e:
            logger.error(f"Error handle messages: {e}")

    async def start(self, run_forever: bool = True):
        try:
            self.initialize()
            await self.connect()
            self.channels = await self.rocket.get_channels()

            subscribed_count = 0

            for channel_id, channel_type in self.channels:
                if channel_type != "d":
                    continue

                async with self.mainLock:
                    if channel_id not in self.subscribers:
                        await self.rocket.subscribe_to_channel_messages_raw(
                            channel_id, self.handle_messages
                        )
                        self.subscribers[channel_id] = True
                        subscribed_count += 1
                        logger.info(f"Subscribed to DM: {channel_id}")
                    else:
                        logger.debug(f"Already subscribed to DM: {channel_id}")

            logger.info(f"Total unique DM subscriptions: {subscribed_count}")

            if run_forever:
                logger.info("Bot is running. Press Ctrl+C to stop.")
                await self._shutdown_event.wait()

        except Exception as e:
            logger.error(f"Error in start: {e}")
            raise
