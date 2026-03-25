// ── 피드백 시스템 ──

async function submitFeedback(type) {
  const id = window._currentProjectId;
  if (!id) return;

  let content = null;
  if (type === 'comment') {
    content = document.getElementById('feedback-comment')?.value.trim();
    if (!content) return toast('피드백을 입력해주세요', 'error');
  }

  const result = await API.post('/feedback', {
    project_id: id,
    feedback_type: type,
    content: content
  });

  if (result.ok) {
    toast(type === 'comment' ? '피드백 등록!' : (type === 'like' ? '👍 감사합니다!' : '👎 피드백 반영할게요!'), 'success');
    if (type === 'comment') document.getElementById('feedback-comment').value = '';
    // 버튼 하이라이트
    document.querySelectorAll('.feedback-btn').forEach(b => b.classList.remove('active'));
    if (type !== 'comment') {
      document.querySelector(`.feedback-btn[data-type="${type}"]`)?.classList.add('active');
    }
    loadFeedbackList(id);
  }
}

async function submitStepFeedback(stepNo, btn) {
  const id = window._currentProjectId;
  if (!id) return;
  const input = btn.parentElement.querySelector('.step-fb-input');
  const content = input?.value.trim();
  if (!content) return toast('피드백을 입력해주세요', 'error');

  const result = await API.post('/feedback', {
    project_id: id,
    step_no: stepNo,
    feedback_type: 'comment',
    content: content
  });
  if (result.ok) {
    toast(`STEP ${stepNo} 피드백 등록!`, 'success');
    input.value = '';
    loadStepFeedbacks(id, stepNo);
    loadFeedbackList(id);
  }
}

async function loadStepFeedbacks(projectId, stepNo) {
  const container = document.querySelector(`.step-feedback[data-step="${stepNo}"] .step-fb-list`);
  if (!container) return;
  const feedbacks = await API.get(`/feedback?project_id=${projectId}`);
  const stepFbs = feedbacks.filter(f => f.step_no === stepNo);
  if (!stepFbs.length) { container.innerHTML = ''; return; }
  container.innerHTML = stepFbs.map(f => {
    const time = formatFbTime(f.created_at);
    const scene = f.scene_no ? `<span style="color:var(--accent);font-size:0.7rem">장면${f.scene_no}</span> ` : '';
    return `<div class="fb-item" style="padding:3px 0;font-size:0.78rem;display:flex;justify-content:space-between;align-items:center;gap:4px">
      <span style="min-width:0;overflow:hidden;text-overflow:ellipsis" id="step-fb-content-${f.id}">💬 ${scene}${f.content} <span style="color:var(--text-muted);font-size:0.7rem">${time}</span></span>
      <span class="fb-actions">
        <button onclick="editStepFb(${f.id}, '${projectId}', ${stepNo})" class="fb-action-btn">✏️</button>
        <button onclick="deleteStepFb(${f.id}, '${projectId}', ${stepNo})" class="fb-action-btn">🗑️</button>
      </span>
    </div>`;
  }).join('');
}

async function loadAllStepFeedbacks(projectId) {
  for (let i = 1; i <= 5; i++) {
    await loadStepFeedbacks(projectId, i);
  }
}

// done 상태 스텝에 피드백 입력 표시
function showStepFeedbackInputs() {
  document.querySelectorAll('.step-feedback').forEach(el => {
    const step = el.dataset.step;
    const stepEl = document.getElementById(`step-${step}`);
    if (stepEl && stepEl.classList.contains('done')) {
      el.classList.remove('hidden');
    }
  });
}

async function submitLightboxFeedback() {
  const id = window._currentProjectId;
  const input = document.getElementById('lightbox-feedback-input');
  if (!id || !input) return;
  const content = input.value.trim();
  if (!content) return toast('피드백을 입력해주세요', 'error');

  // 라이트박스에서 현재 보고 있는 아이템의 step/scene 판별
  const isClip = _lbType === 'video' || _lbType === 'mixed';
  const step_no = isClip ? 4 : 3;
  const scene_no = _lbIndex + 1;

  const result = await API.post('/feedback', {
    project_id: id,
    step_no: step_no,
    scene_no: scene_no,
    feedback_type: 'comment',
    content: content
  });

  if (result.ok) {
    toast(`장면 ${scene_no} 피드백 등록!`, 'success');
    input.value = '';
    loadFeedbackList(id);
    loadStepFeedbacks(id, step_no);
    loadLightboxFeedbacks(id, step_no, scene_no);
  }
}

