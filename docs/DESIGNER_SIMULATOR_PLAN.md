# 演出家チーム向けシミュレーター — 実現性検討と仕様案(2026-09-24)

**→ 決定(2026-09-24): 方式 A を採用。bundle は zip ではなく JSON(CSV 本文を埋め込み、
音楽は名前のみ)。**

## 0. 要求(依頼者の言葉)

- 配線ナビ(vglabjp.synology.me)から吐き出した CSV を読み込めること
- refresh パターン(Top to Bottom など)を選択できること
- Windows / macOS のどちらでも配布しやすく簡便に使えること
- Timeline ページと相関性があり、シミュレーターで決めた演出をサーバー UI でも Load できること

演出家チームは「ドレスの色パターン(design CSV)とタイムコードに合わせたショーの流れ」を作る。
機体(radxa)や RS-485 とは無関係で、必要なのは **見た目のシミュレーションと演出ファイルの受け渡し**。

## 1. いま持っている資産

| 資産 | 場所 | 演出家用に流用できるか |
|---|---|---|
| CSV の取り込み(map / design、配線ナビの命名規則そのまま) | `conductor/server.py` `Workspace.save` / `conductor/look.py` | そのまま |
| 遷移 6 種(ソケット順・図心・上→下・下→上・客席左→右・右→左)とデザイン単位/キュー単位の設定 | `conductor/sequence.py`、Designs / Timeline ページ | そのまま |
| タイムライン(Start / Refresh / End、キューごとの書き換え時間、機体ごとの最短間隔の検証) | `conductor/timeline.py` | そのまま(機体割り当てが無い項目は「(型番)」という仮のバスで検証される) |
| 再生シミュレーター(6 ルック 1 行、書き換え中のチカチカ、遷移の波、音楽同期、シーク) | `conductor/web/index.html` | そのまま |
| 演出の保存/復元(`epaper-show` JSON、Save show… / Load show…) | `/api/show/export` `/api/show/import` | そのまま = **サーバー UI との相関性はこれで担保** |
| 起動: Python 3.9+ の標準ライブラリだけ、`127.0.0.1` のみ待ち受け | `Start Conductor.bat` | Windows は済み。macOS は同じコマンドで動く(Windows 固有コードなし) |

つまり「演出家用シミュレーター」は、機能的には **Conductor の Designs + Timeline タブそのもの**である。
足りないのは (a) 演出家向けの見た目・配布形態、(b) CSV と音楽も含めた**まるごとの受け渡し**、の 2 点。

## 2. 実現方式の比較

| 方式 | 中身 | 配布・起動 | 開発量 | リスク |
|---|---|---|---|---|
| **B. Conductor の「Designer モード」**(推奨) | 同じサーバー・同じページ。Units タブと機体まわりを隠し、演出家に要る物だけ出す | zip 1 個(リポジトリ)+ Windows は `Start Conductor.bat`、Mac は `Start Simulator.command`。**Python 3 が必要**(Mac は python.org のインストーラ 1 回、または Xcode CLT) | 小(1〜2 日) | Python のインストールを演出家に頼む点。ロジックの重複はゼロなので、サーバー UI と結果が食い違う心配がない |
| A. 単一 HTML ファイル(サーバー不要) | CSV 解析・遷移の順位計算・タイムライン検証・エクスポートを JS で再実装 | .html を 1 個渡してダブルクリック。**インストール不要**、Mac/Win/iPad でも開く | 中〜大(3〜5 日)+ 以後の二重保守 | Python 側と JS 側の**計算の食い違い**(遷移の順位、最短間隔、完了時刻)。Python の結果を正解とする照合テストで抑えるが、機能追加のたびに両方を直す |
| C. Pyodide(ブラウザ内 Python)で単一 HTML | Python のモジュールをそのままブラウザで実行 | HTML 1 個だが 10 MB 超のランタイムを毎回読み込む(オフライン配布は可) | 中 | 起動が遅い(数秒〜十数秒)、標準の `http.server` 構造は使えず改修が要る |
| D. Electron / Tauri でアプリ化 | B を包む | .app / .exe。署名なしだと Mac の Gatekeeper で警告 | 大(ビルド環境・署名) | 保守コストが最も高い |

