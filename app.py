import gzip
import json
import logging
import os
import time
import threading
import requests
from flask import Flask, Response, send_from_directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")

SOURCE_URL  = "https://regieessencequebec.ca/stations.geojson.gz"
CACHE_TTL   = int(os.environ.get("CACHE_TTL", 300))   # secondes (défaut 5 min)
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "*")

# ── Cache en mémoire ──────────────────────────────────────────────────────────
_cache_lock      = threading.Lock()
_cache_body      = None   # bytes JSON sérialisé
_cache_timestamp = 0.0    # epoch de la dernière mise à jour
_cache_error     = None   # dernier message d'erreur si fetch échoué


def _fetch_from_source():
    """Télécharge et décompresse le GeoJSON de la Régie. Retourne bytes JSON."""
    r = requests.get(SOURCE_URL, timeout=30, headers={
        "User-Agent": "Mozilla/5.0 (compatible; RegieEssenceProxy/1.0)",
        "Accept-Encoding": "gzip, deflate",
        "Accept": "*/*",
    })
    r.raise_for_status()
    content = r.content
    try:
        content = gzip.decompress(content)
    except Exception:
        pass                            # déjà décompressé par requests
    parsed = json.loads(content)
    return json.dumps(parsed, ensure_ascii=False).encode("utf-8")


def _refresh_cache():
    """Rafraîchit le cache si le TTL est dépassé. Thread-safe."""
    global _cache_body, _cache_timestamp, _cache_error

    now = time.time()
    age = now - _cache_timestamp

    if _cache_body is not None and age < CACHE_TTL:
        return  # cache encore valide

    with _cache_lock:
        # Double-check après acquisition du verrou
        age = time.time() - _cache_timestamp
        if _cache_body is not None and age < CACHE_TTL:
            return

        log.info("Cache expiré (âge=%.0fs), récupération depuis la Régie…", age)
        try:
            body = _fetch_from_source()
            _cache_body      = body
            _cache_timestamp = time.time()
            _cache_error     = None
            log.info("Cache mis à jour — %d octets, %d stations",
                     len(body),
                     body.count(b'"type":"Feature"'))
        except Exception as exc:
            _cache_error = str(exc)
            log.error("Échec de la récupération : %s", exc)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/stations")
def stations():
    _refresh_cache()

    if _cache_body is None:
        body = json.dumps({"error": _cache_error or "Données indisponibles"}).encode()
        return Response(body, status=502, content_type="application/json",
                        headers={"Access-Control-Allow-Origin": CORS_ORIGIN})

    age   = int(time.time() - _cache_timestamp)
    remaining = max(0, CACHE_TTL - age)

    return Response(
        _cache_body,
        content_type="application/json; charset=utf-8",
        headers={
            "Access-Control-Allow-Origin": CORS_ORIGIN,
            "X-Cache-Age":     str(age),
            "X-Cache-TTL":     str(CACHE_TTL),
            "Cache-Control":   f"public, max-age={remaining}",
        }
    )


@app.route("/health")
def health():
    age = int(time.time() - _cache_timestamp)
    return {
        "status":        "ok",
        "cache_age_sec": age,
        "cache_ttl_sec": CACHE_TTL,
        "cache_fresh":   _cache_body is not None and age < CACHE_TTL,
        "last_error":    _cache_error,
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8765, debug=False)
