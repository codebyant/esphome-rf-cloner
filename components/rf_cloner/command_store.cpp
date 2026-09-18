#include "command_store.h"

#include <cstring>

namespace esphome::rf_cloner {

namespace {

// On-flash layout:
//
//   header  (10 bytes, fixed)     magic, format_version, slot_count, index_bytes, crc
//   index   (28 + slots*44 + 2)   registry metadata, one StoredSlot per slot, then a crc
//   payload (pulse_count * 4)     one record per occupied slot, native-endian int32 timings
//
// Split this way because ESPHome's preference load() only succeeds on an exact length match: the
// fixed-size header must be read first to learn the lengths of the index and each payload.
// Padding slots to max_pulses instead would cost about 1 kB each and exhaust the ~20 kB NVS
// budget well before the slot count did.
//
// Keeping the header a fixed size that never changes is also what lets a future format be
// recognised rather than misparsed: whatever the rest looks like, the version and the index
// length can always be read.

struct StoredHeader {
  uint16_t magic;
  uint8_t format_version;
  uint8_t slot_count;
  uint16_t index_bytes;
  uint16_t reserved;
  uint16_t crc16;  // over the preceding 8 bytes
} __attribute__((packed));

/// Registry-wide metadata, at the head of the index. Kept in the same record as the slots so that
/// assigning an id and advancing next_command_id land in one write; splitting them would open a
/// window where a crash could hand the same id out twice.
struct StoredMeta {
  uint8_t bridge_id[BRIDGE_ID_BYTES];
  uint32_t next_command_id;
  uint32_t revision;
  uint8_t restore_state;  // 1 while a restore is in progress
  uint8_t reserved[3];
} __attribute__((packed));

struct StoredSlot {
  char name[NAME_BUF_LEN];
  uint32_t command_id;
  uint16_t pulse_count;
  uint16_t repeat_times;
  uint32_t gap_us;
  uint32_t frequency_hz;
  uint8_t modulation;
  uint8_t flags;
  uint16_t payload_crc16;
} __attribute__((packed));

static_assert(sizeof(StoredHeader) == 10, "StoredHeader must stay fixed across formats");
static_assert(sizeof(StoredMeta) == 28, "StoredMeta layout changed; bump STORE_FORMAT_VERSION");
static_assert(sizeof(StoredSlot) == 44, "StoredSlot layout changed; bump STORE_FORMAT_VERSION");

constexpr size_t CRC_BYTES = 2;

size_t index_bytes_for(uint8_t slot_count) {
  return sizeof(StoredMeta) + static_cast<size_t>(slot_count) * sizeof(StoredSlot) + CRC_BYTES;
}

constexpr uint32_t fnv1a_32(const char *text) {
  uint32_t hash = 2166136261UL;
  while (*text != '\0') {
    hash ^= static_cast<uint8_t>(*text++);
    hash *= 16777619UL;
  }
  return hash;
}

constexpr uint32_t KEY_HEADER = fnv1a_32("rf_cloner/hdr/v1");
constexpr uint32_t KEY_INDEX = fnv1a_32("rf_cloner/idx/v1");
constexpr uint32_t KEY_PAYLOAD_BASE = fnv1a_32("rf_cloner/pl/v1");

uint32_t payload_key(uint8_t slot) { return KEY_PAYLOAD_BASE ^ (0x9E3779B9u * (static_cast<uint32_t>(slot) + 1u)); }

uint16_t crc16_ccitt(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= static_cast<uint16_t>(data[i]) << 8;
    for (uint8_t bit = 0; bit < 8; bit++) {
      crc = (crc & 0x8000u) != 0 ? static_cast<uint16_t>((crc << 1) ^ 0x1021u) : static_cast<uint16_t>(crc << 1);
    }
  }
  return crc;
}

uint16_t timings_crc(const std::vector<int32_t> &timings) {
  if (timings.empty()) {
    return 0;
  }
  return crc16_ccitt(reinterpret_cast<const uint8_t *>(timings.data()), timings.size() * sizeof(int32_t));
}

}  // namespace

