"""Network side of the real-city-data pipeline: place search (Nominatim),
OpenStreetMap features (Overpass, several public mirrors with fallback,
backoff, per-chunk disk cache), and terrain (AWS Terrain Tiles, Terrarium
encoding). Pure processing of what comes back lives in osm_city.py.

Policies honoured on purpose:
- Nominatim usage policy: at most 1 request/second (RateLimiter), a real
  identifying User-Agent, no bulk geocoding, results cached on disk.
- Overpass: public mirrors are shared infrastructure that is often at
  capacity (502/504/429) or timing out. Every chunk is tried on each mirror
  in turn, with exponential backoff between rounds, honouring Retry-After,
  and a successful raw response is written to the cache so the same area is
  never asked for twice (and works offline afterwards).
- Terrain tiles are fetched once per tile and cached the same way.

Everything is blocking and meant to run on a worker thread (city_data.py
runs it off the Core/UI paths and reports `progress(phase, done, total)`).
"""

import base64
import gzip
import hashlib
import json
import math
import os
import struct
import threading
import time
import zlib
from typing import Callable, Optional

import citydata_http as http

# Ordered by observed reliability; the first healthy mirror wins, the rest
# are fallbacks. (overpass-api.de is the reference instance but queues
# heavily; the others are community mirrors.)
OVERPASS_ENDPOINTS = [
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
TERRARIUM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"

OVERPASS_ROUNDS = 4  # passes over the whole mirror list
OVERPASS_BACKOFF_S = 3.0
OVERPASS_TIMEOUT_S = 120.0

Progress = Callable[..., None]  # (phase, done, total[, message]); may raise Cancelled to abort


class Cancelled(Exception):
    """Raised (by a progress callback) to abort a download/extraction the
    user cancelled. Network calls already in flight finish first (bounded
    by their timeouts); the next progress tick unwinds."""


def _noop_progress(_phase: str, _done: int, _total: int, _message: str = "") -> None:
    pass


# --- cache dir --------------------------------------------------------------

def cache_root() -> str:
    """Where extracted areas live. SOTRICE_CITYDATA_DIR overrides (tests,
    isolated runs); otherwise a per-user folder, NOT the install dir (which
    may be read-only and is replaced on every update)."""
    override = os.environ.get("SOTRICE_CITYDATA_DIR")
    if override:
        root = override
    else:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        root = os.path.join(base, "Sotrice", "citydata")
    os.makedirs(root, exist_ok=True)
    return root


def _atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def read_json_gz(path: str):
    with gzip.open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def write_json_gz(path: str, obj) -> None:
    _atomic_write(path, gzip.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), 6))


# --- Nominatim ----------------------------------------------------------------

_nominatim_limiter = http.RateLimiter(1.1)  # policy: max 1 req/s


