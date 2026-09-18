// Host-side unit tests for the capture validator and the command store. No ESPHome headers, so
// this builds with a plain C++ compiler:
//
//   sh tests/run.sh

#include "capture_validator.h"
#include "command_store.h"

#include <cstdio>
#include <cstdlib>
#include <cinttypes>
#include <map>
#include <string>
#include <vector>

using namespace esphome::rf_cloner;

namespace {

int g_failures = 0;
int g_checks = 0;

void check(bool condition, const char *what) {
  g_checks++;
  if (!condition) {
    g_failures++;
    std::printf("  FAIL: %s\n", what);
  }
}

#define CHECK(cond) check((cond), #cond)

/// In-memory stand-in for ESPHome preferences, with the same exact-length load semantics.
class FakeBackend : public StorageBackend {
 public:
  bool save(uint32_t key, const uint8_t *data, size_t len) override {
    if (this->fail_next_save) {
      this->fail_next_save = false;
      return false;
    }
    this->records[key] = std::vector<uint8_t>(data, data + len);
    this->writes++;
    return true;
  }

  bool load(uint32_t key, uint8_t *data, size_t len) override {
    auto it = this->records.find(key);
    if (it == this->records.end() || it->second.size() != len) {
      return false;  // same rule ESP32PreferenceBackend::load enforces
    }
    std::copy(it->second.begin(), it->second.end(), data);
    return true;
  }

  bool sync() override { return true; }

