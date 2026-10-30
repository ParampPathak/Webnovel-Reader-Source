"""Explicit legacy audio worker entry point. No Edge TTS or scraper changes."""
import threading
from app import create_app, start_workers

if __name__ == '__main__':
    application = create_app()
    start_workers(application)
    threading.Event().wait()