def geocode(query: str, limit: int = 5) -> list[dict]:
    """Place search. Returns [{name, lat, lon, bbox:{south,west,north,east},
    class, type, osm_type, osm_id}] best-first. Cached on disk per query
    (a repeated search never hits Nominatim again)."""
    query = query.strip()
    if not query:
        return []
    key = hashlib.sha1(f"{query.lower()}|{limit}".encode("utf-8")).hexdigest()[:16]
    path = os.path.join(cache_root(), "geocode", f"{key}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    _nominatim_limiter.wait()
    raw = http.request(
        NOMINATIM_URL + "?" + "&".join(f"{k}={_q(v)}" for k, v in {"q": query, "format": "jsonv2", "limit": limit, "addressdetails": 0}.items()),
        timeout=30,
    )
    out = []
    for item in json.loads(raw.decode("utf-8")):
        bb = item.get("boundingbox") or []
        out.append({
            "name": item.get("display_name") or item.get("name") or query,
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
            "bbox": {"south": float(bb[0]), "north": float(bb[1]), "west": float(bb[2]), "east": float(bb[3])} if len(bb) == 4 else None,
            "class": item.get("category") or item.get("class"),
            "type": item.get("type"),
            "osm_type": item.get("osm_type"),
            "osm_id": item.get("osm_id"),
        })
    _atomic_write(path, json.dumps(out).encode("utf-8"))
    return out


def _q(value) -> str:
    import urllib.parse

    return urllib.parse.quote(str(value), safe="")


# --- Overpass -------------------------------------------------------------------

def feature_query(bbox: dict, timeout_s: int = 180) -> str:
    """The one query that pulls everything the city needs for `bbox`
    (south, west, north, east in degrees). Ways come back with full geometry
    (`out geom`); route relations come back as member lists only (`out
    body`) because their member geometry can span a whole country."""
    b = f"{bbox['south']:.6f},{bbox['west']:.6f},{bbox['north']:.6f},{bbox['east']:.6f}"
    return f"""[out:json][timeout:{timeout_s}][maxsize:1073741824];
(
  way["highway"]({b});
  way["railway"]({b});
  way["building"]({b});
  way["building:part"]({b});
  way["landuse"]({b});
  way["natural"]({b});
  way["leisure"]({b});
  way["waterway"]({b});
  way["amenity"="parking"]({b});
  way["place"="square"]({b});
  way["public_transport"="platform"]({b});
  way["man_made"~"^(pier|bridge)$"]({b});
  rel["building"]({b});
  rel["type"="multipolygon"]["landuse"]({b});
  rel["type"="multipolygon"]["natural"]({b});
  rel["type"="multipolygon"]["leisure"]({b});
  rel["type"="multipolygon"]["water"]({b});
  rel["type"="multipolygon"]["place"="square"]({b});
  node["railway"~"^(station|halt|tram_stop|subway_entrance|stop)$"]({b});
  node["public_transport"~"^(stop_position|platform|station)$"]({b});
  node["highway"~"^(bus_stop|crossing|traffic_signals)$"]({b});
  node["amenity"]({b});
  node["shop"]({b});
  node["office"]({b});
  node["tourism"]({b});
  node["leisure"]({b});
  node["natural"="tree"]({b});
);
out geom;
rel["route"~"^(subway|tram|light_rail|train|monorail)$"]({b});
out body;"""


def overpass(query: str, progress: Progress = _noop_progress, label: str = "") -> dict:
    """Runs one Overpass query against the mirror list with retry/backoff.
    Progress messages say what is happening ("servers busy, retrying") so the
    UI never looks frozen. Raises RuntimeError with a plain-language message
    if every mirror failed every round."""
    last_error = "no attempt made"
    for round_index in range(OVERPASS_ROUNDS):
        for endpoint in OVERPASS_ENDPOINTS:
            host = endpoint.split("/")[2]
            progress("download", round_index, OVERPASS_ROUNDS,
                     f"Asking {host}" + (f" (part {label})" if label else "") + (f" - retry {round_index}" if round_index else ""))
            try:
                raw = http.request(endpoint, data={"data": query}, timeout=OVERPASS_TIMEOUT_S)
                doc = json.loads(raw.decode("utf-8"))
            except http.HttpError as exc:
                last_error = f"{host}: {exc}"
                if exc.retry_after:
                    time.sleep(min(exc.retry_after, 30))
                continue
            except ValueError as exc:  # HTML error page with a 200
                last_error = f"{host}: not JSON ({exc})"
                continue
            remark = str(doc.get("remark") or "")
            if "runtime error" in remark or "Timeout" in remark or "out of memory" in remark.lower():
                last_error = f"{host}: {remark[:160]}"
                continue
            return doc
        wait = OVERPASS_BACKOFF_S * (2 ** round_index)
        progress("download", round_index + 1, OVERPASS_ROUNDS, f"All map servers are busy - waiting {wait:.0f}s before retrying ({last_error[:70]})")
        time.sleep(wait)
    raise RuntimeError(
        "All the public OpenStreetMap servers are busy or unreachable right now "
        f"(last error: {last_error[:80]}). Try again in a few minutes - areas you already downloaded still open from the cache."
    )


def split_bbox(bbox: dict, max_side_m: float = 1500.0) -> list[dict]:
    """Tiles a big area into chunks of at most `max_side_m` per side, so a
    district-sized area becomes several modest queries (each cached and
    retried on its own) instead of one query the mirrors would refuse."""
    lat_mid = (bbox["south"] + bbox["north"]) / 2
    h_m = (bbox["north"] - bbox["south"]) * 111195.0
    w_m = (bbox["east"] - bbox["west"]) * 111195.0 * math.cos(math.radians(lat_mid))
    ny = max(1, math.ceil(h_m / max_side_m))
    nx = max(1, math.ceil(w_m / max_side_m))
    chunks = []
    dlat = (bbox["north"] - bbox["south"]) / ny
    dlon = (bbox["east"] - bbox["west"]) / nx
    # a small overlap so a feature straddling a seam is certainly caught
    # by at least one chunk; merging dedups by OSM id.
    pad_lat, pad_lon = 0.0001, 0.0001
    for j in range(ny):
        for i in range(nx):
            chunks.append({
                "south": bbox["south"] + j * dlat - (pad_lat if j else 0),
                "north": bbox["south"] + (j + 1) * dlat + (pad_lat if j < ny - 1 else 0),
                "west": bbox["west"] + i * dlon - (pad_lon if i else 0),
                "east": bbox["west"] + (i + 1) * dlon + (pad_lon if i < nx - 1 else 0),
            })
    return chunks


def fetch_features(area_dir: str, bbox: dict, progress: Progress = _noop_progress, force: bool = False) -> tuple[list[dict], dict]:
    """All OSM elements for `bbox`, from the per-area raw cache when
    present, else from Overpass (chunked). Returns (elements deduped by
    (type, id), info{chunks, cached_chunks, osm_timestamp, seconds})."""
    started = time.time()
    chunks = split_bbox(bbox)
    elements: dict[tuple[str, int], dict] = {}
    cached = 0
    osm_timestamp = None
    for index, chunk in enumerate(chunks):
        progress("download", index, len(chunks))
        path = os.path.join(area_dir, "raw", f"chunk_{index}.json.gz")
        if os.path.exists(path) and not force:
            doc = read_json_gz(path)
            cached += 1
        else:
            doc = overpass(feature_query(chunk), progress, label=f"{index + 1}/{len(chunks)}")
            write_json_gz(path, doc)
        osm_timestamp = osm_timestamp or (doc.get("osm3s") or {}).get("timestamp_osm_base")
        for element in doc.get("elements", []):
            key = (element["type"], element["id"])
            # `out body` relation stubs and `out geom` ones can both appear; keep the richer one
            if key not in elements or len(json.dumps(element)) > len(json.dumps(elements[key])):
                elements[key] = element
    progress("download", len(chunks), len(chunks))
    info = {"chunks": len(chunks), "cached_chunks": cached, "osm_timestamp": osm_timestamp, "seconds": round(time.time() - started, 2)}
    return list(elements.values()), info


# --- Terrain (Terrarium PNG tiles) ------------------------------------------------

def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png_rgb(data: bytes) -> tuple[int, int, list[bytearray], int]:
    """Minimal PNG decoder: 8-bit, non-interlaced, colour type 2 (RGB) or 6
    (RGBA) — what Terrarium tiles are. Returns (width, height, rows, bpp)
    with each row a bytearray of width*bpp samples. stdlib only."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos = 8
    width = height = 0
    color_type = bit_depth = interlace = None
    idat = bytearray()
    while pos < len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"IDAT":
            idat.extend(chunk)
        elif ctype == b"IEND":
            break
    if bit_depth != 8 or color_type not in (2, 6) or interlace != 0:
        raise ValueError(f"unsupported PNG (depth {bit_depth}, colour {color_type}, interlace {interlace})")
    bpp = 3 if color_type == 2 else 4
    raw = zlib.decompress(bytes(idat))
    stride = width * bpp
    rows: list[bytearray] = []
    prev = bytearray(stride)
    offset = 0
    for _ in range(height):
        ftype = raw[offset]
        line = bytearray(raw[offset + 1:offset + 1 + stride])
        offset += 1 + stride
        if ftype == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ftype == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ftype == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                upleft = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 255
        rows.append(line)
        prev = line
    return width, height, rows, bpp


def _tile_xy(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def terrain_zoom_for(bbox: dict) -> int:
    """Zoom so a tile pixel is roughly <= 10 m on the ground (Terrarium goes to 15)."""
    lat = (bbox["south"] + bbox["north"]) / 2
    span_m = max((bbox["north"] - bbox["south"]) * 111195.0, (bbox["east"] - bbox["west"]) * 111195.0 * math.cos(math.radians(lat)))
    return 15 if span_m < 1600 else 14 if span_m < 6000 else 13


def fetch_terrain(area_dir: str, bbox: dict, step_m: float, proj_fwd, proj_inv, progress: Progress = _noop_progress) -> Optional[dict]:
    """Samples a regular height grid over `bbox` from AWS Terrarium tiles.
    `proj_fwd(lat, lon) -> (x, z)` and `proj_inv(x, z) -> (lat, lon)` are the
    area's local projection. Returns the schema's `terrain` object
    ({x0, z0, dx, nx, nz, base_elev_m, min_m, max_m, heights_dm[...]}), heights in
    decimetres relative to `base_elev_m` (the elevation at the area origin).
    None if the tiles are unreachable (the city still works, just flat)."""
    zoom = terrain_zoom_for(bbox)
    corners = [_tile_xy(bbox["north"], bbox["west"], zoom), _tile_xy(bbox["south"], bbox["east"], zoom)]
    tx0, ty0 = int(math.floor(corners[0][0])), int(math.floor(corners[0][1]))
    tx1, ty1 = int(math.floor(corners[1][0])), int(math.floor(corners[1][1]))
    needed = [(x, y) for y in range(ty0, ty1 + 1) for x in range(tx0, tx1 + 1)]
    tiles: dict[tuple[int, int], tuple[int, list[bytearray], int]] = {}
    for index, (x, y) in enumerate(needed):
        progress("terrain", index, len(needed))
        path = os.path.join(area_dir, "terrain", f"{zoom}_{x}_{y}.png")
        if os.path.exists(path):
            with open(path, "rb") as f:
                blob = f.read()
        else:
            try:
                blob = http.request(TERRARIUM_URL.format(z=zoom, x=x, y=y), timeout=60)
            except http.HttpError as exc:
                print(f"osm_fetch: terrain tile {zoom}/{x}/{y} unavailable ({exc}); the city will be flat there")
                return None
            _atomic_write(path, blob)
        w, h, rows, bpp = decode_png_rgb(blob)
        tiles[(x, y)] = (w, rows, bpp)
    progress("terrain", len(needed), len(needed))

    def elevation(lat: float, lon: float) -> float:
        fx, fy = _tile_xy(lat, lon, zoom)
        tx, ty = int(math.floor(fx)), int(math.floor(fy))
        tile = tiles.get((tx, ty))
        if tile is None:
            return 0.0
        size, rows, bpp = tile
        px, py = (fx - tx) * size - 0.5, (fy - ty) * size - 0.5
        x0, y0 = max(0, min(size - 1, int(math.floor(px)))), max(0, min(size - 1, int(math.floor(py))))
        x1, y1 = min(size - 1, x0 + 1), min(size - 1, y0 + 1)
        ax, ay = max(0.0, min(1.0, px - x0)), max(0.0, min(1.0, py - y0))

        def sample(ix: int, iy: int) -> float:
            row = rows[iy]
            o = ix * bpp
            return row[o] * 256.0 + row[o + 1] + row[o + 2] / 256.0 - 32768.0

        top = sample(x0, y0) * (1 - ax) + sample(x1, y0) * ax
        bottom = sample(x0, y1) * (1 - ax) + sample(x1, y1) * ax
        return top * (1 - ay) + bottom * ay

    x_min, z_min = proj_fwd(bbox["south"], bbox["west"])
    x_max, z_max = proj_fwd(bbox["north"], bbox["east"])
    nx = max(2, int(math.ceil((x_max - x_min) / step_m)) + 1)
    nz = max(2, int(math.ceil((z_max - z_min) / step_m)) + 1)
    base = elevation(*proj_inv(0.0, 0.0))
    heights = []
    lo = hi = 0
    for j in range(nz):
        for i in range(nx):
            lat, lon = proj_inv(x_min + i * step_m, z_min + j * step_m)
            dm = int(round((elevation(lat, lon) - base) * 10))
            heights.append(dm)
            lo, hi = min(lo, dm), max(hi, dm)
    return {
        "x0": round(x_min, 2), "z0": round(z_min, 2), "dx": step_m, "nx": nx, "nz": nz,
        "base_elev_m": round(base, 2), "min_m": lo / 10.0, "max_m": hi / 10.0,
        "heights_dm": heights,
        "source": "Terrain Tiles on AWS (Terrarium), zoom %d" % zoom,
    }


def encode_heights_b64(heights_dm: list[int]) -> str:
    """int16 little-endian, base64 — the compact wire form of a terrain grid."""
    return base64.b64encode(struct.pack(f"<{len(heights_dm)}h", *heights_dm)).decode("ascii")
