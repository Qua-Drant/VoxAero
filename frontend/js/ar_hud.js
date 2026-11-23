import { paintBattery } from "./battery_display.js";

const CYAN = "#38bdf8";
const TEXT = "#f8fafc";
const MUTED = "#9baabd";
const typography = getComputedStyle(document.documentElement);
const READOUT_FONT = typography.getPropertyValue("--font-mono").trim() || "monospace";
const LABEL_FONT = typography.getPropertyValue("--font-sans").trim() || "system-ui, sans-serif";
const PHASE_LABELS = { takeoff: "起飞", cruise: "巡航", landing: "降落", transition: "调整高度" };

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function line(ctx, points) {
  ctx.beginPath();
  ctx.moveTo(...points[0]);
  for (const point of points.slice(1)) ctx.lineTo(...point);
  ctx.stroke();
}

function text(ctx, value, x, y, { color = TEXT, size = 13, weight = 650, align = "left", font = READOUT_FONT, maxWidth } = {}) {
  ctx.font = `${weight} ${size}px ${font}`;
  ctx.textAlign = align;
  ctx.fillStyle = color;
  if (maxWidth === undefined) ctx.fillText(value, x, y);
  else ctx.fillText(value, x, y, maxWidth);
}

function plate(ctx, x, y, width, height, tint = "#79b5dea6") {
  const glass = ctx.createLinearGradient(0, y, 0, y + height);
  glass.addColorStop(0, "#161e2bd9");
  glass.addColorStop(1, "#090d15eb");
  ctx.fillStyle = glass;
  ctx.strokeStyle = tint;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(x, y, width, height, Math.min(9, height / 2));
  ctx.fill();
  ctx.stroke();
}

function drawTape(ctx, { x, y, width, height, right, name, value, unit, step, range, compact }) {
  const top = y - height / 2;
  const bottom = y + height / 2;
  const rail = right ? x + width - 13 : x + 13;
  const inward = right ? -1 : 1;
  const readoutHeight = compact ? 36 : 43;
  const readoutY = top - readoutHeight - 8;
  const center = x + width / 2;
  plate(ctx, x, readoutY, width, readoutHeight);
  text(ctx, name, center, readoutY + 10, {
    color: MUTED, size: compact ? 10 : 11, align: "center", font: LABEL_FONT, maxWidth: width - 8,
  });
  text(ctx, value.toFixed(1) + " " + unit, center, readoutY + readoutHeight * .69, {
    color: CYAN, size: compact ? 11 : 15, weight: 700, align: "center", maxWidth: width - 8,
  });
  plate(ctx, x, top, width, height, "#6a9ecb91");
  const inset = 13;
  const halfScale = height / 2 - inset;
  ctx.strokeStyle = "#a9d3ed99";
  line(ctx, [[rail, top + inset], [rail, bottom - inset]]);
  const first = Math.max(0, Math.ceil((value - range) / step) * step);
  for (let tick = first; tick <= value + range; tick += step) {
    const ty = y - ((tick - value) / range) * halfScale;
    const major = tick % (step * 2) === 0;
    ctx.strokeStyle = major ? "#bfe1f8c4" : "#9cc8e663";
    line(ctx, [[rail, ty], [rail + inward * (major ? 10 : 5), ty]]);
    if (major) {
      const align = right ? "left" : "right";
      text(ctx, String(tick), right ? x + 9 : x + width - 9, ty,
        { color: TEXT, size: compact ? 11 : 13, align });
    }
  }
  ctx.fillStyle = CYAN;
  ctx.shadowColor = "#13dcff9c";
  ctx.shadowBlur = 8;
  ctx.beginPath();
  ctx.moveTo(rail - inward, y - 6);
  ctx.lineTo(rail + inward * 11, y);
  ctx.lineTo(rail - inward, y + 6);
  ctx.closePath();
  ctx.fill();
  ctx.shadowBlur = 0;
}

function drawCompactReadout(ctx, x, y, name, value, unit) {
  const width = 76;
  plate(ctx, x - width / 2, y, width, 36, "#6a9ecb6b");
  text(ctx, name, x, y + 10, { color: MUTED, size: 10, align: "center", font: LABEL_FONT });
  text(ctx, value.toFixed(1) + " " + unit, x, y + 24, { color: CYAN, size: 12, align: "center" });
}

