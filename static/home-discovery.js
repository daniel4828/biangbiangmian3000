// Homepage reading suggestions (#1212). Existing knowledge/player flows own clicks.
let _homeDiscoveryTimer = null;
let _homeDiscoveryRequest = 0;

async function initHomeDiscovery() {
  clearTimeout(_homeDiscoveryTimer);
  const root = document.getElementById('home-discovery');
  if (!root) return;
  const request = ++_homeDiscoveryRequest;
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
    const data = await api('GET', '/api/home-discovery?tz=' + encodeURIComponent(tz));
    if (request !== _homeDiscoveryRequest || root !== document.getElementById('home-discovery')) return;
    const coffee = data.podcast;
    root.innerHTML = `<div class="home-coffee">
      <div class="home-discovery-heading"><strong>☕ 声动早咖啡</strong><span>${_escHtml(data.date)}</span></div>
      ${coffee ? `<a class="home-coffee-link" href="#knowledge-${coffee.id}" onclick="${coffee.can_listen ? 'openHomePodcast' : 'openKnowledgeItem'}(${coffee.id});return false;">${_escHtml(coffee.title || '声动早咖啡')}<span>${coffee.can_listen ? 'Listen &amp; read' : 'Listen &amp; read is not ready yet · View details'}</span></a>`
        : '<p class="home-discovery-empty">No episode for today yet.</p>'}
      </div>` + (data.recommendations_visible ? `
      <div class="home-discovery-heading home-picks-heading"><strong>From your knowledge base</strong><span>New picks every 2 hours</span></div>
      ${data.recommendations.length ? `<div class="home-picks">${data.recommendations.map(item => `
        <a class="home-pick" href="#knowledge-${item.id}" onclick="openKnowledgeItem(${item.id});return false;">
          <span class="home-pick-kind">${_escHtml(item.kind)}</span><span>${_escHtml(item.title || 'Untitled')}</span>
        </a>`).join('')}</div>` : '<p class="home-discovery-empty">No reading material available yet.</p>'}` : '');
  } catch (error) {
    if (request !== _homeDiscoveryRequest || root !== document.getElementById('home-discovery')) return;
    root.innerHTML = '<p class="home-discovery-empty">Could not load today’s picks. <button class="btn-secondary" onclick="initHomeDiscovery()">Retry</button></p>';
    console.warn('home discovery failed', error);
  } finally {
    if (request === _homeDiscoveryRequest) {
      // Minute boundary also discovers newly ingested coffee episodes. Skips
      // hidden tabs; visibility/focus events catch up after sleep or travel.
      _homeDiscoveryTimer = setTimeout(_refreshHomeDiscovery, 60000 - Date.now() % 60000);
    }
  }
}

function _refreshHomeDiscovery() {
  if (document.visibilityState === 'visible' && _currentView === 'decks') initHomeDiscovery();
}

async function openHomePodcast(id) {
  await openKnowledgeItem(id, 'fulltext');
  if (_currentView === 'knowledge' && _knowledgeDetailId === id && _knowledgeDetailEpisode?.id === id) {
    await doStartListen(id);
  }
}

document.addEventListener('visibilitychange', _refreshHomeDiscovery);
window.addEventListener('focus', _refreshHomeDiscovery);
