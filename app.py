import os
import sys
import time
import threading
import http.server
import socketserver

import worker_main
from worker_main import MockupWorker, logger


def worker_loop():
    """Arranca el worker y lo reinicia si muere."""
    while True:
        try:
            logger.info("🚀 Arrancando MockupWorker...")
            worker = MockupWorker()
            worker.run()
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"💥 Worker terminó con error, reintentando en 5s: {e}", exc_info=True)
            time.sleep(5)


class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.do_GET()

    def log_message(self, *args):
        pass


def main():
    threading.Thread(target=worker_loop, daemon=True).start()

    port = int(os.getenv("PORT", "7860"))
    with socketserver.ThreadingTCPServer(("", port), HealthHandler) as httpd:
        logger.info(f"✅ Health server escuchando en puerto {port}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
