"use strict";

const FALLBACK_SPEC = {
  pitch: { default: 0, min: -1.5, max: 1.5, step: 0.1 },
  speed: { default: 1, min: 0.95, max: 1.05, step: 0.01 },
  highpass_hz: { default: 70, min: 20, max: 500, step: 1 },
  lowpass_hz: { default: 15000, min: 4000, max: 20000, step: 50 },
  noise_db: { default: -48, min: -80, max: -24, step: 1 },
  noise_type: { default: "pink" },
  output_format: { default: "wav" },
  eq: { default: true },
};

const fileInput = document.querySelector("#file");
const dropzone = document.querySelector("#dropzone");
const dropTitle = document.querySelector("#drop-title");
const dropMeta = document.querySelector("#drop-meta");
const clearFileButton = document.querySelector("#clear-file");
const sourcePreview = document.querySelector("#source-preview");
const sourceWave = document.querySelector("#source-wave");
const sourceAudio = document.querySelector("#source-audio");
const form = document.querySelector("#form");
const pitch = document.querySelector("#pitch");
const speed = document.querySelector("#speed");
const eq = document.querySelector("#eq");
const highpass = document.querySelector("#highpass");
const lowpass = document.querySelector("#lowpass");
const noiseDb = document.querySelector("#noise-db");
const randomize = document.querySelector("#randomize");
const seed = document.querySelector("#seed");
const formError = document.querySelector("#form-error");
const formStatus = document.querySelector("#form-status");
const submitButton = document.querySelector("#submit");
const resultEmpty = document.querySelector("#result-empty");
const resultBody = document.querySelector("#result-body");
const resultWave = document.querySelector("#result-wave");
const resultAudio = document.querySelector("#result-audio");
const resultMeta = document.querySelector("#result-meta");
const resultNote = document.querySelector("#result-note");
const downloadButton = document.querySelector("#download");
const resultSeconds = document.querySelector("#result-seconds");

let spec = FALLBACK_SPEC;
let ffmpegOk = true;
let busy = false;
let sourceUrl = "";
let resultUrl = "";
let resultBlob = null;
let resultFilename = "audio.wav";
let resultDuration = 0;
let youtubePreviewUrl = "";
let sourceSummary = null;
let resultSummary = null;

document.querySelector("#gentle").addEventListener("click", applyGentle);
document.querySelector("#audition").addEventListener("click", applyAudition);
document.querySelector("#demo").addEventListener("click", () => process({ demo: true }));
clearFileButton.addEventListener("click", clearFile);
form.addEventListener("submit", (event) => {
  event.preventDefault();
  process({ demo: false });
});
form.addEventListener("input", sync);
fileInput.addEventListener("change", () => {
  const file = fileInput.files && fileInput.files[0];
  if (file) onFile(file);
});

["dragenter", "dragover"].forEach((name) => {
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.add("is-over");
  });
});
["dragleave", "drop"].forEach((name) => {
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-over");
  });
});
dropzone.addEventListener("drop", (event) => {
  const file = event.dataTransfer && event.dataTransfer.files[0];
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  fileInput.files = transfer.files;
  onFile(file);
});

document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    form.requestSubmit();
  }
});
window.addEventListener("resize", () => {
  if (sourceSummary) drawSummary(sourceWave, sourceSummary, "#d7deea");
  if (resultSummary) drawSummary(resultWave, resultSummary, "#ffb703");
});
window.addEventListener("beforeunload", revokeUrls);

document.querySelectorAll("[data-tab]").forEach((button) => {
  button.addEventListener("click", () => showTab(button.dataset.tab));
});
document.querySelector("#yt-browse").addEventListener("click", chooseSaveFolder);
downloadButton.addEventListener("click", downloadKeptSeconds);
resultAudio.addEventListener("loadedmetadata", () => {
  resultDuration = Number.isFinite(resultAudio.duration) ? resultAudio.duration : 0;
  if (resultDuration > 0) {
    resultSeconds.max = String(Math.ceil(resultDuration));
    resultSeconds.value = String(Math.max(1, Math.round(resultDuration)));
  }
});

loadCapabilities();
sync();

