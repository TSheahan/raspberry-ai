# Workstream 2 — Agent Starting Brief

*2026-04-03 — Starting brief for the deep driver/ALSA/Python interface analysis agent*

---

## 1. Mission

Read the kernel driver source in `C:\Users\tim\PycharmProjects\seeed-voicecard\` and the Python application source in `C:\Users\tim\PycharmProjects\raspberry-ai\`, trace the full interface from `snd_pcm_release()` through ASoC to `ac108_aif_shutdown`, and produce a single structured findings document answering the five analysis questions in section 4. You are producing an analysis document to guide subsequent driver coding — you are not writing code.

The analysis question driving this workstream: **what in the `seeed-voicecard` / `ac108` kernel driver teardown path causes a Raspberry Pi reboot when the ALSA PCM file descriptor closes on process exit?**

---

## 2. Context documents — read first

Two companion roadmap documents have been prepared. Read them in order before proceeding to driver source. They contain file maps, reading guidance, and the background needed to understand the five analysis questions.

1. **Python-side roadmap** — `C:\Users\tim\PycharmProjects\raspberry-ai\mvp-modules\forked_assistant\archive\2026-04-03_python_alsa_interface_brief.md` (214 lines). Sections 5–7 (monkey-patches, shutdown sequence, crash facts) are the high-signal parts.

2. **Driver-side roadmap** — `C:\Users\tim\PycharmProjects\seeed-voicecard\2026-04-03_driver_repo_roadmap.md` (100 lines). Confirms file map and pre-identified suspicious patterns.

---

## 3. Pre-confirmed findings

These four findings are confirmed from direct source reading. Treat them as established facts — do not spend context re-verifying them. They narrow the scope of the five questions in section 4.

### 3a. Regmap cache type is REGCACHE_FLAT

`ac108.c:1406–1412` defines the regmap configuration:

```c
static const struct regmap_config ac108_regmap = {
    .reg_bits = 8,
    .val_bits = 8,
    .reg_stride = 1,
    .max_register = 0xDF,
    .cache_type = REGCACHE_FLAT,
};
```

At probe time (`ac108.c:1460`), the regmap is initialised via `devm_regmap_init_i2c(i2c, &ac108_regmap)`. Immediately after, `ac10x_fill_regcache` (defined in `ac101.c:1583–1602`) reads every register from hardware and writes the values into the FLAT cache. After init, `regmap_read` returns from cache without touching the I2C bus (non-sleeping). However, `regmap_update_bits` and `regmap_write` still perform real I2C bus writes (sleeping). No `volatile_reg` callback is registered, so no register is marked volatile.

### 3b. CONFIG_AC10X_TRIG_LOCK = 0 — machine driver spinlock compiled out

`ac10x.h:33`:

```c
#define CONFIG_AC10X_TRIG_LOCK  0
```

The `#if CONFIG_AC10X_TRIG_LOCK` guards in `seeed_voice_card_trigger` (`seeed-voicecard.c:230–238`) are compiled out. The machine driver does NOT hold a spinlock around `_set_clock` calls during TRIGGER_START.

However, `ac108_trigger` TRIGGER_START (`ac108.c:1068`) holds its own **unconditional** `spin_lock_irqsave(&ac10x->lock, flags)` while calling:
- `ac10x_read(I2S_CTRL, ...)` — cache-served via REGCACHE_FLAT, non-sleeping, safe
- `ac108_multi_update_bits(I2S_CTRL, ...)` — calls `regmap_update_bits` which does a real I2C write, sleeping

This is the sleeping-in-atomic suspect.

### 3c. No workqueue drain on stream close

`seeed_voice_card_shutdown` (`seeed-voicecard.c:128–142`) only restores channel counts and calls `clk_disable_unprepare` on CPU/codec DAI clocks. It performs zero codec I2C interaction. It does NOT call `cancel_work_sync` or `flush_work` on `work_codec_clk`.

Only `seeed_voice_card_remove` (`seeed-voicecard.c:893–902`) — the platform driver remove callback, which fires on module unload or device unbind — calls `cancel_work_sync(&priv->work_codec_clk)`.

On the normal `snd_pcm_release()` path, the workqueue is not drained before `ac108_aif_shutdown` executes. The race between `work_cb_codec_clk` and `ac108_aif_shutdown` is unguarded.

### 3d. work_cb_codec_clk retries on failure

`seeed-voicecard.c:193–209`:

