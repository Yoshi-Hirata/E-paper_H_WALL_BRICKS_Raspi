# 展示モード(EXHIBITION)― PC なし・ルータなしでショーを回す

> **要約**
> - **radxa-05 が Conductor 兼 Wi-Fi ホットスポット**(SSID `AZ-Epaper`、10.42.0.1)。
>   **radxa-05 は制御専用 ― 衣装は付けない**(ショーの中で radxa-05 に衣装を割り当てない)。
>   ほかの機体はそのホットスポットに固定アドレス `10.42.0.1NN` で入る。
>   ルータも PC も会場には要らない
> - ショーの音は **radxa-05 の USB スピーカー**から出る(`mpg123`)
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
  --speaker --speaker-output alsa --passcode … --port 8765` を systemd(`epaper-conductor.service`)
  が常時動かす。落ちても 10 秒で立ち上がる
- **Conductor だけの再起動なら続く**: Conductor が立ち上がると自分のワークスペースを
  コンパイルし、**同じショー(同じ id)を持っていて絵が書き込み済み**(一部の基板が失敗した
  `failed` も含む ― START の関門が判断する)の機体をそのまま「持っている」とみなす
  (`Corrected automatically:` に `radxa-01: holds this show already (adopted after a restart of
  the conductor)`)。機体がショーを走らせていればランも引き取り、Loop はそのランの終わりから、
  または次の ③ START からまた回る。別のショーを持っている機体・絵の無い機体は
  `… - Upload before START` と出るので **① Upload**。**引き取れるのは Conductor 側だけが
  再起動したとき**: 機体ごと電源が落ちた(会場の停電・ブレーカー)あとは各機体のエージェントが
  絵を `none`(再起動後は未書き込み)と報告するので、**radxa-05 の画面から ① Upload が必要**
  (5 章)。radxa-05 の Conductor に送ったあとまだ Upload していないときも同じ
- **Conductor は 1 台だけ**: PC の Conductor と radxa-05 の Conductor が同じネットワークで
  同じ機体を見ると、互いの T0 を「補正」し合って喧嘩する。会場では PC の Conductor を閉じる
  (事務所で radxa-05 に送るときも、送ったら PC 側は Units タブを開いたまま START しない)
- 機体の割り当ては `/home/radxa/exhibition/fleet.json`(雛形 `radxa/exhibition/fleet.json`:
  `units`、`hotspot`(= radxa-05)、`passcode`)。radxa-05 自身は `127.0.0.1:8787`
- ルータのネットワーク(192.168.51.x)と両方が見えるところでは、**radxa-01〜04・06〜10 は
  ルータを優先**する(`AZ-Epaper` のクライアントプロファイルは priority −10)。会場では
  ルータが無いのでホットスポットに落ちる。radxa-05 は 2.2 の仕組みで決める

## 2. radxa-05 で一度だけやること

ssh で `radxa@192.168.51.105`(事務所)に入って、順に。リポジトリは最新に
(`cd ~/E-paper_H_WALL_BRICKS_Raspi && git pull --ff-only`)。

### 2.1 mpg123 を入れる・音量を固定する

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

音量を決めて**保存**する(サービスは PulseAudio 無しの ALSA 直なので、ここで決めた
値がそのまま会場の音量):

```bash
amixer sset Master 80%     # Master が無いカードは `amixer scontrols` で名前を見る(PCM など)
sudo alsactl store
```

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

1. 事務所の PC で `Start Conductor.bat` → いつもどおり Designs / Timeline でショーを作る
   (CSV、タイムライン、音楽、遷移、機体の割り当て)。**機体の割り当て(radxa-NN)は
   会場と同じにし、radxa-05 には衣装を割り当てない**
2. PC を radxa-05 に届くネットワークにつなぐ:
   - radxa-05 がルータにつながっているなら送り先は **`192.168.51.105:8765`**
   - radxa-05 がホットスポットで立ち上がっているなら PC を `AZ-Epaper` に入れる →
     送り先は **`10.42.0.1:8765`**
3. PC の Conductor の **Units タブ → SEND THIS WORKSPACE TO ANOTHER CONDUCTOR**:
   送り先を入れて **Send workspace to …**。PC の Conductor が相手に **本当に Conductor か**
   (`/api/fleet` が答えるか)を確かめてから送る。進み具合(MB)と、向こうが受け取った内容
   (CSV の数・キューの数・音楽・機体ごとのショー id)が出る。PC 側の `showdata/fleet.json`
   に radxa-05 と同じ `"passcode"` を書いておく(送るときに一緒に渡す)
4. 断られるとき:
   - `a run is active on this Conductor - STOP it first` … radxa-05 でショーが動いている
     (Loop の待ち時間中も含む)。10.42.0.1:8765 を開いて STOP してから送り直す
   - `passcode required` … radxa-05 のパスコードが PC 側の `showdata/fleet.json` に無い
   - `fleet token required` … radxa-05 の `fleet.json` に `token` がある。PC 側にも同じ `token`
   - `the workspace is at most 200 MB` … 音楽ファイルが大きすぎる(音楽の上限は 64 MB)
   - `… is not a Conductor` … 送り先の番地が違う(機体のエージェント 8787 や別の機器)

送ったあと radxa-05 では**ショーがまだ機体に書かれていない**(タイムラインが新しい。
その前に Conductor が知っていた機体は「古い Upload」扱い)。5 章の ① Upload から。

コマンドラインでも同じことができる(パスコード・トークンはヘッダで):

```bash
curl -o ws.tar http://127.0.0.1:8765/api/workspace/export                        # PC 側で書き出し
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
  待ち時間中の ③ START は次のランをすぐ始める(「もう走っている」とは聞かれない)
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
| 音が出ない | MUSIC 行のメッセージ。`mpg123 not found` → 2.1。`mpg123 exited` → スピーカーの抜き差し、`sudo systemctl restart epaper-conductor`。曲が radxa-05 に無い(`no track loaded there yet`)→ 4 章で送り直す。音量は 2.1 の `amixer` |
| 音が絵より遅れる / 早い | `--speaker-lead-ms`(既定 50 = 測ったパイプ往復 + 50 ms 早くアンパウズ。**50 は当て推量**: クリック音源で一度測って決める)を service の ExecStart で変えて `daemon-reload` + `restart` |
| Loop が回らない | `Loop` のチェック、待ち時間が 40〜600 か。Units タブの `Corrected automatically:` の行に `Loop: …` の理由 |
| 再起動後に START が `Upload again` / `Upload first` | 機体が持っているショーが今のタイムラインと違う(送り直した・編集した)。① Upload |
| ショーを差し替えたい | 会場でも PC を `AZ-Epaper` に入れれば 4 章の手順で送れる(先に STOP) |

ログ: `journalctl -u epaper-conductor -f`(`speaker: loaded xxx.mp3, unpause latency 12 ms`、
`Loop: run 2 started` などが出る)、`journalctl -u epaper-exhibition-net`。
