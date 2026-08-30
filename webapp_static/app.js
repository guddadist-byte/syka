"use strict";

const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
if (tg) {
  tg.ready();
  tg.expand();
  try { tg.setHeaderColor("#2a0a10"); } catch (e) {}
  try { tg.setBackgroundColor("#120306"); } catch (e) {}
}

const INIT_DATA = tg ? tg.initData : "";

const state = {
  me: null,
  history: [],
};

// --- API helper ------------------------------------------------------------

async function api(path, options = {}) {
  const opts = Object.assign({}, options);
  const isFormData = opts.body instanceof FormData;
  opts.headers = Object.assign(
    { "X-Telegram-Init-Data": INIT_DATA },
    isFormData ? {} : { "Content-Type": "application/json" },
    options.headers || {}
  );
  const resp = await fetch("/api" + path, opts);
  let data = null;
  try { data = await resp.json(); } catch (e) { data = null; }
  if (!resp.ok) {
    const err = new Error((data && (data.detail || data.error)) || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

function apiGet(path) { return api(path); }
function apiPost(path, body) { return api(path, { method: "POST", body: JSON.stringify(body || {}) }); }
function apiPatch(path, body) { return api(path, { method: "PATCH", body: JSON.stringify(body || {}) }); }
function apiDelete(path) { return api(path, { method: "DELETE" }); }
function apiUpload(path, formData) { return api(path, { method: "POST", body: formData }); }

// A plain <img src="/api/..."> can't carry the auth header the backend
// requires on every /api/ route — fetch the bytes ourselves (with the
// header) and hand the <img> a blob: URL instead.
async function apiBlobUrl(path) {
  const resp = await fetch("/api" + path, { headers: { "X-Telegram-Init-Data": INIT_DATA } });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const blob = await resp.blob();
  return URL.createObjectURL(blob);
}

// --- toast --------------------------------------------------------------

let toastTimer = null;
function toast(text) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

// --- header / navigation --------------------------------------------------

const screenRoot = document.getElementById("screen-root");
const headerTitle = document.getElementById("headerTitle");
const headerSubtitle = document.getElementById("headerSubtitle");
const backBtn = document.getElementById("backBtn");
const headerLogo = document.getElementById("headerLogo");

function setHeader(title, subtitle, showBack) {
  headerTitle.textContent = title;
  headerSubtitle.textContent = subtitle || "";
  backBtn.hidden = !showBack;
  headerLogo.style.display = showBack ? "none" : "block";
}

backBtn.addEventListener("click", () => {
  if (state.history.length) {
    const prev = state.history.pop();
    render(prev.screen, prev.params, true);
  } else {
    render("home", {}, true);
  }
});

function go(screen, params) {
  state.history.push({ screen: currentScreen, params: currentParams });
  render(screen, params);
}

let currentScreen = "home";
let currentParams = {};

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

function loading() {
  screenRoot.innerHTML = '<div class="spinner"></div>';
}

// --- BackButton (native Telegram) hooked to same history -------------------

if (tg && tg.BackButton) {
  tg.BackButton.onClick(() => backBtn.click());
}

function syncNativeBack(show) {
  if (!tg || !tg.BackButton) return;
  if (show) tg.BackButton.show(); else tg.BackButton.hide();
}

// ============================================================================
// Screens
// ============================================================================

const SCREENS = {};

async function render(screen, params, isBack) {
  currentScreen = screen;
  currentParams = params || {};
  if (!isBack) {
    // history already pushed by go()
  }
  syncNativeBack(screen !== "home");
  const fn = SCREENS[screen];
  if (!fn) { renderHome(); return; }
  await fn(currentParams);
}

// --- Home / tile menu -------------------------------------------------------

SCREENS.home = renderHome;
async function renderHome() {
  setHeader(state.me ? state.me.full_name : "GUDDA CRM", state.me ? state.me.role_label : "", false);
  loading();
  try {
    const me = await apiGet("/me");
    state.me = me;
    let unreadCount = "";
    try {
      const chats = await apiGet("/chats?filter=unread");
      if (chats.chats.length) unreadCount = chats.chats.length;
    } catch (e) {}

    headerTitle.textContent = me.full_name || "GUDDA CRM";
    headerSubtitle.textContent = me.role_label + (me.points.length ? " · " + me.points.map(p => p.name).join(", ") : "");

    screenRoot.innerHTML = `
      <div class="tiles">
        <button class="tile wide ${me.on_shift ? "shift-on" : "shift-off"}" id="shiftTile">
          <span class="tile-icon">${me.on_shift ? "💼" : "🛌"}</span>
          <span class="tile-label">${me.on_shift ? "Вы на смене — нажмите, чтобы уйти отдыхать" : "Вы отдыхаете — нажмите, чтобы выйти на смену"}</span>
        </button>
        <button class="tile" data-go="chats" data-filter="unread">
          <span class="tile-icon">📩</span>
          <span class="tile-label">Непрочитанные</span>
          ${unreadCount ? `<span class="tile-badge">${unreadCount}</span>` : ""}
        </button>
        <button class="tile" data-go="chats" data-filter="recent">
          <span class="tile-icon">🕒</span>
          <span class="tile-label">Недавние</span>
        </button>
        <button class="tile" data-go="points">
          <span class="tile-icon">📍</span>
          <span class="tile-label">Мои точки</span>
        </button>
        <button class="tile" data-go="orders">
          <span class="tile-icon">📦</span>
          <span class="tile-label">Заказы Avito</span>
        </button>
        <button class="tile ${me.is_admin ? "" : "wide"}" data-go="profile">
          <span class="tile-icon">👤</span>
          <span class="tile-label">Мой профиль</span>
        </button>
        ${me.is_manager_or_above ? `
        <button class="tile ${me.is_admin ? "" : "wide"}" data-go="myTemplates">
          <span class="tile-icon">📋</span>
          <span class="tile-label">Мои шаблоны</span>
        </button>` : ""}
        ${me.is_admin ? `
        <button class="tile wide" data-go="adminHome">
          <span class="tile-icon">⚙️</span>
          <span class="tile-label">Админ-панель</span>
        </button>` : ""}
      </div>
    `;

    document.getElementById("shiftTile").addEventListener("click", async (e) => {
      e.currentTarget.disabled = true;
      try {
        const res = await apiPost("/shift", { on_shift: !me.on_shift });
        toast(res.on_shift ? "💼 Вы на смене" : "🛌 Вы отдыхаете");
        renderHome();
      } catch (err) {
        toast("Ошибка: " + err.message);
        e.currentTarget.disabled = false;
      }
    });

    screenRoot.querySelectorAll("[data-go]").forEach(btn => {
      btn.addEventListener("click", () => go(btn.dataset.go, { filter: btn.dataset.filter }));
    });
  } catch (err) {
    renderError(err, renderHome);
  }
}

function renderError(err, retry) {
  let msg = "Ошибка загрузки";
  if (err && err.data && err.data.error === "not_approved") {
    msg = "Ваша заявка ещё не одобрена. Откройте бота и нажмите /start.";
  } else if (err && err.data && err.data.error === "not_registered") {
    msg = "Вы ещё не зарегистрированы. Откройте бота и нажмите /start.";
  } else if (err && err.data && err.data.error === "blocked") {
    msg = "Ваш доступ заблокирован. Обратитесь к руководителю.";
  } else if (err && err.message) {
    msg = err.message;
  }
  screenRoot.innerHTML = `
    <div class="empty-state">${esc(msg)}</div>
    <button class="btn block secondary" id="retryBtn">Обновить</button>
  `;
  const retryBtn = document.getElementById("retryBtn");
  if (retryBtn) retryBtn.addEventListener("click", () => retry());
}

// --- Chats list --------------------------------------------------------------

SCREENS.chats = renderChats;
async function renderChats(params) {
  const filter = params.filter || "unread";
  setHeader(filter === "recent" ? "Недавние" : "Непрочитанные", "", true);
  loading();
  try {
    const data = await apiGet(`/chats?filter=${filter}`);
    if (!data.chats.length) {
      screenRoot.innerHTML = '<div class="empty-state">Пока пусто</div>';
      return;
    }
    screenRoot.innerHTML = data.chats.map(c => `
      <button class="list-btn ${c.is_new_lead ? "lead" : ""}" data-short="${esc(c.short_id)}">
        <div class="row-top">
          <span class="name">${c.is_new_lead ? "🆕 " : "💬 "}${esc(c.client_name || "Клиент")}</span>
          ${c.unread_count ? `<span class="unread-dot">${c.unread_count}</span>` : ""}
        </div>
        ${c.item_title ? `<div class="preview">📦 ${esc(c.item_title)}</div>` : ""}
      </button>
    `).join("");
    screenRoot.querySelectorAll("[data-short]").forEach(btn => {
      btn.addEventListener("click", () => go("chatDetail", { shortId: btn.dataset.short }));
    });
  } catch (err) {
    renderError(err, () => renderChats(params));
  }
}

// --- Chat detail --------------------------------------------------------------

SCREENS.chatDetail = renderChatDetail;
async function renderChatDetail(params) {
  setHeader("Чат", "", true);
  loading();
  try {
    const chat = await apiGet(`/chats/${params.shortId}`);
    setHeader(chat.client_name || "Клиент", chat.item_title || "", true);

    screenRoot.innerHTML = `
      ${chat.item_title ? `
        <div class="card">
          <div class="card-row">
            <span>📦 ${chat.item_url ? `<a href="${esc(chat.item_url)}" target="_blank" style="color:var(--accent-2)">${esc(chat.item_title)}</a>` : esc(chat.item_title)}</span>
          </div>
        </div>` : ""}
      <div class="messages" id="msgList">
        ${chat.messages.map(m => `<div class="msg ${m.direction}">${esc(m.text) || (m.has_image ? "📷 Фото" : "")}</div>`).join("")}
      </div>
      <div id="sentBanner"></div>
      <div class="chat-actions">
        <button class="btn secondary small" id="markReadBtn">✅ Прочитано</button>
        <button class="btn secondary small" id="refreshBtn">🔄 Обновить</button>
        <button class="btn secondary small" id="aiBtn">🧠 ИИ-ответ</button>
        <button class="btn secondary small" id="tplBtn">📋 Шаблоны</button>
      </div>
      <div id="assistPanel"></div>
      <div id="warnBanner"></div>
      <div class="reply-bar">
        <input type="file" id="photoInput" accept="image/*" multiple hidden>
        <button class="icon-btn" id="photoBtn" style="background:var(--card-bg);border:1px solid var(--card-border)">📷</button>
        <textarea id="replyText" rows="1" placeholder="Ответ клиенту…"></textarea>
        <button class="icon-btn" id="sendBtn">➤</button>
      </div>
    `;

    const msgList = document.getElementById("msgList");
    msgList.scrollTop = msgList.scrollHeight;
    const replyText = document.getElementById("replyText");
    const sendBtn = document.getElementById("sendBtn");
    const warnBanner = document.getElementById("warnBanner");
    let lastDraft = null;

    function clearWarning() {
      warnBanner.innerHTML = "";
      sendBtn.disabled = false;
    }
    replyText.addEventListener("input", () => {
      if (lastDraft !== null && replyText.value !== lastDraft) clearWarning();
    });

    function applyDraft(draft, allowSend) {
      replyText.value = draft;
      lastDraft = draft;
      document.getElementById("assistPanel").innerHTML = "";
      if (!allowSend) {
        sendBtn.disabled = true;
        warnBanner.innerHTML = `<div class="card" style="border-color:var(--danger);font-size:12.5px">⚠️ В черновике похоже есть цена/оценка — отправка заблокирована. Отредактируйте текст, чтобы снять блок.</div>`;
      } else {
        clearWarning();
      }
    }

    document.getElementById("markReadBtn").addEventListener("click", async () => {
      try {
        await apiPost(`/chats/${params.shortId}/read`, {});
        toast("Отмечено прочитанным");
        renderChatDetail(params);
      } catch (err) { toast("Ошибка: " + err.message); }
    });

    document.getElementById("refreshBtn").addEventListener("click", () => renderChatDetail(params));

    document.getElementById("aiBtn").addEventListener("click", () => {
      const panel = document.getElementById("assistPanel");
      panel.innerHTML = `
        <div class="card field">
          <label>Промпт для ИИ (необязательно — оставьте пустым для авто-ответа)</label>
          <input type="text" id="aiPromptInput" placeholder="например: уточни про доставку">
          <button class="btn block small" id="aiGenBtn">Сгенерировать черновик</button>
        </div>
      `;
      document.getElementById("aiGenBtn").addEventListener("click", async () => {
        const prompt = document.getElementById("aiPromptInput").value.trim();
        toast("Генерирую черновик…");
        try {
          const res = await apiPost(`/chats/${params.shortId}/ai-draft`, prompt ? { prompt } : {});
          applyDraft(res.draft, res.allow_send);
        } catch (err) {
          toast("Ошибка ИИ: " + err.message);
        }
      });
    });

    document.getElementById("tplBtn").addEventListener("click", async () => {
      const panel = document.getElementById("assistPanel");
      panel.innerHTML = '<div class="spinner"></div>';
      try {
        const data = await apiGet(`/chats/${params.shortId}/templates`);
        if (!data.templates.length) {
          panel.innerHTML = '<div class="card" style="font-size:13px">Шаблонов для этой точки пока нет.</div>';
          return;
        }
        panel.innerHTML = `<div class="card" style="gap:8px">` + data.templates.map(t =>
          `<button class="list-btn" data-tpl="${t.id}">${t.kind === "ai_prompt" ? "🧠" : "📝"} ${esc(t.title)}</button>`
        ).join("") + `</div>`;
        panel.querySelectorAll("[data-tpl]").forEach(btn => {
          btn.addEventListener("click", async () => {
            toast("Применяю шаблон…");
            try {
              const res = await apiPost(`/chats/${params.shortId}/templates/${btn.dataset.tpl}/apply`, {});
              applyDraft(res.draft, res.allow_send);
            } catch (err) {
              toast("Ошибка: " + err.message);
            }
          });
        });
      } catch (err) {
        toast("Ошибка: " + err.message);
      }
    });

    document.getElementById("photoBtn").addEventListener("click", () => {
      document.getElementById("photoInput").click();
    });
    document.getElementById("photoInput").addEventListener("change", async (e) => {
      const files = Array.from(e.target.files || []);
      if (!files.length) return;
      const form = new FormData();
      files.forEach(f => form.append("photos", f, f.name));
      sendBtn.disabled = true;
      toast(`Отправляю ${files.length} фото…`);
      try {
        const res = await apiUpload(`/chats/${params.shortId}/reply-photo`, form);
        for (let i = 0; i < res.sent_count; i++) {
          const bubble = document.createElement("div");
          bubble.className = "msg out";
          bubble.textContent = "📷 Фото";
          msgList.appendChild(bubble);
        }
        msgList.scrollTop = msgList.scrollHeight;
        showSentBanner(res.msg_ref, `✅ Отправлено ${res.sent_count} фото`);
      } catch (err) {
        toast("Ошибка отправки фото: " + err.message);
      } finally {
        sendBtn.disabled = false;
        e.target.value = "";
      }
    });

    function showSentBanner(msgRef, label) {
      const banner = document.getElementById("sentBanner");
      if (!banner) return;
      banner.innerHTML = `
        <div class="card card-row" style="font-size:12.5px">
          <span>${label}</span>
          <button class="btn secondary small" id="deleteSentBtn">🗑 Удалить</button>
        </div>
      `;
      document.getElementById("deleteSentBtn").addEventListener("click", async () => {
        try {
          await apiDelete(`/messages/${msgRef}`);
          toast("🗑 Сообщение удалено");
          banner.innerHTML = "";
        } catch (err) {
          toast("Не удалось удалить: " + err.message);
        }
      });
    }

    sendBtn.addEventListener("click", async () => {
      const text = replyText.value.trim();
      if (!text) return;
      sendBtn.disabled = true;
      try {
        const res = await apiPost(`/chats/${params.shortId}/reply`, { text });
        const bubble = document.createElement("div");
        bubble.className = "msg out";
        bubble.textContent = text;
        msgList.appendChild(bubble);
        msgList.scrollTop = msgList.scrollHeight;
        showSentBanner(res.msg_ref, "✅ Отправлено");
        replyText.value = "";
        lastDraft = null;
      } catch (err) {
        toast("Ошибка отправки: " + err.message);
      } finally {
        sendBtn.disabled = false;
      }
    });
  } catch (err) {
    renderError(err, () => renderChatDetail(params));
  }
}

// --- Points (subscriptions) --------------------------------------------------

SCREENS.points = renderPoints;
async function renderPoints() {
  setHeader("Мои точки", "", true);
  loading();
  try {
    const [all, mine] = await Promise.all([apiGet("/points"), apiGet("/points/mine")]);
    const mineSet = new Set(mine.point_ids);
    if (!all.points.length) {
      screenRoot.innerHTML = '<div class="empty-state">Точек пока нет</div>';
      return;
    }
    screenRoot.innerHTML = all.points.map(p => `
      <div class="card">
        <div class="card-row">
          <div>
            <div style="font-weight:600">${esc(p.name)}</div>
            ${p.address ? `<div style="font-size:12px;color:var(--text-dim)">${esc(p.address)}</div>` : ""}
          </div>
          <label class="switch">
            <input type="checkbox" data-point="${p.id}" ${mineSet.has(p.id) ? "checked" : ""}>
            <span class="track"><span class="thumb"></span></span>
          </label>
        </div>
      </div>
    `).join("");
    screenRoot.querySelectorAll("input[data-point]").forEach(input => {
      input.addEventListener("change", async () => {
        try {
          await apiPost("/points/subscribe", { point_id: Number(input.dataset.point), subscribed: input.checked });
          toast(input.checked ? "Подписка оформлена" : "Подписка снята");
        } catch (err) {
          input.checked = !input.checked;
          toast("Ошибка: " + err.message);
        }
      });
    });
  } catch (err) {
    renderError(err, renderPoints);
  }
}

// --- Orders list ---------------------------------------------------------------

SCREENS.orders = renderOrders;
async function renderOrders() {
  setHeader("Заказы Avito", "", true);
  loading();
  try {
    const data = await apiGet("/orders");
    if (data.errors && data.errors.length) {
      toast("⚠️ " + data.errors[0]);
    }
    if (!data.orders.length) {
      screenRoot.innerHTML = '<div class="empty-state">Активных заказов нет</div>';
      return;
    }
    screenRoot.innerHTML = data.orders.map(o => `
      <button class="list-btn" data-order="${esc(o.id)}" data-account="${esc(o.account_id)}">
        <div class="row-top">
          <span class="name">${esc(o.status_label)}</span>
        </div>
        <div class="preview">${esc(o.title)} · ${esc(o.account_name || "")}</div>
      </button>
    `).join("");
    screenRoot.querySelectorAll("[data-order]").forEach(btn => {
      btn.addEventListener("click", () => go("orderDetail", { orderId: btn.dataset.order, accountId: btn.dataset.account }));
    });
  } catch (err) {
    renderError(err, renderOrders);
  }
}

// --- Order detail --------------------------------------------------------------

SCREENS.orderDetail = renderOrderDetail;
async function renderOrderDetail(params) {
  setHeader("Заказ", "", true);
  loading();
  try {
    const order = await apiGet(`/orders/${params.accountId}/${params.orderId}`);
    setHeader(order.status_label, order.account_name || "", true);

    const actions = order.available_actions || [];
    const actionBtn = (name, label, cls) =>
      actions.includes(name) ? `<button class="btn ${cls || ""} small" data-action="${name}">${label}</button>` : "";

    screenRoot.innerHTML = `
      ${order.has_barcode ? `<img class="barcode-img" id="barcodeImg" alt="barcode">` : ""}
      <div class="card">
        ${order.point_name ? `<div class="card-row"><span>Точка</span><span>${esc(order.point_name)}</span></div>` : ""}
        ${order.point_address ? `<div class="card-row"><span>Адрес</span><span>${esc(order.point_address)}</span></div>` : ""}
        <div class="card-row"><span>Кабинет</span><span>${esc(order.account_name)}</span></div>
        <div class="card-row"><span>Номер заказа</span><span>${esc(order.id)}</span></div>
        ${order.track_number ? `<div class="card-row"><span>Трек-номер</span><span>${esc(order.track_number)}</span></div>` : ""}
        <div class="card-row"><span>Товар</span><span>${esc((order.items || []).join(", "))}</span></div>
        ${order.total != null ? `<div class="card-row"><span>Сумма</span><span>${esc(order.total)}</span></div>` : ""}
        ${order.commission != null ? `<div class="card-row"><span>Комиссия</span><span>${esc(order.commission)}</span></div>` : ""}
        ${order.delivery_service ? `<div class="card-row"><span>Служба доставки</span><span>${esc(order.delivery_service)}</span></div>` : ""}
      </div>

      <div class="chat-actions">
        ${actionBtn("confirm", "✅ Подтвердить")}
        ${actionBtn("reject", "❌ Отменить", "secondary")}
        ${actionBtn("setMarkings", "🏷 Маркировка", "secondary")}
        ${actionBtn("setCNCDetails", "📍 Подготовить самовывоз", "secondary")}
        ${order.delivery_type === "pvz" ? '<button class="btn secondary small" data-action="checkConfirmationCode">✅ Код получения</button>' : ""}
        ${order.chat_short_id ? `<button class="btn secondary small" id="orderChatBtn">💬 Чат с покупателем</button>` : ""}
      </div>
      <div id="orderActionForm"></div>
    `;

    if (order.chat_short_id) {
      document.getElementById("orderChatBtn").addEventListener("click", () => go("chatDetail", { shortId: order.chat_short_id }));
    }

    if (order.has_barcode) {
      // track_number came back with this same order-detail response —
      // pass it straight through so the backend renders the PNG locally
      // instead of re-fetching Avito's whole order list a second time
      // just to look it up again (that was the real ~20s slowdown).
      apiBlobUrl(`/orders/${params.accountId}/${params.orderId}/barcode.png?track=${encodeURIComponent(order.track_number)}`)
        .then(url => { const img = document.getElementById("barcodeImg"); if (img) img.src = url; })
        .catch(() => { const img = document.getElementById("barcodeImg"); if (img) img.remove(); });
    }

    screenRoot.querySelectorAll("[data-action]").forEach(btn => {
      btn.addEventListener("click", () => handleOrderAction(btn.dataset.action, params, order));
    });
  } catch (err) {
    renderError(err, () => renderOrderDetail(params));
  }
}

async function submitOrderAction(params, body) {
  try {
    await apiPost(`/orders/${params.accountId}/${params.orderId}/action`, body);
    toast("✅ Готово");
    renderOrderDetail(params);
  } catch (err) {
    toast("Ошибка: " + err.message);
  }
}

function handleOrderAction(action, params, order) {
  const formEl = document.getElementById("orderActionForm");
  if (action === "confirm" || action === "reject") {
    submitOrderAction(params, { action });
    return;
  }
  if (action === "setMarkings") {
    formEl.innerHTML = `
      <div class="card field">
        <label>Коды маркировки (через запятую)</label>
        <input type="text" id="markingsInput" placeholder="0104...  0104...">
        <button class="btn block" id="markingsSubmit">Отправить</button>
      </div>
    `;
    document.getElementById("markingsSubmit").addEventListener("click", () => {
      submitOrderAction(params, { action: "setMarkings", markings: document.getElementById("markingsInput").value });
    });
    return;
  }
  if (action === "setCNCDetails") {
    formEl.innerHTML = `
      <div class="card field">
        <label>Адрес выдачи</label>
        <input type="text" id="cncAddress">
        <label>Срок хранения (дней)</label>
        <input type="number" id="cncPeriod" value="7">
        <label>Комментарий (необязательно, "-" чтобы пропустить)</label>
        <input type="text" id="cncComment" placeholder="-">
        <button class="btn block" id="cncSubmit">Отправить</button>
      </div>
    `;
    document.getElementById("cncSubmit").addEventListener("click", () => {
      submitOrderAction(params, {
        action: "setCNCDetails",
        address: document.getElementById("cncAddress").value,
        period: Number(document.getElementById("cncPeriod").value || 7),
        comment: document.getElementById("cncComment").value || "-",
      });
    });
    return;
  }
  if (action === "checkConfirmationCode") {
    formEl.innerHTML = `
      <div class="card field">
        <label>Код получения от клиента</label>
        <input type="text" id="codeInput" inputmode="numeric">
        <button class="btn block" id="codeSubmit">Проверить</button>
      </div>
    `;
    document.getElementById("codeSubmit").addEventListener("click", () => {
      submitOrderAction(params, { action: "checkConfirmationCode", code: document.getElementById("codeInput").value });
    });
  }
}

// --- Profile -------------------------------------------------------------------

SCREENS.profile = renderProfile;
async function renderProfile() {
  setHeader("Мой профиль", "", true);
  loading();
  try {
    const p = await apiGet("/profile");
    screenRoot.innerHTML = `
      <div class="card">
        <div style="font-size:16px;font-weight:700">${esc(p.full_name)}</div>
        <div style="color:var(--text-dim);font-size:13px">${esc(p.role_label)}</div>
        ${p.points.length ? `<div style="font-size:13px">📍 ${esc(p.points.join(", "))}</div>` : ""}
        <div style="font-size:13px">${p.on_shift ? "💼 На смене" : "🛌 Отдыхает"}</div>
        <div class="card-row" style="margin-top:8px">
          <span>⭐ Рейтинг</span><span style="font-weight:700">${p.rating_points}</span>
        </div>
      </div>
      <div class="section-title">Общий рейтинг</div>
      <div class="card">
        ${p.leaderboard.map((row, i) => `
          <div class="leaderboard-row ${row.is_me ? "me" : ""}">
            <span>${i + 1}. ${esc(row.full_name || "—")}</span>
            <span>${row.rating_points}</span>
          </div>
        `).join("")}
        ${p.my_rank && p.my_rank > p.leaderboard.length ? `
          <div class="leaderboard-row me"><span>Ваше место: ${p.my_rank}</span><span>${p.rating_points}</span></div>
        ` : ""}
      </div>
    `;
  } catch (err) {
    renderError(err, renderProfile);
  }
}

// ============================================================================
// My templates (📋 Мои шаблоны — manager role, own responsible point)
// ============================================================================

SCREENS.myTemplates = renderMyTemplates;
async function renderMyTemplates() {
  setHeader("Мои шаблоны", "", true);
  loading();
  try {
    const data = await apiGet("/templates/mine");
    screenRoot.innerHTML = `
      <div class="card field">
        <label>Тип</label>
        <select id="newTplKind" style="border-radius:14px;border:1px solid var(--card-border);background:rgba(255,255,255,0.06);color:var(--text);padding:11px 13px;font-size:14px">
          <option value="text">📝 Текст</option>
          <option value="ai_prompt">🧠 AI-промпт</option>
        </select>
        <label>Заголовок</label>
        <input type="text" id="newTplTitle" placeholder="Например: Часы работы">
        <label>Текст / промпт</label>
        <input type="text" id="newTplBody" placeholder="!КОДВ — часы, !КОДА — адрес">
        <button class="btn block" id="newTplSubmit">➕ Создать шаблон</button>
      </div>
      <div class="section-title">Существующие</div>
      ${data.templates.length ? data.templates.map(t => `
        <div class="card card-row">
          <span>${t.kind === "ai_prompt" ? "🧠" : "📝"} ${esc(t.title)}</span>
          <button class="btn secondary small" data-del="${t.id}">🗑</button>
        </div>
      `).join("") : '<div class="empty-state">Шаблонов пока нет</div>'}
    `;
    document.getElementById("newTplSubmit").addEventListener("click", async () => {
      const kind = document.getElementById("newTplKind").value;
      const title = document.getElementById("newTplTitle").value.trim();
      const body = document.getElementById("newTplBody").value.trim();
      if (!body) { toast("Введите текст шаблона"); return; }
      try {
        await apiPost("/templates/mine", { kind, title, body });
        toast("✅ Создано");
        renderMyTemplates();
      } catch (err) { toast("Ошибка: " + err.message); }
    });
    screenRoot.querySelectorAll("[data-del]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await apiDelete(`/templates/mine/${btn.dataset.del}`);
          renderMyTemplates();
        } catch (err) { toast("Ошибка: " + err.message); }
      });
    });
  } catch (err) {
    renderError(err, renderMyTemplates);
  }
}