```c
static void work_cb_codec_clk(struct work_struct *work)
{
    struct seeed_card_data *priv = container_of(work, struct seeed_card_data, work_codec_clk);
    int r = 0;

    if (_set_clock[SNDRV_PCM_STREAM_CAPTURE]) {
        r = r || _set_clock[SNDRV_PCM_STREAM_CAPTURE](0, NULL, 0, NULL);
    }
    if (_set_clock[SNDRV_PCM_STREAM_PLAYBACK]) {
        r = r || _set_clock[SNDRV_PCM_STREAM_PLAYBACK](0, NULL, 0, NULL);
    }

    if (r && priv->try_stop++ < TRY_STOP_MAX) {
        if (0 != schedule_work(&priv->work_codec_clk)) {}
    }
    return;
}
```

On failure, `work_cb_codec_clk` re-schedules itself up to `TRY_STOP_MAX=3` times (`seeed-voicecard.c:74`). Each retry calls `ac108_set_clock(0, ...)` which performs 5+ `regmap_update_bits` / `regmap_read` calls on the AC108. This widens the race window with `ac108_aif_shutdown`.

---

## 4. The five analysis questions

These are the questions your findings document must answer. Each is refined by the pre-confirmed findings above to narrow your investigation scope.

### Q1. Teardown call sequence

Trace the kernel path from process-exit FD close through `snd_pcm_release()` to `ac108_aif_shutdown`.

**Key sub-question:** When the stream was already stopped before FD close — specifically, PortAudio's paComplete callback return caused an earlier `snd_pcm_drop()` which fired TRIGGER_STOP — does `snd_pcm_release()` fire TRIGGER_STOP again, or does it skip directly to `shutdown`? The answer determines whether the workqueue race (Q4) is in play on the paComplete path.

You will need to reason about the ALSA PCM state machine: a stream in `SNDRV_PCM_STATE_SETUP` (post-drop) vs `SNDRV_PCM_STATE_RUNNING` at FD close time. Consult kernel ALSA documentation or your knowledge of `sound/core/pcm_native.c` for the `snd_pcm_release()` → `snd_pcm_drop()` → trigger path.

### Q2. paComplete and snd_pcm_drop

Does PortAudio call `snd_pcm_drop()` or `snd_pcm_drain()` when the callback returns `paComplete`? These have different kernel paths:
- `snd_pcm_drop()` → immediate TRIGGER_STOP, stream moves to SETUP state
- `snd_pcm_drain()` → waits for buffer to empty, then TRIGGER_STOP

The Python application uses PortAudio in callback mode. When the guarded callback returns `(None, pyaudio.paComplete)`, PortAudio's internal thread handles stream termination. Confirm from PortAudio source which ALSA call this triggers. PortAudio source may be available at `.venv/Lib/site-packages/` in the raspberry-ai repo, or reason from PortAudio's documented ALSA host API behaviour.

### Q3. Sleeping in atomic context

**Pre-confirmed:** reads are cache-served (safe). The remaining question: does `ac108_multi_update_bits` → `ac10x_update_bits` → `regmap_update_bits` perform a real I2C bus write inside the `spin_lock_irqsave` block at `ac108.c:1068–1075`?

In standard Linux regmap with an I2C bus, `regmap_update_bits` does: cache read (non-sleeping) + I2C write (sleeping). Confirm this is the case for the AC108 regmap configuration (no bus-specific override that would make writes non-sleeping). If confirmed, this is a sleeping-in-atomic kernel BUG.

Assess: does this bug fire on every TRIGGER_START, or only under specific conditions (the `if` guard at line 1071 checking BCLK_IOEN and LRCK_IOEN)? How likely is the conditional branch to be taken during normal stream start?

### Q4. Workqueue race with shutdown

**Pre-confirmed:** `seeed_voice_card_shutdown` does not drain the workqueue. The race is structurally present.

Your task: determine whether the race is reachable on the actual crash path. Specifically:

1. On the FD-close path after paComplete, does TRIGGER_STOP fire (see Q1)? If yes, does it take the `in_irq()` branch (line 250) that defers to the workqueue, or the else branch (line 254) that calls `_set_clock` synchronously?
2. If the workqueue path is taken: `work_cb_codec_clk` calls `ac108_set_clock(0)` which writes PLL_CTRL1, I2S_CTRL, LRCK registers. `ac108_aif_shutdown` writes MOD_CLK_EN and MOD_RST_CTRL. Both write to the same AC108 chip over the same I2C bus with no shared lock. What happens when these I2C transactions interleave?
3. What is the time window? `ac108_set_clock(0)` performs ~5 I2C writes; `ac108_aif_shutdown` performs 2. At I2C standard-mode speed (100 kHz), each write is ~0.1–0.3ms. The race window is small but non-zero.

