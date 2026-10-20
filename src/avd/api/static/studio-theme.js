/* Set the persisted theme before paint; no server or credential state is stored here. */
(() => {
  let theme = 'midnight';
  try {
    const saved = localStorage.getItem('avd.studio.theme');
    if (['midnight', 'glass', 'acid', 'editorial'].includes(saved)) theme = saved;
  } catch { /* Browser storage is optional. */ }
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme === 'midnight' ? 'dark' : 'light';
})();