// ============================================================================
// Admin panel
// ============================================================================

SCREENS.adminHome = renderAdminHome;
async function renderAdminHome() {
  setHeader("Админ-панель", "", true);
  const sections = [
    ["adminUsers", "👥", "Все пользователи"],
    ["adminOnshift", "🕐", "Кто на смене"],
    ["adminRequests", "📋", "Заявки на вступление"],
    ["adminPoints", "🏢", "Точки"],
    ["adminAvito", "🔑", "Avito API"],
    ["adminAI", "🧠", "Настройки ИИ"],
    ["adminProxy", "🌐", "Прокси"],
    ["adminPayment", "⭐", "Платный доступ"],
    ["adminWelcome", "✉️", "Приветственное сообщение"],
    ["adminBackup", "💾", "Резервные копии"],
    ["adminReviews", "⭐", "Отзывы Avito"],
    ["adminBroadcast", "📢", "Сообщение всем"],
  ];
  screenRoot.innerHTML = sections.map(([screen, icon, label]) =>
    `<button class="list-btn" data-go="${screen}"><span class="name">${icon} ${label}</span></button>`
  ).join("");
  screenRoot.querySelectorAll("[data-go]").forEach(btn => {
    btn.addEventListener("click", () => go(btn.dataset.go, {}));
  });
}

