'use strict';
const $ = id => document.getElementById(id);
const START = '__start__';
const roleNames = { cover: '封面', front: '卷首图', body: '正文插图', ignore: '忽略' };
let state = null, active = 0, viewStart = 0, viewPage = null, busy = false, toastTimer, savedSnapshot = null, generatedFile = null, generating = false;
let paragraphEditor = null, editHistory = [];
let localImageURLs = new Map();
const MAX_PROJECT = 64 * 1024 * 1024;
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
const imageAt = () => state.donor.illustrations[active];
const decisionAt = () => state.settings.images.find(x => x.id === imageAt().id);
const chapterStart = id => '__chapter_start__:' + id;
const chapterEnd = id => '__chapter_end__:' + id;
const pageStart = id => '__page_start__:' + id;
const pageEnd = id => '__page_end__:' + id;
const pageForAnchor = id => (state?.text.pages || []).find(p => pageStart(p.id) === id || pageEnd(p.id) === id);
function renderSections() {
  $('chapterSelect').innerHTML = (state.text.pages || []).map(p => '<option value="' + escapeHTML(p.id) + '">' + escapeHTML(p.title) + '</option>').join('') + state.text.chapters.map(c => '<option value="' + escapeHTML(c.id) + '">' + escapeHTML(c.title) + '</option>').join('');
}
function paragraphIndex(id) {
  if (id === START) return -1;
  const chapter = state?.text.chapters.find(c => chapterStart(c.id) === id || chapterEnd(c.id) === id);
  if (chapter) {
    const ps = state.text.paragraphs.filter(p => p.chapter_id === chapter.id);
    return id === chapterStart(chapter.id) ? ps[0]?.index ?? -1 : ps.at(-1)?.index ?? -1;
  }
  return state.text.paragraphs.findIndex(x => x.id === id);
}
function canonicalAnchor(id) {
  if (id === START) return chapterStart(state.text.paragraphs[0]?.chapter_id || state.text.chapters[0].id);
  const index = state.text.paragraphs.findIndex(p => p.id === id);
  if (index >= 0 && state.text.paragraphs[index+1]?.chapter_id !== state.text.paragraphs[index].chapter_id) return chapterEnd(state.text.paragraphs[index].chapter_id);
  return id;
}
function locationLabel(id) {
  if (!id) return '尚未选择插入位置';
  const page = pageForAnchor(id);
  if (page) return page.title + (id === pageStart(page.id) ? ' · 扉页开头' : ' · 页面末尾');
  id = canonicalAnchor(id);
  const chapter = state.text.chapters.find(c => chapterStart(c.id) === id || chapterEnd(c.id) === id);
  if (chapter) return chapter.title + (id === chapterStart(chapter.id) ? ' · 本章开头（标题之后、首段之前）' : ' · 本章结尾（末段之后）');
  const p = state.text.paragraphs[paragraphIndex(id)];
  return p ? p.chapter_title + ' · 第 ' + (p.index+1) + ' 段之后：' + (p.text.trim() ? p.text.slice(0,32) : '（空段落）') : '无效位置';
}
function clearLocalImageURLs() {
  for (const url of localImageURLs.values()) URL.revokeObjectURL(url);
  localImageURLs.clear();
}
function imageURL(id) {
  const attachment = (state.settings.added_images || []).find(x => x.id === id);
  if (!attachment) return '/api/image/' + encodeURIComponent(state.session) + '/' + encodeURIComponent(id);
  if (!localImageURLs.has(id)) {
    const data = Uint8Array.from(atob(attachment.data), c => c.charCodeAt(0));
    const info = state.donor.illustrations.find(x => x.id === id);
    localImageURLs.set(id, URL.createObjectURL(new Blob([data], {type:info.media_type})));
  }
  return localImageURLs.get(id);
}

function normalizeImageRotations(images) {
  if (!Array.isArray(images)) throw new Error('校对项目缺少图片设置。');
  return images.map(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) throw new Error('项目中存在无效的图片设置。');
    const rotation = Object.hasOwn(item, 'rotation') ? item.rotation : 0;
    if (!Number.isInteger(rotation) || ![0, 90, 180, 270].includes(rotation)) throw new Error('图片旋转角度无效，只能是整数 0、90、180 或 270。');
    return {...item, rotation};
  });
}

// All previews fit the rotated bounding box, without changing or re-encoding the source.
function imagePreviewHTML(image, decision, alt, lazy = false) {
  return '<span class="image-preview" data-preview-id="' + escapeHTML(image.id) + '" data-rotation="' + decision.rotation + '"><img' + (lazy ? ' loading="lazy"' : '') + ' src="' + escapeHTML(imageURL(image.id)) + '" alt="' + escapeHTML(alt) + '"></span>';
}
function fitImagePreview(preview) {
  const img = preview.querySelector('img');
  if (!img) return;
  const width = preview.clientWidth, height = preview.clientHeight;
  if (!img.complete || !img.naturalWidth || !img.naturalHeight || !width || !height) {
    img.style.visibility = 'hidden';
    img.style.removeProperty('transform');
    img.style.removeProperty('width');
    img.style.removeProperty('height');
    return;
  }
  const rotation = Number(preview.dataset.rotation), sideways = rotation === 90 || rotation === 270;
  const boxWidth = sideways ? img.naturalHeight : img.naturalWidth;
  const boxHeight = sideways ? img.naturalWidth : img.naturalHeight;
  const scale = Math.min(width / boxWidth, height / boxHeight);
  img.style.width = img.naturalWidth * scale + 'px';
  img.style.height = img.naturalHeight * scale + 'px';
  img.style.transform = 'translate(-50%, -50%) rotate(' + rotation + 'deg)';
  img.style.visibility = 'visible';
}
function refreshImagePreviews(root) {
  const previews = root.matches('.image-preview') ? [root] : root.querySelectorAll('.image-preview');
  for (const preview of previews) {
    const img = preview.querySelector('img');
    img.onload = img.onerror = () => fitImagePreview(preview);
    fitImagePreview(preview);
  }
}
function renderCurrentImage(image, decision) {
  const img = $('currentImage'), preview = img.parentElement, url = imageURL(image.id);
  preview.dataset.previewId = image.id;
  preview.dataset.rotation = decision.rotation;
  if (img.getAttribute('src') !== url) {
    // Never show the previous image's transform while the next source is loading.
    img.style.visibility = 'hidden';
    img.style.removeProperty('transform');
    img.style.removeProperty('width');
    img.style.removeProperty('height');
    img.src = url;
  }
  img.alt = roleNames[decision.role] + '，第 ' + (active + 1) + ' 张';
  refreshImagePreviews(preview);
  updateRotationControls();
}
function updateRotationControls() {
  const disabled = busy || !state || !state.donor.illustrations.length;
  $('rotateLeft').disabled = $('rotateRight').disabled = disabled;
  $('imageRotation').textContent = '角度 ' + (state?.donor.illustrations.length ? decisionAt().rotation : 0) + '°';
}
function rotateActiveImage(delta) {
  if (busy || !state || !state.donor.illustrations.length) return;
  const decision = decisionAt();
  decision.rotation = (decision.rotation + delta + 360) % 360;
  // Update only pictures: keep placement, confirmation, order and reader scroll intact.
  for (const preview of document.querySelectorAll('.image-preview')) {
    if (preview.dataset.previewId !== decision.id) continue;
    preview.dataset.rotation = decision.rotation;
    fitImagePreview(preview);
  }
  updateRotationControls();
}
$('rotateLeft').addEventListener('click', () => rotateActiveImage(-90));
$('rotateRight').addEventListener('click', () => rotateActiveImage(90));
const imagePreviewRoots = [$('imageRail'), $('currentImage').parentElement, $('imageOrderList')];
if (typeof ResizeObserver !== 'undefined') {
  const previewObserver = new ResizeObserver(entries => {
    for (const entry of entries) refreshImagePreviews(entry.target);
  });
  for (const root of imagePreviewRoots) previewObserver.observe(root);
} else {
  window.addEventListener('resize', () => imagePreviewRoots.forEach(refreshImagePreviews));
}

