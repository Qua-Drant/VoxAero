const PRESETS = {
  delivery: "中午高峰配送：将餐食从信息学部食堂送至测国重楼顶，避开图书馆静谧区",
  inspection: "夜间高空安防巡检：从信息学部起飞，沿 60–90m 巡检层前往图书馆西侧巡检点",
  emergency: "应急航线演示：从 VP02 沿 60–90m 巡检层前往 VP03",
};
const RESPONSE_LABELS = {
  planned: "航线已规划", help: "使用指引", out_of_scope: "这条指令无法调度", needs_intent: "请说明任务",
  needs_clarification: "请补充地点", clarification_closed: "请重新提交指令", constraint_conflict: "飞行要求有冲突",
};
const ACTION_STEPS = {
  classify_dispatch: ["读指令", "先确认这条消息是在安排任务、补充地点，还是询问用法。"],
  parse_dispatch_intent: ["懂需求", "根据指令确认是配送还是巡检，以及要从哪里飞到哪里。"],
  clarify_dispatch: ["补地点", "用你刚提供的地点补全任务，保留之前的飞行要求。"],
  replan_emergency: ["做应急", "根据突发情况，重新选择可以飞的路线。"],
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
    .replace(/(\d+(?:\.\d+)?)\s+m\b/g, (_, meters) => Number(meters).toFixed(1) + " m")
    .replaceAll("起降坪", "起降点")
    .replaceAll("候选航线", "找到的路线")
    .replaceAll("合规航线", "符合飞行要求的路线")
    .replaceAll("硬约束", "必须满足的要求")
    .replaceAll("硬时限", "的到达时限")
    .replaceAll("全高度绕开", "从任何高度都绕开")
    .replace("并应用其余空域限制", "并遵守其他飞行限制")
    .replace("应用建筑缓冲区和全部静态空域限制", "让航线远离建筑并避开禁飞区")
    .replace("模型调用失败或参数不合规，切换至本地规则", "模型未能完成识别，已用内置规则继续处理")
    .replace("新航线已通过空域校验", "新路线已符合飞行要求");
}

function readableStep({ role, content }) {
  if (role === "action") {
    const [action, detail] = content.split(" · ");
    if (action === "校验空域") return ["查限制", `检查可用的巡航高度（${plainDispatchText(detail ?? "适用航层")}），确认哪些地方要绕开。`];
    if (action === "plan_3d_route") {
      if (detail?.includes("备降") || detail?.includes("应急坪")) return ["找路线", "比较附近能到达的备降点，选一条符合飞行要求的路线。"];
      if (detail?.startsWith("从当前")) return ["找路线", "从无人机现在的位置出发，找一条能绕开限制区域的路线。"];
      return ["找路线", "寻找能避开建筑和禁飞区、也符合任务要求的路线。"];
    }
    return ACTION_STEPS[action] ?? ["核对", "继续核对这项任务的飞行要求。"];
  }
  if (role === "thought") return content.startsWith("保持飞行当前位置")
    ? ["看位置", "记录无人机现在的位置，作为改航的起点。"]
    : ["看要求", "从指令中找出任务类型、地点和需要遵守的要求。"];
  const endpoints = content.match(/^\S+ (.+) → \S+ (.+)$/);
  if (endpoints) return ["地点匹配", `计划从${endpoints[1]}出发，飞往${endpoints[2]}。`];
  const route = content.match(/^规划成功，\d+ 个航点，([\d.]+) m。$/);
  if (route) return ["航线已生成", "航点已生成，路线符合飞行要求。"];
  return [content.includes("空域") || content.includes("避让") ? "规则核验" : "处理反馈", plainDispatchText(content)];
}

function altitudeText(limits) {
  const parts = [];
  if (limits.min_alt !== null) parts.push(`${limits.min_exclusive ? ">" : "≥"} ${number.format(limits.min_alt)} m`);
  if (limits.max_alt !== null) parts.push(`${limits.max_exclusive ? "<" : "≤"} ${number.format(limits.max_alt)} m`);
  return parts.join(" 且 ");
}

function planRequirements({ intent, waypoints, kpis }, zoneNames) {
  const { cruise, deadline_seconds, preferred_cruise, preferred_deadline_seconds } = intent.constraints;
  const rows = [];
  if (cruise) rows.push({ text: `巡航 ${altitudeText(cruise)}（场景高度）` });
  if (deadline_seconds !== null) rows.push({ text: `起飞后 ${number.format(deadline_seconds)} 秒内到达` });
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
  if (preferred_deadline_seconds !== null) {
    const missed = kpis.estimated_seconds > preferred_deadline_seconds;
    rows.push({ text: `到达时限偏好 ${number.format(preferred_deadline_seconds)} s${missed ? " · 预计超时" : ""}`, warning: missed });
  }
  return rows.length ? rows : [{ text: "采用默认空域与建筑避障规则" }];
}

