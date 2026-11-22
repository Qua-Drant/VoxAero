import { CampusMap } from "./map3d.js?v=7";
import { DroneSim } from "./drone_sim.js?v=4";
import { FlightReplay, flightClock } from "./flight_replay.js";
import { FlightHUD } from "./ar_hud.js?v=7";
import { AgentUI, plainDispatchText } from "./agent_ui.js?v=6";

const element = (id) => document.getElementById(id);
const layerToggles = [...document.querySelectorAll("[data-layer]")];
const viewButtons = [...document.querySelectorAll("[data-view]")];
const rateButtons = [...document.querySelectorAll("[data-rate]")];
const playButton = element("play-flight");
const resetButton = element("reset-flight");
const focusButton = element("focus-route");
const windButton = element("wind");
const batteryButton = element("battery");
const closeEmergency = setupEmergencyMenu();
const phaseNames = { takeoff: "起飞", cruise: "巡航", landing: "降落", transition: "调整高度" };
const eventNames = { wind_gust: "突发阵风 / 临时禁飞区", low_battery: "低电量强制备降" };
const taskReplies = new Set(["planned", "needs_clarification", "constraint_conflict"]);
const format = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
const ui = new AgentUI(dispatch, clearClarification);
const hud = new FlightHUD(element("hud"));

let map;
let sim;
let review;
let sceneVersion;
let mission;
let draft;
let busy;
let clarificationToken;
let clarificationQuery;
let frame;
let padNames;
let zoneNames;
let clearance = null;
let lastTelemetry = -Infinity;

function setupEmergencyMenu() {
  const fallback = element("emergency-menu");
  const panel = element("emergency-actions");
  if (!("popover" in HTMLElement.prototype)) return () => { fallback.open = false; };

  const trigger = element("emergency-trigger");
  if (!("command" in HTMLButtonElement.prototype)) {
    trigger.removeAttribute("commandfor");
    trigger.removeAttribute("command");
    trigger.setAttribute("popovertarget", panel.id);
  }
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

function setKPIs(kpis) {
  text("distance", format.format(kpis.distance_m) + " m");
  text("duration", format.format(kpis.estimated_seconds) + " s");
  text("saving", kpis.time_saving_percent === null ? "—" : format.format(kpis.time_saving_percent) + "%");
  text("compliance", kpis.compliant ? format.format(kpis.compliance_percent) + "%" : "未通过");
}

function updateControls() {
  const planned = Boolean(mission);
  const replaying = Boolean(review?.active);
  const locked = Boolean(busy) || replaying;
  const cruising = planned && Boolean(frame) && frame.elapsed > 0 && !frame.complete
    && frame.leg.phase === "cruise" && frame.coordinates.alt >= 30;
  ui.setAvailable(Boolean(sim) && !locked && !isInFlight());
  playButton.disabled = !planned || locked || Boolean(draft) || frame?.complete || mission?.hold;
  resetButton.disabled = !planned || locked || Boolean(draft);
  focusButton.disabled = !sim;
  windButton.disabled = !cruising || locked || mission.hazards.length >= 20;
  batteryButton.disabled = !cruising || locked || mission.diverted;
  review?.setAvailable(review.record?.sceneVersion === sceneVersion
    && !busy && !draft && (!isInFlight() || mission?.hold));
  for (const button of rateButtons) {
    button.disabled = !planned || Boolean(busy);
    button.setAttribute("aria-pressed", Number(button.dataset.rate) === (replaying ? review.rate : sim?.rate));
  }
  for (const button of viewButtons) {
    button.disabled = !sim || ((!planned || draft) && button.dataset.view !== "overview");
    button.setAttribute("aria-pressed", button.dataset.view === (review?.sim?.view ?? sim?.view ?? "overview"));
  }
  text("play-flight", frame?.complete ? "飞行完成" : frame?.playing ? "暂停飞行"
    : frame?.elapsed || mission?.rerouted ? "继续飞行" : "开始飞行");
  text("emergency-hint", replaying ? "回放中。退出回放后可操作当前任务。"
    : busy === "replan" ? "仿真已暂停，正在计算新航线。"
    : cruising ? "应急事件将从当前飞行位置重新规划。"
      : "进入巡航后可注入应急事件。");
}

function isInFlight() {
  return Boolean(frame && !frame.onGround);
}

function flightState() {
  if (review?.active) return "历史回放";
  if (busy === "dispatch") return "调度中";
  if (busy === "replan") return "重规划中";
  if (draft) return "候选航线 · 待确认";
  if (mission?.hold) return "应急悬停";
  if (!frame) return "待命";
  if (frame.complete) return mission.diverted ? "已备降 · 停机" : "已抵达 · 停机";
  if (!frame.playing) {
    if (mission.rerouted) return mission.diverted ? "备降待续" : "绕飞待续";
    return frame.elapsed > 0 ? "已暂停" : "待起飞";
  }
  if (mission.rerouted && frame.leg.phase === "cruise") return mission.diverted ? "备降途中" : "绕飞途中";
  return phaseNames[frame.leg.phase];
}

function refreshFlightState() {
  text("flight-state", flightState());
  element("flight-state").dataset.tone = mission?.hold ? "error" : busy || mission?.diverted ? "warn" : "normal";
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
    clearance = map.clearance(next.coordinates, mission.avoid);
  }
  paintTelemetry(next, sim.view);
  setFlightProgress(mission.elapsedBeforeSegment + next.elapsed, mission.elapsedBeforeSegment + next.duration);
  if (next.complete && !previous?.complete) {
    const outcome = mission.diverted ? "已备降 · 原任务终止" : "已抵达 · 任务完成";
    saveReview(next, outcome, mission.diverted ? "已备降" : "已抵达");
  }
  if (!previous || previous.playing !== next.playing || previous.leg !== next.leg || previous.complete !== next.complete) {
    refreshFlightState();
  }
}

