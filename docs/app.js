/* 税務六法 Web ─ ビューア
 *
 * データは docs/data/ の階層JSON（scripts/parse_laws.py が e-Gov 全文JSONから生成）。
 * 読み込みは3段階：index.json → {law}/toc.json → {law}/main-NN.json（開いた条の1本だけ）。
 * 附則は全体の約半分を占めるため、タブを開くまで一切読まない。
 *
 * 出典：e-Gov法令検索（デジタル庁）／政府標準利用規約2.0
 */
'use strict';

const DATA = 'data';
const LS = { mode: 'zeiho.kakkoMode', size: 'zeiho.fontSize' };

let INDEX = null;
const tocCache = new Map();    // lawKey -> toc.json
const chunkCache = new Map();  // "lawKey/3" -> [article]
const supCache = new Map();    // "lawKey/2" -> [suppl]

let state = { law: null, art: null, view: 'home', sup: null };
let kakkoMode = localStorage.getItem(LS.mode) || 'mcolor';
let fontSize = +localStorage.getItem(LS.size) || 16.5;

const $ = (s) => document.querySelector(s);
const nav = $('#nav'), main = $('#main'), scrim = $('#scrim');

/* ─────────────────────────────── ユーティリティ */

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

async function getJSON(path) {
  const r = await fetch(`${DATA}/${path}`, { cache: 'force-cache' });
  if (!r.ok) throw new Error(`${path} が読み込めませんでした（${r.status}）`);
  return r.json();
}

async function getToc(key) {
  if (!tocCache.has(key)) {
    const t = await getJSON(`${key}/toc.json`);
    t._flat = flattenToc(t.toc);              // 条の一覧（ジャンプ・検索・前後移動に使う）
    t._byId = new Map(t._flat.map((a) => [a.id, a]));
    tocCache.set(key, t);
  }
  return tocCache.get(key);
}

async function getChunk(key, f) {
  const ck = `${key}/${f}`;
  if (!chunkCache.has(ck)) {
    const d = await getJSON(`${key}/main-${String(f).padStart(2, '0')}.json`);
    chunkCache.set(ck, d.arts);
  }
  return chunkCache.get(ck);
}

async function getArticle(key, id) {
  const toc = await getToc(key);
  const meta = toc._byId.get(id);
  if (!meta) return null;
  const arts = await getChunk(key, meta.f);
  return arts.find((a) => a.id === id) || null;
}

function flattenToc(nodes, out = [], path = []) {
  for (const n of nodes) {
    if (n.k === '条') out.push({ id: n.id, l: n.l, t: n.t, cap: n.cap, capd: n.capd, f: n.f, path });
    else if (n.c) flattenToc(n.c, out, path.concat(n.t));
  }
  return out;
}

/* 短い条番号ラベル（"22の2"）を条文の書き方（"第22条の2"）に直す。
 * 枝番は「第22条の2」であって「第22の2条」ではない。範囲条（"4〜7"＝第四条から
 * 第七条まで）は両端をそれぞれ条にする。 */
function joDisp(label) {
  return String(label).split('〜').map((x) => {
    const [head, ...branch] = x.split('の');
    return `第${head}条` + (branch.length ? 'の' + branch.join('の') : '');
  }).join('〜');
}

const lawOf = (key) => INDEX.laws.find((l) => l.key === key);
const groupOf = (key) => INDEX.groups.find((g) => g.key === lawOf(key).group);
const colorOf = (key) => groupOf(key).color;

/* 通達は法令ではない。番号の見せ方も出典も違うので、ここで一度だけ分岐する。
 * 通達の項目番号「36-1」は条ではないため joDisp を通さずそのまま出す。 */
const isTsu = (key) => (lawOf(key) || {}).kind === 'tsutatsu';
const numDisp = (key, label) => (isTsu(key) ? String(label) : joDisp(label));
/* 1つの法令に通達が複数ぶら下がることがある（租税特別措置法は所得税・源泉・株式・
 * 山林譲渡の4本）。tsuOf は代表（＝最初の1本）、tsusOf は全部。 */
const tsusOf = (groupKey) => INDEX.laws.filter((l) => l.group === groupKey && l.kind === 'tsutatsu');
const tsuOf = (groupKey) => tsusOf(groupKey)[0];

/* ─────────────────────────────── 括弧書きの色分け（指示書9）
 *
 * 「（」「）」の対応から入れ子の深さを判定し、((depth-1)%5)+1 の階層色で包む。
 *
 * 実データ対応が2点必要だった（モックの colorize には無い）:
 *  1) 読替規定「〜とあるのは「純損失の金額（」と〜」は、他法令の条文の"断片"を
 *     鉤括弧で引用するため、括弧が開いたまま／閉じたままになる。素朴に数えると
 *     深さが漏れて以降の条文が総崩れになる（実測308か所）。
 *     → 「」『』を深さの障壁とし、鉤括弧を出るときに内側の spanを全て閉じる。
 *  2) 対応する開き括弧の無い「）」は span を閉じずに地の文として出す
 *     （閉じ漏れの自動補完＝指示書9-6）。
 */
function colorize(t) {
  let out = '';
  const stack = [];       // 開いている span の数
  const floors = [0];     // 鉤括弧に入った時点の深さ＝そこより下は閉じない
  for (const ch of t) {
    if (ch === '「' || ch === '『') {
      floors.push(stack.length);
      out += ch;
    } else if (ch === '」' || ch === '』') {
      if (floors.length > 1) {
        const f = floors.pop();
        while (stack.length > f) { stack.pop(); out += '</span>'; }
      }
      out += ch;
    } else if (ch === '（') {
      stack.push(1);
      out += `<span class="p${((stack.length - 1) % 5) + 1}">（`;
    } else if (ch === '）') {
      if (stack.length > floors[floors.length - 1]) { stack.pop(); out += '）</span>'; }
      else out += '）';   // 開き括弧の無い断片（読替規定に実在する）
    } else {
      out += esc(ch);
    }
  }
  while (stack.length) { stack.pop(); out += '</span>'; }
  return out;
}

