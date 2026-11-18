<template>
  <section class="page" data-module="dws_layer">
    <header class="page-head">
      <div>
        <h2>汇总层管理管理</h2>
        <p class="page-desc">维护汇总层DWS表的输入、执行约束与结果记录，为汇总层管理提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记汇总表</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出汇总层管理清单</button>
      </div>
    </header>

    <div v-if="notifications.length" class="notice-bar" data-testid="notice-bar">
      <strong>待处理通知（{{ notifications.length }}）：</strong>
      <ul>
        <li v-for="note in notifications" :key="note.id">
          <span class="notice-level">{{ note.level }}</span>{{ note.title }} —— {{ note.message }}
        </li>
      </ul>
    </div>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>聚合状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span :class="['status-pill', row['聚合状态'] === '中断' ? 'status-bad' : '']">
              {{ row['聚合状态'] ?? '未执行' }}
              <template v-if="row['异常窗口数']">（待修复窗口 {{ row['异常窗口数'] }}）</template>
            </span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" :data-testid="`aggregate-${row.id}`" @click="runAggregate(row)">
              执行聚合
            </button>
            <button class="link" type="button" :data-testid="`detail-${row.id}`" @click="openDetail(row)">
              结果详情
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无汇总层管理数据，可先登记汇总表</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记汇总表</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <!-- DWS聚合结果表详情：主结果 / 异常隔离 / 待修复窗口 / 运行态 -->
    <div v-if="detailRow" class="modal-backdrop" data-testid="detail-modal" @click.self="closeDetail">
      <div class="modal modal-wide">
        <div class="detail-head">
          <h3>DWS聚合结果表：{{ detailRow['表名称'] }}（{{ detailRow['表编号'] }}）</h3>
          <button class="btn" type="button" data-testid="detail-close" @click="closeDetail">关闭</button>
        </div>
        <p class="page-desc">
          结果版本 v{{ detailRow['结果版本'] ?? 0 }} ｜ 合法分组 {{ detailRow['合法分组数'] ?? 0 }} 个 ｜
          隔离异常 {{ detailRow['异常行数'] ?? 0 }} 条 ｜ 待修复窗口 {{ detailRow['异常窗口数'] ?? 0 }} 个
        </p>

        <div class="tab-bar" role="tablist">
          <button
            v-for="tab in tabs"
            :key="tab.key"
            type="button"
            class="tab-btn"
            :class="{ active: activeTab === tab.key }"
            :data-testid="`tab-${tab.key}`"
            @click="switchTab(tab.key)"
          >
            {{ tab.label }}
          </button>
        </div>

        <!-- 运行态：中断定位与续写 -->
        <div v-if="activeTab === 'run'" data-testid="pane-run">
          <div v-if="!runInfo" class="empty-state">该汇总表尚未执行过聚合。</div>
          <div v-else class="run-panel">
            <p>
              运行状态：<strong :class="runInfo.status === '中断' ? 'error-text' : ''">{{ runInfo.status }}</strong>
              ｜ 已处理 {{ runInfo.processed_indexes?.length ?? 0 }} / {{ runInfo.total_rows }} 行
              ｜ 结果版本 v{{ runInfo.result_version ?? '—' }}
            </p>
            <div v-if="runInfo.status === '中断'" class="page-actions">
              <button class="btn primary" type="button" data-testid="resume-aggregate" @click="resumeAggregate">
                续写未完成部分（{{ (runInfo.total_rows ?? 0) - (runInfo.processed_indexes?.length ?? 0) }} 行）
              </button>
            </div>
            <p v-else class="page-desc">最近一次聚合已完成，可在「主结果」页签核对分组与明细总数。</p>
          </div>
        </div>

        <!-- 待修复窗口清单 -->
        <div v-else-if="activeTab === 'windows'" data-testid="pane-windows">
          <table class="data-table">
            <thead>
              <tr><th>窗口键</th><th>异常原因</th><th>异常行数</th><th>状态</th><th>首次发现</th><th>修复时间</th></tr>
            </thead>
            <tbody>
              <tr v-for="w in windows" :key="String(w.window_id || w.window_key)" :data-window-status="w['状态']">
                <td>{{ w.window_key || '（空维度）' }}</td>
                <td>{{ w['异常原因'] }}</td>
                <td>{{ w['异常行数'] }}</td>
                <td>{{ w['状态'] }}</td>
                <td>{{ w['首次发现'] }}</td>
                <td>{{ w['修复时间'] ?? '—' }}</td>
              </tr>
              <tr v-if="!windows.length"><td colspan="6" class="empty-state">暂无待修复窗口，所有统计窗口均正常。</td></tr>
            </tbody>
          </table>
        </div>

        <!-- 主结果 / 异常隔离：筛选、排序、翻页 -->
        <div v-else>
          <form class="filter-bar" @submit.prevent="loadResultPage(1)">
            <label class="filter-item">
              <span>关键字检索</span>
              <input v-model="resultQuery.keyword" data-testid="result-keyword" placeholder="全列模糊搜索" />
            </label>
            <label class="filter-item">
              <span>列过滤</span>
              <input v-model="resultQuery.field" data-testid="result-filter-field" placeholder="列名，如 order_status" />
            </label>
            <label class="filter-item">
              <span>过滤值</span>
              <input v-model="resultQuery.value" data-testid="result-filter-value" placeholder="期望包含的值" />
            </label>
            <label class="filter-item">
              <span>排序字段</span>
              <input v-model="resultQuery.sortBy" data-testid="result-sort-by" placeholder="列名" />
            </label>
            <label class="filter-item">
              <span>排序方向</span>
              <select v-model="resultQuery.sortOrder" data-testid="result-sort-order">
                <option value="asc">升序</option>
                <option value="desc">降序</option>
              </select>
            </label>
            <button class="btn" type="button" data-testid="result-query" @click="loadResultPage(1)">查询过滤</button>
            <button class="btn ghost" type="button" data-testid="result-reset" @click="resetResultQuery()">重置</button>
          </form>

          <div class="table-scroll">
            <table class="data-table" :data-testid="`result-table-${activeTab}`">
              <thead>
                <tr>
                  <th v-for="col in resultColumns" :key="col">
                    <button class="link" type="button" @click="sortByColumn(col)">{{ col }}⇅</button>
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(item, idx) in resultRows" :key="idx" :data-testid="`result-row-${idx}`">
                  <td v-for="col in resultColumns" :key="col">{{ item[col] ?? '—' }}</td>
                </tr>
                <tr v-if="!resultRows.length">
                  <td :colspan="Math.max(resultColumns.length, 1)" class="empty-state">
                    {{ activeTab === 'abnormal' ? '暂无异常隔离记录。' : '主结果表暂无数据，请先执行聚合。' }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <footer class="page-foot">
            <span data-testid="result-total">共 {{ resultTotal }} 条 ｜ 第 {{ resultQuery.page }} / {{ totalResultPages }} 页</span>
            <span class="page-actions">
              <button class="btn" type="button" data-testid="result-prev" :disabled="resultQuery.page <= 1" @click="loadResultPage(resultQuery.page - 1)">上一页</button>
              <button class="btn" type="button" data-testid="result-next" :disabled="resultQuery.page >= totalResultPages" @click="loadResultPage(resultQuery.page + 1)">下一页</button>
            </span>
          </footer>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条汇总层管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>
type ResultRow = Record<string, string | number | null>
type Notice = { id: number; level: string; title: string; message: string }
type WindowItem = Record<string, string | number | null>
type RunInfo = {
  status: string
  total_rows: number
  processed_indexes: number[]
  result_version?: number
}

const ENDPOINT = '/api/dws_layer'
const columns = ["表编号", "统计粒度", "汇总维度", "度量字段", "聚合函数", "时间范围", "刷新周期", "创建批次"]
const actions = ["提交校验", "确认上线", "标记下线"]
const statuses = ["草稿", "待校验", "已上线", "已下线"]
const metricNames = ["汇总表总数", "待校验表", "已上线表"]
const tabs = [
  { key: 'main', label: '主结果（合法分组）' },
  { key: 'abnormal', label: '异常隔离表' },
  { key: 'windows', label: '待修复窗口清单' },
  { key: 'run', label: '运行态/续写' },
] as const
type TabKey = (typeof tabs)[number]['key']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const notifications = ref<Notice[]>([])

// 详情弹窗状态
const detailRow = ref<Row | null>(null)
const activeTab = ref<TabKey>('main')
const resultRows = ref<ResultRow[]>([])
const resultColumns = ref<string[]>([])
const resultTotal = ref(0)
const windows = ref<WindowItem[]>([])
const runInfo = ref<RunInfo | null>(null)
const resultQuery = ref({
  keyword: '',
  field: '',
  value: '',
  sortBy: '',
  sortOrder: 'asc',
  page: 1,
  size: 10,
})

const totalResultPages = computed(() => Math.max(1, Math.ceil(resultTotal.value / resultQuery.value.size)))

const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function loadNotifications() {
  try {
    const response = await request(`${ENDPOINT}/notifications?only_open=true`)
    if (response.ok) {
      const payload = await response.json()
      notifications.value = payload.items ?? []
    }
  } catch {
    notifications.value = []
  }
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'dws_layer.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.message || '汇总层管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '汇总层管理操作失败'
  }
}

