const C = globalThis.Cesium;
const SPEED = { delivery: 12, inspection: 15 };
const CLIMB_SPEED = 3;
const NOSE_OFFSET = 0.42;

export function batteryAfterFlight(battery, seconds) {
  return Math.max(0, battery - seconds * .10);
}

function world({ lon, lat, alt }) {
  return C.Cartesian3.fromDegrees(lon, lat, alt);
}

function geographic(point) {
  const cartographic = C.Cartographic.fromCartesian(point);
  return {
    lon: C.Math.toDegrees(cartographic.longitude),
    lat: C.Math.toDegrees(cartographic.latitude),
    alt: Math.max(0, cartographic.height),
  };
}

function horizontalGap(a, b) {
  return C.Cartesian3.distance(world({ ...a, alt: 0 }), world({ ...b, alt: 0 }));
}

function plus(point, vector, meters) {
  return C.Cartesian3.add(point, C.Cartesian3.multiplyByScalar(vector, meters, new C.Cartesian3()), new C.Cartesian3());
}

function unit(vector) {
  return C.Cartesian3.normalize(vector, new C.Cartesian3());
}

function cross(a, b) {
  return C.Cartesian3.cross(a, b, new C.Cartesian3());
}

function makeTrack(waypoints, corridor) {
  const property = new C.SampledPositionProperty();
  const epoch = C.JulianDate.now();
  const time = (seconds) => C.JulianDate.addSeconds(epoch, seconds, new C.JulianDate());
  const points = waypoints.map(world);
  const legs = [];
  let duration = 0;
  property.addSample(epoch, points[0]);
  for (let i = 1; i < points.length; i += 1) {
    const horizontal = horizontalGap(waypoints[i - 1], waypoints[i]);
    const dz = waypoints[i].alt - waypoints[i - 1].alt;
    const seconds = Math.max(horizontal / SPEED[corridor], Math.abs(dz) / CLIMB_SPEED);
    if (seconds === 0) continue;
    legs.push({
      start: duration, end: duration + seconds, horizontal, dz,
      waypointIndex: i,
      speed: Math.hypot(horizontal, dz) / seconds,
      direction: C.Cartesian3.subtract(points[i], points[i - 1], new C.Cartesian3()),
      phase: waypoints[i].phase,
    });
    duration += seconds;
    property.addSample(time(duration), points[i]);
  }
  if (!legs.length) throw new Error("航线缺少可飞行航段。");
  const gates = [];
  let nextGate = 34;
  const at = (seconds) => property.getValue(time(seconds));
  for (const leg of legs) {
    if (leg.phase !== "cruise" || leg.horizontal < 2) continue;
    while (nextGate < leg.horizontal - 2) {
      const second = leg.start + (leg.end - leg.start) * nextGate / leg.horizontal;
      const center = at(second);
      const up = C.Ellipsoid.WGS84.geodeticSurfaceNormal(center, new C.Cartesian3());
      const side = unit(cross(leg.direction, up));
      const corner = (right, high) => plus(plus(center, side, right * 4.8), up, high * 3.2);
      gates.push({ second, corners: [corner(-1, -1), corner(-1, 1), corner(1, 1), corner(1, -1), corner(-1, -1)] });
      nextGate += 30;
    }
    nextGate = Math.max(5, nextGate - leg.horizontal);
  }
  return {
    property, time, duration, legs, at, gates, waypoints, origin: waypoints[0], target: waypoints.at(-1),
  };
}

export class DroneSim {
  constructor(map, onFrame) {
    this.map = map;
    this.onFrame = onFrame;
    this.rate = 1;
    this.view = "overview";
    this.playing = false;
    this.battery = 100;
    this.elapsed = 0;
    this.frameRequest = null;
    this.previousFrame = null;
    this.guideTime = -Infinity;
  }

  load(waypoints, corridor, { battery, target, elapsed = 0 }) {
    const track = makeTrack(waypoints, corridor);
    this.pause(false);
    this.track = track;
    this.targetId = target;
    this.elapsed = Math.min(elapsed, track.duration);
    this.heading = undefined;
    this.bank = 0;
    this.guideTime = -Infinity;
    this.battery = battery;
    this.attach();
  }

