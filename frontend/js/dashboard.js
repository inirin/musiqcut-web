// ── 대시보드 ──────────────────────────────────────
pages['dashboard'] = loadDashboard;

function _dashScheduleText(sched) {
  const fmtFuture = ms => {
    if (ms <= 0) return '곧 시작';
    const m = Math.floor(ms / 60000);
    if (m < 60) return `${m}분 후`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    return rm > 0 ? `${h}시간 ${rm}분 후` : `${h}시간 후`;
  };
  if (sched.running_project) {
    const pid = sched.running_project.id;
    const title = sched.running_project.title;
    return `<span class="gen-dot running"></span>
      <span class="gen-primary-text">자동 생성 중 : ${title}</span>`;
  }
  } else if (sched.last_created_at) {
    const nextMs = new Date(sched.last_created_at + 'Z').getTime() + (sched.interval_hours || 2) * 3600000;
    const remaining = nextMs - Date.now();
    if (remaining <= 0) {
      return `<span class="gen-dot pending"></span>
        <span class="gen-primary-text">자동 생성 예정 : 곧 시작</span>`;
    }
    return `<span class="gen-dot pending"></span>
      <span class="gen-primary-text">자동 생성 예정 : ${fmtFuture(remaining)}</span>`;
  }
  return '';
}

async function loadDashboard() {
  // 스케줄러 상태 (생성 중 / 다음 생성)
  try {
    const scheds = await API.get('/feedback/schedules');
    const sched = scheds.generation;
    const el = document.getElementById('dash-schedule-status');
    if (el && sched?.enabled) {
      const isRunning = !!sched.running_project;
      const pid = isRunning ? sched.running_project.id : '';
      el.innerHTML = `<div class="gen-primary dash-schedule-box${isRunning ? ' clickable' : ''}" style="margin-bottom:8px"${isRunning ? ` onclick="window._currentProjectId='${pid}';showPage('result')"` : ''}>${_dashScheduleText(sched)}</div>`;
    } else if (el) {
      el.innerHTML = '';
    }
  } catch {}

  const projects = await API.get('/projects');
  const grid = document.getElementById('gallery-grid');

  if (!projects.length) {
    grid.innerHTML = `
      <div class="empty-state" style="grid-column:1/-1">
        <div class="icon">🎭</div>
        <p>아직 만든 작품이 없어요.<br>첫 번째 뮤직비디오를 만들어봐요!</p>
        <button class="btn btn-primary" onclick="openCreateModal()">+ 새 작품 만들기</button>
      </div>`;
    return;
  }

  grid.innerHTML = projects.map(p => {
    const thumb = p.status === 'done'
      ? `<img src="/storage/projects/${p.id}/images/scene_01.png" onerror="this.parentElement.innerHTML='🎬'">`
      : p.status === 'running' ? '⏳' : p.status === 'failed' ? '❌' : '🎭';
    return `
      <div class="project-card" onclick="viewProject('${p.id}')">
        <div class="project-thumb">${typeof thumb === 'string' && thumb.startsWith('<') ? thumb : `<span>${thumb}</span>`}</div>
        <div class="card-badge">${statusBadge(p.status)}</div>
        <div class="project-info">
          <div class="project-title">${p.title || p.theme}</div>
          <div class="project-meta"><span>${formatDate(p.created_at)}</span></div>
        </div>
      </div>`;
  }).join('');
}

function viewProject(id) {
  window._currentProjectId = id;
  showPage('result');
}

pages['result'] = loadResult;

let _lastLoadedProjectId = null;

