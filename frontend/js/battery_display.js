export function batteryReading(value) {
  const percent = Math.round(Math.min(100, Math.max(0, value)));
  const tone = percent <= 8 ? "critical" : percent < 20 ? "warn" : "normal";
  return { percent, tone, segments: Math.ceil(percent / 20) };
}

export function paintBattery(label, meter, { percent, tone, segments }) {
  const text = percent + "%";
  if (label.textContent === text) return;
  label.textContent = text;
  label.closest(".battery-reading").dataset.chargeTone = tone;
  Array.from(meter.children).forEach((bar, index) => bar.toggleAttribute("data-lit", index < segments));
}
