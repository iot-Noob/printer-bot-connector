import ssl
import websockets
import asyncio
from rocketchat_async import RocketChat
import urllib3
import os
from dotenv import load_dotenv
from functools import partial
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, HttpUrl
from universal_printer import (
    DocumentPrinter,
    PDFGenerationError,
    PDFValidator,
    PrintingError,
    UniversalPrinterError,
)
import requests
from rocketchat_API.rocketchat import RocketChat as RestRocketChat
# Monkey patch websockets
original_connect = websockets.connect
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from urllib.parse import unquote
from MenuOptionHelper import DynamicMenu
load_dotenv()
from MenuOptionHelper import DynamicMenu

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


class BotConfig(BaseSettings):
    """Bot configuration with validation"""

    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=6)
    server_url: HttpUrl = Field(...)
    ssl_verify: bool = Field(default=False)
    max_queue_size: int = Field(default=1000, ge=1, le=10000)
    poll_interval: float = Field(default=3.0, ge=0.5, le=30.0)
    api_timeout: int = Field(default=30, ge=5, le=120)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ROCKET_",
        extra="ignore",
        validate_default=True,
    )


class PrinterBot:
    def __init__(self, **config):
        self.config = BotConfig(**config)
        self.mainLock = asyncio.Lock()
        self.rocket = None
        self.channels = None
        self.pending_jobs = {}
        # self.rest_client = None
        self.downloads_dir = "downloads"  # ← ADD THIS LINE
        os.makedirs(self.downloads_dir, exist_ok=True)  # ← Create directory
        if not os.path.exists(self.downloads_dir):
            os.makedirs(self.downloads_dir)
            print(f"📁 Created downloads directory: {self.downloads_dir}")
        self.subscribers = {}
        self._processing_messages = set()
        self.dmc=None
    HELP_MESSAGE = (
        "📋 **Available Commands:**\n"
        "├─ 1. `show_dir` - 📁 Show your downloads directory\n"
        "├─ 2. `print` - 🖨️ Print your files\n"
        "└─ h `help` - ❓ Show this help message\n\n"
        "💡 **Example:** `show_dir`"
    )

    def initialize(self):
        """Async initialization - call this before starting"""
        if not self.rocket:
            self.rocket = RocketChat()

            print(f"✅ Bot initialized for: {self.config.username}")
        return self

    async def connect(self):
        try:
            self.initialize()

            # ✅ FIX: Convert HttpUrl to WebSocket URL string
            server_url_str = str(self.config.server_url)
            server_url_str = server_url_str.rstrip("/")

            if server_url_str.startswith("https://"):
                ws_url = server_url_str.replace("https://", "wss://") + "/websocket"
            elif server_url_str.startswith("http://"):
                ws_url = server_url_str.replace("http://", "ws://") + "/websocket"
            else:
                ws_url = f"wss://{server_url_str}/websocket"

            print(f"🔌 Connecting to: {ws_url}")

            await self.rocket.start(
                address=ws_url,
                username=self.config.username,
                password=self.config.password,
            )
            # self.rest_client = RestRocketChat(
            #     user=self.config.username,
            #     password=self.config.password,
            #     server_url=server_url_str,
            #     ssl_verify=False
            # )
            self.dmc=DynamicMenu(self)
            self._register_menus()
            print("✅ Connected!")

        except Exception as e:
            raise ValueError(f"error occur connect: {e}")

  

    async def download_file(
        self, file_link: str, file_name: str, user_data: dict = {}
    ) -> str:
        """Download file using token authentication."""
        try:
            self.initialize()
            server_url = str(self.config.server_url).rstrip("/")
            full_url = f"{server_url}{file_link}"

            udr_sender_id = user_data.get("sender_id", "unknown")
            decoded_name = unquote(file_name)

            # ✅ Create user directory correctly
            user_dir = os.path.join(self.downloads_dir, udr_sender_id)
            os.makedirs(user_dir, exist_ok=True)

            # ✅ Create safe filename (remove invalid characters)
            safe_name = decoded_name.replace("/", "_").replace("\\", "_")
            local_path = os.path.join(user_dir, safe_name)

            print(f"🔍 Download URL: {full_url}")
            print(f"🔍 Saving to: {local_path}")

            # Get auth token
            login_url = f"{server_url}/api/v1/login"
            login_response = requests.post(
                login_url,
                json={"user": self.config.username, "password": self.config.password},
                verify=False,
            )

            if login_response.status_code != 200:
                print(f"❌ Login failed: {login_response.status_code}")
                return None

            login_data = login_response.json()

            # Extract token
            auth_token = login_data.get("authToken")
            user_id = login_data.get("userId")

            if not auth_token and "data" in login_data:
                auth_token = login_data["data"].get("authToken")
                user_id = login_data["data"].get("userId")

            if not auth_token:
                print(f"❌ Could not extract auth token")
                return None

            # Download with token headers
            headers = {"X-Auth-Token": auth_token, "X-User-Id": user_id}

            response = requests.get(
                full_url, headers=headers, verify=False, stream=True
            )

            print(f"🔍 Download Response Status: {response.status_code}")
            print(f"🔍 Content-Type: {response.headers.get('Content-Type', 'unknown')}")

            # ✅ Check if we got an actual file or HTML error page
            content_type = response.headers.get("Content-Type", "")

            if response.status_code == 200 and "html" not in content_type.lower():
                with open(local_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                print(
                    f"✅ Downloaded: {safe_name} ({os.path.getsize(local_path)} bytes)"
                )
                return local_path
            else:
                print(f"❌ Download failed: Got {content_type} instead of file")
                if "html" in content_type.lower():
                    print(
                        "   This usually means authentication failed or file doesn't exist"
                    )
                return None

        except Exception as e:
            print(f"❌ Download error: {e}")
            return None
    
    def _format_size(self, size_bytes: int) -> str:
        """Format file size human-readable."""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
    
    
    def walk_dir(self, path):
        """Get all files in directory."""
        try:
            files = []
            for root, dirs, filenames in os.walk(path):
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    files.append(file_path)
            return files
        except Exception as e:
            print(f"Error walking dir: {e}")
            return []

    async def custom_list(self,user_id):
        try:
            user_dir = os.path.join(self.downloads_dir, user_id)
            if not os.path.exists(user_dir):
                return "📁 No files found. Send me a file!"
            
            files=[]
            

        except Exception as e:
            raise ValueError(f"Error occur show custom list due to {e}")
    
    def _register_menus(self):
        """Register all menus and submenus"""
        
        # Main Menu
        self.dmc.register_callable_menu("main", "📋 Main Menu", {
            "1": {"text": "📁 Show Downloads", "action": self.menu_show_downloads},
            "2": {"text": "🖨️ Print Files", "next_menu": "print_menu"},
            "h": {"text": "❓ Help", "action": self.menu_help},
        })
        
        # Print Submenu
        self.dmc.register_callable_menu("print_menu", "🖨️ Print Menu", {
            "1": {"text": "Print All Files", "action": self.menu_print_all},
            "2": {"text": "Print Last File", "action": self.menu_print_last},
            "3": {"text": "Custom Print File", "next_menu": "custom_print_file"},
            "b": {"text": "◀ Back", "next_menu": "main"},
        })
        self.dmc.register_callable_menu("custom_print_file", "🖨️ Print Menu", {
            "1": {"text": "Print All Files", "action":""},

            "b": {"text": "◀ Back", "next_menu": "main"},
        })
    async def menu_show_downloads(self, user_id: str, room_id: str) -> str:
        """Show user's downloads directory"""
        user_dir = os.path.join(self.downloads_dir, user_id)
        
        if not os.path.exists(user_dir):
            return "📁 No files found. Send me a file!"
        
        files = []
        for f in os.listdir(user_dir):
            f_path = os.path.join(user_dir, f)
            if os.path.isfile(f_path):
                file_size = os.path.getsize(f_path)
                files.append(f"• `{f}` ({self._format_size(file_size)})")
        
        if not files:
            return "📁 Directory is empty."
        
        file_list = "\n".join(files[:20])
        remaining = len(files) - 20
        
        message = f"📁 **Your Downloads**\n\n{file_list}"
        if remaining > 0:
            message += f"\n\n... and {remaining} more files"
        message += f"\n\n📊 Total: {len(files)} files"
        
        return message

    async def menu_help(self, user_id: str, room_id: str) -> str:
        """Show help"""
        return self.HELP_MESSAGE

    async def menu_print_all(self, user_id: str, room_id: str) -> str:
        """Print all files"""
        user_dir = os.path.join(self.downloads_dir, user_id)
        if not os.path.exists(user_dir):
            return "📁 No files found."
        
        files = [f for f in os.listdir(user_dir) if os.path.isfile(os.path.join(user_dir, f))]
        if not files:
            return "📁 No files to print."
        
        # TODO: Add actual printing logic here
        return f"🖨️ Printing {len(files)} file(s)..."

    async def menu_print_last(self, user_id: str, room_id: str) -> str:
        """Print last file"""
        user_dir = os.path.join(self.downloads_dir, user_id)
        if not os.path.exists(user_dir):
            return "📁 No files found."
        
        files = [f for f in os.listdir(user_dir) if os.path.isfile(os.path.join(user_dir, f))]
        if not files:
            return "📁 No files to print."
        
        # TODO: Add actual printing logic here
        return f"🖨️ Printing last file: {files[-1]}"
    async def chat_bot(
        self,
        file: dict = {},
        attachments: dict = {},
        msg: str = "",
        msg_id: str = None,
        room_id: str = None,
        sender_id: str = None,
        sender: str = None,
    ):
        try:
            if attachments:
                ra = [dict(ra) for ra in attachments]

                # print(ra[0],end="\n\n")
                # print(file[0])
                title = ra[0].get("title", "")
                image_link = ra[0].get("title_link", "")
                image_dimention = ra[0].get("image_dimensions", {})
                image_preview = ra[0].get("image_preview", "")
                image_type = ra[0].get("image_type", "")
                description = ra[0].get("description", "")
                asyncio.create_task(
                    self.download_file(
                        image_link,
                        title,
                        user_data={
                            "name": sender,
                            "message_id": msg_id,
                            "room_id": room_id,
                            "sender_id": sender_id,
                        },
                    )
                )
            
            elif msg:
                command = msg.lower().strip()
                handled = await self.dmc.menu_worker(sender_id, command, room_id)
                match command:
                    case "menu":
                        if not handled:
                            await self.dmc.set_user_menu(sender_id, "main")
                            await self.dmc._display_menu(sender_id, room_id, "main")
                    case _:
                        if not handled and not await self.dmc.is_in_menu(sender_id):
                            await self.dmc.set_user_menu(sender_id, "main")
                            await self.dmc._display_menu(sender_id, room_id, "main")
                # handled = await self.dmc.menu_worker(sender_id, command, room_id)
                # match command:

                #     case _:
                #         await self.dmc.set_user_menu(sender_id, "main")
                #         await self.dmc._display_menu(sender_id, room_id, "main")
                # # If not handled (user not in menu), offer to start menu
                # if not handled and command == "menu":
                #     await self.dmc.set_user_menu(sender_id, "main")
                #     await self.dmc._display_menu(sender_id, room_id, "main")
                # elif not handled:
                #     await self.rocket.send_message("Type `menu` to see available commands", room_id)
                # match command:
                #     case "1":
                #         user_dir = os.path.join(self.downloads_dir, sender_id)
                        
                #         # Check if directory exists
                #         if not os.path.exists(user_dir):
                #             await self.rocket.send_message(
                #                 f"📁 **Your Downloads Directory**\n\n"
                #                 f"No files found. Send me a file to get started!",
                #                 room_id
                #             )
                #             return
                        
                #         # Get files
                #         files = []
                #         for f in os.listdir(user_dir):
                #             f_path = os.path.join(user_dir, f)
                #             if os.path.isfile(f_path):
                #                 file_size = os.path.getsize(f_path)
                #                 files.append(f"• `{f}` ({self._format_size(file_size)})")
                        
                #         if not files:
                #             await self.rocket.send_message(
                #                 f"📁 **Your Downloads Directory**\n\n"
                #                 f"Directory exists but is empty.",
                #                 room_id
                #             )
                #             return
                        
                #         # Format message (limit to 20 files to avoid message too long)
                #         file_list = "\n".join(files[:20])
                #         remaining = len(files) - 20
                        
                #         message = f"📁 **Your Downloads Directory**\n\n"
                #         message += file_list
                #         if remaining > 0:
                #             message += f"\n\n... and {remaining} more files"
                #         message += f"\n\n📊 **Total:** {len(files)} files"
                        
                #         await self.rocket.send_message(message, room_id)
                #     case "2":
                #         pass

                #     case "h":
                #         await self.rocket.send_message(
                #             f"❓ **Bot Help**\n\n{self.HELP_MESSAGE}", room_id
                #         )

                #     case _:
                #         await self.rocket.send_message(
                #             f"❌ **Invalid Command**\n\n{self.HELP_MESSAGE}", room_id
                #         )
                

        except Exception as e:
            raise ValueError(f"Error occur chat bot due to {e}")

    # def handle_messages(self,message_data):
    #     try:
    #         msg_id = message_data.get('_id')
    #         room_id = message_data.get('rid')
    #         sender = message_data.get('u', {}).get('username')
    #         sender_id = message_data.get('u', {}).get('_id')
    #         text = message_data.get('msg', '')
    #         files = message_data.get('files', [])
    #         attachments = message_data.get('attachments', [])
    #         ##print([a.get('description') for a in attachments])
    #         asyncio.create_task(
    #             self.chat_bot(files, attachments, text, msg_id, room_id, sender_id, sender)
    #         )
    #     except Exception as e:
    #         raise ValueError(f"Error handle messages due to {e}")
    def handle_messages(self, message_data):
        try:
            msg_id = message_data.get("_id")
            if not msg_id:
                return

            # ✅ 1. Ignore messages from the bot itself (prevent infinite loop)
            sender = message_data.get("u", {}).get("username")
            if sender == self.config.username:
                return

            # ✅ 2. Ignore system messages (e.g. user joined, room changed)
            if message_data.get("t"):
                return

            # ✅ 3. Deduplication: Check if already processing/processed this message
            if msg_id in self._processing_messages:
                return

            self._processing_messages.add(msg_id)

            # Clean up old entries if set gets too large (keep last 1000)
            if len(self._processing_messages) > 1000:
                # Remove some entries to prevent memory growth without clearing everything
                # We convert to list and slice to keep roughly the most recent IDs
                self._processing_messages = set(list(self._processing_messages)[-800:])

            room_id = message_data.get("rid")
            sender_id = message_data.get("u", {}).get("_id")
            text = message_data.get("msg", "")
            files = message_data.get("files", [])
            attachments = message_data.get("attachments", [])

            # ✅ Start processing in background
            asyncio.create_task(
                self.chat_bot(
                    files, attachments, text, msg_id, room_id, sender_id, sender
                )
            )

            # Note: We NO LONGER discard msg_id from _processing_messages here.
            # This ensures that "changed" events for the same message ID won't trigger another response.

        except Exception as e:
            print(f"Error handle messages: {e}")

    # async def start(self,run_forever:bool=True):
    #     try:
    #         self.initialize()
    #         await self.connect()
    #         self.channels=await self.rocket.get_channels()
    #         for channel_id, channel_type in self.channels:
    #             async with self.mainLock:
    #                 if not channel_id in self.subscribers:

    #                     sr=await self.rocket.subscribe_to_channel_messages_raw(channel_id, self.handle_messages)
    #                     self.subscribers[channel_id]=sr
    #             print(f"🔔 Subscribed to channel: {channel_id}")
    #         if run_forever:
    #             await self.rocket.run_forever()
    #     except Exception as e:
    #         raise ValueError(f"Error occur start Rocket Chat due to {e}")
    async def start(self, run_forever: bool = True):
        try:
            self.initialize()
            await self.connect()
            self.channels = await self.rocket.get_channels()

            subscribed_count = 0

            for channel_id, channel_type in self.channels:
                # ✅ Only subscribe to Direct Messages (DM)
                if channel_type != "d":
                    continue

                async with self.mainLock:
                    if channel_id not in self.subscribers:
                        await self.rocket.subscribe_to_channel_messages_raw(
                            channel_id, self.handle_messages
                        )
                        self.subscribers[channel_id] = True
                        subscribed_count += 1
                        print(f"🔔 Subscribed to DM: {channel_id}")
                    else:
                        print(f"⚠️ Already subscribed to DM: {channel_id} - skipping")

            print(f"📊 Total unique DM subscriptions: {subscribed_count}")

            if run_forever:
                print("✅ Bot is running. Press Ctrl+C to stop.")
                await self.rocket.run_forever()

        except Exception as e:
            raise ValueError(f"Error occur start Rocket Chat due to {e}")


async def main():
    try:
        pb = PrinterBot()
        await pb.start(run_forever=True)  # ← Change from connect() to start()
        asyncio.Future()
    except Exception as e:
        print(f"error occur in main due to {e}")


if __name__ == "__main__":
    asyncio.run(main())  # ← Fix this line too
