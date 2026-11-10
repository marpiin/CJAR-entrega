const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const ICON = {
  upload: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/></svg>',
  cam: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>',
  ok: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M20 6 9 17l-5-5"/></svg>',
  bad: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>',
};
// Silueta de carné con los 5 puntos de la plantilla ArcFace (ojos, nariz, comisuras)
const LMK = [[38.29, 51.70], [73.53, 51.50], [56.03, 71.74], [41.55, 92.37], [70.73, 92.20]];
const SILUETA = `<svg viewBox="0 0 112 140" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
  <ellipse cx="56" cy="62" rx="30" ry="38"/><path d="M14 140c4-24 20-34 42-34s38 10 42 34"/>
  ${LMK.map(([x, y]) => `<path d="M${x - 4} ${y * .62 + 30}h8M${x} ${y * .62 + 26}v8" stroke-width="1.6"/>`).join('')}</svg>`;
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// Solo se pintan imágenes generadas por el servidor (data:image/jpeg;base64) o locales (blob:)
const imgSrc = s => typeof s === 'string' && /^(data:image\/(jpeg|png);base64,[A-Za-z0-9+/=]+|blob:)/.test(s) ? esc(s) : '';

// Guilloché de la cabecera
const gCanvas = $('#guilloche');
function drawGuilloche() {
  const dpr = window.devicePixelRatio || 1;
  const w = gCanvas.clientWidth, h = gCanvas.clientHeight;
  gCanvas.width = w * dpr; gCanvas.height = h * dpr;
  const ctx = gCanvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const css = getComputedStyle(document.documentElement);
  const inks = [css.getPropertyValue('--guilloche-a').trim(), css.getPropertyValue('--guilloche-b').trim()];
  const x0 = Math.min(w * 0.28, 260);
  ctx.lineWidth = 0.7;
  for (let k = 0; k < 26; k++) {
    ctx.strokeStyle = inks[k % 2];
    ctx.beginPath();
    for (let x = x0; x <= w; x += 2) {
      const t = (x - x0) / (w - x0);
      const amp = h * 0.36 * Math.sin(Math.PI * Math.min(1, t * 1.6));
      const y = h / 2 + amp * Math.sin(x / 34 + k * 0.24) * Math.cos(x / 97 - k * 0.13);
      ctx.globalAlpha = Math.min(1, t * 2.2);
      x === x0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
}
drawGuilloche();
window.addEventListener('resize', drawGuilloche);
matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', drawGuilloche);
if (document.fonts) document.fonts.ready.then(drawGuilloche);

const state = { modo: 'completo', usado: 'completo' };  // usado: flujo de la última petición

$$('#modo button').forEach(b => b.onclick = () => {
  $$('#modo button').forEach(x => x.setAttribute('aria-pressed', x === b));
  state.modo = b.dataset.v;
});

const tabBtns = $$('.tab');
function activateTab(b) {
  tabBtns.forEach(x => x.setAttribute('aria-selected', x === b));
  $$('.panel').forEach(p => p.classList.toggle('on', p.id === 'p-' + b.dataset.t));
  if (b.dataset.t === 'personas') loadInfo();
}
tabBtns.forEach(b => b.onclick = () => activateTab(b));

let toastTimer;
function toast(msg, type = 'err') {
  const t = $('#toast');
  t.textContent = msg; t.className = 'toast on ' + type;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.className = 'toast', 3500);
}

const lightbox = $('#lightbox');
function openLightbox(src) { $('img', lightbox).src = src; lightbox.classList.add('on'); }
lightbox.onclick = () => lightbox.classList.remove('on');
document.addEventListener('keydown', e => { if (e.key === 'Escape') lightbox.classList.remove('on'); });
document.addEventListener('click', e => {
  const z = e.target.closest('[data-zoom]');
  if (z) { const img = $('img', z) || z; if (img.src) openLightbox(img.src); }
});

function asyncConfirm(msg, ok = 'Borrar') {
  return new Promise(resolve => {
    const m = $('#custom-confirm');
    $('#confirm-msg').textContent = msg;
    $('#confirm-ok').textContent = ok;
    $('#confirm-title').textContent = ok === 'Borrar' ? 'Borrar registro' : 'Confirmar';
    m.classList.add('on');
    $('#confirm-cancel').focus();
    const close = res => { m.classList.remove('on'); resolve(res); };
    $('#confirm-cancel').onclick = () => close(false);
    $('#confirm-ok').onclick = () => close(true);
  });
}

// Selector de foto (subir, arrastrar o cámara).
// Se redimensiona en el navegador: respeta la rotación EXIF de las fotos del móvil
// (OpenCV la ignora) y evita subir imágenes enormes.
async function normalize(file) {
  const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
  const scale = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));
  const c = document.createElement('canvas');
  c.width = Math.round(bmp.width * scale); c.height = Math.round(bmp.height * scale);
  c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
  return new Promise(r => c.toBlob(r, 'image/jpeg', 0.92));
}

