const INK = "#b1ffce";
const MONO = "ui-monospace, Consolas, monospace";

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
    const x = width / 2;
    const y = height / 2;
    const spread = Math.max(104, Math.min(width * .178, 234));
    const pitch = (frame.pitch ?? 0) * 180 / Math.PI;
    const radius = Math.min(48, spread * .28);
    const alarm = !frame.onGround && this.clearance !== null && this.clearance < 12;
    const low = frame.battery < 18;

    this.label("VOXAERO-01", "FPV AR", x, Math.max(74, height * .145), INK);
    const readoutOffset = Math.min(85, height * .17);
    this.label("SPD", frame.speed.toFixed(1) + " m/s", x - spread, y - readoutOffset, INK);
    this.label("ALT", frame.coordinates.alt.toFixed(1) + " m", x + spread, y - readoutOffset, INK);
    this.label("BAT", Math.round(frame.battery) + "%", x - spread, y + readoutOffset, low ? "#ff9a93" : INK);
    this.label("CLEARANCE", this.clearance === null ? "—" : this.clearance.toFixed(1) + " m",
      x + spread, y + readoutOffset, alarm ? "#ff9a93" : INK);

    ctx.strokeStyle = INK;
    ctx.fillStyle = INK;
    ctx.font = "11px " + MONO;
    ctx.lineWidth = 1.15;
    ctx.shadowColor = "#061711";
    ctx.shadowBlur = 3;
    ctx.beginPath();
    ctx.moveTo(x - 28, y);
    ctx.lineTo(x - 11, y);
    ctx.lineTo(x, y + 6);
    ctx.lineTo(x + 11, y);
    ctx.lineTo(x + 28, y);
    ctx.stroke();
    for (let angle = -40; angle <= 40; angle += 5) {
      const py = y + (pitch - angle) * 4.2;
      if (Math.abs(py - y) > Math.min(110, height * .28) || Math.abs(py - y) < 12) continue;
      ctx.globalAlpha = angle === 0 ? .95 : .66;
      ctx.setLineDash(angle < 0 ? [4, 3] : []);
      ctx.beginPath();
      ctx.moveTo(x - radius, py);
      ctx.lineTo(x - 19, py);
      ctx.moveTo(x + 19, py);
      ctx.lineTo(x + radius, py);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.textAlign = "left";
      ctx.strokeStyle = "#061711";
      ctx.lineWidth = 2.8;
      ctx.strokeText(String(angle), x + radius + 5, py);
      ctx.strokeStyle = INK;
      ctx.lineWidth = 1.15;
      ctx.fillText(String(angle), x + radius + 5, py);
    }
    ctx.globalAlpha = 1;

    const heading = String(Math.round(frame.heading ?? 0) % 360).padStart(3, "0") + "°";
    this.label("HDG", heading, x, y - Math.min(142, height * .24), INK);
    if (alarm || low) {
      const blink = !frame.playing || this.reducedMotion.matches || Math.floor(performance.now() / 450) % 2 === 0;
      ctx.globalAlpha = blink ? 1 : .52;
      this.label("WARNING", [alarm && "净空距离过近", low && "低电量"].filter(Boolean).join("  /  "),
        x, Math.min(height - 104, y + Math.min(138, height * .24)), "#ffa49c");
      ctx.globalAlpha = 1;
    }
  }

  label(name, value, x, y, tint) {
    const ctx = this.context;
    ctx.shadowBlur = 0;
    ctx.textAlign = "center";
    ctx.font = "650 14px " + MONO;
    const width = Math.max(71, ctx.measureText(value).width + 19, name.length * 7 + 16);
    ctx.fillStyle = "#081b16db";
    ctx.strokeStyle = tint;
    ctx.lineWidth = .8;
    ctx.fillRect(x - width / 2, y - 17, width, 38);
    ctx.strokeRect(x - width / 2, y - 17, width, 38);
    ctx.fillStyle = tint;
    ctx.font = "10px " + MONO;
    ctx.fillText(name, x, y - 7);
    ctx.font = "650 14px " + MONO;
    ctx.fillText(value, x, y + 9);
  }
}
