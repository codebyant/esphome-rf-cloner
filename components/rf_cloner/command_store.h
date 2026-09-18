#pragma once

// Persistent command registry. Storage is reached through the injected StorageBackend and
// failures are returned as enums, so this builds and tests on the host.
//
// Identity model: a command is identified by an immutable command_id, and its name is mutable
// metadata. The registry as a whole carries a bridge_id that is independent of the physical
// board, so a replacement ESP32 can be restored into the same logical bridge.

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace esphome::rf_cloner {

static constexpr uint16_t STORE_MAGIC = 0x5246;  // 'RF'
/// The first supported on-flash format. Pre-release builds wrote a different layout under the
/// same number; it is not recognised, and a device carrying it is refused rather than reinterpreted
/// (see begin()). Bump this only for formats that shipped.
static constexpr uint8_t STORE_FORMAT_VERSION = 1;

static constexpr size_t NAME_BUF_LEN = 24;  // 23 usable characters plus NUL
static constexpr size_t NAME_MAX_CHARS = NAME_BUF_LEN - 1;

static constexpr size_t BRIDGE_ID_BYTES = 16;
/// Not a valid command id; marks a free slot on flash.
static constexpr uint32_t COMMAND_ID_NONE = 0;
static constexpr uint32_t COMMAND_ID_FIRST = 1;
/// Highest assignable id. next_command_id past this means the space is exhausted; it never wraps,
/// because a reused id would silently redirect a Home Assistant entity at a different command.
static constexpr uint32_t COMMAND_ID_LAST = 0xFFFFFFFEu;

/// Revision of a registry that has just been written for the first time, by a fresh boot or a
/// factory reset. Every committed mutation adds one, so revision 0 never reaches flash and a
/// reader can treat "revision changed" as "the registry changed" without a special empty case.
static constexpr uint32_t REVISION_INITIAL = 1;

/// Fills `out` with `len` random bytes, used once per bridge to mint a bridge_id.
///
/// Deliberately left undefined here: the device build satisfies it from ESPHome's random_bytes()
/// and the host tests supply a deterministic stand-in, which keeps this translation unit free of
/// ESPHome headers.
bool store_random_bytes(uint8_t *out, size_t len);

enum class StoreResult : uint8_t {
  OK = 0,
  NAME_EMPTY,
  NAME_TOO_LONG,
  NAME_INVALID,
  NAME_TAKEN,
  NOT_FOUND,
  STORE_FULL,
  TOO_MANY_PULSES,
  BUDGET_EXCEEDED,
  STORAGE_ERROR,
  /// Storage holds data this firmware refuses to interpret or rewrite; see LoadReport::fault.
  READ_ONLY,
  ID_EXHAUSTED,
  ID_CONFLICT,
  RESTORE_NOT_EMPTY,
  RESTORE_NOT_ACTIVE,
  /// A restore is in progress; finish it, or clear to abandon it.
  RESTORE_IN_PROGRESS,
};

const char *store_result_to_string(StoreResult result);

/// Why begin() put the store into read-only mode. Anything other than NONE means flash holds data
/// that was left exactly as it was found.
enum class StoreFault : uint8_t {
  NONE = 0,
  /// Header record present but its magic, CRC or declared index length does not check out.
  HEADER_INVALID,
  /// Header was fine; the index it points at is missing or failed its CRC.
  INDEX_INVALID,
  /// format_version is not one this firmware writes.
  VERSION_UNSUPPORTED,
  /// An occupied slot's waveform is missing, the wrong length, or failed its CRC. The index still
  /// describes that command, so rewriting it would drop the command for good.
  PAYLOAD_INVALID,
};

const char *store_fault_to_string(StoreFault fault);

struct Command {
  /// Assigned by put(); never reused, and never changed by a rename.
  uint32_t command_id{COMMAND_ID_NONE};
  std::string name;
  std::vector<int32_t> timings;
  uint32_t gap_us{0};
  uint16_t repeat_times{1};
  uint32_t frequency_hz{0};
  uint8_t modulation{0};
};

/// One row of the registry listing, without the waveform.
struct CommandEntry {
  uint32_t command_id{COMMAND_ID_NONE};
  std::string name;
  uint16_t pulse_count{0};
};

/// What begin() found on flash; the component logs it.
struct LoadReport {
  bool had_stored_data{false};
  bool header_invalid{false};
  bool version_mismatch{false};
  bool slot_count_changed{false};
  /// A restore was interrupted before it committed. The registry still replays.
  bool restore_incomplete{false};
  /// Mutations are refused for this session. Flash was left untouched.
  bool read_only{false};
  StoreFault fault{StoreFault::NONE};
  uint8_t stored_format_version{0};
  uint8_t stored_slot_count{0};
  uint8_t loaded{0};
  /// Occupied slots whose waveform could not be read. Any of these makes the session read-only,
  /// because the index still names the command and rewriting it would discard it.
  uint8_t payload_faults{0};
  /// Slot and name of the first payload fault, so the log can say which command is affected.
  uint8_t fault_slot{0};
  std::string fault_name;
  /// Slots the current max_commands / max_pulses can no longer hold. The records are intact and
  /// are left on flash; restoring the previous configuration brings them back.
  uint8_t dropped_unfittable{0};
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

  /// Read the registry.
  ///
  /// The fixed-size header is read first and states the format version and the exact length of
  /// the index that follows, so a record this firmware does not understand is recognised as such
  /// instead of being parsed as something else. Anything unreadable leaves flash exactly as found
  /// and puts the store into read-only mode, so a later user action cannot overwrite data that
  /// might still be recoverable. factory_reset() is the deliberate way out.
  LoadReport begin();

  /// Insert, or overwrite the command of the same name, keeping that command's id so an entity
  /// built on it survives a relearn. Payload is written before the index, so a partially applied
  /// write leaves the index pointing at the previous consistent state.
  StoreResult put(const Command &command);

  bool get(const std::string &name, Command &out) const;
  bool get_by_id(uint32_t command_id, Command &out) const;
  bool has(const std::string &name) const;
  bool has_id(uint32_t command_id) const;

  /// Change only the name. The waveform and the command id are untouched.
  StoreResult rename(uint32_t command_id, const std::string &new_name);

  StoreResult remove(const std::string &name);
  StoreResult remove_by_id(uint32_t command_id);

  /// Drop every command while keeping the logical identity: bridge_id and next_command_id are
  /// preserved, so a cleared bridge is still the same bridge and no id is ever handed out twice.
  /// Also abandons an unfinished restore, which is what makes it the abort for one.
  ///
  /// Idempotent: on an empty registry with no restore open it succeeds without writing and
  /// without advancing the revision, so a redundant clear does not look like a change.
  StoreResult clear_all();

  /// Discard everything, including the identity, and write a fresh empty registry.
  ///
  /// The one operation allowed to run while the store is read-only, because it is the only way to
  /// make a device carrying unreadable data usable again. It writes only this component's own
  /// records, so unrelated ESPHome state is untouched.
  StoreResult factory_reset();

  /// Adopt an external identity and prepare to receive imported commands. Only accepted on an
  /// empty registry, which is what keeps a restore from silently overwriting a live bridge.
  StoreResult restore_begin(const uint8_t *bridge_id, uint32_t next_command_id);
  /// Write one command under the id it held on the bridge being restored.
  StoreResult import_command(const Command &command);
  /// Mark the restore complete. Until this lands, begin() reports restore_incomplete.
  StoreResult restore_commit();

  size_t count() const;
  std::vector<std::string> names() const;
  std::vector<CommandEntry> entries() const;

  const uint8_t *bridge_id() const { return this->bridge_id_; }
  std::string bridge_id_hex() const;
  uint32_t next_command_id() const { return this->next_command_id_; }
  uint32_t revision() const { return this->revision_; }
  bool restore_incomplete() const { return this->restore_state_ != 0; }
  bool read_only() const { return this->read_only_; }
  StoreFault fault() const { return this->fault_; }

  uint32_t used_bytes() const;
  uint32_t capacity_bytes() const { return this->max_storage_bytes_; }
  uint8_t max_commands() const { return this->max_commands_; }
  uint16_t max_pulses() const { return this->max_pulses_; }
  /// Bytes required if every slot held a max_pulses command.
  uint32_t worst_case_bytes() const;
  /// Header, meta and index for `slot_count` slots, before any waveform.
  static uint32_t index_overhead_bytes(uint8_t slot_count);

  static StoreResult validate_name(const std::string &name);

 protected:
  void load_index_(uint8_t stored_slot_count, uint16_t stored_index_bytes, LoadReport &report);
  void mint_identity_();
  void enter_read_only_(LoadReport &report, StoreFault fault);
  /// Shared guard for the mutations that are not part of a restore.
  StoreResult writable_() const;

  int find_slot_(const std::string &name) const;
  int find_slot_by_id_(uint32_t command_id) const;
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

  uint8_t bridge_id_[BRIDGE_ID_BYTES]{};
  uint32_t next_command_id_{COMMAND_ID_FIRST};
  uint32_t revision_{0};
  uint8_t restore_state_{0};
  bool read_only_{false};
  StoreFault fault_{StoreFault::NONE};

  /// Occupied when command_id is not COMMAND_ID_NONE; sized by configure().
  std::vector<Command> slots_;
};

}  // namespace esphome::rf_cloner
