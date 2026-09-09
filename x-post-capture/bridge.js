// bridge.js
// ISOLATED world で動作。
// MAIN world の content.js が window.postMessage で送ってきたデータを受け取り、
// chrome.runtime.sendMessage で background.js に転送する。
//
// MV3 の Service Worker はアイドル時にスリープするため、
// 接続エラーが発生した場合は最大 3 回リトライする。
// リトライ対象は接続エラーのみ。それ以外は即座に報告して終了する。

window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  if (event.data?.type !== "__X_POST_CAPTURE__") return;

  const { filename, json } = event.data.payload ?? {};
  if (!filename || !json) return;

  sendWithRetry({ type: "SAVE_RESPONSE", filename, json }, 3);
});

/**
 * chrome.runtime.sendMessage を最大 maxRetries 回リトライする。
 *
 * リトライ対象: Service Worker のスリープに起因する接続エラー
 *   - "Could not establish connection"
 *   - "The message port closed"
 *
 * リトライ対象外: メッセージが大きすぎる場合など、リトライしても解消しないエラー。
 *   これらは即座に console.error で報告して終了する。
 */
async function sendWithRetry(message, maxRetries, attempt = 1) {
  try {
    await chrome.runtime.sendMessage(message);
  } catch (err) {
    const isConnectionError =
      err.message?.includes("Could not establish connection") ||
      err.message?.includes("The message port closed");

    if (!isConnectionError) {
      // リトライしても回復しないエラーは即座に報告
      console.error(
        "[X Post Capture bridge] sendMessage 失敗（リトライ対象外）:",
        err.message,
      );
      return;
    }

    if (attempt < maxRetries) {
      // Service Worker のウォームアップを待って再試行
      const delay = attempt * 200; // 200ms, 400ms
      console.log(
        `[X Post Capture bridge] Service Worker 復帰待ち (${attempt}/${maxRetries})、${delay}ms 後にリトライ`,
      );
      await sleep(delay);
      return sendWithRetry(message, maxRetries, attempt + 1);
    }

    // リトライ上限を超えた場合も error レベルで記録
    console.error(
      `[X Post Capture bridge] sendMessage 失敗、リトライ上限 (${maxRetries}) 超過:`,
      err.message,
    );
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
