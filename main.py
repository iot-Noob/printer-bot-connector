import asyncio
import signal
import logging
import sys
import os
from repository.PrinterBot import PrinterBot

# ============================================================================
# RUN-TIME LOGGING SETUP
# ============================================================================
# Clear any existing handlers (this kicks out VS Code's hidden crashy handlers)
for h in logging.root.handlers[:]:
    logging.root.removeHandler(h)

# Alex Chen Style: Force UTF-8 for console and files to handle emojis on Windows
if os.name == 'nt':
    import io
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, io.UnsupportedOperation):
        pass

class SafeStreamHandler(logging.StreamHandler):
    """Bypasses the standard logging emit and silences internal error blocks."""
    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream
            try:
                # Try printing normally first
                stream.write(msg + self.terminator)
            except (UnicodeEncodeError, BlockingIOError):
                # Fallback: Force ASCII with replacements for the console
                safe_msg = msg.encode('ascii', 'replace').decode('ascii')
                stream.write(safe_msg + self.terminator)
            self.flush()
        except:
            pass
            
    def handleError(self, record):
        """Prevents '--- Logging error ---' tracebacks from ever appearing."""
        pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(name)s - %(message)s',
    handlers=[
        logging.FileHandler('printer_bot.log', encoding='utf-8'),
        SafeStreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("main")

async def main():
    """
    Principal Entry Point for the Elastic Printer Bot.
    Uses a Supervisor Loop to ensure the bot restarts upon failure.
    """
    backoff = 5
    max_backoff = 60
    
    while True:
        start_time = asyncio.get_event_loop().time()
        bot = None
        
        try:
            logger.info("🚀 Initializing Elastic Printer Bot Connector...")
            bot = PrinterBot()
            
            # Register handlers for Unix
            try:
                loop = asyncio.get_running_loop()
                def signal_handler():
                    logger.info("🛑 Shutdown signal received. Cleaning up...")
                    if bot: asyncio.create_task(bot.shutdown())

                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, signal_handler)
            except (NotImplementedError, AttributeError):
                pass

            await bot.start(run_forever=True)
            
            # If it finishes normally (shutdown requested), exit loop
            break
            
        except KeyboardInterrupt:
            logger.info("⌨️ Keyboard interrupt detected. Exiting...")
            if bot: await bot.shutdown()
            break
            
        except Exception as e:
            logger.error(f"💥 Bot crashed or lost connection: {e}")
            if bot: 
                try:
                    await bot.shutdown()
                except:
                    pass
            
            # Reset backoff if the bot ran successfully for more than 5 minutes
            if asyncio.get_event_loop().time() - start_time > 300:
                backoff = 5
            
            logger.info(f"♻️ Attempting automatic restart in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    logger.info("👋 Bot process concluded.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