function showTab(name) {
  document.querySelectorAll("[data-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.panel !== name;
  });
  document.querySelectorAll("[data-tab]").forEach((button) => {
    const selected = button.dataset.tab === name;
    button.setAttribute("aria-selected", selected ? "true" : "false");
  });
}

async function loadCapabilities() {
  const status = document.querySelector("#api-status");
  try {
    const response = await fetch("/api/capabilities");
    if (!response.ok) throw new Error("capabilities");
    const data = await response.json();
    spec = data.spec || FALLBACK_SPEC;
    ffmpegOk = Boolean(data.ffmpeg);
    applyBounds();
    applyGentle();
    const mp3 = document.querySelector('input[name="output_format"][value="mp3"]');
    mp3.disabled = !ffmpegOk;
    document.querySelector("#ffmpeg-note").hidden = ffmpegOk;
    if (!ffmpegOk && mp3.checked) {
      document.querySelector('input[name="output_format"][value="wav"]').checked = true;
    }
    applyYoutubeDefaults(data.youtube || {});
    status.textContent = "API sẵn sàng.";
  } catch (error) {
    status.textContent = "Không kết nối được API. Hãy chạy python api.py rồi mở lại trang này.";
  }
}

document.querySelector("#youtube-form").addEventListener("submit", (event) => {
  event.preventDefault();
  downloadYoutube();
});

function applyYoutubeDefaults(youtube) {
  const note = document.querySelector("#yt-tool-note");
  const cookiesNote = document.querySelector("#yt-cookies-note");
  const directory = document.querySelector("#yt-dir");
  const remembered = window.localStorage.getItem("audio-augment-save-dir");
  if (remembered) {
    directory.value = remembered;
  } else if (!directory.value.trim() && youtube.output_dir) {
    directory.value = youtube.output_dir;
  }
  cookiesNote.textContent = youtube.cookies_found
    ? `Đã thấy cookies: ${youtube.cookies}`
    : "Chưa thấy Downloads\\cookies.txt. Video công khai vẫn tải được.";
  if (!youtube.yt_dlp) {
    note.hidden = false;
    note.textContent = "Chưa thấy yt-dlp. Cài bằng winget install yt-dlp.yt-dlp rồi mở lại trang.";
    document.querySelector("#yt-submit").disabled = true;
  } else if (!youtube.ffmpeg) {
    note.hidden = false;
    note.textContent = "Tải audio YouTube cần FFmpeg trong PATH.";
    document.querySelector("#yt-submit").disabled = true;
  } else {
    note.hidden = true;
  }
}

async function downloadYoutube() {
  const button = document.querySelector("#yt-submit");
  const status = document.querySelector("#yt-status");
  const error = document.querySelector("#yt-error");
  const startTime = document.querySelector("#yt-start").value.trim();
  const endTime = document.querySelector("#yt-end").value.trim();
  const folder = document.querySelector("#yt-dir").value.trim();
  error.hidden = true;
  if (!startTime || !endTime) {
    error.hidden = false;
    error.textContent = "Tải YouTube chỉ cần thời điểm bắt đầu và thời điểm kết thúc.";
    return;
  }
  if (!folder) {
    error.hidden = false;
    error.textContent = "Hãy chọn thư mục lưu. Có thể để ở thư mục khác, không bắt buộc D:\\ytmusic.";
    return;
  }
  window.localStorage.setItem("audio-augment-save-dir", folder);
  button.disabled = true;
  const started = performance.now();
  status.textContent = "Đang tải đoạn từ YouTube…";
  const timer = window.setInterval(() => {
    const seconds = ((performance.now() - started) / 1000).toFixed(0);
    status.textContent = `Đang tải đoạn từ YouTube… ${seconds} giây`;
  }, 500);
  try {
    const body = new FormData();
    body.append("url", document.querySelector("#yt-url").value.trim());
    body.append("start", document.querySelector("#yt-start").value.trim());
    body.append("end", document.querySelector("#yt-end").value.trim());
    body.append("output_dir", document.querySelector("#yt-dir").value.trim());
    body.append("cookies", document.querySelector("#yt-cookies").value.trim());
    const response = await fetch("/api/youtube", { method: "POST", body });
    if (!response.ok) {
      let message = "Không tải được đoạn YouTube.";
      try {
        const payload = await response.json();
        message = formatDetail(payload.detail);
      } catch (readError) {
        message = "Không tải được đoạn YouTube.";
      }
      throw new Error(message);
    }
    const blob = await response.blob();
    const metaHeader = response.headers.get("X-Augment-Meta");
    const meta = metaHeader ? JSON.parse(decodeURIComponent(metaHeader)) : { kind: "youtube" };
    const saved = response.headers.get("X-Saved-Path");
    if (saved) meta.saved_path = decodeURIComponent(saved);
    showYoutubeResult(blob, meta);
    status.textContent = meta.saved_path
      ? `Đã lưu ${meta.saved_path}`
      : `Xong trong ${((performance.now() - started) / 1000).toFixed(1)} giây.`;
  } catch (downloadError) {
    error.hidden = false;
    error.textContent = downloadError.message || "Không tải được đoạn YouTube.";
    status.textContent = "";
  } finally {
    window.clearInterval(timer);
    button.disabled = false;
  }
}

