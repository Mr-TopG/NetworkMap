(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const svgNS = "http://www.w3.org/2000/svg";
  const emptyState = { revision: 0, updated_at: null, nodes: [], links: [], areas: [], settings: {} };
  const app = {
    state: { ...emptyState },
    loaded: false,
    connected: false,
    selectedNodeId: null,
    selectedLinkId: null,
    activeView: "overview",
    activeConfigTab: "inventory",
    topologyEditing: false,
    syncStatus: null,
    syncStatusTimer: null,
    token: "",
    eventSource: null,
    eventRefreshTimer: null,
    refreshTimer: null,
    layoutFitTimer: null,
    nodeIndex: new Map(),
    linkIndex: new Map(),
    linksByNode: new Map(),
    visualPositions: new Map(),
    transform: { x: 0, y: 0, scale: 1 },
    interaction: null,
    rawDirty: false,
    rawBaseRevision: 0,
    discoveryResults: [],
    confirmAction: null
  };
  const deviceIconKinds = new Set([
    "router", "switch", "server", "workstation", "laptop", "mobile", "access-point",
    "firewall", "printer", "camera", "storage", "nas", "iot", "cloud", "other"
  ]);

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
    return `<img src="${deviceIconPath(kind)}" alt="" width="48" height="36" draggable="false">`;
  }

  function deviceIconPath(kind) {
    return `/static/devices/${deviceIconKinds.has(kind) ? kind : "other"}.svg`;
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
  function nodeById(id) { return app.nodeIndex.get(String(id)); }
  function linkById(id) { return app.linkIndex.get(String(id)); }
  function linksForNode(id) { return app.linksByNode.get(String(id)) || []; }
  function endpointId(link, end) { return link[end] ?? link[`${end}_id`] ?? ""; }
  function linkUiStatus(status) { return status === "active" || status === "online" ? "online" : status === "inactive" || status === "offline" ? "offline" : status === "degraded" ? "degraded" : "unknown"; }

  const SPEED_PRESETS = [10, 100, 1000, 2500, 5000, 10000, 25000, 40000, 50000, 100000, 200000, 400000, 800000];
  const DUPLEX_LABELS = { unknown: "Duplex not specified", full: "Full duplex", half: "Half duplex", auto: "Auto-negotiation" };

  function formatLinkSpeed(value) {
    if (value == null || value === "") return "Not specified";
    const mbps = Number(value);
    if (!Number.isFinite(mbps) || mbps < 0) return "Not specified";
    const number = mbps >= 1000 ? mbps / 1000 : mbps;
    return `${number.toLocaleString(undefined, { maximumFractionDigits: 12 })} ${mbps >= 1000 ? "Gbps" : "Mbps"}`;
  }

  function formatLinkDuplex(value) {
    return Object.hasOwn(DUPLEX_LABELS, value) ? DUPLEX_LABELS[value] : DUPLEX_LABELS.unknown;
  }

  function setLinkSpeedMode() {
    const custom = $("#linkSpeedPreset").value === "custom";
    $("#linkCustomSpeedField").hidden = !custom;
    $("#linkSpeed").disabled = !custom;
    $("#linkSpeed").required = custom;
  }

  function rebuildStateIndexes() {
    app.nodeIndex = new Map(app.state.nodes.map(node => [String(node.id), node]));
    app.linkIndex = new Map(app.state.links.map(link => [String(link.id), link]));
    app.linksByNode = new Map(app.state.nodes.map(node => [String(node.id), []]));
    app.state.links.forEach(link => {
      const source = String(endpointId(link, "source"));
      const target = String(endpointId(link, "target"));
      app.linksByNode.get(source)?.push(link);
      if (target !== source) app.linksByNode.get(target)?.push(link);
    });
  }

  function managementUrl(node) {
    const configured = String(node?.management_url || "").trim();
    if (configured) {
      try {
        const parsed = new URL(configured);
        if (["http:", "https:"].includes(parsed.protocol) && !parsed.username && !parsed.password) return parsed.href;
      } catch (_) { /* invalid legacy value */ }
    }
    if (nodeKind(node || {}) === "server") return "";
    const address = String(node?.ip || node?.hostname || "").trim();
    if (!address) return "";
    const host = address.includes(":") && !address.startsWith("[") ? `[${address}]` : address;
    return `http://${host}/`;
  }

  function isMikrotik(node) {
    return /mikrotik/i.test(String(node?.vendor || ""));
  }

  function winboxTarget(node) {
    return String(node?.ip || node?.mac || node?.hostname || "").trim();
  }

  function sshUrl(node) {
    if (nodeKind(node || {}) !== "server") return "";
    const address = String(node?.ip || node?.hostname || "").trim();
    if (!address) return "";
    return `networkmap-ssh://connect/${encodeURIComponent(address)}`;
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
    $("#mapTip").textContent = app.topologyEditing ? "Edit mode · Drag devices/areas · Click to edit areas or links" : "View mode · Click a device to inspect";
    if (app.loaded) { renderMap(); renderInspector(); }
  }

  function scheduleMapFit(delay = 0) {
    clearTimeout(app.layoutFitTimer);
    if (!app.loaded || app.activeView !== "overview" || (!app.state.nodes.length && !app.state.areas.length)) return;
    app.layoutFitTimer = setTimeout(() => {
      app.layoutFitTimer = null;
      fitMap();
    }, delay);
  }

  function storeLayoutPreference(key, value) {
    try { localStorage.setItem(key, value ? "1" : "0"); } catch (_) { /* unavailable */ }
  }

  function setSidebarCollapsed(collapsed, { persist = true } = {}) {
    const isCollapsed = Boolean(collapsed);
    $("#appShell").classList.toggle("sidebar-collapsed", isCollapsed);
    const button = $("#sidebarCollapseButton");
    button.setAttribute("aria-expanded", String(!isCollapsed));
    button.setAttribute("aria-label", isCollapsed ? "Expand navigation" : "Collapse navigation");
    button.title = isCollapsed ? "Expand navigation" : "Collapse navigation";
    if (persist) storeLayoutPreference("networkmap_sidebar_collapsed", isCollapsed);
    scheduleMapFit(280);
  }

  function setTopbarHidden(hidden, { persist = true, focusControl = false } = {}) {
    const isHidden = Boolean(hidden);
    $("#appShell").classList.toggle("topbar-hidden", isHidden);
    $("#topbar").hidden = isHidden;
    $("#overviewHeading").hidden = isHidden;
    $("#networkSummary").hidden = isHidden;
    $("#topbarShowButton").hidden = !isHidden;
    if (persist) storeLayoutPreference("networkmap_topbar_hidden", isHidden);
    if (focusControl) (isHidden ? $("#topbarShowButton") : $("#topbarHideButton")).focus();
    scheduleMapFit();
  }

  function restoreLayout() {
    let sidebarCollapsed = false;
    let topbarHidden = false;
    try {
      sidebarCollapsed = localStorage.getItem("networkmap_sidebar_collapsed") === "1";
      topbarHidden = localStorage.getItem("networkmap_topbar_hidden") === "1";
    } catch (_) { /* unavailable */ }
    setSidebarCollapsed(sidebarCollapsed, { persist: false });
    setTopbarHidden(topbarHidden, { persist: false });
  }

  async function api(path, options = {}) {
    const { expectedRevision, ...requestOptions } = options;
    const headers = new Headers(requestOptions.headers || {});
    const method = String(requestOptions.method || "GET").toUpperCase();
    headers.set("Accept", "application/json");
    if (app.token) headers.set("Authorization", `Bearer ${app.token}`);
    if (requestOptions.body && !(requestOptions.body instanceof FormData)) headers.set("Content-Type", "application/json");
    const stateMutation = ["POST", "PUT", "PATCH", "DELETE"].includes(method)
      && !["/api/discovery", "/api/session", "/api/sync/actions"].includes(path);
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
      if (response.status === 409 && payload?.error?.code === "revision_conflict") {
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
      areas: Array.isArray(next.areas) ? next.areas : [],
      settings: next.settings && typeof next.settings === "object" ? next.settings : {}
    };
    rebuildStateIndexes();
    app.loaded = true;
    if (app.selectedNodeId && !app.nodeIndex.has(String(app.selectedNodeId))) app.selectedNodeId = null;
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
    const next = Boolean(connected);
    if (app.connected === next) return;
    app.connected = next;
    renderSyncStatus();
  }

  function syncRemoteLabel(value) {
    if (!value) return "Not configured";
    try {
      const parsed = new URL(value);
      return parsed.host || value;
    } catch (_) {
      return String(value);
    }
  }

  function syncTimeLabel(value) {
    if (!value) return "Never";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Unknown" : relativeTime(date);
  }

  function renderSyncStatus() {
    const pill = $("#connectionPill");
    const pulse = $(".pulse-dot");
    const workspaceDot = $("#workspaceStatusDot");
    pill.classList.remove("connected", "disconnected", "warning");
    pulse.classList.remove("disconnected", "warning");
    workspaceDot.classList.remove("online", "offline", "unknown");
    if (!app.connected) {
      pill.classList.add("disconnected");
      pulse.classList.add("disconnected");
      workspaceDot.classList.add("offline");
      $("span:last-child", pill).textContent = "Offline";
      $("#workspaceMode").textContent = "Local workspace unavailable";
      $("#syncLabel").textContent = "Connection lost";
      $("#syncDetail").textContent = "Trying to reopen the workspace";
      $("#syncCardTitle").textContent = "Workspace unavailable";
      $("#syncCardDetail").textContent = "The local NetworkMap service is not responding.";
      $("#syncNowButton").hidden = true;
      $("#useHostedButton").hidden = true;
      $("#useLocalButton").hidden = true;
      $("#syncBackupNote").classList.add("hidden");
      return;
    }

    const status = app.syncStatus || { mode: "server", state: "hosted" };
    const state = status.state || "hosted";
    const pending = Math.max(0, Number(status.pending_changes) || 0);
    const views = {
      hosted: ["Hosted", "Hosted server ready", "Shared server workspace", "Hosted workspace", "This server is ready for browser and Linux clients.", "ok"],
      "local-only": ["Local", "Saved locally", "Hosted sync not configured", "Local-only workspace", "Connect a hosted server from the Linux app to synchronize this copy.", "ok"],
      checking: ["Syncing", "Checking hosted server…", "Local editing remains available", "Checking hosted server", status.message || "Comparing the local and hosted copies.", "warning"],
      synced: ["Synced", "Up to date", `Synchronized with ${syncRemoteLabel(status.remote_url)}`, "Copies are up to date", status.message || "Local and hosted topologies contain the same version.", "ok"],
      pushing: ["Syncing", "Uploading local changes…", "The local copy stays available", "Uploading changes", status.message || "Publishing the local topology to the hosted server.", "warning"],
      pulling: ["Syncing", "Updating local copy…", "The local copy stays available", "Getting hosted version", status.message || "Updating this computer from the hosted topology.", "warning"],
      offline: ["Local", pending ? "Changes waiting" : "Working locally", "Hosted server is unavailable", "Working locally", status.message || "Changes are safe here and synchronization will retry automatically.", "warning"],
      "auth-required": ["Attention", "Sign-in required", "Local changes are safe", "Hosted sign-in required", status.message || "Update the hosted server token and try again.", "error"],
      conflict: ["Attention", "Sync needs attention", "Both copies changed", "Choose which copy to keep", status.message || "Nothing was overwritten because both topologies changed.", "error"],
      error: ["Attention", "Synchronization paused", "Local changes are safe", "Could not synchronize", status.message || "Review the error and try again.", "error"]
    };
    const view = views[state] || views.error;
    $("span:last-child", pill).textContent = view[0];
    $("#syncLabel").textContent = view[1];
    $("#syncDetail").textContent = view[2];
    $("#syncCardTitle").textContent = pending && state === "offline" ? `${pending} ${pending === 1 ? "change" : "changes"} waiting` : view[3];
    $("#syncCardDetail").textContent = view[4];
    $("#workspaceMode").textContent = status.mode === "native-sync" ? (state === "synced" ? "Local copy · synchronized" : "Local copy") : "Hosted workspace";
    const tone = view[5];
    pill.classList.add(tone === "ok" ? "connected" : tone === "warning" ? "warning" : "disconnected");
    if (tone === "warning") pulse.classList.add("warning");
    else if (tone === "error") pulse.classList.add("disconnected");
    workspaceDot.classList.add(tone === "ok" ? "online" : tone === "warning" ? "unknown" : "offline");
    $("#syncRemote").textContent = syncRemoteLabel(status.remote_url);
    $("#syncRemote").title = status.remote_url || "";
    $("#syncLastRun").textContent = syncTimeLabel(status.last_sync_at);
    const nativeSync = status.mode === "native-sync" && Boolean(status.remote_url);
    const canResolve = nativeSync
      && state === "conflict"
      && status.can_resolve !== false
      && /^[0-9a-f]{64}$/.test(String(status.decision_id || ""));
    $("#syncNowButton").hidden = !nativeSync || ["checking", "pushing", "pulling", "conflict"].includes(state);
    $("#useHostedButton").hidden = !canResolve;
    $("#useLocalButton").hidden = !canResolve;
    $("#syncBackupNote").classList.toggle("hidden", !canResolve);
  }

  async function fetchSyncStatus() {
    try {
      app.syncStatus = await api("/api/sync/status");
    } catch (error) {
      if (error.status === 404) app.syncStatus = { mode: "server", state: "hosted" };
    }
    renderSyncStatus();
  }

  async function requestSyncAction(action) {
    const labels = { "sync-now": "Sync requested", "use-local": "Uploading local copy", "use-hosted": "Getting hosted copy" };
    try {
      const request = { action };
      if (["use-local", "use-hosted"].includes(action)) {
        request.decision_id = String(app.syncStatus?.decision_id || "");
      }
      await api("/api/sync/actions", { method: "POST", body: JSON.stringify(request) });
      toast(labels[action] || "Sync requested", action === "sync-now" ? "NetworkMap will check both copies now." : "A backup will be created before either copy is replaced.");
      app.syncStatus = { ...(app.syncStatus || {}), state: "checking", can_resolve: false, message: "Applying your synchronization choice." };
      renderSyncStatus();
      setTimeout(() => fetchSyncStatus().catch(() => {}), 1200);
    } catch (error) {
      reportError("Could not request synchronization", error);
    }
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
    if (app.activeView === "overview") {
      renderMap();
      renderInspector();
    } else {
      renderActiveConfigPanel();
    }
  }

  function renderActiveConfigPanel({ forceRaw = false } = {}) {
    if (app.activeConfigTab === "inventory") renderDeviceTable();
    else if (app.activeConfigTab === "links") renderLinkTable();
    else if (app.activeConfigTab === "areas") areaEditor.renderTable();
    else if (app.activeConfigTab === "settings") renderSettings();
    else if (app.activeConfigTab === "data") renderRawJson(forceRaw);
  }

  function renderSummary() {
    const nodes = app.state.nodes;
    let online = 0;
    let issues = 0;
    nodes.forEach(node => {
      if (node.status === "online") online += 1;
      else if (node.status === "offline" || node.status === "degraded") issues += 1;
    });
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
      $("#lastUpdated").textContent = Number.isNaN(date.getTime()) ? "Saved just now" : `Saved ${relativeTime(date)}`;
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

  function hasStoredPosition(node) {
    return node.x !== null && node.x !== "" && node.y !== null && node.y !== ""
      && Number.isFinite(Number(node.x)) && Number.isFinite(Number(node.y));
  }

  function ensurePositions() {
    if (app.visualPositions.size === app.state.nodes.length) return;
    const unplacedCount = app.state.nodes.reduce((count, node) => count + (hasStoredPosition(node) ? 0 : 1), 0);
    let unplacedIndex = 0;
    app.state.nodes.forEach(node => {
      const id = String(node.id);
      if (app.visualPositions.has(id)) {
        if (!hasStoredPosition(node)) unplacedIndex += 1;
        return;
      }
      if (hasStoredPosition(node)) {
        app.visualPositions.set(id, { x: Number(node.x), y: Number(node.y) });
      } else {
        const count = Math.max(unplacedCount, 1);
        const angle = (unplacedIndex / count) * Math.PI * 2 - Math.PI / 2;
        const radius = count <= 1 ? 0 : 170 + Math.floor(unplacedIndex / 10) * 100;
        app.visualPositions.set(id, { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius });
        unplacedIndex += 1;
      }
    });
  }

  function renderMap() {
    $("#mapLoading").classList.toggle("hidden", app.loaded);
    $("#mapEmpty").classList.toggle("hidden", !app.loaded || app.state.nodes.length > 0 || app.state.areas.length > 0);
    areaEditor.renderMap();
    const linkLayer = $("#linkLayer");
    const nodeLayer = $("#nodeLayer");
    if (!app.state.nodes.length) {
      linkLayer.replaceChildren();
      nodeLayer.replaceChildren();
      applyTransform();
      return;
    }
    ensurePositions();
    const linkFragment = document.createDocumentFragment();
    const nodeFragment = document.createDocumentFragment();
    app.state.links.forEach(link => renderLinkSvg(link, linkFragment));
    app.state.nodes.forEach(node => renderNodeSvg(node, nodeFragment));
    linkLayer.replaceChildren(linkFragment);
    nodeLayer.replaceChildren(nodeFragment);
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
      "aria-pressed": String(String(app.selectedNodeId) === id),
      "aria-label": `${node.name || "Unnamed device"}, ${node.status || "unknown"}. Press Enter to inspect${app.topologyEditing ? "; arrow keys to move." : "."}`
    });
    const tooltip = svgEl("title");
    tooltip.textContent = [node.name || "Unnamed device", titleCase(nodeKind(node)), node.ip || node.hostname, node.status || "unknown"].filter(Boolean).join(" · ");
    group.append(tooltip);
    group.append(svgEl("rect", { x: -63, y: -48, width: 126, height: 100, rx: 4, class: "node-halo" }));
    group.append(svgEl("rect", { x: -63, y: -48, width: 126, height: 100, rx: 4, class: "node-body" }));
    group.append(svgEl("image", { x: -36, y: -42, width: 72, height: 56, href: deviceIconPath(nodeKind(node)), class: "node-device-image", "aria-hidden": "true" }));
    if (["offline", "degraded"].includes(node.status)) {
      group.append(svgEl("circle", { cx: 35, cy: -32, r: 5, class: "node-status-ring" }));
      group.append(svgEl("circle", { cx: 35, cy: -32, r: 3.5, class: `node-status ${node.status}` }));
    }
    const title = svgEl("text", { x: 0, y: 32, class: "node-title" });
    title.textContent = truncate(node.name || "Unnamed device", 17);
    group.append(title);
    if (!app.state.settings.compact_labels) {
      const subtitle = svgEl("text", { x: 0, y: 46, class: "node-subtitle" });
      subtitle.textContent = truncate(node.ip || node.hostname || titleCase(nodeKind(node)), 21);
      group.append(subtitle);
    }
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

  function startTopologyInteraction(event) {
    const node = event.target.closest?.(".node[data-node-id]");
    if (node) {
      startNodeDrag(event, node.dataset.nodeId);
      return;
    }
    startPan(event);
  }

  function handleTopologyClick(event) {
    const node = event.target.closest?.(".node[data-node-id]");
    if (node && !app.topologyEditing) {
      selectNode(node.dataset.nodeId);
      return;
    }
    const link = event.target.closest?.(".link-group[data-link-id]");
    if (link && app.topologyEditing) openLinkDialog(link.dataset.linkId);
  }

  function handleTopologyKeydown(event) {
    const node = event.target.closest?.(".node[data-node-id]");
    if (node) {
      handleNodeKeydown(event, node.dataset.nodeId);
      return;
    }
    const link = event.target.closest?.(".link-group[data-link-id]");
    if (link && app.topologyEditing && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      openLinkDialog(link.dataset.linkId);
    }
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
    linksForNode(id).forEach(link => {
      const old = $(`.link-group[data-link-id="${CSS.escape(String(link.id))}"]`);
      if (!old) return;
      const holder = document.createDocumentFragment();
      renderLinkSvg(link, holder);
      old.replaceWith(holder);
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
    const scale = Math.min(2.5, Math.max(.001, app.transform.scale * factor));
    app.transform.x = sx - rect.left - worldX * scale;
    app.transform.y = sy - rect.top - worldY * scale;
    app.transform.scale = scale;
    applyTransform();
  }

  function fitMap(padding = 80) {
    if (!app.state.nodes.length && !app.state.areas.length) return;
    ensurePositions();
    const rect = $("#topology").getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    app.visualPositions.forEach(position => {
      minX = Math.min(minX, position.x);
      maxX = Math.max(maxX, position.x);
      minY = Math.min(minY, position.y);
      maxY = Math.max(maxY, position.y);
    });
    minX -= 65; maxX += 65; minY -= 55; maxY += 55;
    app.state.areas.forEach(area => {
      minX = Math.min(minX, area.x); maxX = Math.max(maxX, area.x + area.width);
      minY = Math.min(minY, area.y); maxY = Math.max(maxY, area.y + area.height);
    });
    const width = Math.max(maxX - minX, 130);
    const height = Math.max(maxY - minY, 110);
    const scale = Math.min(1.3, Math.max(.001, Math.min((rect.width - padding) / width, (rect.height - padding) / height)));
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
      adjacency.get(source)?.push(target);
      if (target !== source) adjacency.get(target)?.push(source);
    });
    const preferredKinds = { router: 0, firewall: 1, switch: 2 };
    let root = nodes[0];
    for (let index = 1; index < nodes.length; index += 1) {
      const candidate = nodes[index];
      const candidateDegree = adjacency.get(String(candidate.id))?.length || 0;
      const rootDegree = adjacency.get(String(root.id))?.length || 0;
      const candidatePriority = preferredKinds[nodeKind(candidate)] ?? 9;
      const rootPriority = preferredKinds[nodeKind(root)] ?? 9;
      if (candidateDegree > rootDegree || (candidateDegree === rootDegree && candidatePriority < rootPriority)) root = candidate;
    }
    const levels = new Map([[String(root.id), 0]]);
    const queue = [String(root.id)];
    let queueIndex = 0;
    while (queueIndex < queue.length) {
      const id = queue[queueIndex];
      queueIndex += 1;
      for (const next of adjacency.get(id) || []) if (!levels.has(next)) { levels.set(next, levels.get(id) + 1); queue.push(next); }
    }
    let disconnectedLevel = 1;
    levels.forEach(level => { disconnectedLevel = Math.max(disconnectedLevel, level + 1); });
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
        body: JSON.stringify({ nodes: arrangedNodes, links: app.state.links, areas: app.state.areas, settings: app.state.settings })
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

  function updateMapSelection() {
    $$(".node.selected", $("#nodeLayer")).forEach(element => { element.classList.remove("selected"); element.setAttribute("aria-pressed", "false"); });
    if (!app.selectedNodeId) return;
    const selected = $(`.node[data-node-id="${CSS.escape(String(app.selectedNodeId))}"]`, $("#nodeLayer"));
    selected?.classList.add("selected");
    selected?.setAttribute("aria-pressed", "true");
  }

  function selectNode(id) {
    app.selectedNodeId = String(id);
    updateMapSelection();
    renderInspector();
  }

  function clearSelection() {
    app.selectedNodeId = null;
    app.selectedLinkId = null;
    updateMapSelection(); renderInspector();
  }

  function renderInspector() {
    const node = nodeById(app.selectedNodeId);
    const inspector = $("#inspector");
    const wasVisible = !inspector.hidden;
    const visible = Boolean(node);
    inspector.hidden = !visible;
    inspector.classList.toggle("has-selection", visible);
    $("#overviewGrid").classList.toggle("has-inspector", visible);
    if (wasVisible !== visible) scheduleMapFit();
    if (!node) return;
    $("#inspectorAvatar").innerHTML = iconMarkup(nodeKind(node));
    $("#inspectorName").textContent = node.name || "Unnamed device";
    $("#inspectorKind").textContent = titleCase(nodeKind(node));
    const state = $("#inspectorState"); state.textContent = node.status || "unknown"; state.className = `device-state ${node.status || "unknown"}`;
    const mikrotik = isMikrotik(node);
    const server = nodeKind(node) === "server";
    const webUrl = managementUrl(node);
    const webButton = $("#manageSelected");
    webButton.hidden = !webUrl || mikrotik || server;
    webButton.href = webUrl || "#";
    webButton.title = webUrl ? `Manage at ${webUrl}` : "";
    const sshButton = $("#sshSelected");
    const serverSshUrl = sshUrl(node);
    sshButton.hidden = !serverSshUrl;
    sshButton.href = serverSshUrl || "#";
    sshButton.title = serverSshUrl ? `Open an SSH session to ${node.ip || node.hostname}` : "";
    const winboxButton = $("#winboxSelected");
    winboxButton.hidden = !mikrotik || !winboxTarget(node);
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
    const connections = linksForNode(node.id);
    const list = $("#inspectorConnections");
    if (!connections.length) list.innerHTML = '<span class="empty-connections">No mapped connections.</span>';
    else list.innerHTML = '<p class="connection-data-note">Configured speed and duplex, not live measurements.</p>' + connections.map(link => {
      const otherId = String(endpointId(link, "source")) === String(node.id) ? endpointId(link, "target") : endpointId(link, "source");
      const other = nodeById(otherId);
      const status = linkUiStatus(link.status);
      return `<button class="connection-item" data-link-id="${escapeHtml(link.id)}"><span class="connection-line-icon ${status}"><svg viewBox="0 0 24 24"><path d="M5 12h14M16 9l3 3-3 3"/></svg></span><div><strong>${escapeHtml(other?.name || "Unknown device")}</strong><span>${escapeHtml(linkName(link) || titleCase(linkKind(link)))}</span><span class="connection-metrics">${escapeHtml(formatLinkSpeed(link.bandwidth_mbps))} · ${escapeHtml(formatLinkDuplex(link.duplex))}</span></div></button>`;
    }).join("");
    $$(".connection-item", list).forEach(button => {
      button.setAttribute("aria-disabled", String(!app.topologyEditing));
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
      return `<tr data-link-id="${escapeHtml(link.id)}"><td>${escapeHtml(source?.name || "Missing device")}</td><td>${escapeHtml(target?.name || "Missing device")}</td><td>${escapeHtml(linkName(link) || titleCase(linkKind(link)))}</td><td>${escapeHtml(formatLinkSpeed(link.bandwidth_mbps))}</td><td>${escapeHtml(formatLinkDuplex(link.duplex))}</td><td><span class="status-badge ${status}">${escapeHtml(titleCase(link.status || "unknown"))}</span></td><td><div class="row-actions"><button data-action="edit" title="Edit connection" aria-label="Edit connection"><svg viewBox="0 0 24 24"><path d="m4 20 4.2-1 10.6-10.6a2 2 0 0 0-2.8-2.8L5.4 16.2Z"/></svg></button><button class="danger-action" data-action="delete" title="Remove connection" aria-label="Remove connection"><svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M9 11v6M15 11v6M6 7l1 14h10l1-14"/></svg></button></div></td></tr>`;
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
    return { revision: app.state.revision, updated_at: app.state.updated_at, nodes: app.state.nodes, links: app.state.links, areas: app.state.areas, settings: app.state.settings };
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
      if (!Array.isArray(parsed.nodes) || !Array.isArray(parsed.links) || !parsed.settings || Array.isArray(parsed.settings) || typeof parsed.settings !== "object") throw new Error("Expected nodes, links, and settings");
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
    $("#breadcrumbCurrent").textContent = name === "overview" ? "Topology" : "Configuration";
    $("#appShell").classList.remove("nav-open"); $("#menuButton").setAttribute("aria-expanded", "false");
    if (name === "overview") {
      renderMap();
      renderInspector();
      if (app.state.nodes.length || app.state.areas.length) requestAnimationFrame(() => fitMap());
    } else {
      renderActiveConfigPanel();
    }
  }

  function switchConfigTab(name) {
    app.activeConfigTab = name;
    $$('[data-config-tab]').forEach(button => button.setAttribute("aria-selected", String(button.dataset.configTab === name)));
    $$(".config-panel").forEach(panel => { const active = panel.id === `${name}Panel`; panel.hidden = !active; panel.classList.toggle("active", active); });
    if (app.activeView === "configuration") renderActiveConfigPanel({ forceRaw: name === "data" });
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
      vendor, management_url: $("#nodeManagementUrl").value.trim(),
      tags: $("#nodeTags").value.split(",").map(item => item.trim()).filter(Boolean), notes: $("#nodeNotes").value.trim(), config
    };
    if (!id) {
      const position = nextNodePosition(); payload.x = position.x; payload.y = position.y;
    }
    const submit = $("#nodeSubmit"); setBusy(submit, true, id ? "Saving…" : "Adding…");
    try {
      const next = await api(id ? `/api/nodes/${encodeURIComponent(id)}` : "/api/nodes", { method: id ? "PATCH" : "POST", body: JSON.stringify(payload), expectedRevision: Number(form.dataset.baseRevision) });
      $("#nodeDialog").close(); applyState(next, { fit: !id });
      toast(id ? "Device updated" : "Device added", `${payload.name} was saved to the workspace.`);
      if (!id) {
        const created = [...next.nodes].reverse().find(node => node.name === payload.name) || next.nodes.at(-1);
        if (created) selectNode(created.id);
      }
    } catch (error) { reportError(id ? "Could not update device" : "Could not add device", error); }
    finally { setBusy(submit, false); }
  }

  function nextNodePosition() {
    return nodePositionForIndex(app.state.nodes.length);
  }

  function nodePositionForIndex(count) {
    const angle = count * 2.4; const radius = 90 + 28 * Math.sqrt(count);
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
    $("#linkSpeedPreset").innerHTML = '<option value="">Not specified</option>' + SPEED_PRESETS.map(speed => `<option value="${speed}">${escapeHtml(formatLinkSpeed(speed))}</option>`).join("") + '<option value="custom">Custom speed…</option>';
    const speed = link?.bandwidth_mbps;
    $("#linkSpeedPreset").value = speed == null ? "" : SPEED_PRESETS.includes(Number(speed)) ? String(Number(speed)) : "custom";
    $("#linkSpeed").value = link?.bandwidth_mbps ?? "";
    $("#linkSpeedPreset").onchange = setLinkSpeedMode;
    setLinkSpeedMode();
    $("#linkDuplex").value = Object.hasOwn(DUPLEX_LABELS, link?.duplex) ? link.duplex : "unknown";
    $("#speedPresetTableBody").innerHTML = SPEED_PRESETS.map(speed => `<tr><td>${escapeHtml(formatLinkSpeed(speed))}</td><td>${speed.toLocaleString()} Mbps</td></tr>`).join("");
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
    const preset = $("#linkSpeedPreset").value;
    const speed = preset === "custom" ? $("#linkSpeed").value : preset;
    const payload = { source, target, name: $("#linkLabel").value.trim(), kind: $("#linkType").value, status: $("#linkStatus").value, directed: $("#linkDirected").checked, bandwidth_mbps: speed === "" ? null : Number(speed), duplex: $("#linkDuplex").value, notes: $("#linkNotes").value.trim(), config: linkById(id)?.config || {} };
    const submit = $("#linkSubmit"); setBusy(submit, true, "Saving…");
    try {
      const next = await api(id ? `/api/links/${encodeURIComponent(id)}` : "/api/links", { method: id ? "PATCH" : "POST", body: JSON.stringify(payload), expectedRevision: Number(event.currentTarget.dataset.baseRevision) });
      $("#linkDialog").close(); applyState(next); toast(id ? "Connection updated" : "Devices connected", payload.name || `${nodeById(source)?.name} ↔ ${nodeById(target)?.name}`);
    } catch (error) { reportError("Could not save connection", error); }
    finally { setBusy(submit, false); }
  }

  function askConfirmation({ title, message, label = "Remove", busyLabel = "Working…", action }) {
    $("#confirmTitle").textContent = title; $("#confirmMessage").textContent = message; $("#confirmButton").textContent = label;
    $("#confirmButton").dataset.busyLabel = busyLabel;
    app.confirmAction = action; $("#confirmDialog").returnValue = ""; $("#confirmDialog").showModal();
  }

  function confirmDeleteNode(id) {
    const node = nodeById(id); if (!node) return;
    const links = linksForNode(id).length;
    askConfirmation({ title: `Remove ${node.name}?`, message: links ? `This also removes ${links} connected ${links === 1 ? "link" : "links"}. This action cannot be undone.` : "This device will be permanently removed from the map.", busyLabel: "Removing…", action: async () => {
      const next = await api(`/api/nodes/${encodeURIComponent(id)}`, { method: "DELETE" }); applyState(next); clearSelection(); toast("Device removed", `${node.name} was removed from the workspace.`);
    }});
  }

  function confirmDeleteLink(id) {
    const link = linkById(id); if (!link) return;
    askConfirmation({ title: "Remove connection?", message: "The devices will remain on the map, but this connection will be removed.", busyLabel: "Removing…", action: async () => {
      const next = await api(`/api/links/${encodeURIComponent(id)}`, { method: "DELETE" }); applyState(next); toast("Connection removed", "The topology has been updated.");
    }});
  }

  async function runConfirmedAction() {
    const action = app.confirmAction; app.confirmAction = null; if (!action) return;
    const button = $("#confirmButton"); setBusy(button, true, button.dataset.busyLabel || "Working…");
    try { await action(); } catch (error) { reportError("Action failed", error); }
    finally { setBusy(button, false); }
  }

  async function submitSettings(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = { name: $("#settingName").value.trim() || "Main network", description: $("#settingDescription").value.trim(), subnet: $("#settingSubnet").value.trim(), refresh_interval: Number($("#settingRefresh").value), show_link_labels: $("#settingLinkLabels").checked, compact_labels: $("#settingCompact").checked };
    const button = $('button[type="submit"]', form); setBusy(button, true, "Saving…");
    try { const next = await api("/api/settings", { method: "PATCH", body: JSON.stringify(payload), expectedRevision: Number(form.dataset.baseRevision) }); applyState(next); form.dataset.baseRevision = String(app.state.revision); $("#settingsHint").textContent = "Saved just now"; toast("Workspace updated", "Your preferences were saved."); }
    catch (error) { reportError("Could not save settings", error); }
    finally { setBusy(button, false); }
  }

  async function replaceState(payload, successMessage, expectedRevision = app.state.revision) {
    const next = await api("/api/state", { method: "PUT", body: JSON.stringify(payload), expectedRevision });
    app.rawDirty = false; applyState(next, { fit: true }); toast("Workspace imported", successMessage);
  }

  async function saveRawJson() {
    const parsed = validateRawJson(); if (!parsed) { toast("Invalid JSON", "Fix the highlighted JSON error before saving.", "error"); return; }
    askConfirmation({ title: "Replace this workspace?", message: "All current devices, connections, areas, and settings will be replaced by the JSON editor contents.", label: "Replace workspace", busyLabel: "Replacing…", action: () => replaceState(parsed, "The JSON configuration is now active.", app.rawBaseRevision) });
  }

  async function importState(payload, successMessage, expectedRevision = app.state.revision) {
    const next = await api("/api/import", { method: "POST", body: JSON.stringify(payload), expectedRevision });
    app.rawDirty = false; applyState(next, { fit: true }); toast("Workspace imported", successMessage);
  }

  async function importFile(file) {
    if (!file) return;
    try {
      const parsed = JSON.parse(await file.text());
      if (!Array.isArray(parsed.nodes) || !Array.isArray(parsed.links) || !parsed.settings || Array.isArray(parsed.settings) || typeof parsed.settings !== "object") throw new Error("This is not a valid NetworkMap workspace file.");
      const baseRevision = app.state.revision;
      askConfirmation({ title: "Import this workspace?", message: `Import ${parsed.nodes.length} devices and ${parsed.links.length} connections, replacing the current map?`, label: "Import workspace", busyLabel: "Importing…", action: () => importState(parsed, `${parsed.nodes.length} devices and ${parsed.links.length} connections were loaded.`, baseRevision) });
    } catch (error) { reportError("Could not import file", error); }
    finally { $("#importFile").value = ""; }
  }

  async function exportState() {
    $("#topologyDataMenu").removeAttribute("open");
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
    const baseNodeCount = app.state.nodes.length;
    let expectedRevision = app.state.revision;
    let latest = null; let added = 0;
    try {
      for (const discovered of selected) {
        const position = nodePositionForIndex(baseNodeCount + added);
        const allowed = { name: discovered.name || discovered.hostname || discovered.ip || "Discovered device", kind: discovered.kind || "other", ip: discovered.ip || "", mac: discovered.mac || "", hostname: discovered.hostname || "", vendor: discovered.vendor || "", status: discovered.status || "online", x: position.x, y: position.y, notes: discovered.notes || "Discovered by network scan", tags: Array.isArray(discovered.tags) ? discovered.tags : ["discovered"], config: discovered.config && typeof discovered.config === "object" ? discovered.config : {} };
        latest = await api("/api/nodes", { method: "POST", body: JSON.stringify(allowed), expectedRevision });
        expectedRevision = latest.revision;
        added += 1;
        setBusy(submit, true, `Adding ${added}/${selected.length}…`);
      }
      $("#discoveryDialog").close(); if (latest) applyState(latest, { fit: true }); toast("Devices imported", `${added} ${added === 1 ? "device was" : "devices were"} added to the map.`);
    } catch (error) { if (latest) applyState(latest, { fit: true }); if (!error.handled) toast("Import stopped", `${added} added. ${error.message}`, "error"); }
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
    $("#syncNowButton").addEventListener("click", () => requestSyncAction("sync-now"));
    $("#useHostedButton").addEventListener("click", () => requestSyncAction("use-hosted"));
    $("#useLocalButton").addEventListener("click", () => requestSyncAction("use-local"));
    $("#inventorySearch").addEventListener("input", renderDeviceTable);
    $("#statusFilter").addEventListener("change", renderDeviceTable);
    $("#deviceTableBody").addEventListener("click", handleDeviceTableClick);
    $("#linkTableBody").addEventListener("click", handleLinkTableClick);
    $("#mapSearch").addEventListener("input", applyMapSearch);
    $("#closeInspector").addEventListener("click", clearSelection);
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
    $("#sidebarCollapseButton").addEventListener("click", () => setSidebarCollapsed(!$("#appShell").classList.contains("sidebar-collapsed")));
    $("#topbarHideButton").addEventListener("click", () => setTopbarHidden(true, { focusControl: true }));
    $("#topbarShowButton").addEventListener("click", () => setTopbarHidden(false, { focusControl: true }));
    $("#openConfigurationButton").addEventListener("click", () => { switchView("configuration"); switchConfigTab("inventory"); });
    const topology = $("#topology");
    topology.addEventListener("pointerdown", startTopologyInteraction); topology.addEventListener("pointermove", movePointer); topology.addEventListener("pointerup", endPointer); topology.addEventListener("pointercancel", endPointer);
    topology.addEventListener("click", handleTopologyClick);
    topology.addEventListener("keydown", handleTopologyKeydown);
    topology.addEventListener("wheel", event => { event.preventDefault(); zoomAt(event.deltaY < 0 ? 1.1 : 1 / 1.1, event.clientX, event.clientY); }, { passive: false });
    $("#menuButton").addEventListener("click", () => { const open = $("#appShell").classList.toggle("nav-open"); $("#menuButton").setAttribute("aria-expanded", String(open)); });
    $("#sidebarScrim").addEventListener("click", () => { $("#appShell").classList.remove("nav-open"); $("#menuButton").setAttribute("aria-expanded", "false"); });
    $("#themeButton").addEventListener("click", toggleTheme);
    $("#confirmDialog").addEventListener("close", () => { if ($("#confirmDialog").returnValue === "confirm") runConfirmedAction(); else app.confirmAction = null; });
    $("#rawJson").addEventListener("input", () => { app.rawDirty = true; validateRawJson(); });
    $("#rawJson").addEventListener("keydown", event => { if (event.key === "Tab") { event.preventDefault(); const field = event.currentTarget; const start = field.selectionStart; field.setRangeText("  ", start, field.selectionEnd, "end"); field.dispatchEvent(new Event("input")); } });
    $("#saveJsonButton").addEventListener("click", saveRawJson);
    $("#copyJsonButton").addEventListener("click", async () => { try { await navigator.clipboard.writeText($("#rawJson").value); toast("Copied to clipboard", "Workspace JSON is ready to paste."); } catch (_) { $("#rawJson").select(); document.execCommand("copy"); toast("Copied to clipboard"); } });
    $$('[data-action="export-topology"]').forEach(button => button.addEventListener("click", exportState));
    $$('[data-action="import-topology"]').forEach(button => button.addEventListener("click", () => { $("#topologyDataMenu").removeAttribute("open"); $("#importFile").click(); }));
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
    document.addEventListener("click", event => { const menu = $("#topologyDataMenu"); if (menu.open && !menu.contains(event.target)) menu.removeAttribute("open"); });
    document.addEventListener("keydown", event => { if (event.key !== "Escape") return; $("#topologyDataMenu").removeAttribute("open"); if ($("#appShell").classList.contains("nav-open")) { $("#appShell").classList.remove("nav-open"); $("#menuButton").setAttribute("aria-expanded", "false"); $("#menuButton").focus(); } });
    window.addEventListener("resize", () => { if (window.innerWidth > 930) { $("#appShell").classList.remove("nav-open"); $("#menuButton").setAttribute("aria-expanded", "false"); } scheduleMapFit(180); });
    window.addEventListener("beforeunload", () => {
      closeEvents();
      clearInterval(app.syncStatusTimer);
      clearInterval(app.refreshTimer);
      clearTimeout(app.eventRefreshTimer);
      clearTimeout(app.layoutFitTimer);
      clearTimeout(handleNodeKeydown.timer);
    });
  }

  async function loadInitialState() {
    if (app.token) {
      try { await api("/api/session", { method: "POST" }); forgetBootstrapToken(); }
      catch (error) { if (error.status !== 401) reportError("Could not establish session", error); return; }
    }
    fetchState({ fit: true }).then(() => fetchSyncStatus()).catch(() => {});
  }

  function init() {
    readInitialToken(); restoreTheme(); restoreLayout(); attachEvents(); renderAll(); renderSyncStatus();
    setTopologyEditing(false);
    loadInitialState();
    app.syncStatusTimer = setInterval(() => fetchSyncStatus().catch(() => {}), 4000);
    if ("serviceWorker" in navigator && location.protocol !== "file:") navigator.serviceWorker.register("/static/sw.js", { scope: "/" }).catch(() => {});
  }

  const areaEditor = window.createNetworkMapAreas({
    getState: () => app.state, isEditing: () => app.topologyEditing,
    getTransform: () => app.transform, worldPoint, svgEl, escapeHtml, api,
    applyState, applyTransform, askConfirmation, setBusy, toast, reportError,
    showMap: () => switchView("overview")
  });
  init();
})();
