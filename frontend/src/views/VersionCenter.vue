<script setup>
import {computed, onBeforeUnmount, onMounted, ref} from 'vue';
import {api, formatCount} from '../api.js';
import Icon from '../components/StudioIcon.vue';
import {useStudio} from '../context.js';

const ctx = useStudio();
const repositoryURL = 'https://github.com/chixiaotao-Exm/ai-video-director';
const info = ref(null);
const loading = ref(true);
const error = ref('');
const refreshedAt = ref(null);
const syncLabels = {current: '本地记录一致', ahead: '本地提交领先', behind: '本地记录落后', diverged: '本地记录已分叉', no_upstream: '未设置跟踪分支', no_commits: '尚无提交', unavailable: '比较暂不可用'};
const syncLabel = computed(() => syncLabels[info.value?.sync?.status] || '比较暂不可用');
const changes = computed(() => info.value?.changes || {});
const changeCount = computed(() => Number(changes.value.tracked || 0) + Number(changes.value.untracked || 0));
const commits = computed(() => info.value?.recent_commits || []);
const tags = computed(() => info.value?.tags || []);
let controller;
let active = true;

function dateLabel(value, includeTime = false) {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return '时间未提供';
  return new Intl.DateTimeFormat('zh-CN', {year: 'numeric', month: '2-digit', day: '2-digit',
    ...(includeTime ? {hour: '2-digit', minute: '2-digit', second: '2-digit'} : {})}).format(date);
}

function commitURL(hash) {
  return info.value?.remote_matches_repository && /^[0-9a-f]{40,64}$/i.test(hash || '')
    ? `${repositoryURL}/commit/${hash}` : undefined;
}

async function refresh() {
  if (controller) controller.abort();
  controller = new AbortController();
  loading.value = true;
  error.value = '';
  try {
    info.value = await api('/version-control', {signal: controller.signal});
    refreshedAt.value = new Date().toISOString();
  } catch (cause) {
    if (active && cause.name !== 'AbortError') {
      error.value = cause.message || '暂时无法读取本地版本信息。';
      ctx.notify(error.value, 'error');
    }
  } finally {
    if (active) { loading.value = false; ctx.ready(); }
  }
}

onMounted(refresh);
onBeforeUnmount(() => { active = false; controller?.abort(); });
</script>