const char *store_result_to_string(StoreResult result) {
  switch (result) {
    case StoreResult::OK:
      return "ok";
    case StoreResult::NAME_EMPTY:
      return "name_empty";
    case StoreResult::NAME_TOO_LONG:
      return "name_too_long";
    case StoreResult::NAME_INVALID:
      return "name_invalid";
    case StoreResult::NAME_TAKEN:
      return "name_taken";
    case StoreResult::NOT_FOUND:
      return "not_found";
    case StoreResult::STORE_FULL:
      return "store_full";
    case StoreResult::TOO_MANY_PULSES:
      return "too_many_pulses";
    case StoreResult::BUDGET_EXCEEDED:
      return "budget_exceeded";
    case StoreResult::STORAGE_ERROR:
      return "storage_error";
    case StoreResult::READ_ONLY:
      return "read_only";
    case StoreResult::ID_EXHAUSTED:
      return "id_exhausted";
    case StoreResult::ID_CONFLICT:
      return "id_conflict";
    case StoreResult::RESTORE_NOT_EMPTY:
      return "restore_not_empty";
    case StoreResult::RESTORE_NOT_ACTIVE:
      return "restore_not_active";
    case StoreResult::RESTORE_IN_PROGRESS:
      return "restore_in_progress";
  }
  return "unknown";
}

const char *store_fault_to_string(StoreFault fault) {
  switch (fault) {
    case StoreFault::NONE:
      return "none";
    case StoreFault::HEADER_INVALID:
      return "header_invalid";
    case StoreFault::INDEX_INVALID:
      return "index_invalid";
    case StoreFault::VERSION_UNSUPPORTED:
      return "version_unsupported";
    case StoreFault::PAYLOAD_INVALID:
      return "payload_invalid";
  }
  return "unknown";
}

StoreResult CommandStore::validate_name(const std::string &name) {
  if (name.empty()) {
    return StoreResult::NAME_EMPTY;
  }
  if (name.size() > NAME_MAX_CHARS) {
    return StoreResult::NAME_TOO_LONG;
  }
  // Names appear in Home Assistant scripts and log lines; keep the character set unambiguous.
  for (char c : name) {
    const bool allowed = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' ||
                         c == '-' || c == '.';
    if (!allowed) {
      return StoreResult::NAME_INVALID;
    }
  }
  return StoreResult::OK;
}

uint32_t CommandStore::header_key_() const { return KEY_HEADER ^ this->key_salt_; }
uint32_t CommandStore::index_key_() const { return KEY_INDEX ^ this->key_salt_; }
uint32_t CommandStore::payload_key_(uint8_t slot) const { return payload_key(slot) ^ this->key_salt_; }

uint32_t CommandStore::index_overhead_bytes(uint8_t slot_count) {
  return static_cast<uint32_t>(sizeof(StoredHeader) + index_bytes_for(slot_count));
}

std::string CommandStore::bridge_id_hex() const {
  static const char *const DIGITS = "0123456789abcdef";
  std::string out;
  out.reserve(BRIDGE_ID_BYTES * 2);
  for (uint8_t byte : this->bridge_id_) {
    out.push_back(DIGITS[byte >> 4]);
    out.push_back(DIGITS[byte & 0x0F]);
  }
  return out;
}

void CommandStore::configure(StorageBackend *backend, uint8_t max_commands, uint16_t max_pulses,
                             uint32_t max_storage_bytes, uint32_t key_salt) {
  this->backend_ = backend;
  this->key_salt_ = key_salt;
  this->max_commands_ = max_commands;
  this->max_pulses_ = max_pulses;
  this->max_storage_bytes_ = max_storage_bytes;
  this->slots_.assign(max_commands, Command{});
}

void CommandStore::mint_identity_() {
  if (!store_random_bytes(this->bridge_id_, BRIDGE_ID_BYTES)) {
    // Leaves the id all-zero rather than shipping one that collides with every other failed mint.
    std::memset(this->bridge_id_, 0, BRIDGE_ID_BYTES);
  }
  this->next_command_id_ = COMMAND_ID_FIRST;
  this->revision_ = REVISION_INITIAL;
  this->restore_state_ = 0;
}

void CommandStore::enter_read_only_(LoadReport &report, StoreFault fault) {
  this->read_only_ = true;
  this->fault_ = fault;
  report.read_only = true;
  report.fault = fault;
}

StoreResult CommandStore::writable_() const {
  if (this->backend_ == nullptr) {
    return StoreResult::STORAGE_ERROR;
  }
  if (this->read_only_) {
    return StoreResult::READ_ONLY;
  }
  if (this->restore_state_ != 0) {
    // A half-restored registry is not a registry to learn into. Finish the restore, or clear to
    // abandon it.
    return StoreResult::RESTORE_IN_PROGRESS;
  }
  return StoreResult::OK;
}

