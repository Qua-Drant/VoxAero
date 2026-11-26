const WIDTH = 244;
const LEFT = 8;
const RIGHT = 236;
const TOP = 8;
const BOTTOM = 64;
const CORRIDORS = { delivery: [30, 60], inspection: [60, 90] };
const element = (suffix) => document.getElementById("profile-" + suffix);
const path = (points) => points.map(([x, y], index) => (index ? "L" : "M") + x.toFixed(2) + "," + y.toFixed(2)).join(" ");

export class VerticalProfile {
  constructor(map) {
    this.map = map;
    this.nodes = Object.fromEntries([
      "band", "start", "end", "corridor", "building", "baseline", "superseded", "plan",
      "area", "track", "clip", "position", "cursor", "clearance-line", "altitude",
    ].map((key) => [key, element(key)]));
    this.ticks = document.querySelectorAll(".profile-ticks span");
  }

  setTrack(track, corridor, start, end, priorWaypoints = null) {
    const nodes = this.nodes;
    const [floor, ceiling] = CORRIDORS[corridor];
    nodes.band.textContent = `场景航层 ${floor}–${ceiling} m`;
    nodes.start.textContent = "出发 " + start;
    nodes.end.textContent = "到达 " + end;
    this.track = track;
    // Include route bends in addition to evenly spaced surface samples.
    const times = [...new Set([
      ...track.legs.map(({ end }) => end),
      ...Array.from({ length: 97 }, (_, index) => track.duration * index / 96),
    ])].sort((a, b) => a - b);
    this.samples = times.map((time) => {
      const { longitude, latitude, height } = Cesium.Cartographic.fromCartesian(track.at(time));
      const surface = this.map.profileSurface(Cesium.Math.toDegrees(longitude), Cesium.Math.toDegrees(latitude));
      return { x: this.x(time), alt: height, ...surface };
    });
    let priorSamples = null;
    if (priorWaypoints?.length > 1) {
      let total = 0;
      const cumulative = [0];
      for (let i = 1; i < priorWaypoints.length; i += 1) {
        const a = Cesium.Cartesian3.fromDegrees(priorWaypoints[i - 1].lon, priorWaypoints[i - 1].lat, priorWaypoints[i - 1].alt);
        const b = Cesium.Cartesian3.fromDegrees(priorWaypoints[i].lon, priorWaypoints[i].lat, priorWaypoints[i].alt);
        total += Cesium.Cartesian3.distance(a, b);
        cumulative.push(total);
      }
      if (total > 0) {
        priorSamples = priorWaypoints.map((point, index) => {
          const surface = this.map.profileSurface(point.lon, point.lat);
          return { x: LEFT + (RIGHT - LEFT) * (cumulative[index] / total), alt: point.alt, ...surface };
        });
      }
    }
    const maxPrior = priorSamples ? Math.max(...priorSamples.map(({ alt, ground }) => alt - ground)) : 0;
    const max = Math.max(maxPrior, ...this.samples.map(({ alt, roof, ground }) => Math.max(alt, roof, ceiling) - ground));
    this.limit = Math.max(50, Math.ceil((max + 4) / 20) * 20);
    [this.limit, this.limit / 2, 0].forEach((value, index) => { this.ticks[index].textContent = value; });
    const upper = this.samples.map(({ x, ground }) => [x, this.y(ceiling - ground)]);
    const lower = this.samples.map(({ x, ground }) => [x, this.y(floor - ground)]);
    const silhouette = this.samples.map(({ x, roof, ground }) => [x, this.y(roof - ground)]);
    const flight = this.samples.map(({ x, alt, ground }) => [x, this.y(alt - ground)]);
    nodes.corridor.setAttribute("d", path([...upper, ...lower.slice().reverse()]) + "Z");
    nodes.baseline.setAttribute("d", path(lower));
    nodes.building.setAttribute("d", path([[LEFT, BOTTOM], ...silhouette, [RIGHT, BOTTOM]]) + "Z");
    nodes.superseded.setAttribute("d", priorSamples ? path(priorSamples.map(({ x, alt, ground }) => [x, this.y(alt - ground)])) : "");
    nodes.plan.setAttribute("d", path(flight));
    nodes.track.setAttribute("d", path(flight));
    nodes.area.setAttribute("d", path([[LEFT, BOTTOM], ...flight, [RIGHT, BOTTOM]]) + "Z");
  }

  x(time) {
    return LEFT + (RIGHT - LEFT) * Math.max(0, Math.min(1, time / this.track.duration));
  }

  y(agl) {
    return BOTTOM - Math.max(0, Math.min(this.limit, agl)) / this.limit * (BOTTOM - TOP);
  }

  paint(frame) {
    const nodes = this.nodes;
    const x = this.x(frame.elapsed);
    const y = this.y(frame.agl);
    nodes.clip.setAttribute("width", x);
    nodes.position.setAttribute("cx", x);
    nodes.position.setAttribute("cy", y);
    nodes.position.setAttribute("visibility", "visible");
    nodes.position.dataset.tone = frame.instrumentTone;
    nodes.cursor.setAttribute("x1", x);
    nodes.cursor.setAttribute("x2", x);
    nodes.cursor.setAttribute("y1", TOP - 4);
    nodes.cursor.setAttribute("y2", y - 4);
    nodes.cursor.setAttribute("visibility", y > TOP + 2 ? "visible" : "hidden");

    const sample = this.samples.find(({ x: at }) => at >= x) ?? this.samples.at(-1);
    const surfaceY = this.y(sample.roof - sample.ground);
    const line = nodes["clearance-line"];
    line.setAttribute("x1", x);
    line.setAttribute("x2", x);
    line.setAttribute("y1", y + 4);
    line.setAttribute("y2", surfaceY);
    line.setAttribute("visibility", !frame.onGround && surfaceY > y + 7 ? "visible" : "hidden");
    line.dataset.tone = frame.instrumentTone;
    nodes.altitude.hidden = false;
    if (nodes.altitude.textContent !== frame.altitudeText) nodes.altitude.textContent = frame.altitudeText;
    nodes.altitude.style.setProperty("--plot-x", 100 * x / WIDTH + "%");
  }
}