async function loadLightboxFeedbacks(projectId, stepNo, sceneNo) {
  const container = document.getElementById('lightbox-fb-list');
  if (!container) return;
  const feedbacks = await API.get(`/feedback?project_id=${projectId}`);
  const sceneFbs = feedbacks.filter(f => f.step_no === stepNo && f.scene_no === sceneNo);
  if (!sceneFbs.length) { container.innerHTML = ''; return; }
  container.innerHTML = sceneFbs.map(f => {
    const time = formatFbTime(f.created_at);
    return `<div class="fb-item" id="lb-fb-${f.id}" style="padding:3px 0;font-size:0.78rem;display:flex;justify-content:space-between;align-items:center;gap:4px;color:#fff">
      <span style="min-width:0;overflow:hidden;text-overflow:ellipsis" id="lb-fb-content-${f.id}">💬 ${f.content} <span style="opacity:0.5;font-size:0.7rem">${time}</span></span>
      <span class="fb-actions">
        <button onclick="editLbFb(${f.id},'${projectId}',${stepNo},${sceneNo})" class="fb-action-btn">✏️</button>
        <button onclick="deleteFeedback(${f.id},'${projectId}');loadLightboxFeedbacks('${projectId}',${stepNo},${sceneNo});loadAllStepFeedbacks('${projectId}')" class="fb-action-btn">🗑️</button>
      </span>
    </div>`;
  }).join('');
}

function editLbFb(fbId, projectId, stepNo, sceneNo) {
  const item = document.getElementById(`lb-fb-${fbId}`);
  if (!item) return;
  const el = document.getElementById(`lb-fb-content-${fbId}`);
  const text = el.textContent.replace(/💬\s*/, '').replace(/\d+월.*/, '').trim();
  item.innerHTML = fbEditFormHtml(fbId, text, { projectId, stepNo, sceneNo, dark: true });
  autoResizeTextarea(item);
}

async function loadFeedbackList(projectId) {
  const container = document.getElementById('feedback-list');
  if (!container) return;

  const feedbacks = await API.get(`/feedback?project_id=${projectId}`);
  if (!feedbacks.length) {
    container.innerHTML = '';
    return;
  }

  container.innerHTML = feedbacks.slice(0, 20).map(f => {
    const icon = f.feedback_type === 'like' ? '👍' : f.feedback_type === 'dislike' ? '👎' : '💬';
    const step = f.step_no ? `STEP ${f.step_no}` : '전체';
    const scene = f.scene_no ? ` 장면${f.scene_no}` : '';
    const time = formatFbTime(f.created_at);
    return `<div class="fb-item" data-fb-id="${f.id}" style="padding:6px 0;font-size:0.8rem">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span>${icon} <span style="color:var(--text-muted)">${step}${scene} · ${time}</span></span>
        <span class="fb-actions">
          ${f.content ? `<button onclick="editFeedback(${f.id}, this)" class="fb-action-btn" title="수정">✏️</button>` : ''}
          <button onclick="deleteFeedback(${f.id}, '${projectId}')" class="fb-action-btn" title="삭제">🗑️</button>
        </span>
      </div>
      ${f.content ? `<div class="fb-content" id="fb-content-${f.id}">${f.content}</div>` : ''}
    </div>`;
  }).join('');
}

async function processFeedback() {
  toast('피드백 분석 중...');
  const result = await API.post('/feedback/process', {});
  if (result.ok) {
    if (result.improvements?.length) {
      toast(`${result.improvements.length}개 개선 적용!`, 'success');
      loadPromptHistory();
    } else {
      toast(result.message || '처리할 피드백이 없습니다.', 'info');
    }
  } else {
    toast('분석 실패', 'error');
  }
}