LoadReport CommandStore::begin() {
  LoadReport report{};
  this->read_only_ = false;
  this->fault_ = StoreFault::NONE;

  if (this->backend_ == nullptr) {
    report.header_invalid = true;
    this->enter_read_only_(report, StoreFault::HEADER_INVALID);
    return report;
  }

  StoredHeader header{};
  if (!this->backend_->load(this->header_key_(), reinterpret_cast<uint8_t *>(&header), sizeof(header))) {
    // No record at all: a fresh device. Mint an identity and write it now, so the bridge_id is
    // stable from first boot rather than changing until the first learn.
    this->mint_identity_();
    // A failure here costs nothing recoverable, so the store stays writable and the next write
    // persists the identity instead.
    this->write_index_();
    this->backend_->sync();
    return report;
  }

  report.had_stored_data = true;
  report.stored_format_version = header.format_version;
  report.stored_slot_count = header.slot_count;

  const uint16_t expected_crc = crc16_ccitt(reinterpret_cast<const uint8_t *>(&header), sizeof(header) - CRC_BYTES);
  if (header.magic != STORE_MAGIC || header.crc16 != expected_crc || header.slot_count == 0) {
    report.header_invalid = true;
    this->enter_read_only_(report, StoreFault::HEADER_INVALID);
    return report;
  }

  if (header.format_version != STORE_FORMAT_VERSION) {
    // Refuse to reinterpret a format this firmware does not write. Flash stays intact so another
    // build can read it, and the session is read-only so nothing overwrites it meanwhile.
    report.version_mismatch = true;
    this->enter_read_only_(report, StoreFault::VERSION_UNSUPPORTED);
    return report;
  }

  this->load_index_(header.slot_count, header.index_bytes, report);
  return report;
}

void CommandStore::load_index_(uint8_t stored_slot_count, uint16_t stored_index_bytes, LoadReport &report) {
  const size_t expected = index_bytes_for(stored_slot_count);
  if (stored_index_bytes != expected) {
    // The version matches but the index is not the shape this firmware writes. Pre-release builds
    // land here, as would a corrupted length field. Either way it is not ours to rewrite.
    report.header_invalid = true;
    this->enter_read_only_(report, StoreFault::HEADER_INVALID);
    return;
  }

  std::vector<uint8_t> raw(expected);
  if (!this->backend_->load(this->index_key_(), raw.data(), raw.size())) {
    this->enter_read_only_(report, StoreFault::INDEX_INVALID);
    return;
  }
  uint16_t stored_crc = 0;
  std::memcpy(&stored_crc, raw.data() + raw.size() - CRC_BYTES, CRC_BYTES);
  if (crc16_ccitt(raw.data(), raw.size() - CRC_BYTES) != stored_crc) {
    this->enter_read_only_(report, StoreFault::INDEX_INVALID);
    return;
  }

  StoredMeta meta{};
  std::memcpy(&meta, raw.data(), sizeof(meta));
  std::memcpy(this->bridge_id_, meta.bridge_id, BRIDGE_ID_BYTES);
  this->next_command_id_ = meta.next_command_id;
  this->revision_ = meta.revision;
  this->restore_state_ = meta.restore_state;
  report.restore_incomplete = meta.restore_state != 0;
  report.slot_count_changed = stored_slot_count != this->max_commands_;

  for (uint8_t i = 0; i < stored_slot_count; i++) {
    StoredSlot slot{};
    std::memcpy(&slot, raw.data() + sizeof(StoredMeta) + static_cast<size_t>(i) * sizeof(StoredSlot), sizeof(slot));
    slot.name[NAME_BUF_LEN - 1] = '\0';
    if (slot.command_id == COMMAND_ID_NONE || slot.name[0] == '\0' || slot.pulse_count == 0) {
      continue;  // free slot
    }
    if (i >= this->max_commands_ || slot.pulse_count > this->max_pulses_) {
      // max_commands or max_pulses was reduced in YAML; flash keeps the record until the next
      // write.
      report.dropped_unfittable++;
      continue;
    }

    std::vector<int32_t> timings(slot.pulse_count);
    if (!this->backend_->load(this->payload_key_(i), reinterpret_cast<uint8_t *>(timings.data()),
                              timings.size() * sizeof(int32_t)) ||
        timings_crc(timings) != slot.payload_crc16) {
      // Missing, the wrong length, or failed its CRC. The index still names this command, so a
      // later write would quietly drop it; the session goes read-only below instead.
      if (report.payload_faults == 0) {
        report.fault_slot = i;
        report.fault_name = slot.name;
      }
      report.payload_faults++;
      continue;
    }

    Command &target = this->slots_[i];
    target.command_id = slot.command_id;
    target.name = slot.name;
    target.timings = std::move(timings);
    target.gap_us = slot.gap_us;
    target.repeat_times = slot.repeat_times;
    target.frequency_hz = slot.frequency_hz;
    target.modulation = slot.modulation;
    report.loaded++;
  }

  if (report.payload_faults > 0) {
    // Everything that did read cleanly stays available for reading, sending and export; only
    // writes are refused, so nothing rewrites the index over the damaged command.
    this->enter_read_only_(report, StoreFault::PAYLOAD_INVALID);
  }
}

