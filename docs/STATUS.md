# 現在地と再開手順

最終更新: 2026-09-26

**この文書は「次に何をするか」だけを書く。** 経緯は
[DEVELOPMENT.md](DEVELOPMENT.md)、20 枚構成の検討は [SCALING.md](SCALING.md)、
手順は [README](../README.md) を見ること。

## 1. いま動いているもの

| 機体 | 役割 | 状態 |
|---|---|---|
| **Radxa Cubie A7Z × 10** `radxa-01`〜`radxa-10` = `radxa@192.168.50.101`〜`.110` | **本番機 10 台**。01 がゴールデンイメージ元、02〜10 はそのクローン(2026-09-16 全台起動確認済み)。01 に本番制御基板 2 枚(ID:1, ID:20)を接続 | 全台 `epaper-ui` 有効・自動起動、同一コミット |
| Raspberry Pi Zero 2 W `r2@192.168.50.25` | 予備機。パネル未接続 | `epaper-ui` active。ポート待ちのまま待機 |

- 両機とも同じコードで、差分は `ui/boards.py` のプロファイルのみ
- テスト 246 件(Windows で全通過、Radxa の Python 3.9 でも全通過 2026-09-20)

## 2. 直近で完成したもの

**スイープの実機確認(radxa-01、生基板 3 枚、2026-09-26)と、そこで出た
指摘の修正(F1/F2/F4/F6/F8)**

実機で確認できたこと(**方向の論理は正しかった**):

- 3 枚とも delay table の保存に成功。`sweep table saved` の socket 数は
  **60 / 50 / 60**。発火は `cue … fired slot N` が **+1〜2 ms**(要求した
  瞬間との差)
- `center`(中心から外へ)で全基板が `last starts +2.44 s` と記録した。
  span は 3.00 秒。**これは異常ではない** ― 1 着のスケールは複数の基板に
  またがっており、span を締めくくる「いちばん遠いスケール」は別の基板に
  載っているだけ。ただしログからはそれが読み取れなかったので、
  文言を変えた(下記)

直したもの:

- **F2(中)`center` の rank が 0 から始まっていなかった**。`center` は
  前面の重心からの距離を丸めて rank にするが、実機のマップでは重心が
  スケールとスケールの**あいだ**に落ちるため、いちばん近いスケールでも
  rank 1 で、rank 0 が存在しなかった。結果、衣装全体がトリガーより
  `span / max_rank` だけ遅れて始まり(3 秒 span で **+0.19 秒**)、
  スイープは span の残りしか使っていなかった。`ranks()` で最小 rank を
  引く(リベース)ようにした。行・列のシーケンスはもともと 0 から
  数えているので影響なし(テストで固定)
  - 副作用 1: **全スケールが中心から等距離の衣装はスイープ無しになる**
    (今までは「全部が span 秒だけ遅れる」= スイープではなくただの遅延)。
    `CenterTie` フィクスチャがまさにその形で、ゴールデンがそう言う
  - 副作用 2: リベースで max_rank が下がるところでは rank 間の刻みが
    変わるので、`center` のゴールデンは全部動く。**それが修正の中身**
  - `conductor/web/sim/model.js` にも同じ変更。ゴールデン再生成
    (**997 ケース / 21 フィクスチャ**、重心がスケールの間に落ちる
    `CenterGap` フィクスチャを追加)、ブラウザのセルフテスト **998/998**
- **F1(高、潜在)発火後のガード停止がスイープの中に落ちうる**。詳細は
  [SPECIFICATION 4.2](SPECIFICATION.md)。固定 12 秒(リフレッシュ 7 秒
  + 余裕 5 秒)だったのを、キューが申告する `span_s` / `refresh_s` から
  `発火 + refresh_s + span_s + 余裕 5 秒` で出すようにした。`/prepare` の
  ボディ(`conductor/server.py` の `compile_units`)に `span_s`・`refresh_s`
  を載せ、ショーファイルのキューはすでに両方を持っていたので
  `ShowPlayer` から `arm()` に渡すだけ。**申告の無い古いボディは従来の
  固定 12 秒**、かつ 12 秒はつねに下限(従来より早く出ることはない)。
  span の上限を別途縛る必要が無いのはこのため
- **F4(中低)スケールの無いソケットに 0 ではなく最終フレームを送る**。
  正しいファームウェアでは無視される値だが、0 は「T0 で描け」の意味
- **F8(低)シミュレーターのジッターがスイープの行を混ぜていた**。実機は
  等しい delay の scale が同じ 10 ms フレームで始まるのでジッターは
  ゼロ。3 秒 span では行の刻みが 0.14 秒しかないのに最大 0.35 秒の
  ジッターを足しており、約 2.5 行ぶん混ざっていた。スイープのあるキューは
  刻みの 0.3 倍で頭打ち(`SWEEP_JITTER_FRAC`)、ふつうのリフレッシュの
  ちらつきは従来どおり(こちらは実写に基づくパネル個体差のモデル)
- **F6(低)Units タブの MANUAL CUE** に「Prepare が使うのは**デザイン
  自身のトランジション**(Designs タブ)であって、タイムラインのキューの
  上書きではない」の 1 行
- ログの文言: `sweep table saved, 60 sockets, last starts +2.44 s of a
  3.00 s span - the farthest scales are on other boards`(span に届いて
  いる基板、および span を知らされていない呼び出しでは従来どおりの
  1 行)

**シミュレーター: 1 着ごとの「Add CSV」と、本番と同じ LOOK・型番の初期データ
(2026-09-25、依頼者の要望から)**

- **Designs タブの「DESIGNS OF THIS ITEM」に「Add CSV」**。本番の Conductor
  (`conductor/web/index.html` の `uploadOwn()`)と同じ動き ― 選んだ CSV は、別の型番の
  名前が付いていてもその 1 着の名前で保存され(トーストが旧名と新名を両方出す)、
  その 1 着だけのものになる。`*_map.csv` でも `*_color_名前_grid.csv` でもない名前は、
  ファイル名を挙げて拒否する。ヘッダーの「Add CSV」とフォルダのドロップはそのまま。
  `.filebtn`(中の `input` が `display:none` の `<label>`)は `tabindex`/`role="button"` を
  持ち、Enter と Space で開く(Timeline の Space=再生より先に捕まえる)
- **初期データのラベルが本番 `show.json` と一致**。`tools/make_starter.py` の
  `STARTER_LABELS` に LOOK 番号と型番を明記(`AZ271SD1305` → `LOOK 23 · AZ271SD1305`、
  LOOK 26 は Tops と Skirt の 2 着、バッグ 3 種は LOOK 番号を持たず型番だけで出る)。
  今までは `model` が空で「LOOK 23」としか出ていなかった。トラック名・キュー表・
  looks 行・EDIT CUE・`SHORTEST INTERVAL PER ITEM` が型番まで出すようになり、LOOK を
  持たない衣装は型番で名乗る(ファイル名の型番コードは出さない)
- **他の衣装のマップ CSV は 1 着ごとの「Add CSV」では受け付けない**(レビュー指摘):
  デザイン CSV は今まで通りその 1 着の名前に付け替えるが、**マップ(配線図)は
  付け替えない** ― 付け替えると、その衣装の配線がそのまま別の衣装のものに
  置き換わり(元のテキストは消える)、トーストは成功と表示し、数百件の CHECK 問題だけが
  手がかりになっていた。同じ 1 回の選択で同じ名前に化ける 2 ファイルも、2 つ目を
  両方の名前を挙げて断る
- **前に開いたことがある人にも新しいラベルが届く**(レビュー指摘):自動保存は
  初期データより優先されるので、一度開いた designer は古いラベル(型番が空)を
  持ち続け、戻す道が「Start a new (empty) project」しか無かった。保存されている
  プロジェクトの**型番が空の項目だけ**を初期データから埋める(入力済みの型番と
  LOOK 番号には触れない)
- 共有 LOOK の出し方(レビュー指摘):`LOOK 26 · AZ271SB2303 (Skirt)` ―― 型番コードでは
  なく**型番**で見分け、その下に同じ型番を二重に出さない。`SHORTEST INTERVAL PER ITEM`
  も同じ
- ドロップ・選択で CSV でないファイルは**ファイル名を挙げて断る**(レビュー指摘。
  今までヘッダー側は黙って捨てていたので、CSV が 1 階層下にあるフォルダを落としたときと
  区別が付かなかった)。長い一覧はトーストで 3 件 + 残数にまとめる
- テスト: `tests/test_designer_build.py` に 11 件追加(ヘッドレスで 1 着ごとのボタン、
  2 種類の拒否とリネーム、他衣装マップの拒否、同名衝突、LOOK 無し衣装の型番表示、
  Tops が Skirt の上、古い保存データの実起動、初期ラベル表と本番 `show.json` の一致、
  ラベル漏れなし)。全 762 件通過

**「Write to units…」に **WHICH LOOKs** - 書き込むルックを選ぶ(2026-09-25、依頼者の要望から)**

依頼者の要望:「WRITE TO UNITS のポップアップウィンドウ内で、書き込むルックを選択できる
ようにして。ラジオボタンをクリックして選択するでよい」。1 ルックだけ直したときに
全 10 台へ焼き直す必要は無い ― が、**そのぶん「全部入っているのか」を嘘にしない**のが
このラウンドの中身。

- **ダイアログ上部のラジオ 1 組**(2 つの選択肢の上):「**All LOOKs — n units**」(既定。
  ダイアログを開くたびにここへ戻る ― 1 時間前の 1 ルック選択が残っていることが、
  ショーが 1 台だけに出て行く道そのものなので)、続けてタイムラインが届くルックを
  LOOK 順に 1 行ずつ:
  `LOOK 26 · AZ271SC6302 (Tops) + AZ271SB2303 (Skirt) → radxa-01 · 5 pictures ≈ 2 s`。
  行の作り方は「THE LOOKS AT」と同じ `lookGroups()` ― **1 台に縫い付けた上下 2 着は 1 行**、
  2 台にまたがるルックは両方の機体名を出す。LOOK 番号を持たないもの(バッグ)は
  型番で 1 行。機体が割り当たっていない行は **`— no unit` で disabled** のまま並べる
  (隠さない ― ショーの一部であることは言う価値がある)。矢印キーで移動でき、
  disabled の行は飛ばす。Tab の閉じ込めはそのまま
- **選んだ行が下のすべてを絞る**: 送り先の一覧、`About N s — … on the slowest unit`、
  見出しの `… · 1 unit · LOOK 26 only`、実行後の結果と焼き込みの進み。
  **チップは fleet 全体のまま**(「いま画面にあるものは機体に入っているか」は、
  これから 1 ルックを書くかどうかで変わる問いではない)
- **選択時の一文**:Upload の下に「Only LOOK 26's unit is written. **START needs every unit
  of the timeline to hold this upload** — use this for checking one look, then Upload for
  all before the show.」、Save on the units の下に「Only LOOK 26's unit gets the demo;
  the other units keep theirs.」。ショー進行中の Upload の確認ダイアログは、
  **これから一瞬ショーを離れる機体の名前**を出す
- **API**: `POST /api/fleet/upload` ・ `/api/fleet/write_demo` に任意の **`units: [名前]`**
  (省略 = 全部 = これまでの全クライアントの動き)。名前はコンパイル済みのショーと
  照合し、**このタイムラインの機体でなければ 400 `radxa-07 is not a unit of this
  timeline`**(ページは見えているタイムラインから作るので、食い違い = 片方が古い。
  黙って落として「成功」と言うより止める)。`Fleet.upload(shows, force=, only=)` ・
  `write_demo(..., only=)` はその機体だけに POST し、**`self.shows` は置き換えではなく
  マージ**(残りの機体はまだ前の Upload で走っている ― 忘れると START と SEEK ごと失う)
- **部分的な書き込みは、fleet 全体を「書き込み済み」にしない**: ワークスペースの印を
  **機体ごと**にし(`Workspace.unit_marks`)、fleet 全体の印は**タイムラインの全機体が
  その版を持っているときだけ**立てる(それ以外は落とす)。`/api/fleet` の `timeline` に
  **`uploaded_units`** と **`demo_units`** を追加。チップはこの機体ごとの版を読むので、
  1 ルックの Upload は `uploaded 1/2`(判定語なし)、編集後に 1 ルックだけ焼き直せば
  `2/2 · changed since` になり、黙り込まない。ダイアログの機体一覧とタイルの Show 行に
  `· has this upload` / `· older upload`(`(older timeline)`)が出る。
  自分の版を持つデモは、**その機体の Upload が古いというだけでは古いと言わない**
- **敵対的レビューの指摘(F1-F8)を同ラウンドで修正**:
  - **F1(BLOCKER): 「START は全機体が同じ Upload を持っていることを要求する」を実装した**。
    書いただけでは嘘だった ― 機体の show id はこの PC が渡した id なので `_burn()` の
    id 照合は古い Upload の機体でも通り、2 つのタイムラインが同時に走っていた。
    `conductor/server.py` の `_one_timeline()` を **start と preset の直前**に置き、
    (a) タイムラインが必要とする機体で**一度も書いていない**ものがある、
    (b) 機体ごとの印が**食い違っている**、(c) 全機体が揃って**画面より古い**、のいずれでも
    400 で断る(`radxa-04 is not on this upload - Upload for All LOOKs before the show` /
    `every unit holds an older upload than the timeline on screen - Upload again before
    the show`)。**印を 1 つも持たない conductor(再起動直後)は断らない** ― 知らないことを
    理由に本番を止めない。**`force` は通す**(ページは焼き込み失敗と同じ作法で確認を出し、
    「はい」で force 再送)。必要な機体は `Workspace.timeline_units()` が show.json だけから
    出す(コンパイルは 10 機体 18 キューで秒単位、START の 3 秒リードには置けない)
  - **F2**: `upload(only=)` は、書かない機体が持っているショーと**長さが違う**ときに断る
    (`this timeline is 90 s long but radxa-02 still holds a 600 s one`)。長さは
    fleet 全体で 1 つの数(`show_duration()` は最大値)なので、混在すると SEEK/START が
    ある機体の終端より後ろを受け付けてしまう
  - **F3**: 部分 Upload の `self.shows` マージが、**タイムラインから消えた機体**を残していた
    (`_targets()` がそれを駆動し、START がショーに居ない機体へ /show/run を出す)。
    `shows` に無い機体は落とすように修正
  - **F4**: ページが送る機体名を「その LOOK に割り当てられた機体」から
    「**コンパイル済みショーに実際にある機体**」へ(キューの無い機体名を送ると 400 で
    1 台も書けなかった)。行には `(radxa-02: no cue yet)` と出す
  - **F5**: ショー進行中の 1 台救済 Upload が `run["force"]` を**全機体**に立てていた。
    救済した機体だけを `run["forced"]` に記録し、`/show/run` の force はその機体にだけ付く
  - **F6**: 1 ルックを書いたあとはラジオが **All LOOKs に戻り**、結果欄に
    「START refuses a fleet split over two uploads: Upload again with All LOOKs」と出る
  - **F7**: 機体ごとの印は増える一方だった ― タイムラインから消えた機体の印は
    `mark_written()` で落とし、`delete_demo` はその名前の印を忘れる
  - **N1**: この関門の解除は **`split_ok` 専用フィールド**にした。焼き込み失敗の
    `force` が両方の関門を一度に開けていたので、「基板が書けていないが start するか」に
    はいと答えただけで、**分裂したまま START できてしまっていた**。ページは該当する
    ぶんだけ質問し(焼き込み → `force`、分裂 → `split_ok`)、答えた分だけを送る
  - **N2**: そもそもコンパイルが通らないときは「Upload again」ではなく
    **「the timeline has problems - fix them on the Timeline tab, then Upload」**。
    直前の `compile_show()` の結果(版・problems・units)を `Workspace.compiled` に
    覚えておき、画面と同じ版のときだけ使う(START でコンパイルはしない)
  - **N3**: 救済 Upload の `run["forced"]` に入るのは**書き込みが成功した機体だけ**
  - **F8**: 行の所要時間は「on the slowest of them」、選択中も**書かない機体を
    `older upload` の印つきで表示**(それが F1 の警告そのもの)、結果の見出しは
    部分失敗でも LOOK 名を出す、1 台に複数ルックが載るときは
    「the unit's other looks ride along」
- **テスト: 723 件 + skip 1**(+8: server の units フィルタ 4 = 受理・400・部分 Upload が
  fleet の印を立てない・全体印が戻る、fleet の `only=` 3、ページの id と文言 1)。
  ブラウザ確認は偽機体 2 台(**127.0.0.1:19401/19402**)と 8786 の conductor で、
  LOOK 26 を選んで Upload → radxa-04 には 1 バイトも行かず、チップは `uploaded 1/2`

**演出家のシミュレーターに音源を埋め込む(2026-09-25、依頼者の指摘「音がなっていないようだ」から)**

原因は仕様どおりの動作だった:ブラウザは自分でディスク上のファイルを開けないので、
曲**名**しか知らないページは黙っているしかない。演出家が毎回 Pick file… で音源を選ばない限り、
`dist/az27ss-simulator.html` は無音のまま。**音源をファイルの中に入れる**以外に道は無い。

- **配布ファイルに音源を埋め込む**: `tools/build_designer.py --music PATH|auto|none`。
  `auto` はワークスペース(既定 `./showdata`)の `show.json` が指す音源。
  `SIM.embeddedMusic = {name, type, size, dataUrl}` という `<script>` 1 個を他のモジュールより
  前に差し込む。ページ側は起動時に 1 度だけ Blob 化(base64 文字列は捨てる)、
  最初の Play は利用者のクリックなので autoplay 制限にも当たらない
- **「今後音源が変更となる可能性」への答えは Conductor 側のボタン**: Timeline ツールバーの
  **Simulator for designers…**(`GET /api/simulator?music=1`)が、そのとき読み込まれている音源で
  ページをサーバ内で組み立てて
  `az27ss-simulator-YYYYMMDD-with-music.html` としてダウンロードさせる。
  **音源を差し替えたら、このボタンをもう 1 回押して渡し直すだけ**(開発者もチェックアウトも不要。
  音源の 名前/サイズ/mtime をキーにキャッシュするので 2 回目は即座)。
  音楽が未アップロードなら音源なし版が落ちてきて、その旨トーストで出る
- **画面**: 音楽欄は「曲名 · built in」。**Pick another file…** でそのセッションだけ差し替えでき、
  **Back to the built-in track** で戻る。プロジェクトが別の曲名を持っているときは、
  従来の黄色い帯に加えて「Playing the built-in track 曲名 instead」も出す
  (記録されている名前と、実際に鳴るものの両方を言う)。Run self-test や New project で
  埋め込み音源を失わないことも確認済み
- **コミットされるのは今までどおり音源なしの `dist/az27ss-simulator.html`**(565,274 バイト、
  2 MB の上限も `--check` もこちらだけ)。音源入り
  (`dist/az27ss-simulator-with-music.html`)は .gitignore 済み
- 実測(本番音源 AZ 27SS.DEMO.mp3 = 17,542,144 バイト):ページ 23,955,070 バイト、
  ヘッドレス Edge で `domInteractive` 348 ms / `domComplete` 747 ms(音源なしは
  34 ms / 211 ms)。サーバ側の初回ビルド約 1.8 秒(その間だけオペレーターの画面が
  固まる ―― Python の CPU 処理が GIL を握るため)、2 回目はキャッシュで 0.05 秒

**「Write to units…」- 機体への書き込みを 1 つの入口に(2026-09-25、依頼者の指摘から)**

依頼者の指摘:「Timeline で作成したシナリオを各機体に焼き込む機能について、UI 上で
書き込みボタンが見つけられなかった」。実際、書き込みの道は 2 つあるのに、① Upload は
「ショーの手順の 1 つ」に見え、もう一方は Units タブのいちばん下の「STANDALONE DEMO」という
名前のカードの中にしか無かった。Timeline タブには入口が 1 つも無かった。

- **入口は 1 つ、ダイアログも 1 つ**: Timeline ツールバーの **Write to units…**、
  THE SHOW カードの **④ Save on units (plays without this PC)**、
  「PLAY WITHOUT THIS PC (DEMO STORED ON THE UNITS)」(旧 STANDALONE DEMO)カードの
  ボタン ― どれも同じダイアログを開く(④ とカードは 2 つめの選択肢に焦点を当てて開く)
