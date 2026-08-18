async function getJSON(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function manualOpenOptions() {
  const requestId = (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return { method: "POST", headers: { "X-Idempotency-Key": requestId } };
}

function fmtConfidence(v) {
  if (v === null || v === undefined) return "--";
  return `${Math.round(v * 100)}%`;
}

function eventTitle(ev) {
  if (ev.event_type === "manual_open") return "Abertura manual";
  if (ev.event_type === "anpr_authorized") return "ANPR autorizado";
  if (ev.event_type === "anpr_denied") return "ANPR negado";
  if (ev.event_type === "plate_added") return "Matricula adicionada";
  if (ev.event_type === "plate_removed") return "Matricula removida";
  return ev.event_type;
}

function eventClass(ev) {
  if (ev.event_type === "manual_open") return "warn";
  if (ev.event_type === "plate_added") return "ok";
  if (ev.event_type === "plate_removed") return "warn";
  if (ev.authorized === 1) return "ok";
  if (ev.authorized === 0) return "bad";
  return "";
}
function renderEvents(events) {
  const el = document.getElementById("events");
  el.innerHTML = "";
  for (const ev of events) {
    const row = document.createElement("div");
    row.className = "event";

    const img = ev.snapshot_path
      ? `<button type="button" class="event-snapshot" aria-label="Ampliar imagem do evento">
           <img src="/${ev.snapshot_path}" alt="Imagem do evento">
         </button>`
      : `<div style="width:96px;height:72px;background:#000;border-radius:8px"></div>`;

    row.innerHTML = `
      ${img}
      <div>
        <div class="event-title ${eventClass(ev)}">${eventTitle(ev)}</div>
        <div class="muted">${ev.ts}</div>
        ${ev.plate ? `<div>Matrícula: <strong>${ev.plate}</strong></div>` : ""}
        ${ev.confidence !== null ? `<div>Confiança: ${fmtConfidence(ev.confidence)}</div>` : ""}
        ${ev.client_ip ? `<div>Origem: ${ev.client_name ? `${ev.client_name} · ` : ""}${ev.client_ip}</div>` : ""}
        ${ev.note ? `<div class="muted">${ev.note}</div>` : ""}
      </div>
    `;
    row.querySelector(".event-snapshot")?.addEventListener("click", () => {
      openEventImage(`/${ev.snapshot_path}`);
    });
    el.appendChild(row);
  }
}

function openEventImage(src) {
  const viewer = document.getElementById("event-image-viewer");
  const image = document.getElementById("event-image-large");
  if (!viewer || !image) return;
  image.src = src;
  viewer.hidden = false;
  document.body.classList.add("modal-open");
  document.getElementById("event-image-close")?.focus();
}

function closeEventImage() {
  const viewer = document.getElementById("event-image-viewer");
  const image = document.getElementById("event-image-large");
  if (!viewer || viewer.hidden) return;
  viewer.hidden = true;
  image.removeAttribute("src");
  document.body.classList.remove("modal-open");
}

document.getElementById("event-image-close")?.addEventListener("click", closeEventImage);
document.getElementById("event-image-viewer")?.addEventListener("click", (event) => {
  if (event.target.id === "event-image-viewer") closeEventImage();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeEventImage();
});

async function refreshStatus() {
  const data = await getJSON("/api/status");
  setupCameras(data.cameras || []);
  const latest = data.latest || {};
  document.getElementById("latest-plate").textContent = latest.plate || "--";
  document.getElementById("latest-confidence").textContent = fmtConfidence(latest.confidence);
  document.getElementById("latest-state").textContent =
    latest.authorized === true ? "Autorizada" :
    latest.authorized === false ? "Negada" : "--";
}

let selectedCamera = "1";
let availableCameras = [];

function updateCameraLabel() {
  const camera = availableCameras.find((item) => item.id === selectedCamera);
  document.getElementById("camera-ip").textContent = camera
    ? `${camera.name}: ${camera.ip || "por configurar"}`
    : "Camara: --";
}

function selectCamera(button) {
  const configured = button.dataset.configured !== "false";
  selectedCamera = button.dataset.camera;
  document.querySelectorAll(".camera-thumb").forEach((item) => {
    const active = item === button;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", String(active));
  });

  const feed = document.getElementById("live-feed");
  const unavailable = document.getElementById("live-unavailable");
  feed.hidden = !configured;
  unavailable.hidden = configured;
  if (configured && feed.dataset.camera !== selectedCamera) {
    feed.src = `${button.dataset.feed}?view=${Date.now()}`;
    feed.dataset.camera = selectedCamera;
    feed.alt = `Imagem em direto da Camara ${selectedCamera}`;
  }
  updateCameraLabel();
}

function setupCameras(cameras) {
  availableCameras = cameras;
  for (const camera of cameras) {
    const button = document.querySelector(`.camera-thumb[data-camera="${camera.id}"]`);
    if (!button) continue;
    button.dataset.configured = String(camera.configured);
    const small = button.querySelector("small");
    if (small) small.textContent = camera.configured ? (camera.ip || "Disponivel") : "Por configurar";

    if (camera.id === "2" && camera.configured && !button.querySelector("img")) {
      const preview = document.createElement("img");
      preview.src = button.dataset.feed;
      preview.alt = `Miniatura da ${camera.name}`;
      button.querySelector(".thumb-placeholder")?.replaceWith(preview);
    }
  }
  updateCameraLabel();
}

document.querySelectorAll(".camera-thumb").forEach((button) => {
  button.addEventListener("click", () => selectCamera(button));
});

async function refreshEvents() {
  // Verifica mais eventos do que os quatro apresentados para nao perder alertas em rajada.
  const events = await getJSON("/api/events?limit=20");
  renderEvents(events.slice(0, 4));
  checkForAlerts(events);
}

// ============ Alerta persistente de matricula nao autorizada ============
let soundEnabled = false;
let audioUnlocked = false;
let lastSeenEventId = null;
let audioCtx = null;
let alarmTimer = null;
let titleTimer = null;
let originalTitle = document.title;
let activeDeniedAlert = null;
const deniedAlertQueue = [];
const queuedDeniedIds = new Set();

function initAudio() {
  if (audioCtx) return;
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  } catch (e) { console.warn("AudioContext indisponivel", e); }
}

async function enableAlertSound() {
  if ("Notification" in window && Notification.permission === "default") {
    try { await Notification.requestPermission(); } catch (e) {}
  }
  initAudio();
  if (audioCtx && audioCtx.state === "suspended") {
    try { await audioCtx.resume(); } catch (e) {}
  }
  audioUnlocked = !!audioCtx && audioCtx.state === "running";
  soundEnabled = audioUnlocked;
  updateAlertsButton();
  updateNotificationStatus();
  if (soundEnabled) playDeniedBeep(true);
}

function playDeniedBeep(testOnly = false) {
  if (!soundEnabled || !audioUnlocked || !audioCtx) return;
  const now = audioCtx.currentTime;
  const count = testOnly ? 1 : 3;
  for (let i = 0; i < count; i++) {
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = "sawtooth";
    osc.frequency.value = i % 2 ? 620 : 480;
    gain.gain.setValueAtTime(0.0001, now + i * 0.28);
    gain.gain.exponentialRampToValueAtTime(0.38, now + i * 0.28 + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + i * 0.28 + 0.22);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start(now + i * 0.28);
    osc.stop(now + i * 0.28 + 0.24);
  }
}

function showNotification(title, body) {
  if (!("Notification" in window)) return;
  if (Notification.permission !== "granted") return;
  try {
    const notification = new Notification(title, {
      body,
      tag: "anpr-denied",
      renotify: true,
      requireInteraction: true,
    });
    notification.onclick = () => {
      window.focus();
      notification.close();
    };
  } catch (e) { console.warn("Notification falhou", e); }
}

function checkForAlerts(events) {
  if (!events || events.length === 0) return;
  const newestId = Math.max(...events.map((event) => Number(event.id) || 0));
  if (lastSeenEventId === null) {
    lastSeenEventId = newestId;
    return;
  }
  const novos = events
    .filter((event) => Number(event.id) > lastSeenEventId && event.event_type === "anpr_denied")
    .sort((a, b) => Number(a.id) - Number(b.id));
  lastSeenEventId = Math.max(lastSeenEventId, newestId);
  for (const event of novos) {
    enqueueDeniedAlert(event);
  }
  showNextDeniedAlert();
}

function enqueueDeniedAlert(event) {
  const eventId = Number(event.id) || 0;
  if (eventId) lastSeenEventId = Math.max(lastSeenEventId || 0, eventId);
  if (queuedDeniedIds.has(event.id)) return;
  queuedDeniedIds.add(event.id);
  deniedAlertQueue.push(event);
  showNextDeniedAlert();
}

function connectAlertStream() {
  if (!("EventSource" in window)) return;
  const stream = new EventSource("/api/alerts/stream");
  stream.addEventListener("denied", (message) => {
    try {
      enqueueDeniedAlert(JSON.parse(message.data));
    } catch (error) {
      console.error("Alerta SSE invalido", error);
    }
  });
  stream.addEventListener("error", () => {
    // EventSource volta a ligar automaticamente; o polling continua como redundancia.
    console.warn("Canal de alertas temporariamente desligado; a tentar novamente.");
  });
  window.addEventListener("beforeunload", () => stream.close(), { once: true });
}

function updateAlertsButton() {
  const btn = document.getElementById("enable-alerts-btn");
  if (!btn) return;
  const notificationsEnabled = "Notification" in window && Notification.permission === "granted";
  btn.textContent = soundEnabled && audioUnlocked && notificationsEnabled
    ? "🔔 Notificações e som ativos"
    : "🔔 Ativar notificações e som";
  btn.classList.toggle("sound-ready", soundEnabled && audioUnlocked);
}

function updateNotificationStatus() {
  const status = document.getElementById("notification-status");
  if (!status) return;
  if (!("Notification" in window)) {
    status.textContent = "Este browser não suporta notificações do sistema.";
  } else if (!window.isSecureContext) {
    status.textContent = "As notificações exigem HTTPS (ou acesso por localhost).";
  } else if (Notification.permission === "granted") {
    status.textContent = "Notificações do sistema autorizadas.";
  } else if (Notification.permission === "denied") {
    status.textContent = "Notificações bloqueadas. Autorize-as nas definições do browser.";
  } else {
    status.textContent = "Clique para autorizar os alertas mesmo com a janela minimizada.";
  }
}

async function toggleAlerts() {
  if (!soundEnabled) {
    await enableAlertSound();
    return;
  }
  soundEnabled = false;
  stopDeniedAlarm();
  updateAlertsButton();
  updateNotificationStatus();
}

function startDeniedAlarm() {
  stopDeniedAlarm();
  playDeniedBeep();
  if (soundEnabled) alarmTimer = setInterval(playDeniedBeep, 2200);
  let visible = true;
  titleTimer = setInterval(() => {
    visible = !visible;
    document.title = visible ? "⚠ MATRICULA NAO AUTORIZADA" : originalTitle;
  }, 700);
}

function stopDeniedAlarm() {
  if (alarmTimer) clearInterval(alarmTimer);
  if (titleTimer) clearInterval(titleTimer);
  alarmTimer = null;
  titleTimer = null;
  document.title = originalTitle;
}

function showNextDeniedAlert() {
  if (activeDeniedAlert || deniedAlertQueue.length === 0) return;
  activeDeniedAlert = deniedAlertQueue.shift();
  const viewer = document.getElementById("denied-alert");
  document.getElementById("denied-alert-plate").textContent = activeDeniedAlert.plate || "DESCONHECIDA";
  document.getElementById("denied-alert-time").textContent = activeDeniedAlert.ts || "";
  const image = document.getElementById("denied-alert-image");
  if (activeDeniedAlert.snapshot_path) {
    image.src = `/${activeDeniedAlert.snapshot_path}`;
    image.hidden = false;
  } else {
    image.hidden = true;
    image.removeAttribute("src");
  }
  document.getElementById("denied-alert-sound-state").hidden = soundEnabled && audioUnlocked;
  viewer.hidden = false;
  startDeniedAlarm();
  showNotification("Matricula nao autorizada junto ao portao", activeDeniedAlert.plate || "Matricula desconhecida");
}

function dismissDeniedAlert() {
  if (!activeDeniedAlert) return;
  queuedDeniedIds.delete(activeDeniedAlert.id);
  activeDeniedAlert = null;
  document.getElementById("denied-alert").hidden = true;
  stopDeniedAlarm();
  showNextDeniedAlert();
}

async function openGateFromAlert() {
  const button = document.getElementById("denied-alert-open");
  button.disabled = true;
  button.textContent = "A abrir...";
  try {
    const data = await getJSON("/api/open_gate", manualOpenOptions());
    document.getElementById("manual-result").textContent = `Portao aberto. Origem: ${data.client_name ? `${data.client_name} · ` : ""}${data.client_ip}`;
    dismissDeniedAlert();
  } catch (error) {
    document.getElementById("denied-alert-error").textContent = `Nao foi possivel abrir: ${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = "Abrir portao";
  }
}

async function openGate() {
  const btn = document.getElementById("open-gate-btn");
  const result = document.getElementById("manual-result");
  btn.disabled = true;
  btn.textContent = "A abrir...";
  try {
    const data = await getJSON("/api/open_gate", manualOpenOptions());
    result.textContent = `Portão aberto. Origem: ${data.client_name ? `${data.client_name} · ` : ""}${data.client_ip}`;
    await refreshEvents();
  } catch (e) {
    result.textContent = `Erro: ${e.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Abrir portão";
  }
}

async function reloadPlates() {
  try {
    const data = await getJSON("/api/reload_plates", { method: "POST" });
    alert(`Lista recarregada: ${data.count} matrículas`);
  } catch (e) {
    alert(`Erro: ${e.message}`);
  }
}

document.getElementById("open-gate-btn")?.addEventListener("click", openGate);
document.getElementById("reload-plates-btn")?.addEventListener("click", reloadPlates);
document.getElementById("enable-alerts-btn")?.addEventListener("click", toggleAlerts);
document.getElementById("denied-alert-open")?.addEventListener("click", openGateFromAlert);
document.getElementById("denied-alert-dismiss")?.addEventListener("click", dismissDeniedAlert);
document.getElementById("denied-alert-enable-sound")?.addEventListener("click", async () => {
  await enableAlertSound();
  document.getElementById("denied-alert-sound-state").hidden = soundEnabled && audioUnlocked;
  if (activeDeniedAlert) startDeniedAlarm();
});
updateAlertsButton();
updateNotificationStatus();
connectAlertStream();

function openBusModal(){
  const modal = document.getElementById("bus-modal");
  if(!modal) return;
  document.getElementById("bus-plate-input").value = "";
  document.getElementById("bus-label-input").value = "";
  document.getElementById("bus-msg").textContent = "";
  modal.hidden = false;
  setTimeout(()=>document.getElementById("bus-plate-input").focus(), 50);
}

function closeBusModal(){
  const modal = document.getElementById("bus-modal");
  if(modal) modal.hidden = true;
}

document.getElementById("external-bus-btn")?.addEventListener("click", openBusModal);
document.getElementById("bus-cancel")?.addEventListener("click", closeBusModal);
document.getElementById("bus-modal")?.addEventListener("click", (ev)=>{
  if(ev.target.id === "bus-modal") closeBusModal();
});
document.addEventListener("keydown", (ev)=>{
  if(ev.key === "Escape") closeBusModal();
});

document.getElementById("bus-form")?.addEventListener("submit", async (ev)=>{
  ev.preventDefault();
  const plate = document.getElementById("bus-plate-input").value;
  const label = document.getElementById("bus-label-input").value;
  const msg = document.getElementById("bus-msg");
  msg.textContent = "A adicionar...";
  try{
    const r = await fetch("/api/plates", {
      method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({
        plates: plate,
        label: label || "Autocarro externo",
        is_external_bus: true,
      }),
    });
    const data = await r.json();
    if(!r.ok) throw new Error(data.error || "erro");
    if(data.added && data.added.length){
      msg.textContent = `Autorizada: ${data.added.join(", ")}`;
      setTimeout(closeBusModal, 800);
      await refreshEvents();
    } else if(data.skipped && data.skipped.length){
      msg.textContent = `Já existia: ${data.skipped.join(", ")}`;
    } else {
      msg.textContent = "Nenhuma matrícula válida.";
    }
  }catch(e){
    msg.textContent = "Erro: " + e.message;
  }
});

async function tick() {
  // O alerta de eventos deve continuar mesmo que a consulta de estado da camera falhe.
  const results = await Promise.allSettled([refreshStatus(), refreshEvents()]);
  for (const result of results) {
    if (result.status === "rejected") console.error(result.reason);
  }
}

async function loadPlates() {
  try {
    const plates = await getJSON("/api/plates");
    document.getElementById("plates-count").textContent = `${plates.length} ativas`;
    const el = document.getElementById("plates-list");
    el.innerHTML = "";
    if (plates.length === 0) {
      el.innerHTML = `<div class="muted">Sem matrículas autorizadas.</div>`;
      return;
    }
    for (const p of plates) {
      const row = document.createElement("div");
      row.className = "plate-row";
      row.innerHTML = `
        <div class="info">
          <div class="plate">${p.plate}</div>
          <div class="muted">
            ${p.label ? p.label + " · " : ""}
            adicionada ${p.added_at}${p.added_by_ip ? " · IP " + p.added_by_ip : ""}
          </div>
        </div>
        <button data-id="${p.id}" data-plate="${p.plate}">Remover</button>
      `;
      row.querySelector("button").addEventListener("click", async (ev) => {
        const id = ev.target.dataset.id;
        const plate = ev.target.dataset.plate;
        if (!confirm(`Remover ${plate}?`)) return;
        try {
          await getJSON(`/api/plates/${id}`, { method: "DELETE" });
          await loadPlates();
          await refreshEvents();
        } catch (e) { alert("Erro: " + e.message); }
      });
      el.appendChild(row);
    }
  } catch (e) {
    console.error(e);
  }
}

document.getElementById("plate-form")?.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const plate = document.getElementById("plate-input").value;
  const label = document.getElementById("label-input").value;
  const msg = document.getElementById("plate-msg");
  msg.textContent = "A adicionar...";
  try {
    const r = await fetch("/api/plates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plate, label }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || "erro");
    msg.textContent = `Adicionada: ${data.plate}`;
    document.getElementById("plate-input").value = "";
    document.getElementById("label-input").value = "";
    await loadPlates();
    await refreshEvents();
  } catch (e) {
    msg.textContent = "Erro: " + e.message;
  }
});

tick();
setInterval(tick, 3000);
