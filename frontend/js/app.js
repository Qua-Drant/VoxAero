import { CampusMap } from "./map3d.js?v=17";
import { DroneSim } from "./drone_sim.js?v=8";
import { FlightReplay, flightClock } from "./flight_replay.js?v=13";
import { FlightHUD } from "./ar_hud.js?v=15";
import { batteryReading, paintBattery } from "./battery_display.js";
import { AgentUI, plainDispatchText } from "./agent_ui.js?v=17";

import { VerticalProfile } from "./flight_profile.js?v=14";

const element = (id) => document.getElementById(id);
const layerToggles = [...document.querySelectorAll("[data-layer]")];
const viewButtons = [...document.querySelectorAll("[data-view]")];
const rateButtons = [...document.querySelectorAll("[data-rate]")];
const playButton = element("play-flight");
const fpvPanelButtons = [...document.querySelectorAll("button[data-fpv-panel]")];
const batteryLabel = element("charge");
const batteryMeter = element("battery-meter");
const resetButton = element("reset-flight");
const focusButton = element("focus-route");
const windButton = element("wind");
const batteryButton = element("battery");
const automaticEmergency = element("automatic-emergency");
const closeEmergency = setupEmergencyMenu();
const phaseNames = { takeoff: "垂直起飞", cruise: "巡航", landing: "降落", transition: "调整高度" };
const directions = ["北", "北偏东", "东北", "东偏北", "东", "东偏南", "东南", "南偏东", "南", "南偏西", "西南", "西偏南", "西", "西偏北", "西北", "北偏西"];
const eventNames = { wind_gust: "突发阵风 / 临时禁飞区", low_battery: "低电量强制备降" };
const format = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const ui = new AgentUI(dispatch, clearClarification);
const hud = new FlightHUD(element("hud"));

let map;
let sim;
let review;
let sceneVersion;
let mission;
let draft;
let proposal;
let pickRole;
let spatialPicks = { avoid: [] };
let busy;
const pickButtons = [...document.querySelectorAll("[data-map-pick]")];
const pickLabels = { start: "起点", end: "终点", avoid: "避让区" };
let clarificationToken;
let clarificationQuery;
let frame;
let padNames;
let zoneNames;
let clearance = null;
let lastTelemetry = -Infinity;
let profile;
const timeline = element("flight-timeline");
const seek = element("dock-seek");
const clock = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
function tickClock() { text("sim-clock", clock.format()); }
tickClock();
setInterval(tickClock, 1000);

const guide = element("user-guide");
const guideKey = "voxaero.user-guide.v1";
element("open-user-guide").addEventListener("click", () => guide.showModal());
guide.addEventListener("close", () => {
  try { localStorage.setItem(guideKey, "seen"); } catch { /* Storage can be blocked. */ }
});
let seenGuide = false;
try { seenGuide = localStorage.getItem(guideKey) === "seen"; } catch { /* Show the guide without storage. */ }
if (!seenGuide) guide.showModal();

function sampleEmergency(legs, demo) {
  if (!demo && Math.random() >= .35) return null;
  const cruise = legs.filter((leg) => leg.phase === "cruise" && leg.horizontal > 1);
  const cruiseSeconds = cruise.reduce((total, leg) => total + leg.end - leg.start, 0);
  if (cruiseSeconds < 3) return null;
  // Sample along the accumulated cruise time, leaving room to respond before landing.
  let offset = cruiseSeconds * (.2 + Math.random() * .35);
  for (const leg of cruise) {
    const length = leg.end - leg.start;
    if (offset < length) {
      return { at: leg.start + offset, type: Math.random() < .5 ? "wind_gust" : "low_battery" };
    }
    offset -= length;
  }
  return null;
}

function setupEmergencyMenu() {
  const fallback = element("emergency-menu");
  const panel = element("emergency-actions");
  if (!("popover" in HTMLElement.prototype)) return () => { fallback.open = false; };

  const trigger = element("emergency-trigger");
  panel.setAttribute("popover", "auto");
  fallback.replaceWith(panel);
  trigger.hidden = false;
  return () => {
    const restoreFocus = panel.contains(document.activeElement);
    panel.hidePopover();
    if (restoreFocus) trigger.focus();
  };
}

function text(id, value) {
  const target = element(id);
  if (target.textContent !== value) target.textContent = value;
}

async function api(path, payload, exchange) {
  const options = payload === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  };
  let response;
  let raw;
  try {
    response = await fetch(path, options);
    raw = await response.text();
  } catch (error) {
    const detail = response ? "读取回复时连接中断。" : "没有收到调度回复，请检查本机服务。";
    if (response) exchange?.receive(response, detail);
    else exchange?.fail(detail);
    throw new Error(detail, { cause: error });
  }
  let body;
  try {
    body = JSON.parse(raw);
  } catch (error) {
    exchange?.receive(response, raw);
    throw new Error("服务返回了无法解析的响应正文。", { cause: error });
  }
  exchange?.receive(response, body);
  if (!response.ok) {
    const error = new Error(typeof body?.detail === "string" ? body.detail : "请求失败，请检查参数后重试。");
    error.steps = body?.thought_steps;
    error.dispatchNotice = body?.status ? body : null;
    throw error;
  }
  return body;
}

