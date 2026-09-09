// popup.js
// ポップアップを開いたとき background から captures 一覧を取得して描画する。
// 各行の「保存」ボタンで再ダウンロードが可能。

const badge = document.getElementById("badge");
const empty = document.getElementById("empty");
const list = document.getElementById("list");

// ポップアップが開いている間、ダウンロード済みファイル名を保持する
const downloadedSet = new Set();

/**
 * キャプチャ 1 件分の DOM 要素を生成する。
 * innerHTML を使わず createElement + textContent で組み立てることで XSS を防ぐ。
 */
function createItem(filename, fullText) {
  const alreadyDownloaded = downloadedSet.has(filename);

  // 改行を除去して冒頭 24 文字をプレビュー表示（切り捨てた場合は … を付加）
  const normalized = (fullText ?? "").replace(/\s+/g, " ");
  const preview =
    normalized.length > 24 ? normalized.slice(0, 24) + "…" : normalized;

  const item = document.createElement("div");
  item.className = "item";

  const info = document.createElement("div");
  info.className = "item-info";

  const filenameEl = document.createElement("div");
  filenameEl.className = "item-filename";
  filenameEl.title = filename;
  filenameEl.textContent = filename;

  const previewEl = document.createElement("div");
  previewEl.className = "item-time";
  previewEl.textContent = preview;

  info.appendChild(filenameEl);
  info.appendChild(previewEl);

  const btn = document.createElement("button");
  btn.className = "btn-dl";
  btn.textContent = alreadyDownloaded ? "✓" : "保存";
  btn.addEventListener("click", () => {
    btn.textContent = "…";
    btn.disabled = true;

    chrome.runtime.sendMessage({ type: "DOWNLOAD", filename }, () => {
      downloadedSet.add(filename);
      btn.textContent = "✓";
      btn.disabled = false;
    });
  });

  item.appendChild(info);
  item.appendChild(btn);

  return item;
}

/**
 * キャプチャ一覧を描画する
 */
function render(captures) {
  badge.textContent = captures.length;

  if (captures.length === 0) {
    empty.hidden = false;
    list.hidden = true;
    return;
  }

  empty.hidden = true;
  list.hidden = false;

  // 前回の描画内容をクリア
  list.replaceChildren();

  for (const { filename, fullText } of captures) {
    list.appendChild(createItem(filename, fullText));
  }
}

// 初期ロード
chrome.runtime.sendMessage({ type: "GET_CAPTURES" }, (response) => {
  render(response?.captures ?? []);
});
