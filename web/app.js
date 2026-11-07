/* 手柄当鼠标用 · 控制面板逻辑 */
'use strict';

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const GROUPS = [
  { title: '光标移动', rows: [
    { key: 'move_x', name: '水平移动', hint: '默认左摇杆 ←→', analog: true },
    { key: 'move_y', name: '垂直移动', hint: '默认左摇杆 ↑↓', analog: true },
  ]},
  { title: '鼠标按键', rows: [
    { key: 'left_click', name: '左键', hint: '按住 + 摇杆 = 拖拽', test: 'left_click' },
    { key: 'right_click', name: '右键', hint: '', test: 'right_click' },
  ]},
  { title: '滚轮', rows: [
    { key: 'wheel_axis', name: '无级滚轮', hint: '默认右摇杆 ↑↓，推得越深越快', analog: true, test: 'wheel_up' },
    { key: 'wheel_up', name: '向上滚（逐格）', hint: '默认十字键 ↑', test: 'wheel_up' },
    { key: 'wheel_down', name: '向下滚（逐格）', hint: '默认十字键 ↓', test: 'wheel_down' },
  ]},
  { title: '上一步 / 下一步', rows: [
    { key: 'prev', name: '上一步', hint: '默认 LB / L', test: 'prev' },
    { key: 'next', name: '下一步', hint: '默认 RB / R', test: 'next' },
  ]},
  { title: '系统', rows: [
    { key: 'toggle', name: '暂停 / 继续', hint: '默认 View + Menu 同时按住 0.4 秒' },
  ]},
];

const SLIDERS = [
  { key: 'cursor_speed', label: '光标速度', min: 100, max: 3000, step: 10, unit: ' px/s', digits: 0 },
  { key: 'deadzone', label: '摇杆死区', min: 0, max: 0.45, step: 0.01, unit: '', digits: 2 },
  { key: 'curve', label: '精细曲线', min: 1, max: 3.5, step: 0.1, unit: '', digits: 1 },
  { key: 'wheel_speed', label: '滚轮速度', min: 1, max: 60, step: 1, unit: ' 格/秒', digits: 0 },
  { key: 'step_wheel_speed', label: '逐格滚动速度', min: 1, max: 60, step: 1, unit: ' 格/秒', digits: 0 },
];

const TOGGLES = [
  { key: 'swap_sticks', label: '用右摇杆移动光标（左摇杆滚轮）' },
  { key: 'invert_move_y', label: '反转光标上下' },
  { key: 'invert_wheel', label: '反转滚轮方向' },
];

let state = null;
let gameState = null;
let bindStarted = 0;
let lastBindPending = false;
let lastDeviceSig = '';
let lastBtnSig = '';
const busy = {};      // 滑块：用户正在拖动
const timers = {};    // 配置提交防抖
let toastTimer = null;
let tickTimer = null;

/* ------------------------------------------------------------------ 构建静态 UI */
function buildUI() {
  $('#mappingGroups').innerHTML = GROUPS.map((g) => `
    <div class="group">
      <div class="group-title"><span class="gi"></span>${esc(g.title)}</div>
      ${g.rows.map((r) => `
        <div class="map-row">
          <div class="map-info"><div class="map-name">${esc(r.name)}</div>
            ${r.hint ? `<div class="map-hint">${esc(r.hint)}</div>` : ''}</div>
          <div class="bind-pill" data-pill="${r.key}">—</div>
          <div class="row-btns">
            ${r.test ? `<button class="row-btn test" data-test="${r.test}">测试</button>` : ''}
            <button class="row-btn primary" data-bind="${r.key}">重新绑定</button>
          </div>
        </div>`).join('')}
    </div>`).join('');

  $('#sliders').innerHTML = SLIDERS.map((s) => `
    <div class="slider-row">
      <div class="slider-top"><b>${esc(s.label)}</b><span class="slider-val" data-val="${s.key}">—</span></div>
      <input type="range" data-slider="${s.key}" min="${s.min}" max="${s.max}" step="${s.step}">
    </div>`).join('');

  $('#toggles').innerHTML = TOGGLES.map((t) => `
    <label class="toggle"><input type="checkbox" data-toggle="${t.key}"><span class="switch"></span>${esc(t.label)}</label>`).join('');
}