function updateControls() {
  const planned = Boolean(mission);
  const replaying = Boolean(review?.active);
  const locked = Boolean(busy) || replaying;
  const cruising = planned && Boolean(frame) && frame.elapsed > 0 && !frame.complete
    && frame.leg.phase === "cruise" && frame.coordinates.alt >= 30;
  const canPlan = Boolean(sim) && !locked && !isInFlight();
  ui.setAvailable(canPlan, Boolean(draft || proposal));
  for (const button of pickButtons) button.disabled = !canPlan;
  element("clear-spatial").disabled = !canPlan;
  playButton.disabled = !replaying && (!planned || locked || Boolean(draft || proposal) || frame?.complete || mission?.hold);
  resetButton.disabled = !planned || locked || Boolean(draft || proposal);
  focusButton.disabled = !sim;
  windButton.disabled = !cruising || locked || mission.hazards.length >= 20;
  batteryButton.disabled = !cruising || locked || mission.diverted;
  review?.setAvailable(review.record?.sceneVersion === sceneVersion
    && !busy && !draft && !proposal && (!isInFlight() || mission?.hold));
  for (const button of rateButtons) {
    button.disabled = !planned || Boolean(busy);
    button.setAttribute("aria-pressed", Number(button.dataset.rate) === (replaying ? review.rate : sim?.rate));
  }
  for (const button of viewButtons) {
    button.disabled = !sim || ((!planned || draft || proposal) && button.dataset.view !== "overview");
    button.setAttribute("aria-pressed", button.dataset.view === (review?.sim?.view ?? sim?.view ?? "overview"));
  }
  const playing = replaying ? review.playing : frame?.playing;
  text("play-label", replaying ? (playing ? "暂停回放" : "播放回放")
    : frame?.complete ? "已完成" : playing ? "暂停"
      : frame?.elapsed || mission?.rerouted ? "继续" : "开始");
  element("play-icon").setAttribute("href", playing ? "#icon-pause" : "#icon-play");
  seek.hidden = !replaying;
  text("emergency-hint", replaying ? "回放中。退出回放后可操作当前任务。"
    : busy === "replan" ? "仿真已暂停，正在计算新航线。"
    : cruising ? "应急事件将从当前飞行位置重新规划。"
      : "进入巡航后可手动模拟应急。");
}

function isInFlight() {
  return Boolean(frame && !frame.onGround);
}

function flightState() {
  if (review?.active) return "历史回放";
  if (busy === "dispatch") return "调度中";
  if (busy === "replan") return "重规划中";
  if (proposal) return proposal.result.choice_type === "routes" ? "候选航线 · 待选择" : "候选起降点 · 待选择";
  if (draft) return "候选航线 · 待确认";
  if (mission?.hold) return "应急悬停";
  if (!frame) return "待命";
  if (frame.complete) return mission.diverted ? "已备降 · 停机" : "已抵达 · 停机";
  if (!frame.playing) {
    if (mission.rerouted) return mission.diverted ? "备降待续" : "绕飞待续";
    return frame.elapsed > 0 ? "已暂停" : "待起飞";
  }
  if (mission.rerouted && frame.leg.phase === "cruise") return mission.diverted ? "备降途中" : "绕飞途中";
  if (frame.leg.phase === "takeoff" && frame.coordinates.alt >= 30) return "爬升至航层";
  return phaseNames[frame.leg.phase];
}

function refreshFlightState() {
  text("flight-state", flightState());
  const moving = frame?.playing && !frame.onGround && !review?.active;
  const tone = busy === "replan" || mission?.hold || (moving && mission?.rerouted) ? "warn"
    : busy === "dispatch" ? "vector" : moving ? frame.instrumentTone : "neutral";
  element("flight-state").dataset.tone = tone;
  if (!review?.active && mission && !draft && !proposal && busy !== "dispatch") {
    const state = busy === "replan" ? "应急 · 改航中" : mission.hold ? "应急 · 悬停"
      : frame?.complete ? (mission.diverted ? "已备降" : "任务完成")
        : frame?.playing ? "已确认 · 执行中" : frame?.elapsed ? "任务已暂停" : "已确认 · 待起飞";
    ui.setMissionState(state, tone);
  }
  updateControls();
}

function onFrame(next) {
  const previous = frame;
  frame = next;
  next.onGround = next.complete || (next.elapsed === 0 && !next.playing && !mission.rerouted);
  const now = performance.now();
  if (!next.playing || next.playing !== previous?.playing || next.leg !== previous?.leg
      || now - lastTelemetry >= 120 || next.elapsed < previous?.elapsed) {
    lastTelemetry = now;
    clearance = next.onGround ? null : map.clearance(next.coordinates, mission.avoid);
  }
  recordVisits(next.elapsed);
  paintTelemetry(next, sim);
  setFlightProgress(mission.elapsedBeforeSegment + next.elapsed, mission.elapsedBeforeSegment + next.duration, next.onGround ? null : next.leg.phase);
  if (next.complete && !previous?.complete) {
    const outcome = mission.diverted ? "已备降 · 原任务终止"
      : mission.completedVisits ? `任务完成 · 已飞越 ${mission.completedVisits} 处` : "任务完成";
    saveReview(next, outcome, mission.diverted ? "已备降" : "已抵达");
    ui.status(outcome + "。");
  }
  if (!previous || previous.playing !== next.playing || previous.leg !== next.leg || previous.complete !== next.complete
      || (next.leg.phase === "takeoff" && (previous.coordinates.alt < 30) !== (next.coordinates.alt < 30))) {
    refreshFlightState();
  }
  if (next.elapsed > previous?.elapsed && next.playing && next.leg.phase === "cruise" && next.leg.horizontal > 1
      && automaticEmergency.checked && !busy && !review?.active
      && mission.automaticEvent && next.elapsed >= mission.automaticEvent.at) {
    void replan(mission.automaticEvent.type, { automatic: true });
  }
}