- **ダイアログは「書式」ではなく「選択」**: 2 つの選択肢を並べ、それぞれ一文で結果を言う。
  **Upload for the show**(いま絵を焼き、タイムラインを渡す。以後この PC が START / HOLD /
  STOP を出す)と **Save on the units**(各機体のメニューに名前つきで保存。KEY1 で PC 無しに
  再生)。所要時間(1 枚 **0.31 秒** × いちばん重い機体の枚数。枚数はいまのタイムラインから
  数える ― 基板数 × その機体のキュー時刻の数)、送り先の機体一覧(online / offline /
  デモ再生中)、押せない理由(キュー無し・タイムラインの問題・ショー進行中・デモ再生中・
  名前未入力・名前が LCD で出せない・1 台も応答していない)をその場に書く。実行後は
  機体ごとの結果(**見出し自身が成否を言う** ― 全部 / 一部 / 1 台も、最後は赤)、
  Upload ならタイルと同じ
  `picturesText()` による焼き込みの進み(同じポーリングの同じデータ)、デモなら機体側の
  手順(**KEY2** でメニュー → **名前**(STANDBY のすぐ下)→ **KEY1** で再生、**KEY2** で停止。
  機体に DEMO というサブメニューは無い ― デモは STANDBY の下に並ぶ行そのもの)。
  Esc / 背景クリック(書き込み中は無効)で閉じ、Tab はダイアログの中だけを回り、
  Ctrl+Z や矢印キーは後ろのページへ届かない。名前欄の Enter は
  「Save on the units」だけを実行する。二度押しは 1 回しか聞かない
- **チップ 2 つ**(Timeline の THE LOOKS AT バーと Units の THE SHOW 見出し、ダイアログの頭):
  `uploaded 10/10 · up to date` / `demo PARIS SS26 10/10 · changed since`
  (1280 px 以下では判定の語を隠し、色とツールチップに任せる ― 1024 px でも
  dock の見出しが 1 行に収まるように)。判定は **2 つの照合の両方**が揃ったときだけ言う:
  - 機体が報告する `units[].show.id` ・ `units[].demos[].show_id` と、この PC が書いた
    `/api/fleet` の `shows[unit].id`(= 機体が持っているのは、この PC が送ったものか)
  - ワークスペースの版 `timeline.revision` と、書き込んだ時点の版 `timeline.uploaded` ・
    `timeline.demos[名前]`(= 送ったものは、いま画面にあるタイムラインか)。
    **id だけでは分からない**(機体の id はこの PC が送った id そのものなので、そのあと
    いくら編集しても一致したまま ― レビュー指摘。以前はページの記憶に頼っていて
    再読み込みで消えた)
- **API**: `GET /api/fleet` の各機体に **`demos`**(`[{slug,name,cues,duration,loop,show_id,
  current}]`。`current` は上の id 照合、比較材料が無ければ `null`)を追加。
  一覧は**専用スレッド 1 本**が機体ごと **10 秒に 1 回**(`DEMO_LIST_EVERY_S`)取り
  (ポーリングループから外した ― eMMC の一覧取得がその機体の `/status` の間隔を
  2 秒 → 3.5 秒に延ばしていた。失敗しても 10 秒は空ける)、
  `/demo/save` `/demo/delete` の応答(どちらも機体のメニュー全体を返す)でも更新するので、
  **タイル 1 枚につき 1 リクエストにはならない**。答えられない機体(旧エージェントの 404、
  無応答)は `null` =「分からない」で、`[]` =「1 つも無い」とは区別する。
  機体が自分で数えている個数は `demo_count` に移した(旧 `demos` の整数)
- **API**: `GET /api/fleet` に **`timeline`**(`{revision, uploaded, demos: {名前: 版}}`)を追加。
  `revision` は show.json と全 CSV(名前・サイズ・mtime)の指紋で、**約 1 ms**。
  コンパイル済みの id を毎回出す案は測って捨てた ― `compile_show()` は 2 機体 5 キューの
  おもちゃのショーで 364 ms(10 機体 18 キューなら秒単位)で、毎秒のポーリングでは払えない。
  指紋の誤差は安全側にしか出ない(絵が変わらない編集を「changed since」と言うだけ)
- **`POST /api/fleet/write_demo` はショー進行中に 400 `stop the show first`**(Upload と同じ。
  ただしデモに `force` は無い ― 走っているショーへの復帰手段ではないので)
- タイルの「Demos」行は「**On unit**」行になり、個数ではなく
  `PARIS SS26 (4 cues · 1:30 · loop)` と中身を出す(`no demo stored` / 分からないときは「—」)
- **テスト: 686 件 + skip 1**(+11: fleet のデモキャッシュ 6、サーバの snapshot と
  write_demo 拒否 2、ページの id と文言 3)。ページは msedge の
  `--headless=new --dump-dom` と実ブラウザで、偽機体 2 台(127.0.0.1:19101/19104)相手に確認

**pre-burn 統合ラウンド完了(U2 + V2 + 第 2 巡レビューの修正、2026-09-25)- 実機 radxa-01 で確認済み**

このラウンドで pre-burn(Upload の時点で全部の絵をスロットへ焼き込み、本番中はトリガだけ)は
unit 側・conductor 側とも一本化された。以下がいまの契約。**PC と機体は必ず一緒に更新すること**
(古いページは機体の `cancelled` / `none` を `written` のように見せる)。

- **焼き込みの状態**(`/status` の `show.burn`、ロード済みショーには必ず付く):
  `burning`(`done`/`total`)/ `burned` / `failed` / `cancelled` / `none`。
  - **最後までリストを歩いた終わり方だけが `failed`** で、`failed: [[基板, スロット], …]` は
    「その基板が拒否した/不在だった」を意味する。**途中で終わった焼き込みは必ず
    `cancelled` + `reason`**(焼き込み中の STOP = 理由なし、`no serial port`、
    `bus busy: …`、`interrupted: the port was taken`、`the worker was stopped first`)。
    空の `failed` を持つ `failed` が PC に「0 board(s) not written」と force を
    勧めさせていたのを直した(第 2 巡レビュー)
  - **基板が 1 枚も答えない機体は `failed`**(全ペアが不在)+ `reason` =
    「none of its 16 boards answered」。衣装の電源が入っていないのはショー当日の
    普通の状況なので、**これで他の 9 台の START を止めない** - 機体自身のゲートは
    不在だけなら通し、PC はダイアログで聞いてから force で進める
    (「radxa-07: none of its 16 boards answered (that garment keeps whatever it
    shows) — start anyway?」)。`cancelled` はあくまで「何が書けたか分からない
    終わり方」だけ
  - 焼き込みは終わったが記録をディスクに書けなかったときは `record: "unsaved: <err>"` が付く
    (タイルに出る。`note` には入れない - `note` は status() が burn より先に読むので 1 ポーリング
    遅れ、run() が消してしまう)。再試行は次の `load()` のときだけ
  - **force が通すのは「生きている基板が拒否した `failed`」だけ**。`burning` / `cancelled` /
    `none` は force でも通らない(unit・conductor の両方で拒否)
- **記録の場所**: `~/.epaper/show-burn.json`(`{"burned": ショー id, "when", "state", "total",
  "failed"}`)。`load()` は新しいショーファイルを書く**前に**これを消すので、焼き込み中の再起動は
  同じショーでも必ず `none` で戻る
- **PC 側の文言**(fleet.py とページで同一): `writing 12/48` / `written` /
  `✗ not written (cancelled: 理由) — Upload again` / `✗ not written since it restarted —
  Upload again` / `✗ 10 of 12 pictures not written on boards 1, 2, 3 +1 more`。
  最後のものだけが ② ③ の「anyway?」(force)の対象。**枚数(基板 × キュー)で数える** -
  12 基板の 1 枚が落ちれば 18 枚であって「1 board」ではない
- **ショー中の ① Upload**: 確認ダイアログ付きで押せるようになった(`force`)。絵を失った 1 台を
  戻すための手段で、同じショーを持っている機体は 1 枚も書かない。**全機体がいったんショームから
  外れ、数秒後に監視が戻す**(ダイアログにもそう書いてある)。これまで復帰手段は STOP(全機体の
  ショーが終わる)しかなかった。この force は `self.run["force"]` にも乗るので、救出した機体の
  焼き直しが生きている基板で失敗しても、監視はその機体をショーに戻す(第 3 巡 R4)
- **conductor を再起動して走行中のショーを拾った run(`_adopt()`)は `force: True` を持つ** -
  すでにゲートを通って走っているショーに、監視の `/show/run` が force なしで入って拒否される
  のを防ぐ
- **機体がショー中に再起動しても白は出さない**: 起動時のスタンバイ(白)を飛ばし
  (`ShowPlayer.restored_running` を `ui/main.py` が見る)、**遅れているトリガを基板の探索より
  先に**送る。実機では探索に 16 秒かかり、その間ステージ上の衣装が真っ白になっていた。
  **飛ばすのは「まだ終わっていないショー」のときだけ**(`RESTORE_OVER_S`): 前夜のショーが
  終わったあとに電源を入れ直した機体は普通にスタンバイの白を出す(そうしないと壁が
  フィナーレのまま残る)。HOLD 中の機体にも同じ判定を使う
- **基板の探索はキューに道を譲る**(`PROBE_HOLD_S = 3 s`): 不在の基板 1 枚の探索は実機で
  約 1.5〜2.5 秒かかるので、次のトリガがこの時間内に来るなら次の探索を始めず、
  `_setup` の掃引はそのトリガを正確に待って先に撃つ(`_reprobe` は残りを次の間隔に回す)。
  **掃引と掃引の間の待ち・最初の 0.3 秒も 50 ms 刻みで同じ判定をする**(`_wait_probing`) -
  平坦な sleep のままだと隙間に落ちたキューが約 1.5 秒遅れていた。焼き込み中(`_run_burn`・
  焼き込み前の探索)も同じ。実機では再起動直後の 6 枚の探索(15 秒)が次のキューを
  **+4003 ms** 遅らせていた
- **焼き込みの所要時間のログはジョブを受けた瞬間から数える**:
  `burn done: 8/64 in 23.5 s (probe 22.1 s, 2 live boards, 14 absent: 3-16)` -
  実機では最初の探索 22 秒がタイマの外で「1.4 s」と出ており、操作者の待ち時間
  (24 秒)と合わなかった。不在の基板一覧も同じ 1 行に入れた(タイルのログは 6 行しか
  出ないので、`… - skipped` の行は長い焼き込みでは流れてしまう)
- **焼き込み時間**(実測に基づく見積り、`SAVE_S_PER_BOARD` 0.25 s + `CLEAR_S_PER_BOARD` 0.06 s):
  **36 基板 × 10 キュー ≈ 112 秒、× 18 キュー ≈ 195 秒(約 3.3 分)**。F5 のクリア(0x25)が
  初回だけ (基板, スロット) ごとに 1 フレーム増えるぶんを含む。**中身の変わらない再 Upload は
  ≈ 0 秒**(キャッシュが効いて 1 枚も書かない)

**実機測定(radxa-01、FW_260923、基板 1-2 のみ通電、2026-09-25)**

- 2 基板 × 4 スロットの焼き込みは cfg / clear 込みで **約 1.2 秒**(1 枚あたり約 0.15 秒)
- START 後のトリガは送信予定時刻に対して **+2 ms / +1 ms / +1 ms**(20 秒間隔、走行中の 0x13 は 0 件)
- PRESET を飛ばして START した回はプリセットのキューが 5 秒遅れて出た - **START が自力で治す**
- 不在/未知の基板は **1 プロセス起動につき 1 枚あたり約 1.5 秒**のシリアルタイムアウトを払う。
  16 基板の衣装で 14 枚が不在だと、以前は**最初のスロットの中で 22 秒**かかっていた(2〜4 枚目は
  各 0.3 秒)。いまは slot 1 の前にまとめて 1 回だけ払い、ログに
  `14 boards absent (3-16) - skipped` と出す。焼き込みの最後には
  `burn done: 8/64 in 23.5 s (2 live boards, 14 absent)` の 1 行
- 起動時のスタンバイの基板探索(3-8 の 6 枚が不在)は **約 16 秒**
- 中身の変わらない再 Upload は **0.02 秒**で `written` に戻る(書き込み 0 件)
- ショー中に `epaper-ui` を再起動 → `show-burn.json` から復元、conductor は
  「T0 confirmed after its restart」、書き直しゼロ、以後のキューは +1 ms で発火。
  修正後の再確認: 復帰後の最初のバス動作が「cue q01 fired slot 2」(再起動の 3 秒後)、
  スタンバイの白なし。その次のキューが探索に埋もれて +4003 ms 遅れた件は
  `PROBE_HOLD_S` で解消(上)
- START の新しい文言も実機で確認: 「56 of 64 pictures not written on boards 3, 4, 5
  +11 more」で拒否 → force で +1 ms 発火

**このラウンドの中身**(コミット: U2 = `5e4cede` のマージまで、V2 = `39d75fa` のマージまで、
第 2 巡の修正 = `20911c2`(unit)+ `4eb36a1`(conductor)+ 本エントリ)

- U2: 焼き込みをショーに紐付けて永続化(F1)、`force`(F2)、中断された焼き込み(F4)、
  古いスイープ表のクリア(F5)、F7、F9、F10
- V2: `_burn_problems` の状態の切り分け(F1)、force を機体まで運ぶ(F2)、PRESET のゲート(F3)、
  showfile の delays 必須化(F5)、監視の連打防止(F6)、デモ焼き込み中の扱い(F7)、docs(F8)、F11
- 第 2 巡: 上記の「途中で終わった焼き込みは cancelled」「枚数で数える」「ショー中の Upload」
  「adopt の force」「記録が書けないときの `record`」「再起動しても白を出さない」
  「absent 基板の探索を先に済ませて 1 行で言う」「電源の入っていない衣装 1 台で
  他の 9 台を止めない」「探索はキューに道を譲る」「START の force 再試行を PRESET と対称に」
  「LCD のヒントを 1 行に収める」「Pictures 行の言葉と CONDUCTOR_START §6 の表」
- **記録の細目**: `show-burn.json` は `reason` も残す(再起動しても「none of its 16 boards
  answered」が続く)。ただし基板が答え始めたらその一文は消える(`_fresh_reason`)- 絵は
  相変わらず書けていないが、「電源が入っていない」はもう本当ではないため(第 3 巡 R5)
- **テスト: 682 件 + skip 1**(Windows、4 分 2 秒。ラウンド開始時 667 + 1 から +15)

**フォローアップ(未着手)**

- **F9**: show file の各キューの `boards`(差分)は unit 側で誰も読まない。`validate_show` から
  `boards` を外し、`showfile.py` の `"boards"` を消せばファイルは約 1/3 小さくなる。
  `id` のハッシュが変わるので全機体が焼き直しになる - **本番前に済ませること**
- **フレーム単価の実測**: 上の 112 秒 / 195 秒は 1 枚 0.31 秒の見積り。36 基板の実機で 1 回
  計って置き換えること(radxa-01 の 2 基板では 1 枚 0.15 秒だった - バスが空いているぶん速い)

**pre-burn レビュー修正ラウンド、conductor 側(Coder V2、2026-09-25)- F1/F2/F3/F5/F6/F7/F8/F11 の PC 側**

- (履歴。統合後のいまの契約は上の統合エントリを見ること - 焼き込み状態には `cancelled` の
  `reason` が増え、`failed` のメッセージは枚数で数えるようになった)
- 対象は敵対的レビュー(`review_preburn_findings.md`)の conductor 側。unit 側(F1 の unit 半分、
  F2 の `_burn_gate(force)`、F4、F9 の死にコード、F10)は Coder U2 が同時に `ui/*` で実装した。
  **unit 側との契約**: 焼き込み状態は
  `"burning" | "burned" | "failed" | "cancelled" | "none"`(none = 再起動後・焼き込みを始められなかった
  load)。新しいエージェントはロード済みショーに **必ず `burn` dict** を返し、古いエージェントは
  `burn` キー自体を持たない。`/show/run` と `/show/preset` は `{"force": true}` を受け、force が通すのは
  **生きている基板の "failed" だけ**(burning / cancelled / none は通さない)。
- **F1(conductor 半分)** `Fleet._burn()`/`_burn_problems()`: 「`burn` キーなし(旧エージェント)= 止めない」と
  「`burn: null`・`cancelled`・`none`・未知の state = 止める」を区別(`_NO_BURN_KEY` 番兵)。メッセージは
  機体ごとに「radxa-04: pictures not written (cancelled) - Upload again」「… not written since it restarted -
  Upload again」「… not written - Upload again」。force でも通らない。snapshot の `burn` 要約はこれらを
  分母に入れて `burned` に数えない。Units タイルの Pictures 行も同じ言葉で出す(`"burn" in u.show` で
  旧エージェントの「—」と区別)。
- **F2** START の `force` を機体まで運ぶ: `start_show()` が `run["force"]` に保存し、`_send_run()`
  (START / SEEK / RESUME / NEXT)と `_supervise()` の `/show/run` が `{"t0","show","force"}` を送る
  (force なしでも `force: false` を明示)。fleet 側ゲートは force でも burning / cancelled / none を拒否した
  まま。テスト: force が body に届く、burning は force でも何も送らない。
- **F3** `Fleet.preset(force=False)`: force で "failed" だけを START と同じに通し、`/show/preset` に
  `{"force": …}` を送る。`/api/fleet/preset` は `body.force` を渡す。ページの ② Show preset は START と同じ
  確認ダイアログ(`burnFailedQuestion()` を共用: 「radxa-04: 2 board(s) not written (3, 7) — Show the preset
  anyway?」)を出して force で送り、ポーリング遅れで後から同じ理由の 400 が返ったときも一度だけ聞き直して
  再送する。
- **F5** `conductor/showfile.py`: **全キュー・全基板に `delays` を必ず出す**(スイープの無いキュー・共有機体の
  スイープしない側のアイテムの基板は全ソケット NO_DELAY の `NO_SWEEP_TABLE`。unit はそれを 0x25 として
  そのスロットへ書く)。`timeline.sweeps()` をキュー単位で見るので span 0 の custom は 0 フレーム表ではなく
  クリア表になる。`span` も全キューに出す(0.0)。**サイズ**: 6 ルックのサンプル
  (`docs/samples/az27ss_sample_show.json`、6 機体 × 3 キュー、16〜32 基板)で合計 200,485 → 221,804 B
  (+10.6%)。スイープの無い機体(radxa-04)は 13.9 → 26.7 KB(約 2 倍)、共有機体(radxa-03)は片側アイテムの
  表が加わって +19%、それ以外は同一。表は 1 基板 1 キューあたり 256 文字。36 基板 × 10 キューなら
  delays ≈ 95 KB が boards+state ≈ 95 KB に加わる見込み。初回焼き込みは基板・スロットごとに 0x25 が
  1 フレーム増える(unit は前回送った表と同じなら送らない)。`tests/test_showfile.py` を新設。
- **F6** `_supervise()`: `/show/load`・`/show/run` は `_post_or_refused()` 経由 - 送る**前に** `_corrected` を
  立てるので 409 でも次のポーリングで連打しない(SUPERVISE_EVERY_S 後に再試行)。拒否は理由が変わった
  ときだけ「radxa-04: run refused: <reason>」と 1 行記録し、通ったら忘れる。
- **F7** `_playing_demo()` は `state == "loaded"` かつ `demo` かつ `burn.state == "burning"`(デモの焼き込み中)でも
  真。upload()/_send_run() は「writing its demo pictures (n/N) - wait or STOP it」で断り、監視は
  「writing its demo pictures, left alone」と 1 回だけ記録して放置する。
- **F8** `conductor/timeline.py` docstring の 1-19/19 枚、README の「スロット 19 のみ使用する」を 0 / 1〜18 / 19 の
  割り当てに修正。`docs/CONDUCTOR_START.md` §6 を「① Upload → Pictures written on n / n units を待つ(36 基板
  × 10 キューで約 90 秒 - いまは約 112 秒に更新済み)→ ② → ③」にし、焼き込み中の STOP・機体の再起動は Upload し直し、FAILED は ② ③ の
  ダイアログで force、書き込み中は force 不可、と明記。`docs/SPECIFICATION.md` §3.3 に delays 必須・焼き込み
  状態・force の規則を追加。