async function runAggregate(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/aggregate`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || payload.ok === false) {
      errorMessage.value = payload.message || '聚合执行失败'
    }
    await reload()
    if (detailRow.value && String(detailRow.value.id) === String(row.id)) {
      await openDetail(row)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '聚合请求失败'
  }
}

async function resumeAggregate() {
  if (!detailRow.value) return
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${detailRow.value.id}/aggregate/resume`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || payload.ok === false) {
      errorMessage.value = payload.message || '续写失败'
    }
    await reload()
    await openDetail(detailRow.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '续写请求失败'
  }
}

async function openDetail(row: Row) {
  detailRow.value = row
  activeTab.value = 'main'
  resetResultQuery(false)
  await Promise.all([loadResultPage(1), loadWindows(), loadRun()])
}

function closeDetail() {
  detailRow.value = null
}

async function switchTab(tab: TabKey) {
  activeTab.value = tab
  if (tab === 'windows') {
    await loadWindows()
  } else if (tab === 'run') {
    await loadRun()
  } else {
    resetResultQuery(false)
    await loadResultPage(1)
  }
}

async function loadWindows() {
  if (!detailRow.value) return
  const response = await request(`${ENDPOINT}/${detailRow.value.id}/windows`)
  if (response.ok) {
    const payload = await response.json()
    windows.value = payload.items ?? []
  }
}