/* 検索語のハイライト。colorize 済みHTMLには使わない（タグを壊すため）。 */
function hl(text, q) {
  if (!q) return esc(text);
  const i = text.indexOf(q);
  if (i < 0) return esc(text);
  return esc(text.slice(0, i)) + '<mark>' + esc(text.slice(i, i + q.length)) + '</mark>' + esc(text.slice(i + q.length));
}

/* ─────────────────────────────── 条文本文の組み立て */

function renderItems(items, level) {
  return items.map((it) => {
    let h = '';
    if (it.cols) {
      // 定義規定（法法2等）の二段組み
      h += `<p class="go l${level}"><span class="gonum">${esc(it.n)}</span><span class="cols">` +
        it.cols.map((c, i) => `<span class="c${i + 1}">${colorize(c)}</span>`).join('') + '</span></p>';
    } else {
      h += `<p class="go l${level}"><span class="gonum">${esc(it.n)}</span>${colorize(it.t)}</p>`;
    }
    if (it.blocks) h += renderBlocks(it.blocks);
    if (it.items) h += renderItems(it.items, level + 1);
    return h;
  }).join('');
}

function renderBlocks(blocks) {
  return blocks.map((b) => {
    if (b.kind === 'list') return `<p class="go l1">${colorize(b.t)}</p>`;
    // 通達の算式・図表（国税庁が画像で提供しているもの）。法令側は fig を出さない。
    // alt には算式が文章で書かれているので、画像が出ないときの代わりにも検索にも使える。
    if (b.kind === 'fig') {
      if (!b.src) return `<p class="figAlt">${esc(b.alt || '（図表）')}</p>`;
      return `<figure class="fig"><img src="${DATA}/${state.law}/${esc(b.src)}" alt="${esc(b.alt)}" loading="lazy">
        <figcaption>${esc(b.alt)}</figcaption></figure>`;
    }
    if (b.kind !== 'table') return '';
    let h = '<div class="tbl">';
    if (b.title) h = `<p class="tblTitle">${esc(b.title)}</p>` + h;
    h += '<table>' + (b.rows || []).map((r) =>
      '<tr>' + r.map((c) =>
        `<td${c.rs ? ` rowspan="${c.rs}"` : ''}${c.cs ? ` colspan="${c.cs}"` : ''}>${colorize(c.t)}</td>`
      ).join('') + '</tr>'
    ).join('') + '</table></div>';
    if (b.remarks) h += b.remarks.map((r) => `<p class="tblNote">${esc(r)}</p>`).join('');
    return h;
  }).join('');
}

function renderParas(paras) {
  return paras.map((p) => {
    let h = '';
    if (p.cap) h += `<p class="kocap">${esc(p.cap)}</p>`;
    // 第1項の項番号は原文（ParagraphNum）でも空。印刷六法と同じくバッジを出さない。
    const badge = p.n !== '1' ? `<span class="konum">${esc(p.n)}</span>` : '';
    h += `<p class="ko">${badge}${colorize(p.t)}</p>`;
    if (p.blocks) h += renderBlocks(p.blocks);
    if (p.items) h += renderItems(p.items, 1);
    return h;
  }).join('');
}

/* ─────────────────────────────── 画面：初期メニュー */

/* 財産評価基本通達だけは独立したカードを立てる。相続税法の下にぶら下がる通達だが、
 * 実務では単独で引くことが多く、Phase 1 のトップにも専用カードが置かれていた。 */
function hyokaCard() {
  const h = INDEX.laws.find((l) => l.key === 'hyoka_tsutatsu');
  if (!h) return '';
  return `<div class="lawCard" style="--c:var(--tsu)">
    <div class="cardTop">
      <div class="medal tsu">${esc(h.abbr)}</div>
      <div class="cardName">${esc(h.name)}<small>相続税・贈与税の財産評価／${h.articles}項目</small></div>
    </div>
    <div class="cardBtns">
      <button class="cBtn tsu" data-open="${h.key}">通達<span class="p2">${esc(h.abbr)}</span></button>
      <button class="cBtn" disabled>─</button><button class="cBtn" disabled>─</button><button class="cBtn" disabled>─</button>
    </div>
  </div>`;
}

function renderHome() {
  state = { law: null, art: null, view: 'home', sup: null };
  nav.style.display = 'none';
  closeDrawer();
  const cards = INDEX.groups.map((g) => {
    const laws = INDEX.laws.filter((l) => l.group === g.key);
    const byKind = (k) => laws.find((l) => l.kind === k);
    const btn = (l, cls, label) => l
      ? `<button class="cBtn ${cls}" data-open="${l.key}">${label}</button>`
      : `<button class="cBtn" disabled>${label}</button>`;
    // 通達は条ではなく項目なので「本則○条」には数えない
    const total = laws.filter((l) => l.kind !== 'tsutatsu').reduce((s, l) => s + l.articles, 0);
    const tsus = tsusOf(g.key);
    const tsu = tsus[0];
    const tsuItems = tsus.reduce((s, l) => s + l.articles, 0);
    const tsuBtn = tsu
      ? `<button class="cBtn tsu" data-open="${tsu.key}" title="${esc(tsus.map((t) => t.name).join('／'))}">通達<span class="p2">${
        tsus.length > 1 ? `${esc(tsu.abbr)}ほか${tsus.length - 1}` : esc(tsu.abbr)}</span></button>`
      : '<button class="cBtn" disabled>通達<span class="p2">未収録</span></button>';
    return `<div class="lawCard" style="--c:${g.color}">
      <div class="cardTop">
        <div class="medal">${esc(g.abbr)}</div>
        <div class="cardName">${esc(g.name)}<small>施行令・施行規則つき／本則${total}条${tsu ? `／通達${tsuItems}項目` : ''}</small></div>
      </div>
      <div class="cardBtns">
        ${btn(byKind('act'), 'main', '法律')}
        ${btn(byKind('cabinet_order'), '', '施行令')}
        ${btn(byKind('ministerial_ordinance'), '', '規則')}
        ${tsuBtn}
      </div>
    </div>`;
  }).join('');

  main.innerHTML = `
    <div class="home">
      <div class="homeTitle">
        <span class="big">税務六法</span>
        <span class="sub">国税法令集 Web</span>
      </div>
      <div class="cardGrid">${cards}${hyokaCard()}</div>
      <div class="homeFoot">
        カードの「法律」「施行令」「規則」「通達」を押すと目次ツリー＋本文画面が開きます。<br>
        ヘッダーの条番号ジャンプに <b>法法22</b> ／ <b>所法56</b> ／ <b>措法66の6</b> ／ <b>所基通36-1</b> のように入力すると直行します。<br>
        出典：e-Gov法令検索（デジタル庁）　法令データは加工して表示しています。正確な法令は原文・官報で確認してください。<br>
        ${tsuOf('shotoku') ? '出典：国税庁ホームページ 法令解釈通達を加工して表示。正確な内容は原文を確認してください。' : ''}
      </div>
    </div>`;

  main.querySelectorAll('.cBtn[data-open]').forEach((b) => {
    b.onclick = () => openLaw(b.dataset.open);
  });
}

