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
  highlighting: false,
  redacting: false,
  placing: false,      // une image attend d'être posée sur une page
  pendingImage: null,  // { file, url, ratio, width } de l'image à insérer
  editing: null,   // élément .tf en cours d'édition
  fonts: [],       // polices proposées par le serveur
  style: null,     // barre de style ouverte sur le fragment en cours
  hits: [],        // occurrences de la dernière recherche
  hitIndex: -1,
  hitQuery: null,
  lens: false,     // loupe active
  lensZoom: 2.5,   // facteur de grossissement, réglable à la molette
  scanPages: [],   // pages sans texte extractible
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
  highlight: $('#btn-highlight'),
  redact: $('#btn-redact'),
  imageInput: $('#image-input'),
  mergeInput: $('#merge-input'),
  pagesPanel: $('#pages-panel'),
  extractSpec: $('#extract-spec'),
  pagesInfo: $('#pages-info'),
  notice: $('#notice'),
  noticeText: $('#notice-text'),
  thumbsBox: $('#thumbs-box'),
  thumbs: $('#thumbs'),
  formPanel: $('#form-panel'),
  formFields: $('#form-fields'),
  formInfo: $('#form-info'),
  formApply: $('#btn-form-apply'),
  signModal: $('#sign-modal'),
  signCanvas: $('#sign-canvas'),
  menu: $('#btn-menu'),
  sidebar: $('#sidebar'),
  scrim: $('#scrim'),
  sideActions: $('#sidebar-actions'),
  dock: $('#viewdock'),
  lens: $('#lens'),
  lensBtn: $('#btn-lens'),
  prevHit: $('#btn-prev-hit'),
  nextHit: $('#btn-next-hit'),
  findPosition: $('#find-position'),
};

/* ------------------------------------------------------------------ utils */

/* L'écran d'attente n'apparaît qu'au bout de BUSY_DELAY : une opération plus
   rapide se termine sans rien afficher, ce qui évite le clignotement. Et s'il est
   apparu, il reste au moins BUSY_MIN à l'écran — sans quoi une opération de 250 ms
   produirait exactement le clignotement qu'on cherche à supprimer. */
const BUSY_DELAY = 220;
const BUSY_MIN = 450;
const BUSY_FADE = 200;   // doit correspondre à la transition CSS de .overlay

let busyCount = 0;
let showTimer = null;
let hideTimer = null;
let shownAt = 0;

/** `mode: 'squeeze'` bascule l'overlay sur l'illustration du citron pressé. */
function busy(on, text, mode) {
  busyCount = Math.max(0, busyCount + (on ? 1 : -1));

  if (on) {
    el.overlayText.textContent = text || 'Traitement…';
    el.overlay.classList.toggle('squeezing', mode === 'squeeze');
    if (busyCount === 1) {
      clearTimeout(hideTimer);
      clearTimeout(showTimer);
      showTimer = setTimeout(showOverlay, BUSY_DELAY);
    }
    return;
  }

  if (busyCount > 0) return;          // une autre opération est encore en cours
  clearTimeout(showTimer);
  if (!shownAt) return;               // jamais affiché : rien à retirer
  clearTimeout(hideTimer);
  hideTimer = setTimeout(hideOverlay, Math.max(0, BUSY_MIN - (Date.now() - shownAt)));
}

function showOverlay() {
  shownAt = Date.now();
  el.overlay.hidden = false;
  // Sans lecture forcée de la mise en page, le navigateur applique `hidden` et
  // la classe dans la même passe : l'opacité sauterait à 1 sans transition.
  void el.overlay.offsetHeight;
  el.overlay.classList.add('on');
}

