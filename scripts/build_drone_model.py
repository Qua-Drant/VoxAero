"""Build the original, animated VOXAERO-01 glTF asset for the Cesium preview."""

import json
import struct
from collections import defaultdict
from math import cos, pi, sin, tau
from pathlib import Path

import numpy as np

OUTPUT = Path(__file__).resolve().parents[1] / "frontend/assets/voxaero-01.glb"


class Geometry:
    def __init__(self):
        self.positions = defaultdict(list)
        self.normals = defaultdict(list)

    def loft(self, material, rings):
        """Connect two or more rings with flat-shaded, outward-facing triangles."""
        rings = np.asarray(rings, dtype=float)
        center = rings.mean(axis=(0, 1))
        sides = rings.shape[1]
        faces = [rings[0], rings[-1]]
        for bottom, top in zip(rings[:-1], rings[1:], strict=True):
            for i in range(sides):
                j = (i + 1) % sides
                faces.append(np.array([bottom[i], bottom[j], top[j], top[i]]))
        for face in faces:
            normal = np.cross(face[1] - face[0], face[2] - face[0])
            normal /= np.linalg.norm(normal)
            if np.dot(normal, face.mean(axis=0) - center) < 0:
                face = face[::-1]
                normal = -normal
            for i in range(1, len(face) - 1):
                self.positions[material].extend((face[0], face[i], face[i + 1]))
                self.normals[material].extend((normal, normal, normal))

    def block(self, material, a, b):
        x0, y0, z0 = a
        x1, y1, z1 = b
        return self.loft(material, [
            [(x0, y, z0), (x1, y, z0), (x1, y, z1), (x0, y, z1)] for y in (y0, y1)
        ])

    def tube(self, material, a, b, start_radius, end_radius=None, *, sides=12):
        start, end = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
        axis = end - start
        axis /= np.linalg.norm(axis)
        tangent = np.cross(axis, np.eye(3)[int(np.argmin(np.abs(axis)))])
        tangent /= np.linalg.norm(tangent)
        bitangent = np.cross(axis, tangent)
        outline = [
            tangent * cos(tau * i / sides) + bitangent * sin(tau * i / sides) for i in range(sides)
        ]
        radii = (start_radius, start_radius if end_radius is None else end_radius)
        self.loft(material, [[point + radius * n for n in outline] for point, radius in zip((start, end), radii)])


def material(name, rgb, metallic=0, roughness=.48, emission=None):
    result = {
        "name": name,
        "pbrMetallicRoughness": {
            "baseColorFactor": [*rgb, 1], "metallicFactor": metallic, "roughnessFactor": roughness,
        },
    }
    if emission:
        result["emissiveFactor"] = emission
    return result


def aircraft():
    # glTF is Y-up and the nose points toward +Z. Skids touch y=0 at rest.
    hull = Geometry()
    outline = [(-.11, .30), (.11, .30), (.19, .21), (.205, -.18),
               (.14, -.32), (-.14, -.32), (-.205, -.18), (-.19, .21)]

    def body(name, profiles, shift=0):
        hull.loft(name, [[(x * s, y, z * s + shift) for x, z in outline] for y, s in profiles])

    body("Carapace", [(.25, .73), (.29, .93), (.43, 1), (.48, .84)])
    body("Top shell", [(.451, .84), (.48, .86), (.506, .70)], shift=-.035)
    hull.block("Metal trim", (-.11, .498, -.21), (.11, .524, -.04))
    hull.block("Mint", (-.011, .525, -.179), (.011, .529, -.070))
    hull.block("Carbon", (-.123, .330, .276), (.123, .397, .310))
    for side in (-1, 1):
        x = side * .222
        hull.tube("Carbon", (x, .015, -.315), (x, .015, .325), .015, sides=10)
        for z in (-.23, .223):
            hull.tube("Carbon", (x, .022, z), (side * .142, .28, z * .80), .014, .021)
            hull.tube("Metal trim", (side * .169, .24, z * .84), (side * .154, .28, z * .80), .024)
        for i in range(4):
            z = -.195 + i * .047
            hull.tube("Carbon", (side * .179, .388, z), (side * .201, .388, z), .012, sides=6)
        hull.tube("Optics", (side * .094, .365, .279), (side * .094, .365, .316), .016, sides=12)

    hull.tube("Metal trim", (0, .249, .17), (0, .207, .192), .045)
    hull.tube("Carbon", (-.057, .173, .197), (.057, .173, .197), .052)
    hull.tube("Metal trim", (0, .17, .221), (0, .17, .270), .044)
    hull.tube("Optics", (0, .17, .265), (0, .17, .277), .033)

    pivots = []
    for side in (-1, 1):
        for direction in (1, -1):
            outer = (side * .43, .39, direction * .435)
            hull.tube("Carbon", (side * .15, .38, direction * .15), outer, .048, .028, sides=8)
            collar = (side * .315, .39, direction * .310)
            thin = (side * .34, .39, direction * .336)
            hull.tube("Mint" if direction == 1 else "Metal trim", collar, thin, .038, .035, sides=8)
            x, _, z = outer
            hull.tube("Carbon", (x, .359, z), (x, .449, z), .058, .068)
            hull.tube("Metal trim", (x, .449, z), (x, .486, z), .067, .058)
            hull.tube("Carbon", (x, .486, z), (x, .501, z), .057, .036)
            nav = "Navigation red" if side < 0 else "Navigation green"
            hull.tube(nav, (x, .348, z), (x, .366, z), .032, .040, sides=10)
            pivots.append([x, .512, z])

    rotor = Geometry()
    rotor.tube("Carbon", (0, -.014, 0), (0, .012, 0), .031)
    for sign in (-1, 1):
        blade = [(.018, -.010), (.105, -.026), (.192, -.006), (.223, .014),
                 (.204, .030), (.143, .034), (.056, .021), (.018, .010)]
        rotor.loft("Propeller", [[(sign * x, y, sign * z) for x, z in blade] for y in (-.004, .003)])
        rotor.loft("Tip paint", [[(sign * x, y, sign * z) for x, z in [
            (.201, .001), (.218, .011), (.217, .017), (.201, .029), (.195, .021),
        ]] for y in (.0034, .0041)])
    return hull, rotor, pivots


