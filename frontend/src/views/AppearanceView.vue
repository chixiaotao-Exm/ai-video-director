<script setup>
import {onMounted} from 'vue';
import {THEMES} from '../themes.js';
import {useStudio} from '../context.js';
import StudioIcon from '../components/StudioIcon.vue';
const studio=useStudio();
onMounted(studio.ready);
</script>
<template><div class="appearance-view"><header class="workspace-title"><div><span class="hero-kicker">MAKE IT YOURS</span><h1>你的工作室，你的气质。</h1><p>四套完整的设计语言。切换即时生效，当前作品和输入都会保留。</p></div><span class="studio-badge"><StudioIcon name="palette" :size="16"/>外观实验室</span></header><div class="theme-grid"><button v-for="theme in THEMES" :key="theme.id" class="theme-card" :class="{selected:studio.theme.value===theme.id}" :aria-pressed="studio.theme.value===theme.id" @click="studio.selectTheme(theme.id)"><div class="theme-preview" :data-preview-theme="theme.id"><div class="preview-sidebar"><i/><i/><i/><i/></div><div class="preview-main"><div class="preview-top"><i/><span/></div><div class="preview-hero"><div><span/><span/><i/></div><div class="preview-art"><StudioIcon name="music" :size="30"/></div></div><div class="preview-cards"><i/><i/><i/></div></div><span class="theme-check" v-if="studio.theme.value===theme.id"><StudioIcon name="check" :size="18"/></span></div><div class="theme-card-body"><div><span class="hero-kicker">{{theme.subtitle}}</span><h2>{{theme.name}}</h2></div><p>{{theme.description}}</p><div class="theme-bottom"><span>{{theme.mood}}</span><span class="swatch-row"><i v-for="color in theme.swatches" :key="color" class="swatch" :style="{background:color}"/></span></div></div></button></div><div class="studio-card appearance-note"><StudioIcon name="shield" :size="22"/><div><h3>好看的界面，也应该用得舒服。</h3><p>动画会跟随系统“减少动态效果”设置。外观偏好只保存在这个浏览器，不会修改项目、模型配置或账单。iOS 27 方案为独立的 Apple 风格灵感设计，其他方案是面向 2027 的概念外观。</p></div></div></div></template>
