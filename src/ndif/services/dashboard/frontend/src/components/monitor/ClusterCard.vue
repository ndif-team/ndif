<script setup lang="ts">
import { computed } from 'vue'

interface ClusterEntry {
  timestamp: string
  nodes: number
  total_gpus: number
  total_memory_bytes: number
  available_memory_bytes: number
  node_details: {
    node_id: string
    gpus: number
    memory_bytes: number
    available_bytes: number
    deployments: string[]
  }[]
}

const props = defineProps<{ entries: ClusterEntry[] }>()

const latest = computed<ClusterEntry | null>(() => {
  return props.entries.length ? props.entries[props.entries.length - 1] : null
})

function gb(bytes: number): string {
  return (bytes / 1024 ** 3).toFixed(1) + ' GB'
}
function pct(used: number, total: number): number {
  return total > 0 ? (used / total) * 100 : 0
}
</script>

<template>
  <div class="card">
    <h3>Cluster</h3>
    <div v-if="latest" class="grid">
      <div class="row stats">
        <div>
          <div class="muted">Nodes</div>
          <div class="big">{{ latest.nodes }}</div>
        </div>
        <div>
          <div class="muted">GPUs</div>
          <div class="big">{{ latest.total_gpus }}</div>
        </div>
        <div>
          <div class="muted">Memory used</div>
          <div class="big">
            {{ gb(latest.total_memory_bytes - latest.available_memory_bytes) }} /
            {{ gb(latest.total_memory_bytes) }}
          </div>
        </div>
      </div>

      <div class="nodes">
        <div v-for="n in latest.node_details" :key="n.node_id" class="node">
          <div class="node-head">
            <span class="mono">{{ n.node_id }}</span>
            <span class="muted">{{ n.gpus }} GPU</span>
          </div>
          <div class="bar">
            <div
              class="bar-fill"
              :style="{ width: pct(n.memory_bytes - n.available_bytes, n.memory_bytes) + '%' }"
            ></div>
          </div>
          <div class="muted bar-label">
            <span>{{ gb(n.memory_bytes - n.available_bytes) }} used</span>
            <span>{{ gb(n.memory_bytes) }} total</span>
          </div>
          <ul class="deps">
            <li v-for="d in n.deployments" :key="d" class="mono">{{ d }}</li>
            <li v-if="!n.deployments.length" class="muted">— no deployments</li>
          </ul>
        </div>
      </div>
    </div>
    <p v-else class="muted">No cluster snapshots yet</p>
  </div>
</template>

<style scoped>
.stats {
  gap: 2rem;
  flex-wrap: wrap;
}
.big {
  font-family: 'VT323', monospace;
  font-size: 1.6rem;
  color: var(--accent);
  letter-spacing: 0.04em;
}
.muted {
  font-size: 0.7rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.nodes {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 0.75rem;
}
.node {
  border: 1px solid var(--border);
  padding: 0.7rem;
}
.node-head {
  display: flex;
  justify-content: space-between;
  margin-bottom: 0.4rem;
}
.bar {
  height: 8px;
  background: var(--border);
  margin-bottom: 0.25rem;
}
.bar-fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.3s;
}
.bar-label {
  display: flex;
  justify-content: space-between;
  font-size: 0.6rem;
  margin-bottom: 0.4rem;
}
.deps {
  list-style: none;
  font-size: 0.7rem;
  line-height: 1.4;
}
.mono {
  font-family: 'Space Mono', monospace;
}
</style>
