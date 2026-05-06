<script setup lang="ts">
import { onMounted, onBeforeUnmount, watch, ref } from 'vue'
import { Chart, registerables } from 'chart.js'

Chart.register(...registerables)

interface ModelEntry {
  timestamp: string
  results: { model: string; status: string; latency_s?: number }[]
}

const props = defineProps<{ entries: ModelEntry[] }>()

const canvas = ref<HTMLCanvasElement | null>(null)
let chart: Chart | null = null

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

function buildAvgSeries(): { x: number; y: number }[] {
  const out: { x: number; y: number }[] = []
  for (const e of props.entries) {
    const ok = e.results.filter((r) => r.status === 'ok' && r.latency_s != null)
    if (!ok.length) continue
    const avg =
      ok.reduce((a, b) => a + (b.latency_s ?? 0), 0) / ok.length
    out.push({ x: new Date(e.timestamp).getTime(), y: avg })
  }
  return out
}

function render() {
  if (!canvas.value) return
  if (chart) chart.destroy()
  chart = new Chart(canvas.value, {
    type: 'line',
    data: {
      datasets: [
        {
          label: 'avg latency (s)',
          data: buildAvgSeries(),
          borderColor: cssVar('--accent') || '#4ade80',
          backgroundColor: 'transparent',
          tension: 0.25,
          pointRadius: 1.5,
          borderWidth: 1.4
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      scales: {
        x: {
          type: 'linear',
          ticks: {
            color: cssVar('--muted'),
            font: { family: 'Space Mono', size: 10 },
            maxTicksLimit: 6,
            callback: (v) => {
              const d = new Date(Number(v))
              return `${d.getMonth() + 1}/${d.getDate()}`
            }
          },
          grid: { color: cssVar('--chart-grid') }
        },
        y: {
          beginAtZero: true,
          ticks: {
            color: cssVar('--muted'),
            font: { family: 'Space Mono', size: 10 }
          },
          grid: { color: cssVar('--chart-grid') }
        }
      },
      plugins: { legend: { display: false } }
    }
  })
}

onMounted(render)
watch(() => props.entries, render, { deep: true })
onBeforeUnmount(() => chart?.destroy())
</script>

<template>
  <div class="card chart-card">
    <h3>Average latency</h3>
    <div class="chart-wrap">
      <canvas ref="canvas"></canvas>
    </div>
  </div>
</template>

<style scoped>
.chart-card {
  display: flex;
  flex-direction: column;
}
.chart-wrap {
  position: relative;
  flex: 1;
  min-height: 220px;
}
</style>
