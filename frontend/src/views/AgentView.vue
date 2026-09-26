<!-- frontend/src/views/AgentView.vue -->
<template>
  <div class="agent-view">
    <aside class="task-panel">
      <div class="panel-header">
        <span class="panel-title">任务 Agent</span>
        <button v-if="agentStore.tasks.length" class="btn-text-sm" @click="agentStore.clearTasks()">清空</button>
      </div>
      <div class="task-input-area">
        <textarea v-model="taskText" class="task-textarea" placeholder="描述你的任务，Agent 会自动拆解步骤..." :disabled="agentStore.running" @keydown.ctrl.enter="runTask" rows="4" />
        <div class="kb-switch">
          <span class="kb-label">知识库</span>
          <button class="kb-opt" :class="{ active: knowledgeMode === 'auto' }"
                  @click="knowledgeMode = 'auto'" title="自动：默认检索（与智能对话同一判定）">自动</button>
          <button class="kb-opt" :class="{ active: knowledgeMode === 'force' }"
                  @click="knowledgeMode = 'force'" title="强制检索：这次一定走知识库分支">强制</button>
          <button class="kb-opt" :class="{ active: knowledgeMode === 'off' }"
                  @click="knowledgeMode = 'off'" title="关闭：不让 Agent 查知识库">关闭</button>
        </div>
        <div class="input-actions">
          <span class="hint">Ctrl+Enter 执行</span>
          <button class="btn btn-primary" @click="runTask" :disabled="!taskText.trim() || agentStore.running">{{ agentStore.running ? '执行中...' : '执行任务' }}</button>
        </div>
      </div>
      <div class="examples-section">
        <div class="section-label">示例任务</div>
        <div v-for="ex in agentStore.examples" :key="ex.title" class="example-item" @click="useExample(ex.task)" :class="{ disabled: agentStore.running }">
          <div class="ex-content">
            <div class="ex-title">{{ ex.title }}</div>
            <div class="ex-desc">{{ ex.task.slice(0, 40) }}...</div>
          </div>
        </div>
      </div>
      <div class="tools-section">
        <div class="section-label">可用工具（{{ agentStore.toolList.length }}）</div>
        <div class="tool-chips">
          <div v-for="t in agentStore.toolList" :key="t.name" class="tool-chip" :title="t.description">{{ t.label }}</div>
        </div>
      </div>
    </aside>
    <main class="execution-panel" ref="taskListEl">
      <div v-if="!agentStore.tasks.length" class="empty-state">
        <div class="empty-title">任务执行 Agent</div>
        <div class="empty-desc">在左侧输入任务，Agent 会自动规划步骤，调用合适的工具完成</div>
        <div class="feature-tags">
          <span class="tag tag-blue">联网搜索</span>
          <span class="tag tag-green">知识库检索</span>
          <span class="tag tag-purple">数学计算</span>
          <span class="tag tag-amber">生成报告</span>
        </div>
      </div>
      <div v-else class="task-list">
        <div v-for="task in agentStore.tasks" :key="task.id" class="task-block">
          <div class="task-header">
            <div class="task-meta">
              <span class="task-status-dot" :class="`dot-${task.status}`" />
              <span class="task-index">任务 #{{ task.id }}</span>
              <span class="task-time">{{ formatTime(task.startTime) }}</span>
              <span v-if="task.duration" class="task-duration">{{ (task.duration / 1000).toFixed(1) }}s</span>
            </div>
            <div class="task-desc">{{ task.task }}</div>
          </div>
          <!-- 意图路由结果：这次任务走了哪条执行路径、为什么 -->
          <div v-if="task.intent" class="route-bar">
            <span class="route-tag" :class="task.intent.route">{{ task.intent.routeLabel || task.intent.route }}</span>
            <span class="route-reason">{{ task.intent.reason }}</span>
          </div>

          <!-- 走了知识分支但没命中：把原因摊开（库空 / 被权限过滤 / 分数低于阈值） -->
          <div v-if="task.recall && !task.sources?.length" class="agent-recall">
            知识库未命中：{{ task.recall.explain }}
            <span v-if="task.recall.totalChunks !== undefined">
              （库内切片 {{ task.recall.totalChunks }} · 通过过滤 {{ task.recall.candidates }}<template
                v-if="task.recall.bestScore != null"> · 最高分 {{ task.recall.bestScore }}</template><template
                v-if="task.recall.threshold != null"> · 阈值 {{ task.recall.threshold }}</template>）
            </span>
          </div>

          <!-- 知识库引用来源 -->
          <div v-if="task.sources?.length" class="agent-sources">
            <div v-for="(s, i) in task.sources" :key="s.chunkId" class="agent-source">
              <span class="as-idx">[{{ i + 1 }}]</span>
              <span class="as-title">{{ s.title }}</span>
              <span class="as-loc">{{ s.pageLabel || (s.pageNumber ? '第' + s.pageNumber + '页' : '无页码') }}</span>
              <span class="as-meta">{{ s.department }} · {{ s.version }}</span>
              <span class="as-score">{{ (s.score * 100).toFixed(0) }}%</span>
            </div>
          </div>

          <div v-if="task.steps.length" class="steps-list">
            <ToolCallCard v-for="step in task.steps" :key="step.id" :step="step" />
          </div>
          <div v-if="task.status === 'running' && !task.steps.length" class="thinking-hint">
            <div class="spinner" /><span>Agent 正在思考...</span>
          </div>
          <div v-if="task.answer" class="final-answer">
            <div class="answer-header">
              <span>最终回答</span>
              <button class="btn-copy" @click="copyAnswer(task.answer)">复制</button>
            </div>
            <div class="answer-content markdown-body" v-html="renderMd(task.answer)" />
            <span v-if="task.status === 'running' && task.answer" class="cursor-blink" />
          </div>
          <div v-if="task.status === 'error'" class="error-hint">{{ task.answer || '任务执行失败，请重试' }}</div>
        </div>
      </div>
    </main>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { marked } from 'marked'
