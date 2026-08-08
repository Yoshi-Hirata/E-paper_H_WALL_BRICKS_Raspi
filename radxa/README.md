# Radxa Cubie A7Z 対応

Raspberry Pi Zero 2 W と**同じコード**を Radxa Cubie A7Z で動かすための
ボード固有部分。アプリ本体(`ui/`・`host/`)は共通で、**分岐しているのは
ピン定義と GPIO の叩き方だけ**。

## ボード固有ファイルの置き場所

| 場所 | 内容 |
|---|---|
| `ui/boards.py` | **両ボードのピンプロファイル**。物理ピン番号を基準に、各 SoC のライン番号へ翻訳する。ボードは device-tree の compatible で自動判別 |
| `ui/gpio.py` | GPIO キャラクタデバイス経由の実装(python-periphery)。**gpiozero は Raspberry Pi 専用**のため、Pi 以外はこちらを使う |
| `radxa/setup.sh` | このボード用のセットアップ(SPI オーバーレイ・パッケージ・グループ) |
| `radxa/README.md` | この文書 |

systemd ユニット(`raspi/*.service.in`)とアプリのコードは**共用**。

## 実機で確認した構成(2026-08-08)

| 項目 | 値 |
|---|---|
| ボード | Radxa Cubie A7Z(Allwinner A733 / sun60iw2)、`radxa,cubie-a7z` |
| OS | Debian 11 (bullseye)、kernel 5.15.147-21-a733、**Python 3.9** |
| GPIO | `gpiochip0` = 352 ライン(PA〜PK)、`gpiochip1` = 64 ライン(PL・PM) |
| ライン名 | **付いていない**(番号で指定するしかない) |
| libgpiod | 1.6.2(v1 API)。Python バインドは未導入 → python-periphery を使用 |
| ユーザ | `radxa`、`gpio` `spidev` `i2c` グループに所属済み |

### ピン番号の算出

ベンダ資料の式を実機のライン数で裏取りした:

```
gpiochip0 のライン = 32 × バンク(PA=0 … PK=10) + n     ← 352 = 11 バンク
gpiochip1 のライン = 32 × バンク(PL=0, PM=1)   + n     ← 64  = 2 バンク
```

例: PB7 = 32×1 + 7 = 39(chip0)、PL5 = 5(chip1)、PJ25 = 32×9 + 25 = 313。

**SPI は Raspberry Pi と同じ物理ピン(19/21/23/24)に出ている**ため、
LCD HAT は配線の改造なしにそのまま挿さる。デバイスノードが
`/dev/spidev1.0` になるだけ。

## Python 3.9 対応

Debian 11 の Python は 3.9 で、`X | None` 形式の型注釈が**実行時エラー**に
なる。全モジュールに `from __future__ import annotations` を入れて解決した
(注釈の書き換えは不要で、Pi 側の動作にも影響しない)。

## セットアップ

```bash
git clone https://github.com/Yoshi-Hirata/E-paper_H_WALL_BRICKS_Raspi.git
cd E-paper_H_WALL_BRICKS_Raspi
./radxa/setup.sh      # SPI オーバーレイ有効化・依存導入・サービス登録
sudo reboot           # オーバーレイとグループ反映のため必須
.venv/bin/python -m ui.main --check
```

`setup.sh` は `sudo` を多用するので、パスワード不要の sudo を設定して
おくと通しで実行できる。

## Pi との差分まとめ

| | Raspberry Pi Zero 2 W | Radxa Cubie A7Z |
|---|---|---|
| SPI ノード | `/dev/spidev0.0` | `/dev/spidev1.0` |
| SPI 有効化 | `dtparam=spi=on`(config.txt) | dtbo オーバーレイ + `u-boot-update` |
| GPIO | gpiozero + lgpio | python-periphery(キャラクタデバイス) |
| GPIO 番号 | BCM = chip0 のライン番号 | バンク式(上記)、2 つの chip に跨る |
| Python | 3.13 | 3.9 |

Pi は本番機のため、**移植で挙動が変わらないよう gpiozero のまま**にして
ある(`ui/boards.py` の `gpio_backend`)。python-periphery が Pi でも
検証できたら 1 本に寄せる。
