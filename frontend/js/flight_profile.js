const WIDTH = 244;
const HEIGHT = 56;
const CORRIDORS = { delivery: [30, 60], inspection: [60, 90] };
const element = (suffix) => document.getElementById("profile-" + suffix);
const path = (points) => points.map(([x, y], index) => (index ? "L" : "M") + x.toFixed(2) + "," + y.toFixed(2)).join(" ");

export class VerticalProfile {
  constructor(map) {
    this.map = map;
    this.nodes = Object.fromEntries([
      "band", "start", "end", "corridor", "building", "baseline", "plan",
      "area", "track", "clip", "position", "clearance-line", "altitude", "clearance",
    ].map((key) => [key, element(key)]));
  }

  setTrack(track, corridor, start, end) {
    const nodes = this.nodes;
    const [floor, ceiling] = CORRIDORS[corridor];
    nodes.band.textContent = `巡航带 ${floor}–${ceiling} m`;
    nodes.start.textContent = "● " + start;
    nodes.end.textContent = end + " ◎";
    this.track = track;
    // Include the original bends as well as samples for the terrain and buildings.
    const times = [...new Set([
      ...track.legs.map(({ end }) => end),
      ...Array.from({ length: 97 }, (_, index) => track.duration * index / 96),
    ])].sort((a, b) => a - b);
    const samples = times.map((time) => {
      const { longitude, latitude, height } = Cesium.Cartographic.fromCartesian(track.at(time));
      const surface = this.map.profileSurface(Cesium.Math.toDegrees(longitude), Cesium.Math.toDegrees(latitude));
      return { x: this.x(time), alt: height, ...surface };
    });
    this.limit = Math.max(95, ...samples.map(({ alt, roof, ground }) => Math.max(alt, roof, ceiling) - ground + 5));
    const upper = samples.map(({ x, ground }) => [x, this.y(ceiling - ground)]);
    const lower = samples.map(({ x, ground }) => [x, this.y(floor - ground)]);
    const silhouette = samples.map(({ x, roof, ground }) => [x, this.y(roof - ground)]);
    const flight = samples.map(({ x, alt, ground }) => [x, this.y(alt - ground)]);
    nodes.corridor.setAttribute("d", path([...upper, ...lower.slice().reverse()]) + "Z");
    nodes.baseline.setAttribute("d", path(lower));
    nodes.building.setAttribute("d", path([[0, HEIGHT], ...silhouette, [WIDTH, HEIGHT]]) + "Z");
    nodes.plan.setAttribute("d", path(flight));
    nodes.track.setAttribute("d", path(flight));
    nodes.area.setAttribute("d", path([[0, HEIGHT], ...flight, [WIDTH, HEIGHT]]) + "Z");
  }

  x(time) {
    return 4 + (WIDTH - 8) * Math.min(1, time / this.track.duration);
  }

  y(agl) {
    return HEIGHT - 4 - Math.max(0, agl) / this.limit * (HEIGHT - 8);
  }

  paint(frame, clearance) {
    if (!this.track) return;
    const nodes = this.nodes;
    const { lon, lat, alt } = frame.coordinates;
    const { ground, roof } = this.map.profileSurface(lon, lat);
    const agl = Math.max(0, alt - ground);
    const x = this.x(frame.elapsed);
    const y = this.y(agl);
    nodes.clip.setAttribute("width", x);
    nodes.position.setAttribute("cx", x);
    nodes.position.setAttribute("cy", y);
    nodes.position.setAttribute("visibility", "visible");
    const line = nodes["clearance-line"];
    line.setAttribute("x1", x);
    line.setAttribute("x2", x);
    line.setAttribute("y1", y + 3);
    line.setAttribute("y2", this.y(roof - ground));
    line.setAttribute("visibility", alt - roof > 6 ? "visible" : "hidden");
    nodes.altitude.hidden = false;
    nodes.altitude.textContent = agl.toFixed(1) + " m";
    nodes.altitude.style.left = (100 * x / WIDTH) + "%";
    nodes.altitude.style.top = Math.max(0, Math.min(39, y - 11)) + "px";
    nodes.altitude.style.translate = x > WIDTH * .72 ? "calc(-100% - 6px)" : "6px";
    nodes.clearance.textContent = clearance == null ? "净空 —" : `净空 ${clearance.toFixed(1)} m`;
  }
}
