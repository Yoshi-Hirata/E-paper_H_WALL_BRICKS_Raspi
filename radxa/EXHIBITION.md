# 展示モード(EXHIBITION)― PC なし・ルータなしでショーを回す

> **要約**
> - **radxa-05 が Conductor 兼 Wi-Fi ホットスポット**(SSID `AZ-Epaper`、10.42.0.1)。
>   **radxa-05 は制御専用 ― 衣装は付けない**(ショーの中で radxa-05 に衣装を割り当てない)。
>   ほかの機体はそのホットスポットに固定アドレス `10.42.0.1NN` で入る。
>   ルータも PC も会場には要らない
> - ショーの音は **radxa-05 のスピーカー**から出る(`mpg123` → PulseAudio → Bluetooth の Bose、
>   または USB スピーカー)
> - **Loop** を入れておくと、ショーが終わるたびに待ち時間(既定 45 秒、40 秒以上)のあと
>   ③ START を Conductor 自身が押す(カウントダウン込み)。STOP で止まる
> - ショーのデータ(ワークスペース)は事務所の PC で作り、Conductor の画面の
>   **Send workspace to …** で radxa-05 に送る(ssh 不要)
> - 会場での操作はスマホ・タブレットをホットスポットにつなぎ **http://10.42.0.1:8765**。
>   最初に **パスコード**を聞かれる(radxa-05 で決めたもの。1 回入れれば覚える)
> - **Wi-Fi のパスワードは来場者に教えない**。git の例(`hwall2026`)から**必ず変える**

## 1. 構成

```
                AZ-Epaper (5 GHz ch36, WPA2)  10.42.0.0/24
radxa-05  ─┬─  radxa-01  10.42.0.101:8787
 10.42.0.1 ├─  radxa-02  10.42.0.102:8787
 Conductor ├─  …
 :8765     ├─  radxa-10  10.42.0.110:8787
 USB スピーカ └─  スマホ / タブレット(操作画面、DHCP。機体の番地は予約済みで取られない)
 衣装なし(制御専用、自分のエージェント 127.0.0.1:8787 でタイルだけ出る)
```

- Conductor は `python3 -m conductor serve --workspace /home/radxa/exhibition --host 0.0.0.0
  --speaker --speaker-output pulse --adopt --port 8765` を systemd(`epaper-conductor.service`)が
  常時動かす。落ちても 10 秒で立ち上がる
- **Conductor だけの再起動なら続く**(サービスの `--adopt`。**radxa-05 だけ** ― PC の Conductor は
  今までどおり再起動すると Upload からで、この引き取りをしない): Conductor が立ち上がると
  自分のワークスペースをコンパイルし、**同じショー(同じ id)を持っていて絵が書き込み済み**(一部の基板が失敗した
  `failed` も含む ― START の関門が判断する)の機体をそのまま「持っている」とみなす
  (`Corrected automatically:` に `radxa-01: holds this show already (adopted after a restart of
  the conductor)`)。引き取った機体は「その時点のタイムラインを持っている」印が付くので、
  そのあとタイムラインを編集して ③ START を押すと従来どおり `Upload again` と断られる
  (古い絵で新しいタイムラインが走ることはない)。機体がショーを走らせていればランも引き取り、Loop はそのランの終わりから、
  または次の ③ START からまた回る。別のショーを持っている機体・絵の無い機体は
  `… - Upload before START` と出るので **① Upload**。**引き取れるのは Conductor 側だけが
  再起動したとき**: 機体ごと電源が落ちた(会場の停電・ブレーカー)あとは各機体のエージェントが
  絵を `none`(再起動後は未書き込み)と報告するので、**radxa-05 の画面から ① Upload が必要**
  (5 章)。radxa-05 の Conductor に送ったあとまだ Upload していないときも同じ
- **Conductor は 1 台だけ**: PC の Conductor と radxa-05 の Conductor が同じネットワークで
  同じ機体を見ると、互いの T0 を「補正」し合って喧嘩する。会場では PC の Conductor を閉じる
  (事務所で radxa-05 に送るときも、送ったら PC 側は Units タブを開いたまま START しない)
