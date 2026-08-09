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

function setupTopbarSearchFilter() {
  const input = document.getElementById("topbar-search-input");
  if (!input) return;

  const targets = document.querySelectorAll("[data-series-name]");
  if (!targets.length) return;

  input.addEventListener("input", () => {
    const query = input.value.trim().toLowerCase();
    targets.forEach((el) => {
      const matches = !query || el.dataset.seriesName.includes(query);
      el.style.display = matches ? "" : "none";
    });
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

document.addEventListener("DOMContentLoaded", () => {
  setupPushButton();
  setupMenus();
  setupThemeToggle();
  setupTopbarSearchFilter();
  setupSortableTables();
});