export class AgentUI {
  constructor(dispatch, clearClarification) {
    this.mode = document.getElementById("mode");
    this.input = document.getElementById("query");
    this.form = document.getElementById("dispatch-form");
    this.messages = document.getElementById("agent-messages");
    this.mission = document.getElementById("active-mission");
    this.exchangeTemplate = document.getElementById("agent-exchange-template");
    this.planTemplate = document.getElementById("dispatch-plan-template");
    this.guide = document.getElementById("agent-welcome");
    this.help = document.getElementById("agent-help");
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
    this.help.addEventListener("click", () => this.showGuide(this.guide.hidden));
    for (const button of this.guide.querySelectorAll("[data-welcome-example]")) {
      button.addEventListener("click", () => {
        this.clear.click();
        this.setInput(PRESETS[button.dataset.welcomeExample]);
        this.showGuide(false);
        this.input.focus();
      });
    }
  }

  showGuide(show) {
    this.guide.hidden = !show;
    this.form.closest(".dispatch").toggleAttribute("data-guide", show);
    this.help.setAttribute("aria-expanded", String(show));
  }

  setInput(value) {
    this.input.value = value;
  }

  beginExchange() {
    this.showGuide(false);
    const element = this.exchangeTemplate.content.cloneNode(true).firstElementChild;
    const part = (name) => element.querySelector(`[data-exchange="${name}"]`);
    const exchange = { element, part, body: null, ok: false };
    part("trace").addEventListener("click", () => {
      const open = part("trace").getAttribute("aria-expanded") !== "true";
      part("trace").setAttribute("aria-expanded", String(open));
      part("summary").hidden = open;
      part("steps").hidden = !open;
      part("trace-title").textContent = open ? "返回摘要" : `展开 ${exchange.total} 步详情`;
    });
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
    const planned = body?.kpis?.compliant && (body.status === "planned" || Boolean(body.emergency_type));
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
      this.setTraceProgress(exchange, steps.length, index + 1);
      count.textContent = `${index + 1}/${steps.length}`;
      this.setTraceSummary(exchange, [[label, detail]]);
    }
    delete element.dataset.pending;
    if (steps.length) {
      count.textContent = `${steps.length}/${steps.length} ${ok && planned ? "通过" : "已处理"}`;
      if (ok && planned) element.dataset.passed = "";
    }
    if (planned && ok) {
      const { kpis, intent } = body;
      const corridor = CORRIDORS[body.corridor];
      const constraints = intent ? planRequirements(body, zoneNames) : [];
      const checked = constraints.find(({ text }) => text.includes("避让") || text.includes("时限"));
      this.setTraceSummary(exchange, [
        ["指令解析", intent
          ? `确认${intent.mission_type === "inspection" ? "巡检" : "配送"}任务 · ${intent.start_id} → ${intent.end_id}`
          : `重新研判当前机位与目的地 ${body.target_id}`],
        ["约束核验", checked?.text ?? `航层 ${corridor} · 避开禁飞区`],
        ["航线生成", `生成 ${decimal(kpis.distance_m)} m 避障三维航迹`],
      ]);
    } else if (body?.detail) {
      this.setTraceSummary(exchange, [[RESPONSE_LABELS[body.status] ?? "调度反馈", plainDispatchText(body.detail)]]);
    }
  }

  setClarification(active) {
    this.clear.hidden = !active;
    document.getElementById("submit-label").textContent = active ? "提交补答" : "规划航线";
    this.input.placeholder = active ? "例如：食堂" : "例如：从 VP01 巡检至 VP04";
  }

  setAvailable(available) {
    this.submit.disabled = !available;
    this.clear.disabled = !available;
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

  showPlan(result, zoneNames, { confirm, edit, locate }, running = false) {
    const card = this.planTemplate.content.cloneNode(true).firstElementChild;
    const part = (name) => card.querySelector(`[data-plan="${name}"]`);
    const { intent } = result;
    part("type").textContent = intent.mission_type === "inspection" ? "校园巡检任务" : "校园配送任务";
    part("id").textContent = "#MSN-" + String(++this.missionNumber).padStart(3, "0");
    this.plan = { card, part, locate, zoneNames };
    this.setPlanEndpoint("start", intent.start_id, result.start_name);
    this.setPlanEndpoint("end", intent.end_id, result.end_name);
    part("confirm").addEventListener("click", confirm);
    part("edit").addEventListener("click", edit);
    this.mission.replaceChildren(card);
    this.updatePlan(result, intent.end_id, result.end_name);
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
    part("corridor").textContent = CORRIDORS[result.corridor];
    part("compliance").textContent = kpis.compliant
      ? `${Math.round(kpis.compliance_percent)}% · 校验 ${zoneNames.size + (result.temporary_nofly_zone ? 1 : 0)} 区` : "未通过";
    part("clearance").textContent = kpis.min_clearance_m == null ? "—" : `≥ ${(Math.floor(kpis.min_clearance_m * 10) / 10).toFixed(1)} m`;
    part("clearance").title = "规划航段距最近建筑或禁飞区包络的最小净空";
    part("source").textContent = !intent ? "本地应急规划" : result.model_fallback ? "本地规则兜底"
      : result.mode === "live_llm" ? "LLM 语义校验" : "本地规则";
    part("kpis").textContent = `规划航程 ${decimal(kpis.distance_m)} m · 预计 ${decimal(kpis.estimated_seconds)} s`;
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