function applyBounds() {
  setRange(pitch, spec.pitch);
  setRange(speed, spec.speed);
  setRange(noiseDb, spec.noise_db);
  setNumber(highpass, spec.highpass_hz);
  setNumber(lowpass, spec.lowpass_hz);
}

function setRange(input, item) {
  input.min = item.min;
  input.max = item.max;
  input.step = item.step;
}

function setNumber(input, item) {
  input.min = item.min;
  input.max = item.max;
  input.step = item.step;
}

function applyGentle() {
  pitch.value = spec.pitch.default;
  speed.value = spec.speed.default;
  highpass.value = spec.highpass_hz.default;
  lowpass.value = spec.lowpass_hz.default;
  noiseDb.value = spec.noise_db.default;
  eq.checked = spec.eq.default !== false;
  selectRadio("noise_type", spec.noise_type.default);
  selectRadio("output_format", ffmpegOk ? spec.output_format.default : "wav");
  randomize.checked = false;
  seed.value = "";
  clearError();
  sync();
}

function applyAudition() {
  applyGentle();
  pitch.value = spec.pitch.max;
  speed.value = spec.speed.max;
  const audible = Math.min(spec.noise_db.max, Math.max(spec.noise_db.min, -30));
  noiseDb.value = audible;
  sync();
}

function selectRadio(name, value) {
  const input = document.querySelector(`input[name="${name}"][value="${value}"]`);
  if (input && !input.disabled) input.checked = true;
}

function selectedValue(name) {
  const input = document.querySelector(`input[name="${name}"]:checked`);
  return input ? input.value : "";
}

function sync() {
  document.querySelector("#pitch-readout").textContent = formatPitch(pitch.value);
  document.querySelector("#speed-readout").textContent = `${Number(speed.value).toFixed(2)}×`;
  document.querySelector("#noise-readout").textContent = `${prettyMinus(Number(noiseDb.value).toFixed(0))} dB`;
  paintRange(pitch);
  paintRange(speed);
  paintRange(noiseDb);

  const noiseOn = selectedValue("noise_type") !== "none";
  noiseDb.disabled = !noiseOn;
  highpass.disabled = !eq.checked;
  lowpass.disabled = !eq.checked;
  pitch.disabled = randomize.checked;
  speed.disabled = randomize.checked;

  setChip("pitch", randomize.checked || Math.abs(Number(pitch.value)) > 0.001);
  setChip("speed", randomize.checked || Math.abs(Number(speed.value) - 1) > 0.0001);
  setChip("eq", eq.checked);
  setChip("noise", noiseOn);
}

function setChip(step, on) {
  const chip = document.querySelector(`[data-step="${step}"]`);
  chip.classList.toggle("is-on", on);
}

function paintRange(input) {
  const min = Number(input.min);
  const max = Number(input.max);
  const value = Number(input.value);
  const fill = max === min ? 0 : ((value - min) / (max - min)) * 100;
  input.style.setProperty("--fill", `${fill}%`);
}