/* ─────────────────────────────── 画面：目次 */

let navSearchQ = '';
let fullTextHits = null;   // 本文検索の結果（条id の Set）。null = 未実行

function tocTreeHtml(nodes, cur) {
  const KIND = { '編': 'hen', '章': 'sho', '節': 'setsu', '款': 'kan', '目': 'moku', '関係': 'kankei', '群': 'gun' };
  return nodes.map((n) => {
    if (n.k === '条') {
      const cap = n.cap ? `<span class="cap${n.capd ? ' d' : ''}">${hl(n.cap, navSearchQ)}</span>` : '';
      return `<div class="jo${n.id === cur ? ' now' : ''}" data-id="${n.id}">
        <span class="num">${esc(numDisp(state.law, n.l))}</span>${cap}</div>`;
    }
    const cls = KIND[n.k] || 'setsu';
    return `<div class="grp"><div class="lbl ${cls}">${esc(n.t)}</div>
      <div class="kids">${tocTreeHtml(n.c || [], cur)}</div></div>`;
  }).join('');
}

/* 検索で絞った木を作る（該当条だけ残し、空の階層は落とす） */
function filterToc(nodes, keep) {
  const out = [];
  for (const n of nodes) {
    if (n.k === '条') { if (keep.has(n.id)) out.push(n); }
    else {
      const c = filterToc(n.c || [], keep);
      if (c.length) out.push({ ...n, c });
    }
  }
  return out;
}

/* 検索結果の見出し＋目次ツリー。
 *
 * 検索のたびに nav 全体を作り直すと、検索窓の <input> そのものが作り直される。
 * すると iPad（iOS Safari）では日本語入力が変換の途中で切られてしまい、
 * そもそも日本語を打てない。ここだけを差し替えられるように切り出してある。 */
function tocAreaHtml(key, toc) {
  const g = groupOf(key);
  let tree, note = '';
  if (navSearchQ || fullTextHits) {
    const q = navSearchQ;
    const keep = new Set();
    for (const a of toc._flat) {
      const inMidashi = q && ((a.cap || '').includes(q) || numDisp(key, a.l).includes(q) || a.l === q);
      if (inMidashi || (fullTextHits && fullTextHits.has(a.id))) keep.add(a.id);
    }
    const unit = isTsu(key) ? '項目' : '条';
    tree = tocTreeHtml(filterToc(toc.toc, keep), state.art);
    note = `<div class="srchHit">「${esc(q)}」に一致：${keep.size}${unit}` +
      (fullTextHits ? '（本文を含む）' : `（${isTsu(key) ? '項目番号' : '条番号'}・見出し）`) + '</div>';
    if (!keep.size) tree = `<div class="none">一致する${unit}がありません。</div>`;
  } else {
    tree = tocTreeHtml(toc.toc, state.art);
  }
  return note + `<div class="toc" style="--c:${g.color}">${tree}</div>`;
}

function bindToc() {
  nav.querySelectorAll('.jo').forEach((d) => {
    d.onclick = () => { goto(state.law, d.dataset.id); closeDrawer(); };
  });
  nav.querySelectorAll('.grp > .lbl').forEach((l) => {
    l.onclick = () => l.parentElement.classList.toggle('closed');
  });
}

/* 検索窓・タブはそのままに、目次ツリーだけ描き直す。
 * <input> に触らないので、入力中のカーソル位置も変換中の文字も保たれる。 */
async function updateToc() {
  const toc = await getToc(state.law);
  const area = $('#tocArea');
  if (!area) return renderNav();
  area.innerHTML = tocAreaHtml(state.law, toc);
  bindToc();
}