int CommandStore::find_slot_(const std::string &name) const {
  for (size_t i = 0; i < this->slots_.size(); i++) {
    if (this->slots_[i].command_id != COMMAND_ID_NONE && this->slots_[i].name == name) {
      return static_cast<int>(i);
    }
  }
  return -1;
}

int CommandStore::find_slot_by_id_(uint32_t command_id) const {
  if (command_id == COMMAND_ID_NONE) {
    return -1;
  }
  for (size_t i = 0; i < this->slots_.size(); i++) {
    if (this->slots_[i].command_id == command_id) {
      return static_cast<int>(i);
    }
  }
  return -1;
}

int CommandStore::find_free_slot_() const {
  for (size_t i = 0; i < this->slots_.size(); i++) {
    if (this->slots_[i].command_id == COMMAND_ID_NONE) {
      return static_cast<int>(i);
    }
  }
  return -1;
}

uint32_t CommandStore::used_bytes() const {
  uint32_t total = index_overhead_bytes(this->max_commands_);
  for (const Command &command : this->slots_) {
    if (command.command_id != COMMAND_ID_NONE) {
      total += static_cast<uint32_t>(command.timings.size() * sizeof(int32_t));
    }
  }
  return total;
}

uint32_t CommandStore::worst_case_bytes() const {
  return index_overhead_bytes(this->max_commands_) +
         static_cast<uint32_t>(this->max_commands_) * this->max_pulses_ * sizeof(int32_t);
}

uint32_t CommandStore::projected_bytes_(int replacing_slot, uint16_t new_pulse_count) const {
  uint32_t total = index_overhead_bytes(this->max_commands_);
  for (size_t i = 0; i < this->slots_.size(); i++) {
    if (static_cast<int>(i) == replacing_slot) {
      continue;
    }
    if (this->slots_[i].command_id != COMMAND_ID_NONE) {
      total += static_cast<uint32_t>(this->slots_[i].timings.size() * sizeof(int32_t));
    }
  }
  return total + static_cast<uint32_t>(new_pulse_count) * sizeof(int32_t);
}

StoreResult CommandStore::put(const Command &command) {
  const StoreResult gate = this->writable_();
  if (gate != StoreResult::OK) {
    return gate;
  }
  const StoreResult name_check = validate_name(command.name);
  if (name_check != StoreResult::OK) {
    return name_check;
  }
  if (command.timings.empty()) {
    return StoreResult::TOO_MANY_PULSES;  // an empty waveform is not storable either
  }
  if (command.timings.size() > this->max_pulses_) {
    return StoreResult::TOO_MANY_PULSES;
  }

  int slot = this->find_slot_(command.name);
  const bool overwriting = slot >= 0;
  uint32_t command_id;
  if (overwriting) {
    // Relearning under an existing name keeps that command's id, so the Home Assistant entity,
    // its dashboard cards and its history survive the relearn.
    command_id = this->slots_[slot].command_id;
  } else {
    slot = this->find_free_slot_();
    if (slot < 0) {
      return StoreResult::STORE_FULL;
    }
    if (this->next_command_id_ > COMMAND_ID_LAST) {
      return StoreResult::ID_EXHAUSTED;
    }
    command_id = this->next_command_id_;
  }

  if (this->projected_bytes_(overwriting ? slot : -1, static_cast<uint16_t>(command.timings.size())) >
      this->max_storage_bytes_) {
    return StoreResult::BUDGET_EXCEEDED;
  }

  // Retained so a failed write can be rolled back in RAM as well as on flash.
  Command previous = this->slots_[slot];
  const uint32_t previous_next_id = this->next_command_id_;
  const uint32_t previous_revision = this->revision_;

  this->slots_[slot] = command;
  this->slots_[slot].command_id = command_id;
  if (!overwriting) {
    this->next_command_id_ = command_id + 1;
  }
  this->revision_++;

  // Payload first: a half-applied write is invisible until the index references it.
  if (!this->write_payload_(static_cast<uint8_t>(slot)) || !this->write_index_()) {
    this->slots_[slot] = std::move(previous);
    this->next_command_id_ = previous_next_id;
    this->revision_ = previous_revision;
    return StoreResult::STORAGE_ERROR;
  }
  this->backend_->sync();
  return StoreResult::OK;
}