  std::map<uint32_t, std::vector<uint8_t>> records;
  unsigned writes{0};
  bool fail_next_save{false};
};

/// A real 65-pulse 433 MHz OOK capture, used as the fixture throughout these tests.
const std::vector<int32_t> &reference_frame() {
  static const std::vector<int32_t> frame = {
      280,  -710, 257,  -714, 249,  -729, 244, -731, 286,  -684, 278,  -724, 247,  -730, 724, -248, 743,
      -259, 713,  -231, 743,  -247, 734,  -248, 723, -234, 269,  -717, 274,  -702, 753,  -231, 754, -222,
      286,  -700, 281,  -680, 274,  -704, 753, -229, 267,  -718, 250,  -732, 269,  -706, 268,  -703, 759,
      -247, 731,  -226, 745,  -225, 756,  -250, 258, -703, 755,  -220, 267,  -711, 278};
  return frame;
}

std::vector<int32_t> jitter(const std::vector<int32_t> &source, int percent) {
  std::vector<int32_t> out;
  out.reserve(source.size());
  for (size_t i = 0; i < source.size(); i++) {
    const int sign = source[i] < 0 ? -1 : 1;
    const int64_t magnitude = source[i] < 0 ? -source[i] : source[i];
    // Alternate the direction of the error so it does not simply scale the frame.
    const int64_t delta = magnitude * percent / 100 * ((i % 2 == 0) ? 1 : -1);
    out.push_back(static_cast<int32_t>(sign * (magnitude + delta)));
  }
  return out;
}

// ---------------------------------------------------------------------------

void test_strip_terminator() {
  std::printf("strip_terminator\n");
  // remote_receiver appends a terminator to every capture; it must not reach the store.
  std::vector<int32_t> frame = reference_frame();
  frame.push_back(-4000);  // what the RMT path appends for idle: 4ms
  strip_terminator(frame);
  CHECK(frame.size() == 65);
  CHECK(frame == reference_frame());

  std::vector<int32_t> empty;
  strip_terminator(empty);
  CHECK(empty.empty());

  std::vector<int32_t> single{123};
  strip_terminator(single);
  CHECK(single.empty());
}

void test_validate_frame() {
  std::printf("validate_frame\n");
  CaptureLimits limits;  // defaults
  CHECK(validate_frame(reference_frame(), limits) == FrameVerdict::OK);

  CHECK(validate_frame({100, -100}, limits) == FrameVerdict::TOO_FEW_PULSES);

  CaptureLimits tight = limits;
  tight.max_pulses = 32;
  CHECK(validate_frame(reference_frame(), tight) == FrameVerdict::TOO_MANY_PULSES);

  std::vector<int32_t> with_runt = reference_frame();
  with_runt[4] = 12;  // below min_pulse_us
  CHECK(validate_frame(with_runt, limits) == FrameVerdict::PULSE_OUT_OF_RANGE);

  std::vector<int32_t> with_giant = reference_frame();
  with_giant[4] = 5'000'000;
  CHECK(validate_frame(with_giant, limits) == FrameVerdict::PULSE_OUT_OF_RANGE);

  std::vector<int32_t> with_zero = reference_frame();
  with_zero[4] = 0;
  CHECK(validate_frame(with_zero, limits) == FrameVerdict::PULSE_OUT_OF_RANGE);
}

void test_frames_agree() {
  std::printf("frames_agree\n");
  CHECK(frames_agree(reference_frame(), reference_frame(), 25));
  // Measured frame-to-frame variation sits well inside 25%.
  CHECK(frames_agree(reference_frame(), jitter(reference_frame(), 10), 25));
  CHECK(!frames_agree(reference_frame(), jitter(reference_frame(), 40), 25));

  // A different pulse count is never an agreement.
  std::vector<int32_t> shorter = reference_frame();
  shorter.pop_back();
  CHECK(!frames_agree(reference_frame(), shorter, 25));

  // A mark cannot substitute for a space of the same length.
  std::vector<int32_t> flipped = reference_frame();
  flipped[1] = -flipped[1];
  CHECK(!frames_agree(reference_frame(), flipped, 25));

  CHECK(!frames_agree({}, {}, 25));
}

void test_helpers() {
  std::printf("frame_duration_us\n");
  // 65 pulses averaging roughly 480 us.
  const uint32_t duration = frame_duration_us(reference_frame());
  CHECK(duration > 25000 && duration < 40000);
}

std::vector<int32_t> build_window(int frame_count, uint32_t gap_us, bool leading_partial = false,
                                  bool trailing_partial = false) {
  // What remote_receiver returns when `idle:` sits above the target's inter-frame gap: several
  // repeats separated by the gap as an ordinary space.
  std::vector<int32_t> window;
  for (int f = 0; f < frame_count; f++) {
    const std::vector<int32_t> &src = reference_frame();
    size_t begin = (f == 0 && leading_partial) ? src.size() / 2 : 0;
    size_t end = (f == frame_count - 1 && trailing_partial) ? src.size() / 2 : src.size();
    for (size_t i = begin; i < end; i++) {
      window.push_back(src[i]);
    }
    if (f + 1 < frame_count) {
      window.push_back(-static_cast<int32_t>(gap_us));
    }
  }
  return window;
}

void test_split_frames() {
  std::printf("split_frames (physical gap, read not inferred)\n");
  constexpr uint32_t THRESHOLD = 3000;

  // Three clean repeats separated by a known gap.
  SplitFrames split = split_frames(build_window(3, 8658), THRESHOLD);
  CHECK(split.frames.size() == 3);
  CHECK(split.gaps.size() == 2);
  CHECK(split.gaps[0] == 8658);
  CHECK(split.gaps[1] == 8658);
  CHECK(split.frames[0] == reference_frame());
  CHECK(split.frames[1] == reference_frame());

  // Split frames must still pass validation and agree with their neighbours.
  CaptureLimits limits;
  limits.max_pulses = 128;
  CHECK(validate_frame(split.frames[0], limits) == FrameVerdict::OK);
  CHECK(frames_agree(split.frames[0], split.frames[1], 25));

  // A window opening and closing mid-transmission: partial frames survive the split but fail to
  // agree, so the caller ignores them.
  split = split_frames(build_window(4, 8658, true, true), THRESHOLD);
  CHECK(split.frames.size() == 4);
  CHECK(split.gaps.size() == 3);
  CHECK(!frames_agree(split.frames[0], split.frames[1], 25));  // leading partial
  CHECK(frames_agree(split.frames[1], split.frames[2], 25));   // the good pair in the middle
  CHECK(split.gaps[1] == 8658);
  CHECK(!frames_agree(split.frames[2], split.frames[3], 25));  // trailing partial

  // A single-frame window yields no gap; the caller falls back to the estimator.
  split = split_frames(reference_frame(), THRESHOLD);
  CHECK(split.frames.size() == 1);
  CHECK(split.gaps.empty());

  // The threshold must exceed the longest space inside a frame; the fixture's longest is ~732 us.
  // A threshold below that shreds the frame, and nothing agrees.
  split = split_frames(build_window(2, 8658), 500);
  CHECK(split.frames.size() > 2);
  CHECK(validate_frame(split.frames[0], limits) != FrameVerdict::OK);

  // Gaps are never reported without a frame on both sides.
  std::vector<int32_t> trailing = reference_frame();
  trailing.push_back(-8658);
  split = split_frames(trailing, THRESHOLD);
  CHECK(split.frames.size() == 1);
  CHECK(split.gaps.empty());

  CHECK(split_frames({}, THRESHOLD).frames.empty());

  // Measured gaps vary slightly between frames.
  std::vector<int32_t> jittered = build_window(2, 8600);
  for (int32_t v : build_window(2, 8710)) {
    jittered.push_back(v);
  }
  split = split_frames(jittered, THRESHOLD);
  CHECK(split.gaps.size() >= 2);
  for (uint32_t g : split.gaps) {
    CHECK(g >= 8500 && g <= 8800);
  }
}

void test_estimate_gap() {
  std::printf("estimate_gap_us\n");
  // Measurements from an ESP32 receiving a real 433 MHz remote. The frame occupies 31583 us
  // and the inter-frame gap, later read directly off the waveform, is ~9430 us.
  constexpr uint32_t MIN_GAP = 1000;
  constexpr uint32_t MAX_GAP = 200000;
  constexpr uint32_t MIN_INTERVALS = 3;

  // 6 intervals; arrivals quantised bimodally at ~33 ms and ~50 ms by the loop period.
  const uint32_t deltas[] = {48779, 35059, 32928, 51096, 34039, 34000};
  uint32_t span = 0;
  for (uint32_t d : deltas) {
    span += d;
  }
  const uint32_t gap_6 = estimate_gap_us(span, 6, 31572, MIN_GAP, MAX_GAP, MIN_INTERVALS);
  // A median of these deltas returns 2476 us, which the target device rejects.
  CHECK(gap_6 > 7000 && gap_6 < 8500);

  // 21 intervals, period 41373 us.
  const uint32_t gap_21 = estimate_gap_us(41373u * 21u, 21, 31563, MIN_GAP, MAX_GAP, MIN_INTERVALS);
  CHECK(gap_21 > 9000 && gap_21 < 10500);

  // More intervals must not make the estimate worse.
  constexpr int64_t MEASURED_GAP = 9430;  // read off the waveform on the same remote
  const int64_t err_6 = static_cast<int64_t>(gap_6) - MEASURED_GAP;
  const int64_t err_21 = static_cast<int64_t>(gap_21) - MEASURED_GAP;
  CHECK(std::llabs(err_21) <= std::llabs(err_6) + 500);

  // Too few intervals to average over.
  CHECK(estimate_gap_us(80000, 2, 31583, MIN_GAP, MAX_GAP, MIN_INTERVALS) == 0);
  CHECK(estimate_gap_us(0, 0, 31583, MIN_GAP, MAX_GAP, MIN_INTERVALS) == 0);

  // A period shorter than the frame itself is impossible.
  CHECK(estimate_gap_us(30000u * 5u, 5, 31583, MIN_GAP, MAX_GAP, MIN_INTERVALS) == 0);

  // Outside the plausibility window, so the caller falls back to its default.
  CHECK(estimate_gap_us((31583u + 500u) * 5u, 5, 31583, MIN_GAP, MAX_GAP, MIN_INTERVALS) == 0);
  CHECK(estimate_gap_us((31583u + 300000u) * 5u, 5, 31583, MIN_GAP, MAX_GAP, MIN_INTERVALS) == 0);
}

void test_name_validation() {
  std::printf("validate_name\n");
  CHECK(CommandStore::validate_name("fan_speed_1") == StoreResult::OK);
  CHECK(CommandStore::validate_name("Fan.Speed-1") == StoreResult::OK);
  CHECK(CommandStore::validate_name("") == StoreResult::NAME_EMPTY);
  CHECK(CommandStore::validate_name(std::string(NAME_MAX_CHARS, 'a')) == StoreResult::OK);
  CHECK(CommandStore::validate_name(std::string(NAME_MAX_CHARS + 1, 'a')) == StoreResult::NAME_TOO_LONG);
  CHECK(CommandStore::validate_name("fan speed") == StoreResult::NAME_INVALID);
  CHECK(CommandStore::validate_name("fan/speed") == StoreResult::NAME_INVALID);
}

Command make_command(const std::string &name, size_t pulses = 65) {
  Command command;
  command.name = name;
  command.timings.assign(reference_frame().begin(), reference_frame().begin() + pulses);
  command.gap_us = 8658;
  command.repeat_times = 20;
  command.frequency_hz = 433920000;
  return command;
}

void test_store_roundtrip() {
  std::printf("store: round-trip across a simulated reboot\n");
  FakeBackend backend;

  CommandStore store;
  store.configure(&backend, 16, 256, 12288, 0);
  store.begin();
  CHECK(store.count() == 0);

  CHECK(store.put(make_command("vel_1")) == StoreResult::OK);
  CHECK(store.put(make_command("vel_2", 40)) == StoreResult::OK);
  CHECK(store.count() == 2);

  // Reboot: fresh store object, same flash contents.
  CommandStore reloaded;
  reloaded.configure(&backend, 16, 256, 12288, 0);
  const LoadReport report = reloaded.begin();
  CHECK(report.had_stored_data);
  CHECK(!report.header_invalid);
  CHECK(!report.version_mismatch);
  CHECK(report.loaded == 2);
  CHECK(report.dropped_corrupt == 0);
  CHECK(reloaded.count() == 2);

  Command out;
  CHECK(reloaded.get("vel_1", out));
  CHECK(out.timings == reference_frame());
  CHECK(out.gap_us == 8658);
  CHECK(out.repeat_times == 20);
  CHECK(out.frequency_hz == 433920000);
  CHECK(reloaded.get("vel_2", out));
  CHECK(out.timings.size() == 40);
  CHECK(!reloaded.get("nope", out));
}

void test_store_overwrite_and_delete() {
  std::printf("store: overwrite, delete, slot reuse\n");
  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 4, 256, 12288, 0);
  store.begin();

