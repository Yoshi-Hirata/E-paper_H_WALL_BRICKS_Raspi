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

### HDMI モニタを繋ぐと電源が落ちることがある(CEC)

デバッグで HDMI モニタを繋ぐと、`sunxi_cec` がシステムの電源ボタンとして
登録され、モニタが送る CEC のスタンバイ信号を logind が電源ボタン押下と
解釈して**基板をシャットダウンする**(radxa-01 で発生、2026-09-15。ログイン
画面まで出た後に電源断)。対策として `raspi/logind-appliance.conf` を
`/etc/systemd/logind.conf.d/10-appliance.conf` に置き、電源/サスペンド系の
キーを全て `ignore` にしてある(`setup.sh` が導入。ゴールデンイメージにも
反映済み)。落ちてしまったら電源を入れ直せば直る。


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

## 基板ファームウェアのアップデート(LCD メニュー)

FW イメージはリポジトリに同梱している(`FW/FW_<yymmdd>/*.bin`、ファイル名は
メーカー配布のまま)ので、Radxa への配布は `git pull` だけでよい。UI はその中の
**最新フォルダ**の `.bin` をメニューの `UPDATE FW` 行に出す(`.hex` は
SWD 用で対象外)。その `git pull` 自体もメニュー末尾の **`GIT PULL`** 行で
LCD から実行できる(下記)。

### FW VERSION(基板の FW を確認)

メニューの `FW VERSION` 行 → KEY1 で、設定された全アドレス(既定 1〜20)に
停止(0x17)を送って存在確認し、応答した基板を 1 行ずつ列挙する。**応答が
1 枚だけ(= USB 直結の基板、485 は抜いてある)のときだけ**、その基板に OTA
状態照会(0x29)を送って FW を判定する。表示は次のいずれか:

| 表示 | 意味 |
|---|---|
| `V1.1, flashed FW_260917 09-17 17:19` | 16 色 FW(0x29 に応答)で、**この機体の UPDATE FW が** その日時にそのイメージを書き込んだ記録がある(USB シリアル = STM32 の UID で照合、`~/.epaper/flash-log.json`) |
| `V1.1, no flash record here` | 16 色 FW だが、この機体からの書き込み記録が無い(他の機体・PC で書いた、または記録以前)。基板自身は再起動後 size=0 を返すのでビルドは分からない |
| `V1.0 6-color (no OTA)` | 旧 6 色 FW(0x29 を知らない) |
| `on 485 bus - unplug 485 to identify` | 485 に複数枚いるので照会していない。1 枚ずつ USB 直結で見る |
| `FW_260917` など(緑) | 0x29 が size/CRC を返した場合のみ。現行 FW では出ない |

状態行には USB 直結基板のシリアル(`USB 48EC7570324C`)が出る。書き込みが
成功するたびに記録が更新されるので、「この基板は 260917 か」は、その機体で
UPDATE FW を通した基板については画面で答えられる。

**485 を繋いだまま 0x29 を中継すると USB 直結基板が固まる**(2026-09-17 実測、
電源再投入まで復旧しない)ため、複数枚が応答した時は照会しない。プロトコルに
バージョン照会コマンドは無く(V1.0 の 0x02 は V1.1 FW でも ACK_FAIL 0x0A)、
ビルドの識別にはメーカーのコマンド追加が要る。UP/DOWN でスクロール、KEY1 で
再スキャン、KEY2 でメニューへ(白待機が走る)。

### GIT PULL(LCD からリポジトリを更新)

各画面の上部バーに**ホスト名**(`radxa-01`〜`radxa-10`)が出るので、どの機体を
操作しているかは画面で分かる。コードや同梱 FW を配った後は、各機体で:

1. メニュー末尾 `GIT PULL` → KEY1。画面に現在のコミット(`now`)が出る
2. KEY1 で `git pull --ff-only` を実行(ネットワーク待ちは最長 180 秒)。
   `new` に取り込んだコミットが出て、状態が `UPDATED` になる
   (変化なしなら `UP TO DATE`、失敗なら `FAILED` とログの赤い行)
