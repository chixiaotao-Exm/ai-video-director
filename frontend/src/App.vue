<script setup>
import {computed, nextTick, onBeforeUnmount, onMounted, provide, reactive, ref, watch} from 'vue';
import StudioIcon from './components/StudioIcon.vue';
import LegacyView from './components/LegacyView.vue';
import StudioHome from './views/StudioHome.vue';
import AppearanceView from './views/AppearanceView.vue';
import PerformanceView from './views/PerformanceView.vue';
import VersionCenter from './views/VersionCenter.vue';
import {api, formatCount, routeInfo} from './api.js';
import {THEMES, DEFAULT_THEME, resolveTheme} from './themes.js';

function stored(key, fallback='') { try { return localStorage.getItem(key) || fallback; } catch { return fallback; } }
const theme = ref(resolveTheme(stored('avd.studio.theme',DEFAULT_THEME)));
const hash = ref(location.hash);
const route = computed(() => routeInfo(hash.value));
const sideOpen = ref(false);
const themeMenuOpen = ref(false);
const initialized = ref(false);
const epoch = ref(0);
const state = reactive({projects:[],projectId:stored('avd.project'),notification:null,connection:'connecting'});
const createDialog = ref(null);
const commandDialog = ref(null);
const commandQuery = ref('');
const creating = ref(false);
const creationNonce = ref(0);
const creationError = ref('');
const draft = reactive({title:'',mode:'remake',file:null,projectId:null});
const navigation = [
  {key:'dashboard',title:'创作空间',icon:'home',href:'#/'},
  {key:'performance',title:'唱跳演出',icon:'music',href:'#/performance',tag:'NEW'},
  {key:'anime',title:'动漫续作',icon:'sparkles',href:'#/anime'},
  {key:'projects',title:'当前项目',icon:'film',href:'#/projects'},
  {key:'assets',title:'素材空间',icon:'layers',href:'#/assets'},
  {key:'jobs',title:'任务队列',icon:'jobs',href:'#/jobs'},
  {key:'costs',title:'用量与费用',icon:'wallet',href:'#/costs'},
  {key:'appearance',title:'外观实验室',icon:'palette',href:'#/appearance'},
  {key:'settings',title:'连接与设置',icon:'settings',href:'#/settings'},
  {key:'system',title:'系统状态',icon:'system',href:'#/system'},
  {key:'versions',title:'版本中心',icon:'git',href:'#/versions'}
];
const selectedProject = computed(() => state.projects.find(item=>item.id===state.projectId));
const pageTitle = computed(() => navigation.find(item=>item.key===route.value.key)?.title || '工作室');
const currentTheme = computed(() => THEMES.find(item=>item.id===theme.value));
const currentView = computed(() => ({dashboard:StudioHome,anime:StudioHome,performance:PerformanceView,appearance:AppearanceView,versions:VersionCenter}[route.value.key] || LegacyView));
const commands = computed(() => [...navigation.map(item=>({...item,id:item.key,caption:'前往页面'})),...state.projects.map(item=>({id:item.id,title:item.title,icon:'film',href:`#/projects/${item.id}/chat`,caption:'打开项目'}))].filter(item=>item.title.toLowerCase().includes(commandQuery.value.toLowerCase())).slice(0,14));
let notificationTimer;
let projectRefreshId=0;