function recordVisits(elapsed) {
  while (mission.nextVisit < mission.segmentVisits.length) {
    const visit = mission.segmentVisits[mission.nextVisit];
    if (elapsed < visit.second) break;
    mission.nextVisit += 1;
    mission.completedVisits += 1;
    mission.events.push({
      at: mission.elapsedBeforeSegment + visit.second, name: `飞越 ${visit.name}`,
      position: visit.position, segmentIndex: mission.segments.length - 1, visit: true,
    });
    ui.setVisitProgress(mission.completedVisits);
  }
}

function measurement(id, value, unit) {
  const target = element(id);
  const number = value == null ? "—" : format.format(value);
  if (target.dataset.value === number) return;
  target.dataset.value = number;
  if (value == null) {
    target.textContent = number;
    return;
  }
  const label = document.createElement("span");
  label.className = "unit";
  label.textContent = unit;
  target.replaceChildren(number, " ", label);
}

function paintTelemetry(next, currentSim) {
  const { leg, coordinates, complete, battery } = next;
  const moving = !next.onGround && (next.playing || review?.active);
  const seconds = leg.end - leg.start;
  const heading = Math.round((Cesium.Math.toDegrees(currentSim.heading ?? 0) + 360) % 360) % 360;
  const vertical = moving ? leg.dz / seconds : 0;
  next.agl = map.altitudeAGL(coordinates);
  next.altitudeText = format.format(next.agl) + " m";
  next.groundSpeed = moving ? leg.horizontal / seconds : 0;
  next.charge = batteryReading(battery);
  const lowClearance = !next.onGround && clearance !== null && clearance < 12;
  const flightActive = next.playing || review?.playing;
  const warning = lowClearance || (!review?.active && mission?.hold);
  next.instrumentTone = next.onGround ? "neutral" : warning ? "warn"
    : !flightActive ? "neutral" : leg.phase === "cruise" ? "normal" : "vector";
  element("altitude").closest(".flight-board").dataset.tone = next.instrumentTone;
  measurement("altitude", next.agl, "m");
  measurement("speed", next.groundSpeed, "m/s");
  paintBattery(batteryLabel, batteryMeter, next.charge);
  measurement("clearance", next.onGround ? null : clearance, "m");
  element("clearance").dataset.tone = lowClearance ? "warn" : "neutral";
  text("clearance-status", next.onGround ? "地面停机" : clearance === null ? "等待净空数据"
    : lowClearance ? "净空偏低" : "距最近障碍物");
  element("clearance-status").dataset.tone = next.onGround || clearance === null ? "neutral" : next.instrumentTone;
  text("vertical-speed", vertical > .05 ? `↑ 爬升 +${format.format(vertical)} m/s`
    : vertical < -.05 ? `↓ 下降 ${format.format(vertical)} m/s` : "垂直速度 0.0 m/s");
  text("flight-heading", `航向 ${String(heading).padStart(3, "0")}° ${directions[Math.round(heading / 22.5) % 16]}`);
  const remaining = complete ? 0 : currentSim.track.legs.slice(next.legIndex).reduce((distance, step) => distance
    + Math.hypot(step.horizontal, step.dz) * (step.end - Math.max(step.start, next.elapsed)) / (step.end - step.start), 0);
  text("distance", format.format(remaining) + " m");
  text("duration", format.format(next.duration - next.elapsed) + " s");
  map.updateAircraftTag(next.altitudeText, next.instrumentTone, next.agl);
  if (profile.track === currentSim.track) profile.paint(next);
  const emergencyPhase = !review?.active && !next.onGround && (busy === "replan" || mission?.hold || mission?.rerouted)
    ? flightState() : null;
  hud.update(currentSim.view === "fpv", next, next.onGround ? null : clearance, emergencyPhase);
}

const landingStart = (track) => track.legs.find((leg) => leg.phase === "landing")?.start ?? track.duration;

function setTimeline(climb, landing, total) {
  timeline.style.setProperty("--climb-end", Math.min(100, total ? 100 * climb / total : 0) + "%");
  timeline.style.setProperty("--cruise-end", Math.min(100, total ? 100 * landing / total : 0) + "%");
}

function setMissionTimeline() {
  setTimeline(mission.climbEnds, mission.elapsedBeforeSegment + landingStart(sim.track),
    mission.elapsedBeforeSegment + sim.track.duration);
}

function setFlightProgress(elapsed, duration, phase) {
  const fraction = duration ? elapsed / duration : 0;
  element("flight-progress").value = fraction;
  seek.value = fraction;
  seek.setAttribute("aria-valuetext", flightClock(elapsed));
  timeline.style.setProperty("--progress", fraction * 100 + "%");
  text("flight-elapsed", flightClock(elapsed));
  text("flight-total", flightClock(duration));
  for (const label of timeline.querySelectorAll("[data-phase]")) {
    label.toggleAttribute("data-active", Boolean(phase) && label.dataset.phase === (phase === "takeoff" ? "climb" : phase === "landing" ? "landing" : "cruise"));
  }
}

