# X Post Capture

X（旧 Twitter）の個別投稿ページを開いたとき、内部 API `TweetResultByRestId` のレスポンスを自動でキャプチャする Chrome 拡張機能。
キャプチャした JSON はポップアップの「保存」ボタンでダウンロードできる。

## ファイル構成

```text
x-post-capture/
├── manifest.json   拡張機能の定義
├── content.js      MAIN world : fetch / XHR をフックしてレスポンスを取得
├── bridge.js       ISOLATED world : postMessage を chrome.runtime に中継
├── background.js   Service Worker : キャプチャ管理・ダウンロード処理
├── popup.html      ツールバーポップアップ UI
└── popup.js        ポップアップのロジック
```

## インストール手順

1. Chrome のアドレスバーに `chrome://extensions` と入力して開く
2. 右上の「デベロッパーモード」をオンにする
3. 「パッケージ化されていない拡張機能を読み込む」をクリック
4. `x-post-capture` ディレクトリを選択する

インストール後、ツールバーに拡張機能のアイコンが表示される。

## 使い方

1. Chrome で `https://x.com/<username>/status/<id>` を開く
2. ページが読み込まれると自動でキャプチャが走る
3. ツールバーのアイコンをクリックしてポップアップを開く
4. 一覧に表示されたキャプチャの「保存」ボタンを押してダウンロード

## ポップアップの見方

ツールバーアイコンをクリックすると、現在のセッションでキャプチャしたポストの一覧が表示される。

- ファイル名の下にツイート本文の冒頭がプレビュー表示される。
- 「保存」ボタンでダウンロード。ダウンロード済みのものは「✓」に変わる。
- 同一セッション中のキャプチャは最大 50 件保持される。超えた分は古いものから削除される。

ブラウザを閉じるとキャプチャは失われるため、必要なファイルはその都度ダウンロードして保存すること。

## 動作の仕組み

Chrome 拡張の `webRequest` API はレスポンス本文を読めない。
そのため `content.js` をページの JS コンテキスト（MAIN world）で動かして `window.fetch` と `XMLHttpRequest` をフックし、`TweetResultByRestId` を含む URL へのレスポンスを横取りする。
X のページが `window.fetch` を自前でラップして上書きする場合に備えて、`Object.defineProperty` でフックを書き換え不可にしている。

取得した JSON は次の流れで Service Worker に届く。

```text
content.js (MAIN world)
  → window.postMessage
  → bridge.js (ISOLATED world)
  → chrome.runtime.sendMessage（スリープ時は最大 3 回リトライ）
  → background.js (Service Worker)
  → chrome.storage.session に保存
```

MV3 の Service Worker では `URL.createObjectURL()` が使えないため、ダウンロード時は JSON を base64 エンコードして `data:` URL 形式に変換し、`chrome.downloads.download` に渡す。
ダウンロード先の選択ダイアログは表示されず、ブラウザの既定のダウンロードフォルダに直接保存される。

## 注意事項

- X の内部 API 仕様は予告なく変更される。キャプチャが動かなくなった場合は DevTools の Network タブで `TweetResultByRestId` のレスポンス構造を確認すること。
- `window.fetch` のフックはページの他の JS と競合する可能性がある。通常の閲覧に影響はないが、問題が発生した場合は拡張機能を無効化すること。
