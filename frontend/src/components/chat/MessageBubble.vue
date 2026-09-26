<!-- frontend/src/components/chat/MessageBubble.vue -->
<!-- 消息气泡：Markdown 渲染 + 代码高亮 + 知识库引用（放在回答下方）
     引用做**两级展开**：
       1) "引用来源 N 条"整体收起/展开
       2) 展开后每一条引用还能单独点开，看命中的原文 chunk 全文
          （后端 sources[].content 就是喂给模型的那段原文，不是摘要）
     注：原先的"复制/重新生成/赞/踩"按钮已按要求移除 -->
<template>
  <div ref="rootEl" class="message-wrap" :class="message.role">
    <!-- 用户消息 -->
    <div v-if="message.role === 'user'" class="user-msg">
      <div class="bubble user-bubble">{{ message.content }}</div>
      <div class="user-avatar">我</div>
    </div>

    <!-- AI 消息 -->
    <div v-else class="ai-msg">
      <div class="ai-avatar">AI</div>
      <div class="ai-content">
        <!-- 缓存标签 -->
        <div v-if="message.fromCache" class="cache-badge">缓存</div>

        <!-- 知识库联动：这次是否查了知识库、为什么 -->
        <div v-if="message.intent" class="kb-intent" :class="{ hit: !!message.sources?.length }"
             :title="message.intent.reason || ''">
          <span class="kb-dot" />
          <span v-if="message.sources?.length">
            已检索企业知识库 · 命中 {{ message.sources.length }} 条（{{ intentReason }}）
          </span>
          <span v-else-if="message.intent.needKnowledge">
            {{ missSummary }}
          </span>
          <span v-else>未检索知识库（{{ intentReason }}）</span>
          <!-- 全链路：这一轮从提问到回答，系统都干了什么（意图/检索/重排/模型/工具） -->
          <router-link v-if="message.runId" class="trace-link" :to="'/trace?run=' + message.runId"
                       target="_blank" title="看这次请求的完整执行链路">全链路</router-link>
        </div>

        <!-- 未命中要能自证原因：库空 / 被权限过滤 / 分数低于阈值，处理方式完全不同 -->
        <div v-if="showRecallDetail" class="kb-recall">{{ recallDetail }}</div>

        <!-- 消息内容（Markdown 渲染）。
             回答里的 [1] [2] 会被渲染成可点击的蓝色角标（见 utils/citations.js），
             点一下展开下方"引用来源"并跳到对应那条 —— v-html 注入的元素绑不上 Vue 事件，
             所以点击统一委托到这个容器上。 -->
        <div
          class="bubble ai-bubble markdown-body"
          v-html="renderedContent"
          @click="onContentClick"
        />

        <!-- 流式输出时的光标 -->
        <span v-if="message.streaming" class="cursor-blink" />

        <!-- 引用来源：放在回答**下面**，默认收起、可展开。
             收起的标题行也会给出文档名，不用点开就知道答案的出处；
             展开后每条显示页码/结构/相似度/片段预览。 -->
        <div v-if="message.sources?.length && !message.streaming" class="kb-cites">
          <button class="cites-toggle" :class="{ open: showSources }" @click="showSources = !showSources">
            <span class="cites-caret">{{ showSources ? '▾' : '▸' }}</span>
            <span class="cites-label">引用来源 {{ message.sources.length }} 条</span>
            <span class="cites-docs">{{ sourcesSummary }}</span>
          </button>

          <div v-show="showSources" class="kb-sources">
            <!-- 批量操作：6 条引用一条条点太累 -->
            <div class="cites-tools">
              <button class="link-btn" @click="toggleAllChunks">
                {{ allChunksOpen ? '收起全部原文' : '展开全部原文' }}
              </button>
            </div>

            <!-- 每条引用独立展开：点开看命中的原文 chunk 全文（就是喂给模型的那段） -->
            <div v-for="(s, i) in message.sources" :key="s.chunkId" class="kb-source"
                 :data-chunk-id="s.chunkId">
              <button class="ks-row" :class="{ open: !!openChunks[s.chunkId] }"
                      @click="toggleChunk(s.chunkId)" :title="openChunks[s.chunkId] ? '收起原文' : '展开原文片段'">
                <span class="ks-caret">{{ openChunks[s.chunkId] ? '▾' : '▸' }}</span>
                <span class="ks-idx">[{{ i + 1 }}]</span>
                <span class="ks-title">{{ s.title }}</span>
                <span class="ks-loc">{{ s.pageLabel || (s.pageNumber ? '第' + s.pageNumber + '页' : '无页码') }} · {{ elementLabel(s.elementType) }}</span>
                <span v-if="s.headingPath?.length" class="ks-path" :title="s.headingPath.join(' › ')">
                  {{ s.headingPath.join(' › ') }}
                </span>
                <span class="ks-meta">{{ deptLabel(s.department) }} · {{ s.version }}</span>
                <!-- 两个分数含义不同：向量分=整体语义像不像；重排分=能不能回答这个问题 -->
                <span class="ks-score" :title="scoreTitle(s)">
                  {{ (s.score * 100).toFixed(0) }}%
                  <span v-if="s.rerankScore != null" class="ks-rr">重排 {{ fmtRerank(s.rerankScore) }}</span>
                </span>
              </button>

              <div v-if="openChunks[s.chunkId]" class="ks-content">
                <div class="ks-content-head">
                  原文切片 · {{ s.chunkId }} · {{ (s.content || '').length }} 字
                  <span v-if="s.fileName"> · 来源文件 {{ s.fileName }}</span>
                </div>
                <div class="ks-content-body">{{ s.content }}</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick } from 'vue'
