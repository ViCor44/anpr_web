async function getJSON(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
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
      ? `<img src="/${ev.snapshot_path}" alt="snapshot">`
      : `<div style="width:96px;height:72px;background:#000;border-radius:8px"></div>`;

    row.innerHTML = `
      ${img}
      <div>
        <div class="event-title ${eventClass(ev)}">${eventTitle(ev)}</div>
        <div class="muted">${ev.ts}</div>
        ${ev.plate ? `<div>Matrícula: <strong>${ev.plate}</strong></div>` : ""}
        ${ev.confidence !== null ? `<div>Confiança: ${fmtConfidence(ev.confidence)}</div>` : ""}
        ${ev.client_ip ? `<div>IP origem: ${ev.client_ip}</div>` : ""}
        ${ev.note ? `<div class="muted">${ev.note}</div>` : ""}
      </div>
    `;
    el.appendChild(row);
  }
}

async function refreshStatus() {
  const data = await getJSON("/api/status");
  document.getElementById("camera-ip").textContent = `Câmara: ${data.camera_ip}`;
  const latest = data.latest || {};
  document.getElementById("latest-plate").textContent = latest.plate || "--";
  document.getElementById("latest-confidence").textContent = fmtConfidence(latest.confidence);
  document.getElementById("latest-state").textContent =
    latest.authorized === true ? "Autorizada" :
    latest.authorized === false ? "Negada" : "--";
}

async function refreshEvents() {
  const events = await getJSON("/api/events?limit=4");
  renderEvents(events);
}

async function openGate() {
  const btn = document.getElementById("open-gate-btn");
  const result = document.getElementById("manual-result");
  btn.disabled = true;
  btn.textContent = "A abrir...";
  try {
    const data = await getJSON("/api/open_gate", { method: "POST" });
    result.textContent = `Portão aberto. IP origem: ${data.client_ip}`;
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
  try {
    await refreshStatus();
    await refreshEvents();    
  } catch (e) {
    console.error(e);
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