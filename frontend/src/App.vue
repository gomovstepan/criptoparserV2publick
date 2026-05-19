<template>
  <div class="app">
    <h1>Crypto Arbitrage Dashboard</h1>

    <div class="tabs">
      <button :class="{ active: activeTab === 'raw' }" @click="activeTab = 'raw'">Raw Data</button>
      <button :class="{ active: activeTab === 'arbitrage' }" @click="activeTab = 'arbitrage'">Arbitrage</button>
      <button :class="{ active: activeTab === 'history' }" @click="activeTab = 'history'">History</button>
      <button :class="{ active: activeTab === 'settings' }" @click="activeTab = 'settings'">Settings</button>
    </div>

    <div v-if="activeTab === 'raw'" class="panel">
      <h2>Raw Orderbook Snapshot</h2>
      <table>
        <thead>
          <tr>
            <th>Exchange</th>
            <th>Symbol</th>
            <th>ASK</th>
            <th>BID</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rawRows" :key="`${row.exchange}-${row.symbol}`">
            <td>{{ row.exchange }}</td>
            <td>{{ row.symbol }}</td>
            <td>{{ row.ask }}</td>
            <td>{{ row.bid }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-else-if="activeTab === 'arbitrage'" class="panel">
      <h2>Active Arbitrage Opportunities</h2>
      <table>
        <thead>
          <tr>
            <th>Coin</th>
            <th>Direction</th>
            <th>Buy Price</th>
            <th>Sell Price</th>
            <th>Spread %</th>
            <th>Max Spread</th>
            <th>Duration</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in arbitrageRows" :key="row.key" :class="spreadClass(row.spread_percent)">
            <td>{{ row.coin }}</td>
            <td>{{ row.buy_exchange }} → {{ row.sell_exchange }}</td>
            <td>{{ row.buy_price }}</td>
            <td>{{ row.sell_price }}</td>
            <td>{{ row.spread_percent }}</td>
            <td>{{ row.max_spread }}</td>
            <td>{{ formatDurationFromStart(row.start_time) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-else-if="activeTab === 'history'" class="panel">
      <div class="history-header">
        <h2>Arbitrage History</h2>
        <div class="history-actions">
          <button @click="fetchHistory">Refresh</button>
          <button @click="clearHistory">Clear history</button>
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th>Coin</th>
            <th>Direction</th>
            <th @click="toggleHistorySort('start_time')" class="sortable">
              Start Time {{ sortIndicator('start_time') }}
            </th>
            <th @click="toggleHistorySort('end_time')" class="sortable">
              End Time {{ sortIndicator('end_time') }}
            </th>
            <th @click="toggleHistorySort('duration')" class="sortable">
              Duration {{ sortIndicator('duration') }}
            </th>
            <th @click="toggleHistorySort('max_spread')" class="sortable">
              Max Spread {{ sortIndicator('max_spread') }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in historyRows" :key="row.id">
            <td>{{ row.coin }}</td>
            <td>{{ row.buy_exchange }} → {{ row.sell_exchange }}</td>
            <td>{{ row.formatted_start_time }}</td>
            <td>{{ row.formatted_end_time }}</td>
            <td>{{ formatDurationSeconds(row.duration_seconds_numeric) }}</td>
            <td>{{ row.max_spread }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-else class="panel">
      <div class="settings-header">
        <h2>Runtime Settings</h2>
        <div class="settings-actions">
          <button @click="fetchSettings">Refresh</button>
          <button @click="saveSettings" :disabled="savingSettings">{{ savingSettings ? 'Saving...' : 'Save' }}</button>
        </div>
      </div>
      <div v-if="settingsError" class="settings-error">{{ settingsError }}</div>
      <div v-if="settingsSuccess" class="settings-success">{{ settingsSuccess }}</div>
      <div v-for="group in settingsGroups" :key="group.category" class="settings-group">
        <h3>{{ group.category }}</h3>
        <div class="settings-fields">
          <div v-for="field in group.fields" :key="field.key" class="settings-field">
            <label :for="field.key">{{ field.key }}</label>
            <input
              v-if="!field.read_only"
              :id="field.key"
              v-model="settingsValues[field.key]"
              type="number"
              :step="field.type === 'int' ? 1 : 0.01"
            />
            <span v-else class="settings-readonly">{{ settingsValues[field.key] }}</span>
            <span class="settings-meta">default: {{ field.default }} | {{ field.type }}</span>
          </div>
        </div>
      </div>
      <div v-if="API_KEY" class="settings-apikey">
        <label>API Key (required to save)</label>
        <input v-model="settingsApiKey" type="password" placeholder="Enter API key" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'

const activeTab = ref('raw')
const rawSnapshot = ref({})
const arbitrage = ref([])
const history = ref([])
const historySort = ref({ column: 'max_spread', direction: 'desc' })
const nowMs = ref(Date.now())
const settingsSchema = ref([])
const settingsValues = ref({})
const settingsApiKey = ref('')
const savingSettings = ref(false)
const settingsError = ref('')
const settingsSuccess = ref('')
let rawPollTimer
let arbitragePollTimer
let durationTimer
let settingsPollTimer
let isFetchingRaw = false
let isFetchingArbitrage = false
const vladivostokFormatter = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Vladivostok',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23'
})

const formatPriceDisplay = (value) => {
  if (value === undefined || value === null || value === '-') return '-'
  const s = String(value)
  if (!s.includes('.')) return s
  return s.replace(/\.?0+$/, '') || '0'
}

const rawRows = computed(() => {
  const rows = []
  for (const [exchange, symbols] of Object.entries(rawSnapshot.value || {})) {
    for (const [symbol, sides] of Object.entries(symbols || {})) {
      const bestAsk = formatPriceDisplay(sides.ASK?.[0]?.price) ?? '-'
      const bestBid = formatPriceDisplay(sides.BID?.[0]?.price) ?? '-'
      rows.push({ exchange, symbol, ask: bestAsk, bid: bestBid })
    }
  }
  return rows
})

const arbitrageRows = computed(() =>
  (arbitrage.value || []).map((item) => ({
    ...item,
    key: `${item.coin}-${item.buy_exchange}-${item.sell_exchange}`
  }))
)

const formatDateTimeVladivostok = (dateInput) => {
  if (!dateInput) return '-'
  const parsed = new Date(dateInput)
  if (Number.isNaN(parsed.getTime())) return '-'

  const parts = vladivostokFormatter.formatToParts(parsed)
  const map = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  return `${map.year}-${map.month}-${map.day} ${map.hour}:${map.minute}:${map.second}`
}

const normalizedHistoryRows = computed(() =>
  (history.value || []).map((item) => {
    const durationSeconds = Math.max(0, Number.parseInt(item.duration_seconds ?? 0, 10) || 0)
    const maxSpreadNumeric = Number.parseFloat(item.max_spread)

    return {
      ...item,
      duration_seconds_numeric: durationSeconds,
      max_spread_numeric: Number.isNaN(maxSpreadNumeric) ? Number.NEGATIVE_INFINITY : maxSpreadNumeric,
      start_time_ms: Date.parse(item.start_time),
      end_time_ms: Date.parse(item.end_time),
      formatted_start_time: formatDateTimeVladivostok(item.start_time),
      formatted_end_time: formatDateTimeVladivostok(item.end_time)
    }
  })
)

const historyRows = computed(() => {
  const rows = [...normalizedHistoryRows.value]
  const { column, direction } = historySort.value
  const order = direction === 'asc' ? 1 : -1

  if (!column) return rows

  rows.sort((a, b) => {
    if (column === 'start_time') {
      const aTime = Number.isNaN(a.start_time_ms) ? Number.NEGATIVE_INFINITY : a.start_time_ms
      const bTime = Number.isNaN(b.start_time_ms) ? Number.NEGATIVE_INFINITY : b.start_time_ms
      return (aTime - bTime) * order
    }

    if (column === 'end_time') {
      const aTime = Number.isNaN(a.end_time_ms) ? Number.NEGATIVE_INFINITY : a.end_time_ms
      const bTime = Number.isNaN(b.end_time_ms) ? Number.NEGATIVE_INFINITY : b.end_time_ms
      return (aTime - bTime) * order
    }

    if (column === 'duration') {
      return (a.duration_seconds_numeric - b.duration_seconds_numeric) * order
    }

    if (column === 'max_spread') {
      const primary = (a.max_spread_numeric - b.max_spread_numeric) * order
      if (primary !== 0) return primary
      return (a.duration_seconds_numeric - b.duration_seconds_numeric) * order
    }

    return 0
  })

  return rows
})

const toggleHistorySort = (column) => {
  const current = historySort.value
  if (current.column === column) {
    historySort.value = {
      column,
      direction: current.direction === 'asc' ? 'desc' : 'asc'
    }
    return
  }

  historySort.value = {
    column,
    direction: 'desc'
  }
}

const sortIndicator = (column) => {
  if (historySort.value.column !== column) return '↕'
  return historySort.value.direction === 'asc' ? '↑' : '↓'
}

const spreadClass = (spread) => {
  const numeric = Number.parseFloat(spread)
  if (Number.isNaN(numeric)) return ''
  if (numeric >= 1.5) return 'spread-high'
  if (numeric >= 0.8) return 'spread-medium'
  return ''
}

const formatDurationSeconds = (secondsInput) => {
  const seconds = Math.max(0, Number.parseInt(secondsInput ?? 0, 10) || 0)
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const secs = seconds % 60

  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return `${minutes}m ${secs}s`
  return `${secs}s`
}

const formatDurationFromStart = (startTime) => {
  const startMs = Date.parse(startTime)
  if (Number.isNaN(startMs)) return '-'
  const seconds = Math.floor((nowMs.value - startMs) / 1000)
  return formatDurationSeconds(seconds)
}

const fetchRaw = async () => {
  if (isFetchingRaw) return
  isFetchingRaw = true
  try {
    const controller = new AbortController()
    const timeoutId = setTimeout(() => controller.abort(), 5000)
    const response = await fetch('/api/raw', { signal: controller.signal })
    clearTimeout(timeoutId)
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    const payload = await response.json()
    rawSnapshot.value = payload.data || {}
  } catch (e) {
    console.error('fetchRaw error:', e)
  } finally {
    isFetchingRaw = false
  }
}

const fetchArbitrage = async () => {
  if (isFetchingArbitrage) return
  isFetchingArbitrage = true
  try {
    const controller = new AbortController()
    const timeoutId = setTimeout(() => controller.abort(), 5000)
    const response = await fetch('/api/arbitrage', { signal: controller.signal })
    clearTimeout(timeoutId)
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    const payload = await response.json()
    arbitrage.value = payload.data || []
  } catch (e) {
    console.error('fetchArbitrage error:', e)
  } finally {
    isFetchingArbitrage = false
  }
}

const fetchHistory = async () => {
  const response = await fetch('/api/history')
  const payload = await response.json()
  history.value = payload.data || []
}

const API_KEY = import.meta.env.VITE_API_KEY || ''

const clearHistory = async () => {
  const headers = {}
  if (API_KEY) {
    headers['X-API-Key'] = API_KEY
  }
  const response = await fetch('/api/history', { method: 'DELETE', headers })
  if (response.status === 403) {
    alert('Access denied: API key required to clear history')
    return
  }
  await fetchHistory()
}

const fetchSettingsSchema = async () => {
  try {
    const response = await fetch('/api/settings/schema')
    const payload = await response.json()
    settingsSchema.value = payload.data || []
  } catch (e) {
    console.error('Failed to load settings schema', e)
  }
}

const fetchSettings = async () => {
  try {
    const response = await fetch('/api/settings')
    const payload = await response.json()
    settingsValues.value = payload.data || {}
    settingsError.value = ''
  } catch (e) {
    console.error('Failed to load settings', e)
    settingsError.value = 'Failed to load settings'
  }
}

const settingsGroups = computed(() => {
  const groups = {}
  for (const item of settingsSchema.value) {
    const cat = item.category || 'Other'
    if (!groups[cat]) groups[cat] = { category: cat, fields: [] }
    groups[cat].fields.push(item)
  }
  return Object.values(groups)
})

const saveSettings = async () => {
  savingSettings.value = true
  settingsError.value = ''
  settingsSuccess.value = ''
  try {
    const headers = { 'Content-Type': 'application/json' }
    if (API_KEY) {
      headers['X-API-Key'] = settingsApiKey.value || API_KEY
    }
    const response = await fetch('/api/settings', {
      method: 'POST',
      headers,
      body: JSON.stringify(settingsValues.value)
    })
    if (response.status === 403) {
      settingsError.value = 'Access denied: invalid API key'
      return
    }
    if (!response.ok) {
      const err = await response.json()
      settingsError.value = err.detail?.errors ? JSON.stringify(err.detail.errors) : 'Save failed'
      return
    }
    settingsSuccess.value = 'Settings saved successfully'
  } catch (e) {
    settingsError.value = 'Network error while saving'
  } finally {
    savingSettings.value = false
  }
}

onMounted(() => {
  fetchRaw()
  fetchArbitrage()
  fetchHistory()
  fetchSettingsSchema()
  fetchSettings()
  rawPollTimer = setInterval(fetchRaw, 1000)
  arbitragePollTimer = setInterval(fetchArbitrage, 1000)
  durationTimer = setInterval(() => {
    nowMs.value = Date.now()
  }, 1000)
  settingsPollTimer = setInterval(fetchSettings, 5000)
})

onUnmounted(() => {
  if (rawPollTimer) {
    clearInterval(rawPollTimer)
  }
  if (arbitragePollTimer) {
    clearInterval(arbitragePollTimer)
  }
  if (durationTimer) {
    clearInterval(durationTimer)
  }
  if (settingsPollTimer) {
    clearInterval(settingsPollTimer)
  }
})
</script>
