"""Normalize an OSM map extract and licensed Skadi HGT tile into the campus scene."""

import argparse
import gzip
import json
import re
from collections import defaultdict
from hashlib import sha256
from math import ceil, floor, sqrt
from pathlib import Path
from xml.etree import ElementTree

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon, box, mapping
from shapely.ops import polygonize, polylabel, unary_union
from shapely.strtree import STRtree

from backend.config import BUILDING_BUFFER_M, CEILING_M, ORIGIN_LON_LAT, STUDY_BOUNDS
from backend.core.airspace import LocalENU
from backend.core.terrain import TerrainSurface

OSM_URL = "https://api.openstreetmap.org/api/0.6/map?bbox=114.3486,30.5236,114.3624,30.5334"
DEM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/N30/N30E114.hgt.gz"
FRAME = LocalENU(*ORIGIN_LON_LAT)
AOI = box(*STUDY_BOUNDS)


def measure(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(m|ft|')?", value or "", re.IGNORECASE)
    if not match:
        return None
    amount = float(match[1]) * (0.3048 if match[2] in {"ft", "'"} else 1)
    return amount if amount > 0 else None


def tags(element):
    return {tag.attrib["k"]: tag.attrib["v"] for tag in element.findall("tag")}


def name_of(data):
    return data.get("name:zh") or data.get("name") or ""


def category(data):
    name, kind = name_of(data), data.get("building")
    if kind == "library" or "图书馆" in name:
        return "library"
    if "实验" in name or "实验室" in name:
        return "lab"
    if "食堂" in name or "餐厅" in name:
        return "canteen"
    if kind == "dormitory" or re.search(r"宿舍|\d舍", name):
        return "dorm"
    if kind in {"university", "school", "college"} or "教学楼" in name:
        return "teaching"
    return "other"


def modeled_height(data, area):
    explicit = measure(data.get("height"))
    if explicit:
        return explicit, "osm_height"
    levels = measure(data.get("building:levels"))
    if levels:
        floor_height = 3.9 if category(data) in {"lab", "teaching", "library"} else 3.2
        return round(levels * floor_height, 1), "osm_levels_estimate"
    if data.get("building") in {"shed", "garage", "garages", "roof", "kiosk"}:
        return 3.5, "footprint_estimate"
    return round(max(5, min(24, sqrt(area) * .44)), 1), "footprint_estimate"


def parts(geometry, kind):
    if geometry.geom_type == kind and not geometry.is_empty:
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from parts(part, kind)


def to_wgs84(geom):
    def inverse(points):
        coords = [FRAME.to_position((x, y, 0)) for x, y in points]
        return np.asarray([(p.lon, p.lat) for p in coords])
    return shapely.transform(geom, inverse)


def feature(geometry, **properties):
    return {"type": "Feature", "properties": properties, "geometry": mapping(geometry)}


class OsmMap:
    def __init__(self, path):
        root = ElementTree.parse(path).getroot()
        self.node_elements = {e.attrib["id"]: e for e in root.findall("node")}
        self.nodes = {key: (float(e.attrib["lon"]), float(e.attrib["lat"])) for key, e in self.node_elements.items()}
        self.ways = {e.attrib["id"]: e for e in root.findall("way")}
        self.relations = {e.attrib["id"]: e for e in root.findall("relation")}

    def line(self, identifier):
        ids = [point.attrib["ref"] for point in self.ways[identifier].findall("nd")]
        if len(ids) < 2 or any(node not in self.nodes for node in ids):
            raise ValueError(f"Missing geometry for way {identifier}")
        return LineString([self.nodes[node] for node in ids])

    def polygon(self, identifier):
        ring = self.line(identifier)
        if not ring.is_closed:
            raise ValueError(f"Open ring in polygon way {identifier}")
        return shapely.make_valid(Polygon(ring))

    def multipolygon(self, identifier):
        members = [e for e in self.relations[identifier].findall("member") if e.attrib["type"] == "way"]
        rings = {"outer": [], "inner": []}
        for member in members:
            role = member.attrib.get("role") or "outer"
            if role in rings:
                rings[role].append(self.line(member.attrib["ref"]))
        outer = list(polygonize(unary_union(rings["outer"])))
        inner = list(polygonize(unary_union(rings["inner"])))
        if not outer:
            raise ValueError(f"Unclosed multipolygon relation {identifier}")
        outline = unary_union(outer).difference(unary_union(inner))
        return shapely.make_valid(outline), [e.attrib["ref"] for e in members]

    def buildings(self, terrain):
        candidates = []
        used = set()
        for key, element in self.relations.items():
            props = tags(element)
            if props.get("building") not in {None, "no"} and props.get("type") == "multipolygon":
                geometry, members = self.multipolygon(key)
                candidates.append((f"relation/{key}", props, geometry))
                used.update(members)
        for key, element in self.ways.items():
            props = tags(element)
            if key not in used and props.get("building") not in {None, "no"}:
                candidates.append((f"way/{key}", props, self.polygon(key)))

        buildings = []
        for identifier, data, geometry in candidates:
            source_xy = shapely.transform(geometry, FRAME.project_xy)
            height, source = modeled_height(data, source_xy.area)
            for index, polygon in enumerate(parts(geometry.intersection(AOI), "Polygon"), start=1):
                xy = shapely.transform(polygon, FRAME.project_xy)
                if xy.area < .01:
                    continue
                outline = np.asarray(xy.exterior.coords)
                base = round(float(terrain.elevation(outline[:, 0], outline[:, 1]).min()), 3)
                item = feature(
                    shapely.orient_polygons(polygon),
                    id=f"osm-{identifier.replace('/', '-')}-{index:02d}",
                    name=name_of(data) or "OSM " + identifier,
                    category=category(data),
                    height=height,
                    base_alt=base,
                    height_source=source,
                    osm_ref=identifier,
                )
                if source == "footprint_estimate":
                    item["properties"]["collision_top_alt"] = max(CEILING_M, round(base + height, 3))
                buildings.append((item, xy))
        return buildings

    def roads(self):
        for key, element in self.ways.items():
            data = tags(element)
            highway = data.get("highway")
            if not highway or highway in {"proposed", "construction"}:
                continue
            line = self.line(key).intersection(AOI)
            for index, segment in enumerate(parts(line, "LineString"), start=1):
                yield feature(segment, id=f"osm-way-{key}-{index:02d}", name=name_of(data), highway=highway)


def place_kind(data):
    if data.get("leisure") in {"park", "garden", "pitch", "sports_centre", "playground"}:
        return "open_area"
    if data.get("place") == "square":
        return "open_area"
    if data.get("natural") == "water":
        return "water"
    if data.get("barrier") in {"gate", "lift_gate"} or data.get("entrance"):
        return "entrance"
    if data.get("highway") == "bus_stop":
        return "transit"
    if data.get("highway") not in {None, "construction", "proposed"}:
        return "road"
    return None


def osm_places(osm):
    named_roads = defaultdict(list)
    points = defaultdict(list)
    for key, element in osm.node_elements.items():
        data = tags(element)
        name, kind = name_of(data), place_kind(data)
        if not name or kind not in {"open_area", "entrance", "transit"}:
            continue
        point = Point(osm.nodes[key])
        if not AOI.covers(point):
            continue
        if kind == "transit" and not name.endswith("站"):
            name += "公交站"
        if any(point.distance(previous) < .00038 for previous in points[name]):
            continue
        points[name].append(point)
        yield feature(point, id=f"node/{key}", name=name, kind=kind, osm_ref=f"node/{key}")

    for key, element in osm.ways.items():
        data = tags(element)
        name, kind = name_of(data), place_kind(data)
        if not name or kind not in {"open_area", "water", "road"}:
            continue
        if kind == "road":
            named_roads[name].append((key, osm.line(key).intersection(AOI)))
        else:
            try:
                geometry = osm.polygon(key).intersection(AOI)
            except ValueError:
                continue
            for index, polygon in enumerate(parts(geometry, "Polygon"), 1):
                yield feature(polygon, id=f"way/{key}-{index}", name=name, kind=kind, osm_ref=f"way/{key}")
    for name, segments in named_roads.items():
        lines = [part for _, geometry in segments for part in parts(geometry, "LineString")]
        if lines:
            reference = next(f"way/{key}" for key, geometry in segments if list(parts(geometry, "LineString")))
            yield feature(unary_union(lines), id=reference, name=name, kind="road", osm_ref=reference)
    for key, element in osm.relations.items():
        data = tags(element)
        name, kind = name_of(data), place_kind(data)
        if data.get("type") != "multipolygon" or not name or kind not in {"open_area", "water"}:
            continue
        try:
            geometry, _ = osm.multipolygon(key)
        except (KeyError, ValueError):
            continue
        for index, polygon in enumerate(parts(geometry.intersection(AOI), "Polygon"), 1):
            yield feature(polygon, id=f"relation/{key}-{index}", name=name, kind=kind, osm_ref=f"relation/{key}")


def heightmap(hgt):
    samples = np.frombuffer(gzip.decompress(hgt), dtype=">i2")
    n = int(sqrt(len(samples)))
    if n not in (1201, 3601) or n * n != len(samples):
        raise ValueError("Expected a Skadi HGT tile at N30E114")
    step = n - 1
    west, south, east, north = AOI.bounds
    x0, x1 = floor((west - 114) * step) - 2, ceil((east - 114) * step) + 2
    y0, y1 = floor((31 - north) * step) - 2, ceil((31 - south) * step) + 2
    crop = samples.reshape(n, n)[y0:y1 + 1, x0:x1 + 1].astype(int)
    if (crop == -32768).any():
        raise ValueError("Terrain has voids inside the scene")
    datum = int(crop.min())
    return {
        "crs_horizontal": "EPSG:4326",
        "source_vertical_datum": "EGM96",
        "reference_elevation_m": datum,
        "bounds": [114 + x0 / step, 31 - y1 / step, 114 + x1 / step, 31 - y0 / step],
        "elevations_m": (crop - datum).tolist(),
        "source": DEM_URL,
        "sha256_source": sha256(hgt).hexdigest(),
    }


def vertiports(osm, buildings, terrain, restricted):
    index = STRtree([xy for _, xy in buildings])
    restrictions = unary_union(restricted)

    def point_of(geometry):
        return shapely.transform(geometry, FRAME.project_xy)

    def safe_point(anchor, *, within=None, occupied=None):
        x, y = anchor.coords[0]
        dx, dy = np.meshgrid(np.arange(-85, 86, 2), np.arange(-85, 86, 2))
        candidates = shapely.points(np.column_stack((x + dx.ravel(), y + dy.ravel())))
        nearest = index.nearest(candidates)
        clear = shapely.distance(candidates, index.geometries[nearest])
        valid = (clear >= BUILDING_BUFFER_M + 7) & ~shapely.intersects(restrictions, candidates)
        valid &= shapely.covers(point_of(AOI), candidates)
        if within is not None:
            valid &= shapely.covers(within, candidates)
        if occupied is not None:
            valid &= ~shapely.intersects(occupied, candidates)
            samples = np.array([terrain.elevation(shapely.get_x(candidates) + a, shapely.get_y(candidates) + b)
                                for a, b in ((5, 0), (-5, 0), (0, 5), (0, -5))])
            valid &= np.ptp(samples, axis=0) <= 1.5
        if not valid.any():
            raise ValueError("No safe candidate within the landmark surroundings")
        ranked = candidates[valid]
        offsets = shapely.distance(ranked, anchor)
        return ranked[int(np.argmin(offsets))]

    def ground(key, name, kind, reference, anchor, **limits):
        chosen = safe_point(anchor, **limits)
        alt = ceil((float(terrain.elevation(chosen.x, chosen.y)) + .04) * 100) / 100
        return feature(to_wgs84(chosen), id=key, name=name, type=kind, altitude=alt,
                       osm_ref=reference, placement="simulated_ground_pad")

    lab, roof_outline = next((item, poly) for item, poly in buildings if item["properties"]["osm_ref"] == "way/306407524")
    lab_data = lab["properties"]
    roof = polylabel(roof_outline, tolerance=.15)
    library = point_of(osm.polygon("104108001"))
    west_of_library = box(library.bounds[0] - 85, library.bounds[1] - 25,
                          library.bounds[0] - 5, library.bounds[3] + 25)
    library_anchor = Point(library.bounds[0], library.centroid.y)
    park, _ = osm.multipolygon("19149478")
    west_field, east_field = point_of(park), point_of(osm.polygon("1382683958"))
    east_half = east_field.intersection(box(east_field.centroid.x, east_field.bounds[1],
                                            east_field.bounds[2], east_field.bounds[3]))
    pads = [
        ground("VP01", "信息学部一食堂起降坪", "logistics", "node/13513425337",
               point_of(Point(osm.nodes["13513425337"]))),
        ground("VP02", "菜鸟驿站物流站", "logistics", "node/13266231640",
               point_of(Point(osm.nodes["13266231640"]))),
        feature(to_wgs84(roof), id="VP03", name="测国重楼顶停机坪", type="inspection",
                altitude=round(lab_data["base_alt"] + lab_data["height"], 3), osm_ref="way/306407524",
                placement="simulated_roof_pad", host_building_id=lab_data["id"],
                altitude_source=lab_data["height_source"]),
        ground("VP04", "图书馆西侧巡检点", "inspection", "way/104108001",
               library_anchor, within=west_of_library),
        ground("VP05", "星湖运动场应急备降坪", "emergency", "relation/19149478",
               west_field.representative_point(), within=west_field),
        ground("VP06", "信息学部操场东侧应急备降坪", "emergency", "way/1382683958",
               east_half.representative_point(), within=east_half),
    ]
    roadbed = unary_union([
        point_of(shapely.geometry.shape(road["geometry"])).buffer(6)
        for road in osm.roads() if road["properties"]["highway"] not in {"footway", "path", "steps", "cycleway"}
    ])
    keep_clear = roadbed.union(restrictions.buffer(6))
    landmarks = (
        ("VP07", "星空广场仿真坪", "logistics", "way", "1155391715"),
        ("VP08", "一教公交站附近仿真坪", "logistics", "node", "9558228576"),
        ("VP09", "信息学部操场西侧仿真坪", "inspection", "way", "1382683958"),
        ("VP10", "星湖运动场北侧仿真坪", "inspection", "relation", "19149478"),
        ("VP11", "露天篮球场仿真坪", "inspection", "way", "1480437168"),
        ("VP12", "竹园网球场仿真坪", "logistics", "way", "1382564211"),
        ("VP13", "友谊广场仿真坪", "logistics", "relation", "19073122"),
        ("VP14", "珞喻门西段仿真坪", "logistics", "node", "13518537639"),
        ("VP15", "珞喻二门附近仿真坪", "logistics", "node", "3148705291"),
        ("VP16", "玉兰亭附近仿真坪", "inspection", "node", "12862472452"),
    )
    for key, name, kind, element, reference in landmarks:
        if element == "node":
            anchor, within = point_of(Point(osm.nodes[reference])), None
        else:
            polygon = osm.polygon(reference) if element == "way" else osm.multipolygon(reference)[0]
            within = point_of(polygon)
            if key == "VP09":
                within = within.intersection(box(within.bounds[0], within.bounds[1], within.centroid.x, within.bounds[3]))
            if key == "VP10":
                within = within.intersection(box(within.bounds[0], within.centroid.y, within.bounds[2], within.bounds[3]))
            if within.is_empty:
                continue
            anchor = within.representative_point()
        near_pad = unary_union([point_of(shapely.geometry.shape(pad["geometry"])).buffer(18) for pad in pads])
        try:
            pad = ground(key, name, kind, f"{element}/{reference}", anchor,
                         within=within, occupied=keep_clear.union(near_pad))
        except ValueError:
            print(f"Skipped {key}: no safe candidate")
            continue
        pads.append(pad)
    return pads


def nofly_zones(osm):
    library = shapely.transform(osm.polygon("104108001"), FRAME.project_xy).buffer(5)
    lake = shapely.transform(osm.polygon("295111844"), FRAME.project_xy)
    school = unary_union([
        shapely.transform(osm.polygon(key), FRAME.project_xy)
        for key in ("127025727", "127025729", "127025731")
    ]).convex_hull
    zones = [
        ("NFZ01", "图书馆静谧区", 120, library, "way/104108001"),
        ("NFZ02", "星湖水面仿真禁飞区", 48, lake, "way/295111844"),
        ("NFZ03", "密集教学区", 45, school, "way/127025727;way/127025729;way/127025731"),
    ]
    return [
        feature(shapely.orient_polygons(to_wgs84(poly)), id=key, name=name, min_alt=0.0,
                max_alt=float(limit), reason="依实景地物设置的仿真围栏", osm_ref=source)
        for key, name, limit, poly, source in zones
    ], [zone[3] for zone in zones]


def collection(items, **meta):
    entries = ",\n".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in items)
    header = json.dumps({"type": "FeatureCollection", **meta}, ensure_ascii=False)
    return header[:-1] + ', "features": [\n' + entries + "\n]}\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osm", type=Path, required=True)
    parser.add_argument("--dem", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    osm = OsmMap(args.osm)
    terrain_asset = heightmap(args.dem.read_bytes())
    terrain = TerrainSurface(terrain_asset, FRAME)
    buildings = osm.buildings(terrain)
    zones, restricted = nofly_zones(osm)
    pads = vertiports(osm, buildings, terrain, restricted)
    source = {"source": OSM_URL, "attribution": "© OpenStreetMap contributors (ODbL 1.0)",
              "sha256_source": sha256(args.osm.read_bytes()).hexdigest()}
    args.output.mkdir(parents=True, exist_ok=True)
    products = {
        "whu_buildings.geojson": collection((item for item, _ in buildings), **source),
        "vertiports.geojson": collection(pads, **source),
        "nofly_zones.geojson": collection(zones, **source),
        "whu_roads.geojson": collection(osm.roads(), **source),
        "whu_places.geojson": collection(osm_places(osm), **source),
        "whu_terrain.json": json.dumps(terrain_asset, separators=(",", ":")) + "\n",
    }
    for filename, contents in products.items():
        (args.output / filename).write_text(contents, encoding="utf-8")
    print(f"Wrote {len(buildings)} buildings, {len(pads)} pads and {len(zones)} fences")


if __name__ == "__main__":
    main()
