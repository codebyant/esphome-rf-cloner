// Host-side unit tests for the capture validator and the command store. No ESPHome headers, so
// this builds with a plain C++ compiler:
//
//   sh tests/run.sh

#include "capture_validator.h"
#include "command_store.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cinttypes>
#include <map>
#include <string>
#include <vector>

namespace esphome::rf_cloner {

/// Deterministic stand-in for the device's random_bytes(). Every call returns a different value,
/// so two freshly initialised stores still get distinct bridge ids, but a test run is repeatable.
bool store_random_bytes(uint8_t *out, size_t len) {
  static uint32_t counter = 0;
  counter++;
  for (size_t i = 0; i < len; i++) {
    out[i] = static_cast<uint8_t>((counter * 2654435761u) >> (8 * (i % 4))) ^ static_cast<uint8_t>(i);
  }
  return true;
}

}  // namespace esphome::rf_cloner

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
///
/// `fail_after` models a power cut: every save up to that count lands, the rest are refused, and
/// `records` then holds exactly what flash would hold at that instant.
class FakeBackend : public StorageBackend {
 public:
  bool save(uint32_t key, const uint8_t *data, size_t len) override {
    if (this->fail_next_save) {
      this->fail_next_save = false;
      return false;
    }
    if (this->fail_after >= 0 && this->writes >= static_cast<unsigned>(this->fail_after)) {
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
  /// Refuse every save once this many have landed. Negative disables it.
  int fail_after{-1};
};


/// Independent CRC-16/CCITT, so a test that hand-edits a stored record computes the check value
/// without borrowing the implementation it is checking.
uint16_t fixture_crc16(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= static_cast<uint16_t>(data[i]) << 8;
    for (int bit = 0; bit < 8; bit++) {
      crc = (crc & 0x8000u) != 0 ? static_cast<uint16_t>((crc << 1) ^ 0x1021u) : static_cast<uint16_t>(crc << 1);
    }
  }
  return crc;
}

/// The NVS key the store writes its header under, so a test can plant a record the store must
/// refuse. Restated here rather than shared, for the same reason as the CRC above.
uint32_t header_key_for(uint32_t salt) {
  uint32_t hash = 2166136261UL;
  for (const char *p = "rf_cloner/hdr/v1"; *p != '\0'; p++) {
    hash ^= static_cast<uint8_t>(*p);
    hash *= 16777619UL;
  }
  return hash ^ salt;
}
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

/// Bytes the header and index occupy before any waveform, for a store of `slots` slots.
constexpr uint32_t overhead(uint8_t slots) { return 10 + 28 + static_cast<uint32_t>(slots) * 44 + 2; }

/// Drive a store into read-only mode by damaging the header of a registry it wrote itself.
void corrupt_header(FakeBackend &backend, uint32_t salt = 0) {
  std::vector<uint8_t> &header = backend.records[header_key_for(salt)];
  header[0] ^= 0xFF;  // breaks both magic and CRC
}

/// Plant what a pre-release build left behind: our magic and our version number, but an index laid
/// out differently. The declared length is what catches it, and no slot count can make the two
/// agree, since n*40 + 2 == 28 + n*44 + 2 has no solution.
void plant_foreign_layout(FakeBackend &backend, uint8_t slot_count = 16, uint32_t salt = 0) {
  std::vector<uint8_t> header(10, 0);
  header[0] = 0x46;  // magic 0x5246, little-endian
  header[1] = 0x52;
  header[2] = STORE_FORMAT_VERSION;
  header[3] = slot_count;
  const uint16_t foreign_index_bytes = static_cast<uint16_t>(slot_count * 40 + 2);
  header[4] = static_cast<uint8_t>(foreign_index_bytes & 0xFF);
  header[5] = static_cast<uint8_t>(foreign_index_bytes >> 8);
  const uint16_t crc = fixture_crc16(header.data(), 8);
  header[8] = static_cast<uint8_t>(crc & 0xFF);
  header[9] = static_cast<uint8_t>(crc >> 8);
  backend.records[header_key_for(salt)] = header;
}

void test_layout_constants() {
  std::printf("store: on-flash sizes\n");
  // Locked by the design: growing these silently would push the registry past max_storage_bytes
  // on a device that previously fit.
  CHECK(CommandStore::index_overhead_bytes(16) == 744);
  CHECK(CommandStore::index_overhead_bytes(16) == overhead(16));
  CHECK(CommandStore::index_overhead_bytes(4) == overhead(4));
  CHECK(STORE_FORMAT_VERSION == 1);
}

void test_store_roundtrip() {
  std::printf("store: round-trip across a simulated reboot\n");
  FakeBackend backend;

  CommandStore store;
  store.configure(&backend, 16, 256, 12288, 0);
  store.begin();
  CHECK(store.count() == 0);
  CHECK(!store.read_only());
  // A fresh device mints and persists its identity immediately, so bridge_id is stable from the
  // first boot rather than changing until the first learn.
  const std::string bridge = store.bridge_id_hex();
  CHECK(bridge.size() == 32);
  CHECK(bridge != std::string(32, '0'));

  CHECK(store.put(make_command("vel_1")) == StoreResult::OK);
  CHECK(store.put(make_command("vel_2", 40)) == StoreResult::OK);
  CHECK(store.count() == 2);
  CHECK(store.has_id(1));
  CHECK(store.has_id(2));
  CHECK(store.next_command_id() == 3);

  // Reboot: fresh store object, same flash contents.
  CommandStore reloaded;
  reloaded.configure(&backend, 16, 256, 12288, 0);
  const LoadReport report = reloaded.begin();
  CHECK(report.had_stored_data);
  CHECK(report.stored_format_version == STORE_FORMAT_VERSION);
  CHECK(!report.header_invalid);
  CHECK(!report.version_mismatch);
  CHECK(!report.read_only);
  CHECK(!report.restore_incomplete);
  CHECK(report.loaded == 2);
  CHECK(report.payload_faults == 0 && report.dropped_unfittable == 0);
  CHECK(reloaded.count() == 2);
  CHECK(reloaded.bridge_id_hex() == bridge);
  CHECK(reloaded.next_command_id() == 3);
  CHECK(reloaded.revision() == store.revision());

  Command out;
  CHECK(reloaded.get("vel_1", out));
  CHECK(out.command_id == 1);
  CHECK(out.timings == reference_frame());
  CHECK(out.gap_us == 8658);
  CHECK(out.repeat_times == 20);
  CHECK(out.frequency_hz == 433920000);
  CHECK(reloaded.get_by_id(2, out));
  CHECK(out.name == "vel_2");
  CHECK(out.timings.size() == 40);
  CHECK(!reloaded.get("nope", out));
  CHECK(!reloaded.get_by_id(99, out));
  CHECK(!reloaded.get_by_id(COMMAND_ID_NONE, out));

  const std::vector<CommandEntry> rows = reloaded.entries();
  CHECK(rows.size() == 2);
  CHECK(rows[0].command_id == 1 && rows[0].name == "vel_1" && rows[0].pulse_count == 65);
  CHECK(rows[1].command_id == 2 && rows[1].name == "vel_2" && rows[1].pulse_count == 40);
}

void test_relearn_preserves_command_id() {
  std::printf("store: relearning a name keeps its command id\n");
  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 4, 256, 12288, 0);
  store.begin();