<template>
  <div class="version-page">
    <div class="page-heading"><div><p class="eyebrow">VERSION CONTROL</p><h1>每一步，都有迹可循。</h1><p>在本地查看软件版本、分支状态和最近的改动。</p></div><div class="version-actions"><button class="studio-button" type="button" :disabled="loading" @click="refresh"><Icon name="refresh" :class="{refreshing: loading}"/> {{ loading ? '读取中…' : '刷新本地状态' }}</button><a class="studio-button primary" :href="repositoryURL" target="_blank" rel="noopener noreferrer"><Icon name="git"/> 打开 GitHub <Icon name="external"/></a></div></div>

    <div v-if="error" class="studio-card version-error" role="alert"><strong>{{ info ? '刷新未完成，以下保留上次本地快照。' : '版本信息暂时不可用。' }}</strong><p>{{ error }}</p><button v-if="!info" class="studio-button" type="button" :disabled="loading" @click="refresh">重新读取</button></div>
    <div v-if="loading && !info" class="studio-loading" role="status">正在读取本地版本记录…</div>

    <template v-if="info">
      <section class="studio-card repository-banner" aria-labelledby="repository-title">
        <div class="repository-mark"><Icon name="git" :size="30"/></div>
        <div class="repository-copy"><p class="eyebrow">YOUR SOFTWARE REPOSITORY</p><h2 id="repository-title">ai-video-director</h2><p>chixiaotao-Exm / AI Video Director</p><div class="version-meta"><span class="studio-badge">{{ info.available ? '本地仓库已读取' : '安装版本' }}</span><span v-if="info.package_version" class="studio-badge">应用 {{ info.package_version }}</span><span class="studio-badge">只读视图</span><span v-if="info.current_tag" class="studio-badge ready">{{ info.current_tag }}</span></div></div>
        <div class="repository-head"><small>当前提交</small><code>{{ info.short_head || '尚无可用提交' }}</code><span v-if="refreshedAt">读取于 {{ dateLabel(refreshedAt, true) }}</span></div>
      </section>

      <section v-if="!info.available" class="studio-card empty-state" aria-labelledby="unavailable-title"><Icon name="layers" :size="30"/><h2 id="unavailable-title">此安装未提供可读取的 Git 记录</h2><p>{{ info.reason || 'Git 未安装、仓库未附带或本地查询暂不可用。' }}</p><p>仍可正常使用创作功能。项目素材的版本历史请在制作项目内查看。</p><a class="studio-button" :href="repositoryURL" target="_blank" rel="noopener noreferrer">到 GitHub 查看源码 <Icon name="external"/></a></section>

      <template v-else>
        <div class="metric-grid version-metrics">
          <article class="metric-card"><Icon name="git"/><span>当前分支</span><strong class="branch-value">{{ info.branch || (info.head ? '游离 HEAD' : '尚未建立') }}</strong><p>{{ info.sync?.upstream ? `跟踪 ${info.sync.upstream}` : '暂无可用跟踪分支' }}</p></article>
          <article class="metric-card"><Icon name="layers"/><span>工作区变更</span><strong>{{ info.changes_available ? formatCount(changeCount) : '—' }}</strong><p>{{ !info.changes_available ? '变更统计暂不可用' : info.dirty ? '有尚未提交的本地文件' : '工作区干净' }}</p></article>
          <article class="metric-card"><Icon name="arrow"/><span>领先 / 落后</span><strong>{{ info.sync?.ahead == null ? '—' : formatCount(info.sync.ahead) }} <span class="metric-divider">/</span> {{ info.sync?.behind == null ? '—' : formatCount(info.sync.behind) }}</strong><p>{{ syncLabel }} · 依据本地引用</p></article>
          <article class="metric-card"><Icon name="check"/><span>本地版本标签</span><strong>{{ formatCount(tags.length) }}</strong><p>{{ tags.length ? '最多展示最近 30 个' : '尚未读取到标签' }}</p></article>
        </div>

        <div class="version-details">
          <section class="studio-card" aria-labelledby="working-title"><div class="section-heading"><h2 id="working-title">工作区状态</h2><span class="studio-badge" :class="{ready: info.changes_available && !info.dirty}">{{ !info.changes_available ? '暂不可用' : info.dirty ? '有待记录的改动' : '已全部记录' }}</span></div><dl class="changes-list"><div><dt>已暂存</dt><dd>{{ info.changes_available ? formatCount(changes.staged) : '—' }}</dd></div><div><dt>未暂存</dt><dd>{{ info.changes_available ? formatCount(changes.unstaged) : '—' }}</dd></div><div><dt>未跟踪</dt><dd>{{ info.changes_available ? formatCount(changes.untracked) : '—' }}</dd></div><div><dt>冲突</dt><dd :class="{conflict: changes.conflicted}">{{ info.changes_available ? formatCount(changes.conflicted) : '—' }}</dd></div></dl><p class="studio-note">同一文件可能同时有已暂存和未暂存修改，因此这些计数不一定相加等于变更文件数。这里不展示素材文件名。</p></section>
          <section class="studio-card" aria-labelledby="sync-title"><div class="section-heading"><h2 id="sync-title">本地与远端记录</h2><span class="studio-badge">本地引用</span></div><strong class="sync-state">{{ syncLabel }}</strong><p class="sync-description">领先和落后来自本地保存的跟踪分支；刷新只重新读取本机，不向 GitHub 拉取最新状态。</p><dl class="remote-list"><div><dt>跟踪分支</dt><dd><code>{{ info.sync?.upstream || '尚未配置' }}</code></dd></div><div><dt>仓库对应</dt><dd>{{ info.remote_matches_repository ? 'origin 指向此 GitHub 仓库' : info.configured_remote ? 'origin 指向其他仓库' : '没有可用的 origin 地址' }}</dd></div></dl><p class="studio-note">此页面不会提交、切换分支、拉取、推送或自动安装 Git。需要在线状态时，打开 GitHub 查看。</p></section>
        </div>

        <section class="studio-card history-card" aria-labelledby="history-title"><div class="section-heading"><div><p class="eyebrow">COMMIT HISTORY</p><h2 id="history-title">最近的制作工具迭代</h2></div><span class="studio-badge">{{ commits.length }} 条本地提交</span></div><ol v-if="commits.length" class="version-timeline"><li v-for="commit in commits" :key="commit.hash" class="version-entry"><div class="commit-copy"><h3>{{ commit.subject || '未提供提交说明' }}</h3><div class="commit-meta"><a v-if="commitURL(commit.hash)" :href="commitURL(commit.hash)" target="_blank" rel="noopener noreferrer" :aria-label="`在 GitHub 查看提交 ${commit.short_hash}（需已推送）`"><code>{{ commit.short_hash }}</code><Icon name="external" :size="12"/></a><code v-else>{{ commit.short_hash }}</code><time :datetime="commit.committed_at">{{ dateLabel(commit.committed_at, true) }}</time></div></div></li></ol><p v-else class="studio-note">还没有可读取的提交记录。</p><p v-if="commits.length && info.remote_matches_repository" class="studio-note">提交链接指向指定 GitHub 仓库；只在本地的提交需要推送后才能在线查看。</p></section>

        <section class="studio-card tags-card" aria-labelledby="tags-title"><div class="section-heading"><h2 id="tags-title">版本标签</h2><span class="studio-badge">本地标签</span></div><div v-if="tags.length" class="tag-list"><article v-for="tag in tags" :key="`${tag.name}-${tag.hash}`" class="tag-item"><Icon name="layers" :size="17"/><div><strong>{{ tag.name }}</strong><time :datetime="tag.created_at">{{ dateLabel(tag.created_at) }}</time></div><code>{{ tag.hash.slice(0, 8) }}</code></article></div><p v-else class="studio-note">暂无本地标签。仍可通过提交记录追踪已提交的修改。</p></section>
      </template>
      <p class="version-footer">这是程序代码的版本中心。每个视频节点的生成版本、回退与素材历史，仍在项目工作区管理。</p>
    </template>
  </div>