### Q5. Error handling in shutdown

`ac108_aif_shutdown` (`ac108.c:1109–1125`) calls `ac108_multi_write(MOD_CLK_EN, 0x0, ...)` and `ac108_multi_write(MOD_RST_CTRL, 0x0, ...)`. The `ac108_multi_write` function (`ac108.c:447–453`) calls `ac10x_write` → `regmap_write` for each codec. Return values are silently discarded:

```c
static int ac108_multi_write(u8 reg, u8 val, struct ac10x_priv *ac10x) {
    u8 i;
    for (i = 0; i < ac10x->codec_cnt; i++) {
        ac10x_write(reg, val, ac10x->i2cmap[i]);
    }
    return 0;
}
```

If the I2C bus is wedged — from the atomic-context violation (Q3), from a concurrent write (Q4), or from AC108 chip-level I2C stretch timeout — what does the kernel I2C subsystem do? Trace the error path from `regmap_write` failure through the BCM2835 I2C driver (the Pi's I2C controller) and back up through the ASoC `shutdown` callback return chain. Does any layer translate an I2C timeout into a kernel panic, or does it hang waiting for the bus?

---

## 5. Reading order and context budget

Prioritised reading order. Total critical code is ~380 lines of driver C and ~230 lines of Python — well within a single context window.

### Priority 1 — Driver crash path (read all of these)

| Order | File (in seeed-voicecard repo) | Lines | Content |
|---|---|---|---|
| 1 | `ac108.c:1050–1135` | 85 | `ac108_trigger` + `ac108_aif_shutdown` — primary crash code |
| 2 | `ac108.c:994–1038` | 44 | `ac108_set_clock` — workqueue-deferred target |
| 3 | `seeed-voicecard.c:128–268` | 140 | `seeed_voice_card_shutdown` + `work_cb_codec_clk` + `seeed_voice_card_trigger` |
| 4 | `ac108.c:1406–1476` | 70 | regmap config, i2c_probe, regcache fill |
| 5 | `ac108.c:173–200` | 27 | `ac10x_read`, `ac10x_write`, `ac10x_update_bits` wrappers |
| 6 | `ac108.c:447–463` | 16 | `ac108_multi_write`, `ac108_multi_update_bits` |
| 7 | `ac10x.h` (full) | 126 | struct definitions, function declarations, CONFIG_AC10X_TRIG_LOCK |

### Priority 2 — Python side (for Q2 context)

| Order | File (in raspberry-ai repo) | Lines | Content |
|---|---|---|---|
| 8 | `mvp-modules/forked_assistant/src/recorder_child.py:639–835` | 196 | Monkey-patches, shutdown sequence, os._exit |
| 9 | `mvp-modules/forked_assistant/src/recorder_state.py:200–236` | 36 | `_start_stream` / `_stop_stream` with paComplete flag |

### Priority 3 — Reference (only if needed)

| File | When to read |
|---|---|
| `ac108.h` | Register address confirmation — don't read in full, grep for specific register names |
| `seeed-voicecard.c:893–902` | `seeed_voice_card_remove` — already confirmed in section 3c |
| `ac108_plugin/pcm_ac108.c` | Only if Q2 analysis raises questions about whether the ALSA plugin is in the teardown path |
| PortAudio ALSA host API source | Only if Q2 cannot be answered from PortAudio documentation / known behaviour |

---

## 6. Output specification

Produce a single document: `mvp-modules/forked_assistant/archive/2026-04-03_ws2_interface_analysis.md`

### Required structure

**For each of the five questions (Q1–Q5):**
- Confirmed answer with file:line citations from the driver source
- Confidence level: one of `confirmed-from-source`, `inferred`, or `uncertain`
- Caveats or assumptions

**Additional findings section:**
- Any crash-relevant issues discovered during reading that are NOT covered by Q1–Q5

**Recommended fix order:**
- Based on severity and dependency, which driver issues should be fixed first
- Note any fixes that are prerequisites for others

**What remains uncertain:**
- Anything that requires runtime tracing (dmesg, ftrace, kernel debug prints) rather than static source analysis
- Specific experiments that would resolve each uncertainty

---

## 7. Constraints

- Do not write or modify driver code. Analysis only.
- Do not modify any file in either repo except the output document.
- Cite file paths and line numbers for every factual claim about source code.
- Distinguish clearly between what you read in the source and what you inferred from kernel API knowledge.
- If a question cannot be fully answered from static analysis, say so explicitly and describe what runtime evidence would resolve it.
