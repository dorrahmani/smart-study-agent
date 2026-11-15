/* The study site's behaviour (04/11). A static site with no server of its own: what you did lives in this browser. */
(function () {
  'use strict';
  var B = document.body, H = document.documentElement, D = B.dataset;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return [].slice.call((r || document).querySelectorAll(s)); };
  var get = function (k, d) { try { return JSON.parse(localStorage.getItem(k)) || d; } catch (e) { return d; } };
  var put = function (k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private window */ } };
  var esc = function (s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); };
  var ROOT = '/learn/', ck = D.course || '';
  var MATH = { delimiters: [{ left: '$$', right: '$$', display: true }, { left: '\\[', right: '\\]', display: true }, { left: '$', right: '$', display: false }, { left: '\\(', right: '\\)', display: false }], throwOnError: false };
  if (window.renderMathInElement) renderMathInElement(document.body, MATH);    // here and not in onload="": the CSP allows no inline handlers

  function toast(t) {
    var e = $('.toast');
    if (!e) { e = document.createElement('div'); e.className = 'toast'; e.setAttribute('role', 'status'); B.appendChild(e); }
    e.textContent = t; e.classList.add('on');
    clearTimeout(toast.t); toast.t = setTimeout(function () { e.classList.remove('on'); }, 2400);
  }

  /* ── what's done ─────────────────────────────────────────── */
  var done = get('ssa-done', {});
  var links = $$('.side a[data-k]');

  function paint() {
    var n = 0;
    links.forEach(function (a) { var on = !!done[a.dataset.k]; a.classList.toggle('done', on); if (on) n++; });
    var p = $('.prog');
    if (p) {
      $('i', p).style.width = (links.length ? 100 * n / links.length : 0) + '%';
      $('span', p).textContent = n + ' מתוך ' + links.length + ' ' + (p.dataset.unit || 'דפים');
    }
    $$('.grp').forEach(function (g) {
      var a = $$('a[data-k]', g), c = a.filter(function (x) { return done[x.dataset.k]; }).length, s = $('summary small', g);
      if (s) s.textContent = c + '/' + a.length;
    });
    $$('.ch a[data-k], .stage a[data-k]').forEach(function (a) { a.classList.toggle('done', !!done[a.dataset.k]); });
    /* the path: finished stages, and the one you're on open */
    var firstOpen = true;
    $$('.stage').forEach(function (st) {
      var a = $$('a[data-k]', st), all = a.length && a.every(function (x) { return done[x.dataset.k]; });
      st.classList.toggle('passed', !!all);
      var now = !all && firstOpen;
      st.classList.toggle('now', now);
      if (now) { st.open = true; firstOpen = false; }
    });
    /* the path of topics: what's done, what's next, what changed since you finished it */
    var firstTp = true;
    $$('.tp').forEach(function (a) {
      var on = !!done[a.dataset.k], now = !on && firstTp;
      a.classList.toggle('done', on);
      a.classList.toggle('now', now);
      a.classList.toggle('upd', on && +a.dataset.at > done[a.dataset.k]);
      if (now) firstTp = false;
    });
    var b = $('.mark');
    if (b) {
      var on = !!done[b.dataset.k];
      b.classList.toggle('alt', on);
      b.title = on ? 'לחיצה מבטלת' : 'מקש D';
      $('span', b).textContent = on ? 'סיימתי' : 'סמן שסיימתי';
    }
    var c = $('.cont');
    if (c && n) {
      var f = links.filter(function (a) { return !done[a.dataset.k]; })[0];
      if (f) { c.href = f.href; c.style.display = ''; }
    }
  }
  function toggleMark() {
    var b = $('.mark'); if (!b) return;
    var k = b.dataset.k, adding = !done[k];
    if (adding && b.dataset.quiz) { toast('נסגר כשעוברים את הבדיקה'); return; }
    if (adding) done[k] = Date.now(); else delete done[k];
    put('ssa-done', done); paint();
    if (adding) toast('✓ נשמר');
  }
  var mk = $('.mark'); if (mk) mk.addEventListener('click', toggleMark);

  /* remember where you are: the page per course + the scroll in it */
  var key = location.pathname;
  if ($('.prose') && ck) {
    var last = get('ssa-last', {});
    last[ck] = { u: location.pathname, t: ($('.title') || {}).textContent || '', at: Date.now() };
    put('ssa-last', last);
    var pos = get('ssa-pos', {});
    if (pos[key] > 400 && !location.hash) window.scrollTo(0, pos[key]);
    var saving;
    addEventListener('scroll', function () {
      clearTimeout(saving);
      saving = setTimeout(function () {
        var p = get('ssa-pos', {}); p[key] = Math.round(scrollY);
        var ks = Object.keys(p); if (ks.length > 80) delete p[ks[0]];
        put('ssa-pos', p);
      }, 400);
    }, { passive: true });
  }

  /* ── the reading bar + this page's contents ──────────────── */
  var bar = $('.readbar i');
  function onScroll() {
    if (!bar) return;
    var max = H.scrollHeight - innerHeight;
    bar.style.width = (max > 0 ? Math.min(100, 100 * scrollY / max) : 0) + '%';
  }
  addEventListener('scroll', onScroll, { passive: true }); onScroll();

  var toc = $('.toc'), prose = $('.prose');
  if (toc && prose) {
    var hs = $$('h2,h3', prose).filter(function (h) { return !h.closest('details'); });
    if (hs.length >= 3) {
      toc.innerHTML = '<b>בדף הזה</b>' + hs.map(function (h, i) {
        h.id = h.id || 's' + (i + 1);
        return '<a class="l' + h.tagName[1] + '" href="#' + h.id + '">' + esc(h.textContent.replace(/\$/g, '')) + '</a>';
      }).join('');
      var ta = $$('a', toc);
      if ('IntersectionObserver' in window) {
        var io = new IntersectionObserver(function (es) {
          es.forEach(function (e) {
            if (e.isIntersecting) {
              ta.forEach(function (a) { a.classList.toggle('on', a.getAttribute('href') === '#' + e.target.id); });
            }
          });
        }, { rootMargin: '-15% 0px -75% 0px' });
        hs.forEach(function (h) { io.observe(h); });
      }
    }
  }

  /* ── wide tables scroll inside their own box; code copies ─── */
  $$('.prose table, .card table').forEach(function (t) {
    if (t.parentElement.classList.contains('tw')) return;
    var w = document.createElement('div'); w.className = 'tw'; t.parentNode.insertBefore(w, t); w.appendChild(t);
  });
  $$('.prose pre').forEach(function (p) {
    var b = document.createElement('button'); b.className = 'copy'; b.textContent = 'העתק';
    b.onclick = function () { navigator.clipboard.writeText($('code', p) ? $('code', p).textContent : p.textContent).then(function () { b.textContent = 'הועתק'; }); };
    p.appendChild(b);
  });

  /* ── try first, then say if you knew it ──────────────────── */
  var recall = get('ssa-recall', {});
  $$('details.ans').forEach(function (d, i) {
    var id = key + '#' + i, r = document.createElement('div');
    r.className = 'recall';
    r.innerHTML = 'ידעת?<button data-v="1">ידעתי</button><button data-v="0">לחזור על זה</button>';
    d.appendChild(r);
    function show() { $$('button', r).forEach(function (b) { b.classList.toggle('on', recall[id] && String(recall[id].v) === b.dataset.v); }); }
    show();
    r.addEventListener('click', function (e) {
      var v = e.target.dataset && e.target.dataset.v; if (v === undefined) return;
      recall = get('ssa-recall', {});
      recall[id] = { v: +v, u: key, c: ck, t: ($('.title') || $('h1') || {}).textContent || '', at: Date.now() };
      put('ssa-recall', recall); show();
    });
  });
  var again = $('.again');
  if (again) {
    var rows = Object.keys(recall).map(function (k) { return recall[k]; }).filter(function (r) { return r.c === ck && !r.v; });
    var seen = {}, uniq = rows.filter(function (r) { return seen[r.u] ? false : (seen[r.u] = 1); });
    if (uniq.length) {
      again.hidden = false;
      $('ul', again).innerHTML = uniq.slice(0, 12).map(function (r) { return '<li><a href="' + esc(r.u) + '">' + esc(r.t) + '</a></li>'; }).join('');
    }
  }

  /* ── a stage's quiz: passing is what closes the page ──────── */
  var quiz = $('.quiz');
  if (quiz) {
    var qs = $$('.qq', quiz), res = $('.qres', quiz), good = 0, answered = 0;
    quiz.addEventListener('click', function (e) {
      if (e.target.closest('.again-q')) {
        good = answered = 0; res.hidden = true;
        qs.forEach(function (q) {
          $$('.opt', q).forEach(function (o) { o.disabled = false; o.classList.remove('right', 'wrong'); });
          $('.why', q).hidden = true;
        });
        scrollTo({ top: 0, behavior: 'smooth' });
        return;
      }
      var o = e.target.closest('.opt'); if (!o || o.disabled) return;
      var q = o.closest('.qq'), right = q.dataset.a;
      $$('.opt', q).forEach(function (x) { x.disabled = true; if (x.dataset.i === right) x.classList.add('right'); });
      var ok = o.dataset.i === right;
      if (!ok) o.classList.add('wrong'); else good++;
      $('.why', q).hidden = false;
      answered++;
      if (answered === qs.length) {
        var pass = good / qs.length >= +quiz.dataset.pass, b = $('.mark');
        $('b', res).textContent = good + ' מתוך ' + qs.length + (pass ? '. עברת' : '. עוד סיבוב קטן ואתה שם');
        res.hidden = false;
        if (pass && b) { done[b.dataset.k] = Date.now(); put('ssa-done', done); paint(); }
        var on2 = $('.pager a.next');
        if (pass && on2 && !$('.go-next', res)) { var nx = on2.cloneNode(true); nx.className = 'btn go-next'; nx.textContent = 'לנושא הבא ❮'; res.appendChild(nx); }
      }
    });
  }

  /* ── search everything, from anywhere (Ctrl+K or /) ──────── */
  var scrim = $('.scrim.pal-scrim'), pal = $('.pal'), input = pal && $('input', pal), list = pal && $('.list', pal), IX = null, sel = 0;
  function loadIndex() {
    if (IX) return Promise.resolve();
    var cs = (ck ? [ck] : (D.courses || '').split(',')).filter(Boolean);
    var tracks = ['topics', 'guide', 'tutorials'];
    IX = [];
    return Promise.all(cs.reduce(function (acc, c) {
      return acc.concat(tracks.map(function (t) {
        return fetch(ROOT + c + '/' + t + '/search.json').then(function (r) { return r.ok ? r.json() : []; }).then(function (j) {
          j.forEach(function (p) { IX.push({ t: p.t, x: p.x, u: ROOT + c + '/' + t + '/' + p.u, c: c }); });
        }).catch(function () {});
      }));
    }, []));
  }
  function search() {
    var q = input.value.trim(), ws = q.split(/\s+/).filter(Boolean);
    if (!ws.length) { list.innerHTML = '<div class="empty">חיפוש בכל החומר: הגדרה, משפט, מושג, נוסחה</div>'; return; }
    var seen = {}, r = [];
    (IX || []).forEach(function (p) {
      var all = p.t + ' ' + p.x;
      if (seen[p.t] || !ws.every(function (w) { return all.indexOf(w) >= 0; })) return;
      seen[p.t] = 1;
      var i = p.x.indexOf(ws[0]);
      r.push({ p: p, s: i < 0 ? '' : p.x.slice(Math.max(0, i - 36), i + 90), top: p.t.indexOf(ws[0]) >= 0 });
    });
    r.sort(function (a, b) { return b.top - a.top; });
    sel = 0;
    var names = JSON.parse(D.names || '{}');
    list.innerHTML = r.length ? r.slice(0, 30).map(function (h, i) {
      var s = esc(h.s);
      ws.forEach(function (w) { s = s.split(esc(w)).join('<mark>' + esc(w) + '</mark>'); });
      return '<a href="' + h.p.u + '"' + (i ? '' : ' class="sel"') + '><b>' + esc(h.p.t) + '</b>' + (ck ? '' : ' <span class="mute">' + esc(names[h.p.c] || '') + '</span>') +
        (s ? '<small>…' + s + '…</small>' : '') + '</a>';
    }).join('') : '<div class="empty">' + (IX && IX.length ? 'לא נמצא' : 'טוען…') + '</div>';
  }
  function openPal() {
    if (!pal) return;
    pal.classList.add('open'); scrim.classList.add('open'); input.value = ''; input.focus(); search();
    loadIndex().then(search);
  }
  function closeOver() {
    $$('.open').forEach(function (e) { e.classList.remove('open'); });
    B.classList.remove('side-open');
  }
  if (pal) {
    input.addEventListener('input', search);
    input.addEventListener('keydown', function (e) {
      var a = $$('a', list);
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!a.length) return;
        a[sel].classList.remove('sel');
        sel = (sel + (e.key === 'ArrowDown' ? 1 : a.length - 1)) % a.length;
        a[sel].classList.add('sel'); a[sel].scrollIntoView({ block: 'nearest' });
      } else if (e.key === 'Enter' && a[sel]) { location = a[sel].href; }
    });
  }

  /* ── reading settings ────────────────────────────────────── */
  var prefs = $('.prefs');
  function paintPrefs() {
    var p = get('ssa-prefs', {});
    $$('.seg [data-p]').forEach(function (b) {
      var cur = p[b.dataset.p] || (b.dataset.p === 'theme' ? 'auto' : 'm');
      b.classList.toggle('on', b.dataset.v === cur);
    });
  }
  if (prefs) {
    paintPrefs();
    prefs.addEventListener('click', function (e) {
      var b = e.target.closest('[data-p]'); if (!b) return;
      var p = get('ssa-prefs', {}); p[b.dataset.p] = b.dataset.v; put('ssa-prefs', p);
      if (b.dataset.p === 'theme') { if (b.dataset.v === 'auto') H.removeAttribute('data-theme'); else H.dataset.theme = b.dataset.v; }
      else H.dataset.size = b.dataset.v;
      paintPrefs();
    });
  }

  /* ── focus: nothing but the page and a 25-minute clock ───── */
  var clock = { left: 1500, on: null };
  var T = $('.timer');
  function draw() { $('b', T).textContent = String(Math.floor(clock.left / 60)).padStart(2, '0') + ':' + String(clock.left % 60).padStart(2, '0'); }
  function stopClock() { clearInterval(clock.on); clock.on = null; if (T) $('[data-act=timer]', T).textContent = 'המשך'; }
  function startClock() {
    clock.on = setInterval(function () {
      clock.left--; draw();
      if (clock.left <= 0) { stopClock(); clock.left = 300; draw(); toast('25 דקות. קח 5 דקות הפסקה'); }
    }, 1000);
    $('[data-act=timer]', T).textContent = 'עצור';
  }
  function toggleFocus(force) {
    if (!T) return;
    var on = force !== undefined ? force : !B.classList.contains('focus');
    B.classList.toggle('focus', on);
    if (on && !clock.on) startClock();
    if (!on) stopClock();
    draw();
  }

  /* ── clicks + keys ───────────────────────────────────────── */
  document.addEventListener('click', function (e) {
    var a = e.target.closest('[data-act]');
    if (a) {
      var w = a.dataset.act;
      if (w === 'print') { e.preventDefault(); print(); return; }
      if (w === 'search') openPal();
      else if (w === 'focus') toggleFocus();
      else if (w === 'timer') { if (clock.on) stopClock(); else startClock(); }
      else if (w === 'prefs') { e.stopPropagation(); prefs.classList.toggle('open'); }
      else if (w === 'side') B.classList.toggle('side-open');
      return;
    }
    if (e.target.classList.contains('scrim')) closeOver();
    if (prefs && prefs.classList.contains('open') && !e.target.closest('.prefs')) prefs.classList.remove('open');
  });
  document.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPal(); return; }
    if (e.key === 'Escape') { closeOver(); if (B.classList.contains('focus')) toggleFocus(false); return; }
    if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === '/') { e.preventDefault(); openPal(); return; }
    if (B.dataset.deck) return;                                        // the flash cards have their own keys
    var n = $('.pager a.next'), p = $('.pager a.prev');
    if (e.key === 'ArrowLeft' && n) location = n.href;
    if (e.key === 'ArrowRight' && p) location = p.href;
    if (e.key.toLowerCase() === 'f' || e.key === 'כ') toggleFocus();
    if (e.key.toLowerCase() === 'd' || e.key === 'ג') toggleMark();
  });
  $$('.side .on').forEach(function (a) { a.scrollIntoView({ block: 'center' }); });

  /* ── hub pages: rings, continue, countdowns ──────────────── */
  $$('[data-days]').forEach(function (e) {
    var d = Math.ceil((new Date(e.dataset.days + 'T00:00:00') - new Date(new Date().toDateString())) / 864e5);
    e.textContent = d <= 0 ? 'היום' : d === 1 ? 'מחר' : d;
    var u = e.nextElementSibling; if (u && d <= 1 && u.dataset.unit !== undefined) u.textContent = '';
  });
  /* pages you already read in the old guide count: a topic whose sections are all done there starts done (once, so
     un-marking it holds) */
  function carry(j) {
    var ch = false;
    j.forEach(function (r) {
      if (!r[2] || !r[2].length || done['mv-' + r[0]]) return;
      if (r[2].every(function (k) { return done[k]; })) {
        if (!done[r[0]]) done[r[0]] = Date.now();
        done['mv-' + r[0]] = 1; ch = true;
      }
    });
    if (ch) { put('ssa-done', done); paint(); }
  }
  $$('[data-keys]').forEach(function (el) {
    var track = el.dataset.track || 'guide', base = ROOT + el.dataset.keys + '/' + track + '/';
    fetch(base + 'keys.json').then(function (r) { return r.json(); }).then(function (j) {
      carry(j);
      var n = j.filter(function (x) { return done[x[0]]; }).length, pct = j.length ? Math.round(100 * n / j.length) : 0;
      var ring = $('.ring circle+circle', el); if (ring) ring.setAttribute('stroke-dasharray', pct + ' 100');
      var t = $('.pct', el); if (t) t.textContent = n ? pct + '% מהחומר' : 'עוד לא התחלת';
      var last = get('ssa-last', {})[el.dataset.keys], cont = $('.resume', el);
      if (cont && track === 'topics') {
        var up = j.filter(function (x) { return !done[x[0]] && x[5]; })[0];
        cont.href = up ? base + up[1] : base;
        $('span', cont).textContent = up ? (n ? 'הנושא הבא: ' : 'מתחילים: ') + up[3].slice(0, 38) : 'עברת על הכול. למסלול';
      } else if (cont) {
        var next = j.filter(function (x) { return !done[x[0]]; })[0];
        var go = last ? last.u : next ? base + next[1] : '';
        if (go) { cont.href = go; $('span', cont).textContent = last ? 'המשך: ' + last.t.slice(0, 34) : 'התחל ללמוד'; }
      }
    }).catch(function () {});
  });
  /* what to do with the time you have, and how much of the course is behind you */
  $$('[data-plan]').forEach(function (el) {
    var base = ROOT + el.dataset.plan + '/topics/', ol = $('ol', el);
    fetch(base + 'keys.json').then(function (r) { return r.json(); }).then(function (j) {
      function draw(min) {
        var sum = 0, pick = [];
        j.filter(function (r) { return !done[r[0]] && r[5]; }).some(function (r) {
          if (pick.length && sum + r[4] > min) return true;
          pick.push(r); sum += r[4]; return false;
        });
        ol.innerHTML = pick.map(function (r) { return '<li><a href="' + base + r[1] + '">' + esc(r[3]) + '</a> <span class="mute">' + r[4] + ' דק׳</span></li>'; }).join('')
          || '<li class="mute">אין כרגע נושא פתוח. כל הכבוד.</li>';
      }
      function pts(l) { return l.reduce(function (a, r) { return a + (r[6] || 0); }, 0); }
      var fin = j.filter(function (r) { return done[r[0]]; }), all = pts(j);
      $('.cov', el).textContent = 'סיימת ' + fin.length + ' מתוך ' + j.length + ' נושאים' + (all ? ', ' + Math.round(100 * pts(fin) / all) + '% ממה שחוזר במבחנים' : '') + '.';
      draw(20); el.hidden = false;
      el.addEventListener('click', function (e) {
        var b = e.target.closest('[data-min]'); if (!b) return;
        $$('[data-min]', el).forEach(function (x) { x.classList.toggle('on', x === b); });
        draw(+b.dataset.min);
      });
    }).catch(function () {});
  });
  var g = $('.greet[data-hi]');
  if (g) { var h = new Date().getHours(); g.textContent = (h < 5 ? 'לילה טוב' : h < 12 ? 'בוקר טוב' : h < 17 ? 'צהריים טובים' : h < 21 ? 'ערב טוב' : 'לילה טוב') + ', המשתמש'; }

  /* ── flash cards: a card you know comes back later, one you don't comes back soon ── */
  var deck = $('.deck');
  if (deck) {
    B.dataset.deck = '1';
    var GAP = [0, 1, 3, 7, 16, 35], SKEY = 'ssa-cards', S = get(SKEY, {}), DAY = 864e5, cards = [], q = [], cur = null;
    var fc = $('.fc', deck), acts = $('.acts', deck), sel2 = $('select', deck), left = $('.left', deck);
    function due(c) { var s = S[c.id]; return !s || s.due <= Date.now(); }
    function math(el) { if (window.renderMathInElement) renderMathInElement(el, { delimiters: [{ left: '$$', right: '$$', display: true }, { left: '$', right: '$', display: false }], throwOnError: false }); }
    function fill() {
      var ch = sel2.value;
      q = cards.filter(function (c) { return (!ch || c.c === ch) && due(c); }).sort(function () { return Math.random() - .5; });
      next();
    }
    function next() {
      cur = q.shift();
      left.textContent = cur ? 'נשארו ' + (q.length + 1) + ' להיום' : '';
      acts.classList.remove('on'); fc.classList.remove('flip');
      if (!cur) {
        var all = cards.length, known = Object.keys(S).filter(function (k) { return S[k].box >= 2; }).length;
        fc.innerHTML = '<div class="done-all"><div class="big-n">✓</div><p>סיימת את הכרטיסיות להיום.<br><span class="mute">' + known + ' מתוך ' + all + ' כבר יושבות טוב.</span></p></div>';
        return;
      }
      fc.innerHTML = '<div class="chap">' + esc(cur.c) + '</div><div class="front">' + esc(cur.f) + '</div><div class="back">' + cur.b + '</div><div class="hint">רווח או לחיצה להצגת התשובה</div>';
      math(fc);
    }
    function flip() { if (cur && !fc.classList.contains('flip')) { fc.classList.add('flip'); acts.classList.add('on'); } }
    function rate(ok) {
      if (!cur || !fc.classList.contains('flip')) return;
      var s = S[cur.id] || { box: 0 };
      s.box = ok ? Math.min(s.box + 1, GAP.length - 1) : 0;
      s.due = Date.now() + GAP[s.box] * DAY;
      S[cur.id] = s; put(SKEY, S);
      if (!ok) q.splice(Math.min(3, q.length), 0, cur);                // once more, a few cards later
      next();
    }
    fc.addEventListener('click', flip);
    acts.addEventListener('click', function (e) { var b = e.target.closest('[data-ok]'); if (b) rate(b.dataset.ok === '1'); });
    sel2.addEventListener('change', fill);
    document.addEventListener('keydown', function (e) {
      if (/INPUT|SELECT/.test(e.target.tagName)) return;
      if (e.key === ' ') { e.preventDefault(); flip(); }
      if (e.key === '1') rate(false);
      if (e.key === '2') rate(true);
    });
    fetch('cards.json').then(function (r) { return r.json(); }).then(function (j) {
      cards = j;
      var chs = []; j.forEach(function (c) { if (chs.indexOf(c.c) < 0) chs.push(c.c); });
      sel2.innerHTML = '<option value="">כל הפרקים</option>' + chs.map(function (c) { return '<option>' + esc(c) + '</option>'; }).join('');
      var go = function () { if (window.renderMathInElement) fill(); else setTimeout(go, 120); };
      go();
    });
  }

  paint();
})();
