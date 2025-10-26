const form = document.querySelector('.settings__form');
const navButtons = Array.from(document.querySelectorAll('.settings-nav__item'));
const panels = Array.from(document.querySelectorAll('.settings-panel'));
const llmNavButton = navButtons.find((button) => button.dataset.section === 'llm');

const statusSelectors = {
  llm: document.querySelector('[data-role="llm-preview-status"]'),
  normalization: document.querySelector('[data-role="normalization-preview-status"]'),
};

const outputAreas = {
  llm: document.querySelector('[data-role="llm-preview-output"]'),
  normalization: document.querySelector('[data-role="normalization-preview-output"]'),
};

const normalizationAudio = document.querySelector('[data-role="normalization-preview-audio"]');

function setStatus(target, message, state) {
  if (!target) {
    return;
  }
  target.textContent = message || '';
  if (state) {
    target.dataset.state = state;
  } else {
    delete target.dataset.state;
  }
}

function clearStatus(target) {
  setStatus(target, '', null);
}

function activatePanel(section) {
  if (!section) {
    return;
  }
  navButtons.forEach((button) => {
    const isActive = button.dataset.section === section;
    button.classList.toggle('is-active', isActive);
  });
  let activePanel = null;
  panels.forEach((panel) => {
    const isActive = panel.dataset.section === section;
    panel.classList.toggle('is-active', isActive);
    if (isActive) {
      activePanel = panel;
    }
  });
  if (activePanel) {
    const focusable = activePanel.querySelector('input, select, textarea');
    if (focusable) {
      window.requestAnimationFrame(() => {
        focusable.focus({ preventScroll: false });
      });
    }
  }
}

function initNavigation() {
  if (!navButtons.length || !panels.length) {
    return;
  }
  navButtons.forEach((button) => {
    button.addEventListener('click', () => {
      activatePanel(button.dataset.section);
      if (button.dataset.section) {
        window.history.replaceState(null, '', `#${button.dataset.section}`);
      }
    });
  });
  const hash = window.location.hash.replace('#', '');
  if (hash && panels.some((panel) => panel.dataset.section === hash)) {
    activatePanel(hash);
  } else {
    const current = navButtons.find((button) => button.classList.contains('is-active'));
    if (current) {
      activatePanel(current.dataset.section);
    }
  }
  window.addEventListener('hashchange', () => {
    const section = window.location.hash.replace('#', '');
    if (section) {
      activatePanel(section);
    }
  });
}

function parseNumber(value, fallback) {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function collectLLMFields() {
  const baseUrl = form.querySelector('#llm_base_url');
  const apiKey = form.querySelector('#llm_api_key');
  const model = form.querySelector('#llm_model');
  const prompt = form.querySelector('#llm_prompt');
  const timeout = form.querySelector('#llm_timeout');
  const context = form.querySelector('input[name="llm_context_mode"]:checked');
  return {
    base_url: baseUrl ? baseUrl.value.trim() : '',
    api_key: apiKey ? apiKey.value.trim() : '',
    model: model ? model.value.trim() : '',
    prompt: prompt ? prompt.value : '',
    context_mode: context ? context.value : 'sentence',
    timeout: timeout ? parseNumber(timeout.value, 30) : 30,
  };
}

function updateModelOptions(models) {
  const select = form.querySelector('#llm_model');
  if (!select) {
    return;
  }
  const current = select.dataset.currentModel || select.value;
  select.innerHTML = '';
  if (!Array.isArray(models) || !models.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = 'No models found';
    select.appendChild(option);
    select.dataset.currentModel = '';
    select.disabled = true;
    return;
  }
  const fragment = document.createDocumentFragment();
  let matchedCurrent = false;
  models.forEach((entry) => {
    let identifier = '';
    let label = '';
    if (typeof entry === 'string') {
      identifier = entry;
      label = entry;
    } else if (entry && typeof entry === 'object') {
      identifier = String(entry.id || entry.name || entry.label || '').trim();
      label = String(entry.label || entry.name || identifier || '').trim();
    }
    if (!identifier) {
      return;
    }
    if (!label) {
      label = identifier;
    }
    const option = document.createElement('option');
    option.value = identifier;
    option.textContent = label;
    if (identifier === current) {
      option.selected = true;
      matchedCurrent = true;
    }
    fragment.appendChild(option);
  });
  select.appendChild(fragment);
  if (!matchedCurrent && select.options.length) {
    select.selectedIndex = 0;
  }
  select.dataset.currentModel = select.value || '';
  select.disabled = false;
}

async function refreshModels(button) {
  const status = statusSelectors.llm;
  const llmFields = collectLLMFields();
  if (!llmFields.base_url) {
    setStatus(status, 'Enter a base URL before refreshing models.', 'error');
    return;
  }
  clearStatus(status);
  setStatus(status, 'Fetching models…');
  button.disabled = true;
  try {
    const response = await fetch('/api/llm/models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        base_url: llmFields.base_url,
        api_key: llmFields.api_key,
        timeout: llmFields.timeout,
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || 'Unable to load models.');
    }
    updateModelOptions(payload.models || []);
    const count = Array.isArray(payload.models) ? payload.models.length : 0;
    if (count) {
      setStatus(status, `Loaded ${count} model${count === 1 ? '' : 's'}.`, 'success');
    } else {
      setStatus(status, 'No models were returned.', 'error');
    }
  } catch (error) {
    setStatus(status, error instanceof Error ? error.message : 'Failed to load models.', 'error');
  } finally {
    button.disabled = false;
  }
}

