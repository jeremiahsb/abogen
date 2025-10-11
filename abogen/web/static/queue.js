import { initReaderUI } from "./reader.js";

const initQueuePage = () => {
  initReaderUI();
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initQueuePage, { once: true });
} else {
  initQueuePage();
}