const pickers = {};
$$('[data-picker]').forEach(el => {
  const key = el.dataset.picker, multi = 'multi' in el.dataset;
  const vacio = multi ? 'Arrastra una o varias fotos' : 'Arrastra una foto';
  el.innerHTML = `
    <div class="slot${multi ? ' wide' : ''}" tabindex="0" role="button" aria-label="${multi ? 'Elegir fotos' : 'Elegir foto'}"><button class="clear" type="button" title="Quitar foto">✕</button>
      <div class="hint">${multi ? '' : SILUETA}<div>${vacio}<br>o haz clic para elegir${multi ? 'las' : 'la'}</div></div></div>
    ${multi ? '<div class="thumbs"></div>' : ''}
    <input type="file" accept="image/*" ${multi ? 'multiple' : ''} hidden>
    <div class="row"><button class="btn up" type="button">${ICON.upload}${multi ? 'Añadir fotos' : 'Subir'}</button><button class="btn cam" type="button">${ICON.cam}Cámara</button></div>`;
  const slot = $('.slot', el), input = $('input', el), thumbs = $('.thumbs', el);
  const p = pickers[key] = { blob: null, blobs: [] };

  const renderThumbs = () => {
    thumbs.innerHTML = '';
    p.blobs.forEach((b, i) => {
      const t = document.createElement('div'); t.className = 'thumb';
      t.innerHTML = `<img src="${imgSrc(URL.createObjectURL(b))}" alt="Foto ${i + 1}"><span class="n">${i + 1}</span><button type="button" title="Quitar">✕</button>`;
      $('button', t).onclick = () => { p.blobs.splice(i, 1); renderThumbs(); };
      thumbs.appendChild(t);
    });
    $('.hint div', slot).innerHTML = p.blobs.length
      ? `${p.blobs.length} foto${p.blobs.length > 1 ? 's' : ''}. Haz clic o arrastra para añadir más`
      : 'Arrastra una o varias fotos<br>o haz clic para elegirlas';
  };
  p.reset = () => { p.blob = null; p.blobs = []; if (multi) renderThumbs(); else { $('img', slot)?.remove(); slot.classList.remove('has'); } };
  p.add = async files => {
    for (const f of files) {
      let b;
      try { b = await normalize(f); } catch { toast('No se ha podido leer una de las imágenes. Prueba con un JPEG o PNG.'); continue; }
      if (multi) { p.blobs.push(b); continue; }
      p.blob = b;
      $('img', slot)?.remove();
      const img = new Image(); img.src = URL.createObjectURL(b); img.alt = 'Foto elegida';
      slot.appendChild(img); slot.classList.add('has');
    }
    if (multi) renderThumbs();
  };
  const open = () => input.click();
  slot.onclick = e => { if (!e.target.closest('.clear')) open(); };
  slot.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } };
  $('.up', el).onclick = open;
  $('.cam', el).onclick = () => camera(b => p.add([b]));
  $('.clear', slot).onclick = e => { e.stopPropagation(); p.reset(); };
  input.onchange = () => { p.add([...input.files]); input.value = ''; };
  slot.ondragover = e => { e.preventDefault(); slot.classList.add('drag'); };
  slot.ondragleave = () => slot.classList.remove('drag');
  slot.ondrop = e => {
    e.preventDefault(); slot.classList.remove('drag');
    const files = [...e.dataTransfer.files].filter(f => f.type.startsWith('image/'));
    if (files.length) p.add(multi ? files : files.slice(0, 1));
  };
});

