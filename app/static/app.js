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

// The search filter, the "hide acknowledged" toggle, and the library filter chips
// all hide/show the same [data-series-name] elements. They are combined here: an
// element shows only if it passes search, library filter, and acknowledged state.
let searchQuery = "";
let hideAcknowledged = true;
let watchlistLibraryFilter = "all";
const WATCHLIST_LIB_FILTER_KEY = "abtracker-watchlist-library-filter";

function applyCombinedVisibility() {
  let countAll = 0;
  let countInLib = 0;
  let countNotInLib = 0;
  let countInLibUnack = 0;
  let countMissingAck = 0;

  const watchlistRows = document.querySelectorAll("#watchlist-table-wrap tbody tr");
  const isWatchlist = watchlistRows.length > 0;
  const ackTargetBookIds = [];

  document.querySelectorAll("[data-series-name]").forEach((el) => {
    const seriesName = el.dataset.seriesName || el.getAttribute("data-series-name") || "";
    const bookTitle = el.dataset.bookTitle || el.getAttribute("data-book-title") || "";
    const seriesMatch = seriesName.includes(searchQuery);
    const bookMatch = bookTitle.includes(searchQuery);
    const searchOk = !searchQuery || Boolean(seriesMatch || bookMatch);

    const isAck = (el.dataset.acknowledged || el.getAttribute("data-acknowledged")) === "true";
    const inLibAttr = el.getAttribute("data-in-library");
    const hasLibData = inLibAttr !== null;
    const isInLib = inLibAttr === "true";

    if (isWatchlist && hasLibData && searchOk) {
      const ackEligible = !(hideAcknowledged && isAck);
      if (ackEligible) {
        countAll++;
        if (isInLib) countInLib++;
        else countNotInLib++;
      }
      if (isInLib && !isAck) {
        countInLibUnack++;
      }
      if (!isInLib && isAck) {
        countMissingAck++;
      }
    }

    let ackOk = !(hideAcknowledged && isAck);
    let libOk = true;

    if (hasLibData) {
      if (watchlistLibraryFilter === "in_library") {
        libOk = isInLib;
      } else if (watchlistLibraryFilter === "not_in_library") {
        libOk = !isInLib;
      } else if (watchlistLibraryFilter === "in_library_unacknowledged") {
        libOk = isInLib && !isAck;
        ackOk = true;
      } else if (watchlistLibraryFilter === "not_in_library_acknowledged" || watchlistLibraryFilter === "missing_acknowledged") {
        libOk = !isInLib && isAck;
        ackOk = true; // explicitly display acknowledged books for this audit filter
      }
    }

    el.style.display = searchOk && ackOk && libOk ? "" : "none";

    // Track unacknowledged books that match the active filter and search query for Acknowledge All
    if (isWatchlist && searchOk && !isAck) {
      let matchesFilter = false;
      if (watchlistLibraryFilter === "all") {
        matchesFilter = true;
      } else if (watchlistLibraryFilter === "in_library" || watchlistLibraryFilter === "in_library_unacknowledged") {
        matchesFilter = isInLib;
      } else if (watchlistLibraryFilter === "not_in_library") {
        matchesFilter = !isInLib;
      }
      if (matchesFilter) {
        const bookId = el.getAttribute("data-book-id");
        if (bookId) {
          ackTargetBookIds.push(bookId);
        }
      }
    }
  });

  const elCountAll = document.getElementById("chip-count-all");
  const elCountInLib = document.getElementById("chip-count-in-library");
  const elCountNotInLib = document.getElementById("chip-count-not-in-library");
  const elCountInLibUnack = document.getElementById("chip-count-in-lib-unack");
  const elCountMissingAck = document.getElementById("chip-count-missing-ack");
  if (elCountAll) elCountAll.textContent = countAll;
  if (elCountInLib) elCountInLib.textContent = countInLib;
  if (elCountNotInLib) elCountNotInLib.textContent = countNotInLib;
  if (elCountInLibUnack) elCountInLibUnack.textContent = countInLibUnack;
  if (elCountMissingAck) elCountMissingAck.textContent = countMissingAck;

  const isActionFilter = watchlistLibraryFilter === "in_library_unacknowledged" ||
                         watchlistLibraryFilter === "not_in_library_acknowledged" ||
                         watchlistLibraryFilter === "missing_acknowledged";
  const filterControls = document.getElementById("watchlist-filter-controls");
  if (filterControls && isWatchlist) {
    filterControls.classList.toggle("deemphasized", isActionFilter);
  }

  const ackAllForm = document.getElementById("acknowledge-all-form");
  const ackAllBtn = document.getElementById("acknowledge-all-btn");
  const ackAllFilterInput = document.getElementById("acknowledge-all-filter");
  const ackAllBookIdsInput = document.getElementById("acknowledge-all-book-ids");

  if (ackAllForm && ackAllBtn) {
    const ackCount = ackTargetBookIds.length;
    if (ackCount === 0) {
      ackAllForm.style.display = "none";
    } else {
      ackAllForm.style.display = "";
      if (ackAllFilterInput) {
        ackAllFilterInput.value = watchlistLibraryFilter;
      }
      if (ackAllBookIdsInput) {
        ackAllBookIdsInput.value = ackTargetBookIds.join(",");
      }

      const watchlistWrap = document.getElementById("watchlist-table-wrap");
      const seriesName = watchlistWrap?.dataset?.filteredSeries || "";
      const isSeriesView = Boolean(seriesName || new URLSearchParams(window.location.search).get("series_id"));

      const btnText = isSeriesView
        ? `Acknowledge all in this series (${ackCount})`
        : `Acknowledge all (${ackCount})`;
      const noun = ackCount === 1 ? "book" : "books";
      const confirmMsg = seriesName
        ? `Acknowledge all ${ackCount} ${noun} in ${seriesName}?`
        : `Acknowledge all ${ackCount} ${noun}?`;

      ackAllBtn.textContent = btnText;
      ackAllForm.dataset.confirmMsg = confirmMsg;
    }
  }

  const ackToggle = document.getElementById("hide-acknowledged-toggle");
  if (ackToggle && isWatchlist) {
    ackToggle.disabled = false;
    ackToggle.checked = hideAcknowledged;
  }

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
      const isSeriesView = Boolean(
        watchlistWrap.dataset.filteredSeries ||
        new URLSearchParams(window.location.search).get("series_id")
      );

      if (searchQuery) {
        watchlistHint.textContent = isSeriesView
          ? "No books in this series match your search."
          : "No watchlist books match your search.";
      } else if (watchlistLibraryFilter === "in_library") {
        watchlistHint.textContent = hideAcknowledged
          ? (isSeriesView
              ? "No unacknowledged library books in this series. Toggle “Hide acknowledged” off to see library books in this series."
              : "No unacknowledged library books. Toggle “Hide acknowledged” off to see library books.")
          : (isSeriesView
              ? "No library books in this series."
              : "No library books in this view.");
      } else if (watchlistLibraryFilter === "not_in_library") {
        watchlistHint.textContent = hideAcknowledged
          ? (isSeriesView
              ? "No unacknowledged non-library books in this series. Toggle “Hide acknowledged” off to see non-library books in this series."
              : "No unacknowledged non-library books. Toggle “Hide acknowledged” off to see non-library books.")
          : (isSeriesView
              ? "No non-library books in this series."
              : "No non-library books in this view.");
      } else if (watchlistLibraryFilter === "in_library_unacknowledged") {
        watchlistHint.textContent = isSeriesView
          ? "You're all caught up! No unacknowledged books in this series."
          : "You're all caught up! No unacknowledged books are in your library.";
      } else if (watchlistLibraryFilter === "not_in_library_acknowledged" || watchlistLibraryFilter === "missing_acknowledged") {
        watchlistHint.textContent = isSeriesView
          ? "Great news! No acknowledged books in this series are missing from your library."
          : "Great news! No acknowledged books are missing from your library.";
      } else {
        watchlistHint.textContent = hideAcknowledged
          ? (isSeriesView
              ? "All caught up — every book in this series is acknowledged. Toggle “Hide acknowledged” off to see all books in this series."
              : "All caught up — every book in this view is acknowledged. Toggle “Hide acknowledged” off to see all books.")
          : (isSeriesView
              ? "No books in this series."
              : "No books in this view.");
      }
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

    const storageKey = table.id ? `abtracker-table-sort-${table.id}` : null;

    function applySort(th, dir, persist = true) {
      const index = parseInt(th.dataset.sortIndex, 10);
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
        const av = a.children[index]?.textContent.trim() || "";
        const bv = b.children[index]?.textContent.trim() || "";
        const cmp = av.localeCompare(bv, undefined, { numeric: true, sensitivity: "base" });
        return dir === "asc" ? cmp : -cmp;
      });
      rows.forEach((row) => tbody.appendChild(row));

      if (persist && storageKey) {
        safeSetStorage(storageKey, JSON.stringify({ sortIndex: th.dataset.sortIndex, dir }));
      }
    }

    headers.forEach((th) => {
      th.classList.add("sortable-col");
      th.addEventListener("click", () => {
        const dir = th.dataset.sortDir === "asc" ? "desc" : "asc";
        applySort(th, dir, true);
      });
    });

    if (storageKey) {
      const savedRaw = safeGetStorage(storageKey);
      if (savedRaw) {
        try {
          const saved = JSON.parse(savedRaw);
          const matchingTh = headers.find((h) => h.dataset.sortIndex === String(saved.sortIndex));
          if (matchingTh && (saved.dir === "asc" || saved.dir === "desc")) {
            applySort(matchingTh, saved.dir, false);
          }
        } catch (e) {}
      }
    }
  });
}