function toast(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 5000);
}

function setBusy(value) {
  busy = value;
  document.body.classList.toggle('busy', value);
  for (const id of ['analyze', 'demo', 'export', 'report', 'reimport', 'saveProject', 'acceptAll', 'addImages', 'removeLocalImage']) $(id).disabled = value;
  for (const id of ['generateText','saveRawTxt','convertTxt']) $(id).disabled = value || generating;
  $('analyze').disabled = value || generating;
  $('demo').disabled = value || generating;
  updateRotationControls();
  if (!value && state) updateSummary();
}

async function request(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = '操作失败，请检查本地服务是否仍在运行。';
    try { message = (await response.json()).error || message; } catch (_) { /* Use readable fallback. */ }
    throw new Error(message);
  }
  return response;
}

function bindUpload(inputID, labelID, dropID) {
  const input = $(inputID), label = $(labelID), drop = $(dropID);
  input.addEventListener('change', () => { if (inputID === 'textFile') generatedFile = null; label.textContent = input.files[0]?.name || (inputID === 'textFile' ? '选择 mojimoon 无图 EPUB（也支持 TXT）' : '选择同卷 auto-novel EPUB'); });
  drop.addEventListener('dragover', event => { event.preventDefault(); drop.classList.add('drag'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('drag'));
  drop.addEventListener('drop', event => {
    event.preventDefault(); drop.classList.remove('drag');
    if (busy || !event.dataTransfer.files.length) return;
    const transfer = new DataTransfer(); transfer.items.add(event.dataTransfer.files[0]);
    input.files = transfer.files; input.dispatchEvent(new Event('change'));
  });
}
bindUpload('textFile', 'textName', 'textDrop');
bindUpload('donorFile', 'donorName', 'donorDrop');

async function analyze(demo = false) {
  if (busy || generating) return;
  let body;
  if (!demo) {
    const text = $('textFile').files[0] || generatedFile, donor = $('donorFile').files[0];
    if (!text || !donor) { $('importStatus').textContent = '请先选择中文正文和同卷插图源。'; return; }
    if (!/\.(epub|txt)$/i.test(text.name) || !/\.epub$/i.test(donor.name)) { $('importStatus').textContent = '正文支持 EPUB／TXT，插图源支持 EPUB。'; return; }
    if (text.size > 80 * 1024 * 1024 || donor.size > 80 * 1024 * 1024) { $('importStatus').textContent = '每份文件请控制在 80 MB 以内，建议按卷导入。'; return; }
    body = new FormData(); body.append('text', text); body.append('donor', donor);
  }
  setBusy(true);
  $('importStatus').textContent = '正在读取正文、提取插图并匹配位置… 文件较大时请稍候。';
  try {
    const response = await request(demo ? '/api/demo' : '/api/analyze', { method: 'POST', body });
    const next = await response.json();
    next.settings.images = normalizeImageRotations(next.settings.images);
    next.settings.image_order = validateImageOrder(next.donor, next.settings.image_order);
    clearLocalImageURLs();
    state = next; editHistory = []; paragraphEditor = null; active = 0; viewStart = 0; viewPage = null; savedSnapshot = JSON.stringify(state.settings);
    renderMetadata();
    $('intro').hidden = true; $('importPanel').hidden = true; $('workspace').hidden = false;
    $('importStatus').textContent = '';
    renderSections();
    $('sourceSummary').textContent = '正文 ' + state.text.paragraphs.length + ' 段 · ' + state.text.chapters.length + ' 个章节 · ' + state.donor.illustrations.length + ' 张图片 · 查看处理说明';
    $('sourceNotes').innerHTML = '<p><strong>正文：</strong>' + escapeHTML(state.text.filename) + '<br><strong>插图：</strong>' + escapeHTML(state.donor.filename) + '</p>' + [...state.text.warnings, ...state.donor.warnings].map(x => '<p>· ' + escapeHTML(x) + '</p>').join('');
    $('searchResults').hidden = true; $('searchText').value = '';
    renderAll(true);
    window.scrollTo({ top: 0, behavior: 'smooth' });
    if (demo) toast('已载入合成示例；两张正文插图等待你确认。');
  } catch (error) {
    $('importStatus').textContent = error.message;
  } finally { setBusy(false); }
}
$('analyze').addEventListener('click', () => analyze(false));
$('demo').addEventListener('click', () => analyze(true));
$('reimport').addEventListener('click', () => {
  $('intro').hidden = false; $('importPanel').hidden = false;
  window.scrollTo({ top: 0, behavior: 'smooth' });
  if (state) toast('重新分析会替换当前工作区；需要继续校对时，请先保存项目。');
});

function renderRail() {
  $('imageRail').innerHTML = state.donor.illustrations.map((image, index) => {
    const decision = state.settings.images.find(x => x.id === image.id);
    const pending = decision.role === 'body' && (!decision.confirmed || !decision.after_id);
    return '<button class="thumb ' + (index === active ? 'active' : '') + '" data-image="' + index + '" aria-label="第 ' + (index + 1) + ' 张，' + roleNames[decision.role] + (pending ? '，待确认' : '') + '" aria-pressed="' + (index === active) + '">' + imagePreviewHTML(image, decision, '插图缩略图', true) + '<span class="thumb-label">' + (index + 1) + ' · ' + roleNames[decision.role] + '<i class="dot ' + (pending ? 'pending' : 'done') + '"></i></span></button>';
  }).join('');
  refreshImagePreviews($('imageRail'));
}

function updateSummary() {
  if (!state) return;
  $('undoTextEdit').disabled = busy || !editHistory.length;
  $('renameChapter').disabled = busy || !!viewPage;
  const images = state.settings.images;
  const bodies = images.filter(x => x.role === 'body');
  const pending = bodies.filter(x => !x.confirmed || !x.after_id).length;
  const included = images.filter(x => x.role !== 'ignore').length;
  $('reviewCount').textContent = bodies.length + ' 张正文插图 · ' + pending + ' 张待确认';
  $('exportStatus').textContent = pending ? '还有 ' + pending + ' 张正文插图需要确认' : '已就绪，可以重新装订';
  $('exportHint').textContent = pending ? '核对图片前后文，确认建议位置，或手动选择一段之后插入。' : (state.text.layout === 'mojimoon' ? '保留 mojimoon 原排版与目录' : '保留中文正文 · 统一排版') + ' · 包含 ' + included + ' 张图片 · JPEG 旋转优先无损处理';
  $('export').disabled = busy || pending > 0;
  $('report').disabled = busy || pending > 0;
  $('acceptAll').disabled = busy || !bodies.some(x => x.after_id && !x.confirmed);
  $('bookTitle').value.trim() || ($('export').disabled = true);
}

function renderAll(jump = false) {
  renderRail(); renderActive(jump); updateSummary();
}

function renderActive(jump = false) {
  const image = imageAt(), decision = decisionAt();
  $('imageNumber').textContent = String(active + 1).padStart(2, '0') + ' / ' + String(state.donor.illustrations.length).padStart(2, '0');
  renderCurrentImage(image, decision);
  $('viewImage').href = imageURL(image.id);
  $('imageInfo').textContent = (image.width ? image.width + ' × ' + image.height + ' · ' : '') + (image.size / 1024).toFixed(0) + ' KB · 原图';
  $('imageRole').value = decision.role;
  $('removeLocalImage').hidden = !image.local;
  if (image.local) $('imageInfo').textContent += ' · ' + (state.settings.added_images.find(x => x.id === image.id)?.filename || '本地图片');
  $('placeTitlePage').hidden = !(state.text.pages || []).length;
  $('previousImage').disabled = active === 0; $('nextImage').disabled = active === state.donor.illustrations.length - 1;
  $('donorContext').innerHTML = '<p><strong>图片之前</strong><br>' + (image.before.length ? image.before.map(escapeHTML).join('<br><br>') : '没有可用中文前文') + '</p><p><strong>图片之后</strong><br>' + (image.after.length ? image.after.map(escapeHTML).join('<br><br>') : '没有可用中文后文') + '</p>';
  renderImageOrder();
  const body = decision.role === 'body';
  $('bodyControls').hidden = !body; $('nonBodyNote').hidden = body;
  $('positionTitle').textContent = body ? (decision.confirmed ? '已确认插入位置' : '建议插入位置') : roleNames[decision.role];
  $('matchScore').hidden = !body;
  const selectedSuggestion = (image.suggestions || []).find(x => x.after_id === decision.after_id);
  $('matchScore').textContent = selectedSuggestion ? '匹配 ' + selectedSuggestion.score.toFixed(2) : '手动位置';
  $('matchScore').classList.toggle('low', !!selectedSuggestion && selectedSuggestion.score < .55);
  const titlePage = pageForAnchor(decision.after_id);
  $('matchNote').textContent = body && titlePage ? '图片会写入原有扉页。请选择扉页开头或页面末尾，并确认位置。' : body ? (image.note || '请手动选择正文中的插入位置。') + ' 分数是相似度，不代表准确率。' : '';
  const nonBody = { cover: '这张图片会作为 EPUB 封面，放在整卷最前面。每卷只能有一张封面。', front: '这张图片会放在封面之后、正文之前。多张卷首图可在下方调整顺序。', ignore: '这张图片不会写入导出的 EPUB。你随时可以重新选择用途。' };
  $('nonBodyNote').textContent = nonBody[decision.role] || '';
  if (!body) return;
  const suggestions = (image.suggestions || []).filter(s => currentLocations().has(s.after_id));
  $('candidates').innerHTML = '<option value="">手动选择正文位置</option>' + suggestions.map((s, i) => {
    const label = locationLabel(s.after_id);
    return '<option value="' + escapeHTML(s.after_id) + '">匹配 ' + s.score.toFixed(2) + ' · ' + escapeHTML(label) + '</option>';
  }).join('');
  $('candidates').value = suggestions.some(x => x.after_id === decision.after_id) ? decision.after_id : '';
  if (jump) {
    viewPage = pageForAnchor(decision.after_id)?.id || null;
    const index = Math.max(0,paragraphIndex(decision.after_id));
    const first = state.text.paragraphs.findIndex(p => p.chapter_id === state.text.paragraphs[index]?.chapter_id);
    viewStart = Math.max(first,index-2);
    const targetChapter = state.text.chapters.find(c => chapterStart(c.id) === decision.after_id || chapterEnd(c.id) === decision.after_id);
    if (targetChapter) $('chapterSelect').value = targetChapter.id;
    else if (!viewPage && state.text.paragraphs[index]) $('chapterSelect').value = state.text.paragraphs[index].chapter_id;
  }
  renderReader();
  $('confirmPosition').disabled = !decision.after_id;
  $('confirmPosition').classList.toggle('done', !!decision.confirmed);
  $('confirmPosition').textContent = decision.confirmed ? '已确认 ✓' : '确认这个位置 ✓';
}

function renderReader() {
  const paragraphs = state.text.paragraphs, decision = decisionAt();
  const page = (state.text.pages || []).find(p => p.id === viewPage);
  $('renameChapter').disabled = busy || !!page;
  $('placeChapterStart').textContent = page ? '插入扉页开头' : '插入本章开头';
  $('placeChapterEnd').textContent = page ? '插入扉页末尾' : '插入本章结尾';
  if (page) {
    const slot = (id,label) => '<button class="slot chapter-edge ' + (decision.after_id === id ? 'selected' : '') + '" data-slot="' + id + '">' + (decision.after_id === id ? '✓ 图片位置：' : '＋ ') + escapeHTML(label) + '</button>';
    $('reader').innerHTML = '<h4 class="reader-chapter">' + escapeHTML(page.title) + '</h4>' + slot(pageStart(page.id),'扉页开头') + page.preview.map(t => '<p>' + escapeHTML(t) + '</p>').join('') + '<p class="match-note">图片会写入这一页，导出时不显示重复的卷号标题；没有插图的空扉页会自动跳过。</p>' + slot(pageEnd(page.id),'扉页末尾');
    $('readerRange').textContent = page.title;
    $('previousPage').disabled = true; $('nextPage').disabled = true;
    $('chapterSelect').value = page.id;
    $('selectedPosition').textContent = '当前选择：' + locationLabel(decision.after_id);
    $('reader').scrollTop = 0;
    return;
  }
  const selectedChapter = state.text.chapters.find(c => c.id === $('chapterSelect').value);
  if (selectedChapter && !selectedChapter.count) {
    $('reader').innerHTML = '<h4 class="reader-chapter">'+escapeHTML(selectedChapter.title)+'</h4><p class="match-note">本章暂无正文段落。</p><button class="secondary" data-add-empty="'+selectedChapter.id+'">添加段落</button>';
    $('readerRange').textContent = '空章节';
    $('previousPage').disabled = true; $('nextPage').disabled = true;
    $('selectedPosition').textContent = '当前选择：'+locationLabel(decision.after_id);
    return;
  }
  viewStart = Math.max(0, Math.min(viewStart, paragraphs.length - 1));
  const end = Math.min(paragraphs.length, viewStart + 6);
  const slot = (id, label, edge = false) => '<button class="slot ' + (edge ? 'chapter-edge ' : '') + (canonicalAnchor(decision.after_id) === id ? 'selected' : '') + '" data-slot="' + id + '" aria-label="' + escapeHTML(label) + '">' + (canonicalAnchor(decision.after_id) === id ? '✓ 图片位置：' : '＋ ') + escapeHTML(label) + '</button>';
  let content = '';
  for (let i = viewStart; i < end; i++) {
    const p = paragraphs[i];
    if (i === 0 || paragraphs[i-1].chapter_id !== p.chapter_id) {
      if (i === viewStart && i > 0) content += slot(chapterEnd(paragraphs[i-1].chapter_id), '上一章结尾 · ' + paragraphs[i-1].chapter_title, true);
      content += '<h4 class="reader-chapter">' + escapeHTML(p.chapter_title) + '</h4>' + slot(chapterStart(p.chapter_id), '本章开头 · ' + p.chapter_title, true);
    }
    content += '<div class="reader-paragraph"><span class="paragraph-meta">' + escapeHTML(p.chapter_title) + ' · ' + (i + 1) + '</span><p>' + (p.text.trim() ? escapeHTML(p.text) : '<span class="empty-paragraph">（空段落）</span>') + '</p><div class="paragraph-tools">' + [['edit','修改'],['before','前面添加'],['after','后面添加'],['delete','删除']].map(([op,label]) => '<button class="quiet '+(op==='delete'?'delete-paragraph':'')+'" data-paragraph="'+p.id+'" data-edit-op="'+op+'">'+label+'</button>').join('')+'</div></div>';
    content += paragraphs[i+1]?.chapter_id !== p.chapter_id ? slot(chapterEnd(p.chapter_id), '本章结尾 · ' + p.chapter_title, true) : slot(p.id, '在第 ' + (i+1) + ' 段之后插入');
  }
  $('reader').innerHTML = content;
  $('readerRange').textContent = (viewStart + 1) + '–' + end + ' / ' + paragraphs.length + ' 段';
  $('previousPage').disabled = viewStart === 0; $('nextPage').disabled = end === paragraphs.length;
  $('chapterSelect').value = paragraphs[viewStart].chapter_id;
  $('selectedPosition').textContent = '当前选择：' + locationLabel(decision.after_id);
  $('reader').scrollTop = 0;
}
for (const [id, anchor] of [['placeChapterStart',chapterStart],['placeChapterEnd',chapterEnd]]) {
  $(id).addEventListener('click', () => {
    const id = $('chapterSelect').value;
    decisionAt().after_id = (viewPage ? (anchor === chapterStart ? pageStart : pageEnd) : anchor)(id);
    decisionAt().confirmed = false;
    renderAll(true);
  });
}

$('placeTitlePage').addEventListener('click', () => {
  const page = (state.text.pages || []).find(p => p.id === viewPage) || state.text.pages?.[0];
  if (!page) return;
  Object.assign(decisionAt(), {role: 'body', after_id: pageStart(page.id), confirmed: false});
  renderAll(true);
});

$('imageRail').addEventListener('click', event => {
  const button = event.target.closest('[data-image]'); if (!button) return;
  active = Number(button.dataset.image); $('searchResults').hidden = true; renderAll(true);
});
$('previousImage').addEventListener('click', () => { if (active > 0) { active--; renderAll(true); } });
$('nextImage').addEventListener('click', () => { if (active < state.donor.illustrations.length - 1) { active++; renderAll(true); } });
$('imageRole').addEventListener('change', event => {
  const decision = decisionAt(); decision.role = event.target.value; decision.confirmed = decision.role !== 'body';
  if (decision.role === 'cover') {
    const other = state.settings.images.find(x => x.id !== decision.id && x.role === 'cover');
    if (other) { other.role = 'front'; other.confirmed = true; toast('原封面已改为卷首图。'); }
  }
  if (decision.role === 'body' && !decision.after_id) decision.after_id = imageAt().suggestions.find(s => currentLocations().has(s.after_id))?.after_id || null;
  renderAll(true);
});
$('candidates').addEventListener('change', event => {
  if (event.target.value === '') return;
  const suggestion = imageAt().suggestions.find(s => s.after_id === event.target.value);
  if (!suggestion || !currentLocations().has(suggestion.after_id)) return;
  decisionAt().after_id = suggestion.after_id; decisionAt().confirmed = false; renderAll(true);
});
$('reader').addEventListener('click', event => {
  if (busy) return;
  const edit = event.target.closest('[data-edit-op], [data-add-empty]');
  if (edit) { openParagraphEditor(edit.dataset.editOp || 'empty', edit.dataset.paragraph || edit.dataset.addEmpty); return; }
  const button = event.target.closest('[data-slot]'); if (!button) return;
  decisionAt().after_id = button.dataset.slot; decisionAt().confirmed = false; renderAll(false);
});
$('confirmPosition').addEventListener('click', () => {
  if (!decisionAt().after_id) return;
  decisionAt().confirmed = true; renderAll(false); toast('已确认这张插图的位置。');
});
$('acceptAll').addEventListener('click', () => {
  let count = 0;
  for (const item of state.settings.images) if (item.role === 'body' && item.after_id && !item.confirmed) { item.confirmed = true; count++; }
  renderAll(false); toast('已采用 ' + count + ' 张已定位图片的当前建议；未定位图片仍需处理。');
});
$('previousPage').addEventListener('click', () => { viewStart -= 5; renderReader(); });
$('nextPage').addEventListener('click', () => { viewStart += 5; renderReader(); });
$('chapterSelect').addEventListener('change', event => {
  viewPage = (state.text.pages || []).find(p => p.id === event.target.value)?.id || null;
  if (viewPage) { renderReader(); return; }
  const index = state.text.paragraphs.findIndex(p => p.chapter_id === event.target.value);
  if (index >= 0) viewStart = index;
  renderReader();
});
$('searchForm').addEventListener('submit', event => {
  event.preventDefault(); const query = $('searchText').value.trim();
  if (!query) { $('searchResults').hidden = true; return; }
  const matches = state.text.paragraphs.filter(p => p.text.toLocaleLowerCase().includes(query.toLocaleLowerCase())).slice(0, 30);
  $('searchResults').innerHTML = matches.length ? matches.map(p => '<button data-result="' + p.index + '">' + escapeHTML(p.chapter_title + ' · ' + p.text.slice(0, 90)) + '</button>').join('') : '<p class="match-note">没有找到这段文字，请缩短搜索内容。</p>';
  $('searchResults').hidden = false;
});
$('searchResults').addEventListener('click', event => {
  const button = event.target.closest('[data-result]'); if (!button) return;
  viewPage = null; viewStart = Math.max(0, Number(button.dataset.result) - 1); $('searchResults').hidden = true; renderReader();
});
for (const id of ['bookTitle', 'bookAuthor']) $(id).addEventListener('input', () => {
  state.settings.title = $('bookTitle').value.trim(); state.settings.author = $('bookAuthor').value.trim(); updateSummary();
});

function download(blob, filename) {
  const url = URL.createObjectURL(blob), anchor = document.createElement('a');
  anchor.href = url; anchor.download = filename; document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}
function safeName(value) { return (value || '合并电子书').replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').slice(0, 100); }
$('saveProject').addEventListener('click', () => {
  if (!state) return;
  download(new Blob([JSON.stringify(state.settings, null, 2)], { type: 'application/json' }), safeName(state.settings.title) + '·校对项目.json');
  savedSnapshot = JSON.stringify(state.settings);
  toast('已保存章节标题、段落修改、插图位置与角度和书籍信息；恢复时需导入同一组原始文件。');
});
$('loadProject').addEventListener('change', async event => {
  const file = event.target.files[0]; if (!file || !state || busy) return;
  setBusy(true);
  try {
    if (file.size > MAX_PROJECT) throw new Error('项目文件过大。');
    const project = JSON.parse(await file.text());
    if (project.version !== 1 || project.text_fingerprint !== state.text.fingerprint || project.donor_fingerprint !== state.donor.fingerprint) throw new Error('项目与当前文件不一致，请先导入保存项目时的同一组文件。');
    if ((project.layout || 'rebuild') !== state.text.layout) throw new Error('项目使用了旧的正文重排方式，请在保留 mojimoon 底稿的模式下重新确认图片位置。');
    const normalizedImages = normalizeImageRotations(project.images);
    const editedText = await previewEdits(project.text_edits || []);
    const nextDonor = await previewImages(project.added_images || []);
    const imageOrder = validateImageOrder(nextDonor, project.image_order);
    sortImageList(nextDonor, imageOrder);
    const ids = new Set(nextDonor.illustrations.map(x => x.id)), pids = currentLocations(editedText), seen = new Set();
    if (normalizedImages.length !== ids.size || typeof project.title !== 'string' || typeof project.author !== 'string') throw new Error('校对项目格式不完整。');
    for (const item of normalizedImages) {
      if (!item || !ids.has(item.id) || seen.has(item.id) || !Object.hasOwn(roleNames, item.role) || typeof item.confirmed !== 'boolean' || (item.after_id != null && !pids.has(item.after_id))) throw new Error('项目中存在无效的图片或段落位置。');
      seen.add(item.id);
    }
    if (normalizedImages.filter(x => x.role === 'cover').length > 1) throw new Error('项目中存在多张封面。');
    for (const key of ['description','series','series_index','description_source']) if (key in project && typeof project[key] !== 'string') throw new Error('书籍元数据格式无效。');
    if ('subjects' in project && (!Array.isArray(project.subjects) || project.subjects.some(x => typeof x !== 'string'))) throw new Error('标签格式无效。');
    clearLocalImageURLs();
    state.text = editedText; state.donor = nextDonor; active = Math.min(active, nextDonor.illustrations.length - 1); editHistory = [];
    state.settings = {...state.settings, ...project, images: normalizedImages, text_edits: project.text_edits || [], added_images: project.added_images || [], image_order: imageOrder}; savedSnapshot = JSON.stringify(state.settings); renderMetadata();
    renderSections(); renderAll(true); toast('已恢复章节标题、段落修改、插图位置与角度，以及新增图片和顺序。');
  } catch (error) { toast('无法载入：' + error.message); }
  finally { event.target.value = ''; setBusy(false); }
});

async function exportFile(report = false) {
  if (busy || !state) return;
  setBusy(true);
  try {
    const response = await request(report ? '/api/report' : '/api/export', { method:'POST', headers:{ 'Content-Type':'application/json' }, body:JSON.stringify({session:state.session, settings:state.settings}) });
    const encodedName = response.headers.get('Content-Disposition')?.match(/filename\*=UTF-8''(.+)$/i)?.[1];
    download(await response.blob(), encodedName ? decodeURIComponent(encodedName) : safeName(state.settings.title) + (report ? '·合并记录.html' : '.epub'));
    toast(report ? '合并记录已下载。' : 'EPUB 已导出，祝阅读愉快。');
  } catch (error) { toast(error.message); }
  finally { setBusy(false); }
}
$('export').addEventListener('click', () => exportFile(false));
$('report').addEventListener('click', () => exportFile(true));
window.addEventListener('beforeunload', event => {
  if (state && (JSON.stringify(state.settings) !== savedSnapshot || ($('paragraphDialog').open && $('paragraphText').value !== editorOriginalText()))) { event.preventDefault(); event.returnValue = ''; }
});



function currentLocations(text = state.text) {
  return new Set([START, ...(text.pages || []).flatMap(p => [pageStart(p.id),pageEnd(p.id)]), ...text.paragraphs.map(p => p.id), ...text.chapters.flatMap(c => [chapterStart(c.id),chapterEnd(c.id)])]);
}
async function previewEdits(edits) {
  const body = JSON.stringify({session:state.session, text_edits:edits});
  if (new Blob([body]).size > 8*1024*1024) throw new Error('文字修改记录过大，请减少修改后再保存。');
  return (await request('/api/text/edit', {method:'POST', headers:{'Content-Type':'application/json'}, body})).json();
}
function editorOriginalText() {
  if (paragraphEditor?.op === 'rename_chapter') return state.text.chapters.find(c => c.id === paragraphEditor.id)?.title || '';
  return paragraphEditor?.op === 'edit' ? state.text.paragraphs.find(p => p.id === paragraphEditor.id)?.text || '' : '';
}
$('renameChapter').addEventListener('click', () => {
  if (!busy && state && !viewPage) openParagraphEditor('rename_chapter', $('chapterSelect').value);
});
function openParagraphEditor(op, id) {
  const p = state.text.paragraphs.find(p => p.id === id);
  const chapter = state.text.chapters.find(c => c.id === (p?.chapter_id || id));
  if (!chapter || (!['empty','rename_chapter'].includes(op) && !p)) return;
  const index = p ? state.text.paragraphs.indexOf(p) : -1;
  const previous = state.text.paragraphs[index-1];
  const after = op === 'after' ? p.id : previous?.chapter_id === chapter.id ? previous.id : null;
  paragraphEditor = {op, id, chapter_id:chapter.id, after_id:after};
  $('paragraphDialogTitle').textContent = ({edit:'修改段落',before:'在前面添加段落',after:'在后面添加段落',delete:'删除段落',empty:'添加段落',rename_chapter:'修改章节标题'})[op];
  const attached = state.settings.images.filter(i => i.role === 'body' && i.after_id === id).length;
  $('paragraphDialogContext').textContent = op === 'rename_chapter' ? '同步更新正文标题、目录和插图位置说明。' : chapter.title + (op === 'delete' ? '\n'+(p.text.trim() ? p.text : '（空段落）')+'\n删除后可撤销。'+(attached ? attached+' 张插图将清除位置，需要重新选择。' : '') : '\n修改后，本章插图需要重新确认。');
  $('paragraphText').hidden = $('paragraphTextLabel').hidden = op === 'delete';
  $('paragraphTextLabel').textContent = op === 'rename_chapter' ? '章节标题（不能为空，最多 300 字）' : '正文（可留空）';
  $('paragraphText').required = op === 'rename_chapter';
  $('paragraphText').maxLength = op === 'rename_chapter' ? 300 : 20000;
  $('paragraphText').rows = op === 'rename_chapter' ? 3 : 9;
  $('paragraphText').placeholder = op === 'rename_chapter' ? '输入新的章节标题' : '留空保存为空段落；删除整段请使用删除按钮。';
  $('paragraphText').value = op === 'rename_chapter' ? chapter.title : op === 'edit' ? p.text : '';
  $('paragraphEditStatus').textContent = '';
  $('saveParagraphEdit').textContent = op === 'rename_chapter' ? '保存标题' : op === 'delete' ? '删除段落' : '保存段落';
  $('paragraphDialog').showModal();
  (op === 'delete' ? $('cancelParagraphEdit') : $('paragraphText')).focus();
}
$('cancelParagraphEdit').addEventListener('click', () => { if (!busy) $('paragraphDialog').close(); });
$('paragraphDialog').addEventListener('cancel', e => { if (busy) e.preventDefault(); });
$('paragraphForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !paragraphEditor) return;
  const context = paragraphEditor, renaming = context.op === 'rename_chapter';
  const text = renaming ? $('paragraphText').value.trim() : $('paragraphText').value;
  if (renaming && !text) { $('paragraphEditStatus').textContent = '章节标题不能为空。'; return; }
  if (['edit','rename_chapter'].includes(context.op) && text === editorOriginalText()) { $('paragraphDialog').close(); return; }
  const added = !['edit','delete','rename_chapter'].includes(context.op);
  const edit = added ? {op:'add',id:'u_'+crypto.randomUUID(),chapter_id:context.chapter_id,after_id:context.after_id,text} : {op:context.op,id:context.id,...(context.op !== 'delete' ? {text} : {})};
  const edits = [...(state.settings.text_edits || []), edit];
  setBusy(true); $('saveParagraphEdit').disabled = $('cancelParagraphEdit').disabled = true;
  $('paragraphEditStatus').textContent = renaming ? '正在保存章节标题…' : '正在保存段落…';
  try {
    const next = await previewEdits(edits);
    editHistory.push({edits:structuredClone(state.settings.text_edits || []), images:structuredClone(state.settings.images), kind:context.op, focus:renaming ? state.text.paragraphs.find(p => p.chapter_id === context.chapter_id)?.id : context.id, chapter:context.chapter_id});
    if (editHistory.length > 30) editHistory.shift();
    for (const image of renaming ? [] : state.settings.images) {
      if (context.op === 'delete' && image.after_id === context.id) { image.after_id = null; if (image.role === 'body') image.confirmed = false; }
      else if (image.role === 'body' && (state.text.paragraphs.find(p => p.id === image.after_id)?.chapter_id === context.chapter_id || [chapterStart(context.chapter_id),chapterEnd(context.chapter_id)].includes(image.after_id) || (image.after_id === START && context.chapter_id === state.text.chapters[0].id))) image.confirmed = false;
    }
    state.text = next; state.settings.text_edits = edits;
    viewPage = null; renderSections(); $('chapterSelect').value = context.chapter_id;
    const index = next.paragraphs.findIndex(p => p.id === edit.id);
    if (index >= 0) viewStart = Math.max(0,index-1);
    else viewStart = Math.max(0,Math.min(viewStart,next.paragraphs.length-1));
    $('searchResults').hidden = true;
    $('paragraphDialog').close(); renderAll(false);
    toast(renaming ? '章节标题已保存，并同步目录；保存校对项目可保留修改。' : context.op === 'delete' ? '段落已删除；本章插图请重新确认，原本跟随此段的插图需重新定位。' : '段落已保存；本章插图请重新确认。保存校对项目可保留修改。');
  } catch(error) { $('paragraphEditStatus').textContent = error.message; }
  finally { setBusy(false); $('saveParagraphEdit').disabled = $('cancelParagraphEdit').disabled = false; }
});
$('undoTextEdit').addEventListener('click', async () => {
  if (busy || !editHistory.length) return;
  const previous = editHistory.at(-1);
  setBusy(true);
  try {
    const next = await previewEdits(previous.edits);
    editHistory.pop(); state.text = next;
    const currentCover = state.settings.images.find(x => x.role === 'cover')?.id;
    state.settings.text_edits = previous.edits;
    if (previous.kind !== 'rename_chapter') {
      const currentImages = new Map(state.settings.images.map(image => [image.id, image]));
      state.settings.images = state.donor.illustrations.map(image => {
        const current = currentImages.get(image.id);
        const restored = previous.images.find(x => x.id === image.id) || current;
        // Rotation is an image edit, not part of the text edit being undone.
        return {...restored, rotation: current.rotation};
      });
    }
    const covers = state.settings.images.filter(x => x.role === 'cover');
    if (covers.length > 1) for (const image of covers) if (image.id !== currentCover) {image.role = 'front'; image.confirmed = true;}
    viewPage = null; renderSections(); $('chapterSelect').value = previous.chapter;
    viewStart = Math.max(0,next.paragraphs.findIndex(p => p.id === previous.focus));
    $('searchResults').hidden = true; renderAll(false);
    toast(previous.kind === 'rename_chapter' ? '已撤销最近一次章节标题修改。' : '已撤销最近一次段落修改，并恢复当时的插图位置。');
  } catch(error) { toast(error.message); }
  finally { setBusy(false); }
});


async function previewImages(added) {
  const body = JSON.stringify({session:state.session, added_images:added});
  if (new Blob([body]).size > MAX_PROJECT) throw new Error('新增图片数据过大。');
  return (await request('/api/images/preview', {method:'POST', headers:{'Content-Type':'application/json'}, body})).json();
}
function readImageData(file) {
  return new Promise((resolve,reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.slice(reader.result.indexOf(',') + 1));
    reader.onerror = () => reject(new Error('无法读取图片：' + file.name));
    reader.readAsDataURL(file);
  });
}
$('addImages').addEventListener('click', () => {if (!busy && state) $('localImages').click();});
$('localImages').addEventListener('change', async event => {
  const files = Array.from(event.target.files || []);
  if (!files.length || !state || busy) return;
  setBusy(true);
  try {
    if (files.some(f => !f.size || f.size > 20*1024*1024)) throw new Error('图片不能为空，单张最多 20 MB。');
    const existing = state.settings.added_images || [];
    const oldSize = existing.reduce((sum,x) => sum + Math.floor(x.data.length*3/4),0);
    if (oldSize + files.reduce((sum,f)=>sum+f.size,0) > 40*1024*1024) throw new Error('新增图片合计最多 40 MB。');
    if (files.length + state.donor.illustrations.length > 400) throw new Error('图片总数最多 400 张。');
    const added = [];
    for (const file of files) {
      try { const decoded = await createImageBitmap(file); decoded.close(); }
      catch (_) { throw new Error('无法读取图片：'+file.name+'，请使用有效的 JPEG、PNG、GIF 或 WebP。'); }
      const data = await readImageData(file);
      const sha256 = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer())), b => b.toString(16).padStart(2,'0')).join('');
      added.push({id:'local_'+crypto.randomUUID(), filename:file.name, data, sha256});
    }
    const all = [...existing, ...added];
    const nextDonor = await previewImages(all);
    const anchor = decisionAt().role === 'body' && currentLocations().has(decisionAt().after_id) ? decisionAt().after_id : null;
    const nextSettings = {...state.settings, added_images:all, image_order:[...state.settings.image_order, ...added.map(x=>x.id)], images:[...state.settings.images, ...added.map(x=>({id:x.id,role:'body',after_id:anchor,confirmed:false,rotation:0}))]};
    if (new Blob([JSON.stringify(nextSettings, null, 2)]).size > MAX_PROJECT) throw new Error('项目超过 64 MB，请减少新增图片。');
    sortImageList(nextDonor, nextSettings.image_order);
    const firstNew = nextDonor.illustrations.findIndex(x=>x.id===added[0].id);
    state.donor = nextDonor; state.settings = nextSettings; active = firstNew;
    renderAll(true); toast('已添加 '+added.length+' 张图片，请选择用途、核对位置并确认。保存项目会包含这些原图。');
  } catch(error) {toast('添加失败：'+error.message);}
  finally {event.target.value='';setBusy(false);}
});
$('removeLocalImage').addEventListener('click', () => {
  if (busy || !state || !imageAt().local) return;
  const id = imageAt().id;
  state.settings.added_images = state.settings.added_images.filter(x=>x.id!==id);
  state.settings.images = state.settings.images.filter(x=>x.id!==id);
  state.settings.image_order = state.settings.image_order.filter(x=>x!==id);
  state.donor.illustrations = state.donor.illustrations.filter(x=>x.id!==id);
  clearLocalImageURLs(); active = Math.min(active,state.donor.illustrations.length-1);
  renderAll(true); toast('已移除新增图片；原插图仍可通过「忽略」排除。');
});


