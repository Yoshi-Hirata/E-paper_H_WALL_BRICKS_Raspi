# E-paper H_WALL_BRICKS — Raspberry Pi Zero 2 W 版

六角形カラー電子ペーパーパネル(H_WALL_BRICKS / MCB03、DeviceType `0x01`)を
**Raspberry Pi Zero 2 W からスタンドアロン制御**するためのリポジトリ。

PC版リポジトリ([E-paper_H_WALL_BRICKS](https://github.com/Yoshi-Hirata/E-paper_H_WALL_BRICKS))
で実証済みの Python ホストツール群を移植し、Pi 上での常時運転
(電源投入で自動開始)に対応させる。

## なぜ Raspberry Pi か

M5StickS3 + RS485/TTL でのスタンドアロン化を試みたが、基板間リンクは
未文書化の独自プロトコル(3.3V TTL UART 9600bps + 未知の実行トリガ 0x1E +
不明な受理条件)であり、外部マイコンからの直接制御は現時点で不可能と判明
(詳細は PC 版リポジトリの docs/DEVELOPMENT.md 8章)。

一方、**USB CDC 経由(→基板 0x01 →中継→基板 0x02)の制御は完全に動作実績が
ある**ため、PC を Raspberry Pi に置き換えるのが最短・最確実の構成。
両パネルとも制御できる。

## ハードウェア構成

```
Pi Zero 2 W ──USB OTG(micro-B "USB"ポート)── 基板 ID:1 ──4芯ケーブル── 基板 ID:2 … ID:20
```

本番は基板 20 枚(DIP スイッチで ID 1〜20)。ソフトは既定で **ID 1〜20 を
対象**とし、**応答しない基板はスキップして残りだけで動く**(初期テストは
ID:1 と ID:20 の 2 枚のみ接続)。欠けている基板は 60 秒ごとに軽い再プローブで
探し続け、後から電源が入れば自動的に演出へ参加する(待機中なら白を描き直す)。
対象を絞りたいときは `--boards 1 20` のように指定する。

- Pi の給電は "PWR" と刻印された micro-USB に 5V/2.5A 以上の AC アダプタ
- 基板との接続は **"USB" と刻印された micro-USB(OTG)** に、
  OTG アダプタ + USB-C ケーブルで基板 ID:1 へ
- パネル制御基板の電源は従来どおり専用電源から供給(USB は通信のみ)
- 基板 ID:1 は DIP スイッチ = 1(バスマスタ)であること

## 検証済みの実機環境

| 項目 | 値 |
|---|---|
| 機体 | Raspberry Pi Zero 2 W Rev 1.0(ホスト名 `R2-RaspiZero2WH`) |
| OS | Raspbian GNU/Linux 13 (trixie) / Python 3.13.5 |
| USB OTG | `config.txt` に `otg_mode=1` と `dtoverlay=dwc2,dr_mode=host` 設定済み |
| 権限 | ユーザー `r2` は `dialout` グループ所属済み |
| 導入状況 | `~/E-paper_H_WALL_BRICKS_Raspi` に配置、venv 作成・テスト24件合格 |

**2026-08-04 実機動作確認済み**:

- 基板は `/dev/ttyACM0`(0483:5740)として認識。`stop.py --addr 1` / `--addr 2`
  ともに **ACK_SUCCESS**(基板 ID:2 の ACK も中継経由で正常に返る)
- `wave_demo.py --cycles 3` で 2 枚のパネルが同期リフレッシュすることを目視確認
- `epaper-demo.service` を有効化し、**再起動後に自動で演出が再開**することを確認
  (電源投入から約 35 秒で 1 サイクル目を送信)

## セットアップ

**Radxa Cubie A7Z を使う場合は [radxa/README.md](radxa/README.md) の
「セットアップ手順」を参照**(SPI オーバーレイ・GPIO の叩き方・Wi-Fi の
扱いが Pi と異なる)。以下は Raspberry Pi 向け。


1. Raspberry Pi OS Lite (64-bit) を microSD に書き込み
   (Raspberry Pi Imager で Wi-Fi / SSH / ホスト名を事前設定しておくと楽)
2. SSH でログインし、本リポジトリを取得:

   ```bash
   sudo apt update && sudo apt install -y git
   git clone https://github.com/Yoshi-Hirata/E-paper_H_WALL_BRICKS_Raspi.git
   cd E-paper_H_WALL_BRICKS_Raspi
   ```

3. セットアップスクリプトを実行(venv 作成、依存導入、シリアル権限、
   systemd サービス登録まで一括):

   ```bash
   ./raspi/setup.sh
   ```

4. 動作確認(基板を USB 接続した状態で):

   ```bash
   .venv/bin/python host/stop.py --addr 1     # ACK_SUCCESS が返ればOK
   .venv/bin/python host/wave_demo.py --cycles 3
   ```

## LCD HAT の UI(Waveshare 1.3inch LCD HAT)

240x240 の IPS LCD + ジョイスティック + KEY1〜3 でデモを操作する。

### 待機状態(全面白)

パネル基板は電源投入だけでメーカーのデモを自動再生し始めるため、
放っておくと「待機中」がショーの背景として使えない映像になる。
そこで **Pi / Radxa が基板との接続を確立した時点で自動的に**
再生を止め、両面の全セクターを白にして停止させる。これを待機状態とする。

- `python -m ui.main` 起動時、`--pattern` が指定されていなければ実行される
- メニュー画面の下段に `standby: blanking + check 0/20...` →
  `standby: white, boards 2/20 OK` と表示(数字は 応答基板数/設定基板数。
  通信エラーがあれば `ERROR ...` が赤で出る)
- **メニュー最上段の `STANDBY` を選んで KEY1** でいつでも再実行できる
  (全セクター白 + 全基板の通信チェック)。デモと違い画面はメニューに
  留まり、結果が上記のステータス行に出る
- 白の描画は 1 回だけ。書き込み後にガード時間(既定 12 秒)を置いて再度
  停止指令を送り、リフレッシュ完了後にメーカーデモへ戻るのを防ぐ
- 基板がリフレッシュ中(9.8 秒間は無応答)でもリトライして必ず白にする
- 応答しない基板はスキップし、居る基板だけを白にする。以後 60 秒ごとに
  再プローブし、後から現れた基板は白を描き直して待機に取り込む
- **USB を抜き差ししても白に戻る**。基板は電源が切れるとメーカーデモを
  再生しながら復帰するので、待機中はデバイスノード
  (`/dev/ttyACM*`)を 2 秒ごとに監視し、消失または再列挙を検知したら
  白を描き直す。検知漏れの保険として 60 秒ごとに停止指令も送る
- 白にしたくない場合は `--no-standby`

> 監視は待機中のみ。デモ実行中に抜き差しした場合はバスを開き直して
> **次のサイクルで描き直される**。デモの**一時停止中**は次のサイクルが
> 来ないため、KEY1 で再開するまでメーカーデモが流れたままになる。

| 操作 | 動作 |
|---|---|
| ジョイスティック 上下左右 | デモパターンの選択(ラップする) |
| **KEY1** | 開始 → 一時停止 → 再開(サイクル数とタイマーを保持) |
| **KEY1 を 1 秒長押し** | リセット(ゼロから開始) |
| KEY2 | メニューに戻る(実行中のデモは停止) |
| KEY3 | バックライト消灯(どのボタンでも復帰) |

メニュー末尾の行は、デモではなく**機体そのものの操作**:

| 行 | 動作 |
|---|---|
| `UPDATE FW` | 基板の FW を OTA で書き込む(485 を抜いて 1 枚ずつ) |
| `FW VERSION` | 応答した基板の FW を一覧 |
| `GIT PULL` | `git pull --ff-only`。更新があれば KEY1 で UI を再起動 |
| **`REBOOT`** | **機体(OS)を再起動**。確認画面で **KEY1 を 1 秒長押し**したときだけ実行(短押しでは何も起きない)。KEY2 で中止 |

`REBOOT` は `sudo -n systemctl reboot` を呼ぶので、パスワード不要の sudo が
前提(Radxa は `radxa/README.md` のセットアップ手順 3、Pi OS は既定で可)。
拒否された場合は画面に `FAILED` と理由が出て、長押しで再試行できる。
再起動後は通常起動と同じく**待機(全面白)**に入る(`--pattern` /
`--no-standby` 運用の機体はそれぞれの挙動)。約 1 分で UI が戻る。

実行画面には**経過タイマー(時:分:秒)**、サイクル数、直近のログが表示され、
通信エラーは赤で強調される。一時停止中は `PAUSED` と表示され、
**停止していた時間は経過時間に加算されない**。

消灯からの復帰に使った押下は**消費される**ので、暗い場所で手探りしても
デモの状態は変わらない。

### ショー運用・バッテリー運用のオプション

`/etc/default/epaper-ui` の `UI_ARGS` に追加する:

| オプション | 用途 |
|---|---|
| `--locked` | ボタンを無効化(接触事故でデモが止まるのを防ぐ)。解除は KEY2→KEY3→KEY2、1 分の無操作で自動再ロック |
| `--blank-after 10` | 10 秒無操作でバックライト消灯。**既定はオフ**。バッテリー運用で 20〜40mA の節約 |
| `--no-standby` | 起動時に全面白へ落とさず、パネルの表示をそのままにする(メーカーデモが流れ続ける) |

追加後は `sudo systemctl restart epaper-ui`。

選択できるパターン:

| パターン | 内容 | 切替間隔 |
|---|---|---|
| `SOLID+RANDOM` | **単色6色 → ランダム6回 を無限ループ**(常時デモの既定) | 各ステップ準拠 |
| `WAVE` | ID:1 グラデーション + ID:2 スパイラル | 既定値 |
| `GRADIENT` | 両面とも中心から広がる同心円 | 既定値 |
| `SPIRAL` | 両面とも外周から中心へ時計回り | 既定値 |
| `MIRROR` | 位相をずらしたグラデーションが追いかける | 既定値 |
| `RANDOM` | 各三角形をランダムな色に | 20 秒 |
| `SOLID` | 2枚とも全面単色を 白→黄→青→赤→黒→緑 の順に巡回 | 15 秒 |

既定値は `--interval`(既定 60 秒)。`SOLID`/`RANDOM` はパターン側で
間隔を指定している(`ui/patterns.py` の `Pattern.interval`)。
`SOLID+RANDOM` は `Playlist` で、各ステップが自分の間隔を保ったまま
順番に流れる(1周 = 単色90秒 + ランダム120秒 = 約3分30秒)。

ループの構成を変えるには `ui/patterns.py` の
`Playlist("loop", ..., steps=((_SOLID, 6), (_RANDOM, 6)))` を編集する
(タプルは「パターン, そのパターンを何サイクル続けるか」)。

**リフレッシュ所要時間の実測値**: 全面書き換えは色によらず、
初代基板 **9.8 秒**(2026-08-04)、本番基板 **約 16 秒**(2026-08-14)、
**最新 FW では約 7 秒**(2026-09-21 報告)。
64 バイト配列の保存が約 0.2 秒。本番基板では 1 サイクルの物理的下限は
約 17 秒で、15 秒間隔の SOLID は実質リフレッシュ律速になる。

**リフレッシュ中のコマンドは破棄されず、受信バッファに溜まって完了後に
実行される**(2026-08-14 実測)。show ブロードキャストを保険で複数回
送ると、その回数だけ全面再描画が繰り返され、基板間で描画回数がずれて
「基板間ラグ」に見える。show は 1 サイクル 1 回だけ送ること。

なお毎サイクル 2 枚分のフラッシュ書き込みが発生するため、常時運転する
場合は間隔を長めにすること。

### 起動

```bash
.venv/bin/python -m ui.main --check         # パネル/SPI/GPIO/LCD の準備状況を診断
.venv/bin/python -m ui.main                 # HAT があれば LCD、無ければ PNG+キーボード
.venv/bin/python -m ui.main --display png --frames /tmp/ui   # HAT 無しで動作確認
.venv/bin/python -m ui.main --preview /tmp/ui                # 画面サンプルを書き出して終了
```

**HAT を挿したら、まず `--check` を実行すること。** 全項目が `ready` なら
そのまま `python -m ui.main` で動く。`gpio pins: BUSY` と出た場合は他の
プロセスがピンを掴んでいる(下記の HAT 競合を参照)。

### ⚠️ 他の Waveshare HAT との競合

このリポジトリの Pi には元々 **Waveshare 7.5inch e-Paper HAT**(気象
ダッシュボード)が載っており、1.3inch LCD HAT と**物理的に競合する**:

| 信号 | 7.5inch e-Paper HAT | 1.3inch LCD HAT |
|---|---|---|
| GPIO25 | DC | DC |
| GPIO24 | BUSY | BL(バックライト) |
| GPIO8 | CS (SPI0 CE0) | CS (SPI0 CE0) |

40 ピンヘッダも 1 枚しか挿せないため**共存は不可**。本プロジェクトを
優先する方針とし、2026-08-04 に気象ダッシュボードの cron を無効化した
(削除ではなくコメントアウト。root の crontab を
`/home/r2/dashboard/crontab-root.backup-*` にバックアップ済み。
アプリ本体は `~/dashboard` に残置、git から復元も可能)。

戻す場合は `sudo crontab -e` でコメントを外す。

**HAT が届く前でも開発・確認できる**: `--display png` は毎フレームを
`/tmp/ui/latest.png` に書き出し、`--input keyboard` は標準入力で操作できる
(`w`/`s` 選択、`1`/`2`/`3` = KEY1〜3、いずれも Enter で確定)。

### ピン配置(Waveshare 1.3inch LCD HAT / BCM)

| 信号 | ピン | 信号 | ピン |
|---|---|---|---|
| LCD SPI | SPI0 (CE0) | KEY1 | GPIO21 |
| LCD DC | GPIO25 | KEY2 | GPIO20 |
| LCD RST | GPIO27 | KEY3 | GPIO16 |
| LCD BL | GPIO24 | 上/下 | GPIO6 / GPIO19 |
| | | 左/右 | GPIO5 / GPIO26 |
| | | 中央押し | GPIO13 |

SPI の有効化(`dtparam=spi=on`)は `raspi/setup.sh` が行う(要再起動)。
画面の向きが合わない場合は `ui/display.py` の `ST7789Display(madctl=...)`
を変更する(既定 `0x70`)。

**gpiozero には `lgpio` が必須**(requirements に含む)。無いと gpiozero が
実験的な native factory にフォールバックし、ボタンの `Button()` が
すべて `EINVAL` で失敗する(症状: ボタンが一切効かない)。

### HAT 未着時点での検証状況(2026-08-04)

LCD 本体は未接続だが、以下は実機で確認済み:

- ST7789 ドライバは実 SPI/GPIO 上で init → フレーム転送 → クローズまで
  例外なく完走(init 538ms、1 フレーム約 150ms)
- 8 個の入力ピン + DC/RST/BL の計 11 ピンすべて確保・解放できる
- UI からデモを起動し、**実際の電子ペーパーパネル 2 枚が 3 サイクル更新**
  (ジョイスティック選択 → KEY1 開始 → KEY3 終了までスクリプト入力で再現)
- `epaper-ui.service` が起動し PNG フレームを出力(HAT 無し時の自動退避)

残るのは LCD の表示そのもの(向き・色・視認性)の確認のみ。

## ルック(衣装)の CSV 取り込み — `conductor/`

ショーでは 1 ルック = Radxa 1 台(基板 最大 60 枚 × 鱗 60 枚)。デザイナーから
届く 2 種類の CSV を、基板ごとの 64 バイト配列に変換する(PC 側で実行)。

| ファイル | 内容 |
|---|---|
| `LookNN_map.csv` | `side,row,col,board_no,socket,label` — 鱗 1 枚 = 1 行。衣装上の位置と、どの基板のどのソケットか |
| `LookNN_color_patternMM_grid.csv` | `side,row,shift,1,2,3,…` — 衣装の 1 段 = 1 行。セルは色コード `0x00`〜`0x0F`、**穴の無いマスは `0`、色未指定は `-`**(先方 README の定義)。キューごとに 1 ファイル |

ファイル名の `_map` / `_color_patternMM` より前がアイテム名(`Look22`、
`Look20-Skirt`、バッグの名前など)で、map と grid はこの名前で対応付ける。

- 2 つは `(side, row, col)` で結合する。row 0 が裾、最大の row が首側
- **CSV は内側(体側)から見た向き**。客席から見ると左右が逆なので、
  プレビューは既定で反転して「外側(客席から)」を描く
- **1 機体に複数アイテム**(Look20 = トップス + スカート)を載せる場合、
  DIP の ID は**機体のバス全体で通し番号**(スカート 001〜016 → ID 1〜16、
  トップス 076〜091 → ID 17〜32)。アイテムごとに 1 から振ると衝突する
- `board_no` は基板の固有番号。**ルック内で昇順に並べた順位が DIP の ID**
  (017→1、018→2 …。飛び番があっても ID は詰める)
- `socket` N = 配列インデックス N(P1〜P60、DeviceType 0x03)。鱗の無い
  ソケットは `0xFF`(非更新)
- 色は `FW/FW_260917/260917_16_Color_Chart_ changed.xlsx` の表
  (**0x05 = 緑、0x06 = ターコイズ**)
- **`0` は「穴なし」、白は `0x00`**。取り違えを防ぐため、map に鱗があるのに
  grid に色が無い(`0` や `-`)/grid に穴があるのに map に鱗が無い、は
  どちらもエラー。一部の鱗だけ変えるキューは `--partial`(色の無い鱗は現状維持)

**Web UI(PC 上、localhost のみ)**: CSV をドロップして取り込み、検証結果・
仕上がり/配線プレビュー・DIP 設定表を確認し、アイテムを機体
(`radxa-01`〜`10`)に割り当てる。データは `./showdata`(git 管理外)に置く。

```bash
python -m conductor serve        # http://127.0.0.1:8765
```

**タイムライン**タブ: アイテムごとに 1 本のトラックがあり、1 アイテムに
何枚でも取り込めるデザイン(`*_color_patternNN_grid.csv`)を、ショー開始からの
経過時刻に割り当てる(`showdata/show.json` に保存)。

- トラックをクリックでキュー追加、キューをクリックで時刻・デザイン・
  「時刻の意味」・一部更新を編集。再生位置を動かすと、その時刻の各アイテムの
  見え方(一部更新の重ね合わせ込み)をプレビューする
- **書き換え時間**(送信から絵の完成まで)はショーの設定値で、既定 **7 秒**
  (最新 FW)。FW で変わってきた値なので(9.8 → 16 → 7 秒)、タイムライン画面の
  「書き換え時間」欄で変更できる。旧 FW の基板が混ざるなら長い方に合わせる
- **時刻の意味**: 既定は「この時刻に完成」(書き換え時間ぶん前に送信)。
  「この時刻に変化を開始」も選べる。**0:00 のキューは
  プリセット**(START の前に表示しておく)
- 検証(`conductor/timeline.py`): 同じ機体の書き換えは、送信から次の送信まで
  「書き換え時間 + 基板数 × 0.22 秒 + 余裕 3 秒」必要(7 秒なら基板 16 枚で
  14 秒、36 枚で 18 秒)。同じ機体に載るアイテム(Look20 の上下)は同時刻なら 1 回の
  書き換え、ずらすなら上の間隔が要る。ほかに、未取り込みのデザイン、
  色未指定(`-`)の残るデザインを通常キューで使う、書き換え時間より前に「完成」、
  ショー終了後の時刻、0:00 プリセットなし(警告)を検出する
- **元に戻す / やり直す**(ヘッダのボタン、Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z):
  タイムライン・ショーの長さ・機体の割り当てが対象。履歴は
  `showdata/history.json` に最大 200 手、ページの再読み込みやサーバの再起動を
  またいで残る。CSV の追加・削除は対象外(削除は確認ダイアログあり)
- `-` の残るデザインは「一部更新用」として扱い、キューにすると自動で
  一部更新になる(色の無い鱗は直前の表示のまま)

**機体**タブ(P1b): 10 台のエージェントを 2 秒ごとに問い合わせ、オンライン状態・
応答している基板数・コミット・**時計のずれの測定精度**を表示する。手動の一斉表示は
2 段階: **① 準備**(アイテムごとに選んだデザインを各機体の基板へ保存。表示は
変わらない)→ **② GO**(N 秒後の同じ瞬間に全機体が表示命令を 1 回だけ送る)。
ほかに「発火を取り消す」「全機体 STANDBY(白)」「本体メニューへ戻す」。
機体の宛先は既定で `radxa-NN` = `192.168.50.(100+NN):8787`、変える場合は
`showdata/fleet.json`(`{"units": {"radxa-01": "host:port"}, "token": "..."}`)。

### 機体側のリモートエージェント(`ui/agent.py`, `ui/remote.py`)

`epaper-ui`(`python -m ui.main`)は既定で **TCP 8787 に HTTP エージェント**を立てる
(`--no-remote` で無効、`--remote-port`、`--remote-token TOKEN` で認証必須に)。
標準ライブラリのみで、機体の requirements は変わらない。できるのはパネルの
表示変更だけ(再起動・git pull・FW 更新は不可)。

| エンドポイント | 内容 |
|---|---|
| `GET /status` | ホスト名・コミット・状態・応答基板・直近ログ + 機体の時計 |
| `POST /prepare` | `{"cue","label","dev_type","boards":{"1": 64バイトのhex,…}}` を基板へ保存 |
| `POST /fire` | `{"cue","at"}` — **機体自身の monotonic 時刻 `at`** に表示命令(0x1D ブロードキャスト)を 1 回送る |
| `POST /cancel` `/standby` `/release` | 発火の取り消し / 全面白 / 本体メニューへ戻す |

- PC が各機体の時計のずれを測り(往復の中点、直近 8 回のうち往復が最短の回を採用)、
  「同じ瞬間」を**機体ごとの時計に換算して**渡す。だから命令の到着が遅れても
  発火時刻は揃う。間に合わなかった機体は即発火し、遅れを ms で報告する
- monotonic を使うのは、機体の実時計が timesyncd で飛ぶことがあるため
- PC から操作されると LCD は `REMOTE` 画面になる(ロード済みデザイン、
  保存できた基板数、発火までの秒数 / 発火の遅れ)。**KEY2 で本体メニューへ戻る**。
  `--locked` の機体ではボタンで抜けられない。UPDATE FW・FW VERSION のスキャン・
  REBOOT の実行中は PC からの操作を拒否する(HTTP 409)
- 実測(2026-09-21、radxa-01〜03、実 Wi-Fi、基板は偽バス): 往復 最小 5.6 /
  中央 7.6 ms、ずれの推定のばらつき 0.7 ms / 30 秒、発火は指定の瞬間から
  +0.1〜0.7 ms、3 台の送信時刻の差は 1 ms 未満(推定誤差の上限 ±3 ms は別)

コマンドラインでも同じ検証ができる:

```bash
python -m conductor check   Look22_map.csv Look22_color_pattern01_grid.csv
python -m conductor preview Look22_map.csv Look22_color_pattern01_grid.csv -o p01.png   # 仕上がり
python -m conductor preview Look22_map.csv -o wiring.png     # 配線図(基板の色分け + ソケット番号)
python -m conductor dip     Look22_map.csv -o dip.csv        # DIP スイッチ設定表
python -m conductor arrays  Look22_map.csv GRID.csv -o p01.json
python -m conductor send    Look22_map.csv GRID.csv --only 1 2   # ベンチ: 繋がっている基板へ書く
```

`send` は基板が繋がっている機体で実行する(先に `sudo systemctl stop epaper-ui`)。

## 常時運転(systemd)

常時デモは `epaper-demo.service`(`python -m ui.main --pattern loop
--display null --input none`)として動く。LCD HAT 版 UI と同じコードを
画面なしで走らせているだけなので、演出の追加はどちらにも同時に効く。

`raspi/setup.sh` が `epaper-demo.service` を登録する。既定では
**無効**なので、確認が済んだら有効化する:

```bash
sudo systemctl enable --now epaper-demo   # 電源投入で自動開始
journalctl -u epaper-demo -f              # ログ確認
sudo systemctl stop epaper-demo           # 停止
```

演出のパラメータは `/etc/default/epaper-demo` で変更できる
(サイクル数、間隔など。編集後は `sudo systemctl restart epaper-demo`)。

## ツール(PC版から移植)

| スクリプト | 用途 |
|---|---|
| `host/stop.py` | 再生停止 (0x17)。`--addr N` / `--broadcast` |
| `host/show.py` | 任意パターンの表示(停止→スロット設定→色保存→単張表示) |
| `host/demo.py` | ランダムカラーデモ |
| `host/wave_demo.py` | グラデーション+スパイラル演出(隣接同色なし保証) |
| `host/probe.py` | 疎通診断 |

シリアルポートは STM32 CDC(VID:PID 0483:5740、Linux では `/dev/ttyACM0`)を
自動検出する。手動指定は `--port /dev/ttyACM0`。Pi 内蔵 UART(`/dev/ttyS0`)は
候補から除外されるため、基板が未接続なら「ポートが見つかりません」と
明確に失敗する(誤って内蔵 UART を掴むことはない)。

## 運用上の注意(PC版で実証済みの制約)

- **スロット 0〜18 はメーカーデータのため書き込み・削除禁止**(復元不可)。
  テスト・演出はスロット 19 のみ使用する
- 色保存(0x13)は毎回フラッシュ書き込みを伴う。常時運転ではサイクル間隔を
  長めに設定する(既定 20 秒、`/etc/default/epaper-demo` で変更可)
- 電源投入時はメーカーのオートプレイが自動再生される。wave_demo は
  開始時のブロードキャスト停止+毎サイクル再送+ガード停止で抑止する
- 基板 ID:2 の ACK は返らない環境がある(コマンド実行はされる)。
  ツールのリトライ・タイムアウトはこの前提で運用する

## テスト(ハードウェア不要)

```bash
.venv/bin/python -m pytest tests/ -q
```

## ドキュメント

- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — 開発経緯と判断の記録(なぜそうしたか)
- [docs/SPECIFICATION.md](docs/SPECIFICATION.md) — 通信プロトコル・色データ・演出の仕様
- **[docs/STATUS.md](docs/STATUS.md) — 現在地と再開手順。まずここを見る**
- [docs/PORTING.md](docs/PORTING.md) — 他ボードへの移植可否と手法(Radxa Cubie A7Z 検討)
- [docs/POWER.md](docs/POWER.md) — 待機電力の削減手法(検討メモ、未適用)
- [docs/RELIABILITY.md](docs/RELIABILITY.md) — 無停止化の設計(ショー運用向け、未実装)
- [docs/SCALING.md](docs/SCALING.md) — **20 枚 / 1200 セクター構成の実現性検討**(検討メモ)
- 開発経緯・実機検証の詳細は PC 版リポジトリの docs/ を参照
