/* LemonPDF — logique de l'interface.
   Chaque page est une image rendue par le serveur, surmontée d'une couche de
   fragments de texte éditables positionnés aux coordonnées exactes du PDF. */

'use strict';

const RENDER_SCALE = 2;          // qualité de rendu des pages (facteur PDF -> pixels)
const ZOOM_STEPS = [0.5, 0.65, 0.8, 1, 1.25, 1.5, 2, 3];

const state = {
  docId: null,
  name: '',
  pages: [],
  version: 0,
  zoom: 1,
  adding: false,
  editing: null,   // élément .tf en cours d'édition
  fonts: [],       // polices proposées par le serveur
  style: null,     // barre de style ouverte sur le fragment en cours
};

const $ = (sel) => document.querySelector(sel);
const el = {
  main: $('#main'),
  viewer: $('#viewer'),
  dropzone: $('#dropzone'),
  fileInput: $('#file-input'),
  docTools: $('#doc-tools'),
  docActions: $('#doc-actions'),
  filename: $('#filename'),
  undo: $('#btn-undo'),
  redo: $('#btn-redo'),
  zoomLabel: $('#zoom-label'),
  findPanel: $('#find-panel'),
  findSearch: $('#find-search'),
  findReplace: $('#find-replace'),
  findCase: $('#find-case'),
  findInfo: $('#find-info'),
  overlay: $('#overlay'),
  overlayText: $('#overlay-text'),
  toast: $('#toast'),
  compress: $('#btn-compress'),
};

/* ------------------------------------------------------------------ utils */

let busyCount = 0;
/** `mode: 'squeeze'` bascule l'overlay sur l'illustration citron pressé. */
function busy(on, text, mode) {
  busyCount = Math.max(0, busyCount + (on ? 1 : -1));
  el.overlayText.textContent = text || 'Traitement…';
  el.overlay.hidden = busyCount === 0;
  if (busyCount === 0 || mode) el.overlay.classList.toggle('squeezing', mode === 'squeeze');
}

let toastTimer;
function toast(message, isError) {
  el.toast.textContent = message;
  el.toast.classList.toggle('error', !!isError);
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.toast.hidden = true; }, isError ? 5000 : 2600);
}

const NETWORK_ERROR = 'Le serveur ne répond plus. Vérifiez qu’il tourne toujours dans le terminal.';

