<template>
  <!-- frontend/src/components/rag/DocumentUploader.vue
       上传入库：文件/粘贴文本 + 必填元数据表单（AI 可预填，人工必须确认）。
       元数据结构决定了后续 RAG 的权限过滤与版本过滤能力，所以是必填而非可选。 -->
  <div class="uploader">
    <div class="head">
      <div class="title">文档入库</div>
      <div class="modes">
        <button class="mode" :class="{ active: mode === 'file' }" @click="mode = 'file'">文件</button>
        <button class="mode" :class="{ active: mode === 'text' }" @click="mode = 'text'">粘贴文本</button>
      </div>
    </div>

    <!-- 文件选择 -->
    <div v-if="mode === 'file'"
         class="drop" :class="{ over: dragging, filled: !!file }"
         @dragover.prevent="dragging = true" @dragleave="dragging = false"
         @drop.prevent="onDrop" @click="fileInput?.click()">
      <input ref="fileInput" type="file" accept=".txt,.md,.markdown,.pdf" hidden @change="onPick" />
      <template v-if="!file">
        <div class="drop-icon">📂</div>
        <div class="drop-text">拖拽文件到此处，或点击选择</div>
        <div class="drop-sub">支持 .pdf / .md / .txt，单个不超过 10MB</div>
      </template>
      <template v-else>
        <div class="file-row">
          <span class="file-icon">{{ fileIcon }}</span>
          <span class="file-meta">
            <b>{{ file.name }}</b>
            <small>{{ formatSize(file.size) }}</small>
          </span>
          <button class="icon-btn" @click.stop="clearFile" title="移除">×</button>
        </div>
      </template>
    </div>

    <!-- 粘贴文本 -->
    <textarea v-else v-model="textContent" class="input textarea"
              rows="5" placeholder="在此粘贴文档内容（将保存为 .txt 源文件）" />

    <!-- 元数据表单 -->
    <div class="meta-head">
      <span>元数据</span>
      <button class="ai-btn" :disabled="suggesting || !canSuggest" @click="aiFill">
        <span v-if="suggesting" class="spinner-sm" />
        {{ suggesting ? 'AI 识别中...' : '✨ AI 预填' }}
      </button>
    </div>
    <div v-if="suggestReason" class="ai-reason">AI 依据：{{ suggestReason }}</div>

    <div class="grid">
      <label class="field span2">
        <span class="label">文档标题 <i>*</i></span>
        <input v-model="meta.document_title" class="input" placeholder="如：员工年假管理规定" />
      </label>

      <label class="field">
        <span class="label">归属部门 <i>*</i></span>
        <select v-model="meta.department" class="input">
          <option value="">请选择</option>
          <option v-for="d in options.departments" :key="d.value" :value="d.value">{{ d.label }}</option>
        </select>
      </label>

      <label class="field">
        <span class="label">文档类型 <i>*</i></span>
        <select v-model="meta.doc_type" class="input">
          <option value="">请选择</option>
          <option v-for="t in options.docTypes" :key="t.value" :value="t.value">{{ t.label }}</option>
        </select>
      </label>

      <label class="field">
        <span class="label">版本号 <i>*</i></span>
        <input v-model="meta.version" class="input" placeholder="2026-08 或 v1.2" />
      </label>

      <label class="field">
        <span class="label">密级 <i>*</i></span>
        <select v-model="meta.security_level" class="input">
          <option v-for="l in options.securityLevels" :key="l.value" :value="l.value">{{ l.label }}</option>
        </select>
      </label>

      <label class="field">
        <span class="label">生效日期 <i>*</i></span>
        <input v-model="meta.effective_date" type="date" class="input" />
      </label>

      <label class="field">
        <span class="label">失效日期<span class="tip-inline">（默认"无" = 长期有效）</span></span>
        <!-- 原生 date 控件的占位符永远是"年/月/日"，改成"无"只能盖一层提示：
             留空即长期有效，比让人对着 yyyy/mm/dd 猜要清楚 -->
        <div class="date-field">
          <input v-model="meta.expired_date" type="date" class="input" />
          <span v-if="!meta.expired_date" class="date-ph">无</span>
        </div>
      </label>

      <label class="field">
        <span class="label">责任人</span>
        <input v-model="meta.owner" class="input" placeholder="如：人力资源部" />
      </label>

      <label class="field">
        <span class="label">标签</span>
        <input v-model="meta.tags" class="input" placeholder="逗号分隔，如：年假,考勤" />
      </label>

      <label class="field span2">
        <span class="label">备注</span>
        <input v-model="meta.remark" class="input" placeholder="可选" />
      </label>
    </div>

    <!-- 可见范围与有效期预览：不填的字段会造成什么后果，入库前先说清楚 -->
    <div class="preview" :class="{ wide: visibility.wide }">
      <div class="pv-row">
        <span class="pv-label">入库后谁能看</span>
        <span class="pv-value" :class="{ warn: visibility.wide }">{{ visibility.text }}</span>
      </div>
      <div class="pv-row">
        <span class="pv-label">有效期</span>
        <span class="pv-value">{{ validity.text }}</span>
      </div>
      <div v-if="visibility.wide || validity.neverExpires" class="pv-note">
        {{ visibility.wide ? '⚠ 「全员通用」或「公开」意味着所有身份都能检索到这份文档；' : '' }}
        {{ validity.neverExpires ? '⚠ 未填失效日期时文档长期有效，旧制度不会被自动淘汰。' : '' }}
      </div>
    </div>

    <div v-if="error" class="error">{{ error }}</div>

    <div v-if="missing.length" class="missing">还差：{{ missing.join('、') }}</div>

    <div v-if="knStore.uploading" class="progress">
      <div class="progress-bar"><div class="progress-fill" :style="{ width: knStore.uploadProgress + '%' }" /></div>
      <div class="progress-text">{{ knStore.uploadStage }} · {{ knStore.uploadProgress }}%（解析→分片→向量化在同一请求内完成）</div>
    </div>

    <!-- 提交按钮固定在卡片底部：表单很长时也一定看得见 -->
    <div class="submit-bar">
      <button class="btn-primary" :disabled="knStore.uploading || !canSubmit" @click="submit">
        {{ knStore.uploading ? '处理中...' : '📥 入库' }}
      </button>
    </div>
    <div class="hint">
      同一文件（内容 SHA256 相同）重复上传会命中幂等直接复用，不重复向量化、不重复计费（会询问是否强制入库一份新的）；
      同标题同类型同部门上传新版本时，旧版本会自动标记为「已被替代」。
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useKnowledgeStore } from '@/stores/knowledge.js'
import { useAppStore } from '@/stores/app.js'

