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
  const compare = result.choice_type === "routes";
  header.textContent = compare ? "候选航线" : "候选起降点";
  const fieldset = document.createElement("fieldset");
  const legend = document.createElement("legend");
  legend.textContent = compare ? `比较 ${result.options.length} 条通过检查的方案` : "选择起终点及对应航线";
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
  rejectedText.textContent = Object.entries(result.rejected).map(([key, count]) => `${key} ${count} 次`).join(" · ")
    || "通过场景范围、障碍、地形和航线检查。";
  rejected.append(rejectedTitle, rejectedText);

  const actions = document.createElement("div");
  actions.className = "plan-actions";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "action-button";
  cancel.textContent = "返回修改";
  cancel.addEventListener("click", callbacks.cancel);
  const use = document.createElement("button");
  use.type = "submit";
  use.className = "action-button primary";
  use.textContent = compare ? "采用航线" : "采用候选";
  actions.append(cancel, use);
  const itinerary = document.createElement("p");
  itinerary.className = "candidate-itinerary";
  form.append(header, fieldset, itinerary, reason, airspace, rejected, actions);

  function preview(option) {
    const offsets = [
      option.start.offset_m > 0 ? `起飞点距${option.start.reference_name} ${number.format(option.start.offset_m)} m` : null,
      option.end.offset_m > 0 ? `落点距${option.end.reference_name} ${number.format(option.end.offset_m)} m` : null,
    ].filter(Boolean);
    const layer = option.corridor === "inspection" ? "60–90 m" : "30–60 m";
    reason.textContent = [`巡航航层 ${layer}`, option.planning_note, ...offsets].filter(Boolean).join(" · ");
    const visits = option.route_visits ?? [];
    itinerary.hidden = !visits.length;
    itinerary.textContent = "依序飞越 " + visits.map(visit => visit.name).join(" → ");
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
    const head = document.createElement("span");
    head.className = "candidate-head";
    const route = document.createElement("strong");
    const endpoints = `${option.start.name} → ${option.end.name}`;
    route.textContent = compare ? option.route_label : endpoints;
    route.title = endpoints;
    head.append(route);
    if (option.highlights?.length) {
      const badges = document.createElement("span");
      badges.className = "candidate-badges";
      for (const tag of option.highlights) {
        const badge = document.createElement("em");
        badge.className = "candidate-badge";
        badge.dataset.kind = tag === "最大净空" ? "safe" : tag === "最短耗时" ? "fast" : "eco";
        badge.textContent = tag;
        badges.append(badge);
      }
      head.append(badges);
    }
    const measurements = document.createElement("small");
    const kpis = option.kpis;
    const clearanceText = kpis.min_clearance_m == null ? "净空待估" : `净空 ≥ ${number.format(kpis.min_clearance_m)} m`;
    measurements.textContent = `${number.format(kpis.distance_m)} m · 预计 ${number.format(kpis.estimated_seconds)} s · ${clearanceText} · 耗电 ${number.format(kpis.estimated_battery_percent)}%`;
    if (compare) {
      const location = document.createElement("small");
      location.className = "candidate-endpoints";
      location.textContent = endpoints;
      location.title = endpoints;
      content.append(head, location, measurements);
    } else content.append(head, measurements);
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