async function api(path, options) {
  let res;
  try {
    res = await fetch(path, options);
  } catch (_) {
    // « Failed to fetch » : aucune réponse HTTP. Le plus souvent une connexion
    // persistante fermée par le serveur, parfois le serveur arrêté.
    const err = new Error(NETWORK_ERROR);
    err.network = true;
    throw err;
  }
  if (!res.ok) {
    let detail = `Erreur ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* réponse non JSON */ }
    throw new Error(detail);
  }
  return res.json();
}

const postJSON = (path, body) => api(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

/* ----------------------------------------------------------------- upload */

el.fileInput.addEventListener('change', () => {
  if (el.fileInput.files[0]) openFile(el.fileInput.files[0]);
  el.fileInput.value = '';
});

['dragenter', 'dragover'].forEach((ev) =>
  el.dropzone.addEventListener(ev, (e) => { e.preventDefault(); el.dropzone.classList.add('hover'); }));
['dragleave', 'drop'].forEach((ev) =>
  el.dropzone.addEventListener(ev, () => el.dropzone.classList.remove('hover')));
el.dropzone.addEventListener('drop', (e) => {
  e.preventDefault();
  const file = e.dataTransfer.files[0];
  if (file) openFile(file);
});

async function openFile(file) {
  if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') {
    return toast('Ce fichier n’est pas un PDF.', true);
  }
  const form = new FormData();
  form.append('file', file);
  busy(true, 'Ouverture du PDF…');
  try {
    const data = await api('/api/upload', { method: 'POST', body: form });
    state.docId = data.doc_id;
    state.name = data.name;
    applyState(data);
    el.dropzone.hidden = true;
    el.viewer.hidden = false;
    el.docTools.hidden = false;
    el.docActions.hidden = false;
    el.filename.textContent = data.name;
    buildPages();
    toast(`${data.pages.length} page(s) chargée(s)`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false);
  }
}

function applyState(data) {
  state.pages = data.pages;
  state.version = data.version;
  el.undo.disabled = !data.can_undo;
  el.redo.disabled = !data.can_redo;
}

/* --------------------------------------------------------------- rendu pages */

const observer = new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (entry.isIntersecting) loadPage(entry.target);
  });
}, { root: el.main, rootMargin: '600px 0px' });

function buildPages() {
  el.viewer.innerHTML = '';
  state.pages.forEach((page) => {
    const node = document.createElement('div');
    node.className = 'page';
    node.dataset.page = page.number;
    node.dataset.loaded = '';
    node.style.width = `${page.width * state.zoom}px`;
    node.style.height = `${page.height * state.zoom}px`;

    const img = document.createElement('img');
    img.alt = `Page ${page.number + 1}`;
    img.draggable = false;
    node.appendChild(img);

    const layer = document.createElement('div');
    layer.className = 'layer';
    node.appendChild(layer);

    const badge = document.createElement('div');
    badge.className = 'page-badge';
    badge.textContent = `${page.number + 1} / ${state.pages.length}`;
    node.appendChild(badge);

    node.addEventListener('mousedown', onPageMouseDown);
    el.viewer.appendChild(node);
    observer.observe(node);
  });
  // Les premières pages sont chargées sans attendre l'observateur.
  [...el.viewer.children].slice(0, 3).forEach(loadPage);
}

const pageNode = (pno) => el.viewer.querySelector(`.page[data-page="${pno}"]`);

/** Applique le zoom courant : les images suivent en CSS, la couche texte est redessinée. */
function sizePages() {
  el.viewer.querySelectorAll('.page').forEach((node) => {
    const page = state.pages[+node.dataset.page];
    node.style.width = `${page.width * state.zoom}px`;
    node.style.height = `${page.height * state.zoom}px`;
    if (node._items) drawLayer(node, node._items);
  });
}

async function loadPage(node) {
  const pno = +node.dataset.page;
  if (node.dataset.loaded === String(state.version)) return;
  node.dataset.loaded = String(state.version);

  const img = node.querySelector('img');
  img.src = `/api/${state.docId}/page/${pno}.png?scale=${RENDER_SCALE}&v=${state.version}`;
  try {
    const data = await api(`/api/${state.docId}/page/${pno}/items`);
    if (data.version !== state.version) return;   // état obsolète entre-temps
    node._items = data.items;
    drawLayer(node, data.items);
  } catch (err) {
    toast(err.message, true);
  }
}

/** Reconstruit la couche de texte éditable d'une page. */
function drawLayer(node, items) {
  const layer = node.querySelector('.layer');
  layer.innerHTML = '';
  const z = state.zoom;

  items.forEach((item) => {
    const [x0, y0, x1, y1] = item.bbox;
    const div = document.createElement('div');
    div.className = 'tf';
    div.textContent = item.text;
    div.dataset.id = item.id;
    div.dataset.original = item.text;
    div.dataset.alias = item.font;
    div.dataset.bold = item.bold ? '1' : '';
    div.dataset.italic = item.italic ? '1' : '';

    const height = Math.max(y1 - y0, item.size);
    div.style.left = `${x0 * z}px`;
    div.style.top = `${y0 * z}px`;
    div.style.minWidth = `${(x1 - x0) * z}px`;
    div.style.height = `${height * z}px`;
    div.style.lineHeight = `${height * z}px`;
    div.style.fontSize = `${item.size * z}px`;
    div.style.fontFamily = fontStack(item.font);
    div.style.fontWeight = item.bold ? '700' : '400';
    div.style.fontStyle = item.italic ? 'italic' : 'normal';
    div.style.setProperty('--tf-color', item.color);

    div.addEventListener('click', (e) => { e.stopPropagation(); startEdit(div, node); });
    layer.appendChild(div);
  });
}

function fontStack(alias) {
  if (alias.startsWith('co')) return 'ui-monospace, "SF Mono", Menlo, Consolas, monospace';
  if (alias.startsWith('ti')) return 'Times, "Times New Roman", Georgia, serif';
  return 'Helvetica, Arial, sans-serif';
}

/* ------------------------------------------------------- édition d'un fragment */

function startEdit(div, node) {
  if (state.adding || state.editing === div) return;
  commitEdit();
  state.editing = div;
  div.classList.add('editing');
  div.style.color = div.style.getPropertyValue('--tf-color') || '#000';
  div.style.setProperty('--tf-bg', sampleBackground(node, div));
  div.contentEditable = 'true';
  div.spellcheck = true;
  div.focus();
  selectAll(div);
  openStyleBar(div, node);

  div.addEventListener('keydown', onEditKey);
  div.addEventListener('paste', onPaste);
  div.addEventListener('blur', () => commitEdit(), { once: true });
}

/* ------------------------------------------------------- choix de la police */

const CSS_FAMILY = {
  arial: 'Arial, Helvetica, sans-serif',
  times: '"Times New Roman", Times, serif',
};

/** Petite barre flottante au-dessus du fragment : police, gras, italique. */
function openStyleBar(div, node) {
  closeStyleBar();
  const bar = document.createElement('div');
  bar.className = 'stylebar';

  const select = document.createElement('select');
  select.innerHTML = '<option value="">Police du document</option>';
  state.fonts.forEach((f) => {
    const option = document.createElement('option');
    option.value = f.key;
    option.textContent = f.available ? f.label : `${f.label} (substituée)`;
    select.appendChild(option);
  });

  const bold = document.createElement('button');
  bold.innerHTML = '<b>G</b>';
  bold.title = 'Gras';
  bold.classList.toggle('active', div.dataset.bold === '1');

  const italic = document.createElement('button');
  italic.innerHTML = '<i>I</i>';
  italic.title = 'Italique';
  italic.classList.toggle('active', div.dataset.italic === '1');

  bar.append(select, bold, italic);
  // Sans cela, cliquer dans la barre ferait perdre le focus au texte, donc
  // validerait la saisie avant même que le choix soit pris en compte.
  bar.addEventListener('mousedown', (e) => e.preventDefault());

  const apply = () => {
    state.style.family = select.value || null;
    state.style.bold = bold.classList.contains('active');
    state.style.italic = italic.classList.contains('active');
    state.style.touched = true;
    div.style.fontFamily = CSS_FAMILY[select.value] || fontStack(div.dataset.alias || 'helv');
    div.style.fontWeight = state.style.bold ? '700' : '400';
    div.style.fontStyle = state.style.italic ? 'italic' : 'normal';
  };
  select.addEventListener('change', apply);
  bold.addEventListener('click', () => { bold.classList.toggle('active'); apply(); });
  italic.addEventListener('click', () => { italic.classList.toggle('active'); apply(); });

  node.appendChild(bar);
  const top = div.offsetTop - bar.offsetHeight - 6;
  bar.style.left = `${Math.max(2, Math.min(div.offsetLeft, node.clientWidth - bar.offsetWidth - 2))}px`;
  bar.style.top = `${top < 2 ? div.offsetTop + div.offsetHeight + 6 : top}px`;

  state.style = {
    bar,
    family: null,
    bold: div.dataset.bold === '1',
    italic: div.dataset.italic === '1',
    touched: false,
  };
}

function closeStyleBar() {
  if (state.style) {
    state.style.bar.remove();
    state.style = null;
  }
}

function selectAll(div) {
  const range = document.createRange();
  range.selectNodeContents(div);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

function onEditKey(e) {
  if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); }
  else if (e.key === 'Escape') {
    e.preventDefault();
    e.target.textContent = e.target.dataset.original;
    e.target.blur();
  }
}

function onPaste(e) {
  e.preventDefault();
  const text = (e.clipboardData || window.clipboardData).getData('text').replace(/\s+/g, ' ');
  document.execCommand('insertText', false, text);
}

/** Devine la couleur de fond derrière un fragment pour masquer l'ancien texte. */
function sampleBackground(node, div) {
  const img = node.querySelector('img');
  if (!img.complete || !img.naturalWidth) return '#fff';
  try {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    const ratio = img.naturalWidth / node.clientWidth;
    const x = Math.max(0, Math.min(img.naturalWidth - 1, (div.offsetLeft - 3) * ratio));
    const y = Math.max(0, Math.min(img.naturalHeight - 1, (div.offsetTop - 2) * ratio));
    ctx.drawImage(img, x, y, 1, 1, 0, 0, 1, 1);
    const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
    return `rgb(${r}, ${g}, ${b})`;
  } catch (_) {
    return '#fff';   // canvas « tainted » : on retombe sur du blanc
  }
}

async function commitEdit() {
  const div = state.editing;
  if (!div) return;
  state.editing = null;
  div.contentEditable = 'false';
  div.classList.remove('editing');
  div.style.color = '';
  div.removeEventListener('keydown', onEditKey);
  div.removeEventListener('paste', onPaste);

  const chosen = state.style && state.style.touched ? state.style : null;
  const style = chosen
    ? { family: chosen.family, bold: chosen.bold, italic: chosen.italic }
    : null;
  closeStyleBar();

  const text = div.textContent.replace(/\s+/g, ' ').trim();
  const original = div.dataset.original;
  if (text === original.trim() && !style) { div.textContent = original; return; }

  // La couche a pu être reconstruite pendant la saisie : on retrouve la page par
  // son numéro plutôt que par le parent du fragment, qui peut être détaché.
  const node = pageNode(+div.dataset.id.split('-')[0]);
  busy(true, 'Application de la modification…');
  try {
    const data = await postJSON(`/api/${state.docId}/edit`, {
      edits: [{ id: div.dataset.id, text, original, style }],
    });
    applyState(data);
    if (data.changed) {
      if (node) await refreshPage(node);
      if (!text) toast('Texte supprimé');
      else if (data.approximated) toast('Texte modifié — police substituée');
      else toast('Texte modifié');
    } else {
      div.textContent = original;
      toast('Modification non appliquée : le texte n’a pas été retrouvé.', true);
    }
  } catch (err) {
    div.textContent = original;
    toast(err.message, true);
  } finally {
    busy(false);
  }
}

/* ------------------------------------------------------- rafraîchissement */

async function refreshPage(node) {
  node.dataset.loaded = '';
  await loadPage(node);
}

/** Recharge toutes les pages déjà affichées (après annulation, remplacement, zoom). */
function refreshAll() {
  el.viewer.querySelectorAll('.page').forEach((node) => {
    const wasLoaded = node.dataset.loaded !== '';
    node.dataset.loaded = '';
    if (wasLoaded) loadPage(node);
  });
}

/* ------------------------------------------------------------ ajout de texte */

$('#btn-add').addEventListener('click', () => {
  state.adding = !state.adding;
  $('#btn-add').classList.toggle('active', state.adding);
  el.viewer.querySelectorAll('.page').forEach((n) => n.classList.toggle('adding', state.adding));
  if (state.adding) toast('Cliquez à l’endroit où placer le texte');
});

function onPageMouseDown(e) {
  if (!state.adding || e.button !== 0) return;
  const node = e.currentTarget;
  const rect = node.getBoundingClientRect();
  openNewBox(node, (e.clientX - rect.left) / state.zoom, (e.clientY - rect.top) / state.zoom);
}

function openNewBox(node, x, y) {
  node.querySelectorAll('.newbox').forEach((n) => n.remove());
  const box = document.createElement('div');
  box.className = 'newbox';
  box.style.left = `${Math.min(x * state.zoom, node.clientWidth - 250)}px`;
  box.style.top = `${y * state.zoom}px`;
  box.innerHTML = `
    <textarea placeholder="Votre texte…"></textarea>
    <div class="row">
      <input type="number" class="size" value="12" min="4" max="96" step="1" title="Taille">
      <input type="color" class="color" value="#000000" title="Couleur">
      <button class="bold" title="Gras"><b>G</b></button>
      <button class="italic" title="Italique"><i>I</i></button>
      <span class="grow"></span>
      <button class="cancel">Annuler</button>
      <button class="ok primary">Ajouter</button>
    </div>`;
  node.appendChild(box);
  const textarea = box.querySelector('textarea');
  textarea.focus();

  box.querySelector('.bold').addEventListener('click', (ev) => ev.currentTarget.classList.toggle('active'));
  box.querySelector('.italic').addEventListener('click', (ev) => ev.currentTarget.classList.toggle('active'));
  box.querySelector('.cancel').addEventListener('click', () => box.remove());
  box.addEventListener('mousedown', (ev) => ev.stopPropagation());
  textarea.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') box.remove();
    if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey)) box.querySelector('.ok').click();
  });

  box.querySelector('.ok').addEventListener('click', async () => {
    const text = textarea.value;
    if (!text.trim()) return box.remove();
    busy(true, 'Ajout du texte…');
    try {
      const data = await postJSON(`/api/${state.docId}/textbox`, {
        page: +node.dataset.page,
        x, y, text,
        size: +box.querySelector('.size').value || 12,
        color: box.querySelector('.color').value,
        bold: box.querySelector('.bold').classList.contains('active'),
        italic: box.querySelector('.italic').classList.contains('active'),
      });
      applyState(data);
      box.remove();
      await refreshPage(node);
      toast('Texte ajouté');
    } catch (err) {
      toast(err.message, true);
    } finally {
      busy(false);
    }
  });
}

/* ------------------------------------------------------ recherche / remplace */

$('#btn-find').addEventListener('click', () => toggleFind());

function toggleFind(force) {
  const show = force !== undefined ? force : el.findPanel.hidden;
  el.findPanel.hidden = !show;
  $('#btn-find').classList.toggle('active', show);
  if (show) el.findSearch.focus();
}

$('#btn-count').addEventListener('click', async () => {
  const q = el.findSearch.value;
  if (!q) return;
  busy(true, 'Recherche…');
  try {
    const data = await api(`/api/${state.docId}/search?q=${encodeURIComponent(q)}&case_sensitive=${el.findCase.checked}`);
    el.findInfo.textContent = `${data.count} occurrence(s)`;
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false);
  }
});

$('#btn-replace-all').addEventListener('click', async () => {
  const search = el.findSearch.value;
  if (!search) return toast('Saisissez le texte à rechercher.', true);
  busy(true, 'Remplacement en cours…');
  try {
    const data = await postJSON(`/api/${state.docId}/replace`, {
      search,
      replace: el.findReplace.value,
      case_sensitive: el.findCase.checked,
    });
    applyState(data);
    refreshAll();
    const approx = data.approximated ? `, dont ${data.approximated} en police approchée` : '';
    el.findInfo.textContent = `${data.changed} fragment(s) modifié(s)${approx}`;
    toast(data.changed ? `${data.changed} fragment(s) modifié(s)${approx}` : 'Aucune occurrence trouvée');
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false);
  }
});

/* --------------------------------------------------- annuler / zoom / export */

el.undo.addEventListener('click', () => history('undo'));
el.redo.addEventListener('click', () => history('redo'));

async function history(action) {
  if (!state.docId) return;
  busy(true, action === 'undo' ? 'Annulation…' : 'Rétablissement…');
  try {
    const data = await postJSON(`/api/${state.docId}/${action}`, {});
    applyState(data);
    refreshAll();
    if (!data.ok) toast(action === 'undo' ? 'Rien à annuler' : 'Rien à rétablir');
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false);
  }
}

el.compress.addEventListener('click', compressDoc);

async function compressDoc() {
  if (!state.docId) return;
  busy(true, 'Pressage du PDF…', 'squeeze');
  try {
    const data = await postJSON(`/api/${state.docId}/compress`, {});
    applyState(data);
    refreshAll();
    if (data.after < data.before) {
      const pct = Math.round((1 - data.after / data.before) * 100);
      toast(`PDF allégé de ${pct} % (${formatSize(data.before)} → ${formatSize(data.after)})`);
    } else {
      toast('Déjà bien pressé : aucun gain possible');
    }
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false);
  }
}

function formatSize(bytes) {
  return bytes >= 1024 * 1024
    ? `${(bytes / (1024 * 1024)).toFixed(1)} Mo`
    : `${Math.round(bytes / 1024)} Ko`;
}

function setZoom(delta) {
  const index = ZOOM_STEPS.indexOf(state.zoom);
  const next = ZOOM_STEPS[Math.min(ZOOM_STEPS.length - 1, Math.max(0, index + delta))];
  if (next === state.zoom) return;
  state.zoom = next;
  el.zoomLabel.textContent = `${Math.round(next * 100)} %`;
  sizePages();
}

$('#btn-zoom-in').addEventListener('click', () => setZoom(1));
$('#btn-zoom-out').addEventListener('click', () => setZoom(-1));

$('#btn-download').addEventListener('click', () => {
  if (state.docId) window.location.href = `/api/${state.docId}/download`;
});

$('#btn-close').addEventListener('click', async () => {
  if (!confirm('Fermer le document ? Les modifications non téléchargées seront perdues.')) return;
  const id = state.docId;
  state.docId = null;
  el.viewer.innerHTML = '';
  el.viewer.hidden = true;
  el.dropzone.hidden = false;
  el.docTools.hidden = true;
  el.docActions.hidden = true;
  toggleFind(false);
  try { await fetch(`/api/${id}`, { method: 'DELETE' }); } catch (_) { /* sans importance */ }
});

/* -------------------------------------------------------------- raccourcis */

document.addEventListener('keydown', (e) => {
  const mod = e.metaKey || e.ctrlKey;
  if (!mod || !state.docId) return;
  if (e.key === 'z' && !e.shiftKey) { e.preventDefault(); history('undo'); }
  else if ((e.key === 'z' && e.shiftKey) || e.key === 'y') { e.preventDefault(); history('redo'); }
  else if (e.key === 'f') { e.preventDefault(); toggleFind(true); }
  else if (e.key === 's') { e.preventDefault(); $('#btn-download').click(); }
});

api('/api/fonts')
  .then((data) => { state.fonts = data.fonts; })
  .catch(() => { state.fonts = []; });   // le sélecteur se limitera à « police du document »

window.addEventListener('beforeunload', (e) => {
  if (state.docId && state.version > 0) { e.preventDefault(); e.returnValue = ''; }
});
