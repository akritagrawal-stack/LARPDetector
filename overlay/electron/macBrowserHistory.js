const os = require('os');
const path = require('path');
const { spawn } = require('child_process');
const { pathToFileURL } = require('url');

const MAX_HISTORY_URL_AGE_SECONDS = 2 * 60 * 60;
const COMPANION_HISTORY_URL_AGE_SECONDS = 15 * 60;
const HISTORY_QUERY_TIMEOUT_MS = 1500;

function parseHistoryRow(raw, source) {
  const line = String(raw || '').trim().split(/\r?\n/)[0] || '';
  const tabIndex = line.lastIndexOf('\t');
  if (tabIndex <= 0) return null;

  const url = line.slice(0, tabIndex).trim();
  const ageSeconds = Number(line.slice(tabIndex + 1).trim());
  if (!/^https?:\/\/(?:[^/]+\.)?linkedin\.com\/in\/[^/?#]+/i.test(url)) return null;
  if (!Number.isFinite(ageSeconds) || ageSeconds < 0) return null;
  return { url, ageSeconds, source };
}

function queryHistoryDatabase(candidate) {
  return new Promise((resolve) => {
    let settled = false;
    let stdout = '';
    let child;

    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };

    try {
      // Chromium keeps History locked while the browser is open. SQLite's
      // immutable URI mode provides a safe read-only snapshot of the main
      // database without waiting on that writer lock. It may omit a
      // not-yet-checkpointed WAL row, so this remains a fallback rather than
      // replacing the live browser companion.
      const databaseUri = pathToFileURL(candidate.dbPath);
      databaseUri.searchParams.set('immutable', '1');
      child = spawn(
        '/usr/bin/sqlite3',
        ['-readonly', '-separator', '\t', databaseUri.href, candidate.query],
        { stdio: ['ignore', 'pipe', 'ignore'], windowsHide: true }
      );
    } catch {
      resolve(null);
      return;
    }

    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        // Best-effort only.
      }
      finish(null);
    }, HISTORY_QUERY_TIMEOUT_MS);

    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
    });
    child.once('error', () => finish(null));
    child.once('exit', () => finish(parseHistoryRow(stdout, candidate.source)));
  });
}

async function getRecentMacBrowserHistoryUrl(
  homeDir = os.homedir(),
  maxAgeSeconds = MAX_HISTORY_URL_AGE_SECONDS
) {
  const chromiumQuery =
    "select url, cast(strftime('%s','now') - " +
    '(last_visit_time / 1000000 - 11644473600) as integer) ' +
    "from urls where url like '%linkedin.com/in/%' " +
    'order by last_visit_time desc limit 1;';
  const candidates = [
    {
      source: 'safari-history',
      dbPath: path.join(homeDir, 'Library', 'Safari', 'History.db'),
      query:
        "select history_items.url, " +
        "cast(strftime('%s','now') - (history_visits.visit_time + 978307200) as integer) " +
        'from history_visits join history_items ' +
        'on history_items.id = history_visits.history_item ' +
        "where history_items.url like '%linkedin.com/in/%' " +
        'and history_visits.load_successful = 1 ' +
        'order by history_visits.visit_time desc limit 1;'
    },
    {
      source: 'comet-history',
      dbPath: path.join(homeDir, 'Library', 'Application Support', 'Comet', 'Default', 'History'),
      query: chromiumQuery
    },
    {
      source: 'chrome-history',
      dbPath: path.join(
        homeDir,
        'Library',
        'Application Support',
        'Google',
        'Chrome',
        'Default',
        'History'
      ),
      query: chromiumQuery
    },
    {
      source: 'brave-history',
      dbPath: path.join(
        homeDir,
        'Library',
        'Application Support',
        'BraveSoftware',
        'Brave-Browser',
        'Default',
        'History'
      ),
      query: chromiumQuery
    },
    {
      source: 'edge-history',
      dbPath: path.join(
        homeDir,
        'Library',
        'Application Support',
        'Microsoft Edge',
        'Default',
        'History'
      ),
      query: chromiumQuery
    },
    {
      source: 'arc-history',
      dbPath: path.join(
        homeDir,
        'Library',
        'Application Support',
        'Arc',
        'User Data',
        'Default',
        'History'
      ),
      query: chromiumQuery
    }
  ];

  const results = (await Promise.all(candidates.map(queryHistoryDatabase)))
    .filter((item) => item && item.ageSeconds <= maxAgeSeconds)
    .sort((a, b) => a.ageSeconds - b.ageSeconds);

  return results[0] || null;
}

module.exports = {
  MAX_HISTORY_URL_AGE_SECONDS,
  COMPANION_HISTORY_URL_AGE_SECONDS,
  parseHistoryRow,
  getRecentMacBrowserHistoryUrl,
};