function hideOverlay() {
  shownAt = 0;
  el.overlay.classList.remove('on');
  hideTimer = setTimeout(() => {
    if (!busyCount) el.overlay.hidden = true;
  }, BUSY_FADE);
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
const STALE_SERVER_ERROR =
  'Le serveur tourne dans une version plus ancienne que cette page : arrêtez-le (Ctrl+C) et relancez-le.';

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
    // 405 sur une route de l'API : elle n'existe pas dans le serveur qui tourne,
    // le POST est tombé sur le service des fichiers statiques. Autrement dit le
    // serveur a été lancé avant cette version du code.
    if (res.status === 405) throw new Error(STALE_SERVER_ERROR);
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

/* -------------------------------------------------------------- menu latéral */

function setMenu(open) {
  el.sidebar.classList.toggle('open', open);
  el.scrim.classList.toggle('open', open);
  el.menu.setAttribute('aria-expanded', open ? 'true' : 'false');
}

const menuOpen = () => el.sidebar.classList.contains('open');

el.menu.addEventListener('click', () => setMenu(!menuOpen()));
el.scrim.addEventListener('click', () => setMenu(false));
$('#btn-menu-close').addEventListener('click', () => setMenu(false));

// Une action choisie dans le menu le referme : les panneaux qu'elle ouvre se
// trouvent sous la barre d'outils, que le menu recouvrirait.
el.sideActions.addEventListener('click', (e) => {
  if (e.target.closest('button, label')) setMenu(false);
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

/* --------------------------------------------------- reprise d'un document */

/** Documents sauvegardés sur cette machine, proposés à l'ouverture. */
async function loadResumable() {
  try {
    const data = await api('/api/sessions');
    const list = data.sessions || [];
    const box = $('#resume');
    const items = $('#resume-list');
    items.innerHTML = '';
    if (!list.length) { box.hidden = true; return; }

    list.forEach((entry) => {
      const row = document.createElement('div');
      row.className = 'resume-row';
      const open = document.createElement('button');
      open.className = 'resume-open';
      open.innerHTML = '<b></b><span></span>';
      open.querySelector('b').textContent = entry.name;
      open.querySelector('span').textContent =
        `${formatSize(entry.size)} — ${formatWhen(entry.saved_at)}`;
      open.addEventListener('click', () => resumeDoc(entry.doc_id));

      const forget = document.createElement('button');
      forget.className = 'icon';
      forget.textContent = '✕';
      forget.title = 'Oublier ce document';
      forget.addEventListener('click', async () => {
        try { await api(`/api/${entry.doc_id}`, { method: 'DELETE' }); } catch (_) { /* déjà parti */ }
        loadResumable();
      });

      row.append(open, forget);
      items.appendChild(row);
    });
    box.hidden = false;
  } catch (_) { /* serveur sans sauvegarde : l'accueil reste tel quel */ }
}

function formatWhen(seconds) {
  const minutes = Math.max(0, Math.round((Date.now() - seconds * 1000) / 60000));
  if (minutes < 1) return 'à l’instant';
  if (minutes < 60) return `il y a ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `il y a ${hours} h`;
  return new Date(seconds * 1000).toLocaleDateString('fr-FR');
}

async function resumeDoc(docId) {
  busy(true, 'Reprise du document…');
  try {
    const data = await api(`/api/${docId}/state`);
    state.docId = data.doc_id;
    state.name = data.name;
    adoptDocument(data);
    toast('Document repris — l’historique d’annulation repart de zéro');
  } catch (err) {
    toast(err.message, true);
    loadResumable();
  } finally {
    busy(false);
  }
}

/** Bascule l'interface en mode « document ouvert ». */
function adoptDocument(data) {
  applyState(data);
  el.dropzone.hidden = true;
  el.viewer.hidden = false;
  el.docTools.hidden = false;
  el.docActions.hidden = false;
  el.sideActions.hidden = false;
  el.dock.hidden = false;
  el.filename.textContent = data.name;
  buildPages();
  buildThumbs();
  checkScan();
}

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
    adoptDocument(data);
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

    node.appendChild(buildPageTools(page.number));

    node.addEventListener('mousedown', onPageMouseDown);
    el.viewer.appendChild(node);
    observer.observe(node);
  });
  // Les premières pages sont chargées sans attendre l'observateur.
  [...el.viewer.children].slice(0, 3).forEach(loadPage);
}

/** Barre d'actions affichée au survol d'une page. */
function buildPageTools(pno) {
  const tools = document.createElement('div');
  tools.className = 'page-tools';

  const actions = [
    ['↺', 'Pivoter à gauche', () => rotatePage(pno, -90)],
    ['↻', 'Pivoter à droite', () => rotatePage(pno, 90)],
    ['↑', 'Monter la page', () => movePage(pno, -1)],
    ['↓', 'Descendre la page', () => movePage(pno, 1)],
    ['🖍✕', 'Retirer les surlignages', () => clearHighlights(pno)],
    ['🗑', 'Supprimer la page', () => deletePage(pno)],
  ];

  actions.forEach(([label, title, handler], index) => {
    const button = document.createElement('button');
    button.textContent = label;
    button.title = title;
    if (index === actions.length - 1) button.classList.add('danger');
    if (label === '↑') button.disabled = pno === 0;
    if (label === '↓') button.disabled = pno === state.pages.length - 1;
    button.addEventListener('click', (e) => { e.stopPropagation(); handler(); });
    tools.appendChild(button);
  });

  // Un mousedown sur la barre ne doit pas être pris pour un clic « ajouter du texte ».
  tools.addEventListener('mousedown', (e) => e.stopPropagation());
  return tools;
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

    div.addEventListener('click', (e) => {
      e.stopPropagation();
      if (state.highlighting) highlightFragment(div);
      else startEdit(div, node);
    });
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
      else if (data.reflowed) toast('Texte modifié — paragraphe recomposé');
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
  setMode(state.adding ? null : 'adding');
});

/* --------------------------------------------------------------- surlignage */

el.highlight.addEventListener('click', () => {
  setMode(state.highlighting ? null : 'highlighting');
});

/** Un seul mode de clic à la fois : ajouter, surligner, caviarder, poser une image. */
function setMode(mode) {
  state.adding = mode === 'adding';
  state.highlighting = mode === 'highlighting';
  state.redacting = mode === 'redacting';
  state.placing = mode === 'placing';
  if (!state.placing) clearPendingImage();
  if (!state.redacting) el.viewer.querySelectorAll('.selbox').forEach((n) => n.remove());

  $('#btn-add').classList.toggle('active', state.adding);
  el.highlight.classList.toggle('active', state.highlighting);
  el.redact.classList.toggle('active', state.redacting);
  $('#btn-image').classList.toggle('active', state.placing);
  el.viewer.querySelectorAll('.page').forEach((n) => {
    n.classList.toggle('adding', state.adding);
    n.classList.toggle('highlighting', state.highlighting);
    n.classList.toggle('redacting', state.redacting);
    n.classList.toggle('placing', state.placing);
  });
  if (state.adding) toast('Cliquez à l’endroit où placer le texte');
  if (state.highlighting) toast('Cliquez un texte pour le surligner');
  if (state.redacting) toast('Tracez un rectangle sur la zone à masquer');
  if (state.placing) toast('Cliquez à l’endroit où poser l’image');
}

async function highlightFragment(div) {
  const node = pageNode(+div.dataset.id.split('-')[0]);
  busy(true, 'Surlignage…');
  try {
    const data = await postJSON(`/api/${state.docId}/highlight`, {
      id: div.dataset.id,
      original: div.dataset.original,
    });
    applyState(data);
    if (node) await refreshPage(node);
    toast('Texte surligné');
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false);
  }
}

async function clearHighlights(pno) {
  busy(true, 'Retrait des surlignages…');
  try {
    const data = await api(`/api/${state.docId}/page/${pno}/highlights`, { method: 'DELETE' });
    applyState(data);
    const node = pageNode(pno);
    if (node) await refreshPage(node);
    toast(data.removed ? `${data.removed} surlignage(s) retiré(s)` : 'Aucun surlignage sur cette page');
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false);
  }
}

/* ------------------------------------------------------- pages : opérations */

/** Les opérations de page changent la structure du document : on rebâtit la vue. */
async function pageOperation(label, request) {
  busy(true, label);
  try {
    const data = await request();
    applyState(data);
    resetSearch();
    buildPages();
    buildThumbs();
    checkScan();
    return data;
  } catch (err) {
    toast(err.message, true);
    return null;
  } finally {
    busy(false);
  }
}

async function rotatePage(pno, delta) {
  const data = await pageOperation('Rotation…', () =>
    postJSON(`/api/${state.docId}/page/${pno}/rotate`, { delta }));
  if (data) toast('Page pivotée');
}

async function movePage(pno, offset) {
  const data = await pageOperation('Déplacement…', () =>
    postJSON(`/api/${state.docId}/page/${pno}/move`, { offset }));
  if (data) toast(`Page déplacée en position ${data.page + 1}`);
}

async function deletePage(pno) {
  if (!confirm(`Supprimer la page ${pno + 1} ?`)) return;
  const data = await pageOperation('Suppression…', () =>
    api(`/api/${state.docId}/page/${pno}`, { method: 'DELETE' }));
  if (data) toast('Page supprimée');
}

/** Une recherche devient obsolète dès que la pagination change. */
function resetSearch() {
  state.hits = [];
  state.hitIndex = -1;
  state.hitQuery = null;
  updateHitNav();
}

/* ------------------------------------------------------------------ fusion */

el.mergeInput.addEventListener('change', async () => {
  const file = el.mergeInput.files[0];
  el.mergeInput.value = '';
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  const data = await pageOperation('Fusion…', () =>
    api(`/api/${state.docId}/merge`, { method: 'POST', body: form }));
  if (data) toast(`${data.added} page(s) ajoutée(s)`);
});

/* --------------------------------------------------------- extraction pages */

$('#btn-pages').addEventListener('click', () => togglePages());

$('#btn-extract').addEventListener('click', () => {
  const spec = el.extractSpec.value.trim();
  if (!spec) return toast('Indiquez les pages à extraire, par exemple 1-3, 5.', true);
  el.pagesInfo.textContent = '';
  window.location.href = `/api/${state.docId}/extract?pages=${encodeURIComponent(spec)}`;
});

el.extractSpec.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); $('#btn-extract').click(); }
});

function onPageMouseDown(e) {
  if (e.button !== 0) return;
  const node = e.currentTarget;
  const rect = node.getBoundingClientRect();
  const x = (e.clientX - rect.left) / state.zoom;
  const y = (e.clientY - rect.top) / state.zoom;
  if (state.adding) openNewBox(node, x, y);
  else if (state.placing) openImageBox(node, x, y);
  else if (state.redacting) startSelection(node, e, x, y);
}

/** Coordonnées PDF d'un événement souris sur une page. */
function pagePoint(node, e) {
  const rect = node.getBoundingClientRect();
  return {
    x: (e.clientX - rect.left) / state.zoom,
    y: (e.clientY - rect.top) / state.zoom,
  };
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

/* --------------------------------------------------- image et signature */

// Largeur par défaut du cadre posé sur la page, en points PDF (~6 cm).
const IMAGE_DEFAULT_WIDTH = 170;

el.imageInput.addEventListener('change', () => {
  const file = el.imageInput.files[0];
  el.imageInput.value = '';
  if (!file) return;
  if (!/^image\//.test(file.type)) return toast('Ce fichier n’est pas une image.', true);

  // On lit les dimensions dans le navigateur pour proposer un cadre aux bonnes
  // proportions avant même d'envoyer quoi que ce soit au serveur.
  const url = URL.createObjectURL(file);
  const probe = new Image();
  probe.onload = () => {
    clearPendingImage();
    const ratio = probe.naturalWidth / probe.naturalHeight || 1;
    state.pendingImage = { file, url, ratio, width: IMAGE_DEFAULT_WIDTH };
    setMode('placing');
  };
  probe.onerror = () => {
    URL.revokeObjectURL(url);
    toast('Image illisible.', true);
  };
  probe.src = url;
});

function clearPendingImage() {
  el.viewer.querySelectorAll('.imgbox').forEach((n) => n.remove());
  if (state.pendingImage) {
    URL.revokeObjectURL(state.pendingImage.url);
    state.pendingImage = null;
  }
}

/** Cadre d'aperçu déplaçable et redimensionnable, avant insertion définitive. */
function openImageBox(node, x, y) {
  const pending = state.pendingImage;
  if (!pending) return;
  node.querySelectorAll('.imgbox').forEach((n) => n.remove());

  const geo = { x, y, w: pending.width, h: pending.width / pending.ratio };
  const box = document.createElement('div');
  box.className = 'imgbox';
  box.innerHTML = `
    <img alt="Aperçu" draggable="false">
    <span class="grip" title="Redimensionner"></span>
    <div class="row">
      <button class="cancel">Annuler</button>
      <button class="ok primary">Insérer</button>
    </div>`;
  box.querySelector('img').src = pending.url;
  node.appendChild(box);

  const place = () => {
    box.style.left = `${geo.x * state.zoom}px`;
    box.style.top = `${geo.y * state.zoom}px`;
    box.style.width = `${geo.w * state.zoom}px`;
    box.style.height = `${geo.h * state.zoom}px`;
  };
  place();

  // Glisser l'aperçu le déplace ; glisser la poignée le redimensionne, à
  // proportions constantes pour ne pas déformer une signature.
  const drag = (e, onMove) => {
    e.preventDefault();
    e.stopPropagation();
    const start = pagePoint(node, e);
    const from = { ...geo };
    const move = (ev) => {
      const p = pagePoint(node, ev);
      onMove(from, p.x - start.x, p.y - start.y);
      place();
    };
    const up = () => {
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  };

  box.querySelector('img').addEventListener('mousedown', (e) => drag(e, (from, dx, dy) => {
    geo.x = Math.max(0, from.x + dx);
    geo.y = Math.max(0, from.y + dy);
  }));
  box.querySelector('.grip').addEventListener('mousedown', (e) => drag(e, (from, dx) => {
    geo.w = Math.max(16, from.w + dx);
    geo.h = geo.w / pending.ratio;
  }));
  box.addEventListener('mousedown', (e) => e.stopPropagation());
  box.querySelector('.cancel').addEventListener('click', () => setMode(null));

  box.querySelector('.ok').addEventListener('click', async () => {
    const form = new FormData();
    form.append('file', pending.file, pending.file.name || 'image.png');
    form.append('page', String(+node.dataset.page));
    form.append('x', String(geo.x));
    form.append('y', String(geo.y));
    form.append('width', String(geo.w));
    form.append('height', String(geo.h));
    busy(true, 'Insertion de l’image…');
    try {
      const data = await api(`/api/${state.docId}/image`, { method: 'POST', body: form });
      applyState(data);
      setMode(null);
      await refreshPage(node);
      toast('Image insérée');
    } catch (err) {
      toast(err.message, true);
    } finally {
      busy(false);
    }
  });
}

/* -------------------------------------------------------------- caviardage */

el.redact.addEventListener('click', () => {
  setMode(state.redacting ? null : 'redacting');
});

/** Tracé du rectangle à masquer, puis choix entre effacer et noircir. */
function startSelection(node, e, x0, y0) {
  e.preventDefault();
  node.querySelectorAll('.selbox').forEach((n) => n.remove());
  const area = { x0, y0, x1: x0, y1: y0 };
  const box = document.createElement('div');
  box.className = 'selbox';
  node.appendChild(box);

  const place = () => {
    const z = state.zoom;
    box.style.left = `${Math.min(area.x0, area.x1) * z}px`;
    box.style.top = `${Math.min(area.y0, area.y1) * z}px`;
    box.style.width = `${Math.abs(area.x1 - area.x0) * z}px`;
    box.style.height = `${Math.abs(area.y1 - area.y0) * z}px`;
  };
  place();

  const move = (ev) => {
    const p = pagePoint(node, ev);
    area.x1 = p.x;
    area.y1 = p.y;
    place();
  };
  const up = () => {
    window.removeEventListener('mousemove', move);
    window.removeEventListener('mouseup', up);
    if (Math.abs(area.x1 - area.x0) < 5 || Math.abs(area.y1 - area.y0) < 5) {
      box.remove();   // simple clic : rien à masquer
      return;
    }
    addSelectionTools(node, box, area);
  };
  window.addEventListener('mousemove', move);
  window.addEventListener('mouseup', up);
}

function addSelectionTools(node, box, area) {
  const tools = document.createElement('div');
  tools.className = 'selbox-tools';
  tools.innerHTML = `
    <button class="erase">Effacer</button>
    <button class="black">Noircir</button>
    <button class="cancel">✕</button>`;
  tools.addEventListener('mousedown', (e) => e.stopPropagation());
  tools.querySelector('.cancel').addEventListener('click', () => box.remove());
  tools.querySelector('.erase').addEventListener('click', () => applyRedaction(node, box, area, false));
  tools.querySelector('.black').addEventListener('click', () => applyRedaction(node, box, area, true));
  box.appendChild(tools);
}

async function applyRedaction(node, box, area, blackout) {
  const pno = +node.dataset.page;
  busy(true, blackout ? 'Noircissage…' : 'Effacement…');
  try {
    const data = await postJSON(`/api/${state.docId}/page/${pno}/redact`, {
      x0: area.x0, y0: area.y0, x1: area.x1, y1: area.y1, blackout,
    });
    applyState(data);
    box.remove();
    resetSearch();
    await refreshPage(node);
    toast(blackout ? 'Zone noircie' : 'Zone effacée');
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false);
  }
}

/* ------------------------------------------------------ recherche / remplace */

$('#btn-find').addEventListener('click', () => toggleFind());

function toggleFind(force) {
  const show = force !== undefined ? force : el.findPanel.hidden;
  el.findPanel.hidden = !show;
  $('#btn-find').classList.toggle('active', show);
  // Les panneaux sont collés au même endroit sous la barre : un seul à la fois.
  if (show) { togglePages(false); toggleAssist(false); toggleForm(false); }
  if (show) el.findSearch.focus();
}

function togglePages(force) {
  const show = force !== undefined ? force : el.pagesPanel.hidden;
  el.pagesPanel.hidden = !show;
  $('#btn-pages').classList.toggle('active', show);
  if (show) { toggleFind(false); toggleAssist(false); toggleForm(false); }
  if (show) el.extractSpec.focus();
}

$('#btn-count').addEventListener('click', runSearch);
el.prevHit.addEventListener('click', () => gotoHit(-1));
el.nextHit.addEventListener('click', () => gotoHit(1));

el.findSearch.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  // Entrée relance la recherche si le terme a changé, sinon avance d'une occurrence.
  if (state.hits.length && el.findSearch.value === state.hitQuery) gotoHit(e.shiftKey ? -1 : 1);
  else runSearch();
});

async function runSearch() {
  const q = el.findSearch.value;
  if (!q) return;
  busy(true, 'Recherche…');
  try {
    const data = await api(
      `/api/${state.docId}/search?q=${encodeURIComponent(q)}&case_sensitive=${el.findCase.checked}`
    );
    state.hits = data.hits || [];
    state.hitQuery = q;
    state.hitIndex = -1;
    el.findInfo.textContent = `${data.count} occurrence(s)`;
    updateHitNav();
    if (state.hits.length) gotoHit(1);
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false);
  }
}

function updateHitNav() {
  const total = state.hits.length;
  el.prevHit.disabled = total === 0;
  el.nextHit.disabled = total === 0;
  el.findPosition.textContent = total ? `${state.hitIndex + 1} / ${total}` : '—';
}

/** Fait défiler jusqu'à l'occurrence suivante (ou précédente) et la fait clignoter. */
async function gotoHit(step) {
  const total = state.hits.length;
  if (!total) return;
  state.hitIndex = (state.hitIndex + step + total) % total;
  updateHitNav();

  const hit = state.hits[state.hitIndex];
  const node = pageNode(hit.page);
  if (!node) return;
  await loadPage(node);

  const [x0, y0, x1, y1] = hit.bbox;
  const z = state.zoom;
  node.querySelectorAll('.find-hit').forEach((n) => n.remove());
  const mark = document.createElement('div');
  mark.className = 'find-hit';
  mark.style.left = `${x0 * z}px`;
  mark.style.top = `${y0 * z}px`;
  mark.style.width = `${(x1 - x0) * z}px`;
  mark.style.height = `${(y1 - y0) * z}px`;
  mark.addEventListener('animationend', () => mark.remove());
  node.appendChild(mark);

  node.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

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

/* --------------------------------------------------------- assistant LLM */

const assist = {
  provider: $('#assist-provider'),
  model: $('#assist-model'),
  key: $('#assist-key'),
  scope: $('#assist-scope'),
  instruction: $('#assist-instruction'),
  panel: $('#assist-panel'),
  info: $('#assist-info'),
  results: $('#assist-results'),
  list: $('#assist-list'),
  count: $('#assist-count'),
  apply: $('#btn-assist-apply'),
  remember: $('#assist-remember'),
  keyNote: $('#assist-key-note'),
};

let providers = [];

api('/api/llm/providers')
  .then((data) => {
    providers = data.providers || [];
    assist.provider.innerHTML = '';
    providers.forEach((p) => {
      const option = document.createElement('option');
      option.value = p.key;
      option.textContent = p.available ? p.label : `${p.label} — paquet absent`;
      option.disabled = !p.available;
      assist.provider.appendChild(option);
    });
    // On présélectionne un fournisseur réellement utilisable.
    const usable = providers.find((p) => p.available);
    if (usable) assist.provider.value = usable.key;
    else assist.provider.innerHTML = '<option value="">Aucun paquet LLM installé</option>';
    refreshKeys();
  })
  .catch(() => { /* serveur trop ancien : le panneau restera inerte */ });

/* Les clés ne sont jamais conservées par le navigateur — ni localStorage, ni
   sessionStorage, ni cookie. Elles vivent côté serveur, dans le trousseau du
   système quand il existe, et la page n'apprend jamais que leur existence. Le
   champ reste donc vide même lorsqu'une clé est enregistrée. */
let keyStatus = { providers: {}, where: '' };

async function refreshKeys() {
  try {
    keyStatus = await api('/api/llm/keys');
  } catch (_) {
    // 403 : instance consultée à distance, les clés enregistrées sont hors jeu.
    keyStatus = { providers: {}, where: '' };
  }
  syncProvider();
}

function syncProvider() {
  const chosen = providers.find((p) => p.key === assist.provider.value);
  assist.model.placeholder = chosen ? chosen.default_model : 'modèle';

  const info = (keyStatus.providers || {})[assist.provider.value] || {};
  assist.key.value = '';
  $('#btn-key-forget').hidden = !(info.stored && info.editable);
  $('#assist-remember').parentElement.hidden = !!info.from_env;

  if (info.from_env) {
    assist.key.placeholder = 'fournie par l’environnement';
    assist.keyNote.textContent =
      ' La clé vient d’une variable d’environnement : rien n’est enregistré par l’application.';
  } else if (info.stored) {
    assist.key.placeholder = 'clé enregistrée — laissez vide pour l’utiliser';
    assist.keyNote.textContent =
      ` La clé est conservée dans le ${keyStatus.where}, sur cette machine, et n’est jamais renvoyée à cette page.`;
  } else {
    assist.key.placeholder = 'collez votre clé';
    assist.keyNote.textContent = '';
  }
}

assist.provider.addEventListener('change', syncProvider);

$('#btn-key-forget').addEventListener('click', async () => {
  try {
    keyStatus = await api(`/api/llm/keys/${assist.provider.value}`, { method: 'DELETE' });
    syncProvider();
    toast('Clé oubliée');
  } catch (err) {
    toast(err.message, true);
  }
});

$('#btn-assist').addEventListener('click', () => toggleAssist());

function toggleAssist(force) {
  const show = force !== undefined ? force : assist.panel.hidden;
  assist.panel.hidden = !show;
  $('#btn-assist').classList.toggle('active', show);
  if (show) { toggleFind(false); togglePages(false); toggleForm(false); assist.instruction.focus(); }
}

/** Page la plus proche du centre de la fenêtre : c'est celle que l'utilisateur regarde. */
function currentPage() {
  const middle = el.main.getBoundingClientRect().top + el.main.clientHeight / 2;
  let best = 0;
  let bestGap = Infinity;
  el.viewer.querySelectorAll('.page').forEach((node) => {
    const rect = node.getBoundingClientRect();
    const gap = Math.abs((rect.top + rect.bottom) / 2 - middle);
    if (gap < bestGap) { bestGap = gap; best = +node.dataset.page; }
  });
  return best;
}

$('#btn-assist-run').addEventListener('click', runAssist);
assist.instruction.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); runAssist(); }
});

async function runAssist() {
  if (!state.docId) return;
  if (!assist.provider.value) return toast('Aucun paquet LLM installé côté serveur.', true);
  const info = (keyStatus.providers || {})[assist.provider.value] || {};
  if (!assist.key.value.trim() && !info.stored) return toast('Collez votre clé API.', true);
  if (!assist.instruction.value.trim()) return toast('Indiquez ce qu’il faut corriger.', true);

  const scopeDocument = assist.scope.value === 'document';
  assist.results.hidden = true;
  assist.info.textContent = '';
  busy(true, 'Squeezing…', 'squeeze');
  try {
    const data = await postJSON(`/api/${state.docId}/assist`, {
      instruction: assist.instruction.value.trim(),
      provider: assist.provider.value,
      api_key: assist.key.value.trim(),
      remember: assist.remember.checked,
      model: assist.model.value.trim(),
      page: scopeDocument ? null : currentPage(),
    });
    // Le champ est vidé dès l'envoi : la clé n'a pas à traîner dans la page.
    if (assist.key.value) { assist.key.value = ''; refreshKeys(); }
    renderSuggestions(data);
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false);
  }
}

function renderSuggestions(data) {
  const found = data.suggestions || [];
  const examined = `${data.examined} fragment(s) examiné(s)`;
  assist.info.textContent = found.length
    ? `${found.length} proposition(s) — ${examined}`
    : `Aucune correction proposée — ${examined}`;
  if (data.truncated) {
    // Mieux vaut le dire que laisser croire que tout a été relu.
    toast('Document trop long : seul le début a été analysé. Relancez page par page.', true);
  }
  if (!found.length) { assist.results.hidden = true; return; }

  assist.list.innerHTML = '';
  found.forEach((s, i) => {
    const li = document.createElement('li');
    li.innerHTML = `
      <label class="pick"><input type="checkbox" checked></label>
      <div class="texts">
        <span class="page-ref">p. ${s.page + 1}</span>
        <del></del>
        <ins></ins>
        <em class="why"></em>
      </div>
      <button class="goto" title="Voir dans la page">↗</button>`;
    li.querySelector('del').textContent = s.original;
    li.querySelector('ins').textContent = s.text;
    li.querySelector('.why').textContent = s.reason;
    li.querySelector('input').addEventListener('change', updateAssistCount);
    li.querySelector('.goto').addEventListener('click', () => {
      const node = pageNode(s.page);
      if (node) node.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
    li._suggestion = s;
    li.dataset.index = String(i);
    assist.list.appendChild(li);
  });
  assist.results.hidden = false;
  updateAssistCount();
}

const pickedSuggestions = () =>
  [...assist.list.querySelectorAll('li')].filter((li) => li.querySelector('input').checked);

function updateAssistCount() {
  const picked = pickedSuggestions().length;
  assist.count.textContent = `${picked} sélectionnée(s)`;
  assist.apply.disabled = picked === 0;
}

const setAllPicks = (checked) => {
  assist.list.querySelectorAll('input').forEach((box) => { box.checked = checked; });
  updateAssistCount();
};
$('#btn-assist-all').addEventListener('click', () => setAllPicks(true));
$('#btn-assist-none').addEventListener('click', () => setAllPicks(false));

assist.apply.addEventListener('click', async () => {
  const picked = pickedSuggestions();
  if (!picked.length) return;
  // On passe par la route d'édition ordinaire : même vérification du texte
  // d'origine, même pile d'annulation. Le LLM n'a aucun accès direct au PDF.
  const edits = picked.map((li) => ({
    id: li._suggestion.id,
    text: li._suggestion.text,
    original: li._suggestion.original,
  }));
  busy(true, 'Application des corrections…');
  try {
    const data = await postJSON(`/api/${state.docId}/edit`, { edits });
    applyState(data);
    refreshAll();
    assist.results.hidden = true;
    assist.info.textContent = `${data.changed} correction(s) appliquée(s)`;
    const skipped = data.skipped ? `, ${data.skipped} ignorée(s)` : '';
    toast(`${data.changed} correction(s) appliquée(s)${skipped} — Ctrl+Z pour annuler`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false);
  }
});

/* ------------------------------------------------------ PDF scannés (sans texte) */

$('#notice-close').addEventListener('click', () => { el.notice.hidden = true; });

/** Un scan n'a pas de texte : l'app n'a rien à y corriger, autant le dire. */
async function checkScan() {
  if (!state.docId) return;
  try {
    const data = await api(`/api/${state.docId}/scan`);
    state.scanPages = data.scan_pages || [];
    el.viewer.querySelectorAll('.page').forEach((node) => {
      node.classList.toggle('scanned', state.scanPages.includes(+node.dataset.page));
    });
    if (!state.scanPages.length) { el.notice.hidden = true; return; }
    el.noticeText.textContent = data.fully_scanned
      ? 'Ce PDF est un scan : ses pages sont des images, il n’y a aucun texte à corriger. '
        + 'Vous pouvez tout de même surligner, caviarder, ajouter du texte, signer ou gérer les pages.'
      : `${state.scanPages.length} page(s) sur ${data.page_count} sont des images sans texte : `
        + 'le clic sur le texte n’y fonctionnera pas.';
    el.notice.hidden = false;
  } catch (_) { /* route absente sur un serveur plus ancien : sans conséquence */ }
}

/* --------------------------------------------------------------- vignettes */

/** Colonne de miniatures du menu latéral : navigation et réordonnancement. */
function buildThumbs() {
  el.thumbs.innerHTML = '';
  if (!state.docId) { el.thumbsBox.hidden = true; return; }
  el.thumbsBox.hidden = false;

  state.pages.forEach((page) => {
    const thumb = document.createElement('div');
    thumb.className = 'thumb';
    thumb.draggable = true;
    thumb.dataset.page = page.number;
    thumb.title = `Page ${page.number + 1}`;

    const img = document.createElement('img');
    img.src = `/api/${state.docId}/page/${page.number}.png?scale=0.2&v=${state.version}`;
    img.alt = '';
    img.draggable = false;
    const label = document.createElement('span');
    label.textContent = page.number + 1;
    thumb.append(img, label);

    thumb.addEventListener('click', () => {
      const node = pageNode(page.number);
      if (node) node.scrollIntoView({ behavior: 'smooth', block: 'start' });
      setMenu(false);
    });

    thumb.addEventListener('dragstart', (e) => {
      dragFrom = page.number;
      thumb.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      // Firefox n'amorce pas le glissement sans données attachées.
      e.dataTransfer.setData('text/plain', String(page.number));
    });
    thumb.addEventListener('dragend', clearDropMarks);
    thumb.addEventListener('dragover', (e) => {
      if (dragFrom === null || dragFrom === page.number) return;
      e.preventDefault();
      const box = thumb.getBoundingClientRect();
      const after = e.clientX > box.left + box.width / 2;
      clearDropMarks(false);
      thumb.classList.add(after ? 'drop-after' : 'drop-before');
    });
    thumb.addEventListener('drop', (e) => {
      e.preventDefault();
      if (dragFrom === null || dragFrom === page.number) return clearDropMarks();
      const box = thumb.getBoundingClientRect();
      const after = e.clientX > box.left + box.width / 2;
      const from = dragFrom;
      clearDropMarks();
      // Position d'insertion dans la liste, puis correction du décalage provoqué
      // par le retrait de la page déplacée quand elle vient d'avant la cible.
      const slot = page.number + (after ? 1 : 0);
      dropPage(from, from < slot ? slot - 1 : slot);
    });

    el.thumbs.appendChild(thumb);
  });
}

let dragFrom = null;

function clearDropMarks(reset = true) {
  el.thumbs.querySelectorAll('.thumb').forEach((n) =>
    n.classList.remove('drop-before', 'drop-after'));
  if (reset) {
    el.thumbs.querySelectorAll('.dragging').forEach((n) => n.classList.remove('dragging'));
    dragFrom = null;
  }
}

async function dropPage(from, to) {
  if (from === to) return;
  const data = await pageOperation('Déplacement…', () =>
    postJSON(`/api/${state.docId}/page/${from}/move`, { to }));
  if (data) toast(`Page déplacée en position ${data.page + 1}`);
}

/* --------------------------------------------------------- formulaires PDF */

$('#btn-form').addEventListener('click', () => toggleForm());

function toggleForm(force) {
  const show = force !== undefined ? force : el.formPanel.hidden;
  el.formPanel.hidden = !show;
  $('#btn-form').classList.toggle('active', show);
  if (!show) return;
  toggleFind(false);
  togglePages(false);
  toggleAssist(false);
  loadFields();
}

async function loadFields() {
  el.formFields.innerHTML = '';
  el.formApply.disabled = true;
  el.formInfo.textContent = 'Lecture des champs…';
  try {
    const data = await api(`/api/${state.docId}/fields`);
    const fields = data.fields || [];
    if (!fields.length) {
      el.formInfo.textContent = 'Ce PDF ne contient aucun champ de formulaire.';
      return;
    }
    el.formInfo.textContent = `${fields.length} champ(s)`;
    fields.forEach((f) => el.formFields.appendChild(fieldRow(f)));
    el.formApply.disabled = false;
  } catch (err) {
    el.formInfo.textContent = err.message;
  }
}

const CHECKED = (v) => !['', 'off', 'false', '0', 'none'].includes(String(v).toLowerCase());

function fieldRow(field) {
  const row = document.createElement('label');
  row.className = 'form-field';
  const head = document.createElement('span');
  head.innerHTML = '<b></b><i class="fpage"></i>';
  head.querySelector('b').textContent = field.name;
  head.querySelector('.fpage').textContent =
    'p. ' + field.pages.map((p) => p + 1).join(', ');

  let input;
  if (field.kind === 'checkbox') {
    row.classList.add('bool');
    input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = CHECKED(field.value);
    // Une case cochée s'écrit avec le nom de son état, que le PDF a choisi.
    row.dataset.on = field.options[0] || '1';
  } else if (field.kind === 'choice' || field.kind === 'radio') {
    input = document.createElement('select');
    const options = field.kind === 'radio' ? ['Off', ...field.options] : field.options;
    if (field.value && !options.includes(field.value)) options.unshift(field.value);
    options.forEach((value) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = value === 'Off' ? '— non coché —' : value;
      input.appendChild(option);
    });
    input.value = field.value || (field.kind === 'radio' ? 'Off' : options[0] || '');
  } else {
    input = document.createElement('input');
    input.type = 'text';
    input.value = field.value || '';
    if (field.max_len) input.maxLength = field.max_len;
  }
  input.dataset.field = field.name;
  input.dataset.kind = field.kind;

  row.append(field.kind === 'checkbox' ? input : head, field.kind === 'checkbox' ? head : input);
  return row;
}

el.formApply.addEventListener('click', async () => {
  const values = {};
  el.formFields.querySelectorAll('[data-field]').forEach((input) => {
    if (input.dataset.kind === 'checkbox') {
      values[input.dataset.field] = input.checked
        ? (input.closest('.form-field').dataset.on || '1')
        : 'Off';
    } else {
      values[input.dataset.field] = input.value;
    }
  });
  busy(true, 'Écriture du formulaire…');
  try {
    const data = await postJSON(`/api/${state.docId}/fields`, { values });
    applyState(data);
    refreshAll();
    toast(`${data.filled} champ(s) renseigné(s)`);
  } catch (err) {
    toast(err.message, true);
  } finally {
    busy(false);
  }
});

/* ---------------------------------------------------- signature manuscrite */

$('#btn-sign').addEventListener('click', openSignature);

const sign = { drawing: false, dirty: false, ctx: null, last: null };

function openSignature() {
  el.signModal.hidden = false;
  sign.ctx = el.signCanvas.getContext('2d');
  clearSignature();
}

function clearSignature() {
  sign.ctx.clearRect(0, 0, el.signCanvas.width, el.signCanvas.height);
  sign.dirty = false;
  $('#sign-ok').disabled = true;
}

/** Le canvas est affiché plus petit que sa résolution : on ramène donc les
    coordonnées de l'événement dans son espace propre. */
function signPoint(e) {
  const box = el.signCanvas.getBoundingClientRect();
  return {
    x: (e.clientX - box.left) * (el.signCanvas.width / box.width),
    y: (e.clientY - box.top) * (el.signCanvas.height / box.height),
  };
}

el.signCanvas.addEventListener('pointerdown', (e) => {
  el.signCanvas.setPointerCapture(e.pointerId);
  sign.drawing = true;
  sign.last = signPoint(e);
});
el.signCanvas.addEventListener('pointermove', (e) => {
  if (!sign.drawing) return;
  const point = signPoint(e);
  const ctx = sign.ctx;
  ctx.strokeStyle = $('#sign-color').value;
  ctx.lineWidth = +$('#sign-width').value * 1.6;
  ctx.lineCap = ctx.lineJoin = 'round';
  ctx.beginPath();
  ctx.moveTo(sign.last.x, sign.last.y);
  ctx.lineTo(point.x, point.y);
  ctx.stroke();
  sign.last = point;
  sign.dirty = true;
  $('#sign-ok').disabled = false;
});
['pointerup', 'pointercancel', 'pointerleave'].forEach((ev) =>
  el.signCanvas.addEventListener(ev, () => { sign.drawing = false; }));

$('#sign-clear').addEventListener('click', clearSignature);
$('#sign-cancel').addEventListener('click', () => { el.signModal.hidden = true; });

$('#sign-ok').addEventListener('click', () => {
  const trimmed = trimSignature();
  if (!trimmed) return toast('Rien n’a été dessiné.', true);
  el.signModal.hidden = true;
  trimmed.toBlob((blob) => {
    clearPendingImage();
    const file = new File([blob], 'signature.png', { type: 'image/png' });
    const url = URL.createObjectURL(blob);
    // Une signature se pose petite : 150 pt de large, soit ~5 cm.
    state.pendingImage = { file, url, ratio: trimmed.width / trimmed.height, width: 150 };
    setMode('placing');
  }, 'image/png');
});

/** Rogne le canvas à l'encadrement du tracé : sans cela la signature serait
    noyée dans un rectangle largement vide, impossible à placer correctement. */
function trimSignature() {
  const { width: w, height: h } = el.signCanvas;
  const pixels = sign.ctx.getImageData(0, 0, w, h).data;
  let x0 = w, y0 = h, x1 = -1, y1 = -1;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      if (pixels[(y * w + x) * 4 + 3] < 8) continue;
      if (x < x0) x0 = x;
      if (x > x1) x1 = x;
      if (y < y0) y0 = y;
      if (y > y1) y1 = y;
    }
  }
  if (x1 < 0) return null;
  const pad = 10;
  x0 = Math.max(0, x0 - pad); y0 = Math.max(0, y0 - pad);
  x1 = Math.min(w - 1, x1 + pad); y1 = Math.min(h - 1, y1 + pad);

  const out = document.createElement('canvas');
  out.width = x1 - x0 + 1;
  out.height = y1 - y0 + 1;
  out.getContext('2d').drawImage(el.signCanvas, x0, y0, out.width, out.height,
    0, 0, out.width, out.height);
  return out;
}

/* ------------------------------------------------------------------- loupe */

const LENS_SIZE = 180;                  // diamètre de la loupe, en pixels d'écran
const LENS_MIN = 1.5, LENS_MAX = 6;

el.lensBtn.addEventListener('click', () => setLens(!state.lens));

function setLens(on) {
  state.lens = on;
  el.lensBtn.classList.toggle('active', on);
  el.viewer.classList.toggle('lens-on', on);
  el.lens.hidden = true;                // réapparaît au premier mouvement sur une page
  if (on) toast('Loupe : molette pour régler le grossissement, Échap pour quitter');
}

el.viewer.addEventListener('mousemove', (e) => {
  if (!state.lens) return;
  // Pendant la saisie d'un fragment, la loupe masquerait le texte en train
  // d'être tapé : on la retire le temps de l'édition.
  const node = state.editing ? null : e.target.closest('.page');
  if (node) drawLens(node, e.clientX, e.clientY);
  else el.lens.hidden = true;
});

el.viewer.addEventListener('mouseleave', () => { el.lens.hidden = true; });

// La molette règle le grossissement au lieu de faire défiler la page.
el.viewer.addEventListener('wheel', (e) => {
  if (!state.lens) return;
  const node = e.target.closest('.page');
  if (!node) return;
  e.preventDefault();
  const factor = state.lensZoom * (e.deltaY < 0 ? 1.12 : 1 / 1.12);
  state.lensZoom = Math.min(LENS_MAX, Math.max(LENS_MIN, Math.round(factor * 100) / 100));
  drawLens(node, e.clientX, e.clientY);
}, { passive: false });

/** Place le disque sous le curseur et recadre l'image de la page derrière lui. */
function drawLens(node, clientX, clientY) {
  const img = node.querySelector('img');
  if (!img || !img.complete || !img.naturalWidth) return;
  const rect = img.getBoundingClientRect();
  const half = LENS_SIZE / 2;
  const f = state.lensZoom;
  // Le point visé doit se retrouver au centre du disque : on décale donc le
  // fond agrandi de la position du curseur, elle-même multipliée par f.
  const x = (clientX - rect.left) * f;
  const y = (clientY - rect.top) * f;

  el.lens.hidden = false;
  el.lens.style.transform = `translate3d(${clientX - half}px, ${clientY - half}px, 0)`;
  el.lens.style.backgroundImage = `url("${hiResSrc(node) || img.src}")`;
  el.lens.style.backgroundSize = `${rect.width * f}px ${rect.height * f}px`;
  el.lens.style.backgroundPosition = `${half - x}px ${half - y}px`;
}

/** Rendu à 4× de la page, chargé à la demande.
 *  L'image d'affichage n'a que 2× de détail : au-delà, la loupe grossirait du
 *  flou, précisément là où l'on veut voir net. Le premier passage sur une page
 *  utilise donc l'image courante, puis bascule sur la version fine une fois
 *  celle-ci arrivée. */
function hiResSrc(node) {
  const wanted = `/api/${state.docId}/page/${node.dataset.page}.png?scale=4&v=${state.version}`;
  if (node._hiResSrc === wanted) return node._hiResReady ? wanted : null;
  node._hiResSrc = wanted;
  node._hiResReady = false;
  const probe = new Image();
  probe.onload = () => { if (node._hiResSrc === wanted) node._hiResReady = true; };
  probe.src = wanted;
  return null;
}

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
  el.sideActions.hidden = true;
  el.dock.hidden = true;
  el.notice.hidden = true;
  el.thumbsBox.hidden = true;
  el.thumbs.innerHTML = '';
  state.scanPages = [];
  setMenu(false);
  toggleFind(false);
  togglePages(false);
  toggleAssist(false);
  toggleForm(false);
  setMode(null);
  setLens(false);
  resetSearch();
  // La suppression efface aussi la sauvegarde : fermer est un geste volontaire.
  try { await fetch(`/api/${id}`, { method: 'DELETE' }); } catch (_) { /* sans importance */ }
  loadResumable();
});

/* -------------------------------------------------------------- raccourcis */

document.addEventListener('keydown', (e) => {
  // Échap quitte le mode en cours ; l'édition d'un fragment gère sa propre
  // touche Échap, on ne lui marche donc pas dessus.
  if (e.key === 'Escape' && !el.signModal.hidden) { el.signModal.hidden = true; return; }
  if (e.key === 'Escape' && menuOpen()) { setMenu(false); return; }
  const inMode = state.adding || state.highlighting || state.redacting || state.placing;
  if (e.key === 'Escape' && !state.editing && (inMode || state.lens)) {
    if (inMode) setMode(null);
    if (state.lens) setLens(false);
    return;
  }
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

// Un serveur laissé tourner depuis une version antérieure servirait cette page
// tout en ignorant ses routes : autant le dire tout de suite plutôt qu'au
// premier clic sur une fonctionnalité récente.
fetch('/healthz').then((res) => {
  if (!res.ok) toast(STALE_SERVER_ERROR, true);
}).catch(() => { /* serveur injoignable : les appels suivants le signaleront */ });

loadResumable();

window.addEventListener('beforeunload', (e) => {
  if (state.docId && state.version > 0) { e.preventDefault(); e.returnValue = ''; }
});