  CHECK(store.put(make_command("a")) == StoreResult::OK);
  CHECK(store.put(make_command("b")) == StoreResult::OK);
  Command out;
  CHECK(store.get("a", out));
  const uint32_t original_id = out.command_id;
  CHECK(original_id == 1);
  const uint32_t next_before = store.next_command_id();

  // Relearning overwrites in place. The id is what a Home Assistant entity is built on, so it has
  // to outlive the waveform it started with.
  CHECK(store.put(make_command("a", 20)) == StoreResult::OK);
  CHECK(store.count() == 2);
  CHECK(store.get("a", out));
  CHECK(out.command_id == original_id);
  CHECK(out.timings.size() == 20);
  // An overwrite consumes no id.
  CHECK(store.next_command_id() == next_before);

  CommandStore reloaded;
  reloaded.configure(&backend, 4, 256, 12288, 0);
  CHECK(reloaded.begin().loaded == 2);
  CHECK(reloaded.get("a", out));
  CHECK(out.command_id == original_id);
  CHECK(out.timings.size() == 20);
}

void test_deleted_ids_are_never_reused() {
  std::printf("store: a deleted id never comes back\n");
  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 4, 256, 12288, 0);
  store.begin();

  CHECK(store.put(make_command("a")) == StoreResult::OK);  // id 1
  CHECK(store.put(make_command("b")) == StoreResult::OK);  // id 2
  CHECK(store.remove_by_id(1) == StoreResult::OK);
  CHECK(store.count() == 1);
  CHECK(!store.has_id(1));
  CHECK(store.remove_by_id(1) == StoreResult::NOT_FOUND);

  // The slot is reusable; the id is not.
  CHECK(store.put(make_command("c")) == StoreResult::OK);
  Command out;
  CHECK(store.get("c", out));
  CHECK(out.command_id == 3);
  CHECK(!store.has_id(1));

  // Name-keyed delete resolves to the same command and behaves identically.
  CHECK(store.remove("b") == StoreResult::OK);
  CHECK(store.remove("b") == StoreResult::NOT_FOUND);
  CHECK(store.put(make_command("d")) == StoreResult::OK);
  CHECK(store.get("d", out));
  CHECK(out.command_id == 4);

  CommandStore reloaded;
  reloaded.configure(&backend, 4, 256, 12288, 0);
  reloaded.begin();
  CHECK(reloaded.next_command_id() == 5);
  CHECK(!reloaded.has_id(1));
  CHECK(!reloaded.has_id(2));
}

void test_id_exhaustion_fails_rather_than_wraps() {
  std::printf("store: id exhaustion refuses instead of wrapping\n");
  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 4, 256, 12288, 0);
  store.begin();

  // Drive next_command_id to the last assignable value by restoring into that state, which is the
  // only supported way to set it directly.
  uint8_t bridge[BRIDGE_ID_BYTES];
  std::memset(bridge, 0xAB, sizeof(bridge));
  CHECK(store.restore_begin(bridge, COMMAND_ID_LAST) == StoreResult::OK);
  CHECK(store.restore_commit() == StoreResult::OK);
  CHECK(store.next_command_id() == COMMAND_ID_LAST);

  CHECK(store.put(make_command("last")) == StoreResult::OK);
  Command out;
  CHECK(store.get("last", out));
  CHECK(out.command_id == COMMAND_ID_LAST);
  CHECK(store.next_command_id() == COMMAND_ID_LAST + 1);

  // No more ids, and none are recycled from the exhausted space.
  CHECK(store.put(make_command("one_too_many")) == StoreResult::ID_EXHAUSTED);
  CHECK(store.count() == 1);
  // Overwriting an existing command needs no new id, so it still works.
  CHECK(store.put(make_command("last", 20)) == StoreResult::OK);
  CHECK(store.get("last", out));
  CHECK(out.command_id == COMMAND_ID_LAST);
}