bool CommandStore::get(const std::string &name, Command &out) const {
  const int slot = this->find_slot_(name);
  if (slot < 0) {
    return false;
  }
  out = this->slots_[slot];
  return true;
}

bool CommandStore::get_by_id(uint32_t command_id, Command &out) const {
  const int slot = this->find_slot_by_id_(command_id);
  if (slot < 0) {
    return false;
  }
  out = this->slots_[slot];
  return true;
}

bool CommandStore::has(const std::string &name) const { return this->find_slot_(name) >= 0; }

bool CommandStore::has_id(uint32_t command_id) const { return this->find_slot_by_id_(command_id) >= 0; }

StoreResult CommandStore::rename(uint32_t command_id, const std::string &new_name) {
  const StoreResult gate = this->writable_();
  if (gate != StoreResult::OK) {
    return gate;
  }
  const StoreResult name_check = validate_name(new_name);
  if (name_check != StoreResult::OK) {
    return name_check;
  }
  const int slot = this->find_slot_by_id_(command_id);
  if (slot < 0) {
    return StoreResult::NOT_FOUND;
  }
  if (this->slots_[slot].name == new_name) {
    return StoreResult::OK;  // no-op; not worth a flash write or a revision bump
  }
  if (this->find_slot_(new_name) >= 0) {
    return StoreResult::NAME_TAKEN;
  }

  const std::string previous = this->slots_[slot].name;
  const uint32_t previous_revision = this->revision_;
  this->slots_[slot].name = new_name;
  this->revision_++;

  // Index only: the waveform record is untouched, so a rename cannot damage it.
  if (!this->write_index_()) {
    this->slots_[slot].name = previous;
    this->revision_ = previous_revision;
    return StoreResult::STORAGE_ERROR;
  }
  this->backend_->sync();
  return StoreResult::OK;
}

StoreResult CommandStore::remove(const std::string &name) {
  const StoreResult gate = this->writable_();
  if (gate != StoreResult::OK) {
    return gate;
  }
  const int slot = this->find_slot_(name);
  if (slot < 0) {
    return StoreResult::NOT_FOUND;
  }
  return this->remove_by_id(this->slots_[slot].command_id);
}

StoreResult CommandStore::remove_by_id(uint32_t command_id) {
  const StoreResult gate = this->writable_();
  if (gate != StoreResult::OK) {
    return gate;
  }
  const int slot = this->find_slot_by_id_(command_id);
  if (slot < 0) {
    return StoreResult::NOT_FOUND;
  }

  Command previous = std::move(this->slots_[slot]);
  const uint32_t previous_revision = this->revision_;
  this->slots_[slot] = Command{};
  this->revision_++;

  // Only the index is rewritten; the orphaned payload stays on flash until the slot is reused
  // and is not counted by used_bytes(). next_command_id is untouched, so the freed id is gone
  // for good.
  if (!this->write_index_()) {
    this->slots_[slot] = std::move(previous);
    this->revision_ = previous_revision;
    return StoreResult::STORAGE_ERROR;
  }
  this->backend_->sync();
  return StoreResult::OK;
}

