pages['settings'] = loadSettings;

async function loadSettings() {
  const status = await API.get('/keys/status');

  updateKeyIndicator('suno', status.suno);

  if (status.gemini) {
    const el = document.getElementById('key-status-gemini');
    if (el) {
      const cnt = status.imagen_count || 1;
      el.textContent = `● ${cnt}개 연결됨`;
      el.style.color = 'var(--success)';
    }
    // 마스킹된 키 표시
    const keys = status.imagen_keys_masked || [];
    if (keys[0]) document.getElementById('key-input-gemini').value = keys[0];
    else if (status.gemini_masked) document.getElementById('key-input-gemini').value = status.gemini_masked;
    if (keys[1]) document.getElementById('key-input-gemini2').value = keys[1];
    if (keys[2]) document.getElementById('key-input-gemini3').value = keys[2];
  } else {
    updateKeyIndicator('gemini', false);
  }
  if (status.suno_masked) {
    document.getElementById('key-input-suno').value = status.suno_masked;
  }

  updateNotifStatus();

  // 자동 생성 스케줄 로드
  try {
    const scheds = await API.get('/feedback/schedules');
    const sched = scheds.generation;
    if (sched) {
      const toggle = document.getElementById('schedule-gen-toggle');
      const interval = document.getElementById('schedule-gen-interval');
      const status = document.getElementById('schedule-gen-status');
      if (toggle) toggle.checked = !!sched.enabled;
      if (interval) interval.value = sched.interval_hours || 2;
      if (status) status.innerHTML = renderGenStatus(sched);
    }
  } catch {}

  // 플랫폼 연동 상태 로드
  _loadAllPlatformSettings();
}

async function _loadAllPlatformSettings() {
  const platforms = [
    { key: 'youtube', dot: 'yt-status-dot', desc: 'yt-account-desc', actions: 'yt-actions',
      label: 'YouTube Shorts', endpoint: '/upload/account' },
    { key: 'instagram', dot: 'ig-status-dot', desc: 'ig-account-desc', actions: 'ig-actions',
      label: 'Instagram Reels', endpoint: '/upload/instagram/account' },
    { key: 'tiktok', dot: 'tt-status-dot', desc: 'tt-account-desc', actions: 'tt-actions',
      label: 'TikTok', endpoint: '/upload/tiktok/account' },
  ];

  for (const p of platforms) {
    try {
      const data = await API.get(p.endpoint);
      const dot = document.getElementById(p.dot);
      const desc = document.getElementById(p.desc);
      const actions = document.getElementById(p.actions);
      if (data.connected) {
        if (dot) { dot.textContent = '● 연결됨'; dot.style.color = 'var(--success)'; }
        if (desc) desc.textContent = data.channel_title || p.label;
        if (actions) actions.innerHTML = `<button class="btn btn-secondary btn-sm" onclick="disconnectPlatform('${p.key}')">연결 해제</button>`;
      } else {
        if (dot) { dot.textContent = '● 미연결'; dot.style.color = 'var(--text-muted)'; }
        if (actions) actions.innerHTML = `<button class="btn btn-primary btn-sm" onclick="connectPlatform('${p.key}')">연결하기</button>`;
      }
    } catch {}
  }

  // 플랫폼별 자동 업로드 토글
  try {
    const autoData = await API.get('/upload/auto-upload');
    for (const p of ['youtube', 'instagram', 'tiktok']) {
      const toggle = document.getElementById(`auto-upload-${p}`);
      if (toggle) toggle.checked = !!autoData[p];
    }
  } catch {}
}

// 하위 호환
function _loadYouTubeSettings() { _loadAllPlatformSettings(); }

async function togglePlatformAutoUpload(platform, enabled) {
  try {
    await fetch(`/api/upload/auto-upload/${platform}?enabled=${enabled}`, { method: 'POST' });
    const label = {youtube:'YouTube',instagram:'Instagram',tiktok:'TikTok'}[platform];
    showToast(`${label} 자동 업로드 ${enabled ? '활성화' : '비활성화'}`, 'success');
  } catch (e) {
    showToast('설정 변경 실패', 'error');
  }
}

function setPlaceholder(id, text) {
  const el = document.getElementById(id);
  if (el) el.placeholder = text;
}

function updateKeyIndicator(key, ok) {
  const el = document.getElementById(`key-status-${key}`);
  if (!el) return;
  el.textContent = ok ? '● 연결됨' : '● 미설정';
  el.style.color = ok ? 'var(--success)' : 'var(--error)';
}

async function saveKey(api) {
  let body = {};

  if (api === 'gemini') {
    const val = document.getElementById('key-input-gemini')?.value.trim();
    if (!val) return toast('API 키를 입력해주세요', 'error');
    body.gemini_api_key = val;

  } else if (api === 'suno') {
    const val = document.getElementById('key-input-suno')?.value.trim();
    if (!val) return toast('API 키를 입력해주세요', 'error');
    body.suno_api_key = val;

  }

  try {
    const result = await API.post('/keys/save', body);
    if (result.ok) {
      toast(`저장 완료: ${result.saved.join(', ')}`, 'success');
      clearInputs(api);
      await loadSettings();
    } else {
      const msg = result.error || result.detail || JSON.stringify(result);
      toast(`저장 실패: ${msg}`, 'error');
    }
  } catch (e) {
    toast(`서버 연결 실패 — 서버가 실행 중인지 확인하세요`, 'error');
  }
}