import { marked } from 'marked'
import hljs from 'highlight.js'
import 'highlight.js/styles/github-dark.css'
import { citeIndexFromEvent, decorateCitations, findSourceNode, revealNode } from '@/utils/citations.js'
const props = defineProps({
  message: { type: Object, required: true },
})

// 分数说明：向量召回分（0-1）与重排分（LLM 打分 0-10 / cross-encoder 0-1）不是一回事，
// 鼠标悬停时说清楚，避免把"重排 2 分"误读成"相似度 2%"
// 重排分有两种量纲：cross-encoder（bge/jina/cohere）是 0-1，LLM 打分是 0-10
function fmtRerank(v) {
  if (v == null) return ''
  return v <= 1 ? Math.round(v * 100) + '%' : v + '/10'
}

function scoreTitle(s) {
  const parts = ['向量相似度 ' + (s.score * 100).toFixed(1) + '%（整体语义接近程度）']
  if (s.rerankScore != null) parts.push('重排分 ' + fmtRerank(s.rerankScore) + '（能否直接回答这个问题，由重排模型判定）')
  return parts.join('；')
}

// 引用来源默认收起（回答本身才是主角），点标题行展开明细
const showSources = ref(false)

// 点回答里的 [1] 角标 → 展开引用框 + 展开那条原文 + 滚过去闪一下
const rootEl = ref(null)
async function onContentClick(event) {
  const idx = citeIndexFromEvent(event)
  if (idx < 0) return
  const source = (props.message.sources || [])[idx]
  if (!source) return
  showSources.value = true
  openChunks.value = { ...openChunks.value, [source.chunkId]: true }
  await nextTick()
  revealNode(findSourceNode(rootEl.value, source.chunkId))
}

// 每条引用再单独展开：看到命中的原文 chunk 全文（后端 sources[].content）
// 用 chunkId 做 key，而不是数组下标 —— 重新生成/换范围后下标会错位
const openChunks = ref({})

function toggleChunk(chunkId) {
  openChunks.value[chunkId] = !openChunks.value[chunkId]
}

// 全部展开/收起：引用多的时候一条条点太累
const allChunksOpen = computed(() =>
  (props.message.sources || []).length > 0 &&
  props.message.sources.every(s => openChunks.value[s.chunkId])
)

function toggleAllChunks() {
  const next = !allChunksOpen.value
  const map = {}
  for (const s of props.message.sources || []) map[s.chunkId] = next
  openChunks.value = map
}

// 收起状态也要能看出出处：标题行给出命中的文档名（去重，最多 2 个）
const sourcesSummary = computed(() => {
  const titles = [...new Set((props.message.sources || []).map(s => s.title).filter(Boolean))]
  if (!titles.length) return ''
  const head = titles.slice(0, 2).map(t => '《' + t + '》').join('')
  return titles.length > 2 ? head + ' 等 ' + titles.length + ' 篇' : head
})

const ELEMENT_LABELS = { title: '标题', paragraph: '段落', list: '列表', table: '表格', code: '代码', other: '其他' }
const DEPT_LABELS = { general: '通用', hr: '人力', tech: '技术', finance: '财务', legal: '法务', product: '产品', sales: '市场' }
const elementLabel = (v) => ELEMENT_LABELS[v] || v
const deptLabel = (v) => DEPT_LABELS[v] || v

// 未命中原因：后端直接给出 reason，前端不再统一写成"当前身份下未命中内容"
// （之前那句话把所有情况都说成权限问题，把排查方向带偏了）
const MISS_LABELS = {
  kb_empty: '知识库当前没有可用内容',
  all_filtered: '候选被权限/版本条件全部过滤',
  below_threshold: '相似度低于阈值',
  storage_unavailable: '知识库数据库不可用',
  embedding_unavailable: 'embedding 模型不可用',
}

