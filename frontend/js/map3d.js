const Cesium = globalThis.Cesium;

const color = (hex, opacity = 1) => Cesium.Color.fromCssColorString(hex).withAlpha(opacity);
const degrees = ([lon, lat], alt = 0) => Cesium.Cartesian3.fromDegrees(lon, lat, alt);

function campusTerrain(asset) {
  if (!asset) return new Cesium.EllipsoidTerrainProvider();
  const [west, south, east, north] = asset.bounds;
  const grid = asset.elevations_m;
  const rows = grid.length;
  const cols = grid[0].length;
  const size = 33;
  const feather = .0008;
  const tilingScheme = new Cesium.GeographicTilingScheme();
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  return new Cesium.CustomHeightmapTerrainProvider({
    width: size,
    height: size,
    tilingScheme,
    callback(x, y, level) {
      const rectangle = tilingScheme.tileXYToRectangle(x, y, level);
      const left = Cesium.Math.toDegrees(rectangle.west);
      const right = Cesium.Math.toDegrees(rectangle.east);
      const top = Cesium.Math.toDegrees(rectangle.north);
      const bottom = Cesium.Math.toDegrees(rectangle.south);
      const buffer = new Float32Array(size * size);
      if (right < west - feather || left > east + feather || top < south - feather || bottom > north + feather) return buffer;
      for (let row = 0; row < size; row += 1) {
        const lat = top - (top - bottom) * row / (size - 1);
        const gy = clamp((north - lat) * (rows - 1) / (north - south), 0, rows - 1);
        const j = Math.min(rows - 2, Math.floor(gy));
        for (let col = 0; col < size; col += 1) {
          const lon = left + (right - left) * col / (size - 1);
          const gx = clamp((lon - west) * (cols - 1) / (east - west), 0, cols - 1);
          const i = Math.min(cols - 2, Math.floor(gx));
          const u = gx - i;
          const v = gy - j;
          const blend = clamp(1 - Math.max(west - lon, lon - east, south - lat, lat - north) / feather, 0, 1);
          buffer[row * size + col] = blend * (
            (1 - v) * ((1 - u) * grid[j][i] + u * grid[j][i + 1])
            + v * ((1 - u) * grid[j + 1][i] + u * grid[j + 1][i + 1])
          );
        }
      }
      return buffer;
    },
  });
}

function hierarchy(rings) {
  const [outer, ...holes] = rings;
  const ring = (coordinates) => coordinates.slice(0, -1).map((point) => degrees(point));
  return new Cesium.PolygonHierarchy(
    ring(outer), holes.map((hole) => new Cesium.PolygonHierarchy(ring(hole))),
  );
}

function volume(entitySet, feature, { bottom, top, fill, outline, hoverText }) {
  const rings = feature.geometry.coordinates;
  entitySet.add({
    name: feature.properties.name,
    properties: { hoverText },
    polygon: {
      hierarchy: hierarchy(rings),
      height: bottom,
      extrudedHeight: top,
      material: fill,
    },
  });
  rings.forEach((ring) => entitySet.add({
    properties: { hoverText },
    polyline: {
      positions: ring.map((point) => degrees(point, top + 0.2)),
      material: outline,
      width: 1.3,
      arcType: Cesium.ArcType.NONE,
    },
  }));
}

function ringDistance(ring, x, y) {
  let inside = false;
  let distance = Infinity;
  for (let i = 1; i < ring.length; i += 1) {
    const [ax, ay] = ring[i - 1];
    const [bx, by] = ring[i];
    const dx = bx - ax;
    const dy = by - ay;
    const length2 = dx * dx + dy * dy;
    const t = length2 ? Math.min(1, Math.max(0, ((x - ax) * dx + (y - ay) * dy) / length2)) : 0;
    distance = Math.min(distance, Math.hypot(x - ax - dx * t, y - ay - dy * t));
    if ((ay > y) !== (by > y) && x < ax + (y - ay) * dx / dy) inside = !inside;
  }
  return { inside, distance };
}

function footprintDistance(rings, x, y) {
  const [exterior, ...holes] = rings;
  const edge = ringDistance(exterior, x, y);
  if (!edge.inside) return edge.distance;
  for (const ring of holes) {
    const hole = ringDistance(ring, x, y);
    if (hole.inside) return hole.distance;
  }
  return 0;
}