function siteName(id) {
  const sites = review?.active ? review.record.taskSites : mission?.original.task_sites;
  return padNames.get(id) ?? sites?.find((site) => site.id === id)?.name ?? id;
}

function targetLabel(id) {
  return id + " · " + siteName(id);
}

function showRouteData(result, target) {
  const saving = result.kpis.time_saving_percent;
  text("saving-label", saving < 0 ? "较地面多耗时" : "较地面节省");
  text("saving", saving == null ? "—" : format.format(Math.abs(saving)) + "%");
  element("saving").toggleAttribute("data-positive", saving > 0);
  const start = mission.original.intent.start_id;
  const short = (id) => {
    const name = siteName(id);
    return name !== id ? id + " " + name.replace(/^信息学部/, "").replace(/(?:起降坪|停机坪|巡检点|应急备降坪)$/, "") : id;
  };
  text("mission-target", short(start) + " → " + short(target));
  element("mission-target").title = targetLabel(start) + " → " + targetLabel(target);
}

function installRoute(result, target, battery = 100, rerouted = false) {
  mission.target = target;
  mission.current = result;
  mission.rerouted = rerouted;
  mission.segments.push({ result, target, battery, start: mission.elapsedBeforeSegment, volumes: [...mission.volumes] });
  mission.nextVisit = 0;
  mission.segmentVisits = [];
  if (!rerouted) {
    mission.completedVisits = 0;
    mission.events = [{
      at: 0, name: "起飞", position: result.waypoints[0], segmentIndex: 0,
    }];
  }
  lastTelemetry = -Infinity;
  map.setHazards(mission.volumes);
  map.setRouteTargets(mission.original.intent.start_id, target);
  map.drawRoute(result.waypoints, rerouted, false, result.route_visits);
  showRouteData(result, target);
  ui.updatePlan(result, target, siteName(target));
  ui.setVisitProgress(mission.completedVisits, mission.diverted);
  sim.load(result.waypoints, result.corridor, { battery, target });
  mission.segmentVisits = (result.route_visits ?? []).flatMap(visit => {
    const leg = sim.track.legs.find(step => step.waypointIndex === visit.waypoint_index);
    return leg ? [{ ...visit, second: leg.end, position: result.waypoints[visit.waypoint_index] }] : [];
  });
  profile.setTrack(sim.track, result.corridor, rerouted ? "改航点" : mission.original.intent.start_id, target);
  profile.paint(frame);
  if (!rerouted) {
    mission.climbEnds = sim.track.legs.find((leg) => leg.phase !== "takeoff")?.start ?? sim.track.duration;
    mission.automaticEvent = sampleEmergency(sim.track.legs, mission.demo);
  }
  setMissionTimeline();
}

function saveReview(next, outcome, endEvent) {
  const segmentIndex = mission.segments.length - 1;
  const duration = mission.elapsedBeforeSegment + next.elapsed;
  const events = [...mission.events];
  if (endEvent) events.push({ at: duration, name: endEvent, position: next.coordinates, segmentIndex });
  review.setRecord({
    sceneVersion, duration, events, avoid: mission.avoid, battery: next.battery, startId: mission.original.intent.start_id,
    taskSites: mission.original.task_sites,
    climbEnds: mission.climbEnds, landingStarts: mission.elapsedBeforeSegment + landingStart(sim.track),
    outcome: `${mission.original.intent.start_id} → ${mission.target} · ${outcome}`,
    segments: [...mission.segments.slice(0, -1), { ...mission.segments[segmentIndex], flown: sim.flownWaypoints(next) }],
  });
}

function paintReplay(next, record, segment) {
  const at = segment.start + next.elapsed;
  next.onGround = next.complete || at === 0;
  next.speed = next.onGround || at >= record.duration ? 0 : next.leg.speed;
  const now = performance.now();
  if (!review.playing || now - lastTelemetry >= 120) {
    lastTelemetry = now;
    clearance = next.onGround ? null : map.clearance(next.coordinates, record.avoid);
    if (profile.track !== review.sim.track) {
      profile.setTrack(review.sim.track, segment.result.corridor, segment.start ? "改航点" : record.startId, segment.target);
    }
  }
  ui.setVisitProgress(record.events.filter(event => event.visit && event.at <= at).length);
  paintTelemetry(next, review.sim);
  setFlightProgress(at, record.duration, next.onGround ? null : next.leg.phase);
}

function setReplayMode(active) {
  document.body.toggleAttribute("data-replay", active);
  lastTelemetry = -Infinity;
  if (active) {
    ui.setMissionState("历史回放");
    map.setTaskSites(review.record.taskSites);
    setTimeline(review.record.climbEnds, review.record.landingStarts, review.record.duration);
    review.sim.view = sim.view;
    review.rate = sim.rate;
  } else {
    document.body.dataset.view = sim.view;
    if (sim.view === "overview") map.resetView();
    map.setTaskSites(mission.original.task_sites);
    map.setHazards(mission.volumes);
    map.drawRoute(mission.current.waypoints, mission.rerouted, false, mission.current.route_visits);
    ui.setVisitProgress(mission.completedVisits, mission.diverted);
    map.setRouteTargets(mission.original.intent.start_id, mission.target);
    showRouteData(mission.current, mission.target);
    profile.setTrack(sim.track, mission.current.corridor, mission.rerouted ? "改航点" : mission.original.intent.start_id, mission.target);
    setMissionTimeline();
    sim.attach();
  }
  refreshFlightState();
}