const missSummary = computed(() => {
  const r = props.message.recall
  const label = (r && MISS_LABELS[r.reason]) || '未命中内容'
  return `已检索企业知识库 · ${label}（${intentReason.value}）`
})

const showRecallDetail = computed(() =>
  !!props.message.recall && !props.message.sources?.length && !!props.message.intent?.needKnowledge
)

const recallDetail = computed(() => {
  const r = props.message.recall
  if (!r) return ''
  const bits = []
  if (r.totalChunks !== undefined) bits.push(`库内切片 ${r.totalChunks}`)
  if (r.candidates !== undefined) bits.push(`通过过滤 ${r.candidates}`)
  if (r.bestScore != null) bits.push(`最高分 ${r.bestScore}`)
  if (r.threshold != null) bits.push(`阈值 ${r.threshold}`)
  const hintNote = (r.departmentHint && !r.departmentHintMatched)
    ? `；模型猜的部门（${r.departmentHint}）只作提示、不参与过滤`
    : ''
  return (r.explain || '') + (bits.length ? `（${bits.join(' · ')}` + hintNote + '）' : hintNote)
})

// 意图可解释性：把后端的判定依据翻译成一句人话
// 默认策略是"召回优先"（recall-first）：默认就查库，只有闲聊/纯算式这类
// 明确与知识库无关的消息才跳过 —— 完整解释由后端 reason 字段给出，挂在 title 上
const INTENT_LABELS = {
  'recall-first': '默认检索知识库',
  'forced': '手动强制检索',
  'disabled': '已关闭知识库检索',
  'strict-rule': '按关键词判定',
  'strict-model': '模型判定需要查资料',
}
const intentReason = computed(() => {
  const it = props.message.intent
  if (!it) return ''
  const key = it.decisionSource || ''
  if (INTENT_LABELS[key]) return INTENT_LABELS[key]
  if (key.startsWith('skip-')) return '与知识库无关'
  if (it.ruleHit?.length) return '命中关键词：' + it.ruleHit.slice(0, 4).join('、')
  return '与知识库无关'
})

// 配置 marked：代码块自动高亮
marked.setOptions({
  highlight(code, lang) {
    if (lang && hljs.getLanguage(lang)) {
      return hljs.highlight(code, { language: lang }).value
    }
    return hljs.highlightAuto(code).value
  },
  breaks: true,     // 换行转 <br>
  gfm: true,        // GitHub Flavored Markdown
})

// 把 Markdown 文本转成 HTML，并把 [1] 这类引用编号变成可点击角标
const renderedContent = computed(() => {
  if (!props.message.content) return ''
  try {
    return decorateCitations(marked(props.message.content), props.message.sources)
  } catch {
    return props.message.content
  }
})

</script>

<style scoped>
.message-wrap { display: flex; flex-direction: column; }

/* 用户消息 */
.user-msg {
  display: flex;
  align-items: flex-start;
  justify-content: flex-end;
  gap: 10px;
}

.user-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: var(--color-success);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 600;
  flex-shrink: 0;
}

.user-bubble {
  max-width: 72%;
  padding: 10px 14px;
  background: var(--color-primary);
  color: #fff;
  border-radius: 14px 4px 14px 14px;
  font-size: 14px;
  line-height: 1.7;
  word-break: break-word;
}

/* AI 消息 */
.ai-msg {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.ai-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: var(--color-primary);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 700;
  flex-shrink: 0;
  margin-top: 2px;
}

.ai-content {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.cache-badge {
  display: inline-block;
  font-size: 10px;
  color: #6d28d9;
  background: #ede9fe;
  padding: 2px 8px;
  border-radius: var(--radius-full);
  margin-bottom: 4px;
}

.ai-bubble {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 4px 14px 14px 14px;
  padding: 12px 16px;
  font-size: 14px;
  line-height: 1.75;
  word-break: break-word;
  max-width: 78%;
}

/* 知识库联动：意图提示 */
.kb-intent {
  display: flex; align-items: center; gap: 6px;
  font-size: 11px; color: var(--color-text-muted);
  margin-bottom: 6px;
}
.kb-intent .kb-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--color-border); flex-shrink: 0;
}
.kb-intent.hit { color: var(--color-primary-dark); }
.kb-intent.hit .kb-dot { background: var(--color-success); }
.kb-intent .trace-link { margin-left: auto; }

/* 未命中原因明细：原文 + 候选数/最高分/阈值，便于判断是入库问题还是阈值问题 */
.kb-recall {
  font-size: 10.5px; line-height: 1.6; color: #b45309;
  background: #fffbeb; border: 1px solid #fde68a;
  border-radius: var(--radius-sm);
  padding: 4px 8px; margin-bottom: 6px;
}

