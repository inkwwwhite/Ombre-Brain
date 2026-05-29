/* ============================================================
   砚池 · Ombre Brain Mobile
   前端逻辑
   ============================================================ */

const API_BASE = window.location.origin;
const TOKEN_KEY = 'ombre_token';

const state = {
  token: localStorage.getItem(TOKEN_KEY) || null,
  buckets: [],
  stats: null,
  filter: 'all',
  search: '',
  includeArchive: false,
  current: null,   // 当前查看/编辑的桶
  editing: false,
};

// ============================================================
// API
// ============================================================

async function api(path, opts = {}) {
  const headers = {
    'Content-Type': 'application/json',
    ...(opts.headers || {}),
  };
  if (state.token) {
    headers['Authorization'] = `Bearer ${state.token}`;
  }
  const resp = await fetch(`${API_BASE}${path}`, { ...opts, headers });
  if (resp.status === 401) {
    state.token = null;
    localStorage.removeItem(TOKEN_KEY);
    showLogin();
    throw new Error('未登录');
  }
  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.error || `${resp.status}`);
  }
  return data;
}

// ============================================================
// 登录
// ============================================================

function showLogin() {
  document.getElementById('page-login').classList.remove('hidden');
  document.getElementById('page-main').classList.add('hidden');
}

function showMain() {
  document.getElementById('page-login').classList.add('hidden');
  document.getElementById('page-main').classList.remove('hidden');
  loadBuckets();
}