- **F11** `tests/test_show_e2e.py` の RESUME 後の許容差を「0.2 s + resume() に実際にかかった時間」に。
- **F9(conductor 部分)は未対応・フォローアップ**: show file の各キューの `boards`(差分)は unit 側で誰も
  読まないが、unit の `validate_show` が `boards` キーを要求しているので**残してある**。U2 が
  `validate_show` から `boards` を外したら `showfile.py` の `"boards"` 行を消す(state と同サイズなので
  ファイルは約 1/3 減る。`id` のハッシュが変わるので全機体が焼き直しになる - 本番前に済ませること)。
- **e2e(`tests/test_show_e2e.py`)について**: U2 との統合後に回してある(上の統合エントリ)。
- コミット: `19c68cc`(fleet/server/page/showfile の本体、停電で PM が WIP 保存)→ `fbf1096`(F5 + F3 server
  test)→ `80f2762`(F1/F2/F3/F6/F7 の fleet tests)→ `35f017a`(F8 docs)→ `a8f9c3c`(F11)→ 本エントリ。
  テスト: **653 件 + skip 1**(Windows、3 分 51 秒。前ラウンド 636 + 1 から +17)。ページはネットワーク無しの
  疑似機体 6 台(failed / burned / null / cancelled / none / burn キーなし)を持つ conductor をブラウザで開いて確認:
  Pictures 行が 6 状態とも上の言葉で出る、要約が「pictures written on 1 / 5 units」(旧エージェントは分母外)、
  混在時は ② が質問せずサーバの拒否一覧を出す、failed だけのときは ② ③ とも「radxa-01: 2 board(s) not
  written (3, 7) — … anyway?」を聞いて `force: true` が `/show/preset`・`/show/run` の body に届く。

**pre-burn 統合(U の unit 側 + V の conductor 側を main に統合、2026-09-25)**

- U のブランチを main にリベースして統合(`df2da01`/`dc21e3c`)。統合で e2e が見つけた 2 つの隙間を
  `72b4ed9` で埋めた: (1) **PRESET も START と同じ焼き込みゲート**(`Fleet.preset()`、`/api/fleet/preset`
  はこれを使う)- 焼き込み中の unit は `/show/preset` を拒否するので、タイルごとのエラーではなく
  START と同じ「radxa-04: still writing 12/48」の一覧を返す(PRESET に force は無い)。
  (2) **監視(`_supervise`)は再起動した unit に `/show/load` を送った同じ回に `/show/run` を送らない**
  (焼き込み中で拒否され、修正の記録も失われていた)。「show reloaded, writing its pictures」と記録し、
  `status.show.burn.state == "burning"` の間は放置、焼き込みが終わった最初のポーリングで run を送る
  (「started late」)。holding 中は従来どおり hold を送る。
- e2e(`tests/test_show_e2e.py`)は Upload 後に `burned()` で焼き込み完了を待ってから PRESET/START する
  (本番の操作も同じ: Units タブの「Pictures written on n / n units」を見てから ② ③)。
- fake fleet(scratchpad/fake_fleet.py、127.0.0.1:18701/18704)で API を通した: Upload → 焼き込み
  (radxa-01: 32 枚、radxa-04: 48 枚)→ PRESET → START → ショー中の Upload は 400「stop the show first」
  → STOP。同じショーの再 Upload はキャッシュにより 0.5 秒未満で burned。
- テスト 636 件 + skip 1(Windows)。**当時は実機未検証**: 焼き込みの所要時間(いまの見積りは
  36 基板 × 10 キューで ≈ 112 秒 - 上の統合エントリ)、8 秒間隔の 0x1D、12 V レール。Radxa 復帰後に `git pull` と `epaper-ui` 再起動が全台に必要
  (`ui/*` が大きく変わった。conductor と unit は常に同時更新)。
- **統合版の敵対的レビュー(Opus, 2026-09-25)の判定は Block。機体への配布・本番使用は F1〜F5 の修正まで禁止。**
  要点(全文はセッションのスクラッチパッド `review_preburn_findings.md`):
  F1 焼き込み状態がセッション限りで `burn: null` が「旧エージェント」と「未焼き込み」を区別できない →
  (a) 焼き込み中に STOP → `cancel_burn()` が None にするので run/START が通る(スロットには前のショーの絵)、
  (b) Upload 後に機体が再起動 → `restore()` は再焼き込みせず完了記録もない、(c) 機体ビジー中の `/show/load` で
  新 id + 旧「burned」。修正: cancel は "cancelled"、`burn_finished` が show id を永続化、`load()` は変更前に状態を
  立てる、fleet は「burn キーなし」と「None」を区別。F2 START の `force` が unit に届かない(`/show/run` に force を
  運び `_burn_gate(force)` へ)。F3 PRESET に force がなく不在基板 1 枚で 0:00 が出せない。F4 ワーカーが焼き込みを
  放棄すると "burning" のまま固まる(`_stop` 分岐で `burn_finished`)。F5 スイープを全部外した再 Upload で古い
  0x1F テーブルが残る(delays を常に出して clear)。F6〜F11 は中程度以下(監視の連打、デモ焼き込み中の unit を
  `_playing_demo` が見ない、1-19 と書き残したドキュメント、死にコード、スレッド越しの set 反復、flaky e2e)。
  合格した点: スロット契約 0/1-18/19 は全経路で厳密、partial キューも全体像を焼く、RUNNING 中の 0x13 なし。

**本体側(unit): 事前焼き込み(pre-burn)方式への全面移行 - ショー中は 0x13 を一切送らない(2026-09-25)**

- 経緯: 「毎回リアルタイムに書き込みを行うのはショーにおいてリスクが高い」という利用者(Hirata)の
  要望を受け、当初検討していた三スロット・ローテーション+先読み書き込み方式(2 段階セッション、
  `FIRE_GUARD_S`)を実装途中で破棄し(WIP コミットとして残置)、Meris の回答
  (`docs/MERIS_REPLY_3SLOT.pdf`: 0x13/0x1F/0x1B は電源断をまたいで保持される、0x1D は起動トリガの
  みで書き込みではない)に基づく新方式に作り直した。契約(コーディネータ確定分): 基板は
  `slot_capacity: 20`(スロット 0〜19)を持ち、ショーの各キューはスロット **1〜18** のみを使う。
  スロット **19** は手動 Prepare(Designs タブ)とデモ行のワンショットプレビュー専用(ショー自身の
  焼き込みを誤って上書きしないため)、スロット **0** はスタンバイの白(マスタの電源投入直後の
  自動再生が白になる)。
- `ui/showplay.py`: `ShowPlayer.load()` はバリデーション後、各キューの **`state`(差分ではなく全体
  像)** を対応スロットへ書き込む「焼き込み(burn)」ジョブを `RemoteSession.burn()` 経由でランナーの
  ワーカーに積む。進捗は `status()["burn"] = {"done","total","failed":[[board,slot],...],
  "state":"burning"|"burned"|"failed"}`。`run()`/`preset()` は `burning` 中は
  `"still writing the pictures: n/N"` で拒否し、`failed` でも生きている基板が焼き込みを拒否して
  いれば拒否する(単に不在の基板は許容 - `runner.absent` で判定)。RUNNING 中の `_plan()` は一切
  書き込まず、`t0+cue["sent"]` に該当スロットの `0x1D` を 1 回投げるだけ(`session.arm()` という
  新しい軽量パス - 配列を持たず即座に READY になる)。ヒール(再結線した基板が最後のトリガを
  取りこぼした場合)も書き込みなしで同じスロットを撃ち直すだけ。スロットの無い旧ショーファイルは
  `"show file has no slots - update the conductor"` で明示的に拒否(`slot_capacity` 欠落も同じ扱い)。
  各キューのスロットは 1〜18 の範囲でなければロード時に拒否する。
- `ui/remote.py`: `RemoteSession` は単一ステージのまま(2 段階セッション案は不要と判断・破棄)。
  `prepare()`(手動 1 枚もの、常にスロット 19)はそのまま、新設の `arm(cue_id, slot, dev_type,
  label)` はボード配列を持たずいきなり READY にする軽量パス。`burn()`/`take_burn_job()`/
  `burn_progress()`/`burn_finished()`/`burn_status()`/`cancel_burn()` を追加。`due()` は
  `(cue_id, fire_at, slot, dev_type)` を返すようになり、`_fire_at()` はキューごとに異なるスロットを
  ブロードキャストできる。
- `ui/runner.py`: `_save_one()`(cfg→delay→save の 1 基板ぶん、**per-board の 0x17 を送らない** -
  Meris 回答どおり 0x13 は素の保存コマンド)を新設し、手動 `_save_cue()` と焼き込み `_run_burn()` の
  両方から使う。キャッシュを `(board, slot)` 単位に変更: `_cfg_done`(旧 `_needs_cfg` を反転した
  「済み」キャッシュ)、`_delays_sent`、新設の `_burn_cache`(`(board,slot)`→`(array,table)`。**再
  Upload で内容が変わらないキューは書き直さない** - 変わったキューのスロットだけ書き直す)。基板が
  1 回でも脱落したら(`_drop()`)その基板の 3 つのキャッシュを全スロット分まとめて捨てる
  (`_forget_board()`)。手動 `prepare()` がスロット 19 に書いた分は `_forget_burned()` で焼き込み
  キャッシュ側から明示的に無効化する(次の焼き込みで必ず上書き)。ブロードキャスト `0x17` は
  ポート取得時の `_setup()` 内で 1 回のみ(旧: 保存のたびに毎基板へ送っていた)。スタンバイは
  `standby()` がパターンループへ `slot=0` を明示的に渡すことで白をスロット 0 に描く。
  `_fire_at()` の待機ループは `_reprobe()` を挟むようにした(キュー間隔が数十秒に開き得るため -
  焼き込み済みなのでいつ撃っても構わないが、離脱していた基板の復帰検知を長い待機の間も止めない
  ため)。
- 見つけて直したタイミングのバグ 2 件(実機未検証、テストで再現・固定):
  1. `load()` は `_run_no` を自分でも 1 つ進めるようにした - 進めないと、焼き込み待ちのポーリング
     中に背景ループが**前回の run の FIRED セッションキー**を拾って `applied` を早合点し、今回の
     run の最初のキューを送らずに終わることがあった(焼き込みが実時間を取るようになって初めて顕在化)。
  2. `_plan()` の「今映すべきものを映す」判定は、`applied`(セッションの FIRED を見て決まる)と
     `current`(このティック自身の時計で決まる)という**別々の時計**を比較していたため、キューの
     境目ちょうどで数マイクロ秒の食い違いが起きると、直前に発火し終えたキューを「まだ違う」と
     誤判定して撃ち直すことがあった。`applied` が指すキューの `sent` と `current` の `sent` を
     比較する(順序で見る)ように修正。
- 焼き込み時間の見積り: 1 枚あたり `SAVE_S_PER_BOARD = 0.25 s` + `CLEAR_S_PER_BOARD = 0.06 s`
  (F5 のクリア。実測 0.22〜0.25 s の既存の定数に初回のクリアを足したもの)として、
  **36 基板 × 10 キュー ≈ 112 秒、× 18 キュー ≈ 195 秒**(`tests/test_showplay.py`
  `test_a_36_board_10_cue_burn_is_one_save_per_pair` に固定。初出のこの entry は「≈ 90 秒」と
  書いていた - 上の統合エントリで更新)。radxa-01 の 2 基板では 1 枚 0.15 秒だった。
- **`ui/app.py`(LCD の KEY1)も同日中に追従**: KEY1 は `player.load(show, demo=True, ...)` を
  呼んで焼き込みを始めるだけになり(`run()` はここではもう呼ばない)、DEMO 画面はその場で開いて
  `status.show.burn` から「writing pictures n/N - KEY2 cancel」をヒント行に出す。焼き込みが
  `"burned"` に落ち着いた最初のティックで `App._track_demo()`(新設の `_await_demo_burn()`)が
  1 回だけ `run()` を呼ぶ。`"failed"` で生きている基板が拒否していれば「N boards failed - KEY2
  menu」を出したまま `run()` は呼ばない(不在の基板だけなら `ShowPlayer.run()` 自身のゲートが
  通すので普通に走る)。KEY2 は焼き込み中でも `_stop_demo()` → `session.release()` →
  `player.stop()` の経路で `cancel_burn()` まで届く(既存の配線のまま)。ループ再生
  (`_loop_demo_show()`)は再ロードしない(=再焼き込みしない)まま。`tests/test_ui_demos.py` に
  この流れの新規テスト 4 件を追加、KEY1 系の既存テストは `_pump()`(`app.tick()` を回して待つ)
  へ全面的に書き換え。
- **残る既知の制約**: `_plan()` は先の「アーム済みの次キュー」がある間は同じセッション枠を
  使い回すため、直前のキューのヒール(離脱基板の再結線トリガ)をブロックすることがある(次
  キューが撃たれれば自然に解消するが、次キューが遠い間はヒールが後回しになる - 単一ステージの
  セッションゆえの制約として許容)。

**Load bundle…: レビュー指摘の修正 - whole-or-nothing、boards も units と同じ扱い、
危険なファイル名は拒否、上書きを申告(2026-09-24)**

- 元になっている計画は plan_designer_sim.md(コーダー間の作業分担メモ。方式 A = 単一 HTML・
  bundle は zip ではなく **CSV 本文を埋め込んだ JSON 1 個**、音楽は名前のみ。
  `docs/DESIGNER_SIMULATOR_PLAN.md` にも決定として追記した)
- **`boards`(基板番号の付け替え)を `units` と同じ「運用側のデータ」として扱う**: バンドルの
  `show.boards` が空ならキーごと外して現場の付け替えに触れない(`units` と同じロジックを
  `for key in ("units", "boards")` でまとめた)。戻り値に `boards_kept` を追加
- **`units: {"Look22": null}` が全割り当てを消す事故を修正**: `units_kept` は生の
  `show.get("units")` の真偽ではなく、`{k: v for k, v in ... if v}` で null/空文字を除いた
  **後**の中身が空かどうかで決める。これで「1 項目だけ null」を含むバンドルが、
  現場の割り当てを丸ごと `{}` に潰すことがなくなった
- **whole-or-nothing に変更**: 以前は CSV を保存してからタイムラインを検証していたため、
  タイムラインが壊れているとバンドルの CSV だけが書き込まれた状態になり得た。
  `import_show()` の検証部分を `_validate_show()`(コミットしない)として切り出し、
  **ファイル名の安全確認とタイムラインの検証を、CSV を 1 つも保存する前に**両方済ませる
  ようにした。ファイル数の上限(200)も追加
- **ファイル名は `/api/files` と同じ規則で確認するが、危険な名前は勝手に読み替えない**:
  `../x_map.csv` や `/etc/x_map.csv`、NUL を含む名前は保存を試みず最初から `refused` に
  積む(以前は `Workspace.save()` に渡して例外を拾っていたため、内部で無害化されてから
  弾かれていた)
- **上書きは申告制**: 既に存在していた同名 CSV の一覧を `overwritten` として返す。ボタンの
  説明文・確認ダイアログ・トーストのいずれにも「同名の CSV は上書きされ、Undo では戻らない」
  と明記
- **音楽の名前は `payload.get("music") or show.get("music")`** から取るように変更(どちらかに
  入っていれば拾う)。ページ側はバージョン不一致を確認ダイアログより前でトースト表示にし、
  トーストは拒否されたファイルの理由・警告を(短ければ)そのまま、長ければコンソールに
  逃がして件数だけ出す
- レスポンス: `{"ok","saved","refused","overwritten","cues","warnings","units_kept",
  "boards_kept","music"}`
- テスト追加(`tests/test_bundle.py`、計 18 件 + フィクスチャ不在時は 1 件 skip): boards が
  空のバンドルで基板の付け替えが残ること・逆にバンドルが運べば適用されること、
  `units: {"Look22": null}` が他の割り当てを消さないこと、上書き一覧、危険なファイル名 3 種
  が保存されずに拒否されること、値が文字列でないファイルが1つでもあると何も書き込まれない
  こと、201 ファイルが拒否されること、バンドルの音楽名が実際の音楽ファイルを書き換えない
  こと。`test_designer_bundle_fixture_imports` は P のフィクスチャが無い間は
  `pytest.skip`(以前は自作の代用バンドルで黙って通していた)
- `docs/SIMULATOR_FOR_DESIGNERS.md` §6(渡し方: 上書き・ラベル/遷移が消える項目・基板の
  付け替えが残ることを明記)・§7(「2 着が同じ radxa」の重なりチェックだけはシミュレーターで
  出せない、と明記)を更新

**Conductor 側: 演出家のシミュレーターから「まるごとプロジェクト」を読み込む Load bundle…(2026-09-24)**

- 演出家チーム向けシミュレーター(単一 HTML・サーバー不要、`docs/DESIGNER_SIMULATOR_PLAN.md`)が
  書き出す `epaper-show-bundle` v1(CSV 一式 + タイムラインを 1 個の JSON にまとめたもの)を、
  Conductor 側で受け取れるようにした。Timeline タブの「Load show…」の隣に「Load bundle…」を
  追加(`conductor/web/index.html`。確認ダイアログは「デザイナーから送られたプロジェクトを
  読み込みますか。CSV はワークスペースに追加され、タイムラインは今のものと置き換わります。
  機体の割り当てはそのまま残ります」の趣旨)。
- **`Workspace.import_bundle(payload)`**(新規、`conductor/server.py`): フォーマット/バージョンを
  検証 → CSV を `POST /api/files` と同じ規則で保存(`*_map.csv` / `*_color_*_grid.csv` 以外は
  refused に積むだけで処理は続ける) → `show = dict(payload["show"])` から `units` が空(演出家の
  シミュレーターには機体という概念が無いので通常はこちら)なら `units` キーごと外してから
  `import_show()` に渡す。**タイムラインの置き換えは `import_show()` と同じ 1 コミット**
  (CSV の保存自体は show.json の undo 対象外 - `/api/files` と同じ扱い)。**現場の機体割り当ては、
  バンドル側が自分の割り当てを運んできたときだけ上書きし、それ以外は一切触らない**
  (戻り値の `units_kept` で判定結果を返す)。音楽はバンドルでも名前だけの参考情報 - 実体は
  シミュレーター側の制約と同じくこのマシンで毎回選び直す。
- **`POST /api/bundle/import`**(新規): `/api/show/import` の直後に追加。同じ例外タプルで
  壊れた JSON(`{"show": null}` 等)も 500 ではなく 400 になる。
- レスポンス: `{"ok","saved","refused","cues","warnings","units_kept","music"}`。
- テスト `tests/test_bundle.py`(11 件): CSV 保存とタイムライン反映、機体割り当てを維持する
  ケースとバンドル側の割り当てが勝つケース、undo が 1 手で完全に戻ること(CSV は戻らない)、
  フォーマット/バージョン/ファイル名が悪いときの拒否、バンドル自身が運んできた CSV を同じ
  取り込みの中で警告チェックが見つけられること、既存の `/api/show/import` が無傷であること
  を確認。P 担当の `tests/fixtures/sim/bundle_v1.json` はこのコミットの時点でまだ存在しない
  ため、`tests/test_look.py` の MAP/GRID から手作りした最小バンドルで代用(存在すればそちらを
  優先して読む実装済み - 後で置かれれば自動的に切り替わる)。
- デザイナー向けの使い方(開き方・CSV の入れ方・mm.ss・保存と受け渡し・制限)は日本語で
  `docs/SIMULATOR_FOR_DESIGNERS.md` に。

**Timeline: 1 秒 gap ルールをレビューで修正 - 本体の準備時間より詰めない(2026-09-24)**
**Conductor 修正ラウンド: START ゲートの信頼性・Upload 中ショー禁止・瞬間単位の間隔
ルール・スロット契約 1-18/19/0(2026-09-24, Conductor 側。すぐ下の「事前焼き込み方式」
エントリの数値・文言をここで更新する - スロットは 19 枚までではなく 18 枚まで)**

- **スロット契約を機体側コーダーと確定**: スロット 0 = 白のスタンバイ、
  **1〜18 = ショーの絵**(`conductor/timeline.py` の `MAX_CUES_PER_UNIT = 18`)、
  **19 = 手動発火(Prepare)・スタンドアロンデモの単発表示専用**(ショーの
  タイムラインは使わない)。`conductor/showfile.py` の show ファイルは
  `"slots"` ではなく **`"slot_capacity": 20`**(`timeline.SLOT_CAPACITY`、基板の
  スロット総数)を持つ。19 枚目の問題文言は「a board holds 18 show pictures
  (slot 0 is the white standby, slot 19 the manual one-shot)」に変更。