  attach() {
    this.map.flightEntity.position = this.track.property;
    this.map.flightEntity.show = this.view !== "fpv";
    this.map.flightEntity.point.show = this.view === "overview";
    this.map.viewer.scene.screenSpaceCameraController.enableInputs = this.view === "overview";
    this.guideTime = -Infinity;
    this.paint();
  }

  seek(elapsed, battery, jumped, delta = 0) {
    this.elapsed = Math.min(elapsed, this.track.duration);
    this.battery = battery;
    if (jumped) {
      this.heading = undefined;
      this.bank = 0;
      this.guideTime = -Infinity;
    }
    this.paint(delta);
  }

  flownWaypoints(frame) {
    const points = this.track.waypoints;
    if (frame.elapsed === 0) return points.slice(0, 1);
    if (frame.complete) return points;
    return [...points.slice(0, frame.leg.waypointIndex), { ...frame.coordinates, phase: frame.leg.phase }];
  }

  setRate(rate) {
    this.rate = rate;
  }

  setView(view) {
    this.view = view;
    this.guideTime = -Infinity;
    this.map.viewer.scene.screenSpaceCameraController.enableInputs = view === "overview";
    if (view === "overview") this.map.resetView();
    if (this.track) {
      this.map.flightEntity.show = view !== "fpv";
      this.map.flightEntity.point.show = view === "overview";
      this.paint();
    }
  }

  play() {
    if (this.playing || !this.track || this.elapsed >= this.track.duration) return;
    this.playing = true;
    this.previousFrame = null;
    this.paint();
    this.frameRequest = requestAnimationFrame(this.tick);
  }

  pause(update = true) {
    this.playing = false;
    if (this.frameRequest !== null) cancelAnimationFrame(this.frameRequest);
    this.frameRequest = null;
    if (update && this.track) this.paint();
  }

  tick = (now) => {
    const delta = this.previousFrame === null ? 0 : Math.min((now - this.previousFrame) / 1000, .25);
    this.previousFrame = now;
    const advanced = Math.min(delta * this.rate, this.track.duration - this.elapsed);
    this.elapsed += advanced;
    this.battery = batteryAfterFlight(this.battery, advanced);
    if (this.elapsed >= this.track.duration) this.playing = false;
    this.paint(delta);
    this.frameRequest = this.playing ? requestAnimationFrame(this.tick) : null;
  };

  snapshot() {
    const position = this.track.at(this.elapsed);
    const legIndex = this.track.legs.findIndex((leg) => this.elapsed < leg.end);
    const leg = this.track.legs[legIndex === -1 ? this.track.legs.length - 1 : legIndex];
    const coordinates = this.elapsed === 0 ? this.track.origin
      : this.elapsed >= this.track.duration ? this.track.target : geographic(position);
    const { lon, lat, alt } = coordinates;
    return {
      position, coordinates: { lon, lat, alt }, leg, legIndex, battery: this.battery,
      elapsed: this.elapsed, duration: this.track.duration, playing: this.playing,
      speed: this.playing ? leg.speed : 0, complete: legIndex === -1,
    };
  }

  paint(delta = 0) {
    const frame = this.snapshot();
    this.map.viewer.clock.currentTime = this.track.time(this.elapsed);
    const pose = this.updatePose(frame, delta);
    if (this.view !== "overview") this.pointCamera(frame, pose);
    if (this.view !== "fpv") {
      this.map.showGuides([], null);
    } else if (this.elapsed - this.guideTime >= .18) {
      this.guideTime = this.elapsed;
      this.map.showGuides(this.gates(), this.track.target, this.targetId);
    }
    this.map.viewer.scene.requestRender();
    this.onFrame(frame);
  }