  CHECK(store.put(make_command("a")) == StoreResult::OK);
  CHECK(store.count() == 1);

  // Relearning under an existing name overwrites in place - this is what replaces a rename API.
  Command shorter = make_command("a", 20);
  CHECK(store.put(shorter) == StoreResult::OK);
  CHECK(store.count() == 1);
  Command out;
  CHECK(store.get("a", out));
  CHECK(out.timings.size() == 20);

  CHECK(store.remove("a") == StoreResult::OK);
  CHECK(store.count() == 0);
  CHECK(store.remove("a") == StoreResult::NOT_FOUND);

  // The freed slot is reusable, and the stale payload record does not resurrect anything.
  CHECK(store.put(make_command("b")) == StoreResult::OK);
  CHECK(store.get("b", out));
  CHECK(out.timings.size() == 65);

  CommandStore reloaded;
  reloaded.configure(&backend, 4, 256, 12288, 0);
  CHECK(reloaded.begin().loaded == 1);
  CHECK(!reloaded.has("a"));
  CHECK(reloaded.has("b"));
}

void test_store_full_and_budget() {
  std::printf("store: capacity and budget limits\n");
  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 2, 256, 12288, 0);
  store.begin();

  CHECK(store.put(make_command("one")) == StoreResult::OK);
  CHECK(store.put(make_command("two")) == StoreResult::OK);
  CHECK(store.put(make_command("three")) == StoreResult::STORE_FULL);
  // Overwriting an existing name still works when every slot is taken.
  CHECK(store.put(make_command("two", 30)) == StoreResult::OK);

  // Budget: index alone is 10 + 2*40 + 2 = 92 bytes, so 200 leaves room for 27 pulses.
  FakeBackend tight_backend;
  CommandStore tight;
  tight.configure(&tight_backend, 2, 256, 200, 0);
  tight.begin();
  CHECK(tight.put(make_command("big")) == StoreResult::BUDGET_EXCEEDED);
  CHECK(tight.count() == 0);
  CHECK(tight.put(make_command("small", 20)) == StoreResult::OK);

  // max_pulses is enforced independently of the byte budget.
  FakeBackend capped_backend;
  CommandStore capped;
  capped.configure(&capped_backend, 4, 32, 12288, 0);
  capped.begin();
  CHECK(capped.put(make_command("too_long")) == StoreResult::TOO_MANY_PULSES);
}

