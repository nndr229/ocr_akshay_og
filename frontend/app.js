/* AP Invoice Console — frontend */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const fmtMoney = (amount, currency = "INR") => {
  if (amount === null || amount === undefined || amount === "") return "—";
  currency = currency || "INR";
  try {
    return new Intl.NumberFormat("en-IN", { style: "currency", currency }).format(amount);
  } catch {
    return `${currency} ${Number(amount).toFixed(2)}`;
  }
};
const fmtDate = (s) => (s ? s.slice(0, 10) : "—");
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), ms);
}

/* ---------------- navigation ---------------- */
$$(".nav-btn").forEach((btn) =>
  btn.addEventListener("click", () => {
    $$(".nav-btn").forEach((b) => b.classList.toggle("active", b === btn));
    $$(".view").forEach((v) => v.classList.remove("active"));
    $(`#view-${btn.dataset.view}`).classList.add("active");
    if (btn.dataset.view === "dashboard") loadDashboard();
    if (btn.dataset.view === "invoices") loadInvoices();
  })
);

/* ---------------- uploads (shared by dashboard button + chat plus) ---------------- */
const fileInput = $("#file-input");
let uploadOrigin = "dashboard";

$("#dash-upload-btn").addEventListener("click", () => {
  uploadOrigin = "dashboard";
  fileInput.click();
});
$("#chat-plus").addEventListener("click", () => {
  uploadOrigin = "chat";
  fileInput.click();
});

fileInput.addEventListener("change", async () => {
  const files = [...fileInput.files];
  fileInput.value = "";
  if (!files.length) return;

  if (uploadOrigin === "chat") {
    await extractForChat(files);
  } else {
    await extractWithOverlay(files);
  }
});

async function postFiles(files) {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  const res = await fetch("/api/invoices/upload", { method: "POST", body: form });
  if (!res.ok) throw new Error(`Server error (${res.status})`);
  return (await res.json()).results;
}

/* dashboard flow: full-screen progress overlay, then extracted results */
let extractTimer;

async function extractWithOverlay(files) {
  const overlay = $("#extract-overlay");
  const progress = $("#extract-progress");
  const results = $("#extract-results");
  overlay.classList.remove("hidden");
  progress.classList.remove("hidden");
  results.classList.add("hidden");
  $("#extract-title").textContent =
    files.length === 1 ? `Reading ${files[0].name}…` : `Reading ${files.length} invoices…`;

  const started = Date.now();
  const update = () => {
    const secs = Math.round((Date.now() - started) / 1000);
    $("#extract-sub").textContent = `Extracting vendor, line items, taxes and totals… ${secs}s`;
  };
  update();
  extractTimer = setInterval(update, 1000);

  let rows;
  try {
    rows = await postFiles(files);
  } catch (e) {
    rows = files.map((f) => ({ ok: false, filename: f.name, error: e.message }));
  } finally {
    clearInterval(extractTimer);
  }

  $("#extract-result-rows").innerHTML = rows
    .map((r) => {
      if (r.ok) {
        const inv = r.invoice;
        return `
          <div class="extract-row">
            <span class="icon">✅</span>
            <div class="info">
              <div><strong>${esc(inv.vendor_name || "Unknown vendor")}</strong> — ${esc(inv.invoice_number || "no number")} · ${fmtMoney(inv.total_amount, inv.currency)}${inv.confidence === "low" ? ' <span class="conf-low">⚠ review</span>' : ""}</div>
              <div class="file">${esc(r.filename)}</div>
            </div>
            <button class="btn primary" onclick="viewExtracted(${inv.id})">View invoice</button>
          </div>`;
      }
      return `
        <div class="extract-row fail">
          <span class="icon">❌</span>
          <div class="info">
            <div>${esc(r.error)}</div>
            <div class="file">${esc(r.filename)}</div>
          </div>
        </div>`;
    })
    .join("");

  progress.classList.add("hidden");
  results.classList.remove("hidden");
  loadDashboard();

  // single successful file → jump straight to the extracted invoice
  const successes = rows.filter((r) => r.ok);
  if (rows.length === 1 && successes.length === 1) {
    viewExtracted(successes[0].invoice.id);
  }
}

window.viewExtracted = function (id) {
  $("#extract-overlay").classList.add("hidden");
  openInvoice(id);
};

$("#extract-done").addEventListener("click", () => $("#extract-overlay").classList.add("hidden"));