function notify(text,type='success') {
  clearTimeout(notificationTimer); state.notification={text,type};
  notificationTimer=setTimeout(()=>{state.notification=null;},6500);
}
async function ready() { await nextTick(); window.__AVD_READY=true; document.body.dataset.appReady='true'; }
function selectTheme(value) { theme.value=resolveTheme(value); themeMenuOpen.value=false; }
watch(theme,value=>{
  document.documentElement.dataset.theme=value;
  document.documentElement.style.colorScheme=value==='midnight'?'dark':'light';
  try{localStorage.setItem('avd.studio.theme',value);}catch{/* optional browser storage */}
},{immediate:true});
function setProject(value) {
  state.projectId=value;
  try{localStorage.setItem('avd.project',value);}catch{/* optional */}
}
function navigate(destination) {
  const next=destination.startsWith('#')?destination:`#/${destination.replace(/^\//,'')}`;
  sideOpen.value=false;
  if(next==='#/projects' && state.projectId) return navigate(`#/projects/${state.projectId}/chat`);
  if(location.hash===next) return refreshPage();
  location.hash=next;
}
async function refreshProjects() {
  const requestId=++projectRefreshId;
  let projects;
  try{projects=await api('/projects');}
  catch(error){if(requestId!==projectRefreshId)return false;throw error;}
  if(requestId!==projectRefreshId)return false;
  state.projects=projects;
  const routed=route.value.key==='projects'?route.value.params[0]:null;
  if(routed && state.projects.some(item=>item.id===routed)) setProject(routed);
  else if(!state.projects.some(item=>item.id===state.projectId)) setProject(state.projects[0]?.id || '');
  if(routed && !state.projects.some(item=>item.id===routed)) {
    location.hash=state.projectId?`#/projects/${state.projectId}/chat`:'#/projects';
  }
  return true;
}
async function refreshPage() {
  try{await refreshProjects();epoch.value++;state.connection='connected';}
  catch(error){state.connection='offline';notify(error.message,'error');}
}
function switchProject(event) {
  setProject(event.target.value);
  if(route.value.key==='projects') navigate(`#/projects/${state.projectId}/chat`);
  else epoch.value++;
}
function openProject(options={}) {
  draft.title=options.title || '';draft.mode=options.mode || 'remake';draft.file=null;draft.projectId=null;
  creationNonce.value++;creationError.value='';createDialog.value?.showModal();
}
async function createProject() {
  if(creating.value) return;
  creating.value=true;creationError.value='';
  try{
    if(!draft.projectId){
      const result=await api('/projects',{method:'POST',body:{title:draft.title.trim() || draft.file?.name?.replace(/\.[^.]+$/,'') || '未命名作品',mode:draft.mode}});
      draft.projectId=result.id;
    }
    if(draft.file){
      const body=new FormData();body.append('file',draft.file);
      await api(`/projects/${draft.projectId}/source`,{method:'POST',body});
    }
    await refreshProjects();setProject(draft.projectId);createDialog.value.close();
    navigate(`#/projects/${draft.projectId}/chat`);notify(draft.file?'参考片已上传，导演会在对话中汇报分析结果。':'作品已创建，从一句创作想法开始。');
  }catch(error){creationError.value=`${error.message}${draft.projectId?' · 项目已保留，可直接重试上传。':''}`;}
  finally{creating.value=false;}
}
function openCommand() {commandQuery.value='';commandDialog.value?.showModal();}
function goCommand(command){commandDialog.value.close();navigate(command.href);}
function onKey(event){if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'){event.preventDefault();openCommand();}if(event.key==='Escape'){sideOpen.value=false;themeMenuOpen.value=false;}}
function onDocumentClick(event){if(!(event.target instanceof Element)||!event.target.closest('.theme-switcher'))themeMenuOpen.value=false;}
function onHash(){
  themeMenuOpen.value=false;window.__AVD_READY=false;document.body.dataset.appReady='false';hash.value=location.hash;sideOpen.value=false;document.querySelector('#notice').hidden=true;
  if(route.value.key==='projects'&&route.value.params[0])setProject(route.value.params[0]);
  document.querySelectorAll('dialog:not(.studio-dialog)').forEach(node=>node.remove());
  // Legacy actions can mutate the project list before navigating. Refresh the
  // shell references without increasing epoch or remounting a valid editor.
  void refreshProjects().then(updated=>{if(updated)state.connection='connected';})
    .catch(error=>{state.connection='offline';notify(`项目列表暂未刷新：${error.message}`,'error');});
}
provide('studio',{state,route,theme,currentTheme,selectTheme,navigate,notify,ready,openProject,refreshPage,refreshProjects});
onMounted(async()=>{
  window.addEventListener('hashchange',onHash);document.addEventListener('keydown',onKey);document.addEventListener('click',onDocumentClick);
  try{await refreshProjects();state.connection='connected';}catch(error){state.connection='offline';notify(error.message,'error');}
  initialized.value=true;
});
onBeforeUnmount(()=>{projectRefreshId++;window.removeEventListener('hashchange',onHash);document.removeEventListener('keydown',onKey);document.removeEventListener('click',onDocumentClick);clearTimeout(notificationTimer);});
watch([initialized,route],()=>{if(initialized.value && route.value.key==='projects' && !state.projectId) void ready();});
watch(pageTitle,title=>document.title=`${title} · AVD Studio`,{immediate:true});
document.addEventListener('loadstart',event=>{const element=event.target;if(element instanceof HTMLMediaElement&&!element.dataset.volumeSet){element.volume=.1;element.dataset.volumeSet='1';}},true);
</script>

<template>
  <div class="studio-shell" :class="{'menu-open':sideOpen}">
    <button v-if="sideOpen" class="menu-backdrop" aria-label="关闭导航" @click="sideOpen=false"/>
    <aside class="studio-sidebar" :class="{'is-open':sideOpen}">
      <a class="studio-brand" href="#/" aria-label="AVD 创作空间"><span class="brand-symbol"><StudioIcon name="sparkles" :size="26"/></span><span>AVD<span class="brand-suffix">STUDIO</span><small>想象，现在开场。</small></span></a>
      <button class="studio-button primary sidebar-create" @click="openProject()"><StudioIcon name="plus" :size="18"/>新建作品<kbd>＋</kbd></button>
      <div class="nav-caption">YOUR WORKSPACE</div>
      <nav class="studio-nav" aria-label="主导航">
        <a v-for="item in navigation" :key="item.key" :href="item.key==='projects'&&state.projectId?`#/projects/${state.projectId}/chat`:item.href" class="nav-item" :class="{'is-active':route.key===item.key}" :aria-current="route.key===item.key?'page':undefined" @click="sideOpen=false"><StudioIcon :name="item.icon" :size="19"/><span>{{item.title}}</span><small v-if="item.tag">{{item.tag}}</small></a>
      </nav>
      <div class="sidebar-foot"><div class="sidebar-person"><span class="avatar-chip">C</span><div><strong>Creator workspace</strong><small>你的创意，保存在本机</small></div></div><span class="local-state"><i :class="state.connection"/>{{state.connection==='connected'?'本地服务已连接':state.connection==='offline'?'服务暂未连接':'正在连接工作室'}}</span></div>
    </aside>
    <div class="studio-body">
      <header class="studio-topbar">
        <div class="topbar-left"><button id="menu-toggle" class="mobile-menu" aria-label="打开导航" :aria-expanded="sideOpen" @click="sideOpen=!sideOpen"><StudioIcon name="menu"/></button><div class="breadcrumbs"><span>工作室</span><span>/</span><strong>{{pageTitle}}</strong></div></div>
        <div class="topbar-right"><button class="command-trigger" aria-label="搜索页面与项目" @click="openCommand"><StudioIcon name="search" :size="17"/><span>搜索工作室</span><kbd>⌘ K</kbd></button><select class="topbar-project" aria-label="当前项目" :value="state.projectId" @change="switchProject"><option value="">选择作品</option><option v-for="project in state.projects" :key="project.id" :value="project.id">{{project.title}}</option></select><div class="theme-switcher"><button class="theme-trigger" aria-label="切换外观模板" aria-haspopup="menu" :aria-expanded="themeMenuOpen" @click="themeMenuOpen=!themeMenuOpen"><StudioIcon name="palette" :size="18"/><span>{{currentTheme?.name}}</span></button><div v-if="themeMenuOpen" class="theme-popover" role="menu" aria-label="外观模板"><button v-for="item in THEMES" :key="item.id" role="menuitemradio" :aria-checked="theme===item.id" :data-theme-option="item.id" @click="selectTheme(item.id)"><span class="theme-option-swatch" :style="{background:item.swatches[0],borderColor:item.swatches[2]}"/><span>{{item.name}}<small>{{item.subtitle}}</small></span><StudioIcon v-if="theme===item.id" name="check" :size="16"/></button><button class="theme-lab-link" role="menuitem" @click="themeMenuOpen=false;navigate('#/appearance')">打开外观实验室<StudioIcon name="arrow" :size="16"/></button></div></div><span class="avatar-chip topbar-avatar">C</span></div>
      </header>
      <main id="content" class="studio-main" aria-live="polite">
        <div v-if="!initialized" class="empty-state"><span class="loading-orbit"/><h2>正在准备你的工作室</h2><p>载入项目与创作空间…</p></div>
        <div v-else-if="route.key==='projects'&&!state.projectId" class="empty-state"><StudioIcon name="film" :size="40"/><h2>给你的下一部作品一个名字</h2><p>从参考片开始，也可以先和导演聊一个想法。</p><button class="studio-button primary" @click="openProject()">创建第一部作品</button></div>
        <component v-else :is="currentView" :key="`${route.path}:${route.key==='projects'?state.projectId:''}:${epoch}`" :page="route.key"/>
      </main>
      <footer class="studio-footer"><span>AVD Studio · 每一个版本，都是创作的一部分。</span><a href="#/versions">版本与更新 <StudioIcon name="arrow" :size="14"/></a></footer>
    </div>
    <div v-if="state.notification" class="studio-toast" :class="state.notification.type" role="status"><StudioIcon :name="state.notification.type==='error'?'activity':'check'" :size="18"/><span>{{state.notification.text}}</span><button aria-label="关闭提示" @click="state.notification=null"><StudioIcon name="close" :size="16"/></button></div>
    <dialog ref="createDialog" class="studio-dialog" aria-label="新建作品"><form @submit.prevent="createProject"><header class="dialog-top"><div><span class="hero-kicker">START SOMETHING</span><h2>新作品，从这里开始。</h2></div><button type="button" aria-label="关闭" :disabled="creating" @click="createDialog.close()"><StudioIcon name="close"/></button></header><label class="control-label">作品名称<input v-model="draft.title" class="studio-input" maxlength="255" placeholder="例如：午夜舞台 · 我的第一支唱跳 MV"/></label><label class="control-label">创作方式<select v-model="draft.mode" class="studio-input"><option value="remake">自由创作 / 参考风格</option><option value="anime_sequel">动漫续作</option></select></label><label class="control-label">参考视频 <small>可选；上传后开始结构分析</small><input class="studio-input" type="file" :key="creationNonce" accept=".mp4,.mov,.webm,.mkv,.avi" @change="draft.file=$event.target.files[0] || null"/></label><p class="studio-note">只提取参考片的结构与风格。人物和声音在素材空间中单独授权。</p><p v-if="creationError" class="error-text" role="alert">{{creationError}}</p><button class="studio-button primary" type="submit" :disabled="creating">{{creating?'正在准备…':draft.file?'创建并分析参考片':'进入创作工作台'}}<StudioIcon name="arrow" :size="18"/></button></form></dialog>
    <dialog ref="commandDialog" class="studio-dialog command-dialog" aria-label="搜索工作室"><header class="dialog-top"><StudioIcon name="search"/><input v-model="commandQuery" class="studio-input" placeholder="搜索项目、页面或设置…" autofocus aria-label="搜索关键词"/><button aria-label="关闭搜索" @click="commandDialog.close()"><StudioIcon name="close" :size="18"/></button></header><div class="command-list"><button v-for="command in commands" :key="command.id" class="command-row" @click="goCommand(command)"><StudioIcon :name="command.icon"/><span><strong>{{command.title}}</strong><small>{{command.caption}}</small></span><StudioIcon name="arrow" :size="16"/></button><p v-if="!commands.length" class="studio-note">没有找到匹配项，试试另一个名字。</p></div></dialog>
  </div>
</template>