- **PC では展示専用の Conductor を使う**: `Start Exhibition Conductor.bat`(ポート **8766**、
  フォルダ **`exhibition-data/`**、ページに琥珀色の **EXHIBITION** バッジ、ウィンドウタイトル
  `EXHIBITION · Conductor`)。本番ショーの `Start Conductor.bat`(8765、`showdata/`)とは
  **別のアプリ**として振る舞い、データも混ざらない。ただし中身は同じプログラムなので、
  **2 つを同時に機体へ向けない**(片方だけ Units タブで Upload / START する)。4 章
- 機体の割り当ては `/home/radxa/exhibition/fleet.json`(雛形 `radxa/exhibition/fleet.json`:
  `units`、`hotspot`(= radxa-05)、`passcode`)。radxa-05 自身は `127.0.0.1:8787`
- ルータのネットワーク(192.168.51.x)と両方が見えるところでは、**radxa-01〜04・06〜10 は
  ルータを優先**する(`AZ-Epaper` のクライアントプロファイルは priority −10)。会場では
  ルータが無いのでホットスポットに落ちる。radxa-05 は 2.2 の仕組みで決める

## 2. radxa-05 で一度だけやること

ssh で `radxa@192.168.51.105`(事務所)に入って、順に。リポジトリは最新に
(`cd ~/E-paper_H_WALL_BRICKS_Raspi && git pull --ff-only`)。

### 2.1 mpg123 を入れる・音量を固定する(USB スピーカーのとき)

