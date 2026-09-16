<script setup lang="ts">
/**
 * A thin ECharts host.
 *
 * Deliberately generic: Phase 7's monitoring page needs the same lifecycle, and
 * a per-chart wrapper would mean writing the dispose-and-resize dance twice and
 * getting it wrong once.
 *
 * Three things this handles that a naive `<div ref>` plus `echarts.init` does
 * not:
 *
 * * **Disposal.** An ECharts instance holds a canvas, a resize observer and a
 *   global registry entry. Leaving one behind on every route change leaks all
 *   three, and the symptom is a page that gets slower the longer somebody uses
 *   the app rather than an error anybody can point at.
 * * **Resize.** The sidebar collapses and the window resizes; a chart that does
 *   not observe its own container keeps its old width and clips.
 * * **Watching.** The option is rebuilt from fetched data, so a chart that only
 *   sets the option on mount renders once and then shows stale figures forever.
 */
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import {
  DatasetComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'

// Registered once at module scope: `echarts.use` is idempotent but the import
// graph would otherwise re-run it for every chart on the page.
echarts.use([
  BarChart,
  LineChart,
  PieChart,
  DatasetComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  CanvasRenderer,
])

const props = withDefaults(
  defineProps<{
    // ECharts' own option type is a deep union that is awkward to import
    // piecemeal; the option is validated by the chart library at runtime far
    // better than it could be by a structural type here.
    option: Record<string, unknown>
    height?: string
    /** Skip the built-in "no data" state — the caller is drawing its own. */
    loading?: boolean
  }>(),
  { height: '280px', loading: false },
)

const host = ref<HTMLDivElement | null>(null)
// `shallowRef`: ECharts instances are large, self-mutating graphs. Deep
// reactivity would walk every node on each update and buy nothing.
const chart = shallowRef<echarts.ECharts | null>(null)
const failed = ref(false)
let observer: ResizeObserver | null = null

onMounted(() => {
  if (!host.value) return
  try {
    chart.value = echarts.init(host.value, undefined, { renderer: 'canvas' })
    chart.value.setOption(props.option)
  } catch (error) {
    // A canvas that will not give up a 2D context — an old browser, a hardened
    // profile, a headless renderer — must not take the page down with it. The
    // figures are all present as text elsewhere on the page, so the honest thing
    // is to say the drawing failed and leave the rest usable.
    failed.value = true
    console.warn('chart_init_failed', error)
    return
  }

  observer = new ResizeObserver(() => chart.value?.resize())
  observer.observe(host.value)
})

watch(
  () => props.option,
  (option) => {
    // `notMerge` is the default because the option is rebuilt from scratch each
    // time; merging would leave series from a previous filter on screen.
    try {
      chart.value?.setOption(option, { notMerge: true })
    } catch (error) {
      failed.value = true
      console.warn('chart_option_failed', error)
    }
  },
  { deep: true },
)

watch(
  () => props.loading,
  (loading) => {
    if (loading) {
      chart.value?.showLoading('default', { text: '', maskColor: 'rgba(255,255,255,0.6)' })
    } else {
      chart.value?.hideLoading()
    }
  },
)

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  chart.value?.dispose()
  chart.value = null
})
</script>

<template>
  <div v-if="failed" class="wl-chart-failed" :style="{ height }">
    <v-icon icon="mdi-chart-box-outline" size="28" class="mb-2" />
    <div class="text-body-2">图表无法绘制（浏览器不支持 Canvas），上方与下方的数值不受影响。</div>
  </div>
  <div v-else ref="host" class="wl-chart" :style="{ height }" />
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-chart {
  width: 100%;
}

.wl-chart-failed {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  color: $text-grey;
  border: 1px dashed rgba(0, 0, 0, 0.12);
  border-radius: 6px;
  padding: 0 16px;
}
</style>
