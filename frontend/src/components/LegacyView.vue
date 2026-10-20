<script setup>
import {ref, onMounted, onBeforeUnmount} from 'vue';
import {useStudio} from '../context.js';
const props = defineProps({page: String});
const studio = useStudio();
const host = ref(null);
const error = ref('');
const controller = new AbortController();
const disposals = [];
onBeforeUnmount(() => { controller.abort(); disposals.splice(0).forEach(fn => fn()); });
onMounted(async () => {
  const ctx = {
    projectId: studio.state.projectId, projects: studio.state.projects,
    params: studio.route.value.params, query: studio.route.value.query, signal: controller.signal,
    reload: studio.refreshPage, navigate: studio.navigate,
    onDispose(fn) { if (controller.signal.aborted) fn(); else disposals.push(fn); },
    poll(fn, interval=4000) {
      let busy=false;
      const timer=setInterval(async () => {
        if (busy || controller.signal.aborted || document.hidden) return;
        busy=true;
        try { await fn(); } catch(e) { if(e.name!=='AbortError') studio.notify(e.message,'error'); } finally { busy=false; }
      },interval);
      disposals.push(() => clearInterval(timer)); return timer;
    }
  };
  try {
    const name = {projects:'project', assets:'assets', jobs:'jobs', costs:'costs', settings:'settings', system:'system'}[props.page];
    if(!name) throw new Error('没有找到这个页面');
    const module = await import(/* @vite-ignore */ `/static/pages/${name}.js`);
    const element = await module.render(ctx);
    if(!controller.signal.aborted) host.value.replaceChildren(element);
  } catch(e) { if(e.name!=='AbortError') error.value=e.message; }
  finally { if(!controller.signal.aborted) studio.ready(); }
});
</script>
<template><div class="legacy-host"><div v-if="error" class="empty-state"><h2>页面暂时没有加载</h2><p>{{error}}</p><button class="studio-button primary" @click="studio.refreshPage">重新加载</button></div><div ref="host"/></div></template>