// --- Users --------------------------------------------------------------

SCREENS.adminUsers = renderAdminUsers;
async function renderAdminUsers() {
  setHeader("Все пользователи", "", true);
  loading();
  try {
    const data = await apiGet("/admin/users");
    screenRoot.innerHTML = data.users.map(u => `
      <button class="list-btn" data-id="${u.telegram_id}">
        <div class="row-top">
          <span class="name">${esc(u.full_name || u.username || u.telegram_id)}</span>
          <span class="preview">${u.status !== "approved" ? "⛔" : ""}</span>
        </div>
        <div class="preview">${esc(u.role_label)}${u.trade_point_name ? " · " + esc(u.trade_point_name) : ""}</div>
      </button>
    `).join("");
    screenRoot.querySelectorAll("[data-id]").forEach(btn => {
      btn.addEventListener("click", () => go("adminUserEdit", { userId: btn.dataset.id }));
    });
  } catch (err) {
    renderError(err, renderAdminUsers);
  }
}

const ROLE_OPTIONS = [
  ["employee", "🧑‍💼 Сотрудник точки"],
  ["manager", "📋 Ответственный точки"],
  ["admin", "🛡 РОП"],
  ["director", "👑 Админ"],
];

SCREENS.adminUserEdit = renderAdminUserEdit;
async function renderAdminUserEdit(params) {
  setHeader("Пользователь", "", true);
  loading();
  try {
    const [usersData, pointsData, userPointsData] = await Promise.all([
      apiGet("/admin/users"), apiGet("/points"), apiGet(`/admin/users/${params.userId}/points`),
    ]);
    const user = usersData.users.find(u => String(u.telegram_id) === String(params.userId));
    if (!user) { screenRoot.innerHTML = '<div class="empty-state">Пользователь не найден</div>'; return; }
    setHeader(user.full_name || user.username || user.telegram_id, user.role_label, true);
    const subscribedIds = new Set(userPointsData.point_ids);

    screenRoot.innerHTML = `
      <div class="card field">
        <label>ФИО</label>
        <input type="text" id="editFullName" value="${esc(user.full_name || "")}">
        <label>Торговая точка (свободный текст)</label>
        <input type="text" id="editTradePoint" value="${esc(user.trade_point_name || "")}">
        <button class="btn block small" id="saveNameBtn">💾 Сохранить</button>
      </div>

      <div class="section-title">Роль</div>
      <div class="card" style="gap:8px">
        ${ROLE_OPTIONS.map(([code, label]) => `<button class="btn ${user.role === code ? "" : "secondary"} block small" data-role="${code}">${label}</button>`).join("")}
      </div>
      <div id="rolePickerBox"></div>

      <div class="section-title">Подписки на точки</div>
      <div class="card" id="pointsBox" style="gap:8px"></div>

      <div class="section-title">Действия</div>
      <div class="chat-actions">
        ${user.status === "blocked"
          ? '<button class="btn small" id="unblockBtn">🔓 Разблокировать</button>'
          : '<button class="btn secondary small" id="blockBtn">🚫 Уволить</button>'}
        <button class="btn secondary small" id="deleteBtn" style="border-color:var(--danger)">🗑 Удалить аккаунт</button>
      </div>
    `;

    const pointsBox = document.getElementById("pointsBox");
    pointsBox.innerHTML = pointsData.points.map(p => `
      <label class="card-row">
        <span>${esc(p.name)}</span>
        <input type="checkbox" data-point-check="${p.id}" ${subscribedIds.has(p.id) ? "checked" : ""}>
      </label>
    `).join("") + '<button class="btn block small" id="savePointsBtn" style="margin-top:8px">💾 Сохранить подписки</button>';

    document.getElementById("savePointsBtn").addEventListener("click", async () => {
      const ids = Array.from(pointsBox.querySelectorAll("[data-point-check]:checked")).map(cb => Number(cb.dataset.pointCheck));
      try {
        await apiPost(`/admin/users/${user.telegram_id}/points`, { point_ids: ids });
        toast("✅ Подписки сохранены");
      } catch (err) { toast("Ошибка: " + err.message); }
    });

    document.getElementById("saveNameBtn").addEventListener("click", async () => {
      try {
        await apiPatch(`/admin/users/${user.telegram_id}`, {
          full_name: document.getElementById("editFullName").value,
          trade_point_name: document.getElementById("editTradePoint").value,
        });
        toast("✅ Сохранено");
      } catch (err) { toast("Ошибка: " + err.message); }
    });

    screenRoot.querySelectorAll("[data-role]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const role = btn.dataset.role;
        let point_id = null;
        if (role === "manager") {
          const pid = await pickPointInline(pointsData.points, document.getElementById("rolePickerBox"));
          if (pid == null) return;
          point_id = pid;
        }
        try {
          await apiPost(`/admin/users/${user.telegram_id}/role`, { role, point_id });
          toast("✅ Роль изменена");
          renderAdminUserEdit(params);
        } catch (err) { toast("Ошибка: " + err.message); }
      });
    });

    document.getElementById("blockBtn")?.addEventListener("click", async () => {
      try {
        await apiPost(`/admin/users/${user.telegram_id}/block`, {});
        toast("🚫 Уволен");
        go("adminUsers", {});
      } catch (err) { toast("Ошибка: " + err.message); }
    });
    document.getElementById("unblockBtn")?.addEventListener("click", async () => {
      try {
        await apiPost(`/admin/users/${user.telegram_id}/unblock`, {});
        toast("🔓 Разблокирован");
        renderAdminUserEdit(params);
      } catch (err) { toast("Ошибка: " + err.message); }
    });
    document.getElementById("deleteBtn").addEventListener("click", async () => {
      try {
        await apiDelete(`/admin/users/${user.telegram_id}`);
        toast("🗑 Удалён");
        go("adminUsers", {});
      } catch (err) { toast("Ошибка: " + err.message); }
    });
  } catch (err) {
    renderError(err, () => renderAdminUserEdit(params));
  }
}

