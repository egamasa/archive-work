// content.js
// world: MAIN で動作するため、ページの fetch を直接フックできる。
// TweetResultByRestId レスポンスをキャプチャして bridge.js に渡す。
//
// X のページが window.fetch を再ラップして上書きする場合に備えて、
// Object.defineProperty でフックした fetch を書き換え不可にする。
// さらに XHR も同時にフックして取りこぼしを防ぐ。

(function () {
  "use strict";

  const TARGET_URL_PATTERN = "TweetResultByRestId";

  // ---- fetch フック ----

  const originalFetch = window.fetch.bind(window);

  async function hookedFetch(...args) {
    const response = await originalFetch(...args);

    const url = typeof args[0] === "string" ? args[0] : (args[0]?.url ?? "");
    if (url.includes(TARGET_URL_PATTERN)) {
      processResponse(url, response.clone());
    }

    return response;
  }

  // 書き換え不可・列挙不可にして X のコードに上書きされないようにする
  Object.defineProperty(window, "fetch", {
    value: hookedFetch,
    writable: false,
    configurable: false,
  });

  // ---- XHR フック（fetch で取れない場合の保険）----
  //
  // コンストラクタ関数で別オブジェクトを return するパターンは
  // prototype の設定が無意味になるため、class 継承で書き直す。
  // open() をオーバーライドして URL を記録し、load イベントで処理する。

  const OriginalXHR = window.XMLHttpRequest;

  class HookedXHR extends OriginalXHR {
    #captureUrl = "";

    open(method, url, ...rest) {
      this.#captureUrl = url;
      return super.open(method, url, ...rest);
    }

    constructor() {
      super();
      this.addEventListener("load", () => {
        if (!this.#captureUrl.includes(TARGET_URL_PATTERN)) return;
        try {
          const data = JSON.parse(this.responseText);
          processData(data);
        } catch {
          // レスポンスが JSON でない場合は無視
        }
      });
    }
  }

  Object.defineProperty(window, "XMLHttpRequest", {
    value: HookedXHR,
    writable: false,
    configurable: false,
  });

  // ---- 共通処理 ----

  function processResponse(url, clonedResponse) {
    clonedResponse
      .json()
      .then(processData)
      .catch((err) => {
        console.warn("[X Post Capture] レスポンスの解析に失敗しました", err);
      });
  }

  function processData(data) {
    const result = data?.data?.tweetResult?.result;
    if (!result) return;

    const userResult = result?.core?.user_results?.result;
    const screenName = userResult?.core?.screen_name;
    const tweetId = result?.rest_id ?? result?.legacy?.id_str;

    if (!screenName || !tweetId) {
      console.warn(
        "[X Post Capture] screen_name または id が取得できませんでした",
        {
          screenName,
          tweetId,
          userResultKeys: userResult ? Object.keys(userResult) : null,
        },
      );
      return;
    }

    const filename = `${screenName}-${tweetId}.json`;

    window.postMessage(
      {
        type: "__X_POST_CAPTURE__",
        payload: {
          filename,
          json: JSON.stringify(data, null, 2),
        },
      },
      "*",
    );

    console.log(`[X Post Capture] キャプチャ完了: ${filename}`);
  }
})();
