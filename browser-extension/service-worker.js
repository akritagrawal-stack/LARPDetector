const ENDPOINT = 'http://127.0.0.1:8756/browser-tab';
const PRESENCE_ENDPOINT = 'http://127.0.0.1:8756/browser-companion';
const PROFILE_URL = /^https:\/\/([a-z0-9-]+\.)?linkedin\.com\/in\//i;
const extensionApi = globalThis.browser || globalThis.chrome;

async function publishPresence() {
  try {
    await fetch(PRESENCE_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ browser: navigator.userAgent })
    });
  } catch {
    // The desktop app may be closed. The next browser event retries.
  }
}

async function publishTab(tab) {
  if (!tab || !PROFILE_URL.test(tab.url || '')) return;
  try {
    await fetch(ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: tab.url,
        browser: navigator.userAgent
      })
    });
  } catch {
    // The desktop app may be closed. The next tab event or heartbeat retries.
  }
}

async function publishActiveTab() {
  await publishPresence();
  const tabs = await extensionApi.tabs.query({ active: true, lastFocusedWindow: true });
  await publishTab(tabs[0]);
}

extensionApi.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    await publishTab(await extensionApi.tabs.get(tabId));
  } catch {
    // The tab may have closed between activation and lookup.
  }
});

extensionApi.tabs.onUpdated.addListener((_tabId, changeInfo, tab) => {
  if (changeInfo.url || changeInfo.status === 'complete') {
    publishPresence();
    publishTab(tab);
  }
});

extensionApi.windows.onFocusChanged.addListener(() => publishActiveTab());
extensionApi.action.onClicked.addListener((tab) => {
  publishPresence();
  publishTab(tab);
});
extensionApi.runtime.onInstalled.addListener(() => {
  extensionApi.alarms.create('publish-active-profile', { periodInMinutes: 0.5 });
  publishActiveTab();
});
extensionApi.runtime.onStartup.addListener(() => publishActiveTab());
extensionApi.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'publish-active-profile') publishActiveTab();
});

// A Manifest V3 worker can restart independently of browser startup. Publishing
// here makes Settings update as soon as the worker wakes, even when the active
// tab is not LinkedIn.
publishPresence();
