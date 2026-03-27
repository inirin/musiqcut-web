// ── 전역 유틸 ──────────────────────────────────────

const NON_VOCAL = new Set(['(intro)', '(outro)', '(interlude)', '(instrumental)', '']);

/**
 * STEP 2 Whisper 추출 가사를 step-2-prompt 요소에 렌더링
 * @param {string} projectId
 * @param {Array|null} whisperLyrics - SSE로 받은 whisper_lyrics (없으면 lyrics.json에서 로드)
 */
async function renderStep2Lyrics(projectId, whisperLyrics) {
  const el = document.getElementById('step-2-prompt');
  if (!el) return;

  let rawLines = [];
  if (whisperLyrics?.length) {
    rawLines = whisperLyrics.map(l => {
      const txt = (typeof l === 'object' && l.text !== undefined) ? l.text : l;
      return (txt && txt.trim()) ? txt : '-';
    });
  } else if (projectId) {
    try {
      const d = await fetch(`/storage/projects/${projectId}/lyrics.json?t=${Date.now()}`).then(r => r.json());
      const scenes = d.scenes || [];
      if (scenes.length) {
        rawLines = scenes.map(s => {
          const vl = (s.vocal_lines || []).find(l => l.trim() && !NON_VOCAL.has(l.trim()));
          return vl || '-';
        });
      } else {
        rawLines = (d.whisper_lyrics || []).map(l => {
          const txt = (typeof l === 'object') ? l.text : l;
          return (txt && txt.trim()) ? txt : '-';
        });
      }
    } catch {}
  }

  if (rawLines.length) {
    const lines = rawLines.map((label, i) => {
      const sec = i * 5;
      const mm = Math.floor(sec / 60), ss = String(Math.floor(sec % 60)).padStart(2, '0');
      return `<span style="color:var(--accent);font-size:0.75rem">${mm}:${ss}</span> ${label}`;
    });
    el.innerHTML = `<span style="color:var(--text-muted);font-size:0.7rem">Whisper 추출 가사</span><br>${lines.join('<br>')}`;
    el.classList.remove('hidden');
  }
}