async function renderNav() {
  const key = state.law, toc = await getToc(key), law = lawOf(key), g = groupOf(key);
  const sibs = INDEX.laws.filter((l) => l.group === g.key);
  const tab = (kind, label) => {
    const l = sibs.find((s) => s.kind === kind);
    if (!l) return `<button disabled>${label}</button>`;
    return `<button class="${l.key === key ? 'on' : ''}" data-open="${l.key}">${label}</button>`;
  };

  const secBtn = (v, label, on) => on
    ? `<button class="${state.view === v ? 'on' : ''}" data-sec="${v}">${label}</button>`
    : `<button disabled>${label}</button>`;

  const tsus = tsusOf(g.key);
  const tsu = tsus[0];
  const tsuTab = tsu
    ? `<button class="${isTsu(key) ? 'on' : ''} tsuTab" data-open="${tsu.key}">通達</button>`
    : '<button disabled title="未収録">通達</button>';
  // 通達が複数ある法令（措置法は4本）は、通達を開いたときだけ切り替えの列を出す
  const tsuRow = (tsus.length > 1 && isTsu(key))
    ? `<div class="navTabs tsuRow" style="--c:${g.color};margin-top:6px;">
        ${tsus.map((t) => `<button class="${t.key === key ? 'on' : ''} tsuTab" data-open="${t.key}"
          title="${esc(t.name)}">${esc(t.abbr)}</button>`).join('')}
      </div>` : '';
  const isT = isTsu(key);

  nav.style.display = '';
  nav.innerHTML = `
    <div class="navHead" style="--c:${g.color}">
      <div class="navHeadRow"><div class="medal${isT ? ' tsu' : ''}">${esc(law.abbr)}</div>
        <div class="nm">${esc(law.name)}<small>${esc(isT ? law.law_num : law.law_num)}</small></div></div>
      <div class="navTabs" style="--c:${g.color}">
        ${tab('act', '法律')}${tab('cabinet_order', '施行令')}${tab('ministerial_ordinance', '規則')}
        ${tsuTab}
      </div>
      ${tsuRow}
      <div class="navSearch" style="--c:${g.color}">
        <input id="nq" placeholder="${isT ? 'この通達内を検索' : 'この法令内を検索'}" value="${esc(navSearchQ)}" aria-label="法令内検索"
               autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false">
        <button id="ftBtn" title="本文の全文も検索します">本文も</button>
      </div>
      ${isT ? '' : `<div class="navTabs" style="--c:${g.color};margin-top:6px;">
        ${secBtn('art', '本則', true)}
        ${secBtn('suppl', `附則(${law.suppls})`, toc.has_suppl)}
        ${secBtn('appdx', `別表(${law.appdx})`, toc.has_appdx)}
      </div>`}
    </div>
    <div id="tocArea">${tocAreaHtml(key, toc)}</div>
    <button class="backHome">⌂ 法令メニューへ戻る</button>
    <div class="navNote">${isT
      ? `通達${law.articles}項目／附則${law.suppls}本。${esc(law.law_num)}。出典：国税庁ホームページ。`
      : `本則${law.articles}条／附則${law.suppls}本／別表${law.appdx}。
         施行日 ${esc(law.enforced || '－')}。出典：e-Gov法令検索。`}</div>`;

  bindToc();
  nav.querySelectorAll('[data-open]').forEach((b) => { b.onclick = () => openLaw(b.dataset.open); });
  nav.querySelectorAll('[data-sec]').forEach((b) => {
    b.onclick = () => {
      const v = b.dataset.sec;
      if (v === 'art') goto(state.law, state.art || tocCache.get(state.law)._flat[0].id);
      else location.hash = `#/${state.law}/${v}`;
    };
  });
  nav.querySelector('.backHome').onclick = () => { location.hash = '#/'; };

  /* 検索窓。IME（日本語入力）の変換中は検索しない。
   *
   * iPad で日本語が打てなかった原因はここ。変換の途中でも input が飛ぶので、
   * 素直に拾うと「にほんご」の1文字ごとに目次を描き直すことになり、
   * iOS Safari では変換が中断されて入力そのものが成立しなかった。
   * compositionstart〜compositionend の間は何もせず、変換が確定してから検索する。
   * 描き直すのは目次ツリーだけ（updateToc）で、<input> には触らない。 */
  const nq = $('#nq');
  let timer, composing = false;
  const search = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = nq.value.trim();
      if (q === navSearchQ) return;   // 変換確定で中身が変わっていなければ何もしない
      navSearchQ = q;
      fullTextHits = null;
      await updateToc();
    }, 200);
  };
  nq.addEventListener('compositionstart', () => { composing = true; clearTimeout(timer); });
  nq.addEventListener('compositionend', () => { composing = false; search(); });
  nq.addEventListener('input', (e) => {
    if (composing || e.isComposing) return;   // 変換中（未確定）は検索しない
    search();
  });
  // 変換候補を確定するEnterでは検索が走らないよう、確定後のEnterだけ拾う
  nq.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing && !composing) { clearTimeout(timer); search(); }
  });
  $('#ftBtn').onclick = () => runFullText();
  // 現在の条を目次内に見えるところへ
  const now = nav.querySelector('.jo.now');
  if (now) now.scrollIntoView({ block: 'center' });
}

/* 法令内の本文検索。本則の全チャンクを読む（最大でも措法の約4.7MB）。 */
async function runFullText() {
  // 変換確定を待たずに押されることがあるので、窓の中身を直に読む
  const q = ($('#nq').value || '').trim();
  navSearchQ = q;
  if (!q) { alert('検索語を入力してください。'); return; }
  const key = state.law, toc = await getToc(key);
  const btn = $('#ftBtn');
  btn.textContent = '検索中…'; btn.disabled = true;
  const hits = new Set();
  const hay = (a) => {
    let s = (a.cap || '') + a.title;
    for (const p of a.paras) {
      s += p.t;
      const walk = (items) => { for (const it of items) { s += it.n + it.t; if (it.items) walk(it.items); } };
      if (p.items) walk(p.items);
      if (p.blocks) for (const b of p.blocks) {
        if (b.kind === 'list') s += b.t;
        if (b.kind === 'fig') s += b.alt || '';   // 算式は alt に文章で入っている
        if (b.kind === 'table') for (const r of b.rows || []) for (const c of r) s += c.t;
      }
    }
    return s;
  };
  for (let f = 0; f < toc.chunks; f++) {
    for (const a of await getChunk(key, f)) if (hay(a).includes(q)) hits.add(a.id);
  }
  fullTextHits = hits;
  await updateToc();          // 検索窓は作り直さない（入力途中の文字を消さない）
  btn.textContent = '本文も'; btn.disabled = false;
}

/* ─────────────────────────────── 画面：条文 */

