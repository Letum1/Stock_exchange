/* Reusable playful human-verification widget.
 *
 * Usage:
 *   Challenge.mount(container, {
 *     purpose:   'registration' | 'deep_mine',
 *     onSuccess: () => { ... },
 *     onCancel:  () => { ... }   // optional
 *   });
 *
 * The widget asks the server for one of: math, riddle, emoji, pattern, drag.
 * It calls onSuccess() once the answer is verified server-side.
 */
(function () {
  const STYLE_ID = 'challenge-widget-style';
  const CSS = `
    .ch-wrap   { background:#13151f; border:1px solid #2d3148; border-radius:10px;
                 padding:16px; color:#e2e8f0; }
    .ch-label  { font-size:.7rem; color:#64748b; text-transform:uppercase;
                 letter-spacing:.07em; font-weight:700; margin-bottom:10px; }
    .ch-prompt { font-size:.95rem; color:#f1f5f9; margin-bottom:12px; line-height:1.45; }
    .ch-input  { width:100%; padding:10px 12px; border-radius:7px;
                 background:#0f1117; border:1px solid #2d3148; color:#e2e8f0;
                 font-size:1rem; outline:none; }
    .ch-input:focus { border-color:#7c85ff; }
    .ch-row    { display:flex; gap:8px; align-items:center; margin-top:10px; }
    .ch-btn    { padding:9px 14px; border:none; border-radius:7px; cursor:pointer;
                 font-size:.85rem; font-weight:700; background:#7c85ff; color:#fff; }
    .ch-btn.ghost { background:#2d3148; color:#94a3b8; }
    .ch-btn:hover { opacity:.85; }
    .ch-msg-err { color:#fca5a5; font-size:.8rem; margin-top:8px; min-height:1.1em; }
    .ch-options { display:grid; grid-template-columns:repeat(4, 1fr); gap:8px; }
    .ch-opt { padding:14px 8px; background:#0f1117; border:1px solid #2d3148;
              border-radius:8px; cursor:pointer; text-align:center; font-size:1.5rem;
              transition:border-color .12s, transform .08s, background .12s; }
    .ch-opt:hover { border-color:#7c85ff; background:#1e2130; }
    .ch-opt:active { transform:scale(.94); }
    .ch-pattern { display:grid; grid-template-columns:repeat(6, 1fr); gap:6px; }
    .ch-drag-row { display:flex; align-items:center; justify-content:space-between;
                   gap:14px; margin-top:10px; }
    .ch-drag-items { display:flex; gap:8px; flex:1; flex-wrap:wrap; }
    .ch-drag-item { width:54px; height:54px; display:flex; align-items:center;
                    justify-content:center; font-size:1.7rem; background:#0f1117;
                    border:1px solid #2d3148; border-radius:9px; cursor:grab;
                    user-select:none; transition:transform .08s, opacity .15s; }
    .ch-drag-item:active { cursor:grabbing; transform:scale(.94); }
    .ch-drag-item.placed { opacity:.25; cursor:default; }
    .ch-drop {
      width:74px; height:74px; display:flex; align-items:center; justify-content:center;
      font-size:2.4rem; background:#0f1117; border:2px dashed #2d3148;
      border-radius:12px; flex-shrink:0; transition:border-color .15s, background .15s;
    }
    .ch-drop.over { border-color:#7c85ff; background:#1e2130; }
    .ch-loading { color:#64748b; font-size:.85rem; padding:8px 0; }
  `;

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  function escapeHtml(t) {
    return String(t).replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function setError(host, msg) {
    let el = host.querySelector('.ch-msg-err');
    if (!el) { el = document.createElement('div'); el.className = 'ch-msg-err'; host.appendChild(el); }
    el.textContent = msg || '';
  }

  async function submit(host, id, answer, purpose, onSuccess, refresh) {
    setError(host, '');
    try {
      const r = await fetch('/api/challenge/verify', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ id, answer, purpose }),
      });
      const d = await r.json();
      if (r.ok && d.ok) { onSuccess && onSuccess(); return; }
      setError(host, d.error || 'Try a new one.');
      if (d.expired) { refresh(); }
      else { setTimeout(refresh, 700); }
    } catch (e) {
      setError(host, 'Network error — try again.');
    }
  }

  function renderTextish(host, chal, opts, refresh) {
    const wrap = document.createElement('div');
    wrap.className = 'ch-wrap';
    wrap.innerHTML = `
      <div class="ch-label">🤖 Quick human check</div>
      <div class="ch-prompt">${escapeHtml(chal.prompt)}</div>
      <input class="ch-input" type="text" inputmode="numeric" placeholder="Your answer" autocomplete="off"/>
      <div class="ch-row">
        <button type="button" class="ch-btn submit">Check</button>
        <button type="button" class="ch-btn ghost refresh">↻ New one</button>
      </div>
    `;
    const input = wrap.querySelector('.ch-input');
    const trySubmit = () => submit(wrap, chal.id, input.value.trim(), opts.purpose, opts.onSuccess, refresh);
    wrap.querySelector('.submit').addEventListener('click', trySubmit);
    wrap.querySelector('.refresh').addEventListener('click', refresh);
    input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); trySubmit(); } });
    host.innerHTML = ''; host.appendChild(wrap);
    setTimeout(() => input.focus(), 50);
  }

  function renderEmoji(host, chal, opts, refresh) {
    const wrap = document.createElement('div');
    wrap.className = 'ch-wrap';
    wrap.innerHTML = `
      <div class="ch-label">🤖 Pick the right emoji</div>
      <div class="ch-prompt">${escapeHtml(chal.prompt)}</div>
      <div class="ch-options"></div>
      <div class="ch-row"><button type="button" class="ch-btn ghost refresh">↻ New one</button></div>
    `;
    const opts_el = wrap.querySelector('.ch-options');
    chal.options.forEach(opt => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ch-opt';
      b.textContent = opt;
      b.addEventListener('click', () => submit(wrap, chal.id, opt, opts.purpose, opts.onSuccess, refresh));
      opts_el.appendChild(b);
    });
    wrap.querySelector('.refresh').addEventListener('click', refresh);
    host.innerHTML = ''; host.appendChild(wrap);
  }

  function renderPattern(host, chal, opts, refresh) {
    const wrap = document.createElement('div');
    wrap.className = 'ch-wrap';
    wrap.innerHTML = `
      <div class="ch-label">🤖 Find the odd one out</div>
      <div class="ch-prompt">${escapeHtml(chal.prompt)}</div>
      <div class="ch-pattern"></div>
      <div class="ch-row"><button type="button" class="ch-btn ghost refresh">↻ New one</button></div>
    `;
    const grid = wrap.querySelector('.ch-pattern');
    chal.items.forEach((it, idx) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ch-opt';
      b.style.fontSize = '1.7rem';
      b.textContent = it;
      b.addEventListener('click', () => submit(wrap, chal.id, String(idx), opts.purpose, opts.onSuccess, refresh));
      grid.appendChild(b);
    });
    wrap.querySelector('.refresh').addEventListener('click', refresh);
    host.innerHTML = ''; host.appendChild(wrap);
  }

  function renderDrag(host, chal, opts, refresh) {
    const wrap = document.createElement('div');
    wrap.className = 'ch-wrap';
    wrap.innerHTML = `
      <div class="ch-label">🤖 Drag the right thing</div>
      <div class="ch-prompt">${escapeHtml(chal.prompt)}</div>
      <div class="ch-drag-row">
        <div class="ch-drag-items"></div>
        <div class="ch-drop" title="Drop target">${escapeHtml(chal.target)}</div>
      </div>
      <div class="ch-row"><button type="button" class="ch-btn ghost refresh">↻ New one</button></div>
    `;
    const items_el = wrap.querySelector('.ch-drag-items');
    const drop_el  = wrap.querySelector('.ch-drop');

    chal.items.forEach((it, idx) => {
      const d = document.createElement('div');
      d.className = 'ch-drag-item';
      d.textContent = it;
      d.draggable = true;
      d.dataset.value = it;
      d.dataset.idx = idx;

      // Desktop drag-and-drop
      d.addEventListener('dragstart', e => {
        e.dataTransfer.setData('text/plain', it);
        e.dataTransfer.effectAllowed = 'move';
      });

      // Touch / pointer fallback — tap to "throw" it at the target
      d.addEventListener('click', () => {
        d.classList.add('placed');
        submit(wrap, chal.id, it, opts.purpose, opts.onSuccess, refresh);
      });

      items_el.appendChild(d);
    });

    drop_el.addEventListener('dragover', e => { e.preventDefault(); drop_el.classList.add('over'); });
    drop_el.addEventListener('dragleave', () => drop_el.classList.remove('over'));
    drop_el.addEventListener('drop', e => {
      e.preventDefault();
      drop_el.classList.remove('over');
      const val = e.dataTransfer.getData('text/plain');
      submit(wrap, chal.id, val, opts.purpose, opts.onSuccess, refresh);
    });

    wrap.querySelector('.refresh').addEventListener('click', refresh);
    host.innerHTML = ''; host.appendChild(wrap);
  }

  function render(host, chal, opts, refresh) {
    if (chal.kind === 'math' || chal.kind === 'riddle') return renderTextish(host, chal, opts, refresh);
    if (chal.kind === 'emoji')   return renderEmoji(host, chal, opts, refresh);
    if (chal.kind === 'pattern') return renderPattern(host, chal, opts, refresh);
    if (chal.kind === 'drag')    return renderDrag(host, chal, opts, refresh);
    host.innerHTML = '<div class="ch-wrap"><div class="ch-msg-err">Unknown challenge type</div></div>';
  }

  async function load(host, opts) {
    ensureStyle();
    host.innerHTML = '<div class="ch-wrap"><div class="ch-loading">Loading challenge…</div></div>';
    let chal;
    try {
      const r = await fetch('/api/challenge/new');
      chal = await r.json();
    } catch (e) {
      host.innerHTML = '<div class="ch-wrap"><div class="ch-msg-err">Could not load challenge — refresh the page.</div></div>';
      return;
    }
    const refresh = () => load(host, opts);
    render(host, chal, opts, refresh);
  }

  window.Challenge = {
    mount(host, opts) { load(host, opts || {}); },
  };
})();