/** 피드백 날짜 포맷 (UTC → KST) */
function formatFbTime(isoStr) {
  return new Date(isoStr + 'Z').toLocaleString('ko-KR', {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
}

/** 피드백 편집 폼 HTML 생성 */
function fbEditFormHtml(fbId, text, opts = {}) {
  const { projectId, stepNo, sceneNo, dark } = opts;
  const attrs = [`data-fb-id="${fbId}"`];
  if (projectId) attrs.push(`data-project="${projectId}"`);
  if (stepNo) attrs.push(`data-step="${stepNo}"`);
  if (sceneNo) attrs.push(`data-scene="${sceneNo}"`);
  const bg = dark ? 'background:rgba(255,255,255,0.1);color:#fff' : 'background:var(--bg-card);color:var(--text-primary)';
  const cancelFn = sceneNo ? `loadLightboxFeedbacks('${projectId}',${stepNo},${sceneNo})`
    : stepNo ? `loadStepFeedbacks('${projectId}',${stepNo})`
    : `loadFeedbackList(window._currentProjectId)`;
  return `<form class="fb-edit-form" ${attrs.join(' ')} style="display:flex;gap:4px;align-items:center;width:100%">
    <textarea class="fb-edit-input auto-resize" enterkeyhint="send" rows="1" style="flex:1;padding:3px 6px;border:1px solid var(--border);border-radius:4px;${bg};font-size:0.78rem;min-width:0">${text}</textarea>
    <button type="submit" class="fb-action-btn" title="저장">✅</button>
    <button type="button" onclick="${cancelFn}" class="fb-action-btn" title="취소">❌</button>
  </form>`;
}

/** textarea 높이 자동 조정 */
function autoResizeTextarea(container) {
  const ta = container.querySelector('.auto-resize');
  if (ta) { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 120) + 'px'; }
}

let _activeWs = null;  // 현재 WebSocket 연결

const API = {
  async get(path) {
    const r = await fetch(`/api${path}`);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(`/api${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    return r.json();
  },
  async delete(path) {
    const r = await fetch(`/api${path}`, { method: 'DELETE' });
    return r.json();
  },
  /**
   * WebSocket 연결 + 폴링 폴백 하이브리드
   * WebSocket 실패 시 자동으로 2초 폴링으로 전환
   * @returns {{ close: Function }} 연결 핸들
   */
  connectWS(projectId, onEvent) {
    // 이전 연결 닫기
    if (_activeWs) {
      try { _activeWs.close(); } catch {}
      _activeWs = null;
    }

    let closed = false;
    let wsConnected = false;
    let reconnectDelay = 1000;
    let pollTimer = null;

    // ── 폴링 폴백 (WebSocket 실패 시) ──
    function startPolling() {
      if (pollTimer || closed) return;
      console.log('[MusiqCut] WebSocket 불가, 폴링 모드 전환');
      pollTimer = setInterval(async () => {
        if (closed) { stopPolling(); return; }
        try {
          const steps = await API.get(`/projects/${projectId}/steps`);
          const latest = {};
          steps.forEach(s => { latest[s.step_no] = s; });
          for (const s of Object.values(latest)) {
            const d = JSON.parse(s.output_data || '{}');
            if (s.started_at) d.started_at = s.started_at;
            if (s.finished_at) d.finished_at = s.finished_at;
            onEvent({ type: 'step', step: s.step_no, status: s.status, message: s.step_name || '', data: d });
          }
          // 프로젝트 완료 체크
          const proj = await API.get(`/projects/${projectId}`);
          if (proj.status === 'done') {
            onEvent({ type: 'complete', video_url: `/storage/projects/${projectId}/video/final.mp4`, message: '영상 생성 완료!' });
            closed = true; stopPolling();
          } else if (proj.status === 'failed') {
            onEvent({ type: 'error', step: 0, message: proj.error_msg || '파이프라인 실패' });
            closed = true; stopPolling();
          }
        } catch {}
      }, 2000);
    }

    function stopPolling() {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    // ── WebSocket 연결 시도 ──
    function connect() {
      if (closed) return;
      try {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const ws = new WebSocket(`${proto}//${location.host}/api/pipeline/ws/${projectId}`);
        _activeWs = ws;

        // 3초 내 연결 안 되면 폴링으로 전환
        const connectTimeout = setTimeout(() => {
          if (!wsConnected && !closed) {
            console.log('[MusiqCut] WebSocket 연결 타임아웃');
            try { ws.close(); } catch {}
            startPolling();
          }
        }, 3000);

        let pingInterval = null;
        ws.onopen = () => {
          wsConnected = true;
          clearTimeout(connectTimeout);
          stopPolling();
          reconnectDelay = 1000;
          // 15초마다 ping 전송 (프록시 idle timeout 방지)
          pingInterval = setInterval(() => {
            try { if (ws.readyState === 1) ws.send('ping'); } catch {}
          }, 15000);
        };

        ws.onmessage = (e) => {
          try {
            const evt = JSON.parse(e.data);
            if (evt.type === 'ping') return;
            onEvent(evt);
            if (evt.type === 'complete' || evt.type === 'error') {
              closed = true; stopPolling();
            }
          } catch {}
        };

        ws.onclose = () => {
          _activeWs = null;
          clearTimeout(connectTimeout);
          if (pingInterval) { clearInterval(pingInterval); pingInterval = null; }
          if (!closed) {
            // 재접속 시도, 실패하면 폴링으로 전환
            wsConnected = false;
            setTimeout(() => {
              if (!closed) connect();
            }, reconnectDelay);
            reconnectDelay = Math.min(reconnectDelay * 2, 10000);
            // 재접속 동안 폴링도 시작
            startPolling();
          }
        };

        ws.onerror = () => { /* onclose에서 처리 */ };
      } catch {
        // WebSocket 생성 자체 실패 → 폴링
        startPolling();
      }
    }

    connect();
    return {
      close() {
        closed = true;
        stopPolling();
        if (_activeWs) { try { _activeWs.close(); } catch {} _activeWs = null; }
      }
    };
  }
};

