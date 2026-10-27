(() => {
  const filePattern = /\.(mp4|m4v|webm|mov|mp3|m4a|aac|ogg|opus|wav|m3u8|mpd|ts|m4s)(?:$|[?#])/i;
  const seen = new Set();
  // 接管判定：用户真实点击了链接或按钮后，短时间内出现的文件下载才转交下载器。
  // 不能只认带文件扩展名的 href——微软等站点用 fwlink 跳转链接（go.microsoft.com/fwlink/?LinkId=...），
  // 也有站点用 <button> 触发 location 跳转或合成 <a download>。这些都要记录，
  // 由 background 的"精确 URL 优先 + 最近点击兜底"来匹配，8 秒时间窗保证只接管刚发生的下载。
  const triggerSelector = "a[href],button,[role=button],input[type=button],input[type=submit]";
  function recordableHref(href) {
    let parsed;
    try { parsed = new URL(href, location.href); } catch { return ""; }
    if (!/^https?:$/.test(parsed.protocol)) return "";
    // 纯页内锚点（SPA 路由/滚动）不携带下载意图，仍按"有点击"记录但不带 URL。
    if (parsed.hash && parsed.origin === location.origin
      && parsed.pathname === location.pathname && !parsed.search) return "";
    return parsed.href;
  }
  document.addEventListener("click", event => {
    if (!event.isTrusted || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    let target = null;
    // composedPath 兼容 Shadow DOM 里的按钮/链接（event.target 会被重定向到 shadow host）。
    const path = typeof event.composedPath === "function" ? event.composedPath() : [];
    for (const node of path) {
      if (node && typeof node.matches === "function" && node.matches(triggerSelector)) { target = node; break; }
    }
    if (!target && typeof event.target?.closest === "function") target = event.target.closest(triggerSelector);
    if (!target) return;
    const url = target.matches("a[href]") ? recordableHref(target.href) : "";
    chrome.runtime.sendMessage({type: "userClickedDownloadLink", url, page: location.href});
  }, true);
  function inspect(entry) {
    const url = entry?.name;
    if (!url || seen.has(url)) return;
    let parsed;
    try { parsed = new URL(url); } catch { return; }
    const googleVideo = (parsed.hostname === "googlevideo.com" || parsed.hostname.endsWith(".googlevideo.com"))
      && /\/videoplayback$/i.test(parsed.pathname);
    if (!googleVideo && !filePattern.test(url)) return;
    seen.add(url);
    chrome.runtime.sendMessage({type: "mediaResourceCandidate", url});
  }
  try {
    new PerformanceObserver(list => list.getEntries().forEach(inspect)).observe({type: "resource", buffered: true});
  } catch {
    performance.getEntriesByType("resource").forEach(inspect);
  }
  performance.getEntriesByType("resource").forEach(inspect);
})();