void test_rename() {
  std::printf("store: rename touches only the name\n");
  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 4, 256, 12288, 0);
  store.begin();
  CHECK(store.put(make_command("old")) == StoreResult::OK);
  CHECK(store.put(make_command("other")) == StoreResult::OK);

  const unsigned writes_before = backend.writes;
  CHECK(store.rename(1, "new") == StoreResult::OK);
  // Index only; the waveform record is not rewritten, so a rename cannot damage it.
  CHECK(backend.writes == writes_before + 2);  // index and header

  Command out;
  CHECK(store.get("new", out));
  CHECK(out.command_id == 1);
  CHECK(out.timings == reference_frame());
  CHECK(!store.has("old"));

  CHECK(store.rename(1, "other") == StoreResult::NAME_TAKEN);
  CHECK(store.rename(1, "has space") == StoreResult::NAME_INVALID);
  CHECK(store.rename(1, "") == StoreResult::NAME_EMPTY);
  CHECK(store.rename(99, "orphan") == StoreResult::NOT_FOUND);
  CHECK(store.has("new"));

  // Renaming to the current name is a no-op and costs neither a write nor a revision.
  const uint32_t revision_before = store.revision();
  const unsigned writes_now = backend.writes;
  CHECK(store.rename(1, "new") == StoreResult::OK);
  CHECK(store.revision() == revision_before);
  CHECK(backend.writes == writes_now);

  CommandStore reloaded;
  reloaded.configure(&backend, 4, 256, 12288, 0);
  CHECK(reloaded.begin().loaded == 2);
  CHECK(reloaded.get_by_id(1, out));
  CHECK(out.name == "new");
  CHECK(out.timings == reference_frame());
}

void test_clear_preserves_identity() {
  std::printf("store: clear keeps bridge_id and next_command_id\n");
  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 4, 256, 12288, 0);
  store.begin();
  CHECK(store.put(make_command("a")) == StoreResult::OK);
  CHECK(store.put(make_command("b")) == StoreResult::OK);

  const std::string bridge = store.bridge_id_hex();
  const uint32_t next_id = store.next_command_id();
  const uint32_t revision_before = store.revision();

  CHECK(store.clear_all() == StoreResult::OK);
  CHECK(store.count() == 0);
  // Clearing the commands does not make this a different logical bridge, and the ids that were
  // handed out stay spent.
  CHECK(store.bridge_id_hex() == bridge);
  CHECK(store.next_command_id() == next_id);
  CHECK(store.revision() == revision_before + 1);

  CHECK(store.put(make_command("c")) == StoreResult::OK);
  Command out;
  CHECK(store.get("c", out));
  CHECK(out.command_id == next_id);

  CommandStore reloaded;
  reloaded.configure(&backend, 4, 256, 12288, 0);
  reloaded.begin();
  CHECK(reloaded.bridge_id_hex() == bridge);
  CHECK(reloaded.count() == 1);
  CHECK(!reloaded.has_id(1));
  CHECK(!reloaded.has_id(2));
}

void test_clear_is_idempotent() {
  std::printf("store: clear only writes when it changes something\n");
  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 8, 256, 12288, 0);
  store.begin();
  const std::string bridge = store.bridge_id_hex();

  // Case 1: commands present. Normal clear, one write, one revision.
  CHECK(store.put(make_command("a")) == StoreResult::OK);
  CHECK(store.put(make_command("b")) == StoreResult::OK);
  uint32_t revision = store.revision();
  unsigned writes = backend.writes;
  CHECK(store.clear_all() == StoreResult::OK);
  CHECK(store.count() == 0);
  CHECK(store.revision() == revision + 1);
  CHECK(backend.writes > writes);
  CHECK(store.bridge_id_hex() == bridge);
  CHECK(store.next_command_id() == 3);

  // Case 2: already empty, no restore open. Succeeds, but changes nothing at all.
  revision = store.revision();
  writes = backend.writes;
  const std::map<uint32_t, std::vector<uint8_t>> untouched = backend.records;
  CHECK(store.clear_all() == StoreResult::OK);
  CHECK(store.revision() == revision);
  CHECK(backend.writes == writes);
  CHECK(backend.records == untouched);
  // Still idempotent when repeated.
  CHECK(store.clear_all() == StoreResult::OK);
  CHECK(store.revision() == revision);
  CHECK(backend.writes == writes);
  // And the identity is untouched by a no-op.
  CHECK(store.bridge_id_hex() == bridge);
  CHECK(store.next_command_id() == 3);

  // Case 3: a restore is open with nothing imported yet. The registry is empty, but there is
  // still something to clear, so this must write and count as a mutation.
  uint8_t adopted[BRIDGE_ID_BYTES];
  std::memset(adopted, 0x6C, sizeof(adopted));
  CHECK(store.restore_begin(adopted, 9) == StoreResult::OK);
  CHECK(store.restore_incomplete());
  CHECK(store.count() == 0);
  revision = store.revision();
  writes = backend.writes;
  CHECK(store.clear_all() == StoreResult::OK);
  CHECK(!store.restore_incomplete());
  CHECK(store.revision() == revision + 1);
  CHECK(backend.writes > writes);
  // The adopted identity survives the abort, so the restore can simply be replayed.
  CHECK(store.bridge_id_hex() == "6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c");
  CHECK(store.next_command_id() == 9);

  // Now that the restore is gone, clearing is a no-op again.
  revision = store.revision();
  writes = backend.writes;
  CHECK(store.clear_all() == StoreResult::OK);
  CHECK(store.revision() == revision);
  CHECK(backend.writes == writes);

  // The no-ops left nothing behind for the next boot to disagree about.
  CommandStore reloaded;
  reloaded.configure(&backend, 8, 256, 12288, 0);
  const LoadReport report = reloaded.begin();
  CHECK(report.loaded == 0);
  CHECK(!report.restore_incomplete);
  CHECK(reloaded.revision() == revision);
  CHECK(reloaded.next_command_id() == 9);
}