// ── 탭 복귀 시 상태 갱신 ──
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && window._currentProjectId) {
    // 결과 페이지가 보이면 최신 상태로 갱신
    const resultPage = document.querySelector('[data-page="result"]:not(.hidden)');
    if (resultPage && typeof loadResult === 'function') {
      loadResult();
    }
  }
});

// ── textarea 자동 높이 ──
document.addEventListener('input', e => {
  if (e.target.classList.contains('auto-resize')) {
    e.target.style.height = 'auto';
    e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px';
  }
});

function toast(msg, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function formatDate(iso) {
  if (!iso) return '';
  // DB는 UTC 저장 — 타임존 없으면 Z 추가하여 UTC로 파싱
  const d = new Date(/[Z+]/.test(iso) ? iso : iso + 'Z');
  return d.toLocaleString('ko-KR', {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false
  });
}

function statusBadge(status) {
  const map = {
    done: ['status-done', '완료'],
    running: ['status-running', '생성중'],
    failed: ['status-failed', '실패'],
    pending: ['status-pending', '대기'],
  };
  const [cls, label] = map[status] || ['status-pending', status];
  return `<span class="status-badge ${cls}">${label}</span>`;
}

// ── 브라우저 알림 ─────────────────────────────────
function isNotificationEnabled() {
  return localStorage.getItem('notif_enabled') !== 'false';
}

function sendNotification(title, body, onClick) {
  if (!isNotificationEnabled()) return;
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  const n = new Notification(title, {
    body,
    icon: '/favicon.ico',
    tag: 'pipeline-status',
    renotify: true
  });
  if (onClick) n.onclick = () => { window.focus(); onClick(); n.close(); };
  setTimeout(() => n.close(), 8000);
}

function toggleNotification(enabled) {
  if (enabled && 'Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission().then(perm => {
      if (perm !== 'granted') {
        document.getElementById('notif-toggle').checked = false;
        localStorage.setItem('notif_enabled', 'false');
      } else {
        localStorage.setItem('notif_enabled', 'true');
      }
      updateNotifStatus();
    });
    return;
  }
  localStorage.setItem('notif_enabled', enabled ? 'true' : 'false');
  updateNotifStatus();
}

function updateNotifStatus() {
  const el = document.getElementById('notif-status');
  if (!el) return;
  const toggle = document.getElementById('notif-toggle');

  const isHttp = location.protocol === 'http:' && location.hostname !== 'localhost';

  if (!('Notification' in window) || (isHttp && Notification.permission === 'denied')) {
    el.textContent = isHttp
      ? 'HTTP 환경에서는 브라우저 알림을 사용할 수 없습니다 (HTTPS 필요)'
      : '이 브라우저는 알림을 지원하지 않습니다';
    if (toggle) { toggle.checked = false; toggle.disabled = true; }
    return;
  }

  const enabled = isNotificationEnabled();
  if (toggle) toggle.checked = enabled;

  if (Notification.permission === 'denied') {
    el.innerHTML = '알림이 차단되었습니다. <strong>주소창 🔒 → 사이트 설정 → 알림 → 허용</strong>으로 변경 후 새로고침해주세요.';
    if (toggle) { toggle.checked = false; toggle.disabled = true; }
  } else if (Notification.permission === 'granted' && enabled) {
    el.textContent = '알림 활성화됨';
  } else if (Notification.permission === 'default') {
    el.textContent = enabled ? '알림을 켜면 브라우저 권한을 요청합니다' : '알림 비활성화됨';
  } else {
    el.textContent = '알림 비활성화됨';
  }
}

// ── 페이지 라우터 ──────────────────────────────────
const pages = {};

