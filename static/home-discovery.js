// Homepage reading suggestions (#1212). Existing knowledge/player flows own clicks.
let _homeDiscoveryTimer = null;
let _homeDiscoveryRequest = 0;
let _homeDiscoveryPodcast = null;
let _homePlayerRequest = 0;

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
    _homeDiscoveryPodcast = coffee;
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
  const request = ++_homePlayerRequest;
  const owner = _raOwnerForEpisodeListen({id,
    title: _homeDiscoveryPodcast?.id === id ? _homeDiscoveryPodcast.title : '声动早咖啡'});
  // Opening the player is navigation, never a play/pause toggle. In-memory
  // progress wins over the last (throttled) server save for this same item.
  if (_raIsActive(owner)) {
    _raOpenFullscreen();
    return;
  }
  const previousPlayer = _raPlayer;
  const previousView = _currentView;
  const isCurrent = () => request === _homePlayerRequest &&
    _currentView === previousView && _raPlayer === previousPlayer;
  try {
    const query = `owner_kind=episode&owner_id=${id}&lang=zh&variant=fulltext`;
    const track = await api('GET', '/api/audio/track?' + query);
    if (!isCurrent()) return;
    if (track.status !== 'ready') {
      // First-time listening still uses the existing preparation/status UI.
      await openKnowledgeItem(id, 'fulltext', () => request === _homePlayerRequest &&
        _raPlayer === previousPlayer && _currentView === 'loading');
      if (request !== _homePlayerRequest) return;
      if (_currentView === 'knowledge' && _knowledgeDetailId === id && _knowledgeDetailEpisode?.id === id) {
        await doStartListen(id);
      }
      return;
    }
    // Unlike the general detail-page lookup, a failed progress read must not
    // silently turn a "continue listening" link into "start from zero".
    const progress = await api('GET', '/api/audio/progress?' + query);
    if (!isCurrent()) return;
    _raTrack = {...track, owner_kind: 'episode', owner_id: id, lang: 'zh', variant: 'fulltext'};
    _raProgress = {...progress, owner_kind: 'episode', owner_id: id, lang: 'zh', variant: 'fulltext'};
    const player = _raSync(owner, _raTrack);
    // Keep even the first few saved seconds; Play consumes this resume point.
    const position = progress.finished ? 0 : Math.max(0, progress.position_ms || 0);
    player.resumeMs = position;
    player.lastMs = position;
    _raOpenFullscreen();
  } catch (error) {
    if (isCurrent()) showError('Could not open player: ' + error.message);
  }
}

document.addEventListener('visibilitychange', _refreshHomeDiscovery);
window.addEventListener('focus', _refreshHomeDiscovery);