function egovUrl(key) {
  return `https://laws.e-gov.go.jp/law/${lawOf(key).law_id}`;
}

/* 通達の原文URL。ページ単位でしか辿れないので、その項目が載っているページへ送る。 */
function ntaUrl(key, art) {
  const base = (lawOf(key).source_url || '').replace(/\/[^/]*$/, '/');
  return art && art.page ? base + art.page : lawOf(key).source_url;
}

/* 通達の項目 → 関係する法令の条（36-1 → 所法36条）。逆は toc.rel が持つ。 */
function relLaw(key) {
  const parent = lawOf(key).parent;
  return parent ? lawOf(parent) : null;
}

/* 条文↔通達の相互リンク（指示書 Step3-4）。
 *
 * 対応表は通達側の toc.json が持つ（法条id → 項目idの配列）。項目番号そのものから
 * 作っているので、国税庁の目次のリンク誤りの影響を受けない。
 * 法令側から見るときだけ通達の toc.json を余分に読むが、開いた条にだけ効く。 */
async function refBarHtml(key, a) {
  const chips = [];
  if (isTsu(key)) {
    chips.push(`<a class="refChip now" href="${ntaUrl(key, a)}" target="_blank" rel="noopener">国税庁の原文で確認 ↗</a>`);
    const parent = relLaw(key);
    if (parent && a.ja) {
      const ptoc = await getToc(parent.key);
      for (const jo of a.ja) {
        const id = 'a' + jo.replace(/の/g, '_');
        if (ptoc._byId.has(id)) {
          chips.push(`<a class="refChip law" href="#/${parent.key}/${id}">${esc(parent.abbr)}${esc(joDisp(jo))} へ</a>`);
        }
      }
    }
  } else {
    chips.push(`<a class="refChip now" href="${egovUrl(key)}" target="_blank" rel="noopener">e-Gov原文で確認 ↗</a>`);
    // 通達が対応するのは法律本体だけ（施行令・規則には条番号の対応表が無い）。
    // さらに、項目番号が条を表す通達に限る。法基通の 1-1-1 は章-節-連番で条ではないため、
    // 条から通達を引く手がかりが無い（「該当項目なし」と出すと収録漏れに見えるので出さない）。
    const linkable = tsusOf(lawOf(key).group).filter((t) => t.parent === key && t.numbering === 'article');
    for (const tsu of linkable) {
      const ttoc = await getToc(tsu.key);
      const hits = (ttoc.rel || {})[a.id] || [];
      if (hits.length) {
        chips.push(`<a class="refChip tsu" href="#/${tsu.key}/${hits[0]}" title="${esc(tsu.name)}">${
          esc(tsu.abbr)} ${hits.length}項目 →</a>`);
      }
    }
    if (linkable.length && !chips.some((c) => c.includes('refChip tsu'))) {
      chips.push(`<span class="refChip tsu off">通達に該当項目なし</span>`);
    }
  }
  return chips.join('');
}

async function renderArticle() {
  const key = state.law, law = lawOf(key), g = groupOf(key);
  const toc = await getToc(key);
  const a = await getArticle(key, state.art);
  if (!a) { showError(`${law.name}に その条は見つかりませんでした。`); return; }

  const flat = toc._flat;
  const i = flat.findIndex((x) => x.id === a.id);
  const prev = flat[i - 1], next = flat[i + 1];

  const midashi = a.cap
    ? `<span class="midashi${a.capd ? ' d' : ''}"${a.capd ? ' title="この条には原文に見出しがありません。直近の款・目の名称を見出しとして表示しています。"' : ''}>${esc(a.cap)}</span>`
    : '';
  const isDeleted = a.paras.length === 1 && /^削除$/.test(a.paras[0].t.trim());

  const isT = isTsu(key);
  main.innerHTML = `
    <article style="--c:${g.color}">
      <div class="pankuzu"><span class="lawChip${isT ? ' tsu' : ''}">${esc(law.name)}</span>${a.path.map(esc).join('　›　')}</div>
      <div class="joHead">
        <div class="stamp${isT ? ' tsu' : ''}">${esc(law.abbr)}<br>${esc(a.label)}</div>
        <div class="joTitle">${midashi}<span class="jonum">${esc(isT ? a.title : a.title)}</span></div>
      </div>
      <div class="modeBar">
        <span class="lbl">カッコ書き：</span>
        <button data-m="mcolor">色分け</button>
        <button data-m="mplain">標準</button>
        <button data-m="musu">薄字（骨格読み）</button>
        <span class="sizer"><span class="lbl">文字</span>
          <button data-fs="-1" aria-label="文字を小さく">ｰ</button>
          <button data-fs="1" aria-label="文字を大きく">＋</button></span>
      </div>
      <div class="honbun ${kakkoMode === 'mplain' ? '' : kakkoMode}${isDeleted ? ' deleted' : ''}"
           style="font-size:${fontSize}px">${renderParas(a.paras)}</div>
      <div class="refBar">${await refBarHtml(key, a)}</div>
      <div class="navi">
        <button data-go="${prev ? prev.id : ''}" ${prev ? '' : 'disabled'}>‹ ${prev ? esc(numDisp(key, prev.l)) : ''}</button>
        <button data-go="${next ? next.id : ''}" ${next ? '' : 'disabled'}>${next ? esc(numDisp(key, next.l)) : ''} ›</button>
      </div>
      <div class="srcNote">
        カッコ色分け：${['青', '赤', '緑', '黄', '紫']
      .map((nm, i) => `<span class="sw sw${i + 1}">第${i + 1}階層・${nm}</span>`).join('／')}（6階層目からは循環）。
        括弧の外＝${isT ? '本文' : '条文'}の骨格は太字で表示します。「薄字」は括弧書きを淡色化して骨格だけを追う読み方用です。<br>
        ${isT
      ? `出典：<a href="${ntaUrl(key, a)}" target="_blank" rel="noopener">国税庁ホームページ 法令解釈通達（${esc(law.name)}）</a>を加工して表示。
         ${esc(law.law_num)}／データ取得 ${esc((INDEX.built_at || '').slice(0, 10))}。
         正確な内容は原文を確認してください。`
      : `出典：<a href="${egovUrl(key)}" target="_blank" rel="noopener">e-Gov法令検索（${esc(law.name)}）</a>を加工して作成。
         ${esc(law.law_num)}／施行日 ${esc(law.enforced || '－')}／データ取得 ${esc((INDEX.built_at || '').slice(0, 10))}。
         正確な法令は原文・官報で確認してください。`}
      </div>
    </article>`;

  main.querySelectorAll('.modeBar [data-m]').forEach((b) => {
    b.classList.toggle('on', b.dataset.m === kakkoMode);
    b.onclick = () => { kakkoMode = b.dataset.m; localStorage.setItem(LS.mode, kakkoMode); renderArticle(); };
  });
  main.querySelectorAll('[data-fs]').forEach((b) => {
    b.onclick = () => {
      fontSize = Math.min(24, Math.max(13, fontSize + (+b.dataset.fs)));
      localStorage.setItem(LS.size, fontSize);
      main.querySelector('.honbun').style.fontSize = fontSize + 'px';
    };
  });
  main.querySelectorAll('[data-go]').forEach((b) => {
    if (b.dataset.go) b.onclick = () => goto(state.law, b.dataset.go);
  });
  main.scrollTop = 0;
  document.title = `${law.abbr}${a.label} ${a.cap || ''}｜税務六法 Web`;
  main.querySelectorAll('.refChip[href^="#/"]').forEach((el) => {
    el.onclick = () => closeDrawer();
  });
}

