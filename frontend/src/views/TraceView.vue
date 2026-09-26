<!-- frontend/src/views/TraceView.vue -->
<!-- 全链路追踪：一次请求（run）内部到底发生了什么，按时间线展开。
     左边是"哪次请求"，右边是"这次请求的每一步"—— 意图判定 / 向量化 / 检索 / 重排 /
     缓存 / 提示词 / 模型生成 / 工具入参出参 / 异常，每步都能点开看原始数据。
     相当于把开发者盯终端看流程的那套东西，变成可回看、能搜索、能对着某条聊天记录打开。 -->
<template>
  <div class="trace-view">
    <!-- 顶部：小结 + 筛选 -->
    <div class="toolbar">
      <div class="stats">
        <span class="stat-chip" v-for="s in statsToday" :key="s.feature">
          {{ s.label }} <b>{{ s.total }}</b>
          <em v-if="s.failed" class="bad">失败 {{ s.failed }}</em>
        </span>
        <span v-if="!statsToday.length" class="stat-chip muted">今天还没有追踪记录</span>
      </div>
      <div class="filters">
        <select v-model="filter.feature" class="input" @change="loadRuns">
          <option value="">全部功能</option>
          <option value="chat">对话助手</option>
          <option value="agent">任务 Agent</option>
          <option value="knowledge">RAG 知识库</option>
        </select>
        <select v-model="filter.status" class="input" @change="loadRuns">
          <option value="">全部状态</option>
          <option value="ok">成功</option>
          <option value="error">失败</option>
          <option value="cancelled">中断</option>
        </select>
        <input v-model="filter.q" class="input q" placeholder="搜问题 / runId" @keyup.enter="loadRuns" />
        <button class="btn btn-ghost btn-sm" @click="loadRuns">刷新</button>
        <button class="btn btn-ghost btn-sm" :class="{ on: autoRefresh }" @click="toggleAuto">
          {{ autoRefresh ? '自动刷新中' : '自动刷新' }}
        </button>
        <button class="btn btn-ghost btn-sm danger" @click="clearAll">清空</button>
      </div>
    </div>

    <div class="body">
      <!-- 左：请求列表 -->
      <aside class="run-list">
        <div v-if="!runs.length" class="empty">暂无记录，去对话或 Agent 跑一次就会出现在这里</div>
        <button v-for="r in runs" :key="r.runId" class="run-item" :class="{ active: r.runId === currentRunId }"
                @click="openRun(r.runId)">
          <div class="run-line1">
            <span class="dot" :class="'st-' + r.status" />
            <span class="tag">{{ r.featureLabel }}</span>
            <span class="time">{{ fmtTime(r.time) }}</span>
            <span class="dur">{{ r.durationMs }}ms</span>
            <span class="steps">{{ r.stepCount }} 步</span>
          </div>
          <div class="q">{{ r.question || '(无问题文本)' }}</div>
          <div v-if="r.error" class="err">{{ r.error }}</div>
        </button>
      </aside>

      <!-- 右：时间线 -->
      <main class="detail">
        <div v-if="!detail" class="empty">← 选一条记录，查看它的完整执行链路</div>
        <template v-else>
          <div class="detail-head">
            <div class="dh-line1">
              <span class="tag">{{ detail.featureLabel }}</span>
              <span class="dot" :class="'st-' + detail.status" />
              <span class="status-text">{{ statusLabel(detail.status) }}</span>
              <span class="dur">总耗时 {{ detail.durationMs }}ms</span>
              <span class="steps">{{ detail.stepCount }} 步</span>
              <span class="mono runid" :title="detail.runId">{{ detail.runId }}</span>
            </div>
            <div class="dh-q">{{ detail.question }}</div>
            <div class="dh-meta">
              <span>身份：{{ detail.userName || detail.userId || '匿名' }}（{{ detail.tenantId }}）</span>
              <span>时间：{{ fmtTime(detail.time) }}</span>
              <span v-if="detail.error" class="err">错误：{{ detail.error }}</span>
            </div>
            <div v-if="Object.keys(detail.summary || {}).length" class="dh-summary">
              <span v-for="(v, k) in detail.summary" :key="k" class="sum-chip">{{ k }}: {{ short(v) }}</span>
            </div>
          </div>

          <div class="timeline">
            <div v-for="s in detail.steps" :key="s.idx" class="step" :class="'k-' + s.kind">
              <div class="step-rail">
                <span class="step-dot" :class="{ bad: s.status !== 'ok' }" />
              </div>
              <div class="step-body">
                <button class="step-head" @click="toggle(s.idx)">
                  <span class="step-icon">{{ kindIcon(s.kind) }}</span>
                  <span class="step-name">{{ s.name }}</span>
                  <span class="step-off">+{{ s.offsetMs }}ms</span>
                  <span v-if="s.durationMs" class="step-dur">{{ s.durationMs }}ms</span>
                  <span v-if="s.status !== 'ok'" class="badge bad">{{ s.status }}</span>
                  <span class="caret">{{ isOpen(s.idx) ? '▾' : '▸' }}</span>
                </button>
                <pre v-if="isOpen(s.idx)" class="detail-json">{{ pretty(s.detail) }}</pre>
              </div>
            </div>
          </div>
        </template>
      </main>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import http from '@/utils/http.js'
