import asyncio
import os
import signal
import logging
import sys
from repository.PrinterBot import PrinterBot

# ============================================================================
# RUN-TIME LOGGING SETUP
# ============================================================================
# Alex Chen Style: Structured, multi-handler, and informative.
if os.name == 'nt':
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


class UTF8ConsoleHandler(logging.StreamHandler):
    """Custom handler that safely writes UTF-8 to Windows console"""
    def emit(self, record):
        try:
            msg = self.format(record) + self.terminator
            # Write UTF-8 directly to binary buffer
            sys.stdout.buffer.write(msg.encode('utf-8', errors='replace'))
            sys.stdout.buffer.flush()
        except Exception:
            self.handleError(record)


# Configure handlers
file_handler = logging.FileHandler('printer_bot.log', encoding='utf-8')
console_handler = UTF8ConsoleHandler()
formatter = logging.Formatter('%(asctime)s - [%(levelname)s] - %(name)s - %(message)s')

file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers.clear()
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

logger = logging.getLogger("main")

async def main():
    """
    Principal Entry Point for the Elastic Printer Bot.
    Handles lifecycle, signals, and non-blocking orchestration.
    """
    logger.info("🚀 Starting Elastic Printer Bot Connector...")
    
    # Initialize the Orchestrator
    bot = PrinterBot()
    
    # Graceful Shutdown Logic
    loop = asyncio.get_running_loop()
    
    def signal_handler():
        logger.info("🛑 Shutdown signal received. Cleaning up...")
        # Create task for shutdown to avoid blocking signal handler
        asyncio.create_task(bot.shutdown())

    # Register handlers (Unix only, Windows handled via KeyboardInterrupt)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)
    except NotImplementedError:
        # Graceful fallback for Windows/environments without signal support
        logger.debug("Signal handlers not supported in this environment.")

    try:
        # Start the bot connector
        await bot.start(run_forever=True)
    except KeyboardInterrupt:
        logger.info("⌨️ Keyboard interrupt. Exiting...")
        await bot.shutdown()
    except Exception as e:
        logger.critical(f"💥 Fatal crash: {e}", exc_info=True)
        await bot.shutdown()
        sys.exit(1)
    finally:
        logger.info("👋 Bot has shut down successfully.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