function clearClarification() {
  if (!clarificationToken) return;
  clarificationToken = undefined;
  clarificationQuery = undefined;
  ui.setClarification(false);
  ui.status("请输入新的完整调度指令。");
}

function showDispatchNotice(result, query) {
  if (result.status === "needs_clarification") {
    clarificationToken = result.clarification_token;
    clarificationQuery = query;
    ui.setClarification(true);
    ui.setInput("");
  } else if (result.status === "constraint_conflict" || result.status === "clarification_closed") {
    clearClarification();
  }
  const conflict = result.status === "constraint_conflict";
  if (result.mode) ui.setMode(result.mode);
  const message = result.status === "airspace_report" ? "空域核验已完成。" : plainDispatchText(result.detail);
  ui.status(message, conflict);
}

function discardDraft() {
  if (!draft && !proposal) return;
  draft = proposal = null;
  ui.restoreMission();
  map.clearPadFocus();
  map.setTaskSites(mission?.original.task_sites);
  map.setHazards(mission?.volumes ?? []);
  map.setRouteTargets(mission?.original.intent.start_id, mission?.target);
  map.drawRoute(mission?.current.waypoints ?? [], mission?.rerouted, false, mission?.current.route_visits);
  renderSpatialPicks();
}

function startMission(result, demo = false) {
  clearSpatialPicks();
  mission = {
    original: result, hazards: [], volumes: [...(result.task_zones ?? [])],
    avoid: new Set(result.intent.avoid_zone_ids), diverted: false, hold: false, elapsedBeforeSegment: 0,
    segments: [], events: [], demo, automaticEvent: null,
  };
  map.clearPadFocus();
  map.setTaskSites(result.task_sites);
  installRoute(result, result.intent.end_id);
  if (sim.view === "overview") focusTaskRoute();
  sim.play();
}

function confirmDraft() {
  if (busy || !draft || isInFlight()) return;
  const { result, demo } = draft;
  draft = null;
  ui.acceptPlan();
  startMission(result, demo);
  announceFlightStart();
  playButton.focus({ preventScroll: true });
}

function announceFlightStart() {
  ui.status(mission.demo && mission.automaticEvent && automaticEmergency.checked
    ? "应急演示已开始，巡航时会随机触发一次应急。"
    : "已确认任务，仿真运行中。");
}

function editDraft() {
  if (!draft || busy) return;
  const { query } = draft;
  discardDraft();
  ui.setInput(query);
  ui.input.focus();
  ui.status("修改调度指令后重新规划。");
  refreshFlightState();
}

async function dispatch(query, { autoPlay = false, newTask = false } = {}) {
  if (busy || !sim || review?.active || isInFlight()) return;
  stopMapPick();
  if (newTask) {
    clearClarification();
    clearSpatialPicks();
  }
  const originalQuery = clarificationQuery ?? query;
  discardDraft();
  busy = "dispatch";
  refreshFlightState();
  ui.status("已发送指令，正在等候回复…");
  const path = "/api/agent/dispatch";
  const payload = { query, scene_version: sceneVersion, clarification_token: clarificationToken };
  if (spatialPicks.start || spatialPicks.end || spatialPicks.avoid.length) payload.spatial = spatialPicks;
  const exchange = ui.beginExchange();
  try {
    const result = await api(path, payload, exchange);
    ui.status("已收到回复，正在逐步说明处理过程…");
    await ui.revealSteps(exchange, result.thought_steps, zoneNames);
    if (result.status === "choose_endpoints") {
      presentChoices(result, query);
      return;
    }
    if (result.status !== "planned") {
      showDispatchNotice(result, query);
      return;
    }
    let editQuery = query;
    if (originalQuery !== query && !result.model_called) {
      const { start_id, end_id } = result.intent;
      const supplemented = `从 ${start_id} 到 ${end_id}。${originalQuery}`;
      editQuery = supplemented.length <= ui.input.maxLength ? supplemented : originalQuery;
    }
    presentDraft(result, editQuery, autoPlay);
  } catch (error) {
    discardDraft();
    await ui.revealSteps(exchange, error.steps);
    if (error.dispatchNotice) showDispatchNotice(error.dispatchNotice, query);
    else {
      const message = plainDispatchText(error.message);
      ui.status(message, true);
    }
  } finally {
    busy = null;
    refreshFlightState();
  }
}

function presentDraft(result, query, autoPlay = false) {
  if (result.scene_version !== sceneVersion || !result.kpis.compliant) {
    ui.status("该航线未通过当前场景校验，请刷新后重新规划。", true);
    return;
  }
  clearClarification();
  ui.setMode(result.mode);
  const actions = { confirm: confirmDraft, edit: editDraft, locate: (id) => map.focusPad(id) };
  if (autoPlay && !(result.model_fallback && mission)) {
    ui.showPlan(result, zoneNames, actions, true);
    startMission(result, true);
    ui.setInput("");
    announceFlightStart();
    return;
  }
  proposal = null;
  draft = { result, query, demo: autoPlay };
  element("spatial-input").open = false;
  setView("overview");
  renderSpatialPicks();
  map.setTaskSites(result.task_sites);
  map.setHazards(result.task_zones ?? []);
  map.setRouteTargets(result.intent.start_id, result.intent.end_id);
  map.drawRoute(result.waypoints, false, true, result.route_visits);
  ui.showPlan(result, zoneNames, actions);
  ui.setInput("");
  ui.status("航线待确认。核对任务后可开始仿真。");
}

