// ── 영상 제목 복사 ──────────────────────────────
function _copyVideoTitle() {
  const el = document.getElementById('result-video-title');
  if (!el) return;
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

// ── 업로드 ──────────────────────────────────────
function _goConnectPlatform(label) {
  alert('설정에서 ' + label + ' 계정을 연결해주세요');
  showPage('settings');
  // 해당 플랫폼 카드로 스크롤
  const map = {YouTube:'yt',Instagram:'ig',TikTok:'tt'};
  const prefix = map[label];
  if (prefix) {
    setTimeout(() => {
      const el = document.getElementById(prefix + '-status-dot');
      if (el) el.closest('.card').scrollIntoView({behavior:'smooth', block:'center'});
    }, 100);
  }
}

// ── 업로드 버튼 (멀티 플랫폼) ───────────────────
const _PLATFORMS = [
  { key: 'youtube', label: 'YouTube', icon: '▶' },
  { key: 'instagram', label: 'Instagram', icon: '▶' },
  { key: 'tiktok', label: 'TikTok', icon: '▶' },
];

async function _loadUploadButtons(projectId) {
  const container = document.getElementById('upload-buttons');
  if (!container) return;

  try {
    const [ytAcct, igAcct, ttAcct, statusData] = await Promise.all([
      fetch('/api/upload/account').then(r => r.json()),
      fetch('/api/upload/instagram/account').then(r => r.json()),
      fetch('/api/upload/tiktok/account').then(r => r.json()),
      fetch(`/api/upload/${projectId}/status`).then(r => r.json()),
    ]);

    const accounts = { youtube: ytAcct, instagram: igAcct, tiktok: ttAcct };
    const uploads = statusData.uploads || [];
    // 다운로드는 HTML에서 별도 렌더링
    const dlBtn = document.getElementById('result-download');
    if (dlBtn) dlBtn.href = `/storage/projects/${projectId}/video/final.mp4?t=${Date.now()}`;
    let html = '';
    let hasAny = false;

    for (const p of _PLATFORMS) {
      hasAny = true;
      const connected = accounts[p.key].connected;
      const u = connected
        ? (uploads.find(u => u.platform === p.key && u.status !== 'failed') ||
           uploads.find(u => u.platform === p.key))
        : null;

      if (!connected) {
        html += `<button type="button" class="btn btn-secondary upload-not-connected" data-platform="${p.label}" style="opacity:0.6">${p.label}<br>업로드</button>`;
      } else if (_uploadingPlatforms.has(p.key)) {
        html += `<button type="button" class="btn btn-secondary" disabled>${p.label}<br>⏳ 업로드 중</button>`;
      } else if (u && u.status === 'done') {
        html += `<button type="button" class="btn btn-secondary upload-reupload" data-pid="${projectId}" data-platform="${p.key}" data-url="${u.platform_url}">✅ ${p.label}<br>업로드됨</button>`;
      } else if (u && u.status === 'uploading') {
        html += `<button type="button" class="btn btn-secondary" disabled>${p.label}<br>⏳ 업로드 중</button>`;
      } else if (u && u.status === 'failed') {
        html += `<button type="button" class="btn btn-secondary upload-action" data-pid="${projectId}" data-platform="${p.key}">${p.label}<br>🔄 재시도</button>`;
      } else {
        html += `<button type="button" class="btn btn-secondary upload-action" data-pid="${projectId}" data-platform="${p.key}">${p.label}<br>업로드</button>`;
      }
    }

    if (uploads.some(u => u.status === 'uploading')) {
      setTimeout(() => _loadUploadButtons(projectId), 3000);
    }

    container.innerHTML = hasAny ? html : '';

    // 이벤트 바인딩 — 미연결
    container.querySelectorAll('.upload-not-connected').forEach(btn => {
      btn.addEventListener('click', () => _goConnectPlatform(btn.dataset.platform));
    });
    // 이벤트 바인딩 — 업로드/재시도
    container.querySelectorAll('.upload-action').forEach(btn => {
      btn.addEventListener('click', () => _uploadToPlatform(btn.dataset.pid, btn.dataset.platform));
    });
    // 이벤트 바인딩 — 재업로드 (업로드 완료된 플랫폼)
    container.querySelectorAll('.upload-reupload').forEach(btn => {
      btn.addEventListener('click', () => {
        const url = btn.dataset.url;
        const plat = btn.dataset.platform;
        const choice = confirm(`${plat} 재업로드하시겠습니까?\n\n[확인] 재업로드\n[취소] 기존 게시물 열기\n\n현재 URL: ${url}`);
        if (choice) {
          _uploadToPlatform(btn.dataset.pid, plat, true);
        } else {
          window.open(url, '_blank');
        }
      });
    });
  } catch (e) {
    console.error('_loadUploadButtons error:', e);
    container.innerHTML = '';
  }
}

let _uploadingPlatforms = new Set();

async function _uploadToPlatform(projectId, platform, reupload = false) {
  if (_uploadingPlatforms.has(platform)) return;
  const label = _PLATFORMS.find(p => p.key === platform)?.label || platform;
  if (!reupload && !confirm(`${label}에 이 작품을 업로드하시겠습니까?`)) return;

  _uploadingPlatforms.add(platform);
  _loadUploadButtons(projectId);

  try {
    const reuploadParam = reupload ? '?reupload=true' : '';
    const res = await fetch(`/api/upload/${projectId}/upload/${platform}${reuploadParam}`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      toast(`${label} 업로드를 시작합니다`, 'success');
      // 완료될 때까지 폴링
      const pollUpload = setInterval(async () => {
        try {
          const st = await fetch(`/api/upload/${projectId}/status`).then(r => r.json());
          const u = (st.uploads || []).find(u => u.platform === platform);
          if (u && u.status === 'done') {
            clearInterval(pollUpload);
            _uploadingPlatforms.delete(platform);
            _loadUploadButtons(projectId);
            toast(`${label} 업로드 완료!`, 'success');
            sendNotification(`${label} 업로드 완료`, _pipelineTitle || '영상이 업로드되었습니다.');
          } else if (u && u.status === 'failed') {
            clearInterval(pollUpload);
            _uploadingPlatforms.delete(platform);
            _loadUploadButtons(projectId);
            toast(`${label} 업로드 실패`, 'error');
            sendNotification(`${label} 업로드 실패`, _pipelineTitle || '업로드 중 오류가 발생했습니다.');
          }
        } catch {}
      }, 3000);
    } else {
      toast(data.error || '업로드 실패', 'error');
      _uploadingPlatforms.delete(platform);
      _loadUploadButtons(projectId);
    }
  } catch (e) {
    toast('업로드 요청 실패', 'error');
    _uploadingPlatforms.delete(platform);
    _loadUploadButtons(projectId);
  }
}

// ── 공통 유틸 ─────────────────────────────────
const _NON_VOCAL = NON_VOCAL; // app.js 전역 참조

function renderStep1Meta(data) {
  const chars = data.characters || [];
  const vocalist = chars[0];
  const supporting = chars.slice(1);
  let html = '<div class="step1-sections">';

  // 보컬리스트 섹션
  if (vocalist || data.vocal_style) {
    html += '<div class="step1-section vocalist">';
    html += '<div class="step1-section-header"><span class="step1-badge vocalist">VOCALIST</span>';
    if (vocalist) html += `<span class="step1-char-name">${vocalist.name}</span>`;
    if (data.vocal_style) html += `<span class="step1-vocal-style">${data.vocal_style}</span>`;
    html += '</div>';
    if (vocalist?.description_en) {
      html += `<div class="step1-char-desc step1-collapse" onclick="this.classList.toggle('open')">${vocalist.description_en}</div>`;
    }
    html += '</div>';
  }

  // 등장인물 섹션
  if (supporting.length) {
    html += '<div class="step1-section">';
    html += '<div class="step1-section-header"><span class="step1-badge">CAST</span></div>';
    html += '<div class="step1-cast-grid">';
    for (const ch of supporting) {
      html += `<div class="step1-cast-item">
        <div class="step1-char-name">${ch.name}</div>
        <div class="step1-char-desc step1-collapse" onclick="this.classList.toggle('open')">${ch.description_en}</div>
      </div>`;
    }
    html += '</div></div>';
  }

  html += '</div>';
  return html;
}

async function regenScene(includeImage) {
  const id = window._currentProjectId;
  if (!id) return;
  const sceneNo = _lbIndex + 1;
  const label = includeImage ? '이미지+클립' : '클립';
  if (!confirm(`장면 ${sceneNo}의 ${label}을 재생성하시겠습니까?`)) return;
  closeLightbox();
  toast(`장면 ${sceneNo} ${label} 재생성 시작...`);
  const result = await API.post(`/pipeline/${id}/regenerate-scene/${sceneNo}?include_image=${includeImage}`, {});
  if (result.ok && result.queued) {
    // 파이프라인 실행 중 — 파일만 삭제됨, 해당 슬롯을 대기중으로 업데이트
    const idx = sceneNo - 1;
    // Step 4 클립 슬롯
    const clipContainer = document.getElementById('clip-previews');
    if (clipContainer) {
      const slots = clipContainer.querySelectorAll('.clip-slot, .scene-thumb');
      if (slots[idx]) {
        const imgUrl = slots[idx].querySelector('img')?.src || slots[idx].querySelector('video')?.poster || '';
        const badge = slots[idx].querySelector('.clip-badge')?.outerHTML || '';
        slots[idx].outerHTML = `<div class="clip-slot clip-pending" data-lb-idx="${idx}">
          <img src="${imgUrl}">
          <div class="clip-overlay"><span class="clip-wait-text">대기중</span></div>
          ${badge}
        </div>`;
      }
    }
    // Step 3 이미지 슬롯 (이미지+클립 재생성 시)
    if (includeImage) {
      const imgContainer = document.getElementById('image-previews');
      if (imgContainer) {
        const imgSlots = imgContainer.querySelectorAll('.scene-thumb');
        if (imgSlots[idx]) {
          imgSlots[idx].innerHTML = `<div class="clip-overlay"><span class="clip-wait-text">대기중</span></div><span class="scene-num">${sceneNo}</span>`;
        }
      }
    }
    toast(`장면 ${sceneNo} 재생성 대기 중 (현재 클립 완료 후 자동 실행)`, 'success');
  } else if (result.ok) {
    resetProgressUI(result.from_step || 3);
    if (_wsHandle) _wsHandle.close();
    _pipelineRunning = true; _updateAbortButton(true);
    _pipelineProjectId = id;
    _wsHandle = API.connectWS(id, handlePipelineEvent);
    document.getElementById('result-status').innerHTML = statusBadge('running');
    toast(`장면 ${sceneNo} 재생성 시작!`, 'success');
  } else {
    toast(result.error || '재생성 실패', 'error');
  }
}

function isLipSync(meta) {
  /** _has_vocal (Whisper 타이밍) 우선 → vocal_lines 텍스트 폴백 */
  const hasVocal = meta._has_vocal !== undefined ? meta._has_vocal
    : (meta.vocal_lines || []).some(l => l.trim() && !_NON_VOCAL.has(l.trim()));
  const isVocalist = meta.is_vocalist !== undefined ? meta.is_vocalist : false;
  return hasVocal && isVocalist;
}

// hasVocalInScene 제거 — 뱃지는 isLipSync로 통일

// ── 새 프로젝트 생성 모달 + 파이프라인 실행 ─────────

const selectedLength = 'short';  // 숏폼 전용 (최대 60초)

function openCreateModal() {
  document.getElementById('create-modal').classList.remove('hidden');
  history.pushState({ modal: 'create' }, '');
}

function closeCreateModal() {
  document.getElementById('create-modal').classList.add('hidden');
}

document.getElementById('create-form')?.addEventListener('submit', async e => {
  e.preventDefault();
  const theme = document.getElementById('theme-input').value.trim();
  if (!theme) return toast('테마를 입력해주세요', 'error');

  closeCreateModal();
  _pipelineRunning = true;
  resetProgressUI();
  document.getElementById('result-title').textContent = '작품 생성 중...';
  document.getElementById('result-video-card')?.classList.add('hidden');
  // showPage 대신 직접 페이지 전환 (loadResult가 구 상태 덮어쓰는 것 방지)
  document.querySelectorAll('[data-page]').forEach(el => {
    el.classList.toggle('hidden', el.dataset.page !== 'result');
  });
  startPipeline(theme, 'auto');
});

// ── 파이프라인 실행 ───────────────────────────────

const STEP_NAMES = ['', '스토리/컨셉 생성', '음악 생성', '이미지 생성 (Imagen)', '영상 클립 생성 (Wan S2V)', '최종 영상 합성'];
const STEP_ICONS = ['', '✍️', '🎵', '🖼️', '🎞️', '🎬'];

let currentProjectId = null;
let _pipelineRunning = false;
let _wsHandle = null;  // WebSocket 연결 핸들

// ── 스텝 경과 시간 (DB 기반) ────────────────────────
const _stepStartTimes = {};  // { stepNo: isoString } — running 스텝의 시작시각 캐시
let _elapsedTickTimer = null;

function _fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}초`;
  const m = Math.floor(s / 60);
  return `${m}분 ${s % 60}초`;
}

function _setStepElapsed(step, text) {
  const el = document.querySelector(`#step-${step} .step-elapsed`);
  if (!el) return;
  el.textContent = text;
  el.classList.remove('hidden');
}

