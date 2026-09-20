# 東京都822施設｜GitHub無料更新

対象：特養590・老健195・介護医療院37、合計822施設。

## 仕組み

- 取得元：厚生労働省「介護サービス情報公表システム」東京都
- GitHub Actions + Chromium（Playwright）で取得
- 50施設ごとに結果をGitHubへ保存
- 822施設まで自動継続
- 途中で失敗しても、それ以前の50件単位のコミットは残る
- 毎日 04:00 JST に自動実行
- GitHub画面の Actions → `Tokyo 822 official data free update` → `Run workflow` でも手動実行可能

## 出力

- `docs/tokyo_822_latest.json`
- `docs/tokyo_822_latest.csv`

Netlify側は `docs/tokyo_822_latest.json` のRaw URLを読み込む構成にすれば、GAS課金なしで表示へ反映できる。

## 重要

公式URLが確認できない6施設は `skipped_no_official_url` として保持し、推測でURLを作らない。
公式ページに空床数・待機者数が掲載されていない場合も推測で埋めず空欄にする。