/* ------------------------------------------------------------------ 事件 */
function wireEvents() {
  $('#mappingGroups').addEventListener('click', (e) => {
    const bind = e.target.closest('[data-bind]');
    if (bind) { cmd({ cmd: 'bind_start', action: bind.dataset.bind }); return; }
    const test = e.target.closest('[data-test]');
    if (test) { cmd({ cmd: 'test', action: test.dataset.test }); return; }
  });

  $('#sliders').addEventListener('pointerdown', (e) => {
    if (e.target.dataset.slider) busy[e.target.dataset.slider] = true;
  });
  document.addEventListener('pointerup', () => {
    for (const k in busy) busy[k] = false;
  });
  $('#sliders').addEventListener('input', (e) => {
    const key = e.target.dataset.slider;
    if (!key) return;
    const spec = SLIDERS.find((s) => s.key === key);
    setSliderText(key, parseFloat(e.target.value), spec);
    clearTimeout(timers[key]);
    timers[key] = setTimeout(() => config({ [key]: parseFloat(e.target.value) }), 140);
  });

  $('#toggles').addEventListener('change', (e) => {
    const key = e.target.dataset.toggle;
    if (key) config({ [key]: e.target.checked });
  });

  $('#selNav').addEventListener('change', (e) => config({ nav_mode: e.target.value }));
  $('#selBackend').addEventListener('change', (e) => config({ backend: e.target.value }));
  $('#selDevice').addEventListener('change', (e) => config({ device: e.target.value }));
  $('#btnPower').addEventListener('click', () => cmd({ cmd: 'toggle' }));
  $('#btnRescan').addEventListener('click', () => cmd({ cmd: 'rescan' }));
  $('#btnWake').addEventListener('click', async () => {
    const b = $('#btnWake');
    b.disabled = true;
    b.textContent = '唤醒中…';
    try {
      await fetch('/api/probe/init', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
      });
      toast('已发送唤醒命令，请稍候 1~2 秒');
      setTimeout(probeTick, 1400);
    } catch (e) {
      toast('唤醒失败：' + e.message);
    }
    setTimeout(() => { b.disabled = false; b.textContent = '唤醒手柄'; }, 2600);
  });
  $('#btnRecenter').addEventListener('click', async () => {
    try {
      const r = await fetch('/api/probe/recenter', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
      });
      const d = await r.json();
      toast(d.ok ? '摇杆中心已更新' : (d.message || '校准失败'));
      setTimeout(probeTick, 400);
    } catch (e) { toast('校准失败：' + e.message); }
  });
  // --- 游戏模式 ---
  $('#chkGameGuard').addEventListener('change', (e) => config({ game_guard: e.target.checked }));
  $('#selGameMode').addEventListener('change', (e) => config({ game_mode: e.target.value }));
  $('#selGameGrace').addEventListener('change', (e) => config({ game_grace: parseFloat(e.target.value) }));
  $('#btnGameOverride').addEventListener('click', () => {
    const restoring = !(gameState && gameState.override);
    gameAction(restoring ? 'override' : 'clear_override', 0);
  });
  $('#btnGameRescan').addEventListener('click', () => gameAction('rescan'));
  $('#btnGameAdd').addEventListener('click', () => {
    const el = $('#gameNameInput');
    const v = el.value.trim();
    if (v) { gameAction('add', v); el.value = ''; }
  });
  $('#gameNameInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); $('#btnGameAdd').click(); }
  });
  $('#gameChips').addEventListener('click', (e) => {
    const b = e.target.closest('[data-remove]');
    if (b) gameAction('remove', b.dataset.remove);
  });
  $('#gameWinList').addEventListener('click', (e) => {
    const add = e.target.closest('[data-add]');
    const del = e.target.closest('[data-remove]');
    if (add) gameAction('add', add.dataset.add);
    else if (del) gameAction('remove', del.dataset.remove);
  });
  $('#btnDefaults').addEventListener('click', () => { cmd({ cmd: 'defaults' }); toast('已恢复默认设置'); });
  $('#btnRawDefaults').addEventListener('click', () => { cmd({ cmd: 'raw_defaults' }); toast('已套用通用手柄映射，建议逐项校准'); });
  $('#btnBindCancel').addEventListener('click', () => cmd({ cmd: 'bind_cancel' }));
  $('#btnQuit').addEventListener('click', () => {
    if (!confirm('确定要完全退出应用吗？退出后手柄将不再控制鼠标。')) return;
    cmd({ cmd: 'quit' }).then(() => {
      clearInterval(tickTimer);
      document.body.innerHTML = '<div style="display:grid;place-items:center;min-height:90vh;text-align:center;' +
        'font:15px/2 \'Segoe UI\',\'Microsoft YaHei\',sans-serif;color:#5b6579">' +
        '<div><div style="font-size:36px;margin-bottom:8px">✓</div>' +
        '<b style="color:#1b2233;font-size:18px">应用已退出</b><br>' +
        '手柄已停止控制鼠标，可以关闭此窗口。<br>' +
        '下次使用时双击「启动.bat」即可。</div></div>';
      try { window.close(); } catch (e) { /* 浏览器可能禁止脚本关闭窗口 */ }
    });
  });
}