function drawHeading(ctx, x, y, span, heading, compact) {
  const rise = compact ? 12 : 25;
  const degrees = (Math.round(heading) % 360 + 360) % 360;
  const arcY = (offset) => y + rise * offset * offset;
  const arc = ctx.createLinearGradient(x - span, 0, x + span, 0);
  arc.addColorStop(0, "#0de7ff00");
  arc.addColorStop(.23, "#29e6ffad");
  arc.addColorStop(.5, CYAN);
  arc.addColorStop(.77, "#29e6ffad");
  arc.addColorStop(1, "#0de7ff00");
  ctx.strokeStyle = arc;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(x - span, y + rise);
  ctx.quadraticCurveTo(x, y - rise, x + span, y + rise);
  ctx.stroke();

  for (let tick = Math.ceil((heading - 40) / 10) * 10; tick <= heading + 40; tick += 10) {
    const offset = (tick - heading) / 40;
    const tx = x + offset * span;
    const ty = arcY(offset);
    ctx.strokeStyle = "#56ebffc7";
    ctx.lineWidth = 1;
    line(ctx, [[tx, ty], [tx - offset * 3, ty + 7]]);
  }
  ctx.fillStyle = CYAN;
  ctx.shadowColor = CYAN;
  ctx.shadowBlur = 7;
  ctx.beginPath();
  ctx.arc(x, y, 3, 0, Math.PI * 2);
  ctx.fill();
  ctx.shadowBlur = 0;
  ctx.strokeStyle = TEXT;
  line(ctx, [[x, y + 8], [x - 6, y + 16], [x + 6, y + 16], [x, y + 8]]);
  text(ctx, String(degrees).padStart(3, "0") + "°", x, y + (compact ? 26 : 30),
    { color: CYAN, size: compact ? 13 : 15, weight: 720, align: "center" });
}

function drawPitch(ctx, x, y, width, height, pitch, compact) {
  const radius = clamp(width * .105, 34, 104);
  const gap = clamp(radius * .38, 17, 44);
  const scale = clamp(height / 112, 3.8, 8);
  const clip = Math.min(height * (compact ? .17 : .21), 138);
  ctx.save();
  ctx.strokeStyle = CYAN;
  ctx.lineWidth = 1.2;
  ctx.shadowColor = "#06141f";
  ctx.shadowBlur = 4;
  for (let angle = -40; angle <= 40; angle += compact ? 10 : 5) {
    const offset = (pitch - angle) * scale;
    if (Math.abs(offset) > clip || Math.abs(offset) < 13) continue;
    const ty = y + offset;
    const major = angle % 10 === 0;
    const arm = major ? radius : radius * .72;
    ctx.globalAlpha = (major ? .78 : .38) * (1 - .5 * Math.abs(offset) / clip);
    ctx.setLineDash(angle < 0 ? [5, 4] : []);
    line(ctx, [[x - arm, ty], [x - gap, ty]]);
    line(ctx, [[x + gap, ty], [x + arm, ty]]);
    ctx.setLineDash([]);
    if (major && !compact) {
      const label = (angle > 0 ? "+" : "") + angle;
      const size = width < 630 ? 11 : 12;
      text(ctx, label, x - arm - 8, ty, { color: CYAN, align: "right", size });
      text(ctx, label, x + arm + 8, ty, { color: CYAN, size });
    }
  }
  ctx.globalAlpha = 1;
  ctx.lineWidth = 1.65;
  line(ctx, [[x - 36, y], [x - 14, y], [x - 8, y + 5]]);
  line(ctx, [[x + 36, y], [x + 14, y], [x + 8, y + 5]]);
  line(ctx, [[x, y - 5], [x, y + 5]]);
  ctx.restore();
}

function setText(node, value) {
  if (node.textContent !== value) node.textContent = value;
}