import { useAppStore } from '@/stores/app.js'

const appStore = useAppStore()
const route = useRoute()

const runs = ref([])
const detail = ref(null)
const currentRunId = ref('')
const statsToday = ref([])
const openSteps = reactive({})
const autoRefresh = ref(true)
const filter = reactive({ feature: '', status: '', q: '' })
let timer = null

// 步骤类型 → 图标（一眼看出这一段在干什么）
const KIND_ICONS = {
  request: '📥', intent: '🎯', embedding: '🧬', vector_search: '🔍', rerank: '📊',
  retrieval: '📚', prompt: '📝', cache: '⚡', llm: '🤖', tool_call: '🔧',
  tool_result: '📦', route: '🧭', response: '✅', error: '❌',
}
const kindIcon = (k) => KIND_ICONS[k] || '•'
const STATUS = { ok: '成功', error: '失败', running: '进行中', cancelled: '已中断' }
const statusLabel = (s) => STATUS[s] || s

// 默认展开"最需要看入参出参"的那几步：工具调用/返回、异常、路由判定
const OPEN_BY_DEFAULT = new Set(['tool_call', 'tool_result', 'error', 'route'])
const isOpen = (idx) => openSteps[idx] !== undefined ? openSteps[idx] : false
function toggle(idx) { openSteps[idx] = !isOpen(idx) }

function fmtTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  const p = (n) => String(n).padStart(2, '0')
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds())
}
function short(v) {
  const s = typeof v === 'string' ? v : JSON.stringify(v)
  return s && s.length > 40 ? s.slice(0, 40) + '…' : s
}
function pretty(v) {
  try { return JSON.stringify(v ?? {}, null, 2) } catch { return String(v) }
}

async function loadStats() {
  try {
    const d = await http.get('/trace/stats', { silent: true })
    statsToday.value = d.today || []
  } catch { /* 静默：统计拿不到不影响看列表 */ }
}

async function loadRuns(keepSelection = true) {
  try {
    const qs = new URLSearchParams()
    qs.set('limit', '80')
    if (filter.feature) qs.set('feature', filter.feature)
    if (filter.status) qs.set('status', filter.status)
    if (filter.q) qs.set('q', filter.q)
    const d = await http.get('/trace/runs?' + qs.toString(), { silent: true })
    runs.value = d.runs || []
    if (!keepSelection || !currentRunId.value) {
      if (runs.value.length) openRun(runs.value[0].runId)
    }
  } catch (err) {
    // 后台轮询失败不弹 toast（和后端没起来时的表现一致）
  }
}