// Cámara
let stream, onShot;
async function camera(cb) {
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { width: 1280, height: 720, facingMode: 'user' } });
  } catch { return toast('No se puede acceder a la cámara. Da permiso en el navegador y abre la web en http://localhost.'); }
  onShot = cb; $('#cam-video').srcObject = stream; $('#cam').classList.add('on');
}
function closeCam() { stream?.getTracks().forEach(t => t.stop()); $('#cam').classList.remove('on'); }
$('#cam-cancel').onclick = closeCam;
$('#cam-shot').onclick = () => {
  const v = $('#cam-video'), c = document.createElement('canvas');
  c.width = v.videoWidth; c.height = v.videoHeight;
  const ctx = c.getContext('2d'); ctx.translate(c.width, 0); ctx.scale(-1, 1); ctx.drawImage(v, 0, 0);
  c.toBlob(b => { onShot(b); closeCam(); }, 'image/jpeg', 0.92);
};

// El servidor rechaza cualquier petición que cambie datos sin esta cabecera: otra web
// abierta en el navegador no puede añadirla, así que no puede inscribir ni borrar (CSRF)
const CSRF = { 'X-CJAR': '1' };
// Clave de administración: se guarda solo en esta pestaña y se manda en cada petición
const keyInput = $('#admin-key');
const store = { get: () => { try { return sessionStorage.getItem('cjar-key') || ''; } catch { return ''; } },
                set: v => { try { sessionStorage.setItem('cjar-key', v); } catch { /* sin almacenamiento */ } } };
keyInput.value = store.get();
keyInput.onchange = () => { store.set(keyInput.value.trim()); loadInfo(); };
const headers = () => ({ ...CSRF, 'X-CJAR-Token': keyInput.value.trim() });
const NO_KEY = 'Hace falta la clave de administración (aparece en el log del servidor al arrancar).';
async function api(path, form, btn, avisos = []) {
  const label = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Analizando…';
  try {
    // Verificar e identificar siempre usan el flujo completo (el servidor no admite otro)
    state.usado = /verificar|identificar/.test(path) ? 'completo' : state.modo;
    if (state.usado !== 'completo') form.append('modo', state.usado);
    const r = await fetch(path, { method: 'POST', body: form, headers: headers() });
    const data = await r.json().catch(() => ({}));
    if (avisos.includes(r.status)) return { conflicto: r.status, ...data };
    if (r.status === 401) throw new Error(NO_KEY);
    if (!r.ok) throw new Error(data.detail || 'El servidor ha devuelto un error.');
    return data;
  } catch (e) {
    toast(e.message === 'Failed to fetch' ? 'No hay conexión con el servidor. Comprueba que está arrancado.' : e.message);
    return null;
  } finally { btn.disabled = false; btn.innerHTML = label; }
}
const need = key => { if (!pickers[key].blob) { toast('Primero elige una foto.'); return false; } return true; };

// Resultados
const empty = txt => `<div class="empty">${SILUETA}<p>${txt}</p></div>`;
$('#ins-res').innerHTML = empty('Aquí aparecerán las caras alineadas que se han guardado y, con varias fotos, lo coherentes que son entre sí.');
$('#ver-res').innerHTML = empty('Aquí aparecerá el resultado y, si coincide, en qué rasgos se parece la foto a la persona.');
$('#idf-res').innerHTML = empty('Aquí aparecerá quién es, si supera el umbral, y en qué rasgos se parece.');
$('#cmp-res').innerHTML = empty('Elige dos fotos para ver su similitud y en qué rasgos se parecen.');

const pct = s => Math.max(0, Math.min(1, s)) * 100;
const verdict = (ok, title, sub) => `
  <div class="verdict ${ok ? 'ok' : 'bad'}"><span class="seal">${ok ? ICON.ok : ICON.bad}</span>
  <div><h3>${title}</h3><p>${sub}</p></div></div>`;
const gauge = (score, th) => `
  <div class="gauge ${score >= th ? 'ok' : 'bad'}">
    <div class="gauge-head"><span>Similitud coseno</span><strong>${score.toFixed(3)}</strong></div>
    <div class="gauge-track"><span class="fill" data-w="${pct(score)}"></span><span class="th" style="left:${pct(th)}%"><span>umbral ${th.toFixed(2)}</span></span></div>
    <div class="gauge-ticks"><span>0</span><span>0.25</span><span>0.50</span><span>0.75</span><span>1</span></div>
  </div>`;
