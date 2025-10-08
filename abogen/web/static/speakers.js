const audioElement = new Audio();
let activeButton = null;
let activeUrl = null;

const setLoadingState = (button, isLoading) => {
  if (!button) return;
  button.disabled = isLoading;
  if (isLoading) {
    button.setAttribute("data-loading", "true");
  } else {
    button.removeAttribute("data-loading");
  }
};

const stopCurrentPlayback = () => {
  if (audioElement && !audioElement.paused) {
    audioElement.pause();
  }
  if (activeUrl) {
    URL.revokeObjectURL(activeUrl);
    activeUrl = null;
  }
  if (activeButton) {
    setLoadingState(activeButton, false);
    activeButton = null;
  }
};

audioElement.addEventListener("ended", () => {
  stopCurrentPlayback();
});

audioElement.addEventListener("pause", () => {
  if (audioElement.currentTime === 0 || audioElement.currentTime >= audioElement.duration) {
    stopCurrentPlayback();
  }
});

const playPreview = async (button) => {
  const text = (button.dataset.previewText || "").trim();
  const voice = (button.dataset.voice || "").trim();
  const language = (button.dataset.language || "a").trim() || "a";
  const speedRaw = button.dataset.speed || "1";
  const useGpu = (button.dataset.useGpu || "true") !== "false";
  const speed = Number.parseFloat(speedRaw);

  if (!text) {
    console.warn("Skipping speaker preview: no text provided");
    return;
  }
  if (!voice) {
    console.warn("Skipping speaker preview: no voice provided");
    return;
  }

  const payload = {
    text,
    voice,
    language,
    speed: Number.isFinite(speed) ? speed : 1.0,
    use_gpu: useGpu,
    max_seconds: 8,
  };

  stopCurrentPlayback();
  activeButton = button;
  setLoadingState(button, true);

  try {
    const response = await fetch("/api/speaker-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const message = await response.text();
      throw new Error(message || `Preview failed with status ${response.status}`);
    }
    const blob = await response.blob();
    activeUrl = URL.createObjectURL(blob);
    audioElement.src = activeUrl;
    await audioElement.play();
  } catch (error) {
    console.error("Failed to play speaker preview", error);
    stopCurrentPlayback();
  } finally {
    setLoadingState(button, false);
  }
};

document.addEventListener("click", (event) => {
  const trigger = event.target.closest('[data-role="speaker-preview"]');
  if (!trigger) return;
  event.preventDefault();
  if (trigger.disabled) return;
  playPreview(trigger);
});
