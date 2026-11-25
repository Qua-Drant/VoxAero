const Cesium = globalThis.Cesium;
const typography = getComputedStyle(document.documentElement);
const labelFont = typography.getPropertyValue("--font-sans").trim() || "system-ui, sans-serif";
const readoutFont = typography.getPropertyValue("--font-mono").trim() || "monospace";

const color = (hex, opacity = 1) => Cesium.Color.fromCssColorString(hex).withAlpha(opacity);
const degrees = ([lon, lat], alt = 0) => Cesium.Cartesian3.fromDegrees(lon, lat, alt);

function terrainSampler(asset) {
  if (!asset) return () => 0;
  const [west, south, east, north] = asset.bounds;
  const { elevations_m: grid } = asset;
  const rows = grid.length;
  const columns = grid[0].length;
  const feather = .0008;
  return (lon, lat) => {
    const gx = Cesium.Math.clamp((lon - west) * (columns - 1) / (east - west), 0, columns - 1);
    const gy = Cesium.Math.clamp((north - lat) * (rows - 1) / (north - south), 0, rows - 1);
    const i = Math.min(columns - 2, Math.floor(gx));
    const j = Math.min(rows - 2, Math.floor(gy));
    const upper = Cesium.Math.lerp(grid[j][i], grid[j][i + 1], gx - i);
    const lower = Cesium.Math.lerp(grid[j + 1][i], grid[j + 1][i + 1], gx - i);
    const blend = Cesium.Math.clamp(1 - Math.max(west - lon, lon - east, south - lat, lat - north) / feather, 0, 1);
    return blend * Cesium.Math.lerp(upper, lower, gy - j);
  };
}