- **START ゲートが古い/よその burn 報告を信用しないように**
  (`conductor/fleet.py` の `_burn`/`_burn_problems`、レビューで発見):
  対象機体が offline または未応答なら「radxa-0N: not answering」、
  `status.show.id` が今回アップロードした show の id と一致しなければ
  「radxa-0N: has not taken this show yet」- どちらも `force` では越えられない。
  `burn` フィールドが無い(旧機体ソフト)ことは今まで通り許可。
  `"failed"`(基板の書き込み失敗)は `force=True` のときだけ通す(「burning」は
  `force` があっても常に拒否)。機体が自分のデモを焼いている最中(`show.demo`)は
  「radxa-0N: writing its demo pictures (n/N) - wait or STOP it」。複数機体が
  引っかかれば `"; "` で連結。ページの START は、`fleet.units` の
  `show.burn.state === "failed"` を見て「radxa-04: 2 board(s) not written
  (12, 15) — start anyway?」を `confirm()` で聞き、承諾すると `force: true` を
  付けて再送する(サーバ側の拒否は据え置き - 二重の防御)。
- **Upload 中にショーが動いていたら拒否**: `conductor/server.py` の
  `/api/fleet/upload` は `fleet.run` がある間 `force` なしでは 400
  「stop the show first」。ページは `#show-upload` を run 中は disabled にし、
  title も「Stop the show first.」に変える(通常時は Upload の意味を説明する
  title - 挿入で全キューの slot 番号がずれて全体を焼き直す(1 機体あたり
  約 3 分)のに対し、末尾への追加やその場の編集は自分のスロットだけで済む、
  という注意も含む)。
- **バス間隔ルールは「前のキュー」ではなく「前の瞬間(送信)」単位に修正**
  (レビューで発見): Look20 の上下のように 1 台を共有する 2 アイテムが同時刻の
  キューを持つとき、次の送信までの必要間隔は「その瞬間の全キューの中で一番遅い
  refresh・一番長いスイープ」で決まる - 以前は瞬間内で最後にソートされた 1 キュー
  だけを見ていたため、スイープしない方のアイテムがたまたま後に来ると必要な余裕を
  過小評価していた。テスト
  `test_a_sweep_on_one_item_of_a_shared_unit_sets_the_room_for_both`
  (Top が 5 秒スイープ・Skirt はしない、同時刻 → 次に必要な間隔は 7+5+1=13 秒)。
- **Fleet summary の頑健化**: `snapshot()["burn"]["total"]` は「burn 情報を
  実際に報告している機体の数」だけを数える(アップロード先だが burn について
  何も言っていない機体は分母に入れない)。ページは `total` が 0 なら
  「pictures written on …」の行を出さない。`_burn`/`_burn_problems`/`_raw_burn`
  は `show`/`burn` が dict でない(hostile/古い応答)場合を想定して isinstance
  ガードを追加。タイルの Pictures 行の数値は `esc()` を通し、「writing n / N」の
  隣に経過秒数(このブラウザタブが burning を初めて見てからの秒数、cheap)を出す。
- **Timeline の「Shortest interval」表示**は数字から逆算する形に修正
  (`need`・`refresh` から `gap = need - refresh` を計算、`refresh` も 1 桁小数) -
  「1 s」や `toFixed(0)` のような決め打ちをやめた。
- テスト: `tests/test_timeline.py`(`test_a_unit_may_carry_at_most_eighteen_show_pictures`
  に改名・18 枚に変更、`test_coincident_cues_on_a_shared_unit_count_as_one_picture`、
  `test_a_sweep_on_one_item_of_a_shared_unit_sets_the_room_for_both` を追加)、
  `tests/test_fleet.py`(offline のstale「burned」・id 不一致・2 台が burning 中の
  連結・`force` は failed のみ通し burning は通さない・デモ焼き込み中の文言、
  計 6 件追加)、`tests/test_conductor_server.py`
  (`test_upload_while_a_show_is_running_is_refused_unless_forced` を追加。
  既存の `fleet.links = {}` を使う START 系テスト 3 件は、新しい burn ゲートが
  「機体不明 = not answering」で弾いてしまうため `StubLink` を使うよう更新)。
- 依存: `git merge main` で `docs/MERIS_REPLY_3SLOT.pdf` が `docs/MERIS_REPLY_3SLOT.md`
  (テキスト、PDF は git 対象外)に置き換わったので、参照箇所をすべて `.md` に変更。

**Timeline/Showfile: 事前焼き込み方式に設計変更 - 本番中は基板に一切書き込まない
(2026-09-24, Conductor 側。以下は直前の「1 秒 gap ルール」エントリ全体を置き換える。
スロット数・`"slots"` キーは上のエントリで 18/`slot_capacity` に更新済み)**

- 依頼者(Hirata)の方針転換:「毎回リアルタイムに書き込みを行うのはショーにおいて
  リスクが高い。Timeline 焼き込みの段階で 20 スロットをできる限り使って書き込みを
  終了させておき、radxa からはトリガーのみ送る」。直前のエントリで実装した「書き込み
  時間を見積もって間隔を詰めすぎない」ルール(3 スロットのローテーションで書き込みと
  発火を重ねる案も含め)は、この方針でまるごと不要になった - 本番中に書き込みが起きない
  なら、書き込み時間を見積もる理由がない。
- メーカー回答(Meris, 2026-09-24、`docs/MERIS_REPLY_3SLOT.pdf`): スロット 0〜19 は
  全て構造上同一でいつでも上書き可能、0x13/0x1F/0x1B は電源断をまたいで保持される。
  これを踏まえた新しい割り当て: **スロット 0 = 白のスタンバイ専用**(基板の電源投入時
  オートプレイもここになる)、**スロット 1〜19 = ショーの絵**(1 基板 19 枚まで)。
- `conductor/showfile.py`: `build_unit_show` が各ユニットキュー(プリセット含む)に
  送信順で `"slot": 1, 2, 3, …` を割り当てる(スロット 0 は使わない)。`show["slots"]`
  に基板の総スロット数(19)を持たせた。
- `conductor/timeline.py`: 前回導入した「書き込み項」「rejoin 項」(本体の
  `UNIT_SAVE_S_PER_BOARD`/`UNIT_PREP_MARGIN_S`/`UNIT_SETUP_S`/`UNIT_SETUP_S_PER_BOARD`
  を複製した定数群)を丸ごと削除。`validate()`/`min_interval()` は今後
  **`refresh + gap`(既定 8.0 s)一本**になり、基板数に一切左右されない - プリセット直後の
  最初のキューも同じ式(以前あった「本体が `/show/run` で合流し直す」ぶんの上乗せは、
  書き込みがもう発生しないので不要)。代わりに新しい制約を追加: 1 基板が持てる絵は
  **`MAX_CUES_PER_UNIT = 19` 枚**までで、1 機体のタイムラインがそれを超えると
  「radxa-04 carries 20 pictures but a board holds 19 (slot 0 is the white standby) -
  merge or remove cues」のように問題として出す(超過分の各キューに個別に出る)。
- `conductor/fleet.py`: 機体の `/status` が返す `show.burn`(本体側 Coder が実装中の
  形: `{"done","total","failed":[[board,slot],...],"state":"burning"|"burned"|"failed"}`)
  を読み、`start_show()` はどれかの対象機体が `"burning"`(「radxa-04: still writing
  12/48」)または `"failed"`(「radxa-04: 2 board(s) failed to write their pictures」)の
  間は `ValueError` で拒否する(機体ごとのメッセージを `"; "` で連結)。`snapshot()` に
  `"burn": {"burned": n, "total": m}`(この conductor がアップロードした機体のうち
  書き込み完了した数)を追加 - `/api/fleet` でページに渡る。`upload()` 自体は従来どおり
  `/show/load` を投げて即座に返る(焼き込みは機体側で非同期)。
- `conductor/web/index.html`: Units タブの各タイルに **Pictures** 行(writing n / N・
  written・✗ FAILED)、「THE SHOW」カードに「pictures written on n / m units」の行、
  ① Upload ボタンに「今すぐ全ての絵を基板に書き込む・本番中はトリガーのみ」という
  title、Timeline タブの「Shortest interval」を機体別の一覧から単一の値
  (例: 「8.0 s (7 s refresh + 1 s)」)に変更 - 基板数で変わらなくなったため。
- テスト: `tests/test_timeline.py`(`min_interval` は `refresh+gap` のみを検証、
  `test_a_unit_may_carry_at_most_nineteen_pictures` を追加、書き込み項・rejoin 項の
  テストは全て削除・置き換え)、`tests/test_show_e2e.py`
  (`test_every_unit_cue_gets_its_own_slot_in_send_order`)、`tests/test_fleet.py`
  (`test_start_refuses_while_a_unit_is_still_burning_its_pictures` ほか、burn 関連 5 件)。
  `tests/test_conductor_server.py` は数値(8.0/17.0)に変更なし(元から refresh 項が
  勝つケースだった)。
- **未対応・要検証**: 本体側(`ui/showplay.py`・`ui/runner.py`・`ui/remote.py`)の
  実際の焼き込みジョブ・`show.burn` 進捗報告・古い show file(`slot` なし)の扱いは
  別のコーダーが並行実装中(このセッションの対象外)。実機(Radxa)での焼き込み所要
  時間(36 枚 × 10 キューなど)・スロット 0 のオートプレイの実測は未確認のまま
  (docs/STATUS.md §3 系のメーカー再質問リスト参照)。

**Timeline: 1 秒 gap ルールをレビューで修正 - 本体の準備時間より詰めない(2026-09-24、
上のエントリにより置き換え済み - 経緯として残す)**

- ディレクターの要望:「Reflesh が終わった後、1 秒後に次のデザインへの refresh に入ることができる
  ようにしたい」。旧ルール `min_interval(boards, refresh) = refresh + boards×0.22 s + 3.0 s`(例:
  16 枚・refresh 7 s → 13.5 s、36 枚 → 17.9 s)は、次のキューの書き込みが前の絵の完成後に始まる、
  という前提だった。
- **このセッションの最初の実装がレビューで指摘された問題**: 「refresh + gap か、書き込み時間か、
  大きい方」という式自体は妥当だが、書き込み時間の見積もりに `conductor/timeline.py` 独自の楽観的な
  定数(0.22 s/基板・余裕 1.0 s)を使っていた。これは本体(`ui/showplay.py`)が実際に使っている値
  (`SAVE_S_PER_BOARD = 0.25 s`・`PREP_MARGIN_S = 2.0 s`)より短く、その結果 conductor が「問題なし」
  と判定する間隔が、本体自身の準備時間より詰まってしまうケースがあった。今日のコードではこれが
  起きると、本体側 `ui/showplay.py` の `_plan()` がまだ発火していないキューを追い越して次を書き始め
  てしまう(本体側の対策は別のコーダーが別ブランチで対応中)。**conductor は、本体が実際に必要と
  する時間より短い間隔を安全だと太鼓判を押してはならない** - この回はその修正。
- 新ルール(`conductor/timeline.py`): 3 つの候補のうち一番大きいものを `need` とする。
  - refresh 項: 「前のキュー自身の」refresh(`effective_refresh(before, refresh)`。show 既定値では
    なく、前のキューが `refresh_s` を持てばその値)+ 前のキューのスイープ span + `GAP_AFTER_REFRESH_S`
    (1.0 s、ディレクターの最小値)。
  - 書き込み項: 本体の定数をそのまま複製した `UNIT_SAVE_S_PER_BOARD = 0.25 s`・
    `UNIT_PREP_MARGIN_S = 2.0 s` を使い、`boards × 0.25 s + 2.0 s`(次のキューがスイープするなら
    delay table も書くので `boards × 2 × 0.25 s + 2.0 s`)。
  - rejoin 項: 前のキューがプリセット(`at <= 0`)のときだけ、書き込み項にさらに
    `UNIT_SETUP_S = 1.0 s`(本体の `SETUP_S`)+ `boards × UNIT_SETUP_S_PER_BOARD`(`0.15 s`、本体の
    `SETUP_S_PER_BOARD`)+ gap を足したもの。本体は `/show/run` が届いて初めて書き込みを始め、PC は
    それを T0 の `DEFAULT_LEAD_S`(`conductor/fleet.py`、3 秒)前に送るだけなので、プリセット直後の
    最初のキューでは「ちょうど今 `/show/run` で合流し直した本体」を想定しないと安全でない。
  - `min_interval(boards, refresh, gap, sweep=False)` は refresh 項と書き込み項のみ(ページの
    「1 台あたりの最短間隔」表示に使う値。スイープなしの数値であることに注意 - ページはスイープする
    キールを想定していない)。数値: **16 枚・スイープなし 8.0 s、16 枚・スイープあり 10.0 s、
    24 枚 8.0 s、27 枚 8.75 s、32 枚 10.0 s、36 枚 11.0 s**。プリセット直後の最初のキュー(16 枚・
    スイープなし)は rejoin 項が効いて **10.4 s**(旧ルールでは 10 s だったので実は旧ルールより厳しい
    - 旧ルールはこのケースを想定していなかった)。
  - 同一アイテム内の「前の絵が完成する前に始まる」ルール(重複を避けるための `overlapped` 抑制)は、
    **refresh 項が binding のときだけ**バス側のメッセージを黙らせる。書き込み項・rejoin 項が binding
    のときは必ず出す(本体の書き込み時間が足りないという事実は、絵の完成時刻の話とは別物なので)。
  - 文言は 1 桁小数で曖昧さなし。どちらも「after the previous send」で統一:
    refresh 項が binding「only 7.5 s after the previous send on radxa-04; at least 8.0 s is needed
    (7.0 s refresh + 1.0 s gap)」、書き込み項が binding「only 8.0 s after the previous send on
    radxa-04; writing its 32 boards needs 10.0 s (32 × 0.25 s + 2.0 s)」。
- **証拠の見直し(誠実な書き直し)**: 前回のこのセッションは「2026-08-14 の記録が repaint 中の save
  実行を証明した」と書いたが、これは言い過ぎだった。**2026-08-14 の記録が実際に示すのは**、repaint
  中に届いたコマンドが**キューに積まれ、repaint が終わってから実行される**こと、そして 1 回余分な
  show コマンドを送ると 1 回余分な repaint が起きること、の 2 点だけである。**示していないもの**:
  repaint の途中で save が実際に実行されること(むしろ上の観測はその逆、実行が repaint 後に遅延する
  ことを示唆する)。また、既定の refresh 7 秒は最新ファームウェアの**報告値**であって測定値ではない
  (`REFRESH_S` のコメント参照)。
- **実機(Radxa)が戻ったら測定すべきこと**(この 1 秒 gap を実ショーで使う前に、すべて未確認):
  1. 基板 1 枚あたりの実際の repaint 時間(command → 絵が完成するまで)。
  2. repaint 開始 1〜2 秒後に stop/save を送った場合: ACK が返るか、それがいつ実行されるか、絵が
     壊れずに残るか。
  3. 同じスロットへの上書き保存が repaint 中に届いた場合の挙動。
  4. ある基板がまだ repaint 中に、次の show(全体ブロードキャスト)が届いた場合の挙動。
  5. 基板 32 枚・36 枚時の、リトライを含めた基板ごとの実際の save 時間。
  6. 10 キュー連続のショーを流し、発火回数(show コマンド数)が想定通りであることの確認。
  7. 重なった書き込み(複数基板が同時に stop/save 中)での 12 V レールの電圧・電流。
- `gap` は将来ショーの設定(`show.json` の `gap_s`、0〜30 s・小数点 1 桁、デフォルト 1.0)にして操作
  側で緩められるようにする予定だが、`timeline.clean()`/`set_timeline()` の配線は `conductor/server.py`
  にあり今回のセッションでは触れないため、モデル側(`min_interval(..., gap=..., sweep=...)`・
  `validate(..., gap=...)`、デフォルト `GAP_AFTER_REFRESH_S`)のみ実装した。**TODO: `gap_s` を
  ショー設定として保存・編集できるようにする server.py 側・ページ側の配線(別セッションで割り当て)。**
- テスト: `tests/test_timeline.py`(`min_interval` の新しい期待値と `sweep=` 引数、本体定数のミラーが
  `ui/showplay.py`(と `conductor/fleet.py` の `DEFAULT_LEAD_S`)と一致することを確認するテスト、
  `test_the_interval_is_never_shorter_than_the_units_prepare_lead`・
  `test_a_sweeping_cue_doubles_the_write_term`・
  `test_the_previous_cues_own_refresh_time_sets_the_gap`・
  `test_the_first_cue_must_leave_time_to_write_the_boards_after_start`(rejoin 項、10.4 s)を追加/更新)、
  `tests/test_conductor_server.py`(3 枚では書き込み項が効かないため数値は 8.0/17.0 のまま変更なし)、
  `tests/test_showplay.py`(`test_the_write_is_issued_right_after_the_previous_fire` に改名 - 前の
  refresh 完了前に書き込みが始まることまでは主張せず、次のキュー自身の送信時刻までに書き込みが
  始まっていること・時刻通りに発火することだけを確認するよう、判定窓を広げてフレーク耐性を上げた)。
**機体側: スタンドアローンデモの書き込み・再生(2026-09-24、Unit side のみ。PC/Conductor 側は別対応)**

- `ui/demos.py`(新規)に `DemoStore`: PC がユニットごとに作る show ファイル(`conductor/showfile.py`
  の出力そのもの)を名前つきで `~/.epaper/demos/<slug>.json` に 1 デモ 1 ファイルで保存。スラグは名前の
  `[a-z0-9-]` 化、同名なら上書き・別名の衝突は `-2` 採番、上限 20 件(超えると
  `"demo store full - delete one first"`)。バリデーションは `ui/showplay.py` に切り出した
  `validate_show()` を共用 - 壊れた show は書き込み時に弾く。
- `ui/agent.py`: `POST /demo/save {"name","loop","show"}` → `{"ok","slug","demos":[...]}`、
  `GET /demo/list` → `{"demos":[...]}`、`POST /demo/delete {"slug"}` → `{"ok","demos":[...]}`。
  `/demo/save` は show 実行中・HOLD 中は `/show/load` と同じ文言で拒否。`/status` に `"demos": 件数`。
  `/show/load` は **デモ実行中のときだけ**同じ文言で拒否 - PC 主導の show 実行中の再読込
  (`conductor/fleet.py` の supervise の再送)は今まで通り塞がない。
- `ui/app.py`: メニューは STANDBY の直後にデモを 1 行ずつ差し込む(`DemoRow`、店の内容が変われば
  `refresh_demos()` で 2 秒ごとに追随)。KEY1 で `ShowPlayer.load(show, demo=True)` →
  `run(t0=いま+リード)`(プリセットが 0:00 に間に合う)。画面は新設の `Screen.DEMO` -
  `render.remote_screen()` を `title="DEMO <name>"` で再利用(カウントダウン・ログはそのまま)。
  KEY2 は `remote.release()`(show PC と同じセッションを解放 - そうしないと画面が REMOTE に
  跳ねる)、KEY1 長押しで 0:00 から再走、`loop` 指定なら ENDED から `LOOP_GAP_S=5s` 後に自走再開。
  ロック中は他の行と同じく無効。
- ついでに見つけたバグを修正: `ShowPlayer._tally()` が旧 run の FIRED セッションを新しい run の
  ものと誤認する経路があった(1 キューだけの show を再走すると、最初と最後のキュー id が同じ
  `"q00"` になるため顕在化)。run 番号もキーに含めて比較するよう修正。
- `tests/test_ui_demos.py`(新規、23 件): store の保存/一覧/削除/スラグ衝突/上限、agent の
  3 エンドポイントと拒否、メニュー行の増減、KEY1 再生(fake bus でプリセット→次キューの発火時刻を確認)、
  KEY2 停止、ループ再開、ロック無効化。既存テストは無変更で全通過(全 517 件)。
- 未対応(Conductor/Coder B 側): `conductor/fleet.py` の `write_demo`/`list_demos`/`delete_demo`、
  `POST /api/fleet/write_demo` 等のサーバ API、Units タブの「STANDALONE DEMO」カード、README/
  CONDUCTOR_START.md。LCD のフォント(DejaVuSans / Windows は Segoe UI)は日本語グリフを持たないため、
  ユニットに表示する名前は ASCII のみに制限すること(14 文字、PC 側での強制が必要)。
**スタンドアローンデモ: タイムラインを機体自身のメニューに書き込む(2026-09-24、Conductor 側)**

