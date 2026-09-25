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
- `showdata/music/` … アップロードしたショーの音楽ファイル。
  **音源を差し替えたら Timeline タブの Simulator for designers… で作り直して演出家に渡す**
- `showdata/history.json` … Undo / Redo の履歴

**別の PC でも同じショーを使うには `showdata/` ごとコピーする**(演出だけなら Timeline の Save show… / Load show… の JSON でも移せる。音楽は別途)。 バックアップもこのフォルダを
丸ごと取ればよい。UI を起動したまま `showdata/files/` に CSV を置いても、次の再描画で拾う。

### サンプル演出(6 ルック通し)

`docs/samples/az27ss_sample_show.json` は 2026-09-24 に組んだ 6 ルック通しのサンプル(LOOK 23〜28、
19 キュー、遷移 6 種、機体は radxa-01〜06 に仮割り当て)。Timeline の **Load show…** で読み込める
(CSV は `showdata/files/` にあるものを参照するので、`showdata/` ごと移した先で使う)。デザイン CSV
がまだ無いルックに仮のデザイン(`*_color_sampleA/B_grid.csv`)を作るには、UI を起動したまま
`python tools/make_sample_grids.py . http://127.0.0.1:8765`(map から生成し、check を通してから取り込む)。

## 5b. 別の PC へ移す(移植)

コードは GitHub、ショーのデータは `showdata/` フォルダ、の 2 つを持っていけばよい。

1. 新しい PC に **Python 3.9 以降** を入れる(https://www.python.org/ 、インストーラで
   「Add python.exe to PATH」にチェック)。追加パッケージは不要
