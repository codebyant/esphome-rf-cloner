#pragma once

// Persistent named-command registry. Storage is reached through the injected StorageBackend and
// failures are returned as enums, so this builds and tests on the host.

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace esphome::rf_cloner {

static constexpr uint16_t STORE_MAGIC = 0x5246;  // 'RF'
static constexpr uint8_t STORE_FORMAT_VERSION = 1;
static constexpr size_t NAME_BUF_LEN = 24;  // 23 usable characters plus NUL
static constexpr size_t NAME_MAX_CHARS = NAME_BUF_LEN - 1;

enum class StoreResult : uint8_t {
  OK = 0,
  NAME_EMPTY,
  NAME_TOO_LONG,
  NAME_INVALID,
  NOT_FOUND,
  STORE_FULL,
  TOO_MANY_PULSES,
  BUDGET_EXCEEDED,
  STORAGE_ERROR,
};

const char *store_result_to_string(StoreResult result);

struct Command {
  std::string name;
  std::vector<int32_t> timings;
  uint32_t gap_us{0};
  uint16_t repeat_times{1};
  uint32_t frequency_hz{0};
  uint8_t modulation{0};
};

/// What begin() found on flash; the component logs it.
struct LoadReport {
  bool had_stored_data{false};
  bool header_invalid{false};
  bool version_mismatch{false};
  bool slot_count_changed{false};
  uint8_t stored_format_version{0};
  uint8_t stored_slot_count{0};
  uint8_t loaded{0};
  uint8_t dropped_corrupt{0};
};

/// Storage abstraction: ESPHome preferences on device, an in-memory map in the host tests.
///
/// Implementors must cache handles per (key, length). ESPHome's ESP32 backend heap-allocates on
/// every global_preferences->make_preference() call and ESPPreferenceObject never frees it, so a
/// handle per save/load leaks.
class StorageBackend {
 public:
  virtual ~StorageBackend() = default;
  virtual bool save(uint32_t key, const uint8_t *data, size_t len) = 0;
  virtual bool load(uint32_t key, uint8_t *data, size_t len) = 0;
  /// Commit pending writes; a power cycle straight after a learn would otherwise lose it.
  virtual bool sync() = 0;
};

class CommandStore {
 public:
  /// `key_salt` separates the NVS keys of multiple instances on one device; codegen derives it
  /// from the component's YAML id.
  void configure(StorageBackend *backend, uint8_t max_commands, uint16_t max_pulses, uint32_t max_storage_bytes,
                 uint32_t key_salt);

  /// Read header, index and payloads. A slot that fails to load or fails its CRC is dropped from
  /// the in-RAM view only; flash is left untouched until the next write.
  LoadReport begin();

  /// Insert, or overwrite a command of the same name. Payload is written before the index, so a
  /// partially applied write leaves the index pointing at the previous consistent state.
  StoreResult put(const Command &command);

  bool get(const std::string &name, Command &out) const;
  bool has(const std::string &name) const;
  StoreResult remove(const std::string &name);
  StoreResult clear_all();

  size_t count() const;
  std::vector<std::string> names() const;

  uint32_t used_bytes() const;
  uint32_t capacity_bytes() const { return this->max_storage_bytes_; }
  uint8_t max_commands() const { return this->max_commands_; }
  uint16_t max_pulses() const { return this->max_pulses_; }
  /// Bytes required if every slot held a max_pulses command.
  uint32_t worst_case_bytes() const;

  static StoreResult validate_name(const std::string &name);

 protected:
  int find_slot_(const std::string &name) const;
  int find_free_slot_() const;
  uint32_t header_key_() const;
  uint32_t index_key_() const;
  uint32_t payload_key_(uint8_t slot) const;
  bool write_payload_(uint8_t slot);
  bool write_index_();
  uint32_t projected_bytes_(int replacing_slot, uint16_t new_pulse_count) const;

  StorageBackend *backend_{nullptr};
  uint8_t max_commands_{16};
  uint16_t max_pulses_{128};
  uint32_t max_storage_bytes_{12288};
  uint32_t key_salt_{0};
  /// Occupied when name is non-empty; sized by configure().
  std::vector<Command> slots_;
};

}  // namespace esphome::rf_cloner
