<!-- frontend/src/views/WorkflowView.vue -->
<template>
  <div class="workflow-view">
    <!-- 左侧：模板选择 + 流程图 -->
    <aside class="wf-sidebar" ref="sidebarEl">
      <div class="template-section">
        <div class="section-label">选择工作流模板</div>
        <div class="template-grid">
          <div
            v-for="t in wfStore.templates"
            :key="t.id"
            class="template-card"
            :class="{ active: wfStore.selectedTemplate === t.id }"
            @click="selectAndReset(t.id)"
          >
            <!-- <span class="tpl-icon">{{ t.icon }}</span> -->
            <div class="tpl-info">
              <div class="tpl-title">{{ t.title }}</div>
              <div class="tpl-desc">{{ t.desc }}</div>
            </div>
          </div>
        </div>
      </div>

      <div v-if="currentMeta" class="graph-section" ref="graphEl">
        <div class="section-label">执行流程</div>
        <WorkflowGraph :nodes="currentMeta.nodes" />
      </div>
    </aside>

    <!-- 右侧：输入/审核/结果 -->
    <main class="wf-main" ref="mainEl">
      <!-- 未选模板 -->
      <div v-if="!wfStore.selectedTemplate" class="empty-state">
        <div class="empty-icon">⚙️</div>
        <div class="empty-title">选择一个工作流模板</div>
        <div class="empty-desc">从左侧选择工作流类型，然后输入内容开始执行</div>
      </div>

      <template v-else>
        <!-- 输入阶段 -->
        <div v-if="!wfStore.running && !wfStore.paused && !wfStore.result" class="input-phase">
          <div class="phase-title">
            <!-- <span class="phase-icon">{{ currentMeta.icon }}</span> -->
            {{ currentMeta.title }}
          </div>

          <div v-if="currentMeta.extraField" class="form-field">
            <label class="field-label">{{ currentMeta.extraField.label }}</label>
            <input v-model="extraValue" class="input" :placeholder="currentMeta.extraField.placeholder" />
          </div>

          <div class="form-field">
            <label class="field-label">{{ currentMeta.inputLabel }}</label>
            <textarea v-model="mainInput" class="input" :placeholder="currentMeta.inputPlaceholder" rows="8" />
            <div class="char-count">{{ mainInput.length }} 字</div>
          </div>

          <button class="btn btn-primary start-btn" @click="startWorkflow" :disabled="!mainInput.trim()">
            ▶ 开始执行
          </button>
        </div>

        <!-- 执行中：不只转圈，把"走到哪一步了"摊开，用户不用去左边找流程图 -->
        <div v-if="wfStore.running && !wfStore.paused && !wfStore.streamBuffer" class="running-phase">
          <div class="running-header">
            <div class="spinner" />
            <span>工作流执行中，请稍候...</span>
          </div>
          <ul class="running-steps">
            <li v-for="n in currentMeta.nodes" :key="n.id" :class="wfStore.nodeStates[n.id] || 'idle'">
              <span class="rs-dot" />
              <span class="rs-label">{{ n.label }}</span>
              <span class="rs-state">{{ stateText(wfStore.nodeStates[n.id], n.isHuman) }}</span>
              <span v-if="wfStore.nodeOutputs[n.id]" class="rs-preview">{{ wfStore.nodeOutputs[n.id] }}</span>
            </li>
          </ul>
        </div>

        <!-- 人工审核 -->
        <div v-if="wfStore.paused" class="review-phase">
          <HumanReviewPanel @approve="resumeWithFeedback" @abort="handleAbort" />
        </div>

        <!-- 流式输出最终内容 -->
        <div v-if="wfStore.running && wfStore.streamBuffer" class="streaming-phase">
          <div class="streaming-label">正在生成最终内容...</div>
          <div class="streaming-content markdown-body" v-html="renderMd(wfStore.streamBuffer)" />
          <span class="cursor-blink" />
        </div>

        <!-- 结果展示 -->
        <div v-if="wfStore.result && !wfStore.running" class="result-phase">
          <div class="result-header">
            <span>✅ 生成完成</span>
            <div class="result-actions">
              <button class="btn btn-ghost btn-sm" @click="copyResult">复制内容</button>
              <button class="btn btn-ghost btn-sm" @click="restart">重新开始</button>
            </div>
          </div>
          <div class="result-content markdown-body" v-html="renderMd(wfStore.result)" />
        </div>
      </template>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { marked } from 'marked'