const marks = () => state.usado === 'sd' ? '' :
  `<svg class="lmk" viewBox="0 0 112 112" aria-hidden="true">${LMK.map(([x, y]) => `<path d="M${x - 3} ${y}h6M${x} ${y - 3}v6"/>`).join('')}</svg>`;
const fig = (src, cap, extra = '') => src ? `
  <figure class="fig"><div class="frame" data-zoom><img src="${imgSrc(src)}" alt="${esc(cap)}">${marks()}${extra}</div><figcaption>${cap}</figcaption></figure>` : '';
const figs = list => `<div class="figs">${list.map(([src, cap]) => fig(src, cap)).join('')}</div>`;
const faceLabel = () => state.usado === 'sd' ? 'Entrada al extractor' : 'Alineada (112 px)';
function show(el, html) {
  el.innerHTML = html;
  requestAnimationFrame(() => $$('.fill[data-w]', el).forEach(f => f.style.width = f.dataset.w + '%'));
}

// En qué zonas se parecen (src/explicador.py)
const ZONA_MIN = 0.01; // por debajo de esto la zona apenas influye en la similitud
const lista = xs => xs.length > 1 ? xs.slice(0, -1).join(', ') + ' y ' + xs[xs.length - 1] : xs[0];
function cotejo(x, labels, nota = '') {
  if (!x) return '';
  const max = Math.max(0.02, ...x.regiones.map(r => Math.abs(r.aporte)));
  const si = x.regiones.filter(r => r.aporte >= ZONA_MIN).map(r => esc(r.nombre.toLowerCase()));
  const no = x.regiones.filter(r => r.aporte <= -ZONA_MIN).map(r => esc(r.nombre.toLowerCase())).reverse();
  const resumen = (si.length ? `Se parecen sobre todo en <span class="si">${lista(si.slice(0, 3))}</span>.` : 'Ninguna zona sostiene claramente el parecido.')
    + (no.length ? ` Les diferencia: <span class="no">${lista(no)}</span>.` : '');
  const filas = x.regiones.map(r => {
    const tipo = r.aporte >= ZONA_MIN ? 'si' : r.aporte <= -ZONA_MIN ? 'no' : 'neutro';
    return `<li class="${tipo}" tabindex="0" data-caja="${r.caja.map(Number).join(',')}">
      <span>${esc(r.nombre)}</span>
      <span class="dbar"><span class="fill ${r.aporte >= 0 ? 'pos' : 'neg'}" data-w="${Math.min(1, Math.abs(r.aporte) / max) * 50}"></span></span>
      <span class="t-val">${r.aporte >= 0 ? '+' : '−'}${Math.abs(r.aporte).toFixed(3)}</span>
      <span class="t-word">${{ si: 'Coincide', no: 'Difiere', neutro: 'Sin peso' }[tipo]}</span></li>`;
  }).join('');
  const mapas = x.mapas.map((m, i) => m ? `
    <figure class="fig"><div class="frame" data-zoom><img src="${imgSrc(m)}" alt="Mapa de ${esc(labels[i])}"><span class="zone"></span></div><figcaption>${labels[i]}</figcaption></figure>` : '').join('');
  if (state.usado === 'sd') nota += (nota ? ' ' : '') + 'En el flujo sin detector la cara no está alineada, así que las zonas son aproximadas.';
  return `<section class="cotejo">
    <h4>En qué se parecen</h4>
    <p class="lead">Se tapa cada zona de la cara y se vuelve a medir la similitud. La cifra son los puntos que se pierden al taparla. Pasa el ratón por una zona para verla en el mapa; izquierda y derecha son como se ve en la foto.</p>
    <p class="resumen">${resumen}</p>
    <div class="cotejo-body"><div class="maps">${mapas}</div><ol class="traits">${filas}</ol></div>
    <p class="legend"><span><i style="background:var(--match)"></i>Coincide: al taparla baja la similitud</span><span><i style="background:var(--nomatch)"></i>Difiere: al taparla sube</span></p>
    ${nota ? `<p class="aside">${nota}</p>` : ''}</section>`;
}
function highlight(row, on) {
  const box = row.closest('.cotejo');
  box.classList.toggle('hl', on);
  if (!on) return;
  const [x0, y0, x1, y1] = row.dataset.caja.split(',').map(v => v / 1.12); // 112 px → %
  $$('.zone', box).forEach(z => Object.assign(z.style, { left: x0 + '%', top: y0 + '%', width: x1 - x0 + '%', height: y1 - y0 + '%' }));
}
const traitOf = e => e.target.closest?.('.traits li');
document.addEventListener('mouseover', e => { const r = traitOf(e); if (r) highlight(r, true); });
document.addEventListener('mouseout', e => { const r = traitOf(e); if (r && !r.contains(e.relatedTarget)) highlight(r, false); });
document.addEventListener('focusin', e => { const r = traitOf(e); if (r) highlight(r, true); });
document.addEventListener('focusout', e => { const r = traitOf(e); if (r) highlight(r, false); });
// En verificar/identificar solo hay mapa de la foto de la BD si se guardó al inscribir
const notaRef = (x, id) => x && !x.mapas[1]
  ? `No se muestra la foto guardada de ${esc(id)} (solo la ve el administrador): se enseña únicamente tu foto frente a su plantilla.` : '';