2. **Git** を入れて(https://git-scm.com/)、置きたい場所で:

   ```bash
   git clone https://github.com/Yoshi-Hirata/E-paper_H_WALL_BRICKS_Raspi.git
   ```

   Git を入れたくないときは GitHub のページの **Code → Download ZIP** を展開してもよい
   (その場合、以後の更新も ZIP を取り直す)
3. 元の PC で **`Backup showdata.bat`** をダブルクリック → リポジトリの隣に
   `showdata-YYYYMMDD-HHMM.zip` ができる(CSV・タイムライン・機体割り当て・音楽・履歴のすべて)。
   これを新しい PC のリポジトリ直下に展開して `showdata/` フォルダにする
   (`showdata/files/…` という階層になっていること)
4. `Start Conductor.bat` をダブルクリック → http://localhost:8765 が開き、Designs / Timeline に
   同じ内容が出る。Units タブは専用ルータにつなげば online になる(機体のアドレスは
   `192.168.51.101〜110` 固定で、設定不要)
5. コードを最新にするときはリポジトリのフォルダで `git pull`(黒いウィンドウを閉じてから
   `Start Conductor.bat` をやり直す)。`showdata/` は Git の管理外なので `git pull` で消えない

- 機体のアドレスを変えたいときだけ `showdata/fleet.json` を作る:
  `{"units": {"radxa-01": "192.168.51.101:8787", ...}}`(書いた機体だけ上書き)
- 別 PC で開発・テストもするなら `pip install pytest` のうえ `python -m pytest -q`(約 3 分)

## 6. ショー当日の順番(要点)

> **タイムラインを機体に書き込む道は 2 つある。** どちらも Timeline タブの
> **Write to units…**(Units タブの **④ Save on units** ・「PLAY WITHOUT THIS PC」
> カードのボタンでも同じ)から選ぶ:
>
> - **A = Upload for the show** … **ショー当日はこちら**。いま全部の絵を基板へ焼き込み、
>   タイムラインを機体へ渡す。以後この PC が START / HOLD / NEXT / STOP を出す
>   (= 下の ① Upload と同じ動作)
> - **B = Save on the units** … **PC なしで流したいときはこちら**(§7)。各機体自身の
>   メニューに名前つきで保存し、機体が KEY1 で再生する
>
> どちらもダイアログ上部の **WHICH LOOKs** のラジオで「All LOOKs」(既定)か LOOK 1 つを
> 選べる。1 つだけ選ぶとその LOOK の機体にしか書かないので、**START はタイムラインの
> 全機体が同じ Upload を持っていることを要求する** - 1 ルックの Upload は確認用、
> 本番前には必ず All LOOKs でもう一度 Upload すること。
>
> いまタイムラインが機体に入っているかは、Timeline の「THE LOOKS AT」バーと
> Units タブの「THE SHOW」見出しのチップ(`uploaded 10 / 10 · up to date` /
> `demo "PARIS SS26" 10 / 10 · changed since`)で分かる。

1. PC を専用ルータにつなぐ → `Start Conductor.bat`
2. Units タブで 10 台が online、時計精度(±ms)が出ていることを確認
3. **① Upload**(ショーを全機体へ配布 = 各キューの絵を基板のスロットへ焼き込む)→ Units タブの
   「THE SHOW」カードが **Pictures written on n / n units** になるまで待つ(**36 基板 × 10 キューで
   約 112 秒、18 キューなら約 195 秒 = 3 分半**。各機体タイルの **Pictures** 行に `writing n / N` の
   進み具合が出る)→ **② Show preset**(開始前の絵を出す)→ **③ START**
   - 中身の変わっていないショーをもう一度 Upload しても **1 枚も書かない**(約 0.02 秒で
     `written` に戻る)。時間がかかるのは初回と、キューの挿入・削除で slot 番号がずれたとき
   - 絵の無い(応答しない)基板は 1 枚あたり約 1.5 秒の待ちを 1 回だけ払う。機体の
     ログに `14 boards absent (3-16) - skipped` と出る
4. 本番中は HOLD / RESUME / NEXT / STOP
   - **Pictures 行の読み方**(この行が `written` でない機体があると ② ③ は機体名つきで断る):

     | Pictures 行 | 意味 | 操作 |
     |---|---|---|
     | `writing 12/48 (8 s)` | 焼き込み中 | 待つ。force では越えられない |
     | `written` | 全部の絵がスロットに入っている | そのまま ② ③ へ |
     | `✗ not written (cancelled: 理由) — Upload again` | 焼き込みが最後まで行かなかった(焼き込み中の STOP、機体がポートを取られた、ポートが無い) | **① Upload をやり直す**。force では越えられない |
     | `✗ not written since it restarted — Upload again` | Upload のあとに機体が再起動した | **① Upload をやり直す**。force では越えられない |
     | `✗ 10 of 12 pictures not written on boards 1, 2, 3` | 生きている基板が書き込みを拒否した(または不在) | その基板抜きで進めるなら ② ③ の確認ダイアログで「anyway」(`force`)。直すなら ① Upload |
     | `✗ none of its 16 boards answered` | その衣装の基板が 1 枚も答えない(電源が入っていない・ケーブルが抜けている) | 直すなら電源・ケーブル。**その 1 台を置いて他の 9 台で始められる**: ② ③ のダイアログで「anyway」(`force`)- その衣装はいま映っているものを映したまま |

   - **焼き込み中の STOP、焼き込み中の機体再起動 → ① Upload をやり直し、全タイルが written に
     なるまで待つ**
   - ショーが動いている最中でも ① Upload は押せる(確認ダイアログが出る)。1 台だけ絵を失った
     ときの復帰手段で、すでに同じショーを持っている機体は 1 枚も書かない。押した機体は
     いったんショーから外れ、次の監視ポーリング(数秒)で自動的に戻る
   - 機体が「run refused: …」で断ったときは、その理由が **その機体のタイルにも**出る
     (Corrected automatically の行だけでなく)
5. 終わったら黒いウィンドウを閉じる。機体は本体のメニューに戻る(Units タブの Release)
6. **PC と機体のコードは必ず一緒に更新する**。古いページは機体の `cancelled` / `none` を
   `written` のように見せてしまう(焼き込みの状態は 2026-09-25 に増えた)。`git pull` は
   ショー PC と 10 台すべてに

## 7. スタンドアローンでデモを流す(§6 の B)

展示会のブースなど、この PC を毎回持って行かなくても機体単体でショーの一部を
流したいとき。**ショー当日(A)は §6 の ① Upload の方**で、こちらではない。

1. **Write to units…** を押す(Timeline タブのツールバー、Units タブの
   **④ Save on units (plays without this PC)**、または「PLAY WITHOUT THIS PC
   (DEMO STORED ON THE UNITS)」カードのボタン。どれも同じダイアログ)
2. 右側の **Save on the units** に名前を入力(**A〜Z・0〜9・記号、最大 14 文字**。
   機体の画面は日本語を描けないので拒否される)。ループさせるなら「Loop」も。
   書けないときは理由がボタンの真上に出る(タイムラインの問題、**ショーが進行中**
   → 先に STOP、デモ再生中の機体がある、名前が空)
3. **Save on the units**(名前欄で Enter でも同じ)→ 機体ごとに ✓ written / FAILED が
   ダイアログに出る(オフラインの機体は FAILED。あとで online になってから改めて書き込む)。
   続けて機体側の手順も出る
4. 機体のジョイスティックでメニューを開き(**KEY2**)、STANDBY のすぐ下にできた行を選ぶ →
   **KEY1** で再生(**先に自分で絵を焼いてから**流れる。ダイアログに出ていた秒数ぶん待つ)。
   **KEY2** で停止してメニューへ戻る。**KEY1 長押し**で最初からやり直し
5. 書き込んだデモは各機体タイルの「**On unit**」行と「Demos on the units」の表に並ぶ
   (機体・名前・キュー数・長さ・ループの有無)。要らなくなったら表の **Delete** で機体から消す
6. デモの再生中はその機体だけ PC の Upload・START・SEEK・RESUME・NEXT の対象から
   外れ、「FAILED — playing a demo - press STOP first」と出る。Units タブの
   その機体の「Show」行に `demo: <名前>` と出ていたら、まず **STOP** を押す
   (機体のデモも一緒に終わる)。それから改めて Upload・START すればよい

タイムラインを直したあとに再度 Write すると、同じ名前の場所へ上書きされる
(見出しのチップが `demo "PARIS SS26" 10 / 10 · changed since` と出ていたら、
機体のデモはいまのタイムラインより前のもの。
「Demos on the units」の **Timeline** 列が `older` と出ていたら、その機体の
デモはいまのタイムラインより前のもの — 上書きするまで古い内容のまま流れる。
比較する材料が無い場合(このセッションでまだ Upload しておらず、かつタイムラインに
問題が残っている場合など)は `older` ではなく「—」と出る)。
