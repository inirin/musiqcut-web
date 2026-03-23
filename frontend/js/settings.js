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
  } else {
    updateKeyIndicator('gemini', false);
  }
  if (status.suno_masked) {
    document.getElementById('key-input-suno').value = status.suno_masked;
  }

  updateNotifStatus();

  // 스케줄 설정 로드
  try {
    const scheds = await API.get('/feedback/schedules');
    for (const [type, sched] of Object.entries(scheds)) {
      const prefix = type === 'generation' ? 'gen' : 'fb';
      const toggle = document.getElementById(`schedule-${prefix}-toggle`);
      const interval = document.getElementById(`schedule-${prefix}-interval`);
      const status = document.getElementById(`schedule-${prefix}-status`);
      if (toggle) toggle.checked = !!sched.enabled;
      if (interval) interval.value = sched.interval_hours || (type === 'feedback' ? 6 : 2);
      if (status) {
        const last = sched.last_run_at ? ' · 마지막: ' + new Date(sched.last_run_at + 'Z').toLocaleString('ko-KR') : '';
        status.textContent = sched.enabled ? `활성 (${sched.interval_hours}시간 간격)${last}` : '비활성';
      }
    }
  } catch {}

  // 프롬프트 개선 이력
  if (typeof loadPromptHistory === 'function') loadPromptHistory();
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
  if (!k1) return toast('주키를 입력해주세요', 'error');
  const keys = [k1, k2].filter(Boolean).join(',');
  try {
    const result = await API.post('/keys/save', {
      gemini_api_key: k1,
      imagen_api_keys: keys
    });
    if (result.ok) {
      toast(`Gemini 키 ${keys.split(',').length}개 저장 완료`, 'success');
      document.getElementById('key-input-gemini').value = '';
      document.getElementById('key-input-gemini2').value = '';
      await loadSettings();
    } else {
      toast(`저장 실패: ${result.error || ''}`, 'error');
    }
  } catch { toast('서버 연결 실패', 'error'); }
}

async function toggleSchedule(type, enabled) {
  const prefix = type === 'generation' ? 'gen' : 'fb';
  const interval = parseFloat(document.getElementById(`schedule-${prefix}-interval`)?.value || '2');
  const label = type === 'generation' ? '작품 생성' : '피드백 분석';
  const result = await API.post(`/feedback/schedule?schedule_type=${type}&enabled=${enabled}&interval_hours=${interval}`, {});
  if (result.ok) {
    toast(enabled ? `${label} 스케줄 활성화 (${interval}시간)` : `${label} 스케줄 비활성화`, 'success');
    await loadSettings();
  }
}

async function rollbackPrompt() {
  const result = await API.post('/feedback/rollback', {});
  toast(result.ok ? '롤백 완료!' : (result.error || '롤백 실패'), result.ok ? 'success' : 'error');
  if (typeof loadPromptHistory === 'function') loadPromptHistory();
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