// Telegram's in-app WebView does not support window.prompt (no native
// equivalent in the Mini App popup API), so point selection anywhere in
// the admin UI is an inline <select> injected into a designated container
// rather than a browser prompt dialog.
function pickPointInline(points, containerEl) {
  return new Promise((resolve) => {
    containerEl.innerHTML = `
      <div class="card field" style="margin-top:8px">
        <label>Выберите точку</label>
        <select id="pointPickerSelect" style="border-radius:14px;border:1px solid var(--card-border);background:rgba(255,255,255,0.06);color:var(--text);padding:11px 13px;font-size:14px">
          ${points.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("")}
        </select>
        <div class="chat-actions">
          <button class="btn small" id="pointPickerOk">✅ Готово</button>
          <button class="btn secondary small" id="pointPickerCancel">Отмена</button>
        </div>
      </div>
    `;
    document.getElementById("pointPickerOk").addEventListener("click", () => {
      const val = Number(document.getElementById("pointPickerSelect").value);
      containerEl.innerHTML = "";
      resolve(val);
    });
    document.getElementById("pointPickerCancel").addEventListener("click", () => {
      containerEl.innerHTML = "";
      resolve(null);
    });
  });
}

// --- On shift -------------------------------------------------------------

SCREENS.adminOnshift = renderAdminOnshift;
async function renderAdminOnshift() {
  setHeader("Кто на смене", "", true);
  loading();
  try {
    const data = await apiGet("/admin/onshift");
    screenRoot.innerHTML = data.users.length ? data.users.map(u => `
      <div class="card card-row">
        <span>👤 ${esc(u.full_name || u.username || u.telegram_id)}</span>
        <span class="preview">${esc(u.role_label)} · ${esc(u.point_label)}</span>
      </div>
    `).join("") : '<div class="empty-state">Сейчас никто не на смене</div>';
  } catch (err) {
    renderError(err, renderAdminOnshift);
  }
}

