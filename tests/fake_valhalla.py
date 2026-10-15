"""Minimalny atrapa serwera Valhalla (POST /route) do testów offline.

Odcinki to linie proste podzielone na 20 punktów; długość = haversine * 1.25.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from ltl.geo import haversine_km
from ltl.routing import encode_polyline

REQUESTS: list = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        REQUESTS.append(body)
        locs = body["locations"]
        legs, cur = [], [locs[0]]
        for p in locs[1:]:
            cur.append(p)
            if p.get("type", "break") == "break":
                pts = []
                for a, b in zip(cur, cur[1:]):
                    for k in range(20):
                        t = k / 20
                        pts.append((a["lat"] + (b["lat"] - a["lat"]) * t, a["lon"] + (b["lon"] - a["lon"]) * t))
                pts.append((cur[-1]["lat"], cur[-1]["lon"]))
                km = sum(float(haversine_km(*pts[i], *pts[i + 1])) for i in range(len(pts) - 1)) * 1.25
                legs.append({"shape": encode_polyline(pts, 6), "summary": {"length": km, "time": km / 70 * 3600}})
                cur = [p]
        data = json.dumps({"trip": {"legs": legs}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start(port=0):
    srv = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    srv, url = start(8765)
    print(url, flush=True)
    threading.Event().wait()