import hljs from 'highlight.js'
import { useWorkflowStore } from '@/stores/workflow.js'
import { useAppStore } from '@/stores/app.js'
import WorkflowGraph from '@/components/workflow/WorkflowGraph.vue'
import HumanReviewPanel from '@/components/workflow/HumanReviewPanel.vue'

const wfStore  = useWorkflowStore()
const appStore = useAppStore()

const mainInput  = ref('')
const extraValue = ref('')

// 自动滚动：点开始执行后把左侧流程图滚进视野（流程在动，用户得看得见），
// 右侧开始流式输出时再把主面板滚到底部，跟着文字走。
const sidebarEl = ref(null)
const graphEl   = ref(null)
const mainEl    = ref(null)

function stateText(state, isHuman) {
  if (state === 'running') return '执行中'
  if (state === 'done')    return '完成'
  if (state === 'waiting') return '等待审核'
  return isHuman ? '待审核' : '等待'
}

watch(() => wfStore.running, async (running) => {
  if (!running) return
  await nextTick()
  graphEl.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  mainEl.value?.scrollTo({ top: 0, behavior: 'smooth' })
})

// 流式输出时跟随到底部（用户手动往上翻时不抢滚动条）
watch(() => wfStore.streamBuffer.length, async () => {
  const el = mainEl.value
  if (!el) return
  const stick = el.scrollHeight - el.scrollTop - el.clientHeight < 200
  await nextTick()
  if (stick) el.scrollTop = el.scrollHeight
})

marked.setOptions({
  highlight: (c, l) => l && hljs.getLanguage(l) ? hljs.highlight(c, { language: l }).value : c,
  breaks: true,
})
function renderMd(t) { try { return marked(t || '') } catch { return t } }

const currentMeta = computed(() =>
  wfStore.templates.find(t => t.id === wfStore.selectedTemplate) || null
)

function selectAndReset(id) {
  wfStore.selectTemplate(id)
  mainInput.value = ''
  extraValue.value = ''
}

async function startWorkflow() {
  if (!mainInput.value.trim() || !currentMeta.value) return

  const fieldMaps = {
    weekly_report:   { mainKey: 'points',      extraKey: 'dept' },
    meeting_minutes: { mainKey: 'rawNotes',    extraKey: 'meetingTitle' },
    email_polish:    { mainKey: 'draft',       extraKey: 'recipient' },
    prd_skeleton:    { mainKey: 'description', extraKey: null },
  }

  const m = fieldMaps[wfStore.selectedTemplate] || { mainKey: 'input', extraKey: null }
  const payload = { [m.mainKey]: mainInput.value }
  if (m.extraKey && extraValue.value.trim()) payload[m.extraKey] = extraValue.value

  await wfStore.startWorkflow(payload)
}

async function resumeWithFeedback(feedback) {
  await wfStore.resumeWorkflow(feedback)
}

function handleAbort() {
  wfStore.reset()
  mainInput.value = ''
  extraValue.value = ''
}

async function copyResult() {
  await navigator.clipboard.writeText(wfStore.result)
  appStore.toast.success('已复制到剪贴板')
}

function restart() {
  wfStore.reset()
}

onMounted(() => wfStore.loadTemplates())
</script>

<style scoped>
.workflow-view { display:flex; height:100%; overflow:hidden; background:var(--color-bg); }

