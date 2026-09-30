# 展示モード(EXHIBITION)― PC なし・ルータなしでショーを回す

> **要約**
> - **radxa-05 が Conductor 兼 Wi-Fi ホットスポット**(SSID `AZ-Epaper`、10.42.0.1)。
>   ほかの機体はそのホットスポットに固定アドレス `10.42.0.1NN` で入る。
>   ルータも PC も会場には要らない
> - ショーの音は **radxa-05 の USB スピーカー**から出る(`mpg123`)。
> - **Loop** を入れておくと、ショーが終わるたびに待ち時間(既定 30 秒)のあと
>   ③ START を Conductor 自身が押す(カウントダウン込み)。STOP で止まる
> - ショーのデータ(ワークスペース)は事務所の PC で作り、Conductor の画面の
>   **Send workspace to …** で radxa-05 に送る(ssh 不要)
> - 会場での操作はスマホ・タブレットをホットスポットにつなぎ **http://10.42.0.1:8765**

## 1. 構成

```
                AZ-Epaper (5 GHz ch36, WPA2)  10.42.0.0/24
radxa-05  ─┬─  radxa-01  10.42.0.101:8787
 10.42.0.1 ├─  radxa-02  10.42.0.102:8787
 Conductor ├─  …
 :8765     ├─  radxa-10  10.42.0.110:8787
 USB スピーカ └─  スマホ / タブレット(操作画面、DHCP)
 自分の衣装は 127.0.0.1:8787
```

- Conductor は `python3 -m conductor serve --workspace /home/radxa/exhibition --host 0.0.0.0 --speaker --port 8765`
  を systemd(`epaper-conductor.service`)が常時動かす。落ちても 10 秒で立ち上がる
- 機体の割り当ては `/home/radxa/exhibition/fleet.json`(雛形 `radxa/exhibition/fleet.json`)。
  radxa-05 自身の衣装は `127.0.0.1:8787`
- ルータのネットワーク(192.168.51.x)と両方が見えるところでは、**radxa-01〜04・06〜10 は
  ルータを優先**する(ホットスポットのプロファイルは priority −10)。会場ではルータが
  無いのでホットスポットに落ちる

## 2. radxa-05 で一度だけやること

ssh で `radxa@192.168.51.105`(事務所)に入って、順に。

### 2.1 mpg123 を入れる

```bash
sudo apt-get update
sudo apt-get install -y mpg123
mpg123 --version          # 1.26.x と出ること
```

USB スピーカーを挿し、ALSA の既定出力になっていることを確認する:

```bash
aplay -l                  # card 1: ... USB Audio ... のように見えること
speaker-test -c 2 -t wav -l 1
```

鳴らないときは `/etc/asound.conf` に既定カードを書く(`card 1` は `aplay -l` の番号):

```
defaults.pcm.card 1
defaults.ctl.card 1
```

### 2.2 ホットスポットのプロファイル

`AZ-Epaper` のプロファイルはすでにある(無ければ次の 1 行で作る):

```bash
sudo nmcli dev wifi hotspot ifname wlan0 con-name AZ-Epaper ssid AZ-Epaper password hwall2026 band a channel 36
```

**起動時に自動でホットスポットになる**ようにする(会場に PC は無いので、手で
`nmcli con up` はできない)。ルータのプロファイルより優先度を上げる:

```bash
sudo nmcli con modify AZ-Epaper connection.autoconnect yes connection.autoconnect-priority 20
nmcli -f NAME,AUTOCONNECT,AUTOCONNECT-PRIORITY con show
```

> こうすると radxa-05 は事務所でもホットスポットとして立ち上がる(ルータには
> つながない)。事務所から radxa-05 に入るときは PC を `AZ-Epaper` につないで
> `ssh radxa@10.42.0.1`。ルータに戻したいときだけ `sudo nmcli con up "<ルータの接続名>"`。

### 2.3 ワークスペースのフォルダと fleet.json

```bash
mkdir -p /home/radxa/exhibition
cp ~/E-paper_H_WALL_BRICKS_Raspi/radxa/exhibition/fleet.json /home/radxa/exhibition/fleet.json
```

ショーの中身(CSV・タイムライン・音楽)はまだ空でよい。あとで PC から送る(4 章)。

