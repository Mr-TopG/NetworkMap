/* Map regions are annotations: they never configure VLANs on equipment. */
window.createNetworkMapAreas = function createNetworkMapAreas(deps) {
  "use strict";
  const { getState, isEditing, getTransform, worldPoint, svgEl, escapeHtml,
    api, applyState, applyTransform, askConfirmation, setBusy, toast, reportError, showMap } = deps;
  const $ = selector => document.querySelector(selector);
  const svg = $("#topology");
  const layer = $("#areaLayer");
  const colors = new Set(["blue", "green", "amber", "violet", "gray"]);
  let selectedId = null;
  let drag = null;
  let saving = false;
  const areas = () => getState().areas || [];
  const find = id => areas().find(area => String(area.id) === String(id));
  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

  function draw(area) {
    const selected = isEditing() && String(area.id) === selectedId;
    const group = svgEl("g", {
      class: `map-area area-${colors.has(area.color) ? area.color : "blue"}${selected ? " selected" : ""}`,
      "data-area-id": area.id, transform: `translate(${area.x} ${area.y})`,
      role: isEditing() ? "button" : "img",
      "aria-label": `${area.label}${area.vlan_id == null ? "" : `, VLAN ${area.vlan_id}`}${isEditing() ? ". Click or press Enter to edit; drag to move." : ""}`
    });
    if (isEditing()) group.setAttribute("tabindex", "0");
    const title = svgEl("title");
    title.textContent = [area.label, area.vlan_id == null ? "" : `VLAN ${area.vlan_id}`].filter(Boolean).join(" · ");
    group.append(title);
    const ellipse = area.shape === "ellipse";
    group.append(svgEl(ellipse ? "ellipse" : "rect", ellipse
      ? { cx: area.width / 2, cy: area.height / 2, rx: area.width / 2, ry: area.height / 2, class: "area-shape" }
      : { x: 0, y: 0, width: area.width, height: area.height, rx: 3, class: "area-shape" }));
    const text = svgEl("text", {
      x: ellipse ? area.width / 2 : 12, y: ellipse ? Math.max(20, area.height * .2) : 22,
      "text-anchor": ellipse ? "middle" : "start", class: "area-label"
    });
    const label = `${area.label}${area.vlan_id == null ? "" : ` · VLAN ${area.vlan_id}`}`;
    const length = Math.max(2, Math.floor((area.width * (ellipse ? .65 : 1) - 24) / 7.5));
    text.textContent = label.length > length ? `${label.slice(0, length - 1)}…` : label;
    group.append(text);
    if (selected) {
      group.append(svgEl("rect", { x: 0, y: 0, width: area.width, height: area.height, class: "area-resize-bounds" }));
      group.append(svgEl("rect", { x: area.width - 7, y: area.height - 7, width: 14, height: 14, class: "area-resize-handle", "data-area-resize": "true" }));
    }
    return group;
  }

  function renderMap() {
    // A server refresh cancels a gesture rather than overwriting concurrent edits.
    if (drag && (drag.revision !== getState().revision || !isEditing())) cancelDrag();
    if (!find(selectedId)) selectedId = null;
    layer.replaceChildren(...areas().map(area => draw(drag?.id === String(area.id) ? drag.preview : area)));
  }

  function renderTable() {
    $("#areaTableBody").innerHTML = areas().map(area => `<tr data-area-id="${escapeHtml(area.id)}"><td><strong>${escapeHtml(area.label)}</strong></td><td>${area.vlan_id ?? "—"}</td><td>${area.shape === "ellipse" ? "Ellipse / circle" : "Rectangle / square"}</td><td>${area.width} × ${area.height}</td><td><div class="row-actions area-row-actions"><button data-area-action="locate" title="Show area on map">Show</button><button data-area-action="edit" title="Edit area">Edit</button><button class="danger-action" data-area-action="delete" title="Remove area">Remove</button></div></td></tr>`).join("");
    $("#areaTableEmpty").hidden = areas().length > 0;
  }

  function openDialog(id = null) {
    if (saving) return;
    const area = id ? find(id) : null;
    if (id && !area) return;
    const bounds = svg.getBoundingClientRect();
    const center = bounds.width ? worldPoint(bounds.left + bounds.width / 2, bounds.top + bounds.height / 2) : { x: 0, y: 0 };
    $("#areaForm").reset();
    $("#areaId").value = area?.id || "";
    $("#areaLabel").value = area?.label || "";
    $("#areaShape").value = area?.shape || "rectangle";
    $("#areaVlan").value = area?.vlan_id ?? "";
    $("#areaColor").value = area?.color || "blue";
    $("#areaWidth").value = area?.width ?? 320;
    $("#areaHeight").value = area?.height ?? 220;
    $("#areaX").value = area?.x ?? Math.round(center.x - 160);
    $("#areaY").value = area?.y ?? Math.round(center.y - 110);
    $("#areaEqualSides").checked = Boolean(area && area.width === area.height);
    $("#areaDialogTitle").textContent = area ? "Edit area" : "Add an area";
    $("#areaSubmit").textContent = area ? "Save changes" : "Add area";
    $("#areaDelete").hidden = !area;
    $("#areaForm").dataset.baseRevision = String(getState().revision ?? 0);
    $("#areaDialog").showModal();
    $("#areaLabel").focus();
  }

  async function submit(event) {
    if (event.submitter?.value === "cancel") return;
    event.preventDefault();
    if (saving || !event.currentTarget.reportValidity()) return;
    const id = $("#areaId").value;
    const payload = {
      label: $("#areaLabel").value.trim(), shape: $("#areaShape").value, color: $("#areaColor").value,
      vlan_id: $("#areaVlan").value === "" ? null : Number($("#areaVlan").value),
      x: Number($("#areaX").value), y: Number($("#areaY").value),
      width: Number($("#areaWidth").value), height: Number($("#areaHeight").value)
    };
    const existingIds = new Set(areas().map(area => String(area.id)));
    saving = true; setBusy($("#areaSubmit"), true, "Saving…");
    try {
      const state = await api(id ? `/api/areas/${encodeURIComponent(id)}` : "/api/areas", {
        method: id ? "PATCH" : "POST", body: JSON.stringify(payload),
        expectedRevision: Number(event.currentTarget.dataset.baseRevision)
      });
      selectedId = id || String(state.areas.find(area => !existingIds.has(String(area.id)))?.id || "");
      $("#areaDialog").close(); applyState(state, { fit: !id });
      toast(id ? "Area updated" : "Area added", payload.label);
    } catch (error) { reportError("Could not save area", error); }
    finally { saving = false; setBusy($("#areaSubmit"), false); }
  }

  function remove(id) {
    const area = find(id);
    if (!area || saving) return;
    const revision = getState().revision;
    askConfirmation({ title: `Remove ${area.label}?`, message: "Only this area is removed. Devices and connections inside it are kept.", busyLabel: "Removing…", action: async () => {
      const state = await api(`/api/areas/${encodeURIComponent(id)}`, { method: "DELETE", expectedRevision: revision });
      if ($("#areaDialog").open) $("#areaDialog").close();
      if (selectedId === String(id)) selectedId = null;
      applyState(state); toast("Area removed", area.label);
    } });
  }

  function focus(id) {
    const area = find(id);
    if (!area) return;
    selectedId = String(id); showMap();
    requestAnimationFrame(() => {
      const bounds = svg.getBoundingClientRect();
      const transform = getTransform();
      transform.scale = Math.max(.001, Math.min(1, (bounds.width - 80) / area.width, (bounds.height - 80) / area.height));
      transform.x = bounds.width / 2 - (area.x + area.width / 2) * transform.scale;
      transform.y = bounds.height / 2 - (area.y + area.height / 2) * transform.scale;
      applyTransform(); renderMap();
    });
  }

  function cancelDrag() {
    const pointerId = drag?.pointerId;
    drag = null;
    svg.classList.remove("dragging");
    if (pointerId != null && svg.hasPointerCapture?.(pointerId)) svg.releasePointerCapture(pointerId);
  }

  async function persist(id, geometry, revision) {
    saving = true;
    try {
      applyState(await api(`/api/areas/${encodeURIComponent(id)}`, {
        method: "PATCH", body: JSON.stringify(geometry), expectedRevision: revision
      }));
    } catch (error) { renderMap(); reportError("Area position not saved", error); }
    finally { saving = false; }
  }

  svg.addEventListener("pointerdown", event => {
    const target = event.target.closest?.(".map-area");
    if (!target || !isEditing() || event.button !== 0) return;
    event.stopImmediatePropagation(); event.preventDefault();
    if (saving) return;
    const original = find(target.dataset.areaId);
    if (!original) return;
    selectedId = String(original.id);
    drag = { id: selectedId, original, preview: { ...original }, revision: getState().revision,
      pointerId: event.pointerId, start: worldPoint(event.clientX, event.clientY),
      resize: Boolean(event.target.closest("[data-area-resize]")), moved: false };
    renderMap(); svg.classList.add("dragging");
    svg.setPointerCapture?.(event.pointerId);
  }, true);

  svg.addEventListener("pointermove", event => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    event.stopImmediatePropagation();
    const point = worldPoint(event.clientX, event.clientY);
    const dx = point.x - drag.start.x, dy = point.y - drag.start.y;
    drag.moved ||= Math.hypot(dx, dy) * getTransform().scale > 3;
    if (!drag.moved) return;
    const grid = getState().settings.snap_to_grid ? Number(getState().settings.grid_size) || 20 : 1;
    const snap = value => Math.round(value / grid) * grid;
    const next = { ...drag.original };
    if (drag.resize) {
      next.width = clamp(snap(next.width + dx), 40, 100000);
      next.height = clamp(snap(next.height + dy), 40, 100000);
      if (event.shiftKey) next.width = next.height = Math.max(next.width, next.height);
    } else {
      next.x = clamp(snap(next.x + dx), -1000000, 1000000);
      next.y = clamp(snap(next.y + dy), -1000000, 1000000);
    }
    drag.preview = next;
    layer.querySelector(`[data-area-id="${CSS.escape(drag.id)}"]`)?.replaceWith(draw(next));
  }, true);

  svg.addEventListener("pointerup", event => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    event.stopImmediatePropagation();
    const action = drag; cancelDrag();
    if (action.moved) {
      const { x, y, width, height } = action.preview;
      persist(action.id, { x, y, width, height }, action.revision);
    } else if (!action.resize) openDialog(action.id);
  }, true);

  for (const name of ["pointercancel", "lostpointercapture"]) svg.addEventListener(name, event => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    event.stopImmediatePropagation(); cancelDrag(); renderMap();
  }, true);

  svg.addEventListener("keydown", event => {
    const target = event.target.closest?.(".map-area");
    if (!target || !isEditing()) return;
    if (["Enter", " "].includes(event.key)) {
      event.preventDefault(); event.stopImmediatePropagation(); openDialog(target.dataset.areaId);
    }
  }, true);

  $("#areaForm").addEventListener("submit", submit);
  $("#areaDelete").addEventListener("click", () => remove($("#areaId").value));
  document.querySelectorAll('[data-action="add-area"]').forEach(button => button.addEventListener("click", () => openDialog()));
  $("#areaTableBody").addEventListener("click", event => {
    const button = event.target.closest("[data-area-action]");
    const id = button?.closest("[data-area-id]")?.dataset.areaId;
    if (!id) return;
    if (button.dataset.areaAction === "locate") focus(id);
    else if (button.dataset.areaAction === "edit") openDialog(id);
    else if (button.dataset.areaAction === "delete") remove(id);
  });
  for (const id of ["areaWidth", "areaHeight", "areaEqualSides"]) $(`#${id}`).addEventListener("input", () => {
    if (!$("#areaEqualSides").checked) return;
    const source = id === "areaHeight" ? "areaHeight" : "areaWidth";
    $(source === "areaWidth" ? "#areaHeight" : "#areaWidth").value = $(`#${source}`).value;
  });
  return { renderMap, renderTable };
};