/* 左侧 */
.wf-sidebar { width:280px; flex-shrink:0; background:var(--color-surface); border-right:1px solid var(--color-border); overflow-y:auto; display:flex; flex-direction:column; }
.template-section, .graph-section { padding:var(--space-md); border-bottom:1px solid var(--color-border-light); }
.section-label { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--color-text-muted); margin-bottom:var(--space-sm); }
.template-grid { display:flex; flex-direction:column; gap:6px; }
.template-card { display:flex; align-items:flex-start; gap:10px; padding:10px 12px; border-radius:var(--radius-lg); border:1.5px solid var(--color-border); cursor:pointer; transition:all var(--transition); background:var(--color-bg); }
.template-card:hover { border-color:var(--color-primary); }
.template-card.active { border-color:var(--color-primary); background:var(--color-primary-bg); }
.tpl-icon { font-size:18px; flex-shrink:0; }
.tpl-title { font-size:12px; font-weight:600; color:var(--color-text); }
.tpl-desc { font-size:11px; color:var(--color-text-muted); margin-top:2px; line-height:1.4; }

/* 右侧 */
/* 执行中：节点进度列表（不用去左侧流程图也能看清走到哪了） */
.running-steps { list-style:none; margin:16px 0 0; padding:0; display:flex; flex-direction:column; gap:6px; }
.running-steps li { display:grid; grid-template-columns:10px 1fr auto; gap:10px; align-items:center;
  padding:8px 12px; border:1px solid var(--color-border-light); border-radius:var(--radius-md);
  font-size:12.5px; color:var(--color-text-muted); background:var(--color-surface); }
.running-steps li.running { border-color:var(--color-primary); color:var(--color-text); }
.running-steps li.done { color:var(--color-text-sub); }
.running-steps li.waiting { border-color:var(--color-warning); color:#b45309; }
.rs-dot { width:8px; height:8px; border-radius:50%; background:var(--color-border); }
.running-steps li.running .rs-dot { background:var(--color-primary); animation:pulse 1s infinite; }
.running-steps li.done .rs-dot { background:var(--color-success); }
.running-steps li.waiting .rs-dot { background:var(--color-warning); }
.rs-state { font-size:11px; }
.rs-preview { grid-column:2 / 4; font-size:11px; color:var(--color-text-muted);
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
@keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:.35 } }

.wf-main { flex:1; overflow-y:auto; padding:var(--space-xl) var(--space-2xl); display:flex; flex-direction:column; }
.empty-state { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:10px; color:var(--color-text-muted); text-align:center; }
.empty-icon { font-size:48px; }
.empty-title { font-size:18px; font-weight:600; color:var(--color-text); }
.empty-desc { font-size:13px; max-width:360px; line-height:1.7; }
.phase-title { display:flex; align-items:center; gap:10px; font-size:16px; font-weight:700; color:var(--color-text); margin-bottom:var(--space-lg); }
.phase-icon { font-size:20px; }
.input-phase { display:flex; flex-direction:column; gap:var(--space-md); max-width:680px; }
.form-field { display:flex; flex-direction:column; gap:6px; }
.field-label { font-size:13px; font-weight:600; color:var(--color-text); }
.char-count { font-size:11px; color:var(--color-text-muted); text-align:right; }
.start-btn { align-self:flex-end; padding:10px 28px; }
.running-phase { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:var(--space-lg); }
.running-header { display:flex; align-items:center; gap:12px; font-size:15px; font-weight:500; color:var(--color-text-sub); }
.review-phase { max-width:680px; }
.streaming-phase { max-width:680px; }
.streaming-label { font-size:12px; color:var(--color-text-muted); margin-bottom:10px; }
.streaming-content { background:var(--color-surface); border:1px solid var(--color-border); border-radius:var(--radius-lg); padding:var(--space-lg); font-size:14px; line-height:1.75; min-height:100px; }
.result-phase { max-width:680px; }
.result-header { display:flex; align-items:center; gap:10px; margin-bottom:var(--space-md); font-size:14px; font-weight:600; color:var(--color-text); }
.result-actions { display:flex; gap:6px; margin-left:auto; }
.btn-sm { padding:5px 12px; font-size:12px; }
.result-content { background:var(--color-surface); border:1px solid var(--color-border); border-radius:var(--radius-lg); padding:var(--space-xl); font-size:14px; line-height:1.8; }
</style>
