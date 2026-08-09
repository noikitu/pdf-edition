/* Éditeur PDF — logique de l'interface.
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
};

/* ------------------------------------------------------------------ utils */

let busyCount = 0;
function busy(on, text) {
  busyCount = Math.max(0, busyCount + (on ? 1 : -1));
  el.overlayText.textContent = text || 'Traitement…';
  el.overlay.hidden = busyCount === 0;
}

let toastTimer;
function toast(message, isError) {
  el.toast.textContent = message;
  el.toast.classList.toggle('error', !!isError);
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.toast.hidden = true; }, isError ? 5000 : 2600);
}

async function api(path, options) {
  const res = await fetch(path, options);
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

  div.addEventListener('keydown', onEditKey);
  div.addEventListener('paste', onPaste);
  div.addEventListener('blur', () => commitEdit(), { once: true });
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

  const text = div.textContent.replace(/\s+/g, ' ').trim();
  const original = div.dataset.original;
  if (text === original.trim()) { div.textContent = original; return; }

  const node = div.closest('.page');
  busy(true, 'Application de la modification…');
  try {
    const data = await postJSON(`/api/${state.docId}/edit`, { edits: { [div.dataset.id]: text } });
    applyState(data);
    if (data.changed) {
      await refreshPage(node);
      toast(text ? 'Texte modifié' : 'Texte supprimé');
    } else {
      div.textContent = original;
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
    el.findInfo.textContent = `${data.changed} fragment(s) modifié(s)`;
    toast(data.changed ? `${data.changed} fragment(s) modifié(s)` : 'Aucune occurrence trouvée');
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

window.addEventListener('beforeunload', (e) => {
  if (state.docId && state.version > 0) { e.preventDefault(); e.returnValue = ''; }
});
