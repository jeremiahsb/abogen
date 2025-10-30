const modal = document.querySelector('[data-role="opds-modal"]');
const browser = modal?.querySelector('[data-role="opds-browser"]') || null;

if (modal && browser) {
  const statusEl = browser.querySelector('[data-role="opds-status"]');
  const resultsEl = browser.querySelector('[data-role="opds-results"]');
  const navEl = browser.querySelector('[data-role="opds-nav"]');
  const tabsEl = modal.querySelector('[data-role="opds-tabs"]');
  const searchForm = modal.querySelector('[data-role="opds-search"]');
  const searchInput = searchForm?.querySelector('input[name="q"]');
  const refreshButton = searchForm?.querySelector('[data-action="opds-refresh"]');
  const openButtons = document.querySelectorAll('[data-action="open-opds-modal"]');
  const closeTargets = modal.querySelectorAll('[data-role="opds-modal-close"]');

  const TabIds = {
    ROOT: 'root',
    SEARCH: 'search',
    CUSTOM: 'custom',
  };

  const EntryTypes = {
    BOOK: 'book',
    NAVIGATION: 'navigation',
    OTHER: 'other',
  };

  const state = {
    query: '',
    currentHref: '',
    activeTab: TabIds.ROOT,
    tabs: [],
    tabsReady: false,
    requestToken: 0,
  };

  let isOpen = false;
  let lastTrigger = null;

  const truncate = (text, limit = 160) => {
    if (!text || typeof text !== 'string') {
      return '';
    }
    if (text.length <= limit) {
      return text;
    }
    return `${text.slice(0, limit - 1).trim()}…`;
  };

  const formatAuthors = (authors) => {
    if (!Array.isArray(authors) || !authors.length) {
      return '';
    }
    return authors.filter((author) => !!author).join(', ');
  };

  const setStatus = (message, level) => {
    if (!statusEl) {
      return;
    }
    statusEl.textContent = message || '';
    if (level) {
      statusEl.dataset.state = level;
    } else {
      delete statusEl.dataset.state;
    }
  };

  const clearStatus = () => setStatus('', null);

  const focusSearch = () => {
    if (!searchInput) {
      return;
    }
    window.requestAnimationFrame(() => {
      try {
        searchInput.focus({ preventScroll: true });
      } catch (error) {
        // Ignore focus issues
      }
    });
  };

  const resolveRelLink = (links, rel) => {
    if (!links) {
      return null;
    }
    if (links[rel]) {
      return links[rel];
    }
    const key = Object.keys(links).find((entry) => entry === rel || entry.endsWith(rel));
    return key ? links[key] : null;
  };

  const findNavigationLink = (entry) => {
    if (!entry || !Array.isArray(entry.links)) {
      return null;
    }
    const candidates = entry.links.filter((link) => link && link.href);
    return (
      candidates.find((link) => {
        const rel = (link.rel || '').toLowerCase();
        const type = (link.type || '').toLowerCase();
        if (!link.href) {
          return false;
        }
        if (rel.includes('acquisition')) {
          return false;
        }
        if (rel === 'self') {
          return false;
        }
        if (type.includes('opds-catalog')) {
          return true;
        }
        if (rel.includes('subsection') || rel.includes('collection')) {
          return true;
        }
        if (rel.startsWith('http://opds-spec.org/sort') || rel.startsWith('http://opds-spec.org/group')) {
          return true;
        }
        return false;
      }) || null
    );
  };

  const resolveTabIdForHref = (href) => {
    if (!href) {
      return TabIds.ROOT;
    }
    const matching = state.tabs.find((tab) => tab.href === href);
    return matching ? matching.id : null;
  };

  const buildTabsFromFeed = (feed) => {
    if (!feed || !Array.isArray(feed.entries)) {
      return;
    }
    const seen = new Set();
    const nextTabs = [];
    feed.entries.forEach((entry) => {
      const navLink = findNavigationLink(entry);
      if (!navLink || !navLink.href) {
        return;
      }
      if (seen.has(navLink.href)) {
        return;
      }
      seen.add(navLink.href);
      const label = entry.title || navLink.title || 'Catalog view';
      nextTabs.push({
        id: navLink.href,
        label,
        href: navLink.href,
      });
    });
    state.tabs = nextTabs;
    state.tabsReady = true;
    renderTabs();
  };

  const renderTabs = () => {
    if (!tabsEl) {
      return;
    }
    tabsEl.innerHTML = '';
    const tabs = [];
    tabs.push({ id: TabIds.ROOT, label: 'Catalog home', href: '' });
    state.tabs.forEach((tab) => tabs.push(tab));
    if (state.activeTab === TabIds.SEARCH && state.query) {
      tabs.push({
        id: TabIds.SEARCH,
        label: `Search: "${truncate(state.query, 32)}"`,
        href: '',
        isSearch: true,
      });
    }
    tabs.forEach((tab) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'opds-tab';
      if (tab.isSearch) {
        button.classList.add('opds-tab--search');
      }
      if (state.activeTab === tab.id || (tab.id !== TabIds.SEARCH && state.activeTab === tab.href)) {
        button.classList.add('is-active');
      }
      button.textContent = tab.label;
      button.addEventListener('click', () => {
        if (tab.id === TabIds.SEARCH) {
          loadFeed({ href: '', query: state.query, activeTab: TabIds.SEARCH });
          return;
        }
        if (tab.id === TabIds.ROOT) {
          loadFeed({ href: '', query: '', activeTab: TabIds.ROOT, updateTabs: true });
          return;
        }
        loadFeed({ href: tab.href, query: '', activeTab: tab.id });
      });
      tabsEl.appendChild(button);
    });
    tabsEl.classList.toggle('is-empty', tabs.length <= 1);
  };

  const renderNav = (links) => {
    if (!navEl) {
      return;
    }
    navEl.innerHTML = '';
    const descriptors = [
      { key: 'up', label: 'Up one level' },
      { key: 'previous', label: 'Previous page' },
      { key: 'next', label: 'Next page' },
    ];
    descriptors.forEach(({ key, label }) => {
      const link = resolveRelLink(links, key) || resolveRelLink(links, `/${key}`);
      if (!link || !link.href) {
        return;
      }
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'button button--ghost';
      button.textContent = label;
      button.addEventListener('click', () => {
        const targetQuery = key === 'up' ? '' : state.query;
        const tabId = resolveTabIdForHref(link.href);
        loadFeed({ href: link.href, query: targetQuery, activeTab: tabId || (targetQuery ? TabIds.SEARCH : TabIds.CUSTOM) });
      });
      navEl.appendChild(button);
    });
    navEl.hidden = !navEl.childElementCount;
  };

  const createEntry = (entry) => {
    const item = document.createElement('li');
    item.className = 'opds-browser__entry';

    const header = document.createElement('div');
    header.className = 'opds-browser__entry-head';

    const title = document.createElement('h3');
    title.className = 'opds-browser__title';
    title.textContent = entry.title || 'Untitled';
    header.appendChild(title);

    const authors = formatAuthors(entry.authors);
    if (authors) {
      const meta = document.createElement('p');
      meta.className = 'opds-browser__meta';
      meta.textContent = authors;
      header.appendChild(meta);
    }

    item.appendChild(header);

    const summarySource = entry.summary || entry?.alternate?.title || entry?.download?.title || '';
    if (summarySource) {
      const summary = document.createElement('p');
      summary.className = 'opds-browser__summary';
      summary.textContent = truncate(summarySource, 420);
      item.appendChild(summary);
    }

    const actions = document.createElement('div');
    actions.className = 'opds-browser__actions';

    const downloadLink = entry.download && entry.download.href ? entry.download.href : null;
    const alternateLink = entry.alternate && entry.alternate.href ? entry.alternate.href : null;
    const navigationLink = findNavigationLink(entry);

    let entryType = EntryTypes.OTHER;
    if (downloadLink) {
      entryType = EntryTypes.BOOK;
    } else if (navigationLink && navigationLink.href) {
      entryType = EntryTypes.NAVIGATION;
      item.classList.add('opds-browser__entry--navigation');
    }

    if (entryType === EntryTypes.BOOK) {
      const queueButton = document.createElement('button');
      queueButton.type = 'button';
      queueButton.className = 'button';
      queueButton.textContent = 'Configure conversion';
      queueButton.addEventListener('click', () => importEntry(entry, queueButton));
      actions.appendChild(queueButton);
    } else if (entryType === EntryTypes.NAVIGATION && navigationLink) {
      const browseButton = document.createElement('button');
      browseButton.type = 'button';
      browseButton.className = 'button button--ghost';
      browseButton.textContent = 'Browse view';
      browseButton.addEventListener('click', () => {
        clearStatus();
        const tabId = resolveTabIdForHref(navigationLink.href);
        loadFeed({ href: navigationLink.href, query: '', activeTab: tabId || TabIds.CUSTOM });
      });
      actions.appendChild(browseButton);
    }

    if (alternateLink && entryType !== EntryTypes.NAVIGATION) {
      const previewLink = document.createElement('a');
      previewLink.className = 'button button--ghost';
      previewLink.href = alternateLink;
      previewLink.target = '_blank';
      previewLink.rel = 'noreferrer';
      previewLink.textContent = 'Open in Calibre';
      actions.appendChild(previewLink);
    }

    if (!actions.childElementCount) {
      const fallback = document.createElement('span');
      fallback.className = 'opds-browser__hint';
      fallback.textContent = 'No downloadable formats exposed.';
      actions.appendChild(fallback);
    }

    item.appendChild(actions);
    return { element: item, type: entryType };
  };

  const renderEntries = (entries) => {
    if (!resultsEl) {
      return { [EntryTypes.BOOK]: 0, [EntryTypes.NAVIGATION]: 0, [EntryTypes.OTHER]: 0 };
    }
    resultsEl.innerHTML = '';
    if (!Array.isArray(entries) || !entries.length) {
      const empty = document.createElement('li');
      empty.className = 'opds-browser__empty';
      empty.textContent = 'No catalog entries found here yet.';
      resultsEl.appendChild(empty);
      return { [EntryTypes.BOOK]: 0, [EntryTypes.NAVIGATION]: 0, [EntryTypes.OTHER]: 0 };
    }
    const fragment = document.createDocumentFragment();
    const stats = {
      [EntryTypes.BOOK]: 0,
      [EntryTypes.NAVIGATION]: 0,
      [EntryTypes.OTHER]: 0,
    };
    entries.forEach((entry) => {
      const { element, type } = createEntry(entry);
      stats[type] += 1;
      fragment.appendChild(element);
    });
    resultsEl.appendChild(fragment);
    return stats;
  };

  const importEntry = async (entry, trigger) => {
    if (!entry?.download?.href) {
      setStatus('This entry cannot be imported automatically.', 'error');
      return;
    }
    const button = trigger;
    const originalLabel = button ? button.textContent : '';
    if (button) {
      button.disabled = true;
      button.dataset.loading = 'true';
      button.textContent = 'Preparing…';
    }
    setStatus('Downloading book from Calibre. This can take a minute…', 'loading');
    try {
      const response = await fetch('/api/integrations/calibre-opds/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ href: entry.download.href, title: entry.title || '' }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || 'Unable to queue this book.');
      }
      setStatus('Preparing the conversion wizard…', 'success');
      closeModal();
      const redirectUrl = payload.redirect_url || '';
      if (redirectUrl) {
        const wizard = window.AbogenWizard;
        if (wizard?.requestStep) {
          try {
            const target = new URL(redirectUrl, window.location.origin);
            target.searchParams.set('format', 'json');
            await wizard.requestStep(target.toString(), { method: 'GET' });
          } catch (wizardError) {
            console.error('Unable to open wizard via JSON payload', wizardError);
            window.location.assign(redirectUrl);
          }
        } else {
          window.location.assign(redirectUrl);
        }
      }
    } catch (error) {
      setStatus(error instanceof Error ? error.message : 'Unable to queue this book.', 'error');
    } finally {
      if (button) {
        button.disabled = false;
        delete button.dataset.loading;
        if (originalLabel) {
          button.textContent = originalLabel;
        }
      }
    }
  };

  const loadFeed = async ({ href = '', query = '', activeTab = null, updateTabs = false } = {}) => {
    const params = new URLSearchParams();
    if (href) {
      params.set('href', href);
    }
    if (query) {
      params.set('q', query);
    }

    const requestId = ++state.requestToken;
    setStatus('Loading catalog…', 'loading');

    try {
      const url = `/api/integrations/calibre-opds/feed${params.toString() ? `?${params.toString()}` : ''}`;
      const response = await fetch(url);
      const payload = await response.json();
      if (requestId !== state.requestToken) {
        return;
      }
      if (!response.ok) {
        throw new Error(payload.error || 'Unable to load the Calibre catalog.');
      }
      const feed = payload.feed || {};
      state.currentHref = href;
      state.query = query;
      if (typeof activeTab === 'string') {
        state.activeTab = activeTab;
      } else if (query) {
        state.activeTab = TabIds.SEARCH;
      } else if (href) {
        state.activeTab = resolveTabIdForHref(href) || TabIds.CUSTOM;
      } else {
        state.activeTab = TabIds.ROOT;
      }

      if (searchInput) {
        searchInput.value = query || '';
      }

      if (updateTabs || !state.tabsReady) {
        buildTabsFromFeed(feed);
      } else {
        renderTabs();
      }

      renderNav(feed.links);
      const stats = renderEntries(feed.entries || []);
      const books = stats?.[EntryTypes.BOOK] || 0;
      const views = stats?.[EntryTypes.NAVIGATION] || 0;

      if (query) {
        if (books) {
          setStatus(`Found ${books} book${books === 1 ? '' : 's'} for "${query}".`, 'success');
        } else if (views) {
          setStatus(`Browse ${views} catalog view${views === 1 ? '' : 's'} related to "${query}".`, 'info');
        } else {
          setStatus(`No results for "${query}".`, 'error');
        }
        return;
      }

      if (books && views) {
        setStatus(`Showing ${books} book${books === 1 ? '' : 's'} and ${views} catalog view${views === 1 ? '' : 's'}.`, 'success');
      } else if (books) {
        setStatus(`Found ${books} book${books === 1 ? '' : 's'} in this view.`, 'success');
      } else if (views) {
        setStatus(`Browse ${views} catalog view${views === 1 ? '' : 's'} to drill deeper.`, 'info');
      } else {
        setStatus('No catalog entries found here yet.', 'info');
      }
    } catch (error) {
      if (requestId !== state.requestToken) {
        return;
      }
      setStatus(error instanceof Error ? error.message : 'Unable to load the Calibre catalog.', 'error');
      renderEntries([]);
      if (navEl) {
        navEl.innerHTML = '';
      }
    }
  };

  const openModal = (trigger) => {
    if (isOpen) {
      focusSearch();
      return;
    }
    isOpen = true;
    lastTrigger = trigger || null;
    modal.hidden = false;
    modal.dataset.open = 'true';
    document.body.classList.add('modal-open');
    focusSearch();
    loadFeed({ href: state.currentHref || '', query: state.query || '', activeTab: state.activeTab || TabIds.ROOT, updateTabs: !state.tabsReady });
  };

  const closeModal = () => {
    if (!isOpen) {
      return;
    }
    isOpen = false;
    modal.hidden = true;
    delete modal.dataset.open;
    document.body.classList.remove('modal-open');
    if (lastTrigger instanceof HTMLElement) {
      lastTrigger.focus({ preventScroll: true });
    }
  };

  const handleKeydown = (event) => {
    if (event.key === 'Escape' && isOpen) {
      event.preventDefault();
      closeModal();
    }
  };

  document.addEventListener('keydown', handleKeydown);

  openButtons.forEach((button) => {
    button.addEventListener('click', (event) => {
      event.preventDefault();
      openModal(button);
    });
  });

  closeTargets.forEach((target) => {
    target.addEventListener('click', (event) => {
      event.preventDefault();
      closeModal();
    });
  });

  modal.addEventListener('click', (event) => {
    if (event.target === modal) {
      closeModal();
    }
  });

  if (searchForm && searchInput) {
    searchForm.addEventListener('submit', (event) => {
      event.preventDefault();
      const query = searchInput.value.trim();
      if (!query) {
        loadFeed({ href: '', query: '', activeTab: TabIds.ROOT, updateTabs: true });
      } else {
        loadFeed({ href: '', query, activeTab: TabIds.SEARCH });
      }
    });
  }

  if (refreshButton && searchInput) {
    refreshButton.addEventListener('click', () => {
      searchInput.value = '';
      loadFeed({ href: '', query: '', activeTab: TabIds.ROOT, updateTabs: true });
    });
  }
}
