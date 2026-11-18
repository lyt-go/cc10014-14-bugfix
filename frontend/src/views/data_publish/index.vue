<template>
  <section class="page" data-module="data_publish">
    <header class="page-head">
      <div>
        <h2>数据发布管理</h2>
        <p class="page-desc">维护发布版本的输入、执行约束与结果记录，为数据发布提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记发布版本</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出数据发布清单</button>
      </div>
    </header>

    <div v-if="notifications.length" class="notice-bar" data-testid="notice-bar">
      <strong>发布阻断通知（{{ notifications.length }}）：</strong>
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
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
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
            <button class="link" type="button" :data-testid="`blockers-${row.id}`" @click="openBlockers(row)">
              阻断窗口
            </button>
            <button class="link" type="button" :data-testid="`view-data-${row.id}`" @click="openPublished(row)">
              查看发布数据
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无数据发布数据，可先登记发布版本</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记发布版本</h3>
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

    <!-- 阻断窗口复核：沿数据流回溯到DWS待修复窗口 -->
    <div v-if="blockerRow" class="modal-backdrop" data-testid="blocker-modal" @click.self="blockerRow = null">
      <div class="modal modal-wide">
        <div class="detail-head">
          <h3>待修复窗口：{{ blockerRow['数据集名称'] }}（{{ blockerRow['版本编号'] }}）</h3>
          <button class="btn" type="button" @click="blockerRow = null">关闭</button>
        </div>
        <p class="page-desc">发布前校验在汇总层DWS表发现以下窗口，异常记录已隔离主结果；修复并重新聚合后即可发布，旧版本保持不变。</p>
        <table class="data-table" data-testid="blocker-table">
          <thead>
            <tr><th>窗口键</th><th>异常原因</th><th>异常行数</th><th>状态</th><th>首次发现</th></tr>
          </thead>
          <tbody>
            <tr v-for="w in blockerWindows" :key="String(w.window_id || w.window_key)">
              <td>{{ w.window_key || '（空维度）' }}</td>
              <td>{{ w['异常原因'] }}</td>
              <td>{{ w['异常行数'] }}</td>
              <td>{{ w['状态'] }}</td>
              <td>{{ w['首次发现'] }}</td>
            </tr>
            <tr v-if="!blockerWindows.length"><td colspan="5" class="empty-state">当前无待修复窗口，可以正常发布。</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 已发布数据：版本化快照，筛选/排序/翻页复核 -->
    <div v-if="publishedRow" class="modal-backdrop" data-testid="published-modal" @click.self="publishedRow = null">
      <div class="modal modal-wide">
        <div class="detail-head">
          <h3>已发布数据：{{ publishedRow['数据集名称'] }}</h3>
          <button class="btn" type="button" data-testid="published-close" @click="publishedRow = null">关闭</button>
        </div>
        <p class="page-desc">
          发布版本 v{{ publishedMeta.version ?? '—' }} ｜ 来源表 {{ publishedMeta.table ?? '—' }} ｜
          发布时间 {{ publishedMeta.published_at ?? '—' }}
        </p>
        <form class="filter-bar" @submit.prevent="loadPublishedPage(1)">
          <label class="filter-item">
            <span>关键字检索</span>
            <input v-model="publishedQuery.keyword" data-testid="published-keyword" placeholder="全列模糊搜索" />
          </label>
          <label class="filter-item">
            <span>列过滤</span>
            <input v-model="publishedQuery.field" data-testid="published-filter-field" placeholder="列名" />
          </label>
          <label class="filter-item">
            <span>过滤值</span>
            <input v-model="publishedQuery.value" data-testid="published-filter-value" placeholder="期望值" />
          </label>
          <label class="filter-item">
            <span>排序字段</span>
            <input v-model="publishedQuery.sortBy" data-testid="published-sort-by" placeholder="列名" />
          </label>
          <label class="filter-item">
            <span>排序方向</span>
            <select v-model="publishedQuery.sortOrder" data-testid="published-sort-order">
              <option value="asc">升序</option>
              <option value="desc">降序</option>
            </select>
          </label>
          <button class="btn" type="button" data-testid="published-query" @click="loadPublishedPage(1)">查询过滤</button>
          <button class="btn ghost" type="button" data-testid="published-reset" @click="resetPublishedQuery()">重置</button>
        </form>
        <div class="table-scroll">
          <table class="data-table" data-testid="published-table">
            <thead>
              <tr>
                <th v-for="col in publishedColumns" :key="col">
                  <button class="link" type="button" @click="sortPublishedBy(col)">{{ col }}⇅</button>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(item, idx) in publishedRows" :key="idx" :data-testid="`published-row-${idx}`">
                <td v-for="col in publishedColumns" :key="col">{{ item[col] ?? '—' }}</td>
              </tr>
              <tr v-if="!publishedRows.length">
                <td :colspan="Math.max(publishedColumns.length, 1)" class="empty-state">暂无已发布数据。</td>
              </tr>
            </tbody>
          </table>
        </div>
        <footer class="page-foot">
          <span data-testid="published-total">共 {{ publishedTotal }} 条 ｜ 第 {{ publishedQuery.page }} / {{ totalPublishedPages }} 页</span>
          <span class="page-actions">
            <button class="btn" type="button" data-testid="published-prev" :disabled="publishedQuery.page <= 1" @click="loadPublishedPage(publishedQuery.page - 1)">上一页</button>
            <button class="btn" type="button" data-testid="published-next" :disabled="publishedQuery.page >= totalPublishedPages" @click="loadPublishedPage(publishedQuery.page + 1)">下一页</button>
          </span>
        </footer>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据发布记录</span>
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