function presentChoices(result, query) {
  if (result.scene_version !== sceneVersion || !result.options.length) {
    ui.status("候选与当前场景不匹配，请刷新后重新规划。", true);
    return;
  }
  clearClarification();
  ui.setMode(result.mode);
  proposal = { result, query };
  element("spatial-input").open = false;
  setView("overview");
  renderSpatialPicks();
  ui.showChoices(result, {
    preview: (option) => {
      proposal.option = option;
      map.clearPadFocus();
      map.setTaskSites([option.start, option.end]);
      map.setHazards(result.task_zones ?? []);
      map.setRouteTargets(option.start.id, option.end.id);
      map.drawRoute(option.waypoints, false, true, option.route_visits);
      focusTaskRoute();
    },
    confirm: confirmCandidate,
    cancel: () => {
      if (busy) return;
      discardDraft();
      ui.input.focus({ preventScroll: true });
      ui.status("调整地点或选点后可重新规划。");
      refreshFlightState();
    },
  });
  ui.status(result.detail);
}

async function confirmCandidate(optionId) {
  if (busy || !proposal || isInFlight() || review?.active) return;
  const { result: choices, query } = proposal;
  busy = "dispatch";
  refreshFlightState();
  const exchange = ui.beginExchange();
  ui.status("正在复核所选起降点…");
  try {
    const result = await api("/api/agent/confirm-candidate", {
      candidate_token: choices.candidate_token, option_id: optionId, scene_version: sceneVersion,
    }, exchange);
    void ui.revealSteps(exchange, result.thought_steps, zoneNames);
    if (result.status === "planned") presentDraft(result, query);
    else showDispatchNotice(result, query);
  } catch (error) {
    await ui.revealSteps(exchange, error.steps, zoneNames);
    ui.status(plainDispatchText(error.message), true);
  } finally {
    busy = null;
    refreshFlightState();
  }
}

function renderSpatialPicks() {
  const markers = ["start", "end"].flatMap((role) => spatialPicks[role]
    ? [{ ...spatialPicks[role], name: pickLabels[role] }] : []);
  for (const [index, area] of spatialPicks.avoid.entries()) {
    markers.push({ ...area, name: `避让区 ${index + 1}` });
  }
  const list = markers.map(({ lon, lat, name, radius_m }) => `${name} ${lon.toFixed(5)}, ${lat.toFixed(5)}`
    + (radius_m ? ` · 半径 ${radius_m} 米` : ""));
  text("spatial-count", markers.length ? `已选 ${markers.length} 处` : "");
  text("spatial-picks", list.join("；") || "也可在指令中填写 [经度, 纬度]。");
  element("clear-spatial").hidden = !markers.length;
  map?.showPickMarkers(draft || proposal ? [] : markers);
}

function stopMapPick() {
  pickRole = undefined;
  map?.pickLocation(null);
  for (const button of pickButtons) button.setAttribute("aria-pressed", "false");
}

function clearSpatialPicks() {
  stopMapPick();
  spatialPicks = { avoid: [] };
  renderSpatialPicks();
}

function beginMapPick(role) {
  if (!sim || busy || review?.active || isInFlight()) return;
  if (pickRole === role) {
    stopMapPick();
    ui.status("已取消地图点选。");
    return;
  }
  const radius = element("avoid-radius");
  const radiusMeters = radius.valueAsNumber;
  if (role === "avoid" && (!radius.reportValidity() || !Number.isFinite(radiusMeters))) {
    ui.status("请填写 5 至 250 米的避让半径。", true);
    return;
  }
  if (role === "avoid" && spatialPicks.avoid.length >= 3) {
    ui.status("每个任务最多可添加三处避让区。", true);
    return;
  }
  discardDraft();
  clearClarification();
  ui.showGuide(false);
  setView("overview");
  pickRole = role;
  for (const button of pickButtons) button.setAttribute("aria-pressed", String(button.dataset.mapPick === role));
  ui.status(`在三维地图上点击${pickLabels[role]}，按退出键取消。`);
  map.pickLocation((point) => {
    if (!pickRole) return;
    const [west, south, east, north] = map.bounds;
    if (point.lon < west || point.lon > east || point.lat < south || point.lat > north) {
      ui.status("该位置在场景范围外，请在校园场景内点选。", true);
      return;
    }
    if (role === "avoid") spatialPicks.avoid.push({ ...point, radius_m: radiusMeters });
    else spatialPicks[role] = point;
    stopMapPick();
    renderSpatialPicks();
    ui.status("已记录地图位置。输入任务并规划航线后校验。");
    ui.input.focus({ preventScroll: true });
  });
}

