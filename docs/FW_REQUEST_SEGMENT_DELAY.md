# Firmware request: per-segment refresh delay / 固件需求：分段刷新延迟

Board: H_WALL_BRICKS e-paper controller, protocol V1.1 (FW_260917), DeviceType 0x03 (NUMBER_BRAND, socket N = array index N).
Requested by: R2 Engineering (Hirata), 2026-09-22. Contact for questions: y.hirata@r2-engineering.com

主控板：H_WALL_BRICKS 电子纸控制板，协议 V1.1（FW_260917），设备类型 0x03（NUMBER_BRAND，插座 N = 数组下标 N）。
需求方：R2 Engineering（Hirata），2026-09-22。联系邮箱：y.hirata@r2-engineering.com

---

> **Answered 2026-09-23 / 已于 2026-09-23 答复** — `FW/FW_260923/fw2029.09.23/REPLY_FW_REQUEST_SEGMENT_DELAY.pdf`,
> protocol V1.4 §7.4 / §7.5 (`Display Control Protocol_16Color_V1.4.pdf`, same folder).
> What was implemented differs from the proposal below, and the units follow the implementation:
> - Command **0x1F** "send per-segment pipeline", not 0x1E (0x1E is "switch to next slot" and must never be probed).
> - Value = **uint16 frames of 10 ms** (0–65535), sent as two 66-byte frames per chip: low bytes (flags 0x00), then high bytes (flags 0x02 | 0x01 last).
> - **0x25** clears a slot's table; 0x14 / 0x15 clear it with the colours. The table is kept in flash, paired with the slot.
> - Equal delays start in the same frame (truly simultaneous is allowed); keep the largest delay under ~30 s.
> - Firmware before V1.4 answers ACK_INVALID_CMD to 0x1F.
> 
> 実装は提案と異なる。機体は実装に従う: **0x1F**(0x1E は「次スロットへ切替」なので送らない)、
> 値は **10 ms のフレーム数(uint16)**、下位バイト・上位バイトの 2 フレームで送る、**0x25** で表を消す。

## 1. Background / 背景

**EN** — Each board drives up to 60 e-paper scales sewn into a garment. When a board receives the broadcast "show single" command (0x1D), it refreshes its segments one after another in socket order P01 → P60, about 100 ms apart, so a full refresh takes about 7 s. The order is fixed in the firmware. On the garment the 60 sockets of one board are wired to scattered positions, so the visible change runs in wiring order, which has no meaning to the audience.

For the fashion show we want the change to sweep across the garment in a chosen direction — top to bottom, bottom to top, left to right, right to left, or outward from the centre — with the same ~100 ms step between rows that the P01 → P60 order has today. The host (PC + Radxa unit) knows where every socket sits on the garment and can compute, for every socket, how long it should wait before it starts. **What is missing is a way to tell the board that per-segment start delay.**

**中文** — 每块主控板驱动缝在服装上的最多 60 片电子纸鳞片。主控板收到广播"显示单张"命令（0x1D）后，按插座顺序 P01 → P60 逐个刷新，每片间隔约 100 ms，整块板刷新约需 7 s。该顺序在固件中固定。在服装上，同一块板的 60 个插座连接到分散的位置，因此肉眼看到的变化按接线顺序进行，对观众没有意义。

时装秀需要让变化按指定方向在整件服装上扫过——从上到下、从下到上、从左到右、从右到左、或从中心向外——行与行之间保持与现在 P01 → P60 相同的约 100 ms 间隔。主机（PC + Radxa）知道每个插座在服装上的位置，可以为每个插座算出应等待多久再开始刷新。**目前缺少的是把"每个分段的起始延迟"告知主控板的手段。**

---

## 2. Requirement / 需求

**EN**

1. The host can store, per slot, a **delay table** of 64 bytes: for every socket the delay from the show trigger to the start of that segment's refresh.
2. On "show single" (0x1D) the board starts each segment at `T0 + delay[socket]`, where `T0` is the reception of the 0x1D frame. Segments whose colour is 0xFF (no refresh) are skipped as today.
3. A slot without a delay table behaves **exactly as today** (P01 → P60, ~100 ms apart). Existing commands and data formats do not change.
4. Firmware without this feature must answer the new command with `ACK_INVALID_CMD (0x83)`, so the host can detect support per board.