async function loadRun() {
  if (!detailRow.value) return
  const response = await request(`${ENDPOINT}/${detailRow.value.id}/run`)
  if (response.ok) {
    const payload = await response.json()
    runInfo.value = payload.has_run ? (payload.run as RunInfo) : null
  }
}

function resetResultQuery(reloadData = true) {
  resultQuery.value = { keyword: '', field: '', value: '', sortBy: '', sortOrder: 'asc', page: 1, size: 10 }
  if (reloadData) void loadResultPage(1)
}

function sortByColumn(col: string) {
  if (resultQuery.value.sortBy === col) {
    resultQuery.value.sortOrder = resultQuery.value.sortOrder === 'asc' ? 'desc' : 'asc'
  } else {
    resultQuery.value.sortBy = col
    resultQuery.value.sortOrder = 'asc'
  }
  void loadResultPage(1)
}

async function loadResultPage(page: number) {
  if (!detailRow.value) return
  const q = resultQuery.value
  q.page = Math.max(1, page)
  const params = new URLSearchParams({
    page: String(q.page),
    size: String(q.size),
    sort_order: q.sortOrder,
  })
  if (q.keyword) params.set('keyword', q.keyword)
  if (q.sortBy) params.set('sort_by', q.sortBy)
  if (q.field && q.value) params.set(q.field, q.value)
  const path = activeTab.value === 'abnormal' ? 'abnormal' : 'data'
  const response = await request(`${ENDPOINT}/${detailRow.value.id}/${path}?${params.toString()}`)
  if (!response.ok) {
    errorMessage.value = '结果表读取失败'
    return
  }
  const payload = await response.json()
  resultRows.value = payload.items ?? []
  resultTotal.value = payload.total ?? 0
  resultColumns.value = payload.columns ?? []
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('汇总表列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await loadNotifications()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '汇总层管理列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.notice-bar {
  background: #fff7ed;
  border: 1px solid #fdba74;
  border-radius: 8px;
  padding: 10px 14px;
  margin-bottom: 12px;
  font-size: 13px;
}
.notice-bar ul { margin: 6px 0 0; padding-left: 18px; }
.notice-level {
  display: inline-block;
  background: #b42318;
  color: #fff;
  border-radius: 4px;
  padding: 0 6px;
  margin-right: 6px;
  font-size: 12px;
}
.status-pill { font-size: 12px; color: #475467; }
.status-bad { color: #b42318; font-weight: 600; }
.modal-wide { width: min(960px, 100%); }
.detail-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.tab-bar { display: flex; gap: 8px; margin: 12px 0; flex-wrap: wrap; }
.tab-btn {
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 6px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 13px;
}
.tab-btn.active { background: var(--brand); border-color: var(--brand); color: #fff; }
.table-scroll { max-height: 360px; overflow: auto; border: 1px solid var(--border); border-radius: 8px; }
.run-panel { background: #f8fafc; border-radius: 8px; padding: 12px; }
</style>
