const filePattern = /\.(mp4|m4v|webm|mov|mp3|m4a|aac|ogg|opus|wav|m3u8|mpd|ts|m4s)(?:$|[?#])/i;
const videoMime = /^video\//i;
const streamMime = /^application\/(vnd\.apple\.mpegurl|x-mpegurl|dash\+xml)/i;
const audioMime = /^audio\//i;

// Default takeover is safe only because it is gated by a recent, trusted click
// on a download link below. Earlier versions lacked that click gate.
chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get({autoTakeoverClickGateVersion: 0}, state => {
    if (state.autoTakeoverClickGateVersion < 1) {
      chrome.storage.local.set({autoTakeover: true, autoTakeoverClickGateVersion: 1});
    }
  });
});

function isNoise(url) {
  try {
    const u = new URL(url);
    return /\/(search|s\/search|generate_204|ptracking|api\/stats)\//i.test(u.pathname)
      || /\/success\.(mp3|mp4|webm)$/i.test(u.pathname)
      || (u.hostname === "googlevideo.com" || u.hostname.endsWith(".googlevideo.com")) && !/\/videoplayback$/i.test(u.pathname);
  } catch { return true; }
}

function mediaDetails(url) {
  try {
    const u = new URL(url);
    const mime = decodeURIComponent(u.searchParams.get("mime") || "");
    const itag = u.searchParams.get("itag");
    const quality = u.searchParams.get("quality") || "";
    const isGoogleVideo = (u.hostname === "googlevideo.com" || u.hostname.endsWith(".googlevideo.com")) && /\/videoplayback$/i.test(u.pathname);
    return {isGoogleVideo, mime, itag, quality};
  } catch { return {isGoogleVideo:false, mime:"", itag:"", quality:""}; }
}

// Capture only ordinary, non-secret browser headers for the active playback
// request. Cookie and Authorization are deliberately excluded.
function safePlaybackHeaders(headers) {
  const allowed = new Set(["user-agent", "referer", "origin", "accept", "accept-language", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site"]);
  const result = {};
  for (const header of headers || []) {
    const name = String(header.name || "").toLowerCase();
    if (allowed.has(name) && typeof header.value === "string") result[name] = header.value.slice(0, 2048);
  }
  return result;
}

function clearTabMedia(tabId) {
  chrome.storage.session.remove([String(tabId), `requestHeaders:${tabId}`]);
}

chrome.webRequest.onBeforeSendHeaders.addListener(details => {
  if (details.tabId < 0 || !mediaDetails(details.url).isGoogleVideo) return;
  const headers = safePlaybackHeaders(details.requestHeaders);
  const key = `requestHeaders:${details.tabId}`;
  chrome.storage.session.get({[key]: {}}, state => {
    const byUrl = state[key] || {};
    byUrl[details.url] = headers;
    const urls = Object.keys(byUrl);
    for (const oldUrl of urls.slice(0, Math.max(0, urls.length - 60))) delete byUrl[oldUrl];
    chrome.storage.session.set({[key]: byUrl});
  });
}, {urls: ["https://*.googlevideo.com/videoplayback*"]}, ["requestHeaders"]);

function saveCandidate(tabId, url, responseMime = "", requestType = "") {
  if (tabId < 0 || isNoise(url)) return;
  const info = mediaDetails(url);
  const mime = info.isGoogleVideo && info.mime ? info.mime : responseMime || info.mime;
  const isMediaRequest = requestType === "media";
  const accepted = info.isGoogleVideo && (videoMime.test(mime) || audioMime.test(mime))
    || isMediaRequest && (videoMime.test(mime) || audioMime.test(mime) || streamMime.test(mime))
    || (filePattern.test(url) && (isMediaRequest || videoMime.test(mime) || streamMime.test(mime)));
  if (!accepted) return;
  const key = String(tabId);
  const headersKey = `requestHeaders:${tabId}`;
  chrome.storage.session.get({[key]: [], [headersKey]: {}}, state => {
    const list = state[key];
    const capturedHeaders = state[headersKey]?.[url] || {};
    const existing = list.find(item => item.url === url);
    if (existing) {
      existing.requestHeaders = capturedHeaders;
      existing.isGoogleVideo = info.isGoogleVideo;
      existing.time = Date.now();
      list.splice(list.indexOf(existing), 1);
      list.unshift(existing);
    } else {
      const kind = videoMime.test(mime) ? "视频" : audioMime.test(mime) ? "音频" : "媒体流";
      const quality = info.quality || "";
      const format = [mime, info.itag ? `itag ${info.itag}` : "", quality].filter(Boolean).join(" · ");
      list.unshift({url, mime: format || "媒体请求", kind, isGoogleVideo: info.isGoogleVideo, requestHeaders: capturedHeaders, time: Date.now()});
    }
    chrome.storage.session.set({[key]: list.slice(0, 60)});
  });
}

// Headers arrive before a media response body finishes (which may take minutes).
chrome.webRequest.onHeadersReceived.addListener(details => {
  const headers = details.responseHeaders || [];
  const mime = (headers.find(h => h.name.toLowerCase() === "content-type")?.value || "").split(";")[0].trim().toLowerCase();
  saveCandidate(details.tabId, details.url, mime, details.type);
}, {urls: ["http://*/*", "https://*/*"]}, ["responseHeaders"]);

// Resource Timing is a fallback for MSE fetch/XHR requests that Chrome does not
// expose as type "media" in the webRequest event.
// Chrome 155 起 downloads.onCreated 的 item 不再带 tabId，按标签页分别记录点击会永远匹配不上。
// 因此点击记录改为全局列表，靠"真实用户点击 + 8 秒时间窗"保证只接管用户刚点击后触发的下载。
// 记录范围覆盖链接与按钮（含 fwlink 式跳转链接和 JS 合成下载），URL 可为空（按钮没有 href）。
function recordClickedDownload(url, page) {
  const entry = {url: typeof url === "string" ? url : "", time: Date.now()};
  if (typeof page === "string" && /^https?:/i.test(page)) entry.page = page;
  chrome.storage.session.get({clickedDownloadFeed: []}, state => {
    const recent = (state.clickedDownloadFeed || []).filter(item => Date.now() - item.time < 8000);
    recent.unshift(entry);
    chrome.storage.session.set({clickedDownloadFeed: recent.slice(0, 10)});
  });
}

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.type === "mediaResourceCandidate" && sender.tab) {
    saveCandidate(sender.tab.id, message.url, "", "resource");
  }
  if (message?.type === "userClickedDownloadLink") {
    recordClickedDownload(message.url, message.page);
  }
});

