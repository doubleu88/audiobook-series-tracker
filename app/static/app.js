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

document.addEventListener("DOMContentLoaded", setupPushButton);