/* ─────────────────────────────── 画面：附則・別表 */

async function renderSuppl() {
  const key = state.law, law = lawOf(key), g = groupOf(key);
  const idx = await getJSON(`${key}/suppl.json`);
  if (state.sup === null) {
    main.innerHTML = `<article style="--c:${g.color}">
      <div class="pankuzu"><span class="lawChip">${esc(law.name)}</span>附則</div>
      <div class="supHead">附則　全${idx.list.length}本</div>
      <div class="supMeta">改正法令ごとに分かれています。条番号は本則と重複するため、
        本則の条番号ジャンプの対象からは外しています。</div>
      <div class="appdxBody">${idx.list.map((s, i) =>
      `<div class="appdxItem"><h3><a href="#/${key}/suppl/${i}">${esc(s.label)}${s.extract ? '（抄）' : ''}</a></h3>
          <div class="rel">${esc(s.amend || '制定時')}／${s.n}か条・項</div></div>`).join('')}</div>
    </article>`;
    main.scrollTop = 0;
    return;
  }
  const meta = idx.list[state.sup];
  if (!meta) { showError('その附則は見つかりませんでした。'); return; }
  const ck = `${key}/${meta.f}`;
  if (!supCache.has(ck)) supCache.set(ck, (await getJSON(`${key}/suppl-${String(meta.f).padStart(2, '0')}.json`)).sups);
  const s = supCache.get(ck)[meta.i];
  const body = s.arts.map((a) =>
    `<div class="supArt">${esc(a.title)}${a.cap ? '　' + esc(a.cap) : ''}</div>
     <div class="honbun ${kakkoMode === 'mplain' ? '' : kakkoMode}" style="font-size:${fontSize}px">${renderParas(a.paras)}</div>`
  ).join('') + (s.paras.length
    ? `<div class="honbun ${kakkoMode === 'mplain' ? '' : kakkoMode}" style="font-size:${fontSize}px">${renderParas(s.paras)}</div>` : '');

  main.innerHTML = `<article style="--c:${g.color}">
    <div class="pankuzu"><span class="lawChip">${esc(law.name)}</span>
      <a href="#/${key}/suppl">附則</a>　›　${esc(meta.label)}</div>
    <div class="supHead">${esc(s.label)}${s.extract ? '（抄）' : ''}</div>
    <div class="supMeta">${esc(s.amend || '制定時')}</div>
    ${body || '<div class="supMeta">この附則に条文はありません。</div>'}
    ${(s.tables || []).map((t) => `<div class="appdxBody"><h3>${esc(t.title)}</h3>${renderBlocks([{ kind: 'table', ...t }])}</div>`).join('')}
    <div class="srcNote">出典：e-Gov法令検索（${esc(law.name)}）を加工して作成。</div>
  </article>`;
  main.scrollTop = 0;
}

async function renderAppdx() {
  const key = state.law, law = lawOf(key), g = groupOf(key);
  const d = await getJSON(`${key}/appdx.json`);
  main.innerHTML = `<article style="--c:${g.color}">
    <div class="pankuzu"><span class="lawChip">${esc(law.name)}</span>別表・様式</div>
    <div class="supHead">別表・様式　全${d.appdx.length}件</div>
    <div class="appdxBody">${d.appdx.map((x) => `
      <div class="appdxItem">
        <h3>${esc(x.title)}</h3>
        ${x.rel ? `<div class="rel">${esc(x.rel)}</div>` : ''}
        ${x.note ? `<div class="rel">${esc(x.note)}　<a href="${egovUrl(key)}" target="_blank" rel="noopener">e-Gov原文 ↗</a></div>` : ''}
        ${(x.tables || []).map((t) => renderBlocks([{ kind: 'table', ...t }])).join('')}
        ${x.items ? `<div class="honbun ${kakkoMode === 'mplain' ? '' : kakkoMode}" style="font-size:${fontSize}px">${renderItems(x.items, 1)}</div>` : ''}
      </div>`).join('')}</div>
    <div class="srcNote">出典：e-Gov法令検索（${esc(law.name)}）を加工して作成。</div>
  </article>`;
  main.scrollTop = 0;
}

