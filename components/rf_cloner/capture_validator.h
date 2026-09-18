#pragma once

// Capture validation and frame extraction. Free of ESPHome headers so it can be unit-tested on
// the host; failures are returned rather than logged.

#include <cstdint>
#include <vector>

namespace esphome::rf_cloner {

enum class FrameVerdict : uint8_t {
  OK = 0,
  TOO_FEW_PULSES,
  TOO_MANY_PULSES,
  PULSE_OUT_OF_RANGE,
};

const char *frame_verdict_to_string(FrameVerdict verdict);

struct CaptureLimits {
  uint16_t min_pulses{8};
  uint16_t max_pulses{128};
  uint32_t min_pulse_us{100};
  uint32_t max_pulse_us{100000};
  uint8_t tolerance_percent{25};
};

/// Drop the trailing entry `remote_receiver` appends to every capture.
///
/// The RMT path pushes a synthetic value equal to the configured `idle:`; the software path
/// returns the terminating silence. Neither belongs in a stored waveform.
void strip_terminator(std::vector<int32_t> &timings);

/// Shape check for a single frame. Runs after strip_terminator().
FrameVerdict validate_frame(const std::vector<int32_t> &timings, const CaptureLimits &limits);

/// True when two frames match in length, polarity at every position, and per-pulse duration
/// within `tolerance_percent`.
///
/// Agreement between repeats is the noise gate: a held remote emits identical frames, noise does
/// not. RSSI would be the obvious alternative but `cc1101` does not expose it in async mode.
bool frames_agree(const std::vector<int32_t> &a, const std::vector<int32_t> &b, uint8_t tolerance_percent);

/// Sum of all pulse magnitudes, i.e. how long the frame occupies the air.
uint32_t frame_duration_us(const std::vector<int32_t> &timings);

/// One receive window split into the repeated frames it contains.
///
/// When `idle:` exceeds the target's inter-frame gap, a window holds several repeats and the gap
/// between them appears as an ordinary space, timed by the RMT peripheral at 1 us resolution.
struct SplitFrames {
  /// Complete frames, in order. The first and last may be partial if the window began or ended
  /// mid-transmission; agreement checking discards those naturally.
  std::vector<std::vector<int32_t>> frames;
  /// gaps[i] is the space, in microseconds, between frames[i] and frames[i + 1].
  /// Always one shorter than `frames`.
  std::vector<uint32_t> gaps;
};

/// Split a receive window at every space of at least `frame_gap_min_us`.
///
/// The threshold must exceed the longest space inside a frame and stay below the gap between
/// frames. A wrong value yields frames that fail to agree, so the caller falls back rather than
/// storing a bad capture.
SplitFrames split_frames(const std::vector<int32_t> &timings, uint32_t frame_gap_min_us);

/// Infer the inter-frame gap from frame arrival times, for windows that hold a single frame.
///
/// Arrivals are timestamped in loop(), so each one is quantised to a loop tick (~16 ms with WiFi
/// and web_server active) and individual deltas snap to multiples of it. Taking a median of those
/// deltas selects one quantisation mode and can be wrong by milliseconds; averaging over the span
/// bounds the error at one loop period divided by the interval count.
///
/// Returns 0 when the span is too short to average or the result is implausible.
uint32_t estimate_gap_us(uint32_t span_us, uint32_t intervals, uint32_t frame_us, uint32_t min_gap_us,
                         uint32_t max_gap_us, uint32_t min_intervals);

}  // namespace esphome::rf_cloner