// --- Access requests --------------------------------------------------------

SCREENS.adminRequests = renderAdminRequests;
async function renderAdminRequests() {
  setHeader("Заявки на вступление", "", true);
  loading();
  try {
    const data = await apiGet("/admin/requests");
    if (!data.requests.length) { screenRoot.innerHTML = '<div class="empty-state">Заявок нет</div>'; return; }
    screenRoot.innerHTML = data.requests.map(u => `
      <div class="card" data-req="${u.telegram_id}">
        <div style="font-weight:700">${esc(u.full_name || u.username || u.telegram_id)}</div>
        ${u.trade_point_name ? `<div class="preview">ТТ: ${esc(u.trade_point_name)}</div>` : ""}
        <div class="chat-actions" style="margin-top:8px">
          <button class="btn small" data-approve="${u.telegram_id}">✅ Одобрить</button>
          <button class="btn secondary small" data-reject="${u.telegram_id}">❌ Отклонить</button>
          ${u.has_unrefunded_payment ? `<button class="btn secondary small" data-reject-refund="${u.telegram_id}">💸 С возвратом</button>` : ""}
        </div>
      </div>
    `).join("");
    screenRoot.querySelectorAll("[data-approve]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await apiPost(`/admin/requests/${btn.dataset.approve}/approve`, {});
          toast("✅ Одобрено");
          renderAdminRequests();
        } catch (err) { toast("Ошибка: " + err.message); }
      });
    });
    screenRoot.querySelectorAll("[data-reject]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await apiPost(`/admin/requests/${btn.dataset.reject}/reject`, { refund: false });
          toast("❌ Отклонено");
          renderAdminRequests();
        } catch (err) { toast("Ошибка: " + err.message); }
      });
    });
    screenRoot.querySelectorAll("[data-reject-refund]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await apiPost(`/admin/requests/${btn.dataset.rejectRefund}/reject`, { refund: true });
          toast("💸 Отклонено, возврат выполнен");
          renderAdminRequests();
        } catch (err) { toast("Ошибка: " + err.message); }
      });
    });
  } catch (err) {
    renderError(err, renderAdminRequests);
  }
}