function onFile(file) {
  clearError();
  dropzone.classList.add("has-file");
  dropTitle.textContent = file.name;
  dropMeta.textContent = formatBytes(file.size);
  clearFileButton.hidden = false;
  if (sourceUrl) URL.revokeObjectURL(sourceUrl);
  sourceUrl = URL.createObjectURL(file);
  sourceAudio.src = sourceUrl;
  sourcePreview.hidden = false;
  sourceWave.hidden = false;
  sourceSummary = null;
  decodeChannel(file)
    .then((channel) => {
      if (!channel) return;
      sourceSummary = summarize(channel);
      requestAnimationFrame(() => drawSummary(sourceWave, sourceSummary, "#d7deea"));
      const seconds = channel.length ? "" : "";
      dropMeta.textContent = `${formatBytes(file.size)} · ${channelDuration(file, channel)}`;
    })
    .catch(() => {
      sourceWave.hidden = true;
    });
}

function channelDuration(file, channel) {
  if (sourceAudio.duration && Number.isFinite(sourceAudio.duration)) {
    return `${sourceAudio.duration.toFixed(1)} giây`;
  }
  return file.type || "âm thanh";
}

function clearFile() {
  fileInput.value = "";
  dropzone.classList.remove("has-file");
  dropTitle.textContent = "Kéo thả file vào đây";
  dropMeta.textContent = "hoặc bấm để chọn";
  clearFileButton.hidden = true;
  sourcePreview.hidden = true;
  sourceSummary = null;
  sourceAudio.removeAttribute("src");
  if (sourceUrl) URL.revokeObjectURL(sourceUrl);
  sourceUrl = "";
}

async function process({ demo }) {
  if (busy) return;
  const file = fileInput.files && fileInput.files[0];
  if (!demo && !file) {
    showError("Hãy chọn một file âm thanh, hoặc bấm Xử lý âm mẫu.");
    return;
  }
  clearError();
  setBusy(true);
  const started = performance.now();
  const timer = window.setInterval(() => {
    const seconds = ((performance.now() - started) / 1000).toFixed(0);
    formStatus.textContent = `Đang xử lý… ${seconds} giây`;
  }, 250);
  formStatus.textContent = "Đang xử lý…";
  try {
    const body = buildParams();
    if (!demo) body.append("file", file, file.name);
    const response = await fetch(demo ? "/api/demo" : "/api/augment", { method: "POST", body });
    if (!response.ok) {
      let message = "Không xử lý được file.";
      try {
        const payload = await response.json();
        message = formatDetail(payload.detail);
      } catch (error) {
        message = "Không xử lý được file.";
      }
      throw new Error(message);
    }
    const blob = await response.blob();
    const metaHeader = response.headers.get("X-Augment-Meta");
    const meta = metaHeader ? JSON.parse(decodeURIComponent(metaHeader)) : null;
    const filename = filenameFromDisposition(
      response.headers.get("Content-Disposition"),
      demo ? "am_mau_aug.wav" : "audio_aug.wav",
    );
    showResult(blob, meta, filename);
    formStatus.textContent = `Xong trong ${((performance.now() - started) / 1000).toFixed(1)} giây.`;
  } catch (error) {
    showError(error.message || "Không kết nối được máy chủ.");
    formStatus.textContent = "";
  } finally {
    window.clearInterval(timer);
    setBusy(false);
  }
}

function buildParams() {
  const body = new FormData();
  body.append("pitch", pitch.value);
  body.append("speed", speed.value);
  body.append("highpass_hz", highpass.value);
  body.append("lowpass_hz", lowpass.value);
  body.append("eq", eq.checked ? "true" : "false");
  body.append("noise_type", selectedValue("noise_type"));
  body.append("noise_db", noiseDb.value);
  body.append("output_format", selectedValue("output_format") || "wav");
  body.append("randomize", randomize.checked ? "true" : "false");
  if (seed.value.trim()) body.append("seed", seed.value.trim());
  return body;
}

function showYoutubeResult(blob, meta) {
  const box = document.querySelector("#yt-result");
  const audio = document.querySelector("#yt-audio");
  const saved = document.querySelector("#yt-saved");
  if (youtubePreviewUrl) URL.revokeObjectURL(youtubePreviewUrl);
  youtubePreviewUrl = URL.createObjectURL(blob);
  audio.src = youtubePreviewUrl;
  saved.textContent = meta.saved_path
    ? `Đã lưu: ${meta.saved_path}`
    : "Đã tải xong.";
  box.hidden = false;
}

