/* Swipeable bottom-nav navigation.
   On touch devices a horizontal swipe across the page jumps to the
   previous / next bottom-nav link, just like swiping between tabs in
   Instagram or TikTok. Vertical scrolls are ignored so feeds keep
   working normally. */
(function () {
  if (!('ontouchstart' in window)) return;

  function navLinks() {
    // Bottom nav (mobile) is `.app-nav.mobile`. Fall back to sidebar.
    var bar = document.querySelector('.app-nav.mobile') ||
              document.querySelector('.app-nav');
    if (!bar) return [];
    return Array.prototype.slice.call(bar.querySelectorAll('a[href]'));
  }

  function currentIndex(links) {
    var path = location.pathname.replace(/\/+$/, '') || '/';
    for (var i = 0; i < links.length; i++) {
      var lp = new URL(links[i].href, location.origin).pathname.replace(/\/+$/, '') || '/';
      if (lp === path) return i;
    }
    return -1;
  }

  var startX = 0, startY = 0, tracking = false, t0 = 0;

  document.addEventListener('touchstart', function (e) {
    if (e.touches.length !== 1) { tracking = false; return; }
    // Don't hijack swipes inside elements that handle their own touch.
    var t = e.target;
    while (t && t !== document.body) {
      if (t.dataset && (t.dataset.noSwipe === '1' || t.dataset.swipeOwner === '1'))
        { tracking = false; return; }
      // Don't trigger inside scrollable horizontal carousels.
      if (t.scrollWidth > t.clientWidth + 4 &&
          getComputedStyle(t).overflowX !== 'visible') { tracking = false; return; }
      t = t.parentElement;
    }
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    t0 = Date.now();
    tracking = true;
  }, { passive: true });

  document.addEventListener('touchend', function (e) {
    if (!tracking) return;
    tracking = false;
    var t = e.changedTouches[0];
    var dx = t.clientX - startX;
    var dy = t.clientY - startY;
    var dt = Date.now() - t0;
    if (dt > 600) return;                    // too slow → not a swipe
    if (Math.abs(dx) < 80) return;           // not far enough
    if (Math.abs(dy) > Math.abs(dx) * 0.6) return; // mostly vertical → ignore
    var links = navLinks();
    var idx = currentIndex(links);
    if (idx === -1) return;
    var next = dx < 0 ? idx + 1 : idx - 1;
    if (next < 0 || next >= links.length) return;
    location.href = links[next].href;
  }, { passive: true });
})();