document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('login-btn');
  const err = document.getElementById('login-error');
  err.textContent = '';
  btn.disabled = true;
  btn.textContent = '...';

  try {
    const data = await fetch(`${API_BASE}/api/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: document.getElementById('login-user').value,
        password: document.getElementById('login-pass').value,
      }),
    }).then(r => r.json().then(d => ({ ok: r.ok, ...d })));

    if (!data.ok) {
      err.textContent = data.error || '登录失败';
      btn.disabled = false;
      btn.textContent = '入';
      return;
    }

    state.token = data.token;
    localStorage.setItem(TOKEN_KEY, data.token);
    btn.textContent = '入';
    btn.disabled = false;
    showMain();
  } catch (e) {
    err.textContent = '网络错误';
    btn.disabled = false;
    btn.textContent = '入';
  }
});

// ============================================================
// 列表
// ============================================================

async function loadBuckets() {
  const container = document.getElementById('list-container');
  container.innerHTML = '<div class="loading-row">浮现中</div>';

  try {
    const data = await api(`/api/buckets?include_archive=${state.includeArchive}`);
    state.buckets = data.buckets;
    state.stats = data.stats;
    renderList();
    renderStats();
  } catch (e) {
    container.innerHTML = `<div class="loading-row">${e.message}</div>`;
  }
}

function renderList() {
  const container = document.getElementById('list-container');
  let list = state.buckets;

  // 筛选
  if (state.filter === 'pinned') {
    list = list.filter(b => b.pinned);
  } else if (state.filter === 'unresolved') {
    list = list.filter(b => !b.resolved && !b.pinned && b.type !== 'archived');
  } else if (state.filter === 'resolved') {
    list = list.filter(b => b.resolved);
  } else if (state.filter === 'archive') {
    list = list.filter(b => b.type === 'archived');
  }

  // 搜索
  if (state.search.trim()) {
    const q = state.search.trim().toLowerCase();
    list = list.filter(b => {
      return (b.name || '').toLowerCase().includes(q)
        || (b.content_preview || '').toLowerCase().includes(q)
        || (b.tags || []).some(t => t.toLowerCase().includes(q))
        || (b.domain || []).some(d => d.toLowerCase().includes(q));
    });
  }

  // 排序：钉选优先，然后按权重
  list.sort((a, b) => {
    if (a.pinned !== b.pinned) return b.pinned ? 1 : -1;
    return (b.score || 0) - (a.score || 0);
  });

  if (list.length === 0) {
    container.innerHTML = `
      <div class="empty">
        <p class="empty-text">空</p>
        <p class="empty-hint">这里还没有记忆</p>
      </div>
    `;
    return;
  }

  container.innerHTML = list.map(renderBucketItem).join('');

  // 绑定点击
  container.querySelectorAll('.bucket-item').forEach(el => {
    el.addEventListener('click', () => openDetail(el.dataset.id));
  });
}

function iconFor(b) {
  if (b.pinned) return '📌';
  if (b.type === 'archived') return '○';
  if (b.resolved) return '·';
  if (b.type === 'permanent') return '◆';
  return '◇';
}

function renderBucketItem(b) {
  const cls = [
    'bucket-item',
    b.pinned ? 'bucket-pinned' : '',
    b.resolved ? 'bucket-resolved' : '',
  ].join(' ').trim();

  const domains = (b.domain || []).map(d => `<span>${escapeHtml(d)}</span>`).join('·');
  const importanceDots = '○'.repeat(Math.max(0, 10 - b.importance)) + '●'.repeat(Math.min(10, b.importance || 0));

  return `
    <article class="${cls}" data-id="${b.id}">
      <div class="bucket-head">
        <span class="bucket-icon">${iconFor(b)}</span>
        <span class="bucket-name">${escapeHtml(b.name || b.id)}</span>
        <span class="bucket-score">${(b.score || 0).toFixed(2)}</span>
      </div>
      ${b.content_preview ? `<div class="bucket-preview">${escapeHtml(b.content_preview)}</div>` : ''}
      <div class="bucket-meta">
        ${domains ? `<span>${domains}</span>` : ''}
        ${domains ? '<span class="dot">·</span>' : ''}
        <span>重 ${b.importance}</span>
        <span class="dot">·</span>
        <span>V${(b.valence || 0).toFixed(1)} / A${(b.arousal || 0).toFixed(1)}</span>
      </div>
    </article>
  `;
}

function renderStats() {
  if (!state.stats) return;
  document.getElementById('stat-permanent').textContent = `钉 ${state.stats.permanent}`;
  document.getElementById('stat-dynamic').textContent = `动 ${state.stats.dynamic}`;
  document.getElementById('stat-archive').textContent = `归 ${state.stats.archive}`;
  document.getElementById('stat-size').textContent = `${state.stats.total_kb} KB`;
}

// ============================================================
// 详情/编辑抽屉
// ============================================================

async function openDetail(id) {
  const drawer = document.getElementById('drawer');
  const body = document.getElementById('drawer-body');
  drawer.classList.remove('hidden');
  document.getElementById('drawer-title').textContent = '...';
  body.innerHTML = '<div class="loading-row">读取中</div>';
  state.editing = false;

  try {
    const data = await api(`/api/buckets/${encodeURIComponent(id)}`);
    state.current = data;
    renderDetail();
  } catch (e) {
    body.innerHTML = `<div class="loading-row">${e.message}</div>`;
  }
}

function renderDetail() {
  if (!state.current) return;
  const b = state.current;
  const meta = b.metadata || {};

  document.getElementById('drawer-title').textContent = meta.name || b.id;

  const domains = (meta.domain || []).map(d =>
    `<span class="detail-domain-chip">${escapeHtml(d)}</span>`
  ).join('');

  const tags = (meta.tags || []).map(t =>
    `<span class="bucket-tag">${escapeHtml(t)}</span>`
  ).join('');

  const created = meta.created_at ? new Date(meta.created_at).toLocaleString('zh-CN', { hour12: false }) : '?';
  const lastActive = meta.last_active ? new Date(meta.last_active).toLocaleString('zh-CN', { hour12: false }) : '?';

  document.getElementById('drawer-body').innerHTML = `
    ${domains ? `<div class="detail-domain">${domains}</div>` : ''}
    <div class="detail-meta">
      <span class="detail-meta-item"><span class="detail-meta-label">权重</span> ${(b.score || 0).toFixed(2)}</span>
      <span class="detail-meta-item"><span class="detail-meta-label">重要</span> ${meta.importance || 0}</span>
      <span class="detail-meta-item"><span class="detail-meta-label">效价</span> ${(meta.valence || 0).toFixed(2)}</span>
      <span class="detail-meta-item"><span class="detail-meta-label">唤醒</span> ${(meta.arousal || 0).toFixed(2)}</span>
    </div>
    <div class="detail-content">${escapeHtml(b.content || '')}</div>
    ${tags ? `<div class="detail-tags">${tags}</div>` : ''}
    <div class="detail-meta">
      <span class="detail-meta-item"><span class="detail-meta-label">建</span> ${created}</span>
      <span class="detail-meta-item"><span class="detail-meta-label">激</span> ${lastActive}</span>
    </div>
    <div class="detail-actions">
      <button class="btn" id="act-resolve">${meta.resolved ? '激活' : '沉底'}</button>
      <button class="btn" id="act-pin">${meta.pinned ? '取消钉' : '钉选'}</button>
      <button class="btn btn-danger" id="act-delete">删除</button>
    </div>
  `;

  document.getElementById('act-resolve').addEventListener('click', () => toggleField('resolved', !meta.resolved));
  document.getElementById('act-pin').addEventListener('click', () => toggleField('pinned', !meta.pinned));
  document.getElementById('act-delete').addEventListener('click', confirmDelete);
}

async function toggleField(field, value) {
  if (!state.current) return;
  try {
    await api(`/api/buckets/${encodeURIComponent(state.current.id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ [field]: value }),
    });
    toast(field === 'resolved' ? (value ? '已沉底' : '已激活') : (value ? '已钉选' : '已取消钉选'));
    // 刷新当前详情和列表
    const fresh = await api(`/api/buckets/${encodeURIComponent(state.current.id)}`);
    state.current = fresh;
    renderDetail();
    loadBuckets();
  } catch (e) {
    toast(e.message);
  }
}

