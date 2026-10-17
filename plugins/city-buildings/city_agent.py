"""city_agent — the city's own AI agent (Phase B), built on agent_core.SpinAgent.

What it knows (and ONLY this — it has no handle on any other spin or on Core):
* the real places of the active area from the schema-v1 dataset: rail stations (name, name:en, mode,
  platforms), POIs (name, name:en, kind) and named / landmark buildings;
* what the user is looking at: `city_view_state` published by the city view (camera focus, distance,
  mode, the selected building), turned into a compact context (nearest places + nearest buildings);
* area aggregates (building count, height range, tallest few).

What it can do (every change goes through the SAME validators as the voice / drag edit paths,
city_buildings.ALLOWED_EDIT_FIELDS + the colour check below, and is persisted in the area overlay):
* find_places (read-only): resolve "the big train station", a name in any language, a POI kind;
* buildings_around (read-only): count / heights / types around a place, the selection or the focus;
* edit_buildings: colour, height (levels / metres / scale), material, type, name for the buildings
  within a radius of a place / the selection / the view focus (or for explicit ids);
* move_building: shift one building.
Edits relayed from the app sidebar are PROPOSED first (confirm card); chat inside the city applies
directly (agent_core.requires_confirmation).

AI efficiency: agent_core runs the budget model first, tiny tool schemas, a compact context (nearest
8 places + 10 buildings + selection, never a dump), a bounded step loop and a 60 s answer cache, and
reports requests / tokens in every result.
"""

import math
import re
import unicodedata
from typing import Any, Optional

import agent_core as ac

LEVEL_M = 3.0                 # one storey
MAX_EDIT_BUILDINGS = 400
DEFAULT_RADIUS_M = 150.0
MAX_RADIUS_M = 800.0
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

STATION_WORDS = ("train station", "railway station", "station", "bahnhof", "metro", "subway", "u-bahn", "ubahn", "s-bahn", "tram stop", "gare", "estacion", "eki")
MODE_RANK = {"train": 0, "subway": 1, "light_rail": 2, "monorail": 3, "tram": 4}