function paintTelemetry(next, view) {
  text("altitude", format.format(next.coordinates.alt) + " m");
  text("speed", format.format(next.speed) + " m/s");
  text("charge", Math.round(next.battery) + "%");
  text("clearance", clearance === null ? "—" : format.format(clearance) + " m");
  element("charge").dataset.alert = next.battery < 18;
  element("clearance").dataset.alert = !next.onGround && clearance !== null && clearance < 12;
  hud.update(view === "fpv", next, clearance);
}

function setFlightProgress(elapsed, duration) {
  element("flight-progress").value = duration ? elapsed / duration : 0;
  text("flight-time", flightClock(elapsed) + " / " + flightClock(duration));
}

function targetLabel(id) {
  return id + " · " + (padNames.get(id) ?? id);
}

function showRouteData(result, target, rerouted) {
  text("distance-label", rerouted ? "接管时剩余航程" : "规划航程");
  text("duration-label", rerouted ? "接管时预计飞行" : "预计飞行");
  setKPIs(result.kpis);
  text("mission-target", targetLabel(target));
}

function installRoute(result, target, battery = 100, rerouted = false) {
  mission.target = target;
  mission.current = result;
  mission.rerouted = rerouted;
  mission.segments.push({ result, target, battery, start: mission.elapsedBeforeSegment, volumes: [...mission.volumes] });
  if (!rerouted) {
    mission.events = [{
      at: 0, name: "开始仿真", detail: targetLabel(target), position: result.waypoints[0], segmentIndex: 0,
    }];
  }
  lastTelemetry = -Infinity;
  map.setHazards(mission.volumes);
  map.drawRoute(result.waypoints, rerouted);
  showRouteData(result, target, rerouted);
  sim.load(result.waypoints, result.corridor, { battery, target });
}

