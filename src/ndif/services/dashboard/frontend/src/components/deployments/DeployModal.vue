<script setup lang="ts">
import { ref, watch } from 'vue'
import { DEFAULT_ENVOY_CLASS } from '@/deploy'

export interface DeployForm {
  checkpoint: string
  revision: string | null
  actor_class: string | null
  envoy_class: string | null
  padding_factor: number | null
  execution_timeout_seconds: number | null
  pinned: boolean
}

const props = defineProps<{
  initial?: Partial<DeployForm>
  saving?: boolean
  error?: string | null
}>()

const emit = defineEmits<{
  submit: [form: DeployForm]
  close: []
}>()

const form = ref<DeployForm>({
  checkpoint: '',
  revision: null,
  actor_class: null,
  envoy_class: DEFAULT_ENVOY_CLASS,
  padding_factor: null,
  execution_timeout_seconds: null,
  pinned: false,
  ...(props.initial ?? {})
})

watch(
  () => props.initial,
  (v) => {
    if (v) form.value = { ...form.value, ...v }
  }
)

function submit() {
  emit('submit', { ...form.value })
}
</script>

<template>
  <div class="modal-backdrop" @click.self="emit('close')">
    <div class="modal">
      <h3>Deploy a model</h3>

      <form class="grid form-grid" @submit.prevent="submit">
        <label class="field full">
          Checkpoint (HF repo id)
          <input
            v-model="form.checkpoint"
            placeholder="meta-llama/Llama-3.1-8B"
            autofocus
            required
          />
        </label>

        <label class="field">
          Revision
          <input v-model="form.revision" placeholder="(default)" />
        </label>

        <label class="field">
          Actor class
          <input v-model="form.actor_class" placeholder="(default)" />
        </label>

        <label class="field full">
          Envoy class
          <input
            v-model="form.envoy_class"
            placeholder="nnsight.modeling.language.LanguageModel"
          />
        </label>

        <label class="field">
          Padding factor
          <input
            type="number"
            step="0.01"
            min="0"
            v-model.number="form.padding_factor"
            placeholder="(default)"
          />
        </label>

        <label class="field">
          Execution timeout (s)
          <input
            type="number"
            step="1"
            min="0"
            v-model.number="form.execution_timeout_seconds"
            placeholder="(default)"
          />
        </label>

        <label class="field full check-field">
          <input type="checkbox" v-model="form.pinned" />
          <span>
            <strong>Pinned</strong>
            <span class="muted">— won't be evicted by hotswapping.</span>
          </span>
        </label>

        <p v-if="error" class="error full">{{ error }}</p>

        <div class="actions full">
          <button type="button" class="btn" @click="emit('close')">Cancel</button>
          <button type="submit" class="btn primary" :disabled="saving">
            {{ saving ? '...' : 'Deploy' }}
          </button>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.form-grid {
  grid-template-columns: 1fr 1fr;
  gap: 0.85rem;
}
.field.full { grid-column: 1 / -1; }
.check-field {
  flex-direction: row;
  align-items: center;
  gap: 0.55rem;
  text-transform: none;
  letter-spacing: 0.02em;
}
.check-field input[type='checkbox'] {
  width: 1rem;
  height: 1rem;
  margin: 0;
}
.check-field strong { color: var(--text); }
.check-field .muted { color: var(--muted); font-size: 0.75rem; }
.actions.full {
  grid-column: 1 / -1;
  display: flex;
  gap: 0.5rem;
  justify-content: flex-end;
}
.error {
  color: var(--red);
  font-size: 0.8rem;
}
</style>
