function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = atob(base64);
  return Uint8Array.from([...rawData].map((c) => c.charCodeAt(0)));
}

async function setupPushButton() {
  const btn = document.getElementById("push-toggle-btn");
  if (!btn) return;

  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    btn.textContent = "Push not supported on this browser";
    btn.disabled = true;
    return;
  }

  const registration = await navigator.serviceWorker.register("/sw.js");
  const existing = await registration.pushManager.getSubscription();
  updateButton(existing);

  btn.addEventListener("click", async () => {
    const current = await registration.pushManager.getSubscription();
    if (current) {
      await fetch("/push/unsubscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint: current.endpoint }),
      });
      await current.unsubscribe();
      updateButton(null);
      return;
    }

    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      alert("Notification permission was not granted.");
      return;
    }

    const keyResp = await fetch("/push/vapid-public-key");
    const { key } = await keyResp.json();
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key),
    });

    await fetch("/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(subscription.toJSON()),
    });
    updateButton(subscription);
  });

  function updateButton(subscription) {
    btn.textContent = subscription ? "🔕 Disable notifications" : "🔔 Enable notifications";
  }
}

function setupMenus() {
  const menus = [
    { btn: "settings-menu-btn", panel: "settings-menu-panel" },
    { btn: "profile-menu-btn", panel: "profile-menu-panel" },
  ];

  menus.forEach(({ btn, panel }) => {
    const btnEl = document.getElementById(btn);
    const panelEl = document.getElementById(panel);
    if (!btnEl || !panelEl) return;

    btnEl.addEventListener("click", (event) => {
      event.stopPropagation();
      const isOpen = panelEl.classList.contains("open");
      document.querySelectorAll(".menu-panel.open").forEach((p) => p.classList.remove("open"));
      if (!isOpen) panelEl.classList.add("open");
    });
    panelEl.addEventListener("click", (event) => event.stopPropagation());
  });

  document.addEventListener("click", () => {
    document.querySelectorAll(".menu-panel.open").forEach((p) => p.classList.remove("open"));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      document.querySelectorAll(".menu-panel.open").forEach((p) => p.classList.remove("open"));
    }
  });
}

const THEME_STORAGE_KEY = "abtracker-theme";

function applyTheme(choice) {
  if (choice === "dark" || choice === "light") {
    document.documentElement.setAttribute("data-theme", choice);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
  document.querySelectorAll(".theme-option").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.themeChoice === choice);
  });
}

function setupThemeToggle() {
  const buttons = document.querySelectorAll(".theme-option");
  if (!buttons.length) return;

  const stored = localStorage.getItem(THEME_STORAGE_KEY) || "system";
  applyTheme(stored);

  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const choice = btn.dataset.themeChoice;
      localStorage.setItem(THEME_STORAGE_KEY, choice);
      applyTheme(choice);
    });
  });
}

// The search filter and the "hide acknowledged" toggle both hide/show the
// same [data-series-name] elements (recently-released cards in particular
// are covered by both). Two independent handlers each setting el.style.display
// would fight each other — whichever ran last would win outright, ignoring
// the other's condition. Instead, both conditions are combined here: an
// element shows only if it passes the search AND isn't hidden-by-acknowledged.
let searchQuery = "";
let hideAcknowledged = true;

function applyCombinedVisibility() {
  document.querySelectorAll("[data-series-name]").forEach((el) => {
    const searchOk = !searchQuery || el.dataset.seriesName.includes(searchQuery);
    const ackOk = !(hideAcknowledged && el.dataset.acknowledged === "true");
    el.style.display = searchOk && ackOk ? "" : "none";
  });
  const hint = document.getElementById("recent-books-all-hidden-hint");
  const grid = document.getElementById("recent-books-grid");
  if (hint && grid) {
    const cards = grid.querySelectorAll(".card");
    const anyVisible = Array.from(cards).some((card) => card.style.display !== "none");
    if (cards.length && !anyVisible) {
      hint.textContent = searchQuery
        ? "No recently released books match your search."
        : "All caught up — every book in this window is acknowledged. Toggle “Hide acknowledged” off to see them.";
      hint.style.display = "";
    } else {
      hint.style.display = "none";
    }
  }
  const watchlistHint = document.getElementById("watchlist-all-hidden-hint");
  const watchlistWrap = document.getElementById("watchlist-table-wrap");
  if (watchlistHint && watchlistWrap) {
    const rows = watchlistWrap.querySelectorAll("tbody tr");
    const anyVisible = Array.from(rows).some((row) => row.style.display !== "none");
    if (rows.length && !anyVisible) {
      watchlistHint.textContent = searchQuery
        ? "No watchlist books match your search."
        : "All caught up — every book in this view is acknowledged. Toggle “Hide acknowledged” off to see them.";
      watchlistHint.style.display = "";
      watchlistWrap.style.display = "none";
    } else {
      watchlistHint.style.display = "none";
      watchlistWrap.style.display = "";
    }
  }
}

function setupTopbarSearchFilter() {
  const input = document.getElementById("topbar-search-input");
  if (!input) return;
  input.addEventListener("input", () => {
    searchQuery = input.value.trim().toLowerCase();
    applyCombinedVisibility();
  });
}

function setupSortableTables() {
  document.querySelectorAll("table.sortable").forEach((table) => {
    const headers = Array.from(table.querySelectorAll("thead th[data-sort-index]"));
    if (!headers.length) return;

    headers.forEach((th) => {
      th.classList.add("sortable-col");
      th.addEventListener("click", () => {
        const index = parseInt(th.dataset.sortIndex, 10);
        const dir = th.dataset.sortDir === "asc" ? "desc" : "asc";

        headers.forEach((h) => {
          delete h.dataset.sortDir;
          h.querySelector(".sort-indicator")?.remove();
        });
        th.dataset.sortDir = dir;
        const indicator = document.createElement("span");
        indicator.className = "sort-indicator";
        indicator.textContent = dir === "asc" ? " ▲" : " ▼";
        th.appendChild(indicator);

        const tbody = table.querySelector("tbody");
        const rows = Array.from(tbody.querySelectorAll("tr"));
        rows.sort((a, b) => {
          const av = a.children[index].textContent.trim();
          const bv = b.children[index].textContent.trim();
          const cmp = av.localeCompare(bv, undefined, { numeric: true, sensitivity: "base" });
          return dir === "asc" ? cmp : -cmp;
        });
        rows.forEach((row) => tbody.appendChild(row));
      });
    });
  });
}

const HIDE_ACKNOWLEDGED_STORAGE_KEY = "abtracker-hide-acknowledged";

function setupAcknowledgedToggle() {
  const checkbox = document.getElementById("hide-acknowledged-toggle");
  if (!checkbox) return;

  const stored = localStorage.getItem(HIDE_ACKNOWLEDGED_STORAGE_KEY);
  hideAcknowledged = stored === null ? true : stored === "true";
  checkbox.checked = hideAcknowledged;
  applyCombinedVisibility();

  checkbox.addEventListener("change", () => {
    hideAcknowledged = checkbox.checked;
    localStorage.setItem(HIDE_ACKNOWLEDGED_STORAGE_KEY, hideAcknowledged ? "true" : "false");
    applyCombinedVisibility();
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupPushButton();
  setupMenus();
  setupThemeToggle();
  setupAcknowledgedToggle();
  setupTopbarSearchFilter();
  setupSortableTables();
});