function showPage(name, skipHash) {
  if (name !== 'result') stopResourceMonitor();
  document.querySelectorAll('[data-page]').forEach(el => {
    el.classList.toggle('hidden', el.dataset.page !== name);
  });
  document.querySelectorAll('nav a').forEach(a => {
    a.classList.toggle('active', a.dataset.nav === name);
  });
  if (!skipHash) {
    const hash = name === 'result' && window._currentProjectId
      ? `#result/${window._currentProjectId}`
      : `#${name}`;
    if (location.hash !== hash) {
      const prevIsDashboard = !location.hash || location.hash === '#dashboard';
      if (name === 'dashboard') {
        // 대시보드로 갈 때는 replace (스택 안 쌓음)
        history.replaceState(null, '', hash);
      } else if (prevIsDashboard) {
        // 대시보드에서 나갈 때만 push (뒤로가기 1번에 대시보드로)
        history.pushState(null, '', hash);
      } else {
        // 비대시보드 → 비대시보드: replace (스택 안 쌓음)
        history.replaceState(null, '', hash);
      }
    }
  }
  if (pages[name]) pages[name]();
}

document.querySelectorAll('nav a[data-nav]').forEach(a => {
  a.addEventListener('click', e => {
    e.preventDefault();
    showPage(a.dataset.nav);
  });
});

// ── 테마 ──────────────────────────────────────────
const THEME_ICONS = { auto: '🌗', light: '☀️', dark: '🌙' };

function setTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('theme', theme);
  const cur = document.getElementById('theme-current');
  if (cur) cur.textContent = THEME_ICONS[theme] || '🌗';
  document.querySelectorAll('#theme-menu button').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.themeVal === theme);
  });
}

function toggleThemeMenu() {
  document.getElementById('theme-menu')?.classList.toggle('hidden');
}

document.getElementById('theme-menu')?.addEventListener('click', e => {
  const btn = e.target.closest('[data-theme-val]');
  if (btn) {
    setTheme(btn.dataset.themeVal);
    document.getElementById('theme-menu')?.classList.add('hidden');
  }
});

document.addEventListener('click', e => {
  if (!e.target.closest('#theme-dropdown')) {
    document.getElementById('theme-menu')?.classList.add('hidden');
  }
});

// ── 초기화 ────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  setTheme(localStorage.getItem('theme') || 'auto');
  loadApiStatus();
  const hash = location.hash.slice(1);
  // 대시보드를 히스토리 베이스로 깔기 (뒤로가기 시 항상 대시보드)
  history.replaceState(null, '', '#dashboard');
  if (hash.startsWith('result/')) {
    window._currentProjectId = hash.split('/')[1];
    showPage('result');
  } else if (['guide', 'settings'].includes(hash)) {
    showPage(hash);
  } else {
    showPage('dashboard');
  }
});

window.addEventListener('popstate', (e) => {
  // 모달/라이트박스가 열려있으면 닫기만 (pushState로 열림)
  const lightbox = document.getElementById('lightbox');
  if (lightbox && !lightbox.classList.contains('hidden')) {
    closeLightbox();
    return;
  }
  const modal = document.getElementById('create-modal');
  if (modal && !modal.classList.contains('hidden')) {
    closeCreateModal();
    return;
  }
  // 그 외 뒤로가기 — 대시보드로 이동
  showPage('dashboard', true);
});

async function loadApiStatus() {
  const data = await API.get('/keys/status');
  const bar = document.getElementById('api-status-bar');
  if (!bar) return;
  const items = [
    { key: 'gemini', label: 'Gemini' },
    { key: 'suno', label: 'Suno' },
  ];
  const allOk = items.every(({ key }) => data[key]);
  if (allOk) {
    bar.innerHTML = '';
    bar.style.display = 'none';
    return;
  }
  bar.style.display = '';
  const details = items.filter(({ key }) => !data[key]).map(({ key, label }) =>
    `<span class="api-dot-label"><span class="dot dot-err"></span>${label} 미연결</span>`
  ).join('');
  bar.innerHTML = `<div class="api-status-row">
    <span class="api-status-title">🟡 API 미연결</span>
    <span class="api-status-dots">${details}</span>
  </div>`;
}

// ── 리소스 모니터 (스텝 카드 내부 인라인) ─────────────
let _resMonitorTimer = null;
let _resMonitorStep = 0;

function _resLevel(pct) {
  if (pct >= 85) return 'high';
  if (pct >= 50) return 'mid';
  return 'low';
}