async function chooseSaveFolder() {
  const status = document.querySelector("#yt-status");
  const error = document.querySelector("#yt-error");
  const directory = document.querySelector("#yt-dir");
  const button = document.querySelector("#yt-browse");
  error.hidden = true;
  button.disabled = true;
  status.textContent = "Đang mở hộp chọn thư mục…";
  try {
    const body = new FormData();
    body.append("initial", directory.value.trim());
    const response = await fetch("/api/pick-folder", { method: "POST", body });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(formatDetail(payload.detail));
    if (payload.path) {
      directory.value = payload.path;
      window.localStorage.setItem("audio-augment-save-dir", payload.path);
      status.textContent = `Sẽ lưu vào ${payload.path}`;
    } else {
      status.textContent = "Giữ thư mục đang nhập.";
    }
  } catch (pickError) {
    error.hidden = false;
    error.textContent = pickError.message || "Không chọn được thư mục. Anh vẫn có thể dán đường dẫn vào ô.";
    status.textContent = "";
  } finally {
    button.disabled = false;
  }
}

async function downloadKeptSeconds() {
  if (!resultBlob) return;
  const requested = Number(resultSeconds.value);
  const keepAll = !Number.isFinite(requested) || requested <= 0 || requested >= resultDuration - 0.4;
  downloadButton.disabled = true;
  try {
    let blob = resultBlob;
    let filename = resultFilename;
    if (!keepAll) {
      const body = new FormData();
      body.append("file", resultBlob, resultFilename);
      body.append("seconds", String(requested));
      const response = await fetch("/api/clip", { method: "POST", body });
      if (!response.ok) {
        let message = "Không cắt được số giây muốn tải.";
        try {
          const payload = await response.json();
          message = formatDetail(payload.detail);
        } catch (readError) {
          message = "Không cắt được số giây muốn tải.";
        }
        throw new Error(message);
      }
      blob = await response.blob();
      filename = filenameFromDisposition(response.headers.get("Content-Disposition"), filename);
    }
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
    clearError();
  } catch (downloadError) {
    showError(downloadError.message || "Không tải được file.");
  } finally {
    downloadButton.disabled = false;
  }
}

function showResult(blob, meta, filename) {
  if (resultUrl) URL.revokeObjectURL(resultUrl);
  resultBlob = blob;
  resultFilename = filename;
  resultUrl = URL.createObjectURL(blob);
  resultAudio.src = resultUrl;
  resultEmpty.hidden = true;
  resultBody.hidden = false;
  resultSeconds.value = "";
  resultSeconds.disabled = false;
  downloadButton.disabled = false;
  renderMeta(meta);
  resultSummary = null;
  resultWave.hidden = false;
  decodeChannel(blob)
    .then((channel) => {
      if (!channel) return;
      resultSummary = summarize(channel);
      requestAnimationFrame(() => drawSummary(resultWave, resultSummary, "#ffb703"));
    })
    .catch(() => {
      resultWave.hidden = true;
    });
  document.querySelector("#result-panel").scrollIntoView({ block: "nearest" });
}

function renderMeta(meta) {
  resultMeta.replaceChildren();
  resultNote.hidden = true;
  if (!meta) return;
  if (meta.kind === "youtube") {
    const kept = meta.keep_seconds
      ? `${Number(meta.keep_seconds).toFixed(0)} giây, từ giây thứ ${Number(meta.offset_seconds).toFixed(0)}`
      : "cả đoạn";
    const rows = [
      ["Tiêu đề", meta.title || "YouTube"],
      ["Khoảng", `${meta.section_start} – ${meta.section_end}`],
      ["Đã giữ", kept],
      ["Thời lượng file", `${Number(meta.duration_out).toFixed(2)} giây`],
      ["Đã lưu", meta.saved_path || ""],
    ];
    rows.forEach(([label, value]) => {
      const term = document.createElement("dt");
      term.textContent = label;
      const description = document.createElement("dd");
      description.textContent = value;
      resultMeta.append(term, description);
    });
    return;
  }
  const rows = [
    ["Cao độ", formatPitch(meta.pitch_semitones)],
    ["Tempo", `${Number(meta.speed).toFixed(3)}×`],
    ["EQ", meta.eq ? `${formatHz(meta.highpass_hz)} – ${formatHz(meta.lowpass_hz)}` : "Tắt"],
    ["Nhiễu", meta.noise_type === "none" ? "Tắt" : `${meta.noise_type === "pink" ? "Hồng" : "Trắng"} ${prettyMinus(Number(meta.noise_db).toFixed(0))} dB`],
    ["Thời lượng", `${Number(meta.duration_in).toFixed(2)} s → ${Number(meta.duration_out).toFixed(2)} s`],
    ["Sample rate", `${meta.sample_rate} Hz`],
    ["Kênh", String(meta.channels)],
    ["Seed", meta.seed === null || meta.seed === undefined ? "không cố định" : String(meta.seed)],
  ];
  rows.forEach(([label, value]) => {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value;
    resultMeta.append(term, description);
  });
  if (meta.eq_clamped) {
    resultNote.hidden = false;
    resultNote.textContent = "Tần số cắt đã được hạ vì sample rate của file không đủ cho mức đã chọn.";
  }
}