**中文**

1. 主机可以为每个槽位保存一张 64 字节的**延迟表**：每个插座从显示触发到该分段开始刷新的延迟。
2. 收到"显示单张"（0x1D）后，主控板在 `T0 + delay[socket]` 时刻启动各分段，其中 `T0` 为收到 0x1D 帧的时刻。颜色为 0xFF（不刷新）的分段照旧跳过。
3. 没有延迟表的槽位**行为与现在完全一致**（P01 → P60，约 100 ms 间隔）。现有命令和数据格式不变。
4. 不支持此功能的固件，对新命令应回复 `ACK_INVALID_CMD (0x83)`，以便主机逐板判断是否支持。

---

## 3. Proposed protocol / 建议的协议

### 3.1 New command 0x1E "save segment delays" / 新命令 0x1E "保存分段延迟"

**EN** — Same frame envelope as 0x13 (DataLen 66, so it passes the RS485 relay like 0x13 does). Any other free command code is acceptable if 0x1E is taken.

**中文** — 帧结构与 0x13 相同（DataLen 66，可像 0x13 一样经 RS485 中继）。若 0x1E 已被占用，可换用其他空闲命令码。

| Offset / 偏移 | Length / 长度 | Content / 内容 |
|---|---|---|
| 0 | 1 B | Slot number 0–19 / 槽位号 0–19 |
| 1 | 1 B | Flags, reserved, send 0x00 / 标志位，保留，填 0x00 |
| 2–65 | 64 B | Delay table: index N = socket N (same indexing as the colour array of DeviceType 0x03). Index 0 and 63 are ignored. / 延迟表：下标 N = 插座 N（与设备类型 0x03 的颜色数组同样的下标）。下标 0 和 63 忽略 |

Byte value / 字节含义:

| Value / 值 | Meaning / 含义 |
|---|---|
| 0x00–0xFE | Delay in units of **100 ms** (0 = start at T0, 0xFE = 25.4 s) / 延迟，单位 **100 ms**（0 = 在 T0 启动，0xFE = 25.4 s）|
| 0xFF | No delay given for this socket: use today's default timing for it / 该插座未指定延迟：沿用现有默认时序 |

Reply / 应答: `ACK_SUCCESS (0x80)` when stored; `ACK_PARAM_ERROR (0x85)` for a slot out of range; `ACK_INVALID_CMD (0x83)` on firmware without the feature.

### 3.2 Storage / 存储

**EN** — The delay table belongs to the slot, like its colour data: it persists across power cycles, is overwritten by a new 0x1E, and is removed by "delete slot" (0x14) and "clear all" (0x15). Sending colour data (0x13) to the slot **does not** remove the delay table, so the host can send the table once and then only colours for each cue. If flash wear is a concern, keeping the table in RAM only (lost at power-off, re-sent by the host) is also acceptable — please tell us which.

**中文** — 延迟表属于槽位，与颜色数据一样：掉电保留，新的 0x1E 覆盖旧表，"删除槽位"（0x14）和"全部清除"（0x15）一并删除。向该槽位下发颜色数据（0x13）**不**删除延迟表，这样主机可以只发一次表，之后每个画面只发颜色。若担心 flash 寿命，仅保存在 RAM（掉电丢失、由主机重发）也可以接受——请告知采用哪种。

### 3.3 Timing on 0x1D / 0x1D 时的时序

**EN**

- `T0` = reception of the 0x1D frame (broadcast or unicast).
- Segment N starts its refresh no earlier than `T0 + delay[N] × 100 ms`.
- If several segments are due at the same time and the hardware can only drive a limited number at once, the board drives them in socket order at its minimum spacing (the same ~100 ms as today). The delay is therefore a "not before" time; the host will spread delays so that this rarely happens.
- The refresh waveform of each segment is unchanged. The slot's total time = max delay + one segment refresh.
- Any 0x1B display mode / direction setting is ignored for a slot that has a delay table.

**中文**

