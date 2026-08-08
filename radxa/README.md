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

## 実機検証の状況(2026-08-09 完了)

| 項目 | 結果 |
|---|---|
| ボード自動判別 | `cubie-a7z` / SPI1 / periphery を自動選択 |
| テスト 132 件 | **Python 3.9 上で全合格** |
| GPIO 11 本 | 全て開ける。ボタンはプルアップで idle=High |
| ボタン入力 | **8 種 + KEY1 長押しを全て検出**。長押し後に短押しが二重発火しないことも確認 |
| LCD 表示 | **正常**(向きも Pi と同じ `madctl=0x70` で合う)。init 370ms、1 フレーム 107〜142ms(**Pi の 150ms より速い**) |
| パネル通信 | **両基板 ACK**、デモ 6 サイクル完走 |
| `--check` | **全項目 ready** |
| LCD UI | メニュー表示・KEY1 でデモ開始・電子ペーパー更新すべて動作 |
| サービス自動起動 | 再起動後に `active`、ウォッチドッグの誤検知なし(NRestarts=0) |

計算で導いたライン番号(PB7=39、PL5=5、PJ25=313 など)が**実機で全て有効**だった。

## このボード特有の運用上の注意

### USB データポートは実質 1 つ

USB-C は 2 口あるが、**片方は電源入力**として使うため、データ用は
**USB 3.1 側の 1 口だけ**になる。キーボードとパネル基板は同時に挿せない。

- **スタンドアロン運用ではパネル基板がその 1 口を占有する**。コンソール
  操作は SSH で行う
- 再起動後にパネルが見えないときは、キーボードが挿さっていないか確認する
  (故障ではなく、単に差し替わっているだけのことが多い)
- 両方必要なら USB ハブを使う

### Wi-Fi はシステム全体の接続にする

デスクトップ環境が入っているため、Wi-Fi 接続が**ユーザーセッション紐付け**
だと**ログインするまで繋がらず、再起動後にヘッドレスで見失う**。実際に
2 回発生した。手順は「セットアップ手順」の 1 番。

### journal を読むには adm グループが要る

Pi のイメージと違い、このイメージのユーザは `adm` に入っていない。
入れないと `journalctl` が「No entries」を返し、`raspi/runlog.py` も
サイクル数を拾えない。`radxa/setup.sh` が追加する。

## Python 3.9 対応

Debian 11 の Python は 3.9 で、`X | None` 形式の型注釈が**実行時エラー**に
なる。全モジュールに `from __future__ import annotations` を入れて解決した
(注釈の書き換えは不要で、Pi 側の動作にも影響しない)。

## セットアップ手順(まっさらな状態から)

`radxa/setup.sh` が自動化できるのは 4 番以降だけ。1〜3 は**ボードの画面と
キーボードで行う前提作業**で、これを飛ばすと SSH で入れない・sudo が通らない
・再起動でネットワークから消える、という順に詰まる。

### 1. ネットワークを「システム全体の」接続にする

デスクトップ環境つきイメージのため、Wi-Fi をデスクトップから繋いだだけだと
**ユーザーセッション紐付け**になり、**ログインするまで接続されない**。
ヘッドレス再起動で行方不明になるので、必ず外す。

```bash
nmcli connection show                    # 接続名を確認
sudo nmcli connection modify "<接続名>" connection.autoconnect yes
sudo nmcli connection modify "<接続名>" connection.permissions ""
```

IP も固定しておく(ルータの DHCP 予約でも可)。
確認: **誰もログインしていない状態で再起動し、20 秒ほどで SSH に応答すること**。

### 2. SSH 鍵を登録する

作業マシンの公開鍵を追加する。以降の手順はすべてリモートから実行できる。

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo '<作業マシンの ~/.ssh/id_*.pub の中身>' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

### 3. sudo をパスワード不要にする

`setup.sh` は sudo を何度も使う。非対話で通すために設定する。

```bash
sudo sh -c 'printf "radxa ALL=(ALL) NOPASSWD:ALL
" > /etc/sudoers.d/010_radxa-nopasswd'
sudo chmod 440 /etc/sudoers.d/010_radxa-nopasswd
sudo visudo -c        # 全ファイルが parsed OK になること
```

**必ず `visudo -c` を確認する。** 綴り間違い(`NOPASSWD` を `NOPASSWORD`
など)があると、そのファイルは無視され、しかも sudo のたびに警告が出る。
構文エラーを放置すると締め出しに繋がる。

### 4. リポジトリを取得してセットアップ

```bash
git clone https://github.com/Yoshi-Hirata/E-paper_H_WALL_BRICKS_Raspi.git
cd E-paper_H_WALL_BRICKS_Raspi
./radxa/setup.sh
```

`setup.sh` の内容: apt パッケージ(python3-venv ほか)、venv と依存の導入、
**SPI1 オーバーレイの有効化**(`u-boot-update` まで)、グループ追加
(`dialout` `gpio` `spidev` `adm`)、systemd ユニット 3 種の登録。

### 5. 再起動

```bash
sudo reboot
```

**SPI オーバーレイとグループ追加はどちらも再起動が必要。**

### 6. 配線して確認

パネル基板を USB-C(データ側)に接続する。**キーボードとは同じポートを
奪い合う**ので、スタンドアロン運用ではパネルを挿してコンソールは SSH で使う。

```bash
.venv/bin/python -m ui.main --check      # 全項目 ready になること
```

### 7. サービスを有効化

排他なのでどちらか一方だけ。

```bash
sudo systemctl enable --now epaper-ui      # LCD HAT のメニュー
# sudo systemctl enable --now epaper-demo  # 画面なしの常時デモ
```

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