function updateWatchlistHeaderCounts() {
  const tbody = document.querySelector("#watchlist-table-wrap tbody");
  if (!tbody) return;
  const rows = Array.from(tbody.querySelectorAll("tr"));
  const unackCount = rows.filter((r) => r.getAttribute("data-acknowledged") !== "true").length;
  const totalCount = rows.length;

  const headerCount = document.getElementById("watchlist-header-count");
  if (headerCount) {
    headerCount.textContent = `(${unackCount} / ${totalCount})`;
  }
}

function setupWatchlistRowActions() {
  const wrap = document.getElementById("watchlist-table-wrap");
  if (!wrap) return;

  wrap.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form || !form.action) return;

    const isAck = form.action.includes("/acknowledge");
    const isUnack = form.action.includes("/unacknowledge");
    if (!isAck && !isUnack) return;

    event.preventDefault();

    const row = form.closest("tr");
    const submitBtn = form.querySelector("button[type=submit]");
    if (submitBtn) submitBtn.disabled = true;

    try {
      const resp = await fetch(form.action, {
        method: "POST",
        headers: { "Accept": "application/json" },
        body: new FormData(form),
      });

      if (!resp.ok && resp.status !== 303) {
        form.submit();
        return;
      }

      if (row) {
        if (isAck) {
          row.setAttribute("data-acknowledged", "true");
          row.dataset.acknowledged = "true";
          row.classList.add("row-acknowledged");
          form.action = form.action.replace("/acknowledge", "/unacknowledge");
          if (submitBtn) {
            submitBtn.textContent = "Watch";
            submitBtn.disabled = false;
          }
        } else {
          row.setAttribute("data-acknowledged", "false");
          row.dataset.acknowledged = "false";
          row.classList.remove("row-acknowledged");
          form.action = form.action.replace("/unacknowledge", "/acknowledge");
          if (submitBtn) {
            submitBtn.textContent = "Acknowledge";
            submitBtn.disabled = false;
          }
        }

        applyCombinedVisibility();
        updateWatchlistHeaderCounts();
      }
    } catch (err) {
      form.submit();
    }
  });

  const ackAllForm = document.getElementById("acknowledge-all-form");
  if (ackAllForm) {
    ackAllForm.removeAttribute("onsubmit");
    ackAllForm.addEventListener("submit", async (event) => {
      const confirmMsg = ackAllForm.dataset.confirmMsg;
      if (confirmMsg && !window.confirm(confirmMsg)) {
        event.preventDefault();
        return;
      }

      event.preventDefault();

      const submitBtn = ackAllForm.querySelector("button[type=submit]");
      if (submitBtn) submitBtn.disabled = true;

      try {
        const resp = await fetch(ackAllForm.action, {
          method: "POST",
          headers: { "Accept": "application/json" },
          body: new FormData(ackAllForm),
        });

        if (!resp.ok && resp.status !== 303) {
          HTMLFormElement.prototype.submit.call(ackAllForm);
          return;
        }

        const data = await resp.json();
        const ackedSet = new Set((data.acknowledged_ids || []).map(String));

        const tbody = wrap.querySelector("tbody");
        if (tbody) {
          tbody.querySelectorAll("tr").forEach((row) => {
            const bId = row.getAttribute("data-book-id");
            if (ackedSet.has(bId)) {
              row.setAttribute("data-acknowledged", "true");
              row.dataset.acknowledged = "true";
              row.classList.add("row-acknowledged");
              const form = row.querySelector("form");
              if (form) {
                form.action = form.action.replace("/acknowledge", "/unacknowledge");
                const btn = form.querySelector("button[type=submit]");
                if (btn) {
                  btn.textContent = "Watch";
                  btn.disabled = false;
                }
              }
            }
          });
        }

        applyCombinedVisibility();
        updateWatchlistHeaderCounts();
      } catch (err) {
        HTMLFormElement.prototype.submit.call(ackAllForm);
      } finally {
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  }
}

function safeGetStorage(key) {
  try {
    return localStorage.getItem(key);
  } catch (e) {
    return null;
  }
}

function safeSetStorage(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (e) {}
}

function setupWatchlistFilterChips() {
  const stateIsland = document.getElementById("state-chip-island");
  const actionIsland = document.getElementById("action-chip-island");
  if (!stateIsland && !actionIsland) return;

  const validState = ["all", "in_library", "not_in_library"];
  const validAction = ["in_library_unacknowledged", "not_in_library_acknowledged", "missing_acknowledged"];

  let lastStateFilter = "all";
  const stored = safeGetStorage(WATCHLIST_LIB_FILTER_KEY);
  if (stored) {
    if (validState.includes(stored)) {
      watchlistLibraryFilter = stored;
      lastStateFilter = stored;
    } else if (validAction.includes(stored)) {
      watchlistLibraryFilter = stored === "missing_acknowledged" ? "not_in_library_acknowledged" : stored;
      lastStateFilter = "all";
    }
  }

  function updateActiveChip() {
    const isActionFilter = watchlistLibraryFilter === "in_library_unacknowledged" ||
                           watchlistLibraryFilter === "not_in_library_acknowledged" ||
                           watchlistLibraryFilter === "missing_acknowledged";

    const filterControls = document.getElementById("watchlist-filter-controls");
    if (filterControls) {
      filterControls.classList.toggle("deemphasized", isActionFilter);
    }

    if (stateIsland) {
      stateIsland.querySelectorAll(".filter-chip").forEach((btn) => {
        const f = btn.getAttribute("data-filter");
        btn.classList.toggle("active", !isActionFilter && f === watchlistLibraryFilter);
      });
    }

    if (actionIsland) {
      actionIsland.querySelectorAll(".filter-chip").forEach((btn) => {
        const f = btn.getAttribute("data-filter");
        const match = (f === watchlistLibraryFilter) ||
                      (f === "not_in_library_acknowledged" && watchlistLibraryFilter === "missing_acknowledged");
        btn.classList.toggle("active", match);
      });
    }
  }

  updateActiveChip();

  function handleFilterClick(filter) {
    const isClickedAction = filter === "in_library_unacknowledged" ||
                            filter === "not_in_library_acknowledged" ||
                            filter === "missing_acknowledged";

    if (isClickedAction) {
      const isAlreadyActive = watchlistLibraryFilter === filter ||
                              (filter === "not_in_library_acknowledged" && watchlistLibraryFilter === "missing_acknowledged");
      if (isAlreadyActive) {
        // Toggle off back to last state filter
        watchlistLibraryFilter = lastStateFilter;
      } else {
        watchlistLibraryFilter = filter === "missing_acknowledged" ? "not_in_library_acknowledged" : filter;
      }
    } else {
      // Clicked a state filter in the upper island
      watchlistLibraryFilter = filter;
      lastStateFilter = filter;
    }

    safeSetStorage(WATCHLIST_LIB_FILTER_KEY, watchlistLibraryFilter);
    updateActiveChip();
    applyCombinedVisibility();
  }

  if (stateIsland) {
    stateIsland.addEventListener("click", (event) => {
      const btn = event.target.closest(".filter-chip");
      if (!btn) return;
      const filter = btn.getAttribute("data-filter");
      if (filter) handleFilterClick(filter);
    });
  }

  if (actionIsland) {
    actionIsland.addEventListener("click", (event) => {
      const btn = event.target.closest(".filter-chip");
      if (!btn) return;
      const filter = btn.getAttribute("data-filter");
      if (filter) handleFilterClick(filter);
    });
  }
}

// Dashboard and Watchlist each have their own "Hide acknowledged" checkbox,
// with independent remembered preferences — toggling one shouldn't silently
// change what the other page shows. Each template's checkbox carries its own
// data-storage-key; the two pages never render at the same time, so a single
// module-level hideAcknowledged variable is still fine at runtime.
function setupAcknowledgedToggle() {
  const checkbox = document.getElementById("hide-acknowledged-toggle");
  if (!checkbox) return;

  const storageKey = checkbox.dataset.storageKey;
  const stored = safeGetStorage(storageKey);
  hideAcknowledged = stored === null ? true : stored === "true";
  checkbox.checked = hideAcknowledged;
  applyCombinedVisibility();

  checkbox.addEventListener("change", () => {
    hideAcknowledged = checkbox.checked;
    safeSetStorage(storageKey, hideAcknowledged ? "true" : "false");

    const isActionFilter = watchlistLibraryFilter === "in_library_unacknowledged" ||
                           watchlistLibraryFilter === "not_in_library_acknowledged" ||
                           watchlistLibraryFilter === "missing_acknowledged";
    if (isActionFilter) {
      watchlistLibraryFilter = "all";
      safeSetStorage(WATCHLIST_LIB_FILTER_KEY, "all");
      const filterControls = document.getElementById("watchlist-filter-controls");
      if (filterControls) filterControls.classList.remove("deemphasized");
      const stateIsland = document.getElementById("state-chip-island");
      if (stateIsland) {
        stateIsland.querySelectorAll(".filter-chip").forEach((btn) => {
          btn.classList.toggle("active", btn.getAttribute("data-filter") === "all");
        });
      }
      const actionIsland = document.getElementById("action-chip-island");
      if (actionIsland) {
        actionIsland.querySelectorAll(".filter-chip").forEach((btn) => {
          btn.classList.remove("active");
        });
      }
    }

    applyCombinedVisibility();
  });
}

function initApp() {
  setupPushButton();
  setupMenus();
  setupThemeToggle();
  setupWatchlistFilterChips();
  setupAcknowledgedToggle();
  setupTopbarSearchFilter();
  setupSortableTables();
  setupWatchlistRowActions();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initApp);
} else {
  initApp();
}