def _fold(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def _pos(item: dict) -> tuple:
    p = item.get("pos") or item.get("centroid") or (0.0, 0.0)
    return float(p[0]), float(p[1])


class CityKnowledge:
    """Places + buildings of the ACTIVE area, indexed once per area (linear scans are fine at 1-5k items)."""

    def __init__(self, ds: dict, buildings_by_id: dict):
        self.area_name = ds["area"]["name"]
        self.buildings = buildings_by_id            # legacy id -> building dict (the publisher's live records)
        self.places: list[dict] = []
        for s in ds["rail"]["stations"]:
            self.places.append({
                "ref": s["id"], "kind": "station", "mode": s.get("mode"), "name": s.get("name"), "name_en": s.get("name_en"),
                "x": s["pos"][0], "z": s["pos"][1], "platforms": len(s.get("platform_ids") or []), "entrances": len(s.get("entrance_ids") or []),
            })
        for p in ds["pois"]:
            if p.get("name") and p.get("pos"):
                self.places.append({"ref": p["id"], "kind": p.get("kind") or "poi", "sub": p.get("subkind"), "name": p["name"], "name_en": p.get("name_en"), "x": p["pos"][0], "z": p["pos"][1]})
        self._building_places = 0
        for b in ds["buildings"]:
            if b.get("has_parts"):
                continue
            raw = (b.get("type_raw") or "")
            if b.get("name") or raw in ("train_station", "station", "church", "cathedral", "hospital", "stadium", "castle", "palace", "museum", "town_hall"):
                legacy = b["id"][1:] if b["id"].startswith("w") else b["id"]
                self.places.append({"ref": "b:" + legacy, "kind": "building", "sub": raw, "name": b.get("name"), "name_en": b.get("name_en"), "x": b["centroid"][0], "z": b["centroid"][1], "area_m2": b.get("area_m2")})
                self._building_places += 1
        self._by_ref = {p["ref"]: p for p in self.places}
        self._folded = [(_fold(p.get("name")), _fold(p.get("name_en"))) for p in self.places]

    # -- places ---------------------------------------------------------------------------------------

    def find(self, query: str, kind: Optional[str], focus: tuple, limit: int = 8) -> list[dict]:
        q = _fold(query)
        want_station = kind == "station"
        for w in STATION_WORDS:
            if w in q:
                want_station = True
                q = q.replace(w, " ")
        for w in ("the", "big", "biggest", "large", "main", "this", "here", "near", "nearest", "nearby", "der", "die", "das", "den", "grosse", "große", "grosser"):
            q = re.sub(rf"\b{w}\b", " ", q)
        tokens = [t for t in re.split(r"\W+", q) if len(t) >= 2]
        out = []
        for place, (fn, fe) in zip(self.places, self._folded):
            if want_station and place["kind"] != "station":
                continue
            if kind and kind != "station" and place["kind"] != kind:
                continue
            if tokens:
                hay = fn + " " + fe + " " + _fold(place.get("sub"))
                hits = sum(1 for t in tokens if t in hay)
                if hits == 0:
                    continue
                score = hits * 100 - len(hay) * 0.01
            else:
                score = 0.0
            d = math.hypot(place["x"] - focus[0], place["z"] - focus[1])
            if place["kind"] == "station":
                score += 20 - 4 * MODE_RANK.get(place.get("mode") or "", 5) + 3 * (place.get("platforms") or 0)
            out.append((score - d * 0.01, d, place))
        out.sort(key=lambda t: -t[0])
        return [self._brief(p, d) for _s, d, p in out[:limit]]

    def _brief(self, p: dict, dist: float) -> dict:
        r = {"ref": p["ref"], "kind": p["kind"], "name": p.get("name"), "x": round(p["x"]), "z": round(p["z"]), "dist_m": round(dist)}
        if p.get("name_en") and p["name_en"] != p.get("name"):
            r["name_en"] = p["name_en"]
        for k in ("mode", "sub", "platforms"):
            if p.get(k):
                r[k] = p[k]
        return r

    def near_places(self, focus: tuple, n: int = 6, radius: float = 500.0) -> list[dict]:
        ranked = sorted(((math.hypot(p["x"] - focus[0], p["z"] - focus[1]), p) for p in self.places), key=lambda t: t[0])
        # stations first-class: always show the nearest few even when slightly further away
        out, seen = [], set()
        for d, p in ranked:
            if d > radius and len(out) >= 3:
                break
            if len(out) < n and p["ref"] not in seen:
                out.append(self._brief(p, d))
                seen.add(p["ref"])
        stations = [self._brief(p, d) for d, p in ranked if p["kind"] == "station"][:3]
        for s in stations:
            if s["ref"] not in seen:
                out.append(s)
        return out

    def place_pos(self, ref: str) -> Optional[tuple]:
        p = self._by_ref.get(ref)
        return (p["x"], p["z"]) if p else None

    # -- buildings ---------------------------------------------------------------------------------------

    def buildings_within(self, centre: tuple, radius: float) -> list[dict]:
        out = []
        for b in self.buildings.values():
            if b.get("part_of"):
                continue
            pos = b["position"]
            if math.hypot(pos["x"] - centre[0], pos["z"] - centre[1]) <= radius:
                out.append(b)
        return out

    def nearest_buildings(self, centre: tuple, n: int) -> list[tuple]:
        ranked = sorted(((math.hypot(b["position"]["x"] - centre[0], b["position"]["z"] - centre[1]), b) for b in self.buildings.values()), key=lambda t: t[0])
        return ranked[:n]

    def aggregate(self) -> dict:
        hs = [b["height_m"] for b in self.buildings.values()]
        tall = ac.top_n(list(self.buildings.values()), "height_m", 3)
        return {
            "name": self.area_name,
            "buildings": len(hs),
            "height_m": ac.aggregate(list(self.buildings.values()), "height_m"),
            "tallest": [{"id": b["id"], "name": b.get("name"), "h": round(b["height_m"])} for b in tall],
        }


def _num(args: dict, key: str) -> Optional[float]:
    v = args.get(key)
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def make_agent_parts(pub: Any):
    """-> (allowlist, context_provider, tools, system_prompt) for `pub`, the city_buildings.BuildingPublisher."""

    def view() -> dict:
        return pub.view_state or {}

    def focus_xy() -> tuple:
        f = view().get("focus") or {}
        return float(f.get("x", 0.0)), float(f.get("z", 0.0))

    def knowledge() -> CityKnowledge:
        if pub.knowledge is None:
            raise ac.ToolError("the city data is not loaded yet")
        return pub.knowledge

    def brief_building(b: dict, dist: Optional[float] = None) -> dict:
        r = {"id": b["id"], "type": b.get("building_type"), "h": round(b["height_m"])}
        if b.get("name"):
            r["name"] = b["name"]
        if dist is not None:
            r["dist_m"] = round(dist)
        return r

    def selected_building() -> Optional[dict]:
        sid = (view().get("selected") or {}).get("id") if isinstance(view().get("selected"), dict) else view().get("selected")
        if not isinstance(sid, str) or pub.knowledge is None:
            return None
        sid = sid[1:] if sid.startswith("w") else sid
        return pub.knowledge.buildings.get(sid)

    def context(_instruction: str) -> dict:
        k = pub.knowledge
        if k is None:
            return {"area": {"note": "no city loaded yet"}}
        fx, fz = focus_xy()
        v = view()
        near = k.nearest_buildings((fx, fz), 8)
        sel = selected_building()
        vis_r = max(80.0, min(500.0, float(v.get("distance_m") or 300.0) * 0.7))
        return {
            "area": k.aggregate(),
            "view": {
                "focus": [round(fx), round(fz)],
                "distance_m": round(float(v.get("distance_m") or 0)),
                "mode": v.get("mode"),
                "visible_radius_m": round(vis_r),
                "selected": ({**brief_building(sel), "x": round(sel["position"]["x"]), "z": round(sel["position"]["z"])} if sel else None),
                "near_places": k.near_places((fx, fz)),
                "near_buildings": [brief_building(b, d) for d, b in near],
            },
        }

    allowlist = ac.Allowlist({
        "area": ac.Section(fields=("name", "buildings", "height_m", "tallest", "note")),
        "view": ac.Section(fields=("focus", "distance_m", "mode", "visible_radius_m", "selected", "near_places", "near_buildings")),
    })

    # -- tools ------------------------------------------------------------------------------------------------

    def resolve_center(args: dict, k: CityKnowledge) -> tuple:
        c = args.get("center")
        if isinstance(c, str):
            if c == "selected":
                sel = selected_building()
                if sel is None:
                    raise ac.ToolError("nothing is selected in the city view")
                return (sel["position"]["x"], sel["position"]["z"]), "the selected building"
            if c == "focus":
                return focus_xy(), "the view centre"
            p = k.place_pos(c)
            if p is None:
                raise ac.ToolError(f"unknown place ref {c!r}; call find_places first and use its ref")
            return p, k._by_ref[c].get("name") or c
        x, z = _num(args, "x"), _num(args, "z")
        if x is not None and z is not None:
            return (x, z), f"({x:.0f}, {z:.0f})"
        return focus_xy(), "the view centre"

    def q_find_places(args: dict) -> Any:
        k = knowledge()
        kind = args.get("kind") if args.get("kind") in ("station", "poi", "building", "shop", "food", "transit", "school", "health", "work") else None
        res = k.find(str(args.get("query") or ""), kind, focus_xy())
        return res or "no matching place near here; try another name or kind"

    def q_buildings_around(args: dict) -> Any:
        k = knowledge()
        centre, label = resolve_center(args, k)
        r = max(10.0, min(MAX_RADIUS_M, _num(args, "radius_m") or DEFAULT_RADIUS_M))
        bs = k.buildings_within(centre, r)
        types: dict[str, int] = {}
        for b in bs:
            types[b.get("building_type") or "other"] = types.get(b.get("building_type") or "other", 0) + 1
        tall = ac.top_n(bs, "height_m", 5)
        return {"around": label, "radius_m": r, "count": len(bs), "height_m": ac.aggregate(bs, "height_m"), "types": types, "tallest": [brief_building(b) for b in tall]}

    def p_edit_buildings(args: dict) -> ac.Plan:
        k = knowledge()
        ids = args.get("ids")
        if isinstance(ids, list) and ids:
            chosen = []
            for i in ids[:MAX_EDIT_BUILDINGS]:
                b = k.buildings.get(str(i)[1:] if str(i).startswith("w") else str(i))
                if b is not None:
                    chosen.append(b)
            label = "listed"
        else:
            centre, label = resolve_center(args, k)
            r = max(10.0, min(MAX_RADIUS_M, _num(args, "radius_m") or DEFAULT_RADIUS_M))
            chosen = k.buildings_within(centre, r)
            label = f"within {r:.0f} m of {label}"
        if not chosen:
            raise ac.ToolError(f"no buildings {label}; try a larger radius or another centre")
        if len(chosen) > MAX_EDIT_BUILDINGS:
            raise ac.ToolError(f"{len(chosen)} buildings {label} is too many for one edit (max {MAX_EDIT_BUILDINGS}); use a smaller radius")

        colour = args.get("color") or args.get("colour")
        if colour is not None and not (isinstance(colour, str) and _HEX.match(colour)):
            raise ac.ToolError("color must be a hex string like #ff69b4")
        delta_levels, delta_m, abs_h, scale = _num(args, "height_delta_levels"), _num(args, "height_delta_m"), _num(args, "height_m"), _num(args, "height_scale")
        material, btype, name = args.get("material"), args.get("building_type"), args.get("name")
        if name is not None and len(chosen) != 1:
            raise ac.ToolError("a name can only be set on exactly one building")
        if all(v is None for v in (colour, delta_levels, delta_m, abs_h, scale, material, btype, name)):
            raise ac.ToolError("nothing to change: give color, a height change, material, building_type or name")

        specs = pub.edit_fields()
        items = []
        for b in chosen:
            fields: dict = {}
            if abs_h is not None or delta_levels is not None or delta_m is not None or scale is not None:
                h = abs_h if abs_h is not None else b["height_m"] * (scale if scale is not None else 1.0)
                h += (delta_levels or 0.0) * LEVEL_M + (delta_m or 0.0)
                ok, val = specs["building_height_m"]["validate"](max(1.0, min(500.0, h)))
                if not ok:
                    raise ac.ToolError(str(val))
                fields["height_m"] = val
            for attr, v in (("building_material", material), ("building_type", btype), ("building_name", name)):
                if v is not None:
                    ok, val = specs[attr]["validate"](v)
                    if not ok:
                        raise ac.ToolError(str(val))
                    fields[specs[attr]["overlay_key"]] = val
            items.append((b, fields))

        bits = []
        if colour:
            bits.append(f"paint them {colour}")
        if abs_h is not None:
            bits.append(f"set height to {abs_h:.0f} m")
        if delta_levels is not None or delta_m is not None:
            bits.append(f"raise them by {(delta_levels or 0) * LEVEL_M + (delta_m or 0):+.0f} m")
        if scale is not None:
            bits.append(f"scale height x{scale:g}")
        for lab, v in (("material", material), ("type", btype), ("name", name)):
            if v is not None:
                bits.append(f"set {lab} to {v}")
        noun = "building" if len(chosen) == 1 else "buildings"
        scope = label if label.startswith("within") else "(listed)"
        description = f"{len(chosen)} {noun} {scope}: " + ", ".join(bits)
        return ac.Plan(description, lambda: pub.apply_building_edits(items, colour.lower() if isinstance(colour, str) else None))

    def p_move_building(args: dict) -> ac.Plan:
        k = knowledge()
        bid = args.get("building")
        b = selected_building() if bid in (None, "selected") else k.buildings.get(str(bid)[1:] if str(bid).startswith("w") else str(bid))
        if b is None:
            raise ac.ToolError("which building? give its id or select one in the city view")
        dx, dz = _num(args, "dx_m") or 0.0, _num(args, "dz_m") or 0.0
        if dx == 0.0 and dz == 0.0:
            raise ac.ToolError("give dx_m (east +) and/or dz_m (north +)")
        if abs(dx) > 2000 or abs(dz) > 2000:
            raise ac.ToolError("that is too far; move at most 2000 m")
        return ac.Plan(f"move building {b['id']}{' ' + repr(b['name']) if b.get('name') else ''} by {dx:+.0f} m east, {dz:+.0f} m north", lambda: pub.move_building_by(b, dx, dz))

    tools = [
        ac.Tool("find_places", "Find real places of this city: stations, POIs, named buildings. Use for 'the big train station' or any name (any language, name:en works). Returns refs sorted by relevance near the view focus.",
                {"query": {"type": "string"}, "kind": {"type": "string", "description": "station|poi|building|shop|food|transit|school|health|work (optional)"}}, ("query",), query=q_find_places),
        ac.Tool("buildings_around", "Count/heights/types of buildings within radius_m of a place ref, 'selected' or 'focus'.",
                {"center": {"type": "string", "description": "place ref from find_places, or 'selected', or 'focus'"}, "radius_m": {"type": "number"}}, (), query=q_buildings_around),
        ac.Tool("edit_buildings", "Change the buildings within radius_m of a center (place ref / 'selected' / 'focus') or the listed ids: color (#rrggbb), height (height_delta_levels = storeys, 1 storey = 3 m; or height_delta_m, height_m, height_scale), material, building_type, name.",
                {"center": {"type": "string"}, "radius_m": {"type": "number"}, "ids": {"type": "array", "items": {"type": "string"}}, "color": {"type": "string"},
                 "height_delta_levels": {"type": "number"}, "height_delta_m": {"type": "number"}, "height_m": {"type": "number"}, "height_scale": {"type": "number"},
                 "material": {"type": "string"}, "building_type": {"type": "string"}, "name": {"type": "string"}}, (), prepare=p_edit_buildings),
        ac.Tool("move_building", "Move one building (default: the selected one) by dx_m east and dz_m north.",
                {"building": {"type": "string"}, "dx_m": {"type": "number"}, "dz_m": {"type": "number"}}, (), prepare=p_move_building),
    ]

    system = (
        "You are the assistant of a live simulation of a REAL city (OpenStreetMap data). You only know what is in the context and what your tools return; "
        "you cannot see or touch anything else. The context 'view' is what the user is looking at right now: 'this/here/that' means the view focus or the selected building. "
        "Resolve named or described places (\"the big train station\") with find_places first (prefer a station near the focus with the most platforms), then use its ref as the center. "
        "Change buildings only with edit_buildings / move_building. 1 storey = 3 m ('20 stories taller' = height_delta_levels 20). Colours are hex, e.g. pink #ff69b4. "
        "Around a big station use radius_m 150-250. If asked about anything that is not part of this city (classes, files, other apps, the user's data), say plainly that you can only see and change this city. "
        "Reply in the SAME LANGUAGE the user wrote in, in one or two short sentences, saying what you did or found. Never invent ids or places."
    )
    return allowlist, context, tools, system