def build():
    materials = [
        material("Carapace", (.19, .24, .31), .18, .46, emission=(.018, .024, .032)),
        material("Top shell", (.42, .49, .56), .18, .41, emission=(.045, .055, .068)),
        material("Metal trim", (.37, .43, .49), .40, .34, emission=(.025, .033, .041)),
        material("Carbon", (.043, .06, .077), .23, .53),
        material("Mint", (.07, .54, .39), .15, .42, emission=(.02, .12, .082)),
        material("Optics", (.025, .08, .13), .44, .25),
        material("Navigation red", (.6, .018, .013), emission=(.7, .025, .02)),
        material("Navigation green", (.028, .53, .22), emission=(.04, .66, .31)),
        material("Propeller", (.074, .09, .105), .12, .43),
        material("Tip paint", (.45, .58, .67), .08, .45),
    ]
    material_ids = {item["name"]: i for i, item in enumerate(materials)}
    payload = bytearray()
    views, accessors = [], []

    def accessor(values, kind, *, geometry=False, extent=False):
        data = np.asarray(values, dtype="<f4")
        view = {"buffer": 0, "byteOffset": len(payload), "byteLength": data.nbytes}
        if geometry:
            view["target"] = 34962
        views.append(view)
        payload.extend(data.tobytes())
        entry = {"bufferView": len(views) - 1, "componentType": 5126, "count": len(data), "type": kind}
        if extent:
            entry.update(min=data.reshape(len(data), -1).min(axis=0).tolist(),
                         max=data.reshape(len(data), -1).max(axis=0).tolist())
        accessors.append(entry)
        return len(accessors) - 1

    hull, rotor, pivots = aircraft()
    meshes = []
    for name, mesh in (("Airframe and gimbal", hull), ("Two-blade propeller", rotor)):
        primitives = [
            {"attributes": {
                "POSITION": accessor(positions, "VEC3", geometry=True, extent=True),
                "NORMAL": accessor(mesh.normals[key], "VEC3", geometry=True),
            }, "material": material_ids[key]}
            for key, positions in mesh.positions.items()
        ]
        meshes.append({"name": name, "primitives": primitives})
    nodes = [{"name": "VOXAERO-01", "mesh": 0, "children": [1, 2, 3, 4]}]
    nodes.extend({"name": f"Rotor {i}", "mesh": 1, "translation": pivot} for i, pivot in enumerate(pivots, 1))
    clock = accessor([0, .055, .11, .165, .22], "SCALAR", extent=True)
    rotations = [
        accessor([[0, sin(direction * pi * i / 4), 0, cos(pi * i / 4)] for i in range(5)], "VEC4")
        for direction in (-1, 1)
    ]
    animation = {
        "name": "Counter-rotating propellers",
        "samplers": [{"input": clock, "output": values, "interpolation": "LINEAR"} for values in rotations],
        "channels": [
            {"sampler": direction, "target": {"node": node, "path": "rotation"}}
            for node, direction in enumerate((0, 1, 1, 0), 1)
        ],
    }
    manifest = {
        "asset": {"version": "2.0", "generator": "VoxAero original aircraft generator"},
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": nodes, "meshes": meshes, "materials": materials, "animations": [animation],
        "accessors": accessors, "bufferViews": views, "buffers": [{"byteLength": len(payload)}],
    }
    document = json.dumps(manifest, separators=(",", ":")).encode()
    document += b" " * (-len(document) % 4)
    header = struct.pack("<4sII", b"glTF", 2, 28 + len(document) + len(payload))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(
        header + struct.pack("<I4s", len(document), b"JSON") + document
        + struct.pack("<I4s", len(payload), b"BIN\0") + payload
    )
    print(f"{OUTPUT.name}: {OUTPUT.stat().st_size} bytes")


if __name__ == "__main__":
    build()
