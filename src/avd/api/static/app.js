import {api} from './api.js';
import {h, button, select, badge, money, notice, icon, empty, form, field, input, dialog, pageHeader, media, link} from './components.js';
import {t} from './i18n/zh.js';
import {render as projectPage} from './pages/project.js';
import {render as assetsPage} from './pages/assets.js';
import {render as jobsPage} from './pages/jobs.js';
import {render as costsPage} from './pages/costs.js';
import {render as settingsPage} from './pages/settings.js';
import {render as systemPage} from './pages/system.js';

const state = {projects: [], projectId: '', controller: null, cleanup: [], serial: 0};
try { state.projectId = localStorage.getItem('avd.project') || ''; } catch { /* local storage is optional */ }
const content = document.querySelector('#content');
const nav = document.querySelector('#navigation');
const NAV = [['dashboard','dashboard'],['project','project'],['assets','assets'],['jobs','jobs'],['costs','costs'],['settings','settings'],['system','system']];

function navigate(hash) {
  const destination = hash.startsWith('#') ? hash : `#/${hash.replace(/^\//,'')}`;
  if (location.hash === destination) return renderPage();
  location.hash = destination;
}

function saveProject(id) {
  state.projectId = id;
  try { localStorage.setItem('avd.project', id); } catch { /* optional */ }
}

function renderNavigation(active) {
  nav.replaceChildren(...NAV.map(([key, glyph]) => {
    const href = key === 'dashboard' ? '#/' : key === 'project' ? (state.projectId ? `#/projects/${state.projectId}/overview` : '#/projects') : `#/${key}`;
    return h('a', {class: `nav-link ${active === key ? 'active' : ''}`, href, ...(active === key ? {'aria-current': 'page'} : {})}, icon(glyph), t(`nav.${key}`));
  }));
}

function updateTopbar() {
  const switcher = select([{value:'',label:t('shell.selectProject')}, ...state.projects.map(project => ({value: project.id,label:project.title}))], state.projectId, {'aria-label': t('shell.selectProject')});
  switcher.addEventListener('change', () => {
    saveProject(switcher.value);
    if (switcher.value && location.hash.startsWith('#/projects')) navigate(`#/projects/${switcher.value}/overview`);
    else void renderPage();
  });
  document.querySelector('#project-switcher').replaceChildren(switcher);
  const selected = state.projects.find(project => project.id === state.projectId);
  document.querySelector('#top-budget').textContent = selected ? t('shell.budget', {spent: money(selected.spent), limit:money(selected.budget_limit)}) : t('shell.noProject');
}

function newProject(ctx) {
  let modal;
  const create = form([
    field(t('dashboard.projectName'), input('text','',{name:'title',required:true,maxLength:255,placeholder:t('dashboard.namePlaceholder')})),
    field(t('dashboard.budget'), input('number','0',{name:'budget',min:0,step:.01,required:true}),t('dashboard.budgetHelp'))
  ], async data => {
    const result = await api('/projects',{method:'POST',body:{title:String(data.get('title')).trim(),budget_limit:Number(data.get('budget'))}});
    saveProject(result.id); modal.close(); notice(t('dashboard.created')); navigate(`#/projects/${result.id}/overview`);
  },t('dashboard.create'));
  modal = dialog(t('dashboard.new'),create);
  void api('/settings',{signal:ctx.signal}).then(settings => {
    if (settings.settings.default_budget_limit != null) create.elements.budget.value = settings.settings.default_budget_limit;
  }).catch(() => {});
}

async function dashboard(ctx) {
  const data = await api('/dashboard',{signal:ctx.signal});
  const projects = data.projects;
  const totalSpend = projects.reduce((sum, project) => sum + project.spent,0);
  const completed = projects.filter(project => project.status === 'clean' || project.status === 'succeeded').length;
  const pending = projects.filter(project => project.status === 'running').length;
  const stat = (label,value,note) => h('div',{class:'stat'},h('span',{class:'muted'},label),h('strong',{},value),h('small',{class:'muted'},note));
  const root = h('div',{class:'page dashboard-page'},
    pageHeader(t('dashboard.title'),t('dashboard.subtitle'),button([icon('plus',16),t('dashboard.new')],()=>newProject(ctx),{primary:true})),
    h('div',{class:'stats-grid'},stat(t('dashboard.projects'),projects.length,t('dashboard.savedLocally')),stat(t('dashboard.active'),pending,t('dashboard.queueContinues')),stat(t('dashboard.complete'),completed,t('dashboard.versioned')),stat(t('dashboard.spend'),money(totalSpend),t('dashboard.estimated')))
  );
  const cards = projects.map(project => h('article',{class:'card project-card'},
    h('a',{href:`#/projects/${project.id}/overview`,'aria-label':project.title},project.thumbnail_path ? media(project.thumbnail_path,project.thumbnail_kind,{...(project.thumbnail_kind==='video'?{controls:false,muted:true}:{}),alt:project.title}) : h('div',{class:'media-empty'},icon('film',32),t('dashboard.noThumbnail'))),
    h('div',{class:'row spread'},h('h3',{},link(project.title,`#/projects/${project.id}/overview`)),badge(project.status)),
    h('div',{class:'project-card-meta'},h('span',{},t('dashboard.spent',{value:money(project.spent)})),h('span',{},t('dashboard.limit',{value:money(project.budget_limit)}))),
    h('a',{class:'row spread',href:`#/projects/${project.id}/overview`},t('dashboard.open'),icon('arrow',16))
  ));
  root.append(h('section',{class:'stack'},h('div',{class:'section-heading'},h('h2',{},t('dashboard.recent')),h('small',{class:'muted'},t('dashboard.projectCount',{n:projects.length}))),cards.length?h('div',{class:'project-grid'},cards):empty(t('dashboard.empty'),t('dashboard.emptyHelp'),button(t('dashboard.new'),()=>newProject(ctx),{primary:true}))));
  const workerValue = data.system.workers?.workers || [];
  const workers = Array.isArray(workerValue) ? workerValue : Object.values(workerValue);
  const runningWorkers = workers.filter(worker => worker.state === 'running').length;
  const configured = Object.values(data.system.providers).filter(Boolean).length;
  const service = (title,note,status,label,href,action) => h('div',{class:'service-row'},h('div',{},h('h3',{},title),h('small',{},note)),h('div',{class:'toolbar'},badge(status,label),link(action,href)));
  root.append(h('section',{class:'card'},h('div',{class:'section-heading'},h('h2',{},t('dashboard.services')),link(t('dashboard.system'), '#/system')),
    h('div',{class:'service-list'},
      service(t('dashboard.providers'),t('dashboard.providerNote',{n:configured}),configured===3?'ok':'unknown',t(configured===3?'status.ok':'status.unconfigured'),'#/settings',t('common.configure')),
      service(t('common.comfyui'),data.system.comfyui.profile,data.system.comfyui.status,t(`status.${data.system.comfyui.status}`),'#/settings',t('dashboard.testConnection')),
      service(t('dashboard.workers'),t('dashboard.workerNote',{n:runningWorkers}),'unknown',t('dashboard.runningCount',{n:runningWorkers}),'#/system',t('common.manage'))
    )));
  return root;
}