function _clearStepElapsed(step) {
  const el = document.querySelector(`#step-${step} .step-elapsed`);
  if (el) { el.textContent = ''; el.classList.add('hidden'); }
}

function _parseUTC(iso) {
  if (!iso) return NaN;
  // +00:00 또는 Z가 있으면 그대로, 없으면 Z 추가
  const d = new Date(/[Z+]/.test(iso) ? iso : iso + 'Z');
  return d.getTime();
}

function _tickRunningElapsed() {
  for (const [step, iso] of Object.entries(_stepStartTimes)) {
    const ms = Date.now() - _parseUTC(iso);
    if (isNaN(ms) || ms < 0) continue;
    _setStepElapsed(Number(step), _fmtElapsed(ms) + ' 경과');
  }
}

function _startElapsedTick() {
  if (_elapsedTickTimer) return;
  _elapsedTickTimer = setInterval(_tickRunningElapsed, 1000);
}

function _stopElapsedTick() {
  if (_elapsedTickTimer) { clearInterval(_elapsedTickTimer); _elapsedTickTimer = null; }
  Object.keys(_stepStartTimes).forEach(k => delete _stepStartTimes[k]);
}

async function startPipeline(theme, mood) {
  _pipelineRunning = true;
  _pipelineProjectId = null;
  resetProgressUI();

  // 1) POST로 파이프라인 시작 → project_id 받기
  const result = await API.post('/pipeline/run', {
    theme, mood, length: selectedLength
  });

  if (!result.ok) {
    toast(result.error || '파이프라인 시작 실패', 'error');
    _pipelineRunning = false;
    return;
  }

  // 2) project_id 설정
  const projectId = result.project_id;
  _pipelineProjectId = projectId;
  currentProjectId = projectId;
  window._currentProjectId = projectId;

  // 3) WebSocket 연결 → 실시간 이벤트 수신
  if (_wsHandle) _wsHandle.close();
  _wsHandle = API.connectWS(projectId, handlePipelineEvent);
}

