<script setup>
import {ref} from 'vue';
import LegacyView from '../components/LegacyView.vue';
import PerformancePlan from './PerformancePlan.vue';
const planningOpen = ref(false);
</script>

<template>
  <div class="performance-entry">
    <LegacyView page="performance"/>
    <details class="studio-card performance-planning" @toggle="planningOpen = $event.target.open">
      <summary>展开模型路线与原创 MV 制作草案</summary>
      <p class="studio-note">这里保留选型资料与浏览器草案；实际唱跳换人、换声、换场景请使用上方工作台。</p>
      <PerformancePlan v-if="planningOpen"/>
    </details>
  </div>
</template>

<style scoped>
.performance-entry {display: grid; gap: 24px; min-width: 0;}
.performance-planning {padding: 22px; min-width: 0;}
.performance-planning > summary {cursor: pointer; font-weight: 600;}
.performance-planning[open] > summary {margin-bottom: 16px;}
</style>