function _resHtml() {
  return `<div class="res-inline" id="res-inline">
    <div class="res-item">
      <div class="res-header"><span class="res-label">GPU</span><span class="res-value" data-r="gpu">—</span></div>
      <div class="res-bar"><div class="res-bar-fill res-gpu" data-r="gpu-bar"></div></div>
      <div class="res-detail" data-r="gpu-d"></div>
    </div>
    <div class="res-item">
      <div class="res-header"><span class="res-label">VRAM</span><span class="res-value" data-r="vram">—</span></div>
      <div class="res-bar"><div class="res-bar-fill res-vram" data-r="vram-bar"></div></div>
      <div class="res-detail" data-r="vram-d"></div>
    </div>
    <div class="res-item">
      <div class="res-header"><span class="res-label">CPU</span><span class="res-value" data-r="cpu">—</span></div>
      <div class="res-bar"><div class="res-bar-fill res-cpu" data-r="cpu-bar"></div></div>
    </div>
    <div class="res-item">
      <div class="res-header"><span class="res-label">RAM</span><span class="res-value" data-r="ram">—</span></div>
      <div class="res-bar"><div class="res-bar-fill res-ram" data-r="ram-bar"></div></div>
      <div class="res-detail" data-r="ram-d"></div>
    </div>
  </div>`;
}

function _resSet(attr, text, pct) {
  const el = document.querySelector(`[data-r="${attr}"]`);
  if (!el) return;
  el.textContent = text;
  if (pct !== undefined) el.dataset.level = _resLevel(pct);
}
function _resBar(attr, pct) {
  const el = document.querySelector(`[data-r="${attr}"]`);
  if (el) el.style.width = pct + '%';
}

async function _fetchResourceStats() {
  try {
    const d = await API.get('/system/stats');
    if (d.gpu) {
      _resSet('gpu', d.gpu.util + '%', d.gpu.util);
      _resBar('gpu-bar', d.gpu.util);
      const el = document.querySelector('[data-r="gpu-d"]');
      if (el) el.textContent = d.gpu.temp + '°C';

      const vp = Math.round(d.gpu.mem_used / d.gpu.mem_total * 100);
      _resSet('vram', vp + '%', vp);
      _resBar('vram-bar', vp);
      const ve = document.querySelector('[data-r="vram-d"]');
      if (ve) ve.textContent = (d.gpu.mem_used/1024).toFixed(1) + '/' + (d.gpu.mem_total/1024).toFixed(0) + 'G';
    }
    _resSet('cpu', d.cpu.percent + '%', d.cpu.percent);
    _resBar('cpu-bar', d.cpu.percent);
    _resSet('ram', d.ram.percent + '%', d.ram.percent);
    _resBar('ram-bar', d.ram.percent);
    const re = document.querySelector('[data-r="ram-d"]');
    if (re) re.textContent = d.ram.used + '/' + d.ram.total + 'G';
  } catch (e) {}
}

function _ensureResInStep(stepNo) {
  if (_resMonitorStep === stepNo && document.getElementById('res-inline')) return;
  // 기존 위치에서 제거
  document.getElementById('res-inline')?.remove();
  // 새 스텝에 삽입
  const body = document.querySelector(`#step-${stepNo} .step-body`);
  if (!body) return;
  const bar = body.querySelector('.progress-bar');
  if (bar) bar.insertAdjacentHTML('afterend', _resHtml());
  else body.insertAdjacentHTML('beforeend', _resHtml());
  _resMonitorStep = stepNo;
}

function startResourceMonitor(stepNo) {
  if (stepNo) _ensureResInStep(stepNo);
  _fetchResourceStats();
  if (_resMonitorTimer) clearInterval(_resMonitorTimer);
  _resMonitorTimer = setInterval(_fetchResourceStats, 2000);
}

function moveResourceMonitor(stepNo) {
  _ensureResInStep(stepNo);
}

function stopResourceMonitor() {
  if (_resMonitorTimer) {
    clearInterval(_resMonitorTimer);
    _resMonitorTimer = null;
  }
  document.getElementById('res-inline')?.remove();
  _resMonitorStep = 0;
}
