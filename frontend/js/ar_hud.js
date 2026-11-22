const CYAN = "#38bdf8";
const TEXT = "#f8fafc";
const MUTED = "#9baabd";
const SAFE = "#34d399";
const DANGER = "#ff858e";
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

function text(ctx, value, x, y, { color = TEXT, size = 13, weight = 650, align = "left", font = READOUT_FONT } = {}) {
  ctx.font = `${weight} ${size}px ${font}`;
  ctx.textAlign = align;
  ctx.fillStyle = color;
  ctx.fillText(value, x, y);
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
  const readoutHeight = compact ? 40 : 36;
  const gap = 8;
  plate(ctx, x, top - readoutHeight - gap, width, readoutHeight);
  if (compact) {
    text(ctx, name, x + width / 2, top - gap - 28, { color: MUTED, size: 10, align: "center", font: LABEL_FONT });
    text(ctx, value.toFixed(1) + unit, x + width / 2, top - gap - 12,
      { color: CYAN, size: 12, weight: 740, align: "center" });
  } else {
    const baseline = top - gap - readoutHeight / 2;
    const rightEdge = x + width - 9;
    text(ctx, name, x + 9, baseline, { color: MUTED, size: 11, font: LABEL_FONT });
    text(ctx, unit, rightEdge, baseline + 2, { color: MUTED, size: 11, align: "right", font: LABEL_FONT });
    text(ctx, value.toFixed(1), rightEdge - ctx.measureText(unit).width - 4, baseline,
      { color: CYAN, size: 17, weight: 740, align: "right" });
  }
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

function drawHeading(ctx, x, y, span, heading, compact) {
  const rise = compact ? 22 : 34;
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
  text(ctx, String(degrees).padStart(3, "0") + "°", x, y + 30,
    { color: CYAN, size: compact ? 13 : 15, weight: 720, align: "center" });
}

function drawPitch(ctx, x, y, width, height, pitch) {
  const radius = clamp(width * .11, 42, 142);
  const gap = clamp(radius * .38, 17, 44);
  const scale = clamp(height / 112, 3.8, 8);
  const clip = Math.min(height * .24, 172);
  ctx.save();
  ctx.strokeStyle = CYAN;
  ctx.lineWidth = 1.2;
  ctx.shadowColor = "#06141f";
  ctx.shadowBlur = 4;
  for (let angle = -40; angle <= 40; angle += 5) {
    const offset = (pitch - angle) * scale;
    if (Math.abs(offset) > clip || Math.abs(offset) < 13) continue;
    const ty = y + offset;
    const major = angle % 10 === 0;
    const arm = major ? radius : radius * .72;
    ctx.globalAlpha = (major ? .94 : .53) * (1 - .45 * Math.abs(offset) / clip);
    ctx.setLineDash(angle < 0 ? [5, 4] : []);
    line(ctx, [[x - arm, ty], [x - gap, ty]]);
    line(ctx, [[x + gap, ty], [x + arm, ty]]);
    ctx.setLineDash([]);
    if (major) {
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

function drawTelemetry(ctx, x, y, battery, clearance, low, alarm, compact) {
  const width = compact ? 248 : 306;
  const height = compact ? 33 : 42;
  plate(ctx, x, y, width, height, "#65bbd4c0");
  const middle = y + height / 2;
  const batteryWidth = compact ? 28 : 35;
  const start = x + 9;
  ctx.strokeStyle = low ? DANGER : CYAN;
  ctx.lineWidth = 1;
  ctx.strokeRect(start, middle - 6, batteryWidth, 12);
  line(ctx, [[start + batteryWidth + 2, middle - 3], [start + batteryWidth + 2, middle + 3]]);
  const segmentWidth = (batteryWidth - 8) / 4;
  for (let i = 0; i < 4; i++) {
    ctx.fillStyle = low ? DANGER : CYAN;
    ctx.globalAlpha = battery > i * 25 ? 1 : .19;
    ctx.fillRect(start + 2 + i * (segmentWidth + 1), middle - 4, segmentWidth, 8);
  }
  ctx.globalAlpha = 1;
  const textX = start + batteryWidth + 10;
  text(ctx, "BAT", textX, middle, { color: MUTED, size: compact ? 10 : 11, font: LABEL_FONT });
  text(ctx, Math.round(battery) + "%", textX + 25, middle,
    { color: low ? DANGER : CYAN, size: compact ? 13 : 16, weight: 750 });
  const divider = x + (compact ? 110 : 132);
  ctx.strokeStyle = "#9fc7e482";
  line(ctx, [[divider, y + 7], [divider, y + height - 7]]);
  text(ctx, "CLEARANCE", divider + 10, middle, { color: MUTED, size: compact ? 10 : 11, font: LABEL_FONT });
  text(ctx, clearance === null ? "—" : clearance.toFixed(1) + " m", x + width - 10, middle,
    { color: alarm ? DANGER : CYAN, size: compact ? 12 : 16, weight: 750, align: "right" });
}

export class FlightHUD {
  constructor(canvas) {
    this.canvas = canvas;
    this.context = canvas.getContext("2d");
    this.reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
    new ResizeObserver(() => this.draw()).observe(canvas);
  }

  update(active, frame, clearance) {
    this.canvas.hidden = !active;
    this.frame = frame;
    this.clearance = clearance;
    if (active) this.draw();
  }

  draw() {
    if (this.canvas.hidden || !this.frame) return;
    const { canvas, context: ctx, frame, clearance } = this;
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

    const compact = width < 630;
    const margin = compact ? 9 : 21;
    const centerX = width / 2;
    const centerY = height * .50;
    const tapeWidth = compact ? 60 : 114;
    const tapeHeight = Math.min(height * .47, 365);
    const alarm = !frame.onGround && clearance !== null && clearance < 12;
    const low = frame.battery < 18;

    const phase = frame.complete ? "已抵达" : frame.onGround ? "地面待命" : PHASE_LABELS[frame.leg?.phase] ?? "飞行";
    const call = compact ? "VOXAERO-01  ·  FPV" : "CALLSIGN  VOXAERO-01    /    FPV AR-HUD    /    " + phase;
    plate(ctx, margin, 13, compact ? 164 : 399, 31);
    text(ctx, call, margin + 10, 28, { size: compact ? 11 : 13 });
    const status = [alarm && "净空过近", low && "低电量"].filter(Boolean).join(" / ")
      || (frame.onGround ? phase : clearance === null ? "净空待测" : "净空正常");
    const tint = alarm || low ? DANGER : frame.onGround || clearance === null ? MUTED : SAFE;
    const liveBlink = frame.playing && !this.reducedMotion.matches && Math.floor(performance.now() / 450) % 2 !== 0;
    ctx.globalAlpha = (alarm || low) && liveBlink ? .68 : 1;
    const statusWidth = compact ? 103 : 140;
    const statusX = width - margin - statusWidth;
    plate(ctx, statusX, 45, statusWidth, 28, tint);
    text(ctx, status, statusX + statusWidth / 2, 59,
      { color: tint, size: compact ? 11 : 13, align: "center", font: LABEL_FONT });
    ctx.globalAlpha = 1;

    drawHeading(ctx, centerX, Math.max(77, height * .133), Math.min(width * .255, 220), frame.heading ?? 0, compact);
    drawPitch(ctx, centerX, centerY, compact ? width * .72 : width, height, (frame.pitch ?? 0) * 180 / Math.PI);
    drawTape(ctx, { x: margin, y: centerY, width: tapeWidth, height: tapeHeight, right: false,
      name: "AGL", value: frame.agl, unit: "m", step: 5, range: 35, compact });
    drawTape(ctx, { x: width - margin - tapeWidth, y: centerY, width: tapeWidth, height: tapeHeight, right: true,
      name: "SPD", value: frame.speed, unit: "m/s", step: 1, range: 7, compact });

    // Leave the bottom band clear for the flight controls.
    const telemetryY = height - (compact ? 135 : 110);
    drawTelemetry(ctx, margin, telemetryY, frame.battery, clearance, low, alarm, compact);
  }
}