// --- Points admin -----------------------------------------------------------

SCREENS.adminPoints = renderAdminPoints;
async function renderAdminPoints() {
  setHeader("Точки", "", true);
  loading();
  try {
    const data = await apiGet("/admin/points");
    screenRoot.innerHTML = `
      <div class="chat-actions">
        <button class="btn secondary small" id="syncBtn">🗺 Синк с Avito</button>
        <button class="btn secondary small" id="conflictsBtn">🔍 Проверка близких точек</button>
        <button class="btn secondary small" id="unassignedBtn">📭 Чаты без точки</button>
        <button class="btn secondary small" id="bulkBtn">📥 Массовый импорт</button>
      </div>
      <div id="pointsReport"></div>
      ${data.points.map(p => `
        <button class="list-btn" data-point="${p.id}">
          <div class="row-top">
            <span class="name">${p.is_active ? "🟢" : "🔴"} ${esc(p.name)}</span>
          </div>
          ${p.address ? `<div class="preview">${esc(p.address)}</div>` : ""}
        </button>
      `).join("")}
    `;
    screenRoot.querySelectorAll("[data-point]").forEach(btn => {
      btn.addEventListener("click", () => go("adminPointEdit", { pointId: btn.dataset.point }));
    });
    document.getElementById("syncBtn").addEventListener("click", async () => {
      const report = document.getElementById("pointsReport");
      report.innerHTML = '<div class="spinner"></div>';
      try {
        const res = await apiPost("/admin/points/sync", {});
        report.innerHTML = `<div class="card" style="font-size:12.5px;white-space:pre-wrap">${esc(res.report.join("\n"))}\n\nВсего точек: ${res.total_points}</div>`;
      } catch (err) { toast("Ошибка: " + err.message); }
    });
    document.getElementById("conflictsBtn").addEventListener("click", async () => {
      const report = document.getElementById("pointsReport");
      report.innerHTML = '<div class="spinner"></div>';
      try {
        const res = await apiGet("/admin/points/conflicts");
        report.innerHTML = res.conflicts.length
          ? `<div class="card" style="font-size:12.5px">` + res.conflicts.map(c => `⚠️ «${esc(c.point_a)}» ↔ «${esc(c.point_b)}»: ${c.distance_m} м`).join("<br>") + `</div>`
          : `<div class="card" style="font-size:12.5px">Близких точек не найдено.</div>`;
      } catch (err) { toast("Ошибка: " + err.message); }
    });
    document.getElementById("unassignedBtn").addEventListener("click", async () => {
      const report = document.getElementById("pointsReport");
      report.innerHTML = '<div class="spinner"></div>';
      try {
        const res = await apiGet("/admin/points/unassigned");
        if (!res.chats.length) { report.innerHTML = '<div class="card" style="font-size:12.5px">Все чаты привязаны.</div>'; return; }
        report.innerHTML = res.chats.map(c => `
          <button class="list-btn" data-chat="${c.short_id}"><span class="name">📭 ${esc(c.client_name || "Клиент")}</span></button>
          <div id="reassignBox-${c.short_id}"></div>
        `).join("");
        report.querySelectorAll("[data-chat]").forEach(btn => {
          btn.addEventListener("click", async () => {
            const box = document.getElementById(`reassignBox-${btn.dataset.chat}`);
            const pid = await pickPointInline(data.points.filter(p => p.is_active), box);
            if (pid == null) return;
            try {
              await apiPost("/admin/points/reassign", { chat_short_id: btn.dataset.chat, point_id: pid });
              toast("✅ Переназначено");
              renderAdminPoints();
            } catch (err) { toast("Ошибка: " + err.message); }
          });
        });
      } catch (err) { toast("Ошибка: " + err.message); }
    });
    document.getElementById("bulkBtn").addEventListener("click", () => {
      const report = document.getElementById("pointsReport");
      report.innerHTML = `
        <div class="card field">
          <label>По одной точке на строку: КОД Адрес Часы</label>
          <textarea id="bulkText" rows="4" placeholder="ТКЧ Ростов-на-Дону ул. Текучева 141а 8:00-20:00"></textarea>
          <button class="btn block small" id="bulkSubmit">Импортировать</button>
        </div>
      `;
      document.getElementById("bulkSubmit").addEventListener("click", async () => {
        try {
          const res = await apiPost("/admin/points/bulk-import", { text: document.getElementById("bulkText").value });
          report.innerHTML = `<div class="card" style="font-size:12.5px">✅ Обновлено: ${res.updated.length}<br>${res.updated.join("<br>")}${res.not_found.length ? "<br><br>⚠️ Не найдено:<br>" + res.not_found.join("<br>") : ""}</div>`;
        } catch (err) { toast("Ошибка: " + err.message); }
      });
    });
  } catch (err) {
    renderError(err, renderAdminPoints);
  }
}