/* 引用来源：放在回答下方，默认收起 */
.kb-cites { margin-top: 8px; max-width: 78%; }

.cites-toggle {
  display: flex; align-items: center; gap: 6px;
  width: 100%; text-align: left;
  padding: 5px 9px;
  background: var(--color-bg);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-sm);
  color: var(--color-text-sub);
  font-size: 11.5px;
  cursor: pointer;
  transition: all var(--transition);
}
.cites-toggle:hover { border-color: var(--color-primary); color: var(--color-primary); }
.cites-toggle.open { border-color: var(--color-primary); color: var(--color-primary); }
.cites-caret { color: var(--color-text-muted); font-size: 10px; }
.cites-label { font-weight: 600; flex-shrink: 0; }
.cites-docs {
  color: var(--color-text-muted); font-weight: 400;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

.kb-sources { display: flex; flex-direction: column; gap: 4px; margin-top: 6px; }
.kb-source {
  display: flex; flex-direction: column; gap: 3px;
  font-size: 11px; padding: 5px 9px;
  background: var(--color-bg); border: 1px solid var(--color-border-light);
  border-radius: var(--radius-sm);
}
.cites-tools { display: flex; justify-content: flex-end; }
.link-btn {
  border: none; background: none; padding: 0 2px;
  color: var(--color-primary); font-size: 11px; cursor: pointer;
}
.link-btn:hover { text-decoration: underline; }

/* 每条引用：整行可点，展开后是原文切片 */
.ks-row {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  width: 100%; text-align: left;
  border: none; background: none; padding: 0;
  color: inherit; font-size: 11px; cursor: pointer;
}
.ks-row:hover .ks-title { color: var(--color-primary); }
.ks-caret { color: var(--color-text-muted); font-size: 10px; flex-shrink: 0; }

.ks-content {
  margin-top: 5px; padding: 6px 8px;
  background: var(--color-surface);
  border-left: 2px solid var(--color-primary);
  border-radius: var(--radius-sm);
}
.ks-content-head {
  font-size: 10.5px; color: var(--color-text-muted);
  font-family: var(--font-mono); margin-bottom: 4px;
}
.ks-content-body {
  font-size: 11.5px; line-height: 1.7; color: var(--color-text-sub);
  white-space: pre-wrap; word-break: break-word;
  max-height: 240px; overflow-y: auto;
}
.ks-idx { font-family: var(--font-mono); color: var(--color-primary); font-weight: 600; }
.ks-title { font-weight: 600; color: var(--color-text); }
.ks-loc { color: var(--color-text-muted); }
.ks-meta {
  color: var(--color-primary-dark); background: var(--color-primary-bg);
  border-radius: var(--radius-full); padding: 0 6px;
}
/* 章节路径：切片是按标题树切的，引用时告诉用户"出自哪一节" */
.ks-path {
  color: var(--color-text-muted); font-size: 10.5px;
  max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ks-score { margin-left: auto; color: var(--color-success); font-family: var(--font-mono); white-space: nowrap; }
.ks-rr {
  color: var(--color-primary-dark); background: var(--color-primary-bg);
  border-radius: var(--radius-full); padding: 0 5px; margin-left: 4px;
}

/* 打字机光标 */
.cursor-blink {
  display: inline-block;
  width: 2px;
  height: 1em;
  background: var(--color-primary);
  margin-left: 2px;
  vertical-align: text-bottom;
  animation: blink 0.7s step-end infinite;
}
@keyframes blink {
  0%, 100% { opacity: 1; }
  50%       { opacity: 0; }
}
</style>

<!-- Markdown 渲染全局样式（非 scoped） -->
<style>
.markdown-body h1, .markdown-body h2, .markdown-body h3 {
  margin: 1em 0 .5em;
  font-weight: 600;
  line-height: 1.4;
}
.markdown-body h1 { font-size: 1.4em; }
.markdown-body h2 { font-size: 1.2em; }
.markdown-body h3 { font-size: 1.05em; }
.markdown-body p  { margin: .6em 0; }
.markdown-body ul, .markdown-body ol {
  padding-left: 1.5em;
  margin: .5em 0;
}
.markdown-body li { margin: .25em 0; }
.markdown-body strong { font-weight: 600; }
.markdown-body table {
  width: 100%;
  border-collapse: collapse;
  margin: .75em 0;
  font-size: 13px;
}
.markdown-body th, .markdown-body td {
  padding: 6px 12px;
  border: 1px solid var(--color-border);
  text-align: left;
}
.markdown-body th { background: var(--color-border-light); font-weight: 600; }
.markdown-body blockquote {
  border-left: 3px solid var(--color-primary);
  padding-left: 12px;
  color: var(--color-text-sub);
  margin: .5em 0;
}
</style>