function showError(msg) {
  main.innerHTML = `<div class="err">${esc(msg)}<br>
    <a href="#/">法令メニューへ戻る</a></div>`;
}

/* ─────────────────────────────── 条番号ジャンプ */

const Z2H = (s) => s.replace(/[０-９]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xFEE0));
const KANJI = { 〇: 0, 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 };

/* 「二十二」→22。条番号に出る範囲（〜千）だけを扱う。 */
function kanjiNum(s) {
  if (!/^[〇一二三四五六七八九十百千]+$/.test(s)) return null;
  let total = 0, cur = 0;
  for (const c of s) {
    if (c in KANJI) cur = cur * 10 + KANJI[c];
    else {
      const unit = { 十: 10, 百: 100, 千: 1000 }[c];
      total += (cur || 1) * unit; cur = 0;
    }
  }
  return total + cur;
}

/* 通達の項目番号「36-1」「36の2-1」「23～35共-1」→ DOM id。
 * scripts/parse_tsutatsu.py の item_id() と同じ規則。原文はダッシュを4種類
 * 混在させているので（－ - ― ー）、入力もその全部を受ける。 */
function tsuItemId(rest) {
  let s = Z2H(rest).replace(/[－―ー−‐–—]/g, '-');
  s = s.replace(/付表/g, 'f').replace(/の/g, '_').replace(/から|～/g, 'r').replace(/・/g, 'n').replace(/共/g, 'c');
  return /^[0-9A-Za-z_-]+$/.test(s) ? 't' + s : null;
}

/* 「法法22の2」「法人税法第22条の2」「所法56」→ {key, num}
 * 「所基通36-1」→ {key, id} */
function parseJump(raw) {
  let q = Z2H(raw.trim()).replace(/\s|　/g, '');
  if (!q) return null;

  // 略称 or 正式名称の長い順に前方一致（「所基通」が「所法」より先に当たるように）
  const cands = [];
  for (const l of INDEX.laws) { cands.push([l.abbr, l.key]); cands.push([l.name, l.key]); }
  cands.sort((a, b) => b[0].length - a[0].length);
  const hit = cands.find(([nm]) => q.startsWith(nm));
  if (!hit) return null;
  let rest = q.slice(hit[0].length);

  if (isTsu(hit[1])) {
    const id = tsuItemId(rest);
    return id ? { key: hit[1], id } : null;
  }

  rest = rest.replace(/^第/, '').replace(/条$/, '');
  // 「22条の2」「22の2」「二十二の二」いずれも受ける
  const parts = rest.split(/条?の/).filter((x) => x !== '');
  if (!parts.length) return null;
  const nums = parts.map((p) => (/^\d+$/.test(p) ? +p : kanjiNum(p)));
  if (nums.some((n) => n === null || Number.isNaN(n))) return null;
  return { key: hit[1], num: nums.join('_') };
}

/* ─────────────────────────────── 打てる略称の一覧
 *
 * 略称は全部で35個あり、覚えていないと条番号ジャンプが使えない。入力欄をタップしたら
 * 一覧を出し、押すとその略称が入った状態にする（続けて番号を打てばよい）。
 * 一覧は INDEX から組み立てるので、通達を足しても手直しは要らない。 */
function jumpHelpHtml() {
  const chip = (l, ex) =>
    `<button class="jhChip" data-abbr="${esc(l.abbr)}" title="${esc(l.name)}">${esc(l.abbr)}` +
    `<small>${esc(ex)}</small></button>`;

  // 法令はグループごとに（法律・施行令・規則）、通達は最後にまとめる
  const rows = INDEX.groups.map((g) => {
    const laws = INDEX.laws.filter((l) => l.group === g.key && l.kind !== 'tsutatsu');
    if (!laws.length) return '';
    return `<div class="jhRow"><span class="jhName">${esc(g.name)}</span>
      <span class="jhChips">${laws.map((l) => chip(l, l.kind === 'act' ? '22' : '1')).join('')}</span></div>`;
  }).join('');

  const tsus = INDEX.laws.filter((l) => l.kind === 'tsutatsu');
  const tsuRow = tsus.length
    ? `<div class="jhRow tsu"><span class="jhName">通達</span>
        <span class="jhChips">${tsus.map((t) => chip(t, exampleOf(t))).join('')}</span></div>`
    : '';

  return `<div class="jhHead">押すとその略称が入ります。続けて番号を打って Enter。</div>
    ${rows}${tsuRow}
    <div class="jhFoot">漢数字・全角数字・「第22条の2」の形でも通ります。
      通達の番号は原文のダッシュ（－ - ー ―）のどれでも構いません。</div>`;
}

/* 一覧に添える番号の例。通達は体系ごとに書き方が違うので実物から採る。 */
function exampleOf(t) {
  const toc = tocCache.get(t.key);
  if (toc && toc._flat) {
    const a = toc._flat.find((x) => /^t\d/.test(x.id));
    if (a) return a.l;
  }
  return { article: '36-1', chapter: '1-1-1', flat: '1' }[t.numbering] || '1';
}