/* ------------------------------------------------------------------ 网络 */
async function cmd(body) {
  try {
    return await (await fetch('/api/cmd', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })).json();
  } catch (e) { return { ok: false }; }
}
async function config(patch) {
  try {
    await fetch('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ patch }),
    });
  } catch (e) { /* 后台可能正在退出 */ }
}
async function gameAction(action, value) {
  try {
    const r = await fetch('/api/games', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, value }),
    });
    const d = await r.json();
    if (d && d.message) toast(d.message);
    if (d && d.snapshot) renderGame(d.snapshot);
    else gameTick();
    return d;
  } catch (e) { return { ok: false }; }
}

/* ------------------------------------------------------------------ 状态渲染 */
function setSliderText(key, value, spec) {
  const el = $(`[data-val="${key}"]`);
  if (el) el.textContent = value.toFixed(spec.digits) + spec.unit;
}

function labelFor(id, st) {
  if (!id) return '—';
  if (st.labels && st.labels[id]) return st.labels[id];
  if (id.startsWith('btn:')) return '按钮 ' + id.slice(4);
  if (id.startsWith('hat:')) {
    const d = id.split(':').pop();
    return { up: '十字键 ↑', down: '十字键 ↓', left: '十字键 ←', right: '十字键 →' }[d] || id;
  }
  if (id.startsWith('axis:')) {
    const a = id.slice(5);
    const names = { LX: '左摇杆 ←→', LY: '左摇杆 ↑↓', RX: '右摇杆 ←→', RY: '右摇杆 ↑↓', LT: 'LT', RT: 'RT' };
    return (names[a.replace(/[+-]$/, '')] || '轴 ' + a) + (a.endsWith('+') ? ' 正方向' : a.endsWith('-') ? ' 负方向' : '');
  }
  return id;
}

function axisOf(st, preferred, fallbackIndex) {
  const a = st.inputs.axes || {};
  if (preferred in a) return a[preferred];
  const keys = Object.keys(a);
  return keys[fallbackIndex] !== undefined ? a[keys[fallbackIndex]] : 0;
}

