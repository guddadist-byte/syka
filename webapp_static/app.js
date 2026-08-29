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
  opts.headers = Object.assign({
    "X-Telegram-Init-Data": INIT_DATA,
    "Content-Type": "application/json",
  }, options.headers || {});
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
  setHeader("GUDDA CRM", state.me ? state.me.role_label : "", false);
  loading();
  try {
    const me = await apiGet("/me");
    state.me = me;
    let unreadCount = "";
    try {
      const chats = await apiGet("/chats?filter=unread");
      if (chats.chats.length) unreadCount = chats.chats.length;
    } catch (e) {}

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
        <button class="tile wide" data-go="profile">
          <span class="tile-icon">👤</span>
          <span class="tile-label">Мой профиль</span>
        </button>
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
      <div class="chat-actions">
        <button class="btn secondary small" id="markReadBtn">✅ Прочитано</button>
        <button class="btn secondary small" id="refreshBtn">🔄 Обновить</button>
      </div>
      <div class="reply-bar">
        <textarea id="replyText" rows="1" placeholder="Ответ клиенту…"></textarea>
        <button class="icon-btn" id="sendBtn">➤</button>
      </div>
    `;

    const msgList = document.getElementById("msgList");
    msgList.scrollTop = msgList.scrollHeight;

    document.getElementById("markReadBtn").addEventListener("click", async () => {
      try {
        await apiPost(`/chats/${params.shortId}/read`, {});
        toast("Отмечено прочитанным");
        renderChatDetail(params);
      } catch (err) { toast("Ошибка: " + err.message); }
    });

    document.getElementById("refreshBtn").addEventListener("click", () => renderChatDetail(params));

    document.getElementById("sendBtn").addEventListener("click", async () => {
      const textarea = document.getElementById("replyText");
      const text = textarea.value.trim();
      if (!text) return;
      const btn = document.getElementById("sendBtn");
      btn.disabled = true;
      try {
        await apiPost(`/chats/${params.shortId}/reply`, { text });
        toast("✅ Отправлено");
        renderChatDetail(params);
      } catch (err) {
        toast("Ошибка отправки: " + err.message);
        btn.disabled = false;
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
      ${order.has_barcode ? `<img class="barcode-img" src="/api/orders/${params.accountId}/${params.orderId}/barcode.png" alt="barcode">` : ""}
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
// Boot
// ============================================================================

async function boot() {
  const minSplash = new Promise(r => setTimeout(r, 900));
  await Promise.all([renderHome(), minSplash]);
  const splash = document.getElementById("splash");
  splash.classList.add("fade-out");
  setTimeout(() => splash.remove(), 550);
}

boot();
