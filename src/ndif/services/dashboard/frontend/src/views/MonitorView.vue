<script setup lang="ts">
import { onMounted, ref, computed, onBeforeUnmount } from 'vue'
import { api } from '@/api'
import ConnectivityCalendar from '@/components/monitor/ConnectivityCalendar.vue'
import LatencyChart from '@/components/monitor/LatencyChart.vue'
import ModelTimeline from '@/components/monitor/ModelTimeline.vue'
import ClusterCard from '@/components/monitor/ClusterCard.vue'

const connected = ref<{ timestamp: string; status: string }[]>([])
const models = ref<any[]>([])
const cluster = ref<any[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

let refreshTimer: number | undefined

async function loadAll() {
  try {
    const [c, m, cl] = await Promise.all([
      api.get<any>('/api/monitor/connected'),
      api.get<any>('/api/monitor/models'),
      api.get<any>('/api/monitor/cluster')
    ])
    connected.value = c
    models.value = m
    cluster.value = cl
    error.value = null
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    loading.value = false
  }
}

const headerStatus = computed(() => {
  if (!connected.value.length) return { text: 'no data', cls: '' }
  const last = connected.value[connected.value.length - 1]
  if (last.status === 'ok') return { text: 'connected', cls: 'ok' }
  return { text: last.status, cls: 'bad' }
})

const stats = computed(() => {
  const total = connected.value.length
  const ok = connected.value.filter((e) => e.status === 'ok').length
  const apiUptime = total ? (ok / total) * 100 : 0

  const lastM = models.value.length ? models.value[models.value.length - 1] : null
  const allLat: number[] = []
  for (const e of models.value) {
    for (const r of e.results || []) {
      if (r.status === 'ok' && r.latency_s != null) allLat.push(r.latency_s)
    }
  }
  const avgLat = allLat.length ? allLat.reduce((a, b) => a + b, 0) / allLat.length : null

  const allTs = [
    ...connected.value.map((e: any) => e.timestamp),
    ...models.value.map((e: any) => e.timestamp)
  ].filter(Boolean)
  allTs.sort()
  const lastTs = allTs.length ? allTs[allTs.length - 1] : null

  return { apiUptime, lastM, avgLat, lastTs, total, ok }
})

function timeAgo(ts: string | null): string {
  if (!ts) return '—'
  const sec = Math.floor((Date.now() - new Date(ts).getTime()) / 1000)
  if (sec < 60) return `${sec}s ago`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`
  return `${Math.floor(sec / 86400)}d ago`
}

onMounted(async () => {
  await loadAll()
  refreshTimer = window.setInterval(loadAll, 5 * 60 * 1000)
})
onBeforeUnmount(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<template>
  <section class="monitor">
    <div class="row monitor-head">
      <h1 class="page-title">Monitor</h1>
      <span :class="['pill', headerStatus.cls]">{{ headerStatus.text }}</span>
      <button class="btn" @click="loadAll">Refresh</button>
    </div>

    <div v-if="error" class="card error">{{ error }}</div>

    <div class="stats-row">
      <div class="card stat">
        <div class="muted">API uptime</div>
        <div class="big">{{ stats.total ? stats.apiUptime.toFixed(1) + '%' : '—' }}</div>
        <div class="muted small">{{ stats.ok }}/{{ stats.total }} checks</div>
      </div>
      <div class="card stat">
        <div class="muted">Models OK</div>
        <div class="big">
          {{ stats.lastM ? `${stats.lastM.ok}/${stats.lastM.total}` : '—' }}
        </div>
        <div class="muted small">last check</div>
      </div>
      <div class="card stat">
        <div class="muted">Avg latency</div>
        <div class="big">{{ stats.avgLat != null ? stats.avgLat.toFixed(2) + 's' : '—' }}</div>
        <div class="muted small">all models</div>
      </div>
      <div class="card stat">
        <div class="muted">Last check</div>
        <div class="big">{{ timeAgo(stats.lastTs) }}</div>
        <div class="muted small">{{ stats.lastTs ?? '' }}</div>
      </div>
    </div>

    <div class="grid main-grid">
      <ConnectivityCalendar :entries="connected" />
      <LatencyChart :entries="models" />
    </div>

    <ModelTimeline :entries="models" />
    <ClusterCard :entries="cluster" />
  </section>
</template>

<style scoped>
.monitor {
  display: grid;
  gap: 1rem;
}
.monitor-head {
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
}
.page-title {
  font-family: 'VT323', monospace;
  font-weight: normal;
  font-size: 1.6rem;
  letter-spacing: 0.06em;
  margin-right: auto;
}
.error {
  color: var(--red);
}
.stats-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 1rem;
}
.stat .big {
  font-family: 'VT323', monospace;
  color: var(--accent);
  font-size: 1.8rem;
  letter-spacing: 0.04em;
  line-height: 1.1;
  margin: 0.25rem 0;
}
.stat .muted {
  font-size: 0.7rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.stat .small {
  font-size: 0.6rem;
  letter-spacing: 0.05em;
  text-transform: none;
}
.main-grid {
  grid-template-columns: 1fr 1fr;
}
@media (max-width: 880px) {
  .main-grid {
    grid-template-columns: 1fr;
  }
}
</style>
