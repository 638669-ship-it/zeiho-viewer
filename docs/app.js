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
    const total = laws.reduce((s, l) => s + l.articles, 0);
    return `<div class="lawCard" style="--c:${g.color}">
      <div class="cardTop">
        <div class="medal">${esc(g.abbr)}</div>
        <div class="cardName">${esc(g.name)}<small>施行令・施行規則つき／本則${total}条</small></div>
      </div>
      <div class="cardBtns">
        ${btn(byKind('act'), 'main', '法律')}
        ${btn(byKind('cabinet_order'), '', '施行令')}
        ${btn(byKind('ministerial_ordinance'), '', '規則')}
        <button class="cBtn" disabled>通達<span class="p2">Phase 2</span></button>
      </div>
    </div>`;
  }).join('');

  main.innerHTML = `
    <div class="home">
      <div class="homeTitle">
        <span class="big">税務六法</span>
        <span class="sub">国税法令集 Web</span>
      </div>
      <div class="cardGrid">${cards}
        <div class="lawCard" style="--c:#B5342A">
          <div class="cardTop">
            <div class="medal">評基通</div>
            <div class="cardName">財産評価基本通達<small>相続税・贈与税の財産評価</small></div>
          </div>
          <div class="cardBtns">
            <button class="cBtn" disabled>通達<span class="p2">Phase 2</span></button>
            <button class="cBtn" disabled>─</button><button class="cBtn" disabled>─</button><button class="cBtn" disabled>─</button>
          </div>
        </div>
      </div>
      <div class="homeFoot">
        カードの「法律」「施行令」「規則」を押すと目次ツリー＋条文画面が開きます。<br>
        ヘッダーの条番号ジャンプに <b>法法22</b> ／ <b>所法56</b> ／ <b>措法66の6</b> のように入力すると直行します。<br>
        出典：e-Gov法令検索（デジタル庁）　法令データは加工して表示しています。正確な法令は原文・官報で確認してください。
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
  const KIND = { '編': 'hen', '章': 'sho', '節': 'setsu', '款': 'kan', '目': 'moku' };
  return nodes.map((n) => {
    if (n.k === '条') {
      const cap = n.cap ? `<span class="cap${n.capd ? ' d' : ''}">${hl(n.cap, navSearchQ)}</span>` : '';
      return `<div class="jo${n.id === cur ? ' now' : ''}" data-id="${n.id}">
        <span class="num">${esc(joDisp(n.l))}</span>${cap}</div>`;
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

async function renderNav() {
  const key = state.law, toc = await getToc(key), law = lawOf(key), g = groupOf(key);
  const sibs = INDEX.laws.filter((l) => l.group === g.key);
  const tab = (kind, label) => {
    const l = sibs.find((s) => s.kind === kind);
    if (!l) return `<button disabled>${label}</button>`;
    return `<button class="${l.key === key ? 'on' : ''}" data-open="${l.key}">${label}</button>`;
  };

  let tree, note = '';
  if (navSearchQ || fullTextHits) {
    const q = navSearchQ;
    const keep = new Set();
    for (const a of toc._flat) {
      const inMidashi = q && ((a.cap || '').includes(q) || joDisp(a.l).includes(q) || a.l === q);
      if (inMidashi || (fullTextHits && fullTextHits.has(a.id))) keep.add(a.id);
    }
    tree = tocTreeHtml(filterToc(toc.toc, keep), state.art);
    note = `<div class="srchHit">「${esc(q)}」に一致：${keep.size}条` +
      (fullTextHits ? '（本文を含む）' : '（条番号・見出し）') + '</div>';
    if (!keep.size) tree = '<div class="none">一致する条がありません。</div>';
  } else {
    tree = tocTreeHtml(toc.toc, state.art);
  }

  const secBtn = (v, label, on) => on
    ? `<button class="${state.view === v ? 'on' : ''}" data-sec="${v}">${label}</button>`
    : `<button disabled>${label}</button>`;

  nav.style.display = '';
  nav.innerHTML = `
    <div class="navHead" style="--c:${g.color}">
      <div class="navHeadRow"><div class="medal">${esc(law.abbr)}</div>
        <div class="nm">${esc(law.name)}<small>${esc(law.law_num)}</small></div></div>
      <div class="navTabs" style="--c:${g.color}">
        ${tab('act', '法律')}${tab('cabinet_order', '施行令')}${tab('ministerial_ordinance', '規則')}
        <button disabled title="Phase 2 で収録">通達</button>
      </div>
      <div class="navSearch" style="--c:${g.color}">
        <input id="nq" placeholder="この法令内を検索" value="${esc(navSearchQ)}" aria-label="法令内検索"
               autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false">
        <button id="ftBtn" title="本文（条文の全文）も検索します">本文も</button>
      </div>
      <div class="navTabs" style="--c:${g.color};margin-top:6px;">
        ${secBtn('art', '本則', true)}
        ${secBtn('suppl', `附則(${law.suppls})`, toc.has_suppl)}
        ${secBtn('appdx', `別表(${law.appdx})`, toc.has_appdx)}
      </div>
    </div>
    ${note}
    <div class="toc" style="--c:${g.color}">${tree}</div>
    <button class="backHome">⌂ 法令メニューへ戻る</button>
    <div class="navNote">本則${law.articles}条／附則${law.suppls}本／別表${law.appdx}。
      施行日 ${esc(law.enforced || '－')}。出典：e-Gov法令検索。</div>`;

  nav.querySelectorAll('.jo').forEach((d) => {
    d.onclick = () => { goto(state.law, d.dataset.id); closeDrawer(); };
  });
  nav.querySelectorAll('.grp > .lbl').forEach((l) => {
    l.onclick = () => l.parentElement.classList.toggle('closed');
  });
  nav.querySelectorAll('[data-open]').forEach((b) => { b.onclick = () => openLaw(b.dataset.open); });
  nav.querySelectorAll('[data-sec]').forEach((b) => {
    b.onclick = () => {
      const v = b.dataset.sec;
      if (v === 'art') goto(state.law, state.art || tocCache.get(state.law)._flat[0].id);
      else location.hash = `#/${state.law}/${v}`;
    };
  });
  nav.querySelector('.backHome').onclick = () => { location.hash = '#/'; };

  const nq = $('#nq');
  let timer;
  nq.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      navSearchQ = nq.value.trim();
      fullTextHits = null;
      await renderNav();
      const el = $('#nq'); el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    }, 200);
  };
  $('#ftBtn').onclick = () => runFullText();
  // 現在の条を目次内に見えるところへ
  const now = nav.querySelector('.jo.now');
  if (now) now.scrollIntoView({ block: 'center' });
}