function render(st) {
  const dev = st.device;
  const badge = $('#connBadge');
  if (dev) {
    badge.className = 'conn-badge ok';
    $('#connText').textContent = '已连接 · ' + dev.name;
    $('#deviceName').textContent = dev.name;
    $('#deviceLayout').textContent = dev.layout;
    $('#deviceBackend').textContent = dev.backend;
    $('#deviceNote').textContent = dev.mode === 'joystick'
      ? '该设备以「原始手柄」识别（NS2 Pro 等新型手柄常见）：按「重新绑定」逐项学习一次即可，功能与标准手柄完全相同；也可点「套用通用手柄映射」。'
      : '已按标准手柄映射读取，Xbox / Switch Pro 可直接上手。';
  } else {
    badge.className = 'conn-badge warn';
    $('#connText').textContent = '未检测到手柄';
    $('#deviceName').textContent = '未检测到手柄';
    $('#deviceLayout').textContent = '—';
    $('#deviceBackend').textContent = st.stats.errors?.[0] || '等待连接';
    $('#deviceNote').textContent = '请用 USB 或蓝牙连接手柄后点「重新扫描」。Xbox 手柄也可切换到 XInput 后端。';
  }

  const on = st.enabled;
  $('#btnPower').classList.toggle('off', !on);
  $('#powerText').textContent = on ? '控制已启用' : '已暂停';
  $('#hzText').textContent = (st.stats.hz || 0).toFixed(0) + ' Hz';
  const guardNote = (st.game && st.game.paused) ? ' · 游戏会话中，已避让' : '';
  $('#footStatus').textContent = `轮询 ${(st.stats.hz || 0).toFixed(0)} Hz · ${dev ? dev.backend : '无设备'} · ${on ? '正在控制鼠标' : '控制已暂停'}${guardNote}`;

  // 设备下拉
  const sig = JSON.stringify(st.devices) + '|' + st.selected;
  if (sig !== lastDeviceSig) {
    lastDeviceSig = sig;
    const sel = $('#selDevice');
    sel.innerHTML = '<option value="auto">自动选择</option>' +
      st.devices.map((d) => `<option value="${esc(d.id)}">${esc(d.name)} · ${esc(d.kind)}</option>`).join('');
    sel.value = st.selected || 'auto';
  }
  if (document.activeElement !== $('#selNav')) $('#selNav').value = st.config.nav_mode;
  if (document.activeElement !== $('#selBackend')) $('#selBackend').value = st.config.backend;

  // 映射
  for (const key in st.binding_labels) {
    const pill = $(`[data-pill="${key}"]`);
    if (pill) {
      const txt = st.binding_labels[key];
      if (pill.textContent !== txt) pill.textContent = txt;
      pill.classList.toggle('pending', st.bind.pending && st.bind.action === key);
    }
  }

  // 绑定横幅
  const pending = !!st.bind.pending;
  $('#bindBanner').hidden = !pending;
  if (pending && !lastBindPending) {
    bindStarted = Date.now();
    const titles = st.titles || {};
    $('#bindTitle').textContent = `请设置「${titles[st.bind.action] || st.bind.action}」`;
    $('#bindHint').textContent = '按下手柄按键 / 拨动摇杆 / 按十字键，12 秒内无操作自动取消';
  }
  if (pending) {
    const left = Math.max(0, 12 - Math.floor((Date.now() - bindStarted) / 1000));
    $('#bindCount').textContent = left + 's';
  }
  if (!pending && lastBindPending && st.bind.result) toast('绑定成功 → ' + st.bind.result);
  lastBindPending = pending;

  // 滑块与开关
  for (const s of SLIDERS) {
    const val = st.config[s.key];
    setSliderText(s.key, val, s);
    const input = $(`[data-slider="${s.key}"]`);
    if (input && !busy[s.key] && document.activeElement !== input) input.value = val;
  }
  for (const t of TOGGLES) {
    const el = $(`[data-toggle="${t.key}"]`);
    if (el) el.checked = !!st.config[t.key];
  }

  // 摇杆可视化
  const lx = axisOf(st, 'LX', 0), ly = axisOf(st, 'LY', 1);
  const rx = axisOf(st, 'RX', 2), ry = axisOf(st, 'RY', 3);
  $('#dotL').style.transform = `translate(calc(-50% + ${(lx * 35).toFixed(1)}px), calc(-50% + ${(ly * 35).toFixed(1)}px))`;
  $('#dotR').style.transform = `translate(calc(-50% + ${(rx * 35).toFixed(1)}px), calc(-50% + ${(ry * 35).toFixed(1)}px))`;

  // 活动输入 chips
  const active = st.inputs.active || [];
  const chipsSig = active.join('|');
  if ($('#activeChips').dataset.sig !== chipsSig) {
    $('#activeChips').dataset.sig = chipsSig;
    $('#activeChips').innerHTML = active.length
      ? active.slice(0, 8).map((id) => `<span class="chip">${esc(labelFor(id, st))}</span>`).join('')
      : '<span class="chip chip-empty">无</span>';
  }

  // 按钮网格
  const buttons = st.inputs.buttons || {};
  const keys = Object.keys(buttons);
  const btnSig = keys.join('|');
  if (btnSig !== lastBtnSig) {
    lastBtnSig = btnSig;
    $('#btnGrid').innerHTML = keys.length
      ? keys.map((k) => `<span class="bchip" data-b="${esc(k)}">${esc(labelFor('btn:' + k, st))}</span>`).join('')
      : '<span class="muted">等待设备…</span>';
  }
  $('#btnGrid').querySelectorAll('[data-b]').forEach((el) => {
    el.classList.toggle('on', !!buttons[el.dataset.b]);
  });

  // 日志
  const logSig = (st.log || []).join('|');
  if ($('#logList').dataset.sig !== logSig) {
    $('#logList').dataset.sig = logSig;
    $('#logList').innerHTML = (st.log || []).slice(-8).reverse()
      .map((l) => `<div>${esc(l)}</div>`).join('') || '<div class="muted">暂无日志</div>';
  }
}

