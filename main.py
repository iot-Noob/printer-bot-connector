import asyncio
import signal
import logging
import sys
from repository.PrinterBot import PrinterBot

# ============================================================================
# RUN-TIME LOGGING SETUP
# ============================================================================
# Alex Chen Style: Structured, multi-handler, and informative.
# Alex Chen Style: Force UTF-8 for console and files to handle emojis on Windows
if os.name == 'nt':
    import io
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8')
    except (AttributeError, io.UnsupportedOperation):
        pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(name)s - %(message)s',
    handlers=[
        logging.FileHandler('printer_bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
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