SCREENS.adminPointEdit = renderAdminPointEdit;
async function renderAdminPointEdit(params) {
  setHeader("Точка", "", true);
  loading();
  try {
    const data = await apiGet("/admin/points");
    const point = data.points.find(p => String(p.id) === String(params.pointId));
    if (!point) { screenRoot.innerHTML = '<div class="empty-state">Точка не найдена</div>'; return; }
    setHeader(point.name, "", true);
    screenRoot.innerHTML = `
      <div class="card field">
        <label>Название</label>
        <input type="text" id="ptName" value="${esc(point.name)}">
        <label>Код (для шаблонов/массового импорта)</label>
        <input type="text" id="ptCode" value="${esc(point.code || "")}">
        <label>Адрес</label>
        <input type="text" id="ptAddress" value="${esc(point.address || "")}">
        <label>Часы работы</label>
        <input type="text" id="ptHours" value="${esc(point.working_hours || "")}">
        <button class="btn block small" id="ptSave">💾 Сохранить</button>
      </div>
      <button class="btn ${point.is_active ? "secondary" : ""} block small" id="ptToggle" style="${point.is_active ? "border-color:var(--danger)" : ""}">
        ${point.is_active ? "🔴 Удалить (скрыть)" : "🟢 Активировать"}
      </button>
    `;
    document.getElementById("ptSave").addEventListener("click", async () => {
      try {
        await apiPatch(`/admin/points/${point.id}`, {
          name: document.getElementById("ptName").value,
          code: document.getElementById("ptCode").value,
          address: document.getElementById("ptAddress").value,
          working_hours: document.getElementById("ptHours").value,
        });
        toast("✅ Сохранено");
      } catch (err) { toast("Ошибка: " + err.message); }
    });
    document.getElementById("ptToggle").addEventListener("click", async () => {
      try {
        await apiPost(`/admin/points/${point.id}/toggle`, {});
        renderAdminPointEdit(params);
      } catch (err) { toast("Ошибка: " + err.message); }
    });
  } catch (err) {
    renderError(err, () => renderAdminPointEdit(params));
  }
}

// --- Avito accounts -----------------------------------------------------

SCREENS.adminAvito = renderAdminAvito;
async function renderAdminAvito() {
  setHeader("Avito API", "", true);
  loading();
  try {
    const data = await apiGet("/admin/avito-accounts");
    screenRoot.innerHTML = `
      ${data.accounts.map(a => `
        <div class="card card-row">
          <span>${a.is_active ? "🟢" : "🔴"} ${esc(a.name)}${a.last_poll_error ? " ⚠️" : ""}</span>
          <button class="btn secondary small" data-toggle="${a.id}">${a.is_active ? "Выключить" : "Включить"}</button>
        </div>
      `).join("")}
      <div class="card field">
        <label>Название (для себя)</label>
        <input type="text" id="accName">
        <label>client_id</label>
        <input type="text" id="accClientId">
        <label>client_secret</label>
        <input type="text" id="accClientSecret">
        <button class="btn block small" id="accSubmit">➕ Добавить аккаунт</button>
      </div>
    `;
    screenRoot.querySelectorAll("[data-toggle]").forEach(btn => {
      btn.addEventListener("click", async () => {
        try {
          await apiPost(`/admin/avito-accounts/${btn.dataset.toggle}/toggle`, {});
          renderAdminAvito();
        } catch (err) { toast("Ошибка: " + err.message); }
      });
    });
    document.getElementById("accSubmit").addEventListener("click", async () => {
      try {
        await apiPost("/admin/avito-accounts", {
          name: document.getElementById("accName").value,
          client_id: document.getElementById("accClientId").value,
          client_secret: document.getElementById("accClientSecret").value,
        });
        toast("✅ Аккаунт добавлен");
        renderAdminAvito();
      } catch (err) { toast("Ошибка: " + err.message); }
    });
  } catch (err) {
    renderError(err, renderAdminAvito);
  }
}

// --- AI config -----------------------------------------------------------

SCREENS.adminAI = renderAdminAI;
async function renderAdminAI() {
  setHeader("Настройки ИИ", "", true);
  loading();
  try {
    const cfg = await apiGet("/admin/ai-config");
    screenRoot.innerHTML = `
      <div class="card field">
        <label>base_url</label>
        <input type="text" id="aiBaseUrl" value="${esc(cfg.base_url)}">
        <label>model</label>
        <input type="text" id="aiModel" value="${esc(cfg.model)}">
        <label>api_key ${cfg.has_api_key ? "(установлен, оставьте пустым чтобы не менять)" : "(не задан)"}</label>
        <input type="text" id="aiApiKey" placeholder="sk-...">
        <button class="btn block small" id="aiSave">💾 Сохранить</button>
      </div>
      <button class="btn ${cfg.is_enabled ? "secondary" : ""} block small" id="aiToggle">${cfg.is_enabled ? "🔴 Выключить" : "🟢 Включить"}</button>
    `;
    document.getElementById("aiSave").addEventListener("click", async () => {
      const body = { base_url: document.getElementById("aiBaseUrl").value, model: document.getElementById("aiModel").value };
      const key = document.getElementById("aiApiKey").value.trim();
      if (key) body.api_key = key;
      try {
        await apiPatch("/admin/ai-config", body);
        toast("✅ Сохранено");
      } catch (err) { toast("Ошибка: " + err.message); }
    });
    document.getElementById("aiToggle").addEventListener("click", async () => {
      try {
        await apiPatch("/admin/ai-config", { is_enabled: !cfg.is_enabled });
        renderAdminAI();
      } catch (err) { toast("Ошибка: " + err.message); }
    });
  } catch (err) {
    renderError(err, renderAdminAI);
  }
}

// --- Proxy -----------------------------------------------------------------

SCREENS.adminProxy = renderAdminProxy;
async function renderAdminProxy() {
  setHeader("Прокси", "", true);
  loading();
  try {
    const cfg = await apiGet("/admin/proxy-config");
    screenRoot.innerHTML = `
      <div class="card" style="font-size:12.5px;color:var(--text-dim)">
        ⚠️ Сохранение прокси перезапускает бота (~3 секунды простоя).
      </div>
      <div class="card field">
        <label>URL (http://... или socks5://...)</label>
        <input type="text" id="proxyUrl" value="${esc(cfg.proxy_url || "")}">
        <button class="btn block small" id="proxySave">💾 Сохранить и перезапустить</button>
      </div>
      <button class="btn ${cfg.is_enabled ? "secondary" : ""} block small" id="proxyToggle">${cfg.is_enabled ? "🔴 Выключить" : "🟢 Включить"}</button>
    `;
    document.getElementById("proxySave").addEventListener("click", async () => {
      try {
        await apiPatch("/admin/proxy-config", { proxy_url: document.getElementById("proxyUrl").value, is_enabled: true });
        toast("⚠️ Сохранено, бот перезапускается…");
      } catch (err) { toast("Ошибка: " + err.message); }
    });
    document.getElementById("proxyToggle").addEventListener("click", async () => {
      try {
        await apiPatch("/admin/proxy-config", { is_enabled: !cfg.is_enabled });
        toast("⚠️ Сохранено, бот перезапускается…");
      } catch (err) { toast("Ошибка: " + err.message); }
    });
  } catch (err) {
    renderError(err, renderAdminProxy);
  }
}

// --- Payment / welcome / backup ---------------------------------------------