```bash
sudo apt-get update
sudo apt-get install -y mpg123 alsa-utils
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

音量を決めて**保存**する(`--speaker-output alsa` で ALSA 直に鳴らすときはここで決めた
値がそのまま会場の音量。既定の `pulse` では 2.1b の `pactl` と本体のボタン):

```bash
amixer sset Master 80%     # Master が無いカードは `amixer scontrols` で名前を見る(PCM など)
sudo alsactl store
```

### 2.1b Bluetooth スピーカー(Bose など)

2026-09-30 に radxa-05 と **Bose SoundLink Flex** で確認した手順(radxa-05 では済んでいる。
イメージを焼き直した機体では**この順に**もう一度)。Bose の USB は HID だけで USB オーディオ
にならないので Bluetooth(A2DP)で使う。サービスは `--speaker-output pulse`(radxa ユーザの
PulseAudio 経由。Bluetooth でも USB スピーカーでも同じ設定で鳴る。PulseAudio が変なときの
逃げ道は 2.1 の USB スピーカー + `--speaker-output alsa`)。

a. **使っていないログイン画面のユーザ `sddm` を止め、radxa の PulseAudio を常駐にする**
   (`sddm` が自分の PulseAudio を立ち上げて A2DP の口を先に掴むため、sink が出てこなかった):

```bash
sudo systemctl disable --now sddm
sudo loginctl enable-linger radxa
mkdir -p ~/.config/pulse && echo "exit-idle-time = -1" >> ~/.config/pulse/daemon.conf && systemctl --user restart pulseaudio
```

b. **ペアリング**。スピーカーをペアリングモード(青点滅)にし、**近くのスマホ・PC の Bluetooth を
   切る**(Bose はそちらに自動接続してペアリングモードから抜ける)。BlueZ はペアリングしていない
   スキャン結果を 30 秒ほどで忘れるので、**スキャンとペアリングは 1 回の bluetoothctl セッションで**:

```bash
bluetoothctl --timeout 20 scan on            # MAC を探す
bluetoothctl devices | grep -v LE-           # `LE-…` は BLE(音声ではない)。うちの Bose: AC:BF:71:FA:8F:AB
MAC=AC:BF:71:FA:8F:AB
{ echo "scan on"; for i in $(seq 1 30); do sleep 2; bluetoothctl devices | grep -q "^Device $MAC" && break; done; echo "pair $MAC"; sleep 8; echo "trust $MAC"; sleep 1; echo "connect $MAC"; sleep 6; echo "quit"; } | bluetoothctl
```

c. **既定の sink にする**(`<MAC_>` は MAC の `:` を `_` にしたもの):

```bash
pactl list short sinks                       # bluez_sink.AC_BF_71_FA_8F_AB.a2dp_sink が出ること
pactl set-default-sink bluez_sink.AC_BF_71_FA_8F_AB.a2dp_sink
pactl set-sink-volume bluez_sink.AC_BF_71_FA_8F_AB.a2dp_sink 100%
```

   **スピーカー本体の +/− ボタンはこの構成では効かない**(AVRCP の音量通知もキーイベントも来ない ―
   スピーカーは音源に従う)。音の大きさは **音源側の AVRCP 絶対音量**、つまり Conductor が
   busctl で bluez の `MediaTransport1 Volume`(0〜127。47 は小さい、90 が快適、127 が最大)に
   書く値で決まり、PulseAudio の sink は 100 % に固定しておく。決め方は 3 つ、どれも同じ値
   (`fleet.json` の `"speaker_volume"`、0〜100、既定 70。ホストごとの設定でショーには入らない):
   ページの MUSIC 行のスライダー / ± ボタン、radxa-05 の LCD の EXHIBITION 画面の LEFT / RIGHT、
   または fleet.json を直接。Conductor は起動時・スピーカーがつなぎ直ったとき(transport の
   `fdN` は再接続のたびに変わるので 5 秒ごとに探し直す)・値を変えたときに適用する。
   USB スピーカー(pulse の既定 sink が `bluez_sink.*` でない)なら同じ値を
   `pactl set-sink-volume @DEFAULT_SINK@ NN%` で。手で確かめるなら:

```bash
busctl --system tree org.bluez | grep -oE '/org/bluez/hci0/dev_AC_BF_71_FA_8F_AB/sep[0-9]+/fd[0-9]+'
busctl --system get-property org.bluez <そのパス> org.bluez.MediaTransport1 Volume     # q 47 など
busctl --system set-property org.bluez <そのパス> org.bluez.MediaTransport1 Volume q 90
```

d. **テスト**: `mpg123 -o pulse <曲>` を 10 秒。sink-input は出ているのに無音なら、スピーカーが
   別の音源(スマホ)を鳴らしている ― `bluetoothctl disconnect $MAC; bluetoothctl connect $MAC`
   で radxa-05 が有効な音源になる。

e. **注意**: A2DP は **100〜200 ms** 遅れる ― 測って `--speaker-lead-ms` に入れる(6 章)。
   スピーカーの電源を入れ直したあとは自動で再接続するはず(Trusted)― 一度確かめる。
   **スピーカーの自動オフのタイマーは切る**か、Loop の待ち時間をそれより短くしておく。

### 2.2 ホットスポットと「展示モードの武装」

`AZ-Epaper` のプロファイルはすでにある(無ければ次の 1 行で作る。**パスワードは
必ず変える**):

```bash
sudo nmcli dev wifi hotspot ifname wlan0 con-name AZ-Epaper ssid AZ-Epaper password <新しいパスワード> band a channel 36
sudo nmcli con modify AZ-Epaper connection.autoconnect no
```

`AZ-Epaper` は **autoconnect=no のまま**(LCD の WIFI 行の前提。ui の WIFI 行は再起動で
戻る)。会場で人手なしにホットスポットになるのは次の oneshot サービスの仕事:

```bash
sudo cp ~/E-paper_H_WALL_BRICKS_Raspi/radxa/epaper-exhibition-net.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable epaper-exhibition-net
```

`radxa/exhibition-net.sh` は起動時に **最長 90 秒、クライアントの Wi-Fi(ルータ)が
自分で上がる(`activated`)のを待ち、上がらなければ `nmcli con up AZ-Epaper`** する
(どちらでも journal に 1 行)。つまり **このサービスを有効にした状態 = 展示モードの武装**:

- 事務所や、ルータを持ち込んだ会場 → radxa-05 は今までどおりルータのクライアント
  (PC の Conductor から 192.168.51.105 で見える)。「上がった」と数えるのは STATE が
  `activated` のプロファイルだけ(つなぎに行って失敗したものは数えない)
- ルータの無い会場 → 90 秒後に radxa-05 がホットスポットになる。**知っているルータの SSID が
  スキャンに見えている間は最長 10 分待つ**(停電のあとルータの立ち上がりが遅くても負けない)
- どちらでも LCD の **WIFI** 行でいつでも手で切り替えられる
- 展示が終わったら `sudo systemctl disable epaper-exhibition-net` で武装解除。有効のままでも
  ルータが見えていれば radxa-05 はルータに入る ― **ただし、ルータと機体が一緒に停電した
  あとルータが 10 分以上戻らないと、radxa-05 はホットスポットになり、ほかの機体もそちらに
  落ちてくる**。戻すには radxa-05 の LCD の WIFI 行でルータを選ぶか、10.42.0.1:8765 の
  Units タブで **All units → router in 20 s**

> なぜ priority や autoconnect で決めないか: `AZ-Epaper` を autoconnect にすると事務所でも
> ホットスポットとして立ち上がってルータに入らず、LCD の WIFI 行(再起動でクライアントに
> 戻る前提)とも食い違う。「ルータが見えなければホットスポット」を 1 度だけ判断する
> oneshot が一番安全。

**DHCP の予約**(スマホが機体の番地を取らないように)。NM 1.42.4 には
`ipv4.shared-dhcp-range` が無いので、dnsmasq の予約ファイルを置く:

```bash
sudo cp ~/E-paper_H_WALL_BRICKS_Raspi/radxa/exhibition/az-epaper-dhcp.conf /etc/NetworkManager/dnsmasq-shared.d/
sudo nmcli con down AZ-Epaper; sudo nmcli con up AZ-Epaper     # 読み直し(下がっていれば up だけ)
```

ファイルには機体ごとの `dhcp-host=<wlan0 の MAC>,10.42.0.1NN,radxa-NN` が入っている。
**radxa-06 と radxa-08 は未記入**(2026-09-30 に読めなかった)― その機体で
`cat /sys/class/net/wlan0/address` を読んで行のコメントを外す。機体側の固定アドレス
(3 章)はそのまま持たせる(予約は二重の安全)。

### 2.3 ワークスペースのフォルダと fleet.json

```bash
mkdir -p /home/radxa/exhibition
cp ~/E-paper_H_WALL_BRICKS_Raspi/radxa/exhibition/fleet.json /home/radxa/exhibition/fleet.json
nano /home/radxa/exhibition/fleet.json      # "passcode" を決めて書き換える(必須)
chmod 600 /home/radxa/exhibition/fleet.json
```

**パスコードはこのファイルだけに置く**(サービスファイルには書かない ― `systemctl show` で
見えてしまう)。`--host 0.0.0.0` の Conductor は、パスコードが無いか例の値
`CHANGE-ME-2026` のままだと **起動を拒否**する(journal に `refusing to serve on 0.0.0.0: …`)。
ショーの中身(CSV・タイムライン・音楽)はまだ空でよい。あとで PC から送る(4 章)。

### 2.4 サービスを入れて有効にする

```bash
sudo cp ~/E-paper_H_WALL_BRICKS_Raspi/radxa/epaper-conductor.service /etc/systemd/system/epaper-conductor.service
sudo systemctl daemon-reload
sudo systemctl enable --now epaper-conductor
systemctl status epaper-conductor --no-pager
journalctl -u epaper-conductor -n 20 --no-pager
```

ログに次のように出れば動いている(2 行目の番地は、そのとき radxa-05 が持っている
IPv4 ― ルータのクライアントなら `192.168.51.105`、ホットスポットなら `10.42.0.1`):

```
conductor UI: http://127.0.0.1:8765
conductor UI: http://192.168.51.105:8765
  workspace /home/radxa/exhibition  speaker: mpg123 on this host  passcode: set