async function showImprovementHistory() {
  const history = await API.get('/feedback/prompt-history');

  const content = history.length
    ? history.map(h => {
        const step = h.step_target || '?';
        const time = formatFbTime(h.created_at);
        return `<div style="padding:8px 0;border-bottom:1px solid var(--border)">
          <strong>STEP ${step}</strong> <span style="color:var(--text-muted)">${time}</span><br>
          <span style="font-size:0.85rem">${h.after_summary || ''}</span>
        </div>`;
      }).join('')
    : '<p style="color:var(--text-muted)">아직 개선 이력이 없습니다.</p>';

  // 간단한 팝업
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:1000;display:flex;align-items:center;justify-content:center;padding:16px';
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  overlay.innerHTML = `<div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:20px;max-width:500px;width:100%;max-height:80vh;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <strong style="font-size:1.1rem">프롬프트 개선 이력</strong>
      <button onclick="this.closest('div[style*=fixed]').remove()" style="background:none;border:none;color:var(--text-muted);font-size:1.5rem;cursor:pointer">&times;</button>
    </div>
    ${content}
  </div>`;
  document.body.appendChild(overlay);
}

async function deleteFeedback(fbId, projectId) {
  if (!confirm('삭제하시겠습니까?')) return;
  const result = await API.delete(`/feedback/${fbId}`);
  if (result.ok) {
    toast('삭제 완료', 'success');
    loadFeedbackList(projectId);
    loadAllStepFeedbacks(projectId);
  } else {
    toast('삭제 실패', 'error');
  }
}

function editFeedback(fbId, btn) {
  const item = document.querySelector(`[data-fb-id="${fbId}"]`);
  if (!item) return;
  const contentEl = document.getElementById(`fb-content-${fbId}`);
  const current = contentEl?.textContent || '';
  item.innerHTML = fbEditFormHtml(fbId, current);
  autoResizeTextarea(item);
}

function editStepFb(fbId, projectId, stepNo) {
  const item = document.getElementById(`step-fb-content-${fbId}`)?.closest('.fb-item');
  if (!item) return;
  const el = document.getElementById(`step-fb-content-${fbId}`);
  const text = el.textContent.replace(/💬\s*/, '').replace(/장면\d+\s*/, '').replace(/\d+월.*/, '').trim();
  item.innerHTML = fbEditFormHtml(fbId, text, { projectId, stepNo });
  autoResizeTextarea(item);
}

// ── form submit 이벤트 (모바일 엔터 대응) ──
document.addEventListener('submit', e => {
  const form = e.target;
  e.preventDefault();
  if (form.classList.contains('step-fb-form')) {
    const stepNo = parseInt(form.dataset.step);
    submitStepFeedback(stepNo, form.querySelector('button[type="submit"]'));
  } else if (form.id === 'fb-comment-form') {
    submitFeedback('comment');
  } else if (form.id === 'lightbox-fb-form') {
    submitLightboxFeedback();
  } else if (form.classList.contains('fb-edit-form')) {
    const fbId = parseInt(form.dataset.fbId);
    const input = form.querySelector('.fb-edit-input');
    if (!input) return;
    const content = input.value.trim();
    if (!content) return toast('내용을 입력해주세요', 'error');
    API.post(`/feedback/${fbId}`, { content }).then(result => {
      if (result.ok) {
        toast('수정 완료', 'success');
        const pid = form.dataset.project || window._currentProjectId;
        const stepNo = form.dataset.step ? parseInt(form.dataset.step) : null;
        const sceneNo = form.dataset.scene ? parseInt(form.dataset.scene) : null;
        loadFeedbackList(pid);
        if (stepNo) loadStepFeedbacks(pid, stepNo);
        if (stepNo && sceneNo) loadLightboxFeedbacks(pid, stepNo, sceneNo);
        loadAllStepFeedbacks(pid);
      }
    });
  }
});

// textarea에서 Enter = 전송, Shift+Enter = 줄바꿈
document.addEventListener('keydown', e => {
  const ta = e.target;
  if (!(ta.tagName === 'TEXTAREA' && (ta.classList.contains('step-fb-input') ||
    ta.classList.contains('fb-comment-input') || ta.classList.contains('lightbox-fb-input') ||
    ta.classList.contains('fb-edit-input')))) return;
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const form = ta.closest('form');
    if (form) form.requestSubmit();
  }
});

// 하위호환
async function loadPromptHistory() { /* 팝업으로 대체됨 */ }