function bindJumpHelp() {
  const nq = $('#jump'), help = $('#jumpHelp');
  let over = false;
  // 例に実物の番号を出したいので、通達の目次を一度だけ先に読んでおく
  let warmed = false;
  const warm = async () => {
    if (warmed) return;
    warmed = true;
    await Promise.all(INDEX.laws.filter((l) => l.kind === 'tsutatsu')
      .map((t) => getToc(t.key).catch(() => null)));
    if (!help.hidden) help.innerHTML = jumpHelpHtml(), bindChips();
  };
  // 略称を選んだ直後は入力欄に focus を戻す（続けて番号を打てるように）。
  // その focus で一覧が開き直さないよう、1回だけ開くのを見送る。
  let skipOpen = false;
  const bindChips = () => help.querySelectorAll('.jhChip').forEach((b) => {
    b.onclick = () => {
      nq.value = b.dataset.abbr;
      close();
      skipOpen = true;
      nq.focus();
    };
  });
  const open = () => {
    if (!help.hidden) return;
    help.innerHTML = jumpHelpHtml();
    help.hidden = false;
    nq.setAttribute('aria-expanded', 'true');
    bindChips();
    warm();
  };
  const close = () => {
    help.hidden = true;
    nq.setAttribute('aria-expanded', 'false');
  };
  nq.addEventListener('focus', () => {
    if (skipOpen) { skipOpen = false; return; }
    open();
  });
  nq.addEventListener('click', open);
  // 一覧の上を押している間は閉じない（blur が先に来るため）
  help.addEventListener('pointerdown', () => { over = true; });
  help.addEventListener('pointerup', () => { over = false; });
  nq.addEventListener('blur', () => { if (!over) setTimeout(close, 120); });
  nq.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { close(); nq.blur(); }
    if (e.key === 'Enter') close();
  });
  document.addEventListener('pointerdown', (e) => {
    if (!$('#jumpWrap').contains(e.target)) close();
  });
}

async function doJump() {
  $('#jumpHelp').hidden = true;
  const raw = $('#jump').value;
  if (!raw.trim()) return;
  const p = parseJump(raw);
  if (!p) {
    alert('「法法22」「所法56」「措法66の6」「所基通36-1」のように、略称＋番号で入力してください。\n' +
      '略称：' + INDEX.laws.map((l) => l.abbr).join('・'));
    return;
  }
  const toc = await getToc(p.key);
  const art = p.id
    ? toc._flat.find((a) => a.id === p.id)
    : toc._flat.find((a) => a.id === 'a' + p.num) ||
      // 「第四条から第七条まで」のような範囲条：範囲に含まれていれば拾う
      toc._flat.find((a) => a.id.includes('-') && inRange(a, p.num));
  if (!art) {
    alert(p.id
      ? `${lawOf(p.key).name} に その通達番号は見当たりません。\n（例：所基通36-1／所基通2-47／所基通36の2-1）`
      : `${lawOf(p.key).name} に ${joDisp(p.num.replace(/_/g, 'の'))} は見当たりません。\n` +
        '（削除された条・附則の条は本則の一覧にありません）');
    return;
  }
  navSearchQ = ''; fullTextHits = null;
  goto(p.key, art.id);
  closeDrawer();
  $('#jump').blur();
}

function inRange(a, num) {
  const m = /^a(\d+)-(\d+)$/.exec(a.id);
  if (!m || num.includes('_')) return false;
  const n = +num;
  return n >= +m[1] && n <= +m[2];
}

/* ─────────────────────────────── ルーティング */

function goto(key, id) { location.hash = `#/${key}/${id}`; }
function openLaw(key) {
  const t = tocCache.get(key);
  if (t) { goto(key, t._flat[0].id); return; }
  getToc(key).then((toc) => goto(key, toc._flat[0].id));
}

async function route() {
  const h = location.hash.replace(/^#\/?/, '');
  const seg = h.split('/').filter(Boolean);
  try {
    if (!seg.length) { renderHome(); return; }
    const key = seg[0];
    if (!lawOf(key)) { renderHome(); return; }
    if (state.law !== key) { navSearchQ = ''; fullTextHits = null; }
    state.law = key;

    if (seg[1] === 'suppl') { state.view = 'suppl'; state.sup = seg[2] !== undefined ? +seg[2] : null; }
    else if (seg[1] === 'appdx') { state.view = 'appdx'; state.sup = null; }
    else {
      state.view = 'art'; state.sup = null;
      const toc = await getToc(key);
      state.art = seg[1] && toc._byId.has(seg[1]) ? seg[1] : toc._flat[0].id;
    }
    main.innerHTML = '<div class="loading">読み込み中…</div>';
    await renderNav();
    if (state.view === 'suppl') await renderSuppl();
    else if (state.view === 'appdx') await renderAppdx();
    else await renderArticle();
  } catch (e) {
    console.error(e);
    showError('データの読み込みに失敗しました：' + e.message);
  }
}

/* ─────────────────────────────── ドロワー（iPad/スマホ） */

function closeDrawer() { nav.classList.remove('show'); scrim.hidden = true; $('#menuBtn').setAttribute('aria-expanded', 'false'); }
function toggleDrawer() {
  const on = nav.classList.toggle('show');
  scrim.hidden = !on;
  $('#menuBtn').setAttribute('aria-expanded', String(on));
}

/* ─────────────────────────────── 起動 */

(async function boot() {
  try {
    INDEX = await getJSON('index.json');
  } catch (e) {
    document.body.innerHTML = `<div class="err">データ（docs/data/index.json）が見つかりません。<br>
      <code>python scripts/parse_laws.py</code> を実行してから、<code>docs/</code> をHTTPサーバで開いてください
      （file:// では fetch がブロックされます）。<br>${esc(e.message)}</div>`;
    return;
  }
  $('#indexBar').innerHTML = INDEX.groups.map((g) =>
    `<span style="background:${g.color}" title="${esc(g.name)}" data-g="${g.key}"></span>`).join('');
  $('#indexBar').querySelectorAll('[data-g]').forEach((s) => {
    s.onclick = () => {
      const act = INDEX.laws.find((l) => l.group === s.dataset.g && l.kind === 'act');
      if (act) openLaw(act.key);
    };
  });
  $('#footBuild').textContent = `データ生成 ${(INDEX.built_at || '').slice(0, 10)}｜法令データは e-Gov API v${INDEX.source.api_version} から取得`;

  $('#jumpBtn').onclick = doJump;
  $('#jump').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing) doJump(); });
  bindJumpHelp();
  $('#menuBtn').onclick = toggleDrawer;
  scrim.onclick = closeDrawer;
  $('#homeBtnHdr').onclick = () => { location.hash = '#/'; };
  $('h1').onclick = () => { location.hash = '#/'; };
  window.addEventListener('hashchange', route);
  route();
})();