void test_factory_reset() {
  std::printf("store: factory reset is the way out of read-only\n");
  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 8, 256, 12288, 0);
  store.begin();
  CHECK(store.put(make_command("a")) == StoreResult::OK);
  CHECK(store.put(make_command("b")) == StoreResult::OK);
  const std::string old_bridge = store.bridge_id_hex();

  corrupt_header(backend);
  CommandStore stuck;
  stuck.configure(&backend, 8, 256, 12288, 0);
  const LoadReport report = stuck.begin();
  CHECK(report.read_only);
  CHECK(stuck.put(make_command("blocked")) == StoreResult::READ_ONLY);

  // The one operation allowed while read-only, and the only way back to a usable device.
  CHECK(stuck.factory_reset() == StoreResult::OK);
  CHECK(!stuck.read_only());
  CHECK(stuck.fault() == StoreFault::NONE);
  CHECK(stuck.count() == 0);
  // A factory reset is a new logical bridge, so the identity and the id counter start over.
  CHECK(stuck.bridge_id_hex() != old_bridge);
  CHECK(stuck.bridge_id_hex() != std::string(32, '0'));
  CHECK(stuck.next_command_id() == COMMAND_ID_FIRST);
  CHECK(stuck.put(make_command("fresh")) == StoreResult::OK);

  CommandStore reloaded;
  reloaded.configure(&backend, 8, 256, 12288, 0);
  const LoadReport after = reloaded.begin();
  CHECK(!after.read_only);
  CHECK(after.loaded == 1);
  CHECK(reloaded.bridge_id_hex() == stuck.bridge_id_hex());
}

void test_restore_flow() {
  std::printf("store: restore adopts an external identity\n");
  uint8_t bridge[BRIDGE_ID_BYTES];
  for (size_t i = 0; i < sizeof(bridge); i++) {
    bridge[i] = static_cast<uint8_t>(0x10 + i);
  }

  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 8, 256, 12288, 0);
  store.begin();
  const std::string minted = store.bridge_id_hex();

  // A restore onto a populated registry is refused. This is the guard that keeps a stale Home
  // Assistant backup from overwriting a live bridge.
  CHECK(store.put(make_command("local")) == StoreResult::OK);
  CHECK(store.restore_begin(bridge, 7) == StoreResult::RESTORE_NOT_EMPTY);
  CHECK(store.bridge_id_hex() == minted);

  // Importing outside a restore is refused too, so an id can never be written that
  // next_command_id knows nothing about.
  Command stray = make_command("stray");
  stray.command_id = 3;
  CHECK(store.import_command(stray) == StoreResult::RESTORE_NOT_ACTIVE);

  CHECK(store.clear_all() == StoreResult::OK);
  CHECK(store.restore_begin(bridge, 7) == StoreResult::OK);
  CHECK(store.bridge_id_hex() == "101112131415161718191a1b1c1d1e1f");
  CHECK(store.next_command_id() == 7);
  CHECK(store.restore_incomplete());

  Command first = make_command("vel_1");
  first.command_id = 2;
  CHECK(store.import_command(first) == StoreResult::OK);

  Command second = make_command("vel_6", 40);
  second.command_id = 6;
  CHECK(store.import_command(second) == StoreResult::OK);

  // Rejections that keep the restored registry self-consistent.
  Command duplicate_id = make_command("dup");
  duplicate_id.command_id = 2;
  CHECK(store.import_command(duplicate_id) == StoreResult::ID_CONFLICT);
  Command future_id = make_command("future");
  future_id.command_id = 7;
  CHECK(store.import_command(future_id) == StoreResult::ID_CONFLICT);
  Command zero_id = make_command("zero");
  zero_id.command_id = COMMAND_ID_NONE;
  CHECK(store.import_command(zero_id) == StoreResult::ID_CONFLICT);
  Command duplicate_name = make_command("vel_1");
  duplicate_name.command_id = 3;
  CHECK(store.import_command(duplicate_name) == StoreResult::NAME_TAKEN);
  CHECK(store.count() == 2);

  CHECK(store.restore_commit() == StoreResult::OK);
  CHECK(!store.restore_incomplete());
  CHECK(store.restore_commit() == StoreResult::RESTORE_NOT_ACTIVE);

  CommandStore reloaded;
  reloaded.configure(&backend, 8, 256, 12288, 0);
  const LoadReport report = reloaded.begin();
  CHECK(!report.restore_incomplete);
  CHECK(report.loaded == 2);
  CHECK(reloaded.bridge_id_hex() == "101112131415161718191a1b1c1d1e1f");
  CHECK(reloaded.next_command_id() == 7);
  Command out;
  CHECK(reloaded.get_by_id(6, out));
  CHECK(out.name == "vel_6");
  CHECK(out.timings.size() == 40);
  // Learning after a restore continues from the restored id counter.
  CHECK(reloaded.put(make_command("fresh")) == StoreResult::OK);
  CHECK(reloaded.get("fresh", out));
  CHECK(out.command_id == 7);
}

void test_restore_in_progress_blocks_normal_mutations() {
  std::printf("store: a half-restored registry accepts only restore work\n");
  uint8_t bridge[BRIDGE_ID_BYTES];
  std::memset(bridge, 0x5A, sizeof(bridge));

  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 8, 256, 12288, 0);
  store.begin();
  CHECK(store.restore_begin(bridge, 5) == StoreResult::OK);

  Command one = make_command("vel_1");
  one.command_id = 1;
  CHECK(store.import_command(one) == StoreResult::OK);

  // Learning into, renaming inside or deleting from a registry that is only half restored would
  // race the rest of the import.
  CHECK(store.put(make_command("learned")) == StoreResult::RESTORE_IN_PROGRESS);
  CHECK(store.rename(1, "renamed") == StoreResult::RESTORE_IN_PROGRESS);
  CHECK(store.remove("vel_1") == StoreResult::RESTORE_IN_PROGRESS);
  CHECK(store.remove_by_id(1) == StoreResult::RESTORE_IN_PROGRESS);
  CHECK(store.count() == 1);

  // Reads are never blocked: the bridge keeps replaying what it already has.
  Command out;
  CHECK(store.get_by_id(1, out));
  CHECK(out.name == "vel_1");

  CHECK(store.restore_commit() == StoreResult::OK);
  CHECK(store.put(make_command("learned")) == StoreResult::OK);
}