StoreResult CommandStore::clear_all() {
  if (this->backend_ == nullptr) {
    return StoreResult::STORAGE_ERROR;
  }
  if (this->read_only_) {
    return StoreResult::READ_ONLY;
  }

  std::vector<Command> previous = this->slots_;
  const uint32_t previous_revision = this->revision_;
  const uint8_t previous_restore_state = this->restore_state_;
  this->slots_.assign(this->max_commands_, Command{});
  // Clearing is also how an unfinished restore is abandoned; the identity it adopted is kept,
  // because adopting it was explicit and a retry needs it.
  this->restore_state_ = 0;
  this->revision_++;

  // bridge_id and next_command_id are deliberately preserved: clearing the commands does not make
  // this a different logical bridge, and an id that has been handed out once must never come back.
  if (!this->write_index_()) {
    this->slots_ = std::move(previous);
    this->restore_state_ = previous_restore_state;
    this->revision_ = previous_revision;
    return StoreResult::STORAGE_ERROR;
  }
  this->backend_->sync();
  return StoreResult::OK;
}

StoreResult CommandStore::factory_reset() {
  if (this->backend_ == nullptr) {
    return StoreResult::STORAGE_ERROR;
  }

  // Deliberately ignores read_only_: this is the way out of it. Only this component's own records
  // are written, so unrelated ESPHome preferences are untouched.
  this->slots_.assign(this->max_commands_, Command{});
  this->mint_identity_();
  if (!this->write_index_()) {
    return StoreResult::STORAGE_ERROR;
  }
  this->backend_->sync();
  this->read_only_ = false;
  this->fault_ = StoreFault::NONE;
  return StoreResult::OK;
}

StoreResult CommandStore::restore_begin(const uint8_t *bridge_id, uint32_t next_command_id) {
  if (this->backend_ == nullptr) {
    return StoreResult::STORAGE_ERROR;
  }
  if (this->read_only_) {
    return StoreResult::READ_ONLY;
  }
  if (this->count() != 0) {
    // The one guard that makes "never overwrite a live bridge" enforceable in firmware rather
    // than in policy. Clearing first is an explicit, separate decision.
    return StoreResult::RESTORE_NOT_EMPTY;
  }
  if (next_command_id < COMMAND_ID_FIRST || next_command_id > COMMAND_ID_LAST + 1) {
    return StoreResult::ID_CONFLICT;
  }

  uint8_t previous_bridge[BRIDGE_ID_BYTES];
  std::memcpy(previous_bridge, this->bridge_id_, BRIDGE_ID_BYTES);
  const uint32_t previous_next_id = this->next_command_id_;
  const uint32_t previous_revision = this->revision_;
  const uint8_t previous_restore_state = this->restore_state_;

  std::memcpy(this->bridge_id_, bridge_id, BRIDGE_ID_BYTES);
  this->next_command_id_ = next_command_id;
  this->restore_state_ = 1;
  this->revision_++;

  if (!this->write_index_()) {
    std::memcpy(this->bridge_id_, previous_bridge, BRIDGE_ID_BYTES);
    this->next_command_id_ = previous_next_id;
    this->restore_state_ = previous_restore_state;
    this->revision_ = previous_revision;
    return StoreResult::STORAGE_ERROR;
  }
  this->backend_->sync();
  return StoreResult::OK;
}

StoreResult CommandStore::import_command(const Command &command) {
  if (this->backend_ == nullptr) {
    return StoreResult::STORAGE_ERROR;
  }
  if (this->read_only_) {
    return StoreResult::READ_ONLY;
  }
  if (this->restore_state_ == 0) {
    // Importing outside a restore would let an id be written that next_command_id knows nothing
    // about, so the two could disagree.
    return StoreResult::RESTORE_NOT_ACTIVE;
  }
  const StoreResult name_check = validate_name(command.name);
  if (name_check != StoreResult::OK) {
    return name_check;
  }
  if (command.command_id == COMMAND_ID_NONE || command.command_id >= this->next_command_id_ ||
      this->has_id(command.command_id)) {
    return StoreResult::ID_CONFLICT;
  }
  if (this->has(command.name)) {
    return StoreResult::NAME_TAKEN;
  }
  if (command.timings.empty() || command.timings.size() > this->max_pulses_) {
    return StoreResult::TOO_MANY_PULSES;
  }

  const int slot = this->find_free_slot_();
  if (slot < 0) {
    return StoreResult::STORE_FULL;
  }
  if (this->projected_bytes_(-1, static_cast<uint16_t>(command.timings.size())) > this->max_storage_bytes_) {
    return StoreResult::BUDGET_EXCEEDED;
  }

  const uint32_t previous_revision = this->revision_;
  this->slots_[slot] = command;
  this->revision_++;

  if (!this->write_payload_(static_cast<uint8_t>(slot)) || !this->write_index_()) {
    this->slots_[slot] = Command{};
    this->revision_ = previous_revision;
    return StoreResult::STORAGE_ERROR;
  }
  this->backend_->sync();
  return StoreResult::OK;
}

