const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1, minimumFractionDigits: 1 });

export function renderAirspaceCheck(container, check) {
  container.replaceChildren();
  container.hidden = !check;
  if (!check) return;
  const disclosure = document.createElement("details");
  const title = document.createElement("summary");
  title.className = "status-label";
  title.dataset.tone = check.status === "source_checked" ? "normal" : "neutral";
  title.textContent = check.status === "source_checked" ? "外部空域快照已复核" : "外部空域待核验";
  const description = document.createElement("p");
  description.textContent = check.detail;
  disclosure.append(title, description);
  if (check.source_name) {
    const note = document.createElement("p");
    note.textContent = `${check.source_name} · 版本 ${check.source_version}`;
    if (check.valid_until) note.append(" · 有效至 " + new Intl.DateTimeFormat("zh-CN", {
      dateStyle: "short", timeStyle: "short",
    }).format(new Date(check.valid_until)));
    disclosure.append(note);
  }
  container.append(disclosure);
}

export function spatialCandidateForm(result, callbacks) {
  const form = document.createElement("form");
  form.className = "candidate-form";
  form.setAttribute("aria-labelledby", "spatial-candidates-heading");
  const header = document.createElement("h3");
  header.id = "spatial-candidates-heading";
  header.tabIndex = -1;
  header.textContent = "候选起降点";
  const fieldset = document.createElement("fieldset");
  const legend = document.createElement("legend");
  legend.textContent = "选择一组起终点";
  fieldset.append(legend);
  const reason = document.createElement("p");
  reason.className = "candidate-reason";
  const airspace = document.createElement("div");
  airspace.className = "airspace-readback";
  const rejected = document.createElement("details");
  rejected.className = "candidate-rejected";
  const rejectedTitle = document.createElement("summary");
  rejectedTitle.textContent = "查看筛选说明";
  const rejectedText = document.createElement("p");
  rejectedText.textContent = Object.entries(result.rejected).map(([key, count]) => `${key} ${count} 处`).join(" · ")
    || "通过场景范围、障碍、地形和航线检查。";
  rejected.append(rejectedTitle, rejectedText);

  const actions = document.createElement("div");
  actions.className = "plan-actions";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "返回修改";
  cancel.addEventListener("click", callbacks.cancel);
  const use = document.createElement("button");
  use.type = "submit";
  use.textContent = "采用候选";
  actions.append(cancel, use);
  form.append(header, fieldset, reason, airspace, rejected, actions);

  function preview(option) {
    reason.textContent = option.recommendation;
    renderAirspaceCheck(airspace, option.airspace_check);
    callbacks.preview(option);
  }

  result.options.forEach((option, index) => {
    const label = document.createElement("label");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "candidate";
    radio.value = option.id;
    radio.checked = index === 0;
    radio.addEventListener("change", () => preview(option));
    const content = document.createElement("span");
    const route = document.createElement("strong");
    route.textContent = `${option.start.name} → ${option.end.name}`;
    const measurements = document.createElement("small");
    measurements.textContent = `${number.format(option.kpis.distance_m)} 米 · ${number.format(option.kpis.estimated_seconds)} 秒`;
    content.append(route, measurements);
    label.append(radio, content);
    fieldset.append(label);
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const option = new FormData(form).get("candidate");
    if (!option) return;
    fieldset.disabled = cancel.disabled = use.disabled = true;
    try {
      await callbacks.confirm(option);
    } finally {
      fieldset.disabled = cancel.disabled = use.disabled = false;
    }
  });
  preview(result.options[0]);
  return form;
}