export class CampusMap {
  constructor(container, bounds, terrain) {
    if (!Cesium) throw new Error("Cesium 资源加载失败，请检查 CDN 连接。");
    this.bounds = bounds;
    this.terrain = terrain;
    const [west, south, east, north] = bounds;
    const localOrigin = degrees([(west + east) / 2, (south + north) / 2]);
    this.worldToLocal = Cesium.Matrix4.inverseTransformation(
      Cesium.Transforms.eastNorthUpToFixedFrame(localOrigin), new Cesium.Matrix4(),
    );
    this.viewer = new Cesium.Viewer(container, {
      baseLayer: false,
      terrainProvider: campusTerrain(terrain),
      animation: false,
      timeline: false,
      baseLayerPicker: false,
      geocoder: false,
      homeButton: false,
      sceneModePicker: false,
      navigationHelpButton: false,
      fullscreenButton: false,
      infoBox: false,
      selectionIndicator: false,
      requestRenderMode: true,
      shouldAnimate: false,
      automaticallyTrackDataSourceClocks: false,
    });
    const tiles = new Cesium.UrlTemplateImageryProvider({
      url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      maximumLevel: 19,
      credit: new Cesium.Credit(
        '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap contributors</a>',
        true,
      ),
    });
    // The keyless prototype uses OSM tiles with a Dark Matter-style palette.
    this.viewer.imageryLayers.add(new Cesium.ImageryLayer(tiles, {
      brightness: 0.38,
      contrast: 1.11,
      saturation: 0.24,
      gamma: 0.82,
    }));
    this.viewer.scene.globe.baseColor = color("#222d3b");
    this.viewer.scene.globe.enableLighting = false;
    this.viewer.scene.globe.depthTestAgainstTerrain = Boolean(terrain);
    if (terrain) {
      this.viewer.scene.globe.maximumScreenSpaceError = 1.3;
      this.viewer.creditDisplay.addStaticCredit(new Cesium.Credit("Elevation: NASA SRTM / Mapzen", true));
    }
    this.viewer.scene.fog.enabled = false;
    this.viewer.scene.backgroundColor = color("#182b3c");
    this.viewer.scene.skyAtmosphere.show = false;
    this.viewer.scene.skyBox.show = false;
    this.viewer.scene.sun.show = false;
    this.viewer.scene.moon.show = false;
    this.layers = new Map();
    this.staticVolumes = [];
    this.dynamicVolumes = [];
    this.flightEntity = this.viewer.entities.add({
      name: "VOXAERO-01",
      show: false,
      orientation: new Cesium.ConstantProperty(),
      point: { pixelSize: 9, color: color("#96ffe1"), outlineColor: color("#124e48"), outlineWidth: 2 },
      model: {
        uri: "/assets/voxaero-01.glb",
        minimumPixelSize: 56,
        maximumScale: 2.5,
        runAnimations: true,
      },
      label: {
        text: "VOXAERO-01", font: "600 12px monospace", fillColor: color("#baffec"),
        showBackground: true, backgroundColor: color("#10242b", .92),
        pixelOffset: new Cesium.Cartesian2(0, -37),
      },
    });
    this.guides = this.viewer.entities.add({
      show: false,
      label: {
        text: "AR 降落靶标", font: "600 12px system-ui", fillColor: color("#abffb0"),
        showBackground: true, backgroundColor: color("#122e23", .9),
        pixelOffset: new Cesium.Cartesian2(0, 16), disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    });
    this.guideLines = Array.from({ length: 6 }, () => this.viewer.entities.add({
      show: false,
      polyline: { width: 2.7, material: color("#87ff9c", .95), arcType: Cesium.ArcType.NONE },
    }));
    this.tooltip = document.getElementById("map-tooltip");
    this.viewer.screenSpaceEventHandler.setInputAction(({ endPosition }) => {
      const picked = this.viewer.scene.pick(endPosition);
      const label = picked?.id?.properties?.hoverText?.getValue();
      this.tooltip.hidden = !label;
      if (label) {
        this.tooltip.textContent = label;
        this.tooltip.style.left = Math.min(endPosition.x + 12, this.viewer.canvas.clientWidth - 165) + "px";
        this.tooltip.style.top = Math.max(endPosition.y - 33, 5) + "px";
      }
    }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);
    this.viewer.canvas.addEventListener("pointerleave", () => { this.tooltip.hidden = true; });
  }

  async load(data) {
    for (const key of ["buildings", "vertiports", "nofly_zones", "airspace", "route", "hazards", "roads"]) {
      this.layers.set(key, new Cesium.CustomDataSource(key));
    }
    const buildings = this.layers.get("buildings").entities;
    for (const feature of data.buildings.features) {
      const { height, base_alt = 0, height_source, collision_top_alt } = feature.properties;
      const top = base_alt + height;
      const provenance = { osm_height: "OSM 标注", osm_levels_estimate: "楼层估算", footprint_estimate: "缺高估算" }[height_source];
      volume(buildings, feature, {
        bottom: base_alt, top,
        fill: color("#bdcee0", 0.82), outline: color("#e8f4ff", 0.87),
        hoverText: feature.properties.name + " · " + height + " m" + (provenance ? " · " + provenance : ""),
      });
      this.staticVolumes.push(this.asVolume(feature, base_alt, collision_top_alt ?? top));
    }
    this.roadFeatures = data.roads?.features ?? [];
    this.layers.get("roads").show = false;
    const pads = this.layers.get("vertiports").entities;
    for (const feature of data.vertiports.features) {
      const { id, name, altitude, type } = feature.properties;
      const tint = color(type === "emergency" ? "#fad383" : "#86f2d8");
      pads.add({
        name,
        position: degrees(feature.geometry.coordinates, altitude + 1),
        ellipse: {
          height: altitude + 0.65,
          semiMajorAxis: 11,
          semiMinorAxis: 11,
          material: tint.withAlpha(0.36),
          outline: true,
          outlineColor: tint,
        },
        point: { pixelSize: 7, color: tint, outlineColor: color("#172838"), outlineWidth: 1.5 },
        label: {
          text: id,
          font: "600 13px system-ui",
          fillColor: tint,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          outlineColor: color("#101724"),
          outlineWidth: 3,
          pixelOffset: new Cesium.Cartesian2(0, -17),
          showBackground: true,
          backgroundColor: color("#111a26", 0.8),
          backgroundPadding: new Cesium.Cartesian2(5, 3),
        },
      });
    }
    const zones = this.layers.get("nofly_zones").entities;
    for (const feature of data.nofly_zones.features) {
      const { min_alt, max_alt } = feature.properties;
      volume(zones, feature, {
        bottom: min_alt, top: max_alt,
        fill: color("#f14755", 0.26), outline: color("#ff8790", 0.92),
      });
      this.staticVolumes.push(this.asVolume(feature, min_alt, max_alt));
    }
    this.drawGrid(this.layers.get("airspace").entities, data.airspace_layers);
    await Promise.all(Array.from(this.layers.values(), (layer) => this.viewer.dataSources.add(layer)));
    this.resetView();
  }

  drawGrid(entities, layers) {
    const [west, south, east, north] = this.bounds;
    const boundaries = layers.filter((layer) => layer.id !== "ground").map((layer) => layer.min_alt);
    for (const [index, alt] of boundaries.entries()) {
      const tint = index === 0 ? "#63cfd9" : "#9293f3";
      const addLine = (a, b, edge = false) => entities.add({
        polyline: {
          positions: [degrees(a, alt), degrees(b, alt)],
          width: edge ? 1.5 : 1,
          material: color(tint, edge ? 0.55 : 0.23),
          arcType: Cesium.ArcType.NONE,
        },
      });
      const divisions = 10;
      for (let i = 0; i <= divisions; i += 1) {
        const lon = west + (east - west) * i / divisions;
        const lat = south + (north - south) * i / divisions;
        const edge = i === 0 || i === divisions;
        addLine([lon, south], [lon, north], edge);
        addLine([west, lat], [east, lat], edge);
      }
      entities.add({
        position: degrees([west + (east - west) * 0.22, south + (north - south) * 0.09], alt),
        label: {
          text: alt + " m",
          font: "600 12px system-ui",
          fillColor: color(tint),
          showBackground: true,
          backgroundColor: color("#111a26", 0.9),
          pixelOffset: new Cesium.Cartesian2(8, index === 0 ? 6 : -6),
          horizontalOrigin: Cesium.HorizontalOrigin.LEFT,
        },
      });
    }
  }

  showLayer(key, visible) {
    if (key === "roads" && visible) {
      const entities = this.layers.get("roads").entities;
      if (!entities.values.length) {
        for (const feature of this.roadFeatures) {
          entities.add({ polyline: {
            positions: feature.geometry.coordinates.map((point) => degrees(point)),
            width: 2,
            material: color("#62dacf", .78),
            clampToGround: true,
          } });
        }
      }
    }
    this.layers.get(key).show = visible;
    this.viewer.scene.requestRender();
  }

  resetView() {
    const [west, south, east, north] = this.bounds;
    const target = degrees([(west + east) / 2, (south + north) / 2], this.terrain ? 35 : 20);
    const extent = Cesium.Cartesian3.distance(degrees([west, south]), degrees([east, north]));
    const aspect = this.viewer.canvas.clientWidth / this.viewer.canvas.clientHeight;
    const range = extent * Math.max(1.06, 1.1 / Math.max(.55, aspect));
    this.viewer.camera.lookAt(target, new Cesium.HeadingPitchRange(-0.30, -0.80, range));
    this.viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
    this.viewer.scene.requestRender();
  }

  drawRoute(waypoints, rerouted) {
    const entities = this.layers.get("route").entities;
    entities.removeAll();
    const tints = { takeoff: "#6be1ff", cruise: "#b2ffa2", landing: "#d7a5ff", transition: "#ffcd85" };
    for (let i = 1; i < waypoints.length; i += 1) {
      const phase = waypoints[i].phase;
      const hex = rerouted && phase === "cruise" ? "#ffca78" : tints[phase];
      entities.add({
        polyline: {
          positions: waypoints.slice(i - 1, i + 1).map(({ lon, lat, alt }) => degrees([lon, lat], alt)),
          width: 5,
          material: new Cesium.PolylineGlowMaterialProperty({ color: color(hex), glowPower: .2 }),
          arcType: Cesium.ArcType.NONE,
        },
      });
    }
    this.viewer.scene.requestRender();
  }

  setHazards(features) {
    const entities = this.layers.get("hazards").entities;
    entities.removeAll();
    this.dynamicVolumes = features.map((feature) => {
      const { min_alt, max_alt } = feature.properties;
      volume(entities, feature, {
        bottom: min_alt, top: max_alt, fill: color("#f56735", .32), outline: color("#ffb25d"),
        hoverText: "临时禁飞区 · " + min_alt + "–" + max_alt + " m",
      });
      return this.asVolume(feature, min_alt, max_alt);
    });
    this.viewer.scene.requestRender();
  }

  project(lon, lat) {
    const point = Cesium.Matrix4.multiplyByPoint(this.worldToLocal, degrees([lon, lat]), new Cesium.Cartesian3());
    return [point.x, point.y];
  }

  asVolume(feature, bottom, top) {
    return {
      rings: feature.geometry.coordinates.map((ring) => ring.map(([lon, lat]) => this.project(lon, lat))),
      id: feature.properties.id, bottom, top,
    };
  }

  clearance({ lon, lat, alt }, avoidedZones) {
    const [x, y] = this.project(lon, lat);
    let nearest = Infinity;
    for (const volume of [...this.staticVolumes, ...this.dynamicVolumes]) {
      const extended = avoidedZones.has(volume.id);
      const top = extended ? Math.max(volume.top, 100) : volume.top;
      const height = Math.max(0, (extended ? 0 : volume.bottom) - alt, alt - top);
      if (height < nearest) nearest = Math.min(nearest, Math.hypot(height, footprintDistance(volume.rings, x, y)));
    }
    return Number.isFinite(nearest) ? nearest : null;
  }

  showGuides(gates, target, targetId) {
    const lines = [...gates];
    if (target) {
      const anchor = degrees([target.lon, target.lat], target.alt + .45);
      const localFrame = Cesium.Transforms.eastNorthUpToFixedFrame(anchor);
      const offset = (east, north, up = 0) => Cesium.Matrix4.multiplyByPoint(
        localFrame, new Cesium.Cartesian3(east, north, up), new Cesium.Cartesian3(),
      );
      lines.push([offset(-8, 0), offset(8, 0)], [offset(0, -8), offset(0, 8)]);
      this.guides.position = offset(0, 0, 2);
      this.guides.label.text = "AR 降落靶标 · " + targetId;
    }
    this.guides.show = Boolean(target);
    this.guideLines.forEach((line, index) => {
      line.show = Boolean(lines[index]);
      if (line.show) line.polyline.positions = lines[index];
    });
    this.viewer.scene.requestRender();
  }
}
