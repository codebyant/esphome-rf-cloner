#include "capture_validator.h"

#include <cstdlib>

namespace esphome::rf_cloner {

const char *frame_verdict_to_string(FrameVerdict verdict) {
  switch (verdict) {
    case FrameVerdict::OK:
      return "ok";
    case FrameVerdict::TOO_FEW_PULSES:
      return "too_few_pulses";
    case FrameVerdict::TOO_MANY_PULSES:
      return "too_many_pulses";
    case FrameVerdict::PULSE_OUT_OF_RANGE:
      return "pulse_out_of_range";
  }
  return "unknown";
}

void strip_terminator(std::vector<int32_t> &timings) {
  if (!timings.empty()) {
    timings.pop_back();
  }
}

FrameVerdict validate_frame(const std::vector<int32_t> &timings, const CaptureLimits &limits) {
  if (timings.size() < limits.min_pulses) {
    return FrameVerdict::TOO_FEW_PULSES;
  }
  if (timings.size() > limits.max_pulses) {
    return FrameVerdict::TOO_MANY_PULSES;
  }
  for (int32_t timing : timings) {
    if (timing == 0) {
      return FrameVerdict::PULSE_OUT_OF_RANGE;
    }
    const uint32_t magnitude = static_cast<uint32_t>(timing < 0 ? -static_cast<int64_t>(timing) : timing);
    if (magnitude < limits.min_pulse_us || magnitude > limits.max_pulse_us) {
      return FrameVerdict::PULSE_OUT_OF_RANGE;
    }
  }
  return FrameVerdict::OK;
}

bool frames_agree(const std::vector<int32_t> &a, const std::vector<int32_t> &b, uint8_t tolerance_percent) {
  if (a.size() != b.size() || a.empty()) {
    return false;
  }
  for (size_t i = 0; i < a.size(); i++) {
    // A mark can never substitute for a space.
    if ((a[i] < 0) != (b[i] < 0)) {
      return false;
    }
    const uint32_t reference = static_cast<uint32_t>(a[i] < 0 ? -static_cast<int64_t>(a[i]) : a[i]);
    const uint32_t candidate = static_cast<uint32_t>(b[i] < 0 ? -static_cast<int64_t>(b[i]) : b[i]);
    // Matches remote_base's percentage tolerance bounds.
    const uint32_t lower = static_cast<uint32_t>((100u - tolerance_percent) * static_cast<uint64_t>(reference) / 100u);
    const uint32_t upper = static_cast<uint32_t>((100u + tolerance_percent) * static_cast<uint64_t>(reference) / 100u);
    if (candidate < lower || candidate > upper) {
      return false;
    }
  }
  return true;
}

uint32_t frame_duration_us(const std::vector<int32_t> &timings) {
  uint64_t total = 0;
  for (int32_t timing : timings) {
    total += static_cast<uint64_t>(timing < 0 ? -static_cast<int64_t>(timing) : timing);
  }
  return total > UINT32_MAX ? UINT32_MAX : static_cast<uint32_t>(total);
}

SplitFrames split_frames(const std::vector<int32_t> &timings, uint32_t frame_gap_min_us) {
  SplitFrames result;
  std::vector<int32_t> current;
  for (int32_t timing : timings) {
    const bool is_space = timing < 0;
    const uint32_t magnitude = static_cast<uint32_t>(is_space ? -static_cast<int64_t>(timing) : timing);
    if (is_space && magnitude >= frame_gap_min_us) {
      // A window can open on a gap, so only close a frame that holds something.
      if (!current.empty()) {
        result.frames.push_back(current);
        current.clear();
        result.gaps.push_back(magnitude);
      }
      continue;
    }
    current.push_back(timing);
  }
  if (!current.empty()) {
    result.frames.push_back(current);
  }
  // A gap with no following frame separates nothing.
  while (result.gaps.size() >= result.frames.size() && !result.gaps.empty()) {
    result.gaps.pop_back();
  }
  return result;
}

uint32_t estimate_gap_us(uint32_t span_us, uint32_t intervals, uint32_t frame_us, uint32_t min_gap_us,
                         uint32_t max_gap_us, uint32_t min_intervals) {
  if (intervals < min_intervals || intervals == 0) {
    return 0;
  }
  const uint32_t period_us = span_us / intervals;
  if (period_us <= frame_us) {
    return 0;
  }
  const uint32_t gap = period_us - frame_us;
  if (gap < min_gap_us || gap > max_gap_us) {
    return 0;
  }
  return gap;
}

}  // namespace esphome::rf_cloner