async function previewLLM(button) {
  const status = statusSelectors.llm;
  const output = outputAreas.llm;
  const previewText = document.querySelector('#llm_preview_text');
  if (!previewText) {
    return;
  }
  const llmFields = collectLLMFields();
  if (!llmFields.base_url) {
    setStatus(status, 'Enter a base URL to preview.', 'error');
    return;
  }
  if (!llmFields.model) {
    setStatus(status, 'Select a model to preview.', 'error');
    return;
  }
  const sample = previewText.value.trim();
  if (!sample) {
    setStatus(status, 'Add some sample text first.', 'error');
    return;
  }
  clearStatus(status);
  if (output) {
    output.textContent = '';
  }
  setStatus(status, 'Generating preview…');
  button.disabled = true;
  try {
    const response = await fetch('/api/llm/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: sample,
        base_url: llmFields.base_url,
        api_key: llmFields.api_key,
        model: llmFields.model,
        prompt: llmFields.prompt,
        context_mode: llmFields.context_mode,
        timeout: llmFields.timeout,
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || 'Preview failed.');
    }
    if (output) {
      output.textContent = payload.normalized_text || '';
    }
    setStatus(status, 'Preview ready.', 'success');
  } catch (error) {
    if (output) {
      output.textContent = '';
    }
    setStatus(status, error instanceof Error ? error.message : 'Preview failed.', 'error');
  } finally {
    button.disabled = false;
  }
}

function collectNormalizationSettings() {
  const normalization = {
    normalization_numbers: Boolean(form.querySelector('input[name="normalization_numbers"]')?.checked),
    normalization_titles: Boolean(form.querySelector('input[name="normalization_titles"]')?.checked),
    normalization_terminal: Boolean(form.querySelector('input[name="normalization_terminal"]')?.checked),
    normalization_phoneme_hints: Boolean(form.querySelector('input[name="normalization_phoneme_hints"]')?.checked),
    normalization_apostrophe_mode: form.querySelector('input[name="normalization_apostrophe_mode"]:checked')?.value || 'spacy',
  };
  return normalization;
}

function updateLLMNavState() {
  if (!llmNavButton) {
    return;
  }
  const fields = collectLLMFields();
  if (fields.base_url && fields.api_key) {
    llmNavButton.classList.remove('is-disabled');
  } else {
    llmNavButton.classList.add('is-disabled');
  }
}

async function previewNormalization(button) {
  const status = statusSelectors.normalization;
  const output = outputAreas.normalization;
  const textArea = document.querySelector('#normalization_sample_text');
  const voiceSelect = document.querySelector('#normalization_sample_voice');
  if (!textArea) {
    return;
  }
  const sample = textArea.value.trim();
  if (!sample) {
    setStatus(status, 'Enter some text to preview.', 'error');
    return;
  }
  clearStatus(status);
  if (output) {
    output.textContent = '';
  }
  if (normalizationAudio) {
    normalizationAudio.hidden = true;
    normalizationAudio.removeAttribute('src');
  }
  setStatus(status, 'Building preview…');
  button.disabled = true;
  try {
    const normalization = collectNormalizationSettings();
    const llmFields = collectLLMFields();
    const response = await fetch('/api/normalization/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: sample,
        voice: voiceSelect ? voiceSelect.value : undefined,
        normalization,
        llm: {
          llm_base_url: llmFields.base_url,
          llm_api_key: llmFields.api_key,
          llm_model: llmFields.model,
          llm_prompt: llmFields.prompt,
          llm_context_mode: llmFields.context_mode,
          llm_timeout: llmFields.timeout,
        },
        max_seconds: 8,
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || 'Preview failed.');
    }
    if (output) {
      output.textContent = payload.normalized_text || '';
    }
    if (payload.audio_base64 && normalizationAudio) {
      normalizationAudio.src = `data:audio/wav;base64,${payload.audio_base64}`;
      normalizationAudio.hidden = false;
      normalizationAudio.load();
      normalizationAudio.play().catch(() => {
        /* autoplay can fail; ignore */
      });
    }
    setStatus(status, 'Preview updated.', 'success');
  } catch (error) {
    if (output) {
      output.textContent = '';
    }
    if (normalizationAudio) {
      normalizationAudio.hidden = true;
      normalizationAudio.removeAttribute('src');
    }
    setStatus(status, error instanceof Error ? error.message : 'Preview failed.', 'error');
  } finally {
    button.disabled = false;
  }
}

function initSampleSelector() {
  const select = document.querySelector('#normalization_sample_select');
  const textArea = document.querySelector('#normalization_sample_text');
  if (!select || !textArea) {
    return;
  }
  select.addEventListener('change', () => {
    const option = select.selectedOptions[0];
    if (option) {
      textArea.value = option.value;
    }
  });
}

function initActions() {
  const refreshButton = document.querySelector('[data-action="llm-refresh-models"]');
  if (refreshButton) {
    refreshButton.addEventListener('click', () => refreshModels(refreshButton));
  }
  const llmPreviewButton = document.querySelector('[data-action="llm-preview"]');
  if (llmPreviewButton) {
    llmPreviewButton.addEventListener('click', () => previewLLM(llmPreviewButton));
  }
  const normalizationButton = document.querySelector('[data-action="normalization-preview"]');
  if (normalizationButton) {
    normalizationButton.addEventListener('click', () => previewNormalization(normalizationButton));
  }
}

function initLLMStateWatchers() {
  const baseUrlInput = form.querySelector('#llm_base_url');
  const apiKeyInput = form.querySelector('#llm_api_key');
  if (!baseUrlInput || !apiKeyInput) {
    return;
  }
  const handler = () => updateLLMNavState();
  baseUrlInput.addEventListener('input', handler);
  apiKeyInput.addEventListener('input', handler);
  updateLLMNavState();
}

if (form) {
  initNavigation();
  initSampleSelector();
  initActions();
  initLLMStateWatchers();
}