/* chat flow: inline status messages */
async function extractForChat(files) {
  addChatSystem(`⏳ Processing ${files.length} file(s): ${files.map((f) => f.name).join(", ")} …`);
  try {
    const rows = await postFiles(files);
    for (const r of rows) {
      if (r.ok) {
        const inv = r.invoice;
        addChatSystem(`✅ ${r.filename}: filed invoice ${inv.invoice_number || "(no number)"} from ${inv.vendor_name || "unknown vendor"} — ${fmtMoney(inv.total_amount, inv.currency)}${inv.confidence === "low" ? " ⚠️ low confidence, review it" : ""}`);
      } else {
        addChatSystem(`❌ ${r.filename}: ${r.error}`, true);
      }
    }
    loadDashboard();
  } catch (e) {
    addChatSystem(`Upload failed: ${e.message}`, true);
  }
}

/* ---------------- dashboard ---------------- */
async function loadDashboard() {
  const [stats, activity] = await Promise.all([
    fetch("/api/stats").then((r) => r.json()),
    fetch("/api/activity").then((r) => r.json()),
  ]);

  const byStatus = Object.fromEntries(stats.by_status.map((s) => [s.status, s]));
  const pending = byStatus.pending || { c: 0, s: 0 };
  const paid = byStatus.paid || { c: 0, s: 0 };
  $("#stat-cards").innerHTML = `
    <div class="card"><div class="label">Total invoices</div><div class="value">${stats.total_invoices}</div>
      <div class="sub">${fmtMoney(stats.total_amount)} recorded</div></div>
    <div class="card"><div class="label">Pending</div><div class="value">${pending.c}</div>
      <div class="sub">${fmtMoney(pending.s)} awaiting action</div></div>
    <div class="card"><div class="label">Paid</div><div class="value">${paid.c}</div>
      <div class="sub">${fmtMoney(paid.s)} settled</div></div>
    <div class="card"><div class="label">Needs review</div><div class="value">${stats.needs_review}</div>
      <div class="sub">low-confidence extractions</div></div>`;

  const months = stats.by_month.filter((m) => m.m);
  const maxM = Math.max(...months.map((m) => m.s), 1);
  $("#chart-months").innerHTML = months.length
    ? months.map((m) => `
        <div class="bar-col">
          <div class="bv">${m.s >= 1000 ? (m.s / 1000).toFixed(1) + "k" : Math.round(m.s)}</div>
          <div class="bar" style="height:${Math.max(4, (m.s / maxM) * 120)}px"></div>
          <div class="bl">${m.m}</div>
        </div>`).join("")
    : '<div class="empty">No data yet</div>';

  const maxV = Math.max(...stats.top_vendors.map((v) => v.s), 1);
  $("#chart-vendors").innerHTML = stats.top_vendors.length
    ? stats.top_vendors.map((v) => `
        <div class="hbar">
          <div class="name" title="${esc(v.vendor_name)}">${esc(v.vendor_name)}</div>
          <div class="track"><div class="fill" style="width:${(v.s / maxV) * 100}%"></div></div>
          <div class="amt">${fmtMoney(v.s)}</div>
        </div>`).join("")
    : '<div class="empty">No data yet</div>';

  $("#due-soon").innerHTML = stats.due_soon.length
    ? `<table class="inv-table"><tbody>${stats.due_soon.map((i) => `
        <tr onclick="openInvoice(${i.id})">
          <td>${esc(i.vendor_name || "—")}</td><td>${esc(i.invoice_number || "—")}</td>
          <td>due ${fmtDate(i.due_date)}</td>
          <td class="num">${fmtMoney(i.total_amount, i.currency)}</td>
          <td><span class="status-pill status-${i.status}">${i.status}</span></td>
        </tr>`).join("")}</tbody></table>`
    : '<div class="empty">Nothing due — all clear 🎉</div>';

  $("#activity-log").innerHTML = activity.length
    ? activity.map((a) => `
        <div class="act-row">
          <span class="when">${a.created_at.slice(0, 16).replace("T", " ")}</span>
          <span class="tag ${a.event}">${a.event.replace("_", " ")}</span>
          <span>${esc(a.detail || "")}</span>
        </div>`).join("")
    : '<div class="empty">No activity yet</div>';
}

/* ---------------- invoice log ---------------- */
let searchTimer;
$("#inv-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadInvoices, 300);
});
$("#inv-status").addEventListener("change", loadInvoices);