const knStore = useKnowledgeStore()
const appStore = useAppStore()
const options = computed(() => knStore.options)

const mode = ref('file')
const file = ref(null)
const textContent = ref('')
const dragging = ref(false)
const fileInput = ref(null)
const suggesting = ref(false)
const suggestReason = ref('')
const error = ref('')

const today = new Date().toISOString().slice(0, 10)
const thisMonth = today.slice(0, 7)

const meta = reactive({
  document_title: '', department: '', doc_type: '', version: thisMonth,
  security_level: 'internal', effective_date: today, expired_date: '',
  owner: '', tags: '', remark: '',
})

const fileIcon = computed(() => (file.value?.name.split('.').pop() || '').toLowerCase() === 'pdf' ? '📕' : '📄')
const canSuggest = computed(() => !!(file.value || textContent.value.trim()))
const canSubmit = computed(() => missing.value.length === 0)

// 还缺哪些必填项：按钮置灰时直接写出来，而不是让人猜（原来只禁用不解释）
const missing = computed(() => {
  const miss = []
  if (mode.value === 'file' && !file.value) miss.push('选择文件')
  if (mode.value === 'text' && !textContent.value.trim()) miss.push('粘贴文本内容')
  if (!meta.document_title.trim()) miss.push('文档标题')
  if (!meta.department) miss.push('归属部门')
  if (!meta.doc_type) miss.push('文档类型')
  if (!meta.version.trim()) miss.push('版本号')
  if (!meta.effective_date) miss.push('生效日期')
  return miss
})

const deptLabel = computed(() => {
  const found = (options.value.departments || []).find(d => d.value === meta.department)
  return found?.label || meta.department
})