### 2.4 サービスを入れて有効にする

```bash
sudo cp ~/E-paper_H_WALL_BRICKS_Raspi/radxa/epaper-conductor.service /etc/systemd/system/epaper-conductor.service
sudo systemctl daemon-reload
sudo systemctl enable --now epaper-conductor
systemctl status epaper-conductor --no-pager
journalctl -u epaper-conductor -n 20 --no-pager
```

ログに次のように出れば動いている:

```
conductor UI: http://127.0.0.1:8765
conductor UI: http://10.42.0.1:8765
  workspace /home/radxa/exhibition  speaker: mpg123 on this host
```

`speaker: mpg123 not found` と出たら 2.1 をやり直す(Conductor は止まらない。
音が出ないだけ)。`epaper-ui`(LCD のメニュー)はそのまま動かしておく ―
radxa-05 の衣装はそれが描く。

## 3. ほかの機体(radxa-01〜04・06〜10)で一度だけやること

各機体に ssh で入り(事務所のルータ経由 `radxa@192.168.51.1NN`)、
**その機体の番号 NN を入れて** 1 行:

```bash
sudo nmcli con add type wifi ifname wlan0 con-name AZ-Epaper ssid AZ-Epaper \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk hwall2026 \
  ipv4.method manual ipv4.addresses 10.42.0.1NN/24 ipv6.method ignore \
  connection.autoconnect yes connection.autoconnect-priority -10
```

例: radxa-03 なら `ipv4.addresses 10.42.0.103/24`。radxa-05 には**作らない**
(自分がホットスポット)。

確認:

```bash
nmcli -f NAME,AUTOCONNECT,AUTOCONNECT-PRIORITY con show     # AZ-Epaper が -10、ルータの方が 0 以上
```

priority −10 なので、**ルータとホットスポットの両方が見える場所ではルータが勝つ**
(事務所ではこれまでどおり PC の Conductor が 192.168.51.x で機体を見る)。
会場ではルータが無いので `AZ-Epaper` に入る。

> `radxa/firstboot.sh`(毎起動、ホスト名から 192.168.51.1NN を Wi-Fi プロファイルに
> 入れる)は **`AZ-Epaper` という名前のプロファイルを飛ばす**ようにしてある
> (2026-09-30)。各機体で `git pull` して新しい firstboot.sh にしておくこと ―
> 古いままだと、会場で `AZ-Epaper` が有効なとき(一覧の先頭に来る)にそのプロファイルが
> 192.168.51.1NN に書き換えられ、ホットスポットから外れる。

`git pull` は LCD メニュー末尾の **GIT PULL** 行か、ssh で
`cd ~/E-paper_H_WALL_BRICKS_Raspi && git pull --ff-only`。

## 4. ショーを radxa-05 に送る(PC から、ssh 不要)

1. 事務所の PC で `Start Conductor.bat` → いつもどおり Designs / Timeline でショーを作る
   (CSV、タイムライン、音楽、遷移、機体の割り当て)。**機体の割り当て(radxa-NN)は
   会場と同じにしておく**
2. PC を radxa-05 に届くネットワークにつなぐ:
   - radxa-05 がホットスポットで立ち上がっているなら PC を `AZ-Epaper` に入れる →
     送り先は **`10.42.0.1:8765`**
   - radxa-05 がルータにつながっているなら送り先は **`192.168.51.105:8765`**
3. PC の Conductor の **Units タブ → SEND THIS WORKSPACE TO ANOTHER CONDUCTOR**:
   送り先を入れて **Send workspace to …**。進み具合(MB)と、向こうが受け取った内容
   (CSV の数・キューの数・音楽・機体ごとのショー id)が出る
4. 断られるとき:
   - `a run is active on this Conductor - STOP it first` … radxa-05 でショーが動いている
     (Loop の待ち時間中も含む)。10.42.0.1:8765 を開いて STOP してから送り直す
   - `fleet token required` … radxa-05 の `fleet.json` に `token` がある。PC 側の
     `showdata/fleet.json` にも同じ `token` を書く
   - `the workspace is at most 200 MB` … 音楽ファイルが大きすぎる(音楽の上限は 64 MB)