```

`speaker: mpg123 not found` と出たら 2.1 をやり直す(Conductor は止まらない。
音が出ないだけ)。`epaper-ui`(LCD のメニュー)はそのまま動かしておく。

**音が本当に出る状態かを確かめる**(サービスはログインセッションを持たないので、
`Environment=XDG_RUNTIME_DIR=/run/user/1000` と `PULSE_SERVER=…` で radxa の PulseAudio に
つないでいる。これが無いと `-o pulse` はサーバを見つけられない):

```bash
systemctl status epaper-conductor --no-pager      # active (running)
journalctl -u epaper-conductor -n 20 --no-pager   # speaker: loaded xxx.mp3, unpause latency … / volume 70 applied via bluez
```

そのあと画面(10.42.0.1:8765 か 192.168.51.105:8765)の MUSIC 行が
`music plays on the Conductor host (USB speaker) — loaded, xxx.mp3, …` と **`loaded`** になっている
こと(`mpg123 exited` や `volume: …` のエラーではなく)を見てから、テストで ③ START を 1 回。

## 3. ほかの機体(radxa-01〜04・06〜10)で一度だけやること

まず**その機体の番号 NN** を確かめる: LCD の各画面の上部バーに `radxa-03` のように
ホスト名が出る(ssh なら `hostname`)。各機体に ssh で入り(事務所のルータ経由
`radxa@192.168.51.1NN`)、リポジトリを最新にして(`git pull --ff-only`、または LCD
メニュー末尾の **GIT PULL**)、**NN を入れて** 1 行:

```bash
sudo nmcli con add type wifi ifname wlan0 con-name AZ-Epaper ssid AZ-Epaper \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk <2.2 で決めたパスワード> \
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
会場ではルータが無いので `AZ-Epaper` に入る。`radxa/firstboot.sh`(毎起動)は
`AZ-Epaper` という名前・SSID のプロファイルを飛ばすので、この設定を
192.168.51.1NN に書き換えることはない(main 8682002 以降。古い機体は `git pull`)。