StoreResult CommandStore::restore_commit() {
  if (this->backend_ == nullptr) {
    return StoreResult::STORAGE_ERROR;
  }
  if (this->read_only_) {
    return StoreResult::READ_ONLY;
  }
  if (this->restore_state_ == 0) {
    return StoreResult::RESTORE_NOT_ACTIVE;
  }

  const uint32_t previous_revision = this->revision_;
  this->restore_state_ = 0;
  this->revision_++;

  if (!this->write_index_()) {
    this->restore_state_ = 1;
    this->revision_ = previous_revision;
    return StoreResult::STORAGE_ERROR;
  }
  this->backend_->sync();
  return StoreResult::OK;
}

size_t CommandStore::count() const {
  size_t total = 0;
  for (const Command &command : this->slots_) {
    if (command.command_id != COMMAND_ID_NONE) {
      total++;
    }
  }
  return total;
}

std::vector<std::string> CommandStore::names() const {
  std::vector<std::string> result;
  for (const Command &command : this->slots_) {
    if (command.command_id != COMMAND_ID_NONE) {
      result.push_back(command.name);
    }
  }
  return result;
}

std::vector<CommandEntry> CommandStore::entries() const {
  std::vector<CommandEntry> result;
  for (const Command &command : this->slots_) {
    if (command.command_id != COMMAND_ID_NONE) {
      result.push_back(
          CommandEntry{command.command_id, command.name, static_cast<uint16_t>(command.timings.size())});
    }
  }
  return result;
}

bool CommandStore::write_payload_(uint8_t slot) {
  const Command &command = this->slots_[slot];
  return this->backend_->save(this->payload_key_(slot), reinterpret_cast<const uint8_t *>(command.timings.data()),
                              command.timings.size() * sizeof(int32_t));
}

bool CommandStore::write_index_() {
  const size_t total = index_bytes_for(this->max_commands_);
  std::vector<uint8_t> raw(total, 0);

  StoredMeta meta{};
  std::memcpy(meta.bridge_id, this->bridge_id_, BRIDGE_ID_BYTES);
  meta.next_command_id = this->next_command_id_;
  meta.revision = this->revision_;
  meta.restore_state = this->restore_state_;
  std::memcpy(raw.data(), &meta, sizeof(meta));

  for (uint8_t i = 0; i < this->max_commands_; i++) {
    StoredSlot slot{};
    const Command &command = this->slots_[i];
    if (command.command_id != COMMAND_ID_NONE) {
      std::strncpy(slot.name, command.name.c_str(), NAME_BUF_LEN - 1);
      slot.command_id = command.command_id;
      slot.pulse_count = static_cast<uint16_t>(command.timings.size());
      slot.repeat_times = command.repeat_times;
      slot.gap_us = command.gap_us;
      slot.frequency_hz = command.frequency_hz;
      slot.modulation = command.modulation;
      slot.payload_crc16 = timings_crc(command.timings);
    }
    std::memcpy(raw.data() + sizeof(StoredMeta) + static_cast<size_t>(i) * sizeof(StoredSlot), &slot, sizeof(slot));
  }

  const uint16_t index_crc = crc16_ccitt(raw.data(), raw.size() - CRC_BYTES);
  std::memcpy(raw.data() + raw.size() - CRC_BYTES, &index_crc, CRC_BYTES);
  if (!this->backend_->save(this->index_key_(), raw.data(), raw.size())) {
    return false;
  }

  StoredHeader header{};
  header.magic = STORE_MAGIC;
  header.format_version = STORE_FORMAT_VERSION;
  header.slot_count = this->max_commands_;
  header.index_bytes = static_cast<uint16_t>(total);
  header.reserved = 0;
  header.crc16 = crc16_ccitt(reinterpret_cast<const uint8_t *>(&header), sizeof(header) - CRC_BYTES);
  return this->backend_->save(this->header_key_(), reinterpret_cast<const uint8_t *>(&header), sizeof(header));
}

}  // namespace esphome::rf_cloner
