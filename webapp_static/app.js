"use strict";

const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
if (tg) {
  tg.ready();
  tg.expand();
  // Kept in step with --bg-top / --bg-bottom in style.css so Telegram's own
  // header does not sit as a dark band above a light app.
  try { tg.setHeaderColor("#bcbcbe"); } catch (e) {}
  try { tg.setBackgroundColor("#a6a6a9"); } catch (e) {}
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

// esc() goes through textContent, which leaves quotes intact — fine inside
// an element, unsafe inside an attribute value. Use this one there.
function escAttr(s) {
  return (s == null ? "" : String(s)).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
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
// Left rail (primary navigation)
// ============================================================================

// Line icons rather than emoji: emoji render in each platform's own colors
// and instantly break a monochrome design (and look different on iOS,
// Android and desktop). These inherit currentColor, so the same markup
// works on the dark rail and on the light tiles.
function svg(body) {
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ' +
         'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + body + '</svg>';
}

const ICONS = {
  home: svg('<path d="M3 10.5 12 3l9 7.5"/><path d="M5.5 9.4V21h13V9.4"/>'),
  inbox: svg('<path d="M4 5h16l2 8v6H2v-6z"/><path d="M2 13h5l2 3h6l2-3h5"/>'),
  clock: svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5.2l3.2 1.9"/>'),
  pin: svg('<path d="M12 21s7-6.2 7-11a7 7 0 1 0-14 0c0 4.8 7 11 7 11z"/><circle cx="12" cy="10" r="2.4"/>'),
  box: svg('<path d="M21 7.8 12 3 3 7.8v8.4L12 21l9-4.8z"/><path d="M3 7.8 12 12.6l9-4.8"/><path d="M12 12.6V21"/>'),
  user: svg('<circle cx="12" cy="8" r="3.6"/><path d="M4.6 20a7.4 7.4 0 0 1 14.8 0"/>'),
  list: svg('<path d="M8.5 6H20M8.5 12H20M8.5 18H20"/><path d="M4 6h.01M4 12h.01M4 18h.01"/>'),
  // Sliders rather than a cog: at 21px a cog's teeth turn to mush, and the
  // simplified outline that survives reads as a sun instead.
  gear: svg('<path d="M4 6h9M19 6h1M4 12h3M13 12h7M4 18h9M19 18h1"/><circle cx="16" cy="6" r="2.2"/><circle cx="10" cy="12" r="2.2"/><circle cx="16" cy="18" r="2.2"/>'),
  briefcase: svg('<rect x="3" y="7.5" width="18" height="12" rx="2.5"/><path d="M9 7.5V6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v1.5"/><path d="M3 12.5h18"/>'),
  moon: svg('<path d="M20 13.4A8.5 8.5 0 1 1 10.6 4a6.8 6.8 0 0 0 9.4 9.4z"/>'),

  // Actions
  check: svg('<path d="M20 6.5 9.5 17.5 4 12"/>'),
  x: svg('<path d="M6 6l12 12M18 6 6 18"/>'),
  refresh: svg('<path d="M20.4 12a8.4 8.4 0 1 1-2.5-6"/><path d="M20.5 4.2v5h-5"/>'),
  trash: svg('<path d="M4 7h16"/><path d="M9.5 7V5.3A1.3 1.3 0 0 1 10.8 4h2.4a1.3 1.3 0 0 1 1.3 1.3V7"/><path d="M6.6 7l.8 12.1A1.5 1.5 0 0 0 8.9 20.5h6.2a1.5 1.5 0 0 0 1.5-1.4L17.4 7"/>'),
  plus: svg('<path d="M12 5v14M5 12h14"/>'),
  send: svg('<path d="M4.6 11.9 20 4.4 15.4 20l-4-6.6z"/><path d="M11.4 13.4 20 4.4"/>'),
  save: svg('<path d="M4.5 4.5h11l4 4v11h-15z"/><path d="M8 4.5v5h6.5v-5"/><path d="M8 13h8v6.5H8z"/>'),
  upload: svg('<path d="M12 16.2V4.4"/><path d="M7.6 8.8 12 4.4l4.4 4.4"/><path d="M4.6 15v4.6h14.8V15"/>'),
  download: svg('<path d="M12 4.4v11.8"/><path d="M7.6 11.8 12 16.2l4.4-4.4"/><path d="M4.6 15v4.6h14.8V15"/>'),
  search: svg('<circle cx="11" cy="11" r="6.4"/><path d="m15.8 15.8 4.6 4.6"/>'),
  map: svg('<path d="M9 4.4 3.6 6.7v12.9L9 17.3l6 2.3 5.4-2.3V4.4L15 6.7z"/><path d="M9 4.4v12.9M15 6.7v12.9"/>'),
  unlock: svg('<rect x="4.6" y="10.4" width="14.8" height="9.6" rx="2.2"/><path d="M8 10.4V7.3A4 4 0 0 1 15.6 6"/>'),
  ban: svg('<circle cx="12" cy="12" r="8.4"/><path d="m6.1 6.1 11.8 11.8"/>'),
  refund: svg('<path d="M4.2 9.4h11.4a4.6 4.6 0 0 1 0 9.2H9"/><path d="M7.6 6 4.2 9.4 7.6 12.8"/>'),

  // Objects
  ai: svg('<path d="M12 3.4 13.7 9l5.6 1.7-5.6 1.7L12 18l-1.7-5.6L4.7 10.7 10.3 9z"/><path d="m18.4 15.4.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z"/>'),
  doc: svg('<path d="M6 3.6h7.4L19 9.2v11.2H6z"/><path d="M13.4 3.6v5.6H19"/><path d="M9 13.2h7M9 16.6h5"/>'),
  camera: svg('<path d="M3 8.6h3.6L8 6h8l1.4 2.6H21v10.9H3z"/><circle cx="12" cy="13.6" r="3.4"/>'),
  clip: svg('<path d="m17.4 10.6-6.5 6.5a3.5 3.5 0 0 1-5-5l7-7a2.5 2.5 0 0 1 3.6 3.5l-7 7a1.5 1.5 0 0 1-2.1-2.1l6.4-6.4"/>'),
  tag: svg('<path d="M3.6 11.4V4h7.4l9 9a1.7 1.7 0 0 1 0 2.4l-5 5a1.7 1.7 0 0 1-2.4 0z"/><circle cx="7.4" cy="7.9" r="1.3"/>'),
  star: svg('<path d="m12 4 2.5 5.3 5.6.8-4.1 3.9 1 5.6L12 17l-5 2.6 1-5.6L3.9 10l5.6-.8z"/>'),
  chat: svg('<path d="M20.4 11.9c0 4-3.8 7.3-8.4 7.3-1 0-2-.1-2.9-.4L4 20.4l1.4-3.6a7 7 0 0 1-1.8-4.9c0-4 3.8-7.3 8.4-7.3s8.4 3.3 8.4 7.3z"/>'),
  lead: svg('<path d="M12 3.5 13.6 9 19 10.6 13.6 12.2 12 17.6 10.4 12.2 5 10.6 10.4 9z"/>'),
  users: svg('<circle cx="9.4" cy="8" r="3.3"/><path d="M3 20a6.4 6.4 0 0 1 12.8 0"/><path d="M16 5.2a3.3 3.3 0 0 1 0 6.4"/><path d="M18.2 20a6.5 6.5 0 0 0-1.7-4.4"/>'),
  megaphone: svg('<path d="M4 10v4h3l7 4V6l-7 4z"/><path d="M17.4 9.2a4 4 0 0 1 0 5.6"/>'),
  building: svg('<path d="M4 20.4V5.6A1.6 1.6 0 0 1 5.6 4h6.8A1.6 1.6 0 0 1 14 5.6v14.8"/><path d="M14 10h4.4A1.6 1.6 0 0 1 20 11.6v8.8"/><path d="M2.6 20.4h18.8"/><path d="M7 8h4M7 12h4M7 16h4"/>'),
  key: svg('<circle cx="7.8" cy="12" r="3.9"/><path d="M11.7 12h9"/><path d="M17.4 12v3.2M20.4 12v2.2"/>'),
  globe: svg('<circle cx="12" cy="12" r="8.4"/><path d="M3.6 12h16.8"/><path d="M12 3.6a13 13 0 0 1 0 16.8 13 13 0 0 1 0-16.8z"/>'),
  mail: svg('<rect x="3.2" y="5.6" width="17.6" height="12.8" rx="2.2"/><path d="m3.8 6.9 8.2 5.8 8.2-5.8"/>'),
  // A pin struck through: a chat we could not tie to any branch.
  pinOff: svg('<path d="M12 20.8s6.8-6.1 6.8-10.8a6.8 6.8 0 0 0-9.8-6.1"/><path d="M5.4 7.3A6.8 6.8 0 0 0 5.2 10c0 4.7 6.8 10.8 6.8 10.8"/><path d="m4 4 16 16"/>'),
  shield: svg('<path d="M12 3.6 5.2 6v5.5c0 4.2 2.8 7.5 6.8 8.9 4-1.4 6.8-4.7 6.8-8.9V6z"/>'),
  crown: svg('<path d="M4 17.4 5.4 7l4.1 3.5L12 5l2.5 5.5L18.6 7 20 17.4z"/><path d="M4.4 20.2h15.2"/>'),
};

// A status dot beats a 🟢/🔴 emoji here: emoji circles are rendered by the
// platform's own font, so they arrive at different sizes and shades on iOS,
// Android and desktop and never line up with the text next to them.
function dot(on) { return `<span class="dot ${on ? "on" : "off"}"></span>`; }

// id is what the rail highlights on. "chats" needs two entries with the
// same screen and different params, hence an explicit id rather than the
// screen name alone.
function navItems(me) {
  const items = [
    { id: "home", screen: "home", params: {}, icon: "home", label: "Главная" },
    { id: "chats:unread", screen: "chats", params: { filter: "unread" }, icon: "inbox", label: "Непрочитанные", badge: true },
    { id: "chats:recent", screen: "chats", params: { filter: "recent" }, icon: "clock", label: "Недавние" },
    { id: "points", screen: "points", params: {}, icon: "pin", label: "Мои точки" },
    { id: "orders", screen: "orders", params: {}, icon: "box", label: "Заказы Avito" },
    { id: "profile", screen: "profile", params: {}, icon: "user", label: "Мой профиль" },
  ];
  if (me && me.is_manager_or_above) {
    items.push({ id: "myTemplates", screen: "myTemplates", params: {}, icon: "list", label: "Мои шаблоны" });
  }
  if (me && me.is_admin) {
    // Both pinned to the bottom of the rail, away from everyday work.
    items.push({ id: "leadershipHome", screen: "leadershipHome", params: {}, icon: "shield", label: "Меню руководителя", bottom: true });
  }
  if (me && me.is_director) {
    items.push({ id: "adminHome", screen: "adminHome", params: {}, icon: "gear", label: "Настройки" });
  }
  return items;
}

// Kept in step with the rows in renderAdminHome, and with _require_director
// on the server: these are the director-only settings screens, everything
// else under admin* is leadership work open to a РОП.
const SETTINGS_SCREENS = [
  "adminHome", "adminPoints", "adminPointEdit", "adminAvito", "adminAI",
  "adminProxy", "adminPayment", "adminWelcome", "adminBackup",
];

// Drill-down screens keep their parent section lit rather than clearing the
// rail — opening one chat is still "being in" the chat list.
function navIdFor(screen, params) {
  if (screen === "chats") return "chats:" + ((params && params.filter) || "unread");
  if (screen === "chatDetail") return null;      // keep whichever chats entry is lit
  if (screen === "orderDetail") return "orders";
  // Settings screens belong to the director's menu; every other admin
  // screen is leadership work and lights that entry instead.
  if (SETTINGS_SCREENS.indexOf(screen) >= 0) return "adminHome";
  if (screen.indexOf("admin") === 0) return "leadershipHome";
  return screen;
}

const railEl = document.getElementById("rail");
let railUnread = 0;

function buildRail() {
  const items = navItems(state.me);
  let html = "";
  let hotkey = 0;
  items.forEach(item => {
    hotkey += 1;
    if (item.bottom) html += '<div class="rail-spacer"></div><div class="rail-sep"></div>';
    const badge = item.badge && railUnread
      ? `<span class="rail-badge">${railUnread > 99 ? "99+" : railUnread}</span>` : "";
    html += `<button class="rail-btn" data-nav="${item.id}" data-hotkey="${hotkey}"
      title="${escAttr(item.label)} (${hotkey})" aria-label="${escAttr(item.label)}">${ICONS[item.icon]}${badge}</button>`;
  });
  railEl.innerHTML = html;
  railEl.querySelectorAll("[data-nav]").forEach(btn => {
    const item = items.find(i => i.id === btn.dataset.nav);
    btn.addEventListener("click", () => navTo(item));
  });
  markRailActive(navIdFor(currentScreen, currentParams));
}

function markRailActive(navId) {
  if (!navId) return;
  railEl.querySelectorAll(".rail-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.nav === navId);
  });
}

// Rail entries are top-level destinations, so they reset the trail instead
// of stacking onto it — otherwise Back walks through every section the user
// tapped on the way here rather than returning to the home screen.
function navTo(item) {
  if (!item) return;
  if (tg && tg.HapticFeedback) { try { tg.HapticFeedback.selectionChanged(); } catch (e) {} }
  state.history = item.screen === "home" ? [] : [{ screen: "home", params: {} }];
  render(item.screen, Object.assign({}, item.params));
}

// Hotkeys — Telegram Desktop has a real keyboard, and the rail doubles as
// the legend for them (each button's tooltip carries its digit).
document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const el = document.activeElement;
  if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
  if (e.key === "Escape") { backBtn.click(); return; }
  if (!/^[1-9]$/.test(e.key)) return;
  const btn = railEl.querySelector(`[data-hotkey="${e.key}"]`);
  if (btn) { e.preventDefault(); btn.click(); }
});

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
  markRailActive(navIdFor(screen, currentParams));
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

    // The rail is built here rather than at boot: which entries it has
    // depends on the role, and /me is what tells us the role.
    railUnread = unreadCount || 0;
    buildRail();

    headerTitle.textContent = me.full_name || "GUDDA CRM";
    headerSubtitle.textContent = me.role_label + (me.points.length ? " · " + me.points.map(p => p.name).join(", ") : "");

    screenRoot.innerHTML = `
      <div class="tiles">
        <button class="tile wide ${me.on_shift ? "shift-on" : "shift-off"}" id="shiftTile">
          <span class="tile-icon">${me.on_shift ? ICONS.briefcase : ICONS.moon}</span>
          <span class="tile-label">${me.on_shift ? "Вы на смене — нажмите, чтобы уйти отдыхать" : "Вы отдыхаете — нажмите, чтобы выйти на смену"}</span>
        </button>
        <button class="tile" data-go="chats" data-filter="unread">
          <span class="tile-icon">${ICONS.inbox}</span>
          <span class="tile-label">Непрочитанные</span>
          ${unreadCount ? `<span class="tile-badge">${unreadCount}</span>` : ""}
        </button>
        <button class="tile" data-go="chats" data-filter="recent">
          <span class="tile-icon">${ICONS.clock}</span>
          <span class="tile-label">Недавние</span>
        </button>
        <button class="tile" data-go="points">
          <span class="tile-icon">${ICONS.pin}</span>
          <span class="tile-label">Мои точки</span>
        </button>
        <button class="tile" data-go="orders">
          <span class="tile-icon">${ICONS.box}</span>
          <span class="tile-label">Заказы Avito</span>
        </button>
        <button class="tile" data-go="profile">
          <span class="tile-icon">${ICONS.user}</span>
          <span class="tile-label">Мой профиль</span>
        </button>
        ${me.is_manager_or_above ? `
        <button class="tile" data-go="myTemplates">
          <span class="tile-icon">${ICONS.list}</span>
          <span class="tile-label">Мои шаблоны</span>
        </button>` : ""}
        ${me.is_admin ? `
        <button class="tile" data-go="leadershipHome">
          <span class="tile-icon">${ICONS.shield}</span>
          <span class="tile-label">Меню руководителя</span>
        </button>` : ""}
        ${me.is_director ? `
        <button class="tile" data-go="adminHome">
          <span class="tile-icon">${ICONS.gear}</span>
          <span class="tile-label">Настройки</span>
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
          <span class="name">${c.is_new_lead ? ICONS.lead : ICONS.chat}${esc(c.client_name || "Клиент")}</span>
          ${c.unread_count ? `<span class="unread-dot">${c.unread_count}</span>` : ""}
        </div>
        ${c.item_title ? `<div class="preview">${esc(c.item_title)}</div>` : ""}
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
            <span>${ICONS.box} ${chat.item_url ? `<a href="${esc(chat.item_url)}" target="_blank" style="color:var(--accent-2)">${esc(chat.item_title)}</a>` : esc(chat.item_title)}</span>
          </div>
        </div>` : ""}
      <div class="messages" id="msgList">
        ${chat.messages.map(m => {
          // A photo the client sent renders as the picture itself; the
          // "📷 Фото" placeholder is only for images whose URL Avito
          // didn't give us.
          const photo = m.image_url
            ? `<img class="msg-photo" src="${escAttr(m.image_url)}" alt="Фото" loading="lazy">`
            : (m.has_image ? `<span class="msg-att">${ICONS.camera}Фото</span>` : "");
          // A bubble with neither text nor a picture is an attachment type
          // the parser didn't recognise — say so rather than rendering an
          // empty bubble (that blankness is what hid unparsed voice
          // messages until now).
          const body = esc(m.text) || (photo ? "" : `<span class="msg-att">${ICONS.clip}Вложение</span>`);
          return `<div class="msg ${m.direction}">${photo}${photo && body ? "<br>" : ""}${body}</div>`;
        }).join("")}
      </div>
      <div id="sentBanner"></div>
      <div class="chat-actions">
        <button class="btn secondary small" id="markReadBtn">${ICONS.check} Прочитано</button>
        <button class="btn secondary small" id="refreshBtn">${ICONS.refresh} Обновить</button>
        <button class="btn secondary small" id="aiBtn">${ICONS.ai} ИИ-ответ</button>
        <button class="btn secondary small" id="tplBtn">${ICONS.doc} Шаблоны</button>
      </div>
      <div id="assistPanel"></div>
      <div id="warnBanner"></div>
      <div class="reply-bar">
        <input type="file" id="photoInput" accept="image/*" multiple hidden>
        <button class="icon-btn" id="photoBtn" style="background:var(--card-bg);border:1px solid var(--card-border);color:var(--text)">${ICONS.camera}</button>
        <textarea id="replyText" rows="1" placeholder="Ответ клиенту…"></textarea>
        <button class="icon-btn" id="sendBtn">${ICONS.send}</button>
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
          `<button class="list-btn" data-tpl="${t.id}"><span class="name">${t.kind === "ai_prompt" ? ICONS.ai : ICONS.doc}${esc(t.title)}</span></button>`
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
          bubble.innerHTML = `<span class="msg-att">${ICONS.camera}Фото</span>`;
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
          <button class="btn secondary small" id="deleteSentBtn">${ICONS.trash} Удалить</button>
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
        <div class="card-row"><span>Товар</span><span>${
          order.item_url
            ? `<a href="#" id="orderItemLink" class="item-link">${esc((order.items || []).join(", "))}</a>`
            : esc((order.items || []).join(", "))
        }</span></div>
        ${order.total != null ? `<div class="card-row"><span>Сумма</span><span>${esc(order.total)}</span></div>` : ""}
        ${order.commission != null ? `<div class="card-row"><span>Комиссия</span><span>${esc(order.commission)}</span></div>` : ""}
        ${order.delivery_service ? `<div class="card-row"><span>Служба доставки</span><span>${esc(order.delivery_service)}</span></div>` : ""}
      </div>

      <div class="chat-actions">
        ${actionBtn("confirm", `${ICONS.check} Подтвердить`)}
        ${actionBtn("reject", `${ICONS.x} Отменить`, "secondary")}
        ${actionBtn("setMarkings", `${ICONS.tag} Маркировка`, "secondary")}
        ${actionBtn("setCNCDetails", `${ICONS.pin} Подготовить самовывоз`, "secondary")}
        ${order.delivery_type === "pvz" ? '<button class="btn secondary small" data-action="checkConfirmationCode">${ICONS.check} Код получения</button>' : ""}
        ${order.chat_short_id ? `<button class="btn secondary small" id="orderChatBtn">${ICONS.chat} Чат с покупателем</button>` : ""}
      </div>
      <div id="orderActionForm"></div>
    `;

    if (order.chat_short_id) {
      document.getElementById("orderChatBtn").addEventListener("click", () => go("chatDetail", { shortId: order.chat_short_id }));
    }

    if (order.item_url) {
      // A plain <a target="_blank"> does nothing inside Telegram's WebView —
      // opening an external page is what tg.openLink is for.
      document.getElementById("orderItemLink").addEventListener("click", (e) => {
        e.preventDefault();
        if (tg && tg.openLink) tg.openLink(order.item_url);
        else window.open(order.item_url, "_blank");
      });
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
        ${p.points.length ? `<div class="card-row" style="font-size:13px;justify-content:flex-start;gap:8px">${ICONS.pin}<span>${esc(p.points.join(", "))}</span></div>` : ""}
        <div class="card-row" style="font-size:13px;justify-content:flex-start;gap:8px">${p.on_shift ? ICONS.briefcase : ICONS.moon}<span>${p.on_shift ? "На смене" : "Отдыхает"}</span></div>
        <div class="card-row" style="margin-top:8px">
          <span>Рейтинг</span><span style="font-weight:700">${p.rating_points}</span>
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
    // A manager owns exactly one point and the server picks it. Admins and
    // directors have no point of their own, so they choose which point's
    // templates they're editing — without this they got an empty list and
    // an error on every create.
    const needsPointPicker = !!(state.me && state.me.is_admin);
    const qs = state.templatePointId ? `?point_id=${state.templatePointId}` : "";
    const data = await apiGet("/templates/mine" + qs);
    if (data.point_id) state.templatePointId = data.point_id;

    let pointBar = "";
    if (needsPointPicker) {
      const label = state.templatePointName
        ? esc(state.templatePointName)
        : (state.templatePointId ? `#${state.templatePointId}` : "не выбрана");
      pointBar = `
        <div class="card card-row">
          <span>Точка: <b>${label}</b></span>
          <button class="btn secondary small" id="tplPickPoint">Выбрать</button>
        </div>
        <div id="tplPointPicker"></div>`;
    }

    const canCreate = !needsPointPicker || !!state.templatePointId;
    screenRoot.innerHTML = `
      ${pointBar}
      ${canCreate ? `
      <div class="card field">
        <label>Тип</label>
        <select id="newTplKind" style="border-radius:14px;border:1px solid var(--card-border);background:rgba(255,255,255,0.06);color:var(--text);padding:11px 13px;font-size:14px">
          <option value="text">Текст</option>
          <option value="ai_prompt">AI-промпт</option>
        </select>
        <label>Заголовок</label>
        <input type="text" id="newTplTitle" placeholder="Например: Часы работы">
        <label>Текст / промпт</label>
        <input type="text" id="newTplBody" placeholder="!КОДВ — часы, !КОДА — адрес">
        <button class="btn block" id="newTplSubmit">${ICONS.plus} Создать шаблон</button>
      </div>` : '<div class="empty-state">Выберите точку, чтобы добавить шаблон</div>'}
      <div class="section-title">Существующие</div>
      ${data.templates.length ? data.templates.map(t => `
        <div class="card card-row">
          <span class="name">${t.kind === "ai_prompt" ? ICONS.ai : ICONS.doc}${esc(t.title)}</span>
          <button class="btn secondary small" data-del="${t.id}">${ICONS.trash}</button>
        </div>
      `).join("") : '<div class="empty-state">Шаблонов пока нет</div>'}
    `;

    if (needsPointPicker) {
      document.getElementById("tplPickPoint").addEventListener("click", async () => {
        try {
          const pts = (await apiGet("/points")).points || [];
          if (!pts.length) { toast("Точек нет"); return; }
          const chosen = await pickPointInline(pts, document.getElementById("tplPointPicker"));
          if (chosen == null) return;
          state.templatePointId = chosen;
          const hit = pts.find(p => p.id === chosen);
          state.templatePointName = hit ? hit.name : null;
          renderMyTemplates();
        } catch (err) { toast("Ошибка: " + err.message); }
      });
    }

    if (canCreate) {
    document.getElementById("newTplSubmit").addEventListener("click", async () => {
      const kind = document.getElementById("newTplKind").value;
      const title = document.getElementById("newTplTitle").value.trim();
      const body = document.getElementById("newTplBody").value.trim();
      if (!body) { toast("Введите текст шаблона"); return; }
      try {
        await apiPost("/templates/mine", { kind, title, body, point_id: state.templatePointId || undefined });
        toast("✅ Создано");
        renderMyTemplates();
      } catch (err) { toast("Ошибка: " + err.message); }
    });
    }
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

function renderMenuScreen(sections) {
  screenRoot.innerHTML = sections.map(([screen, icon, label]) =>
    `<button class="list-btn" data-go="${screen}"><span class="name">${ICONS[icon]}${esc(label)}</span></button>`
  ).join("");
  screenRoot.querySelectorAll("[data-go]").forEach(btn => {
    btn.addEventListener("click", () => go(btn.dataset.go, {}));
  });
}

// Leadership work (РОП and up) and system settings (director only) are two
// separate menus in the bot, and they are two separate screens here for the
// same reason: the settings screens hold Avito keys, the AI key, proxy
// credentials and full-database backups, and mixing them into one list that
// merely hides rows invites reaching the wrong one. The server enforces the
// same split independently — see _require_director in webapp.py.
SCREENS.leadershipHome = renderLeadershipHome;
async function renderLeadershipHome() {
  setHeader("Меню руководителя", state.me ? state.me.role_label : "", true);
  renderMenuScreen([
    ["adminUsers", "users", "Все пользователи"],
    ["adminOnshift", "clock", "Кто на смене"],
    ["adminRequests", "doc", "Заявки на вступление"],
    ["adminUnassigned", "pinOff", "Чаты без точки"],
    ["adminReviews", "star", "Отзывы Avito"],
    ["adminBroadcast", "megaphone", "Сообщение всем"],
  ]);
}

SCREENS.adminHome = renderAdminHome;
async function renderAdminHome() {
  setHeader("Настройки", "", true);
  renderMenuScreen([
    ["adminPoints", "building", "Точки"],
    ["adminAvito", "key", "Avito API"],
    ["adminAI", "ai", "Настройки ИИ"],
    ["adminProxy", "globe", "Прокси"],
    ["adminPayment", "star", "Платный доступ"],
    ["adminWelcome", "mail", "Приветственное сообщение"],
    ["adminBackup", "save", "Резервные копии"],
  ]);
}

// Chats Avito gave us no branch for. The API has always allowed any РОП to
// sort these out, but the only way in was a button inside the Точки screen,
// which is director-only — so in practice a РОП could not reach their own
// queue. It lives in the leadership menu now, like it does in the bot.
SCREENS.adminUnassigned = renderAdminUnassigned;
async function renderAdminUnassigned() {
  setHeader("Чаты без точки", "", true);
  loading();
  try {
    const [res, pointsData] = await Promise.all([
      apiGet("/admin/points/unassigned"),
      apiGet("/admin/points"),
    ]);
    if (!res.chats.length) {
      screenRoot.innerHTML = '<div class="empty-state">Все чаты привязаны к точкам.</div>';
      return;
    }
    const points = pointsData.points.filter(p => p.is_active);
    screenRoot.innerHTML = `
      <div class="section-title">Не удалось определить точку: ${res.chats.length}</div>
      ${res.chats.map(c => `
        <button class="list-btn" data-chat="${escAttr(c.short_id)}">
          <span class="name">${ICONS.pinOff}${esc(c.client_name || "Клиент")}</span>
          ${c.item_id ? `<div class="preview">Объявление ${esc(c.item_id)}</div>` : ""}
        </button>
        <div id="reassignBox-${escAttr(c.short_id)}"></div>
      `).join("")}
    `;
    screenRoot.querySelectorAll("[data-chat]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const box = document.getElementById(`reassignBox-${btn.dataset.chat}`);
        const pid = await pickPointInline(points, box);
        if (pid == null) return;
        try {
          await apiPost("/admin/points/reassign", { chat_short_id: btn.dataset.chat, point_id: pid });
          toast("Переназначено");
          renderAdminUnassigned();
        } catch (err) { toast("Ошибка: " + err.message); }
      });
    });
  } catch (err) {
    renderError(err, renderAdminUnassigned);
  }
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
          <span class="preview">${u.status !== "approved" ? "заблокирован" : ""}</span>
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
        <button class="btn block small" id="saveNameBtn">${ICONS.save} Сохранить</button>
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
          ? `<button class="btn small" id="unblockBtn">${ICONS.unlock} Разблокировать</button>`
          : `<button class="btn secondary small" id="blockBtn">${ICONS.ban} Уволить</button>`}
        <button class="btn secondary small" id="deleteBtn" style="border-color:var(--danger)">${ICONS.trash} Удалить аккаунт</button>
      </div>
    `;

    const pointsBox = document.getElementById("pointsBox");
    pointsBox.innerHTML = pointsData.points.map(p => `
      <label class="card-row">
        <span>${esc(p.name)}</span>
        <input type="checkbox" data-point-check="${p.id}" ${subscribedIds.has(p.id) ? "checked" : ""}>
      </label>
    `).join("") + '<button class="btn block small" id="savePointsBtn" style="margin-top:8px">${ICONS.save} Сохранить подписки</button>';

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
          <button class="btn small" id="pointPickerOk">${ICONS.check} Готово</button>
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
        <span class="name">${ICONS.user}${esc(u.full_name || u.username || u.telegram_id)}</span>
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
          <button class="btn small" data-approve="${u.telegram_id}">${ICONS.check} Одобрить</button>
          <button class="btn secondary small" data-reject="${u.telegram_id}">${ICONS.x} Отклонить</button>
          ${u.has_unrefunded_payment ? `<button class="btn secondary small" data-reject-refund="${u.telegram_id}">${ICONS.refund} С возвратом</button>` : ""}
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
        <button class="btn secondary small" id="syncBtn">${ICONS.map} Синк с Avito</button>
        <button class="btn secondary small" id="conflictsBtn">${ICONS.search} Проверка близких точек</button>
        <button class="btn secondary small" id="bulkBtn">${ICONS.download} Массовый импорт</button>
      </div>
      <div id="pointsReport"></div>
      ${data.points.map(p => `
        <button class="list-btn" data-point="${p.id}">
          <div class="row-top">
            <span class="name">${dot(p.is_active)}${esc(p.name)}</span>
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
        <button class="btn block small" id="ptSave">${ICONS.save} Сохранить</button>
      </div>
      <button class="btn ${point.is_active ? "secondary" : ""} block small" id="ptToggle" style="${point.is_active ? "border-color:var(--danger)" : ""}">
        ${point.is_active ? "Удалить (скрыть)" : "Активировать"}
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
          <span class="name">${dot(a.is_active)}${esc(a.name)}${a.last_poll_error ? " ⚠️" : ""}</span>
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
        <button class="btn block small" id="accSubmit">${ICONS.plus} Добавить аккаунт</button>
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
        <button class="btn block small" id="aiSave">${ICONS.save} Сохранить</button>
      </div>
      <button class="btn ${cfg.is_enabled ? "secondary" : ""} block small" id="aiToggle">${cfg.is_enabled ? "Выключить" : "Включить"}</button>
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
        <button class="btn block small" id="proxySave">${ICONS.save} Сохранить и перезапустить</button>
      </div>
      <button class="btn ${cfg.is_enabled ? "secondary" : ""} block small" id="proxyToggle">${cfg.is_enabled ? "Выключить" : "Включить"}</button>
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
        <label>Сумма (Stars)</label>
        <input type="number" id="paymentAmount" value="${cfg.amount_stars}">
        <button class="btn block small" id="paymentSave">${ICONS.save} Сохранить сумму</button>
      </div>
      <button class="btn ${cfg.is_enabled ? "secondary" : ""} block small" id="paymentToggle">${cfg.is_enabled ? "Выключить" : "Включить"}</button>
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
        <button class="btn block small" id="welcomeSave">${ICONS.save} Сохранить</button>
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
        <button class="btn block small" id="backupIntervalSave">${ICONS.save} Сохранить</button>
      </div>
      <button class="btn ${cfg.is_enabled ? "secondary" : ""} block small" id="backupToggle">${cfg.is_enabled ? "Выключить" : "Включить"}</button>
      <button class="btn block small" id="backupNow">${ICONS.upload} Сделать бэкап сейчас</button>
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
        <span>Рейтинг</span><span>${data.score ?? "—"} (${data.reviews_count} отзывов)</span>
      </div>
      ${data.reviews.map(r => `
        <div class="card">
          <div class="card-row"><span>${esc(r.sender_name)}</span><span class="stars">${ICONS.star.repeat(r.score)}</span></div>
          ${r.item_title ? `<div class="preview">${esc(r.item_title)}</div>` : ""}
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
      <button class="btn block" id="bcSend">${ICONS.megaphone} Отправить всем</button>
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
