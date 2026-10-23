"""city_appearance - the "looks" layer of the city: OUR appearance data for every building, kept next to the city
data (contract: docs/city-schema/looks.md, schema "city-looks/1"). Stdlib only.

What a building looks like is resolved per LEAF field (era, wall.material, wall.colour, floors, ...) from four layers,
lowest priority first:

    rule  deterministic and free: derived from the building's own signals (type, size, height, levels, OSM tags) and
          its neighbourhood (touching neighbours, building density, distance to the area centre, linked POIs). The
          per-building variation comes from a hash of the building id, never from random numbers, so every run, every
          machine and every process produces the same city. A rule fills ONLY what no higher layer knows and it is
          conditioned on what they do know (a baroque building the AI or the user says is brick gets a brick colour,
          not a stucco one), so the result never contradicts itself.
    ai    plausible values from the budget model (city_appearance_ai.py), stored per root building in looks.json and
          cached by content hash in looks_cache.json: the same signature is never asked twice.
    osm   real facts: building:colour / roof:colour / materials / roof:shape / building:levels / start_date +
          building:architecture (era) / POIs of the shop|food|service|lodging kinds (ground floor = shop).
    user  Laszlo's edits (looks_overrides.json). Never touched by regeneration, always wins.

Invariants (a bad input must never break a tile):
- every record that leaves this module is complete and vocabulary-valid: invalid values are dropped at the door
  (`validate_record`) and the field falls back one layer down;
- `src.<group>` says where each group came from; the geometry-changing groups (`floors`, `roof` = roof.shape) report the
  provenance of the geometry leaf itself, so a rule value can never be mistaken for a trusted fact (rules never change
  the shape of the city, the renderer ignores `rule` for geometry);
- parts of a split building inherit the appearance of their outline; only floors/floor_h/roof.shape come from the part
  itself (its own tags, its own height);
- files are written atomically, a corrupt file is an empty layer plus a warning (and a backup before it is overwritten),
  never a crash.
"""

import colorsys
import copy
import gzip
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger("city_appearance")

SCHEMA = "city-looks/1"
VOCAB_VERSION = 1  # bump when the closed vocabulary changes (part of every content hash)
RULES_VERSION = 1  # informational: rules are recomputed on every load, never cached
TILE_M = 500.0

LOOKS_FILE = "looks.json"
OVERRIDES_FILE = "looks_overrides.json"
CACHE_FILE = "looks_cache.json"

# ------------------------------------------------------------------------------------------------- vocabulary

ERAS = ("med", "bar", "kla", "his", "int", "pwr", "lmd", "con", "ver")
WALL_MATERIALS = ("stucco", "brick", "stone", "concrete", "wood", "metal", "glass")
WINDOWS = ("tall", "sq", "wide", "strip", "glass", "small", "none")
GROUNDS = ("res", "shop", "arcade", "garage", "portal", "none")
BALCONIES = ("none", "juliet", "row", "loggia")
CORNICES = (0, 1, 2)
ROOF_SHAPES = ("flat", "gabled", "hipped", "half-hipped", "mansard", "pyramidal", "skillion", "dome", "gambrel")
ROOF_MATERIALS = ("tiles", "slate", "tar", "metal", "concrete", "glass", "grass", "stone", "thatch")
FLOORS_RANGE = (1, 40)
FLOOR_H_RANGE = (2.4, 5.0)

SOURCES = ("rule", "ai", "osm", "user")
SRC_RANK = {"rule": 0, "ai": 1, "osm": 2, "user": 3}
SRC_GROUPS = ("era", "wall", "floors", "win", "ground", "balcony", "cornice", "roof")

# every settable leaf, in the order records are written
LEAF_PATHS = (
    "era", "wall.material", "wall.colour", "floors", "floor_h", "win", "ground", "balcony", "cornice",
    "roof.shape", "roof.material", "roof.colour", "weather",
)
NESTED_GROUPS = ("wall", "roof")
# leaves that describe the SHAPE of one part (its own height / roof), as opposed to the appearance of the building
GEOMETRY_LEAVES = frozenset({"floors", "floor_h", "roof.shape"})
# which `src` group a leaf belongs to (weather belongs to none)
GROUP_OF_LEAF = {"era": "era", "wall.material": "wall", "wall.colour": "wall", "floors": "floors", "floor_h": "floors", "win": "win", "ground": "ground",
                 "balcony": "balcony", "cornice": "cornice", "roof.shape": "roof", "roof.material": "roof", "roof.colour": "roof"}

_HEX6 = re.compile(r"^#?([0-9a-fA-F]{6})$")


def norm_colour(value: Any) -> Optional[str]:
    """'#RRGGBB' / 'rrggbb' -> '#rrggbb', anything else None."""
    if isinstance(value, str):
        m = _HEX6.match(value.strip())
        if m:
            return "#" + m.group(1).lower()
    return None


def _v_enum(values: Iterable[str]) -> Callable[[Any], Optional[str]]:
    allowed = frozenset(values)
    return lambda v: v if isinstance(v, str) and v in allowed else None


def _v_int(lo: int, hi: int) -> Callable[[Any], Optional[int]]:
    def check(v: Any) -> Optional[int]:
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            n = v
        elif isinstance(v, float) and math.isfinite(v) and v.is_integer():
            n = int(v)
        else:
            return None
        return n if lo <= n <= hi else None
    return check


def _v_float(lo: float, hi: float, digits: int = 3) -> Callable[[Any], Optional[float]]:
    def check(v: Any) -> Optional[float]:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return None
        return round(float(v), digits) if lo <= v <= hi else None
    return check


LEAF_VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "era": _v_enum(ERAS),
    "wall.material": _v_enum(WALL_MATERIALS),
    "wall.colour": norm_colour,
    "floors": _v_int(*FLOORS_RANGE),
    "floor_h": _v_float(*FLOOR_H_RANGE),
    "win": _v_enum(WINDOWS),
    "ground": _v_enum(GROUNDS),
    "balcony": _v_enum(BALCONIES),
    "cornice": _v_int(0, 2),
    "roof.shape": _v_enum(ROOF_SHAPES),
    "roof.material": _v_enum(ROOF_MATERIALS),
    "roof.colour": norm_colour,
    "weather": _v_float(0.0, 1.0, 2),
}


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 28 else text[:25] + "..."


def validate_record(rec: Any) -> tuple[dict, list[str]]:
    """A (partial) record -> (clean nested partial record, problems). Only vocabulary-valid leaves survive; an unknown
    key, a wrong type or an out-of-vocabulary value is dropped and reported, never stored. `src` is not accepted (it is
    computed, never stored)."""
    if not isinstance(rec, dict):
        return {}, ["record must be an object"]
    clean: dict = {}
    problems: list[str] = []
    for key, value in rec.items():
        if key in NESTED_GROUPS:
            if not isinstance(value, dict):
                problems.append(f"{key}: must be an object")
                continue
            sub: dict = {}
            for key2, value2 in value.items():
                path = f"{key}.{key2}"
                check = LEAF_VALIDATORS.get(path)
                if check is None:
                    problems.append(f"{path}: unknown field")
                    continue
                norm = check(value2)
                if norm is None:
                    problems.append(f"{path}: invalid value {_short(value2)}")
                    continue
                sub[key2] = norm
            if sub:
                clean[key] = sub
            continue
        check = LEAF_VALIDATORS.get(key) if isinstance(key, str) else None
        if check is None:
            problems.append(f"{key}: unknown field" if key != "src" else "src: computed, cannot be set")
            continue
        norm = check(value)
        if norm is None:
            problems.append(f"{key}: invalid value {_short(value)}")
            continue
        clean[key] = norm
    return clean, problems


def validate_patch(patch: Any) -> tuple[dict, list[str]]:
    """The `set` part of a looks patch: same rules as `validate_record` (partial, nested partials allowed); an empty
    result is reported as a problem so a patch can never be a silent no-op."""
    clean, problems = validate_record(patch)
    if not clean and not problems:
        problems.append("nothing to set")
    return clean, problems


def validate_clear(paths: Any) -> tuple[list[str], list[str]]:
    """The `clear` list of a patch: leaf paths ('wall.colour'), group names ('wall', 'roof') or '*' (the whole override
    of that building) -> (clean paths, problems)."""
    if paths is None:
        return [], []
    if not isinstance(paths, list):
        return [], ["clear must be a list of paths"]
    ok, problems = [], []
    for p in paths:
        if isinstance(p, str) and (p in LEAF_VALIDATORS or p in NESTED_GROUPS or p == "*"):
            if p not in ok:
                ok.append(p)
        else:
            problems.append(f"clear: unknown path {_short(p)}")
    return ok, problems


def flatten(partial: dict) -> dict[str, Any]:
    """{'wall': {'colour': '#aa..'}, 'floors': 5} -> {'wall.colour': '#aa..', 'floors': 5}."""
    out: dict[str, Any] = {}
    for key, value in partial.items():
        if isinstance(value, dict):
            for key2, value2 in value.items():
                out[f"{key}.{key2}"] = value2
        else:
            out[key] = value
    return out


def unflatten(flat: dict[str, Any]) -> dict:
    out: dict = {}
    for path in LEAF_PATHS:
        if path in flat:
            if "." in path:
                group, leaf = path.split(".", 1)
                out.setdefault(group, {})[leaf] = flat[path]
            else:
                out[path] = flat[path]
    return out


# --------------------------------------------------------------------------------------- small deterministic tools

