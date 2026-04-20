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
from universal_printer import DocumentPrinter, UniversalPrinterError
import requests
from rocketchat_API.rocketchat import RocketChat as RestRocketChat

# Monkey patch websockets
original_connect = websockets.connect
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from urllib.parse import unquote
from MenuOptionHelper import DynamicMenu

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


class BotConfig(BaseSettings):
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
        self.downloads_dir = "downloads"
        os.makedirs(self.downloads_dir, exist_ok=True)
        if not os.path.exists(self.downloads_dir):
            os.makedirs(self.downloads_dir)
            print(f"📁 Created downloads directory: {self.downloads_dir}")
        self.subscribers = {}
        self._processing_messages = set()
        self.dmc = None
        
        # Initialize universal printer
        self.printer = DocumentPrinter()
        
        # User settings storage
        self.user_settings = {}  # user_id -> settings
        self.print_queue = {}    # user_id -> list of queued jobs
        
    HELP_MESSAGE = (
        "📋 **Available Commands:**\n"
        "├─ `menu` - Open main menu\n"
        "├─ `help` - Show this help\n"
        "└─ `status` - Check bot status"
    )

    def initialize(self):
        if not self.rocket:
            self.rocket = RocketChat()
            print(f"✅ Bot initialized for: {self.config.username}")
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

            print(f"🔌 Connecting to: {ws_url}")

            await self.rocket.start(
                address=ws_url,
                username=self.config.username,
                password=self.config.password,
            )
            
            self.dmc = DynamicMenu(self)
            self._register_menus()
            print("✅ Connected!")

        except Exception as e:
            raise ValueError(f"error occur connect: {e}")

    async def download_file(self, file_link: str, file_name: str, user_data: dict = {}) -> str:
        """Download file using token authentication."""
        try:
            server_url = str(self.config.server_url).rstrip("/")
            full_url = f"{server_url}{file_link}"

            udr_sender_id = user_data.get("sender_id", "unknown")
            decoded_name = unquote(file_name)

            user_dir = os.path.join(self.downloads_dir, udr_sender_id)
            os.makedirs(user_dir, exist_ok=True)

            safe_name = decoded_name.replace("/", "_").replace("\\", "_")
            local_path = os.path.join(user_dir, safe_name)

            print(f"🔍 Download URL: {full_url}")
            print(f"🔍 Saving to: {local_path}")

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

            auth_token = login_data.get("authToken")
            user_id = login_data.get("userId")

            if not auth_token and "data" in login_data:
                auth_token = login_data["data"].get("authToken")
                user_id = login_data["data"].get("userId")

            if not auth_token:
                print(f"❌ Could not extract auth token")
                return None

            headers = {"X-Auth-Token": auth_token, "X-User-Id": user_id}

            response = requests.get(full_url, headers=headers, verify=False, stream=True)

            print(f"🔍 Download Response Status: {response.status_code}")
            print(f"🔍 Content-Type: {response.headers.get('Content-Type', 'unknown')}")

            content_type = response.headers.get("Content-Type", "")

            if response.status_code == 200 and "html" not in content_type.lower():
                with open(local_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                print(f"✅ Downloaded: {safe_name} ({os.path.getsize(local_path)} bytes)")
                return local_path
            else:
                print(f"❌ Download failed: Got {content_type} instead of file")
                if "html" in content_type.lower():
                    print("This usually means authentication failed or file doesn't exist")
                return None

        except Exception as e:
            print(f"❌ Download error: {e}")
            return None
    
    def _format_size(self, size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
    
    def get_file_type(self, filename: str) -> str:
        """Determine file type from extension"""
        ext = filename.lower().split('.')[-1] if '.' in filename else ''
        
        # PDF
        if ext in ['pdf']:
            return 'pdf'
        
        # Images
        elif ext in ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'tiff', 'svg']:
            return 'image'
        
        # Word documents
        elif ext in ['doc', 'docx']:
            return 'word'
        
        # Excel documents
        elif ext in ['xls', 'xlsx', 'xlsm', 'csv']:
            return 'excel'
        
        # PowerPoint
        elif ext in ['ppt', 'pptx']:
            return 'powerpoint'
        
        # Text files
        elif ext in ['txt', 'md', 'py', 'json', 'xml', 'html', 'css', 'js']:
            return 'text'
        
        # Other
        else:
            return 'unknown'
    
    def estimate_pages(self, file_path: str, file_name: str) -> int:
        """Estimate number of pages in a file"""
        file_type = self.get_file_type(file_name)
        
        if file_type == 'pdf':
            try:
                import PyPDF2
                with open(file_path, 'rb') as f:
                    reader = PyPDF2.PdfReader(f)
                    return len(reader.pages)
            except:
                return 1
        elif file_type == 'image':
            return 1
        elif file_type == 'text':
            try:
                with open(file_path, 'r') as f:
                    lines = len(f.readlines())
                    return max(1, lines // 50)
            except:
                return 1
        else:
            return 1
    
    def get_user_settings(self, user_id: str) -> dict:
        """Get user's printer settings"""
        if user_id not in self.user_settings:
            self.user_settings[user_id] = {
                "paper_size": "A4",
                "quality": "normal",
                "color": "color",
                "copies": 1,
                "duplex": False
            }
        return self.user_settings[user_id]
    
    def convert_to_pdf(self, file_path: str) -> str:
        """Convert document to PDF using LibreOffice"""
        try:
            # Create output directory
            output_dir = os.path.dirname(file_path)
            output_pdf = file_path.rsplit('.', 1)[0] + '.pdf'
            
            # Use LibreOffice for conversion
            cmd = [
                'libreoffice', '--headless', '--convert-to', 'pdf',
                '--outdir', output_dir, file_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0 and os.path.exists(output_pdf):
                return output_pdf
            else:
                print(f"Conversion failed: {result.stderr}")
                return None
                
        except Exception as e:
            print(f"Conversion error: {e}")
            return None

    def print_with_libreoffice(self, file_path: str, settings: dict, copies: int) -> bool:
        """Print using LibreOffice (for DOCX, XLSX, PPTX)"""
        try:
            # LibreOffice print command
            cmd = [
                'libreoffice', '--headless', '--print-to-file',
                '--printer-name', 'default',
                '--outdir', '/tmp', file_path
            ]
            
            for i in range(copies):
                subprocess.run(cmd, capture_output=True)
            
            return True
        except Exception as e:
            print(f"LibreOffice print error: {e}")
            return False
    
    def update_user_settings(self, user_id: str, key: str, value):
        """Update user's printer settings"""
        settings = self.get_user_settings(user_id)
        settings[key] = value
        self.user_settings[user_id] = settings
    
    def get_available_printers(self) -> list:
        """Get list of available printers"""
        try:
            if os.name == 'nt':  # Windows
                result = subprocess.run(['wmic', 'printer', 'get', 'name'], capture_output=True, text=True)
                printers = [p.strip() for p in result.stdout.split('\n')[1:] if p.strip()]
                return printers
            else:  # Linux/Mac
                result = subprocess.run(['lpstat', '-e'], capture_output=True, text=True)
                printers = [p.strip() for p in result.stdout.split('\n') if p.strip()]
                return printers
        except:
            return ["Default Printer"]
    
    def get_print_queue(self, user_id: str = None) -> list:
        """Get print queue status"""
        try:
            if os.name == 'nt':  # Windows
                result = subprocess.run(['wmic', 'printjob', 'get', 'document,status'], capture_output=True, text=True)
                jobs = [j.strip() for j in result.stdout.split('\n')[1:] if j.strip()]
                return jobs
            else:  # Linux/Mac
                result = subprocess.run(['lpstat', '-o'], capture_output=True, text=True)
                jobs = [j.strip() for j in result.stdout.split('\n') if j.strip()]
                return jobs
        except:
            return ["No jobs in queue"]
    
    def cancel_print_job(self, job_id: str) -> bool:
        """Cancel a print job"""
        try:
            if os.name == 'nt':  # Windows
                subprocess.run(['cancel', job_id], capture_output=True)
            else:  # Linux/Mac
                subprocess.run(['cancel', job_id], capture_output=True)
            return True
        except:
            return False
    
    def print_file(self, file_path: str, user_id: str, page_range: str = None) -> str:
        """Print a file - handles all document formats"""
        try:
            settings = self.get_user_settings(user_id)
            copies = settings.get("copies", 1)
            file_type = self.get_file_type(file_path)
            
            # Check if file exists
            if not os.path.exists(file_path):
                return f"❌ File not found: {os.path.basename(file_path)}"
            
            # Convert non-PDF documents to PDF first
            pdf_path = None
            if file_type in ['word', 'excel', 'powerpoint']:
                # Convert to PDF using LibreOffice
                pdf_path = self.convert_to_pdf(file_path)
                if not pdf_path:
                    return f"❌ Could not convert {os.path.basename(file_path)} to PDF. Please install LibreOffice."
                print_file = pdf_path
            else:
                print_file = file_path
            
            # Print using system commands
            if os.name == 'nt':  # Windows
                for i in range(copies):
                    subprocess.run([
                        'powershell', '-Command',
                        f'Start-Process -FilePath "{print_file}" -Verb Print -WindowStyle Hidden'
                    ], capture_output=True)
            else:  # Linux/Mac with CUPS
                cmd = ['lp']
                
                # Add copies
                if copies > 1:
                    cmd.extend(['-n', str(copies)])
                
                # Add page range
                if page_range:
                    cmd.extend(['-o', f'page-ranges={page_range}'])
                
                # Add file
                cmd.append(print_file)
                
                result = subprocess.run(cmd, capture_output=True)
                if result.returncode != 0:
                    error_msg = result.stderr.decode()
                    # Clean up converted PDF if it was created
                    if pdf_path and os.path.exists(pdf_path):
                        os.remove(pdf_path)
                    return f"❌ Print failed: {error_msg}"
            
            # Clean up temporary PDF if converted
            if pdf_path and os.path.exists(pdf_path) and pdf_path != file_path:
                os.remove(pdf_path)
            
            copy_text = f"({copies} copies)" if copies > 1 else ""
            return f"✅ Printed: {os.path.basename(file_path)} {copy_text}"
            
        except Exception as e:
            return f"❌ Print error: {e}"

    def _register_menus(self):
        """Register all menus and submenus"""
        
        # Main Menu
        self.dmc.register_callable_menu("main", "📋 Main Menu", {
            "1": {"text": "📁 Show Downloads", "action": self.menu_show_downloads},
            "2": {"text": "🖨️ Print Files", "next_menu": "print_menu"},
            "3": {"text": "⚙️ Printer Settings", "next_menu": "settings_menu"},
            "4": {"text": "📊 Printer Status", "next_menu": "status_menu"},
            "h": {"text": "❓ Help", "action": self.menu_help},
        })
        
        # Print Submenu
        self.dmc.register_callable_menu("print_menu", "🖨️ Print Menu", {
            "1": {"text": "Print All Files", "action": self.menu_print_all},
            "2": {"text": "Print Last File", "action": self.menu_print_last},
            "3": {"text": "Select File to Print", "action": self.menu_select_file},
            "b": {"text": "◀ Back", "next_menu": "main"},
        })
        
        # Settings Menu
        self.dmc.register_callable_menu("settings_menu", "⚙️ Printer Settings", {
            "1": {"text": "Paper Size", "next_menu": "paper_menu"},
            "2": {"text": "Print Quality", "next_menu": "quality_menu"},
            "3": {"text": "Color Mode", "next_menu": "color_menu"},
            "4": {"text": "Copies", "action": self.menu_set_copies},
            "b": {"text": "◀ Back", "next_menu": "main"},
        })
        
        # Paper Size Menu
        self.dmc.register_callable_menu("paper_menu", "📄 Paper Size", {
            "1": {"text": "A4", "action": self.menu_set_paper, "args": {"size": "A4"}},
            "2": {"text": "Letter", "action": self.menu_set_paper, "args": {"size": "Letter"}},
            "3": {"text": "Legal", "action": self.menu_set_paper, "args": {"size": "Legal"}},
            "b": {"text": "◀ Back", "next_menu": "settings_menu"},
        })
        
        # Quality Menu
        self.dmc.register_callable_menu("quality_menu", "🎨 Print Quality", {
            "1": {"text": "Draft", "action": self.menu_set_quality, "args": {"quality": "draft"}},
            "2": {"text": "Normal", "action": self.menu_set_quality, "args": {"quality": "normal"}},
            "3": {"text": "High", "action": self.menu_set_quality, "args": {"quality": "high"}},
            "b": {"text": "◀ Back", "next_menu": "settings_menu"},
        })
        
        # Color Mode Menu
        self.dmc.register_callable_menu("color_menu", "🎨 Color Mode", {
            "1": {"text": "Color", "action": self.menu_set_color, "args": {"color": "color"}},
            "2": {"text": "Black & White", "action": self.menu_set_color, "args": {"color": "grayscale"}},
            "b": {"text": "◀ Back", "next_menu": "settings_menu"},
        })
        
        # Status Menu
        self.dmc.register_callable_menu("status_menu", "📊 Printer Status", {
            "1": {"text": "Available Printers", "action": self.menu_show_printers},
            "2": {"text": "Print Queue", "action": self.menu_show_queue},
            "3": {"text": "Cancel Job", "action": self.menu_cancel_job},
            "b": {"text": "◀ Back", "next_menu": "main"},
        })
    
    # ============ Menu Action Methods ============
    
    async def menu_show_downloads(self, user_id: str, room_id: str) -> str:
        """Show user's downloads directory with file type icons"""
        user_dir = os.path.join(self.downloads_dir, user_id)
        
        if not os.path.exists(user_dir):
            return "📁 No files found. Send me a file!"
        
        files = []
        for f in os.listdir(user_dir):
            f_path = os.path.join(user_dir, f)
            if os.path.isfile(f_path):
                file_size = os.path.getsize(f_path)
                file_type = self.get_file_type(f)
                
                # Choose emoji based on file type
                emoji = "📄" if file_type == 'pdf' else "🖼️" if file_type == 'image' else "📝" if file_type == 'text' else "📊" if file_type in ['word', 'excel', 'powerpoint'] else "📁"
                
                files.append(f"{emoji} `{f}` ({self._format_size(file_size)})")
        
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
        
        results = []
        for f in files:
            file_path = os.path.join(user_dir, f)
            file_type = self.get_file_type(f)
            
            if file_type in ['pdf', 'image', 'text']:
                result = self.print_file(file_path, user_id)
                results.append(result)
            else:
                results.append(f"⚠️ Skipped {f} (unsupported format)")
        
        return "\n".join(results)

    async def menu_print_last(self, user_id: str, room_id: str) -> str:
        """Print last file"""
        user_dir = os.path.join(self.downloads_dir, user_id)
        if not os.path.exists(user_dir):
            return "📁 No files found."
        
        files = [f for f in os.listdir(user_dir) if os.path.isfile(os.path.join(user_dir, f))]
        if not files:
            return "📁 No files to print."
        
        last_file = files[-1]
        file_path = os.path.join(user_dir, last_file)
        file_type = self.get_file_type(last_file)
        
        if file_type not in ['pdf', 'image', 'text']:
            return f"❌ Unsupported file format: {last_file}"
        
        return self.print_file(file_path, user_id)

    async def menu_select_file(self, user_id: str, room_id: str):
        """Select file to print from list"""
        user_dir = os.path.join(self.downloads_dir, user_id)
        if not os.path.exists(user_dir):
            await self.rocket.send_message("📁 No files found.", room_id)
            return False
        
        files = [f for f in os.listdir(user_dir) if os.path.isfile(os.path.join(user_dir, f))]
        if not files:
            await self.rocket.send_message("📁 No files to print.", room_id)
            return False
        
        # Build dynamic file selection menu with proper icons
        options = {}
        for i, f in enumerate(files[:20], 1):
            file_type = self.get_file_type(f)
            
            # Choose emoji based on file type
            if file_type == 'pdf':
                emoji = "📄"
            elif file_type == 'image':
                emoji = "🖼️"
            elif file_type == 'word':
                emoji = "📝"
            elif file_type == 'excel':
                emoji = "📊"
            elif file_type == 'powerpoint':
                emoji = "📽️"
            elif file_type == 'text':
                emoji = "📃"
            else:
                emoji = "📁"
            
            options[str(i)] = {
                "text": f"{emoji} {f}",
                "action": self.menu_print_selected_file,
                "args": {"filename": f}
            }
        
        options["a"] = {"text": "📚 All Files", "action": self.menu_print_all}
        options["b"] = {"text": "◀ Back", "next_menu": "print_menu"}
        
        self.dmc.register_callable_menu("file_select", "📁 Select File to Print", options)
        await self.dmc.set_user_menu(user_id, "file_select")
        await self.dmc._display_menu(user_id, room_id, "file_select")
        return False

    async def menu_print_selected_file(self, user_id: str, room_id: str, filename: str) -> str:
        """Print selected file"""
        user_dir = os.path.join(self.downloads_dir, user_id)
        file_path = os.path.join(user_dir, filename)
        
        if not os.path.exists(file_path):
            return "❌ File not found."
        
        file_type = self.get_file_type(filename)
        if file_type not in ['pdf', 'image', 'text']:
            return f"❌ Unsupported file format: {filename}"
        
        # Ask for page range
        pages = self.estimate_pages(file_path, filename)
        if pages > 1:
            await self.rocket.send_message(f"📄 {filename} has {pages} pages.\nReply with page range (e.g., `1-5` or `all`):", room_id)
            # Store for callback
            self.pending_jobs[user_id] = {"file_path": file_path, "filename": filename}
            return False
        
        return self.print_file(file_path, user_id)

    async def menu_set_paper(self, user_id: str, room_id: str, size: str) -> str:
        """Set paper size"""
        self.update_user_settings(user_id, "paper_size", size)
        return f"✅ Paper size set to: {size}"

    async def menu_set_quality(self, user_id: str, room_id: str, quality: str) -> str:
        """Set print quality"""
        self.update_user_settings(user_id, "quality", quality)
        return f"✅ Print quality set to: {quality}"

    async def menu_set_color(self, user_id: str, room_id: str, color: str) -> str:
        """Set color mode"""
        self.update_user_settings(user_id, "color", color)
        return f"✅ Color mode set to: {color}"

    async def menu_set_copies(self, user_id: str, room_id: str) -> str:
        """Set number of copies"""
        return "📊 Enter number of copies (1-99):"

    async def menu_show_printers(self, user_id: str, room_id: str) -> str:
        """Show available printers"""
        printers = self.get_available_printers()
        if not printers:
            return "📠 No printers found."
        
        printer_list = "\n".join([f"├─ {p}" for p in printers[:10]])
        return f"📠 **Available Printers**\n\n{printer_list}"

    async def menu_show_queue(self, user_id: str, room_id: str) -> str:
        """Show print queue"""
        queue = self.get_print_queue()
        if not queue:
            return "📊 Print queue is empty."
        
        queue_list = "\n".join([f"├─ {q}" for q in queue[:10]])
        return f"📊 **Print Queue**\n\n{queue_list}"

    async def menu_cancel_job(self, user_id: str, room_id: str) -> str:
        """Cancel a print job"""
        return "🔧 Enter job ID to cancel (or 'all' for all jobs):"

    async def chat_bot(self, file={}, attachments={}, msg="", msg_id=None, room_id=None, sender_id=None, sender=None):
        try:
            if attachments:
                ra = [dict(ra) for ra in attachments]
                title = ra[0].get("title", "")
                image_link = ra[0].get("title_link", "")
                
                # Send immediate acknowledgment
                await self.rocket.send_message(f"📥 Receiving: {title}", room_id)
                
                # Download file
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
                
                # Handle pending jobs (page range response)
                if sender_id in self.pending_jobs:
                    job = self.pending_jobs.pop(sender_id)
                    if command == "all":
                        result = self.print_file(job["file_path"], sender_id)
                    else:
                        import re
                        if re.match(r'^\d+-\d+$', command):
                            result = self.print_file(job["file_path"], sender_id, page_range=command)
                        else:
                            result = f"❌ Invalid page range: {command}"
                    await self.rocket.send_message(result, room_id)
                    return
                
                # Check if user is in menu
                if self.dmc and await self.dmc.is_in_menu(sender_id):
                    await self.dmc.menu_worker(sender_id, command, room_id)
                    return
                
                # Handle "menu" command
                if command == "menu" or command == "m":
                    await self.dmc.set_user_menu(sender_id, "main")
                    await self.dmc._display_menu(sender_id, room_id, "main")
                    return
                
                # Handle "help" command
                if command == "help" or command == "h":
                    await self.rocket.send_message(self.HELP_MESSAGE, room_id)
                    return
                
                # Handle "status" command
                if command == "status":
                    settings = self.get_user_settings(sender_id)
                    await self.rocket.send_message(
                        f"📊 **Bot Status**\n\n"
                        f"├─ Paper: {settings['paper_size']}\n"
                        f"├─ Quality: {settings['quality']}\n"
                        f"├─ Color: {settings['color']}\n"
                        f"├─ Copies: {settings['copies']}\n"
                        f"└─ Connected: ✅",
                        room_id
                    )
                    return
                
                # Handle copies input (only when NOT in menu)
                if command.isdigit() and 1 <= int(command) <= 99:
                    self.update_user_settings(sender_id, "copies", int(command))
                    await self.rocket.send_message(f"✅ Copies set to: {command}", room_id)
                    return
                
                # Unknown command
                await self.rocket.send_message("❌ Unknown command. Type `menu` or `help`", room_id)
        
        except Exception as e:
            print(f"Error in chat_bot: {e}")

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
            print(f"Error handle messages: {e}")

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
        await pb.start(run_forever=True)
    except Exception as e:
        print(f"error occur in main due to {e}")


if __name__ == "__main__":
    asyncio.run(main())