void test_interrupted_restore_is_visible() {
  std::printf("store: an interrupted restore is reported, not hidden\n");
  uint8_t bridge[BRIDGE_ID_BYTES];
  std::memset(bridge, 0x5A, sizeof(bridge));

  FakeBackend backend;
  {
    CommandStore store;
    store.configure(&backend, 8, 256, 12288, 0);
    store.begin();
    CHECK(store.restore_begin(bridge, 5) == StoreResult::OK);
    Command one = make_command("vel_1");
    one.command_id = 1;
    CHECK(store.import_command(one) == StoreResult::OK);
    // Power lost here, before restore_commit().
  }

  CommandStore reloaded;
  reloaded.configure(&backend, 8, 256, 12288, 0);
  const LoadReport report = reloaded.begin();
  CHECK(report.restore_incomplete);
  CHECK(reloaded.restore_incomplete());
  CHECK(!reloaded.read_only());  // still a working bridge
  CHECK(reloaded.count() == 1);
  CHECK(reloaded.next_command_id() == 5);

  // Replay is the recovery path, and clearing is how the abandoned attempt is dropped. The
  // adopted identity is kept, so the retry restores into the same logical bridge.
  CHECK(reloaded.clear_all() == StoreResult::OK);
  CHECK(!reloaded.restore_incomplete());
  CHECK(reloaded.bridge_id_hex() == "5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a");
  CHECK(reloaded.restore_begin(bridge, 5) == StoreResult::OK);
  Command one = make_command("vel_1");
  one.command_id = 1;
  CHECK(reloaded.import_command(one) == StoreResult::OK);
  CHECK(reloaded.restore_commit() == StoreResult::OK);
  CHECK(!reloaded.restore_incomplete());
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

  // Budget: the index alone is 10 + 28 + 2*44 + 2 = 128 bytes, so 220 leaves room for 23 pulses.
  FakeBackend tight_backend;
  CommandStore tight;
  tight.configure(&tight_backend, 2, 256, 220, 0);
  tight.begin();
  CHECK(overhead(2) == 128);
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
  // A rejected name consumes no id.
  CHECK(store.next_command_id() == COMMAND_ID_FIRST);
}

void test_corrupt_payload_is_read_only() {
  std::printf("store: an unreadable waveform makes the session read-only\n");
  FakeBackend backend;
  {
    CommandStore store;
    store.configure(&backend, 4, 256, 12288, 0);
    store.begin();
    CHECK(store.put(make_command("first", 65)) == StoreResult::OK);   // id 1, slot 0
    CHECK(store.put(make_command("second", 40)) == StoreResult::OK);  // id 2, slot 1
    CHECK(store.put(make_command("third", 30)) == StoreResult::OK);   // id 3, slot 2
  }
  // Damage the middle command's waveform only.
  for (auto &entry : backend.records) {
    if (entry.second.size() == 40 * sizeof(int32_t)) {
      entry.second[7] ^= 0xFF;
      break;
    }
  }
  const std::map<uint32_t, std::vector<uint8_t>> untouched = backend.records;

  CommandStore store;
  store.configure(&backend, 4, 256, 12288, 0);
  const LoadReport report = store.begin();

  // A specific fault, naming the affected command.
  CHECK(report.payload_faults == 1);
  CHECK(report.fault == StoreFault::PAYLOAD_INVALID);
  CHECK(report.fault_slot == 1);
  CHECK(report.fault_name == "second");
  CHECK(report.read_only);
  CHECK(store.read_only());
  CHECK(store.fault() == StoreFault::PAYLOAD_INVALID);

  // Siblings still load, and stay readable and sendable.
  CHECK(report.loaded == 2);
  CHECK(store.count() == 2);
  Command out;
  CHECK(store.get_by_id(1, out));
  CHECK(out.name == "first");
  CHECK(out.timings == reference_frame());
  CHECK(store.get_by_id(3, out));
  CHECK(out.timings.size() == 30);
  CHECK(!store.get_by_id(2, out));
  const std::vector<CommandEntry> rows = store.entries();
  CHECK(rows.size() == 2);

  // Every normal mutation is refused, and none of them writes a byte.
  const unsigned writes_before = backend.writes;
  uint8_t bridge[BRIDGE_ID_BYTES];
  std::memset(bridge, 0x33, sizeof(bridge));
  Command imported = make_command("imported");
  imported.command_id = 1;
  CHECK(store.put(make_command("another")) == StoreResult::READ_ONLY);
  CHECK(store.rename(1, "renamed") == StoreResult::READ_ONLY);
  CHECK(store.remove("first") == StoreResult::READ_ONLY);
  CHECK(store.remove_by_id(1) == StoreResult::READ_ONLY);
  CHECK(store.clear_all() == StoreResult::READ_ONLY);
  CHECK(store.restore_begin(bridge, 9) == StoreResult::READ_ONLY);
  CHECK(store.import_command(imported) == StoreResult::READ_ONLY);
  CHECK(store.restore_commit() == StoreResult::READ_ONLY);
  CHECK(backend.writes == writes_before);
  CHECK(backend.records == untouched);

  // Rebooting does not quietly finish the job: the index still names the damaged command, so a
  // repaired payload would come back.
  CommandStore rebooted;
  rebooted.configure(&backend, 4, 256, 12288, 0);
  const LoadReport again = rebooted.begin();
  CHECK(again.read_only);
  CHECK(again.payload_faults == 1);
  CHECK(again.loaded == 2);
  CHECK(backend.records == untouched);

  // Repairing the record is enough; nothing had to be relearned.
  for (auto &entry : backend.records) {
    if (entry.second.size() == 40 * sizeof(int32_t)) {
      entry.second[7] ^= 0xFF;
      break;
    }
  }
  CommandStore repaired;
  repaired.configure(&backend, 4, 256, 12288, 0);
  const LoadReport healthy = repaired.begin();
  CHECK(!healthy.read_only);
  CHECK(healthy.payload_faults == 0);
  CHECK(healthy.loaded == 3);
  CHECK(repaired.get_by_id(2, out));
  CHECK(out.name == "second");
  CHECK(out.timings.size() == 40);
}