$('#ins-go').onclick = async e => {
  const id = $('#ins-id').value.trim();
  if (!id) return toast('Escribe un identificador.');
  const blobs = pickers.ins.blobs;
  if (!blobs.length) return toast('Añade al menos una foto.');
  const form = (reemplazar = false) => {
    const f = new FormData(); f.append('id', id);
    if (reemplazar) f.append('reemplazar', 'true');
    blobs.forEach((b, i) => f.append('fotos', b, `foto${i + 1}.jpg`));
    return f;
  };
  const btn = e.currentTarget;
  let d = await api('/api/inscribir', form(), btn, [409]);
  if (d && d.conflicto) {
    if (!(await asyncConfirm(`«${id}» ya está inscrito. ¿Sustituir su plantilla por la de estas fotos?`, 'Sustituir'))) return;
    d = await api('/api/inscribir', form(true), btn);
  }
  if (!d) return;
  const n = d.caras.length;
  const coh = n > 1 ? `<div class="block"><h4>Coherencia entre fotos</h4><p>Parecido de cada foto con la media de las demás.</p>
    <ol class="ranking">${d.coherencia.map((c, i) => `
      <li><span class="pos">${i + 1}</span><span class="who">Foto ${i + 1}</span><span class="val ${c < d.umbral ? 'low' : ''}">${c.toFixed(3)}</span>
      <span class="bar"><span class="fill ${c < d.umbral ? 'bad' : 'ok'}" data-w="${pct(c)}"></span><span class="th" style="left:${pct(d.umbral)}%"></span></span></li>`).join('')}</ol>
    ${d.coherencia.some(c => c < d.umbral) ? '<p class="aside">Las fotos en rojo no pasarían una verificación contra el resto. Puede que sean de mala calidad o de otra persona: prueba a quitarlas.</p>' : ''}</div>` : '';
  show($('#ins-res'), verdict(true, d.actualizado ? `${esc(d.id)} actualizado` : `${esc(d.id)} inscrito`,
    n > 1 ? `Media de ${n} plantillas guardada en la base de datos` : 'Plantilla de 512 dimensiones guardada en la base de datos') +
    coh + figs(d.caras.map((c, i) => [c, n > 1 ? `Foto ${i + 1}` : faceLabel()])));
  if (d.foto_guardada === false) toast('Inscrita, pero no se ha podido guardar su foto: revisa los permisos de fotos/ (mira el log del servidor).');
  else toast('Inscripción completada', 'ok');
  pickers.ins.reset();
  loadInfo();
};

$('#ver-go').onclick = async e => {
  const id = $('#ver-id').value.trim();
  if (!id) return toast('Escribe un identificador.');
  if (!need('ver')) return;
  const f = new FormData(); f.append('id', id); f.append('foto', pickers.ver.blob, 'foto.jpg');
  const d = await api('/api/verificar', f, e.currentTarget);
  if (!d) return;
  // El servidor solo devuelve la decisión: la similitud no se expone (evita ataques por aproximación)
  show($('#ver-res'), verdict(d.match, d.match ? `Es ${esc(id)}` : `No es ${esc(id)}`,
    d.match ? 'La similitud supera el umbral del sistema' : 'No coincide con la persona indicada o esa persona no está inscrita') +
    figs([[d.cara, faceLabel()]]) +
    cotejo(d.explicacion, ['Tu foto', `Foto de ${esc(id)}`], notaRef(d.explicacion, id)));
};

