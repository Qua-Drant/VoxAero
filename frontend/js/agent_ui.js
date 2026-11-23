import { renderAirspaceCheck, spatialCandidateForm } from "./spatial_ui.js?v=3";

const PRESETS = {
  spatial: "从信息学部附近找一个可起飞的地面点，去图书馆西侧巡检，绕开静谧区。",
  route: "从星空广场起飞，依次途经星湖、星湖南路，最后到信息学部操场巡检，比较多条航线。",
  delivery: "中午高峰配送：将餐食从信息学部食堂送至测国重楼顶，避开图书馆静谧区",
  inspection: "夜间高空安防巡检：从信息学部起飞，沿 60–90m 巡检层前往图书馆西侧巡检点",
  emergency: "应急航线演示：从 VP02 沿 60–90m 巡检层前往 VP03",
};
const RESPONSE_LABELS = {
  choose_endpoints: "已生成候选", needs_spatial_location: "请补充地点", airspace_report: "空域核验",
  planned: "航线已规划", help: "使用指引", out_of_scope: "这条指令无法调度", needs_intent: "请说明任务",
  needs_clarification: "请补充地点", clarification_closed: "请重新提交指令", constraint_conflict: "飞行要求有冲突",
};
const ACTION_STEPS = {
  classify_dispatch: ["请求分类", "识别任务、地点补充或空域查询。"],
  parse_dispatch_intent: ["意图识别", "核对任务类型与起终点。"],
  clarify_dispatch: ["地点补全", "更新起终点，沿用既定约束。"],
  replan_emergency: ["应急评估", "按事件类型评估绕飞或备降。"],
};
const decimal = (value) => value.toFixed(1);
const CORRIDORS = { delivery: "30–60 m", inspection: "60–90 m" };
const number = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function plainDispatchText(content) {
  if (typeof content !== "string" || /(?:^|\s)[{\[]\s*["{\[]|["\w]+"\s*:\s*["{\[]/.test(content)) {
    return "平台已返回回复，请查看任务状态。";
  }
  return content
    .replace(/(\d+(?:\.\d+)?)\s+m\b/g, (_, meters) => meters + " m")
    .replaceAll("起降坪", "起降点")
    .replaceAll("候选航线", "找到的路线")
    .replaceAll("合规航线", "符合飞行要求的路线")
    .replaceAll("硬约束", "必须满足的要求")
    .replaceAll("全高度绕开", "从任何高度都绕开")
    .replace("并应用其余空域限制", "并遵守其他飞行限制")
    .replace("应用建筑缓冲区和全部静态空域限制", "让航线远离建筑并避开禁飞区")
    .replace("模型调用失败或参数不合规，切换至本地规则", "模型未能完成识别，已用内置规则继续处理")
    .replace("新航线已通过空域校验", "新路线已符合飞行要求");
}

function readableStep({ role, content }) {
  if (role === "action") {
    const [action, detail] = content.split(" · ");
    if (action === "校验空域") return ["空域检查", `核对巡航航层 ${plainDispatchText(detail ?? "适用航层")}。`];
    if (content.startsWith("模型提取地点") || content.startsWith("按本地规则识别")) return ["识别地点", content];
    if (content.startsWith("用场景地形")) return ["起降筛选", "核对地形、建筑、围栏和候选航线。"];
    if (action === "plan_3d_route") {
      if (detail?.includes("备降") || detail?.includes("应急坪")) return ["备降规划", "比较可达备降点与航程。"];
      if (detail?.startsWith("从当前")) return ["绕飞规划", "从当前机位规划绕飞航线。"];
      return ["航线规划", "搜索三维航线并复核障碍净空。"];
    }
    return ACTION_STEPS[action] ?? ["任务复核", "检查既定飞行条件。"];
  }
  if (role === "thought") return content.startsWith("保持飞行当前位置")
    ? ["机位锁定", "记录当前位置作为改航起点。"]
    : ["约束提取", "提取航层与避让要求。"];
  const endpoints = content.match(/^\S+ (.+) → \S+ (.+)$/);
  if (endpoints) return ["地点匹配", `${endpoints[1]} → ${endpoints[2]}`];
  const route = content.match(/^规划成功，\d+ 个航点，([\d.]+) m。$/);
  if (route) return ["航线复核", `已生成合规航线 · ${route[1]} m`];
  return [content.includes("空域") || content.includes("避让") ? "规则核验" : "处理反馈", plainDispatchText(content)];
}

function altitudeText(limits) {
  const parts = [];
  if (limits.min_alt !== null) parts.push(`${limits.min_exclusive ? ">" : "≥"} ${number.format(limits.min_alt)} 米`);
  if (limits.max_alt !== null) parts.push(`${limits.max_exclusive ? "<" : "≤"} ${number.format(limits.max_alt)} 米`);
  return parts.join(" 且 ");
}

function planRequirements({ intent, waypoints }, zoneNames) {
  const { cruise, preferred_cruise } = intent.constraints;
  const rows = [];
  if (cruise) rows.push({ text: `巡航 ${altitudeText(cruise)}（场景高度）` });
  if (intent.avoid_zone_ids.length) {
    rows.push({ text: "全高度避让 " + intent.avoid_zone_ids.map((id) => zoneNames.get(id) ?? id).join("、") });
  }
  if (preferred_cruise) {
    const limits = preferred_cruise;
    const allows = ({ alt }) => (limits.min_alt === null || (limits.min_exclusive ? alt > limits.min_alt : alt >= limits.min_alt))
      && (limits.max_alt === null || (limits.max_exclusive ? alt < limits.max_alt : alt <= limits.max_alt));
    const missed = waypoints.some((point, index) => index > 0 && point.phase === "cruise"
      && (!allows(waypoints[index - 1]) || !allows(point)));
    rows.push({ text: `巡航偏好 ${altitudeText(limits)}（场景高度）${missed ? " · 未满足" : ""}`, warning: missed });
  }
  return rows.length ? rows : [{ text: "采用默认空域与建筑避障规则" }];
}

export class AgentUI {
  constructor(dispatch, clearClarification, pickClarification) {
    this.mode = document.getElementById("mode");
    this.input = document.getElementById("query");
    this.form = document.getElementById("dispatch-form");
    this.messages = document.getElementById("agent-messages");
    this.mission = document.getElementById("active-mission");
    this.exchangeTemplate = document.getElementById("agent-exchange-template");
    this.planTemplate = document.getElementById("dispatch-plan-template");
    this.guide = document.getElementById("agent-welcome");
    this.clarification = document.getElementById("location-clarification");
    this.clarificationHeading = document.getElementById("clarification-heading");
    this.clarificationDetail = document.getElementById("clarification-detail");
    this.clarificationSlots = document.getElementById("clarification-slots");
    this.clarificationActions = document.getElementById("clarification-actions");
    this.pickClarification = pickClarification;
    this.dispatch = dispatch;
    this.panel = this.form.closest(".dispatch");
    this.feedback = document.getElementById("dispatch-status");
    this.submit = this.form.querySelector("#submit");
    this.clear = document.getElementById("clear-clarification");
    this.clear.addEventListener("click", clearClarification);
    this.presets = [...document.querySelectorAll("[data-preset]")];
    this.missionNumber = 0;
    for (const button of this.presets) {
      button.addEventListener("click", () => {
        const preset = button.dataset.preset;
        this.setInput(PRESETS[preset]);
        dispatch(this.input.value, { newTask: true, autoPlay: preset === "emergency" });
      });
    }
    this.form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (this.submit.disabled) return;
      const query = this.input.value.trim();
      if (query) dispatch(query);
      else this.status("请输入起降坪与任务要求。", true);
    });
    this.input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.isComposing || event.keyCode === 229 || event.altKey || event.metaKey) return;
      if (event.ctrlKey) {
        event.preventDefault();
        const { selectionStart, selectionEnd, value, maxLength } = this.input;
        if (value.length - (selectionEnd - selectionStart) < maxLength) {
          this.input.setRangeText("\n", selectionStart, selectionEnd, "end");
        }
      } else if (!event.shiftKey) {
        event.preventDefault();
        if (!event.repeat && !this.submit.disabled) this.form.requestSubmit(this.submit);
      }
    });
    for (const button of this.guide.querySelectorAll("[data-welcome-example]")) {
      button.addEventListener("click", () => {
        this.clear.click();
        this.setInput(PRESETS[button.dataset.welcomeExample]);
        this.guide.hidden = true;
        this.input.focus();
      });
    }
  }

  setInput(value) {
    this.input.value = value;
  }

  beginExchange() {
    this.guide.hidden = true;
    const element = this.exchangeTemplate.content.cloneNode(true).firstElementChild;
    const part = (name) => element.querySelector(`[data-exchange="${name}"]`);
    const exchange = { element, part, body: null, ok: false };
    part("trace").addEventListener("click", () => {
      const open = part("trace").getAttribute("aria-expanded") !== "true";
      part("trace").setAttribute("aria-expanded", String(open));
      part("summary").hidden = open;
      part("details").hidden = !open;
      this.updateTraceScroll(exchange);
      part("trace-title").textContent = open ? "返回摘要" : `展开 ${exchange.total} 步详情`;
    });
    part("steps").addEventListener("scroll", () => this.updateTraceScroll(exchange), { passive: true });
    this.traceResize?.disconnect();
    this.traceResize = new ResizeObserver(() => this.updateTraceScroll(exchange));
    this.traceResize.observe(part("steps"));
    element.dataset.pending = "";
    this.messages.replaceChildren(element);
    this.setTraceProgress(exchange, 8, 0);
    this.setTraceSummary(exchange, [["调度请求", "已发送，等待平台确认任务与飞行条件。"]]);
    exchange.receive = (response, body) => {
      exchange.body = typeof body === "object" ? body : null;
      exchange.ok = response.ok && Boolean(exchange.body);
      part("count").textContent = exchange.ok ? "已收到回复" : "请求待处理";
    };
    exchange.fail = () => { part("count").textContent = "连接失败"; };
    return exchange;
  }

  updateTraceScroll({ part }) {
    const list = part("steps");
    part("scroll-hint").hidden = list.scrollHeight - list.scrollTop <= list.clientHeight + 2;
  }

  setTraceProgress({ part }, total, complete) {
    const progress = part("progress");
    if (progress.childElementCount !== total) {
      progress.replaceChildren(...Array.from({ length: total }, () => document.createElement("span")));
    }
    Array.from(progress.children).forEach((cell, index) => cell.toggleAttribute("data-done", index < complete));
  }

  setTraceSummary({ part }, steps) {
    part("summary").replaceChildren(...steps.map(([label, detail], index) => {
      const row = document.createElement("li");
      const number = document.createElement("span");
      number.className = "mono";
      number.textContent = String(index + 1).padStart(2, "0");
      const name = document.createElement("b");
      name.textContent = label;
      const body = document.createElement("p");
      body.textContent = detail;
      row.append(number, name, body);
      return row;
    }));
  }

  async revealSteps(exchange, steps = [], zoneNames = new Map()) {
    const { element, part, body, ok } = exchange;
    const list = part("steps");
    const count = part("count");
    const choosing = body?.status === "choose_endpoints";
    const planned = choosing || body?.kpis?.compliant && (body.status === "planned" || Boolean(body.emergency_type));
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    exchange.total = steps.length;
    part("trace-title").textContent = steps.length ? `展开 ${steps.length} 步详情` : "无步骤";
    part("trace").disabled = !steps.length;
    this.setTraceProgress(exchange, steps.length, 0);
    if (steps.length) count.textContent = `0/${steps.length}`;
    else if (count.textContent === "已收到回复") count.textContent = "已处理";
    for (const [index, step] of steps.entries()) {
      if (!reduced) await delay(index === 0 ? 220 : Math.min(310, 2600 / steps.length));
      if (!element.isConnected) return;
      const [label, detail] = readableStep(step);
      const row = document.createElement("li");
      const number = document.createElement("span");
      number.className = "mono";
      number.textContent = String(index + 1).padStart(2, "0");
      const title = document.createElement("b");
      title.textContent = label;
      const text = document.createElement("p");
      text.textContent = detail;
      row.append(number, title, text);
      list.append(row);
      if (!part("details").hidden) this.updateTraceScroll(exchange);
      this.setTraceProgress(exchange, steps.length, index + 1);
      count.textContent = `${index + 1}/${steps.length}`;
      this.setTraceSummary(exchange, [[label, detail]]);
    }
    delete element.dataset.pending;
    if (steps.length) {
      count.textContent = `${steps.length}/${steps.length} ${ok && planned ? "通过" : "已处理"}`;
      if (ok && planned) element.dataset.passed = "";
    }
    if (choosing && ok) {
      this.setTraceSummary(exchange, [
        ["地点筛选", "核对起降点及途经地点。"],
        ["航线复核", `共 ${body.options.length} 条航线通过场景检查。`],
        ["候选确认", "可预览航线，比较净空与航程。"],
      ]);
    } else if (planned && ok) {
      const { kpis, intent } = body;
      const corridor = CORRIDORS[body.corridor];
      const constraints = intent ? planRequirements(body, zoneNames) : [];
      const checked = constraints.find(({ text }) => text.includes("避让"));
      this.setTraceSummary(exchange, [
        ["指令解析", intent
          ? `确认${intent.mission_type === "inspection" ? "巡检" : "配送"}任务 · ${intent.start_id} → ${intent.end_id}`
          : `重新研判当前机位与目的地 ${body.target_id}`],
        ["约束核验", checked?.text ?? `航层 ${corridor} · 避开禁飞区`],
        ["航线生成", `生成 ${decimal(kpis.distance_m)} m 三维航迹${body.route_visits?.length ? ` · 飞越 ${body.route_visits.length} 处` : ""}`],
      ]);
    } else if (body?.clarification) {
      this.setTraceSummary(exchange, [["地点核对", "已保留当前任务，等待确认上方标出的地点。"]]);
    } else if (body?.detail) {
      const readback = [[RESPONSE_LABELS[body.status] ?? "调度反馈", plainDispatchText(body.detail)]];
      if (body.airspace_check) readback.push(["来源核验", body.airspace_check.detail]);
      this.setTraceSummary(exchange, readback);
    }
  }

  showLocationClarification(question, detail) {
    this.clarificationHeading.textContent = `确认${question.label}`;
    this.clarificationDetail.textContent = plainDispatchText(detail);
    const rows = question.slots.map(({ key, label, phrase, resolved_name }) => {
      const row = document.createElement("div");
      const title = document.createElement("dt");
      const value = document.createElement("dd");
      row.toggleAttribute("data-current", key === question.slot);
      title.textContent = label;
      value.textContent = resolved_name
        ? `${resolved_name} · 已识别`
        : phrase ? `${phrase} · 待确认` : "待指定";
      row.append(title, value);
      return row;
    });
    this.clarificationSlots.replaceChildren(...rows);
    const actions = question.options.map(({ label, reply }) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "action-button";
      button.textContent = label;
      button.addEventListener("click", () => this.dispatch(reply));
      return button;
    });
    if (question.map_pick) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "action-button";
      button.textContent = "地图点选";
      button.id = "clarification-map-pick";
      button.setAttribute("aria-pressed", "false");
      button.addEventListener("click", () => this.pickClarification(question.label));
      actions.push(button);
    }
    this.clarificationActions.replaceChildren(...actions);
    this.clarificationActions.hidden = !actions.length;
    this.clarification.hidden = false;
    this.clarificationHeading.focus({ preventScroll: true });
  }

  setClarification(active, question) {
    this.clear.hidden = !active;
    document.getElementById("submit-label").textContent = active ? "提交补答" : "规划航线";
    this.input.placeholder = question ? `回复${question.label}的方位或附近地标`
      : active ? "例如：食堂" : "例如：从 VP01 巡检至 VP04";
    if (!question) {
      this.clarification.hidden = true;
      this.clarificationActions.replaceChildren();
    }
  }

  setAvailable(available, decisionPending) {
    this.panel.toggleAttribute("data-decision", decisionPending);
    if (this.clear.hidden) {
      const label = decisionPending ? "重新规划" : "规划航线";
      const submitLabel = document.getElementById("submit-label");
      if (submitLabel.textContent !== label) submitLabel.textContent = label;
    }
    this.submit.disabled = !available;
    this.clear.disabled = !available;
    for (const button of this.clarificationActions.querySelectorAll("button")) button.disabled = !available;
    for (const button of this.presets) button.disabled = !available;
  }

  setMode(mode) {
    this.mode.textContent = { live_llm: "模型模式", fallback_mock: "本地规则" }[mode] ?? "服务离线";
    this.mode.dataset.tone = mode ? "normal" : "error";
  }

  status(message, error = false) {
    this.feedback.textContent = plainDispatchText(message);
    this.feedback.dataset.error = error;
    this.feedback.toggleAttribute("data-active", !error && /已确认|飞行中|已接入|已开始/.test(message));
  }

  showChoices(result, callbacks) {
    this.plan = null;
    this.mission.replaceChildren(spatialCandidateForm(result, callbacks));
    this.mission.querySelector("h3").focus({ preventScroll: true });
  }

  showPlan(result, zoneNames, { confirm, edit, locate }, running = false) {
    const card = this.planTemplate.content.cloneNode(true).firstElementChild;
    const part = (name) => card.querySelector(`[data-plan="${name}"]`);
    const { intent } = result;
    part("type").textContent = intent.mission_type === "inspection" ? "校园巡检任务" : "校园配送任务";
    part("id").textContent = "#MSN-" + String(++this.missionNumber).padStart(3, "0");
    this.plan = { card, part, locate, zoneNames, visits: result.route_visits ?? [] };
    this.setPlanEndpoint("start", intent.start_id, result.start_name);
    this.setPlanEndpoint("end", intent.end_id, result.end_name);
    part("confirm").addEventListener("click", confirm);
    part("edit").addEventListener("click", edit);
    this.mission.replaceChildren(card);
    this.updatePlan(result, intent.end_id, result.end_name);
    this.setVisitProgress(0);
    if (running) this.acceptPlan();
  }

  setPlanEndpoint(key, id, name) {
    const { part, locate } = this.plan;
    const button = part(key);
    const code = document.createElement("span");
    code.className = "mono";
    code.textContent = id;
    const label = document.createElement("span");
    label.textContent = name;
    button.replaceChildren(code, label);
    button.title = `定位${key === "start" ? "起点" : "终点"} ${id} ${name}`;
    button.onclick = () => locate(id);
  }

  updatePlan(result, target, targetName) {
    if (!this.plan) return;
    const { part, zoneNames } = this.plan;
    const { kpis, intent } = result;
    this.setPlanEndpoint("end", target, targetName);
    part("corridor").textContent = result.cruise_altitude_m == null
      ? CORRIDORS[result.corridor] : `${number.format(result.cruise_altitude_m)} m`;
    part("compliance").textContent = kpis.compliant
      ? `${Math.round(kpis.compliance_percent)}% · 校验 ${zoneNames.size + (result.temporary_nofly_zone ? 1 : 0)} 区` : "未通过";
    part("clearance").textContent = kpis.min_clearance_m == null ? "—" : `≥ ${(Math.floor(kpis.min_clearance_m * 10) / 10).toFixed(1)} m`;
    part("clearance").title = "规划航段距最近建筑或禁飞区包络的最小净空";
    part("source").textContent = !intent ? "本地应急规划" : result.model_fallback ? "本地规则兜底"
      : result.mode === "live_llm" ? "模型识别" : "本地规则";
    renderAirspaceCheck(part("airspace"), result.airspace_check);
    part("kpis").textContent = `规划航程 ${decimal(kpis.distance_m)} m · 预计 ${decimal(kpis.estimated_seconds)} s`;
  }

  setVisitProgress(completed, diverted = false) {
    if (!this.plan?.visits.length) return;
    const { part, visits } = this.plan;
    const panel = part("visits");
    panel.hidden = false;
    const status = diverted ? `原任务中止 · 已飞越 ${completed}/${visits.length}`
      : `飞越进度 ${completed}/${visits.length}`;
    if (part("visit-progress").textContent !== status) part("visit-progress").textContent = status;
    const itinerary = visits.map(visit => visit.name).join(" → ");
    if (part("visit-names").textContent !== itinerary) part("visit-names").textContent = itinerary;
  }

  setMissionState(label, tone = "neutral") {
    const state = this.plan?.part("state");
    if (!state) return;
    state.textContent = label;
    state.dataset.tone = tone;
  }

  acceptPlan() {
    this.activePlan = this.plan;
    this.setMissionState("已确认 · 执行中", "vector");
    this.plan.part("actions").hidden = true;
  }

  restoreMission() {
    this.plan = this.activePlan;
    this.mission.replaceChildren(...(this.plan ? [this.plan.card] : []));
  }
}