async function replan(type, { automatic = false } = {}) {
  const button = type === "wind_gust" ? windButton : batteryButton;
  if (button.disabled || busy || !mission) return;
  const hazard = type === "wind_gust" ? sim.proposeHazard(mission.hazards) : null;
  if (type === "wind_gust" && !hazard) {
    if (automatic && !batteryButton.disabled) {
      type = "low_battery";
    } else {
      ui.status("前方巡航航段不足，无法在避开当前位置与目的地的情况下放置禁飞区。", true);
      return;
    }
  }

  const incident = automatic ? `自动应急 · ${eventNames[type]}` : eventNames[type];
  mission.automaticEvent = null;

  const before = sim.snapshot();
  const constraints = mission.original.intent.constraints;
  const elapsed = mission.elapsedBeforeSegment + before.elapsed;
  const segmentIndex = mission.segments.length - 1;
  const event = { at: elapsed, position: before.coordinates };
  busy = "replan";
  if (!automatic) closeEmergency();
  if (type === "low_battery") sim.battery = Math.min(sim.battery, 14);
  sim.pause();
  refreshFlightState();
  ui.status(`${incident}，正在从当前位置规划新航线…`);
  const path = "/api/agent/replan";
  const payload = {
    current_pos: before.coordinates,
    scene_version: sceneVersion,
    target_id: mission.target,
    mission_token: mission.original.mission_token,
    mission_type: mission.current.corridor,
    emergency_type: type,
    ...(hazard && { dynamic_hazard: hazard }),
    active_hazards: mission.hazards,
    avoid_zone_ids: [...mission.avoid],
    cruise_limits: constraints.cruise,
    completed_visits: mission.diverted ? 0 : mission.completedVisits,
  };
  const exchange = ui.beginExchange();
  try {
    const result = await api(path, payload, exchange);
    if (result.scene_version !== sceneVersion) throw new Error("空域场景已变更，请刷新后重新调度。");
    if (result.temporary_nofly_zone && hazard) {
      mission.hazards.push(hazard);
      mission.volumes.push(result.temporary_nofly_zone);
    }
    mission.diverted ||= type === "low_battery";
    mission.elapsedBeforeSegment = elapsed;
    mission.hold = false;
    mission.segments[segmentIndex].flown = sim.flownWaypoints(before);
    installRoute(result, result.target_id, sim.battery, true);
    const action = type === "low_battery" ? "备降" : "绕飞";
    const detail = hazard ? `临时禁飞区半径 ${format.format(hazard.radius_m)} 米`
      : `改降 ${targetLabel(result.target_id)}`;
    mission.events.push({
      ...event, segmentIndex: segmentIndex + 1, name: `${incident} · ${action}获准`, compare: true,
      detail,
    });
    ui.status(`${incident}，${action}航线已接入。`);
    if (before.playing) sim.play();
    void ui.revealSteps(exchange, result.thought_steps, zoneNames);
  } catch (error) {
    const detail = plainDispatchText(error.message);
    mission.hold = true;
    mission.events.push({ ...event, segmentIndex, name: `${incident} · 仿真悬停`,
      detail, battery: sim.battery });
    saveReview(sim.snapshot(), "仿真悬停 · 应急规划未成功");
    ui.status(`${incident}，已悬停 · ${detail}`, true);
    void ui.revealSteps(exchange, error.steps);
  } finally {
    busy = null;
    refreshFlightState();
  }
}

async function launch() {
  try {
    const [data] = await Promise.all([
      api("/api/layers/all"),
      document.fonts.load('600 12px "Vox Mono"', "VP01").catch(() => []),
      document.fonts.load('600 12px "Vox CJK"', "降落靶标").catch(() => []),
    ]);
    sceneVersion = data.scene_version;
    map = new CampusMap("map", data.bounds, data.terrain);
    await map.load(data);
    padNames = new Map(data.vertiports.features.map(({ properties }) => [properties.id, properties.name]));
    zoneNames = new Map(data.nofly_zones.features.map(({ properties }) => [properties.id, properties.name]));
    profile = new VerticalProfile(map);
    sim = new DroneSim(map, onFrame);
    review = new FlightReplay(map, {
      onFrame: paintReplay,
      onSegment: ({ result, target }) => {
        lastTelemetry = -Infinity;
        map.setRouteTargets(review.record.startId, target);
        showRouteData(result, target);
      },
      onMode: setReplayMode, onOverview: () => setView("overview"), onPlayback: updateControls,
    }, seek);
    for (const toggle of layerToggles) {
      toggle.disabled = toggle.dataset.layer === "roads" && !data.roads;
      toggle.addEventListener("change", () => {
        map.showLayer(toggle.dataset.layer, toggle.checked);
        updateLayerCount();
      });
    }
    const heightDatum = data.terrain ? " · 地形高程" : "";
    text("scene-status", data.buildings.features.length + " 建筑 · " + padNames.size + " 起降坪" + heightDatum);
    text("pads-label", "起降坪 ×" + padNames.size);
    text("sim-live", "仿真在线");
    element("system-status").dataset.ready = "";
    element("north").disabled = false;
    updateLayerCount();
    bindOrientation();
    element("reset-view").disabled = false;
    refreshFlightState();
  } catch (error) {
    text("scene-status", "地图初始化失败 · " + error.message);
    text("sim-live", "仿真离线");
    element("system-status").dataset.tone = "error";
    console.error("VoxAero scene initialization failed", error);
    element("scene-status").dataset.error = "true";
  }
}

function updateLayerCount() {
  text("layer-count", layerToggles.filter(({ checked }) => checked).length + " / " + layerToggles.length);
}

