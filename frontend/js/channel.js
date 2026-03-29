/* 채널 페이지 — YouTube 연동 + 업로드 이력 */

pages['channel'] = loadChannel;

async function loadChannel() {
  await Promise.all([
    _loadPlatformAccount('youtube', 'channel-yt-status', 'channel-yt-actions'),
    _loadPlatformAccount('instagram', 'channel-ig-status', 'channel-ig-actions'),
    _loadPlatformAccount('tiktok', 'channel-tt-status', 'channel-tt-actions'),
    _loadUploadHistory(),
  ]);
}

async function _loadPlatformAccount(platform, statusId, actionsId) {
  try {
    const res = await fetch(`/api/upload/${platform}/account`);
    const data = await res.json();
    const statusEl = document.getElementById(statusId);
    const actionsEl = document.getElementById(actionsId);
    if (!statusEl || !actionsEl) return;

    if (data.connected) {
      statusEl.textContent = `${data.channel_title} 연결됨`;
      statusEl.style.color = 'var(--success)';
      actionsEl.innerHTML = `
        <button class="btn btn-secondary btn-sm" onclick="disconnectPlatform('${platform}')">연결 해제</button>
      `;
    } else {
      statusEl.textContent = '연결되지 않음';
      statusEl.style.color = 'var(--text-muted)';
      actionsEl.innerHTML = `
        <button class="btn btn-primary btn-sm" onclick="connectPlatform('${platform}')">연결하기</button>
      `;
    }
  } catch (e) {
    console.error(`${platform} account load error:`, e);
  }
}

async function _loadUploadHistory() {
  const container = document.getElementById('upload-history-list');
  try {
    const res = await fetch('/api/upload/history');
    const data = await res.json();
    const uploads = data.uploads || [];

    if (!uploads.length) {
      container.innerHTML = `
        <div class="empty-state" style="padding:40px 0;text-align:center">
          <div style="font-size:2rem;margin-bottom:8px">📤</div>
          <p style="color:var(--text-muted)">아직 업로드한 작품이 없어요.</p>
        </div>`;
      return;
    }

    container.innerHTML = uploads.map(u => {
      const platformLabel = {youtube:'YouTube',instagram:'Instagram',tiktok:'TikTok'}[u.platform] || u.platform;
      const statusBadge = u.status === 'done'
        ? `<span class="upload-badge done">완료</span>`
        : u.status === 'uploading'
        ? `<span class="upload-badge uploading">업로드 중</span>`
        : u.status === 'failed'
        ? `<span class="upload-badge failed">실패</span>`
        : `<span class="upload-badge">대기</span>`;

      const date = u.uploaded_at
        ? new Date(u.uploaded_at).toLocaleString('ko-KR', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})
        : '';

      const failMsg = u.status === 'failed' && u.error_msg
        ? `<div class="upload-error">${u.error_msg.slice(0,100)}</div>`
        : '';

      const linkBtn = u.platform_url
        ? `<a href="${u.platform_url}" target="_blank" rel="noopener" class="btn btn-secondary btn-sm">보기</a>`
        : '';

      return `
        <div class="card upload-card">
          <div class="upload-card-title">${u.project_title || u.title || '제목 없음'}</div>
          <div class="upload-card-meta">${platformLabel} ${statusBadge} ${date ? `<span class="upload-date">${date}</span>` : ''}</div>
          ${failMsg}
          <div class="upload-card-actions">
            ${linkBtn}
            <button class="btn btn-secondary btn-sm" onclick="deleteUpload(${u.id})" title="삭제">✕</button>
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    container.innerHTML = '<p style="color:var(--error)">이력을 불러올 수 없습니다.</p>';
  }
}


/* ── OAuth 연동 (범용) ── */

const _platformNames = { youtube: 'YouTube', instagram: 'Instagram', tiktok: 'TikTok' };

function connectPlatform(platform) {
  // YouTube는 기존 connectYouTube 호환
  fetch(`/api/upload/${platform}/auth-url`)
    .then(r => r.json())
    .then(data => {
      if (!data.ok) {
        toast(data.error || `${_platformNames[platform]} 설정이 필요합니다`, 'error');
        return;
      }
      const popup = window.open(data.url, `${platform}-auth`, 'width=500,height=700');
      window.addEventListener('message', function handler(e) {
        if (e.data && e.data.type === `${platform}-auth`) {
          window.removeEventListener('message', handler);
          if (e.data.ok) {
            toast(`${_platformNames[platform]} 연결 완료`, 'success');
          } else {
            toast(e.data.error || `${_platformNames[platform]} 연결 실패`, 'error');
          }
          if (typeof loadChannel === 'function') loadChannel();
          if (typeof _loadAllPlatformSettings === 'function') _loadAllPlatformSettings();
        }
      });
    });
}

function connectYouTube() { connectPlatform('youtube'); }

async function disconnectPlatform(platform) {
  if (!confirm(`${_platformNames[platform]} 연결을 해제하시겠습니까?`)) return;
  await fetch(`/api/upload/${platform}/account`, { method: 'DELETE' });
  toast(`${_platformNames[platform]} 연결 해제됨`, 'success');
  if (typeof loadChannel === 'function') loadChannel();
  if (typeof _loadAllPlatformSettings === 'function') _loadAllPlatformSettings();
}

async function disconnectYouTube() { await disconnectPlatform('youtube'); }

async function deleteUpload(uploadId) {
  if (!confirm('업로드 이력을 삭제하시겠습니까?')) return;
  await fetch(`/api/upload/record/${uploadId}`, { method: 'DELETE' });
  _loadUploadHistory();
}
