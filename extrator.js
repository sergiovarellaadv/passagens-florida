async (rt) => {
  // Lê uma busca do Google Voos já aberta. rt = true para ida e volta.
  const S = ms => new Promise(r => setTimeout(r, ms));
  const t0 = Date.now();
  const B = () => document.body.innerText;
  const T = () => [...document.querySelectorAll('[role=tab]')];
  const N = s => +String(s || '').replace(/[^0-9]/g, '');
  const H = () => [...document.querySelectorAll('h2,h3,[role=heading]')].map(h => h.innerText).join(' | ');
  const R = () => [...document.querySelectorAll('li [aria-label]')]
    .filter(e => /^(Preço total a partir de|A partir de) /.test(e.getAttribute('aria-label')))
    .map(e => {
      const a = e.getAttribute('aria-label'), li = e.closest('li');
      const t = [...a.matchAll(/às (\d\d:\d\d)/g)].map(m => m[1]);
      return {
        el: e,
        p: N((a.match(/a partir de ([\d.]+) Reais/i) || [])[1]),
        st: /autotransfer/i.test(li ? li.innerText : a),
        c: ((a.match(/Voo d[aoe]s? (.+?) com/) || [])[1] || '').trim(),
        x: /sem escalas|direto/i.test(a) ? 0 : N((a.match(/com (\d+) paradas?/) || [])[1]),
        h: (t[0] || '') + '-' + (t[1] || ''),
        d: ((a.match(/Duração total: ([^.]+)\./) || [])[1] || '').trim()
      };
    }).filter(r => r.p > 0).sort((a, b) => a.p - b.p);
  const W = async (m = 45) => { for (let i = 0; i < m; i++) { await S(1000); if (!/Buscando resultados/.test(B())) return 1; } return 0; };
  const F = r => r ? [r.p, r.c, r.x, r.h, r.d, r.st ? 1 : 0] : null;
  const naIda = () => R().length && !/Opções de reserva/.test(B()) && (!rt || /voos de ida/i.test(H()));

  // Lê a tela de opções de reserva
  const lerReserva = async () => {
    for (let i = 0; i < 25; i++) { await S(1000); if (/Opções de reserva/.test(B())) break; }
    await S(2500);
    const mais = [...document.querySelectorAll('[role=button],button')].concat([...document.querySelectorAll('div,span')])
      .find(e => /^Mais \d+ opç(ão|ões) de reserva$/.test((e.innerText || '').trim()));
    if (mais) { mais.click(); await S(2000); }
    const chg = /preço que você viu na página anterior mudou/.test(B()) ? 1 : 0;
    const s = (B().split('Opções de reserva')[1] || '').split(/Informações de preço|Os preços incluem/)[0];
    const bk = s.split('Reserve com ').slice(1).map(x => {
      const m = x.match(/R\$\s?([\d.]+)/);
      return m ? [x.split('\n')[0].replace(/^(a|o) /, '').replace('Companhia aérea', '').trim(), N(m[1])] : null;
    }).filter(Boolean).sort((a, b) => a[1] - b[1]).slice(0, 3);
    return { chg, bk };
  };

  // Abre um voo de ida; na ida e volta, escolhe também a volta mais barata
  const abrir = async el => {
    el.click();
    if (rt) {
      let ok = false;
      for (let i = 0; i < 25; i++) { await S(1000); if (/voos de volta/i.test(H()) && R().length) { ok = true; break; } if (/Opções de reserva/.test(B())) break; }
      if (ok) {
        await W(); await S(1500);
        const volta = R().filter(r => !r.st)[0] || R()[0];
        if (!volta) return { chg: 0, bk: [], back: 1 };
        o.volta = F(volta);
        volta.el.click();
      }
      const r = await lerReserva(); r.back = ok ? 2 : 1; return r;
    }
    const r = await lerReserva(); r.back = 1; return r;
  };

  const o = { ok: 0, rt: rt ? 1 : 0 };
  for (let i = 0; i < 40 && !T().length; i++) await S(500);
  o.dt = [...document.querySelectorAll('input')].map(i => i.value).filter(v => /\d+ de [a-z]{3}/.test(v)).slice(0, 2).join(' / ');
  if (/captcha|tráfego incomum|unusual traffic|não sou um robô|not a robot/i.test(B()) || !T().length) {
    o.err = 'BLOQUEIO'; o.snip = B().slice(0, 200);
  } else {
    await W(); await S(1500);
    const b = R(); o.best = b[0] ? b[0].p : null;
    const ct = T().find(t => /Menores preços/.test(t.innerText));
    if (ct) { ct.click(); await S(2000); o.ld = await W(); await S(3000); }
    const c = R(); o.cheap = F(c[0]);
    // Se o voo regular mais barato estiver só em "Melhor opção", volta para essa aba
    const bReg = b.filter(r => !r.st)[0], cReg = c.filter(r => !r.st)[0];
    let lista = c;
    if (bReg && (!cReg || bReg.p < cReg.p)) {
      const bt = T().find(t => /Melhor opção/.test(t.innerText));
      if (bt) { bt.click(); await S(2500); await W(); await S(1500); lista = R(); }
    }
    const todos = (lista.length ? lista : b).filter(r => !r.st);
    const tentados = []; o.tr = []; let win = null; const ruins = new Set(); let voltar = 0;
    for (let j = 0; j < todos.length && o.tr.length < 3; j++) {
      const q = todos[j]; if (ruins.has(q.c)) continue;
      if (o.tr.length > 0) {
        for (let k = 0; k < voltar; k++) { history.back(); await S(1500); }
        for (let i = 0; i < 20; i++) { await S(1000); if (naIda()) break; }
        await S(2000);
      }
      const el = o.tr.length === 0 ? q.el : (R().filter(r => !r.st).find(r => r.p === q.p && r.h === q.h) || {}).el;
      if (!el) break;
      tentados.push(q);
      const { chg, bk, back } = await abrir(el); voltar = back;
      o.tr.push([q.p, bk[0] ? bk[0][1] : null, chg]);
      if (chg) ruins.add(q.c);
      if (bk.length && (!win || bk[0][1] < win.bk[0][1])) win = { r: q, bk, chg, volta: o.volta };
      if (bk.length && !chg) break;
    }
    const r0 = win ? win.r : tentados[0];
    o.reg = F(r0); o.book = win ? win.bk : []; o.conf = win ? 1 : 0;
    if (win && win.volta) o.volta = win.volta;
    o.fin = win ? win.bk[0][1] : (r0 ? r0.p : null);
    o.ok = o.cheap ? 1 : 0;
  }
  o.s = Math.round((Date.now() - t0) / 1000);
  return JSON.stringify(o);
}
