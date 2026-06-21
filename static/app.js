document.addEventListener('DOMContentLoaded', () => {
  // Form Config Elements
  const configForm = document.getElementById('config-form');
  const qbittorrentHostInput = document.getElementById('qbittorrentHost');
  const qbittorrentPortInput = document.getElementById('qbittorrentPort');
  const qbittorrentUsernameInput = document.getElementById('qbittorrentUsername');
  const qbittorrentPasswordInput = document.getElementById('qbittorrentPassword');
  
  const stuckLimitMinutesInput = document.getElementById('stuckLimitMinutes');
  const checkIntervalSecondsInput = document.getElementById('checkIntervalSeconds');
  const cronExpressionInput = document.getElementById('cronExpression');

  const autoReannounceInput = document.getElementById('autoReannounce');
  const injectPublicTrackersInput = document.getElementById('injectPublicTrackers');

  const downloadsDirPathInput = document.getElementById('downloadsDirPath');
  const enableOrphanedCleanerInput = document.getElementById('enableOrphanedCleaner');
  const orphanedCleanerDryRunInput = document.getElementById('orphanedCleanerDryRun');

  const excludeTagsInput = document.getElementById('excludeTags');
  const excludeCategoriesInput = document.getElementById('excludeCategories');

  const webhookUrlInput = document.getElementById('webhookUrl');
  const webhookTypeInput = document.getElementById('webhookType');

  // Buttons
  const testQbtBtn = document.getElementById('test-qbt-btn');
  const triggerCheckBtn = document.getElementById('trigger-check-btn');
  const triggerOrphanedDryBtn = document.getElementById('trigger-orphaned-dry-btn');
  const triggerOrphanedLiveBtn = document.getElementById('trigger-orphaned-live-btn');
  const clearLogsBtn = document.getElementById('clear-logs-btn');

  // Stats elements
  const statRuns = document.getElementById('stat-runs');
  const statPaused = document.getElementById('stat-paused');
  const statReannounced = document.getElementById('stat-reannounced');
  const statInjected = document.getElementById('stat-injected');
  const statOrphanedData = document.getElementById('stat-orphaned-data');
  const globalStatusDot = document.getElementById('global-status-dot');
  const globalStatusText = document.getElementById('global-status-text');

  // Lists and outputs
  const torrentsList = document.getElementById('torrents-list');
  const consoleOutput = document.getElementById('console-output');

  let lastLogsJson = '';

  // Load config from server
  async function loadConfig() {
    try {
      const res = await fetch('/api/config');
      const data = await res.json();
      
      qbittorrentHostInput.value = data.qbittorrentHost || 'localhost';
      qbittorrentPortInput.value = data.qbittorrentPort || 8080;
      qbittorrentUsernameInput.value = data.qbittorrentUsername || 'admin';
      qbittorrentPasswordInput.value = data.qbittorrentPassword || '';
      
      stuckLimitMinutesInput.value = data.stuckLimitMinutes || 15;
      checkIntervalSecondsInput.value = data.checkIntervalSeconds || 900;
      cronExpressionInput.value = data.cronExpression || '';

      autoReannounceInput.checked = !!data.autoReannounce;
      injectPublicTrackersInput.checked = !!data.injectPublicTrackers;

      downloadsDirPathInput.value = data.downloadsDirPath || '/downloads';
      enableOrphanedCleanerInput.checked = !!data.enableOrphanedCleaner;
      orphanedCleanerDryRunInput.checked = !!data.orphanedCleanerDryRun;

      excludeTagsInput.value = (data.excludeTags || []).join(', ');
      excludeCategoriesInput.value = (data.excludeCategories || []).join(', ');

      webhookUrlInput.value = data.webhookUrl || '';
      webhookTypeInput.value = data.webhookType || 'discord';
    } catch (err) {
      appendLog('error', `Failed to fetch settings config: ${err.message}`);
    }
  }

  // Save Config to Server
  configForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    
    // Format exclude lists
    const excludeTags = excludeTagsInput.value.split(',').map(x => x.trim()).filter(Boolean);
    const excludeCategories = excludeCategoriesInput.value.split(',').map(x => x.trim()).filter(Boolean);

    const configData = {
      qbittorrentHost: qbittorrentHostInput.value,
      qbittorrentPort: parseInt(qbittorrentPortInput.value),
      qbittorrentUsername: qbittorrentUsernameInput.value,
      qbittorrentPassword: qbittorrentPasswordInput.value,
      stuckLimitMinutes: parseFloat(stuckLimitMinutesInput.value),
      checkIntervalSeconds: parseInt(checkIntervalSecondsInput.value),
      cronExpression: cronExpressionInput.value,
      autoReannounce: autoReannounceInput.checked,
      injectPublicTrackers: injectPublicTrackersInput.checked,
      downloadsDirPath: downloadsDirPathInput.value,
      enableOrphanedCleaner: enableOrphanedCleanerInput.checked,
      orphanedCleanerDryRun: orphanedCleanerDryRunInput.checked,
      excludeTags: excludeTags,
      excludeCategories: excludeCategories,
      webhookUrl: webhookUrlInput.value,
      webhookType: webhookTypeInput.value
    };

    try {
      const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(configData)
      });
      const data = await res.json();
      if (res.ok) {
        alert('Configuration saved successfully!');
        loadConfig();
      } else {
        alert(`Error saving configuration: ${data.detail || data.error}`);
      }
    } catch (err) {
      alert(`Network error saving config: ${err.message}`);
    }
  });

  // Test Connection
  testQbtBtn.addEventListener('click', async () => {
    const origText = testQbtBtn.innerText;
    testQbtBtn.innerText = 'Testing...';
    testQbtBtn.disabled = true;

    try {
      const res = await fetch('/api/test-qbt', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          host: qbittorrentHostInput.value,
          port: qbittorrentPortInput.value,
          username: qbittorrentUsernameInput.value,
          password: qbittorrentPasswordInput.value
        })
      });
      const data = await res.json();
      if (res.ok && data.success) {
        alert(`Connected Successfully! qBittorrent version: ${data.version}`);
        appendLog('info', `qBittorrent connection test succeeded. Version: ${data.version}`);
      } else {
        alert(`Connection failed: ${data.error}`);
        appendLog('error', `qBittorrent connection test failed: ${data.error}`);
      }
    } catch (err) {
      alert(`Network error during test: ${err.message}`);
    } finally {
      testQbtBtn.innerText = origText;
      testQbtBtn.disabled = false;
    }
  });

  // Execute check now
  triggerCheckBtn.addEventListener('click', async () => {
    triggerCheckBtn.disabled = true;
    try {
      const res = await fetch('/api/trigger', { method: 'POST' });
      if (res.ok) {
        appendLog('info', 'Check cycle manually requested.');
        setTimeout(fetchStatus, 1000);
      }
    } catch (err) {
      console.error(err);
    } finally {
      triggerCheckBtn.disabled = false;
    }
  });

  // Clean Orphaned files trigger
  async function cleanOrphanedFiles(dryRun) {
    const btn = dryRun ? triggerOrphanedDryBtn : triggerOrphanedLiveBtn;
    const origText = btn.innerText;
    btn.innerText = dryRun ? 'Scanning...' : 'Cleaning...';
    btn.disabled = true;

    try {
      appendLog('info', `Executing manual orphaned files cleanup (Dry-Run: ${dryRun})...`);
      const res = await fetch('/api/clean-orphaned', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dry_run: dryRun })
      });
      const data = await res.json();
      
      if (res.ok) {
        if (data.error) {
          alert(`Error: ${data.error}`);
          appendLog('error', `Cleanup error: ${data.error}`);
        } else {
          const filesCount = data.cleaned_files ? data.cleaned_files.length : 0;
          const mbReclaimed = data.total_bytes ? (data.total_bytes / (1024 * 1024)).toFixed(1) : '0.0';
          
          const alertMsg = dryRun 
            ? `Dry-Run Scan Complete!\nFound ${filesCount} orphaned files totaling ${mbReclaimed} MB.`
            : `Cleanup Complete!\nSuccessfully deleted ${filesCount} files and reclaimed ${mbReclaimed} MB.`;
          
          alert(alertMsg);
          appendLog('info', `Orphaned Scan: Found ${filesCount} files (${mbReclaimed} MB). Dry-run: ${dryRun}`);
          fetchStatus();
        }
      }
    } catch (err) {
      alert(`Network error during cleanup: ${err.message}`);
    } finally {
      btn.innerText = origText;
      btn.disabled = false;
    }
  }

  triggerOrphanedDryBtn.addEventListener('click', () => cleanOrphanedFiles(true));
  triggerOrphanedLiveBtn.addEventListener('click', () => {
    if (confirm("WARNING: This will permanently delete orphaned files from your local downloads folder. Are you sure you want to proceed?")) {
      cleanOrphanedFiles(false);
    }
  });

  // Fine-grained torrent table updater to prevent lag and layout thrashing
  function updateTorrentsTable(torrents, qbtConnected) {
    if (!torrents || torrents.length === 0) {
      const msg = qbtConnected ? 'No torrents inside client.' : 'qBittorrent client offline.';
      const expectedHtml = `<tr><td colspan="6" class="empty-state">${msg}</td></tr>`;
      if (torrentsList.innerHTML !== expectedHtml) {
        torrentsList.innerHTML = expectedHtml;
      }
      return;
    }

    // Check if the empty state row is currently present, if so, clear it
    if (torrentsList.querySelector('.empty-state')) {
      torrentsList.innerHTML = '';
    }

    const existingRows = {};
    torrentsList.querySelectorAll('tr[data-hash]').forEach(row => {
      existingRows[row.getAttribute('data-hash')] = row;
    });

    const newHashes = new Set(torrents.map(t => t.hash));

    // 1. Remove rows for torrents that are no longer in the list
    Object.keys(existingRows).forEach(hash => {
      if (!newHashes.has(hash)) {
        existingRows[hash].remove();
      }
    });

    // 2. Add or update rows
    torrents.forEach((t, index) => {
      const sizeGB = (t.size / (1024 * 1024 * 1024)).toFixed(2);
      const progressPercent = t.progress;
      
      let recoveryBadgeHtml = '';
      let badgeClass = 'badge-active';
      let badgeText = 'Normal';
      if (t.stuck) {
        badgeClass = 'badge-stalled';
        badgeText = 'Stuck & Paused';
      } else if (t.staged_stage === 'injected') {
        badgeClass = 'badge-injected';
        badgeText = 'Trackers Injected';
      } else if (t.staged_stage === 'reannounced') {
        badgeClass = 'badge-reannounced';
        badgeText = 'Reannounced';
      } else if (t.duration_stuck > 0) {
        badgeClass = 'badge-metadata';
        badgeText = 'Tracking Stalled';
      }
      recoveryBadgeHtml = `<span class="badge ${badgeClass}">${badgeText}</span>`;

      const stuckTextVal = t.duration_stuck > 0 ? `${t.duration_stuck} mins` : '-';
      const stateTextVal = t.state ? t.state.toLowerCase() : '';

      let row = existingRows[t.hash];
      if (!row) {
        // Create new row
        row = document.createElement('tr');
        row.setAttribute('data-hash', t.hash);
        row.innerHTML = `
          <td class="cell-name">
            <strong title="${escapeHtml(t.name)}">${escapeHtml(t.name)}</strong><br>
            <span class="cell-details" style="color: var(--text-muted); font-size:0.75rem;">Size: ${sizeGB} GB | Category: ${escapeHtml(t.category || 'None')}</span>
          </td>
          <td class="cell-progress">
            <div class="progress-bar-container">
              <div class="progress-bar-fill" style="width: ${progressPercent}%"></div>
            </div>
            <span class="progress-text">${progressPercent}%</span>
          </td>
          <td class="cell-state"><span class="badge badge-active state-badge" style="text-transform:lowercase;">${stateTextVal}</span></td>
          <td class="cell-stuck">${stuckTextVal}</td>
          <td class="cell-recovery">${recoveryBadgeHtml}</td>
          <td class="cell-actions" style="text-align: right;">
            <button class="btn btn-secondary btn-small inject-trackers-btn" data-hash="${t.hash}" style="padding: 0.35rem 0.75rem; font-size: 0.75rem;">
              💉 Inject
            </button>
          </td>
        `;
        torrentsList.appendChild(row);
      } else {
        // Update existing row if anything changed
        const detailsSpan = row.querySelector('.cell-details');
        const expectedDetails = `Size: ${sizeGB} GB | Category: ${t.category || 'None'}`;
        if (detailsSpan.textContent !== expectedDetails) {
          detailsSpan.textContent = expectedDetails;
        }

        const fillEl = row.querySelector('.progress-bar-fill');
        const textEl = row.querySelector('.progress-text');
        if (fillEl.style.width !== `${progressPercent}%`) {
          fillEl.style.width = `${progressPercent}%`;
        }
        if (textEl.textContent !== `${progressPercent}%`) {
          textEl.textContent = `${progressPercent}%`;
        }

        const stateBadge = row.querySelector('.state-badge');
        if (stateBadge.textContent !== stateTextVal) {
          stateBadge.textContent = stateTextVal;
        }

        const stuckCell = row.querySelector('.cell-stuck');
        if (stuckCell.textContent !== stuckTextVal) {
          stuckCell.textContent = stuckTextVal;
        }

        const recoveryCell = row.querySelector('.cell-recovery');
        if (recoveryCell.innerHTML !== recoveryBadgeHtml) {
          recoveryCell.innerHTML = recoveryBadgeHtml;
        }
      }

      // Maintain sorting: ensure the DOM element is at the correct index position
      if (torrentsList.children[index] !== row) {
        torrentsList.insertBefore(row, torrentsList.children[index]);
      }
    });
  }

  // Fetch status details
  async function fetchStatus() {
    try {
      const res = await fetch('/api/status');
      const data = await res.json();
      
      // Connection Status Dot
      if (data.qbtConnected) {
        globalStatusDot.className = 'pulse-dot active';
        globalStatusText.innerText = 'qBittorrent Connected';
      } else {
        globalStatusDot.className = 'pulse-dot idle';
        globalStatusText.innerText = 'qBittorrent Offline';
      }

      // Stats
      const stats = data.stats || {};
      statRuns.innerText = stats.runsCount || 0;
      statPaused.innerText = stats.pausedCount || 0;
      statReannounced.innerText = stats.reannouncedCount || 0;
      statInjected.innerText = stats.injectedCount || 0;

      // Cleaned Orphaned Stats Formatting
      const cleanedCount = stats.cleanedOrphanedCount || 0;
      const cleanedMB = stats.cleanedOrphanedBytes ? (stats.cleanedOrphanedBytes / (1024 * 1024)).toFixed(1) : '0.0';
      statOrphanedData.innerText = `${cleanedCount} files (${cleanedMB} MB)`;

      // Render Torrent list with fine-grained updates
      updateTorrentsTable(data.torrents, data.qbtConnected);
      
    } catch (err) {
      console.error('Failed to fetch status:', err);
    }
  }

  // Fetch logs
  async function fetchLogs() {
    try {
      const res = await fetch('/api/logs');
      const data = await res.json();
      
      const logsJson = JSON.stringify(data);
      if (logsJson === lastLogsJson) {
        return; // Skip rendering if no log changes
      }
      lastLogsJson = logsJson;

      // Slice to maximum 100 entries for console performance
      const slicedLogs = data.slice(0, 100);

      consoleOutput.innerHTML = slicedLogs.map(logLine => {
        const timeStr = logLine.timestamp.split('T')[1].substring(0, 8);
        const levelClass = logLine.level.toLowerCase();
        return `
          <div class="log-row ${levelClass}">
            <span class="time">[${timeStr}]</span>
            <span class="level">[${logLine.level}]</span>
            <span class="msg">${escapeHtml(logLine.message)}</span>
          </div>
        `;
      }).join('');
    } catch (err) {
      console.error('Failed to fetch logs:', err);
    }
  }

  // Clear log logs
  clearLogsBtn.addEventListener('click', async () => {
    try {
      const res = await fetch('/api/logs/clear', { method: 'POST' });
      if (res.ok) {
        consoleOutput.innerHTML = '';
        lastLogsJson = ''; // Reset JSON cache to allow re-render
        appendLog('info', 'Daemon logs cleared.');
      }
    } catch (err) {
      console.error(err);
    }
  });

  // Appending inline log helper
  function appendLog(level, message) {
    const timeStr = new Date().toTimeString().split(' ')[0];
    const logRow = document.createElement('div');
    logRow.className = `log-row ${level}`;
    logRow.innerHTML = `
      <span class="time">[${timeStr}]</span>
      <span class="level">[${level.toUpperCase()}]</span>
      <span class="msg">${escapeHtml(message)}</span>
    `;
    consoleOutput.prepend(logRow);
  }

  function escapeHtml(unsafe) {
    return unsafe
         .replace(/&/g, "&amp;")
         .replace(/</g, "&lt;")
         .replace(/>/g, "&gt;")
         .replace(/"/g, "&quot;")
         .replace(/'/g, "&#039;");
  }

  // Handle manual trackers injection via button delegation
  torrentsList.addEventListener('click', async (e) => {
    const btn = e.target.closest('.inject-trackers-btn');
    if (!btn) return;
    
    const hash = btn.getAttribute('data-hash');
    const origHtml = btn.innerHTML;
    btn.innerHTML = '💉 Injecting...';
    btn.disabled = true;
    
    try {
      const res = await fetch(`/api/torrent/${hash}/inject-trackers`, { method: 'POST' });
      const data = await res.json();
      if (res.ok && data.success) {
        alert(data.message);
        appendLog('info', `Manually injected trackers database into torrent: ${hash}`);
        fetchStatus();
      } else {
        alert(`Failed to inject trackers: ${data.detail || data.error}`);
        appendLog('error', `Tracker injection failed for torrent ${hash}: ${data.detail || data.error}`);
      }
    } catch (err) {
      alert(`Network error during injection: ${err.message}`);
    } finally {
      btn.innerHTML = origHtml;
      btn.disabled = false;
    }
  });

  const trackerUploadForm = document.getElementById('tracker-upload-form');
  const trackerFileInput = document.getElementById('trackerFile');
  const customTrackersCount = document.getElementById('custom-trackers-count');

  // Fetch current tracker database size
  async function fetchTrackersCount() {
    try {
      const res = await fetch('/api/trackers');
      const data = await res.json();
      if (customTrackersCount) {
        customTrackersCount.innerText = data.total || 0;
      }
    } catch (err) {
      console.error('Failed to fetch trackers count:', err);
    }
  }

  // Handle tracker list upload
  if (trackerUploadForm) {
    trackerUploadForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      
      const file = trackerFileInput.files[0];
      if (!file) return;
      
      const formData = new FormData();
      formData.append('file', file);
      
      const uploadBtn = document.getElementById('upload-trackers-btn');
      const origText = uploadBtn.innerText;
      uploadBtn.innerText = 'Uploading...';
      uploadBtn.disabled = true;
      
      try {
        const res = await fetch('/api/trackers/upload', {
          method: 'POST',
          body: formData
        });
        const data = await res.json();
        
        if (res.ok && data.success) {
          alert(`Successfully uploaded trackers!\nAdded ${data.added} new trackers.\nTotal database size: ${data.total} trackers.`);
          appendLog('info', `Uploaded tracker file. Added ${data.added} new tracker URLs. Total database: ${data.total}.`);
          fetchTrackersCount();
          trackerUploadForm.reset();
        } else {
          alert(`Upload failed: ${data.detail || data.error}`);
          appendLog('error', `Failed to upload trackers: ${data.detail || data.error}`);
        }
      } catch (err) {
        alert(`Network error uploading file: ${err.message}`);
      } finally {
        uploadBtn.innerText = origText;
        uploadBtn.disabled = false;
      }
    });
  }

  // Init loops
  loadConfig();
  fetchStatus();
  fetchLogs();
  fetchTrackersCount();

  setInterval(fetchStatus, 3000);
  setInterval(fetchLogs, 3000);
});