- Units タブに新しいカード「STANDALONE DEMO」。名前(A〜Z・0〜9・記号、最大 14
  文字 — 機体の LCD フォント DejaVu は日本語を描けないため拒否)と Loop を決めて
  「Write demo to units」を押すと、いまのタイムラインを機体ごとに `compile_show()`
  した結果(Upload と同じショーファイル)を各機体の `/demo/save` へ送る。
  タイムラインに問題が残っている間・**ショーが進行中の間**は Upload と同じ判定に
  加えて押せない(まず STOP)。
- `conductor/fleet.py`: `Fleet.write_demo(name, loop, shows)`(`upload()` と同じ
  `_each()` の形で、`self.shows`/`run` には触れない。`/demo/save` はネットワーク
  の速さと無関係な eMMC 書き込みを含むので `UnitLink.post(..., learn=False,
  timeout=DEMO_SAVE_TIMEOUT_S)` — 往復時間を時計モデルに混ぜない・タイムアウトを
  4 倍にする)、`list_demos()`(`GET /demo/list`、`UnitLink.get()` を新設)、
  `delete_demo(slug)`(`POST /demo/delete`、**online な機体だけ**。offline は
  `{"ok": false, "error": "offline"}` で試みずに返す、list_demos() と同じ)。
- `conductor/server.py`: `POST /api/fleet/write_demo` `{"name","loop"}` →
  `{"units","problems","name"}`(名前は 1〜14 文字・印字可能 ASCII のみ、前後の
  空白は捨てる。`loop` は JSON の真偽値のみ許可)。`GET /api/fleet/demos` →
  `{"units": {unit: [demo,...]}, "offline": [...], "failed": {unit: "..."}}`
  (無応答は `offline`、応答はしたが失敗した機体 — 旧いエージェントの 404 など —
  は `failed` に理由付きで分ける)。各デモに `current`(その per-unit ショー ID が、
  いま**アップロード済みの版**(`fleet.shows`、何もアップロードしていなければ
  その場で `compile_show()` した結果)と一致するか。比較材料が無い・そのデモに
  `show_id` が無い場合は `null` — ページは `older` ではなく「—」と出す)を添えて
  返す。`POST /api/fleet/delete_demo {"slug"}`(slug は `^[a-z0-9][a-z0-9-]*$` を
  PC 側でも検証、外れは 400 "bad demo id")。
- ページ: 「Demos on the units」表(slug と名前の組でグルーピング、機体ごとに
  キュー数・長さ・Loop が違うことがあるので `16 · 16 · 27` のように機体の並び順で
  列挙・**Timeline**(current / older(該当機体) / 「—」)・保持機体・Delete)。
  取得に失敗すると表の代わりにエラー文を出し、以後は Refresh か書き込み・削除の
  あとにしか取り直さない(失敗のたびに毎秒リトライしない)。ショー進行中は
  「Write demo to units」も無効(ヒント表示)。機体タイルに「Demos」行(`/status`
  の `demos` 件数。旧いエージェントには無いので「—」)。
- **機体が自分のデモを再生中(running/holding)は PC の進行と衝突させない**:
  `fleet.py` の `_adopt()`/`_supervise()`/`_send_run()` は、機体の
  `show.state` が running/holding **かつ** `demo: true` のときだけ対象から外す
  (`_playing_demo()`)。**stopped/ended など再生中でなくなれば通常どおり監視・
  補正の対象に戻る**(デモの状態を見ず `demo` だけで無条件にスキップしていたのは
  レビューで見つかったバグ — 一晩放置されるところだった)。`_send_run()`
  (START・SEEK・RESUME・NEXT が通る唯一の経路)は再生中の機体を
  `{"ok": false, "error": "playing a demo - press STOP first"}` として報告し、
  そもそも `/show/run` を送らない(機体自身も 409 で拒否するが、PC 側も
  同じ判定を先にしている)。「left alone」の記録はエピソードごとに 1 回だけ
  (`_demo_told`、毎回のポーリングでは出さない)。Units タブの Show 行は
  そのとき `✗ old version` の代わりに `demo: <名前>`(`/status.show.demo_name`)
  と出す。STOP は今まで通りデモも含めて止める。
- 機体側(`ui/demos.py`・`ui/agent.py`・メニュー)は別セッションの実装分。
  この変更は凍結した契約に対して書かれており、そちら側のブランチが未マージの間は
  実機での疎通確認ができていない(`tests/test_fleet.py`・`test_conductor_server.py`
  は StubLink/実 IP を避けたスタブ相手のテストで独立に確認済み)。
- **フィックスラウンドで見つかったもの**(レビュー指摘、実装側の見落とし):
  デモ関連のテストの一部が `Fleet({})` の既定 10 台(`192.168.51.101…`)に
  実際に発信していた(空 dict は falsy → `default_units()` にフォールバックする
  ことを見落としていた) — 到達可能な実機がなければタイムアウトで気づかないまま
  本番機に触れる危険があった。`Fleet({"radxa-01": "127.0.0.1:1"})` のように
  ローカルの未使用ポートへ差し替えて修正。`python -m pytest -q` 全件 pass
  (LAN に一切出ないことを目視でも確認)。

**マップが制作サイトの行ごとの shift を持つように(2026-09-24)**

- 制作サイト(vglabjp.synology.me の配線ページ、csvMap())の実際のルールは「中央セル(穴アドレスに
  `C`)を持つ行は shift 0、それ以外の行は shift 0.5」で、`conductor/look.py` の
  `default_shift()`(奇数行 0.5・偶数行 0)とは食い違う行があった: AZ271SD1301 の 33 行、
  AZ271SC6302 の 19 行、AZ271SB2303 の 24 行、AZ271SD1307 の 1 行(front 32)。サイト製のデザイン
  グリッドは行ごとの shift を自分で運ぶので正しく描けていたが、Wiring 表示・デザイン未投入のサムネ・
  こちらで作ったサンプルデザインは奇数/偶数の仮定のまま段違いに描いていた
  (AZ271SD1306・AZ271SD1305・AZ271SD1305_B の 3 点は元々ズレ 0 件で影響なし)。
- `LookMap` が `side,row,col,board_no,socket,label` に続く 7 列目 `shift` を任意で読むようになった
  (無ければ全行 `default_shift()` のまま、6 列の既存マップは無変更でロードできる)。
  `LookMap.shift(side, row)` はマップの値、無ければ `default_shift(row)` を返す。
  `Design.shift()` は変更なし(デザイン自身の shift が常に優先)。
- `Workspace.state()` の `map` payload に `shifts`(`"side|row": 0.5` の形、`default_shift()` と
  一致する行は送らない)を追加。ページの `renderGarment()`/`shiftOf()` は `opts.shifts` が
  null(デザイン未選択、`wornAt()` の初期値)のとき `item.map.shifts` にフォールバックするので、
  Wiring 表示とデザイン前のサムネもマップの shift で描かれる。
- `tools/make_sample_grids.py` のサンプルグリッドも `default_shift(row)` ではなく
  `look_map.shift(side, row)` から shift 列を書くようにした。
- サイトの配線ページから 7 枚の `<item>_map.csv` を作り直すツール `tools/site_maps.py`
  (`scratchpad/site_maps3.py` が元、shift 列を追加しただけで座標の計算式は同一)を追加。
  新旧マップは shift 列以外すべて一致することを確認済み(行数・side/row/col/board_no/socket/label
  とも 0 件の不一致)。実際に `showdata/files/` へ反映するのは別途。
- `conductor/preview.py`(CLI の PNG プレビュー、配線ビュー)と `conductor/sequence.py` の
  `ranks()`(重心 `center` の x 座標)も `look_map.shift()` に切り替え済み(コミット
  09d205c、2026-09-24)。マップに shift 列を持つルック(AZ271SD1301 ほか)では重心の値が変わり、
  ランクの入れ替わる鱗が出る(AZ271SD1301 は 1482 枚中 215 枚)。`tests/test_sequence.py` に
  `test_centre_uses_the_maps_own_shift`(shift 列ありのマップと無しのマップで `ranks()` を
  比べる)を追加済み。

**Timeline: 赤い再生ヘッドがシーク操作そのものに(2026-09-24)**

- ドックのスライダー(ドック全幅)とトラック上の再生ヘッド(名前列 150 px の右から)が同じ時刻で別の x に
  あって直観的でなかった。`#ph-head`(三角+時刻ラベル、pointer capture でドラッグ)と `#ruler` の
  押下→スクラブを追加し、スライダー `#ph` は Simulator view のときだけ描く。三角は線の中心に合わせ、
  ラベルは絶対配置で右に吊る(フレックスで一体にすると三角がずれる)。ドラッグ中は `seeking` で
  `tick()` を待たせ、音声のシークは 250 ms 間隔・離した瞬間に確定。←/→ 1 s、Shift で 5 s、Home。
- 直前の変更: ドックの自動サイズ(トラックの下に残る高さ、90〜360 px)と上端つまみでの手動サイズ
  (`tl.dockCells`、ダブルクリックで自動へ)。

**Timeline の組み立て直し: 下部ドック・横並び EDIT CUE・EDIT CUE からデザインの遷移を編集(2026-09-24)**

- 1366×768 でキューを選ぶと THE LOOKS AT が y=890(画面外)まで押し下げられていた。THE LOOKS AT を
  `#tl-dock`(position: fixed、下端)にし、compact(≈167 px、ルック枠 90 px)/ collapsed(≈44 px)/
  Simulator view(全画面)の 3 状態。`ui.dock` を localStorage `tl.dock` に保存。`#content` の下余白と
  `--tl-dock-h` は実測(`syncDockSpacing()`)。
- `#tl-editing` は 1400 px 以上で `minmax(0,1fr) 400px` のグリッド、EDIT CUE(`#tl-editor-card`)は
  sticky・最大高さはドックの上まで。見出しクリックで折りたたみ(`ui.editorOpen`)、Esc で選択解除。
  側面カラムでは各行の説明文をコントロールの下に回す。
- EDIT CUE の Transition 行を Refresh 行と同じラジオ対に: 「this design」は Designs タブと同じ
  `POST /api/transition`(同じ data-tr-seq/data-tr-span 属性で既存ハンドラを共用)、「this cue only」は
  従来の custom。どちらで変えても `refresh()` 後に両タブ・CUES 表・シミュレーターが揃う。

**UI の 16 色を実機見本色に(2026-09-24)**

