<!-- frontend/src/views/MonitorView.vue -->
<template>
  <div class="monitor-view">
    <div class="metrics-grid">
      <MetricCard label="今日 API 调用" :value="s.overview?.apiCallsToday ?? 0" :sub="`总计 ${s.overview?.totalCallsToday ?? 0} 次`" color="blue" />
      <MetricCard label="缓存命中率" :value="s.overview?.cacheHitRate ?? '0%'"
                  :sub="`命中 ${s.overview?.cacheHitsToday ?? 0} 次 · 省下 ${s.overview?.savedTokensToday ?? 0} tokens`"
                  color="purple" />
      <MetricCard label="今日费用" :value="`¥${s.overview?.costCNYToday ?? 0}`" :sub="`预算 ¥${s.overview?.dailyBudget ?? 50}`" color="amber" />
      <MetricCard label="平均响应" :value="`${s.latency?.avg ?? 0}ms`" :sub="`P99: ${s.latency?.p99 ?? 0}ms`" color="green" />
    </div>

    <div class="budget-bar-wrap">
      <div class="budget-label">
        <span>今日预算使用</span>
        <span class="budget-pct" :class="{ warn: (s.overview?.budgetUsedPct??0) >= 80 }">{{ s.overview?.budgetUsedPct ?? 0 }}%</span>
        <!-- 数据源必须显式标出来：落库(postgres)=重启不丢；memory=降级，只统计当前进程 -->
        <span class="source-badge" :class="{ degraded: s.overview?.dataSource !== 'postgres' }"
              :title="s.overview?.dataSource === 'postgres'
                ? '统计已落库（PostgreSQL usage_calls），重启/重建容器都不会丢'
                : '数据库不可用，当前只统计本进程内的调用，重启会丢'">
          {{ s.overview?.dataSource === 'postgres' ? '数据源：PostgreSQL' : '数据源：内存（降级）' }}
        </span>
        <button class="btn-text-xs" @click="showBE = !showBE">修改预算</button>
      </div>
      <div class="budget-bar">
        <div class="budget-fill" :style="{ width: Math.min(s.overview?.budgetUsedPct??0, 100) + '%' }" :class="{ warn: (s.overview?.budgetUsedPct??0) >= 80, danger: (s.overview?.budgetUsedPct??0) >= 100 }" />
      </div>
      <div v-if="showBE" class="budget-edit">
        <input type="number" v-model.number="newBudget" class="input budget-input" min="1" />
        <button class="btn btn-primary btn-xs" @click="updateBudget">保存</button>
        <button class="btn btn-ghost btn-xs" @click="showBE = false">取消</button>
      </div>
    </div>

    <div class="charts-row">
      <div class="chart-card">
        <div class="chart-title">近 7 日 Token 消耗</div>
        <div class="bar-chart">
          <div v-for="day in (s.last7Days||[])" :key="day.date" class="bar-col">
            <div class="bar-group">
              <div class="bar input-bar" :style="{ height: barH(day.inputT) + 'px' }" :title="`输入 ${day.inputT}`" />
              <div class="bar output-bar" :style="{ height: barH(day.outputT) + 'px' }" :title="`输出 ${day.outputT}`" />
            </div>
            <div class="bar-label">{{ day.label }}</div>
            <div class="bar-cost">¥{{ day.costCNY }}</div>
          </div>
        </div>
        <div class="chart-legend">
          <span class="legend-item input">输入</span>
          <span class="legend-item output">输出</span>
        </div>
      </div>

      <div class="chart-card">
        <div class="chart-title">今日调用分布</div>
        <div v-if="!(s.byFeature?.length)" class="chart-empty">暂无今日数据</div>
        <div v-else class="feature-list">
          <div v-for="f in s.byFeature" :key="f.feature" class="feature-row">
            <span class="feature-label">{{ f.label }}</span>
            <div class="feature-bar-wrap"><div class="feature-bar" :style="{ width: featureBarW(f.calls) + '%' }" /></div>
            <span class="feature-calls">{{ f.calls }}</span>
            <span class="feature-cost">¥{{ f.costCNY }}</span>
          </div>
        </div>
      </div>

      <div class="chart-card">
        <div class="chart-title">响应时间</div>
        <div class="latency-stats">
          <div class="lat-item" v-for="(val, key) in latencyItems" :key="key">
            <div class="lat-label">{{ key }}</div>
            <div class="lat-value">{{ val }}ms</div>
          </div>
        </div>
      </div>
    </div>

    <div class="table-card">
      <div class="table-header">
        <span class="table-title">最近调用记录</span>
        <div class="table-filters">
          <select v-model="featureFilter" class="input filter-select">
            <option value="">全部功能</option>
            <option v-for="f in featureOptions" :key="f.feature" :value="f.feature">{{ f.label }}</option>
          </select>
          <button class="btn btn-ghost btn-sm" @click="loadStats">刷新</button>
          <button class="btn btn-ghost btn-sm danger" @click="resetStats" :disabled="resetting">
            {{ resetting ? '重置中...' : '重置统计' }}
          </button>
        </div>
      </div>
      <div class="table-wrap">
        <table class="call-table">
          <thead><tr><th>时间</th><th>功能</th><th>输入 T</th><th>输出 T</th><th>费用</th><th>延迟</th><th>来源</th></tr></thead>
          <tbody>
            <tr v-if="!filteredCalls.length"><td colspan="7" class="empty-row">暂无记录，进行操作后刷新</td></tr>
            <tr v-for="(c,i) in filteredCalls" :key="i" :class="{ 'from-cache': c.fromCache }">
              <td class="time-cell">{{ fmtTime(c.time) }}</td>
              <td><span class="feature-tag">{{ featureLabel(c.feature) }}</span></td>
              <td>{{ c.inputT }}</td><td>{{ c.outputT }}</td>
              <td>{{ c.fromCache ? '—' : `¥${c.costCNY}` }}</td>
              <td>{{ c.fromCache ? '—' : `${c.latencyMs}ms` }}</td>
              <td>
                <span :class="c.fromCache ? 'cache-badge' : 'api-badge'">{{ c.fromCache ? '缓存' : 'API' }}</span>
                <!-- 估算：embedding / bge 重排这类接口不回 usage，token 是按字符估的，如实标出来 -->
                <span v-if="c.estimated" class="est-badge" title="该接口不返回 usage，token 按字符估算">估算</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
