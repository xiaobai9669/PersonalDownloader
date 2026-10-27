const root = document.getElementById("list");
document.getElementById("version").textContent = `v${chrome.runtime.getManifest().version}`;
chrome.tabs.query({active:true,currentWindow:true}, tabs => {
  const tab = tabs[0];
  const isYouTube = /^https?:\/\/(?:www\.)?(?:youtube\.com|youtu\.be)\//i.test(tab?.url || "");
  // YouTube watch/shorts/live/embed pages carry a video; youtu.be short links too.
  const isYouTubeVideoPage = /\/(?:watch|shorts|live|embed)\b|youtu\.be\//i.test(tab?.url || "");
  function isFresh(item) {
    if (!item.isGoogleVideo) return true;
    if (!Number.isFinite(item.time) || Date.now() - item.time > 10 * 60 * 1000) return false;
    try {
      const exp = Number(new URL(item.url).searchParams.get("expire"));
      if (exp > 0 && (exp < 1e12 ? exp * 1000 : exp) <= Date.now()) return false;
    } catch { return false; }
    return true;
  }
  async function sendMedia(item) {
    const pageUrl = tab.url || "";
    const mediaUrl = new URL(item.url);
    const itag = mediaUrl.searchParams.get("itag");
    const mime = decodeURIComponent(mediaUrl.searchParams.get("mime") || item.mime || "video/mp4");
    const extension = mime.includes("webm") ? "webm" : mime.startsWith("audio/") ? "m4a" : "mp4";
    const label = itag ? ` [itag-${itag}]` : "";
    const filename = `${tab.title || "视频"}${label}.${extension}`;
    const browserResponse = await fetch(item.url, {
      method: "GET", credentials: "include", cache: "no-store",
      referrer: pageUrl, referrerPolicy: "strict-origin-when-cross-origin"
    });
    if (!browserResponse.ok || !browserResponse.body) {
      throw new Error(`Chrome 请求当前视频流失败（HTTP ${browserResponse.status}）。请刷新视频页并重新播放后再试。`);
    }
    const headers = {
      "Content-Type": "application/octet-stream",
      "X-Source-Url": item.url,
      "X-File-Name": encodeURIComponent(filename)
    };
    const length = browserResponse.headers.get("content-length");
    if (length && /^\d+$/.test(length)) headers["X-Total-Bytes"] = length;
    const response = await fetch("http://127.0.0.1:18765/api/browser-stream", {
      method: "POST", headers, body: browserResponse.body, duplex: "half"
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }
  const takeover = document.getElementById("autoTakeover");
  chrome.storage.local.get({autoTakeover:true}, state => takeover.checked = state.autoTakeover);
  takeover.onchange = () => chrome.storage.local.set({autoTakeover:takeover.checked});
  document.getElementById("sendPage").onclick = async () => {
    const button = document.getElementById("sendPage");
    const originalText = button.textContent;
    button.disabled = true;
    const restore = () => {
      button.disabled = false;
      setTimeout(() => { if (!button.disabled) button.textContent = originalText; }, 6000);
    };
    try {
      const pageUrl = tab.url || "";
      if (isYouTube && !isYouTubeVideoPage) {
        button.textContent = "请在视频播放页使用";
        button.title = "请先打开具体的 YouTube 视频页（地址含 watch?v=、youtu.be/、/shorts/ 或 /live/），再点此按钮。";
        restore();
        return;
      }
      // 发送页面 URL，由下载器的 yt-dlp 解析完整格式并合并音视频（支持 yt-dlp 已知的数百个站点）。
      // 个别情况下 yt-dlp 解析会被登录/验证拦截，此时可在下方媒体列表对单条媒体流使用"发送此流"。
      const response = await fetch("http://127.0.0.1:18765/api/tasks", {
        method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({url: pageUrl})
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      button.textContent = isYouTube
        ? "已发送视频页：下载器解析中（在下载器中开始任务）"
        : "已发送，下载器中已添加任务";
      button.title = isYouTube
        ? "下载器会解析最高画质并自动合并音视频。若解析失败（登录或验证），请播放视频后在下方媒体列表对单条播放流使用“发送此流”。"
        : "";
      restore();
    } catch (error) {
      button.textContent = "发送失败：请先启动下载器";
      button.title = error.message;
      restore();
    }
  };
  document.getElementById("copyPage").onclick = async () => {
    await navigator.clipboard.writeText(tab.url || "");
    document.getElementById("copyPage").textContent = "页面链接已复制，请粘贴到下载器";
  };
  chrome.storage.session.get({[String(tab.id)]:[]}, state => {
    const allItems = state[String(tab.id)] || [];
    const items = allItems.filter(isFresh);
    if (!items.length) {
      const message = allItems.length
        ? "已捕获的临时媒体流过期了。<br>刷新视频页、播放几秒后再打开扩展。"
        : "暂未发现媒体请求。<br>播放或加载网页中的视频后再打开此面板。";
      root.innerHTML = `<div class="empty">${message}</div>`;
      return;
    }
    for (const item of items) {
      const box = document.createElement("div"); box.className = "item";
      const meta = document.createElement("div"); meta.className = "meta";
      const type = document.createElement("span"); type.textContent = `${item.kind || "媒体"} · ${item.mime || "未知格式"}`;
      const actions = document.createElement("span"); actions.className = "actions";
      const button = document.createElement("button"); button.className = "copy"; button.textContent = "复制地址";
      button.onclick = async () => { await navigator.clipboard.writeText(item.url); button.textContent = "已复制"; setTimeout(()=>button.textContent="复制地址",1200); };
      actions.append(button);
      const send = document.createElement("button"); send.className = "sendMedia"; send.textContent = "发送此流";
      send.title = "直接下载这一条媒体流；部分格式可能仅含音频或仅含视频";
      send.onclick = async () => {
        send.disabled = true;
        try {
          await sendMedia(item);
          send.textContent = "已发送";
        } catch (error) {
          send.textContent = "发送失败";
          send.title = error.message;
        } finally { send.disabled = false; }
      };
      actions.append(send);
      meta.append(type, actions);
      const url = document.createElement("div"); url.className = "url"; url.textContent = item.url;
      box.append(meta,url); root.append(box);
    }
    document.getElementById("copyall").onclick = async () => {
      await navigator.clipboard.writeText(items.map(i=>i.url).join("\n"));
      document.getElementById("copyall").textContent = "已复制全部链接";
    };
  });
});