function validateImageOrder(donor, order) {
  const ids = donor.illustrations.map(x=>x.id);
  if (order === undefined) return ids;
  if (!Array.isArray(order) || order.length !== ids.length || order.some(id=>typeof id!=='string' || !ids.includes(id)) || new Set(order).size!==ids.length) throw new Error('图片排序无效，所有图片须各出现一次。');
  return [...order];
}
function sortImageList(donor, order) {
  const rank = new Map(order.map((id,i)=>[id,i]));
  donor.illustrations.sort((a,b)=>rank.get(a.id)-rank.get(b.id));
}
function imageGroup(decision, locations) {
  if (decision.role === 'front') return 'front';
  if (decision.role === 'body' && locations.has(decision.after_id)) return 'body:'+(decision.after_id === START && state.text.layout !== 'mojimoon' ? START : canonicalAnchor(decision.after_id));
  return null;
}
function groupedImages() {
  const locations=currentLocations(), group = imageGroup(decisionAt(), locations);
  return group ? state.donor.illustrations.filter(image=>imageGroup(state.settings.images.find(x=>x.id===image.id), locations)===group) : [];
}
function renderImageOrder() {
  const images = groupedImages();
  $('imageOrderPanel').hidden = !images.length;
  if (!images.length) return;
  $('imageOrderTitle').textContent = decisionAt().role === 'front' ? '卷首图顺序' : '同一位置的图片顺序';
  $('imageOrderHint').textContent = images.length > 1 ? '从上到下依次插入。上移、下移可交叉排列原插图与新增图片。' : '这里目前只有一张图片；将其他图片放在同一位置后即可排序。';
  $('imageOrderList').innerHTML = images.map((image,i)=>{
    const name = image.local ? state.settings.added_images.find(x=>x.id===image.id)?.filename : image.source_path.split('/').at(-1);
    return '<li class="'+(image.id===imageAt().id?'current':'')+'"><button class="order-preview" data-order-select="'+image.id+'" aria-label="查看第 '+(i+1)+' 张：'+escapeHTML(name)+'">'+imagePreviewHTML(image, state.settings.images.find(x=>x.id===image.id), '')+'<span><b>'+(i+1)+'</b> '+escapeHTML(name)+'</span></button><div class="order-actions"><button class="quiet" data-order-move="'+image.id+'" data-direction="-1" '+(i===0?'disabled':'')+' aria-label="上移第 '+(i+1)+' 张">↑ 上移</button><button class="quiet" data-order-move="'+image.id+'" data-direction="1" '+(i===images.length-1?'disabled':'')+' aria-label="下移第 '+(i+1)+' 张">↓ 下移</button></div></li>';
  }).join('');
  refreshImagePreviews($('imageOrderList'));
}
$('imageOrderList').addEventListener('click', event=>{
  if (busy) return;
  const select = event.target.closest('[data-order-select]');
  if (select) {active=state.donor.illustrations.findIndex(x=>x.id===select.dataset.orderSelect);renderAll(false);return;}
  const button=event.target.closest('[data-order-move]');
  if (!button || button.disabled) return;
  const images=groupedImages(), from=images.findIndex(x=>x.id===button.dataset.orderMove), to=from+Number(button.dataset.direction);
  if (from<0 || to<0 || to>=images.length) return;
  const selected=imageAt().id, order=[...state.settings.image_order];
  const a=order.indexOf(images[from].id), b=order.indexOf(images[to].id);
  [order[a],order[b]]=[order[b],order[a]];
  state.settings.image_order=order;sortImageList(state.donor,order);
  active=state.donor.illustrations.findIndex(x=>x.id===selected);
  renderAll(false);
  $('imageOrderList').querySelector('[data-order-move="'+button.dataset.orderMove+'"][data-direction="'+button.dataset.direction+'"]')?.focus();
  toast('图片顺序已调整；保存项目和导出 EPUB 都会保留。');
});