// 可见范围：把"部门 × 密级"翻译成一句人话，避免"以为填了其实全员可见"
const visibility = computed(() => {
  if (!meta.department) return { text: '⚠ 未选择归属部门，无法入库', wide: false }
  if (meta.security_level === 'public') return { text: '全员可见（密级=公开）', wide: true }
  if (meta.security_level === 'confidential') {
    return { text: `仅「${deptLabel.value}」且持机密授权的身份可见`, wide: false }
  }
  if (meta.department === 'general') {
    return { text: '全员可见（部门=全员通用 + 密级=内部）', wide: true }
  }
  return { text: `仅「${deptLabel.value}」成员可见（密级=内部）`, wide: false }
})

// 有效期：留空失效日期 = 长期有效，这里显式说明
const validity = computed(() => {
  if (!meta.effective_date) return { text: '未填生效日期', neverExpires: false }
  if (!meta.expired_date) return { text: `自 ${meta.effective_date} 起长期有效`, neverExpires: true }
  return { text: `${meta.effective_date} ~ ${meta.expired_date}`, neverExpires: false }
})

function formatSize(b) {
  if (!b) return '0 B'
  if (b < 1024) return b + ' B'
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB'
  return (b / 1048576).toFixed(1) + ' MB'
}

function onDrop(e) {
  dragging.value = false
  const f = e.dataTransfer.files?.[0]
  if (f) setFile(f)
}
function onPick(e) { const f = e.target.files?.[0]; if (f) setFile(f) }
function setFile(f) {
  const ext = f.name.split('.').pop().toLowerCase()
  if (!['pdf', 'md', 'markdown', 'txt'].includes(ext)) {
    appStore.toast.warning('只支持 .pdf / .md / .txt')
    return
  }
  if (f.size > 10 * 1024 * 1024) { appStore.toast.warning('文件不能超过 10MB'); return }
  file.value = f
  error.value = ''
  if (!meta.document_title) meta.document_title = f.name.replace(/\.[^.]+$/, '')
}
function clearFile() { file.value = null; if (fileInput.value) fileInput.value.value = '' }

async function aiFill() {
  suggesting.value = true
  suggestReason.value = ''
  try {
    const suggestion = await knStore.suggestMetadata(
      file.value?.name || (meta.document_title ? meta.document_title + '.txt' : ''),
      textContent.value.slice(0, 1500),
    )
    Object.assign(meta, {
      document_title: suggestion.document_title || meta.document_title,
      department: suggestion.department || meta.department,
      doc_type: suggestion.doc_type || meta.doc_type,
      security_level: suggestion.security_level || meta.security_level,
      version: suggestion.version || meta.version,
      effective_date: suggestion.effective_date || meta.effective_date,
      tags: (suggestion.tags || []).join(','),
    })
    suggestReason.value = suggestion.reason || ''
    appStore.toast.success('已预填，请人工确认后入库')
  } catch (err) {
    appStore.toast.error(err?.response?.data?.error?.message || 'AI 预填失败，请手动填写')
  } finally {
    suggesting.value = false
  }
}

async function submit() {
  error.value = ''
  try {
    const doc = await knStore.upload({
      file: mode.value === 'file' ? file.value : null,
      content: mode.value === 'text' ? textContent.value : null,
      meta: { ...meta },
    })
    if (doc) {
      clearFile()
      textContent.value = ''
      suggestReason.value = ''
      meta.document_title = ''; meta.tags = ''; meta.remark = ''
      await knStore.selectDocument(doc.doc_id)
    }
  } catch (err) {
    error.value = err?.message || '入库失败'
  }
}

onMounted(() => { if (!knStore.options.departments.length) knStore.loadOptions() })
</script>

<style scoped>
.uploader { display: flex; flex-direction: column; gap: 10px; }
.head { display: flex; align-items: center; justify-content: space-between; }
.title { font-size: 13px; font-weight: 600; color: var(--color-text); }
.modes { display: flex; background: var(--color-bg); border-radius: var(--radius-md); padding: 2px; }
.mode {
  border: none; background: none; cursor: pointer; font-size: 11.5px;
  padding: 3px 10px; border-radius: var(--radius-sm); color: var(--color-text-sub);
}
.mode.active { background: var(--color-surface); color: var(--color-primary); font-weight: 600; box-shadow: var(--shadow-sm); }

