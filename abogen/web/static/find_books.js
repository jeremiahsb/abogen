const browser = document.querySelector('[data-role="opds-browser"]');

if (browser) {
  const statusEl = browser.querySelector('[data-role="opds-status"]');
  const resultsEl = browser.querySelector('[data-role="opds-results"]');
  const navEl = browser.querySelector('[data-role="opds-nav"]');
  const searchForm = browser.querySelector('[data-role="opds-search"]');
  const searchInput = searchForm?.querySelector('input[name="q"]');
  const refreshButton = browser.querySelector('[data-action="opds-refresh"]');

  const state = {
    query: '',
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

  const truncate = (text, limit = 320) => {
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

  const renderNav = (links) => {
    if (!navEl) {
      return;
    }
    navEl.innerHTML = '';
    const descriptors = [
      { key: 'up', label: 'Up' },
      { key: 'previous', label: 'Previous' },
      { key: 'next', label: 'Next' },
    ];
    descriptors.forEach(({ key, label }) => {
      const link = resolveRelLink(links, key) || resolveRelLink(links, `/${key}`);
      if (!link || !link.href) {
        return;
      }
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'button button--ghost';
      button.dataset.href = link.href;
      button.dataset.rel = key;
      button.textContent = label;
      button.addEventListener('click', () => {
        clearStatus();
        loadFeed({ href: link.href, query: state.query });
      });
      navEl.appendChild(button);
    });
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

    if (entry.summary) {
      const summary = document.createElement('p');
      summary.className = 'opds-browser__summary';
      summary.textContent = truncate(entry.summary, 380);
      item.appendChild(summary);
    }

    const actions = document.createElement('div');
    actions.className = 'opds-browser__actions';

    const downloadLink = entry.download && entry.download.href ? entry.download.href : null;
    const alternateLink = entry.alternate && entry.alternate.href ? entry.alternate.href : null;

    if (downloadLink) {
      const queueButton = document.createElement('button');
      queueButton.type = 'button';
      queueButton.className = 'button';
      queueButton.textContent = 'Queue for conversion';
      queueButton.addEventListener('click', () => importEntry(entry, queueButton));
      actions.appendChild(queueButton);
    }

    if (alternateLink) {
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
    return item;
  };

  const renderEntries = (entries) => {
    if (!resultsEl) {
      return;
    }
    resultsEl.innerHTML = '';
    if (!Array.isArray(entries) || !entries.length) {
      const empty = document.createElement('li');
      empty.className = 'opds-browser__empty';
      empty.textContent = 'No books found in this view.';
      resultsEl.appendChild(empty);
      return;
    }
    const fragment = document.createDocumentFragment();
    entries.forEach((entry) => {
      fragment.appendChild(createEntry(entry));
    });
    resultsEl.appendChild(fragment);
  };

  const importEntry = async (entry, trigger) => {
    if (!entry?.download?.href) {
      setStatus('This entry cannot be imported automatically.', 'error');
      return;
    }
    const button = trigger;
    if (button) {
      button.disabled = true;
      button.dataset.loading = 'true';
    }
    setStatus(`Queueing “${entry.title || 'Untitled'}”…`, 'loading');
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
      setStatus('Book queued. Opening the conversion wizard…', 'success');
      if (payload.redirect_url) {
        window.location.href = payload.redirect_url;
      }
    } catch (error) {
      setStatus(error instanceof Error ? error.message : 'Unable to queue this book.', 'error');
    } finally {
      if (button) {
        button.disabled = false;
        delete button.dataset.loading;
      }
    }
  };

  const loadFeed = async ({ href = '', query = '' } = {}) => {
    const params = new URLSearchParams();
    if (href) {
      params.set('href', href);
    }
    if (query) {
      params.set('q', query);
    }
    setStatus('Loading catalog…', 'loading');
    try {
      const url = `/api/integrations/calibre-opds/feed${params.toString() ? `?${params.toString()}` : ''}`;
      const response = await fetch(url);
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || 'Unable to load the Calibre catalog.');
      }
      const feed = payload.feed || {};
      state.query = query;
      renderNav(feed.links);
      renderEntries(feed.entries || []);
      if (Array.isArray(feed.entries) && feed.entries.length) {
        setStatus(`Showing ${feed.entries.length} item${feed.entries.length === 1 ? '' : 's'}.`, 'success');
      } else {
        setStatus('No books found in this view.', 'info');
      }
    } catch (error) {
      setStatus(error instanceof Error ? error.message : 'Unable to load the Calibre catalog.', 'error');
      renderEntries([]);
      if (navEl) {
        navEl.innerHTML = '';
      }
    }
  };

  if (searchForm && searchInput) {
    searchForm.addEventListener('submit', (event) => {
      event.preventDefault();
      const query = searchInput.value.trim();
      loadFeed({ query });
    });
  }

  if (refreshButton && searchInput) {
    refreshButton.addEventListener('click', () => {
      searchInput.value = '';
      loadFeed({});
    });
  }

  loadFeed({});
}
