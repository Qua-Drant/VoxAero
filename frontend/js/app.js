import { CampusMap } from "./map3d.js";
import { DroneSim } from "./drone_sim.js";
import { FlightHUD } from "./ar_hud.js";
import { AgentUI } from "./agent_ui.js";

const element = (id) => document.getElementById(id);
const layerToggles = [...document.querySelectorAll("[data-layer]")];
const viewButtons = [...document.querySelectorAll("[data-view]")];
const rateButtons = [...document.querySelectorAll("[data-rate]")];
const playButton = element("play-flight");
const resetButton = element("reset-flight");
const windButton = element("wind");
const batteryButton = element("battery");
const emergencyMenu = element("emergency-menu");
const phaseNames = { takeoff: "起飞", cruise: "巡航", landing: "降落", transition: "调整高度" };
const corridorNames = { delivery: "配送层 30–60 m", inspection: "巡检层 60–90 m" };
const eventNames = { wind_gust: "突发阵风 / 临时禁飞区", low_battery: "低电量强制备降" };
const format = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
const ui = new AgentUI(dispatch);
const hud = new FlightHUD(element("hud"));

let map;
let sim;
let mission;
let busy;
let frame;
let padNames;
let clearance = null;
let lastTelemetry = -Infinity;

function text(id, value) {
  const target = element(id);
  if (target.textContent !== value) target.textContent = value;
}

async function api(path, payload) {
  const options = payload === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  };
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) {
    const error = new Error(typeof body.detail === "string" ? body.detail : "请求失败，请检查参数后重试。");
    error.steps = body.thought_steps;
    throw error;
  }
  return body;
}

function clock(seconds) {
  const whole = Math.floor(seconds);
  return [Math.floor(whole / 60), whole % 60].map((value) => String(value).padStart(2, "0")).join(":");
}

function setKPIs(kpis) {
  text("distance", format.format(kpis.distance_m) + " m");
  text("duration", format.format(kpis.estimated_seconds) + " s");
  text("saving", kpis.time_saving_percent === null ? "—" : format.format(kpis.time_saving_percent) + "%");
  text("compliance", kpis.compliant ? format.format(kpis.compliance_percent) + "%" : "未通过");
}

function updateControls() {
  const planned = Boolean(mission);
  const cruising = planned && Boolean(frame) && frame.elapsed > 0 && !frame.complete
    && frame.leg.phase === "cruise" && frame.coordinates.alt >= 30;
  ui.setAvailable(Boolean(sim) && !busy);
  playButton.disabled = !planned || Boolean(busy) || frame?.complete || mission?.hold;
  resetButton.disabled = !planned || Boolean(busy);
  windButton.disabled = !cruising || Boolean(busy) || mission.hazards.length >= 20;
  batteryButton.disabled = !cruising || Boolean(busy) || mission.diverted;
  for (const button of rateButtons) {
    button.disabled = !planned || Boolean(busy);
    button.setAttribute("aria-pressed", Number(button.dataset.rate) === sim?.rate);
  }
  for (const button of viewButtons) {
    button.disabled = !sim || (!planned && button.dataset.view !== "overview");
    button.setAttribute("aria-pressed", button.dataset.view === (sim?.view ?? "overview"));
  }
  text("play-flight", frame?.complete ? "飞行完成" : frame?.playing ? "暂停飞行"
    : frame?.elapsed || mission?.rerouted ? "继续飞行" : "开始飞行");
  text("emergency-hint", busy === "replan" ? "仿真已暂停，正在计算新航线。"
    : cruising ? "应急事件将从当前飞行位置重新规划。"
      : "进入巡航后可注入应急事件。");
}

function flightState() {
  if (busy === "dispatch") return "调度中";
  if (busy === "replan") return "重规划中";
  if (mission?.hold) return "应急悬停";
  if (!frame) return "待命";
  if (frame.complete) return "已抵达 · 停机";
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
  text("altitude", format.format(next.coordinates.alt) + " m");
  text("speed", format.format(next.speed) + " m/s");
  text("charge", Math.round(next.battery) + "%");
  text("clearance", clearance === null ? "—" : format.format(clearance) + " m");
  element("charge").dataset.alert = next.battery < 18;
  element("clearance").dataset.alert = !next.onGround && clearance !== null && clearance < 12;
  hud.update(sim.view === "fpv", next, clearance);
  element("flight-progress").value = next.elapsed / next.duration;
  text("flight-time", clock(next.elapsed) + " / " + clock(next.duration));
  if (!previous || previous.playing !== next.playing || previous.leg !== next.leg || previous.complete !== next.complete) {
    refreshFlightState();
  }
}