.drop {
  border: 1.5px dashed var(--color-border); border-radius: var(--radius-md);
  padding: 16px 12px; text-align: center; cursor: pointer; transition: var(--transition);
  background: var(--color-bg);
}
.drop:hover, .drop.over { border-color: var(--color-primary); background: var(--color-primary-bg); }
.drop.filled { border-style: solid; padding: 10px; text-align: left; }
.drop-icon { font-size: 22px; }
.drop-text { font-size: 12.5px; color: var(--color-text-sub); margin-top: 4px; }
.drop-sub { font-size: 11px; color: var(--color-text-muted); margin-top: 2px; }
.file-row { display: flex; align-items: center; gap: 8px; }
.file-icon { font-size: 18px; }
.file-meta { display: flex; flex-direction: column; flex: 1; min-width: 0; }
.file-meta b { font-size: 12.5px; color: var(--color-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-meta small { font-size: 11px; color: var(--color-text-muted); }
.icon-btn { border: none; background: none; cursor: pointer; color: var(--color-text-muted); font-size: 16px; }

.meta-head { display: flex; align-items: center; justify-content: space-between; margin-top: 4px; }
.meta-head > span { font-size: 12px; font-weight: 600; color: var(--color-text-sub); }
.ai-btn {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 11.5px; padding: 3px 9px; cursor: pointer;
  border: 1px solid var(--color-primary); color: var(--color-primary);
  background: var(--color-primary-bg); border-radius: var(--radius-full);
}
.ai-btn:disabled { opacity: .5; cursor: not-allowed; }
.ai-reason {
  font-size: 11px; color: var(--color-primary-dark); background: var(--color-primary-bg);
  border-radius: var(--radius-sm); padding: 5px 8px;
}

.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.field { display: flex; flex-direction: column; gap: 3px; }
.field.span2 { grid-column: span 2; }
.label { font-size: 11px; color: var(--color-text-sub); }
.label i { color: var(--color-danger); font-style: normal; }
.input {
  border: 1px solid var(--color-border); border-radius: var(--radius-sm);
  padding: 5px 8px; font-size: 12px; color: var(--color-text);
  background: var(--color-surface); outline: none; width: 100%;
}
.input:focus { border-color: var(--color-primary); }
.textarea { resize: vertical; font-family: var(--font-sans); }

/* 失效日期留空时显示"无"；右边 28px 留给原生日历图标 */
.date-field { position: relative; }
.date-ph {
  position: absolute; left: 0; top: 0; bottom: 0; right: 28px;
  display: flex; align-items: center; padding-left: 8px;
  background: var(--color-surface); color: var(--color-text-muted);
  font-size: 12px; border-radius: var(--radius-sm);
  pointer-events: none;
}

.error { font-size: 11.5px; color: var(--color-danger); }
.progress { display: flex; flex-direction: column; gap: 4px; }
.progress-bar { height: 5px; background: var(--color-border-light); border-radius: var(--radius-full); overflow: hidden; }
.progress-fill { height: 100%; background: var(--color-primary); transition: width .2s; }
.progress-text { font-size: 11px; color: var(--color-text-muted); }

.preview {
  border: 1px solid var(--color-border-light); background: var(--color-bg);
  border-radius: var(--radius-md); padding: 7px 10px; display: flex; flex-direction: column; gap: 3px;
}
.preview.wide { border-color: #fde68a; background: #fffbeb; }
.pv-row { display: flex; gap: 8px; font-size: 11.5px; }
.pv-label { color: var(--color-text-muted); min-width: 76px; flex-shrink: 0; }
.pv-value { color: var(--color-text); font-weight: 600; }
.pv-value.warn { color: #b45309; }
.pv-note { font-size: 10.5px; color: #b45309; line-height: 1.5; }

.missing { font-size: 11.5px; color: var(--color-warning); }

.submit-bar {
  position: sticky; bottom: 0; z-index: 2;
  padding-top: 8px; background: var(--color-surface);
}
.tip-inline { color: var(--color-text-muted); font-weight: 400; }

.btn-primary {
  border: none; cursor: pointer; padding: 8px; border-radius: var(--radius-md);
  background: var(--color-primary); color: #fff; font-size: 13px; font-weight: 600;
  transition: var(--transition);
}
.btn-primary:hover:not(:disabled) { background: var(--color-primary-dark); }
.btn-primary:disabled { opacity: .5; cursor: not-allowed; }
.hint { font-size: 11px; color: var(--color-text-muted); line-height: 1.5; }
.spinner-sm {
  width: 10px; height: 10px; border: 2px solid currentColor; border-top-color: transparent;
  border-radius: 50%; animation: spin .7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
</style>