function toast(msg) {
  let el = $('#toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.style.cssText = 'position:fixed;left:50%;bottom:34px;transform:translateX(-50%);background:#1b2233;color:#fff;' +
      'padding:10px 18px;border-radius:9px;font-size:12.5px;box-shadow:0 8px 26px rgba(0,0,0,.2);z-index:99;transition:.2s';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.style.opacity = '1';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.style.opacity = '0'; }, 2200);
}

async function tick() {
  try {
    const r = await fetch('/api/state', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    state = await r.json();
    render(state);
  } catch (e) {
    const badge = $('#connBadge');
    if (badge) {
      badge.className = 'conn-badge warn';
      $('#connText').textContent = '后台服务已断开';
    }
  }
}

// NS2 Pro 需要厂商握手才会上报数据；主面板常驻显示唤醒状态，
// 重新插拔 / 重启电脑后若手柄沉默，可一键重新唤醒。
async function probeTick() {
  const bar = $('#ns2Bar');
  if (!bar) return;
  try {
    const r = await fetch('/api/probe', { cache: 'no-store' });
    if (!r.ok) return;
    const p = await r.json();
    if (!p.found) { bar.hidden = true; return; }
    bar.hidden = false;
    const ok = !!(p.streaming && p.reports > 3);
    const drift = p.center_drift || 0;
    const drifting = ok && drift > 300;
    bar.classList.toggle('warn', !ok || drifting);
    $('#ns2Text').textContent = !ok
      ? (p.error ? `NS2 Pro 未唤醒：${p.error}` : 'NS2 Pro 未唤醒（手柄沉默，需发送启动命令）')
      : drifting
        ? `摇杆中心偏差 ${drift} · 光标可能自己飘，松开摇杆后点「校准摇杆中心」`
        : `NS2 Pro 原生直读 · ${Math.round(p.hz || 0)} Hz · 已唤醒 · 中心偏差 ${drift}`;
    $('#btnWake').hidden = ok;
  } catch (e) {
    // 服务尚未就绪时静默，下一轮再试
  }
}

/* ------------------------------------------------------------ 游戏模式渲染 */
let lastChipsSig = '';
let lastWinSig = '';

function renderGame(g) {
  if (!g || g.error) return;
  gameState = g;
  const locked = !!g.locked;
  const paused = !!g.paused;
  const off = !g.enabled || g.mode === 'off';

  // 顶部横幅：只要处于游戏会话就常驻，明确告诉用户当前谁在接管
  const banner = $('#gameBanner');
  banner.hidden = !locked;
  if (locked) {
    $('#gameBannerTitle').textContent = paused
      ? `游戏会话中 · ${g.game_name || '游戏'} · 已暂停接管`
      : `游戏会话中 · ${g.game_name || '游戏'} · 已临时恢复控制`;
    $('#gameBannerHint').textContent = paused
      ? '手柄输入交给游戏；切出来查攻略也不会接管鼠标'
      : '注意：此时手柄会同时影响鼠标，可能与游戏冲突';
    $('#btnGameOverride').textContent = g.override ? '撤销临时恢复' : '临时恢复控制';
  }

  const box = $('#gameStatus');
  box.classList.toggle('warn', locked && paused);
  let text;
  if (off) text = '游戏检测已关闭，手柄始终接管鼠标';
  else if (locked && paused) text = `已暂停接管 · ${g.game_name}（${g.reason}）· 持续 ${g.duration} 秒`;
  else if (locked && g.grace_left > 0) text = `游戏已退出，${g.grace_left} 秒后恢复接管`;
  else if (locked) text = `已识别 ${g.game_name}，本次会话临时恢复了控制`;
  else text = '未检测到游戏 · 手柄正常接管鼠标';
  $('#gameStatusText').textContent = text;
  $('#gameStateTag').textContent = off ? '已关闭' : (locked ? (paused ? '避让中' : '已恢复') : '待命');

  if (document.activeElement !== $('#chkGameGuard')) $('#chkGameGuard').checked = !!g.enabled;
  if (document.activeElement !== $('#selGameMode')) $('#selGameMode').value = g.mode;
  if (document.activeElement !== $('#selGameGrace')) $('#selGameGrace').value = String(g.grace);

  const chipsSig = JSON.stringify(g.whitelist || []);
  if (chipsSig !== lastChipsSig) {
    lastChipsSig = chipsSig;
    const list = g.whitelist || [];
    $('#gameChips').innerHTML = list.length
      ? list.map((x) => `<span class="chip"><span title="${esc(x)}">${esc(x)}</span>` +
          `<button type="button" data-remove="${esc(x)}" title="移出名单">×</button></span>`).join('')
      : '<span class="muted">暂无手动添加</span>';
  }

  const rows = g.windows || [];
  const winSig = JSON.stringify(rows.map((w) => [w.pid, w.name, w.title, w.is_game, w.whitelisted]));
  if (winSig !== lastWinSig) {
    lastWinSig = winSig;
    $('#gameWinList').innerHTML = rows.length ? rows.map((w) => `
      <div class="win-row">
        <span class="win-name" title="${esc(w.path)}">${esc(w.name)}</span>
        <span class="win-title" title="${esc(w.title)}">${esc(w.title || '（无标题）')}</span>
        ${w.is_game ? '<span class="win-tag">已识别</span>' : ''}
        ${w.whitelisted
          ? `<button class="btn-mini" type="button" data-remove="${esc(w.name)}">移出名单</button>`
          : (w.is_game
              ? '<span class="win-muted">自动识别</span>'
              : `<button class="btn-mini" type="button" data-add="${esc(w.name)}">加为游戏</button>`)}
      </div>`).join('') : '<div class="win-empty">没有检测到可见窗口</div>';
  }
}

async function gameTick() {
  try {
    const r = await fetch('/api/games', { cache: 'no-store' });
    if (!r.ok) return;
    renderGame(await r.json());
  } catch (e) { /* 服务尚未就绪 */ }
}

buildUI();
wireEvents();
tick();
probeTick();
gameTick();
tickTimer = setInterval(tick, 70);
setInterval(probeTick, 700);
setInterval(gameTick, 700);