送ったあと radxa-05 では**ショーがまだ機体に書かれていない**(タイムラインが新しい)。
5 章の ① Upload から。

コマンドラインでも同じことができる(トークンがあれば `-H "X-Show-Token: …"`):

```bash
curl -o ws.tar http://127.0.0.1:8765/api/workspace/export                        # PC 側で書き出し
curl --data-binary @ws.tar -H "Content-Type: application/x-tar" http://10.42.0.1:8765/api/workspace/import
```

## 5. 会場での操作

1. 衣装側の 12 V を全部入れてから、機体(radxa-01〜10)の電源を入れる
   (順番は docs/CONDUCTOR_START.md 6 章と同じ)
2. スマホ / タブレットを Wi-Fi `AZ-Epaper`(パスワード `hwall2026`)につなぎ、ブラウザで
   **http://10.42.0.1:8765** を開く → **Units** タブ
3. 全機体のタイルが online になるのを待つ(2 秒ごとに更新)
4. **① Upload** → 全タイルが `written` になるまで待つ → **② Show preset**
5. **Loop: next run after [30] s** にチェック(待ち時間は 10〜600 秒。ショーと一緒に
   保存されるので、PC で入れて送ってあれば入ったまま)
6. **③ START**。カウントダウン(既定 11 秒)のあと 0:00 で曲と絵が始まる。ショーが
   終わると大きな時計が `ENDED · NEXT RUN IN 0:30 (run 2)` と数え、0 で ③ START を
   Conductor が押す。NOW → NEXT ボード(ステージモニターも)も同じカウントを出す
7. 止めるときは **STOP**(Loop も止まる。次の ③ START までは再開しない)

- **音**: radxa-05 のスピーカーから出る。ブラウザ側の MUSIC 行には
  `music plays on the Conductor host (USB speaker)` と出て、**ブラウザの再生は既定で
  ミュート**(二重に鳴らさないため)。手元でも聞きたければ **Unmute**(そのブラウザに
  記憶される)。`music plays on the Conductor host — mpg123 not found` なら 2.1
- **HOLD / RESUME / NEXT / MOVE** はいつもどおり(音も追いかける)。Loop の待ち時間中に
  MOVE や NEXT をすると待ちはいったん解除され、次にショーが終わったときにまた数える
- **Clear pictures after the show** と Loop を両方入れたときは、消去は **STOP のとき**
  だけ(ラン間では消さない ― 次のランが同じ絵を使う)
- Loop の再スタートが断られたとき(機体が 1 台落ちている等)は、③ START を押したときと
  同じ理由が `Loop: the next run could not start yet — …` と出て、5 秒ごとに再試行する。
  その機体を直す(電源・USB)か、STOP で止める

## 6. 困ったとき

| 症状 | 見るところ・対処 |
|---|---|
| 10.42.0.1:8765 が開かない | radxa-05 の電源。ホットスポットの SSID が見えるか。`ssh radxa@10.42.0.1` で `systemctl status epaper-conductor` |
| 機体が offline | その機体の電源。`AZ-Epaper` に入っているか(LCD の上部バーの IP が 10.42.0.1NN か)。3 章のプロファイルが無い・番号違い |
| 音が出ない | MUSIC 行のメッセージ。`mpg123 not found` → 2.1。`mpg123 exited` → スピーカーの抜き差し、`sudo systemctl restart epaper-conductor`。曲が radxa-05 に無い(`no track loaded there yet`)→ 4 章で送り直す |
| 音が絵より遅れる / 早い | `--speaker-lead-ms`(既定 50 = アンパウズを測った往復時間 + 50 ms 早く送る)を service の ExecStart で変えて `daemon-reload` + `restart`。数十 ms 単位 |
| Loop が回らない | `Loop` のチェック、待ち時間が 10〜600 か。Units タブの `Corrected automatically:` の行に `Loop: …` の理由 |
| ショーを差し替えたい | 会場でも PC を `AZ-Epaper` に入れれば 4 章の手順で送れる(先に STOP) |

ログ: `journalctl -u epaper-conductor -f`(`speaker: loaded xxx.mp3, unpause latency 12 ms`、
`Loop: run 2 started` などが出る)。