async function openRun(runId) {
  if (!runId) return
  currentRunId.value = runId
  openStepsReset()
  try {
    detail.value = await http.get('/trace/runs/' + runId, { silent: true })
    // 关键步骤默认展开
    for (const s of detail.value.steps || []) {
      if (OPEN_BY_DEFAULT.has(s.kind)) openSteps[s.idx] = true
    }
    // 深链：把 runId 写进地址栏，方便把某一次执行直接发给别人看
    if (route.query.run !== runId) {
      window.history.replaceState(null, '', '/trace?run=' + runId)
    }
  } catch (err) {
    appStore.toast.error('读取追踪详情失败：' + (err?.message || ''))
  }
}
function openStepsReset() {
  for (const k of Object.keys(openSteps)) delete openSteps[k]
}

function toggleAuto() {
  autoRefresh.value = !autoRefresh.value
  autoRefresh.value ? startTimer() : stopTimer()
}
function startTimer() {
  stopTimer()
  timer = setInterval(() => { loadRuns(); loadStats() }, 8000)
}
function stopTimer() { if (timer) clearInterval(timer); timer = null }

async function clearAll() {
  if (!confirm('清空全部追踪记录？（聊天记录不受影响）')) return
  try {
    const d = await http.delete('/trace/runs')
    appStore.toast.success('已清空 ' + (d.cleared || 0) + ' 条追踪')
    detail.value = null
    currentRunId.value = ''
    await loadRuns(false)
    await loadStats()
  } catch { appStore.toast.error('清空失败') }
}

onMounted(async () => {
  await loadStats()
  const deepLink = route.query.run
  await loadRuns(!deepLink)
  if (deepLink) await openRun(String(deepLink))
  startTimer()
})
onUnmounted(stopTimer)
</script>

<style scoped>
.trace-view { height:100%; display:flex; flex-direction:column; background:var(--color-bg); overflow:hidden; }

