const DEMO_QUERY = "从星空广场起飞，依次途经星湖、星湖南路，最后到信息学部操场巡检，比较多条航线。";

let supportsDisplayTransition;
function canTransitionDisplay() {
  if (supportsDisplayTransition !== undefined) return supportsDisplayTransition;
  if (!globalThis.CSS?.supports?.("transition-behavior", "allow-discrete") || !document.body) {
    supportsDisplayTransition = false;
    return false;
  }
  const probe = document.createElement("div");
  probe.style.cssText = "transition: display 1s allow-discrete !important; display: block;";
  document.body.appendChild(probe);
  getComputedStyle(probe).display;
  probe.style.display = "none";
  supportsDisplayTransition = getComputedStyle(probe).display === "block";
  probe.remove();
  return supportsDisplayTransition;
}

function showElement(el) {
  el.removeAttribute("data-closing");
  el.hidden = false;
}

async function hideElement(el) {
  if (el.hidden) return;
  if (canTransitionDisplay()) {
    el.hidden = true;
  } else {
    el.setAttribute("data-closing", "");
  }
  const animations = el.getAnimations?.() ?? [];
  if (animations.length > 0) {
    await Promise.race([
      Promise.allSettled(animations.map((anim) => anim.finished)),
      new Promise((resolve) => setTimeout(resolve, 400)),
    ]);
  }
  if (el.hasAttribute("data-closing")) {
    el.removeAttribute("data-closing");
    el.hidden = true;
  }
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    function onAbort() {
      clearTimeout(timer);
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
    }
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

async function waitUntil(predicate, signal, timeoutMs = 240000) {
  const deadline = performance.now() + timeoutMs;
  while (performance.now() < deadline) {
    const value = predicate();
    if (value) return value;
    await sleep(120, signal);
  }
  throw new Error("Director step timed out");
}

export class DemoDirector {
  constructor(hooks) {
    this.hooks = hooks;
    this.banner = document.getElementById("director-banner");
    this.stepLabel = document.getElementById("director-step");
    this.titleLabel = document.getElementById("director-title");
    this.captionLabel = document.getElementById("director-caption");
    this.stopButton = document.getElementById("director-stop");
    this.toggleButton = document.getElementById("director-toggle");
    this.toggleLabel = document.getElementById("director-toggle-label");
    this.controller = null;
    this.savedAutoEmergency = null;

    this.stopButton?.addEventListener("click", () => {
      const restore = document.activeElement === this.stopButton;
      this.stop("已退出导播，当前仿真由你手动控制。");
      if (restore) this.toggleButton?.focus({ preventScroll: true });
    });
    this.toggleButton?.addEventListener("click", () => {
      if (this.active) this.stop("已停止导播，当前任务可继续手动操作。");
      else void this.start();
    });
  }

  get active() {
    return Boolean(this.controller);
  }

  setAvailable(available) {
    if (!this.toggleButton) return;
    this.toggleButton.disabled = !this.active && !available;
    this.toggleButton.toggleAttribute("data-running", this.active);
    if (this.toggleLabel) this.toggleLabel.textContent = this.active ? "停止导播" : "一键导播";
  }

  setChapter(step, title, caption) {
    if (!this.banner) return;
    this.stepLabel.textContent = `${step}/5`;
    this.titleLabel.textContent = title;
    this.captionLabel.textContent = caption;
    if (this.banner.hidden) showElement(this.banner);
  }

  stop(reason = null) {
    if (!this.controller) return;
    const activeController = this.controller;
    this.controller = null;
    activeController.abort();
    if (this.savedAutoEmergency !== null) {
      this.hooks.restoreAutoEmergency(this.savedAutoEmergency);
      this.savedAutoEmergency = null;
    }
    this.setAvailable(this.hooks.canStart());
    void hideElement(this.banner);
    if (reason) this.hooks.onStatus(reason);
  }

  async start() {
    if (this.active || !this.hooks.canStart()) return;
    const controller = new AbortController();
    const { signal } = controller;
    this.controller = controller;
    this.savedAutoEmergency = this.hooks.prepareStart();
    this.setAvailable(true);

    try {
      this.setChapter(1, "多方案空域调度", "解析有序飞越意图，同屏比选最短耗时与最大净空三维航线");
      await this.hooks.dispatchDemo(DEMO_QUERY);
      if (signal.aborted) return;

      const proposal = this.hooks.getProposal();
      if (proposal?.result?.options?.length >= 2) {
        await sleep(950, signal);
        this.hooks.previewCandidate(1);
        await sleep(1450, signal);
        this.hooks.previewCandidate(0);
        await sleep(900, signal);
      }
      const activeProposal = this.hooks.getProposal();
      if (activeProposal?.result?.options?.length) {
        await this.hooks.confirmCandidate(activeProposal.result.options[0].id);
      }
      if (this.hooks.getDraft()) {
        await sleep(750, signal);
        this.hooks.confirmDraft();
      }

      await waitUntil(() => this.hooks.getState().mission && this.hooks.getState().frame, signal, 8000);
      this.setChapter(2, "垂直起飞与跟随爬升", "脱离 0–30m 地面缓冲层，入弯预判横滚并汇入 60–90m 巡检走廊");
      this.hooks.setRate(2);
      await sleep(750, signal);
      this.hooks.setView("follow");

      await waitUntil(() => {
        const { frame } = this.hooks.getState();
        return frame && frame.leg?.phase === "cruise" && frame.leg.horizontal > 8 && (frame.groundSpeed ?? 0) > 5;
      }, signal);

      this.setChapter(3, "第一人称 AR-HUD 巡航", "地平线随转弯横滚联动，锁定景深虚拟航道框并实时监测障碍净空");
      this.hooks.setRate(1);
      this.hooks.setView("fpv");
      await sleep(3200, signal);

      await waitUntil(() => {
        const { frame } = this.hooks.getState();
        return frame && (frame.complete || this.hooks.canInjectWindGust());
      }, signal);

      let replanned = false;
      if (this.hooks.canInjectWindGust()) {
        this.setChapter(4, "突发禁飞区与实时改航", "前方注入阵风禁飞区阻断原航线，Agent 空中重规划并同屏对比绕飞轨迹");
        replanned = await this.hooks.triggerWindGust();
      }
      if (replanned) {
        await sleep(1400, signal);
        this.hooks.setView("overview");
        this.hooks.focusRoute();
        this.hooks.setRate(1);
        await sleep(4200, signal);
        this.hooks.setRate(4);
        await waitUntil(() => {
          const { frame } = this.hooks.getState();
          return frame && (frame.complete || frame.duration - frame.elapsed <= 12);
        }, signal);
      }

      const beforeApproach = this.hooks.getState().frame;
      if (beforeApproach && !beforeApproach.complete) {
        this.setChapter(5, "靶标进近与任务复盘", "第一人称对准 AR 降落十字靶标实时测距着陆，生成任务全流程时间轴");
        this.hooks.setView("fpv");
        this.hooks.setRate(2);
        await waitUntil(() => this.hooks.getState().frame?.complete, signal);
      }

      await sleep(650, signal);
      this.hooks.setView("overview");
      this.hooks.focusRoute();
      this.hooks.openReview();
      this.setChapter(5, "导播演示完成", "已展开最近任务记录，可点击事件定位或拖动时间轴回放");
      await sleep(2400, signal);
      this.stop("五幕工程演示已完成，可点击底部事件或切换回放查看细节。");
    } catch (error) {
      if (error?.name === "AbortError" || signal.aborted) return;
      console.error("DemoDirector error", error);
      this.stop("导播已结束，可继续手动操作。");
    }
  }
}
