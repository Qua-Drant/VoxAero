import { CampusMap } from "./map3d.js";

const sampleQuery = "中午高峰配送：将餐食从信息学部食堂送至测国重楼顶，避开图书馆静谧区";
const roles = { thought: "任务", action: "调用", observation: "结果" };
const element = (id) => document.getElementById(id);
const form = element("dispatch-form");
const query = element("query");
const sample = element("sample");
const submit = element("submit");
const status = element("dispatch-status");
const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
const clearanceNumber = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
let map;
let latestRoute;

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) {
    const message = typeof body.detail === "string"
      ? body.detail : "请求失败，请检查指令后重试。";
    const error = new Error(message);
    error.steps = body.thought_steps;
    throw error;
  }
  return body;
}

function showMode(mode) {
  const badge = element("mode");
  badge.textContent = mode === "live_llm" ? "模型模式" : "本地规则";
  badge.title = mode;
}

function showTrace(steps) {
  const list = element("trace");
  list.replaceChildren(...steps.map(({ role, content }) => {
    const row = document.createElement("li");
    const label = document.createElement("span");
    const text = document.createElement("p");
    label.textContent = roles[role] || role;
    text.textContent = content;
    row.append(label, text);
    return row;
  }));
}

function showMetrics(kpis) {
  element("distance").textContent = number.format(kpis.distance_m) + " m";
  element("duration").textContent = number.format(kpis.estimated_seconds) + " s";
  element("saving").textContent = kpis.time_saving_percent === null
    ? "—" : number.format(kpis.time_saving_percent) + "%";
  element("clearance").textContent = kpis.min_clearance_m === null
    ? "—" : clearanceNumber.format(kpis.min_clearance_m) + " m";
  element("compliance").textContent = "合规 " + kpis.compliance_percent + "%";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const instruction = query.value.trim();
  if (submit.disabled) return;
  if (!instruction) {
    status.dataset.error = "true";
    status.textContent = "请输入起点、终点和任务要求。";
    return;
  }
  sample.disabled = true;
  submit.disabled = true;
  status.dataset.error = "false";
  status.textContent = "正在校验空域并规划航线…";
  try {
    const result = await api("/api/agent/dispatch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: instruction }),
    });
    latestRoute = result.waypoints;
    map?.drawRoute(latestRoute);
    showMode(result.mode);
    showMetrics(result.kpis);
    showTrace(result.thought_steps);
    status.textContent = result.intent.start_id + " → " + result.intent.end_id + " · 航线已更新";
  } catch (error) {
    latestRoute = null;
    map?.clearRoute();
    for (const id of ["distance", "duration", "saving", "clearance"]) element(id).textContent = "—";
    element("compliance").textContent = "";
    status.dataset.error = "true";
    status.textContent = error.message;
    showTrace(error.steps || []);
  } finally {
    sample.disabled = false;
    submit.disabled = false;
  }
});

sample.addEventListener("click", () => {
  query.value = sampleQuery;
  form.requestSubmit();
});

async function loadMap() {
  const sceneStatus = element("scene-status");
  try {
    const data = await api("/api/layers/all");
    const campus = new CampusMap("map", data.bounds);
    await campus.load(data);
    map = campus;
    if (latestRoute) map.drawRoute(latestRoute);
    document.querySelectorAll("[data-layer]").forEach((toggle) => {
      toggle.disabled = false;
      toggle.addEventListener("change", () => map.showLayer(toggle.dataset.layer, toggle.checked));
    });
    const reset = element("reset-view");
    reset.disabled = false;
    reset.addEventListener("click", () => map.resetView());
    sceneStatus.textContent = data.buildings.features.length + " 栋建筑 · "
      + data.vertiports.features.length + " 个起降坪 · 模拟空域";
  } catch (error) {
    sceneStatus.dataset.error = "true";
    sceneStatus.textContent = "地图初始化失败 · " + error.message;
  }
}

loadMap();
api("/api/health").then(({ llm_mode }) => {
  if (!latestRoute) showMode(llm_mode);
}).catch(() => { element("mode").textContent = "服务离线"; });