function campusTerrain(asset, elevation) {
  if (!asset) return new Cesium.EllipsoidTerrainProvider();
  const [west, south, east, north] = asset.bounds;
  const size = 33;
  const tilingScheme = new Cesium.GeographicTilingScheme();
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
      if (right < west - .0008 || left > east + .0008 || top < south - .0008 || bottom > north + .0008) return buffer;
      for (let row = 0; row < size; row += 1) {
        const lat = top - (top - bottom) * row / (size - 1);
        for (let col = 0; col < size; col += 1) {
          const lon = left + (right - left) * col / (size - 1);
          buffer[row * size + col] = elevation(lon, lat);
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
  constructor(container, bounds, terrain, origin) {
    if (!Cesium) throw new Error("Cesium 资源加载失败，请检查 CDN 连接。");
    this.bounds = bounds;
    this.terrain = terrain;
    this.elevation = terrainSampler(terrain);
    const [west, south, east, north] = bounds;
    const localOrigin = degrees(origin ?? [(west + east) / 2, (south + north) / 2]);
    this.worldToLocal = Cesium.Matrix4.inverseTransformation(
      Cesium.Transforms.eastNorthUpToFixedFrame(localOrigin), new Cesium.Matrix4(),
    );
    this.viewer = new Cesium.Viewer(container, {
      baseLayer: false,
      terrainProvider: campusTerrain(terrain, this.elevation),
      creditContainer: document.getElementById("map-credits"),
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
    this.viewer.imageryLayers.add(new Cesium.ImageryLayer(tiles, {
      brightness: 0.42,
      contrast: 1.15,
      saturation: 0.25,
      gamma: 0.66,
    }));
    this.viewer.scene.globe.baseColor = color("#15191e");
    this.viewer.scene.globe.enableLighting = false;
    this.viewer.scene.globe.depthTestAgainstTerrain = Boolean(terrain);
    if (terrain) {
      this.viewer.scene.globe.maximumScreenSpaceError = 1.3;
      this.viewer.creditDisplay.addStaticCredit(new Cesium.Credit("Elevation: NASA SRTM / Mapzen", true));
    }
    this.viewer.scene.fog.enabled = false;
    this.viewer.scene.backgroundColor = color("#080a0f");
    this.viewer.scene.skyAtmosphere.show = false;
    this.viewer.scene.skyBox.show = false;
    this.viewer.scene.sun.show = false;
    this.viewer.scene.moon.show = false;
    this.layers = new Map();
    this.staticVolumes = [];
    this.buildingRoofs = [];
    this.padLabels = new Map();
    this.taskSiteIds = [];
    this.pickMarkers = [];
    this.dynamicVolumes = [];
    this.padFocus = this.viewer.entities.add({
      show: false,
      point: { pixelSize: 15, color: color("#d6fff8"), outlineColor: color("#17a48f"), outlineWidth: 3 },
    });
    this.flightEntity = this.viewer.entities.add({
      name: "VOXAERO-01",
      show: false,
      orientation: new Cesium.ConstantProperty(),
      point: { pixelSize: 9, color: color("#96ffe1"), outlineColor: color("#124e48"), outlineWidth: 2, disableDepthTestDistance: Number.POSITIVE_INFINITY },
      model: {
        uri: "/assets/voxaero-01.glb",
        minimumPixelSize: 56,
        maximumScale: 2.5,
        runAnimations: true,
      },
    });
    const pose = (time) => this.flightEntity.position?.getValue(time);
    const anchor = (time) => {
      const position = pose(time);
      if (!position) return undefined;
      const { longitude, latitude, height } = Cesium.Cartographic.fromCartesian(position);
      return Cesium.Cartesian3.fromRadians(longitude, latitude, height + (this.lowAircraft ? 18 : 13));
    };
    this.aircraftAnchor = anchor;
    this.aircraftTag = document.getElementById("aircraft-tag");
    this.aircraftTagValue = this.aircraftTag.querySelector(".aircraft-tag-value");
    this.aircraftTagWidth = 176;
    new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0) this.aircraftTagWidth = this.aircraftTag.offsetWidth;
    }).observe(this.aircraftTag);
    this.aircraftLeader = this.viewer.entities.add({
      show: false,
      polyline: {
        positions: new Cesium.CallbackProperty((time) => {
          const position = pose(time);
          return position ? [position, anchor(time)] : [];
        }, false),
        width: 1.5, material: color("#38bdf8", .7), arcType: Cesium.ArcType.NONE,
      },
    });
    this.landingLabel = document.getElementById("ar-landing-label");
    const hud = document.querySelector(".hud-overlay");
    const hudHeader = hud.querySelector(".hud-header");
    const hudReadouts = document.getElementById("hud-readouts");
    const dock = document.querySelector(".flight-dock");
    const measureFpv = () => {
      this.landingLabel.hidden = true;
      const canvas = this.viewer.canvas.getBoundingClientRect();
      const view = hud.getBoundingClientRect();
      const bottom = (hudReadouts.hidden ? dock : hudReadouts).getBoundingClientRect().top;
      this.landingBounds = {
        left: view.left - canvas.left + 6, right: view.right - canvas.left - 6,
        top: Math.max(hudHeader.getBoundingClientRect().bottom - canvas.top + 8, canvas.height * .23),
        bottom: bottom - canvas.top - 8,
      };
      this.viewer.scene.requestRender();
    };
    const fpvLayout = new ResizeObserver(measureFpv);
    for (const node of [hud, hudHeader, hudReadouts, dock, dock.parentElement]) fpvLayout.observe(node);
    window.addEventListener("resize", measureFpv);
    this.viewer.scene.postRender.addEventListener(() => this.positionLabels());
    this.guideLines = Array.from({ length: 6 }, (_, index) => this.viewer.entities.add({
      show: false,
      polyline: { width: index < 4 ? 1.5 : 2, material: color("#8ae3b6", index < 4 ? .58 : .86), arcType: Cesium.ArcType.NONE },
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
    this.viewer.screenSpaceEventHandler.setInputAction(({ position }) => {
      if (!this.locationPicker) return;
      const { camera, scene } = this.viewer;
      const ray = camera.getPickRay(position);
      const hit = ray && scene.globe.pick(ray, scene);
      if (!hit) return;
      const { longitude, latitude } = Cesium.Cartographic.fromCartesian(hit);
      this.locationPicker({ lon: Cesium.Math.toDegrees(longitude), lat: Cesium.Math.toDegrees(latitude) });
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
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
        hoverText: feature.properties.name + " · " + height.toFixed(1) + " 米" + (provenance ? " · " + provenance : ""),
      });
      const bounds = this.asVolume(feature, base_alt, collision_top_alt ?? top);
      this.staticVolumes.push(bounds);
      this.buildingRoofs.push({ ...bounds, top });
    }
    this.roadFeatures = data.roads?.features ?? [];
    this.layers.get("roads").show = false;
    for (const feature of data.vertiports.features) {
      this.addPad(feature.properties, feature.geometry.coordinates);
    }
    const zones = this.layers.get("nofly_zones").entities;
    for (const feature of data.nofly_zones.features) {
      const { min_alt, max_alt, name, source_kind } = feature.properties;
      const external = source_kind === "external";
      volume(zones, feature, {
        bottom: min_alt, top: max_alt,
        fill: color(external ? "#d9466c" : "#ef4444", 0.20),
        outline: color(external ? "#f19db1" : "#ef7777", 0.82),
        hoverText: (external ? "外部空域快照" : "场景围栏") + " · " + name,
      });
      this.staticVolumes.push(this.asVolume(feature, min_alt, max_alt));
    }
    this.drawGrid(this.layers.get("airspace").entities, data.airspace_layers);
    await Promise.all(Array.from(this.layers.values(), (layer) => this.viewer.dataSources.add(layer)));
    this.resetView();
  }

  addPad({ id, name, altitude, type }, coordinates) {
    const pads = this.layers.get("vertiports").entities;
    const tint = color(type === "task" ? "#93c5fd" : type === "emergency" ? "#97aeaf" : "#86d7cc");
    const position = degrees(coordinates, altitude + 1);
    const anchor = degrees(coordinates, altitude + 7);
    pads.add({
      id, name, position,
      ellipse: {
        height: altitude + 0.65, semiMajorAxis: 11, semiMinorAxis: 11,
        material: tint.withAlpha(.36), outline: true, outlineColor: tint,
      },
      point: { pixelSize: 7, color: tint, outlineColor: color("#172838"), outlineWidth: 1.5, disableDepthTestDistance: Number.POSITIVE_INFINITY },
      properties: { hoverText: name },
    });
    const leader = pads.add({
      position: anchor,
      polyline: { positions: [position, anchor], width: 1, material: tint.withAlpha(.4), arcType: Cesium.ArcType.NONE },
      label: {
        text: id, font: `600 11px ${readoutFont}`, fillColor: tint.withAlpha(.76),
        style: Cesium.LabelStyle.FILL_AND_OUTLINE, outlineColor: color("#101724"), outlineWidth: 3,
        pixelOffset: new Cesium.Cartesian2(0, -12), disableDepthTestDistance: Number.POSITIVE_INFINITY,
        showBackground: true, backgroundColor: color("#111a26", .8), backgroundPadding: new Cesium.Cartesian2(5, 3),
      },
    });
    this.padLabels.set(id, { leader, anchor, tint });
  }

  setTaskSites(sites = []) {
    const pads = this.layers.get("vertiports").entities;
    for (const id of this.taskSiteIds) {
      pads.removeById(id);
      pads.remove(this.padLabels.get(id).leader);
      this.padLabels.delete(id);
    }
    this.taskSiteIds = [];
    for (const { id, name, position } of sites) {
      if (!id.startsWith("TMP-")) continue;
      this.addPad({ id, name, type: "task", altitude: position.alt }, [position.lon, position.lat]);
      this.taskSiteIds.push(id);
    }
    this.viewer.scene.requestRender();
  }

  pickLocation(handler) {
    this.locationPicker = handler;
    this.viewer.canvas.classList.toggle("picking-site", Boolean(handler));
  }

  showPickMarkers(markers) {
    for (const marker of this.pickMarkers) this.viewer.entities.remove(marker);
    this.pickMarkers = markers.map(({ lon, lat, name }) => this.viewer.entities.add({
      position: degrees([lon, lat], this.elevation(lon, lat) + 1),
      point: {
        color: color("#93c5fd"), pixelSize: 8, outlineColor: color("#111927"),
        outlineWidth: 2, disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
      label: {
        text: name, font: `600 11px ${labelFont}`, fillColor: color("#e7eef8"),
        showBackground: true, backgroundColor: color("#111927", .9),
        pixelOffset: new Cesium.Cartesian2(0, -14), disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    }));
    this.viewer.scene.requestRender();
  }

  drawGrid(entities, layers) {
    const [west, south, east, north] = this.bounds;
    const boundaries = layers.filter((layer) => layer.id !== "ground").map((layer) => layer.min_alt);
    const tint = "#818cf8";
    for (const alt of boundaries) {
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
    }
    const edgePoints = [.44, .40, .48, .36, .52].map((fraction) => [
      west + (east - west) * fraction, north - (north - south) * .06,
    ]);
    const corner = edgePoints.find(([lon, lat]) => {
      const [x, y] = this.project(lon, lat);
      return this.staticVolumes.every(({ bounds: [left, bottom, right, top], rings }) =>
        x < left - 22 || x > right + 22 || y < bottom - 22 || y > top + 22 || footprintDistance(rings, x, y) > 22);
    }) ?? edgePoints[0];
    const base = this.elevation(...corner);
    const ticks = [0, ...boundaries];
    entities.add({ polyline: {
      positions: [degrees(corner, base), degrees(corner, base + ticks.at(-1))],
      width: 1.2, material: color(tint, .72), arcType: Cesium.ArcType.NONE,
    } });
    for (const alt of ticks) {
      const side = [corner[0] + .00005, corner[1]];
      entities.add({
        position: degrees(side, base + alt),
        polyline: {
          positions: [degrees(corner, base + alt), degrees(side, base + alt)],
          width: 1.2, material: color(tint, .8), arcType: Cesium.ArcType.NONE,
        },
        label: {
          text: (alt === ticks.at(-1) ? "离地 " : "") + alt + " m",
          font: `650 11px ${readoutFont}`, fillColor: color("#d2d6ff"),
          showBackground: true, backgroundColor: color("#171d32", .94), backgroundPadding: new Cesium.Cartesian2(5, 3),
          pixelOffset: new Cesium.Cartesian2(4, 0), horizontalOrigin: Cesium.HorizontalOrigin.LEFT,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      });
    }
  }

  setRouteTargets(start, end) {
    for (const [id, marker] of this.padLabels) {
      const role = id === start ? "start" : id === end ? "end" : "";
      marker.role = role;
      const label = marker.leader.label;
      const tint = role === "start" ? color("#7dd3fc") : role === "end" ? color("#5eead4") : marker.tint;
      label.font = `${role ? "700 12px" : "600 11px"} ${readoutFont}`;
      label.fillColor = tint.withAlpha(role ? 1 : .7);
      label.backgroundColor = color("#0a121c", role ? .95 : .68);
      marker.leader.polyline.material = tint.withAlpha(role ? .74 : .35);
    }
    this.viewer.scene.requestRender();
  }

  altitudeAGL({ lon, lat, alt }) {
    return Math.max(0, alt - this.elevation(lon, lat));
  }

  updateAircraftTag(altitude, tone, agl) {
    this.lowAircraft = agl < 12;
    if (this.aircraftTagValue.textContent !== altitude) this.aircraftTagValue.textContent = altitude;
    if (this.aircraftTag.dataset.tone !== tone) this.aircraftTag.dataset.tone = tone;
  }

  positionLabels() {
    const { scene, clock, canvas } = this.viewer;
    const fpv = document.body.dataset.view === "fpv";
    this.positionLandingLabel(fpv);
    const position = this.flightEntity.show && !fpv ? this.aircraftAnchor(clock.currentTime) : undefined;
    const point = position && Cesium.SceneTransforms.worldToWindowCoordinates(scene, position);
    const visible = Boolean(point && point.x >= 0 && point.x <= canvas.clientWidth
      && point.y >= 34 && point.y <= canvas.clientHeight);
    const occupied = [];
    if (this.aircraftLeader.show !== visible) {
      this.aircraftLeader.show = visible;
      scene.requestRender();
    }
    this.aircraftTag.hidden = !visible;
    if (visible) {
      const edge = this.aircraftTagWidth / 2 + 4;
      const x = Cesium.Math.clamp(point.x, edge, canvas.clientWidth - edge);
      this.aircraftTag.style.transform = `translate3d(${x}px, ${point.y}px, 0) translate(-50%, -100%)`;
      occupied.push({ x, y: point.y - 13, width: this.aircraftTagWidth, height: 26 });
    }

    const points = [];
    if (this.layers.get("vertiports")?.show) {
      const markers = [...this.padLabels.values()].sort((a, b) => Number(Boolean(b.role)) - Number(Boolean(a.role)));
      for (const { leader, anchor, role } of markers) points.push({
        position: anchor, label: leader.label, width: Math.max(42, leader.label.text.getValue().length * 7 + 10),
        offsets: role === "start" ? [[-23, -12], [-43, 2], [34, 7], [0, 24], [0, -37]]
          : role === "end" ? [[25, -15], [36, 12], [-37, 8], [0, 24], [0, -37]]
            : [[0, -11], [21, -13], [-22, -10], [0, 15], [-28, 11]],
      });
    }
    if (this.layers.get("route").show) {
      points.push(...(this.routeVisitLabels ?? []));
      if (this.routeAltitudeLabel) points.push(this.routeAltitudeLabel);
    }
    for (const { position, label, width, offsets } of points) {
      const height = 19;
      const point = !fpv && Cesium.SceneTransforms.worldToWindowCoordinates(scene, position);
      const placement = point && [...offsets, [0, 43], [-51, 28], [51, 28], [0, -63]].find(([dx, dy]) => {
        const x = point.x + dx;
        const y = point.y + dy;
        return x >= width / 2 + 3 && x <= canvas.clientWidth - width / 2 - 3
          && y >= height / 2 + 3 && y <= canvas.clientHeight - height / 2 - 3
          && occupied.every((box) => Math.abs(x - box.x) >= (width + box.width) / 2 + 6
            || Math.abs(y - box.y) >= (height + box.height) / 2 + 5);
      });
      const show = Boolean(placement);
      if (label.show?.getValue(clock.currentTime) !== show) {
        label.show = show;
        scene.requestRender();
      }
      if (!placement) continue;
      const [x, y] = placement;
      occupied.push({ x: point.x + x, y: point.y + y, width, height });
      const current = label.pixelOffset?.getValue(clock.currentTime);
      if (current?.x !== x || current?.y !== y) {
        label.pixelOffset = new Cesium.Cartesian2(x, y);
        scene.requestRender();
      }
    }
  }

  positionLandingLabel(fpv) {
    const { scene, camera } = this.viewer;
    const bounds = this.landingBounds;
    const target = this.guidePosition;
    const heading = target && Cesium.Cartesian3.subtract(target, camera.positionWC, new Cesium.Cartesian3());
    const ahead = heading && Cesium.Cartesian3.dot(camera.directionWC, heading) > 0;
    const point = fpv && bounds && ahead && Cesium.SceneTransforms.worldToWindowCoordinates(scene, target);
    const desiredY = point && point.y + 15;
    const lowerEdge = point && bounds.bottom - 24;
    const y = point && Math.min(desiredY, lowerEdge);
    const visible = Boolean(point && point.x - 66 >= bounds.left && point.x + 66 <= bounds.right && y >= bounds.top);
    this.landingLabel.hidden = !visible;
    if (visible) {
      this.landingLabel.toggleAttribute("data-below", desiredY > lowerEdge);
      this.landingLabel.style.transform = `translate3d(${point.x}px, ${y}px, 0) translateX(-50%)`;
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
            material: color("#8793a4", .6),
            clampToGround: true,
          } });
        }
      }
    }
    this.layers.get(key).show = visible;
    this.viewer.scene.requestRender();
  }

  resetView() {
    this.viewer.camera.cancelFlight();
    this.clearPadFocus();
    const [west, south, east, north] = this.bounds;
    const target = degrees([(west + east) / 2, (south + north) / 2], this.terrain ? 35 : 20);
    const extent = Cesium.Cartesian3.distance(degrees([west, south]), degrees([east, north]));
    const aspect = this.viewer.canvas.clientWidth / this.viewer.canvas.clientHeight;
    const range = extent * Math.max(.78, .92 / Math.max(.55, aspect));
    this.viewer.camera.lookAt(target, new Cesium.HeadingPitchRange(-0.30, -0.80, range));
    this.viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
    this.viewer.scene.requestRender();
  }

  focusRoute(waypoints, usableWidth) {
    const camera = this.viewer.camera;
    const { clientWidth, clientHeight } = this.viewer.canvas;
    const points = waypoints.map(({ lon, lat, alt }) => degrees([lon, lat], alt));
    const bounds = Cesium.BoundingSphere.fromPoints(points);
    const vertical = Math.tan(camera.frustum.fovy / 2) * .76;
    const aspect = (usableWidth || clientWidth) / Math.max(1, clientHeight);
    const halfAngle = Math.atan(vertical * Math.min(1, aspect));
    const range = Math.max(185, (bounds.radius * 1.18 + 12) / Math.sin(halfAngle));
    camera.flyToBoundingSphere(bounds, {
      offset: new Cesium.HeadingPitchRange(-.3, -.86, range),
      duration: matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : .8,
    });
    this.viewer.scene.requestRender();
  }

  focusPad(id) {
    const pad = this.layers.get("vertiports").entities.getById(id);
    if (!pad) return;
    this.focusPoint(pad.position.getValue(this.viewer.clock.currentTime));
  }

  focusPosition({ lon, lat, alt }) {
    // Keep the event above the review controls at the bottom of the scene.
    this.focusPoint(degrees([lon, lat], alt), 28);
  }

  focusPoint(position, cameraDrop = 0) {
    const camera = this.viewer.camera;
    camera.cancelFlight();
    this.padFocus.position = position;
    this.padFocus.show = true;
    camera.lookAt(position, new Cesium.HeadingPitchRange(-0.3, -0.92, 135));
    camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
    if (cameraDrop) camera.moveDown(cameraDrop);
    this.viewer.scene.requestRender();
  }

  clearPadFocus() {
    this.padFocus.show = false;
    this.viewer.scene.requestRender();
  }

  drawRoute(waypoints, rerouted, preview = false, visits = []) {
    const entities = this.layers.get("route").entities;
    entities.removeAll();
    this.addRoute(entities, waypoints, rerouted, preview ? "#7dd3fc" : null);
    this.addRouteDepth(entities, waypoints, rerouted);
    this.routeVisitLabels = this.addVisitMarkers(entities, waypoints, visits);
    this.viewer.scene.requestRender();
  }

  drawReplayRoutes(segments, comparison) {
    const entities = this.layers.get("route").entities;
    entities.removeAll();
    if (comparison) this.addRoute(entities, comparison, false, "#6588aa");
    segments.forEach(({ flown }, index) => this.addRoute(entities, flown, index > 0));
    this.addRouteDepth(entities, segments.at(-1)?.flown ?? [], segments.length > 1);
    this.routeVisitLabels = segments.flatMap(({ result, flown }) => this.addVisitMarkers(entities, flown,
      (result.route_visits ?? []).filter(visit => {
        const point = flown[visit.waypoint_index];
        return point && point.lon === visit.lon && point.lat === visit.lat;
      })));
    this.viewer.scene.requestRender();
  }

  addVisitMarkers(entities, waypoints, visits) {
    return visits.flatMap((visit, index) => {
      const point = waypoints[visit.waypoint_index];
      if (!point) return [];
      const position = degrees([point.lon, point.lat], point.alt);
      const { label } = entities.add({
        position,
        properties: { hoverText: `飞越 ${visit.name}` },
        point: { pixelSize: 8, color: color("#9df5d8"), outlineColor: color("#10241f"), outlineWidth: 2,
          disableDepthTestDistance: Number.POSITIVE_INFINITY },
        label: {
          text: `途经 ${index + 1}`, font: `650 11px ${labelFont}`, fillColor: color("#b5f3df"),
          showBackground: true, backgroundColor: color("#0d211d", .94),
          backgroundPadding: new Cesium.Cartesian2(6, 3), pixelOffset: new Cesium.Cartesian2(0, -14),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      });
      return [{ position, label, width: 58, offsets: [[0, -14], [23, -26], [-26, -15], [24, 18]] }];
    });
  }

  addRoute(entities, waypoints, rerouted, dashedTint = null) {
    const tints = { takeoff: "#38bdf8", cruise: "#42cbdc", landing: "#2dd4bf", transition: "#818cf8" };
    for (let i = 1; i < waypoints.length; i += 1) {
      const phase = waypoints[i].phase;
      const hex = rerouted && phase === "cruise" ? "#ffca78" : tints[phase];
      entities.add({
        polyline: {
          positions: waypoints.slice(i - 1, i + 1).map(({ lon, lat, alt }) => degrees([lon, lat], alt)),
          width: dashedTint ? 4 : 5,
          material: dashedTint ? new Cesium.PolylineDashMaterialProperty({ color: color(dashedTint) })
            : new Cesium.PolylineGlowMaterialProperty({ color: color(hex), glowPower: .2 }),
          arcType: Cesium.ArcType.NONE,
        },
      });
    }
  }

  addRouteDepth(entities, waypoints, rerouted) {
    this.routeAltitudeLabel = null;
    if (waypoints.length < 2) return;
    const cartesian = ({ lon, lat, alt }) => degrees([lon, lat], alt);
    const dash = (opacity) => new Cesium.PolylineDashMaterialProperty({
      color: color("#38bdf8", opacity), dashLength: 11,
    });
    const horizontal = waypoints.filter((point, index) => !index
      || point.lon !== waypoints[index - 1].lon || point.lat !== waypoints[index - 1].lat);
    if (horizontal.length > 1) {
      entities.add({ polyline: {
        positions: horizontal.map(({ lon, lat }) => degrees([lon, lat])), width: 2,
        clampToGround: true, material: dash(.48),
      } });
    }
    const takeoff = waypoints.slice(1).findLast((point) => point.phase === "takeoff");
    const approach = waypoints.findLast((point) => point.phase === "cruise" || point.phase === "transition");
    for (const [pad, flight] of [[waypoints[0], takeoff], [waypoints.at(-1), approach]]) {
      if (!flight) continue;
      const ground = this.elevation(pad.lon, pad.lat);
      const altitude = Math.max(pad.alt, flight.alt);
      if (altitude > ground + 1) entities.add({ polyline: {
        positions: [degrees([pad.lon, pad.lat], ground + .5), degrees([pad.lon, pad.lat], altitude)],
        width: 1.6, arcType: Cesium.ArcType.NONE, material: dash(.75),
      } });
    }
    const cruise = waypoints.slice(1).flatMap((point, index) => point.phase === "cruise"
      ? [[cartesian(waypoints[index]), cartesian(point)]] : []);
    const middle = cruise[Math.floor(cruise.length / 2)];
    const lerp = (a, b, fraction) => Cesium.Cartesian3.lerp(a, b, fraction, new Cesium.Cartesian3());
    const tint = color(rerouted ? "#f59e0b" : "#7dd3fc");
    for (const [a, b] of cruise) {
      const length = Cesium.Cartesian3.distance(a, b);
      if (length < 12) continue;
      const count = Math.min(4, Math.ceil(length / 100));
      for (let index = 1; index <= count; index += 1) {
        const end = index / (count + 1);
        const start = Math.max(0, end - Math.min(.18, 15 / length));
        entities.add({ polyline: {
          positions: [lerp(a, b, start), lerp(a, b, end)], width: 8,
          material: new Cesium.PolylineArrowMaterialProperty(tint),
          arcType: Cesium.ArcType.NONE,
        } });
      }
    }
    if (middle) {
      const position = lerp(...middle, .5);
      const { longitude, latitude, height } = Cesium.Cartographic.fromCartesian(position);
      const altitude = this.altitudeAGL({ lon: Cesium.Math.toDegrees(longitude), lat: Cesium.Math.toDegrees(latitude), alt: height });
      const { label } = entities.add({
        position,
        label: {
          text: "航道离地 " + altitude.toFixed(1) + " m", font: `620 11px ${labelFont}`, fillColor: color("#c4d3df"),
          showBackground: true, backgroundColor: color("#101821", .98),
          backgroundPadding: new Cesium.Cartesian2(7, 4),
          pixelOffset: new Cesium.Cartesian2(0, -17), disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      });
      this.routeAltitudeLabel = { position, label, width: 116, offsets: [[0, -17], [20, -32], [-18, 19], [40, 13]] };
    }
  }

  profileSurface(lon, lat) {
    const ground = this.elevation(lon, lat);
    const [x, y] = this.project(lon, lat);
    let roof = ground;
    for (const building of this.buildingRoofs) {
      const [west, south, east, north] = building.bounds;
      if (x >= west && x <= east && y >= south && y <= north && footprintDistance(building.rings, x, y) < .01) {
        roof = Math.max(roof, building.top);
      }
    }
    return { ground, roof };
  }

  setHazards(features) {
    const entities = this.layers.get("hazards").entities;
    entities.removeAll();
    this.dynamicVolumes = features.map((feature) => {
      const { min_alt, max_alt, reason, name } = feature.properties;
      const task = reason === "task_avoidance";
      volume(entities, feature, {
        bottom: min_alt, top: max_alt,
        fill: color(task ? "#8b5cf6" : "#f59e0b", .22), outline: color(task ? "#c4b5fd" : "#fbbf24"),
        hoverText: name + " · " + min_alt.toFixed(1) + "–" + max_alt.toFixed(1) + " 米",
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
    const rings = feature.geometry.coordinates.map((ring) => ring.map(([lon, lat]) => this.project(lon, lat)));
    const x = rings[0].map((point) => point[0]);
    const y = rings[0].map((point) => point[1]);
    return { rings, bounds: [Math.min(...x), Math.min(...y), Math.max(...x), Math.max(...y)], id: feature.properties.id, bottom, top };
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
    this.guidePosition = null;
    if (target) {
      const anchor = degrees([target.lon, target.lat], target.alt + .45);
      const localFrame = Cesium.Transforms.eastNorthUpToFixedFrame(anchor);
      const offset = (east, north, up = 0) => Cesium.Matrix4.multiplyByPoint(
        localFrame, new Cesium.Cartesian3(east, north, up), new Cesium.Cartesian3(),
      );
      lines.push([offset(-8, 0), offset(8, 0)], [offset(0, -8), offset(0, 8)]);
      this.guidePosition = offset(0, 0, 2);
      const text = "降落靶标 · " + targetId;
      if (this.landingLabel.textContent !== text) this.landingLabel.textContent = text;
    }
    this.guideLines.forEach((line, index) => {
      line.show = Boolean(lines[index]);
      if (line.show) line.polyline.positions = lines[index];
    });
    this.viewer.scene.requestRender();
  }
}
