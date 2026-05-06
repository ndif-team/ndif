<script setup lang="ts">
import { computed } from 'vue'

interface ModelEntry {
  timestamp: string
  results: { model: string; status: string; latency_s?: number; error?: string }[]
}

const props = defineProps<{ entries: ModelEntry[] }>()

const SLOT_MS = 2 * 60 * 60 * 1000 // 2-hour slots
const WINDOW_DAYS = 30
const CHECK_VALIDITY_MS = (2 * 60 + 15) * 60 * 1000 // 2h15m grace per slot

type SegStatus = 'ok' | 'fail' | 'cold' | 'gap'

interface Row {
  model: string
  isHot: boolean // appeared in the most recent monitor run
  lastLatency: number | null
  segs: { status: SegStatus; label: string }[]
}

const rendered = computed<{ rows: Row[]; firstSlot: Date; totalSlots: number } | null>(() => {
  const entries = props.entries
  if (!entries.length) return null

  // 1. Window
  const now = new Date()
  const firstSlot = new Date(now)
  firstSlot.setUTCDate(firstSlot.getUTCDate() - (WINDOW_DAYS - 1))
  firstSlot.setUTCHours(0, 0, 0, 0)
  const totalSlots = Math.ceil((now.getTime() - firstSlot.getTime()) / SLOT_MS)

  // 2. Sorted (timestamp asc) check list — each entry maps model -> status
  const checks: { time: Date; results: Record<string, string> }[] = entries
    .map((e) => {
      const r: Record<string, string> = {}
      for (const x of e.results || []) r[x.model] = x.status
      return { time: new Date(e.timestamp), results: r }
    })
    .sort((a, b) => a.time.getTime() - b.time.getTime())

  const firstCheckTime = checks.length ? checks[0].time : null

  // 3. Set of all model names ever observed
  const allModels = new Set<string>()
  for (const c of checks) for (const m of Object.keys(c.results)) allModels.add(m)

  // 4. Models seen in the latest check = currently HOT (per the cron's
  //    HOT-only filter in extract_hot_models)
  const latestModels = checks.length
    ? new Set(Object.keys(checks[checks.length - 1].results))
    : new Set<string>()

  // 5. Latest status (ok/fail) for each model from the most recent check
  //    that included it
  const lastStatus: Record<string, string> = {}
  for (let i = checks.length - 1; i >= 0; i--) {
    for (const [m, s] of Object.entries(checks[i].results)) {
      if (!(m in lastStatus)) lastStatus[m] = s
    }
  }

  // 6. Latest latency per model
  const lastLatency: Record<string, number | null> = {}
  for (const m of allModels) lastLatency[m] = null
  for (let i = entries.length - 1; i >= 0; i--) {
    for (const r of entries[i].results || []) {
      if (lastLatency[r.model] == null && r.status === 'ok' && r.latency_s != null) {
        lastLatency[r.model] = r.latency_s
      }
    }
  }

  // 7. Sort: hot+failing first (red dot), hot+ok next (green), not-hot last (blue)
  const sorted = Array.from(allModels).sort((a, b) => {
    const aHot = latestModels.has(a)
    const bHot = latestModels.has(b)
    const aOk = lastStatus[a] === 'ok'
    const bOk = lastStatus[b] === 'ok'
    const aOrder = !aHot ? 2 : !aOk ? 0 : 1
    const bOrder = !bHot ? 2 : !bOk ? 0 : 1
    if (aOrder !== bOrder) return aOrder - bOrder
    return a.localeCompare(b)
  })

  // 8. Per-row segments
  const rows: Row[] = sorted.map((name) => {
    const segs: Row['segs'] = []
    for (let i = 0; i < totalSlots; i++) {
      const slotStart = new Date(firstSlot.getTime() + i * SLOT_MS)
      const slotEnd = new Date(slotStart.getTime() + SLOT_MS)

      let status: SegStatus
      let label: string

      if (!firstCheckTime || slotEnd.getTime() <= firstCheckTime.getTime()) {
        status = 'gap'
        label = 'not recording'
      } else {
        // Find the most recent check at or before slotEnd
        let lastCheck: (typeof checks)[number] | null = null
        for (let j = checks.length - 1; j >= 0; j--) {
          if (checks[j].time.getTime() < slotEnd.getTime()) {
            lastCheck = checks[j]
            break
          }
        }
        const checkAgainst = slotEnd.getTime() > now.getTime() ? now.getTime() : slotEnd.getTime()
        if (!lastCheck || checkAgainst - lastCheck.time.getTime() > CHECK_VALIDITY_MS) {
          // No recent check ran — monitor was down
          status = 'fail'
          label = 'no check'
        } else if (lastCheck.results[name] !== undefined) {
          // The check ran AND included this model (= it was HOT)
          status = lastCheck.results[name] === 'ok' ? 'ok' : 'fail'
          label = lastCheck.results[name]
        } else {
          // The check ran but did not include this model — it wasn't HOT
          status = 'cold'
          label = 'not hot'
        }
      }

      segs.push({ status, label })
    }

    return {
      model: name,
      isHot: latestModels.has(name),
      lastLatency: lastLatency[name],
      segs
    }
  })

  return { rows, firstSlot, totalSlots }
})

function fmtLat(s: number | null): string {
  return s == null ? '—' : `${s.toFixed(1)}s`
}
</script>

<template>
  <div class="card">
    <h3>Models · 30 days</h3>
    <div v-if="!rendered" class="muted center">No model checks recorded yet.</div>
    <template v-else>
      <div v-for="row in rendered.rows" :key="row.model" class="row">
        <div class="row-head">
          <span :class="['dot', row.isHot ? 'hot' : 'cold']"></span>
          <span class="name" :title="row.model">{{ row.model }}</span>
          <span class="lat">{{ fmtLat(row.lastLatency) }}</span>
        </div>
        <div class="tl" :style="{ '--slots': rendered.totalSlots }">
          <span
            v-for="(seg, i) in row.segs"
            :key="i"
            :class="['seg', seg.status]"
            :title="seg.label"
          ></span>
        </div>
      </div>
      <div class="tl-labels">
        <span>30d ago</span>
        <span>20d</span>
        <span>10d</span>
        <span>now</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.row {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 0.35rem 0;
  border-top: 1px solid var(--border);
}
.row:first-of-type {
  border-top: none;
}
.row-head {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.78rem;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.dot.hot {
  background: var(--green);
  box-shadow: 0 0 6px var(--green-soft);
}
.dot.cold {
  background: var(--blue);
}
.name {
  font-family: 'Space Mono', monospace;
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text);
}
.lat {
  font-family: 'Space Mono', monospace;
  font-size: 0.7rem;
  color: var(--muted);
  flex-shrink: 0;
}
.tl {
  display: grid;
  grid-template-columns: repeat(var(--slots, 360), 1fr);
  gap: 1px;
  height: 14px;
  width: 100%;
}
.seg {
  height: 100%;
}
.seg.ok {
  background: var(--green);
}
.seg.fail {
  background: var(--red);
}
.seg.cold {
  background: var(--blue);
}
.seg.gap {
  background: var(--border);
  opacity: 0.4;
}
.tl-labels {
  display: flex;
  justify-content: space-between;
  font-size: 0.65rem;
  color: var(--muted);
  margin-top: 0.5rem;
  letter-spacing: 0.05em;
  font-family: 'Space Mono', monospace;
}
.center {
  text-align: center;
  padding: 1.5rem 0;
}
</style>