void test_corrupt_payload_recovers_via_factory_reset() {
  std::printf("store: factory reset still escapes a payload fault\n");
  FakeBackend backend;
  {
    CommandStore store;
    store.configure(&backend, 4, 256, 12288, 0);
    store.begin();
    CHECK(store.put(make_command("keeper", 65)) == StoreResult::OK);
    CHECK(store.put(make_command("broken", 40)) == StoreResult::OK);
  }
  for (auto &entry : backend.records) {
    if (entry.second.size() == 40 * sizeof(int32_t)) {
      entry.second[0] ^= 0xFF;
      break;
    }
  }

  CommandStore store;
  store.configure(&backend, 4, 256, 12288, 0);
  CHECK(store.begin().fault == StoreFault::PAYLOAD_INVALID);
  CHECK(store.read_only());

  CHECK(store.factory_reset() == StoreResult::OK);
  CHECK(!store.read_only());
  CHECK(store.fault() == StoreFault::NONE);
  CHECK(store.count() == 0);
  CHECK(store.next_command_id() == COMMAND_ID_FIRST);
  CHECK(store.revision() == REVISION_INITIAL);

  CommandStore rebooted;
  rebooted.configure(&backend, 4, 256, 12288, 0);
  const LoadReport report = rebooted.begin();
  CHECK(!report.read_only);
  CHECK(report.payload_faults == 0);
  CHECK(report.loaded == 0);
  CHECK(rebooted.put(make_command("fresh")) == StoreResult::OK);
}

void test_unfittable_slot_is_not_a_payload_fault() {
  std::printf("store: a slot the config outgrew is reported separately\n");
  FakeBackend backend;
  {
    CommandStore store;
    store.configure(&backend, 4, 256, 12288, 0);
    store.begin();
    CHECK(store.put(make_command("long_one", 65)) == StoreResult::OK);
    CHECK(store.put(make_command("short_one", 20)) == StoreResult::OK);
  }
  // max_pulses lowered below what the first command needs. The record is intact, so this is a
  // configuration conflict rather than corruption.
  CommandStore store;
  store.configure(&backend, 4, 32, 12288, 0);
  const LoadReport report = store.begin();
  CHECK(report.dropped_unfittable == 1);
  CHECK(report.payload_faults == 0);
  CHECK(report.fault == StoreFault::NONE);
  CHECK(report.loaded == 1);
  CHECK(store.has("short_one"));

  // Restoring the configuration brings it back.
  CommandStore restored;
  restored.configure(&backend, 4, 256, 12288, 0);
  CHECK(restored.begin().loaded == 2);
  CHECK(restored.has("long_one"));
}

void test_header_corruption_is_read_only() {
  std::printf("store: a damaged header is refused, not overwritten\n");
  FakeBackend backend;
  {
    CommandStore store;
    store.configure(&backend, 4, 256, 12288, 0);
    store.begin();
    CHECK(store.put(make_command("x")) == StoreResult::OK);
  }
  corrupt_header(backend);
  const std::map<uint32_t, std::vector<uint8_t>> untouched = backend.records;

  CommandStore store;
  store.configure(&backend, 4, 256, 12288, 0);
  const LoadReport report = store.begin();
  CHECK(report.had_stored_data);
  CHECK(report.header_invalid);
  CHECK(report.read_only);
  CHECK(report.fault == StoreFault::HEADER_INVALID);
  CHECK(store.count() == 0);
  CHECK(backend.records == untouched);
}

void test_future_format_is_refused() {
  std::printf("store: an unsupported format version is refused, not read as empty\n");
  FakeBackend backend;
  {
    CommandStore store;
    store.configure(&backend, 4, 256, 12288, 0);
    store.begin();
    CHECK(store.put(make_command("x")) == StoreResult::OK);
  }
  std::vector<uint8_t> &header = backend.records[header_key_for(0)];
  header[2] = STORE_FORMAT_VERSION + 1;
  const uint16_t crc = fixture_crc16(header.data(), 8);
  header[8] = static_cast<uint8_t>(crc & 0xFF);
  header[9] = static_cast<uint8_t>(crc >> 8);
  const std::map<uint32_t, std::vector<uint8_t>> untouched = backend.records;

  CommandStore store;
  store.configure(&backend, 4, 256, 12288, 0);
  const LoadReport report = store.begin();
  CHECK(report.version_mismatch);
  CHECK(!report.header_invalid);
  CHECK(report.read_only);
  CHECK(report.fault == StoreFault::VERSION_UNSUPPORTED);
  CHECK(store.count() == 0);
  // Flash is left intact so another build can still read it.
  CHECK(backend.records == untouched);
}

void test_foreign_index_length_is_refused() {
  std::printf("store: a same-version record of the wrong shape is refused\n");
  FakeBackend backend;
  plant_foreign_layout(backend);
  const std::map<uint32_t, std::vector<uint8_t>> untouched = backend.records;

  CommandStore store;
  store.configure(&backend, 16, 256, 12288, 0);
  const LoadReport report = store.begin();
  CHECK(report.had_stored_data);
  CHECK(report.stored_format_version == STORE_FORMAT_VERSION);
  CHECK(report.header_invalid);
  CHECK(report.read_only);
  CHECK(report.fault == StoreFault::HEADER_INVALID);
  CHECK(store.count() == 0);
  CHECK(backend.records == untouched);
  CHECK(backend.writes == 0);
}