void test_store_rejects_bad_names() {
  std::printf("store: name rejection\n");
  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 4, 256, 12288, 0);
  store.begin();
  CHECK(store.put(make_command("")) == StoreResult::NAME_EMPTY);
  CHECK(store.put(make_command("has space")) == StoreResult::NAME_INVALID);
  CHECK(store.count() == 0);
}

void test_corruption_is_survivable() {
  std::printf("store: corruption fails safely\n");
  FakeBackend backend;
  {
    CommandStore store;
    store.configure(&backend, 4, 256, 12288, 0);
    store.begin();
    CHECK(store.put(make_command("good")) == StoreResult::OK);
    CHECK(store.put(make_command("bad")) == StoreResult::OK);
  }

  // Corrupt one payload record. Its slot must drop out; the other must survive.
  for (auto &entry : backend.records) {
    if (entry.second.size() == 65 * sizeof(int32_t)) {
      entry.second[0] ^= 0xFF;
      break;
    }
  }
  CommandStore store;
  store.configure(&backend, 4, 256, 12288, 0);
  const LoadReport report = store.begin();
  CHECK(report.dropped_corrupt == 1);
  CHECK(report.loaded == 1);
  CHECK(store.count() == 1);
}

void test_header_corruption_and_version() {
  std::printf("store: header CRC and version mismatch\n");
  FakeBackend backend;
  {
    CommandStore store;
    store.configure(&backend, 4, 256, 12288, 0);
    store.begin();
    CHECK(store.put(make_command("x")) == StoreResult::OK);
  }
  // Find the 10-byte header record and damage it.
  std::vector<uint8_t> *header = nullptr;
  for (auto &entry : backend.records) {
    if (entry.second.size() == 10) {
      header = &entry.second;
    }
  }
  CHECK(header != nullptr);
  if (header != nullptr) {
    std::vector<uint8_t> saved = *header;

    (*header)[0] ^= 0xFF;  // breaks both magic and CRC
    {
      CommandStore store;
      store.configure(&backend, 4, 256, 12288, 0);
      const LoadReport report = store.begin();
      CHECK(report.had_stored_data);
      CHECK(report.header_invalid);
      CHECK(store.count() == 0);
    }

    // A future format version must be refused rather than misread.
    *header = saved;
    (*header)[2] = STORE_FORMAT_VERSION + 1;
    // Recompute the header CRC so the version check, not the CRC check, is what trips.
    {
      uint16_t crc = 0xFFFF;
      for (size_t i = 0; i < 8; i++) {
        crc ^= static_cast<uint16_t>((*header)[i]) << 8;
        for (int bit = 0; bit < 8; bit++) {
          crc = (crc & 0x8000u) != 0 ? static_cast<uint16_t>((crc << 1) ^ 0x1021u) : static_cast<uint16_t>(crc << 1);
        }
      }
      (*header)[8] = static_cast<uint8_t>(crc & 0xFF);
      (*header)[9] = static_cast<uint8_t>(crc >> 8);
    }
    CommandStore store;
    store.configure(&backend, 4, 256, 12288, 0);
    const LoadReport report = store.begin();
    CHECK(report.version_mismatch);
    CHECK(!report.header_invalid);
    CHECK(store.count() == 0);
    // Flash is left intact so a downgrade can still read it.
    CHECK(backend.records.size() > 1);
  }
}

