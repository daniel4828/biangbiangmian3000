/* #1271：在样式表加载前应用主题，避免深色页面先闪白。 */
(() => {
  const root = document.documentElement;
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  let choice = 'system';
  try {
    const saved = localStorage.getItem('theme');
    if (['system', 'light', 'dark'].includes(saved)) choice = saved;
  } catch (_) { /* 浏览器禁用存储时，当前页面仍然可以切换主题。 */ }
  const apply = () => {
    const resolved = choice === 'system' ? (media.matches ? 'dark' : 'light') : choice;
    root.dataset.theme = resolved;
    root.style.colorScheme = resolved;
  };
  apply();
  const onSystemChange = () => { if (choice === 'system') apply(); };
  if (media.addEventListener) media.addEventListener('change', onSystemChange);
  else if (media.addListener) media.addListener(onSystemChange);
  const bind = () => {
    const selector = document.getElementById('theme-select');
    if (!selector) return;
    selector.value = choice;
    selector.addEventListener('change', () => {
      choice = ['system', 'light', 'dark'].includes(selector.value) ? selector.value : 'system';
      try { localStorage.setItem('theme', choice); } catch (_) { /* 保留当前页面的选择。 */ }
      apply();
    });
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();