chrome.downloads.onCreated.addListener(async item => {
  if (item.byExtensionId === chrome.runtime.id || !/^https?:\/\//i.test(item.url)) return;
  // 读取默认值与 popup 保持一致（true），避免 onInstalled 尚未写入时开关显示开、实际关。
  chrome.storage.local.get({autoTakeover: true}, async ({autoTakeover}) => {
    if (!autoTakeover) return;
    const {clickedDownloadFeed: feed = []} = await chrome.storage.session.get({clickedDownloadFeed: []});
    const now = Date.now();
    const recent = (feed || []).filter(entry => now - entry.time < 8000);
    // Exact URLs handle ordinary links; the single pending-click fallback
    // handles redirector links (go.microsoft.com/fwlink etc.) and button-triggered
    // downloads, where the clicked URL differs from Chrome's final item URL.
    let match = recent.findIndex(entry => entry.url && entry.url === item.url);
    if (match < 0 && recent.length) match = 0;
    if (match < 0) {
      // 没有可接管的点击：顺手清理已过期的点击记录，避免 storage.session 堆积无效条目。
      if (recent.length !== (feed || []).length) {
        await chrome.storage.session.set({clickedDownloadFeed: recent});
      }
      return;
    }
    const clicked = recent[match];
    recent.splice(match, 1);
    await chrome.storage.session.set({clickedDownloadFeed: recent});
    // 转发重定向后的最终文件地址：跳转链接（go.microsoft.com/fwlink 等）的 item.url 是
    // 跳转前 URL，直接拿来命名会存成 "fwlink" 这类无扩展名文件；finalUrl 才是真实文件地址。
    // 匹配仍用 item.url（与点击记录的 URL 一致），仅转发时替换为 finalUrl。
    let target = item.url;
    if (typeof item.finalUrl === "string" && /^https?:\/\//i.test(item.finalUrl)) target = item.finalUrl;
    // 转发浏览器 UA 和 Referer，兼容校验 UA 或防盗链的文件服务器；不转发 Cookie。
    // Referer 优先用 Chrome 实际请求下载时用的（跨域重定向后可能是重定向站 origin），
    // 缺失时退回点击所在页面 URL。
    const headers = {"User-Agent": navigator.userAgent};
    const referrer = item.referrer || clicked.page || "";
    if (referrer) headers["Referer"] = referrer;
    try {
      const response = await fetch("http://127.0.0.1:18765/api/direct-download", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({url: target, headers}),
      });
      if (!response.ok) return;
      chrome.downloads.cancel(item.id);
    } catch {
      // If the app is not running or declines the link, leave Chrome's download alone.
    }
  });
});

chrome.tabs.onRemoved.addListener(tabId => chrome.storage.session.remove([String(tabId), `requestHeaders:${tabId}`]));
// Do not carry signed playback URLs over a page reload/navigation.
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === "loading") clearTabMedia(tabId);
});
