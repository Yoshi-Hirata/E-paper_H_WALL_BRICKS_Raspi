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
| 導入状況 | `~/E-paper_H_WALL_BRICKS_Raspi` に配置、venv 作成・テスト24件合格・
systemd サービス登録済み(未有効化) |

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
