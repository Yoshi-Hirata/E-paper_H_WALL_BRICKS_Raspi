# Reply from Meris (LIN) to MERIS_PROPOSAL_3SLOT - 2026-09-24

Text extracted from the PDF (kept locally as docs/MERIS_REPLY_3SLOT.pdf; not in git because of the slow uplink on 2026-09-24).

```
===== page 1 (2677 chars) =====
Reply to MERIS_PROPOSAL_3SLOT.pdf / 三槽轮换提案
回复
Date: 2026-09-24 To: R2 Engineering (Y. Hirata), AZ-27SS e-paper garment project From: LIN Firmware:
FW_260923 (protocol V1.4)
Overall / 总体结论
We checked your 3-slot rotation (17 → 18 → 19) against FW_260923. It works with the existing firmware; no
firmware change is needed. Each picture gets a write window of about two intervals (≈16 s), and 36 boards of
0x13 take 8–9 s, so there is roughly 7 s of margin. The sequence is safe for the controller and for the stored
data. The remaining point is the paper-quality risk of 1 s intervals, which you accept in section 1 of your
proposal.
我们对照  FW_260923 核查了你们的三槽轮换⽅案（ 17 → 18 → 19 ）：现有固件即可⽀持，⽆需改动固件。每张
画的写⼊窗⼝约为两个间隔（ ≈16 s ）， 36 块板的  0x13 需  8–9 s ，裕量约  7 s 。该流程对控制器和数据都是安全
的；剩下的只有  1 s 间隔的纸⾯质量⻛险，如你们提案第  1 节所述由贵⽅承担。
Q1. Slots 17 and 18 / 槽位  17 和  18
Yes, slots 17 and 18 can be overwritten permanently (0x13 / 0x1F / 0x1B). The factory pictures in them will be
lost, which you already accept. In the firmware all 20 slots (0–19) are the same: same storage layout (colour
data + per-segment delay table + slot configuration), same capacity, same behaviour. No slot is reserved, and
there is no difference between slots 0–18 and slot 19. The only autoplay-related note: the master board
(address 0x01) auto-plays slot 0 once at power-up (slaves do not autoplay), so send one broadcast 0x17 after
power-up (see Q4).
可以，槽位  17 、 18 可以永久覆盖（ 0x13 / 0x1F / 0x1B ），其中的出⼚图⽚会丢失，这点贵⽅已接受。固件⾥  20
个槽位（ 0–19 ）完全相同：存储结构（颜⾊数据  + 逐段延迟表  + 槽位配置）、容量、⾏为都⼀样，没有保留槽
位，槽位  0–18 与  19 之间没有差异。唯⼀与⾃动播放有关的注意点：主机板（地址  0x01 ）上电会⾃播⼀次槽位
0 （从机不⾃播），上电后⼴播⼀次  0x17 即可（⻅  Q4 ）。
Q2. Commands received during a refresh / 刷新过程中收到命令
(a) The ACK is returned immediately, also while the board is refreshing. (b) The command is executed
immediately, not queued. FW_260923 receives and processes bus commands in parallel with the refresh. For
0x13, the storage write happens as soon as the last frame for that board arrives, and the ACK is sent after the
write completes, so your measured 0.22–0.25 s per board already includes the erase/program time. (c) The
receive buffer holds 16 frames for burst absorption. Since commands execute immediately, your flow (one
frame per board, ACK awaited) will not hit this limit, and there is no "queue until the refresh ends" behaviour.
Your 2026-08-14 measurement of queuing matches the older, pre-FW_260923 firmware; on FW_260923
execution is immediate.
（ a ） ACK ⽴即返回，板卡正在刷新时同样如此。  （ b ）命令⽴即执⾏，不排队。 FW_260923 在刷新期间并⾏
接收并处理总线命令。 0x13 在该板最后⼀帧收⻬时即执⾏存储写⼊， ACK 在写⼊完成后才发出，所以你们实测
的每板  0.22–0.25 s 已包含擦写时间。  （ c ）接收缓冲为  16 帧，⽤于吸收突发。命令是即时执⾏的，你们的流
REPLY_MERIS_3SLOT_PROPOSAL.md 2026-09-24
1 / 4
===== page 2 (2934 chars) =====
程（每板⼀帧、等  ACK ）不会触及该上限，不存在 " 排队到刷新结束 " 的⾏为。你们  2026-08-14 测到的排队现象
对应  FW_260923 之前的旧版固件； FW_260923 上是即时执⾏的。
Q3. Writing a slot that is not being displayed / 写⼊未在显⽰的槽位
Safe. The board latches the whole picture into internal RAM before the refresh starts, and the refresh itself
never reads the slot storage back. Writing another slot S2 with 0x13 during the refresh of S1 cannot affect the
picture being refreshed; the timing is not restricted in any way, mid-repaint included. For the opposite case
you asked about: even if 0x13 arrives for the same slot that is currently refreshing, the image is already
latched, so the ongoing refresh is unaffected and cannot tear. The new data takes effect the next time that slot
is shown (the next 0x1D for that slot). The display interface and the storage chip also sit on two independent
buses.
安全。刷新开始前，板卡已把整幅画⾯锁存进内部  RAM ，刷新过程不会回读槽位存储。在对槽位  S1 刷新期间
⽤  0x13 写另⼀槽位  S2 ，不会影响正在刷新的画⾯，时间上没有任何限制，刷新进⾏中也可以。你们问的反向
情况：即使  0x13 写的是正在刷新的同⼀槽位，画⾯也已锁存，正在进⾏的刷新不受影响，不会撕裂；新数据在
该槽下⼀次显⽰（下⼀发对该槽的  0x1D ）时才⽣效。显⽰接⼝与存储芯⽚也在两条相互独⽴的总线上。
Q4. 0x17 stop before each write / 每次写⼊前的  0x17 停⽌
Not necessary. 0x13 is a pure storage command; it never starts or affects playback. Autoplay happens only
once, at power-up (master board, slot 0). Recommended practice: after each system power-up, send one
broadcast 0x17, then use only 0x1D per picture for the rest of the show. If 0x17 arrives while a refresh is in
progress, it stops the playback state machine (no next slot, no completion notification), but it does not abort
the physical refresh; the current refresh always runs to completion. No need to repeat it.
不需要。 0x13 是纯存储命令，不会启动播放，也不影响播放。⾃动播放只发⽣⼀次，即上电时刻（主机板、槽
位  0 ）。推荐做法：每次系统上电后⼴播⼀次  0x17 ，之后整场演出只按画⾯发  0x1D 。若  0x17 在刷新进⾏中到
达，它会停⽌播放状态机（不切下⼀槽、不发完成通知），但不会中⽌物理刷新，当前刷新总会完整跑完，⽆需
重复发送。
Q5. 0x1D while a board is still refreshing / 板卡仍在刷新时收到  0x1D
It is not dropped. The board finishes the current refresh, then loads the newly commanded slot and refreshes
it, so the board does one extra refresh, the same behaviour you observed on 2026-08-14. There is no
firmware-side minimum interval between the end of one refresh and the next 0x1D; 1 s is fine for the
controller (the paper-quality risk is on your side, as stated). Boards differ by only a few hundred ms, so a board
that is still finishing when the next 0x1D arrives simply runs the two refreshes back to back, which matches
what the show director wants for that board.
不会丢弃。板卡先完成当前刷新，随后装载新命令的槽位并刷新，即补刷⼀次，与你们  2026-08-14 观察到的
⾏为⼀致。固件对 " 上⼀刷新结束到下⼀  0x1D" 没有最⼩间隔要求；对控制器⽽⾔  1 s 没有问题（纸⾯质量⻛险
由贵⽅承担，如前所述）。板间只差⼏百毫秒，仍在收尾的板收到下⼀发  0x1D 时会背靠背完成两次刷新，这也
正符合演出导演对该板的期望。
Q6. 0x1E "switch to next slot" / 0x1E" 切换下⼀槽位 "
0x1E does not help this rotation, and please do not probe it on the bus. 0x1E is an internal coordination
command that the master uses during 0x16 loop playback to advance slaves to the next slot; it has no "pre-
REPLY_MERIS_3SLOT_PROPOSAL.md 2026-09-24
2 / 4
===== page 3 (2978 chars) =====
select the next slot" function. A board in slave mode that receives 0x1E will immediately display the slot given
in data[0] and reply ACK_SUCCESS, so a probe would trigger a real, visible display during the show. In other
modes the command is silently ignored (no reply). The rotation does not need it: the broadcast 0x1D already
carries the slot number and starts all boards together.
0x1E 对该轮换⽅案没有帮助，并且请勿在总线上探测该命令。 0x1E 是  0x16 循环播放时主机推进从机切槽的内
部协调命令，没有 " 预选下⼀槽位 " 的功能。从机模式的板收到  0x1E 会⽴即显⽰  data[0] 所指槽位并回复
ACK_SUCCESS ，探测会在演出中触发真实的可⻅显⽰；其他模式下该命令被静默忽略（⽆应答）。轮换⽅案不
需要它：⼴播  0x1D ⾃带槽位号，全板同时起刷。
Q7. Refresh duration / 刷新时⻓
Nominal ≈ 7 s from 0x1D to the finished picture. The duration is set by the display waveform and does not
depend on the number of boards (after the broadcast each board refreshes its own panel in parallel), nor in
practice on how many segments change or which colours are involved: every refresh is a full repaint, and fill
("no refresh") segments are skipped without changing the cadence. A few hundred ms of board-to-board
variation is normal (panel busy timing). The only case where the duration grows by design is a slot with a 0x1F
per-segment delay table ("sweep" picture): session ≈ max delay of the table + waveform tail. Please verify with
your real 16-colour designs as planned in your section 5.
标称约  7 s （从  0x1D 到画⾯完成）。时⻓由显⽰波形决定，与板上数量⽆关（⼴播后各板并⾏刷新⾃⼰的屏），
实际也与变化段数、涉及颜⾊基本⽆关：每次都是整幅重刷，填充（ " 不刷新 " ）段被跳过但不改变节奏。板间
⼏百毫秒的差异属正常（屏体忙信号时序）。唯⼀会按设计变⻓的情况是该槽带有  0x1F 逐段延迟表（ " 扫描 " 画
⾯）：场次  ≈ 表内最⼤延迟  + 波形尾巴。请按你们第  5 节的计划⽤真实  16 ⾊设计⽬测验证。
Q8. Flash endurance / Flash 寿命
500–1000 writes per slot is well within the endurance of the storage chip (NOR flash, typically ≥100k
erase/program cycles per sector), roughly a hundred times of margin, so wear is not a concern for a show and
its rehearsals. A RAM-only "next picture" buffer does not exist in the firmware and is not needed for this
rotation: the 3-slot scheme already solves the timing, and keeping the pictures in non-volatile storage means
they also survive a board reboot mid-show.
每槽  500–1000 次写⼊远在存储芯⽚（ NOR Flash ，典型每扇区  ≥10 万次擦写循环）的寿命范围之内，裕量约
⼀百倍，对⼀场演出及其排练没有磨损顾虑。固件没有 " 仅  RAM 的下⼀张缓冲 " ，该轮换⽅案也不需要：三槽⽅
案已解决时序问题，且画⾯保存在⾮易失存储中，演出中途板卡重启画⾯也不丢失。
Q9. Power / 电源
No controller-side current or timing constraint. In particular there is no window during a refresh in which
storage writes are forbidden (no "do not write within the first N ms" rule). The display interface and the
storage chip use separate buses, and the storage write current (mA level) is negligible against the panel
refresh current. The only timing cost of a write is the ≈0.2 s erase/program + ACK per board, already inside
your 8–9 s budget for 36 boards. Measuring the 12 V rail at the last board during a back-to-back run, as you
plan in section 5, will confirm this at system level.
控制器侧没有电流或时序⽅⾯的约束，特别是不存在刷新期间禁⽌写存储的时间窗（没有 " 开始后  N ms 内不要
写 " 的规则）。显⽰接⼝与存储芯⽚使⽤相互独⽴的总线，存储写⼊电流（ mA 级）相对屏体刷新电流可忽略。
REPLY_MERIS_3SLOT_PROPOSAL.md 2026-09-24
3 / 4
===== page 4 (1645 chars) =====
写⼊的唯⼀时间代价是每板约  0.2 s 的擦写  + ACK ，已包含在你们  36 块板  8–9 s 的预算内。按你们第  5 节的计
划在背靠背连播时测末端板  12 V 轨，即可在系统层⾯确认这⼀点。
Additional notes / 补充建议
1. After power-up, and after any reboot of the master board, make the first command a single broadcast
0x17. A slave that reboots mid-show needs no special handling: it does not autoplay, and the next
broadcast 0x1D brings it back in sync.
2. 0x1B / 0x13 / 0x1F are all persistent; re-sending after a reboot is only needed when you want to change
values. Your practice ("once per slot, or after a board reboot") is harmless, so keeping it is fine.
3. The 0x1D data body only needs 1 byte (slot number). The optional second byte (interval) has no effect
in single-shot mode.
4. Count the 0x13 ACKs per picture and re-send to boards that did not answer before firing the 0x1D, so
no board is left showing a stale picture.
5. As a mitigation for the 1 s cadence: full-white refresh pictures during rehearsals and intermissions help
the paper recover contrast. During the show itself, 1 s is safe for the controller and the data.
补充建议：
1. 上电后以及主机板每次重启后，第⼀条命令建议为⼴播  0x17 （仅⼀次）。演出中途重启的从机⽆需特殊
处理：从机不⾃播，下⼀发⼴播  0x1D 会⾃动将其拉回同步。
2. 0x1B / 0x13 / 0x1F 均持久化存储，重启后仅在需要修改数值时才须重发。你们 " 每槽⼀次、或重启后重
发 " 的做法⽆害，可以保留。
3. 0x1D 数据体只需  1 字节（槽位号），可选的第⼆个字节（间隔）在单张模式下⽆作⽤。
4. 每张画统计  0x13 的  ACK 数，对未应答的板先补发、再发  0x1D ，避免个别板显⽰旧图。
5. 作为  1 s 节奏的缓解措施：排练和幕间插⼊全⽩刷新画⾯，帮助纸⾯恢复对⽐度。正式演出期间， 1 s 间
隔对控制器和数据侧是安全的。
If anything you observe differs from what is described above, please tell me promptly; I will prepare a
countermeasure as soon as possible.
如果贵⽅实测现象与上述描述有差异，请及时告知我，我会尽快给出尽可能的处理⽅案。
Best regards, LIN
REPLY_MERIS_3SLOT_PROPOSAL.md 2026-09-24
4 / 4
```