async function confirmDelete() {
  if (!state.current) return;
  if (!confirm(`确定要遗忘「${state.current.metadata.name || state.current.id}」吗？`)) return;

  try {
    await api(`/api/buckets/${encodeURIComponent(state.current.id)}`, { method: 'DELETE' });
    toast('已遗忘');
    closeDrawer();
    loadBuckets();
  } catch (e) {
    toast(e.message);
  }
}

function renderEditForm() {
  if (!state.current) return;
  const b = state.current;
  const meta = b.metadata || {};

  document.getElementById('drawer-title').textContent = '编辑';

  document.getElementById('drawer-body').innerHTML = `
    <form class="edit-form" id="edit-form">
      <div class="field">
        <span class="field-label">名称</span>
        <input type="text" name="name" value="${escapeAttr(meta.name || '')}">
      </div>
      <div class="field">
        <span class="field-label">内容</span>
        <textarea name="content">${escapeHtml(b.content || '')}</textarea>
      </div>
      <div class="field">
        <span class="field-label">领域（逗号分隔）</span>
        <input type="text" name="domain" value="${escapeAttr((meta.domain || []).join(', '))}">
      </div>
      <div class="field">
        <span class="field-label">标签（逗号分隔）</span>
        <input type="text" name="tags" value="${escapeAttr((meta.tags || []).join(', '))}">
      </div>
      <div class="slider-row">
        <label>重要度 <span class="val" id="val-importance">${meta.importance || 5}</span></label>
        <input type="range" name="importance" min="1" max="10" step="1" value="${meta.importance || 5}">
      </div>
      <div class="slider-row">
        <label>效价 valence <span class="val" id="val-valence">${(meta.valence || 0.5).toFixed(2)}</span></label>
        <input type="range" name="valence" min="0" max="1" step="0.05" value="${meta.valence || 0.5}">
      </div>
      <div class="slider-row">
        <label>唤醒度 arousal <span class="val" id="val-arousal">${(meta.arousal || 0.3).toFixed(2)}</span></label>
        <input type="range" name="arousal" min="0" max="1" step="0.05" value="${meta.arousal || 0.3}">
      </div>
      <div class="detail-actions">
        <button type="button" class="btn" id="edit-cancel">取消</button>
        <button type="submit" class="btn btn-ink">保存</button>
      </div>
    </form>
  `;

  // 滑块联动显示
  ['importance', 'valence', 'arousal'].forEach(name => {
    const input = document.querySelector(`[name="${name}"]`);
    const val = document.getElementById(`val-${name}`);
    input.addEventListener('input', () => {
      val.textContent = name === 'importance' ? input.value : parseFloat(input.value).toFixed(2);
    });
  });

  document.getElementById('edit-cancel').addEventListener('click', () => {
    state.editing = false;
    renderDetail();
  });

  document.getElementById('edit-form').addEventListener('submit', saveEdit);
}