import hljs from 'highlight.js'
import { useAgentStore } from '@/stores/agent.js'
import { useAppStore } from '@/stores/app.js'
import ToolCallCard from '@/components/agent/ToolCallCard.vue'

const agentStore = useAgentStore()
const appStore   = useAppStore()
const taskText   = ref('')
// 知识库模式：auto=自动（后端"召回优先"）/ force=强制走知识分支 / off=不查知识库
const knowledgeMode = ref('auto')
const taskListEl = ref(null)

marked.setOptions({ highlight: (c, l) => l && hljs.getLanguage(l) ? hljs.highlight(c, { language: l }).value : c, breaks: true })
function renderMd(t) { try { return marked(t || '') } catch { return t } }
function formatTime(iso) { return iso ? new Date(iso).toLocaleTimeString('zh-CN', { hour:'2-digit', minute:'2-digit', second:'2-digit' }) : '' }
async function runTask() {
  if (!taskText.value.trim() || agentStore.running) return
  const t = taskText.value.trim()
  taskText.value = ''
  await agentStore.runTask(t, {
    useKnowledge: knowledgeMode.value === 'auto' ? undefined : knowledgeMode.value === 'force',
  })
}
function useExample(task) { if (!agentStore.running) taskText.value = task }
async function copyAnswer(text) { await navigator.clipboard.writeText(text); appStore.toast.success('已复制') }
onMounted(() => agentStore.loadMeta())
</script>
<style scoped>
/* 意图路由条 */
.route-bar {
  display: flex; align-items: center; gap: 8px;
  margin: 8px 0; padding: 6px 10px;
  background: var(--color-bg); border-left: 3px solid var(--color-primary);
  border-radius: var(--radius-sm); font-size: 11.5px;
}
.route-tag {
  font-weight: 600; padding: 1px 8px; border-radius: var(--radius-full);
  background: var(--color-primary-bg); color: var(--color-primary-dark);
}
.route-tag.tool { background: #e0e7ff; color: #4338ca; }
.route-tag.knowledge { background: #dcfce7; color: #15803d; }
.route-tag.chat { background: var(--color-border-light); color: var(--color-text-sub); }
.route-reason { color: var(--color-text-muted); }

/* 引用来源 */
.kb-switch { display: inline-flex; align-items: center; gap: 2px; margin: 6px 0 2px; }
.kb-label { font-size: 11px; color: var(--color-text-muted); margin-right: 4px; }
.kb-opt {
  border: 1px solid var(--color-border); background: transparent; color: var(--color-text-muted);
  font-size: 11px; padding: 1px 8px; border-radius: var(--radius-full); cursor: pointer;
  transition: all var(--transition);
}
.kb-opt:hover { border-color: var(--color-primary); color: var(--color-primary); }
.kb-opt.active {
  background: var(--color-primary-bg); border-color: var(--color-primary);
  color: var(--color-primary); font-weight: 600;
}

.agent-recall {
  font-size: 11.5px; line-height: 1.6; color: #b45309;
  background: #fffbeb; border: 1px solid #fde68a; border-radius: var(--radius-sm);
  padding: 6px 9px; margin-bottom: 8px;
}
.agent-sources { display: flex; flex-direction: column; gap: 4px; margin-bottom: 8px; }
.agent-source {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: 11px; padding: 4px 8px;
  background: var(--color-bg); border: 1px solid var(--color-border-light);
  border-radius: var(--radius-sm);
}
.as-idx { font-family: var(--font-mono); color: var(--color-primary); font-weight: 600; }
.as-title { font-weight: 600; color: var(--color-text); }
.as-loc { color: var(--color-text-muted); }
.as-meta { color: var(--color-primary-dark); background: var(--color-primary-bg); border-radius: var(--radius-full); padding: 0 6px; }
.as-score { margin-left: auto; color: var(--color-success); font-family: var(--font-mono); }

.agent-view { display:flex; height:100%; overflow:hidden; background:var(--color-bg); }
.task-panel { width:300px; flex-shrink:0; background:var(--color-surface); border-right:1px solid var(--color-border); display:flex; flex-direction:column; overflow-y:auto; }
.panel-header { display:flex; align-items:center; justify-content:space-between; padding:14px 16px 10px; border-bottom:1px solid var(--color-border-light); flex-shrink:0; }
.panel-title { font-size:14px; font-weight:700; color:var(--color-text); }
.btn-text-sm { font-size:11px; color:var(--color-text-muted); background:none; border:none; cursor:pointer; }
.task-input-area { padding:var(--space-md); border-bottom:1px solid var(--color-border-light); }
.task-textarea { width:100%; padding:10px 12px; background:var(--color-bg); border:1.5px solid var(--color-border); border-radius:var(--radius-lg); font-size:13px; line-height:1.65; color:var(--color-text); resize:none; box-sizing:border-box; font-family:inherit; transition:border-color var(--transition); }
.task-textarea:focus { border-color:var(--color-primary); outline:none; }
.task-textarea:disabled { opacity:.65; }
.input-actions { display:flex; align-items:center; justify-content:space-between; margin-top:8px; }
.hint { font-size:11px; color:var(--color-text-muted); }
.examples-section, .tools-section { padding:var(--space-md); }
.section-label { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--color-text-muted); margin-bottom:8px; }
.example-item { display:flex; gap:8px; padding:8px 10px; border-radius:var(--radius-md); cursor:pointer; transition:background var(--transition); margin-bottom:4px; }
.example-item:hover:not(.disabled) { background:var(--color-border-light); }
.example-item.disabled { opacity:.5; cursor:not-allowed; }
.ex-title { font-size:12px; font-weight:600; color:var(--color-text); margin-bottom:2px; }
.ex-desc  { font-size:11px; color:var(--color-text-muted); }
.tool-chips { display:flex; flex-wrap:wrap; gap:5px; }
.tool-chip { display:inline-flex; align-items:center; gap:4px; font-size:11px; padding:3px 9px; background:var(--color-border-light); border:1px solid var(--color-border); border-radius:var(--radius-full); color:var(--color-text-sub); white-space:nowrap; }
.tool-chip .el-icon { font-size:12px; }
.execution-panel { flex:1; overflow-y:auto; padding:var(--space-lg) var(--space-xl); }
.empty-state { height:100%; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:10px; color:var(--color-text-muted); text-align:center; }
.empty-title { font-size:18px; font-weight:600; color:var(--color-text); }
.empty-desc { font-size:13px; max-width:380px; line-height:1.7; }
.feature-tags { display:flex; gap:8px; flex-wrap:wrap; justify-content:center; margin-top:4px; }
.task-list { display:flex; flex-direction:column; gap:var(--space-xl); }
.task-block { background:var(--color-surface); border:1px solid var(--color-border); border-radius:var(--radius-xl); overflow:hidden; }
.task-header { padding:14px 18px 12px; border-bottom:1px solid var(--color-border-light); background:var(--color-bg); }
.task-meta { display:flex; align-items:center; gap:8px; margin-bottom:6px; }
.task-status-dot { width:7px; height:7px; border-radius:50%; flex-shrink:0; }
.task-index { font-size:11px; font-weight:700; color:var(--color-text-muted); font-family:var(--font-mono); }
.task-time  { font-size:11px; color:var(--color-text-muted); }
.task-duration { font-size:11px; color:var(--color-success); font-weight:600; }
.task-desc { font-size:14px; font-weight:500; color:var(--color-text); line-height:1.6; }
.steps-list { padding:var(--space-md) var(--space-lg); display:flex; flex-direction:column; gap:8px; }
.thinking-hint { display:flex; align-items:center; gap:10px; padding:var(--space-md) var(--space-lg); font-size:13px; color:var(--color-text-muted); }
.final-answer { padding:var(--space-md) var(--space-lg) var(--space-lg); border-top:1px solid var(--color-border-light); }
.answer-header { display:flex; align-items:center; gap:8px; margin-bottom:10px; font-size:12px; font-weight:600; color:var(--color-text-sub); }
.btn-copy { margin-left:auto; padding:2px 10px; font-size:11px; background:var(--color-border-light); border:1px solid var(--color-border); border-radius:var(--radius-sm); color:var(--color-text-sub); cursor:pointer; transition:all var(--transition); }
.btn-copy:hover { background:var(--color-primary-bg); color:var(--color-primary); }
.answer-content { font-size:14px; line-height:1.75; color:var(--color-text); }
.error-hint { display:flex; align-items:center; gap:6px; padding:var(--space-md) var(--space-lg); color:var(--color-danger); font-size:13px; background:#fef2f2; border-top:1px solid #fecaca; }
</style>