SCREENS.adminPayment = renderAdminPayment;
async function renderAdminPayment() {
  setHeader("Платный доступ", "", true);
  loading();
  try {
    const cfg = await apiGet("/admin/payment-config");
    screenRoot.innerHTML = `
      <div class="card field">
        <label>Сумма (⭐ Stars)</label>
        <input type="number" id="paymentAmount" value="${cfg.amount_stars}">
        <button class="btn block small" id="paymentSave">💾 Сохранить сумму</button>
      </div>
      <button class="btn ${cfg.is_enabled ? "secondary" : ""} block small" id="paymentToggle">${cfg.is_enabled ? "🔴 Выключить" : "🟢 Включить"}</button>
    `;
    document.getElementById("paymentSave").addEventListener("click", async () => {
      try {
        await apiPatch("/admin/payment-config", { amount_stars: Number(document.getElementById("paymentAmount").value) });
        toast("✅ Сохранено");
      } catch (err) { toast("Ошибка: " + err.message); }
    });
    document.getElementById("paymentToggle").addEventListener("click", async () => {
      try {
        await apiPatch("/admin/payment-config", { is_enabled: !cfg.is_enabled });
        renderAdminPayment();
      } catch (err) { toast("Ошибка: " + err.message); }
    });
  } catch (err) {
    renderError(err, renderAdminPayment);
  }
}

SCREENS.adminWelcome = renderAdminWelcome;
async function renderAdminWelcome() {
  setHeader("Приветственное сообщение", "", true);
  loading();
  try {
    const data = await apiGet("/admin/welcome");
    screenRoot.innerHTML = `
      <div class="card field">
        <label>Текст</label>
        <textarea id="welcomeText" rows="5">${esc(data.text)}</textarea>
        <button class="btn block small" id="welcomeSave">💾 Сохранить</button>
      </div>
    `;
    document.getElementById("welcomeSave").addEventListener("click", async () => {
      try {
        await apiPatch("/admin/welcome", { text: document.getElementById("welcomeText").value });
        toast("✅ Сохранено");
      } catch (err) { toast("Ошибка: " + err.message); }
    });
  } catch (err) {
    renderError(err, renderAdminWelcome);
  }
}

SCREENS.adminBackup = renderAdminBackup;
async function renderAdminBackup() {
  setHeader("Резервные копии", "", true);
  loading();
  try {
    const cfg = await apiGet("/admin/backup-config");
    screenRoot.innerHTML = `
      <div class="card">
        <div class="card-row"><span>Последняя</span><span>${cfg.last_backup_at ? esc(cfg.last_backup_at) : "ещё не было"}</span></div>
      </div>
      <div class="card field">
        <label>Периодичность (часы)</label>
        <input type="number" id="backupInterval" value="${cfg.interval_hours}">
        <button class="btn block small" id="backupIntervalSave">💾 Сохранить</button>
      </div>
      <button class="btn ${cfg.is_enabled ? "secondary" : ""} block small" id="backupToggle">${cfg.is_enabled ? "🔴 Выключить" : "🟢 Включить"}</button>
      <button class="btn block small" id="backupNow">📤 Сделать бэкап сейчас</button>
    `;
    document.getElementById("backupIntervalSave").addEventListener("click", async () => {
      try {
        await apiPatch("/admin/backup-config", { interval_hours: Number(document.getElementById("backupInterval").value) });
        toast("✅ Сохранено");
      } catch (err) { toast("Ошибка: " + err.message); }
    });
    document.getElementById("backupToggle").addEventListener("click", async () => {
      try {
        await apiPatch("/admin/backup-config", { is_enabled: !cfg.is_enabled });
        renderAdminBackup();
      } catch (err) { toast("Ошибка: " + err.message); }
    });
    document.getElementById("backupNow").addEventListener("click", async (e) => {
      e.target.disabled = true;
      toast("Бэкап запускается, файл придёт вам в бота…");
      try {
        await apiPost("/admin/backup/run", {});
        toast("✅ Бэкап отправлен в бота");
      } catch (err) {
        toast("Ошибка: " + err.message);
      } finally {
        e.target.disabled = false;
      }
    });
  } catch (err) {
    renderError(err, renderAdminBackup);
  }
}

// --- Reviews -----------------------------------------------------------

SCREENS.adminReviews = renderAdminReviews;
async function renderAdminReviews() {
  setHeader("Отзывы Avito", "", true);
  loading();
  try {
    const data = await apiGet("/admin/reviews");
    screenRoot.innerHTML = `
      <div class="card card-row">
        <span>⭐ Рейтинг</span><span>${data.score ?? "—"} (${data.reviews_count} отзывов)</span>
      </div>
      ${data.reviews.map(r => `
        <div class="card">
          <div class="card-row"><span>${esc(r.sender_name)}</span><span>${"⭐".repeat(r.score)}</span></div>
          ${r.item_title ? `<div class="preview">📦 ${esc(r.item_title)}</div>` : ""}
          <div style="font-size:13px">${esc(r.text)}</div>
          ${r.answer ? `<div class="preview">↳ Ваш ответ: ${esc(r.answer)}</div>` : (r.can_answer ? `<button class="btn secondary small" data-answer="${r.id}" style="margin-top:8px">✍️ Ответить</button><div id="ansForm-${r.id}"></div>` : "")}
        </div>
      `).join("")}
    `;
    screenRoot.querySelectorAll("[data-answer]").forEach(btn => {
      btn.addEventListener("click", () => {
        const box = document.getElementById(`ansForm-${btn.dataset.answer}`);
        box.innerHTML = `
          <div class="field" style="margin-top:8px">
            <input type="text" id="ansText-${btn.dataset.answer}" placeholder="Текст ответа">
            <button class="btn block small" id="ansSubmit-${btn.dataset.answer}">Отправить</button>
          </div>
        `;
        document.getElementById(`ansSubmit-${btn.dataset.answer}`).addEventListener("click", async () => {
          try {
            await apiPost(`/admin/reviews/${btn.dataset.answer}/answer`, {
              account_id: data.account_id, text: document.getElementById(`ansText-${btn.dataset.answer}`).value,
            });
            toast("✅ Ответ отправлен");
            renderAdminReviews();
          } catch (err) { toast("Ошибка: " + err.message); }
        });
      });
    });
  } catch (err) {
    renderError(err, renderAdminReviews);
  }
}

// --- Broadcast -----------------------------------------------------------

SCREENS.adminBroadcast = renderAdminBroadcast;
async function renderAdminBroadcast() {
  setHeader("Сообщение всем", "", true);
  screenRoot.innerHTML = `
    <div class="card field">
      <label>Текст рассылки</label>
      <textarea id="bcText" rows="5" placeholder="Текст сообщения…"></textarea>
      <label>Фото (необязательно)</label>
      <input type="file" id="bcPhoto" accept="image/*">
      <button class="btn block" id="bcSend">📢 Отправить всем</button>
    </div>
  `;
  document.getElementById("bcSend").addEventListener("click", async (e) => {
    const text = document.getElementById("bcText").value.trim();
    if (!text) { toast("Введите текст"); return; }
    e.target.disabled = true;
    toast("Рассылаю…");
    try {
      const file = document.getElementById("bcPhoto").files[0];
      let res;
      if (file) {
        const form = new FormData();
        form.append("text", text);
        form.append("photo", file, file.name);
        res = await apiUpload("/admin/broadcast", form);
      } else {
        res = await apiPost("/admin/broadcast", { text });
      }
      toast(`✅ Доставлено: ${res.sent}, недоступны: ${res.failed}`);
    } catch (err) {
      toast("Ошибка: " + err.message);
    } finally {
      e.target.disabled = false;
    }
  });
}

// ============================================================================
// Boot
// ============================================================================

async function boot() {
  const minSplash = new Promise(r => setTimeout(r, 3200));
  await Promise.all([renderHome(), minSplash]);
  const splash = document.getElementById("splash");
  splash.classList.add("fade-out");
  setTimeout(() => splash.remove(), 700);
}

boot();