async function saveEdit(e) {
  e.preventDefault();
  const form = new FormData(e.target);
  const payload = {
    name: form.get('name'),
    content: form.get('content'),
    domain: (form.get('domain') || '').split(',').map(s => s.trim()).filter(Boolean),
    tags: (form.get('tags') || '').split(',').map(s => s.trim()).filter(Boolean),
    importance: parseInt(form.get('importance'), 10),
    valence: parseFloat(form.get('valence')),
    arousal: parseFloat(form.get('arousal')),
  };

  try {
    await api(`/api/buckets/${encodeURIComponent(state.current.id)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    });
    toast('已保存');
    const fresh = await api(`/api/buckets/${encodeURIComponent(state.current.id)}`);
    state.current = fresh;
    state.editing = false;
    renderDetail();
    loadBuckets();
  } catch (err) {
    toast(err.message);
  }
}

function renderNewForm() {
  state.current = null;
  state.editing = true;
  document.getElementById('drawer').classList.remove('hidden');
  document.getElementById('drawer-title').textContent = '新建';

  document.getElementById('drawer-body').innerHTML = `
    <form class="edit-form" id="new-form">
      <p class="foot-tip" style="margin-bottom:12px">系统会自动分析内容并打标，你也可以手动指定。</p>
      <div class="field">
        <span class="field-label">内容</span>
        <textarea name="content" autofocus placeholder="想存什么..."></textarea>
      </div>
      <div class="slider-row">
        <label>重要度 <span class="val" id="val-importance">5</span></label>
        <input type="range" name="importance" min="1" max="10" step="1" value="5">
      </div>
      <div class="toggle-row">
        <span class="toggle-label">钉选（永久保留）</span>
        <button type="button" class="toggle" id="toggle-pinned"></button>
      </div>
      <div class="field">
        <span class="field-label">额外标签（可选，逗号分隔）</span>
        <input type="text" name="tags" placeholder="自动打标会补充">
      </div>
      <div class="detail-actions">
        <button type="button" class="btn" id="new-cancel">取消</button>
        <button type="submit" class="btn btn-ink">存入</button>
      </div>
    </form>
  `;

  document.querySelector('[name="importance"]').addEventListener('input', e => {
    document.getElementById('val-importance').textContent = e.target.value;
  });

  let pinned = false;
  document.getElementById('toggle-pinned').addEventListener('click', (e) => {
    pinned = !pinned;
    e.target.classList.toggle('on', pinned);
  });

  document.getElementById('new-cancel').addEventListener('click', closeDrawer);

  document.getElementById('new-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = new FormData(e.target);
    const content = (form.get('content') || '').trim();
    if (!content) {
      toast('内容不能为空');
      return;
    }
    const payload = {
      content,
      importance: parseInt(form.get('importance'), 10),
      tags: (form.get('tags') || '').split(',').map(s => s.trim()).filter(Boolean),
      pinned,
    };
    try {
      toast('打标中');
      await api('/api/buckets', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      toast('已存入');
      closeDrawer();
      loadBuckets();
    } catch (err) {
      toast(err.message);
    }
  });
}

function closeDrawer() {
  document.getElementById('drawer').classList.add('hidden');
  state.current = null;
  state.editing = false;
}

// ============================================================
// Toast
// ============================================================

let toastTimer = null;
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  // 重置动画
  el.style.animation = 'none';
  void el.offsetWidth;
  el.style.animation = '';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 2200);
}

// ============================================================
// 辅助
// ============================================================

function escapeHtml(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function escapeAttr(s) {
  return escapeHtml(s);
}

// ============================================================
// 事件绑定
// ============================================================

// 筛选 chip
document.querySelectorAll('.chip').forEach(chip => {
  chip.addEventListener('click', () => {
    document.querySelectorAll('.chip').forEach(c => c.classList.remove('chip-active'));
    chip.classList.add('chip-active');
    state.filter = chip.dataset.filter;
    renderList();
  });
});

// 搜索
let searchTimer = null;
document.getElementById('search-input').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = e.target.value;
    renderList();
  }, 200);
});

// 顶栏按钮
document.getElementById('btn-new').addEventListener('click', renderNewForm);
document.getElementById('btn-menu').addEventListener('click', () => {
  document.getElementById('sidemenu').classList.remove('hidden');
});

// 抽屉
document.getElementById('drawer-close').addEventListener('click', closeDrawer);
document.querySelector('#drawer .drawer-backdrop').addEventListener('click', closeDrawer);
document.getElementById('drawer-edit').addEventListener('click', () => {
  if (!state.current) return;
  state.editing = !state.editing;
  if (state.editing) {
    renderEditForm();
  } else {
    renderDetail();
  }
});

// 侧边菜单
document.querySelector('#sidemenu .sidemenu-backdrop').addEventListener('click', () => {
  document.getElementById('sidemenu').classList.add('hidden');
});

document.getElementById('menu-refresh').addEventListener('click', () => {
  document.getElementById('sidemenu').classList.add('hidden');
  loadBuckets();
  toast('已刷新');
});

document.getElementById('menu-archive-toggle').addEventListener('click', () => {
  state.includeArchive = !state.includeArchive;
  document.getElementById('menu-archive-state').textContent = state.includeArchive ? '开' : '关';
  loadBuckets();
});

document.getElementById('menu-logout').addEventListener('click', () => {
  if (!confirm('确定要登出吗？')) return;
  state.token = null;
  localStorage.removeItem(TOKEN_KEY);
  document.getElementById('sidemenu').classList.add('hidden');
  showLogin();
});

// 下拉刷新（轻量实现）
let touchStart = 0;
let pulling = false;
const listEl = document.getElementById('list-container');
listEl.addEventListener('touchstart', (e) => {
  if (listEl.scrollTop === 0) {
    touchStart = e.touches[0].clientY;
    pulling = true;
  }
});
listEl.addEventListener('touchmove', (e) => {
  if (!pulling) return;
  const delta = e.touches[0].clientY - touchStart;
  if (delta > 80) {
    pulling = false;
    loadBuckets();
    toast('刷新');
  }
});
listEl.addEventListener('touchend', () => { pulling = false; });

// ============================================================
// 启动
// ============================================================

(async () => {
  // 显示版本信息
  try {
    const h = await fetch(`${API_BASE}/health`).then(r => r.json());
    document.getElementById('foot-version').textContent =
      `桶 ${h.buckets} · 衰减 ${h.decay_engine === 'running' ? '运行' : '停'}`;
    document.getElementById('menu-user').textContent =
      h.auth_enabled ? '已登录' : '匿名模式';
  } catch (e) {}

  // 检查 token
  if (!state.token) {
    showLogin();
    return;
  }

  // 验证 token
  try {
    await api('/api/me');
    showMain();
  } catch (e) {
    showLogin();
  }
})();