3. `UPDATED` なら KEY1 で **UI を再起動**(プロセスが終了し、systemd の
   `Restart=always` で約 15 秒後に新コードで立ち上がる)。KEY2 はメニューへ

fast-forward できない状態(機体側で編集した等)や `requirements.txt` の変更は
対象外なので、その時は SSH で `git pull` / `pip install -r requirements.txt`。

1. 更新する基板の 485 ケーブルを抜き、その基板を USB-C(データ側)に直結する
   (USB を挿した基板は自分の DIP アドレス宛だけをローカル処理し、他は
   485 へ中継するため、点対点でやるのが確実)
2. メニューで `UPDATE FW` を選び KEY1。ランナーが止まりポートが空き、
   1〜20 番へ状態照会(0x29)を送るスキャンが数秒走る
3. 応答が 1 件ならその基板が自動で選ばれ、状態行に `IDLE size=… crc=… (auto)`
   と出る(DIP スイッチを読む必要はない。DIP 全 OFF の基板は 1 番として
   応答する)。`boards 01,20 answer: unplug 485 or pick one` なら 485 経由の
   基板も答えているので、485 を抜いて KEY2 → `UPDATE FW` で入り直す(再
   スキャン)か、UP/DOWN で選ぶ。`no board answers 0x29` は無応答、UP/DOWN で個別に当たったとき
   `ACK_INVALID_CMD` なら OTA 非対応の旧 FW(SWD で焼く)
4. KEY1 で書き込み開始。約 1100 チャンク、1〜3 分。**途中でボタンは効かない**
   (KEY3 の消灯のみ)。ケーブルを抜かないこと
5. `DONE` で完了。KEY2 でメニューに戻ると白待機が走り、再起動した基板の
   工場デモを止める。`FAILED` のときはログ行(journal にも全文)を見て
   KEY1 でやり直す

0x28 の後に基板が USB を再列挙しない(`dmesg` に `error -71`)ことがある。
UI は 25 秒待って応答が無ければ `xhci-hcd` を unbind/bind して復旧を試みる
(`sudo -n` が通ること = セットアップ手順 3)。不要なら `UI_ARGS` に
`--no-usb-rebind` を足す。イメージを差し替えるときは `--firmware PATH`。
CLI からは `host/ota.py FW/FW_260903/OTA_16c.bin --addr auto` で同じ
スキャンが使える(`--check --addr auto` で応答する基板の一覧だけ出す)。

## 10 台への複製(ゴールデンイメージ)

10 台の Radxa は **1 種類の microSD イメージ**で運用し、個体差はホスト名
`radxa-01`〜`radxa-10` だけにする。IP はホスト名から導出する
(`radxa-NN` → `192.168.50.(100+NN)/24`、GW/DNS `192.168.50.1`)。
開発機が `radxa-01` = `.101`。

仕組みは 2 段:

1. **Radxa 純正の初回起動フック**: `rsetup.service` が毎起動時に
   `/config/before.txt` → `config.txt` → `after.txt` を処理し、before/after は
   処理後に削除される。`/config` は 16 MB の FAT パーティションで、
   **イメージを書いた直後の microSD を Windows で開いて置ける**
2. **`epaper-firstboot.service`**(`radxa/firstboot.sh`、rsetup の後に毎起動
   実行、冪等): ホスト名が `radxa-NN` なら Wi-Fi プロファイルの IPv4 を
   導出値に合わせる。すでに一致していれば何もしない

### ゴールデンイメージの作り方

開発機側(`radxa-01`)の準備:

```bash
cd ~/E-paper_H_WALL_BRICKS_Raspi && git status      # clean であること
systemctl is-enabled epaper-ui epaper-firstboot      # 両方 enabled
sudo apt clean
sudo journalctl --vacuum-size=20M
rm -f ~/.bash_history
sudo truncate -s0 /etc/machine-id                    # 次回起動時に再生成
sudo poweroff
```