export class FlightHUD {
  constructor(canvas) {
    this.canvas = canvas;
    this.context = canvas.getContext("2d");
    this.overlay = canvas.parentElement;
    this.header = this.overlay.querySelector(".hud-header");
    this.phase = document.getElementById("hud-phase");
    this.alert = document.getElementById("hud-alert");
    this.readouts = document.getElementById("hud-readouts");
    this.battery = document.getElementById("hud-battery");
    this.meter = document.getElementById("hud-meter");
    this.clearance = document.getElementById("hud-clearance");

    const layout = new ResizeObserver(() => {
      this.headerBottom = this.header.offsetTop + this.header.offsetHeight;
      const rail = this.readouts.getBoundingClientRect();
      this.readoutsTop = rail.height ? rail.top - this.canvas.getBoundingClientRect().top : null;
      this.draw();
    });
    layout.observe(canvas);
    layout.observe(this.header);
    layout.observe(this.readouts.parentElement);
  }

  update(active, frame, clearance, emergencyPhase) {
    this.overlay.hidden = !active;
    this.readouts.hidden = !active;
    this.frame = frame;
    if (!active) return;

    const alarm = !frame.onGround && clearance !== null && clearance < 12;
    const low = !frame.onGround && frame.charge.tone !== "normal";
    const phase = emergencyPhase ?? (frame.complete ? "已停机" : frame.onGround ? "地面待命" : PHASE_LABELS[frame.leg?.phase] ?? "飞行");
    const warnings = [alarm && "净空过近", low && "低电量"].filter(Boolean);
    setText(this.phase, phase);
    setText(this.alert, warnings.join(" · ") || (frame.onGround ? "地面状态" : clearance === null ? "等待净空数据" : "净空正常"));
    this.alert.dataset.tone = low && frame.charge.tone === "critical" ? "critical"
      : warnings.length ? "warn" : frame.onGround || clearance === null ? "neutral" : "normal";
    this.alert.toggleAttribute("data-live-warning", Boolean(frame.playing && warnings.length));
    paintBattery(this.battery, this.meter, frame.charge);
    setText(this.clearance, clearance === null ? "—" : clearance.toFixed(1) + " m");
    this.readouts.toggleAttribute("data-clearance-alert", alarm);
    this.draw();
  }

  draw() {
    if (this.overlay.hidden || !this.frame) return;
    const { canvas, context: ctx, frame } = this;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height) return;
    const ratio = Math.min(devicePixelRatio || 1, 2);
    const pixelWidth = Math.round(width * ratio);
    const pixelHeight = Math.round(height * ratio);
    if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
    }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round";
    ctx.lineCap = "round";

    const compact = width < 570 || height < 500;
    const centerX = width / 2;
    const centerY = height * .48;
    const headerBottom = this.headerBottom || 48;
    const headingY = Math.max(headerBottom + (compact ? 14 : 28), height * .13);
    const instruments = [
      { name: "机位离地", value: frame.agl, unit: "m", step: 10, range: 40 },
      { name: "地速", value: frame.groundSpeed, unit: "m/s", step: 2, range: 8 },
    ];

    const headingSpan = compact ? Math.max(25, Math.min(width / 2 - 91, width * .21)) : Math.min(width * .22, 208);
    drawHeading(ctx, centerX, headingY, headingSpan, frame.heading ?? 0, compact);
    drawPitch(ctx, centerX, centerY, compact ? width * .83 : width, height, (frame.pitch ?? 0) * 180 / Math.PI, compact);
    if (compact) {
      drawCompactReadout(ctx, 49, headerBottom + 12, instruments[0].name, frame.agl, "m");
      drawCompactReadout(ctx, width - 49, headerBottom + 12, instruments[1].name, frame.groundSpeed, "m/s");
      return;
    }
    const tapeWidth = Math.min(width * .11, 92);
    const tapeHeight = Math.min(height * .33, 264);
    const tapeY = Math.min(centerY, (this.readoutsTop ?? height) - 16 - tapeHeight / 2);
    drawTape(ctx, { ...instruments[0], x: 16, y: tapeY, width: tapeWidth, height: tapeHeight, right: false, compact });
    drawTape(ctx, { ...instruments[1], x: width - 16 - tapeWidth, y: tapeY, width: tapeWidth, height: tapeHeight, right: true, compact });
  }
}