function useGeneratedFile(file) {
  generatedFile = file;
  $('textFile').value = ''; $('textName').textContent = file.name;
  $('downloadText').hidden = false;
}

async function generateFromWenku(format = 'epub') {
  if (busy || generating) return;
  const aid = $('wenkuAid').value.trim(), volume = $('wenkuVolume').value, vid = $('wenkuVid').value.trim();
  if (!aid || !/^\d+$/.test(volume) || Number(volume) < 1 || Number(volume) > 2000 || (vid && !/^\d+$/.test(vid))) {
    $('generateStatus').textContent = '请填写小说 ID／链接和有效的目录序号；分卷 ID 可以留空。'; return;
  }
  generating = true; setBusy(false);
  $('generateLog').hidden = false; $('generateLog').textContent = '';
  $('generateStatus').textContent = format === 'txt' ? '正在下载分卷 TXT，保留原始编码…' : '正在下载文库分卷 TXT，并使用 mojimoon 排版生成无图 EPUB…';
  try {
    const response = await request('/api/wenku/build', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({aid,volume,vid,scraper:$('wenkuScraper').value,format})});
    const {job} = await response.json();
    let status;
    while (true) {
      status = await (await request('/api/wenku/jobs/' + encodeURIComponent(job))).json();
      $('generateLog').textContent = status.lines.join('\n');
      if (status.status === 'error') throw new Error(status.error || '文件准备失败。');
      if (status.status === 'done') break;
      await new Promise(resolve => setTimeout(resolve, 1200));
    }
    const fileResponse = await request('/api/wenku/jobs/' + encodeURIComponent(job) + '/' + format);
    const file = new File([await fileResponse.blob()], status.result.filename, {type:format === 'txt' ? 'text/plain' : 'application/epub+zip'});
    if (format === 'txt') {
      download(file, file.name);
      const transfer = new DataTransfer(); transfer.items.add(file); $('localTxtFile').files = transfer.files;
      $('localTxtFile').dispatchEvent(new Event('change'));
      $('txtTitle').value = status.result.title;
      $('txtAuthor').value = status.result.author || '';
      $('txtSeries').value = status.result.series || '';
      $('txtSeriesIndex').value = status.result.series_index || '';
      $('txtTags').value = (status.result.subjects || []).join(' ');
      $('txtDescription').value = status.result.description || '';
      $('txtEncoding').value = status.result.encoding === 'utf-16' ? 'utf-16' : 'utf-8';
      $('localTxtFile').closest('details').open = true;
      $('generateStatus').textContent = 'TXT 已保存，原始编码：' + status.result.encoding + '。也已填入下方，可直接生成无图 EPUB。';
      toast('TXT 已保存，保留下载时的原始字节。');
    } else {
      useGeneratedFile(file);
      $('generateStatus').textContent = '无图底稿已就绪：' + status.result.title + '。选择同卷插图源后，点击「开始匹配」。';
      toast('已生成 mojimoon 无图 EPUB，并填入中文正文。');
    }
  } catch (error) {
    $('generateStatus').textContent = error.message;
  } finally { generating = false; setBusy(false); }
}
$('generateText').addEventListener('click', () => generateFromWenku('epub'));
$('saveRawTxt').addEventListener('click', () => generateFromWenku('txt'));
$('localTxtFile').addEventListener('change', () => {
  const file = $('localTxtFile').files[0];
  $('localTxtName').textContent = file?.name || '尚未选择文件';
  $('txtTitle').value = file?.name.replace(/\.txt$/i,'') || '';
  for (const id of ['txtAuthor','txtSeries','txtSeriesIndex','txtTags','txtDescription','txtVolumeTitle']) $(id).value = '';
  $('txtEncoding').value = 'auto'; $('txtConvertStatus').textContent = '';
});
$('convertTxt').addEventListener('click', async () => {
  if (busy || generating) return;
  const file = $('localTxtFile').files[0];
  if (!file || !/\.txt$/i.test(file.name) || file.size>80*1024*1024) {
    $('txtConvertStatus').textContent = '请选择 80 MB 以内的 TXT 文件。'; return;
  }
  const settings = {title:$('txtTitle').value.trim(),author:$('txtAuthor').value.trim(),
    encoding:$('txtEncoding').value,volume_title:$('txtVolumeTitle').value.trim(),
    description:$('txtDescription').value.trim(),series_index:$('txtSeriesIndex').value.trim()};
  if ($('txtSeries').value.trim()) settings.series = $('txtSeries').value.trim();
  if ($('txtTags').value.trim()) settings.subjects = [...new Set($('txtTags').value.split(/[\s,，、;；]+/).filter(Boolean))];
  const body = new FormData();body.append('text',file);body.append('settings',JSON.stringify(settings));
  generating=true;setBusy(false);$('txtConvertStatus').textContent='正在读取 TXT 并生成无图 EPUB…';
  try {
    const response=await request('/api/txt/build',{method:'POST',body});
    const encoded=response.headers.get('Content-Disposition')?.match(/filename\*=UTF-8''(.+)$/i)?.[1];
    const name=encoded?decodeURIComponent(encoded):safeName(settings.title)+'.epub';
    useGeneratedFile(new File([await response.blob()],name,{type:'application/epub+zip'}));
    $('txtConvertStatus').textContent='无图 EPUB 已生成，并填入下方中文正文。可下载底稿，或选择插图源继续匹配。';
    toast('TXT 已转换为 mojimoon 无图 EPUB。');
  } catch(error) { $('txtConvertStatus').textContent=error.message; }
  finally {generating=false;setBusy(false);}
});
$('downloadText').addEventListener('click', () => {
  if (generatedFile) download(generatedFile, generatedFile.name);
});

function renderMetadata() {
  const settings = state.settings;
  $('bookTitle').value = settings.title;
  $('bookAuthor').value = settings.author;
  $('bookSeries').value = settings.series || '';
  $('bookSeriesIndex').value = settings.series_index || '';
  $('bookTags').value = (settings.subjects || []).join(' ');
  $('bookDescription').value = settings.description || '';
  $('descriptionSource').textContent = settings.description_source ? '来源：' + settings.description_source : '可自行填写或修改';
}
for (const [id,key] of [['bookSeries','series'],['bookSeriesIndex','series_index'],['bookDescription','description']]) {
  $(id).addEventListener('input', () => {
    state.settings[key] = $(id).value.trim();
    if (key === 'description') {state.settings.description_source = '手动编辑'; $('descriptionSource').textContent = '来源：手动编辑';}
  });
}
$('bookTags').addEventListener('input', () => {state.settings.subjects = [...new Set($('bookTags').value.split(/[\s,，、;；]+/).filter(Boolean))];});