microSD を Windows 機に挿す。**Windows からはカードのパーティションが
見えない**(FAT の `/config` もパーティション種別 GUID が basic data でないため
ドライブレターが付かない)し、**`wsl --mount` は USB カードリーダーを
アタッチできない**(HCS 0x8007000f)。そのため生ディスクの読み書きは
`radxa/clone/rawdisk.py` を管理者権限の Python で動かして行う。

```powershell
# 1. 読み出し(ディスク番号は Get-Disk で確認。28.9 GB の USB)
Get-Disk
Start-Process python -Verb RunAs -ArgumentList 'radxa\clone\rawdisk.py','read','2','D:\radxa-golden\radxa-01-full.img','D:\radxa-golden\read.log'
Get-Content D:\radxa-golden\read.log -Tail 1     # DONE まで約 16 分(33 MB/s)

# 2. 縮小(WSL Ubuntu、root。gdisk が必要。loop デバイスは /mnt/d 上の
#    ファイルでは使えないので、出力は WSL 内に置いてから D: へコピーする)
wsl -d Ubuntu -u root -- bash radxa/clone/shrink.sh /mnt/d/radxa-golden/radxa-01-full.img /root/golden/radxa-01-golden.img
wsl -d Ubuntu -u root -- cp /root/golden/radxa-01-golden.img /mnt/d/radxa-golden/
```

`shrink.sh` はルート ext4 を `resize2fs -M` で最小化し、その直後でファイルを
切り詰める(約 5 GB)。**GPT のパーティション表は触らない**: パーティション 3
はカード全体(28.5 GB)のままなので、クローン側は初回起動の `resize_root`
(rsetup、`resize2fs`)でファイルシステムを広げるだけでよい。切り詰めで
失われる末尾のバックアップ GPT ヘッダは、書き込み時に `rawdisk.py` が
ディスク末尾に作り直す。

### 実績(2026-09-16)

radxa-02〜10 の 9 台をこの手順で作成し、全台の起動を確認した。カードは
ゴールデンより 55 MB 小さい個体があり、`rawdisk.py` がルートパーティションの
終端を自動で詰めた(正常動作)。初回起動で `epaper-firstboot` が rsetup の
完了を待たず `.101` を設定してしまう競合が 02 で見つかり、修正済み
(e489b84、ゴールデンイメージにも反映)。03 以降は手作業なしで揃った。

### クローンの作り方(カード 1 枚ごと)

```powershell
.\radxa\clone\write_card.ps1 -Unit 5 -Disk 2     # radxa-05 = 192.168.50.105
```

やること: ディスクが USB でカードサイズであることを確認 → `YES` の入力 →
WSL の `mkconfig.sh` がゴールデンイメージから 16 MB の `/config`
パーティションを取り出し `before.txt` を書く:

```
update_hostname radxa-05
regenerate_ssh_hostkey
resize_root
```

→ 管理者権限(UAC)の `rawdisk.py write` がイメージ本体、`/config`
パッチ、ディスク末尾のバックアップ GPT を順に書く(5 GB で数分)。

起動すると rsetup がホスト名・SSH ホスト鍵・ルート FS 拡張を行い
`before.txt` を消す。続いて `epaper-firstboot` が `192.168.50.105` を設定する。
`machine-id` は空にしてあるので起動時に固有値が生成される。確認:

```bash
ssh radxa@192.168.50.105
hostname; df -h /; systemctl is-active epaper-ui
```

Wi-Fi プロファイルは MAC に束縛していない(`802-11-wireless.mac-address`
空)ので、どの個体でもそのまま繋がる。ルート FS の UUID とディスク GUID は
全機同一になるが、別々の機体なので問題ない。カードは同じ容量(28.9 GB)の
ものを使うこと。小さいカードにはパーティション 3 が収まらず `rawdisk.py`
が拒否する。

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