**推奨: B を先に出し、演出家側で「Python を入れるのが無理」となったときだけ A を検討する。**
理由: 相関性(同じ計算・同じファイル形式)が要求の中で最も重く、B はそれを構造的に満たす。
Mac の Python は python.org の pkg を 1 回入れるだけで、演出家チームに 1 人でも PC 担当がいれば済む。

## 3. 仕様案(方式 B: Designer モード)

### 3.1 起動と画面

- `python -m conductor serve --designer --open`(ワークスペース既定 `showdata/`、ポート 8765)。
- `Start Simulator.command`(Mac)/ `Start Simulator.bat`(Win)を同梱。Mac の `.command` はダブルクリックで Terminal が開く。
  zip 展開後に実行権限が落ちていた場合の代替手順(Terminal で `python3 -m conductor serve --designer --open`)も README に書く。
- Designer モードでは:
  - **Units タブを出さない**。機体の割り当て(radxa-NN)の欄も出さない。項目はルック番号と型番だけで扱う。
  - 検証は「同じ衣装の書き換えが重なっていないか」「同じルックの Tops/Skirt が同時に書き換わっていないか」など、
    機体を知らなくても言えることに限る。最短間隔は「1 衣装 = 1 機体」と仮定して計算(本番で複数衣装を 1 機体に載せるかは操作者側の判断)。
  - ヘッダに「Designer」表示。Undo/Redo・Save/Load・音楽・シミュレーターはそのまま。
  - LOOK 番号・型番の編集(既存)は残す。演出家が並び順を決められる。

### 3.2 CSV の扱い

- 配線ナビの **map CSV**(`<型番>_map.csv`、shift 列つき)と **design CSV**(`<型番>_color_<名前>_grid.csv`)をドラッグ&ドロップで取り込む(既存)。
- 取り込み時に、map と design の整合(穴の数、shift の一致)を検証して問題を表示(既存の CHECK)。
- 配線ナビからの再エクスポートで同名ファイルが来たら上書き(既存)。

### 3.3 演出(遷移)の設定

- デザインごとの遷移(順序 6 種+秒数)を Designs タブと EDIT CUE のどちらからでも設定(既存、同期済み)。
- キューごとの上書き(this cue only)も既存のまま。

### 3.4 タイムラインと音楽

- Start / Refresh / End、キューごとの書き換え時間、ショー長、音楽のアップロードと同期再生、赤い再生ヘッドのドラッグ(既存)。
- **タイムコード入力**: 既存の m:ss 入力に加え、`h:mm:ss.f` と **タイムコード貼り付け(hh:mm:ss:ff、29.97/25/30 fps 選択)**を受け付ける。
  演出台本がタイムコードで来るため。内部は秒(既存)のまま。
- 新規: **キュー一覧の CSV 書き出し**(Start / Complete / End / LOOK / デザイン / 遷移)。台本との突き合わせ用。

### 3.5 受け渡し(サーバー UI との相関)

- 既存の Save show… / Load show…(`epaper-show` JSON)を土台に、**「Save bundle…」/「Load bundle…」**を追加する。
  bundle = zip 1 個: `show.json`(既存形式)+ `files/*.csv`(map と design)+ `music/<file>`(任意)+ `bundle.json`(作成日時、作成者、Designer モードの版)。
- 操作者側(本番 Conductor)の **Load bundle…** は:
  1. CSV を `showdata/files/` に追加(同名は上書き。削除はしない)
  2. `show.json` の cues / transitions / labels / boards を取り込む(既存 import と同じ、Undo 1 手)
  3. **機体の割り当て(units)は操作者側の現在値を保持**(bundle に units が無い、または空のとき)。演出家は機体を知らないため
  4. 音楽があれば `showdata/music/` に置き換え
  5. 結果を一覧表示(追加した CSV、取り込んだキュー数、警告)