/* 法令内の本文検索。本則の全チャンクを読む（最大でも措法の約4.7MB）。 */
async function runFullText() {
  const q = navSearchQ;
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
        if (b.kind === 'table') for (const r of b.rows || []) for (const c of r) s += c.t;
      }
    }
    return s;
  };
  for (let f = 0; f < toc.chunks; f++) {
    for (const a of await getChunk(key, f)) if (hay(a).includes(q)) hits.add(a.id);
  }
  fullTextHits = hits;
  await renderNav();
}

/* ─────────────────────────────── 画面：条文 */

function egovUrl(key) {
  return `https://laws.e-gov.go.jp/law/${lawOf(key).law_id}`;
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

  main.innerHTML = `
    <article style="--c:${g.color}">
      <div class="pankuzu"><span class="lawChip">${esc(law.name)}</span>${a.path.map(esc).join('　›　')}</div>
      <div class="joHead">
        <div class="stamp">${esc(law.abbr)}<br>${esc(a.label)}</div>
        <div class="joTitle">${midashi}<span class="jonum">${esc(a.title)}</span></div>
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
      <div class="refBar">
        <a class="refChip now" href="${egovUrl(key)}" target="_blank" rel="noopener">e-Gov原文で確認 ↗</a>
        <span class="refChip tsu">関連通達（Phase 2）</span>
      </div>
      <div class="navi">
        <button data-go="${prev ? prev.id : ''}" ${prev ? '' : 'disabled'}>‹ ${prev ? esc(joDisp(prev.l)) : ''}</button>
        <button data-go="${next ? next.id : ''}" ${next ? '' : 'disabled'}>${next ? esc(joDisp(next.l)) : ''} ›</button>
      </div>
      <div class="srcNote">
        カッコ色分け：${['青', '赤', '緑', '黄', '紫']
      .map((nm, i) => `<span class="sw sw${i + 1}">第${i + 1}階層・${nm}</span>`).join('／')}（6階層目からは循環）。
        括弧の外＝条文の骨格は太字で表示します。「薄字」は括弧書きを淡色化して骨格だけを追う読み方用です。<br>
        出典：<a href="${egovUrl(key)}" target="_blank" rel="noopener">e-Gov法令検索（${esc(law.name)}）</a>を加工して作成。
        ${esc(law.law_num)}／施行日 ${esc(law.enforced || '－')}／データ取得 ${esc((INDEX.built_at || '').slice(0, 10))}。
        正確な法令は原文・官報で確認してください。
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

/* 「法法22の2」「法人税法第22条の2」「所法56」→ {key, num} */
function parseJump(raw) {
  let q = Z2H(raw.trim()).replace(/\s|　/g, '');
  if (!q) return null;

  // 略称 or 正式名称の長い順に前方一致
  const cands = [];
  for (const l of INDEX.laws) { cands.push([l.abbr, l.key]); cands.push([l.name, l.key]); }
  cands.sort((a, b) => b[0].length - a[0].length);
  const hit = cands.find(([nm]) => q.startsWith(nm));
  if (!hit) return null;
  let rest = q.slice(hit[0].length);

  rest = rest.replace(/^第/, '').replace(/条$/, '');
  // 「22条の2」「22の2」「二十二の二」いずれも受ける
  const parts = rest.split(/条?の/).filter((x) => x !== '');
  if (!parts.length) return null;
  const nums = parts.map((p) => (/^\d+$/.test(p) ? +p : kanjiNum(p)));
  if (nums.some((n) => n === null || Number.isNaN(n))) return null;
  return { key: hit[1], num: nums.join('_') };
}

async function doJump() {
  const raw = $('#jump').value;
  if (!raw.trim()) return;
  const p = parseJump(raw);
  if (!p) {
    alert('「法法22」「所法56」「措法66の6」のように、略称＋条番号で入力してください。\n' +
      '略称：' + INDEX.laws.map((l) => l.abbr).join('・'));
    return;
  }
  const toc = await getToc(p.key);
  const art = toc._flat.find((a) => a.id === 'a' + p.num) ||
    // 「第四条から第七条まで」のような範囲条：範囲に含まれていれば拾う
    toc._flat.find((a) => a.id.includes('-') && inRange(a, p.num));
  if (!art) {
    alert(`${lawOf(p.key).name} に ${joDisp(p.num.replace(/_/g, 'の'))} は見当たりません。\n` +
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
  $('#jump').addEventListener('keydown', (e) => { if (e.key === 'Enter') doJump(); });
  $('#menuBtn').onclick = toggleDrawer;
  scrim.onclick = closeDrawer;
  $('#homeBtnHdr').onclick = () => { location.hash = '#/'; };
  $('h1').onclick = () => { location.hash = '#/'; };
  window.addEventListener('hashchange', route);
  route();
})();