async function loadInvoices() {
  const params = new URLSearchParams();
  if ($("#inv-search").value.trim()) params.set("search", $("#inv-search").value.trim());
  if ($("#inv-status").value) params.set("status", $("#inv-status").value);
  const rows = await fetch(`/api/invoices?${params}`).then((r) => r.json());
  $("#inv-empty").classList.toggle("hidden", rows.length > 0);
  $("#inv-rows").innerHTML = rows.map((i) => `
    <tr onclick="openInvoice(${i.id})">
      <td>${i.id}</td>
      <td>${esc(i.vendor_name || "—")}${i.confidence === "low" ? '<span class="conf-low">⚠ review</span>' : ""}</td>
      <td>${esc(i.invoice_number || "—")}</td>
      <td>${fmtDate(i.invoice_date)}</td>
      <td>${fmtDate(i.due_date)}</td>
      <td class="num">${fmtMoney(i.total_amount, i.currency)}</td>
      <td><span class="status-pill status-${i.status}">${i.status}</span></td>
      <td>${i.source}</td>
      <td>›</td>
    </tr>`).join("");
}

/* ---------------- invoice detail modal ---------------- */
window.openInvoice = async function (id) {
  const inv = await fetch(`/api/invoices/${id}`).then((r) => r.json());
  const items = Array.isArray(inv.line_items) ? inv.line_items : [];
  const warnings = Array.isArray(inv.extraction_warnings) ? inv.extraction_warnings : [];
  $("#modal-body").innerHTML = `
    <h2>${esc(inv.vendor_name || "Unknown vendor")}</h2>
    <p style="color:var(--muted);font-size:13px;margin-top:4px">
      Invoice ${esc(inv.invoice_number || "—")} · <span class="status-pill status-${inv.status}">${inv.status}</span>
      · source: ${inv.source}${inv.original_filename ? ` (${esc(inv.original_filename)})` : ""}</p>
    <div class="detail-grid">
      <div><div class="k">Invoice date</div>${fmtDate(inv.invoice_date)}</div>
      <div><div class="k">Due date</div>${fmtDate(inv.due_date)}</div>
      <div><div class="k">Subtotal</div>${fmtMoney(inv.subtotal, inv.currency)}</div>
      <div><div class="k">Tax</div>${fmtMoney(inv.tax_amount, inv.currency)}</div>
      <div><div class="k">Discount</div>${fmtMoney(inv.discount_amount, inv.currency)}</div>
      <div><div class="k">Total</div><strong>${fmtMoney(inv.total_amount, inv.currency)}</strong></div>
      <div><div class="k">Payment terms</div>${esc(inv.payment_terms || "—")}</div>
      <div><div class="k">PO number</div>${esc(inv.po_number || "—")}</div>
      <div><div class="k">Vendor tax ID</div>${esc(inv.vendor_tax_id || "—")}</div>
      <div><div class="k">Confidence</div>${inv.confidence}</div>
    </div>
    ${items.length ? `
      <div class="detail-items"><div class="k">Line items</div>
      <table><thead><tr><th>Description</th><th>Qty</th><th>Unit</th><th>Amount</th></tr></thead>
      <tbody>${items.map((li) => `
        <tr><td>${esc(li.description || "")}</td><td>${li.quantity ?? ""}</td>
        <td>${li.unit_price ?? ""}</td><td>${li.amount ?? ""}</td></tr>`).join("")}
      </tbody></table></div>` : ""}
    ${warnings.length ? `<ul class="warn-list">${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
    ${inv.notes ? `<p style="margin-top:12px;font-size:13px"><span class="k">Notes: </span>${esc(inv.notes)}</p>` : ""}
    <div class="modal-actions">
      ${["pending", "approved", "paid", "rejected"]
        .filter((s) => s !== inv.status)
        .map((s) => `<button class="btn" onclick="setStatus(${inv.id}, '${s}')">Mark ${s}</button>`).join("")}
      <button class="btn" style="color:var(--red)" onclick="deleteInvoice(${inv.id})">Delete</button>
    </div>`;
  $("#modal").classList.remove("hidden");
};

window.setStatus = async function (id, status) {
  await fetch(`/api/invoices/${id}/status`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  $("#modal").classList.add("hidden");
  toast(`Invoice #${id} marked ${status}`);
  loadInvoices();
  loadDashboard();
};

window.deleteInvoice = async function (id) {
  if (!confirm(`Delete invoice #${id}? This cannot be undone.`)) return;
  await fetch(`/api/invoices/${id}`, { method: "DELETE" });
  $("#modal").classList.add("hidden");
  toast(`Invoice #${id} deleted`);
  loadInvoices();
  loadDashboard();
};

$("#modal-close").addEventListener("click", () => $("#modal").classList.add("hidden"));
$("#modal").addEventListener("click", (e) => {
  if (e.target === $("#modal")) $("#modal").classList.add("hidden");
});

/* ---------------- chat ---------------- */
const chatHistory = [];

function addChatMsg(role, text) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.innerHTML = `<div class="bubble"></div>`;
  div.querySelector(".bubble").textContent = text;
  $("#chat-messages").appendChild(div);
  $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
  return div.querySelector(".bubble");
}