void test_failed_write_does_not_corrupt() {
  std::printf("store: a failed write leaves the previous state intact\n");
  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 4, 256, 12288, 0);
  store.begin();
  CHECK(store.put(make_command("keep")) == StoreResult::OK);

  backend.fail_next_save = true;
  CHECK(store.put(make_command("new_one")) == StoreResult::STORAGE_ERROR);
  CHECK(store.count() == 1);
  CHECK(store.has("keep"));
  CHECK(!store.has("new_one"));

  // And the earlier command still reloads.
  CommandStore reloaded;
  reloaded.configure(&backend, 4, 256, 12288, 0);
  CHECK(reloaded.begin().loaded == 1);
  CHECK(reloaded.has("keep"));
}

void test_slot_count_change() {
  std::printf("store: max_commands change is detected, not silently destructive\n");
  FakeBackend backend;
  {
    CommandStore store;
    store.configure(&backend, 8, 256, 12288, 0);
    store.begin();
    CHECK(store.put(make_command("a")) == StoreResult::OK);
    CHECK(store.put(make_command("b")) == StoreResult::OK);
  }
  CommandStore smaller;
  smaller.configure(&backend, 4, 256, 12288, 0);
  const LoadReport report = smaller.begin();
  CHECK(report.slot_count_changed);
  CHECK(report.stored_slot_count == 8);
  // Both commands sat in slots 0 and 1, so both still fit.
  CHECK(report.loaded == 2);
}