$('#idf-go').onclick = async e => {
  if (!need('idf')) return;
  const f = new FormData(); f.append('foto', pickers.idf.blob, 'foto.jpg');
  const d = await api('/api/identificar', f, e.currentTarget);
  if (!d) return;
  show($('#idf-res'), verdict(!!d.id, d.id ? `Identificado: ${esc(d.id)}` : 'Persona desconocida',
    d.id ? 'La mejor coincidencia supera el umbral del sistema' : 'Nadie de la base de datos supera el umbral') +
    figs([[d.cara, faceLabel()]]) +
    (d.id ? cotejo(d.explicacion, ['Tu foto', `Foto de ${esc(d.id)}`], notaRef(d.explicacion, d.id)) : ''));
};

$('#cmp-go').onclick = async e => {
  if (!need('c1') || !need('c2')) return;
  const f = new FormData(); f.append('foto1', pickers.c1.blob, '1.jpg'); f.append('foto2', pickers.c2.blob, '2.jpg');
  const d = await api('/api/comparar', f, e.currentTarget);
  if (!d) return;
  show($('#cmp-res'), verdict(d.match, d.match ? 'Misma persona' : 'Personas distintas',
    `Con el umbral del sistema, ${d.umbral.toFixed(2)}`) + gauge(d.score, d.umbral) + figs([[d.caras[0], 'Foto 1'], [d.caras[1], 'Foto 2']]) +
    cotejo(d.explicacion, ['Foto 1', 'Foto 2']));
};

// Base de datos
let personas = [];
function renderPeople() {
  const q = $('#filtro').value.trim().toLowerCase();
  const lista = personas.filter(p => p.toLowerCase().includes(q));
  $('#people').innerHTML = !personas.length
    ? '<p class="people-empty">No hay nadie inscrito todavía. Ve a <b>Inscribir</b> para empezar.</p>'
    : !lista.length ? `<p class="people-empty">Nadie coincide con «${esc(q)}».</p>`
    : lista.map(p => `
      <div class="person"><div class="avatar">${esc(p[0].toUpperCase())}<img data-foto="${esc(p)}" alt="" data-zoom hidden></div>
      <span class="n" title="${esc(p)}">${esc(p)}</span><button data-del="${esc(p)}" title="Borrar a ${esc(p)}" aria-label="Borrar a ${esc(p)}">${ICON.trash}</button></div>`).join('');
  // Las fotos necesitan la clave, así que se piden con fetch; sin foto se queda la inicial
  $$('.avatar img[data-foto]').forEach(async img => {
    try {
      const r = await fetch(`/fotos/${encodeURIComponent(img.dataset.foto)}.jpg`, { headers: headers() });
      if (!r.ok) return img.remove();
      img.src = URL.createObjectURL(await r.blob()); img.hidden = false;
    } catch { img.remove(); }
  });
  $$('[data-del]').forEach(b => b.onclick = async () => {
    const id = b.dataset.del;
    if (!(await asyncConfirm(`Se borrará a «${id}» de la base de datos. No se puede deshacer.`))) return;
    const r = await fetch('/api/personas/' + encodeURIComponent(id), { method: 'DELETE', headers: headers() });
    r.ok ? toast(`«${id}» borrado`, 'ok') : toast(r.status === 401 ? NO_KEY : 'No se ha podido borrar. Recarga la página y vuelve a intentarlo.');
    loadInfo();
  });
}
$('#filtro').oninput = renderPeople;
async function loadInfo() {
  try {
    const d = await (await fetch('/api/info', { headers: headers() })).json();
    $('#status-dot').classList.remove('off'); $('#status-text').textContent = 'Sistema activo';
    $('#count').textContent = d.inscritos;
    personas = d.personas || [];
    $('#ids').innerHTML = personas.map(p => `<option value="${esc(p)}">`).join('');
    if (!d.admin) { $('#people').innerHTML = `<p class="people-empty">${esc(NO_KEY)} Sin ella solo se ve cuántas personas hay inscritas.</p>`; return; }
    renderPeople();
  } catch {
    $('#status-dot').classList.add('off'); $('#status-text').textContent = 'Sin conexión';
  }
}
loadInfo();
