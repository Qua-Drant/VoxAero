const Cesium = globalThis.Cesium;

const color = (hex, opacity = 1) => Cesium.Color.fromCssColorString(hex).withAlpha(opacity);
const degrees = ([lon, lat], alt = 0) => Cesium.Cartesian3.fromDegrees(lon, lat, alt);

function hierarchy(rings) {
  const [outer, ...holes] = rings;
  const ring = (coordinates) => coordinates.slice(0, -1).map((point) => degrees(point));
  return new Cesium.PolygonHierarchy(
    ring(outer), holes.map((hole) => new Cesium.PolygonHierarchy(ring(hole))),
  );
}

function volume(entitySet, feature, { bottom, top, fill, outline }) {
  const rings = feature.geometry.coordinates;
  entitySet.add({
    name: feature.properties.name,
    polygon: {
      hierarchy: hierarchy(rings),
      height: bottom,
      extrudedHeight: top,
      material: fill,
    },
  });
  rings.forEach((ring) => entitySet.add({
    polyline: {
      positions: ring.map((point) => degrees(point, top + 0.2)),
      material: outline,
      width: 1.3,
      arcType: Cesium.ArcType.NONE,
    },
  }));
}

export class CampusMap {
  constructor(container, bounds) {
    if (!Cesium) throw new Error("Cesium 资源加载失败，请检查 CDN 连接。");
    this.bounds = bounds;
    this.viewer = new Cesium.Viewer(container, {
      baseLayer: false,
      terrainProvider: new Cesium.EllipsoidTerrainProvider(),
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
    this.viewer.scene.fog.enabled = false;
    this.layers = new Map();
    this.route = null;
  }

  async load(data) {
    for (const key of ["buildings", "vertiports", "nofly_zones", "airspace"]) {
      this.layers.set(key, new Cesium.CustomDataSource(key));
    }
    const buildings = this.layers.get("buildings").entities;
    for (const feature of data.buildings.features) {
      volume(buildings, feature, {
        bottom: 0, top: feature.properties.height,
        fill: color("#c0cdd9", 0.87), outline: color("#e8f4ff", 0.8),
      });
    }
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
    this.layers.get(key).show = visible;
    this.viewer.scene.requestRender();
  }

  resetView() {
    const [west, south, east, north] = this.bounds;
    const target = degrees([(west + east) / 2, (south + north) / 2], 20);
    const extent = Cesium.Cartesian3.distance(degrees([west, south]), degrees([east, north]));
    this.viewer.camera.lookAt(target, new Cesium.HeadingPitchRange(-0.34, -0.75, extent * 0.86));
    this.viewer.camera.lookAtTransform(Cesium.Matrix4.IDENTITY);
    this.viewer.scene.requestRender();
  }

  drawRoute(waypoints) {
    this.clearRoute();
    this.route = this.viewer.entities.add({
      id: "mission-route",
      name: "规划航线",
      polyline: {
        positions: waypoints.map(({ lon, lat, alt }) => degrees([lon, lat], alt)),
        width: 6,
        material: new Cesium.PolylineGlowMaterialProperty({ color: color("#b4ffa6"), glowPower: 0.24 }),
        arcType: Cesium.ArcType.NONE,
      },
    });
    this.viewer.scene.requestRender();
  }

  clearRoute() {
    if (!this.route) return;
    this.viewer.entities.remove(this.route);
    this.route = null;
    this.viewer.scene.requestRender();
  }
}