function saveReview(next, outcome, endEvent) {
  const segmentIndex = mission.segments.length - 1;
  const duration = mission.elapsedBeforeSegment + next.elapsed;
  const events = [...mission.events];
  if (endEvent) events.push({ at: duration, name: endEvent, detail: targetLabel(mission.target), position: next.coordinates, segmentIndex });
  review.setRecord({
    sceneVersion, duration, events, avoid: mission.avoid, battery: next.battery,
    outcome: `${outcome} · ${targetLabel(mission.target)}`,
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
    clearance = map.clearance(next.coordinates, record.avoid);
  }
  paintTelemetry(next, review.sim.view);
  setFlightProgress(at, record.duration);
}

function setReplayMode(active) {
  lastTelemetry = -Infinity;
  if (active) {
    review.sim.view = sim.view;
    review.rate = sim.rate;
  } else {
    document.body.dataset.view = sim.view;
    if (sim.view === "overview") map.resetView();
    map.setHazards(mission.volumes);
    map.drawRoute(mission.current.waypoints, mission.rerouted);
    showRouteData(mission.current, mission.target, mission.rerouted);
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
  const message = plainDispatchText(result.detail);
  ui.status(message, conflict);
  ui.message(conflict ? "error" : "agent", message);
}

function discardDraft(label) {
  if (!draft) return;
  draft = null;
  ui.finishPlan(label);
  map.clearPadFocus();
  map.drawRoute(mission?.current.waypoints ?? [], mission?.rerouted);
}

function startMission(result) {
  mission = {
    original: result, hazards: [], volumes: [],
    avoid: new Set(result.intent.avoid_zone_ids), diverted: false, hold: false, elapsedBeforeSegment: 0,
    segments: [], events: [],
  };
  map.clearPadFocus();
  installRoute(result, result.intent.end_id);
  if (sim.view === "overview") focusTaskRoute();
  sim.play();
}

function confirmDraft() {
  if (busy || !draft || isInFlight()) return;
  const { result } = draft;
  draft = null;
  ui.finishPlan("已确认");
  startMission(result);
  ui.status("已确认任务，仿真开始。");
  playButton.focus({ preventScroll: true });
}

function editDraft() {
  if (!draft || busy) return;
  const { query } = draft;
  discardDraft("等待修改");
  ui.setInput(query);
  ui.input.focus();
  ui.status("修改调度指令后重新规划。");
  refreshFlightState();
}

async function dispatch(query, { autoPlay = false, newTask = false } = {}) {
  if (busy || !sim || review?.active || isInFlight()) return;
  if (newTask) clearClarification();
  const startsNewThread = !clarificationToken;
  const originalQuery = clarificationQuery ?? query;
  discardDraft("已取消预览");
  busy = "dispatch";
  refreshFlightState();
  ui.status("已发送指令，正在等候回复…");
  const prompt = ui.beginDispatch(query);
  const path = "/api/agent/dispatch";
  const payload = { query, scene_version: sceneVersion, clarification_token: clarificationToken };
  const exchange = ui.beginExchange(path, payload);
  try {
    const result = await api(path, payload, exchange);
    ui.finishDispatch(prompt, exchange, startsNewThread && taskReplies.has(result.status));
    ui.status("已收到回复，正在逐步说明处理过程…");
    await ui.revealSteps(exchange, result.thought_steps);
    if (result.status !== "planned") {
      showDispatchNotice(result, query);
      return;
    }
    clearClarification();
    ui.setMode(result.mode);
    if (result.scene_version !== sceneVersion || !result.kpis.compliant) {
      const message = "该航线未通过当前场景校验，请刷新后重新规划。";
      ui.status(message, true);
      ui.message("error", message);
      return;
    }
    if (autoPlay && !(result.model_fallback && mission)) {
      startMission(result);
      ui.setInput("");
      ui.message("agent", "应急演示已开始 · " + result.end_name);
      ui.status("应急演示已开始，进入巡航后可注入事件。");
      return;
    }
    const { start_id, end_id } = result.intent;
    let editQuery = query;
    if (originalQuery !== query && !result.model_called) {
      const supplemented = `从 ${start_id} 到 ${end_id}。${originalQuery}`;
      editQuery = supplemented.length <= ui.input.maxLength ? supplemented : originalQuery;
    }
    draft = { result, query: editQuery };
    setView("overview");
    map.drawRoute(result.waypoints, false, true);
    ui.showPlan(result, zoneNames, {
      confirm: confirmDraft, edit: editDraft,
      locate: (id) => map.focusPad(id),
    }, exchange);
    ui.setInput("");
    ui.status("航线待确认。核对任务后可开始仿真。");
  } catch (error) {
    discardDraft("预览失败");
    ui.finishDispatch(prompt, exchange, startsNewThread && taskReplies.has(error.dispatchNotice?.status));
    await ui.revealSteps(exchange, error.steps);
    if (error.dispatchNotice) showDispatchNotice(error.dispatchNotice, query);
    else {
      const message = plainDispatchText(error.message);
      ui.status(message, true);
      ui.message("error", message);
    }
  } finally {
    busy = null;
    refreshFlightState();
  }
}

async function replan(type) {
  const button = type === "wind_gust" ? windButton : batteryButton;
  if (button.disabled || busy || !mission) return;
  const hazard = type === "wind_gust" ? sim.proposeHazard(mission.hazards) : null;
  if (type === "wind_gust" && !hazard) {
    ui.status("前方巡航航段不足，无法在避开当前位置与目的地的情况下放置禁飞区。", true);
    return;
  }

  const before = sim.snapshot();
  const constraints = mission.original.intent.constraints;
  const elapsed = mission.elapsedBeforeSegment + before.elapsed;
  const segmentIndex = mission.segments.length - 1;
  const priorTarget = mission.target;
  const event = { at: elapsed, position: before.coordinates };
  const deadline = mission.diverted ? null : constraints.deadline_seconds;
  busy = "replan";
  closeEmergency();
  if (type === "low_battery") sim.battery = Math.min(sim.battery, 14);
  sim.pause();
  refreshFlightState();
  ui.message("user", "注入应急事件 · " + eventNames[type]);
  ui.status("已冻结当前位置，正在请求应急航线…");
  const path = "/api/agent/replan";
  const payload = {
    current_pos: before.coordinates,
    scene_version: sceneVersion,
    target_id: mission.target,
    mission_type: mission.current.corridor,
    emergency_type: type,
    ...(hazard && { dynamic_hazard: hazard }),
    active_hazards: mission.hazards,
    avoid_zone_ids: [...mission.avoid],
    cruise_limits: constraints.cruise,
    remaining_deadline_seconds: deadline === null ? null : deadline - elapsed,
  };
  const exchange = ui.beginExchange(path, payload);
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
    const detail = hazard ? `半径 ${hazard.radius_m} m · 继续前往 ${targetLabel(result.target_id)}`
      : `${targetLabel(priorTarget)} → ${targetLabel(result.target_id)}`;
    mission.events.push({
      ...event, segmentIndex: segmentIndex + 1, name: `${eventNames[type]} · ${action}获准`, compare: true,
      detail,
    });
    ui.status(action + "航线已接入。");
    if (before.playing) sim.play();
    void ui.revealSteps(exchange, result.thought_steps);
  } catch (error) {
    const detail = plainDispatchText(error.message);
    mission.hold = true;
    mission.events.push({ ...event, segmentIndex, name: `${eventNames[type]} · 仿真悬停`,
      detail: targetLabel(priorTarget) + " · " + detail, battery: sim.battery });
    saveReview(sim.snapshot(), "仿真悬停 · 应急规划未成功");
    ui.status("已悬停 · " + detail, true);
    void ui.revealSteps(exchange, error.steps);
  } finally {
    busy = null;
    refreshFlightState();
  }
}

async function launch() {
  try {
    const data = await api("/api/layers/all");
    sceneVersion = data.scene_version;
    map = new CampusMap("map", data.bounds, data.terrain);
    await map.load(data);
    padNames = new Map(data.vertiports.features.map(({ properties }) => [properties.id, properties.name]));
    zoneNames = new Map(data.nofly_zones.features.map(({ properties }) => [properties.id, properties.name]));
    sim = new DroneSim(map, onFrame);
    review = new FlightReplay(map, {
      onFrame: paintReplay,
      onSegment: ({ result, target }, rerouted) => {
        lastTelemetry = -Infinity;
        showRouteData(result, target, rerouted);
      },
      onMode: setReplayMode, onOverview: () => setView("overview"),
    });
    for (const toggle of layerToggles) {
      toggle.disabled = toggle.dataset.layer === "roads" && !data.roads;
      toggle.addEventListener("change", () => map.showLayer(toggle.dataset.layer, toggle.checked));
    }
    const heightDatum = data.terrain ? " · 地形高程已加载" : "";
    text("scene-status", data.buildings.features.length + " 栋建筑 · " + padNames.size + " 个起降坪" + heightDatum);
    element("reset-view").disabled = false;
    refreshFlightState();
  } catch (error) {
    text("scene-status", "地图初始化失败 · " + error.message);
    element("scene-status").dataset.error = "true";
  }
}

function setView(view) {
  map?.viewer.camera.cancelFlight();
  (review?.sim ?? sim)?.setView(view);
  document.body.dataset.view = view;
  updateControls();
}

function focusTaskRoute() {
  if (!sim) return;
  const currentSim = review?.sim ?? sim;
  const waypoints = review?.active
    ? review.record.segments.slice(0, review.segmentIndex + 1).flatMap(({ flown }) => flown)
    : draft?.result.waypoints ?? mission?.current.waypoints;
  if (currentSim.view !== "overview") setView("overview");
  if (waypoints?.length) map.focusRoute(waypoints, document.querySelector(".map-overlay").clientWidth);
  else map.resetView();
}

element("reset-view").addEventListener("click", () => setView("overview"));
focusButton.addEventListener("click", focusTaskRoute);
for (const button of viewButtons) button.addEventListener("click", () => setView(button.dataset.view));
for (const button of rateButtons) {
  button.addEventListener("click", () => {
    const rate = Number(button.dataset.rate);
    if (review.active) review.rate = rate;
    else sim.setRate(rate);
    updateControls();
  });
}
playButton.addEventListener("click", () => sim.playing ? sim.pause() : sim.play());
resetButton.addEventListener("click", () => {
  if (isInFlight() && !mission.hold) saveReview(frame, "任务中止 · 已重置仿真", "重置飞行");
  mission.hazards = [];
  mission.volumes = [];
  mission.diverted = false;
  mission.hold = false;
  mission.elapsedBeforeSegment = 0;
  mission.segments = [];
  installRoute(mission.original, mission.original.intent.end_id);
  ui.status("已返回起点。");
  refreshFlightState();
});
windButton.addEventListener("click", () => replan("wind_gust"));
batteryButton.addEventListener("click", () => replan("low_battery"));
launch();
api("/api/health").then(({ llm_mode }) => ui.setMode(llm_mode)).catch(() => ui.setMode(null));
