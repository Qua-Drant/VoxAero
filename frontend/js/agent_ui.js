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
const MAX_VISIBLE_MESSAGES = 18;
const CORRIDORS = { delivery: "配送层 30–60 m", inspection: "巡检层 60–90 m" };
const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 6 });
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function plainDispatchText(content) {
  return content
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
    if (action === "校验空域") return ["查限制", `检查可用的巡航高度（${detail}），确认哪些地方要绕开。`];
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
  if (endpoints) return ["核对", `计划从${endpoints[1]}出发，飞往${endpoints[2]}。`];
  const route = content.match(/^规划成功，\d+ 个航点，([\d.]+) m。$/);
  if (route) return ["结果", `找到了约 ${route[1]} 米的路线。`];
  return ["结果", plainDispatchText(content)];
}

function formatPayload(payload) {
  if (typeof payload === "string") return payload || "空正文";
  return JSON.stringify(payload, (key, value) => key === "clarification_token" && value ? "[会话令牌已隐藏]" : value, 2);
}

function stage(element, state) {
  element.dataset.state = state;
  if (state === "active") element.setAttribute("aria-current", "step");
  else element.removeAttribute("aria-current");
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
    this.conversation = document.getElementById("conversation");
    this.messages = document.getElementById("agent-messages");
    this.exchangeTemplate = document.getElementById("agent-exchange-template");
    this.planTemplate = document.getElementById("dispatch-plan-template");
    this.guide = document.getElementById("agent-welcome");
    this.help = document.getElementById("agent-help");
    this.feedback = document.getElementById("dispatch-status");
    this.submit = this.form.querySelector("#submit");
    this.clear = document.getElementById("clear-clarification");
    this.clear.addEventListener("click", clearClarification);
    this.presets = [...document.querySelectorAll("[data-preset]")];
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
          this.fitInput();
        }
      } else if (!event.shiftKey) {
        event.preventDefault();
        if (!event.repeat && !this.submit.disabled) this.form.requestSubmit(this.submit);
      }
    });
    this.setupGuide();
    this.setupInputSizing();
  }

  setupGuide() {
    this.help.addEventListener("click", () => {
      const show = this.guide.hidden;
      this.showGuide(show);
      if (show) this.conversation.scrollTop = 0;
    });
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
    this.help.setAttribute("aria-expanded", String(show));
  }

  setupInputSizing() {
    this.nativeFieldSizing = CSS.supports("field-sizing", "content");
    if (this.nativeFieldSizing) return;
    this.input.addEventListener("input", () => this.fitInput());
    let width;
    new ResizeObserver(([entry]) => {
      const nextWidth = entry.contentRect.width;
      if (nextWidth === width) return;
      width = nextWidth;
      this.fitInput();
    }).observe(this.form);
    this.fitInput();
  }

  fitInput() {
    if (this.nativeFieldSizing) return;
    this.input.style.blockSize = "auto";
    const borders = this.input.offsetHeight - this.input.clientHeight;
    this.input.style.blockSize = (this.input.scrollHeight + borders) + "px";
  }

  setInput(value) {
    this.input.value = value;
    this.fitInput();
  }

  beginDispatch(query) {
    this.showGuide(false);
    return this.message("user", query);
  }

  finishDispatch(prompt, exchange, replaceThread) {
    if (replaceThread) this.messages.replaceChildren(prompt, exchange.element);
  }

  beginExchange(path, payload) {
    if (this.lastTrace) this.lastTrace.open = false;
    const element = this.exchangeTemplate.content.cloneNode(true).firstElementChild;
    const part = (name) => element.querySelector(`[data-exchange="${name}"]`);
    part("endpoint").textContent = `POST ${path}`;
    part("request").textContent = formatPayload(payload);
    this.lastTrace = part("trace");
    this.appendMessage(element);
    return {
      element, part,
      receive: (response, body) => {
        const label = RESPONSE_LABELS[body?.status] ?? (body?.emergency_type ? "应急航线已规划" : "");
        const status = part("status");
        status.textContent = "已收到回复";
        status.dataset.outcome = label || (response.ok ? "已收到回复" : "这次请求未成功");
        part("http").textContent = `HTTP ${response.status}`;
        status.toggleAttribute("data-error", !response.ok && body?.status !== "needs_clarification");
        part("response").textContent = formatPayload(body);
        part("receive").textContent = "已收到回复";
        stage(part("receive"), "done");
      },
      fail: (message) => {
        part("status").textContent = "连接失败";
        part("status").setAttribute("data-error", "");
        part("http").textContent = "未收到 HTTP 回复";
        part("response").textContent = message;
        part("receive").textContent = "没有收到回复";
        stage(part("receive"), "error");
      },
    };
  }

  async revealSteps({ part }, steps = []) {
    const panel = part("trace");
    const review = part("review");
    const status = part("status");
    if (panel.open || part("steps").childElementCount) return;
    if (!steps.length) {
      review.textContent = "没有处理步骤";
      if (status.dataset.outcome) status.textContent = status.dataset.outcome;
      return;
    }
    panel.hidden = false;
    panel.open = true;
    stage(review, "active");
    part("trace-title").textContent = `调度过程 · ${steps.length} 步`;
    const list = part("steps");
    // Keep the readout paced even when the reply arrives in a single frame.
    const interval = Math.min(440, 3600 / Math.max(1, steps.length - 1));
    for (const [index, step] of steps.entries()) {
      await delay(index === 0 ? 260 : interval);
      if (!panel.isConnected) return;
      const [label, detail] = readableStep(step);
      const row = document.createElement("li");
      row.value = step.step;
      const title = document.createElement("span");
      title.textContent = label;
      const body = document.createElement("p");
      body.textContent = detail;
      row.append(title, body);
      const atEnd = this.conversation.scrollHeight - this.conversation.scrollTop - this.conversation.clientHeight < 36;
      list.append(row);
      if (atEnd && panel.open && panel === this.lastTrace) this.scrollToLatest();
    }
    stage(review, "done");
    if (status.dataset.outcome) status.textContent = status.dataset.outcome;
  }

  scrollToLatest() {
    this.conversation.scrollTop = this.conversation.scrollHeight;
  }

  setClarification(active) {
    this.clear.hidden = !active;
    this.submit.textContent = active ? "提交补答" : "规划航线";
    this.input.placeholder = active ? "例如：食堂" : "例如：从 VP01 巡检至 VP04";
    this.fitInput();
  }

  setAvailable(available) {
    this.submit.disabled = !available;
    this.clear.disabled = !available;
    for (const button of this.presets) button.disabled = !available;
  }

  setMode(mode) {
    this.mode.textContent = { live_llm: "模型模式", fallback_mock: "本地规则" }[mode] ?? "服务离线";
    this.mode.dataset.tone = mode ? "normal" : "error";
    this.mode.title = mode ?? "";
  }

  status(message, error = false) {
    this.feedback.textContent = message;
    this.feedback.dataset.error = error;
  }

  showPlan(result, zoneNames, { confirm, edit, locate }, exchange) {
    const card = this.planTemplate.content.cloneNode(true).firstElementChild;
    const part = (name) => card.querySelector(`[data-plan="${name}"]`);
    const { intent, kpis } = result;
    part("type").textContent = intent.mission_type === "inspection" ? "巡检任务" : "配送任务";
    part("corridor").textContent = CORRIDORS[result.corridor] + "（场景高度）";
    for (const [key, id, name, role] of [
      ["start", intent.start_id, result.start_name, "起点"],
      ["end", intent.end_id, result.end_name, "终点"],
    ]) {
      const button = part(key);
      button.textContent = `${id} · ${name}`;
      button.setAttribute("aria-label", `定位${role} ${button.textContent}`);
      button.addEventListener("click", () => locate(id));
    }
    for (const { text, warning } of planRequirements(result, zoneNames)) {
      const row = document.createElement("li");
      row.textContent = text;
      row.toggleAttribute("data-warning", Boolean(warning));
      part("constraints").append(row);
    }
    part("source").textContent = result.model_fallback ? "本地规则 · 模型调用失败后兜底"
      : result.mode === "live_llm" ? "接入模型 · 已校验任务参数" : "本地规则";
    part("kpis").textContent = `${number.format(kpis.distance_m)} m · 预计 ${number.format(kpis.estimated_seconds)} s · `
      + (kpis.compliant ? "模拟空域校验通过" : "模拟空域校验未通过");
    part("confirm").addEventListener("click", confirm);
    part("edit").addEventListener("click", edit);
    part("exchange").addEventListener("click", () => {
      const details = exchange.part("trace");
      details.open = true;
      const summary = details.querySelector("summary");
      summary.focus({ preventScroll: true });
      this.conversation.scrollTop += exchange.element.getBoundingClientRect().top
        - this.conversation.getBoundingClientRect().top;
    });
    this.plan = card;
    this.message("agent", card);
    // Keep the route summary in view when the scrollable panel is short.
    this.conversation.scrollTop = card.closest(".message").getBoundingClientRect().top
      - this.conversation.getBoundingClientRect().top + this.conversation.scrollTop;
  }

  finishPlan(label) {
    if (!this.plan) return;
    this.plan.querySelector('[data-plan="state"]').textContent = label;
    this.plan.querySelector('[data-plan="actions"]').hidden = true;
    for (const button of this.plan.querySelectorAll(".plan-location")) button.disabled = true;
    this.plan = null;
  }

  message(kind, message) {
    const bubble = document.createElement("article");
    bubble.className = "message";
    bubble.dataset.kind = kind;
    const header = document.createElement("p");
    header.className = "message-header";
    header.textContent = { user: "操作员", agent: "VoxAero Agent", error: "调度异常" }[kind];
    bubble.append(header);
    if (typeof message === "string") {
      const text = document.createElement("p");
      text.textContent = message;
      bubble.append(text);
    } else {
      bubble.append(message);
    }
    this.appendMessage(bubble);
    return bubble;
  }

  appendMessage(bubble) {
    this.messages.append(bubble);
    if (this.messages.childElementCount > MAX_VISIBLE_MESSAGES) this.messages.firstElementChild.remove();
    this.scrollToLatest();
  }
}
