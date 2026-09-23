# ショー UI(Conductor)の起動手順

ショー PC で動かす Web UI(http://localhost:8765)の起動・停止・困ったときの手順。
UI 本体の使い方は README の「Conductor」の節を参照。

## 1. 起動する(通常)

1. リポジトリ直下の **`Start Conductor.bat`** をダブルクリックする
2. 黒いウィンドウ(タイトル `E-paper Show Conductor`)が開き、続いて既定のブラウザで
   http://localhost:8765 が開く
3. **黒いウィンドウは閉じない**。閉じると UI は止まる(開始済みのショーは各機体が自走するので
   止まらない)

- もう一度ダブルクリックしても二重には起動しない(ページが開くだけ)
- ブラウザを閉じてしまったら、http://localhost:8765 を開き直せばよい(UI は動いたまま)
- ブラウザのブックマークに http://localhost:8765 を入れておくと早い

デスクトップにショートカットを置きたいときは、`Start Conductor.bat` を右クリック →
「ショートカットの作成」→ できたショートカットをデスクトップへ。

## 2. 停止する

黒いウィンドウを閉じる(または そのウィンドウで Ctrl+C)。

## 3. 前提

- Windows に Python 3.9 以降(このショー PC には 3.10 と 3.14 が入っている)
- 追加のインストールは不要(標準ライブラリだけで動く)
- ネットワークは **不要**。`localhost` はこの PC の中だけで完結するので、Wi-Fi やルータが
  変わっても UI 自体は開ける。機体(radxa-01〜10)との通信だけがネットワークに依存する

## 4. 開けないとき

| 症状 | 原因と対処 |
|---|---|
| ブラウザが「接続できません」 | UI が起動していない。`Start Conductor.bat` をダブルクリック |
| ダブルクリックしても黒いウィンドウがすぐ消える | 起動に失敗している。ウィンドウが消える前のメッセージを見る。残るときは `pause` で止まるので読める。多いのは Python 未検出(`Python 3.9 or newer was not found`)と、ポート 8765 を別のものが使っている(`cannot listen on port 8765`) |
| 黒いウィンドウに `already running` と出る | すでに起動している。ページが開くだけで正常 |
| Units タブが全部 offline | UI は正常。PC が機体と同じネットワーク(専用ルータ)にいない。つなぎ直せば自動で online に戻る。Designs / Timeline の編集はオフラインのままでできる |
| PC を再起動した・スリープから戻った | UI は起動し直す(`Start Conductor.bat`)。データは消えていない |

コマンドラインから起動するときは、リポジトリのフォルダで:

```bash
python -m conductor serve --open
```

別のポートで動かす(2 つ目を試すとき):`python -m conductor serve --port 8766 --workspace <別フォルダ>`

## 4b. ネットワーク

- 機体は固定 IP **`192.168.51.101〜110`**(radxa-NN = 51.(100+NN))、ゲートウェイ・DNS は
  `192.168.51.1`。**専用ルータの LAN は `192.168.51.1 / 255.255.255.0`** にし、DHCP の配布範囲は
  `101〜110` を避ける(例: `192.168.51.20〜99`)。PC は DHCP のままでよい
- `192.168.50.x` にしていない理由: 家庭用ルータや上流回線の既定と重なりやすく、トラベルルータは
  WAN 側と衝突すると LAN を勝手に別の番号に変える(2026-09-22 に発生)
- PC が別の Wi-Fi に切り替わっていたら、Wi-Fi 一覧からルータの SSID を選び直す
- Units タブが全部 offline のときは、まず PC のアドレスが `192.168.51.x` かを確認(`ipconfig`)

## 5. データの場所

すべて `showdata/` フォルダ(リポジトリ直下、Git の管理外):

- `showdata/files/` … 取り込んだ CSV(`LookNN_map.csv`、`LookNN_color_NAME_grid.csv`)
- `showdata/show.json` … タイムライン、遷移の設定、機体の割り当て、LOOK 番号と型番、基板番号の書き換え、音楽ファイル名
- `showdata/music/` … アップロードしたショーの音楽ファイル
- `showdata/history.json` … Undo / Redo の履歴

**別の PC でも同じショーを使うには `showdata/` ごとコピーする**(演出だけなら Timeline の Save show… / Load show… の JSON でも移せる。音楽は別途)。 バックアップもこのフォルダを
丸ごと取ればよい。UI を起動したまま `showdata/files/` に CSV を置いても、次の再描画で拾う。

## 6. ショー当日の順番(要点)

1. PC を専用ルータにつなぐ → `Start Conductor.bat`
2. Units タブで 10 台が online、時計精度(±ms)が出ていることを確認
3. **Upload**(ショーを全機体へ配布)→ **Show preset**(開始前の絵を出す)
4. 本番:**START**。途中は HOLD / RESUME / NEXT / STOP
5. 終わったら黒いウィンドウを閉じる。機体は本体のメニューに戻る(Units タブの Release)
