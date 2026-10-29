"""Atrapa serwera Valhalla (POST /route) do testów offline.

* trasa = linia prosta podzielona na punkty co ok. 2 km (jak gęsta linia prawdziwego silnika), długość = haversine * 1.25,
* limit 1500 km na zapytanie (błąd 154 jak na publicznym serwerze), 400/442 dla identycznych punktów,
* przeprawa między Francją a Wielką Brytanią: odcinek morski Calais-Dover z manewrami promu (typ 28 / 29).
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ltl.geo import haversine_km
from ltl.routing import encode_polyline

REQUESTS: list = []
CALAIS, DOVER = (50.967, 1.853), (51.127, 1.333)
DELAY = float(os.environ.get("FAKE_DELAY", "0"))


def _is_gb(p) -> bool:
    return p["lon"] <= 1.75 and p["lat"] >= 50.5


def _dense(a, b, step_km=2.0):
    km = float(haversine_km(a[0], a[1], b[0], b[1]))
    n = max(2, int(km / step_km))
    return [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n + 1)]


def _length(pts):
    return sum(float(haversine_km(*pts[i], *pts[i + 1])) for i in range(len(pts) - 1)) * 1.25


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        REQUESTS.append(body)
        if DELAY:
            time.sleep(DELAY)
        locs = body["locations"]
        total_m = sum(float(haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])) * 1250
                      for a, b in zip(locs, locs[1:]))
        if total_m > 1_500_000:
            return self._reply(400, {"error_code": 154, "error": "Path distance exceeds the max distance limit: "
                                     "1500000 meters", "status_code": 400, "status": "Bad Request"})
        if total_m < 10:
            return self._reply(400, {"error_code": 442, "error": "No path could be found for input",
                                     "status_code": 400, "status": "Bad Request"})
        legs, cur = [], [locs[0]]
        for p in locs[1:]:
            cur.append(p)
            if p.get("type", "break") == "break":
                pts, maneuvers, ferry_km = [], [], 0.0
                for a, b in zip(cur, cur[1:]):
                    if _is_gb(a) != _is_gb(b):                       # przeprawa promowa
                        src, dst = (CALAIS, DOVER) if not _is_gb(a) else (DOVER, CALAIS)
                        first = _dense((a["lat"], a["lon"]), src)
                        last = _dense(dst, (b["lat"], b["lon"]))
                        pts += first
                        maneuvers.append({"type": 28, "begin_shape_index": len(pts) - 1, "end_shape_index": len(pts) - 1,
                                          "instruction": "Take the ferry.", "street_names": []})
                        pts += last
                        maneuvers.append({"type": 29, "begin_shape_index": len(first), "end_shape_index": len(first),
                                          "instruction": "Disembark.", "street_names": []})
                        ferry_km += 45
                    else:
                        pts += _dense((a["lat"], a["lon"]), (b["lat"], b["lon"]))
                km = _length(pts) + ferry_km
                legs.append({"shape": encode_polyline(pts, 6), "maneuvers": maneuvers,
                             "summary": {"length": km, "time": km / 70 * 3600}})
                cur = [p]
        self._reply(200, {"trip": {"legs": legs}})


def start(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    srv, url = start(8765)
    print(url, flush=True)
    threading.Event().wait()