- `T0` = 收到 0x1D 帧（广播或单播）的时刻。
- 分段 N 不早于 `T0 + delay[N] × 100 ms` 开始刷新。
- 若多个分段同时到期而硬件只能同时驱动有限数量，主控板按插座顺序、以其最小间隔（与现在相同的约 100 ms）依次驱动。因此延迟是"不早于"的时刻；主机会尽量错开延迟，使这种情况很少发生。
- 各分段的刷新波形不变。槽位总时长 = 最大延迟 + 一个分段的刷新时间。
- 对带有延迟表的槽位，0x1B 的显示模式 / 方向设置忽略。

### 3.4 Sequence used by the host / 主机的使用流程

```
per board / 每块板:   0x17 stop  →  [0x1E delay table, when the sequence changed / 顺序变化时]  →  0x13 colours (slot 19)
broadcast / 广播:      0x1D show single (slot 19)   — once, at the show's instant / 一次，在演出时刻
```

The host waits for `ACK_SUCCESS` after 0x1E and 0x13 as it does today. / 主机与现在一样，在 0x1E 和 0x13 之后等待 `ACK_SUCCESS`。

---

## 4. Example / 示例

**EN** — A board whose sockets 1–6 sit on garment rows 3, 1, 2, 2, 5, 4 (row 1 at the top); the show should run top to bottom, 100 ms per row:

**中文** — 某板插座 1–6 位于服装的第 3、1、2、2、5、4 行（第 1 行在最上面）；演出要求从上到下、每行 100 ms：

```
delay[1..6] = 0x02, 0x00, 0x01, 0x01, 0x04, 0x03     (units of 100 ms / 单位 100 ms)
delay[7..62] = 0xFF                                    (unused sockets / 未使用的插座)
```

Result / 效果: socket 2 starts at T0, sockets 3 and 4 at T0 + 100 ms, socket 1 at T0 + 200 ms, socket 6 at T0 + 300 ms, socket 5 at T0 + 400 ms. Every board on the garment receives its own table, so the whole garment changes row by row at the same instants.
每块板收到各自的表，因此整件服装在相同的时刻逐行变化。

---

## 5. Acceptance test / 验收测试

**EN**

1. Old firmware behaviour unchanged: slot without table → P01 → P60 as today.
2. 0x1E then 0x1D on one board: with a table of `delay[N] = N` (N = 1..60), a video shows the segments starting in socket order about 100 ms apart (same as today); with `delay[N] = 60 − N`, in reverse order; with all `0x00`, as close to simultaneous as the hardware allows, without brown-out or reset.
3. Table survives power-off (or is documented as RAM-only).
4. 0x14 on the slot removes the table; the next 0x1D runs in default order.
5. Delay table sent through the RS485 relay (board ID 2 behind board ID 1 on USB) is acknowledged and applied like 0x13.

**中文**

1. 旧行为不变：无表的槽位 → 与现在一样 P01 → P60。
2. 对一块板先 0x1E 后 0x1D：表为 `delay[N] = N`（N = 1..60）时，录像显示各分段按插座顺序、约 100 ms 间隔启动（与现在相同）；`delay[N] = 60 − N` 时顺序相反；全部 `0x00` 时在硬件允许范围内尽量同时启动，且无掉电复位。
3. 表在掉电后保留（或明确说明仅在 RAM 中）。
4. 对该槽位执行 0x14 后表被删除，下一次 0x1D 按默认顺序运行。
5. 经 RS485 中继（USB 上的 1 号板后面的 2 号板）发送的延迟表，能像 0x13 一样得到应答并生效。

---

## 6. Questions for the manufacturer / 请厂家确认

1. Is 0x1E free? If not, which code should we use? / 0x1E 是否空闲？若否，应使用哪个命令码？
2. Minimum spacing between two segments that start "at the same time" (power limit)? / 两个"同时"启动的分段之间的最小间隔（功耗限制）是多少？
3. Is a delay unit of 100 ms and a maximum of 25.4 s acceptable, or would 50 ms units (max 12.7 s) be easy as well? / 100 ms 单位、最大 25.4 s 是否合适？50 ms 单位（最大 12.7 s）是否也容易实现？
4. Flash or RAM storage for the table (see 3.2)? / 表存 flash 还是 RAM（见 3.2）？
5. Expected delivery date. We can test on real garments (boards ID 1–36 on one RS485 bus) within a day of receiving a build. / 预计交付时间。收到固件后一天内可在真实服装（一条 RS485 总线上的 1–36 号板）上测试。
