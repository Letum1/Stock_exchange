/* PaperTrade — first-visit guided tour.
   Pure-JS overlay that highlights navigation tabs and explains them.
   Triggered automatically on the first page load (per-account, stored in
   localStorage). Can be re-launched any time via window.startPaperTour(). */
(function () {
  'use strict';

  var KEY = 'pt_tour_v1_done';

  // Only show on small screens? No — beginners on desktop benefit too.
  // But we tailor copy & target the bottom nav on mobile, the sidebar on desktop.
  var IS_MOBILE = function () { return window.matchMedia('(max-width: 880px)').matches; };

  // Steps reference nav links by href. Each step describes the target
  // and what to say. The last "step" is a closing card with no target.
  var STEPS = [
    {
      href: null,
      title: 'Welcome to PaperTrade! 👋',
      body: "We'll take 30 seconds to show you around. You can skip any time.",
      cta: "Show me",
      isIntro: true
    },
    {
      href: '/market',
      title: 'Market 📊',
      body: 'Find live stocks &amp; crypto. Tap any ticker to see a chart and buy.'
    },
    {
      href: '/trading',
      title: 'Portfolio 📈',
      body: "Everything you own, with live profit and loss. Sell from here when you're ready."
    },
    {
      href: '/wallet',
      title: 'Wallet 💰',
      body: 'Your virtual cash &amp; tokens live here. Convert between them any time.'
    },
    {
      href: '/items',
      title: 'Shop 🎒',
      body: "The item marketplace — browse, buy, or list your own items for sale."
    },
    {
      href: '/trades',
      title: 'Swap 🔄',
      body: 'Trade items, cash and shares directly with other players. Both sides confirm.'
    },
    {
      href: '/about',
      title: "That's it! 🎉",
      body: 'Anytime you need a refresher, the About page has the full guide.',
      cta: "Let's go",
      isOutro: true
    }
  ];

  var styleTag = null;
  function injectStyle() {
    if (styleTag) return;
    styleTag = document.createElement('style');
    styleTag.textContent = [
      '#pt-tour-bg{position:fixed;inset:0;z-index:99000;background:rgba(0,0,0,.72);',
      '  backdrop-filter:blur(3px);display:none;font-family:-apple-system,system-ui,sans-serif}',
      '#pt-tour-bg.open{display:block}',
      '#pt-tour-spot{position:fixed;border-radius:14px;pointer-events:none;',
      '  box-shadow:0 0 0 4px #7c85ff, 0 0 0 9999px rgba(0,0,0,.72);',
      '  transition:all .28s cubic-bezier(.2,.8,.2,1);z-index:99001}',
      '#pt-tour-card{position:fixed;left:50%;transform:translateX(-50%);',
      '  width:min(360px,calc(100vw - 28px));background:#13151f;color:#e7eaf3;',
      '  border:1px solid #2d3148;border-radius:16px;padding:18px 18px 16px;',
      '  box-shadow:0 24px 60px rgba(0,0,0,.6);z-index:99002;',
      '  transition:top .28s cubic-bezier(.2,.8,.2,1), bottom .28s cubic-bezier(.2,.8,.2,1)}',
      '#pt-tour-card .pt-step{font-size:.7rem;color:#7c85ff;font-weight:700;',
      '  letter-spacing:.08em;text-transform:uppercase;margin-bottom:6px}',
      '#pt-tour-card h3{font-size:1.05rem;font-weight:800;margin-bottom:6px;letter-spacing:-.01em}',
      '#pt-tour-card p{font-size:.88rem;color:#cbd5e1;line-height:1.5}',
      '#pt-tour-card .pt-row{display:flex;align-items:center;justify-content:space-between;',
      '  gap:10px;margin-top:14px}',
      '#pt-tour-card .pt-dots{display:flex;gap:5px}',
      '#pt-tour-card .pt-dot{width:6px;height:6px;border-radius:50%;background:#2d3148}',
      '#pt-tour-card .pt-dot.on{background:#7c85ff}',
      '#pt-tour-card .pt-btns{display:flex;gap:8px}',
      '#pt-tour-card button{font-family:inherit;font-size:.82rem;font-weight:700;',
      '  padding:8px 14px;border-radius:9px;cursor:pointer;border:none}',
      '#pt-tour-card .pt-skip{background:transparent;color:#94a3b8}',
      '#pt-tour-card .pt-skip:hover{color:#e7eaf3}',
      '#pt-tour-card .pt-next{background:linear-gradient(135deg,#7c85ff,#a855f7);color:#fff;',
      '  box-shadow:0 6px 18px rgba(124,133,255,.35)}',
      '#pt-tour-card .pt-next:hover{transform:translateY(-1px)}',
      '#pt-tour-card .pt-close{position:absolute;top:8px;right:8px;background:transparent;',
      '  color:#64748b;font-size:18px;width:28px;height:28px;display:flex;',
      '  align-items:center;justify-content:center;border-radius:50%}',
      '#pt-tour-card .pt-close:hover{background:#1c1f2c;color:#e7eaf3}'
    ].join('\n');
    document.head.appendChild(styleTag);
  }

  function findTarget(href) {
    if (!href) return null;
    return document.querySelector('.app-nav a[href="' + href + '"]');
  }

  function placeSpotlight(spot, target) {
    if (!target) {
      spot.style.display = 'none';
      return;
    }
    spot.style.display = 'block';
    var r = target.getBoundingClientRect();
    var pad = 4;
    spot.style.top    = (r.top - pad) + 'px';
    spot.style.left   = (r.left - pad) + 'px';
    spot.style.width  = (r.width + pad * 2) + 'px';
    spot.style.height = (r.height + pad * 2) + 'px';
  }

  function placeCard(card, target) {
    // If target exists, place card opposite the target.
    // Mobile bottom nav = card sits above (bottom of viewport - nav height - card)
    // Desktop sidebar = card sits to the right of the highlighted item
    card.style.removeProperty('top');
    card.style.removeProperty('bottom');
    card.style.removeProperty('left');
    card.style.removeProperty('right');
    card.style.transform = 'translateX(-50%)';

    if (!target) {
      // Center on screen
      card.style.top  = '50%';
      card.style.left = '50%';
      card.style.transform = 'translate(-50%, -50%)';
      return;
    }
    var r = target.getBoundingClientRect();
    var vw = window.innerWidth, vh = window.innerHeight;

    if (IS_MOBILE()) {
      // Place above the bottom nav with a small gap.
      card.style.left = '50%';
      card.style.bottom = (vh - r.top + 14) + 'px';
    } else {
      // Sidebar nav: place card centered vertically next to the item.
      var cardWidth = 360;
      var leftPx = r.right + 18;
      if (leftPx + cardWidth + 14 > vw) leftPx = vw - cardWidth - 14;
      card.style.left = leftPx + 'px';
      card.style.transform = 'none';
      var topPx = r.top + r.height / 2 - 90;
      if (topPx < 14) topPx = 14;
      if (topPx + 220 > vh) topPx = vh - 240;
      card.style.top  = topPx + 'px';
    }
  }

  var state = { idx: 0, bg: null, spot: null, card: null, onResize: null };

  function scrollNavToTarget(target, done) {
    if (!target) { done(); return; }
    var nav = document.querySelector('.app-nav');
    if (!nav || !IS_MOBILE()) { done(); return; }
    // Center the target horizontally in the bottom nav so the spotlight is
    // always visible, then wait for the smooth-scroll to settle.
    var navRect = nav.getBoundingClientRect();
    var aRect   = target.getBoundingClientRect();
    var center  = aRect.left + aRect.width / 2 - navRect.left;
    var goal    = nav.scrollLeft + center - navRect.width / 2;
    goal = Math.max(0, goal);
    if (Math.abs(goal - nav.scrollLeft) < 2) { done(); return; }
    nav.scrollTo({ left: goal, behavior: 'smooth' });
    setTimeout(done, 320);
  }

  function render() {
    var step = STEPS[state.idx];
    var target = findTarget(step.href);
    var stepNum = state.idx + 1;
    var total = STEPS.length;
    var dots = '';
    for (var i = 0; i < total; i++) {
      dots += '<span class="pt-dot' + (i === state.idx ? ' on' : '') + '"></span>';
    }
    var cta = step.cta || (state.idx === total - 1 ? 'Done' : 'Next');
    state.card.innerHTML =
      '<button class="pt-close" aria-label="Close">×</button>' +
      '<div class="pt-step">Step ' + stepNum + ' of ' + total + '</div>' +
      '<h3>' + step.title + '</h3>' +
      '<p>' + step.body + '</p>' +
      '<div class="pt-row">' +
        '<div class="pt-dots">' + dots + '</div>' +
        '<div class="pt-btns">' +
          (state.idx < total - 1 ? '<button class="pt-skip">Skip</button>' : '') +
          '<button class="pt-next">' + cta + '</button>' +
        '</div>' +
      '</div>';
    state.card.querySelector('.pt-close').onclick = end;
    state.card.querySelector('.pt-next').onclick = next;
    var skip = state.card.querySelector('.pt-skip');
    if (skip) skip.onclick = end;

    // Bring the highlighted nav button on-screen first so the spotlight is
    // never hidden behind the bottom nav's overflow scroll.
    scrollNavToTarget(target, function () {
      placeSpotlight(state.spot, target);
      placeCard(state.card, target);
    });
  }

  function next() {
    state.idx++;
    if (state.idx >= STEPS.length) { end(); return; }
    render();
  }

  function end() {
    try { localStorage.setItem(KEY, '1'); } catch (e) {}
    if (state.bg) state.bg.classList.remove('open');
    if (state.onResize) {
      window.removeEventListener('resize', state.onResize);
      window.removeEventListener('scroll', state.onResize, true);
      state.onResize = null;
    }
    setTimeout(function () {
      if (state.bg && state.bg.parentNode) state.bg.parentNode.removeChild(state.bg);
      state.bg = state.spot = state.card = null;
    }, 200);
  }

  function start() {
    injectStyle();
    if (state.bg) return; // already open
    state.idx = 0;

    state.bg = document.createElement('div');
    state.bg.id = 'pt-tour-bg';
    state.spot = document.createElement('div');
    state.spot.id = 'pt-tour-spot';
    state.card = document.createElement('div');
    state.card.id = 'pt-tour-card';
    state.bg.appendChild(state.spot);
    state.bg.appendChild(state.card);
    document.body.appendChild(state.bg);

    state.bg.addEventListener('click', function (e) {
      // Click on backdrop (not card or spotlight) closes.
      if (e.target === state.bg) end();
    });
    document.addEventListener('keydown', function (e) {
      if (!state.bg) return;
      if (e.key === 'Escape') end();
      else if (e.key === 'ArrowRight' || e.key === 'Enter') next();
    });

    state.onResize = function () { if (state.card) render(); };
    window.addEventListener('resize', state.onResize);
    window.addEventListener('scroll', state.onResize, true);

    requestAnimationFrame(function () {
      state.bg.classList.add('open');
      render();
    });
  }

  // Public API
  window.startPaperTour = function () {
    try { localStorage.removeItem(KEY); } catch (e) {}
    start();
  };

  // Auto-start on first visit (after a brief delay so the page settles).
  function maybeAutoStart() {
    try {
      if (localStorage.getItem(KEY) === '1') return;
    } catch (e) { return; }
    // Don't start on auth/login/register pages — user isn't in the app yet.
    var p = location.pathname;
    if (p === '/login' || p === '/register' || p.indexOf('/login') === 0 ||
        p.indexOf('/register') === 0) return;
    // Only run if a nav exists (i.e., user is logged in).
    if (!document.querySelector('.app-nav')) return;
    // If the welcome ad popup is open, wait for it to close before starting
    // the tour so they don't overlap on top of each other.
    function tryStart() {
      var popup = document.getElementById('ad-popup-bg');
      if (popup && popup.style.display === 'flex') {
        setTimeout(tryStart, 500);
      } else {
        start();
      }
    }
    setTimeout(tryStart, 1100);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', maybeAutoStart);
  } else {
    maybeAutoStart();
  }
})();