function clearInputs(api) {
  const ids = {
    gemini: ['key-input-gemini'],
    suno: ['key-input-suno'],
  };
  (ids[api] || []).forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
}

async function saveGeminiKeys() {
  const k1 = document.getElementById('key-input-gemini')?.value.trim();
  const k2 = document.getElementById('key-input-gemini2')?.value.trim();
  const k3 = document.getElementById('key-input-gemini3')?.value.trim();
  if (!k1) return toast('주키를 입력해주세요', 'error');
  const keys = [k1, k2, k3].filter(Boolean).join(',');
  try {
    const result = await API.post('/keys/save', {
      gemini_api_key: k1,
      imagen_api_keys: keys
    });
    if (result.ok) {
      toast(`Gemini 키 ${keys.split(',').length}개 저장 완료`, 'success');
      document.getElementById('key-input-gemini').value = '';
      document.getElementById('key-input-gemini2').value = '';
      document.getElementById('key-input-gemini3').value = '';
      await loadSettings();
    } else {
      toast(`저장 실패: ${result.error || ''}`, 'error');
    }
  } catch { toast('서버 연결 실패', 'error'); }
}

async function toggleSchedule(type, enabled) {
  const prefix = type === 'generation' ? 'gen' : 'fb';
  const interval = parseFloat(document.getElementById(`schedule-${prefix}-interval`)?.value || '2');
  const label = '작품 생성';
  const result = await API.post(`/feedback/schedule?schedule_type=${type}&enabled=${enabled}&interval_hours=${interval}`, {});
  if (result.ok) {
    toast(enabled ? `${label} 스케줄 활성화 (${interval}시간)` : `${label} 스케줄 비활성화`, 'success');
    await loadSettings();
  }
}


function renderGenStatus(sched) {
  if (!sched.enabled) return '';

  let html = '';

  // ── 상단: 현재 상태 (생성 중 or 다음 생성예정) ──
  html += '<div class="gen-primary">';
  if (sched.running_project) {
    let nextInfo = '';
    if (sched.last_created_at) {
      const nMs = schedNextMs(sched);
      nextInfo = nMs <= Date.now()
        ? ' · 다음 생성 : 완성 후 즉시'
        : ` · 다음 생성 : ${schedFmtFuture(nMs - Date.now())}`;
    }
    html += `<span class="gen-dot running"></span>
      <span class="gen-primary-text">생성 중 : ${sched.running_project.title}<span class="gen-abs-time">${nextInfo}</span></span>`;
  } else if (sched.last_created_at) {
    const nMs = schedNextMs(sched);
    const remaining = nMs - Date.now();
    if (remaining <= 0) {
      html += `<span class="gen-dot pending"></span>
        <span class="gen-primary-text">다음 생성 : 곧 시작</span>`;
    } else {
      const nextAbs = schedFmtAbs(new Date(nMs).toISOString().replace('Z',''));
      html += `<span class="gen-dot pending"></span>
        <span class="gen-primary-text">다음 생성 : ${schedFmtFuture(remaining)} <span class="gen-abs-time">(${nextAbs})</span></span>`;
    }
  } else {
    html += `<span class="gen-dot pending"></span>
      <span class="gen-primary-text">대기 중</span>`;
  }
  html += '</div>';

  // ── 하단: 이력 3열 ──
  const hasHistory = sched.last_created_at || sched.last_success_at || sched.last_failure_at;
  if (hasHistory) {
    html += '<div class="gen-history">';
    if (sched.last_created_at) {
      html += `<div class="gen-hist-item">
        <div class="gen-hist-label">마지막 생성</div>
        <div class="gen-hist-time">${schedFmtRel(sched.last_created_at)}</div>
        <div class="gen-hist-abs">${schedFmtAbs(sched.last_created_at)}</div>
      </div>`;
    }
    if (sched.last_success_at) {
      html += `<div class="gen-hist-item">
        <div class="gen-hist-label">마지막 완성</div>
        <div class="gen-hist-time">${schedFmtRel(sched.last_success_at)}</div>
        <div class="gen-hist-abs">${schedFmtAbs(sched.last_success_at)}</div>
      </div>`;
    }
    if (sched.last_failure_at) {
      const reason = sched.last_failure_reason || '';
      html += `<div class="gen-hist-item failure">
        <div class="gen-hist-label">마지막 실패</div>
        <div class="gen-hist-time">${schedFmtRel(sched.last_failure_at)}</div>
        <div class="gen-hist-abs">${schedFmtAbs(sched.last_failure_at)}</div>
        ${reason ? `<div class="gen-reason">사유 : ${reason}</div>` : ''}
      </div>`;
    }
    html += '</div>';
  }

  return html;
}

async function testKey(api) {
  const btn = document.getElementById(`test-btn-${api}`);
  if (btn) btn.disabled = true;
  toast(`${api} 연결 테스트 중...`);

  const result = await API.post(`/keys/test/${api}`, {});
  if (result.ok) {
    toast(`${api} 연결 성공!`, 'success');
    updateKeyIndicator(api, true);
  } else {
    toast(`${api} 실패: ${result.error}`, 'error');
  }
  if (btn) btn.disabled = false;
}