void test_factory_reset_is_the_only_escape_from_read_only() {
  std::printf("store: factory reset is the only destructive exception to read-only\n");
  FakeBackend backend;
  plant_foreign_layout(backend);
  const std::map<uint32_t, std::vector<uint8_t>> untouched = backend.records;

  CommandStore store;
  store.configure(&backend, 16, 256, 12288, 0);
  CHECK(store.begin().read_only);
  CHECK(store.read_only());

  // Every normal mutation is refused, including the ones that would otherwise destroy data.
  uint8_t bridge[BRIDGE_ID_BYTES];
  std::memset(bridge, 0x77, sizeof(bridge));
  Command imported = make_command("imported");
  imported.command_id = 1;
  CHECK(store.put(make_command("learned")) == StoreResult::READ_ONLY);
  CHECK(store.rename(1, "renamed") == StoreResult::READ_ONLY);
  CHECK(store.remove("anything") == StoreResult::READ_ONLY);
  CHECK(store.remove_by_id(1) == StoreResult::READ_ONLY);
  CHECK(store.clear_all() == StoreResult::READ_ONLY);
  CHECK(store.restore_begin(bridge, 4) == StoreResult::READ_ONLY);
  CHECK(store.import_command(imported) == StoreResult::READ_ONLY);
  CHECK(store.restore_commit() == StoreResult::READ_ONLY);
  CHECK(backend.records == untouched);
  CHECK(backend.writes == 0);

  // The single exception, and the only way to make the device usable again.
  CHECK(store.factory_reset() == StoreResult::OK);
  CHECK(!store.read_only());
  CHECK(store.fault() == StoreFault::NONE);
  CHECK(backend.records != untouched);

  // Reboot: a healthy, empty, writable registry with no trace of the refused layout.
  CommandStore rebooted;
  rebooted.configure(&backend, 16, 256, 12288, 0);
  const LoadReport report = rebooted.begin();
  CHECK(report.had_stored_data);
  CHECK(!report.read_only);
  CHECK(!report.header_invalid);
  CHECK(!report.version_mismatch);
  CHECK(!report.restore_incomplete);
  CHECK(report.fault == StoreFault::NONE);
  CHECK(report.loaded == 0);
  CHECK(report.payload_faults == 0 && report.dropped_unfittable == 0);
  CHECK(rebooted.count() == 0);
  CHECK(!rebooted.read_only());
  CHECK(rebooted.put(make_command("first")) == StoreResult::OK);
  Command out;
  CHECK(rebooted.get("first", out));
  CHECK(out.command_id == COMMAND_ID_FIRST);
}

void test_post_factory_reset_state() {
  std::printf("store: the state a factory reset leaves behind\n");
  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 8, 256, 12288, 0);
  store.begin();

  // Put the registry somewhere well away from its initial state first, so nothing below passes by
  // coincidence: three ids spent, one deleted, an identity already minted.
  CHECK(store.put(make_command("a")) == StoreResult::OK);
  CHECK(store.put(make_command("b")) == StoreResult::OK);
  CHECK(store.put(make_command("c")) == StoreResult::OK);
  CHECK(store.remove_by_id(2) == StoreResult::OK);
  const std::string old_bridge = store.bridge_id_hex();
  CHECK(store.next_command_id() == 4);
  CHECK(store.revision() > REVISION_INITIAL);

  CHECK(store.factory_reset() == StoreResult::OK);

  // The exact post-reset contract.
  const std::string new_bridge = store.bridge_id_hex();
  CHECK(new_bridge != old_bridge);              // a newly generated identity
  CHECK(new_bridge != std::string(32, '0'));    // and a real one, not a failed mint
  CHECK(new_bridge.size() == 32);
  CHECK(store.next_command_id() == COMMAND_ID_FIRST);
  CHECK(store.next_command_id() == 1);
  CHECK(store.count() == 0);
  CHECK(!store.restore_incomplete());           // restore_state == 0
  CHECK(store.revision() == REVISION_INITIAL);
  CHECK(!store.read_only());
  CHECK(store.fault() == StoreFault::NONE);
  CHECK(store.entries().empty());
  CHECK(store.used_bytes() == overhead(8));

  // Ids start over from 1: the previous generation's ids belonged to a different bridge, so
  // reusing the numbers cannot collide with anything that survives.
  CHECK(store.put(make_command("fresh")) == StoreResult::OK);
  Command out;
  CHECK(store.get("fresh", out));
  CHECK(out.command_id == 1);
  CHECK(store.revision() == REVISION_INITIAL + 1);

  // Reboot: the new identity is on flash, not just in RAM.
  CommandStore rebooted;
  rebooted.configure(&backend, 8, 256, 12288, 0);
  const LoadReport report = rebooted.begin();
  CHECK(!report.read_only);
  CHECK(report.loaded == 1);
  CHECK(rebooted.bridge_id_hex() == new_bridge);
  CHECK(rebooted.next_command_id() == 2);
  CHECK(rebooted.revision() == REVISION_INITIAL + 1);
  CHECK(!rebooted.restore_incomplete());
}

void test_fresh_device_matches_post_reset_state() {
  std::printf("store: a first boot and a factory reset agree on the initial state\n");
  // One semantic, reached two ways; a reader cannot tell them apart and should not have to.
  FakeBackend fresh_backend;
  CommandStore fresh;
  fresh.configure(&fresh_backend, 8, 256, 12288, 0);
  fresh.begin();

  FakeBackend reset_backend;
  CommandStore reset;
  reset.configure(&reset_backend, 8, 256, 12288, 0);
  reset.begin();
  CHECK(reset.put(make_command("doomed")) == StoreResult::OK);
  CHECK(reset.factory_reset() == StoreResult::OK);

  CHECK(fresh.revision() == reset.revision());
  CHECK(fresh.revision() == REVISION_INITIAL);
  CHECK(fresh.next_command_id() == reset.next_command_id());
  CHECK(fresh.count() == reset.count());
  CHECK(fresh.restore_incomplete() == reset.restore_incomplete());
  CHECK(fresh.read_only() == reset.read_only());
  // Different bridges, though: an identity is never shared between two registries.
  CHECK(fresh.bridge_id_hex() != reset.bridge_id_hex());
}

