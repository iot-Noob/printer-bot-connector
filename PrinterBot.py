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
from universal_printer import DocumentPrinter,PDFGenerationError,PDFValidator,PrintingError,UniversalPrinterError
import requests
from rocketchat_API.rocketchat import RocketChat as RestRocketChat
# Monkey patch websockets
original_connect = websockets.connect
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from urllib.parse import unquote
load_dotenv()
def patched_connect(uri, **kwargs):
    kwargs['ssl'] = kwargs.get('ssl', True)
    if isinstance(kwargs['ssl'], bool) and kwargs['ssl']:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        kwargs['ssl'] = ssl_context
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

    def __init__(self,**config):
        self.config=BotConfig(**config)
        self.rocket=None
        self.channels=None
        self.pending_jobs = {}
        # self.rest_client = None
        self.downloads_dir = "downloads"  # ← ADD THIS LINE
        os.makedirs(self.downloads_dir, exist_ok=True)  # ← Create directory
        if not os.path.exists(self.downloads_dir):
            os.makedirs(self.downloads_dir)
            print(f"📁 Created downloads directory: {self.downloads_dir}")
    def initialize(self):
        """Async initialization - call this before starting"""
        if not self.rocket:
            self.rocket =RocketChat()
            
            print(f"✅ Bot initialized for: {self.config.username}")
        return self
    
    async def connect(self):
        try:
            self.initialize()
            
            # ✅ FIX: Convert HttpUrl to WebSocket URL string
            server_url_str = str(self.config.server_url)
            server_url_str = server_url_str.rstrip('/')
            
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
                password=self.config.password
            )
            # self.rest_client = RestRocketChat(
            #     user=self.config.username,
            #     password=self.config.password,
            #     server_url=server_url_str,
            #     ssl_verify=False
            # )
            print("✅ Connected!")
            
        except Exception as e:
            raise ValueError(f"error occur connect: {e}")


    async def download_file(self, file_link: str, file_name: str, user_data: dict = {}) -> str:
        """Download file using token authentication."""
        try:
            server_url = str(self.config.server_url).rstrip('/')
            full_url = f"{server_url}{file_link}"
            
            udr_sender_id = user_data.get('sender_id', 'unknown')
            decoded_name = unquote(file_name)
            
            # ✅ Create user directory correctly
            user_dir = os.path.join(self.downloads_dir, udr_sender_id)
            os.makedirs(user_dir, exist_ok=True)
            
            # ✅ Create safe filename (remove invalid characters)
            safe_name = decoded_name.replace('/', '_').replace('\\', '_')
            local_path = os.path.join(user_dir, safe_name)
            
            print(f"🔍 Download URL: {full_url}")
            print(f"🔍 Saving to: {local_path}")
            
            # Get auth token
            login_url = f"{server_url}/api/v1/login"
            login_response = requests.post(
                login_url,
                json={"user": self.config.username, "password": self.config.password},
                verify=False
            )
            
            if login_response.status_code != 200:
                print(f"❌ Login failed: {login_response.status_code}")
                return None
            
            login_data = login_response.json()
            
            # Extract token
            auth_token = login_data.get('authToken')
            user_id = login_data.get('userId')
            
            if not auth_token and 'data' in login_data:
                auth_token = login_data['data'].get('authToken')
                user_id = login_data['data'].get('userId')
            
            if not auth_token:
                print(f"❌ Could not extract auth token")
                return None
            
            # Download with token headers
            headers = {
                "X-Auth-Token": auth_token,
                "X-User-Id": user_id
            }
            
            response = requests.get(full_url, headers=headers, verify=False, stream=True)
            
            print(f"🔍 Download Response Status: {response.status_code}")
            print(f"🔍 Content-Type: {response.headers.get('Content-Type', 'unknown')}")
            
            # ✅ Check if we got an actual file or HTML error page
            content_type = response.headers.get('Content-Type', '')
            
            if response.status_code == 200 and 'html' not in content_type.lower():
                with open(local_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                print(f"✅ Downloaded: {safe_name} ({os.path.getsize(local_path)} bytes)")
                return local_path
            else:
                print(f"❌ Download failed: Got {content_type} instead of file")
                if 'html' in content_type.lower():
                    print("   This usually means authentication failed or file doesn't exist")
                return None
                        
        except Exception as e:
            print(f"❌ Download error: {e}")
            return None
    def chat_bot(self,file:dict={},attachments:dict={},msg:str='',msg_id:str=None,room_id:str=None,sender_id:str=None,sender:str=None):
        try:
            if attachments:
                ra=[dict(ra) for ra in attachments]
                
                # print(ra[0],end="\n\n")
                # print(file[0])
                title=ra[0].get('title','') 
                image_link=ra[0].get('title_link','')
                image_dimention=ra[0].get('image_dimensions',{})
                image_preview=ra[0].get('image_preview','')
                image_type=ra[0].get('image_type','')
                description=ra[0].get('description','')
                asyncio.create_task(self.download_file(image_link,title,user_data={"name":sender,"message_id":msg_id,"room_id":room_id,"sender_id":sender_id}))
            elif msg:   
                pass
            
        except Exception as e:
            raise ValueError(f"Error occur chat bot due to {e}")

    def handle_messages(self,message_data):
        try:
            msg_id = message_data.get('_id')
            room_id = message_data.get('rid')
            sender = message_data.get('u', {}).get('username')
            sender_id = message_data.get('u', {}).get('_id')
            text = message_data.get('msg', '')
            files = message_data.get('files', [])
            attachments = message_data.get('attachments', [])
            ##print([a.get('description') for a in attachments])
            self.chat_bot(files,attachments,text,msg_id,room_id,sender_id,sender)
        except Exception as e:      
            raise ValueError(f"Error handle messages due to {e}") 

    async def start(self,run_forever:bool=True):
        try:
            self.initialize()
            await self.connect()
            self.channels=await self.rocket.get_channels()
            for channel_id, channel_type in self.channels:
                await self.rocket.subscribe_to_channel_messages_raw(channel_id, self.handle_messages)
                print(f"🔔 Subscribed to channel: {channel_id}")
            if run_forever:
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
 