function setBusy(value) {
  busy = value;
  submitButton.disabled = value;
  document.querySelector("#demo").disabled = value;
}

function showError(message) {
  formError.hidden = false;
  formError.textContent = message;
}

function clearError() {
  formError.hidden = true;
  formError.textContent = "";
}

function formatDetail(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg || "Yêu cầu không hợp lệ.").join(" ");
  return "Yêu cầu không hợp lệ.";
}

function filenameFromDisposition(header, fallback) {
  if (!header) return fallback;
  const star = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (star) return decodeURIComponent(star[1]);
  const plain = /filename="([^"]+)"/i.exec(header);
  return plain ? plain[1] : fallback;
}

function formatPitch(value) {
  const number = Number(value);
  const text = `${number > 0 ? "+" : ""}${number.toFixed(1)} st`;
  return prettyMinus(text);
}

function prettyMinus(text) {
  return String(text).replace("-", "−");
}

function formatHz(value) {
  const number = Number(value);
  if (number >= 1000) {
    const khz = number / 1000;
    const digits = khz >= 10 ? 1 : 2;
    return `${khz.toFixed(digits).replace(/\.0$/, "")} kHz`;
  }
  return `${Math.round(number)} Hz`;
}

function formatBytes(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

async function decodeChannel(source) {
  const buffer = await source.arrayBuffer();
  const context = new AudioContext();
  try {
    const audio = await context.decodeAudioData(buffer.slice(0));
    return audio.getChannelData(0).slice();
  } finally {
    await context.close();
  }
}

function summarize(channel, buckets = 1200) {
  const size = Math.max(1, Math.floor(channel.length / buckets));
  const mins = [];
  const maxs = [];
  for (let index = 0; index < channel.length; index += size) {
    let min = 1;
    let max = -1;
    const end = Math.min(channel.length, index + size);
    for (let cursor = index; cursor < end; cursor += 1) {
      const value = channel[cursor];
      if (value < min) min = value;
      if (value > max) max = value;
    }
    mins.push(min);
    maxs.push(max);
  }
  return { mins, maxs };
}

function drawSummary(canvas, summary, color) {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight || 84;
  if (width < 2) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#0d0f13";
  context.fillRect(0, 0, width, height);
  const mid = height / 2;
  context.strokeStyle = "rgba(255,255,255,0.08)";
  context.beginPath();
  context.moveTo(0, mid);
  context.lineTo(width, mid);
  context.stroke();
  context.strokeStyle = color;
  context.lineWidth = 1;
  context.beginPath();
  const amp = height * 0.42;
  const count = summary.mins.length;
  for (let x = 0; x < width; x += 1) {
    const index = Math.min(count - 1, Math.floor((x / width) * count));
    context.moveTo(x + 0.5, mid + summary.mins[index] * amp);
    context.lineTo(x + 0.5, mid + summary.maxs[index] * amp);
  }
  context.stroke();
}

function revokeUrls() {
  if (sourceUrl) URL.revokeObjectURL(sourceUrl);
  if (resultUrl) URL.revokeObjectURL(resultUrl);
  if (youtubePreviewUrl) URL.revokeObjectURL(youtubePreviewUrl);
}
