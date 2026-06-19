"""
Railway entrypoint.
Runs the Flask auth/status server in a background thread,
and the trading loop in the main thread.

Railway start command (Procfile): python run.py
"""

import threading
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("run")


def start_web_server():
    from web import run_web
    log.info("[RUN] Starting Flask web server thread...")
    run_web()


def start_trading_loop():
    from main import run_forever
    log.info("[RUN] Starting trading loop...")
    run_forever()


if __name__ == "__main__":
    web_thread = threading.Thread(target=start_web_server, daemon=True)
    web_thread.start()

    # Trading loop runs in main thread (keeps process alive)
    start_trading_loop()
