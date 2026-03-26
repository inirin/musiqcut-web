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
  const pad = n => String(n).padStart(2,'0');
  const fmtAbs = t => {
    if (!t) return '';
    const d = new Date(t + 'Z');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  };
  const fmtRel = t => {
    if (!t) return '';
    const diff = Math.floor((Date.now() - new Date(t + 'Z')) / 60000);
    if (diff < 1) return '방금 전';
    if (diff < 60) return `${diff}분 전`;
    if (diff < 1440) return `${Math.floor(diff/60)}시간 전`;
    return fmtAbs(t);
  };
  const fmtFuture = ms => {
    if (ms <= 0) return '곧 시작';
    const m = Math.floor(ms / 60000);
    if (m < 60) return `${m}분 후`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    return rm > 0 ? `${h}시간 ${rm}분 후` : `${h}시간 후`;
  };

  let html = '';

  // ── 상단: 현재 상태 (생성 중 or 다음 생성예정) ──
  html += '<div class="gen-primary">';
  if (sched.running_project) {
    let nextInfo = '';
    if (sched.last_created_at) {
      const nextMs = new Date(sched.last_created_at + 'Z').getTime() + (sched.interval_hours || 2) * 3600000;
      if (nextMs <= Date.now()) {
        nextInfo = ' · 다음 생성 : 완성 후 즉시';
      } else {
        nextInfo = ` · 다음 생성 : ${fmtFuture(nextMs - Date.now())}`;
      }
    }
    html += `<span class="gen-dot running"></span>
      <span class="gen-primary-text">생성 중 : ${sched.running_project.title}<span class="gen-abs-time">${nextInfo}</span></span>`;
  } else if (sched.last_created_at) {
    const nextMs = new Date(sched.last_created_at + 'Z').getTime() + (sched.interval_hours || 2) * 3600000;
    const remaining = nextMs - Date.now();
    if (remaining <= 0) {
      html += `<span class="gen-dot pending"></span>
        <span class="gen-primary-text">다음 생성 : 곧 시작</span>`;
    } else {
      const nextAbs = fmtAbs(new Date(nextMs).toISOString().replace('Z',''));
      html += `<span class="gen-dot pending"></span>
        <span class="gen-primary-text">다음 생성 : ${fmtFuture(remaining)} <span class="gen-abs-time">(${nextAbs})</span></span>`;
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
        <div class="gen-hist-time">${fmtRel(sched.last_created_at)}</div>
        <div class="gen-hist-abs">${fmtAbs(sched.last_created_at)}</div>
      </div>`;
    }
    if (sched.last_success_at) {
      html += `<div class="gen-hist-item">
        <div class="gen-hist-label">마지막 완성</div>
        <div class="gen-hist-time">${fmtRel(sched.last_success_at)}</div>
        <div class="gen-hist-abs">${fmtAbs(sched.last_success_at)}</div>
      </div>`;
    }
    if (sched.last_failure_at) {
      const reason = sched.last_failure_reason || '';
      html += `<div class="gen-hist-item failure">
        <div class="gen-hist-label">마지막 실패</div>
        <div class="gen-hist-time">${fmtRel(sched.last_failure_at)}</div>
        <div class="gen-hist-abs">${fmtAbs(sched.last_failure_at)}</div>
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
