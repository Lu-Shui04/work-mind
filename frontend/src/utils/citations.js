// frontend/src/utils/citations.js
// 知识库引用角标：把回答里的 [1] [2] 渲染成可点击的蓝色小框，点一下跳到"引用来源"里对应的那一条。
//
// 为什么需要它：后端已经要求模型"在句末直接写 [1] 这种编号"（编号 = 资料列表里的序号），
// 但 marked 渲染出来的 [1] 只是普通文字 —— 和正文没区别、也点不了。
// 这里在渲染之后做一次轻量后处理，把编号换成 <button class="cite-chip" data-cite-idx="0">1</button>，
// 点击由页面上的事件委托接管（v-html 注入的元素绑不上 Vue 事件，只能委托到容器上）。

// 老格式兼容：模型偶尔还会写【来源：文档标题 · 第N页】（以及历史缓存里的旧回答）
const LEGACY_RE = /【来源：\s*([^】·]+?)\s*·\s*第\s*(\d+)\s*页\s*】/g
const NUMERIC_RE = /\[(\d{1,2})\]/g

function normalize(s) {
  return String(s || '')
    .replace(/[\s《》〈〉“”"'（）()·.、,，:：-]/g, '')
    .toLowerCase()
}

/** 按标题（优先）或页码，在引用列表里找到对应的一条；找不到返回 -1 */
function findSourceIndex(sources, { title = '', page = null } = {}) {
  if (!Array.isArray(sources) || !sources.length) return -1
  const t = normalize(title)
  if (t) {
    const exact = sources.findIndex((s) => normalize(s.title) === t)
    if (exact >= 0) return exact
    const partial = sources.findIndex((s) => {
      const st = normalize(s.title)
      return st && (st.includes(t) || t.includes(st))
    })
    if (partial >= 0) return partial
  }
  if (page != null) {
    const byPage = sources.findIndex((s) => Number(s.pageNumber) === Number(page))
    if (byPage >= 0) return byPage
  }
  return -1
}

function escapeAttr(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

function chipHtml(index, sources) {
  const s = sources[index] || {}
  const loc = s.pageLabel || (s.pageNumber ? '第' + s.pageNumber + '页' : '')
  const tip = [s.title ? '《' + s.title + '》' : '', loc].filter(Boolean).join(' · ')
  return '<button type="button" class="cite-chip" data-cite-idx="' + index + '"'
    + ' title="查看出处：' + escapeAttr(tip || ('引用 ' + (index + 1))) + '">' + (index + 1) + '</button>'
}

/**
 * 把答案 HTML 里的 [n] / 【来源：… · 第N页】 换成可点击角标。
 * @param {string} html marked 渲染后的 HTML
 * @param {Array} sources 该条消息的引用列表（顺序 = 编号顺序）
 */
export function decorateCitations(html, sources) {
  if (!html || !Array.isArray(sources) || !sources.length) return html
  // 代码块 / 行内代码里的 [1] 是代码不是引用：按代码段切开，只处理非代码段
  return String(html)
    .split(/(<pre[\s\S]*?<\/pre>|<code[\s\S]*?<\/code>)/g)
    .map((part) => {
      if (/^<(pre|code)[\s>]/.test(part)) return part
      let out = part.replace(LEGACY_RE, (m, title, page) => {
        const i = findSourceIndex(sources, { title, page })
        return i >= 0 ? chipHtml(i, sources) : m
      })
      out = out.replace(NUMERIC_RE, (m, n) => {
        const i = Number(n) - 1
        return i >= 0 && i < sources.length ? chipHtml(i, sources) : m
      })
      return out
    })
    .join('')
}

/** 从被点的角标里取出引用下标（不是角标返回 -1） */
export function citeIndexFromEvent(event) {
  const chip = event?.target?.closest?.('.cite-chip')
  if (!chip) return -1
  const idx = Number(chip.dataset.citeIdx)
  return Number.isInteger(idx) && idx >= 0 ? idx : -1
}

/** 在容器里找到某条引用对应的 DOM 节点（按 chunkId 找，不用拼选择器，避免 id 里的特殊字符） */
export function findSourceNode(container, chunkId) {
  if (!container || !chunkId) return null
  const nodes = container.querySelectorAll('[data-chunk-id]')
  for (const n of nodes) if (n.dataset.chunkId === chunkId) return n
  return null
}

/** 滚动到引用条目并闪一下，让用户看清跳到了哪一条 */
export function revealNode(node) {
  if (!node) return
  node.scrollIntoView({ behavior: 'smooth', block: 'center' })
  node.classList.add('cite-flash')
  setTimeout(() => node.classList.remove('cite-flash'), 1500)
}