def u01(key: str, salt: str) -> float:
    """Deterministic uniform number in [0, 1) from (key, salt): the ONLY source of per-building variation. blake2b, not
    hash(): Python's hash() is randomised per process and would change the city on every start."""
    digest = hashlib.blake2b(f"{salt}\x1f{key}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 18446744073709551616.0


def pick(items: Iterable[tuple[Any, float]], u: float) -> Any:
    """Weighted choice: items [(value, weight)], u in [0,1). Zero/negative weights never win."""
    live = [(v, w) for v, w in items if w > 0]
    if not live:
        raise ValueError("pick: no positive weight")
    total = sum(w for _, w in live)
    t = u * total
    acc = 0.0
    for value, weight in live:
        acc += weight
        if t < acc:
            return value
    return live[-1][0]


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _num(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return float(value)


# --------------------------------------------------------------------------------------------------- colour tools

def _rgb(hex_: str) -> tuple[float, float, float]:
    return int(hex_[1:3], 16) / 255.0, int(hex_[3:5], 16) / 255.0, int(hex_[5:7], 16) / 255.0


def _hex(r: float, g: float, b: float) -> str:
    return "#%02x%02x%02x" % (round(_clamp(r, 0, 1) * 255), round(_clamp(g, 0, 1) * 255), round(_clamp(b, 0, 1) * 255))


def saturation(hex_: str) -> float:
    """HSV saturation 0..1 of '#rrggbb'."""
    return colorsys.rgb_to_hsv(*_rgb(hex_))[1]


def limit_saturation(hex_: str, max_s: float) -> str:
    """Same hue and value, saturation capped at max_s (natural, no neon)."""
    h, s, v = colorsys.rgb_to_hsv(*_rgb(hex_))
    if s <= max_s:
        return hex_
    return _hex(*colorsys.hsv_to_rgb(h, max_s, v))


def jitter_colour(hex_: str, key: str, salt: str, *, dv: float = 0.06, dh_deg: float = 3.0, ds: float = 0.10, max_s: float = 0.445) -> str:
    """Deterministic small variation of a palette colour (value +-dv, hue +-dh_deg, saturation +-ds, capped)."""
    h, s, v = colorsys.rgb_to_hsv(*_rgb(hex_))
    v = _clamp(v * (1.0 + (u01(key, salt + "v") - 0.5) * 2.0 * dv), 0.16, 0.97)
    h = (h + (u01(key, salt + "h") - 0.5) * 2.0 * dh_deg / 360.0) % 1.0
    s = _clamp(s * (1.0 + (u01(key, salt + "s") - 0.5) * 2.0 * ds), 0.0, max_s)
    return _hex(*colorsys.hsv_to_rgb(h, s, v))


# Natural, desaturated Central-European facade colours (HSV saturation <= 0.45, checked by the unit tests).
STUCCO_FAMILIES: dict[str, tuple[str, ...]] = {
    "cream": ("#ece4d0", "#e8dfc9", "#efe9d9", "#e3dac4"),
    "ochre": ("#e3d3ae", "#dccb9f", "#d9c690", "#e0cf9a"),
    "yellow": ("#e6d896", "#ddcf84", "#e9de9f"),
    "apricot": ("#e0c4a0", "#dcbf9c", "#e3caa8"),
    "salmon": ("#dfc0b4", "#d9b8a8", "#e2c8bc"),
    "pink": ("#e0c6c4", "#dcbdbb", "#d9a7a0"),
    "terracotta": ("#c9a58a", "#c4a083", "#cfae94"),
    "sage": ("#c8d0b4", "#bfc8aa", "#cfd6bd"),
    "blue_grey": ("#bfc8cc", "#c8d0d4", "#b8c2c8"),
    "grey": ("#cfcbc2", "#c3beb4", "#d6d2c8"),
    "white": ("#eeeae0", "#f0ece4", "#e9e6de"),
    "dark_grey": ("#9a968d", "#8f8b83", "#a39f96"),
}
STUCCO_BY_ERA: dict[str, tuple[tuple[str, float], ...]] = {
    "med": (("cream", .25), ("grey", .25), ("ochre", .2), ("terracotta", .1), ("white", .1), ("apricot", .1)),
    "bar": (("ochre", .22), ("yellow", .2), ("cream", .15), ("apricot", .12), ("salmon", .12), ("terracotta", .08), ("sage", .05), ("white", .03), ("grey", .03)),
    "kla": (("cream", .25), ("ochre", .2), ("yellow", .12), ("sage", .12), ("apricot", .1), ("grey", .08), ("salmon", .07), ("blue_grey", .06)),
    "his": (("ochre", .22), ("cream", .2), ("grey", .14), ("terracotta", .12), ("apricot", .1), ("salmon", .08), ("sage", .06), ("yellow", .05), ("blue_grey", .03)),
    "int": (("cream", .25), ("white", .15), ("grey", .15), ("ochre", .1), ("apricot", .1), ("salmon", .1), ("sage", .08), ("blue_grey", .07)),
    "pwr": (("white", .25), ("cream", .2), ("grey", .15), ("apricot", .1), ("yellow", .08), ("sage", .07), ("salmon", .06), ("blue_grey", .05), ("pink", .04)),
    "lmd": (("white", .22), ("cream", .2), ("grey", .14), ("apricot", .12), ("yellow", .1), ("terracotta", .08), ("sage", .06), ("salmon", .05), ("blue_grey", .03)),
    "con": (("white", .35), ("grey", .2), ("cream", .13), ("dark_grey", .12), ("blue_grey", .1), ("sage", .05), ("ochre", .05)),
    "ver": (("cream", .22), ("white", .18), ("yellow", .12), ("apricot", .12), ("ochre", .1), ("salmon", .08), ("sage", .08), ("grey", .06), ("blue_grey", .04)),
}
WALL_PALETTES: dict[str, tuple[str, ...]] = {
    "brick": ("#9a6a5a", "#a06f5f", "#8f6355", "#a47a68", "#8c5f50", "#b08a76", "#94705f"),
    "stone": ("#b8b2a4", "#a9a496", "#c4bfb0", "#9d978a", "#b5ae9c", "#8f8a7e", "#bdb7a8"),
    "concrete": ("#b9b8b4", "#a6a5a1", "#c8c7c2", "#9a9996", "#d0cfca"),
    "wood": ("#8c6f55", "#7d6450", "#9a7d62", "#7b6d5e", "#a08566"),
    "metal": ("#8d9399", "#7d8489", "#a7adb2", "#6f777d"),
    "glass": ("#7f9aa8", "#6f8793", "#8aa3ad", "#9fb4bd", "#5f7886"),
}
ROOF_PALETTES: dict[str, tuple[str, ...]] = {
    "tiles": ("#8f6150", "#946453", "#9b6a58", "#7f5648", "#8a5e4e", "#a0705c"),
    "slate": ("#5a6068", "#4d535b", "#666c74", "#585d63"),
    "tar": ("#55585c", "#4a4d51", "#6a6c6e", "#5f6165"),
    "metal": ("#8a9096", "#79807f", "#9ba1a5", "#6f8a84"),
    "concrete": ("#8f8f8b", "#9c9b97", "#a8a7a2"),
    "glass": ("#7f9aa8", "#8fa7b3"),
    "grass": ("#6f8a58", "#7a9462", "#66814f"),
    "stone": ("#8a857a", "#9a958a"),
    "thatch": ("#a8946a", "#9a8660", "#b39e72"),
}

# The AI names colours as CLOSED FAMILY WORDS, never hex digits (a live test showed hex answers collapse onto a handful of
# near-white values and copy the examples of the prompt): the family picks a palette, the shade inside it is a deterministic
# function of the signature, so the answer is natural, varied and cheap (one word instead of ~4 tokens of digits).
AI_WALL_FAMILIES = ("cream", "ochre", "yellow", "apricot", "salmon", "pink", "terracotta", "sage", "bluegrey", "grey", "white", "darkgrey", "natural")
AI_ROOF_FAMILIES = ("terracotta", "red", "brown", "grey", "anthracite", "black", "green", "beige", "natural")
ROOF_FAMILIES: dict[str, tuple[str, ...]] = {
    "terracotta": ("#8f6150", "#946453", "#9b6a58", "#a0705c"),
    "red": ("#8e5a52", "#94605a", "#855650", "#9a6358"),
    "brown": ("#7f6252", "#6f5a4e", "#8a6d5a", "#76604f"),
    "grey": ("#7c7f84", "#8a8d91", "#6f7377", "#9a9da1"),
    "anthracite": ("#4d535b", "#585d63", "#5a6068", "#464c53"),
    "black": ("#3f4144", "#4a4d51", "#36393c"),
    "green": ("#6f8a84", "#78948a", "#657f78"),  # weathered copper
    "beige": ("#b8a88a", "#a89a80", "#c0b394"),
}


WALL_FAMILY_ALIAS = {"beige": "cream", "offwhite": "white", "orange": "apricot", "peach": "apricot", "rose": "pink", "brown": "terracotta", "green": "sage",
                     "blue": "bluegrey", "gray": "grey", "lightgrey": "grey", "lightgray": "grey", "darkgray": "darkgrey", "gold": "yellow", "tan": "apricot"}
ROOF_FAMILY_ALIAS = {"orange": "terracotta", "rust": "red", "copper": "green", "slate": "anthracite", "silver": "grey", "gray": "grey", "darkgrey": "anthracite",
                     "darkgray": "anthracite", "dark": "anthracite", "tan": "beige", "tile": "terracotta", "tiles": "terracotta"}


def _family_word(word: str) -> str:
    return re.sub(r"[\s_\-]+", "", word.strip().lower())


def wall_colour_from_family(family: str, material: Optional[str], key: str) -> Optional[str]:
    """AI family word (+ the wall material it chose) -> '#rrggbb', deterministic per `key`. Rendered walls take the family's
    palette; every other material takes its own natural palette (the family is moot for brick, stone, glass...). None = not a
    family word."""
    fam = _family_word(family)
    fam = WALL_FAMILY_ALIAS.get(fam, fam)
    stucco = {_family_word(k): v for k, v in STUCCO_FAMILIES.items()}
    if material in WALL_PALETTES and material != "stucco":
        palette = WALL_PALETTES[material]
    elif fam in stucco:
        palette = stucco[fam]
    elif fam == "natural":
        palette = WALL_PALETTES.get(material or "", STUCCO_FAMILIES["cream"])
    else:
        return None
    base = palette[int(u01(key, "ai.wall.shade") * len(palette)) % len(palette)]
    return jitter_colour(base, key, "ai.wall", dv=0.04, dh_deg=2.0, ds=0.06)


def roof_colour_from_family(family: str, material: Optional[str], key: str) -> Optional[str]:
    fam = _family_word(family)
    fam = ROOF_FAMILY_ALIAS.get(fam, fam)
    if fam == "natural":
        palette = ROOF_PALETTES.get(material or "", ROOF_PALETTES["tiles"])
    elif fam in ROOF_FAMILIES:
        palette = ROOF_FAMILIES[fam]
    else:
        return None
    base = palette[int(u01(key, "ai.roof.shade") * len(palette)) % len(palette)]
    return jitter_colour(base, key, "ai.roof", dv=0.04, dh_deg=2.0, ds=0.06)


# ------------------------------------------------------------------------------------------------ OSM interpretation

_YEAR_RE = re.compile(r"(?<!\d)(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})(?!\d)")
_DECADE_RE = re.compile(r"(?<!\d)(1[0-9]{2}|20[0-9])0s")
_CENTURY_RE = re.compile(r"(?<![0-9a-z])c\s*(\d{1,2})(?!\d)", re.I)
ERA_YEARS = (("med", 1000, 1649), ("bar", 1650, 1779), ("kla", 1780, 1859), ("his", 1860, 1918), ("int", 1919, 1944), ("pwr", 1945, 1974), ("lmd", 1975, 1999), ("con", 2000, 2100))


def year_range(text: Any) -> Optional[tuple[int, int]]:
    """OSM start_date -> (earliest, latest) year: '1873', '1873-05-01', '~1850', 'c. 1850', '1850..1860', '1850s', 'C16'
    (century), 'before 1900', 'after 1950'; None when there is no usable year."""
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip().lower()
    m = _DECADE_RE.search(s)
    if m:
        y = int(m.group(1)) * 10
        return y, y + 9
    years = [int(y) for y in _YEAR_RE.findall(s)]
    if years:
        lo, hi = min(years), max(years)
        if s.startswith("before") or s.startswith("<"):
            return max(1000, lo - 80), lo - 1
        if s.startswith("after") or s.startswith(">"):
            return lo + 1, min(2100, lo + 60)
        return (lo, hi) if hi - lo <= 150 else (lo, lo)
    m = _CENTURY_RE.search(s)
    if m:
        c = int(m.group(1))
        if 5 <= c <= 21:
            return (c - 1) * 100, (c - 1) * 100 + 99
    return None


def era_for_year(year: int) -> str:
    for era, lo, hi in ERA_YEARS:
        if lo <= year <= hi:
            return era
    return "con" if year > 2000 else "med"


def era_from_range(lo: int, hi: int, key: str) -> str:
    """A single year maps straight to its era; a range ('C19', '1850s') picks among the eras it overlaps, weighted by
    overlap and deterministic per building (the tag says WHEN roughly, the pick inside that span is ours)."""
    if hi <= lo:
        return era_for_year(lo)
    weights = []
    for era, a, b in ERA_YEARS:
        overlap = min(hi, b) - max(lo, a) + 1
        if overlap > 0:
            weights.append((era, overlap))
    return pick(weights, u01(key, "era_range")) if weights else era_for_year(lo)


ARCH_ERA = {
    "romanesque": "med", "gothic": "med", "renaissance": "med", "mannerism": "med",
    "baroque": "bar", "barocco": "bar", "rococo": "bar", "baroque_revival": "his",
    "classicism": "kla", "neoclassicism": "kla", "neoclassical": "kla", "biedermeier": "kla", "empire": "kla", "classical": "kla", "palladian": "kla",
    "historicism": "his", "eclecticism": "his", "eclectic": "his", "art_nouveau": "his", "jugendstil": "his", "secession": "his", "gruenderzeit": "his",
    "art_deco": "int", "bauhaus": "int", "functionalism": "int", "expressionism": "int", "new_objectivity": "int",
    "modern": "pwr", "modernism": "pwr", "modernist": "pwr", "brutalism": "pwr", "brutalist": "pwr", "international_style": "pwr", "mid_century": "pwr",
    "post_modern": "lmd", "postmodern": "lmd", "postmodernism": "lmd",
    "contemporary": "con", "deconstructivism": "con", "high_tech": "con", "blobitecture": "con", "parametricism": "con",
}


def arch_era(value: Any) -> Optional[str]:
    """OSM building:architecture ('barocco;rococo', 'neo-renaissance', 'art_nouveau', ...) -> era or None."""
    if not isinstance(value, str):
        return None
    for token in re.split(r"[;,]", value.lower()):
        t = token.strip().replace(" ", "_").replace("-", "_")
        if t in ARCH_ERA:
            return ARCH_ERA[t]
        if t.startswith("neo"):
            return "his"
    return None


ROOF_ALIAS = {
    "gabled": "gabled", "gable": "gabled", "saltbox": "gabled", "double_saltbox": "gabled", "quadruple_saltbox": "gabled", "gabled_row": "gabled",
    "hipped": "hipped", "hip": "hipped", "hipped_and_gabled": "half-hipped", "half-hipped": "half-hipped", "half_hipped": "half-hipped",
    "side_hipped": "half-hipped", "jerkinhead": "half-hipped",
    "flat": "flat", "mansard": "mansard", "pyramidal": "pyramidal", "cone": "pyramidal",
    "skillion": "skillion", "mono_pitched": "skillion", "lean_to": "skillion", "pent": "skillion",
    "dome": "dome", "round": "dome", "onion": "dome", "gambrel": "gambrel",
}


def roof_shape_of(value: Any) -> Optional[str]:
    """OSM roof:shape (or an AI answer) -> the closed roof vocabulary, None when unknown ('many', 'complex', ...)."""
    if not isinstance(value, str):
        return None
    return ROOF_ALIAS.get(value.strip().lower().replace(" ", "_"))


WALL_ALIAS = {"plaster": "stucco", "render": "stucco", "rendered": "stucco", "bricks": "brick", "timber": "wood", "steel": "metal", "limestone": "stone", "sandstone": "stone"}


def wall_material_of(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    v = WALL_ALIAS.get(v, v)
    return v if v in WALL_MATERIALS else None


def roof_material_of(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    v = {"roof_tiles": "tiles", "tile": "tiles", "tar_paper": "tar", "copper": "metal", "zinc": "metal", "steel": "metal"}.get(v, v)
    return v if v in ROOF_MATERIALS else None


# raw OSM tags worth keeping next to the dataset (everything the dataset does not already carry as a field)
TAG_KEYS = ("start_date", "building:architecture", "heritage", "wikidata", "architect", "historic", "tourism", "amenity", "shop",
            "office", "religion", "castle_type", "man_made", "leisure")


def load_osm_tags(area_dir: str) -> dict[str, dict]:
    """The area's raw Overpass cache (raw/chunk_<n>.json.gz) -> {'w123' | 'r45': {whitelisted tags}} for every building and
    building:part. Join key = dataset id without the '.<n>' of a multipolygon outer ring. A missing or damaged chunk is
    skipped with a warning (the looks just have fewer OSM facts)."""
    out: dict[str, dict] = {}
    raw = os.path.join(area_dir, "raw")
    if not os.path.isdir(raw):
        return out
    for name in sorted(os.listdir(raw)):
        if not (name.startswith("chunk_") and name.endswith(".json.gz")):
            continue
        try:
            with gzip.open(os.path.join(raw, name), "rb") as f:
                data = json.loads(f.read().decode("utf-8"))
        except (OSError, ValueError, EOFError) as exc:
            log.warning("looks: raw chunk %s unreadable (%s) - its OSM facts are skipped", name, exc)
            continue
        for el in (data.get("elements") or ()) if isinstance(data, dict) else ():
            tags = el.get("tags")
            kind = el.get("type")
            if kind not in ("way", "relation") or not isinstance(tags, dict) or ("building" not in tags and "building:part" not in tags):
                continue
            slim = {k: str(tags[k])[:80] for k in TAG_KEYS if k in tags}
            if slim:
                out.setdefault(("w" if kind == "way" else "r") + str(el.get("id")), {}).update(slim)
    return out


# ------------------------------------------------------------------------------------------------ type classification

SACRAL_TYPES = frozenset({"church", "cathedral", "chapel", "monastery", "temple", "mosque", "synagogue", "shrine", "basilica", "convent", "religious", "kingdom_hall"})
# trivial structures: never worth an AI question (rules give them a plausible look for free)
AUX_TYPES = frozenset({
    "garage", "garages", "carport", "shed", "hut", "roof", "kiosk", "greenhouse", "service", "cabin", "container", "toilets", "boathouse",
    "bunker", "transformer_tower", "guardhouse", "sauna", "gazebo", "ruins", "static_caravan", "allotment_house", "farm_auxiliary", "bridge",
    "elevator", "tent", "silo", "tank", "storage_tank", "chimney", "digester", "pavilion", "shelter", "bakehouse",
})
IND_TYPES = frozenset({"industrial", "warehouse", "factory", "manufacture", "barn", "stable", "cowshed", "hangar", "parking", "depot", "storage", "workshop"})
CIVIC_TYPES = frozenset({
    "school", "university", "college", "kindergarten", "hospital", "government", "public", "civic", "museum", "theatre", "library", "train_station",
    "transportation", "fire_station", "townhall", "courthouse", "prison", "police", "stadium", "sports_hall", "sports_centre", "gymnasium", "clinic",
    "community_centre", "embassy", "castle", "palace",
})
CIVIC_AMENITY = frozenset({"school", "university", "college", "kindergarten", "hospital", "townhall", "courthouse", "theatre", "library", "community_centre", "police", "fire_station", "events_venue", "arts_centre"})
COMM_TYPES = frozenset({"retail", "commercial", "supermarket", "office", "hotel", "shop", "restaurant", "cafe", "mall", "bank", "inn", "guest_house"})
APT_TYPES = frozenset({"apartments", "dormitory"})
RES_TYPES = frozenset({"house", "detached", "semidetached_house", "terrace", "bungalow", "farm", "residential_house", "villa"})
TINY_AREA_M2 = 15.0


def type_class(sig: dict) -> str:
    """sacral | civic | comm | ind | aux | apt | res | mixed - the coarse role of a building, from its OSM type, OSM tags,
    size and height. Drives every rule table."""
    t = (sig.get("type_raw") or "yes").lower()
    tags = sig.get("tags") or {}
    if t in SACRAL_TYPES or tags.get("amenity") == "place_of_worship":
        return "sacral"
    if t in AUX_TYPES or sig["area"] < TINY_AREA_M2:
        return "aux"
    if t in IND_TYPES:
        return "ind"
    if t in CIVIC_TYPES or tags.get("amenity") in CIVIC_AMENITY or tags.get("historic") == "castle":
        return "civic"
    if t in COMM_TYPES or tags.get("shop") or tags.get("office") or tags.get("tourism") in ("hotel", "hostel", "guest_house", "motel"):
        return "comm"
    if t in APT_TYPES:
        return "apt"
    if t in RES_TYPES:
        return "res"
    h, lv, area = sig["height"], sig["levels"] or 0, sig["area"]
    if sig["hsrc"] == "estimated_default" and not lv:  # the 9 m default says nothing: decide by footprint and surroundings
        if sig.get("urban") == "dense":
            return "apt" if area > 350 else "mixed"
        return "res" if area < 230 and sig.get("urban") != "dense" else "mixed"
    if lv >= 4 or h >= 14:
        return "apt"
    if h < 9.5 and area < 300:
        return "res"
    return "mixed"


def is_trivial(sig: dict) -> bool:
    """True for structures the AI is never asked about (garages, sheds, kiosks, carports, tiny buildings...)."""
    return (sig.get("type_raw") or "yes").lower() in AUX_TYPES or sig["area"] < TINY_AREA_M2


# OSM building values that say nothing about how a building looks ("a house", "an apartment block", "a building")
GENERIC_TYPES = frozenset({"yes", "house", "detached", "semidetached_house", "terrace", "residential", "apartments", "bungalow", "dormitory", "residential_house", "villa"})
AI_BIG_AREA_M2 = 1500.0  # a footprint this large is never an ordinary house
AI_TALL_M = 45.0         # a REAL height this tall is a landmark or a high-rise (25 m is just a Gruenderzeit block)
# facts that say something about WHAT a building is (style, architect, landmark, usage); a bare start date, a colour/material
# (owned by the OSM layer anyway), `heritage` or `notable` (Vienna links nearly every protected house to Wikidata) do not
AI_SPECIFIC_FACTS = ("arch=", "architect=", "hist=", "tour=", "amen=", "shop=", "office=", "castle=")


def is_informative(sig: dict) -> bool:
    """True when the AI can know more about this building than the rules do: it has a name (landmarks, institutions, shops), a
    real OSM fact (start date, style, heritage, notable, architect, colours...), a specific type (church, school, hotel, station...)
    or an exceptional size. A plain `building=yes` / house / apartment block with an estimated height has NOTHING the model could
    use: live test (looks-2, 130 such buildings): it answered the SAME template line for 88 of them (post-war, three storeys, flat
    roof), which would turn a town of gabled houses into a town of flat roofs. For those the rules sample the local prior instead,
    with per-building variety, and the AI budget goes to the buildings where its knowledge matters."""
    if sig.get("name"):
        return True
    if (sig.get("type_raw") or "yes").lower() not in GENERIC_TYPES:
        return True
    if any(token.startswith(AI_SPECIFIC_FACTS) for token in facts_string(sig).split(";")):
        return True
    if sig["area"] >= AI_BIG_AREA_M2:
        return True
    return sig["hsrc"] != "estimated_default" and sig["height"] >= AI_TALL_M


# ------------------------------------------------------------------------------------------------------ signals

TOUCH_DIST_M = 1.5       # two footprints touch when vertices of both lie this close
DENSITY_RADIUS_M = 60.0  # building coverage is measured in this circle around a building
BAND_CORE, BAND_MID = 0.40, 0.80  # distance to the area centre / (half of the smaller extent)


def _ring_centroid(ring: list) -> tuple[float, float]:
    n = len(ring)
    return (sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n) if n else (0.0, 0.0)


def _touch_counts(rings: dict[str, list]) -> dict[str, int]:
    """Number of OTHER buildings whose footprint vertices lie within TOUCH_DIST_M of this one's (shared party walls /
    shared OSM nodes). A spatial hash on the vertices keeps it linear."""
    cell = 3.0
    grid: dict[tuple[int, int], list[tuple[float, float, str]]] = {}
    for bid, ring in rings.items():
        for x, z in ring:
            grid.setdefault((int(x // cell), int(z // cell)), []).append((x, z, bid))
    near: dict[str, set] = {bid: set() for bid in rings}
    r2 = TOUCH_DIST_M * TOUCH_DIST_M
    for bid, ring in rings.items():
        mine = near[bid]
        for x, z in ring:
            cx, cz = int(x // cell), int(z // cell)
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for qx, qz, other in grid.get((cx + dx, cz + dz), ()):
                        if other != bid and other not in mine and (qx - x) ** 2 + (qz - z) ** 2 <= r2:
                            mine.add(other)
                            near[other].add(bid)
    return {bid: len(s) for bid, s in near.items()}


def _densities(items: list[tuple[str, float, float, float]]) -> dict[str, float]:
    """Building coverage 0..1 within DENSITY_RADIUS_M of each building (other buildings' footprint area whose centroid lies
    inside the circle; a single huge footprint is capped so one palace does not make its whole neighbourhood 'dense')."""
    cell = DENSITY_RADIUS_M
    grid: dict[tuple[int, int], list[tuple[str, float, float, float]]] = {}
    for item in items:
        grid.setdefault((int(item[1] // cell), int(item[2] // cell)), []).append(item)
    circle = math.pi * DENSITY_RADIUS_M ** 2
    r2 = DENSITY_RADIUS_M ** 2
    out: dict[str, float] = {}
    for bid, x, z, _a in items:
        cx, cz = int(x // cell), int(z // cell)
        total = 0.0
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                for ob, ox, oz, oa in grid.get((cx + dx, cz + dz), ()):
                    if ob != bid and (ox - x) ** 2 + (oz - z) ** 2 <= r2:
                        total += min(oa, 6000.0)
        out[bid] = min(1.0, total / circle)
    return out


def urban_class(touch: int, dens: float, band: str) -> str:
    """dense | town | open: how built-up the surroundings of a building are."""
    score = min(touch, 3) * 0.25 + min(dens / 0.5, 1.0) * 0.5 + (0.1 if band == "core" else 0.0)
    return "dense" if score >= 0.75 else "town" if score >= 0.35 else "open"


POI_GROUND_KINDS = frozenset({"shop", "food", "service", "lodging"})


class Signals:
    """Everything the looks layers know about each building record of one dataset.
    by_id[id] -> signal dict; roots = ids that are their own root (outlines and unsplit buildings, plus parts with no
    outline); parts_of[root] = ids of its parts; root_of[id] = root id."""

    def __init__(self) -> None:
        self.by_id: dict[str, dict] = {}
        self.roots: list[str] = []
        self.parts_of: dict[str, list[str]] = {}
        self.root_of: dict[str, str] = {}
        self.half_extent_m: float = 600.0

    def ai_candidates(self, include_uninformative: bool = False) -> list[str]:
        """Root ids worth an AI question: roots that are neither trivial (garages, sheds...) nor uninformative (plain generic
        buildings the model knows nothing about, see is_informative; `include_uninformative` asks about those too)."""
        return [rid for rid in self.roots if not is_trivial(self.by_id[rid]) and (include_uninformative or is_informative(self.by_id[rid]))]


def _osm_leaves(sig: dict, b: dict) -> tuple[dict, dict]:
    """(osm facts, dataset-level user facts) of one record as flat leaf dicts. A shop/food/service/lodging POI makes the GROUND
    floor a shop for the building (not for a church); its parts inherit that (street-level parts only), see build_signals."""
    osm: dict[str, Any] = {}
    user: dict[str, Any] = {}
    if sig["era_osm"]:
        osm["era"] = sig["era_osm"]
    surfaces = b.get("surfaces") or {}
    for group, mat_of in (("wall", wall_material_of), ("roof", roof_material_of)):
        slot = surfaces.get(group)
        if not isinstance(slot, dict):
            continue
        target = osm if slot.get("source") == "osm" else user if slot.get("source") == "user" else None
        if target is None:
            continue
        material = mat_of(slot.get("material"))
        if material:
            target[f"{group}.material"] = material
        colour = norm_colour(slot.get("colour"))
        if colour:
            target[f"{group}.colour"] = colour
    lv = sig["levels"]
    if lv and FLOORS_RANGE[0] <= lv <= FLOORS_RANGE[1]:
        osm["floors"] = lv
    if sig["roof_shape"]:
        osm["roof.shape"] = sig["roof_shape"]
    if sig["poi_ground"] and sig["root"] == sig["id"] and sig["tcls"] != "sacral":  # a gift shop inside a cathedral does not give it a shopfront
        osm["ground"] = "shop"
    return osm, user


def build_signals(ds: dict, osm_tags: Optional[dict] = None) -> Signals:
    """Dataset (+ optional raw OSM tags joined by id) -> Signals for every building/part record."""
    osm_tags = osm_tags or {}
    sg = Signals()
    area = ds.get("area") or {}
    ext = area.get("extent_m") or {}
    span_x = _num(ext.get("x1"), 600.0) - _num(ext.get("x0"), -600.0)
    span_z = _num(ext.get("z1"), 600.0) - _num(ext.get("z0"), -600.0)
    sg.half_extent_m = max(100.0, min(span_x, span_z) / 2.0)
    records = [b for b in (ds.get("buildings") or []) if isinstance(b, dict) and isinstance(b.get("id"), str)]
    known = {b["id"] for b in records}
    rings: dict[str, list] = {}
    dens_items: list[tuple[str, float, float, float]] = []
    for b in records:
        bid = b["id"]
        parent = b.get("part_of")
        root = parent if isinstance(parent, str) and parent in known and parent != bid else bid
        sg.root_of[bid] = root
        footprint = b.get("footprint") or {}
        ring = footprint.get("outer") if isinstance(footprint, dict) else None
        ring = [p for p in (ring or []) if isinstance(p, (list, tuple)) and len(p) >= 2]
        centroid = b.get("centroid")
        if isinstance(centroid, (list, tuple)) and len(centroid) >= 2:
            x, z = _num(centroid[0], 0.0), _num(centroid[1], 0.0)
        else:
            x, z = _ring_centroid(ring)
        tags = osm_tags.get(re.sub(r"\.\d+$", "", bid)) or {}
        levels = b.get("levels")
        levels = levels if isinstance(levels, int) and not isinstance(levels, bool) and levels > 0 else None
        roof = b.get("roof") if isinstance(b.get("roof"), dict) else {}
        roof_h = roof.get("height_m")
        sig = {
            "id": bid, "root": root, "kind": b.get("kind") or "building",
            "type_raw": (b.get("type_raw") or "yes") if isinstance(b.get("type_raw"), str) else "yes", "type": b.get("type"),
            "name": b.get("name") if isinstance(b.get("name"), str) else None,
            "address": b.get("address") if isinstance(b.get("address"), str) else None,
            "area": max(0.0, _num(b.get("area_m2"), 0.0)), "height": max(0.5, _num(b.get("height_m"), 9.0)),
            "hsrc": b.get("height_source") if isinstance(b.get("height_source"), str) else "estimated_default",
            "levels": levels, "min_h": max(0.0, _num(b.get("min_height_m"), 0.0)),
            "roof_shape": roof_shape_of(roof.get("shape")), "roof_h": float(roof_h) if isinstance(roof_h, (int, float)) and not isinstance(roof_h, bool) and roof_h > 0 else None,
            "x": x, "z": z, "tags": tags,
            "touch": 0, "dens": 0.0, "band": "mid", "urban": "town", "poi": [], "poi_ground": False,
        }
        sig["era_osm"] = _era_from_tags(tags, root)
        sg.by_id[bid] = sig
        if root == bid:
            sg.roots.append(bid)
            if len(ring) >= 3:
                rings[bid] = [(_num(p[0], 0.0), _num(p[1], 0.0)) for p in ring]
            dens_items.append((bid, x, z, sig["area"]))
        else:
            sg.parts_of.setdefault(root, []).append(bid)
    touches = _touch_counts(rings)
    dens = _densities(dens_items)
    for rid in sg.roots:
        sig = sg.by_id[rid]
        sig["touch"] = touches.get(rid, 0)
        sig["dens"] = round(dens.get(rid, 0.0), 3)
        r = math.hypot(sig["x"], sig["z"]) / sg.half_extent_m
        sig["band"] = "core" if r < BAND_CORE else "mid" if r < BAND_MID else "edge"
        sig["urban"] = urban_class(sig["touch"], sig["dens"], sig["band"])
    # POIs -> their building's root
    pois: dict[str, list] = {}
    for p in ds.get("pois") or []:
        if isinstance(p, dict) and isinstance(p.get("building_id"), str) and p["building_id"] in sg.root_of:
            pois.setdefault(sg.root_of[p["building_id"]], []).append(p)
    for rid, plist in pois.items():
        sig = sg.by_id[rid]
        labels = sorted({_clean_text(f"{p.get('kind')}:{p.get('subkind')}" if p.get("subkind") else str(p.get("kind")), 26).replace(" ", "_") for p in plist if p.get("kind")})
        sig["poi"] = labels
        sig["poi_ground"] = any(p.get("kind") in POI_GROUND_KINDS for p in plist)
    # parts share the context of their root; type class + facts last (they need the context)
    for bid, sig in sg.by_id.items():
        root_sig = sg.by_id[sig["root"]]
        if root_sig is not sig:
            for k in ("touch", "dens", "band", "urban", "poi", "poi_ground"):
                sig[k] = root_sig[k]
    for bid, sig in sg.by_id.items():
        sig["tcls"] = type_class(sig)
    by_rec = {b["id"]: b for b in records}
    for bid, sig in sg.by_id.items():
        sig["osm_flat"], sig["user_flat"] = _osm_leaves(sig, by_rec[bid])
    for bid, sig in sg.by_id.items():
        if sig["root"] != bid:  # a part inherits the building's non-geometry facts; ground only exists on parts at street level
            root_sig = sg.by_id[sig["root"]]
            sig["osm_inherit"] = {k: v for k, v in root_sig["osm_flat"].items() if k not in GEOMETRY_LEAVES and not (k == "ground" and sig["min_h"] > 0.5)}
        else:
            sig["osm_inherit"] = {}
    return sg


def _era_from_tags(tags: dict, key: str) -> Optional[str]:
    if not tags:
        return None
    rng = year_range(tags.get("start_date"))
    if rng:
        return era_from_range(rng[0], rng[1], key)
    return arch_era(tags.get("building:architecture"))


# ---------------------------------------------------------------------------------------- AI signature + content hash

def _clean_text(value: Optional[str], limit: int) -> str:
    if not value:
        return ""
    text = re.sub(r"[|;\r\n\t]+", " ", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].strip()


def facts_string(sig: dict) -> str:
    """The real OSM facts of a building as compact `key=value` tokens (what the AI is told as known)."""
    tags = sig.get("tags") or {}
    tokens: list[str] = []
    if tags.get("start_date"):
        tokens.append("start=" + _clean_text(tags["start_date"], 14))
    if tags.get("building:architecture"):
        tokens.append("arch=" + _clean_text(tags["building:architecture"], 24))
    if tags.get("heritage"):
        tokens.append("heritage")
    if tags.get("wikidata"):
        tokens.append("notable")
    if tags.get("architect"):
        tokens.append("architect=" + _clean_text(tags["architect"].split(";")[0], 28))
    for key, short in (("historic", "hist"), ("tourism", "tour"), ("amenity", "amen"), ("shop", "shop"), ("office", "office"), ("religion", "rel"), ("castle_type", "castle")):
        if tags.get(key) and tags[key].strip().lower() not in ("yes", "undefined", "unknown"):  # a value that says nothing wastes tokens
            tokens.append(f"{short}={_clean_text(tags[key], 20)}")
    flat = sig.get("osm_flat") or {}
    for path, short in (("wall.material", "mat"), ("wall.colour", "col"), ("roof.material", "rmat"), ("roof.colour", "rcol")):
        if path in flat:
            tokens.append(f"{short}={flat[path]}")
    return ";".join(tokens)


def ai_fields(sig: dict) -> tuple[str, ...]:
    """The exact columns the AI is shown for one building (after idx): type_raw|name|street+number|area_m2|height_m|levels|
    osm_roof|touch|centre_band|poi_kinds|osm_facts. Quantised (area to 10 m2, height to 1 m, touch capped), no coordinates,
    no ids: identical looking buildings have identical fields, so they are asked once and cached once."""
    m2 = max(10, int(sig["area"] / 10.0 + 0.5) * 10)
    h = int(sig["height"] + 0.5)
    estimated = "e" if sig["hsrc"] == "estimated_default" else ""
    return (
        _clean_text(sig["type_raw"], 24) or "yes",
        _clean_text(sig["name"], 48),
        _clean_text(sig["address"], 40),
        str(m2),
        f"{h}{estimated}",
        str(sig["levels"] or ""),
        sig["roof_shape"] or "",
        str(min(int(sig["touch"]), 6)),
        sig["band"],
        "+".join(sig["poi"][:3]),
        facts_string(sig),
    )


def content_hash(fields: tuple, *, prompt_version: str, vocab_version: int, model: str, context_key: str = "") -> str:
    """sha256 over the canonical JSON of (prompt version, vocabulary version, model id, area context, quantised signature
    fields): same signature -> same cached answer -> no AI call, ever. Key order and whitespace cannot change it."""
    payload = {"pv": prompt_version, "vv": vocab_version, "m": model, "ctx": context_key, "sig": list(fields)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------------------------ rules
# Weight tables are plain data; every rule is `pick(weights, u01(building id, leaf name))`, conditioned on what is already
# known about the building. Era groups: OLD = med bar kla his, NEW = pwr lmd con.

_OLD = ("med", "bar", "kla", "his")

ERA_BASE: dict[str, dict[str, float]] = {
    "apt": dict(med=.01, bar=.07, kla=.09, his=.30, int=.09, pwr=.20, lmd=.10, con=.09, ver=.05),
    "mixed": dict(med=.02, bar=.08, kla=.10, his=.26, int=.07, pwr=.17, lmd=.09, con=.08, ver=.13),
    "res": dict(med=.01, bar=.03, kla=.05, his=.07, int=.07, pwr=.20, lmd=.20, con=.14, ver=.23),
    "civic": dict(med=.03, bar=.07, kla=.10, his=.30, int=.08, pwr=.16, lmd=.10, con=.10, ver=.06),
    "sacral": dict(med=.25, bar=.40, kla=.08, his=.17, int=.02, pwr=.04, lmd=.02, con=.02, ver=0.0),
    "comm": dict(med=.01, bar=.03, kla=.05, his=.18, int=.07, pwr=.20, lmd=.20, con=.22, ver=.04),
    "ind": dict(med=0.0, bar=.01, kla=.02, his=.15, int=.15, pwr=.25, lmd=.20, con=.15, ver=.07),
    "aux": dict(med=0.0, bar=.01, kla=.01, his=.03, int=.06, pwr=.22, lmd=.22, con=.15, ver=.30),
}
ERA_URBAN_TILT: dict[str, dict[str, float]] = {
    "dense": dict(med=1.3, bar=1.5, kla=1.5, his=1.5, int=1.0, pwr=.9, lmd=.6, con=.7, ver=.3),
    "town": dict(med=1, bar=1, kla=1, his=1, int=1, pwr=1, lmd=1, con=1, ver=1),
    "open": dict(med=.6, bar=.6, kla=.6, his=.6, int=.9, pwr=1.1, lmd=1.3, con=1.2, ver=1.6),
}


def rule_era(rsig: dict) -> str:
    """Era of a building (of its ROOT: parts share it): real dates first, else a weighted pick by type, surroundings and
    heritage signals."""
    if rsig["era_osm"]:
        return rsig["era_osm"]
    tags = rsig.get("tags") or {}
    weights = {e: ERA_BASE[rsig["tcls"]][e] * ERA_URBAN_TILT[rsig["urban"]][e] for e in ERAS}
    old_signal = bool(tags.get("heritage")) or tags.get("historic") in ("castle", "monument", "building", "yes", "heritage") or bool(tags.get("castle_type"))
    if old_signal:
        for e in ERAS:
            weights[e] *= 3.5 if e in _OLD else 0.12
    if rsig["band"] == "core":
        for e in ("bar", "kla", "his"):
            weights[e] *= 1.2
    elif rsig["band"] == "edge":
        for e in ("pwr", "lmd", "con", "ver"):
            weights[e] *= 1.15
    if rsig["height"] >= 45 and rsig["hsrc"] != "estimated_default":
        for e in ERAS:
            weights[e] *= {"pwr": 2.0, "lmd": 2.0, "con": 2.5}.get(e, 0.4 if e in _OLD else 1.0)
    if (rsig["levels"] or 0) >= 5 and rsig["urban"] == "dense":
        weights["his"] *= 1.3
    if rsig["area"] < 60 and rsig["tcls"] in ("res", "mixed"):
        for e in ERAS:
            weights[e] *= 2.0 if e == "ver" else 0.4 if e in _OLD else 1.0
    return pick([(e, weights[e]) for e in ERAS], u01(rsig["id"], "era"))


WALL_BY_ERA: dict[str, dict[str, float]] = {
    "med": dict(stone=.55, stucco=.45),
    "bar": dict(stucco=.78, stone=.2, brick=.02),
    "kla": dict(stucco=.9, stone=.07, brick=.03),
    "his": dict(stucco=.8, brick=.1, stone=.1),
    "int": dict(stucco=.7, brick=.2, concrete=.05, stone=.05),
    "pwr": dict(stucco=.6, concrete=.22, brick=.12, metal=.02, glass=.02, stone=.02),
    "lmd": dict(stucco=.5, concrete=.2, brick=.12, glass=.08, metal=.05, wood=.05),
    "con": dict(stucco=.35, concrete=.2, glass=.2, metal=.1, wood=.1, brick=.05),
    "ver": dict(stucco=.72, brick=.1, wood=.1, stone=.08),
}
WALL_TYPE_MULT: dict[str, dict[str, float]] = {
    "sacral": dict(stone=4.0, stucco=1.0, brick=.5, concrete=.3, glass=.1, metal=.1, wood=.1),
    "civic": dict(stone=1.8, brick=1.3, glass=1.3),
    "comm": dict(glass=2.2, concrete=1.6, metal=1.8, stone=1.1),
    "ind": dict(brick=2.5, concrete=2.5, metal=3.0, stucco=.5, stone=.3),
    "aux": dict(concrete=2.5, wood=3.0, metal=2.5, brick=1.5, stucco=.6, stone=.3),
}


def rule_wall_material(rsig: dict, era: str) -> str:
    weights = dict(WALL_BY_ERA[era])
    for mat, mult in WALL_TYPE_MULT.get(rsig["tcls"], {}).items():
        if mat in weights:
            weights[mat] *= mult
    return pick([(m, weights[m]) for m in WALL_MATERIALS if m in weights], u01(rsig["id"], "wall.material"))


def rule_wall_colour(rsig: dict, era: str, material: str) -> str:
    uid = rsig["id"]
    if material == "stucco":
        family = pick(STUCCO_BY_ERA[era], u01(uid, "wall.family"))
        palette = STUCCO_FAMILIES[family]
    else:
        palette = WALL_PALETTES[material]
    base = palette[int(u01(uid, "wall.shade") * len(palette)) % len(palette)]
    return jitter_colour(base, uid, "wall.colour")


FLOOR_H_BY_ERA: dict[str, tuple[float, float]] = {
    "med": (3.1, 3.7), "bar": (3.5, 4.2), "kla": (3.1, 3.4), "his": (3.4, 4.2), "int": (2.9, 3.1),
    "pwr": (2.7, 2.9), "lmd": (2.9, 3.2), "con": (3.0, 3.3), "ver": (2.7, 3.1),
}


def _roof_extra(sig: dict) -> float:
    """Metres of the building's height that belong to the roof (so floors are counted up to the eave)."""
    if sig["roof_h"] is not None:
        return sig["roof_h"]
    if sig["hsrc"] == "levels":
        return 0.0  # the dataset height is levels * 3 m exactly: no roof inside it
    shape = sig["roof_shape"]
    if shape == "flat" or (shape is None and sig["tcls"] in ("comm", "ind", "aux")):
        return 0.0
    return min(4.0, 0.25 * sig["height"])


def rule_floor_h(sig: dict, era: str) -> float:
    """Metres per storey. Real geometry first (tagged height and tagged levels give the actual storey height), else by era."""
    lv = sig["levels"]
    if lv and sig["hsrc"] == "tag":
        eave = sig["height"] - sig["min_h"] - _roof_extra(sig)
        if eave > 0:
            return round(_clamp(eave / lv, FLOOR_H_RANGE[0], FLOOR_H_RANGE[1]) * 20.0) / 20.0
    lo, hi = FLOOR_H_BY_ERA[era]
    return round((lo + (hi - lo) * u01(sig["id"], "floor_h")) * 20.0) / 20.0


def rule_floors(sig: dict, floor_h: float) -> int:
    """Storeys up to the eave. Counted from the building's real height so the facade matches the geometry that exists
    (a rule never makes the city taller; the AI may suggest more floors, the renderer decides what to do with ai/osm/user)."""
    if sig["levels"]:
        return int(_clamp(sig["levels"], FLOORS_RANGE[0], FLOORS_RANGE[1]))
    eave = sig["height"] - sig["min_h"] - _roof_extra(sig)
    return int(_clamp(round(eave / max(floor_h, 2.4)), FLOORS_RANGE[0], FLOORS_RANGE[1]))


WIN_BY_ERA: dict[str, dict[str, float]] = {
    "med": dict(small=.45, tall=.25, sq=.25, none=.05),
    "bar": dict(tall=.55, sq=.3, small=.1, wide=.05),
    "kla": dict(tall=.5, sq=.45, small=.05),
    "his": dict(tall=.7, sq=.25, wide=.03, small=.02),
    "int": dict(sq=.5, wide=.2, tall=.12, strip=.1, small=.08),
    "pwr": dict(sq=.38, wide=.28, strip=.17, small=.1, tall=.04, glass=.03),
    "lmd": dict(sq=.35, wide=.3, strip=.15, glass=.1, small=.06, tall=.04),
    "con": dict(glass=.3, wide=.25, sq=.2, strip=.2, tall=.05),
    "ver": dict(sq=.55, small=.2, wide=.12, tall=.1, strip=.03),
}
WIN_TYPE_MULT: dict[str, dict[str, float]] = {
    "ind": dict(strip=4.0, small=2.0, none=4.0, tall=.3, glass=.5),
    "aux": dict(none=8.0, small=3.0, strip=1.5, tall=.2, glass=.1),
    "sacral": dict(tall=3.0, small=1.5, glass=.05, strip=.05, wide=.1),
    "civic": dict(tall=1.3, wide=1.3, strip=1.2),
    "comm": dict(glass=2.5, wide=1.5, strip=1.5),
}


def rule_win(rsig: dict, era: str) -> str:
    weights = dict(WIN_BY_ERA[era])
    for kind, mult in WIN_TYPE_MULT.get(rsig["tcls"], {}).items():
        weights[kind] = weights.get(kind, 0.03) * mult
    return pick([(w, weights[w]) for w in WINDOWS if w in weights], u01(rsig["id"], "win"))


GROUND_BY_CLASS: dict[str, dict[str, float]] = {
    "apt:dense": dict(shop=.38, res=.22, portal=.2, arcade=.05, garage=.05, none=.1),
    "apt:town": dict(shop=.18, res=.45, portal=.15, garage=.08, arcade=.02, none=.12),
    "apt:open": dict(res=.6, garage=.15, none=.15, portal=.05, shop=.05),
    "comm": dict(shop=.6, arcade=.08, res=.07, portal=.1, none=.1, garage=.05),
    "civic": dict(portal=.5, res=.25, arcade=.05, none=.2),
    "sacral": dict(portal=.85, none=.15),
    "res": dict(res=.78, garage=.12, none=.1),
    "ind": dict(none=.4, garage=.35, res=.15, portal=.1),
    "aux": dict(none=.6, garage=.4),
}


def rule_ground(sig: dict, rsig: dict, era: str) -> str:
    if sig["min_h"] > 0.5:
        return "none"  # a part that starts above the ground has no street level
    t = rsig["type_raw"].lower()
    if t in ("garage", "garages", "carport"):
        return "garage"
    tcls = rsig["tcls"]
    key = f"apt:{rsig['urban']}" if tcls in ("apt", "mixed") else tcls
    weights = dict(GROUND_BY_CLASS[key])
    if tcls in ("apt", "mixed") and era in ("bar", "kla", "his") and rsig["urban"] == "dense":
        weights["arcade"] = weights.get("arcade", 0.0) * 2.0
    if tcls in ("apt", "mixed") and era in ("pwr", "lmd", "con"):
        weights["garage"] = weights.get("garage", 0.0) * 1.5
    return pick([(g, weights[g]) for g in GROUNDS if g in weights], u01(rsig["id"], "ground"))


BALCONY_BY_ERA: dict[str, dict[str, float]] = {
    "med": dict(none=.95, juliet=.05),
    "bar": dict(none=.75, juliet=.2, row=.05),
    "kla": dict(none=.6, juliet=.3, row=.1),
    "his": dict(none=.45, juliet=.3, row=.2, loggia=.05),
    "int": dict(none=.45, row=.3, juliet=.1, loggia=.15),
    "pwr": dict(none=.3, row=.4, loggia=.2, juliet=.1),
    "lmd": dict(none=.3, row=.35, loggia=.25, juliet=.1),
    "con": dict(none=.25, row=.3, loggia=.3, juliet=.15),
    "ver": dict(none=.75, juliet=.1, row=.1, loggia=.05),
}


def rule_balcony(rsig: dict, era: str) -> str:
    tcls = rsig["tcls"]
    if tcls in ("sacral", "civic", "ind", "aux"):
        weights = dict(none=.96, juliet=.02, row=.01, loggia=.01)
    elif tcls == "res":
        weights = dict(none=.6, loggia=.2, row=.1, juliet=.1) if era in ("pwr", "lmd", "con") else dict(none=.82, juliet=.06, loggia=.06, row=.06)
    else:
        weights = BALCONY_BY_ERA[era]
    return pick([(b, weights[b]) for b in BALCONIES if b in weights], u01(rsig["id"], "balcony"))


CORNICE_BY_ERA: dict[str, tuple[float, float, float]] = {
    "med": (.5, .45, .05), "bar": (.15, .4, .45), "kla": (.15, .55, .3), "his": (.1, .35, .55), "int": (.55, .35, .1),
    "pwr": (.9, .1, 0.0), "lmd": (.97, .03, 0.0), "con": (.97, .03, 0.0), "ver": (.8, .2, 0.0),
}


def rule_cornice(rsig: dict, era: str) -> int:
    w0, w1, w2 = CORNICE_BY_ERA[era]
    tcls = rsig["tcls"]
    if tcls in ("aux", "ind"):
        w0, w1, w2 = .97, .03, 0.0
    elif tcls == "res":
        w2 *= .5
    elif tcls == "sacral":
        w1, w2 = w1 * 1.2, w2 * 1.3
    elif tcls == "civic":
        w2 *= 1.3
    return pick([(0, w0), (1, w1), (2, w2)], u01(rsig["id"], "cornice"))


ROOF_BASE: dict[str, dict[str, float]] = {
    "apt": dict(gabled=.30, hipped=.25, flat=.22, mansard=.08, **{"half-hipped": .08}, skillion=.04, pyramidal=.01, gambrel=.0),
    "mixed": dict(gabled=.32, hipped=.25, flat=.2, mansard=.06, **{"half-hipped": .08}, skillion=.05, pyramidal=.01, gambrel=.01),
    "res": dict(gabled=.45, hipped=.28, **{"half-hipped": .10}, flat=.06, skillion=.05, mansard=.03, gambrel=.01, pyramidal=.02),
    "civic": dict(hipped=.3, gabled=.3, flat=.25, mansard=.05, pyramidal=.03, **{"half-hipped": .05}, dome=.02),
    "sacral": dict(gabled=.5, hipped=.15, pyramidal=.12, dome=.08, **{"half-hipped": .05}),
    "comm": dict(flat=.6, skillion=.12, gabled=.18, hipped=.06, mansard=.02, **{"half-hipped": .02}),
    "ind": dict(flat=.45, gabled=.35, skillion=.15, hipped=.05),
    "aux": dict(flat=.5, gabled=.25, skillion=.2, hipped=.05),
}


def rule_roof_shape(sig: dict, rsig: dict, era: str) -> str:
    """Roof shape: the type/era default (a rule: the renderer never builds geometry from it). A part uses its own height
    and size but the building's class and surroundings."""
    weights = dict(ROOF_BASE[rsig["tcls"]])
    if era in ("med", "bar", "kla", "his"):
        for shape in weights:
            weights[shape] *= 0.5 if shape == "flat" else 1.2
        if era == "his":
            weights["mansard"] = weights.get("mansard", 0.0) * 1.5
        if era == "bar":
            weights["dome"] = weights.get("dome", 0.0) * 1.5
    elif era in ("pwr", "lmd", "con"):
        weights["flat"] = weights.get("flat", 0.0) * (1.8 if era == "con" else 1.6)
        if era == "con":
            weights["skillion"] = weights.get("skillion", 0.0) * 1.5
    elif era == "ver":
        weights["gabled"] = weights.get("gabled", 0.0) * 1.3
    if rsig["touch"] >= 2:  # terraced: shared party walls leave no free sides for a hip
        weights["hipped"] = weights.get("hipped", 0.0) * 0.6
        weights["gabled"] = weights.get("gabled", 0.0) * 1.2
        weights["skillion"] = weights.get("skillion", 0.0) * 1.2
    if sig["height"] >= 30 and sig["hsrc"] != "estimated_default":
        for shape in weights:
            weights[shape] *= 3.0 if shape == "flat" else 0.4
    return pick([(s, weights[s]) for s in ROOF_SHAPES if s in weights], u01(sig["id"], "roof.shape"))


def rule_roof_material(rsig: dict, era: str, shape: str) -> str:
    modern = era in ("lmd", "con")
    if shape == "flat":
        weights = dict(tar=.55, concrete=.18, metal=.1, grass=.08 if modern else .01, glass=.02)
    elif shape == "dome":
        weights = dict(metal=.6, stone=.15, tiles=.15, glass=.1)
    elif shape == "pyramidal":
        weights = dict(tiles=.5, metal=.3, slate=.2)
    elif shape == "skillion":
        weights = dict(metal=.45, tar=.25, tiles=.2, concrete=.05)
    else:
        weights = dict(tiles=.78, slate=.15 if era in ("bar", "kla", "his") or shape == "mansard" else .03, metal=.2 if era == "con" else .1, concrete=.04 if era in ("pwr", "lmd") else .0, thatch=.02 if era == "ver" else .0)
        if shape == "mansard":
            weights["slate"] = .5
    return pick([(m, weights[m]) for m in ROOF_MATERIALS if m in weights], u01(rsig["id"], "roof.material"))


def rule_roof_colour(rsig: dict, material: str) -> str:
    palette = ROOF_PALETTES[material]
    base = palette[int(u01(rsig["id"], "roof.shade") * len(palette)) % len(palette)]
    return jitter_colour(base, rsig["id"], "roof.colour")


WEATHER_BASE = dict(med=.72, bar=.66, kla=.58, his=.52, int=.5, pwr=.42, lmd=.32, con=.14, ver=.45)


def rule_weather(rsig: dict, era: str) -> float:
    w = WEATHER_BASE[era] + (u01(rsig["id"], "weather") - 0.5) * 0.24 + (0.05 if rsig["urban"] == "dense" else 0.0)
    return round(_clamp(w, 0.0, 1.0), 2)


# ------------------------------------------------------------------------------------------------- resolve a record

AI_WALL_MAX_S, AI_ROOF_MAX_S = 0.50, 0.62  # saturation caps for AI colours (also applied when answers are parsed)


def _layered(path: str, user: dict, osm: dict, ai: dict) -> tuple[Any, str]:
    v = user.get(path)
    if v is not None:
        return v, "user"
    v = osm.get(path)
    if v is not None:
        return v, "osm"
    v = ai.get(path)
    if v is not None:
        return v, "ai"
    return None, "rule"


def _best(a: str, b: str) -> str:
    return a if SRC_RANK[a] >= SRC_RANK[b] else b


def resolve_record(sig: dict, rsig: dict, user: dict, osm: dict, ai: dict) -> dict:
    """One complete record from the layers (flat leaf dicts, highest priority checked first per leaf). Whatever no layer
    knows is generated by the rules, conditioned on the already-resolved era / materials / roof shape."""
    era, s_era = _layered("era", user, osm, ai)
    if era is None:
        era = rule_era(rsig)
    wmat, s_wmat = _layered("wall.material", user, osm, ai)
    if wmat is None:
        wmat = rule_wall_material(rsig, era)
    wcol, s_wcol = _layered("wall.colour", user, osm, ai)
    if wcol is None:
        wcol = rule_wall_colour(rsig, era, wmat)
    shape, s_shape = _layered("roof.shape", user, osm, ai)
    if shape is None:
        shape = rule_roof_shape(sig, rsig, era)
    rmat, s_rmat = _layered("roof.material", user, osm, ai)
    if rmat is None:
        rmat = rule_roof_material(rsig, era, shape)
    rcol, s_rcol = _layered("roof.colour", user, osm, ai)
    if rcol is None:
        rcol = rule_roof_colour(rsig, rmat)
    fh, s_fh = _layered("floor_h", user, osm, ai)
    if fh is None:
        fh = rule_floor_h(sig, era)
    floors, s_floors = _layered("floors", user, osm, ai)
    if floors is None:
        floors = rule_floors(sig, fh)
    win, s_win = _layered("win", user, osm, ai)
    if win is None:
        win = rule_win(rsig, era)
    ground, s_ground = _layered("ground", user, osm, ai)
    if ground is None:
        ground = rule_ground(sig, rsig, era)
    balcony, s_balcony = _layered("balcony", user, osm, ai)
    if balcony is None:
        balcony = rule_balcony(rsig, era)
    cornice, s_cornice = _layered("cornice", user, osm, ai)
    if cornice is None:
        cornice = rule_cornice(rsig, era)
    weather, s_weather = _layered("weather", user, osm, ai)
    if weather is None:
        weather = rule_weather(rsig, era)
    # group provenance: wall = the better of material/colour; floors = floors; roof = roof.SHAPE (the only geometry leaf of the
    # group, so a rule shape is never mistaken for a fact just because the roof colour is real)
    src = {"era": s_era, "wall": _best(s_wmat, s_wcol), "floors": s_floors, "win": s_win, "ground": s_ground, "balcony": s_balcony,
           "cornice": s_cornice, "roof": s_shape}
    # leaf provenance, ONLY where it differs from its group (compact): consumers read src[leaf] ?? src[group]
    for leaf, leaf_src in (("wall.material", s_wmat), ("wall.colour", s_wcol), ("floor_h", s_fh), ("roof.material", s_rmat), ("roof.colour", s_rcol)):
        if leaf_src != src[GROUP_OF_LEAF[leaf]]:
            src[leaf] = leaf_src
    if s_weather != "rule":
        src["weather"] = s_weather
    return {
        "era": era, "wall": {"material": wmat, "colour": wcol}, "floors": floors, "floor_h": fh, "win": win, "ground": ground,
        "balcony": balcony, "cornice": cornice, "roof": {"shape": shape, "material": rmat, "colour": rcol}, "weather": weather, "src": src,
    }


# ----------------------------------------------------------------------------------------------------- file helpers

def atomic_write_json(path: str, obj: Any, *, indent: Optional[int] = None, sort_keys: bool = False) -> None:
    """Write-then-replace: a crash or power loss leaves either the old complete file or the new complete file."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    text = json.dumps(obj, indent=indent, sort_keys=sort_keys, separators=None if indent else (",", ":"))
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(8):  # Windows: a virus scanner may hold the target for a moment
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.remove(tmp)  # never leave a half-written temp file behind; the old file is untouched
        except OSError:
            pass
        raise


def read_json_tolerant(path: str) -> tuple[Any, str]:
    """-> (value, 'ok') | (None, 'missing') | (None, 'corrupt'). Never raises for a bad file."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), "ok"
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError) as exc:
        log.warning("looks: %s is unreadable (%s) - treated as empty", path, exc)
        return None, "corrupt"


def _backup_bad_file(path: str) -> None:
    """Keep what we could not read: it is moved aside before a fresh file takes its place."""
    try:
        os.replace(path, f"{path}.bad-{int(time.time())}")
    except OSError:
        pass


def read_dataset(path: str) -> dict:
    with gzip.open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


# ------------------------------------------------------------------------------------------------------- the stores

class LooksCache:
    """Content-hash cache shared by all areas: {hash: {"ai": record, "model", "t"}} in one JSON file. Tolerant (a corrupt
    file is an empty cache plus a warning, never a crash), validated on read, written atomically."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.entries: dict[str, dict] = {}
        self._bad = False
        obj, status = read_json_tolerant(path)
        if status == "corrupt" or (status == "ok" and not isinstance(obj, dict)):
            self._bad = True
            if status == "ok":
                log.warning("looks: %s is not a cache object - treated as empty", path)
            return
        for h, e in (obj or {}).items():
            if isinstance(h, str) and isinstance(e, dict):
                ai, _problems = validate_record(e.get("ai"))
                if ai:
                    self.entries[h] = {"ai": ai, "model": str(e.get("model") or ""), "t": e.get("t")}

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, h: str) -> bool:
        return h in self.entries

    def get(self, h: str) -> Optional[dict]:
        e = self.entries.get(h)
        return copy.deepcopy(e["ai"]) if e else None

    def put(self, h: str, ai: dict, model: str, t: Optional[str] = None) -> None:
        clean, _problems = validate_record(ai)
        if clean:
            self.entries[h] = {"ai": clean, "model": model, "t": t or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def save(self) -> None:
        if self._bad and os.path.exists(self.path):
            _backup_bad_file(self.path)
        self._bad = False
        atomic_write_json(self.path, self.entries)


def empty_looks(area_id: Optional[str]) -> dict:
    return {"schema": SCHEMA, "area_id": area_id, "prompt_version": None, "vocab_version": VOCAB_VERSION, "generated_at": None, "model": None,
            "usage": {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "thought_tokens": 0, "usd": 0.0}, "units": {}}


def read_looks(path: str) -> dict:
    """looks.json -> normalised dict (always has `units`, every unit's `ai` re-validated). Missing/corrupt -> empty layer."""
    obj, status = read_json_tolerant(path)
    looks = empty_looks(None)
    if status != "ok" or not isinstance(obj, dict):
        return looks
    for k in ("area_id", "prompt_version", "vocab_version", "generated_at", "model"):
        looks[k] = obj.get(k) if isinstance(obj.get(k), (str, int)) else looks[k]
    usage = obj.get("usage")
    if isinstance(usage, dict):
        for k in looks["usage"]:
            if isinstance(usage.get(k), (int, float)) and not isinstance(usage.get(k), bool):
                looks["usage"][k] = usage[k]
    for uid, e in (obj.get("units") or {}).items() if isinstance(obj.get("units"), dict) else ():
        if isinstance(uid, str) and isinstance(e, dict):
            ai, _problems = validate_record(e.get("ai"))
            if ai:
                looks["units"][uid] = {"h": e.get("h") if isinstance(e.get("h"), str) else "", "ai": ai}
    return looks


def write_looks(path: str, looks: dict) -> None:
    atomic_write_json(path, looks)


class OverridesStore:
    """looks_overrides.json: Laszlo's edits {id: partial record}. Reading validates every entry (an invalid leaf is dropped
    with a warning, a corrupt file is empty); the original is kept as a `.bad-<time>` copy before the first rewrite, so a
    typo in a hand-edited file never silently destroys the rest of it."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.data: dict[str, dict] = {}
        self._lossy = False
        obj, status = read_json_tolerant(path)
        if status == "corrupt" or (status == "ok" and not isinstance(obj, dict)):
            self._lossy = True
            return
        for uid, partial in (obj or {}).items():
            if not isinstance(uid, str):
                self._lossy = True
                continue
            clean, problems = validate_record(partial)
            if problems:
                self._lossy = True
                log.warning("looks: overrides for %s ignored in part: %s", uid, "; ".join(problems[:4]))
            if clean:
                self.data[uid] = clean

    def save(self) -> None:
        if self._lossy and os.path.exists(self.path):
            _backup_bad_file(self.path)
        self._lossy = False
        atomic_write_json(self.path, self.data, indent=1, sort_keys=True)


# -------------------------------------------------------------------------------------------------- patch overrides

MAX_PATCHES_PER_MESSAGE = 500


def _delete_path(partial: dict, path: str) -> None:
    if path == "*":
        partial.clear()
    elif path in NESTED_GROUPS:
        partial.pop(path, None)
    elif "." in path:
        group, leaf = path.split(".", 1)
        sub = partial.get(group)
        if isinstance(sub, dict):
            sub.pop(leaf, None)
            if not sub:
                partial.pop(group, None)
    else:
        partial.pop(path, None)


def _deep_merge(base: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict):
            sub = base.setdefault(key, {})
            if isinstance(sub, dict):
                sub.update(value)
        else:
            base[key] = value


def patch_overrides(overrides: dict, patches: Any, known_ids: Iterable[str], batch_id: Any = None) -> tuple[dict, dict, set]:
    """Applies a batch of looks patches [{id, set?, clear?}] to a COPY of the overrides.
    Per item and atomically: unknown id, bad vocabulary, nothing to do -> rejected, never stored; otherwise `clear` is
    applied first, then `set` is deep-merged (nested partials). Returns (new overrides, ack, touched ids):
    ack = {batch_id, accepted, rejected: [{id, reason}] (first 50), rejected_count}. The caller persists and republishes."""
    known = known_ids if isinstance(known_ids, (set, frozenset, dict)) else set(known_ids)
    new = {k: copy.deepcopy(v) for k, v in overrides.items()}
    accepted = 0
    rejected: list[dict] = []
    touched: set[str] = set()
    items = patches if isinstance(patches, list) else []
    for index, item in enumerate(items):
        pid = item.get("id") if isinstance(item, dict) else None
        if index >= MAX_PATCHES_PER_MESSAGE:
            rejected.append({"id": pid if isinstance(pid, str) else None, "reason": f"batch too large (max {MAX_PATCHES_PER_MESSAGE})"})
            continue
        if not isinstance(item, dict) or not isinstance(pid, str):
            rejected.append({"id": pid if isinstance(pid, str) else None, "reason": "malformed patch"})
            continue
        if pid not in known:
            rejected.append({"id": pid, "reason": "unknown building id"})
            continue
        clear, clear_problems = validate_clear(item.get("clear"))
        sets: dict = {}
        set_problems: list[str] = []
        if item.get("set") is not None:
            sets, set_problems = validate_patch(item.get("set"))
        problems = clear_problems + set_problems
        if problems:
            rejected.append({"id": pid, "reason": "; ".join(problems[:4])})
            continue
        if not clear and not sets:
            rejected.append({"id": pid, "reason": "nothing to set or clear"})
            continue
        entry = new.setdefault(pid, {})
        for path in clear:
            _delete_path(entry, path)
        _deep_merge(entry, sets)
        if not entry:
            new.pop(pid, None)
        accepted += 1
        touched.add(pid)
    ack = {"batch_id": batch_id, "accepted": accepted, "rejected": rejected[:50], "rejected_count": len(rejected)}
    return new, ack, touched


# ------------------------------------------------------------------------------------------------- the area model

class AreaLooks:
    """The looks of one area: signals + the layers. Build once per area load (signals are the expensive part); `effective`
    is cheap and can be re-run for a few ids after a patch or a new looks.json.

        looks = AreaLooks(ds, osm_tags, ai_units=read_looks(...)["units"], overrides=store.data)
        records = looks.effective()                 # {id: complete record with src}
        records = looks.effective(looks.touched(["w123"]))   # only these (a root id includes its parts)
    """

    def __init__(self, ds: dict, osm_tags: Optional[dict] = None, *, ai_units: Optional[dict] = None, overrides: Optional[dict] = None,
                 ai_meta: Optional[dict] = None, signals: Optional[Signals] = None) -> None:
        self.area_id = (ds.get("area") or {}).get("id")
        self.signals = signals if signals is not None else build_signals(ds, osm_tags)
        self._ai: dict[str, dict] = {}
        self._ov: dict[str, dict] = {}
        self.ai_meta: dict = {}
        self.set_ai(ai_units or {}, ai_meta)
        self.set_overrides(overrides or {})

    # layers -------------------------------------------------------------------------------------------------------
    def set_ai(self, units: dict, meta: Optional[dict] = None) -> None:
        """units: {root id: {"h", "ai"}} as in looks.json (re-validated; an invalid field falls back to the rule)."""
        self._ai = {}
        for uid, e in units.items():
            ai, _problems = validate_record(e.get("ai") if isinstance(e, dict) else None)
            if ai and uid in self.signals.by_id:
                self._ai[uid] = flatten(ai)
        if meta is not None:
            self.ai_meta = meta

    def set_overrides(self, overrides: dict) -> None:
        self._ov = {}
        for uid, partial in overrides.items():
            clean, _problems = validate_record(partial)
            if clean:
                self._ov[uid] = flatten(clean)

    @property
    def ids(self) -> list[str]:
        return list(self.signals.by_id)

    def touched(self, ids: Iterable[str]) -> set[str]:
        """A root id stands for the building AND its parts; a part id for itself."""
        out: set[str] = set()
        for uid in ids:
            if uid in self.signals.by_id:
                out.add(uid)
                out.update(self.signals.parts_of.get(uid, ()))
        return out

    # resolution ---------------------------------------------------------------------------------------------------
    def resolve(self, uid: str) -> dict:
        sg = self.signals
        sig = sg.by_id[uid]
        root = sig["root"]
        rsig = sg.by_id[root]
        if root == uid:
            ai = self._ai.get(uid, _EMPTY)
            osm = sig["osm_flat"]
            user = {**sig["user_flat"], **self._ov.get(uid, _EMPTY)} if (sig["user_flat"] or uid in self._ov) else _EMPTY
        else:
            ai_root = self._ai.get(root, _EMPTY)
            ai = {k: v for k, v in ai_root.items() if k not in GEOMETRY_LEAVES and not (k == "ground" and sig["min_h"] > 0.5)} if ai_root else _EMPTY
            osm = {**sig["osm_inherit"], **sig["osm_flat"]}
            user = {**rsig["user_flat"], **sig["user_flat"], **self._ov.get(root, _EMPTY), **self._ov.get(uid, _EMPTY)}
        return resolve_record(sig, rsig, user, osm, ai)

    def effective(self, ids: Optional[Iterable[str]] = None) -> dict[str, dict]:
        """{unit id: complete record} for every building/part id (or only `ids`)."""
        want = self.signals.by_id if ids is None else [i for i in ids if i in self.signals.by_id]
        return {uid: self.resolve(uid) for uid in want}

    def rule_records(self) -> dict[str, dict]:
        """The pure rule layer (no AI, no overrides; OSM facts only as far as the rules themselves read them)."""
        out = {}
        for uid, sig in self.signals.by_id.items():
            out[uid] = resolve_record(sig, self.signals.by_id[sig["root"]], _EMPTY, _EMPTY, _EMPTY)
        return out

    def meta(self, records: Optional[dict] = None) -> dict:
        return meta(self.area_id, records if records is not None else self.effective(), self.ai_meta)


_EMPTY: dict = {}


def meta(area_id: Optional[str], records: dict, looks: Optional[dict] = None) -> dict:
    """`city_looks_meta`: how many units and where their field groups came from (counts of unit x group fields)."""
    by_src = {s: 0 for s in SOURCES}
    for rec in records.values():
        for group in SRC_GROUPS:
            by_src[rec["src"][group]] += 1
    looks = looks or {}
    usage = looks.get("usage") if isinstance(looks.get("usage"), dict) else empty_looks(None)["usage"]
    return {"schema": SCHEMA, "area_id": area_id, "units": len(records), "by_src": by_src, "model": looks.get("model"),
            "generated_at": looks.get("generated_at"), "usage": usage}


def partition_by_tile(records: dict[str, dict], tiles: dict) -> dict[tuple[int, int], dict[str, dict]]:
    """Splits effective records by tile: `tiles` is what city_data.partition_tiles(ds) returns ({(i, j): {"ct_buildings":
    [...]}}), so every unit's record lands in exactly the tile whose `ct_buildings` holds that unit (= the tile of its
    centroid). A tile without buildings gets {}."""
    out: dict[tuple[int, int], dict[str, dict]] = {}
    for key, tile in tiles.items():
        out[key] = {b["id"]: records[b["id"]] for b in tile.get("ct_buildings", ()) if b["id"] in records}
    return out


def effective_records(ds: dict, area_dir: Optional[str] = None, *, osm_tags: Optional[dict] = None, use_raw_tags: bool = True,
                      ai_units: Optional[dict] = None, overrides: Optional[dict] = None) -> dict[str, dict]:
    """Convenience: everything from the area's folder -> {unit id: complete record with src}. With `area_dir` the layers
    come from <area_dir>/looks.json, looks_overrides.json and the raw OSM cache; an area without looks.json still gets
    rule + osm records (free, deterministic). Explicit arguments win over the files."""
    if area_dir:
        if ai_units is None:
            ai_units = read_looks(os.path.join(area_dir, LOOKS_FILE))["units"]
        if overrides is None:
            overrides = OverridesStore(os.path.join(area_dir, OVERRIDES_FILE)).data
        if osm_tags is None and use_raw_tags:
            osm_tags = load_osm_tags(area_dir)
    return AreaLooks(ds, osm_tags, ai_units=ai_units, overrides=overrides).effective()
