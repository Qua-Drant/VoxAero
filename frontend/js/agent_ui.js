const PRESETS = {
  delivery: "中午高峰配送：将餐食从信息学部食堂送至测国重楼顶，避开图书馆静谧区",
  inspection: "夜间高空安防巡检：从信息学部起飞，沿 60–90m 巡检层前往图书馆西侧巡检点",
  emergency: "应急航线演示：从 VP02 沿 60–90m 巡检层前往 VP03",
};
const ROLES = { thought: "任务", action: "调用", observation: "结果" };

export class AgentUI {
  constructor(dispatch, clearClarification) {
    this.mode = document.getElementById("mode");
    this.input = document.getElementById("query");
    this.form = document.getElementById("dispatch-form");
    this.conversation = document.getElementById("conversation");
    this.feedback = document.getElementById("dispatch-status");
    this.submit = this.form.querySelector("#submit");
    this.clear = document.getElementById("clear-clarification");
    this.clear.addEventListener("click", clearClarification);
    this.presets = [...document.querySelectorAll("[data-preset]")];
    for (const button of this.presets) {
      button.addEventListener("click", () => {
        const preset = button.dataset.preset;
        this.input.value = PRESETS[preset];
        dispatch(this.input.value, { newTask: true, autoPlay: preset === "emergency" });
      });
    }
    this.form.addEventListener("submit", (event) => {
      event.preventDefault();
      const query = this.input.value.trim();
      if (query) dispatch(query);
      else this.status("请输入起降坪与任务要求。", true);
    });
    this.setupWelcome();
  }

  setupWelcome() {
    const panel = document.getElementById("agent-welcome");
    const help = document.getElementById("agent-help");
    const nativePopover = "popover" in HTMLElement.prototype;
    const hide = () => {
      if (nativePopover) panel.hidePopover();
      else panel.hidden = true;
    };
    const show = () => {
      if (nativePopover) panel.showPopover();
      else panel.hidden = false;
    };
    if (nativePopover) {
      panel.popover = "auto";
      panel.hidden = false;
    } else {
      help.setAttribute("aria-controls", panel.id);
      help.addEventListener("click", () => { if (panel.hidden) show(); else hide(); });
      panel.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          hide();
          help.focus();
        }
      });
    }
    document.getElementById("welcome-close").addEventListener("click", () => { hide(); help.focus(); });
    for (const button of panel.querySelectorAll("[data-welcome-example]")) {
      button.addEventListener("click", () => {
        this.clear.click();
        this.input.value = PRESETS[button.dataset.welcomeExample];
        hide();
        this.input.focus();
      });
    }
    let firstVisit = true;
    try {
      firstVisit = !sessionStorage.getItem("voxaero-welcome-shown");
      sessionStorage.setItem("voxaero-welcome-shown", "1");
    } catch { /* Storage may be unavailable in a private preview. */ }
    if (firstVisit) show();
  }

  setClarification(active) {
    this.clear.hidden = !active;
    this.submit.textContent = active ? "提交补答" : "规划航线";
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
    this.mode.title = mode ?? "";
  }

  status(message, error = false) {
    this.feedback.textContent = message;
    this.feedback.dataset.error = error;
  }

  message(kind, message, steps = []) {
    this.conversation.querySelector(".chat-empty")?.remove();
    const bubble = document.createElement("article");
    bubble.className = "message";
    bubble.dataset.kind = kind;
    const header = document.createElement("p");
    header.className = "message-header";
    header.textContent = { user: "操作员", agent: "VoxAero Agent", error: "调度异常" }[kind];
    const text = document.createElement("p");
    text.textContent = message;
    bubble.append(header, text);
    if (steps.length) {
      if (this.lastTrace) this.lastTrace.open = false;
      const panel = document.createElement("details");
      panel.className = "trace";
      panel.open = true;
      const summary = document.createElement("summary");
      summary.textContent = "调度过程 · " + steps.length + " 步";
      const list = document.createElement("ol");
      for (const { role, content, step } of steps) {
        const row = document.createElement("li");
        row.value = step;
        const title = document.createElement("span");
        title.textContent = ROLES[role] ?? role;
        const body = document.createElement("p");
        body.textContent = content;
        row.append(title, body);
        list.append(row);
      }
      panel.append(summary, list);
      bubble.append(panel);
      this.lastTrace = panel;
    }
    this.conversation.append(bubble);
    this.conversation.scrollTop = this.conversation.scrollHeight;
  }
}
