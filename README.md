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
Pi Zero 2 W ──USB OTG(micro-B "USB"ポート)── 基板 ID:1 ──4芯ケーブル── 基板 ID:2
```

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

| 操作 | 動作 |
|---|---|
| ジョイスティック 上下左右 | デモパターンの選択(ラップする) |
| **KEY1** | 選択したデモを開始 / 実行中は停止・再開のトグル |
| KEY2 | メニューに戻る(実行中のデモは停止) |
| KEY3 | UI 終了 |

実行画面には**経過タイマー(時:分:秒)**、サイクル数、直近のログが表示され、
通信エラーは赤で強調される。

選択できるパターン: `WAVE`(グラデーション+スパイラル)/ `GRADIENT` /
`SPIRAL` / `MIRROR`(位相をずらしたグラデーションが追いかける)/
`RANDOM` / `SOLID`(単色を順に切替)。

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

## 常時運転(systemd)

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

- [docs/SPECIFICATION.md](docs/SPECIFICATION.md) — 通信プロトコル・色データ・演出の仕様
- 開発経緯・実機検証の詳細は PC 版リポジトリの docs/ を参照