<script setup>
import { ref, computed, onMounted } from 'vue'
import http from '@/utils/http.js'
import { useAppStore } from '@/stores/app.js'
import { useMonitorStore } from '@/stores/monitor.js'
const appStore = useAppStore()
// 看板数据统一来自 store（后端 /api/monitor/stats），顶部预算预警和这里是同一份数字，
// 不再各算各的（以前页面自己拉接口、顶栏读前端计数器，两边对不上）
const mon = useMonitorStore()
const s = computed(() => ({
  overview: mon.overview,
  latency: mon.latency,
  last7Days: mon.last7Days,
  byFeature: mon.byFeature,
  recentCalls: mon.recentCalls,
}))
const showBE = ref(false)
const newBudget = ref(50)
const featureFilter = ref('')
const featureNames = { chat:'对话助手', knowledge:'RAG 知识库', agent:'任务 Agent', workflow:'内容工作流', erp:'ERP 审批', prompt:'Prompt 调试' }
function featureLabel(f) { return featureNames[f] || f }
// 重置用量与缓存统计（测试时把累计数字清零，避免干扰判断）
const resetting = ref(false)
async function resetStats() {
  if (!confirm('重置用量统计与缓存命中统计？（预算设置保留）')) return
  resetting.value = true
  try {
    await http.post('/monitor/reset', {})
    await loadStats()
    appStore.toast.success('统计已重置')
  } catch (err) {
    appStore.toast.error('重置失败')
  } finally {
    resetting.value = false
  }
}