void test_read_only_refuses_every_mutation() {
  std::printf("store: read-only mode refuses every write except factory reset\n");
  FakeBackend backend;
  {
    CommandStore store;
    store.configure(&backend, 8, 256, 12288, 0);
    store.begin();
    CHECK(store.put(make_command("vel_1")) == StoreResult::OK);
  }
  corrupt_header(backend);

  CommandStore store;
  store.configure(&backend, 8, 256, 12288, 0);
  CHECK(store.begin().read_only);
  CHECK(store.read_only());

  const std::map<uint32_t, std::vector<uint8_t>> untouched = backend.records;
  const unsigned writes_before = backend.writes;

  CHECK(store.put(make_command("new")) == StoreResult::READ_ONLY);
  CHECK(store.rename(1, "renamed") == StoreResult::READ_ONLY);
  CHECK(store.remove("vel_1") == StoreResult::READ_ONLY);
  CHECK(store.remove_by_id(1) == StoreResult::READ_ONLY);
  CHECK(store.clear_all() == StoreResult::READ_ONLY);

  uint8_t bridge[BRIDGE_ID_BYTES];
  std::memset(bridge, 0x11, sizeof(bridge));
  CHECK(store.restore_begin(bridge, 4) == StoreResult::READ_ONLY);
  Command imported = make_command("imported");
  imported.command_id = 1;
  CHECK(store.import_command(imported) == StoreResult::READ_ONLY);
  CHECK(store.restore_commit() == StoreResult::READ_ONLY);

  // Not one byte moved.
  CHECK(backend.writes == writes_before);
  CHECK(backend.records == untouched);
}

void test_index_corruption_is_read_only() {
  std::printf("store: an unreadable index is refused, not overwritten\n");
  FakeBackend backend;
  {
    CommandStore store;
    store.configure(&backend, 16, 256, 12288, 0);
    store.begin();
    CHECK(store.put(make_command("vel_1")) == StoreResult::OK);
  }
  // The index is the only record of its exact length.
  for (auto &entry : backend.records) {
    if (entry.second.size() == 734) {
      entry.second[40] ^= 0xFF;
      break;
    }
  }
  const std::map<uint32_t, std::vector<uint8_t>> untouched = backend.records;

  CommandStore store;
  store.configure(&backend, 16, 256, 12288, 0);
  const LoadReport report = store.begin();
  CHECK(report.read_only);
  CHECK(report.fault == StoreFault::INDEX_INVALID);
  CHECK(store.count() == 0);
  CHECK(backend.records == untouched);
  CHECK(store.put(make_command("anything")) == StoreResult::READ_ONLY);
}

void test_failed_write_does_not_corrupt() {
  std::printf("store: a failed write leaves the previous state intact\n");
  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 4, 256, 12288, 0);
  store.begin();
  CHECK(store.put(make_command("keep")) == StoreResult::OK);
  const uint32_t next_id = store.next_command_id();
  const uint32_t revision = store.revision();

  backend.fail_next_save = true;
  CHECK(store.put(make_command("new_one")) == StoreResult::STORAGE_ERROR);
  CHECK(store.count() == 1);
  CHECK(store.has("keep"));
  CHECK(!store.has("new_one"));
  // A write that never landed must not spend an id or advance the revision.
  CHECK(store.next_command_id() == next_id);
  CHECK(store.revision() == revision);

  // A failed rename must not leave the new name in RAM either.
  backend.fail_next_save = true;
  CHECK(store.rename(1, "other_name") == StoreResult::STORAGE_ERROR);
  CHECK(store.has("keep"));
  CHECK(!store.has("other_name"));
  CHECK(store.revision() == revision);

  // Nor a failed delete.
  backend.fail_next_save = true;
  CHECK(store.remove_by_id(1) == StoreResult::STORAGE_ERROR);
  CHECK(store.count() == 1);
  CHECK(store.has("keep"));

  // Nor a failed clear.
  backend.fail_next_save = true;
  CHECK(store.clear_all() == StoreResult::STORAGE_ERROR);
  CHECK(store.count() == 1);
  CHECK(store.revision() == revision);

  // And the earlier command still reloads.
  CommandStore reloaded;
  reloaded.configure(&backend, 4, 256, 12288, 0);
  CHECK(reloaded.begin().loaded == 1);
  CHECK(reloaded.has("keep"));
  CHECK(reloaded.next_command_id() == next_id);
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
  // Separate registries mean separate logical bridges.
  CHECK(second.bridge_id_hex() != first.bridge_id_hex());

  // Neither instance can see or clobber the other.
  CommandStore first_again;
  first_again.configure(&backend, 4, 256, 12288, 0x1111'1111);
  CHECK(first_again.begin().loaded == 1);
  CHECK(first_again.has("only_in_first"));
  CHECK(!first_again.has("only_in_second"));
  CHECK(first_again.bridge_id_hex() == first.bridge_id_hex());
}

void test_used_bytes() {
  std::printf("store: used_bytes accounting\n");
  FakeBackend backend;
  CommandStore store;
  store.configure(&backend, 16, 256, 12288, 0);
  store.begin();
  const uint32_t empty_bytes = store.used_bytes();
  CHECK(empty_bytes == overhead(16));
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

  test_layout_constants();
  test_store_roundtrip();
  test_relearn_preserves_command_id();
  test_deleted_ids_are_never_reused();
  test_id_exhaustion_fails_rather_than_wraps();
  test_rename();
  test_clear_preserves_identity();
  test_clear_is_idempotent();
  test_factory_reset();
  test_restore_flow();
  test_restore_in_progress_blocks_normal_mutations();
  test_interrupted_restore_is_visible();
  test_store_full_and_budget();
  test_store_rejects_bad_names();
  test_corrupt_payload_is_read_only();
  test_corrupt_payload_recovers_via_factory_reset();
  test_unfittable_slot_is_not_a_payload_fault();
  test_header_corruption_is_read_only();
  test_future_format_is_refused();
  test_foreign_index_length_is_refused();
  test_factory_reset_is_the_only_escape_from_read_only();
  test_post_factory_reset_state();
  test_fresh_device_matches_post_reset_state();
  test_read_only_refuses_every_mutation();
  test_index_corruption_is_read_only();
  test_failed_write_does_not_corrupt();
  test_slot_count_change();
  test_key_salt_isolates_instances();
  test_used_bytes();

  std::printf("\n%d checks, %d failure(s)\n", g_checks, g_failures);
  return g_failures == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