function bindOrientation() {
  const C = Cesium;
  const { camera, scene, canvas } = map.viewer;
  let lastUpdate = -Infinity;
  function update() {
    const now = performance.now();
    if (now - lastUpdate < 125) return;
    lastUpdate = now;
    const heading = C.Math.toDegrees(camera.heading);
    const pitch = Math.round(C.Math.toDegrees(camera.pitch));
    element("north-needle").style.rotate = -heading + "deg";
    text("camera-angle", `航向 ${String(Math.round(heading) % 360).padStart(3, "0")}° · 俯仰 ${pitch}°`);
    const { clientWidth: width, clientHeight: height } = canvas;
    const pick = (x) => {
      const ray = camera.getPickRay(new C.Cartesian2(x, height * .54));
      return ray && scene.globe.pick(ray, scene);
    };
    const middle = pick(width / 2);
    const right = pick(width / 2 + 65);
    const place = C.Cartographic.fromCartesian(middle ?? camera.positionWC);
    if (place) {
      const lat = C.Math.toDegrees(place.latitude);
      const lon = C.Math.toDegrees(place.longitude);
      text("geo-coordinate", `${lat < 0 ? "南纬" : "北纬"} ${Math.abs(lat).toFixed(4)}° · ${lon < 0 ? "西经" : "东经"} ${Math.abs(lon).toFixed(4)}°`);
    }
    if (middle && right) {
      element("scale-rule").hidden = false;
      const metersPerPixel = C.Cartesian3.distance(middle, right) / 65;
      const target = metersPerPixel * 96;
      if (target > 0) {
        const scale = 10 ** Math.floor(Math.log10(target));
        const nice = [5, 2, 1].find((value) => value * scale <= target) * scale;
        element("scale-rule").style.width = nice / metersPerPixel + "px";
        text("scale-value", nice + " m");
      }
    } else {
      element("scale-rule").hidden = true;
      text("scale-value", "—");
    }
  }
  camera.percentageChanged = .003;
  scene.postRender.addEventListener(update);
  update();
  element("north").addEventListener("click", () => {
    const destination = C.Cartesian3.clone(camera.positionWC);
    const pitch = camera.pitch;
    if ((review?.sim ?? sim).view !== "overview") setView("overview");
    camera.cancelFlight();
    camera.setView({ destination, orientation: { heading: 0, pitch, roll: 0 } });
    scene.requestRender();
  });
}

function setFpvPanel(panel) {
  if (panel) document.body.dataset.fpvPanel = panel;
  else delete document.body.dataset.fpvPanel;
  for (const button of fpvPanelButtons) button.setAttribute("aria-expanded", String(button.dataset.fpvPanel === panel));
}

function setView(view) {
  if (document.body.dataset.view !== view) setFpvPanel(null);
  document.body.dataset.view = view;
  map?.viewer.camera.cancelFlight();
  (review?.sim ?? sim)?.setView(view);
  updateControls();
}

function focusTaskRoute() {
  if (!sim) return;
  const currentSim = review?.sim ?? sim;
  const waypoints = review?.active
    ? review.record.segments.slice(0, review.segmentIndex + 1).flatMap(({ flown }) => flown)
    : proposal?.option?.waypoints ?? draft?.result.waypoints ?? mission?.current.waypoints;
  if (currentSim.view !== "overview") setView("overview");
  if (waypoints?.length) map.focusRoute(waypoints, document.querySelector(".map-overlay").clientWidth);
  else map.resetView();
}

element("reset-view").addEventListener("click", () => setView("overview"));
focusButton.addEventListener("click", focusTaskRoute);
seek.addEventListener("input", () => {
  if (!review?.active) return;
  review.pause();
  review.seek(seek.valueAsNumber * review.record.duration);
});
for (const button of fpvPanelButtons) {
  button.addEventListener("click", () => {
    const panel = button.dataset.fpvPanel;
    setFpvPanel(document.body.dataset.fpvPanel === panel ? null : panel);
  });
}
for (const button of viewButtons) button.addEventListener("click", () => setView(button.dataset.view));
for (const button of rateButtons) {
  button.addEventListener("click", () => {
    const rate = Number(button.dataset.rate);
    if (review.active) review.rate = rate;
    else sim.setRate(rate);
    updateControls();
  });
}
playButton.addEventListener("click", () => {
  if (review?.active) {
    if (review.playing) review.pause();
    else review.resume();
    return;
  }
  if (sim.playing) {
    sim.pause();
    ui.status("仿真已暂停。");
  } else {
    sim.play();
    ui.status("仿真飞行中。");
  }
});
resetButton.addEventListener("click", () => {
  if (isInFlight() && !mission.hold) saveReview(frame, "任务中止 · 已重置仿真", "重置飞行");
  mission.hazards = [];
  mission.volumes = [...(mission.original.task_zones ?? [])];
  mission.diverted = false;
  mission.hold = false;
  mission.elapsedBeforeSegment = 0;
  mission.segments = [];
  installRoute(mission.original, mission.original.intent.end_id);
  ui.status("已返回起点。");
  refreshFlightState();
});
for (const button of pickButtons) button.addEventListener("click", () => beginMapPick(button.dataset.mapPick));
element("clear-spatial").addEventListener("click", () => {
  discardDraft();
  clearSpatialPicks();
  refreshFlightState();
  ui.status("已清除地图选点和任务避让区。");
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && pickRole && !guide.open) {
    stopMapPick();
    ui.status("已取消地图点选。");
  }
});
windButton.addEventListener("click", () => replan("wind_gust"));
batteryButton.addEventListener("click", () => replan("low_battery"));
launch();
api("/api/health").then(({ llm_mode }) => ui.setMode(llm_mode)).catch(() => ui.setMode(null));