// 统一走 store 的 refresh：数字来源只有后端一处，顶部预警和看板永远一致
async function loadStats() {
  const d = await mon.refresh()
  newBudget.value = d?.overview?.dailyBudget ?? newBudget.value
}
async function updateBudget() {
  await http.put('/monitor/budget', { dailyBudget: newBudget.value })
  await loadStats(); showBE.value = false; appStore.toast.success('预算已更新')
}
const maxT = computed(() => Math.max(...(s.value.last7Days||[]).map(d=>d.inputT+d.outputT), 1))
const maxC = computed(() => Math.max(...(s.value.byFeature||[]).map(f=>f.calls), 1))
function barH(val) { return Math.max(2, Math.round((val/maxT.value)*80)) }
function featureBarW(calls) { return Math.round((calls/maxC.value)*100) }
const latencyItems = computed(() => ({ P50: s.value.latency?.p50??0, P90: s.value.latency?.p90??0, P99: s.value.latency?.p99??0, AVG: s.value.latency?.avg??0 }))
const filteredCalls = computed(() => { const c = s.value.recentCalls||[]; return featureFilter.value ? c.filter(x=>x.feature===featureFilter.value) : c })
const featureOptions = computed(() => [...new Set((s.value.recentCalls||[]).map(c=>c.feature))].map(f=>({ feature:f, label:featureLabel(f) })))
function fmtTime(iso) { if (!iso) return ''; const d = new Date(iso); return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}` }
// 轮询交给 store（顶部栏已经 start 了同一个定时器），这里只保证进来先拉一次
onMounted(() => { mon.refresh(); newBudget.value = mon.dailyBudget })
</script>
<script>
const MetricCard = {
  props: ['label','value','sub','color'],
  template: `<div class="metric-card" :class="'color-'+color"><div class="metric-value">{{value}}</div><div class="metric-label">{{label}}</div><div class="metric-sub">{{sub}}</div></div>`,
}
export default { components: { MetricCard } }
</script>
<style scoped>
/* 为什么必须有 > * { flex-shrink:0 }：
   .monitor-view 是 flex 纵向容器，子项默认 flex-shrink:1 —— 视口一变矮，
   卡片就被"压缩"到几十像素而不是把容器撑高，于是 overflow-y:auto 永远没有可滚的内容，
   表现就是**界面完全没法上下滑动**（表格被压成 31px、内容被裁掉还看不到滚动条）。
   让子项保持自然高度，容器才会真正溢出 -> 滚动条出现。 */
.monitor-view { height:100%; overflow-y:auto; padding:var(--space-lg) var(--space-xl); display:flex; flex-direction:column; gap:var(--space-lg); background:var(--color-bg); }
.monitor-view > * { flex-shrink: 0; }
/* 窄屏/矮窗口下指标卡两列、图表竖排，避免四个卡片挤成一团 */
@media (max-width: 1180px) { .metrics-grid { grid-template-columns:repeat(2,1fr); } .charts-row { grid-template-columns:1fr; } }
.metrics-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:var(--space-md); }
.metric-card { background:var(--color-surface); border:1px solid var(--color-border); border-radius:var(--radius-lg); padding:var(--space-md) var(--space-lg); }
.color-blue   { border-top:3px solid var(--color-info); }
.color-purple { border-top:3px solid var(--color-primary); }
.color-amber  { border-top:3px solid var(--color-warning); }
.color-green  { border-top:3px solid var(--color-success); }
.metric-value { font-size:24px; font-weight:800; color:var(--color-text); line-height:1.2; }
.metric-label { font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.06em; color:var(--color-text-muted); margin-top:4px; }
.metric-sub { font-size:11px; color:var(--color-text-muted); margin-top:2px; }
.btn-ghost.danger:hover:not(:disabled) { color:var(--color-danger); border-color:var(--color-danger); }
.btn-ghost:disabled { opacity:.5; cursor:not-allowed; }
.budget-bar-wrap { background:var(--color-surface); border:1px solid var(--color-border); border-radius:var(--radius-lg); padding:var(--space-md) var(--space-lg); }
.budget-label { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--color-text-sub); margin-bottom:8px; }
.budget-pct { font-weight:700; color:var(--color-text); }
.budget-pct.warn { color:var(--color-warning); }
.btn-text-xs { font-size:11px; color:var(--color-primary); background:none; border:none; cursor:pointer; margin-left:auto; }
.source-badge { font-size:10px; padding:2px 8px; border-radius:var(--radius-full); background:var(--color-border-light); color:var(--color-text-muted); }
.source-badge.degraded { background:#fef3c7; color:#b45309; }
.budget-bar { height:6px; background:var(--color-border); border-radius:var(--radius-full); overflow:hidden; }
.budget-fill { height:100%; background:var(--color-primary); border-radius:var(--radius-full); transition:width .5s; }
.budget-fill.warn { background:var(--color-warning); }
.budget-fill.danger { background:var(--color-danger); }
.budget-edit { display:flex; align-items:center; gap:8px; margin-top:10px; }
.budget-input { width:100px; padding:5px 8px; }
.btn-xs { padding:4px 10px; font-size:11px; }
.charts-row { display:grid; grid-template-columns:2fr 1.5fr 1fr; gap:var(--space-md); }
.chart-card { background:var(--color-surface); border:1px solid var(--color-border); border-radius:var(--radius-lg); padding:var(--space-lg); }
.chart-title { font-size:12px; font-weight:600; color:var(--color-text); margin-bottom:var(--space-md); }
.chart-empty { font-size:12px; color:var(--color-text-muted); text-align:center; padding:24px 0; }
.bar-chart { display:flex; align-items:flex-end; gap:var(--space-sm); height:100px; padding-bottom:4px; }
.bar-col { display:flex; flex-direction:column; align-items:center; flex:1; gap:3px; }
.bar-group { display:flex; align-items:flex-end; gap:2px; }
.bar { width:10px; border-radius:2px 2px 0 0; transition:height .3s; min-height:2px; }
.input-bar { background:var(--color-primary); }
.output-bar { background:var(--color-info); }
.bar-label { font-size:9px; color:var(--color-text-muted); }
.bar-cost { font-size:9px; color:var(--color-text-muted); }
.chart-legend { display:flex; gap:var(--space-md); margin-top:var(--space-sm); }
.legend-item { display:flex; align-items:center; gap:4px; font-size:10px; color:var(--color-text-muted); }
.legend-item::before { content:''; width:10px; height:3px; border-radius:2px; display:inline-block; }
.legend-item.input::before { background:var(--color-primary); }
.legend-item.output::before { background:var(--color-info); }
.feature-list { display:flex; flex-direction:column; gap:8px; }
.feature-row { display:flex; align-items:center; gap:8px; }
.feature-label { font-size:11px; color:var(--color-text-sub); width:70px; flex-shrink:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.feature-bar-wrap { flex:1; height:6px; background:var(--color-border); border-radius:var(--radius-full); overflow:hidden; }
.feature-bar { height:100%; background:var(--color-primary); border-radius:var(--radius-full); transition:width .4s; }
.feature-calls { font-size:11px; color:var(--color-text-muted); width:28px; text-align:right; }
.feature-cost { font-size:11px; color:var(--color-warning); width:40px; text-align:right; }
.latency-stats { display:grid; grid-template-columns:1fr 1fr; gap:var(--space-sm); }
.lat-item { text-align:center; padding:10px 6px; background:var(--color-bg); border-radius:var(--radius-md); border:1px solid var(--color-border-light); }
.lat-label { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--color-text-muted); }
.lat-value { font-size:18px; font-weight:800; color:var(--color-text); margin:4px 0; }
.table-card { background:var(--color-surface); border:1px solid var(--color-border); border-radius:var(--radius-lg); overflow:hidden; }
.table-header { display:flex; align-items:center; justify-content:space-between; padding:var(--space-md) var(--space-lg); border-bottom:1px solid var(--color-border-light); }
.table-title { font-size:13px; font-weight:600; color:var(--color-text); }
.table-filters { display:flex; gap:var(--space-sm); }
.filter-select { padding:5px 10px; font-size:12px; }
.btn-sm { padding:5px 12px; font-size:12px; }
.table-wrap { overflow-x:auto; }
.call-table { width:100%; border-collapse:collapse; font-size:12px; }
.call-table th { padding:8px 16px; background:var(--color-bg); font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.06em; color:var(--color-text-muted); text-align:left; border-bottom:1px solid var(--color-border); }
.call-table td { padding:8px 16px; border-bottom:1px solid var(--color-border-light); color:var(--color-text-sub); }
.call-table tr.from-cache td { opacity:.7; }
.call-table tr:hover td { background:var(--color-border-light); }
.empty-row { text-align:center; color:var(--color-text-muted); padding:24px !important; }
.time-cell { font-family:var(--font-mono); color:var(--color-text-muted); }
.feature-tag { font-size:10px; padding:2px 7px; background:var(--color-primary-bg); color:var(--color-primary); border-radius:var(--radius-full); }
.cache-badge { font-size:10px; padding:2px 7px; background:#ede9fe; color:#6d28d9; border-radius:var(--radius-full); }
.api-badge { font-size:10px; padding:2px 7px; background:var(--color-border-light); color:var(--color-text-muted); border-radius:var(--radius-full); }
.est-badge { font-size:10px; padding:2px 6px; margin-left:4px; background:#fff7ed; color:#c2410c; border-radius:var(--radius-full); }
</style>