Conductor の Units タブの **All units → AZ-Epaper in 20 s / All units → router in 20 s**
でも全機体をまとめて切り替えられる(5 章)。

## 4. ショーを radxa-05 に送る(PC から、ssh 不要)

> **PC には Conductor が 2 つある。** 本番ショーの **`Start Conductor.bat`**(ポート 8765、
> データは `showdata/`)と、展示の **`Start Exhibition Conductor.bat`**(ポート **8766**、
> データは **`exhibition-data/`**)。同じプログラムだが、展示のほうはページの左上に琥珀色の
> **EXHIBITION** バッジ、ブラウザのタブは `EXHIBITION · Conductor`、黒いウィンドウのタイトルは
> `E-paper Exhibition Conductor`、タブのアイコンも琥珀色 ― 本番の窓と見分けがつく。
> データも別フォルダなので、展示のショーを作っても本番のショーは変わらない
> (バックアップも別: `Backup exhibition-data.bat` → `..\exhibition-data-<日時>.zip`)。
>
> **決まり: 2 つの Conductor を同時に機体へ向けない。** PC の展示 Conductor(8766)の役目は
> **ショーを作る・リハーサルで Upload する・radxa-05 にワークスペースを送る**まで。
> 会場では **radxa-05 自身の Conductor** がショーを回す(PC の Conductor は閉じる)。
> 本番 Conductor(8765)と展示 Conductor(8766)の両方で Units タブを開いて Upload / START
> することは決してしない(互いの T0 を補正し合って喧嘩する ― 1 章)。

1. 事務所の PC で **`Start Exhibition Conductor.bat`** → http://localhost:8766 が開く
   (ページ左上に **EXHIBITION**)。いつもどおり Designs / Timeline でショーを作る
   (CSV、タイムライン、音楽、遷移、機体の割り当て)。**機体の割り当て(radxa-NN)は
   会場と同じにし、radxa-05 には衣装を割り当てない**。
   初回は `exhibition-data/` が作られ、その中に `fleet.json`(`"hotspot": "radxa-05"` と
   コメントだけ。機体はルータの既定 `192.168.51.1NN`)が一度だけ書かれる ― 以後は
   触らない(手で書き換えたものが残る)
2. PC を radxa-05 に届くネットワークにつなぐ:
   - radxa-05 がルータにつながっているなら送り先は **`192.168.51.105:8765`**
   - radxa-05 がホットスポットで立ち上がっているなら PC を `AZ-Epaper` に入れる →
     送り先は **`10.42.0.1:8765`**
3. PC の展示 Conductor の **Units タブ → SEND THIS WORKSPACE TO ANOTHER CONDUCTOR**:
   送り先を入れて **Send workspace to …**。PC の Conductor が相手に **本当に Conductor か**
   (`/api/fleet` が答えるか)を確かめてから送る。進み具合(MB)と、向こうが受け取った内容
   (CSV の数・キューの数・音楽・機体ごとのショー id)が出る。PC 側の
   **`exhibition-data/fleet.json`** に radxa-05 と同じ `"passcode"` を書いておく
   (送るときに一緒に渡す。`showdata/fleet.json` ではない)