function addChatSystem(text, isError = false) {
  const div = document.createElement("div");
  div.className = `msg system${isError ? " err" : ""}`;
  div.innerHTML = `<div class="bubble"></div>`;
  div.querySelector(".bubble").textContent = text;
  $("#chat-messages").appendChild(div);
  $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
}

async function sendChat() {
  const input = $("#chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  input.style.height = "auto";
  addChatMsg("user", message);
  const bubble = addChatMsg("assistant", "");
  bubble.classList.add("typing");
  bubble.textContent = "Thinking…";
  $("#chat-send").disabled = true;

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, history: chatHistory }),
    });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let full = "", buffer = "";
    bubble.classList.remove("typing");
    bubble.textContent = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop();
      for (const evt of events) {
        const line = evt.trim();
        if (!line.startsWith("data: ")) continue;
        const payload = line.slice(6);
        if (payload === "[DONE]") continue;
        const obj = JSON.parse(payload);
        if (obj.error) {
          bubble.textContent = `Error: ${obj.error}`;
          bubble.closest(".msg").classList.add("err");
        } else if (obj.text) {
          full += obj.text;
          bubble.textContent = full;
          $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
        }
      }
    }
    chatHistory.push({ role: "user", content: message });
    chatHistory.push({ role: "assistant", content: full });
  } catch (e) {
    bubble.classList.remove("typing");
    bubble.textContent = `Request failed: ${e.message}`;
  } finally {
    $("#chat-send").disabled = false;
    input.focus();
  }
}

$("#chat-send").addEventListener("click", sendChat);
$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendChat();
  }
});
$("#chat-input").addEventListener("input", function () {
  this.style.height = "auto";
  this.style.height = Math.min(this.scrollHeight, 130) + "px";
});

/* ---------------- manual entry ---------------- */
function addLineRow() {
  const row = document.createElement("div");
  row.className = "line-item-row";
  row.innerHTML = `
    <input placeholder="Description" data-f="description">
    <input placeholder="Qty" type="number" step="any" data-f="quantity">
    <input placeholder="Unit price" type="number" step="0.01" data-f="unit_price">
    <input placeholder="Amount" type="number" step="0.01" data-f="amount">
    <button type="button" class="rm" title="Remove">✕</button>`;
  row.querySelector(".rm").addEventListener("click", () => row.remove());
  $("#line-items").appendChild(row);
}
$("#add-line").addEventListener("click", addLineRow);
addLineRow();

$("#manual-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const msg = $("#manual-msg");
  const body = {};
  new FormData(form).forEach((v, k) => {
    if (v === "") return;
    body[k] = ["subtotal", "tax_amount", "discount_amount", "total_amount"].includes(k) ? Number(v) : v;
  });
  body.line_items = [...$("#line-items").querySelectorAll(".line-item-row")]
    .map((row) => {
      const item = {};
      row.querySelectorAll("input").forEach((inp) => {
        if (inp.value === "") return;
        item[inp.dataset.f] = inp.dataset.f === "description" ? inp.value : Number(inp.value);
      });
      return item;
    })
    .filter((i) => i.description);

  msg.textContent = "Saving…";
  msg.className = "";
  try {
    const res = await fetch("/api/invoices/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Save failed");
    }
    const inv = await res.json();
    msg.textContent = `✅ Saved invoice #${inv.id}`;
    msg.className = "ok";
    form.reset();
    $("#line-items").innerHTML = "";
    addLineRow();
    loadDashboard();
  } catch (err) {
    msg.textContent = `❌ ${err.message}`;
    msg.className = "err";
  }
});

/* ---------------- backup / restore ---------------- */
$("#restore-btn").addEventListener("click", () => $("#restore-input").click());

$("#restore-input").addEventListener("change", async () => {
  const file = $("#restore-input").files[0];
  $("#restore-input").value = "";
  if (!file) return;
  if (!confirm("Restore this backup? It replaces the current database with the backup's contents.")) return;
  const msg = $("#restore-msg");
  msg.textContent = "Restoring…";
  msg.className = "";
  const form = new FormData();
  form.append("file", file);
  try {
    const res = await fetch("/api/restore", { method: "POST", body: form });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || "Restore failed");
    msg.textContent = `✅ Restored ${body.invoices} invoices (${body.files} files)`;
    msg.className = "ok";
    loadDashboard();
    loadInvoices();
  } catch (e) {
    msg.textContent = `❌ ${e.message}`;
    msg.className = "err";
  }
});

/* ---------------- auth ---------------- */
$("#logout-btn").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  window.location.href = "/login.html";
});

/* ---------------- init ---------------- */
loadDashboard();
