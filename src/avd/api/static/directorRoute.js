export function effectiveDirectorRoute(settings) {
  const override = settings.llm_routes?.director;
  if (override) return {provider: override.provider, model: override.model || ''};
  const provider = settings.director_provider || 'anthropic';
  const field = {gemini: 'gemini_model', deepseek: 'deepseek_model', grok: 'grok_model'}[provider] || 'director_model';
  return {provider, model: settings[field] || ''};
}

export function directorRouteChanges(settings, provider, model) {
  const selected = String(model || '').trim();
  if (!selected) throw new Error('请选择或输入这个服务实际可用的导演模型。');
  return {
    director_provider: provider,
    director_model: selected,
    llm_routes: {...settings.llm_routes, director: {provider, model: selected}},
  };
}

export function mergeRouteDraft(draft, baseline, current) {
  const result = {...draft};
  for (const role of new Set([...Object.keys(baseline || {}), ...Object.keys(current || {})])) {
    if (JSON.stringify(draft[role]) === JSON.stringify(baseline?.[role])) {
      if (Object.hasOwn(current || {}, role)) result[role] = current[role];
      else delete result[role];
    }
  }
  return result;
}