- 制作サイト(vglabjp.synology.me)の色表が `PAL_VER 260921`「表示色 = 実機見本(肉眼)色」に更新された
  のに合わせ、`conductor/look.py` の `PALETTE` の RGB を同じ 16 色にした(白 #89ADC3、黄 #B4AE40、
  青 #005CB6、赤 #72473B、黒 #1A3757、緑 #438372、ターコイズ #76944C、アーモンド #777A65、
  ライトピンク #707070、スカイブルー #2473B3、オレンジ #815242、黄緑 #86AE59、オリーブグレー #3C8374、
  ブラウン #7E553F、ダークブラウン #6C634B、スモーキーブルー #387793)。コードと名前は FW_260917 の
  チャートのまま(基板に送るのはコード)。機体の LCD 用 `host/epaper/pattern.py` の RGB は変えていない。
- ページ(Designs の描画・Timeline / Units のサムネイル・色一覧)は `/api/state` の `palette` を
  そのまま使うので追加変更なし。テスト `test_palette_names_the_fw_chart_and_shows_the_site_sample_colours`。

**シミュレーターに E-paper の書き換え中の見え方(2026-09-24)**

- 実機 AZ271SD1307 の動画(ソケット順で 2 回書き換え、各約 7 s)を 0.25 s 刻みで観察し、
  鱗 1 枚の相を τ/R(R = キューの書き換え時間)で定義: A 0–0.13 濃紺/紫、B 0.13–0.50
  灰・青・黄・茶・生成りのランダム(0.25 s ごとに変化)、C 0.50–0.72 黄・茶・灰寄り、
  D 0.72–0.95 目標色を #b8a030 側へ 0.45→0 で混色、E ≥0.95 目標色。
- `index.html` の `wornAt()`/`sweptColors()` に実装。鱗ごとの開始 = `cue.sent` + 遷移の遅延
  (rank × span / maxRank)+ 0–0.35 s の決定的ジッタ(鱗番号のハッシュ)。色は 32 bit の整数
  ハッシュで選び、フレームループに文字列生成や `Math.random` はない。item+cue ごとに遅延・
  ジッタを `Float64Array` に前計算(`refreshModelCache`)。`worn.colors` の値は従来のパレット
  コードに加え `"#rrggbb"` 文字列を許し、`fillFor()` で塗りに変換(Designs タブの描画も同じ経路)。
- 計測: 7 ルック 7,250 鱗で `sweptColors` ≈ 0.3 µs/鱗、更新 1 回 ≈ 2.5 ms(100/250 ms 周期のまま)。
- 既知の差: `cue.complete` はジッタを含まないので、最後に始まる鱗の相 D が最大 0.35 s 早く
  目標色に置き換わる(色が間違うことはない)。仕様の再開メモ: scratchpad/plan_refresh_sim.md。

**SEEK のレビュー指摘の修正: 覚えた開始位置の検証漏れ、供給側の競合、スキップされたキュー(2026-09-24)**

- **START が検証していなかった経路を修正(重大)**: `from_s` を渡さない START は
  `fleet.start_at`(SEEK が覚えた位置)をそのまま使い、`show_duration()` との
  範囲チェックを一切していなかった。ページは `from_s` を送らない経路しか使わない
  ため、実際には**常に無検証**だった: 12:00 の位置まで SEEK → 5:00 のショーへ
  再アップロード → START で `"Started from 9:00."` の 200 が返り、全機体が最初の
  ティックで ENDED になって何も発火しない、という筋。`fleet.start_show()` が
  `at`(呼び出し側が解決した値、SEEK の再読み込みはしない)自身を必ず範囲検証する
  ように統一し、`fleet.upload()` も新しいショーの受け入れ時に `start_at` を忘れる
  ようにした
- **既存の run に紛れ込む `start_at` を除去**: `snapshot()` は run が無いときだけ
  `start_at` を返す(run 中は常に 0)。`_adopt()`(再起動した conductor が機体から
  実行中のショーを拾う経路)も拾った瞬間に `start_at` を忘れる。以前は拾った直後の
  ライブなショーに「START FROM 3:00」+「Back to 0:00」が出て、後者を押すと
  `seek(0)` の実行中分岐で T0 が未来に飛び、観客の前で機体が最初からやり直す
  ことがあった
- **機体側(`ui/showplay.py`)の修正**: SEEK/NEXT で T0 を前方へ大きく動かし、
  読み込み中/待機中/セット済みだったキューをその新しい T0 が追い越すと、以前は
  そのキューがそのまま予定時刻で発火していた(飛ばしたはずの絵が一瞬映る)。
  `ShowPlayer.run()` は新しい T0 のもとでそのキューの `sent` が既に過去になって
  いれば解除し、`_plan()` の「今のキューを塗り直す」分岐がブロックされたままに
  ならないよう、その分岐の "busy" 判定も今・次のキューだけを見るようにした。
  **この修正は機体側の再デプロイが要る**(各 `radxa-NN` で `git pull` して
  `epaper-ui` を再起動)。後方への SEEK(飛ばさない)は従来どおり再スケジュール
  されるだけで、この修正の影響を受けない
- **供給側(`_supervise()`)の競合**: `run` をロック外へコピーしてから
  `/show/run`・`/show/hold` を送るまでの間に SEEK/RESUME/STOP が割り込むと、
  古い T0 を後から送ってしまう(あるいは HOLD の後に RUN が届いて機体だけが
  動き続ける)ことがあった。`run` への書き込みごとに増える世代カウンタ
  (`_run_gen`)を追加し、実際に送る直前にロックを取り直して世代が動いていないか
  確認、動いていれば送らない。`_send_run()` 自身もロックを取り直して
  `run["state"] == "running"` を送信直前に再確認する(seek/stop・seek/hold の
  競合が典型)
- SEEK の丸め(0.1 秒単位)だけで境界を外れて拒否されないよう、ショーの長さの
  ちょうど 0.1 秒以内はショーの長さへ丸める(719.96 秒のショーで 719.96 を
  指定すると 720.0 に丸まって拒否されていた)
- HOLD 中・START 待ちの SEEK にもそれぞれの状態に応じたメッセージを追加
  (`"On hold at 1:30. RESUME continues from here."` / `"START will begin at
  1:30."`)。何もアップロードしていないのに機体から実行中のショーを拾っている
  ときの SEEK は `"This conductor did not upload the show - Upload first."`
  (`mode: "none"`)と正直に答える
- 巨大な JSON 整数(`10**400` 桁)は `float()` が `OverflowError` を投げ、
  今までは 500 相当(接続が切れる)になっていたのを 400 に統一
- 変更ファイル: `conductor/fleet.py`・`conductor/server.py`・`ui/showplay.py`、
  テスト一式(`tests/test_fleet.py`・`tests/test_conductor_server.py`・
  `tests/test_showplay.py`)

**SEEK(手動でショーの再生位置を動かす)・Designs 一覧のレイアウト・LOOK サムネイル行(2026-09-24)**

- **`POST /api/fleet/seek`**(新規): `{"to_s","manual":true,"lead_s"}` でショーの位置を
  動かす。実行中は全機体の T0 を動かして `/show/run` を送り直し(`mode: "running"`)、
  HOLD 中は保留した位置だけを動かして何も送らない(`mode: "holding"`)、未実行なら
  次の START が始まる位置を覚えるだけ(`mode: "start_at"`)。`manual` が JSON の
  `true` でない限り拒否(`"true"` や `1` も不可)。範囲外は
  `"The show is 0:00 to 12:00."` の 400、未アップロードは 200 で
  `"Nothing uploaded yet - Upload first."`
- **`POST /api/fleet/start` を拡張**(後方互換): `from_s` を渡すとその位置から
  始める(渡さなければ SEEK が覚えた位置)。`from_s > 0` は同じく `manual: true` が
  要る。成功した START・STOP はどちらもその位置を忘れる(次の START は 0:00 から)
- **`GET /api/fleet` に `start_at`・`show_duration` を追加**(常時。未アップロードは
  `show_duration: null`)。フリート未設定時のフォールバックにも同じキーを用意
- **Designs タブのデザイン一覧を表・2 列グリッドから 1 デザイン = 2 行の帯に変更**
  (1366 px 幅でも横スクロールなしで Transition の操作まで届く)。同じ操作を
  ガーメント上のツールバーからも(選択中のデザインのみ)。Units タブに手動シークバー
  (MANUAL CONTROL チェックで解禁)と、LOOK ごとの小さいサムネイル行を追加。
  これらの画面側(`conductor/web/index.html`)は別担当の実装
- 機体側(`ui/*.py`)は変更なし。SEEK は既存の `/show/run`(T0 を渡すだけ)を
  そのまま使う

**キューの時間の意味を統一: Start・Complete・End、キューごとの書き換え時間(2026-09-24)**

- **`at` = Start(指令を送った瞬間、e ペーパーが書き換えを始める時刻)に統一**。`align`
  (`"done"`/`"start"`)は廃止。**Complete** = Start + 書き換え時間 + スイープの Span、
  **End** = 同じアイテムの次のキューの Start(最後のキューはショーの終わり)。
  `/api/state` の各キューは `sent`/`complete` に加えて `end`・`end_source`
  (`"next"`|`"show"`)を返す
- **キューごとの書き換え時間の上書き**: `cue.refresh_s`(null か 1〜60 s の数値、null =
  ショー既定値)。有効値は `cue.refresh` / どちらを使ったかは `cue.refresh_source`
  (`"show"`|`"cue"`)。範囲外は保存時にクランプせず `validate()` の問題として警告。
  ユニット向けの書き出しファイル(`showfile.build_unit_show`)の各キューにも実効値を
  `refresh_s` として持たせ、同じ瞬間に複数アイテムが重なる場合は一番遅い値を採用。
  `ui/showplay.py` はショー既定より先にキュー自身の `refresh_s` を見る
- **旧 `align` 付きの show.json は自動移行**: `Workspace` が一度だけ、`align: "done"` の
  キューの `at` をその時点の書き換え・スイープから逆算して Start に付け替え、
  `align: "start"` はキーを外すだけ。undo 1 手にまとまり、2 回目の読み込みでは何も
  変わらない(冪等)。エクスポート/インポートも同じ移行を通す
- 同じアイテムの次のキューが前のキューの Complete より前に始まる場合は
  「starts before the previous picture is complete」と明示(バス間隔の警告と二重には
  出さない)
- レビュー指摘の修正: インポートの `transitions` が壊れた値でも `/api/state` を落とさない
  よう検証・無害化、`{"at": null}` 等の壊れた JSON は 400(500 落ちを修正)、
  `showfile`/`validate()` の「スイープするか」の判定を 1 箇所(`timeline.sweeps()`)に統一、
  `set_transition` も 30 s 上限を強制、BGM 配信のキャッシュヘッダ(ETag・長期キャッシュ)と
  拡張子チェック、アップロードのファイル名を unquote、`save_music` は先にポインタを
  commit してからファイルを差し替え(失敗時に旧ファイルを消さない)、ユニット側は配送
  テーブル送信失敗時のキャッシュ忘れと `delay_unit_ms` 不一致の拒否を追加
- 変更ファイル: `conductor/timeline.py`・`conductor/server.py`・`conductor/showfile.py`・
  `ui/showplay.py`・`ui/runner.py`、テスト一式。表示側(`conductor/web/index.html`)は
  別担当

**アイテム名を制作サイトの型番に揃えた(2026-09-24)**

- 制作インデックス https://vglabjp.synology.me/az27ss/ が更新され、配線ナビの書き出しファイル名の
  先頭が **型番**(`AZ271SD1301` など)になった(`look` フィールド = 型番)。デザイナーの CSV は
  `AZ271SD1305_color_<name>_grid.csv`、`AZ271SD1305_B_map.csv` の形で届く
- ワークスペースのアイテム名を `Look19` → `AZ271SD1301`、`Look20` → `AZ271SC6302`、
  `Look20-Skirt` → `AZ271SB2303`、`Look22` → `AZ271SD1305`、`Look22-2` → `AZ271SD1305_B`、
  `Look23` → `AZ271SD1306`、`Look24` → `AZ271SD1307` に改名(ファイル名の付け替え + show.json を
  1 手で更新。旧ファイルは一時フォルダに丸ごと退避)。以後、サイトの CSV は `Add CSV` /
  ドロップでそのまま正しいアイテムに入る
- **AZ271SD1305_B(LOOK 24)にはサイトに専用の配線ナビができた**(`/az271sd1305b-wiring/`、基板
  119〜134)。ソケットの配線は SD1305 と 1 枚も違わず、基板番号だけ違うことを確認。手で付け替えて
  いた基板番号は、サイトの map をそのまま使う形にして解消(`boards` の上書きは削除)
- 他 6 着の配線データは 09-21 取得分と差分なし。AZ271SD1304(旧 LOOK 21)はサイトでも非公開(制作
  取りやめ)
- 現場の予定: ショーは 2026-09-29 19:00 パリ(日本時間 9/30 02:00)
**演出側: デザイン単位の遷移・10 ms 単位の遅延表・BGM アップロード・保存/読込(2026-09-24)**

- **手動 Prepare も遷移を運ぶ(2026-09-24 追記)**: `compile_units()` がデザインの遷移から
  `delays` を組み立てて `/prepare` に同梱(以前はタイムライン経由のみ)。機体側 `runner`
  は遅延表を基板に保存/消去するたびにログへ 1 行出す。radxa-01 の実基板(FW_260923)で
  「board 1: sweep table saved, 60 sockets, last starts +3.00 s」→ 発火 +1 ms を確認
- **遷移(sweep)をデザイン単位に**: `show.json` に `transitions: {デザインファイル名:
  {sequence, span_s}}` を追加。キューは既定で自分のデザインの遷移を継承し(`transition:
  "design"`)、`transition: "custom"` のときだけキュー自身の `sequence`/`span_s` を使う。
  切り替えても両方の値は捨てずに保持する。`/api/state` は `item.designs[n].transition`
  (常に存在、既定は natural/0)と `cue.sweep` (`{sequence, span_s, source}`)、
  `state.transitions` を返す。新規 `POST /api/transition` (undo 対応)
- **`span_s` の意味を変更**: 「最初の柄が変わる指令から最後の柄までの秒数」に統一
  (旧 `step_s` ×段数 のような掛け算はしない)。30 s 超はキューの問題として警告
  (ファームウェアの上限)、`clean_span()` は 0〜120 s にクランプしジャンクは 0 に
- **遅延表を 10 ms のフレーム単位・uint16 に**: `conductor/sequence.py` の
  `compile_delays()` が 64 ソケット × uint16 ビッグエンディアン(128 バイト、
  `struct.pack(">64H", ...)`)を作る。`NO_DELAY = 0xFFFF`。最終ランクは必ず
  span_s ちょうどのフレーム数に一致(テストで assert)。ショーファイルに
  `delay_unit_ms: 10` を追加。`ui/runner.py._save_delays` は
  `struct.unpack(">64H", ...)` して 0x1F(`save_pipeline`)/0x25(`clear_pipeline`)
  にそのまま渡す(フレーム値は掛け算なしで直接使う)。`ui/remote.py` は 128 バイト
  以外の表を拒否(旧 64 バイトの表も含めて)
- **ショーの BGM**: `POST /api/music`(生バイト、`X-File-Name` ヘッダ、256 KB ずつ
  `<workspace>/music/*.part` に書いてから `os.replace`、`Workspace._lock` は
  ストリーミング中は取らない、64 MB 超は読む前に拒否)、`POST /api/music/remove`、
  `GET/HEAD /api/music/file` は Range 対応(206/416、`Accept-Ranges`、
  `Cache-Control: no-cache`)。`state.music` は `{name, size, type, url}` か null
- **演出の保存とアップロード**: `GET /api/show/export` がタイムライン・遷移・ラベル・
  ユニット割当・ボード番号(音楽は名前のみ)を 1 つの JSON ファイルとしてダウンロードさせ、
  `POST /api/show/import` で読み込んだキーだけをまとめて 1 履歴として反映(触れなかった
  キー・音楽の実体はそのまま)。フォーマット/バージョン不一致は 400 で拒否し、
  ワークスペースにない項目/デザインは警告として返す(取り込み自体は成功)

**FW VERSION が V1.4(FW_260923)を見分ける(2026-09-24)**

- 0x29 では V1.1 と V1.4 の区別がつかない(どちらも答え、size/crc は再起動後 0)。V1.4 で増えた
  pipeline 系のうち無害な **0x25(スロット 19 の遅延表を消す)**を USB 直結の 1 枚にだけ送り、
  0x80 なら `V1.4 16-color (FW_260923+)`、0x83 なら `V1.1 16-color`、続けてフラッシュ記録
  (`flashed FW_260923 09-23 15:33` など)。記録が V1.4 より古ければ `record older than the board`
- 副作用対策: runner は各ワーカーの `_setup` で送信済み遅延表の記憶を捨て、次のキューで送り直す
- 開発環境: radxa-01 + 基板 1 枚(ユーザー指示 2026-09-24)。ルータ `yoshihirock.net` は LAN は生きて
  いるが WAN(上流 `VF_Grand_Haneda_CD`)が未接続でインターネットに出られない → PC は上流 Wi-Fi に
  いて、機体操作のときだけ `yoshihirock.net` に切り替える。機体は git pull できないので
  `git bundle` を scp して取り込む

**FW_260923 と、順序(Sequence)の実装を V1.4 に合わせる(2026-09-23)**

- メーカーから **FW_260923**(`FW/FW_260923/fw2029.09.23/MCB_16_0923.bin`、プロトコル V1.4)と、
  分段遅延の依頼への回答が届いた。回答: 0x1E は使用済み(「次スロットへ切替」、探査すると再生が
  始まる副作用あり)→ **0x1F**(逐段 pipeline、uint16、単位 10 ms フレーム、低位・高位の 2 フレーム)、
  **0x25** で表を消去、flash 保存、同値は同フレーム開始(真の同時も可)、最大遅延は 30 s 以内
- 機体側を 0x1F / 0x25 に切り替えた(`host/epaper/commands.py` の `save_pipeline` / `clear_pipeline`、
  `ui/runner.py` の `_save_delays`)。ショーファイルの表(0.1 s 単位、0xFF = 指定なし)はそのままで、
  機体が 10 倍してフレームにする。全 0xFF の表は 0x25。旧 FW は 0x1F に 0x83 → `no_sweep`
- **UPDATE FW で書き込む FW を選べる**: `FW/FW_*/**/*.bin` を全部列挙し、確認画面で
  **LEFT / RIGHT** が版、UP / DOWN が基板。既定は最新(FW_260923)。FW_260917 に戻せる。
  表示は `FW_260923/MCB_16_0923.bin` と `< 2/3 >`
- 回答書の勧め(未対応・要検討): 衣装の鱗には汎用型 0x06 を使う、マスタに 0x1A で従機数を設定。
  本システムはブロードキャスト 0x1D で同時発火しているので、当面は現状のまま。V1.4 では
  基板の FW を混在させないこと(全機 0923 に揃える)
- 未確認: 0923 の実機での書き換え時間、0x1F を使った順序の見え方(次に基板があるとき)

**機体のサブネットを 192.168.51 に変更(2026-09-22)**

- 現場のトラベルルータ(ASUS RT-BE3600 Go)の WAN 側が `192.168.50.x` だったため、ルータが LAN を
  `192.168.51.1` に自動変更し、固定 `192.168.50.10x` の機体に届かなくなった。PC に 2 つ目の
  アドレスを足す案は、Windows では DHCP と手動アドレスを両立できず不可(試して DHCP が切れた)
- 機体を **`192.168.51.10x`** に揃える(ユーザー決定)。`radxa/firstboot.sh` の `SUBNET`、
  `conductor/fleet.py` の既定、書き込みスクリプト・README を変更。firstboot は毎起動時に
  ホスト名から導いた住所を適用するので、各機で `git pull` → `sudo bash radxa/firstboot.sh`
  (または再起動)で切り替わる。専用ルータの LAN も `192.168.51.1` で運用する

**機体のローカルモードは基板 1〜60 を探索する(2026-09-22)**

- それまでローカルのスタンバイ・デモは ID 1〜20 固定で、21 枚の LOOK 28(radxa-06)では ID 21
  (基板 053)を探しもせず「19/20」と出ていた(ユーザー指摘)。`--boards` を与えないときは
  **探索**: 1 から上へ順に探し、最後に応答した基板の先 `EXPLORE_GAP` = 6 個が続けて無応答なら
  そこで打ち切る(21 枚なら 27 回の探索、約 10 s。途中の 1 枚が死んでいても止まらない)。
  表示の分母は「応答した最大の ID」(`runner.expected`、`panels online: 20/21`)。
  再探索で上端の基板が現れたら、その先 6 個を探索対象に足す。ショー PC から基板一覧が来たら
  探索は止めてその一覧に従う。`/status` の `boards` も探索結果(1〜最大 ID)を返す
- 未変更: FW 更新(`ui/updater.py`)と FW 版一覧(`ui/versions.py`)は ID 1〜20 のまま

**変化の順序(Sequence)と FW 依頼(2026-09-22)**

- 要望: 色が変わる順番を演出にしたい(現状の P01→P60 / 図心から放射状 / 上→下 / 下→上 /
  右→左 / 左→右)。行ごとの間隔は、いまの P01→P60 の 1 本あたり約 0.1 s と同程度。
- **いまの FW では不可能**: 基板は自分の 60 ソケットを固定順(P01→P60、約 0.1 s 間隔、
  全体約 7 s、部分更新でも約 7 s。実測済み)で変える。FW の「再生方向」は基板内部の並び用、
  「グループ開始遅延」は基板単位・1 秒刻み。PC 側の部分更新ウェーブは 1 段 = 書き換え 1 回
  (7 s +)になり、行ごと 0.1 s にはならない
- **メーカーへの FW 依頼書**(英中併記): `docs/FW_REQUEST_SEGMENT_DELAY.md`(PDF 版も同じ場所。
  Markdown → HTML → Edge の headless 印刷で作成)。新コマンド
  0x1E「分段遅延の保存」= `[slot][flags][64B 遅延表]`(index = ソケット、単位 0.1 s、
  0xFF = 指定なし)。0x1D で各ソケットが T0 + 遅延に開始。表なしのスロットは従来どおり。
  未対応 FW は ACK_INVALID_CMD(0x83)を返す(対応の判別に使う)
- **PC 側と機体側は先に実装済み**(FW が来ればそのまま動く):
  - キューに `sequence`(6 種)と `step_s`(0.1〜5.0 s)。`conductor/sequence.py` が map の
    座標から鱗ごとの順位を計算(行 = CSV の行、列 = CSV の列、放射状 = 前面の図心からの
    距離をグリッド単位で丸めたリング。左右は客席から見た向きで面ごと: 前は列を反転、後ろは
    そのまま。後ろの中心は前面の図心の真裏)
  - 順序つきのキューの所要時間 = 書き換え + span(最後の鱗の遅延)。`timeline.times` /
    `validate` が span を織り込む(次の書き換えまでの間隔にも、遅延表の書き込み時間にも)。
    span はサーバが map から計算して cue["span"] で渡す
  - ショーファイル: どれか 1 つでも順序つきなら全キューに `delays`(基板ごとの 64B 表、
    順序なしのキューは全 0xFF)と `span` を載せる。機体は前回と同じ表は書かない
    (`runner._delays_sent`)。0x1E に 0x83 が返った基板は `no_sweep` に入れて以後送らず、
    キューは従来どおり出す(`/status` の `no_sweep` で見える)
  - Timeline の再生位置プレビューは、順序どおりに変わっていく途中経過を描く
  - テスト: `tests/test_sequence.py` ほか
- EDIT CUE は Start / End の 2 つの時刻で編集できるようにした(順序つきは End 固定で Step を逆算)
- 未確認(FW 待ち): 実機での見え方、同時に到期した複数ソケットの扱い(依頼書 3.3 で FW 側の
  最小間隔に委ねた)

**LOOK 番号・型番、10 機体の枠、ドラッグでの並べ替え(2026-09-21 朝)**

- Designs タブの一覧を radxa-01〜10 の 10 枠にし、各アイテムに LOOK 番号と型番を併記。
  どちらも一覧の上で編集でき(`POST /api/label` → `show.json` の `labels`)、Undo 対象。
  Timeline / Units タブの表示名も `LOOK 25 · AZ271SD1301` の形に統一
- **並べ替えはドラッグ&ドロップ**(カードの `⠿`)。機体が着ているもの全部が一緒に動き
  (トップス + スカート)、間のものは 1 つずつずれる。Unassigned から落とすとその機体に
  加わる。割り当て全体を 1 回で置き換える `POST /api/arrange` = Undo 1 手。ページ全体の
  「CSV をドロップ」は、外からのファイルのドラッグにだけ反応するようにした。テスト 402 件
- `showdata` に全 7 点の雛形(`*_map.csv`)を用意した。出どころは制作インデックス
  https://vglabjp.synology.me/az27ss/ の各「配線ナビ」ページに埋め込まれた配線データで、
  ページ自身の対応表(`csvMap()`)と同じ計算で作った(既存の `Look22_map.csv` と行・順序とも
  完全一致)。関係者限定のデータなのでリポジトリには入れていない(`showdata/` は gitignore)
- **計画変更後の並び**(ユーザー指示)。ファイル名はサイトが書き出す名前のまま(デザイン CSV が
  その名前で届くため)で、LOOK 番号と型番は表示ラベル:

  | 機体 | LOOK | 型番 | ファイル | 基板 |
  |---|---|---|---|---|
  | radxa-01 | 23 | AZ271SD1305 | `Look22_*` | 16 |
  | radxa-02 | 24 | AZ271SD1305(同じ型を 2 着) | `Look22-2_*`(独立したアイテム) | 16(基板 119〜134) |
  | radxa-03 | 25 | AZ271SD1301 | `Look19_*` | 27 |
  | radxa-04 | 26 | AZ271SC6302 (Tops) + AZ271SB2303 (Skirt) | `Look20_*` + `Look20-Skirt_*` | 16 + 16(スカートが DIP 1〜16、トップスが 17〜32) |
  | radxa-05 | 27 | AZ271SD1306 | `Look23_*` | 22 |
  | radxa-06 | 28 | AZ271SD1307 | `Look24_*` | 21 |

  AZ271SD1304(サイトの Look21、36 枚)は今回のショーに無い。朝に入れた `Look21_map.csv` は
  `showdata` から外した(必要になればサイトの配線ナビから同じ手順で作り直せる)
- **同じ型を 2 着 = 独立したアイテム 2 つ**(ユーザー指示: LOOK 23 と 24 で CSV は共有しない。
  色パターンも演出タイミングも別になりうる)。カードの Duplicate は map CSV を別名でコピーする
  だけ(`Look22-2_map.csv`、`POST /api/duplicate`)。途中で作った「ファイルを共有する複製」
  (`copies` / `copy_boards` / 専用デザイン)は廃止して消した
  - 同じ名前で届くデザイン CSV は、アイテムを選んで `Add CSV to this item` から入れると
    そのアイテムの名前に付け替えて保存される
  - **基板番号はどのアイテムでも画面で書き換えられる**(`POST /api/boards` → `show.json` の
    `boards`、CSV は触らない、重複・範囲外は拒否、Undo 対象)。CSV を後から差し替えて番号が
    衝突したら、書き換えを無視して警告を出す(2 枚の基板を 1 枚に混ぜない)
  - LOOK 24 = `Look22-2`、radxa-02、基板は AZ271SD1304 用だったものから 16 枚: 017〜032 →
    **119〜134**(どの 16 枚か決まったら画面で直す)。デザインはまだ 0(LOOK 23 の 3 つは
    LOOK 23 だけのもの)。テスト 406 件
- 一覧から機体名の見出しを外した(カードのプルダウンと二重だったため)。空の機体だけ、
  ドロップ先として細い点線の枠に機体名を出す
- **注意**: `Look19`(LOOK 25)の衣装にはヨーク(穴アドレスが `FY-..` / `BY-..`)がある。サイトの
  書き出しは穴アドレスの先頭が `F` かどうかで前後を決めているため、前ヨークが `back` になって
  後ろヨークと衝突し、グリッド CSV からはヨークの行が抜ける。こちらの雛形はデータの `side` を
  使って前ヨーク = front にしてある。サイト側が直るまで、この衣装のデザイン CSV はヨーク行が
  欠けて届く見込み

**夜間の自律ブラッシュアップ(2026-09-21 04:40〜、ユーザー不在・レビューチーム運用)**

- **UI を全面英語化**(Web UI と、UI に出る検証メッセージ)。タブは Designs / Timeline /
  Units。3 タブを実表示して日本語 0 文字を機械確認
- **起動ショートカット `Start Conductor.bat`**(リポジトリ直下、`serve --open`)。二重起動は
  ページを開くだけ。Windows では SO_REUSEADDR のせいで同じポートに 2 つ目のサーバが
  立ててしまえたので、ポートを排他にし、起動前に稼働中かを確認するようにした
- **レビューチーム**(読み取り専用・機体/ネットワーク接触なし): 機体側の並行処理 = Opus、
  PC 側 = Sonnet、Web UI = Sonnet、文書整合 = Haiku、修正の 2 次レビュー = Sonnet。
  2 巡で指摘は約 50 件。採用して修正したもの(回帰テスト付き):
  - 機体: HOLD/STOP 時に「基板へ保存中」のキューが発火してしまう / `stop()` の join が
    タイムアウトするとワーカーが二重になり表示命令を 2 回送り得る / 一部の基板が保存に
    失敗しても「適用済み」扱いで以後差分だけ送り続ける(→ `dirty` として次の機会に
    全体像を 1 回送る)/ ショーファイルの非アトミック保存 / エージェントの接続に
    タイムアウトが無くスレッドが漏れる / RTC 無しで復元 T0 が未来になる / 不正な
    ショーファイルを受理してしまう / プレイヤーのロックを握ったままポートを取りに行く
  - PC: **STOP したショーが、STOP を聞き逃した 1 台のせいで全台再開してしまう** /
    送信時刻の float 等値比較 / CSV の重複列・重複位置の黙殺 / 1 回の外れ値で時計履歴を
    破棄 / GET の例外未処理 / show.json の非アトミック保存 / START の二重クリックで T0 が動く
  - UI: **`saveShow` の引数 `refresh` が再読込関数を隠し、書き換え時間を設定値にして以降
    タイムライン編集のたびに保存後の再描画が例外で落ちていた**(ページを実操作して発見)/
    ショー操作ボタンの状態連動と二重送信防止 / 機体由来文字列の未エスケープ / Release に確認 /
    キューのドラッグ移動(5 秒刻み)と「Copy time to all items」を追加
  - 2 巡目(1 巡目の修正が開けた穴): ロック外に出した `_send` が HOLD / STOP / 再配布と
    競合して発火し得る(→ コマンドごとの epoch。発火時刻の設定はロック内・同一 epoch のみ)/
    最後のキューが死んだ基板で失敗し続けると ENDED にならない / 同じショーを頭から
    再 START したときに前回のセッションキーと衝突し得る(→ ラン番号をキーに含める)/
    conductor 再起動直後は STOP ボタンが無効 / ドラッグ直後のクリックで下のトラックに
    キューが追加される / STOP を聞き逃した機体への停止指令は 1 回だけにする
  - 見送り(理由): `_corrected` のロック(GIL 下で実害なし)、壊れた map の基板数が最短間隔の
    計算から抜ける件(壊れた map は配布自体を止めるので到達しない)
- **実機(radxa-01 = 基板 ID:1、radxa-10 = 基板 ID:2)**:
  - 2 台で同じショー(計 4 回): 送信時刻の差 0.1〜3.3 ms(時計精度の上限 ±5 ms)、
    各機の発火は指定の瞬間から +0.8〜5.0 ms。最終版では ENDED まで完走し、頭からの
    再 START でも全キューが流れ直すことを確認
  - HOLD / RESUME / NEXT を実機で確認(スクリプトからも、**実際の画面のボタンからも**)。
    START の 2 連打でも T0 は動かない。STOP 後に勝手に再開しない
  - ショー途中で radxa-10 の UI プロセスを再起動 → ディスクから自力復帰し、全体像を出し直して
    次のキューに 2 台そろって合流(差 0.6 ms)
- 配布: radxa-01 / radxa-10 のみ最新 `8525eff`(機体上で 398 件の通過を確認してから再起動)。
  **radxa-02〜09 は `9f0ac66` のまま**(基板未接続。ユーザー不在中は触らない方針)。
  PC 側との互換性は保たれているが、今回の機体側修正を入れるには各機で `GIT PULL` が要る
- テスト 398 件(Windows、radxa-01 / radxa-10 の Python 3.9 とも全通過)
- **お詫び**: UI の操作テストで一度、ユーザーのタイムラインを 1 手巻き戻してしまった
  (直後に Redo で完全に復元。キュー 8 個・履歴とも元どおり)。その名残で「Redo」に
  テスト用のドラッグ操作が 1 手残っている(押すと 8:00 のキューが 8:40 に動く。次に何か
  編集すれば消える)。以後、UI の操作テストは一時ワークスペースの別ポートで行った
- 既知の注意: `tests/test_show_e2e.py` の再起動テストが、全テストを並列負荷の高い状態で
  流したとき一度だけ落ちた(単独・負荷下の再実行では再現せず)。ベンチでは不在基板 15 枚の
  探索に約 30 秒かかるため、再起動直後の追いつき表示が「+20 秒遅れ」と表示される
  (基板が揃う本番では発生しない)

**ショー制御 P2: タイムラインの配布と自律実行(2026-09-21)**

- `conductor/showfile.py`: タイムライン → 機体ごとのショーファイル。同じ機体・
  同じ瞬間のキューは 1 つにまとめ、各キューは機体の全基板に書く(変えない基板は
  全 0xFF)。`boards`(差分)と `state`(そのキュー後の全体像)を持つ
- `ui/showplay.py`: 機体側プレイヤー。T0 を受け取ったら自律実行。HOLD / RESUME /
  NEXT は T0 の移動だけ。途中参加・再起動・飛び越しは `state` を送って 1 回で復帰。
  ディスク保存(`~/.epaper/show.json`, `show-run.json`)から再起動後に合流
- `conductor/fleet.py`: 配布 / プリセット / START / HOLD / RESUME / NEXT / STOP、
  各機体の T0 と版の監視と自動修復、PC 再起動時の T0 復元。UI は機体タブ上段
- P1b の実機確認(radxa-01 + 基板 ID:1): 準備 → GO が +1 ms で発火、全面(P01)と
  一部更新(P03、0xFF の非更新)とも見た目に問題なし(ユーザー確認)。
  10 台にエージェント配布済み、時計精度 ±3〜4 ms
- テスト 369 件(Windows 全通過)
- 次: 実基板で短いショーを通す → 複数基板(3 枚以上)のバス確認、保存時間の実測、
  本番ルータでの同期確認、バッテリー運用の確認(STATUS 3 章)

**ショー制御 P1b: 機体エージェント + 時計同期 + 機体タブ + 一斉 GO(2026-09-21、未コミット・未配布)**

- 機体側: `ui/remote.py`(2 段階キューのセッション)、`ui/agent.py`(HTTP、
  既定 8787、標準ライブラリのみ)、`DemoRunner.start_remote` / `_run_remote`
  (保存は既存の再試行・欠落基板スキップを流用、発火は monotonic 時刻に
  0x1D ブロードキャスト 1 回、発火後 guard_delay で停止指令)。LCD に `REMOTE` 画面、
  KEY2 で本体メニューへ。`ui.main` は既定でエージェントを起動(`--no-remote`)
- PC 側: `conductor/fleet.py`(機体ごとのポーリング兼オフセット測定、
  prepare / fire / cancel / standby / release を並列送信)、
  `Workspace.compile_units`(選んだデザイン → 機体ごとの配列、共有機体は
  通しアドレス)、UI の「機体」タブ
- 実測で直した 2 点: (1) エージェントの応答がヘッダと本文の 2 セグメントに
  分かれ、Nagle + 遅延 ACK で往復が一定 60 ms・戻りだけ遅い = オフセット誤差
  約 30 ms → 1 セグメント送信 + TCP_NODELAY。(2) Windows の `time.monotonic()`
  は 15.6 ms 刻み → PC の基準時計を `time.perf_counter` に
- 実 Wi-Fi の実測(radxa-01〜03、`/tmp` の一時コピーで実施し撤去済み、基板は
  偽バス): 往復 最小 5.6 / 中央 7.6 ms、測定精度の上限 ±3 ms、発火の遅れ
  +0.1〜0.7 ms、3 台の送信時刻の差 < 1 ms(5 回)
- テスト 350 件(Windows 全通過。機体側 + fleet + conductor の 88 件は Radxa の
  Python 3.9 でも通過)
- **未確認**: 実基板での保存所要時間と 0xFF(非更新)の挙動、ソケット N = 鱗 N、
  書き換え 7 秒の実測、3 枚以上のバス。radxa-01 は 2026-09-21 時点で
  `no serial port`(基板未接続)
- **既知の制約**: 機体の一部アイテムだけを変えるキュー(Look20 の上だけ等)でも
  表示命令はブロードキャストなので、変えていない基板も同じ絵を再表示する
  (7 秒の書き換えが見える可能性)。P2 で宛先指定の表示に切り替えるか実機で判断
- 次: 配布(push → 各機体 GIT PULL)→ 実基板で ①準備 ②GO → P2(タイムラインを
  各機体へ配布して自律実行、START / HOLD / PANIC、再起動後の復帰)

**ショー制御 P1a: ルックの CSV → 基板配列 + PC の Web UI(2026-09-21、未コミット)**

- 用途が確定: **ファッションショーの衣装**。1 ルック = Radxa 1 台をモデルが
  着用、最大 10 ルック。1 台あたり基板は最大 60 枚(意匠上は 35 枚程度)。
  PC から 10 台を時刻同期して制御する(キューは 10 分で 10 回以内、既定は
  「キュー時刻に書き換え完了」= 書き換え時間ぶん前に発火、キューごとに開始基準も選べる)
- `conductor/look.py`: `LookNN_map.csv` + `LookNN_color_patternMM_grid.csv`
  → 基板 ID ごとの 64 バイト配列。board_no は順位で DIP ID に変換。両ファイルの
  相互検証(`0`=鱗なし と `0x00`=白 の取り違え検出)、`--partial`
- `conductor/preview.py`: 仕上がりビューと配線ビュー(PNG)。
  `python -m conductor check|preview|dip|arrays|send`
- 実データ Look22 で確認: 鱗 862 枚 / 基板 16 枚(017〜032 → ID 1〜16)、
  map と grid は 862 対 862 で完全一致
- `conductor/server.py` + `web/index.html`: `python -m conductor serve`
  (標準ライブラリのみ、127.0.0.1 限定)。CSV のドロップ取り込み、検証、
  仕上がり/配線プレビュー(外側/内側の向き切替、基板ハイライト)、DIP 表、
  アイテム → 機体の割り当て。データは `./showdata`(git 管理外)
- **タイムライン**(`conductor/timeline.py` + UI の「タイムライン」タブ):
  アイテムごとのトラックに「時刻 → デザイン」のキューを置く。既定は
  「この時刻に完成」(書き換え時間ぶん前に送信)、キューごとに「この時刻に開始」も可、
  0:00 はプリセット。機体ごとの最短間隔(書き換え時間 + 基板数 × 0.22 秒 + 3 秒)、
  同一機体に載るアイテムのバス共有、デザインの有無・一部更新の整合を検証。
  再生位置での見え方プレビュー付き。保存先は `showdata/show.json`。
  Undo / Redo あり(`showdata/history.json`、最大 200 手、再起動後も有効)
- **書き換え時間の既定を 16 → 7 秒に変更**(2026-09-21、最新 FW で約 7 秒との
  報告)。ショーごとの設定値 `refresh_s` にして画面から編集可。最短間隔は
  基板 16 枚で 14 秒、36 枚で 18 秒。**Radxa 側 UI の値は未変更**
  (`--guard-delay 12`、SOLID 15 秒 / RANDOM 20 秒間隔): 7 秒でも安全側だが、
  デモを速く回すなら見直せる
- 先方 README(Look19)で確定: grid の `0` = 穴なし、`-` = 色未指定、
  **図は内側(体側)から見た向き**(プレビューは既定で反転)。幅はルックごと
  (Look19 は 31 列、Look22 は 27 列)
- 今回のショー(AZ-27SS): LOOK19〜24 の 6 ルック(7 アイテム、基板 154 枚、
  鱗 8,258 枚、最大は Look21 の 36 枚)+ バッグ 4 点 = Radxa 10 台。
  **Look20 はトップス + スカートで Radxa 1 台** → DIP ID は機体単位の通し番号
  (`unit_board_ids`)。バッグも同じ map + grid 形式
- **色表を FW_260917 の Excel に合わせた**(ユーザー指示): 0x05 = 緑、
  0x06 = ターコイズ。`pattern.py` / SPECIFICATION 3.1 / テストを更新。
  FW_260903 のままの基板ではこの 2 色が逆に出る
- テスト 319 件(Windows 全通過。look の変換テストは Radxa の Python 3.9 でも通過)
- 次: P1b(Radxa 側リモートエージェント + PC との時計同期 + 機体一覧 +
  一斉 GO)→ P2(`show.json` のタイムラインを配列化して各機体へ配布、
  自律実行、START / HOLD / PANIC)

**LCD に `REBOOT`(機体の再起動)を追加(2026-09-21、実機未反映)**

- メニュー末尾 `REBOOT`(`ui/rebooter.py`): 確認画面でホスト名を大きく
  表示し、**KEY1 の 1 秒長押しでのみ** `sudo -n systemctl reboot` を実行。
  短押し・ジョイスティックでは何も起きない(メニューでの連打対策)。
  受理後はボタン無効、拒否されたら `FAILED` + 理由で長押し再試行
- ランナーは止めない(拒否されればデモは続き、受理されれば OS が全部止める)。
  再起動後は通常起動と同じ待機(全面白)に入る
- 前提はパスワード不要 sudo。radxa-01 で `sudo -n true` と
  `/etc/sudoers.d/010_radxa-nopasswd`(`NOPASSWD:ALL`)を確認済み。
  ユニットに `NoNewPrivileges` は付いていない。Pi 予備機は 2026-09-21 時点で
  SSH 不達のため未確認
- テスト 262 件(Windows で全通過)。**配布は push → 各機体で `GIT PULL` →
  KEY1 再起動**(`requirements.txt` の変更なし)

**デモ `RND16+SOLID16` を追加(2026-09-20、radxa-01 反映済み)**

- メニュー `RND16+SOLID16`(key `loop16`): **RANDOM16**(60 セグメントに
  0x00〜0x0F をランダム配置、`grid.repair` で上下左右の隣接同色なし、20 秒)
  と **SOLID16RANDOM**(全パネル同一色、色は 16 色からランダム、直前と同色は
  選ばない、15 秒)を 1 サイクルずつ交互に繰り返す。滞在サイクル数は
  `LOOP16` の steps で変更可
- `SOLID16RANDOM` は単独のメニュー行にもある。色は「プロセス起動時の乱数
  シード + サイクル番号」から決めるので、LCD のキャプション(`0x0A Orange`
  など)とパネルの色が必ず一致し、サービス再起動ごとに順序が変わる
- 既存の `RANDOM16` にも隣接同色回避を入れた(以前は完全ランダム)
- テスト 246 件

**UPDATE FW から戻るときの白リフレッシュを廃止(2026-09-17、radxa-01 反映済み)**

- `DONE`/`FAILED` 後の KEY2 はメニューに戻るだけ。以前はここで白待機
  (全面白 + リンク確認、e-paper の全面書き換え 16 秒)を走らせていたが、
  画面を抜けるためだけの全面更新は不要とのことで削除
- 再起動した基板は工場デモを再生したままになる。止めるなら `STANDBY` 行、
  またはデモ開始。FW VERSION から戻る経路も同様に白待機なし(同日追記)
- **radxa-02〜10 を SSH で 1b57c6e に更新(2026-09-17 夜、全 9 台
  e489b84 → 1b57c6e、`epaper-ui` active)**。これで 10 台とも同一コミット。
  以後の配布は各機体の LCD `GIT PULL` → KEY1 再起動で足りる

**書き込み記録で「260917 か」に答える(2026-09-17、radxa-01 反映済み)**

- 基板は自分のビルドを言えない(0x02 不可、0x29 size=0)ので、ホスト側で
  記録する: UPDATE FW 成功時に **USB シリアル(STM32 UID)→ イメージ・
  size/CRC・時刻** を `~/.epaper/flash-log.json` に保存(`ui/flashlog.py`)。
  FW VERSION は USB 直結基板のシリアルで引き、`V1.1, flashed FW_260917
  09-17 17:19` / `V1.1, no flash record here` と表示。状態行に `USB <serial>`
- radxa-01 の記録は今朝の OTA ログ(08:15/08:19 UTC、両基板とも 0x28 後に
  再起動し 0x29 応答 = CRC 受理)から 48EC7570324C(ID:1)と
  48EB685C324C(ID:2)を FW_260917 として初期投入済み
- 限界: 記録はその機体で書いた分だけ。他機体や PC で書いた基板は
  `no flash record here`。本当の版照会はメーカーのコマンド追加待ち
- FW VERSION の状態行と各行は**折り返し表示**にした(`render._wrap`)。
  Radxa の DejaVu フォントでは `V1.1, flashed FW_260917 09-17 08:19` が
  1 行に収まらず「…」で切れていた。2 行になる行があるためページ送りの
  刻みは 5 行(1 行の行なら 10 行見える)

**485 中継 0x29 の wedge を特定、スキャンを中継安全に変更(2026-09-17、radxa-01 反映済み)**

- 実測(radxa-01、ID:1 USB + ID:2 485): 0x17 は中継で 0.05 秒 ACK、
  **0x29 を ID:2 宛に中継すると 6 秒無応答 → 以後 ID:1 の CDC が Write
  timeout(電源再投入まで復旧せず)**。0x29 は自分宛なら 485 接続中でも即答。
  旧「未解決: USB(ID:1)→485 方向の中継に応答が無く wedge」はこれで説明がつく
  (当時も 0x29 系を送っていた)
- 0x02(V1.0 の版照会)は FW_260917 でも全変種 ACK_FAIL 0x0A。0x29 の
  size/crc は再起動後 0。**版の識別は現状不可能** → メーカーに版照会
  コマンドを依頼する
- `ota.scan` を「0x17 で存在確認 → 1 枚だけなら 0x29」に変更。UPDATE FW は
  485 接続中(複数応答)だと照会も書き込みもせず `unplug 485` を表示、
  FW VERSION は複数枚を `on 485 bus` と列挙するだけ。テスト 239 件
- 運用: **FW VERSION / UPDATE FW は 485 を抜いて 1 枚ずつ**。485 接続中に
  0x29 を送る旧版 UI(160d6f0 以前)は使わない
- 描画の取りこぼしを修正: `App.draw()` が描画後に状態キーを記録していたため、
  描画中にワーカーの結果が確定するとその変化が「描画済み」になり画面が
  `01 answers...` のまま止まった(radxa-01 実機)。キーを描画前に取る順序に
  変更、回帰テスト追加

**LCD に `FW VERSION`(基板 FW の一覧)を追加(2026-09-17、radxa-01 反映済み)**

- メニュー行 `FW VERSION`(`ui/versions.py`): ランナーを止めて 1〜20 に
  0x29 を送り、応答した基板を `01  FW_260917` のように列挙。判定は
  `host/ota.py` の `identify()`: 同梱イメージ(`FW_*/*.bin`)の size/CRC16 と
  一致すればフォルダ名、size=0 なら `V1.1 16-color, build unknown`、
  ACK_INVALID_CMD なら `V1.0 6-color (no OTA)`
- **前提が未確認**: 0x29 の size/crc を基板が再起動後も保持するか。保持
  しなければ 16 色版は全部 `build unknown` になる → 基板を USB に挿して
  `FW VERSION` を一度走らせ、結果次第でメーカーにバージョン照会コマンドを
  依頼する
- テスト 229 件

**LCD にホスト名表示 + `GIT PULL` メニューを追加(2026-09-17)**

- 全画面の上部バーに `socket.gethostname()`(`radxa-01`〜`10`)を表示。
  メニューではタイトル位置、実行中/FW UPDATE/GIT PULL 画面では状態語の左
- メニュー末尾 **`GIT PULL`**(`ui/puller.py`): `git pull --ff-only` を
  ワーカースレッドで実行し、`now`/`new` のコミットとログを表示。HEAD が
  動いたら KEY1 で UI を再起動(プロセス終了 → `Restart=always` で復帰、
  sudo 不要)。`GIT_TERMINAL_PROMPT=0` と 180 秒タイムアウトで固まらない
- `--pattern` の headless 運用や `App` 単体には影響なし(puller 未接続なら
  行が出ない)。テスト 216 件
- 運用: 以後の配布は「PC から push → 各機体で `GIT PULL` → KEY1 再起動」。
  `requirements.txt` が変わる更新だけは SSH が要る
- **radxa-01 に反映済み**(2026-09-17、SSH で pull + `epaper-ui` 再起動、
  216 件通過)。radxa-02〜10 は最初の 1 回だけ SSH で
  `git pull && sudo systemctl restart epaper-ui`、以後は `GIT PULL` 行で

**メーカー更新 FW `FW_260917` を同梱、`UPDATE FW` が自動で選ぶ(2026-09-17)**

- 16 色の発色修正版(0x06/0x07/0x08 の乖離を報告した後の版)。ファイルは
  `FW/FW_260917/` にメーカー配布名のまま(`MCB_e16_2029.09.17.bin` 64188 B、
  同名 `.hex` は SWD 用、色見本 xlsx と写真 `image/` も同梱)
- `ui/updater.py` の探索を `FW_*/OTA_*.bin` から `FW_*/*.bin` に広げ、
  ファイル名に関係なく**最新フォルダの .bin** を出すようにした。テストは
  この構成(旧 OTA_16c.bin と新メーカー名の共存)を固定
- Radxa への配布は従来通り `git pull`。radxa-01 に反映済み(md5 一致、
  `epaper-ui` 再起動)。**基板への書き込みはこれから**: LCD の
  `UPDATE FW` で 1 枚ずつ、書き込み後に `SOLID16` で 0x06/0x07/0x08 を目視

**Radxa 10 台のクローン完了(2026-09-16)**

- radxa-02〜10 を `radxa/clone/write_card.ps1 -Unit N -Disk 2` で 1 枚ずつ
  書き込み、各機の起動を確認(ユーザー確認。03 以降は初回起動だけで
  ホスト名・SSH 鍵・machine-id・ルート FS 拡張・IP `.1NN` が自動で揃う)
- 台数分の作業はすべて同じ手順で、個体差はホスト名 `radxa-NN` だけ。
  ゴールデンイメージは `D:\radxa-golden\radxa-01-golden.img`
  (sha256 `62a49726…f14b`、リポジトリ e489b84 同梱)。追加・交換時も
  同じコマンドで作れる
- 全台を同時に起動しても IP は分かれる。SSH は `radxa@192.168.50.1NN`
  (01=.101 … 10=.110)。ホスト鍵は機体ごとに異なるので、初回接続時に
  known_hosts へ追加される


- `write_card.ps1 -Unit 2 -Disk 2` で書いたカードで radxa-02 が起動。
  ホスト名 `radxa-02`、SSH 鍵と machine-id は新規、ルート FS 28 GB、
  HDMI 接続のまま安定(CEC 対策が効いている)
- ただし IP が `.101` のままだった: `epaper-firstboot` が rsetup の
  ホスト名変更より先に走っていた(`rsetup.service` は oneshot ではないので
  `After=` は開始しか待たない)。`radxa/firstboot.sh` が rsetup の終了を
  待つよう修正(e489b84)。radxa-02 は手動再実行で `.102` に移行済み
- ゴールデンイメージ内のリポジトリを e489b84 へ fast-forward
  (WSL でループマウントして `git pull`)。sha256 は `62a49726…f14b`。
  **以降のクローンは修正版で初回起動する**。radxa-03 以降は
  `write_card.ps1 -Unit N -Disk 2` を繰り返すだけ


- radxa-01 にモニタを繋いだらログイン画面の後に電源が落ちた。原因は
  HDMI-CEC: `sunxi_cec` が電源ボタンとして登録され、モニタのスタンバイ
  信号で logind が poweroff していた(ジャーナルは 84 秒で綺麗な Power-Off)。
  `raspi/logind-appliance.conf`(電源/サスペンド系キーを ignore)を追加し、
  実機・ゴールデンイメージ・`setup.sh` に反映。本番はヘッドレスなので
  影響は無いが、ブリングアップでモニタを繋ぐと再発するため恒久対処
- ゴールデンイメージの sha256 が更新された(`D:\radxa-golden\radxa-01-golden.sha256`)

**Radxa 10 台複製の仕組み(2026-09-15)**

- 個体差はホスト名 `radxa-NN` だけ。IP は `radxa/firstboot.sh`
  (`epaper-firstboot.service`、毎起動・冪等)が `192.168.50.(100+NN)` に
  導出して Wi-Fi プロファイルへ適用する
- ホスト名と SSH ホスト鍵は Radxa 純正の `rsetup` が `/config/before.txt`
  (FAT、Windows から書ける)で設定する。手順は [radxa/README.md](../radxa/README.md)
  「10 台への複製」
- 開発機は `radxa-01` に改名済み。**ゴールデンイメージ作成済み**:
  `D:\radxa-golden\radxa-01-golden.img`(7.04 GB、sha256 は同名 .sha256)。
  元の全体読み出し `radxa-01-full.img`(31 GB)も同じ場所
- 書き込みは `radxa/clone/write_card.ps1 -Unit NN -Disk N`(WSL + 管理者
  Python)。Windows はカードのパーティションを見せず、`wsl --mount` は USB
  リーダー不可だったため、生ディスク I/O を `radxa/clone/rawdisk.py` で実装。
  スパース 29 GB ファイルへのドライランで GPT 修復・fsck・`resize_root`
  相当の拡張・`before.txt` 反映を確認済み。**実カードへの書き込みはこれから**

**LCD メニューから基板 FW を OTA アップデートできるようにした(2026-09-14、Radxa デプロイ済み)**

- メニュー末尾に **`UPDATE FW`** を追加(`ui/updater.py`)。FW イメージは
  リポジトリ同梱の `FW/FW_<yymmdd>/*.bin` のうち**最新フォルダ**を自動選択
  (当時 `FW_260903/OTA_16c.bin`、65544 B。現在は FW_260917)。Radxa への「コピー」は
  `git pull` で完了する(a6bd425 で同梱済み、md5 一致を確認)
- 操作: `UPDATE FW` で KEY1 → ランナー停止(ポート解放)→ 確認画面で
  **UP/DOWN で USB 直結基板の DIP アドレス**を選ぶ(0x29 で状態を照会し
  `IDLE …` / `no reply` / `ACK_INVALID_CMD`(旧 FW)を表示)→ KEY1 で書き込み
  → 進捗バー + ログ → `DONE`/`FAILED`。KEY1 でもう一度、KEY2 でメニューへ
  (**待機(白 + リンク確認)を再実行**し、再起動した基板の工場デモを止める)
- **宛先は自動スキャン**(同日追記): 確認画面に入ると 1〜20 へ 0x29 を
  1 回ずつ送り(0.3 秒待ち、最大約 6 秒)、**応答が 1 件ならそれを自動選択**
  して状態行に `IDLE … (auto)` と出す。UP/DOWN は上書き用に残す。複数応答
  (485 が繋がったまま)は `boards 01,20 answer: unplug 485 or pick one`、
  無応答は `no board answers 0x29`。CLI は `ota.py FW.bin --addr auto`
  (`--check --addr auto` で応答一覧)
- **実機検証済み(2026-09-14)**: LCD から 3 枚を addr 1 で更新(1093
  チャンク 65〜70 秒、全て 0x28 後に再列挙せず → 自動 xhci 再バインドで
  復帰 → IDLE)。**DIP 全 OFF の基板はアドレス 1 として応答する**
  (0 は PC のアドレス。仕様外の挙動で、485 バス上では本物の ID:1 と衝突
  するので組み込み前に DIP を設定すること)
- 書き込み中はボタンを全て無視(KEY3 消灯のみ可)。中断手段は意図的に無い
  (中断→再送がストール中の転送を殺す実測があるため)。サービス停止で
  プロセスごと落ちても、基板側は 0x28 前ならステージングのみで無害
- `host/ota.py` は `log`/`progress` コールバック化(CLI は従来通り)。
  Linux で 0x28 直後に出る `SerialException: device disconnected` は
  **成功の合図**として扱う(2026-09-11 の実測)
- 再起動後に基板が USB 再列挙しない場合(dmesg `error -71`)は
  **xhci-hcd を unbind/bind して自動復旧**(`usb_rebind`、`sudo -n` 前提。
  `--no-usb-rebind` で無効化)。それでも戻らなければ `FAILED` +
  「replug USB」
- `python -m ui.main --preview DIR` に `update_*.png` 4 枚を追加。
  テスト 30 件追加(計 195)

**SOLID16 デモ追加 + 新 FW はリフレッシュ中も応答する(2026-08-29)**

- メニューに `SOLID16`(全面単色で 0x00→0x0F を 15 秒間隔で一巡)を追加
- **新 FW(RTOS 版)はリフレッシュ中も ACK_SUCCESS を返し続ける**
  (show 後 35 秒間 0.75 秒間隔でポーリングし全て 0x80、黒・白の 2 回実測)。
  旧 FW の「リフレッシュ中 16 秒無応答」は解消。ただし電気的に
  リフレッシュ完了を検知する手段が無くなったため、**所要時間は目視計測
  のみ**(旧 FW 実測 16 秒、16 色波形で同等以上の見込み)。runner の
  deaf 前提のバックオフは新 FW では実質不要(害もない)

**DeviceType は NUMBER_BRAND(0x03)を採用(2026-08-29)**

- **GEN(0x06)と六角形(0x01)モードは新 FW でインデックス 12 を
  再描画しない**(基板1で実測: 12 だけ 0xFF・単独赤の最小フレームでも
  不変。仕様の蜂窝レイアウトのスペーサ位置 12/52 の名残とみられ、52 も
  同様の可能性が高い(ベンチでは 52 未結線のため未確認))
- 0x03 は仕様 5.5 の通り「インデックス 1〜60 = 番号 1〜60、61/62 詰め物」
  でメーカー README の P1-P60 記述と一致。UI の既定 dev_type を 0x03 に
  変更し、配列レイアウトは共通(build_gen_array)のまま
- 注意: 基板1の CDC は不調時に Radxa ごと巻き込んで OS を落とした
  (2026-08-29 朝、要電源再投入)。USB 異常ループを見たら早めに基板の
  電源を入れ直すこと

**全デモを GEN 形式 + 5×12 グリッドへ移行(2026-08-28、Radxa デプロイ済み)**

- 物理配置図は不要になった: セグメント 1〜60 は自由割当てで、現行は
  **横 5 × 縦 12** のグリッド(行優先、左上が 1)。幾何・隣接・リング・
  スパイラル順は `host/epaper/grid.py` が導出(隣接同色なし保証つき)
- STANDBY / SOLID / RANDOM / WAVE / GRADIENT / SPIRAL / MIRROR / 16COLORS
  すべて GEN(0x06)+ 新マーカーで送信。`Pattern` の既定が GEN になった
- パレットは `COLOR_NAMES_16`(V1.1 LUT)。**緑は 0x06 に移動**
  (0x05 は青緑)。SOLID の 6 色は新コードで従来通りの色になる
- 三角形版(`effects.py`/`geometry.py`/`build_hexagon_array`)は初代基板の
  ホストスクリプト用に残置
- 教訓: 六角形レイアウトの欠番 17〜22 は新基板では実在するため、旧形式の
  standby は 16COLORS の色を塗り残した(基板1で実測)。GEN 移行で解消

**16 色テストパターン `16COLORS` を UI に追加(2026-08-28、Radxa 実機デプロイ済み)**

- メニュー 2 段目 `16COLORS`: セグメント n = 色 (n-1)%16(1〜16 が
  0x00〜0x0F のランプ、17 以降繰り返し)。V1.1 の **GEN(0x06)形式**
  (新マーカー 0xFE/0xFF、セグメント 1〜60 = P1〜P60)で送る
- 実装: `build_gen_array`(pattern.py)、`Pattern` に `array`/`dev_type`
  フィールド追加、commands/runner が dev_type を伝搬。テスト 161 件通過
- **実機検証**: ID:1(USB)・ID:20(485 中継)とも GEN 形式の
  cfg/save/show が ACK_SUCCESS(2026-08-28)。**Radxa からは 485 中継が
  正常動作**(PC ベンチで見えた中継不通は Radxa では再現せず)
- 旧フォーマット(0x21/0x37, dev 0x01)も新 FW で ACK され standby は
  動作中。ただし新 LUT では旧 0x05(緑)が青緑に変わった可能性など、
  **既存デモ 6 色の発色は目視未確認** — 次回 SOLID を流して確認する

**新ファームウェアの OTA 導入(2026-08-28、PC + 基板 No.1/No.2 のベンチ)**

- メーカー新 FW 一式は CLOTHING フォルダ(OneDrive 経由)。16 色対応、
  プロトコル **V1.1**(`DISPLAY_Protocol(CH)V1.1.pdf`): OTA コマンド
  0x26 開始 / 0x27 データ / 0x28 終了 / 0x29 状態照会が追加
- **`host/ota.py` を新規作成**。`python host/ota.py FW.bin --addr N`
  (照会のみは `--check`)。**基板 No.1 は OTA 成功・新 FW 動作確認済み**
  (旧 FW は 0x29 を無視、新 FW は state=IDLE を返すのが判別点)
- **OTA のハマりどころ(実測)**:
  - 基板は約 9.6KB 受信ごとに **9〜17 秒無応答**(ステージング書き込み)。
    この間にチャンクを再送すると**毎回同じ位置で転送が死ぬ**。
    正解は「1 回送って ACK を最大 45 秒待つ」(ota.py 実装済み。全 1071
    チャンク約 3 分)
  - 0x28 成功時は **ACK なしで即リセット**が正常。ただし USB 切断を
    通知せずリセットするため **Windows 側の COM ポートが機能不全のまま
    残る**。復旧は USB 抜き差し、または管理者権限で
    `pnputil /restart-device`(実績あり)
- **基板 No.2 も USB 直結で OTA 成功**(全 1071 チャンク 67 秒、
  ストールなし)。ベンチの 2 枚は **ID:1 と ID:20**(No.2 の DIP は
  SW3+SW5 = 20)。本番 Radxa 系と同じ構成
- **USB 直結時のアドレスの罠(重要)**: USB を挿した基板は
  **自分の DIP アドレス宛のフレームだけをローカル処理**し、それ以外は
  すべて 485 へ中継する。ID:20 の基板に USB を挿して dest=1 に送ると、
  485 経由で ID:1 の基板が応答する(「基板 2 が addr 1 で応答した」
  ように見えるが別基板)。485 ケーブルを抜けば切り分けられる。
  また 485 接続状態で USB 側から dest=自分 以外を送ると中継が衝突して
  CDC ポートが wedge することがある(dest=20 で再現)。**OTA は
  「485 ケーブルを抜いて、その基板の DIP アドレス宛に USB 直結」が確実**
- **解決(2026-09-17)**: 下記は「OTA 系 0x29 を中継すると無応答 + wedge」
  で説明がつく(SPECIFICATION 5.7)。通常コマンドの中継は双方向とも正常
- (旧記述)ベンチ構成では USB(ID:1)→485→ID:20 方向の中継に
  応答が無い(逆方向 ID:20→ID:1 は動作確認済みなので配線は生きている)。
  新 FW の ID:1 は応答の無い中継後に USB が wedge する。未検証の仮説:
  0x1A 従機数が 2 のまま(今日 2 を設定・保存はしていない)で、
  マスタが addr 20 まで中継しない可能性 → 次回 `0x1A=20` を設定して
  `stop.py --addr 20 --groups 20` を再試験。本番 Radxa 系(ID:1+ID:20)
  では中継 ACK 即答の実績があるため、設定差分の線が濃い

**基板間ラグの解消(2026-08-14 実測で確定)** — show(0x1D)の 3 連発
ブロードキャストが原因だった。基板はリフレッシュ中のコマンドを**破棄せず
バッファして完了後に実行する**ため、3 連発 = 全面再描画 2〜3 回となり、
基板ごとの再描画回数の差が「ラグ」に見えていた。show を 1 回に変更後、
ID:1 と ID:20 は **0.5 秒以内の同時リフレッシュ**(deaf 窓 0.7-17.1s /
1.2-17.3s)。本番基板の全面書き換えは**約 16 秒**(初代 9.8 秒)。
プロトコル PDF も入手済み(`Datasheet/显示控制协议(1).pdf`): 0x16 同期
再生・0x18 完了報告・0x1E 同期切替・0x1A 従機数設定が存在する(M5 時代の
「謎の 0x1E」はマスタの同期切替トリガだった)。現状 0x1D 1 回で同期が
取れているため未使用。

**本番基板(ID:1, ID:20)の疎通確認 — 2026-08-14 実機確認済み**

- `stop.py --addr 1 --groups 20` / `--addr 20 --groups 20` とも
  **ACK_SUCCESS**。GroupCount=20 をファームウェアは受理する
- ID:20 の ACK は ID:1 の中継経由で即答(リトライなし)。DIP は純2進
  (`Datasheet/PCBA_DIP_SWITCH_H_WALL_BRICKS.png`: 20 = SW3+SW5)

**20 枚化と欠落基板スキップ(旧 7.1)** — 対象基板は既定で **ID 1〜20**。
応答しない基板はスキップして残りだけで動き、60 秒ごとに再プローブして
後から電源が入った基板を取り込む(待機中なら白を描き直して参加させる)。
セットアップは基板リストを 3 回掃引するので、リペイント中(9.8 秒無応答)の
基板を「不在」と誤判定しない。`--check` も 1〜20 を走査し、居る基板を
報告する(欠けていても失敗にしない)。

**待機状態(全面白)** — 接続確立時にメーカーデモを止め、全セクターを白にする。
起動時・USB 抜き差し後のいずれも実機で目視確認済み(旧 2 枚構成)。

- 白を「表示」させただけでは基板が再生中のままなので、ガード時間後に
  **もう一度停止指令**を送っている。これが無いとリフレッシュ完了後に
  メーカーデモへ流れ込む
- 待機中はデバイスノードの**同一性(inode + ctime)**を 2 秒ごとに監視し、
  変化したら白を描き直す。60 秒ごとの停止指令が検知漏れの保険
- 既知の割り切り: **一時停止中**の抜き差しは KEY1 で再開するまで復帰しない

## 3. 次にやること — 20 枚構成の実現性確認

[SCALING.md](SCALING.md) 9 章に詳細。**物理作業と問い合わせが先**で、
ソフト改修はその後でよい。

### 3.1 メーカーへの問い合わせ(最優先)

> **基板の DC-DC(5V→12V 昇圧)の出力定格は何 W か。
> 1 給電点あたり何枚まで 4 ピンで繋いでよいか。**

これで給電点の数が決まり、20 枚構成の全体像が確定する。
配電の電圧降下は 12V なので小さく、**律速は DC-DC の容量**である
(各基板の DC-DC は自分のパネル 1 枚分 0.9W のために載っているはずで、
20 枚分 18W を 1 箇所から吐くことはできない)。

### 3.2 現物確認(テスターと基板があればできる)

1. **DIP スイッチの本数を数える** — 20 枚には 5 本必要。
   4 本なら 1 バスに 20 枚は載らない(5 枚 × 4 系統なら 3 本で足りる)
2. **3 枚目を繋いで ACK 往復時間を測る** — 共有バスか数珠つなぎかで
   1 コマが 14 秒のままか 30 秒級かが決まる。**2 枚では区別できない**
3. **PD バッテリーの低負荷オートカット確認** — 白待機のまま 30 分放置し、
   `--check` で ACK が返るか。切れると全枚がメーカーデモへ落ち、
   **USB CDC デバイスはホスト直結の 1 枚だけなので待機監視が検知できない**

### 3.3 ソフト改修(3.1 / 3.2 の後)

**7.1(欠落基板スキップ)と 7.2/7.3(不在基板のリトライ予算)は 2026-08-14 に
実装済み**(2 章参照)。不在基板のコストは「1 分あたり短いプローブ 1 回
(約 0.5 秒)」まで下げてある。残るは実運転での挙動確認のみ。

## 4. 未解決(据え置き)

- **基板 ID:1 のパネルは M5 から制御不可**(ゲートウェイ仕様)。メーカー回答待ち
- ~~ID:1 の三角形 61 番が固着~~ — **初代基板の個体の話**。本番基板では未確認
- **セグメント番号は基板世代で異なる**: 本番 = 1〜60、初代 = 2〜61(+1)。
  コードは本番番号に切替済み(2026-08-14)。初代基板に戻す場合は
  `host/epaper/pattern.py` と `geometry.py` の 2 箇所を +1 に戻すこと
- 省電力対策([POWER.md](POWER.md) 3 章)は未適用
- **電源投入時のデモ自動開始は実装しない**(明示的な非採用)
- セクター数の食い違い: 本機は 1 枚 **54** セクター。1200 を満たすには
  1 枚 60 セクターの別品番が要る(要確認)

## 5. 再開手順

```bash
# 手元
cd <このリポジトリ>
git pull
python -m pytest tests/ -q            # 全件 passed を確認(2026-09-21 時点で 371 件。pytest / pyserial / pillow が要る)

# ショー用 PC の UI を起動(ブラウザが開く。二重に起動してもページを開くだけ)
"Start Conductor.bat"                 # = python -m conductor serve --open

# 本番機(パネル接続側)
ssh radxa@192.168.50.101
cd ~/E-paper_H_WALL_BRICKS_Raspi && git pull
systemctl status epaper-ui
journalctl -u epaper-ui -f            # 白待機なら "standby ready" で止まっている

# パネルの疎通確認(サービスを止めてから。ポートを専有するため)
sudo systemctl stop epaper-ui
.venv/bin/python -m ui.main --check
sudo systemctl start epaper-ui
```

**LCD の操作**: ジョイスティックで選択 → KEY1 開始 →(KEY1 一時停止/再開、
1 秒長押しでリセット)→ KEY2 でメニュー。KEY3 でバックライト消灯。
自動消灯は既定オフ、`--blank-after 10` で有効化。

**基板 FW のアップデート(LCD から)**: 基板の 485 ケーブルを抜き、更新する
基板を USB 直結 → メニュー末尾 `UPDATE FW` → KEY1 → 数秒のスキャンで
状態行が `IDLE … (auto)` になる(複数応答なら 485 を抜くか UP/DOWN で選ぶ)
→ KEY1。約 1〜2 分で `DONE`。KEY2 でメニューに戻る(白待機は走らない)。CLI で
行う場合は `sudo systemctl stop epaper-ui` してから `timeout 600
.venv/bin/python host/ota.py FW/FW_260917/MCB_e16_2029.09.17.bin --addr auto`。