  updatePose({ position, leg, legIndex, complete }, delta) {
    const ellipsoid = C.Ellipsoid.WGS84;
    const up = ellipsoid.geodeticSurfaceNormal(position, new C.Cartesian3());
    const lookLeg = leg.horizontal > 1 ? leg
      : this.track.legs.slice(Math.max(0, legIndex)).find((step) => step.horizontal > 1)
        ?? this.track.legs.findLast((step) => step.horizontal > 1);
    const local = C.Transforms.eastNorthUpToFixedFrame(position);
    const north = C.Matrix4.multiplyByPointAsVector(local, C.Cartesian3.UNIT_Y, new C.Cartesian3());
    const east = C.Matrix4.multiplyByPointAsVector(local, C.Cartesian3.UNIT_X, new C.Cartesian3());
    const preferred = lookLeg?.direction ?? north;
    const desired = Math.atan2(C.Cartesian3.dot(preferred, east), C.Cartesian3.dot(preferred, north));
    const turn = C.Math.negativePiToPi(desired - (this.heading ?? desired));
    const smooth = 1 - Math.exp(-delta * 9);
    this.heading = this.heading === undefined ? desired : this.heading + turn * smooth;
    const cruising = leg.phase === "cruise" && !complete;
    this.bank = cruising ? C.Math.lerp(this.bank, C.Math.clamp(turn * .65, -.23, .23), smooth) : 0;
    const pitch = cruising ? C.Math.clamp(Math.atan2(leg.dz, leg.horizontal), -.12, .12) : 0;
    // Cesium maps the glTF forward axis (+Z) to local +X, which points east at zero heading.
    const attitude = new C.HeadingPitchRoll(this.heading - Math.PI / 2, pitch, this.bank);
    this.map.flightEntity.orientation.setValue(C.Transforms.headingPitchRollQuaternion(position, attitude));
    const forward = unit(plus(plus(new C.Cartesian3(), north, Math.cos(this.heading)), east, Math.sin(this.heading)));
    return { up, forward };
  }

  pointCamera(frame, { up, forward }) {
    const { position, leg } = frame;
    const right = unit(cross(forward, up));
    let eye;
    let sight;
    if (this.view === "fpv") {
      const climb = Math.atan2(leg.dz, Math.max(leg.horizontal, .01));
      const pitch = Math.max(-.55, Math.min(.25, climb));
      eye = plus(plus(position, forward, NOSE_OFFSET), up, .47);
      sight = unit(plus(forward, up, Math.tan(pitch)));
    } else {
      eye = plus(plus(plus(position, forward, -6.4), up, 3.9), right, 2.8);
      const aim = plus(plus(position, forward, .65), up, .38);
      sight = unit(C.Cartesian3.subtract(aim, eye, new C.Cartesian3()));
    }
    const cameraRight = unit(cross(sight, up));
    const cameraUp = unit(cross(cameraRight, sight));
    this.map.viewer.camera.setView({ destination: eye, orientation: { direction: sight, up: cameraUp } });
    frame.pitch = this.map.viewer.camera.pitch;
    frame.heading = C.Math.toDegrees(C.Math.zeroToTwoPi(this.heading));
  }

  gates() {
    return this.track.gates
      .filter(({ second }) => second > this.elapsed + .6 && second <= this.elapsed + 14)
      .slice(0, 4).map(({ corners }) => corners);
  }

  proposeHazard(activeHazards) {
    const { coordinates: current } = this.snapshot();
    const target = this.track.target;
    for (const ahead of [7, 9, 5, 12, 15, 4, 18, 23]) {
      const time = this.elapsed + ahead;
      if (!this.track.legs.some((leg) => leg.phase === "cruise" && time >= leg.start && time < leg.end)) continue;
      const center = geographic(this.track.at(time));
      const margin = Math.min(horizontalGap(center, current), horizontalGap(center, target));
      const radius = Math.min(28, Math.floor(margin * .42));
      if (radius < 14) continue;
      if (activeHazards.some((hazard) => horizontalGap(center, {
        lon: hazard.center_lon, lat: hazard.center_lat,
      }) < hazard.radius_m + radius + 9)) continue;
      return { center_lon: center.lon, center_lat: center.lat, radius_m: radius, min_alt: 0, max_alt: 100 };
    }
    return null;
  }
}