async function renderPage() {
  const serial = ++state.serial;
  state.controller?.abort();
  state.cleanup.splice(0).forEach(cleanup=>cleanup());
  document.querySelectorAll('dialog').forEach(element=>element.remove());
  const controller = new AbortController(); state.controller = controller;
  window.__AVD_READY = false;
  document.body.classList.remove('menu-open');
  document.querySelector('#notice').hidden = true;
  document.querySelector('#menu-toggle').setAttribute('aria-expanded','false');
  content.replaceChildren(h('div',{class:'loading-view',role:'status'},t('common.loading')));
  const hashURL = new URL(location.hash.slice(1) || '/', location.origin);
  const parts = hashURL.pathname.split('/').filter(Boolean).map(decodeURIComponent);
  const key = parts[0] || 'dashboard';
  try {
    state.projects = await api('/projects',{signal:controller.signal});
    if (key === 'projects' && parts[1]) saveProject(parts[1]);
    if (state.projectId && !state.projects.some(project=>project.id===state.projectId)) saveProject('');
    if (!state.projectId && state.projects.length) saveProject(state.projects[0].id);
    updateTopbar(); renderNavigation(key === 'projects' ? 'project' : key);
    const ctx = {
      projectId:state.projectId, projects:state.projects, params:parts.slice(1), query:hashURL.searchParams, signal:controller.signal,
      reload:renderPage, navigate,
      onDispose(cleanup){ state.cleanup.push(cleanup); },
      poll(callback, interval=4000){
        let busy=false;
        const timer=setInterval(async()=>{ if(busy || controller.signal.aborted || document.hidden)return; busy=true;try{await callback();}catch(error){if(error.name!=='AbortError')notice(error.message,true);}finally{busy=false;} },interval);
        state.cleanup.push(()=>clearInterval(timer)); return timer;
      }
    };
    let view;
    if (key === 'projects' && !parts[1]) {
      if (state.projectId) { navigate(`#/projects/${state.projectId}/overview`); return; }
      view=h('div',{class:'page'},pageHeader(t('nav.project'),t('dashboard.subtitle')),empty(t('dashboard.empty'),t('dashboard.emptyHelp'),button(t('dashboard.new'),()=>newProject(ctx),{primary:true})));
    } else {
      const page={dashboard,projects:projectPage,assets:assetsPage,jobs:jobsPage,costs:costsPage,settings:settingsPage,system:systemPage}[key];
      view=page ? await page(ctx) : empty(t('error.notFound'),t('error.notFoundHelp'),link(t('nav.dashboard'),'#/'));
    }
    if(serial!==state.serial)return;
    content.replaceChildren(view); content.setAttribute('aria-busy','false');
    document.querySelector('#connection').replaceChildren(badge('ok',t('shell.connected')));
    document.title=`${t(`nav.${key==='projects'?'project':key}`)} · ${t('shell.name')}`;
    window.__AVD_READY = true;
  } catch(error) {
    if(error.name==='AbortError'||serial!==state.serial)return;
    content.replaceChildren(empty(t('error.loadFailed'),error.message,button(t('common.retry'),renderPage)));
    document.querySelector('#connection').replaceChildren(badge('failed',t('shell.disconnected')));
    window.__AVD_READY=true;
  }
}

document.querySelector('#brand').replaceChildren(icon('logo',32),h('span',{},t('shell.name'),h('small',{},t('shell.tagline'))));
document.querySelector('#sidebar-note').textContent=t('shell.localNote');
document.querySelector('#menu-toggle').append(icon('menu'));
document.querySelector('#menu-toggle').setAttribute('aria-label',t('shell.menu'));
document.querySelector('#menu-toggle').addEventListener('click',()=>{const opened=document.body.classList.toggle('menu-open');document.querySelector('#menu-toggle').setAttribute('aria-expanded',String(opened));});
document.querySelector('#refresh').replaceChildren(icon('refresh',18));
document.querySelector('#refresh').setAttribute('aria-label',t('common.refresh'));
document.querySelector('#refresh').addEventListener('click',()=>void renderPage());
document.addEventListener('keydown',event=>{if(event.key==='Escape')document.body.classList.remove('menu-open');});
window.addEventListener('hashchange',()=>void renderPage());
void renderPage();
