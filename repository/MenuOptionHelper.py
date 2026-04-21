"""
DynamicMenu.py - Enterprise Production Ready
Features: TypedDict, Prometheus metrics, Circuit Breaker
"""

import asyncio
import logging
import time
from typing import Dict, Any, Optional, List, Callable, TypedDict, Union
from dataclasses import dataclass
import inspect
from .storage_service import storage

# Prometheus (install: pip install prometheus-client)
try:
    from prometheus_client import Counter, Histogram, Gauge
    METRICS_ENABLED = True
except ImportError:
    METRICS_ENABLED = False
    # Dummy metrics
    class DummyMetric:
        def __init__(self, *args, **kwargs): pass
        def labels(self, *args, **kwargs): return self
        def inc(self, *args, **kwargs): pass
        def observe(self, *args, **kwargs): pass
        def set(self, *args, **kwargs): pass
    Counter = Histogram = Gauge = DummyMetric

logger = logging.getLogger("repository.MenuOptionHelper")


# ============ Strict Typing ============

class MenuOption(TypedDict, total=False):
    """Strict type for menu options"""
    text: str
    action: Union[str, Callable]
    next_menu: str
    args: Dict[str, Any]


class MenuDefinition(TypedDict):
    """Strict type for menu definition"""
    title: str
    options: Dict[str, MenuOption]


class UserSession(TypedDict):
    """Strict type for user session"""
    current_menu: str
    history: List[str]
    last_activity: float
    circuit_breaker_state: Dict[str, int]


# ============ Circuit Breaker ============

@dataclass
class CircuitBreaker:
    """Circuit breaker to prevent repeated failures"""
    name: str
    failure_threshold: int = 5
    recovery_timeout: int = 60
    _failures: int = 0
    _last_failure_time: float = 0
    _state: str = "CLOSED"
    
    def record_failure(self):
        """Record a failure"""
        self._failures += 1
        self._last_failure_time = time.time()
        
        if self._failures >= self.failure_threshold:
            self._state = "OPEN"
            logger.warning(f"Circuit breaker '{self.name}' OPEN after {self._failures} failures")
    
    def record_success(self):
        """Record a success"""
        if self._state == "HALF_OPEN":
            self._state = "CLOSED"
            self._failures = 0
            logger.info(f"Circuit breaker '{self.name}' CLOSED (recovered)")
        elif self._state == "CLOSED":
            self._failures = max(0, self._failures - 1)
    
    def is_allowed(self) -> bool:
        """Check if request is allowed"""
        if self._state == "CLOSED":
            return True
        
        if self._state == "OPEN":
            if time.time() - self._last_failure_time > self.recovery_timeout:
                self._state = "HALF_OPEN"
                logger.info(f"Circuit breaker '{self.name}' HALF_OPEN (testing)")
                return True
            return False
        
        return True
    
    @property
    def state(self) -> str:
        return self._state


# ============ Enterprise DynamicMenu ============

