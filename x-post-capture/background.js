// background.js (Service Worker)
// bridge.js から SAVE_RESPONSE メッセージを受け取り、
// キャプチャを chrome.storage.session に保持する。
// popup.js からの GET_CAPTURES / DOWNLOAD リクエストに応答する。

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "SAVE_RESPONSE") {
    handleSaveResponse(message).then(() => sendResponse({ ok: true }));
    return true; // 非同期レスポンスを使うため true を返す
  }

  if (message.type === "GET_CAPTURES") {
    getCaptures().then((captures) => sendResponse({ captures }));
    return true;
  }

  if (message.type === "DOWNLOAD") {
    downloadCapture(message.filename).then(() => sendResponse({ ok: true }));
    return true;
  }
});

/**
 * JSON から full_text を取り出す。
 * パース失敗時は空文字を返す。
 */
function extractFullText(json) {
  try {
    const data = JSON.parse(json);
    return data?.data?.tweetResult?.result?.legacy?.full_text ?? "";
  } catch {
    return "";
  }
}

/**
 * キャプチャを storage.session に保存する（ダウンロードはしない）。
 * fullText は保存時に抽出しておき、GET_CAPTURES のたびに再パースしない。
 */
async function handleSaveResponse({ filename, json }) {
  const { captures = [] } = await chrome.storage.session.get("captures");
  const entry = {
    filename,
    json,
    fullText: extractFullText(json),
    capturedAt: new Date().toISOString(),
  };

  // 同一ファイル名があれば上書き
  const idx = captures.findIndex((c) => c.filename === filename);
  if (idx >= 0) {
    captures[idx] = entry;
  } else {
    captures.unshift(entry);
    if (captures.length > 50) captures.length = 50;
  }
  await chrome.storage.session.set({ captures });
}

/**
 * storage.session のキャプチャ一覧を返す（json 本文は除いてメタ情報のみ）。
 * fullText は保存時に抽出済みのためここでは再パース不要。
 */
async function getCaptures() {
  const { captures = [] } = await chrome.storage.session.get("captures");
  return captures.map(({ filename, capturedAt, fullText }) => ({
    filename,
    capturedAt,
    fullText,
  }));
}

/**
 * 指定ファイル名のキャプチャを storage.session から取り出してダウンロードする。
 */
async function downloadCapture(filename) {
  const { captures = [] } = await chrome.storage.session.get("captures");
  const entry = captures.find((c) => c.filename === filename);
  if (!entry) return;
  await triggerDownload(entry.filename, entry.json);
}

/**
 * MV3 Service Worker では URL.createObjectURL() / Blob が使えないため、
 * JSON を base64 エンコードした data: URL を使って chrome.downloads.download を実行する。
 * unescape() は非推奨のため TextEncoder で UTF-8 バイト列に変換する。
 */
async function triggerDownload(filename, json) {
  const bytes = new TextEncoder().encode(json);
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  const base64 = btoa(binary);
  const url = `data:application/json;base64,${base64}`;

  await chrome.downloads.download({
    url,
    filename,
    saveAs: false,
    conflictAction: "overwrite",
  });
}
