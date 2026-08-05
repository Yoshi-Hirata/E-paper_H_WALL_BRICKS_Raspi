# 他ボードへの移植可否と移植手法(Radxa Cubie A7Z 検討)

作成: 2026-08-05。Raspberry Pi Zero 2 W の欠品・価格高騰を受けた代替機検討。
**本書は判断材料と手順のまとめであり、実装は未着手。**

## 1. 結論

**移植は可能。ただし我々のコードの問題ではなく、ボード側の足回り
(GPIO ライブラリ・SPI オーバーレイ・OS 成熟度)が論点になる。**

- 書き換えが必要なのは**ハードウェアに触れる 2 ファイルとピン定義だけ**
  (実装量にして約 120 行)。演出・プロトコル・UI・テストは無改造で動く
- 最大のリスクは **Allwinner A733 が 2025 年 8 月発表の新 SoC** であること。
  Radxa 公式 Debian は存在するが Armbian はコミュニティ対応中で、
  GPIO/SPI まわりのカーネル成熟度が読めない
- **性能面での必要性はない**。現行の負荷は Pi Zero 2 W で CPU 15% 程度。
  A7Z(8 コア + 3 TOPS NPU)は完全なオーバースペックで、
  新 SoC のドライバリスクを負う見返りが乏しい(→ 6 章に代替案)

## 2. 移植の影響範囲

### 無改造で動く(全体の大部分)

| 対象 | 理由 |
|---|---|
| `host/` 一式(protocol / transport / pattern / commands / geometry / effects / CLI) | pyserial と `/dev/ttyACM*` のみに依存。ポート検出は VID:PID(0483:5740)なので機種非依存 |
| `ui/patterns.py` `runner.py` `render.py` `app.py` `main.py` | 純 Python + Pillow。ハードウェアに触れない |
| テスト 82 件 | 元よりハードウェア不要 |
| systemd ユニット | そのまま |

### 書き換えが必要

| 対象 | 内容 |
|---|---|
| `ui/inputs.py` の `GpioInput` | gpiozero.Button × 8 → libgpiod |
| `ui/display.py` の `ST7789Display` | gpiozero.DigitalOutputDevice × 3(DC/RST/BL)→ libgpiod。SPI ノード番号 |
| `ui/config.py` | BCM 番号 → `(gpiochip, line)` 番号 |
| `raspi/setup.sh` | SPI 有効化手順(`config.txt` の `dtparam=spi=on` → Radxa は `rsetup` でオーバーレイ)、パッケージ名、グループ |

### なぜ gpiozero が使えないか

gpiozero の lgpio ピンファクトリは、**内部で Raspberry Pi であることを
前提**にしている(lgpio 自体は機種非依存だが、gpiozero 側の実装が Pi の
ボード情報を要求する)。Radxa は公式ドキュメントで **libgpiod (gpiod)** を
案内しており、こちらは Radxa ROCK / Raspberry Pi / BeagleBone を横断して
使える。→ 移植では libgpiod に寄せるのが正解。

## 3. 推奨する移植アーキテクチャ

**Pi 用と A7Z 用の 2 系統に分岐させず、libgpiod 単一バックエンドに統一する。**
libgpiod は Raspberry Pi でも動く(現行の lgpio/gpiozero も内部では同じ
`/dev/gpiochip*` を叩いている)ため、コードは 1 本のまま両対応できる。

```
ui/gpio.py     ← 新規。OutputLine / InputLine(コールバック付き)の薄い抽象
ui/boards.py   ← 新規。ボードごとのピンプロファイル
                  PI_ZERO_2W  : gpiochip0, BCM 番号がそのまま line 番号
                  CUBIE_A7Z   : gpiochipN, 40 ピン物理位置 → line 番号の対応表
ui/config.py   ← 実行時にプロファイルを選ぶ(/proc/device-tree/model で判定)
```

`ui/inputs.py` と `ui/display.py` はこの抽象だけを見るようにする。
既にハードウェア依存が 2 ファイルに閉じているので、この形にするのは容易。

**移植と同時にリグレッションを防ぐ手段も用意する**: 現行の
`python -m ui.main --check` を拡張し、ボード判定・gpiochip/line 解決・
SPI ノード・全ピンの確保可否を一括で報告させる。移植先で最初に叩く。