void test_key_salt_isolates_instances() {
  std::printf("store: two instances on one device do not share NVS keys\n");
  FakeBackend backend;  // one device, one preferences namespace

  CommandStore first;
  first.configure(&backend, 4, 256, 12288, 0x1111'1111);
  first.begin();
  CHECK(first.put(make_command("only_in_first")) == StoreResult::OK);

  CommandStore second;
  second.configure(&backend, 4, 256, 12288, 0x2222'2222);
  const LoadReport report = second.begin();
  CHECK(!report.had_stored_data);
  CHECK(second.count() == 0);
  CHECK(second.put(make_command("only_in_second")) == StoreResult::OK);

  // Neither instance can see or clobber the other.
  CommandStore first_again;
  first_again.configure(&backend, 4, 256, 12288, 0x1111'1111);
  CHECK(first_again.begin().loaded == 1);
  CHECK(first_again.has("only_in_first"));
  CHECK(!first_again.has("only_in_second"));
}

void test_used_bytes() {
  std::printf("store: used_bytes accounting\n");
  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 16, 256, 12288, 0);
  store.begin();
  const uint32_t empty_bytes = store.used_bytes();
  CHECK(empty_bytes == 10 + 16 * 40 + 2);
  CHECK(store.put(make_command("a")) == StoreResult::OK);
  CHECK(store.used_bytes() == empty_bytes + 65 * 4);
  CHECK(store.remove("a") == StoreResult::OK);
  CHECK(store.used_bytes() == empty_bytes);
  CHECK(store.worst_case_bytes() == empty_bytes + 16 * 256 * 4);
}

}  // namespace

int main() {
  test_strip_terminator();
  test_validate_frame();
  test_frames_agree();
  test_helpers();
  test_split_frames();
  test_estimate_gap();
  test_name_validation();
  test_store_roundtrip();
  test_store_overwrite_and_delete();
  test_store_full_and_budget();
  test_store_rejects_bad_names();
  test_corruption_is_survivable();
  test_header_corruption_and_version();
  test_failed_write_does_not_corrupt();
  test_slot_count_change();
  test_key_salt_isolates_instances();
  test_used_bytes();

  std::printf("\n%d checks, %d failure(s)\n", g_checks, g_failures);
  return g_failures == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