/* 顶部工具条 */
.toolbar {
  display:flex; align-items:center; justify-content:space-between; gap:var(--space-md);
  padding:10px var(--space-lg); border-bottom:1px solid var(--color-border);
  background:var(--color-surface); flex-wrap:wrap; flex-shrink:0;
}
.stats { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.stat-chip {
  font-size:11.5px; color:var(--color-text-sub); background:var(--color-bg);
  border:1px solid var(--color-border-light); border-radius:var(--radius-full); padding:3px 10px;
}
.stat-chip b { color:var(--color-text); }
.stat-chip.muted { color:var(--color-text-muted); }
.stat-chip .bad { color:var(--color-danger); font-style:normal; margin-left:4px; }
.filters { display:flex; align-items:center; gap:8px; }
.filters .input { padding:5px 8px; font-size:12px; }
.filters .q { width:180px; }
.btn-sm { padding:5px 10px; font-size:12px; }
.btn-ghost.on { color:var(--color-primary); border-color:var(--color-primary); }
.btn-ghost.danger:hover { color:var(--color-danger); border-color:var(--color-danger); }

.body { flex:1; display:flex; min-height:0; }

/* 左：列表 */
.run-list {
  width:360px; flex-shrink:0; overflow-y:auto;
  border-right:1px solid var(--color-border); background:var(--color-surface);
}
.run-item {
  display:block; width:100%; text-align:left; cursor:pointer;
  padding:10px 14px; border:none; border-bottom:1px solid var(--color-border-light);
  background:transparent; transition:background var(--transition);
}
.run-item:hover { background:var(--color-bg); }
.run-item.active { background:var(--color-primary-bg); border-left:3px solid var(--color-primary); }
.run-line1 { display:flex; align-items:center; gap:6px; font-size:11px; color:var(--color-text-muted); }
.tag {
  font-size:10px; padding:1px 7px; border-radius:var(--radius-full);
  background:var(--color-border-light); color:var(--color-text-sub);
}
.dot { width:7px; height:7px; border-radius:50%; background:var(--color-border); flex-shrink:0; }
.dot.st-ok { background:var(--color-success); }
.dot.st-error { background:var(--color-danger); }
.dot.st-cancelled { background:var(--color-warning); }
.dur, .steps { font-family:var(--font-mono); }
.q {
  margin-top:4px; font-size:12.5px; color:var(--color-text); line-height:1.5;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden;
}
.err { margin-top:3px; font-size:11px; color:var(--color-danger); }

/* 右：详情 */
.detail { flex:1; min-width:0; overflow-y:auto; padding:var(--space-lg); }
.detail > * { flex-shrink:0; }
.empty { color:var(--color-text-muted); font-size:12.5px; padding:32px; text-align:center; }
.detail-head {
  background:var(--color-surface); border:1px solid var(--color-border);
  border-radius:var(--radius-lg); padding:12px 16px; margin-bottom:var(--space-md);
}
.dh-line1 { display:flex; align-items:center; gap:10px; font-size:11.5px; color:var(--color-text-muted); flex-wrap:wrap; }
.status-text { color:var(--color-text-sub); }
.runid { margin-left:auto; font-size:11px; color:var(--color-text-muted); font-family:var(--font-mono); }
.dh-q { margin:8px 0 6px; font-size:14px; font-weight:600; color:var(--color-text); line-height:1.6; }
.dh-meta { display:flex; gap:14px; font-size:11.5px; color:var(--color-text-muted); flex-wrap:wrap; }
.dh-summary { margin-top:8px; display:flex; gap:6px; flex-wrap:wrap; }
.sum-chip {
  font-size:11px; font-family:var(--font-mono); color:var(--color-primary-dark);
  background:var(--color-primary-bg); border-radius:var(--radius-full); padding:2px 9px;
}

/* 时间线 */
.timeline { display:flex; flex-direction:column; }
.step { display:flex; gap:10px; }
.step-rail { width:12px; display:flex; justify-content:center; position:relative; }
.step-rail::before {
  content:''; position:absolute; top:0; bottom:0; width:1px; background:var(--color-border);
}
.step:first-child .step-rail::before { top:9px; }
.step:last-child .step-rail::before { bottom:calc(100% - 9px); }
.step-dot {
  position:relative; z-index:1; width:9px; height:9px; border-radius:50%; margin-top:5px;
  background:var(--color-surface); border:2px solid var(--color-primary);
}
.step-dot.bad { border-color:var(--color-danger); background:var(--color-danger); }
.step-body { flex:1; min-width:0; padding-bottom:10px; }
.step-head {
  display:flex; align-items:center; gap:8px; width:100%; text-align:left;
  background:var(--color-surface); border:1px solid var(--color-border);
  border-radius:var(--radius-md); padding:6px 10px; cursor:pointer; font-size:12.5px;
  color:var(--color-text); transition:all var(--transition);
}
.step-head:hover { border-color:var(--color-primary); }
.step-icon { flex-shrink:0; }
.step-name { font-weight:600; }
.step-off { color:var(--color-text-muted); font-family:var(--font-mono); font-size:11px; }
.step-dur { color:var(--color-success); font-family:var(--font-mono); font-size:11px; }
.caret { margin-left:auto; color:var(--color-text-muted); font-size:10px; }
.badge { font-size:10px; padding:1px 7px; border-radius:var(--radius-full); }
.badge.bad { background:#fee2e2; color:#b91c1c; }
.detail-json {
  margin:6px 0 0; padding:10px 12px; font-size:11.5px; line-height:1.65;
  font-family:var(--font-mono); color:var(--color-text-sub);
  background:var(--color-surface); border:1px solid var(--color-border-light);
  border-left:2px solid var(--color-primary);
  border-radius:var(--radius-sm); max-height:420px; overflow:auto;
  white-space:pre-wrap; word-break:break-word;
}
.k-error .step-dot { border-color:var(--color-danger); background:var(--color-danger); }
@media (max-width: 1100px) { .run-list { width:280px; } }
</style>