## 4. 移植手順(想定)

1. **ボード実機で情報収集**(コード変更前)
   - `gpioinfo` で gpiochip とライン数、40 ピンヘッダとの対応を確認
   - Radxa ドキュメントの 40 ピン配置表を入手し、
     LCD HAT が使う 11 本(SPI0 CS/CLK/MOSI + DC/RST/BL + ボタン 8)を特定
   - `rsetup` で SPI オーバーレイを有効化し `/dev/spidev*` を確認
   - パネル基板を挿して `/dev/ttyACM0` を確認
2. **無改造部分の動作確認** — `host/stop.py --addr 1` が ACK を返せば、
   システムの中核(プロトコル・シリアル)は移植完了と同義
3. `ui/gpio.py` / `ui/boards.py` を追加し、`inputs.py` / `display.py` を
   その抽象経由に置換(Pi 上で先に実装し、Pi で 82 件のテスト + 実機確認を
   通してから A7Z へ持ち込む = 一度に 2 つの変数を動かさない)
4. A7Z 用プロファイルを追加し、`--check` → ボタンテスト → UI 起動の順で確認
5. `setup.sh` をボード判定に対応させる

**工数の見積り**: コード自体は 0.5〜1 日。ただし 1 の情報収集と、
オーバーレイ・権限まわりの試行錯誤が読めない(新 SoC のため)。

## 5. 購入前に確認すべきこと

| # | 確認事項 | なぜ重要か |
|---|---|---|
| 1 | **USB ホストがどのポートで使えるか** | A7Z は USB-C 2.0(OTG 兼電源)と USB-C 3.0(DP Alt)の 2 口。パネル基板は USB CDC デバイスなので**ホスト側になれるポートが 1 つ必要**。電源と兼用ポートしかホストになれない場合、電源を 40 ピンの 5V から取るなどの工夫が要る |
| 2 | SPI オーバーレイの有効化手順と `/dev/spidev*` の番号 | LCD HAT の必須条件 |
| 3 | 40 ピンヘッダの gpiochip/line 対応表 | ピン定義の書き換えに必須 |
| 4 | LCD HAT の機械的干渉 | 65×30mm で Pi Zero と同寸だが、USB-C コネクタ位置やヒートシンクとの干渉は要現物確認 |
| 5 | OS イメージの Python バージョン、`libgpiod` パッケージの有無 | Python 3.11+ と pyserial/Pillow が入ればよい |
| 6 | 電源容量 | A733 は 8 コア。Pi Zero 2 W より消費が大きく、5V/3A 級を想定 |

## 6. リスク評価と代替案

**A7Z を選ぶ理由が「入手性と価格」だけなら、より枯れたボードを勧める。**
本システムの要求は「USB ホスト 1 口 + SPI + GPIO 11 本 + Python」で、
Pi Zero 2 W ですら余裕がある。新 SoC の性能は一切活かせない一方、
ドライバ成熟度のリスクだけを負うことになる。

| 候補 | 評価 |
|---|---|
| **Radxa Zero 3W**(RK3566) | Pi Zero 形状・40 ピン。RK3566 は枯れており Radxa 公式 Debian / Armbian ともに実績多数。**代替の第一候補** |
| Radxa Cubie A7Z(A733) | 形状・端子は要件を満たす。新 SoC ゆえ足回りが未知数。性能は過剰 |
| Pi Zero W(無印) | **非推奨**。UI の RGB565 変換は CPU 律速で Zero 2 W でも 125ms/フレーム。単コア ARMv6 では体感が悪化する |
| Pi 4 / Pi 5 など | 確実に動くが大きく高い。設置形態が許すなら選択肢 |

**現行機を維持する選択も有効**: 既に Pi Zero 2 W で完成・稼働しており、
代替機の検討は「増設・故障時の予備」の話。急いで移植する必要はない。

## 7. 移植性を保つために(今後の実装方針)

将来どのボードに移っても影響を局所化できるよう、以下を維持する:

- ハードウェア依存は `ui/display.py` と `ui/inputs.py` の 2 ファイルに閉じる
- ピン番号は `ui/config.py` に集約し、コード中に直書きしない
- シリアルポートは VID:PID で検出する(デバイス名を直書きしない)
- テストはハードウェア不要のまま維持する(移植先で最初に回せる回帰網)