const ENDPOINT = '/api/data_publish'
const columns = ["版本编号", "数据集名称", "发布范围", "查询参数", "响应字段", "缓存秒数", "分页上限", "消费应用"]
const actions = ["提交校验", "确认发布", "撤回版本"]
const statuses = ["草稿", "待校验", "已发布", "已撤回"]
const metricNames = ["发布版本总数", "待校验版本", "已发布版本"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const notifications = ref<Notice[]>([])

const blockerRow = ref<Row | null>(null)
const blockerWindows = ref<WindowItem[]>([])
const publishedRow = ref<Row | null>(null)
const publishedRows = ref<ResultRow[]>([])
const publishedColumns = ref<string[]>([])
const publishedTotal = ref(0)
const publishedMeta = ref<Record<string, string | number | null>>({})
const publishedQuery = ref({
  keyword: '', field: '', value: '', sortBy: '', sortOrder: 'asc', page: 1, size: 10,
})

const totalPublishedPages = computed(() => Math.max(1, Math.ceil(publishedTotal.value / publishedQuery.value.size)))

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
    link.download = 'data_publish.csv'
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
      throw new Error(payload.message || '数据发布动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    // 阻断类错误必须把后端原因直接展示给用户，而不是静默吞掉
    errorMessage.value = error instanceof Error ? error.message : '数据发布操作失败'
  }
}

async function openBlockers(row: Row) {
  blockerRow.value = row
  blockerWindows.value = []
  const response = await request(`${ENDPOINT}/${row.id}/blockers`)
  if (response.ok) {
    const payload = await response.json()
    blockerWindows.value = payload.items ?? []
  }
}

async function openPublished(row: Row) {
  publishedRow.value = row
  resetPublishedQuery(false)
  await loadPublishedPage(1)
}

function resetPublishedQuery(reloadData = true) {
  publishedQuery.value = { keyword: '', field: '', value: '', sortBy: '', sortOrder: 'asc', page: 1, size: 10 }
  if (reloadData) void loadPublishedPage(1)
}

function sortPublishedBy(col: string) {
  if (publishedQuery.value.sortBy === col) {
    publishedQuery.value.sortOrder = publishedQuery.value.sortOrder === 'asc' ? 'desc' : 'asc'
  } else {
    publishedQuery.value.sortBy = col
    publishedQuery.value.sortOrder = 'asc'
  }
  void loadPublishedPage(1)
}

async function loadPublishedPage(page: number) {
  if (!publishedRow.value) return
  const q = publishedQuery.value
  q.page = Math.max(1, page)
  const params = new URLSearchParams({ page: String(q.page), size: String(q.size), sort_order: q.sortOrder })
  if (q.keyword) params.set('keyword', q.keyword)
  if (q.sortBy) params.set('sort_by', q.sortBy)
  if (q.field && q.value) params.set(q.field, q.value)
  const response = await request(`${ENDPOINT}/${publishedRow.value.id}/data?${params.toString()}`)
  if (!response.ok) {
    errorMessage.value = '发布数据读取失败'
    return
  }
  const payload = await response.json()
  publishedRows.value = payload.items ?? []
  publishedTotal.value = payload.total ?? 0
  publishedColumns.value = payload.columns ?? []
  publishedMeta.value = {
    version: payload.version ?? null,
    table: payload.table ?? null,
    published_at: payload.published_at ?? null,
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('发布版本列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await loadNotifications()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据发布列表读取失败'
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
.modal-wide { width: min(960px, 100%); }
.detail-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.table-scroll { max-height: 360px; overflow: auto; border: 1px solid var(--border); border-radius: 8px; }
</style>