let _pipelineProjectId = null;
let _pipelineTitle = '';

function handlePipelineEvent(evt) {
  if (evt.type === 'ping' || evt.type === 'info') return;

  // 다른 프로젝트 페이지로 이동했으면 UI 업데이트 무시
  if (_pipelineProjectId && window._currentProjectId !== _pipelineProjectId) return;

  if (evt.type === 'step') {
    const d = evt.data || {};
    if (evt.started_at) d.started_at = evt.started_at;
    if (evt.finished_at) d.finished_at = evt.finished_at;
    updateStepUI(evt.step, evt.status, evt.message, d);
    // Step 1 시작 알림
    if (evt.status === 'running' && evt.step === 1) {
      sendNotification('작품 생성 시작', _pipelineTitle || '새 작품을 생성합니다.');
    }
    if (evt.status === 'running' && evt.step === 4) startResourceMonitor(evt.step);
    if (evt.status === 'done' && evt.step === 4) stopResourceMonitor();
    // 클립 재생성 완료 알림
    if (evt.step === 4 && d?.clip_slots && evt.message?.includes('재생성')) {
      const sceneMatch = evt.message.match(/장면\s*(\d+)/);
      const sceneNo = sceneMatch ? sceneMatch[1] : '?';
      sendNotification('클립 재생성 완료', `${_pipelineTitle || '작품'} — 장면 ${sceneNo}`);
    }
  }

  if (evt.type === 'complete') {
    _pipelineRunning = false;
    _pipelineProjectId = null;
    if (_wsHandle) { _wsHandle.close(); _wsHandle = null; }
    stopResourceMonitor();
    _updateAbortButton(false);
    toast('뮤직비디오 완성!', 'success');
    sendNotification('영상 완성!', _pipelineTitle || '뮤직비디오가 생성되었습니다.', () => {
      showPage('result');
    });
    // 영상 카드 표시
    const id = window._currentProjectId;
    const videoUrl = `/storage/projects/${id}/video/final.mp4`;
    document.getElementById('result-video').src = videoUrl + `?t=${Date.now()}`;
    // result-download는 upload-buttons 그리드 안에서 렌더링
    document.getElementById('result-video-card')?.classList.remove('hidden');
    document.getElementById('result-title').textContent = '완성!';
    document.getElementById('result-status').innerHTML = statusBadge('done');
    _loadUploadButtons(id);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  if (evt.type === 'aborted') {
    _pipelineRunning = false;
    _pipelineProjectId = null;
    if (_wsHandle) { _wsHandle.close(); _wsHandle = null; }
    stopResourceMonitor();
    _updateAbortButton(false);
    // running/pending 슬롯의 스피너 제거
    document.querySelectorAll('.clip-slot.clip-running, .clip-slot.clip-pending').forEach(el => {
      el.className = 'clip-slot clip-pending';
      const overlay = el.querySelector('.clip-overlay');
      if (overlay) overlay.innerHTML = '<span class="clip-wait-text">중단됨</span>';
    });
    document.getElementById('result-status').innerHTML = statusBadge('failed');
    toast('파이프라인이 중단되었습니다.', 'success');
  }

  if (evt.type === 'error') {
    _pipelineRunning = false;
    _pipelineProjectId = null;
    if (_wsHandle) { _wsHandle.close(); _wsHandle = null; }
    stopResourceMonitor();
    _updateAbortButton(false);
    markStepFailed(evt.step, evt.message);
    document.getElementById('result-status').innerHTML = statusBadge('failed');
    toast(`오류: ${evt.message}`, 'error');
    if (evt.step > 0) {
      sendNotification('작업 실패', `${_pipelineTitle ? _pipelineTitle + ' — ' : ''}STEP ${evt.step} 오류`);
    }
  }
}

function resetProgressUI(fromStep = 1) {
  _stopElapsedTick();
  for (let i = fromStep; i <= 5; i++) {
    const el = document.getElementById(`step-${i}`);
    if (!el) continue;
    el.className = 'step';
    el.querySelector('.step-icon').textContent = STEP_ICONS[i];
    el.querySelector('.step-msg').textContent = '대기 중...';
    const bar = el.querySelector('.progress-bar-fill');
    if (bar) bar.style.width = '0%';
    el.querySelector('.retry-inline')?.remove();
    const elapsed = el.querySelector('.step-elapsed');
    if (elapsed) { elapsed.textContent = ''; elapsed.classList.add('hidden'); }
  }
  if (fromStep <= 1) {
    const lyricsEl = document.getElementById('step-1-lyrics');
    if (lyricsEl) { lyricsEl.textContent = ''; lyricsEl.classList.add('hidden'); }
    const metaEl = document.getElementById('step-1-meta');
    if (metaEl) { metaEl.innerHTML = ''; metaEl.classList.add('hidden'); }
  }
  if (fromStep <= 2) {
    const audioEl = document.getElementById('step-2-audio');
    if (audioEl) { audioEl.src = ''; audioEl.classList.add('hidden'); }
    const promptEl = document.getElementById('step-2-prompt');
    if (promptEl) { promptEl.innerHTML = ''; promptEl.classList.add('hidden'); }
  }
  if (fromStep <= 3) {
    const imgEl = document.getElementById('image-previews');
    if (imgEl) { imgEl.innerHTML = ''; imgEl._prevKey = null; }
  }
  if (fromStep <= 4) {
    const clipEl = document.getElementById('clip-previews');
    if (clipEl) { clipEl.innerHTML = ''; clipEl._prevDoneKey = null; clipEl._prevKey = null; }
  }
}

function updateStepUI(step, status, message, data) {
  const el = document.getElementById(`step-${step}`);
  if (!el) return;

  el.className = `step ${status}`;
  el.querySelector('.step-msg').textContent = message;

  // 경과 시간 (DB/SSE 기반)
  if (status === 'running' && data?.started_at && !_stepStartTimes[step]) {
    _stepStartTimes[step] = data.started_at;
    _tickRunningElapsed();
    _startElapsedTick();
  } else if (status === 'done' || status === 'failed') {
    delete _stepStartTimes[step];
    if (data?.started_at && data?.finished_at) {
      const ms = _parseUTC(data.finished_at) - _parseUTC(data.started_at);
      if (!isNaN(ms) && ms >= 0) _setStepElapsed(step, _fmtElapsed(ms) + ' 소요됨');
    }
    if (Object.keys(_stepStartTimes).length === 0) _stopElapsedTick();
  }

  // STEP 1: 스토리 (done 시)
  if (step === 1 && data?.title) _pipelineTitle = data.title;
  if (step === 1 && data?.lyrics) {
    const lyricsEl = document.getElementById('step-1-lyrics');
    if (lyricsEl) { lyricsEl.textContent = data.lyrics; lyricsEl.classList.remove('hidden'); }
    if (data.art_style) {
      document.getElementById('result-art-style').textContent = data.art_style;
    }
    // 보컬리스트 + 등장인물
    const metaEl = document.getElementById('step-1-meta');
    if (metaEl && (data.vocal_style || data.characters?.length)) {
      metaEl.innerHTML = renderStep1Meta(data);
      metaEl.classList.remove('hidden');
    }
  }
  // STEP 2: 오디오 (running/done 모두)
  if (step === 2 && data?.audio_url) {
    const audio = document.getElementById('step-2-audio');
    const audioUrl = data.audio_url + (data.audio_url.includes('?') ? '&' : '?') + 't=' + Date.now();
    if (audio && !audio.src.includes(data.audio_url.split('?')[0])) {
      audio.src = audioUrl; audio.classList.remove('hidden');
    }
  }
  // STEP 2: Whisper 가사
  if (step === 2 && (data?.whisper_lyrics?.length || status === 'done')) {
    renderStep2Lyrics(window._currentProjectId, data?.whisper_lyrics);
  }

  if (status === 'done') {
    el.querySelector('.step-icon').textContent = '✅';
    const bar = el.querySelector('.progress-bar-fill');
    if (bar) bar.style.width = '100%';
  }

  if (data?.current && data?.total) {
    const pct = Math.round((data.current / data.total) * 100);
    const bar = el.querySelector('.progress-bar-fill');
    if (bar) bar.style.width = pct + '%';
  }

  if (step === 3 && data?.image_urls) {
    _renderPreviews('image-previews', 'image', data.image_urls);
  }

  if (step === 4 && data?.clip_slots) {
    _renderClipSlots('clip-previews', data.clip_slots);
  } else if (step === 4 && data?.clip_urls) {
    _renderPreviews('clip-previews', 'video', data.clip_urls);
  }

}

// ── STEP 2: Whisper 추출 가사 표시 ──
function markStepFailed(step, msg) {
  delete _stepStartTimes[step];
  const el = document.getElementById(`step-${step}`);
  if (!el) return;
  el.className = 'step failed';
  el.querySelector('.step-icon').textContent = '❌';
  el.querySelector('.step-msg').textContent = `오류: ${msg}`;
  _addRetryButton(el, step);
}

function _addRetryButton(stepEl, step) {
  const body = stepEl.querySelector('.step-body');
  if (!body || body.querySelector('.retry-inline')) return;
  const wrap = document.createElement('div');
  wrap.className = 'retry-inline';
  wrap.style.cssText = 'margin-top:8px; padding-top:8px; border-top:1px dashed var(--border); display:flex; gap:8px; flex-wrap:wrap;';
  const id = window._currentProjectId;

  if (step === 3 || step === 4) {
    const isFailed = stepEl.classList.contains('failed');
    if (isFailed) {
      // 실패: 이어서 + 처음부터
      const btnResume = document.createElement('button');
      btnResume.className = 'btn btn-primary btn-sm';
      btnResume.textContent = `▶ 이어서 생성`;
      btnResume.onclick = (e) => { e.stopPropagation(); if (confirm(`이어서 생성하시겠습니까?`)) retryFromStep(id, step); };
      wrap.appendChild(btnResume);
      const btnReset = document.createElement('button');
      btnReset.className = 'btn btn-secondary btn-sm';
      btnReset.textContent = `↺ 처음부터 재생성`;
      btnReset.onclick = (e) => { e.stopPropagation(); if (confirm(`STEP ${step}을 처음부터 재생성하시겠습니까?`)) retryFromStep(id, step, true); };
      wrap.appendChild(btnReset);
    } else {
      // 완성: 재시도 하나만 (다른 스텝과 동일 스타일)
      const btn = document.createElement('button');
      btn.className = 'btn btn-primary btn-sm';
      btn.textContent = `↺ STEP ${step}부터 재시도`;
      btn.onclick = (e) => { e.stopPropagation(); if (confirm(`STEP ${step}부터 재시도하시겠습니까?`)) retryFromStep(id, step, true); };
      wrap.appendChild(btn);
    }
  } else {
    const btn = document.createElement('button');
    btn.className = 'btn btn-primary btn-sm';
    btn.textContent = `↺ STEP ${step}부터 재시도`;
    btn.onclick = (e) => { e.stopPropagation(); if (confirm(`STEP ${step}부터 재시도하시겠습니까?`)) retryFromStep(id, step); };
    wrap.appendChild(btn);
  }

  body.appendChild(wrap);
}

function _addCompletedRetryButtons() {
  for (let i = 1; i <= 5; i++) {
    const el = document.getElementById(`step-${i}`);
    if (!el) continue;
    if (el.classList.contains('done') || el.classList.contains('failed')) {
      _addRetryButton(el, i);
    }
  }
}

async function retryFromStep(id, step, reset = false) {
  _pipelineRunning = true;
  _pipelineProjectId = id;
  window._currentProjectId = id;
  currentProjectId = id;
  resetProgressUI(step || 1);
  document.getElementById('result-title').textContent = `STEP ${step || '?'}부터 재시도 중`;
  document.getElementById('result-status').innerHTML = statusBadge('running');
  document.getElementById('result-video-card')?.classList.add('hidden');
  // showPage 대신 직접 페이지 전환 (loadResult 호출 방지 — 구 상태 덮어쓰기 차단)
  document.querySelectorAll('[data-page]').forEach(el => {
    el.classList.toggle('hidden', el.dataset.page !== 'result');
  });

  // 1) POST로 재시도 시작
  const resetParam = reset ? '&reset=true' : '';
  const result = await API.post(`/pipeline/resume/${id}?from_step=${step}${resetParam}`, {});
  if (!result.ok) {
    toast(result.error || '재시도 실패', 'error');
    _pipelineRunning = false;
    return;
  }

  // 2) WebSocket 연결 → 실시간 이벤트 수신
  if (_wsHandle) _wsHandle.close();
  _wsHandle = API.connectWS(id, handlePipelineEvent);
}

// ── 클립 슬롯 렌더 (장면 순서 유지, 미생성은 이미지 블러) ──
function _renderClipSlots(containerId, slots) {
  const container = document.getElementById(containerId);
  if (!container) return;

  // done 클립 변화 시에만 전체 재렌더 (스피너 깜빡임 방지)
  const doneKey = slots.filter(s => s.status === 'done').map(s => s.url).join('|');
  const needsFullRender = container._prevDoneKey !== doneKey;
  container._prevDoneKey = doneKey;

  if (!needsFullRender) {
    // running/pending 상태만 CSS로 업데이트 (innerHTML 교체 없이)
    const slotEls = container.querySelectorAll('.clip-slot');
    slots.forEach((slot, i) => {
      if (slotEls[i] && slot.status !== 'done') {
        const isRunning = slot.status === 'running';
        slotEls[i].className = `clip-slot ${isRunning ? 'clip-running' : 'clip-pending'}`;
        const overlay = slotEls[i].querySelector('.clip-overlay');
        if (overlay) overlay.innerHTML = isRunning
          ? '<div class="clip-slot-spinner"></div>'
          : '<span class="clip-wait-text">대기중</span>';
      }
    });
    return;
  }

  // 라이트박스용: 모든 슬롯 (done=영상, pending/running=이미지)
  container._lbType = 'mixed';
  container._lbUrls = slots.map(s => s.url || s.image_url || '');
  container._lbMeta = slots;

  const bust = `?t=${Date.now()}`;
  function _modelBadge(slot) {
    return isLipSync(slot) ? '<span class="clip-badge echo">보컬</span>' : '';
  }

  container.innerHTML = slots.map((slot, i) => {
    const badge = _modelBadge(slot);
    const imgUrl = slot.image_url || '';
    if (slot.status === 'done') {
      return `<div class="clip-slot clip-done" data-lb-idx="${i}">
        <video src="${slot.url}${bust}" muted poster="${imgUrl}${bust}" preload="metadata"></video>
        <div class="clip-play-icon">▶</div>
        ${badge}
      </div>`;
    }
    if (slot.status === 'running') {
      return `<div class="clip-slot clip-running" data-lb-idx="${i}">
        <img src="${imgUrl}${bust}">
        <div class="clip-overlay"><div class="clip-slot-spinner"></div></div>
        ${badge}
      </div>`;
    }
    return `<div class="clip-slot clip-pending" data-lb-idx="${i}">
      <img src="${imgUrl}${bust}">
      <div class="clip-overlay"><span class="clip-wait-text">대기중</span></div>
      ${badge}
    </div>`;
  }).join('');
}

// ── 썸네일 렌더 + 이벤트 위임 ──────────────────
async function _renderPreviews(containerId, type, urls) {
  const container = document.getElementById(containerId);
  if (!container) return;

  container._lbType = type;
  container._lbUrls = urls;
  container._lbMeta = null;  // lyrics.json 폴백 사용

  // 이미지 타입은 보컬 뱃지를 위해 lyrics.json 먼저 로드
  let sceneMeta = null;
  if (type === 'image' && window._currentProjectId) {
    try {
      const ld = await fetch(`/storage/projects/${window._currentProjectId}/lyrics.json?t=${Date.now()}`).then(r => r.json());
      sceneMeta = ld.scenes || null;
    } catch {}
  }

  // URL + 뱃지 상태가 동일하면 재렌더 안 함 (폴링 깜빡임 방지)
  const badgeKey = sceneMeta ? sceneMeta.map(s => isLipSync(s) ? '1' : '0').join('') : '';
  const key = urls.join('|') + badgeKey;
  if (container._prevKey === key) return;
  container._prevKey = key;

  const bust = `?t=${Date.now()}`;
  if (type === 'video') {
    container.innerHTML = urls.map((url, i) => {
      const poster = url.replace('/clips/clip_', '/images/scene_').replace('.mp4', '.png');
      return `<video src="${url}${bust}" muted poster="${poster}${bust}" preload="metadata" data-lb-idx="${i}" onerror="this.style.opacity=0.3"></video>`;
    }).join('');
  } else {
    container.innerHTML = urls.map((url, i) => {
      let badge = '';
      if (sceneMeta && sceneMeta[i] && isLipSync(sceneMeta[i])) {
        badge = '<span class="clip-badge echo">보컬</span>';
      }
      return `<div class="scene-thumb" data-lb-idx="${i}"><img src="${url}${bust}" onerror="this.style.opacity=0.3"><span class="scene-num">${i+1}</span>${badge}</div>`;
    }).join('');
  }
}

// 이벤트 위임: scene-previews 컨테이너 클릭 → 라이트박스
document.addEventListener('click', e => {
  const el = e.target.closest('[data-lb-idx]');
  if (!el) return;
  const container = el.closest('.scene-previews');
  if (!container || !container._lbUrls) return;
  e.preventDefault();
  e.stopPropagation();
  openLightbox(container._lbType, container._lbUrls, parseInt(el.dataset.lbIdx), container._lbMeta);
});

// ── 라이트박스 ───────────────────────────────
let _lbItems = [];
let _lbIndex = 0;
let _lbType = 'image';
let _lbMeta = null;  // 클립 메타데이터 (start_sec, end_sec, vocal_lines)
let _lbAudioInterval = null;

function openLightbox(type, urls, index, meta) {
  _lbType = type;
  _lbItems = urls;
  _lbIndex = index;
  _lbMeta = meta || null;
  _showLightboxItem();
  document.getElementById('lightbox').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  history.pushState({ modal: 'lightbox' }, '');
}

function closeLightbox(e) {
  if (e && e.target !== e.currentTarget && !e.target.classList.contains('lightbox-close')) return;
  document.getElementById('lightbox').classList.add('hidden');
  document.body.style.overflow = '';
  const vid = document.getElementById('lightbox-video');
  vid.onplay = null; vid.onpause = null; vid.onseeked = null;
  vid.pause(); vid.src = '';
  const aud = document.getElementById('lightbox-audio');
  if (aud) { aud.onplay = null; aud.onpause = null; aud.pause(); aud.src = ''; }
  if (_lbAudioInterval) { clearInterval(_lbAudioInterval); _lbAudioInterval = null; }
}

function navigateLightbox(dir) {
  event.stopPropagation();
  _lbIndex = (_lbIndex + dir + _lbItems.length) % _lbItems.length;
  _showLightboxItem();
}

async function _showLightboxItem() {
  try { await _showLightboxItemInner(); } catch(e) {
    const cap = document.getElementById('lightbox-caption');
    if (cap) cap.textContent = '[ERR] ' + e.message;
  }
}
async function _showLightboxItemInner() {
  const img = document.getElementById('lightbox-img');
  const vid = document.getElementById('lightbox-video');
  const cap = document.getElementById('lightbox-caption');
  const infoEl = document.getElementById('lightbox-info');
  const lyricsEl = document.getElementById('lightbox-lyrics');
  const audioEl = document.getElementById('lightbox-audio');
  const url = _lbItems[_lbIndex];

  // 이전 상태 초기화
  if (_lbAudioInterval) { clearInterval(_lbAudioInterval); _lbAudioInterval = null; }
  if (audioEl) { audioEl.pause(); audioEl.removeAttribute('src'); }
  if (infoEl) { infoEl.classList.add('hidden'); lyricsEl.innerHTML = ''; }

  const cleanUrl = url.split('?')[0];
  const bust = `?t=${Date.now()}`;
  const curMeta = _lbMeta ? _lbMeta[_lbIndex] : null;
  const isImage = _lbType === 'image' || (!cleanUrl.endsWith('.mp4') && !curMeta?.status);
  const clipStatus = isImage ? 'done' : (curMeta?.status || (cleanUrl.endsWith('.mp4') ? 'done' : 'pending'));
  const overlay = document.getElementById('lightbox-status-overlay');

  if (clipStatus === 'done' && cleanUrl.endsWith('.mp4')) {
    // 완성된 영상 클립 — 오디오는 별도 audio element로 싱크 재생
    img.classList.add('hidden');
    vid.classList.remove('hidden');
    vid.src = cleanUrl + bust;
    vid.muted = true;
    if (overlay) overlay.classList.add('hidden');
  } else if (isImage) {
    // STEP 3 이미지
    vid.classList.add('hidden');
    vid.pause(); vid.src = '';
    img.classList.remove('hidden');
    img.src = cleanUrl + bust;
    if (overlay) overlay.classList.add('hidden');
  } else {
    vid.classList.add('hidden');
    vid.pause(); vid.src = '';
    img.classList.remove('hidden');
    img.src = ((curMeta?.image_url || cleanUrl).split('?')[0]) + bust;
    // 상태 오버레이
    if (overlay) {
      overlay.classList.remove('hidden');
      if (clipStatus === 'running') {
        overlay.innerHTML = '<div class="lb-spinner"></div><span class="status-text">클립 생성중...</span>';
      } else {
        overlay.innerHTML = '<span class="status-text">대기중</span>';
      }
    }
  }

  // 메타데이터: clip_slots 또는 lyrics.json에서
  let meta = _lbMeta ? _lbMeta[_lbIndex] : null;
  let scenesData = null;
  if (window._currentProjectId) {
    try {
      scenesData = await fetch(`/storage/projects/${window._currentProjectId}/lyrics.json`).then(r => r.json());
    } catch (e) { console.warn('[Lightbox] lyrics.json fetch failed:', e); }
  }
  // lyrics.json scenes에서 항상 병합 (clip_slots에 누락된 필드 보완)
  if (scenesData?.scenes?.[_lbIndex]) {
    const sceneMeta = scenesData.scenes[_lbIndex];
    if (!meta) {
      meta = sceneMeta;
    } else {
      for (const key of ['_has_vocal','is_vocalist','image_prompt','description','vocal_lines','shot_type']) {
        if ((meta[key] === undefined || meta[key] === null || (Array.isArray(meta[key]) && !meta[key].length)) && sceneMeta[key] !== undefined) {
          meta[key] = sceneMeta[key];
        }
      }
    }
  }
  if (!meta && scenesData?.scenes?.[_lbIndex]) {
    meta = scenesData.scenes[_lbIndex];
  }
  if (meta && infoEl) {
    infoEl.classList.remove('hidden');

    const lines = (meta.vocal_lines || []).filter(l => l.trim());
    const shotType = meta.shot_type || 'medium';
    const isLipSyncClip = isLipSync(meta);
    const audioRow = audioEl ? audioEl.closest('.lightbox-audio-row') : null;
    if (audioRow) audioRow.style.display = 'none';  // 오디오 UI 숨김 (싱크 재생은 유지)
    const startSec = meta.start_sec || 0;
    const endSec = meta.end_sec || (startSec + (meta.duration || 0));

    // 텍스트 표시: 타입별 라벨 + 내용
    function _infoHtml() {
      const badge = isLipSyncClip ? '<span class="lb-badge echo">보컬</span> ' : '';
      const shotLabel = `<span class="lb-shot">${shotType}</span>`;
      const vocalText = lines.filter(l => !_NON_VOCAL.has(l.trim())).join(' / ');

      if (_lbType === 'image') {
        const imgBadge = isLipSync(meta) ? '<span class="lb-badge echo">보컬</span> ' : '';
        return `${imgBadge}${shotLabel} <span class="lb-label">이미지 프롬프트</span><br><span class="lb-prompt">${meta.image_prompt || meta.description || ''}</span>`;
      }
      // 모든 클립: 자막 + 실제 프롬프트 (영어)
      let parts = `${badge}${shotLabel}`;
      if (vocalText) {
        parts += `<br><span class="lb-vocal">"${vocalText}"</span>`;
      }
      const promptText = meta.image_prompt || meta.description || '';
      if (promptText) {
        parts += `<br><span class="lb-prompt">${promptText}</span>`;
      }
      if (!vocalText && !promptText) {
        parts += ` <span class="lb-label">(instrumental)</span>`;
      }
      return parts;
    }
    lyricsEl.innerHTML = _infoHtml();

    if (_lbType === 'image') {
      if (audioRow) audioRow.style.display = 'none';
    } else if (!window._currentProjectId || endSec <= startSec) {
      if (audioRow) audioRow.style.display = 'none';
    } else {
      const audioSrc = `/storage/projects/${window._currentProjectId}/music/output.mp3?t=${Date.now()}`;

      audioEl.src = audioSrc;
      audioEl.currentTime = startSec;
      vid.onplay = () => {
        audioEl.currentTime = startSec + vid.currentTime;
        audioEl.play().catch(() => {});
      };
      vid.onpause = () => { audioEl.pause(); };
      vid.onseeked = () => { audioEl.currentTime = startSec + vid.currentTime; };
      audioEl.onplay = () => {
        vid.currentTime = audioEl.currentTime - startSec;
        vid.play().catch(() => {});
      };
      audioEl.onpause = () => { if (!vid.paused) vid.pause(); };
      _lbAudioInterval = setInterval(() => {
        if (audioEl.currentTime >= endSec) {
          audioEl.pause(); vid.pause();
          clearInterval(_lbAudioInterval); _lbAudioInterval = null;
        }
      }, 100);
    }
  } else if (infoEl) {
    infoEl.classList.add('hidden');
  }

  cap.textContent = `${_lbIndex + 1} / ${_lbItems.length}`;

  // 재생성 버튼 표시 (Step 3 이상 완료된 작품)
  const regenWrap = document.getElementById('lightbox-regen');
  if (regenWrap) {
    const step3 = document.getElementById('step-3');
    const hasImages = step3 && (step3.classList.contains('done') || step3.classList.contains('running'));
    regenWrap.style.display = hasImages ? '' : 'none';
  }

  // 라이트박스 피드백 로드
  if (typeof loadLightboxFeedbacks === 'function' && window._currentProjectId) {
    const isClip = _lbType === 'video' || _lbType === 'mixed';
    loadLightboxFeedbacks(window._currentProjectId, isClip ? 4 : 3, _lbIndex + 1);
  }
}

document.addEventListener('keydown', e => {
  const lb = document.getElementById('lightbox');
  if (lb.classList.contains('hidden')) return;
  if (e.key === 'Escape') closeLightbox();
  if (e.key === 'ArrowLeft') navigateLightbox(-1);
  if (e.key === 'ArrowRight') navigateLightbox(1);
});
