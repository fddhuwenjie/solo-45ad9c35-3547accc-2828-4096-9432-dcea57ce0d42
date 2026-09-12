#!/usr/bin/env python3
"""开发服务器：python3 run_server.py [port]"""

import sys
from wsgiref.simple_server import make_server

from prepreg_release import make_app

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    app = make_app("prepreg.db")
    print(f"预浸料铺层放行 API  listening on http://0.0.0.0:{port}")
    make_server("0.0.0.0", port, app).serve_forever()
