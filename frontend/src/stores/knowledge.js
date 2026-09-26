// frontend/src/stores/knowledge.js
// 知识库模块状态：文档资产管理（上传 / 元数据 / 列表 / 详情 / 删除 / 检索验证）。
// 注意：知识库页面不再提供对话能力；检索能力由「智能对话」和「任务 Agent」消费。
import { defineStore } from 'pinia'
import { ref, reactive } from 'vue'
import http from '@/utils/http.js'
import { useAppStore } from './app.js'
import { useIdentityStore } from './identity.js'

export const useKnowledgeStore = defineStore('knowledge', () => {
  const appStore = useAppStore()
  const identity = useIdentityStore()

  // ── 字典与统计 ────────────────────────────────────────────────
  const options = ref({ departments: [], docTypes: [], securityLevels: [], docStatuses: [], versions: [] })
  const stats = ref({})
  const loading = ref(false)

  // ── 列表与筛选 ────────────────────────────────────────────────
  const documents = ref([])
  const filters = reactive({
    department: '', docType: '', status: '', version: '', keyword: '', includeSuperseded: true,
  })

  // ── 上传 ──────────────────────────────────────────────────────
  const uploading = ref(false)
  const uploadProgress = ref(0)
  const uploadStage = ref('')

  // ── 详情 ──────────────────────────────────────────────────────
  const selectedId = ref('')
  const detail = ref(null)
  const detailLoading = ref(false)

  // ── 检索验证（工程工具：验证"权限 + 版本"过滤真的生效）─────────
  const searchPreview = reactive({
    question: '', loading: false, hits: [], diagnostics: null, appliedFilters: null, error: '',
  })

  function errMsg(err, fallback = '操作失败') {
    return err?.response?.data?.error?.message || err?.message || fallback
  }

  async function loadOptions() {
    try {
      const data = await http.get('/knowledge/options')
      options.value = data
    } catch (err) { appStore.toast.error(errMsg(err, '加载字典失败')) }
  }

  async function loadStats() {
    try { stats.value = await http.get('/knowledge/stats') } catch { /* 统计失败不打断页面 */ }
  }

  async function loadDocuments() {
    loading.value = true
    try {
      const params = new URLSearchParams()
      Object.entries(filters).forEach(([k, v]) => {
        if (v !== '' && v !== null && v !== undefined) params.append(k, v)
      })
      const data = await http.get(`/knowledge/documents?${params.toString()}`)
      documents.value = data.documents || []
      stats.value = { ...stats.value, visible: data.stats }
      // 选中的文档被过滤掉了就清空详情，避免"详情与列表不一致"
      if (selectedId.value && !documents.value.some(d => d.doc_id === selectedId.value)) {
        selectedId.value = ''; detail.value = null
      }
    } catch (err) {
      appStore.toast.error(errMsg(err, '加载文档列表失败'))
    } finally {
      loading.value = false
    }
  }

  async function refresh() {
    await Promise.all([loadOptions(), loadDocuments(), loadStats()])
  }

  // ── AI 预填元数据（结果只做建议，入库前必须人工确认）────────────
  async function suggestMetadata(fileName, snippet = '') {
    const data = await http.post('/knowledge/metadata/suggest', { fileName, snippet })
    return data.suggestion
  }

  // ── 上传入库（用 XHR 以获得真实上传进度）────────────────────────
  async function upload({ file, content, meta, force = false }) {
    if (uploading.value) return null
    uploading.value = true
    uploadProgress.value = 0
    uploadStage.value = '上传中'

    const formData = new FormData()
    if (file) formData.append('file', file)
    if (content) formData.append('content', content)
    Object.entries(meta).forEach(([k, v]) => {
      if (v !== null && v !== undefined && v !== '') formData.append(k, v)
    })

    try {
      const result = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest()
        xhr.open('POST', '/api/knowledge/documents' + (force ? '?force=true' : ''))
        Object.entries(identity.headers()).forEach(([k, v]) => xhr.setRequestHeader(k, v))

        xhr.upload.addEventListener('progress', (e) => {
          if (e.lengthComputable) uploadProgress.value = Math.round((e.loaded / e.total) * 70)
        })
        xhr.addEventListener('load', () => {
          let payload = {}
          try { payload = JSON.parse(xhr.responseText) } catch { /* 忽略非 JSON 响应 */ }
          if (xhr.status >= 200 && xhr.status < 300) {
            uploadProgress.value = 100
            resolve(payload)
          } else {
            reject(new Error(payload?.error?.message || `上传失败（HTTP ${xhr.status}）`))
          }
        })
        xhr.addEventListener('error', () => reject(new Error('网络错误，上传失败')))
        xhr.send(formData)
      })

      // 内容重复时问一下要不要强制入库（测试时经常需要同一份文件按不同元数据各存一版）
      if (result.duplicated && !force) {
        uploading.value = false
        const again = confirm(
          `该文件内容已存在（《${result.document.document_title}》），已复用未重复计费。\n\n` +
          '是否仍然强制入库一份新的（按当前填写的元数据）？'
        )
        if (again) return await upload({ file, content, meta, force: true })
        return result.document
      }

      // 服务端在返回前已完成解析/分片/向量化，这里把阶段提示补全
      uploadStage.value = result.duplicated ? '命中幂等，已复用' : '入库完成'
      appStore.toast.success(
        result.duplicated
          ? result.message
          : `《${result.document.document_title}》入库完成：${result.document.chunk_count} 个切片 / ${result.document.page_count} 页`
      )
      await loadDocuments(); await loadStats()
      return result.document
    } catch (err) {
      uploadStage.value = ''
      appStore.toast.error(errMsg(err, '入库失败'))
      throw err
    } finally {
      uploading.value = false
      setTimeout(() => { uploadProgress.value = 0; uploadStage.value = '' }, 1200)
    }
  }

  // ── 详情 ──────────────────────────────────────────────────────
  async function selectDocument(docId) {
    if (!docId) { selectedId.value = ''; detail.value = null; return }
    selectedId.value = docId
    detailLoading.value = true
    try {
      detail.value = await http.get(`/knowledge/documents/${docId}?chunk_limit=100`)
    } catch (err) {
      appStore.toast.error(errMsg(err, '加载文档详情失败'))
      detail.value = null
    } finally {
      detailLoading.value = false
    }
  }

  async function deleteDocument(docId) {
    await http.delete(`/knowledge/documents/${docId}`)
    appStore.toast.success('文档已删除')
    if (selectedId.value === docId) { selectedId.value = ''; detail.value = null }
    await loadDocuments(); await loadStats()
  }

  // 清空当前身份可见的全部文档（含源文件与向量）
  async function clearAllDocuments() {
    try {
      const data = await http.delete('/knowledge/documents')
      selectedId.value = ''
      detail.value = null
      await loadDocuments(); await loadStats()
      appStore.toast.success(`已清空 ${data.deleted ?? 0} 篇文档（${data.chunks ?? 0} 个切片）`)
    } catch (err) {
      appStore.toast.error(errMsg(err, '清空失败'))
    }
  }

  async function reindexDocument(docId) {
    const data = await http.post(`/knowledge/documents/${docId}/reindex`, {})
    appStore.toast.success(`重新入库完成：${data.document.chunk_count} 个切片`)
    await selectDocument(docId); await loadDocuments()
  }

  // 源文件下载：带身份头，用 blob 下载（直接 window.open 带不上自定义头）
  async function downloadSource(docId) {
    const data = await http.get(`/knowledge/documents/${docId}/file`, { responseType: 'blob' })
    const doc = detail.value?.document
    const url = URL.createObjectURL(data)
    const a = document.createElement('a')
    a.href = url
    a.download = doc?.file_name || 'source'
    a.click()
    URL.revokeObjectURL(url)
  }

  // ── 检索验证 ──────────────────────────────────────────────────
  async function runSearch(question, extra = {}) {
    searchPreview.question = question
    searchPreview.loading = true
    searchPreview.error = ''
    try {
      const data = await http.post('/knowledge/search', {
        question, k: 5,
        department: extra.department || filters.department || undefined,
        version: extra.version || filters.version || undefined,
        docType: extra.docType || filters.docType || undefined,
        includeSuperseded: extra.includeSuperseded ?? filters.includeSuperseded,
      })
      searchPreview.hits = data.hits || []
      searchPreview.diagnostics = data.diagnostics
      searchPreview.appliedFilters = data.appliedFilters
    } catch (err) {
      searchPreview.hits = []
      searchPreview.error = errMsg(err, '检索失败')
    } finally {
      searchPreview.loading = false
    }
  }

  function resetFilters() {
    Object.assign(filters, { department: '', docType: '', status: '', version: '', keyword: '', includeSuperseded: true })
    return loadDocuments()
  }

  return {
    options, stats, documents, filters, loading,
    uploading, uploadProgress, uploadStage,
    selectedId, detail, detailLoading,
    searchPreview,
    loadOptions, loadStats, loadDocuments, refresh,
    suggestMetadata, upload, selectDocument, deleteDocument, clearAllDocuments, reindexDocument, downloadSource,
    runSearch, resetFilters,
  }
})