class DynamicMenu:
    """Production-ready dynamic menu with metrics and circuit breakers"""
    
    def __init__(self, bot):
        self.bot = bot
        self.menu_options: Dict[str, MenuDefinition] = {}
        self._circuit_breakers: Dict[str, CircuitBreaker] = {}
        
        # Configuration
        self._session_timeout = 300  # 5 minutes
        self._max_history = 10  # Reduced for overhead
        
        # In-memory transient state (still in memory as it's very transient)
        self._transient_sessions: Dict[str, Dict] = {}
        
        # Metrics setup
        self._menu_hits = Counter('menu_hits_total', 'Total menu hits', ['menu_name', 'option'])
        self._menu_errors = Counter('menu_errors_total', 'Total menu errors', ['error_type'])
        self._menu_latency = Histogram('menu_action_latency_seconds', 'Latency of menu actions', ['action'])
        self._active_sessions = Gauge('active_menu_sessions', 'Active menu sessions')
        
        # Start cleanup task
        self._start_cleanup_task()
    
    async def _get_session(self, user_id: str) -> Optional[Dict]:
        """Get user session from storage or transient memory"""
        if user_id not in self._transient_sessions:
            # We don't necessarily persist the EXACT menu position in storage if it's too frequent,
            # but we'll use a local cache for now.
            return None
        return self._transient_sessions.get(user_id)
    
    async def _set_session(self, user_id: str, session: Dict):
        """Save user session"""
        self._transient_sessions[user_id] = session
    
    def _get_circuit_breaker(self, action_name: str) -> CircuitBreaker:
        """Get or create circuit breaker for an action"""
        if action_name not in self._circuit_breakers:
            self._circuit_breakers[action_name] = CircuitBreaker(action_name)
        return self._circuit_breakers[action_name]
    
    def register_callable_menu(self, menu_name: str, title: str, options: Dict[str, MenuOption]):
        """Register a menu with strict type checking"""
        try:
            for key, opt in options.items():
                if not isinstance(opt, dict):
                    raise TypeError(f"Option '{key}' must be dict, got {type(opt)}")
            
            self.menu_options[menu_name] = {"title": title, "options": options}
            logger.info(f"Menu registered: {menu_name} - {title}")
            return self
        except Exception as e:
            logger.error(f"Failed to register menu {menu_name}: {e}")
            raise
    
    async def set_user_menu(self, user_id: str, menu_name: str, save_history: bool = True) -> bool:
        """Set current menu for a user"""
        try:
            if menu_name not in self.menu_options:
                logger.warning(f"Menu '{menu_name}' not found")
                return False
            
            session = await self._get_session(user_id)
            if not session:
                session = {
                    "current_menu": "",
                    "history": [],
                    "last_activity": time.time(),
                    "circuit_breaker_state": {}
                }
            
            if save_history and session["current_menu"]:
                session["history"].append(session["current_menu"])
                if len(session["history"]) > self._max_history:
                    session["history"].pop(0)
            
            session["current_menu"] = menu_name
            session["last_activity"] = time.time()
            
            await self._set_session(user_id, session)
            return True
            
        except Exception as e:
            logger.error(f"Error setting user menu: {e}")
            return False
    
    async def clear_user_menu(self, user_id: str):
        """Clear user's menu session"""
        self._transient_sessions.pop(user_id, None)
    
    async def is_in_menu(self, user_id: str) -> bool:
        """Check if user is in a menu"""
        session = await self._get_session(user_id)
        return bool(session and session.get("current_menu"))
    
    async def go_back(self, user_id: str) -> Optional[str]:
        """Go back to previous menu"""
        session = await self._get_session(user_id)
        if not session or not session.get("history"):
            return None
        
        previous_menu = session["history"].pop()
        session["current_menu"] = previous_menu
        session["last_activity"] = time.time()
        
        await self._set_session(user_id, session)
        return previous_menu
    
    async def _execute_with_metrics(self, func: Callable, action_name: str, user_id: str, room_id: str, **kwargs):
        """Execute function with latency metrics"""
        start = time.time()
        try:
            cb = self._get_circuit_breaker(action_name)
            if not cb.is_allowed():
                self._menu_errors.labels(error_type="circuit_breaker_open").inc()
                await self._safe_send_message(room_id, "⚠️ Service temporarily unavailable. Please try later.")
                return None
            
            if inspect.iscoroutinefunction(func):
                result = await asyncio.wait_for(
                    func(user_id, room_id, **kwargs),
                    timeout=30
                )
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(func, user_id, room_id, **kwargs),
                    timeout=30
                )
            
            cb.record_success()
            self._menu_latency.labels(action=action_name).observe(time.time() - start)
            return result
            
        except asyncio.TimeoutError:
            cb.record_failure()
            self._menu_errors.labels(error_type="timeout").inc()
            await self._safe_send_message(room_id, "⏰ Operation timed out.")
            return None
        except Exception as e:
            cb.record_failure()
            self._menu_errors.labels(error_type="execution").inc()
            logger.error(f"Error in {action_name}: {e}")
            await self._safe_send_message(room_id, f"❌ Error: {str(e)[:100]}")
            return None
    
    async def _safe_send_message(self, room_id: str, message: str):
        """Safely send message"""
        try:
            await self.bot.rocket.send_message(message, room_id)
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
    
    async def menu_worker(self, user_id: str, command: str, room_id: str) -> bool:
        """Process menu commands with hardening"""
        start_time = time.time()
        command = command.strip().lower()
        
        try:
            session = await self._get_session(user_id)
            if not session:
                return False
            
            current_menu = session.get("current_menu")
            if not current_menu:
                return False
            
            menu = self.menu_options.get(current_menu)
            if not menu:
                return False
            
            session["last_activity"] = time.time()
            await self._set_session(user_id, session)
            
            self._menu_hits.labels(menu_name=current_menu, option=command).inc()
            
            if command == "x":
                await self.clear_user_menu(user_id)
                await self._safe_send_message(room_id, "👋 Exited menu.")
                return True
            
            if command == "b":
                previous = await self.go_back(user_id)
                if previous:
                    await self._display_menu(user_id, room_id, previous)
                else:
                    await self.clear_user_menu(user_id)
                    await self._safe_send_message(room_id, "👋 No previous menu. Exited.")
                return True
            
            option = menu.get("options", {}).get(command)
            if not option:
                await self._safe_send_message(room_id, f"❌ Invalid option: {command}")
                await self._display_menu(user_id, room_id, current_menu)
                return True
            
            if "next_menu" in option:
                await self.set_user_menu(user_id, option["next_menu"])
                await self._display_menu(user_id, room_id, option["next_menu"])
                return True
            
            action = option.get("action")
            if action:
                action_name = action if isinstance(action, str) else action.__name__
                
                result = await self._execute_with_metrics(
                    action if callable(action) else getattr(self.bot, action),
                    action_name,
                    user_id, room_id,
                    **option.get("args", {})
                )
                
                if result is not False:
                    await self.clear_user_menu(user_id)
                
                if result and isinstance(result, str):
                    await self._safe_send_message(room_id, result)
                
                return True
            
            return False
            
        finally:
            pass
    
    async def _display_menu(self, user_id: str, room_id: str, menu_name: str):
        """Display menu"""
        try:
            menu = self.menu_options.get(menu_name)
            if not menu:
                return
            
            title = menu.get("title", menu_name)
            message = f"**{title}**\n\n"
            
            for key, opt in menu.get("options", {}).items():
                text = opt.get("text", key)
                message += f"├─ `{key}` - {text}\n"
            
            message += "\n└─ `b` - Back | `x` - Exit"
            
            await self._safe_send_message(room_id, message)
            
        except Exception as e:
            logger.error(f"Error displaying menu: {e}")
    
    def _start_cleanup_task(self):
        """Start background cleanup task with hardening"""
        async def cleanup():
            while True:
                try:
                    await asyncio.sleep(60)
                    now = time.time()
                    to_delete = []
                    for user_id, session in self._transient_sessions.items():
                        if now - session.get("last_activity", 0) > self._session_timeout:
                            to_delete.append(user_id)
                    for user_id in to_delete:
                        del self._transient_sessions[user_id]
                    self._update_metrics()
                except Exception as e:
                    logger.error(f"Menu cleanup error: {e}")
                    await asyncio.sleep(10)
        
        asyncio.create_task(cleanup())
    def _update_metrics(self):
        """Update global metrics"""
        self._active_sessions.set(len(self._transient_sessions))