function installRoute(result, target, battery = 100, rerouted = false) {
  mission.target = target;
  mission.current = result;
  mission.rerouted = rerouted;
  lastTelemetry = -Infinity;
  map.setHazards(mission.volumes);
  map.drawRoute(result.waypoints, rerouted);
  text("distance-label", rerouted ? "接管时剩余航程" : "规划航程");
  text("duration-label", rerouted ? "接管时预计飞行" : "预计飞行");
  setKPIs(result.kpis);
  text("mission-target", target + " · " + (padNames.get(target) ?? target));
  sim.load(result.waypoints, result.corridor, { battery, target });
}

async function dispatch(query, autoPlay) {
  if (busy || !sim) return;
  busy = "dispatch";
  sim.pause();
  refreshFlightState();
  ui.status("正在校验空域并规划航线…");
  ui.message("user", query);
  try {
    const result = await api("/api/agent/dispatch", { query });
    mission = {
      original: result, hazards: [], volumes: [],
      avoid: new Set(result.intent.avoid_zone_ids), diverted: false, hold: false,
    };
    installRoute(result, result.intent.end_id);
    ui.setMode(result.mode);
    ui.message("agent", result.intent.start_id + " → " + result.intent.end_id + " · "
      + corridorNames[result.corridor] + " · 航线就绪", result.thought_steps);
    ui.status(autoPlay ? "应急演示航线已生成。" : "航线已生成。");
    if (autoPlay) sim.play();
  } catch (error) {
    ui.status(error.message, true);
    ui.message("error", error.message, error.steps);
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
  busy = "replan";
  emergencyMenu.open = false;
  if (type === "low_battery") sim.battery = Math.min(sim.battery, 14);
  sim.pause();
  refreshFlightState();
  ui.message("user", "注入应急事件 · " + eventNames[type]);
  ui.status("已冻结当前位置，正在请求应急航线…");
  try {
    const result = await api("/api/agent/replan", {
      current_pos: before.coordinates,
      target_id: mission.target,
      mission_type: mission.current.corridor,
      emergency_type: type,
      ...(hazard && { dynamic_hazard: hazard }),
      active_hazards: mission.hazards,
      avoid_zone_ids: [...mission.avoid],
    });
    if (result.temporary_nofly_zone && hazard) {
      mission.hazards.push(hazard);
      mission.volumes.push(result.temporary_nofly_zone);
    }
    mission.diverted ||= type === "low_battery";
    mission.hold = false;
    installRoute(result, result.target_id, sim.battery, true);
    const action = type === "low_battery" ? "备降" : "绕飞";
    ui.message("agent", action + "航线已更新 · " + result.target_id + " "
      + (padNames.get(result.target_id) ?? ""), result.thought_steps);
    ui.status(action + "航线已接入。");
    if (before.playing) sim.play();
  } catch (error) {
    mission.hold = true;
    ui.status("已悬停 · " + error.message, true);
    ui.message("error", error.message, error.steps);
  } finally {
    busy = null;
    refreshFlightState();
  }
}

async function launch() {
  try {
    const data = await api("/api/layers/all");
    map = new CampusMap("map", data.bounds);
    await map.load(data);
    padNames = new Map(data.vertiports.features.map(({ properties }) => [properties.id, properties.name]));
    sim = new DroneSim(map, onFrame);
    for (const toggle of layerToggles) {
      toggle.disabled = false;
      toggle.addEventListener("change", () => map.showLayer(toggle.dataset.layer, toggle.checked));
    }
    text("scene-status", data.buildings.features.length + " 栋建筑 · " + padNames.size + " 个起降坪");
    element("reset-view").disabled = false;
    refreshFlightState();
  } catch (error) {
    text("scene-status", "地图初始化失败 · " + error.message);
    element("scene-status").dataset.error = "true";
  }
}

function setView(view) {
  sim?.setView(view);
  document.body.dataset.view = view;
  updateControls();
}

element("reset-view").addEventListener("click", () => setView("overview"));
for (const button of viewButtons) button.addEventListener("click", () => setView(button.dataset.view));
for (const button of rateButtons) {
  button.addEventListener("click", () => {
    sim.setRate(Number(button.dataset.rate));
    updateControls();
  });
}
playButton.addEventListener("click", () => sim.playing ? sim.pause() : sim.play());
resetButton.addEventListener("click", () => {
  mission.hazards = [];
  mission.volumes = [];
  mission.diverted = false;
  mission.hold = false;
  installRoute(mission.original, mission.original.intent.end_id);
  ui.status("已返回起点。");
  refreshFlightState();
});
windButton.addEventListener("click", () => replan("wind_gust"));
batteryButton.addEventListener("click", () => replan("low_battery"));
launch();
api("/api/health").then(({ llm_mode }) => ui.setMode(llm_mode)).catch(() => ui.setMode(null));