- 逆方向(操作者 → 演出家)も同じ bundle で渡せる(Save bundle… は本番側にも付く)。
- 実装は zip の読み書きに Python 標準の `zipfile` を使う(依存追加なし)。ページ側は既存のアップロード経路(`/api/show/import` 相当)に bundle 用の `/api/bundle/import`(multipart ではなく生 POST、既存の音楽アップロードと同じ方式)を足す。

### 3.6 配布物

- `E-paper_H_WALL_BRICKS_Raspi-designer-YYYYMMDD.zip`: リポジトリ一式(conductor/、docs/、`Start Simulator.command`、`Start Simulator.bat`、`README_DESIGNER.md`)。GitHub の Code → Download ZIP でも同じ。
- `README_DESIGNER.md`(日本語、1 ページ): Python の入れ方(Win: python.org、Mac: python.org の pkg。「Add to PATH」)、起動、CSV の入れ方、演出の作り方、bundle の出し方、困ったとき。
- 演出家側の showdata は空で始める(サンプル演出 `docs/samples/az27ss_sample_show.json` と、配線ナビから落とした CSV を入れれば本番と同じ 6 ルックが出る)。

### 3.7 テスト・受け入れ

- Designer モードで Units タブが無く、機体関連の API を叩かない(fleet を起動しない: `--designer` では `Fleet` を作らず、`/api/fleet` は 404)。
- bundle の往復: Designer で作成 → 本番で Load → キュー・遷移・ラベル一致、units は本番側の値のまま、CSV が追加されている。
- タイムコード入力の変換(29.97 drop-frame は使わない前提。必要なら明記)。
- macOS での起動確認(手元に Mac があれば実機、無ければ `python3 -m conductor serve` が stdlib のみで動くことのテストで代替)。

## 4. 方式 A(単一 HTML)を選ぶ場合の要点(参考)

- JS に移植する Python: `look.py`(CSV 解析・check)、`sequence.py`(ranks / span)、`timeline.py`(times / validate / min_interval)。
  いずれも純粋関数で 700 行程度。Python 側の出力を JSON 化した**ゴールデンファイル**を使い、JS 実装が同じ答えを出すテストを `tests/` に置く。
- ページ本体は `conductor/web/index.html` を分割して共通部分(描画・シミュレーター)を両方から使う。
- 保存: ブラウザのダウンロードで bundle(zip は JSZip を同梱)。読み込み: File API。
- 配布: `simulator.html` 1 個。ブラウザの制約で音楽は毎回開き直し(ファイルの記憶はできない)。

## 5. 見積りと進め方

| 段階 | 内容 | 目安 |
|---|---|---|
| 1 | `--designer` フラグ、Units タブと機体欄の非表示、fleet を起動しない、Mac/Win ランチャー、README_DESIGNER | 0.5 日 |
| 2 | bundle の Save/Load(サーバー・ページ・テスト)、units 保持ルール | 0.5〜1 日 |
| 3 | タイムコード入力、キュー一覧 CSV 書き出し | 0.5 日 |
| 4 | 演出家チームでの試用 → フィードバック。Python の導入が障害なら方式 A を判断 | — |

各段階とも、コーダー → 敵対的・好意的レビュー → 統合の手順で進める。

## 6. 確認したいこと

1. 演出家チームの PC に Python 3 を入れてもらえるか(Mac: python.org の pkg 1 回)。不可なら最初から方式 A。
2. タイムコードの形式(hh:mm:ss:ff の fps。ドロップフレームの有無)。
3. 演出家側で機体の割り当てを一切見せない、でよいか(見せると誤解の元になるため隠す案)。
4. 演出家に渡す初期データ: 配線ナビの CSV 7 種と、いまのサンプル演出でよいか。