async function loadResult() {
  const id = window._currentProjectId;
  if (!id) return showPage('dashboard');

  // WebSocket 활성 중이어도 DB 체크는 허용 (에러 감지 + 재시도 버튼 표시용)

  const [project, steps] = await Promise.all([
    API.get(`/projects/${id}`),
    API.get(`/projects/${id}/steps`)
  ]);

  const latest = {};
  steps.forEach(s => { latest[s.step_no] = s; });

  // 기본 정보
  document.getElementById('result-title').textContent = project.title || project.theme;
  document.getElementById('result-theme').textContent = project.theme || '';
  // 트렌드 영감 — mood에서 [트렌드 힌트: ...] 파싱 (자동 생성만)
  const inspiredEl = document.getElementById('project-inspired');
  const hintMatch = (project.mood || '').match(/\[트렌드 힌트:\s*(.+?)\]$/);
  if (hintMatch && inspiredEl) {
    const hint = hintMatch[1].trim();
    inspiredEl.innerHTML = `<div class="step1-section inspired">
      <div class="step1-section-header"><span class="step1-badge inspired">INSPIRED BY</span></div>
      <div class="step1-char-desc step1-collapse" onclick="this.classList.toggle('open')">${hint}</div>
    </div>`;
    inspiredEl.style.display = '';
  } else if (inspiredEl) {
    inspiredEl.innerHTML = '';
    inspiredEl.style.display = 'none';
  }
  // art_style은 lyrics.json에서 로드 (아래 step1 블록에서 처리)
  document.getElementById('result-art-style').textContent = '';
  // 곡 길이/장면 수: 오디오 파일에서 파생
  const dur = project.actual_duration;
  document.getElementById('result-duration').textContent =
    dur ? `${Math.round(dur)}초 / ${Math.max(3, Math.ceil(dur / 5))}장면` : '음악 생성 후 자동 결정';
  document.getElementById('result-date').textContent = formatDate(project.created_at);
  document.getElementById('result-status').innerHTML = statusBadge(project.status);

  // 완성 영상 (프로젝트 변경 시에만 src 교체 — 영상 깜빡임 방지)
  const videoUrl = `/storage/projects/${id}/video/final.mp4`;
  const videoCard = document.getElementById('result-video-card');
  const videoEl = document.getElementById('result-video');
  if (project.status === 'done') {
    const videoOk = await fetch(videoUrl, { method: 'HEAD' }).then(r => r.ok).catch(() => false);
    if (videoOk) {
      if (_lastLoadedProjectId !== id || videoCard.classList.contains('hidden')) {
        videoEl.src = videoUrl + `?t=${Date.now()}`;
        document.getElementById('result-download').href = videoUrl;
      }
      videoCard.classList.remove('hidden');
      if (typeof loadFeedbackList === 'function') loadFeedbackList(id);
    } else {
      videoCard.classList.add('hidden');
    }
  } else {
    videoCard.classList.add('hidden');
  }

  // 파이프라인 스텝 UI — 프로젝트 변경 시에만 리셋 (폴링 시 깜빡임 방지)
  if (_lastLoadedProjectId !== id) {
    resetProgressUI();
  }
  _lastLoadedProjectId = id;

  for (const s of Object.values(latest)) {
    if (s.status === 'done') {
      const data = JSON.parse(s.output_data || '{}');
      if (s.started_at) data.started_at = s.started_at;
      if (s.finished_at) data.finished_at = s.finished_at;
      updateStepUI(s.step_no, 'done', s.step_name, data);

      // step1 가사 + 아트 스타일 + 캐릭터/보컬
      if (s.step_no === 1) {
        try {
          const lyricsData = await fetch(`/storage/projects/${id}/lyrics.json?t=${Date.now()}`).then(r => r.json());
          const lyricsEl = document.getElementById('step-1-lyrics');
          if (lyricsEl && !lyricsEl.textContent && lyricsData.lyrics) {
            lyricsEl.textContent = lyricsData.lyrics;
            lyricsEl.classList.remove('hidden');
          }
          const artEl = document.getElementById('result-art-style');
          if (artEl && !artEl.textContent && lyricsData.art_style) {
            artEl.textContent = lyricsData.art_style;
          }
          // 캐릭터/보컬 — Step 1에 표시
          const metaEl = document.getElementById('step-1-meta');
          if (metaEl && !metaEl.innerHTML.trim()) {
            const html = renderStep1Meta(lyricsData);
            if (html) { metaEl.innerHTML = html; metaEl.classList.remove('hidden'); }
          }
        } catch {}
      }
      // step2 오디오 + music_prompt
      if (s.step_no === 2) {
        const audio = document.getElementById('step-2-audio');
        if (audio && !audio.src.includes(`/projects/${id}/music/`)) {
          audio.src = `/storage/projects/${id}/music/output.mp3?t=${Date.now()}`;
        }
        if (audio) audio.classList.remove('hidden');
        renderStep2Lyrics(id, null);
      }
      // step3 이미지
      if (s.step_no === 3 && data.image_count) {
        const urls = Array.from({ length: data.image_count }, (_, i) =>
          `/storage/projects/${id}/images/scene_${String(i+1).padStart(2,'0')}.png`);
        _renderPreviews('image-previews', 'image', urls);
      }
      // step4 클립
      if (s.step_no === 4 && data.clip_slots) {
        _renderClipSlots(document.getElementById('clip-previews'), data.clip_slots);
      } else if (s.step_no === 4 && data.clip_count) {
        const urls = Array.from({ length: data.clip_count }, (_, i) =>
          `/storage/projects/${id}/clips/clip_${String(i+1).padStart(2,'0')}.mp4`);
        _renderPreviews('clip-previews', 'video', urls);
      }
      // step5 표현 후처리 (현재 패스스루)
    } else if (s.status === 'failed') {
      markStepFailed(s.step_no, s.error_msg);
    } else if (s.status === 'running') {
      const runData = JSON.parse(s.output_data || '{}');
      if (s.started_at) runData.started_at = s.started_at;
      const msg = (runData.current && runData.total)
        ? `${s.step_name} 중... ${runData.current}/${runData.total}`
        : '실행 중...';
      // DB에 clip_slots가 있으면 그대로 사용, 없으면 current 기반으로 생성
      if (runData.current > 0) {
        if (s.step_no === 3) {
          runData.image_urls = Array.from({ length: runData.current }, (_, i) =>
            `/storage/projects/${id}/images/scene_${String(i+1).padStart(2,'0')}.png`);
        } else if (s.step_no === 4 && !runData.clip_slots) {
          // clip_slots 없으면 레거시 순차 URL 생성
          runData.clip_urls = Array.from({ length: runData.current }, (_, i) =>
            `/storage/projects/${id}/clips/clip_${String(i+1).padStart(2,'0')}.mp4`);
        }
      }
      updateStepUI(s.step_no, 'running', msg, runData);
    }
  }

  // 완료/실패 시 재시도 버튼 + 상태 리셋
  const isRunning = project.status === 'running';
  if (!isRunning) {
    _pipelineRunning = false;
    if (_wsHandle) { _wsHandle.close(); _wsHandle = null; }
    _addCompletedRetryButtons();
  }

  // 리소스 모니터: STEP 4(로컬 GPU) 실행 중일 때만 표시
  if (isRunning) {
    const runningStep = Object.values(latest).find(s => s.status === 'running');
    if (runningStep && runningStep.step_no === 4) {
      startResourceMonitor(4);
    } else {
      stopResourceMonitor();
    }
  } else {
    stopResourceMonitor();
  }

  // done 스텝에 피드백 입력 + 기존 피드백 표시
  if (typeof showStepFeedbackInputs === 'function') showStepFeedbackInputs();
  if (typeof loadAllStepFeedbacks === 'function') loadAllStepFeedbacks(id);

  // 실행 중이면 WebSocket 자동 연결
  if (isRunning && !_wsHandle) {
    _pipelineRunning = true;
    _pipelineProjectId = id;
    _wsHandle = API.connectWS(id, handlePipelineEvent);
  }
}

async function retryProject(id) {
  retryFromStep(id, 0);
}

async function deleteProject() {
  const id = window._currentProjectId;
  if (!id) return;
  if (!confirm('이 작품을 삭제하시겠습니까?\n생성된 모든 파일이 함께 삭제됩니다.')) return;
  try {
    await API.delete(`/projects/${id}`);
    toast('작품이 삭제되었습니다', 'success');
    window._currentProjectId = null;
    showPage('dashboard');
  } catch (e) {
    toast(`삭제 실패: ${e.message}`, 'error');
  }
}
