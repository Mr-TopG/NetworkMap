(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const svgNS = "http://www.w3.org/2000/svg";
  const emptyState = { revision: 0, updated_at: null, nodes: [], links: [], settings: {} };
  const app = {
    state: { ...emptyState },
    loaded: false,
    connected: false,
    selectedNodeId: null,
    selectedLinkId: null,
    activeView: "overview",
    activeConfigTab: "inventory",
    topologyEditing: false,
    token: "",
    eventSource: null,
    eventRefreshTimer: null,
    refreshTimer: null,
    visualPositions: new Map(),
    transform: { x: 0, y: 0, scale: 1 },
    interaction: null,
    rawDirty: false,
    rawBaseRevision: 0,
    discoveryResults: [],
    confirmAction: null
  };

  const deviceIcons = {
    router: '<circle cx="0" cy="0" r="9"/><path d="M-5 0h10M0-5v10M-5 0l2-2M-5 0l2 2M5 0 3-2M5 0l3 2M0-5l-2 2M0-5l2 2"/>',
    switch: '<rect x="-10" y="-7" width="20" height="14" rx="2"/><path d="M-6-2h2M0-2h2M6-2h1M-6 3h2M0 3h2M6 3h1"/>',
    server: '<rect x="-9" y="-10" width="18" height="20" rx="2"/><path d="M-5-5h6M-5 0h6M-5 5h6M5-5h.1M5 0h.1M5 5h.1"/>',
    workstation: '<rect x="-10" y="-9" width="20" height="14" rx="2"/><path d="M-4 10h8M0 5v5"/>',
    laptop: '<path d="M-8-9H8v13H-8Z M-11 8h22l-2 3H-9Z"/>',
    mobile: '<rect x="-6" y="-11" width="12" height="22" rx="3"/><path d="M-2-8h4M0 8h.1"/>',
    "access-point": '<circle cx="0" cy="6" r="2"/><path d="M-5 2a7 7 0 0 1 10 0M-9-2a13 13 0 0 1 18 0M0 8v3"/>',
    firewall: '<path d="M0-11 9-7v6c0 6-4 9-9 12-5-3-9-6-9-12v-6Z"/><path d="M-7-4h14M-7 1h14M-3-4v5M4-4v5"/>',
    printer: '<path d="M-7-5v-5H7v5M-7 6v5H7V6"/><rect x="-10" y="-5" width="20" height="11" rx="2"/><path d="M6-1h.1"/>',
    camera: '<rect x="-10" y="-7" width="20" height="14" rx="3"/><circle cx="0" cy="0" r="4"/><path d="m-6-7 2-4h8l2 4"/>',
    storage: '<ellipse cx="0" cy="-7" rx="9" ry="4"/><path d="M-9-7V7c0 2 4 4 9 4s9-2 9-4V-7M-9 0c0 2 4 4 9 4s9-2 9-4"/>',
    nas: '<rect x="-9" y="-10" width="18" height="20" rx="2"/><path d="M-5-6h10M-5-1h10M-5 4h10M5 7h.1"/>',
    iot: '<rect x="-8" y="-8" width="16" height="16" rx="3"/><path d="M-11-4h3M-11 2h3M8-4h3M8 2h3M-4-11v3M2-11v3M-4 8v3M2 8v3"/><circle cx="0" cy="0" r="3"/>',
    cloud: '<path d="M-7 7h14a6 6 0 0 0 1-12 9 9 0 0 0-17 3A4.5 4.5 0 0 0-7 7Z"/>',
    other: '<circle cx="0" cy="0" r="9"/><path d="M0-4v.1M0 0v5"/>'
  };

  function readInitialToken() {
    const url = new URL(window.location.href);
    const rawHash = url.hash.startsWith("#") ? url.hash.slice(1) : url.hash;
    const hashHasToken = /(^|&)token=/.test(rawHash);
    const hashParams = hashHasToken ? new URLSearchParams(rawHash) : null;
    const fragmentToken = hashParams?.get("token") || "";
    const incoming = url.searchParams.get("token") || fragmentToken;
    try {
      if (incoming) sessionStorage.setItem("networkmap_token", incoming);
      app.token = incoming || sessionStorage.getItem("networkmap_token") || "";
    } catch (_) {
      app.token = incoming || "";
    }
    if (incoming) {
      url.searchParams.delete("token");
      if (hashParams) hashParams.delete("token");
      const cleanHash = hashParams ? hashParams.toString() : rawHash;
      history.replaceState(null, "", url.pathname + (url.search ? url.search : "") + (cleanHash ? `#${cleanHash}` : ""));
    }
  }

  function iconMarkup(kind) {
    return `<svg viewBox="-12 -12 24 24" aria-hidden="true">${deviceIcons[kind] || deviceIcons.other}</svg>`;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
  }

  function titleCase(value) {
    return String(value || "unknown").replace(/[-_]/g, " ").replace(/\b\w/g, c => c.toUpperCase());
  }

  function nodeKind(node) { return node.kind || node.type || "other"; }
  function linkKind(link) { return link.kind || link.type || "other"; }
  function linkName(link) { return link.name || link.label || ""; }
  function nodeById(id) { return app.state.nodes.find(node => String(node.id) === String(id)); }
  function linkById(id) { return app.state.links.find(link => String(link.id) === String(id)); }
  function endpointId(link, end) { return link[end] ?? link[`${end}_id`] ?? ""; }
  function linkUiStatus(status) { return status === "active" || status === "online" ? "online" : status === "inactive" || status === "offline" ? "offline" : status === "degraded" ? "degraded" : "unknown"; }

  function managementUrl(node) {
    const configured = String(node?.management_url || "").trim();
    if (configured) {
      try {
        const parsed = new URL(configured);
        if (["http:", "https:"].includes(parsed.protocol) && !parsed.username && !parsed.password) return parsed.href;
      } catch (_) { /* invalid legacy value */ }
    }
    const address = String(node?.hostname || node?.ip || "").trim();
    if (!address) return "";
    const host = address.includes(":") && !address.startsWith("[") ? `[${address}]` : address;
    return `http://${host}/`;
  }

  function isMikrotik(node) {
    const identity = [node?.vendor, node?.name, node?.hostname, ...(node?.tags || [])].join(" ");
    return Boolean(node?.winbox_enabled || /mikrotik|routeros/i.test(identity));
  }

  function winboxTarget(node) {
    return String(node?.ip || node?.mac || node?.hostname || "").trim();
  }

  function openSelectedManagement() {
    const node = nodeById(app.selectedNodeId);
    const url = managementUrl(node);
    if (!url) {
      toast("No management address", "Add an IP, hostname, or HTTP(S) management URL in Edit topology.", "warning");
      return;
    }
    window.open(url, "_blank", "noopener,noreferrer");
  }

  function openSelectedWinbox() {
    const node = nodeById(app.selectedNodeId);
    const target = winboxTarget(node);
    if (!node || !target) {
      toast("No WinBox address", "Add an IP address, MAC address, or hostname first.", "warning");
      return;
    }
    navigator.clipboard?.writeText(target).catch(() => {});
    const launcher = document.createElement("a");
    launcher.href = `winbox://connect/${encodeURIComponent(target)}`;
    launcher.setAttribute("aria-hidden", "true");
    document.body.append(launcher);
    launcher.click();
    launcher.remove();
    toast("Opening WinBox", `${target} was also copied to your clipboard.`);
  }

  function setTopologyEditing(editing) {
    app.topologyEditing = Boolean(editing);
    $("#appShell").classList.toggle("topology-editing", app.topologyEditing);
    $("#topologyEditButton").setAttribute("aria-pressed", String(app.topologyEditing));
    $("span", $("#topologyEditButton")).textContent = app.topologyEditing ? "Done editing" : "Edit topology";
    $$(".topology-edit-only").forEach(element => { element.hidden = !app.topologyEditing; });
    $("#mapTip").textContent = app.topologyEditing ? "Edit mode · Drag nodes or select links" : "View mode · Click a device to inspect";
    if (app.loaded) { renderMap(); renderInspector(); }
  }

  async function api(path, options = {}) {
    const { expectedRevision, ...requestOptions } = options;
    const headers = new Headers(requestOptions.headers || {});
    const method = String(requestOptions.method || "GET").toUpperCase();
    headers.set("Accept", "application/json");
    if (app.token) headers.set("Authorization", `Bearer ${app.token}`);
    if (requestOptions.body && !(requestOptions.body instanceof FormData)) headers.set("Content-Type", "application/json");
    const stateMutation = ["POST", "PUT", "PATCH", "DELETE"].includes(method) && path !== "/api/discovery" && path !== "/api/session";
    const revision = expectedRevision === undefined ? app.state.revision : expectedRevision;
    if (stateMutation && app.loaded && Number.isInteger(Number(revision))) headers.set("If-Match", `"${revision}"`);
    let response;
    try {
      response = await fetch(path, { ...requestOptions, headers });
    } catch (error) {
      setConnection(false);
      throw new Error("The NetworkMap server is unavailable. Check that it is running and try again.");
    }
    if (response.status === 401) {
      showAuthDialog();
      const error = new Error("An access token is required.");
      error.status = 401;
      throw error;
    }
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("application/json") ? await response.json().catch(() => ({})) : await response.text();
    if (!response.ok) {
      const detail = payload?.error?.message || payload?.message || (typeof payload === "string" && payload) || `Request failed (${response.status})`;
      const error = new Error(detail);
      error.status = response.status;
      error.details = payload?.error?.details;
      if (response.status === 409) {
        try {
          const latest = await api("/api/state");
          applyState(latest);
          $$('form[data-base-revision]').forEach(form => { form.dataset.baseRevision = String(latest.revision); });
          if (app.rawDirty) app.rawBaseRevision = latest.revision;
        } catch (_) { /* keep the current view if reload fails */ }
        toast("Workspace changed elsewhere", "The latest version has been loaded. Review it and try your change again.", "warning");
        error.handled = true;
      }
      throw error;
    }
    setConnection(true);
    return payload;
  }

  function applyState(next, { fit = false } = {}) {
    if (!next || !Array.isArray(next.nodes) || !Array.isArray(next.links)) return;
    const incomingRevision = Number(next.revision);
    const currentRevision = Number(app.state.revision);
    if (app.loaded && Number.isFinite(incomingRevision) && Number.isFinite(currentRevision) && incomingRevision < currentRevision) return;
    app.visualPositions.clear();
    app.state = {
      revision: next.revision ?? app.state.revision ?? 0,
      updated_at: next.updated_at ?? new Date().toISOString(),
      nodes: next.nodes,
      links: next.links,
      settings: next.settings && typeof next.settings === "object" ? next.settings : {}
    };
    app.loaded = true;
    const ids = new Set(app.state.nodes.map(node => String(node.id)));
    if (app.selectedNodeId && !ids.has(String(app.selectedNodeId))) app.selectedNodeId = null;
    renderAll();
    scheduleRefreshInterval();
    if (fit) requestAnimationFrame(() => fitMap());
  }

  async function fetchState({ fit = false, quiet = false } = {}) {
    try {
      const state = await api("/api/state");
      applyState(state, { fit: fit || !app.loaded });
      connectEvents();
      return state;
    } catch (error) {
      $("#mapLoading").classList.add("hidden");
      if (!quiet && error.status !== 401) toast("Could not load network", error.message, "error");
      throw error;
    }
  }

  function setConnection(connected) {
    app.connected = connected;
    const pill = $("#connectionPill");
    pill.classList.toggle("connected", connected);
    pill.classList.toggle("disconnected", !connected);
    $("span:last-child", pill).textContent = connected ? "Live" : "Offline";
    $("#syncLabel").textContent = connected ? "Changes synced" : "Connection lost";
    $("#syncDetail").textContent = connected ? "Web & Linux clients" : "Trying to reconnect";
    const pulse = $(".pulse-dot");
    pulse.classList.toggle("disconnected", !connected);
  }

  function connectEvents() {
    if (app.eventSource || typeof EventSource === "undefined") return;
    const source = new EventSource("/api/events");
    app.eventSource = source;
    source.onopen = () => setConnection(true);
    source.onmessage = event => handleServerEvent(event);
    ["ready", "state", "change", "updated"].forEach(name => source.addEventListener(name, event => handleServerEvent(event)));
    source.onerror = () => {
      setConnection(false);
      if (source.readyState === EventSource.CLOSED) app.eventSource = null;
    };
  }

  function handleServerEvent(event) {
    let data = null;
    try { data = JSON.parse(event.data); } catch (_) { /* change ping */ }
    if (data?.nodes && data?.links) {
      applyState(data);
      return;
    }
    const announcedRevision = Number(data?.revision);
    if (app.loaded && Number.isFinite(announcedRevision) && announcedRevision <= Number(app.state.revision)) return;
    clearTimeout(app.eventRefreshTimer);
    app.eventRefreshTimer = setTimeout(() => fetchState({ quiet: true }).catch(() => {}), 150);
  }

  function closeEvents() {
    if (app.eventSource) app.eventSource.close();
    app.eventSource = null;
  }

  function forgetBootstrapToken() {
    app.token = "";
    try { sessionStorage.removeItem("networkmap_token"); } catch (_) { /* unavailable */ }
  }

  function showAuthDialog() {
    closeEvents();
    const dialog = $("#authDialog");
    if (!dialog.open) dialog.showModal();
    setTimeout(() => $("#authToken").focus(), 30);
  }

  function scheduleRefreshInterval() {
    clearInterval(app.refreshTimer);
    const seconds = Number(app.state.settings.refresh_interval || 0);
    if (seconds >= 15) app.refreshTimer = setInterval(() => fetchState({ quiet: true }).catch(() => {}), seconds * 1000);
  }

  function renderAll() {
    renderSummary();
    renderMap();
    renderInspector();
    renderDeviceTable();
    renderLinkTable();
    renderSettings();
    renderRawJson(false);
  }

  function renderSummary() {
    const nodes = app.state.nodes;
    const online = nodes.filter(node => node.status === "online").length;
    const issues = nodes.filter(node => node.status === "offline" || node.status === "degraded").length;
    $("#statDevices").textContent = nodes.length;
    $("#statOnline").textContent = online;
    $("#statLinks").textContent = app.state.links.length;
    $("#statIssues").textContent = issues;
    $("#statDevicesHint").textContent = nodes.length === 1 ? "1 mapped device" : `${nodes.length} mapped devices`;
    $("#statOnlineHint").textContent = nodes.length ? `${Math.round((online / nodes.length) * 100)}% available` : "No devices yet";
    $("#statIssuesHint").textContent = issues === 1 ? "1 device flagged" : `${issues} devices flagged`;
    $("#deviceTabCount").textContent = nodes.length;
    $("#linkTabCount").textContent = app.state.links.length;
    $("#mapSubtitle").textContent = `${nodes.length} ${nodes.length === 1 ? "device" : "devices"} · ${app.state.links.length} ${app.state.links.length === 1 ? "connection" : "connections"}`;
    $("#revisionLabel").textContent = `#${app.state.revision ?? 0}`;
    const name = app.state.settings.name || app.state.settings.network_name || "Main network";
    $("#workspaceNameSide").textContent = name;
    document.title = `${name} · NetworkMap`;
    if (app.state.updated_at) {
      const date = new Date(app.state.updated_at);
      $("#lastUpdated").textContent = Number.isNaN(date.getTime()) ? "Synced just now" : `Updated ${relativeTime(date)}`;
    }
  }

  function relativeTime(date) {
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    if (seconds < 10) return "just now";
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function ensurePositions() {
    const placed = app.state.nodes.filter(node => Number.isFinite(Number(node.x)) && Number.isFinite(Number(node.y)));
    app.state.nodes.forEach((node, index) => {
      const id = String(node.id);
      if (app.visualPositions.has(id)) return;
      if (Number.isFinite(Number(node.x)) && Number.isFinite(Number(node.y))) {
        app.visualPositions.set(id, { x: Number(node.x), y: Number(node.y) });
      } else {
        const count = Math.max(app.state.nodes.length - placed.length, 1);
        const angle = (index / count) * Math.PI * 2 - Math.PI / 2;
        const radius = count <= 1 ? 0 : 170 + Math.floor(index / 10) * 100;
        app.visualPositions.set(id, { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius });
      }
    });
  }

  function renderMap() {
    $("#mapLoading").classList.toggle("hidden", app.loaded);
    $("#mapEmpty").classList.toggle("hidden", !app.loaded || app.state.nodes.length > 0);
    const linkLayer = $("#linkLayer");
    const nodeLayer = $("#nodeLayer");
    linkLayer.replaceChildren();
    nodeLayer.replaceChildren();
    if (!app.state.nodes.length) return;
    ensurePositions();
    app.state.links.forEach(link => renderLinkSvg(link, linkLayer));
    app.state.nodes.forEach(node => renderNodeSvg(node, nodeLayer));
    applyTransform();
    applyMapSearch();
  }

  function svgEl(name, attributes = {}) {
    const element = document.createElementNS(svgNS, name);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value));
    return element;
  }

  function renderLinkSvg(link, layer) {
    const source = app.visualPositions.get(String(endpointId(link, "source")));
    const target = app.visualPositions.get(String(endpointId(link, "target")));
    if (!source || !target) return;
    const group = svgEl("g", { class: "link-group", "data-link-id": link.id, role: "button", tabindex: "0", "aria-label": `${app.topologyEditing ? "Edit" : "View"} connection ${linkName(link) || "between devices"}` });
    const deltaX = target.x - source.x;
    const deltaY = target.y - source.y;
    const length = Math.max(Math.hypot(deltaX, deltaY), 1);
    const trim = 57;
    const x1 = source.x + deltaX / length * trim;
    const y1 = source.y + deltaY / length * trim;
    const x2 = target.x - deltaX / length * trim;
    const y2 = target.y - deltaY / length * trim;
    const pathValue = `M ${x1} ${y1} L ${x2} ${y2}`;
    const statusClass = linkUiStatus(link.status);
    const path = svgEl("path", { d: pathValue, class: `topology-link ${statusClass}${link.directed ? " directed" : ""}` });
    const hit = svgEl("path", { d: pathValue, class: "topology-link-hit" });
    group.append(path, hit);
    const label = linkName(link);
    if (label && app.state.settings.show_link_labels !== false && app.state.settings.show_labels !== false) {
      const midX = (x1 + x2) / 2;
      const midY = (y1 + y2) / 2;
      const width = Math.min(Math.max(label.length * 5.4 + 12, 35), 120);
      group.append(svgEl("rect", { x: midX - width / 2, y: midY - 8, width, height: 16, rx: 5, class: "link-label-bg" }));
      const text = svgEl("text", { x: midX, y: midY + .5, class: "link-label" });
      text.textContent = label.length > 20 ? `${label.slice(0, 19)}…` : label;
      group.append(text);
    }
    group.addEventListener("click", event => { event.stopPropagation(); if (app.topologyEditing) openLinkDialog(link.id); });
    group.addEventListener("keydown", event => { if (app.topologyEditing && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); openLinkDialog(link.id); } });
    layer.append(group);
  }

  function renderNodeSvg(node, layer) {
    const id = String(node.id);
    const position = app.visualPositions.get(id);
    const group = svgEl("g", {
      class: `node${String(app.selectedNodeId) === id ? " selected" : ""}`,
      transform: `translate(${position.x} ${position.y})`,
      "data-node-id": id,
      role: "button",
      tabindex: "0",
      "aria-label": `${node.name || "Unnamed device"}, ${node.status || "unknown"}. Press Enter to inspect${app.topologyEditing ? "; arrow keys to move." : "."}`
    });
    group.append(svgEl("rect", { x: -63, y: -48, width: 126, height: 96, rx: 17, class: "node-halo" }));
    group.append(svgEl("rect", { x: -55, y: -40, width: 110, height: 80, rx: 13, class: "node-body" }));
    group.append(svgEl("circle", { cx: 0, cy: -12, r: 17, class: "node-icon-disc" }));
    const icon = svgEl("g", { class: "node-icon", transform: "translate(0 -12)" });
    const template = document.createElementNS(svgNS, "svg");
    template.innerHTML = deviceIcons[nodeKind(node)] || deviceIcons.other;
    [...template.children].forEach(child => icon.append(child));
    group.append(icon);
    const statusRing = svgEl("circle", { cx: 13, cy: -24, r: 5, class: "node-status-ring" });
    const status = svgEl("circle", { cx: 13, cy: -24, r: 3.5, class: `node-status ${node.status || "unknown"}` });
    group.append(statusRing, status);
    const title = svgEl("text", { x: 0, y: 16, class: "node-title" });
    title.textContent = truncate(node.name || "Unnamed device", 17);
    group.append(title);
    if (!app.state.settings.compact_labels) {
      const subtitle = svgEl("text", { x: 0, y: 29, class: "node-subtitle" });
      subtitle.textContent = truncate(node.ip || node.hostname || titleCase(nodeKind(node)), 21);
      group.append(subtitle);
    }
    const degree = app.state.links.filter(link => String(endpointId(link, "source")) === id || String(endpointId(link, "target")) === id).length;
    if (degree) {
      group.append(svgEl("rect", { x: 37, y: 28, width: 23, height: 14, rx: 7, class: "node-badge" }));
      const badge = svgEl("text", { x: 48.5, y: 35.5, class: "node-badge-text" });
      badge.textContent = degree;
      group.append(badge);
    }
    group.addEventListener("pointerdown", event => startNodeDrag(event, id));
    group.addEventListener("click", event => { if (!app.topologyEditing) { event.stopPropagation(); selectNode(id); } });
    group.addEventListener("keydown", event => handleNodeKeydown(event, id));
    layer.append(group);
  }

  function truncate(text, length) {
    const value = String(text || "");
    return value.length > length ? `${value.slice(0, length - 1)}…` : value;
  }

  function applyTransform() {
    $("#viewport").setAttribute("transform", `translate(${app.transform.x} ${app.transform.y}) scale(${app.transform.scale})`);
  }

  function worldPoint(clientX, clientY) {
    const rect = $("#topology").getBoundingClientRect();
    return { x: (clientX - rect.left - app.transform.x) / app.transform.scale, y: (clientY - rect.top - app.transform.y) / app.transform.scale };
  }

  function startNodeDrag(event, id) {
    if (!app.topologyEditing) return;
    if (event.button !== 0 && event.pointerType !== "touch") return;
    event.stopPropagation();
    const svg = $("#topology");
    const startWorld = worldPoint(event.clientX, event.clientY);
    const original = { ...app.visualPositions.get(id) };
    app.interaction = { type: "node", id, pointerId: event.pointerId, startWorld, original, moved: false };
    svg.classList.add("dragging");
    svg.setPointerCapture?.(event.pointerId);
  }

  function startPan(event) {
    if (event.target.closest?.(".node, .link-group") || (event.button !== 0 && event.button !== 1)) return;
    app.interaction = { type: "pan", pointerId: event.pointerId, clientX: event.clientX, clientY: event.clientY, origin: { x: app.transform.x, y: app.transform.y }, moved: false };
    $("#topology").classList.add("panning");
    $("#topology").setPointerCapture?.(event.pointerId);
  }

  function movePointer(event) {
    const action = app.interaction;
    if (!action || action.pointerId !== event.pointerId) return;
    if (action.type === "pan") {
      const dx = event.clientX - action.clientX;
      const dy = event.clientY - action.clientY;
      action.moved ||= Math.hypot(dx, dy) > 3;
      app.transform.x = action.origin.x + dx;
      app.transform.y = action.origin.y + dy;
      applyTransform();
    } else if (action.type === "node") {
      const point = worldPoint(event.clientX, event.clientY);
      let x = action.original.x + point.x - action.startWorld.x;
      let y = action.original.y + point.y - action.startWorld.y;
      if (app.state.settings.snap_to_grid) {
        const grid = Number(app.state.settings.grid_size) || 20;
        x = Math.round(x / grid) * grid;
        y = Math.round(y / grid) * grid;
      }
      action.moved ||= Math.hypot(x - action.original.x, y - action.original.y) > 2;
      app.visualPositions.set(action.id, { x, y });
      updateNodeAndLinksPosition(action.id);
    }
  }

  function endPointer(event) {
    const action = app.interaction;
    if (!action || action.pointerId !== event.pointerId) return;
    app.interaction = null;
    $("#topology").classList.remove("panning", "dragging");
    if (action.type === "node") {
      if (!action.moved) {
        selectNode(action.id);
      } else {
        const position = app.visualPositions.get(action.id);
        persistNodePosition(action.id, position);
      }
    } else if (!action.moved) {
      clearSelection();
    }
  }

  function updateNodeAndLinksPosition(id) {
    const group = $(`.node[data-node-id="${CSS.escape(String(id))}"]`);
    const position = app.visualPositions.get(String(id));
    if (group && position) group.setAttribute("transform", `translate(${position.x} ${position.y})`);
    app.state.links.forEach(link => {
      if (String(endpointId(link, "source")) === String(id) || String(endpointId(link, "target")) === String(id)) {
        const old = $(`.link-group[data-link-id="${CSS.escape(String(link.id))}"]`);
        if (!old) return;
        const holder = document.createDocumentFragment();
        renderLinkSvg(link, holder);
        old.replaceWith(holder);
      }
    });
  }

  async function persistNodePosition(id, position) {
    try {
      const next = await api(`/api/nodes/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ x: Math.round(position.x * 10) / 10, y: Math.round(position.y * 10) / 10 }) });
      applyState(next);
    } catch (error) {
      app.visualPositions.delete(String(id)); renderMap();
      reportError("Position not saved", error);
    }
  }

  function handleNodeKeydown(event, id) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault(); selectNode(id); return;
    }
    const vectors = { ArrowLeft: [-10, 0], ArrowRight: [10, 0], ArrowUp: [0, -10], ArrowDown: [0, 10] };
    if (!vectors[event.key] || !app.topologyEditing) return;
    event.preventDefault();
    const amount = event.shiftKey ? 5 : 1;
    const position = app.visualPositions.get(String(id));
    position.x += vectors[event.key][0] * amount;
    position.y += vectors[event.key][1] * amount;
    updateNodeAndLinksPosition(id);
    clearTimeout(handleNodeKeydown.timer);
    handleNodeKeydown.timer = setTimeout(() => persistNodePosition(id, position), 350);
  }

  function zoomAt(factor, clientX, clientY) {
    const svg = $("#topology");
    const rect = svg.getBoundingClientRect();
    const sx = clientX ?? rect.left + rect.width / 2;
    const sy = clientY ?? rect.top + rect.height / 2;
    const worldX = (sx - rect.left - app.transform.x) / app.transform.scale;
    const worldY = (sy - rect.top - app.transform.y) / app.transform.scale;
    const scale = Math.min(2.5, Math.max(.25, app.transform.scale * factor));
    app.transform.x = sx - rect.left - worldX * scale;
    app.transform.y = sy - rect.top - worldY * scale;
    app.transform.scale = scale;
    applyTransform();
  }

  function fitMap(padding = 80) {
    if (!app.state.nodes.length) return;
    ensurePositions();
    const rect = $("#topology").getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const positions = [...app.visualPositions.values()];
    const minX = Math.min(...positions.map(p => p.x)) - 65;
    const maxX = Math.max(...positions.map(p => p.x)) + 65;
    const minY = Math.min(...positions.map(p => p.y)) - 55;
    const maxY = Math.max(...positions.map(p => p.y)) + 55;
    const width = Math.max(maxX - minX, 130);
    const height = Math.max(maxY - minY, 110);
    const scale = Math.min(1.3, Math.max(.25, Math.min((rect.width - padding) / width, (rect.height - padding) / height)));
    app.transform.scale = scale;
    app.transform.x = rect.width / 2 - ((minX + maxX) / 2) * scale;
    app.transform.y = rect.height / 2 - ((minY + maxY) / 2) * scale;
    applyTransform();
  }

  function focusNode(id) {
    const position = app.visualPositions.get(String(id));
    const rect = $("#topology").getBoundingClientRect();
    if (!position || !rect.width) return;
    app.transform.scale = Math.max(app.transform.scale, .9);
    app.transform.x = rect.width / 2 - position.x * app.transform.scale;
    app.transform.y = rect.height / 2 - position.y * app.transform.scale;
    applyTransform();
  }

  async function autoLayout() {
    if (!app.topologyEditing || !app.state.nodes.length) return;
    const nodes = app.state.nodes;
    const adjacency = new Map(nodes.map(node => [String(node.id), []]));
    app.state.links.forEach(link => {
      const source = String(endpointId(link, "source"));
      const target = String(endpointId(link, "target"));
      adjacency.get(source)?.push(target); adjacency.get(target)?.push(source);
    });
    const preferredKinds = { router: 0, firewall: 1, switch: 2 };
    const root = [...nodes].sort((a, b) => (adjacency.get(String(b.id))?.length || 0) - (adjacency.get(String(a.id))?.length || 0) || ((preferredKinds[nodeKind(a)] ?? 9) - (preferredKinds[nodeKind(b)] ?? 9)))[0];
    const levels = new Map([[String(root.id), 0]]);
    const queue = [String(root.id)];
    while (queue.length) {
      const id = queue.shift();
      for (const next of adjacency.get(id) || []) if (!levels.has(next)) { levels.set(next, levels.get(id) + 1); queue.push(next); }
    }
    let disconnectedLevel = Math.max(0, ...levels.values()) + 1;
    nodes.forEach(node => { if (!levels.has(String(node.id))) levels.set(String(node.id), disconnectedLevel); });
    const grouped = new Map();
    nodes.forEach(node => { const level = levels.get(String(node.id)); if (!grouped.has(level)) grouped.set(level, []); grouped.get(level).push(node); });
    grouped.forEach((items, level) => {
      if (level === 0 && items.length === 1) { app.visualPositions.set(String(items[0].id), { x: 0, y: 0 }); return; }
      const radius = 175 * Math.max(1, level);
      items.sort((a, b) => String(a.name).localeCompare(String(b.name))).forEach((node, index) => {
        const angle = (index / items.length) * Math.PI * 2 - Math.PI / 2 + (level % 2 ? .18 : 0);
        app.visualPositions.set(String(node.id), { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius });
      });
    });
    renderMap(); fitMap();
    const plannedPositions = new Map(app.visualPositions);
    const expectedRevision = app.state.revision;
    const button = $("#mapMenuButton");
    button.disabled = true;
    try {
      const arrangedNodes = nodes.map(node => {
        const position = plannedPositions.get(String(node.id));
        return position ? { ...node, x: Math.round(position.x), y: Math.round(position.y) } : node;
      });
      const latest = await api("/api/state", {
        method: "PUT",
        expectedRevision,
        body: JSON.stringify({ nodes: arrangedNodes, links: app.state.links, settings: app.state.settings })
      });
      applyState(latest);
      toast("Map arranged", `${nodes.length} device positions were saved.`);
    } catch (error) {
      reportError("Layout not saved", error, "warning");
    } finally { button.disabled = false; }
  }

  function applyMapSearch() {
    const query = $("#mapSearch").value.trim().toLowerCase();
    $$(".node", $("#nodeLayer")).forEach(element => {
      const node = nodeById(element.dataset.nodeId);
      const haystack = [node?.name, node?.ip, node?.mac, node?.hostname, nodeKind(node || {})].join(" ").toLowerCase();
      const match = !query || haystack.includes(query);
      element.classList.toggle("dimmed", Boolean(query) && !match);
      element.classList.toggle("search-match", Boolean(query) && match);
    });
  }

  function selectNode(id) {
    app.selectedNodeId = String(id);
    renderMap();
    renderInspector();
    $("#inspector").classList.add("has-selection");
  }

  function clearSelection() {
    app.selectedNodeId = null;
    app.selectedLinkId = null;
    renderMap(); renderInspector();
    $("#inspector").classList.remove("has-selection");
  }

  function renderInspector() {
    const node = nodeById(app.selectedNodeId);
    $("#inspectorPlaceholder").classList.toggle("hidden", Boolean(node));
    $("#inspectorContent").classList.toggle("hidden", !node);
    if (!node) return;
    $("#inspectorAvatar").innerHTML = iconMarkup(nodeKind(node));
    $("#inspectorName").textContent = node.name || "Unnamed device";
    $("#inspectorKind").textContent = titleCase(nodeKind(node));
    const state = $("#inspectorState"); state.textContent = node.status || "unknown"; state.className = `device-state ${node.status || "unknown"}`;
    const webUrl = managementUrl(node);
    const webButton = $("#openSelected");
    webButton.disabled = !webUrl;
    $("span", webButton).textContent = webUrl ? `Open ${webUrl.toLowerCase().startsWith("https://") ? "HTTPS" : "HTTP"}` : "No web address";
    webButton.title = webUrl || "Edit this device to add a management address";
    const winboxButton = $("#winboxSelected");
    winboxButton.hidden = !isMikrotik(node) || !winboxTarget(node);
    winboxButton.title = winboxButton.hidden ? "" : `Open ${winboxTarget(node)} in MikroTik WinBox`;
    const detailValues = [
      ["IP address", node.ip ? `<code>${escapeHtml(node.ip)}</code>` : "—"],
      ["MAC address", node.mac ? `<code>${escapeHtml(node.mac)}</code>` : "—"],
      ["Hostname", escapeHtml(node.hostname || "—")],
      ["Vendor", escapeHtml(node.vendor || node.config?.vendor_model || "—")],
      ["Management", webUrl ? escapeHtml(webUrl.replace(/\/$/, "")) : "—"],
      ["Tags", escapeHtml(Array.isArray(node.tags) && node.tags.length ? node.tags.join(", ") : "—")]
    ];
    $("#inspectorDetails").innerHTML = detailValues.map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`).join("");
    $("#inspectorNotes").textContent = node.notes || "No notes for this device.";
    const connections = app.state.links.filter(link => String(endpointId(link, "source")) === String(node.id) || String(endpointId(link, "target")) === String(node.id));
    const list = $("#inspectorConnections");
    if (!connections.length) list.innerHTML = '<span class="empty-connections">No mapped connections.</span>';
    else list.innerHTML = connections.map(link => {
      const otherId = String(endpointId(link, "source")) === String(node.id) ? endpointId(link, "target") : endpointId(link, "source");
      const other = nodeById(otherId);
      const status = linkUiStatus(link.status);
      return `<button class="connection-item" data-link-id="${escapeHtml(link.id)}"><span class="connection-line-icon ${status}"><svg viewBox="0 0 24 24"><path d="M5 12h14M16 9l3 3-3 3"/></svg></span><div><strong>${escapeHtml(other?.name || "Unknown device")}</strong><span>${escapeHtml(linkName(link) || titleCase(linkKind(link)))}</span></div></button>`;
    }).join("");
    $$(".connection-item", list).forEach(button => {
      button.disabled = !app.topologyEditing;
      button.title = app.topologyEditing ? "Edit connection" : "Enable Edit topology to change this connection";
      button.addEventListener("click", () => { if (app.topologyEditing) openLinkDialog(button.dataset.linkId); });
    });
  }

  function renderDeviceTable() {
    const query = $("#inventorySearch").value.trim().toLowerCase();
    const filter = $("#statusFilter").value;
    const nodes = app.state.nodes.filter(node => {
      const matches = [node.name, node.ip, node.mac, node.hostname, nodeKind(node), ...(node.tags || [])].join(" ").toLowerCase().includes(query);
      return matches && (filter === "all" || node.status === filter);
    });
    const body = $("#deviceTableBody");
    body.innerHTML = nodes.map(node => `<tr data-node-id="${escapeHtml(node.id)}"><td><div class="device-cell"><span class="device-avatar">${iconMarkup(nodeKind(node))}</span><span><strong>${escapeHtml(node.name || "Unnamed device")}</strong><small>${escapeHtml(node.hostname || node.vendor || node.config?.vendor_model || "No hostname")}</small></span></div></td><td><span class="status-badge ${escapeHtml(node.status || "unknown")}">${escapeHtml(node.status || "unknown")}</span></td><td>${escapeHtml(node.ip || "—")}</td><td><code>${escapeHtml(node.mac || "—")}</code></td><td><span class="type-label">${escapeHtml(titleCase(nodeKind(node)))}</span></td><td><div class="row-actions"><button data-action="locate" title="Show on map" aria-label="Show ${escapeHtml(node.name)} on map"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg></button><button data-action="edit" title="Edit" aria-label="Edit ${escapeHtml(node.name)}"><svg viewBox="0 0 24 24"><path d="m4 20 4.2-1 10.6-10.6a2 2 0 0 0-2.8-2.8L5.4 16.2Z"/></svg></button><button class="danger-action" data-action="delete" title="Remove" aria-label="Remove ${escapeHtml(node.name)}"><svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M9 11v6M15 11v6M6 7l1 14h10l1-14"/></svg></button></div></td></tr>`).join("");
    $("#deviceTableEmpty").classList.toggle("hidden", nodes.length > 0);
  }

  function handleDeviceTableClick(event) {
    const row = event.target.closest("tr[data-node-id]");
    const action = event.target.closest("button")?.dataset.action;
    if (row && action === "edit") openNodeDialog(row.dataset.nodeId);
    else if (row && action === "delete") confirmDeleteNode(row.dataset.nodeId);
    else if (row && action === "locate") { switchView("overview"); selectNode(row.dataset.nodeId); setTimeout(() => focusNode(row.dataset.nodeId), 50); }
  }

  function renderLinkTable() {
    const body = $("#linkTableBody");
    body.innerHTML = app.state.links.map(link => {
      const source = nodeById(endpointId(link, "source")); const target = nodeById(endpointId(link, "target"));
      const status = linkUiStatus(link.status);
      const speed = link.bandwidth_mbps != null ? `${Number(link.bandwidth_mbps).toLocaleString()} Mbps` : "—";
      return `<tr data-link-id="${escapeHtml(link.id)}"><td>${escapeHtml(source?.name || "Missing device")}</td><td>${escapeHtml(target?.name || "Missing device")}</td><td>${escapeHtml(linkName(link) || titleCase(linkKind(link)))}</td><td>${escapeHtml(speed)}</td><td><span class="status-badge ${status}">${escapeHtml(titleCase(link.status || "unknown"))}</span></td><td><div class="row-actions"><button data-action="edit" title="Edit connection" aria-label="Edit connection"><svg viewBox="0 0 24 24"><path d="m4 20 4.2-1 10.6-10.6a2 2 0 0 0-2.8-2.8L5.4 16.2Z"/></svg></button><button class="danger-action" data-action="delete" title="Remove connection" aria-label="Remove connection"><svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M9 11v6M15 11v6M6 7l1 14h10l1-14"/></svg></button></div></td></tr>`;
    }).join("");
    $("#linkTableEmpty").classList.toggle("hidden", app.state.links.length > 0);
  }

  function handleLinkTableClick(event) {
    const row = event.target.closest("tr[data-link-id]");
    const action = event.target.closest("button")?.dataset.action;
    if (row && action === "edit") openLinkDialog(row.dataset.linkId);
    else if (row && action === "delete") confirmDeleteLink(row.dataset.linkId);
  }

  function renderSettings() {
    const settings = app.state.settings;
    if (document.activeElement?.closest("#settingsForm")) return;
    $("#settingName").value = settings.name || settings.network_name || "Main network";
    $("#settingDescription").value = settings.description || "";
    $("#settingSubnet").value = settings.subnet || settings.discovery_cidr || "";
    $("#settingRefresh").value = String(settings.refresh_interval || 0);
    $("#settingLinkLabels").checked = settings.show_link_labels ?? settings.show_labels ?? true;
    $("#settingCompact").checked = Boolean(settings.compact_labels);
    $("#settingsForm").dataset.baseRevision = String(app.state.revision ?? 0);
  }

  function serializableState() {
    return { revision: app.state.revision, updated_at: app.state.updated_at, nodes: app.state.nodes, links: app.state.links, settings: app.state.settings };
  }

  function renderRawJson(force) {
    if (!force && (app.rawDirty || document.activeElement === $("#rawJson"))) return;
    $("#rawJson").value = JSON.stringify(serializableState(), null, 2);
    app.rawDirty = false;
    app.rawBaseRevision = app.state.revision ?? 0;
    validateRawJson();
  }

  function validateRawJson() {
    const label = $("#jsonValidity");
    try {
      const parsed = JSON.parse($("#rawJson").value);
      if (!Array.isArray(parsed.nodes) || !Array.isArray(parsed.links) || typeof parsed.settings !== "object") throw new Error("Expected nodes, links, and settings");
      label.textContent = "Valid JSON"; label.classList.remove("invalid"); return parsed;
    } catch (error) {
      label.textContent = error.message; label.classList.add("invalid"); return null;
    }
  }

  function switchView(name) {
    if (name !== "overview" && app.topologyEditing) setTopologyEditing(false);
    app.activeView = name;
    $$('[data-view-panel]').forEach(panel => { const active = panel.dataset.viewPanel === name; panel.hidden = !active; panel.classList.toggle("active", active); });
    $$(".nav-item").forEach(button => { const active = button.dataset.view === name; button.classList.toggle("active", active); if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current"); });
    $("#breadcrumbCurrent").textContent = name === "overview" ? "Overview" : "Configuration";
    $("#appShell").classList.remove("nav-open"); $("#menuButton").setAttribute("aria-expanded", "false");
    if (name === "overview") setTimeout(() => { renderMap(); if (app.state.nodes.length) fitMap(); }, 30);
  }

  function switchConfigTab(name) {
    app.activeConfigTab = name;
    $$('[data-config-tab]').forEach(button => button.setAttribute("aria-selected", String(button.dataset.configTab === name)));
    $$(".config-panel").forEach(panel => { const active = panel.id === `${name}Panel`; panel.hidden = !active; panel.classList.toggle("active", active); });
    if (name === "data") renderRawJson(true);
  }

  function openNodeDialog(id = null) {
    const node = id ? nodeById(id) : null;
    const form = $("#nodeForm"); form.reset();
    $("#nodeId").value = node?.id ?? "";
    $("#nodeName").value = node?.name || "";
    $("#nodeType").value = nodeKind(node || { kind: "router" });
    $("#nodeStatus").value = node?.status || "online";
    $("#nodeIp").value = node?.ip || "";
    $("#nodeMac").value = node?.mac || "";
    $("#nodeHostname").value = node?.hostname || "";
    $("#nodeVendor").value = node?.vendor || node?.config?.vendor_model || "";
    $("#nodeManagementUrl").value = node?.management_url || "";
    $("#nodeWinboxEnabled").checked = Boolean(node?.winbox_enabled || (node && isMikrotik(node)));
    $("#nodeTags").value = Array.isArray(node?.tags) ? node.tags.join(", ") : "";
    $("#nodeNotes").value = node?.notes || "";
    const config = { ...(node?.config || {}) }; delete config.vendor_model;
    $("#nodeConfig").value = Object.keys(config).length ? JSON.stringify(config, null, 2) : "";
    $("#nodeDialogKicker").textContent = node ? "DEVICE SETTINGS" : "NEW DEVICE";
    $("#nodeDialogTitle").textContent = node ? `Edit ${node.name}` : "Add a device";
    $("#nodeSubmit").textContent = node ? "Save changes" : "Add device";
    form.dataset.baseRevision = String(app.state.revision ?? 0);
    $("#nodeDialog").showModal();
    setTimeout(() => $("#nodeName").focus(), 30);
  }

  async function submitNode(event) {
    if (event.submitter?.value === "cancel") return;
    event.preventDefault();
    const form = event.currentTarget;
    if (!form.reportValidity()) return;
    let config = {};
    if ($("#nodeConfig").value.trim()) {
      try { config = JSON.parse($("#nodeConfig").value); if (!config || Array.isArray(config) || typeof config !== "object") throw new Error(); }
      catch (_) { toast("Invalid configuration", "Configuration must be a valid JSON object.", "error"); $("#nodeConfig").focus(); return; }
    }
    const vendor = $("#nodeVendor").value.trim();
    const id = $("#nodeId").value;
    const payload = {
      name: $("#nodeName").value.trim(), kind: $("#nodeType").value, status: $("#nodeStatus").value,
      ip: $("#nodeIp").value.trim(), mac: $("#nodeMac").value.trim(), hostname: $("#nodeHostname").value.trim(),
      vendor, management_url: $("#nodeManagementUrl").value.trim(), winbox_enabled: $("#nodeWinboxEnabled").checked,
      tags: $("#nodeTags").value.split(",").map(item => item.trim()).filter(Boolean), notes: $("#nodeNotes").value.trim(), config
    };
    if (!id) {
      const position = nextNodePosition(); payload.x = position.x; payload.y = position.y;
    }
    const submit = $("#nodeSubmit"); setBusy(submit, true, id ? "Saving…" : "Adding…");
    try {
      const next = await api(id ? `/api/nodes/${encodeURIComponent(id)}` : "/api/nodes", { method: id ? "PATCH" : "POST", body: JSON.stringify(payload), expectedRevision: Number(form.dataset.baseRevision) });
      $("#nodeDialog").close(); applyState(next, { fit: !id });
      toast(id ? "Device updated" : "Device added", `${payload.name} is synced to the workspace.`);
      if (!id) {
        const created = [...next.nodes].reverse().find(node => node.name === payload.name) || next.nodes.at(-1);
        if (created) selectNode(created.id);
      }
    } catch (error) { reportError(id ? "Could not update device" : "Could not add device", error); }
    finally { setBusy(submit, false); }
  }

  function nextNodePosition() {
    ensurePositions();
    const count = app.state.nodes.length; const angle = count * 2.4; const radius = 90 + 28 * Math.sqrt(count);
    return { x: Math.round(Math.cos(angle) * radius), y: Math.round(Math.sin(angle) * radius) };
  }

  function populateLinkSelects(selectedSource, selectedTarget) {
    const options = app.state.nodes.map(node => `<option value="${escapeHtml(node.id)}">${escapeHtml(node.name || node.ip || "Unnamed device")}</option>`).join("");
    $("#linkSource").innerHTML = `<option value="">Choose source…</option>${options}`;
    $("#linkTarget").innerHTML = `<option value="">Choose target…</option>${options}`;
    $("#linkSource").value = selectedSource ? String(selectedSource) : "";
    $("#linkTarget").value = selectedTarget ? String(selectedTarget) : "";
  }

  function openLinkDialog(id = null, sourceHint = null) {
    if (app.state.nodes.length < 2) { toast("Two devices required", "Add another device before creating a connection.", "warning"); return; }
    const link = id ? linkById(id) : null;
    $("#linkForm").reset(); $("#linkId").value = link?.id ?? "";
    populateLinkSelects(link ? endpointId(link, "source") : sourceHint, link ? endpointId(link, "target") : null);
    $("#linkLabel").value = linkName(link || {});
    $("#linkType").value = linkKind(link || { kind: "ethernet" });
    $("#linkSpeed").value = link?.bandwidth_mbps ?? "";
    $("#linkStatus").value = link?.status || "active";
    $("#linkDirected").checked = Boolean(link?.directed);
    $("#linkNotes").value = link?.notes || "";
    $("#linkDialogTitle").textContent = link ? "Edit connection" : "Add a connection";
    $("#linkSubmit").textContent = link ? "Save changes" : "Add connection";
    $("#linkForm").dataset.baseRevision = String(app.state.revision ?? 0);
    $("#linkDialog").showModal();
  }

  async function submitLink(event) {
    if (event.submitter?.value === "cancel") return;
    event.preventDefault(); if (!event.currentTarget.reportValidity()) return;
    const id = $("#linkId").value;
    const source = $("#linkSource").value; const target = $("#linkTarget").value;
    if (source === target) { toast("Choose two devices", "A connection cannot link a device to itself.", "error"); return; }
    const speed = $("#linkSpeed").value;
    const payload = { source, target, name: $("#linkLabel").value.trim(), kind: $("#linkType").value, status: $("#linkStatus").value, directed: $("#linkDirected").checked, bandwidth_mbps: speed === "" ? null : Number(speed), notes: $("#linkNotes").value.trim(), config: linkById(id)?.config || {} };
    const submit = $("#linkSubmit"); setBusy(submit, true, "Saving…");
    try {
      const next = await api(id ? `/api/links/${encodeURIComponent(id)}` : "/api/links", { method: id ? "PATCH" : "POST", body: JSON.stringify(payload), expectedRevision: Number(event.currentTarget.dataset.baseRevision) });
      $("#linkDialog").close(); applyState(next); toast(id ? "Connection updated" : "Devices connected", payload.name || `${nodeById(source)?.name} ↔ ${nodeById(target)?.name}`);
    } catch (error) { reportError("Could not save connection", error); }
    finally { setBusy(submit, false); }
  }

  function askConfirmation({ title, message, label = "Remove", action }) {
    $("#confirmTitle").textContent = title; $("#confirmMessage").textContent = message; $("#confirmButton").textContent = label;
    app.confirmAction = action; $("#confirmDialog").returnValue = ""; $("#confirmDialog").showModal();
  }

  function confirmDeleteNode(id) {
    const node = nodeById(id); if (!node) return;
    const links = app.state.links.filter(link => String(endpointId(link, "source")) === String(id) || String(endpointId(link, "target")) === String(id)).length;
    askConfirmation({ title: `Remove ${node.name}?`, message: links ? `This also removes ${links} connected ${links === 1 ? "link" : "links"}. This action cannot be undone.` : "This device will be permanently removed from the map.", action: async () => {
      const next = await api(`/api/nodes/${encodeURIComponent(id)}`, { method: "DELETE" }); applyState(next); clearSelection(); toast("Device removed", `${node.name} was removed from the workspace.`);
    }});
  }

  function confirmDeleteLink(id) {
    const link = linkById(id); if (!link) return;
    askConfirmation({ title: "Remove connection?", message: "The devices will remain on the map, but this connection will be removed.", action: async () => {
      const next = await api(`/api/links/${encodeURIComponent(id)}`, { method: "DELETE" }); applyState(next); toast("Connection removed", "The topology has been updated.");
    }});
  }

  async function runConfirmedAction() {
    const action = app.confirmAction; app.confirmAction = null; if (!action) return;
    const button = $("#confirmButton"); setBusy(button, true, "Removing…");
    try { await action(); } catch (error) { reportError("Action failed", error); }
    finally { setBusy(button, false); }
  }

  async function submitSettings(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = { name: $("#settingName").value.trim() || "Main network", description: $("#settingDescription").value.trim(), subnet: $("#settingSubnet").value.trim(), refresh_interval: Number($("#settingRefresh").value), show_link_labels: $("#settingLinkLabels").checked, compact_labels: $("#settingCompact").checked };
    const button = $('button[type="submit"]', form); setBusy(button, true, "Saving…");
    try { const next = await api("/api/settings", { method: "PATCH", body: JSON.stringify(payload), expectedRevision: Number(form.dataset.baseRevision) }); applyState(next); form.dataset.baseRevision = String(app.state.revision); $("#settingsHint").textContent = "Saved just now"; toast("Workspace updated", "Your preferences are synced."); }
    catch (error) { reportError("Could not save settings", error); }
    finally { setBusy(button, false); }
  }

  async function replaceState(payload, successMessage, expectedRevision = app.state.revision) {
    const next = await api("/api/state", { method: "PUT", body: JSON.stringify(payload), expectedRevision });
    app.rawDirty = false; applyState(next, { fit: true }); toast("Workspace imported", successMessage);
  }

  async function saveRawJson() {
    const parsed = validateRawJson(); if (!parsed) { toast("Invalid JSON", "Fix the highlighted JSON error before saving.", "error"); return; }
    askConfirmation({ title: "Replace this workspace?", message: "All current devices, connections, and settings will be replaced by the JSON editor contents.", label: "Replace workspace", action: () => replaceState(parsed, "The JSON configuration is now active.", app.rawBaseRevision) });
  }

  async function importFile(file) {
    if (!file) return;
    try {
      const parsed = JSON.parse(await file.text());
      if (!Array.isArray(parsed.nodes) || !Array.isArray(parsed.links) || typeof parsed.settings !== "object") throw new Error("This is not a valid NetworkMap workspace file.");
      const baseRevision = app.state.revision;
      askConfirmation({ title: "Import this workspace?", message: `Import ${parsed.nodes.length} devices and ${parsed.links.length} connections, replacing the current map?`, label: "Import workspace", action: () => replaceState(parsed, `${parsed.nodes.length} devices and ${parsed.links.length} connections were loaded.`, baseRevision) });
    } catch (error) { reportError("Could not import file", error); }
    finally { $("#importFile").value = ""; }
  }

  async function exportState() {
    try {
      let payload;
      try { payload = await api("/api/export"); } catch (error) { if (error.status === 404) payload = serializableState(); else throw error; }
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const link = document.createElement("a"); const url = URL.createObjectURL(blob); const date = new Date().toISOString().slice(0, 10);
      link.href = url; link.download = `networkmap-${date}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("Backup downloaded", "Your workspace was exported as JSON.");
    } catch (error) { reportError("Export failed", error); }
  }

  function openDiscovery() {
    $("#discoveryForm").reset(); $("#discoveryCidr").value = app.state.settings.subnet || app.state.settings.discovery_cidr || "192.168.1.0/24"; $("#discoveryNmap").checked = true;
    $("#discoverySetup").classList.remove("hidden"); $("#discoveryProgress").classList.add("hidden"); $("#discoveryResults").classList.add("hidden");
    $("#discoverySubmit").textContent = "Start scan"; $("#discoverySubmit").disabled = false; app.discoveryResults = [];
    $("#discoveryDialog").showModal(); setTimeout(() => $("#discoveryCidr").focus(), 30);
  }

  async function handleDiscoverySubmit(event) {
    if (event.submitter?.value === "cancel") return;
    event.preventDefault();
    if (app.discoveryResults.length) { await importDiscovered(); return; }
    if (!event.currentTarget.reportValidity()) return;
    $("#discoverySetup").classList.add("hidden"); $("#discoveryProgress").classList.remove("hidden");
    const submit = $("#discoverySubmit"); setBusy(submit, true, "Scanning…");
    let result = null;
    try {
      result = await api("/api/discovery", { method: "POST", body: JSON.stringify({ cidr: $("#discoveryCidr").value.trim(), use_nmap: $("#discoveryNmap").checked }) });
      app.discoveryResults = Array.isArray(result) ? result : (result.devices || result.nodes || result.discovered || []);
      if (Array.isArray(result.warnings) && result.warnings.length) toast("Scan completed with notes", result.warnings.join(" · "), "warning");
    } catch (error) {
      $("#discoverySetup").classList.remove("hidden"); reportError("Scan failed", error);
    } finally { $("#discoveryProgress").classList.add("hidden"); setBusy(submit, false); if (result) renderDiscoveryResults(); }
  }

  function renderDiscoveryResults() {
    const results = app.discoveryResults;
    $("#discoveryResults").classList.remove("hidden");
    $("#discoveryCount").textContent = `${results.length} ${results.length === 1 ? "device" : "devices"} found`;
    const existingKeys = new Set(app.state.nodes.flatMap(node => [node.mac, node.ip].filter(Boolean).map(value => value.toLowerCase())));
    $("#discoveryList").innerHTML = results.length ? results.map((node, index) => {
      const duplicate = [node.mac, node.ip].some(value => value && existingKeys.has(String(value).toLowerCase()));
      return `<label class="discovered-device"><input type="checkbox" data-index="${index}" ${duplicate ? "disabled" : "checked"}><span class="device-avatar">${iconMarkup(node.kind || "other")}</span><span><strong>${escapeHtml(node.name || node.hostname || node.ip || "Discovered device")}</strong><small>${escapeHtml(titleCase(node.kind || "other"))}${duplicate ? " · Already mapped" : ""}</small></span><span>${escapeHtml(node.ip || node.mac || "")}</span></label>`;
    }).join("") : '<div class="table-empty"><h3>No devices found</h3><p>Check the subnet and try again.</p></div>';
    $("#discoverySubmit").textContent = results.length ? `Add selected (${selectedDiscoveryCount()})` : "Scan again";
    if (!results.length) { app.discoveryResults = []; $("#discoverySetup").classList.remove("hidden"); $("#discoveryResults").classList.add("hidden"); }
  }

  function selectedDiscoveryCount() { return $$('#discoveryList input[type="checkbox"]:checked').length; }

  async function importDiscovered() {
    const selected = $$('#discoveryList input[type="checkbox"]:checked').map(input => app.discoveryResults[Number(input.dataset.index)]);
    if (!selected.length) { toast("Nothing selected", "Choose at least one discovered device to add.", "warning"); return; }
    const submit = $("#discoverySubmit"); setBusy(submit, true, `Adding 0/${selected.length}…`);
    let latest = null; let added = 0;
    try {
      for (const discovered of selected) {
        const position = nextNodePosition();
        const allowed = { name: discovered.name || discovered.hostname || discovered.ip || "Discovered device", kind: discovered.kind || "other", ip: discovered.ip || "", mac: discovered.mac || "", hostname: discovered.hostname || "", vendor: discovered.vendor || "", status: discovered.status || "online", x: position.x, y: position.y, notes: discovered.notes || "Discovered by network scan", tags: Array.isArray(discovered.tags) ? discovered.tags : ["discovered"], config: discovered.config && typeof discovered.config === "object" ? discovered.config : {} };
        latest = await api("/api/nodes", { method: "POST", body: JSON.stringify(allowed) }); added++; applyState(latest); setBusy(submit, true, `Adding ${added}/${selected.length}…`);
      }
      $("#discoveryDialog").close(); if (latest) applyState(latest, { fit: true }); toast("Devices imported", `${added} ${added === 1 ? "device was" : "devices were"} added to the map.`);
    } catch (error) { if (!error.handled) toast("Import stopped", `${added} added. ${error.message}`, "error"); }
    finally { setBusy(submit, false); }
  }

  function setBusy(button, busy, label) {
    if (busy) { button.dataset.originalLabel ||= button.textContent; button.disabled = true; button.textContent = label; }
    else { button.disabled = false; if (button.dataset.originalLabel) { button.textContent = button.dataset.originalLabel; delete button.dataset.originalLabel; } }
  }

  function reportError(title, error, type = "error") {
    if (!error?.handled) toast(title, error?.message || "An unexpected error occurred.", type);
  }

  function toast(title, message = "", type = "success") {
    const item = document.createElement("div"); item.className = `toast ${type}`; item.setAttribute("role", type === "error" ? "alert" : "status");
    item.innerHTML = `<span class="toast-icon">${type === "success" ? "✓" : type === "warning" ? "!" : "×"}</span><span><strong>${escapeHtml(title)}</strong>${message ? `<small>${escapeHtml(message)}</small>` : ""}</span><button aria-label="Dismiss notification">×</button>`;
    const close = () => { item.classList.add("out"); setTimeout(() => item.remove(), 220); };
    $("button", item).addEventListener("click", close); $("#toastRegion").append(item); setTimeout(close, type === "error" ? 6500 : 4200);
  }

  function toggleTheme() {
    const current = document.documentElement.dataset.theme || "dark"; const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("networkmap_theme", next); } catch (_) { /* unavailable */ }
  }

  function restoreTheme() {
    let theme = "";
    try { theme = localStorage.getItem("networkmap_theme") || localStorage.getItem("netatlas_theme") || ""; } catch (_) { /* unavailable */ }
    if (!theme && window.matchMedia?.("(prefers-color-scheme: light)").matches) theme = "light";
    document.documentElement.dataset.theme = theme || "dark";
  }

  function attachEvents() {
    $$(".nav-item").forEach(button => button.addEventListener("click", () => switchView(button.dataset.view)));
    $$('[data-config-tab]').forEach(button => button.addEventListener("click", () => switchConfigTab(button.dataset.configTab)));
    $$('[data-action="add-node"]').forEach(button => button.addEventListener("click", () => openNodeDialog()));
    $$('[data-action="add-link"]').forEach(button => button.addEventListener("click", () => openLinkDialog()));
    $$('[data-action="discover"]').forEach(button => button.addEventListener("click", openDiscovery));
    $("#discoverButton").addEventListener("click", openDiscovery);
    $("#nodeForm").addEventListener("submit", submitNode);
    $("#linkForm").addEventListener("submit", submitLink);
    $("#settingsForm").addEventListener("submit", submitSettings);
    $("#inventorySearch").addEventListener("input", renderDeviceTable);
    $("#statusFilter").addEventListener("change", renderDeviceTable);
    $("#deviceTableBody").addEventListener("click", handleDeviceTableClick);
    $("#linkTableBody").addEventListener("click", handleLinkTableClick);
    $("#mapSearch").addEventListener("input", applyMapSearch);
    $("#closeInspector").addEventListener("click", clearSelection);
    $("#openSelected").addEventListener("click", openSelectedManagement);
    $("#winboxSelected").addEventListener("click", openSelectedWinbox);
    $("#editSelected").addEventListener("click", () => openNodeDialog(app.selectedNodeId));
    $("#deleteSelected").addEventListener("click", () => confirmDeleteNode(app.selectedNodeId));
    $("#focusSelected").addEventListener("click", () => focusNode(app.selectedNodeId));
    $("#connectSelected").addEventListener("click", () => openLinkDialog(null, app.selectedNodeId));
    $("#zoomIn").addEventListener("click", () => zoomAt(1.2));
    $("#zoomOut").addEventListener("click", () => zoomAt(1 / 1.2));
    $("#fitMap").addEventListener("click", () => fitMap());
    $("#mapMenuButton").addEventListener("click", autoLayout);
    $("#topologyEditButton").addEventListener("click", () => setTopologyEditing(!app.topologyEditing));
    $("#openConfigurationButton").addEventListener("click", () => { switchView("configuration"); switchConfigTab("inventory"); });
    const topology = $("#topology");
    topology.addEventListener("pointerdown", startPan); topology.addEventListener("pointermove", movePointer); topology.addEventListener("pointerup", endPointer); topology.addEventListener("pointercancel", endPointer);
    topology.addEventListener("wheel", event => { event.preventDefault(); zoomAt(event.deltaY < 0 ? 1.1 : 1 / 1.1, event.clientX, event.clientY); }, { passive: false });
    $("#menuButton").addEventListener("click", () => { const open = $("#appShell").classList.toggle("nav-open"); $("#menuButton").setAttribute("aria-expanded", String(open)); });
    $("#sidebarScrim").addEventListener("click", () => { $("#appShell").classList.remove("nav-open"); $("#menuButton").setAttribute("aria-expanded", "false"); });
    $("#themeButton").addEventListener("click", toggleTheme);
    $("#confirmDialog").addEventListener("close", () => { if ($("#confirmDialog").returnValue === "confirm") runConfirmedAction(); else app.confirmAction = null; });
    $("#rawJson").addEventListener("input", () => { app.rawDirty = true; validateRawJson(); });
    $("#rawJson").addEventListener("keydown", event => { if (event.key === "Tab") { event.preventDefault(); const field = event.currentTarget; const start = field.selectionStart; field.setRangeText("  ", start, field.selectionEnd, "end"); field.dispatchEvent(new Event("input")); } });
    $("#saveJsonButton").addEventListener("click", saveRawJson);
    $("#copyJsonButton").addEventListener("click", async () => { try { await navigator.clipboard.writeText($("#rawJson").value); toast("Copied to clipboard", "Workspace JSON is ready to paste."); } catch (_) { $("#rawJson").select(); document.execCommand("copy"); toast("Copied to clipboard"); } });
    $("#exportButton").addEventListener("click", exportState);
    $("#importButton").addEventListener("click", () => $("#importFile").click());
    $("#importFile").addEventListener("change", event => importFile(event.target.files[0]));
    $("#discoveryForm").addEventListener("submit", handleDiscoverySubmit);
    $("#discoveryList").addEventListener("change", () => { $("#discoverySubmit").textContent = `Add selected (${selectedDiscoveryCount()})`; });
    $("#toggleDiscoverySelection").addEventListener("click", () => { const boxes = $$('#discoveryList input[type="checkbox"]:not(:disabled)'); const shouldSelect = boxes.some(box => !box.checked); boxes.forEach(box => { box.checked = shouldSelect; }); $("#toggleDiscoverySelection").textContent = shouldSelect ? "Clear all" : "Select all"; $("#discoverySubmit").textContent = `Add selected (${selectedDiscoveryCount()})`; });
    $("#authDialog").addEventListener("cancel", event => event.preventDefault());
    $("#authForm").addEventListener("submit", async event => {
      event.preventDefault(); const token = $("#authToken").value.trim(); if (!token) return;
      app.token = token; try { sessionStorage.setItem("networkmap_token", token); } catch (_) { /* unavailable */ }
      const button = $('button[type="submit"]', event.currentTarget); setBusy(button, true, "Connecting…");
      try { await api("/api/session", { method: "POST" }); forgetBootstrapToken(); await fetchState({ fit: true }); $("#authToken").value = ""; $("#authDialog").close(); toast("Connected", "Secure session established."); }
      catch (error) { if (error.status !== 401) toast("Connection failed", error.message, "error"); }
      finally { setBusy(button, false); }
    });
    window.addEventListener("resize", () => { if (app.activeView === "overview" && app.state.nodes.length) fitMap(); });
    window.addEventListener("beforeunload", closeEvents);
  }

  async function loadInitialState() {
    if (app.token) {
      try { await api("/api/session", { method: "POST" }); forgetBootstrapToken(); }
      catch (error) { if (error.status !== 401) reportError("Could not establish session", error); return; }
    }
    fetchState({ fit: true }).catch(() => {});
  }

  function init() {
    readInitialToken(); restoreTheme(); attachEvents(); renderAll();
    setTopologyEditing(false);
    loadInitialState();
    if ("serviceWorker" in navigator && location.protocol !== "file:") navigator.serviceWorker.register("/static/sw.js").catch(() => {});
  }

  init();
})();