4. 断られるとき:
   - `a run is active on this Conductor - STOP it first` … radxa-05 でショーが動いている
     (Loop の待ち時間中も含む)。10.42.0.1:8765 を開いて STOP してから送り直す
   - `passcode required` … radxa-05 のパスコードが PC 側の `exhibition-data/fleet.json` に無い
   - `fleet token required` … radxa-05 の `fleet.json` に `token` がある。PC 側にも同じ `token`
   - `the workspace is at most 200 MB` … 音楽ファイルが大きすぎる(音楽の上限は 64 MB)
   - `… is not a Conductor` … 送り先の番地が違う(機体のエージェント 8787 や別の機器)

送ったあと radxa-05 では**ショーがまだ機体に書かれていない**(タイムラインが新しい。
その前に Conductor が知っていた機体は「古い Upload」扱い)。5 章の ① Upload から。

コマンドラインでも同じことができる(パスコード・トークンはヘッダで):

```bash
curl -o ws.tar http://127.0.0.1:8766/api/workspace/export                        # PC の展示 Conductor(8766)から書き出し
curl --data-binary @ws.tar -H "Content-Type: application/x-tar" -H "X-Passcode: <パスコード>" http://10.42.0.1:8765/api/workspace/import
```

## 5. 会場での操作

1. 衣装側の 12 V を全部入れてから、機体(radxa-01〜10)の電源を入れる
   (順番は docs/CONDUCTOR_START.md 6 章と同じ)。radxa-05 は 30 秒ほどでホットスポットになる
2. スマホ / タブレットを Wi-Fi `AZ-Epaper` につなぎ、ブラウザで **http://10.42.0.1:8765**
   を開く → **Units** タブ。最初の操作でパスコードを聞かれる(1 回)
3. 全機体のタイルが online になるのを待つ(2 秒ごとに更新)。タイルの `Wi-Fi` 行に
   `AZ-Epaper · 10.42.0.1NN · 72%`(radxa-05 は `… · hotspot`)。まだルータ側に残っている機体が
   あれば **All units → AZ-Epaper in 20 s**(ホットスポットの radxa-05 には 5 秒、ほかは 20 秒
   後に切り替わる。ショー中の機体は断る。radxa-05 は最後に伝える)
4. **① Upload** → 全タイルが `written` になるまで待つ → **② Show preset**
   (再起動後で機体が同じショーを持っていれば Upload は要らない ― 1 章)
5. **Loop: next run after [45] s** にチェック(待ち時間は 40〜600 秒。ショーと一緒に
   保存されるので、PC で入れて送ってあれば入ったまま)
6. **③ START**。カウントダウン(既定 11 秒)のあと 0:00 で曲と絵が始まる。ショーが
   終わると大きな時計が `ENDED · NEXT RUN IN 0:45 (run 2)` と数え、0 で ③ START を
   Conductor が押す。NOW → NEXT ボード(ステージモニターも)も同じカウントを出す
7. 止めるときは **STOP**(Loop も止まる。次の ③ START までは再開しない)。
   帰るときに **All units → router in 20 s** を押しておくと、事務所に戻したとき
   全機体がルータに戻る(クライアントは 5 秒、radxa-05 は 20 秒後)

- **音**: radxa-05 のスピーカーから出る。ブラウザ側の MUSIC 行には
  `music plays on the Conductor host (USB speaker)` と出て、**ブラウザの再生は既定で
  ミュート**(二重に鳴らさないため)。手元でも聞きたければ **Unmute**(そのブラウザに
  記憶される)。`… — mpg123 not found` なら 2.1
- **HOLD / RESUME / NEXT / MOVE** はいつもどおり(音も追いかける)。Loop の待ち時間中に
  MOVE や NEXT をすると待ちはいったん解除され、次にショーが終わったときにまた数える。
  待ち時間中(と ENDED のあと)の ③ START は次のランをすぐ始める ― 「もう走っている、やり直す?」
  とは聞かれず `force` も付かない(意図した動作: 焼き込みの関門は最初の START と同じように聞く)
- **PC の Conductor でも見える新しいボタン**(Units タブの Send workspace to … と All units →
  AZ-Epaper / router)は確認ダイアログ付きだが**本物** ― PC で押せば本当に送る / 切り替える
