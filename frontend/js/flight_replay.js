import { batteryAfterFlight, DroneSim } from "./drone_sim.js?v=4";

export function flightClock(seconds) {
  const whole = Math.floor(seconds);
  return [Math.floor(whole / 60), whole % 60].map((value) => String(value).padStart(2, "0")).join(":");
}

export class FlightReplay {
  constructor(map, { onFrame, onSegment, onMode, onOverview }) {
    this.map = map;
    this.onFrame = onFrame;
    this.onSegment = onSegment;
    this.onMode = onMode;
    this.onOverview = onOverview;
    this.panel = document.getElementById("flight-review");
    this.outcome = document.getElementById("review-outcome");
    this.list = document.getElementById("review-events");
    this.comparison = document.getElementById("review-comparison");
    this.sliderRow = document.getElementById("review-slider-row");
    this.slider = document.getElementById("replay-time");
    this.clock = document.getElementById("replay-clock");
    this.start = document.getElementById("replay-start");
    this.play = document.getElementById("replay-play");
    this.exit = document.getElementById("replay-exit");
    this.eventButtons = [];
    this.rate = 1;
    this.playing = false;
    this.start.addEventListener("click", () => { this.open(); this.slider.focus({ preventScroll: true }); });
    this.play.addEventListener("click", () => this.playing ? this.pause() : this.resume());
    this.exit.addEventListener("click", () => { this.close(); this.start.focus({ preventScroll: true }); });
    this.panel.addEventListener("toggle", () => { if (!this.panel.open) this.close(); });
    this.slider.addEventListener("input", () => {
      this.pause();
      this.seek(this.slider.valueAsNumber);
    });
  }

  get active() {
    return Boolean(this.sim);
  }

  setRecord(record) {
    this.record = record;
    this.outcome.textContent = record.outcome;
    this.eventButtons = record.events.map((event) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "review-event";
      button.setAttribute("aria-pressed", "false");
      const timestamp = document.createElement("time");
      timestamp.dateTime = `PT${event.at.toFixed(2)}S`;
      timestamp.textContent = flightClock(event.at);
      const name = document.createElement("strong");
      name.textContent = event.name;
      button.append(timestamp, name);
      if (event.detail) {
        const detail = document.createElement("small");
        detail.textContent = event.detail;
        button.append(detail);
      }
      button.addEventListener("click", () => {
        this.open();
        this.pause();
        this.onOverview();
        this.seek(event.at, event);
        this.map.focusPosition(event.position);
      });
      return button;
    });
    this.list.replaceChildren(...this.eventButtons.map((button) => {
      const item = document.createElement("li");
      item.append(button);
      return item;
    }));
    this.panel.hidden = false;
  }

  setAvailable(available) {
    this.start.disabled = !available;
    for (const button of this.eventButtons) button.disabled = !available;
  }

  open() {
    if (this.active || !this.record || this.start.disabled) return;
    this.segmentIndex = -1;
    this.event = null;
    this.sim = new DroneSim(this.map, (frame) => this.onFrame(frame, this.record, this.segment));
    this.slider.max = Math.ceil(this.record.duration * 10) / 10;
    this.sliderRow.hidden = this.play.hidden = this.exit.hidden = false;
    this.start.hidden = true;
    this.onMode(true);
    this.seek(0);
  }

  close() {
    if (!this.active) return;
    this.pause();
    this.sim = null;
    this.sliderRow.hidden = this.play.hidden = this.exit.hidden = this.comparison.hidden = true;
    this.start.hidden = false;
    for (const button of this.eventButtons) button.setAttribute("aria-pressed", "false");
    this.map.clearPadFocus();
    this.onMode(false);
  }

  seek(seconds, event = null, jumped = true, delta = 0) {
    this.seconds = Math.max(0, Math.min(seconds, this.record.duration));
    const index = event?.segmentIndex ?? this.record.segments.findLastIndex(({ start }) => start <= this.seconds);
    const segment = this.record.segments[index];
    const elapsed = this.seconds - segment.start;
    const battery = event?.battery ?? (this.seconds === this.record.duration ? this.record.battery
      : batteryAfterFlight(segment.battery, elapsed));
    const changedSegment = index !== this.segmentIndex;
    if (changedSegment || event !== this.event) {
      if (!event) this.map.clearPadFocus();
      this.event = event;
      this.segmentIndex = index;
      this.segment = segment;
      const priorPlan = event?.compare ? this.record.segments[index - 1].result.waypoints : null;
      this.map.setHazards(segment.volumes);
      this.map.drawReplayRoutes(this.record.segments.slice(0, index + 1), priorPlan);
      this.comparison.hidden = !priorPlan;
      this.onSegment(segment, index > 0);
      this.eventButtons.forEach((button, item) => button.setAttribute("aria-pressed", String(this.record.events[item] === event)));
    }
    if (changedSegment) {
      this.sim.load(segment.result.waypoints, segment.result.corridor, { battery, target: segment.target, elapsed });
    } else {
      this.sim.seek(elapsed, battery, jumped, delta);
    }
    const position = flightClock(this.seconds);
    this.slider.value = this.seconds === this.record.duration ? this.slider.max : this.seconds;
    if (this.slider.getAttribute("aria-valuetext") !== position) this.slider.setAttribute("aria-valuetext", position);
    const label = `${position} / ${flightClock(this.record.duration)}`;
    if (this.clock.textContent !== label) this.clock.textContent = label;
  }

  resume() {
    if (this.seconds === this.record.duration) this.seek(0);
    this.playing = true;
    this.previous = null;
    this.play.textContent = "暂停回放";
    this.request = requestAnimationFrame(this.tick);
  }

  pause() {
    if (this.request !== undefined) cancelAnimationFrame(this.request);
    this.request = undefined;
    this.playing = false;
    this.play.textContent = "播放回放";
  }

  tick = (now) => {
    const delta = this.previous === null ? 0 : Math.min((now - this.previous) / 1000, .25);
    this.previous = now;
    this.seek(this.seconds + delta * this.rate, null, false, delta);
    if (this.seconds >= this.record.duration) this.pause();
    else this.request = requestAnimationFrame(this.tick);
  };
}