</template>

<style scoped>
.version-page { min-width: 0; }
.version-actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.repository-banner { display: flex; align-items: center; flex-wrap: wrap; gap: 22px; margin-bottom: 24px; background: radial-gradient(ellipse at 0 0, var(--hero-glow), transparent 65%), var(--surface); }
.repository-mark { display: grid; place-items: center; width: 67px; height: 67px; border-radius: 19px; background: var(--accent-soft); color: var(--accent-b); flex-shrink: 0; }
.repository-copy { flex: 1; min-width: 180px; }
.repository-copy h2 { font-size: 24px; letter-spacing: -.04em; margin: 0 0 5px; }
.repository-copy > p:not(.eyebrow) { font-size: 12px; color: var(--muted); margin: 0; overflow-wrap: anywhere; }
.repository-copy .version-meta { margin-bottom: 0; }
.repository-head { display: flex; flex-direction: column; gap: 8px; text-align: right; }
.repository-head small, .repository-head > span { color: var(--muted); font-size: 10px; }
.repository-head code { font-size: 22px; color: var(--accent-b); }
.version-metrics .branch-value { font-size: 20px; overflow-wrap: anywhere; }
.metric-divider { color: var(--subtle); font-weight: 400; font-size: 22px; }
.section-heading { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 22px; }
.section-heading h2 { font-size: 16px; margin: 0; }
.changes-list { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin: 0; gap: 10px; }
.changes-list dt { font-size: 11px; color: var(--muted); }
.changes-list dd { margin: 5px 0 0; font-size: 23px; font-weight: 600; }
.changes-list .conflict { color: var(--danger); }
.studio-note { font-size: 11px; color: var(--muted); line-height: 1.85; margin: 18px 0 0; }
.sync-state { font-size: 18px; font-weight: 600; }
.sync-description { font-size: 12px; color: var(--muted); line-height: 1.9; }
.remote-list { margin: 20px 0 0; display: grid; gap: 12px; font-size: 11px; }
.remote-list > div { display: grid; grid-template-columns: 75px minmax(0, 1fr); gap: 12px; }
.remote-list dt { color: var(--muted); }
.remote-list dd { margin: 0; overflow-wrap: anywhere; }
.history-card, .tags-card { margin-top: 24px; }
.version-timeline { padding-left: 0; list-style: none; }
.commit-copy { width: 100%; }
.commit-copy h3 { font-size: 13px; font-weight: 500; line-height: 1.8; margin: 0 0 7px; }
.commit-meta { display: flex; align-items: center; gap: 10px 16px; flex-wrap: wrap; }
.commit-meta a { display: inline-flex; align-items: center; color: var(--accent-b); text-decoration: none; }
.commit-meta a:hover { text-decoration: underline; text-underline-offset: 3px; }
.commit-meta code { margin-right: 4px; }
.tag-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 13px; }
.tag-item { display: flex; align-items: center; gap: 11px; padding: 15px; background: var(--surface-2); border: 1px solid var(--line); border-radius: var(--radius); min-width: 0; }
.tag-item > svg { color: var(--accent-b); flex-shrink: 0; }
.tag-item > div { display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 1; }
.tag-item strong { font-size: 12px; font-weight: 600; overflow-wrap: anywhere; }
.tag-item time, .tag-item > code { color: var(--muted); font-size: 10px; }
.version-footer { color: var(--muted); font-size: 11px; line-height: 1.8; margin-top: 24px; }
.version-error { margin-bottom: 24px; color: var(--danger); }
.version-error strong { font-size: 13px; }
.version-error p, .empty-state p { font-size: 12px; color: var(--muted); line-height: 1.9; }
.empty-state > svg { color: var(--accent-b); }
.empty-state h2 { font-size: 18px; }
.refreshing { animation: version-refresh 1.2s linear infinite; }
@keyframes version-refresh { to { transform: rotate(360deg); } }
@media (max-width: 1100px) { .tag-list { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 700px) { .repository-head { width: 100%; text-align: left; padding-top: 18px; border-top: 1px solid var(--line); } .tag-list { grid-template-columns: minmax(0, 1fr); } .repository-copy h2 { font-size: 21px; } .changes-list { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; } }
@media (prefers-reduced-motion: reduce) { .refreshing { animation: none; } }
</style>