- PC の Conductor は radxa-05 から届いたショー(ショーファイル・ワークスペース)の **Loop を
  受け取らない**(off に戻して `Corrected automatically:` に 1 行)。Loop を持てるのは
  `--adopt` の展示 Conductor だけ
- **Clear pictures after the show** と Loop を両方入れたときは、消去は **STOP のとき**
  だけ(ラン間では消さない ― 次のランが同じ絵を使う)
- **1 台が準備できないとき**(電源が落ちた、絵が消えた): Loop は ③ START と同じ理由で
  `Loop: the next run could not start yet — radxa-03: not answering` と出して 5 秒ごとに
  やり直し、**60 秒たっても揃わなければ揃った機体だけで次のランを始める**
  (`started without radxa-03 (not ready) - it joins this run as soon as it answers with the
  show`)。その機体は監視されたままで、**ショーを持って答えた瞬間に走行中のランへ入る**
  (`started late`)― 次のランを待たない。同じ理由のままなら次の再スタートでは 60 秒待たずに
  すぐ外す(理由が変われば、また 60 秒待つ)。1 着のために展示全体は止めない。Loop の
  再スタートは `force` を使わない(基板の書き込み失敗を「それでも始める」と決めるのは人が
  ③ START を押すときだけ)
- **会場で停電したら**: 機体は再起動すると絵を `none` と報告する(Conductor が引き取れるのは
  Conductor だけの再起動のとき)。Units タブで **① Upload** → `written` → ③ START(Loop は
  入ったまま)

## 6. 困ったとき

| 症状 | 見るところ・対処 |
|---|---|
| 10.42.0.1:8765 が開かない | radxa-05 の電源。ホットスポットの SSID が見えるか(起動から 30 秒以上待つ)。見えなければ LCD の WIFI 行で `AZ-Epaper` を選ぶ。`ssh radxa@10.42.0.1` で `systemctl status epaper-conductor epaper-exhibition-net` |
| パスコードを忘れた | radxa-05 の `/home/radxa/exhibition/fleet.json`(`sudo cat`)。ブラウザで入れ直したいときは `localStorage` の `conductor.passcode` を消す(または別のブラウザ) |
| `epaper-conductor` が起動しない、journal に `refusing to serve on 0.0.0.0` | fleet.json の `"passcode"` が無い / 例の値のまま。2.3 |
| 機体が offline | その機体の電源。`AZ-Epaper` に入っているか(LCD の上部バーの IP が 10.42.0.1NN か)。3 章のプロファイルが無い・番号違い。2.2 の予約に MAC が無い機体はスマホと番地がぶつかることがある |
| 音が出ない | MUSIC 行のメッセージ。`mpg123 not found` → 2.1。`mpg123 exited` → スピーカーの抜き差し、`sudo systemctl restart epaper-conductor`。曲が radxa-05 に無い(`no track loaded there yet`)→ 4 章で送り直す。Bluetooth: `pactl list short sinks` に `bluez_sink.…a2dp_sink` が無ければ 2.1b の b〜c(スピーカーの電源、スマホの Bluetooth を切る)、sink はあるのに無音なら `bluetoothctl disconnect` → `connect`(2.1b d)。音量はページの MUSIC 行のスライダー(= fleet.json `speaker_volume`)。本体のボタンは効かない(2.1b c) |
| 音が絵より遅れる / 早い | `--speaker-lead-ms`(既定 50 = 測ったパイプ往復 + 50 ms 早くアンパウズ。**50 は当て推量**、Bluetooth(A2DP)は 100〜200 ms 余計に遅れる: クリック音源で一度測って決める)を service の ExecStart で変えて `daemon-reload` + `restart` |
| Loop が回らない | `Loop` のチェック、待ち時間が 40〜600 か。Units タブの `Corrected automatically:` の行に `Loop: …` の理由 |
| 再起動後に START が `Upload again` / `Upload first` | 機体が持っているショーが今のタイムラインと違う(送り直した・編集した)。① Upload |
| ショーを差し替えたい | 会場でも PC を `AZ-Epaper` に入れれば 4 章の手順で送れる(先に STOP) |

ログ: `journalctl -u epaper-conductor -f`(`speaker: loaded xxx.mp3, unpause latency 12 ms`、
`Loop: run 2 started` などが出る)、`journalctl -u epaper-exhibition-net`。
