import json
import os
import asyncio
import logging
from typing import Dict, Any, Optional
from .config import config

logger = logging.getLogger(__name__)

class StorageService:
    def __init__(self):
        self.file_path = config.storage_file
        self._data: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._initialized = False

    async def _ensure_initialized(self):
        if not self._initialized:
            async with self._lock:
                if not self._initialized:
                    await self._load()
                    self._initialized = True

    async def _load(self):
        """Load data from JSON file into memory (Async-safe)."""
        def load_sync():
            if os.path.exists(self.file_path):
                try:
                    with open(self.file_path, 'r') as f:
                        return json.load(f)
                except Exception as e:
                    logger.error(f"Failed to load storage: {e}")
            return {}

        self._data = await asyncio.to_thread(load_sync)

    async def _save(self):
        """Save memory data to JSON file (Async-safe)."""
        def save_sync(data):
            try:
                with open(self.file_path, 'w') as f:
                    json.dump(data, f, indent=4)
            except Exception as e:
                logger.error(f"Failed to save storage: {e}")

        await asyncio.to_thread(save_sync, self._data)

    async def get_user_settings(self, user_id: str) -> Dict[str, Any]:
        await self._ensure_initialized()
        return self._data.get(user_id, {
            "paper_size": "A4",
            "quality": "normal",
            "color": "color",
            "copies": 1,
            "duplex": False,
            "printer": None,
        })

    async def update_user_settings(self, user_id: str, key: str, value: Any):
        await self._ensure_initialized()
        async with self._lock:
            if user_id not in self._data:
                self._data[user_id] = await self.get_user_settings(user_id)
            
            self._data[user_id][key] = value
            await self._save()

    async def set_all_user_settings(self, user_id: str, settings: Dict[str, Any]):
        await self._ensure_initialized()
        async with self._lock:
            self._data[user_id] = settings
            await self._save()

# Singleton instance
storage = StorageService()
